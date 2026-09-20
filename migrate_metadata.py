#!/usr/bin/env python3
"""Migrate a metadata.db (see image_metadata.py) to the single-rating schema.

Old schema: 'rating' held the star rating 0-5 and 'reject' (-1/0/1) held
the reject/pick status, so a rejected image had rating 0 and reject -1.

New schema: 'rating' carries everything in one combined value
(-1 = REJECT, 0 = UNRATED, 1-5 = PICK / STAR RATING), and 'reject' is
kept as a value derived from it:

    reject = -1 if rating == -1
    reject =  0 if rating == 0
    reject =  1 if rating > 0

Rows are migrated as follows:
    reject == -1        -> rating = -1
    otherwise           -> rating is kept (clamped to [0, 5])
and then 'reject' is recomputed from the new rating. The migration is
idempotent: running it on an already-migrated database changes nothing.

Rows whose old columns were inconsistent (e.g. reject == 1 but
rating == 0, i.e. a pick with no stars) are logged as warnings; their
rating stays 0, so their 'reject' becomes 0.

Usage:
    migrate_metadata.py --database metadata.db
    migrate_metadata.py --database metadata.db --output_db metadata_new.db
"""

import shutil
import sqlite3

from absl import app
from absl import flags
from absl import logging

FLAGS = flags.FLAGS

flags.DEFINE_string(
    "database", None, "Path to the metadata sqlite3 database to migrate.")
flags.DEFINE_string(
    "output_db", None,
    "If given, copy --database here and migrate the copy, leaving "
    "--database untouched. Otherwise --database is migrated in place.")
flags.mark_flag_as_required("database")


def migrate(db_path):
    conn = sqlite3.connect(db_path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(images)")}
        if not {'rating', 'reject'} <= columns:
            raise app.UsageError(
                "%s has no images(rating, reject) columns; is it a "
                "metadata.db produced by image_metadata.py?" % db_path)

        inconsistent = conn.execute(
            "SELECT COUNT(*) FROM images "
            "WHERE (reject = 1 AND rating <= 0) "
            "   OR (reject = 0 AND rating > 0) "
            "   OR (reject = -1 AND rating > 0)").fetchone()[0]
        if inconsistent:
            logging.warning(
                "%d rows have inconsistent rating/reject values; "
                "rating is kept unless reject == -1", inconsistent)

        with conn:  # single transaction
            conn.execute(
                "UPDATE images SET rating = -1 WHERE reject = -1")
            conn.execute(
                "UPDATE images SET rating = 0 WHERE rating < -1 "
                "OR (rating = -1 AND reject != -1)")
            conn.execute(
                "UPDATE images SET rating = 5 WHERE rating > 5")
            conn.execute(
                "UPDATE images SET reject = CASE "
                "WHEN rating < 0 THEN -1 WHEN rating = 0 THEN 0 ELSE 1 END")

        counts = conn.execute(
            "SELECT rating, COUNT(*) FROM images GROUP BY rating "
            "ORDER BY rating").fetchall()
        logging.info("migrated %s; rating histogram: %s", db_path,
                     ", ".join(f"{r}={n}" for r, n in counts))
    finally:
        conn.close()


def main(argv):
    if len(argv) != 1:
        raise app.UsageError(
            "This tool takes no positional arguments; use --database "
            "instead (got: %s)" % argv[1:])

    target = FLAGS.database
    if FLAGS.output_db:
        shutil.copyfile(FLAGS.database, FLAGS.output_db)
        target = FLAGS.output_db
    migrate(target)


if __name__ == "__main__":
    app.run(main)
