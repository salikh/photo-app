#!/usr/bin/env python3
"""Act on a compare.py-style classification of a target copy (ticket 137, per 136's answer).

Every action starts as a printed, reviewable shell script -- nothing on either copy is touched
just by running this tool. Per bucket (see compare.py's docstring for what each one means):

  rejected  ALWAYS script-only: `rm -v --` lines for the target copy's already-handled files.
            This tool never deletes anything on a target copy itself, --apply or not -- it
            typically has no reliable write access to "another machine" anyway, and a delete is
            not the kind of write this app extends automatic trust to (see docs/tickets/136.md).
  lost      `cp -v -n`-equivalent lines that copy the file from the target copy into
            pictures_dir, preserving its relative directory structure. With --apply, this tool
            performs the copy itself (Python's own copy, not a shelled-out cp) instead of only
            printing it -- copying in is additive, not destructive, the same trust level this app
            already gives its own additive writes (a sidecar, a thumbnail). A destination that
            already exists is skipped, never overwritten.
  new       same mechanics as `lost` (script by default, --apply copies in); semantically
            "content this repo has never had an opinion on," not "a photo it used to have."

A copied-in file lands as a completely ordinary file at a plain path -- no "pending import" state
in the database. It becomes a normal, unrated Photo the next time this library is scanned
(the Rescan button, or python -m photoapp.fullscan); this tool does not scan or touch the database.

Inputs: the same --live/--target_db as compare.py (ticket 135), plus --target_root (the directory
--target_db's filenames are relative to -- the same directory tools/archive/catalog.py's own
--root_dir pointed at when that catalog was built). pictures_dir comes from this app's own
configuration (photos.toml / --pictures_dir, see photoapp/config.py), not a separate flag, so a
copy-in can never land somewhere other than where the running app actually expects it.

Usage:
    tools/archive/apply.py --live=live.jsonl --target_db=backup.sqlite \
        --target_root=/mnt/backup/Pictures > apply.sh          # review only, copies nothing
    tools/archive/apply.py --live=live.jsonl --target_db=backup.sqlite \
        --target_root=/mnt/backup/Pictures --apply              # also performs lost/new copies
"""

import os
import sys

from absl import app
from absl import flags
from absl import logging

_here = os.path.dirname(os.path.abspath(__file__))         # tools/archive/, for catalog_lib
_repo_root = os.path.dirname(os.path.dirname(_here))        # for photoapp
sys.path.insert(0, _here)
sys.path.insert(0, _repo_root)
import catalog_lib  # noqa: E402

from photoapp import config  # noqa: E402  (this tool writes into pictures_dir, so it uses the
                             # app's own configuration -- see the module docstring)

FLAGS = flags.FLAGS

flags.DEFINE_string("live", None, "This machine's export from python -m photoapp.export_catalog.")
flags.DEFINE_string("target_db", None,
                    "The target copy's hash catalog (tools/archive/catalog.py or "
                    "import_sha224sum.py format).")
flags.DEFINE_string(
    "target_root", None,
    "Directory --target_db's filenames are relative to (the --root_dir a catalog.py run against "
    "the target copy used).")
flags.DEFINE_boolean(
    "apply", False,
    "Actually copy 'lost' and 'new' files into pictures_dir, instead of only printing what would "
    "be copied. Never affects the 'rejected' bucket, which is always script-only.")
flags.mark_flag_as_required("live")
flags.mark_flag_as_required("target_db")
flags.mark_flag_as_required("target_root")


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(f"unexpected arguments: {argv[1:]}")
  settings = config.Settings.load()
  file_hashes, decisions = catalog_lib.load_live(FLAGS.live)
  target_files = catalog_lib.load_target(FLAGS.target_db)
  buckets = catalog_lib.compare(target_files, file_hashes, decisions)
  for b in catalog_lib.BUCKETS:
    logging.info("%s: %d", b, len(buckets[b]))

  actions = catalog_lib.plan(buckets, settings.pictures_dir)
  sys.stdout.write(catalog_lib.format_script(actions, FLAGS.target_root, settings.pictures_dir))

  if FLAGS.apply:
    copied, skipped = catalog_lib.apply_copies(actions, FLAGS.target_root)
    logging.info("applied: copied %d file(s), skipped %d (destination already exists)",
                len(copied), len(skipped))
    for dest, reason in skipped:
      logging.warning("skipped %s: %s", dest, reason)


if __name__ == "__main__":
  app.run(main)
