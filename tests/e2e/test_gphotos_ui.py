"""The 'Also upload to Google Photos' part of the export dialog (epic 205, ticket 214), against the
fake Google server in tests/fake_gphotos.py. Nothing real is ever uploaded."""

import json
import os
import time

import pytest
from playwright.sync_api import expect

from tests.e2e.harness import Server
from tests.fake_gphotos import FakeGPhotos, TOKEN


@pytest.fixture
def fake():
  f = FakeGPhotos().start()
  yield f
  f.stop()


def authorize(settings):
  os.makedirs(settings.state_dir, exist_ok=True)
  json.dump({"installed": {"client_id": "c", "client_secret": "s"}},
            open(settings.gphotos_client_path, "w"))
  json.dump({"refresh_token": "R", "access_token": TOKEN, "expires_at": time.time() + 9999},
            open(settings.gphotos_token_path, "w"))


@pytest.fixture
def gserver(tmp_path, fake):
  s = Server(tmp_path, gphotos_api_base_url=fake.url).start()
  authorize(s.settings)
  yield s
  s.stop()


@pytest.fixture
def gpage(browser, gserver):
  ctx = browser.new_context(viewport={"width": 1280, "height": 800})
  pg = ctx.new_page()
  pg.errors = []
  pg.on("pageerror", lambda e: pg.errors.append(str(e)))
  yield pg
  ctx.close()
  assert pg.errors == [], pg.errors


def open_export(page, server):
  page.goto(server.url + "/#/2024/trip")
  expect(page.locator(".cell")).to_have_count(6)
  page.get_by_role("button", name="Export", exact=True).click()
  expect(page.locator(".confirm-card")).to_be_visible()


def submit(page, server):
  """Click Export and wait until the request was accepted (jobs exist), then for the queue to drain."""
  page.locator(".confirm-card button.danger").click()
  expect(page.locator("#toast")).to_contain_text("queued")
  assert server.app.state.jobs.wait_idle(30)


def test_the_checkbox_is_disabled_with_a_hint_until_google_is_set_up(browser, tmp_path):
  s = Server(tmp_path).start()
  ctx = browser.new_context(viewport={"width": 1280, "height": 800})
  page = ctx.new_page()
  try:
    open_export(page, s)
    expect(page.locator(".export-gphotos")).to_be_disabled()
    expect(page.locator(".export-gphotos-section")).to_contain_text("google_auth")
  finally:
    ctx.close()
    s.stop()


def test_export_and_upload_to_a_new_album_named_after_the_folder(gpage, gserver, fake):
  open_export(gpage, gserver)
  expect(gpage.locator(".export-gps")).not_to_be_checked()
  gpage.locator(".export-gphotos").check()
  expect(gpage.locator(".export-album-name")).to_have_value("2024 trip")
  submit(gpage, gserver)
  expect(gpage.locator("#toast")).to_contain_text("upload to Google Photos")
  (album,) = fake.albums.values()
  assert album["title"] == "2024 trip" and len(album["items"]) == 6
  assert len(os.listdir(os.path.join(gserver.pictures, "Exported", "2024/trip"))) == 6


def test_same_name_warning_offers_to_add_to_the_existing_album(gpage, gserver, fake):
  fake.albums["old"] = {"title": "2024 trip", "items": []}
  open_export(gpage, gserver)
  gpage.locator(".export-gphotos").check()
  warn = gpage.locator(".export-album-warn")
  expect(warn).to_contain_text("already exists")
  warn.get_by_role("button", name="Add to it instead").click()
  expect(gpage.locator(".export-album-pick")).to_be_visible()
  expect(gpage.locator(".export-album-name")).to_be_hidden()
  submit(gpage, gserver)
  assert list(fake.albums) == ["old"] and len(fake.albums["old"]["items"]) == 6


def test_creating_another_album_with_the_same_name_is_still_possible(gpage, gserver, fake):
  fake.albums["old"] = {"title": "2024 trip", "items": []}
  open_export(gpage, gserver)
  gpage.locator(".export-gphotos").check()
  expect(gpage.locator(".export-album-warn")).to_contain_text("already exists")
  submit(gpage, gserver)
  assert len(fake.albums) == 2 and fake.albums["old"]["items"] == []


def test_a_selection_is_uploaded_in_grid_order(gpage, gserver, fake):
  gpage.goto(gserver.url + "/#/2024/trip")
  gpage.get_by_role("button", name="Select").click()
  gpage.locator(".cell").nth(3).click()          # clicked first: IMG_0004
  gpage.locator(".cell").nth(1).click()          # then IMG_0002
  gpage.get_by_role("button", name="Export", exact=True).click()
  gpage.locator(".export-gphotos").check()
  submit(gpage, gserver)
  (album,) = fake.albums.values()
  assert [fake.items[m]["filename"] for m in album["items"]] == ["IMG_0002.jpg", "IMG_0004.jpg"]
