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

  def __init__(self, db_path, handlers, workers=2, low_priority=False):
    self._db_path = db_path
    self._handlers = handlers
    self._workers = workers
    self._low_priority = low_priority
    self._threads = []
    self._wake = threading.Event()
    self._stop = threading.Event()
    self._conn_lock = threading.Lock()
    self._conn = db.connect(db_path, busy_timeout=60.0)

  # -- enqueue / inspect ----------------------------------------------------

  def enqueue(self, kind, file_id):
    """Queue a job unless the same kind+file is already queued or running."""
    with self._conn_lock:
      row = self._conn.execute(
          "SELECT id FROM jobs WHERE kind = ? AND file_id = ? AND "
          "state IN ('queued', 'running')", (kind, file_id)).fetchone()
      if row:
        return row["id"]
      cur = self._conn.execute(
          "INSERT INTO jobs (kind, file_id, state, created_at) "
          "VALUES (?, ?, 'queued', ?)", (kind, file_id, _now()))
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

  # -- workers ----------------------------------------------------------------

  def start(self):
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
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        f"SELECT * FROM jobs WHERE state = 'queued' AND kind IN "
        f"({placeholders}) ORDER BY id LIMIT 1", kinds).fetchone()
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
