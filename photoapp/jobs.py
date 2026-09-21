"""Small persistent job queue on top of the jobs table.

Jobs survive a restart: anything left 'running' is put back to 'queued' on
start. Each worker thread has its own database connection.
"""

import datetime
import threading

from absl import logging

from photoapp import db


def _now():
  return datetime.datetime.now().isoformat(timespec="seconds")


class JobQueue:
  """handlers: {kind: fn(conn, job_row)}; fn raises to fail the job."""

  def __init__(self, db_path, handlers, workers=2):
    self._db_path = db_path
    self._handlers = handlers
    self._workers = workers
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
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1"
    ).fetchone()
    if row:
      conn.execute("UPDATE jobs SET state = 'running' WHERE id = ?",
                   (row["id"],))
    conn.commit()
    return row

  def _run(self):
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
