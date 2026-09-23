# TODO

Open task list for the photo manager web app. **All completed ticket pointers have been moved to
`STATUS.md`** — this file holds only open tickets and open questions. Plan:
`docs/plans/photo-manager-plan.md`; requirements: `docs/reqs/photo-manager-requirements.md`.
Tickets live in `docs/tickets/NNN.md`. Check an item off here (and move its pointer to
`STATUS.md`) when the ticket's status is done.

## Open tickets

All ten build-order epics, v1 filtering, and every cross-cutting ticket are done (see
`STATUS.md`). What's left:

- [ ] [093](docs/tickets/093.md) Render a tuned RAW's thumbnails once at Huge and downscale the
      rest from it, instead of demosaicing separately per size (follow-up to 085/090, not blocking)
- [ ] [094](docs/tickets/094.md) Plan a new tuning architecture: provisional (not global) thumbnail
      regen while adjusting RAW settings, a hotkey to compare current vs. new rendering, room for
      future browser-local rendering, and an explicit Save button that commits + triggers the real
      backend regen
- [ ] [095](docs/tickets/095.md) Shift+Click in the grid should select the range from the last
      selected photo to the newly clicked one, not just toggle the one photo
- [ ] [096](docs/tickets/096.md) Epic: exports live in the library, linked to their originals
  - [ ] [097](docs/tickets/097.md) (question) Merge exported file into the original's Photo, or
        keep it separate with a cross-reference?
  - [ ] [098](docs/tickets/098.md) Export target moves to `/zoo/Pictures/Exported`, with
        collision-safe destination naming
  - [ ] [099](docs/tickets/099.md) Import + link every export to its original in real time, going
        forward (uses the export job's own record, no heuristics)
  - [ ] [100](docs/tickets/100.md) One-off backfill: link what's already in `/zoo/Pictures/Exported`
        to its original (still-live job log first, then filename + dhash heuristics)

093-096 are open follow-ups filed after 085 (2026-09-23/24), none yet scoped/built. 085-089, 091 and
092 are done — see `STATUS.md`.

## Question tickets (waiting on the user)

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues (and its pointer moves to `STATUS.md`). A ticket is "actionable"
when it has no open blocker.

- [ ] [097](docs/tickets/097.md) Merge an exported file into the original's Photo (reuse
      `manual_links`' existing role='export'), or keep it as its own browseable Photo with a
      separate "jump to original" cross-reference? Blocks 098-100 (filed 2026-09-24, epic 096).

090 was answered 2026-09-23 (see `STATUS.md`) and unblocked 085.

To try things on a device: `./start.sh` (add `--one_star_is_unrated` if you want 1 star hidden), then open
`http://<this machine>:8080/` on the phone or tablet.
