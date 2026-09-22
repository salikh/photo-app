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

## Filtering (culling)

The row under the title filters the folder by rating: `All`, `Rejected`, `Unrated`, and exactly `★1`..`★5`
(`★1` is left out with `--one_star_is_unrated`). The `more` menu has picked, rated, favorites and
"sidecars disagree". The filter is kept in the URL and when you change folder, and the viewer shows it.
Each button shows how many photos of the folder it holds (dimmed at 0), kept current as you rate. In the grid
(not in the viewer): `Shift`+`A` all, `Shift`+`X` rejected, `Shift`+`0` unrated, `Shift`+`1`..`5` exactly that many stars.

While a filter is active, a photo whose rating stops matching it **leaves the view** at once (grid and viewer):
in the viewer the next photo is shown, when the last one leaves the viewer closes, and `U` brings the photo
back where it was. So `Unrated` + a rating key is the fast cull: rate, and the next unrated photo is in front of you.

## Zoom

`Z`, a click, or a double tap zooms to 100% of the original pixels; pinch (two fingers, or a trackpad, or `Ctrl`+wheel) zooms
between fit and 4x around the fingers, drag pans, `+`/`-` zoom, double tap or `Z` goes back. Only the photo scales, not the page.

## The thumbnail strip

Under the photo, a strip shows every photo of the folder (and filter). Scroll it (wheel, trackpad, or a sideways swipe on a phone) and
click or tap a thumbnail to jump there; it follows the current photo and stays out of your way while you scroll it by hand.

## Speed of the viewer

Moving to another photo or zooming is meant to be instant: the viewer preloads the pictures of the two photos each side of the
current one (2000 px first, then the full-size image, which is also decoded ahead after a short pause), and stops on a data-saving
or slow connection. `tools/measure_navigation.py` measures it on large synthetic photos.

## Keys in the viewer

`←`/`→` navigate, `↑`/`↓` rating +1/-1 (clamped, un-reject restores the previous stars), `0`-`5` rate, `X` reject (again: restore), `F` favorite, `T` tags (`-name` removes),
`Z` zoom to full size, `G` cycle the shown file of a DNG+JPG pair, `I` files panel, `U` undo, `Esc` back.
The buttons under the photo are `✖ ☆ 1 2 3 4 5` (reject first), then favorite, tag, undo, files, close.
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
