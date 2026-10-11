import os
import struct

import pytest
from PIL import Image

from photoapp import config
from photoapp import db


def make_jpeg(path, size=(30, 20), color="red"):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  Image.new("RGB", size, color).save(path)


def write_synthetic_pef(path):
  """A minimal big-endian TIFF (like a PEF) with EXIF in IFD0 + the Exif sub-IFD, built by hand
  because Pillow's TIFF writer drops the sub-IFD. Exercises the ticket 161 fallback reader and the
  ticket 162 stale-record path without a real camera file."""
  blobs = {"make": b"PENTAX Corporation\x00", "model": b"PENTAX *ist DL\x00",
           "dt": b"2008:02:16 18:25:54\x00", "dto": b"2008:02:16 18:29:00\x00",
           "lens": b"smc PENTAX-DA 18-55mm\x00",
           "exp": struct.pack(">II", 1, 8), "fnum": struct.pack(">II", 45, 10),
           "focal": struct.pack(">II", 43, 1)}
  ifd0_off, ifd0_len, exif_len = 8, 2 + 4 * 12 + 4, 2 + 6 * 12 + 4
  exif_off = ifd0_off + ifd0_len
  offsets, cur = {}, exif_off + exif_len
  for blob in blobs.values():
    offsets[blob] = cur
    cur += len(blob)

  def ascii_(tag, key):
    b = blobs[key]
    return struct.pack(">HHI", tag, 2, len(b)) + struct.pack(">I", offsets[b])
  def rational_(tag, key):
    return struct.pack(">HHI", tag, 5, 1) + struct.pack(">I", offsets[blobs[key]])
  def short_(tag, v):
    return struct.pack(">HHI", tag, 3, 1) + struct.pack(">H", v) + b"\x00\x00"

  ifd0 = struct.pack(">H", 4) + ascii_(0x010F, "make") + ascii_(0x0110, "model") \
      + ascii_(0x0132, "dt") + struct.pack(">HHI", 0x8769, 4, 1) + struct.pack(">I", exif_off) \
      + struct.pack(">I", 0)
  exif = struct.pack(">H", 6) + rational_(0x829A, "exp") + rational_(0x829D, "fnum") \
      + short_(0x8827, 200) + ascii_(0x9003, "dto") + rational_(0x920A, "focal") \
      + ascii_(0xA434, "lens") + struct.pack(">I", 0)
  with open(path, "wb") as f:
    f.write(b"MM" + struct.pack(">H", 42) + struct.pack(">I", ifd0_off) + ifd0 + exif
            + b"".join(blobs.values()))


def write_synthetic_raf(path, undefined_aperture=False):
  """A minimal Fujifilm RAF: the 16-byte magic, a big-endian offset/length pair at byte 84 for an
  embedded EXIF JPEG (as in a real RAF), and filler instead of raw data. Pillow cannot open it,
  exactly like a real RAF (ticket 218). undefined_aperture writes the EXIF 'undefined' FNumber a
  manual lens produces (0/0, which Pillow reads as NaN)."""
  import io
  exif = Image.Exif()
  exif[0x010F], exif[0x0110] = "FUJIFILM", "X-H1"
  sub = exif.get_ifd(0x8769)
  sub[0x9003] = "2026:10:11 17:23:10"
  sub[0x829A] = (1, 28)
  sub[0x829D] = (0, 0) if undefined_aperture else (28, 10)
  sub[0x8827] = 51200
  sub[0x920A] = (50, 1)
  sub[0xA405] = 75
  buf = io.BytesIO()
  Image.new("RGB", (30, 20), "blue").save(buf, "JPEG", exif=exif)
  jpeg = buf.getvalue()
  header = b"FUJIFILMCCD-RAW " + b"0201FF129502" + b"X-H1".ljust(32, b"\0") + b"0100".ljust(4, b"\0")
  header = header.ljust(84, b"\0") + struct.pack(">II", 160, len(jpeg))
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "wb") as f:
    f.write(header.ljust(160, b"\0") + jpeg + b"\0" * 64)


@pytest.fixture
def settings(tmp_path):
  pics = tmp_path / "pics"
  pics.mkdir()
  return config.Settings(
      pictures_dir=str(pics), thumbs_dir=str(tmp_path / "thumbs"),
      state_dir=str(tmp_path / "state"))


@pytest.fixture
def conn(settings):
  c = db.open_state(settings.state_dir)
  yield c
  c.close()
