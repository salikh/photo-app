import os
import subprocess
import sys

from photoapp import db
from tests.test_scan import build_years


def run(args):
  return subprocess.run([sys.executable, "-m", "photoapp.fullscan", "--logtostderr"] + args,
                        capture_output=True, text=True, timeout=120,
                        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_fullscan_command_scans_year_by_year_and_logs_progress(tmp_path):
  pics, state = str(tmp_path / "pics"), str(tmp_path / "state")
  os.makedirs(pics)
  build_years(pics)
  r = run([f"--pictures_dir={pics}", f"--state_dir={state}", f"--thumbs_dir={tmp_path / 't'}",
           "--scan_workers=2"])
  assert r.returncode == 0, r.stderr[-2000:]
  for step in ("(files in the root) (1/4)", "2019 (2/4)", "2020 (3/4)", "2021 (4/4)"):
    assert "done " + step in r.stderr, r.stderr[-2000:]
  assert "peak memory" in r.stderr
  conn = db.open_state(state)
  assert conn.execute("SELECT COUNT(*) FROM files WHERE photo_id IS NULL").fetchone()[0] == 0
  assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 7


def test_fullscan_scan_dirs_and_hashes_db(tmp_path):
  import sqlite3
  pics, state = str(tmp_path / "pics"), str(tmp_path / "state")
  os.makedirs(pics)
  build_years(pics)
  hashes = str(tmp_path / "h.db")
  c = sqlite3.connect(hashes)
  c.execute("CREATE TABLE hashes (filename TEXT PRIMARY KEY, hash TEXT, mtime REAL)")
  st = os.stat(os.path.join(pics, "2019", "sub", "b.jpg"))
  c.execute("INSERT INTO hashes VALUES ('2019/sub/b.jpg', 'precomputed', ?)", (st.st_mtime,))
  c.commit()
  c.close()
  r = run([f"--pictures_dir={pics}", f"--state_dir={state}", f"--thumbs_dir={tmp_path / 't'}",
           "--scan_dirs=2019", f"--hashes_db={hashes}", "--scan_workers=2"])
  assert r.returncode == 0, r.stderr[-2000:]
  assert "done 2019 (1/1)" in r.stderr
  conn = db.open_state(state)
  assert {p.split("/")[0] for (p,) in conn.execute("SELECT path FROM files")} == {"2019"}
  assert conn.execute("SELECT hash FROM files WHERE path = '2019/sub/b.jpg'").fetchone()[0] == "precomputed"
  bad = run([f"--pictures_dir={pics}", f"--state_dir={state}", "--scan_dirs=nope"])
  assert bad.returncode == 0 and "not a directory" in bad.stderr      # skipped with a warning
