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
    ids = selected.length ? selected : state.photos.map((p) => p.id);
  }
  if (!ids.length) { toast('nothing to export'); return; }

  let target = '';
  try {
    target = (await get('/api/export/default_path?dir=' + encodeURIComponent(route.dir))).path;
  } catch (e) { /* leave blank -- the user can still type one */ }
  if (modal) return;   // a second Export click while the fetch above was pending

  const input = el('input', {type: 'text', value: target, class: 'export-target',
                             'aria-label': 'target folder'});
  // Ticket 208: never remembered, unchecked every time the dialog opens.
  const gps = el('input', {type: 'checkbox', class: 'export-gps', 'aria-label': 'include GPS'});
  const confirmBtn = el('button', {class: 'danger', text: `Export ${ids.length} photo(s)`,
    onclick: async () => {
      confirmBtn.disabled = true;
      try {
        const r = await post('/api/export', {ids, dir: route.dir, target: input.value,
                                          include_gps: gps.checked});
        toast(`queued ${r.queued.length} for export` +
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
      el('label', {class: 'export-label'}, gps, ' Include GPS location in the exported files ',
        el('span', {class: 'meta', text: '(off unless ticked, every time)'})),
      el('div', {class: 'row'},
        el('button', {text: 'Cancel', onclick: close}),
        confirmBtn)));
  document.body.append(modal);
  input.focus();
  input.select();
}
