// Full-window viewer: keyboard culling, swipe navigation and rating, filmstrip,
// preloading, tunings panel. The list of photos is state.photos.

import {get, post, imgUrl, setRevision, seedRevisions} from './api.js';
import {el, toast, isTyping, enqueue, retryImage, setChildren, fmtBytes} from './util.js';
import {href} from './route.js';
import {afterKey, step, label, REJECT, display, choices} from './rating.js';
import {state} from './state.js';
import {attachSwipe} from './gestures.js';
import {label as filterLabel, primary as filterPrimary, starValues} from './filters.js';
import * as preloader from './preload.js';
import {createFilmstrip} from './filmstrip.js';
import {createZoom} from './zoom.js';
import {updateCell, refreshCellThumb, settle, reinsertPhotos, loadRest, loadFolder} from './grid.js';
import {matches, scheduleCountsRefresh} from './filters.js';
import {RawTuningSession} from './rawTuning.js';
import * as exportAction from './export.js';


let root = null;
const ui = {};
let index = -1;
let zoomed = false;
let filesOpen = false;
let filterPanelOpen = false;   // ticket 092: the filter-switcher panel opened from the HUD's filter tag
let brokenFileId = null;   // set when the main image fails to load after every retry (ticket 079)

const current = () => state.photos[index];

// Ticket 119: a forced re-fetch of an image whose URL may already carry imgUrl's ?r=revision.
function forceUrl(size, fileId, tag) {
  const url = imgUrl(size, fileId);
  return url + (url.includes('?') ? '&' : '?') + 'n=' + (tag ?? Date.now());
}

function build() {
  if (root) return;
  window.__preloadedUrls = preloader.urls;      // test hook: what is being held ahead of time
  ui.img = el('img', {class: 'main', alt: '', draggable: 'false',
                      onload: () => updateCropShade()});
  // Ticket 115: a dark shading layer over the cropped-out part of the frame, shown for a file
  // with a crop while viewing the full-frame Medium/Huge (the cropped Thumb/Small are rendered
  // server-side instead). Pointer-events none; purely visual.
  ui.cropShade = el('div', {class: 'crop-shade', hidden: true},
    el('div', {class: 'crop-rect'}));
  ui.tuning = el('img', {class: 'tuning', alt: '', draggable: 'false', hidden: true,
                        onload: syncTuningVisible});
  // Ticket 107: a second, independent overlay for hovering a sibling file's row in the Files
  // panel (shows that file's own thumbnail) -- deliberately a separate element from ui.tuning
  // rather than a shared one, so the two hover mechanics never fight over one element's src.
  // Appended after ui.tuning so it paints on top if both were somehow active at once.
  ui.rowPreview = el('img', {class: 'tuning row-preview', alt: '', draggable: 'false', hidden: true,
                             onload: () => { if (hoverFile) ui.rowPreview.hidden = false; }});
  // Ticket 108: shown for the span of a RawTuningSession.render() call (requestPreview), so the
  // user sees something is happening during a slow local LibRaw-Wasm decode (worst case: a cold
  // session's DNG fetch + WASM compile).
  ui.tuningBusy = el('div', {class: 'tuning-busy', hidden: true});
  ui.preview = el('div', {class: 'rate-preview'});
  ui.stage = el('div', {class: 'stage'},
    ui.img, ui.cropShade, ui.tuning, ui.rowPreview, ui.tuningBusy, ui.preview,
    el('button', {class: 'nav-hint prev', 'aria-label': 'previous', text: '‹', onclick: (e) => { e.stopPropagation(); go(-1); }}),
    el('button', {class: 'nav-hint next', 'aria-label': 'next', text: '›', onclick: (e) => { e.stopPropagation(); go(1); }}));
  ui.strip = createFilmstrip((id) => {
    const i = state.photos.findIndex((p) => p.id === id);
    if (i >= 0) show(i);
  });
  ui.filmstrip = ui.strip.element;
  ui.hud = el('div', {class: 'hud'});
  ui.panel = null;
  ui.filterPanel = null;
  root = el('div', {class: 'loupe', hidden: true, role: 'dialog', 'aria-label': 'photo viewer'},
            ui.stage, ui.filmstrip, ui.hud);
  document.body.append(root);

  // Pinch, drag, double tap, Ctrl+wheel: see zoom.js. The viewer supplies the photo's real size and
  // what to show while zoomed.
  ui.zoom = createZoom(ui.stage, ui.img, {fullSize, onEnter: enterZoom, onExit: exitZoom});
  window.addEventListener('resize', updateCropShade);

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
  state.onPhotosChanged = () => { if (isOpen() && index >= 0) renderFilmstrip(); };
  document.body.style.overflow = 'hidden';
  document.addEventListener('keydown', onKey);
  document.addEventListener('keyup', onKeyUp);
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
  state.onPhotosChanged = null;
  ui.strip.reset();
  preloader.clear();
  document.body.style.overflow = '';
  document.removeEventListener('keydown', onKey);
  document.removeEventListener('keyup', onKeyUp);
  closeFiles();
  closeFilterPicker();
  closeDeleteModal();
  exitCropMode();   // ticket 115: leaving the viewer drops an unsaved crop
  ui.zoom.reset();
  zoomed = false;
  ui.stage.classList.remove('zoomed');
}

function show(i) {
  const delta = index >= 0 && i !== index ? Math.sign(i - index) : 1;   // direction of travel
  index = i;
  const photo = current();
  closeDeleteModal();   // it would otherwise still show, referring to the previous photo's file
  exitCropMode(false);  // ticket 115: navigating away discards an unsaved crop
  ui.zoom.reset();
  zoomed = false;
  ui.stage.classList.remove('zoomed');
  resetDrag();
  brokenFileId = null;
  discardPending();   // ticket 094: never let a provisional render leak past navigating away
  ui.img.src = imgUrl('Medium', photo.file_id);
  ui.img.alt = photo.name;
  updateCropShade();
  retryOnce(ui.img, photo);
  renderHud();
  renderFilmstrip(true);                 // centered on the current photo
  preload(delta);
  if (state.route) {
    state.route.photo = photo.id;
    history.replaceState(null, '', href(state.route));
  }
  if (filesOpen) openFiles();
}

// A RAW without a preview is rendered in the background; retry a few times. If it is still
// broken after every retry, offer a way to fix it rather than failing silently (ticket 079).
function retryOnce(img, photo) {
  let n = 0;
  img.onerror = () => {
    if (current() !== photo) return;
    if (n >= 6) {
      brokenFileId = photo.file_id;
      renderHud();
      return;
    }
    n++;
    setTimeout(() => { if (current() === photo) img.src = forceUrl('Medium', photo.file_id, n); }, 1500 * n);
  };
}

// Debug action (ticket 079): every render path in this app deliberately never overwrites an
// existing cached thumbnail, so a broken one otherwise never gets a fresh render. Clears the
// cache server-side and reloads what is on screen; the next request regenerates it normally.
async function rerenderThumbs() {
  const photo = current();
  try {
    await post(`/api/files/${photo.file_id}/rerender_thumbs`);
    brokenFileId = null;
    ui.img.src = forceUrl('Medium', photo.file_id);
    retryOnce(ui.img, photo);
    toast('re-rendering thumbnails…');
  } catch (e) { /* post() already showed a toast for a server error */ }
  renderHud();
}

function preload(delta) {
  const photo = current();
  preloader.update(state.photos, index, delta,
                   [imgUrl('Medium', photo.file_id), imgUrl('Huge', photo.file_id)]);
  preloader.decodeCurrentHuge(photo);
}

function renderFilmstrip(center = false) {
  ui.strip.render(state.photos, index, {center});
}

export function go(delta) {
  const next = index + delta;
  if (delta > 0 && next >= state.photos.length && state.loaded < state.total) {
    toast('loading more\u2026');                    // the rest of the folder is still coming in
    loadRest(state.route).then(() => { if (isOpen()) go(delta); });
    return;
  }
  if (next < 0 || next >= state.photos.length) {
    toast(delta > 0 ? 'last picture' : 'first picture');
    return;
  }
  show(next);
}

// The photo's size in original pixels, in the orientation it is shown: the long edge from the
// scan, the shape from the picture on screen (which the browser has already rotated).
function fullSize() {
  const photo = current();
  const shown = ui.img;
  if (!photo || !shown.naturalWidth) return null;
  const aspect = shown.naturalWidth / shown.naturalHeight;
  const long = Math.max(photo.width || 0, photo.height || 0) || Math.max(shown.naturalWidth, shown.naturalHeight);
  return aspect >= 1 ? {W: long, H: long / aspect} : {W: long * aspect, H: long};
}

function enterZoom() {
  zoomed = true;
  ui.stage.classList.add('zoomed');
  ui.tuning.hidden = true;   // ticket 094: the provisional overlay doesn't track the zoom transform
  ui.cropShade.hidden = true;   // ticket 115: nor does the crop shading
  const photo = current();
  const url = imgUrl('Huge', photo.file_id);
  const full = new Image();                          // swap in the full-size picture once it is there
  full.onload = () => {
    if (zoomed && current() === photo) {
      ui.img.src = url;
      ui.zoom.setFullSize(full.naturalWidth, full.naturalHeight);
    }
  };
  full.onerror = () => {
    if (current() !== photo) return;
    toast('full size not available');
    ui.zoom.exit();
  };
  full.src = url;
}

function exitZoom() {
  zoomed = false;
  ui.stage.classList.remove('zoomed');
  const photo = current();
  if (photo) ui.img.src = imgUrl('Medium', photo.file_id);
  syncTuningVisible();   // ticket 094/107: restore the overlay if hover/Shift still calls for it
  updateCropShade();
}

// Zoom needs the size of the picture on screen; if it has not loaded yet (a slow link), wait for it.
function whenShown(fn) {
  if (ui.img.complete && ui.img.naturalWidth) fn();
  else ui.img.addEventListener('load', fn, {once: true});
}

function toggleZoom() {
  if (ui.zoom.isActive()) ui.zoom.exit();
  else whenShown(() => ui.zoom.enter({scale: 1}));   // 100%, around the middle
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
    // recursive (ticket 086): the folder's grid can span several subfolders that share a
    // filename, so show the full relative path in the visible name, not just on hover.
    el('div', {class: 'name', title: p.path,
              text: (state.route && state.route.recursive ? p.path : p.name) +
                    (p.files > 1 ? `  (+${p.files - 1} files)` : '')}),
    el('span', {class: 'pos', text: `${index + 1}/${Math.max(state.photos.length, state.total - state.removed)}`}),
    el('span', {class: 'stars' + (p.rating === REJECT ? ' reject' : ''), text: label(p.rating)}),
    p.fav ? el('span', {class: 'fav-on', text: '♥'}) : null,
    p.conflict ? el('span', {class: 'conflict', title: 'sidecars disagree', text: '⚠ conflict'}) : null,
    p.tags.map((t) => el('span', {class: 'tag', text: t})),
    // Ticket 123: dot-directory names in the path read as implicit tags (a hidden folder's name).
    (p.implied || []).map((t) => el('span', {class: 'tag implied',
      title: 'from a hidden folder name', text: t})),
    // Deliberately not class "tag" -- .hud .tag is also used for the photo's own tags, and tests
    // (and a future feature) count them separately from this filter-switcher button.
    state.route
      ? el('button', {class: 'filter-tag' + (filterPanelOpen ? ' on' : ''), title: 'switch the active filter',
                      text: 'filter: ' + filterLabel(state.route.filter),
                      onclick: (e) => { e.stopPropagation(); toggleFilterPicker(); }}) : null,
    ui.tagInput,
    el('div', {class: 'buttons'},
      el('button', {text: '✖', title: 'reject (X)', class: p.rating === REJECT ? 'on' : '', onclick: () => setRating(afterKey(p.rating, 'x', p.previous_stars))}),
      choices().map(rate),
      el('button', {text: '♥', title: 'fav (F)', class: p.fav ? 'on' : '', onclick: toggleFav}),
      el('button', {text: 'tag', title: 'tags (T)', onclick: openTagInput}),
      el('button', {text: '↶', title: 'undo (U)', onclick: undo}),
      el('button', {text: 'crop', title: 'crop this photo (non-destructive)',
                    class: cropMode ? 'on' : '',
                    onclick: (e) => { e.stopPropagation(); toggleCropMode(); }}),
      el('button', {text: '⟲', title: 'rotate left 90° (R)',
                    onclick: (e) => { e.stopPropagation(); rotateLeft(); }}),
      el('button', {text: 'export', title: 'export this photo',
                    onclick: (e) => { e.stopPropagation(); exportAction.open([p.id]); }}),
      el('button', {text: 'files', title: 'files / tunings (I)', class: filesOpen ? 'on' : '', onclick: () => filesOpen ? closeFiles() : openFiles()}),
      brokenFileId === p.file_id
        ? el('button', {class: 'broken-thumb', title: 'this thumbnail failed to load -- click to re-render it',
                        text: '⚠ fix thumbnail', onclick: rerenderThumbs})
        : el('button', {class: 'debug-menu', title: 'debug: re-render this photo’s thumbnails',
                        text: '⋯', onclick: rerenderThumbs}),
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

// ------------------------------------------------ photos leaving the view (ticket 056)

// After an edit, take the photo out of the view if it no longer matches the active filter
// and, if it was the one on screen, move on to the next (or the previous, or close when none
// is left). Returns the removed entries [{photo, index}] for undo.
function leaveIfNoLongerMatching(photo) {
  const at = state.photos.indexOf(photo);
  const wasCurrent = current() === photo;
  const entries = settle(photo);
  if (!entries.length) return entries;
  if (!isOpen()) return entries;
  if (wasCurrent) {
    if (!state.photos.length) {
      if (state.loaded < state.total) {            // more photos are still loading
        loadRest(state.route).then(() => {
          if (!isOpen()) return;
          if (state.photos.length) show(0); else { toast('no more photos in this filter'); closeToGrid(); }
        });
        return entries;
      }
      toast('no more photos in this filter'); closeToGrid(); return entries;
    }
    show(Math.min(at, state.photos.length - 1));
  } else {
    if (at < index) index -= 1;                    // keep showing the same photo
    renderHud();
    renderFilmstrip();
  }
  return entries;
}

// Bring the view in line with the server's answer for this photo: remove it if it no longer
// matches, or put it back (and show it) if it was removed but does match after all (a failed
// write, or a dry run).
function reconcile(photo, left) {
  const filter = state.route && state.route.filter;
  const inList = state.photos.includes(photo);
  const should = !filter || filter === 'all' || matches(photo, filter);
  if (!should && inList) {
    left.push(...leaveIfNoLongerMatching(photo));
  } else if (should && !inList) {
    const entry = left.find((e) => e.photo === photo);
    if (entry) {
      left.splice(left.indexOf(entry), 1);
      reinsertPhotos([entry]);
      if (isOpen()) show(state.photos.indexOf(photo));
    }
  } else {
    refresh(photo);
  }
}

// Rapid edits to the same field of the same photo (holding a rating key, a fast double
// fav-toggle) go out as a strict FIFO of requests (util.enqueue), each carrying an absolute
// value. A response can arrive after a *newer* optimistic edit has already moved the photo
// further, and applying it then would silently undo that newer edit. beginEdit() marks the
// start of an edit and returns a check that is only true if no later edit has started since;
// the async continuation uses it to skip applying a stale response (the bookkeeping -- the
// activity log entry, the undo stack push -- still happens, since the write itself is real and
// undo-able; only overwriting the locally-displayed value is skipped).
const editSeq = new WeakMap();

function beginEdit(photo) {
  const seq = (editSeq.get(photo) || 0) + 1;
  editSeq.set(photo, seq);
  return () => editSeq.get(photo) === seq;
}

// ------------------------------------------------------------------ edits

function setRating(value) {
  const photo = current();
  if (value == null || value === photo.rating) return;
  const before = {rating: photo.rating, previous_stars: photo.previous_stars};
  if (value === REJECT && photo.rating > 0) photo.previous_stars = photo.rating;
  photo.rating = value;
  const isLatest = beginEdit(photo);
  const left = leaveIfNoLongerMatching(photo);      // optimistic: it leaves at once
  if (!left.length) refresh(photo);
  showPreview(value); hidePreview();
  return enqueue(async () => {
    try {
      const r = await post(`/api/photos/${photo.id}/rating`, {rating: value});
      if (isLatest()) { Object.assign(photo, r.photo); reconcile(photo, left); }
      if (r.dry_run) toast('dry run: nothing was saved');
      if (r.activity_ids.length) state.undoStack.push({ids: r.activity_ids, left});
    } catch (e) {
      if (isLatest()) { Object.assign(photo, before); reconcile(photo, left); }  // back where it was
      toast(e.message, true);
    }
    scheduleCountsRefresh();
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
  const isLatest = beginEdit(photo);
  const left = leaveIfNoLongerMatching(photo);
  if (!left.length) refresh(photo);
  return enqueue(async () => {
    try {
      const r = await post(`/api/photos/${photo.id}/fav`, {fav: value});
      if (isLatest()) { Object.assign(photo, r.photo); reconcile(photo, left); }
      if (r.dry_run) toast('dry run: nothing was saved');
      if (r.activity_ids.length) state.undoStack.push({ids: r.activity_ids, left});
    } catch (e) {
      if (isLatest()) { photo.fav = !value; reconcile(photo, left); }
      toast(e.message, true);
    }
    scheduleCountsRefresh();
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
      const left = [];
      reconcile(photo, left);
      if (r.activity_ids.length) state.undoStack.push({ids: r.activity_ids, left});
    } catch (err) { toast(err.message, true); refresh(photo); }
    scheduleCountsRefresh();
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
      const undoneIds = new Set();
      for (const r of results) {
        const photo = state.photos.find((p) => p.id === r.photo.id);
        if (photo) { Object.assign(photo, r.photo); refresh(photo); continue; }
        // it had left the view after that change: it is put back where it was
        const entry = (item.left || []).find((e) => e.photo.id === r.photo.id);
        if (entry) { Object.assign(entry.photo, r.photo); undoneIds.add(entry.photo.id); }
      }
      // keep the removal order, so the reverse puts everything back exactly
      const restored = (item.left || []).filter((e) => undoneIds.has(e.photo.id));
      if (restored.length) {
        reinsertPhotos(restored);
        for (const e of restored) updateCell(e.photo);
        if (isOpen()) show(state.photos.indexOf(restored[0].photo));   // show it again
      }
      if (!item.batch_id) toast('undone');
    } catch (e) { toast(e.message, true); }
    scheduleCountsRefresh();
  });
}

// ------------------------------------------------ files / tunings panel

function formatAperture(a) {
  if (a == null) return null;
  const r = Math.round(a * 10) / 10;
  return 'f/' + (Number.isInteger(r) ? r : r.toFixed(1));
}

function formatShutterSpeed(s) {
  if (s == null) return null;
  return s >= 1 ? (Math.round(s * 10) / 10) + 's' : '1/' + Math.round(1 / s) + 's';
}

// Ticket 170/172: the real focal length, with its 35mm full-frame equivalent in parentheses when
// the camera recorded one, e.g. "35mm (52mm)".
function formatFocalLength(mm, mm35) {
  const real = mm == null ? null : Math.round(mm) + 'mm';
  const equiv = mm35 == null ? null : Math.round(mm35) + 'mm';
  if (real && equiv) return real + ' (' + equiv + ')';
  return real || equiv;
}

// Corporate noise words dropped from a camera Make before it is shown (ticket 168), so a Make of
// "PENTAX Corporation"/"OLYMPUS IMAGING CORP." is just "PENTAX"/"OLYMPUS".
const MAKE_NOISE_WORDS = new Set(
  ['corporation', 'corp.', 'corp', 'inc.', 'inc', 'ltd.', 'ltd', 'co.', 'co', 'gmbh', 'company',
   'imaging']);

function canonicalMake(make) {
  if (!make) return make;
  const words = make.split(/\s+/).filter((w) => !MAKE_NOISE_WORDS.has(w.toLowerCase()));
  return words.length ? words.join(' ') : make;
}

// Camera make + model, without repeating the make when the model already starts with it (many
// cameras' Model is e.g. "PENTAX K-5", their Make "PENTAX"). Ticket 168: compare against the
// canonical make too, so "PENTAX Corporation" + "PENTAX *ist DL" shows only "PENTAX *ist DL" and
// "OLYMPUS IMAGING CORP." + "u830" shows "OLYMPUS u830".
function formatCamera(make, model) {
  make = make ? make.trim() : null; model = model ? model.trim() : null;
  if (!model) return make;
  const canonical = canonicalMake(make);
  const modelLower = model.toLowerCase();
  if (canonical && modelLower.startsWith(canonical.toLowerCase())) return model;
  if (make && modelLower.startsWith(make.toLowerCase())) return model;
  const shown = canonical || make;
  return shown ? shown + ' ' + model : model;
}

// Tickets 084/111/156: camera (make/model), lens, focal length, aperture/shutter speed/ISO and
// exif_date, one line, omitting whatever's absent.
function cameraMetaText(f) {
  const parts = [formatCamera(f.camera_make, f.camera_model), f.lens_model,
                  formatFocalLength(f.focal_length, f.focal_length_35mm),
                  formatAperture(f.aperture), formatShutterSpeed(f.shutter_speed),
                  f.iso != null ? 'ISO ' + Math.round(f.iso) : null, f.exif_date]
      .filter((p) => p != null);
  return parts.length ? parts.join('  ·  ') : null;
}

// ------------------------------------------- per-file RAW conversion settings (085, then 094) ---
//
// Ticket 085's original shape had every slider tick immediately POST and clear the real thumbnail
// cache -- as expensive and as permanent as a Save. Ticket 094 splits that in two: dragging a
// slider only updates `pending` (this file's not-yet-committed values) and asks the server for a
// cheap, never-cached provisional render (GET /api/files/{id}/raw_preview) to show in `ui.tuning`,
// an overlay on top of the committed image; nothing touches files.raw_* or the thumbs cache until
// Save actually calls set_raw_settings (still exactly what it always did).
//
// Ticket 107 replaces 094's "shown by default, hide while comparing" model with the reverse: the
// committed image (ui.img) is the default, and the tuned overlay only appears while the user is
// actively asking to see it -- hovering the sliders block, or holding Shift as a keyboard/touch
// fallback (see syncTuningVisible, onKey/onKeyUp). A third, independent hover state swaps in a
// sibling file's own thumbnail when hovering its row in the Files panel (see ui.rowPreview,
// showRowPreview/hideRowPreview).
//
// Because the provisional render never writes anywhere, there is nothing to clean up if the user
// navigates away without saving (discardPending() below just stops asking for more of them and
// hides the overlay -- see the calls from show()/closeFiles()).

const WB_MODES = ['camera', 'auto', 'manual'];
const DEFAULT_WB = {r: 2.0, g: 1.0, b: 1.5};
// Ticket 112: advanced RAW controls. Select values are strings; the noise/demosaic defaults
// (off/AHD) map back to NULL so picking them again keeps "at default" true.
const NOISE_MODES = [['0', 'off'], ['1', 'light'], ['2', 'full']];
const DEMOSAIC_MODES = [['1', 'VNG'], ['2', 'PPG'], ['3', 'AHD (default)'], ['4', 'DCB'],
                        ['11', 'DHT'], ['12', 'AAHD']];
const PREVIEW_DEBOUNCE_MS = 200;

let pending = null;      // {fileId, values} for whichever file's controls are currently open
let sliderHover = false; // mouse is over the raw-settings sliders block (ticket 107)
let shiftHeld = false;   // Shift is held as the compare modifier (ticket 107)
let hoverFile = null;    // a sibling file whose own thumbnail ui.rowPreview is showing (ticket 107)

function committedValues(f) {
  return {bright: f.raw_bright, wb_mode: f.raw_wb_mode, wb_r: f.raw_wb_r, wb_g: f.raw_wb_g,
          wb_b: f.raw_wb_b, highlight: f.raw_highlight,
          exposure: f.raw_exposure, shadow: f.raw_shadow,
          saturation: f.raw_saturation, contrast: f.raw_contrast, noise: f.raw_noise,
          demosaic: f.raw_demosaic};
}

function isDirty(values, committed) {
  return Object.keys(values).some((k) => values[k] !== committed[k]);
}

// Ticket 107: the tuned overlay shows only while the user is asking to compare (hovering the
// sliders, or holding the Shift fallback) -- never merely because a provisional render loaded.
function syncTuningVisible() {
  ui.tuning.hidden = !(pending && ui.tuning.src && (sliderHover || shiftHeld));
}

function showRowPreview(f) {
  hoverFile = f;
  ui.rowPreview.hidden = true;   // wait for load, same as ui.tuning, to avoid a broken-image flash
  ui.rowPreview.src = imgUrl('Medium', f.id);
}

function hideRowPreview() {
  hoverFile = null;
  ui.rowPreview.hidden = true;
  ui.rowPreview.src = '';
}

function discardPending() {
  if (pending) { clearTimeout(pending.timer); if (pending.session) pending.session.dispose(); }
  pending = null;
  sliderHover = false;
  previewSeq++;   // ticket 108: invalidate any requestPreview() still in flight
  if (ui.tuning) { ui.tuning.hidden = true; ui.tuning.src = ''; }
  if (ui.tuningBusy) ui.tuningBusy.hidden = true;
}

function schedulePreview(f) {
  clearTimeout(pending.timer);
  pending.timer = setTimeout(() => requestPreview(f), PREVIEW_DEBOUNCE_MS);
}

// Ticket 108: bumped at the start of every requestPreview() call and compared against on the way
// out, so a busy indicator hide from a stale/superseded call (a rapid slider drag, or the panel
// closing mid-render) can never clobber a newer call's own show/hide.
let previewSeq = 0;

// Ticket 105: try a local LibRaw-Wasm render first (one RawTuningSession per file, reused
// across every tick); requestPreviewNetwork -- 094's original GET .../raw_preview round trip --
// is the fallback when render() can't (LibRaw-Wasm unavailable, the preview DNG missing, or a
// decode error), not something removed. Ticket 108: ui.tuningBusy shows for exactly the span of
// the render() call -- "LibRaw-Wasm is working" -- not the network fallback's own image load.
async function requestPreview(f) {
  if (!pending || pending.fileId !== f.id) return;   // superseded by a discard/navigation
  if (!pending.session) pending.session = new RawTuningSession(f.id);
  const mySeq = ++previewSeq;
  ui.tuningBusy.hidden = false;
  const url = await pending.session.render(pending.values);
  if (mySeq === previewSeq) ui.tuningBusy.hidden = true;
  if (!pending || pending.fileId !== f.id) return;   // superseded while the render was in flight
  if (url) ui.tuning.src = url;
  else requestPreviewNetwork(f);
}

function requestPreviewNetwork(f) {
  const qs = new URLSearchParams({size: 'Medium'});
  for (const [k, v] of Object.entries(pending.values)) if (v != null) qs.set(k, v);
  ui.tuning.src = `/api/files/${f.id}/raw_preview?${qs}`;   // onload -> syncTuningVisible
}

async function saveRawSettings(f) {
  const values = pending.values;
  let res;
  try {
    res = await post(`/api/files/${f.id}/raw_settings`, values);
    toast('saved; thumbnails will regenerate on next view');
  } catch (e) { return; }   // post() already showed a toast for a server error
  setRevision(f.id, res.rev);   // ticket 119: future imgUrl() calls fetch the fresh render
  if (current().file_id === f.id) current().rev = res.rev;
  discardPending();
  if (current().file_id === f.id) {
    ui.img.src = imgUrl('Medium', f.id);
    retryOnce(ui.img, current());
    // Ticket 110/119: the grid cell and filmstrip node for this photo, if already in the DOM, would
    // otherwise never notice the Thumb they're showing just went stale server-side.
    refreshCellThumb(current().id, f.id);
    ui.strip.refreshThumb(current().id, f.id);
  }
  if (filesOpen) openFiles();
}

function rawSettingsControls(f) {
  if (!pending || pending.fileId !== f.id) {
    if (pending && pending.session) pending.session.dispose();   // ticket 105: don't leak a worker
    pending = {fileId: f.id, values: committedValues(f), timer: null};
  }
  // Ticket 107: hovering the sliders block is one of the two ways to reveal the tuned overlay
  // (the other is holding Shift). Attached to this outer container, which renderRawSettingsBody
  // repopulates in place via setChildren -- so the listener survives every slider tick's re-render.
  const container = el('div', {
    class: 'raw-settings',
    onmouseenter: () => { sliderHover = true; syncTuningVisible(); },
    onmouseleave: () => { sliderHover = false; syncTuningVisible(); },
  });
  renderRawSettingsBody(f, container);
  return container;
}

function renderRawSettingsBody(f, container) {
  const values = pending.values;
  const committed = committedValues(f);
  const wbMode = values.wb_mode || 'camera';
  const bright = values.bright ?? 1.0;
  const highlight = values.highlight ?? 0;
  const brightLabel = el('span', {class: 'meta', text: bright.toFixed(2)});
  const highlightLabel = el('span', {class: 'meta', text: String(highlight)});
  const exposureLabel = el('span', {class: 'meta', text: (values.exposure ?? 1.0).toFixed(2)});
  const shadowLabel = el('span', {class: 'meta', text: (values.shadow ?? 0.0).toFixed(2)});
  const saturationLabel = el('span', {class: 'meta', text: (values.saturation ?? 1.0).toFixed(2)});
  const contrastLabel = el('span', {class: 'meta', text: (values.contrast ?? 1.0).toFixed(2)});
  const saveBtn = el('button', {class: 'primary', text: 'Save', onclick: () => saveRawSettings(f)});
  const discardBtn = el('button', {text: 'Discard changes', onclick: () => {
    discardPending();
    pending = {fileId: f.id, values: committedValues(f), timer: null};
    renderRawSettingsBody(f, container);
  }});
  const syncButtons = () => {
    const dirty = isDirty(values, committed);
    saveBtn.disabled = !dirty;
    discardBtn.hidden = !dirty;
  };
  syncButtons();
  const onTick = (key, label, fmt) => (e) => {
    values[key] = Number(e.target.value);
    label.textContent = fmt(values[key]);
    syncButtons();
    schedulePreview(f);
  };
  return setChildren(container,
    el('div', {class: 'row'},
      el('label', {text: 'Brightness'}),
      el('input', {type: 'range', min: '0.25', max: '3', step: '0.05', value: bright,
                 'aria-label': 'brightness', oninput: onTick('bright', brightLabel, (v) => v.toFixed(2))}),
      brightLabel),
    el('div', {class: 'row'},
      el('label', {text: 'Highlight recovery'}),
      el('input', {type: 'range', min: '0', max: '9', step: '1', value: highlight,
                 'aria-label': 'highlight recovery', oninput: onTick('highlight', highlightLabel, String)}),
      highlightLabel),
    el('div', {class: 'row'},
      el('label', {text: 'White balance'}),
      WB_MODES.map((m) => el('button', {
        class: wbMode === m ? 'on' : '', text: m,
        onclick: () => {
          Object.assign(values, m === 'manual'
            ? {wb_mode: m, wb_r: values.wb_r ?? DEFAULT_WB.r, wb_g: values.wb_g ?? DEFAULT_WB.g,
               wb_b: values.wb_b ?? DEFAULT_WB.b}
            : {wb_mode: m, wb_r: null, wb_g: null, wb_b: null});
          schedulePreview(f);
          renderRawSettingsBody(f, container);
        },
      }))),
    wbMode === 'manual' ? el('div', {class: 'row'},
      ['r', 'g', 'b'].map((c) => el('input', {
        type: 'number', step: '0.1', min: '0.1', class: 'wb-multiplier',
        value: values[`wb_${c}`] ?? DEFAULT_WB[c], 'aria-label': `white balance ${c}`,
        onchange: (e) => { values[`wb_${c}`] = Number(e.target.value); syncButtons(); schedulePreview(f); }}))) : null,
    el('div', {class: 'row'},
      el('label', {text: 'Exposure'}),
      el('input', {type: 'range', min: '0.25', max: '8.0', step: '0.05', value: values.exposure ?? 1.0,
                 'aria-label': 'exposure', oninput: onTick('exposure', exposureLabel, (v) => v.toFixed(2))}),
      exposureLabel),
    el('div', {class: 'row'},
      el('label', {text: 'Shadow pull'}),
      el('input', {type: 'range', min: '0.0', max: '0.5', step: '0.01', value: values.shadow ?? 0.0,
                 'aria-label': 'shadow pull', oninput: onTick('shadow', shadowLabel, (v) => v.toFixed(2))}),
      shadowLabel),
    // Ticket 112: the advanced controls (saturation, contrast, noise reduction, demosaic
    // algorithm) live behind a zipper so the loupe stays uncluttered for the common sliders above.
    el('details', {class: 'advanced'},
      el('summary', {text: 'Advanced'}),
      el('div', {class: 'row'},
        el('label', {text: 'Saturation'}),
        el('input', {type: 'range', min: '0.0', max: '2.0', step: '0.05',
                   value: values.saturation ?? 1.0, 'aria-label': 'saturation',
                   oninput: onTick('saturation', saturationLabel, (v) => v.toFixed(2))}),
        saturationLabel),
      el('div', {class: 'row'},
        el('label', {text: 'Contrast'}),
        el('input', {type: 'range', min: '0.5', max: '2.0', step: '0.05',
                   value: values.contrast ?? 1.0, 'aria-label': 'contrast',
                   oninput: onTick('contrast', contrastLabel, (v) => v.toFixed(2))}),
        contrastLabel),
      el('div', {class: 'row'},
        el('label', {text: 'Noise reduction'}),
        el('select', {'aria-label': 'noise reduction', onchange: (e) => {
          const v = Number(e.target.value);
          values.noise = v === 0 ? null : v;   // off is the default -> back to NULL
          syncButtons();
          schedulePreview(f);
        }}, NOISE_MODES.map(([v, text]) => el('option', {value: v, text,
              selected: Number(v) === (values.noise ?? 0)})))),
      el('div', {class: 'row'},
        el('label', {text: 'Demosaic'}),
        el('select', {'aria-label': 'demosaic', onchange: (e) => {
          const v = Number(e.target.value);
          values.demosaic = v === 3 ? null : v;   // AHD is the default -> back to NULL
          syncButtons();
          schedulePreview(f);
        }}, DEMOSAIC_MODES.map(([v, text]) => el('option', {value: v, text,
              selected: Number(v) === (values.demosaic ?? 3)}))))),
    el('div', {class: 'row'}, saveBtn, discardBtn,
      Object.values(values).every((v) => v == null) ? null : el('button', {
        text: 'Reset to default', onclick: () => {
          Object.assign(values, {bright: null, wb_mode: null, wb_r: null, wb_g: null, wb_b: null, highlight: null, exposure: null, shadow: null, saturation: null, contrast: null, noise: null, demosaic: null});
          syncButtons();
          schedulePreview(f);
          renderRawSettingsBody(f, container);
        },
      })));
}

// ------------------------------------------- non-destructive crop (ticket 115)
//
// A crop is stored per file as normalized x/y/w/h fractions. Thumb/Small are rendered cropped
// server-side (the grid and filmstrip show the intended composition), while the loupe's
// Medium/Huge stay full-frame and the cropped-out part is shaded dark (ticket 116's answer).
// The editor below overlays an interactive rectangle on the full-frame Medium image; Save POSTs
// the rectangle and clears the thumbnail cache so the cropped sizes regenerate; Discard just
// drops the overlay (nothing was written until Save).
//
// Non-RAW files never got a raw-settings panel, so this is deliberately its own mode rather than
// folded into it -- crop applies to JPEG and RAW alike.

let cropMode = false;
let cropState = null;   // {photo, rect, layer, rectEl, drag}
const CROP_MIN = 0.05;
const CROP_HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const clampTo = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function cropFromColumns(row) {
  return row && row.crop_x != null
    ? {x: row.crop_x, y: row.crop_y, w: row.crop_w, h: row.crop_h} : null;
}

function setRectStyle(node, rect) {
  node.style.left = rect.x * 100 + '%';
  node.style.top = rect.y * 100 + '%';
  node.style.width = rect.w * 100 + '%';
  node.style.height = rect.h * 100 + '%';
}

// Ticket 129: map a normalized rectangle from the source frame into the frame displayed when the
// image is rotated `rotation` degrees counter-clockwise (and back, with (360 - rotation) % 360).
// The server applies rotation to the pixels as the final render transform, but the crop rectangle
// is stored in source coordinates, so the shaded-out region has to be turned the same way.
function rotateRect(rect, rotation) {
  const r = ((rotation || 0) % 360 + 360) % 360;
  if (r === 90) return {x: rect.y, y: 1 - (rect.x + rect.w), w: rect.h, h: rect.w};
  if (r === 180) return {x: 1 - (rect.x + rect.w), y: 1 - (rect.y + rect.h), w: rect.w, h: rect.h};
  if (r === 270) return {x: 1 - (rect.y + rect.h), y: rect.x, w: rect.h, h: rect.w};
  return rect;
}

// Position a shade/editor layer exactly over the displayed image (object-fit: contain centers it
// within the stage, so img.getBoundingClientRect() is the real content box) and place its rect.
function positionCropLayer(layer, rect) {
  const stage = ui.stage.getBoundingClientRect();
  const img = ui.img.getBoundingClientRect();
  layer.style.left = img.left - stage.left + 'px';
  layer.style.top = img.top - stage.top + 'px';
  layer.style.width = img.width + 'px';
  layer.style.height = img.height + 'px';
  setRectStyle(layer.querySelector('.crop-rect'), rect);
}

function updateCropShade() {
  if (!ui.cropShade) return;
  const photo = current();
  if (cropMode || zoomed || !photo || !photo.crop || !ui.img.naturalWidth) {
    ui.cropShade.hidden = true;
    return;
  }
  positionCropLayer(ui.cropShade, rotateRect(photo.crop, photo.rotation));
  ui.cropShade.hidden = false;
}

function toggleCropMode() { cropMode ? exitCropMode(false) : enterCropMode(); }

function enterCropMode() {
  const photo = current();
  if (!photo) return;
  if (!ui.img.naturalWidth) {
    // The image (and so its on-screen box) is not there yet; retry once it loads, unless the user
    // has navigated away in the meantime.
    whenShown(() => { if (current() === photo) enterCropMode(); });
    return;
  }
  if (zoomed) ui.zoom.exit();
  exitCropMode(false);
  cropMode = true;
  // Ticket 129: the editor works in the displayed (rotated) frame; saveCrop turns the rectangle
  // back into source coordinates before posting.
  const rect = rotateRect(photo.crop ? {...photo.crop} : {x: 0, y: 0, w: 1, h: 1}, photo.rotation);
  const rectEl = el('div', {class: 'crop-rect crop-editable'},
    CROP_HANDLES.map((dir) => el('div', {class: 'crop-handle ' + dir, dataset: {dir}})));
  const layer = el('div', {class: 'crop-shade crop-editing',
    // Keep the stage's swipe handler from capturing the pointer (which would swallow the
    // actions' clicks), the same way the Files panel does.
    onpointerdown: (e) => e.stopPropagation()},
    rectEl,
    el('div', {class: 'crop-actions'},
      el('button', {class: 'primary', text: 'Save crop',
                    onclick: (e) => { e.stopPropagation(); saveCrop(); }}),
      el('button', {text: 'Discard', onclick: (e) => { e.stopPropagation(); exitCropMode(false); }})));
  ui.stage.append(layer);
  cropState = {photo, rect, layer, rectEl, drag: null};
  setRectStyle(rectEl, rect);
  positionCropLayer(layer, rect);
  ui.cropShade.hidden = true;
  layer.addEventListener('pointerdown', onCropPointerDown);
  renderHud();
}

function exitCropMode() {
  if (!cropMode) return;
  cropMode = false;
  if (cropState) { cropState.layer.remove(); cropState = null; }
  updateCropShade();
  renderHud();
}

function onCropPointerDown(e) {
  if (!cropState) return;
  const dir = e.target.dataset ? e.target.dataset.dir : null;
  const moving = e.target.classList.contains('crop-editable');
  if (!dir && !moving) return;
  e.preventDefault();
  e.stopPropagation();
  const box = cropState.layer.getBoundingClientRect();
  cropState.drag = {dir: dir || 'move', startX: e.clientX, startY: e.clientY,
                    rect: {...cropState.rect}, boxW: box.width || 1, boxH: box.height || 1};
  e.target.setPointerCapture(e.pointerId);
  cropState.layer.addEventListener('pointermove', onCropPointerMove);
  cropState.layer.addEventListener('pointerup', onCropPointerUp);
  cropState.layer.addEventListener('pointercancel', onCropPointerUp);
}

function onCropPointerMove(e) {
  const d = cropState && cropState.drag;
  if (!d) return;
  const dx = (e.clientX - d.startX) / d.boxW;
  const dy = (e.clientY - d.startY) / d.boxH;
  const r = d.rect;
  let {x, y, w, h} = r;
  if (d.dir === 'move') {
    x = clampTo(r.x + dx, 0, 1 - r.w);
    y = clampTo(r.y + dy, 0, 1 - r.h);
  } else {
    let left = r.x, top = r.y, right = r.x + r.w, bottom = r.y + r.h;
    if (d.dir.includes('w')) left = clampTo(r.x + dx, 0, right - CROP_MIN);
    if (d.dir.includes('e')) right = clampTo(r.x + r.w + dx, left + CROP_MIN, 1);
    if (d.dir.includes('n')) top = clampTo(r.y + dy, 0, bottom - CROP_MIN);
    if (d.dir.includes('s')) bottom = clampTo(r.y + r.h + dy, top + CROP_MIN, 1);
    x = left; y = top; w = right - left; h = bottom - top;
  }
  cropState.rect = {x, y, w, h};
  setRectStyle(cropState.rectEl, cropState.rect);
}

function onCropPointerUp() {
  if (!cropState) return;
  cropState.drag = null;
  cropState.layer.removeEventListener('pointermove', onCropPointerMove);
  cropState.layer.removeEventListener('pointerup', onCropPointerUp);
  cropState.layer.removeEventListener('pointercancel', onCropPointerUp);
}

async function saveCrop() {
  if (!cropState) return;
  const {photo, rect} = cropState;
  // The editor drew in the displayed frame; convert back to source coordinates (ticket 129).
  const sourceRect = rotateRect(rect, (360 - (photo.rotation || 0)) % 360);
  try {
    const res = await post(`/api/files/${photo.file_id}/crop`, sourceRect);
    photo.crop = cropFromColumns(res.crop);
    photo.rev = res.rev;
    setRevision(photo.file_id, res.rev);   // ticket 119: Thumb/Small were cleared server-side
    toast('crop saved; thumbnails will regenerate on next view');
  } catch (e) { return; }   // post() already toasted the server error
  exitCropMode();
  if (current() === photo) {
    // Medium is unchanged (full frame) but the Thumb/Small the grid and filmstrip show are stale.
    refreshCellThumb(photo.id, photo.file_id);
    ui.strip.refreshThumb(photo.id, photo.file_id);
    updateCropShade();
  }
  if (filesOpen) openFiles();
}

// ------------------------------------------- non-destructive rotation (ticket 129)
//
// The "⟲" HUD button turns the representative file 90 degrees left (counter-clockwise),
// cumulatively, and stores the result per file (degrees counter-clockwise, 0/90/180/270) like the
// crop. The server applies it as the final render transform for every size, so all this does is
// POST the new value, clear/reload the cached render, and keep the grid/filmstrip thumbs fresh --
// ticket 119's thumb_rev carries the cache-busting.
async function rotateLeft() {
  const photo = current();
  const before = photo.rotation || 0;
  const value = (before + 90) % 360;
  photo.rotation = value;
  updateCropShade();                       // optimistic: the shade turns at once
  const isLatest = beginEdit(photo);
  return enqueue(async () => {
    try {
      const r = await post(`/api/files/${photo.file_id}/rotation`, {rotation: value});
      if (isLatest()) {
        photo.rotation = r.rotation;
        photo.rev = r.rev;
        setRevision(photo.file_id, r.rev);   // future imgUrl() calls fetch the turned render
        ui.img.src = imgUrl('Medium', photo.file_id);
        retryOnce(ui.img, photo);
        refreshCellThumb(photo.id, photo.file_id);
        ui.strip.refreshThumb(photo.id, photo.file_id);
        updateCropShade();
      }
      toast('rotated');
    } catch (e) {
      if (isLatest()) { photo.rotation = before; updateCropShade(); }
      toast(e.message, true);
    }
  });
}

async function openFiles() {
  filesOpen = true;
  if (ui.stage) {
    ui.stage.classList.add('panel-open');   // ticket 121: recenter image immediately
    updateCropShade();
  }
  const photo = current();
  let detail;
  try { detail = await get('/api/photos/' + photo.id); } catch (e) { toast(e.message, true); closeFiles(); return; }
  if (!filesOpen || current() !== photo) return;
  seedRevisions(detail.files);   // ticket 119: sibling files' revisions for row previews
  closePanelOnly();
  hideRowPreview();   // ticket 107: drop any hover state from the panel being replaced
  ui.panel = el('div', {class: 'files-panel', onclick: (e) => e.stopPropagation(), onpointerdown: (e) => e.stopPropagation()},
    el('h3', {text: `Files (${detail.files.length})`}),
    el('div', {class: 'meta', text: `rating ${label(detail.rating)}` + (detail.conflict ? ' — sidecars disagree' : '')}),
    detail.sidecars.map((s) => el('div', {class: 'meta', text: `${s.path}: ${s.rating ?? 'no rating'}${s.fav ? ' ♥' : ''}`})),
    el('p'),
    // Ticket 107: hovering a sibling (non-representative) file's row previews that file's own
    // thumbnail via ui.rowPreview -- the representative row is already what's on screen, so it's
    // excluded rather than swapping an image in for itself.
    detail.files.map((f) => el('div', {
      class: 'file' + (f.id === detail.representative_file_id ? ' rep' : ''),
      onmouseenter: f.id === detail.representative_file_id ? null : () => showRowPreview(f),
      onmouseleave: f.id === detail.representative_file_id ? null : hideRowPreview,
    },
      el('div', {text: f.path.split('/').pop() + '  ·  ' + f.role + (f.link_source === 'manual' ? ' (manual)' : '')}),
      (() => {
        // Ticket 154: the directory portion of f.path becomes its own link (jump straight to
        // that folder, non-recursive, keeping the current filter/sort -- "browsing a filtered
        // view of a big tree and want to jump to where a file actually lives") -- but only in a
        // recursive ("this folder + subfolders") view: in a non-recursive one a file's directory
        // is always the folder already being browsed, so the link would be trivial. Falls back to
        // the plain path (dir === null at the root, nothing to split off) unchanged either way.
        const slash = f.path.lastIndexOf('/');
        const dir = slash < 0 ? null : f.path.slice(0, slash);
        const name = slash < 0 ? f.path : f.path.slice(slash + 1);
        const pathParts = (state.route.recursive && dir)
          ? [el('a', {href: href({dir, filter: state.route.filter, sort: state.route.sort}),
                     text: dir}), '/' + name]
          : [f.path];
        return el('div', {class: 'meta'},
          `${f.width || '?'}×${f.height || '?'}  ${fmtBytes(f.bytesize)}  `, ...pathParts,
          f.missing ? '  MISSING' : '');
      })(),
      // Ticket 099: a file exported (096/097) from another photo links back to it -- own Photo,
      // just cross-referenced, not merged into the source's file list.
      f.exported_from ? el('div', {class: 'meta'},
        'exported from: ', el('a', {href: href({dir: f.exported_from.dir, photo: f.exported_from.photo_id}),
                                    text: f.exported_from.path})) : null,
      cameraMetaText(f) ? el('div', {class: 'meta', text: cameraMetaText(f)}) : null,
      f.is_raw && !f.missing ? rawSettingsControls(f) : null,
      el('div', {class: 'row'},
        f.id === detail.representative_file_id ? el('span', {class: 'ok', text: 'shown'}) :
          el('button', {text: 'Show this', onclick: () => setRepresentative(detail, f.id)}),
        f.id !== detail.original_file_id ? el('button', {text: 'Unlink', onclick: () => unlink(f)}) : null,
        !f.missing ? el('button', {class: 'danger', text: 'Delete',
                                   title: 'move this file to trash', onclick: () => confirmDeleteFile(f)}) : null))),
    el('div', {class: 'row'},
      el('button', {text: 'Use default', onclick: () => setRepresentative(detail, null)}),
      el('button', {text: 'Close', onclick: closeFiles})));
  ui.stage.append(ui.panel);
  ui.stage.classList.add('panel-open');   // ticket 121: recenter image to avoid panel
  updateCropShade();
  renderHud();
}

function closePanelOnly() {
  if (ui.panel) { ui.panel.remove(); ui.panel = null; }
  if (ui.stage && !filesOpen) {
    ui.stage.classList.remove('panel-open');   // ticket 121
    updateCropShade();
  }
}

function closeFiles() {
  filesOpen = false;
  discardPending();   // ticket 094: closing the panel without Save drops any provisional tuning
  hideRowPreview();   // ticket 107
  if (ui.stage) {
    ui.stage.classList.remove('panel-open');
    updateCropShade();
  }
  closePanelOnly();
  if (root && !root.hidden) renderHud();
}

// ------------------------------------------------- filter switcher (ticket 092)
//
// Switches the active filter from inside the loupe without losing the currently-shown photo:
// the filmstrip/prev-next context updates to the new filter's photo list, but the image on
// screen does not reload or reset (no call to show()). Deliberately not app.js's filterRow.go(),
// which always closes the loupe and drops the current photo.

function renderFilterPanel() {
  const photo = current();
  const option = (value, text) => {
    const active = state.route.filter === value;
    const ok = matches(photo, value);
    const count = state.counts ? state.counts[value] : null;
    return el('button', {
      class: 'filter-option' + (active ? ' on' : ''),
      dataset: {filter: value},
      disabled: !ok,
      title: ok ? null : 'this photo does not match this filter',
      onclick: ok ? () => switchFilterTo(value) : null,
    }, el('span', {class: 'lbl', text}), count != null ? el('span', {class: 'n', text: String(count)}) : null);
  };
  ui.filterPanel = el('div', {class: 'filter-picker', onclick: (e) => e.stopPropagation(), onpointerdown: (e) => e.stopPropagation()},
    el('div', {class: 'filter-row'},
      filterPrimary().filter(([value]) => !/^rating:/.test(value))
        .map(([value, text]) => option(value, text))),
    // Ticket 117: a 3-column grid per star value -- <= N, = N, >= N (ticket 092's panel, extended).
    el('div', {class: 'filter-grid'},
      starValues().map((n) => el('div', {class: 'filter-star-row'},
        el('span', {class: 'star-lbl', text: `★${n}`}),
        option(`rating<=${n}`, '≤'), option(`rating:${n}`, '='), option(`rating>=${n}`, '≥')))));
  ui.stage.append(ui.filterPanel);
}

function openFilterPicker() {
  filterPanelOpen = true;
  closeFilterPanelOnly();
  renderFilterPanel();
  renderHud();
}

function closeFilterPanelOnly() {
  if (ui.filterPanel) { ui.filterPanel.remove(); ui.filterPanel = null; }
}

function closeFilterPicker() {
  filterPanelOpen = false;
  closeFilterPanelOnly();
  if (root && !root.hidden) renderHud();
}

function toggleFilterPicker() { filterPanelOpen ? closeFilterPicker() : openFilterPicker(); }

async function switchFilterTo(newFilter) {
  const photoId = current().id;
  closeFilterPicker();
  if (newFilter === state.route.filter) return;
  const newRoute = {...state.route, filter: newFilter};
  history.replaceState(null, '', href(newRoute));
  try {
    await loadFolder(newRoute);
    if (!state.photos.some((p) => p.id === photoId)) await loadRest(newRoute);
  } catch (e) { toast(e.message, true); return; }
  if (!isOpen()) return;
  const i = state.photos.findIndex((p) => p.id === photoId);
  if (i < 0) { toast('lost track of the photo while switching filters', true); return; }
  index = i;
  renderFilmstrip(true);
  preload(1);
  renderHud();
}

async function setRepresentative(detail, fileId) {
  const photo = current();
  try {
    const d = await post(`/api/photos/${photo.id}/representative`, {file_id: fileId});
    seedRevisions(d.files);   // ticket 119: the new representative's revision
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

// Modal confirmation before moving a single file to trash (ticket 082) -- deliberately its own
// small modal, not the Files panel or a route: this acts on one file, not a whole review set.
function confirmDeleteFile(file) {
  closeDeleteModal();
  const name = file.path.split('/').pop();
  ui.deleteModal = el('div', {class: 'confirm-modal', onclick: closeDeleteModal},
    el('div', {class: 'confirm-card', onclick: (e) => e.stopPropagation()},
      el('h3', {text: 'Move this file to trash?'}),
      el('img', {class: 'confirm-img', src: imgUrl('Medium', file.id), alt: name}),
      el('div', {class: 'meta', text: file.path}),
      el('div', {class: 'row'},
        el('button', {text: 'Cancel', onclick: closeDeleteModal}),
        el('button', {class: 'danger', text: 'Move to trash', onclick: () => deleteFile(file)}))));
  document.body.append(ui.deleteModal);
}

function closeDeleteModal() {
  if (ui.deleteModal) { ui.deleteModal.remove(); ui.deleteModal = null; }
}

async function deleteFile(file) {
  try {
    await post(`/api/files/${file.id}/trash`);
    closeDeleteModal();
    toast('moved to trash');
    state.dirty = true;
    if (filesOpen) openFiles();
  } catch (e) { toast(e.message, true); }
}

// -------------------------------------------------------------- keyboard

function onKey(e) {
  if (ui.deleteModal) { if (e.key === 'Escape') closeDeleteModal(); return; }
  // Ticket 115: crop mode swallows the normal hotkeys (an accidental rating key while dragging a
  // handle would be surprising); Escape discards the crop.
  if (cropMode) { if (e.key === 'Escape') exitCropMode(); return; }
  // Ticket 107: Shift is the compare modifier -- checked before isTyping's guard because a bare
  // modifier key never types a character into a focused input (unlike a letter hotkey), so it
  // must keep working even while a raw-settings slider has focus, which is exactly when comparing
  // is most wanted.
  if (e.key === 'Shift') {
    if (!e.repeat) { shiftHeld = true; syncTuningVisible(); }
    return;
  }
  if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
  const key = e.key;
  if (key === 'ArrowRight' || key === ' ') go(1);
  else if (key === 'ArrowLeft' || key === 'Backspace') go(-1);
  else if (key === 'ArrowUp') rateStep(1);
  else if (key === 'ArrowDown') rateStep(-1);
  else if (key === 'Home') show(0);
  else if (key === 'End') show(state.photos.length - 1);
  else if (/^[0-5]$/.test(key) || key === 'x' || key === 'X') { const p = current(); setRating(afterKey(p.rating, key, p.previous_stars)); }
  else if (key === 'f' || key === 'F') toggleFav();
  else if (key === 't' || key === 'T') openTagInput();
  else if (key === 'z' || key === 'Z') toggleZoom();
  else if (key === '+' || key === '=') whenShown(() => ui.zoom.zoomIn());
  else if (key === '-' || key === '_') whenShown(() => ui.zoom.zoomOut());
  else if (key === 'g' || key === 'G') cycleRepresentative();
  else if (key === 'r' || key === 'R') rotateLeft();
  else if (key === 'u' || key === 'U') undo();
  else if (key === 'i' || key === 'I') filesOpen ? closeFiles() : openFiles();
  else if (key === 'Escape') {
    if (filterPanelOpen) closeFilterPicker();
    else if (filesOpen) closeFiles();
    else closeToGrid();
  }
  else return;
  e.preventDefault();
}

function onKeyUp(e) {
  // Ticket 107: releasing Shift drops the keyboard vote for showing the tuned overlay; it stays
  // visible if the mouse is still hovering the sliders block.
  if (e.key === 'Shift') { shiftHeld = false; syncTuningVisible(); }
}
