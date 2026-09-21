"""sqlite schema, migrations and connection helpers.

The database is a rebuildable cache of what is on disk, except for the
tables listed in NOT_REBUILDABLE, which must be backed up.
"""

import os
import sqlite3
import time

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
    """
    ALTER TABLE photos ADD COLUMN representative_source TEXT NOT NULL
      DEFAULT 'auto';  -- 'auto' | 'manual' (user override, survives rescans)
    """,
    """
    ALTER TABLE manual_links ADD COLUMN role TEXT;  -- 'tuning' | 'export' (link)
    """,
    """
    ALTER TABLE xmp_sidecars ADD COLUMN tags TEXT;  -- JSON list, without 'fav'
    """,
    """
    -- RAW sizes used to come from Pillow's IFD0 thumbnail (160x120). Force
    -- the next scan to re-read every RAW row.
    UPDATE files SET bytesize = -1 WHERE lower(path) GLOB '*.dng'
      OR lower(path) GLOB '*.cr2' OR lower(path) GLOB '*.cr3'
      OR lower(path) GLOB '*.nef' OR lower(path) GLOB '*.arw'
      OR lower(path) GLOB '*.raf' OR lower(path) GLOB '*.rw2'
      OR lower(path) GLOB '*.orf' OR lower(path) GLOB '*.pef';
    DELETE FROM dir_mtimes;
    """,
    """
    ALTER TABLE activity_log ADD COLUMN batch_id TEXT;  -- one id per batch action
    """,
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- e.g. grouping_version
    """,
    """
    CREATE INDEX xmp_sidecars_file ON xmp_sidecars(file_id);
    """,
    """
    -- The database is an eligible source of the authoritative rating (ticket 021): the newest
    -- of the database and the sidecars wins. rating_updated_at is when the rating was set here.
    ALTER TABLE photos ADD COLUMN rating_updated_at REAL;     -- epoch seconds, NULL = unknown/oldest
    ALTER TABLE rating_by_hash ADD COLUMN rated_at REAL;
    UPDATE photos SET rating_updated_at = (
        SELECT MAX(s.mtime) FROM xmp_sidecars s JOIN files f ON f.id = s.file_id
        WHERE f.photo_id = photos.id AND s.rating = photos.rating)
      WHERE rating_source = 'xmp';
    UPDATE photos SET rating_updated_at = (
        SELECT CAST(strftime('%s', MAX(a.ts), 'utc') AS REAL) FROM activity_log a
        WHERE a.photo_id = photos.id AND a.field IN ('rating', 'fav', 'tags'))
      WHERE rating_source = 'app';
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


class DatabaseBusy(Exception):
  """The database stayed locked by another writer for the whole retry budget."""


def is_busy(error):
  """True for sqlite's 'database is locked' / 'database table is locked' / busy errors."""
  if not isinstance(error, sqlite3.OperationalError):
    return False
  text = str(error).lower()
  return "locked" in text or "busy" in text


def retry_busy(fn, total_seconds=60.0, first_delay=0.1, max_delay=4.0,
               sleep=time.sleep, clock=time.monotonic, on_retry=None):
  """Call fn(); if the database is busy, wait (doubling, capped) and call it again.

  Gives up with DatabaseBusy once about total_seconds have passed (measured on
  clock, so waiting inside sqlite counts too). fn must be safe to run again;
  on_retry() runs before each wait (for example to roll the connection back).
  Errors that are not "busy" propagate at once.
  """
  start, delay = clock(), first_delay
  while True:
    try:
      return fn()
    except sqlite3.OperationalError as e:
      if not is_busy(e):
        raise
      if on_retry:
        on_retry()
      if clock() - start + delay > total_seconds:
        raise DatabaseBusy(f"database busy for {clock() - start:.0f} s: {e}") from e
      sleep(delay)
      delay = min(delay * 2, max_delay)


def connect(db_path, busy_timeout=5.0):
  """Open (creating if needed) and migrate the database at db_path.

  busy_timeout: how long sqlite itself waits for a lock before failing with
  'database is locked' (background workers use a long one; the web app uses a
  short one and retries, see retry_busy).
  """
  if db_path != ":memory:":
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
  conn = sqlite3.connect(db_path, timeout=busy_timeout, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  if db_path != ":memory:":
    conn.execute("PRAGMA journal_mode = WAL")
  migrate(conn)
  return conn


def open_state(state_dir, busy_timeout=5.0):
  """Open the app database in state_dir."""
  return connect(os.path.join(state_dir, DB_NAME), busy_timeout)
