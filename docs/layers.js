// Layers: put the presenter (the main video) over another picture or video, with or without
// their own background. Person cut-outs use MediaPipe's selfie segmenter, on this device.
import { FilesetResolver, ImageSegmenter } from './lib/mediapipe/vision_bundle.mjs';
import { Input, BlobSource, ALL_FORMATS, CanvasSink } from './lib/mediabunny.min.mjs';

export const GRADIENTS = { sunset: ['#ff6a3d', '#ffd21a'], ocean: ['#0b3d91', '#38c6f4'], night: ['#141a26', '#3a2d6b'], board: ['#f4f6f8', '#dfe5ec'] };
export const defaultLayers = () => ({ bg: 'none', color: '#1e7a46', grad: 'ocean', cut: false, size: 1, x: 0.5, y: 0.5, shape: 'round', hasFile: false, fileIsVideo: false });
export const layersActive = (L) => L && (L.bg !== 'none' || L.cut);

// ---------- Segmentation ----------
let segmenter = null, segReady = null, ts = 0;
export function loadSegmenter(landscape = true) {
  segReady ??= (async () => {
    const base = new URL('./lib/mediapipe/', import.meta.url).href;
    const fileset = await FilesetResolver.forVisionTasks(base);
    segmenter = await ImageSegmenter.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: base + (landscape ? 'selfie_segmenter_landscape.tflite' : 'selfie_segmenter.tflite'), delegate: 'CPU' },
      runningMode: 'VIDEO', outputCategoryMask: false, outputConfidenceMasks: true,
    });
    return segmenter;
  })().catch((e) => { segReady = null; throw e; });
  return segReady;
}

/** Turns a frame into a soft alpha mask canvas (model resolution; scaled up smoothly when drawn). */
export class Masker {
  constructor() { this.prev = null; this.canvas = document.createElement('canvas'); }
  mask(source) {
    if (!segmenter) return null;
    const r = segmenter.segmentForVideo(source, ++ts);
    const m = r.confidenceMasks?.[0];
    if (!m) { r.close?.(); return null; }
    const w = m.width, h = m.height, v = m.getAsFloat32Array();
    if (!this.prev || this.prev.length !== v.length) this.prev = new Float32Array(v);
    if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; }
    const ctx = this.canvas.getContext('2d'), img = ctx.createImageData(w, h), d = img.data;
    for (let i = 0; i < v.length; i++) {
      const s = this.prev[i] = this.prev[i] * 0.35 + v[i] * 0.65;      // calm flicker between frames
      const a = Math.min(1, Math.max(0, (s - 0.35) / 0.35));            // crisper edge
      d[i * 4 + 3] = a * 255;
    }
    ctx.putImageData(img, 0, 0);
    r.close?.();
    return this.canvas;
  }
}

// ---------- Background video for export (reads frames in order, loops) ----------
export class BgVideo {
  async open(file) {
    const input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
    this.track = await input.getPrimaryVideoTrack();
    this.duration = await input.computeDuration();
    this.restart();
    return this;
  }
  restart() { this.gen = new CanvasSink(this.track, { width: 1280, fit: 'contain' }).canvases(); this.cur = null; this.next = null; this.base = 0; }
  async frameAt(t) {
    const local = t % this.duration;
    if (this.cur && local + 0.05 < this.cur.timestamp) this.restart();   // looped around
    for (;;) {
      if (!this.next) {
        let r = await this.gen.next();
        if (r.done) { if (this.cur) return this.cur.canvas; this.restart(); r = await this.gen.next(); if (r.done) return null; }
        this.next = r.value;
      }
      if (!this.cur || this.next.timestamp <= local) { this.cur = this.next; this.next = null; } else break;
    }
    return this.cur.canvas;
  }
}

// ---------- Drawing ----------
function cover(ctx, img, iw, ih, W, H) {
  const s = Math.max(W / iw, H / ih), cw = W / s, ch = H / s;
  ctx.drawImage(img, (iw - cw) / 2, (ih - ch) / 2, cw, ch, 0, 0, W, H);
}
const tmp = document.createElement('canvas'), blurS = document.createElement('canvas'), blurM = document.createElement('canvas');
function blurred(ctx, img, iw, ih, W, H) {
  blurS.width = 64; blurS.height = Math.max(8, Math.round(64 * H / W));
  cover(blurS.getContext('2d'), img, iw, ih, blurS.width, blurS.height);
  blurM.width = blurS.width * 4; blurM.height = blurS.height * 4;
  const m = blurM.getContext('2d'); m.imageSmoothingQuality = 'high'; m.drawImage(blurS, 0, 0, blurM.width, blurM.height);
  ctx.imageSmoothingQuality = 'high'; ctx.drawImage(blurM, 0, 0, W, H);
  ctx.fillStyle = 'rgba(0,0,0,0.25)'; ctx.fillRect(0, 0, W, H);
}

/**
 * Draws one composed frame. frame: presenter frame (fw×fh). mask: from Masker (or null).
 * bgImg: user's background picture/video frame (or null).
 */
export function composite(ctx, W, H, frame, fw, fh, mask, bgImg, L) {
  // 1) Background
  if (L.bg === 'none') cover(ctx, frame, fw, fh, W, H);
  else if (L.bg === 'blur') blurred(ctx, frame, fw, fh, W, H);
  else if (L.bg === 'color') { ctx.fillStyle = L.color; ctx.fillRect(0, 0, W, H); }
  else if (L.bg === 'grad') { const [a, b] = GRADIENTS[L.grad] || GRADIENTS.ocean; const g = ctx.createLinearGradient(0, 0, W, H); g.addColorStop(0, a); g.addColorStop(1, b); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H); }
  else if (L.bg === 'file' && bgImg) cover(ctx, bgImg, bgImg.videoWidth || bgImg.width, bgImg.videoHeight || bgImg.height, W, H);
  else { ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H); }
  if (L.bg === 'none' && !L.cut) return;

  // 2) Presenter box (same shape as the presenter video, scaled by size, centered at x,y)
  const full = L.size >= 0.999;
  let bw, bh;
  if (full) { const s = Math.max(W / fw, H / fh); bw = fw * s; bh = fh * s; }
  else { bh = H * L.size; bw = bh * fw / fh; if (bw > W * 0.95) { bw = W * 0.95; bh = bw * fh / fw; } }
  const bx = full ? (W - bw) / 2 : Math.min(W - bw, Math.max(0, L.x * W - bw / 2));
  const by = full ? (H - bh) / 2 : Math.min(H - bh, Math.max(0, L.y * H - bh / 2));

  if (L.cut && mask) {
    tmp.width = Math.max(1, Math.round(bw)); tmp.height = Math.max(1, Math.round(bh));
    const t = tmp.getContext('2d');
    t.globalCompositeOperation = 'source-over'; t.clearRect(0, 0, tmp.width, tmp.height);
    t.drawImage(frame, 0, 0, tmp.width, tmp.height);
    t.globalCompositeOperation = 'destination-in'; t.imageSmoothingQuality = 'high';
    t.drawImage(mask, 0, 0, tmp.width, tmp.height);
    t.globalCompositeOperation = 'source-over';
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,0.35)'; ctx.shadowBlur = Math.min(W, H) * 0.02;
    ctx.drawImage(tmp, bx, by);
    ctx.restore();
  } else {
    ctx.save();
    ctx.beginPath();
    const r = L.shape === 'circle' ? Math.min(bw, bh) / 2 : L.shape === 'round' ? Math.min(bw, bh) * 0.08 : 0;
    if (L.shape === 'circle') { const d = Math.min(bw, bh); ctx.arc(bx + bw / 2, by + bh / 2, d / 2, 0, Math.PI * 2); }
    else if (ctx.roundRect) ctx.roundRect(bx, by, bw, bh, r); else ctx.rect(bx, by, bw, bh);
    ctx.shadowColor = 'rgba(0,0,0,0.45)'; ctx.shadowBlur = Math.min(W, H) * 0.025; ctx.fillStyle = '#000'; ctx.fill();
    ctx.shadowBlur = 0; ctx.clip();
    if (L.shape === 'circle') { const d = Math.min(bw, bh); ctx.drawImage(frame, (fw - fw * d / bw) / 2, (fh - fh * d / bh) / 2, fw * d / bw, fh * d / bh, bx + (bw - d) / 2, by + (bh - d) / 2, d, d); }
    else ctx.drawImage(frame, bx, by, bw, bh);
    ctx.restore();
    if (!full) { ctx.save(); ctx.lineWidth = Math.max(2, Math.min(W, H) * 0.006); ctx.strokeStyle = 'rgba(255,255,255,0.9)'; ctx.beginPath();
      if (L.shape === 'circle') { const d = Math.min(bw, bh); ctx.arc(bx + bw / 2, by + bh / 2, d / 2, 0, Math.PI * 2); } else if (ctx.roundRect) ctx.roundRect(bx, by, bw, bh, r); else ctx.rect(bx, by, bw, bh);
      ctx.stroke(); ctx.restore(); }
  }
}
