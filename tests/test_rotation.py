import io
import os

import pytest
from PIL import Image

from photoapp import db
from photoapp import export
from photoapp import library
from photoapp import rotation
from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg


def _file_id(conn, path="a.jpg"):
  return conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()["id"]


def test_validate_rejects_non_multiples_of_90():
  rotation.validate(90)
  rotation.validate(-90)
  rotation.validate(0)
  rotation.validate(None)
  for bad in (45, 1, 100, 90.0, "90", True):
    with pytest.raises(rotation.RotationError):
      rotation.validate(bad)


def test_normalize_wraps_and_zero_becomes_none():
  assert rotation.normalize(0) is None
  assert rotation.normalize(360) is None
  assert rotation.normalize(90) == 90
  assert rotation.normalize(-90) == 270
  assert rotation.normalize(450) == 90


def test_apply_turns_counter_clockwise_and_expands():
  img = Image.new("RGB", (4, 2), "red")
  assert rotation.apply(img, None).size == (4, 2)
  assert rotation.apply(img, 0).size == (4, 2)
  assert rotation.apply(img, 90).size == (2, 4)
  assert rotation.apply(img, 180).size == (4, 2)
  assert rotation.apply(img, 270).size == (2, 4)


def test_set_get_and_clear(settings):
  conn = db.open_state(settings.state_dir)
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  fid = _file_id(conn)
  assert rotation.get(conn, fid) == 0
  rotation.set(conn, fid, 90)
  assert rotation.get(conn, fid) == 90
  assert not rotation.is_default(rotation.get(conn, fid))
  assert conn.execute("SELECT thumb_rev FROM files WHERE id = ?", (fid,)).fetchone()[0] == 1
  rotation.set(conn, fid, 0)
  assert rotation.get(conn, fid) == 0
  conn.close()


def test_set_rejects_unknown_file_and_bad_angle(settings):
  conn = db.open_state(settings.state_dir)
  with pytest.raises(rotation.RotationError):
    rotation.set(conn, 99999, 90)
  with pytest.raises(rotation.RotationError):
    rotation.set(conn, 1, 45)
  conn.close()


def test_every_size_is_rotated(settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = _file_id(conn)
  rotation.set(conn, fid, 90)
  r = rotation.get(conn, fid)
  for size in ("Thumb", "Small", "Medium", "Huge"):
    path = thumbs.make(settings.pictures_dir, settings.thumbs_dir, "a.jpg", size, rotation=r)[0]
    assert Image.open(path).size == (50, 100), size
  conn.close()


def test_downscale_from_a_larger_cache_is_not_rotated_twice(settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = _file_id(conn)
  rotation.set(conn, fid, 90)
  r = rotation.get(conn, fid)
  huge = thumbs.make(settings.pictures_dir, settings.thumbs_dir, "a.jpg", "Huge", rotation=r)[0]
  assert Image.open(huge).size == (50, 100)
  medium = thumbs.make(settings.pictures_dir, settings.thumbs_dir, "a.jpg", "Medium", rotation=r)[0]
  assert Image.open(medium).size == (50, 100)   # not (100, 50): the cached Huge is already turned
  conn.close()


def test_ensure_uses_the_stored_rotation(settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = _file_id(conn)
  rotation.set(conn, fid, 270)
  path = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", "Thumb")
  assert Image.open(path).size == (50, 100)
  conn.close()


def test_endpoint_saves_clears_and_rejects(settings):
  from fastapi.testclient import TestClient
  from photoapp import api
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = _file_id(conn)
  thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", "Thumb")
  assert thumbs.lookup(settings.thumbs_dir, "Thumb", "a.jpg")
  app = api.create_app(conn, settings)
  client = TestClient(app)

  r = client.post(f"/api/files/{fid}/rotation", json={"rotation": 90})
  assert r.status_code == 200 and r.json()["rotation"] == 90
  assert r.json()["rev"] == 1
  assert thumbs.lookup(settings.thumbs_dir, "Thumb", "a.jpg") is None   # cache cleared
  img = Image.open(io.BytesIO(client.get(f"/img/Thumb/{fid}").content))
  assert img.size == (50, 100)   # served turned

  assert client.post(f"/api/files/{fid}/rotation", json={"rotation": 45}).status_code == 400
  assert client.post("/api/files/99999/rotation", json={"rotation": 90}).status_code == 404

  r = client.post(f"/api/files/{fid}/rotation", json={"rotation": 0})
  assert r.json()["rotation"] == 0
  conn.close()


def test_list_photos_and_detail_carry_rotation(settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = _file_id(conn)
  rotation.set(conn, fid, 90)
  page = library.list_photos(conn, ".")
  assert page["photos"][0]["rotation"] == 90
  detail = library.photo_detail(conn, page["photos"][0]["id"])
  assert detail["files"][0]["rotation"] == 90
  conn.close()


def test_export_file_applies_rotation(settings, conn):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"), size=(40, 30))
  scan.scan(conn, d)
  fid = _file_id(conn, "2020/a.jpg")
  rotation.set(conn, fid, 90)
  dest = os.path.join(str(settings.state_dir) + "-out", "a.jpg")
  out = export.export_file(conn, settings, fid, "2020/a.jpg", dest)
  assert out == dest
  with Image.open(dest) as im:
    assert im.format == "JPEG" and im.size == (30, 40)
