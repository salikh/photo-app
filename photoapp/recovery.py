"""Recover ratings for files that moved or were renamed.

rating_by_hash remembers (hash -> rating, fav, last_path) for every rated
Photo. A Photo with no rating anywhere whose file content matches a
remembered hash whose last_path no longer exists gets that rating and fav
back (written to its sidecar, logged with cause 'hash-recovery').

Only unambiguous cases are acted on. If several unrated Photos share one
hash, or a Photo's files point at different remembered ratings, nothing is
guessed; the case is reported by ambiguous().
"""

from photoapp import curation


def remember_photo(conn, photo_id):
  """Record a Photo's current rating/fav under the hash of each live file."""
  p = conn.execute("SELECT rating, fav FROM photos WHERE id = ?",
                   (photo_id,)).fetchone()
  if p is None or (p["rating"] == 0 and not p["fav"]):
    return
  for f in conn.execute(
      "SELECT hash, path FROM files WHERE photo_id = ? AND missing = 0 "
      "AND hash IS NOT NULL", (photo_id,)):
    conn.execute(
        "INSERT INTO rating_by_hash (hash, rating, fav, last_path) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(hash) DO UPDATE SET "
        "rating = excluded.rating, fav = excluded.fav, "
        "last_path = excluded.last_path",
        (f["hash"], p["rating"], p["fav"], f["path"]))


def _candidates(conn):
  """Unrated Photos with remembered-but-lost ratings for their files.

  Returns {photo_id: [(hash, rating, fav, old_path), ...]}.
  """
  rows = conn.execute(
      "SELECT f.photo_id, f.hash, r.rating, r.fav, r.last_path "
      "FROM photos p JOIN files f ON f.photo_id = p.id AND f.missing = 0 "
      "JOIN rating_by_hash r ON r.hash = f.hash "
      "WHERE p.rating = 0 AND p.fav = 0 AND p.rating_source IS NULL "
      " AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id) "
      " AND NOT EXISTS (SELECT 1 FROM xmp_sidecars s JOIN files sf ON "
      "  sf.id = s.file_id WHERE sf.photo_id = p.id) "
      " AND f.path != r.last_path "
      " AND NOT EXISTS (SELECT 1 FROM files o WHERE o.path = r.last_path "
      "  AND o.missing = 0) "
      " AND (r.rating != 0 OR r.fav != 0)").fetchall()
  out = {}
  for r in rows:
    out.setdefault(r["photo_id"], []).append(
        (r["hash"], r["rating"], r["fav"], r["last_path"]))
  return out


def _classify(conn):
  """(unique, ambiguous): {photo_id: (rating, fav, old_path)} and a list of
  {photo_id, reason, options}."""
  cands = _candidates(conn)
  by_hash = {}
  for pid, entries in cands.items():
    for h, *_ in entries:
      by_hash.setdefault(h, set()).add(pid)
  unique, ambiguous = {}, []
  for pid, entries in sorted(cands.items()):
    options = sorted({(r, f) for _, r, f, _ in entries})
    if len(options) > 1:
      ambiguous.append({"photo_id": pid, "reason": "files disagree",
                        "options": [{"rating": r, "fav": bool(f)}
                                    for r, f in options]})
    elif any(len(by_hash[h]) > 1 for h, *_ in entries):
      ambiguous.append({"photo_id": pid, "reason": "several photos share this content",
                        "options": [{"rating": options[0][0],
                                     "fav": bool(options[0][1])}]})
    else:
      unique[pid] = (options[0][0], options[0][1], entries[0][3])
  return unique, ambiguous


def ambiguous(conn):
  return _classify(conn)[1]


def recover(conn, settings):
  """Apply every unambiguous recovery. Returns the list of applied changes."""
  unique, _ = _classify(conn)
  applied = []
  for pid, (rating, fav, old_path) in unique.items():
    try:
      if rating:
        curation.set_rating(conn, settings, pid, rating, cause="hash-recovery")
      if fav:
        curation.set_fav(conn, settings, pid, True, cause="hash-recovery")
    except curation.CurationError:
      continue          # e.g. original missing; leave for a later pass
    if not settings.xmp_dry_run:
      conn.execute("UPDATE photos SET rating_source = 'hash-recovery' "
                       "WHERE id = ? AND rating_source = 'xmp'", (pid,))
      remember_photo(conn, pid)
    applied.append({"photo_id": pid, "rating": rating, "fav": bool(fav),
                    "from": old_path})
  conn.commit()
  return applied
