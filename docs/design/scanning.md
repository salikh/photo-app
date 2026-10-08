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
the work is dominated by storage I/O latency, not CPU — measured at 2.2 files/s single-threaded versus up to
21 files/s with `--scan_workers=8` on the same directory (small, sparse directories parallelize worse than
large ones: real runs saw as little as 4-6 files/s on them even with the pool). Earlier notes describe `/zoo`
as NAS-mounted; ticket 069's investigation (2026-09-23) checked the actual mount on the running deployment and
found it is a local ZFS pool on two SATA HDDs in a mirror (`zpool status zoo`), not a network filesystem — the
latency is real (mechanical seek time across large RAW files, not round-trip network cost), just not from the
cause these notes originally assumed. The parallelism reasoning and the measured numbers above are unaffected.
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

## Move/rename detection repoints the row and moves its thumbnails (ticket 128)

A file is matched to the database by **path**, so a rename would otherwise look like a deletion of the old path
plus the arrival of a brand-new, unrated file. Ticket 128 recognises a move instead: when a file is found at a
path that is **not yet known**, its freshly computed content hash is looked up against the **whole** `files`
table (`files_hash`), and a row is a candidate only if its own file is **gone from disk**
(`os.path.exists(pictures_dir/old_path)` is false). This filesystem check is what lets a *limited* rescan
(a single subdirectory, a nightly `scan_dir` job, `fullscan --scan_dirs`) still catch a move whose old path lies
in a directory that was not walked — the old row may still say `missing=0`, so the database alone cannot answer
it.

Exactly as with hash recovery ([ratings-and-xmp.md](ratings-and-xmp.md)), the rule is deliberately conservative:
a move is applied only when **exactly one** candidate remains, and only when exactly one not-yet-known file in
the batch carries that hash. A copy (the old path still exists), a content duplicate, or two gone rows with the
same content are all skipped and fall back to today's insert-a-new-row behaviour.

Applying a move **repoints the existing row** (`UPDATE files SET path=?, <refreshed metadata>, missing=0
WHERE id=old_id`) rather than inserting a new one, so the `files.id` -- and with it the Photo, rating/fav, tags,
every `raw_*`/crop/`thumb_rev` column, `xmp_sidecars.file_id` and `exported_from_file_id` -- is preserved for
free. As a followup, the artifacts keyed by the picture's name are renamed to the new name:
`thumbs.move_thumbnails` moves every cached `thumbs_dir/<Size>/<old>.jpg` to the new path and updates its
`thumbs` row, and `raw_preview_dng.move` does the same for the `PreviewDNG` cache -- so the thumbnail tree keeps
matching the picture tree and no render is redone. The moved-from directory is added to the scan's changed
directories so grouping there is revisited too.

Hash recovery still exists for the cases this cannot cover: a row that is genuinely gone (a rebuilt database) or
an ambiguous match, where the content hash in `rating_by_hash` is the only way back to the rating.

## Videos

`fileinfo.is_media()` (image or video) decides what a scan indexes; `read_image_metadata` returns nothing
useful for a video (Pillow cannot open one), so `scan._with_video_info()` overlays the ffprobe result
(size with the rotation tag applied, duration, fps, codec, date) onto the record. The date is the file
name's camera-local timestamp (`VID_20180605_171430`) first, then the container's `creation_time`, which
is UTC and so differs by the time zone (seen on a real Samsung clip), else NULL (the date sort falls back
to mtime). Without ffprobe the record lacks the `duration` key, so `metacache.record_lacks_video_info`
re-probes it once ffprobe exists (a present `None` means "unprobeable" and is not retried).

