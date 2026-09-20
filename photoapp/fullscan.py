"""Scan the whole library from the command line, with progress output.

  python -m photoapp.fullscan --hashes_db=~/zoo.db [--pictures_dir=...] [--state_dir=...]

Read-only on the library (only the state database is written). --scan_dirs=2001,2026
restricts it to those directories. Safe to
interrupt and rerun: finished directories are skipped by their mtime.
"""

import os
import threading
import time

from absl import app
from absl import flags
from absl import logging

from photoapp import config
from photoapp import db
from photoapp import fileinfo
from photoapp import manual_links
from photoapp import recovery
from photoapp import scan

FLAGS = flags.FLAGS
flags.DEFINE_list(
    "scan_dirs", [],
    "Only scan these directories (relative to --pictures_dir, comma separated). "
    "Default: the whole library.")


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  settings = config.Settings.from_flags()
  conn = db.open_state(settings.state_dir)
  manual_links.restore_if_empty(conn, settings.state_dir)
  hashes = (fileinfo.load_precomputed_hashes(os.path.expanduser(settings.hashes_db))
            if settings.hashes_db else None)
  logging.info("scanning %s into %s (%d workers, %s hashes)",
               settings.pictures_dir, settings.state_dir, settings.scan_workers,
               len(hashes) if hashes else "no")
  progress = scan.Progress()
  done = threading.Event()

  def report():
    start = time.time()
    while not done.wait(30):
      logging.info("dirs=%d skipped=%d files_seen=%d processed=%d sidecars=%d (%.1f files/s)",
                   progress.dirs_seen, progress.dirs_skipped, progress.files_seen,
                   progress.files_processed,
                   progress.sidecars_processed,
                   progress.files_processed / max(1, time.time() - start))

  threading.Thread(target=report, daemon=True).start()
  targets = [os.path.join(settings.pictures_dir, d.strip("/")) for d in FLAGS.scan_dirs] \
      or [settings.pictures_dir]
  for target in targets:
    if not os.path.isdir(target):
      raise SystemExit(f"not a directory: {target}")
  for target in targets:
    logging.info("scanning %s", target)
    scan.scan(conn, settings.pictures_dir, target, hashes=hashes,
              progress=progress, thumbs_dir=settings.thumbs_dir,
              on_done=lambda c: recovery.recover(c, settings),
              workers=settings.scan_workers)
    if progress.error:
      break
  done.set()
  logging.info("finished: %s", progress)
  for table in ("files", "photos", "xmp_sidecars", "tags"):
    logging.info("%s: %d", table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
  if progress.error:
    raise SystemExit(f"scan failed: {progress.error}")


if __name__ == "__main__":
  app.run(main)
