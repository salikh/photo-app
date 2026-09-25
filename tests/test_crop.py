import os

import pytest
from PIL import Image

from photoapp import crop
from photoapp import db
from photoapp import library
from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg


def test_validate_and_whole_frame_normalizes_to_null(settings):
  conn = db.open_state(settings.state_dir)
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()["id"]

  crop.set(conn, fid, 0.25, 0.1, 0.5, 0.6)
  got = crop.get(conn, fid)
  assert (got["crop_x"], got["crop_y"], got["crop_w"], got["crop_h"]) == (0.25, 0.1, 0.5, 0.6)
  assert not crop.is_default(got)

  crop.set(conn, fid, 0.0, 0.0, 1.0, 1.0)   # whole frame == no crop
  assert crop.is_default(crop.get(conn, fid))
  conn.close()


@pytest.mark.parametrize("rect", [
    (0.9, 0.0, 0.2, 0.5),    # right edge past 1
    (0.0, 0.0, 0.01, 0.5),   # too narrow
    (-0.1, 0.0, 0.5, 0.5),   # negative x
    (0.0, 0.0, 1.5, 0.5),    # too wide
])
def test_validate_rejects_bad_rectangles(settings, rect):
  with pytest.raises(crop.CropError):
    crop.validate(*rect)


def test_pixel_box_rounds_inside_the_image():
  assert crop.pixel_box(None, 100, 50) == (0, 0, 100, 50)
  assert crop.pixel_box(crop.to_columns(0.25, 0.25, 0.5, 0.5), 100, 50) == (25, 12, 50, 25)


def test_thumb_and_small_are_cropped_but_medium_is_full(settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()["id"]
  crop.set(conn, fid, 0.25, 0.25, 0.5, 0.5)
  c = crop.get(conn, fid)

  thumb = thumbs.make(settings.pictures_dir, settings.thumbs_dir, "a.jpg", "Thumb", crop=c)[0]
  small = thumbs.make(settings.pictures_dir, settings.thumbs_dir, "a.jpg", "Small", crop=c)[0]
  medium = thumbs.make(settings.pictures_dir, settings.thumbs_dir, "a.jpg", "Medium", crop=c)[0]
  assert Image.open(thumb).size == (50, 25)     # cropped then not upscaled
  assert Image.open(small).size == (50, 25)
  assert Image.open(medium).size == (100, 50)   # full frame
  conn.close()


def test_ensure_uses_the_stored_crop(settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()["id"]
  crop.set(conn, fid, 0.25, 0.25, 0.5, 0.5)
  path = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", "Thumb")
  assert Image.open(path).size == (50, 25)
  conn.close()


def test_crop_endpoint_saves_clears_and_rejects(settings):
  import io
  from fastapi.testclient import TestClient
  from photoapp import api
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()["id"]
  thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", "Thumb")
  assert thumbs.lookup(settings.thumbs_dir, "Thumb", "a.jpg")
  app = api.create_app(conn, settings)
  client = TestClient(app)

  r = client.post(f"/api/files/{fid}/crop", json={"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5})
  assert r.status_code == 200 and r.json()["crop"]["crop_w"] == 0.5
  assert r.json()["rev"] == 1          # ticket 119: a render change bumps the file's revision
  assert thumbs.lookup(settings.thumbs_dir, "Thumb", "a.jpg") is None   # cache cleared
  img = Image.open(io.BytesIO(client.get(f"/img/Thumb/{fid}").content))
  assert img.size == (50, 25)   # Thumb served cropped

  assert client.post(f"/api/files/{fid}/crop",
                     json={"x": 0.9, "y": 0, "w": 0.5, "h": 0.5}).status_code == 400
  assert client.post("/api/files/99999/crop",
                     json={"x": 0, "y": 0, "w": 0.5, "h": 0.5}).status_code == 404

  r = client.post(f"/api/files/{fid}/crop", json={"x": 0, "y": 0, "w": 1, "h": 1})
  assert r.json()["crop"]["crop_x"] is None
  conn.close()


def test_list_photos_and_detail_carry_the_crop(settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(100, 50))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()["id"]
  crop.set(conn, fid, 0.1, 0.2, 0.3, 0.4)

  page = library.list_photos(conn, ".")
  assert page["photos"][0]["crop"] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
  assert page["photos"][0]["rev"] == 1   # ticket 119: the client uses this to cache-bust /img URLs
  detail = library.photo_detail(conn, page["photos"][0]["id"])
  assert detail["files"][0]["crop_x"] == 0.1
  assert detail["files"][0]["thumb_rev"] == 1
  conn.close()
