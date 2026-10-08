// Folder rename action (ticket 155): rename the whole current folder in place. Runs synchronously
// server-side (POST /api/dirs/rename), then a scan_dir job for both the old and new directory
// shows progress on the Jobs page like any other job kind. Unlike move.js's "Move to folder"
// (which offers to create a missing destination), an already-existing target is refused outright
// -- there is no per-file merge story for a whole-directory rename.

import {get, post} from './api.js';
import {el, toast} from './util.js';
import {href} from './route.js';
import {state} from './state.js';

let modal = null;

function close() {
  if (modal) { modal.remove(); modal = null; }
}

async function commit(oldDir, newDir) {
  try {
    const r = await post('/api/dirs/rename', {from: oldDir, to: newDir});
    close();
    toast(`renamed to ${newDir} (${r.files_moved} file(s)) — see the Jobs page for progress`);
    // The folder being browsed no longer exists at its old path -- follow it to the new one,
    // keeping the current filter/sort (same reasoning as ticket 154's directory link).
    location.hash = href({dir: newDir, filter: state.route.filter, tag: state.route.tag, sort: state.route.sort});
  } catch (e) {
    toast(e.message, true);
  }
}

// The second modal: a plain confirmation once the target is known not to exist.
function confirmRename(oldDir, newDir) {
  close();
  const confirmBtn = el('button', {class: 'danger', text: 'Rename', onclick: () => {
    confirmBtn.disabled = true;
    commit(oldDir, newDir);
  }});
  modal = el('div', {class: 'confirm-modal', onclick: close},
    el('div', {class: 'confirm-card', onclick: (e) => e.stopPropagation()},
      el('h3', {text: 'Rename this folder?'}),
      el('p', {class: 'meta', text: `"${oldDir}" → "${newDir}"`}),
      el('div', {class: 'row'},
        el('button', {text: 'Cancel', onclick: close}),
        confirmBtn)));
  document.body.append(modal);
}

export async function open() {
  if (modal || !state.route || state.route.page !== 'browse') return;
  const oldDir = state.route.dir;

  const input = el('input', {type: 'text', value: oldDir, class: 'export-target',
                             'aria-label': 'new folder path'});
  const confirmBtn = el('button', {class: 'danger', text: 'Rename', onclick: async () => {
    const newDir = input.value.trim();
    if (!newDir || newDir === oldDir) return;
    confirmBtn.disabled = true;
    try {
      const exists = (await get('/api/dirs/exists?dir=' + encodeURIComponent(newDir))).exists;
      if (exists) { toast(`"${newDir}" already exists`, true); return; }
      confirmRename(oldDir, newDir);
    } catch (e) {
      toast(e.message, true);
    } finally {
      confirmBtn.disabled = false;
    }
  }});
  modal = el('div', {class: 'confirm-modal', onclick: close},
    el('div', {class: 'confirm-card', onclick: (e) => e.stopPropagation()},
      el('h3', {text: 'Rename folder'}),
      el('label', {class: 'export-label'}, 'New folder path', input),
      el('div', {class: 'row'},
        el('button', {text: 'Cancel', onclick: close}),
        confirmBtn)));
  document.body.append(modal);
  input.focus();
  input.select();
}
