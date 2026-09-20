import os

from photoapp import grouping
from photoapp import scan
from tests.conftest import make_jpeg


def touch(path, data=b"raw"):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "wb") as f:
    f.write(data)


def by_path(conn):
  return {r["path"]: r for r in conn.execute("SELECT * FROM files")}


def photos(conn):
  return {r["id"]: r for r in conn.execute("SELECT * FROM photos")}


def test_dng_jpg_pair_becomes_one_photo_with_roles(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2020", "K___1.DNG"))
  make_jpeg(os.path.join(d, "2020", "K___1.JPG"))
  scan.scan(conn, d)
  f = by_path(conn)
  dng, jpg = f["2020/K___1.DNG"], f["2020/K___1.JPG"]
  assert dng["photo_id"] == jpg["photo_id"] is not None
  assert (dng["role"], jpg["role"]) == ("original", "camera")
  assert jpg["derived_from"] == dng["id"] and dng["derived_from"] is None
  assert jpg["link_source"] == "auto"
  (photo,) = photos(conn).values()
  assert photo["original_file_id"] == dng["id"]
  assert photo["representative_file_id"] == jpg["id"]   # camera JPG by default


def test_case_insensitive_stem_and_extension(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "k1.dng"))
  make_jpeg(os.path.join(d, "K1.jpeg"))
  scan.scan(conn, d)
  assert len(photos(conn)) == 1


def test_lone_files_and_same_name_in_other_dirs_stay_separate(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "2020", "K___1.DNG"))
  make_jpeg(os.path.join(d, "2021", "K___1.JPG"))   # camera counter reuse
  make_jpeg(os.path.join(d, "2021", "solo.jpg"))
  scan.scan(conn, d)
  assert len(photos(conn)) == 3
  assert all(f["role"] == "original" for f in by_path(conn).values())
  assert all(f["derived_from"] is None for f in by_path(conn).values())


def test_non_jpeg_files_are_not_grouped(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "a.png"))
  scan.scan(conn, d)
  f = by_path(conn)
  assert f["a.png"]["photo_id"] not in (f["a.dng"]["photo_id"], None)
  assert len(photos(conn)) == 2


def test_photo_identity_and_rating_survive_rescan_and_new_jpg(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  scan.scan(conn, d)
  (pid,) = photos(conn)
  conn.execute("UPDATE photos SET rating = 4 WHERE id = ?", (pid,))
  conn.commit()
  make_jpeg(os.path.join(d, "a.jpg"))
  os.utime(d, None)
  scan.scan(conn, d)
  (p,) = photos(conn).values()
  assert p["id"] == pid and p["rating"] == 4
  assert by_path(conn)["a.jpg"]["photo_id"] == pid
  assert p["representative_file_id"] == by_path(conn)["a.jpg"]["id"]  # auto rep upgrades


def test_lone_jpg_rating_is_kept_when_dng_appears(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  (pid,) = photos(conn)
  conn.execute("UPDATE photos SET rating = 3 WHERE id = ?", (pid,))
  conn.commit()
  touch(os.path.join(d, "a.dng"))
  os.utime(d, None)
  scan.scan(conn, d)
  (p,) = photos(conn).values()
  assert p["id"] == pid and p["rating"] == 3
  assert p["original_file_id"] == by_path(conn)["a.dng"]["id"]


def test_manual_representative_survives_regroup(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  (pid,) = photos(conn)
  dng = by_path(conn)["a.dng"]["id"]
  conn.execute("UPDATE photos SET representative_file_id = ?, "
               "representative_source = 'manual' WHERE id = ?", (dng, pid))
  grouping.regroup(conn)
  assert photos(conn)[pid]["representative_file_id"] == dng


def test_manual_files_are_left_alone_and_empty_photos_deleted(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  jpg = by_path(conn)["a.jpg"]
  conn.execute("UPDATE files SET link_source = 'manual', role = 'tuning' "
               "WHERE id = ?", (jpg["id"],))
  grouping.regroup(conn)
  after = by_path(conn)["a.jpg"]
  assert after["role"] == "tuning" and after["photo_id"] == jpg["photo_id"]
  # a Photo that no file belongs to is removed
  dng = by_path(conn)["a.dng"]["id"]
  cur = conn.execute("INSERT INTO photos (original_file_id, "
                     "representative_file_id) VALUES (?, ?)", (dng, dng))
  conn.execute("INSERT INTO tags (photo_id, tag) VALUES (?, 'x')", (cur.lastrowid,))
  grouping.regroup(conn)
  assert cur.lastrowid not in photos(conn)
  assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0


def test_regroup_is_idempotent(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d)
  before = ({p: dict(r) for p, r in by_path(conn).items()},
            {p: dict(r) for p, r in photos(conn).items()})
  assert grouping.regroup(conn) == 0
  assert before == ({p: dict(r) for p, r in by_path(conn).items()},
                    {p: dict(r) for p, r in photos(conn).items()})
