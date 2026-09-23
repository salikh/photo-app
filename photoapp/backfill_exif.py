"""Backfill camera metadata (aperture, shutter speed, ISO) for files scanned before ticket 083
added those columns.

  python -m photoapp.backfill_exif [--state_dir=...] [--limit=N]

Read-only on the library: opens each file just far enough to read its EXIF header (the same
fileinfo.read_camera_metadata a normal scan now already runs for every newly-scanned file) -- no
hashing, no grouping, no thumbnail work, so this is much cheaper than forcing a full rescan for
what is really only three new column values (see ticket 083 for why that mattered: scanning is
I/O-latency bound and slow across the real library). Only touches files where none of the three
are already set, so re-running is safe and cheap -- a file whose EXIF genuinely has none of them
(common for scans, screenshots, PNGs) is attempted again on every run, which is harmless (still a
no-op) but does mean this script's own cost does not shrink to zero once the library is "done";
that trade-off was chosen over adding a new column just to remember "already tried."
"""

import os
import time

from absl import app
from absl import flags
from absl import logging
from PIL import Image

from photoapp import config  # noqa: F401  (defines the shared flags)
from photoapp import db
from photoapp import fileinfo

FLAGS = flags.FLAGS
# Named backfill_limit, not limit: photoapp.sync_sidecars already defines --limit, and absl flags
# are process-global -- a test file that imports both directly (unlike populate_thumbs.py's own
# --limit, only ever exercised via a subprocess, this module's tests call its functions directly)
# would hit a DuplicateFlagError otherwise.
flags.DEFINE_integer(
    "backfill_limit", None, "Stop after this many files (for a quick check; default: every file "
    "needing it).")
flags.DEFINE_integer("report_seconds", 30, "How often to log progress.")


def find_unbackfilled(conn, limit=None):
  """[(id, path)] of live files with no camera metadata yet, in path order."""
  q = ("SELECT id, path FROM files WHERE missing = 0 AND aperture IS NULL AND "
       "shutter_speed IS NULL AND iso IS NULL ORDER BY path")
  args = []
  if limit is not None:
    q += " LIMIT ?"
    args.append(limit)
  return conn.execute(q, args).fetchall()


def backfill(conn, pictures_dir, limit=None, report_seconds=30):
  """Reads EXIF for every file find_unbackfilled lists and writes it. Returns
  (updated, unchanged, errors) counts."""
  rows = find_unbackfilled(conn, limit)
  updated = unchanged = errors = 0
  start = last_report = time.time()
  for i, row in enumerate(rows):
    path = os.path.join(pictures_dir, row["path"])
    try:
      with Image.open(path) as img:
        aperture, shutter_speed, iso = fileinfo.read_camera_metadata(img)
    except Exception as e:
      logging.warning("%s: could not read EXIF: %s", row["path"], e)
      errors += 1
      continue
    if aperture is None and shutter_speed is None and iso is None:
      unchanged += 1
      logging.vlog(5, "%s: no camera metadata in EXIF", row["path"])
    else:
      conn.execute(
          "UPDATE files SET aperture = ?, shutter_speed = ?, iso = ? WHERE id = ?",
          (aperture, shutter_speed, iso, row["id"]))
      updated += 1
      logging.vlog(5, "%s: aperture=%s shutter_speed=%s iso=%s",
                   row["path"], aperture, shutter_speed, iso)
    if time.time() - last_report > report_seconds:
      logging.info("backfill_exif: %d/%d files (%.1f/s)", i + 1, len(rows),
                   (i + 1) / max(1, time.time() - start))
      last_report = time.time()
    if (i + 1) % 200 == 0:
      conn.commit()
  conn.commit()
  logging.info("backfill_exif: done -- %d updated, %d had no camera metadata, %d errors",
               updated, unchanged, errors)
  return updated, unchanged, errors


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  settings = config.Settings.from_flags()
  conn = db.open_state(settings.state_dir, busy_timeout=120.0)
  backfill(conn, settings.pictures_dir, FLAGS.backfill_limit, FLAGS.report_seconds)


if __name__ == "__main__":
  app.run(main)
