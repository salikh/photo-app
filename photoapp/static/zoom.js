// Pinch zoom and pan of the photo in the viewer (ticket 060), for tablets and phones (Pointer
// Events, so the same code serves iPad Safari and Android Chrome), with mouse and trackpad too.
//
//   pinch          two fingers zoom around the point between them
//   drag           one finger (or the mouse) pans a zoomed photo
//   double tap     toggles between fit-to-screen and 100%, around the tapped point (touch);
//                  a click toggles the same way with a mouse
//   Ctrl+wheel     zoom around the pointer (a trackpad pinch arrives as Ctrl+wheel)
//   + / - keys     zoom around the center (handled by the viewer, calling zoomBy)
//
// The photo is drawn at its original pixel size and moved/scaled with a CSS transform, so only the
// photo scales; the header and buttons never do (touch-action: none keeps the browser from zooming
// the page under the stage). Zoom range: from "fit to the stage" up to MAX_SCALE of the original.

export const MAX_SCALE = 4;

const TAP_MAX_MS = 350;
const TAP_MAX_MOVE = 10;
const DOUBLE_TAP_MS = 320;
const DOUBLE_TAP_DISTANCE = 40;
const WHEEL_STEP = 0.0015;
const KEY_STEP = 1.25;

// ---- the math, pure so it can be tested on its own ----

// The scale at which the whole photo (W x H) fits the stage (SW x SH).
export function fitScale(W, H, SW, SH) { return Math.min(SW / W, SH / H); }

// Keep a photo drawn at scale s inside the stage: centered on an axis where it is smaller than the
// stage, and not dragged past the stage edges where it is larger.
export function clampOffset(x, y, s, W, H, SW, SH) {
  const w = W * s;
  const h = H * s;
  return {
    x: w <= SW ? (SW - w) / 2 : Math.min(0, Math.max(SW - w, x)),
    y: h <= SH ? (SH - h) / 2 : Math.min(0, Math.max(SH - h, y)),
  };
}

// New offset when the scale changes from s to newS and the point (px, py) of the stage must stay
// where it is on screen.
export function zoomAround(x, y, s, newS, px, py) {
  const k = newS / s;
  return {x: px - (px - x) * k, y: py - (py - y) * k};
}

// ---- the controller ----

// hooks: fullSize() -> {W, H} in original pixels (or null); onEnter(); onExit()
export function createZoom(stage, img, hooks) {
  let active = false;
  let s = 1;
  let x = 0;
  let y = 0;
  let W = 0;
  let H = 0;
  const pointers = new Map();          // pointerId -> {x, y}
  let gesture = null;                  // pinch: {dist, s, x, y, mid: {x, y}}
  let press = null;                    // a possible tap: {id, x, y, t, type}
  let lastTap = null;                  // {x, y, t} of the previous touch tap
  let moved = false;                   // the current press turned into a drag/pinch

  const size = () => {
    const r = stage.getBoundingClientRect();
    return {SW: r.width, SH: r.height, left: r.left, top: r.top};
  };

  function limits() {
    const {SW, SH} = size();
    return {fit: fitScale(W, H, SW, SH), max: Math.max(MAX_SCALE, fitScale(W, H, SW, SH))};
  }

  function apply() {
    const {SW, SH} = size();
    const c = clampOffset(x, y, s, W, H, SW, SH);
    x = c.x;
    y = c.y;
    img.style.width = `${W}px`;
    img.style.height = `${H}px`;
    img.style.transform = `translate(${x}px, ${y}px) scale(${s})`;
  }

  // Start zooming at `scale` (default: 100% of the original pixels) around the stage point (px, py)
  // (default: the middle). Returns false if the photo's size is not known.
  function enter({scale = 1, px, py} = {}) {
    const full = hooks.fullSize();
    if (!full || !full.W || !full.H) return false;
    ({W, H} = full);
    const {SW, SH} = size();
    const fit = fitScale(W, H, SW, SH);
    const start = active ? {x, y, s} : {
      s: fit, ...clampOffset(0, 0, fit, W, H, SW, SH),      // the photo as it is shown when fitted
    };
    const {max} = limits();
    const target = Math.min(max, Math.max(fit, scale));
    const cx = px ?? SW / 2;
    const cy = py ?? SH / 2;
    const moved_ = zoomAround(start.x, start.y, start.s, target, cx, cy);
    s = target;
    x = moved_.x;
    y = moved_.y;
    const wasActive = active;
    active = true;
    apply();
    if (!wasActive) hooks.onEnter();
    return true;
  }

  function exit() {
    if (!active) return;
    reset();
    hooks.onExit();
  }

  // Forget the zoom without telling anyone (a new photo is shown).
  function reset() {
    active = false;
    pointers.clear();
    gesture = press = lastTap = null;
    img.style.width = img.style.height = img.style.transform = '';
  }

  function zoomBy(factor, px, py) {
    if (!active) {
      if (factor < 1) return;                 // nothing to zoom out of
      const {SW, SH} = size();
      const full = hooks.fullSize();
      if (!full) return;
      enter({scale: fitScale(full.W, full.H, SW, SH) * factor, px, py});
      return;
    }
    const {fit, max} = limits();
    const newS = Math.min(max, Math.max(fit, s * factor));
    const {SW, SH} = size();
    const p = zoomAround(x, y, s, newS, px ?? SW / 2, py ?? SH / 2);
    s = newS;
    x = p.x;
    y = p.y;
    apply();
    if (s <= fit * 1.005) exit();
  }

  const local = (e) => {
    const r = stage.getBoundingClientRect();
    return {x: e.clientX - r.left, y: e.clientY - r.top};
  };

  function startPinch() {
    const [a, b] = [...pointers.values()];
    gesture = {
      dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, s, x, y,
      mid: {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2},
    };
  }

  stage.addEventListener('pointerdown', (e) => {
    if (e.button > 0) return;
    const p = local(e);
    pointers.set(e.pointerId, p);
    stage.setPointerCapture(e.pointerId);
    if (pointers.size === 1) {
      moved = false;
      press = {id: e.pointerId, x: p.x, y: p.y, t: performance.now(), type: e.pointerType};
    } else {
      press = null;                       // a second finger: this is a pinch, not a tap
      moved = true;
      if (pointers.size === 2) {
        if (!active) {
          const full = hooks.fullSize();
          if (full) enter({scale: 0});      // start at the fitted size, then scale from there
        }
        startPinch();
      }
    }
  });

  stage.addEventListener('pointermove', (e) => {
    if (!pointers.has(e.pointerId)) return;
    const prev = pointers.get(e.pointerId);
    const p = local(e);
    pointers.set(e.pointerId, p);
    if (pointers.size >= 2 && gesture && active) {
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      const mid = {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
      const {fit, max} = limits();
      const newS = Math.min(max, Math.max(fit * 0.7, gesture.s * dist / gesture.dist));
      // the photo point that was under the start midpoint follows the current midpoint
      const ix = (gesture.mid.x - gesture.x) / gesture.s;
      const iy = (gesture.mid.y - gesture.y) / gesture.s;
      s = newS;
      x = mid.x - ix * newS;
      y = mid.y - iy * newS;
      moved = true;
      apply();
      return;
    }
    if (pointers.size === 1 && active) {
      if (press && Math.hypot(p.x - press.x, p.y - press.y) > TAP_MAX_MOVE) moved = true;
      if (moved) {
        x += p.x - prev.x;
        y += p.y - prev.y;
        apply();
      }
    } else if (press && Math.hypot(p.x - press.x, p.y - press.y) > TAP_MAX_MOVE) {
      moved = true;                         // a swipe at fit size: not a tap
    }
  });

  function release(e, cancelled) {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    if (gesture && pointers.size < 2) {
      gesture = null;
      const {fit} = limits();
      if (active && s <= fit * 1.02) { exit(); pointers.clear(); return; }   // pinched back to fit
      if (pointers.size === 1) { press = null; moved = true; }             // one finger stays: it pans
    }
    if (pointers.size > 0) return;
    const wasPress = press;
    press = null;
    if (cancelled || !wasPress || moved) return;
    const now = performance.now();
    if (now - wasPress.t > TAP_MAX_MS) return;
    onTap(wasPress);
  }

  stage.addEventListener('pointerup', (e) => release(e, false));
  stage.addEventListener('pointercancel', (e) => release(e, true));

  function onTap(t) {
    const toggle = () => {
      if (active) exit();
      else enter({scale: 1, px: t.x, py: t.y});
    };
    if (t.type === 'mouse') { toggle(); return; }            // a click toggles
    const now = performance.now();
    if (lastTap && now - lastTap.t < DOUBLE_TAP_MS &&
        Math.hypot(t.x - lastTap.x, t.y - lastTap.y) < DOUBLE_TAP_DISTANCE) {
      lastTap = null;
      toggle();                                              // a double tap toggles
    } else {
      lastTap = {x: t.x, y: t.y, t: now};
    }
  }

  // Ctrl+wheel (a trackpad pinch) zooms around the pointer; a plain wheel pans a zoomed photo.
  stage.addEventListener('wheel', (e) => {
    const p = local(e);
    if (e.ctrlKey) {
      e.preventDefault();
      zoomBy(Math.exp(-e.deltaY * WHEEL_STEP * (e.deltaMode === 1 ? 16 : 1)), p.x, p.y);
    } else if (active) {
      e.preventDefault();
      x -= e.deltaX;
      y -= e.deltaY;
      apply();
    }
  }, {passive: false});

  window.addEventListener('resize', () => { if (active) apply(); });

  return {
    isActive: () => active,
    scale: () => s,
    enter, exit, reset, zoomBy,
    zoomIn: () => zoomBy(KEY_STEP),
    zoomOut: () => zoomBy(1 / KEY_STEP),
    // Real size of the photo has become known (the full-size image loaded): keep what is shown.
    setFullSize(w, h) {
      if (!active || (w === W && h === H)) return;
      s = s * W / w;                                         // keep the size on screen, the pixels change
      W = w;
      H = h;
      apply();
    },
  };
}
