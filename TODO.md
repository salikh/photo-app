# TODO

Open task list for the photo manager web app. **All completed ticket pointers have been moved to
`STATUS.md`** — this file holds only open tickets and open questions. Plan:
`docs/plans/photo-manager-plan.md`; requirements: `docs/reqs/photo-manager-requirements.md`.
Tickets live in `docs/tickets/NNN.md`. Check an item off here (and move its pointer to
`STATUS.md`) when the ticket's status is done.

## Open tickets

All ten build-order epics, v1 filtering, and every cross-cutting ticket are done (see
`STATUS.md`). What's left:

- [ ] [085](docs/tickets/085.md) Adjustable per-file RAW conversion settings with sliders and
      thumbnail regeneration — unblocked by [090](docs/tickets/090.md)'s answer, now also merges the
      on-demand and background RAW renderers into one
- [ ] [089](docs/tickets/089.md) Export action: Huge-equivalent JPEGs of all/selected photos to a chosen folder

085 and 089 are priority "not now" (user request 2026-09-23); both now have their design
decisions resolved and are ready to implement. 086, 087, 088, 091 and 092 are done — see `STATUS.md`.

## Question tickets (waiting on the user)

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues (and its pointer moves to `STATUS.md`). A ticket is "actionable"
when it has no open blocker.

**No open question tickets right now.** 090 was answered 2026-09-23 (see `STATUS.md`) and unblocked 085.

To try things on a device: `./start.sh` (add `--one_star_is_unrated` if you want 1 star hidden), then open
`http://<this machine>:8080/` on the phone or tablet.
