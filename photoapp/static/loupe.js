// Full-window viewer: keyboard culling, swipe navigation and rating, filmstrip,
// preloading, tunings panel. The list of photos is state.photos.

import {get, post, imgUrl} from './api.js';
import {el, toast, isTyping, enqueue, retryImage, setChildren} from './util.js';
import {href} from './route.js';
import {afterKey, step, label, REJECT, display, choices} from './rating.js';
import {state} from './state.js';
import {attachSwipe} from './gestures.js';
import {label as filterLabel} from './filters.js';
import {updateCell} from './grid.js';

const PRELOAD_NEXT = 3;
const PRELOAD_PREV = 1;
const FILMSTRIP_RADIUS = 6;

let root = null;
const ui = {};
let index = -1;
let zoomed = false;
let filesOpen = false;

const current = () => state.photos[index];

function build() {
  if (root) return;
  ui.img = el('img', {class: 'main', alt: '', draggable: 'false'});
  ui.preview = el('div', {class: 'rate-preview'});
  ui.stage = el('div', {class: 'stage', onclick: () => toggleZoom()},
    ui.img, ui.preview,
    el('button', {class: 'nav-hint prev', 'aria-label': 'previous', text: '‹', onclick: (e) => { e.stopPropagation(); go(-1); }}),
    el('button', {class: 'nav-hint next', 'aria-label': 'next', text: '›', onclick: (e) => { e.stopPropagation(); go(1); }}));
  ui.filmstrip = el('div', {class: 'filmstrip'});
  ui.hud = el('div', {class: 'hud'});
  ui.panel = null;
  root = el('div', {class: 'loupe', hidden: true, role: 'dialog', 'aria-label': 'photo viewer'},
            ui.stage, ui.filmstrip, ui.hud);
  document.body.append(root);

  attachSwipe(ui.stage, {
    enabled: () => !zoomed,
    onDrag(dx, dy, axis) {
      ui.stage.classList.add('dragging');
      ui.img.style.transform = axis === 'x' ? `translateX(${dx}px)` : `translateY(${dy * 0.4}px)`;
      if (axis === 'y' && Math.abs(dy) >= 50) showPreview(step(current().rating, dy < 0 ? 1 : -1, current().previous_stars));
      else hidePreview();
    },
    onSwipe(direction) {
      resetDrag();
      if (direction === 'left') go(1);
      else if (direction === 'right') go(-1);
      else if (direction === 'up') rateStep(1);
      else if (direction === 'down') rateStep(-1);
    },
    onCancel: resetDrag,
  });
}

function resetDrag() {
  ui.stage.classList.remove('dragging');
  ui.img.style.transform = '';
  hidePreview();
}

let previewTimer = null;
function showPreview(rating) {
  ui.preview.textContent = label(rating);
  ui.preview.classList.toggle('reject', rating === REJECT);
  ui.preview.classList.add('show');
  clearTimeout(previewTimer);
}
function hidePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => ui.preview.classList.remove('show'), 350);
}

// ---------------------------------------------------------------- showing

export function isOpen() { return !!root && !root.hidden; }

export function open(photoId) {
  build();
  const i = state.photos.findIndex((p) => p.id === photoId);
  if (i < 0) { toast('photo not in this folder view', true); return false; }
  root.hidden = false;
  document.body.style.overflow = 'hidden';
  document.addEventListener('keydown', onKey);
  show(i);
  return true;
}

export function showById(photoId) {
  const i = state.photos.findIndex((p) => p.id === photoId);
  if (i >= 0 && i !== index) show(i);
}

export function close() {
  if (!root || root.hidden) return;
  root.hidden = true;
  document.body.style.overflow = '';
  document.removeEventListener('keydown', onKey);
  closeFiles();
  zoomed = false;
  ui.stage.classList.remove('zoomed');
}

function show(i) {
  index = i;
  const photo = current();
  zoomed = false;
  ui.stage.classList.remove('zoomed');
  resetDrag();
  ui.img.src = imgUrl('Medium', photo.file_id);
  ui.img.alt = photo.name;
  retryOnce(ui.img, photo);
  renderHud();
  renderFilmstrip();
  preload();
  if (state.route) {
    state.route.photo = photo.id;
    history.replaceState(null, '', href(state.route));
  }
  if (filesOpen) openFiles();
}

// A RAW without a preview is rendered in the background; retry a few times.
function retryOnce(img, photo) {
  let n = 0;
  img.onerror = () => {
    if (current() !== photo || n >= 6) return;
    n++;
    setTimeout(() => { if (current() === photo) img.src = imgUrl('Medium', photo.file_id) + '?r=' + n; }, 1500 * n);
  };
}

function preload() {
  const wanted = [];
  for (let d = 1; d <= PRELOAD_NEXT; d++) wanted.push(index + d);
  for (let d = 1; d <= PRELOAD_PREV; d++) wanted.push(index - d);
  for (const i of wanted) {
    const p = state.photos[i];
    if (p) new Image().src = imgUrl('Medium', p.file_id);
  }
}

function renderFilmstrip() {
  const from = Math.max(0, index - FILMSTRIP_RADIUS);
  const to = Math.min(state.photos.length, index + FILMSTRIP_RADIUS + 1);
  ui.filmstrip.replaceChildren(...state.photos.slice(from, to).map((p, k) => {
    const img = el('img', {src: imgUrl('Thumb', p.file_id), alt: p.name,
                           class: from + k === index ? 'current' : '', onclick: () => show(from + k)});
    retryImage(img);
    return img;
  }));
}

export function go(delta) {
  const next = index + delta;
  if (next < 0 || next >= state.photos.length) {
    toast(delta > 0 ? 'last picture' : 'first picture');
    return;
  }
  show(next);
}

function toggleZoom(force) {
  zoomed = force === undefined ? !zoomed : force;
  ui.stage.classList.toggle('zoomed', zoomed);
  const photo = current();
  ui.img.src = imgUrl(zoomed ? 'Huge' : 'Medium', photo.file_id);
  ui.img.onerror = zoomed ? () => { ui.img.src = imgUrl('Medium', photo.file_id); toast('full size not available'); } : null;
  if (zoomed) { ui.stage.scrollLeft = 0; ui.stage.scrollTop = 0; }
}

// ------------------------------------------------------------------- HUD

function renderHud() {
  const p = current();
  const rate = (r) => el('button', {
    text: r === 0 ? '☆' : String(r), title: 'rate ' + r, class: display(p.rating) === r ? 'on' : '',
    onclick: (e) => { e.stopPropagation(); setRating(r); },
  });
  ui.tagInput = el('input', {class: 'tags', placeholder: 'tag, -remove', hidden: true, onkeydown: onTagKey});
  setChildren(ui.hud,
    el('div', {class: 'name', title: p.path, text: p.name + (p.files > 1 ? `  (+${p.files - 1} files)` : '')}),
    el('span', {class: 'pos', text: `${index + 1}/${state.photos.length}`}),
    el('span', {class: 'stars' + (p.rating === REJECT ? ' reject' : ''), text: label(p.rating)}),
    p.fav ? el('span', {class: 'fav-on', text: '♥'}) : null,
    p.conflict ? el('span', {class: 'conflict', title: 'sidecars disagree', text: '⚠ conflict'}) : null,
    p.tags.map((t) => el('span', {class: 'tag', text: t})),
    state.route && state.route.filter !== 'all'
      ? el('span', {class: 'tag filter-tag', title: 'active filter', text: 'filter: ' + filterLabel(state.route.filter)}) : null,
    ui.tagInput,
    el('div', {class: 'buttons'},
      el('button', {text: '✖', title: 'reject (X)', class: p.rating === REJECT ? 'on' : '', onclick: () => setRating(afterKey(p.rating, 'x', p.previous_stars))}),
      choices().map(rate),
      el('button', {text: '♥', title: 'fav (F)', class: p.fav ? 'on' : '', onclick: toggleFav}),
      el('button', {text: 'tag', title: 'tags (T)', onclick: openTagInput}),
      el('button', {text: '↶', title: 'undo (U)', onclick: undo}),
      el('button', {text: 'files', title: 'files / tunings (I)', class: filesOpen ? 'on' : '', onclick: () => filesOpen ? closeFiles() : openFiles()}),
      el('button', {text: '✕', title: 'close (Esc)', onclick: closeToGrid})));
}

export function closeToGrid() {
  if (state.dirty) { location.reload(); return; }
  location.hash = href({...state.route, photo: null});
}

function refresh(photo) {
  if (current() === photo) renderHud();
  updateCell(photo);
}

// ------------------------------------------------------------------ edits

function setRating(value) {
  const photo = current();
  if (value == null || value === photo.rating) return;
  const before = {rating: photo.rating, previous_stars: photo.previous_stars};
  if (value === REJECT && photo.rating > 0) photo.previous_stars = photo.rating;
  photo.rating = value;
  refresh(photo);
  showPreview(value); hidePreview();
  return enqueue(async () => {
    try {
      const r = await post(`/api/photos/${photo.id}/rating`, {rating: value});
      Object.assign(photo, r.photo);
      if (r.dry_run) toast('dry run: nothing was saved');
      if (r.activity_ids.length) state.undoStack.push({ids: r.activity_ids});
    } catch (e) {
      Object.assign(photo, before);
      toast(e.message, true);
    }
    refresh(photo);
  });
}

function rateStep(delta) {
  const p = current();
  setRating(step(p.rating, delta, p.previous_stars));
}

function toggleFav() {
  const photo = current();
  const value = !photo.fav;
  photo.fav = value;
  refresh(photo);
  return enqueue(async () => {
    try {
      const r = await post(`/api/photos/${photo.id}/fav`, {fav: value});
      Object.assign(photo, r.photo);
      if (r.dry_run) toast('dry run: nothing was saved');
      if (r.activity_ids.length) state.undoStack.push({ids: r.activity_ids});
    } catch (e) { photo.fav = !value; toast(e.message, true); }
    refresh(photo);
  });
}

function openTagInput() {
  ui.tagInput.hidden = false;
  ui.tagInput.focus();
}

function onTagKey(e) {
  e.stopPropagation();
  if (e.key === 'Escape') { ui.tagInput.hidden = true; ui.tagInput.blur(); return; }
  if (e.key !== 'Enter') return;
  const tokens = ui.tagInput.value.split(',').map((t) => t.trim()).filter(Boolean);
  const add = tokens.filter((t) => !t.startsWith('-'));
  const remove = tokens.filter((t) => t.startsWith('-')).map((t) => t.slice(1).trim());
  ui.tagInput.value = '';
  ui.tagInput.hidden = true;
  if (!add.length && !remove.length) return;
  const photo = current();
  enqueue(async () => {
    try {
      const r = await post(`/api/photos/${photo.id}/tags`, {add, remove});
      Object.assign(photo, r.photo);
      if (r.dry_run) toast('dry run: nothing was saved');
      if (r.activity_ids.length) state.undoStack.push({ids: r.activity_ids});
    } catch (err) { toast(err.message, true); }
    refresh(photo);
  });
}

export function undo() {
  // Runs in the edit queue, so an undo pressed right after an edit waits for
  // that edit's response (which is what puts it on the undo stack).
  return enqueue(async () => {
    const item = state.undoStack.pop();
    if (!item) { toast('nothing to undo'); return; }
    try {
      const results = [];
      if (item.batch_id) {
        const r = await post(`/api/activity/batch/${item.batch_id}/undo`);
        results.push(...r.photos.map((p) => ({photo: p})));
        if (r.errors.length) toast(`${r.errors.length} could not be undone (changed since)`, true);
      } else {
        for (const id of [...item.ids].reverse()) results.push(await post(`/api/activity/${id}/undo`));
      }
      for (const r of results) {
        const photo = state.photos.find((p) => p.id === r.photo.id);
        if (photo) { Object.assign(photo, r.photo); refresh(photo); }
      }
      if (!item.batch_id) toast('undone');
    } catch (e) { toast(e.message, true); }
  });
}

// ------------------------------------------------ files / tunings panel

async function openFiles() {
  filesOpen = true;
  const photo = current();
  let detail;
  try { detail = await get('/api/photos/' + photo.id); } catch (e) { toast(e.message, true); return; }
  if (!filesOpen || current() !== photo) return;
  closePanelOnly();
  ui.panel = el('div', {class: 'files-panel', onclick: (e) => e.stopPropagation(), onpointerdown: (e) => e.stopPropagation()},
    el('h3', {text: `Files (${detail.files.length})`}),
    el('div', {class: 'meta', text: `rating ${label(detail.rating)}` + (detail.conflict ? ' — sidecars disagree' : '')}),
    detail.sidecars.map((s) => el('div', {class: 'meta', text: `${s.path}: ${s.rating ?? 'no rating'}${s.fav ? ' ♥' : ''}`})),
    el('p'),
    detail.files.map((f) => el('div', {class: 'file' + (f.id === detail.representative_file_id ? ' rep' : '')},
      el('div', {text: f.path.split('/').pop() + '  ·  ' + f.role + (f.link_source === 'manual' ? ' (manual)' : '')}),
      el('div', {class: 'meta', text: `${f.width || '?'}×${f.height || '?'}  ${f.path}` + (f.missing ? '  MISSING' : '')}),
      el('div', {class: 'row'},
        f.id === detail.representative_file_id ? el('span', {class: 'ok', text: 'shown'}) :
          el('button', {text: 'Show this', onclick: () => setRepresentative(detail, f.id)}),
        f.id !== detail.original_file_id ? el('button', {text: 'Unlink', onclick: () => unlink(f)}) : null))),
    el('div', {class: 'row'},
      el('button', {text: 'Use default', onclick: () => setRepresentative(detail, null)}),
      el('button', {text: 'Close', onclick: closeFiles})));
  ui.stage.append(ui.panel);
  renderHud();
}

function closePanelOnly() {
  if (ui.panel) { ui.panel.remove(); ui.panel = null; }
}

function closeFiles() {
  filesOpen = false;
  closePanelOnly();
  if (root && !root.hidden) renderHud();
}

async function setRepresentative(detail, fileId) {
  const photo = current();
  try {
    const d = await post(`/api/photos/${photo.id}/representative`, {file_id: fileId});
    photo.file_id = d.representative_file_id;
    const rep = d.files.find((f) => f.id === d.representative_file_id);
    if (rep) { photo.path = rep.path; photo.name = rep.path.split('/').pop(); }
    show(index);
    const cell = document.querySelector(`.cell[data-id="${photo.id}"] img`);
    if (cell) cell.src = imgUrl('Thumb', photo.file_id);
  } catch (e) { toast(e.message, true); }
}

async function cycleRepresentative() {
  const photo = current();
  try {
    const d = await get('/api/photos/' + photo.id);
    const live = d.files.filter((f) => !f.missing);
    if (live.length < 2) { toast('only one file'); return; }
    const at = live.findIndex((f) => f.id === d.representative_file_id);
    await setRepresentative(d, live[(at + 1) % live.length].id);
  } catch (e) { toast(e.message, true); }
}

async function unlink(file) {
  try {
    await post(`/api/files/${file.id}/unlink`);
    toast('unlinked; the list will refresh when you close the viewer');
    state.dirty = true;
    openFiles();
  } catch (e) { toast(e.message, true); }
}

// -------------------------------------------------------------- keyboard

function onKey(e) {
  if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
  const key = e.key;
  if (key === 'ArrowRight' || key === ' ') go(1);
  else if (key === 'ArrowLeft' || key === 'Backspace') go(-1);
  else if (key === 'Home') show(0);
  else if (key === 'End') show(state.photos.length - 1);
  else if (/^[0-5]$/.test(key) || key === 'x' || key === 'X') { const p = current(); setRating(afterKey(p.rating, key, p.previous_stars)); }
  else if (key === 'f' || key === 'F') toggleFav();
  else if (key === 't' || key === 'T') openTagInput();
  else if (key === 'z' || key === 'Z') toggleZoom();
  else if (key === 'g' || key === 'G') cycleRepresentative();
  else if (key === 'u' || key === 'U') undo();
  else if (key === 'i' || key === 'I') filesOpen ? closeFiles() : openFiles();
  else if (key === 'Escape') { if (filesOpen) closeFiles(); else closeToGrid(); }
  else return;
  e.preventDefault();
}
