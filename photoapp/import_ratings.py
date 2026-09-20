"""One-shot import of ratings from image_metadata.py's output database.

Usage:
  python -m photoapp.import_ratings --metadata_db=~/metadata.db \
      --state_dir=... [--write_xmp] [--xmp_dry_run]

Rules:
  * Every row is remembered in rating_by_hash (for rename recovery).
  * A Photo whose rating already comes from XMP or was set in the app is
    never changed; if it differs from the import, it is reported.
  * Otherwise the imported rating is stored in the database (source
    'import') and logged. Sidecars are written only with --write_xmp.
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
    return [dict(r) for r in conn.execute(
        "SELECT hash, filepath, rating, merged_paths FROM images")]
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
        "INSERT INTO rating_by_hash (hash, rating, fav, last_path) "
        "VALUES (?, ?, 0, ?) ON CONFLICT(hash) DO UPDATE SET "
        "rating = excluded.rating, last_path = excluded.last_path",
        (row["hash"], rating, row["filepath"]))
    photos = set(by_hash.get(row["hash"], ()))
    for path in json.loads(row["merged_paths"]):
      if path in by_path:
        photos.add(by_path[path])
    if not photos:
      report.unmatched_rows += 1
      continue
    for pid in photos:
      wanted.setdefault(pid, {}).setdefault(rating, []).append(row["filepath"])

  for pid, ratings in sorted(wanted.items()):
    report.matched_photos += 1
    if len(ratings) > 1:
      report.ambiguous.append({"photo_id": pid, "ratings": sorted(ratings)})
      continue
    (rating, paths), = ratings.items()
    if rating == 0:
      report.skipped_unrated += 1
      continue
    photo = conn.execute("SELECT rating, rating_source FROM photos "
                         "WHERE id = ?", (pid,)).fetchone()
    if photo["rating_source"] in ("xmp", "app"):
      report.kept_existing += 1
      if photo["rating"] != rating:
        report.disagreements.append(
            {"photo_id": pid, "path": paths[0], "xmp_or_app": photo["rating"],
             "imported": rating, "source": photo["rating_source"]})
      continue
    if write_xmp:
      curation.set_rating(conn, settings, pid, rating, cause="import")
      # dry run leaves the database alone
    elif photo["rating"] != rating:
      conn.execute("UPDATE photos SET rating = ?, rating_source = 'import' "
                   "WHERE id = ?", (rating, pid))
      conn.execute(
          "INSERT INTO activity_log (ts, photo_id, xmp_path, field, old, new,"
          " cause) VALUES (?, ?, NULL, 'rating', ?, ?, 'import')",
          (curation._now(), pid, str(photo["rating"]), str(rating)))
    else:
      conn.execute("UPDATE photos SET rating_source = 'import' WHERE id = ?",
                   (pid,))
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
