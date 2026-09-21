# Photo manager web app: requirements (grill session summary)

Outcome of a Q&A session to scope a web app for managing the photo library
in `/zoo/Pictures`, using `/zoo/Thumbs` as a cache for smaller thumbnails
and non-destructively tuned image copies. The implementation plan derived
from this is in `docs/plans/photo-manager-plan.md`.

## Facts about the current environment

Measured on disk at the time of the session:

- `/zoo/Pictures`: 847 GB, about 92k images (about 64k JPG, 28k DNG), organized
  by year directories (`1998` ... `2025`) plus `Lightroom/`, `Exported/`,
  `Receipts/`, `Namecards/`.
- About 15k existing XMP sidecars with mixed naming: `K___9911.XMP` (upper
  case, RAW-style) and `K___1596.JPG.xmp` (full filename). Lightroom wrote
  them and considered `NNN.JPG` and `NNN.DNG` separate photos, hence
  duplicated sidecars.
- `/zoo/Thumbs` already has four size trees: `Thumb` (about 92k files,
  3.2 GB), `Small` (about 101k, 8 GB), `Medium` (about 101k, 19 GB),
  `Huge` (about 54k, 34 GB), each mirroring the Pictures relative path.
- Existing tooling in this repo: `file_metadata.py` (per-directory
  `index.json` + sqlite, dir-mtime skipping, EXIF date), `image_metadata.py`
  (logical image rows with combined rating, ratings from `.prod.db` and XMP),
  `ranking.py` (canonical copy heuristic), `connect_thumbs.py`,
  `thumbs_move.py`, `cleanup.py`.

## Decisions

### Users and deployment
- Single user, LAN only, no authentication.
- Runs as one process on the machine that has `/zoo` mounted locally.
  Browsers on other LAN devices connect to it.
- Stack: Python backend (FastAPI + sqlite) with a light JavaScript frontend.
  Reuse the existing Python scripts' logic.

### Scope (v1)
1. Folder browser, thumbnail grid and keyboard-driven loupe view.
2. Rating, reject, fav and keyword editing with XMP write-back.
3. Photo grouping (DNG/JPG automatic plus manual link/unlink) and a view that
   shows multiple tunings of one Photo.

Primary workflow to optimize: **cull a folder fast, keyboard driven**
(full-screen loupe, arrow keys, digit keys for rating, a key for reject,
filmstrip, preloading of the next images).

Added after the grill session: the UI must also work on a smartphone on the
LAN (responsive layout, swipe left/right to move between pictures in a
folder, swipe up/down to change the rating). Tracked in `docs/tickets/043.md`.

The UI uses dark theme colors throughout (`docs/tickets/047.md`).

Not in v1: dedupe/cleanup views, image editing, hard promotion of a tuning to
a new file, GPS/caption editing, multi-user features, remote access.

### Originals and files on disk
- `/zoo/Pictures` is read-only for the app, except for XMP sidecars, which
  the app may create and modify.
- The app never moves or deletes originals. Any future cleanup or dedupe
  decisions are carried out by generating a reviewable shell script (as
  `cleanup.py` does), not by the app itself.

### Photo model
- A **Photo** is a logical image with one or more **files**.
- The original (DNG if present, else the JPG) is the root. The camera JPG is
  treated as a **tuning**: a separate file linked to the original by
  "derived-from". Other tunings (Lightroom exports, later the app's own
  rendered copies) attach the same way.
- One representative tuning is chosen per Photo for display. By default it is
  the camera JPG.
- Grouping: files in the same directory with the same basename are grouped
  automatically at index time when they include at least two of RAW (DNG),
  JPG, TIF and PNG (extended 2026-09-21, ticket 015; originally only DNG/JPG
  pairs, the rule `image_metadata.py` uses). The original is the first of
  RAW > JPG > TIF > PNG; a JPG beside a RAW is the camera file, the rest are
  tunings. Manual link/unlink is available for everything else and always
  overrides the automatic rule on rescan.
- Pick/reject and star rating are **per Photo**, shared across the DNG and
  camera JPG.
- Later (future work, not v1): "hard promotion" of a tuning into an
  independent Photo with a new image file created in `/zoo/Pictures`. The
  schema should not make this hard.

### Rating and flags
- One combined value in [-1, 5]: `-1` reject, `0` unrated, `1..5` picked with
  that many stars (the encoding documented in `METADATA_SOURCES.md` and used
  by `image_metadata.py`).
- **Fav** is an independent flag, unrelated to stars. It is stored as the
  reserved keyword `fav` in `dc:subject`.
- Keywords/tags are `dc:subject`.
- "Pick" has no separate storage in XMP; it is just a positive rating.
- Identity for ratings is the path, with the content hash as a secondary key.
  If a file appears at a new path with no rating and its hash uniquely matches
  a rating recorded for a path that no longer exists, the rating is applied
  automatically and the XMP is written. Every such change goes in an undoable
  activity log.

### XMP handling
- **The authoritative rating is the newest one, whether it is in the database or in an XMP
  file** (changed 2026-09-21, ticket 021; this replaces "XMP sidecars are the source of truth,
  the sqlite DB is a rebuildable cache"). Both the database and every sidecar of the Photo
  (the original's and the camera JPG's) are eligible; whichever was set last wins. Each source
  has a time: a sidecar's is its file modification time, the database's is the time the rating was
  set there. The other sources are brought in line on the next write. Consequences: the
  database holds ratings that are not derivable from the files alone (for example an imported
  rating that was not written to XMP), so it must be backed up; and a rating brought in from
  outside (an import, a recovered rating) carries the time it was originally set, not the time
  it was imported.
- Rating, fav and tags are written to the sidecars of both the original (DNG)
  and its camera JPG, creating the JPG's sidecar if it has none, so the two stay
  in sync in darktable (decided 2026-09-21, ticket 023; this replaces "original
  only"). Other files' sidecars (exports, tunings) are not modified. When
  reading, if the sources (database and sidecars) disagree the newest one wins and the UI
  shows a conflict badge until a write brings them back in sync.
- v2 idea: a function to delete DNG files to save space (most likely for
  rating 1); the rating then lives on in the JPG's sidecar.
- Existing sidecars (`.XMP`, `.JPG.xmp`, ...) are read in any naming style and
  edited in place; there is no mass rename. New sidecars use the full-filename
  style for every file, including RAW originals: `foo.DNG.xmp`, `foo.jpg.xmp`
  (decided 2026-09-21, ticket 042: it is what darktable and 15427 of 15429
  existing sidecars use; this replaces the earlier `foo.xmp` for RAW).
  Normalization "going forward" must not be lossy.
- Lightroom stays in occasional use on the same tree. Writes therefore must:
  round-trip the XML untouched except for the edited nodes (`crs:*` develop
  data and unknown elements survive), be atomic (temp file + rename), and keep
  a one-time backup of the first-seen original.

### Thumbnails and tunings
- The `/zoo/Thumbs/{Thumb,Small,Medium,Huge}` layout stays for now, with a
  new `/zoo/Thumbs/Tuned` for rendered tunings. Each size is managed
  separately so the disk usage per size stays visible.
- The design should allow a later move to content-addressed storage (a script
  that hard-links existing thumbnails to hash-based locations).
- For RAW files without a Thumbs entry: show the embedded JPEG preview
  immediately, and upgrade in the background through an on-demand dcraw job.
- Editing is v2: rotate/crop/straighten, exposure/contrast/white balance, and
  auto-fix presets. Each recipe is stored in sqlite and mirrored as a JSON
  file next to the rendered image.

### Indexing and import
- Scan on demand (a rescan button) plus a nightly job, reusing the
  directory-mtime skipping from `file_metadata.py`.
- Day-one ratings import comes from the table produced by `image_metadata.py`
  (`.prod.db` and XMP sources). Combining pick and star rating into a single
  value before import is acceptable and is a minor change.

## Open items to revisit

- **Durability of manual links.** Manual link/unlink decisions are not stored
  in XMP, so the DB is not a pure cache if they live only there. Decide where
  they are persisted (for example an append-only JSON-lines file kept beside
  the DB, or a custom XMP field).
- **Reject and stars.** Lightroom's `Rating=-1` erases the star count.
  **Decided 2026-09-21 (ticket 024): yes**, the app remembers the previous
  stars (`photos.previous_stars`, not rebuildable from disk) and un-reject
  restores them.
- **Conflict survey.** Before first write-back, count Photos whose DNG and JPG
  sidecars disagree, to check that "newest wins" is acceptable.
- **Camera counter reuse.** Names like `K___0001` can repeat across years. This
  is why automatic grouping is limited to the same directory.
- **Thumbs layout details.** Exact pixel sizes per size tree and the
  extension mapping (JPG/DNG to thumbnail name) still need to be confirmed
  from the existing files.
