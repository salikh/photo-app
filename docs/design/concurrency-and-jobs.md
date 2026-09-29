# Concurrency, the job queue, and the busy database

Code: `photoapp/jobs.py`, `photoapp/db.py` (`retry_busy`, `is_busy`), `photoapp/api.py` (`db_route`, `run_db`).

## sqlite has one writer; everything downstream follows from that

The web app, a running scan (manual or nightly), the on-demand RAW-render job queue, and the background
thumbnail populator (ticket 066) can all be writing to the same sqlite file at once. sqlite allows exactly one
writer at a time; a second writer gets `database is locked` until the first finishes. Two things in this
codebase exist specifically to make that survivable rather than surprising:

### The claim-scoping bug

`JobQueue` instances share the `jobs` table by design (kind differentiates them, `/api/jobs` lists all of them
together so the Jobs page shows everything in one place — see [thumbnails.md](thumbnails.md) for the two
concrete queues that share it). The `_claim` query originally read:

```sql
SELECT * FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1
```

with no `kind` filter at all. This worked as long as only one `JobQueue` existed in the process. The moment a
second one was added (ticket 066's low-priority populate queue, alongside the existing on-demand `raw_render`
queue), it would have periodically claimed a job it had no handler for and failed it with an unknown-kind error
— silently corrupting the *other* queue's work. This was caught by design review before it shipped, with a
regression test (`test_two_queues_sharing_one_table_only_claim_their_own_kind`) that interleaves enqueues of two
kinds across two queues specifically to make the bug reproduce if it ever comes back. The fix scopes the claim
to `kind IN (...)` over that instance's own registered handlers. **Any future code that creates a new
`JobQueue` sharing this table is safe by construction now** — but if `_claim` is ever touched again, this is the
property that must be preserved.

### Retrying instead of failing

The web app (ticket 052) retries a locked database with exponential backoff (0.1s, 0.2s, 0.4s, ... capped at 4s)
for about a minute (`--busy_retry_seconds`) before giving up with a 500 the frontend shows as a toast
(`db.retry_busy`/`db.DatabaseBusy`, wired through every route via the `db_route` decorator in `api.py`). Two
details matter more than they look:

- **The in-process lock is released while sleeping** (`run_db`'s retry loop is outside `app.state.db_lock`), so
  one slow/busy request does not stall every other concurrent request behind it.
- **A rating edit takes sqlite's write lock (`BEGIN IMMEDIATE`) *before* touching any XMP sidecar file**
  (`curation._apply`). This was added specifically so that a failed attempt — busy database, or any other
  failure — has never written a sidecar the database doesn't yet agree with. It also means a failed edit is
  safe to simply retry from scratch, which is exactly what the frontend does. Background writers (a running
  scan, the job workers) instead use a long sqlite-level `busy_timeout` (60-120s) and just wait, since they have
  no user watching a spinner.

## Low priority is per-thread, not per-process

`JobQueue(low_priority=True)` (used only by the background thumbnail populator, ticket 066) lowers CPU niceness
(`os.nice(19)`) and I/O class (`ionice -c3`, best-effort) for its own worker thread only. On Linux,
`setpriority`/`nice`/`ioprio_set` with a pid of 0 (or `nice()`) act on the **calling thread**, not the whole
process — threads are separate schedulable tasks — so this genuinely does not slow down the rest of the running
app, verified with a test that checks the *caller's* niceness is unaffected while the worker thread's is 19.
This is what makes it safe to run the bulk populator inside the long-lived process's job-queue machinery instead
of needing a fully separate OS process for isolation.

In practice this shows up as **the populate job progressing very slowly whenever the app is under real,
interactive use** — that is `ionice -c3` correctly yielding to foreground I/O, not a bug. It also means the
job can be killed by the operating system's own memory-pressure reaping without losing work: `populate_file`
never overwrites an existing thumbnail and the job queue's `enqueue` dedupes by (kind, file_id), so a killed and
restarted run just picks up wherever it left off.

## The interactive rescan is not a job-queue job

The header's "Rescan" button (`POST /api/scan`) runs through `scan.ScanManager`, not `jobs.JobQueue` — it
starts a thread immediately, ahead of anything queued, rather than waiting behind the shared `jobs` table's
backlog. Ticket 140 gave `/api/jobs` a `scan` key (`app.state.scanner.progress`, unchanged shape from
`/api/scan/status`) reported *separately* from `active`/`progress`, deliberately not merged into the same
`Worker: Idle`/`Active — ...` line ticket 113 added: a real library's low-priority `populate_thumb` backlog
can keep that line saying "Active" for a long time on its own, which would otherwise bury whether the user's
own rescan specifically was still running. The Jobs page shows both lines (`Scan: ...` and `Worker: ...`),
polling `/api/jobs` every 1.5s while open.
