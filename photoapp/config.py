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
  busy_retry_seconds: float = 60.0
  one_star_is_unrated: bool = False

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
        one_star_is_unrated=f.one_star_is_unrated)
