import datetime
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


def test_enqueue_by_target_dedupes_independently_of_file_id(settings):
  db.open_state(settings.state_dir).close()
  q = jobs.JobQueue(settings.db_path, {"scan_dir": lambda c, j: None})
  a = q.enqueue("scan_dir", target="2020")
  assert q.enqueue("scan_dir", target="2020") == a       # same target: deduped
  b = q.enqueue("scan_dir", target="2021")
  assert b != a                                          # different target: separate job
  row = {r["id"]: r for r in q.list()}[a]
  assert row["file_id"] is None and row["target"] == "2020"


def test_add_handler_registers_a_kind_after_construction(settings):
  db.open_state(settings.state_dir).close()
  q = jobs.JobQueue(settings.db_path, {})
  seen = []
  q.add_handler("late", lambda c, j: seen.append(j["file_id"]))
  q.enqueue("late", 1)
  q.start()
  assert q.wait_idle()
  q.stop()
  assert seen == [1]


def test_newest_first_claims_the_most_recently_enqueued_job(settings):
  # ticket 080: a "bumped" job (freshly enqueued, not already queued) should be worked before
  # the standing backlog, approximated by claiming highest-id-first instead of FIFO. All three
  # jobs are enqueued (committed) before start(), so there is no ordering race to guard against.
  db.open_state(settings.state_dir).close()
  seen = []
  q = jobs.JobQueue(settings.db_path, {"ok": lambda c, j: seen.append(j["file_id"])},
                    workers=1, newest_first=True)
  q.enqueue("ok", 1)
  q.enqueue("ok", 2)
  q.enqueue("ok", 3)
  q.start()
  assert q.wait_idle()
  q.stop()
  assert seen == [3, 2, 1]   # newest (highest id) first, not FIFO


def test_default_claim_order_is_still_fifo(settings):
  db.open_state(settings.state_dir).close()
  seen = []
  q = jobs.JobQueue(settings.db_path, {"ok": lambda c, j: seen.append(j["file_id"])},
                    workers=1)   # newest_first defaults False
  q.enqueue("ok", 1)
  q.enqueue("ok", 2)
  q.enqueue("ok", 3)
  q.start()
  assert q.wait_idle()
  q.stop()
  assert seen == [1, 2, 3]


def test_prune_deletes_old_done_and_older_failed_jobs(settings):
  conn = db.open_state(settings.state_dir)

  def insert(state, seconds_ago):
    finished = (datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago)).isoformat(
        timespec="seconds")
    conn.execute(
        "INSERT INTO jobs (kind, file_id, state, created_at, finished_at) "
        "VALUES ('k', 1, ?, 'x', ?)", (state, finished))

  DAY = 86400
  insert("done", 6 * DAY)                    # kept: under the 7-day done retention
  insert("done", 8 * DAY)                     # pruned
  insert("failed", 8 * DAY)                   # kept: well under the 365-day failed retention
  insert("failed", 400 * DAY)                 # pruned
  insert("queued", 999 * DAY)                 # never touched regardless of age
  conn.commit()

  done_n, failed_n = jobs.prune(conn)
  assert (done_n, failed_n) == (1, 1)
  states = sorted(r["state"] for r in conn.execute("SELECT state FROM jobs"))
  assert states == ["done", "failed", "queued"]


def test_a_successful_job_deletes_its_own_earlier_failed_attempts(settings):
  db.open_state(settings.state_dir).close()
  attempt = [0]

  def flaky(conn, job):
    attempt[0] += 1
    if attempt[0] < 3:
      raise RuntimeError(f"attempt {attempt[0]} failed")

  q = jobs.JobQueue(settings.db_path, {"flaky": flaky}, workers=1)
  q.enqueue("flaky", 1)
  q.start()
  assert q.wait_idle()
  q.enqueue("flaky", 1)   # finished jobs may run again (test_jobs_run_dedupe_and_isolate_failures)
  assert q.wait_idle()
  q.enqueue("flaky", 1)
  assert q.wait_idle()
  q.stop()

  # ticket 075: the successful 3rd run should have deleted its own two earlier failed attempts,
  # not left them piling up forever.
  rows = q.list()
  assert len(rows) == 1 and rows[0]["state"] == "done"


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


def test_queue_can_be_restarted_after_stop(settings):
  # ticket 073: the load-adaptive worker starts/stops the same queue repeatedly, unlike every
  # other caller (which starts it once); a stopped queue must be able to start again and process
  # jobs enqueued after the restart.
  db.open_state(settings.state_dir).close()
  seen = []
  q = jobs.JobQueue(settings.db_path, {"ok": lambda c, j: seen.append(j["file_id"])}, workers=1)
  q.start()
  q.enqueue("ok", 1)
  assert q.wait_idle()
  q.stop()
  q.enqueue("ok", 2)
  q.start()
  assert q.wait_idle()
  q.stop()
  assert seen == [1, 2]


def test_progress_buckets_by_finished_at(settings):
  import datetime

  conn = db.open_state(settings.state_dir)

  def insert(kind, state, seconds_ago):
    finished = (datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago)).isoformat(
        timespec="seconds") if seconds_ago is not None else None
    conn.execute(
        "INSERT INTO jobs (kind, file_id, state, created_at, finished_at) "
        "VALUES (?, 1, ?, 'x', ?)", (kind, state, finished))

  insert("a", "done", 30)          # within the last minute (and hour, and day)
  insert("a", "failed", 30 * 60)   # within the last hour (and day), not the last minute
  insert("b", "done", 12 * 3600)   # within the last day only
  insert("b", "done", 3 * 86400)   # outside every window
  insert("a", "queued", None)
  insert("b", "running", None)
  conn.commit()

  q = jobs.JobQueue(settings.db_path, {"a": lambda c, j: None, "b": lambda c, j: None})
  p = q.progress()
  assert p["total"] == 6
  assert p["incomplete"] == 2
  assert p["by_kind"] == {"a": {"done": 1, "failed": 1, "queued": 1},
                          "b": {"done": 2, "running": 1}}
  assert p["completed"]["last_minute"] == {"done": 1, "failed": 0}
  assert p["completed"]["last_hour"] == {"done": 1, "failed": 1}
  assert p["completed"]["last_day"] == {"done": 2, "failed": 1}


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
  empty = client.get("/api/jobs").json()
  assert empty["counts"] == {} and empty["jobs"] == []
  assert empty["progress"]["total"] == 0 and empty["progress"]["incomplete"] == 0

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
    assert data["progress"]["total"] == 1 and data["progress"]["incomplete"] == 0
    assert data["progress"]["by_kind"] == {"populate_thumb": {"done": 1}}
    assert data["progress"]["completed"]["last_minute"] == {"done": 1, "failed": 0}
  finally:
    populator.stop()


def test_scan_dir_job_scans_exactly_its_own_directory(settings):
  # ticket 076: a 'scan_dir' job on app.state.background_jobs scans one top-level directory --
  # '.' non-recursively (root files only), everything else recursively -- and nothing more.
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "top.jpg"))
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  make_jpeg(os.path.join(d, "2020", "sub", "b.jpg"))
  make_jpeg(os.path.join(d, "2021", "c.jpg"))
  conn = db.open_state(settings.state_dir)
  app = api.create_app(conn, settings)

  app.state.background_jobs.enqueue("scan_dir", target="2020")
  app.state.background_jobs.start()
  try:
    assert app.state.background_jobs.wait_idle(10)
  finally:
    app.state.background_jobs.stop()

  jobs_seen = app.state.background_jobs.list()
  assert len(jobs_seen) == 1 and jobs_seen[0]["state"] == "done"
  paths = {r["path"] for r in conn.execute("SELECT path FROM files")}
  assert paths == {"2020/a.jpg", "2020/sub/b.jpg"}   # only the requested directory, recursively

  # '.' is non-recursive: only files directly in the root, not the top-level dirs' contents.
  app.state.background_jobs.enqueue("scan_dir", target=".")
  app.state.background_jobs.start()
  try:
    assert app.state.background_jobs.wait_idle(10)
  finally:
    app.state.background_jobs.stop()
  paths = {r["path"] for r in conn.execute("SELECT path FROM files")}
  assert paths == {"top.jpg", "2020/a.jpg", "2020/sub/b.jpg"}   # 2021/c.jpg still unscanned


def test_prune_jobs_job_runs_through_the_background_queue(settings):
  # ticket 075: a 'prune_jobs' job (as enqueue_nightly_scan queues alongside scan_dir jobs) is
  # handled by the running app, using jobs.prune()'s real retention windows.
  conn = db.open_state(settings.state_dir)

  def insert_old_done():
    old = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO jobs (kind, file_id, state, created_at, finished_at) "
        "VALUES ('raw_render', 1, 'done', 'x', ?)", (old,))
    conn.commit()

  insert_old_done()
  app = api.create_app(conn, settings)
  app.state.background_jobs.enqueue("prune_jobs")
  app.state.background_jobs.start()
  try:
    assert app.state.background_jobs.wait_idle(10)
  finally:
    app.state.background_jobs.stop()

  remaining = conn.execute("SELECT kind FROM jobs WHERE kind = 'raw_render'").fetchall()
  assert remaining == []   # the 30-day-old done job was pruned


def test_purge_trash_job_runs_through_the_background_queue(settings):
  # ticket 081: a 'purge_trash' job (as enqueue_nightly_scan queues alongside scan_dir/prune_jobs)
  # is handled by the running app. Age logic itself is unit-tested in tests/test_trash.py; this
  # confirms the wiring runs without error and leaves a freshly-trashed file alone (not yet old).
  from photoapp import trash as trash_lib
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  trash_lib.move_to_trash(d, "a.DNG")
  conn = db.open_state(settings.state_dir)
  app = api.create_app(conn, settings)
  app.state.background_jobs.enqueue("purge_trash")
  app.state.background_jobs.start()
  try:
    assert app.state.background_jobs.wait_idle(10)
  finally:
    app.state.background_jobs.stop()

  assert app.state.background_jobs.list()[0]["state"] == "done"
  assert os.path.isfile(os.path.join(d, trash_lib.TRASH_DIRNAME, "a.DNG"))   # too fresh to purge
