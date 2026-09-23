# TODO

Open task list for the photo manager web app. **All completed ticket pointers have been moved to
`STATUS.md`** — this file holds only open tickets and open questions. Plan:
`docs/plans/photo-manager-plan.md`; requirements: `docs/reqs/photo-manager-requirements.md`.
Tickets live in `docs/tickets/NNN.md`. Check an item off here (and move its pointer to
`STATUS.md`) when the ticket's status is done.

## Open tickets

All ten build-order epics, v1 filtering, and every cross-cutting ticket are done (see
`STATUS.md`). What's left:

- [ ] [085](docs/tickets/085.md) Adjustable per-file dcraw settings with sliders and thumbnail
      regeneration — blocked by question [090](docs/tickets/090.md)
- [ ] [086](docs/tickets/086.md) "This folder" / "this folder + subdirectories" toggle for the grid
- [ ] [087](docs/tickets/087.md) Fav and tag-based filter buttons in the folder view (pairs well with 086)
- [ ] [088](docs/tickets/088.md) Batch actions scoped to the current selection, including delete
- [ ] [089](docs/tickets/089.md) Export action: Huge-equivalent JPEGs of all/selected photos to a chosen folder
- [ ] [091](docs/tickets/091.md) Help screen (keyboard shortcuts), opened by 'h', '?' or F1 (user request 2026-09-23)
- [ ] [092](docs/tickets/092.md) Switch the active filter from inside the loupe (click the "filter: …"
      HUD tag), staying on the same photo; non-matching filters grayed out (user request 2026-09-23)

085-089 are priority "not now" (user request 2026-09-23); each ticket's own design-decision notes
have been resolved (documented as decisions in the ticket text) except 085's, which needs your
answer to 090. 091 and 092 have no priority note yet.

## Question tickets (waiting on the user)

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues (and its pointer moves to `STATUS.md`). A ticket is "actionable"
when it has no open blocker.

- [ ] [090](docs/tickets/090.md) Should 085's adjustable RAW settings affect only the background dcraw
      cache, or the on-demand rawpy path too? → blocks [085](docs/tickets/085.md)

Answered/closed question tickets are listed in `STATUS.md`.

To try things on a device: `./start.sh` (add `--one_star_is_unrated` if you want 1 star hidden), then open
`http://<this machine>:8080/` on the phone or tablet.
