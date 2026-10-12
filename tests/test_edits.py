"""Epic 222: non-destructive edits for non-RAW stills (tickets 226-229)."""

import hashlib
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photoapp import api
from photoapp import db
from photoapp import edits
from photoapp import export
from photoapp import scan
from photoapp import thumbs


def make_gradient(path, size=(240, 160)):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  img = Image.new("RGB", size)
  img.putdata([(x * 255 // (size[0] - 1), 100, 40) for y in range(size[1]) for x in range(size[0])])
  img.save(path, quality=95)


def sha(path):
  with open(path, "rb") as f:
    return hashlib.sha256(f.read()).hexdigest()


def mean(img):
  px = list(img.convert("RGB").getdata())
  return tuple(sum(p[c] for p in px) / len(px) for c in range(3))


# ---- 226: the model -------------------------------------------------------------------------

def test_normalize_drops_identity_values_and_empty_becomes_none():
  assert edits.normalize(None) is None
  assert edits.normalize({}) is None
  assert edits.normalize({"brightness": 0, "contrast": 1.0, "saturation": 1.0,
                          "levels": {"black": 0, "gamma": 1.0, "white": 255},
                          "sharpen": {"amount": 0, "radius": 2}, "mono": False}) is None
  assert edits.normalize({"brightness": 10, "contrast": 1.0}) == {"brightness": 10.0}
  assert edits.normalize({"mono": True}) == {"mono": {"weights": edits.LUMA}}
  assert edits.normalize({"levels": {"black": 10}}) == {
      "levels": {"black": 10, "gamma": 1.0, "white": 255}}


@pytest.mark.parametrize("bad", [
    {"nope": 1}, {"brightness": 101}, {"contrast": 0.1}, {"saturation": 3},
    {"levels": {"black": 200, "white": 100}}, {"levels": {"gamma": 9}},
    {"levels": {"colour": 1}}, {"mono": {"weights": [1, 2]}}, {"mono": {"weights": [9, 0, 0]}},
    {"sharpen": {"amount": 500}}, {"sharpen": {"radius": 0.01, "amount": 10}},
    {"brightness": "high"}, {"brightness": True}, "text", {"levels": 5},
])
def test_normalize_rejects_bad_documents(bad):
  with pytest.raises(edits.EditsError):
    edits.normalize(bad)


def test_sharpen_radius_goes_up_to_40():
  assert edits.normalize({"sharpen": {"amount": 50, "radius": 40}})["sharpen"]["radius"] == 40.0
  with pytest.raises(edits.EditsError):
    edits.normalize({"sharpen": {"amount": 50, "radius": 40.5}})


def test_identity_lut_is_identity_and_levels_compose():
  assert list(edits.compile_lut(None)[0]) == list(range(256))
  r, g, b = edits.compile_lut(edits.normalize({"levels": {"black": 50, "white": 200}}))
  assert r[50] == 0 and r[200] == 255 and r[10] == 0 and r[250] == 255
  assert r == g == b
  r, g, b = edits.compile_lut(edits.normalize({"levels_r": {"black": 100}}))
  assert r[100] == 0 and g[100] == 100 and b[100] == 100          # per channel
  lut = edits.compile_lut(edits.normalize({"levels": {"gamma": 2.0}}))[0]
  assert lut[64] > 64                                              # gamma > 1 brightens midtones
  assert edits.compile_lut({"brightness": 10.0})[0][100] == 126
  assert edits.compile_lut({"contrast": 2.0})[0][128] == 128 and \
      edits.compile_lut({"contrast": 2.0})[0][160] > 160


def test_apply_edits_do_what_they_say():
  img = Image.new("RGB", (64, 64), (100, 100, 100))
  assert edits.apply(img, None) is img                              # unedited: same object
  assert mean(edits.apply(img, {"brightness": 20.0}))[0] > 140
  assert mean(edits.apply(img, edits.normalize({"levels_b": {"white": 50}})))[2] == 255
  grey = edits.apply(Image.new("RGB", (8, 8), (255, 0, 0)), edits.normalize({"mono": True}))
  assert len(set(grey.getpixel((0, 0)))) == 1                       # r == g == b
  assert mean(edits.apply(Image.new("RGB", (8, 8), (200, 50, 50)),
                          {"saturation": 0.0}))[0] == pytest.approx(mean(
                              edits.apply(Image.new("RGB", (8, 8), (200, 50, 50)),
                                          {"saturation": 0.0}))[1], abs=1)
  edge = Image.new("RGB", (3000, 20), (60, 60, 60))
  edge.paste((200, 200, 200), (1500, 0, 3000, 20))
  sharp = edits.apply(edge, edits.normalize({"sharpen": {"amount": 200, "radius": 6}}))
  assert sharp.getpixel((1502, 10))[0] > 200 and sharp.getpixel((1497, 10))[0] < 60  # overshoot


def test_set_and_get_roundtrip_and_thumb_rev(conn, settings):
  make_gradient(os.path.join(settings.pictures_dir, "a.jpg"))
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  rev = lambda: conn.execute("SELECT thumb_rev FROM files WHERE id = ?", (fid,)).fetchone()[0]
  before = rev()
  assert edits.get(conn, fid) is None
  assert edits.set(conn, fid, {"brightness": 5}) == {"brightness": 5.0}
  assert edits.get(conn, fid) == {"brightness": 5.0} and rev() == before + 1
  assert edits.set(conn, fid, {"brightness": 0}) is None and edits.get(conn, fid) is None
  with pytest.raises(edits.EditsError):
    edits.set(conn, 99999, {"brightness": 5})


def test_edits_survive_a_rescan(conn, settings):
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_gradient(path)
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  edits.set(conn, fid, {"contrast": 1.5})
  os.utime(path, (1, 1))
  scan.scan(conn, settings.pictures_dir)
  assert edits.get(conn, fid) == {"contrast": 1.5}


# ---- 227: the pipeline ----------------------------------------------------------------------

def test_edits_are_baked_into_every_size_and_the_original_is_untouched(conn, settings):
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_gradient(path, (2400, 1600))
  digest = sha(path)
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  plain = {s: thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", s)
           for s in thumbs.SIZES}
  plain_mean = {s: mean(Image.open(p)) for s, p in plain.items()}
  edits.set(conn, fid, {"brightness": -30})
  thumbs.clear(settings.thumbs_dir, conn, fid, "a.jpg")
  for size in thumbs.SIZES:
    out = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", size)
    assert mean(Image.open(out))[0] < plain_mean[size][0] - 40, size
  assert sha(path) == digest


def test_an_edited_size_is_never_derived_from_a_larger_cached_one(conn, settings):
  # Medium cached first with the edit; a Small must be rendered from the original, not from that
  # Medium (which would apply the edit twice).
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_gradient(path, (2400, 1600))
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  once = {"brightness": -20.0}
  edits.set(conn, fid, once)
  thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", "Medium")
  small = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", "Small")
  reference = edits.apply(Image.open(path).convert("RGB").resize((1000, 667)), once)
  assert mean(Image.open(small))[0] == pytest.approx(mean(reference)[0], abs=3)
  assert mean(Image.open(small))[0] > mean(Image.open(path))[0] - 70   # not -40 twice


def test_an_unedited_file_takes_the_old_path(conn, settings, monkeypatch):
  make_gradient(os.path.join(settings.pictures_dir, "a.jpg"), (2400, 1600))
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  monkeypatch.setattr(edits, "apply", lambda *a, **k: (_ for _ in ()).throw(
      AssertionError("edit stage must not run")) if a[1] else a[0])
  thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", "Huge")
  small = thumbs.ensure(conn, settings.pictures_dir, settings.thumbs_dir, fid, "a.jpg", "Small")
  assert Image.open(small).size == (1000, 667)


def test_sharpen_radius_scales_with_the_output_size():
  doc = {"sharpen": {"amount": 100.0, "radius": 3.0}}
  captured = []
  import PIL.ImageFilter as F
  real = F.UnsharpMask
  F.UnsharpMask = lambda radius, percent, threshold: captured.append(radius) or real(
      radius, percent, threshold)
  try:
    edits.apply(Image.new("RGB", (6000, 40)), doc)
    edits.apply(Image.new("RGB", (1000, 40)), doc)
  finally:
    F.UnsharpMask = real
  assert captured == [pytest.approx(3.0), pytest.approx(0.5)]


# ---- API, preview, export -------------------------------------------------------------------

def client(conn, settings):
  return TestClient(api.create_app(conn, settings))


def test_edits_api_roundtrip_clears_thumbs_and_serves_the_edited_image(conn, settings):
  path = os.path.join(settings.pictures_dir, "y", "a.jpg")
  make_gradient(path)
  digest = sha(path)
  scan.scan(conn, settings.pictures_dir)
  c = client(conn, settings)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  base = Image.open(__import__("io").BytesIO(c.get(f"/img/Small/{fid}").content))
  r = c.post(f"/api/files/{fid}/edits", json={"edits": {"brightness": -40}})
  assert r.status_code == 200 and r.json()["edits"] == {"brightness": -40.0}
  edited = Image.open(__import__("io").BytesIO(c.get(f"/img/Small/{fid}").content))
  assert mean(edited)[0] < mean(base)[0] - 50
  huge = Image.open(__import__("io").BytesIO(c.get(f"/img/Huge/{fid}").content))
  assert mean(huge)[0] < mean(base)[0] - 50       # Huge is no longer "the original"
  photo = c.get(f"/api/photos/{conn.execute('SELECT photo_id FROM files').fetchone()[0]}").json()
  f = photo["files"][0]
  assert f["edits"] == {"brightness": -40.0} and f["is_editable"] is True
  assert c.post(f"/api/files/{fid}/edits", json={"edits": {"brightness": 500}}).status_code == 400
  c.post(f"/api/files/{fid}/edits", json={"edits": None})
  again = Image.open(__import__("io").BytesIO(c.get(f"/img/Small/{fid}").content))
  assert mean(again)[0] == pytest.approx(mean(base)[0], abs=2)
  assert sha(path) == digest


def test_edits_api_refuses_videos_and_unknown_files(conn, settings):
  open(os.path.join(settings.pictures_dir, "x.mp4"), "wb").write(b"video")
  scan.scan(conn, settings.pictures_dir)
  c = client(conn, settings)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  assert c.post(f"/api/files/{fid}/edits", json={"edits": {"brightness": 5}}).status_code == 400
  assert c.post("/api/files/9999/edits", json={"edits": {"brightness": 5}}).status_code == 404


def test_edits_apply_to_a_raw_file_in_thumbnails_and_raw_preview(conn, settings, monkeypatch):
  # ticket 235: a RAW takes the same adjustments as a JPG, applied after the RAW conversion --
  # baked into every cached size, and into the provisional raw_preview.
  import io
  import json
  from photoapp import previews
  open(os.path.join(settings.pictures_dir, "x.DNG"), "wb").write(b"raw")
  scan.scan(conn, settings.pictures_dir)
  c = client(conn, settings)
  fid, photo_id = conn.execute("SELECT id, photo_id FROM files").fetchone()
  gradient = os.path.join(settings.state_dir, "g.jpg")
  make_gradient(gradient, (600, 400))
  monkeypatch.setattr(previews, "embedded_preview", lambda source: Image.open(gradient).convert("RGB"))
  monkeypatch.setattr(previews, "render",
                      lambda source, pending, half_size=False: Image.open(gradient).convert("RGB"))
  def small():
    return Image.open(io.BytesIO(c.get(f"/img/Small/{fid}").content))
  base = mean(small())
  r = c.post(f"/api/files/{fid}/edits", json={"edits": {"mono": True, "levels": {"black": 20, "gamma": 1, "white": 200}}})
  assert r.status_code == 200 and "mono" in r.json()["edits"]
  edited = mean(small())
  assert abs(edited[0] - edited[2]) < 2 and abs(base[0] - base[2]) > 20          # monochrome baked in
  f = c.get(f"/api/photos/{photo_id}").json()["files"][0]
  assert f["is_raw"] and f["is_editable"] and "mono" in f["edits"]
  preview = c.get(f"/api/files/{fid}/raw_preview",
                  params={"size": "Small", "edits": json.dumps({"mono": True})})
  px = mean(Image.open(io.BytesIO(preview.content)))
  assert abs(px[0] - px[2]) < 2
  plain = mean(Image.open(io.BytesIO(c.get(f"/api/files/{fid}/raw_preview",
                                          params={"size": "Small"}).content)))
  assert abs(plain[0] - plain[2]) > 20
  assert c.get(f"/api/files/{fid}/raw_preview", params={"edits": "{bad"}).status_code == 400


def test_edit_preview_is_provisional_and_never_persisted(conn, settings):
  import io
  import json
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_gradient(path, (2400, 1600))
  scan.scan(conn, settings.pictures_dir)
  c = client(conn, settings)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  q = json.dumps({"brightness": 40})
  r = c.get(f"/api/files/{fid}/edit_preview", params={"edits": q, "size": "Small"})
  assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
  img = Image.open(io.BytesIO(r.content))
  assert max(img.size) == 1000 and mean(img)[0] > 120
  assert edits.get(conn, fid) is None                                # nothing stored
  assert conn.execute("SELECT COUNT(*) FROM thumbs").fetchone()[0] == 0
  assert c.get(f"/api/files/{fid}/edit_preview", params={"edits": "{bad"}).status_code == 400
  assert c.get(f"/api/files/{fid}/edit_preview",
               params={"edits": json.dumps({"brightness": 999})}).status_code == 400


def test_export_of_an_edited_file_uses_the_edited_render(conn, settings):
  path = os.path.join(settings.pictures_dir, "a.jpg")
  make_gradient(path, (600, 400))
  digest = sha(path)
  scan.scan(conn, settings.pictures_dir)
  fid = conn.execute("SELECT id FROM files").fetchone()[0]
  dest = os.path.join(settings.state_dir, "out", "a.jpg")
  export.export_file(conn, settings, fid, "a.jpg", dest)
  plain = mean(Image.open(dest))
  edits.set(conn, fid, {"brightness": -40})
  thumbs.clear(settings.thumbs_dir, conn, fid, "a.jpg")
  export.export_file(conn, settings, fid, "a.jpg", dest)
  assert mean(Image.open(dest))[0] < plain[0] - 50
  assert sha(path) == digest
  # the cropped branch renders straight from the original and bakes the edits too
  from photoapp import crop
  crop.set(conn, fid, 0.0, 0.0, 0.5, 1.0)
  export.export_file(conn, settings, fid, "a.jpg", dest)
  assert Image.open(dest).size[0] == 300 and mean(Image.open(dest))[0] < plain[0] - 50
