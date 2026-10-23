// Draws subtitles on a canvas. Used for both the live preview and the exported video,
// so what you see is what you get.
import { captionWords } from './core.js';

export const FONTS = {
  modern: { family: '"Bricolage Grotesque", system-ui, -apple-system, sans-serif', weight: 800, label: 'Modern' },
  bold: { family: 'Anton, Impact, "Arial Narrow", sans-serif', weight: 400, label: 'Bold' },
  rounded: { family: 'Nunito, ui-rounded, system-ui, sans-serif', weight: 900, label: 'Rounded' },
};
export const FONT = FONTS.modern.family;
export const fontCss = (key, px) => { const f = FONTS[key] || FONTS.modern; return `${f.weight} ${px}px ${f.family}`; };

const PAIR_GAP = 1.0; // seconds between a line and its translation to show them together
const POP = 0.14;     // seconds for the pop-in animation

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
  const prev = caps.findLastIndex(c => c.end <= t);
  if (prev >= 0 && isPair(caps[prev], caps[prev + 1]) && t < caps[prev + 1].start) return [caps[prev]];
  return [];
}

function layoutWords(ctx, words, maxW, space) {
  const lines = [];
  let cur = [], width = 0;
  for (const w of words) {
    const add = cur.length ? space + w.width : w.width;
    if (cur.length && width + add > maxW) { lines.push({ words: cur, width }); cur = [w]; width = w.width; }
    else { cur.push(w); width += add; }
  }
  if (cur.length) lines.push({ words: cur, width });
  return lines;
}

/**
 * style: { size (fraction of the short side), pos (0 bottom..1 top), posX (0 left..1 right),
 *          box, highlight, caps, font, anim, colorFor(lang) }
 */
export function drawCaptions(ctx, W, H, t, blocks, style) {
  if (!blocks.length) return;
  const px = Math.round(Math.min(W, H) * style.size);
  const lh = px * 1.18, blockGap = px * 0.3;
  const cx = W * (style.posX ?? 0.5);
  // Keep lines inside the frame even when the subtitles are moved to a side.
  const maxW = Math.max(W * 0.35, Math.min(W * 0.86, 2 * Math.min(cx, W - cx) * 0.94));
  ctx.font = fontCss(style.font, px);
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';
  ctx.lineJoin = 'round';
  const space = ctx.measureText(' ').width + px * 0.12;

  const laid = blocks.map(c => {
    const words = captionWords(c).map(w => {
      const text = style.caps ? safeUpper(w.text, c.lang) : w.text;
      return { ...w, text, width: ctx.measureText(text).width };
    });
    return { c, lines: layoutWords(ctx, words, maxW, space) };
  });
  const total = laid.reduce((a, b) => a + b.lines.length * lh, 0) + blockGap * (laid.length - 1);
  let y = H * (1 - style.pos) - total / 2 + lh / 2;

  for (const { c, lines } of laid) {
    const color = style.colorFor(c.lang);
    const blockH = lines.length * lh;
    ctx.save();
    if (style.anim) {
      // Pop-in when a line appears.
      const k = Math.min(1, Math.max(0, (t - c.start) / POP));
      const s = 0.82 + 0.18 * (1 - (1 - k) * (1 - k));
      const midY = y - lh / 2 + blockH / 2;
      ctx.translate(cx, midY); ctx.scale(s, s); ctx.translate(-cx, -midY);
      ctx.globalAlpha = 0.4 + 0.6 * k;
    }
    const baseAlpha = ctx.globalAlpha;
    for (const line of lines) {
      let x = cx - line.width / 2;
      if (style.box) {
        const padX = px * 0.28, padY = px * 0.1;
        ctx.fillStyle = 'rgba(0,0,0,0.62)';
        roundRect(ctx, x - padX, y - lh / 2 - padY / 2, line.width + padX * 2, lh + padY, px * 0.22);
        ctx.fill();
      }
      for (const w of line.words) {
        const active = t >= w.start && t < w.end;
        ctx.globalAlpha = baseAlpha * (style.highlight && !active ? 0.55 : 1);
        ctx.lineWidth = px * 0.18;
        ctx.strokeStyle = '#000';
        ctx.fillStyle = color;
        if (style.highlight && active) {
          ctx.save();
          ctx.translate(x + w.width / 2, y);
          ctx.scale(1.05, 1.05);
          if (!style.box) ctx.strokeText(w.text, -w.width / 2, 0);
          ctx.fillText(w.text, -w.width / 2, 0);
          ctx.restore();
        } else {
          if (!style.box) ctx.strokeText(w.text, x, y);
          ctx.fillText(w.text, x, y);
        }
        x += w.width + space;
      }
      y += lh;
    }
    ctx.restore();
    y += blockGap;
  }
}

/** Opening title ("hook") shown near the top for the first seconds of the video. */
export function drawHook(ctx, W, H, text, outT, style) {
  if (!text.trim()) return;
  const px = Math.round(Math.min(W, H) * style.size * 1.15);
  const lh = px * 1.15;
  ctx.save();
  ctx.font = fontCss(style.font, px);
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';
  ctx.lineJoin = 'round';
  const space = ctx.measureText(' ').width;
  const lines = [];
  for (const para of text.split('\n')) {
    const words = para.split(/\s+/).filter(Boolean).map(w => ({ text: w, width: ctx.measureText(w).width }));
    lines.push(...layoutWords(ctx, words, W * 0.84, space));
  }
  const k = style.anim ? Math.min(1, outT / POP) : 1;
  ctx.globalAlpha = 0.3 + 0.7 * k;
  let y = H * 0.17 - (lines.length - 1) * lh / 2;
  const padX = px * 0.35, padY = px * 0.18;
  for (const line of lines) {
    let x = (W - line.width) / 2;
    ctx.fillStyle = 'rgba(0,0,0,0.7)';
    roundRect(ctx, x - padX, y - lh / 2 - padY / 2, line.width + padX * 2, lh + padY, px * 0.25);
    ctx.fill();
    ctx.fillStyle = '#fff';
    for (const w of line.words) { ctx.fillText(w.text, x, y); x += w.width + space; }
    y += lh;
  }
  ctx.restore();
}

/** Shades the areas YouTube Shorts covers with its own buttons and text (preview only). */
export function drawSafeZones(ctx, W, H) {
  ctx.save();
  ctx.fillStyle = 'rgba(255, 90, 90, 0.22)';
  ctx.strokeStyle = 'rgba(255, 120, 120, 0.9)';
  ctx.setLineDash([6, 6]);
  ctx.lineWidth = 2;
  const zones = [
    [0, 0, W, H * 0.08],                    // top bar
    [0, H * 0.79, W, H * 0.21],             // title, channel, music
    [W * 0.84, H * 0.42, W * 0.16, H * 0.37], // like / comment / share buttons
  ];
  for (const [x, y, w, h] of zones) { ctx.fillRect(x, y, w, h); ctx.strokeRect(x + 1, y + 1, w - 2, h - 2); }
  ctx.restore();
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
