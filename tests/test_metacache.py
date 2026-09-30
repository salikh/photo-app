import json
import os
import time

from PIL import Image

from photoapp import fileinfo
from photoapp import metacache
from photoapp import scan
from tests.conftest import make_jpeg
from tests.conftest import write_synthetic_pef


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


def test_lens_model_is_stored_and_written_to_the_cache(conn, settings):
  # ticket 156: the EXIF LensModel tag lands in files.lens_model and in <name>.json/index.json.
  path = os.path.join(settings.pictures_dir, "a.jpg")
  img = Image.new("RGB", (30, 20))
  exif = img.getexif()
  exif.get_ifd(0x8769)[0xA434] = "smc PENTAX-DA 35mm F2.4 AL"   # LensModel
  img.save(path, exif=exif)
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  assert files(conn)["a.jpg"]["lens_model"] == "smc PENTAX-DA 35mm F2.4 AL"
  assert read_index(settings.pictures_dir)["files"]["a.jpg"]["lens_model"] == \
      "smc PENTAX-DA 35mm F2.4 AL"


def test_record_missing_lens_model_is_backfilled(conn, settings, monkeypatch):
  # ticket 156: a record written before lens_model existed is re-read once for just that field.
  make_jpeg(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir, metadata_cache=True)
  d = settings.pictures_dir
  index = read_index(d)
  del index["files"]["a.jpg"]["lens_model"]
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
  assert calls == [os.path.join(d, "a.jpg")]
  assert p.files_processed == 1
  assert "lens_model" in read_index(d)["files"]["a.jpg"]


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


def test_record_is_stale_only_for_generic_raw_with_no_exif():
  # ticket 162 unit: the "Pillow could not read this RAW" signature, and that ordinary records are
  # not stale.
  assert metacache.record_is_stale(
      {"mime_type": "image/x-raw", "exif_date": None, "camera_make": None})
  assert not metacache.record_is_stale(
      {"mime_type": "image/x-raw", "exif_date": "2020-01-01 00:00:00", "camera_make": None})
  assert not metacache.record_is_stale(
      {"mime_type": "image/jpeg", "exif_date": None, "camera_make": None})
  assert not metacache.record_is_stale({})


def test_stale_raw_record_is_reprocessed_and_reextracted(conn, settings, monkeypatch):
  # ticket 162: a PEF cached before the ticket 161 fallback (every key present, all None, generic
  # raw mime) is stale, so an otherwise-unchanged directory is reprocessed and that one record is
  # re-read with the fallback.
  d = settings.pictures_dir
  write_synthetic_pef(os.path.join(d, "a.PEF"))
  scan.scan(conn, d, metadata_cache=True)
  assert files(conn)["a.PEF"]["camera_make"] == "PENTAX Corporation"

  index = read_index(d)
  rec = index["files"]["a.PEF"]
  rec["mime_type"] = "image/x-raw"     # how a real Pillow-unreadable RAW is cached
  for k in ("exif_date", "aperture", "shutter_speed", "iso", "focal_length",
            "camera_make", "camera_model", "lens_model"):
    rec[k] = None
  with open(os.path.join(d, "index.json"), "w") as f:
    json.dump(index, f)
  # Make the directory look unchanged, so only the stale record can trigger reprocessing.
  os.utime(d, (12345, 12345))
  conn.execute("UPDATE dir_mtimes SET mtime = 12345 WHERE dirpath = '.'")
  conn.commit()

  calls = []
  real = fileinfo.read_image_metadata
  monkeypatch.setattr(fileinfo, "read_image_metadata",
                      lambda p_: calls.append(p_) or real(p_))
  p = scan.scan(conn, d, metadata_cache=True)
  assert calls == [os.path.join(d, "a.PEF")]        # only the stale record was re-read
  assert p.files_processed == 1
  assert files(conn)["a.PEF"]["camera_make"] == "PENTAX Corporation"
  assert read_index(d)["files"]["a.PEF"]["camera_make"] == "PENTAX Corporation"


def test_record_lacks_lens_signature():
  # ticket 167: a RAW record whose extraction worked but has no lens (and not the pre-ticket-161
  # all-None case, which record_is_stale handles).
  assert metacache.record_lacks_lens(
      {"lens_model": None, "camera_make": "PENTAX", "mime_type": "image/x-adobe-dng"})
  assert metacache.record_lacks_lens(
      {"lens_model": None, "camera_make": "PENTAX", "mime_type": "image/x-raw"})
  assert not metacache.record_lacks_lens(
      {"lens_model": "smc PENTAX-DA 35mm F2.4 AL", "camera_make": "PENTAX",
       "mime_type": "image/x-adobe-dng"})
  assert not metacache.record_lacks_lens(
      {"lens_model": None, "camera_make": "PENTAX", "mime_type": "image/jpeg"})
  assert not metacache.record_lacks_lens({})


def test_lensless_raw_record_is_patched_by_a_cheap_makernote_probe(conn, settings, monkeypatch):
  # ticket 167: a DNG cache record with lens_model None is filled from read_pentax_lens alone; a
  # full read_image_metadata would mean re-opening every RAW through LibRaw (see ticket 163).
  d = settings.pictures_dir
  Image.new("RGB", (30, 20)).save(os.path.join(d, "a.DNG"), format="JPEG")  # content irrelevant
  raw_md = ("image/x-adobe-dng", 100, 50, "2020-01-01 00:00:00", 2.8, 0.01, 100, 35.0,
            "PENTAX", "PENTAX K-5", None, 52)
  monkeypatch.setattr(fileinfo, "read_image_metadata", lambda p_: raw_md)
  scan.scan(conn, d, metadata_cache=True)
  rec = read_index(d)["files"]["a.DNG"]
  assert rec["mime_type"] == "image/x-adobe-dng" and rec["lens_model"] is None
  assert files(conn)["a.DNG"]["lens_model"] is None

  def no_full_read(p_):
    raise AssertionError("full metadata re-read")
  monkeypatch.setattr(fileinfo, "read_image_metadata", no_full_read)
  probes = []
  monkeypatch.setattr(fileinfo, "read_pentax_lens",
                      lambda p_: probes.append(p_) or "smc PENTAX-DA 35mm F2.4 AL")
  scan.scan(conn, d, metadata_cache=True)
  assert probes == [os.path.join(d, "a.DNG")]      # only the cheap MakerNote probe ran
  assert files(conn)["a.DNG"]["lens_model"] == "smc PENTAX-DA 35mm F2.4 AL"
  assert read_index(d)["files"]["a.DNG"]["lens_model"] == "smc PENTAX-DA 35mm F2.4 AL"


def test_record_lacks_focal_length_35mm_signature():
  # ticket 171: only a *missing* key counts, never a present None (a camera with no 0xA405 tag is
  # complete once the key exists and must not be probed on every later scan).
  assert metacache.record_lacks_focal_length_35mm({})
  assert metacache.record_lacks_focal_length_35mm({"focal_length": 35.0})
  assert not metacache.record_lacks_focal_length_35mm({"focal_length_35mm": None})
  assert not metacache.record_lacks_focal_length_35mm({"focal_length_35mm": 52})


def test_missing_focal_length_35mm_key_is_patched_cheaply(conn, settings, monkeypatch):
  # ticket 171: a record from before ticket 170 is filled from the EXIF-only reader; a full
  # read_image_metadata (which for a RAW re-opens it through LibRaw) must never run.
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  scan.scan(conn, d, metadata_cache=True)
  index = read_index(d)
  del index["files"]["a.jpg"]["focal_length_35mm"]   # as written before ticket 170
  with open(os.path.join(d, "index.json"), "w") as f:
    json.dump(index, f)
  conn.execute("DELETE FROM dir_mtimes")
  conn.commit()

  def no_full_read(p_):
    raise AssertionError("full metadata re-read")
  monkeypatch.setattr(fileinfo, "read_image_metadata", no_full_read)
  probes = []
  monkeypatch.setattr(fileinfo, "read_focal_length_35mm", lambda p_: probes.append(p_) or 52)
  scan.scan(conn, d, metadata_cache=True)
  assert probes == [os.path.join(d, "a.jpg")]      # only the cheap EXIF probe ran
  assert files(conn)["a.jpg"]["focal_length_35mm"] == 52
  assert read_index(d)["files"]["a.jpg"]["focal_length_35mm"] == 52
