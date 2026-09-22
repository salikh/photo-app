# Operating the photo manager

## Running

`./start.sh` (creates `.venv` on first run). Useful flags, all optional:

| Flag | Default | Meaning |
|---|---|---|
| `--pictures_dir` | `/zoo/Pictures` | the library; read-only except XMP sidecars |
| `--thumbs_dir` | `/zoo/Thumbs` | thumbnail cache (sizes are created on demand) |
| `--state_dir` | `~/.local/share/photos` | database, XMP backups, manual link log |
| `--hashes_db` | none | `hash_dir.py` database whose hashes are reused by scans |
| `--xmp_dry_run` | off | compute and log XMP changes without writing anything |
| `--new_raw_sidecar_style` | `full` | new RAW sidecars named `NAME.DNG.xmp` (`stem`: `NAME.xmp`) |
| `--nightly_scan_hour` | 3 | local hour of the nightly rescan, -1 disables |
| `--one_star_is_unrated` | off | display-only: 1 star is shown as unrated (darktable's import default), `picked` means 2+ stars; nothing on disk changes |
| `--job_workers` | 2 | background RAW render threads |
| `--busy_retry_seconds` | 60 | how long the web app keeps retrying when another writer (a running scan) holds the database, before answering "database busy" |
| `--scan_workers` | 8 | threads reading files during a scan (the NAS is latency bound) |
| `--port`, `--host` | 8080, 0.0.0.0 | LAN only, no authentication |

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
The **Rescan** button and the nightly scan work the same way (a folder-scoped rescan scans that folder).

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

It writes to the same state database the running app uses, so its progress — queued/running/done/failed counts
and any per-file error — shows on the app's **Jobs** page immediately, no restart needed. Safe to interrupt
(Ctrl-C, a reboot) and rerun: already-made thumbnails are left alone and the rest picks up where it stopped.

RAW (`.DNG`) files are rendered with the external **`dcraw`** command (`apt install dcraw` if it is not already
on `PATH`; a missing sidecar tool like this fails each RAW file's job with a clear message on the Jobs page
rather than crashing the queue). `Thumb` is the camera's own embedded preview (`dcraw -e -c`); `Small`/`Medium`
are a half-size demosaic (`dcraw -c -h -w`); `Huge` is a full-size, high-quality demosaic (`dcraw -c -w -q 3`).
Non-RAW files use Pillow, the same as on-demand generation. This is a different path from the on-demand RAW
renderer (`raw_render`, ticket 028), which uses `rawpy`/LibRaw instead of `dcraw` — both can run at the same
time without interfering (`--limit=N` restricts a run to N files, handy for a quick check).

## Hidden folders

Folders whose name starts with a dot (`.nu`, `.thumbnails`, `.webaxs`, `.picasaoriginals`, `.comments`:
leftovers of old programs) are not listed in the folder view, and their Photos are not counted in the
folders that contain them. Nothing is deleted, scans still read them, and a hidden folder can be opened
by its path (for example `#/2001/new-epoch/.nu`). A file whose own name starts with a dot is not
affected. Restart the app after updating so the server code is current.

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
