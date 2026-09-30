"""On-disk metadata cache for the full scan (ticket 111).

When ``--write_metadata_json`` is on (the default), a scan writes into each directory:

* ``index.json`` -- the directory's own mtime plus one record per image file
  directly in it, and
* ``<name>.json`` next to each image (``a.DNG`` -> ``a.DNG.json``) -- that one
  file's record.

A later scan of an unchanged directory or file can then answer from these files
instead of decoding the image (and, when the database no longer has the row,
instead of re-hashing it either). The database stays the source of truth: this
is a cache the scan trusts and rebuilds, not something the app answers requests
from.

The validity rule is the same one the in-database ``dir_mtimes`` cache uses: a
record is reused only when its recorded mtime *and* bytesize match the file on
disk. A record from an older cache that lacks a key added later (for example
``focal_length``, or ticket 156's ``lens_model``) is backfilled by re-reading
that file's EXIF; the directory is then reprocessed even though its own mtime is
unchanged (see ``index_lacks_keys``).

Writing ``index.json`` (or a per-file JSON) changes the directory's mtime, which
would make the next scan think the directory changed. ``write_index`` re-stats
the directory after writing and, if the mtime moved, rewrites ``index.json``
with the corrected value, so the file and the caller's ``dir_mtimes`` cache agree
on the directory's true final mtime. This is lifted from the now-retired
``file_metadata.py`` standalone tool (ticket 133), which used exactly this scheme
for hashes/size/dimensions; ``tools/archive/catalog.py`` (ticket 131/133) still
does, for trees this app doesn't own.
"""

import json
import os

INDEX_JSON_NAME = "index.json"

# Keys of a complete per-file cache record. ``mtime`` is the cache-validity key
# itself; the rest are the metadata a scan stores in ``files``.
REQUIRED_KEYS = (
    "mime_type", "width", "height", "hash", "bytesize", "exif_date",
    "aperture", "shutter_speed", "iso", "focal_length", "camera_make",
    "camera_model", "lens_model",
)
RECORD_KEYS = REQUIRED_KEYS + ("mtime",)


def index_path(dirpath):
  return os.path.join(dirpath, INDEX_JSON_NAME)


def record_path(dirpath, name):
  return os.path.join(dirpath, name + ".json")


def load_index(dirpath):
  """The parsed index.json for dirpath as {'mtime': float, 'files': {...}}, or {}.

  A missing or unreadable/malformed file is treated as no cache at all.
  """
  try:
    with open(index_path(dirpath)) as f:
      data = json.load(f)
  except (OSError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def load_records(dirpath):
  """{name: record} from dirpath's index.json, or {} if there is none."""
  files = load_index(dirpath).get("files")
  return files if isinstance(files, dict) else {}


def has_all_keys(record, required_keys=REQUIRED_KEYS):
  """True if record has every required key (a present None counts as present)."""
  return all(k in record for k in required_keys)


def record_is_stale(record):
  """True if a cache record must be re-read once because it predates an extraction fix.

  Ticket 162: a RAW file Pillow cannot identify (a Pentax PEF) was cached before ticket 161's TIFF
  IFD fallback, so its record has every REQUIRED_KEYS key -- all None -- and the generic
  ``image/x-raw`` mime; ``has_all_keys`` passes and a scan would reuse it forever. Keyed on the
  record, not the filename, so any similarly-unreadable RAW heals the same way. After one re-read
  the record either carries real values (no longer stale) or is genuinely EXIF-less (re-read
  cheaply on later scans -- only such records, and there are few).
  """
  return (record.get("mime_type") == "image/x-raw"
          and record.get("exif_date") is None
          and record.get("camera_make") is None)


# The RAW mime types: a lens can only come from the MakerNote for these (ticket 166).
_RAW_MIMES = ("image/x-adobe-dng", "image/x-raw")


def record_lacks_lens(record):
  """True if a RAW record's extraction otherwise worked but has no lens yet (ticket 167).

  A record written before ticket 166 has ``lens_model = None`` (Pentax has no standard LensModel
  tag) with every other field present, so a scan would reuse it and the Files pane would stay
  empty. Unlike ``record_is_stale`` this is fixed by a cheap MakerNote-only probe in ``_scan_files``
  (``read_pentax_lens``), never a full ``read_image_metadata`` re-read; a RAW whose MakerNote has no
  lens is probed again on each scan, which is cheap and only affects such files.
  """
  return (record.get("lens_model") is None
          and record.get("camera_make") is not None
          and record.get("mime_type") in _RAW_MIMES)


def index_lacks_keys(dirpath, names, required_keys=REQUIRED_KEYS):
  """True if any of names lacks a complete record in dirpath's index.json, or has a stale/lens-less
  one.

  A directory with no image names returns False (nothing to cache). If it has image names but no
  index.json at all, every one of them "lacks" a record, so this returns True and the directory is
  reprocessed.
  """
  names = list(names)
  if not names:
    return False
  records = load_records(dirpath)
  return any(not has_all_keys(records.get(n, {}), required_keys)
             or record_is_stale(records.get(n, {}))
             or record_lacks_lens(records.get(n, {})) for n in names)


def write_record(dirpath, name, record):
  """Write one file's per-file <name>.json."""
  with open(record_path(dirpath, name), "w") as f:
    json.dump(record, f, indent=2, sort_keys=True)


def write_index(dirpath, dir_mtime, records):
  """Write dirpath's index.json and return the directory's final mtime.

  Writing the file can itself change dirpath's mtime (a new index.json creates a
  new directory entry; overwriting an existing one usually doesn't). The
  directory is re-stat-ed after writing and, if that differs from dir_mtime,
  index.json is rewritten once more with the corrected value.
  """
  data = {"mtime": dir_mtime, "files": records}
  with open(index_path(dirpath), "w") as f:
    json.dump(data, f, indent=2, sort_keys=True)

  final_mtime = os.stat(dirpath).st_mtime
  if final_mtime != dir_mtime:
    data["mtime"] = final_mtime
    with open(index_path(dirpath), "w") as f:
      json.dump(data, f, indent=2, sort_keys=True)
  return final_mtime


def write_dir(dirpath, dir_mtime, records):
  """Write per-file JSONs plus index.json; return the directory's final mtime.

  Per-file <name>.json files for images that are no longer in the directory are
  removed (a stale cache file is harmless, but leaving it around is untidy and
  would otherwise accumulate forever).
  """
  previous = load_records(dirpath)
  for name, record in records.items():
    write_record(dirpath, name, record)
  for name in previous:
    if name not in records:
      try:
        os.remove(record_path(dirpath, name))
      except OSError:
        pass
  return write_index(dirpath, dir_mtime, records)
