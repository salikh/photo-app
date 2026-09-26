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
import resource
import threading

from absl import logging

from photoapp import db
from photoapp import fileinfo
from photoapp import grouping
from photoapp import manual_links
from photoapp import metacache
from photoapp import paths
from photoapp import ratings
from photoapp import thumbs
from photoapp import trash
from photoapp import xmp


@dataclasses.dataclass
class Progress:
  running: bool = False
  dirs_seen: int = 0
  dirs_skipped: int = 0
  files_seen: int = 0
  files_processed: int = 0
  sidecars_processed: int = 0
  current_dir: str = None    # top-level step being scanned (per-year scans)
  steps_done: int = 0
  steps_total: int = 0
  error: str = None


def _rel(pictures_dir, path):
  rel = os.path.relpath(path, pictures_dir)
  return "." if rel == "." else rel.replace(os.sep, "/")


def _upsert_file(conn, rel_path, record):
  conn.execute(
      "INSERT INTO files (path, hash, mime_type, width, height, bytesize,"
      " mtime, exif_date, aperture, shutter_speed, iso, focal_length,"
      " camera_make, camera_model, missing) VALUES"
      " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
      "ON CONFLICT(path) DO UPDATE SET hash = excluded.hash,"
      " mime_type = excluded.mime_type, width = excluded.width,"
      " height = excluded.height, bytesize = excluded.bytesize,"
      " mtime = excluded.mtime, exif_date = excluded.exif_date,"
      " aperture = excluded.aperture, shutter_speed = excluded.shutter_speed,"
      " iso = excluded.iso, focal_length = excluded.focal_length,"
      " camera_make = excluded.camera_make, camera_model = excluded.camera_model,"
      " missing = 0",
      (rel_path, record["hash"], record["mime_type"], record["width"],
       record["height"], record["bytesize"], record["mtime"],
       record["exif_date"], record["aperture"], record["shutter_speed"],
       record["iso"], record["focal_length"], record["camera_make"],
       record["camera_model"]))


def import_single_file(conn, pictures_dir, rel_path, hashes=None):
  """Read and upsert one already-on-disk file into `files` outside of a directory walk (ticket
   099: a just-written export, known by path rather than discovered by os.walk) -- the same
  metadata extraction _scan_files does per file, without its directory-level batching. Returns
  the file's id. Does not group it into a Photo; call grouping.regroup afterwards for that."""
  full = os.path.join(pictures_dir, rel_path)
  st = os.stat(full)
  (mime_type, width, height, exif_date, aperture, shutter_speed, iso,
   focal_length, camera_make, camera_model) = fileinfo.read_image_metadata(full)
  file_hash = fileinfo.get_or_compute_hash(full, rel_path, st.st_mtime, hashes)
  _upsert_file(conn, rel_path, {
      "hash": file_hash, "mime_type": mime_type, "width": width, "height": height,
      "bytesize": st.st_size, "mtime": st.st_mtime, "exif_date": exif_date,
      "aperture": aperture, "shutter_speed": shutter_speed, "iso": iso,
      "focal_length": focal_length, "camera_make": camera_make,
      "camera_model": camera_model})
  return conn.execute("SELECT id FROM files WHERE path = ?", (rel_path,)).fetchone()["id"]


def _lookup_files(conn, rel_dir, names, columns="id, path, mtime, bytesize, missing"):
  """{path: row} for the given file names in rel_dir, by exact path.

  Cost is proportional to the names asked for, not to the size of the
  directory's subtree (LIKE 'dir/%' would read every file below it).
  """
  prefix = "" if rel_dir == "." else rel_dir + "/"
  wanted = [prefix + n for n in names]
  found = {}
  for i in range(0, len(wanted), 500):
    chunk = wanted[i:i + 500]
    for row in conn.execute(
        f"SELECT {columns} FROM files WHERE path IN ({','.join('?' * len(chunk))})",
        chunk):
      found[row["path"]] = row
  return found


def _scan_files(conn, pictures_dir, dirpath, rel_dir, filenames, hashes,
                progress, pool, metadata_cache=False):
  """Read new or changed image files of one directory.

  The pool runs the per-file work (stat, decode, hash), which is dominated by
  network file system latency; database writes stay on this thread.

  With metadata_cache (ticket 111), a file with a complete, still-valid record
  in the directory's on-disk index.json is reused instead of decoded: only its
  database row is refreshed from the record. A record that is still valid but
  lacks a key added later (e.g. focal_length) is backfilled by re-reading just
  that file's metadata, keeping its cached hash. Returns (changed, records):
  records is {name: record} for every current image file (used to write the new
  index.json), or {} when the cache is off.
  """
  changed = False
  # Include hash: the ticket 111 cache path below reuses an unchanged file's DB hash when its
  # on-disk record is missing/incomplete (ticket 126: the default columns don't carry it).
  known = _lookup_files(conn, rel_dir, filenames, "id, path, mtime, bytesize, missing, hash")
  existing = metacache.load_records(dirpath) if metadata_cache else {}

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

    if metadata_cache:
      cached = existing.get(name)
      cache_valid = (cached is not None and cached.get("mtime") == st.st_mtime
                     and cached.get("bytesize") == st.st_size)
      if cache_valid and metacache.has_all_keys(cached):
        return name, rel_path, dict(cached), False
      # Valid but incomplete (a key added later), or no usable record: re-read the metadata.
      (mime_type, width, height, exif_date, aperture, shutter_speed, iso,
       focal_length, camera_make, camera_model) = fileinfo.read_image_metadata(filepath)
      file_hash = None
      if cache_valid and cached.get("hash") is not None:
        file_hash = cached["hash"]
      elif (old is not None and old["mtime"] == st.st_mtime
            and old["bytesize"] == st.st_size and not old["missing"]):
        file_hash = old["hash"]
      if file_hash is None:
        file_hash = fileinfo.get_or_compute_hash(
            filepath, rel_path, st.st_mtime, hashes)
      logging.vlog(7, "scanned %s (%s, %dx%d)", rel_path, mime_type, width, height)
      return name, rel_path, {
          "hash": file_hash, "mime_type": mime_type, "width": width,
          "height": height, "bytesize": st.st_size, "mtime": st.st_mtime,
          "exif_date": exif_date, "aperture": aperture,
          "shutter_speed": shutter_speed, "iso": iso,
          "focal_length": focal_length, "camera_make": camera_make,
          "camera_model": camera_model}, True

    if (old is not None and old["mtime"] == st.st_mtime
        and old["bytesize"] == st.st_size and not old["missing"]):
      return None
    (mime_type, width, height, exif_date, aperture, shutter_speed, iso,
     focal_length, camera_make, camera_model) = fileinfo.read_image_metadata(filepath)
    file_hash = fileinfo.get_or_compute_hash(
        filepath, rel_path, st.st_mtime, hashes)
    logging.vlog(7, "scanned %s (%s, %dx%d)", rel_path, mime_type, width, height)
    return name, rel_path, {
        "hash": file_hash, "mime_type": mime_type, "width": width,
        "height": height, "bytesize": st.st_size, "mtime": st.st_mtime,
        "exif_date": exif_date, "aperture": aperture,
        "shutter_speed": shutter_speed, "iso": iso,
        "focal_length": focal_length, "camera_make": camera_make,
        "camera_model": camera_model}, True

  records = {}
  for result in pool.map(work, filenames):
    if result is None:
      continue
    name, rel_path, record, was_read = result
    records[name] = record
    _upsert_file(conn, rel_path, record)
    if was_read:
      progress.files_processed += 1
      changed = True
  return changed, records


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
  file_ids = {p: r["id"] for p, r in _lookup_files(conn, rel_dir, images).items()}
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
    logging.vlog(5, "synced sidecar %s (rating=%s, fav=%s, %d tags)",
                 path, parsed.rating, parsed.fav, len(parsed.tags))
  for name in known:
    if name not in sidecar_names:
      conn.execute("DELETE FROM xmp_sidecars WHERE path = ?", (prefix + name,))
      changed = True
  conn.commit()
  return changed


def _load_known_sidecars(conn, rel_scan, recursive=True):
  """{rel_dir: {sidecar_name: row}} for the sidecars under rel_scan."""
  lo, hi = paths.subtree_range(rel_scan)
  result = {}
  for row in conn.execute(
      "SELECT path, file_id, mtime FROM xmp_sidecars WHERE path >= ? AND path < ?",
      (lo, hi)):
    dirname, _, name = row["path"].rpartition("/")
    d = dirname or "."
    if recursive or d == rel_scan:
      result.setdefault(d, {})[name] = row
  return result


def _scan_subtree(conn, pictures_dir, scan_dir, recursive, hashes, progress,
                  thumbs_dir, on_done, pool, metadata_cache=False):
  """One complete unit of work: read a subtree, then make it visible.

  Everything after the walk (grouping, ratings, thumbnails, missing files)
  is limited to this subtree, so when it returns the subtree is fully
  usable in the app even if a later subtree is never scanned.
  """
  rel_scan = _rel(pictures_dir, scan_dir)
  before = (progress.dirs_seen, progress.dirs_skipped, progress.files_seen,
           progress.files_processed, progress.sidecars_processed)
  seen = set()
  changed_dirs = set()
  known_sidecars = _load_known_sidecars(conn, rel_scan, recursive)
  for dirpath, dirnames, filenames in os.walk(scan_dir):
    dirnames.sort()
    if dirpath == pictures_dir and trash.TRASH_DIRNAME in dirnames:
      dirnames.remove(trash.TRASH_DIRNAME)   # never scanned (ticket 072), even by a direct walk
    if not recursive:
      dirnames[:] = []
    filenames = [n for n in filenames if not fileinfo.is_ignored(n)]   # ticket 101
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
    # Ticket 111: an unchanged directory still needs reprocessing when its metadata cache lacks a
    # key added since it was written (e.g. focal_length), so that record can be backfilled.
    if (row is not None and row["mtime"] == mtime
        and not (metadata_cache and metacache.index_lacks_keys(dirpath, images))):
      progress.dirs_skipped += 1
      logging.vlog(7, "%s: unchanged, skipping (%d files)", rel_dir, len(images))
      sync_sidecars()
      continue

    logging.vlog(3, "scanning %s (%d files)", rel_dir, len(images))
    changed_files, records = _scan_files(
        conn, pictures_dir, dirpath, rel_dir, images, hashes, progress, pool,
        metadata_cache)
    if changed_files:
      changed_dirs.add(rel_dir)
    if metadata_cache:
      # Writing the cache changes dirpath's mtime; write_index re-reads it after the write so the
      # dir_mtimes row below records the directory's true final mtime and the next scan skips it.
      mtime = metacache.write_dir(dirpath, mtime, records)
    sync_sidecars()
    conn.execute(
        "INSERT INTO dir_mtimes (dirpath, mtime) VALUES (?, ?) "
        "ON CONFLICT(dirpath) DO UPDATE SET mtime = excluded.mtime",
        (rel_dir, mtime))
    conn.commit()

  gone = _mark_missing(conn, rel_scan, recursive, seen)
  conn.commit()
  if gone:
    logging.vlog(3, "%s: %d file(s) no longer found, marked missing", rel_scan, gone)
  # Also directories of this subtree left ungrouped by an interrupted scan.
  lo, hi = paths.subtree_range(rel_scan)
  for r in conn.execute(
      "SELECT path FROM files WHERE photo_id IS NULL AND path >= ? AND path < ?",
      (lo, hi)):
    d = paths.dirname(r["path"])
    if recursive or d == rel_scan:
      changed_dirs.add(d)
  grouping.regroup(conn, changed_dirs)
  _apply_grouping_rule_version(conn, rel_scan, recursive, scan_dir == pictures_dir)
  manual_links.apply_all(conn)
  ratings.refresh_dirs(conn, changed_dirs)
  ratings.refresh_unresolved(conn)
  if thumbs_dir:
    thumbs.index_existing(conn, thumbs_dir, changed_dirs)
  if on_done:
    on_done(conn)
  logging.vlog(1, "%s: %d dirs seen (%d skipped), %d files seen (%d read), "
               "%d sidecars synced, %d changed dirs",
               rel_scan, progress.dirs_seen - before[0], progress.dirs_skipped - before[1],
               progress.files_seen - before[2], progress.files_processed - before[3],
               progress.sidecars_processed - before[4], len(changed_dirs))


def _apply_grouping_rule_version(conn, rel_scan, recursive, whole_library):
  """Regroup a scope once when the grouping rule has changed since it was done.

  The version is kept per scope that is a complete unit (the whole library,
  a top-level directory, or the files directly in the root), so per-year
  scans migrate one year at a time.
  """
  if whole_library and recursive:
    grouping.regroup_if_rule_changed(conn)
  elif rel_scan == "." and not recursive:
    grouping.regroup_scope_if_rule_changed(conn, ".", False)
  elif recursive and "/" not in rel_scan and rel_scan != ".":
    grouping.regroup_scope_if_rule_changed(conn, rel_scan, True)


def scan(conn, pictures_dir, scan_dir=None, hashes=None, progress=None,
         thumbs_dir=None, on_done=None, workers=8, recursive=True,
         metadata_cache=False):
  """Scan scan_dir (default: pictures_dir) into conn. Returns the Progress.

  With thumbs_dir, thumbnails that already exist for new files are recorded.
  recursive=False reads only the files directly in scan_dir.
  With metadata_cache (ticket 111), read and write the on-disk index.json /
  per-file JSON cache described in photoapp/metacache.py.
  """
  progress = progress or Progress()
  progress.running = True
  pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers))
  try:
    _scan_subtree(conn, pictures_dir, scan_dir or pictures_dir, recursive,
                  hashes, progress, thumbs_dir, on_done, pool, metadata_cache)
  except Exception as e:
    logging.exception("scan failed")
    progress.error = str(e)
  finally:
    pool.shutdown(wait=True)
    progress.running = False
  return progress


def top_level_steps(pictures_dir):
  """The steps of a whole-library scan: the root's own files, then each
  top-level directory in sorted order. [(rel_dir, recursive)]

  Skips trash.TRASH_DIRNAME (ticket 072): unlike other hidden (dot-prefixed) folders, which are
  scanned but not listed, a trashed file's sidecar still says what it always said (e.g. reject),
  so scanning it back in would silently re-create it as a brand new, live Photo -- trash is meant
  to be inert until restored by hand or purged, not part of the library at all.
  """
  names = sorted(e.name for e in os.scandir(pictures_dir)
                 if e.is_dir(follow_symlinks=False) and e.name != trash.TRASH_DIRNAME)
  return [(".", False)] + [(n, True) for n in names]


def scan_all(conn, pictures_dir, dirs=None, hashes=None, progress=None,
             thumbs_dir=None, on_done=None, workers=8, metadata_cache=False):
  """Scan the library one top-level directory at a time.

  Each step is complete on its own (see _scan_subtree), so an interrupted
  run leaves every finished directory usable, and a rerun skips them by
  mtime. dirs restricts the steps to the given relative directories (each
  scanned recursively). Returns the shared Progress.
  """
  progress = progress or Progress()
  progress.running = True
  pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers))
  try:
    steps = ([(d.strip("/") or ".", True) for d in dirs] if dirs
             else top_level_steps(pictures_dir))
    progress.steps_total = len(steps)
    progress.steps_done = 0
    for rel_dir, recursive in steps:
      progress.current_dir = "(files in the root)" if rel_dir == "." else rel_dir
      before = (progress.files_seen, progress.files_processed,
                progress.sidecars_processed)
      target = pictures_dir if rel_dir == "." else os.path.join(pictures_dir, rel_dir)
      if not os.path.isdir(target):
        logging.warning("skipping %s: not a directory", target)
      else:
        _scan_subtree(conn, pictures_dir, target, recursive, hashes, progress,
                      thumbs_dir, on_done, pool, metadata_cache)
      progress.steps_done += 1
      logging.info("done %s (%d/%d): %d files seen, %d read, %d sidecars, "
                   "peak memory %d MB", progress.current_dir, progress.steps_done,
                   progress.steps_total, progress.files_seen - before[0],
                   progress.files_processed - before[1],
                   progress.sidecars_processed - before[2],
                   resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
    if not dirs:
      _mark_missing_top_levels(conn, {d for d, _ in steps})
      conn.commit()
  except Exception as e:
    logging.exception("scan failed")
    progress.error = str(e)
  finally:
    pool.shutdown(wait=True)
    progress.current_dir = None
    progress.running = False
  return progress


def _mark_missing_top_levels(conn, present):
  """Mark files under top-level directories that no longer exist as missing."""
  tops = {r[0] for r in conn.execute(
      "SELECT DISTINCT substr(path, 1, instr(path, '/') - 1) FROM files "
      "WHERE instr(path, '/') > 0")}
  for top in sorted(tops - present):
    lo, hi = paths.subtree_range(top)
    conn.execute("UPDATE files SET missing = 1 WHERE path >= ? AND path < ? "
                 "AND missing = 0", (lo, hi))


def _mark_missing(conn, rel_scan, recursive, seen):
  """Mark files under rel_scan that were not seen this pass as missing."""
  lo, hi = paths.subtree_range(rel_scan)
  gone = []
  for r in conn.execute(
      "SELECT id, path FROM files WHERE missing = 0 AND path >= ? AND path < ?",
      (lo, hi)).fetchall():
    if not recursive and paths.dirname(r["path"]) != rel_scan:
      continue
    if r["path"] not in seen:
      gone.append((r["id"],))
  conn.executemany("UPDATE files SET missing = 1 WHERE id = ?", gone)
  return len(gone)


class ScanManager:
  """Runs at most one scan at a time in a background thread."""

  def __init__(self, db_path, pictures_dir, hashes=None, thumbs_dir=None,
               on_done=None, workers=8, metadata_cache=False):
    self._db_path = db_path
    self._pictures_dir = pictures_dir
    self._hashes = hashes
    self._thumbs_dir = thumbs_dir
    self._on_done = on_done
    self._workers = workers
    self._metadata_cache = metadata_cache
    self._lock = threading.Lock()
    self._thread = None
    self.progress = Progress()

  def start(self, rel_dir=None, recursive=True):
    """Start a scan; returns False if one is already running.

    recursive (ticket 127): whether the requested directory's subtree is walked, or only the files
    directly in it. Only the whole-library case (the root with recursive) uses scan_all; a
    non-recursive root scan reads just the root's own files.
    """
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
          target=self._run, args=(scan_dir, recursive, self.progress), daemon=True)
      self._thread.start()
      return True

  def _run(self, scan_dir, recursive, progress):
    conn = db.connect(self._db_path, busy_timeout=60.0)
    try:
      if scan_dir == self._pictures_dir and recursive:
        scan_all(conn, self._pictures_dir, None, self._hashes, progress,
                 self._thumbs_dir, self._on_done, self._workers,
                 self._metadata_cache)
      else:
        scan(conn, self._pictures_dir, scan_dir, self._hashes, progress,
             self._thumbs_dir, self._on_done, self._workers,
             recursive=recursive, metadata_cache=self._metadata_cache)
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


def enqueue_nightly_scan(queue, pictures_dir):
  """Queue one 'scan_dir' job per top-level directory (ticket 076), instead of running one big
  scan synchronously right now, plus one 'prune_jobs' job (ticket 075's decision: pruning old
  completed jobs rides along with the nightly scan rather than having its own schedule) and one
  'purge_trash' job (ticket 081's decision: same reasoning). Dedup (JobQueue.enqueue) means a
  directory (or either of the housekeeping jobs) whose job from a previous night is still
  queued/running just keeps that job rather than getting a duplicate. Returns how many jobs are
  queued in total (including ones already queued from before)."""
  n = 0
  for rel_dir, _recursive in top_level_steps(pictures_dir):
    queue.enqueue("scan_dir", target=rel_dir)
    n += 1
  queue.enqueue("prune_jobs")
  queue.enqueue("purge_trash")
  return n + 2


class NightlyScan:
  """Every day at a given local hour, queues one background scan job per top-level directory
  (enqueue_nightly_scan) instead of running one big scan synchronously right then (ticket 076) --
  queue's own load-adaptive worker (see load_worker.py) drains them over time, low-priority, only
  while the machine looks idle, so a rescan never forces itself onto a busy machine at 3am and can
  spill into the following day if it needs to.
  """

  def __init__(self, queue, pictures_dir, hour, wait=None):
    self._queue = queue
    self._pictures_dir = pictures_dir
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
      n = enqueue_nightly_scan(self._queue, self._pictures_dir)
      self.runs += 1
      logging.info("nightly scan: queued %d directory scan job(s)", n)
