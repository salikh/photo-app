# TODO

Open task list for the photo manager web app. **All completed ticket pointers have been moved to
`STATUS.md`** — this file holds only open tickets and open questions. Plan:
`docs/plans/photo-manager-plan.md`; requirements: `docs/reqs/photo-manager-requirements.md`.
Tickets live in `docs/tickets/NNN.md`. Check an item off here (and move its pointer to
`STATUS.md`) when the ticket's status is done.

## Open tickets

All ten build-order epics, v1 filtering, and every cross-cutting ticket are done (see
`STATUS.md`). What's left:

- [ ] [094](docs/tickets/094.md) Plan a new tuning architecture: provisional (not global) thumbnail
      regen while adjusting RAW settings, a hotkey to compare current vs. new rendering, room for
      future browser-local rendering, and an explicit Save button that commits + triggers the real
      backend regen
094 is the one open follow-up filed after 085 (2026-09-23/24), not yet built. 093 and 095-101
(epic 096 and its children 097-100) are done — see `STATUS.md`. 085-089, 091 and 092 are done too.

## Question tickets (waiting on the user)

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues (and its pointer moves to `STATUS.md`). A ticket is "actionable"
when it has no open blocker.

**No open question tickets right now.** 097 was answered 2026-09-24 (Option B; see `STATUS.md`)
and unblocked 098-100. 090 was answered 2026-09-23 (see `STATUS.md`) and unblocked 085.

To try things on a device: `./start.sh` (add `--one_star_is_unrated` if you want 1 star hidden), then open
`http://<this machine>:8080/` on the phone or tablet.
