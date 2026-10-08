import os

import pytest
from PIL import Image

from photoapp import db
from photoapp import previews
from photoapp import raw_settings
from photoapp import scan
from photoapp import thumb_populate
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.test_grouping import touch


def fake_image(size):
  return Image.new("RGB", size, (10, 20, 30))


@pytest.fixture
def stub_raw(monkeypatch):
  """Deterministic stand-ins for the two rawpy entry points, with call counts -- this ticket's
  (090's resolution to 085) merged renderer, replacing the old dcraw subprocess calls."""
  calls = {"embedded": 0, "demosaic": 0}

  def embedded(path):
    calls["embedded"] += 1
    return fake_image((1600, 1200))

  def render(path, settings=None, half_size=False):
    calls["demosaic"] += 1
    return fake_image((800, 600) if half_size else (1600, 1200))

  monkeypatch.setattr(previews, "embedded_preview", embedded)
  monkeypatch.setattr(previews, "render", render)
  return calls


def scanned_raw(conn, settings, name="a.DNG"):
  touch(os.path.join(settings.pictures_dir, name))
  scan.scan(conn, settings.pictures_dir)
  return conn.execute("SELECT id FROM files WHERE path = ?", (name,)).fetchone()[0]


# --- populate_file: unified renderer (ticket 085/090), no more dcraw --------------------------

def test_populate_file_raw_at_default_settings_uses_the_embedded_preview(conn, settings, stub_raw):
  fid = scanned_raw(conn, settings)
  made = thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.DNG")
  assert sorted(made) == ["Huge", "Medium", "Small", "Thumb"]
  assert stub_raw == {"embedded": 4, "demosaic": 0}     # every size from the cheap shortcut
  u = thumbs.usage(conn)
  assert all(u[s]["files"] == 1 for s in thumbs.SIZES)
  thumb = thumbs.lookup(settings.thumbs_dir, "Thumb", "a.DNG")
  assert Image.open(thumb).size == (300, 225)           # 1600x1200 scaled to a 300 long edge


def test_populate_file_raw_with_tuned_settings_demosaics_every_size(conn, settings, stub_raw):
  fid = scanned_raw(conn, settings)
  raw_settings.set(conn, fid, bright=1.4)
  made = thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.DNG")
  assert sorted(made) == ["Huge", "Medium", "Small", "Thumb"]
  # ticket 093: the first size demosaics once at Huge; every other size downscales from that
  # cached Huge instead of demosaicing again -- no size takes the free embedded-preview shortcut
  # once tuned, but only one demosaic happens for the whole file, not one per size.
  assert stub_raw == {"embedded": 0, "demosaic": 1}


def test_populate_file_skips_sizes_that_already_exist(conn, settings, stub_raw):
  fid = scanned_raw(conn, settings)
  make_jpeg(os.path.join(settings.thumbs_dir, "Thumb", "a.DNG.jpg"))   # pre-existing: must not be touched
  before = open(os.path.join(settings.thumbs_dir, "Thumb", "a.DNG.jpg"), "rb").read()
  made = thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.DNG")
  assert "Thumb" not in made and sorted(made) == ["Huge", "Medium", "Small"]
  # each remaining size (not Thumb, already cached) independently takes the embedded-preview
  # shortcut -- one call per size. Ticket 093's demosaic-once sharing only applies to the
  # expensive tuned-settings path (see test_populate_file_raw_with_tuned_settings_demosaics_
  # every_size above); the embedded-preview shortcut used here is already cheap, so it isn't
  # worth sharing across sizes.
  assert stub_raw == {"embedded": 3, "demosaic": 0}
  assert open(os.path.join(settings.thumbs_dir, "Thumb", "a.DNG.jpg"), "rb").read() == before
  made_again = thumb_populate.populate_file(
      conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.DNG")
  assert made_again == []   # idempotent


def test_populate_file_non_raw_uses_pillow_not_rawpy(conn, settings, stub_raw):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"), size=(3000, 2000))
  scan.scan(conn, d)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  made = thumb_populate.populate_file(conn, d, settings.thumbs_dir, fid, "a.jpg")
  assert sorted(made) == ["Huge", "Medium", "Small", "Thumb"]
  assert stub_raw == {"embedded": 0, "demosaic": 0}


def test_populate_file_raw_with_no_usable_preview_and_default_settings_fails_gracefully(
    conn, settings):
  # No stub: touch() makes a file rawpy genuinely cannot decode -- matches test_previews.py's
  # own convention for the "undecodable" case, no REAL_DNG needed.
  fid = scanned_raw(conn, settings)
  made = thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.DNG")
  assert made == []


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


def test_find_missing_files_scoped_to_a_directory(conn, settings):
  # ticket 080: rel_dir restricts to files directly in that directory (not subdirectories),
  # matching what a folder's grid actually shows.
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "y", "a.jpg"))
  make_jpeg(os.path.join(d, "y", "sub", "b.jpg"))
  make_jpeg(os.path.join(d, "z", "c.jpg"))
  scan.scan(conn, d)
  paths = {r["path"] for r in thumb_populate.find_missing_files(conn, rel_dir="y")}
  assert paths == {"y/a.jpg"}   # not y/sub/b.jpg, not z/c.jpg
  assert {r["path"] for r in thumb_populate.find_missing_files(conn, rel_dir=".")} == set()


# --- Populator: end to end with a real running queue, rawpy stubbed ----------------------------

def test_populator_processes_queued_files_one_at_a_time(settings, stub_raw):
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


def test_populator_enqueue_missing_scoped_to_a_directory(settings, stub_raw):
  d = settings.pictures_dir
  touch(os.path.join(d, "y", "a.DNG"))
  touch(os.path.join(d, "z", "b.DNG"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  pop = thumb_populate.Populator(settings.db_path, d, settings.thumbs_dir)
  assert pop.enqueue_missing(conn, rel_dir="y") == 1
  jobs_seen = pop.queue.list()
  assert len(jobs_seen) == 1
  pathed = conn.execute("SELECT path FROM files WHERE id = ?",
                        (jobs_seen[0]["file_id"],)).fetchone()["path"]
  assert pathed == "y/a.DNG"


def test_populator_reports_a_failing_file_and_continues(settings, monkeypatch):
  d = settings.pictures_dir
  touch(os.path.join(d, "bad.DNG"))
  make_jpeg(os.path.join(d, "good.jpg"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)

  def embedded(path):
    return None   # simulated failure: no usable embedded preview, and bad.DNG isn't real RAW data

  monkeypatch.setattr(previews, "embedded_preview", embedded)
  pop = thumb_populate.Populator(settings.db_path, d, settings.thumbs_dir)
  pop.enqueue_missing(conn)
  pop.start()
  assert pop.queue.wait_idle(10)
  pop.stop()
  by_state = {j["state"]: j for j in pop.queue.list()}
  assert by_state["failed"]["error"]
  assert by_state["done"]
  assert thumbs.usage(conn)["Thumb"]["files"] == 1               # the good file still got done


# --- videos (ticket 190) ----------------------------------------------------------------------

from photoapp import video
from tests.test_video_tools import make_clip, needs_ffmpeg


def scanned_video(conn, settings, name="v.mp4", real=True):
  path = os.path.join(settings.pictures_dir, name)
  if real:
    os.makedirs(settings.pictures_dir, exist_ok=True)
    make_clip(video.Tools(), path, seconds=3)
  else:
    touch(path)
  scan.scan(conn, settings.pictures_dir)
  return conn.execute("SELECT id FROM files WHERE path = ?", (name,)).fetchone()[0]


@needs_ffmpeg
def test_populate_video_makes_stills_and_animations_then_is_complete(conn, settings):
  fid = scanned_video(conn, settings)
  assert [r["id"] for r in thumb_populate.find_missing_files(conn)] == [fid]
  made = thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "v.mp4")
  assert set(made) == set(thumbs.SIZES) | set(thumbs.ANIM_SIZES)
  sizes = {r["size"] for r in conn.execute("SELECT size FROM thumbs WHERE file_id = ?", (fid,))}
  assert sizes == set(made)
  assert thumb_populate.find_missing_files(conn) == []
  assert thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "v.mp4") == []


@needs_ffmpeg
def test_failed_video_is_recorded_and_not_requeued_until_retry_or_change(conn, settings):
  fid = scanned_video(conn, settings, real=False)
  made = thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "v.mp4")
  assert made == []
  assert conn.execute("SELECT error FROM video_failures WHERE file_id = ?", (fid,)).fetchone()
  assert thumb_populate.find_missing_files(conn) == []
  later = 8 * 86400 + __import__("time").time()
  assert [r["id"] for r in thumb_populate.find_missing_files(conn, now=later)] == [fid]
  conn.execute("UPDATE files SET mtime = mtime + 1 WHERE id = ?", (fid,))      # the file changed
  assert [r["id"] for r in thumb_populate.find_missing_files(conn)] == [fid]
  thumbs.clear(settings.thumbs_dir, conn, fid, "v.mp4")                         # forced re-render
  assert conn.execute("SELECT COUNT(*) FROM video_failures").fetchone()[0] == 0


def test_videos_are_not_queued_without_ffmpeg(conn, settings):
  fid = scanned_video(conn, settings, real=False)
  video.configure(video.Tools("no-such-ffmpeg-xyz", "no-such-ffprobe-xyz"))
  try:
    assert thumb_populate.find_missing_files(conn) == []
  finally:
    video.configure(video.Tools())
  assert [r["id"] for r in thumb_populate.find_missing_files(conn)] == [fid]


@needs_ffmpeg
def test_populator_remakes_anim_small_made_with_an_old_recipe(conn, settings):
  fid = scanned_video(conn, settings)
  thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "v.mp4")
  conn.execute("UPDATE thumbs SET source = 'anim-v1' WHERE file_id = ? AND size = 'AnimSmall'", (fid,))
  conn.commit()
  pop = thumb_populate.Populator(settings.db_path, settings.pictures_dir, settings.thumbs_dir)
  assert pop.enqueue_missing(conn) == 1                                           # stale one removed, requeued
  assert thumbs.anim_lookup(settings.thumbs_dir, "AnimSmall", "v.mp4") is None
  assert thumbs.anim_lookup(settings.thumbs_dir, "AnimThumb", "v.mp4")
  thumb_populate.populate_file(conn, settings.pictures_dir, settings.thumbs_dir, fid, "v.mp4")
  src = conn.execute("SELECT source FROM thumbs WHERE file_id = ? AND size = 'AnimSmall'", (fid,)).fetchone()[0]
  assert src == "anim-v2"
