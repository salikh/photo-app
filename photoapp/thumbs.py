"""Thumbnail lookup, generation and per-size accounting.

Layout (see docs/reqs/thumbs-layout.md): <thumbs_dir>/<Size>/<relative path
of the source with the extension mapping applied>. All path logic lives here
so a later move to content-addressed storage only touches this module.
"""

import os
import tempfile

from absl import logging
from PIL import Image
from PIL import ImageOps

# Smallest to largest. Huge is the full size of the source.
SIZES = ("Thumb", "Small", "Medium", "Huge")
LONG_EDGE = {"Thumb": 300, "Small": 1000, "Medium": 2000, "Huge": None}
JPEG_QUALITY = 85


class Unsupported(Exception):
  """The source cannot be decoded by Pillow (e.g. a RAW file)."""


def thumb_relpath(file_path):
  """Relative thumbnail path (with '/' separators) for a source path.

  NAME.jpg / NAME.JPG -> NAME.jpg ; anything else keeps its extension in
  front: NAME.DNG -> NAME.DNG.jpg, NAME.png -> NAME.png.jpg.
  """
  stem, ext = os.path.splitext(file_path)
  if ext.lower() == ".jpg":
    return stem + ".jpg"
  return file_path + ".jpg"


def thumb_path(thumbs_dir, size, file_path):
  return os.path.join(thumbs_dir, size, thumb_relpath(file_path))


def lookup(thumbs_dir, size, file_path):
  """Existing thumbnail path for exactly this size, or None."""
  path = thumb_path(thumbs_dir, size, file_path)
  return path if os.path.isfile(path) else None


def best_available(thumbs_dir, size, file_path):
  """(size, path) of the wanted size, else the next larger existing one."""
  for candidate in SIZES[SIZES.index(size):]:
    path = lookup(thumbs_dir, candidate, file_path)
    if path:
      return candidate, path
  return None


def render(source, dest, long_edge):
  """Write a JPEG of source, at most long_edge on its long side, to dest.

  Never upscales. long_edge None keeps the full size. The write is atomic.
  Raises Unsupported if Pillow cannot decode source.
  """
  try:
    with Image.open(source) as img:
      img = ImageOps.exif_transpose(img)
      img = img.convert("RGB")
  except Exception as e:  # Pillow raises many kinds of errors
    raise Unsupported(f"{source}: {e}")
  if long_edge:
    img.thumbnail((long_edge, long_edge), Image.LANCZOS)
  os.makedirs(os.path.dirname(dest), exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".tmp")
  try:
    with os.fdopen(fd, "wb") as f:
      img.save(f, "JPEG", quality=JPEG_QUALITY)
    os.replace(tmp, dest)
  except BaseException:
    if os.path.exists(tmp):
      os.unlink(tmp)
    raise
  return dest


def _record(conn, file_id, size, path, source):
  conn.execute(
      "INSERT INTO thumbs (file_id, size, path, bytesize, source) "
      "VALUES (?, ?, ?, ?, ?) ON CONFLICT(file_id, size) DO UPDATE SET "
      "path = excluded.path, bytesize = excluded.bytesize, "
      "source = excluded.source",
      (file_id, size, path, os.path.getsize(path), source))


def ensure(conn, pictures_dir, thumbs_dir, file_id, file_path, size):
  """Return the path of a thumbnail of exactly this size, making it if
  needed, or None if that is not possible without a RAW converter.

  Order: existing file in the tree; downscale from a larger existing size;
  render from the original with Pillow.
  """
  path = lookup(thumbs_dir, size, file_path)
  if path:
    _record(conn, file_id, size, path, "existing")
    return path
  dest = thumb_path(thumbs_dir, size, file_path)
  sources = [lookup(thumbs_dir, s, file_path)
             for s in SIZES[SIZES.index(size) + 1:]]
  sources = [s for s in sources if s]
  sources.append(os.path.join(pictures_dir, file_path))
  for source in sources:
    try:
      render(source, dest, LONG_EDGE[size])
    except Unsupported as e:
      logging.vlog(2, "cannot render %s from %s: %s", size, source, e)
      continue
    _record(conn, file_id, size, dest, "pillow")
    return dest
  return None


def _dirname(path):
  return path.rpartition("/")[0] or "."


def index_existing(conn, thumbs_dir, rel_dirs):
  """Record thumbnails that already exist for files directly in rel_dirs.

  Lists each thumbnail directory once instead of statting every file.
  """
  found = 0
  all_files = None
  for rel_dir in sorted(set(rel_dirs)):
    if all_files is None:
      all_files = {}
      for f in conn.execute("SELECT id, path FROM files"):
        all_files.setdefault(_dirname(f["path"]), []).append(f)
    files = all_files.get(rel_dir, [])
    if not files:
      continue
    for size in SIZES:
      directory = os.path.join(thumbs_dir, size, rel_dir)
      try:
        names = set(os.listdir(directory))
      except OSError:
        continue
      for f in files:
        name = os.path.basename(thumb_relpath(f["path"]))
        if name in names:
          _record(conn, f["id"], size, os.path.join(directory, name),
                  "existing")
          found += 1
  conn.commit()
  return found


def usage(conn):
  """{size: {'files': n, 'bytes': total}} for every size, zeros included."""
  result = {s: {"files": 0, "bytes": 0} for s in SIZES}
  for r in conn.execute(
      "SELECT size, COUNT(*) n, COALESCE(SUM(bytesize), 0) b "
      "FROM thumbs GROUP BY size"):
    result[r["size"]] = {"files": r["n"], "bytes": r["b"]}
  return result


def lacking(conn):
  """{size: number of non-missing files without a recorded thumbnail}."""
  return {
      size: conn.execute(
          "SELECT COUNT(*) FROM files f WHERE f.missing = 0 AND NOT EXISTS "
          "(SELECT 1 FROM thumbs t WHERE t.file_id = f.id AND t.size = ?)",
          (size,)).fetchone()[0]
      for size in SIZES
  }
