import datetime
import os
import threading

from fastapi.testclient import TestClient

from photoapp import api
from photoapp import db
from photoapp import jobs
from photoapp import scan
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan_sidecars import XMP, write


def app_with_pair(settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "y", "K1.DNG"))
  make_jpeg(os.path.join(d, "y", "K1.JPG"))
  make_jpeg(os.path.join(d, "y", "a.jpg"))
  make_jpeg(os.path.join(d, "y", "b.jpg"))
  make_jpeg(os.path.join(d, "Exported", "K1-edit.jpg"), color="blue")
  write(os.path.join(d, "y", "a.jpg.xmp"), XMP % (2, ""), mtime=1_000_000)
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  return TestClient(api.create_app(conn, settings))


def fid(c, path):
  return c.app.state.db.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def pid(c, path):
  return c.app.state.db.execute("SELECT photo_id FROM files WHERE path = ?", (path,)).fetchone()[0]


def test_batch_rating_and_grouped_undo(settings):
  c = app_with_pair(settings)
  ids = [pid(c, "y/a.jpg"), pid(c, "y/b.jpg"), pid(c, "y/K1.DNG"), 99999]
  r = c.post("/api/photos/rating", json={"ids": ids, "rating": 5}).json()
  assert len(r["results"]) == 3 and r["errors"][0]["photo_id"] == 99999
  assert all(x["photo"]["rating"] == 5 for x in r["results"])
  for p in ids[:3]:
    assert c.get(f"/api/photos/{p}").json()["rating"] == 5
  log = c.get("/api/activity").json()
  assert {l["batch_id"] for l in log} == {r["batch_id"]}
  # one of them is changed afterwards: only that one refuses to undo
  c.post(f"/api/photos/{ids[0]}/rating", json={"rating": 3})
  u = c.post(f"/api/activity/batch/{r['batch_id']}/undo").json()
  assert len(u["undone"]) == 2 and len(u["errors"]) == 1
  assert c.get(f"/api/photos/{ids[1]}").json()["rating"] == 0
  assert c.get(f"/api/photos/{ids[0]}").json()["rating"] == 3
  again = c.post(f"/api/activity/batch/{r['batch_id']}/undo").json()
  assert again["undone"] == [] and len(again["errors"]) == 1     # still refuses
  assert c.post("/api/activity/batch/nosuchbatch/undo").status_code == 400
  assert c.post("/api/photos/rating", json={"ids": [], "rating": 1}).status_code == 400


def test_representative_selection(settings):
  c = app_with_pair(settings)
  p = pid(c, "y/K1.DNG")
  d = c.get(f"/api/photos/{p}").json()
  assert d["representative_file_id"] == fid(c, "y/K1.JPG")
  r = c.post(f"/api/photos/{p}/representative", json={"file_id": fid(c, "y/K1.DNG")})
  assert r.json()["representative_file_id"] == fid(c, "y/K1.DNG")
  c.post("/api/scan"); c.app.state.scanner.wait()                  # survives a rescan
  assert c.get(f"/api/photos/{p}").json()["representative_file_id"] == fid(c, "y/K1.DNG")
  assert c.post(f"/api/photos/{p}/representative", json={"file_id": fid(c, "y/a.jpg")}).status_code == 400
  assert c.post("/api/photos/9999/representative", json={"file_id": 1}).status_code == 404
  r = c.post(f"/api/photos/{p}/representative", json={"file_id": None})
  assert r.json()["representative_file_id"] == fid(c, "y/K1.JPG")


def test_link_and_unlink_endpoints(settings):
  c = app_with_pair(settings)
  edit, dng = fid(c, "Exported/K1-edit.jpg"), fid(c, "y/K1.DNG")
  r = c.post(f"/api/files/{edit}/link", json={"target_file_id": dng, "role": "export"})
  assert r.status_code == 200
  files = {f["path"]: f for f in r.json()["files"]}
  assert files["Exported/K1-edit.jpg"]["role"] == "export"
  assert files["Exported/K1-edit.jpg"]["link_source"] == "manual"
  r = c.post(f"/api/files/{edit}/unlink")
  assert [f["path"] for f in r.json()["files"]] == ["Exported/K1-edit.jpg"]
  assert c.post(f"/api/files/{dng}/link", json={"target_file_id": edit}).status_code in (200, 400)
  assert c.post(f"/api/files/{edit}/link", json={"target_file_id": edit}).status_code == 400
  assert c.post("/api/files/9999/link", json={"target_file_id": edit}).status_code == 404
  assert c.post(f"/api/files/{edit}/link", json={"target_file_id": dng, "role": "bad"}).status_code == 400


def test_thumbs_usage_endpoint(settings):
  c = app_with_pair(settings)
  c.get(f"/img/Thumb/{fid(c, 'y/a.jpg')}")
  u = c.get("/api/thumbs/usage", params={"lacking": True}).json()
  assert u["usage"]["Thumb"]["files"] == 1 and u["usage"]["Small"]["files"] == 0
  assert u["lacking"]["Thumb"] == 4 and "lacking" not in c.get("/api/thumbs/usage").json()


def test_photos_route_bumps_missing_thumbnails_for_the_opened_folder(settings):
  # ticket 080: opening a folder's first page enqueues its missing thumbnails on
  # app.state.background_jobs, so the load-adaptive worker would work them ahead of any backlog.
  c = app_with_pair(settings)
  assert c.app.state.background_jobs.list() == []

  r = c.get("/api/photos", params={"dir": "y"})
  assert r.status_code == 200
  jobs_seen = c.app.state.background_jobs.list()
  assert len(jobs_seen) > 0
  assert {j["kind"] for j in jobs_seen} == {"populate_thumb"}

  # a later page of the same folder (offset > 0) does not enqueue again
  before = len(c.app.state.background_jobs.list())
  c.get("/api/photos", params={"dir": "y", "offset": 200})
  assert len(c.app.state.background_jobs.list()) == before


def test_trash_photos_endpoint(settings):
  c = app_with_pair(settings)
  d = settings.pictures_dir
  k1 = pid(c, "y/K1.DNG")
  a = pid(c, "y/a.jpg")
  c.post(f"/api/photos/{k1}/rating", json={"rating": -1})   # reject K1 (DNG + JPG)

  r = c.post("/api/photos/trash", json={"ids": [k1, a, 99999]})
  assert r.status_code == 200
  body = r.json()
  assert len(body["trashed"]) == 1 and body["trashed"][0]["photo_id"] == k1
  moved_from = sorted(m["from"] for m in body["trashed"][0]["moved"])
  assert moved_from == ["y/K1.DNG", "y/K1.DNG.xmp", "y/K1.JPG", "y/K1.JPG.xmp"]
  errored = {e["photo_id"] for e in body["errors"]}
  assert errored == {a, 99999}   # a.jpg was never rejected; 99999 doesn't exist

  assert not os.path.exists(os.path.join(d, "y", "K1.DNG"))
  assert os.path.isfile(os.path.join(d, ".trash", "y", "K1.DNG"))
  assert os.path.isfile(os.path.join(d, "y", "a.jpg"))          # untouched
  assert c.get(f"/api/photos/{k1}").json()["files"][0]["missing"]

  assert c.post("/api/photos/trash", json={"ids": []}).status_code == 400


def test_trash_file_endpoint(settings):
  # ticket 082: file-scoped, not rating-gated -- unlike /api/photos/trash above.
  c = app_with_pair(settings)
  d = settings.pictures_dir
  dng = fid(c, "y/K1.DNG")   # y/K1.DNG + y/K1.JPG are an unrated pair (app_with_pair)

  r = c.post(f"/api/files/{dng}/trash")
  assert r.status_code == 200
  body = r.json()
  assert sorted(m["from"] for m in body["moved"]) == ["y/K1.DNG"]
  assert body["errors"] == []
  assert body["photo"]["files"][0]["missing"] or body["photo"]["files"][1]["missing"]

  assert not os.path.exists(os.path.join(d, "y", "K1.DNG"))
  assert os.path.isfile(os.path.join(d, ".trash", "y", "K1.DNG"))
  assert os.path.isfile(os.path.join(d, "y", "K1.JPG"))   # sibling untouched

  assert c.post(f"/api/files/{dng}/trash").status_code == 404   # already missing
  assert c.post("/api/files/99999/trash").status_code == 404


def test_rerender_thumbs_endpoint(settings):
  # ticket 079: clears the cache so a broken thumbnail gets a fresh render on the next request.
  c = app_with_pair(settings)
  file_id = fid(c, "y/a.jpg")
  c.get(f"/img/Thumb/{file_id}")
  before = c.get("/api/thumbs/usage").json()["usage"]["Thumb"]["files"]
  assert before == 1

  r = c.post(f"/api/files/{file_id}/rerender_thumbs")
  assert r.status_code == 200 and r.json() == {"file_id": file_id, "cleared": ["Thumb"]}
  assert c.get("/api/thumbs/usage").json()["usage"]["Thumb"]["files"] == 0

  assert c.get(f"/img/Thumb/{file_id}").status_code == 200   # regenerates normally
  assert c.get("/api/thumbs/usage").json()["usage"]["Thumb"]["files"] == 1

  assert c.post("/api/files/9999/rerender_thumbs").status_code == 404


def test_seconds_until_next_hour():
  now = datetime.datetime(2026, 9, 21, 1, 30, 0)
  assert scan.seconds_until(3, now) == 90 * 60
  assert scan.seconds_until(1, now) == 23.5 * 3600      # already past: tomorrow
  assert scan.seconds_until(3, datetime.datetime(2026, 9, 21, 3, 0, 0)) == 24 * 3600


def test_enqueue_nightly_scan_queues_one_job_per_top_level_dir(settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "top.jpg"))            # files directly in the root: '.'
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  make_jpeg(os.path.join(d, "2021", "b.jpg"))
  db.open_state(settings.state_dir).close()
  q = jobs.JobQueue(settings.db_path, {"scan_dir": lambda c, j: None, "prune_jobs": lambda c, j: None,
                                       "purge_trash": lambda c, j: None})

  n = scan.enqueue_nightly_scan(q, d)
  assert n == 5   # 3 directories + prune_jobs + purge_trash (tickets 075, 081)
  by_kind = {}
  for j in q.list():
    by_kind.setdefault(j["kind"], []).append(j["target"])
  assert sorted(by_kind["scan_dir"]) == [".", "2020", "2021"]
  assert by_kind["prune_jobs"] == [None]
  assert by_kind["purge_trash"] == [None]

  # A second nightly fire while those jobs are still queued must not duplicate them (ticket 076).
  n2 = scan.enqueue_nightly_scan(q, d)
  assert n2 == 5
  assert len(q.list()) == 5


def test_nightly_scan_thread_fires_and_stops(settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  db.open_state(settings.state_dir).close()
  q = jobs.JobQueue(settings.db_path, {"scan_dir": lambda c, j: None, "prune_jobs": lambda c, j: None,
                                       "purge_trash": lambda c, j: None})
  waits = []
  fired = threading.Event()

  def fake_wait(seconds):
    waits.append(seconds)
    if len(waits) == 2:
      n.stop()
    fired.set()

  n = scan.NightlyScan(q, d, hour=3, wait=fake_wait)
  n.start()
  n._thread.join(5)
  assert n.runs == 1 and len(waits) == 2   # stop() fires during the 2nd wait, before a 2nd run
  assert all(0 < w <= 24 * 3600 for w in waits)
  assert sorted(j["kind"] for j in q.list()) == ["prune_jobs", "purge_trash", "scan_dir", "scan_dir"]
