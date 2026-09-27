# Archive tools

Compare this library against an older backup copy on another machine, drive, or disk image: find
out what's there that isn't here, and split that into content you already have, content you
already rejected and deleted on purpose, content that went missing some other way (recoverable),
and content this repo has never seen before (a candidate to import). See
[docs/tickets/131.md](../../docs/tickets/131.md) for the epic this grew out of and why (it replaced
a pile of ad hoc, per-purpose scripts that used to live at the repo root).

Every tool here is either read-only, or writes only a shell script for you to review before running
it by hand; the one exception (`apply.py --apply`) only ever *adds* files, and only into this
app's own configured `pictures_dir` — see each tool's own section below for exactly what it
touches.

## The tools

| Tool | Reads | Writes | What it's for |
|---|---|---|---|
| `catalog.py` | a directory tree | a sqlite hash catalog | incrementally hash a tree this app doesn't own |
| `import_sha224sum.py` | a `sha224sum` listing | a sqlite hash catalog | bootstrap a catalog without re-hashing, from a listing you already have |
| `compare.py` | this machine's export + a target catalog | a report (stdout) | classify a target copy's files: have / rejected / lost / new |
| `apply.py` | the same, + the target's own files | a script (stdout), or the copies themselves with `--apply` | act on `compare.py`'s classification |
| `find_empty_dirs.py` | a directory tree | a script (stdout) | generic housekeeping, unrelated to hashing |

Two more, unrelated to comparison but generic enough to belong here too:
`../check_images.py` and `../measure_navigation.py` are the app's own smoke/perf tools, one level up.

### `catalog.py`: an incremental hash catalog of a tree

    tools/archive/catalog.py --root_dir=/mnt/backup/Pictures --db=~/backup-catalog.sqlite --logtostderr --v=1

Walks the tree and records each file's sha224 hash into a sqlite `hashes(filename, hash, mtime)`
table, alongside a `dir_mtimes` cache. A directory whose own mtime hasn't changed since the last
run is skipped entirely; within a directory that is rescanned, a file whose own mtime still
matches is left alone. So re-running this against a large, mostly-unchanged backup only pays for
what actually changed — add, remove, or rename a file and the next run notices; edit a file's
bytes in place with the same name and it won't (photos in this kind of archive are add/remove/
rename, never edited in place; see the tool's own docstring). `--dir` scopes a run to one
subdirectory of `--root_dir` without touching the rest of the catalog.

This is also the format `photoapp`'s own `--hashes_db` flag reads (see
[docs/design/databases.md](../../docs/design/databases.md)) — a catalog built here can speed up a
real scan of the same tree later, if that tree ever becomes a `pictures_dir`.

### `import_sha224sum.py`: bootstrap a catalog from a plain checksum listing

    sha224sum -b $(find /mnt/backup -type f) | tools/archive/import_sha224sum.py --db=~/backup-catalog.sqlite

For a backup that already has a `sha224sum` listing (a NAS's own backup tooling, or a machine with
no Python environment to run `catalog.py` on) — writes the same `hashes` table `catalog.py` does,
so the two are interchangeable as `compare.py`/`apply.py` inputs. `nas.txt` at the repo root (not
tracked by version control) is a real example of exactly this kind of listing.

### `compare.py`: classify a target copy against this machine

    python -m photoapp.export_catalog --output=live.jsonl
    tools/archive/compare.py --live=live.jsonl --target_db=~/backup-catalog.sqlite > report.txt

`python -m photoapp.export_catalog` (in `photoapp/`, not here — it needs the live database) writes
this machine's own catalog (every non-missing file's path+hash) and its durable, hash-keyed
decisions (every reject/keep/favorite this app ever recorded, independent of whether the file
itself still exists — see "Why this works even after a purge" below) to a JSONL file.

`compare.py` then classifies every file in `--target_db` into exactly one bucket:

- **have** — already in the live library (a rename on either side doesn't matter, only the hash does).
- **rejected** — not live, but this machine rejected and deleted it on purpose. Expected, not a problem.
- **lost** — not live, but this machine's records say it was a keeper. Missing some other way than
  a deliberate reject — recoverable from the target copy.
- **new** — neither of the above. This repo has never had an opinion on it.

The report is plain, tab-separated text (`bucket<TAB>path<TAB>hash`) with counts up top — pipe it
through `grep ^lost` or similar, or hand it to `apply.py`. `--reverse` asks the opposite question
(which of this machine's live hashes are entirely absent from the target copy?), useful for
deciding whether an old backup is safe to retire.

### `apply.py`: act on a `compare.py`-style classification

    tools/archive/apply.py --live=live.jsonl --target_db=~/backup-catalog.sqlite \
        --target_root=/mnt/backup/Pictures > apply.sh          # review only; nothing is touched
    tools/archive/apply.py --live=live.jsonl --target_db=~/backup-catalog.sqlite \
        --target_root=/mnt/backup/Pictures --apply              # also copies 'lost' and 'new' files in

Re-runs the same classification `compare.py` does, then:

- **rejected** → always a printed `rm -v --` line for the target copy's file. This tool **never**
  deletes anything on a target copy itself, `--apply` or not — review the script and run it
  yourself if and when you're ready to. (Also: this tool typically has no reliable write access to
  "another machine" in the first place.)
- **lost** / **new** → a printed `cp -v -n` line by default; with `--apply`, this tool performs the
  copy itself, straight into this app's own configured `pictures_dir` (read from `photos.toml` /
  `--pictures_dir`, the same way the server itself is configured — never a separately-typed path
  that could drift from what the app actually expects), preserving the target copy's relative
  directory structure. An existing destination is always skipped, never overwritten.

A copied-in file is a completely ordinary file at a plain path — there is no "pending import" state
in the database. **Run a Rescan (the button, or `python -m photoapp.fullscan`) afterwards** to pick
it up as a normal, unrated Photo.

`--target_root` is the same directory `catalog.py`'s `--root_dir` pointed at (or, for a listing
imported via `import_sha224sum.py`, whatever directory the listing's paths are relative to).

### `find_empty_dirs.py`: generic housekeeping

    tools/archive/find_empty_dirs.py --dir=/mnt/backup/Pictures > find_empty_dirs.sh

Prints (deepest-first) `rmdir` lines for every directory that is empty or contains nothing but
other such empty directories. Handy after `apply.py`'s `rm` script has cleared files off a backup
and left bare directories behind. Not hash-based, not part of the comparison pipeline — just a
small tool that happens to live here.

## Why this works even after a purge

`rating_by_hash` (the table `python -m photoapp.export_catalog`'s "decision" rows come from) has no
foreign key onto `files`. It survives a photo being rejected, moved to `.trash/`, and permanently
purged after `trash.RETENTION_DAYS` — nothing in this codebase ever deletes a row from it. So the
"rejected" bucket above is reliable even for a photo whose trace on this machine is long gone; see
[docs/tickets/131.md](../../docs/tickets/131.md)'s Findings for how this was confirmed before any
of this tooling was built.

## A worked example

Two toy libraries: `~/photos` (the live one, already running through `photoapp`) and `/mnt/old-backup`
(an older copy on an external drive). `~/photos/beach.jpg` was rejected and deleted a while ago;
`/mnt/old-backup` still has it, plus a photo `~/photos` has never seen.

    # 1. What does this machine currently have and know? (run from the photoapp checkout)
    python -m photoapp.export_catalog --output=/tmp/live.jsonl

    # 2. Catalog the backup (incremental -- safe and cheap to re-run later)
    tools/archive/catalog.py --root_dir=/mnt/old-backup --db=/tmp/backup.sqlite --v=1 --logtostderr

    # 3. Compare
    tools/archive/compare.py --live=/tmp/live.jsonl --target_db=/tmp/backup.sqlite
    #   # have: 41
    #   # rejected: 1
    #   # lost: 0
    #   # new: 1
    #   #
    #   # bucket	path	hash
    #   new	newcamera/IMG_0512.jpg	<hash>
    #   rejected	beach.jpg	<hash>

    # 4. Review, then act (dry run first)
    tools/archive/apply.py --live=/tmp/live.jsonl --target_db=/tmp/backup.sqlite \
        --target_root=/mnt/old-backup
    #   #!/bin/sh
    #   ...
    #   cp -v -n -- /mnt/old-backup/newcamera/IMG_0512.jpg /home/you/photos/newcamera/IMG_0512.jpg
    #   rm -v -- /mnt/old-backup/beach.jpg

    # 5. Actually copy the new photo in (never deletes beach.jpg on the backup -- that's a choice
    #    you make yourself, by running the printed rm line, or not, whenever you're ready)
    tools/archive/apply.py --live=/tmp/live.jsonl --target_db=/tmp/backup.sqlite \
        --target_root=/mnt/old-backup --apply

    # 6. Pick it up as a normal Photo
    python -m photoapp.fullscan   # or the Rescan button

## Keeping a backup's catalog current (a periodic refresh)

`catalog.py` is cheap to re-run against an unchanged tree (see above), so a backup you check
against repeatedly is worth keeping cataloged rather than rebuilding from scratch each time. A cron
entry (or a line in whatever already backs up that drive) works fine — nothing here needs to run as
a service:

    # crontab -e, on the machine that can see the backup drive:
    0 4 * * 0  nice -n19 ionice -c3 /path/to/photos/tools/archive/catalog.py \
               --root_dir=/mnt/old-backup --db=/var/backups/old-backup-catalog.sqlite \
               --logtostderr --v=1 >> /var/log/backup-catalog.log 2>&1

(This replaces the old `update.sh`, which drove the now-retired `hash_dir.py`/`file_metadata.py`
pair the same way, once a week, at low priority.)
