# Photo manager web app: implementation plan

Requirements and decisions are in `docs/reqs/photo-manager-requirements.md`.
This document is the design and build order derived from them.

## 1. Goals and non-goals

Goals (v1): browse `/zoo/Pictures` by folder, view thumbnails and full
images, cull quickly with the keyboard, and record rating / reject / fav /
keywords into XMP sidecars, treating a DNG and its camera JPG as one Photo.

Non-goals (v1): editing, dedupe/cleanup views, promotion of tunings to new
files, authentication, remote access, live filesystem watching.

## 2. Architecture

```
browser (LAN) ── HTTP/JSON + image bytes ──> app process (uvicorn)
                                              ├─ API layer (FastAPI)
                                              ├─ index/scan module  ── /zoo/Pictures (read-only, + XMP writes)
                                              ├─ xmp module         ── sidecar files
                                              ├─ thumbs module      ── /zoo/Thumbs/{Thumb,Small,Medium,Huge,Tuned}
                                              ├─ job worker (thread/process pool): previews, dcraw
                                              └─ sqlite: state dir, e.g. ~/.local/share/photos/app.sqlite
```

- Single process, single machine. sqlite in WAL mode; one writer connection
  owned by the app, short transactions.
- Configuration through absl flags, like the existing scripts:
  `--pictures_dir=/zoo/Pictures`, `--thumbs_dir=/zoo/Thumbs`,
  `--state_dir`, `--port`, `--host`.
- Code layout (new package next to the existing scripts; existing scripts stay
  usable as CLIs, shared logic moves into importable modules):
  ```
  photoapp/
    __init__.py
    config.py        flags
    db.py            schema, migrations, connection helpers
    scan.py          directory walk, files/photos tables (reuses file_metadata logic)
    grouping.py      auto and manual grouping into Photos
    xmp.py           sidecar discovery, lossless read/write
    ratings.py       combined-rating semantics, conflict resolution, hash recovery
    thumbs.py        lookup, generation, size accounting
    previews.py      embedded preview extraction, dcraw jobs
    jobs.py          background queue
    api.py           routes
    static/          index.html, app.js (ES modules, no bundler), style.css
  tests/
  ```
- Frontend: plain ES modules and CSS, no build step. Small enough to keep
  without a framework; revisit if the loupe/filmstrip state gets unwieldy.
- Dependencies: fastapi, uvicorn, lxml, Pillow, absl-py; `exiftool` and
  `dcraw` as external binaries.

## 3. Data model

All paths are relative to `--pictures_dir`, with `/` separators.

```sql
CREATE TABLE files (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  hash TEXT,                       -- sha224, as in file_metadata.py
  mime_type TEXT, width INTEGER, height INTEGER,
  bytesize INTEGER, mtime REAL NOT NULL,
  exif_date TEXT,                  -- ISO, "2026-01-01 01:02:03+0900"
  photo_id INTEGER REFERENCES photos(id),
  role TEXT NOT NULL,              -- 'original' | 'camera' | 'tuning' | 'export'
  derived_from INTEGER REFERENCES files(id),
  link_source TEXT NOT NULL,       -- 'auto' | 'manual'
  missing INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX files_hash ON files(hash);
CREATE INDEX files_photo ON files(photo_id);

CREATE TABLE photos (
  id INTEGER PRIMARY KEY,
  original_file_id INTEGER NOT NULL REFERENCES files(id),
  representative_file_id INTEGER NOT NULL REFERENCES files(id),
  rating INTEGER NOT NULL DEFAULT 0,   -- combined [-1, 5]
  fav INTEGER NOT NULL DEFAULT 0,
  previous_stars INTEGER,              -- for un-reject (decided: kept, ticket 024)
  rating_source TEXT,                  -- 'xmp' | 'import' | 'app' | 'hash-recovery'
  conflict INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE tags (
  photo_id INTEGER NOT NULL REFERENCES photos(id),
  tag TEXT NOT NULL,
  PRIMARY KEY (photo_id, tag)
);                                   -- 'fav' is never stored here; it is photos.fav

CREATE TABLE xmp_sidecars (
  path TEXT PRIMARY KEY,               -- sidecar path, actual on-disk spelling
  file_id INTEGER REFERENCES files(id),
  mtime REAL NOT NULL, hash TEXT,
  rating INTEGER, has_fav INTEGER,     -- parsed values, for conflict detection
  backup_path TEXT                     -- one-time backup of the first-seen file
);

CREATE TABLE manual_links (            -- durable, see open item on persistence
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL, hash TEXT,       -- the file being linked/unlinked
  target_path TEXT, target_hash TEXT,  -- NULL target = "unlink / stand alone"
  action TEXT NOT NULL,                -- 'link' | 'unlink'
  created_at TEXT NOT NULL
);

CREATE TABLE rating_by_hash (          -- rename recovery
  hash TEXT PRIMARY KEY, rating INTEGER, fav INTEGER, last_path TEXT
);

CREATE TABLE activity_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  photo_id INTEGER, xmp_path TEXT,
  field TEXT NOT NULL,                 -- 'rating' | 'fav' | 'tags'
  old TEXT, new TEXT, cause TEXT,      -- cause: 'user' | 'hash-recovery' | 'import'
  undone INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE thumbs (
  file_id INTEGER NOT NULL REFERENCES files(id),
  size TEXT NOT NULL,                  -- 'Thumb' | 'Small' | 'Medium' | 'Huge' | 'Tuned'
  path TEXT NOT NULL, bytesize INTEGER,
  source TEXT NOT NULL,                -- 'existing' | 'embedded' | 'dcraw' | 'pillow'
  PRIMARY KEY (file_id, size)
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, file_id INTEGER,
  state TEXT NOT NULL,                 -- 'queued' | 'running' | 'done' | 'failed'
  error TEXT, created_at TEXT, finished_at TEXT
);

CREATE TABLE dir_mtimes (dirpath TEXT PRIMARY KEY, mtime REAL NOT NULL);

-- v2: tuning recipes
CREATE TABLE tunings (
  id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL, source_file_id INTEGER NOT NULL,
  recipe TEXT NOT NULL, output_path TEXT, created_at TEXT
);
```

Design notes:

- Everything derivable from disk (files, hashes, parsed XMP values, thumbs) is
  rebuildable by a rescan. Not rebuildable: `manual_links`, `activity_log`,
  `previous_stars`. These must be backed up, and `manual_links` should also be
  mirrored to an append-only `manual_links.jsonl` in the state dir (resolves
  the open item on durability).
- `files.role` and `derived_from` are what makes promotion a data change later:
  promotion sets a new `photos` row for that file and keeps `derived_from` as
  provenance.

## 4. Core behaviors

### 4.1 Scanning
- Walk `--pictures_dir` with `os.walk`; skip a directory whose mtime matches
  `dir_mtimes` (same rule as `file_metadata.py`), but also revisit it when
  any image lacks a record or needs a backfill (as `file_metadata.py` does for
  `exif_date`).
- For each image file (extension set from `file_metadata.IMAGE_EXTENSIONS`):
  stat, read width/height/mime/EXIF date through Pillow, hash by chunked
  sha224 (reuse `--hashes_db` values while mtime matches). Undecodable RAW
  files are kept with NULL dimensions, as today.
- Sidecars (`*.xmp`, any case) are discovered in the same pass and recorded in
  `xmp_sidecars` (path, mtime, parsed rating/fav).
- Files that vanished are marked `missing=1`, not deleted, until a hash
  recovery or a manual cleanup decides their fate.
- Triggers: `POST /api/scan` (optionally scoped to a directory) and a nightly
  timer. Progress is exposed through `GET /api/scan/status`.
- Reuse: factor the reusable parts of `file_metadata.py` (hashing, EXIF,
  dir-mtime logic) into functions the app imports; keep the script as a thin
  CLI. Do not fork the logic.

### 4.2 Grouping
1. Automatic: within one directory, files with the same case-insensitive
   basename form one Photo when they include at least two of: a RAW (DNG,
   ...), a JPG/JPEG, a TIF/TIFF, a PNG (extended 2026-09-21, ticket 015). The
   original is the first of RAW > JPG > TIF > PNG. A JPG beside a RAW is
   `camera`; every other member is a `tuning`; all are `derived_from` the
   original with `link_source='auto'`. A lone file, a second file of the same
   kind (`a.tif` and `a.TIF`) and other types (GIF, WebP, HEIC...) are Photos
   of their own.
2. Manual overrides from `manual_links` are applied after the automatic rule
   and win on every rescan:
   - `link`: attach a file as a `tuning`/`export` of a target Photo.
   - `unlink`: detach a file into its own Photo, even if the rule would group it.
3. The representative file defaults to the `camera` JPG when present, else the
   original; the user can override it (stored on the Photo, survives rescans
   while the file exists).

### 4.3 Ratings, flags, conflict resolution
- Combined rating `r` in [-1, 5]. UI mapping: digit `1..5` sets `r`, `0`
  clears, `X` toggles `-1`. `F` toggles fav. Tag entry edits `dc:subject`
  minus `fav`.
- Read path (scan and on demand): parse every sidecar belonging to the Photo's
  files. If values agree, that is the Photo's value. If they differ, the
  sidecar with the newest mtime wins, `photos.conflict=1`, and the UI shows a
  badge on reading.
- Write path (decided in ticket 023): rating, fav and tags are written to the
  sidecars of the original and its camera JPG (a missing JPG sidecar is
  created), each with its own first-seen backup. Writing therefore brings the
  two back in sync and clears the conflict flag. Sidecars of exports and other
  tunings are not modified. Tags are converged conservatively: only tags the
  app knew about are removed. If the second write fails, the first stays, the
  cache reflects the truth, and the error names what was written.
- Hash recovery: when a scan finds a file that has no rating anywhere, whose
  hash has exactly one row in `rating_by_hash` whose `last_path` is missing,
  apply that rating and fav (and tags, if kept), write the sidecar, and log it
  in `activity_log` with `cause='hash-recovery'`. Zero or multiple matches
  produce no action, and the case is listed in a "needs attention" view
  instead of guessed.
- Day-one import: a one-shot `import_ratings` command loads the
  `image_metadata.py` output (`hash`, `filepath`, `rating`, `reject`,
  `merged_paths`) into `photos`/`rating_by_hash`, merging `reject` and
  `rating` into the combined value first. It does not write XMP unless
  `--write_xmp` is given, and reports Photos whose XMP disagrees.

### 4.4 XMP module
Discovery for a file `dir/name.ext`:
1. Look for an existing sidecar case-insensitively, in either style:
   `name.xmp` and `name.ext.xmp`. If several exist, prefer the one whose
   naming matches the file's convention (RAW: `name.xmp`; else `name.ext.xmp`)
   and read the others as additional sources.
2. If none exists and a write is needed, create the full-filename name
   `name.ext.xmp` for every type, RAW included (decided, ticket 042; flag
   `--new_raw_sidecar_style=stem` gives `name.xmp` for RAW). The Photo's
   sidecar is chosen from the original file.

Lossless editing:
- Parse with lxml, retaining all namespaces, comments, processing instructions
  and unknown elements. Only touch:
  - `xmp:Rating` (either as an attribute on `rdf:Description`, as Lightroom
    writes it, or as a child element; preserve whichever form is present),
  - `dc:subject` (`rdf:Bag` of `rdf:li`),
  - `lr:hierarchicalSubject` is left untouched (reading it is future work).
- A new sidecar is a minimal, valid XMP packet with `x:xmpmeta`,
  `rdf:RDF`, `rdf:Description` and only the fields being set.
- Serialization must not reformat untouched regions. Prefer editing the
  parsed tree and writing with the original XML declaration, encoding and
  `<?xpacket?>` wrapper preserved. A test round-trips every existing sidecar
  in a sample with no edits and asserts byte-identical output (or a documented
  minimal diff).
- Atomic write: write `name.xmp.tmp` in the same directory, `fsync`, then
  `os.replace`. On the first write to an existing sidecar, copy the original
  bytes to `<state_dir>/xmp_backups/<path>.<timestamp>` and record it in
  `xmp_sidecars.backup_path`.
- Concurrency with Lightroom: record mtime and hash at read time; at write
  time re-stat, and if they changed, re-read and re-apply only the edited
  fields to the new content instead of overwriting.

### 4.5 Thumbnails and previews
- Lookup order for a file and requested size: `thumbs` table row, then the
  existing tree `/zoo/Thumbs/<Size>/<relative path>` (checking the extension
  mapping learned in step 4), then generation.
- Generation, in order of preference:
  1. Existing size in the tree, or a larger existing size (downscale, marked
     `source='pillow'`), served immediately.
  2. JPEG/PNG/etc.: Pillow resize into the matching size directory.
  3. RAW: embedded preview through `exiftool -b -PreviewImage` (fall back to
     `-JpgFromRaw`), returned immediately and cached; then a queued dcraw job
     (`dcraw -c -h -w`, piped into Pillow) replaces it and the UI refreshes.
- Writes go into the same layout: `/zoo/Thumbs/<Size>/<relative path>` with a
  `.jpg` extension. Each size directory is managed independently. A
  `GET /api/thumbs/usage` endpoint reports file count and bytes per size
  (from the `thumbs` table, refreshed by scan).
- Jobs run in a small worker pool (2 workers by default, `--job_workers`).
  Job state is in the `jobs` table so a restart resumes queued work.
- Content-addressed layout is future work; keep all path logic in
  `thumbs.py` so a hard-link script and a new lookup can slot in.

## 5. HTTP API (v1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/dirs?path=` | child directories with Photo counts |
| GET | `/api/photos?dir=&sort=&filter=&offset=&limit=` | page of Photos (rating, fav, tags, representative file, conflict) |
| GET | `/api/photos/{id}` | Photo with all files, roles, sidecars |
| POST | `/api/photos/{id}/rating` | `{rating}` sets combined rating, writes XMP |
| POST | `/api/photos/{id}/fav` | `{fav}` |
| POST | `/api/photos/{id}/tags` | `{add:[], remove:[]}` |
| POST | `/api/photos/{id}/representative` | `{file_id}` |
| POST | `/api/files/{id}/link` , `/unlink` | manual grouping |
| GET | `/img/{size}/{file_id}` | thumbnail (`Thumb|Small|Medium|Huge`) with preview fallback |
| GET | `/img/full/{file_id}` | original bytes (or the embedded preview for RAW) |
| POST | `/api/scan`, GET `/api/scan/status` | on-demand rescan |
| GET | `/api/activity`, POST `/api/activity/{id}/undo` | audit log and undo |
| GET | `/api/thumbs/usage` | disk usage per size |
| GET | `/api/jobs` | background job queue |

Batch rating (`POST /api/photos/rating` with many ids) is wanted for culling
runs but can follow single-photo rating.

## 6. Frontend (v1)

- **Folder browser**: tree from `/api/dirs`, breadcrumb, sort by EXIF date or
  name, filter by rating/fav/unrated/rejected/has-conflict.
- **Grid**: virtualized (about 92k Photos overall, thousands per folder),
  lazy-loaded `Thumb` or `Small`, overlays for rating, fav, reject, conflict,
  and a stack icon when a Photo has several files.
- **Loupe** (the workhorse): full-window image (`Medium`, then `Huge`, then
  full on zoom), filmstrip of neighbors, preloading of the next 3 and previous
  1 images, HUD for rating/fav/tags.
  Keys: `←/→` navigate, `0-5` rating, `X` reject, `F` fav, `T` add tag,
  `Z` zoom, `Esc` back to grid, `G` cycle representative tuning, `U` undo.
- **Tunings view** (per Photo): all files with role, size, dimensions, which
  is representative, sidecar path; link/unlink actions.
- **Activity** page: recent XMP changes, hash recoveries, undo.
- Optimistic UI for rating changes with an error toast if the XMP write fails.
- The state (current folder, selection, filter) is in the URL hash so reload
  and back work.

## 7. Build order

Each step ends in something that runs and is checked before the next starts.

1. **Skeleton and schema.** Package, flags, `db.py` with migrations, empty
   API serving a placeholder page. Done when: `python -m photoapp` starts and
   creates the DB.
2. **Scan and files table.** Refactor `file_metadata.py` internals into
   importable functions, populate `files`, `xmp_sidecars`, `dir_mtimes`. Done
   when: a scan of one year directory matches `file_metadata.py`'s
   `index.json` output, and a second scan is a no-op.
3. **Grouping.** Auto grouping and the `manual_links` mechanism. Done when: a
   DNG+JPG directory yields Photos with the right roles, and a manual unlink
   survives a rescan.
4. **Thumbs inspection.** Before writing thumbs code, examine the existing
   `/zoo/Thumbs` trees: pixel sizes per size, extension mapping, and how many
   Photos lack each size. Record findings in
   `docs/reqs/thumbs-layout.md`. Then implement `thumbs.py` lookups.
5. **XMP module and tests.** `xmp.py` with the lossless round-trip suite over
   a copied sample of real sidecars (Lightroom `crs:*` present), atomic write,
   backup, mtime re-check. Done when: no-op round trips are byte-identical,
   and edited files differ only in the touched nodes.
6. **Import and ratings.** `ratings.py`, `import_ratings`, conflict detection,
   and the pre-write conflict survey report (count of Photos whose sidecars
   disagree). Done when: the survey is produced for the whole library and
   reviewed.
7. **Read-only UI.** Folder browser, grid, loupe with thumbs; RAW embedded
   previews; job queue with dcraw upgrades. Done when: a whole year can be
   browsed without missing images.
8. **Curation.** Rating/fav/tags with XMP write-back, activity log and undo,
   keyboard handling. Done when: rating in the loupe writes the original's
   sidecar, Lightroom-style content in it is intact, and undo restores it.
9. **Grouping UI.** Tunings view, representative selection, manual link and
   unlink.
10. **Hash recovery and nightly scan.** Recovery with the activity log, the
    "needs attention" view, the scheduled rescan, and the per-size disk usage
    page.

## 8. Testing

- Unit tests (pytest) for: combined-rating semantics, filename to sidecar
  resolution (all naming styles, upper/lower case), XMP round trip and edit,
  grouping rules and manual override precedence, hash recovery uniqueness
  rules, thumbs lookup order.
- Fixtures: a small synthetic tree (as created ad hoc for `file_metadata.py`)
  plus a copied sample of real sidecars kept out of the repo.
- A dry-run flag (`--xmp_dry_run`) makes the app compute and log XMP changes
  without writing. Use it for the first pass on the real library.
- Integration: start the app against a temporary copy of a few directories, use
  the HTTP API to rate, then check the sidecar bytes and the activity log.
- Manual checks in a real browser for the loupe keyboard flow and preload
  behavior on large images.

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| XMP corruption or loss of Lightroom `crs:*` data | Lossless edit, atomic writes, backups, round-trip tests on real files, dry-run first |
| Lightroom and the app writing at once | mtime/hash check at write time and re-apply |
| Conflicting sidecars for DNG and JPG | Newest wins, conflict badge, survey before first write |
| `Rating=-1` erasing stars | Keep `previous_stars` in the DB so un-reject restores them (decided) |
| Camera counter reuse across years | Automatic grouping only within one directory |
| Slow first scan (847 GB to hash) | Reuse `--hashes_db` hashes, run scan in the background, show progress |
| Manual links lost if the DB is deleted | Mirror to `manual_links.jsonl`, include the state dir in backups |
| Thumbs layout assumptions wrong | Step 4 inspection before any thumbs code |

## 10. Future work (not planned in detail)

- v2 editing: rotate/crop/straighten, exposure/contrast/white balance,
  auto-fix presets, rendered into `/zoo/Thumbs/Tuned/` with recipes in the
  `tunings` table and a JSON mirror next to each output.
- Hard promotion of a tuning to a new Photo with its own file in
  `/zoo/Pictures` and its own sidecar.
- Dedupe/cleanup views that record decisions and generate reviewable scripts
  (the `cleanup.py` model), plus unlinked-pair, missing-thumbs and empty
  directory reports.
- Content-addressed thumbnail storage, with a script that hard-links the
  existing tree into hash-based locations while keeping per-size accounting.
- Read `lr:hierarchicalSubject`, caption/title/GPS editing, filesystem
  watching.
