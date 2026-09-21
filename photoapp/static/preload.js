// Preloading for the viewer (ticket 050): the photos within two positions of the current one
// have their pictures loaded ahead of time, so moving to them (or zooming) shows the image
// at once.
//
//   window      i-2 .. i+2, the direction of travel first
//   order       Medium (what is shown on arrival) for all four, then Huge (the full-size
//               image used for zoom) at low priority
//   decoding    Medium is decoded ahead (about 12 MB each); Huge is only fetched into the
//               cache, except the next photo in the direction of travel (a decoded 16
//               megapixel image is about 64 MB, too much to hold four of)
//   data saver  no Huge preload on a data-saving or slow connection
//   dwelling    after a short pause on a photo (the user is probably about to zoom) its own
//               Huge is decoded ahead too, one at a time
//   fast moves  what leaves the window is cancelled and its memory released, so holding an
//               arrow key does not queue stale downloads

import {imgUrl} from './api.js';

export const WINDOW = 2;
const DWELL_MS = 400;

let dwellTimer = null;

const held = new Map();     // url -> Image. Keeping the object alive keeps the request/decoded data.

export function dataSaverOn() {
  const c = navigator.connection;
  return !!c && (c.saveData === true || /(^|-)2g$|^3g$/.test(c.effectiveType || ''));
}

// The pictures to have ready for a viewer at `index` moving in direction `delta` (+1/-1):
// [{url, huge, decode}] in the order they should be started.
export function plan(photos, index, delta, saver = dataSaverOn()) {
  const dir = delta < 0 ? -1 : 1;
  const offsets = [dir, -dir, 2 * dir, -2 * dir];
  const ready = offsets.map((o) => ({o, photo: photos[index + o]})).filter((x) => x.photo);
  const items = ready.map(({o, photo}) => ({url: imgUrl('Medium', photo.file_id), huge: false, decode: true, offset: o}));
  if (!saver) {
    for (const {o, photo} of ready) {
      items.push({url: imgUrl('Huge', photo.file_id), huge: true, decode: o === dir, offset: o});
    }
  }
  return items;
}

// Start what is missing, cancel what left the window. keepUrls (the current photo's own
// pictures) are never cancelled, the viewer's <img> shares those requests.
export function update(photos, index, delta, keepUrls = []) {
  const wanted = plan(photos, index, delta);
  const keep = new Set([...wanted.map((w) => w.url), ...keepUrls]);
  for (const [url, img] of held) {
    if (!keep.has(url)) {
      img.src = '';                          // aborts a download in progress
      held.delete(url);
    }
  }
  for (const w of wanted) {
    if (held.has(w.url)) continue;
    const img = new Image();
    if (w.huge) img.fetchPriority = 'low';
    img.decoding = 'async';
    img.src = w.url;
    if (w.decode && img.decode) img.decode().catch(() => { /* cancelled or failed: nothing to do */ });
    held.set(w.url, img);
  }
}

// After the viewer rests on a photo, decode its full-size image so a zoom is instant. Only the
// current photo's, replaced on every move, so at most one such image is decoded on purpose.
export function decodeCurrentHuge(photo) {
  clearTimeout(dwellTimer);
  if (dataSaverOn() || !photo) return;
  dwellTimer = setTimeout(() => {
    const url = imgUrl('Huge', photo.file_id);
    let img = held.get(url);
    if (!img) {
      img = new Image();
      img.fetchPriority = 'low';
      img.src = url;
      held.set(url, img);
    }
    if (img.decode) img.decode().catch(() => { /* cancelled or failed */ });
  }, DWELL_MS);
}

export function clear() {
  clearTimeout(dwellTimer);
  for (const img of held.values()) img.src = '';
  held.clear();
}

export function urls() { return [...held.keys()]; }
