// Swipe detection with Pointer Events (touch, pen and mouse share one path).
//
// attachSwipe(element, {onSwipe(direction), onDrag(dx, dy, axis), onCancel(),
//                        enabled()})
// direction is 'left' | 'right' | 'up' | 'down'. The axis is decided once the
// finger has moved a few pixels, and only that axis is followed.

const LOCK_DISTANCE = 10;      // px before the axis is decided
const MIN_DISTANCE = 60;       // px for a horizontal swipe
const MIN_VERTICAL = 50;       // px for a vertical swipe
const MIN_VELOCITY = 0.5;      // px/ms, allows a short fast flick

export function attachSwipe(element, handlers) {
  let start = null;
  const down = new Set();             // pointers currently pressed

  element.addEventListener('pointerdown', (e) => {
    down.add(e.pointerId);
    if (down.size > 1) {                // a second finger: a pinch, not a swipe
      if (start) { start = null; if (handlers.onCancel) handlers.onCancel(); }
      return;
    }
    if (e.button > 0 || (handlers.enabled && !handlers.enabled())) return;
    start = {x: e.clientX, y: e.clientY, t: performance.now(), axis: null, id: e.pointerId};
    element.setPointerCapture(e.pointerId);
  });

  element.addEventListener('pointermove', (e) => {
    if (!start || e.pointerId !== start.id) return;
    const dx = e.clientX - start.x;
    const dy = e.clientY - start.y;
    if (!start.axis) {
      if (Math.hypot(dx, dy) < LOCK_DISTANCE) return;
      start.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }
    if (handlers.onDrag) handlers.onDrag(start.axis === 'x' ? dx : 0, start.axis === 'y' ? dy : 0, start.axis);
  });

  const finish = (e, cancelled) => {
    down.delete(e.pointerId);
    if (!start || e.pointerId !== start.id) return;
    const s = start;
    start = null;
    const dx = e.clientX - s.x;
    const dy = e.clientY - s.y;
    const elapsed = Math.max(1, performance.now() - s.t);
    let direction = null;
    if (!cancelled && s.axis === 'x' &&
        (Math.abs(dx) >= MIN_DISTANCE || Math.abs(dx) / elapsed >= MIN_VELOCITY && Math.abs(dx) > 25)) {
      direction = dx < 0 ? 'left' : 'right';
    } else if (!cancelled && s.axis === 'y' &&
               (Math.abs(dy) >= MIN_VERTICAL || Math.abs(dy) / elapsed >= MIN_VELOCITY && Math.abs(dy) > 25)) {
      direction = dy < 0 ? 'up' : 'down';
    }
    if (direction) handlers.onSwipe(direction);
    else if (s.axis && handlers.onCancel) handlers.onCancel();
    if (s.axis) suppressNextClick(element);
  };

  element.addEventListener('pointerup', (e) => finish(e, false));
  element.addEventListener('pointercancel', (e) => finish(e, true));
}

// A drag must not also count as a click (which would toggle zoom).
function suppressNextClick(element) {
  const block = (e) => { e.stopPropagation(); e.preventDefault(); };
  element.addEventListener('click', block, {capture: true, once: true});
  setTimeout(() => element.removeEventListener('click', block, {capture: true}), 50);
}
