"""Round-trip safety check over a copy of real sidecars.

The sample lives outside the repo. Point XMP_SAMPLE_DIR at a directory of
copied *.xmp files to run it; otherwise the tests are skipped.
"""

import os

import pytest

from photoapp import xmp

SAMPLE = os.environ.get("XMP_SAMPLE_DIR")

pytestmark = pytest.mark.skipif(
    not SAMPLE or not os.path.isdir(SAMPLE), reason="XMP_SAMPLE_DIR not set")


def sample_files():
  return sorted(os.path.join(SAMPLE, n) for n in os.listdir(SAMPLE))


def test_noop_edit_is_byte_identical_for_every_sample():
  for path in sample_files():
    data = open(path, "rb").read()
    assert xmp.edit_bytes(data) == data, path
    rating = xmp.parse(data).rating
    if rating is not None:
      assert xmp.edit_bytes(data, rating=rating) == data, path


def test_edits_verify_and_are_reversible_for_every_sample():
  for path in sample_files():
    data = open(path, "rb").read()
    for kw in (dict(rating=5), dict(rating=-1), dict(add_tags=["fav"]),
               dict(rating=3, add_tags=["fav", "x&y"])):
      xmp.parse(xmp.edit_bytes(data, **kw))       # raises if verification fails
    added = xmp.edit_bytes(data, add_tags=["zz-test"])
    assert xmp.edit_bytes(added, remove_tags=["zz-test"]) == data, path
    rating = xmp.parse(data).rating
    if rating is not None:
      changed = xmp.edit_bytes(data, rating=rating % 5 + 1)
      assert xmp.edit_bytes(changed, rating=rating) == data, path
