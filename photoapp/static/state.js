// Shared view state (one page, so a module-level object is enough).
export const state = {
  route: null,        // parsed URL hash
  dirs: {dirs: [], photos: 0},
  photos: [],         // photos of the current folder, in display order
  total: 0,           // matching photos on the server when the folder was loaded
  loaded: 0,          // how many of them have been fetched (the paging offset)
  removed: 0,         // photos that left the view after a rating change (ticket 056)
  epoch: 0,           // bumped on every removal/re-insertion, so in-flight page loads can be discarded
  selected: new Set(),
  selecting: false,   // touch-friendly selection mode
  undoStack: [],      // {ids: [activity ids]} or {batch_id}, plus left: photos that left the view
  dirty: false,
};
