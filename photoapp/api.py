"""FastAPI application."""

import dataclasses
import os
import threading

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.responses import Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
import pydantic

from photoapp import curation
from photoapp import fileinfo
from photoapp import grouping
from photoapp import jobs
from photoapp import library
from photoapp import manual_links
from photoapp import recovery
from photoapp import scan as scan_lib
from photoapp import thumbs

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
WEB_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class RatingBody(pydantic.BaseModel):
  rating: int


class FavBody(pydantic.BaseModel):
  fav: bool


class BatchRatingBody(pydantic.BaseModel):
  ids: list[int]
  rating: int


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
  hashes = (fileinfo.load_precomputed_hashes(settings.hashes_db)
            if settings.hashes_db else None)
  app.state.scanner = scan_lib.ScanManager(
      settings.db_path, settings.pictures_dir, hashes, settings.thumbs_dir,
      on_done=lambda conn: recovery.recover(conn, settings))

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

  app.state.jobs = jobs.JobQueue(settings.db_path, {"raw_render": raw_render},
                                 settings.job_workers)

  @app.get("/api/jobs")
  def list_jobs(limit: int = 100):
    return {"counts": app.state.jobs.counts(),
            "jobs": app.state.jobs.list(min(limit, 500))}

  @app.get("/")
  def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

  @app.get("/favicon.ico")
  def favicon():
    return Response(status_code=204)

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
  def set_rating(photo_id: int, body: RatingBody):
    return curate(photo_id, curation.set_rating, body.rating)

  @app.post("/api/photos/{photo_id}/fav")
  def set_fav(photo_id: int, body: FavBody):
    return curate(photo_id, curation.set_fav, body.fav)

  @app.post("/api/photos/{photo_id}/tags")
  def edit_tags(photo_id: int, body: TagsBody):
    return curate(photo_id, curation.edit_tags, body.add, body.remove)

  @app.post("/api/photos/rating")
  def batch_rating(body: BatchRatingBody):
    if not body.ids or len(body.ids) > 2000:
      raise HTTPException(400, "give between 1 and 2000 photo ids")
    with app.state.db_lock:
      try:
        return curation.set_rating_batch(app.state.db, settings, body.ids,
                                         body.rating)
      except curation.CurationError as e:
        raise HTTPException(400, str(e))

  @app.post("/api/activity/batch/{batch_id}/undo")
  def undo_batch(batch_id: str):
    with app.state.db_lock:
      try:
        return curation.undo_batch(app.state.db, settings, batch_id)
      except curation.CurationError as e:
        raise HTTPException(400, str(e))

  @app.post("/api/photos/{photo_id}/representative")
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
  def unlink_file(file_id: int):
    with app.state.db_lock:
      try:
        manual_links.unlink(app.state.db, settings.state_dir, path_of(file_id))
        photo_id = app.state.db.execute(
            "SELECT photo_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]
        return library.photo_detail(app.state.db, photo_id)
      except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no such") else 400, str(e))

  @app.get("/api/thumbs/usage")
  def thumbs_usage(lacking: bool = False):
    with app.state.db_lock:
      result = {"usage": thumbs.usage(app.state.db)}
      if lacking:
        result["lacking"] = thumbs.lacking(app.state.db)
      return result

  @app.get("/api/attention")
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
      return {"conflicts": conflicts, "orphan_sidecars": orphans,
              "ambiguous_recovery": recovery.ambiguous(conn)}

  @app.get("/api/activity")
  def activity(limit: int = 100):
    with app.state.db_lock:
      return curation.recent_activity(app.state.db, min(limit, 500))

  @app.post("/api/activity/{activity_id}/undo")
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
  def dirs(path: str = "."):
    return read(library.list_dirs, path)

  @app.get("/api/photos")
  def photos(dir: str = ".", sort: str = "date", filter: str = "all",
             offset: int = 0, limit: int = 200):
    return read(library.list_photos, dir, sort, filter, offset, limit)

  @app.get("/api/photos/{photo_id}")
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

  @app.get("/img/full/{file_id}")
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
    row = file_row(file_id)
    if size == "Huge" and not fileinfo.is_raw(row["path"]):
      # Huge is the full size: an existing one, else the original if the
      # browser can show it. Never re-encode a full-size copy on request.
      path = thumbs.lookup(settings.thumbs_dir, size, row["path"])
      if path is None and row["path"].lower().endswith(WEB_EXTENSIONS):
        path = os.path.join(settings.pictures_dir, row["path"])
    else:
      made = await run_in_threadpool(
          thumbs.make, settings.pictures_dir, settings.thumbs_dir,
          row["path"], size)
      path = None
      if made:
        path, source = made
        with app.state.db_lock:
          thumbs.record(app.state.db, file_id, size, path, source)
          app.state.db.commit()
    if path is None and fileinfo.is_raw(row["path"]):
      # No usable embedded preview: demosaic in the background; the client
      # retries the image after a moment.
      app.state.jobs.enqueue("raw_render", file_id)
      raise HTTPException(404, "being rendered; retry shortly",
                          headers={"Retry-After": "2"})
    if path is None:
      raise HTTPException(404, "no thumbnail available yet")
    return FileResponse(path, headers=cache)

  app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

  return app
