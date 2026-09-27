#!/usr/bin/env python3
"""Imports `sha224sum`-format checksum listings into a catalog.py-format database.

For a backup that already has a plain checksum listing (e.g. a NAS whose backup
tool already ran `sha224sum`, or a machine with no Python environment for
catalog.py) this bootstraps a catalog without re-hashing anything. Relocated,
not rewritten, by ticket 133 (was `import_sha224sum.py` at the repo root); see
tools/archive/README.md.

Each input line has the fixed-width format produced by GNU coreutils'
sha224sum:

    <56-hex-char-digest><space><flag><filename>

`flag` is a single space for text mode or '*' for binary mode. The filename
is taken verbatim from the fixed byte offset after the digest and flag, so
it is never split on whitespace -- filenames containing spaces (including
leading, trailing, or repeated spaces) are handled correctly. A leading
'./' on the filename is stripped, matching the root-relative paths
tools/archive/catalog.py stores. Lines escaped by coreutils (prefixed with
'\\', used when a filename itself contains a backslash or newline) are also
un-escaped.

Usage:
    sha224sum -b $(find . -type f) > checksums.sha224
    tools/archive/import_sha224sum.py --db ~/backup-catalog.sqlite --input checksums.sha224

    # or stream directly:
    sha224sum -b $(find . -type f) | tools/archive/import_sha224sum.py --db ~/backup-catalog.sqlite
"""

import os
import sqlite3
import sys

from absl import app
from absl import flags
from absl import logging

FLAGS = flags.FLAGS

flags.DEFINE_string("db", None, "Path to the sqlite database file.")
flags.DEFINE_string(
    "input", "-",
    "Path to a file containing sha224sum output. '-' (the default) reads "
    "from stdin.")
flags.mark_flag_as_required("db")

_HASH_LEN = 56  # sha224 hex digest length: 224 bits / 4 bits per hex char.
_HEX_DIGITS = frozenset("0123456789abcdef")


def create_tables(conn):
  conn.execute(
      "CREATE TABLE IF NOT EXISTS hashes ("
      "filename TEXT PRIMARY KEY, "
      "hash TEXT NOT NULL)")
  conn.execute(
      "CREATE TABLE IF NOT EXISTS dir_mtimes ("
      "dirpath TEXT PRIMARY KEY, "
      "mtime REAL NOT NULL)")


def unescape_filename(filename):
  out = []
  i = 0
  n = len(filename)
  while i < n:
    c = filename[i]
    if c == "\\" and i + 1 < n and filename[i + 1] in "n\\":
      out.append("\n" if filename[i + 1] == "n" else "\\")
      i += 2
    else:
      out.append(c)
      i += 1
  return "".join(out)


def parse_line(line):
  """Parses one sha224sum output line into (filename, hash), or None."""
  line = line.rstrip("\n").rstrip("\r")
  if not line:
    return None

  escaped = line.startswith("\\")
  if escaped:
    line = line[1:]

  if len(line) < _HASH_LEN + 2:
    logging.warning("Skipping malformed line: %r", line)
    return None

  digest = line[:_HASH_LEN]
  separator = line[_HASH_LEN]
  flag = line[_HASH_LEN + 1]
  filename = line[_HASH_LEN + 2:]

  if separator != " " or flag not in (" ", "*"):
    logging.warning("Skipping malformed line: %r", line)
    return None
  if not (len(digest) == _HASH_LEN and
          all(c in _HEX_DIGITS for c in digest.lower())):
    logging.warning("Skipping malformed line: %r", line)
    return None

  if escaped:
    filename = unescape_filename(filename)

  if filename.startswith("./"):
    filename = filename[2:]
  filename = os.path.normpath(filename)

  return filename, digest.lower()


def import_lines(conn, lines):
  count = 0
  for line in lines:
    parsed = parse_line(line)
    if parsed is None:
      continue
    filename, digest = parsed
    logging.vlog(3, "Importing %s", filename)
    conn.execute(
        "INSERT INTO hashes (filename, hash) VALUES (?, ?) "
        "ON CONFLICT(filename) DO UPDATE SET hash = excluded.hash",
        (filename, digest))
    count += 1
  conn.commit()
  logging.vlog(1, "Imported %d hash entries", count)


def main(argv):
  del argv  # Unused.

  conn = sqlite3.connect(FLAGS.db)
  try:
    create_tables(conn)
    if FLAGS.input == "-":
      import_lines(conn, sys.stdin)
    else:
      with open(FLAGS.input, "r") as f:
        import_lines(conn, f)
  finally:
    conn.close()


if __name__ == "__main__":
  app.run(main)
