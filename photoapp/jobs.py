"""Small persistent job queue on top of the jobs table.

Jobs survive a restart: anything left 'running' is put back to 'queued' on
start. Each worker thread has its own database connection. Several JobQueue
instances (different handlers/kinds, different worker counts) can share the
same jobs table -- each only ever claims jobs of the kinds it has a handler
for (see _claim), so an on-demand queue and a low-priority background queue
never steal each other's work.
"""

import datetime
import json
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

  def __init__(self, db_path, handlers, workers=2, low_priority=False, newest_first=False,
               elevated_workers=0):
    """newest_first (ticket 080): claim the most recently enqueued job first (instead of the
    usual FIFO oldest-first) -- a cheap approximation of "prioritize what's newly relevant" for a
    queue whose backlog can otherwise take a long time to reach a given job. Enqueueing something
    already queued still dedupes to its existing (older) row, so this only actually reorders
    genuinely new jobs ahead of the standing backlog, not jobs already waiting.

    elevated_workers (ticket 233): that many extra threads, at *normal* CPU/I/O priority even when
    low_priority is set, that run only jobs with priority > 0 (a user's Rescan). They are not
    controlled by start()/stop() (the load-adaptive worker toggles those with machine load):
    enqueueing an elevated job -- or start_elevated_if_pending() at startup -- starts them, and they
    keep running. With elevated_workers=0 priority only orders the claims."""
    self._db_path = db_path
    self._handlers = handlers
    self._workers = workers
    self._low_priority = low_priority
    self._newest_first = newest_first
    self._threads = []
    self._wake = threading.Event()
    self._stop = threading.Event()
    self._elevated_workers = elevated_workers
    self._elevated_threads = []
    self._elevated_wake = threading.Event()
    self._elevated_stop = threading.Event()
    self._elevated_lock = threading.Lock()
    self._conn_lock = threading.Lock()
    self._conn = db.connect(db_path, busy_timeout=60.0)

  # -- enqueue / inspect ----------------------------------------------------

  def add_handler(self, kind, fn):
    """Register a handler for an additional kind after construction (ticket 076: several kinds
    can share one low-priority queue/worker, registered as each part of the app sets itself up)."""
    self._handlers[kind] = fn

  def enqueue(self, kind, file_id=None, target=None, options=None, priority=0):
    """Queue a job unless the same kind+file_id+target is already queued or running.

    Most jobs are scoped to a file (file_id); a directory-scoped job (ticket 076's scan_dir)
    passes target instead and leaves file_id None. options (a dict, ticket 208) is stored as JSON
    and is not part of the dedupe key.

    priority (ticket 233): 0 is background work; > 0 is elevated. A duplicate of a still-queued
    job with a lower priority raises that job's priority instead of adding a row.
    """
    with self._conn_lock:
      row = self._conn.execute(
          "SELECT id, state, priority FROM jobs WHERE kind = ? AND file_id IS ? AND target IS ? "
          "AND state IN ('queued', 'running')", (kind, file_id, target)).fetchone()
      if row:
        if row["state"] == "queued" and priority > row["priority"]:
          self._conn.execute("UPDATE jobs SET priority = ? WHERE id = ?", (priority, row["id"]))
          self._conn.commit()
          job_id = row["id"]
        else:
          return row["id"]
      else:
        cur = self._conn.execute(
            "INSERT INTO jobs (kind, file_id, target, state, created_at, options, priority) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (kind, file_id, target, _now(), json.dumps(options) if options else None, priority))
        self._conn.commit()
        job_id = cur.lastrowid
    if priority > 0:
      self._start_elevated()
      self._elevated_wake.set()
    self._wake.set()
    return job_id

  def find_open(self, kind, file_id=None, target=None):
    """The id of a queued/running job of this exact kind+file_id+target, or None."""
    with self._conn_lock:
      row = self._conn.execute(
          "SELECT id FROM jobs WHERE kind = ? AND file_id IS ? AND target IS ? AND "
          "state IN ('queued', 'running')", (kind, file_id, target)).fetchone()
    return row["id"] if row else None

  def list(self, limit=100):
    """Every kind in the shared table (like active()/progress()), each row's file_id resolved to
    its path/photo_id (ticket 142, for the Jobs page's File column to link straight to the photo)
    -- both NULL for a target-scoped job (e.g. scan_dir) or a file_id whose row is gone."""
    with self._conn_lock:
      return [dict(r) for r in self._conn.execute(
          "SELECT j.id, j.kind, j.file_id, j.target, j.state, j.error, j.note, j.created_at,"
          " j.started_at, j.finished_at, j.priority,"
          " (j.kind = 'gphotos_upload' AND j.state = 'done' AND"
          "  COALESCE(json_array_length(j.options, '$.failed'), 0) > 0) AS retryable,"
          " f.path AS path, f.photo_id AS photo_id FROM jobs j"
          " LEFT JOIN files f ON f.id = j.file_id ORDER BY j.id DESC LIMIT ?", (limit,))]

  def counts(self):
    with self._conn_lock:
      return {r["state"]: r["n"] for r in self._conn.execute(
          "SELECT state, COUNT(*) n FROM jobs GROUP BY state")}

  # Time buckets for progress(); (label, seconds) pairs, ticket 074.
  _COMPLETED_WINDOWS = (("last_minute", 60), ("last_hour", 3600), ("last_day", 86400))

  def active(self):
    """What the workers are busy with right now (ticket 113), across every kind in the shared
    table: {'busy': bool, 'running': [{id, kind, file_id, target, path, created_at, started_at,
    duration_seconds}]}. duration_seconds is measured from started_at (created_at for a row that
    predates that column), or None if neither parses."""
    now = datetime.datetime.now()
    with self._conn_lock:
      rows = list(self._conn.execute(
          "SELECT j.id, j.kind, j.file_id, j.target, j.created_at, j.started_at,"
          " f.path AS file_path FROM jobs j LEFT JOIN files f ON f.id = j.file_id"
          " WHERE j.state = 'running' ORDER BY j.id"))
    running = []
    for r in rows:
      started = r["started_at"] or r["created_at"]
      duration = None
      if started:
        try:
          duration = max(0.0, (now - datetime.datetime.fromisoformat(started)).total_seconds())
        except ValueError:
          duration = None
      running.append({
          "id": r["id"], "kind": r["kind"], "file_id": r["file_id"],
          "target": r["target"], "path": r["file_path"],
          "created_at": r["created_at"], "started_at": r["started_at"],
          "duration_seconds": round(duration, 1) if duration is not None else None})
    return {"busy": bool(running), "running": running}

  def progress(self):
    """A summary for the Jobs page (ticket 074): total, incomplete, a per-kind breakdown of
    every state, and how many done/failed jobs finished in the last minute/hour/day. Spans every
    kind in the table, like counts(), not just this queue's own kinds.

    Ticket 114: each `by_kind` entry also carries the per-window done/failed counts the Jobs table
    shows as its "last day/hour/minute" columns (keys like `done_last_day`), so one request drives
    the whole table; the top-level `completed` buckets stay for the overall summary."""
    def empty_kind():
      return {**{state: 0 for state in ("queued", "running", "done", "failed")},
              **{f"{state}_{label}": 0
                 for label, _ in self._COMPLETED_WINDOWS
                 for state in ("done", "failed")}}
    with self._conn_lock:
      by_state = {r["state"]: r["n"] for r in self._conn.execute(
          "SELECT state, COUNT(*) n FROM jobs GROUP BY state")}
      by_kind = {}
      for r in self._conn.execute("SELECT kind, state, COUNT(*) n FROM jobs GROUP BY kind, state"):
        by_kind.setdefault(r["kind"], empty_kind())[r["state"]] = r["n"]
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
        for r in self._conn.execute(
            "SELECT kind, state, COUNT(*) n FROM jobs WHERE finished_at >= ? AND "
            "state IN ('done', 'failed') GROUP BY kind, state", (cutoff,)):
          by_kind.setdefault(r["kind"], empty_kind())[f"{r['state']}_{label}"] = r["n"]
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
      # Only background (priority 0) jobs: start() runs on every idle transition of the
      # load-adaptive worker, and must not touch an elevated job the elevated worker is running.
      self._conn.execute("UPDATE jobs SET state = 'queued', started_at = NULL "
                         "WHERE state = 'running' AND priority = 0")
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

  def _start_elevated(self):
    """Start the elevated worker thread(s) if this queue has any and they are not running."""
    if not self._elevated_workers:
      return
    with self._elevated_lock:
      self._elevated_threads = [t for t in self._elevated_threads if t.is_alive()]
      if self._elevated_threads:
        return
      self._elevated_stop.clear()
      for i in range(self._elevated_workers):
        t = threading.Thread(target=self._run, args=(True,), name=f"job-elevated-{i}",
                             daemon=True)
        t.start()
        self._elevated_threads.append(t)
    self._elevated_wake.set()

  def start_elevated_if_pending(self):
    """At startup: start the elevated worker if an elevated job of this queue's kinds is queued."""
    kinds = tuple(self._handlers)
    placeholders = ",".join("?" * len(kinds))
    with self._conn_lock:
      n = self._conn.execute(
          f"SELECT COUNT(*) FROM jobs WHERE state = 'queued' AND priority > 0 AND "
          f"kind IN ({placeholders})", kinds).fetchone()[0]
    if n:
      self._start_elevated()

  def fail_interrupted_elevated(self):
    """At startup: an elevated job still 'running' was cut off by the restart. Mark it failed
    rather than queueing it again (ticket 233) -- a scan that takes the process down must not
    crash-loop it; the user can simply ask again. Returns how many were marked."""
    with self._conn_lock:
      cur = self._conn.execute(
          "UPDATE jobs SET state = 'failed', error = 'interrupted by restart', "
          "finished_at = ? WHERE state = 'running' AND priority > 0", (_now(),))
      self._conn.commit()
    return cur.rowcount

  def stop_elevated(self):
    self._elevated_stop.set()
    self._elevated_wake.set()
    for t in self._elevated_threads:
      t.join(timeout=10)
    self._elevated_threads = []

  def _claim(self, conn, elevated=False):
    # Scoped to this queue's own handler kinds, so a second JobQueue sharing
    # the same jobs table (a different kind, e.g. a low-priority bulk queue
    # next to the on-demand one) never claims -- and then fails with an
    # unknown-kind error -- a job it has no handler for.
    kinds = tuple(self._handlers)
    placeholders = ",".join("?" * len(kinds))
    order = "priority DESC, " + ("id DESC" if self._newest_first else "id")
    if elevated:
      scope = "AND priority > 0"
    elif self._elevated_workers:
      scope = "AND priority = 0"      # elevated jobs belong to the elevated worker (ticket 233)
    else:
      scope = ""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        f"SELECT * FROM jobs WHERE state = 'queued' {scope} AND kind IN "
        f"({placeholders}) ORDER BY {order} LIMIT 1", kinds).fetchone()
    if row:
      conn.execute("UPDATE jobs SET state = 'running', started_at = ? WHERE id = ?",
                   (_now(), row["id"]))
    conn.commit()
    return row

  def _run(self, elevated=False):
    if self._low_priority and not elevated:
      _set_low_priority()
    stop, wake = ((self._elevated_stop, self._elevated_wake) if elevated
                  else (self._stop, self._wake))
    conn = db.connect(self._db_path, busy_timeout=60.0)
    try:
      while not stop.is_set():
        job = self._claim(conn, elevated)
        if job is None:
          wake.wait(timeout=1.0)
          wake.clear()
          continue
        wake.set()    # let a sibling look for more work
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

  def wait_idle(self, timeout=10.0, kinds=None):
    """Block until no job of this queue's own kinds is queued or running (for tests).

    Scoped to self._handlers like _claim(), unlike counts()/progress(), which deliberately span
    every kind in the shared table for the Jobs page's unified view. Using the unscoped counts()
    here would mean a queued-but-unclaimed job of a *different*, unstarted queue's kind (e.g. a
    populate_thumb job a request handler enqueued via ticket 080's enqueue_missing, sitting in
    app.state.background_jobs, which a test harness may never start) makes this queue's own
    wait_idle() hang forever even though this queue itself has nothing left to do."""
    import time
    deadline = time.time() + timeout
    kinds = tuple(kinds or self._handlers)
    placeholders = ",".join("?" * len(kinds))
    while time.time() < deadline:
      with self._conn_lock:
        n = self._conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE kind IN ({placeholders}) AND "
            "state IN ('queued', 'running')", kinds).fetchone()[0]
      if not n:
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
