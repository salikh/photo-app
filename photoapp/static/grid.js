// Folder view: breadcrumb-independent folder chips, the photo grid, selection.

import {get, post, imgUrl} from './api.js';
import {el, toast, retryImage, enqueue, setChildren} from './util.js';
import {href} from './route.js';
import {label, REJECT, display, choices} from './rating.js';
import {state} from './state.js';
import {matches, scheduleCountsRefresh} from './filters.js';

const PAGE = window.__pageSize || 1000;   // (the override is a test hook)

export async function loadFolder(route) {
  state.route = route;
  state.selected.clear();
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

export function makeCell(photo) {
  const img = el('img', {src: imgUrl('Thumb', photo.file_id), loading: 'lazy', alt: photo.name, decoding: 'async'});
  retryImage(img);
  const check = el('button', {class: 'check', 'aria-label': 'select', text: '✓', onclick: (e) => {
    e.preventDefault(); e.stopPropagation(); toggle(photo.id);
  }});
  return el('a', {
    class: 'cell' + (photo.rating === REJECT ? ' rejected' : '') + (state.selected.has(photo.id) ? ' selected' : ''),
    href: href({...state.route, photo: photo.id}), dataset: {id: photo.id},
    // recursive (ticket 086): several subfolders can share a filename, so disambiguate on hover.
    title: state.route && state.route.recursive ? photo.path : photo.name,
    onclick: (e) => {
      if (state.selecting || e.shiftKey || e.ctrlKey || e.metaKey) { e.preventDefault(); toggle(photo.id); }
    },
  }, img, check, el('div', {class: 'badges'}, badges(photo)));
}

function toggle(id) {
  if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  const cell = document.querySelector(`.cell[data-id="${id}"]`);
  if (cell) cell.classList.toggle('selected', state.selected.has(id));
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
    el('button', {text: 'Clear', onclick: clearSelection}),
  );
}

export function clearSelection() {
  state.selected.clear();
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
