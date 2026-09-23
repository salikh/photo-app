import os

from photoapp import raw_settings
from photoapp import scan
from tests.test_grouping import touch


def file_id(conn, path):
  return conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0]


def scanned(settings, conn):
  touch(os.path.join(settings.pictures_dir, "a.DNG"))
  scan.scan(conn, settings.pictures_dir)
  return file_id(conn, "a.DNG")


def test_get_defaults_to_all_none(settings, conn):
  fid = scanned(settings, conn)
  s = raw_settings.get(conn, fid)
  assert s == {c: None for c in raw_settings.COLUMNS}
  assert raw_settings.is_default(s)


def test_set_then_get_round_trips(settings, conn):
  fid = scanned(settings, conn)
  raw_settings.set(conn, fid, bright=1.5, wb_mode="manual", wb_r=2.1, wb_g=1.0, wb_b=1.8, highlight=2)
  s = raw_settings.get(conn, fid)
  assert s == {"raw_bright": 1.5, "raw_wb_mode": "manual", "raw_wb_r": 2.1, "raw_wb_g": 1.0,
              "raw_wb_b": 1.8, "raw_highlight": 2}
  assert not raw_settings.is_default(s)


def test_set_with_no_args_clears_back_to_default(settings, conn):
  fid = scanned(settings, conn)
  raw_settings.set(conn, fid, bright=1.2)
  assert not raw_settings.is_default(raw_settings.get(conn, fid))
  raw_settings.set(conn, fid)   # every column defaults to None
  assert raw_settings.is_default(raw_settings.get(conn, fid))


def test_set_rejects_bad_wb_mode_and_out_of_range_highlight(settings, conn):
  fid = scanned(settings, conn)
  for kwargs in ({"wb_mode": "nope"}, {"highlight": 10}, {"highlight": -1}):
    try:
      raw_settings.set(conn, fid, **kwargs)
      assert False, f"should have raised for {kwargs}"
    except raw_settings.SettingsError:
      pass
  assert raw_settings.is_default(raw_settings.get(conn, fid))   # nothing partially applied


def test_set_rejects_unknown_file(settings, conn):
  try:
    raw_settings.set(conn, 999999, bright=1.0)
    assert False, "should have raised"
  except raw_settings.SettingsError:
    pass
