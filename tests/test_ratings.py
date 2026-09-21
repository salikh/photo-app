import os

import pytest

from photoapp import ratings
from photoapp import scan
from tests.conftest import make_jpeg
from tests.test_grouping import photos, touch
from tests.test_scan_sidecars import FAV, XMP, write


def sc(path, mtime, rating, fav=False, tags=()):
  return {"path": path, "mtime": mtime, "rating": rating, "has_fav": fav,
          "tags": list(tags)}


def test_apply_key():
  assert [ratings.apply_key(2, k) for k in "012345"] == [0, 1, 2, 3, 4, 5]
  assert ratings.apply_key(3, "x") == -1
  assert ratings.apply_key(-1, "X") == 0
  assert ratings.apply_key(-1, "x", previous_stars=4) == 4
  with pytest.raises(ValueError):
    ratings.apply_key(0, "9")


def test_step_is_clamped_and_uses_previous_stars():
  assert [ratings.step(c, 1) for c in (-1, 0, 4, 5)] == [0, 1, 5, 5]
  assert [ratings.step(c, -1) for c in (-1, 0, 1, 5)] == [-1, -1, 0, 4]
  assert ratings.step(-1, 1, previous_stars=3) == 3


def test_resolve_no_sidecars_and_single():
  assert ratings.resolve([]).rating is None
  r = ratings.resolve([sc("a.xmp", 1, 3, True, ["x"])])
  assert (r.rating, r.fav, r.tags, r.conflict) == (3, True, ("x",), False)


def test_resolve_agreement_is_not_a_conflict():
  r = ratings.resolve([sc("a", 1, 2), sc("b", 5, 2), sc("c", 3, None)])
  assert r.rating == 2 and not r.conflict


def test_resolve_newest_wins_and_flags_conflict():
  r = ratings.resolve([sc("dng", 10, 1), sc("jpg", 20, 4, True)])
  assert r.rating == 4 and r.fav and r.conflict and r.source_path == "jpg"
  r = ratings.resolve([sc("dng", 30, 1), sc("jpg", 20, 4)])
  assert r.rating == 1 and r.conflict


def test_resolve_newest_without_rating_falls_back_to_rated_one():
  r = ratings.resolve([sc("a", 10, 3), sc("b", 20, None, tags=["t"])])
  assert r.rating == 3 and r.tags == ("t",) and not r.conflict


def test_fav_disagreement_is_a_conflict():
  assert ratings.resolve([sc("a", 1, 2, True), sc("b", 2, 2, False)]).conflict


def test_scan_fills_photo_state_from_sidecars(conn, settings):
  d = settings.pictures_dir
  touch(os.path.join(d, "K1.DNG"))
  make_jpeg(os.path.join(d, "K1.JPG"))
  write(os.path.join(d, "K1.DNG.xmp"), XMP % (1, ""), mtime=1_000_000)
  write(os.path.join(d, "K1.JPG.xmp"), XMP % (4, FAV), mtime=2_000_000)
  make_jpeg(os.path.join(d, "lone.jpg"))
  scan.scan(conn, d)
  by = {r["path"]: r for r in conn.execute(
      "SELECT f.path, p.* FROM files f JOIN photos p ON p.id = f.photo_id")}
  p = by["K1.DNG"]
  assert (p["rating"], p["fav"], p["conflict"], p["rating_source"]) == (4, 1, 1, "xmp")
  assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0  # fav is not a tag
  lone = by["lone.jpg"]
  assert (lone["rating"], lone["conflict"], lone["rating_source"]) == (0, 0, None)


def test_sidecar_edit_updates_photo_and_import_survives_without_sidecar(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  make_jpeg(os.path.join(d, "b.jpg"))
  scan.scan(conn, d)
  conn.execute("UPDATE photos SET rating = 5, rating_source = 'import' "
               "WHERE id = (SELECT photo_id FROM files WHERE path = 'a.jpg')")
  conn.commit()
  write(os.path.join(d, "b.jpg.xmp"), XMP % (2, ""), mtime=1_000_000)
  scan.scan(conn, d)
  by = {r["path"]: r for r in conn.execute(
      "SELECT f.path, p.* FROM files f JOIN photos p ON p.id = f.photo_id")}
  assert by["a.jpg"]["rating"] == 5 and by["a.jpg"]["rating_source"] == "import"
  assert by["b.jpg"]["rating"] == 2
  # in-place edit in an otherwise skipped directory
  st = os.stat(d).st_mtime
  write(os.path.join(d, "b.jpg.xmp"), XMP % (5, ""), mtime=2_000_000)
  os.utime(d, (st, st))
  scan.scan(conn, d)
  assert conn.execute("SELECT p.rating FROM photos p JOIN files f ON "
                      "f.photo_id = p.id WHERE f.path = 'b.jpg'").fetchone()[0] == 5


def test_scan_resolves_photos_left_unresolved_by_an_older_version(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  write(os.path.join(d, "a.jpg.xmp"), XMP % (3, ""), mtime=1_000_000)
  scan.scan(conn, d)
  conn.execute("UPDATE photos SET rating = 0, rating_source = NULL, rating_updated_at = NULL")   # as an old DB would be
  conn.commit()
  scan.scan(conn, d)                       # nothing changed on disk
  row = conn.execute("SELECT rating, rating_source FROM photos").fetchone()
  assert (row["rating"], row["rating_source"]) == (3, "xmp")


# --- the database is an eligible source: the newest of database and XMP wins (ticket 021) ---------

def db(mtime, rating, fav=False, tags=()):
  return {"mtime": mtime, "rating": rating, "has_fav": fav, "tags": list(tags)}


def test_database_newer_than_the_sidecars_wins_and_shows_sidecars_behind():
  r = ratings.resolve([sc("a.xmp", 100, 2), sc("b.xmp", 100, 2)], db(200, 4))
  assert (r.rating, r.db_wins, r.conflict, r.source_path) == (4, True, True, ratings.DATABASE)


def test_sidecar_newer_than_the_database_wins_without_a_conflict():
  r = ratings.resolve([sc("a.xmp", 300, 5)], db(200, 4))
  assert (r.rating, r.db_wins, r.conflict, r.source_path, r.mtime) == (5, False, False, "a.xmp", 300)


def test_database_within_the_clock_skew_tolerance_keeps_its_value():
  r = ratings.resolve([sc("a.xmp", 101.5, 5)], db(100, 4))          # sidecar only 1.5 s newer
  assert r.db_wins and r.rating == 4
  r = ratings.resolve([sc("a.xmp", 103, 5)], db(100, 4))            # 3 s newer: beyond the tolerance
  assert not r.db_wins and r.rating == 5


def test_equal_database_and_sidecars_are_not_a_conflict():
  r = ratings.resolve([sc("a.xmp", 100, 3, True, ["x"]), sc("b.xmp", 100, 3, True, ["x"])],
                      db(100, 3, True, ["x"]))
  assert r.rating == 3 and not r.conflict and r.fav and r.tags == ("x",)


def test_database_alone_without_sidecars_and_undated_database_is_ignored():
  assert ratings.resolve([], db(50, 4)).rating == 4
  assert ratings.resolve([sc("a.xmp", 1, 2)], None).rating == 2


def test_database_fav_and_tags_come_with_the_winning_database_value():
  r = ratings.resolve([sc("a.xmp", 100, 2, False, ["old"])], db(500, 2, True, ["new"]))
  assert r.db_wins and r.fav and r.tags == ("new",) and r.conflict     # fav differs from the sidecar


# --- the reject rule (ticket 053) ---

def test_a_single_files_reject_does_not_reject_the_pair_when_the_other_is_picked():
  for reject_time, pick_time in ((300, 200), (200, 300)):                # whichever is newer
    r = ratings.resolve([sc("k.DNG.xmp", reject_time, -1), sc("k.JPG.xmp", pick_time, 1)])
    assert r.rating == 1 and r.conflict, (reject_time, pick_time)
    assert r.reject_overruled == (reject_time > pick_time)      # only when the reject would have won
  r = ratings.resolve([sc("k.DNG.xmp", 300, -1), sc("k.JPG.xmp", 200, 3)])
  assert r.rating == 3 and r.source_path == "k.JPG.xmp"
  r = ratings.resolve([sc("a.xmp", 300, -1), sc("b.xmp", 200, 2), sc("c.xmp", 250, 4)])
  assert r.rating == 4                                                    # the newest picked sidecar


def test_reject_stands_when_nothing_is_picked():
  assert ratings.resolve([sc("a.xmp", 100, -1), sc("b.xmp", 200, -1)]).rating == -1
  assert ratings.resolve([sc("a.xmp", 100, -1)]).rating == -1              # a single sidecar
  # an unrated (0) file has nothing to protect: plain newest-wins
  assert ratings.resolve([sc("a.xmp", 300, -1), sc("b.xmp", 200, 0)]).rating == -1
  assert ratings.resolve([sc("a.xmp", 200, -1), sc("b.xmp", 300, 0)]).rating == 0
  assert ratings.resolve([sc("a.xmp", 200, -1), sc("b.xmp", 300, None)]).rating == -1


def test_a_reject_made_in_the_app_wins_by_being_the_newest():
  # the app wrote -1 to both sidecars and the database: nothing is picked
  assert ratings.resolve([sc("a.xmp", 500, -1), sc("b.xmp", 500, -1)], db(500, -1)).rating == -1
  # a database reject newer than a picked sidecar wins (an app decision, no sidecar reject involved)
  r = ratings.resolve([sc("a.xmp", 100, 3), sc("b.xmp", 100, 3)], db(900, -1))
  assert r.rating == -1 and r.db_wins and not r.reject_overruled
  # a newer darktable pick on one file after the app's reject brings the pair back (that file is picked)
  r = ratings.resolve([sc("a.xmp", 500, -1), sc("b.xmp", 800, 2)], db(500, -1))
  assert r.rating == 2 and not r.db_wins


def make_photo_with_sidecars(conn, settings, ratings_by_name, mtimes):
  d = settings.pictures_dir
  touch(os.path.join(d, "K1.DNG"))
  make_jpeg(os.path.join(d, "K1.JPG"))
  for name, r in ratings_by_name.items():
    write(os.path.join(d, name), XMP % (r, ""), mtime=mtimes[name])
  scan.scan(conn, d)
  return conn.execute("SELECT photo_id FROM files WHERE path = 'K1.DNG'").fetchone()[0]


def row(conn, pid):
  return dict(conn.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone())


def test_scan_stamps_the_database_time_from_the_winning_sidecar(conn, settings):
  pid = make_photo_with_sidecars(conn, settings, {"K1.DNG.xmp": 1, "K1.JPG.xmp": 3},
                                 {"K1.DNG.xmp": 1_000_000, "K1.JPG.xmp": 2_000_000})
  p = row(conn, pid)
  assert (p["rating"], p["rating_updated_at"], p["conflict"]) == (3, 2_000_000, 1)


def test_scan_keeps_a_newer_database_rating_and_a_newer_sidecar_replaces_it(conn, settings):
  import time as time_
  pid = make_photo_with_sidecars(conn, settings, {"K1.DNG.xmp": 2}, {"K1.DNG.xmp": 1_000_000})
  future = time_.time() + 1000
  conn.execute("UPDATE photos SET rating = 5, rating_updated_at = ?, rating_source = 'import' "
               "WHERE id = ?", (future, pid))
  conn.commit()
  scan.scan(conn, settings.pictures_dir)
  os.utime(settings.pictures_dir, None)
  ratings.refresh_photo(conn, pid)
  p = row(conn, pid)
  assert p["rating"] == 5 and p["conflict"] == 1                  # newer than the sidecar: kept, sidecars behind
  # a sidecar edited after that (newer still) takes over and the database follows
  write(os.path.join(settings.pictures_dir, "K1.DNG.xmp"), XMP % (1, ""), mtime=future + 500)
  os.utime(settings.pictures_dir, None)
  scan.scan(conn, settings.pictures_dir)
  p = row(conn, pid)
  assert (p["rating"], p["rating_updated_at"], p["conflict"], p["rating_source"]) == (1, future + 500, 0, "xmp")


def test_app_edit_ties_with_the_sidecars_so_no_conflict_and_a_later_darktable_edit_wins(conn, settings):
  from photoapp import curation
  pid = make_photo_with_sidecars(conn, settings, {"K1.DNG.xmp": 1, "K1.JPG.xmp": 1},
                                 {"K1.DNG.xmp": 1_000_000, "K1.JPG.xmp": 1_000_000})
  curation.set_rating(conn, settings, pid, 4)
  p = row(conn, pid)
  newest = max(os.stat(os.path.join(settings.pictures_dir, n)).st_mtime for n in ("K1.DNG.xmp", "K1.JPG.xmp"))
  assert (p["rating"], p["conflict"], p["rating_updated_at"]) == (4, 0, newest)
  scan.scan(conn, settings.pictures_dir)                         # a rescan keeps it
  assert (row(conn, pid)["rating"], row(conn, pid)["conflict"]) == (4, 0)
  write(os.path.join(settings.pictures_dir, "K1.JPG.xmp"), XMP % (2, ""), mtime=newest + 100)   # darktable
  os.utime(settings.pictures_dir, None)
  scan.scan(conn, settings.pictures_dir)
  assert row(conn, pid)["rating"] == 2


def test_real_case_reject_on_one_file_and_default_one_star_on_the_other(conn, settings):
  pid = make_photo_with_sidecars(conn, settings, {"K1.DNG.xmp": -1, "K1.JPG.xmp": 1},
                                 {"K1.DNG.xmp": 2_000_000, "K1.JPG.xmp": 1_000_000})   # the reject is newer
  p = row(conn, pid)
  assert p["rating"] == 1 and p["conflict"] == 1                  # not rejected; badge until a write syncs them
  from photoapp import curation
  curation.set_rating(conn, settings, pid, -1)                    # now a reject made in the app
  p = row(conn, pid)
  assert (p["rating"], p["conflict"]) == (-1, 0)
  scan.scan(conn, settings.pictures_dir)
  assert row(conn, pid)["rating"] == -1                           # stays a reject: nothing is picked
