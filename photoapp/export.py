"""Export action (ticket 089): a Huge-equivalent JPEG of chosen Photos, written to a folder
the user confirms, outside pictures_dir/thumbs_dir/state_dir. Runs as background jobs (one per
file, via photoapp.jobs.JobQueue's kind='export') so a big export never blocks an HTTP request.
"""

import os
import shutil

from photoapp import thumbs


class ExportError(Exception):
  """A request that cannot be carried out; message is safe to show."""


def default_target(pictures_dir, rel_dir):
  """Default export path for rel_dir: pictures_dir's own parent, with pictures_dir's basename
  swapped for "Exported", then rel_dir appended -- e.g. /zoo/Pictures/2020/trip ->
  /zoo/Exported/2020/trip. Always defined (never "no common folder"): the directory the export
  was started from is always a valid anchor, whether this is a recursive view or an arbitrary
  selection within it -- both are always drawn from rel_dir's own subtree by construction, so
  there's no need for the more general "tightest common ancestor of an arbitrary id list"
  computation the ticket flagged as an open question -- rel_dir already answers it."""
  parent = os.path.dirname(os.path.normpath(pictures_dir))
  base = os.path.join(parent, "Exported")
  return base if rel_dir in ("", ".") else os.path.join(base, rel_dir)


def validate_target(settings, target):
  """Refuse a target that resolves inside pictures_dir/thumbs_dir/state_dir -- exporting into
  the library or the app's own cache is never what this feature is for and risks corrupting
  either. Any other absolute path is trusted as-is (single-user local tool). Raises ExportError."""
  if not target or not os.path.isabs(target):
    raise ExportError("give an absolute target folder")
  target_real = os.path.realpath(target)
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
  """Full destination file path for file_path, mirroring its position under rel_dir."""
  name = os.path.basename(thumbs.thumb_relpath(file_path))
  sub = within_dir(rel_dir, file_path)
  return os.path.join(target_root, sub, name) if sub else os.path.join(target_root, name)


def export_file(conn, settings, file_id, file_path, dest):
  """Render a Huge-equivalent JPEG of one file (reusing the same thumbs.ensure path every
  on-demand /img/Huge request already goes through for a RAW file -- see docs/design/
  thumbnails.md) and copy it to dest, creating parent directories as needed. Raises ExportError
  if no preview could be produced (e.g. a RAW with no usable embedded preview and nothing Pillow
  can decode -- the same case that would otherwise defer to the slow raw_render job queue; export
  reports it rather than blocking a whole batch on a full demosaic)."""
  path = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, file_id, file_path, "Huge")
  if path is None:
    raise ExportError(f"no preview available for {file_path}")
  os.makedirs(os.path.dirname(dest), exist_ok=True)
  shutil.copyfile(path, dest)
  return dest
