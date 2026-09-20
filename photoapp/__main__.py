"""Entry point: python -m photoapp --pictures_dir=... --state_dir=..."""

import uvicorn
from absl import app
from absl import flags
from absl import logging

from photoapp import api
from photoapp import config
from photoapp import db
from photoapp import manual_links

FLAGS = flags.FLAGS


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  conn = db.open_state(FLAGS.state_dir)
  restored = manual_links.restore_if_empty(conn, FLAGS.state_dir)
  if restored:
    logging.info("restored %d manual link decisions", restored)
  logging.info("database in %s, schema version %d", FLAGS.state_dir,
               db.schema_version(conn))
  settings = config.Settings.from_flags()
  uvicorn.run(api.create_app(conn, settings), host=FLAGS.host, port=FLAGS.port)


if __name__ == "__main__":
  app.run(main)
