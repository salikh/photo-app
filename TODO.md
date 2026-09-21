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
- [ ] [007](docs/tickets/007.md) Read-only UI
- [ ] [008](docs/tickets/008.md) Curation
- [x] [009](docs/tickets/009.md) Grouping UI
- [x] [010](docs/tickets/010.md) Hash recovery and nightly scan

## Next: v1 filtering (user request 2026-09-21, do now)

- [ ] [055](docs/tickets/055.md) Rating filter buttons: All, Unrated, Rejected, exactly ★1..★5 (More menu keeps fav, conflict, picked)
- [ ] [056](docs/tickets/056.md) A photo whose rating stops matching the filter disappears from the grid and the loupe advances (undo brings it back)
- [ ] [057](docs/tickets/057.md) Counts on the filter buttons and grid shortcuts
- [ ] [058](docs/tickets/058.md) Move the reject button to the left of the rating buttons (loupe HUD and selection bar)

## Later (requested, not for now)

- [ ] [059](docs/tickets/059.md) Scrollable, tappable thumbnail strip below the main image (whole folder, also on phones)
- [ ] [060](docs/tickets/060.md) Pinch zoom in/out and pan of the zoomed (original size) photo on tablets (iPad, best also Android)

## Cross-cutting

- [x] [040](docs/tickets/040.md) Test infrastructure and fixtures
- [x] [041](docs/tickets/041.md) start.sh to bring up a local server
- [ ] [043](docs/tickets/043.md) Mobile UI (built and tested in an emulated phone; needs a real-phone try)
- [ ] [047](docs/tickets/047.md) Dark theme (built, contrast checked; needs your look)
- [x] [051](docs/tickets/051.md) Incremental scan on subdirectories, run in sequence per year directory
- [ ] [052](docs/tickets/052.md) Retry with backoff on 'database is busy' (about a minute), then a 500 shown as a toast
- [x] [054](docs/tickets/054.md) Do not show folders whose names start with a dot in the folder view
- [ ] [050](docs/tickets/050.md) Preload the big image data of photos within +/-2 of the current one (faster transitions)
- [x] [048](docs/tickets/048.md) Library scan and reports (whole library scanned 2026-09-21: 94,616 files, 66,352 Photos)

## Waiting on the user

Everything that can be built and verified without you is done (139 tests, real-library checks).
What is left needs a decision, a real device, or a go-ahead. Provisional defaults are implemented
where noted, so the app already works; a decision only changes behavior.

Decisions (each ticket has the options and the evidence; 024 and 042 were answered on 2026-09-21):

- [x] [024](docs/tickets/024.md) Un-reject restores the previous star count (implemented as default: yes)
- [x] [042](docs/tickets/042.md) New RAW sidecar name: `NAME.DNG.xmp` (implemented, `--new_raw_sidecar_style=full`) or `NAME.xmp`
- [x] [049](docs/tickets/049.md) darktable's default 1 star: hidden with `--one_star_is_unrated` (opt-in; decided and implemented 2026-09-21)
- [x] [023](docs/tickets/023.md) Ratings are written to both the DNG and JPG sidecars; reading stays newest-wins (decided and implemented 2026-09-21)
- [ ] [015](docs/tickets/015.md) Manual link decisions are mirrored to `manual_links.jsonl` (implemented); confirm
- [x] [046](docs/tickets/046.md) Swipe up/down = one rating step, clamped (confirmed 2026-09-21)

- [ ] [053](docs/tickets/053.md) 75 rejects are hidden by newest-wins (a newer sidecar says 1 or 2): keep, or let a reject win?

Needs a real device or your time:

- [ ] [044](docs/tickets/044.md), [045](docs/tickets/045.md), [046](docs/tickets/046.md) Try the phone UI over the LAN (`./start.sh`, open `http://<host>:8080/`)
- [ ] [031](docs/tickets/031.md) Timed culling trial on a real folder

Epics 006, 007, 008 and 043 close when the items above are settled.
