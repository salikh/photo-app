import os
import stat

import pytest

from photoapp import xmp

DARKTABLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:darktable="http://darktable.sf.net/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
   exif:DateTimeOriginal="2018:10:08 09:05:30"
   xmp:Rating="1"
   darktable:history_end="0">
   <darktable:history>
    <rdf:Seq/>
   </darktable:history>
   <dc:creator>
    <rdf:Seq>
     <rdf:li>Salikh Zakirov</rdf:li>
    </rdf:Seq>
   </dc:creator>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""

WITH_SUBJECT = DARKTABLE.replace(
    b"   <dc:creator>",
    b"   <dc:subject>\n    <rdf:Bag>\n     <rdf:li>trip</rdf:li>\n"
    b"     <rdf:li>fav</rdf:li>\n    </rdf:Bag>\n   </dc:subject>\n"
    b"   <dc:creator>")

LIGHTROOM = b"""<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.6-c015">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   xmp:Rating="4"
   crs:Exposure2012="+0.35"
   crs:ToneCurvePV2012="x">
   <crs:ToneCurvePV2012>
    <rdf:Seq><rdf:li>0, 0</rdf:li><rdf:li>255, 255</rdf:li></rdf:Seq>
   </crs:ToneCurvePV2012>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""

ELEMENT_FORM = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xap="http://ns.adobe.com/xap/1.0/">
   <xap:Rating>-1</xap:Rating>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

BARE = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""/>
 </rdf:RDF>
</x:xmpmeta>"""


def diff_bytes(a, b):
  """(removed, added) after stripping the common prefix and suffix."""
  i = 0
  while i < min(len(a), len(b)) and a[i] == b[i]:
    i += 1
  j = 0
  while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
    j += 1
  return a[i:len(a) - j], b[i:len(b) - j]


@pytest.mark.parametrize("data", [DARKTABLE, WITH_SUBJECT, LIGHTROOM,
                                  ELEMENT_FORM, BARE])
def test_noop_edits_are_byte_identical(data):
  assert xmp.edit_bytes(data) == data
  current = xmp.parse(data).rating
  if current is not None:
    assert xmp.edit_bytes(data, rating=current) == data


def test_rating_attribute_changes_only_the_digit():
  out = xmp.edit_bytes(DARKTABLE, rating=5)
  assert diff_bytes(DARKTABLE, out) == (b"1", b"5")
  assert xmp.parse(out).rating == 5
  assert xmp.parse(xmp.edit_bytes(DARKTABLE, rating=-1)).rating == -1


def test_lightroom_crs_data_and_xpacket_survive():
  out = xmp.edit_bytes(LIGHTROOM, rating=2)
  assert diff_bytes(LIGHTROOM, out) == (b"4", b"2")
  assert b"crs:ToneCurvePV2012" in out and out.endswith(b'<?xpacket end="w"?>')


def test_rating_element_form_is_preserved():
  out = xmp.edit_bytes(ELEMENT_FORM, rating=3)
  assert b"<xap:Rating>3</xap:Rating>" in out
  assert diff_bytes(ELEMENT_FORM, out) == (b"-1", b"3")


def test_rating_added_when_absent_and_zero_is_noop():
  out = xmp.edit_bytes(BARE, rating=2)
  assert xmp.parse(out).rating == 2
  assert xmp.edit_bytes(BARE, rating=0) == BARE
  no_ns = xmp.edit_bytes(BARE, rating=3)
  assert b'xmlns:xmp="http://ns.adobe.com/xap/1.0/"' in no_ns
  # a file that declares the namespace reuses its prefix
  out = xmp.edit_bytes(DARKTABLE.replace(b'   xmp:Rating="1"\n', b""), rating=4)
  assert b'xmp:Rating="4"' in out and out.count(b"xmlns:xmp") == 1


def test_rating_out_of_range_rejected():
  with pytest.raises(xmp.XmpEditError):
    xmp.edit_bytes(DARKTABLE, rating=6)


def test_add_tags_creates_subject_element_with_fav_reserved_word():
  out = xmp.edit_bytes(DARKTABLE, add_tags=["fav", "trip"])
  s = xmp.parse(out)
  assert s.fav and s.tags == ("trip",)
  assert out.count(b"xmlns:dc") == 1     # reused the declared prefix


def test_add_and_remove_tags_in_existing_bag_keeps_layout():
  out = xmp.edit_bytes(WITH_SUBJECT, add_tags=["family"], remove_tags=["trip"])
  s = xmp.parse(out)
  assert s.tags == ("family",) and s.fav
  assert b"     <rdf:li>family</rdf:li>\n    </rdf:Bag>" in out
  assert b"trip" not in out


def test_removing_last_tag_removes_the_subject_element():
  out = xmp.edit_bytes(WITH_SUBJECT, remove_tags=["trip", "fav"])
  assert b"dc:subject" not in out
  assert out == DARKTABLE            # exactly the file we started from


def test_toggle_fav_off_and_on_round_trips():
  off = xmp.edit_bytes(WITH_SUBJECT, remove_tags=["fav"])
  assert not xmp.parse(off).fav and xmp.parse(off).tags == ("trip",)
  on = xmp.edit_bytes(off, add_tags=["fav"])
  assert xmp.parse(on).fav


def test_empty_bag_forms_and_special_characters():
  empty = DARKTABLE.replace(b"   <dc:creator>",
      b"   <dc:subject><rdf:Bag/></dc:subject>\n   <dc:creator>")
  out = xmp.edit_bytes(empty, add_tags=["a & b <c>", "\u65e5\u672c"])
  assert xmp.parse(out).tags == ("a & b <c>", "日本")
  empty2 = DARKTABLE.replace(b"   <dc:creator>",
      b"   <dc:subject><rdf:Bag></rdf:Bag></dc:subject>\n   <dc:creator>")
  assert xmp.parse(xmp.edit_bytes(empty2, add_tags=["x"])).tags == ("x",)


def test_tags_added_to_self_closing_description():
  out = xmp.edit_bytes(BARE, add_tags=["x"], rating=1)
  s = xmp.parse(out)
  assert s.tags == ("x",) and s.rating == 1


def test_malformed_and_utf16_are_refused():
  with pytest.raises(xmp.XmpEditError):
    xmp.edit_bytes(b"<broken xmp:Rating='1'", rating=2)
  with pytest.raises(xmp.XmpEditError):
    xmp.edit_bytes(DARKTABLE.decode().encode("utf-16"), rating=2)


def test_new_sidecar_is_valid_and_minimal():
  data = xmp.new_sidecar_bytes(rating=3, tags=["fav", "x&y"])
  s = xmp.parse(data)
  assert s.rating == 3 and s.fav and s.tags == ("x&y",)
  assert b"crs:" not in data
  assert xmp.parse(xmp.new_sidecar_bytes()).rating is None


# --- file level -------------------------------------------------------------

def test_update_file_atomic_backup_once_and_permissions(tmp_path):
  p = tmp_path / "a.jpg.xmp"
  p.write_bytes(DARKTABLE)
  os.chmod(p, 0o640)
  backup = str(tmp_path / "backups" / "a.jpg.xmp.1")
  r = xmp.update_file(str(p), backup_path=backup, rating=4)
  assert r.changed and r.backup_path == backup
  assert open(backup, "rb").read() == DARKTABLE
  assert xmp.parse(p.read_bytes()).rating == 4
  assert stat.S_IMODE(os.stat(p).st_mode) == 0o640
  # a second edit does not overwrite the first-seen backup
  r = xmp.update_file(str(p), backup_path=backup, rating=2)
  assert r.changed and r.backup_path is None
  assert open(backup, "rb").read() == DARKTABLE
  assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []


def test_update_file_noop_and_dry_run_do_not_write(tmp_path):
  p = tmp_path / "a.xmp"
  p.write_bytes(DARKTABLE)
  mtime = os.stat(p).st_mtime_ns
  assert not xmp.update_file(str(p), rating=1).changed
  r = xmp.update_file(str(p), rating=5, dry_run=True)
  assert r.changed and p.read_bytes() == DARKTABLE
  assert os.stat(p).st_mtime_ns == mtime


def test_update_file_reapplies_edit_when_file_changed_underneath(tmp_path, monkeypatch):
  p = tmp_path / "a.xmp"
  p.write_bytes(DARKTABLE)
  original_edit = xmp.edit_bytes
  calls = []

  def racing_edit(data, **kw):
    calls.append(1)
    out = original_edit(data, **kw)
    if len(calls) == 1:   # another program adds a tag while we compute
      p.write_bytes(original_edit(DARKTABLE, add_tags=["theirs"]))
      os.utime(p, ns=(1, 2_000_000_000))
    return out

  monkeypatch.setattr(xmp, "edit_bytes", racing_edit)
  xmp.update_file(str(p), rating=5)
  s = xmp.parse(p.read_bytes())
  assert s.rating == 5 and s.tags == ("theirs",)     # their change survived
  assert len(calls) == 2


def test_create_file_never_overwrites(tmp_path):
  p = tmp_path / "new.xmp"
  xmp.create_file(str(p), rating=2, tags=["x"])
  assert xmp.parse(p.read_bytes()).rating == 2
  with pytest.raises(FileExistsError):
    xmp.create_file(str(p), rating=5)
  assert xmp.parse(p.read_bytes()).rating == 2
  assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []
