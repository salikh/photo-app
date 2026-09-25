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
  raw_settings.set(conn, fid, bright=1.5, wb_mode="manual", wb_r=2.1, wb_g=1.0, wb_b=1.8,
                   highlight=2, exposure=2.0, shadow=0.3, saturation=1.4, contrast=1.5, noise=1,
                   demosaic=4)
  s = raw_settings.get(conn, fid)
  assert s == {"raw_bright": 1.5, "raw_wb_mode": "manual", "raw_wb_r": 2.1, "raw_wb_g": 1.0,
              "raw_wb_b": 1.8, "raw_highlight": 2, "raw_exposure": 2.0, "raw_shadow": 0.3,
              "raw_saturation": 1.4, "raw_contrast": 1.5, "raw_noise": 1, "raw_demosaic": 4}
  assert not raw_settings.is_default(s)


def test_set_bumps_the_thumb_revision(settings, conn):
  # ticket 119: saving settings changes the rendered bytes, so the file's revision must move.
  fid = scanned(settings, conn)
  rev = lambda: conn.execute("SELECT thumb_rev FROM files WHERE id = ?", (fid,)).fetchone()[0]
  assert rev() == 0
  raw_settings.set(conn, fid, bright=1.2)
  assert rev() == 1
  raw_settings.set(conn, fid)   # back to default also changes the render
  assert rev() == 2


def test_set_with_no_args_clears_back_to_default(settings, conn):
  fid = scanned(settings, conn)
  raw_settings.set(conn, fid, bright=1.2)
  assert not raw_settings.is_default(raw_settings.get(conn, fid))
  raw_settings.set(conn, fid)   # every column defaults to None
  assert raw_settings.is_default(raw_settings.get(conn, fid))


def test_set_rejects_bad_wb_mode_and_out_of_range_values(settings, conn):
  fid = scanned(settings, conn)
  for kwargs in ({"wb_mode": "nope"}, {"highlight": 10}, {"highlight": -1}, {"exposure": 0.2},
                 {"exposure": 8.1}, {"shadow": -0.1}, {"shadow": 0.6}, {"saturation": -0.1},
                 {"saturation": 2.1}, {"contrast": 0.4}, {"contrast": 2.1}, {"noise": 3},
                 {"noise": -1}, {"demosaic": 5}, {"demosaic": 10}):
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
