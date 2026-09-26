// The rating filters offered as buttons (ticket 055), and their labels.
//
// Primary row: All, Rejected, Unrated, and exactly N stars. Everything else the
// server can filter by is in the "more" menu. With --one_star_is_unrated there
// is no 1-star button (1 star counts as unrated, see rating.js).

import {isOneStarUnrated} from './rating.js';
import {get} from './api.js';
import {state} from './state.js';
import {enqueue, el, setChildren} from './util.js';

const MORE = [
  ['picked', 'picked (any stars)'],
  ['rated', 'rated or rejected'],
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
  buttons.push(['fav', '♥ Fav', 'favorites only']);   // ticket 087: promoted out of "more filters"
  return buttons;
}

export function more() { return MORE; }

// Ticket 117: the star values that get a button/row (1 is skipped when 1 star counts as unrated).
export function starValues() {
  const values = [];
  for (let n = 1; n <= 5; n++) {
    if (n === 1 && isOneStarUnrated()) continue;
    values.push(n);
  }
  return values;
}

// The state a star button shows for the current filter: the active comparator (or the plain
// exactly-N state when this star isn't the active one).
export function starButtonState(current, n) {
  for (const [prefix, label] of [['rating>=', `★≥${n}`], ['rating<=', `★≤${n}`], ['rating:', `★${n}`]]) {
    if (current === prefix + n) return {filter: current, active: true, label};
  }
  return {filter: `rating:${n}`, active: false, label: `★${n}`};
}

// The next state when the same star button is clicked again: = -> >= -> <= -> = (ticket 117).
export function nextStarFilter(current, n) {
  if (current === `rating:${n}`) return `rating>=${n}`;
  if (current === `rating>=${n}`) return `rating<=${n}`;
  if (current === `rating<=${n}`) return `rating:${n}`;
  return `rating:${n}`;   // any other filter (or a different star): start at exactly N
}

// Short text for the current filter (loupe HUD, status line).
export function label(filter) {
  const found = primary().find((b) => b[0] === filter) || MORE.find((m) => m[0] === filter);
  if (found) return found[1];
  const ge = /^rating>=([1-5])$/.exec(filter || '');
  if (ge) return `★≥${ge[1]}`;
  const le = /^rating<=([1-5])$/.exec(filter || '');
  if (le) return `★≤${le[1]}`;
  const t = /^tag:(.+)$/.exec(filter || '');
  return t ? `tag: ${t[1]}` : filter;
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
      const eq = /^rating:([1-5])$/.exec(filter || '');
      if (eq) return r === Number(eq[1]);
      const ge = /^rating>=([1-5])$/.exec(filter || '');
      if (ge) return r >= Number(ge[1]);
      const le = /^rating<=([1-5])$/.exec(filter || '');
      if (le) return r >= 1 && r <= Number(le[1]);
      const t = /^tag:(.+)$/.exec(filter || '');
      // Ticket 123: a dot-tag also matches a file's implied tag (its hidden directory name).
      return !!t && ((photo.tags || []).includes(t[1]) || (photo.implied || []).includes(t[1]));
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

// ---- tag filter dropdown (ticket 087, free-form option ticket 122) ----
//
// Tags are an open set (docs/design/databases.md's tags table has no controlled vocabulary), so
// unlike the fixed "more filters" list this <select>'s options are rebuilt from state.tags
// whenever they change, not just its selected value/counts. Ticket 122 adds a "Other tag…"
// sentinel option so any arbitrary tag (notably a dot-tag, ticket 123) can be typed even when it
// is not among the current view's aspects.

export const CUSTOM_TAG = '__custom__';

export function applyTags(tags) {
  state.tags = tags || [];
  const sel = document.querySelector('.filters select.tag-filter');
  if (!sel) return;
  const known = new Set(state.tags.map((t) => 'tag:' + t.tag));
  const options = [el('option', {value: '', text: 'tag: …'})];
  // An already-active custom tag (not in this view's aspects) still gets an option, so the
  // selector shows what is filtering the view instead of silently falling back to the placeholder.
  const active = state.route && state.route.filter;
  if (active && active.startsWith('tag:') && !known.has(active)) {
    options.push(el('option', {value: active, text: 'tag: ' + active.slice(4)}));
    known.add(active);
  }
  options.push(...state.tags.map(({tag, count}) =>
    el('option', {value: 'tag:' + tag, text: `${tag} (${count})`})));
  options.push(el('option', {value: CUSTOM_TAG, text: 'Other tag…'}));
  setChildren(sel, options);
  sel.value = active && known.has(active) ? active : '';
  sel.classList.toggle('on', sel.value !== '');
}

// Refetch after the pending edits have been applied on the server (same ordered queue), so the
// numbers are the server's. Debounced: a run of edits causes one request.
let countsTimer = null;
export function scheduleCountsRefresh() {
  clearTimeout(countsTimer);
  countsTimer = setTimeout(() => {
    enqueue(async () => {
      if (!state.route || state.route.page !== 'browse') return;
      const dir = encodeURIComponent(state.route.dir);
      const rec = state.route.recursive ? '&recursive=1' : '';
      try {
        applyCounts((await get(`/api/photos/counts?dir=${dir}${rec}`)).counts);
      } catch (e) { /* the numbers are a convenience; leave them as they are */ }
      try {
        applyTags((await get(`/api/photos/tags?dir=${dir}${rec}`)).tags);
      } catch (e) { /* same: convenience only */ }
    });
  }, 250);
}
