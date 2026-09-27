# Operating the photo manager

## Running

`./start.sh` (creates `.venv` on first run; see [install.md](install.md) for a new installation). Every
flag below can also be set in a TOML config file (`photos.toml`, `--config=FILE`, `$PHOTOS_CONFIG`; keys
are the flag names, see `photos.example.toml`); the command line wins over the file. Useful flags, all optional:

| Flag | Default | Meaning |
|---|---|---|
| `--config` | see above | the config file; `none` ignores `./photos.toml` and `~/.config/photos/config.toml` |
| `--pictures_dir` | `~/Pictures` | the library; read-only except XMP sidecars and the Delete flow (ticket 072), which moves rejected photos' files to `.trash/` |
| `--thumbs_dir` | `<state_dir>/thumbs` | thumbnail cache (sizes are created on demand) |
| `--state_dir` | `$XDG_DATA_HOME/photos` (`~/.local/share/photos`) | database, XMP backups, manual link log |
| `--database_path` | `<state_dir>/app.sqlite` | the sqlite file, when it should live elsewhere |
| `--hashes_db` | none | `tools/archive/catalog.py` database whose hashes are reused by scans |
| `--xmp_dry_run` | off | compute and log XMP changes without writing anything |
| `--new_raw_sidecar_style` | `full` | new RAW sidecars named `NAME.DNG.xmp` (`stem`: `NAME.xmp`) |
| `--nightly_scan_hour` | 3 | local hour of the nightly rescan, -1 disables |
| `--one_star_is_unrated` | off | display-only: 1 star is shown as unrated (darktable's import default), `picked` means 2+ stars; nothing on disk changes |
| `--job_workers` | 2 | background RAW render threads |
| `--busy_retry_seconds` | 60 | how long the web app keeps retrying when another writer (a running scan) holds the database, before answering "database busy" |
| `--scan_workers` | 8 | threads reading files during a scan (storage I/O is latency bound — `/zoo` is a local ZFS mirror on spinning disks, not a NAS despite earlier notes; see scanning.md) |
| `--write_metadata_json` | on | scans write a per-directory `index.json` and a per-file `<name>.json` metadata cache into the library and reuse it, so an unchanged directory or file need not be decoded again (ticket 111). It **writes into the picture directories**; pass `--nowrite_metadata_json` to turn it off |
| `--port`, `--host` | 8080, 0.0.0.0 | LAN only, no authentication |
| `--load_worker_enabled` | on | run the background thumbnail worker automatically when the machine is idle (ticket 073) |
| `--load_check_seconds` | 30 | how often the load-adaptive worker samples CPU/memory load |
| `--load_start_threshold` / `--load_stop_threshold` | 0.5 / 1.5 | 1-minute load average to start / stop the background worker |
| `--mem_start_percent` / `--mem_stop_percent` | 20 / 10 | % RAM available to start / stop the background worker |

Try a first session with `--xmp_dry_run` to see what would be written.

## Scanning the library

`python -m photoapp.fullscan --hashes_db=~/zoo.db` scans the whole library from the command line
(read-only on the library; it writes only the state database). It works **one top-level (year)
directory at a time**, in sorted order, after the files directly in the root. Each step is complete on
its own: files read, Photos grouped, ratings resolved, thumbnails indexed, vanished files marked. So
an interrupted run (Ctrl-C, a reboot, low memory) leaves every finished year usable in the app, and
running the same command again skips finished directories (by their mtime) and continues. It logs
`done <dir> (n/total)` with the files read and the peak memory after every step.
`--scan_dirs=2001,2026` scans only those directories; `--scan_workers` sets the read threads.
The **Rescan** button works the same way (a folder-scoped rescan scans that folder), and runs
immediately, at normal priority, regardless of the nightly scan or the load-adaptive worker below.

As of ticket 076, the **nightly scan** (`--nightly_scan_hour`, local hour, -1 disables) no longer runs
one big scan synchronously at that hour. Instead it queues one `scan_dir` job per top-level directory
(the same units `fullscan.py` and the table above already scan one at a time) into the same
low-priority queue as the background thumbnail worker, and ticket 073's load-adaptive worker drains
them over time, only while the machine looks idle -- so a rescan still happens every night but never
forces itself onto a busy machine at 3am, and can spill into the following day if it needs to. This
depends on `--load_worker_enabled` (on by default): if that's off, nightly-queued scan jobs sit
unprocessed until it's turned on or they're drained some other way. Progress and any per-directory
failures show on the Jobs page like any other background job.

The same nightly enqueue also queues a `prune_jobs` job (ticket 075): it deletes `done` jobs older
than a week and `failed` jobs older than a year (failures are kept longer since one might still be
worth a human noticing), keeping the Jobs page's history from growing forever. A job that succeeds
also immediately deletes its own earlier failed attempts, rather than waiting for the next prune.

## On-disk scan metadata cache (ticket 111)

As of ticket 111, `--write_metadata_json` makes scans keep a metadata cache on disk next to the
photos: a per-directory `index.json` (the directory's mtime plus one record per image directly in
it) and a per-file `<name>.json` (for example `a.DNG.json`). The record holds what a scan would
otherwise re-read: mime type, size, hash, mtime, and the EXIF date, aperture, shutter speed, ISO,
focal length and camera make/model. A later scan of an unchanged directory or file then answers
from the cache instead of decoding the image — useful after a database rebuild, when the database's
own `dir_mtimes` no longer says the directory is current.

It is **on by default** and writes into the picture directories (`/zoo/Pictures`), which are
otherwise read-only except sidecars and the Delete flow; pass `--nowrite_metadata_json` to disable
it. The cache files are ignored by the scanner (never imported as photos or mistaken for sidecars),
and writing one changes its directory's mtime, which the scan corrects for so an untouched directory
is still skipped next time. The database stays the source of truth: deleting the JSON files (or
turning the flag off again) loses only the shortcut, never data.

## A busy database

The web app, a running scan (`photoapp.fullscan`, the Rescan button, the nightly scan) and the RAW render jobs
write to the same sqlite file, and sqlite lets only one writer in at a time. When the web app finds the database
locked it waits and retries (0.1 s, 0.2 s, 0.4 s ... up to 4 s between attempts) for about a minute
(`--busy_retry_seconds`), then answers **"internal server error: database busy"**, which the page shows as a red
message. During those retries other requests keep working (reads are never blocked), and a rating edit takes the
database lock *before* it touches any sidecar, so a failed edit changes nothing on disk and can simply be repeated.
The scan and the job workers wait up to a minute inside sqlite itself. A busy database while a big scan runs is normal.

## Populating thumbnails in the background

Thumbnails are normally made the first time a photo is opened. To fill in the gaps ahead of time (see
`docs/reqs/thumbs-layout.md` for which years are missing them), run:

    python -m photoapp.populate_thumbs

It finds every file still missing a `Thumb`/`Small`/`Medium`/`Huge`, renders them one file at a time (never in
parallel, so it never competes for CPU or disk), and lowers its own CPU and I/O priority (`os.nice(19)` plus
`ionice -c3` on its worker thread) so it stays out of the way of normal use, a running scan, or the on-demand
render queue. It never overwrites a thumbnail that already exists. Run it detached and low-priority from the
shell too (belt-and-braces, since it already reduces its own priority):

    nice -n19 ionice -c3 python -m photoapp.populate_thumbs &

As of ticket 073, the running server does this automatically: whenever the machine looks idle (1-minute
load average and % RAM available past the `--load_start_threshold`/`--mem_start_percent` thresholds,
checked every `--load_check_seconds`), it starts the same low-priority worker on its own and stops it
again as soon as either threshold is crossed the other way (`--load_stop_threshold`/`--mem_stop_percent`,
deliberately looser than the start thresholds, so it doesn't flap right at one boundary). Every sample is
logged at `--verbosity=3` (see `photoapp/load_worker.py`'s docstring) — the memory thresholds are a first
guess and are meant to be tuned from real observation, not treated as final. `python -m photoapp.populate_thumbs` (above) still works standalone for an explicit,
immediate catch-up run (e.g. right after adding a lot of new files), independent of the automatic worker.
Disable the automatic worker with `--load_worker_enabled=false` if you'd rather only ever run it by hand.

It writes to the same state database the running app uses, so its progress — queued/running/done/failed counts
and any per-file error — shows on the app's **Jobs** page immediately, no restart needed. Safe to interrupt
(Ctrl-C, a reboot) and rerun: already-made thumbnails are left alone and the rest picks up where it stopped.

RAW files and non-RAW files alike are rendered through `thumbs.ensure` — the same `rawpy`/LibRaw renderer and
per-file settings (brightness, white balance, highlight recovery; ticket 085) the on-demand path uses, so a
photo's thumbnails look the same whether this background populator or a live request made them first
(`docs/design/thumbnails.md`). An untouched RAW file's `Thumb`/`Small`/`Medium`/`Huge` all come from the
camera's own embedded preview until its settings are tuned away from default, at which point every size
demosaics instead. This background queue and the on-demand path's own `raw_render` job queue can run at the
same time without interfering (`--limit=N` restricts a run to N files, handy for a quick check). `dcraw` is no
longer used anywhere in this app.

As of ticket 080, opening a folder in the app (the first page of `/api/photos`) bumps that folder's still-missing
thumbnails ahead of the queue's standing backlog: the automatic worker claims its newest-enqueued job first, and
a file already queued from the big background sweep keeps its place (this is a cheap approximation, not a real
priority system — see the ticket for what it does and does not cover), so a folder that was never popular enough
for the background walk to reach yet still fills in reasonably quickly once someone actually opens it.

## Syncing sidecars that are out of sync with the computed rating

A Photo's sidecars only catch up to its currently resolved rating/fav/tags when that Photo is
next edited (ticket 065); a Photo that is never edited again can keep an out-of-sync sidecar (or
two disagreeing sidecars) indefinitely. To fix every such Photo (tracked as `photos.conflict`) in
one step:

    python -m photoapp.sync_sidecars              # preview: prints what would change, writes nothing
    python -m photoapp.sync_sidecars --yes         # actually write

`--xmp_dry_run` also works and takes precedence even with `--yes`. `--limit=N` restricts a run to
N Photos. Nothing is logged to the activity log for a pure sync (nothing actually changed, so
there is nothing to undo) — only the sidecar bytes and the `conflict` flag change.

## Backfilling camera metadata (aperture, shutter speed, ISO)

As of ticket 083, every scan reads a file's EXIF for aperture, shutter speed and ISO alongside the
date it already read. A file scanned *before* that ticket won't have them until it changes and gets
rescanned — to fill them in for the whole library without waiting for that or forcing a full
rescan (scanning is I/O-latency bound and slow, see "Scanning the library" above), run:

    python -m photoapp.backfill_exif

Read-only on the library: opens each file just far enough to read its EXIF header, no hashing, no
grouping, no thumbnail work. Only touches files with none of the three fields already set, so it's
safe and cheap to rerun (`--backfill_limit=N` restricts a run to N files for a quick check).

## Hidden folders

Folders whose name starts with a dot (`.nu`, `.thumbnails`, `.webaxs`, `.picasaoriginals`, `.comments`:
leftovers of old programs) are not listed in the folder view, and their Photos are not counted in the
folders that contain them. Nothing is deleted, scans still read them, and a hidden folder can be opened
by its path (for example `#/2001/new-epoch/.nu`). A file whose own name starts with a dot is not
affected. Restart the app after updating so the server code is current. **One exception**: `.trash/`
(see below) is never scanned at all, not just hidden from the listing — a trashed file's sidecar still
says what it always said (e.g. reject), so scanning it back in would silently re-create it as a live
Photo.

## Deleting rejected photos

The **Delete** button (shown only while the Rejected filter is active) opens a review screen scoped to
the folder it was clicked from: every currently-rejected photo in that folder as a Medium-size render,
with a confirm button below all of them (scroll to reach it — a deliberate speed bump). Confirming moves
every file under each shown Photo — the original, its camera JPG, any tuning, and every XMP sidecar — into
`<pictures_dir>/.trash/`, mirroring the original subdirectory structure (so `2019/trip/IMG_1.DNG` becomes
`.trash/2019/trip/IMG_1.DNG`). Nothing is deleted outright: this is a move, fully recoverable by hand
(move the files back and rescan), and the Photo's rating/history is kept (`missing = 1`, the same state a
file gets when a scan finds it vanished on its own). The server re-checks each Photo is still rated reject
before moving anything, regardless of what the browser's list said.

As of ticket 081, `.trash/` empties itself: the same nightly enqueue that queues `scan_dir`/`prune_jobs`
also queues a `purge_trash` job, which permanently deletes anything that has sat in `.trash/` for more than
`trash.RETENTION_DAYS` (7 days) and removes any subdirectory that ends up empty. "How long has it sat there"
is the file's ctime (bumped by the move itself, unlike mtime, which a same-filesystem move leaves untouched
— confirmed empirically before relying on it). Restoring a file before that window closes is still a manual
`mv` back to its original location (then rescan).

## What is in the state directory, and what to back up

| Path | Rebuildable from disk? | Back it up? |
|---|---|---|
| `app.sqlite` (files, photos, tags, sidecar cache, thumbs, jobs) | mostly: a rescan rebuilds everything that comes from the files | copy it anyway (see next row) |
| `app.sqlite` ratings the database itself decided (`photos.rating_source` = `app`, `import`, `hash-recovery`) that are **not** in any sidecar (an import without `--write_xmp`) | **no**: the newest rating wins whether it is in the database or in a sidecar, so a database-only rating is real data | yes, copy the file |
| `app.sqlite` tables `activity_log` (undo history) and `manual_links` | **no** | yes, copy the file |
| `manual_links.jsonl` | it is the durable copy of `manual_links` | **yes** |
| `xmp_backups/` (first-seen copy of every sidecar the app changed) | no | yes, it is the safety net |

Copy the whole state directory to back it up (stop the app first, or copy `app.sqlite`,
`app.sqlite-wal` and `app.sqlite-shm` together). Deleting `app.sqlite` is safe: on the next start
`manual_links.jsonl` is restored, the library is rescanned, and ratings come back from the sidecars.
Only the undo history is lost.

## Safety rules the app follows

- Originals are never moved, renamed or deleted. The only files it writes in the library are XMP
  sidecars, atomically (temp file + rename), after saving a first-seen backup.
- A rating, fav or tag change is written to the sidecars of the original and its camera JPG (so they
  stay in sync in darktable); sidecars of exports and other tunings are never modified.
- Every change is in the activity log and can be undone; undo refuses if the value changed since.
