"""EXIF for exported JPEGs (ticket 207).

The rendered export JPEG carries no usable EXIF of its own (the cached Huge render is written by
Pillow without any), so a viewer -- Google Photos above all -- would date it by upload time. This
module builds a fresh EXIF block from what the app already knows about the source file (the `files`
row, filled by the scan from the RAW or JPG) plus two tags the scan does not store (exposure bias,
white balance) and, only when asked, GPS read from the source, and splices it into the finished
JPEG's bytes without re-encoding the pixels. The ICC profile (an APP2 segment) is left untouched.

Orientation is never written: an export's pixels are already upright (EXIF orientation applied, crop
and rotation baked in). Maker notes and embedded thumbnails are deliberately not copied.
"""

import fractions
import os
import re
import struct

from absl import logging
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_MAKE, _MODEL = 0x010F, 0x0110
_EXPOSURE_TIME, _FNUMBER, _ISO = 0x829A, 0x829D, 0x8827
_DATETIME_ORIGINAL, _OFFSET_TIME_ORIGINAL = 0x9003, 0x9011
_EXPOSURE_BIAS, _FOCAL_LENGTH = 0x9204, 0x920A
_WHITE_BALANCE, _FOCAL_LENGTH_35MM, _LENS_MODEL = 0xA403, 0xA405, 0xA434

_DATE_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d) (\d\d:\d\d:\d\d)(?:([+-])(\d\d):?(\d\d))?$")


def _rational(value):
  f = fractions.Fraction(value).limit_denominator(1000000)
  return IFDRational(f.numerator, f.denominator)


def _split_date(iso):
  """The files.exif_date form ("2026-01-01 01:02:03+0900") to EXIF ("2026:01:01 01:02:03",
  "+09:00" or None)."""
  m = _DATE_RE.match(iso or "")
  if not m:
    return None, None
  date = f"{m.group(1)}:{m.group(2)}:{m.group(3)} {m.group(4)}"
  offset = f"{m.group(5)}{m.group(6)}:{m.group(7)}" if m.group(5) else None
  return date, offset


def read_source_extras(path):
  """(exposure_bias, white_balance, gps_dict) read from the source file's EXIF, each None / {}
  when absent or when Pillow cannot open the file (e.g. a PEF)."""
  try:
    with Image.open(path) as img:
      exif = img.getexif()
      sub = exif.get_ifd(_EXIF_IFD)
      return sub.get(_EXPOSURE_BIAS), sub.get(_WHITE_BALANCE), dict(exif.get_ifd(_GPS_IFD))
  except Exception as e:
    logging.vlog(3, "No extra EXIF from %s: %s", path, e)
    return None, None, {}



def build_exif(row, source_path=None, include_gps=False):
  """The EXIF bytes (with the 'Exif\\0\\0' prefix, as Pillow writes them) for a `files` row.
  source_path (absolute) supplies exposure bias, white balance and, when include_gps, GPS."""
  exif = Image.Exif()
  sub = exif.get_ifd(_EXIF_IFD)
  if row["camera_make"]:
    exif[_MAKE] = row["camera_make"]
  if row["camera_model"]:
    exif[_MODEL] = row["camera_model"]
  date, offset = _split_date(row["exif_date"])
  if date:
    sub[_DATETIME_ORIGINAL] = date
    if offset:
      sub[_OFFSET_TIME_ORIGINAL] = offset
  if row["aperture"] is not None:
    sub[_FNUMBER] = _rational(row["aperture"])
  if row["shutter_speed"] is not None:
    sub[_EXPOSURE_TIME] = _rational(row["shutter_speed"])
  if row["iso"] is not None:
    sub[_ISO] = int(row["iso"])
  if row["focal_length"] is not None:
    sub[_FOCAL_LENGTH] = _rational(row["focal_length"])
  if row["focal_length_35mm"] is not None:
    sub[_FOCAL_LENGTH_35MM] = int(row["focal_length_35mm"])
  if row["lens_model"]:
    sub[_LENS_MODEL] = row["lens_model"]
  if source_path:
    bias, white_balance, gps = read_source_extras(source_path)
    if bias is not None:
      sub[_EXPOSURE_BIAS] = _rational(float(bias))
    if white_balance is not None:
      sub[_WHITE_BALANCE] = int(white_balance)
    if include_gps and gps:
      exif.get_ifd(_GPS_IFD).update(gps)
  return exif.tobytes()


def _segments(data):
  """Yield (marker, start, end) of the JPEG's header segments after SOI, up to start of scan."""
  pos = 2
  while pos + 4 <= len(data) and data[pos] == 0xFF:
    marker = data[pos + 1]
    if marker == 0xDA or marker == 0xD9:
      return
    length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
    yield marker, pos, pos + 2 + length
    pos += 2 + length


def inject_exif(jpeg_path, exif_bytes):
  """Replace any Exif APP1 in the JPEG at jpeg_path with exif_bytes, keeping every other segment
  (ICC, JFIF) and the compressed data byte-for-byte. Atomic (temp file + rename)."""
  with open(jpeg_path, "rb") as f:
    data = f.read()
  if data[:2] != b"\xff\xd8":
    raise ValueError(f"not a JPEG: {jpeg_path}")
  payload = exif_bytes if exif_bytes.startswith(b"Exif\x00\x00") else b"Exif\x00\x00" + exif_bytes
  if len(payload) + 2 > 0xFFFF:
    raise ValueError("EXIF block too large")
  app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
  keep_from, drop = 2, []
  insert_at = 2
  for marker, start, end in _segments(data):
    if marker == 0xE1 and data[start + 4:start + 10] == b"Exif\x00\x00":
      drop.append((start, end))
    elif marker == 0xE0 and start == insert_at:   # keep JFIF first
      insert_at = end
  out, last = bytearray(), 0
  for start, end in drop:
    out += data[last:start]
    last = end
  out += data[last:]
  # insert_at refers to the original offsets; drops only happen at or after it (APP1 follows APP0).
  shift = sum(e - s for s, e in drop if e <= insert_at)
  at = insert_at - shift
  tmp = jpeg_path + ".exif.tmp"
  with open(tmp, "wb") as f:
    f.write(bytes(out[:at]) + app1 + bytes(out[at:]))
  os.replace(tmp, jpeg_path)


def write_export_exif(conn, settings, file_id, dest, include_gps=False):
  """Give the exported JPEG at dest the EXIF of its source file (ticket 207). Not best effort: a
  failure raises, so the export job shows it."""
  row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
  if row is None:
    return
  source = os.path.join(settings.pictures_dir, row["path"])
  inject_exif(dest, build_exif(row, source, include_gps))
