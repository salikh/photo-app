"""Running ffmpeg / ffprobe (ticket 186): the only module that starts either of them.

Arguments are always a list (never a shell string). Every run has a hard timeout and kills the whole
process group when it expires. A missing ffmpeg is a normal, reported state (`Tools.available()` is
False), not an error: videos are still indexed and get placeholder thumbnails.
"""

import json
import os
import shutil
import signal
import subprocess

from absl import logging


class VideoError(Exception):
  """ffmpeg/ffprobe failed, timed out, or is missing; the message carries the reason."""


class Tools:
  """The ffmpeg and ffprobe binaries to use. ffmpeg/ffprobe are names looked up on $PATH or paths."""

  def __init__(self, ffmpeg=None, ffprobe=None):
    self.ffmpeg = ffmpeg or "ffmpeg"
    self.ffprobe = ffprobe or "ffprobe"
    self._warned = False

  def available(self):
    return shutil.which(self.ffmpeg) is not None and shutil.which(self.ffprobe) is not None

  def warn_once_if_missing(self):
    """Log a single startup warning (not one per file) when the binaries are missing."""
    if not self._warned and not self.available():
      logging.warning(
          "ffmpeg/ffprobe not found (looked for %s, %s): videos are listed but get placeholder "
          "thumbnails and no metadata. Install ffmpeg or set ffmpeg_path/ffprobe_path.",
          self.ffmpeg, self.ffprobe)
    self._warned = True

  def run(self, binary, args, timeout=300, nice=True):
    """Run `binary` (self.ffmpeg or self.ffprobe) with args; returns stdout bytes.

    Raises VideoError when the binary is missing, exits non-zero (message = the last stderr
    lines) or runs past `timeout` seconds (its process group is killed).
    """
    exe = shutil.which(binary)
    if exe is None:
      raise VideoError(f"{binary} not found")
    cmd = [exe] + [str(a) for a in args]
    if nice:
      cmd = ["nice", "-n", "10"] + cmd
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
      out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
      try:
        os.killpg(proc.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      proc.communicate()
      raise VideoError(f"{os.path.basename(exe)} timed out after {timeout}s") from None
    if proc.returncode != 0:
      tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
      raise VideoError(f"{os.path.basename(exe)} exited {proc.returncode}: {' | '.join(tail)}")
    return out

  def ffmpeg_run(self, args, timeout=300):
    return self.run(self.ffmpeg, ["-hide_banner", "-nostdin", "-loglevel", "error"] + list(args),
                    timeout)

  def probe(self, path, timeout=60):
    """ffprobe's JSON (format + streams) for a video file."""
    out = self.run(self.ffprobe, ["-v", "error", "-print_format", "json", "-show_format",
                                  "-show_streams", path], timeout)
    try:
      return json.loads(out)
    except ValueError as e:
      raise VideoError(f"ffprobe returned unparsable output for {path}: {e}") from e


def tools_from(settings):
  return Tools(getattr(settings, "ffmpeg_path", None), getattr(settings, "ffprobe_path", None))
