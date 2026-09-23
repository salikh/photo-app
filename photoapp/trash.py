"""Moving a rejected Photo's files to <pictures_dir>/.trash/ instead of deleting them (ticket 072).

Every other write this app makes is reversible (a rating) or additive (a sidecar, a thumbnail
cache entry); this is the first one that removes an original from where the rest of the library
expects it. Nothing is ever unlinked: every file (the original, its camera JPG, any tuning, and
every sidecar) is *moved* into a mirror of its own subdirectory structure under
<pictures_dir>/.trash/, so it stays fully recoverable by hand for RETENTION_DAYS before
purge_trash (ticket 081) deletes it for good.

The moved file's `files` row is marked missing=1 (the same state a file gets when a scan finds
it vanished, ticket 010), not deleted, so the Photo's rating/history survives. .trash/ itself is
never scanned (see scan.top_level_steps) precisely so a trashed file's still-correct sidecar
(rating -1) does not get re-discovered as a brand new, live Photo.
"""

import os
import shutil
import time

from absl import logging

TRASH_DIRNAME = ".trash"

# ticket 081: how long a file sits in .trash/ before purge_trash deletes it for good.
RETENTION_DAYS = 7


class TrashError(Exception):
  """A request that cannot be carried out; message is safe to show."""


def trash_dir(pictures_dir):
  return os.path.join(pictures_dir, TRASH_DIRNAME)


def _under_pictures_dir(pictures_dir, rel_path):
  """Absolute path for rel_path, raising if it would resolve outside pictures_dir (defense in
  depth -- mirrors ScanManager.start's check on a requested scan directory)."""
  root = os.path.normpath(pictures_dir)
  abs_path = os.path.normpath(os.path.join(root, rel_path))
  if abs_path != root and not abs_path.startswith(root + os.sep):
    raise TrashError(f"path escapes pictures_dir: {rel_path}")
  return abs_path


def _trash_destination(pictures_dir, rel_path):
  """Where rel_path lands in .trash/, avoiding clobbering an earlier trashed file with the same
  relative path (rare: happens only if the same path is trashed, restored by hand, and trashed
  again without the trash ever being emptied)."""
  dest = _under_pictures_dir(pictures_dir, os.path.join(TRASH_DIRNAME, rel_path))
  if not os.path.exists(dest):
    return dest
  base, ext = os.path.splitext(dest)
  n = 1
  while os.path.exists(f"{base}.{n}{ext}"):
    n += 1
  return f"{base}.{n}{ext}"


def move_to_trash(pictures_dir, rel_path):
  """Move one file into .trash/, mirroring its subdirectory structure. Returns the new path
  (relative to pictures_dir), or None if the source is already gone (nothing to do)."""
  src = _under_pictures_dir(pictures_dir, rel_path)
  if not os.path.isfile(src):
    return None
  dest = _trash_destination(pictures_dir, rel_path)
  os.makedirs(os.path.dirname(dest), exist_ok=True)
  shutil.move(src, dest)
  return os.path.relpath(dest, pictures_dir).replace(os.sep, "/")


def _move_file_and_sidecars(conn, settings, file_id, rel_path):
  """Move one file and its own sidecars into .trash/. Returns (moved, errors) in the same shape
  trash_photo/trash_file return under those keys. Does not mark anything missing or commit --
  shared by both callers, which do that at their own granularity (once per file for trash_file,
  once per Photo's worth of files for trash_photo)."""
  rels = [rel_path] + [r["path"] for r in conn.execute(
      "SELECT path FROM xmp_sidecars WHERE file_id = ?", (file_id,))]
  moved, errors = [], []
  for rel in rels:
    try:
      dest = move_to_trash(settings.pictures_dir, rel)
    except (OSError, TrashError) as e:
      errors.append({"path": rel, "error": str(e)})
      continue
    if dest:
      moved.append({"from": rel, "to": dest})
  return moved, errors


def trash_photo(conn, settings, photo_id):
  """Move every live file of one rejected Photo -- and each file's sidecars -- to .trash/, and
  mark those files missing=1. Raises TrashError if the Photo is not (or no longer) rejected.
  Returns {"photo_id", "moved": [{"from", "to"}], "errors": [{"path", "error"}]}."""
  photo = conn.execute("SELECT rating FROM photos WHERE id = ?", (photo_id,)).fetchone()
  if photo is None:
    raise TrashError(f"no such photo: {photo_id}")
  if photo["rating"] != -1:
    raise TrashError("photo is not rejected")
  files = conn.execute(
      "SELECT id, path FROM files WHERE photo_id = ? AND missing = 0",
      (photo_id,)).fetchall()
  moved, errors = [], []
  for f in files:
    m, e = _move_file_and_sidecars(conn, settings, f["id"], f["path"])
    moved += m
    errors += e
    conn.execute("UPDATE files SET missing = 1 WHERE id = ?", (f["id"],))
  conn.commit()
  logging.info("trash_photo %d: moved %d file(s) to %s, %d error(s)",
               photo_id, len(moved), trash_dir(settings.pictures_dir), len(errors))
  return {"photo_id": photo_id, "moved": moved, "errors": errors}


def trash_file(conn, settings, file_id):
  """Move one live file (any role) -- and its own sidecars -- to .trash/, and mark it
  missing=1 (ticket 082). Unlike trash_photo, **not** rating-gated: a single file can be
  deleted regardless of the Photo's rating -- deleting a whole Photo is what the reject-based
  flow (trash_photo, reached from the Rejected view) is for. Does not touch grouping or the
  representative pointer if this file was it, even though that can leave the Photo looking
  broken until the next scan: fix_representatives only repairs a representative that stopped
  being a *member* of its Photo (unlinked or deleted from the files table), not one that is
  still a member but now missing, so it would not actually help here -- left to the next scan,
  the same precedent trash_photo already set (see docs/design/trash.md). Raises TrashError if
  the file does not exist or is already missing. Returns {"file_id", "moved", "errors"}."""
  row = conn.execute("SELECT path FROM files WHERE id = ? AND missing = 0",
                     (file_id,)).fetchone()
  if row is None:
    raise TrashError(f"no such live file: {file_id}")
  moved, errors = _move_file_and_sidecars(conn, settings, file_id, row["path"])
  conn.execute("UPDATE files SET missing = 1 WHERE id = ?", (file_id,))
  conn.commit()
  logging.info("trash_file %d: moved %d file(s) to %s, %d error(s)",
               file_id, len(moved), trash_dir(settings.pictures_dir), len(errors))
  return {"file_id": file_id, "moved": moved, "errors": errors}


def trash_photos(conn, settings, photo_ids):
  """trash_photo for many Photos; a Photo that is not rejected (or fails) is reported in
  errors, not silently skipped, and does not stop the rest. Returns {"trashed", "errors"}."""
  trashed, errors = [], []
  for pid in dict.fromkeys(photo_ids):
    try:
      trashed.append(trash_photo(conn, settings, pid))
    except TrashError as e:
      errors.append({"photo_id": pid, "error": str(e)})
  return {"trashed": trashed, "errors": errors}


def purge_trash(pictures_dir, retention_days=RETENTION_DAYS, now=None):
  """Permanently delete files under .trash/ that have sat there longer than retention_days,
  then remove any subdirectory that ends up empty (ticket 081). now: injectable for tests
  (ctime cannot be backdated through a normal syscall, unlike mtime, so a test simulates an
  "old" file by moving now forward instead); defaults to time.time().

  "How long has this been in the trash" is the file's ctime (metadata-change time), not mtime:
  moving a file within the same filesystem (move_to_trash always does, since .trash/ is always
  under pictures_dir) is a rename, which leaves mtime untouched but does bump ctime -- confirmed
  empirically before writing this, since assuming otherwise would have silently purged nothing,
  or everything, depending on how old the photos themselves were. Using mtime would have measured
  "when was the photo last edited" instead of "when was it trashed," which for old family photos
  can be decades off in the wrong direction.

  Returns (files_deleted, dirs_removed). Never touches .trash/ itself, even if everything under
  it is gone.
  """
  root = trash_dir(pictures_dir)
  if not os.path.isdir(root):
    return 0, 0
  cutoff = (now if now is not None else time.time()) - retention_days * 86400
  files_deleted = dirs_removed = 0
  # topdown=False: a directory is visited only after every entry under it, so by the time we
  # try to remove it, any subdirectory that emptied out has already removed itself.
  for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
    for name in filenames:
      path = os.path.join(dirpath, name)
      try:
        if os.stat(path).st_ctime < cutoff:
          os.remove(path)
          files_deleted += 1
      except OSError as e:
        logging.warning("purge_trash: could not remove %s: %s", path, e)
    if dirpath != root:
      try:
        os.rmdir(dirpath)
        dirs_removed += 1
      except OSError:
        pass   # not empty: a file is still within its retention window, or removal above failed
  if files_deleted or dirs_removed:
    logging.info("purge_trash: deleted %d file(s) older than %d day(s), removed %d empty dir(s)",
                 files_deleted, retention_days, dirs_removed)
  return files_deleted, dirs_removed
