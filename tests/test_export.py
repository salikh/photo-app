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


def test_default_target_is_an_exported_subfolder_of_pictures_dir(settings):
  d = settings.pictures_dir   # .../pics
  assert export.default_target(d, ".") == os.path.join(d, "Exported")
  assert export.default_target(d, "2020/trip") == os.path.join(d, "Exported", "2020/trip")


def test_validate_target_refuses_library_thumbs_and_state_dirs_except_the_export_subtree(settings):
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
  # The new default (096/098) is deliberately allowed, even though it's inside pictures_dir.
  export.validate_target(settings, export.default_target(settings.pictures_dir, "."))
  export.validate_target(settings, export.default_target(settings.pictures_dir, "2020"))
  # A target elsewhere entirely (a custom folder, or the old ticket 089 sibling location) is
  # still accepted -- only the library/cache/state dirs (minus the carved-out exception) are refused.
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


def test_resolve_dest_path_disambiguates_a_genuine_collision(settings, conn):
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  fid = file_id(conn, "2020/a.jpg")
  candidate = os.path.join(settings.pictures_dir, "Exported", "out.jpg")
  make_jpeg(candidate)   # unrelated file already sitting there, not an export of fid
  scan.scan(conn, settings.pictures_dir)   # so it's a real files row too, with no exported_from
  got = export.resolve_dest_path(conn, settings.pictures_dir, candidate, fid)
  assert got == os.path.join(settings.pictures_dir, "Exported", "out-2.jpg")


def test_resolve_dest_path_reuses_an_earlier_export_of_the_same_source(settings, conn):
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  fid = file_id(conn, "2020/a.jpg")
  candidate = os.path.join(settings.pictures_dir, "Exported", "out.jpg")
  make_jpeg(candidate)
  scan.scan(conn, settings.pictures_dir)
  conn.execute("UPDATE files SET exported_from_file_id = ? WHERE path = ?",
              (fid, "Exported/out.jpg"))
  conn.commit()
  got = export.resolve_dest_path(conn, settings.pictures_dir, candidate, fid)
  assert got == candidate   # same source re-exported: overwrite in place, not disambiguated


def test_resolve_dest_path_respects_taken_within_one_batch(settings, conn):
  candidate = os.path.join(settings.pictures_dir, "Exported", "out.jpg")
  got = export.resolve_dest_path(conn, settings.pictures_dir, candidate, 1, taken={candidate})
  assert got == os.path.join(settings.pictures_dir, "Exported", "out-2.jpg")


def test_link_exported_file_imports_and_cross_references(settings, conn):
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  fid = file_id(conn, "2020/a.jpg")
  dest = os.path.join(settings.pictures_dir, "Exported", "a.jpg")
  os.makedirs(os.path.dirname(dest), exist_ok=True)
  make_jpeg(dest)
  new_id = export.link_exported_file(conn, settings, fid, dest)
  assert new_id is not None
  row = conn.execute("SELECT exported_from_file_id, photo_id FROM files WHERE id = ?",
                     (new_id,)).fetchone()
  assert row["exported_from_file_id"] == fid
  assert row["photo_id"] is not None   # got its own Photo (Option B), not merged into fid's


def test_link_exported_file_is_a_noop_outside_the_library(settings, conn):
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  fid = file_id(conn, "2020/a.jpg")
  outside = os.path.join(os.path.dirname(settings.pictures_dir), "elsewhere", "a.jpg")
  os.makedirs(os.path.dirname(outside), exist_ok=True)
  make_jpeg(outside)
  assert export.link_exported_file(conn, settings, fid, outside) is None


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
  assert c.get("/api/export/default_path").json()["path"] == \
      os.path.join(settings.pictures_dir, "Exported")
  assert c.get("/api/export/default_path", params={"dir": "2020"}).json()["path"] == \
      os.path.join(settings.pictures_dir, "Exported", "2020")
  assert c.get("/api/export/default_path", params={"dir": "../x"}).status_code == 400


def test_export_into_the_library_is_imported_and_linked_to_the_source(settings):
  conn = db.open_state(settings.state_dir)
  d = settings.pictures_dir
  build(conn, d)
  source_photo_id, source_file_id = [
      (r["photo_id"], r["file_id"]) for r in conn.execute(
          "SELECT p.id AS photo_id, rf.id AS file_id FROM photos p "
          "JOIN files rf ON rf.id = p.representative_file_id "
          "WHERE rf.path = '2020/a.jpg'")][0]
  app = api.create_app(conn, settings)
  c = TestClient(app)
  target = export.default_target(d, "2020")
  r = c.post("/api/export", json={"ids": [source_photo_id], "dir": "2020", "target": target})
  assert r.status_code == 200, r.text
  app.state.jobs.start()
  assert app.state.jobs.wait_idle()
  app.state.jobs.stop()
  assert [j["state"] for j in c.get("/api/jobs").json()["jobs"]] == ["done"]

  photos_here = c.get("/api/photos", params={"dir": "Exported/2020"}).json()["photos"]
  assert len(photos_here) == 1
  exported_photo_id = photos_here[0]["id"]
  detail = c.get(f"/api/photos/{exported_photo_id}").json()
  exported_from = detail["files"][0]["exported_from"]
  assert exported_from == {"dir": "2020", "photo_id": source_photo_id}
  # The source's own Photo is untouched -- still its own, single-file entry, not merged.
  assert len(c.get("/api/photos", params={"dir": "2020"}).json()["photos"]) == 1


def test_reexporting_the_same_source_overwrites_in_place(settings):
  conn = db.open_state(settings.state_dir)
  d = settings.pictures_dir
  build(conn, d)
  source_photo_id = conn.execute(
      "SELECT p.id FROM photos p JOIN files rf ON rf.id = p.representative_file_id "
      "WHERE rf.path = '2020/a.jpg'").fetchone()[0]
  app = api.create_app(conn, settings)
  c = TestClient(app)
  target = export.default_target(d, "2020")
  for _ in range(2):
    r = c.post("/api/export", json={"ids": [source_photo_id], "dir": "2020", "target": target})
    assert r.status_code == 200, r.text
    app.state.jobs.start()
    assert app.state.jobs.wait_idle()
    app.state.jobs.stop()
  photos_here = c.get("/api/photos", params={"dir": "Exported/2020"}).json()["photos"]
  assert len(photos_here) == 1   # re-exported in place, not a second, suffixed file


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
