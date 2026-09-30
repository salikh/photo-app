# TODO

Open task list for the photo manager web app. **All completed ticket pointers have been moved to
`STATUS.md`** — this file holds only open tickets and open questions. Plan:
`docs/plans/photo-manager-plan.md`; requirements: `docs/reqs/photo-manager-requirements.md`.
Tickets live in `docs/tickets/NNN.md`. Check an item off here (and move its pointer to
`STATUS.md`) when the ticket's status is done.

## Open tickets

Epic 160 (PEF / Pentax RAW support), user request 2026-09-30:

- [ ] [160](docs/tickets/160.md) Epic: PEF support — Pillow cannot open a PEF, so all of its EXIF
      (date, camera, lens, exposure) is NULL; add a TIFF IFD fallback reader and backfill
- [ ] [161](docs/tickets/161.md) Fallback EXIF reader (`fileinfo.read_exif_from_tiff`) wired into
      `read_image_metadata` for RAW files Pillow cannot open
- [ ] [162](docs/tickets/162.md) Re-extract stale `image/x-raw` cache records so a scan self-heals
      existing PEF rows (`metacache.record_is_stale`)
- [ ] [163](docs/tickets/163.md) `backfill_exif` fills the full EXIF set (works for PEF) + docs

Everything else — epic 156 (lens metadata, 157-159), the ten build-order epics, v1 filtering, every
cross-cutting ticket, epic 102 (103-107), epic 131 (132-138), epic 144 (145-148), tickets 139-143,
149-155 and tickets 108-115, 117-124, 127, 128 and 130 — is done (see `STATUS.md`).

## Restart notes

- Work in progress: epic 160 (PEF support), tickets 161-163 open. Epic 156 (lens metadata,
  157-159) is the most recent completed work; see `STATUS.md` for that and everything before it.
  New requests should be filed as tickets in `docs/tickets/NNN.md` first.
- Version control is **jj** (colocated with git); commit each logical step with `jj commit -m ...
  <paths>`, ending the message with the `Co-Authored-By` line. `jj config` for this repo raises
  `snapshot.max-new-file-size` to 2 MiB (the vendored `libraw.wasm` is 1.4 MiB).
- Config: `./photos.toml` (gitignored, holds the `/zoo` paths; template `photos.example.toml`) is
  picked up by `./start.sh` and every `python -m photoapp...` run from the checkout; use
  `--config=none` to ignore it.
- Tests: `.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q` (about 453 tests, ~1-2 min) and
  `REAL_DNG=/zoo/.Trash-1000/files/K___2502.DNG .venv/bin/python -m pytest tests/e2e/test_ui.py -q`
  (~12-20 min; run it in the background). `REAL_DNG` is a real Pentax K-5 DNG usable read-only;
  never write to `/zoo/Pictures`, `/zoo/Thumbs` or real sidecars when testing.
- The machine is often under memory pressure (a `./start.sh` instance of the app runs alongside).
  Known-flaky e2e tests, not regressions: `test_delete_this_file_button_shows_modal_and_moves_to_trash`
  and `test_delete_this_file_modal_cancel_leaves_file_untouched` (30 s click-stability timeout), and
  occasionally the first test of a small `-k` selection; rerun to confirm before chasing.
  `test_double_tap_zooms_to_the_tapped_point` fails consistently (not flaky) as of 2026-09-29 on
  this machine — a real, pre-existing bug, not yet investigated. Full-size `rawpy.postprocess()`
  on the real DNG can take minutes under load — use `half_size=True` for quick checks.
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

132 and 136 were answered 2026-09-27 (see `docs/tickets/132.md`/`136.md`) and unblocked 133/137
(both now done; see `STATUS.md`).

116 was answered 2026-09-25 (Thumb cropped to selected rect; loupe Medium/Huge full with dark shading; Small cropped in grid / shaded single; see `STATUS.md`) and unblocked 115. 103 was answered 2026-09-24 (Option C, a downsampled still-mosaiced DNG via rawpy+tifffile, refined
from the user's initial Option B lean after checking what LibRaw-Wasm actually supports; see
`STATUS.md`) and unblocked 104 (done) and 105. 097 was answered 2026-09-24 (Option B; see
`STATUS.md`) and unblocked 098-100. 090 was answered 2026-09-23 (see `STATUS.md`) and unblocked 085.

To try things on a device: `./start.sh` (add `--one_star_is_unrated` if you want 1 star hidden), then open
`http://<this machine>:8080/` on the phone or tablet.

## Feature requests (to file new tickets)

*No open feature requests right now (dot-tag hidden directory request filed as [123](docs/tickets/123.md)).*
