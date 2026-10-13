// Ticket 241: the grid's magnification. Level 0 is the original small tile; 1..3 are bigger.
// Each level fixes the tile's minimum width (the --cell variable, which the grid's auto-fill
// column rule reads), and the thumbnail size follows from the tile's real on-screen pixels so a
// photo is never upscaled: the 300 px Thumb while the tile is at most that wide, else the 1000 px
// Small. Steps come from '+' / '-', Ctrl+wheel (a trackpad pinch arrives as Ctrl+wheel) and a
// two-finger pinch.

import {imgUrl} from './api.js';

export const LEVELS = 4;
const CELL = {wide: [150, 230, 340, 500], phone: [110, 150, 260, 380]};   // CSS px
const THUMB_EDGE = 300;      // long edge of Thumb (thumbs.LONG_EDGE)
const WHEEL_STEP = 60;       // accumulated Ctrl+wheel delta that makes one step
const PINCH_STEP = 1.25;     // finger-distance ratio that makes one step
const KEY = 'gridLevel';

let level = 0;
try { level = Math.min(LEVELS - 1, Math.max(0, parseInt(localStorage.getItem(KEY), 10) || 0)); } catch (e) { /* no storage */ }

const phone = () => window.matchMedia('(max-width: 600px)').matches;
export function nominalWidth() { return CELL[phone() ? 'phone' : 'wide'][level]; }

// Thumbnail size for a tile this many CSS px wide (a tile's long edge shows the photo's long edge).
export function sizeFor(width, dpr = window.devicePixelRatio || 1) {
  return width * dpr > THUMB_EDGE ? 'Small' : 'Thumb';
}

let measured = 0;   // the real width of a tile, once a grid is on screen
export function gridSize() { return sizeFor(measured || nominalWidth()); }
export function gridImgUrl(fileId) { return imgUrl(gridSize(), fileId); }
export function currentLevel() { return level; }

function apply() {
  document.documentElement.style.setProperty('--cell', nominalWidth() + 'px');
  document.documentElement.classList.toggle('grid-fit', level > 0);   // ticket 242
  const first = document.querySelector('#grid .cell');
  measured = first ? first.getBoundingClientRect().width : 0;
  for (const img of document.querySelectorAll('#grid .cell img[data-fid]')) {
    const url = gridImgUrl(Number(img.dataset.fid));
    if (img.getAttribute('src') !== url) { img.dataset.retryBase = url; img.src = url; }
  }
}

export function setLevel(n) {
  const next = Math.min(LEVELS - 1, Math.max(0, n));
  if (next === level) return false;
  level = next;
  try { localStorage.setItem(KEY, String(level)); } catch (e) { /* no storage */ }
  apply();
  return true;
}
export const step = (d) => setLevel(level + d);

// Wire the gestures once for the whole document; they act only while a grid is on screen.
export function init() {
  apply();
  const onGrid = () => !!document.getElementById('grid') && !document.querySelector('.loupe');
  let acc = 0, lastWheel = 0;
  document.addEventListener('wheel', (e) => {
    if (!e.ctrlKey || !onGrid()) return;
    e.preventDefault();   // also stops the browser's own page zoom
    const now = Date.now();
    if (now - lastWheel > 400) acc = 0;
    lastWheel = now;
    acc += e.deltaY * (e.deltaMode === 1 ? 16 : 1);
    if (Math.abs(acc) >= WHEEL_STEP) { step(acc < 0 ? 1 : -1); acc = 0; }
  }, {passive: false});
  document.addEventListener('keydown', (e) => {
    if (!onGrid() || e.altKey || e.metaKey) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    const d = (e.key === '+' || e.key === '=') ? 1 : (e.key === '-' || e.key === '_') ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    step(d);
  });
  let startDist = 0;
  const dist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  document.addEventListener('touchstart', (e) => {
    startDist = e.touches.length === 2 && onGrid() ? dist(e.touches) : 0;
  }, {passive: true});
  document.addEventListener('touchmove', (e) => {
    if (!startDist || e.touches.length !== 2) return;
    e.preventDefault();
    const ratio = dist(e.touches) / startDist;
    if (ratio >= PINCH_STEP) { step(1); startDist = dist(e.touches); }
    else if (ratio <= 1 / PINCH_STEP) { step(-1); startDist = dist(e.touches); }
  }, {passive: false});
  window.matchMedia('(max-width: 600px)').addEventListener('change', apply);
  new ResizeObserver(() => apply()).observe(document.body);
}
