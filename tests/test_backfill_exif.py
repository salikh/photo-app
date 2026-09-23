import os
import subprocess
import sys

from PIL import Image

from photoapp import backfill_exif
from photoapp import db
from photoapp import scan
from tests.conftest import make_jpeg


def make_exif_jpeg(path, aperture=2.8, shutter=(1, 250), iso=400, size=(30, 20)):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  img = Image.new("RGB", size)
  exif = img.getexif()
  sub = exif.get_ifd(0x8769)
  if aperture is not None:
    sub[0x829D] = aperture         # FNumber
  if shutter is not None:
    sub[0x829A] = shutter          # ExposureTime
  if iso is not None:
    sub[0x8827] = iso              # ISOSpeedRatings
  img.save(path, exif=exif)


def _clear_camera_metadata(conn):
  """Simulate a files row that predates ticket 083 (scanned, but never had EXIF camera fields
  extracted) -- a real scan today already populates them, so tests undo that to exercise the
  backfill path specifically."""
  conn.execute("UPDATE files SET aperture = NULL, shutter_speed = NULL, iso = NULL")
  conn.commit()


def test_find_unbackfilled_lists_only_files_missing_all_three(conn, settings):
  d = settings.pictures_dir
  make_exif_jpeg(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "b.jpg"))   # no EXIF at all
  scan.scan(conn, d)
  _clear_camera_metadata(conn)
  rows = {r["path"] for r in backfill_exif.find_unbackfilled(conn)}
  assert rows == {"a.jpg", "b.jpg"}


def test_backfill_populates_camera_metadata(conn, settings):
  d = settings.pictures_dir
  make_exif_jpeg(os.path.join(d, "a.jpg"), aperture=4.0, shutter=(1, 100), iso=800)
  make_jpeg(os.path.join(d, "b.jpg"))   # genuinely no EXIF
  scan.scan(conn, d)
  _clear_camera_metadata(conn)

  updated, unchanged, errors = backfill_exif.backfill(conn, d)
  assert (updated, unchanged, errors) == (1, 1, 0)
  a = conn.execute(
      "SELECT aperture, shutter_speed, iso FROM files WHERE path = 'a.jpg'").fetchone()
  assert a["aperture"] == 4.0 and a["shutter_speed"] == 0.01 and a["iso"] == 800
  b = conn.execute(
      "SELECT aperture, shutter_speed, iso FROM files WHERE path = 'b.jpg'").fetchone()
  assert (b["aperture"], b["shutter_speed"], b["iso"]) == (None, None, None)


def test_backfill_does_not_touch_already_populated_files(conn, settings):
  d = settings.pictures_dir
  make_exif_jpeg(os.path.join(d, "a.jpg"), aperture=4.0, shutter=(1, 100), iso=800)
  scan.scan(conn, d)   # already populated by the scan itself (ticket 083)
  before = conn.execute(
      "SELECT aperture FROM files WHERE path = 'a.jpg'").fetchone()
  assert before["aperture"] == 4.0   # sanity: the scan already did the work

  updated, unchanged, errors = backfill_exif.backfill(conn, d)
  assert (updated, unchanged, errors) == (0, 0, 0)   # nothing needed backfilling


def test_backfill_respects_limit(conn, settings):
  d = settings.pictures_dir
  for n in range(3):
    make_exif_jpeg(os.path.join(d, f"{n}.jpg"))
  scan.scan(conn, d)
  _clear_camera_metadata(conn)
  updated, unchanged, errors = backfill_exif.backfill(conn, d, limit=2)
  assert updated == 2


def run_cli(args, timeout=60):
  return subprocess.run(
      [sys.executable, "-m", "photoapp.backfill_exif", "--logtostderr"] + args,
      capture_output=True, text=True, timeout=timeout,
      cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_cli_backfills_and_is_idempotent(tmp_path):
  pics, state = str(tmp_path / "pics"), str(tmp_path / "state")
  os.makedirs(pics)
  make_exif_jpeg(os.path.join(pics, "a.jpg"), aperture=5.6, shutter=(1, 60), iso=200)
  conn = db.open_state(state)
  scan.scan(conn, pics)
  _clear_camera_metadata(conn)
  conn.close()

  r = run_cli([f"--pictures_dir={pics}", f"--state_dir={state}"])
  assert r.returncode == 0, r.stderr[-2000:]
  assert "1 updated" in r.stderr

  conn = db.open_state(state)
  row = conn.execute("SELECT aperture, shutter_speed, iso FROM files WHERE path = 'a.jpg'").fetchone()
  assert row["aperture"] == 5.6 and abs(row["shutter_speed"] - 1 / 60) < 1e-9 and row["iso"] == 200

  r2 = run_cli([f"--pictures_dir={pics}", f"--state_dir={state}"])
  assert r2.returncode == 0
  assert "0 updated" in r2.stderr   # already backfilled, nothing left to do
