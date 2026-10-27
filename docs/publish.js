// YouTube helpers: picking thumbnail frames, drawing thumbnails, and (optionally, with Claude)
// writing titles, a description and hashtags.
import { Input, BlobSource, ALL_FORMATS, CanvasSink } from './lib/mediabunny.min.mjs';
import { fontCss } from './render.js';

/** How sharp and well-lit a frame is (higher is better). */
function frameScore(canvas) {
  const w = 160, h = Math.round(canvas.height * 160 / canvas.width);
  const c = new OffscreenCanvas(w, h), x = c.getContext('2d');
  x.drawImage(canvas, 0, 0, w, h);
  const d = x.getImageData(0, 0, w, h).data;
  const g = new Float32Array(w * h);
  let mean = 0;
  for (let i = 0; i < w * h; i++) { g[i] = 0.299 * d[i * 4] + 0.587 * d[i * 4 + 1] + 0.114 * d[i * 4 + 2]; mean += g[i]; }
  mean /= w * h;
  // Sharpness: variance of the Laplacian (blurry or mid-blink motion frames score low).
  let s = 0, s2 = 0, n = 0;
  for (let yy = 1; yy < h - 1; yy++) for (let xx = 1; xx < w - 1; xx++) {
    const i = yy * w + xx, l = 4 * g[i] - g[i - 1] - g[i + 1] - g[i - w] - g[i + w];
    s += l; s2 += l * l; n++;
  }
  const sharp = s2 / n - (s / n) ** 2;
  const light = mean < 50 ? mean / 50 : mean > 210 ? (255 - mean) / 45 : 1;
  return sharp * light;
}

/** Samples frames across the kept parts of the video and returns the best few. */
export async function findFrames(file, ranges, { count = 24, keep = 6 } = {}) {
  const input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
  const track = await input.getPrimaryVideoTrack();
  const total = ranges.reduce((a, r) => a + r.end - r.start, 0);
  const times = [];
  for (let k = 0; k < count; k++) {
    let at = (k + 0.5) * total / count;
    for (const r of ranges) { const len = r.end - r.start; if (at <= len) { times.push(r.start + at); break; } at -= len; }
  }
  const sink = new CanvasSink(track, { width: 480, fit: 'contain' });
  const out = [];
  let i = 0;
  for await (const f of sink.canvasesAtTimestamps(times)) {
    const t = times[i++];
    if (!f) continue;
    const c = document.createElement('canvas'); c.width = f.canvas.width; c.height = f.canvas.height;
    c.getContext('2d').drawImage(f.canvas, 0, 0);
    out.push({ t, score: frameScore(f.canvas), canvas: c });
  }
  // Best frames, but spread out over the video (no two within 4 s of each other).
  out.sort((a, b) => b.score - a.score);
  const picked = [];
  for (const f of out) { if (picked.every(p => Math.abs(p.t - f.t) > 4)) picked.push(f); if (picked.length === keep) break; }
  return picked.sort((a, b) => a.t - b.t);
}

/** Full-quality frame at time t. */
export async function grabFrame(file, t) {
  const input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
  const track = await input.getPrimaryVideoTrack();
  const f = await new CanvasSink(track).getCanvas(t);
  return f?.canvas || null;
}

/**
 * Draws a thumbnail: the frame cover-cropped to 16:9 (1280×720) or 9:16 (1080×1920),
 * with up to two big lines of text on a soft dark gradient.
 */
export function drawThumbnail(ctx, frame, W, H, { line1 = '', line2 = '', color1 = '#ffd21a', color2 = '#ffffff', font = 'bold', top = false, lang1 = 'tr', lang2 = 'tr' }) {
  const sw = frame.width, sh = frame.height, sc = Math.max(W / sw, H / sh);
  const cw = W / sc, ch = H / sc;
  ctx.drawImage(frame, (sw - cw) / 2, (sh - ch) / 2, cw, ch, 0, 0, W, H);
  const up = (t, l) => { try { return t.toLocaleUpperCase(l); } catch { return t.toUpperCase(); } };
  const lines = [[up(line1, lang1), color1], [up(line2, lang2), color2]].filter(([t]) => t.trim());
  if (!lines.length) return;
  const grad = ctx.createLinearGradient(0, top ? 0 : H, 0, top ? H * 0.55 : H * 0.45);
  grad.addColorStop(0, 'rgba(0,0,0,0.75)'); grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad; ctx.fillRect(0, 0, W, H);
  const maxW = W * 0.9;
  let px = Math.round(Math.min(W, H) * (W > H ? 0.17 : 0.11));
  // Shrink until the longest line fits.
  ctx.font = fontCss(font, px);
  while (px > 20 && Math.max(...lines.map(([t]) => ctx.measureText(t).width)) > maxW) { px -= 4; ctx.font = fontCss(font, px); }
  const lh = px * 1.08;
  let y = top ? H * 0.06 + lh / 2 : H - H * 0.06 - lh * (lines.length - 0.5);
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
  for (const [t, col] of lines) {
    const txt = t;
    ctx.lineWidth = px * 0.16; ctx.strokeStyle = '#000';
    ctx.shadowColor = 'rgba(0,0,0,0.6)'; ctx.shadowBlur = px * 0.25;
    ctx.strokeText(txt, W / 2, y);
    ctx.shadowBlur = 0;
    ctx.fillStyle = col; ctx.fillText(txt, W / 2, y);
    y += lh;
  }
}

const jpeg = (canvas) => canvas.toDataURL('image/jpeg', 0.7).split(',')[1];

/**
 * One Claude call: picks the best thumbnail frame and writes titles, description, hashtags
 * and thumbnail texts. Returns parsed JSON.
 */
export async function prepareWithClaude(callClaude, { frames, transcript, langs, descLang, glossary }) {
  const content = [];
  frames.forEach((f, i) => {
    content.push({ type: 'text', text: `Frame ${i + 1}:` });
    content.push({ type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: jpeg(f.canvas) } });
  });
  content.push({ type: 'text', text:
    `These are candidate thumbnail frames from my YouTube video, a language lesson spoken in ${langs}. ` +
    `Transcript:\n${transcript.slice(0, 12000)}\n` +
    (glossary?.trim() ? `\nWords taught:\n${glossary.trim()}\n` : '') +
    `\nReply with only a JSON object, no other text:\n` +
    `{"bestFrame": <number of the best thumbnail frame: face clearly visible, eyes open, natural expression>,\n` +
    ` "frameReason": "<short reason>",\n` +
    ` "titles": ["<3 YouTube title options for the long video, under 70 characters, in ${descLang}>"],\n` +
    ` "shortTitle": "<title for the YouTube Short, under 60 characters, in ${descLang}>",\n` +
    ` "description": "<description in ${descLang}: 2-3 short paragraphs, then the key words/phrases taught as a list>",\n` +
    ` "hashtags": ["<8-12 relevant hashtags without spaces, mixing ${langs} and general learning tags>"],\n` +
    ` "thumbnailTexts": [["<line 1, max 3 words>", "<line 2, max 4 words>"], ... 3 options]}`,
  });
  const r = await callClaude(content, 4000);
  const m = r.text.match(/\{[\s\S]*\}/);
  if (!m) throw new Error('Claude did not return the expected format');
  return JSON.parse(m[0]);
}

// ---------- Cover maker (all on this device, free) ----------
let segmenter = null;
/** Cuts the person out of a picture (MODNet, ~7 MB, downloaded once). Returns a canvas with transparency. */
export async function removeBackground(canvas, onStatus = () => {}) {
  if (!segmenter) {
    onStatus('Loading the background remover (about 7 MB, one time)…');
    const { pipeline, env } = await import('./lib/transformers.min.js');
    env.allowLocalModels = false;
    env.backends.onnx.wasm.wasmPaths = new URL('./lib/', import.meta.url).href;
    segmenter = await pipeline('background-removal', 'Xenova/modnet', { dtype: 'q8' });
  }
  onStatus('Removing the background…');
  const { RawImage } = await import('./lib/transformers.min.js');
  const ctx = canvas.getContext('2d');
  const img = new RawImage(ctx.getImageData(0, 0, canvas.width, canvas.height).data, canvas.width, canvas.height, 4);
  const out = await segmenter(img);
  const r = Array.isArray(out) ? out[0] : out;
  const c = document.createElement('canvas'); c.width = r.width; c.height = r.height;
  c.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(r.rgba ? r.rgba().data : r.data), r.width, r.height), 0, 0);
  return c;
}

/** Bounding box of the visible (non-transparent) part of a cut-out. */
function bbox(canvas) {
  const { width: w, height: h } = canvas, d = canvas.getContext('2d').getImageData(0, 0, w, h).data;
  let x0 = w, y0 = h, x1 = 0, y1 = 0;
  for (let y = 0; y < h; y += 2) for (let x = 0; x < w; x += 2) if (d[(y * w + x) * 4 + 3] > 128) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
  return x1 > x0 ? { x: x0, y: y0, w: x1 - x0, h: y1 - y0 } : { x: 0, y: 0, w, h };
}

export const BACKGROUNDS = {
  photo: 'Original', blur: 'Blurred', sunset: 'Sunset', ocean: 'Ocean', night: 'Night', lang: 'Your colors',
};
const GRADS = { sunset: ['#ff6a3d', '#ffd21a'], ocean: ['#0b3d91', '#38c6f4'], night: ['#141a26', '#3a2d6b'] };

function cover(ctx, img, W, H) {
  const s = Math.max(W / img.width, H / img.height), cw = W / s, ch = H / s;
  ctx.drawImage(img, (img.width - cw) / 2, (img.height - ch) / 2, cw, ch, 0, 0, W, H);
}

/**
 * Draws a cover. layout: where the text goes ('left' | 'right' | 'top' | 'bottom');
 * with a cut-out the person stands on the other side, with a soft white outline.
 */
export function drawCover(ctx, W, H, o) {
  const { photo, cutout, bg = 'blur', layout = 'bottom', scale = 0.95, lines = [], font = 'bold', colorA = '#ffd21a', colorB = '#ffffff' } = o;
  // Background
  if (bg === 'photo' || !cutout) cover(ctx, photo, W, H);
  else if (bg === 'blur') {
    const sm = document.createElement('canvas'); sm.width = 64; sm.height = Math.round(64 * H / W);
    cover(sm.getContext('2d'), photo, sm.width, sm.height);
    const md = document.createElement('canvas'); md.width = sm.width * 4; md.height = sm.height * 4;
    const mc = md.getContext('2d'); mc.imageSmoothingQuality = 'high'; mc.drawImage(sm, 0, 0, md.width, md.height);
    ctx.imageSmoothingQuality = 'high'; ctx.drawImage(md, 0, 0, W, H);
    ctx.fillStyle = 'rgba(0,0,0,0.35)'; ctx.fillRect(0, 0, W, H);
  } else {
    const [a, b] = bg === 'lang' ? [o.langA || '#ffd21a', o.langB || '#4de6ff'] : GRADS[bg];
    const g = ctx.createLinearGradient(0, 0, W, H); g.addColorStop(0, a); g.addColorStop(1, b);
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = 'rgba(0,0,0,0.15)'; ctx.fillRect(0, 0, W, H);
  }
  // Person
  const side = layout === 'left' || layout === 'right';
  if (cutout && bg !== 'photo') {
    const bb = bbox(cutout);
    const targetH = H * scale * (side ? 1 : 0.85);
    const k = Math.min(targetH / bb.h, (side ? W * 0.55 : W * 0.9) / bb.w);
    const pw = bb.w * k, ph = bb.h * k;
    const cx = layout === 'left' ? W * 0.72 : layout === 'right' ? W * 0.28 : W / 2;
    const px = cx - pw / 2, py = H - ph;
    ctx.save();
    ctx.shadowColor = 'rgba(255,255,255,0.95)'; ctx.shadowBlur = Math.min(W, H) * 0.02;
    for (let i = 0; i < 2; i++) ctx.drawImage(cutout, bb.x, bb.y, bb.w, bb.h, px, py, pw, ph);
    ctx.restore();
    ctx.drawImage(cutout, bb.x, bb.y, bb.w, bb.h, px, py, pw, ph);
  } else if (!side) {
    // Plain photo: darken behind the text for legibility.
    const top = layout === 'top';
    const g = ctx.createLinearGradient(0, top ? 0 : H, 0, top ? H * 0.55 : H * 0.45);
    g.addColorStop(0, 'rgba(0,0,0,0.75)'); g.addColorStop(1, 'rgba(0,0,0,0)'); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
  }
  // Text: up to 2 lines, each wrapped and shrunk to fit its area.
  const lines2 = lines.filter(l => l.text.trim());
  if (!lines2.length) return;
  const area = layout === 'left' ? { x: W * 0.04, w: W * 0.5 } : layout === 'right' ? { x: W * 0.46, w: W * 0.5 } : { x: W * 0.05, w: W * 0.9 };
  let px = Math.round(Math.min(W, H) * (side ? 0.16 : (W > H ? 0.15 : 0.1)));
  const wrap = (text) => {
    const words = text.split(/\s+/), out = []; let cur = '';
    for (const w of words) { const t = cur ? `${cur} ${w}` : w; if (cur && ctx.measureText(t).width > area.w) { out.push(cur); cur = w; } else cur = t; }
    if (cur) out.push(cur); return out;
  };
  let laid;
  for (;;) {
    ctx.font = fontCss(font, px);
    laid = lines2.flatMap((l, i) => wrap(l.text).map(t => ({ t, color: i === 0 ? colorA : colorB })));
    const fits = laid.every(l => ctx.measureText(l.t).width <= area.w) && laid.length * px * 1.08 <= H * (side ? 0.85 : 0.42);
    if (fits || px <= 24) break;
    px -= 4;
  }
  const lh = px * 1.08, blockH = laid.length * lh;
  let y = side ? (H - blockH) / 2 + lh / 2 : layout === 'top' ? H * 0.06 + lh / 2 : H - H * 0.06 - blockH + lh / 2;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
  for (const l of laid) {
    const x = area.x + area.w / 2;
    ctx.lineWidth = px * 0.16; ctx.strokeStyle = '#000';
    ctx.shadowColor = 'rgba(0,0,0,0.6)'; ctx.shadowBlur = px * 0.25; ctx.strokeText(l.t, x, y);
    ctx.shadowBlur = 0; ctx.fillStyle = l.color; ctx.fillText(l.t, x, y);
    y += lh;
  }
}

/** Free text ideas for the cover, from what's already in the project. */
export function freeIdeas({ hook, glossary, titles, captions, primary, secondary }) {
  const ideas = [];
  const up = (s) => s.trim();
  if (hook?.trim()) { const [a, ...b] = hook.trim().split(/\n|[–—:]/); ideas.push([up(a), up(b.join(' '))]); }
  for (const line of (glossary || '').split('\n').filter(Boolean).slice(0, 3)) {
    const [a, b] = line.split(/\s*(?:–|—|=|→|:|\t| - )\s*/); if (a) ideas.push([up(a), up(b || '')]);
  }
  for (const t of (titles || []).slice(0, 2)) { const w = t.split(/\s+/); ideas.push([w.slice(0, 3).join(' '), w.slice(3, 7).join(' ')]); }
  // The most repeated second-language word is often the topic of a lesson.
  const counts = new Map();
  for (const c of captions || []) if (c.lang === secondary) for (const w of c.text.split(/\s+/)) {
    const k = w.replace(/[^\p{L}-]/gu, ''); if (k.length > 5) counts.set(k, (counts.get(k) || 0) + 1);
  }
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 2).map(e => e[0]);
  for (const t of top) ideas.push([t, '']);
  const seen = new Set();
  return ideas.filter(([a, b]) => a && !seen.has(a + b) && seen.add(a + b)).slice(0, 6);
}

// ---------- Chapters ----------
const stamp = (t) => { t = Math.max(0, Math.round(t)); const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
  return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(s).padStart(2, '0')}`; };

/** Checks YouTube's chapter rules: starts at 0:00, at least 3 chapters, each at least 10 s. */
export function validChapters(list, total) {
  if (list.length < 3 || list[0].t !== 0) return false;
  for (let i = 1; i < list.length; i++) if (list[i].t - list[i - 1].t < 10) return false;
  return total - list.at(-1).t >= 10;
}
export const chaptersText = (list) => list.map(c => `${stamp(c.t)} ${c.title}`).join('\n');

/**
 * Free chapters: split at the longest pauses near even intervals, name each part after the
 * first word-list term said in it (or its first words). caps: captions on the edited timeline.
 */
export function freeChapters(caps, total, { glossary = '', introTitle = 'Giriş' } = {}) {
  if (total < 30 || caps.length < 4) return [];
  const n = Math.max(3, Math.min(10, Math.round(total / 50)));
  const starts = caps.map((c, i) => ({ t: c.start, gap: i ? c.start - caps[i - 1].end : 0, i }));
  const chosen = [0];
  for (let k = 1; k < n; k++) {
    const target = (k * total) / n;
    // Room for the chapters still to come (each needs 10 s).
    const lo = chosen.at(-1) + 10, hi = total - 10 * (n - k);
    const cand = starts.filter(s => s.t >= lo && s.t <= hi);
    if (!cand.length) continue;
    // Close to an even split, preferring a longer pause before the line.
    const score = (s) => s.gap * 2 - Math.abs(s.t - target) / (total / n);
    cand.sort((a, b) => score(b) - score(a));
    chosen.push(cand[0].t);
  }
  const terms = glossary.split('\n').map(l => l.split(/\s*(?:–|—|=|→|:|\t| - )\s*/)[0].trim()).filter(Boolean);
  const used = new Set();
  const list = chosen.map((t, k) => {
    const end = chosen[k + 1] ?? total;
    const inPart = caps.filter(c => c.start >= t && c.start < end);
    const text = inPart.map(c => c.text).join(' ');
    const term = terms.find(x => !used.has(x) && text.toLowerCase().includes(x.toLowerCase()));
    if (term) used.add(term);
    const first = text.split(/(?<=[.!?])\s/)[0].split(/\s+/).slice(0, 6).join(' ').replace(/[.,!?;:]+$/, '');
    return { t: Math.round(t), title: k === 0 ? (term || introTitle) : (term || first || `Bölüm ${k + 1}`) };
  });
  list[0].t = 0;
  return validChapters(list, total) ? list : [];
}

/** Chapters written by Claude from a timestamped transcript. */
export async function claudeChapters(callClaude, caps, total, descLang) {
  const lines = caps.map(c => `${stamp(c.start)} ${c.text}`).join('\n').slice(0, 14000);
  const r = await callClaude([{ type: 'text', text:
    `Here is the timestamped transcript of my ${stamp(total)} long YouTube lesson.\n${lines}\n\n` +
    `Make YouTube chapters in ${descLang}: 3 to 10 of them, the first at 0:00, each at least 10 seconds long, ` +
    `at real topic changes, with short clear titles (max 6 words). Reply with only JSON: [{"t": <seconds>, "title": "..."}]` }], 1500);
  const m = r.text.match(/\[[\s\S]*\]/);
  if (!m) throw new Error('Claude did not return chapters');
  const list = JSON.parse(m[0]).map(c => ({ t: Math.max(0, Math.round(+c.t || 0)), title: String(c.title || '').trim() })).filter(c => c.title).sort((a, b) => a.t - b.t);
  if (list.length) list[0].t = 0;
  if (!validChapters(list, total)) throw new Error("Claude's chapters break YouTube's rules (3+, first at 0:00, 10 s each)");
  return list;
}
