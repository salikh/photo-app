"""Running ffmpeg / ffprobe (ticket 186): the only module that starts either of them.

Arguments are always a list (never a shell string). Every run has a hard timeout and kills the whole
process group when it expires. A missing ffmpeg is a normal, reported state (`Tools.available()` is
False), not an error: videos are still indexed and get placeholder thumbnails.
"""

import datetime
import json
import os
import re
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


_default_tools = Tools()


def configure(tools):
  """Set the tools the scanner uses (called once at startup from the settings)."""
  global _default_tools
  _default_tools = tools


def default_tools():
  return _default_tools


# VID_20180605_171430.mp4, 20180605_171430.mov, PXL_20210102_030405123.mp4, "2018-06-05 17.14.30"
_FILENAME_TIME_RE = re.compile(
    r"(?<!\d)((?:19|20)\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])[-_. T]?"
    r"([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d)")


def date_from_filename(name):
  """"YYYY-MM-DD HH:MM:SS" from a camera-style timestamp in the file name, or None."""
  m = _FILENAME_TIME_RE.search(os.path.basename(name))
  if not m:
    return None
  y, mo, d, h, mi, s = m.groups()
  try:
    datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
  except ValueError:
    return None
  return f"{y}-{mo}-{d} {h}:{mi}:{s}"


def _container_date(tags):
  """ISO creation_time tag ("2018-06-05T17:14:30.000000Z") -> "YYYY-MM-DD HH:MM:SS+0000"."""
  value = (tags or {}).get("creation_time")
  if not value:
    return None
  try:
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError:
    return None
  if parsed.year < 1990:      # an unset clock ("1970-01-01", "1904-01-01")
    return None
  offset = parsed.utcoffset() or datetime.timedelta(0)
  minutes = int(offset.total_seconds() // 60)
  sign = "+" if minutes >= 0 else "-"
  return (parsed.strftime("%Y-%m-%d %H:%M:%S")
          + f"{sign}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}")


def _rotation(stream):
  """Display rotation in degrees (0/90/180/270) from the stream's side data or tags."""
  for side in stream.get("side_data_list") or []:
    if "rotation" in side:
      try:
        return int(round(float(side["rotation"]))) % 360
      except (TypeError, ValueError):
        pass
  try:
    return int(float((stream.get("tags") or {}).get("rotate", 0))) % 360
  except (TypeError, ValueError):
    return 0


def _rate(text):
  try:
    num, _, den = (text or "").partition("/")
    value = float(num) / float(den or 1)
  except (ValueError, ZeroDivisionError):
    return None
  return round(value, 3) if value > 0 else None


def parse_probe(info, name):
  """Metadata dict from ffprobe's JSON: width, height (rotation applied), duration, fps,
  video_codec, exif_date (container creation time, else the file name's timestamp, else None --
  the library's date sort then falls back to the file's mtime). Missing pieces are None."""
  stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
  fmt = info.get("format") or {}
  width, height = stream.get("width"), stream.get("height")
  if _rotation(stream) in (90, 270):
    width, height = height, width
  try:
    duration = float(fmt.get("duration") or stream.get("duration"))
  except (TypeError, ValueError):
    duration = None
  exif_date = (_container_date(fmt.get("tags")) or _container_date(stream.get("tags"))
               or date_from_filename(name))
  return {"width": width, "height": height, "duration": duration,
          "fps": _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate")),
          "video_codec": stream.get("codec_name"), "exif_date": exif_date}


def read_info(filepath, tools=None):
  """parse_probe of a file on disk; None when ffprobe is unavailable or fails (logged)."""
  tools = tools or _default_tools
  if not tools.available():
    return None
  try:
    return parse_probe(tools.probe(filepath), filepath)
  except VideoError as e:
    logging.warning("could not probe video %s: %s", filepath, e)
    return None
