import os

from fastapi.testclient import TestClient
from PIL import Image

from photoapp import api
from photoapp import db
from photoapp import export
from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg


def file_id(conn, path):
  return conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def test_default_target_swaps_pictures_for_exported_next_to_it(settings):
  d = settings.pictures_dir   # .../pics
  parent = os.path.dirname(d)
  assert export.default_target(d, ".") == os.path.join(parent, "Exported")
  assert export.default_target(d, "2020/trip") == os.path.join(parent, "Exported", "2020/trip")


def test_validate_target_refuses_library_thumbs_and_state_dirs(settings):
  for guarded in (settings.pictures_dir, settings.thumbs_dir, settings.state_dir):
    try:
      export.validate_target(settings, guarded)
      assert False, f"should have refused {guarded}"
    except export.ExportError:
      pass
    try:
      export.validate_target(settings, os.path.join(guarded, "sub"))
      assert False, f"should have refused a path under {guarded}"
    except export.ExportError:
      pass
  export.validate_target(settings, os.path.join(os.path.dirname(settings.pictures_dir), "Exported"))


def test_validate_target_refuses_relative_or_empty_paths(settings):
  for bad in ("", "relative/path"):
    try:
      export.validate_target(settings, bad)
      assert False, f"should have refused {bad!r}"
    except export.ExportError:
      pass


def test_within_dir_is_the_subpath_relative_to_the_browsed_folder():
  assert export.within_dir(".", "2020/a.jpg") == "2020"
  assert export.within_dir("2020", "2020/a.jpg") == ""
  assert export.within_dir("2020", "2020/trip/c.jpg") == "trip"
  assert export.within_dir("2020/trip", "2020/trip/deep/d.jpg") == "deep"


def test_dest_path_mirrors_the_subpath_under_the_target_root():
  assert export.dest_path("/out", "2020", "2020/a.jpg") == "/out/a.jpg"
  assert export.dest_path("/out", "2020", "2020/trip/c.jpg") == "/out/trip/c.jpg"
  # a RAW's mapped name (thumbs.thumb_relpath), not its own extension
  assert export.dest_path("/out", ".", "2020/K1.DNG") == "/out/2020/K1.DNG.jpg"


def test_export_file_always_re_encodes_even_an_already_jpeg_source(settings, conn):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"), size=(40, 30))
  scan.scan(conn, d)
  fid = file_id(conn, "2020/a.jpg")
  dest = os.path.join(str(settings.state_dir) + "-out", "a.jpg")
  out = export.export_file(conn, settings, fid, "2020/a.jpg", dest)
  assert out == dest
  assert os.path.isfile(dest)
  with Image.open(dest) as im:
    assert im.format == "JPEG" and im.size == (40, 30)
  # thumbs.ensure's own cache also got populated as a side effect (matching the on-demand path)
  assert thumbs.lookup(settings.thumbs_dir, "Huge", "2020/a.jpg") is not None


def test_export_file_raises_when_nothing_can_be_decoded(settings, conn):
  d = settings.pictures_dir
  os.makedirs(os.path.join(d, "2020"), exist_ok=True)
  with open(os.path.join(d, "2020", "broken.jpg"), "wb") as f:
    f.write(b"not an image")
  scan.scan(conn, d)
  fid = file_id(conn, "2020/broken.jpg")
  dest = os.path.join(str(settings.state_dir) + "-out", "broken.jpg")
  try:
    export.export_file(conn, settings, fid, "2020/broken.jpg", dest)
    assert False, "should have raised"
  except export.ExportError:
    pass
  assert not os.path.exists(dest)


# ------------------------------------------------------------- API endpoints ------------------

def build(conn, d):
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  make_jpeg(os.path.join(d, "2020", "trip", "c.jpg"))
  scan.scan(conn, d)


def test_export_default_path_endpoint(settings):
  conn = db.open_state(settings.state_dir)
  build(conn, settings.pictures_dir)
  c = TestClient(api.create_app(conn, settings))
  parent = os.path.dirname(settings.pictures_dir)
  assert c.get("/api/export/default_path").json()["path"] == os.path.join(parent, "Exported")
  assert c.get("/api/export/default_path", params={"dir": "2020"}).json()["path"] == \
      os.path.join(parent, "Exported", "2020")
  assert c.get("/api/export/default_path", params={"dir": "../x"}).status_code == 400


def test_export_queues_jobs_and_produces_mirrored_files(settings, tmp_path):
  conn = db.open_state(settings.state_dir)
  d = settings.pictures_dir
  build(conn, d)
  ids = [r["id"] for r in conn.execute(
      "SELECT id FROM photos ORDER BY id")]
  app = api.create_app(conn, settings)
  c = TestClient(app)
  target = str(tmp_path / "out")

  r = c.post("/api/export", json={"ids": ids, "dir": "2020", "target": target})
  assert r.status_code == 200, r.text
  body = r.json()
  assert len(body["queued"]) == 2 and body["missing"] == []
  assert c.get("/api/jobs").json()["counts"] == {"queued": 2}

  app.state.jobs.start()
  assert app.state.jobs.wait_idle()
  app.state.jobs.stop()

  assert [j["state"] for j in c.get("/api/jobs").json()["jobs"]] == ["done", "done"]
  assert os.path.isfile(os.path.join(target, "a.jpg"))          # directly in 2020
  assert os.path.isfile(os.path.join(target, "trip", "c.jpg"))  # mirrored subfolder
  with Image.open(os.path.join(target, "a.jpg")) as im:
    assert im.format == "JPEG"


def test_export_refuses_a_target_inside_the_library_or_the_cache(settings):
  conn = db.open_state(settings.state_dir)
  build(conn, settings.pictures_dir)
  ids = [r["id"] for r in conn.execute("SELECT id FROM photos")]
  c = TestClient(api.create_app(conn, settings))
  for bad in (settings.pictures_dir, settings.thumbs_dir, settings.state_dir,
             os.path.join(settings.pictures_dir, "sub")):
    r = c.post("/api/export", json={"ids": ids, "dir": ".", "target": bad})
    assert r.status_code == 400, (bad, r.text)


def test_export_reports_an_id_that_is_no_longer_a_live_photo(settings, tmp_path):
  conn = db.open_state(settings.state_dir)
  build(conn, settings.pictures_dir)
  real_id = conn.execute("SELECT id FROM photos LIMIT 1").fetchone()[0]
  c = TestClient(api.create_app(conn, settings))
  r = c.post("/api/export", json={"ids": [real_id, 999999], "dir": "2020", "target": str(tmp_path / "out")})
  assert r.status_code == 200
  body = r.json()
  assert len(body["queued"]) == 1 and body["missing"] == [999999]
