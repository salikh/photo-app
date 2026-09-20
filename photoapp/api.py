"""FastAPI application."""

import dataclasses
import os
import threading

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
import pydantic

from photoapp import curation
from photoapp import fileinfo
from photoapp import library
from photoapp import scan as scan_lib
from photoapp import thumbs

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
WEB_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class RatingBody(pydantic.BaseModel):
  rating: int


class FavBody(pydantic.BaseModel):
  fav: bool


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
      settings.db_path, settings.pictures_dir, hashes, settings.thumbs_dir)

  @app.get("/")
  def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

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
    if size == "Huge":
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
    if path is None:
      raise HTTPException(404, "no thumbnail available yet")
    return FileResponse(path, headers=cache)

  app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

  return app
