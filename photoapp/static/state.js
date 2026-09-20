// Shared view state (one page, so a module-level object is enough).
export const state = {
  route: null,        // parsed URL hash
  dirs: {dirs: [], photos: 0},
  photos: [],         // photos of the current folder, in display order
  total: 0,
  selected: new Set(),
  selecting: false,   // touch-friendly selection mode
  undoStack: [],      // {ids: [activity ids]} or {batch_id}
  dirty: false,
};
