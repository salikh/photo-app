"""Ticket 185: video files are indexed as ordinary files, implicitly tagged 'video'."""
import os

from fastapi.testclient import TestClient

from photoapp import api
from photoapp import db
from photoapp import fileinfo
from photoapp import scan
from tests.conftest import make_jpeg


def touch(path, data=b"not really a video"):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "wb") as f:
    f.write(data)


def test_video_predicates():
  for name in ("a.mp4", "A.MOV", "x.avi", "x.3gp", "x.mpg", "x.MKV", "x.mts", "x.m2ts"):
    assert fileinfo.is_video(name) and fileinfo.is_media(name) and not fileinfo.is_image(name)
  assert not fileinfo.is_video("a.jpg") and fileinfo.is_media("a.jpg")
  assert not fileinfo.is_media("notes.txt") and not fileinfo.is_media("a.mp4.xmp")


def test_videos_are_scanned_as_their_own_photos_with_the_implied_video_tag(settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "t", "IMG_1.jpg"))
  touch(os.path.join(d, "t", "IMG_1.mp4"))        # same stem as a JPEG: still its own Photo
  touch(os.path.join(d, "t", "clip.MOV"))
  touch(os.path.join(d, "t", "notes.txt"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  rows = {r["path"]: r for r in conn.execute("SELECT * FROM files")}
  assert set(rows) == {"t/IMG_1.jpg", "t/IMG_1.mp4", "t/clip.MOV"}
  assert rows["t/clip.MOV"]["mime_type"] == "video/quicktime"
  assert rows["t/IMG_1.mp4"]["hash"] and rows["t/IMG_1.mp4"]["photo_id"] != rows["t/IMG_1.jpg"]["photo_id"]

  c = TestClient(api.create_app(conn, settings))
  r = c.get("/api/photos", params={"dir": "t", "filter": "tag:video", "sort": "name"})
  assert r.status_code == 200, r.text
  photos = r.json()["photos"]
  assert [p["path"] for p in photos] == ["t/clip.MOV", "t/IMG_1.mp4"]
  assert all(p["is_video"] and p["implied"] == ["video"] for p in photos)
  detail = c.get(f"/api/photos/{photos[0]['id']}").json()
  assert detail["implied"] == ["video"] and detail["files"][0]["is_video"] is True
  assert photos[0]["name"] == "clip.MOV"
  allp = c.get("/api/photos", params={"dir": "t", "sort": "name"}).json()["photos"]
  assert [p["is_video"] for p in allp] == [True, False, True]
