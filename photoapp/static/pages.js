// Secondary pages: activity (with undo), needs attention, thumbnail usage, jobs, delete review.

import {get, post, imgUrl} from './api.js';
import {el, toast, fmtBytes} from './util.js';
import {href} from './route.js';
import {state} from './state.js';

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
    el('h2', {text: `Sidecars behind a newer rating in the database (${a.sidecars_behind.length})`}),
    a.sidecars_behind.length ? table(['Photo', 'Rating shown'], a.sidecars_behind.map((c) => el('tr', {},
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

function completedLine(label, text, completed) {
  const c = completed[label];
  return `${text}: ${c.done} done, ${c.failed} failed`;
}

export async function jobsPage(main) {
  const j = await get('/api/jobs');
  const p = j.progress;
  const kinds = Object.keys(p.by_kind).sort();
  main.replaceChildren(
    el('h2', {text: 'Background jobs'}),
    el('p', {class: 'status', text:
      `Total: ${p.total} (all time so far)   Incomplete: ${p.incomplete}`}),
    el('p', {class: 'status', text: [
      completedLine('last_minute', 'Last minute', p.completed),
      completedLine('last_hour', 'Last hour', p.completed),
      completedLine('last_day', 'Last day', p.completed),
    ].join('   ')}),
    kinds.length ? table(['Kind', 'Queued', 'Running', 'Done', 'Failed'],
      kinds.map((kind) => {
        const s = p.by_kind[kind];
        return el('tr', {},
          el('td', {text: kind}), el('td', {text: s.queued || 0}),
          el('td', {text: s.running || 0}), el('td', {class: 'ok', text: s.done || 0}),
          el('td', {class: 'bad', text: s.failed || 0}));
      })) : el('p', {class: 'status', text: 'No jobs.'}),
    j.jobs.length ? table(['#', 'Kind', 'File', 'State', 'Error'], j.jobs.map((x) => el('tr', {},
      el('td', {text: x.id}), el('td', {text: x.kind}), el('td', {text: x.file_id}),
      el('td', {class: x.state === 'failed' ? 'bad' : x.state === 'done' ? 'ok' : '', text: x.state}),
      el('td', {text: x.error || ''})))) : '');
}

// ticket 072: review + confirm screen for moving a folder's rejected photos to Pictures/.trash.
// Scoped to state.route.dir (the folder the Delete button was clicked from), not the whole
// library, for a smaller blast radius per click.
export async function deleteReviewPage(main) {
  const dir = state.route.dir || '.';
  const shown = dir === '.' ? 'the root' : dir;
  const back = el('a', {href: href({dir, filter: 'rejected'}), text: '← back to Rejected'});
  const data = await get(`/api/photos?dir=${encodeURIComponent(dir)}&filter=rejected&limit=1000`);
  const photos = data.photos;
  if (!photos.length) {
    main.replaceChildren(
      el('h2', {text: 'Delete rejected photos'}),
      el('p', {class: 'status', text: `No rejected photos in ${shown}.`}), back);
    return;
  }
  const confirmBtn = el('button', {class: 'danger', text: `Move ${photos.length} photo(s) to trash`,
    onclick: async (event) => {
      event.target.disabled = true;
      try {
        const result = await post('/api/photos/trash', {ids: photos.map((p) => p.id)});
        toast(`moved ${result.trashed.length} photo(s) to trash` +
              (result.errors.length ? `, ${result.errors.length} could not be moved` : ''));
        location.hash = href({dir, filter: 'rejected'});
      } catch (e) {
        event.target.disabled = false;   // post() already toasted the error
      }
    }});
  main.replaceChildren(
    el('h2', {text: 'Delete rejected photos'}),
    el('p', {class: 'status', text:
      `${photos.length} rejected photo(s) in ${shown}. Every DNG/JPG/XMP file of each moves to ` +
      'Pictures/.trash — nothing is permanently deleted yet. Scroll down to confirm.'}),
    back,
    el('div', {class: 'grid review-grid'}, photos.map((p) => el('div', {class: 'cell', title: p.name},
      el('img', {src: imgUrl('Medium', p.file_id), loading: 'lazy', alt: p.name, decoding: 'async'})))),
    el('div', {class: 'delete-confirm'}, confirmBtn));
}
