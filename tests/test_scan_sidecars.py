import os
import time

from photoapp import scan
from tests.conftest import make_jpeg

XMP = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/" xmp:Rating="%d">%s
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""
FAV = "<dc:subject><rdf:Bag><rdf:li>fav</rdf:li></rdf:Bag></dc:subject>"


def write(path, text, mtime=None):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write(text)
  if mtime:
    os.utime(path, (mtime, mtime))


def sidecars(conn):
  return {r["path"]: r for r in conn.execute("SELECT * FROM xmp_sidecars")}


def file_id(conn, path):
  return conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def test_both_naming_styles_are_recorded_with_owners(conn, settings):
  d = os.path.join(settings.pictures_dir, "2018")
  make_jpeg(os.path.join(d, "K1.JPG"))
  write(os.path.join(d, "K1.JPG.xmp"), XMP % (1, ""))
  with open(os.path.join(d, "K2.DNG"), "wb") as f:
    f.write(b"raw")
  write(os.path.join(d, "K2.XMP"), XMP % (-1, FAV))       # RAW style, upper case
  write(os.path.join(d, "K3.DNG.xmp"), XMP % (4, ""))     # no such DNG: orphan
  scan.scan(conn, settings.pictures_dir)
  s = sidecars(conn)
  assert set(s) == {"2018/K1.JPG.xmp", "2018/K2.XMP", "2018/K3.DNG.xmp"}
  assert s["2018/K1.JPG.xmp"]["rating"] == 1
  assert s["2018/K1.JPG.xmp"]["file_id"] == file_id(conn, "2018/K1.JPG")
  assert (s["2018/K2.XMP"]["rating"], s["2018/K2.XMP"]["has_fav"]) == (-1, 1)
  assert s["2018/K2.XMP"]["file_id"] == file_id(conn, "2018/K2.DNG")
  assert s["2018/K3.DNG.xmp"]["file_id"] is None
  assert len(s["2018/K1.JPG.xmp"]["hash"]) == 56


def test_bare_sidecar_belongs_to_raw_when_pair_exists(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  with open(os.path.join(d, "a.dng"), "wb") as f:
    f.write(b"raw")
  write(os.path.join(d, "a.xmp"), XMP % (2, ""))
  scan.scan(conn, d)
  assert sidecars(conn)["a.xmp"]["file_id"] == file_id(conn, "a.dng")


def test_in_place_sidecar_edit_seen_in_skipped_directory(conn, settings):
  d = os.path.join(settings.pictures_dir, "y")
  make_jpeg(os.path.join(d, "a.jpg"))
  sc = os.path.join(d, "a.jpg.xmp")
  write(sc, XMP % (1, ""), mtime=1_000_000)
  scan.scan(conn, settings.pictures_dir)
  dir_mtime = os.stat(d).st_mtime
  write(sc, XMP % (5, FAV), mtime=2_000_000)  # in place: dir mtime unchanged
  os.utime(d, (dir_mtime, dir_mtime))
  p = scan.scan(conn, settings.pictures_dir)
  assert p.dirs_skipped >= 1 and p.sidecars_processed == 1
  row = sidecars(conn)["y/a.jpg.xmp"]
  assert (row["rating"], row["has_fav"]) == (5, 1)


def test_unchanged_sidecar_not_reparsed_and_removed_one_forgotten(conn, settings):
  d = settings.pictures_dir
  make_jpeg(os.path.join(d, "a.jpg"))
  write(os.path.join(d, "a.jpg.xmp"), XMP % (3, ""))
  scan.scan(conn, d)
  assert scan.scan(conn, d).sidecars_processed == 0
  os.remove(os.path.join(d, "a.jpg.xmp"))
  scan.scan(conn, d)
  assert sidecars(conn) == {}


def test_sidecar_gets_owner_when_image_appears_later(conn, settings):
  d = settings.pictures_dir
  write(os.path.join(d, "a.jpg.xmp"), XMP % (3, ""))
  scan.scan(conn, d)
  assert sidecars(conn)["a.jpg.xmp"]["file_id"] is None
  make_jpeg(os.path.join(d, "a.jpg"))
  os.utime(d, (time.time() + 5, time.time() + 5))
  scan.scan(conn, d)
  assert sidecars(conn)["a.jpg.xmp"]["file_id"] == file_id(conn, "a.jpg")
