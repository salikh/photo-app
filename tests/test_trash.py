import os
import time

from photoapp import curation
from photoapp import scan
from photoapp import trash
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan_sidecars import XMP, write


def photo_id(conn, path):
  return conn.execute("SELECT photo_id FROM files WHERE path = ?", (path,)).fetchone()[0]


def setup_rejected_pair(conn, settings, rel_dir="2024/trip"):
  """A rejected DNG+JPG Photo, each with a sidecar, under rel_dir."""
  d = settings.pictures_dir
  touch(os.path.join(d, rel_dir, "K1.DNG"))
  make_jpeg(os.path.join(d, rel_dir, "K1.JPG"))
  write(os.path.join(d, rel_dir, "K1.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)
  write(os.path.join(d, rel_dir, "K1.JPG.xmp"), XMP % (1, ""), mtime=1_000_000)
  scan.scan(conn, d)
  pid = photo_id(conn, f"{rel_dir}/K1.DNG")
  curation.set_rating(conn, settings, pid, -1)
  return pid


def test_move_to_trash_mirrors_subdirectory_structure(settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2024", "trip", "a.DNG"))
  dest = trash.move_to_trash(d, "2024/trip/a.DNG")
  assert dest == ".trash/2024/trip/a.DNG"
  assert os.path.isfile(os.path.join(d, ".trash", "2024", "trip", "a.DNG"))
  assert not os.path.exists(os.path.join(d, "2024", "trip", "a.DNG"))


def test_move_to_trash_returns_none_if_source_already_gone(settings):
  assert trash.move_to_trash(settings.pictures_dir, "nope.jpg") is None


def test_move_to_trash_does_not_clobber_an_earlier_trashed_file(settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  first = trash.move_to_trash(d, "a.DNG")
  touch(os.path.join(d, "a.DNG"))   # a second file that happens to land at the same path
  second = trash.move_to_trash(d, "a.DNG")
  assert first != second
  assert os.path.isfile(os.path.join(d, first))
  assert os.path.isfile(os.path.join(d, second))


def test_move_to_trash_rejects_a_path_that_escapes_pictures_dir(settings):
  try:
    trash.move_to_trash(settings.pictures_dir, "../../etc/passwd")
    assert False, "should have raised"
  except trash.TrashError:
    pass


def test_trash_photo_moves_every_file_and_sidecar_and_marks_them_missing(conn, settings):
  pid = setup_rejected_pair(conn, settings)
  d = settings.pictures_dir
  result = trash.trash_photo(conn, settings, pid)
  assert result["errors"] == []
  moved_from = sorted(m["from"] for m in result["moved"])
  assert moved_from == ["2024/trip/K1.DNG", "2024/trip/K1.DNG.xmp",
                        "2024/trip/K1.JPG", "2024/trip/K1.JPG.xmp"]
  for rel in moved_from:
    assert not os.path.exists(os.path.join(d, rel))
    assert os.path.isfile(os.path.join(d, ".trash", rel))
  rows = conn.execute("SELECT path, missing FROM files WHERE photo_id = ?", (pid,)).fetchall()
  assert len(rows) == 2 and all(r["missing"] for r in rows)


def test_trash_photo_moves_the_per_file_metadata_json_too(conn, settings):
  # ticket 111's <name>.json cache lives next to the image, so trashing the image takes it along.
  pid = setup_rejected_pair(conn, settings)
  d = settings.pictures_dir
  for name in ("K1.DNG", "K1.JPG"):
    with open(os.path.join(d, "2024/trip", name + ".json"), "w") as f:
      f.write("{}")
  result = trash.trash_photo(conn, settings, pid)
  moved_from = sorted(m["from"] for m in result["moved"])
  assert "2024/trip/K1.DNG.json" in moved_from and "2024/trip/K1.JPG.json" in moved_from
  for rel in ("2024/trip/K1.DNG.json", "2024/trip/K1.JPG.json"):
    assert not os.path.exists(os.path.join(d, rel))
    assert os.path.isfile(os.path.join(d, ".trash", rel))


def test_trash_photo_refuses_a_photo_that_is_not_rejected(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  pid = photo_id(conn, "a.jpg")
  try:
    trash.trash_photo(conn, settings, pid)
    assert False, "should have raised"
  except trash.TrashError as e:
    assert "not rejected" in str(e)
  assert os.path.isfile(os.path.join(d, "a.jpg"))   # untouched


# ---- trash_file (ticket 082: single-file, not rating-gated) ----

def test_trash_file_moves_one_file_and_its_sidecar_leaves_siblings_alone(conn, settings):
  pid = setup_rejected_pair(conn, settings)   # rejected, but trash_file should not care either way
  d = settings.pictures_dir
  dng_id = conn.execute("SELECT id FROM files WHERE path = '2024/trip/K1.DNG'").fetchone()[0]

  result = trash.trash_file(conn, settings, dng_id)
  assert result["errors"] == []
  moved_from = sorted(m["from"] for m in result["moved"])
  assert moved_from == ["2024/trip/K1.DNG", "2024/trip/K1.DNG.xmp"]
  assert not os.path.exists(os.path.join(d, "2024/trip/K1.DNG"))
  assert os.path.isfile(os.path.join(d, ".trash/2024/trip/K1.DNG"))
  # the JPG (and its sidecar) are untouched -- this is file-scoped, not photo-scoped
  assert os.path.isfile(os.path.join(d, "2024/trip/K1.JPG"))
  assert os.path.isfile(os.path.join(d, "2024/trip/K1.JPG.xmp"))

  rows = {r["path"]: r["missing"] for r in conn.execute(
      "SELECT path, missing FROM files WHERE photo_id = ?", (pid,))}
  assert rows["2024/trip/K1.DNG"] == 1 and rows["2024/trip/K1.JPG"] == 0


def test_trash_file_moves_only_its_own_metadata_json(conn, settings):
  pid = setup_rejected_pair(conn, settings)
  d = settings.pictures_dir
  for name in ("K1.DNG", "K1.JPG"):
    with open(os.path.join(d, "2024/trip", name + ".json"), "w") as f:
      f.write("{}")
  dng_id = conn.execute("SELECT id FROM files WHERE path = '2024/trip/K1.DNG'").fetchone()[0]
  moved_from = sorted(m["from"] for m in trash.trash_file(conn, settings, dng_id)["moved"])
  assert "2024/trip/K1.DNG.json" in moved_from and "2024/trip/K1.JPG.json" not in moved_from
  assert os.path.isfile(os.path.join(d, ".trash/2024/trip/K1.DNG.json"))
  assert os.path.isfile(os.path.join(d, "2024/trip/K1.JPG.json"))   # sibling's stays


def test_trash_file_does_not_require_a_rejected_photo(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files WHERE path = 'a.jpg'").fetchone()[0]
  result = trash.trash_file(conn, settings, fid)   # not rejected; should still work
  assert result["errors"] == []
  assert not os.path.exists(os.path.join(d, "a.jpg"))


def test_trash_file_refuses_an_unknown_or_already_missing_file(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files WHERE path = 'a.jpg'").fetchone()[0]
  trash.trash_file(conn, settings, fid)   # first time: fine
  try:
    trash.trash_file(conn, settings, fid)   # second time: already missing
    assert False, "should have raised"
  except trash.TrashError as e:
    assert "no such live file" in str(e)
  try:
    trash.trash_file(conn, settings, 999999)
    assert False, "should have raised"
  except trash.TrashError as e:
    assert "no such live file" in str(e)


def test_trash_photos_batch_reports_failures_without_stopping(conn, settings):
  d = settings.pictures_dir
  rejected = setup_rejected_pair(conn, settings, "2024/trip")
  make_jpeg(os.path.join(d, "b.jpg"))
  scan.scan(conn, d)
  not_rejected = photo_id(conn, "b.jpg")

  result = trash.trash_photos(conn, settings, [rejected, not_rejected, 999999])
  assert len(result["trashed"]) == 1 and result["trashed"][0]["photo_id"] == rejected
  errored = {e["photo_id"] for e in result["errors"]}
  assert errored == {not_rejected, 999999}
  assert os.path.isfile(os.path.join(d, "b.jpg"))   # untouched


def test_top_level_steps_excludes_trash_directory(settings):
  d = settings.pictures_dir
  os.makedirs(os.path.join(d, "2024"))
  os.makedirs(os.path.join(d, trash.TRASH_DIRNAME, "2024"))
  names = [name for name, _recursive in scan.top_level_steps(d)]
  assert "2024" in names
  assert trash.TRASH_DIRNAME not in names


def test_a_trashed_photos_sidecar_is_not_rediscovered_by_a_scan(conn, settings):
  # ticket 072: .trash/ is never scanned, so a trashed (still 'rejected') sidecar does not come
  # back as a brand new, live Photo the next time the library is rescanned.
  pid = setup_rejected_pair(conn, settings)
  before_photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
  trash.trash_photo(conn, settings, pid)
  scan.scan(conn, settings.pictures_dir)   # whole-library rescan, as the nightly scan would do
  after_photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
  assert after_photos == before_photos   # no new Photo created from the trashed files
  rows = conn.execute("SELECT missing FROM files WHERE photo_id = ?", (pid,)).fetchall()
  assert [r["missing"] for r in rows] == [1, 1]   # still missing


# ---- purge_trash (ticket 081) ----

def test_purge_trash_deletes_only_files_past_retention(settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  touch(os.path.join(d, "b.DNG"))
  trash.move_to_trash(d, "a.DNG")
  trash.move_to_trash(d, "b.DNG")
  far_future = time.time() + 30 * 86400   # well past the default 7-day retention

  deleted, dirs = trash.purge_trash(d, retention_days=7, now=far_future)
  assert deleted == 2
  assert not os.path.exists(os.path.join(d, trash.TRASH_DIRNAME, "a.DNG"))
  assert not os.path.exists(os.path.join(d, trash.TRASH_DIRNAME, "b.DNG"))


def test_purge_trash_keeps_files_within_retention(settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  trash.move_to_trash(d, "a.DNG")

  deleted, dirs = trash.purge_trash(d, retention_days=7)   # now defaults to real time.time()
  assert deleted == 0 and dirs == 0
  assert os.path.isfile(os.path.join(d, trash.TRASH_DIRNAME, "a.DNG"))


def test_purge_trash_removes_now_empty_subdirectories_but_not_trash_itself(settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2019", "trip", "a.DNG"))
  trash.move_to_trash(d, "2019/trip/a.DNG")
  far_future = time.time() + 30 * 86400

  deleted, dirs_removed = trash.purge_trash(d, retention_days=7, now=far_future)
  assert deleted == 1 and dirs_removed == 2   # 2019/trip and 2019, both now empty
  assert not os.path.exists(os.path.join(d, trash.TRASH_DIRNAME, "2019"))
  assert os.path.isdir(os.path.join(d, trash.TRASH_DIRNAME))   # .trash/ itself untouched


def test_purge_trash_on_a_library_with_no_trash_yet(settings):
  assert trash.purge_trash(settings.pictures_dir) == (0, 0)
