// Draws subtitles on a canvas. Used for both the live preview and the exported video,
// so what you see is what you get.
import { captionWords } from './core.js';

export const FONT = '"Bricolage Grotesque", system-ui, -apple-system, sans-serif';
const PAIR_GAP = 1.0; // seconds between a line and its translation to show them together

/**
 * Which captions are on screen at time t (original-video time).
 * layout "single": one line at a time. layout "pair": a main-language line stays up and
 * its second-language translation appears under it.
 */
export function blocksAt(t, caps, layout, primary) {
  const i = caps.findIndex(c => t >= c.start && t < c.end && c.text.trim());
  if (layout !== 'pair') return i >= 0 ? [caps[i]] : [];
  const isPair = (p, n) => p && n && p.lang === primary && n.lang !== primary && n.start - p.end <= PAIR_GAP;
  if (i >= 0) {
    const c = caps[i];
    if (c.lang !== primary && isPair(caps[i - 1], c)) return [caps[i - 1], c];
    return [c];
  }
  // In the short gap between a line and its translation, keep the line up.
  const prev = caps.findLastIndex(c => c.end <= t);
  if (prev >= 0 && isPair(caps[prev], caps[prev + 1]) && t < caps[prev + 1].start) return [caps[prev]];
  return [];
}

/**
 * style: { size (fraction of the short side), pos (0 bottom..1 top), box, highlight, caps,
 *          colorFor(lang) }
 */
export function drawCaptions(ctx, W, H, t, blocks, style) {
  if (!blocks.length) return;
  const px = Math.round(Math.min(W, H) * style.size);
  const lh = px * 1.18, maxW = W * 0.86, blockGap = px * 0.3;
  ctx.font = `800 ${px}px ${FONT}`;
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';
  ctx.lineJoin = 'round';
  const space = ctx.measureText(' ').width + px * 0.12;

  // Lay out every block into lines of words.
  const laid = blocks.map(c => {
    const words = captionWords(c).map(w => {
      const text = style.caps ? safeUpper(w.text, c.lang) : w.text;
      return { ...w, text, width: ctx.measureText(text).width };
    });
    const lines = [];
    let cur = [], width = 0;
    for (const w of words) {
      const add = cur.length ? space + w.width : w.width;
      if (cur.length && width + add > maxW) { lines.push({ words: cur, width }); cur = [w]; width = w.width; }
      else { cur.push(w); width += add; }
    }
    if (cur.length) lines.push({ words: cur, width });
    return { c, lines };
  });
  const total = laid.reduce((a, b) => a + b.lines.length * lh, 0) + blockGap * (laid.length - 1);
  let y = H * (1 - style.pos) - total / 2 + lh / 2;

  for (const { c, lines } of laid) {
    const color = style.colorFor(c.lang);
    for (const line of lines) {
      let x = (W - line.width) / 2;
      if (style.box) {
        const padX = px * 0.28, padY = px * 0.1;
        ctx.fillStyle = 'rgba(0,0,0,0.62)';
        roundRect(ctx, x - padX, y - lh / 2 - padY / 2, line.width + padX * 2, lh + padY, px * 0.22);
        ctx.fill();
      }
      for (const w of line.words) {
        const active = t >= w.start && t < w.end;
        ctx.globalAlpha = style.highlight && !active ? 0.55 : 1;
        if (!style.box) {
          ctx.lineWidth = px * 0.18;
          ctx.strokeStyle = '#000';
          ctx.strokeText(w.text, x, y);
        }
        ctx.fillStyle = color;
        if (style.highlight && active) {
          // Active word pops a little.
          ctx.save();
          ctx.translate(x + w.width / 2, y);
          ctx.scale(1.05, 1.05);
          if (!style.box) ctx.strokeText(w.text, -w.width / 2, 0);
          ctx.fillText(w.text, -w.width / 2, 0);
          ctx.restore();
        } else {
          ctx.fillText(w.text, x, y);
        }
        x += w.width + space;
      }
      ctx.globalAlpha = 1;
      y += lh;
    }
    y += blockGap;
  }
}

export function safeUpper(text, lang) {
  try { return text.toLocaleUpperCase(lang); } catch { return text.toUpperCase(); }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
