"""Pure per-file helpers shared by the scanner and the file_metadata.py CLI.

No absl flags are defined here, so this module is safe to import from
anywhere (file_metadata.py defines required flags at import time).
"""

import datetime
import hashlib
import mimetypes
import os
import re

from absl import logging

from PIL import Image

Image.init()  # populates Image.MIME

# Same set of genuine-image extensions as metadata_db.py (duplicated rather
# than imported, since metadata_db.py is a standalone CLI script whose
# module-level required flags would otherwise leak into this tool's flags).
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.dng', '.tif', '.tiff', '.heic', '.heif',
    '.cr2', '.cr3', '.nef', '.arw', '.raf', '.rw2', '.orf', '.gif',
    '.bmp', '.webp', '.pef',
}

_HASH_CHUNK_SIZE = 1024 * 1024

# EXIF tags: (date tag, matching UTC offset tag), in order of preference.
_EXIF_IFD_POINTER = 0x8769
_EXIF_DATE_TAGS = (
    (0x9003, 0x9011),  # DateTimeOriginal, OffsetTimeOriginal
    (0x9004, 0x9012),  # DateTimeDigitized, OffsetTimeDigitized
    (0x0132, 0x9010),  # DateTime, OffsetTime
)
_EXIF_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def hash_file(path):
  """Same sha224-over-chunks hash as hash_dir.py's hash_file."""
  digest = hashlib.sha224()
  with open(path, "rb") as f:
    while True:
      chunk = f.read(_HASH_CHUNK_SIZE)
      if not chunk:
        break
      digest.update(chunk)
  return digest.hexdigest()


def is_image(name):
  return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def _exif_str(value):
  if isinstance(value, bytes):
    value = value.decode("ascii", "ignore")
  if not isinstance(value, str):
    return None
  return value.strip("\x00 ") or None


def parse_exif_date(date_str, offset_str=None):
  """Convert EXIF "YYYY:MM:DD HH:MM:SS" (+ optional "+09:00") to ISO form.

  Returns e.g. "2026-01-01 01:02:03+0900" ("2026-01-01 01:02:03" without
  a usable offset), or None if date_str is missing or not a valid date
  (cameras with an unset clock write "0000:00:00 00:00:00").
  """
  date_str = _exif_str(date_str)
  if date_str is None:
    return None
  try:
    parsed = datetime.datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
  except ValueError:
    return None
  result = parsed.strftime("%Y-%m-%d %H:%M:%S")
  offset_str = _exif_str(offset_str)
  m = _EXIF_OFFSET_RE.match(offset_str) if offset_str else None
  if m:
    result += m.group(1) + m.group(2) + m.group(3)
  return result


def read_exif_date(img):
  """Return the ISO exif date string of an open PIL image, or None."""
  try:
    exif = img.getexif()
    if not exif:
      return None
    sub_ifd = exif.get_ifd(_EXIF_IFD_POINTER)
  except Exception as e:
    logging.vlog(3, "Could not read EXIF: %s", e)
    return None
  for date_tag, offset_tag in _EXIF_DATE_TAGS:
    # DateTimeOriginal/Digitized live in the Exif sub-IFD; DateTime in IFD0.
    date_str = sub_ifd.get(date_tag, exif.get(date_tag))
    result = parse_exif_date(
        date_str, sub_ifd.get(offset_tag, exif.get(offset_tag)))
    if result is not None:
      return result
  return None


def read_exif_date_from_path(filepath):
  try:
    with Image.open(filepath) as img:
      return read_exif_date(img)
  except Exception as e:
    logging.warning("Could not read EXIF date from %s: %s", filepath, e)
    return None


def read_image_metadata(filepath):
  """Return (mime_type, width, height, exif_date) for filepath.

  width/height/exif_date are None, and mime_type falls back to a
  best-effort guess from the extension, when Pillow can't decode the
  file (e.g. RAW formats like CR3/RAF that aren't valid TIFF and have no
  Pillow plugin). exif_date is also None if the image has no EXIF date.
  """
  try:
    with Image.open(filepath) as img:
      width, height = img.size
      mime_type = Image.MIME.get(img.format)
      return mime_type, width, height, read_exif_date(img)
  except Exception as e:
    logging.warning("Could not decode image %s: %s", filepath, e)
    mime_type, _ = mimetypes.guess_type(filepath)
    return mime_type, None, None, None
