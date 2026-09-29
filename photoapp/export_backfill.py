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
distance threshold; leaves it unlinked rather than guessing otherwise. Ticket 151: measured
against the real library, the single largest bucket of unresolved files is an *exact* dhash tie
between a RAW file and its sibling JPEG (both equally close, since a RAW's embedded/rendered
preview and its sibling JPEG often look identical to a difference hash) -- a tie is broken in
favor of whichever tied candidate's filename matches the export's exactly, case-insensitively
(extension included), since an export is made from the already-rendered file, not the RAW. Never
overrides a worse distance -- only breaks a tie the hash already couldn't, and only when the
matched candidate's mtime is also plausibly close to the export's own (or they share a year-like
path component) -- a guard against a coincidental name+dhash tie between two unrelated photos from
different eras (see _dates_plausible; the mtime side of this is calibrated against the real
library's own already-linked pairs, not guessed).
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
from photoapp import fileinfo
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
  # Ticket 153: "xyz.DNG.jpg" -- a JPEG export literally named after its RAW sibling's full
  # filename, appending .jpg rather than replacing the extension (a real, common convention in
  # this library) -- splitext above only strips the outer .jpg, leaving "xyz.dng", which never
  # equals "xyz" (xyz.DNG/xyz.JPG's own stem). Strip a RAW extension found there too, so it ties.
  inner_stem, inner_ext = os.path.splitext(stem)
  if inner_ext in fileinfo.RAW_EXTENSIONS:
    yield inner_stem


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


def _name_matches(export_name, candidate_name):
  """True if candidate_name (a tied dhash candidate) is what named the export, ticket 151/153:
  either the same name, case-insensitively (an export made from the already-rendered file usually
  keeps its name) or candidate_name + ".jpg" case-insensitively (the "xyz.DNG.jpg" convention --
  a JPEG export literally named after its RAW sibling's full filename)."""
  export_name, candidate_name = export_name.lower(), candidate_name.lower()
  return export_name == candidate_name or export_name == candidate_name + ".jpg"


# Ticket 151: how close in date two files need to be for a name-match tie-break to trust them as
# the same photo -- measured against the real library's 3086 already-linked pairs (script, not
# committed): mtime differences are tightly bimodal, 95.9% within 1 day (the vast majority
# essentially the same moment -- an export made right after, or copied/migrated together with its
# source) and 98.5% within a year; the remaining ~1.5% (up to ~4 years) are real correct links
# whose mtime drifted for unrelated reasons (a re-save, a re-copy). A year is generous enough not
# to reject those while still rejecting a coincidental match between clearly different eras.
_DATE_CLOSE_SECONDS = 365 * 86400
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _dates_plausible(export_mtime, candidate_mtime, export_path, candidate_path):
  """True if export_mtime/candidate_mtime are within _DATE_CLOSE_SECONDS of each other, or their
  paths share a 4-digit year-like component. Checked empirically before relying on either signal
  (ticket 151): mtime closeness is the reliable one for this library (see _DATE_CLOSE_SECONDS);
  comparing path year-folders (either export-path-to-candidate-path, or export's own mtime-year to
  the candidate's path) each matched only ~3-4% of the same known-correct pairs -- this library's
  mtimes and Exported/ paths mostly reflect when a file was copied/migrated, not the photo's
  original capture date, so an Exported/2018-.../2012/... export folder rarely lines up with its
  2012/... source folder's year. Kept as a fallback OR anyway (the user's own suggested second
  signal, and it costs nothing when it doesn't fire), not the primary check."""
  if abs(export_mtime - candidate_mtime) <= _DATE_CLOSE_SECONDS:
    return True
  export_years = set(_YEAR_RE.findall(export_path))
  candidate_years = set(_YEAR_RE.findall(candidate_path))
  return bool(export_years & candidate_years)


def backfill_by_dhash(conn, pictures_dir, thumbs_dir, distance_threshold=10, dir_prefix=None):
  """Tier 2. dir_prefix (ticket 146) scopes `unresolved` to one directory (e.g. right after a
  move lands files there) instead of the whole Exported/ subtree -- candidates (library_rows)
  stay unscoped either way, since an export's original can be anywhere in the library, not just
  near where the export landed. Returns (matched, unmatched)."""
  exported_prefix = export.EXPORT_SUBDIR + "/"
  library_rows = conn.execute(
      "SELECT id, path, mtime FROM files WHERE missing = 0 AND path != ? AND path NOT LIKE ?",
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
      "SELECT id, path, mtime FROM files WHERE missing = 0 AND exported_from_file_id IS NULL "
      f"AND {scope_sql}", scope_args).fetchall()

  matched = unmatched = 0
  for r in unresolved:
    candidates = {}
    for stem in _candidate_stems(os.path.basename(r["path"])):
      for c in by_stem.get(stem, []):
        candidates[c["id"]] = (c["path"], c["mtime"])
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
    for cid, (cpath, cmtime) in candidates.items():
      found = thumbs.best_available(thumbs_dir, "Small", cpath)
      if not found:
        continue   # never force a fresh RAW render just to check a candidate
      try:
        results.append((cid, cpath, cmtime, _hamming(exported_hash, _dhash(found[1]))))
      except Exception:
        continue
    if not results:
      unmatched += 1
      continue
    results.sort(key=lambda t: t[3])
    best_dist = results[0][3]
    tied = [t for t in results if t[3] == best_dist]
    best_id, best_path = results[0][0], results[0][1]
    ambiguous = len(tied) > 1
    if ambiguous:
      # Ticket 151/153: a real, common case among exact ties -- a RAW file and its sibling JPEG
      # both equally close to the export, but only one of them is what the export's own filename
      # points at, per _name_matches (either an exact match -- an export made from the
      # already-rendered JPEG usually keeps its name -- or the "xyz.DNG.jpg" convention, which
      # points at the RAW instead). Still gated by the dhash distance below -- this only ever
      # resolves a tie between candidates the hash already couldn't tell apart, never overrides a
      # worse distance -- and by _dates_plausible, a guard against a coincidental name+dhash tie
      # between two unrelated photos from different eras (not observed in the real data, but cheap
      # to guard).
      export_name = os.path.basename(r["path"])
      name_matches = [t for t in tied if _name_matches(export_name, os.path.basename(t[1]))
                      and _dates_plausible(r["mtime"], t[2], r["path"], t[1])]
      if len(name_matches) == 1:
        ambiguous = False
        best_id, best_path = name_matches[0][0], name_matches[0][1]
    if not ambiguous and best_dist <= distance_threshold:
      conn.execute("UPDATE files SET exported_from_file_id = ? WHERE id = ?",
                  (best_id, r["id"]))
      matched += 1
      logging.vlog(5, "tier 2: %s <- file %d (%s, distance %d)",
                   r["path"], best_id, best_path, best_dist)
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
