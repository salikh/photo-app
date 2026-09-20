"""RAW support through LibRaw (rawpy), replacing exiftool + dcraw.

embedded_preview(): the camera's own JPEG preview, usually full size and
available in milliseconds. render(): a demosaiced half-size render, slower
(about a second) and used only when there is no usable embedded preview.
"""

import io

import numpy as np
import rawpy
from PIL import Image

MIN_PREVIEW_EDGE = 1000   # smaller embedded previews are not used


def _oriented(img, flip, raw_is_landscape):
  """Rotate an embedded preview to display orientation if it is not already.

  LibRaw flip: 0 none, 3 = 180, 5 = 90 counter-clockwise, 6 = 90 clockwise.
  Previews are normally stored already rotated; detect the exception from the
  aspect ratio (the 180 degree case cannot be detected and is left alone).
  """
  if flip in (5, 6):
    want_portrait = raw_is_landscape
    if (img.height > img.width) != want_portrait:
      img = img.rotate(90 if flip == 5 else -90, expand=True)
  return img


def embedded_preview(path):
  """The embedded JPEG/bitmap preview as a PIL image, or None."""
  try:
    with rawpy.imread(path) as raw:
      thumb = raw.extract_thumb()
      sizes = raw.sizes
      if thumb.format == rawpy.ThumbFormat.JPEG:
        img = Image.open(io.BytesIO(thumb.data))
        img.load()
      elif thumb.format == rawpy.ThumbFormat.BITMAP:
        img = Image.fromarray(np.asarray(thumb.data))
      else:
        return None
      img = _oriented(img, sizes.flip, sizes.width >= sizes.height)
  except (rawpy.LibRawError, OSError, ValueError):
    return None
  if max(img.size) < MIN_PREVIEW_EDGE:
    return None
  return img


def render(path):
  """A half-size demosaiced render (camera white balance), or None."""
  try:
    with rawpy.imread(path) as raw:
      rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
  except (rawpy.LibRawError, OSError, ValueError):
    return None
  return Image.fromarray(rgb)
