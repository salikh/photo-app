# Metadata collection process

This document describes how `metadata_db.py` builds a metadata database
(star rating, reject status, popularity) from a photo library's hash
catalog (a sqlite3 database with a `hashes(filename, hash)` table, as
produced by the tooling in this directory -- see `compare.py` and
`cleanup.py`).

## Inputs

- **`database`**: the hash catalog, e.g. `~/zoo.db` or `~/nas.db`. Every
  row is `(filename, hash)`, where `filename` is a path relative to the
  photo library root and `hash` is a content hash (sha224, 56 hex
  chars) of that file's bytes.
- **`--root_dir`**: the directory on disk that `filename` is relative
  to (e.g. `/zoo/Pictures`). Used to reach the actual files for
  `.xmp` sidecars and nested `*.db` files.

## Step 1: keep only genuine images

The `hashes` table also contains sidecars (`.xmp`, `.json`), Lightroom
catalogs (`.lrcat`), thumbnail caches (`Thumbs.db`), and the tracking
databases themselves (`.prod.db`). Only filenames whose extension is a
recognized image type are considered
(`.jpg .jpeg .png .dng .tif .tiff .heic .heif .cr2 .cr3 .nef .arw .raf
.rw2 .orf .gif .bmp .webp .pef`, case-insensitive). Everything else is
dropped before any metadata is collected -- it never appears as a row
in the output database.

## Step 2: pick a canonical copy per hash

Photos are frequently duplicated across old exports, Lightroom
libraries and re-organizations. For every hash shared by more than one
genuine-image file, one copy is picked as "canonical" using the same
ranking heuristic as `cleanup.py` (see `ranking.py`): copies that live
directly under a year directory (optionally under an explicit or
implicit `Pictures/` root) are strongly preferred, a single
subdirectory under the year gets a small extra bonus, deeply-nested
copies (3+ subdirectories under `Pictures`) are penalized, and
Lightroom catalog files always prefer a copy that lives directly in a
`Lightroom/` directory.

## Step 3: merge DNG/JPG renditions of the same shot

A `*.DNG` and a `*.JPG` file that sit side by side with the same base
name (e.g. `2020/2020-03-light/K___8434.DNG` and
`2020/2020-03-light/K___8434.JPG`) are two renditions of the very same
photograph, not two different photographs that happen to look alike --
this mirrors how the `.prod.db` tracking database itself assigns them
the same `id` (see Step 4). These are merged into a single logical
image / single output row. The DNG rendition (the raw master) is used
as the row's `hash` and `filepath`; the JPG rendition's copies and
metadata still contribute to `popularity` and to rating resolution.

## Step 4: collect ratings from `*.db` sources

Any file tracked in `hashes` whose name ends in `.db` (e.g. `.prod.db`)
is opened as sqlite3 (read-only) and checked for the schema used by
this photo library's own indexing tool:

```
images(filename, id, sha224, ...)
photos(id, rating, ...)
```

`images.sha224` is the same content hash as `hashes.hash`, so a rating
is looked up directly by hash: `images.sha224 = hash` gives the
`photos.id`, whose `photos.rating` is the metadata value. Because a
DNG and its JPG twin share the same `id` in this table, this join
naturally reproduces the Step 3 merge for this source. `*.db` files
that fail to open as sqlite3 (e.g. Windows `Thumbs.db`) or don't have
this `images`/`photos` shape (e.g. Lightroom's `*.lrdata/*.db` preview
caches) are skipped, with a note on stderr.

Multiple `*.db` files can exist (a live one plus stale copies inside
old `Exported/...` backups). When they disagree on a hash's rating, a
`CONFLICT` line is logged to stderr, and the copy that is *not* inside
an `Exported/` path wins (it is assumed to be the live, up-to-date
database).

## Step 5: collect ratings from `.xmp` sidecars

For each DNG/JPG rendition's canonical path, `metadata_db.py` looks for
a sidecar at `<path>.xmp` (or `<path>.XMP`) under `--root_dir`. If
found, it is parsed as XML and the rating is read from the `Rating`
property in the `http://ns.adobe.com/xap/1.0/` XMP namespace --
regardless of which namespace prefix a given tool used to write it
(observed in this library: `xap:Rating` as a child element from older
Adobe tools, and `xmp:Rating="N"` as an attribute from darktable). A
tolerant regex fallback is used for any `.xmp` file that fails to
parse as well-formed XML.

## Step 6: resolve precedence and log conflicts

Both sources encode rating and reject status together as a single
**combined value in the range [-1, 5]**:

| value | meaning                          |
|------:|----------------------------------|
|    -1 | rejected, no star rating         |
|     0 | unmarked/unrated, no star rating |
|   1-5 | picked, with an N-star rating    |

This combined value is decoded into the two output columns:
`reject = -1` when the value is negative, `reject = 1` and
`rating = value` when positive, otherwise `reject = 0` and
`rating = 0`.

Candidate ratings are gathered in this precedence order (first
available value wins):

1. `.prod.db`-style rating for the DNG rendition
2. `.prod.db`-style rating for the JPG rendition
3. `.xmp` sidecar rating for the DNG rendition
4. `.xmp` sidecar rating for the JPG rendition

i.e. `.prod.db` always outranks `.xmp`, and within the same source
type the DNG rendition outranks the JPG rendition.

**Exception -- a pick always wins.** If *any* candidate carries a
positive (picked) value, the image is resolved as a pick regardless of
what higher-precedence sources say: a `.prod.db` reject or unmarked
verdict never overrides an actual pick found in, say, an `.xmp`
sidecar. This matters in practice -- it is common for `.prod.db` to
carry a stale `-1` while a later `.xmp` edit marks the photo as picked,
and the pick should not be silently discarded. When multiple sources
agree on picking the image but disagree on the star count, the normal
precedence order above still decides which star count is used.

Whenever candidates disagree, a `CONFLICT` line is printed to stderr
naming the image, every value found, and which file it came from. If
the disagreement was resolved by the "a pick always wins" rule, the
line says so explicitly; otherwise the highest-precedence value is
used as before. Either way, the conflict is always surfaced for manual
review even though the database only stores the resolved value.

## Step 7: compute popularity

`popularity` is a fine-grained duplication score, not a boolean:

```
popularity = copies
            + (exported_bonus if any copy is under '/Exported/' else 0)
            + 200 * (number of copies under a 'Photo Archive' path)
            + 100 * (number of copies under an 'Exported' path)
```

`copies` is the total number of physical files (across every merged
DNG/JPG rendition) that share this image's content -- more redundant
backups of the same shot means a higher baseline popularity.
`exported_bonus` (default 1000, `--exported-bonus`) is a large,
deliberately dominant bonus: a photo that has survived into an old
`/Exported/.../` backup is considered significantly more "popular"
(worth preserving) regardless of how many plain duplicate copies it
has elsewhere.

On top of that flat, once-per-image bonus, two smaller *per-copy*
bonuses reward images with several surviving backup copies rather than
just one: 200 points for every physical copy that lives under a
`Photo Archive` path component, and 100 points for every physical copy
under an `Exported` path component (both counted independently, so a
copy nested under both adds both bonuses).

## Output schema

```sql
CREATE TABLE images (
    hash TEXT PRIMARY KEY,       -- content hash of the canonical (DNG-preferred) copy
    filepath TEXT NOT NULL,      -- canonical relative path of that copy
    rating INTEGER NOT NULL,     -- 0-5, 0 = unrated
    reject INTEGER NOT NULL,     -- -1/0/1
    popularity INTEGER NOT NULL, -- see Step 7
    copies INTEGER NOT NULL,           -- physical copy count across all renditions
    has_exported_copy INTEGER NOT NULL,-- 1 if any copy is under '/Exported/'
    rating_source TEXT,          -- 'prod.db' / 'xmp' / NULL (no metadata found)
    rating_source_path TEXT,     -- the specific file the winning rating came from
    merged_paths TEXT NOT NULL   -- JSON array of every physical file merged into this row
);
```

`rating_source` and `rating_source_path` make every row's provenance
directly inspectable without re-running the tool. `merged_paths` lists
every physical duplicate (including both DNG and JPG renditions where
applicable) that fed into this row's `popularity` and rating
resolution.

## Everything printed during a run

- `INFO:` lines report progress and skip decisions (schema mismatches,
  missing files) and are safe to ignore.
- `CONFLICT:` lines are the ones worth reading: they flag every case
  where two metadata sources disagreed about a photo's rating, so the
  precedence rule's choice can be spot-checked or corrected by hand.
