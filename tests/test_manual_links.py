import json
import os

import pytest

from photoapp import db
from photoapp import grouping
from photoapp import manual_links
from photoapp import scan
from tests.conftest import make_jpeg
from tests.test_grouping import by_path, photos, touch


def make_pair_and_export(d):
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "Exported", "a-edit.jpg"), color="blue")


def bump(d):
  os.utime(d, None)
  for root, dirs, _ in os.walk(d):
    for x in dirs:
      os.utime(os.path.join(root, x), None)


def test_unlink_detaches_camera_jpg_and_survives_rescan(conn, settings):
  d = settings.pictures_dir
  make_pair_and_export(d)
  scan.scan(conn, d)
  assert by_path(conn)["a.jpg"]["photo_id"] == by_path(conn)["a.dng"]["photo_id"]
  manual_links.unlink(conn, settings.state_dir, "a.jpg")
  f = by_path(conn)
  assert f["a.jpg"]["photo_id"] != f["a.dng"]["photo_id"]
  assert f["a.jpg"]["role"] == "original" and f["a.jpg"]["link_source"] == "manual"
  # the DNG's Photo must not keep displaying the detached JPG
  assert photos(conn)[f["a.dng"]["photo_id"]]["representative_file_id"] == f["a.dng"]["id"]
  # a rescan and a full regroup must not undo it
  touch(os.path.join(d, "b.dng"))
  bump(d)
  scan.scan(conn, d)
  grouping.regroup(conn)
  manual_links.apply_all(conn)
  f = by_path(conn)
  assert f["a.jpg"]["photo_id"] != f["a.dng"]["photo_id"]
  assert len(photos(conn)) == 4   # a.dng, a.jpg, a-edit.jpg, b.dng


def test_link_attaches_export_as_tuning_of_photo(conn, settings):
  d = settings.pictures_dir
  make_pair_and_export(d)
  scan.scan(conn, d)
  before = len(photos(conn))
  manual_links.link(conn, settings.state_dir, "Exported/a-edit.jpg", "a.dng",
                    role="export")
  f = by_path(conn)
  e, dng = f["Exported/a-edit.jpg"], f["a.dng"]
  assert e["photo_id"] == dng["photo_id"] and e["role"] == "export"
  assert e["derived_from"] == dng["id"] and e["link_source"] == "manual"
  assert len(photos(conn)) == before - 1      # its own Photo went away
  bump(d)
  scan.scan(conn, d)
  assert by_path(conn)["Exported/a-edit.jpg"]["photo_id"] == dng["photo_id"]


def test_link_then_unlink_latest_decision_wins(conn, settings):
  d = settings.pictures_dir
  make_pair_and_export(d)
  scan.scan(conn, d)
  manual_links.link(conn, settings.state_dir, "Exported/a-edit.jpg", "a.dng")
  manual_links.unlink(conn, settings.state_dir, "Exported/a-edit.jpg")
  f = by_path(conn)
  assert f["Exported/a-edit.jpg"]["photo_id"] != f["a.dng"]["photo_id"]


def test_invalid_links_are_rejected(conn, settings):
  d = settings.pictures_dir
  make_pair_and_export(d)
  scan.scan(conn, d)
  with pytest.raises(ValueError):
    manual_links.link(conn, settings.state_dir, "a.jpg", "a.dng")     # same photo
  with pytest.raises(ValueError):
    manual_links.link(conn, settings.state_dir, "a.dng", "Exported/a-edit.jpg")  # original with members
  with pytest.raises(ValueError):
    manual_links.link(conn, settings.state_dir, "nope.jpg", "a.dng")
  with pytest.raises(ValueError):
    manual_links.link(conn, settings.state_dir, "a.jpg", "a.dng", role="x")
  assert conn.execute("SELECT COUNT(*) FROM manual_links").fetchone()[0] == 0


def test_decisions_are_mirrored_to_jsonl_and_restored_into_fresh_db(settings):
  d = settings.pictures_dir
  make_pair_and_export(d)
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  manual_links.unlink(conn, settings.state_dir, "a.jpg")
  manual_links.link(conn, settings.state_dir, "Exported/a-edit.jpg", "a.dng",
                    role="export")
  conn.close()
  with open(os.path.join(settings.state_dir, "manual_links.jsonl")) as f:
    lines = [json.loads(l) for l in f]
  assert [l["action"] for l in lines] == ["unlink", "link"]

  os.remove(settings.db_path)                       # the DB is only a cache
  for suffix in ("-wal", "-shm"):
    if os.path.exists(settings.db_path + suffix):
      os.remove(settings.db_path + suffix)
  fresh = db.open_state(settings.state_dir)
  assert manual_links.restore_if_empty(fresh, settings.state_dir) == 2
  assert manual_links.restore_if_empty(fresh, settings.state_dir) == 0  # only when empty
  scan.scan(fresh, d)
  f = by_path(fresh)
  assert f["a.jpg"]["photo_id"] != f["a.dng"]["photo_id"]
  assert f["Exported/a-edit.jpg"]["photo_id"] == f["a.dng"]["photo_id"]
  assert f["Exported/a-edit.jpg"]["role"] == "export"


def test_file_that_moved_is_found_by_hash(conn, settings):
  d = settings.pictures_dir
  make_pair_and_export(d)
  scan.scan(conn, d)
  manual_links.unlink(conn, settings.state_dir, "a.jpg")
  # a.jpg moves next to another DNG where the automatic rule would pair it
  touch(os.path.join(d, "sub", "a.dng"))
  os.rename(os.path.join(d, "a.jpg"), os.path.join(d, "sub", "a.jpg"))
  bump(d)
  scan.scan(conn, d)
  f = by_path(conn)
  assert "a.jpg" not in f                                     # ticket 128: repointed, not left missing
  # the unlink decision follows the file (by its unchanged row / hash) and beats the pairing rule
  assert f["sub/a.jpg"]["link_source"] == "manual"
  assert f["sub/a.jpg"]["photo_id"] != f["sub/a.dng"]["photo_id"]
