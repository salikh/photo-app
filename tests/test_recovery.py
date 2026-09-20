import os

from fastapi.testclient import TestClient

from photoapp import api
from photoapp import curation
from photoapp import db
from photoapp import recovery
from photoapp import scan
from photoapp import xmp
from tests.conftest import make_jpeg
from tests.test_scan_sidecars import XMP, write


def pid(conn, path):
  return conn.execute("SELECT photo_id FROM files WHERE path = ?", (path,)).fetchone()[0]


def state(conn, path):
  return dict(conn.execute("SELECT * FROM photos WHERE id = ?", (pid(conn, path),)).fetchone())


def move(settings, old, new):
  os.makedirs(os.path.dirname(os.path.join(settings.pictures_dir, new)), exist_ok=True)
  os.rename(os.path.join(settings.pictures_dir, old), os.path.join(settings.pictures_dir, new))
  os.utime(settings.pictures_dir, None)
  for root, dirs, _ in os.walk(settings.pictures_dir):
    for d in dirs:
      os.utime(os.path.join(root, d), None)


def rescan(conn, settings):
  scan.scan(conn, settings.pictures_dir,
            on_done=lambda c: recovery.recover(c, settings))


def test_rated_photo_moved_without_its_sidecar_gets_its_rating_back(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "old", "a.jpg"))
  scan.scan(conn, d)
  curation.set_rating(conn, settings, pid(conn, "old/a.jpg"), 4)
  curation.set_fav(conn, settings, pid(conn, "old/a.jpg"), True)
  # the picture is moved WITHOUT its sidecar (a plain copy or download)
  move(settings, "old/a.jpg", "new/a-renamed.jpg")
  os.remove(os.path.join(d, "old", "a.jpg.xmp"))
  rescan(conn, settings)
  s = state(conn, "new/a-renamed.jpg")
  assert (s["rating"], s["fav"]) == (4, 1)
  sidecar = os.path.join(d, "new", "a-renamed.jpg.xmp")
  parsed = xmp.parse(open(sidecar, "rb").read())
  assert parsed.rating == 4 and parsed.fav
  log = [(l["field"], l["cause"]) for l in curation.recent_activity(conn)
         if l["cause"] == "hash-recovery"]
  assert ("rating", "hash-recovery") in log and ("fav", "hash-recovery") in log
  rescan(conn, settings)                         # nothing more to do
  assert len([l for l in curation.recent_activity(conn) if l["cause"] == "hash-recovery"]) == len(log)
  assert conn.execute("SELECT last_path FROM rating_by_hash WHERE rating = 4").fetchone()[0] == "new/a-renamed.jpg"


def test_moved_with_sidecar_needs_no_recovery(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "old", "a.jpg"))
  scan.scan(conn, d)
  curation.set_rating(conn, settings, pid(conn, "old/a.jpg"), 3)
  move(settings, "old/a.jpg", "new/a.jpg")
  move(settings, "old/a.jpg.xmp", "new/a.jpg.xmp")
  rescan(conn, settings)
  assert state(conn, "new/a.jpg")["rating_source"] == "xmp"
  assert [l for l in curation.recent_activity(conn) if l["cause"] == "hash-recovery"] == []


def test_copy_while_original_still_exists_is_not_recovered(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  curation.set_rating(conn, settings, pid(conn, "a.jpg"), 5)
  import shutil
  shutil.copyfile(os.path.join(d, "a.jpg"), os.path.join(d, "copy.jpg"))
  os.utime(d, None)
  rescan(conn, settings)
  assert state(conn, "copy.jpg")["rating"] == 0        # original is still live


def test_two_unrated_photos_with_same_content_are_ambiguous(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  curation.set_rating(conn, settings, pid(conn, "a.jpg"), 2)
  os.remove(os.path.join(d, "a.jpg.xmp"))
  import shutil
  shutil.copyfile(os.path.join(d, "a.jpg"), os.path.join(d, "x1.jpg"))
  shutil.copyfile(os.path.join(d, "a.jpg"), os.path.join(d, "x2.jpg"))
  os.remove(os.path.join(d, "a.jpg"))
  os.utime(d, None)
  rescan(conn, settings)
  assert state(conn, "x1.jpg")["rating"] == 0 and state(conn, "x2.jpg")["rating"] == 0
  amb = recovery.ambiguous(conn)
  assert {a["photo_id"] for a in amb} == {pid(conn, "x1.jpg"), pid(conn, "x2.jpg")}


def test_dry_run_recovers_nothing(conn, settings):
  import dataclasses
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  curation.set_rating(conn, settings, pid(conn, "a.jpg"), 4)
  move(settings, "a.jpg", "b.jpg")
  os.remove(os.path.join(d, "a.jpg.xmp"))
  dry = dataclasses.replace(settings, xmp_dry_run=True)
  scan.scan(conn, d, on_done=lambda c: recovery.recover(c, dry))
  assert state(conn, "b.jpg")["rating"] == 0
  assert not os.path.exists(os.path.join(d, "b.jpg.xmp"))


def test_attention_endpoint(settings):
  d = settings.pictures_dir
  conn = db.open_state(settings.state_dir)
  make_jpeg(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "b.jpg"))
  write(os.path.join(d, "a.jpg.xmp"), XMP % (1, ""), mtime=1_000_000)
  write(os.path.join(d, "a.xmp"), XMP % (4, ""), mtime=2_000_000)     # also a's: conflict
  write(os.path.join(d, "gone.jpg.xmp"), XMP % (2, ""))               # orphan
  scan.scan(conn, d)
  c = TestClient(api.create_app(conn, settings))
  r = c.get("/api/attention").json()
  assert [x["path"] for x in r["conflicts"]] == ["a.jpg"]
  assert r["orphan_sidecars"] == ["gone.jpg.xmp"] and r["ambiguous_recovery"] == []
