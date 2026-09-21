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


def read(settings, rel):
  return open(os.path.join(settings.pictures_dir, rel), "rb").read()


def test_rating_is_written_to_both_dng_and_jpg_sidecars_with_backups(conn, settings):
  pid = setup_pair(conn, settings, rating_dng=1, rating_jpg=3)   # they disagree
  assert curation.photo_state(conn, pid)["conflict"]
  dng_before, jpg_before = read(settings, "y/K1.DNG.xmp"), read(settings, "y/K1.JPG.xmp")
  r = curation.set_rating(conn, settings, pid, 4)
  assert r["sidecar"] == "y/K1.DNG.xmp"
  assert r["sidecars"] == ["y/K1.DNG.xmp", "y/K1.JPG.xmp"]
  assert xmp.parse(read(settings, "y/K1.DNG.xmp")).rating == 4
  assert xmp.parse(read(settings, "y/K1.JPG.xmp")).rating == 4
  state = curation.photo_state(conn, pid)
  assert state["rating"] == 4 and not state["conflict"]          # in sync again
  backups = {os.path.basename(f): open(os.path.join(dp, f), "rb").read()
             for dp, _, fs in os.walk(settings.state_dir) for f in fs if "xmp_backups" in dp}
  assert len(backups) == 2
  assert sorted(v for v in backups.values()) == sorted([dng_before, jpg_before])
  assert conn.execute("SELECT rating_source FROM photos WHERE id = ?", (pid,)).fetchone()[0] == "app"   # set here, sidecars tie
  log = curation.recent_activity(conn)
  assert [(l["field"], l["old"], l["new"], l["cause"]) for l in log] == [("rating", "1", "4", "user")]   # tie on mtime: the DNG sidecar (1) was shown
  curation.set_rating(conn, settings, pid, 5)                     # no new backups
  assert len([f for dp, _, fs in os.walk(settings.state_dir) for f in fs if "xmp_backups" in dp]) == 2


def test_missing_jpg_sidecar_is_created_and_bare_shared_sidecar_written_once(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  make_jpeg(os.path.join(d, "a.JPG"))
  write(os.path.join(d, "a.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)     # JPG has none
  touch(os.path.join(d, "b.DNG"))
  make_jpeg(os.path.join(d, "b.JPG"))
  write(os.path.join(d, "b.xmp"), XMP % (1, ""), mtime=1_000_000)         # RAW-style, shared
  scan.scan(conn, d)
  r = curation.set_rating(conn, settings, photo_id(conn, "a.DNG"), 3)
  assert r["sidecars"] == ["a.DNG.xmp", "a.JPG.xmp"]
  assert xmp.parse(read(settings, "a.JPG.xmp")).rating == 3
  r = curation.set_rating(conn, settings, photo_id(conn, "b.DNG"), 2)
  assert r["sidecars"] == ["b.xmp"]                                       # one file, written once
  assert not os.path.exists(os.path.join(d, "b.JPG.xmp"))
  assert len([f for dp, _, fs in os.walk(settings.state_dir) for f in fs if "xmp_backups" in dp]) == 2   # a new sidecar needs no backup


def test_fav_edit_also_syncs_a_disagreeing_rating_and_keeps_unknown_tags(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  make_jpeg(os.path.join(d, "a.JPG"))
  write(os.path.join(d, "a.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)
  write(os.path.join(d, "a.JPG.xmp"), XMP % (3, "<dc:subject><rdf:Bag><rdf:li>known</rdf:li></rdf:Bag></dc:subject>"), mtime=2_000_000)
  scan.scan(conn, d)
  pid = photo_id(conn, "a.DNG")
  # a tag added in darktable after the last scan: the app does not know it yet
  edited = xmp.edit_bytes(read(settings, "a.DNG.xmp"), add_tags=["darktable-only"])
  with open(os.path.join(d, "a.DNG.xmp"), "wb") as f:
    f.write(edited)
  curation.set_fav(conn, settings, pid, True)
  for rel in ("a.DNG.xmp", "a.JPG.xmp"):
    s = xmp.parse(read(settings, rel))
    assert s.rating == 3 and s.fav and "known" in s.tags, rel
  assert "darktable-only" in xmp.parse(read(settings, "a.DNG.xmp")).tags   # never wiped


def test_exports_and_missing_camera_are_not_written(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  make_jpeg(os.path.join(d, "a.JPG"))
  make_jpeg(os.path.join(d, "a-edit.jpg"))
  scan.scan(conn, d)
  from photoapp import manual_links
  manual_links.link(conn, settings.state_dir, "a-edit.jpg", "a.DNG", role="export")
  pid = photo_id(conn, "a.DNG")
  conn.execute("UPDATE files SET missing = 1 WHERE path = 'a.JPG'")
  r = curation.set_rating(conn, settings, pid, 2)
  assert r["sidecars"] == ["a.DNG.xmp"]
  assert not os.path.exists(os.path.join(d, "a-edit.jpg.xmp"))
  assert not os.path.exists(os.path.join(d, "a.JPG.xmp"))


def test_partial_failure_reports_and_keeps_cache_truthful(conn, settings, monkeypatch):
  pid = setup_pair(conn, settings, 1, 1)
  real = xmp.update_file

  def flaky(path, **kw):
    if path.endswith("K1.JPG.xmp"):
      raise OSError("disk full")
    return real(path, **kw)

  monkeypatch.setattr(xmp, "update_file", flaky)
  with pytest.raises(curation.CurationError) as e:
    curation.set_rating(conn, settings, pid, 5)
  assert "K1.JPG.xmp" in str(e.value) and "already written: y/K1.DNG.xmp" in str(e.value)
  assert xmp.parse(read(settings, "y/K1.DNG.xmp")).rating == 5
  assert xmp.parse(read(settings, "y/K1.JPG.xmp")).rating == 1
  assert curation.photo_state(conn, pid)["conflict"]        # the cache says so too


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
  assert r["sidecars"] == ["y/K1.DNG.xmp", "y/K1.JPG.xmp"]
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


def test_result_lists_activity_ids(conn, settings):
  pid = setup_pair(conn, settings)
  r = curation.set_rating(conn, settings, pid, 3)
  assert r["activity_ids"] == [curation.recent_activity(conn)[0]["id"]]
  assert curation.set_rating(conn, settings, pid, 3)["activity_ids"] == []   # no change
