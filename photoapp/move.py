"""Moving a Photo's files to another folder inside pictures_dir (ticket 144/145).

Unlike trash.py's move into .trash/ (a fixed, mirrored destination for a file this app is done
showing), this is a *live* relocation: the file stays fully part of the library, just at a new
path. So instead of marking the files row missing=1, this repoints files.path in place and moves
its cached thumbnails/PreviewDNG (thumbs.move_thumbnails / raw_preview_dng.move -- the same
machinery ticket 128 uses when a move is *discovered* by a scan) so the UI reflects the move
immediately, without waiting for the rescan this triggers to rediscover it via hash matching.

Every live file of a Photo moves together (RAW+JPEG pairs, a linked tuning file, ...), each with
its own sidecars (every xmp_sidecars row for that file_id, plus its ticket 111 per-file
<name>.json) -- matching trash._move_file_and_sidecars's enumeration. A sidecar's *file* moves
here; its `xmp_sidecars` table bookkeeping is deliberately left to the rescan the caller schedules
afterward (see docs/design/move.md), the same as it would be for a move made outside this app.

rename_dir (ticket 155) is a different operation living in the same module: renaming a whole
directory, one `os.rename` for everything inside it at once, rather than moving individually
chosen files to a shared destination. See its own docstring.
"""

import os
import shutil

from absl import logging

from photoapp import paths
from photoapp import raw_preview_dng
from photoapp import thumbs


class MoveError(Exception):
  """A request that cannot be carried out; message is safe to show."""


def _renamed_sidecar_name(old_name, new_name, sidecar_name):
  """sidecar_name (e.g. K.DNG.xmp, K.xmp, or K.DNG.json), reflecting old_name's rename to
  new_name -- so a sidecar still names the right file after a disambiguating suffix (ticket 145).
  xmp.py's two conventions: 'full' (filename.xmp) and 'raw_style' (stem.xmp, RAW only); the
  ticket 111 per-file cache is always filename.json. A sidecar_name that doesn't match either
  pattern for old_name (unexpected) is left unchanged -- best-effort, like the rest of this move."""
  if sidecar_name == old_name + ".xmp":
    return new_name + ".xmp"
  if sidecar_name == old_name + ".json":
    return new_name + ".json"
  stem = os.path.splitext(old_name)[0]
  if sidecar_name == stem + ".xmp":
    return os.path.splitext(new_name)[0] + ".xmp"
  return sidecar_name


def _rel_join(dest_dir, name):
  """dest_dir + name as a relative path in this app's convention: no "./" prefix for the root."""
  return name if dest_dir in ("", ".") else f"{dest_dir}/{name}"


def resolve_dest_path(pictures_dir, dest_dir, name, taken):
  """The relative destination path for `name` inside dest_dir, disambiguated with a numeric
  suffix (name-2.ext, name-3.ext, ...) if something is already there -- either on disk, or already
  claimed earlier in the same batch (`taken`, since those moves haven't happened yet when a later
  file in the batch is resolved). Unlike export.resolve_dest_path, there is no "reusable" special
  case: a collision here always means an unrelated file, and silently overwriting it would be
  wrong."""
  root, ext = os.path.splitext(name)
  n = 1
  while True:
    candidate = name if n == 1 else f"{root}-{n}{ext}"
    rel = _rel_join(dest_dir, candidate)
    if rel not in taken and not os.path.exists(os.path.join(pictures_dir, rel)):
      return rel
    n += 1


def _move_file_and_sidecars(conn, settings, file_id, rel_path, dest_dir, taken):
  """Move one file -- and its sidecars -- into dest_dir. Returns (moved, errors, source_dir):
  moved is [{"from", "to"}] for the image itself plus every sidecar actually moved (mirrors
  trash._move_file_and_sidecars's shape, which lists sidecars individually too); errors is
  [{"path", "error"}] for anything that failed (best-effort, like trash.py: one sidecar failing
  does not undo the image's own move); source_dir is the directory the image moved out of, or
  None if this file was already directly in dest_dir (a no-op, not an error)."""
  if paths.dirname(rel_path) == dest_dir:
    return [], [], None
  name = rel_path.rsplit("/", 1)[-1]
  new_rel = resolve_dest_path(pictures_dir=settings.pictures_dir, dest_dir=dest_dir, name=name,
                              taken=taken)
  taken.add(new_rel)
  src = os.path.join(settings.pictures_dir, rel_path)
  dest = os.path.join(settings.pictures_dir, new_rel)
  try:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(src, dest)
  except OSError as e:
    return [], [{"path": rel_path, "error": str(e)}], None

  conn.execute("UPDATE files SET path = ? WHERE id = ?", (new_rel, file_id))
  if settings.thumbs_dir:
    thumbs.move_thumbnails(conn, settings.thumbs_dir, file_id, rel_path, new_rel)
    raw_preview_dng.move(settings.thumbs_dir, rel_path, new_rel)
  moved = [{"from": rel_path, "to": new_rel}]
  errors = []

  # Sidecars: best-effort, same as trash._move_file_and_sidecars -- a missing one (never existed)
  # is not an error, but a real OSError moving one that exists is reported, not swallowed.
  # Renamed to match new_name if the image itself was disambiguated above, so the sidecar still
  # names the right file. Their xmp_sidecars/index.json bookkeeping is left to the rescan the
  # caller schedules (docs/design/move.md), not hand-maintained here.
  new_name = new_rel.rsplit("/", 1)[-1]
  sidecar_rels = [rel_path + ".json"] + [r["path"] for r in conn.execute(
      "SELECT path FROM xmp_sidecars WHERE file_id = ?", (file_id,))]
  for sidecar_rel in sidecar_rels:
    sidecar_src = os.path.join(settings.pictures_dir, sidecar_rel)
    if not os.path.isfile(sidecar_src):
      continue
    sidecar_dest_name = _renamed_sidecar_name(
        name, new_name, sidecar_rel.rsplit("/", 1)[-1])
    sidecar_dest_rel = _rel_join(dest_dir, sidecar_dest_name)
    sidecar_dest = os.path.join(settings.pictures_dir, sidecar_dest_rel)
    try:
      os.makedirs(os.path.dirname(sidecar_dest), exist_ok=True)
      shutil.move(sidecar_src, sidecar_dest)
      moved.append({"from": sidecar_rel, "to": sidecar_dest_rel})
    except OSError as e:
      errors.append({"path": sidecar_rel, "error": str(e)})

  logging.vlog(3, "%s: moved to %s (file %d, %d sidecar(s))", rel_path, new_rel, file_id,
              len(moved) - 1)
  return moved, errors, paths.dirname(rel_path)


def move_photo(conn, settings, photo_id, dest_dir):
  """Move every live file of one Photo (any role) into dest_dir. Returns {"photo_id", "moved":
  [{"from", "to"}], "errors": [{"path", "error"}], "source_dirs": {...}} -- source_dirs is every
  directory a file actually moved out of, for the caller to schedule a rescan of. Raises MoveError
  if the Photo does not exist."""
  photo = conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
  if photo is None:
    raise MoveError(f"no such photo: {photo_id}")
  files = conn.execute(
      "SELECT id, path FROM files WHERE photo_id = ? AND missing = 0",
      (photo_id,)).fetchall()
  moved, errors, source_dirs, taken = [], [], set(), set()
  for f in files:
    m, e, source_dir = _move_file_and_sidecars(conn, settings, f["id"], f["path"], dest_dir, taken)
    moved += m
    errors += e
    if source_dir:
      source_dirs.add(source_dir)
  conn.commit()
  logging.info("move_photo %d: moved %d file(s) into %s, %d error(s)",
               photo_id, len(moved), dest_dir, len(errors))
  return {"photo_id": photo_id, "moved": moved, "errors": errors, "source_dirs": source_dirs}


def move_photos(conn, settings, photo_ids, dest_dir):
  """move_photo for many Photos; a Photo that fails is reported in errors, not silently skipped,
  and does not stop the rest (matches trash.trash_photos). Returns {"moved": [...], "errors":
  [{"photo_id", "error"}], "source_dirs": {...}} -- source_dirs is the union across every Photo."""
  moved, errors, source_dirs = [], [], set()
  for pid in dict.fromkeys(photo_ids):
    try:
      result = move_photo(conn, settings, pid, dest_dir)
      moved.append(result)
      source_dirs |= result["source_dirs"]
    except MoveError as e:
      errors.append({"photo_id": pid, "error": str(e)})
  return {"moved": moved, "errors": errors, "source_dirs": source_dirs}


def rename_dir(conn, settings, old_rel_dir, new_rel_dir):
  """Rename a whole directory (ticket 155) -- a different operation from move_photos above, not
  built on it: every file, sidecar and per-file JSON cache under old_rel_dir moves in a single
  `os.rename` of the directory itself, with no per-file collision handling needed (the target must
  not already exist -- see the checks below), unlike move_photos's individually-resolved
  destination paths. Raises MoveError for: old_rel_dir missing, new_rel_dir already existing,
  new_rel_dir inside old_rel_dir's own subtree (os.rename would fail confusingly), or the two
  being equal.

  files.path is rewritten for every affected row in one bulk pass (a plain prefix replace -- exactly
  which new path each old one maps to is never ambiguous the way a scan's hash-matching move
  detection can be, so this doesn't wait for the rescan the caller schedules afterward, same
  reasoning as move_photo). Cached thumbnails/PreviewDNG per file still reuse ticket 128's
  `thumbs.move_thumbnails`/`raw_preview_dng.move` (a completely separate tree under thumbs_dir,
  untouched by the source directory's own os.rename, so there's no move-ordering conflict).
  xmp_sidecars table bookkeeping is left to the rescan, same division of labor as move_photo/
  docs/design/move.md. Returns {"old_dir", "new_dir", "files_moved"}."""
  old_abs = os.path.join(settings.pictures_dir, old_rel_dir)
  new_abs = os.path.join(settings.pictures_dir, new_rel_dir)
  if old_rel_dir == new_rel_dir:
    raise MoveError("source and target are the same")
  if old_rel_dir in ("", "."):
    raise MoveError("cannot rename the library root")
  if new_rel_dir == old_rel_dir or new_rel_dir.startswith(old_rel_dir + "/"):
    raise MoveError("target is inside the source directory")
  if not os.path.isdir(old_abs):
    raise MoveError(f"no such directory: {old_rel_dir}")
  if os.path.exists(new_abs):
    raise MoveError(f"target already exists: {new_rel_dir}")

  lo, hi = paths.subtree_range(old_rel_dir)
  rows = conn.execute(
      "SELECT id, path FROM files WHERE path >= ? AND path < ?", (lo, hi)).fetchall()

  os.makedirs(os.path.dirname(new_abs), exist_ok=True)
  os.rename(old_abs, new_abs)

  for row in rows:
    new_path = new_rel_dir + row["path"][len(old_rel_dir):]
    conn.execute("UPDATE files SET path = ? WHERE id = ?", (new_path, row["id"]))
    if settings.thumbs_dir:
      thumbs.move_thumbnails(conn, settings.thumbs_dir, row["id"], row["path"], new_path)
      raw_preview_dng.move(settings.thumbs_dir, row["path"], new_path)
  conn.commit()
  logging.info("rename_dir: %s -> %s (%d file(s))", old_rel_dir, new_rel_dir, len(rows))
  return {"old_dir": old_rel_dir, "new_dir": new_rel_dir, "files_moved": len(rows)}
