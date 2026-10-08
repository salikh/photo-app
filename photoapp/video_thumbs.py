"""Static thumbnails of video files (ticket 188).

The same four sizes and the same tree as stills (`Thumb/NAME.mp4.jpg`...), made from one frame
taken a little way into the clip. All sizes are clean frames: the "this is a video" play indicator
is a client-side overlay (ticket 192), never drawn into the JPEG. When no frame can be had (ffmpeg
missing, undecodable file) a shared flat placeholder is served instead; it lives outside the
per-file tree and is never recorded as a thumbnail, so a later pass (with ffmpeg installed, or a
fixed file) produces the real one.
"""

import io
import os

from absl import logging
from PIL import Image
from PIL import ImageStat

from photoapp import thumbs
from photoapp import video

# Where in the clip to look for a frame, as fractions of the duration; the first frame that is not
# near-black wins (a fade-in or a dark first frame makes a useless thumbnail).
FRAME_POSITIONS = (0.10, 0.25, 0.40, 0.60, 0.0)
DARK_LUMA = 12            # mean luma (0-255) below which a frame counts as black
PLACEHOLDER_DIR = "_placeholder"
PLACEHOLDER_ASPECT = (16, 9)
PLACEHOLDER_GRAY = 40
FRAME_TIMEOUT = 120


def frame_offsets(duration):
  """Seek offsets in seconds to try, in order, for a clip of this duration (None: unknown)."""
  if not duration or duration <= 0:
    return [0.0]
  seen, out = set(), []
  for fraction in FRAME_POSITIONS:
    t = round(min(duration * fraction, max(duration - 0.1, 0.0)), 2)
    if t not in seen:
      seen.add(t)
      out.append(t)
  return out


def is_dark(img):
  return ImageStat.Stat(img.convert("L")).mean[0] < DARK_LUMA


def extract_frame(source, offset, tools):
  """The frame at `offset` seconds as an RGB PIL image, or None (rotation tags are applied by
  ffmpeg; the pixel aspect ratio is made square)."""
  try:
    out = tools.ffmpeg_run(
        ["-ss", f"{offset:.2f}", "-i", source, "-frames:v", "1", "-an",
         "-vf", "scale=trunc(iw*sar):ih,setsar=1", "-f", "image2pipe", "-c:v", "mjpeg",
         "-q:v", "2", "-"], timeout=FRAME_TIMEOUT)
  except video.VideoError as e:
    logging.vlog(3, "no frame at %.2fs of %s: %s", offset, source, e)
    return None
  if not out:
    return None
  try:
    with Image.open(io.BytesIO(out)) as img:
      return img.convert("RGB")
  except Exception as e:   # Pillow raises many kinds of errors
    logging.vlog(3, "bad frame at %.2fs of %s: %s", offset, source, e)
    return None


def best_frame(source, tools=None):
  """A representative, non-black frame of the clip, or None if nothing could be decoded."""
  tools = tools or video.default_tools()
  if not tools.available():
    return None
  try:
    duration = float(tools.probe(source).get("format", {}).get("duration"))
  except (video.VideoError, TypeError, ValueError):
    duration = None
  brightest = None
  for offset in frame_offsets(duration):
    img = extract_frame(source, offset, tools)
    if img is None:
      continue
    if not is_dark(img):
      return img
    if brightest is None or (ImageStat.Stat(img.convert("L")).mean[0]
                             > ImageStat.Stat(brightest.convert("L")).mean[0]):
      brightest = img
  return brightest


def placeholder_path(thumbs_dir, size):
  """The shared placeholder JPEG for a size (created on first use)."""
  dest = os.path.join(thumbs_dir, PLACEHOLDER_DIR, size + ".jpg")
  if not os.path.isfile(dest):
    edge = thumbs.LONG_EDGE[size] or 1280
    w = edge
    h = max(1, edge * PLACEHOLDER_ASPECT[1] // PLACEHOLDER_ASPECT[0])
    img = Image.new("RGB", (w, h), (PLACEHOLDER_GRAY,) * 3)
    thumbs.save(img, dest, None)
  return dest


def make(pictures_dir, thumbs_dir, file_path, size, tools=None):
  """(path, source) of a thumbnail of exactly this size for a video, as thumbs.make() returns.

  A larger cached size is downscaled; otherwise one frame is extracted. source is "ffmpeg", or
  "placeholder" (never recorded) when no frame could be decoded.
  """
  dest = thumbs.thumb_path(thumbs_dir, size, file_path)
  for larger in thumbs.SIZES[thumbs.SIZES.index(size) + 1:]:
    cached = thumbs.lookup(thumbs_dir, larger, file_path)
    if cached:
      with Image.open(cached) as img:
        thumbs.save(img.convert("RGB"), dest, thumbs.LONG_EDGE[size])
      return dest, "pillow"
  frame = best_frame(os.path.join(pictures_dir, file_path), tools)
  if frame is None:
    logging.vlog(3, "%s: no decodable frame, serving the placeholder", file_path)
    return placeholder_path(thumbs_dir, size), "placeholder"
  thumbs.save(frame, dest, thumbs.LONG_EDGE[size])
  return dest, "ffmpeg"


def render_all(pictures_dir, thumbs_dir, file_path, tools=None):
  """Extract one frame and write every missing static size from it. {size: path}; {} when no
  frame could be decoded (the caller records the failure, ticket 190)."""
  missing = [s for s in thumbs.SIZES if not thumbs.lookup(thumbs_dir, s, file_path)]
  if not missing:
    return {}
  frame = best_frame(os.path.join(pictures_dir, file_path), tools)
  if frame is None:
    return {}
  return {s: thumbs.save(frame, thumbs.thumb_path(thumbs_dir, s, file_path), thumbs.LONG_EDGE[s])
          for s in missing}


# ---- animated thumbnails (ticket 189) ------------------------------------------------------------

RECIPE = "anim-v1"        # the AnimThumb recipe (recorded as its thumbs row's source)
# size name -> (long edge px, fps, fragments, fragment seconds, "whole clip" up to this many seconds)
# AnimThumb: the hover preview, eight 1 s fragments (a clip of 8 s or less is played whole).
# AnimSmall (ticket 201): what the loupe shows when the browser cannot play the original, so it should
# stand in for it: a clip of up to 75 s (= 5 x 15 s, where fragments would cover it anyway) whole, a
# longer one as five 15 s fragments spread over it.
ANIM_SPEC = {"AnimThumb": (300, 12, 8, 1.0, 8.0), "AnimSmall": (640, 15, 5, 15.0, 75.0)}
# size name -> recipe recorded in its thumbs row; a row with another recipe is stale (see invalidate_stale)
ANIM_RECIPES = {"AnimThumb": RECIPE, "AnimSmall": "anim-v3"}
ANIM_TIMEOUT = 900


def segment_starts(duration, n=8, length=1.0, whole_up_to=None):
  """Start offsets of the fragments: each is centred in one of n equal slices of the clip. A clip no
  longer than whole_up_to (default n*length, i.e. no room to skip anything) is used whole: [].
  Fragments shrink to tile a clip shorter than n of them (see fragment_length); AnimSmall never gets
  there, as its whole_up_to equals n * length."""
  if whole_up_to is None:
    whole_up_to = n * length
  if duration is None or duration <= whole_up_to:
    return []
  length = fragment_length(duration, n, length)
  slice_len = duration / n
  return [round(min(max((i + 0.5) * slice_len - length / 2, 0.0), duration - length), 3)
          for i in range(n)]


def fragment_length(duration, n, length):
  """The fragment length actually used: `length`, or less when n of them would not fit."""
  return min(length, duration / n) if duration else length


def anim_command(source, dest, duration, long_edge, fps, n=8, length=1.0, whole_up_to=None):
  """ffmpeg argument list that writes the muted VP9 WebM preview of source to dest."""
  if whole_up_to is None:
    whole_up_to = n * length
  scale = (f"scale='if(gt(iw,ih),min({long_edge},iw),-2)':'if(gt(iw,ih),-2,min({long_edge},ih))'"
           f":flags=lanczos,fps={fps},setsar=1,setpts=PTS-STARTPTS")
  starts = segment_starts(duration, n, length, whole_up_to)
  args = []
  if not starts:                        # a short (or unknown length) clip: the whole of it, capped
    args += ["-t", f"{whole_up_to:g}", "-i", source, "-an", "-vf", scale]
  else:
    frag = fragment_length(duration, n, length)
    for t in starts:
      args += ["-ss", f"{t:.3f}", "-t", f"{frag:.3f}", "-i", source]
    chains = "".join(f"[{i}:v]{scale}[v{i}];" for i in range(len(starts)))
    inputs = "".join(f"[v{i}]" for i in range(len(starts)))
    args += ["-an", "-filter_complex", f"{chains}{inputs}concat=n={len(starts)}:v=1:a=0[out]",
             "-map", "[out]"]
  args += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "40", "-pix_fmt", "yuv420p", "-row-mt", "1",
           "-deadline", "good", "-cpu-used", "5", "-f", "webm", "-y", dest]
  return args


def invalidate_stale(conn, thumbs_dir):
  """Delete every animated thumbnail made with an older recipe than its size's current one (rows
  and files), so the populator remakes it. Returns how many were removed (ticket 201)."""
  removed = 0
  for size, recipe in ANIM_RECIPES.items():
    rows = conn.execute("SELECT file_id, path FROM thumbs WHERE size = ? AND source != ?",
                        (size, recipe)).fetchall()
    for r in rows:
      try:
        os.remove(r["path"])
      except FileNotFoundError:
        pass
      conn.execute("DELETE FROM thumbs WHERE file_id = ? AND size = ?", (r["file_id"], size))
      removed += 1
  if removed:
    conn.commit()
    logging.info("removed %d animated thumbnail(s) made with an older recipe", removed)
  return removed


def make_anim(pictures_dir, thumbs_dir, file_path, size, tools=None):
  """Write the animated preview of this size ("AnimThumb" / "AnimSmall") and return its path, or
  None if the clip cannot be decoded (ffmpeg missing, broken file, timeout). The write is atomic."""
  tools = tools or video.default_tools()
  if size not in ANIM_SPEC or not tools.available():
    return None
  long_edge, fps, n, length, whole_up_to = ANIM_SPEC[size]
  source = os.path.join(pictures_dir, file_path)
  try:
    try:
      duration = float(tools.probe(source).get("format", {}).get("duration"))
    except (TypeError, ValueError):
      duration = None
    dest = thumbs.anim_path(thumbs_dir, size, file_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    try:
      tools.ffmpeg_run(anim_command(source, tmp, duration, long_edge, fps, n, length, whole_up_to), timeout=ANIM_TIMEOUT)
      if not os.path.getsize(tmp):
        raise video.VideoError("ffmpeg wrote an empty file")
      os.replace(tmp, dest)
    finally:
      if os.path.exists(tmp):
        os.unlink(tmp)
  except (video.VideoError, OSError) as e:
    logging.warning("%s: animated %s thumbnail failed: %s", file_path, size, e)
    return None
  return dest
