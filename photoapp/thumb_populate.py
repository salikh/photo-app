"""Background population of /zoo/Thumbs (ticket 066): low priority, one file at a time.

Fills in the Thumb/Small/Medium/Huge caches for every file that is missing one, so opening a
photo the first time does not have to wait for it to render. Runs behind the existing
jobs.JobQueue, just with its own low-priority, single-worker queue, so it never competes for
CPU or disk I/O with normal use of the app, a running scan, or the on-demand render queue
(api.py's "raw_render", used when a RAW has no usable embedded preview at all).

Rendering (ticket 090's resolution to ticket 085): the same thumbs.ensure() the on-demand path
uses for every file, RAW or not -- this used to shell out to the external `dcraw` binary for RAW
files specifically (matching the original plan's wording, plan.md 4.5), a second RAW renderer
alongside the on-demand path's rawpy/LibRaw one (ticket 028). That split was retired once a
photo's thumbnails needed to look identical regardless of which path rendered them first (ticket
085's per-file settings): thumbs.ensure() already knows, from a file's settings alone, whether to
take the fast embedded-preview shortcut or actually demosaic, at every size -- so populate_file()
no longer needs its own RAW-specific logic at all. See docs/design/thumbnails.md.
"""

from absl import logging

from photoapp import jobs
from photoapp import paths
from photoapp import thumbs


def populate_file(conn, pictures_dir, thumbs_dir, file_id, rel_path, sizes=thumbs.SIZES):
  """Generate every size in `sizes` that rel_path is still missing, via thumbs.ensure() -- the
  same renderer and per-file settings (ticket 085) the on-demand path uses, so a photo's
  thumbnails look the same whether this background populator or a live request made them first.
  Returns the sizes made.

  Never touches a size that already exists (thumbs.lookup), so this is safe
  to run repeatedly and never overwrites anything a person or another job
  already produced.
  """
  missing = [s for s in sizes if not thumbs.lookup(thumbs_dir, s, rel_path)]
  if not missing:
    logging.vlog(7, "%s: nothing missing, skipping", rel_path)
    return []
  made = []
  # thumbs.ensure() -> make() already logs at vlog(5)/vlog(7) per size.
  for size in missing:
    out = thumbs.ensure(conn, pictures_dir, thumbs_dir, file_id, rel_path, size)
    if out:
      made.append(size)
  conn.commit()
  return made


def find_missing_files(conn, limit=None, sizes=thumbs.SIZES, rel_dir=None):
  """[(id, path)] of live files that lack at least one of `sizes`.

  rel_dir (ticket 080): only files directly in that directory (not its subdirectories, matching
  what a folder's grid actually shows) -- used to bump a just-opened folder's missing thumbnails
  ahead of the standing backlog.
  """
  clauses = " OR ".join(
      "NOT EXISTS (SELECT 1 FROM thumbs t WHERE t.file_id = f.id AND t.size = ?)"
      for _ in sizes)
  q = f"SELECT f.id, f.path FROM files f WHERE f.missing = 0 AND ({clauses})"
  args = list(sizes)
  if rel_dir is not None:
    lo, hi = paths.subtree_range(rel_dir)
    q += f" AND f.path >= ? AND f.path < ? AND {paths.direct_children_sql('f.path')}"
    args += [lo, hi, len(lo) + 1]
  q += " ORDER BY f.path"
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

  def __init__(self, db_path, pictures_dir, thumbs_dir, sizes=thumbs.SIZES, queue=None):
    """queue: share an existing low-priority JobQueue (ticket 076: e.g. one also running scan_dir
    jobs) instead of building a private one -- registers KIND as an extra handler on it."""
    self.pictures_dir = pictures_dir
    self.thumbs_dir = thumbs_dir
    self.sizes = tuple(sizes)
    if queue is not None:
      queue.add_handler(self.KIND, self._handle)
      self.queue = queue
    else:
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

  def enqueue_missing(self, conn, limit=None, rel_dir=None):
    """Queue every file that still needs one of self.sizes, or (ticket 080) only those directly
    in rel_dir, to bump a just-opened folder's missing thumbnails ahead of the standing backlog
    (see jobs.JobQueue's newest_first). Returns how many were found."""
    rows = find_missing_files(conn, limit, self.sizes, rel_dir)
    if rel_dir is None and logging.vlog_is_on(1):
      logging.vlog(1, "enqueue_missing: %d files to queue, missing per size: %s",
                   len(rows), _missing_counts_by_size(conn, self.sizes))
    elif rows:
      logging.vlog(3, "enqueue_missing(%s): %d file(s) bumped to the front of the queue",
                   rel_dir, len(rows))
    for r in rows:
      self.queue.enqueue(self.KIND, r["id"])
    return len(rows)
