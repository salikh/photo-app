"""Shared canonical-copy ranking heuristic, used by cleanup.py and metadata_db.py.

Paths in the 'hashes' tables are stored relative to the photo library root,
which is itself the (implicit) 'Pictures' directory -- the literal string
'Pictures/' is therefore usually absent from the *start* of a path, but can
still show up deeper in a path that comes from an old, separately-rooted
backup (e.g. 'Exported/2018-05-04/Pictures/2001/...').

Ranking rules (higher score = more likely to be kept as canonical):
  1. Major bonus: the first path component right under the (real or
     implicit) 'Pictures' root is a 4-digit year directory, e.g.
     'Pictures/2020/...' or a bare 'YYYY/...' at the start of the path.
     'Pictures/2002-CA/' (and bare '2002-CA/...') is special-cased to
     count the same as a year directory.
  2. Minor bonus: there is exactly one subdirectory between a year
     directory and the file itself, e.g. '2023/2023-jan/foo.jpg'.
  3. Anti-boost (penalty): there are 3 or more subdirectories between
     the (real or implicit) Pictures root and the file -- a sign of a
     deeply-nested, disorganized copy.
  4. Special case: for '*.lrcat' files (Lightroom catalogs), a copy
     living directly in a 'Lightroom/' directory (i.e. matching
     '*/Lightroom/*.lrcat') always wins over any other location,
     overriding rules 1-3.

Ties are broken deterministically by sorting on the path string, so
re-running this heuristic on unchanged input always keeps the same copy.
"""

import re

MAJOR_BONUS = 100   # year directory right under the Pictures root
MINOR_BONUS = 10    # exactly 1 subdirectory under a year directory
ANTI_BOOST = -30    # 3+ subdirectories under the Pictures root
LRCAT_BONUS = 1_000_000  # */Lightroom/*.lrcat always wins for .lrcat files

YEAR_DIR_RE = re.compile(r'^[0-9]{4}$')


def is_year_like(name):
    return bool(YEAR_DIR_RE.match(name)) or name == '2002-CA'


def pictures_anchor(dirs):
    """Index into `dirs` of the first component *after* the Pictures root.

    If a literal 'Pictures' directory is present, the anchor sits right
    after it. Otherwise the whole path is assumed to already live under
    the (implicit) Pictures root, so the anchor is 0.
    """
    for i, d in enumerate(dirs):
        if d == 'Pictures':
            return i + 1
    return 0


def score(path):
    """Return a numeric canonical-ness score for a db-relative path."""
    parts = path.split('/')
    filename = parts[-1]
    dirs = parts[:-1]  # directory components, filename excluded
    s = 0

    # Rule 4: */Lightroom/*.lrcat always wins for .lrcat files, overriding
    # every other rule.
    if filename.endswith('.lrcat') and dirs and dirs[-1] == 'Lightroom':
        s += LRCAT_BONUS

    anchor = pictures_anchor(dirs)
    under_pictures = dirs[anchor:]

    # Rule 1: major bonus.
    if under_pictures and is_year_like(under_pictures[0]):
        s += MAJOR_BONUS

    # Rule 2: minor bonus for exactly one subdirectory under a year dir.
    for i, d in enumerate(dirs):
        if YEAR_DIR_RE.match(d):
            if len(dirs) - (i + 1) == 1:
                s += MINOR_BONUS
            break

    # Rule 3: anti-boost for 3+ subdirectories under the Pictures root.
    if len(under_pictures) >= 3:
        s += ANTI_BOOST

    return s


def rank(files):
    """Sort filenames best-canonical-first: highest score, then alphabetical."""
    return sorted(files, key=lambda f: (-score(f), f))
