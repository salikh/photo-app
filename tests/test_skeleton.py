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


def test_stylesheet_is_served_and_defines_dark_theme(settings):
  client = TestClient(api.create_app(db.connect(":memory:"), settings))
  css = client.get("/static/style.css")
  assert css.status_code == 200
  assert "color-scheme: dark" in css.text and "--bg:" in css.text
  assert "/static/style.css" in client.get("/").text


def test_settings_from_flags_maps_every_flag_by_name():
  from absl import flags
  from photoapp import config
  f = flags.FLAGS
  f.mark_as_parsed()      # (other modules define required flags; parsing a command line here would fail)
  values = {"pictures_dir": "/p", "thumbs_dir": "/t", "state_dir": "/s", "busy_retry_seconds": 7.0,
            "one_star_is_unrated": True, "scan_workers": 3, "job_workers": 5,
            "nightly_scan_hour": 4, "xmp_dry_run": True, "new_raw_sidecar_style": "stem",
            "hashes_db": "/h.db"}
  saved = {name: f[name].value for name in values}
  try:
    for name, value in values.items():
      f[name].value = value
    s = config.Settings.from_flags()
    assert (s.pictures_dir, s.thumbs_dir, s.state_dir, s.hashes_db) == ("/p", "/t", "/s", "/h.db")
    assert s.busy_retry_seconds == 7 and s.one_star_is_unrated is True
    assert (s.scan_workers, s.job_workers, s.nightly_scan_hour) == (3, 5, 4)
    assert s.xmp_dry_run is True and s.new_raw_sidecar_style == "stem"
  finally:
    for name, value in saved.items():
      f[name].value = value
