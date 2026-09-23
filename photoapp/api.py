"""FastAPI application."""

import dataclasses
import functools
import os
import sqlite3
import threading
import time
import uuid

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.responses import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
import pydantic
from absl import logging

from photoapp import curation
from photoapp import db as db_lib
from photoapp import fileinfo
from photoapp import grouping
from photoapp import jobs
from photoapp import library
from photoapp import manual_links
from photoapp import recovery
from photoapp import scan as scan_lib
from photoapp import thumb_populate
from photoapp import thumbs
from photoapp import trash

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
WEB_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class RatingBody(pydantic.BaseModel):
  rating: int


class FavBody(pydantic.BaseModel):
  fav: bool


class BatchRatingBody(pydantic.BaseModel):
  ids: list[int]
  rating: int


class TrashBody(pydantic.BaseModel):
  ids: list[int]


class RepresentativeBody(pydantic.BaseModel):
  file_id: int | None = None


class LinkBody(pydantic.BaseModel):
  target_file_id: int
  role: str = "tuning"


class TagsBody(pydantic.BaseModel):
  add: list[str] = []
  remove: list[str] = []


def create_app(conn, settings):
  """Build the app around an open (migrated) database and its settings."""
  app = FastAPI(title="photos")
  app.state.db = conn
  app.state.settings = settings
  app.state.db_lock = threading.Lock()
  app.state.sleep = time.sleep      # injectable: tests do not want to wait

  def run_db(fn):
    """Run one database operation; if the database is busy (another writer holds the
    lock) retry it with growing waits for about settings.busy_retry_seconds, then answer
    500. The waits happen outside the in-process lock, and the connection is rolled back
    before every retry so nothing half-done is kept."""
    def rollback():
      with app.state.db_lock:
        try:
          app.state.db.rollback()
        except sqlite3.Error:
          pass
    try:
      return db_lib.retry_busy(fn, settings.busy_retry_seconds,
                               sleep=lambda s: app.state.sleep(s), on_retry=rollback)
    except db_lib.DatabaseBusy as e:
      logging.error("giving up: %s", e)
      raise HTTPException(500, "internal server error: database busy")

  def db_route(fn):
    """Route decorator: re-run the whole route function while the database is busy."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
      return run_db(lambda: fn(*args, **kwargs))
    return wrapper
  hashes = (fileinfo.load_precomputed_hashes(settings.hashes_db)
            if settings.hashes_db else None)
  on_scan_done = lambda conn: recovery.recover(conn, settings)
  app.state.scanner = scan_lib.ScanManager(
      settings.db_path, settings.pictures_dir, hashes, settings.thumbs_dir,
      on_done=on_scan_done, workers=settings.scan_workers)

  def raw_render(conn, job):
    row = conn.execute("SELECT id, path FROM files WHERE id = ?",
                       (job["file_id"],)).fetchone()
    if row is None:
      raise RuntimeError("file is gone")
    made = thumbs.render_raw_sizes(settings.pictures_dir, settings.thumbs_dir,
                                   row["path"])
    if not made:
      raise RuntimeError("LibRaw could not decode the file")
    for size, path in made.items():
      thumbs.record(conn, row["id"], size, path, "libraw")
    conn.commit()
    logging.vlog(5, "%s: raw_render done (%s)", row["path"], ", ".join(made))

  app.state.jobs = jobs.JobQueue(settings.db_path, {"raw_render": raw_render},
                                 settings.job_workers)

  # A single low-priority, single-worker queue for everything ticket 073's load-adaptive worker
  # drains: populate_thumb (thumb_populate.Populator, registered below), scan_dir (ticket 076's
  # per-top-level-directory nightly scan jobs) and prune_jobs (ticket 075), so background work
  # never competes for CPU/disk with the on-demand queue above or with interactive use.
  # newest_first (ticket 080): a folder's missing thumbnails, bumped by the /api/photos route
  # below, jump ahead of the standing backlog instead of waiting behind it in enqueue order.
  app.state.background_jobs = jobs.JobQueue(settings.db_path, {}, workers=1, low_priority=True,
                                            newest_first=True)
  app.state.populator = thumb_populate.Populator(
      settings.db_path, settings.pictures_dir, settings.thumbs_dir,
      queue=app.state.background_jobs)
  # A dedicated connection for enqueue_missing(), called from the load-adaptive worker's own
  # thread (not a JobQueue worker thread, so it needs its own connection like everything else here).
  app.state.populate_conn = db_lib.connect(settings.db_path, busy_timeout=60.0)

  def scan_dir_job(conn, job):
    """ticket 076: scan exactly the one directory job['target'] names, same as one step of
    scan_all -- '.' (the root) is non-recursive (just the files directly in it), everything else
    is recursive, matching scan.top_level_steps."""
    target = job["target"]
    scan_dir = (settings.pictures_dir if target == "."
               else os.path.join(settings.pictures_dir, target))
    progress = scan_lib.Progress()
    scan_lib.scan(conn, settings.pictures_dir, scan_dir, hashes, progress, settings.thumbs_dir,
                 on_scan_done, settings.scan_workers, recursive=(target != "."))
    if progress.error:
      raise RuntimeError(progress.error)
    logging.vlog(5, "scan_dir %s: done (%d files seen, %d read)",
                 target, progress.files_seen, progress.files_processed)

  app.state.background_jobs.add_handler("scan_dir", scan_dir_job)

  def prune_jobs_job(conn, job):
    """ticket 075: rides along with the nightly scan (enqueue_nightly_scan) rather than having
    its own schedule. 'done' jobs older than a week, 'failed' ones older than a year."""
    done_n, failed_n = jobs.prune(conn)
    if done_n or failed_n:
      logging.info("prune_jobs: deleted %d done, %d failed job(s) past the retention window",
                   done_n, failed_n)

  app.state.background_jobs.add_handler("prune_jobs", prune_jobs_job)

  def purge_trash_job(conn, job):
    """ticket 081: rides along with the nightly scan, same as prune_jobs above."""
    trash.purge_trash(settings.pictures_dir)

  app.state.background_jobs.add_handler("purge_trash", purge_trash_job)

  @app.get("/api/jobs")
  @db_route
  def list_jobs(limit: int = 100):
    return {"counts": app.state.jobs.counts(),
            "progress": app.state.jobs.progress(),
            "jobs": app.state.jobs.list(min(limit, 500))}

  @app.get("/")
  def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

  @app.get("/favicon.ico")
  def favicon():
    return Response(status_code=204)

  @app.get("/api/config")
  def client_config():
    """Settings the UI needs to present ratings consistently."""
    return {"one_star_is_unrated": settings.one_star_is_unrated,
            "xmp_dry_run": settings.xmp_dry_run}

  @app.get("/api/health")
  def health():
    return {"ok": True}

  @app.post("/api/scan")
  def start_scan(dir: str = None):
    try:
      started = app.state.scanner.start(dir)
    except ValueError as e:
      raise HTTPException(400, str(e))
    return {"started": started}

  @app.get("/api/scan/status")
  def scan_status():
    return dataclasses.asdict(app.state.scanner.progress)

  def curate(photo_id, fn, *args, **kwargs):
    """Run a curation change under the db lock; errors become HTTP 400/404."""
    with app.state.db_lock:
      try:
        result = fn(app.state.db, settings, photo_id, *args, **kwargs)
        result["photo"] = curation.photo_state(app.state.db, photo_id)
        return result
      except curation.CurationError as e:
        status = 404 if str(e).startswith("no such") else 400
        raise HTTPException(status, str(e))

  @app.post("/api/photos/{photo_id}/rating")
  @db_route
  def set_rating(photo_id: int, body: RatingBody):
    return curate(photo_id, curation.set_rating, body.rating)

  @app.post("/api/photos/{photo_id}/fav")
  @db_route
  def set_fav(photo_id: int, body: FavBody):
    return curate(photo_id, curation.set_fav, body.fav)

  @app.post("/api/photos/{photo_id}/tags")
  @db_route
  def edit_tags(photo_id: int, body: TagsBody):
    return curate(photo_id, curation.edit_tags, body.add, body.remove)

  @app.post("/api/photos/rating")
  def batch_rating(body: BatchRatingBody):
    if not body.ids or len(body.ids) > 2000:
      raise HTTPException(400, "give between 1 and 2000 photo ids")
    batch_id = uuid.uuid4().hex        # one id for every attempt, so one undo covers the batch

    def attempt():
      with app.state.db_lock:
        try:
          return curation.set_rating_batch(app.state.db, settings, body.ids,
                                           body.rating, batch_id)
        except curation.CurationError as e:
          raise HTTPException(400, str(e))
    return run_db(attempt)

  @app.post("/api/photos/trash")
  def trash_photos_route(body: TrashBody):
    """Ticket 072: move every still-rejected Photo in body.ids (and every file under it,
    including sidecars) to <pictures_dir>/.trash/. Re-checks each is still rated reject
    server-side rather than trusting the client's list; a Photo that isn't is reported in
    errors, not silently skipped, and does not stop the rest."""
    if not body.ids or len(body.ids) > 2000:
      raise HTTPException(400, "give between 1 and 2000 photo ids")

    def attempt():
      with app.state.db_lock:
        return trash.trash_photos(app.state.db, settings, body.ids)
    return run_db(attempt)

  @app.post("/api/activity/batch/{batch_id}/undo")
  @db_route
  def undo_batch(batch_id: str):
    with app.state.db_lock:
      try:
        return curation.undo_batch(app.state.db, settings, batch_id)
      except curation.CurationError as e:
        raise HTTPException(400, str(e))

  @app.post("/api/photos/{photo_id}/representative")
  @db_route
  def representative(photo_id: int, body: RepresentativeBody):
    with app.state.db_lock:
      try:
        grouping.set_representative(app.state.db, photo_id, body.file_id)
      except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no such") else 400, str(e))
      return library.photo_detail(app.state.db, photo_id)

  def path_of(file_id):
    row = app.state.db.execute("SELECT path FROM files WHERE id = ?",
                               (file_id,)).fetchone()
    if row is None:
      raise ValueError(f"no such file: {file_id}")
    return row["path"]

  @app.post("/api/files/{file_id}/link")
  @db_route
  def link_file(file_id: int, body: LinkBody):
    with app.state.db_lock:
      try:
        manual_links.link(app.state.db, settings.state_dir, path_of(file_id),
                          path_of(body.target_file_id), body.role)
        photo_id = app.state.db.execute(
            "SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]
        return library.photo_detail(app.state.db, photo_id)
      except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no such") else 400, str(e))

  @app.post("/api/files/{file_id}/unlink")
  @db_route
  def unlink_file(file_id: int):
    with app.state.db_lock:
      try:
        manual_links.unlink(app.state.db, settings.state_dir, path_of(file_id))
        photo_id = app.state.db.execute(
            "SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]
        return library.photo_detail(app.state.db, photo_id)
      except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no such") else 400, str(e))

  @app.post("/api/files/{file_id}/trash")
  @db_route
  def trash_file(file_id: int):
    """Ticket 082: move this one file (and its sidecars) to .trash/, regardless of the Photo's
    rating -- deleting an entire rejected Photo is what /api/photos/trash is for. Returns the
    move result plus the Photo's refreshed detail, so the loupe's Files panel can redraw from
    one response."""
    with app.state.db_lock:
      try:
        result = trash.trash_file(app.state.db, settings, file_id)
        photo_id = app.state.db.execute(
            "SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]
        result["photo"] = library.photo_detail(app.state.db, photo_id)
        return result
      except trash.TrashError as e:
        raise HTTPException(404 if e.args[0].startswith("no such") else 400, str(e))

  @app.post("/api/files/{file_id}/rerender_thumbs")
  @db_route
  def rerender_thumbs(file_id: int):
    """Ticket 079: clears every cached thumbnail of this file so the next request regenerates
    it from scratch -- the fix for a broken/corrupt cached thumbnail. Debug-menu action in the
    loupe, or offered automatically after a thumbnail repeatedly fails to load."""
    row = file_row(file_id)
    with app.state.db_lock:
      cleared = thumbs.clear(settings.thumbs_dir, app.state.db, file_id, row["path"])
    return {"file_id": file_id, "cleared": cleared}

  @app.get("/api/thumbs/usage")
  @db_route
  def thumbs_usage(lacking: bool = False):
    with app.state.db_lock:
      result = {"usage": thumbs.usage(app.state.db)}
      if lacking:
        result["lacking"] = thumbs.lacking(app.state.db)
      return result

  @app.get("/api/attention")
  @db_route
  def attention():
    with app.state.db_lock:
      conn = app.state.db
      conflicts = [dict(r) for r in conn.execute(
          "SELECT p.id, p.rating, rf.path FROM photos p JOIN files rf ON "
          "rf.id = p.representative_file_id WHERE p.conflict = 1 "
          "ORDER BY rf.path LIMIT 500")]
      orphans = [r["path"] for r in conn.execute(
          "SELECT path FROM xmp_sidecars WHERE file_id IS NULL "
          "ORDER BY path LIMIT 500")]
      behind = [dict(r) for r in conn.execute(
          "SELECT p.id, p.rating, rf.path FROM photos p JOIN files rf ON "
          "rf.id = p.representative_file_id WHERE p.conflict = 1 AND "
          "p.rating_updated_at IS NOT NULL AND p.rating_updated_at > "
          "COALESCE((SELECT MAX(s.mtime) FROM xmp_sidecars s JOIN files f ON "
          "f.id = s.file_id WHERE f.photo_id = p.id), 0) + 2 "
          "ORDER BY rf.path LIMIT 500")]
      return {"conflicts": conflicts, "sidecars_behind": behind,
              "orphan_sidecars": orphans,
              "ambiguous_recovery": recovery.ambiguous(conn)}

  @app.get("/api/activity")
  @db_route
  def activity(limit: int = 100):
    with app.state.db_lock:
      return curation.recent_activity(app.state.db, min(limit, 500))

  @app.post("/api/activity/{activity_id}/undo")
  @db_route
  def undo(activity_id: int):
    with app.state.db_lock:
      try:
        result = curation.undo(app.state.db, settings, activity_id)
        result["photo"] = curation.photo_state(app.state.db, result["photo_id"])
        return result
      except curation.CurationError as e:
        raise HTTPException(400, str(e))

  def read(fn, *args, **kwargs):
    with app.state.db_lock:
      try:
        return fn(app.state.db, *args, **kwargs)
      except ValueError as e:
        raise HTTPException(400, str(e))

  @app.get("/api/dirs")
  @db_route
  def dirs(path: str = "."):
    return read(library.list_dirs, path)

  @app.get("/api/photos")
  @db_route
  def photos(dir: str = ".", sort: str = "date", filter: str = "all",
             offset: int = 0, limit: int = 200):
    if offset == 0 and settings.load_worker_enabled:
      # ticket 080: someone is looking at this folder right now -- bump its still-missing
      # thumbnails ahead of the background worker's standing backlog (cheap: only the first
      # page load of a folder view triggers this, not every scroll/page-through).
      read(app.state.populator.enqueue_missing, rel_dir=dir)
    return read(library.list_photos, dir, sort, filter, offset, limit,
                settings.one_star_is_unrated)

  @app.get("/api/photos/counts")
  @db_route
  def photo_counts(dir: str = "."):
    return read(library.filter_counts, dir, settings.one_star_is_unrated)

  @app.get("/api/photos/{photo_id}")
  @db_route
  def photo(photo_id: int):
    detail = read(library.photo_detail, photo_id)
    if detail is None:
      raise HTTPException(404, f"no such photo: {photo_id}")
    return detail

  def file_row(file_id):
    with app.state.db_lock:
      row = app.state.db.execute(
          "SELECT id, path, missing FROM files WHERE id = ?",
          (file_id,)).fetchone()
    if row is None or row["missing"]:
      raise HTTPException(404, "no such file")
    return row

  cache = {"Cache-Control": "private, max-age=3600"}

  # The viewer preloads several sizes of the neighboring photos at once (ticket 050); rendering
  # a thumbnail or a RAW preview costs CPU, so only a few run at the same time.
  render_slots = threading.BoundedSemaphore(3)

  def render_limited(*args):
    with render_slots:
      return thumbs.make(*args)

  @app.get("/img/full/{file_id}")
  @db_route
  def full(file_id: int):
    row = file_row(file_id)
    if not row["path"].lower().endswith(WEB_EXTENSIONS):
      raise HTTPException(404, "not viewable in a browser; use a thumbnail size")
    return FileResponse(os.path.join(settings.pictures_dir, row["path"]),
                        headers=cache)

  @app.get("/img/{size}/{file_id}")
  async def image(size: str, file_id: int):
    if size not in thumbs.SIZES:
      raise HTTPException(404, "unknown size")
    row = await run_in_threadpool(run_db, lambda: file_row(file_id))
    logging.vlog(7, "image request: %s size=%s (file %d)", row["path"], size, file_id)
    if size == "Huge" and not fileinfo.is_raw(row["path"]):
      # Huge is the full size: an existing one, else the original if the
      # browser can show it. Never re-encode a full-size copy on request.
      path = thumbs.lookup(settings.thumbs_dir, size, row["path"])
      if path is None and row["path"].lower().endswith(WEB_EXTENSIONS):
        path = os.path.join(settings.pictures_dir, row["path"])
        logging.vlog(7, "%s: Huge served from the original", row["path"])
    else:
      made = await run_in_threadpool(
          render_limited, settings.pictures_dir, settings.thumbs_dir,
          row["path"], size)
      path = None
      if made:
        path, source = made
        def record():
          with app.state.db_lock:
            thumbs.record(app.state.db, file_id, size, path, source)
            app.state.db.commit()
        await run_in_threadpool(run_db, record)
    if path is None and fileinfo.is_raw(row["path"]):
      # No usable embedded preview: demosaic in the background; the client
      # retries the image after a moment.
      logging.vlog(3, "%s: no cached %s, deferring to raw_render (file %d)",
                   row["path"], size, file_id)
      app.state.jobs.enqueue("raw_render", file_id)
      raise HTTPException(404, "being rendered; retry shortly",
                          headers={"Retry-After": "2"})
    if path is None:
      logging.warning("%s: no %s thumbnail available and no RAW fallback (file %d)",
                      row["path"], size, file_id)
      raise HTTPException(404, "no thumbnail available yet")
    return FileResponse(path, headers=cache)

  app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

  return app
