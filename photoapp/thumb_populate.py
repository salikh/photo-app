"""Background population of /zoo/Thumbs (ticket 066): low priority, one file at a time.

Fills in the Thumb/Small/Medium/Huge caches for every file that is missing one, so opening a
photo the first time does not have to wait for it to render. Runs behind the existing
jobs.JobQueue, just with its own low-priority, single-worker queue, so it never competes for
CPU or disk I/O with normal use of the app, a running scan, or the on-demand render queue
(api.py's "raw_render", used when a RAW has no usable embedded preview at all).

Default rendering, as asked (ticket 066) -- this deliberately uses the external `dcraw` binary,
not the rawpy/LibRaw path the on-demand renderer uses (ticket 028); it matches the original
plan's wording (plan.md 4.5: "dcraw -c -h -w, piped into Pillow") and needs no Python RAW
library:

  Thumb           `dcraw -e -c FILE`: the camera's own embedded JPEG preview, straight to
                  stdout. Fast (well under a second) and plenty for a 300px thumbnail.
  Small / Medium  `dcraw -c -h -w FILE`: half-size demosaic (-h), camera white balance (-w),
                  PPM to stdout. One decode covers both sizes.
  Huge            `dcraw -c -w -q 3 FILE`: full-size, high-quality (AHD, -q 3) demosaic, PPM to
                  stdout -- this *is* Huge's "full size of the source" definition, so no resize.

Non-RAW files (JPEG, PNG, ...) are rendered with Pillow directly (thumbs.render), same as
on-demand generation; dcraw is only used for RAW files.
"""

import io
import os
import shutil
import subprocess

from absl import logging
from PIL import Image

from photoapp import fileinfo
from photoapp import jobs
from photoapp import thumbs

DCRAW_TIMEOUT = 180


class DcrawError(Exception):
  """dcraw is missing, timed out, or refused the file."""


def dcraw_path():
  return shutil.which("dcraw")


def dcraw_available():
  return dcraw_path() is not None


def _run_dcraw(args, timeout=DCRAW_TIMEOUT):
  exe = dcraw_path()
  if not exe:
    raise DcrawError(
        "dcraw is not installed (see docs/operations.md 'Populating thumbnails')")
  logging.vlog(7, "dcraw %s", " ".join(args))
  try:
    r = subprocess.run([exe, *args], capture_output=True, timeout=timeout)
  except subprocess.TimeoutExpired:
    raise DcrawError(f"dcraw timed out after {timeout}s: {' '.join(args)}")
  except OSError as e:
    raise DcrawError(f"could not run dcraw: {e}")
  if r.returncode != 0 or not r.stdout:
    raise DcrawError(
        f"dcraw failed (exit {r.returncode}) on {args[-1]}: "
        f"{r.stderr.decode('utf-8', 'replace').strip()[:300]}")
  logging.vlog(7, "dcraw %s -> %d bytes", args[-1], len(r.stdout))
  return r.stdout


def extract_embedded_thumb(source):
  """The DNG's embedded JPEG preview (`dcraw -e -c`), as an RGB PIL image."""
  data = _run_dcraw(["-e", "-c", source])
  return Image.open(io.BytesIO(data)).convert("RGB")


def render_dcraw(source, half_size):
  """Full demosaic of source via dcraw, as an RGB PIL image.

  half_size: -h (fast, about half the linear resolution) for Small/Medium;
  False: full size, higher quality (-q 3) for Huge.
  """
  args = ["-c", "-w"]
  args += ["-h"] if half_size else ["-q", "3"]
  data = _run_dcraw(args + [source])
  return Image.open(io.BytesIO(data)).convert("RGB")


def populate_file(conn, pictures_dir, thumbs_dir, file_id, rel_path, sizes=thumbs.SIZES):
  """Generate every size in `sizes` that rel_path is still missing. Returns the
  sizes made.

  Never touches a size that already exists (thumbs.lookup), so this is safe
  to run repeatedly and never overwrites anything a person or another job
  already produced.
  """
  missing = [s for s in sizes if not thumbs.lookup(thumbs_dir, s, rel_path)]
  if not missing:
    logging.vlog(7, "%s: nothing missing, skipping", rel_path)
    return []
  abs_path = os.path.join(pictures_dir, rel_path)
  made = []

  def emit(img, size, source):
    dest = thumbs.thumb_path(thumbs_dir, size, rel_path)
    thumbs.save(img, dest, thumbs.LONG_EDGE[size])
    thumbs.record(conn, file_id, size, dest, source)
    made.append(size)
    logging.vlog(5, "%s: wrote %s (%s)", rel_path, size, source)

  if not fileinfo.is_raw(rel_path):
    # thumbs.ensure() -> make() already logs at vlog(5)/vlog(7) per size.
    for size in missing:
      out = thumbs.ensure(conn, pictures_dir, thumbs_dir, file_id, rel_path, size)
      if out:
        made.append(size)
    conn.commit()
    return made

  if "Thumb" in missing:
    try:
      emit(extract_embedded_thumb(abs_path), "Thumb", "dcraw-embedded")
      missing = [s for s in missing if s != "Thumb"]
    except DcrawError as e:
      logging.vlog(3, "%s: embedded preview failed (%s), falling back to a half-size render",
                   rel_path, e)
      # left in `missing`: the half-size pass below also covers Thumb.

  half_needed = [s for s in ("Thumb", "Small", "Medium") if s in missing]
  if half_needed:
    img = render_dcraw(abs_path, half_size=True)
    for size in half_needed:
      emit(img, size, "dcraw-half")

  if "Huge" in missing:
    emit(render_dcraw(abs_path, half_size=False), "Huge", "dcraw-full")

  conn.commit()
  return made


def find_missing_files(conn, limit=None, sizes=thumbs.SIZES):
  """[(id, path)] of live files that lack at least one of `sizes`."""
  clauses = " OR ".join(
      "NOT EXISTS (SELECT 1 FROM thumbs t WHERE t.file_id = f.id AND t.size = ?)"
      for _ in sizes)
  q = f"SELECT f.id, f.path FROM files f WHERE f.missing = 0 AND ({clauses}) ORDER BY f.path"
  args = list(sizes)
  if limit is not None:
    q += " LIMIT ?"
    args.append(limit)
  return conn.execute(q, args).fetchall()


def _missing_counts_by_size(conn, sizes):
  """{size: count of live files missing that size} -- for vlog(1) stats only."""
  return {
      size: conn.execute(
          "SELECT COUNT(*) c FROM files f WHERE f.missing = 0 AND NOT EXISTS "
          "(SELECT 1 FROM thumbs t WHERE t.file_id = f.id AND t.size = ?)",
          (size,)).fetchone()["c"]
      for size in sizes
  }


class Populator:
  """Feeds a low-priority, single-worker JobQueue with one job per file that
  still needs a thumbnail. At most one file is ever being processed at a
  time (workers=1); other queues (the on-demand one) and the rest of the
  app run at normal priority throughout.
  """

  KIND = "populate_thumb"

  def __init__(self, db_path, pictures_dir, thumbs_dir, sizes=thumbs.SIZES):
    self.pictures_dir = pictures_dir
    self.thumbs_dir = thumbs_dir
    self.sizes = tuple(sizes)
    self.queue = jobs.JobQueue(db_path, {self.KIND: self._handle}, workers=1,
                              low_priority=True)

  def _handle(self, conn, job):
    row = conn.execute("SELECT path FROM files WHERE id = ? AND missing = 0",
                       (job["file_id"],)).fetchone()
    if row is None:
      raise RuntimeError("file is gone or missing")
    made = populate_file(conn, self.pictures_dir, self.thumbs_dir,
                         job["file_id"], row["path"], self.sizes)
    if not made:
      raise RuntimeError("could not render any size for this file")
    logging.vlog(5, "%s: done (%s)", row["path"], ", ".join(made))

  def start(self):
    self.queue.start()

  def stop(self):
    self.queue.stop()

  def enqueue_missing(self, conn, limit=None):
    """Queue every file that still needs one of self.sizes. Returns how many."""
    rows = find_missing_files(conn, limit, self.sizes)
    if logging.vlog_is_on(1):
      logging.vlog(1, "enqueue_missing: %d files to queue, missing per size: %s",
                   len(rows), _missing_counts_by_size(conn, self.sizes))
    for r in rows:
      self.queue.enqueue(self.KIND, r["id"])
    return len(rows)
