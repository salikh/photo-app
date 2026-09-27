"""tools/archive/compare.py (ticket 135), run as a real subprocess only.

Not imported directly: it defines required absl flags (--live, --target_db) of its own, which
`absl.flags.FLAGS`'s global validation checks on *any* later `FLAGS(argv)` call anywhere else in
the same pytest process (confirmed: importing this module directly broke an unrelated
tests/test_config.py test that calls `FLAGS(["prog", ...])`, since --live/--target_db then had no
value). This is the same reasoning tests/test_archive_tools.py already documents for catalog.py and
import_sha224sum.py.
"""

import json
import os
import sqlite3
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPARE = os.path.join(REPO, "tools", "archive", "compare.py")


def run(*args, timeout=30):
  return subprocess.run([sys.executable, COMPARE, "--logtostderr", *args],
                        capture_output=True, text=True, timeout=timeout)


def write_live(path, files=(), decisions=()):
  """files: [(path, hash)]; decisions: [(hash, rating, fav)]."""
  records = [{"type": "file", "path": p, "hash": h, "bytesize": 1} for p, h in files]
  records += [{"type": "decision", "hash": h, "rating": r, "fav": f, "rated_at": None}
             for h, r, f in decisions]
  path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def write_target(path, entries):
  """entries: [(filename, hash)]."""
  conn = sqlite3.connect(path)
  conn.execute("CREATE TABLE hashes (filename TEXT PRIMARY KEY, hash TEXT NOT NULL)")
  conn.executemany("INSERT INTO hashes VALUES (?, ?)", entries)
  conn.commit()
  conn.close()


def test_have_beats_a_stale_decision(tmp_path):
  # a hash that is both live and (for whatever historical reason) has a decision on record is
  # "have" -- the live library is what matters, not what a decision from the past says.
  live = tmp_path / "live.jsonl"
  write_live(live, files=[("k.jpg", "h")], decisions=[("h", -1, False)])
  target = tmp_path / "target.sqlite"
  write_target(target, [("k.jpg", "h")])
  r = run(f"--live={live}", f"--target_db={target}")
  assert r.returncode == 0, r.stderr
  assert "have\tk.jpg\th" in r.stdout and "# have: 1" in r.stdout
  assert "# rejected: 0" in r.stdout


def test_rejected_lost_new_and_a_fav_only_decision_counts_as_lost(tmp_path):
  live = tmp_path / "live.jsonl"
  write_live(live, decisions=[
      ("reject-h", -1, False),
      ("keeper-h", 4, False),
      ("fav-only-h", 0, True),   # rating 0 but favorited -- still a decision, still "lost"
  ])
  target = tmp_path / "target.sqlite"
  write_target(target, [
      ("a.jpg", "reject-h"), ("b.jpg", "keeper-h"), ("c.jpg", "fav-only-h"), ("d.jpg", "brand-new"),
  ])
  r = run(f"--live={live}", f"--target_db={target}")
  assert r.returncode == 0, r.stderr
  assert "rejected\ta.jpg\treject-h" in r.stdout
  assert "lost\tb.jpg\tkeeper-h" in r.stdout
  assert "lost\tc.jpg\tfav-only-h" in r.stdout
  assert "new\td.jpg\tbrand-new" in r.stdout
  assert "# have: 0\n# rejected: 1\n# lost: 2\n# new: 1" in r.stdout


def test_reverse_reports_only_live_hashes_absent_from_the_target(tmp_path):
  live = tmp_path / "live.jsonl"
  write_live(live, files=[("here.jpg", "shared"), ("only-here.jpg", "only-live")])
  target = tmp_path / "target.sqlite"
  write_target(target, [("x.jpg", "shared"), ("y.jpg", "unrelated-to-live")])
  r = run(f"--live={live}", f"--target_db={target}", "--reverse")
  assert r.returncode == 0, r.stderr
  assert "1 live hash(es) absent from the target copy" in r.stdout
  assert "only-live" in r.stdout and "shared" not in r.stdout.split("#\n")[-1]


def test_missing_required_flags_is_a_clear_usage_error(tmp_path):
  r = run()
  assert r.returncode != 0 and ("--live" in r.stderr or "live" in r.stderr)
