import sqlite3

from fastapi.testclient import TestClient

from photoapp import api
from photoapp import db


def test_migrate_creates_schema_and_is_idempotent(tmp_path):
  path = str(tmp_path / "state" / "app.sqlite")
  conn = db.connect(path)
  assert db.schema_version(conn) == len(db.MIGRATIONS)
  tables = {r[0] for r in conn.execute(
      "SELECT name FROM sqlite_master WHERE type='table'")}
  assert {"files", "photos", "tags", "xmp_sidecars", "manual_links",
          "rating_by_hash", "activity_log", "thumbs", "jobs",
          "dir_mtimes"} <= tables
  conn.close()
  conn = db.connect(path)  # second open is a no-op
  assert db.schema_version(conn) == len(db.MIGRATIONS)
  assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_newer_schema_is_refused(tmp_path):
  path = str(tmp_path / "app.sqlite")
  raw = sqlite3.connect(path)
  raw.execute("PRAGMA user_version = 999")
  raw.commit()
  raw.close()
  try:
    db.connect(path)
  except RuntimeError:
    pass
  else:
    raise AssertionError("expected RuntimeError")


def test_placeholder_page_and_health(settings):
  client = TestClient(api.create_app(db.connect(":memory:"), settings))
  assert client.get("/api/health").json() == {"ok": True}
  assert "Photos" in client.get("/").text
