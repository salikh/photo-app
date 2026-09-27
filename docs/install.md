# Installing the photo manager

A single-user web app: a Python server you run on the machine that can read your photos, and a page
you open in any browser on the same network (desktop or phone). There is nothing to build and no
database server to set up.

## 1. Prerequisites

- Linux or macOS (developed on Linux) with **Python 3.11 or newer** and `venv` (on Debian/Ubuntu:
  `sudo apt install python3 python3-venv`).
- `git`, to get the code.
- Nothing else for the server. The RAW decoder (LibRaw, through `rawpy`) and every other dependency
  come as prebuilt Python wheels. The wheels cover the common Linux/macOS x86-64 and arm64 setups;
  on another platform `pip` builds `rawpy` and `lxml` itself, which needs a C compiler and the
  `libxml2`/`libxslt` development headers.
- For the browser tests only: Google Chrome (Playwright drives the installed Chrome, nothing to download).
- Disk: the thumbnail cache is roughly 10-20 % of the size of the library's JPEGs; it can be
  rebuilt at any time and can be put on another disk (`thumbs_dir`).

## 2. Get the code and install

    git clone <this repository> photos
    cd photos
    ./start.sh --help >/dev/null    # optional: creates .venv and installs requirements.txt

`start.sh` creates the virtual environment `.venv/` on first run and installs `requirements.txt`
into it (again whenever that file changes). To do it by hand, or to use a specific interpreter:

    python3.12 -m venv .venv
    .venv/bin/pip install -r requirements.txt

(`PYTHON=python3.12 ./start.sh` picks the interpreter for a new `.venv`.)

## 3. Configure

Copy the example configuration and edit the paths:

    cp photos.example.toml photos.toml
    $EDITOR photos.toml

The settings that matter:

| Setting | Default | What it is |
|---|---|---|
| `pictures_dir` | `~/Pictures` | your photo library (any folder tree of JPEG/PNG/RAW files) |
| `thumbs_dir` | `<state_dir>/thumbs` | thumbnail cache; rebuildable, may be large |
| `state_dir` | `~/.local/share/photos` | database, first-seen XMP backups, manual link log: **back this up** |
| `database_path` | `<state_dir>/app.sqlite` | the sqlite file, if it should live elsewhere |
| `host`, `port` | `0.0.0.0`, `8080` | where to listen; use `127.0.0.1` to keep it to this machine (there is no login) |

`photos.toml` is looked for in this order: `--config=FILE`, `$PHOTOS_CONFIG`, `./photos.toml`
(the directory you run from; `start.sh` runs from the checkout), `~/.config/photos/config.toml`.
Every setting is also a command line flag, and a flag beats the file. All flags and what they do are in
[operations.md](operations.md). A typo in the file (an unknown key, a wrong type) stops the server at
start with a message naming the key.

No configuration file at all also works, as long as your photos are in `~/Pictures`:

    ./start.sh --pictures_dir=/path/to/photos

The app never deletes or moves your originals. It writes XMP sidecar files next to the photos
(ratings, favorites, tags) and, unless you set `write_metadata_json = false`, small `index.json` /
`<name>.json` metadata caches; exports go to `<pictures_dir>/Exported/` and deleted photos to
`<pictures_dir>/.trash/` (see [operations.md](operations.md)).

## 4. First run

Try it without writing anything to your library first:

    ./start.sh --xmp_dry_run

The log ends with `Photos: http://localhost:8080/`; open that (from another device, use this machine's
name or address instead of `localhost`). The first start creates the state directory, the database and
the thumbnail directory. Then press **Rescan** in the page (or run
`.venv/bin/python -m photoapp.fullscan` in another terminal) to read the library: a big library takes a
while the first time (file reading is I/O bound; folders already scanned are skipped next time), and
folders that are finished can be browsed while the rest goes on. Thumbnails are made when a photo is
first opened and, while the machine is idle, in the background.

When you are satisfied, restart without `--xmp_dry_run` (or set `xmp_dry_run = false`).

If you already have ratings in an older database or in darktable sidecars, see
`photoapp/import_ratings.py` and [operations.md](operations.md); sidecars next to the photos are read by
the scan automatically.

## 5. Run it as a service (optional)

A systemd user unit, `~/.config/systemd/user/photos.service`:

    [Unit]
    Description=Photo manager

    [Service]
    WorkingDirectory=%h/photos
    ExecStart=%h/photos/start.sh
    Restart=on-failure

    [Install]
    WantedBy=default.target

then `systemctl --user daemon-reload && systemctl --user enable --now photos`
(`loginctl enable-linger $USER` keeps it running when you are logged out).

## 6. Upgrade, back up, uninstall

- Upgrade: `git pull`, restart. `start.sh` installs new requirements; the database migrates itself.
- Back up: the state directory (see [operations.md](operations.md#what-is-in-the-state-directory-and-what-to-back-up)).
  Your photos and their sidecars are your usual backup's job.
- Uninstall: delete the checkout and the state and thumbnail directories. Sidecars and photos are left as they are.

## 7. Check the installation

    ./start.sh                                       # smoke test: the log shows the URL
    curl -s http://localhost:8080/api/health         # {"ok":true}
    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python -m pytest -q --ignore=tests/e2e # unit and API tests, about half a minute

The installation tests (a brand-new virtual environment built from `requirements.txt`, then
`start.sh` serving a sample library) are opt-in because they download packages:

    PHOTOS_TEST_INSTALL=1 .venv/bin/python -m pytest -q tests/test_fresh_install.py

## Troubleshooting

- `photos: pictures_dir ... is not a directory`: set `pictures_dir` in `photos.toml` or pass `--pictures_dir`.
- `photos: ... unknown setting`: the key is misspelled; the keys are the flag names in `photos.example.toml`.
- `database busy`: another writer (a scan) holds the database; see "A busy database" in operations.md.
- RAW files show no thumbnails: check `pip show rawpy`; the wheel must have installed (see prerequisites).
