# The viewer frontend

Code: `photoapp/static/loupe.js`, `grid.js`, `preload.js`, `filmstrip.js`, `zoom.js`, `filters.js`, `gestures.js`.
Plain ES modules, no build step, no framework — worth remembering when reading the rest of this: there is no
component tree or reactive store to lean on, so several of the decisions below exist to compensate for that by
hand (an explicit edit-ordering queue, an explicit "what changed" event, explicit sequence guards).

## The rating-clobber race

Found while building ticket 067 (`ArrowUp`/`ArrowDown` rating shortcuts). Every rating/fav edit updates the
display **optimistically and synchronously** on key press (so five rapid key
presses feel instant), then sends an **absolute** value to the server asynchronously. All such requests go
through one shared FIFO (`util.enqueue`) so they reach the server in the order they were made — but that FIFO
only serializes when each *request* fires, not the synchronous local mutation that happens before it. The bug:
press ArrowUp five times fast, and `Object.assign(photo, r.photo)` in an *earlier* request's completion handler
can run **after** a *later* key press has already moved the local rating further ahead — silently snapping the
display back to a stale value. Five rapid presses could leave a photo at 2 stars instead of 5, even though every
individual `{"rating": N}` request that was sent was correct.

This was not found by inspection — it needed a scripted Chrome repro pressing keys faster than one round trip,
which is also why it hid successfully through all of the single-press tests written earlier. The fix
(`beginEdit`/`isLatest` in `loupe.js`, a `WeakMap<photo, sequence number>`) lets every request's write still
happen and still get logged for undo — only the *display update* from a response is skipped if a newer edit of
that same photo has started since the request was sent. Any future code path that does "mutate now, confirm
later" for a field on a `photo` object needs the same guard, or it has the same class of bug; `toggleFav`
already uses it, plain tag edits do not need it because they have no pre-request optimistic mutation to race
against.

## Photos leaving the filtered view: the paging offset problem

Ticket 056. When a filter is active and an edit makes a photo stop matching, it is removed from `state.photos` immediately
(`grid.removePhotos`) and, in the viewer, the display advances to the next one. The non-obvious part is what
this does to **paging**: `state.photos.length` used to *be* the server-side offset for "give me the next page,"
but once photos can be removed from the middle of the list, that count no longer matches how many the server
has actually handed out. `state.loaded` (how many the server has sent) and `state.removed` (how many have since
left locally) are tracked separately, and the next page is requested at `loaded - removed`, not `photos.length`.

A second, subtler problem: a page request can be **in flight** when a removal happens, and its response (queried
against the *old* offset) can arrive describing photos that don't belong where they'd naively be appended.
`state.epoch` is bumped on every removal/re-insertion; a page-load loop checks the epoch before appending its
result and silently discards (and retries) a page whose epoch has gone stale rather than corrupting the list.
This combination (`state.loaded`/`removed`/`epoch`) exists specifically because of a test that held one page
request open with route interception while triggering removals on another page's worth of photos, and only
shows up under exactly that kind of interleaving in practice.

`filters.matches(photo, filterName)` is a second, independent implementation of the server's filter predicates
(`library.filter_condition` in Python), kept **deliberately duplicated** rather than fetched from the server, so
the client can decide "does this edited photo still belong in the current view" instantly, without a round
trip. A test asserts the two independently-written predicates agree across every filter and both settings of
`--one_star_is_unrated`, specifically to catch the two definitions drifting apart if either is changed alone.

## Preloading: direction-aware, tiered, and stops on cellular

`preload.js` keeps up to two neighbors on each side "ready" (`WINDOW = 2`), always starting from the direction
of travel. Two sizes are fetched for each: `Medium` (what's shown on arrival) is *decoded* ahead of time for
every neighbor in the window; `Huge` (the full-size image, used only on zoom) is fetched into the browser cache
for every neighbor but only *decoded* for the very next one in the direction of travel — a decoded 16-megapixel
image is roughly 64MB, so decoding four of them speculatively was judged not worth the memory. After a short
pause on a photo (400ms, `decodeCurrentHuge`), its *own* `Huge` is decoded too, on the theory that a pause means
the user is likely about to zoom — this is what took zoom from ~100ms to ~15ms in measurement (ticket 050,
`tools/measure_navigation.py`). Nothing in this tier system runs when `navigator.connection.saveData` is set or
the connection type looks slow (`2g`/`3g`) — `Medium` still preloads, `Huge` does not.

Preloaded requests that leave the window are actively cancelled (`img.src = ''`) so a user holding an arrow key
does not leave a pile of abandoned downloads queued behind the photo they're actually looking at, and closing
the viewer releases everything held.

## The filmstrip is a virtual list, because "every photo in the folder" can mean thousands

Ticket 059. `filmstrip.js` does not put one DOM node per photo — it sizes an inner element to the *total* list width and
only creates/keeps nodes for what's within `BUFFER` (8) positions of the visible area, discarding the rest as
the strip scrolls. Thumbnail **images** load on an even tighter margin (`LOAD_MARGIN`, 2) and only after
scrolling has been still for `IDLE_MS` (120ms) — sweeping across a folder of un-rendered RAW files at speed
must not fire off a render request for every thumbnail passed over, only the ones actually settled on. A
`MANUAL_MS` (900ms) window after the user's own wheel/touch input suppresses the strip's own "recenter on the
current photo" behavior, so autoscrolling never fights a hand mid-scroll — found necessary specifically because
the "last user interaction" timestamp defaulting to `0` at page load counted as "within the last 900ms" for the
first fraction of a second after open, which a test caught.

## Zoom: gesture math kept separate from DOM wiring, on purpose

Ticket 060. `zoom.js` exports its coordinate math (`fitScale`, `clampOffset`, `zoomAround`) as pure functions, independent of
the Pointer Events plumbing that calls them, specifically so pinch-anchor and pan-clamping correctness could be
tested directly rather than only inferred from on-screen pixel positions. `MAX_SCALE = 4` (four times the
original pixel size) and the fit-to-4x range are implementation choices, not derived from anything measured.
Two interactions with other code deserve calling out: `gestures.js`'s swipe detector cancels an in-progress
swipe the instant a second pointer touches down, so a pinch can never be misread as a fast two-finger swipe; and
`touch-action: none` on the photo stage is what stops the *browser's own* page-zoom from firing at all during a
pinch — without it, iOS/Android would zoom the whole page (header, buttons and all) instead of letting the
app's own transform-based zoom handle it.
