# Google Photos export (epic 205)

Why it is shaped the way it is. How to set it up and use it: [../google-photos.md](../google-photos.md).

- **An optional step of Export, after rendering into `Exported/`.** The files stay there (mirroring the
  library tree), so a future "import from Google Photos" can dedupe against them by hash. The upload job
  reads those files; nothing is rendered twice.
- **The app remembers the albums it uploaded to (ticket 232).** Since March 2025 the API only shows
  this app its own albums, ~10 per request (a listing is ~12 requests, ~15 s), and the app should not
  talk to Google outside an export. So every upload job records its album (id, title, item count) in
  the `gphotos_albums` table and the export dialog lists from there; Google is asked for its list only
  by the dialog's explicit "Refresh from Google", which syncs the table (finds older albums, forgets
  deleted ones). Google stays authoritative; the table can drift until a refresh. The `gphotos_upload`
  job row still holds album id, items, failures (`jobs.options`) and progress (`jobs.note`), and the
  nightly prune removes it like any other job; the table is what outlives it.
- **Idempotence comes from Google**: identical bytes are one media item, so retrying failures or
  re-running an export into the same album cannot duplicate. That replaces a local "uploaded" table.
- **Ordering**: items are added in the order the request lists them (the grid's order), one serial
  `batchCreate` per chunk of 50 (the API forbids parallel batch writes), byte uploads in parallel
  within a chunk. Adding to an existing album only appends; interleaving by `albumPosition` was rejected
  as murky for little value.
- **A job that has failures still finishes** (state `done`, note "uploaded 2 of 3, 1 failed", failures in
  `options.failed`); only a fatal problem (not authorized, album can't be created) fails it. The album
  is created lazily, right before the first successful batch, and its id is saved at once so a restart
  can't create a second one. A request whose files all fail leaves no empty album.
- **EXIF is how metadata travels** (`exif_write.py`): Google dates and describes items from the file's
  EXIF. It is built from the scan's `files` row plus exposure bias, white balance and (only on request)
  GPS from the source, and spliced into the JPEG bytes without re-encoding; ICC is untouched, orientation
  never written (pixels are upright), no maker notes or embedded thumbnails.
- **GPS is a per-export opt-in, not a setting**, and it governs the local file too: writing GPS locally
  and stripping it for the upload would need a second copy of every file, and exported files get shared.
- **`export.file_to_upload()` is the one place that decides which file goes up for a Photo** — today the
  exported file itself; a future video-tuning epic replaces it with "the selected tuning".
- **Auth lives outside the web app**: a one-time `python -m photoapp.google_auth` PKCE flow (prints a URL, the user pastes the code back);
  the app only reads the token file and refreshes the access token. `invalid_grant` surfaces as "run
  google_auth again".
- **Tests never touch Google**: `tests/fake_gphotos.py` is a real-HTTP fake (byte dedupe, 207-style
  partial results, injectable 429/500), selected through `--gphotos_api_base_url`.
- Not done, on purpose or for lack of an API: favourites/stars, captions (needs a DB caption field),
  sync from Google, albums as tags, listing albums made in the Google Photos app. See ticket 216.
