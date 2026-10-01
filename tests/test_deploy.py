"""Ticket 183: the pure helpers behind deploy.sh and a syntax check of the script itself."""
import importlib.util
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deploy_helper", ROOT / "deploy" / "helper.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_env_text_converts_export_lines_and_drops_placeholders():
  text = 'export GEMINI_API_KEY="abc 123"\nexport OTHER=x\n# c\nexport K="<Put your key here>"\n'
  assert helper.env_text(text) == "GEMINI_API_KEY=abc 123\nOTHER=x\n"


def test_mounts_from_config(tmp_path):
  cfg = tmp_path / "c.toml"
  cfg.write_text('pictures_dir = "/zoo/Pictures"\nthumbs_dir = "/zoo/Thumbs"\n')
  assert helper.mounts(str(cfg)) == "RequiresMountsFor=/zoo/Pictures /zoo/Thumbs"
  assert helper.mounts(str(tmp_path / "missing.toml")) == ""


def test_listen_args_override_config(tmp_path):
  cfg = tmp_path / "c.toml"
  cfg.write_text('port = 9000\n')
  assert helper.listen(str(cfg), "PHOTOS_ARGS=--host 10.0.0.1 --port=8081 --v=5") == ("10.0.0.1", 8081)
  assert helper.listen(str(cfg), "") == ("0.0.0.0", 9000)


def test_render_fills_placeholders_and_rejects_leftovers():
  assert helper.render("a @X@ b", {"X": "1"}) == "a 1 b"
  with pytest.raises(SystemExit):
    helper.render("a @X@ @Y@", {"X": "1"})


def test_unit_template_renders_completely():
  text = (ROOT / "deploy" / "photos.service.in").read_text()
  out = helper.render(text, {"USER": "u", "INSTALL": "/opt/photos", "CONFDIR": "/h/.config/photos", "MOUNTS": ""})
  assert "User=u" in out and "ExecStart=/opt/photos/.venv/bin/python -m photoapp" in out


def test_deploy_sh_parses_and_refuses_unknown_args():
  assert subprocess.run(["sh", "-n", str(ROOT / "deploy.sh")]).returncode == 0
  r = subprocess.run(["sh", str(ROOT / "deploy.sh"), "--bogus"], capture_output=True, text=True)
  assert r.returncode == 2 and "unknown argument" in r.stderr
