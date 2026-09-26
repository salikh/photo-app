"""Export action (ticket 089): a Huge-equivalent JPEG of chosen Photos, written by default inside
pictures_dir's own Exported subfolder since ticket 096 (a custom target outside
pictures_dir/thumbs_dir/state_dir is still accepted). Runs as background jobs (one per file, via
photoapp.jobs.JobQueue's kind='export') so a big export never blocks an HTTP request. Ticket 099
imports and links each export into the database the moment its job finishes.
"""

import os
import shutil

from photoapp import crop as crop_lib
from photoapp import grouping
from photoapp import paths
from photoapp import raw_settings
from photoapp import scan
from photoapp import thumbs


class ExportError(Exception):
  """A request that cannot be carried out; message is safe to show."""


EXPORT_SUBDIR = "Exported"


def default_target(pictures_dir, rel_dir):
  """Default export path for rel_dir: an "Exported" subfolder of pictures_dir itself, then
  rel_dir appended -- e.g. /zoo/Pictures/2020/trip -> /zoo/Pictures/Exported/2020/trip (ticket 096:
  moved inside the library, from the original ticket 089 sibling-of-pictures_dir location, so an
  export is scanned and browseable like any other photo). Always defined (never "no common
  folder"): the directory the export was started from is always a valid anchor, whether this is a
  recursive view or an arbitrary selection within it -- both are always drawn from rel_dir's own
  subtree by construction, so there's no need for the more general "tightest common ancestor of an
  arbitrary id list" computation the ticket flagged as an open question -- rel_dir already
  answers it."""
  base = os.path.join(os.path.normpath(pictures_dir), EXPORT_SUBDIR)
  return base if rel_dir in ("", ".") else os.path.join(base, rel_dir)


def validate_target(settings, target):
  """Refuse a target that resolves inside pictures_dir/thumbs_dir/state_dir, with one exception:
  pictures_dir's own EXPORT_SUBDIR (default_target's result, or anywhere under it) is exactly what
  this feature is for since ticket 096 and is explicitly allowed. Any other absolute path is
  trusted as-is (single-user local tool). Raises ExportError."""
  if not target or not os.path.isabs(target):
    raise ExportError("give an absolute target folder")
  target_real = os.path.realpath(target)
  exported_real = os.path.realpath(os.path.join(settings.pictures_dir, EXPORT_SUBDIR))
  if target_real == exported_real or target_real.startswith(exported_real + os.sep):
    return
  for name, guarded in (("the library", settings.pictures_dir),
                        ("the thumbnail cache", settings.thumbs_dir),
                        ("the app's state directory", settings.state_dir)):
    guarded_real = os.path.realpath(guarded)
    if target_real == guarded_real or target_real.startswith(guarded_real + os.sep):
      raise ExportError(f"the export target cannot be inside {name} ({guarded})")


def within_dir(rel_dir, file_path):
  """The directory portion of file_path relative to rel_dir (''  if file_path is directly in
  rel_dir) -- the piece mirrored under the target so two Photos with the same filename from
  different subfolders (recursive view, ticket 086; cross-folder selection, ticket 088) don't
  collide, the same reason trash.py mirrors a whole rel_path under .trash/."""
  prefix = "" if rel_dir in ("", ".") else rel_dir + "/"
  sub = file_path[len(prefix):] if file_path.startswith(prefix) else file_path
  return sub.rsplit("/", 1)[0] if "/" in sub else ""


def dest_path(target_root, rel_dir, file_path):
  """Full destination file path for file_path, mirroring its position under rel_dir. Just the
  mirroring -- see resolve_dest_path (ticket 098) for basename-collision disambiguation."""
  name = os.path.basename(thumbs.thumb_relpath(file_path))
  sub = within_dir(rel_dir, file_path)
  return os.path.join(target_root, sub, name) if sub else os.path.join(target_root, name)


def _is_reusable_export(conn, pictures_dir, path, file_id):
  """True if path already holds a files row that is itself an earlier export of file_id (its
  exported_from_file_id already points there) -- safe to overwrite in place rather than
  disambiguate as a fresh collision. Only meaningful for a path inside pictures_dir (the only
  files this app ever scans into the database); a path outside it is never "reusable" this way,
  just checked for plain existence by the caller."""
  pictures_real = os.path.normpath(pictures_dir)
  path_real = os.path.normpath(path)
  if path_real != pictures_real and not path_real.startswith(pictures_real + os.sep):
    return False
  rel = os.path.relpath(path_real, pictures_real).replace(os.sep, "/")
  row = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = ?", (rel,)).fetchone()
  return row is not None and row["exported_from_file_id"] == file_id


def resolve_dest_path(conn, pictures_dir, candidate, file_id, taken=None):
  """Ticket 098: disambiguate a basename collision at candidate with a numeric suffix
  (name-2.ext, name-3.ext, ...) before the extension, unless the file already there is itself an
  earlier export of this same file_id (then candidate is reused/overwritten in place -- 099's
  re-export case -- not disambiguated). `taken` is the set of destination paths already claimed
  earlier in the same export batch (not yet written to disk, so os.path.exists wouldn't see
  them), also treated as occupied."""
  taken = taken or ()
  root, ext = os.path.splitext(candidate)
  n = 1
  while True:
    path = candidate if n == 1 else f"{root}-{n}{ext}"
    if path not in taken and (not os.path.exists(path)
                              or _is_reusable_export(conn, pictures_dir, path, file_id)):
      return path
    n += 1


def export_file(conn, settings, file_id, file_path, dest):
  """Render a Huge-equivalent JPEG of one file and write it to dest, creating parent directories as
  needed. Raises ExportError if no preview could be produced (e.g. a RAW with no usable embedded
  preview and nothing Pillow can decode -- the same case that would otherwise defer to the slow
  raw_render job queue; export reports it rather than blocking a whole batch on a full demosaic).

  An uncropped file reuses the same thumbs.ensure path every on-demand /img/Huge request already
  goes through and copies the result byte-for-byte (see docs/design/thumbnails.md). A cropped file
  (ticket 125) is a finished, out-of-app artifact where the shaded-out margin makes no sense, so
  the crop is applied for real: the full-frame image is rendered and cropped in one pass, straight
  from the original (never the full-frame, lossy Huge) -- the same pixel rectangle Thumb/Small use.
  """
  file_crop = crop_lib.get(conn, file_id)
  if crop_lib.is_default(file_crop):
    path = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, file_id, file_path,
                         "Huge")
    if path is None:
      raise ExportError(f"no preview available for {file_path}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(path, dest)
    return dest
  original = os.path.join(settings.pictures_dir, file_path)
  os.makedirs(os.path.dirname(dest), exist_ok=True)
  try:
    thumbs.render(original, dest, thumbs.LONG_EDGE["Huge"],
                  settings=raw_settings.get(conn, file_id), crop=file_crop)
  except thumbs.Unsupported:
    raise ExportError(f"no preview available for {file_path}")
  return dest


def link_exported_file(conn, settings, source_file_id, dest):
  """Ticket 099: right after export_file writes dest, import it into the database and cross-
  reference it to its source -- immediately, not left for the next full scan. Only applies when
  dest is inside pictures_dir (098's new default; a custom target outside the library is never
  scanned by this app at all, same as before this ticket, so there's nothing to import). Gives
  the imported file its own Photo via the normal grouping rule (Option B, ticket 097) rather than
  merging it into the source's Photo."""
  pictures_real = os.path.normpath(settings.pictures_dir)
  dest_real = os.path.normpath(dest)
  if dest_real != pictures_real and not dest_real.startswith(pictures_real + os.sep):
    return None
  rel = os.path.relpath(dest_real, pictures_real).replace(os.sep, "/")
  file_id = scan.import_single_file(conn, settings.pictures_dir, rel)
  grouping.regroup(conn, {paths.dirname(rel)})
  conn.execute("UPDATE files SET exported_from_file_id = ? WHERE id = ?",
              (source_file_id, file_id))
  conn.commit()
  return file_id
