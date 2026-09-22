import dataclasses
import os
import subprocess
import sys

from photoapp import curation
from photoapp import scan
from photoapp import sync_sidecars
from photoapp import xmp
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan_sidecars import XMP, write


def read(settings, rel):
  return open(os.path.join(settings.pictures_dir, rel), "rb").read()


def setup_disagreeing_pair(conn, settings, rating_dng=1, rating_jpg=3):
  """A DNG+JPG Photo whose sidecars disagree (photos.conflict), tied on mtime so the DNG
  sidecar wins (matches test_curation.py's setup_pair)."""
  d = settings.pictures_dir
  touch(os.path.join(d, "y", "K1.DNG"))
  make_jpeg(os.path.join(d, "y", "K1.JPG"))
  write(os.path.join(d, "y", "K1.DNG.xmp"), XMP % (rating_dng, ""), mtime=1_000_000)
  write(os.path.join(d, "y", "K1.JPG.xmp"), XMP % (rating_jpg, ""), mtime=1_000_000)
  scan.scan(conn, d)
  return conn.execute("SELECT photo_id FROM files WHERE path = 'y/K1.DNG'").fetchone()[0]


# ---- curation.sync_sidecars ----

def test_sync_sidecars_rewrites_disagreeing_sidecars_without_logging_a_change(conn, settings):
  pid = setup_disagreeing_pair(conn, settings)
  assert curation.photo_state(conn, pid)["conflict"]
  r = curation.sync_sidecars(conn, settings, pid)
  assert xmp.parse(read(settings, "y/K1.DNG.xmp")).rating == 1
  assert xmp.parse(read(settings, "y/K1.JPG.xmp")).rating == 1   # now matches
  state = curation.photo_state(conn, pid)
  assert state["rating"] == 1 and not state["conflict"]
  assert r["changes"] == {}                    # nothing actually changed, just caught up
  assert curation.recent_activity(conn) == []   # so nothing is logged / undo-able


def test_sync_sidecars_respects_xmp_dry_run(conn, settings):
  pid = setup_disagreeing_pair(conn, settings)
  dry = dataclasses.replace(settings, xmp_dry_run=True)
  before = read(settings, "y/K1.JPG.xmp")
  curation.sync_sidecars(conn, dry, pid)
  assert read(settings, "y/K1.JPG.xmp") == before
  assert curation.photo_state(conn, pid)["conflict"]


# ---- sync_sidecars.find_conflicting / preview / run ----

def test_find_conflicting_lists_only_conflicting_photos(conn, settings):
  pid = setup_disagreeing_pair(conn, settings)
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "y", "a.jpg"))
  write(os.path.join(d, "y", "a.jpg.xmp"), XMP % (2, ""), mtime=1_000_000)   # in sync, alone
  scan.scan(conn, d)

  assert [r["path"] for r in sync_sidecars.find_conflicting(conn)] == ["y/K1.DNG"]


def test_preview_shows_resolved_and_each_sidecars_current_value(conn, settings):
  setup_disagreeing_pair(conn, settings)
  rows = sync_sidecars.preview(conn)
  assert len(rows) == 1
  r = rows[0]
  assert r["path"] == "y/K1.DNG" and r["resolved"] == {"rating": 1, "fav": False}
  assert {s["path"]: s["rating"] for s in r["sidecars"]} == {
      "y/K1.DNG.xmp": 1, "y/K1.JPG.xmp": 3}
  assert "1 Photo(s) with conflicting sidecars" in sync_sidecars.format_preview(rows)
  assert "<- differs" in sync_sidecars.format_preview(rows)   # the JPG sidecar (3 != resolved 1)


def test_run_writes_and_clears_conflict_without_touching_others(conn, settings):
  setup_disagreeing_pair(conn, settings)
  written, failed = sync_sidecars.run(conn, settings)
  assert written == 1 and failed == []
  assert xmp.parse(read(settings, "y/K1.JPG.xmp")).rating == 1
  assert sync_sidecars.find_conflicting(conn) == []


def test_run_respects_limit(conn, settings):
  d = settings.pictures_dir
  for n in range(3):
    touch(os.path.join(d, "y", f"K{n}.DNG"))
    make_jpeg(os.path.join(d, "y", f"K{n}.JPG"))
    write(os.path.join(d, "y", f"K{n}.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)
    write(os.path.join(d, "y", f"K{n}.JPG.xmp"), XMP % (3, ""), mtime=1_000_000)
  scan.scan(conn, d)
  assert len(sync_sidecars.find_conflicting(conn)) == 3

  written, failed = sync_sidecars.run(conn, settings, limit=2)
  assert written == 2 and failed == []
  assert len(sync_sidecars.find_conflicting(conn)) == 1   # one left untouched


# ---- CLI ----

def run_cli(args, timeout=60):
  return subprocess.run(
      [sys.executable, "-m", "photoapp.sync_sidecars", "--logtostderr"] + args,
      capture_output=True, text=True, timeout=timeout,
      cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_cli_preview_only_by_default(tmp_path):
  pics, state = str(tmp_path / "pics"), str(tmp_path / "state")
  os.makedirs(pics)
  touch(os.path.join(pics, "y", "K1.DNG"))
  make_jpeg(os.path.join(pics, "y", "K1.JPG"))
  write(os.path.join(pics, "y", "K1.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)
  write(os.path.join(pics, "y", "K1.JPG.xmp"), XMP % (3, ""), mtime=1_000_000)
  from photoapp import db
  conn = db.open_state(state)
  scan.scan(conn, pics)
  conn.close()

  r = run_cli([f"--pictures_dir={pics}", f"--state_dir={state}"])
  assert r.returncode == 0, r.stderr[-2000:]
  assert "1 Photo(s) with conflicting sidecars" in r.stdout
  assert "preview only" in r.stdout
  assert xmp.parse(open(os.path.join(pics, "y", "K1.JPG.xmp"), "rb").read()).rating == 3  # untouched

  r2 = run_cli([f"--pictures_dir={pics}", f"--state_dir={state}", "--yes"])
  assert r2.returncode == 0, r2.stderr[-2000:]
  assert "wrote 1 Photo(s)" in r2.stdout
  assert xmp.parse(open(os.path.join(pics, "y", "K1.JPG.xmp"), "rb").read()).rating == 1
