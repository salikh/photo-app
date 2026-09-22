"""Populate /zoo/Thumbs in the background, at low priority, one file at a time.

  python -m photoapp.populate_thumbs [--limit=N]

Finds every file that still lacks one of the four sizes and renders it (dcraw for RAW files,
Pillow for everything else -- see photoapp/thumb_populate.py for the exact commands). Only ever
one file is being rendered at a time, and the worker thread lowers its own CPU/I/O priority
(os.nice(19) plus best-effort `ionice -c3`), so this never competes with normal use of the app,
with a running scan, or with the on-demand render queue. Read-only on the library; only writes
new thumbnails (never overwrites an existing one) and the shared state database.

Progress and any per-file errors go into the 'jobs' table -- the same sqlite database the running
app uses -- so they show up on the app's Jobs page immediately, with no need to restart or
reconfigure the app. Safe to interrupt (Ctrl-C, a reboot) and rerun: already-made thumbnails are
never touched, and 'running' jobs left over from an interruption are requeued on the next start.

Run it detached, e.g.:
    nice -n19 ionice -c3 python -m photoapp.populate_thumbs &
(the process also reduces its own priority itself, so the wrapper is belt-and-braces, not
required).
"""

import time

from absl import app
from absl import flags
from absl import logging

from photoapp import config  # noqa: F401  (defines the shared flags)
from photoapp import db
from photoapp import thumb_populate
from photoapp import thumbs

FLAGS = flags.FLAGS
flags.DEFINE_integer(
    "limit", None, "Stop after queuing this many files (for a quick check; "
    "default: every file that needs one).")
flags.DEFINE_list(
    "sizes", list(thumbs.SIZES),
    f"Which sizes to fill in, comma separated (subset of {list(thumbs.SIZES)}). "
    "Default: all four.")
flags.DEFINE_integer(
    "report_seconds", 30, "How often to log the queue's progress.")


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  sizes = [s for s in thumbs.SIZES if s in FLAGS.sizes]   # keep thumbs.SIZES' order
  unknown = set(FLAGS.sizes) - set(thumbs.SIZES)
  if unknown or not sizes:
    raise app.UsageError(
        f"--sizes must be a non-empty subset of {list(thumbs.SIZES)}, got {FLAGS.sizes}")
  settings = config.Settings.from_flags()
  if not thumb_populate.dcraw_available():
    logging.warning(
        "dcraw is not on PATH: RAW files will fail (install it, e.g. "
        "'apt install dcraw'); non-RAW files are unaffected.")
  conn = db.open_state(settings.state_dir, busy_timeout=60.0)
  populator = thumb_populate.Populator(
      settings.db_path, settings.pictures_dir, settings.thumbs_dir, sizes)
  queued = populator.enqueue_missing(conn, FLAGS.limit)
  logging.info("queued %d files needing a thumbnail (pictures_dir=%s, thumbs_dir=%s)",
               queued, settings.pictures_dir, settings.thumbs_dir)
  populator.start()
  try:
    while True:
      time.sleep(FLAGS.report_seconds)
      counts = populator.queue.counts()
      logging.info("populate_thumbs: %s", counts)
      if not counts.get("queued") and not counts.get("running"):
        break
  finally:
    populator.stop()
  logging.info("done")


if __name__ == "__main__":
  app.run(main)
