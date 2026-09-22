# Ratings and XMP sidecars

Code: `photoapp/ratings.py`, `photoapp/xmp.py`, `photoapp/curation.py`, `photoapp/import_ratings.py`,
`photoapp/recovery.py`. Schema: `photos.rating`/`fav`/`rating_source`/`rating_updated_at`/`previous_stars`,
`xmp_sidecars`, `rating_by_hash`.

## The combined rating

One integer in `[-1, 5]`: `-1` reject, `0` unrated, `1..5` picked with that many stars. This single value (not
separate "picked" and "star count" fields) matches what `image_metadata.py` already used, and it is what
`ratings.clamp`/`ratings.step` operate on everywhere — the keyboard, swipe gestures, and the filter buttons all
share this one representation rather than each having their own picked/rejected/starred logic.

## The library is darktable, not Lightroom — and it shows

The requirements were written assuming Lightroom-authored sidecars (`.xmp` next to a RAW). Measuring the real
library (ticket 015/018) found **15,427 of 15,429 sidecars are darktable-written, full-filename style**
(`K.DNG.xmp` *and* `K.JPG.xmp` separately) — the opposite of the original assumption. Two decisions changed as a
direct result, both confirmed with the user rather than just adopted from the measurement:

- **New sidecar naming** (`--new_raw_sidecar_style`, default `full`, ticket 042): new RAW sidecars are named
  `NAME.DNG.xmp` to match what is already there, not `NAME.xmp` as first specified. Existing sidecars are always
  read and edited in whatever style they already use — this only affects sidecars this app creates from
  scratch.
- **darktable's default 1-star rating pollutes "picked"** (ticket 049): a survey found **76% of all rated
  sidecars are exactly 1 star** — darktable's import default, not a deliberate choice by anyone. Rather than
  guess which 1-star ratings are real, `--one_star_is_unrated` is an opt-in, **display-only** flag: with it on,
  1 star is shown and filtered as unrated, `picked` means 2+ stars, and no rating control ever offers "1" — but
  nothing on disk or in the database changes. This is why `photoapp/static/rating.js` has a `display()` function
  separate from the raw stored value, and why `GET /api/config` exists at all (the frontend needs to know the
  flag to render consistently with what the server's filters will return).

## Darktable treats a DNG and its JPEG as two pictures — the reject rule

darktable rates the DNG and the camera JPEG **independently**; a reject on one sidecar very often means "keep
only the other file of the pair," not "reject this Photo." A whole-library survey (ticket 053) found 87
Photos where one sidecar says reject and another says something positive; naive "just take the newest" would
have hidden 75 of those rejects behind a newer non-reject value — mostly darktable's default 1 star (above).

The resolved rule, in `ratings.resolve`: when the newest candidate would be a reject *that comes from a single
sidecar*, and another sidecar of the same Photo is picked (rating > 0), the reject is **overruled** — the Photo
takes the newest *picked* sidecar's rating instead. A reject that comes from **this app** (which always writes
both sidecars at once, see below) is never overruled this way, because by construction nothing is picked in the
pair once the app has rejected it. `Resolved.reject_overruled` reports which case happened, and it feeds both
the survey report and the Attention page.

**Un-reject restores the star count** (`photos.previous_stars`, ticket 024, confirmed default): rejecting a
rated Photo remembers its star count (XMP's `Rating=-1` itself carries no such memory — the star count would
otherwise be lost), and un-reject via `X` or swipe-up restores it. This value only exists in the database, which
is why it is one of the things listed as not-rebuildable in `docs/operations.md`.

## Both sidecars are written, on purpose

The original plan wrote only the original's (DNG's) sidecar. Ticket 023 changed this: an edit now writes the
Photo's rating/fav/tags to **both** the original's and the camera JPEG's sidecars (creating the JPEG's sidecar
if it doesn't exist), specifically so darktable — which reads each file's own sidecar independently — shows a
consistent rating for both renditions after an edit in this app. Tags are converged conservatively: only tags
this app already knew about are ever removed from a sidecar, so a tag darktable added between scans is never
silently wiped by an unrelated edit.

## The database became a rating source, not just a cache — and a bug that produced from it

Originally "XMP sidecars are the source of truth, the database is a rebuildable cache." Ticket 021 changed the
rule to **the newest rating wins, whether it lives in the database or in a sidecar** — needed because an
imported rating or a hash-recovered rating can legitimately be newer than anything in a sidecar, and the old
rule made those permanently second-class. `ratings.resolve` takes the database's own dated value
(`photos.rating_updated_at`) as one more candidate alongside the sidecars, with a small clock-skew tolerance
(`SKEW_TOLERANCE = 2.0` seconds — the app machine and the file server have different clocks) so a tie doesn't
flip-flop.

**The subtlety that caused a real bug**: not every value in `photos.rating` is an independent source. Most of
the time it is just a *cache* of what a sidecar already said (`rating_source = 'xmp'`), copied there by the
scan. Treating *that* as an independent "database" candidate made every single sidecar look like it was in
conflict with "the database" — because they're the same value with a `>=` tie that always favored the cache.
`ratings.DECIDED_HERE = ("app", "import", "hash-recovery")` is the fix: only a rating the database *decided by
itself* (an app edit, an import, a hash recovery) counts as a source; a rating merely read from XMP does not.
This was found by running the survey against a real copy of the whole-library database, not by inspection — the
survey's `sidecars_behind` count only made sense once this distinction was added.

**Sidecars catch up on the next edit, not proactively** (ticket 065, confirmed): when the database is newer
than a Photo's sidecars, the UI shows the database's value and flags `conflict` ("sidecars behind" on the
Attention page), but nothing writes to disk until that Photo is next edited — even an edit unrelated to rating
(toggling fav, say) still writes the *current* (possibly db-sourced) rating to both sidecars as a side effect of
`curation._apply` always including the current rating in what it writes. The scan itself never writes sidecars
by design, so this is the only path that brings them back in sync. A future **batch** sync for Photos that are
never edited again is ticket 068 (explicitly requested, not yet built) — the option of writing sidecars
automatically during a scan was explicitly rejected because it would break the scan's read-only-on-the-library
guarantee.

## Ratings that arrive from outside carry their *original* time, not "now"

An imported rating (`import_ratings.py`, ticket 022, reading `image_metadata.py`'s output) or a hash-recovered
rating (`recovery.py`, ticket 036) must be compared against sidecars by *when it was actually decided*, not by
when the import happened to run — otherwise running an import at all would blow away every newer sidecar edit regardless of
which was really more recent. `image_metadata.py` gained a `rating_time` column (the mtime of the file the
rating came from) specifically to support this; `import_ratings` only applies a row if its time is newer than
the Photo's current database time and its newest sidecar, and an import with no dated rows only fills Photos
that have no dated rating at all yet.

## Hash recovery: rename/move detection, deliberately conservative

A rating's primary key is its path; `rating_by_hash` is a fallback keyed by content hash, used when a file
moves or is renamed without its sidecar. Recovery only fires when the match is **unambiguous** — exactly one
remembered hash, matching exactly one currently-unrated file, whose old path is confirmed gone. Anything with
more than one candidate is left alone and surfaced on the Attention page instead of being guessed at, because a
wrong automatic rating write is much worse than an unrated photo staying unrated one scan longer.

## Lossless XMP editing

`xmp.py` edits sidecars by locating and replacing only the specific bytes for `xmp:Rating` and `dc:subject`
(regex-based, operating on the raw bytes/text, not by re-serializing a parsed DOM), specifically so darktable's
`crs:*`/`darktable:*` develop data, comments, and unrelated namespaces round-trip byte-for-byte untouched. This
was verified against 593 real sidecars sampled from the library (ticket 020), which is also how two real bugs
were found: a `dc:subject` whose XML namespace prefix was declared on the element itself (not inherited from
`rdf:Description`) wasn't found on a second edit, and a rating could be written to the wrong
`rdf:Description` block in a sidecar containing more than one. Every write is atomic (temp file + `fsync` +
rename) and keeps a one-time backup of the sidecar's first-seen bytes before any change, under
`<state_dir>/xmp_backups/`.
