"""sqlite schema, migrations and connection helpers.

The database is a rebuildable cache of what is on disk, except for the
tables listed in NOT_REBUILDABLE, which must be backed up.
"""

import os
import sqlite3

DB_NAME = "app.sqlite"

NOT_REBUILDABLE = ("manual_links", "activity_log")

# MIGRATIONS[i] takes the schema from user_version i to i + 1.
MIGRATIONS = [
    """
    CREATE TABLE files (
      id INTEGER PRIMARY KEY,
      path TEXT NOT NULL UNIQUE,
      hash TEXT,
      mime_type TEXT, width INTEGER, height INTEGER,
      bytesize INTEGER, mtime REAL NOT NULL,
      exif_date TEXT,
      photo_id INTEGER REFERENCES photos(id),
      role TEXT NOT NULL DEFAULT 'original',
      derived_from INTEGER REFERENCES files(id),
      link_source TEXT NOT NULL DEFAULT 'auto',
      missing INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX files_hash ON files(hash);
    CREATE INDEX files_photo ON files(photo_id);

    CREATE TABLE photos (
      id INTEGER PRIMARY KEY,
      original_file_id INTEGER NOT NULL REFERENCES files(id),
      representative_file_id INTEGER NOT NULL REFERENCES files(id),
      rating INTEGER NOT NULL DEFAULT 0,
      fav INTEGER NOT NULL DEFAULT 0,
      previous_stars INTEGER,
      rating_source TEXT,
      conflict INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE tags (
      photo_id INTEGER NOT NULL REFERENCES photos(id),
      tag TEXT NOT NULL,
      PRIMARY KEY (photo_id, tag)
    );

    CREATE TABLE xmp_sidecars (
      path TEXT PRIMARY KEY,
      file_id INTEGER REFERENCES files(id),
      mtime REAL NOT NULL, hash TEXT,
      rating INTEGER, has_fav INTEGER,
      backup_path TEXT
    );

    CREATE TABLE manual_links (
      id INTEGER PRIMARY KEY,
      path TEXT NOT NULL, hash TEXT,
      target_path TEXT, target_hash TEXT,
      action TEXT NOT NULL,
      created_at TEXT NOT NULL
    );

    CREATE TABLE rating_by_hash (
      hash TEXT PRIMARY KEY, rating INTEGER, fav INTEGER, last_path TEXT
    );

    CREATE TABLE activity_log (
      id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
      photo_id INTEGER, xmp_path TEXT,
      field TEXT NOT NULL,
      old TEXT, new TEXT, cause TEXT,
      undone INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE thumbs (
      file_id INTEGER NOT NULL REFERENCES files(id),
      size TEXT NOT NULL,
      path TEXT NOT NULL, bytesize INTEGER,
      source TEXT NOT NULL,
      PRIMARY KEY (file_id, size)
    );

    CREATE TABLE jobs (
      id INTEGER PRIMARY KEY, kind TEXT NOT NULL, file_id INTEGER,
      state TEXT NOT NULL,
      error TEXT, created_at TEXT, finished_at TEXT
    );

    CREATE TABLE dir_mtimes (dirpath TEXT PRIMARY KEY, mtime REAL NOT NULL);
    """,
]


def schema_version(conn):
  return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn):
  """Bring conn's schema up to date. Safe to call on every start."""
  version = schema_version(conn)
  if version > len(MIGRATIONS):
    raise RuntimeError(
        f"database schema version {version} is newer than this app "
        f"({len(MIGRATIONS)})")
  for i in range(version, len(MIGRATIONS)):
    # executescript commits first, so run each migration then set the version.
    conn.executescript(MIGRATIONS[i])
    conn.execute(f"PRAGMA user_version = {i + 1}")
    conn.commit()


def connect(db_path):
  """Open (creating if needed) and migrate the database at db_path."""
  if db_path != ":memory:":
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
  conn = sqlite3.connect(db_path, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  if db_path != ":memory:":
    conn.execute("PRAGMA journal_mode = WAL")
  migrate(conn)
  return conn


def open_state(state_dir):
  """Open the app database in state_dir."""
  return connect(os.path.join(state_dir, DB_NAME))
