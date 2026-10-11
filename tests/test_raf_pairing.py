"""Ticket 220: a Fujifilm JPG+RAF pair behaves exactly like a JPG+DNG pair (epic 217)."""

import os

import pytest

from photoapp import curation
from photoapp import move
from photoapp import scan
from photoapp import trash
from tests.conftest import make_jpeg, write_synthetic_raf
from tests.test_grouping import touch
from tests.test_scan_sidecars import XMP, write


def rows(conn):
  return {r["path"]: r for r in conn.execute("SELECT * FROM files")}


def setup_pair(conn, settings, raw="DSCF1001.RAF", jpg="DSCF1001.JPG", rel_dir="2026/fuji"):
  d = settings.pictures_dir
  write_synthetic_raf(os.path.join(d, rel_dir, raw))
  make_jpeg(os.path.join(d, rel_dir, jpg))
  write(os.path.join(d, rel_dir, raw + ".xmp"), XMP % (3, ""), mtime=1_000_000)
  write(os.path.join(d, rel_dir, jpg + ".xmp"), XMP % (3, ""), mtime=1_000_000)
  scan.scan(conn, d)
  return conn.execute("SELECT photo_id FROM files WHERE path = ?",
                      (f"{rel_dir}/{raw}",)).fetchone()[0]


@pytest.mark.parametrize("raw_ext", ["RAF", "raf", "DNG"])
def test_jpg_plus_raw_is_one_photo_with_the_raw_as_original(conn, settings, raw_ext):
  d = settings.pictures_dir
  touch(os.path.join(d, "t", f"DSCF1001.{raw_ext}"))
  make_jpeg(os.path.join(d, "t", "DSCF1001.JPG"))
  scan.scan(conn, d)
  r = rows(conn)
  raw, jpg = r[f"t/DSCF1001.{raw_ext}"], r["t/DSCF1001.JPG"]
  assert raw["photo_id"] == jpg["photo_id"]
  assert (raw["role"], jpg["role"]) == ("original", "camera")
  assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1


def test_extra_raf_and_lone_raf_stay_single(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "t", "A.RAF"))
  touch(os.path.join(d, "t", "A.DNG"))            # a second RAW of the same stem stays on its own
  make_jpeg(os.path.join(d, "t", "A.JPG"))
  touch(os.path.join(d, "t", "B.RAF"))            # a lone RAF is its own Photo
  scan.scan(conn, d)
  r = rows(conn)
  assert r["t/A.JPG"]["photo_id"] == r["t/A.DNG"]["photo_id"]   # first RAW (sorted) takes the JPG
  assert r["t/A.RAF"]["photo_id"] != r["t/A.JPG"]["photo_id"]
  assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 3


def test_raf_sidecar_name_and_rating(conn, settings):
  pid = setup_pair(conn, settings)
  d = settings.pictures_dir
  assert conn.execute("SELECT rating FROM photos WHERE id = ?", (pid,)).fetchone()[0] == 3
  curation.set_rating(conn, settings, pid, -1)    # reject goes to every file of the Photo
  assert conn.execute("SELECT rating FROM photos WHERE id = ?", (pid,)).fetchone()[0] == -1
  for name in ("DSCF1001.RAF.xmp", "DSCF1001.JPG.xmp"):
    assert "Rating=\"-1\"" in open(os.path.join(d, "2026/fuji", name)).read()


def test_a_new_rating_creates_a_full_name_sidecar_for_a_raf(conn, settings):
  d = settings.pictures_dir
  write_synthetic_raf(os.path.join(d, "t", "X.RAF"))
  scan.scan(conn, d)
  pid = conn.execute("SELECT photo_id FROM files WHERE path = 't/X.RAF'").fetchone()[0]
  curation.set_rating(conn, settings, pid, 4)
  assert os.path.isfile(os.path.join(d, "t", "X.RAF.xmp"))    # like K.DNG.xmp, not X.xmp


def test_move_carries_raf_sidecar_and_files(conn, settings):
  pid = setup_pair(conn, settings)
  result = move.move_photo(conn, settings, pid, "2026/keepers")
  moved_from = sorted(m["from"] for m in result["moved"])
  assert moved_from == ["2026/fuji/DSCF1001.JPG", "2026/fuji/DSCF1001.JPG.xmp",
                        "2026/fuji/DSCF1001.RAF", "2026/fuji/DSCF1001.RAF.xmp"]
  assert os.path.isfile(os.path.join(settings.pictures_dir, "2026/keepers/DSCF1001.RAF.xmp"))
  assert set(rows(conn)) == {"2026/keepers/DSCF1001.RAF", "2026/keepers/DSCF1001.JPG"}


def test_trash_carries_raf_and_sidecar(conn, settings):
  pid = setup_pair(conn, settings)
  curation.set_rating(conn, settings, pid, -1)
  d = settings.pictures_dir
  result = trash.trash_photo(conn, settings, pid)
  assert result["errors"] == []
  for rel in ("2026/fuji/DSCF1001.RAF", "2026/fuji/DSCF1001.RAF.xmp", "2026/fuji/DSCF1001.JPG"):
    assert not os.path.exists(os.path.join(d, rel))
    assert os.path.isfile(os.path.join(d, ".trash", rel))


REAL_RAF = os.environ.get("REAL_RAF")


@pytest.mark.skipif(not REAL_RAF or not os.path.exists(REAL_RAF or ""), reason="REAL_RAF not set")
def test_real_raf_thumbnails_from_the_embedded_preview_and_demosaiced_when_tuned(conn, settings):
  # Ticket 220: X.RAF -> X.RAF.jpg like X.DNG -> X.DNG.jpg. An untouched RAF uses the 1920 px
  # embedded preview at every size; a tuned one demosaics the X-Trans mosaic through LibRaw.
  import shutil
  from PIL import Image
  from photoapp import raw_settings, thumbs
  d = settings.pictures_dir
  os.makedirs(os.path.join(d, "t"))
  shutil.copy(REAL_RAF, os.path.join(d, "t", "A.RAF"))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files WHERE path = 't/A.RAF'").fetchone()[0]
  for size, edge in (("Thumb", 300), ("Small", 1000)):
    path = thumbs.ensure(conn, d, settings.thumbs_dir, fid, "t/A.RAF", size)
    assert path.endswith("/A.RAF.jpg") and max(Image.open(path).size) == edge
  raw_settings.set(conn, fid, wb_mode="auto")
  thumbs.clear(settings.thumbs_dir, conn, fid, "t/A.RAF")
  path = thumbs.ensure(conn, d, settings.thumbs_dir, fid, "t/A.RAF", "Small")
  assert max(Image.open(path).size) == 1000
