import dataclasses
import json
import os
import sqlite3

from photoapp import curation
from photoapp import import_ratings
from photoapp import scan
from photoapp import xmp
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan_sidecars import XMP, write


def metadata_db(path, rows):
  c = sqlite3.connect(path)
  c.execute("CREATE TABLE images (hash TEXT PRIMARY KEY, filepath TEXT NOT NULL, "
            "rating INTEGER NOT NULL, reject INTEGER, popularity INTEGER, "
            "copies INTEGER, has_exported_copy INTEGER, rating_source TEXT, "
            "rating_source_path TEXT, merged_paths TEXT NOT NULL)")
  for i, (h, fp, rating, merged) in enumerate(rows):
    c.execute("INSERT INTO images VALUES (?, ?, ?, 0, 0, 1, 0, NULL, NULL, ?)",
              (h, fp, rating, json.dumps(merged)))
  c.commit()
  c.close()
  return path


def photo(conn, path):
  return conn.execute(
      "SELECT p.* FROM photos p JOIN files f ON f.photo_id = p.id "
      "WHERE f.path = ?", (path,)).fetchone()


def build(conn, settings):
  d = settings.pictures_dir
  for n in ("a", "b", "c", "d"):
    make_jpeg(os.path.join(d, "y", n + ".jpg"), color=n and "red")
  touch(os.path.join(d, "y", "e.dng"))
  make_jpeg(os.path.join(d, "y", "e.jpg"))
  write(os.path.join(d, "y", "b.jpg.xmp"), XMP % (2, ""), mtime=1_000_000)
  scan.scan(conn, d)


def test_import_applies_reports_and_remembers_hashes(conn, settings, tmp_path):
  build(conn, settings)
  db_path = metadata_db(str(tmp_path / "m.db"), [
      ("h-a", "y/a.jpg", 4, ["y/a.jpg", "elsewhere/a-copy.jpg"]),
      ("h-b", "y/b.jpg", 5, ["y/b.jpg"]),            # XMP says 2: disagreement
      ("h-c", "y/c.jpg", 0, ["y/c.jpg"]),            # unrated: skipped
      ("h-e", "y/e.dng", -1, ["y/e.dng", "y/e.jpg"]),  # reject applies to the pair
      ("h-none", "gone/x.jpg", 3, ["gone/x.jpg"]),   # not in the library
  ])
  rep = import_ratings.import_ratings(conn, settings, db_path)
  assert (rep.rows, rep.unmatched_rows, rep.applied, rep.skipped_unrated,
          rep.kept_existing) == (5, 1, 2, 1, 1)
  assert rep.disagreements == [
      {"photo_id": photo(conn, "y/b.jpg")["id"], "path": "y/b.jpg",
       "xmp_or_app": 2, "imported": 5, "source": "xmp"}]
  a = photo(conn, "y/a.jpg")
  assert (a["rating"], a["rating_source"]) == (4, "import")
  assert photo(conn, "y/b.jpg")["rating"] == 2                      # XMP wins
  assert photo(conn, "y/e.dng")["rating"] == -1
  assert photo(conn, "y/e.dng")["id"] == photo(conn, "y/e.jpg")["id"]
  assert photo(conn, "y/c.jpg")["rating"] == 0
  assert conn.execute("SELECT COUNT(*) FROM rating_by_hash WHERE hash LIKE 'h-%'").fetchone()[0] == 5
  assert not os.path.exists(os.path.join(settings.pictures_dir, "y", "a.jpg.xmp"))
  log = [(l["photo_id"], l["cause"]) for l in curation.recent_activity(conn)]
  assert len(log) == 2 and {c for _, c in log} == {"import"}


def test_import_is_repeatable(conn, settings, tmp_path):
  build(conn, settings)
  db_path = metadata_db(str(tmp_path / "m.db"), [("h-a", "y/a.jpg", 4, ["y/a.jpg"])])
  import_ratings.import_ratings(conn, settings, db_path)
  rep = import_ratings.import_ratings(conn, settings, db_path)
  assert photo(conn, "y/a.jpg")["rating"] == 4
  assert len(curation.recent_activity(conn)) == 1     # no second log entry
  assert rep.applied == 1


def test_write_xmp_creates_sidecars_and_dry_run_does_not(conn, settings, tmp_path):
  build(conn, settings)
  db_path = metadata_db(str(tmp_path / "m.db"), [("h-a", "y/a.jpg", 4, ["y/a.jpg"])])
  dry = dataclasses.replace(settings, xmp_dry_run=True)
  import_ratings.import_ratings(conn, dry, db_path, write_xmp=True)
  assert not os.path.exists(os.path.join(settings.pictures_dir, "y", "a.jpg.xmp"))
  assert photo(conn, "y/a.jpg")["rating"] == 0
  import_ratings.import_ratings(conn, settings, db_path, write_xmp=True)
  sidecar = os.path.join(settings.pictures_dir, "y", "a.jpg.xmp")
  assert xmp.parse(open(sidecar, "rb").read()).rating == 4
  assert photo(conn, "y/a.jpg")["rating_source"] == "xmp"


def test_ambiguous_photo_is_reported_not_guessed(conn, settings, tmp_path):
  build(conn, settings)
  db_path = metadata_db(str(tmp_path / "m.db"), [
      ("h-e1", "y/e.dng", 5, ["y/e.dng"]),
      ("h-e2", "y/e.jpg", 2, ["y/e.jpg"])])
  rep = import_ratings.import_ratings(conn, settings, db_path)
  assert len(rep.ambiguous) == 1 and rep.applied == 0
  assert photo(conn, "y/e.dng")["rating"] == 0
