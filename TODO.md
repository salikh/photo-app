# TODO

Open task list for the photo manager web app. **All completed ticket pointers have been moved to
`STATUS.md`** — this file holds only open tickets and open questions. Plan:
`docs/plans/photo-manager-plan.md`; requirements: `docs/reqs/photo-manager-requirements.md`.
Tickets live in `docs/tickets/NNN.md`. Check an item off here (and move its pointer to
`STATUS.md`) when the ticket's status is done.

## Open tickets

All ten build-order epics, v1 filtering, every cross-cutting ticket, epic 102 (103-107) and tickets
108-114, and 118 are done (see `STATUS.md`). What's left, in the order to pick it up:

- [ ] [115](docs/tickets/115.md) Ready to start (unblocked by [116](docs/tickets/116.md) answer) — non-destructive
      crop mode in photo editing.
- [ ] [117](docs/tickets/117.md) Not started — multi-state filter buttons cycling =, >=, <= and 3-column
      filter selector in loupe mode.
- [ ] [119](docs/tickets/119.md) Bug — tuned RAW rendering sometimes is not applied when switching images.
- [ ] [120](docs/tickets/120.md) Bug — deletion confirmation page does not support "this folder and subfolders" mode.

## Restart notes

- Version control is **jj** (colocated with git); commit each logical step with `jj commit -m ...
  <paths>`, ending the message with the `Co-Authored-By` line. `jj config` for this repo raises
  `snapshot.max-new-file-size` to 2 MiB (the vendored `libraw.wasm` is 1.4 MiB).
- Tests: `.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q` (about 300 tests, ~30 s) and
  `REAL_DNG=/zoo/.Trash-1000/files/K___2502.DNG .venv/bin/python -m pytest tests/e2e/test_ui.py -q`
  (~12-20 min; run it in the background). `REAL_DNG` is a real Pentax K-5 DNG usable read-only;
  never write to `/zoo/Pictures`, `/zoo/Thumbs` or real sidecars when testing.
- The machine is often under memory pressure (a `./start.sh` instance of the app runs alongside).
  Known-flaky e2e tests, not regressions: `test_delete_this_file_button_shows_modal_and_moves_to_trash`
  and `test_delete_this_file_modal_cancel_leaves_file_untouched` (30 s click-stability timeout), and
  occasionally the first test of a small `-k` selection (e.g. `test_raw_settings_are_provisional_
  until_save`); rerun to confirm before chasing. Full-size `rawpy.postprocess()` on the real DNG can
  take minutes under load — use `half_size=True` for quick checks.
- RAW tuning architecture (epic 102, done): `raw_preview_dng.py` generates a downsampled mosaiced
  preview DNG (needs the source's real Make/Model and a `BlackLevelRepeatDim` tag, see its
  comments); `static/rawTuning.js` renders it locally with the vendored LibRaw-Wasm; `previews.py`
  renders the committed thumbnails server-side. Any new tuning parameter must be added to **both**
  `previews._postprocess_kwargs` and `rawTuning.mapSettings` and verified against a real RAW file
  (that is how the two DNG bugs in 106 were found).

## Question tickets (waiting on the user)

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues (and its pointer moves to `STATUS.md`). A ticket is "actionable"
when it has no open blocker.

*No open question tickets right now.*

116 was answered 2026-09-25 (Thumb cropped to selected rect; loupe Medium/Huge full with dark shading; Small cropped in grid / shaded single; see `STATUS.md`) and unblocked 115. 103 was answered 2026-09-24 (Option C, a downsampled still-mosaiced DNG via rawpy+tifffile, refined
from the user's initial Option B lean after checking what LibRaw-Wasm actually supports; see
`STATUS.md`) and unblocked 104 (done) and 105. 097 was answered 2026-09-24 (Option B; see
`STATUS.md`) and unblocked 098-100. 090 was answered 2026-09-23 (see `STATUS.md`) and unblocked 085.

To try things on a device: `./start.sh` (add `--one_star_is_unrated` if you want 1 star hidden), then open
`http://<this machine>:8080/` on the phone or tablet.

## Feature requests (to file new tickets)

* When filter is to search by tag, and the tag starts with a dot ('.'), during scanning of the matching files include the dot-starting hidden directories. In other words, tags starting with a dot unhide the hidden directories. Also, for the files in a hidden directory, assume they are tagged with the tag equal to the hidden directory name, even if the tag is not written directly into the metadata.
