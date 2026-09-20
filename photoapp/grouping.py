"""Group files into Photos.

Automatic rule: within one directory, files with the same case-insensitive
basename that are a RAW and a JPG form one Photo. The RAW is the
'original'; the JPG is the 'camera' tuning derived from it. Any other file
is a Photo of its own. Manual overrides (link_source = 'manual') are never
touched here; see manual links.

Photos keep their identity across regrouping, so ratings and flags stored on
the Photo survive: a group reuses the Photo of its original, else the Photo
of any member (preferring one that is already rated).
"""

import os

from photoapp import fileinfo

_JPEG_EXTENSIONS = {".jpg", ".jpeg"}


def _dirname(path):
  return path.rpartition("/")[0] or "."


def _split_groups(members):
  """Split one directory's auto files into groups of (original, camera).

  members: [(id, name)] of files in one directory. Returns a list of
  (original_id, [camera_ids], [all ids]) tuples in a deterministic order.
  """
  by_stem = {}
  for file_id, name in sorted(members, key=lambda m: m[1]):
    by_stem.setdefault(os.path.splitext(name)[0].lower(), []).append(
        (file_id, name))
  groups = []
  for stem in sorted(by_stem):
    raws = [m for m in by_stem[stem] if fileinfo.is_raw(m[1])]
    jpgs = [m for m in by_stem[stem]
            if os.path.splitext(m[1])[1].lower() in _JPEG_EXTENSIONS]
    others = [m for m in by_stem[stem] if m not in raws and m not in jpgs]
    if raws and jpgs:
      groups.append((raws[0][0], [jpgs[0][0]],
                     [raws[0][0], jpgs[0][0]]))
      singles = raws[1:] + jpgs[1:] + others
    else:
      singles = by_stem[stem]
    for file_id, _ in singles:
      groups.append((file_id, [], [file_id]))
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
    for original_id, camera_ids, all_ids in _split_groups(members):
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
