# Scanning

Code: `photoapp/scan.py`, `photoapp/paths.py`, `photoapp/fullscan.py`.

## One step per top-level directory, not one pass over the whole library

The scan originally walked the whole `--pictures_dir` in one pass and only ran grouping, rating resolution,
thumbnail indexing and missing-file marking **once, at the very end**. Against the real library (847 GB, ~92k
images) this failed twice in practice: a scan that is killed partway (the machine ran low on memory both
times — see the note under [concurrency-and-jobs.md](concurrency-and-jobs.md) about background-process memory
pressure) left every file it had *read* sitting in the database with no Photo grouping at all, because that step
never got its turn. Files were safely stored; none of them showed up in the app.

Ticket 051 restructured this: `scan_all` runs one **complete, self-contained unit of work** per top-level
directory (`_scan_subtree`) — read files, then group, resolve ratings, index existing thumbnails, and mark
missing files, all scoped to *that* directory — before moving to the next. A directory that finishes is fully
usable in the app immediately, independent of whether later directories ever get scanned. A rerun after an
interruption skips already-finished directories (via the existing per-directory mtime cache) and continues.

This is also why several previously whole-table queries (`grouping.regroup`, `ratings.refresh_dirs`,
`thumbs.index_existing`, sidecar loading) were rewritten around `photoapp/paths.py`'s range queries on the
`files.path` index, scoped to the directories that actually changed — the old whole-table versions were the
actual memory spike behind the failures, not the file-reading itself.

## Why file reads are parallel but everything else is not

`--scan_workers` (default 8) runs the per-file work (stat, decode dimensions, hash) in a thread pool, because
this is a network filesystem (`/zoo` is NAS-mounted) and the work is dominated by round-trip latency, not CPU —
measured at 2.2 files/s single-threaded versus up to 21 files/s with `--scan_workers=8` on the same directory
(small, sparse directories parallelize worse than large ones: real runs saw as little as 4-6 files/s on them
even with the pool).
Everything that writes to sqlite (grouping, ratings, thumbnail recording) stays single-threaded and runs after
the parallel read phase completes for that directory, because sqlite allows only one writer at a time — see
[concurrency-and-jobs.md](concurrency-and-jobs.md) for how the rest of the app handles that same constraint.

## Directory-mtime skip, and why in-place sidecar edits are still caught

Scanning stat's every directory and skips one whose mtime matches what was recorded last time — the same
technique `file_metadata.py` already used, reused rather than reinvented. But **editing an XMP sidecar in
place does not change its parent directory's mtime** (the directory's own entries didn't change, only a file's
contents did), so sidecar sync (`_sync_sidecars`) runs unconditionally on *every* scanned directory, skipped or
not — it has its own per-file check (the sidecar's own mtime against what's recorded for it) rather than relying
on the directory-level skip. This split is easy to miss: "the directory was skipped" and "nothing in it changed"
are not the same statement once darktable is editing sidecars independently of this app.

## RAW dimensions: a real Pillow bug found while doing this

Pillow can technically open a DNG (it's TIFF-based) and reports success — but what it decodes is the **160x120
IFD0 thumbnail embedded in the file**, not the real image. Before this was noticed, the scanner (and
`file_metadata.py` before it) had been recording every DNG in the library as 160x120 `image/tiff`. Fixed by
reading RAW dimensions through LibRaw (`fileinfo.read_raw_size`, via `rawpy`) instead of Pillow whenever the
file is RAW, using LibRaw's *crop* size specifically (not the raw sensor size) because that is what a viewer
actually shows and what matches the camera JPEG's own dimensions. A migration forces every existing RAW file row
to be re-read on the next scan so already-scanned libraries pick up the correct dimensions.
