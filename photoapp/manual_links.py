"""Manual link/unlink decisions, applied after the automatic grouping rule.

Decisions are the one thing about grouping that cannot be rebuilt from disk,
so every change is stored in the manual_links table and also appended to
<state_dir>/manual_links.jsonl. If the database is lost, restore_if_empty()
reloads them. A file is identified by path, with its content hash as a
secondary key for files that moved.

  link:   attach `path` as a tuning of the Photo that contains `target_path`
  unlink: detach `path` into a Photo of its own, even if the rule would group it

The latest decision per path wins. Files touched by a decision get
link_source = 'manual', which the automatic rule leaves alone.
"""

import datetime
import json
import os

from absl import logging

from photoapp import grouping

JSONL_NAME = "manual_links.jsonl"
LINK_ROLES = ("tuning", "export")


def _jsonl_path(state_dir):
  return os.path.join(state_dir, JSONL_NAME)


def _file_row(conn, path):
  return conn.execute(
      "SELECT id, path, hash, photo_id FROM files WHERE path = ?",
      (path,)).fetchone()


def _resolve(conn, path, file_hash):
  """Find the file for a decision: by path, else by a unique live hash."""
  row = _file_row(conn, path)
  if row is not None or not file_hash:
    return row
  rows = conn.execute(
      "SELECT id, path, hash, photo_id FROM files "
      "WHERE hash = ? AND missing = 0", (file_hash,)).fetchall()
  return rows[0] if len(rows) == 1 else None


def _record(conn, entry):
  conn.execute(
      "INSERT INTO manual_links (path, hash, target_path, target_hash,"
      " action, role, created_at) VALUES (:path, :hash, :target_path,"
      " :target_hash, :action, :role, :created_at)", entry)


def _append_jsonl(state_dir, entry):
  """The durable copy. Written after the database commit, so a database step that is
  repeated (busy database) never appends the same decision twice."""
  os.makedirs(state_dir, exist_ok=True)
  with open(_jsonl_path(state_dir), "a") as f:
    f.write(json.dumps(entry, sort_keys=True) + "\n")


def _entry(conn, path, action, target_path=None, role=None):
  f = _file_row(conn, path)
  t = _file_row(conn, target_path) if target_path else None
  return {
      "path": path, "hash": f["hash"] if f else None,
      "target_path": target_path, "target_hash": t["hash"] if t else None,
      "action": action, "role": role,
      "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
  }


def _check_link(conn, file_row, target_row):
  if file_row is None or target_row is None:
    raise ValueError("unknown file")
  if file_row["id"] == target_row["id"]:
    raise ValueError("cannot link a file to itself")
  if file_row["photo_id"] == target_row["photo_id"]:
    raise ValueError("files already belong to the same photo")
  photo = conn.execute(
      "SELECT original_file_id FROM photos WHERE id = ?",
      (file_row["photo_id"],)).fetchone()
  members = conn.execute(
      "SELECT COUNT(*) FROM files WHERE photo_id = ?",
      (file_row["photo_id"],)).fetchone()[0]
  if photo["original_file_id"] == file_row["id"] and members > 1:
    raise ValueError("file is the original of a photo with other files; "
                     "unlink those first")


def link(conn, state_dir, path, target_path, role="tuning"):
  """Attach path as a tuning/export of the Photo containing target_path."""
  if role not in LINK_ROLES:
    raise ValueError(f"role must be one of {LINK_ROLES}")
  _check_link(conn, _file_row(conn, path), _file_row(conn, target_path))
  entry = _entry(conn, path, "link", target_path, role)
  _record(conn, entry)
  apply_all(conn)                     # commits
  _append_jsonl(state_dir, entry)


def unlink(conn, state_dir, path):
  """Detach path into a Photo of its own."""
  if _file_row(conn, path) is None:
    raise ValueError("unknown file")
  entry = _entry(conn, path, "unlink")
  _record(conn, entry)
  apply_all(conn)                     # commits
  _append_jsonl(state_dir, entry)


def _latest_decisions(conn):
  """Latest decision per path, in the order they were made."""
  latest = {}
  for r in conn.execute("SELECT * FROM manual_links ORDER BY id"):
    latest.pop(r["path"], None)
    latest[r["path"]] = r
  return list(latest.values())


def _detach(conn, f):
  """Give file f (already in some Photo) a Photo of its own, if not alone."""
  alone = conn.execute(
      "SELECT COUNT(*) FROM files WHERE photo_id = ?",
      (f["photo_id"],)).fetchone()[0] == 1
  if alone and f["photo_id"] is not None:
    conn.execute("UPDATE files SET role = 'original', derived_from = NULL, "
                 "link_source = 'manual' WHERE id = ?", (f["id"],))
    return
  cur = conn.execute(
      "INSERT INTO photos (original_file_id, representative_file_id) "
      "VALUES (?, ?)", (f["id"], f["id"]))
  conn.execute(
      "UPDATE files SET photo_id = ?, role = 'original', derived_from = NULL,"
      " link_source = 'manual' WHERE id = ?", (cur.lastrowid, f["id"]))


def apply_all(conn):
  """Apply the latest decision for every path. Idempotent; run after
  grouping.regroup on every scan and after each change."""
  for d in _latest_decisions(conn):
    f = _resolve(conn, d["path"], d["hash"])
    if f is None:
      logging.info("manual link for %s: file not found", d["path"])
      continue
    if d["action"] == "unlink":
      _detach(conn, f)
      continue
    t = _resolve(conn, d["target_path"], d["target_hash"])
    if t is None:
      logging.info("manual link %s -> %s: target not found",
                   d["path"], d["target_path"])
      continue
    f = _file_row(conn, f["path"])
    t = _file_row(conn, t["path"])
    if f["photo_id"] == t["photo_id"]:
      conn.execute("UPDATE files SET link_source = 'manual' WHERE id = ?",
                   (f["id"],))
      continue
    try:
      _check_link(conn, f, t)
    except ValueError as e:
      logging.warning("manual link %s -> %s skipped: %s",
                      d["path"], d["target_path"], e)
      continue
    photo = conn.execute(
        "SELECT original_file_id FROM photos WHERE id = ?",
        (t["photo_id"],)).fetchone()
    role = d["role"] or "tuning"
    conn.execute(
        "UPDATE files SET photo_id = ?, role = ?, derived_from = ?, "
        "link_source = 'manual' WHERE id = ?",
        (t["photo_id"], role, photo["original_file_id"], f["id"]))
  grouping._delete_empty_photos(conn)
  grouping.fix_representatives(conn)
  conn.commit()


def restore_if_empty(conn, state_dir):
  """Reload decisions from the JSONL mirror into an empty table."""
  if conn.execute("SELECT COUNT(*) FROM manual_links").fetchone()[0]:
    return 0
  try:
    with open(_jsonl_path(state_dir)) as f:
      lines = [json.loads(l) for l in f if l.strip()]
  except OSError:
    return 0
  for entry in lines:
    conn.execute(
        "INSERT INTO manual_links (path, hash, target_path, target_hash,"
        " action, role, created_at) VALUES (:path, :hash, :target_path,"
        " :target_hash, :action, :role, :created_at)",
        {"role": None, **entry})
  conn.commit()
  return len(lines)
