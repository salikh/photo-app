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
  conn.execute("UPDATE photos SET rating = 0, rating_source = NULL")   # as an old DB would be
  conn.commit()
  scan.scan(conn, d)                       # nothing changed on disk
  row = conn.execute("SELECT rating, rating_source FROM photos").fetchone()
  assert (row["rating"], row["rating_source"]) == (3, "xmp")
