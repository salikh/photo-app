#!/usr/bin/env python3
"""Collects sha224 hashes of files in a directory tree into a sqlite database.

For each directory, the mtime is cached after all files directly in it have
been hashed; on subsequent runs a directory whose mtime has not changed is
skipped entirely, since its files could not have changed either.

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


def process_directory(conn, root_dir, dirpath, filenames):
  rel_dir = os.path.relpath(dirpath, root_dir)

  for name in filenames:
    filepath = os.path.join(dirpath, name)
    if not os.path.isfile(filepath):
      continue
    logging.vlog(3, "Hashing %s", filepath)
    try:
      file_hash = hash_file(filepath)
    except OSError as e:
      logging.error("Skipping %s: %s", filepath, e)
      continue
    rel_path = os.path.normpath(os.path.join(rel_dir, name))
    conn.execute(
        "INSERT INTO hashes (filename, hash) VALUES (?, ?) "
        "ON CONFLICT(filename) DO UPDATE SET hash = excluded.hash",
        (rel_path, file_hash))

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


def collect_hashes(conn, root_dir, scan_dir):
  for dirpath, _, filenames in os.walk(scan_dir):
    current_mtime = os.stat(dirpath).st_mtime
    cached_mtime = get_cached_mtime(conn, dirpath)
    if cached_mtime is not None and cached_mtime == current_mtime:
      logging.vlog(1, "Skipping unchanged directory %s", dirpath)
      continue

    logging.vlog(1, "Processing directory %s", dirpath)
    process_directory(conn, root_dir, dirpath, filenames)

    conn.execute(
        "INSERT INTO dir_mtimes (dirpath, mtime) VALUES (?, ?) "
        "ON CONFLICT(dirpath) DO UPDATE SET mtime = excluded.mtime",
        (dirpath, current_mtime))
    conn.commit()


def main(argv):
  del argv  # Unused.

  scan_dir = FLAGS.dir if FLAGS.dir is not None else FLAGS.root_dir

  conn = sqlite3.connect(FLAGS.db)
  try:
    create_tables(conn)
    collect_hashes(conn, FLAGS.root_dir, scan_dir)
  finally:
    conn.close()


if __name__ == "__main__":
  app.run(main)
