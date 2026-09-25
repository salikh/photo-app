// Ticket 105: client-side RAW tuning via LibRaw-Wasm, replacing 094's GET .../raw_preview
// network round trip with a local decode once ticket 104's lossy tuning-preview DNG has been
// fetched. loupe.js falls back to the network path (requestPreviewNetwork) whenever render()
// resolves null here -- LibRaw-Wasm failing to load, the vendored module missing, the preview
// DNG not being available for this file, or a decode error for a camera model it chokes on.
//
// Settings mapping mirrors previews.py's _postprocess_kwargs exactly (the same parameters
// raw_settings.py persists -- brightness, WB, highlight, exposure, shadow) so the local preview
// and the eventual server-side committed render mean the same thing by each control. `values` is
// loupe.js's pending dict (the same API-shaped keys saveRawSettings POSTs), not the raw_*-prefixed
// raw_settings.get() shape the server uses.

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

function toObjectUrl(img, shadow) {
  if (img.colors !== 3 || img.bits !== 8) {
    throw new Error(`unsupported imageData shape: colors=${img.colors} bits=${img.bits}`);
  }
  const {width, height, data} = img;
  const lift = shadow != null && shadow !== 0;
  const rgba = new Uint8ClampedArray(width * height * 4);
  for (let i = 0, j = 0; i < width * height; i++, j += 3) {
    rgba[i * 4] = lift ? liftShadow(data[j], shadow) : data[j];
    rgba[i * 4 + 1] = lift ? liftShadow(data[j + 1], shadow) : data[j + 1];
    rgba[i * 4 + 2] = lift ? liftShadow(data[j + 2], shadow) : data[j + 2];
    rgba[i * 4 + 3] = 255;
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
      url = await toObjectUrl(img, values.shadow);
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
