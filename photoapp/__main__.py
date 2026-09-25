"""Entry point: python -m photoapp --pictures_dir=... --state_dir=..."""

import uvicorn
from absl import app
from absl import flags
from absl import logging
import logging as py_logging

from photoapp import api
from photoapp import config
from photoapp import db
from photoapp import load_worker
from photoapp import manual_links
from photoapp import scan

FLAGS = flags.FLAGS


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  for module in ["TiffImagePlugin.py"]:
    # Set the noisy module to WARNING or higher to silence its DEBUG/INFO logs
    py_logging.getLogger(module.strip()).setLevel(logging.WARNING)
  conn = db.open_state(FLAGS.state_dir)
  restored = manual_links.restore_if_empty(conn, FLAGS.state_dir)
  if restored:
    logging.info("restored %d manual link decisions", restored)
  logging.info("database in %s, schema version %d", FLAGS.state_dir,
               db.schema_version(conn))
  settings = config.Settings.from_flags()
  application = api.create_app(conn, settings)
  application.state.jobs.start()
  if settings.load_worker_enabled:
    def enqueue_more():
      n = application.state.populator.enqueue_missing(application.state.populate_conn)
      if n:
        logging.info("load_worker: queued %d file(s) needing a thumbnail", n)

    load_worker.LoadAdaptiveWorker(
        application.state.background_jobs, check_seconds=settings.load_check_seconds,
        start_load=settings.load_start_threshold, stop_load=settings.load_stop_threshold,
        start_mem_percent=settings.mem_start_percent, stop_mem_percent=settings.mem_stop_percent,
        on_before_start=enqueue_more).start()
  if settings.nightly_scan_hour >= 0:
    # Queues one job per top-level directory into the same background_jobs queue above (ticket
    # 076); if --load_worker_enabled is off, nothing drains that queue automatically -- the jobs
    # just wait (use --nightly_scan_hour=-1 to not enqueue them at all, or fullscan.py/the Rescan
    # button for an immediate scan regardless of this flag).
    scan.NightlyScan(application.state.background_jobs, settings.pictures_dir,
                     settings.nightly_scan_hour).start()
  uvicorn.run(application, host=FLAGS.host, port=FLAGS.port)


if __name__ == "__main__":
  app.run(main)
