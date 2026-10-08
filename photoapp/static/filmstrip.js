// The thumbnail strip under the photo in the viewer (ticket 059): every photo of the current
// folder and filter, scrollable, and click/tap a thumbnail to jump to it.
//
// The list can be thousands long, so the strip is virtual: an inner element as wide as the whole
// list carries only the thumbnails near the visible part (a few dozen). Images start loading
// once scrolling has paused, so a fast scroll through a folder of RAW files does not ask the
// server for hundreds of thumbnails. The current photo stays centered when the photo changes,
// unless the user has just been scrolling or touching the strip.

import {imgUrl} from './api.js';
import {el, playOverlay, retryImage} from './util.js';

const GAP = 4;                 // between thumbnails
const BUFFER = 8;              // thumbnails kept in the DOM beyond each edge of the view
const LOAD_MARGIN = 2;         // ...and how far beyond the view images are loaded
const IDLE_MS = 120;           // scrolling this long ago counts as paused: start loading images
const MANUAL_MS = 900;         // after the user touched the strip, do not scroll it for them

export function createFilmstrip(onPick) {
  const inner = el('div', {class: 'strip-inner'});
  const root = el('div', {class: 'filmstrip', role: 'listbox', 'aria-label': 'photos in this folder'}, inner);
  const nodes = new Map();     // photo id -> element
  let photos = [];
  let index = -1;
  let lastScroll = -Infinity;  // last scroll event (any cause)
  let lastUser = -Infinity;    // last wheel/touch/pointer on the strip by the user (never yet)
  let idleTimer = null;
  let frame = 0;
  let centeredOnce = false;

  const geometry = () => {
    const w = parseFloat(getComputedStyle(root).getPropertyValue('--thumb-w')) || 84;
    return {w, step: w + GAP};
  };

  function releaseNode(id, node) {
    const img = node.querySelector('img');
    if (img) img.src = '';                 // cancels a download in progress
    node.remove();
    nodes.delete(id);
  }

  function visibleRange(step) {
    const first = Math.max(0, Math.floor(root.scrollLeft / step) - BUFFER);
    const last = Math.min(photos.length - 1, Math.ceil((root.scrollLeft + (root.clientWidth || 600)) / step) + BUFFER);
    return {first, last};
  }

  function render(list, currentIndex, {center = false} = {}) {
    photos = list;
    index = currentIndex;
    const {step} = geometry();
    inner.style.width = `${photos.length * step + GAP * 2}px`;
    const {first, last} = visibleRange(step);
    const wanted = new Set();
    for (let i = first; i <= last; i++) wanted.add(photos[i].id);
    for (const [id, node] of nodes) if (!wanted.has(id)) releaseNode(id, node);
    for (let i = first; i <= last; i++) {
      const photo = photos[i];
      let node = nodes.get(photo.id);
      if (!node) {
        node = el('a', {class: 'thumb', role: 'option', title: photo.name, dataset: {id: photo.id},
                        onclick: (e) => { e.preventDefault(); onPick(photo.id); }});
        inner.append(node);
        nodes.set(photo.id, node);
      }
      node.style.left = `${GAP + i * step}px`;
      node.dataset.index = i;
      const current = i === index;
      node.classList.toggle('current', current);
      node.setAttribute('aria-selected', current);
    }
    if (center) scrollToCurrent();
    scheduleLoad();
  }

  function scrollToCurrent() {
    if (index < 0 || !photos.length) return;
    const now = performance.now();
    if (now - lastUser < MANUAL_MS) return;            // do not fight a hand on the strip
    const {w, step} = geometry();
    const target = Math.max(0, GAP + index * step + w / 2 - root.clientWidth / 2);
    const first = !centeredOnce;
    centeredOnce = true;
    root.scrollTo({left: target, behavior: first ? 'auto' : 'smooth'});
  }

  // Load the images of the thumbnails in (and just beyond) the view, but only once scrolling paused.
  function loadImages() {
    const {step} = geometry();
    const from = Math.floor(root.scrollLeft / step) - LOAD_MARGIN;
    const to = Math.ceil((root.scrollLeft + (root.clientWidth || 600)) / step) + LOAD_MARGIN;
    for (const node of nodes.values()) {
      const i = Number(node.dataset.index);
      if (i < from || i > to || node.querySelector('img')) continue;
      const photo = photos[i];
      if (!photo) continue;
      const img = el('img', {src: imgUrl('Thumb', photo.file_id), alt: photo.name, decoding: 'async', draggable: 'false'});
      retryImage(img, photo.is_video ? 120 : 6);   // ticket 202
      node.append(img);
      if (photo.is_video) node.append(playOverlay());      // ticket 192
    }
  }

  function scheduleLoad() {
    clearTimeout(idleTimer);
    const sinceScroll = performance.now() - lastScroll;
    if (sinceScroll >= IDLE_MS) { loadImages(); return; }
    idleTimer = setTimeout(loadImages, IDLE_MS - sinceScroll + 5);
  }

  root.addEventListener('scroll', () => {
    lastScroll = performance.now();
    if (!frame) frame = requestAnimationFrame(() => { frame = 0; render(photos, index); });
    scheduleLoad();
  }, {passive: true});

  // A vertical mouse wheel scrolls the strip sideways.
  root.addEventListener('wheel', (e) => {
    lastUser = performance.now();
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      root.scrollLeft += e.deltaY;
      e.preventDefault();
    }
  }, {passive: false});
  for (const type of ['pointerdown', 'touchstart']) {
    root.addEventListener(type, () => { lastUser = performance.now(); }, {passive: true});
  }

  return {
    element: root,
    render,
    // Ticket 110/119: a node's <img> is only created once (loadImages skips it if already present),
    // so a re-tuned RAW's already-loaded strip thumbnail never re-fetches on its own -- same bug,
    // same fix, as grid.js's refreshCellThumb; imgUrl carries the file's revision (ticket 119).
    refreshThumb(photoId, fileId) {
      const node = nodes.get(photoId);
      const img = node && node.querySelector('img');
      if (img) img.src = imgUrl('Thumb', fileId);
    },
    // For the viewer opening again: forget the scroll position logic and drop the nodes.
    reset() {
      for (const [id, node] of [...nodes]) releaseNode(id, node);
      centeredOnce = false;
      lastUser = -Infinity;
    },
  };
}
