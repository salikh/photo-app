"""Changing rating, fav and tags: write the sidecars, update the cache, log
the change, undo.

The sidecars of the Photo's original and of its camera JPG are both written
(decided in ticket 023), so the two stay in sync in darktable; reading still
uses "newest sidecar wins". The cache is updated from what was written, then
the Photo is re-resolved from all its sidecars.
"""

import datetime
import hashlib
import json
import os
import uuid

from absl import logging

from photoapp import ratings
from photoapp import xmp

MAX_TAG_LENGTH = 200


class CurationError(Exception):
  """A request that cannot be carried out; message is safe to show."""


def _now():
  return datetime.datetime.now().isoformat(timespec="seconds")


def _photo(conn, photo_id):
  row = conn.execute("SELECT * FROM photos WHERE id = ?",
                     (photo_id,)).fetchone()
  if row is None:
    raise CurationError(f"no such photo: {photo_id}")
  return row


def _photo_tags(conn, photo_id):
  return sorted(r["tag"] for r in conn.execute(
      "SELECT tag FROM tags WHERE photo_id = ?", (photo_id,)))


def _clean_tags(tags):
  out = []
  for t in tags:
    t = t.strip()
    if not t:
      continue
    if len(t) > MAX_TAG_LENGTH:
      raise CurationError("tag too long")
    if t == xmp.FAV_TAG:
      raise CurationError("'fav' is set through the fav flag, not as a tag")
    out.append(t)
  return list(dict.fromkeys(out))


def _sidecar_targets(conn, settings, photo):
  """Sidecars to write for a Photo: the original's and the camera JPG's.

  Returns [{"file_id", "rel", "exists"}], original first, deduplicated by
  sidecar path (a bare NAME.xmp can serve both files). Raises if the original
  is missing on disk.
  """
  files = conn.execute(
      "SELECT id, path, missing, role FROM files WHERE photo_id = ? AND "
      "(id = ? OR role = 'camera') ORDER BY (id = ?) DESC, path",
      (photo["id"], photo["original_file_id"], photo["original_file_id"])
  ).fetchall()
  if not files or files[0]["id"] != photo["original_file_id"] or files[0]["missing"]:
    raise CurationError("the original file is missing on disk")
  targets, seen = [], set()
  listings = {}
  for f in files:
    if f["missing"]:
      continue
    directory, _, name = f["path"].rpartition("/")
    if directory not in listings:
      try:
        listings[directory] = os.listdir(os.path.join(settings.pictures_dir, directory))
      except OSError as e:
        raise CurationError(f"cannot list {directory or '.'}: {e}")
    prefix = directory + "/" if directory else ""
    existing = xmp.find_sidecars(name, listings[directory])
    if existing:
      rel, exists = prefix + existing[0], True
    else:
      rel = prefix + xmp.preferred_sidecar_name(name, settings.new_raw_sidecar_style)
      exists = False
    if rel in seen:
      continue
    seen.add(rel)
    targets.append({"file_id": f["id"], "rel": rel, "exists": exists})
  return targets


def _backup_path(settings, rel_sidecar):
  ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
  return os.path.join(settings.state_dir, "xmp_backups",
                      f"{rel_sidecar}.{ts}")


def _log(conn, photo_id, xmp_path, field, old, new, cause, batch_id=None):
  cur = conn.execute(
      "INSERT INTO activity_log (ts, photo_id, xmp_path, field, old, new,"
      " cause, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
      (_now(), photo_id, xmp_path, field, old, new, cause, batch_id))
  return cur.lastrowid


def _apply(conn, settings, photo_id, rating=None, fav=None, add=(), remove=(),
           cause="user", batch_id=None):
  """Core write path. Returns a dict describing the change (or dry run)."""
  photo = _photo(conn, photo_id)
  add, remove = list(add), list(remove)
  if fav is True:
    add.append(xmp.FAV_TAG)
  elif fav is False:
    remove.append(xmp.FAV_TAG)
  if rating is not None and not (ratings.REJECT <= rating <= ratings.MAX_STARS):
    raise CurationError("rating must be between -1 and 5")

  old_tags = _photo_tags(conn, photo_id)
  old_rating, old_fav = photo["rating"], bool(photo["fav"])
  new_tags = sorted((set(old_tags) | {t for t in add if t != xmp.FAV_TAG})
                    - set(remove))
  new_rating = old_rating if rating is None else rating
  new_fav = old_fav if fav is None else fav
  changes = {}
  if new_rating != old_rating:
    changes["rating"] = (str(old_rating), str(new_rating))
  if new_fav != old_fav:
    changes["fav"] = (str(int(old_fav)), str(int(new_fav)))
  if new_tags != old_tags:
    changes["tags"] = (json.dumps(old_tags), json.dumps(new_tags))

  targets = _sidecar_targets(conn, settings, photo)
  original = targets[0]
  result = {"photo_id": photo_id, "sidecar": original["rel"],
            "sidecars": [t["rel"] for t in targets], "activity_ids": [],
            "changes": {k: {"old": o, "new": n}
                        for k, (o, n) in changes.items()},
            "dry_run": settings.xmp_dry_run, "created": not original["exists"]}
  if (not changes and not any(t["exists"] for t in targets)
      and photo["rating_source"] != "import"):
    return result   # nothing to do (an imported rating still gets written)

  # The desired state is written to every target so that a DNG and its camera
  # JPG stay in sync (e.g. in darktable). Tags are converged conservatively:
  # only tags the app knew about are removed, others in a sidecar are kept.
  desired = list(new_tags) + ([xmp.FAV_TAG] if new_fav else [])
  known = set(old_tags) | ({xmp.FAV_TAG} if old_fav else set()) | set(remove)
  edit = {"rating": new_rating, "add_tags": desired,
          "remove_tags": [t for t in known if t not in desired]}
  default_state = new_rating == 0 and not desired
  if settings.xmp_dry_run:
    logging.info("xmp_dry_run: would write %s with %s",
                 [t["rel"] for t in targets], result["changes"])
    return result

  written, error = [], None
  for t in targets:
    abs_sidecar = os.path.join(settings.pictures_dir, t["rel"])
    backup = None
    try:
      if t["exists"]:
        known_backup = conn.execute(
            "SELECT backup_path FROM xmp_sidecars WHERE path = ?",
            (t["rel"],)).fetchone()
        backup = (known_backup["backup_path"] if known_backup and
                  known_backup["backup_path"] else _backup_path(settings, t["rel"]))
        upd = xmp.update_file(abs_sidecar, backup_path=backup, **edit)
        if upd.backup_path is None and not os.path.exists(backup):
          backup = None
      elif default_state:
        continue          # nothing worth a new file (unrated, no tags)
      else:
        xmp.create_file(abs_sidecar, rating=new_rating, tags=desired)
    except (xmp.XmpEditError, OSError) as e:
      error = f"cannot write {t['rel']}: {e}"
      break
    with open(abs_sidecar, "rb") as f:
      data = f.read()
    parsed = xmp.parse(data)
    conn.execute(
        "INSERT INTO xmp_sidecars (path, file_id, mtime, hash, rating, has_fav,"
        " tags, backup_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET file_id = excluded.file_id,"
        " mtime = excluded.mtime, hash = excluded.hash, rating = excluded.rating,"
        " has_fav = excluded.has_fav, tags = excluded.tags,"
        " backup_path = COALESCE(xmp_sidecars.backup_path, excluded.backup_path)",
        (t["rel"], t["file_id"], os.stat(abs_sidecar).st_mtime,
         hashlib.sha224(data).hexdigest(), parsed.rating, int(parsed.fav),
         json.dumps(list(parsed.tags)), backup))
    written.append(t["rel"])
  if error:
    # Keep the cache truthful about what did get written, then report.
    ratings.refresh_photo(conn, photo_id)
    conn.commit()
    raise CurationError(error + (f" (already written: {', '.join(written)})"
                                 if written else ""))
  if "rating" in changes:
    # Rejecting erases the star count in the sidecar; remember it for un-reject.
    prev = photo["previous_stars"]
    if new_rating == ratings.REJECT and old_rating > 0:
      prev = old_rating
    conn.execute("UPDATE photos SET rating = ?, previous_stars = ?, "
                 "rating_source = 'app' WHERE id = ?",
                 (new_rating, prev, photo_id))
  if "fav" in changes:
    conn.execute("UPDATE photos SET fav = ? WHERE id = ?",
                 (int(new_fav), photo_id))
  if "tags" in changes:
    conn.execute("DELETE FROM tags WHERE photo_id = ?", (photo_id,))
    conn.executemany("INSERT OR IGNORE INTO tags (photo_id, tag) VALUES (?, ?)",
                     [(photo_id, t) for t in new_tags])
  result["activity_ids"] = [
      _log(conn, photo_id, original["rel"], field, o, n, cause, batch_id)
      for field, (o, n) in changes.items()]
  ratings.refresh_photo(conn, photo_id)
  ratings.remember(conn, photo_id)
  conn.commit()
  return result


def set_rating(conn, settings, photo_id, rating, cause="user"):
  return _apply(conn, settings, photo_id, rating=int(rating), cause=cause)


def set_rating_batch(conn, settings, photo_ids, rating):
  """Rate many Photos; failures do not stop the rest.

  All log entries share one batch_id so the whole action can be undone.
  Returns {"batch_id", "results": [...], "errors": [{photo_id, error}]}.
  """
  batch_id = uuid.uuid4().hex
  results, errors = [], []
  for pid in dict.fromkeys(photo_ids):
    try:
      result = _apply(conn, settings, pid, rating=int(rating),
                      batch_id=batch_id)
      result["photo"] = photo_state(conn, pid)
      results.append(result)
    except CurationError as e:
      errors.append({"photo_id": pid, "error": str(e)})
  return {"batch_id": batch_id, "results": results, "errors": errors}


def undo_batch(conn, settings, batch_id):
  """Undo every not-yet-undone entry of a batch (newest first)."""
  entries = conn.execute(
      "SELECT id, photo_id FROM activity_log WHERE batch_id = ? AND "
      "undone = 0 ORDER BY id DESC", (batch_id,)).fetchall()
  if not entries:
    raise CurationError("nothing to undo for this batch")
  undone, errors = [], []
  for e in entries:
    try:
      undo(conn, settings, e["id"])
      undone.append(e["id"])
    except CurationError as ex:
      errors.append({"activity_id": e["id"], "error": str(ex)})
  photo_ids = sorted({e["photo_id"] for e in entries})
  return {"undone": undone, "errors": errors,
          "photos": [photo_state(conn, pid) for pid in photo_ids]}


def set_fav(conn, settings, photo_id, fav, cause="user"):
  return _apply(conn, settings, photo_id, fav=bool(fav), cause=cause)


def edit_tags(conn, settings, photo_id, add=(), remove=(), cause="user"):
  return _apply(conn, settings, photo_id, add=_clean_tags(add),
                remove=_clean_tags(remove), cause=cause)


def photo_state(conn, photo_id):
  """The Photo's cached curation state, as the API returns it."""
  p = _photo(conn, photo_id)
  return {"id": p["id"], "rating": p["rating"], "fav": bool(p["fav"]),
          "tags": _photo_tags(conn, photo_id), "conflict": bool(p["conflict"]),
          "previous_stars": p["previous_stars"]}


def recent_activity(conn, limit=100):
  return [dict(r) for r in conn.execute(
      "SELECT a.*, (SELECT f.path FROM files f WHERE f.id = "
      "p.original_file_id) AS path FROM activity_log a "
      "LEFT JOIN photos p ON p.id = a.photo_id ORDER BY a.id DESC LIMIT ?",
      (limit,))]


def undo(conn, settings, activity_id):
  """Revert one logged change by writing the old value back.

  Refused if the value has changed since (undo would clobber a later edit).
  """
  entry = conn.execute("SELECT * FROM activity_log WHERE id = ?",
                       (activity_id,)).fetchone()
  if entry is None:
    raise CurationError("no such activity entry")
  if entry["undone"]:
    raise CurationError("already undone")
  photo = _photo(conn, entry["photo_id"])
  field, old, new = entry["field"], entry["old"], entry["new"]
  if field == "rating":
    if str(photo["rating"]) != new:
      raise CurationError("the rating has changed since; not undoing")
    result = _apply(conn, settings, photo["id"], rating=int(old), cause="undo")
  elif field == "fav":
    if str(int(photo["fav"])) != new:
      raise CurationError("fav has changed since; not undoing")
    result = _apply(conn, settings, photo["id"], fav=bool(int(old)),
                    cause="undo")
  elif field == "tags":
    current = _photo_tags(conn, photo["id"])
    if current != json.loads(new):
      raise CurationError("tags have changed since; not undoing")
    wanted = json.loads(old)
    result = _apply(conn, settings, photo["id"],
                    add=[t for t in wanted if t not in current],
                    remove=[t for t in current if t not in wanted],
                    cause="undo")
  else:
    raise CurationError(f"cannot undo field {field!r}")
  if not settings.xmp_dry_run:
    conn.execute("UPDATE activity_log SET undone = 1 WHERE id = ?",
                 (activity_id,))
    conn.commit()
  return result
