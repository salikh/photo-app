"""Non-destructive per-file rotation (ticket 129).

One nullable `files.rotation` column holds a clockwise-independent rotation in
degrees **counter-clockwise**, normalized to 90/180/270; `NULL` and `0` both
mean "not rotated", and `0` is normalized back to `NULL` (mirroring crop.py's
whole-frame -> NULL). The rotation is applied as the *final* render transform
(photoapp/thumbs.py's `_open`), after the source's metadata orientation (EXIF
transpose, or LibRaw's flip for a RAW) and after the crop, so it never mixes
with the orientation stored inside the file's metadata.

Like the crop and the per-file RAW settings it is a rendering property of one
physical file, so it lives on `files` (not on `photos` like the rating) and is
never written to XMP sidecars.
"""

COLUMN = "rotation"
ANGLES = (0, 90, 180, 270)   # counter-clockwise


class RotationError(Exception):
  """A rotation that cannot be stored; message is safe to show."""


def get(conn, file_id):
  """The file's stored rotation in degrees counter-clockwise, 0 if none."""
  row = conn.execute(
      f"SELECT {COLUMN} FROM files WHERE id = ?", (file_id,)).fetchone()
  return (row[COLUMN] or 0) if row else 0


def is_default(rotation):
  """True if rotation (degrees, or None) means "not rotated"."""
  return not rotation


def normalize(value):
  """Degrees -> 0/90/180/270; 0 also becomes None (no rotation)."""
  angle = int(value) % 360
  return None if angle == 0 else angle


def validate(value):
  """Raises RotationError unless value is a whole multiple of 90 degrees."""
  if value is None:
    return
  if not isinstance(value, int) or isinstance(value, bool) or value % 90 != 0:
    raise RotationError("rotation must be a whole multiple of 90 degrees")


def apply(img, rotation):
  """Rotate a PIL image by rotation degrees counter-clockwise, expanding the canvas.

  Positive is counter-clockwise, matching the stored column. No-op when not rotated.
  """
  if is_default(rotation):
    return img
  return img.rotate(rotation, expand=True)


def set(conn, file_id, rotation):
  """Replace file_id's rotation; 0 (or None) clears it.

  Raises RotationError for a non-multiple of 90 or an unknown file.
  """
  validate(rotation)
  if conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None:
    raise RotationError(f"no such file: {file_id}")
  conn.execute(
      f"UPDATE files SET {COLUMN} = ?, thumb_rev = thumb_rev + 1 WHERE id = ?",
      (normalize(rotation), file_id))
  conn.commit()
