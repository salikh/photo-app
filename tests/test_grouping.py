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


def roles(conn, *paths):
  f = by_path(conn)
  return [(f[p]["role"], f[p]["derived_from"] == f[paths[0]]["id"] if f[p]["derived_from"] else None)
          for p in paths]


def test_dng_jpg_tif_png_with_the_same_name_form_one_photo(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "a.tif"))
  make_jpeg(os.path.join(d, "a.png"))
  scan.scan(conn, d)
  f = by_path(conn)
  assert len(photos(conn)) == 1
  assert len({f[p]["photo_id"] for p in f}) == 1
  assert [f[p]["role"] for p in ("a.dng", "a.jpg", "a.tif", "a.png")] == \
      ["original", "camera", "tuning", "tuning"]
  assert all(f[p]["derived_from"] == f["a.dng"]["id"] and f[p]["link_source"] == "auto"
             for p in ("a.jpg", "a.tif", "a.png"))
  (photo,) = photos(conn).values()
  assert photo["original_file_id"] == f["a.dng"]["id"]
  assert photo["representative_file_id"] == f["a.jpg"]["id"]


def test_without_a_raw_the_jpeg_is_the_original_then_tiff_then_png(conn, settings):
  d = settings.pictures_dir
  for n in ("a.jpg", "a.tif", "a.png", "b.tif", "b.PNG", "c.TIFF", "c.jpeg"):
    make_jpeg(os.path.join(d, n))
  scan.scan(conn, d)
  f = by_path(conn)
  assert [f[p]["role"] for p in ("a.jpg", "a.tif", "a.png")] == ["original", "tuning", "tuning"]
  assert [f[p]["role"] for p in ("b.tif", "b.PNG")] == ["original", "tuning"]
  assert [f[p]["role"] for p in ("c.jpeg", "c.TIFF")] == ["original", "tuning"]
  assert f["a.tif"]["derived_from"] == f["a.jpg"]["id"]
  assert f["b.PNG"]["derived_from"] == f["b.tif"]["id"]
  assert len(photos(conn)) == 3
  (pa,) = [p for p in photos(conn).values() if p["original_file_id"] == f["a.jpg"]["id"]]
  assert pa["representative_file_id"] == f["a.jpg"]["id"]


def test_lone_tiff_png_and_other_types_stay_alone(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "x.tif"))
  make_jpeg(os.path.join(d, "y.png"))
  make_jpeg(os.path.join(d, "z.jpg"))
  make_jpeg(os.path.join(d, "z.gif"))              # not part of the rule
  make_jpeg(os.path.join(d, "z.webp"))
  make_jpeg(os.path.join(d, "sub", "x.png"))       # same name, other directory
  scan.scan(conn, d)
  assert len(photos(conn)) == 6
  assert all(f["role"] == "original" for f in by_path(conn).values())


def test_duplicate_of_the_same_kind_stays_single(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.tif"))
  make_jpeg(os.path.join(d, "a.TIF"))
  make_jpeg(os.path.join(d, "a.png"))
  scan.scan(conn, d)
  f = by_path(conn)
  assert len(photos(conn)) == 2
  joined = [p for p in ("a.tif", "a.TIF") if f[p]["photo_id"] == f["a.png"]["photo_id"]]
  assert len(joined) == 1                        # the png joins exactly one of the two TIFFs


def test_existing_lone_png_photo_merges_and_the_originals_photo_survives(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.png"))
  # the png and the dng have different names at first, so they are two photos
  os.rename(os.path.join(d, "a.png"), os.path.join(d, "other.png"))
  scan.scan(conn, d)
  dng_photo = by_path(conn)["a.dng"]["photo_id"]
  conn.execute("UPDATE photos SET rating = 4 WHERE id = ?", (dng_photo,))
  conn.commit()
  os.rename(os.path.join(d, "other.png"), os.path.join(d, "a.png"))
  os.utime(d, None)
  scan.scan(conn, d)
  f = by_path(conn)
  assert f["a.png"]["photo_id"] == f["a.dng"]["photo_id"] == dng_photo
  assert photos(conn)[dng_photo]["rating"] == 4
  live = conn.execute("SELECT COUNT(DISTINCT photo_id) FROM files WHERE missing = 0").fetchone()[0]
  assert live == 1              # (the renamed-away row keeps its own, missing, photo)


def test_only_original_and_camera_sidecars_are_written_not_tunings(conn, settings):
  from photoapp import curation
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "a.tif"))
  scan.scan(conn, d)
  pid = by_path(conn)["a.dng"]["photo_id"]
  r = curation.set_rating(conn, settings, pid, 3)
  assert r["sidecars"] == ["a.dng.xmp", "a.jpg.xmp"]
  assert not os.path.exists(os.path.join(d, "a.tif.xmp"))


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


def test_databases_scanned_by_an_older_rule_are_regrouped_once(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "a.dng"))
  make_jpeg(os.path.join(d, "a.png"))
  scan.scan(conn, d)
  # pretend an older rule made them two photos and recorded its version
  conn.execute("UPDATE files SET photo_id = (SELECT id FROM photos ORDER BY id LIMIT 1), "
               "role = 'original', derived_from = NULL WHERE path = 'a.png'")
  conn.execute("DELETE FROM meta WHERE key = 'grouping_version'")
  conn.commit()
  assert grouping.regroup_if_rule_changed(conn) is True
  assert by_path(conn)["a.png"]["role"] == "tuning"
  assert grouping.regroup_if_rule_changed(conn) is False          # only once per version
