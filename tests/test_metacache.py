import json
import os
import time

from photoapp import fileinfo
from photoapp import metacache
from photoapp import scan
from tests.conftest import make_jpeg


def files(conn):
  return {r["path"]: r for r in conn.execute("SELECT * FROM files")}


def read_index(dirpath):
  with open(os.path.join(dirpath, "index.json")) as f:
    return json.load(f)


def test_write_index_fixup_records_directory_final_mtime(settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  mtime = os.stat(settings.pictures_dir).st_mtime
  final = metacache.write_index(settings.pictures_dir, mtime, {"a.jpg": {"mtime": 1}})
  # Whether or not writing changed the mtime, the returned and stored values agree.
  assert final == os.stat(settings.pictures_dir).st_mtime
  assert read_index(settings.pictures_dir)["mtime"] == final


def test_scan_writes_index_and_per_file_json(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  d = os.path.join(settings.pictures_dir, "2020")
  assert os.path.isfile(os.path.join(d, "a.jpg.json"))
  index = read_index(d)
  assert set(index["files"]) == {"a.jpg"}
  record = index["files"]["a.jpg"]
  assert metacache.has_all_keys(record)
  assert record["bytesize"] > 0 and record["mtime"] == os.stat(
      os.path.join(d, "a.jpg")).st_mtime
  # ...and the cache files themselves are never imported as photos.
  p = scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  assert set(files(conn)) == {"2020/a.jpg"}
  assert p.files_seen == 1


def test_stray_json_files_are_ignored(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  with open(os.path.join(settings.pictures_dir, "index.json"), "w") as f:
    f.write("{}")
  with open(os.path.join(settings.pictures_dir, "a.jpg.json"), "w") as f:
    f.write("{}")
  p = scan.scan(conn, settings.pictures_dir)
  assert set(files(conn)) == {"a.jpg"} and p.files_seen == 1


def test_cache_disabled_writes_no_json(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  assert not os.path.exists(os.path.join(settings.pictures_dir, "index.json"))


def test_rebuilt_database_reuses_the_cache_without_reading(conn, settings, monkeypatch):
  from photoapp import db
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "b.jpg"), color="blue")
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)

  def snapshot(c):
    return {r["path"]: tuple(r[k] for k in metacache.RECORD_KEYS + ("missing",))
            for r in c.execute("SELECT * FROM files")}

  before = snapshot(conn)

  # A brand-new database (a rebuild) with the JSON cache still on disk.
  fresh = db.connect(str(settings.state_dir + "2/app.sqlite"))
  calls = []
  real = fileinfo.read_image_metadata
  monkeypatch.setattr(fileinfo, "read_image_metadata",
                      lambda path: calls.append(path) or real(path))

  p = scan.scan(fresh, settings.pictures_dir, metadata_cache=True)
  assert calls == []
  assert p.files_processed == 0 and p.error is None
  assert snapshot(fresh) == before
  fresh.close()


def test_changed_file_is_reprocessed_despite_the_cache(conn, settings, monkeypatch):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_jpeg(path, size=(60, 40), color="green")
  os.utime(path, (time.time() + 5, time.time() + 5))
  os.utime(settings.pictures_dir, (time.time() + 5, time.time() + 5))
  calls = []
  real = fileinfo.read_image_metadata
  monkeypatch.setattr(fileinfo, "read_image_metadata",
                      lambda p_: calls.append(p_) or real(p_))
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  assert calls == [path]
  assert files(conn)["a.jpg"]["width"] == 60


def test_record_missing_a_new_key_is_backfilled_by_rereading_only_it(conn, settings,
                                                                     monkeypatch):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  make_jpeg(os.path.join(settings.pictures_dir, "b.jpg"), color="blue")
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  d = settings.pictures_dir
  index = read_index(d)
  del index["files"]["a.jpg"]["focal_length"]     # a key added after this cache was written
  with open(os.path.join(d, "index.json"), "w") as f:
    json.dump(index, f)
  os.utime(d, (time.time() + 5, time.time() + 5))
  conn.execute("DELETE FROM dir_mtimes")
  conn.commit()
  calls = []
  real = fileinfo.read_image_metadata
  monkeypatch.setattr(fileinfo, "read_image_metadata",
                      lambda p_: calls.append(p_) or real(p_))
  p = scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  assert calls == [os.path.join(d, "a.jpg")]       # only the incomplete record is re-read
  assert p.files_processed == 1
  assert metacache.has_all_keys(read_index(d)["files"]["a.jpg"])


def test_scan_with_cache_on_over_a_database_scanned_with_it_off(conn, settings):
  # ticket 126: rows already exist but no index.json yet (the state after the ticket 111 default
  # flipped on), so the cache path must reuse the DB hash instead of raising "no item with that key".
  make_jpeg(os.path.join(settings.pictures_dir, "2020", "a.jpg"))
  scan.scan(conn, settings.pictures_dir)           # cache off: DB rows written, no index.json
  assert not os.path.exists(os.path.join(settings.pictures_dir, "2020", "index.json"))
  before = files(conn)["2020/a.jpg"]["hash"]

  p = scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  assert p.error is None
  assert files(conn)["2020/a.jpg"]["hash"] == before
  assert os.path.isfile(os.path.join(settings.pictures_dir, "2020", "index.json"))


def test_second_cache_scan_of_untouched_directory_is_a_noop(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  p = scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  assert p.files_processed == 0 and p.dirs_skipped == p.dirs_seen


def test_stale_per_file_json_is_removed(conn, settings):
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  make_jpeg(os.path.join(settings.pictures_dir, "b.jpg"), color="blue")
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  b = os.path.join(settings.pictures_dir, "b.jpg")
  assert os.path.isfile(b + ".json")
  os.remove(b)
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  assert not os.path.exists(b + ".json")
  assert set(files(conn)) == {"a.jpg", "b.jpg"}
  assert files(conn)["b.jpg"]["missing"] == 1
