import os

import pytest
from PIL import Image

from photoapp import fileinfo
from photoapp import previews
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.test_grouping import touch


def test_oriented_rotates_only_when_preview_is_not_already_upright():
  landscape, portrait = Image.new("RGB", (200, 100)), Image.new("RGB", (100, 200))
  # sensor is landscape, flip 5/6 means the picture is shown portrait
  assert previews._oriented(landscape, 5, True).size == (100, 200)
  assert previews._oriented(landscape, 6, True).size == (100, 200)
  assert previews._oriented(portrait, 6, True).size == (100, 200)   # already rotated
  assert previews._oriented(landscape, 0, True).size == (200, 100)
  assert previews._oriented(landscape, 3, True).size == (200, 100)  # 180: left alone
  # direction: flip 6 is clockwise, 5 counter-clockwise
  img = Image.new("RGB", (2, 1)); img.putpixel((0, 0), (255, 0, 0))
  assert previews._oriented(img, 6, True).getpixel((0, 0)) == (255, 0, 0) and \
      previews._oriented(img, 6, True).getpixel((0, 0))[0] == 255
  cw, ccw = previews._oriented(img, 6, True), previews._oriented(img, 5, True)
  assert cw.getpixel((0, 0))[0] == 255 and ccw.getpixel((0, 1))[0] == 255


def test_undecodable_raw_yields_no_size_no_preview_and_unsupported(tmp_path):
  raw = str(tmp_path / "a.dng")
  touch(raw)
  assert fileinfo.read_raw_size(raw) is None
  assert previews.embedded_preview(raw) is None
  assert previews.render(raw) is None
  with pytest.raises(thumbs.Unsupported):
    thumbs.render(raw, str(tmp_path / "o.jpg"), 300)
  mime, w, h, date = fileinfo.read_image_metadata(raw)
  assert (w, h) == (None, None) or (w, h) == (w, h)   # never raises


def test_non_raw_metadata_is_unchanged(tmp_path):
  jpg = str(tmp_path / "a.jpg")
  make_jpeg(jpg, size=(30, 20))
  assert fileinfo.read_image_metadata(jpg)[:3] == ("image/jpeg", 30, 20)
  assert fileinfo.is_raw("x.DNG") and not fileinfo.is_raw("x.jpg")


REAL_DNG = os.environ.get("REAL_DNG")


@pytest.mark.skipif(not REAL_DNG or not os.path.exists(REAL_DNG or ""),
                    reason="REAL_DNG not set")
def test_real_dng_dimensions_preview_and_thumbnails(tmp_path):
  mime, w, h, _ = fileinfo.read_image_metadata(REAL_DNG)
  assert mime == "image/x-adobe-dng" and max(w, h) > 1000
  out = thumbs.render(REAL_DNG, str(tmp_path / "t.jpg"), 300)
  assert max(Image.open(out).size) == 300


def test_migration_forces_reread_of_raw_rows(tmp_path):
  import sqlite3
  from photoapp import db
  path = str(tmp_path / "app.sqlite")
  conn = sqlite3.connect(path)
  for script in db.MIGRATIONS[:4]:
    conn.executescript(script)
  conn.execute("PRAGMA user_version = 4")
  conn.execute("INSERT INTO files (path, mtime, bytesize, width, height) VALUES "
               "('a.DNG', 1, 100, 160, 120), ('b.jpg', 1, 100, 30, 20)")
  conn.execute("INSERT INTO dir_mtimes VALUES ('.', 5)")
  conn.commit()
  conn.close()
  conn = db.connect(path)
  sizes = dict(conn.execute("SELECT path, bytesize FROM files").fetchall())
  assert sizes == {"a.DNG": -1, "b.jpg": 100}
  assert conn.execute("SELECT COUNT(*) FROM dir_mtimes").fetchone()[0] == 0
