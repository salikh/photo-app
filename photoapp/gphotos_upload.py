"""The `gphotos_upload` job (ticket 212): send a finished export to Google Photos as an album.

start_export queues one export job per Photo and then one of these, whose `options` JSON carries the
whole request: {"album_id" | "album_title", "items": [{"file_id", "dest", "export_job_id"}, ...]} in
the grid order the album should have. The handler waits for those export jobs to finish, uploads the
bytes of each chunk of 50 in parallel, then makes the one serial batchCreate for the chunk, so items
land in the album in order. Identical bytes are one media item on Google's side, so re-running (or
retrying the failures) never duplicates anything. A file that fails is recorded in options["failed"]
and the job still finishes; only a fatal problem (not authorized, album cannot be created) fails it.
Nothing about the album is stored in the photo database: Google Photos is authoritative.
"""

import concurrent.futures
import json
import mimetypes
import os
import time

from absl import logging

from photoapp import export
from photoapp import gphotos

UPLOAD_THREADS = 4
POLL_SECONDS = 1.0


def mime_type(path):
  guessed, _ = mimetypes.guess_type(path)
  return guessed or "application/octet-stream"


def _save(conn, job_id, options, note):
  conn.execute("UPDATE jobs SET options = ?, note = ? WHERE id = ?",
               (json.dumps(options), note, job_id))
  conn.commit()


def _wait_for_exports(conn, items, sleep):
  """Block until every item's export job has finished; returns {export_job_id: (state, error)}."""
  ids = [i["export_job_id"] for i in items if i.get("export_job_id")]
  while True:
    states = {}
    for k in range(0, len(ids), 500):
      chunk = ids[k:k + 500]
      for r in conn.execute(f"SELECT id, state, error FROM jobs WHERE id IN "
                            f"({','.join('?' * len(chunk))})", chunk):
        states[r["id"]] = (r["state"], r["error"])
    # a job pruned or missing counts as finished (its file check below decides)
    if not any(s[0] in ("queued", "running") for s in states.values()):
      return states
    sleep(POLL_SECONDS)


def run(conn, settings, job, client, sleep=time.sleep, threads=UPLOAD_THREADS):
  options = json.loads(job["options"])
  items = options["items"]
  failed = []
  exports = _wait_for_exports(conn, items, sleep)
  ready = []
  for it in items:
    state = exports.get(it.get("export_job_id"))
    if state and state[0] == "failed":
      failed.append({**it, "message": f"export failed: {state[1]}"})
      continue
    path = export.file_to_upload(conn, settings, it["file_id"], it["dest"])
    if not os.path.isfile(path):
      failed.append({**it, "message": "the exported file is missing"})
      continue
    ready.append((it, path))

  total, sent = len(items), 0
  album_id = options.get("album_id")

  def note():
    return (f"uploaded {sent} of {total}" + (f", {len(failed)} failed" if failed else ""))

  _save(conn, job["id"], {**options, "failed": failed}, note())
  with concurrent.futures.ThreadPoolExecutor(threads) as pool:
    for k in range(0, len(ready), gphotos.BATCH):
      chunk = ready[k:k + gphotos.BATCH]
      futures = [pool.submit(client.upload_bytes, p, mime_type(p)) for _, p in chunk]
      tokens = []
      for (it, _), f in zip(chunk, futures):
        try:
          tokens.append((it, f.result()))
        except gphotos.GPhotosError as e:
          failed.append({**it, "message": str(e)})
      if tokens:
        if album_id is None:
          album_id = client.create_album(options["album_title"])["id"]
          options["album_id"] = album_id        # saved at once: a restart must not make a 2nd album
          _save(conn, job["id"], {**options, "failed": failed}, note())
        results = client.batch_create([(t, None) for _, t in tokens], album_id)
        for (it, _), r in zip(tokens, results):
          if r["ok"]:
            sent += 1
          else:
            failed.append({**it, "message": r["message"] or "rejected by Google Photos"})
      _save(conn, job["id"], {**options, "failed": failed}, note())
  logging.vlog(3, "gphotos_upload %d: %s", job["id"], note())
