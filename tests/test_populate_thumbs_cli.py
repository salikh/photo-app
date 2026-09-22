import os
import subprocess
import sys

from PIL import Image

from photoapp import db
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan import build_years


def run(args, timeout=60):
  return subprocess.run(
      [sys.executable, "-m", "photoapp.populate_thumbs", "--logtostderr",
       "--report_seconds=1"] + args,
      capture_output=True, text=True, timeout=timeout,
      cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_populate_thumbs_command_fills_gaps_and_reports_progress(tmp_path):
  pics, state, thumbs_dir = str(tmp_path / "pics"), str(tmp_path / "state"), str(tmp_path / "t")
  os.makedirs(pics)
  make_jpeg(os.path.join(pics, "a.jpg"), size=(3000, 2000))
  make_jpeg(os.path.join(pics, "b.jpg"), size=(3000, 2000))
  from photoapp import scan
  conn = db.open_state(state)
  scan.scan(conn, pics, thumbs_dir=thumbs_dir)
  conn.close()

  r = run([f"--pictures_dir={pics}", f"--state_dir={state}", f"--thumbs_dir={thumbs_dir}"])
  assert r.returncode == 0, r.stderr[-3000:]
  assert "queued 2 files needing a thumbnail" in r.stderr
  assert "populate_thumbs: " in r.stderr
  assert "done" in r.stderr

  conn = db.open_state(state)
  u = thumbs.usage(conn)
  assert all(u[s]["files"] == 2 for s in thumbs.SIZES)
  for size in thumbs.SIZES:
    for name in ("a.jpg", "b.jpg"):
      assert os.path.exists(thumbs.thumb_path(thumbs_dir, size, name))

  # rerun: nothing left to do, exits promptly and touches nothing new
  before = {p: os.stat(os.path.join(dp, f)).st_mtime_ns
            for dp, _, fs in os.walk(thumbs_dir) for f in fs for p in [os.path.join(dp, f)]}
  r2 = run([f"--pictures_dir={pics}", f"--state_dir={state}", f"--thumbs_dir={thumbs_dir}"])
  assert r2.returncode == 0 and "queued 0 files" in r2.stderr
  after = {p: os.stat(p).st_mtime_ns for dp, _, fs in os.walk(thumbs_dir) for f in fs
          for p in [os.path.join(dp, f)]}
  assert before == after


def test_populate_thumbs_limit_flag(tmp_path):
  pics, state, thumbs_dir = str(tmp_path / "pics"), str(tmp_path / "state"), str(tmp_path / "t")
  os.makedirs(pics)
  for n in range(4):
    make_jpeg(os.path.join(pics, f"{n}.jpg"))
  from photoapp import scan
  conn = db.open_state(state)
  scan.scan(conn, pics, thumbs_dir=thumbs_dir)
  conn.close()

  r = run([f"--pictures_dir={pics}", f"--state_dir={state}", f"--thumbs_dir={thumbs_dir}", "--limit=2"])
  assert r.returncode == 0 and "queued 2 files needing a thumbnail" in r.stderr
  conn = db.open_state(state)
  assert thumbs.usage(conn)["Thumb"]["files"] == 2


def test_populate_thumbs_sizes_flag_restricts_what_is_made(tmp_path):
  pics, state, thumbs_dir = str(tmp_path / "pics"), str(tmp_path / "state"), str(tmp_path / "t")
  os.makedirs(pics)
  make_jpeg(os.path.join(pics, "a.jpg"), size=(3000, 2000))
  from photoapp import scan
  conn = db.open_state(state)
  scan.scan(conn, pics, thumbs_dir=thumbs_dir)
  conn.close()

  r = run([f"--pictures_dir={pics}", f"--state_dir={state}", f"--thumbs_dir={thumbs_dir}",
           "--sizes=Thumb,Medium"])
  assert r.returncode == 0
  assert os.path.exists(thumbs.thumb_path(thumbs_dir, "Thumb", "a.jpg"))
  assert os.path.exists(thumbs.thumb_path(thumbs_dir, "Medium", "a.jpg"))
  assert not os.path.exists(thumbs.thumb_path(thumbs_dir, "Small", "a.jpg"))
  assert not os.path.exists(thumbs.thumb_path(thumbs_dir, "Huge", "a.jpg"))

  bad = run([f"--pictures_dir={pics}", f"--state_dir={state}", f"--thumbs_dir={thumbs_dir}",
             "--sizes=Bogus"])
  assert bad.returncode != 0 and "must be a non-empty subset" in bad.stderr


def test_populate_thumbs_never_overwrites_an_existing_thumbnail(tmp_path):
  pics, state, thumbs_dir = str(tmp_path / "pics"), str(tmp_path / "state"), str(tmp_path / "t")
  os.makedirs(pics)
  make_jpeg(os.path.join(pics, "a.jpg"), size=(3000, 2000))
  os.makedirs(os.path.join(thumbs_dir, "Thumb"))
  make_jpeg(os.path.join(thumbs_dir, "Thumb", "a.jpg"), size=(11, 7))  # a deliberately odd existing thumb
  from photoapp import scan
  conn = db.open_state(state)
  scan.scan(conn, pics, thumbs_dir=thumbs_dir)
  conn.close()

  r = run([f"--pictures_dir={pics}", f"--state_dir={state}", f"--thumbs_dir={thumbs_dir}"])
  assert r.returncode == 0
  assert Image.open(os.path.join(thumbs_dir, "Thumb", "a.jpg")).size == (11, 7)   # untouched
  conn = db.open_state(state)
  for size in ("Small", "Medium", "Huge"):
    assert os.path.exists(thumbs.thumb_path(thumbs_dir, size, "a.jpg"))
