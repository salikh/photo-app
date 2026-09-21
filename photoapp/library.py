"""Read-only queries behind the browsing API: directories, Photo pages,
Photo detail. Paths are relative to the pictures dir with '/' separators.
"""

import json
import re

FILTERS = ("all", "unrated", "rejected", "picked", "rated", "fav", "conflict")
RATING_FILTER_RE = re.compile(r"^rating:([1-5])$")   # exactly N stars
SORTS = ("date", "name")

_FILTER_SQL = {
    "all": "1",
    "unrated": "p.rating = 0",
    "rejected": "p.rating = -1",
    "picked": "p.rating > 0",
    "rated": "p.rating != 0",
    "fav": "p.fav = 1",
    "conflict": "p.conflict = 1",
}

# With one_star_is_unrated (ticket 049) a 1-star rating counts as unrated.
_FILTER_SQL_ONE_STAR_UNRATED = dict(
    _FILTER_SQL,
    unrated="p.rating IN (0, 1)",
    picked="p.rating > 1",
    rated="p.rating NOT IN (0, 1)",
)


def filter_condition(name, one_star_is_unrated=False):
  """SQL condition (on photos p) for a filter name; ValueError if unknown.

  Names: FILTERS, and 'rating:N' for exactly N stars (N in 1..5). With
  one_star_is_unrated there is no 'rating:1' (1 star counts as unrated).
  """
  filters = _FILTER_SQL_ONE_STAR_UNRATED if one_star_is_unrated else _FILTER_SQL
  if name in filters:
    return filters[name]
  m = RATING_FILTER_RE.match(name or "")
  if m and not (one_star_is_unrated and m.group(1) == "1"):
    return f"p.rating = {int(m.group(1))}"
  raise ValueError(f"filter must be one of {FILTERS} or rating:1..5")


def _prefix_range(rel_dir):
  """(lo, hi) such that a path is under rel_dir iff lo <= path < hi.

  Range scans use the unique index on files.path; LIKE would not.
  """
  if rel_dir in ("", "."):
    return "", "\U0010ffff"
  return rel_dir + "/", rel_dir + "0"     # '0' is the character after '/'


def _norm_dir(rel_dir):
  rel_dir = (rel_dir or ".").strip("/")
  if any(part in ("..",) for part in rel_dir.split("/")):
    raise ValueError("invalid directory")
  return rel_dir or "."


def list_dirs(conn, rel_dir="."):
  """Child directories of rel_dir with Photo counts (whole subtree), and the
  number of Photos directly in rel_dir.

  Folders whose name starts with '.' (leftovers of old programs such as
  .thumbnails or .picasaoriginals) are not listed and their Photos are left
  out of the counts (ticket 054). They still exist and can be opened by
  path; only the directory part of a path is looked at, so a file whose own
  name starts with '.' is unaffected.
  """
  rel_dir = _norm_dir(rel_dir)
  lo, hi = _prefix_range(rel_dir)
  counts = {
      r["name"]: r["n"] for r in conn.execute(
          "SELECT substr(rest, 1, instr(rest, '/') - 1) AS name,"
          " COUNT(DISTINCT photo_id) n FROM ("
          " SELECT substr(f.path, ?) AS rest, f.photo_id FROM files f"
          " WHERE f.path >= ? AND f.path < ? AND f.missing = 0"
          "  AND f.photo_id IS NOT NULL) WHERE instr(rest, '/') > 0"
          "  AND rest NOT LIKE '.%/%' AND rest NOT LIKE '%/.%/%'"
          " GROUP BY name", (len(lo) + 1, lo, hi))
  }
  return {"path": rel_dir,
          "dirs": [{"name": n, "photos": counts[n]} for n in sorted(counts)],
          "photos": conn.execute(
              "SELECT COUNT(DISTINCT p.id) FROM photos p JOIN files rf ON "
              "rf.id = p.representative_file_id WHERE rf.path >= ? AND "
              "rf.path < ? AND instr(substr(rf.path, ?), '/') = 0 AND "
              "rf.missing = 0", (lo, hi, len(lo) + 1)).fetchone()[0]}


def _photo_json(r, tags):
  return {
      "id": r["id"], "file_id": r["file_id"],
      "name": r["path"].rpartition("/")[2], "path": r["path"],
      "rating": r["rating"], "fav": bool(r["fav"]),
      "previous_stars": r["previous_stars"], "tags": tags, "conflict": bool(r["conflict"]),
      "files": r["nfiles"], "width": r["width"], "height": r["height"],
      "exif_date": r["exif_date"],
  }


def list_photos(conn, rel_dir=".", sort="date", filter="all", offset=0,
                limit=200, one_star_is_unrated=False):
  """A page of Photos whose representative file is directly in rel_dir."""
  rel_dir = _norm_dir(rel_dir)
  if sort not in SORTS:
    raise ValueError(f"sort must be one of {SORTS}")
  filter_sql = filter_condition(filter, one_star_is_unrated)
  limit = max(1, min(int(limit), 1000))
  offset = max(0, int(offset))
  lo, hi = _prefix_range(rel_dir)
  where = ("rf.path >= ? AND rf.path < ? AND instr(substr(rf.path, ?), '/') = 0"
           " AND rf.missing = 0 AND " + filter_sql)
  args = (lo, hi, len(lo) + 1)
  order = ("COALESCE(rf.exif_date, datetime(rf.mtime, 'unixepoch')), rf.path"
           if sort == "date" else "rf.path COLLATE NOCASE")
  total = conn.execute(
      "SELECT COUNT(*) FROM photos p JOIN files rf ON rf.id = "
      "p.representative_file_id WHERE " + where, args).fetchone()[0]
  rows = conn.execute(
      "SELECT p.id, p.rating, p.fav, p.conflict, p.previous_stars, rf.id AS file_id, rf.path,"
      " rf.width, rf.height, rf.exif_date,"
      " (SELECT COUNT(*) FROM files x WHERE x.photo_id = p.id AND"
      "  x.missing = 0) AS nfiles"
      " FROM photos p JOIN files rf ON rf.id = p.representative_file_id"
      " WHERE " + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
      args + (limit, offset)).fetchall()
  ids = [r["id"] for r in rows]
  tags = {}
  if ids:
    for t in conn.execute(
        f"SELECT photo_id, tag FROM tags WHERE photo_id IN "
        f"({','.join('?' * len(ids))}) ORDER BY tag", ids):
      tags.setdefault(t["photo_id"], []).append(t["tag"])
  return {"dir": rel_dir, "total": total, "offset": offset,
          "photos": [_photo_json(r, tags.get(r["id"], [])) for r in rows]}


def photo_detail(conn, photo_id):
  """One Photo with all its files and sidecars, or None."""
  p = conn.execute("SELECT * FROM photos WHERE id = ?",
                   (photo_id,)).fetchone()
  if p is None:
    return None
  files = [dict(r) for r in conn.execute(
      "SELECT id, path, role, derived_from, link_source, mime_type, width,"
      " height, bytesize, exif_date, missing, hash FROM files"
      " WHERE photo_id = ? ORDER BY (id = ?) DESC, path",
      (photo_id, p["original_file_id"]))]
  sidecars = [
      {"path": r["path"], "rating": r["rating"], "fav": bool(r["has_fav"]),
       "tags": json.loads(r["tags"] or "[]"), "mtime": r["mtime"]}
      for r in conn.execute(
          "SELECT s.* FROM xmp_sidecars s JOIN files f ON f.id = s.file_id "
          "WHERE f.photo_id = ? ORDER BY s.path", (photo_id,))]
  return {
      "id": p["id"], "rating": p["rating"], "fav": bool(p["fav"]),
      "conflict": bool(p["conflict"]), "previous_stars": p["previous_stars"],
      "original_file_id": p["original_file_id"],
      "representative_file_id": p["representative_file_id"],
      "tags": sorted(r["tag"] for r in conn.execute(
          "SELECT tag FROM tags WHERE photo_id = ?", (photo_id,))),
      "files": files, "sidecars": sidecars,
  }
