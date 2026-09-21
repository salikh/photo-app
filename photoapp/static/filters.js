// The rating filters offered as buttons (ticket 055), and their labels.
//
// Primary row: All, Rejected, Unrated, and exactly N stars. Everything else the
// server can filter by is in the "more" menu. With --one_star_is_unrated there
// is no 1-star button (1 star counts as unrated, see rating.js).

import {isOneStarUnrated} from './rating.js';
import {get} from './api.js';
import {state} from './state.js';
import {enqueue} from './util.js';

const MORE = [
  ['picked', 'picked (any stars)'],
  ['rated', 'rated or rejected'],
  ['fav', 'favorites'],
  ['conflict', 'sidecars disagree'],
];

export function primary() {
  const buttons = [['all', 'All', 'all photos (Shift+A)'],
                   ['rejected', '✖ Rejected', 'rejected only (Shift+X)'],
                   ['unrated', '☆ Unrated', 'unrated only (Shift+0)']];
  for (let n = 1; n <= 5; n++) {
    if (n === 1 && isOneStarUnrated()) continue;
    buttons.push([`rating:${n}`, `★${n}`, `exactly ${n} star${n > 1 ? 's' : ''} (Shift+${n})`]);
  }
  return buttons;
}

export function more() { return MORE; }

// Short text for the current filter (loupe HUD, status line).
export function label(filter) {
  const found = primary().find((b) => b[0] === filter) || MORE.find((m) => m[0] === filter);
  return found ? found[1] : filter;
}

// Does a photo match a filter? Mirrors photoapp/library.py so the UI agrees with what a
// reload would show (used when a rating change makes a photo leave the view, ticket 056).
export function matches(photo, filter) {
  const oneStar = isOneStarUnrated();
  const r = photo.rating;
  switch (filter) {
    case 'all': return true;
    case 'unrated': return oneStar ? (r === 0 || r === 1) : r === 0;
    case 'rejected': return r === -1;
    case 'picked': return oneStar ? r > 1 : r > 0;
    case 'rated': return oneStar ? (r !== 0 && r !== 1) : r !== 0;
    case 'fav': return !!photo.fav;
    case 'conflict': return !!photo.conflict;
    default: {
      const m = /^rating:([1-5])$/.exec(filter || '');
      return !!m && r === Number(m[1]);
    }
  }
}


// ---- counts per filter for the current folder (ticket 057) ----

// Shortcuts for the grid (the viewer has its own keys): Shift + A / X / 0..5. Alt+digit is not
// used because browsers switch tabs with it.
export function shortcutFor(code) {
  if (code === 'KeyA') return 'all';
  if (code === 'KeyX') return 'rejected';
  const m = /^Digit([0-5])$/.exec(code);
  if (!m) return null;
  if (m[1] === '0') return 'unrated';
  if (m[1] === '1' && isOneStarUnrated()) return null;
  return `rating:${m[1]}`;
}

export function applyCounts(counts) {
  state.counts = counts || {};
  document.querySelectorAll('.filters button[data-filter]').forEach((btn) => {
    const n = state.counts[btn.dataset.filter];
    const span = btn.querySelector('.n');
    if (span) span.textContent = n == null ? '' : String(n);
    btn.classList.toggle('zero', n === 0);
  });
}

// Refetch after the pending edits have been applied on the server (same ordered queue), so the
// numbers are the server's. Debounced: a run of edits causes one request.
let countsTimer = null;
export function scheduleCountsRefresh() {
  clearTimeout(countsTimer);
  countsTimer = setTimeout(() => {
    enqueue(async () => {
      if (!state.route || state.route.page !== 'browse') return;
      try {
        applyCounts((await get('/api/photos/counts?dir=' + encodeURIComponent(state.route.dir))).counts);
      } catch (e) { /* the numbers are a convenience; leave them as they are */ }
    });
  }, 250);
}
