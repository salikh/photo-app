// Export action (ticket 089): write a Huge-equivalent JPEG of the current selection -- or, if
// nothing is selected, everything in the current view -- to a folder the user confirms. Runs as
// background jobs (see photoapp.jobs/export.py); progress shows on the Jobs page like any other
// job kind, with no export-specific frontend needed there.

import {get, post} from './api.js';
import {el, toast} from './util.js';
import {state} from './state.js';
import * as grid from './grid.js';

let modal = null;

function close() {
  if (modal) { modal.remove(); modal = null; }
}

export async function open(ids) {
  if (modal || !state.route) return;
  const route = state.route;
  if (!ids) {
    const selected = [...state.selected];
    if (!selected.length) {
      // "the whole view": make sure every page is loaded, not just what has scrolled into view
      await grid.loadRest(route);
    }
    // epic 205: an uploaded album follows the grid's order, not the order of clicking
    ids = selected.length ? state.photos.filter((p) => state.selected.has(p.id)).map((p) => p.id)
                          : state.photos.map((p) => p.id);
    if (selected.length && !ids.length) ids = selected;   // selected photos not in the loaded grid
  }
  if (!ids.length) { toast('nothing to export'); return; }

  let target = '', defaultAlbum = '';
  try {
    const d = await get('/api/export/default_path?dir=' + encodeURIComponent(route.dir));
    target = d.path;
    defaultAlbum = d.album || '';
  } catch (e) { /* leave blank -- the user can still type one */ }
  let gstatus = {configured: false, hint: null};
  try { gstatus = await get('/api/gphotos/status'); } catch (e) { /* offer nothing */ }
  if (modal) return;   // a second Export click while the fetch above was pending

  const up = googlePhotosSection(gstatus, defaultAlbum);

  const input = el('input', {type: 'text', value: target, class: 'export-target',
                             'aria-label': 'target folder'});
  // Ticket 208: never remembered, unchecked every time the dialog opens.
  const gps = el('input', {type: 'checkbox', class: 'export-gps', 'aria-label': 'include GPS'});
  const confirmBtn = el('button', {class: 'danger', text: `Export ${ids.length} photo(s)`,
    onclick: async () => {
      confirmBtn.disabled = true;
      try {
        const r = await post('/api/export', {ids, dir: route.dir, target: input.value,
                                          include_gps: gps.checked, gphotos: up.request()});
        toast(`queued ${r.queued.length} for export` + (r.upload_job ? ' and upload to Google Photos' : '') +
              (r.missing.length ? `, ${r.missing.length} could not be found` : '') +
              ' — see the Jobs page for progress');
        close();
      } catch (e) {
        // Unlike most POSTs here, a bad target (e.g. inside pictures_dir) is a routine 400 the
        // user typed their way into, not just a 5xx api.js already toasts -- show it explicitly.
        toast(e.message, true);
        confirmBtn.disabled = false;
      }
    }});
  modal = el('div', {class: 'confirm-modal', onclick: close},
    el('div', {class: 'confirm-card', onclick: (e) => e.stopPropagation()},
      el('h3', {text: `Export ${ids.length} photo(s)`}),
      el('p', {class: 'meta', text:
        'Writes a full-size JPEG of each (videos: the original) to the folder below (subfolders are mirrored so ' +
        'same-named photos from different folders don’t collide).'}),
      el('label', {class: 'export-label'}, 'Target folder', input),
      el('label', {class: 'export-label'}, gps, ' Include GPS location in the exported files'),
      up.node,
      el('div', {class: 'row'},
        el('button', {text: 'Cancel', onclick: close}),
        confirmBtn)));
  document.body.append(modal);
  input.focus();
  input.select();
}


// The "Also upload to Google Photos" part of the dialog (epic 205, tickets 213/214). request()
// returns the `gphotos` field of POST /api/export, or null when the box is unticked.
function googlePhotosSection(status, defaultName) {
  const box = el('input', {type: 'checkbox', class: 'export-gphotos', 'aria-label': 'upload to Google Photos'});
  const name = el('input', {type: 'text', value: defaultName, class: 'export-album-name',
                            'aria-label': 'album name'});
  const mode = {value: 'new'};
  const newRadio = el('input', {type: 'radio', name: 'album-mode', checked: true,
                                'aria-label': 'new album', onchange: () => { mode.value = 'new'; draw(); }});
  const oldRadio = el('input', {type: 'radio', name: 'album-mode',
                                'aria-label': 'add to an existing album',
                                onchange: () => { mode.value = 'existing'; draw(); }});
  const pick = el('select', {class: 'export-album-pick', 'aria-label': 'existing album'});
  const warn = el('div', {class: 'export-album-warn'});
  const body = el('div', {class: 'export-gphotos-body', hidden: true});
  let albums = null, timer = null, loading = false, failure = null, seq = 0, syncedAt = null;
  const refreshBtn = el('button', {class: 'export-album-refresh', text: 'Refresh from Google',
    title: 'Ask Google Photos for its list of this app\'s albums (takes about 15 s)',
    onclick: (e) => { e.preventDefault(); refresh(true); }});

  // Ticket 232: the list comes from the app's own table of albums it uploaded to (instant, no
  // Google call). Only the button asks Google, in the background (~15 s); poll the *local* server
  // while that runs.
  async function refresh(fromGoogle = false) {
    const mine = ++seq;
    try {
      const url = '/api/gphotos/albums' + (fromGoogle ? '/refresh' : '') +
                  '?name=' + encodeURIComponent(name.value);
      const res = fromGoogle ? await post(url, {}) : await get(url);
      if (mine !== seq) return;           // a newer request (typing) superseded this one
      albums = res.albums; loading = res.loading; failure = res.error; syncedAt = res.synced_at;
    } catch (e) { albums = []; loading = false; failure = e.message; toast(e.message, true); }
    pick.replaceChildren(...(albums.length ? albums.map((a) => el('option', {value: a.id,
      text: `${a.title} (${a.count})` + (a.exact ? ' — same name' : '')}))
      : [el('option', {value: '', disabled: true, selected: true,
          text: loading ? 'Asking Google Photos… (about 15 s)'
              : failure ? `Could not list albums: ${failure}`
              : 'No albums yet — albums you upload to are remembered; "Refresh from Google" finds older ones'})]));
    refreshBtn.disabled = loading;
    draw();
    if (loading) { clearTimeout(timer); timer = setTimeout(() => refresh(false), 1500); }
  }
  function draw() {
    pick.hidden = mode.value !== 'existing';
    name.hidden = mode.value !== 'new';
    const same = albums && albums.find((a) => a.exact);
    warn.replaceChildren();
    if (mode.value === 'new' && same) {
      warn.append(el('span', {text: `An album named “${same.title}” already exists (made by this app). `}),
        el('button', {text: 'Add to it instead', onclick: () => {
          pick.value = same.id; oldRadio.checked = true; mode.value = 'existing'; draw();
        }}),
        el('span', {class: 'meta', text: ' or keep going to create another with the same name.'}));
    }
  }
  name.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(refresh, 300); });
  box.addEventListener('change', () => {
    body.hidden = !box.checked;
    if (box.checked && albums === null) refresh();
  });
  box.disabled = !status.configured;
  body.append(
    el('label', {class: 'export-label'}, newRadio, ' New album ', name),
    el('label', {class: 'export-label'}, oldRadio, ' Add to an existing album ', pick),
    refreshBtn,
    warn,
    el('p', {class: 'meta', text: 'Lists the albums this app uploaded to (Google only shows an app ' +
                                  'its own albums). Uploads count ' +
                                  'against your Google storage.'}));
  draw();
  const node = el('div', {class: 'export-gphotos-section'},
    el('label', {class: 'export-label'}, box, ' Also upload to Google Photos ',
      status.configured ? null
        : el('span', {class: 'meta', text: `(not set up: ${status.hint || 'unavailable'})`})),
    body);
  return {node, request: () => {
    if (!box.checked) return null;
    return mode.value === 'existing' ? {album_id: pick.value} : {album_title: name.value};
  }};
}
