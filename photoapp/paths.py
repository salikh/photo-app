"""Path helpers for queries on the files table.

All paths are relative to the pictures dir with '/' separators; the root is
'.'. Subtree queries use a range on the unique path index instead of LIKE,
so they read only the rows they need.
"""

_MAX = "\U0010ffff"


def dirname(path):
  """Directory of a relative file path ('.' for the root)."""
  return path.rpartition("/")[0] or "."


def subtree_range(rel_dir):
  """(lo, hi): a path is under rel_dir iff lo <= path < hi."""
  if rel_dir in ("", "."):
    return "", _MAX
  return rel_dir + "/", rel_dir + "0"       # '0' is the character after '/'


def direct_children_sql(column="path"):
  """SQL condition: the path in `column` has no '/' after the prefix.

  Use with the arguments (lo, hi, len(lo) + 1) after
  "path >= ? AND path < ? AND ".
  """
  return f"instr(substr({column}, ?), '/') = 0"


def files_in_dir(conn, rel_dir, columns="id, path"):
  """Rows of the files directly in rel_dir (not in its subdirectories)."""
  lo, hi = subtree_range(rel_dir)
  return conn.execute(
      f"SELECT {columns} FROM files WHERE path >= ? AND path < ? AND "
      f"{direct_children_sql()}", (lo, hi, len(lo) + 1)).fetchall()


def dirs_in_subtree(conn, rel_dir, recursive=True):
  """Directories (relative paths) that contain files under rel_dir.

  Streams the paths, so memory is proportional to the number of directories.
  """
  lo, hi = subtree_range(rel_dir)
  dirs = set()
  for row in conn.execute(
      "SELECT path FROM files WHERE path >= ? AND path < ?", (lo, hi)):
    d = dirname(row[0])
    if recursive or d == rel_dir:
      dirs.add(d)
  return dirs
