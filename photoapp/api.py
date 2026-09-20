"""FastAPI application."""

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(conn):
  """Build the app around an open (migrated) database connection."""
  app = FastAPI(title="photos")
  app.state.db = conn

  @app.get("/")
  def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

  @app.get("/api/health")
  def health():
    return {"ok": True}

  return app
