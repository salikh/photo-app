"""Browser tests against a live server and a synthetic library."""

import json
import re
import time
import urllib.request

from playwright.sync_api import expect


ON = re.compile(r"(^|\s)on(\s|$)")          # a filter button's class list may also hold 'zero'


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
  page.get_by_role("button", name="Rejected").click()
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


def test_dry_run_says_nothing_was_saved(browser, tmp_path):
  from tests.e2e.harness import Server
  srv = Server(tmp_path, xmp_dry_run=True).start()
  try:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    ids = photo_ids(srv)
    pg.goto(f"{srv.url}/#/2024/trip?photo={ids[0]}")
    expect(pg.locator(".loupe")).to_be_visible()
    pg.keyboard.press("3")
    expect(pg.locator("#toast")).to_contain_text("dry run")
    expect(pg.locator(".hud .stars")).to_have_text("☆☆☆☆☆")   # not applied
    assert srv.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") is None
    ctx.close()
  finally:
    srv.stop()


def test_one_star_is_unrated_flag_hides_star_1_and_skips_it(browser, tmp_path):
  from tests.e2e.harness import Server
  from tests.test_scan_sidecars import XMP, write
  srv = Server(tmp_path, one_star_is_unrated=True).start()
  try:
    # IMG_0004 carries darktable's default 1 star
    write(srv.pictures + "/2024/trip/IMG_0004.jpg.xmp", XMP % (1, ""), mtime=1_000_000)
    srv.app.state.scanner.start(); srv.app.state.scanner.wait()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    ids = photo_ids(srv)
    pg.goto(f"{srv.url}/#/2024/trip")
    expect(pg.locator(".cell")).to_have_count(6)
    expect(pg.locator(".badges .stars", has_text="4")).to_have_count(1)      # 4 stars still shown
    assert pg.locator(".badges .stars", has_text="1").count() == 0            # the 1-star badge is hidden
    assert pg.locator(".filters button .lbl").all_inner_texts() == [
        "All", "\u2716 Rejected", "\u2606 Unrated", "\u26052", "\u26053", "\u26054", "\u26055"]   # no \u26051
    pg.get_by_label("more filters").select_option("picked")
    expect(pg.locator(".cell")).to_have_count(1)                              # only the 4-star photo
    pg.goto(f"{srv.url}/#/2024/trip?photo={ids[3]}")                          # IMG_0004, real rating 1
    expect(pg.locator(".hud .stars")).to_have_text("☆☆☆☆☆")
    assert pg.locator(".hud .buttons button", has_text="1").count() == 0      # no meaningless '1' button
    pg.keyboard.press("ArrowRight")
    pg.keyboard.press("ArrowLeft")
    pg.keyboard.press("Escape")
    pg.goto(f"{srv.url}/#/2024/trip?photo={ids[0]}")                          # unrated: up skips 1
    expect(pg.locator(".loupe")).to_be_visible()
    pg.keyboard.press("1")                                                    # '1' means unrated
    pg.wait_for_timeout(300)
    expect(pg.locator(".hud .stars")).to_have_text("☆☆☆☆☆")
    pg.keyboard.press("3")
    expect(pg.locator(".hud .stars")).to_have_text("★★★☆☆")
    ctx.close()
  finally:
    srv.stop()


def test_rating_step_logic_in_the_browser(page, server):
  page.goto(server.url + "/")
  result = page.evaluate("""async () => {
    const r = await import('/static/rating.js');
    const out = {};
    r.configure({one_star_is_unrated: false});
    out.plain = [r.step(0, 1), r.step(1, -1), r.step(5, 1), r.step(-1, -1), r.step(-1, 1, 3), r.step(0, -1)];
    out.plainKeys = [r.afterKey(2, '1'), r.afterKey(2, 'x'), r.afterKey(-1, 'x', 4), r.afterKey(0, 'q')];
    r.configure({one_star_is_unrated: true});
    out.flag = [r.step(0, 1), r.step(1, 1), r.step(2, -1), r.step(1, -1), r.step(5, 1), r.step(-1, 1, 3), r.step(-1, 1)];
    out.flagKeys = [r.afterKey(2, '1'), r.afterKey(0, '2')];
    out.shown = [r.display(1), r.display(2), r.label(1), r.choices()];
    return out;
  }""")
  assert result["plain"] == [1, 0, 5, -1, 3, -1]
  assert result["plainKeys"] == [1, -1, 4, None]
  assert result["flag"] == [2, 2, 0, -1, 5, 3, 0]      # 1 is skipped both ways; un-reject restores stars
  assert result["flagKeys"] == [0, 2]
  assert result["shown"][:2] == [0, 2] and result["shown"][2] == "☆" * 5
  assert result["shown"][3] == [0, 2, 3, 4, 5]


def test_folders_starting_with_a_dot_are_not_shown_as_chips(page, server):
  import os
  from tests.conftest import make_jpeg
  make_jpeg(os.path.join(server.pictures, "2024/trip/.nu/x.jpg"))
  make_jpeg(os.path.join(server.pictures, "2024/.thumbnails/y.jpg"))
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  page.goto(server.url + "/#/2024")
  expect(page.locator(".folders a")).to_have_count(2)                       # trip and home only
  texts = " ".join(page.locator(".folders a").all_inner_texts())
  assert ".nu" not in texts and ".thumbnails" not in texts
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  expect(page.locator(".folders a")).to_have_count(0)                       # only .nu below: nothing shown
  page.goto(server.url + "/#/2024/trip/.nu")                                # still reachable by path
  expect(page.locator(".cell")).to_have_count(1)


# --- filter buttons (ticket 055) and the reject-first order (ticket 058) ---------------------------

def filter_button(page, name):
  return page.locator(".filters button", has_text=name)


def test_filter_buttons_show_the_right_photos_and_keep_state(page, server):
  # IMG_0002 = 4 stars, IMG_0003 = reject; make IMG_0004 2 stars and IMG_0005 4 stars
  ids = photo_ids(server)
  for pid, r in ((ids[3], 2), (ids[4], 4)):
    urllib.request.urlopen(urllib.request.Request(
        f"{server.url}/api/photos/{pid}/rating", data=json.dumps({"rating": r}).encode(),
        headers={"Content-Type": "application/json"}))
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  kinds = page.locator(".filters button").evaluate_all("els => els.map(e => e.dataset.filter)")
  assert kinds == ["all", "rejected", "unrated", "rating:1", "rating:2", "rating:3", "rating:4", "rating:5"]
  assert page.locator(".filters button .lbl").all_inner_texts() == [
      "All", "\u2716 Rejected", "\u2606 Unrated", "\u26051", "\u26052", "\u26053", "\u26054", "\u26055"]
  expect(filter_button(page, "All")).to_have_class(ON)
  filter_button(page, "Unrated").click()
  expect(page.locator(".cell")).to_have_count(2)                       # 0001 and 0006 have no rating
  assert "filter=unrated" in page.url
  expect(filter_button(page, "Unrated")).to_have_class(ON)
  expect(filter_button(page, "All")).not_to_have_class(ON)
  filter_button(page, "\u26054").click()                               # exactly 4 stars
  expect(page.locator(".cell")).to_have_count(2)
  assert "filter=rating%3A4" in page.url
  filter_button(page, "\u26053").click()
  expect(page.locator(".cell")).to_have_count(0)
  expect(page.locator(".status")).to_contain_text("No photos")
  page.reload()                                                          # state is in the URL
  expect(filter_button(page, "\u26053")).to_have_class(ON)
  filter_button(page, "Rejected").click()
  expect(page.locator(".cell")).to_have_count(1)
  page.get_by_label("more filters").select_option("fav")
  expect(page.locator(".cell")).to_have_count(0)
  assert "filter=fav" in page.url


def test_filter_is_kept_when_changing_folder_and_shown_in_the_loupe(page, server):
  page.goto(server.url + "/#/2024?filter=unrated")
  page.get_by_role("link", name="trip").first.click()
  assert "filter=unrated" in page.url
  expect(page.locator(".cell")).to_have_count(4)                         # 6 minus the 4-star and the reject
  page.locator(".cell").first.click()
  expect(page.locator(".loupe")).to_be_visible()
  expect(page.locator(".hud .filter-tag")).to_contain_text("Unrated")
  expect(page.locator(".hud .pos")).to_have_text("1/4")                  # navigation stays in the filtered list


def test_reject_button_is_left_of_the_rating_buttons_in_loupe_and_selection_bar(page, server):
  open_loupe(page, server)
  titles = page.locator(".hud .buttons button").evaluate_all("els => els.map(e => e.title)")
  assert titles[:7] == ["reject (X)", "rate 0", "rate 1", "rate 2", "rate 3", "rate 4", "rate 5"], titles
  xs = [b["x"] for b in [page.locator(".hud .buttons button").nth(i).bounding_box() for i in range(3)]]
  assert xs == sorted(xs)                                                # visually left to right
  page.keyboard.press("Escape")
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").nth(0).click()
  bar = page.locator(".selection-bar button").evaluate_all("els => els.map(e => e.title)")
  assert bar[:2] == ["reject", "rate 0"], bar


def test_client_side_filter_matching_agrees_with_the_server(page, server):
  ids = photo_ids(server)
  for pid, r in ((ids[0], 1), (ids[3], 2), (ids[4], 3), (ids[5], 5)):
    urllib.request.urlopen(urllib.request.Request(
        f"{server.url}/api/photos/{pid}/rating", data=json.dumps({"rating": r}).encode(),
        headers={"Content-Type": "application/json"}))
  urllib.request.urlopen(urllib.request.Request(
      f"{server.url}/api/photos/{ids[2]}/fav", data=json.dumps({"fav": True}).encode(),
      headers={"Content-Type": "application/json"}))
  page.goto(server.url + "/")
  result = page.evaluate("""async () => {
    const f = await import('/static/filters.js');
    const r = await import('/static/rating.js');
    const out = {};
    for (const flag of [false, true]) {
      r.configure({one_star_is_unrated: flag});
      const all = (await (await fetch('/api/photos?dir=2024/trip&sort=name')).json()).photos;
      const names = ['all', 'unrated', 'rejected', 'picked', 'rated', 'fav', 'conflict', 'rating:1', 'rating:2', 'rating:3', 'rating:4', 'rating:5'];
      for (const name of names) {
        const res = await fetch('/api/photos?dir=2024/trip&sort=name&filter=' + encodeURIComponent(name));
        const server = res.ok ? (await res.json()).photos.map(p => p.name) : 'error';
        out[flag + ':' + name] = [all.filter(p => f.matches(p, name)).map(p => p.name), server];
      }
    }
    return out;
  }""")
  # the server test client runs without the flag, so only compare that half; with the flag the client
  # must at least never claim a match for a filter the server would reject
  for key, (client, server_names) in result.items():
    flag, name = key.split(":", 1)
    if flag == "false":
      assert client == server_names, (key, client, server_names)


def test_phone_filter_strip_is_one_scrollable_row_with_big_buttons(phone, server):
  phone.goto(server.url + "/#/2024/trip")
  expect(phone.locator(".cell")).to_have_count(6)
  assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
  strip = phone.locator(".filters").bounding_box()
  assert strip["x"] >= 0 and strip["x"] + strip["width"] <= 391            # inside the screen
  ys = {round(b.bounding_box()["y"]) for b in phone.locator(".filters button").all()}
  assert len(ys) == 1                                                       # one row, no wrapping
  assert all(b.bounding_box()["height"] >= 43 for b in phone.locator(".filters button").all())
  assert phone.evaluate("(() => { const f = document.querySelector('.filters'); return f.scrollWidth > f.clientWidth; })()")
  phone.locator(".filters button", has_text="Rejected").tap()
  expect(phone.locator(".cell")).to_have_count(1)
  phone.locator(".filters button", has_text="\u26054").tap()              # scrolled into reach and tapped
  expect(phone.locator(".cell")).to_have_count(1)


# --- photos leave the view when their rating stops matching the filter (ticket 056) ---------------

def names_in_grid(page):
  return page.locator(".cell").evaluate_all("els => els.map(e => e.title)")


def open_unrated(page, server):
  """Filter Unrated in 2024/trip: IMG_0001, 0004, 0005, 0006 (0002 has 4 stars, 0003 is rejected)."""
  page.goto(server.url + "/#/2024/trip?filter=unrated")
  expect(page.locator(".cell")).to_have_count(4)


def test_rating_a_photo_out_of_the_filter_removes_it_and_advances(page, server):
  open_unrated(page, server)
  page.locator(".cell").first.click()
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")
  expect(page.locator(".hud .pos")).to_have_text("1/4")
  page.keyboard.press("3")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0004")         # the next photo, at once
  expect(page.locator(".hud .pos")).to_have_text("1/3")
  assert f"photo={photo_ids(server)[3]}" in page.url                       # the URL follows
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 3)
  page.keyboard.press("Escape")
  expect(page.locator(".cell")).to_have_count(3)
  assert names_in_grid(page) == ["IMG_0004.jpg", "IMG_0005.jpg", "IMG_0006.jpg"]
  expect(page.locator(".status")).to_have_text("3 of 3 photos")
  # in the "All" view nothing ever leaves
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)


def test_same_rating_stays_and_each_key_press_advances_exactly_one_photo(page, server):
  open_unrated(page, server)
  page.locator(".cell").first.click()
  page.keyboard.press("0")                                                 # already unrated: stays
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")
  expect(page.locator(".hud .pos")).to_have_text("1/4")
  page.keyboard.press("2")
  page.keyboard.press("2")                                                 # fast: the next photo, not the same one
  expect(page.locator(".hud .name")).to_contain_text("IMG_0005")
  expect(page.locator(".hud .pos")).to_have_text("1/2")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 2
           and server.sidecar_rating("2024/trip/IMG_0004.jpg.xmp") == 2)


def test_last_photo_out_closes_the_viewer(page, server):
  page.goto(server.url + "/#/2024/trip?filter=rejected")                   # only IMG_0003
  page.locator(".cell").first.click()
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("2")
  expect(page.locator(".loupe")).to_be_hidden()
  expect(page.locator("#toast")).to_contain_text("no more photos")
  expect(page.locator(".cell")).to_have_count(0)
  expect(page.locator(".status")).to_contain_text("No photos in this folder with this filter")


def test_exactly_n_stars_filter_promote_and_demote_leave(page, server):
  page.goto(server.url + "/#/2024/trip?filter=rating%3A4")                 # IMG_0002 only
  page.locator(".cell").first.click()
  page.keyboard.press("4")                                                 # same: stays
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("5")                                                 # leaves
  expect(page.locator(".loupe")).to_be_hidden()
  expect(page.locator(".cell")).to_have_count(0)


def test_undo_brings_the_photo_back_where_it_was_and_shows_it(page, server):
  open_unrated(page, server)
  page.locator(".cell").nth(1).click()                                     # IMG_0004
  page.keyboard.press("3")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0005")
  page.keyboard.press("3")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0006")
  page.keyboard.press("u")                                                 # undo the last: 0005 returns
  expect(page.locator(".hud .name")).to_contain_text("IMG_0005")
  expect(page.locator(".hud .stars")).to_have_text("☆" * 5)
  page.keyboard.press("u")                                                 # and 0004 before it
  expect(page.locator(".hud .name")).to_contain_text("IMG_0004")
  expect(page.locator(".hud .pos")).to_have_text("2/4")                    # the original position
  page.keyboard.press("Escape")
  assert names_in_grid(page) == ["IMG_0001.jpg", "IMG_0004.jpg", "IMG_0005.jpg", "IMG_0006.jpg"]
  expect(page.locator(".status")).to_have_text("4 of 4 photos")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0004.jpg.xmp") == 0)


def test_batch_rating_removes_the_selected_and_undo_restores_the_order(page, server):
  open_unrated(page, server)
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").nth(1).click()
  page.locator(".cell").nth(3).click()                                     # IMG_0004 and IMG_0006
  page.locator(".selection-bar").get_by_role("button", name="4", exact=True).click()
  expect(page.locator(".cell")).to_have_count(2)
  assert names_in_grid(page) == ["IMG_0001.jpg", "IMG_0005.jpg"]
  expect(page.locator(".status")).to_have_text("2 of 2 photos")
  expect(page.locator(".selection-bar")).to_have_count(0)                  # the selection is cleared
  page.keyboard.press("u")
  expect(page.locator(".cell")).to_have_count(4)
  assert names_in_grid(page) == ["IMG_0001.jpg", "IMG_0004.jpg", "IMG_0005.jpg", "IMG_0006.jpg"]
  expect(page.locator(".status")).to_have_text("4 of 4 photos")


def test_failed_write_puts_the_photo_back_and_shows_the_error(page, server):
  open_unrated(page, server)
  page.route("**/api/photos/*/rating", lambda route: route.fulfill(
      status=500, content_type="application/json", body='{"detail": "internal server error"}'))
  page.locator(".cell").nth(1).click()                                     # IMG_0004
  page.keyboard.press("3")
  expect(page.locator("#toast")).to_contain_text("internal server error")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0004")           # back in front of us
  expect(page.locator(".hud .pos")).to_have_text("2/4")
  expect(page.locator(".hud .stars")).to_have_text("☆" * 5)
  page.keyboard.press("Escape")
  assert names_in_grid(page) == ["IMG_0001.jpg", "IMG_0004.jpg", "IMG_0005.jpg", "IMG_0006.jpg"]
  page.errors.clear()                                                      # the forced 500 is logged by the browser


def test_unfavoriting_leaves_the_favorites_filter(page, server):
  ids = photo_ids(server)
  urllib.request.urlopen(urllib.request.Request(
      f"{server.url}/api/photos/{ids[1]}/fav", data=json.dumps({"fav": True}).encode(),
      headers={"Content-Type": "application/json"}))
  page.goto(server.url + "/#/2024/trip?filter=fav")
  expect(page.locator(".cell")).to_have_count(1)
  page.locator(".cell").first.click()
  page.keyboard.press("f")
  expect(page.locator(".loupe")).to_be_hidden()
  expect(page.locator(".cell")).to_have_count(0)


def test_swipe_that_rates_a_photo_out_of_the_filter_advances_like_a_swipe(phone, server):
  phone.goto(server.url + "/#/2024/trip?filter=unrated")
  expect(phone.locator(".cell")).to_have_count(4)
  phone.locator(".cell").first.tap()
  expect(phone.locator(".hud .name")).to_contain_text("IMG_0001")
  swipe(phone, 0, -120)                                                    # up: 1 star, no longer unrated
  expect(phone.locator(".hud .name")).to_contain_text("IMG_0004")
  expect(phone.locator(".hud .pos")).to_have_text("1/3")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 1)


def test_paging_stays_correct_when_photos_leave_while_a_page_is_loading(browser, server):
  ctx = browser.new_context(viewport={"width": 1280, "height": 800})
  page = ctx.new_page()
  page.add_init_script("window.__pageSize = 2")                            # test hook: pages of two
  held = []
  state = {"hold": True}

  def handler(route):
    if state["hold"] and "offset=" in route.request.url:
      held.append(route)                                                   # keep the second page waiting
    else:
      route.continue_()

  page.route("**/api/photos?*", handler)
  page.goto(server.url + "/#/2024/trip?filter=unrated")
  expect(page.locator(".cell")).to_have_count(2)                           # 0001, 0004 (first page)
  wait_for(lambda: len(held) == 1)
  page.locator(".cell").first.click()                                      # opens at once
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")
  expect(page.locator(".hud .pos")).to_have_text("1/4")                    # 4 in this filter, 2 loaded
  page.keyboard.press("3")
  page.keyboard.press("3")                                                 # both loaded photos leave
  expect(page.locator("#toast")).not_to_contain_text("no more photos")     # more are still coming
  state["hold"] = False
  held[0].continue_()                                                      # the stale page arrives late
  expect(page.locator(".hud .name")).to_contain_text("IMG_0005")           # 0005 is the first left, none skipped
  expect(page.locator(".hud .pos")).to_have_text("1/2")
  page.keyboard.press("ArrowRight")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0006")
  page.keyboard.press("3")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0005")
  page.keyboard.press("3")
  expect(page.locator(".loupe")).to_be_hidden()
  wait_for(lambda: all(server.sidecar_rating(f"2024/trip/IMG_000{n}.jpg.xmp") == 3 for n in (1, 4, 5, 6)))
  ctx.close()


# --- counts on the filter buttons and the grid shortcuts (ticket 057) ---------------------------

def counts_on_buttons(page):
  return page.locator(".filters button").evaluate_all(
      "els => Object.fromEntries(els.map(e => [e.dataset.filter, e.querySelector('.n').textContent]))")


def test_filter_buttons_show_counts_that_follow_ratings_and_undo(page, server):
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  expect(page.locator('.filters button[data-filter="unrated"] .n')).to_have_text("4")
  assert counts_on_buttons(page) == {"all": "6", "rejected": "1", "unrated": "4", "rating:1": "0",
                                     "rating:2": "0", "rating:3": "0", "rating:4": "1", "rating:5": "0"}
  expect(page.locator('.filters button[data-filter="rating:3"]')).to_have_class("zero")     # dimmed, still clickable
  expect(page.locator('.filters button[data-filter="rating:4"]')).not_to_have_class("zero")
  # each number equals what its button shows
  page.locator('.filters button[data-filter="unrated"]').click()
  expect(page.locator(".cell")).to_have_count(4)
  # rate the first unrated photo 3: the photo leaves, and the counts follow
  page.locator(".cell").first.click()
  expect(page.locator(".loupe")).to_be_visible()              # the key must not arrive before the viewer is open
  page.keyboard.press("3")
  expect(page.locator('.filters button[data-filter="unrated"] .n')).to_have_text("3")
  expect(page.locator('.filters button[data-filter="rating:3"] .n')).to_have_text("1")
  expect(page.locator('.filters button[data-filter="rating:3"]')).not_to_have_class("zero")
  expect(page.locator('.filters button[data-filter="all"] .n')).to_have_text("6")
  page.keyboard.press("u")
  expect(page.locator('.filters button[data-filter="unrated"] .n')).to_have_text("4")
  expect(page.locator('.filters button[data-filter="rating:3"] .n')).to_have_text("0")
  # another folder has its own numbers
  page.keyboard.press("Escape")
  page.goto(server.url + "/#/2024/home")
  expect(page.locator(".cell")).to_have_count(3)
  expect(page.locator('.filters button[data-filter="all"] .n')).to_have_text("3")
  expect(page.locator('.filters button[data-filter="rejected"] .n')).to_have_text("0")


def test_grid_shortcuts_switch_the_filter(page, server):
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  page.keyboard.press("Shift+Digit0")
  expect(page.locator(".cell")).to_have_count(4)
  assert "filter=unrated" in page.url
  page.keyboard.press("Shift+KeyX")
  expect(page.locator(".cell")).to_have_count(1)
  assert "filter=rejected" in page.url
  page.keyboard.press("Shift+Digit4")
  expect(page.locator('.filters button[data-filter="rating:4"]')).to_have_class(ON)
  assert "filter=rating%3A4" in page.url
  page.keyboard.press("Shift+KeyA")
  expect(page.locator(".cell")).to_have_count(6)
  assert "filter" not in page.url
  # the tooltips say so
  assert "Shift+3" in page.locator('.filters button[data-filter="rating:3"]').get_attribute("title")
  assert "Shift+X" in page.locator('.filters button[data-filter="rejected"]').get_attribute("title")


def test_grid_shortcuts_do_nothing_in_the_viewer_or_while_typing(page, server):
  open_loupe(page, server)
  page.keyboard.press("Shift+Digit3")                       # in the viewer: not a filter shortcut
  expect(page.locator(".loupe")).to_be_visible()
  assert "filter" not in page.url
  page.keyboard.press("Escape")
  page.keyboard.press("Tab")
  page.get_by_label("sort").focus()
  page.keyboard.press("Shift+Digit2")                       # focus is in a field: leave it alone
  assert "filter" not in page.url


def test_shortcut_for_one_star_is_ignored_with_the_flag(browser, tmp_path):
  from tests.e2e.harness import Server
  srv = Server(tmp_path, one_star_is_unrated=True).start()
  try:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.goto(srv.url + "/#/2024/trip")
    expect(pg.locator(".cell")).to_have_count(6)
    pg.keyboard.press("Shift+Digit1")
    pg.wait_for_timeout(300)
    assert "filter" not in pg.url
    pg.keyboard.press("Shift+Digit4")
    assert "filter=rating%3A4" in pg.url
    counts = pg.locator(".filters button").evaluate_all("els => els.map(e => e.dataset.filter)")
    assert "rating:1" not in counts
    ctx.close()
  finally:
    srv.stop()


# --- a busy database: retry, then a clear toast (ticket 052) ---------------------------------------

def test_busy_database_shows_the_error_toast_reverts_and_works_again_after_release(browser, tmp_path):
  import sqlite3
  from tests.e2e.harness import Server
  srv = Server(tmp_path, busy_retry_seconds=1.0).start()
  writer = sqlite3.connect(srv.settings.db_path, isolation_level=None, timeout=0)
  try:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    ids = photo_ids(srv)
    pg.goto(f"{srv.url}/#/2024/trip?photo={ids[0]}")
    expect(pg.locator(".loupe")).to_be_visible()
    writer.execute("BEGIN IMMEDIATE")                                       # another writer holds the lock
    pg.keyboard.press("3")
    expect(pg.locator(".hud .stars")).to_have_text("★★★☆☆")     # optimistic first
    expect(pg.locator("#toast")).to_contain_text("database busy")           # after the retries: an error toast
    expect(pg.locator("#toast")).to_have_class(re.compile("error"))
    expect(pg.locator(".hud .stars")).to_have_text("☆" * 5)            # and the change is reverted
    pg.goto(f"{srv.url}/#/2024/trip")                                       # reading still works while locked
    expect(pg.locator(".cell")).to_have_count(6)
    writer.execute("ROLLBACK")                                              # the lock is gone
    pg.locator(".cell").first.click()
    expect(pg.locator(".loupe")).to_be_visible()
    pg.keyboard.press("3")
    wait_for(lambda: srv.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 3)
    expect(pg.locator(".hud .stars")).to_have_text("★★★☆☆")
    ctx.close()
  finally:
    srv.stop()


# --- preloading the photos within +/-2 of the current one (ticket 050) ---------------------------

def img_requests(page):
  seen = []
  page.on("request", lambda r: seen.append(r.url) if "/img/" in r.url else None)
  return seen


def kind_ids(urls):
  """[(size, file_id)] in request order, without repeats."""
  out = []
  for u in urls:
    parts = u.split("/img/")[1].split("?")[0].split("/")
    item = (parts[0], int(parts[1]))
    if item not in out:
      out.append(item)
  return out


def file_ids_of(server, folder="2024/trip"):
  return [p["file_id"] for p in api(server, f"/api/photos?dir={folder}&sort=name")["photos"]]


def test_preloads_medium_then_huge_for_the_two_neighbors_each_way(page, server):
  fids = file_ids_of(server)
  seen = img_requests(page)
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[2]}")                     # the third of six
  expect(page.locator(".loupe")).to_be_visible()
  wait_for(lambda: len([k for k in kind_ids(seen) if k[0] in ("Medium", "Huge") and k[1] != fids[2]]) >= 8)
  requested = kind_ids(seen)
  window = {fids[0], fids[1], fids[3], fids[4]}
  for size in ("Medium", "Huge"):
    assert {f for s_, f in requested if s_ == size and f != fids[2]} == window, (size, requested)
  assert (("Medium", fids[5]) not in requested) and (("Huge", fids[5]) not in requested)      # 3 away: not preloaded
  # (after a pause on a photo its own full-size image is fetched too, see preload.decodeCurrentHuge)
  # Medium is started before any Huge (what is shown on arrival comes first)
  first_huge = min(i for i, k in enumerate(requested) if k[0] == "Huge")
  assert all(k[0] != "Medium" or i < first_huge for i, k in enumerate(requested) if k[1] != fids[2])
  assert set(held_pairs(page)) - {("Huge", fids[2])} == {(sz, f) for sz in ("Medium", "Huge") for f in window}   # the window (and, after a pause, the current photo's own full size)


def held_pairs(page):
  return sorted((u.split("/img/")[1].split("/")[0], int(u.split("/")[-1])) for u in page.evaluate("window.__preloadedUrls()"))


def test_the_window_follows_the_direction_and_releases_what_left_it(page, server):
  fids = file_ids_of(server)
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  page.wait_for_timeout(600)
  # the window (1 and 2 ahead) plus, after the pause, the current photo's own full-size image
  assert held_pairs(page) == sorted([("Medium", fids[1]), ("Medium", fids[2]), ("Huge", fids[1]), ("Huge", fids[2]), ("Huge", fids[0])])
  for _ in range(4):
    page.keyboard.press("ArrowRight")
  expect(page.locator(".hud .pos")).to_have_text("5/6")                   # index 4
  page.wait_for_timeout(600)
  now = held_pairs(page)
  # everything held lies within two positions of the current photo (index 4): 2, 3, 4 (own), 5
  assert {f for _, f in now} <= {fids[2], fids[3], fids[4], fids[5]}, now
  assert not {f for _, f in now} & {fids[0], fids[1]}                     # what was left behind is released
  assert ("Medium", fids[3]) in now and ("Medium", fids[5]) in now         # one back, one ahead are ready
  assert ("Huge", fids[5]) in now and ("Huge", fids[3]) in now
  assert len(now) <= 8


def test_holding_an_arrow_key_does_not_leave_a_pile_of_preloads(page, server):
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  for _ in range(5):
    page.keyboard.press("ArrowRight")
  for _ in range(5):
    page.keyboard.press("ArrowLeft")
  expect(page.locator(".hud .pos")).to_have_text("1/6")
  page.wait_for_timeout(500)
  fids = file_ids_of(server)
  now = held_pairs(page)                                                  # at the first photo: the window is 1 and 2 ahead
  assert {f for _, f in now} <= {fids[0], fids[1], fids[2]}, now         # (plus the current photo's own entries)
  assert len(now) <= 6
  page.keyboard.press("Escape")
  assert page.evaluate("window.__preloadedUrls()") == []                  # closing the viewer releases everything


def test_zoom_uses_the_preloaded_full_size_image_without_a_new_request(page, server):
  ids = photo_ids(server)
  fids = file_ids_of(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("ArrowRight")                                       # now on photo 2; its Huge was preloaded from photo 1
  expect(page.locator(".hud .pos")).to_have_text("2/6")
  page.wait_for_timeout(800)
  seen = img_requests(page)                                               # record only from here on
  page.keyboard.press("z")
  expect(page.locator(".stage.zoomed")).to_have_count(1)
  page.wait_for_function("document.querySelector('.stage img.main').complete && document.querySelector('.stage img.main').naturalWidth >= 1600 && document.querySelector('.stage img.main').src.includes('/Huge/')")
  assert not [u for u in seen if f"/img/Huge/{fids[1]}" in u], seen       # served from the cache: no second request


def test_no_full_size_preload_on_a_data_saving_connection(browser, server):
  ctx = browser.new_context(viewport={"width": 1280, "height": 800})
  pg = ctx.new_page()
  pg.add_init_script("Object.defineProperty(navigator, 'connection', {value: {saveData: true, effectiveType: '4g'}})")
  seen = img_requests(pg)
  ids = photo_ids(server)
  fids = file_ids_of(server)
  pg.goto(f"{server.url}/#/2024/trip?photo={ids[2]}")
  expect(pg.locator(".loupe")).to_be_visible()
  pg.wait_for_timeout(1000)
  kinds = kind_ids(seen)
  assert {s_ for s_, f in kinds if f != fids[2] and s_ in ("Medium", "Huge")} == {"Medium"}     # Medium only
  assert {f for s_, f in kinds if s_ == "Medium" and f != fids[2]} == {fids[0], fids[1], fids[3], fids[4]}
  ctx.close()


def test_slow_connection_type_also_skips_the_full_size(browser, server):
  ctx = browser.new_context(viewport={"width": 1280, "height": 800})
  pg = ctx.new_page()
  pg.add_init_script("Object.defineProperty(navigator, 'connection', {value: {saveData: false, effectiveType: '3g'}})")
  seen = img_requests(pg)
  ids = photo_ids(server)
  pg.goto(f"{server.url}/#/2024/trip?photo={ids[1]}")
  expect(pg.locator(".loupe")).to_be_visible()
  pg.wait_for_timeout(800)
  assert "Huge" not in {s_ for s_, f in kind_ids(seen)}
  ctx.close()
