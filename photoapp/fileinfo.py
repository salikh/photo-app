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
import struct

from absl import logging

from PIL import Image
from PIL import TiffImagePlugin

from photoapp import pentax_lens

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
# Ticket 170 adds FocalLengthIn35mmFilm (Exif sub-IFD) -- the full-frame-equivalent focal length.
_EXIF_FOCAL_LENGTH = 0x920A
_EXIF_FOCAL_LENGTH_35MM = 0xA405
_EXIF_MAKE = 0x010F
_EXIF_MODEL = 0x0110
_EXIF_LENS_MODEL = 0xA434

# Ticket 166: Pentax keeps the lens in its MakerNote as a two-byte LensType code rather than the
# standard LensModel tag. For a DNG the MakerNote is IFD0's DNGPrivateData (0xC634); for a PEF (and
# everything else) it is the Exif sub-IFD's MakerNote (0x927C). Inside, tag 0x003F (LensRec) holds
# the code in its first two bytes.
_PENTAX_DNG_PRIVATE_DATA = 0xC634
_PENTAX_MAKERNOTE = 0x927C
_PENTAX_LENS_TAG = 0x003F


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
  """Return (focal_length, camera_make, camera_model, lens_model, focal_length_35mm) of an open PIL
  image.

  focal_length is in millimetres (a float, from the EXIF FocalLength rational in the Exif
  sub-IFD); camera_make/model are the Make/Model strings from IFD0; lens_model is the LensModel
  string (ticket 156) from the Exif sub-IFD, the lens used with the picture; focal_length_35mm is
  the FocalLengthIn35mmFilm integer (ticket 170), the full-frame-equivalent focal length. Any of
  the five is None if that tag is absent; all five are None if there is no EXIF at all. Works for
  RAW files for the same reason read_camera_metadata does (ticket 111)."""
  try:
    exif = img.getexif()
    if not exif:
      return None, None, None, None, None
    sub_ifd = exif.get_ifd(_EXIF_IFD_POINTER)
  except Exception as e:
    logging.vlog(3, "Could not read EXIF: %s", e)
    return None, None, None, None, None
  focal_length = sub_ifd.get(_EXIF_FOCAL_LENGTH)
  focal_length_35mm = sub_ifd.get(_EXIF_FOCAL_LENGTH_35MM)
  return (_exif_rational_to_float(focal_length) if focal_length is not None else None,
          _exif_str(exif.get(_EXIF_MAKE)), _exif_str(exif.get(_EXIF_MODEL)),
          _exif_str(sub_ifd.get(_EXIF_LENS_MODEL, exif.get(_EXIF_LENS_MODEL))),
          int(focal_length_35mm) if focal_length_35mm is not None else None)


def _read_tiff_ifd(f, offset, byteorder):
  """Parse the TIFF IFD at offset with PIL's own small IFD reader."""
  ifd = TiffImagePlugin.ImageFileDirectory_v2()
  ifd._endian = byteorder
  f.seek(offset)
  ifd.load(f)
  return ifd


def _pentax_lens_code(makernote):
  """(series, lens_id) from a Pentax MakerNote's LensType, or None (ticket 166).

  Handles both MakerNote headers seen in the library: "PENTAX \\0" (offset 10, DNG DNGPrivateData)
  and "AOC\\0" (offset 6, PEF MakerNote), each followed by an MM/II byte-order marker and a standard
  IFD whose tag 0x003F (LensRec) starts with the two code bytes.
  """
  if makernote[:8] == b"PENTAX \x00":
    header = 8
  elif makernote[:4] == b"AOC\x00":
    header = 4
  else:
    return None
  order = makernote[header:header + 2]
  if order == b"MM":
    endian = ">"
  elif order == b"II":
    endian = "<"
  else:
    return None
  start = header + 2
  try:
    count = struct.unpack(endian + "H", makernote[start:start + 2])[0]
  except struct.error:
    return None
  for i in range(count):
    entry = start + 2 + i * 12
    if entry + 12 > len(makernote):
      break
    tag, value_type, value_count = struct.unpack(endian + "HHI", makernote[entry:entry + 8])
    if tag != _PENTAX_LENS_TAG:
      continue
    value = makernote[entry + 8:entry + 12]
    offset = struct.unpack(endian + "I", value)[0]
    if value_type == 7 and value_count > 4 and offset + 2 <= len(makernote):
      value = makernote[offset:offset + 4]   # LensRec stored out of line (offsets are from the start)
    return (value[0], value[1]) if len(value) >= 2 else None
  return None


def read_pentax_lens(filepath):
  """The lens name from a Pentax MakerNote's LensType code, or None (ticket 166).

  A Pentax file usually has no standard EXIF LensModel tag; the lens is a two-byte code in its
  MakerNote (DNGPrivateData for a DNG, MakerNote for a PEF), decoded with the vendored ExifTool
  table in photoapp/pentax_lens.py. Any failure (not a TIFF, no Pentax MakerNote, a code the table
  does not know) is just None, matching the other EXIF readers' contract.
  """
  try:
    with open(filepath, "rb") as f:
      header = f.read(8)
      if len(header) < 8:
        return None
      if header[:2] == b"II":
        byteorder = "<"
      elif header[:2] == b"MM":
        byteorder = ">"
      else:
        return None
      ifd0 = _read_tiff_ifd(f, struct.unpack(byteorder + "I", header[4:8])[0], byteorder)
      makernote = ifd0.get(_PENTAX_DNG_PRIVATE_DATA)
      if not makernote:
        exif_offset = ifd0.get(_EXIF_IFD_POINTER)
        if exif_offset:
          makernote = _read_tiff_ifd(f, exif_offset, byteorder).get(_PENTAX_MAKERNOTE)
  except Exception as e:
    logging.vlog(3, "Could not read Pentax MakerNote from %s: %s", filepath, e)
    return None
  if not makernote:
    return None
  code = _pentax_lens_code(makernote)
  return pentax_lens.decode(*code) if code is not None else None


def read_focal_length_35mm(filepath):
  """The 35mm full-frame-equivalent focal length (EXIF FocalLengthIn35mmFilm, 0xA405), or None.

  A cheap EXIF-only read -- no RAW decode or hashing -- for the scan's cache patch (ticket 171):
  Pillow for a file it can open, read_exif_from_tiff for a PEF (which Pillow cannot). The value is
  an integer number of millimetres.
  """
  try:
    with Image.open(filepath) as img:
      value = img.getexif().get_ifd(_EXIF_IFD_POINTER).get(_EXIF_FOCAL_LENGTH_35MM)
      return int(value) if value is not None else None
  except Exception as e:
    logging.vlog(3, "Could not read 35mm focal length from %s: %s", filepath, e)
  if is_raw(filepath):
    return read_exif_from_tiff(filepath)[8]   # focal_length_35mm is the last of the nine
  return None


def read_exif_from_tiff(filepath):
  """Fallback EXIF for a RAW file Pillow cannot open (ticket 161: Pentax PEF).

  Returns (exif_date, aperture, shutter_speed, iso, focal_length, camera_make, camera_model,
  lens_model, focal_length_35mm) -- the same values read_exif_date/read_camera_metadata/
  read_lens_metadata give an open PIL image, or all None if filepath is not a readable TIFF.

  A PEF is a Pentax-compressed TIFF: Pillow refuses to identify it (it cannot decode the pixel
  strip), but its EXIF lives in ordinary TIFF IFDs, so reading IFD0 and the Exif sub-IFD directly
  gets everything without touching the image data. Byte order comes from the file header (II/MM).
  """
  none = (None,) * 9
  try:
    with open(filepath, "rb") as f:
      header = f.read(8)
      if len(header) < 8:
        return none
      order = header[:2]
      if order == b"II":
        byteorder = "<"
      elif order == b"MM":
        byteorder = ">"
      else:
        return none
      ifd0 = _read_tiff_ifd(f, struct.unpack(byteorder + "I", header[4:8])[0], byteorder)
      sub_offset = ifd0.get(_EXIF_IFD_POINTER)
      sub_ifd = _read_tiff_ifd(f, sub_offset, byteorder) if sub_offset else {}
  except Exception as e:
    logging.vlog(3, "Could not read TIFF EXIF from %s: %s", filepath, e)
    return none

  def pick(tag):
    # DateTimeOriginal/Digitized and the camera tags live in the Exif sub-IFD; DateTime/Make/Model
    # in IFD0 (same split read_exif_date/read_camera_metadata/read_lens_metadata already assume).
    return sub_ifd.get(tag, ifd0.get(tag))

  aperture = pick(_EXIF_FNUMBER)
  shutter_speed = pick(_EXIF_EXPOSURE_TIME)
  iso = pick(_EXIF_ISO)
  focal_length = pick(_EXIF_FOCAL_LENGTH)
  focal_length_35mm = pick(_EXIF_FOCAL_LENGTH_35MM)
  exif_date = None
  for date_tag, offset_tag in _EXIF_DATE_TAGS:
    result = parse_exif_date(pick(date_tag), pick(offset_tag))
    if result is not None:
      exif_date = result
      break
  return (
      exif_date,
      _exif_rational_to_float(aperture) if aperture is not None else None,
      _exif_rational_to_float(shutter_speed) if shutter_speed is not None else None,
      int(iso) if iso is not None else None,
      _exif_rational_to_float(focal_length) if focal_length is not None else None,
      _exif_str(ifd0.get(_EXIF_MAKE)), _exif_str(ifd0.get(_EXIF_MODEL)),
      _exif_str(pick(_EXIF_LENS_MODEL)),
      int(focal_length_35mm) if focal_length_35mm is not None else None)


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
  camera_make, camera_model, lens_model, focal_length_35mm) for filepath.

  All but mime_type are None, and mime_type falls back to a best-effort guess from the extension,
  when the file cannot be decoded (e.g. RAW formats LibRaw does not know). Any EXIF field is also
  None if the image has no EXIF, or lacks that specific tag. RAW files get their real dimensions
  from LibRaw (see read_raw_size), not Pillow's IFD0 thumbnail -- but camera metadata still comes
  from Pillow's EXIF parse (tickets 083/111/156/170; same reason exif_date already works for RAW:
  the EXIF header parses even when the image data does not). A RAW file Pillow cannot open at all
  (ticket 161: a Pentax PEF) falls back to read_exif_from_tiff, so its EXIF is read from the TIFF
  IFDs instead. When the standard LensModel tag is absent (every Pentax file here), the lens is read
  from the MakerNote's coded LensType and decoded with the vendored ExifTool table (ticket 166).
  """
  mime_type = width = height = exif_date = aperture = shutter_speed = iso = None
  focal_length = camera_make = camera_model = lens_model = focal_length_35mm = None
  try:
    with Image.open(filepath) as img:
      width, height = img.size
      mime_type = Image.MIME.get(img.format)
      exif_date = read_exif_date(img)
      aperture, shutter_speed, iso = read_camera_metadata(img)
      focal_length, camera_make, camera_model, lens_model, focal_length_35mm = \
          read_lens_metadata(img)
  except Exception as e:
    logging.warning("Could not decode image %s: %s", filepath, e)
    mime_type, _ = mimetypes.guess_type(filepath)
    if is_raw(filepath):
      (exif_date, aperture, shutter_speed, iso, focal_length, camera_make, camera_model,
       lens_model, focal_length_35mm) = read_exif_from_tiff(filepath)
  if is_raw(filepath):
    raw_size = read_raw_size(filepath)
    if raw_size is not None:
      width, height, mime_type = raw_size
    if lens_model is None:
      lens_model = read_pentax_lens(filepath)
  return (mime_type, width, height, exif_date, aperture, shutter_speed, iso,
          focal_length, camera_make, camera_model, lens_model, focal_length_35mm)


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
