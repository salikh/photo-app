import os

from fastapi.testclient import TestClient
from PIL import Image

from photoapp import api
from photoapp import db
from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan_sidecars import FAV, XMP, write


def build(settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"), size=(3000, 2000))
  make_jpeg(os.path.join(d, "2020", "B.jpg"))
  touch(os.path.join(d, "2020", "K1.DNG"))
  make_jpeg(os.path.join(d, "2020", "K1.JPG"))
  make_jpeg(os.path.join(d, "2020", "trip", "c.jpg"))
  make_jpeg(os.path.join(d, "2020", "trip", "deep", "d.jpg"))
  make_jpeg(os.path.join(d, "2021", "e.jpg"))
  make_jpeg(os.path.join(d, "top.jpg"))
  write(os.path.join(d, "2020", "a.jpg.xmp"), XMP % (4, FAV), mtime=1_000_000)
  write(os.path.join(d, "2020", "B.jpg.xmp"), XMP % (-1, ""), mtime=1_000_000)
  write(os.path.join(d, "2020", "K1.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)
  write(os.path.join(d, "2020", "K1.JPG.xmp"), XMP % (2, ""), mtime=2_000_000)
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  return TestClient(api.create_app(conn, settings))


def names(resp):
  return [p["name"] for p in resp.json()["photos"]]


def test_dirs_lists_children_with_subtree_counts(settings):
  c = build(settings)
  root = c.get("/api/dirs").json()
  assert root["photos"] == 1 and root["dirs"] == [
      {"name": "2020", "photos": 5}, {"name": "2021", "photos": 1}]
  y = c.get("/api/dirs", params={"path": "2020"}).json()
  assert y["photos"] == 3 and y["dirs"] == [{"name": "trip", "photos": 2}]
  assert c.get("/api/dirs", params={"path": "2020/trip/"}).json()["dirs"] == [
      {"name": "deep", "photos": 1}]
  assert c.get("/api/dirs", params={"path": "nope"}).json() == {
      "path": "nope", "dirs": [], "photos": 0}
  assert c.get("/api/dirs", params={"path": "../etc"}).status_code == 400


def test_photos_page_sort_filter_and_paging(settings):
  c = build(settings)
  r = c.get("/api/photos", params={"dir": "2020", "sort": "name"}).json()
  assert r["total"] == 3
  assert [p["name"] for p in r["photos"]] == ["a.jpg", "B.jpg", "K1.JPG"]  # case-insensitive
  a = r["photos"][0]
  assert a["rating"] == 4 and a["fav"] is True and a["files"] == 1
  assert (a["width"], a["height"]) == (3000, 2000)
  k = r["photos"][2]
  assert k["files"] == 2 and k["conflict"] is True and k["rating"] == 2  # rep = camera JPG
  assert names(c.get("/api/photos", params={"dir": "2020", "filter": "rejected"})) == ["B.jpg"]
  assert names(c.get("/api/photos", params={"dir": "2020", "filter": "fav"})) == ["a.jpg"]
  assert names(c.get("/api/photos", params={"dir": "2020", "filter": "conflict"})) == ["K1.JPG"]
  assert names(c.get("/api/photos", params={"dir": "2020", "filter": "picked", "sort": "name"})) == ["a.jpg", "K1.JPG"]
  assert names(c.get("/api/photos", params={"dir": "2020", "filter": "unrated"})) == []
  page = c.get("/api/photos", params={"dir": "2020", "sort": "name", "limit": 1, "offset": 1}).json()
  assert page["total"] == 3 and [p["name"] for p in page["photos"]] == ["B.jpg"]
  assert names(c.get("/api/photos")) == ["top.jpg"]
  assert c.get("/api/photos", params={"sort": "x"}).status_code == 400
  assert c.get("/api/photos", params={"filter": "x"}).status_code == 400


def test_photos_page_includes_tags(settings):
  c = build(settings)
  pid = c.get("/api/photos", params={"dir": "2020", "sort": "name"}).json()["photos"][0]["id"]
  c.post(f"/api/photos/{pid}/tags", json={"add": ["b", "a"]})
  assert c.get("/api/photos", params={"dir": "2020", "sort": "name"}).json()["photos"][0]["tags"] == ["a", "b"]


def test_missing_files_are_hidden(settings):
  c = build(settings)
  os.remove(os.path.join(settings.pictures_dir, "top.jpg"))
  os.utime(settings.pictures_dir, None)
  c.post("/api/scan")
  c.app.state.scanner.wait()
  assert names(c.get("/api/photos")) == []


def test_photo_detail(settings):
  c = build(settings)
  pid = [p for p in c.get("/api/photos", params={"dir": "2020"}).json()["photos"] if p["name"] == "K1.JPG"][0]["id"]
  d = c.get(f"/api/photos/{pid}").json()
  assert [f["path"] for f in d["files"]] == ["2020/K1.DNG", "2020/K1.JPG"]   # original first
  assert [f["role"] for f in d["files"]] == ["original", "camera"]
  assert {s["path"]: s["rating"] for s in d["sidecars"]} == {"2020/K1.DNG.xmp": 1, "2020/K1.JPG.xmp": 2}
  assert d["conflict"] is True and d["original_file_id"] == d["files"][0]["id"]
  assert c.get("/api/photos/99999").status_code == 404


def test_photo_detail_includes_camera_metadata(settings):
  # Ticket 084: aperture/shutter_speed/iso reach the API, None when the fixture (no real EXIF)
  # has none, and the values 083's extraction would have written when present.
  c = build(settings)
  pid = c.get("/api/photos", params={"dir": "2020", "sort": "name"}).json()["photos"][0]["id"]
  d = c.get(f"/api/photos/{pid}").json()
  assert d["files"][0]["aperture"] is None
  assert d["files"][0]["shutter_speed"] is None
  assert d["files"][0]["iso"] is None
  fid = d["files"][0]["id"]
  c.app.state.db.execute(
      "UPDATE files SET aperture = 8.0, shutter_speed = 0.5, iso = 400 WHERE id = ?", (fid,))
  c.app.state.db.commit()
  f = c.get(f"/api/photos/{pid}").json()["files"][0]
  assert (f["aperture"], f["shutter_speed"], f["iso"]) == (8.0, 0.5, 400)


def file_id(c, path):
  return c.app.state.db.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def test_image_sizes_are_made_on_demand_and_served(settings):
  c = build(settings)
  fid = file_id(c, "2020/a.jpg")
  r = c.get(f"/img/Thumb/{fid}")
  assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
  assert "max-age" in r.headers["cache-control"]
  out = os.path.join(settings.thumbs_dir, "Thumb", "2020", "a.jpg")
  assert Image.open(out).size == (300, 200)
  assert thumbs.usage(c.app.state.db)["Thumb"]["files"] == 1
  assert c.get(f"/img/Medium/{fid}").status_code == 200
  assert Image.open(os.path.join(settings.thumbs_dir, "Medium", "2020", "a.jpg")).size == (2000, 1333)


def test_huge_and_full_serve_the_original_and_raw_has_no_image_yet(settings):
  c = build(settings)
  fid = file_id(c, "2020/a.jpg")
  full = c.get(f"/img/full/{fid}")
  assert full.status_code == 200 and Image.open(__import__("io").BytesIO(full.content)).size == (3000, 2000)
  assert c.get(f"/img/Huge/{fid}").status_code == 200
  assert not os.path.exists(os.path.join(settings.thumbs_dir, "Huge"))   # nothing re-encoded
  raw = file_id(c, "2020/K1.DNG")
  assert c.get(f"/img/Small/{raw}").status_code == 404
  assert c.get(f"/img/full/{raw}").status_code == 404
  assert c.get(f"/img/Bogus/{fid}").status_code == 404
  assert c.get("/img/Thumb/99999").status_code == 404


def test_one_star_is_unrated_changes_filters_only_when_enabled(settings):
  import dataclasses
  d = settings.pictures_dir
  for i, r in enumerate((1, 2, 0, -1)):
    make_jpeg(os.path.join(d, "s", f"{i}.jpg"))
    if r:
      write(os.path.join(d, "s", f"{i}.jpg.xmp"), XMP % (r, ""), mtime=1_000_000)
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)

  def listed(c, f):
    return sorted(p["name"] for p in c.get("/api/photos", params={"dir": "s", "filter": f}).json()["photos"])

  plain = TestClient(api.create_app(conn, settings))
  assert plain.get("/api/config").json()["one_star_is_unrated"] is False
  assert listed(plain, "picked") == ["0.jpg", "1.jpg"] and listed(plain, "unrated") == ["2.jpg"]
  on = TestClient(api.create_app(conn, dataclasses.replace(settings, one_star_is_unrated=True)))
  assert on.get("/api/config").json()["one_star_is_unrated"] is True
  assert listed(on, "picked") == ["1.jpg"]                       # rating >= 2
  assert listed(on, "unrated") == ["0.jpg", "2.jpg"]              # 1 star counts as unrated
  assert listed(on, "rated") == ["1.jpg", "3.jpg"]                # 2 stars and the reject
  assert listed(on, "rejected") == ["3.jpg"]
  # the data itself is untouched: the API still reports the real rating
  assert {p["name"]: p["rating"] for p in on.get("/api/photos", params={"dir": "s"}).json()["photos"]}["0.jpg"] == 1


def test_folders_starting_with_a_dot_are_not_listed_or_counted(settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2001", "trip", "a.jpg"))
  make_jpeg(os.path.join(d, "2001", "trip", ".nu", "thumb1.jpg"))
  make_jpeg(os.path.join(d, "2001", "trip", ".nu", "thumb2.jpg"))
  make_jpeg(os.path.join(d, "2001", "trip", "sub", ".thumbnails", "t.jpg"))
  make_jpeg(os.path.join(d, "2001", "trip", "sub", "b.jpg"))
  make_jpeg(os.path.join(d, "2001", ".picasaoriginals", "o.jpg"))
  make_jpeg(os.path.join(d, ".hidden-top", "h.jpg"))
  make_jpeg(os.path.join(d, "2001", "trip", ".dotfile.jpg"))            # a file, not a folder
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  c = TestClient(api.create_app(conn, settings))

  root = c.get("/api/dirs").json()
  assert [x["name"] for x in root["dirs"]] == ["2001"]                   # .hidden-top is not listed
  y = c.get("/api/dirs", params={"path": "2001"}).json()
  assert y["dirs"] == [{"name": "trip", "photos": 3}]                    # a, .dotfile, sub/b (not .nu, .thumbnails)
  trip = c.get("/api/dirs", params={"path": "2001/trip"}).json()
  assert trip["dirs"] == [{"name": "sub", "photos": 1}]                  # .nu hidden; sub counts b only
  assert trip["photos"] == 2                                             # a.jpg and .dotfile.jpg stay
  # hidden folders can still be opened by path, nothing is deleted
  assert c.get("/api/dirs", params={"path": "2001/trip/.nu"}).json()["photos"] == 2
  listed = c.get("/api/photos", params={"dir": "2001/trip/.nu"}).json()
  assert listed["total"] == 2
  assert c.get("/api/dirs", params={"path": "2001/trip/.nu"}).json()["dirs"] == []


def test_rating_n_filter_means_exactly_n_stars(settings):
  import dataclasses
  d = settings.pictures_dir
  for i, r in enumerate((1, 2, 2, 3, 0, -1)):
    make_jpeg(os.path.join(d, "s", f"{i}.jpg"))
    if r:
      write(os.path.join(d, "s", f"{i}.jpg.xmp"), XMP % (r, ""), mtime=1_000_000)
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  c = TestClient(api.create_app(conn, settings))

  def names(client, f):
    r = client.get("/api/photos", params={"dir": "s", "filter": f, "sort": "name"})
    assert r.status_code == 200, (f, r.text)
    return [p["name"] for p in r.json()["photos"]]

  assert names(c, "rating:1") == ["0.jpg"]
  assert names(c, "rating:2") == ["1.jpg", "2.jpg"]
  assert names(c, "rating:3") == ["3.jpg"]
  assert names(c, "rating:4") == [] and names(c, "rating:5") == []
  for bad in ("rating:0", "rating:6", "rating:", "rating:x", "rating:-1", "stars:3"):
    assert c.get("/api/photos", params={"dir": "s", "filter": bad}).status_code == 400, bad
  # the total in the response follows the filter, so the button and the grid agree
  assert c.get("/api/photos", params={"dir": "s", "filter": "rating:2"}).json()["total"] == 2
  on = TestClient(api.create_app(conn, dataclasses.replace(settings, one_star_is_unrated=True)))
  assert on.get("/api/photos", params={"dir": "s", "filter": "rating:1"}).status_code == 400
  assert names(on, "rating:2") == ["1.jpg", "2.jpg"]


def test_filter_counts_match_what_each_filter_lists(settings):
  import dataclasses
  d = settings.pictures_dir
  for i, r in enumerate((1, 2, 2, 3, 0, 0, -1, 5)):
    make_jpeg(os.path.join(d, "s", f"{i}.jpg"))
    if r:
      write(os.path.join(d, "s", f"{i}.jpg.xmp"), XMP % (r, FAV if i == 3 else ""), mtime=1_000_000)
  make_jpeg(os.path.join(d, "s", ".hidden", "h.jpg"))                        # in a subfolder: not counted
  make_jpeg(os.path.join(d, "other", "o.jpg"))
  conn = db.open_state(settings.state_dir)
  scan.scan(conn, d)
  for flag in (False, True):
    c = TestClient(api.create_app(conn, dataclasses.replace(settings, one_star_is_unrated=flag)))
    counts = c.get("/api/photos/counts", params={"dir": "s"}).json()["counts"]
    assert counts["all"] == 8
    for name, n in counts.items():
      listed = c.get("/api/photos", params={"dir": "s", "filter": name}).json()
      assert listed["total"] == n, (flag, name, n, listed["total"])
    assert ("rating:1" in counts) == (not flag)
  plain = TestClient(api.create_app(conn, settings)).get("/api/photos/counts", params={"dir": "s"}).json()["counts"]
  assert plain["unrated"] == 2 and plain["rejected"] == 1 and plain["rating:2"] == 2 and plain["fav"] == 1
  assert plain["picked"] == 5 and plain["rated"] == 6
  assert TestClient(api.create_app(conn, settings)).get("/api/photos/counts").json()["counts"]["all"] == 0   # root: no files directly in it
  assert TestClient(api.create_app(conn, settings)).get("/api/photos/counts", params={"dir": "../x"}).status_code == 400
