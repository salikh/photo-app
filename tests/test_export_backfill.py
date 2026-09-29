import os

from PIL import Image

from photoapp import export_backfill
from photoapp import scan
from photoapp import thumbs
from tests.conftest import make_jpeg
from tests.test_grouping import touch


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


def test_tier2_breaks_an_exact_tie_in_favor_of_the_case_insensitive_name_match(settings, conn):
  # ticket 151: the real-library-observed pattern -- a RAW's sibling JPEG and (a stand-in for) the
  # RAW itself are equally close to the export (same visual content), but only the JPEG's full
  # name (case-insensitively, extension included) matches the export's own filename exactly.
  d = settings.pictures_dir
  make_pattern_jpeg(os.path.join(d, "2020", "K1.jpg"), "left")     # stands in for the JPEG sibling
  make_pattern_jpeg(os.path.join(d, "2020", "K1.jpeg"), "left")    # same content -- an exact tie
  scan.scan(conn, d)
  for rel in ("2020/K1.jpg", "2020/K1.jpeg"):
    thumbs.ensure(conn, d, settings.thumbs_dir, file_id(conn, rel), rel, "Small")
  make_pattern_jpeg(os.path.join(d, "Exported", "K1.JPG"), "left")   # matches K1.jpg's name exactly
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(conn, d, settings.thumbs_dir)
  assert (matched, unmatched) == (1, 0)
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/K1.JPG'").fetchone()
  assert linked["exported_from_file_id"] == file_id(conn, "2020/K1.jpg")


def test_tier2_name_tie_break_also_requires_a_plausible_date(settings, conn):
  # ticket 151 (the user's follow-up refinement): the name-match tie-break additionally requires
  # the winning candidate's mtime to be plausibly close to the export's (or a shared year-like
  # path component) -- a guard against a coincidental name+dhash tie between unrelated eras.
  d = settings.pictures_dir
  make_pattern_jpeg(os.path.join(d, "2020", "K1.jpg"), "left")     # the name-matching candidate
  make_pattern_jpeg(os.path.join(d, "2020", "K1.jpeg"), "left")    # tied on dhash, not on name
  # Push K1.jpg's mtime far into the past (1970) -- nowhere near "now" (the export's mtime below)
  # and no shared year token with "Exported/K1.JPG" (which has none at all), so
  # _dates_plausible genuinely fails for it.
  far_mtime = 1_000_000
  os.utime(os.path.join(d, "2020", "K1.jpg"), (far_mtime, far_mtime))
  scan.scan(conn, d)
  for rel in ("2020/K1.jpg", "2020/K1.jpeg"):
    thumbs.ensure(conn, d, settings.thumbs_dir, file_id(conn, rel), rel, "Small")
  make_pattern_jpeg(os.path.join(d, "Exported", "K1.JPG"), "left")
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(conn, d, settings.thumbs_dir)
  assert (matched, unmatched) == (0, 1)   # the date guard blocks the name-only tie-break
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/K1.JPG'").fetchone()
  assert linked["exported_from_file_id"] is None


def test_tier2_name_tie_break_accepts_a_shared_year_path_despite_a_far_mtime(settings, conn):
  # The OR condition: a shared year-like path component rescues a tie-break even when mtime alone
  # would fail the plausibility check.
  d = settings.pictures_dir
  make_pattern_jpeg(os.path.join(d, "2020", "K1.jpg"), "left")
  make_pattern_jpeg(os.path.join(d, "2020", "K1.jpeg"), "left")
  far_mtime = 1_000_000
  os.utime(os.path.join(d, "2020", "K1.jpg"), (far_mtime, far_mtime))
  scan.scan(conn, d)
  for rel in ("2020/K1.jpg", "2020/K1.jpeg"):
    thumbs.ensure(conn, d, settings.thumbs_dir, file_id(conn, rel), rel, "Small")
  # This export's own path carries "2020" too, matching K1.jpg's directory year.
  make_pattern_jpeg(os.path.join(d, "Exported", "2020", "K1.JPG"), "left")
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(conn, d, settings.thumbs_dir)
  assert (matched, unmatched) == (1, 0)
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/2020/K1.JPG'").fetchone()
  assert linked["exported_from_file_id"] == file_id(conn, "2020/K1.jpg")


def test_candidate_stems_includes_the_double_extension_convention():
  # ticket 153: "xyz.DNG.jpg" also ties on the plain "xyz" stem (xyz.DNG/xyz.JPG's own), on top
  # of the pre-existing "xyz.dng" (splitext's plain result).
  assert list(export_backfill._candidate_stems("xyz.DNG.jpg")) == ["xyz.dng", "xyz"]
  assert list(export_backfill._candidate_stems("xyz.JPG")) == ["xyz"]   # non-RAW: unaffected


def test_tier2_resolves_the_double_extension_convention_to_the_raw(settings, conn):
  # ticket 153: an export literally named after its RAW sibling's full filename (K___1293.DNG.jpg
  # in the real library) previously found no candidates at all -- now it's found, ties against its
  # JPEG sibling on dhash, and _name_matches resolves the tie to the RAW specifically (the JPEG
  # sibling's plain name doesn't match this convention either way).
  d = settings.pictures_dir
  touch(os.path.join(d, "2020", "xyz.DNG"))
  make_pattern_jpeg(os.path.join(d, "2020", "xyz.JPG"), "left")
  scan.scan(conn, d)
  # The RAW's own Small cache, written directly (never actually rendered from real RAW bytes) --
  # backfill_by_dhash only ever reads an existing cached preview, never forces a fresh render.
  make_pattern_jpeg(thumbs.thumb_path(settings.thumbs_dir, "Small", "2020/xyz.DNG"), "left")
  thumbs.ensure(conn, d, settings.thumbs_dir, file_id(conn, "2020/xyz.JPG"), "2020/xyz.JPG", "Small")
  make_pattern_jpeg(os.path.join(d, "Exported", "xyz.DNG.jpg"), "left")
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(conn, d, settings.thumbs_dir)
  assert (matched, unmatched) == (1, 0)
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/xyz.DNG.jpg'").fetchone()
  assert linked["exported_from_file_id"] == file_id(conn, "2020/xyz.DNG")


def test_tier2_stays_ambiguous_when_both_tied_candidates_match_the_name(settings, conn):
  # Two unrelated photos in different folders happen to share an exact filename (a real pattern
  # seen in the library, e.g. camera-numbering resets across import batches) -- the name-match
  # tie-break can't distinguish them either, since both match equally, so this must stay ambiguous
  # rather than picking one arbitrarily.
  d = settings.pictures_dir
  make_pattern_jpeg(os.path.join(d, "2020", "dup.jpg"), "left")
  make_pattern_jpeg(os.path.join(d, "2021", "dup.jpg"), "left")   # same name, same visual content
  scan.scan(conn, d)
  for rel in ("2020/dup.jpg", "2021/dup.jpg"):
    thumbs.ensure(conn, d, settings.thumbs_dir, file_id(conn, rel), rel, "Small")
  make_pattern_jpeg(os.path.join(d, "Exported", "dup.jpg"), "left")
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(conn, d, settings.thumbs_dir)
  assert (matched, unmatched) == (0, 1)
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/dup.jpg'").fetchone()
  assert linked["exported_from_file_id"] is None


def test_tier2_name_match_never_overrides_a_worse_dhash_distance(settings, conn):
  # ticket 151: "still guard on the dhash similarity" -- a name-matching candidate that is not
  # visually close must not win just because its name matches; the closer, differently-named
  # candidate (not a tie) still wins.
  d = settings.pictures_dir
  make_pattern_jpeg(os.path.join(d, "2020", "other.jpg"), "left")    # the true visual match
  make_pattern_jpeg(os.path.join(d, "2020", "K1.jpg"), "right")      # matches the export's name,
                                                                      # but visually different
  scan.scan(conn, d)
  for rel in ("2020/other.jpg", "2020/K1.jpg"):
    thumbs.ensure(conn, d, settings.thumbs_dir, file_id(conn, rel), rel, "Small")
  make_pattern_jpeg(os.path.join(d, "Exported", "other-2.jpg"), "left")
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(conn, d, settings.thumbs_dir)
  assert (matched, unmatched) == (1, 0)
  linked = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/other-2.jpg'").fetchone()
  assert linked["exported_from_file_id"] == file_id(conn, "2020/other.jpg")


def test_tier2_dir_prefix_scopes_unresolved_to_one_directory(settings, conn):
  # ticket 146: link_exports_job scopes backfill_by_dhash to the directory a move just landed
  # files in, rather than the whole Exported/ subtree.
  d = settings.pictures_dir
  make_pattern_jpeg(os.path.join(d, "2020", "match.jpg"), "left")
  scan.scan(conn, d)
  thumbs.ensure(conn, d, settings.thumbs_dir, file_id(conn, "2020/match.jpg"),
               "2020/match.jpg", "Small")
  make_pattern_jpeg(os.path.join(d, "Exported", "2020", "match.jpg"), "left")
  make_pattern_jpeg(os.path.join(d, "Exported", "2021", "match.jpg"), "left")
  scan.scan(conn, d)
  matched, unmatched = export_backfill.backfill_by_dhash(
      conn, d, settings.thumbs_dir, dir_prefix="Exported/2020")
  assert (matched, unmatched) == (1, 0)
  linked_2020 = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/2020/match.jpg'").fetchone()
  linked_2021 = conn.execute(
      "SELECT exported_from_file_id FROM files WHERE path = 'Exported/2021/match.jpg'").fetchone()
  assert linked_2020["exported_from_file_id"] is not None
  assert linked_2021["exported_from_file_id"] is None   # out of scope, left untouched


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
