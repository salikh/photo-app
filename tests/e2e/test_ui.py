"""Browser tests against a live server and a synthetic library."""

import json
import os
import re
import shutil
import threading
import time
import urllib.request

import pytest
from PIL import Image
from playwright.sync_api import expect


ON = re.compile(r"(^|\s)on(\s|$)")          # a filter button's class list may also hold 'zero'
REAL_DNG = os.environ.get("REAL_DNG")
real_dng_only = pytest.mark.skipif(not REAL_DNG or not os.path.exists(REAL_DNG or ""),
                                   reason="REAL_DNG not set")


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


def test_debug_button_rerenders_thumbnails(page, server):
  # ticket 079: the tucked-away debug button clears and regenerates the current photo's thumbs.
  ids = open_loupe(page, server)
  file_id = api(server, f"/api/photos/{ids[0]}")["representative_file_id"]
  urllib.request.urlopen(f"{server.url}/img/Thumb/{file_id}")   # make sure something is cached first
  assert api(server, "/api/thumbs/usage")["usage"]["Thumb"]["files"] >= 1

  btn = page.locator(".hud .buttons button.debug-menu")
  expect(btn).to_be_visible()
  btn.click()                                                    # ticket 196: opens a menu, no action yet
  expect(page.locator(".actions-modal")).to_be_visible()
  expect(page.locator("#toast")).not_to_contain_text("re-rendering")
  page.locator(".actions-modal .menu-item", has_text="Flush thumbnails").click()
  expect(page.locator(".actions-modal")).to_have_count(0)
  expect(page.locator("#toast")).to_contain_text("re-rendering")
  wait_for(lambda: urllib.request.urlopen(f"{server.url}/img/Thumb/{file_id}").status == 200)


def test_broken_thumbnail_shows_a_prominent_fix_button(page, server):
  # ticket 079: after every retry fails, a prominent fix button replaces the tucked-away one.
  # Real timers (the retry backoff -- 1500ms * (1+2+3+4+5+6) = 31.5s -- isn't configurable, and a
  # fake clock can't safely fast-forward through retries that are each scheduled only once the
  # previous real network response comes back), so this test genuinely takes about half a minute.
  ids = photo_ids(server)
  page.route("**/img/Medium/*", lambda route: route.fulfill(status=404, body="nope"))
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  expect(page.locator(".hud .buttons button.debug-menu")).to_be_visible()   # not broken yet
  expect(page.locator(".hud .buttons button.broken-thumb")).to_be_visible(timeout=40000)
  expect(page.locator(".hud .buttons button.debug-menu")).to_have_count(0)
  page.errors.clear()   # the forced 404s are logged by the browser -- that's the point of the test


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


def test_back_after_filter_and_sort_changes_returns_to_the_previous_folder(page, server):
  # ticket 077: filter/sort changes must not be their own Back-button stop.
  page.goto(server.url + "/#/")
  page.get_by_role("link", name="2024").first.click()          # real folder nav: a Back stop
  page.get_by_role("link", name="trip").first.click()          # real folder nav: a Back stop
  expect(page.locator(".cell")).to_have_count(6)

  page.get_by_role("button", name="Rejected").click()
  expect(page.locator(".cell")).to_have_count(1)
  page.get_by_role("button", name="Unrated").click()
  page.locator('select[aria-label="sort"]').select_option("name")
  assert "filter=unrated" in page.url and "sort=name" in page.url

  page.go_back()   # one press: undoes the folder nav into 'trip', not the filter/sort changes
  expect(page.locator(".folders a")).to_have_count(2)           # back at '2024', showing subfolders
  assert page.url.endswith("/2024")
  assert "filter=unrated" not in page.url and "trip" not in page.url


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


def test_shift_click_selects_range_from_the_anchor(page, server):
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  page.get_by_role("button", name="Select").click()
  cells = page.locator(".cell")
  cells.nth(1).click()   # anchor
  cells.nth(4).click(modifiers=["Shift"])
  expect(page.locator(".selection-bar")).to_contain_text("4 selected")
  for i in range(1, 5):
    expect(cells.nth(i)).to_have_class(re.compile(r"\bselected\b"))
  expect(cells.nth(0)).not_to_have_class(re.compile(r"\bselected\b"))
  expect(cells.nth(5)).not_to_have_class(re.compile(r"\bselected\b"))
  # A second Shift+Click extends from the *same* original anchor (index 1), not from index 4.
  cells.nth(2).click(modifiers=["Shift"])
  expect(page.locator(".selection-bar")).to_contain_text("2 selected")
  expect(cells.nth(1)).to_have_class(re.compile(r"\bselected\b"))
  expect(cells.nth(2)).to_have_class(re.compile(r"\bselected\b"))
  expect(cells.nth(3)).not_to_have_class(re.compile(r"\bselected\b"))
  expect(cells.nth(4)).not_to_have_class(re.compile(r"\bselected\b"))


def test_shift_click_with_nothing_selected_selects_just_the_one_and_sets_the_anchor(page, server):
  page.goto(server.url + "/#/2024/trip")
  page.get_by_role("button", name="Select").click()
  cells = page.locator(".cell")
  cells.nth(2).click(modifiers=["Shift"])
  expect(page.locator(".selection-bar")).to_contain_text("1 selected")
  expect(cells.nth(2)).to_have_class(re.compile(r"\bselected\b"))
  cells.nth(4).click(modifiers=["Shift"])
  expect(page.locator(".selection-bar")).to_contain_text("3 selected")   # 2, 3, 4


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
  expect(page.locator("table")).to_contain_text("PreviewDNG")   # the on-disk RAW tuning cache
  page.goto(server.url + "/#!jobs")
  expect(page.get_by_role("heading", name="Background jobs")).to_be_visible()
  expect(page.locator("p.status", has_text="Scan:")).to_be_visible()      # ticket 140 status line
  expect(page.locator("p.status", has_text="Worker:")).to_be_visible()    # ticket 113 status line
  expect(page.get_by_role("columnheader", name="Last minute")).to_be_visible()   # ticket 114


def test_jobs_page_worker_status_auto_updates_from_active_to_idle(page, server):
  # ticket 140: the Jobs page polls /api/jobs on its own, so a job that starts and finishes while
  # the page is open should flip "Worker: Active" -> "Worker: Idle" without a reload.
  started, release = threading.Event(), threading.Event()

  def blocking(conn, job):
    started.set()
    assert release.wait(10)

  server.app.state.jobs.add_handler("e2e_block", blocking)
  page.goto(server.url + "/#!jobs")
  worker_status = page.locator("p.status", has_text="Worker:")
  expect(worker_status).to_contain_text("Worker: Idle")
  server.app.state.jobs.enqueue("e2e_block", target="probe")
  assert started.wait(5)
  expect(worker_status).to_contain_text("Worker: Active")
  release.set()
  expect(worker_status).to_contain_text("Worker: Idle", timeout=5000)


def test_attention_page_conflict_links_straight_to_the_photo(page, server):
  # ticket 143: photoLink(path, photoId) opens the loupe on that exact photo, not just its folder.
  ids = photo_ids(server)
  path = server.app.state.db.execute(
      "SELECT rf.path FROM photos p JOIN files rf ON rf.id = p.representative_file_id "
      "WHERE p.id = ?", (ids[0],)).fetchone()[0]
  server.app.state.db.execute("UPDATE photos SET conflict = 1 WHERE id = ?", (ids[0],))
  server.app.state.db.commit()
  page.goto(server.url + "/#!attention")
  expect(page.get_by_role("heading", name="Sidecars that disagree (1)")).to_be_visible()
  link = page.get_by_role("link", name=path)
  expect(link).to_be_visible()
  link.click()
  expect(page.locator(".loupe")).to_be_visible()
  expect(page).to_have_url(re.compile(rf"photo={ids[0]}"))


def test_jobs_page_job_list_links_the_file_to_its_photo(page, server):
  # ticket 142: the Jobs page's per-job table shows the photo's path, linked to that photo.
  ids = photo_ids(server)
  fid, path = server.app.state.db.execute(
      "SELECT id, path FROM files WHERE photo_id = ?", (ids[0],)).fetchone()
  # An unregistered kind ("e2e_failed" has no handler on any running queue) so nothing ever claims
  # and processes this job for real -- app.state.jobs is live in this test server, and enqueuing a
  # real kind like raw_render races its workers, which can claim and finish (fail, for a plain JPG)
  # the job before this test's own UPDATE lands, overwriting the 'boom' error below.
  job_id = server.app.state.jobs.enqueue("e2e_failed", fid)
  server.app.state.db.execute(
      "UPDATE jobs SET state = 'failed', error = 'boom' WHERE id = ?", (job_id,))
  server.app.state.db.commit()
  page.goto(server.url + "/#!jobs")
  # Scoped to the row carrying our error message: setup/background jobs may have already touched
  # this same file earlier (e.g. populating its thumbnails), so its path/link can appear more than
  # once in the recent-jobs list -- this is the one this test actually put there.
  row = page.locator("tr", has_text="boom")
  link = row.get_by_role("link")
  expect(link).to_have_text(path)
  link.click()
  expect(page.locator(".loupe")).to_be_visible()
  expect(page).to_have_url(re.compile(rf"photo={ids[0]}"))


def test_jobs_page_shows_a_running_scan_and_clears_it_when_done(page, server):
  # ticket 140: an interactive rescan (app.state.scanner) isn't a jobs-table row, so it's reported
  # on its own "Scan: ..." line, separate from the shared-queue "Worker: ..." line -- check it
  # drives its own Idle -> Running -> Idle transition.
  from photoapp import scan
  page.goto(server.url + "/#!jobs")
  scan_status = page.locator("p.status", has_text="Scan:")
  expect(scan_status).to_contain_text("Scan: Idle")
  server.app.state.scanner.progress = scan.Progress(
      running=True, current_dir="2024/trip",
      started_at="2000-01-01T00:00:00")
  expect(scan_status).to_contain_text("Scan: Running 2024/trip", timeout=5000)
  server.app.state.scanner.progress.running = False
  expect(scan_status).to_contain_text("Scan: Idle", timeout=5000)


def test_zoom_loads_full_size_and_toggles(page, server):
  open_loupe(page, server)
  src = page.locator(".stage img.main").get_attribute("src")
  assert "/img/Medium/" in src
  page.keyboard.press("z")
  expect(page.locator(".stage.zoomed")).to_have_count(1)
  expect(page.locator(".stage img.main")).to_have_attribute("src", re.compile("/img/Huge/"))   # swapped in once loaded
  page.keyboard.press("z")
  expect(page.locator(".stage.zoomed")).to_have_count(0)
  expect(page.locator(".stage img.main")).to_have_attribute("src", re.compile("/img/Medium/"))


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
  # Ticket 107: moving the mouse to click "Show this" (in the DNG's non-representative row)
  # hovers that row on the way, which now also asks for the DNG's own thumbnail as a row
  # preview -- a second legitimate 404 alongside the actual render triggered by the click
  # itself, both async, so give them a moment to land before clearing.
  page.locator(".files-panel").get_by_role("button", name="Show this").click()
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001.DNG")
  page.keyboard.press("g")                          # cycle back to the JPEG
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001.jpg")
  page.wait_for_timeout(600)
  page.errors.clear()      # the fake one-byte DNG legitimately has no image (404)


def test_files_panel_shows_camera_metadata(page, server):
  # tickets 084/111/156: camera (make/model), lens, focal length, aperture/shutter speed/ISO/
  # exif_date, written directly since the fixture files carry no real EXIF (083/111/156 extraction
  # is covered separately).
  ids = photo_ids(server)
  fid = server.app.state.db.execute(
      "SELECT id FROM files WHERE photo_id = ?", (ids[0],)).fetchone()[0]
  server.app.state.db.execute(
      "UPDATE files SET aperture = 2.8, shutter_speed = 0.004, iso = 400,"
      " focal_length = 50.0, focal_length_35mm = 75, camera_make = 'PENTAX',"
      " camera_model = 'PENTAX K-5',"
      " lens_model = 'smc PENTAX-DA 35mm F2.4 AL',"
      " exif_date = '2024:06:01 12:00:00' WHERE id = ?", (fid,))
  server.app.state.db.commit()
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("i")
  expect(page.locator(".files-panel .file")).to_contain_text("PENTAX K-5")
  expect(page.locator(".files-panel .file")).to_contain_text("smc PENTAX-DA 35mm F2.4 AL")
  expect(page.locator(".files-panel .file")).to_contain_text("50mm (75mm)")   # ticket 170/172
  expect(page.locator(".files-panel .file")).to_contain_text("f/2.8")
  expect(page.locator(".files-panel .file")).to_contain_text("1/250s")
  expect(page.locator(".files-panel .file")).to_contain_text("ISO 400")
  expect(page.locator(".files-panel .file")).to_contain_text("2024:06:01 12:00:00")


def test_files_panel_dedupes_the_camera_make(page, server):
  # ticket 168: the corporate part of Make is dropped and not repeated: "PENTAX Corporation" +
  # "PENTAX *ist DL" shows only the model; "OLYMPUS IMAGING CORP." + "u830" shows "OLYMPUS u830".
  ids = photo_ids(server)
  fid = server.app.state.db.execute(
      "SELECT id FROM files WHERE photo_id = ?", (ids[0],)).fetchone()[0]
  for make, model, shown, hidden in [
      ("PENTAX Corporation", "PENTAX *ist DL", "PENTAX *ist DL", "PENTAX Corporation"),
      ("OLYMPUS IMAGING CORP.", "u830", "OLYMPUS u830", "IMAGING")]:
    server.app.state.db.execute(
        "UPDATE files SET camera_make = ?, camera_model = ? WHERE id = ?", (make, model, fid))
    server.app.state.db.commit()
    page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
    expect(page.locator(".loupe")).to_be_visible()
    page.keyboard.press("i")
    panel = page.locator(".files-panel .file")
    expect(panel).to_contain_text(shown)
    expect(panel).not_to_contain_text(hidden)
    page.keyboard.press("i")   # close the panel before the next case


def test_files_panel_directory_link_only_shown_when_browsing_recursively(page, server):
  # ticket 154: the directory portion of a file's path becomes its own link, but only when the
  # current view is recursive ("this folder + subfolders") -- otherwise it's always the same
  # folder already being browsed, so it stays plain text.
  ids = photo_ids(server)   # all in 2024/trip
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")   # non-recursive
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("i")
  expect(page.locator(".files-panel .file .meta", has_text="2024/trip/IMG_0001.jpg")).to_be_visible()
  expect(page.locator(".files-panel .file .meta a")).to_have_count(0)   # no link, non-recursive


def test_files_panel_directory_link_jumps_to_that_folder_recursively(page, server):
  photos = api(server, "/api/photos?dir=2024&recursive=1&sort=name")["photos"]
  trip_photo = next(p for p in photos if p["path"].startswith("2024/trip/"))
  page.goto(f"{server.url}/#/2024?recursive=1&photo={trip_photo['id']}")
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("i")
  link = page.locator(".files-panel .file .meta").first.get_by_role("link")
  expect(link).to_have_text("2024/trip")
  link.click()
  expect(page.locator(".loupe")).to_be_hidden()   # navigating away closes the loupe
  assert "/2024/trip" in page.url and "recursive" not in page.url
  expect(page.locator(".cell")).to_have_count(6)


def test_files_panel_shows_the_file_byte_size(page, server):
  # ticket 150: a B/KB/MB-suffixed size, ~2 significant digits.
  ids = photo_ids(server)
  fid = server.app.state.db.execute(
      "SELECT id FROM files WHERE photo_id = ?", (ids[0],)).fetchone()[0]
  server.app.state.db.execute("UPDATE files SET bytesize = 2613000 WHERE id = ?", (fid,))
  server.app.state.db.commit()
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("i")
  expect(page.locator(".files-panel .file")).to_contain_text("2.5 MB")


def test_files_panel_exported_from_link_shows_the_source_path(page, server):
  # ticket 152: link text is the source file's relative path, not "<dir> (photo <id>)"; the link
  # target (dir + photo_id) is unchanged.
  ids = photo_ids(server)
  export_fid, source_fid = [
      server.app.state.db.execute(
          "SELECT id FROM files WHERE photo_id = ?", (pid,)).fetchone()[0]
      for pid in ids[:2]]
  source_path = server.app.state.db.execute(
      "SELECT path FROM files WHERE id = ?", (source_fid,)).fetchone()[0]
  server.app.state.db.execute(
      "UPDATE files SET exported_from_file_id = ? WHERE id = ?", (source_fid, export_fid))
  server.app.state.db.commit()
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("i")
  link = page.locator(".files-panel .file", has_text="exported from").get_by_role("link")
  expect(link).to_have_text(source_path)
  expect(link).to_have_attribute("href", re.compile(rf"photo={ids[1]}$"))


def test_files_panel_recenters_image_without_obstruction(page, server):
  # ticket 121: opening the files/tuning panel recenters the image so it is not obstructed
  # by the side panel. Both img.main and the flip / tuning overlay (img.tuning / img.row-preview)
  # must clear the panel.
  ids = open_loupe(page, server)
  page.set_viewport_size({"width": 1024, "height": 768})
  page.keyboard.press("i")
  expect(page.locator(".files-panel")).to_be_visible()
  img = page.locator(".stage img.main").bounding_box()
  panel = page.locator(".files-panel").bounding_box()
  assert img["x"] + img["width"] <= panel["x"] + 0.5

  # Flip / tuning overlay is also positioned within the cleared space, clearing the panel.
  rp_box = page.evaluate("""() => {
    const t = document.querySelector(".stage img.row-preview");
    t.hidden = false;
    return t.getBoundingClientRect();
  }""")
  assert rp_box["x"] + rp_box["width"] <= panel["x"] + 0.5

  page.keyboard.press("i")
  expect(page.locator(".files-panel")).to_have_count(0)


def test_crop_mode_saves_and_shades(page, server):
  # ticket 115: the Crop button opens the editor, dragging a handle shrinks the rectangle, Save
  # commits it, and the loupe then shades the cropped-out part (the grid Thumb is rendered
  # cropped server-side, covered by tests/test_crop.py).
  ids = open_loupe(page, server)
  fid = server.app.state.db.execute(
      "SELECT representative_file_id FROM photos WHERE id = ?", (ids[0],)).fetchone()[0]
  page.get_by_role("button", name="crop").click()
  expect(page.locator(".crop-editing")).to_be_visible()
  handle = page.locator(".crop-handle.se").bounding_box()
  page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
  page.mouse.down()
  page.mouse.move(handle["x"] - 80, handle["y"] - 60, steps=8)
  page.mouse.up()
  page.get_by_role("button", name="Save crop").click()
  expect(page.locator(".crop-editing")).to_have_count(0)
  crop_w = wait_for(lambda: server.app.state.db.execute(
      "SELECT crop_w FROM files WHERE id = ?", (fid,)).fetchone()[0])
  assert 0 < crop_w < 1
  expect(page.locator(".crop-shade:not([hidden])")).to_have_count(1)


def test_rotate_button_stores_rotation_and_bumps_the_image_url_revision(page, server):
  # ticket 129: the loupe's rotate button turns the representative file 90 degrees left, saves it on
  # the file, clears the cached render, and the reloaded image URL carries the new revision.
  ids = open_loupe(page, server)
  fid = server.app.state.db.execute(
      "SELECT representative_file_id FROM photos WHERE id = ?", (ids[0],)).fetchone()[0]
  page.get_by_role("button", name="⟲").click()
  rotation = wait_for(lambda: server.app.state.db.execute(
      "SELECT rotation FROM files WHERE id = ?", (fid,)).fetchone()[0])
  assert rotation == 90
  expect(page.locator("#toast")).to_contain_text("rotated")
  rev = wait_for(lambda: server.app.state.db.execute(
      "SELECT thumb_rev FROM files WHERE id = ?", (fid,)).fetchone()[0])
  page.keyboard.press("ArrowRight")
  page.keyboard.press("ArrowLeft")
  expect(page.locator(".stage img.main")).to_have_attribute("src", re.compile(rf"\?r={rev}\b"))


def test_saved_render_bumps_the_image_url_revision(page, server):
  # ticket 119: after a render-changing save, navigating away and back must request the image with
  # the file's new revision, not the browser-cached pre-save URL. Crop and raw_settings share this
  # client path (setRevision -> imgUrl); the raw_settings revision bump is unit-tested separately.
  ids = open_loupe(page, server)
  fid = server.app.state.db.execute(
      "SELECT representative_file_id FROM photos WHERE id = ?", (ids[0],)).fetchone()[0]
  page.get_by_role("button", name="crop").click()
  expect(page.locator(".crop-editing")).to_be_visible()
  handle = page.locator(".crop-handle.se").bounding_box()
  page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
  page.mouse.down()
  page.mouse.move(handle["x"] - 80, handle["y"] - 60, steps=8)
  page.mouse.up()
  page.get_by_role("button", name="Save crop").click()
  expect(page.locator("#toast")).to_contain_text("crop saved")   # the revision is set before this
  rev = wait_for(lambda: server.app.state.db.execute(
      "SELECT thumb_rev FROM files WHERE id = ?", (fid,)).fetchone()[0])
  assert rev >= 1
  page.keyboard.press("ArrowRight")
  page.keyboard.press("ArrowLeft")
  expect(page.locator(".stage img.main")).to_have_attribute("src", re.compile(rf"\?r={rev}\b"))


def test_raw_settings_controls_only_show_for_raw_files(page, server):
  # ticket 085: sliders/controls only appear for a RAW file (not the JPEG sibling).
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")   # loupe finished loading
  page.keyboard.press("i")
  expect(page.locator(".files-panel .file")).to_have_count(2)

  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")
  jpg_row = page.locator(".files-panel .file", has_text="IMG_0001.jpg")
  expect(dng_row.locator(".raw-settings")).to_be_visible()
  expect(jpg_row.locator(".raw-settings")).to_have_count(0)
  page.errors.clear()      # the fake one-byte DNG legitimately has no image (404)


def test_raw_settings_are_provisional_until_save(page, server):
  # Ticket 094: a control only updates local, pending state and asks for a provisional preview --
  # nothing is posted to the server (no toast, nothing persisted) until Save is actually clicked.
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")

  save_btn = dng_row.get_by_role("button", name="Save", exact=True)
  expect(save_btn).to_be_disabled()   # nothing pending yet

  dng_row.get_by_role("button", name="manual", exact=True).click()
  expect(dng_row.locator(".wb-multiplier")).to_have_count(3)   # R/G/B inputs appeared locally
  expect(save_btn).to_be_enabled()
  expect(dng_row.get_by_role("button", name="Discard changes")).to_be_visible()
  expect(page.locator("#toast")).not_to_have_class(re.compile("show"))   # nothing posted, no toast

  page.keyboard.press("i")   # close without saving
  page.keyboard.press("i")   # reopen: the pending change did not survive (never persisted)
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")
  expect(dng_row.get_by_role("button", name="camera", exact=True)).to_have_class(ON)
  expect(dng_row.get_by_role("button", name="Save", exact=True)).to_be_disabled()

  dng_row.get_by_role("button", name="manual", exact=True).click()
  dng_row.get_by_role("button", name="Save", exact=True).click()
  expect(page.locator("#toast")).to_contain_text("saved")
  page.keyboard.press("i")
  page.keyboard.press("i")   # reopen: this time it did survive, because Save was clicked
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")
  expect(dng_row.get_by_role("button", name="manual", exact=True)).to_have_class(ON)
  expect(dng_row.get_by_role("button", name="Reset to default")).to_be_visible()

  # a pending change can also be discarded explicitly, without closing the panel
  dng_row.get_by_role("button", name="Reset to default").click()
  expect(dng_row.get_by_role("button", name="Discard changes")).to_be_visible()
  dng_row.get_by_role("button", name="Discard changes").click()
  expect(dng_row.get_by_role("button", name="manual", exact=True)).to_have_class(ON)   # unchanged
  expect(dng_row.get_by_role("button", name="Save", exact=True)).to_be_disabled()
  page.errors.clear()      # the fake one-byte DNG legitimately has no image (404)


def test_exposure_and_shadow_sliders_preview_and_persist(page, server):
  # Ticket 109: the two new sliders, like every other control, only update provisional state and
  # request a preview until Save; on Save they round-trip through the file detail as committed.
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")

  exposure = dng_row.locator(".raw-settings input[aria-label=exposure]")
  shadow = dng_row.locator(".raw-settings input[aria-label='shadow pull']")
  expect(exposure).to_have_value("1")
  expect(shadow).to_have_value("0")
  save_btn = dng_row.get_by_role("button", name="Save", exact=True)
  expect(save_btn).to_be_disabled()

  exposure.focus()
  exposure.press("ArrowRight")          # 1.0 -> 1.05
  shadow.focus()
  for _ in range(5):
    shadow.press("ArrowRight")          # 0.0 -> 0.05
  expect(save_btn).to_be_enabled()
  save_btn.click()
  expect(page.locator("#toast")).to_contain_text("saved")

  page.keyboard.press("i")
  page.keyboard.press("i")   # reopen: both values survived, because Save was clicked
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")
  expect(dng_row.locator(".raw-settings input[aria-label=exposure]")).to_have_value("1.05")
  expect(dng_row.locator(".raw-settings input[aria-label='shadow pull']")).to_have_value("0.05")
  expect(dng_row.get_by_role("button", name="Reset to default")).to_be_visible()
  page.errors.clear()      # the fake one-byte DNG legitimately has no image (404)


def test_advanced_raw_settings_controls_preview_and_persist(page, server):
  # Ticket 112: saturation/contrast/noise/demosaic live behind the "Advanced" zipper and, like
  # every other control, only persist on Save.
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")

  advanced = dng_row.locator(".raw-settings details.advanced")
  expect(advanced).to_have_count(1)
  advanced.locator("summary").click()   # open the zipper
  saturation = advanced.locator("input[aria-label=saturation]")
  contrast = advanced.locator("input[aria-label=contrast]")
  expect(saturation).to_have_value("1")
  expect(contrast).to_have_value("1")

  save_btn = dng_row.get_by_role("button", name="Save", exact=True)
  expect(save_btn).to_be_disabled()
  saturation.focus(); saturation.press("ArrowRight")     # 1.0 -> 1.05
  contrast.focus(); contrast.press("ArrowRight")         # 1.0 -> 1.05
  advanced.locator("select[aria-label='noise reduction']").select_option("2")
  advanced.locator("select[aria-label=demosaic]").select_option("4")
  expect(save_btn).to_be_enabled()
  save_btn.click()
  expect(page.locator("#toast")).to_contain_text("saved")

  page.keyboard.press("i")
  page.keyboard.press("i")   # reopen: all four survived
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")
  advanced = dng_row.locator(".raw-settings details.advanced")
  advanced.locator("summary").click()
  expect(advanced.locator("input[aria-label=saturation]")).to_have_value("1.05")
  expect(advanced.locator("input[aria-label=contrast]")).to_have_value("1.05")
  expect(advanced.locator("select[aria-label='noise reduction']")).to_have_value("2")
  expect(advanced.locator("select[aria-label=demosaic]")).to_have_value("4")
  expect(dng_row.get_by_role("button", name="Reset to default")).to_be_visible()
  page.errors.clear()      # the fake one-byte DNG legitimately has no image (404)


def test_compare_hover_and_shift_reveal_the_provisional_overlay(page, server, monkeypatch):
  # Ticket 107: the tuned overlay is hidden by default (the committed rendering is what's shown);
  # it only appears while the user is actively asking to compare -- hovering the sliders block, or
  # holding Shift as a keyboard/touch fallback that keeps working even with a slider focused (the
  # exact case that was broken when the hotkey was the bare letter 'c', ticket 094's original
  # shape, since a focused <input type="range"> made isTyping() swallow the key).
  from photoapp import previews

  monkeypatch.setattr(previews, "embedded_preview", lambda path: Image.new("RGB", (1600, 1200), "red"))
  monkeypatch.setattr(previews, "render",
                      lambda path, settings=None, half_size=False: Image.new("RGB", (1600, 1200), "blue"))
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")

  tuning = page.locator(".stage img.tuning:not(.row-preview)")
  expect(tuning).to_be_hidden()
  slider = dng_row.locator(".raw-settings input[type=range]").first
  slider.focus()
  slider.press("ArrowRight")   # dirty the pending value (and leave the slider focused) so a
                                # provisional render is requested
  # Ticket 105: rawTuning.js tries a local LibRaw-Wasm render first, which needs a real preview
  # DNG (GET .../raw_preview_dng) -- the fake one-byte DNG here legitimately has none (a real 404,
  # logged by the browser), so it falls back to 094's network .../raw_preview path, same as before
  # 105 landed. Both the fallback's own request and the 404 that triggered it are expected.
  expect(tuning).to_have_attribute("src", re.compile(r"/api/files/\d+/raw_preview"))   # loaded...
  expect(tuning).to_be_hidden()   # ...but stays hidden: neither hover nor Shift is active yet
  page.errors.clear()

  # Shift works even with the slider itself focused -- the bug ticket 107 fixes.
  page.keyboard.down("Shift")
  expect(tuning).to_be_visible()
  page.keyboard.up("Shift")
  expect(tuning).to_be_hidden()

  # Hovering the sliders block shows it too, without any key held.
  dng_row.locator(".raw-settings").hover()
  expect(tuning).to_be_visible()
  page.locator(".hud").hover()   # move the mouse elsewhere in the viewer
  expect(tuning).to_be_hidden()


def test_hovering_a_sibling_file_row_previews_its_own_thumbnail(page, server, monkeypatch):
  # Ticket 107: hovering a non-representative file's row in the Files panel swaps in that file's
  # own thumbnail via a distinct overlay -- independent of, and not affected by, the RAW-tuning
  # compare overlay above. The camera JPEG is the representative by default (grouping.py's
  # fix_representatives/set_representative), so it's the DNG row -- the non-representative one
  # here -- that's hovered.
  from photoapp import previews

  monkeypatch.setattr(previews, "embedded_preview", lambda path: Image.new("RGB", (1600, 1200), "red"))
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")

  row_preview = page.locator(".stage img.row-preview")
  expect(row_preview).to_be_hidden()
  dng_row.hover()
  expect(row_preview).to_be_visible()
  expect(row_preview).to_have_attribute("src", re.compile(r"/img/Medium/"))
  page.locator(".hud").hover()
  expect(row_preview).to_be_hidden()


@real_dng_only
def test_local_libraw_wasm_renders_the_tuning_preview_for_a_real_raw(page, server):
  # Ticket 105: every other RAW-tuning test here uses a fake one-byte DNG, so it only ever
  # exercises the network fallback (raw_preview_dng.ensure raises Unsupported, a real 404). With
  # a real, decodable RAW file the local LibRaw-Wasm path should actually engage instead: ui.tuning
  # ends up a blob: URL, and no request ever reaches the server's network .../raw_preview endpoint.
  shutil.copy(REAL_DNG, os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"))
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)

  network_preview_requests = []
  page.on("request", lambda r: network_preview_requests.append(r.url)
          if "/raw_preview?" in r.url else None)

  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")

  tuning = page.locator(".stage img.tuning:not(.row-preview)")
  slider = dng_row.locator(".raw-settings input[type=range]").first
  slider.focus()
  slider.press("ArrowRight")
  # A generous timeout: the vendored ~1.4MB WASM module has to compile fresh in every test's own
  # browser context (no cross-test cache), which can take a while under this machine's load.
  expect(tuning).to_have_attribute("src", re.compile(r"^blob:"), timeout=30000)
  assert network_preview_requests == []


@real_dng_only
def test_libraw_wasm_exposure_matches_rawpy_on_a_real_raw(page, server):
  # Ticket 109: the vendored LibRaw-Wasm build's expShift/expCorrec/noAutoBright must mean the
  # same as rawpy's exp_shift/no_auto_bright for the same value (a linear multiplier, not stops).
  # Compare the local render's mean RGB against rawpy rendering the same preview DNG through
  # previews._postprocess_kwargs -- within a few percent, like ticket 106's parity check.
  import numpy as np
  import rawpy
  from photoapp import previews
  from photoapp import raw_preview_dng

  rel = "2024/trip/IMG_0001.DNG"
  shutil.copy(REAL_DNG, os.path.join(server.pictures, rel))
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  preview_dng = raw_preview_dng.ensure(
      server.settings.thumbs_dir, server.settings.pictures_dir, rel)

  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")

  exposure = dng_row.locator(".raw-settings input[aria-label=exposure]")
  exposure.evaluate(
      "el => { el.value = '2'; el.dispatchEvent(new Event('input', {bubbles: true})); }")
  tuning = page.locator(".stage img.tuning:not(.row-preview)")
  expect(tuning).to_have_attribute("src", re.compile(r"^blob:"), timeout=30000)
  wasm_mean = page.evaluate("""async (url) => {
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let r = 0, g = 0, b = 0; const n = d.length / 4;
    for (let i = 0; i < d.length; i += 4) { r += d[i]; g += d[i + 1]; b += d[i + 2]; }
    return [r / n, g / n, b / n];
  }""", tuning.get_attribute("src"))

  with rawpy.imread(preview_dng) as raw:
    rgb = raw.postprocess(**previews._postprocess_kwargs({"raw_exposure": 2.0}))
  rawpy_mean = rgb.reshape(-1, 3).mean(axis=0)
  diff = np.abs(np.array(wasm_mean) - rawpy_mean) / rawpy_mean
  assert diff.max() < 0.05, (wasm_mean, rawpy_mean)


@real_dng_only
def test_libraw_wasm_advanced_params_match_rawpy_on_a_real_raw(page, server):
  # Ticket 112: the advanced controls must render the same locally and server-side. Drives the
  # real RawTuningSession (mapSettings + toObjectUrl) with a combined pending dict and compares
  # its mean RGB against rawpy rendering the same preview DNG through previews' full pipeline
  # (postprocess kwargs + the post-decode contrast/saturation). Noise and demosaic are included
  # to catch a binding-name drift like the one that made `gamm` unusable here.
  import numpy as np
  import rawpy
  from photoapp import previews
  from photoapp import raw_preview_dng
  from photoapp import raw_settings

  pending = {"contrast": 0.6, "saturation": 1.5, "noise": 1, "demosaic": 11}
  rel = "2024/trip/IMG_0001.DNG"
  shutil.copy(REAL_DNG, os.path.join(server.pictures, rel))
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  file_id = server.app.state.db.execute(
      "SELECT id FROM files WHERE path = ?", (rel,)).fetchone()[0]
  preview_dng = raw_preview_dng.ensure(
      server.settings.thumbs_dir, server.settings.pictures_dir, rel)

  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  expect(page.locator(".files-panel")).to_have_count(0)
  wasm_mean = page.evaluate("""async ([fileId, values]) => {
    const m = await import('/static/rawTuning.js');
    const s = new m.RawTuningSession(fileId);
    const url = await s.render(values);
    if (!url) return null;
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let r = 0, g = 0, b = 0; const n = d.length / 4;
    for (let i = 0; i < d.length; i += 4) { r += d[i]; g += d[i + 1]; b += d[i + 2]; }
    s.dispose();
    return [r / n, g / n, b / n];
  }""", [file_id, pending])
  assert wasm_mean is not None

  settings = raw_settings.to_columns(**pending)
  with rawpy.imread(preview_dng) as raw:
    rgb = raw.postprocess(**previews._postprocess_kwargs(settings))
  rgb = previews._apply_contrast(rgb, settings["raw_contrast"])
  rgb = previews._apply_saturation(rgb, settings["raw_saturation"])
  rawpy_mean = rgb.reshape(-1, 3).mean(axis=0)
  diff = np.abs(np.array(wasm_mean) - rawpy_mean) / rawpy_mean
  assert diff.max() < 0.05, (wasm_mean, rawpy_mean)


@real_dng_only
def test_busy_indicator_shows_while_libraw_wasm_renders(page, server):
  # Ticket 108: a real RAW file so there's an actual local render to wait on (a fake DNG resolves
  # near-instantly via the network fallback, giving no real window to observe "busy" in).
  shutil.copy(REAL_DNG, os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"))
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".hud .name")).to_contain_text("+1 files")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")

  busy = page.locator(".tuning-busy")
  expect(busy).to_be_hidden()   # nothing pending yet
  slider = dng_row.locator(".raw-settings input[type=range]").first
  slider.focus()
  slider.press("ArrowRight")
  expect(busy).to_be_visible()   # shown for the span of the render
  expect(busy).to_be_hidden(timeout=30000)   # cleared once it resolves (same cold-compile budget as above)
  tuning = page.locator(".stage img.tuning:not(.row-preview)")
  expect(tuning).to_have_attribute("src", re.compile(r"^blob:"))


def test_saving_raw_settings_cache_busts_the_grid_cell_and_filmstrip_thumb(page, server):
  # Ticket 110: the grid cell's (and filmstrip's) Thumb <img> for a re-tuned RAW must pick up a
  # cache-busting param on Save, or a browser that already cached /img/Thumb/{file_id} keeps
  # showing the pre-edit thumbnail indefinitely -- the URL itself never changes, only its bytes
  # do. Uses a standalone DNG (no same-named JPG sibling, unlike every other RAW-tuning test
  # here) so it's its own Photo's representative file and actually appears in the grid/filmstrip
  # -- tuning a *non*-representative sibling's settings correctly changes nothing on screen.
  open(os.path.join(server.pictures, "2024/home", "K_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server, folder="2024/home")
  photo_id = ids[-1]   # "K_0001.DNG" sorts after the IMG_000{7,8,9}.jpg fixtures already in this folder

  page.goto(f"{server.url}/#/2024/home")
  cell_img = page.locator(f'.cell[data-id="{photo_id}"] img')
  expect(cell_img).to_be_visible()
  before = cell_img.get_attribute("src")

  page.goto(f"{server.url}/#/2024/home?photo={photo_id}")
  expect(page.locator(".hud .name")).to_contain_text("K_0001.DNG")
  page.keyboard.press("i")
  dng_row = page.locator(".files-panel .file", has_text="K_0001.DNG")
  dng_row.get_by_role("button", name="manual", exact=True).click()
  dng_row.get_by_role("button", name="Save", exact=True).click()
  expect(page.locator("#toast")).to_contain_text("saved")

  page.keyboard.press("Escape")   # back to the grid, same folder -- the cell is not rebuilt
  after = cell_img.get_attribute("src")
  # ticket 110/119: the save bumped the file's revision, so the cell's URL moved from the plain
  # (rev 0) URL to ?r=1.
  assert "?r=" not in before and after == before + "?r=1"
  page.wait_for_timeout(500)
  page.errors.clear()   # the fake one-byte DNG legitimately has no image (404): ui.img's, the
                        # grid cell's and the filmstrip's cache-busted refreshes all hit it


def test_delete_this_file_button_shows_modal_and_moves_to_trash(page, server):
  # ticket 082: per-file delete from the Files panel, gated by a modal confirmation.
  import os
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  page.keyboard.press("i")
  expect(page.locator(".files-panel .file")).to_have_count(2)

  dng_row = page.locator(".files-panel .file", has_text="IMG_0001.DNG")
  dng_row.get_by_role("button", name="Delete").click()

  expect(page.locator(".confirm-modal")).to_be_visible()
  expect(page.locator(".confirm-card")).to_contain_text("2024/trip/IMG_0001.DNG")
  expect(page.locator(".confirm-card img")).to_have_attribute("src", re.compile(r"^/img/Medium/"))

  page.locator(".confirm-card").get_by_role("button", name="Move to trash").click()
  expect(page.locator("#toast")).to_contain_text("moved to trash")
  expect(page.locator(".confirm-modal")).to_have_count(0)
  expect(page.locator(".files-panel .file", has_text="IMG_0001.DNG")).to_contain_text("MISSING")

  assert not os.path.exists(os.path.join(server.pictures, "2024/trip/IMG_0001.DNG"))
  assert os.path.isfile(os.path.join(server.pictures, ".trash/2024/trip/IMG_0001.DNG"))
  page.errors.clear()      # the fake one-byte DNG legitimately has no image (404)


def test_delete_this_file_modal_cancel_leaves_file_untouched(page, server):
  import os
  open(os.path.join(server.pictures, "2024/trip", "IMG_0001.DNG"), "wb").write(b"x")
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  page.keyboard.press("i")
  page.locator(".files-panel .file", has_text="IMG_0001.DNG").get_by_role(
      "button", name="Delete").click()
  expect(page.locator(".confirm-modal")).to_be_visible()

  page.locator(".confirm-card").get_by_role("button", name="Cancel").click()
  expect(page.locator(".confirm-modal")).to_have_count(0)
  assert os.path.isfile(os.path.join(server.pictures, "2024/trip/IMG_0001.DNG"))
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


def test_double_tap_toggles_zoom_but_a_swipe_and_a_single_tap_do_not(phone, server):
  open_loupe(phone, server)
  swipe(phone, -150, 0)
  expect(phone.locator(".hud .pos")).to_have_text("2/6")
  expect(phone.locator(".stage.zoomed")).to_have_count(0)     # the drag was not a tap
  phone.wait_for_timeout(500)
  phone.touchscreen.tap(195, 300)                              # one tap: nothing (a double tap is needed on touch)
  phone.wait_for_timeout(500)
  expect(phone.locator(".stage.zoomed")).to_have_count(0)
  phone.touchscreen.tap(195, 300)
  phone.touchscreen.tap(195, 300)                              # a double tap zooms
  expect(phone.locator(".stage.zoomed")).to_have_count(1)
  phone.touchscreen.tap(195, 300)
  phone.touchscreen.tap(195, 300)                              # and another one goes back
  expect(phone.locator(".stage.zoomed")).to_have_count(0)


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
        "All", "\u2716 Rejected", "\u2606 Unrated", "\u26052", "\u26053", "\u26054", "\u26055",
        "\u2665 Fav"]   # no \u26051
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


def test_dot_tag_filter_reveals_a_hidden_folder_and_its_implied_tag(page, server):
  # tickets 122/123: typing tag:.nu (via the free-form input) reveals the hidden folder's photo,
  # and the loupe marks the directory name as an implied tag.
  import os
  from tests.conftest import make_jpeg
  make_jpeg(os.path.join(server.pictures, "2024/trip/.nu/x.jpg"))
  server.app.state.scanner.start()
  server.app.state.scanner.wait()
  page.goto(server.url + "/#/2024/trip?recursive=1")
  expect(page.locator(".cell")).to_have_count(6)      # the hidden .nu photo is not in a normal view
  sel = page.get_by_label("tag filter")
  sel.select_option("__custom__")
  page.get_by_label("tag name").fill(".nu")
  page.get_by_label("tag name").press("Enter")
  expect(page.locator(".cell")).to_have_count(1)
  assert "filter=tag%3A.nu" in page.url
  expect(sel).to_have_value("tag:.nu")
  page.locator(".cell").first.click()
  expect(page.locator(".loupe")).to_be_visible()
  expect(page.locator(".hud .tag.implied")).to_have_text(".nu")


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
  assert kinds == ["all", "rejected", "unrated", "rating:1", "rating:2", "rating:3", "rating:4",
                    "rating:5", "fav"]
  assert page.locator(".filters button .lbl").all_inner_texts() == [
      "All", "\u2716 Rejected", "\u2606 Unrated", "\u26051", "\u26052", "\u26053", "\u26054",
      "\u26055", "\u2665 Fav"]
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
  filter_button(page, "Fav").click()          # ticket 087: promoted out of "more filters"
  expect(page.locator(".cell")).to_have_count(0)
  assert "filter=fav" in page.url
  page.get_by_label("more filters").select_option("picked")
  expect(page.locator(".cell")).to_have_count(3)          # 0002, 0004, 0005 (rating > 0)
  assert "filter=picked" in page.url


def test_star_button_cycles_exactly_at_least_at_most(page, server):
  # ticket 117: a star button cycles = -> >= -> <= -> =; a different star starts at exactly N.
  ids = photo_ids(server)
  for pid, r in ((ids[3], 2), (ids[4], 4)):
    urllib.request.urlopen(urllib.request.Request(
        f"{server.url}/api/photos/{pid}/rating", data=json.dumps({"rating": r}).encode(),
        headers={"Content-Type": "application/json"}))
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  star4 = page.locator(".filters button").nth(6)      # 0 all, 1 rejected, 2 unrated, 3..7 ★1..★5
  star4.click()                                        # =4
  expect(star4).to_have_class(ON)
  expect(page.locator(".cell")).to_have_count(2)       # IMG_0002 and IMG_0005
  assert "filter=rating%3A4" in page.url
  star4.click()                                        # >=4
  assert "filter=rating%3E%3D4" in page.url
  expect(page.locator(".cell")).to_have_count(2)
  star4.click()                                        # <=4
  assert "filter=rating%3C%3D4" in page.url
  expect(page.locator(".cell")).to_have_count(3)       # the 2- and 4-star photos, not rejected/unrated
  star4.click()                                        # back to =4
  assert "filter=rating%3A4" in page.url
  page.locator(".filters button").nth(5).click()       # a different star: exactly 3
  assert "filter=rating%3A3" in page.url
  expect(page.locator(".cell")).to_have_count(0)


def test_loupe_filter_picker_star_grid(page, server):
  # ticket 117: the loupe picker shows <= N / = N / >= N for each star value.
  open_loupe(page, server, 1)                          # IMG_0002, 4 stars
  page.locator(".hud .filter-tag").click()
  expect(page.locator(".filter-picker .filter-star-row")).to_have_count(5)
  for f in ("rating<=3", "rating:3", "rating>=3"):
    expect(page.locator(f'.filter-picker .filter-option[data-filter="{f}"]')).to_be_visible()
  expect(page.locator('.filter-picker .filter-option[data-filter="rating>=5"]')).to_be_disabled()
  page.locator('.filter-picker .filter-option[data-filter="rating>=2"]').click()
  assert "filter=rating%3E%3D2" in page.url
  expect(page.locator(".hud .filter-tag")).to_contain_text("★≥2")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0002")


def add_tag(page, server, index, tag):
  ids = open_loupe(page, server, index)
  page.keyboard.press("t")
  page.locator(".hud input.tags").fill(tag)
  page.keyboard.press("Enter")
  page.keyboard.press("Escape")
  return ids


def test_tag_filter_dropdown_lists_and_filters_by_one_tag(page, server):
  add_tag(page, server, 0, "vacation")
  add_tag(page, server, 1, "vacation")
  add_tag(page, server, 2, "family")
  page.goto(server.url + "/#/2024/trip")

  tag_select = page.get_by_label("tag filter")
  expect(tag_select.locator("option")).to_have_count(4)                 # placeholder + 2 tags + Other tag…
  assert tag_select.locator("option").all_inner_texts()[1:3] == ["family (1)", "vacation (2)"]
  assert tag_select.locator("option").all_inner_texts()[3] == "Other tag…"

  tag_select.select_option("tag:vacation")
  expect(page.locator(".cell")).to_have_count(2)
  assert "filter=tag%3Avacation" in page.url
  expect(tag_select).to_have_class(ON)

  page.reload()                                          # state is in the URL
  expect(page.locator(".cell")).to_have_count(2)
  expect(tag_select).to_have_value("tag:vacation")

  add_tag(page, server, 3, "vacation")                      # a 3rd photo joins the tag mid-session
  page.goto(server.url + "/#/2024/trip?filter=tag%3Avacation")
  expect(page.locator(".cell")).to_have_count(3)


def test_custom_tag_filter_input(page, server):
  # ticket 122: "Other tag…" opens an input to type any tag, even one not among the view's aspects.
  add_tag(page, server, 0, "vacation")
  page.goto(server.url + "/#/2024/trip")
  sel = page.get_by_label("tag filter")

  sel.select_option("__custom__")
  expect(page.locator(".confirm-modal h3")).to_have_text("Filter by tag")
  inp = page.get_by_label("tag name")
  expect(inp).to_be_focused()
  inp.fill("vacation")
  inp.press("Enter")
  expect(page.locator(".cell")).to_have_count(1)
  assert "filter=tag%3Avacation" in page.url
  expect(sel).to_have_value("tag:vacation")

  # a tag with no photos in this view still applies, and the selector shows it rather than resetting
  sel.select_option("__custom__")
  page.get_by_label("tag name").fill("no-such-tag")
  page.get_by_role("button", name="Filter").click()
  expect(page.locator(".cell")).to_have_count(0)
  assert "filter=tag%3Ano-such-tag" in page.url
  expect(sel).to_have_value("tag:no-such-tag")

  # Escape cancels and restores the selector to the still-active filter, without navigating
  sel.select_option("__custom__")
  page.get_by_label("tag name").press("Escape")
  expect(page.locator(".confirm-modal")).to_have_count(0)
  expect(sel).to_have_value("tag:no-such-tag")


def test_filter_is_kept_when_changing_folder_and_shown_in_the_loupe(page, server):
  page.goto(server.url + "/#/2024?filter=unrated")
  page.get_by_role("link", name="trip").first.click()
  assert "filter=unrated" in page.url
  expect(page.locator(".cell")).to_have_count(4)                         # 6 minus the 4-star and the reject
  page.locator(".cell").first.click()
  expect(page.locator(".loupe")).to_be_visible()
  expect(page.locator(".hud .filter-tag")).to_contain_text("Unrated")
  expect(page.locator(".hud .pos")).to_have_text("1/4")                  # navigation stays in the filtered list


# ------------------------------------------------ subdirectory-recursive grid (ticket 086)

def test_recursive_toggle_widens_the_grid_to_the_whole_subtree(page, server):
  page.goto(server.url + "/#/2024")                    # no photos directly in 2024 (only in trip/home)
  expect(page.locator(".cell")).to_have_count(0)
  toggle = page.get_by_role("button", name="This folder", exact=True)
  expect(toggle).to_be_visible()

  toggle.click()
  expect(page.get_by_role("button", name="This folder + subfolders")).to_be_visible()
  expect(page.locator(".cell")).to_have_count(9)        # trip's 6 + home's 3
  assert "recursive=1" in page.url

  page.reload()                                          # state is in the URL
  expect(page.locator(".cell")).to_have_count(9)
  page.get_by_role("button", name="This folder + subfolders").click()
  expect(page.locator(".cell")).to_have_count(0)
  assert "recursive" not in page.url


def test_recursive_grid_shows_the_subfolder_path_on_the_cell_and_in_the_loupe(page, server):
  page.goto(server.url + "/#/2024?recursive=1")
  expect(page.locator(".cell")).to_have_count(9)
  expect(page.locator(".cell").first).to_have_attribute("title", re.compile(r"^2024/"))
  page.locator(".cell").first.click()
  expect(page.locator(".loupe")).to_be_visible()
  expect(page.locator(".hud .name")).to_contain_text("2024/")


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


def test_delete_flow_moves_rejected_photos_to_trash(page, server):
  # ticket 072: Delete only shows on the Rejected filter, review screen shows the rejected set,
  # confirming moves every file to Pictures/.trash and the photo leaves the rejected view.
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator("a.danger")).to_have_count(0)   # not shown outside the Rejected filter
  page.goto(server.url + "/#/2024/trip?filter=rejected")
  expect(page.locator(".cell")).to_have_count(1)                            # only IMG_0003

  page.locator("a.danger").click()
  expect(page.locator("h2")).to_have_text("Delete rejected photos")
  expect(page.locator(".review-grid .cell")).to_have_count(1)
  expect(page.locator(".review-grid img")).to_have_attribute(
      "src", re.compile(r"^/img/Medium/"))
  confirm = page.locator(".delete-confirm button.danger")
  expect(confirm).to_have_text("Move 1 photo(s) to trash")

  confirm.click()
  expect(page.locator("#toast")).to_contain_text("moved 1 photo(s) to trash")
  expect(page).to_have_url(re.compile(r"filter=rejected"))
  expect(page.locator(".cell")).to_have_count(0)                            # gone from the view

  assert not os.path.exists(os.path.join(server.pictures, "2024/trip/IMG_0003.jpg"))
  assert os.path.isfile(os.path.join(server.pictures, ".trash/2024/trip/IMG_0003.jpg"))
  assert not os.path.exists(os.path.join(server.pictures, "2024/trip/IMG_0003.jpg.xmp"))
  assert os.path.isfile(os.path.join(server.pictures, ".trash/2024/trip/IMG_0003.jpg.xmp"))


def test_delete_review_screen_with_nothing_rejected(page, server):
  page.goto(server.url + "/#!delete-review?dir=2024/home")   # nothing rejected there
  expect(page.locator(".status")).to_contain_text("No rejected photos")
  expect(page.locator(".review-grid")).to_have_count(0)


def test_delete_review_keeps_the_subfolders_mode(page, server):
  # ticket 120: the Delete link from a recursive Rejected view must keep that mode on the
  # confirmation page, or the rejected photo in the subfolder silently disappears.
  page.goto(server.url + "/#/2024?filter=rejected&recursive=1")
  expect(page.locator(".cell")).to_have_count(1)          # IMG_0003 in 2024/trip
  page.locator("a.danger").click()
  expect(page.locator("h2")).to_have_text("Delete rejected photos")
  expect(page.locator(".review-grid .cell")).to_have_count(1)
  expect(page.locator(".status")).to_contain_text("and subfolders")
  back = page.get_by_role("link", name="← back to Rejected")
  assert "recursive=1" in back.get_attribute("href")


def test_delete_review_reached_by_the_subfolders_toggle(page, server):
  # ticket 124: exercise the toggle the user actually clicks, not just a hand-written recursive URL.
  page.goto(server.url + "/#/2024?filter=rejected")
  expect(page.locator(".cell")).to_have_count(0)          # nothing rejected directly in 2024
  page.get_by_role("button", name="This folder", exact=True).click()
  expect(page.locator(".cell")).to_have_count(1)          # IMG_0003 appears from 2024/trip
  page.locator("a.danger").click()
  expect(page.locator("h2")).to_have_text("Delete rejected photos")
  expect(page.locator(".review-grid .cell")).to_have_count(1)
  expect(page.locator(".status")).to_contain_text("and subfolders")


def test_selection_delete_review_keeps_the_recursive_scope(page, server):
  # ticket 124: the selection hand-off in a recursive view must still list the hand-picked photo.
  page.goto(server.url + "/#/2024?filter=rejected&recursive=1")
  expect(page.locator(".cell")).to_have_count(1)
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").first.click()
  page.locator(".selection-bar").get_by_role("button", name="Delete").click()
  expect(page.locator("h2")).to_have_text("Delete selected photos")
  expect(page.locator(".review-grid .cell")).to_have_count(1)


def test_delete_review_lists_every_rejected_photo_across_pages(page, server):
  # ticket 124: the header flow used to fetch only the first 1000-photo page, so rejected Photos
  # past it (typically the ones deeper in a recursive scope) silently vanished from the review.
  # The __pageSize test hook makes a two-photo page enough to reproduce.
  ids = photo_ids(server)
  reject(server, ids[0])
  reject(server, ids[3])
  reject(server, ids[4])                                  # plus 0003 already rejected: four total
  page.add_init_script("window.__pageSize = 2")           # test hook: pages of two
  page.goto(server.url + "/#/2024?filter=rejected&recursive=1")
  page.locator("a.danger").click()
  expect(page.locator("h2")).to_have_text("Delete rejected photos")
  expect(page.locator(".review-grid .cell")).to_have_count(4)
  expect(page.locator(".status")).to_contain_text("4 rejected photo(s)")


# --- batch delete from a grid selection (ticket 088) ------------------------------------------

def reject(server, pid):
  urllib.request.urlopen(urllib.request.Request(
      f"{server.url}/api/photos/{pid}/rating", data=json.dumps({"rating": -1}).encode(),
      headers={"Content-Type": "application/json"}))


def test_selection_delete_moves_only_the_selected_photos_and_returns_to_the_origin_view(page, server):
  # The user's own example: filter rejected -> select a few -> Delete trashes only those, not
  # the whole folder or the whole filtered view. Trashing is rating-gated (trash.py), so the
  # selection bar's Delete button only appears at all when filter=rejected (see grid.js).
  ids = photo_ids(server)
  reject(server, ids[3])
  reject(server, ids[4])                       # 0003 (already rejected), 0004, 0005 now rejected
  page.goto(server.url + "/#/2024/trip?filter=rejected")
  expect(page.locator(".cell")).to_have_count(3)
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").nth(1).click()
  page.locator(".cell").nth(2).click()                              # IMG_0004 and IMG_0005
  page.locator(".selection-bar").get_by_role("button", name="Delete").click()

  expect(page.locator("h2")).to_have_text("Delete selected photos")
  expect(page.locator(".review-grid .cell")).to_have_count(2)
  confirm = page.locator(".delete-confirm button.danger")
  expect(confirm).to_have_text("Move 2 photo(s) to trash")
  confirm.click()

  expect(page.locator("#toast")).to_contain_text("moved 2 photo(s) to trash")
  expect(page).to_have_url(re.compile(r"filter=rejected"))            # back to the origin view
  expect(page.locator(".cell")).to_have_count(1)                      # 0003 remains (still rejected)
  assert names_in_grid(page) == ["IMG_0003.jpg"]
  expect(page.locator(".selection-bar")).to_have_count(0)             # selection cleared by the reload

  for n in (4, 5):
    assert not os.path.exists(os.path.join(server.pictures, f"2024/trip/IMG_000{n}.jpg"))
    assert os.path.isfile(os.path.join(server.pictures, f".trash/2024/trip/IMG_000{n}.jpg"))
  assert os.path.isfile(os.path.join(server.pictures, "2024/trip/IMG_0003.jpg"))


def test_selection_delete_button_only_offered_on_the_rejected_filter(page, server):
  page.goto(server.url + "/#/2024/trip")                              # filter=all
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").first.click()
  expect(page.locator(".selection-bar")).to_be_visible()
  expect(page.locator(".selection-bar").get_by_role("button", name="Delete")).to_have_count(0)


def test_selection_delete_review_back_link_deletes_nothing(page, server):
  page.goto(server.url + "/#/2024/trip?filter=rejected")
  expect(page.locator(".cell")).to_have_count(1)                      # IMG_0003
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").first.click()
  page.locator(".selection-bar").get_by_role("button", name="Delete").click()
  expect(page.locator("h2")).to_have_text("Delete selected photos")

  page.get_by_role("link", name="← back").click()
  expect(page).to_have_url(re.compile(r"filter=rejected"))
  expect(page.locator(".cell")).to_have_count(1)                      # nothing was moved
  assert os.path.isfile(os.path.join(server.pictures, "2024/trip/IMG_0003.jpg"))


# --------------------------------------------------------- export action (ticket 089) ---------

def exported_root(server):
  return os.path.join(server.pictures, "Exported")   # ticket 096/098: inside the library now


def test_export_button_prefills_target_and_writes_mirrored_jpegs(page, server):
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  page.get_by_role("button", name="Export", exact=True).click()

  expect(page.locator("h3")).to_have_text("Export 6 photo(s)")
  target_input = page.locator(".export-target")
  expect(target_input).to_have_value(os.path.join(exported_root(server), "2024/trip"))

  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("queued 6 for export")

  assert server.app.state.jobs.wait_idle(10)
  out = os.path.join(exported_root(server), "2024/trip")
  assert len(os.listdir(out)) == 6
  with Image.open(os.path.join(out, "IMG_0001.jpg")) as im:
    assert im.format == "JPEG"


def test_export_button_exports_only_the_selection(page, server):
  page.goto(server.url + "/#/2024/trip")   # unfiltered: all 6 cells, in name/date order 0001..0006
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").nth(1).click()
  page.locator(".cell").nth(3).click()                                # IMG_0002, IMG_0004
  page.get_by_role("button", name="Export", exact=True).click()
  expect(page.locator("h3")).to_have_text("Export 2 photo(s)")

  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("queued 2 for export")

  assert server.app.state.jobs.wait_idle(10)
  out = os.path.join(exported_root(server), "2024/trip")
  assert sorted(os.listdir(out)) == ["IMG_0002.jpg", "IMG_0004.jpg"]  # only the selection


def test_export_applies_the_file_crop_end_to_end(page, server):
  # ticket 125: a cropped photo exports the crop, not the full frame, through the real UI/job.
  from photoapp import crop
  photo = api(server, "/api/photos?dir=2024/trip&sort=name")["photos"][0]   # IMG_0001, 1600x1067
  urllib.request.urlopen(urllib.request.Request(
      f"{server.url}/api/files/{photo['file_id']}/crop",
      data=json.dumps({"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}).encode(),
      headers={"Content-Type": "application/json"}))
  page.goto(server.url + "/#/2024/trip")
  page.get_by_role("button", name="Export", exact=True).click()
  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("queued 6 for export")

  assert server.app.state.jobs.wait_idle(10)
  expected = crop.pixel_box(crop.to_columns(0.25, 0.25, 0.5, 0.5), 1600, 1067)[2:]
  with Image.open(os.path.join(exported_root(server), "2024/trip", "IMG_0001.jpg")) as im:
    assert im.format == "JPEG" and im.size == expected


def test_move_to_folder_moves_the_selection_to_an_existing_folder(page, server):
  # ticket 147: an existing target folder moves straight through, no create-confirm modal.
  d = server.pictures
  page.goto(server.url + "/#/2024/trip")
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").nth(1).click()
  page.locator(".cell").nth(3).click()                                # IMG_0002, IMG_0004
  page.locator(".selection-bar").get_by_role("button", name="Move to folder").click()
  input_box = page.locator(".confirm-card input")
  expect(input_box).to_have_value("2024/trip")                        # preselected with the dir
  input_box.fill("2024/home")
  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("moved 2")
  expect(page.locator(".confirm-modal")).to_have_count(0)             # no create-folder modal
  expect(page.locator(".selection-bar")).to_have_count(0)             # selection cleared

  assert not os.path.exists(os.path.join(d, "2024/trip/IMG_0002.jpg"))
  assert os.path.isfile(os.path.join(d, "2024/home/IMG_0002.jpg"))
  assert os.path.isfile(os.path.join(d, "2024/home/IMG_0004.jpg"))
  expect(page.locator(".cell")).to_have_count(4)                      # 6 - 2 moved away


def test_move_to_folder_confirms_creating_a_new_folder(page, server):
  d = server.pictures
  page.goto(server.url + "/#/2024/trip")
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").first.click()
  page.locator(".selection-bar").get_by_role("button", name="Move to folder").click()
  page.locator(".confirm-card input").fill("2024/keepers")
  page.locator(".confirm-card button.danger").click()
  expect(page.get_by_role("heading", name="Create this folder?")).to_be_visible()
  assert not os.path.isdir(os.path.join(d, "2024/keepers"))           # not created yet

  page.get_by_role("button", name="Create and move").click()
  expect(page.locator("#toast")).to_contain_text("moved 1")
  assert os.path.isdir(os.path.join(d, "2024/keepers"))
  assert os.path.isfile(os.path.join(d, "2024/keepers/IMG_0001.jpg"))
  expect(page.locator(".cell")).to_have_count(5)


def test_move_to_folder_cancel_at_create_confirm_moves_nothing(page, server):
  d = server.pictures
  page.goto(server.url + "/#/2024/trip")
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").first.click()
  page.locator(".selection-bar").get_by_role("button", name="Move to folder").click()
  page.locator(".confirm-card input").fill("2024/never")
  page.locator(".confirm-card button.danger").click()
  expect(page.get_by_role("heading", name="Create this folder?")).to_be_visible()
  page.get_by_role("button", name="Cancel").click()
  expect(page.locator(".confirm-modal")).to_have_count(0)

  assert not os.path.isdir(os.path.join(d, "2024/never"))
  assert os.path.isfile(os.path.join(d, "2024/trip/IMG_0001.jpg"))    # untouched
  expect(page.locator(".cell")).to_have_count(6)                      # nothing removed from view


def test_rename_button_renames_the_folder_and_follows_it(page, server):
  # ticket 155: only offered on the unfiltered ("all") view.
  d = server.pictures
  page.goto(server.url + "/#/2024/trip")
  expect(page.get_by_role("button", name="Rename")).to_be_visible()
  page.get_by_role("button", name="Rename", exact=True).click()
  expect(page.locator(".confirm-card input")).to_have_value("2024/trip")
  page.locator(".confirm-card input").fill("2024/vacation")
  page.locator(".confirm-card button.danger").click()
  expect(page.get_by_role("heading", name="Rename this folder?")).to_be_visible()
  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("renamed to 2024/vacation")

  assert not os.path.isdir(os.path.join(d, "2024/trip"))
  assert os.path.isfile(os.path.join(d, "2024/vacation/IMG_0001.jpg"))
  expect(page).to_have_url(re.compile(r"/2024/vacation$"))   # followed to the new folder
  expect(page.locator(".cell")).to_have_count(6)


def test_rename_button_refuses_an_existing_target(page, server):
  d = server.pictures
  page.goto(server.url + "/#/2024/trip")
  page.get_by_role("button", name="Rename", exact=True).click()
  page.locator(".confirm-card input").fill("2024/home")   # already exists
  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("already exists")
  expect(page.get_by_role("heading", name="Rename this folder?")).to_have_count(0)

  assert os.path.isdir(os.path.join(d, "2024/trip"))       # untouched
  assert os.path.isdir(os.path.join(d, "2024/home"))


def test_rename_button_hidden_under_a_rating_filter(page, server):
  page.goto(server.url + "/#/2024/trip?filter=rejected")
  expect(page.get_by_role("button", name="Rename")).to_have_count(0)


def test_loupe_export_button_exports_only_that_photo(page, server):
  # The loupe's Export button acts as if this one photo were the only selection.
  open_loupe(page, server, index=1)                                        # IMG_0002
  expect(page.locator(".hud").get_by_role("button", name="export", exact=True)).to_have_count(0)
  page.locator(".hud button.debug-menu").click()                          # ticket 196: export lives in the menu
  page.locator(".actions-modal .menu-item", has_text="Export").click()
  expect(page.locator("h3")).to_have_text("Export 1 photo(s)")
  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("queued 1 for export")

  assert server.app.state.jobs.wait_idle(10)
  out = os.path.join(exported_root(server), "2024/trip")
  assert sorted(os.listdir(out)) == ["IMG_0002.jpg"]                       # just this photo


def test_export_refuses_a_target_inside_the_library(page, server):
  page.goto(server.url + "/#/2024/trip")
  page.get_by_role("button", name="Export", exact=True).click()
  page.locator(".export-target").fill(server.pictures)
  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("cannot be inside")
  expect(page.locator(".confirm-modal")).to_be_visible()               # stays open, nothing queued
  page.errors.clear()   # the 400 is the expected outcome of this test, not a real page error


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
                                     "rating:2": "0", "rating:3": "0", "rating:4": "1", "rating:5": "0",
                                     "fav": "0"}
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
  expect(page.locator(".loupe")).to_be_hidden()                           # hashchange closes it...
  assert page.evaluate("window.__preloadedUrls()") == []                  # ...which releases everything


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


# --- the scrollable, tappable thumbnail strip (ticket 059) ----------------------------------------

def big_folder_server(tmp_path, count=300):
  """A server whose library also has a folder 'many' with `count` tiny photos."""
  import os
  from PIL import Image
  from tests.e2e.harness import Server
  srv = Server(tmp_path)
  folder = os.path.join(srv.pictures, "many")
  os.makedirs(folder)
  for i in range(count):
    Image.new("RGB", (16, 10), (i % 256, (i * 7) % 256, 90)).save(os.path.join(folder, f"P_{i + 1:04d}.jpg"))
  return srv.start()


def strip_metrics(page):
  return page.evaluate("""() => {
    const s = document.querySelector('.filmstrip');
    return {scrollLeft: s.scrollLeft, clientWidth: s.clientWidth, scrollWidth: s.scrollWidth,
            innerWidth: document.querySelector('.strip-inner').offsetWidth,
            nodes: s.querySelectorAll('.thumb').length,
            imgs: s.querySelectorAll('.thumb img').length,
            currentCenter: (() => { const c = s.querySelector('.thumb.current'); if (!c) return null;
                                    const r = c.getBoundingClientRect(), b = s.getBoundingClientRect();
                                    return r.left + r.width / 2 - (b.left + b.width / 2); })()};
  }""")


def open_many(page, srv, position):
  ids = [p["id"] for p in api(srv, "/api/photos?dir=many&sort=name&limit=1000")["photos"]]
  page.goto(f"{srv.url}/#/many?photo={ids[position]}")
  expect(page.locator(".loupe")).to_be_visible()
  return ids


def test_strip_covers_the_whole_folder_with_few_dom_nodes_and_jumps_on_click(browser, tmp_path):
  srv = big_folder_server(tmp_path)
  try:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    open_many(page, srv, 0)
    expect(page.locator(".hud .pos")).to_have_text("1/300")
    page.wait_for_function("document.querySelector('.filmstrip').querySelectorAll('.thumb').length > 5")
    m = strip_metrics(page)
    assert m["innerWidth"] >= 300 * 88                                    # the strip is as long as the folder
    assert 10 <= m["nodes"] <= 60                                          # but only the nodes near the view exist
    page.evaluate("document.querySelector('.filmstrip').scrollLeft = document.querySelector('.filmstrip').scrollWidth")
    expect(page.locator('.filmstrip .thumb[data-index="299"]')).to_have_count(1)
    assert strip_metrics(page)["nodes"] <= 60                              # still few after scrolling to the end
    page.locator('.filmstrip .thumb[data-index="299"]').click()
    expect(page.locator(".hud .pos")).to_have_text("300/300")              # jump from far away
    expect(page.locator(".filmstrip .thumb.current")).to_have_attribute("data-index", "299")
    ctx.close()
  finally:
    srv.stop()


def test_current_thumbnail_stays_centered_when_the_photo_changes(browser, tmp_path):
  srv = big_folder_server(tmp_path)
  try:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    open_many(page, srv, 150)
    expect(page.locator(".hud .pos")).to_have_text("151/300")
    page.wait_for_timeout(700)
    assert abs(strip_metrics(page)["currentCenter"]) < 60
    for _ in range(3):
      page.keyboard.press("ArrowRight")
    expect(page.locator(".hud .pos")).to_have_text("154/300")
    page.wait_for_timeout(900)                                             # the smooth scroll finishes
    assert abs(strip_metrics(page)["currentCenter"]) < 60
    page.keyboard.press("End")
    expect(page.locator(".hud .pos")).to_have_text("300/300")
    page.wait_for_timeout(900)
    assert strip_metrics(page)["scrollLeft"] > 20000                       # scrolled to the far end for us
    ctx.close()
  finally:
    srv.stop()


def test_scrolling_the_strip_by_hand_does_not_change_the_photo_or_get_undone(browser, tmp_path):
  srv = big_folder_server(tmp_path)
  try:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    open_many(page, srv, 150)
    page.wait_for_timeout(700)
    box = page.locator(".filmstrip").bounding_box()
    page.mouse.move(box["x"] + 400, box["y"] + 30)
    before = strip_metrics(page)["scrollLeft"]
    page.mouse.wheel(0, 900)                                               # a vertical wheel scrolls it sideways
    page.wait_for_function(f"document.querySelector('.filmstrip').scrollLeft > {before + 500}")
    expect(page.locator(".hud .pos")).to_have_text("151/300")              # the photo did not change
    scrolled = strip_metrics(page)["scrollLeft"]
    page.wait_for_timeout(500)
    assert abs(strip_metrics(page)["scrollLeft"] - scrolled) < 5           # and nothing scrolled it back
    # a thumbnail on screen, far from the current one, is clickable
    target = page.locator(".filmstrip .thumb").evaluate_all(
        "els => els.map(e => [Number(e.dataset.index), e.getBoundingClientRect().left]).filter(x => x[1] > 300 && x[1] < 900)")[0][0]
    page.locator(f'.filmstrip .thumb[data-index="{target}"]').click()
    expect(page.locator(".hud .pos")).to_have_text(f"{target + 1}/300")
    ctx.close()
  finally:
    srv.stop()


def test_fast_scrolling_does_not_request_hundreds_of_thumbnails(browser, tmp_path):
  srv = big_folder_server(tmp_path)
  try:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    open_many(page, srv, 0)
    page.wait_for_timeout(800)
    thumbs = []
    page.on("request", lambda r: thumbs.append(r.url) if "/img/Thumb/" in r.url else None)
    # sweep the whole strip in about 0.6 s, then stop at the far end
    page.evaluate("""async () => {
      const s = document.querySelector('.filmstrip');
      for (let x = 0; x <= s.scrollWidth; x += 700) { s.scrollLeft = x; await new Promise(r => setTimeout(r, 15)); }
    }""")
    page.wait_for_timeout(900)                                             # scrolling paused: the view's images load
    assert len(thumbs) < 60, len(thumbs)                                   # not the ~300 that were swept past
    m = strip_metrics(page)
    assert m["imgs"] >= 5                                                  # but the visible ones do load
    ctx.close()
  finally:
    srv.stop()


def test_strip_follows_the_filter_and_photos_leaving_and_returning(page, server):
  page.goto(server.url + "/#/2024/trip?filter=unrated")
  expect(page.locator(".cell")).to_have_count(4)
  page.locator(".cell").first.click()
  expect(page.locator(".loupe")).to_be_visible()
  page.wait_for_function("document.querySelectorAll('.filmstrip .thumb').length === 4")
  assert strip_metrics(page)["innerWidth"] >= 4 * 88
  page.keyboard.press("3")                                                 # the photo leaves the filter
  expect(page.locator(".hud .pos")).to_have_text("1/3")
  page.wait_for_function("document.querySelectorAll('.filmstrip .thumb').length === 3")
  assert strip_metrics(page)["innerWidth"] < 4 * 88 + 10
  expect(page.locator(".filmstrip .thumb.current")).to_have_attribute("data-index", "0")
  page.keyboard.press("u")                                                 # and comes back
  page.wait_for_function("document.querySelectorAll('.filmstrip .thumb').length === 4")
  expect(page.locator(".hud .pos")).to_have_text("1/4")


def test_strip_is_slim_visible_and_tappable_on_a_phone(browser, tmp_path):
  srv = big_folder_server(tmp_path, count=60)
  try:
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, has_touch=True, is_mobile=True, device_scale_factor=2)
    page = ctx.new_page()
    open_many(page, srv, 10)
    expect(page.locator(".filmstrip")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    hud = page.locator(".hud").bounding_box()
    assert hud["y"] + hud["height"] <= 801                                  # the buttons are still on screen
    thumb = page.locator(".filmstrip .thumb").first.bounding_box()
    assert thumb["height"] >= 43
    page.wait_for_timeout(700)
    target = page.locator(".filmstrip .thumb").evaluate_all(
        "els => els.map(e => [Number(e.dataset.index), e.getBoundingClientRect().left]).filter(x => x[1] > 20 && x[1] < 330 && x[0] !== 10)")[0][0]
    page.locator(f'.filmstrip .thumb[data-index="{target}"]').tap()
    expect(page.locator(".hud .pos")).to_have_text(f"{target + 1}/60")
    # swiping the strip sideways scrolls it and leaves the photo alone
    strip = page.locator(".filmstrip").bounding_box()
    y = strip["y"] + strip["height"] / 2
    before = strip_metrics(page)["scrollLeft"]
    page.mouse.move(strip["x"] + 300, y)
    page.mouse.down()
    for i in range(1, 9):
      page.mouse.move(strip["x"] + 300 - i * 25, y)
    page.mouse.up()
    expect(page.locator(".hud .pos")).to_have_text(f"{target + 1}/60")
    ctx.close()
  finally:
    srv.stop()


# --- pinch zoom and pan on tablets and phones (ticket 060) --------------------------------------

class Touch:
  """Multi-touch through the DevTools protocol (Chrome turns it into Pointer Events)."""

  def __init__(self, page):
    self.cdp = page.context.new_cdp_session(page)

  def send(self, kind, points):
    self.cdp.send("Input.dispatchTouchEvent", {
        "type": kind, "touchPoints": [{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(points)]})

  def pinch(self, page, center, start_gap, end_gap, steps=10, vertical=False):
    cx, cy = center

    def pts(gap):
      return [(cx, cy - gap / 2), (cx, cy + gap / 2)] if vertical else [(cx - gap / 2, cy), (cx + gap / 2, cy)]

    self.send("touchStart", pts(start_gap))
    for i in range(1, steps + 1):
      self.send("touchMove", pts(start_gap + (end_gap - start_gap) * i / steps))
    self.send("touchEnd", [])
    page.wait_for_timeout(120)

  def drag(self, page, start, end, steps=10):
    self.send("touchStart", [start])
    for i in range(1, steps + 1):
      self.send("touchMove", [(start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps)])
    self.send("touchEnd", [])
    page.wait_for_timeout(120)


def zoom_state(page):
  return page.evaluate("""() => {
    const img = document.querySelector('.stage img.main');
    const m = new DOMMatrix(getComputedStyle(img).transform);
    const r = document.querySelector('.stage').getBoundingClientRect();
    return {scale: m.a, x: m.e, y: m.f, W: parseFloat(img.style.width) || 0, H: parseFloat(img.style.height) || 0,
            SW: r.width, SH: r.height, left: r.left, top: r.top,
            zoomed: document.querySelector('.stage').classList.contains('zoomed'),
            pageScale: window.visualViewport.scale};
  }""")


def fit_of(z):
  return min(z["SW"] / z["W"], z["SH"] / z["H"])


def test_zoom_math_pure_functions_in_the_browser(page, server):
  page.goto(server.url + "/")
  r = page.evaluate("""async () => {
    const z = await import('/static/zoom.js');
    const out = {};
    out.fit = z.fitScale(4000, 2000, 1000, 800);                       // width limits: 0.25
    out.fitTall = z.fitScale(2000, 4000, 1000, 800);                   // height limits: 0.2
    out.small = z.clampOffset(-999, -999, 1, 600, 400, 1000, 800);     // smaller than the stage: centered
    out.big = z.clampOffset(50, 50, 1, 3000, 2000, 1000, 800);         // larger: cannot leave a gap on the left/top
    out.bigFar = z.clampOffset(-9999, -9999, 1, 3000, 2000, 1000, 800);// ... nor on the right/bottom
    out.bigOk = z.clampOffset(-500, -300, 1, 3000, 2000, 1000, 800);
    const a = z.zoomAround(-500, -300, 1, 2, 400, 300);                // the point (400,300) stays put
    out.anchorBefore = [(400 - -500) / 1, (300 - -300) / 1];
    out.anchorAfter = [(400 - a.x) / 2, (300 - a.y) / 2];
    out.max = z.MAX_SCALE;
    return out;
  }""")
  assert (r["fit"], round(r["fitTall"], 6)) == (0.25, 0.2)
  assert (r["small"]["x"], r["small"]["y"]) == (200, 200)
  assert (r["big"]["x"], r["big"]["y"]) == (0, 0)
  assert (r["bigFar"]["x"], r["bigFar"]["y"]) == (1000 - 3000, 800 - 2000)
  assert (r["bigOk"]["x"], r["bigOk"]["y"]) == (-500, -300)
  assert r["anchorBefore"] == r["anchorAfter"] and r["max"] == 4


def test_pinch_out_zooms_the_photo_around_the_fingers_and_never_the_page(phone, server):
  open_loupe(phone, server)
  phone.wait_for_timeout(600)
  touch = Touch(phone)
  hud_before = phone.locator(".hud").bounding_box()
  strip_before = phone.locator(".filmstrip").bounding_box()
  mid = (150, 300)
  touch.pinch(phone, mid, 60, 260)
  z = zoom_state(phone)
  fit = fit_of(z)
  assert z["zoomed"] and fit * 1.5 < z["scale"] <= 4, (z, fit)
  # the point of the photo that was under the midpoint is still under it
  x0, y0 = (z["SW"] - z["W"] * fit) / 2, (z["SH"] - z["H"] * fit) / 2       # where the fitted photo sat
  before = ((mid[0] - z["left"] - x0) / fit, (mid[1] - z["top"] - y0) / fit)
  after = ((mid[0] - z["left"] - z["x"]) / z["scale"], (mid[1] - z["top"] - z["y"]) / z["scale"])
  assert abs(before[0] - after[0]) < 4 and abs(before[1] - after[1]) < 4, (before, after)
  assert z["pageScale"] == 1                                                 # the browser did not zoom the page
  assert phone.locator(".hud").bounding_box() == hud_before                  # buttons untouched
  assert phone.locator(".filmstrip").bounding_box() == strip_before
  assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
  expect(phone.locator(".stage img.main")).to_have_attribute("src", re.compile("/img/Huge/"))


def test_pinch_in_back_to_fit_leaves_the_zoom(phone, server):
  open_loupe(phone, server)
  phone.wait_for_timeout(500)
  touch = Touch(phone)
  touch.pinch(phone, (195, 300), 60, 300)
  assert zoom_state(phone)["zoomed"]
  touch.pinch(phone, (195, 300), 300, 40)                                    # pinch in past the fitted size
  expect(phone.locator(".stage.zoomed")).to_have_count(0)
  expect(phone.locator(".stage img.main")).to_have_attribute("src", re.compile("/img/Medium/"))
  assert phone.locator(".stage img.main").evaluate("e => e.style.transform") == ""


def test_pinch_is_capped_at_four_times_the_original_and_never_below_fit(phone, server):
  open_loupe(phone, server)
  phone.wait_for_timeout(500)
  touch = Touch(phone)
  touch.pinch(phone, (195, 300), 20, 380, steps=14)
  touch.pinch(phone, (195, 300), 20, 380, steps=14)
  touch.pinch(phone, (195, 300), 20, 380, steps=14)
  z = zoom_state(phone)
  assert 3.9 <= z["scale"] <= 4.0001, z


def test_dragging_pans_a_zoomed_photo_but_never_out_of_view(phone, server):
  open_loupe(phone, server)
  phone.wait_for_timeout(500)
  phone.keyboard.press("z")                                                  # 100%
  expect(phone.locator(".stage.zoomed")).to_have_count(1)
  touch = Touch(phone)
  z0 = zoom_state(phone)
  touch.drag(phone, (300, 400), (100, 300))
  z1 = zoom_state(phone)
  assert abs((z1["x"] - z0["x"]) - (-200)) < 2 and abs((z1["y"] - z0["y"]) - (-100)) < 2, (z0, z1)   # it followed the finger
  right, bottom = z0["left"] + z0["SW"] - 10, z0["top"] + z0["SH"] - 10       # corners of the stage (inside it)
  left, top = z0["left"] + 10, z0["top"] + 10
  for _ in range(4):                                                         # drag far more than the photo is wide
    touch.drag(phone, (right, bottom), (left, top), steps=6)
  z2 = zoom_state(phone)
  w, h = z2["W"] * z2["scale"], z2["H"] * z2["scale"]
  assert abs(z2["x"] - (z2["SW"] - w)) < 1 and abs(z2["y"] - (z2["SH"] - h)) < 1, z2   # stopped exactly at the far edges
  for _ in range(4):
    touch.drag(phone, (left, top), (right, bottom), steps=6)
  z3 = zoom_state(phone)
  assert abs(z3["x"]) < 1 and abs(z3["y"]) < 1, z3                             # and exactly at the near edges
  expect(phone.locator(".hud .pos")).to_have_text("1/6")                     # panning never navigates


def test_double_tap_zooms_to_the_tapped_point(phone, server):
  open_loupe(phone, server)
  phone.wait_for_timeout(500)
  tap = (300, 350)
  phone.touchscreen.tap(*tap)
  phone.touchscreen.tap(*tap)
  expect(phone.locator(".stage.zoomed")).to_have_count(1)
  z = zoom_state(phone)
  assert abs(z["scale"] - 1) < 0.01                                          # 100% of the original pixels
  # the point of the photo under the finger did not move on screen: it is under the finger now
  fit = fit_of(z)
  x0, y0 = (z["SW"] - z["W"] * fit) / 2, (z["SH"] - z["H"] * fit) / 2
  before = ((tap[0] - z["left"] - x0) / fit, (tap[1] - z["top"] - y0) / fit)
  after = ((tap[0] - z["left"] - z["x"]) / z["scale"], (tap[1] - z["top"] - z["y"]) / z["scale"])
  assert abs(before[0] - after[0]) < 4 and abs(before[1] - after[1]) < 4, (before, after)


def test_a_two_finger_pinch_at_fit_size_does_not_navigate_or_rate(phone, server):
  open_loupe(phone, server)
  phone.wait_for_timeout(500)
  touch = Touch(phone)
  stars = phone.locator(".hud .stars").inner_text()
  touch.pinch(phone, (195, 300), 200, 80, vertical=False)                    # pinch in at fit: nothing to do
  touch.pinch(phone, (195, 300), 80, 200, vertical=True)                     # a vertical pinch out: zooms, no rating
  expect(phone.locator(".hud .pos")).to_have_text("1/6")
  assert phone.locator(".hud .stars").inner_text() == stars
  assert server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") is None


def test_swiping_stays_off_while_zoomed_and_navigation_resets_the_zoom(phone, server):
  open_loupe(phone, server)
  phone.wait_for_timeout(500)
  phone.keyboard.press("z")
  expect(phone.locator(".stage.zoomed")).to_have_count(1)
  Touch(phone).drag(phone, (300, 500), (100, 500))                           # this pans; it must not go to photo 2
  expect(phone.locator(".hud .pos")).to_have_text("1/6")
  phone.keyboard.press("ArrowRight")
  expect(phone.locator(".hud .pos")).to_have_text("2/6")
  expect(phone.locator(".stage.zoomed")).to_have_count(0)
  assert phone.locator(".stage img.main").evaluate("e => e.style.transform") == ""
  swipe(phone, -150, 0)                                                      # swipes work again at fit size
  expect(phone.locator(".hud .pos")).to_have_text("3/6")


def test_ctrl_wheel_and_plus_minus_keys_zoom_on_a_desktop(page, server):
  open_loupe(page, server)
  page.wait_for_timeout(500)
  box = page.locator(".stage").bounding_box()
  page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
  page.keyboard.down("Control")
  page.mouse.wheel(0, -400)                                                  # a trackpad pinch: ctrl + wheel
  page.keyboard.up("Control")
  z = zoom_state(page)
  assert z["zoomed"] and z["scale"] > fit_of(z) * 1.2, z
  page.keyboard.press("+")
  z2 = zoom_state(page)
  assert z2["scale"] > z["scale"]
  page.keyboard.press("-")
  page.keyboard.press("-")
  page.keyboard.press("-")
  page.keyboard.press("-")
  page.keyboard.press("-")
  page.keyboard.press("-")
  expect(page.locator(".stage.zoomed")).to_have_count(0)                     # zoomed out to fit: back to normal
  page.keyboard.press("-")                                                   # nothing to zoom out of: no error, no zoom
  expect(page.locator(".stage.zoomed")).to_have_count(0)


def test_mouse_click_toggles_and_a_drag_pans_when_zoomed_but_navigates_at_fit(page, server):
  open_loupe(page, server)
  page.wait_for_timeout(500)
  box = page.locator(".stage").bounding_box()
  cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
  page.mouse.click(cx, cy)
  expect(page.locator(".stage.zoomed")).to_have_count(1)
  z0 = zoom_state(page)
  page.mouse.move(cx, cy)
  page.mouse.down()
  for i in range(1, 9):
    page.mouse.move(cx - i * 30, cy - i * 10)
  page.mouse.up()
  z1 = zoom_state(page)
  assert z1["x"] < z0["x"] and z1["y"] < z0["y"]
  expect(page.locator(".hud .pos")).to_have_text("1/6")                      # the drag panned, it did not navigate
  page.mouse.click(cx, cy)                                                   # back to fit
  expect(page.locator(".stage.zoomed")).to_have_count(0)
  page.mouse.move(cx, cy)
  page.mouse.down()
  for i in range(1, 9):
    page.mouse.move(cx - i * 25, cy)
  page.mouse.up()
  expect(page.locator(".hud .pos")).to_have_text("2/6")                      # at fit size a drag is a swipe again


def test_zoom_before_the_picture_has_loaded_waits_for_it(page, server):
  page.route("**/img/Medium/*", lambda route: (page.wait_for_timeout(700), route.continue_()))   # a slow link
  ids = photo_ids(server)
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("z")                                                   # pressed before the image is there
  expect(page.locator(".stage.zoomed")).to_have_count(1, timeout=10000)


# --- ArrowUp/ArrowDown step the rating in the viewer (ticket 067) --------------------------------

def test_arrow_up_down_step_the_rating_and_clamp(page, server):
  open_loupe(page, server)                                     # IMG_0001: unrated
  page.keyboard.press("ArrowUp")
  expect(page.locator(".hud .stars")).to_have_text("★☆☆☆☆")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == 1)
  page.keyboard.press("ArrowUp")
  page.keyboard.press("ArrowUp")
  page.keyboard.press("ArrowUp")
  page.keyboard.press("ArrowUp")
  expect(page.locator(".hud .stars")).to_have_text("★" * 5)
  page.keyboard.press("ArrowUp")                                # clamped at 5
  expect(page.locator(".hud .stars")).to_have_text("★" * 5)
  for _ in range(6):
    page.keyboard.press("ArrowDown")
  expect(page.locator(".hud .stars.reject")).to_be_visible()
  page.keyboard.press("ArrowDown")                               # clamped at reject
  expect(page.locator(".hud .stars.reject")).to_be_visible()
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0001.jpg.xmp") == -1)


def test_arrow_up_from_reject_restores_previous_stars(page, server):
  open_loupe(page, server, 1)                                    # IMG_0002 has 4 stars from its sidecar
  page.keyboard.press("x")                                        # reject directly (remembers the 4 stars)
  expect(page.locator(".hud .stars.reject")).to_be_visible()
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0002.jpg.xmp") == -1)
  page.keyboard.press("ArrowUp")                                  # one step up from reject: back to 4 stars
  expect(page.locator(".hud .stars")).to_have_text("★★★★☆")
  wait_for(lambda: server.sidecar_rating("2024/trip/IMG_0002.jpg.xmp") == 4)


def test_arrow_up_down_leave_a_filtered_view_like_other_rating_changes(page, server):
  page.goto(server.url + "/#/2024/trip?filter=unrated")
  expect(page.locator(".cell")).to_have_count(4)
  page.locator(".cell").first.click()
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")
  page.keyboard.press("ArrowUp")
  expect(page.locator(".hud .name")).to_contain_text("IMG_0004")             # left the filter, advanced


def test_arrow_up_down_do_nothing_while_typing_in_the_tag_field(page, server):
  open_loupe(page, server)
  page.keyboard.press("t")
  page.keyboard.press("ArrowUp")                                             # should not rate while typing
  page.keyboard.press("Escape")
  expect(page.locator(".hud .stars")).to_have_text("☆" * 5)


# ------------------------------------------------------------- help overlay (ticket 091)

def test_help_overlay_opens_from_browse_view_and_closes_with_escape(page, server):
  page.goto(server.url + "/#/")
  expect(page.locator(".help-card")).to_be_hidden()
  page.keyboard.press("?")
  expect(page.locator(".help-card")).to_be_visible()
  expect(page.locator(".help-card")).to_contain_text("Browse")
  expect(page.locator(".help-card")).to_contain_text("Viewer")
  page.keyboard.press("Escape")
  expect(page.locator(".help-card")).to_be_hidden()


def test_help_overlay_opens_from_loupe_with_h_or_f1_without_closing_it(page, server):
  open_loupe(page, server)
  page.keyboard.press("h")
  expect(page.locator(".help-card")).to_be_visible()
  expect(page.locator(".loupe")).to_be_visible()                # still on the same photo
  page.keyboard.press("Escape")                                 # closes help, not the loupe
  expect(page.locator(".help-card")).to_be_hidden()
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("F1")
  expect(page.locator(".help-card")).to_be_visible()
  page.locator(".help-card button", has_text="Close").click()
  expect(page.locator(".help-card")).to_be_hidden()


def test_help_overlay_does_not_open_while_typing_in_the_tag_field(page, server):
  open_loupe(page, server)
  page.keyboard.press("t")
  page.keyboard.press("h")
  expect(page.locator(".help-card")).to_be_hidden()
  page.keyboard.press("Escape")


# ------------------------------------------------- filter switcher in the loupe (ticket 092)

def test_switch_filter_from_loupe_keeps_the_same_photo_and_updates_context(page, server):
  open_loupe(page, server, 0)                                   # IMG_0001, unrated, 1/6 in "all"
  expect(page.locator(".hud .pos")).to_have_text("1/6")
  expect(page.locator(".filmstrip .thumb")).to_have_count(6)

  page.locator(".hud .filter-tag").click()
  expect(page.locator(".filter-picker")).to_be_visible()
  expect(page.locator('.filter-picker .filter-option[data-filter="rating>=5"]')).to_be_disabled()   # doesn't match

  page.locator(".filter-picker .filter-option", has_text="Unrated").click()
  expect(page.locator(".filter-picker")).to_have_count(0)       # closes after picking
  expect(page.locator(".hud .name")).to_contain_text("IMG_0001")  # same photo still shown
  expect(page.locator(".hud .pos")).to_have_text("1/4")           # context updated to the new filter
  expect(page.locator(".filmstrip .thumb")).to_have_count(4)
  expect(page.locator(".hud .filter-tag")).to_contain_text("Unrated")
  assert "filter=unrated" in page.url


def test_filter_switcher_disabled_option_does_nothing_and_escape_closes_the_panel(page, server):
  open_loupe(page, server, 0)                                   # IMG_0001, unrated
  page.locator(".hud .filter-tag").click()
  expect(page.locator(".filter-picker")).to_be_visible()
  page.locator('.filter-picker .filter-option[data-filter="rating>=5"]').click(force=True)
  expect(page.locator(".filter-picker")).to_be_visible()        # disabled: nothing happened
  expect(page.locator(".hud .pos")).to_have_text("1/6")          # still unfiltered
  page.keyboard.press("Escape")
  expect(page.locator(".filter-picker")).to_have_count(0)
  expect(page.locator(".loupe")).to_be_visible()                 # Escape closed the panel, not the viewer


# ------------------------------------------------------ AI rating (epic 173, ticket 180) ------

AI_ANSWERS = {"sharpness": 4, "composition": 3, "exposure": 5, "has_people": 0, "faces": 0,
              "subject_interest": 2, "color": 3, "technical_flaws": 1}


class FakeGemini:
  def __init__(self):
    self.calls = 0

  def __call__(self, url, headers, body):
    import json
    self.calls += 1
    text = json.dumps(AI_ANSWERS)
    return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode()


def test_ai_rate_button_rates_only_the_selection(page, server, monkeypatch):
  monkeypatch.setenv("GEMINI_API_KEY", "K")
  fake = server.app.state.gemini_transport = FakeGemini()
  page.goto(server.url + "/#/2024/trip")
  page.get_by_role("button", name="Select").click()
  page.locator(".cell").nth(1).click()
  page.locator(".cell").nth(3).click()
  page.get_by_role("button", name="AI Rate", exact=True).click()
  expect(page.locator("#toast")).to_contain_text("AI rating queued for 2 photo(s)")
  assert server.app.state.jobs.wait_idle(20)
  assert fake.calls == 2
  rated = server.app.state.db.execute(
      "SELECT COUNT(*) FROM files WHERE ai_score IS NOT NULL").fetchone()[0]
  assert rated == 2


def test_ai_rate_button_without_key_shows_the_message(page, server, monkeypatch):
  monkeypatch.delenv("GEMINI_API_KEY", raising=False)
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  page.get_by_role("button", name="AI Rate", exact=True).click()
  expect(page.locator("#toast")).to_contain_text("no Gemini API key configured")
  page.errors.clear()    # the browser logs the 400 itself as a console error


def test_files_panel_shows_the_ai_rating(page, server):
  # ticket 181: the score on its own line, the individual answers in the tooltip
  ids = photo_ids(server)
  fid = server.app.state.db.execute(
      "SELECT id FROM files WHERE photo_id = ?", (ids[0],)).fetchone()[0]
  server.app.state.db.execute("UPDATE files SET ai_score = 7.46 WHERE id = ?", (fid,))
  server.app.state.db.commit()
  page.goto(f"{server.url}/#/2024/trip?photo={ids[0]}")
  expect(page.locator(".loupe")).to_be_visible()
  page.keyboard.press("i")
  expect(page.locator(".files-panel .file .ai-rating")).to_have_text("AI rating 7.5")


def test_sort_by_ai_rating_orders_the_grid_best_first(page, server):
  # ticket 182
  ids = photo_ids(server)
  db = server.app.state.db
  for rank, pid in enumerate(ids[:3]):          # scores 1.0, 2.0, 3.0; the rest stay unscored
    db.execute("UPDATE files SET ai_score = ? WHERE photo_id = ?", (rank + 1.0, pid))
  db.commit()
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  page.locator('select[aria-label="sort"]').select_option("ai")
  assert "sort=ai" in page.url
  expect(page.locator(".cell").first).to_have_attribute("data-id", str(ids[2]))


def test_actions_menu_closes_with_escape_and_outside_click_without_acting(page, server):
  # ticket 196: the "..." menu is a modal; dismissing it does nothing.
  open_loupe(page, server)
  page.locator(".hud button.debug-menu").click()
  expect(page.locator(".actions-modal .menu-item")).to_have_count(2)       # Export, Flush thumbnails
  page.keyboard.press("Escape")
  expect(page.locator(".actions-modal")).to_have_count(0)
  expect(page.locator(".loupe")).to_be_visible()                           # Escape closed only the menu
  page.locator(".hud button.debug-menu").click()
  expect(page.locator(".actions-modal").get_by_role("button", name="Close")).to_have_count(0)
  page.mouse.click(5, 5)                                                    # outside the menu
  expect(page.locator(".actions-modal")).to_have_count(0)
  expect(page.locator("#toast")).not_to_contain_text("re-rendering")


def _next_to(popover, anchor, viewport):
  """ticket 198: the popover touches the anchor's column and sits just above/below it, in view."""
  assert popover["x"] >= 0 and popover["y"] >= 0
  assert popover["x"] + popover["width"] <= viewport["width"] + 1
  assert popover["y"] + popover["height"] <= viewport["height"] + 1
  gap_below = popover["y"] - (anchor["y"] + anchor["height"])
  gap_above = anchor["y"] - (popover["y"] + popover["height"])
  assert 0 <= gap_below <= 20 or 0 <= gap_above <= 20, (popover, anchor)
  overlap = min(popover["x"] + popover["width"], anchor["x"] + anchor["width"]) - max(popover["x"], anchor["x"])
  assert overlap > 0, (popover, anchor)          # horizontally aligned with the anchor, not elsewhere


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 800}, {"width": 600, "height": 700}])
def test_loupe_popovers_open_next_to_the_clicked_element(page, server, viewport):
  page.set_viewport_size(viewport)
  open_loupe(page, server)
  tag = page.locator(".hud .filter-tag")
  tag.click()
  _next_to(page.locator(".filter-picker").bounding_box(), tag.bounding_box(), viewport)
  page.mouse.click(5, 5)                                                   # ticket 199: outside dismisses it
  expect(page.locator(".filter-picker")).to_have_count(0)
  expect(page.locator(".loupe")).to_be_visible()
  more = page.locator(".hud button.debug-menu")
  more.click()
  _next_to(page.locator(".actions-modal .confirm-card").bounding_box(), more.bounding_box(), viewport)
