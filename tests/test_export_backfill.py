import os

from PIL import Image

from photoapp import export_backfill
from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg


def make_pattern_jpeg(path, half):
  """A 40x40 image, half black/half white -- gives dhash a real left/right edge to pick up on,
  unlike a flat solid color (whose dhash would be the same regardless of the actual color)."""
  os.makedirs(os.path.dirname(path), exist_ok=True)
  img = Image.new("RGB", (40, 40), "white")
  box = (0, 0, 20, 40) if half == "left" else (20, 0, 40, 40)
  img.paste((0, 0, 0), box)
  img.save(path)


def file_id(conn, path):
  return conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def test_tier1_links_from_a_still_live_job_after_the_manual_move(settings, conn):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  scan.scan(conn, d)
  fid = file_id(conn, "2020/a.jpg")
  old_root = os.path.join(os.path.dirname(d), "Exported")
  old_target = os.path.join(old_root, "2020", "a.jpg")
  make_jpeg(os.path.join(d, "Exported", "2020", "a.jpg"))   # the user's manual move landed it here
  scan.scan(conn, d)
  conn.execute(
      "INSERT INTO jobs (kind, file_id, state, target) VALUES ('export', ?, 'done', ?)",
      (fid, old_target))
  conn.commit()
  matched, checked = export_backfill.backfill_from_job_log(conn, d)
  assert (matched, checked) == (1, 1)
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/2020/a.jpg'").fetchone()
  assert linked["exported_from_file_id"] == fid


def test_tier1_skips_when_the_moved_file_does_not_exist(settings, conn):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  scan.scan(conn, d)
  fid = file_id(conn, "2020/a.jpg")
  old_root = os.path.join(os.path.dirname(d), "Exported")
  conn.execute(
      "INSERT INTO jobs (kind, file_id, state, target) VALUES ('export', ?, 'done', ?)",
      (fid, os.path.join(old_root, "2020", "a.jpg")))
  conn.commit()
  matched, checked = export_backfill.backfill_from_job_log(conn, d)
  assert (matched, checked) == (0, 1)


def test_tier1_skips_a_job_whose_target_is_outside_the_export_tree(settings, conn):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  scan.scan(conn, d)
  fid = file_id(conn, "2020/a.jpg")
  conn.execute(
      "INSERT INTO jobs (kind, file_id, state, target) VALUES ('export', ?, 'done', ?)",
      (fid, "/somewhere/else/a.jpg"))
  conn.commit()
  matched, checked = export_backfill.backfill_from_job_log(conn, d)
  assert (matched, checked) == (0, 1)


def test_tier1_does_not_relink_an_already_linked_file(settings, conn):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  make_jpeg(os.path.join(d, "2020", "other.jpg"))
  scan.scan(conn, d)
  fid = file_id(conn, "2020/a.jpg")
  other_id = file_id(conn, "2020/other.jpg")
  old_root = os.path.join(os.path.dirname(d), "Exported")
  make_jpeg(os.path.join(d, "Exported", "2020", "a.jpg"))
  scan.scan(conn, d)
  conn.execute("UPDATE files SET exported_from_file_id = ? WHERE path = 'Exported/2020/a.jpg'",
              (other_id,))
  conn.execute(
      "INSERT INTO jobs (kind, file_id, state, target) VALUES ('export', ?, 'done', ?)",
      (fid, os.path.join(old_root, "2020", "a.jpg")))
  conn.commit()
  matched, checked = export_backfill.backfill_from_job_log(conn, d)
  assert (matched, checked) == (0, 1)
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/2020/a.jpg'").fetchone()
  assert linked["exported_from_file_id"] == other_id   # untouched, not overwritten


def test_tier2_matches_a_true_pair_and_rejects_a_true_non_match(settings, conn):
  d = settings.pictures_dir
  make_pattern_jpeg(os.path.join(d, "2020", "match.jpg"), "left")
  make_pattern_jpeg(os.path.join(d, "2021", "match.jpg"), "right")   # same name, different photo
  scan.scan(conn, d)
  for rel in ("2020/match.jpg", "2021/match.jpg"):
    thumbs.ensure(conn, d, settings.thumbs_dir, file_id(conn, rel), rel, "Small")
  # the "export": ticket 098's own collision-suffix naming, same visual content as 2020/match.jpg
  make_pattern_jpeg(os.path.join(d, "Exported", "match-2.jpg"), "left")
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(conn, d, settings.thumbs_dir)
  assert (matched, unmatched) == (1, 0)
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/match-2.jpg'").fetchone()
  assert linked["exported_from_file_id"] == file_id(conn, "2020/match.jpg")


def test_tier2_leaves_unmatched_without_a_filename_candidate(settings, conn):
  d = settings.pictures_dir
  make_pattern_jpeg(os.path.join(d, "Exported", "mystery.jpg"), "left")
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(conn, d, settings.thumbs_dir)
  assert (matched, unmatched) == (0, 1)


def test_run_reports_both_tiers(settings, conn):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "2020", "a.jpg"))
  scan.scan(conn, d)
  make_jpeg(os.path.join(d, "Exported", "2020", "a.jpg"))
  scan.scan(conn, d)
  conn.execute(
      "INSERT INTO jobs (kind, file_id, state, target) VALUES ('export', ?, 'done', ?)",
      (file_id(conn, "2020/a.jpg"),
       os.path.join(os.path.dirname(d), "Exported", "2020", "a.jpg")))
  conn.commit()
  summary = export_backfill.run(conn, d, settings.thumbs_dir)
  assert summary["tier1_matched"] == 1 and summary["tier1_checked"] == 1
  assert summary["tier2_matched"] == 0 and summary["tier2_unmatched"] == 0
