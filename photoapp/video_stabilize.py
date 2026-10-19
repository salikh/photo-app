"""Video stabilization with ffmpeg's vidstab filters (ticket 248, epic 246).

Two passes: `vidstabdetect` analyses the camera motion into a transforms file, then
`vidstabtransform` smooths it and the result is re-encoded as H.264 + AAC in an MP4 that plays on
Android, iOS and in web browsers. Everything tunable lives in the tables below: `LEVELS` are the
named presets the UI offers (`weak`, `strong`); any single parameter of `PARAMS` can be overridden
per run through the options dict ({"level": "strong", "smoothing": 45}), which is how the options
modal can grow explicit fields later without touching this module's logic.
"""

import os
import re
import tempfile

from photoapp import video

# name -> (stage, type, min, max). The stage says which ffmpeg filter takes it; "encode" values are
# consumed by this module. Ranges follow the libvidstab / ffmpeg documentation.
PARAMS = {
    # vidstabdetect
    "shakiness": ("detect", int, 1, 10),       # how shaky the footage is (strength of the search)
    "accuracy": ("detect", int, 1, 15),        # detection accuracy
    "stepsize": ("detect", int, 1, 32),        # search step in pixels
    "mincontrast": ("detect", float, 0.0, 1.0),
    # vidstabtransform
    "smoothing": ("transform", int, 0, 500),   # frames either side averaged: the "amplitude"
    "maxshift": ("transform", int, -1, 4096),  # -1 = unlimited
    "maxangle": ("transform", float, -1.0, 6.3),
    "zoom": ("transform", float, -100.0, 100.0),
    "optzoom": ("transform", int, 0, 2),       # 0 off, 1 one static zoom, 2 adaptive
    "zoomspeed": ("transform", float, 0.0, 5.0),
    "crop": ("transform", str, ("keep", "black")),
    "interpol": ("transform", str, ("no", "linear", "bilinear", "bicubic")),
    "relative": ("transform", int, 0, 1),
    "tripod": ("transform", int, 0, 1),
    # encoding
    "crf": ("encode", int, 14, 35),
    "preset": ("encode", str, ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                               "slow", "slower", "veryslow")),
    "sharpen": ("encode", float, 0.0, 2.0),    # unsharp amount, vidstab blurs a little; 0 = off
}

# What every level starts from. crf 23 / medium is visually close to a camera original while
# usually smaller; interpol=bicubic and optzoom=1 (one static zoom that hides the borders) are the
# vidstab defaults, spelled out so a change is a one-line edit.
BASE = {"shakiness": 6, "accuracy": 15, "smoothing": 15, "optzoom": 1, "zoom": 0, "crop": "keep",
        "interpol": "bicubic", "crf": 23, "preset": "medium", "sharpen": 0.8}

LEVELS = {
    "weak": {"shakiness": 5, "smoothing": 10},
    "strong": {"shakiness": 8, "smoothing": 30},
}
DEFAULT_LEVEL = "strong"


class StabilizeError(Exception):
  """Bad options, or the stabilization run failed."""


def levels():
  return list(LEVELS)


def resolve(options):
  """The complete parameter dict for `options` ({"level": ..., <param>: <value> overrides}):
  BASE, then the level's preset, then validated overrides. Raises StabilizeError."""
  options = dict(options or {})
  level = options.pop("level", None) or DEFAULT_LEVEL
  if level not in LEVELS:
    raise StabilizeError(f"unknown stabilization level {level!r} (known: {', '.join(LEVELS)})")
  params = {**BASE, **LEVELS[level]}
  for name, value in options.items():
    spec = PARAMS.get(name)
    if spec is None:
      raise StabilizeError(f"unknown stabilization parameter {name!r}")
    try:
      if spec[1] is str:
        if value not in spec[2]:
          raise ValueError(f"must be one of {', '.join(spec[2])}")
      else:
        value = spec[1](value)
        if not spec[2] <= value <= spec[3]:
          raise ValueError(f"must be between {spec[2]} and {spec[3]}")
    except (TypeError, ValueError) as e:
      raise StabilizeError(f"bad value for {name}: {value!r} ({e})") from None
    params[name] = value
  params["level"] = level
  return params


def describe(options):
  """One human-readable line for the Files pane and the MP4 comment tag."""
  p = resolve(options)
  tuned = " ".join(f"{k}={p[k]}" for k in PARAMS if PARAMS[k][0] != "encode" and k in p)
  return (f"stabilized ({p['level']}): vidstab {tuned}; H.264 crf={p['crf']} "
          f"preset={p['preset']}" + (f" sharpen={p['sharpen']}" if p["sharpen"] else "")
          + ", AAC 128k")


_LEVEL_RE = re.compile(r"[a-z0-9]+")


def output_name(src_path, level):
  """`dir/clip.MOV` -> `dir/clip.stabilized-strong.mp4` (a path in, a path out; same directory)."""
  if not _LEVEL_RE.fullmatch(level):
    raise StabilizeError(f"bad level name {level!r}")
  stem = os.path.splitext(src_path)[0]
  return f"{stem}.stabilized-{level}.mp4"


def is_stabilized_name(path):
  return re.search(r"\.stabilized-[a-z0-9]+\.mp4$", path) is not None


def available(tools):
  """True when ffmpeg is present and was built with libvidstab and libx264."""
  cached = getattr(tools, "_vidstab_ok", None)    # two ffmpeg runs; the answer cannot change
  if cached is None:
    cached = tools._vidstab_ok = _probe_available(tools)
  return cached


def _probe_available(tools):
  if not tools.available():
    return False
  try:
    filters = tools.run(tools.ffmpeg, ["-hide_banner", "-filters"], timeout=30, nice=False)
    encoders = tools.run(tools.ffmpeg, ["-hide_banner", "-encoders"], timeout=30, nice=False)
  except video.VideoError:
    return False
  filters, encoders = filters.decode("utf-8", "replace"), encoders.decode("utf-8", "replace")
  return all(name in filters for name in ("vidstabdetect", "vidstabtransform")) and "libx264" in encoders


def _filter_arg(value):
  """Escape a path or value for use inside an ffmpeg filter option."""
  return re.sub(r"([\\:'\[\],;])", r"\\\1", str(value))


def _options(p, stage, **fixed):
  parts = [f"{k}={_filter_arg(v)}" for k, v in fixed.items()]
  parts += [f"{k}={_filter_arg(p[k])}" for k, spec in PARAMS.items()
            if spec[0] == stage and k in p]
  return ":".join(parts)


def commands(src, dest_part, trf, options, comment=None):
  """The two ffmpeg argument lists (detect, transform+encode) -- separate from running them so a
  test, or a curious user, can read exactly what is executed."""
  p = resolve(options)
  detect = ["-i", src, "-an", "-vf", "vidstabdetect=" + _options(p, "detect", result=trf),
            "-f", "null", "-"]
  chain = ["vidstabtransform=" + _options(p, "transform", input=trf)]
  if p["sharpen"]:
    chain.append(f"unsharp=5:5:{p['sharpen']}:3:3:{round(p['sharpen'] / 2, 2)}")
  chain += ["scale=trunc(iw/2)*2:trunc(ih/2)*2", "format=yuv420p"]     # H.264 wants even sizes
  transform = ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(chain),
               "-c:v", "libx264", "-profile:v", "high", "-crf", p["crf"], "-preset", p["preset"],
               "-c:a", "aac", "-b:a", "128k", "-map_metadata", "0",
               "-movflags", "+faststart"]
  if comment:
    transform += ["-metadata", f"comment={comment}"]
  transform += ["-f", "mp4", "-y", dest_part]
  return detect, transform


def stabilize(tools, src, dest, options, duration=None, progress=None):
  """Stabilize the video at `src` into `dest` (an .mp4 path); returns the description string.

  The output is written to `dest + ".part"` and renamed on success, so a failure or a kill never
  leaves a half-written file under the final name, and a re-run replaces the previous result
  atomically. `progress(text)` is called at the start of each pass."""
  if not available(tools):
    raise StabilizeError("ffmpeg with libvidstab and libx264 is not available")
  note = describe(options)
  part = dest + ".part"
  timeout = max(900, int(30 * (duration or 60)))
  with tempfile.TemporaryDirectory(prefix="stabilize-") as tmp:
    trf = os.path.join(tmp, "transforms.trf")
    detect, transform = commands(src, part, trf, options, comment=note)
    try:
      if progress:
        progress("pass 1/2: analysing motion")
      tools.ffmpeg_run(detect, timeout=timeout)
      if progress:
        progress("pass 2/2: stabilizing and encoding")
      tools.ffmpeg_run(transform, timeout=timeout)
      os.replace(part, dest)
    except video.VideoError as e:
      raise StabilizeError(str(e)) from e
    finally:
      if os.path.exists(part):
        os.unlink(part)
  return note
