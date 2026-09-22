import os
import threading

from fastapi.testclient import TestClient
from PIL import Image

from photoapp import api
from photoapp import db
from photoapp import jobs
from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.test_grouping import touch


def test_jobs_run_dedupe_and_isolate_failures(settings):
  db.open_state(settings.state_dir).close()
  seen = []

  def ok(conn, job):
    seen.append(job["file_id"])

  def boom(conn, job):
    raise RuntimeError("nope")

  q = jobs.JobQueue(settings.db_path, {"ok": ok, "boom": boom}, workers=2)
  a = q.enqueue("ok", 1)
  assert q.enqueue("ok", 1) == a                     # deduplicated while queued
  q.enqueue("boom", 2)
  q.enqueue("ok", 3)
  q.start()
  assert q.wait_idle()
  by_kind = {(j["kind"], j["file_id"]): j for j in q.list()}
  assert sorted(seen) == [1, 3]
  assert by_kind[("boom", 2)]["state"] == "failed" and "nope" in by_kind[("boom", 2)]["error"]
  assert by_kind[("ok", 1)]["state"] == "done" and by_kind[("ok", 1)]["finished_at"]
  assert q.enqueue("ok", 1) != a                     # finished jobs may run again
  q.wait_idle()
  q.stop()


def test_running_jobs_are_requeued_after_restart(settings):
  conn = db.open_state(settings.state_dir)
  conn.execute("INSERT INTO jobs (kind, file_id, state, created_at) "
               "VALUES ('ok', 7, 'running', 'x')")
  conn.commit()
  ran = threading.Event()
  q = jobs.JobQueue(settings.db_path, {"ok": lambda c, j: ran.set()}, workers=1)
  q.start()
  assert ran.wait(5)
  q.stop()


def test_raw_without_preview_is_rendered_in_background(settings, monkeypatch):
  d = settings.pictures_dir
  touch(os.path.join(d, "y", "a.dng"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]

  def fake_render(pictures_dir, thumbs_dir, file_path):
    out = {}
    for size in ("Thumb", "Small", "Medium"):
      dest = thumbs.thumb_path(thumbs_dir, size, file_path)
      make_jpeg(dest, size=(30, 20))
      out[size] = dest
    return out

  monkeypatch.setattr(thumbs, "render_raw_sizes", fake_render)
  app = api.create_app(conn, settings)
  client = TestClient(app)
  r = client.get(f"/img/Small/{fid}")
  assert r.status_code == 404 and r.headers["retry-after"] == "2"
  assert client.get(f"/api/jobs").json()["counts"] == {"queued": 1}
  client.get(f"/img/Small/{fid}")                          # no duplicate job
  assert len(client.get("/api/jobs").json()["jobs"]) == 1
  app.state.jobs.start()
  assert app.state.jobs.wait_idle()
  r = client.get(f"/img/Small/{fid}")
  assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
  assert client.get("/api/jobs").json()["jobs"][0]["state"] == "done"
  assert conn.execute("SELECT source FROM thumbs WHERE size = 'Small'").fetchone()[0] in ("libraw", "existing")
  app.state.jobs.stop()


def test_failed_raw_render_is_reported(settings):
  touch(os.path.join(settings.pictures_dir, "a.dng"))     # not a real RAW
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  app = api.create_app(conn, settings)
  client = TestClient(app)
  assert client.get(f"/img/Thumb/{fid}").status_code == 404
  app.state.jobs.start()
  app.state.jobs.wait_idle()
  job = client.get("/api/jobs").json()["jobs"][0]
  assert job["state"] == "failed" and "LibRaw" in job["error"]
  app.state.jobs.stop()


def test_two_queues_sharing_one_table_only_claim_their_own_kind(settings):
  db.open_state(settings.state_dir).close()
  seen_a, seen_b = [], []
  qa = jobs.JobQueue(settings.db_path, {"kind_a": lambda c, j: seen_a.append(j["file_id"])}, workers=1)
  qb = jobs.JobQueue(settings.db_path, {"kind_b": lambda c, j: seen_b.append(j["file_id"])}, workers=1)
  # queue jobs of both kinds before either queue starts, interleaved, so a
  # buggy _claim() that ignores kind would very likely hand a "kind_b" job
  # to qa (no handler -> KeyError -> wrongly marked 'failed').
  qa.enqueue("kind_a", 1)
  qb.enqueue("kind_b", 101)
  qa.enqueue("kind_a", 2)
  qb.enqueue("kind_b", 102)
  qa.start()
  qb.start()
  assert qa.wait_idle() and qb.wait_idle()
  assert sorted(seen_a) == [1, 2] and sorted(seen_b) == [101, 102]
  jobs_by_kind = {}
  for row in qa.list(limit=10):
    jobs_by_kind.setdefault(row["kind"], []).append(row["state"])
  assert jobs_by_kind == {"kind_a": ["done", "done"], "kind_b": ["done", "done"]}
  assert all(s == "done" for states in jobs_by_kind.values() for s in states)  # none failed with an unknown-kind error
  qa.stop()
  qb.stop()


def test_low_priority_worker_lowers_only_its_own_thread(settings):
  db.open_state(settings.state_dir).close()
  observed = {}

  def handler(conn, job):
    observed["nice"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())

  q = jobs.JobQueue(settings.db_path, {"low": handler}, workers=1, low_priority=True)
  before = os.getpriority(os.PRIO_PROCESS, os.getpid())    # this (main) thread's own niceness
  q.enqueue("low", 1)
  q.start()
  assert q.wait_idle()
  q.stop()
  assert observed["nice"] == 19               # the worker thread lowered itself
  assert os.getpriority(os.PRIO_PROCESS, os.getpid()) == before   # unaffected outside the worker


def test_populate_thumb_jobs_appear_on_the_running_apps_jobs_page(settings, monkeypatch):
  """The populate queue writes to the same jobs table the running app reads, so its
  progress shows on the existing /api/jobs endpoint (and Jobs page) without any
  change there -- the two kinds ('raw_render' and 'populate_thumb') just coexist."""
  from photoapp import api
  from photoapp import scan
  from photoapp import thumb_populate
  from tests.test_grouping import touch as touch_

  d = settings.pictures_dir
  touch_(os.path.join(d, "a.DNG"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  app = api.create_app(conn, settings)
  client = TestClient(app)
  assert client.get("/api/jobs").json() == {"counts": {}, "jobs": []}

  monkeypatch.setattr(thumb_populate, "dcraw_available", lambda: True)
  monkeypatch.setattr(thumb_populate, "extract_embedded_thumb",
                      lambda source: Image.new("RGB", (300, 200)))
  monkeypatch.setattr(thumb_populate, "render_dcraw",
                      lambda source, half_size: Image.new("RGB", (2000, 1300)))
  populator = thumb_populate.Populator(settings.db_path, d, settings.thumbs_dir)
  populator.enqueue_missing(conn)
  populator.start()
  try:
    assert populator.queue.wait_idle(10)
    data = client.get("/api/jobs").json()
    assert data["counts"] == {"done": 1}
    assert data["jobs"][0]["kind"] == "populate_thumb"
  finally:
    populator.stop()
