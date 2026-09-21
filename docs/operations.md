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

## Hidden folders

Folders whose name starts with a dot (`.nu`, `.thumbnails`, `.webaxs`, `.picasaoriginals`, `.comments`:
leftovers of old programs) are not listed in the folder view, and their Photos are not counted in the
folders that contain them. Nothing is deleted, scans still read them, and a hidden folder can be opened
by its path (for example `#/2001/new-epoch/.nu`). A file whose own name starts with a dot is not
affected. Restart the app after updating so the server code is current.

## What is in the state directory, and what to back up

| Path | Rebuildable from disk? | Back it up? |
|---|---|---|
| `app.sqlite` (files, photos, tags, sidecar cache, thumbs, jobs) | yes, by rescanning | not needed |
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
