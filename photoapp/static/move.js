// Move-to-folder action (ticket 144/147): relocate the selected Photos' files (and sidecars) to
// another folder inside the library. Runs synchronously server-side (POST /api/move), then a
// scan_dir job per touched directory (and a link_exports job for an Exported/ destination) shows
// progress on the Jobs page like any other job kind -- no move-specific frontend needed there.

import {get, post} from './api.js';
import {el, toast} from './util.js';
import {state} from './state.js';

let modal = null;

function close() {
  if (modal) { modal.remove(); modal = null; }
}

// removePhotos/clearSelection are grid.js's own -- passed in by the caller (ticket 147) rather
// than imported here, since the "Move to folder" button lives in grid.js's own selection bar and
// grid.js -> move.js -> grid.js would be a circular import.
async function commit(ids, target, removePhotos, clearSelection) {
  try {
    const r = await post('/api/move', {ids, target});
    close();
    // m.moved can be empty for a Photo whose files were already in the target folder (a no-op,
    // e.g. the modal's input was left at the current folder) -- nothing to remove from view then.
    const movedPhotos = r.moved
      .filter((m) => m.moved.length)
      .map((m) => state.photos.find((p) => p.id === m.photo_id))
      .filter(Boolean);
    if (movedPhotos.length) removePhotos(movedPhotos);
    clearSelection();
    toast(`moved ${r.moved.length}` + (r.errors.length ? `, ${r.errors.length} failed` : '') +
          ' — see the Jobs page for progress');
  } catch (e) {
    toast(e.message, true);
  }
}

// The second modal: the typed folder doesn't exist yet, confirm creating it.
function confirmCreate(ids, target, removePhotos, clearSelection) {
  close();
  const confirmBtn = el('button', {class: 'danger', text: 'Create and move', onclick: () => {
    confirmBtn.disabled = true;
    commit(ids, target, removePhotos, clearSelection);
  }});
  modal = el('div', {class: 'confirm-modal', onclick: close},
    el('div', {class: 'confirm-card', onclick: (e) => e.stopPropagation()},
      el('h3', {text: 'Create this folder?'}),
      el('p', {class: 'meta', text: `"${target}" doesn't exist yet.`}),
      el('div', {class: 'row'},
        el('button', {text: 'Cancel', onclick: close}),
        confirmBtn)));
  document.body.append(modal);
}

export async function open(ids, removePhotos, clearSelection) {
  if (modal || !ids.length || !state.route) return;

  const input = el('input', {type: 'text', value: state.route.dir, class: 'export-target',
                             'aria-label': 'target folder'});
  const confirmBtn = el('button', {class: 'danger', text: `Move ${ids.length} photo(s)`,
    onclick: async () => {
      const target = input.value.trim();
      if (!target) return;
      confirmBtn.disabled = true;
      try {
        const exists = (await get('/api/dirs/exists?dir=' + encodeURIComponent(target))).exists;
        if (exists) { await commit(ids, target, removePhotos, clearSelection); return; }
        confirmCreate(ids, target, removePhotos, clearSelection);
      } catch (e) {
        toast(e.message, true);
      } finally {
        confirmBtn.disabled = false;
      }
    }});
  modal = el('div', {class: 'confirm-modal', onclick: close},
    el('div', {class: 'confirm-card', onclick: (e) => e.stopPropagation()},
      el('h3', {text: `Move ${ids.length} photo(s)`}),
      el('label', {class: 'export-label'}, 'Target folder', input),
      el('div', {class: 'row'},
        el('button', {text: 'Cancel', onclick: close}),
        confirmBtn)));
  document.body.append(modal);
  input.focus();
  input.select();
}
