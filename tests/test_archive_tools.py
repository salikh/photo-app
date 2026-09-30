"""tools/archive/ (ticket 133): run each relocated script as a real subprocess, never imported --
they define absl flags of their own (e.g. both catalog.py and import_sha224sum.py define --db),
which would collide with each other and with photoapp's own flags inside one pytest process.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys

from tests.conftest import make_jpeg

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


# --- ticket 141: --write_metadata_json ----------------------------------------

def seed_index_json(dirpath, name, record):
  """Write dirpath/index.json + dirpath/<name>.json exactly like photoapp/metacache.py would."""
  from photoapp import metacache
  metacache.write_dir(str(dirpath), os.stat(dirpath).st_mtime, {name: record})


def load_records(dirpath):
  from photoapp import metacache
  return metacache.load_records(str(dirpath))


def test_catalog_reuses_a_cached_hash_from_metadata_json_instead_of_hashing(tmp_path):
  root = tmp_path / "pics"
  path = write(str(root / "a.jpg"), b"real content")
  st = os.stat(path)
  fake_hash = "f" * 56   # distinguishable from sha224(b"real content")
  seed_index_json(root, "a.jpg", {"hash": fake_hash, "mtime": st.st_mtime, "bytesize": st.st_size})
  db = str(tmp_path / "catalog.sqlite")

  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json", "--v=2"])
  assert r.returncode == 0, r.stderr
  assert hashes(db) == {"a.jpg": fake_hash}
  assert "Reusing cached hash" in r.stderr


def test_catalog_ignores_a_metadata_json_record_with_a_stale_mtime(tmp_path):
  root = tmp_path / "pics"
  path = write(str(root / "a.jpg"), b"real content")
  fake_hash = "f" * 56
  seed_index_json(root, "a.jpg", {"hash": fake_hash, "mtime": 1.0, "bytesize": 999})
  db = str(tmp_path / "catalog.sqlite")

  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json"])
  assert r.returncode == 0, r.stderr
  assert hashes(db) == {"a.jpg": sha224(b"real content")}


def test_catalog_without_the_flag_ignores_metadata_json_entirely(tmp_path):
  root = tmp_path / "pics"
  write(str(root / "a.jpg"), b"real content")
  fake_hash = "f" * 56
  seed_index_json(root, "a.jpg", {"hash": fake_hash, "mtime": os.stat(root / "a.jpg").st_mtime,
                                  "bytesize": os.stat(root / "a.jpg").st_size})
  db = str(tmp_path / "catalog.sqlite")

  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}"])   # no --write_metadata_json
  assert r.returncode == 0, r.stderr
  assert hashes(db) == {"a.jpg": sha224(b"real content")}   # computed for real, cache ignored


def test_catalog_writes_a_freshly_computed_hash_back_into_metadata_json(tmp_path):
  root = tmp_path / "pics"
  path = write(str(root / "a.jpg"), b"real content")
  db = str(tmp_path / "catalog.sqlite")

  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json"])
  assert r.returncode == 0, r.stderr
  record = load_records(root)["a.jpg"]
  assert record["hash"] == sha224(b"real content")
  assert record["mtime"] == os.stat(path).st_mtime
  assert record["bytesize"] == os.stat(path).st_size


def test_catalog_write_preserves_exif_fields_it_did_not_itself_compute(tmp_path):
  root = tmp_path / "pics"
  path = write(str(root / "a.jpg"), b"real content")
  # A stale-mtime record (forces a real re-hash) that still carries fields only photoapp's own
  # scanner would have populated -- these must survive catalog.py's write.
  seed_index_json(root, "a.jpg", {"hash": "f" * 56, "mtime": 1.0, "bytesize": 999,
                                  "width": 640, "height": 480, "camera_make": "Pentax"})
  db = str(tmp_path / "catalog.sqlite")

  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json"])
  assert r.returncode == 0, r.stderr
  record = load_records(root)["a.jpg"]
  assert record["hash"] == sha224(b"real content") and record["mtime"] == os.stat(path).st_mtime
  assert record["width"] == 640 and record["camera_make"] == "Pentax"


def test_catalog_does_not_catalog_the_metadata_json_files_themselves(tmp_path):
  root = tmp_path / "pics"
  write(str(root / "a.jpg"), b"real content")
  db = str(tmp_path / "catalog.sqlite")

  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json"])
  assert r.returncode == 0, r.stderr
  assert set(hashes(db)) == {"a.jpg"}   # not index.json / a.jpg.json


# --- ticket 149: --metadata_json_fields ----------------------------------------

def test_catalog_default_metadata_json_fields_stays_hash_only(tmp_path):
  root = tmp_path / "pics"
  make_jpeg(str(root / "a.jpg"))
  db = str(tmp_path / "catalog.sqlite")
  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json"])
  assert r.returncode == 0, r.stderr
  record = load_records(root)["a.jpg"]
  assert set(record) == {"hash", "mtime", "bytesize"}   # ticket 141 behavior, unchanged


def test_catalog_metadata_json_fields_dimensions_only(tmp_path):
  root = tmp_path / "pics"
  make_jpeg(str(root / "a.jpg"), size=(30, 20))
  db = str(tmp_path / "catalog.sqlite")
  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json",
                    "--metadata_json_fields=dimensions"])
  assert r.returncode == 0, r.stderr
  record = load_records(root)["a.jpg"]
  assert record["width"] == 30 and record["height"] == 20 and record["mime_type"] == "image/jpeg"
  assert "aperture" not in record and "exif_date" not in record   # exif group not requested


def test_catalog_metadata_json_fields_exif(tmp_path):
  root = tmp_path / "pics"
  make_jpeg(str(root / "a.jpg"))
  db = str(tmp_path / "catalog.sqlite")
  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json",
                    "--metadata_json_fields=exif"])
  assert r.returncode == 0, r.stderr
  record = load_records(root)["a.jpg"]
  # make_jpeg's images carry no real EXIF, so these come back None -- the point is the *keys* are
  # present (computed), unlike the dimensions group, which wasn't requested.
  assert set(record) >= {"exif_date", "aperture", "shutter_speed", "iso", "focal_length",
                         "camera_make", "camera_model", "lens_model"}
  assert "width" not in record


def test_catalog_metadata_json_fields_rejects_an_unknown_group(tmp_path):
  root = tmp_path / "pics"
  make_jpeg(str(root / "a.jpg"))
  db = str(tmp_path / "catalog.sqlite")
  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json",
                    "--metadata_json_fields=nonsense"])
  assert r.returncode != 0


def test_catalog_metadata_json_fields_does_not_recompute_when_already_present(tmp_path):
  root = tmp_path / "pics"
  make_jpeg(str(root / "a.jpg"))
  from photoapp import metacache
  # A pre-existing record already has dimensions (with obviously-fake sentinel values) -- this
  # run must leave them alone rather than re-decoding, whether or not its hash is reused.
  metacache.write_dir(str(root), os.stat(str(root / "a.jpg")).st_mtime, {
      "a.jpg": {"hash": "not-a-real-hash", "mtime": 1.0, "bytesize": 1,
               "width": 999, "height": 999, "mime_type": "sentinel"}})
  db = str(tmp_path / "catalog.sqlite")
  r = run(CATALOG, [f"--root_dir={root}", f"--db={db}", "--write_metadata_json",
                    "--metadata_json_fields=dimensions"])
  assert r.returncode == 0, r.stderr
  record = load_records(root)["a.jpg"]
  # the sentinel values survive: dimensions were already present, so this run never re-decoded
  assert (record["width"], record["height"], record["mime_type"]) == (999, 999, "sentinel")


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
