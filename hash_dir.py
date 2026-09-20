#!/usr/bin/env python3
"""Collects sha224 hashes of files in a directory tree into a sqlite database.

For each directory, the mtime is cached after all files directly in it have
been hashed; on subsequent runs a directory whose mtime has not changed is
skipped entirely, since its files could not have changed either. Each
file's own mtime is cached alongside its hash; when a directory is
skipped because it is unchanged, any of its files that are missing a
cached mtime (e.g. from a database written before this column existed)
have it filled in without recomputing their hash. Within a directory
that does get (re)scanned, a file whose mtime still matches the cached
value is likewise left alone -- only files that are new or whose mtime
has changed are actually re-hashed.

Before scanning, every entry in the database is checked for existence on
disk (regardless of --dir) and entries for files that no longer exist
are dropped.

Usage:
    hash_dir.py
        --logtostderr --v=3
        --root_dir /zoo/Pictures
        --dir /zoo/Pictures/2024
        --db ~/zoo.sqlite
"""

import hashlib
import os
import sqlite3

from absl import app
from absl import flags
from absl import logging

FLAGS = flags.FLAGS

flags.DEFINE_string("db", None, "Path to the sqlite database file.")
flags.DEFINE_string(
    "root_dir", None,
    "Base directory that stored file paths are computed relative to.")
flags.DEFINE_string(
    "dir", None,
    "Directory to scan for files. May be a subdirectory of --root_dir to "
    "only (re)scan part of the tree. Defaults to --root_dir if not given.")
flags.mark_flag_as_required("db")
flags.mark_flag_as_required("root_dir")

_HASH_CHUNK_SIZE = 1024 * 1024


def create_tables(conn):
  conn.execute(
      "CREATE TABLE IF NOT EXISTS hashes ("
      "filename TEXT PRIMARY KEY, "
      "hash TEXT NOT NULL)")
  conn.execute(
      "CREATE TABLE IF NOT EXISTS dir_mtimes ("
      "dirpath TEXT PRIMARY KEY, "
      "mtime REAL NOT NULL)")
  columns = {row[1] for row in conn.execute("PRAGMA table_info(hashes)")}
  if "mtime" not in columns:
    conn.execute("ALTER TABLE hashes ADD COLUMN mtime REAL")


def get_cached_mtime(conn, dirpath):
  row = conn.execute(
      "SELECT mtime FROM dir_mtimes WHERE dirpath = ?", (dirpath,)).fetchone()
  return row[0] if row else None


def hash_file(path):
  digest = hashlib.sha224()
  with open(path, "rb") as f:
    while True:
      chunk = f.read(_HASH_CHUNK_SIZE)
      if not chunk:
        break
      digest.update(chunk)
  return digest.hexdigest()


def get_cached_file_mtime(conn, rel_path):
  row = conn.execute(
      "SELECT mtime FROM hashes WHERE filename = ?", (rel_path,)).fetchone()
  return row[0] if row else None


def process_directory(conn, root_dir, dirpath, filenames):
  rel_dir = os.path.relpath(dirpath, root_dir)

  for name in filenames:
    filepath = os.path.join(dirpath, name)
    if not os.path.isfile(filepath):
      continue
    try:
      file_mtime = os.stat(filepath).st_mtime
    except OSError as e:
      logging.error("Skipping %s: %s", filepath, e)
      continue
    rel_path = os.path.normpath(os.path.join(rel_dir, name))

    cached_mtime = get_cached_file_mtime(conn, rel_path)
    if cached_mtime is not None and cached_mtime == file_mtime:
      logging.vlog(2, "Skipping unchanged file %s", filepath)
      continue

    logging.vlog(3, "Hashing %s", filepath)
    try:
      file_hash = hash_file(filepath)
    except OSError as e:
      logging.error("Skipping %s: %s", filepath, e)
      continue
    conn.execute(
        "INSERT INTO hashes (filename, hash, mtime) VALUES (?, ?, ?) "
        "ON CONFLICT(filename) DO UPDATE SET "
        "hash = excluded.hash, mtime = excluded.mtime",
        (rel_path, file_hash, file_mtime))

  # Drop stale entries for files that used to live directly in this
  # directory but have since been removed or renamed. Subdirectory entries
  # are left alone: they are handled when os.walk visits them.
  current_names = set(filenames)
  if rel_dir == ".":
    rows = conn.execute(
        f"SELECT filename FROM hashes WHERE filename NOT GLOB '*{os.sep}*'")
    for (existing_path,) in rows.fetchall():
      if existing_path not in current_names:
        conn.execute("DELETE FROM hashes WHERE filename = ?", (existing_path,))
  else:
    prefix = rel_dir + os.sep
    rows = conn.execute(
        "SELECT filename FROM hashes WHERE filename LIKE ?", (prefix + "%",))
    for (existing_path,) in rows.fetchall():
      relative = existing_path[len(prefix):]
      if os.sep not in relative and relative not in current_names:
        conn.execute("DELETE FROM hashes WHERE filename = ?", (existing_path,))


def fill_missing_mtimes(conn, root_dir, dirpath):
  """Back-fill mtime for files directly in dirpath whose mtime is NULL.

  Used for directories skipped as unchanged, so that databases written
  before the mtime column existed get it filled in without paying for a
  full re-hash.
  """
  rel_dir = os.path.relpath(dirpath, root_dir)
  if rel_dir == ".":
    rows = conn.execute(
        f"SELECT filename FROM hashes "
        f"WHERE filename NOT GLOB '*{os.sep}*' AND mtime IS NULL").fetchall()
  else:
    prefix = rel_dir + os.sep
    rows = conn.execute(
        "SELECT filename FROM hashes WHERE filename LIKE ? AND mtime IS NULL",
        (prefix + "%",)).fetchall()
    rows = [r for r in rows if os.sep not in r[0][len(prefix):]]

  for (rel_path,) in rows:
    filepath = os.path.join(root_dir, rel_path)
    try:
      file_mtime = os.stat(filepath).st_mtime
    except OSError as e:
      logging.error("Skipping %s: %s", filepath, e)
      continue
    logging.vlog(2, "Filling in missing mtime for %s", filepath)
    conn.execute(
        "UPDATE hashes SET mtime = ? WHERE filename = ?",
        (file_mtime, rel_path))


def collect_hashes(conn, root_dir, scan_dir):
  for dirpath, _, filenames in os.walk(scan_dir):
    current_mtime = os.stat(dirpath).st_mtime
    cached_mtime = get_cached_mtime(conn, dirpath)
    if cached_mtime is not None and cached_mtime == current_mtime:
      logging.vlog(1, "Skipping unchanged directory %s", dirpath)
      fill_missing_mtimes(conn, root_dir, dirpath)
      conn.commit()
      continue

    logging.vlog(1, "Processing directory %s", dirpath)
    process_directory(conn, root_dir, dirpath, filenames)

    conn.execute(
        "INSERT INTO dir_mtimes (dirpath, mtime) VALUES (?, ?) "
        "ON CONFLICT(dirpath) DO UPDATE SET mtime = excluded.mtime",
        (dirpath, current_mtime))
    conn.commit()


def remove_missing_files(conn, root_dir):
  """Delete entries for files that no longer exist on disk.

  Covers cases collect_hashes's per-directory cleanup can't: a whole
  directory removed (os.walk never visits it, so it's never compared
  against) or a file gone missing without its parent directory's mtime
  changing.

  Before stat-ing each individual file, its parent directory's cached
  mtime (from dir_mtimes) is checked once and memoized in `dir_state`:
  if the directory's mtime still matches, its files could not have been
  added or removed, so their existence check is skipped entirely.
  """
  rows = conn.execute("SELECT filename FROM hashes").fetchall()
  removed = 0
  dir_state = {}  # dirpath -> True if unchanged (existence checks skippable)
  for (rel_path,) in rows:
    filepath = os.path.join(root_dir, rel_path)
    dirpath = os.path.dirname(filepath)

    unchanged = dir_state.get(dirpath)
    if unchanged is None:
      cached_mtime = get_cached_mtime(conn, dirpath)
      try:
        current_mtime = os.stat(dirpath).st_mtime
      except OSError:
        current_mtime = None
      unchanged = cached_mtime is not None and cached_mtime == current_mtime
      dir_state[dirpath] = unchanged

    if unchanged:
      logging.vlog(7, "Skipping existence check for %s (unchanged directory)",
                   filepath)
      continue

    if not os.path.isfile(filepath):
      logging.vlog(1, "Removing entry for missing file %s", filepath)
      conn.execute("DELETE FROM hashes WHERE filename = ?", (rel_path,))
      removed += 1
  conn.commit()
  return removed


def main(argv):
  del argv  # Unused.

  scan_dir = FLAGS.dir if FLAGS.dir is not None else FLAGS.root_dir

  conn = sqlite3.connect(FLAGS.db)
  try:
    create_tables(conn)
    removed = remove_missing_files(conn, FLAGS.root_dir)
    logging.info("Removed %d entries for missing files", removed)
    collect_hashes(conn, FLAGS.root_dir, scan_dir)
  finally:
    conn.close()


if __name__ == "__main__":
  app.run(main)
