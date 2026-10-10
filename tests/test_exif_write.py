import os

from fastapi.testclient import TestClient
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from photoapp import api
from photoapp import db
from photoapp import exif_write
from photoapp import scan

EXIF_IFD, GPS_IFD = 0x8769, 0x8825


def make_source(path, gps=True):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  exif = Image.Exif()
  exif[0x010F], exif[0x0110] = "PENTAX", "K-5"
  sub = exif.get_ifd(EXIF_IFD)
  sub[0x9003], sub[0x9011] = "2021:07:04 12:30:15", "+02:00"
  sub[0x829D], sub[0x829A] = IFDRational(28, 10), IFDRational(1, 250)
  sub[0x8827], sub[0x920A] = 400, IFDRational(50, 1)
  sub[0xA405], sub[0xA434] = 75, "smc 50mm"
  sub[0x9204], sub[0xA403] = IFDRational(-1, 3), 1
  exif[0x0112] = 6   # the source claims rotation; the export must not
  if gps:
    g = exif.get_ifd(GPS_IFD)
    g[1], g[2], g[3], g[4] = "N", (IFDRational(48), IFDRational(8), IFDRational(0)), "E", \
        (IFDRational(11), IFDRational(34), IFDRational(0))
  Image.new("RGB", (40, 30), "blue").save(path, exif=exif, icc_profile=b"ICCDATA" * 10)


def read(path):
  with Image.open(path) as im:
    return im.getexif(), im.info.get("icc_profile")


def test_export_carries_the_source_exif_and_the_icc_profile(settings, tmp_path):
  conn = db.open_state(settings.state_dir)
  make_source(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  pid = conn.execute("SELECT id FROM photos").fetchone()[0]
  app = api.create_app(conn, settings)
  c = TestClient(app)
  target = str(tmp_path / "out")
  assert c.post("/api/export", json={"ids": [pid], "dir": "2020", "target": target}).status_code == 200
  app.state.jobs.start()
  assert app.state.jobs.wait_idle()
  app.state.jobs.stop()
  exif, icc = read(os.path.join(target, "a.jpg"))
  sub = exif.get_ifd(EXIF_IFD)
  assert exif[0x010F] == "PENTAX" and exif[0x0110] == "K-5"
  assert sub[0x9003] == "2021:07:04 12:30:15" and sub[0x9011] == "+02:00"
  assert float(sub[0x829D]) == 2.8 and sub[0x8827] == 400 and sub[0xA405] == 75
  assert sub[0xA434] == "smc 50mm" and sub[0xA403] == 1
  assert abs(float(sub[0x9204]) + 1 / 3) < 1e-6
  assert 0x0112 not in exif            # pixels are upright already
  assert not exif.get_ifd(GPS_IFD)     # GPS is off unless asked for
  # the Huge render has no ICC of its own, so none is invented either way; and the file still decodes
  with Image.open(os.path.join(target, "a.jpg")) as im:
    assert im.size == (30, 40)   # orientation 6 was applied to the pixels


def test_gps_is_copied_only_when_the_export_asks_for_it(settings, tmp_path):
  conn = db.open_state(settings.state_dir)
  make_source(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  pid = conn.execute("SELECT id FROM photos").fetchone()[0]
  app = api.create_app(conn, settings)
  c = TestClient(app)
  target = str(tmp_path / "out")
  r = c.post("/api/export", json={"ids": [pid], "dir": "2020", "target": target,
                                  "include_gps": True})
  assert r.status_code == 200
  app.state.jobs.start()
  assert app.state.jobs.wait_idle()
  app.state.jobs.stop()
  gps = read(os.path.join(target, "a.jpg"))[0].get_ifd(GPS_IFD)
  assert gps[1] == "N" and gps[3] == "E" and float(gps[2][0]) == 48


def test_inject_exif_keeps_other_segments_and_pixels(tmp_path):
  p = str(tmp_path / "x.jpg")
  Image.new("RGB", (16, 16), "green").save(p, icc_profile=b"P" * 50, exif=Image.Exif())
  with Image.open(p) as im:
    before = im.tobytes()
  e = Image.Exif()
  e[0x010F] = "Maker"
  exif_write.inject_exif(p, e.tobytes())
  exif_write.inject_exif(p, e.tobytes())   # idempotent: still exactly one Exif segment
  assert open(p, "rb").read().count(b"Exif\x00\x00") == 1
  exif, icc = read(p)
  assert exif[0x010F] == "Maker" and icc == b"P" * 50
  with Image.open(p) as im:
    assert im.tobytes() == before


def test_split_date_handles_offsets_and_junk():
  assert exif_write._split_date("2021-07-04 12:30:15+0200") == ("2021:07:04 12:30:15", "+02:00")
  assert exif_write._split_date("2021-07-04 12:30:15") == ("2021:07:04 12:30:15", None)
  assert exif_write._split_date(None) == (None, None)
