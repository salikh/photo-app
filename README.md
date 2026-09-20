# Photo manager

A single-user web app for culling and rating the photo library in `/zoo/Pictures`, reachable from
any device on the LAN (desktop keyboard workflow and a phone-friendly swipe UI, dark theme).
Ratings, favorites and keywords are written to XMP sidecars; the sqlite database is a rebuildable
cache. Originals are never moved or deleted.

    ./start.sh                      # http://localhost:8080/  (creates .venv on first run)
    ./start.sh --xmp_dry_run        # try it without writing anything

- Flags, safety rules and what to back up: [docs/operations.md](docs/operations.md)
- Requirements and plan: [docs/reqs/](docs/reqs/), [docs/plans/](docs/plans/)
- Findings about the real library: [docs/reqs/xmp-survey.md](docs/reqs/xmp-survey.md),
  [docs/reqs/thumbs-layout.md](docs/reqs/thumbs-layout.md)
- Work tracking: [TODO.md](TODO.md) and [docs/tickets/](docs/tickets/)

## Keys in the viewer

`←`/`→` navigate, `0`-`5` rate, `X` reject (again: restore), `F` favorite, `T` tags (`-name` removes),
`Z` zoom to full size, `G` cycle the shown file of a DNG+JPG pair, `I` files panel, `U` undo, `Esc` back.
On a phone: swipe left/right for the next/previous picture, swipe up/down to raise/lower the rating.

## Layout

    photoapp/        backend (FastAPI): scan, grouping, xmp, ratings, curation, thumbs, jobs, api
    photoapp/static/ frontend (plain ES modules, no build step)
    tests/           unit and API tests; tests/e2e drives a real Chrome with Playwright
    *.py             the original command line tools (hash_dir.py, file_metadata.py, ...)

## Tests

    .venv/bin/python -m pytest -q                       # everything (the browser tests take ~2 minutes)
    .venv/bin/python -m pytest -q --ignore=tests/e2e    # fast part
    XMP_SAMPLE_DIR=<copy of real *.xmp> pytest tests/test_xmp_real_sample.py
    REAL_DNG=<path to a .DNG> pytest tests/test_previews.py

The browser tests need Google Chrome installed (Playwright uses it directly, nothing to download).
