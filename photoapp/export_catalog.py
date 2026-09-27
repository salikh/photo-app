"""Export this library's catalog and durable per-hash decisions (ticket 134).

  python -m photoapp.export_catalog [--state_dir=...] > catalog.jsonl
  python -m photoapp.export_catalog --output=catalog.jsonl

Read-only, and cheap: a single scan of two already-small tables (thousands of
rows, not the millions of file bytes a hash catalog of the library itself
would be), so it is fine to re-run before every comparison against another
copy (tools/archive/compare.py, ticket 135) to pick up whatever has changed
here since the last export.

One JSON object per line, each tagged by "type":

  {"type": "file", "path": "...", "hash": "...", "bytesize": N}
      One row per non-missing `files` row: what this machine currently has,
      and where. A file whose content is duplicated under several paths
      (a RAW+JPEG pair, an export, a manual copy) appears once per path --
      the hash is what identifies "the same content," not the row count.

  {"type": "decision", "hash": "...", "rating": N, "fav": bool, "rated_at": T|null}
      One row per `rating_by_hash` entry: hash -> the last rating/favorite
      this app ever recorded for that content, independent of whether a live
      file with that hash still exists (rating -1 = rejected; this table has
      no foreign key onto `files` and is never pruned, so it durably answers
      "was an absence here a decision" even after the file itself was moved
      to .trash/ and purged -- see docs/tickets/131.md's Findings).

A trailing {"type": "summary", ...} line gives counts, mostly so a shell
pipeline consuming this file has something cheap to sanity-check against
without counting lines itself.
"""

import json
import sys

from absl import app
from absl import flags

from photoapp import config  # noqa: F401  (flags)
from photoapp import db

FLAGS = flags.FLAGS

flags.DEFINE_string(
    "output", None,
    "Where to write the catalog. Default: stdout (so it composes with a pipeline or shell "
    "redirect the way every other report-only tool in this codebase does).")


def export_files(conn):
  """{"type": "file", ...} dicts, one per non-missing files row, oldest path first (stable order,
  handy for diffing two exports of the same, unchanged library by eye)."""
  for r in conn.execute(
      "SELECT path, hash, bytesize FROM files WHERE missing = 0 AND hash IS NOT NULL "
      "ORDER BY path"):
    yield {"type": "file", "path": r["path"], "hash": r["hash"], "bytesize": r["bytesize"]}


def export_decisions(conn):
  """{"type": "decision", ...} dicts, one per rating_by_hash row."""
  for r in conn.execute(
      "SELECT hash, rating, fav, rated_at FROM rating_by_hash ORDER BY hash"):
    yield {"type": "decision", "hash": r["hash"], "rating": r["rating"], "fav": bool(r["fav"]),
           "rated_at": r["rated_at"]}


def export(conn):
  """Yield every record, files then decisions, ending with a summary."""
  files = decisions = 0
  for record in export_files(conn):
    files += 1
    yield record
  for record in export_decisions(conn):
    decisions += 1
    yield record
  yield {"type": "summary", "files": files, "decisions": decisions}


def write(conn, f):
  for record in export(conn):
    f.write(json.dumps(record, sort_keys=True))
    f.write("\n")


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  conn = db.connect(config.Settings.load(check_pictures_dir=False).db_path)
  if FLAGS.output:
    with open(FLAGS.output, "w") as f:
      write(conn, f)
  else:
    write(conn, sys.stdout)


if __name__ == "__main__":
  app.run(main)
