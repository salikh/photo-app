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

const app = document.getElementById('app');
const SORTS = [['date', 'date'], ['name', 'name']];
const PAGES = {activity: pages.activityPage, attention: pages.attentionPage,
               usage: pages.usagePage, jobs: pages.jobsPage,
               'delete-review': pages.deleteReviewPage};

let header = null;
let main = null;
let scanTimer = null;

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
    filters.primary().map(([value, text, title]) => el('button', {
      title, class: route.filter === value ? 'on' : '', 'aria-pressed': route.filter === value,
      dataset: {filter: value}, onclick: () => go(value),
    }, el('span', {class: 'lbl', text}), el('span', {class: 'n'}))),
    el('select', {'aria-label': 'more filters', class: inMore ? 'on' : '', onchange: (e) => e.target.value && go(e.target.value)},
      el('option', {value: '', text: 'more\u2026'}),
      extra.map(([v, text]) => el('option', {value: v, text, selected: v === route.filter}))),
    // ticket 087: options are populated by filters.applyTags (state.tags, an open set) right
    // after this element is in the DOM, not built here from a fixed list like the row above.
    el('select', {'aria-label': 'tag filter', class: 'tag-filter', onchange: (e) => e.target.value && go(e.target.value)},
      el('option', {value: '', text: 'tag: \u2026'})));
}

function renderHeader(route) {
  const browsing = route.page === 'browse';
  const scanStatus = el('span', {class: 'dim', id: 'scan-status'});
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
    browsing && route.filter === 'rejected'
      ? el('a', {class: 'danger', href: hrefPage('delete-review', route.dir),
                text: 'Delete', title: 'review and move these rejected photos to trash'}) : null,
    el('button', {text: 'Rescan', title: 'rescan this folder', onclick: () => rescan(route)}),
    scanStatus,
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

async function rescan(route) {
  try {
    const r = await post('/api/scan' + (route.page === 'browse' && route.dir !== '.' ? '?dir=' + encodeURIComponent(route.dir) : ''));
    if (!r.started) toast('a scan is already running');
    pollScan();
  } catch (e) { toast(e.message, true); }
}

function pollScan() {
  clearTimeout(scanTimer);
  const tick = async () => {
    let s;
    try { s = await get('/api/scan/status'); } catch (e) { return; }
    const label = document.getElementById('scan-status');
    if (s.running) {
      if (label) label.textContent = `scanning… ${s.files_seen} files`;
      scanTimer = setTimeout(tick, 1500);
    } else {
      if (label) label.textContent = '';
      if (s.error) toast('scan failed: ' + s.error, true);
      else if (s.files_processed || s.sidecars_processed) { toast(`scan done: ${s.files_processed} files, ${s.sidecars_processed} sidecars`); state.route = null; render(); }
    }
  };
  tick();
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
