"""Command line flags of the photo manager app."""

import dataclasses
import os
import sys
import tomllib

from absl import flags

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_ENV = "PHOTOS_CONFIG"


def default_state_dir():
  data_home = os.environ.get("XDG_DATA_HOME") or os.path.join("~", ".local", "share")
  return os.path.join(data_home, "photos")


flags.DEFINE_string(
    "config", None,
    "Config file (TOML; the keys are the flag names, see photos.example.toml). Default: "
    f"${CONFIG_ENV}, else ./photos.toml, else ~/.config/photos/config.toml, else none. Values given "
    "on the command line win over the file, the file over the built-in defaults. 'none' "
    "disables the search.")
flags.DEFINE_string(
    "pictures_dir", "~/Pictures",
    "Photo library root. Read-only for the app, except XMP sidecars.")
flags.DEFINE_string(
    "thumbs_dir", None,
    "Thumbnail cache root, with Thumb/Small/Medium/Huge/Tuned subdirectories. "
    "Default: <state_dir>/thumbs.")
flags.DEFINE_string(
    "state_dir", default_state_dir(),
    "Directory for the sqlite database, XMP backups and manual_links.jsonl.")
flags.DEFINE_string(
    "database_path", None,
    "Path of the sqlite database. Default: <state_dir>/app.sqlite.")
flags.DEFINE_string(
    "hashes_db", None,
    "Optional tools/archive/catalog.py-format database whose hashes are reused by "
    "scans while a file's mtime matches.")
flags.DEFINE_boolean(
    "xmp_dry_run", False,
    "Compute and log XMP changes without writing any sidecar or database "
    "change. Use for a first pass on the real library.")
flags.DEFINE_enum(
    "new_raw_sidecar_style", "full", ["full", "stem"],
    "Naming of a NEW sidecar for a RAW original: 'full' = NAME.DNG.xmp (what "
    "darktable and 15427 of 15429 existing sidecars use), 'stem' = NAME.xmp. "
    "Existing sidecars are always edited in place. See ticket 042.")
flags.DEFINE_string(
    "gemini_api_key_file", None,
    "File holding the Google API key used for AI rating (ticket 174). Default: "
    "<state_dir>/gemini_api_key. $GEMINI_API_KEY wins over the file. There is deliberately no "
    "flag for the key itself (it would show up in `ps`); chmod 600 the file.")
flags.DEFINE_string(
    "ai_model", "gemini-flash-lite-3.5",
    "Gemini model that rates photos (ticket 174). Part of the response cache key, so changing it "
    "re-rates pictures.")
flags.DEFINE_integer("job_workers", 2, "Background worker threads (RAW renders).")
flags.DEFINE_integer("nightly_scan_hour", 3, "Local hour (0-23) of the nightly rescan; -1 disables it.")
flags.DEFINE_integer("scan_workers", 8, "Threads reading files during a scan (network file systems are latency bound).")
flags.DEFINE_boolean(
    "write_metadata_json", True,
    "Scans write a per-directory index.json and a per-file <name>.json metadata cache into the "
    "library and reuse them on later scans, so an unchanged directory or file need not be "
    "decoded again (ticket 111). On by default; it writes into the picture directories, so pass "
    "--nowrite_metadata_json to turn it off.")
flags.DEFINE_boolean(
    "one_star_is_unrated", False,
    "Display-only: treat a 1-star rating as unrated (darktable's default on import "
    "is 1 star, 76% of this library's sidecars). Hides the 1-star badge, makes "
    "'picked' mean rating >= 2 and 'unrated' include 1. Nothing on disk changes. "
    "See ticket 049.")
flags.DEFINE_float(
    "busy_retry_seconds", 60.0,
    "When the database is locked by another writer (a running scan, a second instance), "
    "the web app retries with growing waits for about this long, then answers with an "
    "error ('database busy') that the page shows as a message.")
flags.DEFINE_string("host", "0.0.0.0", "Address to listen on (LAN only).")
flags.DEFINE_integer("port", 8080, "Port to listen on.")
flags.DEFINE_boolean(
    "load_worker_enabled", True,
    "Run a low-priority background worker (nice/ionice, ticket 066) that fills in missing "
    "thumbnails automatically whenever the machine looks idle. See ticket 073.")
flags.DEFINE_integer(
    "load_check_seconds", 30, "How often the load-adaptive worker samples CPU/memory load.")
flags.DEFINE_float(
    "load_start_threshold", 0.5,
    "1-minute load average at or below which the background worker is allowed to start.")
flags.DEFINE_float(
    "load_stop_threshold", 1.5,
    "1-minute load average above which the background worker is stopped. Higher than "
    "--load_start_threshold on purpose, to avoid starting/stopping repeatedly right at one "
    "boundary.")
flags.DEFINE_float(
    "mem_start_percent", 20.0,
    "Percentage of RAM that must be available for the background worker to start.")
flags.DEFINE_float(
    "mem_stop_percent", 10.0,
    "Percentage of RAM available below which the background worker is stopped. These memory "
    "thresholds are a first guess (ticket 073) -- every sample is logged at vlog(3) so they can "
    "be tuned from real observation.")


class ConfigError(Exception):
  pass


def _expand(path):
  """Absolute path with ~ and $VARS expanded (None stays None)."""
  return os.path.abspath(os.path.expandvars(os.path.expanduser(path))) if path else None


def find_config_file(explicit=None, environ=None, cwd=None):
  """The config file to use, or None. An explicitly named file must exist."""
  environ = os.environ if environ is None else environ
  named = explicit or environ.get(CONFIG_ENV)
  if named:
    if named == "none":
      return None
    path = _expand(named)
    if not os.path.isfile(path):
      raise ConfigError(f"config file {path} does not exist")
    return path
  for path in (os.path.join(cwd or os.getcwd(), "photos.toml"),
               os.path.join(os.path.expanduser("~"), ".config", "photos", "config.toml")):
    if os.path.isfile(path):
      return path
  return None


def apply_config_file(path, flag_values=None):
  """Make the values of a TOML config file the defaults of the flags.

  A flag given on the command line keeps its command-line value. Unknown keys and values of the wrong
  type raise ConfigError.
  """
  fv = flag_values or flags.FLAGS
  try:
    with open(path, "rb") as f:
      table = tomllib.load(f)
  except (OSError, tomllib.TOMLDecodeError) as e:
    raise ConfigError(f"cannot read config file {path}: {e}") from e
  for key, value in table.items():
    if key == "config" or key not in fv:
      raise ConfigError(f"{path}: unknown setting '{key}' (keys are the flag names, "
                        "see photos.example.toml)")
    try:
      fv.set_default(key, value)
    except (flags.IllegalFlagValueError, ValueError, TypeError) as e:
      raise ConfigError(f"{path}: bad value for '{key}': {value!r} ({e})") from e


@dataclasses.dataclass
class Settings:
  pictures_dir: str
  thumbs_dir: str
  state_dir: str
  hashes_db: str = None
  database_path: str = None
  host: str = "0.0.0.0"
  port: int = 8080
  config_file: str = None
  xmp_dry_run: bool = False
  new_raw_sidecar_style: str = "full"
  job_workers: int = 2
  nightly_scan_hour: int = -1
  scan_workers: int = 8
  write_metadata_json: bool = True
  busy_retry_seconds: float = 60.0
  one_star_is_unrated: bool = False
  load_worker_enabled: bool = True
  load_check_seconds: int = 30
  load_start_threshold: float = 0.5
  load_stop_threshold: float = 1.5
  mem_start_percent: float = 20.0
  mem_stop_percent: float = 10.0
  gemini_api_key_file: str = None
  ai_model: str = "gemini-flash-lite-3.5"

  @property
  def db_path(self):
    return self.database_path or os.path.join(self.state_dir, "app.sqlite")

  def gemini_api_key(self, environ=None):
    """The Google API key for AI rating (ticket 174): $GEMINI_API_KEY, else the key file (default
    <state_dir>/gemini_api_key), stripped; None when neither gives one. Never logs the key."""
    environ = os.environ if environ is None else environ
    key = (environ.get("GEMINI_API_KEY") or "").strip()
    if key:
      return key
    path = self.gemini_api_key_file or os.path.join(self.state_dir, "gemini_api_key")
    try:
      with open(path) as f:
        return f.read().strip() or None
    except OSError:
      return None

  def check_pictures_dir(self):
    if not os.path.isdir(self.pictures_dir):
      raise ConfigError(
          f"pictures_dir {self.pictures_dir} is not a directory; set pictures_dir in the config "
          "file or pass --pictures_dir=... (see docs/install.md)")

  @classmethod
  def load(cls, check_pictures_dir=True):
    """Settings for an entry point: config file, then flags; exits with a message on a bad setup."""
    f = flags.FLAGS
    try:
      path = find_config_file(f.config)
      if path:
        apply_config_file(path)
      settings = cls.from_flags()
      if check_pictures_dir:
        settings.check_pictures_dir()
    except ConfigError as e:
      sys.exit(f"photos: {e}")
    settings.config_file = path
    return settings

  @classmethod
  def from_flags(cls):
    f = flags.FLAGS
    state_dir = _expand(f.state_dir)
    return cls(
        pictures_dir=_expand(f.pictures_dir),
        thumbs_dir=_expand(f.thumbs_dir) or os.path.join(state_dir, "thumbs"),
        state_dir=state_dir, hashes_db=_expand(f.hashes_db), database_path=_expand(f.database_path),
        host=f.host, port=f.port,
        xmp_dry_run=f.xmp_dry_run, new_raw_sidecar_style=f.new_raw_sidecar_style,
        job_workers=f.job_workers, nightly_scan_hour=f.nightly_scan_hour,
        scan_workers=f.scan_workers, busy_retry_seconds=f.busy_retry_seconds,
        write_metadata_json=f.write_metadata_json,
        one_star_is_unrated=f.one_star_is_unrated,
        load_worker_enabled=f.load_worker_enabled,
        load_check_seconds=f.load_check_seconds,
        load_start_threshold=f.load_start_threshold,
        load_stop_threshold=f.load_stop_threshold,
        mem_start_percent=f.mem_start_percent, mem_stop_percent=f.mem_stop_percent,
        gemini_api_key_file=_expand(f.gemini_api_key_file), ai_model=f.ai_model)
