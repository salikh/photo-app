import datetime
import os
import threading

from fastapi.testclient import TestClient

from photoapp import api
from photoapp import db
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


def test_seconds_until_next_hour():
  now = datetime.datetime(2026, 9, 21, 1, 30, 0)
  assert scan.seconds_until(3, now) == 90 * 60
  assert scan.seconds_until(1, now) == 23.5 * 3600      # already past: tomorrow
  assert scan.seconds_until(3, datetime.datetime(2026, 9, 21, 3, 0, 0)) == 24 * 3600


def test_nightly_scan_triggers_and_skips_when_busy():
  class Manager:
    def __init__(self): self.started = 0; self.busy = False
    def start(self):
      if self.busy: return False
      self.started += 1
      return True
  waits, m = [], Manager()
  fired = threading.Event()

  def fake_wait(seconds):
    waits.append(seconds)
    if len(waits) == 3:
      n.stop()
    if len(waits) == 2:
      m.busy = True
    fired.set()

  n = scan.NightlyScan(m, hour=3, wait=fake_wait)
  n.start()
  n._thread.join(5)
  assert m.started == 1 and n.runs == 1 and len(waits) == 3   # 2nd run skipped: busy
  assert all(0 < w <= 24 * 3600 for w in waits)
