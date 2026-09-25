"""A lossy, size-reduced preview DNG per RAW file, for client-side tuning (ticket 102/104).

Not one of thumbs.py's tracked sizes -- a transport artifact for 105's LibRaw-Wasm client, cached
in its own directory alongside the thumbs tree so thumbs.clear()/raw_settings.set() never touch
it (ticket 103's answer: this cache depends only on the original file, never on raw_settings, and
must survive a Save). Still real, undemosaiced Bayer data -- downsampled by binning same-color
pixels together (halving both dimensions per pass, preserving the CFA's 2x2 periodicity) rather
than by demosaicing, so white balance/highlight-recovery/etc. stay genuinely tunable client-side;
see ticket 103's Answer for why a demosaiced ("linear") DNG would defeat that.

rawpy has no DNG-writing API (previews.py stays read-only), so this is the project's first use of
`tifffile`, which can write the DNG-specific tags (CFAPattern, BlackLevel, ColorMatrix1, ...) via
`extratags`. Verified (ticket 103/104) against the real vendored LibRaw-Wasm build, not just
rawpy: a generated preview DNG round-trips through an actual browser `open()` +
`imageData()`/`rawImageData()` call.
"""

import os
import tempfile

import numpy as np
import rawpy
import tifffile
from absl import logging
from PIL import Image

PREVIEW_DIR = "PreviewDNG"   # a cache dimension alongside thumbs.SIZES, tracked purely on disk
MAX_DIM = 1600                # long edge cap -- generous for an on-screen tuning preview
DNG_COLOR_CODE = {"R": 0, "G": 1, "B": 2}   # TIFF-EP/DNG CFAPattern color codes this project needs


class Unsupported(Exception):
  """The source cannot be read as a 2x2-Bayer RAW (matches thumbs.Unsupported's role)."""


def path_for(thumbs_dir, file_path):
  return os.path.join(thumbs_dir, PREVIEW_DIR, file_path + ".preview.dng")


def _make_model(source_path):
  """(Make, Model) from the source's own EXIF (IFD0, tags 271/272), or (None, None).

  Passed straight through into the preview DNG: LibRaw recognizes a real camera's Make/Model
  and applies its own built-in color matrix for it, which is what actually gives a correctly
  colored preview -- confirmed empirically against a real Pentax K-5 DNG, ColorMatrix1/
  AsShotNeutral alone were not enough (see the extratags comment below). Best-effort: read_
  image_metadata already opens RAW files with Pillow purely for EXIF, the same reason this works.
  """
  try:
    with Image.open(source_path) as img:
      exif = img.getexif()
      return exif.get(271), exif.get(272)
  except Exception as e:
    logging.vlog(3, "%s: could not read Make/Model: %s", source_path, e)
    return None, None


FLIP_TO_ORIENTATION = {
    0: 1,  # 0 deg
    1: 2,  # flip H
    2: 4,  # flip V
    3: 3,  # 180 deg
    4: 5,  # transpose
    5: 8,  # 270 CW (90 CCW)
    6: 6,  # 90 CW
    7: 7,  # transverse
}


def _source_exif(source_path, raw):
  """(Make, Model, Orientation) from the source's own EXIF (IFD0), with rawpy fallback."""
  make = model = orient = None
  try:
    with Image.open(source_path) as img:
      exif = img.getexif()
      make, model = exif.get(271), exif.get(272)
      o = exif.get(274)
      if o in range(1, 9):
        orient = o
  except Exception as e:
    logging.vlog(3, "%s: could not read EXIF: %s", source_path, e)

  if orient is None:
    orient = FLIP_TO_ORIENTATION.get(raw.sizes.flip, 1)

  return make, model, orient


def _bin_mosaic(img, factor):
  """Downsample a Bayer mosaic by factor, averaging same-CFA-position pixels together so the
  2x2 pattern stays intact and periodic (each of the 4 phases is binned independently)."""
  if factor == 1:
    return img
  h, w = img.shape
  out = np.empty((h // factor, w // factor), dtype=np.uint16)
  for pr in range(2):
    for pc in range(2):
      sub = img[pr::2, pc::2].astype(np.float32)
      sh, sw = (sub.shape[0] // factor) * factor, (sub.shape[1] // factor) * factor
      sub = sub[:sh, :sw].reshape(sh // factor, factor, sw // factor, factor).mean(axis=(1, 3))
      out[pr::2, pc::2] = np.round(sub).astype(np.uint16)
  return out


def _generate(source_path, dest_path, max_dim=MAX_DIM):
  with rawpy.imread(source_path) as raw:
    pattern = raw.raw_pattern
    if pattern.shape != (2, 2):
      raise Unsupported(f"{source_path}: unsupported CFA pattern shape {pattern.shape} "
                        "(only 2x2 Bayer sensors are supported)")
    cdesc = raw.color_desc.decode()
    tm, lm = raw.sizes.top_margin, raw.sizes.left_margin
    # raw_pattern is phrased relative to raw_image's own (0,0); raw_image_visible starts
    # (top_margin, left_margin) into that, so an odd margin shifts which phase is which color.
    eff_pattern = [[pattern[(r + tm) % 2, (c + lm) % 2] for c in range(2)] for r in range(2)]
    cfa_bytes = bytes(DNG_COLOR_CODE[cdesc[eff_pattern[r][c]]] for r in range(2) for c in range(2))

    img = raw.raw_image_visible
    h, w = img.shape
    factor = 1
    while max(h // factor, w // factor) > max_dim:
      factor *= 2
    # crop to an even multiple of 2*factor first so binning stays phase-aligned
    h2, w2 = (h // (2 * factor)) * (2 * factor), (w // (2 * factor)) * (2 * factor)
    img = _bin_mosaic(img[:h2, :w2], factor)

    black_per_pos = [int(raw.black_level_per_channel[eff_pattern[r][c]])
                     for r in range(2) for c in range(2)]
    # DNG's ColorMatrix1 is XYZ-to-camera; rawpy's rgb_xyz_matrix is the other direction
    # (camera-to-XYZ, with a possible unused 4th row for a second green channel).
    color_matrix1 = np.linalg.inv(np.array(raw.rgb_xyz_matrix[:3], dtype=np.float64))
    wb = np.array(raw.camera_whitebalance[:3], dtype=np.float64)
    wb = wb / wb[1]
    as_shot_neutral = (1.0 / wb).tolist()

    def rational(values, denom=1000000):
      # DNG's numeric tags are RATIONAL/SRATIONAL (a signed/unsigned int32 numerator and
      # denominator), not float/double -- LibRaw's DNG tag parser silently drops values given as
      # DOUBLE for at least BlackLevel and ColorMatrix1 (confirmed empirically: a first version of
      # this function used 'd' for those and both came back as zero through rawpy on the result).
      out = []
      for v in values:
        if float(v).is_integer():
          out.extend((int(v), 1))
        else:
          out.extend((round(v * denom), denom))
      return tuple(out)

    make, model, orient = _source_exif(source_path, raw)

    extratags = [
        (254, "I", 1, (0,), False),                                    # NewSubfileType: main image
        (274, "H", 1, (int(orient),), False),                           # Orientation (ticket 118)
        (33421, "H", 2, (2, 2), False),                                # CFARepeatPatternDim
        (33422, "B", 4, cfa_bytes, False),                             # CFAPattern
        (50706, "B", 4, (1, 4, 0, 0), False),                          # DNGVersion 1.4.0.0
        (50707, "B", 4, (1, 1, 0, 0), False),                          # DNGBackwardVersion 1.1.0.0
        # BlackLevel needs its RepeatDim tag declared alongside it, or LibRaw silently treats a
        # multi-value BlackLevel as unset (confirmed empirically: black_level_per_channel came
        # back all zeros without this, even though the BlackLevel tag itself was present and
        # correctly typed).
        (50713, "H", 2, (2, 2), False),                                # BlackLevelRepeatDim
        (50714, tifffile.DATATYPE.RATIONAL, 4, rational(black_per_pos), False),   # BlackLevel
        (50717, "H", 1, (int(raw.white_level),), False),               # WhiteLevel
        (50728, tifffile.DATATYPE.RATIONAL, 3, rational(as_shot_neutral), False),  # AsShotNeutral
    ]
    # Make/Model, when the source has them, let LibRaw apply its own built-in color matrix for
    # this camera -- confirmed empirically to match the original file's own rendering almost
    # exactly (mean RGB within ~1%). UniqueCameraModel + an embedded ColorMatrix1/2 is the
    # fallback for a source with no usable Make/Model: also confirmed empirically, but as a
    # *combination* with Make/Model present, not a complement -- LibRaw's built-in table produced
    # visibly wrong color once an embedded ColorMatrix1 sat alongside a recognized Make/Model (mean
    # green channel off by ~2.3x), so the two paths are mutually exclusive, not layered.
    if make and model:
      extratags.append((271, "s", 0, str(make).strip() + "\x00", False))
      extratags.append((272, "s", 0, str(model).strip() + "\x00", False))
    else:
      extratags.append((50708, "s", 0, "photoapp tuning preview\x00", False))  # UniqueCameraModel
      extratags.append(
          (50721, tifffile.DATATYPE.SRATIONAL, 9, rational(color_matrix1.flatten()), False))
      extratags.append(
          (50722, tifffile.DATATYPE.SRATIONAL, 9, rational(color_matrix1.flatten()), False))
      extratags.append((50778, "H", 1, (21,), False))   # CalibrationIlluminant1 (D65)
      extratags.append((50779, "H", 1, (17,), False))   # CalibrationIlluminant2 (StdA)

  os.makedirs(os.path.dirname(dest_path), exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest_path), suffix=".tmp")
  os.close(fd)   # tifffile.imwrite wants a path (it reopens by name), not an fd/file object
  try:
    tifffile.imwrite(tmp, img, photometric=tifffile.PHOTOMETRIC.CFA, extratags=extratags)
    os.replace(tmp, dest_path)
  except BaseException:
    if os.path.exists(tmp):
      os.unlink(tmp)
    raise
  logging.vlog(5, "%s: generated preview DNG %dx%d (bin factor %d)",
              source_path, img.shape[1], img.shape[0], factor)


def ensure(thumbs_dir, pictures_dir, file_path):
  """The cached preview DNG's path for file_path, generating it if this is the first request.

  Never overwritten once made (this codebase's usual cache convention -- see thumbs.clear's
  comment): the artifact depends only on the original file, so there is nothing to invalidate.
  Raises Unsupported if the source cannot be read as a 2x2-Bayer RAW.
  """
  dest = path_for(thumbs_dir, file_path)
  if os.path.isfile(dest):
    logging.vlog(7, "%s: preview DNG already cached", file_path)
    return dest
  try:
    _generate(os.path.join(pictures_dir, file_path), dest)
  except (rawpy.LibRawError, OSError, ValueError) as e:
    raise Unsupported(f"{file_path}: {e}") from e
  return dest
