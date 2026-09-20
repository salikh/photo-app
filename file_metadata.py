#!/usr/bin/env python3
"""Collects per-image metadata (type, width, height, hash) about a tree.

Similar in structure to hash_dir.py: for each directory, the mtime is
cached (in the 'dir_mtimes' table of --db) after all image files
directly in it have been processed; on subsequent runs a directory
whose mtime has not changed is skipped entirely. Each file's own mtime
is cached alongside its metadata (in --db's 'image_metadata' table), so
a file whose mtime still matches the cached value is not re-read either
-- only new or changed files are actually decoded and re-hashed.

If --hashes_db is given (a 'hashes'-format database produced by
hash_dir.py against the same --root_dir, e.g. zoo.db or a hash_dir.py
run over a relocated thumbs tree), a file's hash is reused from there
instead of being recomputed, as long as its recorded mtime still
matches the file's current mtime. Otherwise (or if --hashes_db isn't
given) the hash is computed directly, the same sha224-over-chunks way
hash_dir.py does it.

In addition to --db, every (re)processed directory gets an index.json
written directly into it, containing the directory's own mtime and, for
each image file directly in it, its mime_type/width/height/hash/mtime.
Since writing index.json itself can change the directory's mtime, that
mtime is re-read right after the write and, if it changed, index.json
is rewritten with the corrected value -- so both index.json and --db's
dir_mtimes cache agree on the directory's true final mtime, and an
unmodified directory is still correctly skipped on the next run.

Each file's record also carries its plain byte size ('bytesize'). A
record reused from an existing index.json (mtime unchanged) that
predates this field simply has it backfilled in place, without
otherwise being re-read or re-hashed.

Each file's record also carries 'exif_date': the EXIF capture date as an
ISO-style string like "2026-01-01 01:02:03+0900" (DateTimeOriginal, falling
back to DateTimeDigitized, then DateTime; the UTC offset comes from the
matching OffsetTime* tag and is omitted if the file has none), or null if
the image has no (valid) EXIF date. A record from an existing index.json
(mtime unchanged) that lacks the 'exif_date' key entirely is backfilled by
re-reading just the EXIF data; such directories are reprocessed even if
their own mtime has not changed.

Requires Pillow (`import PIL`) to read image dimensions, format and EXIF.

Usage:
    file_metadata.py
        --logtostderr --v=3
        --root_dir /zoo/Pictures
        --dir /zoo/Pictures/2024
        --db ~/metadata.sqlite
        --hashes_db ~/zoo.db
"""

import json
import os
import sqlite3

from absl import app
from absl import flags
from absl import logging

from photoapp.fileinfo import (
    IMAGE_EXTENSIONS, get_or_compute_hash, is_image, load_precomputed_hashes,
    read_exif_date_from_path, read_image_metadata)

FLAGS = flags.FLAGS

flags.DEFINE_string("db", None, "Path to the sqlite database file.")
flags.DEFINE_string(
    "root_dir", None,
    "Base directory that stored file paths are computed relative to.")
flags.DEFINE_string(
    "dir", None,
    "Directory to scan for files. May be a subdirectory of --root_dir to "
    "only (re)scan part of the tree. Defaults to --root_dir if not given.")
flags.DEFINE_string(
    "hashes_db", None,
    "Optional path to a 'hashes'-format sqlite3 database (as produced by "
    "hash_dir.py) for the same --root_dir. A file's hash is reused from "
    "here, instead of being recomputed, when its recorded mtime still "
    "matches the file's current mtime.")
flags.mark_flag_as_required("db")
flags.mark_flag_as_required("root_dir")

_INDEX_JSON_NAME = "index.json"


def create_tables(conn):
  conn.execute(
      "CREATE TABLE IF NOT EXISTS image_metadata ("
      "filename TEXT PRIMARY KEY, "
      "mime_type TEXT, "
      "width INTEGER, "
      "height INTEGER, "
      "hash TEXT, "
      "bytesize INTEGER, "
      "exif_date TEXT, "
      "mtime REAL NOT NULL)")
  conn.execute(
      "CREATE TABLE IF NOT EXISTS dir_mtimes ("
      "dirpath TEXT PRIMARY KEY, "
      "mtime REAL NOT NULL)")
  columns = {row[1] for row in conn.execute("PRAGMA table_info(image_metadata)")}
  if "hash" not in columns:
    conn.execute("ALTER TABLE image_metadata ADD COLUMN hash TEXT")
  if "bytesize" not in columns:
    conn.execute("ALTER TABLE image_metadata ADD COLUMN bytesize INTEGER")
  if "exif_date" not in columns:
    conn.execute("ALTER TABLE image_metadata ADD COLUMN exif_date TEXT")


def get_cached_dir_mtime(conn, dirpath):
  row = conn.execute(
      "SELECT mtime FROM dir_mtimes WHERE dirpath = ?", (dirpath,)).fetchone()
  return row[0] if row else None


def load_existing_index(dirpath):
  """Return {name: record} from dirpath's existing index.json, if any."""
  index_path = os.path.join(dirpath, _INDEX_JSON_NAME)
  try:
    with open(index_path) as f:
      data = json.load(f)
  except (OSError, ValueError):
    return {}
  return data.get("files", {})


def upsert_image_metadata(conn, rel_path, record):
  conn.execute(
      "INSERT INTO image_metadata "
      "(filename, mime_type, width, height, hash, bytesize, exif_date, mtime) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
      "ON CONFLICT(filename) DO UPDATE SET "
      "mime_type = excluded.mime_type, width = excluded.width, "
      "height = excluded.height, hash = excluded.hash, "
      "bytesize = excluded.bytesize, exif_date = excluded.exif_date, "
      "mtime = excluded.mtime",
      (rel_path, record["mime_type"], record["width"], record["height"],
       record["hash"], record["bytesize"], record["exif_date"],
       record["mtime"]))


def process_directory(conn, root_dir, dirpath, filenames, precomputed_hashes):
  """Process every image file directly in dirpath.

  Returns {name: {mime_type, width, height, hash, bytesize, exif_date,
  mtime}} for
  every current image file in the directory (freshly computed or reused
  from that directory's existing index.json), for use in the new
  index.json written for it.
  """
  rel_dir = os.path.relpath(dirpath, root_dir)
  existing_index = load_existing_index(dirpath)
  file_records = {}

  for name in filenames:
    if not is_image(name):
      continue
    filepath = os.path.join(dirpath, name)
    if not os.path.isfile(filepath):
      continue
    try:
      st = os.stat(filepath)
    except OSError as e:
      logging.error("Skipping %s: %s", filepath, e)
      continue
    file_mtime = st.st_mtime

    rel_path = os.path.normpath(os.path.join(rel_dir, name))
    cached = existing_index.get(name)
    if cached is not None and cached.get("mtime") == file_mtime:
      if "bytesize" not in cached:
        logging.vlog(3, "Backfilling bytesize for unchanged file %s", filepath)
        cached = dict(cached, bytesize=st.st_size)
      if "exif_date" not in cached:
        logging.vlog(3, "Backfilling exif_date for unchanged file %s", filepath)
        cached = dict(cached, exif_date=read_exif_date_from_path(filepath))
      file_records[name] = cached
      upsert_image_metadata(conn, rel_path, cached)
      continue

    mime_type, width, height, exif_date = read_image_metadata(filepath)
    file_hash = get_or_compute_hash(
        filepath, rel_path, file_mtime, precomputed_hashes)

    logging.vlog(3, "Writing metadata for %s", filepath)
    record = {
        "mime_type": mime_type, "width": width, "height": height,
        "hash": file_hash, "bytesize": st.st_size, "exif_date": exif_date,
        "mtime": file_mtime,
    }
    file_records[name] = record
    upsert_image_metadata(conn, rel_path, record)

  # Drop stale entries for image files that used to live directly in this
  # directory but have since been removed or renamed. Subdirectory entries
  # are left alone: they are handled when os.walk visits them. Only image
  # files are tracked here, so only those need considering.
  current_image_names = {n for n in filenames if is_image(n)}
  if rel_dir == ".":
    rows = conn.execute(
        f"SELECT filename FROM image_metadata "
        f"WHERE filename NOT GLOB '*{os.sep}*'")
    for (existing_path,) in rows.fetchall():
      if existing_path not in current_image_names:
        conn.execute(
            "DELETE FROM image_metadata WHERE filename = ?", (existing_path,))
  else:
    prefix = rel_dir + os.sep
    rows = conn.execute(
        "SELECT filename FROM image_metadata WHERE filename LIKE ?",
        (prefix + "%",))
    for (existing_path,) in rows.fetchall():
      relative = existing_path[len(prefix):]
      if os.sep not in relative and relative not in current_image_names:
        conn.execute(
            "DELETE FROM image_metadata WHERE filename = ?", (existing_path,))

  return file_records


def write_index_json(dirpath, dir_mtime, file_records):
  """Write dirpath's index.json and return the directory's final mtime.

  Writing the file can itself change dirpath's mtime (a new index.json
  creates a new directory entry; overwriting an existing one usually
  doesn't). The directory is re-stat-ed after writing, and if that
  differs from dir_mtime, index.json is rewritten once more with the
  corrected value, so the file and the caller's dir_mtimes cache both
  end up agreeing with the directory's true final mtime.
  """
  index_path = os.path.join(dirpath, _INDEX_JSON_NAME)
  data = {"mtime": dir_mtime, "files": file_records}
  with open(index_path, "w") as f:
    json.dump(data, f, indent=2, sort_keys=True)

  final_mtime = os.stat(dirpath).st_mtime
  if final_mtime != dir_mtime:
    data["mtime"] = final_mtime
    with open(index_path, "w") as f:
      json.dump(data, f, indent=2, sort_keys=True)

  return final_mtime


def index_lacks_exif_date(dirpath, filenames):
  """True if dirpath's index.json has an image without an 'exif_date' key.

  Such directories need reprocessing to backfill EXIF dates even though
  their mtime is unchanged. A null exif_date counts as present (the file
  was already checked and has none).
  """
  image_names = [
      n for n in filenames
      if is_image(n) and os.path.isfile(os.path.join(dirpath, n))]
  if not image_names:
    return False
  existing_index = load_existing_index(dirpath)
  return any("exif_date" not in existing_index.get(n, {}) for n in image_names)


def collect_metadata(conn, root_dir, scan_dir, precomputed_hashes):
  for dirpath, _, filenames in os.walk(scan_dir):
    current_mtime = os.stat(dirpath).st_mtime
    cached_mtime = get_cached_dir_mtime(conn, dirpath)
    if (cached_mtime is not None and cached_mtime == current_mtime
        and not index_lacks_exif_date(dirpath, filenames)):
      logging.vlog(1, "Skipping unchanged directory %s", dirpath)
      continue

    logging.vlog(1, "Processing directory %s", dirpath)
    file_records = process_directory(
        conn, root_dir, dirpath, filenames, precomputed_hashes)

    final_mtime = write_index_json(dirpath, current_mtime, file_records)

    conn.execute(
        "INSERT INTO dir_mtimes (dirpath, mtime) VALUES (?, ?) "
        "ON CONFLICT(dirpath) DO UPDATE SET mtime = excluded.mtime",
        (dirpath, final_mtime))
    conn.commit()


def main(argv):
  if len(argv) != 1:
    raise app.UsageError(
        "This tool takes no positional arguments; use --db/--root_dir/"
        "--dir/--hashes_db instead (got: %s)" % argv[1:])

  scan_dir = FLAGS.dir if FLAGS.dir is not None else FLAGS.root_dir
  precomputed_hashes = (
      load_precomputed_hashes(FLAGS.hashes_db) if FLAGS.hashes_db else None)
  if precomputed_hashes is not None:
    logging.info("loaded %d precomputed hashes from %s",
                 len(precomputed_hashes), FLAGS.hashes_db)

  conn = sqlite3.connect(FLAGS.db)
  try:
    create_tables(conn)
    collect_metadata(conn, FLAGS.root_dir, scan_dir, precomputed_hashes)
  finally:
    conn.close()


if __name__ == "__main__":
  app.run(main)
