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

from photoapp import crop as crop_lib
from photoapp import fileinfo
from photoapp import paths
from photoapp import previews
from photoapp import raw_settings

# Smallest to largest. Huge is the full size of the source.
SIZES = ("Thumb", "Small", "Medium", "Huge")
LONG_EDGE = {"Thumb": 300, "Small": 1000, "Medium": 2000, "Huge": None}
JPEG_QUALITY = 85
# Ticket 115/116: these sizes are rendered cropped when a file has a crop; Medium/Huge stay
# full-frame and the client shades the cropped-out area instead.
CROPPED_SIZES = ("Thumb", "Small")


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


def _apply_crop(img, crop):
  """Crop img (RGB PIL image) to crop's normalized rectangle, if it has one."""
  if crop_lib.is_default(crop):
    return img
  left, top, box_w, box_h = crop_lib.pixel_box(crop, *img.size)
  return img.crop((left, top, left + box_w, top + box_h))


def _open(source, settings=None, crop=None):
  """Decode source to an RGB PIL image, cropped to crop (ticket 115) if it has one.

  RAW files normally use their embedded preview -- fast, no demosaic (Pillow would "succeed" on
  a DNG but only return its tiny IFD0 thumbnail, which is why this doesn't just use Pillow
  directly). But that shortcut only applies while settings is at default (ticket 085): once
  settings has been tuned away from default for this file, every size demosaics instead, so the
  tuning is actually visible (docs/design/thumbnails.md). Raises Unsupported if nothing decodes.
  """
  if fileinfo.is_raw(source):
    if settings and not raw_settings.is_default(settings):
      img = previews.render(source, settings)
      if img is None:
        raise Unsupported(f"{source}: LibRaw could not decode with the given settings")
      return _apply_crop(img.convert("RGB"), crop)
    preview = previews.embedded_preview(source)
    if preview is None:
      raise Unsupported(f"{source}: no usable embedded preview")
    return _apply_crop(preview.convert("RGB"), crop)
  try:
    with Image.open(source) as img:
      return _apply_crop(ImageOps.exif_transpose(img).convert("RGB"), crop)
  except Exception as e:  # Pillow raises many kinds of errors
    raise Unsupported(f"{source}: {e}")


def save(img, dest, long_edge):
  """Write img as a JPEG at most long_edge on its long side (None: full)."""
  if long_edge:
    img = img.copy()
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


def render(source, dest, long_edge, settings=None, crop=None):
  """Write a JPEG of source, at most long_edge on its long side, to dest.

  settings: the source file's raw_settings.get()-shaped dict (ticket 085), or None -- ignored
  for a non-RAW source. crop: the source file's crop.get()-shaped dict (ticket 115), or None.
  Never upscales. long_edge None keeps the full size. The write is atomic. Raises Unsupported if
  source cannot be decoded (a RAW file without a usable embedded preview, at default settings,
  needs render_raw_sizes()).
  """
  return save(_open(source, settings, crop), dest, long_edge)


def render_raw_sizes(pictures_dir, thumbs_dir, file_path, settings=None, crop=None):
  """Demosaic a RAW and write Thumb/Small (cropped) and Medium (full). Returns {size: path}.

  Slow (about a second); used by the background job when a RAW has no usable embedded preview
  (regardless of settings -- there is no faster path available for this file at all). Half size:
  plenty for these three, and this path never populates Huge (a RAW with no usable embedded
  preview and no tuned settings has no fast way to get a full-size Huge either -- left to the
  next on-demand request, same as before this ticket). Returns {} if the file cannot be decoded.
  Ticket 115: the demosaic is of the full frame; Thumb/Small are cropped from it, Medium is not.
  """
  img = previews.render(os.path.join(pictures_dir, file_path), settings, half_size=True)
  if img is None:
    logging.vlog(3, "%s: LibRaw could not decode this RAW", file_path)
    return {}
  made = {}
  for size in SIZES:
    if LONG_EDGE[size] is None:
      continue
    source = _apply_crop(img, crop) if size in CROPPED_SIZES else img
    made[size] = save(source, thumb_path(thumbs_dir, size, file_path), LONG_EDGE[size])
  logging.vlog(5, "%s: full-decode rendered %s", file_path, ", ".join(made))
  return made


def clear(thumbs_dir, conn, file_id, file_path):
  """Delete every cached thumbnail (file on disk + thumbs row) for this file (ticket 079): the
  fix for a broken/corrupt cached thumbnail, since every render path in this codebase
  deliberately never overwrites an existing one. The next request for any size regenerates it
  from scratch through the normal path (make/ensure, or the RAW job queue) -- clearing is the
  whole fix; no rendering happens here. Returns the sizes that had something to clear."""
  cleared = []
  for size in SIZES:
    path = thumb_path(thumbs_dir, size, file_path)
    existed = os.path.exists(path)
    if existed:
      os.remove(path)
    deleted = conn.execute(
        "DELETE FROM thumbs WHERE file_id = ? AND size = ?", (file_id, size)).rowcount
    if existed or deleted:
      cleared.append(size)
  conn.commit()
  logging.info("%s: cleared cached thumbnails %s for a forced re-render", file_path, cleared)
  return cleared


def _record(conn, file_id, size, path, source):
  conn.execute(
      "INSERT INTO thumbs (file_id, size, path, bytesize, source) "
      "VALUES (?, ?, ?, ?, ?) ON CONFLICT(file_id, size) DO UPDATE SET "
      "path = excluded.path, bytesize = excluded.bytesize, "
      "source = excluded.source",
      (file_id, size, path, os.path.getsize(path), source))


def make(pictures_dir, thumbs_dir, file_path, size, settings=None, crop=None):
  """Return (path, source) of a thumbnail of exactly this size, making it if
  needed, or None if that is not possible without a RAW converter.

  Order: existing file in the tree; downscale from a larger existing size;
  render from the original with Pillow. Touches no database, so it can run
  outside any lock. settings (ticket 085): file_path's raw_settings.get()-shaped dict, threaded
  through to render()/_open() -- only matters for a RAW source (fileinfo.is_raw), and only when
  rendering from the *original*: a "downscale from a larger cached size" source is already a
  plain JPEG the settings were baked into (or not) when that larger size was itself produced, so
  it needs no further settings-awareness here. Callers that change a file's settings are
  responsible for clearing every cached size first (thumbs.clear) so a stale, differently-tuned
  cache is never downscaled from by mistake.

  Ticket 115: crop is file_path's crop.get()-shaped dict. Thumb/Small (CROPPED_SIZES) are
  rendered cropped -- always straight from the original, since every larger cached size is
  full-frame and must not be downscaled from. Medium/Huge ignore crop (full frame; the loupe
  shades the cropped-out area).

  Ticket 093: a settings-tuned RAW demosaics through previews.render() (slow, ~1s) rather than
  the fast embedded-preview path, and would otherwise pay that cost again for every distinct size
  requested (grid Thumb, then loupe Medium, then a Huge zoom -- up to 4 full demosaics for one
  photo). When about to demosaic such a file and nothing larger is cached yet, render Huge once
  first -- written straight into the thumbs tree, same as any other size -- then fall through to
  the existing downscale-from-larger-cached-size path below to produce the size actually asked
  for. A later request for a different size finds that Huge file on disk (lookup() is disk-based)
  and only pays a cheap resize. The bonus Huge isn't recorded in the `thumbs` table here (make()
  stays DB-free); it self-heals into the table the same way any other on-disk-but-unrecorded
  thumbnail does, via index_existing() on the next scan or background populate pass.
  """
  path = lookup(thumbs_dir, size, file_path)
  if path:
    logging.vlog(7, "%s: %s already cached", file_path, size)
    return path, "existing"
  if size in CROPPED_SIZES and not crop_lib.is_default(crop):
    original = os.path.join(pictures_dir, file_path)
    dest = thumb_path(thumbs_dir, size, file_path)
    try:
      render(original, dest, LONG_EDGE[size], settings=settings, crop=crop)
    except Unsupported as e:
      logging.vlog(3, "cannot render cropped %s from %s: %s", size, original, e)
      return None
    logging.vlog(5, "%s: rendered cropped %s from %s", file_path, size, original)
    return dest, "pillow"
  dest = thumb_path(thumbs_dir, size, file_path)
  sources = [lookup(thumbs_dir, s, file_path)
             for s in SIZES[SIZES.index(size) + 1:]]
  sources = [s for s in sources if s]
  original = os.path.join(pictures_dir, file_path)
  if (not sources and size != "Huge" and fileinfo.is_raw(original)
      and settings and not raw_settings.is_default(settings)):
    huge_dest = thumb_path(thumbs_dir, "Huge", file_path)
    try:
      render(original, huge_dest, LONG_EDGE["Huge"], settings=settings)
      sources = [huge_dest]
      logging.vlog(5, "%s: rendered Huge once to also satisfy %s (ticket 093)",
                   file_path, size)
    except Unsupported as e:
      logging.vlog(3, "cannot render Huge from %s: %s", original, e)
  sources.append(original)
  for source in sources:
    try:
      render(source, dest, LONG_EDGE[size], settings=settings)
    except Unsupported as e:
      logging.vlog(3, "cannot render %s from %s: %s", size, source, e)
      continue
    logging.vlog(5, "%s: rendered %s from %s", file_path, size, source)
    return dest, "pillow"
  return None


def ensure(conn, pictures_dir, thumbs_dir, file_id, file_path, size):
  """make() and record the result in the thumbs table. Returns the path.

  Looks up file_id's per-file RAW settings (ticket 085) itself, since it already has conn --
  every caller through ensure() (ticket 089's export, thumb_populate.py's background populator)
  gets settings-awareness for free, unlike bare make(), which stays pure/DB-free for callers that
  already have the settings dict in hand (the on-demand /img/{size} handler, which fetches the
  file row and settings together in one round trip before calling make() directly).
  """
  settings = raw_settings.get(conn, file_id)
  file_crop = crop_lib.get(conn, file_id)
  made = make(pictures_dir, thumbs_dir, file_path, size, settings=settings,
              crop=file_crop)
  if made is None:
    return None
  path, source = made
  _record(conn, file_id, size, path, source)
  return path


def record(conn, file_id, size, path, source):
  _record(conn, file_id, size, path, source)


def _dirname(path):
  return path.rpartition("/")[0] or "."


def index_existing(conn, thumbs_dir, rel_dirs):
  """Record thumbnails that already exist for files directly in rel_dirs.

  Lists each thumbnail directory once instead of statting every file.
  """
  found = 0
  for rel_dir in sorted(set(rel_dirs)):
    files = paths.files_in_dir(conn, rel_dir, "id, path")
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
          path = os.path.join(directory, name)
          _record(conn, f["id"], size, path, "existing")
          found += 1
          logging.vlog(7, "indexed existing %s: %s", size, path)
  conn.commit()
  if found:
    logging.vlog(1, "index_existing: %d thumbnail(s) indexed across %d directories",
                 found, len(set(rel_dirs)))
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
