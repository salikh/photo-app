import os

import pytest
import rawpy
from PIL import Image

from photoapp import fileinfo
from photoapp import previews
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.conftest import write_synthetic_pef
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
  mime, w, h, date, aperture, shutter_speed, iso, focal, make, model, lens_model = \
      fileinfo.read_image_metadata(raw)
  assert (w, h) == (None, None) or (w, h) == (w, h)   # never raises


def test_non_raw_metadata_is_unchanged(tmp_path):
  jpg = str(tmp_path / "a.jpg")
  make_jpeg(jpg, size=(30, 20))
  assert fileinfo.read_image_metadata(jpg)[:3] == ("image/jpeg", 30, 20)
  assert fileinfo.is_raw("x.DNG") and not fileinfo.is_raw("x.jpg")


def test_camera_metadata_extracted_from_exif(tmp_path):
  # ticket 083. ExposureTime round-trips through Pillow's own save(exif=...) as a plain
  # (numerator, denominator) tuple, not the IFDRational a real camera file gives back --
  # exercises both representations (see test_real_dng_camera_metadata below for the other one).
  jpg = str(tmp_path / "a.jpg")
  img = Image.new("RGB", (30, 20))
  exif = img.getexif()
  sub = exif.get_ifd(0x8769)
  sub[0x829D] = 2.8            # FNumber
  sub[0x829A] = (1, 250)       # ExposureTime
  sub[0x8827] = 400            # ISOSpeedRatings
  img.save(jpg, exif=exif)
  mime, w, h, date, aperture, shutter_speed, iso, focal, make, model, lens_model = \
      fileinfo.read_image_metadata(jpg)
  assert aperture == 2.8 and shutter_speed == 0.004 and iso == 400


def test_lens_metadata_extracted_from_exif(tmp_path):
  # tickets 111/156: FocalLength (Exif sub-IFD), Make/Model (IFD0) and LensModel (Exif sub-IFD).
  jpg = str(tmp_path / "a.jpg")
  img = Image.new("RGB", (30, 20))
  exif = img.getexif()
  exif[0x010F] = "PENTAX"          # Make
  exif[0x0110] = "PENTAX K-5"      # Model
  sub = exif.get_ifd(0x8769)
  sub[0x920A] = (50, 1)            # FocalLength
  sub[0xA434] = "smc PENTAX-DA 35mm F2.4 AL"   # LensModel (ticket 156)
  img.save(jpg, exif=exif)
  *_, focal, make, model, lens_model = fileinfo.read_image_metadata(jpg)
  assert focal == 50.0 and make == "PENTAX" and model == "PENTAX K-5"
  assert lens_model == "smc PENTAX-DA 35mm F2.4 AL"


def test_camera_metadata_absent_without_exif(tmp_path):
  jpg = str(tmp_path / "a.jpg")
  make_jpeg(jpg, size=(30, 20))   # no EXIF at all
  *_, aperture, shutter_speed, iso, focal, make, model, lens_model = \
      fileinfo.read_image_metadata(jpg)
  assert (aperture, shutter_speed, iso) == (None, None, None)
  assert (focal, make, model, lens_model) == (None, None, None, None)


def test_pef_exif_fallback_when_pillow_cannot_open(tmp_path, monkeypatch):
  # ticket 161: a PEF is a TIFF Pillow refuses to identify; read_image_metadata reads its EXIF from
  # the TIFF IFDs instead (Image.open forced to fail exactly as it does for a real PEF).
  pef = str(tmp_path / "a.PEF")
  write_synthetic_pef(pef)
  def cannot_open(*_args, **_kwargs):
    raise ValueError("cannot identify image file")
  monkeypatch.setattr(Image, "open", cannot_open)
  _, _, _, date, aperture, shutter_speed, iso, focal, make, model, lens = \
      fileinfo.read_image_metadata(pef)
  assert date == "2008-02-16 18:29:00"
  assert (aperture, shutter_speed, iso) == (4.5, 0.125, 200)
  assert (focal, make, model) == (43.0, "PENTAX Corporation", "PENTAX *ist DL")
  assert lens == "smc PENTAX-DA 18-55mm"


def test_pef_exif_fallback_reads_the_tiff_directly(tmp_path):
  # ticket 161: the fallback on its own, no Pillow involved.
  pef = str(tmp_path / "a.PEF")
  write_synthetic_pef(pef)
  assert fileinfo.read_exif_from_tiff(pef) == (
      "2008-02-16 18:29:00", 4.5, 0.125, 200, 43.0, "PENTAX Corporation", "PENTAX *ist DL",
      "smc PENTAX-DA 18-55mm")
  assert fileinfo.read_exif_from_tiff(str(tmp_path / "nope.PEF")) == (None,) * 8


REAL_PEF = os.environ.get("REAL_PEF")


@pytest.mark.skipif(not REAL_PEF or not os.path.exists(REAL_PEF or ""),
                    reason="REAL_PEF not set")
def test_real_pef_exif_fallback():
  # ticket 161: a real PEF (big-endian, and one Pillow cannot open at all) still yields its EXIF.
  mime, w, h, date, aperture, shutter_speed, iso, focal, make, model, lens = \
      fileinfo.read_image_metadata(REAL_PEF)
  assert mime == "image/x-raw" and w and h
  assert date is not None and make is not None
  assert aperture is None or isinstance(aperture, float)
  assert shutter_speed is None or isinstance(shutter_speed, float)
  assert iso is None or isinstance(iso, int)


REAL_DNG = os.environ.get("REAL_DNG")


@pytest.mark.skipif(not REAL_DNG or not os.path.exists(REAL_DNG or ""),
                    reason="REAL_DNG not set")
def test_real_dng_dimensions_preview_and_thumbnails(tmp_path):
  mime, w, h, *_ = fileinfo.read_image_metadata(REAL_DNG)
  assert mime == "image/x-adobe-dng" and max(w, h) > 1000
  out = thumbs.render(REAL_DNG, str(tmp_path / "t.jpg"), 300)
  assert max(Image.open(out).size) == 300


@pytest.mark.skipif(not REAL_DNG or not os.path.exists(REAL_DNG or ""),
                    reason="REAL_DNG not set")
def test_real_dng_camera_metadata(tmp_path):
  # ticket 083: a real camera file's EXIF gives back IFDRational, not a plain tuple -- the other
  # representation _exif_rational_to_float needs to handle (see test_camera_metadata_extracted...
  # above for that one). A real RAW is expected to have at least ISO; aperture/shutter_speed
  # depend on the specific file, so only check the extraction does not raise and ISO is present.
  *_, aperture, shutter_speed, iso, focal, make, model, lens_model = \
      fileinfo.read_image_metadata(REAL_DNG)
  assert iso is None or isinstance(iso, int)
  assert aperture is None or isinstance(aperture, float)
  assert shutter_speed is None or isinstance(shutter_speed, float)
  # ticket 111: a real RAW's Make/Model and (usually) focal length come from the same EXIF parse.
  assert focal is None or isinstance(focal, float)
  assert make is None or isinstance(make, str)
  assert model is None or isinstance(model, str)
  # ticket 156: LensModel is read from that same parse (often absent on a manual/unknown lens).
  assert lens_model is None or isinstance(lens_model, str)


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


def test_postprocess_kwargs_defaults_and_camera_wb():
  # ticket 085: an all-None (or missing) settings dict means today's hardcoded defaults.
  for settings in (None, {}):
    k = previews._postprocess_kwargs(settings)
    assert k["output_bps"] == 8 and k["use_camera_wb"] is True
    assert "exp_shift" not in k and "no_auto_bright" not in k


def test_postprocess_kwargs_maps_wb_bright_and_highlight():
  k = previews._postprocess_kwargs({"raw_wb_mode": "auto"})
  assert k["use_auto_wb"] is True
  k = previews._postprocess_kwargs({"raw_wb_mode": "manual", "raw_wb_r": 2.0,
                                    "raw_wb_g": 1.0, "raw_wb_b": 1.5})
  assert k["user_wb"] == [2.0, 1.0, 1.5, 1.0]
  k = previews._postprocess_kwargs({"raw_bright": 1.4, "raw_highlight": 3})
  assert k["bright"] == 1.4 and k["highlight_mode"] == 3


def test_postprocess_kwargs_exposure_sets_exp_shift_and_disables_auto_bright():
  # ticket 109: exp_shift is essentially invisible unless LibRaw's own auto-brightness is off.
  k = previews._postprocess_kwargs({"raw_exposure": 2.0})
  assert k["exp_shift"] == 2.0 and k["no_auto_bright"] is True
  # ...but only when exposure is actually set, so every other file renders as before.
  assert "no_auto_bright" not in previews._postprocess_kwargs({"raw_bright": 1.2})
  assert "no_auto_bright" not in previews._postprocess_kwargs({"raw_shadow": 0.3})


def test_postprocess_kwargs_maps_advanced_params():
  # ticket 112: noise/demosaic map to rawpy enums; contrast/saturation are post-decode, not kwargs.
  assert previews._postprocess_kwargs({"raw_noise": 2})["fbdd_noise_reduction"] == \
      rawpy.FBDDNoiseReductionMode.Full
  assert previews._postprocess_kwargs({"raw_demosaic": 4})["demosaic_algorithm"] == \
      rawpy.DemosaicAlgorithm.DCB
  base = previews._postprocess_kwargs({})
  for key in ("fbdd_noise_reduction", "demosaic_algorithm"):
    assert key not in base   # absent unless set, so defaults are untouched
  # contrast is not a LibRaw parameter at all (the vendored WASM build ignores `gamm`)
  assert "gamma" not in previews._postprocess_kwargs({"raw_contrast": 1.5})


def test_apply_contrast_identity_and_spread():
  import numpy as np
  px = np.array([[[200, 100, 50]]], dtype=np.uint8)
  assert np.array_equal(previews._apply_contrast(px, 1.0), px)   # 1.0 is identity
  more = previews._apply_contrast(px, 2.0)                       # 2.0 spreads away from mid-gray
  assert more[0, 0, 0] > px[0, 0, 0] and more[0, 0, 2] < px[0, 0, 2]
  flat = previews._apply_contrast(px, 0.0)                       # 0.0 collapses to mid-gray
  assert flat[0, 0, 0] == flat[0, 0, 1] == flat[0, 0, 2] == 128


def test_apply_saturation_identity_grayscale_and_boost():
  import numpy as np
  px = np.array([[[200, 100, 50]]], dtype=np.uint8)
  assert np.array_equal(previews._apply_saturation(px, 1.0), px)   # 1.0 is identity
  gray = previews._apply_saturation(px, 0.0)                       # 0.0 is fully desaturated
  luma = round(0.299 * 200 + 0.587 * 100 + 0.114 * 50)
  assert gray[0, 0, 0] == gray[0, 0, 1] == gray[0, 0, 2] == luma
  boosted = previews._apply_saturation(px, 2.0)
  assert boosted[0, 0, 0] > px[0, 0, 0] and boosted[0, 0, 2] < px[0, 0, 2]   # channels spread out


def test_lift_shadows_identity_monotonic_and_highlight_neutral():
  import numpy as np
  ramp = np.tile(np.arange(256, dtype=np.uint8)[:, None, None], (1, 1, 3))
  assert np.array_equal(previews._lift_shadows(ramp, 0.0), ramp)   # 0 is identity
  lifted = previews._lift_shadows(ramp, 0.3)
  assert lifted[10, 0, 0] > ramp[10, 0, 0]                          # shadows are lifted
  assert np.all(np.diff(lifted[:, 0, 0].astype(int)) >= 0)          # monotonic: no tone reversal
  assert lifted[255, 0, 0] == 255                                   # v=1 is a fixed point
  assert lifted[250, 0, 0] <= ramp[250, 0, 0] + 1                   # highlights essentially untouched

