// Help overlay: keyboard shortcuts reference (ticket 091). Opened by 'h', '?' or F1 from
// anywhere in the app -- a third, always-on listener (wired in app.js) alongside the browse-view
// one (app.js's own keydown listener) and the viewer one (loupe.js's onKey), since it needs to
// work regardless of which of those two is currently active.

import {el} from './util.js';

const BROWSE = [
  ['Shift+A', 'filter: All'], ['Shift+X', 'filter: Rejected'], ['Shift+0', 'filter: Unrated'],
  ['Shift+1..5', 'filter: exactly N stars'],
  ['U', 'undo'], ['Esc', 'clear selection'],
];
const VIEWER = [
  ['→ / Space', 'next photo'], ['← / Backspace', 'previous photo'],
  ['Home / End', 'first / last photo'],
  ['0..5', 'set rating'], ['X', 'reject'], ['↑ / ↓', 'rating +1 / -1'],
  ['F', 'favorite'], ['T', 'tag'],
  ['Z', 'zoom'], ['+ / -', 'zoom in / out'],
  ['G', 'cycle representative file'],
  ['U', 'undo'], ['I', 'files panel'],
  ['Esc', 'close panel, or close viewer'],
];

let modal = null;

export function isOpen() { return !!modal; }

function section(title, rows) {
  return el('div', {class: 'help-section'},
    el('h4', {text: title}),
    rows.map(([key, action]) => el('div', {class: 'help-row'},
      el('span', {class: 'key', text: key}), el('span', {text: action}))));
}

export function open() {
  if (modal) return;
  modal = el('div', {class: 'confirm-modal', onclick: close},
    el('div', {class: 'confirm-card help-card', onclick: (e) => e.stopPropagation()},
      el('h3', {text: 'Keyboard shortcuts'}),
      section('Browse', BROWSE),
      section('Viewer', VIEWER),
      el('div', {class: 'row'}, el('button', {text: 'Close (Esc)', onclick: close}))));
  document.body.append(modal);
}

export function close() {
  if (modal) { modal.remove(); modal = null; }
}

export function toggle() { if (modal) close(); else open(); }
