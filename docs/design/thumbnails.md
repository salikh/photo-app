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
