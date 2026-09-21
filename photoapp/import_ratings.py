"""One-shot import of ratings from image_metadata.py's output database.

Usage:
  python -m photoapp.import_ratings --metadata_db=~/metadata.db \
      --state_dir=... [--write_xmp] [--xmp_dry_run]

Rules:
  * Every row is remembered in rating_by_hash (for rename recovery).
  * The newest rating wins (ticket 021). An imported rating carries the time it was
    originally set (image_metadata.py's rating_time, the mtime of the file it came from;
    0 when unknown) and is applied only if that is newer than the Photo's database time
    and its newest sidecar. Otherwise the existing rating is kept, and reported when it
    differs. An import without times therefore only fills Photos that have no dated
    rating from a sidecar or the app.
  * An applied rating is stored in the database with its own time (source 'import') and
    logged. Sidecars are written only with --write_xmp, and only for winning ratings.
"""

import dataclasses
import json
import sqlite3

from absl import app
from absl import flags
from absl import logging

from photoapp import config  # noqa: F401  (defines the shared flags)
from photoapp import curation
from photoapp import db as db_lib
from photoapp import ratings

FLAGS = flags.FLAGS
flags.DEFINE_string("metadata_db", None,
                    "Database written by image_metadata.py.")
flags.DEFINE_boolean("write_xmp", False,
                     "Also write imported ratings into sidecars.")


@dataclasses.dataclass
class Report:
  rows: int = 0
  unmatched_rows: int = 0
  matched_photos: int = 0
  applied: int = 0
  skipped_unrated: int = 0
  kept_existing: int = 0
  ambiguous: list = dataclasses.field(default_factory=list)
  disagreements: list = dataclasses.field(default_factory=list)


def _load_rows(metadata_db):
  conn = sqlite3.connect(f"file:{metadata_db}?mode=ro", uri=True)
  conn.row_factory = sqlite3.Row
  try:
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
    time_column = "rating_time" if "rating_time" in columns else "NULL"
    return [dict(r) for r in conn.execute(
        f"SELECT hash, filepath, rating, {time_column} AS rating_time, merged_paths "
        "FROM images")]
  finally:
    conn.close()


def import_ratings(conn, settings, metadata_db, write_xmp=False):
  report = Report()
  by_path, by_hash = {}, {}
  for f in conn.execute(
      "SELECT path, hash, photo_id FROM files WHERE photo_id IS NOT NULL"):
    by_path[f["path"]] = f["photo_id"]
    if f["hash"]:
      by_hash.setdefault(f["hash"], set()).add(f["photo_id"])

  # rating per Photo, from every row that reaches it
  wanted = {}   # photo_id -> {rating: [row filepaths]}
  for row in _load_rows(metadata_db):
    report.rows += 1
    rating = max(-1, min(5, int(row["rating"])))
    conn.execute(
        "INSERT INTO rating_by_hash (hash, rating, fav, last_path, rated_at) "
        "VALUES (?, ?, 0, ?, ?) ON CONFLICT(hash) DO UPDATE SET "
        "rating = excluded.rating, last_path = excluded.last_path, "
        "rated_at = excluded.rated_at",
        (row["hash"], rating, row["filepath"], row["rating_time"]))
    photos = set(by_hash.get(row["hash"], ()))
    for path in json.loads(row["merged_paths"]):
      if path in by_path:
        photos.add(by_path[path])
    if not photos:
      report.unmatched_rows += 1
      continue
    for pid in photos:
      group = wanted.setdefault(pid, {}).setdefault(rating, {"paths": [], "time": 0.0})
      group["paths"].append(row["filepath"])
      group["time"] = max(group["time"], row["rating_time"] or 0.0)

  for pid, by_rating in sorted(wanted.items()):
    report.matched_photos += 1
    if len(by_rating) > 1:
      report.ambiguous.append({"photo_id": pid, "ratings": sorted(by_rating)})
      continue
    (rating, group), = by_rating.items()
    paths, when = group["paths"], group["time"]
    if rating == 0:
      report.skipped_unrated += 1
      continue
    photo = conn.execute(
        "SELECT rating, rating_source, rating_updated_at FROM photos WHERE id = ?",
        (pid,)).fetchone()
    newest_sidecar = conn.execute(
        "SELECT MAX(s.mtime) FROM xmp_sidecars s JOIN files f ON f.id = s.file_id "
        "WHERE f.photo_id = ?", (pid,)).fetchone()[0] or 0.0
    existing = max(photo["rating_updated_at"] or 0.0, newest_sidecar)
    # nothing dated to compare with (no sidecar, no app decision): the import fills the gap
    fills_gap = existing == 0.0 and photo["rating_source"] not in ("xmp", "app")
    if not (when > existing or fills_gap):
      report.kept_existing += 1
      if photo["rating"] != rating:
        report.disagreements.append(
            {"photo_id": pid, "path": paths[0], "xmp_or_app": photo["rating"],
             "imported": rating, "source": photo["rating_source"],
             "imported_time": when or None, "existing_time": existing or None})
      continue
    if write_xmp:
      curation.set_rating(conn, settings, pid, rating, cause="import")
      # dry run leaves the database alone
    elif photo["rating"] != rating:
      conn.execute("UPDATE photos SET rating = ?, rating_source = 'import', "
                   "rating_updated_at = ? WHERE id = ?", (rating, when or None, pid))
      conn.execute(
          "INSERT INTO activity_log (ts, photo_id, xmp_path, field, old, new,"
          " cause) VALUES (?, ?, NULL, 'rating', ?, ?, 'import')",
          (curation._now(), pid, str(photo["rating"]), str(rating)))
    else:
      conn.execute("UPDATE photos SET rating_source = 'import', "
                   "rating_updated_at = COALESCE(?, rating_updated_at) WHERE id = ?",
                   (when or None, pid))
    ratings.refresh_photo(conn, pid)      # sets the conflict flag when the sidecars are behind
    report.applied += 1
  conn.commit()
  return report


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  if not FLAGS.metadata_db:
    raise app.UsageError("--metadata_db is required")
  settings = config.Settings.from_flags()
  conn = db_lib.open_state(settings.state_dir)
  report = import_ratings(conn, settings, FLAGS.metadata_db, FLAGS.write_xmp)
  logging.info(
      "rows=%d unmatched=%d photos=%d applied=%d skipped_unrated=%d "
      "kept_existing=%d ambiguous=%d disagreements=%d", report.rows,
      report.unmatched_rows, report.matched_photos, report.applied,
      report.skipped_unrated, report.kept_existing, len(report.ambiguous),
      len(report.disagreements))
  for d in report.disagreements[:50]:
    logging.info("disagreement: %s", d)


if __name__ == "__main__":
  app.run(main)
