# Thumbnails

Code: `photoapp/thumbs.py`, `photoapp/previews.py`, `photoapp/thumb_populate.py`,
`photoapp/populate_thumbs.py`, `photoapp/raw_settings.py`.

## One RAW renderer, used by both paths (ticket 085/090)

There are two separate places a RAW file's thumbnail can get made — the on-demand path
(`photoapp/previews.py`, ticket 028, when a photo is opened and a size is missing) and the
background bulk populator (`photoapp/thumb_populate.py`, ticket 066, a low-priority process that
walks the whole library filling in every missing size ahead of time) — but as of ticket 085 they
call the **same renderer** (`previews.embedded_preview`/`previews.render`, `rawpy`/LibRaw) with the
**same per-file settings** (`photoapp/raw_settings.py`). This used to not be true: the background
path shelled out to the external `dcraw` binary instead, a second RAW renderer with its own
tool-specific behavior, kept deliberately separate (see the history note below). That split had to
go once a photo's thumbnails needed to look identical regardless of which path rendered them
first — ticket 085's whole point (per-file brightness/white-balance/highlight-recovery settings)
would otherwise silently depend on whichever path won the race for a given size.

Both paths still write into the exact same `/zoo/Thumbs/<Size>/<path>.jpg` layout and the same
`thumbs` table, and **both still respect "never overwrite an existing thumbnail"** — whichever path
gets there first for a given file and size wins, and the other simply has nothing left to do for it.

<details>
<summary>History: why they were two renderers, and why that stopped being true</summary>

The on-demand path (`previews.py`) uses `rawpy`/LibRaw, a Python library — no subprocess, and fast
(an embedded preview extracts in ~0.02s; a demosaic when there's no usable embedded preview is
~0.7s). This replaced the plan's original `exiftool` + `dcraw` design specifically because neither
binary was installed on the development machine and `rawpy` needed nothing beyond `pip install`.

The background populator (ticket 066, explicit user request) used the external `dcraw` binary
instead — requested by name, matching what the original plan (§4.5) had specified before the
on-demand path switched away from it (`dcraw -e -c` for the embedded `Thumb` preview, `dcraw -c -h
-w` for `Small`/`Medium`, `dcraw -c -w -q 3` for the full-size `Huge`). At the time this was
considered deliberate, not duplication to merge: the two paths solve different problems (respond to
a request now vs. work through a backlog patiently) with different constraints (no subprocess/binary
dependency vs. matching the originally-specified tool). Ticket 090's resolution to ticket 085
reversed that: "the split of dcraw in batch and LibRaw in online processing is a legacy artifact,
not an intended state." `dcraw` is no longer used anywhere in this app; `previews.render`'s `rawpy`
`postprocess()` call covers every setting ticket 085 scoped (brightness, white balance, highlight
recovery all have direct LibRaw equivalents).
</details>

## The embedded-preview shortcut is per-file and per-size, not fixed to `Thumb` (ticket 085)

A RAW file's embedded preview (the camera's own already-baked JPEG, extracted via
`previews.embedded_preview`) is not demosaiced at all — a per-file setting (`raw_settings.py`:
brightness, white balance mode, highlight recovery, exposure, shadow, and the advanced saturation,
contrast, noise-reduction and demosaic controls) can only have a visible effect on a render that
actually ran LibRaw's `postprocess()`. So the rule `thumbs._open`/`make` follow, for **every** size,
not just `Thumb`:

- While a file's settings are all at default (every column in `raw_settings.py`'s `COLUMNS` is
  `NULL`), the embedded-preview shortcut is used at every size — `Thumb`/`Small`/`Medium`/`Huge` are
  all just different downscales of the same free, ~20ms extraction. This is why, for the common
  case of an untouched RAW photo, browsing a RAW-heavy folder is not noticeably slower than a
  JPEG-heavy one even before the background populator has caught up.
- The moment a file's settings are tuned away from default, every size (that isn't already cached —
  the "never overwrite" rule above still applies) demosaics instead, applying those settings. A
  settings change clears every cached size first (`thumbs.clear`, ticket 079's mechanism, reused by
  `POST /api/files/{id}/raw_settings`) specifically so nothing stale from before the change survives
  to be found by the "downscale from a larger cached size" step below.
- A RAW with no usable embedded preview at all demosaics regardless of settings — there is no faster
  path available for it either way (`render_raw_sizes`, the `raw_render` job queue fallback).

Earlier drafts of ticket 085 assumed only `Thumb` ever took the embedded-preview shortcut (matching
the *old*, dcraw-based background path, which really did always demosaic `Small`/`Medium`/`Huge`).
That turned out not to describe the on-demand path at all — it already used the shortcut at every
size — and generalizing the shortcut to every size (rather than making `Small`/`Medium`/`Huge`
always demosaic) was a deliberate choice to keep today's browsing speed for untouched photos,
confirmed directly rather than assumed.

See ticket [093](../tickets/093.md) for a follow-up not yet implemented: when a demosaic does have
to happen, doing it once at `Huge` and downscaling the smaller sizes from that, instead of
demosaicing separately per requested size.

## Non-destructive crop changes which sizes are cropped (ticket 115)

A file's crop (`photoapp/crop.py`: normalized `crop_x`/`crop_y`/`crop_w`/`crop_h` on `files`, all
`NULL` = whole frame) changes the rule per size, per ticket 116's answer:

- `Thumb` and `Small` (the grid/filmstrip sizes) are rendered **cropped** — the crop is applied in
  `thumbs._open` before the downscale, so the intended composition is what the grid shows. A
  cropped file always renders these straight from the original: every larger cached size is
  full-frame (see below), so the "downscale from a larger cached size" shortcut above cannot be
  used for them.
- `Medium` and `Huge` (the loupe's large and zoomed views) stay **full-frame**; the client shades
  the cropped-out part (`loupe.js`'s `.crop-shade`). This is also why the `raw_render` job's
  `render_raw_sizes` crops only `Thumb`/`Small` from its one demosaic and leaves `Medium` full.
- Saving a crop (`POST /api/files/{id}/crop`) clears every cached size first, exactly like a
  raw-settings save, so a stale uncropped/cropped `Thumb` is never reused. Crop applies to JPEG and
  RAW alike.
- An **export** (ticket 125) is a finished, out-of-app artifact, so the crop is always applied even
  though `Huge` itself stays full-frame: an uncropped file copies the `Huge` JPEG byte-for-byte,
  while a cropped one is rendered and cropped in one pass straight from the original via
  `thumbs.render` (never the full-frame, lossy `Huge`). The same rectangle as `Thumb`/`Small`.
  Because the crop is baked into the export's pixels, its own imported `files` row starts with all
  `crop_*` columns `NULL` (a fresh row never inherits them), so viewing the export does not crop it
  a second time.

## Non-destructive rotation is the final transform, after metadata orientation (ticket 129)

A file's rotation (`photoapp/rotation.py`: `files.rotation`, degrees counter-clockwise, normalized
to 90/180/270, `NULL`/0 = none) is applied **last**, by `thumbs._finish`/`_open` after both the
metadata orientation and the crop, for every size (`Thumb`/`Small`/`Medium`/`Huge`), for JPEG and
RAW alike. The fixed order is therefore:

1. **Metadata orientation** — `ImageOps.exif_transpose` for a non-RAW file, `previews._oriented`/
   LibRaw's `sizes.flip` for a RAW's embedded preview. This is the camera/file's own claim about
   how the frame should be presented and is never modified by this feature.
2. the per-file RAW conversion settings, when the file is a demosaiced RAW (ticket 085);
3. the **crop** (ticket 115), if any;
4. the **rotation** (ticket 129), expanding the canvas.

Keeping rotation separate from step 1 is the point: the button exists to correct a file whose
metadata orientation is wrong or inconsistent, so it must not be folded into (or confused with)
that orientation. `expand=True` means a 90/270 turn swaps the frame's width and height losslessly.

Saving a rotation (`POST /api/files/{id}/rotation`) clears every cached size and bumps `thumb_rev`,
exactly like a crop or raw-settings save, so no stale unturned render survives. Because rotation is
the final transform, `thumbs.make` must not apply it again when it downscales from a larger *cached*
size (that render already has it baked in) — it passes the rotation only for a render straight from
the original. On `/img/Huge` for a non-RAW file, a rotated file cannot be served straight from the
original (that would ignore the rotation), so it takes the render path instead of the byte-for-byte
original shortcut. An **export** applies the rotation for real (ticket 129, alongside ticket 125's
crop): a rotated file is re-rendered rather than copied.

On the client, `loupe.js` keeps the crop shade aligned by turning the stored source-frame crop
rectangle into the displayed frame (`rotateRect`), and the crop editor draws in the displayed frame
and turns the rectangle back before saving, so crop and rotation compose correctly.

## The layout facts came from measuring the real tree, not from a spec

The pixel sizes per named size tier (`Thumb` 300px, `Small` 1000px, `Medium` 2000px, `Huge` = full source size)
and the filename-extension mapping (`X.DNG` → `X.DNG.jpg`, `X.JPG`/`X.jpg` → `X.jpg`, anything else keeps its
extension in front: `X.png` → `X.png.jpg`) were not designed — they were **measured** against the pre-existing
`/zoo/Thumbs` tree (ticket 016, written up in `docs/reqs/thumbs-layout.md`) so the app's own generation would
match what was already there and be indistinguishable from it. That inspection also found the pre-existing tree
only covers 1998–2020 plus a few named exceptions; **2021 onward had no thumbnails at all** before ticket 066,
which is the direct motivation for the background population feature.

## Lookup order, and why a smaller-than-requested size is never silently upscaled

`thumbs.make`'s order — existing exact file, then downscale from a larger *existing* size, then render from the
original — exists so that if `Huge` already exists (because someone ran the background populator, or it was
already on the NAS), asking for `Thumb` costs a cheap downscale instead of a second full decode of the RAW.
`render`/`save` never upscale: a source image smaller than the requested long edge keeps its own size, matching
what the pre-existing tree already does (verified during the layout inspection) rather than introducing
interpolated blur that wasn't there before.

A "downscale from a larger cached size" source is already a plain JPEG — whatever settings were (or weren't)
baked in when that larger size was itself produced — so `make` doesn't need to re-check settings for that step;
it only matters when rendering straight from the original RAW file. This is safe specifically because a settings
change clears every cached size at once (see above): a smaller size can never end up downscaled from a larger
one that was rendered under different, stale settings.

## Job-queue generation, not inline, for anything that needs a real RAW decode

The web request handler for `/img/{size}/{file_id}` renders inline (in a thread pool, not blocking the event
loop) whenever a cheap render is possible — an existing file, a downscale, a non-RAW Pillow decode, or a RAW's
embedded preview. It only falls back to the background job queue (`raw_render`, capped at
`settings.job_workers`, default 2) for the one case that's actually slow: a RAW with **no usable embedded
preview at all**, which needs a full LibRaw demosaic. The route answers `404` with `Retry-After` while that job
runs, and the frontend already retries automatically (see [frontend-viewer.md](frontend-viewer.md)). This
split — cheap paths inline, only the genuinely slow path queued — is why the job queue's concurrency
(`job_workers`) rarely matters in practice: most requests never touch it.

## Provisional tuning renders never touch this cache (ticket 094)

`GET /api/files/{id}/raw_preview` (added for the tuning UI's live slider feedback) is deliberately
outside every mechanism above: it renders straight from the original with whatever *pending*
settings the frontend passes as query params, at half size for responsiveness, and returns the
JPEG bytes directly — no `thumbs` table row, no file under `thumbs_dir`, no `files.raw_*` write.
Committing (`POST .../raw_settings`, unchanged since 085) is still the only thing that reaches this
cache. That split — preview vs. commit, provisional render vs. persisted one — is also the seam a
future browser-local renderer would slot into: if adjusting brightness/white-balance/highlight/exposure/shadow in
the browser (canvas or WASM, operating on an already-downloaded preview-quality image) turns out
fast enough for some settings, it would replace `raw_preview`'s backend round trip for those
settings without touching `set_raw_settings`/the commit path at all, since the frontend already
treats "pending settings -> some preview image" as its own step, decoupled from persistence. Not
implemented; noted here so the seam isn't accidentally welded shut later.

Filed as [ticket 102](../tickets/102.md) (epic, 2026-09-24): LibRaw-Wasm in the browser, fed a
lossy/size-reduced DNG rather than an "already-downloaded preview-quality image" as guessed above
(a plain preview image would already be demosaiced, losing the ability to tune white
balance/highlight recovery against real Bayer data) — see [103](../tickets/103.md) (open question:
how to actually produce that lossy DNG, since rawpy has no DNG-writing API) through
[106](../tickets/106.md). Not implemented; this paragraph is the seam described, that ticket is
the plan for actually building it.

## Videos (epic 184)

- **Stills** (ticket 188): the same four sizes in the same tree (`Thumb/NAME.mp4.jpg`). One frame from
  10% into the clip (then 25/40/60%, then the start) -- the first that is not near-black. ffmpeg applies
  the rotation tag; the pixel aspect ratio is made square. The images are clean frames: the "this is a
  video" play indicator is a client-side SVG overlay, never drawn into a JPEG. If no frame can be
  decoded (ffmpeg missing, broken file) `thumbs.make()` returns a shared flat placeholder
  (`<thumbs_dir>/_placeholder/<Size>.jpg`, source `placeholder`) that is **never recorded** in the
  `thumbs` table, so the real thumbnail is made once the problem is gone.
- **Animations** (tickets 189, 201): `AnimThumb` (300 px, 12 fps, the hover preview) and `AnimSmall` (640 px, 15 fps, shown when the browser cannot play the original), muted
  VP9 WebM at `<thumbs_dir>/AnimThumb/NAME.mp4.webm`. `AnimThumb`: eight 1 s fragments (a clip of 8 s or less whole); `AnimSmall`: the whole clip up to 60 s,
  else five 15 s fragments (they shrink to tile a 60-75 s clip). Fragments are each centred in one of
  equal slices of the clip (input-side `-ss` per fragment, so a long file on the NAS is not read
  end to end); a clip of 8 s or less is used whole. They are `thumbs` rows with those size names but
  are not in `thumbs.SIZES`; `thumbs.ANIM_SIZES` lists them and `clear()`/`move_thumbnails()` cover
  them. `source` holds the size's recipe (`video_thumbs.ANIM_RECIPES`); bump it and the populator's
  `invalidate_stale()` removes the older ones so they are remade (ticket 201).
- **Population** (ticket 190): `thumb_populate` extracts one frame for all missing stills, then makes
  the animations, one video at a time on the existing low-priority single-worker queue (ffmpeg runs
  under `nice`; 120 s per frame, 600 s per animation). A failure is stored in `video_failures` and the
  video is skipped for 7 days or until the file changes; `thumbs.clear()` (a forced re-render) removes
  the record. With no ffmpeg no video is queued at all. On-demand requests make only what was asked
  (a single frame for the grid's Thumb).
- **Measured 2026-10-08** on 8 random videos of the real library (mp4/mov, 1-390 MB, 2-309 s, read
  from the NAS): the four stills 0.3-0.4 s together; AnimThumb 0.4-3.0 s (6-223 KB); AnimSmall
  0.6-9.9 s (30 KB-1.1 MB) -- that was the old 8 x 1 s `AnimSmall`. With ticket 201's `AnimSmall` (whole
  clip up to 60 s, else 5 x 15 s): 45 s clip 1.2 MB / 18 s, 199 s clip 2.6 MB / 48 s. So a video now takes
  about 25-60 s in all, and the ~770-video backlog roughly 4-5 hours on one worker.
