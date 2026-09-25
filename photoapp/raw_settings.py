"""Per-file RAW conversion settings (ticket 085, extended by 109): brightness, white balance,
highlight recovery, exposure, shadow-pull.

Nullable columns on `files` (photoapp/db.py's migrations); a file is "at default" iff every
column is NULL -- the condition docs/design/thumbnails.md uses to decide whether the cheap
embedded-preview shortcut is still available for that file, at any size.
"""

COLUMNS = ("raw_bright", "raw_wb_mode", "raw_wb_r", "raw_wb_g", "raw_wb_b", "raw_highlight",
          "raw_exposure", "raw_shadow")
WB_MODES = ("camera", "auto", "manual")
EXPOSURE_RANGE = (0.25, 8.0)   # LibRaw's own valid range for exp_shift
SHADOW_RANGE = (0.0, 0.5)      # ticket 109: verified monotonic/highlight-neutral only up to 0.5


class SettingsError(Exception):
  """A request that cannot be carried out; message is safe to show."""


def get(conn, file_id):
  """{column: value} for file_id, every value None if the file has no live settings row."""
  row = conn.execute(
      f"SELECT {', '.join(COLUMNS)} FROM files WHERE id = ?", (file_id,)).fetchone()
  return {c: row[c] for c in COLUMNS} if row else {c: None for c in COLUMNS}


def is_default(settings):
  return all(v is None for v in settings.values())


def validate(wb_mode=None, highlight=None, exposure=None, shadow=None):
  """Raises SettingsError for an invalid wb_mode or an out-of-range highlight/exposure/shadow --
  shared by set() (which persists) and ticket 094's raw_preview route (which never persists, but
  still needs the same "is this a legal settings value" check before spending a demosaic on it)."""
  if wb_mode is not None and wb_mode not in WB_MODES:
    raise SettingsError(f"wb_mode must be one of {WB_MODES}")
  if highlight is not None and not (0 <= highlight <= 9):
    raise SettingsError("highlight must be 0-9")
  if exposure is not None and not (EXPOSURE_RANGE[0] <= exposure <= EXPOSURE_RANGE[1]):
    raise SettingsError(f"exposure must be {EXPOSURE_RANGE[0]}-{EXPOSURE_RANGE[1]}")
  if shadow is not None and not (SHADOW_RANGE[0] <= shadow <= SHADOW_RANGE[1]):
    raise SettingsError(f"shadow must be {SHADOW_RANGE[0]}-{SHADOW_RANGE[1]}")


def to_columns(bright=None, wb_mode=None, wb_r=None, wb_g=None, wb_b=None, highlight=None,
              exposure=None, shadow=None):
  """{column: value}, shaped like get()'s return, for a caller that has pending values in hand
  (ticket 094's raw_preview route) and wants them ready for previews.render() without writing
  anything to the database."""
  return {"raw_bright": bright, "raw_wb_mode": wb_mode, "raw_wb_r": wb_r, "raw_wb_g": wb_g,
          "raw_wb_b": wb_b, "raw_highlight": highlight, "raw_exposure": exposure,
          "raw_shadow": shadow}


def set(conn, file_id, bright=None, wb_mode=None, wb_r=None, wb_g=None, wb_b=None, highlight=None,
       exposure=None, shadow=None):
  """Replace file_id's settings wholesale (every column, including back to NULL for an omitted
  one -- matches how the frontend's sliders/controls always submit the full current set, not a
  partial patch). Raises SettingsError for an invalid wb_mode or an out-of-range highlight/
  exposure/shadow."""
  validate(wb_mode, highlight, exposure, shadow)
  if conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None:
    raise SettingsError(f"no such file: {file_id}")
  conn.execute(
      "UPDATE files SET raw_bright = ?, raw_wb_mode = ?, raw_wb_r = ?, raw_wb_g = ?, "
      "raw_wb_b = ?, raw_highlight = ?, raw_exposure = ?, raw_shadow = ? WHERE id = ?",
      (bright, wb_mode, wb_r, wb_g, wb_b, highlight, exposure, shadow, file_id))
  conn.commit()
