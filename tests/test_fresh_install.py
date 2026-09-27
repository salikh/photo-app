"""Integration tests for a brand-new installation (ticket 130).

Each test starts the real server (`python -m photoapp`, or `./start.sh` in a copy of the checkout) as a
subprocess in a pristine environment: an empty HOME, no XDG_DATA_HOME, no config file, a working
directory that is not the checkout. Nothing of the developer's own setup can leak in.
"""

import ast
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from photoapp import config
from tests.conftest import make_jpeg

REPO = config.REPO_DIR
PYTHON_MODULES_OF = {   # import name -> distribution named in requirements*.txt
    "absl": "absl-py", "PIL": "pillow", "lxml": "lxml", "fastapi": "fastapi", "uvicorn": "uvicorn",
    "numpy": "numpy", "rawpy": "rawpy", "tifffile": "tifffile", "pytest": "pytest", "httpx": "httpx",
    "playwright": "playwright",
    # installed as dependencies of fastapi; importing them directly is fine
    "pydantic": "fastapi", "starlette": "fastapi"}


def free_port():
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


class Install:
  """A pristine machine: empty HOME, a photo library in ~/Pictures, an empty working directory."""

  def __init__(self, tmp_path, photos=3):
    self.home = tmp_path / "home"
    self.cwd = tmp_path / "cwd"
    self.home.mkdir()
    self.cwd.mkdir()
    self.pictures = self.home / "Pictures"
    for i in range(photos):
      make_jpeg(str(self.pictures / "2024" / f"IMG_{i}.jpg"), size=(1200, 800),
                color=("red", "green", "blue", "yellow")[i % 4])
    self.env = {k: v for k, v in os.environ.items()
                if not k.startswith(("PHOTOS_", "XDG_", "VIRTUAL_ENV", "PYTHONPATH"))}
    self.env.update(HOME=str(self.home), PYTHONPATH=REPO)
    self.port = free_port()
    self.proc = None
    self.log = tmp_path / "server.log"

  @property
  def url(self):
    return f"http://127.0.0.1:{self.port}"

  def run(self, module, *args, cwd=None, timeout=120):
    return subprocess.run(
        [sys.executable, "-m", module, "--logtostderr", *args], env=self.env,
        cwd=cwd or self.cwd, capture_output=True, text=True, timeout=timeout)

  def start(self, *args, command=None, cwd=None, wait=True, timeout=60):
    command = command or [sys.executable, "-m", "photoapp"]
    with open(self.log, "w") as log:
      self.proc = subprocess.Popen(
          [*command, "--logtostderr", f"--port={self.port}", "--host=127.0.0.1", *args],
          env=self.env, cwd=cwd or self.cwd, stdout=log, stderr=subprocess.STDOUT)
    if wait:
      self.wait_healthy(timeout)
    return self

  def wait_healthy(self, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
      if self.proc.poll() is not None:
        pytest.fail(f"server exited with {self.proc.returncode}:\n{self.log.read_text()[-3000:]}")
      try:
        if self.get("/api/health") == {"ok": True}:
          return
      except (urllib.error.URLError, ConnectionError, OSError):
        pass
      time.sleep(0.2)
    pytest.fail(f"server did not become healthy:\n{self.log.read_text()[-3000:]}")

  def get(self, path, raw=False):
    with urllib.request.urlopen(self.url + path, timeout=30) as r:
      body = r.read()
      return (r.status, r.headers, body) if raw else json.loads(body)

  def post(self, path, payload):
    req = urllib.request.Request(
        self.url + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
      return json.loads(r.read())

  def wait_scanned(self, count, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
      status = self.get("/api/scan/status")
      photos = self.get("/api/photos?recursive=true")
      if not status["running"] and photos["total"] >= count:
        return photos
      time.sleep(0.2)
    pytest.fail(f"library was not scanned: {self.get('/api/scan/status')}\n"
                f"{self.log.read_text()[-3000:]}")

  def stop(self):
    if self.proc and self.proc.poll() is None:
      self.proc.send_signal(signal.SIGINT)
      try:
        self.proc.wait(15)
      except subprocess.TimeoutExpired:
        self.proc.kill()
        self.proc.wait()


@pytest.fixture
def install(tmp_path):
  i = Install(tmp_path)
  yield i
  i.stop()


def tree(root):
  return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)


def test_first_start_with_no_configuration_at_all(install):
  """No config file, no flags but the port: the library in ~/Pictures is found, scanned and served."""
  install.start()
  photos = install.wait_scanned(3)
  assert photos["total"] == 3

  # state lands in the XDG default, thumbnails under it, nothing in the checkout or the cwd
  state = install.home / ".local" / "share" / "photos"
  assert (state / "app.sqlite").is_file()
  assert install.get("/api/config") == {"one_star_is_unrated": False, "xmp_dry_run": False}
  assert tree(install.cwd) == []

  # a photo can be shown: the thumbnail is made in the default thumbs dir
  status, headers, body = install.get(f"/img/Thumb/{photos['photos'][0]['file_id']}", raw=True)
  assert status == 200 and headers["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
  assert any(f.startswith("Thumb/") for f in tree(state / "thumbs"))
  assert "config file: none" in install.log.read_text()
  assert "Photos: http://127.0.0.1:" in install.log.read_text()


def test_rating_a_photo_on_a_fresh_install_writes_a_sidecar_next_to_it(install):
  install.start()
  photos = install.wait_scanned(3)
  first = photos["photos"][0]
  install.post(f"/api/photos/{first['id']}/rating", {"rating": 4})
  sidecars = [f for f in tree(install.pictures) if f.endswith(".xmp")]
  assert len(sidecars) == 1
  assert 'xmp:Rating="4"' in (install.pictures / sidecars[0]).read_text()
  # the first-seen backup lives in the state dir, not in the library
  assert not any("backup" in f for f in tree(install.pictures))


def test_second_start_keeps_the_data_and_does_not_rescan_on_start(install):
  install.start()
  photos = install.wait_scanned(3)
  install.post(f"/api/photos/{photos['photos'][0]['id']}/rating", {"rating": 5})
  install.stop()
  install.start()
  assert "empty database" not in install.log.read_text()
  again = install.get("/api/photos?recursive=true")
  assert again["total"] == 3 and 5 in [p["rating"] for p in again["photos"]]


def test_config_file_in_the_working_directory_moves_everything(install, tmp_path):
  data = tmp_path / "elsewhere"
  (install.cwd / "photos.toml").write_text(f'''
pictures_dir = "{install.pictures}"
thumbs_dir = "{data}/cache"
state_dir = "{data}/state"
database_path = "{data}/db/my.sqlite"
nightly_scan_hour = -1
one_star_is_unrated = true
''')
  install.start()
  photos = install.wait_scanned(3)
  assert install.get("/api/config")["one_star_is_unrated"] is True
  status, _, _ = install.get(f"/img/Thumb/{photos['photos'][0]['file_id']}", raw=True)
  assert status == 200
  assert (data / "db" / "my.sqlite").is_file()
  assert not (data / "state" / "app.sqlite").exists()
  assert any(f.startswith("Thumb/") for f in tree(data / "cache"))
  assert not (install.home / ".local").exists()          # nothing in the default location
  assert f"config file: {install.cwd}/photos.toml" in install.log.read_text()


def test_config_file_from_env_flag_and_precedence(install, tmp_path):
  other = tmp_path / "lib2"
  make_jpeg(str(other / "only.jpg"))
  from_env = tmp_path / "env.toml"
  from_env.write_text(f'pictures_dir = "{other}"\nstate_dir = "{tmp_path}/s1"\n')
  install.env["PHOTOS_CONFIG"] = str(from_env)
  install.start()
  assert install.wait_scanned(1)["total"] == 1                       # the file named by $PHOTOS_CONFIG
  install.stop()

  install.start(f"--pictures_dir={install.pictures}", f"--state_dir={tmp_path}/s2")
  assert install.wait_scanned(3)["total"] == 3                       # the command line wins
  install.stop()

  from_flag = tmp_path / "flag.toml"
  from_flag.write_text(f'pictures_dir = "{install.pictures}"\nstate_dir = "{tmp_path}/s3"\n')
  install.start(f"--config={from_flag}")
  assert install.wait_scanned(3)["total"] == 3                       # --config wins over $PHOTOS_CONFIG
  assert (tmp_path / "s3" / "app.sqlite").is_file()


@pytest.mark.parametrize("toml, message", [
    ('pictures_dir = "{missing}"\n', "is not a directory"),
    ('pictues_dir = "/x"\n', "unknown setting 'pictues_dir'"),
    ('port = "eighty"\n', "bad value for 'port'"),
    ("this is not toml\n", "cannot read config file"),
])
def test_bad_configuration_stops_with_a_clear_message(install, tmp_path, toml, message):
  bad = tmp_path / "bad.toml"
  bad.write_text(toml.format(missing=tmp_path / "missing"))
  r = install.run("photoapp", f"--config={bad}")
  assert r.returncode != 0 and message in r.stderr and "Traceback" not in r.stderr
  assert not (install.home / ".local").exists()          # failed before creating anything


def test_missing_pictures_dir_and_missing_config_file_are_reported(install, tmp_path):
  shutil.rmtree(install.pictures)
  r = install.run("photoapp")
  assert r.returncode != 0 and "pictures_dir" in r.stderr and "install.md" in r.stderr
  r = install.run("photoapp", f"--config={tmp_path}/missing.toml")
  assert r.returncode != 0 and "does not exist" in r.stderr


def test_command_line_tools_use_the_same_configuration(install, tmp_path):
  (install.cwd / "photos.toml").write_text(
      f'pictures_dir = "{install.pictures}"\nstate_dir = "{tmp_path}/state"\n'
      f'thumbs_dir = "{tmp_path}/thumbs"\n')
  r = install.run("photoapp.fullscan")
  assert r.returncode == 0, r.stderr[-2000:]
  assert (tmp_path / "state" / "app.sqlite").is_file()
  r = install.run("photoapp.populate_thumbs", "--sizes=Thumb")
  assert r.returncode == 0, r.stderr[-2000:]
  assert len([f for f in tree(tmp_path / "thumbs") if f.startswith("Thumb/")]) == 3


def imports_of(paths):
  found = {}
  for path in paths:
    for node in ast.walk(ast.parse(open(path).read(), path)):
      names = ([a.name for a in node.names] if isinstance(node, ast.Import)
               else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
      for name in names:
        found.setdefault(name.split(".")[0], path)
  return {m: p for m, p in found.items()
          if m not in sys.stdlib_module_names
          # "photoapp"/"tests": this repo's own packages. "catalog_lib": tools/archive/'s own
          # flag-free module (ticket 137), imported directly by its tests via a sys.path append,
          # not installed as a package -- not a third-party dependency either way.
          and m not in ("photoapp", "tests", "catalog_lib")}


def requirement_names(filename, seen=()):
  names = set()
  for line in open(os.path.join(REPO, filename)):
    line = line.split("#")[0].strip()
    if line.startswith("-r "):
      names |= requirement_names(line[3:].strip())
    elif line:
      names.add(re.split(r"[<>=!~\[; ]", line)[0].lower())
  return names


def py_files(root):
  return [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(REPO, root))
          if "static" not in d and "__pycache__" not in d for f in fs if f.endswith(".py")]


def test_requirements_cover_every_import_of_the_app():
  needed = imports_of(py_files("photoapp"))
  unknown = set(needed) - set(PYTHON_MODULES_OF)
  assert not unknown, f"unmapped third-party imports {unknown}: add them to requirements.txt and this test"
  runtime = requirement_names("requirements.txt")
  missing = {m: needed[m] for m in needed if PYTHON_MODULES_OF[m] not in runtime}
  assert not missing, f"not in requirements.txt: {missing}"
  # the test-only packages stay out of the runtime install
  assert not runtime & {"pytest", "httpx", "playwright"}


def test_dev_requirements_cover_every_import_of_the_tests():
  needed = imports_of(py_files("tests"))
  dev = requirement_names("requirements-dev.txt")
  missing = {m: needed[m] for m in needed if PYTHON_MODULES_OF.get(m) not in dev}
  assert not missing, f"not in requirements-dev.txt: {missing}"


def test_every_requirement_has_a_minimum_version():
  for filename in ("requirements.txt", "requirements-dev.txt"):
    for line in open(os.path.join(REPO, filename)):
      line = line.split("#")[0].strip()
      if line and not line.startswith("-r"):
        assert ">=" in line, f"{filename}: '{line}' has no minimum version"


def checkout(tmp_path):
  """A copy of the tracked files, like a fresh `git clone`."""
  dest = tmp_path / "checkout"
  dest.mkdir()
  tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True)
  for rel in tracked.stdout.decode().split("\0"):
    if rel and os.path.exists(os.path.join(REPO, rel)):
      os.makedirs(os.path.dirname(dest / rel), exist_ok=True)
      shutil.copy2(os.path.join(REPO, rel), dest / rel)
  return dest


def test_start_sh_refuses_an_old_python_before_creating_anything(tmp_path):
  if not shutil.which("git"):
    pytest.skip("git not available")
  dest = checkout(tmp_path)
  fake = tmp_path / "python3.9"
  fake.write_text("#!/bin/sh\nexit 1\n")       # `python -c 'sys.exit(version < 3.11)'` fails
  fake.chmod(0o755)
  r = subprocess.run(["./start.sh"], cwd=dest, capture_output=True, text=True,
                     env={**os.environ, "PYTHON": str(fake), "HOME": str(tmp_path)})
  assert r.returncode != 0 and "Python 3.11 or newer" in r.stderr
  assert not (dest / ".venv").exists()


@pytest.mark.skipif(not os.environ.get("PHOTOS_TEST_INSTALL"),
                    reason="downloads packages; set PHOTOS_TEST_INSTALL=1")
def test_start_sh_builds_a_new_venv_from_requirements_and_serves_a_library(tmp_path):
  """The whole install path: a copy of the tracked files, no .venv, ./start.sh, a browsable library."""
  dest = checkout(tmp_path)
  assert not (dest / ".venv").exists()
  i = Install(tmp_path)
  i.env.pop("PYTHONPATH")        # the checkout's own code and its own venv, nothing of ours
  (dest / "photos.toml").write_text(
      f'pictures_dir = "{i.pictures}"\nstate_dir = "{tmp_path}/state"\n')
  i.start(command=["./start.sh"], cwd=dest, timeout=600)
  try:
    photos = i.wait_scanned(3)
    status, _, body = i.get(f"/img/Thumb/{photos['photos'][0]['file_id']}", raw=True)
    assert status == 200 and body[:2] == b"\xff\xd8"
    assert (dest / ".venv" / "bin" / "python").is_file()
    assert (tmp_path / "state" / "app.sqlite").is_file()
    assert "Photos: http://127.0.0.1:" in i.log.read_text()
    page = i.get("/", raw=True)[2].decode()
    assert "Photos" in page
  finally:
    i.stop()
  # the venv holds exactly the runtime requirements: no test packages
  freeze = subprocess.run([str(dest / ".venv" / "bin" / "pip"), "freeze"], capture_output=True,
                          text=True).stdout.lower()
  assert "fastapi" in freeze and "rawpy" in freeze and "pytest" not in freeze
