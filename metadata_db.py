#!/usr/bin/env python3
"""Build a metadata database for a photo library's hash catalog.

Reads the 'hashes' table of a sqlite3 image database (filename TEXT,
hash TEXT -- see compare.py/cleanup.py) and --root_dir (the directory on
disk that those filenames are relative to), then writes a NEW sqlite3
database with one row per logical image:

    hash          content hash of the chosen canonical copy
    filepath      canonical path of that copy (see ranking.py)
    rating        star rating, 0-5 (0 = unrated)
    reject        -1 / 0 / 1  (-1 = rejected, 0 = unmarked, 1 = picked)
    popularity    fine-grained duplication score (see below)
    copies        number of physical files that are copies of this image
    has_exported_copy   1 if any copy lives under an '/Exported/' path
    rating_source        'prod.db' / 'xmp' / NULL
    rating_source_path   the specific file the winning rating came from
    merged_paths  JSON list of every physical file merged into this image

Only genuine image files are considered (see IMAGE_EXTENSIONS below) --
sidecar files (.xmp, .json), catalogs (.lrcat), thumbnail caches
(Thumbs.db) and the tracking databases themselves (.prod.db) are never
treated as images, even though they show up as rows in 'hashes' too.

A '*.DNG' file and a '*.JPG' file that sit next to each other with the
same base name (e.g. 'foo/bar.DNG' and 'foo/bar.JPG') are treated as two
renditions of the SAME logical image: their metadata is merged into a
single output row (DNG preferred as the representative hash/filepath).

Metadata sources, in precedence order (highest wins):
    1. .prod.db 's `photos.rating`, for the DNG rendition
    2. .prod.db 's `photos.rating`, for the JPG rendition
    3. the DNG rendition's own '<file>.xmp' sidecar
    4. the JPG rendition's own '<file>.xmp' sidecar
EXCEPT: if ANY source reports a pick (a positive rating), the image is
always resolved as a pick, even if a higher-precedence source says it
was rejected or left unmarked -- a reject/unmarked verdict never
overrides an actual pick. Among multiple picks, the precedence order
above still decides whose star rating is used.
Disagreements between sources are logged to stderr as CONFLICT lines;
the resolved value (following the rule above) is still the one written
to the database.

Both sources use the same "combined" rating encoding (documented in
METADATA_SOURCES.md): a raw value in [-1, 5], where -1 means rejected
with no star rating, 0 means unmarked with no star rating, and a
positive value N means picked with an N-star rating.

See METADATA_SOURCES.md for the full write-up of this process.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

from ranking import rank

IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.dng', '.tif', '.tiff', '.heic', '.heif',
    '.cr2', '.cr3', '.nef', '.arw', '.raf', '.rw2', '.orf', '.gif',
    '.bmp', '.webp', '.pef',
}

# Extensions eligible for the "same base name -> same logical image" merge.
MERGE_EXTENSIONS = {'.dng', '.jpg', '.jpeg'}
# Preference order among merged renditions: DNG (raw master) first.
MERGE_FORMAT_ORDER = {'.dng': 0, '.jpg': 1, '.jpeg': 1}

XMP_RATING_NS = 'http://ns.adobe.com/xap/1.0/'
XMP_RATING_TAG = f'{{{XMP_RATING_NS}}}Rating'
XMP_RATING_RE = re.compile(rb'[Rr]ating[=>]\s*["\']?(-?\d+)')


def load_hashes(db_path):
    """Return dict: hash -> list of filenames, from the 'hashes' table."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute("SELECT hash, filename FROM hashes")
        hash_to_files = defaultdict(list)
        for hash_, filename in cur:
            hash_to_files[hash_].append(filename)
        return hash_to_files
    finally:
        conn.close()


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def merge_key(path):
    root, ext = os.path.splitext(path)
    if ext.lower() in MERGE_EXTENSIONS:
        return root
    return None


class Group:
    """One hash-group of exact-duplicate genuine-image files."""

    __slots__ = ('hash', 'files', 'canonical')

    def __init__(self, hash_, files):
        self.hash = hash_
        self.files = files
        self.canonical = rank(files)[0]

    @property
    def ext(self):
        return os.path.splitext(self.canonical)[1].lower()


def build_logical_images(hash_to_files):
    """Filter to genuine images, rank canonicals, and merge DNG/JPG pairs.

    Returns a list of "logical image" dicts:
        {
            'hash': representative hash,
            'filepath': representative canonical path,
            'copies': total physical file count across all merged groups,
            'has_exported_copy': bool,
            'merged_paths': [all physical file paths],
            'renditions': [Group, ...] sorted DNG-first, for rating lookup,
        }
    """
    groups = []
    for h, files in hash_to_files.items():
        image_files = [f for f in files if is_image(f)]
        if not image_files:
            continue
        groups.append(Group(h, image_files))

    by_key = defaultdict(list)
    standalone = []
    for g in groups:
        key = merge_key(g.canonical)
        if key is not None:
            by_key[key].append(g)
        else:
            standalone.append(g)

    logical_images = []
    for key, merged_groups in by_key.items():
        merged_groups.sort(key=lambda g: MERGE_FORMAT_ORDER.get(g.ext, 99))
        logical_images.append(_make_logical_image(merged_groups))
    for g in standalone:
        logical_images.append(_make_logical_image([g]))

    return logical_images


def _make_logical_image(renditions):
    primary = renditions[0]
    all_paths = [f for g in renditions for f in g.files]
    has_exported = any('Exported' in p.split('/') for p in all_paths)
    return {
        'hash': primary.hash,
        'filepath': primary.canonical,
        'copies': len(all_paths),
        'has_exported_copy': has_exported,
        'merged_paths': sorted(all_paths),
        'renditions': renditions,
    }


def interpret_combined_rating(value):
    """Map a combined [-1, 5] rating value to (star_rating, reject_status)."""
    if value is None:
        return None
    if value < 0:
        return (0, -1)
    if value == 0:
        return (0, 0)
    return (min(value, 5), 1)


class ProdDbSource:
    """One recognized '*.db' metadata source (see METADATA_SOURCES.md)."""

    def __init__(self, db_path, rel_path):
        self.db_path = db_path
        self.rel_path = rel_path  # path relative to root_dir, for reporting
        self.sha224_to_rating = {}

    @classmethod
    def try_load(cls, db_path, rel_path, log):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error as e:
            log(f"INFO: could not open {rel_path} as sqlite3 ({e}); skipping")
            return None
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'images', 'photos'} <= tables:
                log(f"INFO: {rel_path} does not have the expected "
                    f"images/photos schema; skipping")
                return None
            src = cls(db_path, rel_path)
            rows = conn.execute(
                "SELECT images.sha224, photos.rating "
                "FROM images JOIN photos ON images.id = photos.id "
                "WHERE images.sha224 IS NOT NULL AND photos.rating IS NOT NULL"
            )
            for sha224, rating in rows:
                src.sha224_to_rating[sha224] = rating
            return src
        except sqlite3.Error as e:
            log(f"INFO: {rel_path} does not look like a prod.db-style "
                f"database ({e}); skipping")
            return None
        finally:
            conn.close()


def find_prod_db_sources(hash_to_files, root_dir, log):
    """Locate and load every '*.db' file tracked in `hashes`."""
    db_files = sorted(f for f in hash_to_files_all_filenames(hash_to_files)
                       if f.lower().endswith('.db'))
    sources = []
    for rel_path in db_files:
        abs_path = os.path.join(root_dir, rel_path)
        if not os.path.isfile(abs_path):
            log(f"INFO: {rel_path} listed in hashes table but not found "
                f"under root_dir; skipping")
            continue
        src = ProdDbSource.try_load(abs_path, rel_path, log)
        if src is not None:
            log(f"INFO: loaded {len(src.sha224_to_rating)} ratings from "
                f"{rel_path}")
            sources.append(src)
    # Prefer sources that are NOT inside an old 'Exported/' backup: put
    # those first so they win ties in prod_db_rating() below.
    sources.sort(key=lambda s: ('Exported' in s.rel_path.split('/'), s.rel_path))
    return sources


def hash_to_files_all_filenames(hash_to_files):
    for files in hash_to_files.values():
        for f in files:
            yield f


def prod_db_rating(hash_, sources, log):
    """Look up a hash across all prod.db-style sources.

    Returns (value, source_path) for the highest-precedence source that
    has data, logging a CONFLICT if sources disagree.
    """
    found = [(s.rel_path, s.sha224_to_rating[hash_])
             for s in sources if hash_ in s.sha224_to_rating]
    if not found:
        return None
    values = {v for _, v in found}
    if len(values) > 1:
        log(f"CONFLICT: hash {hash_} has disagreeing prod.db ratings: "
            + ", ".join(f"{v} ({p})" for p, v in found))
    return found[0][1], found[0][0]


def xmp_rating(image_path, root_dir, log):
    """Look up the combined rating in '<image_path>.xmp', if it exists."""
    xmp_path = os.path.join(root_dir, image_path + '.xmp')
    if not os.path.isfile(xmp_path):
        alt = os.path.join(root_dir, image_path + '.XMP')
        if os.path.isfile(alt):
            xmp_path = alt
        else:
            return None
    try:
        with open(xmp_path, 'rb') as f:
            data = f.read()
    except OSError as e:
        log(f"INFO: could not read {xmp_path} ({e})")
        return None

    value = _parse_xmp_rating(data)
    if value is None:
        return None
    return value, os.path.relpath(xmp_path, root_dir)


def _parse_xmp_rating(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        root = None

    if root is not None:
        for desc in root.iter():
            if XMP_RATING_TAG in desc.attrib:
                try:
                    return int(desc.attrib[XMP_RATING_TAG])
                except ValueError:
                    pass
            child = desc.find(XMP_RATING_TAG)
            if child is not None and child.text is not None:
                try:
                    return int(child.text.strip())
                except ValueError:
                    pass

    # Fallback for malformed/non-namespaced XMP: a plain regex scan.
    m = XMP_RATING_RE.search(data)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def resolve_rating(image, prod_sources, root_dir, log):
    """Apply the rating precedence chain and return (rating, reject, source, source_path).

    Normally the highest-precedence source (see module docstring) wins.
    Exception: if ANY source reports a pick (a positive combined value),
    the merged image is always resolved as a pick -- a rejection or an
    unmarked status from a higher-precedence source never overrides an
    actual pick. Among multiple picks, the usual precedence order still
    decides which one's star rating is used.
    """
    candidates = []  # in precedence order
    for g in image['renditions']:  # already DNG-first
        r = prod_db_rating(g.hash, prod_sources, log)
        if r is not None:
            value, path = r
            candidates.append(('prod.db', value, path))
    for g in image['renditions']:
        r = xmp_rating(g.canonical, root_dir, log)
        if r is not None:
            value, path = r
            candidates.append(('xmp', value, path))

    if not candidates:
        return 0, 0, None, None

    unique_candidates = list(dict.fromkeys(candidates))
    pick_candidates = [c for c in unique_candidates if c[1] > 0]
    overridden_by_pick = bool(pick_candidates) and len(pick_candidates) < len(unique_candidates)
    pool = pick_candidates if pick_candidates else unique_candidates
    pool_values = {v for _, v, _ in pool}

    if overridden_by_pick:
        log(f"CONFLICT: {image['filepath']} sources disagree: "
            + ", ".join(f"{v} ({src}:{p})" for src, v, p in unique_candidates)
            + " -- resolved as a PICK because at least one source picked it")
    elif len(pool_values) > 1:
        log(f"CONFLICT: {image['filepath']} has disagreeing ratings across "
            "sources: " + ", ".join(
                f"{v} ({src}:{p})" for src, v, p in unique_candidates))

    source, value, path = pool[0]
    rating, reject = interpret_combined_rating(value)
    return rating, reject, source, path


def compute_popularity(image, exported_bonus):
    return image['copies'] + (exported_bonus if image['has_exported_copy'] else 0)


def build_metadata_db(image_db_path, root_dir, output_path, exported_bonus, log):
    hash_to_files = load_hashes(image_db_path)
    log(f"INFO: loaded {sum(len(v) for v in hash_to_files.values())} file "
        f"rows ({len(hash_to_files)} distinct hashes) from {image_db_path}")

    prod_sources = find_prod_db_sources(hash_to_files, root_dir, log)

    logical_images = build_logical_images(hash_to_files)
    log(f"INFO: {len(logical_images)} logical images after filtering to "
        f"genuine image extensions and merging DNG/JPG pairs")

    if os.path.exists(output_path):
        os.remove(output_path)
    conn = sqlite3.connect(output_path)
    conn.execute("""
        CREATE TABLE images (
            hash TEXT PRIMARY KEY,
            filepath TEXT NOT NULL,
            rating INTEGER NOT NULL,
            reject INTEGER NOT NULL,
            popularity INTEGER NOT NULL,
            copies INTEGER NOT NULL,
            has_exported_copy INTEGER NOT NULL,
            rating_source TEXT,
            rating_source_path TEXT,
            merged_paths TEXT NOT NULL
        )
    """)

    rating_counts = defaultdict(int)
    for image in logical_images:
        rating, reject, source, source_path = resolve_rating(
            image, prod_sources, root_dir, log)
        rating_counts[source] += 1
        popularity = compute_popularity(image, exported_bonus)
        conn.execute(
            "INSERT INTO images (hash, filepath, rating, reject, popularity, "
            "copies, has_exported_copy, rating_source, rating_source_path, "
            "merged_paths) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                image['hash'], image['filepath'], rating, reject, popularity,
                image['copies'], int(image['has_exported_copy']), source,
                source_path, json.dumps(image['merged_paths']),
            ),
        )
    conn.commit()
    conn.close()

    log(f"INFO: rating sources used: "
        + ", ".join(f"{k or 'none'}={v}" for k, v in sorted(
            rating_counts.items(), key=lambda kv: str(kv[0]))))
    log(f"INFO: wrote {len(logical_images)} rows to {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Build a metadata database (rating/reject/popularity) "
                    "for a photo library's hash catalog."
    )
    parser.add_argument("database", help="path to the sqlite3 image/hash database")
    parser.add_argument(
        "--root_dir", required=True,
        help="filesystem directory that filenames in the hash database are "
             "relative to (used to locate .xmp sidecars and '*.db' sources)",
    )
    parser.add_argument(
        "-o", "--output", default="metadata.db",
        help="path to write the new metadata sqlite3 database to "
             "(default: metadata.db)",
    )
    parser.add_argument(
        "--exported-bonus", type=int, default=1000,
        help="popularity bonus applied when a copy lives under an "
             "'/Exported/' path (default: 1000)",
    )
    args = parser.parse_args()

    def log(msg):
        print(msg, file=sys.stderr)

    build_metadata_db(args.database, args.root_dir, args.output,
                       args.exported_bonus, log)


if __name__ == "__main__":
    sys.exit(main())
