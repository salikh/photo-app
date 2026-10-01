import os

from photoapp import config


def test_api_key_env_wins_over_file(settings):
  os.makedirs(settings.state_dir)
  with open(os.path.join(settings.state_dir, "gemini_api_key"), "w") as f:
    f.write("filekey\n")
  assert settings.gemini_api_key(environ={}) == "filekey"          # file, stripped
  assert settings.gemini_api_key(environ={"GEMINI_API_KEY": " envkey "}) == "envkey"


def test_api_key_missing_is_none(settings):
  assert settings.gemini_api_key(environ={}) is None
  settings.gemini_api_key_file = os.path.join(settings.state_dir, "other")
  os.makedirs(settings.state_dir)
  open(settings.gemini_api_key_file, "w").write("\n")              # empty file
  assert settings.gemini_api_key(environ={}) is None


def test_default_model():
  assert config.Settings("p", "t", "s").ai_model == "gemini-3.5-flash-lite"
