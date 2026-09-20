import dataclasses
import json
import os

import pytest
from fastapi.testclient import TestClient

from photoapp import api
from photoapp import curation
from photoapp import db
from photoapp import scan
from photoapp import xmp
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan_sidecars import FAV, XMP, write


def photo_id(conn, path):
  return conn.execute("SELECT photo_id FROM files WHERE path = ?",
                      (path,)).fetchone()[0]


def setup_pair(conn, settings, rating_dng=1, rating_jpg=1):
  d = settings.pictures_dir
  touch(os.path.join(d, "y", "K1.DNG"))
  make_jpeg(os.path.join(d, "y", "K1.JPG"))
  write(os.path.join(d, "y", "K1.DNG.xmp"), XMP % (rating_dng, ""), mtime=1_000_000)
  write(os.path.join(d, "y", "K1.JPG.xmp"), XMP % (rating_jpg, ""), mtime=1_000_000)
  scan.scan(conn, d)
  return photo_id(conn, "y/K1.DNG")


def test_rating_writes_only_the_originals_sidecar_and_backs_it_up(conn, settings):
  pid = setup_pair(conn, settings)
  jpg_before = open(os.path.join(settings.pictures_dir, "y/K1.JPG.xmp"), "rb").read()
  dng_before = open(os.path.join(settings.pictures_dir, "y/K1.DNG.xmp"), "rb").read()
  r = curation.set_rating(conn, settings, pid, 4)
  assert r["sidecar"] == "y/K1.DNG.xmp" and r["changes"]["rating"] == {"old": "1", "new": "4"}
  dng = open(os.path.join(settings.pictures_dir, "y/K1.DNG.xmp"), "rb").read()
  assert xmp.parse(dng).rating == 4
  assert open(os.path.join(settings.pictures_dir, "y/K1.JPG.xmp"), "rb").read() == jpg_before
  backups = [os.path.join(dp, f) for dp, _, fs in os.walk(settings.state_dir) for f in fs
             if "xmp_backups" in dp]
  assert len(backups) == 1 and open(backups[0], "rb").read() == dng_before
  state = curation.photo_state(conn, pid)
  assert state["rating"] == 4 and state["conflict"]          # JPG sidecar still says 1
  assert conn.execute("SELECT rating_source FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "xmp"
  log = curation.recent_activity(conn)
  assert [(l["field"], l["old"], l["new"], l["cause"]) for l in log] == [("rating", "1", "4", "user")]
  # second edit: no new backup
  curation.set_rating(conn, settings, pid, 5)
  assert len([f for dp, _, fs in os.walk(settings.state_dir) for f in fs if "xmp_backups" in dp]) == 1


def test_reject_remembers_previous_stars_and_undo_restores(conn, settings):
  pid = setup_pair(conn, settings, 3, 3)
  curation.set_rating(conn, settings, pid, -1)
  assert curation.photo_state(conn, pid)["previous_stars"] == 3
  entry = curation.recent_activity(conn)[0]
  curation.undo(conn, settings, entry["id"])
  assert curation.photo_state(conn, pid)["rating"] == 3
  assert conn.execute("SELECT undone FROM activity_log WHERE id = ?", (entry["id"],)).fetchone()[0] == 1
  with pytest.raises(curation.CurationError):
    curation.undo(conn, settings, entry["id"])            # already undone


def test_undo_refused_when_value_changed_since(conn, settings):
  pid = setup_pair(conn, settings)
  curation.set_rating(conn, settings, pid, 2)
  first = curation.recent_activity(conn)[0]
  curation.set_rating(conn, settings, pid, 5)
  with pytest.raises(curation.CurationError):
    curation.undo(conn, settings, first["id"])
  assert curation.photo_state(conn, pid)["rating"] == 5


def test_fav_and_tags_round_trip_with_undo(conn, settings):
  pid = setup_pair(conn, settings)
  curation.set_fav(conn, settings, pid, True)
  curation.edit_tags(conn, settings, pid, add=["trip", " family "])
  s = curation.photo_state(conn, pid)
  assert s["fav"] and s["tags"] == ["family", "trip"]
  parsed = xmp.parse(open(os.path.join(settings.pictures_dir, "y/K1.DNG.xmp"), "rb").read())
  assert parsed.fav and set(parsed.tags) == {"trip", "family"}
  tags_entry = curation.recent_activity(conn)[0]
  curation.undo(conn, settings, tags_entry["id"])
  assert curation.photo_state(conn, pid)["tags"] == []
  curation.set_fav(conn, settings, pid, False)
  assert not xmp.parse(open(os.path.join(settings.pictures_dir, "y/K1.DNG.xmp"), "rb").read()).fav
  with pytest.raises(curation.CurationError):
    curation.edit_tags(conn, settings, pid, add=["fav"])


def test_photo_without_sidecar_gets_a_new_one_named_by_style(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "b.jpg"))
  scan.scan(conn, d)
  curation.set_rating(conn, settings, photo_id(conn, "a.dng"), 2)
  curation.set_rating(conn, settings, photo_id(conn, "b.jpg"), 3)
  assert xmp.parse(open(os.path.join(d, "a.dng.xmp"), "rb").read()).rating == 2   # 'full'
  assert xmp.parse(open(os.path.join(d, "b.jpg.xmp"), "rb").read()).rating == 3
  stem = dataclasses.replace(settings, new_raw_sidecar_style="stem")
  touch(os.path.join(d, "c.dng"))
  os.utime(d, None)
  scan.scan(conn, d)
  curation.set_rating(conn, stem, photo_id(conn, "c.dng"), 1)
  assert os.path.exists(os.path.join(d, "c.xmp"))


def test_noop_does_not_create_sidecar_or_log(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  pid = photo_id(conn, "a.jpg")
  curation.set_rating(conn, settings, pid, 0)
  assert not os.path.exists(os.path.join(settings.pictures_dir, "a.jpg.xmp"))
  assert curation.recent_activity(conn) == []


def test_dry_run_changes_nothing(conn, settings):
  pid = setup_pair(conn, settings)
  dry = dataclasses.replace(settings, xmp_dry_run=True)
  before = open(os.path.join(settings.pictures_dir, "y/K1.DNG.xmp"), "rb").read()
  r = curation.set_rating(conn, dry, pid, 5)
  assert r["dry_run"] and r["changes"]["rating"]["new"] == "5"
  assert open(os.path.join(settings.pictures_dir, "y/K1.DNG.xmp"), "rb").read() == before
  assert curation.photo_state(conn, pid)["rating"] == 1
  assert curation.recent_activity(conn) == []
  assert not os.path.exists(os.path.join(settings.state_dir, "xmp_backups"))


def test_invalid_requests(conn, settings):
  pid = setup_pair(conn, settings)
  with pytest.raises(curation.CurationError):
    curation.set_rating(conn, settings, pid, 6)
  with pytest.raises(curation.CurationError):
    curation.set_rating(conn, settings, 9999, 3)
  conn.execute("UPDATE files SET missing = 1 WHERE path = 'y/K1.DNG'")
  with pytest.raises(curation.CurationError):
    curation.set_rating(conn, settings, pid, 3)


def test_http_api(settings):
  conn = db.open_state(settings.state_dir)
  pid = setup_pair(conn, settings)
  client = TestClient(api.create_app(conn, settings))
  r = client.post(f"/api/photos/{pid}/rating", json={"rating": 4})
  assert r.status_code == 200 and r.json()["photo"]["rating"] == 4
  assert client.post(f"/api/photos/{pid}/fav", json={"fav": True}).json()["photo"]["fav"]
  r = client.post(f"/api/photos/{pid}/tags", json={"add": ["a"], "remove": []})
  assert r.json()["photo"]["tags"] == ["a"]
  assert client.post(f"/api/photos/{pid}/rating", json={"rating": 9}).status_code == 400
  assert client.post("/api/photos/9999/rating", json={"rating": 1}).status_code == 404
  log = client.get("/api/activity").json()
  assert [l["field"] for l in log] == ["tags", "fav", "rating"]
  u = client.post(f"/api/activity/{log[0]['id']}/undo").json()
  assert u["photo"]["tags"] == []
  assert client.post(f"/api/activity/{log[0]['id']}/undo").status_code == 400
