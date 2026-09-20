"""Group files into Photos.

Automatic rule: within one directory, files with the same case-insensitive
basename form one Photo when they include at least two of RAW, JPG, TIFF,
PNG. The original is the first of RAW > JPG > TIFF > PNG; a JPG beside a RAW
is the 'camera' file, the others are 'tuning' files derived from the
original. Any other file is a Photo of its own. Manual overrides
(link_source = 'manual') are never touched here; see manual links.

Photos keep their identity across regrouping, so ratings and flags stored on
the Photo survive: a group reuses the Photo of its original, else the Photo
of any member (preferring one that is already rated).
"""

import os

from photoapp import fileinfo

# Bump when the automatic rule changes: scan() then regroups every directory
# once, so databases scanned by an older rule catch up.
GROUPING_VERSION = "2"   # 1: RAW+JPG; 2: also TIF/PNG (ticket 015)

_JPEG_EXTENSIONS = {".jpg", ".jpeg"}
_TIFF_EXTENSIONS = {".tif", ".tiff"}
_PNG_EXTENSIONS = {".png"}


def _dirname(path):
  return path.rpartition("/")[0] or "."


def _split_groups(members):
  """Split one directory's auto files into groups.

  members: [(id, name)] of files in one directory. Files with the same
  case-insensitive stem are one Photo when they include at least two of: a
  RAW, a JPEG, a TIFF, a PNG. The original is the first available of
  RAW > JPEG > TIFF > PNG; a JPEG next to a RAW is its 'camera' file; all
  other TIFF/PNG (and JPEG without a RAW) are 'tuning' files derived from the
  original. Extra RAW/JPEG duplicates and any other type stay on their own.

  Returns [(original_id, camera_ids, tuning_ids, all_ids)] in a stable order.
  """
  by_stem = {}
  for file_id, name in sorted(members, key=lambda m: m[1]):
    by_stem.setdefault(os.path.splitext(name)[0].lower(), []).append(
        (file_id, name))

  def ext(m):
    return os.path.splitext(m[1])[1].lower()

  groups = []
  for stem in sorted(by_stem):
    files = by_stem[stem]
    raws = [m for m in files if fileinfo.is_raw(m[1])]
    jpgs = [m for m in files if ext(m) in _JPEG_EXTENSIONS]
    tiffs = [m for m in files if ext(m) in _TIFF_EXTENSIONS]
    pngs = [m for m in files if ext(m) in _PNG_EXTENSIONS]
    kinds = [k for k in (raws, jpgs, tiffs, pngs) if k]
    if len(kinds) < 2:
      for m in files:
        groups.append((m[0], [], [], [m[0]]))
      continue
    # first of each kind takes part; duplicates of a kind stay single
    taking = [k[0] for k in kinds]
    singles = [m for k in kinds for m in k[1:]]
    singles += [m for m in files if m not in raws + jpgs + tiffs + pngs]
    original = taking[0]
    camera = [m[0] for m in taking[1:] if m in jpgs] if raws else []
    tunings = [m[0] for m in taking[1:] if m[0] not in camera]
    groups.append((original[0], camera, tunings,
                   [original[0]] + camera + tunings))
    for m in singles:
      groups.append((m[0], [], [], [m[0]]))
  return groups


def _choose_photo(conn, original_id, member_ids, existing):
  """Pick the existing Photo a group should reuse, or None."""
  candidates = [pid for pid in existing.get(original_id, [])]
  if candidates:
    return candidates[0]
  rated = []
  for m in member_ids:
    for pid in existing.get(m, []):
      rated.append(pid)
  if not rated:
    return None
  rows = conn.execute(
      f"SELECT id FROM photos WHERE id IN ({','.join('?' * len(rated))}) "
      "ORDER BY (rating != 0) DESC, id", rated).fetchall()
  return rows[0]["id"] if rows else None


def regroup(conn, rel_dirs=None):
  """(Re)apply the automatic rule to rel_dirs (default: every directory).

  rel_dirs holds directory paths relative to the pictures dir ('.' for the
  root). Returns the number of Photos created.
  """
  rows = conn.execute(
      "SELECT id, path, photo_id, link_source FROM files").fetchall()
  by_dir = {}
  for r in rows:
    by_dir.setdefault(_dirname(r["path"]), []).append(r)
  dirs = sorted(by_dir) if rel_dirs is None else sorted(
      d for d in rel_dirs if d in by_dir)

  # photo ids currently attached to a file (auto or manual), per file id
  attached = {}
  for r in rows:
    if r["photo_id"] is not None:
      attached.setdefault(r["id"], []).append(r["photo_id"])

  created = 0
  for d in dirs:
    auto = [r for r in by_dir[d] if r["link_source"] == "auto"]
    members = [(r["id"], r["path"].rpartition("/")[2]) for r in auto]
    for original_id, camera_ids, tuning_ids, all_ids in _split_groups(members):
      photo_id = _choose_photo(conn, original_id, all_ids, attached)
      default_rep = camera_ids[0] if camera_ids else original_id
      if photo_id is None:
        cur = conn.execute(
            "INSERT INTO photos (original_file_id, representative_file_id) "
            "VALUES (?, ?)", (original_id, default_rep))
        photo_id = cur.lastrowid
        created += 1
      else:
        photo = conn.execute(
            "SELECT representative_file_id, representative_source "
            "FROM photos WHERE id = ?", (photo_id,)).fetchone()
        keep = (photo["representative_source"] == "manual"
                and photo["representative_file_id"] in all_ids)
        conn.execute(
            "UPDATE photos SET original_file_id = ?, "
            "representative_file_id = ?, "
            "representative_source = ? WHERE id = ?",
            (original_id, photo["representative_file_id"] if keep
             else default_rep, "manual" if keep else "auto", photo_id))
      conn.execute(
          "UPDATE files SET photo_id = ?, role = 'original', "
          "derived_from = NULL, link_source = 'auto' WHERE id = ?",
          (photo_id, original_id))
      for cid in camera_ids:
        conn.execute(
            "UPDATE files SET photo_id = ?, role = 'camera', "
            "derived_from = ?, link_source = 'auto' WHERE id = ?",
            (photo_id, original_id, cid))
      for tid in tuning_ids:
        conn.execute(
            "UPDATE files SET photo_id = ?, role = 'tuning', "
            "derived_from = ?, link_source = 'auto' WHERE id = ?",
            (photo_id, original_id, tid))
  _delete_empty_photos(conn)
  fix_representatives(conn)
  conn.commit()
  return created


def fix_representatives(conn):
  """Reset representatives that are no longer members of their Photo.

  Happens when a file is detached or moved to another Photo. The new
  representative is the default: the camera JPG if there is one, else the
  original.
  """
  bad = conn.execute(
      "SELECT p.id, p.original_file_id FROM photos p "
      "LEFT JOIN files f ON f.id = p.representative_file_id "
      "WHERE f.photo_id IS NOT p.id").fetchall()
  for r in bad:
    camera = conn.execute(
        "SELECT id FROM files WHERE photo_id = ? AND role = 'camera' "
        "ORDER BY path", (r["id"],)).fetchone()
    conn.execute(
        "UPDATE photos SET representative_file_id = ?, "
        "representative_source = 'auto' WHERE id = ?",
        (camera["id"] if camera else r["original_file_id"], r["id"]))


def _delete_empty_photos(conn):
  empty = [r["id"] for r in conn.execute(
      "SELECT id FROM photos WHERE id NOT IN "
      "(SELECT photo_id FROM files WHERE photo_id IS NOT NULL)")]
  for pid in empty:
    conn.execute("DELETE FROM tags WHERE photo_id = ?", (pid,))
    conn.execute("DELETE FROM photos WHERE id = ?", (pid,))


def set_representative(conn, photo_id, file_id):
  """Choose which file represents a Photo; None goes back to the default.

  A chosen file survives rescans while it remains a live member of the
  Photo (see fix_representatives).
  """
  photo = conn.execute("SELECT original_file_id FROM photos WHERE id = ?",
                       (photo_id,)).fetchone()
  if photo is None:
    raise ValueError(f"no such photo: {photo_id}")
  if file_id is None:
    camera = conn.execute(
        "SELECT id FROM files WHERE photo_id = ? AND role = 'camera' AND "
        "missing = 0 ORDER BY path", (photo_id,)).fetchone()
    conn.execute(
        "UPDATE photos SET representative_file_id = ?, "
        "representative_source = 'auto' WHERE id = ?",
        (camera["id"] if camera else photo["original_file_id"], photo_id))
  else:
    member = conn.execute(
        "SELECT id FROM files WHERE id = ? AND photo_id = ? AND missing = 0",
        (file_id, photo_id)).fetchone()
    if member is None:
      raise ValueError("file is not a live member of this photo")
    conn.execute(
        "UPDATE photos SET representative_file_id = ?, "
        "representative_source = 'manual' WHERE id = ?", (file_id, photo_id))
  conn.commit()


def regroup_if_rule_changed(conn):
  """Regroup everything once if the stored rule version is not current."""
  row = conn.execute(
      "SELECT value FROM meta WHERE key = 'grouping_version'").fetchone()
  if row is not None and row["value"] == GROUPING_VERSION:
    return False
  regroup(conn)
  conn.execute(
      "INSERT INTO meta (key, value) VALUES ('grouping_version', ?) "
      "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      (GROUPING_VERSION,))
  conn.commit()
  return True
