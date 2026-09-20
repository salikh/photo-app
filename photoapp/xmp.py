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


# ---------------------------------------------------------------------------
# Lossless editing
#
# Edits are applied as small byte-level changes to the original document, so
# everything we do not touch (darktable/Lightroom data, unknown elements,
# comments, the xpacket wrapper, whitespace) stays byte-identical. Each edit
# is verified by re-parsing: the wanted values must be present and the rest
# of the document must be canonically unchanged; otherwise nothing is written.

import copy
import hashlib
import shutil
import tempfile
from xml.sax.saxutils import escape, unescape


class XmpEditError(Exception):
  pass


def _first_description(root):
  for desc in root.iter(f"{{{NS_RDF}}}Description"):
    return desc
  raise XmpEditError("no rdf:Description element")


def _prefixes(desc, ns):
  """Prefixes bound to ns in scope on desc, in a stable order."""
  return sorted(p for p, u in desc.nsmap.items() if u == ns and p)


def _load(data):
  if data.startswith((b"\xff\xfe", b"\xfe\xff")) or data[:4] in (
      b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00"):
    raise XmpEditError("UTF-16/32 sidecars are not supported")
  try:
    return etree.fromstring(
        data, etree.XMLParser(resolve_entities=False, no_network=True))
  except etree.XMLSyntaxError as e:
    raise XmpEditError(f"malformed XML: {e}")


def _description_tag(data, desc_prefix):
  """Regex match for the opening rdf:Description tag (first one)."""
  m = re.search(rb"<" + re.escape(desc_prefix.encode()) +
                rb":Description\b", data)
  if not m:
    raise XmpEditError("cannot locate rdf:Description in the text")
  return m


def set_rating_bytes(data, rating):
  """Return data with the xmp:Rating set to rating (-1..5)."""
  if not -1 <= rating <= 5:
    raise XmpEditError(f"rating out of range: {rating}")
  root = _load(data)
  desc = _first_description(root)
  # A file can have several rdf:Description blocks; edit the one that
  # already carries the Rating, and only add one to the first if none does.
  carrier = next(
      (d for d in root.iter(f"{{{NS_RDF}}}Description")
       if f"{{{NS_XMP}}}Rating" in d.attrib
       or d.find(f"{{{NS_XMP}}}Rating") is not None), None)
  xmp_prefixes = _prefixes(carrier if carrier is not None else desc, NS_XMP)
  for prefix in xmp_prefixes:  # attribute form, e.g. xmp:Rating="3"
    pat = re.compile(
        rb"(\b" + re.escape(prefix.encode()) + rb":Rating\s*=\s*)([\"'])"
        rb"(-?\d+)(\2)")
    if pat.search(data):
      return pat.sub(lambda m: m.group(1) + m.group(2) +
                     str(rating).encode() + m.group(4), data, count=1)
  for prefix in xmp_prefixes:  # child element form, e.g. <xap:Rating>3<...
    pat = re.compile(
        rb"(<" + re.escape(prefix.encode()) + rb":Rating>\s*)(-?\d+)(\s*</" +
        re.escape(prefix.encode()) + rb":Rating>)")
    if pat.search(data):
      return pat.sub(lambda m: m.group(1) + str(rating).encode() +
                     m.group(3), data, count=1)
  if rating == 0:
    return data  # absent already means unrated
  rdf_prefixes = _prefixes(desc, NS_RDF)
  m = _description_tag(data, rdf_prefixes[0] if rdf_prefixes else "rdf")
  if xmp_prefixes:
    attr = f' {xmp_prefixes[0]}:Rating="{rating}"'
  else:
    attr = f' xmlns:xmp="{NS_XMP}" xmp:Rating="{rating}"'
  return data[:m.end()] + attr.encode() + data[m.end():]


def set_subjects_bytes(data, add=(), remove=()):
  """Return data with dc:subject entries added and removed (order kept).

  Removed items disappear together with their leading whitespace; new items
  are appended after the last item, reusing its leading whitespace.
  """
  add, remove = list(dict.fromkeys(add)), set(remove)
  root = _load(data)
  desc = _first_description(root)
  subject_el = next(root.iter(f"{{{NS_DC}}}subject"), None)
  # The prefix may be declared on the dc:subject element itself (as our own
  # insertions do), so take it from there before looking at the Description.
  dc = (subject_el.prefix if subject_el is not None
        else (_prefixes(desc, NS_DC) or [None])[0])
  rdf = (_prefixes(desc, NS_RDF) or ["rdf"])[0]
  current = set(_subjects_from_tree(root))
  add = [t for t in add if t not in current and t not in remove]
  remove = remove & current
  if not add and not remove:
    return data

  el = None
  if dc:
    el = re.search(
        rb"(<" + re.escape(dc.encode()) + rb":subject\b[^>]*>)(.*?)(</" +
        re.escape(dc.encode()) + rb":subject>)", data, re.DOTALL)
  if el is None:
    return _insert_subject_element(data, rdf, dc, add) if add else data

  inner = el.group(2)
  r = re.escape(rdf.encode())
  li_pat = re.compile(rb"(\s*)(<" + r + rb":li\b[^>]*>(.*?)</" + r + rb":li>)",
                      re.DOTALL)
  items = list(li_pat.finditer(inner))
  survivors = [m for m in items
               if unescape(m.group(3).decode("utf-8")).strip() not in remove]
  if not survivors and not add:
    return _remove_element(data, el)

  new_li = lambda t: f"<{rdf}:li>{escape(t)}</{rdf}:li>".encode("utf-8")
  lead = items[-1].group(1) if items else b""
  out = inner
  # drop removed items back to front so offsets stay valid
  for m in reversed([m for m in items if m not in survivors]):
    out = out[:m.start()] + out[m.end():]
  if add:
    pos = None
    kept = list(li_pat.finditer(out))
    if kept:
      pos = kept[-1].end()
    else:  # empty container: <rdf:Bag/> or <rdf:Bag></rdf:Bag>
      cont = re.search(rb"<" + r + rb":(Bag|Seq|Alt)\b[^>]*?(/?)>", out)
      if cont is None:
        raise XmpEditError("unrecognized dc:subject structure")
      if cont.group(2):  # self-closing: open it up
        name = cont.group(1)
        opening = cont.group(0)[:-2].rstrip() + b">"
        closing = b"</" + rdf.encode() + b":" + name + b">"
        out = out[:cont.start()] + opening + closing + out[cont.end():]
        cont = re.search(rb"<" + r + rb":" + name + rb"\b[^>]*>", out)
      pos = cont.end()
    out = out[:pos] + b"".join(lead + new_li(t) for t in add) + out[pos:]
  return data[:el.start(2)] + out + data[el.end(2):]


def _remove_element(data, el):
  """Remove a regex-matched element, plus its line if it is alone on it."""
  start, end = el.start(), el.end()
  line_start = data.rfind(b"\n", 0, start) + 1
  if data[line_start:start].strip() == b"" and data[end:end + 1] == b"\n":
    start, end = line_start, end + 1
  return data[:start] + data[end:]


def _insert_subject_element(data, rdf, dc, add):
  lis = "".join(f"<{rdf}:li>{escape(t)}</{rdf}:li>" for t in add)
  decl = "" if dc else f' xmlns:dc="{NS_DC}"'
  el = (f"<{dc or 'dc'}:subject{decl}><{rdf}:Bag>{lis}</{rdf}:Bag>"
        f"</{dc or 'dc'}:subject>").encode("utf-8")
  close = re.search(rb"</" + re.escape(rdf.encode()) + rb":Description>", data)
  if close:
    line_start = data.rfind(b"\n", 0, close.start()) + 1
    indent = data[line_start:close.start()]
    if indent.strip() == b"":
      return data[:close.start()] + b" " + el + b"\n" + indent + data[close.start():]
    return data[:close.start()] + el + data[close.start():]
  # self-closing <rdf:Description .../>
  m = re.search(rb"<" + re.escape(rdf.encode()) + rb":Description\b[^>]*?/>", data)
  if not m:
    raise XmpEditError("cannot place dc:subject")
  opening = m.group(0)[:-2].rstrip() + b">"
  return (data[:m.start()] + opening + el + b"</" + rdf.encode() +
          b":Description>" + data[m.end():])


def _signature(el):
  """Structure of an element ignoring namespace declarations and whitespace."""
  if not isinstance(el.tag, str):  # comment / processing instruction
    return (str(el.tag), (el.text or "").strip(), (el.tail or "").strip())
  return (el.tag, tuple(sorted(el.attrib.items())), (el.text or "").strip(),
          (el.tail or "").strip(), tuple(_signature(c) for c in el))


def _canonical_without_edited(root):
  """Signature of the document minus the nodes an edit may touch."""
  root = copy.deepcopy(root)
  for desc in root.iter(f"{{{NS_RDF}}}Description"):
    desc.attrib.pop(f"{{{NS_XMP}}}Rating", None)
    for child in list(desc):
      if child.tag in (f"{{{NS_XMP}}}Rating", f"{{{NS_DC}}}subject"):
        if child.tail and child.tail.strip():
          raise XmpEditError("unexpected text after an edited node")
        prev = child.getprevious()
        if prev is not None:
          prev.tail = (prev.tail or "") + (child.tail or "")
        desc.remove(child)
  return _signature(root)


def edit_bytes(data, rating=None, add_tags=(), remove_tags=()):
  """Apply an edit and verify it. Raises XmpEditError instead of returning
  bytes that would lose or damage anything."""
  before = _load(data)
  expected = parse(data)
  out = data
  if rating is not None:
    out = set_rating_bytes(out, rating)
  if add_tags or remove_tags:
    out = set_subjects_bytes(out, add_tags, remove_tags)
  if out == data:
    return data
  after = _load(out)
  got = parse(out)
  want_rating = rating if rating is not None else expected.rating
  if rating == 0 and expected.rating is None:
    want_rating = None
  if got.rating != want_rating:
    raise XmpEditError(f"rating verification failed: {got.rating}")
  want_subjects = [t for t in (list(expected.tags) +
                               (["fav"] if expected.fav else []))
                   if t not in set(remove_tags)]
  want_subjects += [t for t in add_tags if t not in want_subjects and
                    t not in set(remove_tags)]
  got_subjects = list(got.tags) + (["fav"] if got.fav else [])
  if sorted(got_subjects) != sorted(want_subjects):
    raise XmpEditError(f"tag verification failed: {got_subjects}")
  if _canonical_without_edited(before) != _canonical_without_edited(after):
    raise XmpEditError("edit changed nodes it should not touch")
  return out


def new_sidecar_bytes(rating=0, tags=()):
  """A minimal valid XMP packet with only the fields being set."""
  attrs = f' xmp:Rating="{rating}"' if rating else ""
  subject = ""
  if tags:
    lis = "".join(f"<rdf:li>{escape(t)}</rdf:li>" for t in tags)
    subject = f"<dc:subject><rdf:Bag>{lis}</rdf:Bag></dc:subject>"
  return (
      '<?xml version="1.0" encoding="UTF-8"?>\n'
      '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
      ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
      '  <rdf:Description rdf:about=""\n'
      f'    xmlns:xmp="{NS_XMP}"\n'
      f'    xmlns:dc="{NS_DC}"{attrs}>{subject}\n'
      '  </rdf:Description>\n'
      ' </rdf:RDF>\n'
      '</x:xmpmeta>\n').encode("utf-8")


@dataclasses.dataclass
class UpdateResult:
  changed: bool
  backup_path: str = None     # set when a first-seen backup was made
  before_hash: str = None
  after_hash: str = None


def _stat_key(path):
  st = os.stat(path)
  return (st.st_mtime_ns, st.st_size)


def _atomic_write(path, data, mode):
  directory = os.path.dirname(path) or "."
  fd, tmp = tempfile.mkstemp(dir=directory, prefix=".xmp-", suffix=".tmp")
  try:
    with os.fdopen(fd, "wb") as f:
      f.write(data)
      f.flush()
      os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def update_file(path, backup_path=None, dry_run=False, retries=3, **edit):
  """Edit the sidecar at path in place, atomically.

  edit takes the arguments of edit_bytes (rating, add_tags, remove_tags).
  If backup_path is given and does not exist yet, the original bytes are
  copied there before the first change. If the file changed between reading
  and writing (Lightroom/darktable at the same time), it is re-read and only
  the edited fields are re-applied to the new content.
  """
  for _ in range(retries):
    key = _stat_key(path)
    mode = os.stat(path).st_mode & 0o7777
    with open(path, "rb") as f:
      data = f.read()
    new = edit_bytes(data, **edit)
    if new == data:
      return UpdateResult(False)
    if dry_run:
      return UpdateResult(True, None, hashlib.sha224(data).hexdigest(),
                          hashlib.sha224(new).hexdigest())
    made_backup = None
    if backup_path and not os.path.exists(backup_path):
      os.makedirs(os.path.dirname(backup_path), exist_ok=True)
      shutil.copyfile(path, backup_path)
      made_backup = backup_path
    if _stat_key(path) != key:
      continue  # changed under us: re-read and re-apply
    _atomic_write(path, new, mode)
    return UpdateResult(True, made_backup, hashlib.sha224(data).hexdigest(),
                        hashlib.sha224(new).hexdigest())
  raise XmpEditError(f"{path} keeps changing; giving up")


def create_file(path, rating=0, tags=()):
  """Create a new sidecar; refuses to overwrite an existing file."""
  data = new_sidecar_bytes(rating, tags)
  directory = os.path.dirname(path) or "."
  fd, tmp = tempfile.mkstemp(dir=directory, prefix=".xmp-", suffix=".tmp")
  try:
    with os.fdopen(fd, "wb") as f:
      f.write(data)
      f.flush()
      os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.link(tmp, path)  # fails if path exists
  finally:
    os.unlink(tmp)
