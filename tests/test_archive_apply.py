"""tools/archive/apply.py and catalog_lib.py's plan/format_script/apply_copies (ticket 137).

catalog_lib has no absl flags, so its functions are unit tested by direct import. apply.py itself
defines required flags of its own (--live/--target_db/--target_root), so -- same reasoning as
tests/test_archive_compare.py -- it is only ever exercised as a real subprocess.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tools", "archive"))
import catalog_lib  # noqa: E402

from photoapp import curation
from photoapp import db as db_lib
from photoapp import scan
from photoapp import trash
from tests.conftest import make_jpeg

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPLY = os.path.join(REPO, "tools", "archive", "apply.py")
CATALOG = os.path.join(REPO, "tools", "archive", "catalog.py")


# --- catalog_lib.plan/format_script/apply_copies: unit tests -----------------------------------


def test_plan_skips_have_and_only_sets_a_dest_for_lost_and_new():
  buckets = {"have": [("h.jpg", "hh")], "rejected": [("r.jpg", "rh")],
            "lost": [("l.jpg", "lh")], "new": [("n.jpg", "nh")]}
  actions = catalog_lib.plan(buckets, "/pics")
  assert ("rejected", "r.jpg", None) in actions
  assert ("lost", "l.jpg", os.path.join("/pics", "l.jpg")) in actions
  assert ("new", "n.jpg", os.path.join("/pics", "n.jpg")) in actions
  assert not any(a[0] == "have" for a in actions)


def test_format_script_rm_for_rejected_cp_n_for_the_rest():
  actions = [("rejected", "old/r.jpg", None), ("lost", "l.jpg", "/pics/l.jpg")]
  script = catalog_lib.format_script(actions, "/backup", "/pics")
  assert "rm -v -- /backup/old/r.jpg" in script
  assert "mkdir -p -- /pics" in script
  assert "cp -v -n -- /backup/l.jpg /pics/l.jpg" in script
  assert script.startswith("#!/bin/sh\n")


def test_apply_copies_copies_lost_and_new_never_rejected(tmp_path):
  target = tmp_path / "backup"
  os.makedirs(target)
  (target / "l.jpg").write_bytes(b"lost-content")
  (target / "n.jpg").write_bytes(b"new-content")
  (target / "r.jpg").write_bytes(b"should-never-be-touched")

  dest_dir = tmp_path / "pics"
  actions = [("rejected", "r.jpg", None),
            ("lost", "l.jpg", str(dest_dir / "l.jpg")),
            ("new", "sub/n.jpg", str(dest_dir / "sub" / "n.jpg"))]
  # note: the "new" action's source is "sub/n.jpg" on purpose to also exercise dest-dir creation;
  # give the physical file at target/sub/n.jpg to match.
  os.makedirs(target / "sub")
  (target / "sub" / "n.jpg").write_bytes(b"new-content")

  copied, skipped = catalog_lib.apply_copies(actions, str(target))
  assert set(copied) == {str(dest_dir / "l.jpg"), str(dest_dir / "sub" / "n.jpg")}
  assert skipped == []
  assert (dest_dir / "l.jpg").read_bytes() == b"lost-content"
  assert (dest_dir / "sub" / "n.jpg").read_bytes() == b"new-content"
  assert not (dest_dir / "r.jpg").exists()   # rejected is never copied


def test_apply_copies_never_overwrites_an_existing_destination(tmp_path):
  target = tmp_path / "backup"
  os.makedirs(target)
  (target / "l.jpg").write_bytes(b"from-backup")
  dest_dir = tmp_path / "pics"
  os.makedirs(dest_dir)
  (dest_dir / "l.jpg").write_bytes(b"already-here")

  copied, skipped = catalog_lib.apply_copies(
      [("lost", "l.jpg", str(dest_dir / "l.jpg"))], str(target))
  assert copied == [] and skipped == [(str(dest_dir / "l.jpg"), "already exists")]
  assert (dest_dir / "l.jpg").read_bytes() == b"already-here"


# --- apply.py CLI: subprocess end-to-end --------------------------------------------------------


def photo_id(conn, path):
  return conn.execute("SELECT photo_id FROM files WHERE path = ?", (path,)).fetchone()[0]


@pytest.fixture
def library(tmp_path, settings):
  """A live library with one kept photo and one rejected-and-trashed one, exported to live.jsonl,
  plus a target copy (backup) with the kept file, the (elsewhere-undeleted) rejected file, and a
  brand-new file -- everything the CLI end-to-end tests need."""
  make_jpeg(os.path.join(settings.pictures_dir, "keep.jpg"), color="red")
  make_jpeg(os.path.join(settings.pictures_dir, "gone.jpg"), color="blue")
  conn = db_lib.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)
  pid = photo_id(conn, "gone.jpg")
  curation.set_rating(conn, settings, pid, -1)
  trash.trash_photo(conn, settings, pid)

  live = tmp_path / "live.jsonl"
  subprocess.run([sys.executable, "-m", "photoapp.export_catalog",
                  f"--state_dir={settings.state_dir}", f"--output={live}"],
                 check=True, cwd=REPO, timeout=30)

  backup = tmp_path / "backup"
  make_jpeg(str(backup / "keep.jpg"), color="red")
  make_jpeg(str(backup / "gone.jpg"), color="blue")
  make_jpeg(str(backup / "brandnew.jpg"), color="green")
  target_db = tmp_path / "backup.sqlite"
  subprocess.run([sys.executable, CATALOG, f"--root_dir={backup}", f"--db={target_db}"],
                 check=True, timeout=30)
  return {"live": live, "target_db": target_db, "backup": backup, "settings": settings}


def run_apply(lib, *extra_args, timeout=30):
  return subprocess.run(
      [sys.executable, APPLY, "--logtostderr", "--config=none",
       f"--pictures_dir={lib['settings'].pictures_dir}", f"--state_dir={lib['settings'].state_dir}",
       f"--live={lib['live']}", f"--target_db={lib['target_db']}", f"--target_root={lib['backup']}",
       *extra_args],
      capture_output=True, text=True, timeout=timeout, cwd=REPO)


def test_dry_run_prints_a_script_and_touches_nothing(library):
  r = run_apply(library)
  assert r.returncode == 0, r.stderr
  assert f"cp -v -n -- {library['backup']}/brandnew.jpg" in r.stdout
  assert f"rm -v -- {library['backup']}/gone.jpg" in r.stdout
  assert "keep.jpg" not in r.stdout   # "have": no action at all
  assert not os.path.exists(os.path.join(library["settings"].pictures_dir, "brandnew.jpg"))
  assert os.path.exists(os.path.join(library["backup"], "gone.jpg"))   # never deleted by this tool


def test_apply_copies_new_and_lost_but_never_deletes_the_rejected_target_file(library):
  r = run_apply(library, "--apply")
  assert r.returncode == 0, r.stderr
  pics = library["settings"].pictures_dir
  assert os.path.exists(os.path.join(pics, "brandnew.jpg"))
  with open(os.path.join(pics, "brandnew.jpg"), "rb") as f:
    with open(os.path.join(library["backup"], "brandnew.jpg"), "rb") as g:
      assert f.read() == g.read()
  assert os.path.exists(os.path.join(library["backup"], "gone.jpg"))   # rejected: never touched
  assert not os.path.exists(os.path.join(pics, "gone.jpg"))            # and never re-added here


def test_apply_is_repeatable_and_skips_an_existing_destination(library):
  assert run_apply(library, "--apply").returncode == 0
  r2 = run_apply(library, "--apply")
  assert r2.returncode == 0, r2.stderr
  assert "skipped 1" in r2.stderr


def test_a_live_photo_that_gets_imported_appears_as_a_normal_photo_after_a_rescan(library):
  """The 'no pending-import state' promise in apply.py's docstring, checked for real."""
  assert run_apply(library, "--apply").returncode == 0
  settings = library["settings"]
  conn = db_lib.open_state(settings.state_dir)
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)
  row = conn.execute("SELECT rating, missing FROM files f JOIN photos p ON p.id = f.photo_id "
                     "WHERE f.path = 'brandnew.jpg'").fetchone()
  assert row["missing"] == 0 and row["rating"] == 0   # an ordinary, unrated Photo
