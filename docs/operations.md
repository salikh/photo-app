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
| `--job_workers` | 2 | background RAW render threads |
| `--scan_workers` | 8 | threads reading files during a scan (the NAS is latency bound) |
| `--port`, `--host` | 8080, 0.0.0.0 | LAN only, no authentication |

Try a first session with `--xmp_dry_run` to see what would be written.

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
- Only the original's sidecar is written for a rating; other sidecars of the same Photo are read
  and shown as a conflict, never modified.
- Every change is in the activity log and can be undone; undo refuses if the value changed since.
