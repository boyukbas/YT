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
