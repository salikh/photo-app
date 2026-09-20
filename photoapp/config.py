"""Command line flags of the photo manager app."""

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
flags.DEFINE_string("host", "0.0.0.0", "Address to listen on (LAN only).")
flags.DEFINE_integer("port", 8080, "Port to listen on.")
