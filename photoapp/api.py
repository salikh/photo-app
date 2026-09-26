"""FastAPI application."""

import dataclasses
import functools
import io
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
from PIL import Image

from photoapp import curation
from photoapp import crop as crop_lib
from photoapp import db as db_lib
from photoapp import export
from photoapp import fileinfo
from photoapp import grouping
from photoapp import jobs
from photoapp import library
from photoapp import manual_links
from photoapp import previews
from photoapp import raw_preview_dng
from photoapp import recovery
from photoapp import scan as scan_lib
from photoapp import raw_settings
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


class ExportBody(pydantic.BaseModel):
  ids: list[int]
  dir: str
  target: str


class RawSettingsBody(pydantic.BaseModel):
  bright: float | None = None
  wb_mode: str | None = None
  wb_r: float | None = None
  wb_g: float | None = None
  wb_b: float | None = None
  highlight: int | None = None
  exposure: float | None = None
  shadow: float | None = None
  saturation: float | None = None
  contrast: float | None = None
  noise: int | None = None
  demosaic: int | None = None


class CropBody(pydantic.BaseModel):
  x: float
  y: float
  w: float
  h: float


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
      on_done=on_scan_done, workers=settings.scan_workers,
      metadata_cache=settings.write_metadata_json)

  def raw_render(conn, job):
    row = conn.execute("SELECT id, path FROM files WHERE id = ?",
                       (job["file_id"],)).fetchone()
    if row is None:
      raise RuntimeError("file is gone")
    file_settings = raw_settings.get(conn, row["id"])   # ticket 085: parity with every other path
    file_crop = crop_lib.get(conn, row["id"])           # ticket 115: Thumb/Small rendered cropped
    made = thumbs.render_raw_sizes(settings.pictures_dir, settings.thumbs_dir,
                                   row["path"], file_settings, file_crop)
    if not made:
      raise RuntimeError("LibRaw could not decode the file")
    for size, path in made.items():
      thumbs.record(conn, row["id"], size, path, "libraw")
    conn.commit()
    logging.vlog(5, "%s: raw_render done (%s)", row["path"], ", ".join(made))

  def export_job(conn, job):
    """Ticket 089: job['target'] is the exact destination file path for this one file --
    already resolved (mirrored under the confirmed target folder, disambiguated) when the export
    was queued, by start_export below, which is the only enqueuer and the only place that knows
    the directory the export was started from. Ticket 099: once the file is actually written,
    import it into the database and link it to its source right away -- a no-op (returns None)
    when the target is outside pictures_dir, same as before this ticket."""
    row = conn.execute("SELECT path FROM files WHERE id = ?",
                       (job["file_id"],)).fetchone()
    if row is None:
      raise RuntimeError("file is gone")
    export.export_file(conn, settings, job["file_id"], row["path"], job["target"])
    export.link_exported_file(conn, settings, job["file_id"], job["target"])
    logging.vlog(5, "%s: exported to %s", row["path"], job["target"])

  app.state.jobs = jobs.JobQueue(settings.db_path, {"raw_render": raw_render},
                                 settings.job_workers)
  app.state.jobs.add_handler("export", export_job)

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
            "active": app.state.jobs.active(),
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
  def start_scan(dir: str = None, recursive: bool = True):
    """ticket 127: recursive scopes the requested dir (and root alone), same as the grid toggle."""
    try:
      started = app.state.scanner.start(dir, recursive)
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

  @app.get("/api/export/default_path")
  def export_default_path(dir: str = "."):
    try:
      rel_dir = library._norm_dir(dir)
    except ValueError as e:
      raise HTTPException(400, str(e))
    return {"path": export.default_target(settings.pictures_dir, rel_dir)}

  @app.post("/api/export")
  def start_export(body: ExportBody):
    """Ticket 089: queue one 'export' job per file (photoapp.jobs), each already carrying its
    fully resolved, mirrored destination path (export.dest_path, disambiguated by
    export.resolve_dest_path -- ticket 098) so the job handler needs no extra context beyond
    file_id + target. A photo id that no longer resolves to a live representative file is
    reported in 'missing', not silently dropped, matching trash_photos_route's own
    not-silently-skipped convention."""
    if not body.ids or len(body.ids) > 5000:
      raise HTTPException(400, "give between 1 and 5000 photo ids")
    try:
      rel_dir = library._norm_dir(body.dir)
      export.validate_target(settings, body.target)
    except (ValueError, export.ExportError) as e:
      raise HTTPException(400, str(e))

    def attempt():
      with app.state.db_lock:
        placeholders = ",".join("?" * len(body.ids))
        rows = app.state.db.execute(
            f"SELECT p.id AS photo_id, rf.id AS file_id, rf.path FROM photos p "
            f"JOIN files rf ON rf.id = p.representative_file_id WHERE p.id IN ({placeholders})",
            body.ids).fetchall()
        queued = []
        taken = set()
        for r in rows:
          candidate = export.dest_path(body.target, rel_dir, r["path"])
          dest = export.resolve_dest_path(app.state.db, settings.pictures_dir, candidate,
                                          r["file_id"], taken)
          taken.add(dest)
          job_id = app.state.jobs.enqueue("export", file_id=r["file_id"], target=dest)
          queued.append({"photo_id": r["photo_id"], "job_id": job_id})
      missing = sorted(set(body.ids) - {r["photo_id"] for r in rows})
      return {"queued": queued, "missing": missing}
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

  @app.post("/api/files/{file_id}/raw_settings")
  @db_route
  def set_raw_settings(file_id: int, body: RawSettingsBody):
    """Ticket 085: replace this file's RAW conversion settings wholesale (every field, including
    back to default for one left out -- matches how the frontend's sliders always submit the
    full current set) and clear every cached thumbnail size (ticket 079's rerender_thumbs
    mechanism) so the next request regenerates with the new settings -- a settings change is
    conceptually the same operation as "this thumbnail needs a fresh render," just triggered by
    a slider instead of a broken-image report."""
    row = file_row(file_id)
    with app.state.db_lock:
      try:
        raw_settings.set(app.state.db, file_id, bright=body.bright, wb_mode=body.wb_mode,
                         wb_r=body.wb_r, wb_g=body.wb_g, wb_b=body.wb_b, highlight=body.highlight,
                         exposure=body.exposure, shadow=body.shadow,
                         saturation=body.saturation, contrast=body.contrast, noise=body.noise,
                         demosaic=body.demosaic)
      except raw_settings.SettingsError as e:
        raise HTTPException(400, str(e))
      cleared = thumbs.clear(settings.thumbs_dir, app.state.db, file_id, row["path"])
      current = raw_settings.get(app.state.db, file_id)
      rev = app.state.db.execute(
          "SELECT thumb_rev FROM files WHERE id = ?", (file_id,)).fetchone()[0]
    return {"file_id": file_id, "cleared": cleared, "settings": current, "rev": rev}

  @app.post("/api/files/{file_id}/crop")
  @db_route
  def set_crop(file_id: int, body: CropBody):
    """Ticket 115: replace this file's non-destructive crop (normalized x/y/w/h) and clear every
    cached thumbnail size so Thumb/Small are regenerated cropped. A whole-frame rectangle
    (0, 0, 1, 1) clears the crop. Works for JPEG and RAW alike."""
    row = file_row(file_id)
    with app.state.db_lock:
      try:
        crop_lib.set(app.state.db, file_id, body.x, body.y, body.w, body.h)
      except crop_lib.CropError as e:
        raise HTTPException(400, str(e))
      cleared = thumbs.clear(settings.thumbs_dir, app.state.db, file_id, row["path"])
      current = crop_lib.get(app.state.db, file_id)
      rev = app.state.db.execute(
          "SELECT thumb_rev FROM files WHERE id = ?", (file_id,)).fetchone()[0]
    return {"file_id": file_id, "cleared": cleared, "crop": current, "rev": rev}

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
             offset: int = 0, limit: int = 200, recursive: bool = False):
    if offset == 0 and settings.load_worker_enabled:
      # ticket 080: someone is looking at this folder right now -- bump its still-missing
      # thumbnails ahead of the background worker's standing backlog (cheap: only the first
      # page load of a folder view triggers this, not every scroll/page-through).
      read(app.state.populator.enqueue_missing, rel_dir=dir)
    return read(library.list_photos, dir, sort, filter, offset, limit,
                settings.one_star_is_unrated, recursive)

  @app.get("/api/photos/counts")
  @db_route
  def photo_counts(dir: str = ".", recursive: bool = False):
    return read(library.filter_counts, dir, settings.one_star_is_unrated, recursive)

  @app.get("/api/photos/tags")
  @db_route
  def photo_tags(dir: str = ".", recursive: bool = False):
    return read(library.tags_in_view, dir, recursive)

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

  def render_limited(*args, **kwargs):
    with render_slots:
      return thumbs.make(*args, **kwargs)

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
      def get_file_settings():
        with app.state.db_lock:
          return (raw_settings.get(app.state.db, file_id),
                  crop_lib.get(app.state.db, file_id))
      file_settings, file_crop = await run_in_threadpool(run_db, get_file_settings)
      made = await run_in_threadpool(
          render_limited, settings.pictures_dir, settings.thumbs_dir,
          row["path"], size, settings=file_settings, crop=file_crop)
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

  @app.get("/api/files/{file_id}/raw_preview")
  async def raw_preview(file_id: int, size: str = "Medium", bright: float | None = None,
                        wb_mode: str | None = None, wb_r: float | None = None,
                        wb_g: float | None = None, wb_b: float | None = None,
                        highlight: int | None = None, exposure: float | None = None,
                        shadow: float | None = None, saturation: float | None = None,
                        contrast: float | None = None, noise: int | None = None,
                        demosaic: int | None = None):
    """Ticket 094: a provisional render of file_id with these *pending* settings, for the tuning
    UI to preview before committing anything. Unlike set_raw_settings, this never writes to
    files.raw_* or the thumbs cache -- rendered fresh into memory and returned directly, so there
    is nothing to discard if the user navigates away without saving; the pending values simply
    stop being requested. half_size (previews.render): the interactive tuning loop favors
    responsiveness over the full-resolution quality set_raw_settings' committed render gets."""
    if size not in thumbs.SIZES:
      raise HTTPException(404, "unknown size")
    row = file_row(file_id)
    if not fileinfo.is_raw(row["path"]):
      raise HTTPException(400, "raw_preview is only for RAW files")
    try:
      raw_settings.validate(wb_mode, highlight, exposure=exposure, shadow=shadow,
                            saturation=saturation, contrast=contrast, noise=noise, demosaic=demosaic)
    except raw_settings.SettingsError as e:
      raise HTTPException(400, str(e))
    pending = raw_settings.to_columns(bright, wb_mode, wb_r, wb_g, wb_b, highlight,
                                      exposure=exposure, shadow=shadow, saturation=saturation,
                                      contrast=contrast, noise=noise, demosaic=demosaic)
    full_path = os.path.join(settings.pictures_dir, row["path"])

    def render():
      with render_slots:
        return previews.render(full_path, pending, half_size=True)
    img = await run_in_threadpool(render)
    if img is None:
      raise HTTPException(404, "could not render a preview with these settings")
    long_edge = thumbs.LONG_EDGE[size]
    if long_edge:
      img = img.copy()
      img.thumbnail((long_edge, long_edge), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=thumbs.JPEG_QUALITY)
    return Response(content=buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})

  @app.get("/api/files/{file_id}/raw_preview_dng")
  async def raw_preview_dng_route(file_id: int):
    """Ticket 104: the lossy, size-reduced preview DNG [105](105.md)'s client-side LibRaw-Wasm
    tuning fetches once per session -- generated on first request, cached forever after (it
    depends only on the original file, never on raw_settings; see raw_preview_dng.py)."""
    row = file_row(file_id)
    if not fileinfo.is_raw(row["path"]):
      raise HTTPException(400, "raw_preview_dng is only for RAW files")

    def make():
      with render_slots:
        return raw_preview_dng.ensure(settings.thumbs_dir, settings.pictures_dir, row["path"])
    try:
      path = await run_in_threadpool(make)
    except raw_preview_dng.Unsupported as e:
      raise HTTPException(404, str(e))
    return FileResponse(path, media_type="image/x-adobe-dng", headers=cache)

  app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

  return app
