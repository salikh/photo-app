"""Batch-sync sidecars that are out of sync with the computed rating (ticket 068).

  python -m photoapp.sync_sidecars [--state_dir=...] [--limit=N]      # preview only
  python -m photoapp.sync_sidecars --yes                              # actually write

A Photo's sidecars only catch up to its currently resolved rating/fav/tags when that Photo is
next edited (ticket 065) -- some Photos may never be edited again, so their sidecars can stay out
of sync indefinitely. This covers both cases already tracked as photos.conflict: "sidecars behind"
(the database is newer than a sidecar) and "sidecars disagree with each other" (one DNG/JPG
sidecar newer than the other). Rewrites every such Photo's sidecars to match its currently
resolved state -- exactly what a normal edit's sidecar catch-up already does, just run explicitly
for every conflicting Photo at once.

Without --yes this only prints what it would change (every conflicting Photo, its resolved
rating/fav, and each sidecar's current rating/fav, so you can see exactly what would be rewritten
before anything is). --xmp_dry_run works too and takes precedence even with --yes, matching
curation._apply everywhere else in the app.
"""

from absl import app
from absl import flags
from absl import logging

from photoapp import config  # noqa: F401  (defines the shared flags)
from photoapp import curation
from photoapp import db

FLAGS = flags.FLAGS
flags.DEFINE_boolean(
    "yes", False, "Actually write the sidecars. Without this, only a preview is printed.")
flags.DEFINE_integer(
    "limit", None, "Stop after this many Photos (for a quick check; default: every conflicting "
    "Photo).")


def find_conflicting(conn, limit=None):
  """[(photo_id, original path)] for every Photo with conflict=1, in path order."""
  q = ("SELECT p.id, f.path FROM photos p JOIN files f ON f.id = p.original_file_id "
       "WHERE p.conflict = 1 ORDER BY f.path")
  args = []
  if limit is not None:
    q += " LIMIT ?"
    args.append(limit)
  return conn.execute(q, args).fetchall()


def preview(conn, limit=None):
  """[{photo_id, path, resolved: {rating, fav}, sidecars: [{path, rating, fav}]}] for every
  conflicting Photo -- what sync_sidecars would touch and what each sidecar currently says."""
  out = []
  for row in find_conflicting(conn, limit):
    photo = conn.execute("SELECT rating, fav FROM photos WHERE id = ?", (row["id"],)).fetchone()
    sidecars = conn.execute(
        "SELECT s.path, s.rating, s.has_fav FROM xmp_sidecars s JOIN files f ON f.id = s.file_id "
        "WHERE f.photo_id = ? ORDER BY s.path", (row["id"],)).fetchall()
    out.append({
        "photo_id": row["id"], "path": row["path"],
        "resolved": {"rating": photo["rating"], "fav": bool(photo["fav"])},
        "sidecars": [{"path": s["path"], "rating": s["rating"], "fav": bool(s["has_fav"])}
                     for s in sidecars],
    })
  return out


def format_preview(rows):
  lines = []
  for r in rows:
    res = r["resolved"]
    lines.append(f"{r['path']}: -> rating={res['rating']} fav={res['fav']}")
    for s in r["sidecars"]:
      differs = s["rating"] != res["rating"] or s["fav"] != res["fav"]
      lines.append(f"    {s['path']}: rating={s['rating']} fav={s['fav']}"
                   + ("  <- differs" if differs else ""))
  lines.append(f"{len(rows)} Photo(s) with conflicting sidecars")
  return "\n".join(lines)


def run(conn, settings, limit=None):
  """Writes every conflicting Photo's sidecars (preview() already showed what). Returns how
  many were written and how many failed (with the failures)."""
  written, failed = 0, []
  for row in find_conflicting(conn, limit):
    try:
      curation.sync_sidecars(conn, settings, row["id"])
      written += 1
      logging.vlog(5, "%s: sidecars synced", row["path"])
    except curation.CurationError as e:
      failed.append({"photo_id": row["id"], "path": row["path"], "error": str(e)})
      logging.error("sync_sidecars %s: %s", row["path"], e)
  conn.commit()
  logging.vlog(1, "sync_sidecars: wrote %d, failed %d", written, len(failed))
  return written, failed


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  settings = config.Settings.from_flags()
  conn = db.open_state(settings.state_dir, busy_timeout=120.0)
  rows = preview(conn, FLAGS.limit)
  print(format_preview(rows))
  if not FLAGS.yes:
    print("(preview only; pass --yes to write)")
    return
  written, failed = run(conn, settings, FLAGS.limit)
  print(f"wrote {written} Photo(s)" + (f", {len(failed)} failed" if failed else ""))
  if failed:
    raise SystemExit(1)


if __name__ == "__main__":
  app.run(main)
