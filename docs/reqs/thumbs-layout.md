# /zoo/Thumbs layout (inspection findings)

Inspected 2026-09-21, read-only (sampling plus `/zoo/Thumbs/filenames.txt`, a file list that may
be stale: it shows only 2799 files in `Thumb/`, while the requirements measured about 92k).

## Trees

`/zoo/Thumbs/{Thumb,Small,Medium,Huge}/<same relative path as in /zoo/Pictures>/`. Besides the
image trees the directory holds helper files (`convert.sh`, `monitor`, `monitor.go`,
`http_server.go`, `index.json`, `filenames.txt`, `dirnames.txt`, `README.md`). The app must
ignore everything that is not under the four size trees (and later `Tuned/`).

## Pixel sizes (long edge; measured on `2019/2019-02-mtv/K___4018.*`)

| Tree | Long edge | Example (DNG 4950x3284, camera JPG 1728x1152) |
|---|---|---|
| Thumb | 300 | 300x199 (DNG), 300x200 (JPG) |
| Small | 1000 | 1000x663, 1000x667 |
| Medium | 2000 for the DNG; not upscaled for small sources | 2000x1327; JPG stays 1728x1152 |
| Huge | full size of the source | 4950x3284; JPG 1728x1152 |

Images are never upscaled: a source smaller than the target keeps its own size.

## Extension mapping (source file to thumbnail name)

- `NAME.DNG` -> `NAME.DNG.jpg` (also `NAME.PEF.jpg`)
- `NAME.JPG` / `NAME.jpg` -> `NAME.jpg` (the extension is replaced and lower-cased)
- other types keep the original extension in front: `NAME.png.jpg`, `NAME.PNG.jpg`,
  `NAME.tif.jpg`, `NAME.tiff.jpg`
- Odd names exist, for example `NAME.JPG.L.jpg` (76 in Small/Medium): treat as unknown extra files.

Consequently a DNG and its camera JPG have **separate thumbnails** (`K.DNG.jpg`, `K.jpg`), and
the Photo's representative file decides which one is shown.

Each thumbnail may have a `NAME.<...>.jpg.json` next to it with
`{"bytesize", "dhash", "height", "mtime", "sha224", "width"}`, and every directory has an
`index.json`. The app does not depend on them. (Open question: whether `sha224` is the
hash of the thumbnail or of its source; check before using it for content addressing.)

## Coverage

- Trees cover years 1998-2020 plus `before-2008-10-27`, `for-app`, `incoming`, `nu-a`,
  `Photo Archive`. **2021-2025 have no thumbnails at all**, so those need generation
  (embedded RAW preview then dcraw, see ticket 028, and Pillow for JPEGs, ticket 017).
- `Huge` is sparse: about 54k files, and for JPG sources it holds only 11.9k of the 57k
  entries present in `Small`/`Medium`. `Huge` covers the DNG renders (about 21k) but not all
  JPGs. Treat `Huge` as optional and fall back to `Medium`, then the original.
- Counts of Photos lacking each size: needs a full library scan first (ticket 012 output).
  Deferred to ticket 017.
