// Ticket 250: the loupe's "stabilize" button for videos -- a small popover asking how strongly to
// stabilize, then queueing the job (ticket 249). The server owns the presets (/api/stabilize/levels);
// more explicit parameters are added to EXTRA_FIELDS and travel as `options` in the request.

import {get, post} from './api.js';
import {el, toast, placeNear} from './util.js';

// {key, label, type: 'number' | 'select', hint, min, max, choices: [..], value}: rendered under the
// level choice; a field left empty is not sent, so the preset's own value applies.
const EXTRA_FIELDS = [];

let modal = null;

export function isOpen() { return !!modal; }

export function close() {
  if (modal) { modal.remove(); modal = null; }
}

export async function open(photo, anchor) {
  close();
  let info;
  try { info = await get('/api/stabilize/levels'); } catch (e) { toast(e.message, true); return; }
  if (!info.available) { toast('this ffmpeg has no libvidstab/libx264: stabilization is not available', true); return; }
  const fields = new Map();
  const radios = info.levels.map(({level, description}) => el('label', {class: 'stabilize-level', title: description},
    el('input', {type: 'radio', name: 'stabilize-level', value: level, checked: level === info.default}),
    el('span', {class: 'label', text: level[0].toUpperCase() + level.slice(1)}),
    el('span', {class: 'hint', text: LEVEL_HINTS[level] || ''})));
  const extras = EXTRA_FIELDS.map((f) => {
    const input = f.type === 'select'
      ? el('select', {}, el('option', {value: '', text: 'preset'}), f.choices.map((c) => el('option', {value: c, text: c})))
      : el('input', {type: 'number', min: f.min, max: f.max, placeholder: 'preset'});
    fields.set(f.key, {input, type: f.type});
    return el('label', {class: 'export-label'}, f.label, input);
  });
  const confirm = async () => {
    const level = modal.querySelector('input[name=stabilize-level]:checked').value;
    const options = {};
    for (const [key, {input, type}] of fields) {
      if (input.value !== '') options[key] = type === 'number' ? Number(input.value) : input.value;
    }
    try {
      const r = await post(`/api/photos/${photo.id}/stabilize`, {level, options});
      toast(`stabilization (${level}) queued; the result appears as a second file of this video`);
      close();
      return r;
    } catch (e) { toast(e.message, true); }
  };
  modal = el('div', {class: 'confirm-modal actions-modal stabilize-modal', onclick: close},
    el('div', {class: 'confirm-card', role: 'dialog', onclick: (e) => e.stopPropagation()},
      el('h3', {text: 'Stabilize video'}),
      radios, extras,
      el('div', {class: 'meta', text: 'Runs in the background at medium priority; the result is saved next to the original as a second file.'}),
      el('div', {class: 'row'},
        el('button', {text: 'Cancel', onclick: close}),
        el('button', {class: 'primary stabilize-go', text: 'Stabilize', onclick: confirm}))));
  document.body.append(modal);
  if (anchor) placeNear(modal.querySelector('.confirm-card'), anchor);
  modal.querySelector('.stabilize-go').focus();
}

const LEVEL_HINTS = {
  weak: 'light smoothing, keeps deliberate camera moves',
  strong: 'heavy smoothing, for handheld walking shots (default)',
};
