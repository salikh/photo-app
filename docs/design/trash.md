# Deleting rejected photos: a move, not a delete

Code: `photoapp/trash.py`, `photoapp/scan.py` (`top_level_steps`, `_scan_subtree`'s pruning of
`.trash`), `photoapp/api.py` (`/api/photos/trash`), `photoapp/static/pages.js`
(`deleteReviewPage`).

## Why this is the one write that isn't a rating or a cache entry

Every other write this app makes to `pictures_dir` before ticket 072 was either reversible (a
rating, undo-able) or purely additive (a sidecar, a thumbnail). Ticket 072 asked for a way to
actually get rejected photos off disk. The decision (see the ticket) was: move, never unlink —
every file under a rejected Photo (the original, its camera JPG, any tuning, and every sidecar)
moves into `<pictures_dir>/.trash/`, mirroring its original subdirectory structure exactly, so a
trashed file is fully recoverable by hand (`mv` it back) for as long as it sits there. An automatic
purge after a retention window is deliberately a separate, not-yet-built ticket (081) — trashing
and purging are different enough operations (one is triggered by a user reviewing photos, the
other is a scheduled sweep) that bundling them would have made the first one harder to get right.

## trash must never be scanned, not just hidden

This app already has a "hidden folder" convention (ticket 054): a dot-prefixed directory name is
excluded from the folder browser's listing, but a scan still reads it, because that convention
exists for old programs' leftover folders (`.thumbnails`, `.picasaoriginals`, ...) that happen to
sit inside the library and should not be silently ignored forever.

`.trash/` cannot use that same rule as-is. A trashed file's sidecar still says exactly what it said
before it was trashed — rating `-1`. If a scan read `.trash/` the way it reads any other
dot-folder, the very next nightly or manual rescan would discover
`.trash/2019/trip/IMG_1.DNG(.xmp)` as a *brand new* file at a *brand new* path, group it into a
*brand new* Photo (losing the original's rating history/activity log continuity — Photo identity
in this app is derived from grouping + rating provenance, not a portable ID that follows a file
across an unrelated path change, see [photo-model.md](photo-model.md)), and, because the sidecar
still resolves to reject, that new Photo would come back **still correctly rated reject but fully
visible again** — a photo the user had just deliberately trashed, quietly un-trashed by the
scanner. Confirmed with a real scan-after-trash test
(`tests/test_trash.py::test_a_trashed_photos_sidecar_is_not_rediscovered_by_a_scan`) before this
exclusion was added — the Photo count *did* increase by one without it.

The fix is at the scan layer, not the trash layer: `scan.top_level_steps` excludes
`trash.TRASH_DIRNAME` from the set of top-level directories a whole-library scan iterates (covers
`scan_all`, the nightly enqueue, and the manual Rescan-all button — all three drive off this same
function), and `_scan_subtree`'s own `os.walk` additionally prunes it from `dirnames` whenever the
walk is standing at `pictures_dir` itself, so a direct recursive `scan()` of the whole library
(not just the `top_level_steps` path) is covered too. A user who deliberately navigates to
`.trash/` by hand-editing the URL and asks for a folder-scoped rescan of exactly that path is not
guarded against — that is a genuinely deliberate action with a recoverable outcome (the photo
becomes visible again, nothing is lost), not the routine case this exclusion protects against.

## Database state: `missing = 1`, not a new "gone" representation

A trashed file's `files` row is updated in place (`missing = 1`) at its *original* path — no row
is ever created for the file's new location under `.trash/`, because nothing ever scans there to
create one. This reuses exactly the same mechanism a file gets when a scan finds it has vanished
from disk on its own (ticket 010's hash-recovery groundwork already assumes files can be marked
missing without losing history), so trashing needed no new schema and no new "this Photo is gone"
concept — the rest of the app (grids, filters, `filter_counts`) already know to exclude
`missing = 1` files from every view.

## What is deliberately left alone

Thumbnails in `/zoo/Thumbs` are not cleaned up when a photo is trashed — consistent with the rest
of the codebase, which never deletes a thumbnail once made (see [thumbnails.md](thumbnails.md));
an orphaned cache entry is harmless. The stale `xmp_sidecars` database row at a trashed sidecar's
*old* path is not explicitly cleaned up by the trash operation either — the next ordinary scan of
that (still-scanned, non-`.trash`) directory naturally notices the sidecar file is gone and deletes
the row itself, the same way it always reconciles any other externally-deleted sidecar.
