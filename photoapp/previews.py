"""RAW support through LibRaw (rawpy) -- the one shared renderer for both the on-demand and
background thumbnail paths (ticket 090's resolution to ticket 085).

embedded_preview(): the camera's own JPEG preview, usually full size and available in
milliseconds -- no demosaic, so per-file settings (ticket 085) have no effect on it.
render(): an actual LibRaw demosaic, applying those settings, slower (about a second at full
size); used whenever the embedded-preview shortcut isn't available or isn't allowed for this
file (see docs/design/thumbnails.md).
"""

import io

import numpy as np
import rawpy
from absl import logging
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
        logging.vlog(3, "%s: unsupported embedded thumb format %r, no preview",
                     path, thumb.format)
        return None
      img = _oriented(img, sizes.flip, sizes.width >= sizes.height)
  except (rawpy.LibRawError, OSError, ValueError) as e:
    logging.vlog(3, "%s: embedded preview extraction failed: %s", path, e)
    return None
  if max(img.size) < MIN_PREVIEW_EDGE:
    logging.vlog(3, "%s: embedded preview too small (%dx%d), no preview",
                 path, *img.size)
    return None
  logging.vlog(7, "%s: extracted embedded preview (%dx%d)", path, *img.size)
  return img


def _postprocess_kwargs(settings):
  """rawpy.postprocess() kwargs for a raw_settings.get()-shaped dict (any/all values None ->
  today's hardcoded defaults: camera white balance, everything else LibRaw's own default)."""
  settings = settings or {}
  kwargs = {"output_bps": 8}
  wb_mode = settings.get("raw_wb_mode")
  if wb_mode == "auto":
    kwargs["use_auto_wb"] = True
  elif wb_mode == "manual" and settings.get("raw_wb_r") is not None:
    r, g, b = settings["raw_wb_r"], settings["raw_wb_g"], settings["raw_wb_b"]
    kwargs["user_wb"] = [r, g, b, g]   # LibRaw's 4th multiplier is the 2nd green (G2); reuse G
  else:
    kwargs["use_camera_wb"] = True   # 'camera', or no mode set at all
  if settings.get("raw_bright") is not None:
    kwargs["bright"] = settings["raw_bright"]
  if settings.get("raw_highlight") is not None:
    kwargs["highlight_mode"] = settings["raw_highlight"]
  if settings.get("raw_exposure") is not None:
    # Ticket 109: verified empirically that exp_shift is almost invisible unless LibRaw's own
    # auto-brightness is disabled for this render -- it otherwise renormalizes the output back
    # toward roughly the same overall brightness regardless of exp_shift. Only disabled here, so
    # every file that hasn't touched exposure renders exactly as before this ticket.
    kwargs["exp_shift"] = settings["raw_exposure"]
    kwargs["no_auto_bright"] = True
  return kwargs


def _lift_shadows(rgb, amount):
  """Post-decode shadow lift (ticket 109): v' = v + amount*(1-v)^2 per channel, applied to an
  8-bit RGB array. Not a native LibRaw parameter on either rawpy or LibRaw-Wasm -- this exact
  formula is also what photoapp/static/rawTuning.js applies client-side, so the two agree.
  Verified empirically monotonic (no tone reversal) and highlight-neutral only for amount <= 0.5
  (raw_settings.SHADOW_RANGE enforces that bound before a value ever reaches here)."""
  v = rgb.astype(np.float64) / 255.0
  v = np.clip(v + amount * (1.0 - v) ** 2, 0.0, 1.0)
  return np.round(v * 255.0).astype(np.uint8)


def render(path, settings=None, half_size=False):
  """A demosaiced render applying settings (a raw_settings.get()-shaped dict, or None for
  today's hardcoded defaults), or None if LibRaw can't decode this file. half_size: the
  raw_render job queue's fallback for a RAW with no usable embedded preview at all -- faster,
  and full resolution isn't needed for Thumb/Small/Medium anyway (Huge still wants full size,
  see thumbs.py, so callers that might be asked for Huge should not pass half_size)."""
  try:
    with rawpy.imread(path) as raw:
      rgb = raw.postprocess(half_size=half_size, **_postprocess_kwargs(settings))
  except (rawpy.LibRawError, OSError, ValueError) as e:
    logging.vlog(3, "%s: LibRaw render failed: %s", path, e)
    return None
  shadow = (settings or {}).get("raw_shadow")
  if shadow is not None:
    rgb = _lift_shadows(rgb, shadow)
  img = Image.fromarray(rgb)
  logging.vlog(7, "%s: LibRaw render (%dx%d)%s", path, *img.size,
              "" if half_size else " (full size)")
  return img
