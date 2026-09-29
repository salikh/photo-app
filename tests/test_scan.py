import os
import time

from fastapi.testclient import TestClient

from photoapp import api
from photoapp import scan
from tests.conftest import make_jpeg


def files(conn):
  return {r["path"]: r for r in conn.execute("SELECT * FROM files")}


def build_tree(pics):
  make_jpeg(os.path.join(pics, "2020", "a.jpg"))
  make_jpeg(os.path.join(pics, "2020", "sub", "b.JPG"), color="blue")
  with open(os.path.join(pics, "2020", "raw.dng"), "wb") as f:
    f.write(b"not decodable")
  with open(os.path.join(pics, "2020", "notes.txt"), "w") as f:
    f.write("ignored")
  make_jpeg(os.path.join(pics, "top.jpg"))


def test_scan_populates_files(conn, settings):
  build_tree(settings.pictures_dir)
  p = scan.scan(conn, settings.pictures_dir)
  assert p.error is None and not p.running
  f = files(conn)
  assert set(f) == {"2020/a.jpg", "2020/sub/b.JPG", "2020/raw.dng", "top.jpg"}
  a = f["2020/a.jpg"]
  assert (a["width"], a["height"], a["mime_type"]) == (30, 20, "image/jpeg")
  assert len(a["hash"]) == 56 and a["bytesize"] > 0
  dng = f["2020/raw.dng"]
  assert dng["width"] is None and dng["hash"] is not None
  assert conn.execute("SELECT COUNT(*) FROM dir_mtimes").fetchone()[0] == 3


def test_second_scan_is_noop(conn, settings):
  build_tree(settings.pictures_dir)
  scan.scan(conn, settings.pictures_dir)
  before = {p: dict(r) for p, r in files(conn).items()}
  p = scan.scan(conn, settings.pictures_dir)
  assert p.files_processed == 0 and p.dirs_skipped == p.dirs_seen
  assert {k: dict(v) for k, v in files(conn).items()} == before


def test_changed_file_is_reprocessed_and_keeps_id(conn, settings):
  build_tree(settings.pictures_dir)
  scan.scan(conn, settings.pictures_dir)
  old = files(conn)["2020/a.jpg"]
  path = os.path.join(settings.pictures_dir, "2020", "a.jpg")
  make_jpeg(path, size=(60, 40), color="green")
  os.utime(path, (time.time() + 5, time.time() + 5))
  os.utime(os.path.dirname(path), (time.time() + 5, time.time() + 5))
  p = scan.scan(conn, settings.pictures_dir)
  new = files(conn)["2020/a.jpg"]
  assert p.files_processed == 1
  assert new["id"] == old["id"] and new["width"] == 60
  assert new["hash"] != old["hash"]


def test_appledouble_files_are_not_scanned(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "._a.jpg"))
  with open(os.path.join(settings.pictures_dir, "2020", "a.jpg.xmp"), "w") as f:
    f.write("<x/>")
  with open(os.path.join(settings.pictures_dir, "2020", "._a.jpg.xmp"), "w") as f:
    f.write("garbage")
  p = scan.scan(conn, settings.pictures_dir)
  assert set(files(conn)) == {"2020/a.jpg"}
  assert p.files_seen == 1
  assert conn.execute("SELECT COUNT(*) FROM xmp_sidecars").fetchone()[0] == 1


def test_appledouble_row_from_before_the_fix_becomes_missing(conn, settings):
  # Simulate a row scanned in before ticket 101 existed (is_ignored didn't filter it out yet).
  # The file is still on disk, untouched -- once the filter is in place, the next scan should
  # self-heal the stale row via the normal vanished-file-marked-missing path (it's no longer in
  # `seen`, even though it's still physically there), without any dedicated cleanup code.
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "._a.jpg"))
  conn.execute(
      "INSERT INTO files (path, mtime, missing) VALUES ('2020/._a.jpg', 0, 0)")
  conn.commit()
  scan.scan(conn, settings.pictures_dir)
  assert files(conn)["2020/._a.jpg"]["missing"] == 1


def test_vanished_files_marked_missing_and_reappear(conn, settings):
  build_tree(settings.pictures_dir)
  scan.scan(conn, settings.pictures_dir)
  os.remove(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  assert files(conn)["2020/a.jpg"]["missing"] == 1
  assert files(conn)["top.jpg"]["missing"] == 0
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  assert files(conn)["2020/a.jpg"]["missing"] == 0


def test_vanished_directory_marks_its_files_missing(conn, settings):
  build_tree(settings.pictures_dir)
  scan.scan(conn, settings.pictures_dir)
  os.remove(os.path.join(settings.pictures_dir, "2020", "sub", "b.JPG"))
  os.rmdir(os.path.join(settings.pictures_dir, "2020", "sub"))
  scan.scan(conn, settings.pictures_dir)
  assert files(conn)["2020/sub/b.JPG"]["missing"] == 1


def test_scoped_scan_only_touches_subdirectory(conn, settings):
  build_tree(settings.pictures_dir)
  scan.scan(conn, settings.pictures_dir, os.path.join(settings.pictures_dir, "2020"))
  assert "top.jpg" not in files(conn) and "2020/a.jpg" in files(conn)


def test_precomputed_hash_is_reused_when_mtime_matches(conn, settings):
  build_tree(settings.pictures_dir)
  st = os.stat(os.path.join(settings.pictures_dir, "top.jpg"))
  scan.scan(conn, settings.pictures_dir,
            hashes={"top.jpg": ("precomputed", st.st_mtime)})
  assert files(conn)["top.jpg"]["hash"] == "precomputed"


def test_api_scan_and_status(settings):
  build_tree(settings.pictures_dir)
  from photoapp import db
  app = api.create_app(db.open_state(settings.state_dir), settings)
  client = TestClient(app)
  assert client.post("/api/scan").json() == {"started": True}
  app.state.scanner.wait()
  status = client.get("/api/scan/status").json()
  assert status["running"] is False and status["files_processed"] == 4
  assert client.post("/api/scan", params={"dir": "../etc"}).status_code == 400


def test_parallel_scan_matches_serial_and_is_actually_concurrent(settings, monkeypatch, tmp_path):
  import time
  from photoapp import db, fileinfo
  for i in range(24):
    make_jpeg(os.path.join(settings.pictures_dir, "d", f"{i:02d}.jpg"), color=("red" if i % 2 else "blue"))
  real = fileinfo.read_image_metadata

  def slow(path):
    time.sleep(0.05)              # stands in for network file system latency
    return real(path)

  monkeypatch.setattr(fileinfo, "read_image_metadata", slow)
  serial = db.connect(str(tmp_path / "serial.sqlite"))
  t = time.time(); scan.scan(serial, settings.pictures_dir, workers=1); serial_time = time.time() - t
  parallel = db.connect(str(tmp_path / "parallel.sqlite"))
  t = time.time(); scan.scan(parallel, settings.pictures_dir, workers=8); parallel_time = time.time() - t
  cols = "path, hash, mime_type, width, height, bytesize, exif_date"
  assert [tuple(r) for r in serial.execute(f"SELECT {cols} FROM files ORDER BY path")] == \
         [tuple(r) for r in parallel.execute(f"SELECT {cols} FROM files ORDER BY path")]
  assert serial_time > 1.1 and parallel_time < serial_time / 3


# --- per-directory (per-year) scans, ticket 051 ------------------------------

def build_years(pics):
  from tests.test_grouping import touch
  for year in ("2019", "2020", "2021"):
    touch(os.path.join(pics, year, "a.DNG"))
    make_jpeg(os.path.join(pics, year, "a.JPG"))
    make_jpeg(os.path.join(pics, year, "sub", "b.jpg"))
  make_jpeg(os.path.join(pics, "loose.jpg"))


def snapshot(conn):
  files = [tuple(r) for r in conn.execute(
      "SELECT path, role, missing, photo_id IS NOT NULL FROM files ORDER BY path")]
  photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
  return files, photos


def test_scan_all_matches_a_single_recursive_scan(conn, settings, tmp_path):
  from photoapp import db
  build_years(settings.pictures_dir)
  progress = scan.scan_all(conn, settings.pictures_dir)
  assert progress.error is None and not progress.running and progress.current_dir is None
  assert progress.steps_total == 4 and progress.steps_done == 4      # root files + 3 years
  other = db.connect(str(tmp_path / "other.sqlite"))
  scan.scan(other, settings.pictures_dir)
  assert snapshot(conn) == snapshot(other)
  assert all(grouped for *_, grouped in snapshot(conn)[0])


def test_each_step_is_complete_when_a_later_step_never_runs(conn, settings, monkeypatch):
  from photoapp import grouping
  build_years(settings.pictures_dir)
  real = grouping.regroup
  calls = []

  def dying(conn_, rel_dirs=None):
    calls.append(sorted(rel_dirs or []))
    if "2020" in (rel_dirs or []):         # the 2020 step dies before grouping
      raise RuntimeError("killed")
    return real(conn_, rel_dirs)

  monkeypatch.setattr(grouping, "regroup", dying)
  progress = scan.scan_all(conn, settings.pictures_dir)
  assert progress.error == "killed" and progress.steps_done == 2      # root files, 2019 done
  by_dir = {}
  for path, _, _, grouped in snapshot(conn)[0]:
    by_dir.setdefault(path.rpartition("/")[0] or ".", []).append(grouped)
  assert all(by_dir["2019"]) and all(by_dir["2019/sub"]) and all(by_dir["."])   # finished: usable
  assert not any(by_dir["2020"])                                      # read, not yet grouped
  assert "2021" not in by_dir                                          # never reached

  monkeypatch.setattr(grouping, "regroup", real)
  progress = scan.scan_all(conn, settings.pictures_dir)               # resume
  assert progress.error is None
  assert all(grouped for *_, grouped in snapshot(conn)[0])
  assert progress.dirs_skipped >= 3                                    # finished directories not re-read


def test_a_step_only_groups_its_own_subtree(conn, settings):
  build_years(settings.pictures_dir)
  scan.scan(conn, settings.pictures_dir, os.path.join(settings.pictures_dir, "2019"))
  grouped = {p.split("/")[0] for p, _, _, g in snapshot(conn)[0] if g}
  assert grouped == {"2019"}
  # a second, ungrouped year in the database is left for its own step
  scan.scan(conn, settings.pictures_dir, os.path.join(settings.pictures_dir, "2020"))
  assert {p.split("/")[0] for p, _, _, g in snapshot(conn)[0] if g} == {"2019", "2020"}


def test_root_step_reads_only_the_files_directly_in_the_root(conn, settings):
  build_years(settings.pictures_dir)
  progress = scan.scan(conn, settings.pictures_dir, recursive=False)
  assert [r["path"] for r in conn.execute("SELECT path FROM files")] == ["loose.jpg"]
  assert progress.dirs_seen == 1


def test_removed_year_directory_is_marked_missing_by_the_full_scan(conn, settings):
  import shutil
  build_years(settings.pictures_dir)
  scan.scan_all(conn, settings.pictures_dir)
  shutil.rmtree(os.path.join(settings.pictures_dir, "2020"))
  scan.scan_all(conn, settings.pictures_dir)
  by_year = {}
  for path, _, missing, _ in snapshot(conn)[0]:
    by_year.setdefault(path.split("/")[0], set()).add(missing)
  assert by_year["2020"] == {1} and by_year["2019"] == {0} and by_year["loose.jpg"] == {0}


def test_scan_all_with_dirs_scans_only_those(conn, settings):
  build_years(settings.pictures_dir)
  p = scan.scan_all(conn, settings.pictures_dir, dirs=["2019", "2021"])
  assert p.steps_total == 2
  assert {r["path"].split("/")[0] for r in conn.execute("SELECT path FROM files")} == {"2019", "2021"}


def test_a_scoped_scan_never_reads_the_whole_files_table(conn, settings):
  import re
  build_years(settings.pictures_dir)
  scan.scan_all(conn, settings.pictures_dir)                        # two more years exist
  statements = []
  conn.set_trace_callback(statements.append)
  scan.scan(conn, settings.pictures_dir, os.path.join(settings.pictures_dir, "2019"),
            thumbs_dir=settings.thumbs_dir)
  unbounded = [s_ for s_ in statements if re.search(r"FROM files\b", s_) and "WHERE" not in s_.upper()]
  assert unbounded == []


def test_api_scan_without_a_directory_runs_year_by_year_and_reports_steps(settings):
  from fastapi.testclient import TestClient
  from photoapp import api, db
  build_years(settings.pictures_dir)
  app = api.create_app(db.open_state(settings.state_dir), settings)
  client = TestClient(app)
  assert client.post("/api/scan").json() == {"started": True}
  app.state.scanner.wait()
  status = client.get("/api/scan/status").json()
  assert status["steps_total"] == 4 and status["steps_done"] == 4 and status["error"] is None
  assert status["current_dir"] is None and not status["running"]


# --- Rescan honours the folder's scope, ticket 127 ---------------------------

def scanned_paths(app):
  return {r["path"] for r in app.state.db.execute("SELECT path FROM files")}


def test_api_scan_recursive_false_reads_only_the_requested_subdir(settings):
  from fastapi.testclient import TestClient
  from photoapp import api, db
  build_years(settings.pictures_dir)
  app = api.create_app(db.open_state(settings.state_dir), settings)
  client = TestClient(app)
  assert client.post("/api/scan", params={"dir": "2019", "recursive": 0}).json() == {"started": True}
  app.state.scanner.wait()
  assert scanned_paths(app) == {"2019/a.DNG", "2019/a.JPG"}
  assert client.get("/api/scan/status").json()["error"] is None


def test_api_scan_recursive_true_reads_the_subdir_subtree(settings):
  from fastapi.testclient import TestClient
  from photoapp import api, db
  build_years(settings.pictures_dir)
  app = api.create_app(db.open_state(settings.state_dir), settings)
  client = TestClient(app)
  assert client.post("/api/scan", params={"dir": "2019", "recursive": 1}).json() == {"started": True}
  app.state.scanner.wait()
  assert scanned_paths(app) == {"2019/a.DNG", "2019/a.JPG", "2019/sub/b.jpg"}


def test_api_scan_root_recursive_false_does_not_touch_a_year_directory(settings):
  from fastapi.testclient import TestClient
  from photoapp import api, db
  build_years(settings.pictures_dir)
  app = api.create_app(db.open_state(settings.state_dir), settings)
  client = TestClient(app)
  assert client.post("/api/scan", params={"dir": ".", "recursive": 0}).json() == {"started": True}
  app.state.scanner.wait()
  assert scanned_paths(app) == {"loose.jpg"}
  status = client.get("/api/scan/status").json()
  assert not status["running"] and status["files_seen"] == 1


# --- unified worker-status reporting, ticket 140 ------------------------------

def test_scan_manager_sets_current_dir_and_started_at_for_a_plain_directory_scan(settings):
  from photoapp import db
  db.open_state(settings.state_dir).close()
  build_years(settings.pictures_dir)
  mgr = scan.ScanManager(settings.db_path, settings.pictures_dir)
  mgr.start("2019", recursive=False)
  # Set synchronously by start() itself, before the background thread does any work, so this is
  # deterministic regardless of how fast the tiny test tree scans.
  assert mgr.progress.current_dir == "2019" and mgr.progress.started_at
  mgr.wait()
  assert mgr.progress.current_dir is None    # cleared once the run is over


def test_scan_manager_reports_the_whole_library_request_as_the_root(settings):
  from photoapp import db
  db.open_state(settings.state_dir).close()
  build_years(settings.pictures_dir)
  mgr = scan.ScanManager(settings.db_path, settings.pictures_dir)
  mgr.start(None, recursive=True)
  assert mgr.progress.current_dir == "."   # overwritten per top-level step once scan_all begins
  mgr.wait()
  assert mgr.progress.current_dir is None


def test_api_jobs_reports_the_scanner_separately_from_the_shared_job_queues(settings):
  # ticket 140: /api/jobs's `scan` key is the interactive rescan's own status, independent of
  # `active`/`progress` (the shared jobs-table queues) -- so a busy background job queue can't
  # bury whether the user's own rescan is still running, and vice versa.
  from fastapi.testclient import TestClient
  from photoapp import api, db
  app = api.create_app(db.open_state(settings.state_dir), settings)
  client = TestClient(app)
  assert client.get("/api/jobs").json()["scan"]["running"] is False
  app.state.scanner.progress = scan.Progress(
      running=True, current_dir="2026-new", started_at="2000-01-01T00:00:00",
      files_seen=5, files_processed=2)
  data = client.get("/api/jobs").json()
  assert data["active"] == {"busy": False, "running": []}   # unaffected by the scanner
  assert data["scan"]["running"] is True and data["scan"]["current_dir"] == "2026-new"
  assert data["scan"]["files_seen"] == 5 and data["scan"]["files_processed"] == 2
