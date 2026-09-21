"""Rating semantics and resolving a Photo's state from its sidecars.

The rating is one combined value in [-1, 5]: -1 reject, 0 unrated, 1..5
picked with that many stars. Fav is an independent flag (the reserved
dc:subject keyword 'fav'); tags are the other dc:subject entries.
"""

import dataclasses
import json

REJECT = -1
UNRATED = 0
MAX_STARS = 5


def clamp(value):
  return max(REJECT, min(MAX_STARS, int(value)))


def apply_key(current, key, previous_stars=None):
  """New rating after a rating key press.

  '0'..'5' set the rating directly; 'x' toggles reject. Un-rejecting
  restores previous_stars if it is known, else unrated.
  """
  if key in "012345" and len(key) == 1:
    return int(key)
  if key.lower() == "x":
    if current == REJECT:
      return clamp(previous_stars or UNRATED)
    return REJECT
  raise ValueError(f"not a rating key: {key!r}")


def step(current, delta, previous_stars=None):
  """Move one level along -1, 0, 1..5, clamped (swipe up/down)."""
  new = clamp(current + (1 if delta > 0 else -1 if delta < 0 else 0))
  if current == REJECT and delta > 0 and previous_stars:
    return clamp(previous_stars)
  return new


@dataclasses.dataclass
class Resolved:
  rating: int = None    # None: no sidecar has an opinion
  fav: bool = False
  tags: tuple = ()
  conflict: bool = False
  source_path: str = None   # sidecar the values come from


def resolve(sidecars):
  """Resolve a Photo's state from its sidecars.

  sidecars: dicts with path, mtime, rating (None when absent), has_fav, tags.
  The newest sidecar decides fav and tags, and the rating unless it has
  none (then the newest sidecar that has one). Sidecars that disagree on
  rating or fav set conflict; nothing is modified.
  """
  if not sidecars:
    return Resolved()
  newest_first = sorted(sidecars, key=lambda s: (-s["mtime"], s["path"]))
  winner = newest_first[0]
  rated = [s for s in newest_first if s["rating"] is not None]
  rating = clamp(rated[0]["rating"]) if rated else None
  conflict = (len({clamp(s["rating"]) for s in rated}) > 1
              or len({bool(s["has_fav"]) for s in sidecars}) > 1)
  return Resolved(rating=rating, fav=bool(winner["has_fav"]),
                  tags=tuple(winner["tags"]), conflict=conflict,
                  source_path=(rated[0] if rated else winner)["path"])


def _sidecars_of(conn, photo_id):
  rows = conn.execute(
      "SELECT s.path, s.mtime, s.rating, s.has_fav, s.tags "
      "FROM xmp_sidecars s JOIN files f ON f.id = s.file_id "
      "WHERE f.photo_id = ? ORDER BY s.path", (photo_id,)).fetchall()
  return [dict(r, tags=json.loads(r["tags"] or "[]")) for r in rows]


def refresh_photo(conn, photo_id):
  """Recompute one Photo's cached rating/fav/tags/conflict from its sidecars.

  A Photo with no sidecar keeps what it has (for example an imported
  rating). Returns the Resolved value.
  """
  sidecars = _sidecars_of(conn, photo_id)
  r = resolve(sidecars)
  if not sidecars:
    conn.execute("UPDATE photos SET conflict = 0 WHERE id = ?", (photo_id,))
    return r
  if r.rating is not None:
    conn.execute("UPDATE photos SET rating = ?, rating_source = 'xmp' "
                 "WHERE id = ?", (r.rating, photo_id))
  conn.execute("UPDATE photos SET fav = ?, conflict = ? WHERE id = ?",
               (int(r.fav), int(r.conflict), photo_id))
  conn.execute("DELETE FROM tags WHERE photo_id = ?", (photo_id,))
  conn.executemany("INSERT OR IGNORE INTO tags (photo_id, tag) VALUES (?, ?)",
                   [(photo_id, t) for t in r.tags])
  return r


def remember(conn, photo_id):
  """Keep rating_by_hash current for rename recovery (see recovery.py)."""
  from photoapp import recovery
  recovery.remember_photo(conn, photo_id)


def refresh_dirs(conn, rel_dirs):
  """Refresh every Photo that has a file directly in one of rel_dirs."""
  wanted = set(rel_dirs)
  if not wanted:
    return 0
  from photoapp import paths
  photo_ids = set()
  for d in wanted:
    for r in paths.files_in_dir(conn, d, "photo_id"):
      if r["photo_id"] is not None:
        photo_ids.add(r["photo_id"])
  for pid in sorted(photo_ids):
    refresh_photo(conn, pid)
    remember(conn, pid)
  conn.commit()
  return len(photo_ids)


def refresh_unresolved(conn):
  """Resolve Photos that have sidecars but were never resolved from them.

  Covers databases scanned by an older version and sidecars without a
  rating (cheap: these are re-resolved on every scan).
  """
  ids = [r["id"] for r in conn.execute(
      "SELECT DISTINCT p.id FROM photos p JOIN files f ON f.photo_id = p.id "
      "JOIN xmp_sidecars s ON s.file_id = f.id WHERE p.rating_source IS NULL")]
  for pid in ids:
    refresh_photo(conn, pid)
    remember(conn, pid)
  conn.commit()
  return len(ids)
