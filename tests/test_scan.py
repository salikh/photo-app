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
