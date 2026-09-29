"""One-off backfill (ticket 100): connect exported files already sitting in pictures_dir's
Exported subtree to the photo they came from, for exports that predate ticket 099's real-time
linking. Run once, from the command line:

  python -m photoapp.export_backfill [--state_dir=...] [--dhash_distance=10]

Not a standing job -- ticket 099 already links every export made from here on, the moment its
job finishes.

Tier 1 (backfill_from_job_log): still-live 'export' job rows (jobs.DONE_RETENTION_DAYS = 7, so
this only ever covers very recent app-driven exports) carry the exact source file_id and the
original destination path -- translated to wherever the user has since moved it (the 2026-09-24
manual move from /zoo/Exported into pictures_dir/Exported, preserving relative structure). Exact,
no heuristics.

Tier 2 (backfill_by_dhash): for everything else already under pictures_dir/Exported with no link
yet (older app-driven exports whose job rows are already pruned, and whatever pre-dated this app
entirely), narrow candidate originals by filename, then confirm with a dhash (difference hash)
comparison -- an export is a re-encoded, possibly resized copy, so this looks for a close, not
exact, match, computed against whatever preview size the candidate already has cached (never
forces a fresh RAW render just to check). Only links when exactly one candidate is within the
distance threshold; leaves it unlinked rather than guessing otherwise.
"""

import os
import re

from absl import app
from absl import flags
from absl import logging
import logging as py_logging
from PIL import Image

from photoapp import config  # noqa: F401  (defines the shared flags)
from photoapp import db
from photoapp import export
from photoapp import thumbs

FLAGS = flags.FLAGS
flags.DEFINE_integer(
    "dhash_distance", 10, "Max Hamming distance (out of 64 bits) for a tier-2 dhash match to be "
    "trusted. Lower is stricter.")

_SUFFIX_RE = re.compile(r"-\d+$")   # strips export.resolve_dest_path's own collision suffix


def backfill_from_job_log(conn, pictures_dir, old_export_root=None):
  """Tier 1. Returns (matched, checked)."""
  pictures_real = os.path.normpath(pictures_dir)
  if old_export_root is None:
    old_export_root = os.path.join(os.path.dirname(pictures_real), "Exported")
  new_export_root = os.path.join(pictures_real, export.EXPORT_SUBDIR)
  matched = checked = 0
  for job in conn.execute(
      "SELECT file_id, target FROM jobs WHERE kind = 'export' AND state = 'done' "
      "AND target IS NOT NULL"):
    checked += 1
    target = job["target"]
    if target == old_export_root or target.startswith(old_export_root + os.sep):
      moved = new_export_root + target[len(old_export_root):]
    elif target == new_export_root or target.startswith(new_export_root + os.sep):
      moved = target   # already at the new location (a 098/099-era job)
    else:
      continue          # a custom target elsewhere -- not this app's Exported tree at all
    moved_real = os.path.normpath(moved)
    if not os.path.exists(moved_real):
      continue
    if moved_real != pictures_real and not moved_real.startswith(pictures_real + os.sep):
      continue
    rel = os.path.relpath(moved_real, pictures_real).replace(os.sep, "/")
    row = conn.execute(
        "SELECT id, exported_from_file_id FROM files WHERE path = ? AND missing = 0",
        (rel,)).fetchone()
    if row is None or row["exported_from_file_id"] is not None:
      continue   # not scanned (yet), or already linked (e.g. by 099)
    source = conn.execute("SELECT id FROM files WHERE id = ? AND missing = 0",
                          (job["file_id"],)).fetchone()
    if source is None:
      continue   # source file itself is gone
    conn.execute("UPDATE files SET exported_from_file_id = ? WHERE id = ?",
                (job["file_id"], row["id"]))
    matched += 1
    logging.vlog(5, "tier 1: %s <- job's source file %d", rel, job["file_id"])
  conn.commit()
  return matched, checked


def _candidate_stems(name):
  stem = os.path.splitext(name)[0].lower()
  yield stem
  stripped = _SUFFIX_RE.sub("", stem)
  if stripped != stem:
    yield stripped


def _dhash(path, hash_size=8):
  """Difference hash: hash_size**2 bits, each "is this pixel darker than its right neighbor" on
  a tiny grayscale resize -- robust to the resizing/re-encoding an export applies, not to a crop
  or rotation. Returns a list of bools."""
  with Image.open(path) as img:
    small = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(small.get_flattened_data())
  bits = []
  for row in range(hash_size):
    offset = row * (hash_size + 1)
    for col in range(hash_size):
      bits.append(pixels[offset + col] > pixels[offset + col + 1])
  return bits


def _hamming(a, b):
  return sum(x != y for x, y in zip(a, b))


def backfill_by_dhash(conn, pictures_dir, thumbs_dir, distance_threshold=10, dir_prefix=None):
  """Tier 2. dir_prefix (ticket 146) scopes `unresolved` to one directory (e.g. right after a
  move lands files there) instead of the whole Exported/ subtree -- candidates (library_rows)
  stay unscoped either way, since an export's original can be anywhere in the library, not just
  near where the export landed. Returns (matched, unmatched)."""
  exported_prefix = export.EXPORT_SUBDIR + "/"
  library_rows = conn.execute(
      "SELECT id, path FROM files WHERE missing = 0 AND path != ? AND path NOT LIKE ?",
      (export.EXPORT_SUBDIR, exported_prefix + "%")).fetchall()
  by_stem = {}
  for row in library_rows:
    for stem in _candidate_stems(os.path.basename(row["path"])):
      by_stem.setdefault(stem, []).append(row)

  if dir_prefix is None:
    scope_sql, scope_args = "(path = ? OR path LIKE ?)", (export.EXPORT_SUBDIR, exported_prefix + "%")
  else:
    scope_sql, scope_args = "(path = ? OR path LIKE ?)", (dir_prefix, dir_prefix + "/%")
  unresolved = conn.execute(
      "SELECT id, path FROM files WHERE missing = 0 AND exported_from_file_id IS NULL "
      f"AND {scope_sql}", scope_args).fetchall()

  matched = unmatched = 0
  for r in unresolved:
    candidates = {}
    for stem in _candidate_stems(os.path.basename(r["path"])):
      for c in by_stem.get(stem, []):
        candidates[c["id"]] = c["path"]
    if not candidates:
      unmatched += 1
      logging.vlog(5, "tier 2: %s: no filename candidate", r["path"])
      continue
    try:
      exported_hash = _dhash(os.path.join(pictures_dir, r["path"]))
    except Exception as e:
      unmatched += 1
      logging.warning("tier 2: %s: could not hash: %s", r["path"], e)
      continue
    results = []
    for cid, cpath in candidates.items():
      found = thumbs.best_available(thumbs_dir, "Small", cpath)
      if not found:
        continue   # never force a fresh RAW render just to check a candidate
      try:
        results.append((cid, _hamming(exported_hash, _dhash(found[1]))))
      except Exception:
        continue
    if not results:
      unmatched += 1
      continue
    results.sort(key=lambda t: t[1])
    best_id, best_dist = results[0]
    ambiguous = len(results) > 1 and results[1][1] == best_dist
    if not ambiguous and best_dist <= distance_threshold:
      conn.execute("UPDATE files SET exported_from_file_id = ? WHERE id = ?",
                  (best_id, r["id"]))
      matched += 1
      logging.vlog(5, "tier 2: %s <- file %d (distance %d)", r["path"], best_id, best_dist)
    else:
      unmatched += 1
  conn.commit()
  return matched, unmatched


def run(conn, pictures_dir, thumbs_dir, distance_threshold=10, old_export_root=None):
  """Runs both tiers and logs a summary. Returns {tier1_matched, tier1_checked, tier2_matched,
  tier2_unmatched}."""
  tier1_matched, tier1_checked = backfill_from_job_log(conn, pictures_dir, old_export_root)
  tier2_matched, tier2_unmatched = backfill_by_dhash(conn, pictures_dir, thumbs_dir,
                                                      distance_threshold)
  summary = {"tier1_matched": tier1_matched, "tier1_checked": tier1_checked,
             "tier2_matched": tier2_matched, "tier2_unmatched": tier2_unmatched}
  logging.info(
      "export_backfill: tier 1 matched %d of %d still-live export jobs; "
      "tier 2 matched %d, left %d unmatched",
      tier1_matched, tier1_checked, tier2_matched, tier2_unmatched)
  return summary


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  for module in ["TiffImagePlugin.py"]:
    # Set the noisy module to WARNING or higher to silence its DEBUG/INFO logs
    py_logging.getLogger(module.strip()).setLevel(logging.WARNING)
  settings = config.Settings.load()
  conn = db.connect(settings.db_path, busy_timeout=120.0)
  run(conn, settings.pictures_dir, settings.thumbs_dir, FLAGS.dhash_distance)


if __name__ == "__main__":
  app.run(main)
