# Philosophy

This is not a general-purpose photo manager. It's mine, built for one real, messy, decades-old
photo library, and every non-obvious decision in this codebase traces back to that. If you're
picking this project up cold — a future version of me, or an agent working on it — read this
first; it should save you from re-deriving a stance the hard way, or worse, quietly reversing one
without knowing it was deliberate.

## The archive decides more than the code does

The library predates this app by years: a darktable-managed tree, not Lightroom (15,427 of
15,429 sidecars on disk are darktable's own, full-filename style), with RAW+JPEG pairs, old
exports, and duplicate copies scattered across backups on other machines. A tool built for a tidy,
single-source library would be wrong for this one on day one.

So: **an original is never moved, renamed, or deleted by anything automatic — only by explicit,
reviewable user action, and always as a move, never an unlink.** The only write this app makes on
its own initiative is an XMP sidecar (atomically, after a first-seen backup); everything else that
touches an original happens because a person clicked something that named exactly that action: a
rejected photo moves into `.trash/` (see [docs/design/trash.md](docs/design/trash.md)), and a photo
the user picks and drags to another folder moves there, sidecars and all (ticket 144, see
[docs/design/move.md](docs/design/move.md)) — never a scan, a background job, or a heuristic
deciding to relocate something on its own. The sqlite
database is a deliberately rebuildable cache, not the source of truth; deleting it costs you
nothing durable, because a rescan rebuilds it from the files and sidecars themselves. And
decisions outlive their files: `rating_by_hash` remembers a rejected or kept photo's content hash
with no foreign key back to the file it came from, so it survives that file being trashed and
permanently purged. Epic 131 built on exactly this — making that durability *portable* across a
backup on another machine, not just durable on this one, precisely because "another copy of the
same archive, out there somewhere, in an unknown state" is not a hypothetical here.

## No auth here isn't "not exposed" — it's a boundary

This app has no login of its own, and that's easy to misread as "LAN-only, never on the internet."
It isn't. I do want authenticated remote access — it's just not this codebase's job to provide it.
A private, OAuth-authenticated HTTPS proxy sits in front of it and does that instead. This app
assumes that boundary exists and stays out of the business of reimplementing it, the same way it
stays out of the business of re-implementing a filesystem. One trusted operator per deployment;
authentication is somebody else's layer, on purpose.

Everything else about the interaction design follows from actually using this daily: preloading
and a virtualized filmstrip because a slow viewer makes culling a chore, not a pleasure; a
keyboard-first HUD with a phone-friendly swipe fallback because that's genuinely how I rate photos
— desk and keyboard most of the time, phone on the couch sometimes. None of it is aimed at anyone
else's taste.

## A durable record, because the work outlives any one session

Most of this codebase was written across a long series of AI-agent coding sessions — not
continuously, and not by one tool: Claude Code did the bulk of it, but OpenCode with DeepSeek
Flash and Google Antigravity with Gemini Flash were both actively used too, at different points.
That fact shapes the process more than it might seem to. A record that only made sense to
"whichever tool wrote it last" would fall apart the moment a different one picked the thread back
up — so the discipline here isn't really about any one tool's conventions, it's a paper trail built
to survive being handed off, tool-agnostically: a new request becomes a ticket in `docs/tickets/`
before it becomes code; a design question that needs real judgment (not just an engineering
trade-off) becomes its own **question ticket**, answered by me, rather than guessed at (see
tickets 090, 097, 103, 116, and 132/136); `TODO.md` holds only what's still
open and how to restart; and every logical step gets its own `jj commit`, so the history itself is
the second record, not just the ticket files. `docs/design/*.md` exists for the same reason at a
different grain — the *why* behind a subsystem, once it's settled, written down once instead of
re-discovered from source every time.

The other half of the discipline: a change is trusted once it's been checked against the real
library's actual mess, not just synthetic test fixtures — a real DNG, a real sample of sidecars, a
real backup catalog — because this archive has repeatedly turned out to have edge cases no
synthetic fixture would think to include.

## What changed along the way

The instinct going in was usually a reasonable first guess, and got revised more than once when
the real constraints pushed back: RAW rendering started as two separate paths (an on-demand one
and a background one) and was deliberately merged into one after ticket 090 concluded the split
itself was the actual bug, not something to patch around. The lossy RAW-tuning preview (epic 102)
was expected to be a pre-demosaiced DNG; checking what the renderer actually needed reversed that
call before any code shipped. `dcraw` is gone entirely now, replaced end to end by `rawpy`/LibRaw.
None of that was known up front — it came from building the thing and then checking it against
real files. Not everything is resolved, either: a couple of the browser tests are known-flaky under
this machine's own memory pressure, and that's a tracked, accepted cost, not an oversight.

## What this deliberately isn't

Not a product chasing users. Not multi-tenant — "single-user" here means *one trusted operator per
deployment*, which is compatible with, not contradicted by, ticket 130 making the app itself
portable: that work makes the same deployment shape easy to stand up again (for me, on another
machine, or for someone else entirely), it doesn't turn one running instance into a shared service
with accounts. And, per the section above, not a codebase that reimplements authentication or
internet-exposure handling itself — that's satisfied by composition, deliberately, not by scope
creep into this app.

## So, going forward

A change that touches an original file earns the same scrutiny ticket 072's delete flow (and ticket
144's folder move) got before they shipped — reversible or additive by default, a real deletion
only after it's been thought through in its own ticket. A design call that needs real judgment, not just engineering trade-offs,
becomes a question ticket addressed to me, not a guess dressed up as a decision. And if a future
ticket's Findings shift one of the stances above — not just add a feature, but actually change *why*
this project is shaped the way it is — that ticket's implementation touches this file too, the same
way `docs/design/trash.md` already gets amended alongside the tickets that touched it. This
document is meant to stay true, not just to have been true once.

---

For the detailed *why* behind a specific subsystem, see [docs/design/](docs/design/README.md); for
how a specific stance was reached, the ticket numbers above are in `docs/tickets/`. The archive
tooling mentioned above (epic 131) is [docs/tickets/131.md](docs/tickets/131.md).
