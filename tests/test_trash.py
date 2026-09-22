import os

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
