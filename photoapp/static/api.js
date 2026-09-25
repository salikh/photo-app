// Thin fetch wrappers. Errors carry the server's message; server errors (5xx, for example
// 'database busy') are also shown as a toast right here, so they cannot go unnoticed.

import {toast} from './util.js';

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (e) { /* not json */ }
    if (response.status >= 500) toast(detail, true);
    throw new Error(detail);
  }
  return response.json();
}

export const get = (path) => request(path);
export const post = (path, body) => request(path, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: body === undefined ? undefined : JSON.stringify(body),
});

// Ticket 119: a per-file revision the server bumps whenever a file's rendering changes
// (raw_settings/crop saved). Putting it in the /img URL means a browser that cached the old
// response (Cache-Control: max-age=3600) fetches the fresh thumbnail when it navigates back to a
// tuned photo, instead of serving the pre-tune bytes for an unchanged URL. The map is seeded from
// every photo/file payload that carries a revision, so it also survives a full page reload.
const revisions = new Map();

export function setRevision(fileId, rev) {
  if (rev != null) revisions.set(fileId, rev);
}

// Accept both a photo entry ({file_id, rev}) and a photo-detail file ({id, thumb_rev}).
export function seedRevisions(rows) {
  for (const r of rows || []) {
    const id = r.file_id != null ? r.file_id : r.id;
    const rev = r.rev != null ? r.rev : r.thumb_rev;
    if (id != null && rev != null) revisions.set(id, rev);
  }
}

export function imgUrl(size, fileId) {
  // Revision 0 is the initial (untuned) render, whose plain URL is already correct and cacheable;
  // only a bumped revision (a saved tune/crop) needs a distinct, cache-busting URL.
  const rev = revisions.get(fileId);
  return `/img/${size}/${fileId}` + (rev ? `?r=${rev}` : '');
}
