"""Ticket 249: the stabilize job, the tuning link, the description, the API."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from photoapp import api
from photoapp import db
from photoapp import scan
from photoapp import video
from photoapp import video_stabilize as vs
from tests.test_video_tools import make_clip, needs_ffmpeg
from tests.test_video_stabilize import needs_vidstab

pytestmark = [needs_ffmpeg, needs_vidstab]


@pytest.fixture
def lib(settings, conn):
  tools = video.Tools()
  video.configure(tools)
  clip = os.path.join(settings.pictures_dir, "t", "MOV_1.mkv")
  os.makedirs(os.path.dirname(clip))
  make_clip(tools, clip, seconds=2)
  scan.scan(conn, settings.pictures_dir)
  app = api.create_app(conn, settings)
  app.state.medium_jobs.start()
  client = TestClient(app)
  photo_id = conn.execute("SELECT photo_id FROM files WHERE path = 't/MOV_1.mkv'").fetchone()[0]
  yield client, app, photo_id
  app.state.medium_jobs.stop()


def run_stabilize(client, app, photo_id, level):
  r = client.post(f"/api/photos/{photo_id}/stabilize", json={"level": level})
  assert r.status_code == 200, r.text
  assert app.state.medium_jobs.wait_idle(timeout=120)
  return r.json()


def test_stabilize_makes_a_linked_tuning_with_a_description(lib, settings):
  client, app, photo_id = lib
  queued = run_stabilize(client, app, photo_id, "strong")
  assert "strong" in queued["description"]
  detail = client.get(f"/api/photos/{photo_id}").json()
  by_path = {f["path"]: f for f in detail["files"]}
  assert set(by_path) == {"t/MOV_1.mkv", "t/MOV_1.stabilized-strong.mp4"}
  tuning = by_path["t/MOV_1.stabilized-strong.mp4"]
  assert tuning["role"] == "tuning" and tuning["link_source"] == "manual" and tuning["is_video"]
  assert tuning["tuning_note"].startswith("stabilized (strong)") and tuning["video_codec"] == "h264"
  assert by_path["t/MOV_1.mkv"]["tuning_note"] is None
  assert (tuning["width"], tuning["height"]) == (160, 120)
  job = [j for j in client.get("/api/jobs").json()["jobs"] if j["kind"] == "stabilize"][0]
  assert job["state"] == "done" and job["note"].startswith("pass 2")


def test_rerun_overwrites_the_same_level_and_other_level_adds_a_file(lib, settings, conn):
  client, app, photo_id = lib
  run_stabilize(client, app, photo_id, "strong")
  first = conn.execute("SELECT id FROM files WHERE path LIKE '%strong.mp4'").fetchone()[0]
  run_stabilize(client, app, photo_id, "strong")
  run_stabilize(client, app, photo_id, "weak")
  files = client.get(f"/api/photos/{photo_id}").json()["files"]
  assert sorted(f["path"] for f in files) == [
      "t/MOV_1.mkv", "t/MOV_1.stabilized-strong.mp4", "t/MOV_1.stabilized-weak.mp4"]
  assert [f["id"] for f in files if f["path"].endswith("strong.mp4")] == [first]
  assert not [n for n in os.listdir(os.path.join(settings.pictures_dir, "t")) if n.endswith(".part")]
  links = open(os.path.join(settings.state_dir, "manual_links.jsonl")).read().splitlines()
  assert len(links) == 2                  # the re-run did not add a third decision


def test_the_link_survives_a_rescan(lib, settings, conn):
  client, app, photo_id = lib
  run_stabilize(client, app, photo_id, "weak")
  scan.scan(conn, settings.pictures_dir)
  files = client.get(f"/api/photos/{photo_id}").json()["files"]
  assert len(files) == 2 and {f["role"] for f in files} == {"original", "tuning"}
  assert [f["tuning_note"] for f in files if f["role"] == "tuning"][0].startswith("stabilized (weak)")


def test_errors(lib, settings, conn):
  client, app, photo_id = lib
  assert client.post(f"/api/photos/{photo_id}/stabilize", json={"level": "wild"}).status_code == 400
  assert client.post(f"/api/photos/{photo_id}/stabilize",
                     json={"options": {"smoothing": 99999}}).status_code == 400
  assert client.post("/api/photos/9999/stabilize", json={}).status_code == 404
  from tests.conftest import make_jpeg
  make_jpeg(os.path.join(settings.pictures_dir, "t", "p.jpg"))
  scan.scan(conn, settings.pictures_dir)
  still = conn.execute("SELECT photo_id FROM files WHERE path = 't/p.jpg'").fetchone()[0]
  assert client.post(f"/api/photos/{still}/stabilize", json={}).status_code == 400
  levels = client.get("/api/stabilize/levels").json()
  assert levels["available"] and [l["level"] for l in levels["levels"]] == ["weak", "strong"]


def test_the_same_level_is_not_queued_twice(lib):
  client, app, photo_id = lib
  app.state.medium_jobs.stop()            # keep the first job queued
  a = client.post(f"/api/photos/{photo_id}/stabilize", json={"level": "weak"}).json()
  b = client.post(f"/api/photos/{photo_id}/stabilize", json={"level": "weak"}).json()
  c = client.post(f"/api/photos/{photo_id}/stabilize", json={"level": "strong"}).json()
  assert a["job_id"] == b["job_id"] != c["job_id"]


def test_deleting_the_original_or_the_shown_tuning_leaves_a_sane_photo(lib, settings, conn):
  """Ticket 251: either tuning can be deleted; the Photo keeps showing a live file, also after a
  rescan."""
  from photoapp import trash
  client, app, photo_id = lib
  run_stabilize(client, app, photo_id, "weak")
  ids = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM files")}
  tuning, original = ids["t/MOV_1.stabilized-weak.mp4"], ids["t/MOV_1.mkv"]

  def detail():
    return client.get(f"/api/photos/{photo_id}").json()

  assert client.post(f"/api/photos/{photo_id}/representative", json={"file_id": tuning}).status_code == 200
  assert client.post(f"/api/files/{tuning}/trash").status_code == 200      # the shown one goes
  assert detail()["representative_file_id"] == original
  # bring a fresh tuning back, show it, then delete the unstabilized original
  conn.execute("UPDATE files SET missing = 0 WHERE id = ?", (tuning,))
  os.replace(os.path.join(settings.pictures_dir, ".trash/t/MOV_1.stabilized-weak.mp4"),
             os.path.join(settings.pictures_dir, "t/MOV_1.stabilized-weak.mp4"))
  conn.commit()
  client.post(f"/api/photos/{photo_id}/representative", json={"file_id": original})
  assert client.post(f"/api/files/{original}/trash").status_code == 200
  assert detail()["representative_file_id"] == tuning
  scan.scan(conn, settings.pictures_dir)
  d = detail()
  assert d["representative_file_id"] == tuning
  assert [f["path"] for f in d["files"] if not f["missing"]] == ["t/MOV_1.stabilized-weak.mp4"]
  listed = client.get("/api/photos", params={"dir": "t"}).json()["photos"]
  assert [p["file_id"] for p in listed if p["id"] == photo_id] == [tuning]
  # no source left to stabilize again -> a clear error, not a crash
  assert client.post(f"/api/photos/{photo_id}/stabilize", json={}).status_code == 400


def test_batch_endpoint_queues_one_job_per_video_and_skips_stills(lib, settings, conn):
  """Ticket 253."""
  from tests.conftest import make_jpeg
  client, app, photo_id = lib
  app.state.medium_jobs.stop()
  make_jpeg(os.path.join(settings.pictures_dir, "t", "p.jpg"))
  make_video = os.path.join(settings.pictures_dir, "t", "MOV_2.mkv")
  make_clip(video.Tools(), make_video, seconds=1)
  scan.scan(conn, settings.pictures_dir)
  pid = lambda p: conn.execute("SELECT photo_id FROM files WHERE path = ?", (p,)).fetchone()[0]
  ids = [photo_id, pid("t/MOV_2.mkv"), pid("t/p.jpg"), 99999]
  r = client.post("/api/photos/stabilize", json={"ids": ids, "level": "weak"})
  assert r.status_code == 200, r.text
  body = r.json()
  assert len(body["queued"]) == 2 and len({q["job_id"] for q in body["queued"]}) == 2
  assert sorted(s["reason"] for s in body["skipped"]) == ["no such photo", "no video to stabilize"]
  assert client.post("/api/photos/stabilize", json={"ids": ids, "level": "x"}).status_code == 400
