"""Ticket 186: the ffmpeg/ffprobe wrapper."""
import time

import pytest

from photoapp import config
from photoapp import video

needs_ffmpeg = pytest.mark.skipif(not video.Tools().available(), reason="ffmpeg not installed")


def make_clip(tools, path, seconds=3):
  tools.ffmpeg_run(["-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10",
                    "-pix_fmt", "yuv420p", "-y", path])


def test_missing_binary_is_reported_not_raised_at_startup(caplog):
  t = video.Tools("no-such-ffmpeg-xyz", "no-such-ffprobe-xyz")
  assert not t.available()
  t.warn_once_if_missing()
  with pytest.raises(video.VideoError, match="not found"):
    t.probe("x.mp4")


def test_nonzero_exit_carries_stderr(tmp_path):
  t = video.Tools()
  with pytest.raises(video.VideoError, match="exited"):
    t.run("sh", ["-c", "echo boom >&2; exit 3"], nice=False)


def test_timeout_kills_the_process_group():
  t = video.Tools()
  start = time.time()
  with pytest.raises(video.VideoError, match="timed out"):
    t.run("sh", ["-c", "sleep 30 & sleep 30"], timeout=1, nice=False)
  assert time.time() - start < 10


def test_settings_carry_the_paths():
  s = config.Settings("p", "t", "s", ffmpeg_path="/x/ffmpeg", ffprobe_path="/x/ffprobe")
  t = video.tools_from(s)
  assert (t.ffmpeg, t.ffprobe) == ("/x/ffmpeg", "/x/ffprobe")
  assert video.tools_from(config.Settings("p", "t", "s")).ffmpeg == "ffmpeg"


@needs_ffmpeg
def test_probe_a_generated_clip(tmp_path):
  t = video.Tools()
  clip = str(tmp_path / "c.mp4")
  make_clip(t, clip)
  info = t.probe(clip)
  stream = next(s for s in info["streams"] if s["codec_type"] == "video")
  assert (stream["width"], stream["height"]) == (160, 120)
  assert 2.5 < float(info["format"]["duration"]) < 3.5
