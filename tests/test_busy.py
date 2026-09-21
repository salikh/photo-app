"""Retry with backoff while another writer holds the database (ticket 052)."""

import json
import os
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from photoapp import api
from photoapp import db
from photoapp import scan
from tests.conftest import make_jpeg
from tests.test_curation import photo_id, setup_pair
from tests.test_grouping import touch

LOCKED = sqlite3.OperationalError("database is locked")


# --- the retry helper ---------------------------------------------------------------------------

class FakeTime:
  def __init__(self):
    self.now, self.sleeps = 0.0, []

  def clock(self):
    return self.now

  def sleep(self, seconds):
    self.sleeps.append(seconds)
    self.now += seconds


def test_retry_busy_waits_with_growing_delays_then_succeeds():
  t, calls = FakeTime(), []

  def fn():
    calls.append(1)
    if len(calls) < 6:
      raise LOCKED
    return "done"

  retries = []
  assert db.retry_busy(fn, 60, sleep=t.sleep, clock=t.clock, on_retry=lambda: retries.append(1)) == "done"
  assert t.sleeps == [0.1, 0.2, 0.4, 0.8, 1.6] and len(retries) == 5


def test_retry_busy_gives_up_after_about_a_minute_with_capped_delays():
  t, calls = FakeTime(), []

  def fn():
    calls.append(1)
    raise LOCKED

  with pytest.raises(db.DatabaseBusy):
    db.retry_busy(fn, 60, sleep=t.sleep, clock=t.clock)
  assert max(t.sleeps) == 4.0                              # delays are capped
  assert 55 <= sum(t.sleeps) <= 60                         # about a minute in total, never much more
  assert len(calls) == len(t.sleeps) + 1


def test_only_busy_errors_are_retried():
  t = FakeTime()
  for error in (sqlite3.OperationalError("no such table: x"), ValueError("nope"), sqlite3.IntegrityError("x")):
    calls = []

    def fn():
      calls.append(1)
      raise error

    with pytest.raises(type(error)):
      db.retry_busy(fn, 60, sleep=t.sleep, clock=t.clock)
    assert len(calls) == 1
  assert t.sleeps == []
  assert db.is_busy(sqlite3.OperationalError("database table is locked")) and db.is_busy(sqlite3.OperationalError("database is busy"))
  assert not db.is_busy(ValueError("locked"))


# --- the web API against a real second writer -------------------------------------------------

class Writer:
  """Another connection that holds the write lock, like a running scan."""

  def __init__(self, path):
    self.conn = sqlite3.connect(path, isolation_level=None, timeout=0, check_same_thread=False)

  def lock(self):
    self.conn.execute("BEGIN IMMEDIATE")

  def release(self):
    self.conn.execute("ROLLBACK")


def make_app(settings, budget=1.0):
  import dataclasses
  conn = db.open_state(settings.state_dir, busy_timeout=0.02)
  settings = dataclasses.replace(settings, busy_retry_seconds=budget)
  app = api.create_app(conn, settings)
  app.state.sleep = lambda s: time.sleep(min(s, 0.05))       # keep the test quick, the budget is real time
  return app, TestClient(app), conn, settings


def test_write_waits_for_the_lock_and_then_succeeds_exactly_once(settings):
  conn0 = db.open_state(settings.state_dir)
  pid = setup_pair(conn0, settings, 1, 1)
  conn0.close()
  app, client, conn, settings = make_app(settings, budget=5.0)
  writer = Writer(settings.db_path)
  writer.lock()
  timer = threading.Timer(0.6, writer.release)
  timer.start()
  r = client.post(f"/api/photos/{pid}/rating", json={"rating": 4})
  timer.join()
  assert r.status_code == 200 and r.json()["photo"]["rating"] == 4
  # the repeated attempt left everything consistent and did not double up
  assert len(client.get("/api/activity").json()) == 1
  backups = [os.path.join(dp, f) for dp, _, fs in os.walk(settings.state_dir) for f in fs if "xmp_backups" in dp]
  assert len(backups) == 2                                     # DNG and JPG sidecar, one first-seen copy each
  recorded = {r[0] for r in conn.execute("SELECT backup_path FROM xmp_sidecars WHERE backup_path IS NOT NULL")}
  assert recorded == set(backups)                              # and both are recorded (first-seen kept)
  from photoapp import xmp
  for name in ("y/K1.DNG.xmp", "y/K1.JPG.xmp"):
    assert xmp.parse(open(os.path.join(settings.pictures_dir, name), "rb").read()).rating == 4


def test_gives_up_with_500_database_busy_and_reads_still_work(settings):
  conn0 = db.open_state(settings.state_dir)
  pid = setup_pair(conn0, settings, 1, 1)
  conn0.close()
  app, client, conn, settings = make_app(settings, budget=0.5)
  writer = Writer(settings.db_path)
  writer.lock()
  try:
    r = client.post(f"/api/photos/{pid}/fav", json={"fav": True})
    assert r.status_code == 500 and r.json()["detail"] == "internal server error: database busy"
    assert client.get(f"/api/photos/{pid}").status_code == 200        # WAL readers are not blocked
    assert client.get("/api/dirs").status_code == 200
    assert client.post("/api/photos/rating", json={"ids": [pid], "rating": 3}).status_code == 500
    assert client.post(f"/api/photos/{pid}/tags", json={"add": ["x"], "remove": []}).status_code == 500
  finally:
    writer.release()
  # once the lock is gone the same requests work, and the failed ones left nothing behind
  assert client.get("/api/activity").json() == []
  assert client.post(f"/api/photos/{pid}/fav", json={"fav": True}).status_code == 200


def test_failed_attempts_do_not_hold_the_in_process_lock(settings):
  conn0 = db.open_state(settings.state_dir)
  pid = setup_pair(conn0, settings, 1, 1)
  conn0.close()
  app, client, conn, settings = make_app(settings, budget=2.0)
  writer = Writer(settings.db_path)
  writer.lock()
  results = {}
  t = threading.Thread(target=lambda: results.setdefault("w", client.post(f"/api/photos/{pid}/fav", json={"fav": True})))
  t.start()
  time.sleep(0.3)                                              # the write is now waiting/retrying
  start = time.time()
  assert client.get("/api/dirs").status_code == 200            # another request is not stuck behind it
  assert time.time() - start < 1.0
  writer.release()
  t.join(10)
  assert results["w"].status_code == 200


def test_batch_keeps_one_batch_id_across_attempts_and_undo_covers_it(settings):
  conn0 = db.open_state(settings.state_dir)
  d = settings.pictures_dir
  for n in "abc":
    make_jpeg(os.path.join(d, f"{n}.jpg"))
  scan.scan(conn0, d)
  ids = [r[0] for r in conn0.execute("SELECT id FROM photos ORDER BY id")]
  conn0.close()
  app, client, conn, settings = make_app(settings, budget=5.0)
  writer = Writer(settings.db_path)
  writer.lock()
  timer = threading.Timer(0.5, writer.release)
  timer.start()
  r = client.post("/api/photos/rating", json={"ids": ids, "rating": 3})
  timer.join()
  assert r.status_code == 200 and len(r.json()["results"]) == 3
  batch_id = r.json()["batch_id"]
  assert {row["batch_id"] for row in client.get("/api/activity").json()} == {batch_id}
  u = client.post(f"/api/activity/batch/{batch_id}/undo").json()
  assert len(u["undone"]) == 3 and all(p["rating"] == 0 for p in u["photos"])


def test_link_decision_is_not_appended_twice_when_the_database_step_is_repeated(settings):
  conn0 = db.open_state(settings.state_dir)
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  make_jpeg(os.path.join(d, "a.JPG"))
  make_jpeg(os.path.join(d, "Exported", "a-edit.jpg"))
  scan.scan(conn0, d)
  ids = {p: i for i, p in conn0.execute("SELECT id, path FROM files")}
  conn0.close()
  app, client, conn, settings = make_app(settings, budget=5.0)
  writer = Writer(settings.db_path)
  writer.lock()
  timer = threading.Timer(0.5, writer.release)
  timer.start()
  r = client.post(f"/api/files/{ids['Exported/a-edit.jpg']}/link", json={"target_file_id": ids["a.DNG"], "role": "export"})
  timer.join()
  assert r.status_code == 200
  lines = open(os.path.join(settings.state_dir, "manual_links.jsonl")).read().splitlines()
  assert len(lines) == 1 and json.loads(lines[0])["action"] == "link"
  assert conn.execute("SELECT COUNT(*) FROM manual_links").fetchone()[0] == 1


def test_background_connections_wait_long_inside_sqlite(settings):
  c = db.open_state(settings.state_dir, busy_timeout=60.0)
  assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
  assert db.open_state(settings.state_dir).execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_a_busy_database_fails_before_any_sidecar_is_touched(settings):
  conn0 = db.open_state(settings.state_dir)
  pid = setup_pair(conn0, settings, 1, 1)
  conn0.close()
  paths = [os.path.join(settings.pictures_dir, n) for n in ("y/K1.DNG.xmp", "y/K1.JPG.xmp")]
  before = [open(p, "rb").read() for p in paths]
  mtimes = [os.stat(p).st_mtime_ns for p in paths]
  app, client, conn, settings = make_app(settings, budget=0.4)
  writer = Writer(settings.db_path)
  writer.lock()
  try:
    assert client.post(f"/api/photos/{pid}/rating", json={"rating": 5}).status_code == 500
  finally:
    writer.release()
  assert [open(p, "rb").read() for p in paths] == before                 # bytes unchanged
  assert [os.stat(p).st_mtime_ns for p in paths] == mtimes               # not even rewritten
  assert not os.path.exists(os.path.join(settings.state_dir, "xmp_backups"))
  assert client.get(f"/api/photos/{pid}").json()["rating"] == 1          # database still shows the old value


def test_new_sidecar_is_not_created_when_the_database_is_busy(settings):
  conn0 = db.open_state(settings.state_dir)
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn0, settings.pictures_dir)
  pid = photo_id(conn0, "a.jpg")
  conn0.close()
  app, client, conn, settings = make_app(settings, budget=0.3)
  writer = Writer(settings.db_path)
  writer.lock()
  try:
    assert client.post(f"/api/photos/{pid}/rating", json={"rating": 2}).status_code == 500
  finally:
    writer.release()
  assert not os.path.exists(os.path.join(settings.pictures_dir, "a.jpg.xmp"))
  assert client.post(f"/api/photos/{pid}/rating", json={"rating": 2}).status_code == 200
  assert os.path.exists(os.path.join(settings.pictures_dir, "a.jpg.xmp"))
