from photoapp import load_worker


class FakeQueue:
  def __init__(self):
    self.starts = 0
    self.stops = 0

  def start(self):
    self.starts += 1

  def stop(self):
    self.stops += 1


def run_ticks(samples, **kwargs):
  """Run one _tick() per entry in samples (a list of (load1, mem_percent)); returns the worker."""
  it = iter(samples)
  w = load_worker.LoadAdaptiveWorker(FakeQueue(), sample=lambda: next(it), wait=lambda s: None,
                                     **kwargs)
  for _ in samples:
    w._tick()
  return w


def test_starts_when_idle_and_stops_when_busy():
  w = run_ticks([(0.1, 50.0),    # idle -> start
                 (2.0, 50.0)])   # busy -> stop
  assert w._queue.starts == 1 and w._queue.stops == 1
  assert w.running is False


def test_stays_stopped_while_between_thresholds():
  # 0.6 is above start_load (0.5) but below stop_load (1.5): never starts in the first place.
  w = run_ticks([(0.6, 50.0), (0.6, 50.0), (0.6, 50.0)])
  assert w._queue.starts == 0 and w._queue.stops == 0


def test_hysteresis_does_not_flap_once_running():
  # Once running, load between the two thresholds should NOT stop it (avoids flapping).
  w = run_ticks([(0.1, 50.0),   # start
                 (0.9, 50.0),   # between thresholds: stays running
                 (0.9, 50.0)])  # still running
  assert w._queue.starts == 1 and w._queue.stops == 0
  assert w.running is True


def test_low_memory_prevents_start_and_triggers_stop():
  w = run_ticks([(0.1, 5.0),     # idle CPU but low memory: does not start
                 (0.1, 50.0),    # now enough memory: starts
                 (0.1, 8.0)])    # memory drops below stop_mem_percent: stops
  assert w._queue.starts == 1 and w._queue.stops == 1
  assert w.running is False


def test_on_before_start_runs_once_per_start_transition():
  calls = []
  w = run_ticks([(0.1, 50.0), (0.1, 50.0), (2.0, 50.0), (0.1, 50.0)],
                on_before_start=lambda: calls.append(1))
  # idle, idle (already running, no-op), busy (stop), idle again (start) -> two starts total
  assert w._queue.starts == 2 and len(calls) == 2


def test_on_before_start_exception_does_not_block_start():
  def boom():
    raise RuntimeError("nope")
  w = run_ticks([(0.1, 50.0)], on_before_start=boom)
  assert w._queue.starts == 1


def test_default_sample_reads_real_system():
  load1, mem_percent = load_worker.default_sample()
  assert load1 >= 0
  assert 0 <= mem_percent <= 100


def test_thread_lifecycle_start_and_stop():
  samples = [(0.1, 50.0), (2.0, 50.0)]
  it = iter(samples)
  waits = []

  def fake_wait(seconds):
    waits.append(seconds)
    if len(waits) >= len(samples):
      w.stop()

  w = load_worker.LoadAdaptiveWorker(FakeQueue(), sample=lambda: next(it), wait=fake_wait,
                                     check_seconds=7)
  w.start()
  w._thread.join(5)
  assert w._queue.starts == 1 and w._queue.stops == 1
  assert waits == [7, 7]
