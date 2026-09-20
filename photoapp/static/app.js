// Entry point: header, routing, keyboard outside the loupe, scan control.

import {get, post} from './api.js';
import {el, toast, isTyping} from './util.js';
import {parse, href} from './route.js';
import {state} from './state.js';
import * as grid from './grid.js';
import * as loupe from './loupe.js';
import * as pages from './pages.js';
import {configure} from './rating.js';

const app = document.getElementById('app');
const FILTERS = ['all', 'unrated', 'rejected', 'picked', 'rated', 'fav', 'conflict'];
const SORTS = [['date', 'date'], ['name', 'name']];
const PAGES = {activity: pages.activityPage, attention: pages.attentionPage,
               usage: pages.usagePage, jobs: pages.jobsPage};

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

function renderHeader(route) {
  const browsing = route.page === 'browse';
  const scanStatus = el('span', {class: 'dim', id: 'scan-status'});
  const newHeader = el('header', {class: 'bar'},
    el('a', {class: 'title', href: '#/', text: 'Photos'}),
    browsing ? crumbs(route) : el('div', {class: 'crumbs'}, el('a', {href: href({}), text: '← folders'})),
    el('div', {class: 'spacer'}),
    browsing ? select(FILTERS.map((f) => [f, f]), route.filter,
      (v) => { location.hash = href({...route, filter: v, photo: null}); }, 'filter') : null,
    browsing ? select(SORTS, route.sort,
      (v) => { location.hash = href({...route, sort: v, photo: null}); }, 'sort') : null,
    browsing ? el('button', {class: state.selecting ? 'on' : '', text: 'Select', onclick: toggleSelecting}) : null,
    el('button', {text: 'Rescan', title: 'rescan this folder', onclick: () => rescan(route)}),
    scanStatus,
    el('nav', {},
      el('a', {href: '#!activity', text: 'Activity'}), el('a', {href: '#!attention', text: 'Attention'}),
      el('a', {href: '#!usage', text: 'Thumbnails'}), el('a', {href: '#!jobs', text: 'Jobs'})));
  header ? header.replaceWith(newHeader) : app.prepend(newHeader);
  header = newHeader;
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
                     previous.sort === route.sort && state.route && state.route.page === 'browse';
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
    document.title = (route.dir === '.' ? 'Photos' : route.dir.split('/').pop()) + ' — Photos';
  } else {
    state.route.photo = route.photo;
  }
  previous = {dir: route.dir, filter: route.filter, sort: route.sort};

  if (route.photo) {
    await grid.loadRest(state.route);
    if (loupe.isOpen()) loupe.showById(route.photo); else loupe.open(route.photo);
  } else {
    loupe.close();
  }
}

document.addEventListener('keydown', (e) => {
  if (loupe.isOpen() || isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === 'u' || e.key === 'U') { loupe.undo(); e.preventDefault(); }
  else if (e.key === 'Escape' && state.selected.size) { grid.clearSelection(); e.preventDefault(); }
});

window.addEventListener('hashchange', render);
// The rating presentation depends on server settings, so load them first.
get('/api/config').then(configure).catch(() => {}).finally(render);
