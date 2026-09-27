import os

import pytest
from absl import flags

from photoapp import config
from photoapp import fullscan  # noqa: F401  (defines --scan_dirs, a list flag)

FLAG_NAMES = ("config", "pictures_dir", "thumbs_dir", "state_dir", "database_path", "hashes_db", "port",
              "host", "xmp_dry_run", "nightly_scan_hour", "scan_dirs")


@pytest.fixture
def fv():
  """The real flags, restored afterwards (absl flags are global)."""
  f = flags.FLAGS
  f.mark_as_parsed()
  saved = {n: (f[n].value, f[n].default, f[n].present, f[n].using_default_value) for n in FLAG_NAMES}
  yield f
  for n, (value, default, present, using_default) in saved.items():
    f[n].default = default
    f[n].value, f[n].present, f[n].using_default_value = value, present, using_default


def write_config(tmp_path, text, name="photos.toml"):
  path = tmp_path / name
  path.write_text(text)
  return str(path)


def test_defaults_are_portable_and_derived_from_state_dir(fv, monkeypatch, tmp_path):
  monkeypatch.setenv("HOME", str(tmp_path))
  fv.state_dir = str(tmp_path / "state")
  fv.pictures_dir = "~/Pics"
  s = config.Settings.from_flags()
  assert s.pictures_dir == str(tmp_path / "Pics")
  assert s.thumbs_dir == str(tmp_path / "state" / "thumbs")
  assert s.db_path == str(tmp_path / "state" / "app.sqlite")
  assert "zoo" not in " ".join([s.pictures_dir, s.thumbs_dir, s.state_dir, s.db_path])


def test_default_state_dir_honours_xdg_data_home(monkeypatch):
  monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
  assert config.default_state_dir() == "/xdg/photos"
  monkeypatch.delenv("XDG_DATA_HOME")
  assert config.default_state_dir() == os.path.join("~", ".local", "share", "photos")


def test_database_path_can_be_set_separately(fv, tmp_path):
  fv.state_dir = str(tmp_path / "state")
  fv.database_path = str(tmp_path / "elsewhere" / "my.db")
  s = config.Settings.from_flags()
  assert s.db_path == str(tmp_path / "elsewhere" / "my.db")
  assert s.state_dir == str(tmp_path / "state")


def test_environment_variables_are_expanded_in_paths(fv, monkeypatch, tmp_path):
  monkeypatch.setenv("LIBRARY", str(tmp_path / "lib"))
  fv.pictures_dir = "$LIBRARY/photos"
  assert config.Settings.from_flags().pictures_dir == str(tmp_path / "lib" / "photos")


def test_config_file_sets_the_defaults(fv, tmp_path):
  path = write_config(tmp_path, f'pictures_dir = "{tmp_path}/p"\nport = 9191\n'
                                'xmp_dry_run = true\nnightly_scan_hour = -1\n'
                                'scan_dirs = ["2001", "2002"]\n')
  config.apply_config_file(path)
  s = config.Settings.from_flags()
  assert s.pictures_dir == str(tmp_path / "p") and s.port == 9191
  assert s.xmp_dry_run is True and s.nightly_scan_hour == -1
  assert fv.scan_dirs == ["2001", "2002"]


def test_command_line_wins_over_the_config_file(fv, tmp_path):
  path = write_config(tmp_path, 'port = 9191\nhost = "10.0.0.1"\n')
  fv(["prog", "--port=7000"])            # a real command line: the flag is now `present`
  config.apply_config_file(path)
  s = config.Settings.from_flags()
  assert s.port == 7000 and s.host == "10.0.0.1"


def test_unknown_key_and_wrong_type_are_clear_errors(fv, tmp_path):
  with pytest.raises(config.ConfigError, match="unknown setting 'pictues_dir'"):
    config.apply_config_file(write_config(tmp_path, 'pictues_dir = "/x"\n'))
  with pytest.raises(config.ConfigError, match="bad value for 'port'"):
    config.apply_config_file(write_config(tmp_path, 'port = "eighty"\n', "b.toml"))
  with pytest.raises(config.ConfigError, match="cannot read config file"):
    config.apply_config_file(write_config(tmp_path, "port = = 1\n", "c.toml"))
  with pytest.raises(config.ConfigError, match="unknown setting 'config'"):
    config.apply_config_file(write_config(tmp_path, 'config = "x"\n', "d.toml"))


def test_config_file_search_order(tmp_path, monkeypatch):
  home, cwd = tmp_path / "home", tmp_path / "cwd"
  (home / ".config" / "photos").mkdir(parents=True)
  cwd.mkdir()
  monkeypatch.setenv("HOME", str(home))
  assert config.find_config_file(environ={}, cwd=str(cwd)) is None
  user = write_config(home / ".config" / "photos", "", "config.toml")
  assert config.find_config_file(environ={}, cwd=str(cwd)) == user
  local = write_config(cwd, "")
  assert config.find_config_file(environ={}, cwd=str(cwd)) == local
  env = write_config(tmp_path, "", "env.toml")
  assert config.find_config_file(environ={config.CONFIG_ENV: env}, cwd=str(cwd)) == env
  flag = write_config(tmp_path, "", "flag.toml")
  assert config.find_config_file(flag, environ={config.CONFIG_ENV: env}, cwd=str(cwd)) == flag
  assert config.find_config_file("none", environ={}, cwd=str(cwd)) is None
  with pytest.raises(config.ConfigError, match="does not exist"):
    config.find_config_file(str(tmp_path / "missing.toml"), environ={})


def test_example_config_is_valid_and_documents_every_setting(fv):
  path = os.path.join(config.REPO_DIR, "photos.example.toml")
  config.apply_config_file(path)      # every key in it is a real flag of the right type
  text = open(path).read()
  for name in ("pictures_dir", "thumbs_dir", "state_dir", "database_path", "hashes_db", "host", "port"):
    assert name in text


def test_load_exits_with_a_message_when_pictures_dir_is_missing(fv, tmp_path, capsys):
  fv.config = "none"
  fv.pictures_dir = str(tmp_path / "missing")
  with pytest.raises(SystemExit) as e:
    config.Settings.load()
  assert "is not a directory" in str(e.value)
