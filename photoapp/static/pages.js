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

// photoId (ticket 142) opens the loupe on that exact photo (matching loupe.js's "exported from"
// link), instead of just its folder -- omitted, existing callers are unaffected.
function photoLink(path, photoId) {
  const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '.';
  return el('a', {href: href({dir, photo: photoId}), text: path});
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
      el('td', {}, photoLink(c.path, c.id)), el('td', {text: c.rating})))) : el('p', {class: 'status ok', text: 'None.'}),
    el('h2', {text: `Sidecars behind a newer rating in the database (${a.sidecars_behind.length})`}),
    a.sidecars_behind.length ? table(['Photo', 'Rating shown'], a.sidecars_behind.map((c) => el('tr', {},
      el('td', {}, photoLink(c.path, c.id)), el('td', {text: c.rating})))) : el('p', {class: 'status ok', text: 'None.'}),
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

function fmtDuration(seconds) {
  if (seconds == null) return null;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

// ticket 113: one line saying whether a worker is busy, and what it is working on.
function workerStatusText(active) {
  if (!active || !active.busy) return 'Worker: Idle';
  const items = active.running.map((r) => {
    const what = r.path || (r.target ? `folder ${r.target}` : null) || `file #${r.file_id}`;
    const since = fmtDuration(r.duration_seconds);
    return `${r.kind} ${what}${since ? ` (${since})` : ''}`;
  });
  return `Worker: Active — ${items.join('; ')}`;
}

// ticket 140: the interactive rescan (app.state.scanner) reported on its own line, separate from
// the shared-queue Worker line above -- a real library's low-priority populate_thumb backlog can
// keep that line saying "Active" for a long time on its own, which would otherwise bury whether
// *this* rescan (the thing the user is actually watching) is still running.
function scanStatusText(scan) {
  if (!scan || !scan.running) return 'Scan: Idle';
  const where = scan.current_dir && scan.current_dir !== '.' ? ` ${scan.current_dir}` : '';
  const step = scan.steps_total ? `, step ${scan.steps_done}/${scan.steps_total}` : '';
  return `Scan: Running${where} — ${scan.files_processed}/${scan.files_seen} files${step}`;
}

// ticket 114: the per-kind job counts as a three-level grouped header -- queued/running, then
// done/failed for all time (effectively the retention window) and each shorter window.
function jobsTable(kinds, p) {
  const cell = (value, cls) => el('td', {class: cls || '', text: value || 0});
  const windowHeaders = ['Done', 'Failed', 'Done', 'Failed', 'Done', 'Failed', 'Done', 'Failed'];
  const head = el('thead', {},
    el('tr', {},
      el('th', {rowspan: 2, text: 'Kind'}),
      el('th', {rowspan: 2, text: 'Queued'}),
      el('th', {rowspan: 2, text: 'Running'}),
      el('th', {colspan: 2, text: 'Done / Failed (all)'}),
      el('th', {colspan: 2, text: 'Last day'}),
      el('th', {colspan: 2, text: 'Last hour'}),
      el('th', {colspan: 2, text: 'Last minute'})),
    el('tr', {}, windowHeaders.map((text) => el('th', {text}))));
  const rows = kinds.map((kind) => {
    const s = p.by_kind[kind];
    return el('tr', {},
      el('td', {text: kind}), cell(s.queued), cell(s.running),
      cell(s.done, 'ok'), cell(s.failed, 'bad'),
      cell(s.done_last_day), cell(s.failed_last_day),
      cell(s.done_last_hour), cell(s.failed_last_hour),
      cell(s.done_last_minute), cell(s.failed_last_minute));
  });
  return el('div', {class: 'table-wrap'}, el('table', {}, head, el('tbody', {}, rows)));
}

// ticket 140: the Jobs page is the one place worker status is shown, so it auto-refreshes rather
// than requiring a manual reload -- polls at the same 1.5s cadence the old per-page scan poller
// used. Stops once the user navigates to a different page: this is a single-page router with no
// per-page unmount hook, so "is state.route still 'jobs'?" (set by app.js's render() before a
// page's loader runs) is the existing way to detect that.
function renderJobs(main, j) {
  const p = j.progress;
  const kinds = Object.keys(p.by_kind).sort();
  main.replaceChildren(
    el('h2', {text: 'Background jobs'}),
    el('p', {class: 'status ' + (j.scan && j.scan.running ? 'ok' : ''), text: scanStatusText(j.scan)}),
    el('p', {class: 'status ' + (j.active && j.active.busy ? 'ok' : ''), text: workerStatusText(j.active)}),
    el('p', {class: 'status', text:
      `Total: ${p.total} (all time so far)   Incomplete: ${p.incomplete}`}),
    kinds.length ? jobsTable(kinds, p) : el('p', {class: 'status', text: 'No jobs.'}),
    j.jobs.length ? table(['#', 'Kind', 'File', 'State', 'Error'], j.jobs.map((x) => el('tr', {},
      el('td', {text: x.id}), el('td', {text: x.kind}),
      el('td', {}, x.path ? photoLink(x.path, x.photo_id) : (x.target || x.file_id || '')),
      el('td', {class: x.state === 'failed' ? 'bad' : x.state === 'done' ? 'ok' : '', text: x.state}),
      el('td', {text: x.error || ''})))) : '');
}

export async function jobsPage(main) {
  const tick = async () => {
    if (state.route.page !== 'jobs') return;
    let j;
    try { j = await get('/api/jobs'); } catch (e) { /* transient; retry next tick */ }
    // Re-check after the await: the user may have navigated to a different page while the
    // request was in flight, and main is now that other page's element (ticket 140).
    if (state.route.page !== 'jobs') return;
    if (j) renderJobs(main, j);
    setTimeout(tick, 1500);
  };
  await tick();
}

// ticket 072: review + confirm screen for moving a folder's rejected photos to Pictures/.trash.
// Scoped to state.route.dir (the folder the Delete button was clicked from), not the whole
// library, for a smaller blast radius per click.
// Two ways to reach this page (ticket 088): every rejected Photo in a dir (the header's own
// "Delete" link, dir taken from the route), or an explicit id list handed off from a grid
// selection (grid.js's deleteSelected, via state.deleteReview -- consumed once here, not kept in
// the URL, since selection itself is already session-only and doesn't survive a reload either).
const REVIEW_PAGE = window.__pageSize || 1000;   // (the override is a test hook, as in grid.js)

// ticket 124: the header flow must list every rejected Photo in scope, not just the first server
// page -- a whole-library "This folder + subfolders" view can hold far more than one page, and the
// Photos that fall past it (often the ones in deeper subfolders) silently vanished from the review.
async function allRejectedPhotos(dir, recursive) {
  const rec = recursive ? '&recursive=1' : '';
  const photos = [];
  let total = Infinity;
  while (photos.length < total) {
    const data = await get(`/api/photos?dir=${encodeURIComponent(dir)}&filter=rejected` +
                           `&offset=${photos.length}&limit=${REVIEW_PAGE}${rec}`);
    total = data.total;
    if (!data.photos.length) break;
    photos.push(...data.photos);
  }
  return photos;
}

export async function deleteReviewPage(main) {
  const review = state.deleteReview;
  state.deleteReview = null;
  const dir = state.route.dir || '.';
  const recursive = !!state.route.recursive;   // ticket 120: keep the "this folder + subfolders" mode
  const shown = (dir === '.' ? 'the root' : dir) + (recursive ? ' and subfolders' : '');

  let photos, backHref, backText, heading, emptyText, description;
  if (review) {
    photos = review.ids.map((id) => state.photos.find((p) => p.id === id)).filter(Boolean);
    backHref = href(review.from);
    backText = '← back';
    heading = 'Delete selected photos';
    emptyText = 'Nothing to delete.';
    description = `${photos.length} selected photo(s).`;
  } else {
    photos = await allRejectedPhotos(dir, recursive);
    backHref = href({dir, filter: 'rejected', recursive});
    backText = '← back to Rejected';
    heading = 'Delete rejected photos';
    emptyText = `No rejected photos in ${shown}.`;
    description = `${photos.length} rejected photo(s) in ${shown}.`;
  }
  const back = el('a', {href: backHref, text: backText});
  if (!photos.length) {
    main.replaceChildren(el('h2', {text: heading}), el('p', {class: 'status', text: emptyText}), back);
    return;
  }
  const confirmBtn = el('button', {class: 'danger', text: `Move ${photos.length} photo(s) to trash`,
    onclick: async (event) => {
      event.target.disabled = true;
      try {
        const result = await post('/api/photos/trash', {ids: photos.map((p) => p.id)});
        toast(`moved ${result.trashed.length} photo(s) to trash` +
              (result.errors.length ? `, ${result.errors.length} could not be moved` : ''));
        location.hash = backHref;
      } catch (e) {
        event.target.disabled = false;   // post() already toasted the error
      }
    }});
  main.replaceChildren(
    el('h2', {text: heading}),
    el('p', {class: 'status', text:
      `${description} Every DNG/JPG/XMP file of each moves to Pictures/.trash — nothing is ` +
      'permanently deleted yet. Scroll down to confirm.'}),
    back,
    el('div', {class: 'grid review-grid'}, photos.map((p) => el('div', {class: 'cell', title: p.name},
      el('img', {src: imgUrl('Medium', p.file_id), loading: 'lazy', alt: p.name, decoding: 'async'})))),
    el('div', {class: 'delete-confirm'}, confirmBtn));
}
