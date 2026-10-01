// Entry point: header, routing, keyboard outside the loupe, scan control.

import {get, post} from './api.js';
import {el, toast, isTyping} from './util.js';
import {parse, href, hrefPage} from './route.js';
import {state} from './state.js';
import * as grid from './grid.js';
import * as loupe from './loupe.js';
import * as pages from './pages.js';
import {configure} from './rating.js';
import * as filters from './filters.js';
import * as help from './help.js';
import * as exportAction from './export.js';
import * as aiRate from './aiRate.js';
import * as renameAction from './rename.js';

const app = document.getElementById('app');
const SORTS = [['date', 'date'], ['name', 'name']];
const PAGES = {activity: pages.activityPage, attention: pages.attentionPage,
               usage: pages.usagePage, jobs: pages.jobsPage,
               'delete-review': pages.deleteReviewPage};

let header = null;
let main = null;

function crumbs(route) {
  const parts = route.dir === '.' ? [] : route.dir.split('/');
  const links = [el('a', {href: href({...route, dir: '.', photo: null}), text: 'All'})];
  parts.forEach((name, i) => {
    links.push(' / ', el('a', {href: href({...route, dir: parts.slice(0, i + 1).join('/'), photo: null}), text: name}));
  });
  return el('div', {class: 'crumbs'}, links);
}

function select(options, value, onchange, label) {
  return el('select', {'aria-label': label, onchange: (e) => onchange(e.target.value)},
    options.map(([v, text]) => el('option', {value: v, text, selected: v === value})));
}

// One toggle button per rating mode, plus a menu for the less common filters.
function filterRow(route) {
  // replaceState (not location.hash =) so switching filters is not its own Back-button stop --
  // only folder navigation should be (ticket 077).
  const go = (filter) => {
    history.replaceState(null, '', href({...route, filter, photo: null}));
    render();
  };
  const extra = filters.more();
  const inMore = extra.some(([v]) => v === route.filter);
  return el('div', {class: 'filters', role: 'group', 'aria-label': 'filter'},
    filters.primary().map(([value, text, title]) => {
      const m = /^rating:([1-5])$/.exec(value);
      if (!m) {
        return el('button', {
          title, class: route.filter === value ? 'on' : '', 'aria-pressed': route.filter === value,
          dataset: {filter: value}, onclick: () => go(value),
        }, el('span', {class: 'lbl', text}), el('span', {class: 'n'}));
      }
      // Ticket 117: a star button cycles = -> >= -> <= -> = when clicked again; clicking a
      // different star (or any other filter) starts at exactly N.
      const n = Number(m[1]);
      const s = filters.starButtonState(route.filter, n);
      return el('button', {
        title: `click to cycle exactly / at least / at most ${n} star${n > 1 ? 's' : ''} ` +
               `(Shift+${n} for exactly ${n})`,
        class: s.active ? 'on' : '', 'aria-pressed': s.active,
        dataset: {filter: s.filter}, onclick: () => go(filters.nextStarFilter(route.filter, n)),
      }, el('span', {class: 'lbl', text: s.label}), el('span', {class: 'n'}));
    }),
    el('select', {'aria-label': 'more filters', class: inMore ? 'on' : '', onchange: (e) => e.target.value && go(e.target.value)},
      el('option', {value: '', text: 'more\u2026'}),
      extra.map(([v, text]) => el('option', {value: v, text, selected: v === route.filter}))),
    // ticket 087: options are populated by filters.applyTags (state.tags, an open set) right
    // after this element is in the DOM, not built here from a fixed list like the row above.
    // Ticket 122: the "Other tag…" option opens an input to type any tag (notably a dot-tag).
    el('select', {'aria-label': 'tag filter', class: 'tag-filter', onchange: (e) => {
      if (e.target.value === filters.CUSTOM_TAG) promptCustomTag(go);
      else if (e.target.value) go(e.target.value);
    }},
      el('option', {value: '', text: 'tag: \u2026'})));
}

// Ticket 122: a small modal to type any tag. Enter/Filter applies tag:<typed>; Escape/Cancel
// restores the selector to whatever the active filter is.
function promptCustomTag(go) {
  const input = el('input', {type: 'text', class: 'tag-input', placeholder: 'tag name',
                             'aria-label': 'tag name'});
  const restore = () => filters.applyTags(state.tags);
  const close = () => { modal.remove(); restore(); };
  const apply = () => {
    const tag = input.value.trim();
    modal.remove();
    if (tag) go('tag:' + tag); else restore();
  };
  const modal = el('div', {class: 'confirm-modal', onclick: close},
    el('div', {class: 'confirm-card', onclick: (e) => e.stopPropagation()},
      el('h3', {text: 'Filter by tag'}),
      input,
      el('div', {class: 'row'},
        el('button', {class: 'primary', text: 'Filter', onclick: apply}),
        el('button', {text: 'Cancel', onclick: close}))));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); apply(); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
  });
  document.body.append(modal);
  input.focus();
}

function renderHeader(route) {
  const browsing = route.page === 'browse';
  const newHeader = el('header', {class: 'bar'},
    el('a', {class: 'title', href: '#/', text: 'Photos'}),
    browsing ? crumbs(route) : el('div', {class: 'crumbs'}, el('a', {href: href({}), text: '← folders'})),
    el('div', {class: 'spacer'}),
    browsing ? filterRow(route) : null,
    browsing ? select(SORTS, route.sort,
      (v) => { history.replaceState(null, '', href({...route, sort: v, photo: null})); render(); },
      'sort') : null,
    browsing ? el('button', {
      class: route.recursive ? 'on' : '',
      title: 'toggle whether the grid includes subfolders',
      text: route.recursive ? 'This folder + subfolders' : 'This folder',
      onclick: () => {
        history.replaceState(null, '', href({...route, recursive: !route.recursive, photo: null}));
        render();
      },
    }) : null,
    browsing ? el('button', {class: state.selecting ? 'on' : '', text: 'Select', onclick: toggleSelecting}) : null,
    // ticket 089: exports the selection if any, else everything currently in view (recursive-aware
    // via route.dir/route.recursive, same scope grid.loadFolder already used to populate it).
    browsing ? el('button', {text: 'Export', title: 'export photos to a folder',
                             onclick: () => exportAction.open()}) : null,
    // ticket 180: AI rating of the selection, else everything in view (same scope as Export).
    browsing ? el('button', {text: 'AI Rate', title: 'rate the selected (or all) photos with Gemini',
                             onclick: () => aiRate.start()}) : null,
    // Ticket 155: only offered for the unfiltered view -- the whole folder moves regardless of
    // any active rating/tag filter, so showing this under a filter would be misleading.
    browsing && route.filter === 'all' && route.dir !== '.'
      ? el('button', {text: 'Rename', title: 'rename this folder',
                      onclick: () => renameAction.open()}) : null,
    browsing && route.filter === 'rejected'
      ? el('a', {class: 'danger', href: hrefPage('delete-review', route.dir, route.recursive),
                text: 'Delete', title: 'review and move these rejected photos to trash'}) : null,
    el('button', {text: 'Rescan', title: 'rescan this folder', onclick: () => rescan(route)}),
    el('nav', {},
      el('a', {href: '#!activity', text: 'Activity'}), el('a', {href: '#!attention', text: 'Attention'}),
      el('a', {href: '#!usage', text: 'Thumbnails'}), el('a', {href: '#!jobs', text: 'Jobs'})));
  header ? header.replaceWith(newHeader) : app.prepend(newHeader);
  header = newHeader;
  if (browsing) { filters.applyCounts(state.counts); filters.applyTags(state.tags); }
}

function toggleSelecting() {
  state.selecting = !state.selecting;
  document.getElementById('grid')?.classList.toggle('selecting', state.selecting);
  renderHeader(state.route);
  if (!state.selecting) grid.clearSelection();
}

// ticket 140: progress is reported on the Jobs page (auto-updating there), not polled into a
// header status span on whatever page the button happened to be clicked from.
async function rescan(route) {
  try {
    const q = route.page === 'browse'
      ? '?dir=' + encodeURIComponent(route.dir) + '&recursive=' + (route.recursive ? '1' : '0')
      : '';
    const r = await post('/api/scan' + q);
    toast(r.started ? 'rescan started — see the Jobs page for progress' : 'a scan is already running');
  } catch (e) { toast(e.message, true); }
}

let previous = null;

async function render() {
  const route = parse();
  if (route.page !== 'browse') {
    loupe.close();
    previous = null;
    state.route = route;
    renderHeader(route);
    main ??= el('main');
    app.append(main);
    main.replaceChildren(el('p', {class: 'status', text: 'Loading…'}));
    try { await (PAGES[route.page] || (() => { throw new Error('unknown page'); }))(main); }
    catch (e) { main.replaceChildren(el('p', {class: 'status bad', text: e.message})); }
    return;
  }

  const sameFolder = previous && previous.dir === route.dir && previous.filter === route.filter &&
                     previous.sort === route.sort && previous.recursive === route.recursive &&
                     state.route && state.route.page === 'browse';
  if (!sameFolder) {
    loupe.close();
    state.route = route;
    renderHeader(route);
    main ??= el('main');
    app.append(main);
    main.replaceChildren(el('p', {class: 'status', text: 'Loading…'}));
    try {
      await grid.loadFolder(route);
    } catch (e) {
      main.replaceChildren(el('p', {class: 'status bad', text: e.message}));
      return;
    }
    grid.renderFolder(main);
    filters.applyCounts(state.counts);
    filters.applyTags(state.tags);
    document.title = (route.dir === '.' ? 'Photos' : route.dir.split('/').pop()) + ' — Photos';
  } else {
    state.route.photo = route.photo;
  }
  previous = {dir: route.dir, filter: route.filter, sort: route.sort, recursive: route.recursive};

  if (route.photo) {
    // Open at once if the photo is on the first page; otherwise (a direct link deep into a big
    // folder) wait for the rest of the list.
    if (!state.photos.some((p) => p.id === route.photo)) await grid.loadRest(state.route);
    else grid.loadRest(state.route);
    if (loupe.isOpen()) loupe.showById(route.photo); else loupe.open(route.photo);
  } else {
    loupe.close();
  }
}

document.addEventListener('keydown', (e) => {
  if (loupe.isOpen() || isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
  const shortcut = e.shiftKey ? filters.shortcutFor(e.code) : null;
  if (shortcut && state.route && state.route.page === 'browse') {
    history.replaceState(null, '', href({...state.route, filter: shortcut, photo: null}));
    render();
    e.preventDefault();
  } else if (e.key === 'u' || e.key === 'U') { loupe.undo(); e.preventDefault(); }
  else if (e.key === 'Escape' && state.selected.size) { grid.clearSelection(); e.preventDefault(); }
});

// Help overlay (ticket 091): a third, always-on listener -- works from both the browse view and
// the loupe, so it is not gated on loupe.isOpen() the way the listener above is. Registered after
// it, so an Escape that closes the help overlay must stopImmediatePropagation to keep the other
// two document-level keydown listeners (this one's own selection-clearing above, and loupe.js's
// onKey while the loupe is open) from also reacting to the same keypress.
document.addEventListener('keydown', (e) => {
  if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
  if (help.isOpen()) {
    if (e.key === 'Escape') { help.close(); e.preventDefault(); e.stopImmediatePropagation(); }
    return;
  }
  if (document.querySelector('.confirm-modal')) return;   // another modal (e.g. delete-file) is up
  if (e.key === 'h' || e.key === 'H' || e.key === '?' || e.key === 'F1') {
    help.open();
    e.preventDefault();
    e.stopImmediatePropagation();
  }
});

// A request that failed where nobody was watching for it (network down, an unexpected error)
// still tells the user, instead of failing silently.
window.addEventListener('unhandledrejection', (e) => {
  const message = (e.reason && e.reason.message) || 'unexpected error';
  toast(message === 'Failed to fetch' ? 'cannot reach the server' : message, true);
});

window.addEventListener('hashchange', render);
// The rating presentation depends on server settings, so load them first.
get('/api/config').then(configure).catch(() => {}).finally(render);
