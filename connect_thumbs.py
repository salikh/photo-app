#!/usr/bin/env python3
"""Connect relocated thumbnails back to their original image's hash.

Reads two 'hashes'-format sqlite3 databases:
    --pictures_db   the photo library's own hash catalog (see hash_dir.py),
                     e.g. ~/zoo.db
    --thumbs_db     a hash catalog of the relocated thumbnail tree produced
                     by thumbs_move.py's generated script, e.g. a hash_dir.py
                     run over --output_dir from that tool

and writes a new 'thumbs' table into --output_db with one row per
matched thumbnail:

    orig_hash    hash of the original image's primary rendition
    thumb_hash   hash of the thumbnail file
    thumb_size   the thumbnail's size bucket, e.g. 'Thumb', 'Large', 'Huge'

A JPG and a DNG that sit next to each other with the same base name are
two renditions of the same logical image (see metadata_db.py); the DNG's
hash is used as orig_hash when both exist, since it's the raw master.

A thumbnail is connected to an original purely by matching base name
(filename without directory or extension) -- the thumbnail extraction
tool preserves the original's name but not necessarily its directory or
extension, and thumb_size (the top-level directory under --thumbs_db's
own root, e.g. 'Thumb' in 'Thumb/1998/ancient/foo.jpg') comes along for
free from that same relative path. Thumbnails whose base name matches no
original in --pictures_db are skipped and logged at V(1).

This tool only creates/replaces the 'thumbs' table in --output_db; any
other tables already in that database file are left alone.

Usage:
    connect_thumbs.py
        --pictures_db ~/zoo.db
        --thumbs_db ~/thumbs.db
        --output_db ~/thumb_links.db
"""

import os
import sqlite3
from collections import defaultdict

from absl import app
from absl import flags
from absl import logging

FLAGS = flags.FLAGS

# Kept in sync by hand with metadata_db.py's IMAGE_EXTENSIONS/
# MERGE_FORMAT_ORDER -- not imported from there since metadata_db.py is a
# standalone CLI script whose module-level required flags (--database,
# --root_dir) would otherwise leak into this tool's flag set.
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.dng', '.tif', '.tiff', '.heic', '.heif',
    '.cr2', '.cr3', '.nef', '.arw', '.raf', '.rw2', '.orf', '.gif',
    '.bmp', '.webp', '.pef',
}
# Preference order among same-base-name renditions: DNG (raw master) first.
MERGE_FORMAT_ORDER = {'.dng': 0, '.jpg': 1, '.jpeg': 1}

flags.DEFINE_string(
    "pictures_db", None,
    "Path to the photo library's own 'hashes' sqlite3 database.")
flags.DEFINE_string(
    "thumbs_db", None,
    "Path to a 'hashes' sqlite3 database for the relocated thumbnail tree.")
flags.DEFINE_string(
    "output_db", "thumb_links.db",
    "Path to the sqlite3 database to write the 'thumbs' table into.")
flags.mark_flag_as_required("pictures_db")
flags.mark_flag_as_required("thumbs_db")


def load_hashes(db_path):
    """Return [(filename, hash), ...] from a 'hashes' table."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT filename, hash FROM hashes").fetchall()
    finally:
        conn.close()


def base_name(path):
    """Return the file's base name, without directory or extension."""
    return os.path.splitext(os.path.basename(path))[0]


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def build_orig_hash_by_base_name(pictures_rows):
    """Return {base_name: hash} for the primary rendition of each image.

    When several genuine-image files share a base name (e.g. a DNG and a
    JPG rendition of the same shot), the one preferred by
    MERGE_FORMAT_ORDER (DNG first) is used; ties are broken alphabetically
    by filename for determinism.
    """
    candidates = defaultdict(list)
    for filename, hash_ in pictures_rows:
        if not is_image(filename):
            continue
        ext = os.path.splitext(filename)[1].lower()
        order = MERGE_FORMAT_ORDER.get(ext, 99)
        candidates[base_name(filename)].append((order, filename, hash_))

    orig_hash_by_base_name = {}
    for name, cands in candidates.items():
        cands.sort(key=lambda c: (c[0], c[1]))
        orig_hash_by_base_name[name] = cands[0][2]
    return orig_hash_by_base_name


def connect_thumbs(pictures_rows, thumbs_rows):
    """Return [(orig_hash, thumb_hash, thumb_size), ...] plus counts.

    Returns (rows, num_connected, num_unmatched).
    """
    orig_hash_by_base_name = build_orig_hash_by_base_name(pictures_rows)

    rows = []
    num_unmatched = 0
    for filename, thumb_hash in thumbs_rows:
        if not is_image(filename):
            continue
        parts = filename.split('/')
        if len(parts) < 2:
            logging.vlog(1, "No size component in thumb path: %s", filename)
            num_unmatched += 1
            continue
        thumb_size = parts[0]
        name = base_name(filename)
        orig_hash = orig_hash_by_base_name.get(name)
        if orig_hash is None:
            logging.vlog(1, "No original found for thumb %s (base name %s)",
                         filename, name)
            num_unmatched += 1
            continue
        logging.vlog(3, "Connected %s -> orig_hash=%s thumb_hash=%s size=%s",
                     filename, orig_hash, thumb_hash, thumb_size)
        rows.append((orig_hash, thumb_hash, thumb_size))

    return rows, len(rows), num_unmatched


def write_thumbs_table(output_db, rows):
    conn = sqlite3.connect(output_db)
    try:
        conn.execute("DROP TABLE IF EXISTS thumbs")
        conn.execute(
            "CREATE TABLE thumbs ("
            "orig_hash TEXT NOT NULL, "
            "thumb_hash TEXT NOT NULL, "
            "thumb_size TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO thumbs (orig_hash, thumb_hash, thumb_size) "
            "VALUES (?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def main(argv):
    if len(argv) != 1:
        raise app.UsageError(
            "This tool takes no positional arguments; use --pictures_db/"
            "--thumbs_db/--output_db instead (got: %s)" % argv[1:])

    pictures_rows = load_hashes(FLAGS.pictures_db)
    logging.info("loaded %d file rows from %s",
                 len(pictures_rows), FLAGS.pictures_db)
    thumbs_rows = load_hashes(FLAGS.thumbs_db)
    logging.info("loaded %d file rows from %s",
                 len(thumbs_rows), FLAGS.thumbs_db)

    rows, num_connected, num_unmatched = connect_thumbs(
        pictures_rows, thumbs_rows)

    write_thumbs_table(FLAGS.output_db, rows)

    logging.info("connected %d thumbnails, %d unmatched",
                 num_connected, num_unmatched)
    logging.info("wrote 'thumbs' table (%d rows) to %s",
                 len(rows), FLAGS.output_db)


if __name__ == "__main__":
    app.run(main)
