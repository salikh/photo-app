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
