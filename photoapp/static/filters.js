// The rating filters offered as buttons (ticket 055), and their labels.
//
// Primary row: All, Rejected, Unrated, and exactly N stars. Everything else the
// server can filter by is in the "more" menu. With --one_star_is_unrated there
// is no 1-star button (1 star counts as unrated, see rating.js).

import {isOneStarUnrated} from './rating.js';

const MORE = [
  ['picked', 'picked (any stars)'],
  ['rated', 'rated or rejected'],
  ['fav', 'favorites'],
  ['conflict', 'sidecars disagree'],
];

export function primary() {
  const buttons = [['all', 'All', 'all photos'],
                   ['rejected', '✖ Rejected', 'rejected only'],
                   ['unrated', '☆ Unrated', 'unrated only']];
  for (let n = 1; n <= 5; n++) {
    if (n === 1 && isOneStarUnrated()) continue;
    buttons.push([`rating:${n}`, `★${n}`, `exactly ${n} star${n > 1 ? 's' : ''}`]);
  }
  return buttons;
}

export function more() { return MORE; }

// Short text for the current filter (loupe HUD, status line).
export function label(filter) {
  const found = primary().find((b) => b[0] === filter) || MORE.find((m) => m[0] === filter);
  return found ? found[1] : filter;
}

// Does a photo match a filter? Mirrors photoapp/library.py so the UI agrees with what a
// reload would show (used when a rating change makes a photo leave the view, ticket 056).
export function matches(photo, filter) {
  const oneStar = isOneStarUnrated();
  const r = photo.rating;
  switch (filter) {
    case 'all': return true;
    case 'unrated': return oneStar ? (r === 0 || r === 1) : r === 0;
    case 'rejected': return r === -1;
    case 'picked': return oneStar ? r > 1 : r > 0;
    case 'rated': return oneStar ? (r !== 0 && r !== 1) : r !== 0;
    case 'fav': return !!photo.fav;
    case 'conflict': return !!photo.conflict;
    default: {
      const m = /^rating:([1-5])$/.exec(filter || '');
      return !!m && r === Number(m[1]);
    }
  }
}
