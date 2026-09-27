#!/usr/bin/env python3
"""Compare a target copy's hash catalog against this machine's live library (ticket 135).

Classifies every (filename, hash) pair on a target copy -- an older backup on another machine, a
mounted drive, a disk image -- into exactly one bucket:

  have      the hash is already in this machine's live library (regardless of path -- a rename on
            either side is not a difference worth reporting, same principle as ticket 128's
            in-library move detection).
  rejected  the hash is not live here, but this machine's decisions say it was rejected (rating
            -1) -- an expected absence, not a problem. This is the case docs/tickets/131.md's
            Findings identified: rating_by_hash outlives the file, the trash, and the purge.
  lost      the hash is not live here, and this machine's decisions say it was a keeper (rating
            != -1, or favorited) -- missing some other way than a deliberate reject. Recoverable
            from the target copy; see tools/archive/apply.py (ticket 137).
  new       the hash is in neither -- this repo has never had an opinion on it. Candidate for
            import; see tools/archive/apply.py.

Inputs:
  --live        a JSONL export from `python -m photoapp.export_catalog` (ticket 134): this
                machine's own files table (non-missing) and rating_by_hash table.
  --target_db   a sqlite `hashes(filename, hash)` catalog of the target copy, in the format
                tools/archive/catalog.py writes (build one directly against a mounted target), or
                tools/archive/import_sha224sum.py writes (bootstrap one from an existing plain
                `sha224sum` listing, e.g. a backup that already has one -- see nas.txt).

--reverse asks the opposite question instead: which of this machine's own live hashes are absent
from the target copy at all? Useful for checking whether an old backup is safe to retire (every
bucket there is either "still only on this copy" or not); it does not distinguish rejected/lost/new
since those concepts are about the target, not about this machine.

Usage:
    python -m photoapp.export_catalog --output=live.jsonl
    tools/archive/catalog.py --root_dir=/mnt/backup/Pictures --db=backup.sqlite
    tools/archive/compare.py --live=live.jsonl --target_db=backup.sqlite > report.txt
"""

import json
import sqlite3
import sys

from absl import app
from absl import flags
from absl import logging

FLAGS = flags.FLAGS

flags.DEFINE_string("live", None, "This machine's export from python -m photoapp.export_catalog.")
flags.DEFINE_string("target_db", None,
                    "The target copy's hash catalog (tools/archive/catalog.py or "
                    "import_sha224sum.py format).")
flags.DEFINE_boolean(
    "reverse", False,
    "Report live hashes absent from the target copy, instead of target hashes classified "
    "against this machine.")
flags.mark_flag_as_required("live")
flags.mark_flag_as_required("target_db")

BUCKETS = ("have", "rejected", "lost", "new")


def load_live(path):
  """(file_hashes: set, decisions: {hash: {"rating":, "fav":, "rated_at":}}) from a ticket-134 export."""
  file_hashes = set()
  decisions = {}
  with open(path) as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      record = json.loads(line)
      if record["type"] == "file":
        file_hashes.add(record["hash"])
      elif record["type"] == "decision":
        decisions[record["hash"]] = {"rating": record["rating"], "fav": record["fav"],
                                     "rated_at": record["rated_at"]}
  return file_hashes, decisions


def load_target(db_path):
  """{filename: hash} from a catalog.py/import_sha224sum.py-format database."""
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  try:
    return dict(conn.execute("SELECT filename, hash FROM hashes"))
  finally:
    conn.close()


def classify(file_hash, file_hashes, decisions):
  if file_hash in file_hashes:
    return "have"
  decision = decisions.get(file_hash)
  if decision is None:
    return "new"
  return "rejected" if decision["rating"] == -1 else "lost"


def compare(target_files, file_hashes, decisions):
  """{bucket: [(filename, hash), ...]}, filename-sorted within each bucket."""
  buckets = {b: [] for b in BUCKETS}
  for filename, file_hash in sorted(target_files.items()):
    buckets[classify(file_hash, file_hashes, decisions)].append((filename, file_hash))
  return buckets


def compare_reverse(target_files, file_hashes):
  """Live hashes absent from the target copy at all, as a sorted list of hashes (a live hash may
  correspond to several paths on this machine; the report is about content, not any one path)."""
  target_hashes = set(target_files.values())
  return sorted(file_hashes - target_hashes)


def format_report(buckets):
  lines = []
  lines.append("# tools/archive/compare.py report")
  for b in BUCKETS:
    lines.append(f"# {b}: {len(buckets[b])}")
  lines.append("#")
  lines.append("# bucket\tpath\thash")
  for b in BUCKETS:
    for filename, file_hash in buckets[b]:
      lines.append(f"{b}\t{filename}\t{file_hash}")
  return "\n".join(lines) + "\n"


def format_reverse_report(only_live_hashes):
  lines = [f"# tools/archive/compare.py --reverse report: {len(only_live_hashes)} live hash(es) "
          "absent from the target copy", "#"]
  lines.extend(only_live_hashes)
  return "\n".join(lines) + "\n"


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  file_hashes, decisions = load_live(FLAGS.live)
  target_files = load_target(FLAGS.target_db)
  logging.info("live: %d file(s), %d decision(s); target: %d file(s)",
              len(file_hashes), len(decisions), len(target_files))

  if FLAGS.reverse:
    report = format_reverse_report(compare_reverse(target_files, file_hashes))
  else:
    buckets = compare(target_files, file_hashes, decisions)
    report = format_report(buckets)
    for b in BUCKETS:
      logging.info("%s: %d", b, len(buckets[b]))

  sys.stdout.write(report)


if __name__ == "__main__":
  app.run(main)
