"""Pure per-file helpers used by the scanner (hashing, mime/dimensions, EXIF).

No absl flags are defined here, so this module is safe to import from anywhere.
`hash_file`/`get_or_compute_hash` intentionally match the hashing scheme of
tools/archive/catalog.py (sha224 over chunks) -- not shared code, since that
tool has no dependency on this package, but kept in step so a hash computed by
one is directly comparable to a hash computed by the other (see
tools/archive/README.md).
"""

import datetime
import hashlib
import mimetypes
import os
import re
import sqlite3

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

# Subset of IMAGE_EXTENSIONS that are camera RAW formats.
RAW_EXTENSIONS = {
    '.dng', '.cr2', '.cr3', '.nef', '.arw', '.raf', '.rw2', '.orf', '.pef',
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

# Camera metadata (ticket 083): all three live in the Exif sub-IFD, same as the date tags above.
# FNumber (aperture, f-number); ExposureTime (shutter speed, seconds); ISOSpeedRatings (ISO --
# also the tag PhotographicSensitivity uses under its EXIF 2.3+ name; same numeric tag either way).
_EXIF_FNUMBER = 0x829D
_EXIF_EXPOSURE_TIME = 0x829A
_EXIF_ISO = 0x8827

# Ticket 111: focal length (FocalLength, Exif sub-IFD) and camera make/model (Make/Model, IFD0).
# Ticket 156 adds LensModel (Exif sub-IFD) -- the lens used with the picture.
_EXIF_FOCAL_LENGTH = 0x920A
_EXIF_MAKE = 0x010F
_EXIF_MODEL = 0x0110
_EXIF_LENS_MODEL = 0xA434


def hash_file(path):
  """Same sha224-over-chunks hash as tools/archive/catalog.py's hash_file."""
  digest = hashlib.sha224()
  with open(path, "rb") as f:
    while True:
      chunk = f.read(_HASH_CHUNK_SIZE)
      if not chunk:
        break
      digest.update(chunk)
  return digest.hexdigest()


def is_raw(name):
  return os.path.splitext(name)[1].lower() in RAW_EXTENSIONS


def is_image(name):
  return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


# Ticket 101: filenames that are never scanned into the database at all, checked before
# is_image/is_sidecar. Starts with just macOS AppleDouble resource-fork files ("._IMG_1234.JPG",
# "._K.DNG.xmp"), created automatically whenever macOS writes to a non-native filesystem (e.g. this
# library mounted over SMB/AFP/NFS) -- not real image/sidecar content, even though the extension
# matches.
# Ticket 111 adds the metadata cache files (index.json and the per-file <name>.json next to each
# image), which the opt-in scan cache writes into the library; they must never be scanned as
# photos or mistaken for sidecars, even though they are already not image/sidecar extensions.
def is_ignored(name):
  return name.startswith("._") or name == "index.json" or name.endswith(".json")


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


def _exif_rational_to_float(value):
  """A rational EXIF value to float: normally an IFDRational (has __float__), but a value
  written by Pillow itself (img.getexif() round-tripped through save(exif=...), as in this
  module's own tests) can come back as a plain (numerator, denominator) tuple instead -- float()
  does not accept that directly, so it needs its own conversion."""
  if isinstance(value, tuple) and len(value) == 2:
    num, den = value
    return float(num) / float(den) if den else None
  return float(value)


def read_camera_metadata(img):
  """Return (aperture, shutter_speed, iso) of an open PIL image: f-number (float, e.g. 2.8),
  exposure time in seconds (float, e.g. 0.004 for 1/250s -- formatting that back into "1/250" is
  a display concern, not this function's), and ISO (int). Any of the three is None if that tag
  is absent; all three are None if there is no EXIF at all. Same sub-IFD lookup as
  read_exif_date, and works for RAW files for the same reason (Pillow parses the EXIF/IFD0
  header even when it cannot decode the image data itself)."""
  try:
    exif = img.getexif()
    if not exif:
      return None, None, None
    sub_ifd = exif.get_ifd(_EXIF_IFD_POINTER)
  except Exception as e:
    logging.vlog(3, "Could not read EXIF: %s", e)
    return None, None, None
  aperture = sub_ifd.get(_EXIF_FNUMBER)
  shutter_speed = sub_ifd.get(_EXIF_EXPOSURE_TIME)
  iso = sub_ifd.get(_EXIF_ISO)
  return (_exif_rational_to_float(aperture) if aperture is not None else None,
         _exif_rational_to_float(shutter_speed) if shutter_speed is not None else None,
         int(iso) if iso is not None else None)


def read_lens_metadata(img):
  """Return (focal_length, camera_make, camera_model, lens_model) of an open PIL image.

  focal_length is in millimetres (a float, from the EXIF FocalLength rational in the Exif
  sub-IFD); camera_make/model are the Make/Model strings from IFD0; lens_model is the LensModel
  string (ticket 156) from the Exif sub-IFD, the lens used with the picture. Any of the four is
  None if that tag is absent; all four are None if there is no EXIF at all. Works for RAW files
  for the same reason read_camera_metadata does (ticket 111)."""
  try:
    exif = img.getexif()
    if not exif:
      return None, None, None, None
    sub_ifd = exif.get_ifd(_EXIF_IFD_POINTER)
  except Exception as e:
    logging.vlog(3, "Could not read EXIF: %s", e)
    return None, None, None, None
  focal_length = sub_ifd.get(_EXIF_FOCAL_LENGTH)
  return (_exif_rational_to_float(focal_length) if focal_length is not None else None,
          _exif_str(exif.get(_EXIF_MAKE)), _exif_str(exif.get(_EXIF_MODEL)),
          _exif_str(sub_ifd.get(_EXIF_LENS_MODEL, exif.get(_EXIF_LENS_MODEL))))


def read_exif_date_from_path(filepath):
  try:
    with Image.open(filepath) as img:
      return read_exif_date(img)
  except Exception as e:
    logging.warning("Could not read EXIF date from %s: %s", filepath, e)
    return None


def read_raw_size(filepath):
  """(width, height, mime_type) of a RAW file in display orientation, or None.

  Pillow opens DNG/TIFF-based RAW files but returns only the tiny IFD0
  thumbnail (for example 160x120), so RAW dimensions come from LibRaw. The
  crop size is used: it is the size viewers show and matches the camera JPEG.
  """
  try:
    import rawpy
    with rawpy.imread(filepath) as raw:
      sizes = raw.sizes
      width = sizes.crop_width or sizes.width
      height = sizes.crop_height or sizes.height
      if sizes.flip in (5, 6):
        width, height = height, width
  except Exception as e:  # rawpy raises LibRawError and OSError subclasses
    logging.vlog(3, "LibRaw could not read %s: %s", filepath, e)
    return None
  mime_type, _ = mimetypes.guess_type(filepath)
  if os.path.splitext(filepath)[1].lower() == ".dng":
    mime_type = "image/x-adobe-dng"
  return width, height, mime_type or "image/x-raw"


def read_image_metadata(filepath):
  """Return (mime_type, width, height, exif_date, aperture, shutter_speed, iso, focal_length,
  camera_make, camera_model, lens_model) for filepath.

  width/height/exif_date/aperture/shutter_speed/iso/focal_length/camera_make/camera_model/
  lens_model are None, and mime_type falls back to a best-effort guess from the extension, when the
  file cannot be decoded (e.g. RAW formats LibRaw does not know). Any EXIF field is also None if the
  image has no EXIF, or lacks that specific tag. RAW files get their real dimensions from LibRaw
  (see read_raw_size), not Pillow's IFD0 thumbnail -- but camera metadata still comes from Pillow's
  EXIF parse (tickets 083/111/156; same reason exif_date already works for RAW: the EXIF header
  parses even when the image data does not).
  """
  mime_type = width = height = exif_date = aperture = shutter_speed = iso = None
  focal_length = camera_make = camera_model = lens_model = None
  try:
    with Image.open(filepath) as img:
      width, height = img.size
      mime_type = Image.MIME.get(img.format)
      exif_date = read_exif_date(img)
      aperture, shutter_speed, iso = read_camera_metadata(img)
      focal_length, camera_make, camera_model, lens_model = read_lens_metadata(img)
  except Exception as e:
    logging.warning("Could not decode image %s: %s", filepath, e)
    mime_type, _ = mimetypes.guess_type(filepath)
  if is_raw(filepath):
    raw_size = read_raw_size(filepath)
    if raw_size is not None:
      width, height, mime_type = raw_size
  return (mime_type, width, height, exif_date, aperture, shutter_speed, iso,
          focal_length, camera_make, camera_model, lens_model)


def load_precomputed_hashes(db_path):
  """Return {filename: (hash, mtime)} from a tools/archive/catalog.py-produced database."""
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  try:
    return {
        filename: (hash_, mtime)
        for filename, hash_, mtime in conn.execute(
            "SELECT filename, hash, mtime FROM hashes")
    }
  finally:
    conn.close()


def get_or_compute_hash(filepath, rel_path, file_mtime, precomputed_hashes):
  if precomputed_hashes is not None:
    pre = precomputed_hashes.get(rel_path)
    if pre is not None:
      pre_hash, pre_mtime = pre
      if pre_mtime == file_mtime:
        logging.vlog(5, "Reusing precomputed hash for %s", filepath)
        return pre_hash
  logging.vlog(3, "Hashing %s", filepath)
  try:
    return hash_file(filepath)
  except OSError as e:
    logging.error("Could not hash %s: %s", filepath, e)
    return None
