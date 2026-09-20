"""Browser tests against a live server and a synthetic library."""

import json
import time
import urllib.request

from playwright.sync_api import expect


def api(server, path):
  return json.load(urllib.request.urlopen(server.url + path))


def photo_ids(server, folder="2024/trip"):
  return [p["id"] for p in api(server, f"/api/photos?dir={folder}&sort=name")["photos"]]


def wait_for(cond, timeout=5.0):
  end = time.time() + timeout
  while time.time() < end:
    value = cond()
    if value:
      return value
    time.sleep(0.05)
  raise AssertionError("condition not met in time")


def open_loupe(page, server, index=0):
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[index]}")
  expect(page.locator(".loupe")).to_be_visible()
  return ids


def swipe(page, dx, dy, start=(195, 350)):
  """A drag with the pointer; Pointer Events treat it like a touch swipe."""
  x, y = start
  page.mouse.move(x, y)
  page.mouse.down()
  steps = 8
  for i in range(1, steps + 1):
    page.mouse.move(x + dx * i / steps, y + dy * i / steps)
  page.mouse.up()


# ------------------------------------------------------------------ desktop

def test_browse_folders_grid_badges_and_loupe(page, server):
  page.goto(server.url + "/#/")
  expect(page.locator(".folders a")).to_have_count(2)
  page.get_by_role("link", name="2024").first.click()
  page.get_by_role("link", name="trip").first.click()
  expect(page.locator(".cell")).to_have_count(6)
  expect(page.locator(".badges .stars", has_text="4")).to_have_count(1)
  expect(page.locator(".cell.rejected")).to_have_count(1)
  page.locator(".cell").first.click()
  expect(page.locator(".loupe")).to_be_visible()
  expect(page.locator(".hud .pos")).to_have_text("1/6")
  page.keyboard.press("Escape")
  expect(page.locator(".loupe")).to_be_hidden()


def test_keyboard_culling_writes_sidecars_and_undo(page, server):
  ids = open_loupe(page, server)
  page.keyboard.press("3")
  expect(page.locator(".hud .stars")).to_have_text("★★★☆☆")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 3)
  page.keyboard.press("ArrowRight")
  expect(page.locator(".hud .pos")).to_have_text("2/6")
  page.keyboard.press("x")
  expect(page.locator(".hud .stars.reject")).to_be_visible()
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0002.jpg.xmp") == -1)
  page.keyboard.press("u")                         # undo the reject: back to 4 stars
  expect(page.locator(".hud .stars")).to_have_text("★★★★☆")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0002.jpg.xmp") == 4)
  page.keyboard.press("f")
  expect(page.locator(".hud .fav-on")).to_be_visible()
  wait_for(lambda: b"fav" in open(server.pictures + "/2024/trip/IMG_0002.jpg.xmp", "rb").read())
  page.keyboard.press("ArrowLeft")
  expect(page.locator(".hud .pos")).to_have_text("1/6")
  page.keyboard.press("ArrowLeft")                 # first picture: stays, tells us
  expect(page.locator(".hud .pos")).to_have_text("1/6")
  expect(page.locator("#toast")).to_contain_text("first picture")
  api_state = api(server, f"/api/photos/{ids[0]}")
  assert api_state["rating"] == 3


def test_unreject_restores_previous_stars(page, server):
  open_loupe(page, server, 1)                       # IMG_0002 has 4 stars from its sidecar
  page.keyboard.press("x")
  expect(page.locator(".hud .stars.reject")).to_be_visible()
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0002.jpg.xmp") == -1)
  page.keyboard.press("x")
  expect(page.locator(".hud .stars")).to_have_text("★★★★☆")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0002.jpg.xmp") == 4)


def test_tags_via_keyboard(page, server):
  ids = open_loupe(page, server)
  page.keyboard.press("t")
  page.locator(".hud input.tags").fill("beach, summer")
  page.keyboard.press("Enter")
  expect(page.locator(".hud .tag")).to_have_count(2)
  assert set(api(server, f"/api/photos/{ids[0]}")["tags"]) == {"beach", "summer"}
  page.keyboard.press("t")
  page.locator(".hud input.tags").fill("-beach")
  page.keyboard.press("Enter")
  expect(page.locator(".hud .tag")).to_have_count(1)


def test_url_state_survives_reload_and_back_closes_loupe(page, server):
  ids = open_loupe(page, server, 2)
  page.keyboard.press("ArrowRight")
  expect(page.locator(".hud .pos")).to_have_text("4/6")
  page.reload()
  expect(page.locator(".loupe")).to_be_visible()
  expect(page.locator(".hud .pos")).to_have_text("4/6")
  page.keyboard.press("Escape")
  expect(page.locator(".loupe")).to_be_hidden()
  page.go_back()                                    # back to the loupe entry
  expect(page.locator(".loupe")).to_be_visible()


def test_filter_and_sort_are_in_the_url(page, server):
  page.goto(server.url + "/#/2024/trip")
  page.get_by_label("filter").select_option("rejected")
  expect(page.locator(".cell")).to_have_count(1)
  assert "filter=rejected" in page.url


def test_selection_and_batch_rating_and_undo(page, server):
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  page.get_by_role("button", name="Select").click()
  cells = page.locator(".cell")
  cells.nth(3).click()
  cells.nth(4).click()
  expect(page.locator(".selection-bar")).to_contain_text("2 selected")
  page.locator(".selection-bar").get_by_role("button", name="5", exact=True).click()
  expect(page.locator(".badges .stars", has_text="5")).to_have_count(2)
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0005.jpg.xmp") == 5)
  page.keyboard.press("u")
  expect(page.locator(".badges .stars", has_text="5")).to_have_count(0)


def test_pages_activity_attention_usage_jobs(page, server):
  ids = open_loupe(page, server)
  page.keyboard.press("4")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 4)
  page.goto(server.url + "/#!activity")
  expect(page.locator("table tbody tr")).to_have_count(1)
  page.get_by_role("button", name="Undo").click()
  expect(page.locator("td .dim", has_text="undone")).to_be_visible()
  page.goto(server.url + "/#!attention")
  expect(page.get_by_role("heading", name="Sidecars that disagree (0)")).to_be_visible()
  page.goto(server.url + "/#!usage")
  expect(page.locator("table")).to_contain_text("Thumb")
  page.goto(server.url + "/#!jobs")
  expect(page.get_by_role("heading", name="Background jobs")).to_be_visible()


def test_zoom_loads_full_size_and_toggles(page, server):
  open_loupe(page, server)
  src = page.locator(".stage img.main").get_attribute("src")
  assert "/img/Medium/" in src
  page.keyboard.press("z")
  expect(page.locator(".stage.zoomed")).to_have_count(1)
  assert "/img/Huge/" in page.locator(".stage img.main").get_attribute("src")
  page.keyboard.press("z")
  assert "/img/Medium/" in page.locator(".stage img.main").get_attribute("src")


def test_files_panel_and_representative_for_pair(page, server, tmp_path):
  # add a DNG next to a JPEG so one Photo has two files
  import os
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  expect(page.locator(".files-panel .file")).to_have_count(2)
  page.locator(".files-panel").get_by_role("button", name="Show this").click()
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001.DNG")
  page.keyboard.press("g")                          # cycle back to the JPEG
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001.jpg")
  page.errors.clear()      # the fake one-byte DNG legitimately has no image (404)


# -------------------------------------------------------------------- phone

def test_phone_layout_has_no_horizontal_scroll_and_big_touch_targets(phone, server):
  phone.goto(server.url + "/#/2024/trip")
  expect(phone.locator(".cell")).to_have_count(6)
  assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
  for selector in ("header.bar button", "header.bar select", ".folders a"):
    for box in [b.bounding_box() for b in phone.locator(selector).all()]:
      assert box["height"] >= 43, (selector, box)
  ids = photo_ids(server)
  phone.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(phone.locator(".loupe")).to_be_visible()
  assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
  hud = phone.locator(".hud").bounding_box()
  assert hud["y"] + hud["height"] <= 800 + 1               # HUD is on screen
  for box in [b.bounding_box() for b in phone.locator(".hud .buttons button").all()]:
    assert box["height"] >= 43 and box["width"] >= 43


def test_swipe_left_right_navigates_and_short_drag_does_not(phone, server):
  open_loupe(phone, server)
  expect(phone.locator(".hud .pos")).to_have_text("1/6")
  swipe(phone, -150, 0)
  expect(phone.locator(".hud .pos")).to_have_text("2/6")
  swipe(phone, -150, 0)
  expect(phone.locator(".hud .pos")).to_have_text("3/6")
  swipe(phone, 150, 0)
  expect(phone.locator(".hud .pos")).to_have_text("2/6")
  swipe(phone, -20, 0)                                      # too short
  phone.wait_for_timeout(300)
  expect(phone.locator(".hud .pos")).to_have_text("2/6")
  swipe(phone, 150, 0)
  expect(phone.locator(".hud .pos")).to_have_text("1/6")
  swipe(phone, 150, 0)                                      # already first
  expect(phone.locator(".hud .pos")).to_have_text("1/6")
  expect(phone.locator("#toast")).to_contain_text("first picture")


def test_swipe_up_down_changes_rating_one_step_and_writes_xmp(phone, server):
  open_loupe(phone, server)                                # IMG_0001: unrated
  swipe(phone, 0, -120)
  expect(phone.locator(".hud .stars")).to_have_text("★☆☆☆☆")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 1)
  swipe(phone, 0, -300)                                    # one long swipe is still one step
  expect(phone.locator(".hud .stars")).to_have_text("★★☆☆☆")
  swipe(phone, 0, 120)
  swipe(phone, 0, 120)
  expect(phone.locator(".hud .stars")).to_have_text("☆☆☆☆☆")
  swipe(phone, 0, 120)                                     # down from unrated = reject
  expect(phone.locator(".hud .stars.reject")).to_be_visible()
  swipe(phone, 0, 120)                                     # clamped
  expect(phone.locator(".hud .stars.reject")).to_be_visible()
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == -1)
  swipe(phone, 0, -120)          # up from reject: it was unrated when rejected, so unrated
  expect(phone.locator(".hud .stars")).to_have_text("\u2606\u2606\u2606\u2606\u2606")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 0)


def test_swipe_up_from_reject_restores_the_stars_it_had(phone, server):
  open_loupe(phone, server, 1)                             # IMG_0002 has 4 stars
  phone.locator(".hud .buttons button[title^='reject']").tap()
  expect(phone.locator(".hud .stars.reject")).to_be_visible()
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0002.jpg.xmp") == -1)
  swipe(phone, 0, -120)
  expect(phone.locator(".hud .stars")).to_have_text("\u2605\u2605\u2605\u2605\u2606")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0002.jpg.xmp") == 4)


def test_phone_buttons_and_undo_work_without_keyboard(phone, server):
  open_loupe(phone, server)
  phone.locator(".hud .buttons").get_by_role("button", name="3", exact=True).tap()
  expect(phone.locator(".hud .stars")).to_have_text("★★★☆☆")
  phone.locator(".hud .buttons button[title^='undo']").tap()
  expect(phone.locator(".hud .stars")).to_have_text("☆☆☆☆☆")
  phone.locator(".hud .buttons button[title^='close']").tap()
  expect(phone.locator(".loupe")).to_be_hidden()


def test_tap_toggles_zoom_but_swipe_does_not(phone, server):
  open_loupe(phone, server)
  swipe(phone, -150, 0)
  expect(phone.locator(".hud .pos")).to_have_text("2/6")
  expect(phone.locator(".stage.zoomed")).to_have_count(0)     # the drag was not a tap
  phone.locator(".stage").tap(position={"x": 195, "y": 300})
  expect(phone.locator(".stage.zoomed")).to_have_count(1)
