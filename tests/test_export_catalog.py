import io
import json
import os
import subprocess
import sys

from photoapp import curation
from photoapp import db as db_lib
from photoapp import export_catalog
from photoapp import scan
from tests.conftest import make_jpeg

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def photo_id(conn, path):
  return conn.execute("SELECT photo_id FROM files WHERE path = ?", (path,)).fetchone()[0]


def lines(conn):
  buf = io.StringIO()
  export_catalog.write(conn, buf)
  return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_exports_every_non_missing_file_with_its_hash_and_size(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"), size=(10, 10))
  make_jpeg(os.path.join(settings.pictures_dir, "y", "b.jpg"), size=(20, 10))
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)

  files = {r["path"]: r for r in lines(conn) if r["type"] == "file"}
  assert set(files) == {"a.jpg", "y/b.jpg"}
  a = conn.execute("SELECT hash, bytesize FROM files WHERE path = 'a.jpg'").fetchone()
  assert files["a.jpg"]["hash"] == a["hash"] and files["a.jpg"]["bytesize"] == a["bytesize"]


def test_a_missing_file_is_not_exported(conn, settings):
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_jpeg(path)
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)
  os.remove(path)
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)

  assert conn.execute("SELECT missing FROM files WHERE path = 'a.jpg'").fetchone()["missing"] == 1
  assert [r for r in lines(conn) if r["type"] == "file"] == []


def test_rejecting_and_deleting_a_photo_still_exports_its_decision(conn, settings):
  """The durable half of ticket 131's premise: rating_by_hash outlives the file it came from."""
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_jpeg(path)
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)
  pid = photo_id(conn, "a.jpg")
  file_hash = conn.execute("SELECT hash FROM files WHERE path = 'a.jpg'").fetchone()["hash"]
  curation.set_rating(conn, settings, pid, -1)

  from photoapp import trash
  trash.trash_photo(conn, settings, pid)
  assert conn.execute("SELECT missing FROM files WHERE path = 'a.jpg'").fetchone()["missing"] == 1

  records = lines(conn)
  assert [r for r in records if r["type"] == "file"] == []   # gone from the live catalog
  decisions = {r["hash"]: r for r in records if r["type"] == "decision"}
  assert decisions[file_hash]["rating"] == -1 and decisions[file_hash]["fav"] is False


def test_a_kept_photo_that_goes_missing_is_a_recoverable_decision_not_a_silent_gap(conn, settings):
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_jpeg(path)
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)
  pid = photo_id(conn, "a.jpg")
  file_hash = conn.execute("SELECT hash FROM files WHERE path = 'a.jpg'").fetchone()["hash"]
  curation.set_rating(conn, settings, pid, 5)

  os.remove(path)                      # vanished some other way than trash.trash_photo
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)

  records = lines(conn)
  assert [r for r in records if r["type"] == "file"] == []
  decisions = {r["hash"]: r for r in records if r["type"] == "decision"}
  assert decisions[file_hash]["rating"] == 5    # a keeper, not a reject -- tools/archive/compare.py
                                                 # (135) tells these two cases apart by this field


def test_summary_line_counts_match(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  make_jpeg(os.path.join(settings.pictures_dir, "b.jpg"))
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)
  curation.set_rating(conn, settings, photo_id(conn, "a.jpg"), 3)

  records = lines(conn)
  summary = records[-1]
  assert summary == {"type": "summary",
                     "files": len([r for r in records if r["type"] == "file"]),
                     "decisions": len([r for r in records if r["type"] == "decision"])}
  assert summary["files"] == 2 and summary["decisions"] == 1


def test_output_is_one_json_object_per_line(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir, thumbs_dir=settings.thumbs_dir)
  buf = io.StringIO()
  export_catalog.write(conn, buf)
  for line in buf.getvalue().splitlines():
    assert json.loads(line)["type"] in ("file", "decision", "summary")


def test_cli_writes_to_stdout_and_to_an_output_file(tmp_path):
  pics = tmp_path / "pics"
  make_jpeg(str(pics / "a.jpg"))
  state = str(tmp_path / "state")
  scan.scan(db_lib.open_state(state), str(pics), thumbs_dir=str(tmp_path / "thumbs"))

  def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "photoapp.export_catalog", "--logtostderr",
         f"--state_dir={state}", *args],
        capture_output=True, text=True, timeout=30, cwd=REPO)

  r = run()
  assert r.returncode == 0, r.stderr
  records = [json.loads(line) for line in r.stdout.splitlines()]
  assert any(rec["type"] == "file" and rec["path"] == "a.jpg" for rec in records)

  output = tmp_path / "catalog.jsonl"
  r2 = run(f"--output={output}")
  assert r2.returncode == 0 and r2.stdout == ""
  assert [json.loads(l) for l in output.read_text().splitlines()] == records
