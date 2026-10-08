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
    """
    -- A job's unit of work is usually a file (file_id), but ticket 076's scan_dir jobs are
    -- scoped to a directory instead -- target holds that relative directory path, file_id is
    -- NULL for those. Every existing job kind leaves target NULL.
    ALTER TABLE jobs ADD COLUMN target TEXT;
    """,
    """
    -- Camera metadata (ticket 083), from EXIF: aperture is the f-number (e.g. 2.8), shutter_speed
    -- is exposure time in seconds (e.g. 0.004 for 1/250s -- "1/250" formatting is a display
    -- concern), iso is the ISO speed. NULL when the file has no EXIF, or lacks that tag. Existing
    -- rows are backfilled by photoapp/backfill_exif.py, not by a forced rescan (see the ticket).
    ALTER TABLE files ADD COLUMN aperture REAL;
    ALTER TABLE files ADD COLUMN shutter_speed REAL;
    ALTER TABLE files ADD COLUMN iso INTEGER;
    """,
    """
    -- Per-file dcraw demosaic overrides (ticket 085, original scope). Superseded by the next
    -- migration before anything ever read or wrote these -- 090's resolution replaced the
    -- dcraw-only design with one shared rawpy/LibRaw renderer used everywhere, so these columns'
    -- own "only the dcraw path, not on-demand" framing no longer describes the app. Dropped rather
    -- than repurposed/renamed: migrations are append-only history, and starting the real columns
    -- clean in the next migration is simpler than working around these names' stale meaning.
    ALTER TABLE files ADD COLUMN dcraw_brightness REAL;
    ALTER TABLE files ADD COLUMN dcraw_highlight_mode INTEGER;
    ALTER TABLE files ADD COLUMN dcraw_wb_mode TEXT;
    """,
    """
    -- Per-file RAW conversion settings (ticket 085, per 090's resolution): apply uniformly to
    -- both the on-demand and background render paths, now merged onto rawpy/LibRaw. NULL means
    -- "current hardcoded default" for that setting -- a file is "at default" (docs/design/
    -- thumbnails.md's embedded-preview-shortcut rule) iff every one of these columns is NULL.
    -- raw_wb_mode: 'camera' (LibRaw use_camera_wb, the default), 'auto' (use_auto_wb), or 'manual'
    -- (user_wb from raw_wb_r/g/b, green channel reused for LibRaw's 4th/G2 multiplier).
    -- raw_highlight: LibRaw highlight_mode, 0-9 (0 = clip, LibRaw's own default).
    ALTER TABLE files DROP COLUMN dcraw_brightness;
    ALTER TABLE files DROP COLUMN dcraw_highlight_mode;
    ALTER TABLE files DROP COLUMN dcraw_wb_mode;
    ALTER TABLE files ADD COLUMN raw_bright REAL;
    ALTER TABLE files ADD COLUMN raw_wb_mode TEXT;
    ALTER TABLE files ADD COLUMN raw_wb_r REAL;
    ALTER TABLE files ADD COLUMN raw_wb_g REAL;
    ALTER TABLE files ADD COLUMN raw_wb_b REAL;
    ALTER TABLE files ADD COLUMN raw_highlight INTEGER;
    """,
    """
    -- Ticket 096/097 (Option B): an exported file keeps its own Photo, browseable wherever it
    -- physically lives, cross-referenced to the file it was exported from by this one nullable
    -- column -- not by merging into the original's Photo the way manual_links' role='export'
    -- (reserved, never used) would have. NULL for every file that isn't itself an export.
    ALTER TABLE files ADD COLUMN exported_from_file_id INTEGER REFERENCES files(id);
    """,
    """
    -- Ticket 109: two more per-file RAW conversion settings, alongside 085's four. NULL means
    -- "no correction", same convention as the others.
    -- raw_exposure: LibRaw exp_shift, a linear multiplier in [0.25, 8.0] (LibRaw's own valid
    -- range) applied before highlight recovery -- distinct from raw_bright, which is a multiplier
    -- LibRaw applies *after* its own auto-brightness scaling. Verified empirically (rendering a
    -- real RAW file both ways) that exp_shift has almost no visible effect unless auto-brightness
    -- is disabled for that render, since LibRaw's auto-bright otherwise renormalizes the result
    -- back toward roughly the same overall brightness regardless of exp_shift -- so a render only
    -- disables auto-brightness (LibRaw's no_auto_bright/noAutoBright) when raw_exposure is set,
    -- leaving every already-tuned or untouched file's rendering exactly as before this ticket.
    -- raw_shadow: not a native LibRaw parameter on either rawpy or LibRaw-Wasm (LibRaw has no
    -- shadow-recovery algorithm) -- a post-decode lift applied to the final RGB output on both
    -- sides identically: v' = v + raw_shadow * (1-v)^2 per channel (v, v' in [0,1]), verified
    -- empirically to be monotonic (no tone reversal) and highlight-neutral only for raw_shadow in
    -- [0, 0.5], which is the range this column's values are validated against.
    ALTER TABLE files ADD COLUMN raw_exposure REAL;
    ALTER TABLE files ADD COLUMN raw_shadow REAL;
    """,
    """
    -- Ticket 112: four advanced per-file RAW conversion settings, shown under a collapsible
    -- "Advanced" section. NULL still means "LibRaw's own default", same convention as the rest.
    -- raw_saturation: not a native LibRaw parameter (LibRaw's user_sat is a white-level/brightness
    -- override, not a saturation multiplier -- tested and rejected for this control). A post-decode
    -- saturation adjustment applied identically on both sides: per pixel, luma = 0.299R+0.587G+
    -- 0.114B and v' = luma + (v - luma) * raw_saturation per channel (v, v' in [0,1]); 1.0 is
    -- identity, range [0.0, 2.0] (0 = grayscale, 2 = double saturation).
    -- raw_contrast: post-decode contrast around mid-gray, applied identically on both sides:
    -- v' = clamp(0.5 + (v - 0.5) * raw_contrast) per channel (v, v' in [0,1]); 1.0 is identity,
    -- range [0.5, 2.0]. A native gamma control (rawpy gamma= / libraw-wasm gamm=) was tried first
    -- and rejected: the vendored libraw-wasm 1.6.0 build silently ignores `gamm` (verified on a real
    -- DNG -- identical output for every value, while outputBps/userQual do take effect), so exact
    -- client/server parity is not achievable that way.
    -- raw_noise: FBDD noise reduction mode, 0=off, 1=light, 2=full (LibRaw fbdd_noiserd).
    -- raw_demosaic: LibRaw demosaic quality (user_qual), one of 0=linear, 1=VNG, 2=PPG, 3=AHD
    -- (default), 4=DCB, 11=DHT, 12=AAHD -- the algorithms this LibRaw build supports (AMAZE/LMMSE
    -- need the GPL demosaic packs, which neither binding here ships).
    ALTER TABLE files ADD COLUMN raw_saturation REAL;
    ALTER TABLE files ADD COLUMN raw_contrast REAL;
    ALTER TABLE files ADD COLUMN raw_noise INTEGER;
    ALTER TABLE files ADD COLUMN raw_demosaic INTEGER;
    """,
    """
    -- Ticket 111: focal length and camera make/model, alongside ticket 083's aperture/shutter/ISO.
    -- focal_length is in millimetres (the EXIF FocalLength rational); camera_make/model are the
    -- stripped IFD0 Make/Model strings. NULL when the file has no EXIF, or lacks that tag. Existing
    -- rows are backfilled by a scan (or the metadata JSON cache) re-reading the file's EXIF.
    ALTER TABLE files ADD COLUMN focal_length REAL;
    ALTER TABLE files ADD COLUMN camera_make TEXT;
    ALTER TABLE files ADD COLUMN camera_model TEXT;
    """,
    """
    -- Ticket 113: when a worker claimed the job (running), so the Jobs page can show what the
    -- worker is busy with and for how long. NULL for queued/done/failed jobs and for rows that
    -- predate this column (their duration falls back to created_at until they run again).
    ALTER TABLE jobs ADD COLUMN started_at TEXT;
    """,
    """
    -- Ticket 115: non-destructive crop, normalized [0..1] fractions of the source frame. All four
    -- NULL means "no crop" (the whole frame); a whole-frame rectangle is normalized back to NULL.
    -- Applies to JPEG and RAW alike; Thumb/Small are rendered cropped, Medium/Huge stay full and
    -- the loupe shades the cropped-out area (ticket 116's answer).
    ALTER TABLE files ADD COLUMN crop_x REAL;
    ALTER TABLE files ADD COLUMN crop_y REAL;
    ALTER TABLE files ADD COLUMN crop_w REAL;
    ALTER TABLE files ADD COLUMN crop_h REAL;
    """,
    """
    -- Ticket 119: a per-file revision bumped whenever the file's rendering changes (raw_settings
    -- or crop saved). The client puts it in the /img/{size}/{id}?r=... URL so a browser that has
    -- the old response cached (Cache-Control: max-age=3600) fetches the fresh thumbnail when it
    -- navigates back to a tuned photo, instead of serving the pre-tune bytes for an unchanged URL.
    ALTER TABLE files ADD COLUMN thumb_rev INTEGER NOT NULL DEFAULT 0;
    """,
    """
    -- Ticket 129: per-file non-destructive rotation, in degrees counter-clockwise, normalized to
    -- one of 90/180/270 (NULL and 0 both mean "not rotated"; 0 is normalized back to NULL, like a
    -- whole-frame crop). Applied as the *final* render transform in thumbs._open, after the source's
    -- metadata orientation (EXIF transpose / LibRaw flip) and after the crop -- so this fixes a
    -- bad orientation the metadata got wrong without fighting the metadata itself. DB-only, not
    -- written to sidecars (it is a physical-file rendering fix, like crop/raw settings).
    ALTER TABLE files ADD COLUMN rotation INTEGER;
    """,
    """
    -- Ticket 156: the lens used with the picture, from the EXIF LensModel tag (Exif sub-IFD),
    -- alongside ticket 111's focal length / camera make/model. NULL when the file has no EXIF, or
    -- no LensModel tag. Existing rows are backfilled by a scan (or the metadata JSON cache)
    -- re-reading the file's EXIF -- adding lens_model to metacache.REQUIRED_KEYS makes the next
    -- scan reprocess directories whose cache predates it (ticket 111's own mechanism).
    ALTER TABLE files ADD COLUMN lens_model TEXT;
    """,
    """
    -- Ticket 170: the 35mm full-frame-equivalent focal length, from the EXIF
    -- FocalLengthIn35mmFilm tag (0xA405, Exif sub-IFD), an integer number of millimetres.
    -- NULL when the file has no EXIF or no such tag. Existing rows are filled by the cheap
    -- focal_length_35mm probe on the next scan (tickets 169/171), not a forced RAW re-read.
    ALTER TABLE files ADD COLUMN focal_length_35mm INTEGER;
    """,
    """
    -- Ticket 175: every AI prompt (text + response schema) that was ever used, keyed by its content
    -- hash, so a cached response (ticket 179) can always be traced to the exact text that made it
    -- even after the prompt file is edited. No version numbers: the hash is the version.
    CREATE TABLE ai_prompts (
      hash TEXT PRIMARY KEY, name TEXT NOT NULL, text TEXT NOT NULL, schema TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    """,
    """
    -- Ticket 179: AI rating. ai_responses caches Gemini's raw structured answers per (image,
    -- prompt content hash, model): an unchanged prompt never re-sends the picture, a changed prompt
    -- or model has a different key and so does. image_key is "<files.hash>:<thumb_rev>" (the sent
    -- Medium rendition depends on content and on crop/RAW tuning). files.ai_score is the single
    -- number computed from those answers by ai_score.score(); ai_score_version is that function's
    -- content hash, so editing the function marks every stored score stale (rescored locally).
    CREATE TABLE ai_responses (
      image_key TEXT NOT NULL, prompt_hash TEXT NOT NULL, model TEXT NOT NULL,
      answers TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY (image_key, prompt_hash, model)
    );
    ALTER TABLE files ADD COLUMN ai_score REAL;
    ALTER TABLE files ADD COLUMN ai_score_version TEXT;
    """,
    """
    -- Ticket 187: video metadata (from ffprobe). duration is seconds, fps frames per second,
    -- video_codec the first video stream's codec name (h264, mpeg4, ...). NULL for images and for
    -- videos not probed yet (ffprobe missing). width/height/exif_date of a video are filled by the
    -- same scan, rotation-aware (a portrait phone clip stored as rotated landscape is portrait).
    ALTER TABLE files ADD COLUMN duration REAL;
    ALTER TABLE files ADD COLUMN fps REAL;
    ALTER TABLE files ADD COLUMN video_codec TEXT;
    """,
    """
    -- Ticket 190: a video whose thumbnails could not be made (undecodable, ffmpeg timeout) is not
    -- retried on every populate pass: skipped until retry_after passes or the file changes
    -- (file_mtime no longer matches files.mtime). Cleared by a forced re-render (thumbs.clear).
    CREATE TABLE video_failures (
      file_id INTEGER PRIMARY KEY REFERENCES files(id),
      error TEXT NOT NULL, failed_at REAL NOT NULL, file_mtime REAL
    );
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
