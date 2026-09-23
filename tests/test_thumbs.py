import os

from PIL import Image

from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.test_grouping import touch


def put_thumb(thumbs_dir, size, rel, dims=(10, 7)):
  path = os.path.join(thumbs_dir, size, rel)
  make_jpeg(path, size=dims)
  return path


def test_extension_mapping():
  assert thumbs.thumb_relpath("2019/x/K1.JPG") == "2019/x/K1.jpg"
  assert thumbs.thumb_relpath("K1.jpg") == "K1.jpg"
  assert thumbs.thumb_relpath("K1.DNG") == "K1.DNG.jpg"
  assert thumbs.thumb_relpath("a.png") == "a.png.jpg"
  assert thumbs.thumb_relpath("a.PEF") == "a.PEF.jpg"
  assert thumbs.thumb_relpath("a.tiff") == "a.tiff.jpg"


def test_lookup_and_best_available(settings):
  t = settings.thumbs_dir
  assert thumbs.lookup(t, "Small", "d/K1.JPG") is None
  put_thumb(t, "Medium", "d/K1.jpg")
  assert thumbs.lookup(t, "Small", "d/K1.JPG") is None
  size, path = thumbs.best_available(t, "Small", "d/K1.JPG")
  assert size == "Medium" and path.endswith("Medium/d/K1.jpg")
  assert thumbs.best_available(t, "Huge", "d/K1.JPG") is None
  put_thumb(t, "Small", "d/K1.jpg")
  assert thumbs.best_available(t, "Small", "d/K1.JPG")[0] == "Small"


def test_render_scales_down_never_up_and_honors_exif_orientation(tmp_path):
  src = str(tmp_path / "big.jpg")
  make_jpeg(src, size=(1200, 800))
  out = thumbs.render(src, str(tmp_path / "o" / "t.jpg"), 300)
  assert Image.open(out).size == (300, 200)
  small = str(tmp_path / "small.jpg")
  make_jpeg(small, size=(100, 50))
  assert Image.open(thumbs.render(small, str(tmp_path / "s.jpg"), 300)).size == (100, 50)
  assert Image.open(thumbs.render(src, str(tmp_path / "full.jpg"), None)).size == (1200, 800)
  rot = str(tmp_path / "rot.jpg")
  img = Image.new("RGB", (200, 100))
  exif = img.getexif()
  exif[0x0112] = 6   # rotate 90 clockwise on display
  img.save(rot, exif=exif)
  assert Image.open(thumbs.render(rot, str(tmp_path / "r.jpg"), 300)).size == (100, 200)
  assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []


def test_render_unsupported_for_raw(tmp_path):
  raw = str(tmp_path / "a.dng")
  touch(raw)
  try:
    thumbs.render(raw, str(tmp_path / "o.jpg"), 300)
  except thumbs.Unsupported:
    pass
  else:
    raise AssertionError("expected Unsupported")


def test_ensure_order_existing_then_downscale_then_original(conn, settings):
  d, t = settings.pictures_dir, settings.thumbs_dir
  make_jpeg(os.path.join(d, "y", "a.JPG"), size=(3000, 2000))
  make_jpeg(os.path.join(d, "y", "b.JPG"), size=(3000, 2000))
  scan.scan(conn, d)
  fid = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM files")}
  # existing Small is used as is
  existing = put_thumb(t, "Small", "y/a.jpg", dims=(1000, 667))
  assert thumbs.ensure(conn, d, t, fid["y/a.JPG"], "y/a.JPG", "Small") == existing
  row = conn.execute("SELECT source FROM thumbs WHERE file_id = ? AND size = 'Small'",
                     (fid["y/a.JPG"],)).fetchone()
  assert row["source"] == "existing"
  # Thumb is made from the existing Small, not the 3000 px original
  out = thumbs.ensure(conn, d, t, fid["y/a.JPG"], "y/a.JPG", "Thumb")
  assert Image.open(out).size == (300, 200) and out.endswith("Thumb/y/a.jpg")
  # nothing exists for b: rendered from the original
  out = thumbs.ensure(conn, d, t, fid["y/b.JPG"], "y/b.JPG", "Medium")
  assert Image.open(out).size == (2000, 1333)
  assert conn.execute("SELECT source FROM thumbs WHERE file_id = ? AND size = 'Medium'",
                      (fid["y/b.JPG"],)).fetchone()["source"] == "pillow"


def test_ensure_returns_none_for_raw_without_thumbnail(conn, settings):
  touch(os.path.join(settings.pictures_dir, "a.dng"))
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  assert thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir,
                       fid, "a.dng", "Small") is None


def test_make_tuned_raw_demosaics_once_and_downscales_the_rest_from_huge(conn, settings, monkeypatch):
  # Ticket 093: a settings-tuned RAW's first differently-sized request demosaics once at Huge
  # (regardless of which size was actually asked for) and every later request for a different
  # size downscales from that cached Huge instead of demosaicing again.
  from photoapp import previews
  from photoapp import raw_settings

  calls = {"demosaic": 0}

  def render(path, settings=None, half_size=False):
    calls["demosaic"] += 1
    return Image.new("RGB", (1600, 1200), (10, 20, 30))

  monkeypatch.setattr(previews, "render", render)

  d, t = settings.pictures_dir, settings.thumbs_dir
  touch(os.path.join(d, "a.DNG"))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  raw_settings.set(conn, fid, bright=1.4)

  out = thumbs.ensure(conn, d, t, fid, "a.DNG", "Thumb")
  assert Image.open(out).size == (300, 225) and calls == {"demosaic": 1}
  assert thumbs.lookup(t, "Huge", "a.DNG") is not None   # bonus Huge written as a side effect

  out = thumbs.ensure(conn, d, t, fid, "a.DNG", "Medium")
  assert Image.open(out).size == (1600, 1200) and calls == {"demosaic": 1}   # downscaled, not re-demosaiced

  out = thumbs.ensure(conn, d, t, fid, "a.DNG", "Huge")
  assert Image.open(out).size == (1600, 1200) and calls == {"demosaic": 1}   # the bonus write, found as-is


def test_clear_removes_cached_files_and_rows_so_the_next_request_regenerates(conn, settings):
  d, t = settings.pictures_dir, settings.thumbs_dir
  make_jpeg(os.path.join(d, "y", "a.JPG"), size=(3000, 2000))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  thumbs.ensure(conn, d, t, fid, "y/a.JPG", "Thumb")
  thumbs.ensure(conn, d, t, fid, "y/a.JPG", "Medium")
  assert conn.execute("SELECT COUNT(*) FROM thumbs WHERE file_id = ?", (fid,)).fetchone()[0] == 2
  thumb_path_on_disk = thumbs.thumb_path(t, "Thumb", "y/a.JPG")
  assert os.path.exists(thumb_path_on_disk)

  cleared = thumbs.clear(t, conn, fid, "y/a.JPG")
  assert sorted(cleared) == ["Medium", "Thumb"]
  assert not os.path.exists(thumb_path_on_disk)
  assert conn.execute("SELECT COUNT(*) FROM thumbs WHERE file_id = ?", (fid,)).fetchone()[0] == 0

  # clearing again is a no-op (nothing left to clear), and a fresh ensure() regenerates it
  assert thumbs.clear(t, conn, fid, "y/a.JPG") == []
  out = thumbs.ensure(conn, d, t, fid, "y/a.JPG", "Thumb")
  assert out == thumb_path_on_disk and os.path.exists(out)


def test_index_existing_usage_and_lacking(conn, settings):
  d, t = settings.pictures_dir, settings.thumbs_dir
  touch(os.path.join(d, "y", "K1.DNG"))
  make_jpeg(os.path.join(d, "y", "K1.JPG"))
  make_jpeg(os.path.join(d, "top.jpg"))
  put_thumb(t, "Thumb", "y/K1.DNG.jpg")
  put_thumb(t, "Thumb", "y/K1.jpg")
  put_thumb(t, "Small", "y/K1.jpg")
  put_thumb(t, "Thumb", "top.jpg")
  os.makedirs(os.path.join(t, "Small", "empty"))
  scan.scan(conn, d)
  assert thumbs.index_existing(conn, t, ["y", ".", "nope"]) == 4
  u = thumbs.usage(conn)
  assert u["Thumb"]["files"] == 3 and u["Small"]["files"] == 1
  assert u["Huge"] == {"files": 0, "bytes": 0} and u["Thumb"]["bytes"] > 0
  assert thumbs.lacking(conn) == {"Thumb": 0, "Small": 2, "Medium": 3, "Huge": 3}


def test_scan_records_existing_thumbnails_when_given_thumbs_dir(conn, settings):
  d, t = settings.pictures_dir, settings.thumbs_dir
  make_jpeg(os.path.join(d, "y", "a.jpg"))
  put_thumb(t, "Small", "y/a.jpg")
  scan.scan(conn, d, thumbs_dir=t)
  assert thumbs.usage(conn)["Small"]["files"] == 1
