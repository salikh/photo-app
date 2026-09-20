"""Walk the pictures directory and keep the files/dir_mtimes tables current.

Same rules as file_metadata.py: a directory whose mtime matches
dir_mtimes is skipped; within a rescanned directory a file whose mtime
and size are unchanged is not re-read or re-hashed. Vanished files are
marked missing=1, never deleted. All paths in the database are relative
to the pictures dir with '/' separators.
"""

import dataclasses
import os
import threading

from absl import logging

from photoapp import db
from photoapp import fileinfo


@dataclasses.dataclass
class Progress:
  running: bool = False
  dirs_seen: int = 0
  dirs_skipped: int = 0
  files_seen: int = 0
  files_processed: int = 0
  error: str = None


def _rel(pictures_dir, path):
  rel = os.path.relpath(path, pictures_dir)
  return "." if rel == "." else rel.replace(os.sep, "/")


def _upsert_file(conn, rel_path, record):
  conn.execute(
      "INSERT INTO files (path, hash, mime_type, width, height, bytesize,"
      " mtime, exif_date, missing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0) "
      "ON CONFLICT(path) DO UPDATE SET hash = excluded.hash,"
      " mime_type = excluded.mime_type, width = excluded.width,"
      " height = excluded.height, bytesize = excluded.bytesize,"
      " mtime = excluded.mtime, exif_date = excluded.exif_date, missing = 0",
      (rel_path, record["hash"], record["mime_type"], record["width"],
       record["height"], record["bytesize"], record["mtime"],
       record["exif_date"]))


def _scan_files(conn, pictures_dir, dirpath, rel_dir, filenames, hashes,
                progress):
  known = {
      row["path"]: row for row in conn.execute(
          "SELECT path, mtime, bytesize, missing FROM files "
          "WHERE path LIKE ? ESCAPE '\\'",
          (_like_prefix(rel_dir) + "%",))
  }
  for name in filenames:
    filepath = os.path.join(dirpath, name)
    if not os.path.isfile(filepath):
      continue
    try:
      st = os.stat(filepath)
    except OSError as e:
      logging.error("Skipping %s: %s", filepath, e)
      continue
    rel_path = name if rel_dir == "." else f"{rel_dir}/{name}"
    old = known.get(rel_path)
    if (old is not None and old["mtime"] == st.st_mtime
        and old["bytesize"] == st.st_size and not old["missing"]):
      continue
    mime_type, width, height, exif_date = fileinfo.read_image_metadata(filepath)
    file_hash = fileinfo.get_or_compute_hash(
        filepath, rel_path, st.st_mtime, hashes)
    _upsert_file(conn, rel_path, {
        "hash": file_hash, "mime_type": mime_type, "width": width,
        "height": height, "bytesize": st.st_size, "mtime": st.st_mtime,
        "exif_date": exif_date})
    progress.files_processed += 1


def _like_prefix(rel_dir):
  """LIKE pattern prefix for paths directly or indirectly under rel_dir."""
  if rel_dir == ".":
    return ""
  escaped = rel_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
  return escaped + "/"


def scan(conn, pictures_dir, scan_dir=None, hashes=None, progress=None):
  """Scan scan_dir (default: pictures_dir) into conn. Returns the Progress."""
  progress = progress or Progress()
  progress.running = True
  scan_dir = scan_dir or pictures_dir
  seen = set()
  try:
    for dirpath, dirnames, filenames in os.walk(scan_dir):
      dirnames.sort()
      rel_dir = _rel(pictures_dir, dirpath)
      images = sorted(n for n in filenames if fileinfo.is_image(n))
      progress.dirs_seen += 1
      progress.files_seen += len(images)
      seen.update(images if rel_dir == "." else
                  (f"{rel_dir}/{n}" for n in images))

      mtime = os.stat(dirpath).st_mtime
      row = conn.execute(
          "SELECT mtime FROM dir_mtimes WHERE dirpath = ?",
          (rel_dir,)).fetchone()
      if row is not None and row["mtime"] == mtime:
        progress.dirs_skipped += 1
        continue

      _scan_files(conn, pictures_dir, dirpath, rel_dir, images, hashes,
                  progress)
      conn.execute(
          "INSERT INTO dir_mtimes (dirpath, mtime) VALUES (?, ?) "
          "ON CONFLICT(dirpath) DO UPDATE SET mtime = excluded.mtime",
          (rel_dir, mtime))
      conn.commit()

    _mark_missing(conn, pictures_dir, scan_dir, seen)
    conn.commit()
  except Exception as e:
    logging.exception("scan failed")
    progress.error = str(e)
  finally:
    progress.running = False
  return progress


def _mark_missing(conn, pictures_dir, scan_dir, seen):
  """Mark files under scan_dir that were not seen this pass as missing."""
  rel_scan = _rel(pictures_dir, scan_dir)
  rows = conn.execute(
      "SELECT id, path FROM files WHERE missing = 0 AND path LIKE ? "
      "ESCAPE '\\'", (_like_prefix(rel_scan) + "%",)).fetchall()
  gone = [(r["id"],) for r in rows if r["path"] not in seen]
  conn.executemany("UPDATE files SET missing = 1 WHERE id = ?", gone)
  return len(gone)


class ScanManager:
  """Runs at most one scan at a time in a background thread."""

  def __init__(self, db_path, pictures_dir, hashes=None):
    self._db_path = db_path
    self._pictures_dir = pictures_dir
    self._hashes = hashes
    self._lock = threading.Lock()
    self._thread = None
    self.progress = Progress()

  def start(self, rel_dir=None):
    """Start a scan; returns False if one is already running."""
    with self._lock:
      if self._thread is not None and self._thread.is_alive():
        return False
      scan_dir = self._pictures_dir
      if rel_dir and rel_dir != ".":
        scan_dir = os.path.normpath(os.path.join(self._pictures_dir, rel_dir))
        if not scan_dir.startswith(self._pictures_dir.rstrip("/") + "/"):
          raise ValueError("directory outside pictures_dir")
      self.progress = Progress(running=True)
      self._thread = threading.Thread(
          target=self._run, args=(scan_dir, self.progress), daemon=True)
      self._thread.start()
      return True

  def _run(self, scan_dir, progress):
    conn = db.connect(self._db_path)
    try:
      scan(conn, self._pictures_dir, scan_dir, self._hashes, progress)
    finally:
      conn.close()

  def wait(self):
    if self._thread is not None:
      self._thread.join()
