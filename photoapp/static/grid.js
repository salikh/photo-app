// Folder view: breadcrumb-independent folder chips, the photo grid, selection.

import {get, post, imgUrl, seedRevisions} from './api.js';
import {el, toast, retryImage, enqueue, setChildren} from './util.js';
import {href, hrefPage} from './route.js';
import {label, REJECT, display, choices} from './rating.js';
import {state} from './state.js';
import {matches, scheduleCountsRefresh} from './filters.js';

const PAGE = window.__pageSize || 1000;   // (the override is a test hook)

export async function loadFolder(route) {
  state.route = route;
  state.selected.clear();
  selectAnchor = null;   // ticket 095: a new folder/filter invalidates any earlier range anchor
  const rec = route.recursive ? '&recursive=1' : '';
  const [dirs, first, counts, tags] = await Promise.all([
    get('/api/dirs?path=' + encodeURIComponent(route.dir)),
    get(`/api/photos?dir=${encodeURIComponent(route.dir)}&sort=${route.sort}&filter=${route.filter}&limit=${PAGE}${rec}`),
    get('/api/photos/counts?dir=' + encodeURIComponent(route.dir) + rec),
    get('/api/photos/tags?dir=' + encodeURIComponent(route.dir) + rec),
  ]);
  state.dirs = dirs;
  state.counts = counts.counts;
  state.tags = tags.tags;
  state.photos = first.photos;
  state.total = first.total;
  state.loaded = first.photos.length;
  state.removed = 0;
  seedRevisions(first.photos);   // ticket 119: know each file's revision before building cells
  return first;
}

// Fetch the remaining pages of the current folder (the loupe navigates the
// whole list). Single flight: later callers get the same promise.
export function loadRest(route, onPage = () => {}) {
  if (state.rest && state.rest.route === route) return state.rest.promise;
  const promise = (async () => {
    while (state.loaded < state.total && state.route === route) {
      // Photos that left the view no longer match on the server either, so its list is shorter by
      // that many: the offset is what we hold. The fetch runs in the edit queue (after pending
      // edits) and a page requested before a removal happened is discarded and asked again.
      const epoch = state.epoch;
      const page = await enqueue(() => get(
          `/api/photos?dir=${encodeURIComponent(route.dir)}&sort=${route.sort}` +
          `&filter=${route.filter}&offset=${state.loaded - state.removed}&limit=${PAGE}` +
          (route.recursive ? '&recursive=1' : '')));
      if (state.route !== route) break;
      if (epoch !== state.epoch) continue;
      if (!page.photos.length) break;
      const known = new Set(state.photos.map((p) => p.id));
      const fresh = page.photos.filter((p) => !known.has(p.id));
      state.loaded += page.photos.length;
      state.photos.push(...fresh);
      seedRevisions(fresh);
      onPage(fresh);
      if (state.onPhotosChanged) state.onPhotosChanged();
    }
  })();
  state.rest = {route, promise};
  return promise;
}

export function badges(photo) {
  return [
    photo.rating === REJECT ? el('span', {class: 'rej', text: '✖'})
      : display(photo.rating) > 0 ? el('span', {class: 'stars', text: '★' + photo.rating}) : null,
    photo.fav ? el('span', {class: 'fav', text: '♥'}) : null,
    photo.conflict ? el('span', {class: 'conflict', title: 'sidecars disagree', text: '⚠'}) : null,
    photo.files > 1 ? el('span', {class: 'stack', title: photo.files + ' files', text: '⧉ ' + photo.files}) : null,
  ];
}

export function updateCell(photo) {
  const cell = document.querySelector(`.cell[data-id="${photo.id}"]`);
  if (!cell) return;
  cell.classList.toggle('rejected', photo.rating === REJECT);
  setChildren(cell.querySelector('.badges'), badges(photo));
}

// Ticket 110/119: the file_id (hence the /img/Thumb/{file_id} URL) does not change when a RAW's
// tuning settings are saved, only the bytes that URL serves. imgUrl() carries the file's revision
// (ticket 119), so after a save this produces a fresh URL; a plain node whose src never changes
// would otherwise keep showing the pre-edit thumbnail even across a full page reload while the
// browser's HTTP cache (Cache-Control: max-age=3600 on /img/*) still holds the old response.
export function refreshCellThumb(photoId, fileId) {
  const img = document.querySelector(`.cell[data-id="${photoId}"] img`);
  if (img) img.src = imgUrl('Thumb', fileId);
}

export function makeCell(photo) {
  const img = el('img', {src: imgUrl('Thumb', photo.file_id), loading: 'lazy', alt: photo.name, decoding: 'async'});
  retryImage(img);
  const check = el('button', {class: 'check', 'aria-label': 'select', text: '✓', onclick: (e) => {
    e.preventDefault(); e.stopPropagation();
    if (e.shiftKey) selectRange(photo.id); else toggle(photo.id);
  }});
  return el('a', {
    class: 'cell' + (photo.rating === REJECT ? ' rejected' : '') + (state.selected.has(photo.id) ? ' selected' : ''),
    href: href({...state.route, photo: photo.id}), dataset: {id: photo.id},
    // recursive (ticket 086): several subfolders can share a filename, so disambiguate on hover.
    title: state.route && state.route.recursive ? photo.path : photo.name,
    onclick: (e) => {
      if (e.shiftKey) { e.preventDefault(); selectRange(photo.id); }
      else if (state.selecting || e.ctrlKey || e.metaKey) { e.preventDefault(); toggle(photo.id); }
    },
  }, img, check, el('div', {class: 'badges'}, badges(photo)));
}

// ticket 095: the anchor for Shift+Click range selection -- the last plain/Ctrl-click id, held
// fixed across a run of Shift+Clicks (so Shift+Click, Shift+Click further down extends from the
// same original anchor, not from wherever the previous Shift+Click landed -- the usual
// Explorer/Finder convention).
let selectAnchor = null;

function toggle(id) {
  if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  selectAnchor = id;
  const cell = document.querySelector(`.cell[data-id="${id}"]`);
  if (cell) cell.classList.toggle('selected', state.selected.has(id));
  renderSelectionBar();
}

// Shift+Click: select every photo between the anchor and id, in the grid's current display
// order (state.photos, which already follows the active sort/filter) -- replaces the selection
// rather than adding to it. No anchor yet (nothing selected before) falls back to a plain toggle,
// which then becomes the anchor for the next Shift+Click.
function selectRange(id) {
  if (selectAnchor == null) { toggle(id); return; }
  const ids = state.photos.map((p) => p.id);
  const a = ids.indexOf(selectAnchor);
  const b = ids.indexOf(id);
  if (a === -1 || b === -1) { toggle(id); return; }
  const [lo, hi] = a <= b ? [a, b] : [b, a];
  const range = new Set(ids.slice(lo, hi + 1));
  state.selected = range;
  document.querySelectorAll('.cell').forEach((cell) => {
    cell.classList.toggle('selected', range.has(Number(cell.dataset.id)));
  });
  renderSelectionBar();
}

function selectionBar() {
  let bar = document.querySelector('.selection-bar');
  if (!bar) {
    bar = el('div', {class: 'selection-bar'});
    document.getElementById('app').append(bar);
  }
  return bar;
}

export function renderSelectionBar() {
  const n = state.selected.size;
  let bar = document.querySelector('.selection-bar');
  if (!n) { if (bar) bar.remove(); return; }
  bar = selectionBar();
  const ids = [...state.selected];
  const rate = (rating) => batchRate(ids, rating);
  setChildren(bar,
    el('strong', {text: n + ' selected'}),
    el('button', {text: '✖', title: 'reject', onclick: () => rate(REJECT)}),
    ...choices().map((r) => el('button', {text: r === 0 ? '☆' : String(r), title: 'rate ' + r, onclick: () => rate(r)})),
    n === 2 ? el('button', {text: 'Link 2nd as tuning of 1st', onclick: () => linkSelected(ids)}) : null,
    // Trashing is rating-gated server-side (trash.py's trash_photo: only a rejected Photo can be
    // moved to .trash, an intentional safety rail from ticket 072/081) -- only offer it here when
    // every selected Photo would actually be eligible, i.e. the Rejected filter is active, same
    // as the header's own "Delete" link (ticket 072) already only appears there.
    state.route && state.route.filter === 'rejected'
      ? el('button', {class: 'danger', text: 'Delete', title: 'move selected photos to trash',
                      onclick: () => deleteSelected(ids)}) : null,
    el('button', {text: 'Clear', onclick: clearSelection}),
  );
}

// Ticket 088: reuses pages.js's deleteReviewPage (ticket 072) rather than a second confirm-UI --
// stash the ids and the route to return to (not the URL: an arbitrary-length id list doesn't
// belong in a query string, and this hand-off is inherently one-shot/session-only, same as
// selection itself already is).
function deleteSelected(ids) {
  state.deleteReview = {ids, from: {...state.route}};
  location.hash = hrefPage('delete-review', state.route.dir);
}

export function clearSelection() {
  state.selected.clear();
  selectAnchor = null;
  document.querySelectorAll('.cell.selected').forEach((c) => c.classList.remove('selected'));
  renderSelectionBar();
}

// ---- photos leaving the view when their rating stops matching the filter (ticket 056) ----

function statusText() {
  const shown = state.photos.length;
  const total = state.total - state.removed;
  if (!total) {
    return 'No photos in this folder' + (state.route && state.route.filter !== 'all' ? ' with this filter' : '');
  }
  return `${shown} of ${total} photos`;
}

export function updateStatus() {
  const status = document.querySelector('.status');
  if (status && document.getElementById('grid')) status.textContent = statusText();
}

// Take photos out of the list and the grid, one after the other. Returns the entries in removal
// order, [{photo, index}], where index is the position at the moment of that removal, so that
// putting them back in reverse order restores the list exactly (see reinsertPhotos).
export function removePhotos(photos) {
  const entries = [];
  for (const photo of photos) {
    const index = state.photos.indexOf(photo);
    if (index < 0) continue;
    state.photos.splice(index, 1);
    entries.push({photo, index});
  }
  state.removed += entries.length;
  state.epoch += 1;
  for (const {photo} of entries) {
    state.selected.delete(photo.id);
    const cell = document.querySelector(`.cell[data-id="${photo.id}"]`);
    if (cell) { cell.classList.add('leaving'); setTimeout(() => cell.remove(), 180); }
  }
  updateStatus();
  renderSelectionBar();
  if (state.onPhotosChanged) state.onPhotosChanged();
  return entries;
}

// Undo of removePhotos: entries (in the order they were removed) are put back in reverse order,
// each at the index it had when it was removed.
export function reinsertPhotos(entries) {
  const gridEl = document.getElementById('grid');
  for (const {photo, index} of [...entries].reverse()) {
    if (state.photos.includes(photo)) continue;
    const at = Math.min(index, state.photos.length);
    state.photos.splice(at, 0, photo);
    state.removed -= 1;
    state.epoch += 1;
    if (!gridEl) continue;
    document.querySelectorAll(`.cell[data-id="${photo.id}"]`).forEach((c) => c.remove());
    const next = state.photos[at + 1];
    const before = next && gridEl.querySelector(`.cell[data-id="${next.id}"]:not(.leaving)`);
    gridEl.insertBefore(makeCell(photo), before || null);
  }
  updateStatus();
  if (state.onPhotosChanged) state.onPhotosChanged();
}

// If the photo no longer matches the active filter, take it out of the view.
// Returns the entries that were removed (empty when nothing changed).
export function settle(photo) {
  const filter = state.route && state.route.filter;
  if (!filter || filter === 'all' || !state.photos.includes(photo) || matches(photo, filter)) return [];
  return removePhotos([photo]);
}

function batchRate(ids, rating) {
  return enqueue(async () => {
    try {
      const r = await post('/api/photos/rating', {ids, rating});
      const left = [];
      for (const res of r.results) {
        const photo = state.photos.find((p) => p.id === res.photo_id);
        if (photo) {
          Object.assign(photo, res.photo);
          updateCell(photo);
          left.push(...settle(photo));       // a photo that no longer matches the filter leaves
        }
      }
      clearSelection();
      if (r.results.some((x) => x.dry_run)) { toast('dry run: nothing was saved'); return; }
      if (r.results.length) state.undoStack.push({batch_id: r.batch_id, left});
      scheduleCountsRefresh();
      toast(`rated ${r.results.length}` + (r.errors.length ? `, ${r.errors.length} failed` : '') + ' (press U to undo)', r.errors.length > 0);
    } catch (e) { toast(e.message, true); }
  });
}

async function linkSelected(ids) {
  const [first, second] = ids.map((id) => state.photos.find((p) => p.id === id));
  try {
    const detail = await get('/api/photos/' + first.id);
    await post(`/api/files/${second.file_id}/link`, {target_file_id: detail.original_file_id, role: 'tuning'});
    toast('linked; reloading');
    state.dirty = true;
    location.reload();
  } catch (e) { toast(e.message, true); }
}

export function renderFolder(main) {
  const route = state.route;
  const dirs = state.dirs;
  const children = el('div', {class: 'folders'}, dirs.dirs.map((d) => el('a', {
    href: href({...route, dir: route.dir === '.' ? d.name : route.dir + '/' + d.name, photo: null}),
  }, '📁 ' + d.name, el('span', {class: 'count', text: d.photos}))));
  const status = el('div', {class: 'status'});
  const grid = el('div', {class: 'grid' + (state.selecting ? ' selecting' : ''), id: 'grid'});
  const setStatus = updateStatus;
  grid.append(...state.photos.map(makeCell));
  setChildren(main, dirs.dirs.length ? children : null, status, grid);
  setStatus();
  loadRest(route, (more) => { grid.append(...more.map(makeCell)); setStatus(); });
  renderSelectionBar();
}
