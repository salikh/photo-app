"""Small persistent job queue on top of the jobs table.

Jobs survive a restart: anything left 'running' is put back to 'queued' on
start. Each worker thread has its own database connection. Several JobQueue
instances (different handlers/kinds, different worker counts) can share the
same jobs table -- each only ever claims jobs of the kinds it has a handler
for (see _claim), so an on-demand queue and a low-priority background queue
never steal each other's work.
"""

import datetime
import os
import subprocess
import threading

from absl import logging

from photoapp import db


def _now():
  return datetime.datetime.now().isoformat(timespec="seconds")


def _set_low_priority():
  """Best-effort: make the *calling thread* low priority for CPU and I/O.

  On Linux, setpriority()/ioprio_set() with pid 0 (or nice()) act on the
  calling thread, not the whole process (threads are separate schedulable
  tasks), so this only affects this worker's own thread -- the rest of the
  app (and any other JobQueue's workers) keeps its normal priority. Never
  raises: a platform or a missing 'ionice' just means no effect.
  """
  try:
    os.nice(19)
  except OSError:
    pass
  try:
    tid = threading.get_native_id()
    subprocess.run(["ionice", "-c3", "-p", str(tid)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=5)
  except (OSError, subprocess.SubprocessError):
    pass


class JobQueue:
  """handlers: {kind: fn(conn, job_row)}; fn raises to fail the job.

  low_priority=True lowers the CPU/I/O priority of this queue's own worker
  threads (see _set_low_priority); other queues and the rest of the app are
  unaffected.
  """

  def __init__(self, db_path, handlers, workers=2, low_priority=False, newest_first=False):
    """newest_first (ticket 080): claim the most recently enqueued job first (instead of the
    usual FIFO oldest-first) -- a cheap approximation of "prioritize what's newly relevant" for a
    queue whose backlog can otherwise take a long time to reach a given job. Enqueueing something
    already queued still dedupes to its existing (older) row, so this only actually reorders
    genuinely new jobs ahead of the standing backlog, not jobs already waiting."""
    self._db_path = db_path
    self._handlers = handlers
    self._workers = workers
    self._low_priority = low_priority
    self._newest_first = newest_first
    self._threads = []
    self._wake = threading.Event()
    self._stop = threading.Event()
    self._conn_lock = threading.Lock()
    self._conn = db.connect(db_path, busy_timeout=60.0)

  # -- enqueue / inspect ----------------------------------------------------

  def add_handler(self, kind, fn):
    """Register a handler for an additional kind after construction (ticket 076: several kinds
    can share one low-priority queue/worker, registered as each part of the app sets itself up)."""
    self._handlers[kind] = fn

  def enqueue(self, kind, file_id=None, target=None):
    """Queue a job unless the same kind+file_id+target is already queued or running.

    Most jobs are scoped to a file (file_id); a directory-scoped job (ticket 076's scan_dir)
    passes target instead and leaves file_id None.
    """
    with self._conn_lock:
      row = self._conn.execute(
          "SELECT id FROM jobs WHERE kind = ? AND file_id IS ? AND target IS ? AND "
          "state IN ('queued', 'running')", (kind, file_id, target)).fetchone()
      if row:
        return row["id"]
      cur = self._conn.execute(
          "INSERT INTO jobs (kind, file_id, target, state, created_at) "
          "VALUES (?, ?, ?, 'queued', ?)", (kind, file_id, target, _now()))
      self._conn.commit()
    self._wake.set()
    return cur.lastrowid

  def list(self, limit=100):
    with self._conn_lock:
      return [dict(r) for r in self._conn.execute(
          "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

  def counts(self):
    with self._conn_lock:
      return {r["state"]: r["n"] for r in self._conn.execute(
          "SELECT state, COUNT(*) n FROM jobs GROUP BY state")}

  # Time buckets for progress(); (label, seconds) pairs, ticket 074.
  _COMPLETED_WINDOWS = (("last_minute", 60), ("last_hour", 3600), ("last_day", 86400))

  def progress(self):
    """A summary for the Jobs page (ticket 074): total, incomplete, a per-kind breakdown of
    every state, and how many done/failed jobs finished in the last minute/hour/day. Spans every
    kind in the table, like counts(), not just this queue's own kinds."""
    with self._conn_lock:
      by_state = {r["state"]: r["n"] for r in self._conn.execute(
          "SELECT state, COUNT(*) n FROM jobs GROUP BY state")}
      by_kind = {}
      for r in self._conn.execute("SELECT kind, state, COUNT(*) n FROM jobs GROUP BY kind, state"):
        by_kind.setdefault(r["kind"], {})[r["state"]] = r["n"]
      completed = {}
      for label, seconds in self._COMPLETED_WINDOWS:
        cutoff = (datetime.datetime.now() - datetime.timedelta(seconds=seconds)).isoformat(
            timespec="seconds")
        bucket = {"done": 0, "failed": 0}
        for r in self._conn.execute(
            "SELECT state, COUNT(*) n FROM jobs WHERE finished_at >= ? AND "
            "state IN ('done', 'failed') GROUP BY state", (cutoff,)):
          bucket[r["state"]] = r["n"]
        completed[label] = bucket
    return {
        "total": sum(by_state.values()),
        "incomplete": by_state.get("queued", 0) + by_state.get("running", 0),
        "by_state": by_state,
        "by_kind": by_kind,
        "completed": completed,
    }

  # -- workers ----------------------------------------------------------------

  def start(self):
    # Cleared here (not just set in __init__) so a queue that was stop()ed can be
    # start()ed again -- ticket 073's load-adaptive worker toggles a queue repeatedly
    # over the life of one process, unlike every other caller, which starts it once.
    self._stop.clear()
    with self._conn_lock:
      self._conn.execute("UPDATE jobs SET state = 'queued' "
                         "WHERE state = 'running'")
      self._conn.commit()
    for i in range(self._workers):
      t = threading.Thread(target=self._run, name=f"job-worker-{i}",
                           daemon=True)
      t.start()
      self._threads.append(t)
    self._wake.set()

  def stop(self):
    self._stop.set()
    self._wake.set()
    for t in self._threads:
      t.join(timeout=10)
    self._threads = []

  def _claim(self, conn):
    # Scoped to this queue's own handler kinds, so a second JobQueue sharing
    # the same jobs table (a different kind, e.g. a low-priority bulk queue
    # next to the on-demand one) never claims -- and then fails with an
    # unknown-kind error -- a job it has no handler for.
    kinds = tuple(self._handlers)
    placeholders = ",".join("?" * len(kinds))
    order = "id DESC" if self._newest_first else "id"
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        f"SELECT * FROM jobs WHERE state = 'queued' AND kind IN "
        f"({placeholders}) ORDER BY {order} LIMIT 1", kinds).fetchone()
    if row:
      conn.execute("UPDATE jobs SET state = 'running' WHERE id = ?",
                   (row["id"],))
    conn.commit()
    return row

  def _run(self):
    if self._low_priority:
      _set_low_priority()
    conn = db.connect(self._db_path, busy_timeout=60.0)
    try:
      while not self._stop.is_set():
        job = self._claim(conn)
        if job is None:
          self._wake.wait(timeout=1.0)
          self._wake.clear()
          continue
        self._wake.set()    # let a sibling look for more work
        error = None
        try:
          self._handlers[job["kind"]](conn, job)
        except Exception as e:   # a failing job must not kill the worker
          logging.exception("job %d (%s) failed", job["id"], job["kind"])
          error = f"{type(e).__name__}: {e}"
        conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = ? "
            "WHERE id = ?", ("failed" if error else "done", error, _now(),
                             job["id"]))
        if not error:
          # ticket 075: a successful run makes any earlier failed attempt at the same job (same
          # kind + file_id/target) stale -- no need to keep it around until the next prune.
          cur = conn.execute(
              "DELETE FROM jobs WHERE kind = ? AND file_id IS ? AND target IS ? AND "
              "state = 'failed' AND id != ?",
              (job["kind"], job["file_id"], job["target"], job["id"]))
          if cur.rowcount:
            logging.vlog(7, "job %d (%s): cleared %d earlier failed attempt(s)",
                         job["id"], job["kind"], cur.rowcount)
        conn.commit()
    finally:
      conn.close()

  def wait_idle(self, timeout=10.0):
    """Block until no job is queued or running (for tests)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
      c = self.counts()
      if not c.get("queued") and not c.get("running"):
        return True
      time.sleep(0.05)
    return False


# Retention for prune() (ticket 075): a completed job stays failed longer than a done one, since
# a failure might still need a human to notice and investigate it.
DONE_RETENTION_DAYS = 7
FAILED_RETENTION_DAYS = 365


def prune(conn, done_days=DONE_RETENTION_DAYS, failed_days=FAILED_RETENTION_DAYS):
  """Delete 'done' jobs older than done_days and 'failed' jobs older than failed_days (by
  finished_at). Never touches 'queued'/'running'. Returns (done_deleted, failed_deleted)."""
  now = datetime.datetime.now()
  done_cutoff = (now - datetime.timedelta(days=done_days)).isoformat(timespec="seconds")
  failed_cutoff = (now - datetime.timedelta(days=failed_days)).isoformat(timespec="seconds")
  done_n = conn.execute(
      "DELETE FROM jobs WHERE state = 'done' AND finished_at < ?", (done_cutoff,)).rowcount
  failed_n = conn.execute(
      "DELETE FROM jobs WHERE state = 'failed' AND finished_at < ?", (failed_cutoff,)).rowcount
  conn.commit()
  logging.vlog(1, "prune: deleted %d done job(s) (>%dd) and %d failed job(s) (>%dd)",
              done_n, done_days, failed_n, failed_days)
  return done_n, failed_n
