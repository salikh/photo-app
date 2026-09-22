# The Photo model

Code: `photoapp/grouping.py`, `photoapp/manual_links.py`, schema in `photoapp/db.py` (`files`, `photos` tables).

## Why a Photo is not a file

The library has DNG originals, camera JPEGs, TIFF/PNG exports, and occasional duplicates, and a rating needs to
apply to "this picture" regardless of which rendition is open. A **Photo** is that logical picture; a **file**
is one physical rendition of it, with a `role` (`original`, `camera`, `tuning`, `export`) and a `derived_from`
pointer to the file it came from. Rating, fav and tags live on the Photo row and are shared by every file under
it. This indirection is the reason almost every other design doc in this directory exists — ratings, sidecar
writes, thumbnails and the viewer's photo list all operate on Photos, not files.

## The automatic grouping rule, and why it grew

The original plan only grouped a RAW with its same-name camera JPEG. Ticket 015 extended it, on request, to also
group same-directory, same-stem **TIFF and PNG** files — for example a DNG plus a Picasa/darktable-exported
TIFF. The rule, in `grouping._split_groups`:

- Files with the same case-insensitive stem in one directory are one Photo when at least two of {RAW, JPG, TIF,
  PNG} are present.
- The **original** is the first available of RAW > JPG > TIF > PNG (that priority order, not creation time).
- A JPG next to a RAW is specifically the **camera** file; everything else in the group is a **tuning**.
- A second file of the *same* kind (e.g. `a.tif` and `a.TIF`) does **not** merge into the group — only the
  first of each kind participates, extras stay as their own Photo. This was a deliberate choice to avoid
  silently picking one of two same-kind files as "the" tuning.
- Grouping is **directory-scoped on purpose**: camera filename counters (`K___0001`) repeat across years, so
  cross-directory matching would wrongly merge unrelated photos. This was true from the start and stayed true
  when the rule grew to include TIF/PNG.

Measured effect on the real library (ticket 015): the extended rule merges 28 files (27 TIFF, 1 PNG) that the
DNG/JPG-only rule had left as their own Photos — small, but confirms the rule needed the extension rather than
being purely theoretical.

## Photo identity survives regrouping

A rescan re-derives grouping from scratch each time, but a Photo's `id` (and therefore its rating, fav, tags,
and activity history) must not change just because a file was renamed or a new tuning appeared. `_choose_photo`
in `grouping.py` handles this: a new grouping run reuses the existing Photo of the group's *original* file if
there is one, else the Photo of any member that already has a rating (a lone JPEG that was rated before its DNG
sibling showed up keeps that rating once they merge). This is why `regroup()` takes a set of changed directories
rather than always operating on everything — it only needs to re-derive groups where files actually changed, and
reusing Photo identity is what makes that safe. Confirmed by a real-library check (ticket 014): re-running
`regroup()` on data already grouped by an earlier pass is a no-op (0 Photos created or changed).

## Representative selection

Each Photo has one `representative_file_id` — the file whose thumbnail and image the grid/viewer show. Default
is the camera JPEG when one exists, else the original. A user can override this (`representative_source =
'manual'`), and that survives a rescan **as long as the chosen file is still a live member of the Photo**
(`grouping.fix_representatives`) — if the file is unlinked or goes missing, the representative silently falls
back to the default rather than pointing at a member that no longer belongs. This repair step runs after every
regroup, not just on request, because a manual unlink (below) can orphan the current representative.

## Manual links: the durability problem, and its answer

The automatic rule cannot cover everything (an export in a different directory, a deliberate override of a
same-directory guess), so `manual_links` records `link`/`unlink` decisions that are applied *after* the
automatic rule on every scan and always win over it. The open question from the original requirements was: the
sqlite database is meant to be a rebuildable cache (rebuilt by rescanning), but a manual link/unlink decision
cannot be *derived* from anything on disk — if the database were lost, these decisions would be gone.

The answer (ticket 015): every manual decision is appended to `<state_dir>/manual_links.jsonl` — one line per
decision, written only *after* the database transaction that recorded it commits (see
[concurrency-and-jobs.md](concurrency-and-jobs.md) for why that ordering matters under a busy-database retry).
If the database is deleted and recreated, `manual_links.restore_if_empty` replays the JSONL file before the next
scan, so the decisions come back. This is the one piece of "cache" data that genuinely is not a cache and must
be backed up — see the table in `docs/operations.md`.

A link/unlink decision is matched to a file primarily **by path**, with the file's content hash as a secondary
key so a decision survives a rename as long as the new path's content hash uniquely matches a rating recorded
for a path that has since gone missing (same hash-recovery idea used for ratings, see
[ratings-and-xmp.md](ratings-and-xmp.md)).

## Grouping-rule migrations

`GROUPING_VERSION` in `grouping.py` exists because the grouping rule itself can change (it already has once, for
TIFF/PNG). `regroup_if_rule_changed` / `regroup_scope_if_rule_changed` compare a stored version against the
current one and re-run `regroup()` exactly once per scope when they differ, so an existing database
automatically migrates to a newer rule on its next scan instead of needing a manual rebuild. Because the scan is
now per-top-level-directory (see [scanning.md](scanning.md)), the version is tracked *per scope* (the whole
library, or one top-level directory, or the root's own files) rather than as one global flag — otherwise a
partial/interrupted full-library migration would look "half done" with no way to know which directories still
need it.
