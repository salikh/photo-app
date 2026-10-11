"""Non-destructive edits for non-RAW stills (epic 222, tickets 226/227).

One nullable JSON column `files.edits` holds the canonical edit document (NULL = unedited). The
original file is never modified; the edits are applied by photoapp/thumbs.py after the downscale
and baked into every cached size, so showing an image computes nothing. Like crop, rotation and the
RAW settings it is a rendering property of one physical file (lives on `files`, never in XMP).

Document keys, all optional (a key at its identity value is dropped by normalize()):
  auto_levels  true              stretch each channel to its own 0.5%/99.5% percentiles first
  levels       {black, gamma, white}   master levels, black 0-254, gamma 0.2-5, white 1-255
  levels_r/_g/_b  same shape           per-channel levels, applied after the master
  contrast     0.5-2.0           slope about mid-grey, 1.0 = identity
  brightness   -100..100         additive, percent of full scale
  saturation   0-2               1.0 = identity
  mono         {weights:[r,g,b]} monochrome, weights each -0.5..1.5; `true` = Rec.601 luma
  sharpen      {amount, radius}  unsharp mask: amount 0-300 (%), radius 0.2-40 px *at 6000 px long
                                 edge* (scaled to the output size, so every cached size looks alike)

Everything pointwise (levels, contrast, brightness, per-channel) is composed into one 256-entry
lookup table per channel by compile_lut(), so its cost does not grow with the number of sliders:
~5 ms at 2000 px, ~50 ms at 24 MP. Unsharp is the only expensive step (~1 s at 24 MP, 100 ms at
2000 px), which is why it runs after the downscale. Order: auto levels -> LUT -> saturation ->
monochrome -> sharpen.
"""

import json

from PIL import ImageEnhance, ImageFilter, ImageOps

COLUMN = "edits"
LEVEL_KEYS = ("levels", "levels_r", "levels_g", "levels_b")
KEYS = LEVEL_KEYS + ("auto_levels", "contrast", "brightness", "saturation", "mono", "sharpen")
LUMA = [0.299, 0.587, 0.114]
SHARPEN_REFERENCE_EDGE = 6000
SHARPEN_THRESHOLD = 3


class EditsError(Exception):
  """An edit document that cannot be stored; message is safe to show."""


def _number(doc, key, lo, hi, what=None):
  value = doc.get(key)
  if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
    raise EditsError(f"{what or key} must be a number from {lo} to {hi}")
  return float(value)


def _levels(value, key):
  if not isinstance(value, dict) or frozenset(value) - {"black", "gamma", "white"}:
    raise EditsError(f"{key} must be an object with black, gamma and white")
  out = {"black": int(_number(value, "black", 0, 254, f"{key}.black")) if "black" in value else 0,
         "gamma": _number(value, "gamma", 0.2, 5, f"{key}.gamma") if "gamma" in value else 1.0,
         "white": int(_number(value, "white", 1, 255, f"{key}.white")) if "white" in value else 255}
  if out["black"] >= out["white"]:
    raise EditsError(f"{key}.black must be below {key}.white")
  return out


def normalize(doc):
  """The canonical form of doc with every identity value dropped, or None if nothing is left.
  Raises EditsError for an unknown key or an out-of-range / malformed value."""
  if doc is None:
    return None
  if not isinstance(doc, dict):
    raise EditsError("edits must be an object")
  unknown = frozenset(doc) - frozenset(KEYS)
  if unknown:
    raise EditsError(f"unknown edit: {', '.join(sorted(unknown))}")
  out = {}
  for key in LEVEL_KEYS:
    if doc.get(key) is not None:
      levels = _levels(doc[key], key)
      if levels != {"black": 0, "gamma": 1.0, "white": 255}:
        out[key] = levels
  if doc.get("auto_levels"):
    out["auto_levels"] = True
  for key, lo, hi, identity in (("contrast", 0.5, 2.0, 1.0), ("brightness", -100, 100, 0.0),
                                ("saturation", 0.0, 2.0, 1.0)):
    if doc.get(key) is not None:
      value = _number(doc, key, lo, hi)
      if value != identity:
        out[key] = value
  mono = doc.get("mono")
  if mono:
    weights = LUMA if mono is True else mono.get("weights") if isinstance(mono, dict) else None
    if (not isinstance(weights, list) or len(weights) != 3
        or any(isinstance(w, bool) or not isinstance(w, (int, float)) or not -0.5 <= w <= 1.5
               for w in weights)):
      raise EditsError("mono must be true or {weights: [r, g, b]} with each weight -0.5 to 1.5")
    out["mono"] = {"weights": [float(w) for w in weights]}
  sharpen = doc.get("sharpen")
  if sharpen:
    if not isinstance(sharpen, dict) or frozenset(sharpen) - {"amount", "radius"}:
      raise EditsError("sharpen must be an object with amount and radius")
    amount = _number(sharpen, "amount", 0, 300, "sharpen.amount") if "amount" in sharpen else 0.0
    radius = _number(sharpen, "radius", 0.2, 40, "sharpen.radius") if "radius" in sharpen else 2.0
    if amount > 0:
      out["sharpen"] = {"amount": amount, "radius": radius}
  return out or None


def is_default(doc):
  return not doc


def get(conn, file_id):
  """The file's stored edit document (a dict), or None if unedited."""
  row = conn.execute(f"SELECT {COLUMN} FROM files WHERE id = ?", (file_id,)).fetchone()
  if row is None or not row[COLUMN]:
    return None
  try:
    return json.loads(row[COLUMN])
  except ValueError:
    return None


def set(conn, file_id, doc):
  """Replace file_id's edits wholesale (None or an all-identity document clears them) and bump
  thumb_rev. Returns the stored canonical document. The caller clears the cached thumbnails."""
  doc = normalize(doc)
  if conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None:
    raise EditsError(f"no such file: {file_id}")
  conn.execute(f"UPDATE files SET {COLUMN} = ?, thumb_rev = thumb_rev + 1 WHERE id = ?",
               (json.dumps(doc, sort_keys=True) if doc else None, file_id))
  conn.commit()
  return doc


def _curve(levels, x):
  v = (x - levels["black"]) / float(levels["white"] - levels["black"])
  v = min(1.0, max(0.0, v))
  return v ** (1.0 / levels["gamma"]) if levels["gamma"] != 1.0 else v


def compile_lut(doc):
  """(lut_r, lut_g, lut_b): three 256-entry tables composing master levels, the channel's own
  levels, contrast and brightness. The single definition of the pointwise maths."""
  doc = doc or {}
  luts = []
  for channel in "rgb":
    table = []
    for x in range(256):
      v = x / 255.0
      for key in ("levels", f"levels_{channel}"):
        if key in doc:
          v = _curve(doc[key], v * 255.0)
      v = (v - 0.5) * doc.get("contrast", 1.0) + 0.5
      v += doc.get("brightness", 0.0) / 100.0
      table.append(int(round(min(1.0, max(0.0, v)) * 255)))
    luts.append(table)
  return tuple(luts)


def _has_lut_edits(doc):
  return any(k in doc for k in LEVEL_KEYS + ("contrast", "brightness"))


def apply(img, doc):
  """img (an RGB PIL image, already at its output size) with the edit document applied. Returns img
  itself for an unedited file; never modifies img in place."""
  if not doc:
    return img
  if doc.get("auto_levels"):
    img = ImageOps.autocontrast(img, cutoff=0.5)
  if _has_lut_edits(doc):
    lut_r, lut_g, lut_b = compile_lut(doc)
    img = img.point(lut_r + lut_g + lut_b)
  if "saturation" in doc:
    img = ImageEnhance.Color(img).enhance(doc["saturation"])
  if "mono" in doc:
    w = doc["mono"]["weights"]
    img = img.convert("RGB", tuple(w) + (0,) + tuple(w) + (0,) + tuple(w) + (0,))
  if "sharpen" in doc:
    sharpen = doc["sharpen"]
    radius = max(0.3, sharpen["radius"] * max(img.size) / SHARPEN_REFERENCE_EDGE)
    img = img.filter(ImageFilter.UnsharpMask(radius, int(round(sharpen["amount"])),
                                             SHARPEN_THRESHOLD))
  return img
