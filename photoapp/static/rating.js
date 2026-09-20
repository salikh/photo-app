// The combined rating: -1 reject, 0 unrated, 1..5 stars (see photoapp/ratings.py).

export const REJECT = -1;

export function clamp(n) { return Math.max(-1, Math.min(5, n)); }

// One step along -1, 0, 1..5 (swipe up/down). Un-reject restores the star
// count the server remembered, if any.
export function step(current, delta, previousStars) {
  if (current === REJECT && delta > 0 && previousStars) return clamp(previousStars);
  return clamp(current + (delta > 0 ? 1 : -1));
}

export function afterKey(current, key, previousStars) {
  if (/^[0-5]$/.test(key)) return Number(key);
  if (key === 'x' || key === 'X') return current === REJECT ? clamp(previousStars || 0) : REJECT;
  return null;
}

export function label(rating) {
  if (rating === REJECT) return '✖ reject';
  return '★'.repeat(rating) + '☆'.repeat(5 - rating);
}
