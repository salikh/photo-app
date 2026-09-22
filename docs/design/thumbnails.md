# Thumbnails

Code: `photoapp/thumbs.py`, `photoapp/previews.py`, `photoapp/thumb_populate.py`, `photoapp/populate_thumbs.py`.

## Two different RAW rendering paths — deliberately, not by accident

There are **two separate code paths that render a RAW file into a JPEG thumbnail**, using two different tools,
and that is intentional rather than duplication that should be merged:

- **On-demand** (`photoapp/previews.py`, ticket 028): when a photo is opened and a size is missing, it is
  rendered synchronously from the embedded preview via `rawpy`/LibRaw — a Python library, no subprocess, and
  fast (an embedded preview extracts in ~0.02s; a demosaic when there's no usable embedded preview is ~0.7s).
  This replaced the plan's original `exiftool` + `dcraw` design specifically because neither binary was
  installed on the development machine and `rawpy` needed nothing beyond `pip install`.
- **Background bulk population** (`photoapp/thumb_populate.py`, ticket 066, explicit user request): a
  low-priority process that walks the whole library filling in every missing size ahead of time, using the
  external **`dcraw`** binary — requested by name, matching what the original plan (§4.5) had specified before
  the on-demand path switched away from it (`dcraw -e -c` for the embedded `Thumb` preview, `dcraw -c -h -w` for
  `Small`/`Medium`, `dcraw -c -w -q 3` for the full-size `Huge`). `dcraw` was not installed on the development
  machine either; it was installed (`apt-get install dcraw`) specifically to build and test this path.

Both paths write into the exact same `/zoo/Thumbs/<Size>/<path>.jpg` layout and the same `thumbs` table, and
**both respect "never overwrite an existing thumbnail"** — whichever path gets there first for a given file and
size wins, and the other simply has nothing left to do for it. There is no plan to unify them: they solve
different problems (respond to a request now vs. work through a backlog patiently) with different constraints
(no subprocess/binary dependency vs. matching the originally-specified tool), and forcing one tool to serve both
would compromise whichever requirement it was not built for.

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

## Job-queue generation, not inline, for anything that needs a real RAW decode

The web request handler for `/img/{size}/{file_id}` renders inline (in a thread pool, not blocking the event
loop) whenever a cheap render is possible — an existing file, a downscale, a non-RAW Pillow decode, or a RAW's
embedded preview. It only falls back to the background job queue (`raw_render`, capped at
`settings.job_workers`, default 2) for the one case that's actually slow: a RAW with **no usable embedded
preview at all**, which needs a full LibRaw demosaic. The route answers `404` with `Retry-After` while that job
runs, and the frontend already retries automatically (see [frontend-viewer.md](frontend-viewer.md)). This
split — cheap paths inline, only the genuinely slow path queued — is why the job queue's concurrency
(`job_workers`) rarely matters in practice: most requests never touch it.
