"""XMP sidecar discovery and reading (lossless writing lives further down).

Sidecars in this library come in two naming styles, in any letter case:
  name.xmp        (RAW style, e.g. K___9911.XMP)
  name.ext.xmp    (full filename style, e.g. K___1596.JPG.xmp, K___2177.DNG.xmp)
"""

import dataclasses
import os
import re

from lxml import etree

from photoapp import fileinfo

NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_XMP = "http://ns.adobe.com/xap/1.0/"  # also spelled "xap" by older tools
NS_DC = "http://purl.org/dc/elements/1.1/"

FAV_TAG = "fav"

_RATING_RE = re.compile(rb'[Rr]ating[=>]\s*["\']?(-?\d+)')


def is_sidecar(name):
  return name.lower().endswith(".xmp")


def preferred_sidecar_name(filename):
  """Name a new sidecar for filename would get (never used for existing)."""
  if fileinfo.is_raw(filename):
    return os.path.splitext(filename)[0] + ".xmp"
  return filename + ".xmp"


def find_sidecars(filename, dir_listing):
  """Return the on-disk sidecar names in dir_listing that apply to filename.

  Both naming styles are matched case-insensitively. The style matching
  the file's convention (RAW: name.xmp, else name.ext.xmp) comes first;
  the rest are additional sources. Ties between case variants are broken
  by name, so the result is deterministic.
  """
  by_lower = {}
  for n in dir_listing:
    if is_sidecar(n):
      by_lower.setdefault(n.lower(), []).append(n)
  stem_style = os.path.splitext(filename)[0] + ".xmp"
  full_style = filename + ".xmp"
  order = ([stem_style, full_style] if fileinfo.is_raw(filename)
           else [full_style, stem_style])
  result = []
  for style in order:
    for n in sorted(by_lower.get(style.lower(), [])):
      if n not in result:
        result.append(n)
  return result


@dataclasses.dataclass
class Sidecar:
  """Values parsed from one sidecar. rating is None when absent."""
  rating: int = None
  tags: tuple = ()      # dc:subject, without the reserved 'fav'
  fav: bool = False
  parse_error: bool = False


def _rating_from_tree(root):
  for desc in root.iter(f"{{{NS_RDF}}}Description"):
    value = desc.attrib.get(f"{{{NS_XMP}}}Rating")
    if value is None:
      child = desc.find(f"{{{NS_XMP}}}Rating")
      value = child.text if child is not None else None
    if value is not None:
      try:
        return int(value.strip())
      except ValueError:
        return None
  return None


def _subjects_from_tree(root):
  result = []
  for li in root.iter(f"{{{NS_DC}}}subject"):
    result.extend(x.text.strip() for x in li.iter(f"{{{NS_RDF}}}li")
                  if x.text and x.text.strip())
  return result


def parse(data):
  """Parse sidecar bytes. Malformed XML falls back to a regex rating scan."""
  try:
    root = etree.fromstring(
        data, etree.XMLParser(resolve_entities=False, no_network=True))
  except etree.XMLSyntaxError:
    m = _RATING_RE.search(data)
    return Sidecar(rating=int(m.group(1)) if m else None, parse_error=True)
  subjects = _subjects_from_tree(root)
  return Sidecar(
      rating=_rating_from_tree(root),
      tags=tuple(t for t in subjects if t != FAV_TAG),
      fav=FAV_TAG in subjects)


def read(path):
  """Parse the sidecar at path. Returns None if it cannot be read."""
  try:
    with open(path, "rb") as f:
      return parse(f.read())
  except OSError:
    return None
