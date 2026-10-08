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
