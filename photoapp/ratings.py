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


# Clocks differ (the app machine versus the file server): a database time within this many
# seconds of the newest sidecar counts as a tie, and a tie keeps the database value.
SKEW_TOLERANCE = 2.0

DATABASE = "(database)"


@dataclasses.dataclass
class Resolved:
  rating: int = None        # None: no source has an opinion
  fav: bool = False
  tags: tuple = ()
  conflict: bool = False    # sidecars disagree, or the database is ahead of a differing sidecar
  source_path: str = None   # sidecar path the rating comes from, or DATABASE
  db_wins: bool = False     # the database is the newest source (sidecars are behind or equal)
  mtime: float = None       # time of the winning source
  reject_overruled: bool = False   # a single file's reject was not applied to the Photo (see below)


def _newest_sidecar_first(sidecars):
  return sorted(sidecars, key=lambda s: (-s["mtime"], s["path"]))


def resolve(sidecars, db=None):
  """Resolve a Photo's state: the newest source wins (ticket 021).

  sidecars: dicts with path, mtime, rating (None when absent), has_fav, tags.
  db: the database's own value, {mtime, rating, has_fav, tags}, or None when the database has no
  dated rating. Both the database and every sidecar are eligible; the newest wins. Sidecars are
  ordered by time then path; a database time within SKEW_TOLERANCE of the newest sidecar (or
  later) wins the tie. The winner decides fav and tags too.

  Reject rule (ticket 053): darktable treats the DNG and the JPG as separate images, so a reject
  on one sidecar often only means "keep the other file". If the rating winner is a reject that
  comes from a sidecar and another sidecar is picked (rating 1..5), the Photo is not rejected:
  its rating is the newest picked sidecar's. A reject made in this app is written to every
  sidecar and the database, so nothing is picked and it stands, and if the database is the
  newest source its reject wins outright.

  conflict: the sidecars disagree on rating or fav, or the database wins but a sidecar differs
  from it ("sidecars behind"). Nothing is modified here.
  """
  ordered = _newest_sidecar_first(sidecars)
  if not ordered and not db:
    return Resolved()
  db_cand = dict(db, path=DATABASE) if db else None

  def newest(sidecar_list):
    best = sidecar_list[0] if sidecar_list else None
    if db_cand and (best is None or db_cand["mtime"] + SKEW_TOLERANCE >= best["mtime"]):
      return db_cand
    return best

  rated = [s for s in ordered if s["rating"] is not None]
  winner_all = newest(ordered)
  winner_rating = newest(rated) if (rated or db_cand) else None
  overruled = False
  if winner_rating is not None and winner_rating is not db_cand and winner_rating["rating"] == REJECT:
    picked = [s for s in rated if s["rating"] > 0]
    if picked:
      winner_rating, overruled = picked[0], True     # newest picked sidecar
  rating = clamp(winner_rating["rating"]) if winner_rating is not None else None
  meta = winner_rating if overruled else winner_all
  disagree = (len({clamp(s["rating"]) for s in rated}) > 1
              or len({bool(s["has_fav"]) for s in ordered}) > 1)
  db_wins = winner_rating is db_cand and db_cand is not None
  behind = db_wins and any(
      (s["rating"] is not None and clamp(s["rating"]) != clamp(db_cand["rating"]))
      or bool(s["has_fav"]) != bool(db_cand["has_fav"]) for s in ordered)
  return Resolved(
      rating=rating, fav=bool(meta["has_fav"]) if meta else False,
      tags=tuple(meta["tags"]) if meta else (), conflict=disagree or behind,
      source_path=winner_rating["path"] if winner_rating else None,
      db_wins=db_wins, mtime=winner_rating["mtime"] if winner_rating else None,
      reject_overruled=overruled)


def _sidecars_of(conn, photo_id):
  rows = conn.execute(
      "SELECT s.path, s.mtime, s.rating, s.has_fav, s.tags "
      "FROM xmp_sidecars s JOIN files f ON f.id = s.file_id "
      "WHERE f.photo_id = ? ORDER BY s.path", (photo_id,)).fetchall()
  return [dict(r, tags=json.loads(r["tags"] or "[]")) for r in rows]


# Where a database rating came from. Only ratings the database decided itself are an
# independent source; one derived from a sidecar ('xmp') is just a cache of it.
DECIDED_HERE = ("app", "import", "hash-recovery")


def db_source(conn, photo_id):
  """The database's own dated rating for a Photo, or None.

  None when the rating has no time, or when it was only read from a sidecar (a cache, not a
  second opinion): then the sidecars alone decide.
  """
  p = conn.execute("SELECT rating, fav, rating_updated_at, rating_source FROM photos "
                   "WHERE id = ?", (photo_id,)).fetchone()
  if (p is None or not p["rating_updated_at"]
      or p["rating_source"] not in DECIDED_HERE):
    return None
  tags = [r["tag"] for r in conn.execute(
      "SELECT tag FROM tags WHERE photo_id = ? ORDER BY tag", (photo_id,))]
  return {"mtime": p["rating_updated_at"], "rating": p["rating"],
          "has_fav": p["fav"], "tags": tags}


def refresh_photo(conn, photo_id):
  """Recompute one Photo's cached rating/fav/tags/conflict: the newest of the database and
  its sidecars wins (ticket 021).

  A newer sidecar updates the database value and its time; a newer database value is kept
  (the sidecars catch up on the next write) and shows as conflict. A Photo with no sidecar
  keeps what it has (for example an imported rating). Returns the Resolved value.
  """
  sidecars = _sidecars_of(conn, photo_id)
  r = resolve(sidecars, db_source(conn, photo_id))
  if not sidecars:
    conn.execute("UPDATE photos SET conflict = 0 WHERE id = ?", (photo_id,))
    return r
  if r.db_wins:
    conn.execute("UPDATE photos SET conflict = ? WHERE id = ?", (int(r.conflict), photo_id))
    return r
  if r.rating is not None:
    conn.execute("UPDATE photos SET rating = ?, rating_source = 'xmp', "
                 "rating_updated_at = ? WHERE id = ?", (r.rating, r.mtime, photo_id))
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
