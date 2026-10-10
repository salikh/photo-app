"""A tiny fake of the Google Photos Library API for tests (ticket 211). Real HTTP on 127.0.0.1 in a
thread, so the real client code (urllib, streaming, headers) is exercised. Identical bytes get the
same media item id, like the real service. `inject(path, [status, ...])` makes the next calls to
that path answer with those statuses (e.g. 429, 500) before behaving normally.
"""

import hashlib
import http.server
import json
import threading
import urllib.parse

TOKEN = "fake-access-token"


class FakeGPhotos:
  def __init__(self):
    self.uploads = {}        # upload token -> (bytes, filename, mime)
    self.items = {}          # media id -> {"filename", "sha", "mime"}
    self.albums = {}         # album id -> {"title", "items": [media ids]}
    self.injected = {}       # path -> [status, ...]
    self.reject_filenames = set()
    self.calls = []          # (method, path) log
    self.batch_sizes = []
    self.page_size = 50
    self._n = 0
    self._lock = threading.Lock()
    fake = self

    class Handler(http.server.BaseHTTPRequestHandler):
      def log_message(self, *a):
        pass

      def _reply(self, status, body=b"", ctype="application/json"):
        if not isinstance(body, bytes):
          body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

      def _handle(self, method):
        path = urllib.parse.urlparse(self.path).path
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with fake._lock:
          fake.calls.append((method, path))
          queue = fake.injected.get(path)
          if queue:
            status = queue.pop(0)
            return self._reply(status, {"error": {"message": f"injected {status}"}})
          if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._reply(401, {"error": {"message": "bad token"}})
          return fake._route(self, method, path, query, raw)

      def do_GET(self):
        self._handle("GET")

      def do_POST(self):
        self._handle("POST")

    self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    self.url = f"http://127.0.0.1:{self.httpd.server_port}"
    self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

  def start(self):
    self.thread.start()
    return self

  def stop(self):
    self.httpd.shutdown()
    self.httpd.server_close()

  def inject(self, path, statuses):
    self.injected.setdefault(path, []).extend(statuses)

  def _id(self, prefix):
    self._n += 1
    return f"{prefix}{self._n}"

  def _route(self, h, method, path, query, raw):
    if method == "POST" and path == "/v1/uploads":
      tok = self._id("tok")
      name = urllib.parse.unquote(h.headers.get("X-Goog-Upload-File-Name", ""))
      self.uploads[tok] = (raw, name, h.headers.get("X-Goog-Upload-Content-Type", ""))
      return h._reply(200, tok.encode(), "text/plain")
    if method == "POST" and path == "/v1/mediaItems:batchCreate":
      body = json.loads(raw)
      self.batch_sizes.append(len(body["newMediaItems"]))
      if len(body["newMediaItems"]) > 50:
        return h._reply(400, {"error": {"message": "too many items"}})
      album = self.albums.get(body.get("albumId")) if body.get("albumId") else None
      if body.get("albumId") and album is None:
        return h._reply(400, {"error": {"message": "unknown album"}})
      results = []
      for it in body["newMediaItems"]:
        tok = it["simpleMediaItem"]["uploadToken"]
        if tok not in self.uploads:
          results.append({"uploadToken": tok, "status": {"code": 3, "message": "bad token"}})
          continue
        data, name, mime = self.uploads[tok]
        if name in self.reject_filenames:
          results.append({"uploadToken": tok, "status": {"code": 3, "message": "rejected"}})
          continue
        sha = hashlib.sha256(data).hexdigest()
        mid = next((k for k, v in self.items.items() if v["sha"] == sha), None)
        if mid is None:
          mid = self._id("mid")
          self.items[mid] = {"filename": name, "sha": sha, "mime": mime,
                             "description": it.get("description")}
        if album is not None and mid not in album["items"]:
          album["items"].append(mid)
        results.append({"uploadToken": tok, "status": {"message": "Success"},
                        "mediaItem": {"id": mid, "filename": name, "mimeType": mime}})
      return h._reply(200, {"newMediaItemResults": results})
    if method == "POST" and path == "/v1/albums":
      aid = self._id("alb")
      self.albums[aid] = {"title": json.loads(raw)["album"]["title"], "items": []}
      return h._reply(200, {"id": aid, "title": self.albums[aid]["title"]})
    if method == "GET" and path == "/v1/albums":
      ids = list(self.albums)
      start = int(query.get("pageToken") or 0)
      page = ids[start:start + self.page_size]
      out = {"albums": [{"id": a, "title": self.albums[a]["title"],
                         "mediaItemsCount": str(len(self.albums[a]["items"]))} for a in page]}
      if start + self.page_size < len(ids):
        out["nextPageToken"] = str(start + self.page_size)
      return h._reply(200, out)
    if method == "POST" and path.startswith("/v1/albums/") and path.endswith(":batchAddMediaItems"):
      aid = urllib.parse.unquote(path[len("/v1/albums/"):-len(":batchAddMediaItems")])
      ids = json.loads(raw)["mediaItemIds"]
      if aid not in self.albums or len(ids) > 50:
        return h._reply(400, {"error": {"message": "bad request"}})
      for m in ids:
        if m not in self.albums[aid]["items"]:
          self.albums[aid]["items"].append(m)
      return h._reply(200, {})
    return h._reply(404, {"error": {"message": "no such endpoint"}})
