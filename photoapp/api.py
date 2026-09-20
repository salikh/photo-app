"""FastAPI application."""

import dataclasses
import os

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import FileResponse

from photoapp import fileinfo
from photoapp import scan as scan_lib

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(conn, settings):
  """Build the app around an open (migrated) database and its settings."""
  app = FastAPI(title="photos")
  app.state.db = conn
  app.state.settings = settings
  hashes = (fileinfo.load_precomputed_hashes(settings.hashes_db)
            if settings.hashes_db else None)
  app.state.scanner = scan_lib.ScanManager(
      settings.db_path, settings.pictures_dir, hashes)

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

  return app
