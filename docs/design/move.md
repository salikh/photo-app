# Moving photos to another library folder: a live relocation, not a trash-style move

Code: `photoapp/move.py`, `photoapp/api.py` (`POST /api/move`, `GET /api/dirs/exists`),
`photoapp/static/move.js`, `photoapp/export_backfill.py` (`backfill_by_dhash`'s `dir_prefix`),
`photoapp/api.py`'s `link_exports_job`.

## Why this isn't trash.py with a different destination

`trash.py`'s move into `.trash/` is for a file this app is *done showing*: the row is marked
`missing = 1`, the destination mirrors the original subdirectory structure (so it's recoverable by
hand), and nothing about the move needs to be reflected back into `files.path`, thumbnails, or any
cache — the file simply stops being live. Ticket 144/145's folder move is the opposite kind of
operation: the file stays fully part of the library, just reachable at a different path. So instead
of `missing = 1`, `move.py` repoints `files.path` in place and moves the file's cached thumbnails
(`thumbs.move_thumbnails`) and PreviewDNG cache (`raw_preview_dng.move`) to follow it — the exact
same machinery ticket 128 already uses when a move is *discovered* by a scan's hash matching. Using
it here directly, synchronously, means the grid reflects the move the instant the request returns,
rather than showing a broken thumbnail (or, worse, a transient duplicate/missing Photo) until a
later scan's probabilistic hash matching rediscovers the file — which can also just fail outright
for duplicate-content files (`scan._find_move_source` refuses to guess when more than one gone row
shares a hash).

## What moves synchronously, what's left to the rescan

Every *live* file of a Photo moves together — RAW+JPEG pairs, a linked tuning file, any role — not
just the representative one, the same per-Photo granularity `trash.trash_photo` uses. Each file's
sidecars move with it: every `xmp_sidecars` row for that `file_id`, plus its ticket 111 per-file
`<name>.json`. What does *not* happen synchronously is updating the `xmp_sidecars` table's own
`path`/`file_id` bookkeeping, or `index.json` — those are deliberately left to the rescan
`POST /api/move` schedules for both the source and destination directories immediately afterward.
A full (if cheap, thanks to the scan cache) non-recursive-in-spirit scan of both directories
reconciles that the same way it would for a file moved by hand outside this app entirely; writing
that bookkeeping twice (once here, once again by the rescan) would be duplicated, fragile logic for
no real benefit, since the rescan is already guaranteed to run.

## Collisions: always a stranger, never a reusable one

`export.py`'s own `resolve_dest_path` disambiguates a collision with a numeric suffix, but treats a
destination file that is itself an earlier export of the same source specially (overwritten in
place, not disambiguated) — meaningful for re-running an export, meaningless for a move, and
actively wrong here: silently overwriting an unrelated file at a collision would destroy someone
else's photo. `move.resolve_dest_path` is a fresh, simpler implementation with no such case: a
collision always gets `name-2.ext`, `name-3.ext`, etc., checked against both the filesystem and an
in-batch `taken` set (a later file in the same request hasn't been written yet when an earlier one
in the batch is being disambiguated). When the image itself is renamed this way, its sidecars are
renamed to match (`_renamed_sidecar_name`, aware of both of `xmp.py`'s naming conventions — `name.
ext.xmp` and RAW's `name.xmp`) so a sidecar never silently stops matching the image it belongs to.

## Destination is always inside the library

Unlike `export.py`'s target (anywhere, including outside `pictures_dir` — it produces a copy),
`move`'s target is always validated with `library._norm_dir`, the same normalization every other
`dir` parameter in this app already uses. This is reorganizing the library the app already owns,
not producing something new outside it; moving an original out of `pictures_dir` entirely was never
asked for and would be a much larger, different kind of operation (effectively removing it from the
app's view without the reversibility trash.py at least offers).

## Folder creation is a frontend confirmation, not a server re-ask

`POST /api/move` always `os.makedirs`s the destination if it doesn't exist — the safety gate is
`GET /api/dirs/exists` plus the frontend's own confirm modal (ticket 147), asked *before* the
request is made. The server doesn't re-confirm, the same division of concerns `trash_photos_route`
already draws between "the server enforces invariants that matter regardless of the client" (a
valid Photo id, a path that doesn't escape `pictures_dir`) and "the client owns the UX
confirmation" (nothing server-side re-asks whether the user really meant to reject-and-delete,
either).

## The `Exported/` link-back is a job, not part of the move itself

If the destination starts with `Exported/` (or is exactly `Exported`), `POST /api/move` schedules
one more job: `link_exports` (ticket 146), a thin wrapper around `export_backfill.backfill_by_dhash`
scoped to just the destination directory via a new `dir_prefix` parameter (candidates for "what was
this exported from" still search the whole library — an export's original can be anywhere, not just
near where the export landed). This is deliberately a *job*, not inline work in the move request:
the dhash comparison decodes images, real (if usually small) I/O cost that has no business blocking
an HTTP response, and it belongs on the same low-priority queue `scan_dir`/`populate_thumb` already
share for exactly that reason.
