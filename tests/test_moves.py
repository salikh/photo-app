import os
import shutil

from photoapp import raw_preview_dng
from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg


def row(conn, path):
  return conn.execute(
      "SELECT id, photo_id, missing FROM files WHERE path = ?", (path,)).fetchone()


def all_paths(conn):
  return {r["path"]: r["missing"] for r in conn.execute("SELECT path, missing FROM files")}


def test_rename_within_a_directory_keeps_the_files_identity(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  scan.scan(conn, d)
  before = row(conn, "2020/a.jpg")
  os.rename(os.path.join(d, "2020", "a.jpg"), os.path.join(d, "2020", "renamed.jpg"))

  scan.scan(conn, d)

  after = row(conn, "2020/renamed.jpg")
  assert after is not None and after["missing"] == 0
  assert after["id"] == before["id"] and after["photo_id"] == before["photo_id"]
  assert row(conn, "2020/a.jpg") is None          # repointed, not left behind as missing


def test_move_across_directories_is_detected_by_a_scoped_rescan(conn, settings):
  # The old row lives outside the rescanned directory, so this only works because the hash
  # lookup is against the whole database (the reported requirement).
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  scan.scan(conn, d)
  before = row(conn, "2020/a.jpg")
  os.makedirs(os.path.join(d, "2021"))
  os.rename(os.path.join(d, "2020", "a.jpg"), os.path.join(d, "2021", "a.jpg"))

  scan.scan(conn, d, os.path.join(d, "2021"))     # only the new directory

  after = row(conn, "2021/a.jpg")
  assert after is not None and after["missing"] == 0 and after["id"] == before["id"]


def test_a_copy_is_not_treated_as_a_move(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  scan.scan(conn, d)
  shutil.copyfile(os.path.join(d, "2020", "a.jpg"), os.path.join(d, "2020", "copy.jpg"))

  scan.scan(conn, d)

  assert row(conn, "2020/a.jpg")["missing"] == 0
  assert row(conn, "2020/copy.jpg")["missing"] == 0
  assert row(conn, "2020/a.jpg")["id"] != row(conn, "2020/copy.jpg")["id"]


def test_two_gone_rows_with_the_same_content_are_not_guessed(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"), color="green")
  os.makedirs(os.path.join(d, "2021"))
  shutil.copyfile(os.path.join(d, "2020", "a.jpg"), os.path.join(d, "2021", "a.jpg"))
  scan.scan(conn, d)
  os.remove(os.path.join(d, "2020", "a.jpg"))
  os.remove(os.path.join(d, "2021", "a.jpg"))
  make_jpeg(os.path.join(d, "2022", "a.jpg"), color="green")   # identical content again

  scan.scan(conn, d)

  paths = all_paths(conn)
  assert paths["2020/a.jpg"] == 1 and paths["2021/a.jpg"] == 1   # both left missing
  assert paths["2022/a.jpg"] == 0                                # a fresh row, nothing guessed


def test_thumbnails_and_preview_dng_follow_the_move(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  scan.scan(conn, d)
  fid = row(conn, "2020/a.jpg")["id"]
  thumbs.ensure(conn, d, settings.thumbs_dir, fid, "2020/a.jpg", "Thumb")
  thumbs.ensure(conn, d, settings.thumbs_dir, fid, "2020/a.jpg", "Small")
  preview = raw_preview_dng.path_for(settings.thumbs_dir, "2020/a.jpg")
  os.makedirs(os.path.dirname(preview), exist_ok=True)
  with open(preview, "wb") as f:
    f.write(b"fake preview dng")
  os.rename(os.path.join(d, "2020", "a.jpg"), os.path.join(d, "2020", "renamed.jpg"))

  scan.scan(conn, d, thumbs_dir=settings.thumbs_dir)

  assert row(conn, "2020/renamed.jpg")["id"] == fid
  assert thumbs.lookup(settings.thumbs_dir, "Thumb", "2020/renamed.jpg") is not None
  assert thumbs.lookup(settings.thumbs_dir, "Small", "2020/renamed.jpg") is not None
  assert thumbs.lookup(settings.thumbs_dir, "Thumb", "2020/a.jpg") is None
  stored = {r["path"] for r in conn.execute(
      "SELECT path FROM thumbs WHERE file_id = ?", (fid,))}
  assert stored == {thumbs.thumb_path(settings.thumbs_dir, size, "2020/renamed.jpg")
                    for size in ("Thumb", "Small")}
  assert os.path.isfile(raw_preview_dng.path_for(settings.thumbs_dir, "2020/renamed.jpg"))
  assert not os.path.exists(preview)
