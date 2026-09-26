# STATUS

Completed ticket pointers for the photo manager web app, moved out of `TODO.md` so that file can
stay short and hold only open work. Plan: `docs/plans/photo-manager-plan.md`; requirements:
`docs/reqs/photo-manager-requirements.md`. Tickets live in `docs/tickets/NNN.md`; epics (001-010,
one per build step in plan §7, plus 043 Mobile UI) hold checklists of their child tickets.

## Build order

- [x] [001](docs/tickets/001.md) Skeleton and schema
- [x] [002](docs/tickets/002.md) Scan and files table
- [x] [003](docs/tickets/003.md) Grouping
- [x] [004](docs/tickets/004.md) Thumbs inspection and lookup
- [x] [005](docs/tickets/005.md) XMP module and tests
- [x] [006](docs/tickets/006.md) Import and ratings
- [x] [007](docs/tickets/007.md) Read-only UI
- [x] [008](docs/tickets/008.md) Curation
- [x] [009](docs/tickets/009.md) Grouping UI
- [x] [010](docs/tickets/010.md) Hash recovery and nightly scan

All ten build-order epics are done.

## v1 filtering (user request 2026-09-21, done)

- [x] [055](docs/tickets/055.md) Rating filter buttons: All, Unrated, Rejected, exactly ★1..★5 (More menu keeps fav, conflict, picked)
- [x] [056](docs/tickets/056.md) A photo whose rating stops matching the filter disappears from the grid and the loupe advances (undo brings it back)
- [x] [057](docs/tickets/057.md) Counts on the filter buttons and grid shortcuts
- [x] [058](docs/tickets/058.md) Move the reject button to the left of the rating buttons (loupe HUD and selection bar)

## Toward functionally complete system (done so far)

- [x] [059](docs/tickets/059.md) Scrollable, tappable thumbnail strip below the main image (whole folder, also on phones)
- [x] [060](docs/tickets/060.md) Pinch zoom in/out and pan of the zoomed (original size) photo on tablets (real iPad: "works good")
- [x] [066](docs/tickets/066.md) Background low-priority population of /zoo/Thumbs with dcraw (queue visible on the Jobs page)
- [x] [067](docs/tickets/067.md) ArrowUp/ArrowDown to step the rating in the viewer
- [x] [072](docs/tickets/072.md) "Delete" button on the Rejected view: review screen with Medium thumbnails, big red Delete button removes the source files from /zoo/Pictures (needs design decisions first, see the ticket)
- [x] [073](docs/tickets/073.md) Load-adaptive background worker: watch CPU/memory load, auto start/stop a low-priority nice/ionice worker on the job queue
- [x] [074](docs/tickets/074.md) Jobs page progress indicator: total, incomplete, and completed in the last day/hour/minute
- [x] [075](docs/tickets/075.md) Delete completed jobs older than 1 week on a full rescan
- [x] [076](docs/tickets/076.md) Nightly rescan becomes one queued job per top-level directory, drained by [073](docs/tickets/073.md)'s worker
- [x] [077](docs/tickets/077.md) Filter/sort changes use replaceState so they are not their own browser-history stop (Back always means "previous folder")
- [x] [078](docs/tickets/078.md) Document the sqlite database(s) — location, schema, meaning — in docs/design/databases.md
- [x] [079](docs/tickets/079.md) Loupe "..."/Debug button to force-rerender a photo's thumbnails (fixes broken ones)
- [x] [080](docs/tickets/080.md) Prioritize the currently-open folder in the background thumbnail queue (spun out of 069's investigation)
- [x] [081](docs/tickets/081.md) Automatic purge of Pictures/.trash after a retention window (spun out of 072's decisions)
- [x] [082](docs/tickets/082.md) "Delete this file" button in the loupe's Files panel, with a Medium-thumbnail confirmation screen
- [x] [083](docs/tickets/083.md) Schema + backfill for camera metadata (aperture, shutter speed, ISO) — blocks 084
- [x] [084](docs/tickets/084.md) Show camera metadata in the Files detail view (blocked by 083)
- [x] [091](docs/tickets/091.md) Help screen (keyboard shortcuts), opened by 'h', '?' or F1 (user request 2026-09-23)
- [x] [092](docs/tickets/092.md) Switch the active filter from inside the loupe (click the "filter: …" HUD tag), staying on the same photo; non-matching filters grayed out (user request 2026-09-23)
- [x] [086](docs/tickets/086.md) "This folder" / "this folder + subdirectories" toggle for the grid (user request 2026-09-23)
- [x] [087](docs/tickets/087.md) Fav and tag-based filter buttons in the folder view (user request 2026-09-23)
- [x] [088](docs/tickets/088.md) Batch actions scoped to the current selection, including delete (user request 2026-09-23)
- [x] [089](docs/tickets/089.md) Export action: Huge-equivalent JPEGs of all/selected photos to a chosen folder (user request 2026-09-23)
- [x] [085](docs/tickets/085.md) Adjustable per-file RAW conversion settings with sliders and thumbnail regeneration — merged the on-demand and background RAW renderers onto rawpy/LibRaw (090's resolution); spun off ticket 093 as a follow-up optimization, not blocking
- [x] [101](docs/tickets/101.md) Ignore files by basename pattern, starting with `._*` (macOS AppleDouble junk never scanned into the database; a stale pre-fix row self-heals to missing=1 via the existing vanished-file path, no dedicated cleanup needed)
- [x] [095](docs/tickets/095.md) Shift+Click in the grid selects the range from a fixed anchor (last plain/Ctrl-click) to the newly clicked photo, replacing the selection; the ✓ button honors it too
- [x] [098](docs/tickets/098.md) Export target moves inside the library (`pictures_dir/Exported`), with a collision-safe destination naming scheme (part of epic 096)
- [x] [099](docs/tickets/099.md) Every export is imported and cross-referenced to its source (`files.exported_from_file_id`) the moment its job finishes, with a "exported from" link in the Files panel (part of epic 096)
- [x] [100](docs/tickets/100.md) One-off `photoapp/export_backfill.py`: links pre-existing exports to their originals via the still-live job log, then filename + dhash matching for the rest (part of epic 096)
- [x] [096](docs/tickets/096.md) Epic: exports live in the library, linked to their originals — done (097-100 all landed)
- [x] [093](docs/tickets/093.md) A settings-tuned RAW's first thumbnail request demosaics once at
      Huge and every later request for a different size downscales from that cached Huge, instead
      of a separate full demosaic per size (follow-up to 085/090)
- [x] [094](docs/tickets/094.md) RAW tuning controls only update local, pending state and request a
      provisional preview (`GET /api/files/{id}/raw_preview`, never written to `files.raw_*`/the
      thumbs cache); an explicit Save button commits (085's original behavior, unchanged); holding
      'c' compares the provisional overlay against the committed rendering underneath
- [x] [107](docs/tickets/107.md) Compare hotkey switched from plain 'c' (silently swallowed while
      a slider has focus) to Shift, checked ahead of `isTyping`'s guard since a bare modifier never
      types a character; the tuned overlay is now hidden by default and shown only while hovering
      the sliders block or holding Shift, replacing 094's "shown by default" model; a second,
      independent hover (a non-representative file's row in the Files panel) previews that file's
      own thumbnail via a new overlay element (`ui.rowPreview`)
- [x] [104](docs/tickets/104.md) Generate and cache a lossy, size-reduced tuning-preview DNG per
      RAW file (`photoapp/raw_preview_dng.py`, `GET /api/files/{id}/raw_preview_dng`) — real,
      undemosaiced Bayer data binned down to a size cap (103's answer), written via `tifffile`
      (a new dependency: rawpy can't write DNG); verified against the real vendored LibRaw-Wasm
      1.6.0 build (`photoapp/static/vendor/libraw-wasm/`), not just rawpy
- [x] [105](docs/tickets/105.md) Client-side RAW tuning: `photoapp/static/rawTuning.js`
      (`RawTuningSession`) loads the vendored LibRaw-Wasm module, fetches 104's preview DNG once
      per file, and re-renders locally on every slider tick (`open()` + `imageData()` painted onto
      a throwaway canvas, read back as a `blob:` URL) instead of 094's network `raw_preview`
      round trip, which becomes the fallback when local rendering isn't available
- [x] [106](docs/tickets/106.md) Save flow: already correct unchanged (still POSTs the same four
      parameters, still `thumbs.clear`s and re-renders from the original DNG) — this ticket turned
      out to be its two open design notes: parameter parity resolved as "restrict to today's four"
      (matches 105's `mapSettings`), and the "drift risk" between LibRaw-Wasm and rawpy checked for
      real, finding (and fixing, in 104) two real DNG-generation bugs — a `BlackLevel` tag needs
      its `BlackLevelRepeatDim` companion, and LibRaw's color matrix is driven by a recognized
      Make/Model, not by a DNG's own embedded `ColorMatrix1` (a LibRaw limitation, not a coding
      mistake to design around). After both fixes, the local preview and the backend-committed
      render agree within ~1-2% mean RGB against a real Pentax K-5 DNG.
- [x] [102](docs/tickets/102.md) Epic: client-side RAW tuning via LibRaw-Wasm — done (103-107 all
      landed)
- [x] [108](docs/tickets/108.md) Busy indicator (a small spinner, `.tuning-busy`) shown for the
      span of a local LibRaw-Wasm render, guarded by a sequence counter so a superseded call or
      navigating away mid-render can never leave it stuck visible (user request 2026-09-25)
- [x] [110](docs/tickets/110.md) Bug fix: the grid cell and filmstrip Thumb for a re-tuned RAW
      file (when it's the Photo's representative) never refreshed after Save — `/img/Thumb/
      {file_id}`'s URL doesn't change even though its bytes do, and the response is cached up to
      an hour; `grid.js`'s `refreshCellThumb`/`filmstrip.js`'s `refreshThumb` cache-bust them the
      same way `saveRawSettings` already did for the loupe's own main image (user bug report
      2026-09-25)
- [x] [109](docs/tickets/109.md) Exposure and shadow-pull as tunable RAW conversion parameters
      (user request 2026-09-25): two new `files.raw_exposure`/`raw_shadow` columns, `exp_shift` +
      `no_auto_bright` server-side and `expShift`/`expCorrec`/`noAutoBright` client-side, and a
      post-decode `v + a*(1-v)^2` shadow lift applied identically in `previews._lift_shadows` and
      `rawTuning.js`; verified the real vendored LibRaw-Wasm build matches rawpy on the K-5 DNG
      (mean RGB within 0.005%). Also fixed a pre-existing 105 bug: `mapSettings` read `raw_*` keys
      while the pending dict uses unprefixed ones, so the local preview had been ignoring every
      slider including brightness/WB/highlight.
- [x] [112](docs/tickets/112.md) Four advanced per-file RAW controls under a collapsible
      "Advanced" zipper: demosaic algorithm (`user_qual`/`userQual`), FBDD noise reduction
      (`fbdd_noise_reduction`/`fbddNoiserd`), plus post-decode saturation and contrast applied
      identically in `previews._apply_saturation`/`_apply_contrast` and `rawTuning.js`. Native
      `user_sat` (a white-point override, not saturation) and native gamma were tested and
      rejected — the vendor's libraw-wasm 1.6.0 build silently ignores its documented `gamm`
      setting, so exact parity wasn't achievable that way. Verified on the real K-5 DNG.
- [x] [118](docs/tickets/118.md) Bug fix: initial client-side RAW rendering image was displayed with
      incorrect orientation because `raw_preview_dng.py` omitted tag 274 (`Orientation`) in the
      generated preview DNG, defaulting LibRaw-Wasm's flip to 0 (landscape). Now preserves EXIF
      orientation tag (falling back to LibRaw's `sizes.flip`), so client renders in matching orientation.

- [x] [111](docs/tickets/111.md) Full-scan metadata cache: `--write_metadata_json` (on by default)
      writes a per-directory `index.json` and per-file `<name>.json` next to each image, reused on a
      later scan (even after the database is rebuilt); new EXIF fields focal length and camera
      make/model (three new `files` columns) shown in the Files panel. Machinery lifted into
      `photoapp/metacache.py` (shared with `file_metadata.py`), scanner ignores the JSON files, and
      the directory mtime is corrected after writing so an untouched directory still skips.

- [x] [113](docs/tickets/113.md) Active job reporting on the Jobs page: nullable `jobs.started_at`
      set when a worker claims a job (cleared when a restart requeues it), `JobQueue.active()`
      returning busy state plus every running job's kind/path/start/duration, exposed on
      `/api/jobs`, and a `Worker: Idle` / `Worker: Active — …` status line on the Jobs page.

- [x] [114](docs/tickets/114.md) Recent job activity columns on the Jobs page: `JobQueue.progress()`
      adds per-kind done/failed counts for all time, 1 day, 1 hour and 1 minute, and the page
      replaces the old summary line with a grouped-header table (Queued, Running, Done/Failed per
      window).

- [x] [121](docs/tickets/121.md) Enhancement: recenter loupe image and flip/tuning overlay when
      files/tuning panel is open so neither the main photo nor comparison previews are obstructed
      by the side panel. Also shifts the next button and busy spinner into the unobstructed area.

- [x] [115](docs/tickets/115.md) Non-destructive crop mode (JPEG and RAW): normalized
      `files.crop_x/y/w/h`, `photoapp/crop.py`, `POST /api/files/{id}/crop` (clears cached
      thumbnails), Thumb/Small rendered cropped while Medium/Huge stay full with the cropped-out
      area shaded in the loupe, plus an interactive crop rectangle with drag handles and
      Save/Discard.

- [x] [117](docs/tickets/117.md) Multi-state star filters: `rating>=N`/`rating<=N` over the 1..5
      star scale (server + `filters.matches`), grid star buttons cycling `=`/`≥`/`≤` with the
      label/count following the state, and a 3-column `≤ N`/`= N`/`≥ N` grid per star value in the
      loupe filter picker.

- [x] [119](docs/tickets/119.md) Bug fix: a tuned file's thumbnails were re-served from the
      browser cache (max-age 3600) when navigating away and back, because the `file_id` URL never
      changed. New `files.thumb_rev` (bumped by raw_settings/crop saves) is returned in photo
      payloads and save responses; `api.js` keeps a per-file revision map (seeded on every load,
      updated on save) and `imgUrl` appends `?r=<rev>`, so every image URL (loupe, zoom, preload,
      grid, filmstrip) fetches the fresh render.

- [x] [120](docs/tickets/120.md) Bug fix: the Delete link to the review page dropped the
      "this folder + subfolders" mode, so a subfolder's rejected photos disappeared. `hrefPage`/
      `route.parse` now carry `recursive` for `!` pages, the header/grid hand-off passes the mode,
      and the review page fetches and links back recursively.

- [x] [122](docs/tickets/122.md) Free-form tag filter: the tag dropdown's "Other tag…" option opens
      an autofocused input (Enter/Filter applies `tag:<typed>`, Escape/Cancel restores), and an active
      tag not among the view's aspects still appears as the selected option.
- [x] [123](docs/tickets/123.md) Dot-tags unhide hidden directories and imply a tag from the
      directory name: `tag:.name` includes dot-directories in the query scope, matches an explicit
      tag or the path segment, and list/detail responses carry an `implied` list that `filters.matches`
      and the loupe HUD use.

- [x] [126](docs/tickets/126.md) Bug fix: "Rescan" crashed with `no item with that key` because
      `_scan_files`'s ticket 111 cache path read `old["hash"]` from a `_lookup_files` call that did
      not select `hash` (hit whenever a DB existed without an `index.json` yet). The lookup now
      includes `hash`; regression test added.

- [x] [127](docs/tickets/127.md) Rescan honours the current folder's scope: `/api/scan` takes a
      `recursive` query param, `app.js`'s `rescan` posts the browse route's `route.recursive`
      (root `.` included), and only the root + recursive case still routes to `scan_all`.

- [x] [124](docs/tickets/124.md) Bug fix: the rejected-deletion confirmation screen fetched only the
      first 1000-photo page (`limit=1000`, never paged), so an oversized "This folder + subfolders"
      scope dropped the rejected Photos past it -- often the ones in deeper subfolders. The header
      flow now pages through `/api/photos` until every rejected Photo in scope is collected; the
      toggle, selection hand-off and reload/direct-URL paths were retested and are correct.

- [x] [125](docs/tickets/125.md) Export always applies the file's crop: `export_file` keeps the
      exact old `thumbs.ensure` + copy path for an uncropped file, and for a cropped one renders
      and crops in one pass straight from the original (`thumbs.render` with the source's
      `crop.pixel_box` rectangle, never the full-frame lossy Huge). The imported export row starts
      with all `crop_*` columns `NULL`, so it is not double-cropped.

No open tickets. See `TODO.md`.

## Cross-cutting

- [x] [040](docs/tickets/040.md) Test infrastructure and fixtures
- [x] [041](docs/tickets/041.md) start.sh to bring up a local server
- [x] [043](docs/tickets/043.md) Mobile UI (tried on an iPad: "worked well"; an Android phone specifically is still untried)
- [x] [047](docs/tickets/047.md) Dark theme ("looks good for now, thanks!")
- [x] [051](docs/tickets/051.md) Incremental scan on subdirectories, run in sequence per year directory
- [x] [052](docs/tickets/052.md) Retry with backoff on 'database is busy' (about a minute), then a 500 shown as a toast
- [x] [054](docs/tickets/054.md) Do not show folders whose names start with a dot in the folder view
- [x] [050](docs/tickets/050.md) Preload the big image data of photos within +/-2 of the current one (faster transitions)
- [x] [048](docs/tickets/048.md) Library scan and reports (whole library scanned 2026-09-21: 94,616 files, 66,352 Photos)

## Not blocked, not yet started (done)

- [x] [068](docs/tickets/068.md) Batch sync of XMP sidecars out of sync with the computed rating (future; answer to 065)
- [x] [069](docs/tickets/069.md) Investigate: opening a folder for the first time feels slow (observation from the 063 culling trial)
- [x] [070](docs/tickets/070.md) vlog(1..7) logging in the populate_thumbs tool (user request 2026-09-22)
- [x] [071](docs/tickets/071.md) vlog(1..7) logging in the scan and in-line thumbnail paths (user request 2026-09-22)

## Question tickets, answered and closed

Convention: a ticket that needs the user's input gets a **question ticket** (`Type: question`, with brief
context, the question, options if any, and an empty **Answer** section). The question lists what it
`Blocks:`; the blocked ticket has a `Blocked by:` line. When the user has answered, the question ticket is
closed and the blocked ticket continues. A ticket is "actionable" when it has no open blocker. Open
question tickets are tracked in `TODO.md`, not here.

- [x] [061](docs/tickets/061.md) Try the phone UI on a real phone → tried on iPad, "worked well"; unblocked 044, 045, 046
- [x] [062](docs/tickets/062.md) Try zoom gestures on an iPad / Android tablet → "Works good, thanks!"; unblocked 060
- [x] [063](docs/tickets/063.md) Try culling a real folder and tell how it feels → no issues (folder-open speed noted, see 069); unblocked 031
- [x] [064](docs/tickets/064.md) Have a look at the dark theme → "Looks good for now, thanks!"; unblocked 047
- [x] [065](docs/tickets/065.md) How should ratings newer in the database be written to the sidecars? → on the next edit only (already correct); unblocked 021; a future batch sync is ticket 068
- [x] [090](docs/tickets/090.md) Should 085's adjustable RAW settings affect only the background dcraw cache, or the on-demand rawpy path too? → neither as originally framed: merge the two renderers into one (rawpy/LibRaw for both), with a revised Thumb-size rule; unblocked 085, superseded the "not to be unified" framing in docs/design/thumbnails.md
- [x] [097](docs/tickets/097.md) Merge an exported file into the original's Photo, or keep it as its own browseable Photo with a separate "jump to original" cross-reference? → Option B (own Photo + new `files.exported_from_file_id` column); unblocked 098-100 (epic 096)
- [x] [103](docs/tickets/103.md) How should the lossy tuning-preview DNG (epic 102) actually be produced? → user leaned Option B pending a check of what LibRaw-Wasm supports; that check ruled B out (it needs real Bayer data, not a pre-demosaiced one) and settled on Option C: a downsampled, still-mosaiced DNG via rawpy + a new `tifffile` dependency; unblocked 104 (done) and 105
- [x] [116](docs/tickets/116.md) How should non-destructive crop interact with thumbnail generation and exports? → Thumb size cropped only to selected rectangle; Medium/Huge in loupe shows full image with cropped-out areas shaded dark; Small in grid cropped, or shaded if single image. Unblocks 115.

Answered earlier: 053 (a single file's reject never rejects the pair while the other is picked; app rejects
newest-wins), 015 (JSONL mirror), 023 (write both sidecars, newest-wins), 024 (remember previous stars), 042
(`NAME.DNG.xmp`), 046 (swipe mapping), 049 (`--one_star_is_unrated`), 054 (hide dot folders).
