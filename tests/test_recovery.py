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


def forget_file(conn, path):
  """Delete a file's row (and the FK links to it) as if the database had been rebuilt, while the
  rating_by_hash memory survives -- the state hash recovery exists for."""
  old = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()["id"]
  conn.execute("UPDATE xmp_sidecars SET file_id = NULL WHERE file_id = ?", (old,))
  conn.execute("UPDATE files SET derived_from = NULL WHERE derived_from = ?", (old,))
  conn.execute("UPDATE files SET exported_from_file_id = NULL WHERE exported_from_file_id = ?", (old,))
  conn.execute("DELETE FROM thumbs WHERE file_id = ?", (old,))
  conn.execute("UPDATE files SET photo_id = NULL WHERE id = ?", (old,))
  conn.execute("DELETE FROM photos WHERE original_file_id = ?", (old,))
  conn.execute("DELETE FROM files WHERE id = ?", (old,))
  conn.commit()


def test_rated_photo_moved_without_its_sidecar_keeps_its_rating(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "old", "a.jpg"))
  scan.scan(conn, d)
  before = conn.execute("SELECT id FROM files WHERE path = 'old/a.jpg'").fetchone()["id"]
  curation.set_rating(conn, settings, pid(conn, "old/a.jpg"), 4)
  curation.set_fav(conn, settings, pid(conn, "old/a.jpg"), True)
  # the picture is moved WITHOUT its sidecar (a plain copy or download)
  move(settings, "old/a.jpg", "new/a-renamed.jpg")
  os.remove(os.path.join(d, "old", "a.jpg.xmp"))
  rescan(conn, settings)
  s = state(conn, "new/a-renamed.jpg")
  assert (s["rating"], s["fav"]) == (4, 1)      # ticket 128: the move kept the file/Photo identity
  assert conn.execute("SELECT id FROM files WHERE path = 'new/a-renamed.jpg'").fetchone()["id"] == before
  # so there is nothing for hash recovery to do
  assert [l for l in curation.recent_activity(conn) if l["cause"] == "hash-recovery"] == []


def test_moved_with_sidecar_needs_no_recovery(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "old", "a.jpg"))
  scan.scan(conn, d)
  curation.set_rating(conn, settings, pid(conn, "old/a.jpg"), 3)
  move(settings, "old/a.jpg", "new/a.jpg")
  move(settings, "old/a.jpg.xmp", "new/a.jpg.xmp")
  rescan(conn, settings)
  assert state(conn, "new/a.jpg")["rating"] == 3
  assert [l for l in curation.recent_activity(conn) if l["cause"] == "hash-recovery"] == []


def test_recovery_still_applies_when_the_file_row_is_gone(conn, settings):
  # Hash recovery remains the fallback when there is no row to repoint (e.g. the database was
  # rebuilt): a new, unrated row whose content matches rating_by_hash gets the rating back.
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  curation.set_rating(conn, settings, pid(conn, "a.jpg"), 4)
  forget_file(conn, "a.jpg")
  os.remove(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "b.jpg"))            # same bytes, brand new path
  scan.scan(conn, d)
  assert state(conn, "b.jpg")["rating"] == 0     # new row, no gone row to repoint
  recovery.recover(conn, settings)
  assert state(conn, "b.jpg")["rating"] == 4


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
  forget_file(conn, "a.jpg")
  os.remove(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "b.jpg"))
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
