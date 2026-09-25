"""Command line flags of the photo manager app."""

import dataclasses
import os

from absl import flags

flags.DEFINE_string(
    "pictures_dir", "/zoo/Pictures",
    "Photo library root. Read-only for the app, except XMP sidecars.")
flags.DEFINE_string(
    "thumbs_dir", "/zoo/Thumbs",
    "Thumbnail cache root, with Thumb/Small/Medium/Huge/Tuned subdirectories.")
flags.DEFINE_string(
    "state_dir", os.path.expanduser("~/.local/share/photos"),
    "Directory for the sqlite database, XMP backups and manual_links.jsonl.")
flags.DEFINE_string(
    "hashes_db", None,
    "Optional hash_dir.py-format database whose hashes are reused by scans "
    "while a file's mtime matches.")
flags.DEFINE_boolean(
    "xmp_dry_run", False,
    "Compute and log XMP changes without writing any sidecar or database "
    "change. Use for a first pass on the real library.")
flags.DEFINE_enum(
    "new_raw_sidecar_style", "full", ["full", "stem"],
    "Naming of a NEW sidecar for a RAW original: 'full' = NAME.DNG.xmp (what "
    "darktable and 15427 of 15429 existing sidecars use), 'stem' = NAME.xmp. "
    "Existing sidecars are always edited in place. See ticket 042.")
flags.DEFINE_integer("job_workers", 2, "Background worker threads (RAW renders).")
flags.DEFINE_integer("nightly_scan_hour", 3, "Local hour (0-23) of the nightly rescan; -1 disables it.")
flags.DEFINE_integer("scan_workers", 8, "Threads reading files during a scan (network file systems are latency bound).")
flags.DEFINE_boolean(
    "write_metadata_json", False,
    "Scans write a per-directory index.json and a per-file <name>.json metadata cache into the "
    "library and reuse them on later scans, so an unchanged directory or file need not be "
    "decoded again (ticket 111). Off by default: it writes into the picture directories.")
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


@dataclasses.dataclass
class Settings:
  pictures_dir: str
  thumbs_dir: str
  state_dir: str
  hashes_db: str = None
  xmp_dry_run: bool = False
  new_raw_sidecar_style: str = "full"
  job_workers: int = 2
  nightly_scan_hour: int = -1
  scan_workers: int = 8
  write_metadata_json: bool = False
  busy_retry_seconds: float = 60.0
  one_star_is_unrated: bool = False
  load_worker_enabled: bool = True
  load_check_seconds: int = 30
  load_start_threshold: float = 0.5
  load_stop_threshold: float = 1.5
  mem_start_percent: float = 20.0
  mem_stop_percent: float = 10.0

  @property
  def db_path(self):
    return os.path.join(self.state_dir, "app.sqlite")

  @classmethod
  def from_flags(cls):
    f = flags.FLAGS
    return cls(
        pictures_dir=os.path.abspath(f.pictures_dir),
        thumbs_dir=os.path.abspath(f.thumbs_dir),
        state_dir=os.path.abspath(f.state_dir), hashes_db=f.hashes_db,
        xmp_dry_run=f.xmp_dry_run, new_raw_sidecar_style=f.new_raw_sidecar_style,
        job_workers=f.job_workers, nightly_scan_hour=f.nightly_scan_hour,
        scan_workers=f.scan_workers, busy_retry_seconds=f.busy_retry_seconds,
        write_metadata_json=f.write_metadata_json,
        one_star_is_unrated=f.one_star_is_unrated,
        load_worker_enabled=f.load_worker_enabled,
        load_check_seconds=f.load_check_seconds,
        load_start_threshold=f.load_start_threshold,
        load_stop_threshold=f.load_stop_threshold,
        mem_start_percent=f.mem_start_percent, mem_stop_percent=f.mem_stop_percent)
