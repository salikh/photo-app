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

  @property
  def db_path(self):
    return os.path.join(self.state_dir, "app.sqlite")

  @classmethod
  def from_flags(cls):
    f = flags.FLAGS
    return cls(os.path.abspath(f.pictures_dir), os.path.abspath(f.thumbs_dir),
               os.path.abspath(f.state_dir), f.hashes_db, f.xmp_dry_run,
               f.new_raw_sidecar_style, f.job_workers, f.nightly_scan_hour)
