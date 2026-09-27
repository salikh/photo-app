"""Pre-write survey: how often do a Photo's sidecars disagree?

  python -m photoapp.survey [--state_dir=...]

Read-only. Answers the question behind "newest sidecar wins": before the
app writes anything, how many Photos have sidecars that disagree, and what
would the rule decide for them?
"""

import collections
import json

from absl import app
from absl import flags

from photoapp import config  # noqa: F401  (flags)
from photoapp import db
from photoapp import ratings

FLAGS = flags.FLAGS
SAMPLE = 15


def survey(conn):
  """Return the survey as a plain dict."""
  by_photo = collections.defaultdict(list)
  for r in conn.execute(
      "SELECT f.photo_id, s.file_id, s.path, s.mtime, s.rating, s.has_fav, s.tags "
      "FROM xmp_sidecars s JOIN files f ON f.id = s.file_id "
      "WHERE f.photo_id IS NOT NULL ORDER BY s.path"):
    by_photo[r["photo_id"]].append(dict(r, tags=json.loads(r["tags"] or "[]")))

  totals = conn.execute(
      "SELECT (SELECT COUNT(*) FROM photos) photos, "
      "(SELECT COUNT(*) FROM files WHERE missing = 0) files, "
      "(SELECT COUNT(*) FROM xmp_sidecars) sidecars, "
      "(SELECT COUNT(*) FROM xmp_sidecars WHERE file_id IS NULL) orphans, "
      "(SELECT COUNT(*) FROM (SELECT photo_id FROM files WHERE missing = 0 "
      " GROUP BY photo_id HAVING COUNT(*) > 1)) multi_file").fetchone()
  report = {"totals": dict(totals), "sidecar_count_per_photo": collections.Counter(),
            "rating_distribution": collections.Counter(),
            "conflicts": 0, "rating_conflicts": 0, "fav_conflicts": 0,
            "sidecars_behind": 0, "rejects_overruled": 0,
            "newest_is_original": 0, "newest_is_other": 0,
            "disagreement_pairs": collections.Counter(), "samples": []}
  for pid, sidecars in by_photo.items():
    report["sidecar_count_per_photo"][len(sidecars)] += 1
    resolved = ratings.resolve(sidecars, ratings.db_source(conn, pid))
    report["rating_distribution"][resolved.rating] += 1
    report["rejects_overruled"] += resolved.reject_overruled
    report["sidecars_behind"] += resolved.db_wins and resolved.conflict
    if not resolved.conflict:
      continue
    report["conflicts"] += 1
    rated = {ratings.clamp(s["rating"]) for s in sidecars if s["rating"] is not None}
    favs = {bool(s["has_fav"]) for s in sidecars}
    report["rating_conflicts"] += len(rated) > 1
    report["fav_conflicts"] += len(favs) > 1
    if len(rated) > 1:
      report["disagreement_pairs"][tuple(sorted(rated))] += 1
    photo = conn.execute(
        "SELECT p.original_file_id, f.path FROM photos p JOIN files f ON "
        "f.id = p.original_file_id WHERE p.id = ?", (pid,)).fetchone()
    original = photo["path"]
    newest = max(sidecars, key=lambda s: (s["mtime"], s["path"]))
    if newest["file_id"] == photo["original_file_id"]:
      report["newest_is_original"] += 1
    else:
      report["newest_is_other"] += 1
    if len(report["samples"]) < SAMPLE:
      report["samples"].append({
          "original": original, "decided": resolved.rating,
          "sidecars": [(s["path"].rpartition("/")[2], s["rating"], bool(s["has_fav"]), s["mtime"])
                       for s in sidecars]})
  return report


def format_report(r):
  t = r["totals"]
  lines = [
      f"Photos: {t['photos']}   files: {t['files']}   photos with several files: {t['multi_file']}",
      f"Sidecars: {t['sidecars']}   orphan sidecars (no picture): {t['orphans']}",
      "Sidecars per photo: " + ", ".join(f"{k}: {v}" for k, v in sorted(r["sidecar_count_per_photo"].items())),
      "Resolved rating: " + ", ".join(
          f"{'none' if k is None else k}: {v}" for k, v in sorted(
              r["rating_distribution"].items(), key=lambda kv: (kv[0] is None, kv[0]))),
      f"Photos whose sidecars disagree: {r['conflicts']} "
      f"(rating: {r['rating_conflicts']}, fav: {r['fav_conflicts']})",
      f"Sidecars behind a newer database rating: {r['sidecars_behind']}   "
      f"single-file rejects overruled by a picked file: {r['rejects_overruled']}",
      f"  newest sidecar is the original's: {r['newest_is_original']}, another file's: {r['newest_is_other']}",
      "  most common rating disagreements: " + ", ".join(
          f"{a}: {n}" for a, n in r["disagreement_pairs"].most_common(8)),
  ]
  for s in r["samples"]:
    lines.append(f"  e.g. {s['original']} -> {s['decided']}: " + "; ".join(
        f"{n} rating={rt}{' fav' if f else ''}" for n, rt, f, _ in s["sidecars"]))
  return "\n".join(lines)


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  conn = db.connect(config.Settings.load(check_pictures_dir=False).db_path)
  print(format_report(survey(conn)))


if __name__ == "__main__":
  app.run(main)
