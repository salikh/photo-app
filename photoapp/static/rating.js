// The combined rating: -1 reject, 0 unrated, 1..5 stars (see photoapp/ratings.py).
//
// With the server flag --one_star_is_unrated (ticket 049) a 1-star rating is
// only *shown* as unrated (darktable writes 1 star on import), so the UI never
// offers 1 as a step or a button. The data is unchanged.

export const REJECT = -1;

let oneStarIsUnrated = false;
export function configure(config) { oneStarIsUnrated = !!config.one_star_is_unrated; }
export function isOneStarUnrated() { return oneStarIsUnrated; }

export function clamp(n) { return Math.max(-1, Math.min(5, n)); }

// What the user sees: 1 counts as unrated when the flag is on.
export function display(rating) { return oneStarIsUnrated && rating === 1 ? 0 : rating; }

// Ratings the UI offers as buttons.
export function choices() { return oneStarIsUnrated ? [0, 2, 3, 4, 5] : [0, 1, 2, 3, 4, 5]; }

// One step along -1, 0, 1..5 (swipe up/down); with the flag the step over
// "unrated" skips 1. Un-reject restores the star count the server remembered.
export function step(current, delta, previousStars) {
  if (current === REJECT && delta > 0 && previousStars) return clamp(previousStars);
  const from = display(current);
  let next = clamp(from + (delta > 0 ? 1 : -1));
  if (oneStarIsUnrated && next === 1) next = delta > 0 ? 2 : 0;
  return next;
}

export function afterKey(current, key, previousStars) {
  if (/^[0-5]$/.test(key)) return oneStarIsUnrated && key === '1' ? 0 : Number(key);
  if (key === 'x' || key === 'X') return current === REJECT ? clamp(previousStars || 0) : REJECT;
  return null;
}

export function label(rating) {
  const shown = display(rating);
  if (shown === REJECT) return '✖ reject';
  return '★'.repeat(shown) + '☆'.repeat(5 - shown);
}
