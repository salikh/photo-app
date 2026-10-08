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


# ---- ticket 187: metadata -------------------------------------------------------------------

from photoapp import video


def probe_json(width=1920, height=1080, rotate=None, tags=None, duration="12.5"):
  stream = {"codec_type": "video", "codec_name": "h264", "width": width, "height": height,
            "avg_frame_rate": "30000/1001"}
  if rotate is not None:
    stream["side_data_list"] = [{"side_data_type": "Display Matrix", "rotation": rotate}]
  return {"streams": [{"codec_type": "audio"}, stream],
          "format": {"duration": duration, "tags": tags or {}}}


def test_parse_probe_basic_and_rotation():
  info = video.parse_probe(probe_json(), "x.mp4")
  assert (info["width"], info["height"], info["video_codec"]) == (1920, 1080, "h264")
  assert info["duration"] == 12.5 and info["fps"] == 29.97 and info["exif_date"] is None
  for rot in (-90, 90, 270):
    r = video.parse_probe(probe_json(rotate=rot), "x.mp4")
    assert (r["width"], r["height"]) == (1080, 1920), rot
  r = video.parse_probe(probe_json(rotate=180), "x.mp4")
  assert (r["width"], r["height"]) == (1920, 1080)


def test_parse_probe_date_precedence():
  tags = {"creation_time": "2018-06-05T17:14:30.000000Z"}
  assert video.parse_probe(probe_json(tags=tags), "VID_20170101_000000.mp4")["exif_date"] \
      == "2017-01-01 00:00:00"        # local time from the name wins over the UTC container tag
  assert video.parse_probe(probe_json(tags=tags), "holiday.mp4")["exif_date"] \
      == "2018-06-05 17:14:30+0000"
  # an unset clock falls through to the file name, then to nothing (mtime is the sort fallback)
  bad = {"creation_time": "1970-01-01T00:00:00.000000Z"}
  assert video.parse_probe(probe_json(tags=bad), "VID_20180605_171430.mp4")["exif_date"] \
      == "2018-06-05 17:14:30"
  assert video.parse_probe(probe_json(tags=bad), "holiday.mp4")["exif_date"] is None


def test_date_from_filename():
  assert video.date_from_filename("a/PXL_20210102_030405123.mp4") == "2021-01-02 03:04:05"
  assert video.date_from_filename("2018-06-05 17.14.30.mov") == "2018-06-05 17:14:30"
  assert video.date_from_filename("MVI_1234.AVI") is None
  assert video.date_from_filename("20181345_251414.mp4") is None


def test_formatting_of_missing_fields():
  info = video.parse_probe({"streams": [], "format": {}}, "x.mp4")
  assert info["width"] is None and info["duration"] is None and info["fps"] is None


def test_scan_stores_probe_results_and_exposes_them(settings, monkeypatch):
  d = settings.pictures_dir
  touch(os.path.join(d, "t", "VID_20180605_171430.mp4"))
  monkeypatch.setattr(video, "read_info", lambda path, tools=None: video.parse_probe(
      probe_json(rotate=90), path))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  row = conn.execute("SELECT * FROM files").fetchone()
  assert (row["width"], row["height"], row["duration"], row["video_codec"]) == (1080, 1920, 12.5, "h264")
  assert row["exif_date"] == "2018-06-05 17:14:30"
  c = TestClient(api.create_app(conn, settings))
  p = c.get("/api/photos", params={"dir": "t"}).json()["photos"][0]
  assert p["duration"] == 12.5 and p["width"] == 1080
  f = c.get(f"/api/photos/{p['id']}").json()["files"][0]
  assert (f["duration"], f["fps"], f["video_codec"]) == (12.5, 29.97, "h264")
