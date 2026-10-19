"""Ticket 248: ffmpeg two-pass stabilization."""
import os

import pytest

from photoapp import video
from photoapp import video_stabilize as vs
from tests.test_video_tools import make_clip, needs_ffmpeg


def test_levels_resolve_on_top_of_the_base_and_overrides_are_validated():
  strong, weak = vs.resolve({"level": "strong"}), vs.resolve({"level": "weak"})
  assert strong["smoothing"] > weak["smoothing"] and strong["crf"] == 23
  assert vs.resolve({})["level"] == vs.DEFAULT_LEVEL
  assert vs.resolve({"level": "weak", "smoothing": "45"})["smoothing"] == 45
  for bad in ({"level": "nope"}, {"bogus": 1}, {"smoothing": 9999}, {"crop": "x;y"},
              {"smoothing": "abc"}):
    with pytest.raises(vs.StabilizeError):
      vs.resolve(bad)


def test_description_is_one_readable_line_with_the_parameters():
  text = vs.describe({"level": "strong", "smoothing": 40})
  assert "\n" not in text and text.startswith("stabilized (strong):")
  assert "smoothing=40" in text and "crf=23" in text and "AAC" in text


def test_output_name_keeps_the_directory_and_replaces_the_extension():
  assert vs.output_name("a/b/clip.MOV", "strong") == "a/b/clip.stabilized-strong.mp4"
  assert vs.is_stabilized_name("a/clip.stabilized-weak.mp4") and not vs.is_stabilized_name("a/c.mp4")
  with pytest.raises(vs.StabilizeError):
    vs.output_name("a.mp4", "../x")


def test_commands_are_two_passes_sharing_the_transforms_file():
  detect, transform = vs.commands("in.mp4", "out.mp4.part", "/t/x.trf", {"level": "weak"}, "note")
  assert "vidstabdetect=result=/t/x.trf" in detect[detect.index("-vf") + 1]
  vf = transform[transform.index("-vf") + 1]
  assert vf.startswith("vidstabtransform=input=/t/x.trf") and "smoothing=10" in vf
  assert "libx264" in transform and "+faststart" in transform and "comment=note" in transform
  assert transform[-1] == "out.mp4.part"


def test_filter_paths_are_escaped():
  assert vs._filter_arg("/a b/c:d'e") == "/a b/c\\:d\\'e"


def test_unavailable_ffmpeg_is_an_error_not_a_crash(tmp_path):
  nothing = video.Tools("no-such-ffmpeg-xyz", "no-such-ffprobe-xyz")
  assert not vs.available(nothing)
  with pytest.raises(vs.StabilizeError):
    vs.stabilize(nothing, "a.mp4", str(tmp_path / "o.mp4"), {})


needs_vidstab = pytest.mark.skipif(not vs.available(video.Tools()),
                                   reason="ffmpeg without libvidstab/libx264")


@needs_ffmpeg
@needs_vidstab
def test_stabilize_writes_a_web_playable_mp4_atomically(tmp_path):
  tools = video.Tools()
  src, dest = str(tmp_path / "in.mkv"), str(tmp_path / "in.stabilized-weak.mp4")
  make_clip(tools, src, seconds=2)
  steps = []
  note = vs.stabilize(tools, src, dest, {"level": "weak"}, duration=2, progress=steps.append)
  assert os.path.isfile(dest) and not os.path.exists(dest + ".part")
  assert [s[:6] for s in steps] == ["pass 1", "pass 2"]
  info = tools.probe(dest)
  vstream = next(s for s in info["streams"] if s["codec_type"] == "video")
  assert vstream["codec_name"] == "h264" and vstream["pix_fmt"] == "yuv420p"
  assert (vstream["width"], vstream["height"]) == (160, 120)
  assert note in info["format"]["tags"]["comment"]
  first = os.stat(dest).st_mtime_ns
  vs.stabilize(tools, src, dest, {"level": "weak"}, duration=2)      # a re-run replaces it
  assert os.stat(dest).st_mtime_ns >= first


@needs_ffmpeg
@needs_vidstab
def test_failed_run_leaves_nothing_behind(tmp_path):
  tools = video.Tools()
  src, dest = str(tmp_path / "bad.mp4"), str(tmp_path / "bad.stabilized-weak.mp4")
  open(src, "wb").write(b"not a video")
  with pytest.raises(vs.StabilizeError):
    vs.stabilize(tools, src, dest, {"level": "weak"})
  assert os.listdir(tmp_path) == ["bad.mp4"]
