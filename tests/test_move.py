import os

from photoapp import move
from photoapp import scan
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan_sidecars import XMP, write


def photo_id(conn, path):
  return conn.execute("SELECT photo_id FROM files WHERE path = ?", (path,)).fetchone()[0]


def setup_pair(conn, settings, rel_dir="2024/trip"):
  """A DNG+JPG Photo, each with a sidecar, under rel_dir."""
  d = settings.pictures_dir
  touch(os.path.join(d, rel_dir, "K1.DNG"))
  make_jpeg(os.path.join(d, rel_dir, "K1.JPG"))
  write(os.path.join(d, rel_dir, "K1.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)
  write(os.path.join(d, rel_dir, "K1.JPG.xmp"), XMP % (1, ""), mtime=1_000_000)
  scan.scan(conn, d)
  return photo_id(conn, f"{rel_dir}/K1.DNG")


def test_move_photo_moves_every_file_and_sidecar(conn, settings):
  pid = setup_pair(conn, settings)
  d = settings.pictures_dir
  result = move.move_photo(conn, settings, pid, "2025/keepers")
  moved_from = sorted(m["from"] for m in result["moved"])
  assert moved_from == ["2024/trip/K1.DNG", "2024/trip/K1.DNG.xmp",
                        "2024/trip/K1.JPG", "2024/trip/K1.JPG.xmp"]
  assert result["source_dirs"] == {"2024/trip"}
  for rel in moved_from:
    assert not os.path.exists(os.path.join(d, rel))
  assert os.path.isfile(os.path.join(d, "2025/keepers/K1.DNG"))
  assert os.path.isfile(os.path.join(d, "2025/keepers/K1.DNG.xmp"))
  rows = {r["path"]: r for r in conn.execute(
      "SELECT path, missing, photo_id FROM files WHERE photo_id = ?", (pid,))}
  assert set(rows) == {"2025/keepers/K1.DNG", "2025/keepers/K1.JPG"}
  assert all(not r["missing"] for r in rows.values())   # still live, just relocated


def test_move_photo_moves_the_per_file_metadata_json_too(conn, settings):
  pid = setup_pair(conn, settings)
  d = settings.pictures_dir
  for name in ("K1.DNG", "K1.JPG"):
    with open(os.path.join(d, "2024/trip", name + ".json"), "w") as f:
      f.write("{}")
  result = move.move_photo(conn, settings, pid, "2025/keepers")
  moved_from = sorted(m["from"] for m in result["moved"])
  assert "2024/trip/K1.DNG.json" in moved_from and "2024/trip/K1.JPG.json" in moved_from
  assert os.path.isfile(os.path.join(d, "2025/keepers/K1.DNG.json"))
  assert os.path.isfile(os.path.join(d, "2025/keepers/K1.JPG.json"))


def test_move_photo_keeps_file_identity_rating_and_tags(conn, settings):
  from photoapp import curation
  pid = setup_pair(conn, settings)
  curation.set_rating(conn, settings, pid, 4)
  curation.edit_tags(conn, settings, pid, add=["favorite"], remove=[])
  file_id = conn.execute("SELECT id FROM files WHERE path = ?", ("2024/trip/K1.DNG",)).fetchone()[0]
  move.move_photo(conn, settings, pid, "2025/keepers")
  row = conn.execute("SELECT id, rating FROM photos WHERE id = ?", (pid,)).fetchone()
  assert row["id"] == pid and row["rating"] == 4   # same Photo, same rating -- not a fresh insert
  new_file_id = conn.execute(
      "SELECT id FROM files WHERE path = ?", ("2025/keepers/K1.DNG",)).fetchone()[0]
  assert new_file_id == file_id   # same files row, just repointed
  tags = {r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE photo_id = ?", (pid,))}
  assert tags == {"favorite"}


def test_move_photo_follows_cached_thumbnails(conn, settings):
  from photoapp import thumbs
  pid = setup_pair(conn, settings)
  file_id = conn.execute("SELECT id FROM files WHERE path = ?", ("2024/trip/K1.JPG",)).fetchone()[0]
  made = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, file_id,
                       "2024/trip/K1.JPG", "Thumb")
  assert made is not None and os.path.isfile(made)
  move.move_photo(conn, settings, pid, "2025/keepers")
  assert not os.path.exists(made)
  moved_thumb = thumbs.lookup(settings.thumbs_dir, "Thumb", "2025/keepers/K1.JPG")
  assert moved_thumb is not None and os.path.isfile(moved_thumb)


def test_move_photo_is_a_noop_for_a_file_already_in_the_destination(conn, settings):
  pid = setup_pair(conn, settings, rel_dir="2025/keepers")
  result = move.move_photo(conn, settings, pid, "2025/keepers")
  assert result["moved"] == [] and result["source_dirs"] == set()


def test_move_photo_disambiguates_a_basename_collision(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2024/a", "K1.DNG"))
  scan.scan(conn, d)
  pid = photo_id(conn, "2024/a/K1.DNG")
  touch(os.path.join(d, "2024/dest", "K1.DNG"))   # an unrelated file already occupies the name
  result = move.move_photo(conn, settings, pid, "2024/dest")
  assert result["moved"][0]["to"] == "2024/dest/K1-2.DNG"
  assert os.path.isfile(os.path.join(d, "2024/dest/K1.DNG"))     # untouched
  assert os.path.isfile(os.path.join(d, "2024/dest/K1-2.DNG"))   # the moved file


def test_move_photo_renames_sidecars_to_match_a_disambiguated_name(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2024/a", "K1.DNG"))
  write(os.path.join(d, "2024/a", "K1.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)
  touch(os.path.join(d, "2024/dest", "K1.DNG"))   # occupies the plain name first
  scan.scan(conn, d)
  pid = photo_id(conn, "2024/a/K1.DNG")
  result = move.move_photo(conn, settings, pid, "2024/dest")
  assert result["moved"][0]["to"] == "2024/dest/K1-2.DNG"
  assert os.path.isfile(os.path.join(d, "2024/dest/K1-2.DNG.xmp"))
  assert not os.path.exists(os.path.join(d, "2024/dest/K1.DNG.xmp"))


def test_move_photos_batch_disambiguates_against_each_other(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2024/a", "K1.DNG"))
  touch(os.path.join(d, "2024/b", "K1.DNG"))
  scan.scan(conn, d)
  pid_a = photo_id(conn, "2024/a/K1.DNG")
  pid_b = photo_id(conn, "2024/b/K1.DNG")
  result = move.move_photos(conn, settings, [pid_a, pid_b], "2024/dest")
  assert result["errors"] == []
  dests = sorted(m["moved"][0]["to"] for m in result["moved"])
  assert dests == ["2024/dest/K1-2.DNG", "2024/dest/K1.DNG"]


def test_move_photos_batch_reports_a_failure_without_stopping(conn, settings):
  pid = setup_pair(conn, settings)
  result = move.move_photos(conn, settings, [999999, pid], "2025/keepers")
  assert result["errors"] == [{"photo_id": 999999, "error": "no such photo: 999999"}]
  assert len(result["moved"]) == 1 and result["moved"][0]["photo_id"] == pid


# --- rename_dir (ticket 155) --------------------------------------------------

def test_rename_dir_moves_everything_and_updates_paths(conn, settings):
  pid = setup_pair(conn, settings, rel_dir="2024/trip")
  d = settings.pictures_dir
  touch(os.path.join(d, "2024/trip/sub", "nested.jpg"))
  scan.scan(conn, d)

  result = move.rename_dir(conn, settings, "2024/trip", "2024/vacation")
  assert result == {"old_dir": "2024/trip", "new_dir": "2024/vacation", "files_moved": 3}

  assert not os.path.isdir(os.path.join(d, "2024/trip"))
  for rel in ("K1.DNG", "K1.JPG", "K1.DNG.xmp", "K1.JPG.xmp", "sub/nested.jpg"):
    assert os.path.isfile(os.path.join(d, "2024/vacation", rel))

  paths_now = {r["path"] for r in conn.execute("SELECT path FROM files")}
  assert paths_now == {"2024/vacation/K1.DNG", "2024/vacation/K1.JPG",
                       "2024/vacation/sub/nested.jpg"}
  # the Photo/rating survived -- same row, just repointed, not a fresh insert
  assert conn.execute("SELECT id FROM photos WHERE id = ?", (pid,)).fetchone()["id"] == pid


def test_rename_dir_follows_cached_thumbnails(conn, settings):
  from photoapp import thumbs
  setup_pair(conn, settings, rel_dir="2024/trip")
  fid = conn.execute("SELECT id FROM files WHERE path = '2024/trip/K1.JPG'").fetchone()[0]
  made = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid,
                       "2024/trip/K1.JPG", "Thumb")
  assert made is not None and os.path.isfile(made)
  move.rename_dir(conn, settings, "2024/trip", "2024/vacation")
  assert not os.path.exists(made)
  moved_thumb = thumbs.lookup(settings.thumbs_dir, "Thumb", "2024/vacation/K1.JPG")
  assert moved_thumb is not None and os.path.isfile(moved_thumb)


def test_rename_dir_refuses_an_existing_target(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2024/a", "x.jpg"))
  touch(os.path.join(d, "2024/b", "y.jpg"))
  scan.scan(conn, d)
  try:
    move.rename_dir(conn, settings, "2024/a", "2024/b")
    assert False, "should have raised"
  except move.MoveError as e:
    assert "already exists" in str(e)
  # untouched
  assert os.path.isfile(os.path.join(d, "2024/a/x.jpg"))
  assert os.path.isfile(os.path.join(d, "2024/b/y.jpg"))


def test_rename_dir_refuses_into_its_own_subtree(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2024", "x.jpg"))
  scan.scan(conn, d)
  try:
    move.rename_dir(conn, settings, "2024", "2024/sub")
    assert False, "should have raised"
  except move.MoveError as e:
    assert "inside the source" in str(e)


def test_rename_dir_refuses_a_missing_source(conn, settings):
  try:
    move.rename_dir(conn, settings, "nope", "elsewhere")
    assert False, "should have raised"
  except move.MoveError as e:
    assert "no such directory" in str(e)


def test_rename_dir_refuses_the_same_source_and_target(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2024", "x.jpg"))
  scan.scan(conn, d)
  try:
    move.rename_dir(conn, settings, "2024", "2024")
    assert False, "should have raised"
  except move.MoveError as e:
    assert "same" in str(e)


def test_rename_dir_refuses_the_library_root(conn, settings):
  try:
    move.rename_dir(conn, settings, ".", "elsewhere")
    assert False, "should have raised"
  except move.MoveError as e:
    assert "root" in str(e)
