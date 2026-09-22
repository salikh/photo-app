"""Moving a rejected Photo's files to <pictures_dir>/.trash/ instead of deleting them (ticket 072).

Every other write this app makes is reversible (a rating) or additive (a sidecar, a thumbnail
cache entry); this is the first one that removes an original from where the rest of the library
expects it. Nothing is ever unlinked: every file (the original, its camera JPG, any tuning, and
every sidecar) is *moved* into a mirror of its own subdirectory structure under
<pictures_dir>/.trash/, so it stays fully recoverable by hand (and, eventually, by an automatic
purge after some retention window -- a separate ticket) rather than gone outright.

The moved file's `files` row is marked missing=1 (the same state a file gets when a scan finds
it vanished, ticket 010), not deleted, so the Photo's rating/history survives. .trash/ itself is
never scanned (see scan.top_level_steps) precisely so a trashed file's still-correct sidecar
(rating -1) does not get re-discovered as a brand new, live Photo.
"""

import os
import shutil

from absl import logging

TRASH_DIRNAME = ".trash"


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
    rels = [f["path"]] + [r["path"] for r in conn.execute(
        "SELECT path FROM xmp_sidecars WHERE file_id = ?", (f["id"],))]
    for rel in rels:
      try:
        dest = move_to_trash(settings.pictures_dir, rel)
      except (OSError, TrashError) as e:
        errors.append({"path": rel, "error": str(e)})
        continue
      if dest:
        moved.append({"from": rel, "to": dest})
    conn.execute("UPDATE files SET missing = 1 WHERE id = ?", (f["id"],))
  conn.commit()
  logging.info("trash_photo %d: moved %d file(s) to %s, %d error(s)",
               photo_id, len(moved), trash_dir(settings.pictures_dir), len(errors))
  return {"photo_id": photo_id, "moved": moved, "errors": errors}


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
