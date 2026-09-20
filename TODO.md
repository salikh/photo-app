# TODO

Top-level task list for the photo manager web app. Plan: `docs/plans/photo-manager-plan.md`;
requirements: `docs/reqs/photo-manager-requirements.md`. Tickets live in `docs/tickets/NNN.md`;
epics (001-010, one per build step in plan §7, plus 043 Mobile UI) hold checklists of their child tickets.
Check items off here when the ticket's status is done.

## Build order

- [x] [001](docs/tickets/001.md) Skeleton and schema
- [x] [002](docs/tickets/002.md) Scan and files table
- [ ] [003](docs/tickets/003.md) Grouping
- [ ] [004](docs/tickets/004.md) Thumbs inspection and lookup
- [ ] [005](docs/tickets/005.md) XMP module and tests
- [ ] [006](docs/tickets/006.md) Import and ratings
- [ ] [007](docs/tickets/007.md) Read-only UI
- [ ] [008](docs/tickets/008.md) Curation
- [ ] [009](docs/tickets/009.md) Grouping UI
- [ ] [010](docs/tickets/010.md) Hash recovery and nightly scan

## Cross-cutting

- [ ] [040](docs/tickets/040.md) Test infrastructure and fixtures
- [x] [041](docs/tickets/041.md) start.sh to bring up a local server
- [ ] [043](docs/tickets/043.md) Mobile UI: responsive layout, swipe navigation, swipe rating (after the desktop grid/loupe/rating exist)

## Decisions needed from the user

- [ ] [024](docs/tickets/024.md) Decision: reject vs previous stars
- [ ] [023](docs/tickets/023.md) Review the conflict survey before any XMP write (after it runs)
- [ ] [015](docs/tickets/015.md) Confirm JSONL mirror as the durability answer for manual links
- [ ] [042](docs/tickets/042.md) Naming of new sidecars for RAW originals (survey says the library is darktable full-filename style)
- [ ] [046](docs/tickets/046.md) Confirm swipe up/down rating mapping (up = +1 step, down = -1 step, clamped)

## Suggested first moves

Independent of each other, can start now:

- [x] [011](docs/tickets/011.md) Extract reusable logic from `file_metadata.py`
- [ ] [016](docs/tickets/016.md) Inspect `/zoo/Thumbs` layout (read-only)
- [x] [018](docs/tickets/018.md) XMP sidecar discovery and read
- [x] [001](docs/tickets/001.md) Skeleton and schema
