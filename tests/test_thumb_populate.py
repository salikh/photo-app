import os

import pytest
from PIL import Image

from photoapp import db
from photoapp import scan
from photoapp import thumb_populate
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.test_grouping import touch

REAL_DNG = os.environ.get("REAL_DNG")

needs_real_dng = pytest.mark.skipif(
    not REAL_DNG or not os.path.exists(REAL_DNG or ""), reason="REAL_DNG not set")
needs_dcraw = pytest.mark.skipif(
    not thumb_populate.dcraw_available(), reason="dcraw is not installed")


# --- dcraw wrappers, against a real DNG (same convention as tests/test_previews.py) ---------------

@needs_real_dng
@needs_dcraw
def test_extract_embedded_thumb_is_the_cameras_own_preview():
  img = thumb_populate.extract_embedded_thumb(REAL_DNG)
  assert img.mode == "RGB" and min(img.size) > 1000    # a real camera preview, not a tiny icon


@needs_real_dng
@needs_dcraw
def test_render_dcraw_half_size_is_smaller_than_full_size():
  half = thumb_populate.render_dcraw(REAL_DNG, half_size=True)
  full = thumb_populate.render_dcraw(REAL_DNG, half_size=False)
  assert half.mode == full.mode == "RGB"
  assert max(half.size) < max(full.size)
  assert max(full.size) > 4000                        # this library's cameras are ~16-24 MP


@needs_dcraw
def test_dcraw_error_on_a_file_that_is_not_a_raw(tmp_path):
  not_raw = str(tmp_path / "a.dng")            # a .dng extension but not real RAW data
  touch(not_raw)
  with pytest.raises(thumb_populate.DcrawError):
    thumb_populate.render_dcraw(not_raw, half_size=True)
  with pytest.raises(thumb_populate.DcrawError):
    thumb_populate.extract_embedded_thumb(not_raw)


def test_dcraw_not_installed_is_reported_clearly(monkeypatch):
  monkeypatch.setattr(thumb_populate, "dcraw_path", lambda: None)
  assert not thumb_populate.dcraw_available()
  with pytest.raises(thumb_populate.DcrawError, match="not installed"):
    thumb_populate.render_dcraw("/nonexistent.dng", half_size=True)


# --- populate_file, with dcraw stubbed out so this does not need the real binary -------------------

def fake_image(size):
  return Image.new("RGB", size, (10, 20, 30))


@pytest.fixture
def stub_dcraw(monkeypatch):
  """Deterministic stand-ins for the three dcraw calls, with call counts."""
  calls = {"embedded": 0, "half": 0, "full": 0}

  def embedded(source):
    calls["embedded"] += 1
    return fake_image((320, 240))

  def render(source, half_size):
    calls["half" if half_size else "full"] += 1
    return fake_image((2400, 1600) if half_size else (4800, 3200))

  monkeypatch.setattr(thumb_populate, "extract_embedded_thumb", embedded)
  monkeypatch.setattr(thumb_populate, "render_dcraw", render)
  monkeypatch.setattr(thumb_populate, "dcraw_available", lambda: True)
  return calls


def test_populate_file_dng_uses_one_decode_per_tier(conn, settings, stub_dcraw):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  made = thumb_populate.populate_file(conn, d, settings.thumbs_dir, fid, "a.DNG")
  assert sorted(made) == ["Huge", "Medium", "Small", "Thumb"]
  assert stub_dcraw == {"embedded": 1, "half": 1, "full": 1}     # one decode per tier, not per size
  u = thumbs.usage(conn)
  assert all(u[s]["files"] == 1 for s in thumbs.SIZES)
  sources = {r["size"]: r["source"] for r in conn.execute("SELECT size, source FROM thumbs")}
  assert sources == {"Thumb": "dcraw-embedded", "Small": "dcraw-half", "Medium": "dcraw-half",
                     "Huge": "dcraw-full"}
  # a real Thumb file was written at the expected long edge
  thumb = thumbs.lookup(settings.thumbs_dir, "Thumb", "a.DNG")
  assert Image.open(thumb).size == (300, 225)                    # 320x240 scaled to a 300 long edge


def test_populate_file_skips_sizes_that_already_exist(conn, settings, stub_dcraw):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  make_jpeg(os.path.join(settings.thumbs_dir, "Thumb", "a.DNG.jpg"))   # pre-existing: must not be touched
  before = open(os.path.join(settings.thumbs_dir, "Thumb", "a.DNG.jpg"), "rb").read()
  made = thumb_populate.populate_file(conn, d, settings.thumbs_dir, fid, "a.DNG")
  assert "Thumb" not in made and sorted(made) == ["Huge", "Medium", "Small"]
  assert stub_dcraw["embedded"] == 0                              # never asked to extract it
  assert open(os.path.join(settings.thumbs_dir, "Thumb", "a.DNG.jpg"), "rb").read() == before
  assert thumb_populate.populate_file(conn, d, settings.thumbs_dir, fid, "a.DNG") == []  # idempotent


def test_populate_file_falls_back_to_half_size_when_embedded_extraction_fails(conn, settings, monkeypatch):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  calls = {"half": 0}

  def embedded(source):
    raise thumb_populate.DcrawError("no embedded preview in this file")

  def render(source, half_size):
    calls["half"] += half_size
    return fake_image((2400, 1600))

  monkeypatch.setattr(thumb_populate, "extract_embedded_thumb", embedded)
  monkeypatch.setattr(thumb_populate, "render_dcraw", render)
  made = thumb_populate.populate_file(conn, d, settings.thumbs_dir, fid, "a.DNG")
  assert "Thumb" in made and calls["half"] == 1                   # Thumb came from the half-size decode instead


def test_populate_file_non_raw_uses_pillow_not_dcraw(conn, settings, stub_dcraw):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"), size=(3000, 2000))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  made = thumb_populate.populate_file(conn, d, settings.thumbs_dir, fid, "a.jpg")
  assert sorted(made) == ["Huge", "Medium", "Small", "Thumb"]
  assert stub_dcraw == {"embedded": 0, "half": 0, "full": 0}


def test_populate_file_reports_dcraw_missing_clearly(conn, settings, monkeypatch):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.DNG"))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  monkeypatch.setattr(thumb_populate, "dcraw_path", lambda: None)
  with pytest.raises(thumb_populate.DcrawError, match="not installed"):
    thumb_populate.populate_file(conn, d, settings.thumbs_dir, fid, "a.DNG")


# --- find_missing_files -----------------------------------------------------------------------

def test_find_missing_files(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "b.jpg"))
  touch(os.path.join(d, "c.dng"))
  scan.scan(conn, d)
  fid_a, fid_b, fid_c = (conn.execute("SELECT id FROM files WHERE path = ?", (p,)).fetchone()[0]
                         for p in ("a.jpg", "b.jpg", "c.dng"))
  dummy = os.path.join(d, "dummy-thumb.jpg")
  make_jpeg(dummy)                                                # thumbs.record stats a real file
  for size in thumbs.SIZES:
    thumbs.record(conn, fid_a, size, dummy, "existing")            # a: fully done
  thumbs.record(conn, fid_b, "Thumb", dummy, "existing")           # b: only Thumb done
  missing = {r["id"]: r["path"] for r in thumb_populate.find_missing_files(conn)}
  assert fid_a not in missing
  assert missing[fid_b] == "b.jpg" and missing[fid_c] == "c.dng"
  assert len(thumb_populate.find_missing_files(conn, limit=1)) == 1


# --- Populator: end to end with a real running queue, dcraw stubbed --------------------------------

def test_populator_processes_queued_files_one_at_a_time(settings, stub_dcraw):
  d = settings.pictures_dir
  for n in ("a", "b", "c"):
    touch(os.path.join(d, f"{n}.DNG"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  pop = thumb_populate.Populator(settings.db_path, d, settings.thumbs_dir)
  assert pop.enqueue_missing(conn) == 3
  assert pop.enqueue_missing(conn) == 3        # still lacking thumbnails (nothing has run yet)
  assert len(pop.queue.list()) == 3            # ... but no duplicate job rows (JobQueue.enqueue dedupes)
  pop.start()
  assert pop.queue.wait_idle(10)
  pop.stop()
  states = [j["state"] for j in pop.queue.list()]
  assert states.count("done") == 3
  u = thumbs.usage(conn)
  assert all(u[s]["files"] == 3 for s in thumbs.SIZES)


def test_populator_reports_a_failing_file_and_continues(settings, monkeypatch):
  d = settings.pictures_dir
  touch(os.path.join(d, "bad.DNG"))
  make_jpeg(os.path.join(d, "good.jpg"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)

  def embedded(source):
    raise thumb_populate.DcrawError("simulated failure")

  def render(source, half_size):
    raise thumb_populate.DcrawError("simulated failure")

  monkeypatch.setattr(thumb_populate, "extract_embedded_thumb", embedded)
  monkeypatch.setattr(thumb_populate, "render_dcraw", render)
  monkeypatch.setattr(thumb_populate, "dcraw_available", lambda: True)
  pop = thumb_populate.Populator(settings.db_path, d, settings.thumbs_dir)
  pop.enqueue_missing(conn)
  pop.start()
  assert pop.queue.wait_idle(10)
  pop.stop()
  by_state = {j["state"]: j for j in pop.queue.list()}
  assert by_state["failed"]["error"] and "simulated failure" in by_state["failed"]["error"]
  assert by_state["done"]
  assert thumbs.usage(conn)["Thumb"]["files"] == 1               # the good file still got done
