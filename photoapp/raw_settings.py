"""Per-file RAW conversion settings (ticket 085): brightness, white balance, highlight recovery.

Nullable columns on `files` (photoapp/db.py's migrations); a file is "at default" iff every
column is NULL -- the condition docs/design/thumbnails.md uses to decide whether the cheap
embedded-preview shortcut is still available for that file, at any size.
"""

COLUMNS = ("raw_bright", "raw_wb_mode", "raw_wb_r", "raw_wb_g", "raw_wb_b", "raw_highlight")
WB_MODES = ("camera", "auto", "manual")


class SettingsError(Exception):
  """A request that cannot be carried out; message is safe to show."""


def get(conn, file_id):
  """{column: value} for file_id, every value None if the file has no live settings row."""
  row = conn.execute(
      f"SELECT {', '.join(COLUMNS)} FROM files WHERE id = ?", (file_id,)).fetchone()
  return {c: row[c] for c in COLUMNS} if row else {c: None for c in COLUMNS}


def is_default(settings):
  return all(v is None for v in settings.values())


def set(conn, file_id, bright=None, wb_mode=None, wb_r=None, wb_g=None, wb_b=None, highlight=None):
  """Replace file_id's settings wholesale (every column, including back to NULL for an omitted
  one -- matches how the frontend's sliders/controls always submit the full current set, not a
  partial patch). Raises SettingsError for an invalid wb_mode or an out-of-range highlight."""
  if wb_mode is not None and wb_mode not in WB_MODES:
    raise SettingsError(f"wb_mode must be one of {WB_MODES}")
  if highlight is not None and not (0 <= highlight <= 9):
    raise SettingsError("highlight must be 0-9")
  if conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None:
    raise SettingsError(f"no such file: {file_id}")
  conn.execute(
      "UPDATE files SET raw_bright = ?, raw_wb_mode = ?, raw_wb_r = ?, raw_wb_g = ?, "
      "raw_wb_b = ?, raw_highlight = ? WHERE id = ?",
      (bright, wb_mode, wb_r, wb_g, wb_b, highlight, file_id))
  conn.commit()
