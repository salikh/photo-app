# Design documentation

This directory explains the *why* behind decisions in the code — things a reader would not get just from
reading the source, because they came from a tradeoff, a constraint discovered against the real library, or an
explicit choice between alternatives. It is not a restatement of what the code does; read the code (or
`docs/operations.md`) for that.

Each doc links to the ticket(s) in `docs/tickets/` that carry the full discussion, options considered, and
measurements, if you need more depth than the summary here. `docs/reqs/photo-manager-requirements.md` and
`docs/plans/photo-manager-plan.md` are the original requirements/plan; several of the decisions below are
deliberate departures from that plan, made once the real library's data forced a choice the plan hadn't
anticipated — each such departure is called out explicitly.

- [photo-model.md](photo-model.md) — what a "Photo" is, how files group into one, identity across rescans
- [ratings-and-xmp.md](ratings-and-xmp.md) — the combined rating, the newest-wins rule, the reject rule, lossless
  XMP editing, sidecar naming and durability
- [scanning.md](scanning.md) — why the scan is one step per top-level directory, not one pass over everything
- [thumbnails.md](thumbnails.md) — the two RAW rendering paths (on-demand vs. background) and why they differ
- [concurrency-and-jobs.md](concurrency-and-jobs.md) — the job queue, why two queues can share one table, the
  busy-database retry, and a real race bug that was found and fixed along the way
- [frontend-viewer.md](frontend-viewer.md) — preloading, the virtualized filmstrip, pinch zoom, filters, and
  another real race bug found and fixed along the way
- [databases.md](databases.md) — every sqlite database the app touches: where each lives, its schema table by
  table, and why the state database is separate from the library and split the way it is

## Two bugs worth knowing about specifically

Both were found by testing against realistic conditions (rapid input, two components sharing state), not by
inspection, and both are the kind of thing that is easy to reintroduce by accident in future changes:

- **A stale server response could overwrite a newer optimistic UI edit** (rapid rating key presses could leave
  the display behind what was actually saved). See [frontend-viewer.md](frontend-viewer.md#the-rating-clobber-race).
- **Two `JobQueue` instances sharing one `jobs` table could steal and fail each other's jobs** (a queue claimed
  the oldest queued row regardless of `kind`). See
  [concurrency-and-jobs.md](concurrency-and-jobs.md#the-claim-scoping-bug).

## Provisional decisions still open

A few behaviors were implemented with a stated default because the alternative needed the user's judgment, and
the default is what is running today unless a linked ticket says otherwise:

- **New RAW sidecar naming**
  ([ratings-and-xmp.md](ratings-and-xmp.md#the-library-is-darktable-not-lightroom--and-it-shows)) —
  `--new_raw_sidecar_style` defaults to `full` (`NAME.DNG.xmp`), confirmed by the user (ticket 042).
- **Un-reject restores previous stars**
  ([ratings-and-xmp.md](ratings-and-xmp.md#darktable-treats-a-dng-and-its-jpeg-as-two-pictures--the-reject-rule))
  — confirmed (ticket 024).
- **Sidecars catch up to a newer database rating only on the Photo's next edit**, not proactively — confirmed
  (ticket 065). A future batch-sync command for Photos that are never edited again is ticket 068, not yet built.
