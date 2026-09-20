"""Walk the pictures directory and keep the files/dir_mtimes tables current.

Same rules as file_metadata.py: a directory whose mtime matches
dir_mtimes is skipped; within a rescanned directory a file whose mtime
and size are unchanged is not re-read or re-hashed. Vanished files are
marked missing=1, never deleted. All paths in the database are relative
to the pictures dir with '/' separators.
"""

import concurrent.futures
import dataclasses
import hashlib
import json
import os
import threading

from absl import logging

from photoapp import db
from photoapp import fileinfo
from photoapp import grouping
from photoapp import manual_links
from photoapp import ratings
from photoapp import thumbs
from photoapp import xmp


@dataclasses.dataclass
class Progress:
  running: bool = False
  dirs_seen: int = 0
  dirs_skipped: int = 0
  files_seen: int = 0
  files_processed: int = 0
  sidecars_processed: int = 0
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
                progress, pool):
  """Read new or changed image files of one directory.

  The pool runs the per-file work (stat, decode, hash), which is dominated by
  network file system latency; database writes stay on this thread.
  """
  changed = False
  known = {
      row["path"]: row for row in conn.execute(
          "SELECT path, mtime, bytesize, missing FROM files "
          "WHERE path LIKE ? ESCAPE '\\'",
          (_like_prefix(rel_dir) + "%",))
  }

  def work(name):
    filepath = os.path.join(dirpath, name)
    try:
      if not os.path.isfile(filepath):
        return None
      st = os.stat(filepath)
    except OSError as e:
      logging.error("Skipping %s: %s", filepath, e)
      return None
    rel_path = name if rel_dir == "." else f"{rel_dir}/{name}"
    old = known.get(rel_path)
    if (old is not None and old["mtime"] == st.st_mtime
        and old["bytesize"] == st.st_size and not old["missing"]):
      return None
    mime_type, width, height, exif_date = fileinfo.read_image_metadata(filepath)
    file_hash = fileinfo.get_or_compute_hash(
        filepath, rel_path, st.st_mtime, hashes)
    return rel_path, {
        "hash": file_hash, "mime_type": mime_type, "width": width,
        "height": height, "bytesize": st.st_size, "mtime": st.st_mtime,
        "exif_date": exif_date}

  for result in pool.map(work, filenames):
    if result is None:
      continue
    _upsert_file(conn, *result)
    progress.files_processed += 1
    changed = True
  return changed


def _sidecar_owners(image_names, sidecar_names):
  """Map each sidecar name to the image filename that owns it (or None).

  A full-filename sidecar (K.JPG.xmp) belongs to that file; a bare
  K.xmp belongs to the RAW with that stem if there is one, else to an
  image with that stem. Sidecars with no matching image are orphans.
  """
  best = {}  # sidecar name -> (rank, image name)
  for image in image_names:
    full = (image + ".xmp").lower()
    for sc in xmp.find_sidecars(image, sidecar_names):
      rank = 0 if sc.lower() == full else (1 if fileinfo.is_raw(image) else 2)
      if sc not in best or (rank, image) < best[sc]:
        best[sc] = (rank, image)
  return {sc: best[sc][1] if sc in best else None for sc in sidecar_names}


def _sync_sidecars(conn, dirpath, rel_dir, filenames, images, known, progress,
                   pool):
  """Bring xmp_sidecars for one directory in line with the disk.

  Runs on every scan, also for directories skipped by the mtime rule:
  editing a sidecar in place does not change its directory's mtime.
  known is {sidecar_name: row} for this directory from the database.
  """
  sidecar_names = sorted(n for n in filenames if xmp.is_sidecar(n))
  changed = False
  if not sidecar_names and not known:
    return changed
  file_ids = {
      r["path"]: r["id"] for r in conn.execute(
          "SELECT id, path FROM files WHERE path LIKE ? ESCAPE '\\'",
          (_like_prefix(rel_dir) + "%",))
  }
  prefix = "" if rel_dir == "." else rel_dir + "/"
  owners = _sidecar_owners(images, sidecar_names)
  def work(name):
    """(mtime, data or None): data is read only if the sidecar changed."""
    full = os.path.join(dirpath, name)
    try:
      mtime = os.stat(full).st_mtime
      old = known.get(name)
      if old is not None and old["mtime"] == mtime:
        return mtime, None
      with open(full, "rb") as f:
        return mtime, f.read()
    except OSError as e:
      logging.error("Skipping sidecar %s: %s", prefix + name, e)
      return None

  for name, result in zip(sidecar_names, pool.map(work, sidecar_names)):
    if result is None:
      continue
    mtime, data = result
    path = prefix + name
    owner = owners[name]
    file_id = file_ids.get(prefix + owner) if owner else None
    if data is None:                       # unchanged on disk
      if known[name]["file_id"] != file_id:
        conn.execute("UPDATE xmp_sidecars SET file_id = ? WHERE path = ?",
                     (file_id, path))
        changed = True
      continue
    parsed = xmp.parse(data)
    conn.execute(
        "INSERT INTO xmp_sidecars (path, file_id, mtime, hash, rating,"
        " has_fav, tags) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET file_id = excluded.file_id,"
        " mtime = excluded.mtime, hash = excluded.hash,"
        " rating = excluded.rating, has_fav = excluded.has_fav,"
        " tags = excluded.tags",
        (path, file_id, mtime, hashlib.sha224(data).hexdigest(),
         parsed.rating, int(parsed.fav), json.dumps(list(parsed.tags))))
    changed = True
    progress.sidecars_processed += 1
  for name in known:
    if name not in sidecar_names:
      conn.execute("DELETE FROM xmp_sidecars WHERE path = ?", (prefix + name,))
      changed = True
  conn.commit()
  return changed


def _load_known_sidecars(conn):
  """Return {rel_dir: {sidecar_name: row}} for all sidecars in the database."""
  result = {}
  for row in conn.execute(
      "SELECT path, file_id, mtime FROM xmp_sidecars"):
    dirname, _, name = row["path"].rpartition("/")
    result.setdefault(dirname or ".", {})[name] = row
  return result


def _like_prefix(rel_dir):
  """LIKE pattern prefix for paths directly or indirectly under rel_dir."""
  if rel_dir == ".":
    return ""
  escaped = rel_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
  return escaped + "/"


def scan(conn, pictures_dir, scan_dir=None, hashes=None, progress=None,
         thumbs_dir=None, on_done=None, workers=8):
  """Scan scan_dir (default: pictures_dir) into conn. Returns the Progress.

  With thumbs_dir, thumbnails that already exist for new files are recorded.
  """
  progress = progress or Progress()
  progress.running = True
  pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers))
  scan_dir = scan_dir or pictures_dir
  seen = set()
  changed_dirs = set()
  known_sidecars = _load_known_sidecars(conn)
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

      def sync_sidecars():
        if _sync_sidecars(conn, dirpath, rel_dir, filenames, images,
                          known_sidecars.get(rel_dir, {}), progress, pool):
          changed_dirs.add(rel_dir)

      row = conn.execute(
          "SELECT mtime FROM dir_mtimes WHERE dirpath = ?",
          (rel_dir,)).fetchone()
      if row is not None and row["mtime"] == mtime:
        progress.dirs_skipped += 1
        sync_sidecars()
        continue

      if _scan_files(conn, pictures_dir, dirpath, rel_dir, images, hashes,
                     progress, pool):
        changed_dirs.add(rel_dir)
      sync_sidecars()
      conn.execute(
          "INSERT INTO dir_mtimes (dirpath, mtime) VALUES (?, ?) "
          "ON CONFLICT(dirpath) DO UPDATE SET mtime = excluded.mtime",
          (rel_dir, mtime))
      conn.commit()

    _mark_missing(conn, pictures_dir, scan_dir, seen)
    conn.commit()
    # Also directories left ungrouped by an earlier interrupted scan.
    changed_dirs.update(
        r["path"].rpartition("/")[0] or "." for r in conn.execute(
            "SELECT path FROM files WHERE photo_id IS NULL"))
    grouping.regroup(conn, changed_dirs)
    manual_links.apply_all(conn)
    ratings.refresh_dirs(conn, changed_dirs)
    ratings.refresh_unresolved(conn)
    if thumbs_dir:
      thumbs.index_existing(conn, thumbs_dir, changed_dirs)
    if on_done:
      on_done(conn)
  except Exception as e:
    logging.exception("scan failed")
    progress.error = str(e)
  finally:
    pool.shutdown(wait=True)
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

  def __init__(self, db_path, pictures_dir, hashes=None, thumbs_dir=None,
               on_done=None, workers=8):
    self._db_path = db_path
    self._pictures_dir = pictures_dir
    self._hashes = hashes
    self._thumbs_dir = thumbs_dir
    self._on_done = on_done
    self._workers = workers
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
      scan(conn, self._pictures_dir, scan_dir, self._hashes, progress,
           self._thumbs_dir, self._on_done, self._workers)
    finally:
      conn.close()

  def wait(self):
    if self._thread is not None:
      self._thread.join()


def seconds_until(hour, now):
  """Seconds from now (a datetime) until the next local HH:00."""
  import datetime
  target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
  if target <= now:
    target += datetime.timedelta(days=1)
  return (target - now).total_seconds()


class NightlyScan:
  """Starts a scan every day at a given local hour, in a daemon thread."""

  def __init__(self, manager, hour, wait=None):
    self._manager = manager
    self._hour = hour
    self._stop = threading.Event()
    self._wait = wait or self._stop.wait      # injectable for tests
    self._thread = None
    self.runs = 0

  def start(self):
    self._thread = threading.Thread(target=self._loop, daemon=True,
                                    name="nightly-scan")
    self._thread.start()

  def stop(self):
    self._stop.set()

  def _loop(self):
    import datetime
    while not self._stop.is_set():
      self._wait(seconds_until(self._hour, datetime.datetime.now()))
      if self._stop.is_set():
        return
      if self._manager.start():        # False: a scan is already running
        self.runs += 1
        logging.info("nightly scan started")
