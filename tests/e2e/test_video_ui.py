"""Browser tests for video support (tickets 192, 193): play overlay, hover animation, loupe player."""

import json
import os
import urllib.request

import pytest
from playwright.sync_api import expect

from photoapp import db
from photoapp import thumb_populate
from photoapp import video
from tests.e2e.test_ui import wait_for

pytestmark = pytest.mark.skipif(not video.Tools().available(), reason="ffmpeg not installed")


def api(server, path):
  return json.load(urllib.request.urlopen(server.url + path))


@pytest.fixture
def vserver(server):
  """The standard library plus a playable mp4 and an AVI (not playable in Chrome) in 'clips/'."""
  t = video.Tools()
  d = os.path.join(server.pictures, "clips")
  os.makedirs(d)
  t.ffmpeg_run(["-f", "lavfi", "-i", "testsrc=duration=3:size=320x180:rate=10", "-pix_fmt", "yuv420p",
                "-y", os.path.join(d, "a.mp4")])
  t.ffmpeg_run(["-f", "lavfi", "-i", "testsrc=duration=3:size=320x180:rate=10", "-c:v", "mpeg4",
                "-y", os.path.join(d, "b.avi")])
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  conn = db.open_state(server.settings.state_dir)
  for r in conn.execute("SELECT id, path FROM files WHERE path LIKE 'clips/%'").fetchall():
    thumb_populate.populate_file(conn, server.pictures, server.settings.thumbs_dir, r["id"], r["path"])
  conn.commit()
  conn.close()
  return server


def photos(server):
  return {p["name"]: p for p in api(server, "/api/photos?dir=clips&sort=name")["photos"]}


def test_grid_shows_play_overlay_and_duration_and_animates_on_hover(page, vserver):
  page.goto(vserver.url + "/#/clips")
  cells = page.locator(".cell.video")
  expect(cells).to_have_count(2)
  mp4 = page.locator(".cell.video", has=page.locator("img[alt='a.mp4']"))
  expect(mp4.locator("svg.play")).to_be_visible()
  expect(mp4.locator(".dur")).to_have_text("0:03")
  assert photos(vserver)["a.mp4"]["has_anim"]
  expect(mp4.locator("video.anim")).to_have_count(0)           # never autoplays
  mp4.hover(force=True)   # skips the stability check, a known Playwright artefact here
  expect(mp4.locator("video.anim")).to_have_count(1)
  expect(mp4).to_have_class(__import__("re").compile(r"animating"), timeout=10000)
  expect(mp4.locator("svg.play")).to_be_hidden()
  page.mouse.move(5, 5)
  expect(mp4.locator("video.anim")).to_have_count(0)
  expect(mp4.locator("svg.play")).to_be_visible()


def test_loupe_plays_the_original_and_hides_crop_and_rotate(page, vserver):
  pid = photos(vserver)["a.mp4"]["id"]
  page.goto(f"{vserver.url}/#/clips?photo={pid}")
  expect(page.locator(".loupe")).to_be_visible()
  player = page.locator("video.main-video")
  expect(player).to_be_visible()
  wait_for(lambda: page.evaluate("document.querySelector('video.main-video').readyState >= 1"))
  assert 2.5 < page.evaluate("document.querySelector('video.main-video').duration") < 3.5
  expect(page.get_by_role("button", name="crop")).to_have_count(0)
  expect(page.locator("button[title^='rotate left']")).to_have_count(0)
  # Space plays / pauses; the arrow keys still navigate
  page.keyboard.press(" ")
  wait_for(lambda: page.evaluate("!document.querySelector('video.main-video').paused"))
  page.keyboard.press(" ")
  wait_for(lambda: page.evaluate("document.querySelector('video.main-video').paused"))
  page.keyboard.press("ArrowRight")
  expect(page.locator(".hud .pos")).to_have_text("2/2")


def test_loupe_unplayable_video_falls_back_to_still_preview_and_download_link(page, vserver):
  pid = photos(vserver)["b.avi"]["id"]
  page.goto(f"{vserver.url}/#/clips?photo={pid}")
  fallback = page.locator(".video-fallback")
  expect(fallback).to_be_visible(timeout=10000)
  expect(fallback).to_contain_text("cannot play .avi")
  expect(fallback.locator("a")).to_have_attribute("href", f"/video/{pid}")
  expect(fallback.locator("svg.play")).to_be_visible()
  expect(page.locator("video.main-video")).to_be_hidden()
  expect(fallback.locator("video.fallback-anim")).to_have_count(1)
  # a still photo has none of this
  page.goto(f"{vserver.url}/#/2023")
  page.locator(".cell").first.click()
  expect(page.locator("video.main-video")).to_be_hidden()
  expect(page.get_by_role("button", name="crop")).to_have_count(1)


def test_filmstrip_marks_videos(page, vserver):
  pid = photos(vserver)["a.mp4"]["id"]
  page.goto(f"{vserver.url}/#/clips?photo={pid}")
  expect(page.locator(".filmstrip .thumb svg.play")).to_have_count(2)


def test_video_filter_via_tag_dropdown_and_hud_chip(page, vserver):
  page.goto(vserver.url + "/#/clips")
  select = page.locator(".filters select.tag-filter")
  expect(select.locator("option", has_text="video (2)")).to_have_count(1, timeout=10000)
  select.select_option("tag:video")
  expect(page.locator(".cell")).to_have_count(2)
  assert all(p["is_video"] for p in api(vserver, "/api/photos?dir=clips&filter=tag:video")["photos"])
  # from the loupe: the implied 'video' chip is a shortcut to the same filter
  pid = photos(vserver)["a.mp4"]["id"]
  page.goto(f"{vserver.url}/#/clips?photo={pid}")
  expect(page.locator(".hud .tag.implied", has_text="video")).to_have_count(1)
  page.locator(".hud .tag.implied", has_text="video").click()
  expect(page.locator(".hud .filter-tag")).to_have_text("filter: tag: video")
  expect(page.locator(".hud .pos")).to_have_text("1/2")
