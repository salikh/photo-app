// Secondary pages: activity (with undo), needs attention, thumbnail usage, jobs.

import {get, post} from './api.js';
import {el, toast, fmtBytes} from './util.js';
import {href} from './route.js';

function table(headers, rows) {
  return el('div', {class: 'table-wrap'}, el('table', {},
    el('thead', {}, el('tr', {}, headers.map((h) => el('th', {text: h})))),
    el('tbody', {}, rows)));
}

function photoLink(path) {
  const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '.';
  return el('a', {href: href({dir}), text: path});
}

export async function activityPage(main) {
  const rows = await get('/api/activity?limit=200');
  const render = () => main.replaceChildren(
    el('h2', {text: 'Recent changes'}),
    rows.length ? table(['When', 'Photo', 'Change', 'Cause', ''], rows.map((r) => el('tr', {},
      el('td', {text: r.ts.replace('T', ' ')}),
      el('td', {}, r.path ? photoLink(r.path) : String(r.photo_id)),
      el('td', {text: `${r.field}: ${r.old ?? ''} → ${r.new ?? ''}`}),
      el('td', {text: r.cause + (r.batch_id ? ' (batch)' : '')}),
      el('td', {}, r.undone ? el('span', {class: 'dim', text: 'undone'}) :
        el('button', {text: 'Undo', onclick: async () => {
          try { await post(`/api/activity/${r.id}/undo`); r.undone = 1; toast('undone'); render(); }
          catch (e) { toast(e.message, true); }
        }}))))) : el('p', {class: 'status', text: 'Nothing yet.'}));
  render();
}

export async function attentionPage(main) {
  const a = await get('/api/attention');
  main.replaceChildren(
    el('h2', {text: `Sidecars that disagree (${a.conflicts.length})`}),
    a.conflicts.length ? table(['Photo', 'Rating shown'], a.conflicts.map((c) => el('tr', {},
      el('td', {}, photoLink(c.path)), el('td', {text: c.rating})))) : el('p', {class: 'status ok', text: 'None.'}),
    el('h2', {text: `Sidecars without a picture (${a.orphan_sidecars.length})`}),
    a.orphan_sidecars.length ? table(['Sidecar'], a.orphan_sidecars.map((p) => el('tr', {}, el('td', {}, photoLink(p)))))
      : el('p', {class: 'status ok', text: 'None.'}),
    el('h2', {text: `Ambiguous rating recoveries (${a.ambiguous_recovery.length})`}),
    a.ambiguous_recovery.length ? table(['Photo id', 'Why', 'Options'], a.ambiguous_recovery.map((r) => el('tr', {},
      el('td', {text: r.photo_id}), el('td', {text: r.reason}),
      el('td', {text: r.options.map((o) => `rating ${o.rating}${o.fav ? ' ♥' : ''}`).join(' / ')}))))
      : el('p', {class: 'status ok', text: 'None.'}));
}

export async function usagePage(main) {
  const u = await get('/api/thumbs/usage?lacking=true');
  main.replaceChildren(
    el('h2', {text: 'Thumbnails known to the app'}),
    table(['Size', 'Files', 'Disk', 'Files without this size'],
      Object.keys(u.usage).map((size) => el('tr', {},
        el('td', {text: size}), el('td', {text: u.usage[size].files}),
        el('td', {text: fmtBytes(u.usage[size].bytes)}), el('td', {text: u.lacking[size]})))),
    el('p', {class: 'status', text: 'Counts cover files the app has scanned; sizes are created on demand.'}));
}

export async function jobsPage(main) {
  const j = await get('/api/jobs');
  main.replaceChildren(
    el('h2', {text: 'Background jobs'}),
    el('p', {class: 'status', text: Object.entries(j.counts).map(([k, v]) => `${k}: ${v}`).join('   ') || 'No jobs.'}),
    j.jobs.length ? table(['#', 'Kind', 'File', 'State', 'Error'], j.jobs.map((x) => el('tr', {},
      el('td', {text: x.id}), el('td', {text: x.kind}), el('td', {text: x.file_id}),
      el('td', {class: x.state === 'failed' ? 'bad' : x.state === 'done' ? 'ok' : '', text: x.state}),
      el('td', {text: x.error || ''})))) : '');
}
