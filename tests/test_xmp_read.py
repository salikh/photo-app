from photoapp import xmp

DARKTABLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmp:Rating="3">
   <dc:subject><rdf:Bag><rdf:li>fav</rdf:li><rdf:li>trip</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

OLD_ELEMENT = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:xap="http://ns.adobe.com/xap/1.0/">
   <xap:Rating>-1</xap:Rating>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""


def test_parse_attribute_rating_tags_and_fav():
  s = xmp.parse(DARKTABLE)
  assert s.rating == 3
  assert s.fav is True
  assert s.tags == ("trip",)


def test_parse_element_rating_older_tools():
  s = xmp.parse(OLD_ELEMENT)
  assert s.rating == -1
  assert s.tags == () and not s.fav


def test_parse_no_rating():
  s = xmp.parse(b'<x:xmpmeta xmlns:x="adobe:ns:meta/"/>')
  assert s.rating is None


def test_parse_malformed_falls_back_to_regex():
  s = xmp.parse(b'<broken xmp:Rating="4" ')
  assert s.rating == 4 and s.parse_error


def test_find_sidecars_full_filename_style_any_case():
  listing = ["K___1.JPG", "K___1.JPG.xmp", "K___2.DNG", "K___2.XMP", "x.txt"]
  assert xmp.find_sidecars("K___1.JPG", listing) == ["K___1.JPG.xmp"]
  assert xmp.find_sidecars("K___2.DNG", listing) == ["K___2.XMP"]
  assert xmp.find_sidecars("k___1.jpg", listing) == ["K___1.JPG.xmp"]
  assert xmp.find_sidecars("nope.jpg", listing) == []


def test_find_sidecars_prefers_files_own_convention_and_reads_others():
  listing = ["a.dng", "a.jpg", "a.xmp", "a.jpg.xmp", "a.dng.xmp"]
  assert xmp.find_sidecars("a.dng", listing) == ["a.xmp", "a.dng.xmp"]
  assert xmp.find_sidecars("a.jpg", listing) == ["a.jpg.xmp", "a.xmp"]


def test_preferred_sidecar_name_for_new_sidecars():
  assert xmp.preferred_sidecar_name("a.dng") == "a.dng.xmp"
  assert xmp.preferred_sidecar_name("a.dng", raw_style="stem") == "a.xmp"
  assert xmp.preferred_sidecar_name("a.jpg") == "a.jpg.xmp"
  assert xmp.preferred_sidecar_name("a.b.JPG") == "a.b.JPG.xmp"


def test_read_missing_file_is_none(tmp_path):
  assert xmp.read(str(tmp_path / "nope.xmp")) is None
