# Exporting to Google Photos

The Export dialog can also upload what it exports to Google Photos as an album (epic 205). The export
to `Exported/` always happens first and its files stay there; the upload is an optional extra step.

## One-time setup

1. In the [Google Cloud console](https://console.cloud.google.com/) create (or pick) a project and
   enable the **Photos Library API**.
2. *APIs & Services → OAuth consent screen*: user type **External**, add yourself as a test user, and
   add the scopes `photoslibrary.appendonly`, `photoslibrary.readonly.appcreateddata` and
   `photoslibrary.edit.appcreateddata`. Then **publish the app ("In production")**: in *Testing* status
   Google expires refresh tokens after **7 days** (you would have to authorize again every week). An
   unverified production app shows a "Google hasn't verified this app" screen once, at authorization:
   *Advanced → Go to … (unsafe)*. (Whether Google keeps honouring an unverified personal app is not
   documented; if authorization stops working, see below.)
3. *Credentials → Create credentials → OAuth client ID*, type **Desktop app**. Download the JSON and
   save it as `<state_dir>/gphotos_client.json` (or point `--gphotos_client_file` at it). `chmod 600`.
4. Run `python -m photoapp.google_auth`, open the printed URL in any browser and approve. The
   browser then fails to load a `http://127.0.0.1:1/?code=...` page (expected); paste the code, or the
   whole address, at the prompt. This works over ssh. The refresh token is stored in `<state_dir>/gphotos_token.json` (mode 600).

When the Export dialog's *Also upload to Google Photos* box is enabled, setup is complete. If Google
stops accepting the token ("expired or was revoked" on the Jobs page), run `google_auth` again.

## What it does

- Tick *Also upload to Google Photos* in the Export dialog; the album name defaults to the folder
  ("2024/trip" → "2024 trip"). *New album* is the default; *Add to an existing album* lists albums
  this app created earlier, best match first. A same-name album triggers a warning that offers
  adding to it instead.
- Photos are the rendered export JPEGs with their EXIF (date, camera, lens, exposure). **GPS is
  stripped unless you tick *Include GPS location* in that same dialog; it is off every time.** The
  box governs the files in `Exported/` too. Videos are the original file (hard-linked into `Exported/`).
- Items go into the album in the grid's order, appended after anything already in it. Progress and any
  failed files show on the Jobs page, with *Retry failed*. Uploading the same bytes again never makes a
  duplicate (Google treats identical bytes as one item), so a retry or a re-run is safe.
- Uploads over 25 MB each count against your Google account storage.

## Limits that come from Google (checked 2026-10-10)

- Since March 2025 the Library API only sees albums and photos **this app created**. The album picker
  therefore never lists albums you made in the Google Photos app, and the app can't read your library.
- No API sets favourites or stars, so ratings stay local. Captions (`description`) are supported by the
  API but this app has no caption field yet.
- 10,000 API requests per project per day (about 9,000 photos); 429 answers are waited out (≥30 s).
- Anything you change in Google Photos (moving items between albums, deleting) is invisible to this
  app; it keeps no record of albums. A later epic may import from Google Photos.

## What leaves the machine

Exactly the files of the export (plus their EXIF, minus GPS unless ticked), sent to Google over HTTPS
with your own OAuth grant. The grant can only add items and manage items this app created. Nothing is
uploaded unless the box is ticked in that dialog.
