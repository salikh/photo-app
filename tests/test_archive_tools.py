"""tools/archive/ (ticket 133): run each relocated script as a real subprocess, never imported --
they define absl flags of their own (e.g. both catalog.py and import_sha224sum.py define --db),
which would collide with each other and with photoapp's own flags inside one pytest process.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(REPO, "tools", "archive", "catalog.py")
IMPORT_SHA224SUM = os.path.join(REPO, "tools", "archive", "import_sha224sum.py")
FIND_EMPTY_DIRS = os.path.join(REPO, "tools", "archive", "find_empty_dirs.py")


def run(script, args, **kw):
  return subprocess.run([sys.executable, script, "--logtostderr", *args],
                        capture_output=True, text=True, timeout=60, **kw)


def sha224(data):
  return hashlib.sha224(data).hexdigest()


def write(path, data=b"hello"):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "wb") as f:
    f.write(data)
  return path


def hashes(db_path):
  conn = sqlite3.connect(db_path)
  try:
    return dict(conn.execute("SELECT filename, hash FROM hashes"))
  finally:
    conn.close()


def test_catalog_hashes_a_tree_and_is_incremental(tmp_path):
  root = tmp_path / "pics"
  write(str(root / "a.jpg"), b"one")
  write(str(root / "2024" / "b.jpg"), b"two")
  db = str(tmp_path / "catalog.sqlite")

  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--v=2"])
  assert r.returncode == 0, r.stderr
  assert hashes(db) == {"a.jpg": sha224(b"one"), "2024/b.jpg": sha224(b"two")}

  # rerun: every file's mtime is unchanged, so nothing is re-hashed (both directories are skipped
  # entirely -- confirmed by the log line this tool already emits for that case).
  r2 = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--v=1"])
  assert r2.returncode == 0
  assert "Skipping unchanged directory" in r2.stderr
  assert hashes(db) == {"a.jpg": sha224(b"one"), "2024/b.jpg": sha224(b"two")}


def test_catalog_picks_up_an_added_file_and_drops_a_removed_one(tmp_path):
  """A directory's own mtime only moves when an entry is added/removed/renamed -- not when an
  existing file's content changes in place (photos in this archive are add/remove/rename, never
  edited in place) -- so that is what an incremental rerun is expected to notice."""
  root = tmp_path / "pics"
  write(str(root / "a.jpg"), b"one")
  write(str(root / "2024" / "b.jpg"), b"two")
  db = str(tmp_path / "catalog.sqlite")
  assert run(CATALOG, [f"--root_dir={root}", f"--db={db}"]).returncode == 0

  os.remove(root / "a.jpg")
  write(str(root / "2024" / "c.jpg"), b"three")
  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--v=1"])
  assert r.returncode == 0, r.stderr
  assert hashes(db) == {"2024/b.jpg": sha224(b"two"), "2024/c.jpg": sha224(b"three")}


def test_catalog_scoped_dir_only_touches_that_subtree(tmp_path):
  root = tmp_path / "pics"
  write(str(root / "2023" / "a.jpg"), b"one")
  write(str(root / "2024" / "b.jpg"), b"two")
  db = str(tmp_path / "catalog.sqlite")
  r = run(CATALOG, [f"--root_dir={root}", f"--dir={root}/2024", f"--db={db}"])
  assert r.returncode == 0, r.stderr
  assert hashes(db) == {"2024/b.jpg": sha224(b"two")}


def test_import_sha224sum_loads_a_plain_checksum_listing(tmp_path):
  digest = sha224(b"hello")
  listing = tmp_path / "checksums.sha224"
  listing.write_text(f"{digest}  ./2024/a.jpg\n")
  db = str(tmp_path / "catalog.sqlite")
  r = run(IMPORT_SHA224SUM, [f"--db={db}", f"--input={listing}", "--v=1"])
  assert r.returncode == 0, r.stderr
  assert hashes(db) == {"2024/a.jpg": digest}


def test_import_sha224sum_output_is_a_catalog_py_compatible_database(tmp_path):
  """The bridge format and catalog.py's own must be interchangeable (ticket 131's premise)."""
  digest = sha224(b"hello")
  listing = tmp_path / "checksums.sha224"
  listing.write_text(f"{digest}  ./a.jpg\n")
  db = str(tmp_path / "catalog.sqlite")
  assert run(IMPORT_SHA224SUM, [f"--db={db}", f"--input={listing}"]).returncode == 0

  root = tmp_path / "pics"
  write(str(root / "a.jpg"), b"hello")   # same content -- catalog.py should treat it as cached
  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}"])
  assert r.returncode == 0, r.stderr
  assert hashes(db) == {"a.jpg": digest}


def test_find_empty_dirs_finds_only_directories_with_no_files_anywhere(tmp_path):
  root = tmp_path / "pics"
  os.makedirs(root / "empty" / "also-empty")
  os.makedirs(root / "has-file")
  write(str(root / "has-file" / "a.jpg"))
  r = run(FIND_EMPTY_DIRS, [f"--dir={root}"])
  assert r.returncode == 0, r.stderr
  script = r.stdout
  assert f"rmdir -v -- {root}/empty/also-empty" in script
  assert f"rmdir -v -- {root}/empty\n" in script
  assert "has-file" not in script
  # deepest-first: the child is removed before its parent
  assert (script.index(f"rmdir -v -- {root}/empty/also-empty") <
          script.index(f"rmdir -v -- {root}/empty\n"))
