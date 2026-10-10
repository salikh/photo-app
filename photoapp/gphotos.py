"""Minimal Google Photos Library API client (ticket 211): upload bytes, create media items, create
and list albums, add items to an album. stdlib only. The transport and the sleep function are
injectable, and the API root is a setting, so tests run against a local fake server
(tests/fake_gphotos.py) and never upload anything real.

Since March 2025 the API only sees albums and items this app created (see docs/google-photos.md).
Limits honoured here: 50 items per batch call, batch calls are made one at a time, 429 waits at least
30 s, 5xx is retried a few times.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from absl import logging

BATCH = 50
RETRY_5XX = 4
MIN_429_WAIT = 30


class GPhotosError(Exception):
  def __init__(self, message, status=None):
    super().__init__(message)
    self.status = status


def urllib_transport(method, url, headers, body, timeout=600):
  """body is bytes, a path-like ('file', path) tuple (streamed), or None. Returns (status, bytes)."""
  fobj = None
  if isinstance(body, tuple) and body[0] == "file":
    fobj = open(body[1], "rb")
    headers = dict(headers, **{"Content-Length": str(os.path.getsize(body[1]))})
    data = fobj
  else:
    data = body
  req = urllib.request.Request(url, data=data, headers=headers, method=method)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as r:
      return r.status, r.read()
  except urllib.error.HTTPError as e:
    return e.code, e.read()
  except (urllib.error.URLError, OSError) as e:
    raise GPhotosError(f"cannot reach Google Photos: {e}") from e
  finally:
    if fobj:
      fobj.close()


def _message(raw):
  try:
    return json.loads(raw)["error"]["message"]
  except (ValueError, KeyError, TypeError):
    return raw[:200].decode(errors="replace")


class Client:
  def __init__(self, base_url, token_fn, transport=urllib_transport, sleep=time.sleep):
    self.base = base_url.rstrip("/")
    self._token, self._transport, self._sleep = token_fn, transport, sleep

  def _call(self, method, path, body=None, headers=None, query=None):
    url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
    attempt = 0
    while True:
      h = {"Authorization": "Bearer " + self._token()}
      h.update(headers or {})
      status, raw = self._transport(method, url, h, body)
      if status == 429:
        attempt += 1
        if attempt > 6:
          raise GPhotosError("Google Photos keeps answering 'too many requests'", status)
        wait = max(MIN_429_WAIT, 2 ** attempt * 5)
        logging.warning("Google Photos 429 on %s; waiting %d s", path, wait)
        self._sleep(wait)
        continue
      if status >= 500 and attempt < RETRY_5XX:
        attempt += 1
        self._sleep(2 ** attempt)
        continue
      return status, raw

  def _json(self, method, path, payload=None, query=None):
    status, raw = self._call(method, path, json.dumps(payload).encode() if payload is not None
                             else None, {"Content-Type": "application/json"}, query)
    if status not in (200, 207):
      raise GPhotosError(f"Google Photos {method} {path}: {status} {_message(raw)}", status)
    return json.loads(raw) if raw else {}

  def upload_bytes(self, path, mime_type):
    """Upload one file's bytes; returns the upload token (valid for a day)."""
    status, raw = self._call("POST", "/v1/uploads", ("file", path), {
        "Content-Type": "application/octet-stream", "X-Goog-Upload-Protocol": "raw",
        "X-Goog-Upload-Content-Type": mime_type,
        "X-Goog-Upload-File-Name": urllib.parse.quote(os.path.basename(path))})
    if status != 200 or not raw:
      raise GPhotosError(f"upload of {os.path.basename(path)} failed: {status} {_message(raw)}",
                         status)
    return raw.decode()

  def batch_create(self, items, album_id=None):
    """items: [(upload_token, description or None)], at most BATCH. Returns one dict per item, in
    order: {"ok": bool, "id": media item id or None, "message": str}."""
    if len(items) > BATCH:
      raise ValueError(f"at most {BATCH} items per batchCreate")
    payload = {"newMediaItems": [
        {"simpleMediaItem": {"uploadToken": tok}, **({"description": d} if d else {})}
        for tok, d in items]}
    if album_id:
      payload["albumId"] = album_id
    out = self._json("POST", "/v1/mediaItems:batchCreate", payload)
    results = out.get("newMediaItemResults", [])
    if len(results) != len(items):
      raise GPhotosError(f"batchCreate answered {len(results)} results for {len(items)} items")
    parsed = []
    for r in results:
      st = r.get("status", {})
      item = r.get("mediaItem")
      ok = item is not None and st.get("code", 0) == 0
      parsed.append({"ok": ok, "id": item["id"] if ok else None,
                     "message": st.get("message", "")})
    return parsed

  def create_album(self, title):
    a = self._json("POST", "/v1/albums", {"album": {"title": title}})
    return {"id": a["id"], "title": a.get("title", title)}

  def list_albums(self):
    """Every album this app created (the API shows no others), as [{"id", "title", "count"}]."""
    albums, token = [], None
    while True:
      q = {"pageSize": 50, **({"pageToken": token} if token else {})}
      page = self._json("GET", "/v1/albums", query=q)
      albums += [{"id": a["id"], "title": a.get("title", ""),
                  "count": int(a.get("mediaItemsCount", 0))} for a in page.get("albums", [])]
      token = page.get("nextPageToken")
      if not token:
        return albums

  def add_to_album(self, album_id, media_item_ids):
    for i in range(0, len(media_item_ids), BATCH):
      self._json("POST", f"/v1/albums/{urllib.parse.quote(album_id)}:batchAddMediaItems",
                 {"mediaItemIds": media_item_ids[i:i + BATCH]})
