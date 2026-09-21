# TODO

Top-level task list for the photo manager web app. Plan: `docs/plans/photo-manager-plan.md`;
requirements: `docs/reqs/photo-manager-requirements.md`. Tickets live in `docs/tickets/NNN.md`;
epics (001-010, one per build step in plan §7, plus 043 Mobile UI) hold checklists of their child tickets.
Check items off here when the ticket's status is done.

## Build order

- [x] [001](docs/tickets/001.md) Skeleton and schema
- [x] [002](docs/tickets/002.md) Scan and files table
- [x] [003](docs/tickets/003.md) Grouping
- [x] [004](docs/tickets/004.md) Thumbs inspection and lookup
- [x] [005](docs/tickets/005.md) XMP module and tests
- [ ] [006](docs/tickets/006.md) Import and ratings (021 reopened: the newest of database and XMP is authoritative)
- [x] [007](docs/tickets/007.md) Read-only UI
- [ ] [008](docs/tickets/008.md) Curation
- [x] [009](docs/tickets/009.md) Grouping UI
- [x] [010](docs/tickets/010.md) Hash recovery and nightly scan

## Next: v1 filtering (user request 2026-09-21, do now)

- [x] [055](docs/tickets/055.md) Rating filter buttons: All, Unrated, Rejected, exactly ★1..★5 (More menu keeps fav, conflict, picked)
- [x] [056](docs/tickets/056.md) A photo whose rating stops matching the filter disappears from the grid and the loupe advances (undo brings it back)
- [x] [057](docs/tickets/057.md) Counts on the filter buttons and grid shortcuts
- [x] [058](docs/tickets/058.md) Move the reject button to the left of the rating buttons (loupe HUD and selection bar)

## Later (requested, not for now)

- [x] [059](docs/tickets/059.md) Scrollable, tappable thumbnail strip below the main image (whole folder, also on phones)
- [ ] [060](docs/tickets/060.md) Pinch zoom in/out and pan of the zoomed (original size) photo on tablets (iPad, best also Android)

## Cross-cutting

- [x] [040](docs/tickets/040.md) Test infrastructure and fixtures
- [x] [041](docs/tickets/041.md) start.sh to bring up a local server
- [ ] [043](docs/tickets/043.md) Mobile UI (built and tested in an emulated phone; needs a real-phone try)
- [ ] [047](docs/tickets/047.md) Dark theme (built, contrast checked; needs your look)
- [x] [051](docs/tickets/051.md) Incremental scan on subdirectories, run in sequence per year directory
- [x] [052](docs/tickets/052.md) Retry with backoff on 'database is busy' (about a minute), then a 500 shown as a toast
- [x] [054](docs/tickets/054.md) Do not show folders whose names start with a dot in the folder view
- [x] [050](docs/tickets/050.md) Preload the big image data of photos within +/-2 of the current one (faster transitions)
- [x] [048](docs/tickets/048.md) Library scan and reports (whole library scanned 2026-09-21: 94,616 files, 66,352 Photos)

## Question tickets (waiting on the user)

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues. A ticket is "actionable" when it has no open blocker.

- [ ] [061](docs/tickets/061.md) Try the phone UI on a real phone (blocks 044, 045, 046; on hold by the user)
- [ ] [062](docs/tickets/062.md) Try zoom gestures on an iPad / Android tablet (blocks 060; 060 is built, ready to try)
- [ ] [063](docs/tickets/063.md) Try culling a real folder and tell how it feels (blocks 031)
- [ ] [064](docs/tickets/064.md) Have a look at the dark theme (blocks 047)
- [ ] [065](docs/tickets/065.md) How should ratings newer in the database be written to the sidecars? (blocks the sync item of 021)

Answered so far: 053 (a single file's reject never rejects the pair while the other is picked; app rejects newest-wins), 015 (JSONL mirror), 023 (write both sidecars, newest-wins), 024 (remember previous stars),
042 (`NAME.DNG.xmp`), 046 (swipe mapping), 049 (`--one_star_is_unrated`), 054 (hide dot folders).

Epic 007 is done. Epics 006, 008 and 043 close when the questions above are answered (006: only the optional sync item of 021).

To try things on a device: `./start.sh` (add `--one_star_is_unrated` if you want 1 star hidden), then open `http://<this machine>:8080/` on the phone or tablet.
