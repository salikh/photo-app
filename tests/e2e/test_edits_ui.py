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


def test_raw_panel_has_the_same_adjustments_as_a_jpg_and_saves_them(page, server, monkeypatch):
  # ticket 235: a RAW offers every adjustment a JPG does (same labels, same Advanced split), and
  # Save stores the edit document next to the RAW conversion settings.
  from photoapp import previews
  monkeypatch.setattr(previews, "embedded_preview", lambda path: Image.new("RGB", (1600, 1200), "red"))
  monkeypatch.setattr(previews, "render",
                      lambda path, settings=None, half_size=False: Image.new("RGB", (1600, 1200), "blue"))
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  edit_requests = []
  page.on("request", lambda r: edit_requests.append(r.url) if "raw_preview?" in r.url else None)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")
  jpg = page.locator(".files-panel .file", has_text="IMG_0001.jpg")
  expect(row.locator(".raw-settings")).to_be_visible()
  jpg.locator("input[aria-label]").first.wait_for(state="attached") if jpg.locator("input[aria-label]").count() else None
  def labels(r, advanced):
    scope = r.locator("details.advanced") if advanced else r
    return {el.get_attribute("aria-label") for el in scope.locator("input[aria-label]").all()}
  raw_basic = labels(row, False) - labels(row, True)
  jpg_basic = labels(jpg, False) - labels(jpg, True)
  assert jpg_basic <= raw_basic, jpg_basic - raw_basic      # every basic JPG control is also basic in RAW
  assert labels(jpg, True) <= labels(row, True), labels(jpg, True) - labels(row, True)
  assert {"black point", "midtones", "white point", "monochrome", "sharpen"} <= raw_basic
  assert {"auto levels", "red black", "blue white"} <= labels(row, True)

  row.locator("input[aria-label=monochrome]").check()
  row.locator("input[aria-label='black point']").evaluate(
      "(el) => { el.value = 30; el.dispatchEvent(new Event('input', {bubbles: true})); }")
  expect(page.locator(".stage img.tuning:not(.row-preview)")).to_have_attribute(
      "src", re.compile(r"raw_preview\?.*edits="), timeout=15000)
  row.get_by_role("button", name="Save", exact=True).click()
  expect(page.locator("#toast")).to_contain_text("saved")
  fid = server.app.state.db.execute("SELECT id FROM files WHERE path LIKE '%.DNG'").fetchone()[0]
  stored = api(server, f"/api/photos/{ids[0]}")["files"]
  dng = [f for f in stored if f["id"] == fid][0]
  assert "mono" in dng["edits"] and dng["edits"]["levels"]["black"] == 30
  page.errors.clear()      # the fake one-byte DNG legitimately has no image (404)


def test_plus_minus_buttons_step_a_slider_by_one_step_and_repeat_when_held(page, server):
  # ticket 236: tap areas beside each slider, for fine adjustments on a touch screen.
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")
  page.keyboard.press("i")
  slider = page.locator(".edit-settings input[aria-label='brightness']")
  plus = page.locator(".edit-settings button[aria-label='Brightness: one step up']")
  minus = page.locator(".edit-settings button[aria-label='Brightness: one step down']")
  expect(slider).to_have_value("0")
  plus.click()
  expect(slider).to_have_value("1")
  minus.click(); minus.click()
  expect(slider).to_have_value("-1")
  expect(page.locator(".edit-settings button", has_text="Save")).to_be_enabled()   # as if dragged
  box = plus.bounding_box()
  assert box["width"] >= 32 and box["height"] >= 32                                 # a finger-sized target
  page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
  page.mouse.down()
  page.wait_for_timeout(1200)                                                        # held: it repeats
  page.mouse.up()
  assert int(slider.input_value()) > 3
  stop = int(slider.input_value())
  page.wait_for_timeout(400)
  assert int(slider.input_value()) == stop                                           # released: stops
  # a fractional slider keeps its precision and the end of its range
  contrast = page.locator(".edit-settings input[aria-label='contrast']")
  page.locator(".edit-settings button[aria-label='Contrast: one step up']").click()
  expect(contrast).to_have_value("1.05")


def test_slider_rows_fit_on_one_line_on_a_phone(phone, server):
  # ticket 236 follow-up: "(-) slider (+) value" must not wrap on a narrow screen.
  ids = photo_ids(server)
  phone.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(phone.locator(".hud .name")).to_contain_text("IMG_0001")
  phone.keyboard.press("i")
  row = phone.locator(".edit-settings .row", has=phone.locator("input[aria-label='brightness']"))
  expect(row).to_be_visible()
  boxes = [row.locator(sel).bounding_box() for sel in
           ("button[aria-label='Brightness: one step down']", "input[type=range]",
            "button[aria-label='Brightness: one step up']", ".meta")]
  line = boxes[1]                                    # the slider: every part overlaps its line
  assert all(b["y"] < line["y"] + line["height"] and b["y"] + b["height"] > line["y"]
             for b in boxes), boxes
  assert phone.locator(".files-panel").bounding_box()["width"] <= 390      # the panel fits the phone
  panel = phone.locator(".files-panel").bounding_box()
  slider = row.locator("input[type=range]").bounding_box()
  assert slider["width"] >= 80 and slider["x"] + slider["width"] <= panel["x"] + panel["width"]
  assert phone.evaluate("document.querySelector('.files-panel').scrollWidth <= "
                        "document.querySelector('.files-panel').clientWidth")
