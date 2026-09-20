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
flags.DEFINE_string("host", "0.0.0.0", "Address to listen on (LAN only).")
flags.DEFINE_integer("port", 8080, "Port to listen on.")


@dataclasses.dataclass
class Settings:
  pictures_dir: str
  thumbs_dir: str
  state_dir: str
  hashes_db: str = None

  @property
  def db_path(self):
    return os.path.join(self.state_dir, "app.sqlite")

  @classmethod
  def from_flags(cls):
    f = flags.FLAGS
    return cls(os.path.abspath(f.pictures_dir), os.path.abspath(f.thumbs_dir),
               os.path.abspath(f.state_dir), f.hashes_db)
