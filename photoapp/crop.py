"""Non-destructive crop settings (ticket 115).

Four nullable columns on `files` hold the crop rectangle as normalized [0..1]
fractions of the source image: `crop_x`/`crop_y` are the top-left corner and
`crop_w`/`crop_h` the size, so the same rectangle applies at any resolution.
All four NULL means "no crop" (the whole frame); a rectangle that covers the
whole frame is normalized back to NULL. The crop is non-destructive: the
original file is never modified, only how it is displayed/rendered.

Thumbnails: `Thumb` and `Small` (the grid/filmstrip sizes) are rendered
cropped; `Medium`/`Huge` (the loupe's large/zoomed views) stay full-frame and
the client shades the cropped-out area (ticket 116's answer).
"""

COLUMNS = ("crop_x", "crop_y", "crop_w", "crop_h")
MIN_SIZE = 0.05     # smallest crop side, as a fraction of the frame
_EPS = 1e-6


class CropError(Exception):
  """A crop rectangle that cannot be stored; message is safe to show."""


def get(conn, file_id):
  """{column: value} for file_id, every value None if the file has no crop."""
  row = conn.execute(
      f"SELECT {', '.join(COLUMNS)} FROM files WHERE id = ?", (file_id,)).fetchone()
  return {c: row[c] for c in COLUMNS} if row else {c: None for c in COLUMNS}


def is_default(crop):
  """True if crop (a get()-shaped dict, or None) means the whole frame."""
  return not crop or all(crop.get(c) is None for c in COLUMNS)


def rect(crop):
  """(x, y, w, h) with a default/None crop normalized to the whole frame."""
  if is_default(crop):
    return 0.0, 0.0, 1.0, 1.0
  return (crop["crop_x"], crop["crop_y"], crop["crop_w"], crop["crop_h"])


def pixel_box(crop, width, height):
  """(left, top, box_w, box_h) in pixels for a width x height image."""
  x, y, w, h = rect(crop)
  left = min(width - 1, max(0, round(x * width)))
  top = min(height - 1, max(0, round(y * height)))
  box_w = max(1, min(width - left, round(w * width)))
  box_h = max(1, min(height - top, round(h * height)))
  return left, top, box_w, box_h


def validate(x, y, w, h):
  """Raises CropError unless (x, y, w, h) is a valid in-frame rectangle."""
  if None in (x, y, w, h):
    raise CropError("crop needs x, y, w and h")
  if not (0 <= x < 1 and 0 <= y < 1):
    raise CropError("crop x and y must be in [0, 1)")
  if not (MIN_SIZE <= w <= 1 and MIN_SIZE <= h <= 1):
    raise CropError(f"crop w and h must be in [{MIN_SIZE}, 1]")
  if x + w > 1 + _EPS or y + h > 1 + _EPS:
    raise CropError("crop rectangle must stay inside the image")


def to_columns(x, y, w, h):
  """{column: value} shaped like get()'s return for a rectangle in hand."""
  return {"crop_x": x, "crop_y": y, "crop_w": w, "crop_h": h}


def set(conn, file_id, x, y, w, h):
  """Replace file_id's crop with (x, y, w, h); a whole-frame rectangle clears it.

  Raises CropError for an invalid rectangle or an unknown file.
  """
  validate(x, y, w, h)
  if conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None:
    raise CropError(f"no such file: {file_id}")
  if x <= _EPS and y <= _EPS and w >= 1 - _EPS and h >= 1 - _EPS:
    x = y = w = h = None
  conn.execute(
      "UPDATE files SET crop_x = ?, crop_y = ?, crop_w = ?, crop_h = ?,"
      " thumb_rev = thumb_rev + 1 WHERE id = ?",
      (x, y, w, h, file_id))
  conn.commit()
