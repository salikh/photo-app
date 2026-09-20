// Folder view: breadcrumb-independent folder chips, the photo grid, selection.

import {get, post, imgUrl} from './api.js';
import {el, toast, retryImage, enqueue, setChildren} from './util.js';
import {href} from './route.js';
import {label, REJECT, display, choices} from './rating.js';
import {state} from './state.js';

const PAGE = 1000;

export async function loadFolder(route) {
  state.route = route;
  state.selected.clear();
  const [dirs, first] = await Promise.all([
    get('/api/dirs?path=' + encodeURIComponent(route.dir)),
    get(`/api/photos?dir=${encodeURIComponent(route.dir)}&sort=${route.sort}&filter=${route.filter}&limit=${PAGE}`),
  ]);
  state.dirs = dirs;
  state.photos = first.photos;
  state.total = first.total;
  return first;
}

// Fetch the remaining pages of the current folder (the loupe navigates the
// whole list). Single flight: later callers get the same promise.
export function loadRest(route, onPage = () => {}) {
  if (state.rest && state.rest.route === route) return state.rest.promise;
  const promise = (async () => {
    while (state.photos.length < state.total && state.route === route) {
      const page = await get(`/api/photos?dir=${encodeURIComponent(route.dir)}&sort=${route.sort}` +
                             `&filter=${route.filter}&offset=${state.photos.length}&limit=${PAGE}`);
      if (state.route !== route || !page.photos.length) break;
      state.photos.push(...page.photos);
      onPage(page.photos);
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

function makeCell(photo) {
  const img = el('img', {src: imgUrl('Thumb', photo.file_id), loading: 'lazy', alt: photo.name, decoding: 'async'});
  retryImage(img);
  const check = el('button', {class: 'check', 'aria-label': 'select', text: '✓', onclick: (e) => {
    e.preventDefault(); e.stopPropagation(); toggle(photo.id);
  }});
  return el('a', {
    class: 'cell' + (photo.rating === REJECT ? ' rejected' : '') + (state.selected.has(photo.id) ? ' selected' : ''),
    href: href({...state.route, photo: photo.id}), dataset: {id: photo.id}, title: photo.name,
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
    ...choices().map((r) => el('button', {text: r === 0 ? '☆' : String(r), title: 'rate ' + r, onclick: () => rate(r)})),
    el('button', {text: '✖', title: 'reject', onclick: () => rate(REJECT)}),
    n === 2 ? el('button', {text: 'Link 2nd as tuning of 1st', onclick: () => linkSelected(ids)}) : null,
    el('button', {text: 'Clear', onclick: clearSelection}),
  );
}

export function clearSelection() {
  state.selected.clear();
  document.querySelectorAll('.cell.selected').forEach((c) => c.classList.remove('selected'));
  renderSelectionBar();
}

function batchRate(ids, rating) {
  return enqueue(async () => {
    try {
      const r = await post('/api/photos/rating', {ids, rating});
      for (const res of r.results) {
        const photo = state.photos.find((p) => p.id === res.photo_id);
        if (photo) { Object.assign(photo, res.photo); updateCell(photo); }
      }
      if (r.results.some((x) => x.dry_run)) { toast('dry run: nothing was saved'); return; }
      if (r.results.length) state.undoStack.push({batch_id: r.batch_id});
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
  const setStatus = () => {
    status.textContent = state.total
      ? `${state.photos.length} of ${state.total} photos` : 'No photos in this folder' +
        (route.filter !== 'all' ? ' with this filter' : '');
  };
  grid.append(...state.photos.map(makeCell));
  setStatus();
  setChildren(main, dirs.dirs.length ? children : null, status, grid);
  loadRest(route, (more) => { grid.append(...more.map(makeCell)); setStatus(); });
  renderSelectionBar();
}
