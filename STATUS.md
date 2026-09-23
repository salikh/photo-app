# STATUS

Completed ticket pointers for the photo manager web app, moved out of `TODO.md` so that file can
stay short and hold only open work. Plan: `docs/plans/photo-manager-plan.md`; requirements:
`docs/reqs/photo-manager-requirements.md`. Tickets live in `docs/tickets/NNN.md`; epics (001-010,
one per build step in plan §7, plus 043 Mobile UI) hold checklists of their child tickets.

## Build order

- [x] [001](docs/tickets/001.md) Skeleton and schema
- [x] [002](docs/tickets/002.md) Scan and files table
- [x] [003](docs/tickets/003.md) Grouping
- [x] [004](docs/tickets/004.md) Thumbs inspection and lookup
- [x] [005](docs/tickets/005.md) XMP module and tests
- [x] [006](docs/tickets/006.md) Import and ratings
- [x] [007](docs/tickets/007.md) Read-only UI
- [x] [008](docs/tickets/008.md) Curation
- [x] [009](docs/tickets/009.md) Grouping UI
- [x] [010](docs/tickets/010.md) Hash recovery and nightly scan

All ten build-order epics are done.

## v1 filtering (user request 2026-09-21, done)

- [x] [055](docs/tickets/055.md) Rating filter buttons: All, Unrated, Rejected, exactly ★1..★5 (More menu keeps fav, conflict, picked)
- [x] [056](docs/tickets/056.md) A photo whose rating stops matching the filter disappears from the grid and the loupe advances (undo brings it back)
- [x] [057](docs/tickets/057.md) Counts on the filter buttons and grid shortcuts
- [x] [058](docs/tickets/058.md) Move the reject button to the left of the rating buttons (loupe HUD and selection bar)

## Toward functionally complete system (done so far)

- [x] [059](docs/tickets/059.md) Scrollable, tappable thumbnail strip below the main image (whole folder, also on phones)
- [x] [060](docs/tickets/060.md) Pinch zoom in/out and pan of the zoomed (original size) photo on tablets (real iPad: "works good")
- [x] [066](docs/tickets/066.md) Background low-priority population of /zoo/Thumbs with dcraw (queue visible on the Jobs page)
- [x] [067](docs/tickets/067.md) ArrowUp/ArrowDown to step the rating in the viewer
- [x] [072](docs/tickets/072.md) "Delete" button on the Rejected view: review screen with Medium thumbnails, big red Delete button removes the source files from /zoo/Pictures (needs design decisions first, see the ticket)
- [x] [073](docs/tickets/073.md) Load-adaptive background worker: watch CPU/memory load, auto start/stop a low-priority nice/ionice worker on the job queue
- [x] [074](docs/tickets/074.md) Jobs page progress indicator: total, incomplete, and completed in the last day/hour/minute
- [x] [075](docs/tickets/075.md) Delete completed jobs older than 1 week on a full rescan
- [x] [076](docs/tickets/076.md) Nightly rescan becomes one queued job per top-level directory, drained by [073](docs/tickets/073.md)'s worker
- [x] [077](docs/tickets/077.md) Filter/sort changes use replaceState so they are not their own browser-history stop (Back always means "previous folder")
- [x] [078](docs/tickets/078.md) Document the sqlite database(s) — location, schema, meaning — in docs/design/databases.md
- [x] [079](docs/tickets/079.md) Loupe "..."/Debug button to force-rerender a photo's thumbnails (fixes broken ones)
- [x] [080](docs/tickets/080.md) Prioritize the currently-open folder in the background thumbnail queue (spun out of 069's investigation)
- [x] [081](docs/tickets/081.md) Automatic purge of Pictures/.trash after a retention window (spun out of 072's decisions)
- [x] [082](docs/tickets/082.md) "Delete this file" button in the loupe's Files panel, with a Medium-thumbnail confirmation screen
- [x] [083](docs/tickets/083.md) Schema + backfill for camera metadata (aperture, shutter speed, ISO) — blocks 084
- [x] [084](docs/tickets/084.md) Show camera metadata in the Files detail view (blocked by 083)
- [x] [091](docs/tickets/091.md) Help screen (keyboard shortcuts), opened by 'h', '?' or F1 (user request 2026-09-23)
- [x] [092](docs/tickets/092.md) Switch the active filter from inside the loupe (click the "filter: …" HUD tag), staying on the same photo; non-matching filters grayed out (user request 2026-09-23)
- [x] [086](docs/tickets/086.md) "This folder" / "this folder + subdirectories" toggle for the grid (user request 2026-09-23)
- [x] [087](docs/tickets/087.md) Fav and tag-based filter buttons in the folder view (user request 2026-09-23)
- [x] [088](docs/tickets/088.md) Batch actions scoped to the current selection, including delete (user request 2026-09-23)
- [x] [089](docs/tickets/089.md) Export action: Huge-equivalent JPEGs of all/selected photos to a chosen folder (user request 2026-09-23)
- [x] [085](docs/tickets/085.md) Adjustable per-file RAW conversion settings with sliders and thumbnail regeneration — merged the on-demand and background RAW renderers onto rawpy/LibRaw (090's resolution); spun off ticket 093 as a follow-up optimization, not blocking
- [x] [101](docs/tickets/101.md) Ignore files by basename pattern, starting with `._*` (macOS AppleDouble junk never scanned into the database; a stale pre-fix row self-heals to missing=1 via the existing vanished-file path, no dedicated cleanup needed)
- [x] [095](docs/tickets/095.md) Shift+Click in the grid selects the range from a fixed anchor (last plain/Ctrl-click) to the newly clicked photo, replacing the selection; the ✓ button honors it too
- [x] [098](docs/tickets/098.md) Export target moves inside the library (`pictures_dir/Exported`), with a collision-safe destination naming scheme (part of epic 096)
- [x] [099](docs/tickets/099.md) Every export is imported and cross-referenced to its source (`files.exported_from_file_id`) the moment its job finishes, with a "exported from" link in the Files panel (part of epic 096)
- [x] [100](docs/tickets/100.md) One-off `photoapp/export_backfill.py`: links pre-existing exports to their originals via the still-live job log, then filename + dhash matching for the rest (part of epic 096)
- [x] [096](docs/tickets/096.md) Epic: exports live in the library, linked to their originals — done (097-100 all landed)
- [x] [093](docs/tickets/093.md) A settings-tuned RAW's first thumbnail request demosaics once at
      Huge and every later request for a different size downscales from that cached Huge, instead
      of a separate full demosaic per size (follow-up to 085/090)

Still open: [094](docs/tickets/094.md) — see `TODO.md`.

## Cross-cutting

- [x] [040](docs/tickets/040.md) Test infrastructure and fixtures
- [x] [041](docs/tickets/041.md) start.sh to bring up a local server
- [x] [043](docs/tickets/043.md) Mobile UI (tried on an iPad: "worked well"; an Android phone specifically is still untried)
- [x] [047](docs/tickets/047.md) Dark theme ("looks good for now, thanks!")
- [x] [051](docs/tickets/051.md) Incremental scan on subdirectories, run in sequence per year directory
- [x] [052](docs/tickets/052.md) Retry with backoff on 'database is busy' (about a minute), then a 500 shown as a toast
- [x] [054](docs/tickets/054.md) Do not show folders whose names start with a dot in the folder view
- [x] [050](docs/tickets/050.md) Preload the big image data of photos within +/-2 of the current one (faster transitions)
- [x] [048](docs/tickets/048.md) Library scan and reports (whole library scanned 2026-09-21: 94,616 files, 66,352 Photos)

## Not blocked, not yet started (done)

- [x] [068](docs/tickets/068.md) Batch sync of XMP sidecars out of sync with the computed rating (future; answer to 065)
- [x] [069](docs/tickets/069.md) Investigate: opening a folder for the first time feels slow (observation from the 063 culling trial)
- [x] [070](docs/tickets/070.md) vlog(1..7) logging in the populate_thumbs tool (user request 2026-09-22)
- [x] [071](docs/tickets/071.md) vlog(1..7) logging in the scan and in-line thumbnail paths (user request 2026-09-22)

## Question tickets, answered and closed

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues. A ticket is "actionable" when it has no open blocker. Open
question tickets are tracked in `TODO.md`, not here.

- [x] [061](docs/tickets/061.md) Try the phone UI on a real phone → tried on iPad, "worked well"; unblocked 044, 045, 046
- [x] [062](docs/tickets/062.md) Try zoom gestures on an iPad / Android tablet → "Works good, thanks!"; unblocked 060
- [x] [063](docs/tickets/063.md) Try culling a real folder and tell how it feels → no issues (folder-open speed noted, see 069); unblocked 031
- [x] [064](docs/tickets/064.md) Have a look at the dark theme → "Looks good for now, thanks!"; unblocked 047
- [x] [065](docs/tickets/065.md) How should ratings newer in the database be written to the sidecars? → on the next edit only (already correct); unblocked 021; a future batch sync is ticket 068
- [x] [090](docs/tickets/090.md) Should 085's adjustable RAW settings affect only the background dcraw cache, or the on-demand rawpy path too? → neither as originally framed: merge the two renderers into one (rawpy/LibRaw for both), with a revised Thumb-size rule; unblocked 085, superseded the "not to be unified" framing in docs/design/thumbnails.md
- [x] [097](docs/tickets/097.md) Merge an exported file into the original's Photo, or keep it as its own browseable Photo with a separate "jump to original" cross-reference? → Option B (own Photo + new `files.exported_from_file_id` column); unblocked 098-100 (epic 096)

Answered earlier: 053 (a single file's reject never rejects the pair while the other is picked; app rejects
newest-wins), 015 (JSONL mirror), 023 (write both sidecars, newest-wins), 024 (remember previous stars), 042
(`NAME.DNG.xmp`), 046 (swipe mapping), 049 (`--one_star_is_unrated`), 054 (hide dot folders).
