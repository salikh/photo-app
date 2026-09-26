import os
import shutil

import pytest
import rawpy

from photoapp import raw_preview_dng
from tests.test_grouping import touch


def test_undecodable_raw_raises_unsupported_and_leaves_no_partial_file(tmp_path):
  pictures = tmp_path / "pics"
  thumbs_dir = tmp_path / "thumbs"
  raw = pictures / "a.dng"
  touch(str(raw))   # a real file exists, but rawpy can't decode a 1-byte fake
  with pytest.raises(raw_preview_dng.Unsupported):
    raw_preview_dng.ensure(str(thumbs_dir), str(pictures), "a.dng")
  assert not os.path.exists(raw_preview_dng.path_for(str(thumbs_dir), "a.dng"))


def test_stats_reports_usage_and_lacking_from_disk(settings, conn):
  from photoapp import scan
  from tests.conftest import make_jpeg
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "y", "a.jpg"))
  touch(os.path.join(d, "y", "K1.DNG"))
  scan.scan(conn, d)

  usage, lacking = raw_preview_dng.stats(conn, settings.thumbs_dir)
  assert usage == {"files": 0, "bytes": 0} and lacking == 1   # the one RAW has no preview yet

  dest = raw_preview_dng.path_for(settings.thumbs_dir, "y/K1.DNG")
  os.makedirs(os.path.dirname(dest), exist_ok=True)
  with open(dest, "wb") as f:
    f.write(b"x" * 123)
  usage, lacking = raw_preview_dng.stats(conn, settings.thumbs_dir)
  assert usage == {"files": 1, "bytes": 123} and lacking == 0  # counted straight from disk


REAL_DNG = os.environ.get("REAL_DNG")
_skip = pytest.mark.skipif(not REAL_DNG or not os.path.exists(REAL_DNG or ""), reason="REAL_DNG not set")


@_skip
def test_real_dng_preview_is_downsampled_and_still_a_valid_bayer_raw(tmp_path):
  pictures = tmp_path / "pics"
  os.makedirs(pictures)
  shutil.copy(REAL_DNG, pictures / "a.dng")
  thumbs_dir = str(tmp_path / "thumbs")

  dest = raw_preview_dng.ensure(thumbs_dir, str(pictures), "a.dng")
  assert dest == raw_preview_dng.path_for(thumbs_dir, "a.dng")
  assert os.path.getsize(dest) > 0

  with rawpy.imread(dest) as raw:
    assert max(raw.sizes.width, raw.sizes.height) <= raw_preview_dng.MAX_DIM
    assert raw.raw_pattern.shape == (2, 2)   # still real, undemosaiced Bayer data
    rgb = raw.postprocess(use_camera_wb=True)
  assert 0 < rgb.mean() < 255   # not degenerate (all-black/all-white/NaN)


@_skip
def test_real_dng_preview_color_matches_the_original_closely(tmp_path):
  # Ticket 106's "drift risk" concern, checked directly: LibRaw's color pipeline turned out to
  # ignore a synthetic DNG's own embedded ColorMatrix1 entirely unless the camera's real Make/
  # Model (passed through from the source's EXIF, see _make_model) is also present, letting LibRaw
  # use its own built-in matrix for that camera -- the same one it uses for the original file, so
  # the two should render close to identically (mean RGB within a few percent; some difference is
  # expected from the resolution reduction itself, not a color bug).
  pictures = tmp_path / "pics"
  os.makedirs(pictures)
  shutil.copy(REAL_DNG, pictures / "a.dng")
  thumbs_dir = str(tmp_path / "thumbs")
  dest = raw_preview_dng.ensure(thumbs_dir, str(pictures), "a.dng")

  with rawpy.imread(dest) as preview:
    preview_rgb = preview.postprocess(use_camera_wb=True, output_bps=8)
  with rawpy.imread(REAL_DNG) as original:
    original_rgb = original.postprocess(use_camera_wb=True, output_bps=8)

  for c in range(3):
    p, o = preview_rgb[..., c].mean(), original_rgb[..., c].mean()
    assert abs(p - o) / max(o, 1) < 0.15, f"channel {c}: preview {p} vs original {o}"


@_skip
def test_real_dng_preview_is_cached_not_regenerated(tmp_path, monkeypatch):
  pictures = tmp_path / "pics"
  os.makedirs(pictures)
  shutil.copy(REAL_DNG, pictures / "a.dng")
  thumbs_dir = str(tmp_path / "thumbs")

  first = raw_preview_dng.ensure(thumbs_dir, str(pictures), "a.dng")
  mtime = os.path.getmtime(first)

  def boom(*a, **k):
    raise AssertionError("should not regenerate an already-cached preview DNG")
  monkeypatch.setattr(raw_preview_dng, "_generate", boom)

  second = raw_preview_dng.ensure(thumbs_dir, str(pictures), "a.dng")
  assert second == first and os.path.getmtime(second) == mtime


@_skip
def test_real_dng_preview_preserves_orientation(tmp_path):
  # Ticket 118: preview DNG must preserve orientation (tag 274 / sizes.flip)
  # so LibRaw-Wasm on the client renders the preview in the correct orientation.
  pictures = tmp_path / "pics"
  os.makedirs(pictures)
  shutil.copy(REAL_DNG, pictures / "a.dng")
  thumbs_dir = str(tmp_path / "thumbs")
  dest = raw_preview_dng.ensure(thumbs_dir, str(pictures), "a.dng")

  with rawpy.imread(dest) as preview:
    preview_flip = preview.sizes.flip
    preview_shape = preview.postprocess(use_camera_wb=True).shape
  with rawpy.imread(REAL_DNG) as original:
    original_flip = original.sizes.flip
    original_shape = original.postprocess(use_camera_wb=True).shape

  assert preview_flip == original_flip
  assert (preview_shape[0] > preview_shape[1]) == (original_shape[0] > original_shape[1])

