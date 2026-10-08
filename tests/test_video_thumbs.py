"""Ticket 188: static video thumbnails."""
import os

import pytest
from PIL import Image

from photoapp import thumbs
from photoapp import video
from photoapp import video_thumbs
from tests.test_video_tools import make_clip, needs_ffmpeg


def test_frame_offsets():
  assert video_thumbs.frame_offsets(None) == [0.0]
  assert video_thumbs.frame_offsets(100) == [10.0, 25.0, 40.0, 60.0, 0.0]
  # very short clips never seek past the end, and duplicates collapse
  short = video_thumbs.frame_offsets(0.05)
  assert all(0 <= t <= 0.05 for t in short) and len(set(short)) == len(short)


def test_is_dark():
  assert video_thumbs.is_dark(Image.new("RGB", (8, 8), (0, 0, 0)))
  assert not video_thumbs.is_dark(Image.new("RGB", (8, 8), (90, 90, 90)))


def test_placeholder_when_ffmpeg_is_missing_is_not_recorded(tmp_path, conn):
  pics, th = str(tmp_path / "p"), str(tmp_path / "t")
  os.makedirs(pics)
  open(os.path.join(pics, "a.mp4"), "wb").write(b"junk")
  nothing = video.Tools("no-such-ffmpeg-xyz", "no-such-ffprobe-xyz")
  video.configure(nothing)
  try:
    path, source = thumbs.make(pics, th, "a.mp4", "Small")
    assert source == "placeholder" and Image.open(path).size == (1000, 562)
    assert "_placeholder" in path and not os.path.exists(thumbs.thumb_path(th, "Small", "a.mp4"))
    conn.execute("INSERT INTO files (path, mtime) VALUES ('a.mp4', 1)")
    fid = conn.execute("SELECT id FROM files").fetchone()[0]
    assert thumbs.ensure(conn, pics, th, fid, "a.mp4", "Small") is None
    assert conn.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0] == 0
  finally:
    video.configure(video.Tools())


@needs_ffmpeg
def test_real_clip_makes_clean_frames_in_every_size(tmp_path):
  pics, th = str(tmp_path / "p"), str(tmp_path / "t")
  os.makedirs(pics)
  make_clip(video.Tools(), os.path.join(pics, "a.mp4"), seconds=4)
  path, source = thumbs.make(pics, th, "a.mp4", "Medium")
  assert source == "ffmpeg" and path.endswith("Medium/a.mp4.jpg")
  assert Image.open(path).size == (160, 120)          # never upscaled
  small, src2 = thumbs.make(pics, th, "a.mp4", "Thumb")   # downscaled from the cached Medium
  assert src2 == "pillow" and Image.open(small).size == (160, 120)
  made = video_thumbs.render_all(pics, th, "a.mp4")
  assert set(made) == {"Small", "Huge"}
  assert thumbs.make(pics, th, "a.mp4", "Huge")[1] == "existing"


@needs_ffmpeg
def test_rotated_clip_gives_a_portrait_frame(tmp_path):
  pics, th = str(tmp_path / "p"), str(tmp_path / "t")
  os.makedirs(pics)
  t = video.Tools()
  t.ffmpeg_run(["-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10", "-pix_fmt",
                "yuv420p", "-y", os.path.join(pics, "base.mp4")])
  t.ffmpeg_run(["-display_rotation", "90", "-i", os.path.join(pics, "base.mp4"), "-c", "copy",
                "-y", os.path.join(pics, "r.mp4")])
  path, _ = thumbs.make(pics, th, "r.mp4", "Medium")
  w, h = Image.open(path).size
  assert h > w


@needs_ffmpeg
def test_a_black_start_is_skipped(tmp_path):
  pics, th = str(tmp_path / "p"), str(tmp_path / "t")
  os.makedirs(pics)
  # black for the first 6 s of 10, then colourful: the 10%/25%/40% frames are black, 60% is not
  video.Tools().ffmpeg_run([
      "-f", "lavfi", "-i", "color=black:size=160x120:rate=10:duration=6",
      "-f", "lavfi", "-i", "testsrc=duration=4:size=160x120:rate=10",
      "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]", "-pix_fmt", "yuv420p", "-y",
      os.path.join(pics, "b.mp4")])
  path, _ = thumbs.make(pics, th, "b.mp4", "Medium")
  assert not video_thumbs.is_dark(Image.open(path))


# ---- ticket 189: animated thumbnails --------------------------------------------------------

def test_segment_starts_are_slice_centres():
  starts = video_thumbs.segment_starts(80)             # 8 slices of 10 s, 1 s fragments
  assert starts == [4.5, 14.5, 24.5, 34.5, 44.5, 54.5, 64.5, 74.5]
  assert video_thumbs.segment_starts(8) == [] and video_thumbs.segment_starts(3) == []
  assert video_thumbs.segment_starts(None) == []
  assert all(0 <= t <= 8.9 for t in video_thumbs.segment_starts(9))     # never past the end


def test_anim_command_shape():
  cmd = video_thumbs.anim_command("/x/a.mp4", "/o/a.webm", 80, 300, 12)
  assert cmd.count("-ss") == 8 and cmd.count("-i") == 8 and "concat=n=8:v=1:a=0[out]" in " ".join(cmd)
  assert "libvpx-vp9" in cmd and "-an" in cmd and cmd[-2:] == ["-y", "/o/a.webm"]
  short = video_thumbs.anim_command("/x/a.mp4", "/o/a.webm", 3, 300, 12)
  assert short.count("-i") == 1 and "-filter_complex" not in short


def test_anim_paths_clear_and_move(tmp_path, conn):
  th = str(tmp_path)
  for size in thumbs.ANIM_SIZES:
    p = thumbs.anim_path(th, size, "d/a.mp4")
    os.makedirs(os.path.dirname(p)); open(p, "wb").write(b"x")
  conn.execute("INSERT INTO files (path, mtime) VALUES ('d/a.mp4', 1)")
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  thumbs.record(conn, fid, "AnimThumb", thumbs.anim_path(th, "AnimThumb", "d/a.mp4"), "anim-v1")
  assert thumbs.move_thumbnails(conn, th, fid, "d/a.mp4", "d/b.mp4") == ["AnimThumb", "AnimSmall"]
  assert thumbs.anim_lookup(th, "AnimSmall", "d/b.mp4") and not thumbs.anim_lookup(th, "AnimSmall", "d/a.mp4")
  assert conn.execute("SELECT path FROM thumbs").fetchone()[0].endswith("AnimThumb/d/b.mp4.webm")
  assert thumbs.clear(th, conn, fid, "d/b.mp4") == ["AnimThumb", "AnimSmall"]
  assert not thumbs.anim_lookup(th, "AnimThumb", "d/b.mp4")


def test_make_anim_without_ffmpeg_returns_none(tmp_path):
  none = video.Tools("no-such-ffmpeg-xyz", "no-such-ffprobe-xyz")
  assert video_thumbs.make_anim(str(tmp_path), str(tmp_path / "t"), "a.mp4", "AnimThumb", none) is None


def probe_stream(path):
  info = video.Tools().probe(path)
  v = next(s for s in info["streams"] if s["codec_type"] == "video")
  return v, info


@needs_ffmpeg
def test_make_anim_long_clip_is_eight_seconds_vp9_without_audio(tmp_path):
  pics, th = str(tmp_path / "p"), str(tmp_path / "t")
  os.makedirs(pics)
  make_clip(video.Tools(), os.path.join(pics, "a.mp4"), seconds=20)
  path = video_thumbs.make_anim(pics, th, "a.mp4", "AnimThumb")
  assert path.endswith("AnimThumb/a.mp4.webm") and not os.path.exists(path + ".tmp")
  v, info = probe_stream(path)
  assert v["codec_name"] == "vp9" and (v["width"], v["height"]) == (160, 120)   # never upscaled
  assert all(s["codec_type"] == "video" for s in info["streams"])
  assert 7 < float(info["format"]["duration"]) < 9


@needs_ffmpeg
def test_make_anim_short_clip_is_played_whole_and_downscaled(tmp_path):
  pics, th = str(tmp_path / "p"), str(tmp_path / "t")
  os.makedirs(pics)
  video.Tools().ffmpeg_run(["-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=25",
                            "-pix_fmt", "yuv420p", "-y", os.path.join(pics, "s.mp4")])
  path = video_thumbs.make_anim(pics, th, "s.mp4", "AnimThumb")
  v, info = probe_stream(path)
  assert (v["width"], v["height"]) == (300, 168) and 2.5 < float(info["format"]["duration"]) < 3.6


@needs_ffmpeg
def test_make_anim_of_a_broken_file_returns_none(tmp_path):
  pics = str(tmp_path / "p")
  os.makedirs(pics)
  open(os.path.join(pics, "bad.mp4"), "wb").write(b"not a video")
  assert video_thumbs.make_anim(pics, str(tmp_path / "t"), "bad.mp4", "AnimSmall") is None


# ---- ticket 201: AnimSmall is representative of the whole video -----------------------------------

def test_anim_small_is_the_whole_clip_up_to_a_minute_else_five_15s_fragments():
  long_edge, fps, n, length, whole = video_thumbs.ANIM_SPEC["AnimSmall"]
  assert (n, length, whole) == (5, 15.0, 60.0)
  assert video_thumbs.segment_starts(45, n, length, whole) == []                 # whole
  assert video_thumbs.segment_starts(60, n, length, whole) == []
  starts = video_thumbs.segment_starts(300, n, length, whole)                    # 5 slices of 60 s
  assert starts == [22.5, 82.5, 142.5, 202.5, 262.5]
  # 60-75 s: five 15 s fragments would overlap, so they shrink to tile the clip
  assert video_thumbs.fragment_length(64, n, length) == pytest.approx(12.8)
  tiled = video_thumbs.segment_starts(64, n, length, whole)
  assert tiled[0] == 0 and tiled[-1] == pytest.approx(64 - 12.8, abs=0.01)
  cmd = video_thumbs.anim_command("/x/a.mp4", "/o/a.webm", 300, 640, 15, n, length, whole)
  assert cmd.count("-ss") == 5 and cmd.count("15.000") == 5
  whole_cmd = video_thumbs.anim_command("/x/a.mp4", "/o/a.webm", 45, 640, 15, n, length, whole)
  assert whole_cmd.count("-i") == 1 and whole_cmd[:2] == ["-t", "60"]


def test_anim_thumb_recipe_is_unchanged():
  assert video_thumbs.ANIM_SPEC["AnimThumb"] == (300, 12, 8, 1.0, 8.0)
  assert video_thumbs.segment_starts(80) == [4.5, 14.5, 24.5, 34.5, 44.5, 54.5, 64.5, 74.5]


def test_invalidate_stale_removes_old_recipe_anim_small_only(tmp_path, conn):
  th = str(tmp_path)
  conn.execute("INSERT INTO files (path, mtime) VALUES ('a.mp4', 1), ('b.mp4', 1)")
  ids = [r[0] for r in conn.execute("SELECT id FROM files ORDER BY path")]
  made = {}
  for fid, name, size, source in ((ids[0], "a.mp4", "AnimSmall", "anim-v1"),     # stale
                                  (ids[0], "a.mp4", "AnimThumb", "anim-v1"),     # current: kept
                                  (ids[1], "b.mp4", "AnimSmall", "anim-v2")):    # current: kept
    p = thumbs.anim_path(th, size, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"x")
    thumbs.record(conn, fid, size, p, source)
    made[(name, size)] = p
  assert video_thumbs.invalidate_stale(conn, th) == 1
  assert not os.path.exists(made[("a.mp4", "AnimSmall")])
  assert os.path.exists(made[("a.mp4", "AnimThumb")]) and os.path.exists(made[("b.mp4", "AnimSmall")])
  rows = {(r["file_id"], r["size"]) for r in conn.execute("SELECT file_id, size FROM thumbs")}
  assert rows == {(ids[0], "AnimThumb"), (ids[1], "AnimSmall")}
  assert video_thumbs.invalidate_stale(conn, th) == 0                            # idempotent


@needs_ffmpeg
def test_make_anim_small_of_a_long_clip_is_five_fragments(tmp_path):
  pics, th = str(tmp_path / "p"), str(tmp_path / "t")
  os.makedirs(pics)
  make_clip(video.Tools(), os.path.join(pics, "a.mp4"), seconds=100)
  path = video_thumbs.make_anim(pics, th, "a.mp4", "AnimSmall")
  v, info = probe_stream(path)
  assert 72 < float(info["format"]["duration"]) < 78                              # 5 x 15 s
  short = os.path.join(pics, "s.mp4")
  make_clip(video.Tools(), short, seconds=20)
  path = video_thumbs.make_anim(pics, th, "s.mp4", "AnimSmall")
  assert 19 < float(probe_stream(path)[1]["format"]["duration"]) < 21            # the whole clip
