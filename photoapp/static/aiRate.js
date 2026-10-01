// "AI Rate" action (ticket 180): queue AI rating jobs for the current selection -- or, if nothing
// is selected, everything in the current view. Runs as background 'ai_rate' jobs (progress on the
// Jobs page). The server answers 400 with a readable message when no API key is configured.

import {post} from './api.js';
import {toast} from './util.js';
import {state} from './state.js';
import * as grid from './grid.js';

const CONFIRM_ABOVE = 200;   // each uncached picture is a paid request

export async function start() {
  if (!state.route) return;
  const selected = [...state.selected];
  if (!selected.length) await grid.loadRest(state.route);
  const ids = selected.length ? selected : state.photos.map((p) => p.id);
  if (!ids.length) { toast('nothing to rate'); return; }
  if (ids.length > CONFIRM_ABOVE &&
      !confirm(`Send up to ${ids.length} photos to Gemini for rating?`)) return;
  try {
    const r = await post('/api/ai/rate', {ids});
    toast(`AI rating queued for ${r.queued} photo(s) — see the Jobs page for progress`);
  } catch (e) {
    toast(e.message, true);   // e.g. no API key configured
  }
}
