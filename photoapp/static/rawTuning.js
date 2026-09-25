// Ticket 105: client-side RAW tuning via LibRaw-Wasm, replacing 094's GET .../raw_preview
// network round trip with a local decode once ticket 104's lossy tuning-preview DNG has been
// fetched. loupe.js falls back to the network path (requestPreviewNetwork) whenever render()
// resolves null here -- LibRaw-Wasm failing to load, the vendored module missing, the preview
// DNG not being available for this file, or a decode error for a camera model it chokes on.
//
// Settings mapping mirrors previews.py's _postprocess_kwargs exactly (the same parameters
// raw_settings.py persists) so the local preview and the eventual server-side committed render
// mean the same thing by each control. `values` is loupe.js's pending dict (the same API-shaped
// keys saveRawSettings POSTs), not the raw_*-prefixed raw_settings.get() shape the server uses.

const VENDOR_URL = '/static/vendor/libraw-wasm/index.js';

let libRawPromise = null;
function loadLibRaw() {
  if (!libRawPromise) {
    libRawPromise = import(VENDOR_URL).then((m) => m.default).catch(() => null);
  }
  return libRawPromise;
}

function mapSettings(values) {
  const settings = {outputBps: 8};
  const mode = values.wb_mode || 'camera';
  if (mode === 'auto') settings.useAutoWb = true;
  else if (mode === 'manual' && values.wb_r != null) {
    settings.userMul = [values.wb_r, values.wb_g, values.wb_b, values.wb_g];
  } else {
    settings.useCameraWb = true;   // 'camera', or no mode set at all
  }
  if (values.bright != null) settings.bright = values.bright;
  if (values.highlight != null) settings.highlight = values.highlight;
  if (values.exposure != null) {
    settings.expShift = values.exposure;
    settings.expCorrec = true;
    settings.noAutoBright = true;
  }
// Ticket 112: noise reduction (FBDD) and demosaic algorithm map straight through; contrast and
  // saturation are post-decode in toObjectUrl (the vendored libraw-wasm build ignores its
  // documented `gamm` setting, so no native gamma is sent).
  if (values.noise != null) settings.fbddNoiserd = values.noise;
  if (values.demosaic != null) settings.userQual = values.demosaic;
  return settings;
}

// imageData() (see the vendored index.d.ts) returns {width, height, colors, bits, data}, an
// interleaved RGB buffer -- not something an <img> can show directly, so it's painted onto a
// throwaway canvas and read back out as a blob: URL, matching what ui.tuning already expects
// from the network path.
//
// Ticket 109: raw_shadow is applied here, post-decode, per channel -- v' = v + amount*(1-v)^2 --
// exactly the server-side previews._lift_shadows formula, so a local preview and the eventual
// committed thumbnail agree. Only applied when shadow is set (NULL/0 is a no-op).
function liftShadow(v, amount) {
  const x = v / 255;
  return Math.round(Math.min(1, Math.max(0, x + amount * (1 - x) ** 2)) * 255);
}

// Ticket 112: post-decode saturation (v' = luma + (v - luma)*amount, luma = 0.299R+0.587G+0.114B)
// and contrast (v' = 0.5 + (v - 0.5)*amount), the server's previews._apply_saturation and
// _apply_contrast formulas, on 8-bit values. Applied after the shadow lift, in the same order as
// render(): shadow -> contrast -> saturation. 1.0 is identity for either, so it is skipped then.
function clamp255(x) {
  return Math.max(0, Math.min(255, Math.round(x)));
}

function toObjectUrl(img, shadow, contrast, saturation) {
  if (img.colors !== 3 || img.bits !== 8) {
    throw new Error(`unsupported imageData shape: colors=${img.colors} bits=${img.bits}`);
  }
  const {width, height, data} = img;
  const lift = shadow != null && shadow !== 0;
  const con = contrast != null && contrast !== 1;
  const sat = saturation != null && saturation !== 1;
  const rgba = new Uint8ClampedArray(width * height * 4);
  for (let i = 0, j = 0; i < width * height; i++, j += 3) {
    let r = data[j], g = data[j + 1], b = data[j + 2];
    if (lift) { r = liftShadow(r, shadow); g = liftShadow(g, shadow); b = liftShadow(b, shadow); }
    if (con) {
      r = clamp255(127.5 + (r - 127.5) * contrast);
      g = clamp255(127.5 + (g - 127.5) * contrast);
      b = clamp255(127.5 + (b - 127.5) * contrast);
    }
    if (sat) {
      const luma = 0.299 * r + 0.587 * g + 0.114 * b;
      r = clamp255(luma + (r - luma) * saturation);
      g = clamp255(luma + (g - luma) * saturation);
      b = clamp255(luma + (b - luma) * saturation);
    }
    rgba[i * 4] = r; rgba[i * 4 + 1] = g; rgba[i * 4 + 2] = b; rgba[i * 4 + 3] = 255;
  }
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  canvas.getContext('2d').putImageData(new ImageData(rgba, width, height), 0, 0);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => blob ? resolve(URL.createObjectURL(blob)) : reject(new Error('toBlob failed')));
  });
}

// One session per file, reused across every slider tick while its tuning controls stay open:
// the lossy preview DNG is fetched once, then every render() re-decodes locally from those same
// bytes with the new settings -- no further network requests.
export class RawTuningSession {
  constructor(fileId) {
    this.fileId = fileId;
    this.lr = null;
    this.bytesPromise = null;
    this.failed = false;
    this.seq = 0;          // bumped on every render()/dispose() to drop superseded results
    this.lastUrl = null;
  }

  async _ensure() {
    if (this.failed) return null;
    if (this.lr) return this.lr;
    const LibRaw = await loadLibRaw();
    if (!LibRaw) { this.failed = true; return null; }
    if (!this.bytesPromise) {
      this.bytesPromise = fetch(`/api/files/${this.fileId}/raw_preview_dng`).then((r) => {
        if (!r.ok) throw new Error(`raw_preview_dng: ${r.status}`);
        return r.arrayBuffer();
      });
    }
    try {
      this.bytes = new Uint8Array(await this.bytesPromise);
    } catch (e) {
      this.failed = true;
      return null;
    }
    this.lr = new LibRaw();
    return this.lr;
  }

  // Renders `values` (a raw_settings.get()-shaped dict) to a blob: URL, or null if local
  // rendering isn't available right now (the caller falls back to the network path). A result
  // superseded by a later render()/dispose() call while awaiting resolves to null too.
  async render(values) {
    const lr = await this._ensure();
    if (!lr) return null;
    const mySeq = ++this.seq;
    let url;
    try {
      await lr.open(this.bytes, mapSettings(values));
      const img = await lr.imageData();
      url = await toObjectUrl(img, values.shadow, values.contrast, values.saturation);
    } catch (e) {
      return null;
    }
    if (mySeq !== this.seq) {   // superseded while this render was in flight
      URL.revokeObjectURL(url);
      return null;
    }
    if (this.lastUrl) URL.revokeObjectURL(this.lastUrl);
    this.lastUrl = url;
    return url;
  }

  dispose() {
    this.seq++;   // invalidate any render() still in flight
    if (this.lastUrl) { URL.revokeObjectURL(this.lastUrl); this.lastUrl = null; }
    if (this.lr) { this.lr.dispose(); this.lr = null; }
  }
}
