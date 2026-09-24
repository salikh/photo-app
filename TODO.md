# TODO

Open task list for the photo manager web app. **All completed ticket pointers have been moved to
`STATUS.md`** — this file holds only open tickets and open questions. Plan:
`docs/plans/photo-manager-plan.md`; requirements: `docs/reqs/photo-manager-requirements.md`.
Tickets live in `docs/tickets/NNN.md`. Check an item off here (and move its pointer to
`STATUS.md`) when the ticket's status is done.

## Open tickets

All ten build-order epics, v1 filtering, and every cross-cutting ticket are done (see
`STATUS.md`). What's left:

- [ ] [102](docs/tickets/102.md) Epic: client-side RAW tuning via LibRaw-Wasm (user request
      2026-09-24) — a lossy, size-reduced DNG downloaded once per tuning session, decoded and
      re-rendered locally in the browser as the user adjusts settings, no per-tick backend round
      trip; Save still commits and re-renders from the original DNG server-side, as today
  - [ ] [103](docs/tickets/103.md) (question) blocks 104-106
  - [ ] [104](docs/tickets/104.md) blocked by 103
  - [ ] [105](docs/tickets/105.md) blocked by 104
  - [ ] [106](docs/tickets/106.md) blocked by 105

## Question tickets (waiting on the user)

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues (and its pointer moves to `STATUS.md`). A ticket is "actionable"
when it has no open blocker.

- [ ] [103](docs/tickets/103.md) How should the lossy tuning-preview DNG (epic 102) actually be
      produced? Blocks 104-106.

097 was answered 2026-09-24 (Option B; see `STATUS.md`) and unblocked 098-100. 090 was answered
2026-09-23 (see `STATUS.md`) and unblocked 085.

To try things on a device: `./start.sh` (add `--one_star_is_unrated` if you want 1 star hidden), then open
`http://<this machine>:8080/` on the phone or tablet.
