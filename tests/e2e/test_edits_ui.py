"""Epic 222 (ticket 228): the edit panel for a JPG in the loupe."""

import hashlib
import io
import json
import os
import re

from PIL import Image
from playwright.sync_api import expect

from tests.e2e.test_ui import api, photo_ids


def sha(path):
  with open(path, "rb") as f:
    return hashlib.sha256(f.read()).hexdigest()


def mean_red(server, fid):
  import urllib.request
  data = urllib.request.urlopen(f"{server.url}/img/Small/{fid}").read()
  px = list(Image.open(io.BytesIO(data)).convert("RGB").getdata())
  return sum(p[0] for p in px) / len(px)


def set_slider(page, aria, value):
  page.locator(f".edit-settings input[aria-label='{aria}']").evaluate(
      "(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); }",
      str(value))


def test_edit_panel_previews_saves_and_never_touches_the_original(page, server):
  ids = photo_ids(server)
  fid = server.app.state.db.execute(
      "SELECT id, path FROM files WHERE photo_id = ?", (ids[0],)).fetchone()
  original = os.path.join(server.pictures, fid["path"])
  digest = sha(original)
  before = mean_red(server, fid["id"])
  preview_requests = []
  page.on("request", lambda r: preview_requests.append(r.url) if "/edit_preview?" in r.url else None)

  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")
  page.keyboard.press("i")
  expect(page.locator(".edit-settings")).to_be_visible()
  save = page.locator(".edit-settings button", has_text="Save")
  expect(save).to_be_disabled()

  set_slider(page, "brightness", -60)
  expect(save).to_be_enabled()
  tuning = page.locator(".stage img.tuning:not(.row-preview)")
  expect(tuning).to_have_attribute("src", re.compile(r"^blob:"), timeout=15000)
  assert preview_requests and json.loads(
      re.search(r"edits=([^&]+)", preview_requests[-1]).group(1).replace("%22", '"').replace(
          "%7B", "{").replace("%7D", "}").replace("%3A", ":").replace("%2C", ","))["brightness"] == -60
  assert api(server, f"/api/photos/{ids[0]}")["files"][0]["edits"] is None   # nothing stored yet

  save.click()
  expect(save).to_be_disabled(timeout=15000)
  assert api(server, f"/api/photos/{ids[0]}")["files"][0]["edits"] == {"brightness": -60.0}
  assert mean_red(server, fid["id"]) < before - 20          # baked into the thumbnail
  assert sha(original) == digest                            # the JPG itself is untouched

  page.locator(".edit-settings button", has_text="Reset to default").click()
  page.locator(".edit-settings button", has_text="Save").click()
  expect(page.locator(".edit-settings button", has_text="Save")).to_be_disabled(timeout=15000)
  assert api(server, f"/api/photos/{ids[0]}")["files"][0]["edits"] is None
  assert abs(mean_red(server, fid["id"]) - before) < 3


def test_s_stores_and_a_applies_adjustments_between_files(page, server):
  # Ticket 230: S copies the shown file's edits (pending ones included), A saves them to another.
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")
  page.keyboard.press("i")
  set_slider(page, "brightness", -50)                  # pending, not saved
  page.locator("body").click(position={"x": 5, "y": 5})   # take focus off the slider
  page.keyboard.press("s")
  expect(page.locator("#toast")).to_contain_text("stored adjustments from IMG_0001")
  page.keyboard.press("ArrowRight")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0002")
  page.keyboard.press("a")
  expect(page.locator("#toast")).to_contain_text("applied adjustments from IMG_0001")
  second = api(server, f"/api/photos/{ids[1]}")["files"][0]
  assert second["edits"] == {"brightness": -50.0}
  assert api(server, f"/api/photos/{ids[0]}")["files"][0]["edits"] is None   # the source wasn't saved
  page.keyboard.press("ArrowRight")                    # a third file: store "nothing", apply resets
  page.keyboard.press("ArrowLeft")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0002")
  page.keyboard.press("s")                             # IMG_0002's saved edits
  page.keyboard.press("ArrowLeft")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")
  page.keyboard.press("a")
  expect(page.locator("#toast")).to_contain_text("applied adjustments")
  assert api(server, f"/api/photos/{ids[0]}")["files"][0]["edits"] == {"brightness": -50.0}
