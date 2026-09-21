import os

from photoapp import scan
from photoapp import survey
from tests.conftest import make_jpeg
from tests.test_grouping import touch
from tests.test_scan_sidecars import XMP, write


def test_survey_counts_agreements_conflicts_and_orphans(conn, settings):
  d = settings.pictures_dir
  for n in ("a", "b", "c"):
    touch(os.path.join(d, n + ".DNG"))
    make_jpeg(os.path.join(d, n + ".JPG"))
  # a: agree; b: disagree, JPG sidecar newer; c: disagree, DNG sidecar newer
  write(os.path.join(d, "a.DNG.xmp"), XMP % (2, ""), mtime=100)
  write(os.path.join(d, "a.JPG.xmp"), XMP % (2, ""), mtime=200)
  write(os.path.join(d, "b.DNG.xmp"), XMP % (1, ""), mtime=100)
  write(os.path.join(d, "b.JPG.xmp"), XMP % (4, ""), mtime=200)
  write(os.path.join(d, "c.DNG.xmp"), XMP % (5, ""), mtime=300)
  write(os.path.join(d, "c.JPG.xmp"), XMP % (1, ""), mtime=200)
  write(os.path.join(d, "gone.JPG.xmp"), XMP % (3, ""), mtime=100)
  scan.scan(conn, d)
  r = survey.survey(conn)
  assert r["totals"]["photos"] == 3 and r["totals"]["multi_file"] == 3
  assert r["totals"]["sidecars"] == 7 and r["totals"]["orphans"] == 1
  assert r["conflicts"] == 2 and r["rating_conflicts"] == 2
  assert r["newest_is_original"] == 1 and r["newest_is_other"] == 1
  assert r["disagreement_pairs"] == {(1, 4): 1, (1, 5): 1}
  assert r["rating_distribution"] == {2: 1, 4: 1, 5: 1}
  text = survey.format_report(r)
  assert "disagree: 2" in text.replace("sidecars disagree: 2 ", "disagree: 2 ") and "gone" not in text


def test_survey_and_attention_report_sidecars_behind_and_overruled_rejects(conn, settings):
  import time as time_
  from fastapi.testclient import TestClient
  from photoapp import api
  d = settings.pictures_dir
  for n in ("a", "b"):
    touch(os.path.join(d, n + ".DNG"))
    make_jpeg(os.path.join(d, n + ".JPG"))
  write(os.path.join(d, "a.DNG.xmp"), XMP % (-1, ""), mtime=200)       # a: DNG reject, JPG picked
  write(os.path.join(d, "a.JPG.xmp"), XMP % (1, ""), mtime=100)
  write(os.path.join(d, "b.DNG.xmp"), XMP % (2, ""), mtime=100)        # b: the database gets a newer rating
  write(os.path.join(d, "b.JPG.xmp"), XMP % (2, ""), mtime=100)
  scan.scan(conn, d)
  pid = conn.execute("SELECT photo_id FROM files WHERE path = 'b.DNG'").fetchone()[0]
  conn.execute("UPDATE photos SET rating = 5, rating_updated_at = ?, rating_source = 'import' WHERE id = ?", (time_.time(), pid))
  conn.commit()
  from photoapp import ratings
  ratings.refresh_photo(conn, pid)
  r = survey.survey(conn)
  assert r["rejects_overruled"] == 1 and r["sidecars_behind"] == 1
  assert "single-file rejects overruled by a picked file: 1" in survey.format_report(r)
  data = TestClient(api.create_app(conn, settings)).get("/api/attention").json()
  assert [x["path"] for x in data["sidecars_behind"]] == ["b.JPG"]        # a pair is listed by its camera JPG
