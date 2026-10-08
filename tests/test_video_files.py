"""Ticket 185: video files are indexed as ordinary files, implicitly tagged 'video'."""
import os

import pytest

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


# ---- ticket 191: serving ----------------------------------------------------------------------

def served_app(settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "v", "a.mp4"), bytes(range(256)) * 40)      # 10240 bytes
  make_jpeg(os.path.join(d, "v", "p.jpg"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  ids = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM files")}
  return TestClient(api.create_app(conn, settings)), conn, ids


def test_original_video_supports_range_requests(settings):
  c, _, ids = served_app(settings)
  full = c.get(f"/video/{ids['v/a.mp4']}")
  assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
  assert full.headers["accept-ranges"] == "bytes" and len(full.content) == 10240
  part = c.get(f"/video/{ids['v/a.mp4']}", headers={"Range": "bytes=100-199"})
  assert part.status_code == 206 and part.content == full.content[100:200]
  assert part.headers["content-range"] == "bytes 100-199/10240"
  tail = c.get(f"/video/{ids['v/a.mp4']}", headers={"Range": "bytes=-16"})
  assert tail.status_code == 206 and tail.content == full.content[-16:]
  bad = c.get(f"/video/{ids['v/a.mp4']}", headers={"Range": "bytes=99999-"})
  assert bad.status_code == 416


def test_video_route_refuses_stills_and_unknown_files(settings):
  c, _, ids = served_app(settings)
  assert c.get(f"/video/{ids['v/p.jpg']}").status_code == 404
  assert c.get("/video/99999").status_code == 404


def test_anim_thumbnail_route_and_has_anim_flag(settings):
  from photoapp import thumbs
  c, conn, ids = served_app(settings)
  fid = ids["v/a.mp4"]
  assert c.get(f"/anim/AnimThumb/{fid}").status_code == 404           # not made yet: fall back
  p = c.get("/api/photos", params={"dir": "v", "sort": "name"}).json()["photos"][0]
  assert p["is_video"] and p["has_anim"] is False
  dest = thumbs.anim_path(settings.thumbs_dir, "AnimThumb", "v/a.mp4")
  os.makedirs(os.path.dirname(dest)); open(dest, "wb").write(b"webm-bytes")
  thumbs.record(conn, fid, "AnimThumb", dest, "anim-v1"); conn.commit()
  r = c.get(f"/anim/AnimThumb/{fid}")
  assert r.status_code == 200 and r.headers["content-type"] == "video/webm" and r.content == b"webm-bytes"
  assert c.get(f"/anim/AnimSmall/{fid}").status_code == 404
  assert c.get(f"/anim/Bogus/{fid}").status_code == 404
  assert c.get(f"/anim/AnimThumb/{ids['v/p.jpg']}").status_code == 404
  p = c.get("/api/photos", params={"dir": "v", "sort": "name"}).json()["photos"][0]
  assert p["has_anim"] is True
  assert c.get(f"/api/photos/{p['id']}").json()["files"][0]["has_anim"] is True


def test_video_tag_is_offered_in_the_tag_dropdown_with_a_matching_count(settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "t", "a.jpg"))
  touch(os.path.join(d, "t", "b.mp4"))
  touch(os.path.join(d, "t", "c.MOV"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  c = TestClient(api.create_app(conn, settings))
  tags = c.get("/api/photos/tags", params={"dir": "t"}).json()["tags"]
  assert tags == [{"tag": "video", "count": 2}]
  shown = c.get("/api/photos", params={"dir": "t", "filter": "tag:video"}).json()
  assert shown["total"] == 2
  assert c.get("/api/photos/tags", params={"dir": "."}).json()["tags"] == []   # not recursive
  assert c.get("/api/photos/tags", params={"dir": ".", "recursive": 1}).json()["tags"] == tags


# ---- ticket 195: exclusions, and the features that must keep working ----------------------------

def test_ai_rate_and_export_skip_videos(settings, monkeypatch, tmp_path):
  monkeypatch.setenv("GEMINI_API_KEY", "K")
  c, conn, ids = served_app(settings)
  pids = {r["path"]: r["id"] for r in conn.execute(
      "SELECT f.path, p.id FROM photos p JOIN files f ON f.id = p.representative_file_id")}
  both = [pids["v/a.mp4"], pids["v/p.jpg"]]
  r = c.post("/api/ai/rate", json={"ids": both}).json()
  assert r == {"queued": 1, "skipped_videos": 1, "missing": 0}
  assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='ai_rate'").fetchone()[0] == 1
  r = c.post("/api/export", json={"ids": both, "dir": "v", "target": str(tmp_path / "out")}).json()
  assert len(r["queued"]) == 1 and r["skipped_videos"] == 1 and r["missing"] == []
  # and the job handlers refuse a video enqueued by anything else
  from photoapp import ai_rating, export
  with pytest.raises(ai_rating.AiRatingError, match="videos"):
    ai_rating.rate_file(conn, settings, ids["v/a.mp4"])
  with pytest.raises(export.ExportError, match="videos"):
    export.export_file(conn, settings, ids["v/a.mp4"], "v/a.mp4", str(tmp_path / "x.jpg"))


def test_rating_sidecar_trash_and_move_work_for_a_video(settings):
  d = settings.pictures_dir
  c, conn, ids = served_app(settings)
  pid = conn.execute("SELECT photo_id FROM files WHERE path = 'v/a.mp4'").fetchone()[0]
  assert c.post(f"/api/photos/{pid}/rating", json={"rating": 4}).status_code == 200
  assert os.path.isfile(os.path.join(d, "v", "a.mp4.xmp"))
  scan.scan(conn, d)                                        # the sidecar is read back, not lost
  assert conn.execute("SELECT rating FROM photos WHERE id = ?", (pid,)).fetchone()[0] == 4
  assert c.post(f"/api/photos/{pid}/tags", json={"add": ["trip"], "remove": []}).status_code == 200
  assert c.post("/api/move", json={"ids": [pid], "target": "moved"}).status_code == 200
  assert os.path.isfile(os.path.join(d, "moved", "a.mp4"))
  assert os.path.isfile(os.path.join(d, "moved", "a.mp4.xmp"))
  assert not os.path.exists(os.path.join(d, "v", "a.mp4"))
  assert c.post(f"/api/photos/{pid}/rating", json={"rating": -1}).status_code == 200
  r = c.post("/api/photos/trash", json={"ids": [pid]})
  assert r.status_code == 200, r.text
  assert os.path.isfile(os.path.join(d, ".trash", "moved", "a.mp4"))


# ---- ticket 200: the rating filter and the tag filter are ANDed -----------------------------------

def test_rating_and_tag_filters_are_anded(settings):
  from photoapp import library
  d = settings.pictures_dir
  for n in ("a", "b", "c", "d"):
    make_jpeg(os.path.join(d, "t", f"{n}.jpg"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  pid = {r["path"]: r["id"] for r in conn.execute(
      "SELECT f.path, p.id FROM photos p JOIN files f ON f.id = p.representative_file_id")}
  for path, rating, tags in (("t/a.jpg", 4, ["trip"]), ("t/b.jpg", 4, []),
                             ("t/c.jpg", 2, ["trip"]), ("t/d.jpg", -1, ["trip"])):
    conn.execute("UPDATE photos SET rating = ? WHERE id = ?", (rating, pid[path]))
    for t in tags:
      conn.execute("INSERT INTO tags (photo_id, tag) VALUES (?, ?)", (pid[path], t))
  conn.commit()

  def names(**kw):
    return [p["name"] for p in library.list_photos(conn, "t", sort="name", **kw)["photos"]]

  assert names(filter="rating:4") == ["a.jpg", "b.jpg"]
  assert names(filter="all", tag="trip") == ["a.jpg", "c.jpg", "d.jpg"]
  assert names(filter="rating:4", tag="trip") == ["a.jpg"]                  # the AND
  assert names(filter="rating>=2", tag="trip") == ["a.jpg", "c.jpg"]
  assert names(filter="rejected", tag="trip") == ["d.jpg"]
  assert names(filter="tag:trip") == ["a.jpg", "c.jpg", "d.jpg"]            # legacy form still works
  assert names(filter="tag:trip", tag="other") == []                         # explicit tag wins
  counts = library.filter_counts(conn, "t", tag="trip")["counts"]
  assert counts["all"] == 3 and counts["rating:4"] == 1 and counts["rejected"] == 1
  assert library.filter_counts(conn, "t")["counts"]["all"] == 4
  c = TestClient(api.create_app(conn, settings))
  r = c.get("/api/photos", params={"dir": "t", "filter": "rating:4", "tag": "trip"}).json()
  assert [p["name"] for p in r["photos"]] == ["a.jpg"] and r["total"] == 1
  assert c.get("/api/photos/counts", params={"dir": "t", "tag": "trip"}).json()["counts"]["all"] == 3
