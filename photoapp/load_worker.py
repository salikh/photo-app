"""Starts/stops a low-priority job queue automatically based on system load (ticket 073).

Polls the 1-minute load average and the percentage of RAM available on an interval, and calls
queue.start() when the system is idle enough, queue.stop() when it no longer is. The queue itself
is expected to already run its workers at low priority (jobs.JobQueue(low_priority=True), see
_set_low_priority) -- this module only supervises *when* that queue's workers run, so background
work (today: thumb_populate.Populator, the same one populate_thumbs.py drives by hand) happens on
its own whenever the machine is idle, without ever competing with interactive use.

Two thresholds (not one) for each signal avoid flapping right at a boundary: the queue starts only
once load/memory are comfortably idle (start_load, start_mem_percent) and stops as soon as either
crosses the looser stop threshold (stop_load, stop_mem_percent).

Good memory thresholds are hard to pick from first principles (a legitimate process -- a browser,
an editor -- can use a lot of RAM while the machine is still perfectly idle otherwise), so every
sample is logged at vlog(3) regardless of the decision made from it: the intent is to tune the
thresholds later by correlating these lines with the start/stop ones (also logged, at info level).
"""

import os
import threading

from absl import logging

MEMINFO_PATH = "/proc/meminfo"


def read_meminfo():
  """{'MemTotal': kB, 'MemAvailable': kB, ...} from /proc/meminfo (Linux only)."""
  info = {}
  with open(MEMINFO_PATH) as f:
    for line in f:
      key, _, rest = line.partition(":")
      info[key] = int(rest.strip().split()[0])
  return info


def default_sample():
  """(load1, mem_available_percent): 1-minute load average, % of RAM available."""
  load1, _, _ = os.getloadavg()
  info = read_meminfo()
  return load1, 100.0 * info["MemAvailable"] / info["MemTotal"]


class LoadAdaptiveWorker:
  """Starts `queue`'s workers when the system looks idle, stops them when it does not.

  sample: () -> (load1, mem_available_percent); defaults to default_sample().
  wait: injectable for tests, like scan.NightlyScan's.
  on_before_start: called just before queue.start() on every idle->running transition (e.g. to
  enqueue newly-discovered work); its exceptions are logged, not raised, so a transient database
  error does not kill the supervisor thread.
  """

  def __init__(self, queue, check_seconds=30, start_load=0.5, stop_load=1.5,
              start_mem_percent=20.0, stop_mem_percent=10.0, sample=None, wait=None,
              on_before_start=None):
    self._queue = queue
    self._check_seconds = check_seconds
    self._start_load = start_load
    self._stop_load = stop_load
    self._start_mem_percent = start_mem_percent
    self._stop_mem_percent = stop_mem_percent
    self._sample = sample or default_sample
    self._on_before_start = on_before_start
    self._stop_event = threading.Event()
    self._wait = wait or self._stop_event.wait
    self._thread = None
    self.running = False   # whether queue's workers are currently started

  def start(self):
    self._thread = threading.Thread(target=self._loop, name="load-worker", daemon=True)
    self._thread.start()

  def stop(self):
    # Deliberately does not join self._thread: like scan.NightlyScan, stop() can be called from
    # within the worker's own thread (e.g. an injected `wait` in a test); a thread cannot join
    # itself. The caller joins self._thread separately if it needs to wait for a clean exit.
    self._stop_event.set()
    if self.running:
      self._queue.stop()
      self.running = False

  def _loop(self):
    while not self._stop_event.is_set():
      self._tick()
      self._wait(self._check_seconds)

  def _tick(self):
    load1, mem_percent = self._sample()
    logging.vlog(3, "load_worker: load1=%.2f mem_available=%.1f%% (worker %s)",
                 load1, mem_percent, "running" if self.running else "stopped")
    if not self.running and load1 <= self._start_load and mem_percent >= self._start_mem_percent:
      logging.info(
          "load_worker: system idle (load1=%.2f mem_available=%.1f%%), starting the "
          "background worker", load1, mem_percent)
      if self._on_before_start:
        try:
          self._on_before_start()
        except Exception:
          logging.exception("load_worker: on_before_start failed, starting anyway")
      self._queue.start()
      self.running = True
    elif self.running and (load1 > self._stop_load or mem_percent < self._stop_mem_percent):
      logging.info(
          "load_worker: system busy (load1=%.2f mem_available=%.1f%%), stopping the "
          "background worker", load1, mem_percent)
      self._queue.stop()
      self.running = False
