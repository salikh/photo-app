"""Read-only queries behind the browsing API: directories, Photo pages,
Photo detail. Paths are relative to the pictures dir with '/' separators.
"""

import json
import re

from photoapp import fileinfo
from photoapp import paths
from photoapp import raw_settings

FILTERS = ("all", "unrated", "rejected", "picked", "rated", "fav", "conflict")
RATING_FILTER_RE = re.compile(r"^rating:([1-5])$")    # exactly N stars
RATING_GE_RE = re.compile(r"^rating>=([1-5])$")       # N stars or more
RATING_LE_RE = re.compile(r"^rating<=([1-5])$")       # N stars or fewer
TAG_FILTER_RE = re.compile(r"^tag:(.+)$")            # exactly one tag, e.g. "tag:vacation"
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


def _is_dot_tag(name):
  """True if name is a 'tag:NAME' filter whose tag starts with a dot (ticket 123)."""
  m = TAG_FILTER_RE.match(name or "")
  return bool(m) and m.group(1).startswith('.')


def implied_tags(path):
  """Dot-prefixed directory names in path, e.g. 'a/.picasa/b.jpg' -> ['.picasa'].

  A file in a hidden directory is implicitly tagged with that directory's name even when no sidecar
  says so (ticket 123), so a dot-tag filter can unhide and match it.
  """
  return [seg for seg in path.split('/')[:-1] if seg.startswith('.')]


def filter_condition(name, one_star_is_unrated=False):
  """(sql, params) condition (on photos p) for a filter name; ValueError if unknown.

  Names: FILTERS, 'rating:N' for exactly N stars, 'rating>=N' for N or more and
  'rating<=N' for N or fewer (N in 1..5; the comparators are over the 1..5 star
  scale, so they exclude unrated (0) and rejected (-1), which have their own
  filters -- ticket 117), and 'tag:NAME' for exactly one tag (ticket 087). With
  one_star_is_unrated there is no 'rating:1'. A tag name is arbitrary free text
  (docs/design/databases.md's tags table has no controlled vocabulary), so it
  comes back as a '?' placeholder + param rather than embedded in the SQL string,
  unlike the fixed/rating conditions -- those never carry attacker-controlled text.
  """
  filters = _FILTER_SQL_ONE_STAR_UNRATED if one_star_is_unrated else _FILTER_SQL
  if name in filters:
    return filters[name], ()
  m = RATING_FILTER_RE.match(name or "")
  if m and not (one_star_is_unrated and m.group(1) == "1"):
    return f"p.rating = {int(m.group(1))}", ()
  m = RATING_GE_RE.match(name or "")
  if m:
    return f"p.rating >= {int(m.group(1))}", ()
  m = RATING_LE_RE.match(name or "")
  if m:
    return f"p.rating >= 1 AND p.rating <= {int(m.group(1))}", ()
  m = TAG_FILTER_RE.match(name or "")
  if m:
    tag = m.group(1)
    if tag.startswith('.'):
      # Ticket 123: a dot-tag also matches a file under a directory of that name (an implied tag).
      return ("(EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id AND t.tag = ?)"
              " OR instr('/' || rf.path || '/', '/' || ? || '/') > 0)"), (tag, tag)
    return "EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id AND t.tag = ?)", (tag,)
  raise ValueError(
      f"filter must be one of {FILTERS}, rating:1..5, rating>=1..5, rating<=1..5, or tag:NAME")


def _prefix_range(rel_dir):
  """(lo, hi) such that a path is under rel_dir iff lo <= path < hi.

  Range scans use the unique index on files.path; LIKE would not.
  """
  if rel_dir in ("", "."):
    return "", "\U0010ffff"
  return rel_dir + "/", rel_dir + "0"     # '0' is the character after '/'


def _scope_condition(rel_dir, recursive, include_hidden=False):
  """SQL condition (on files rf) + params selecting a Photo's representative file
  directly in rel_dir, or -- when recursive -- anywhere in its subtree.

  The recursive case excludes dot-prefixed subdirectories (ticket 054's "no dot
  folders in the folder view"), which the non-recursive case never has to think
  about: a Photo directly in rel_dir isn't under any nested subdirectory, dotted
  or not. list_photos and filter_counts both call this so their scope can't drift
  apart (docs/design/databases.md's note: a filter button's count must always
  equal what clicking it shows). include_hidden (ticket 123) drops the exclusion
  when the active filter is a dot-tag, so its hidden directories can be found.
  """
  lo, hi = _prefix_range(rel_dir)
  if recursive:
    sql = "rf.path >= ? AND rf.path < ?"
    args = (lo, hi)
    if not include_hidden:
      sql += " AND substr(rf.path, ?) NOT LIKE '.%/%' AND substr(rf.path, ?) NOT LIKE '%/.%/%'"
      args += (len(lo) + 1, len(lo) + 1)
    return sql, args
  return "rf.path >= ? AND rf.path < ? AND instr(substr(rf.path, ?), '/') = 0", (lo, hi, len(lo) + 1)


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
  crop = None
  if r["crop_x"] is not None:
    crop = {"x": r["crop_x"], "y": r["crop_y"], "w": r["crop_w"], "h": r["crop_h"]}
  return {
      "id": r["id"], "file_id": r["file_id"],
      "name": r["path"].rpartition("/")[2], "path": r["path"],
      "rating": r["rating"], "fav": bool(r["fav"]),
      "previous_stars": r["previous_stars"], "tags": tags, "conflict": bool(r["conflict"]),
      "files": r["nfiles"], "width": r["width"], "height": r["height"],
      "exif_date": r["exif_date"], "crop": crop, "rotation": r["rotation"] or 0,
      "rev": r["thumb_rev"],
      "implied": implied_tags(r["path"]),
  }


def list_photos(conn, rel_dir=".", sort="date", filter="all", offset=0,
                limit=200, one_star_is_unrated=False, recursive=False):
  """A page of Photos whose representative file is directly in rel_dir, or
  (recursive) anywhere in its subtree."""
  rel_dir = _norm_dir(rel_dir)
  if sort not in SORTS:
    raise ValueError(f"sort must be one of {SORTS}")
  filter_sql, filter_args = filter_condition(filter, one_star_is_unrated)
  limit = max(1, min(int(limit), 1000))
  offset = max(0, int(offset))
  scope_sql, scope_args = _scope_condition(rel_dir, recursive, _is_dot_tag(filter))
  where = scope_sql + " AND rf.missing = 0 AND " + filter_sql
  args = scope_args + filter_args
  order = ("COALESCE(rf.exif_date, datetime(rf.mtime, 'unixepoch')), rf.path"
           if sort == "date" else "rf.path COLLATE NOCASE")
  total = conn.execute(
      "SELECT COUNT(*) FROM photos p JOIN files rf ON rf.id = "
      "p.representative_file_id WHERE " + where, args).fetchone()[0]
  rows = conn.execute(
      "SELECT p.id, p.rating, p.fav, p.conflict, p.previous_stars, rf.id AS file_id, rf.path,"
      " rf.width, rf.height, rf.exif_date, rf.crop_x, rf.crop_y, rf.crop_w, rf.crop_h,"
      " rf.rotation, rf.thumb_rev,"
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


COUNT_FILTERS = ("all", "unrated", "rejected", "rating:1", "rating:2", "rating:3", "rating:4",
                 "rating:5", "rating>=1", "rating>=2", "rating>=3", "rating>=4", "rating>=5",
                 "rating<=1", "rating<=2", "rating<=3", "rating<=4", "rating<=5",
                 "picked", "rated", "fav", "conflict")


def filter_counts(conn, rel_dir=".", one_star_is_unrated=False, recursive=False):
  """How many Photos each filter shows in rel_dir (the ones the grid lists there).

  One query; the conditions and the recursive/non-recursive scope are the ones list_photos
  uses (via _scope_condition), so a button's count always equals what clicking it shows.
  With one_star_is_unrated there is no 'rating:1'.
  """
  rel_dir = _norm_dir(rel_dir)
  scope_sql, scope_args = _scope_condition(rel_dir, recursive)
  names = [n for n in COUNT_FILTERS if not (one_star_is_unrated and n == "rating:1")]
  sums = ", ".join(
      f"COALESCE(SUM(CASE WHEN {filter_condition(n, one_star_is_unrated)[0]} THEN 1 ELSE 0 END), 0)"
      for n in names)   # none of COUNT_FILTERS is a tag:NAME filter, so [0] never drops params
  row = conn.execute(
      f"SELECT {sums} FROM photos p JOIN files rf ON rf.id = p.representative_file_id "
      "WHERE " + scope_sql + " AND rf.missing = 0", scope_args).fetchone()
  return {"dir": rel_dir, "counts": dict(zip(names, row))}


def tags_in_view(conn, rel_dir=".", recursive=False):
  """Tags present on any Photo in rel_dir (ticket 087), with how many Photos each has.

  Scoped the same way as filter_counts (via _scope_condition) rather than the whole
  library, so the tag dropdown doesn't grow unbounded and agrees with what's on screen.
  A separate, lightweight query rather than folding into filter_counts: tags are an open
  set, so a COUNT_FILTERS-style one-SUM-per-tag query doesn't fit.
  """
  rel_dir = _norm_dir(rel_dir)
  scope_sql, scope_args = _scope_condition(rel_dir, recursive)
  rows = conn.execute(
      "SELECT t.tag, COUNT(*) AS n FROM tags t"
      " JOIN photos p ON p.id = t.photo_id"
      " JOIN files rf ON rf.id = p.representative_file_id"
      " WHERE " + scope_sql + " AND rf.missing = 0"
      " GROUP BY t.tag ORDER BY t.tag", scope_args).fetchall()
  return {"dir": rel_dir, "tags": [{"tag": r["tag"], "count": r["n"]} for r in rows]}


def _ai_answers(conn, file_id, ai_model):
  """The cached AI answers behind a file's score for the current prompt and `ai_model`, or None."""
  from photoapp import ai_rating      # local import: ai_rating pulls in the thumbnail machinery
  row = conn.execute("SELECT id, hash, thumb_rev FROM files WHERE id = ?", (file_id,)).fetchone()
  return ai_rating.answers_for(conn, ai_model, row)


def photo_detail(conn, photo_id, ai_model=None):
  """One Photo with all its files and sidecars, or None. ai_model (ticket 181) selects which
  model's cached AI answers go into each rated file's `ai_answers`."""
  p = conn.execute("SELECT * FROM photos WHERE id = ?",
                   (photo_id,)).fetchone()
  if p is None:
    return None
  files = [dict(r) for r in conn.execute(
      "SELECT id, path, role, derived_from, link_source, mime_type, width,"
      " height, bytesize, exif_date, aperture, shutter_speed, iso, focal_length,"
      " camera_make, camera_model, lens_model, focal_length_35mm, ai_score, crop_x, crop_y, crop_w, crop_h,"
      " rotation, thumb_rev, missing,"
      f" hash, exported_from_file_id, {', '.join(raw_settings.COLUMNS)} FROM files"
      " WHERE photo_id = ? ORDER BY (id = ?) DESC, path",
      (photo_id, p["original_file_id"]))]
  for f in files:
    f["ai_answers"] = _ai_answers(conn, f["id"], ai_model) if f["ai_score"] is not None else None
    f["is_raw"] = fileinfo.is_raw(f["path"])   # ticket 085: only a RAW file gets settings sliders
    # Ticket 099: "exported from" jump-to-original -- dir + photo_id of the source Photo, so the
    # frontend can build a link the same shape route.href already takes. Ticket 152: path too, for
    # the link's display text (dir alone read as "<dir> (photo <id>)", not a real path).
    if f["exported_from_file_id"] is not None:
      src = conn.execute("SELECT path, photo_id FROM files WHERE id = ?",
                         (f["exported_from_file_id"],)).fetchone()
      f["exported_from"] = (
          {"dir": paths.dirname(src["path"]), "photo_id": src["photo_id"], "path": src["path"]}
          if src is not None and src["photo_id"] is not None else None)
    else:
      f["exported_from"] = None
  sidecars = [
      {"path": r["path"], "rating": r["rating"], "fav": bool(r["has_fav"]),
       "tags": json.loads(r["tags"] or "[]"), "mtime": r["mtime"]}
      for r in conn.execute(
          "SELECT s.* FROM xmp_sidecars s JOIN files f ON f.id = s.file_id "
          "WHERE f.photo_id = ? ORDER BY s.path", (photo_id,))]
  original = next((f["path"] for f in files if f["id"] == p["original_file_id"]), "")
  return {
      "id": p["id"], "rating": p["rating"], "fav": bool(p["fav"]),
      "conflict": bool(p["conflict"]), "previous_stars": p["previous_stars"],
      "original_file_id": p["original_file_id"],
      "representative_file_id": p["representative_file_id"],
      "tags": sorted(r["tag"] for r in conn.execute(
          "SELECT tag FROM tags WHERE photo_id = ?", (photo_id,))),
      "implied": implied_tags(original),   # ticket 123: dot-directory names in the path
      "files": files, "sidecars": sidecars,
  }
