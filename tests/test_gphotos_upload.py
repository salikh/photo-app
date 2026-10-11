import dataclasses
import json
import os
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoapp import api
from photoapp import db
from photoapp import gphotos
from photoapp import scan
from tests.fake_gphotos import FakeGPhotos, TOKEN


@pytest.fixture
def fake():
  f = FakeGPhotos().start()
  yield f
  f.stop()


def distinct_jpeg(path, shade):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  Image.new("RGB", (32, 24), (shade, 10, 10)).save(path)


def setup_app(settings, fake, tmp_path, names=("a", "b", "c"), authorized=True):
  settings = dataclasses.replace(settings, gphotos_api_base_url=fake.url)
  for i, n in enumerate(names):
    distinct_jpeg(os.path.join(settings.pictures_dir, "2020", n + ".jpg"), 40 * (i + 1))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir)
  if authorized:
    json.dump({"installed": {"client_id": "c", "client_secret": "s"}},
              open(settings.gphotos_client_path, "w"))
    json.dump({"refresh_token": "R", "access_token": TOKEN, "expires_at": time.time() + 9999},
              open(settings.gphotos_token_path, "w"))
  app = api.create_app(conn, settings)
  ids = {r["path"]: r["id"] for r in conn.execute(
      "SELECT f.path, p.id FROM photos p JOIN files f ON f.id = p.representative_file_id")}
  return app, TestClient(app), conn, ids


def run_jobs(app):
  app.state.jobs.start()
  assert app.state.jobs.wait_idle(30)
  app.state.jobs.stop()


def job(c, kind):
  return [j for j in c.get("/api/jobs").json()["jobs"] if j["kind"] == kind]


def test_export_uploads_into_a_new_album_in_the_given_order(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path)
  order = [ids["2020/c.jpg"], ids["2020/a.jpg"], ids["2020/b.jpg"]]    # not alphabetical
  r = c.post("/api/export", json={"ids": order, "dir": "2020", "target": str(tmp_path / "out"),
                                  "gphotos": {"album_title": "Trip 2020"}})
  assert r.status_code == 200, r.text
  assert [q["photo_id"] for q in r.json()["queued"]] == order and r.json()["upload_job"]
  run_jobs(app)
  (album,) = fake.albums.values()
  assert album["title"] == "Trip 2020"
  assert [fake.items[m]["filename"] for m in album["items"]] == ["c.jpg", "a.jpg", "b.jpg"]
  assert all(fake.items[m]["mime"] == "image/jpeg" for m in album["items"])
  (up,) = job(c, "gphotos_upload")
  assert up["state"] == "done" and up["note"] == "uploaded 3 of 3" and not up["retryable"]
  assert os.path.isfile(str(tmp_path / "out" / "a.jpg"))     # the local export stays


def test_adding_to_an_existing_album_appends(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path)
  c1 = c.post("/api/export", json={"ids": [ids["2020/a.jpg"]], "dir": "2020",
                                   "target": str(tmp_path / "o1"), "gphotos": {"album_title": "X"}})
  run_jobs(app)
  (album_id,) = fake.albums
  c.post("/api/export", json={"ids": [ids["2020/b.jpg"]], "dir": "2020",
                              "target": str(tmp_path / "o2"), "gphotos": {"album_id": album_id}})
  run_jobs(app)
  assert len(fake.albums) == 1 and len(fake.albums[album_id]["items"]) == 2


def test_failures_are_listed_and_retry_sends_only_those(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path)
  fake.reject_filenames.add("b.jpg")
  c.post("/api/export", json={"ids": [ids[f"2020/{n}.jpg"] for n in "abc"], "dir": "2020",
                              "target": str(tmp_path / "out"), "gphotos": {"album_title": "T"}})
  run_jobs(app)
  (up,) = job(c, "gphotos_upload")
  assert up["state"] == "done" and up["note"] == "uploaded 2 of 3, 1 failed" and up["retryable"]
  (album_id,) = fake.albums
  assert len(fake.albums[album_id]["items"]) == 2
  fake.reject_filenames.clear()
  r = c.post(f"/api/gphotos/jobs/{up['id']}/retry")
  assert r.status_code == 200 and r.json()["retrying"] == 1
  run_jobs(app)
  assert len(fake.albums) == 1 and len(fake.albums[album_id]["items"]) == 3
  assert not [j for j in job(c, "gphotos_upload") if j["id"] == up["id"]][0]["retryable"]
  assert c.post(f"/api/gphotos/jobs/{up['id']}/retry").status_code == 400


def test_an_upload_failure_for_one_file_does_not_fail_the_job(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path, names=("a", "b"))
  fake.inject("/v1/uploads", [400])        # the first byte upload is refused outright
  c.post("/api/export", json={"ids": [ids["2020/a.jpg"], ids["2020/b.jpg"]], "dir": "2020",
                              "target": str(tmp_path / "out"), "gphotos": {"album_title": "T"}})
  run_jobs(app)
  (up,) = job(c, "gphotos_upload")
  assert up["state"] == "done" and "1 failed" in up["note"]


def test_a_bad_export_is_a_failed_item_and_an_all_failed_upload_makes_no_album(
    settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path, names=("a",))
  os.remove(os.path.join(settings.pictures_dir, "2020", "a.jpg"))   # the export job will fail
  c.post("/api/export", json={"ids": [ids["2020/a.jpg"]], "dir": "2020",
                              "target": str(tmp_path / "out"), "gphotos": {"album_title": "T"}})
  run_jobs(app)
  (up,) = job(c, "gphotos_upload")
  assert up["state"] == "done" and up["note"] == "uploaded 0 of 1, 1 failed"
  assert fake.albums == {}


def test_not_authorized_or_nameless_requests_are_refused(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path, authorized=False)
  body = {"ids": [ids["2020/a.jpg"]], "dir": "2020", "target": str(tmp_path / "out")}
  r = c.post("/api/export", json={**body, "gphotos": {"album_title": "T"}})
  assert r.status_code == 400 and "google_auth" in r.text
  app, c, conn, ids = setup_app(settings, fake, tmp_path / "x")
  r = c.post("/api/export", json={**body, "gphotos": {"album_title": "  "}})
  assert r.status_code == 400 and "album name" in r.text
  assert c.post("/api/export", json=body).status_code == 200    # plain export still works


def wait_albums(c, **params):
  """The albums endpoint fills a cache in the background (ticket 231): poll until it has finished."""
  for _ in range(200):
    body = c.get("/api/gphotos/albums", params=params).json()
    if not body["loading"]:
      return body
    time.sleep(0.05)
  raise AssertionError("album listing never finished")


def test_albums_endpoint_ranks_by_name_and_year_and_flags_an_exact_match(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path)
  for title in ("Berlin 2019", "2024 trip", "Birthday 2024", "trip 2024", "Zoo"):
    fake.albums[title] = {"title": title, "items": []}
  got = wait_albums(c, name="2024 trip")["albums"]
  assert [a["title"] for a in got][:2] == ["2024 trip", "trip 2024"]
  assert got[0]["exact"] and got[1]["exact"] is False
  assert [a["title"] for a in got][-1] in ("Zoo", "Berlin 2019")
  assert c.get("/api/gphotos/albums").json()["albums"][0]["score"] == 0.0


def test_albums_are_cached_and_a_cold_request_answers_at_once_with_loading(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path)
  fake.albums["a"] = {"title": "A", "items": []}
  first = c.get("/api/gphotos/albums").json()
  assert "loading" in first and "error" in first            # never blocks on Google
  wait_albums(c)
  calls = []
  real = gphotos.Client.list_albums
  gphotos.Client.list_albums = lambda self: calls.append(1) or real(self)
  try:
    fake.albums["b"] = {"title": "B", "items": []}
    assert [a["title"] for a in c.get("/api/gphotos/albums").json()["albums"]] == ["A"]  # cached
    assert calls == []
    c.get("/api/gphotos/albums", params={"refresh": "true"})
    got = wait_albums(c)["albums"]
    assert sorted(a["title"] for a in got) == ["A", "B"] and calls == [1]
  finally:
    gphotos.Client.list_albums = real


def test_a_failed_listing_reports_the_error_and_keeps_the_last_good_list(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path)
  fake.albums["a"] = {"title": "A", "items": []}
  wait_albums(c)
  real = gphotos.Client.list_albums
  def boom(self):
    raise gphotos.GPhotosError("quota")
  gphotos.Client.list_albums = boom
  try:
    c.get("/api/gphotos/albums", params={"refresh": "true"})
    body = wait_albums(c)
  finally:
    gphotos.Client.list_albums = real
  assert body["error"] == "quota" and [a["title"] for a in body["albums"]] == ["A"]


def test_albums_endpoint_needs_google_setup(settings, fake, tmp_path):
  app, c, conn, ids = setup_app(settings, fake, tmp_path, authorized=False)
  assert c.get("/api/gphotos/albums").status_code == 400


def test_default_album_name_follows_the_folder():
  from photoapp import export
  assert export.default_album_name(".") == "Photos"
  assert export.default_album_name("2024/trip") == "2024 trip"
  assert export.default_album_name("2024/trip/day1") == "day1"
  assert export.default_album_name("misc") == "misc"
  assert export.default_album_name("2024") == "2024"
