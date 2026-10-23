// Speech-to-subtitle logic shared by the browser worker (and testable in Node).
// Works on 16 kHz mono Float32Array audio.

export const SR = 16000;

/** Whisper language codes -> names the pipeline accepts. */
export const LANGS = {
  de: 'german', tr: 'turkish', en: 'english', fr: 'french', es: 'spanish', it: 'italian',
  nl: 'dutch', pt: 'portuguese', pl: 'polish', ru: 'russian', uk: 'ukrainian', ar: 'arabic',
  fa: 'persian', az: 'azerbaijani', el: 'greek', sv: 'swedish', ja: 'japanese',
  ko: 'korean', zh: 'chinese', hi: 'hindi',
};

// ---------- Text helpers ----------

/** Lowercase, strip accents and punctuation, so "Ormanda." ~ "ormanda". */
export function fold(s) {
  return s.toLocaleLowerCase('tr').replace(/ı/g, 'i').replace(/ß/g, 'ss')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^\p{L}\p{N} ]/gu, '').trim();
}
function lev(a, b) {
  if (a === b) return 0;
  const m = a.length, n = b.length;
  if (!m) return n; if (!n) return m;
  let prev = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const cur = [i];
    for (let j = 1; j <= n; j++) cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    prev = cur;
  }
  return prev[n];
}
/** 0..1 similarity of two words/phrases after folding. */
export function similarity(a, b) {
  const x = fold(a), y = fold(b);
  if (!x || !y) return 0;
  return 1 - lev(x, y) / Math.max(x.length, y.length);
}
const trailingPunct = (s) => (s.match(/[.!?,;:…]+$/) || [''])[0];
const capitalize = (s, lang) => s.charAt(0).toLocaleUpperCase(lang) + s.slice(1);

// ---------- Whisper helpers ----------

/** Transcribe audio in one language. Returns words with times in seconds (offset added). */
export async function transcribeWords(asr, audio, lang, offset = 0) {
  const out = await asr(audio, {
    language: LANGS[lang] || lang, task: 'transcribe',
    return_timestamps: 'word', chunk_length_s: 30, stride_length_s: 5,
  });
  const words = [];
  for (const c of out.chunks || []) {
    const text = c.text.trim();
    if (!text) continue;
    let [s, e] = c.timestamp;
    if (e == null) e = s + 0.3;
    words.push({ text, start: offset + s, end: offset + e, lang });
  }
  return words;
}

/**
 * Runs Whisper's encoder once for a clip, so many "how likely is this text?" checks
 * only need the (cheap) decoder. Falls back to recomputing if the session API differs.
 */
export async function encodeClip(asr, Tensor, clip) {
  const { input_features } = await asr.processor(clip);
  try {
    const out = await asr.model.sessions.model.run({ input_features: input_features.ort_tensor ?? input_features });
    return { input_features, enc: new Tensor(out.last_hidden_state) };
  } catch {
    return { input_features, enc: null };
  }
}
async function decode(asr, ctx, Tensor, ids) {
  const decoder_input_ids = new Tensor('int64', BigInt64Array.from(ids.map(BigInt)), [1, ids.length]);
  const inputs = ctx.enc ? { encoder_outputs: ctx.enc, decoder_input_ids } : { input_features: ctx.input_features, decoder_input_ids };
  return (await asr.model(inputs)).logits;
}

/** Restricted language ID: {code: probability} among the given codes. */
export async function languageOdds(asr, ctx, Tensor, codes) {
  const tok = asr.tokenizer;
  const sot = tok.convert_tokens_to_ids(['<|startoftranscript|>'])[0];
  const ids = tok.convert_tokens_to_ids(codes.map(c => `<|${c}|>`));
  const logits = await decode(asr, ctx, Tensor, [sot]);
  const V = logits.dims.at(-1), base = logits.data.length - V;
  const raw = ids.map(id => Number(logits.data[base + id]));
  const max = Math.max(...raw), exp = raw.map(v => Math.exp(v - max)), sum = exp.reduce((a, b) => a + b, 0);
  return Object.fromEntries(codes.map((c, i) => [c, exp[i] / sum]));
}

/** Average log-probability Whisper gives `text` for this clip in `lang` (higher = more natural). */
export async function scoreText(asr, ctx, Tensor, lang, text) {
  const tok = asr.tokenizer;
  const body = tok.encode(' ' + text.trim(), { add_special_tokens: false });
  if (!body.length) return -Infinity;
  const prefix = tok.convert_tokens_to_ids(['<|startoftranscript|>', `<|${lang}|>`, '<|transcribe|>', '<|notimestamps|>']);
  const eot = tok.convert_tokens_to_ids(['<|endoftext|>'])[0];
  const seq = [...prefix, ...body, eot];
  const logits = await decode(asr, ctx, Tensor, seq.slice(0, -1));
  const V = logits.dims.at(-1), d = logits.data;
  let total = 0, n = 0;
  for (let pos = prefix.length - 1; pos < seq.length - 1; pos++) {
    const row = pos * V;
    let max = -Infinity;
    for (let v = 0; v < V; v++) if (d[row + v] > max) max = d[row + v];
    let sum = 0;
    for (let v = 0; v < V; v++) sum += Math.exp(d[row + v] - max);
    total += d[row + seq[pos + 1]] - max - Math.log(sum);
    n++;
  }
  return total / n;
}

/** Splits words into short phrases at punctuation and pauses. */
export function phrases(words) {
  const res = [];
  let cur = [];
  words.forEach((w, i) => {
    cur.push(w);
    const punct = /[.!?,;:–-]$/.test(w.text);
    const gap = i + 1 < words.length ? words[i + 1].start - w.end : Infinity;
    if (punct || gap > 0.25) { res.push(cur); cur = []; }
  });
  if (cur.length) res.push(cur);
  return res;
}

// ---------- Word list (glossary) ----------

/**
 * "Wald – orman" per line (also "=", ":", "→", tab or " - "). Returns
 * {primary: [...terms], secondary: [...terms]}. A line without a separator goes in both.
 */
export function parseGlossary(text) {
  const primary = [], secondary = [];
  for (const raw of (text || '').split(/\n|;/)) {
    const line = raw.trim();
    if (!line) continue;
    const parts = line.split(/\s*(?:–|—|=|→|:|\t| - )\s*/).map(s => s.trim()).filter(Boolean);
    if (parts.length >= 2) { primary.push(parts[0]); secondary.push(parts.slice(1).join(' ')); }
    else { primary.push(parts[0]); secondary.push(parts[0]); }
  }
  // Also accept "der Wald" -> "Wald" so articles don't block matches.
  const strip = t => t.replace(/^(der|die|das|den|dem|des|ein|eine|einen)\s+/i, '');
  for (const t of [...primary]) { const s = strip(t); if (s !== t) primary.push(s); }
  return { primary: [...new Set(primary)], secondary: [...new Set(secondary)] };
}

/**
 * Fixes misheard words in a phrase using the word list. Whisper scores candidates against
 * the audio: the whole phrase replaced by a term (e.g. "Hoşçakalın" -> "boş zaman"), or one
 * or two words replaced by a similar-looking term (e.g. "Amanda" -> "ormanda").
 */
export async function applyGlossary(asr, ctx, Tensor, lang, words, terms, allowWhole = true) {
  if (!terms.length || !words.length) return words;
  const original = words.map(w => w.text).join(' ');
  const base = await scoreText(asr, ctx, Tensor, lang, original);
  const end = trailingPunct(words.at(-1).text);
  const styled = (t, first) => {
    const v = [t];
    if (first && /^\p{Lu}/u.test(words[0].text)) v.push(capitalize(t, lang));
    return v;
  };
  let best = { score: -Infinity };

  // Whole phrase = one term (only for short phrases).
  const tried = new Set();
  for (const term of allowWhole && words.length <= 3 ? terms : []) {
    for (const t of styled(term, true)) {
      const text = t + end;
      if (tried.has(text)) continue;
      tried.add(text);
      const s = await scoreText(asr, ctx, Tensor, lang, text);
      if (s > best.score) best = { score: s, text, whole: true };
    }
  }
  // One or two words swapped for a look-alike term.
  for (let i = 0; i < words.length; i++) {
    for (const n of [1, 2]) {
      if (i + n > words.length) continue;
      const span = words.slice(i, i + n).map(w => w.text).join(' ');
      for (const term of terms) {
        if (similarity(span, term) < 0.45) continue;
        for (const t of styled(term, i === 0)) {
          const repl = t + trailingPunct(words[i + n - 1].text);
          const text = [...words.slice(0, i).map(w => w.text), repl, ...words.slice(i + n).map(w => w.text)].join(' ');
          if (tried.has(text) || text === original) continue;
          tried.add(text);
          const s = await scoreText(asr, ctx, Tensor, lang, text);
          if (s > best.score) best = { score: s, text, i, n, repl };
        }
      }
    }
  }
  // Accept if plausible on its own and not much less natural than Whisper's guess.
  if (best.score < -2.5 || best.score < base - 1.0) return words;
  if (best.whole) {
    return [{ text: best.text, start: words[0].start, end: words.at(-1).end, lang }];
  }
  const out = [...words];
  out.splice(best.i, best.n, { text: best.repl, start: words[best.i].start, end: words[best.i + best.n - 1].end, lang });
  return out;
}

// ---------- Full pipeline ----------

/**
 * Main-language pass, per-phrase language check, re-transcription of second-language
 * phrases (optionally with a bigger model), then word-list fixes.
 */
export async function transcribeMixed({
  asr, asrSecondary = null, Tensor, audio, primary, secondary, glossary = '',
  prefilter = 0.1, sure = 0.97, margin = 0.3, onStatus = () => {},
}) {
  const terms = parseGlossary(glossary);
  onStatus('main');
  const words = await transcribeWords(asr, audio, primary);
  if (!words.length) return words;
  const two = secondary && secondary !== primary;
  const groups = phrases(words);
  const duration = audio.length / SR;
  const result = [];
  const fixer = asrSecondary || asr;

  for (let i = 0; i < groups.length; i++) {
    onStatus('detect', i + 1, groups.length);
    const g = groups[i];
    const prevEnd = i > 0 ? groups[i - 1].at(-1).end : 0;
    const nextStart = i + 1 < groups.length ? groups[i + 1][0].start : duration;
    const s = Math.max(0, prevEnd, g[0].start - 0.15);
    const e = Math.min(duration, nextStart, g.at(-1).end + 0.15);
    const a = Math.floor(s * SR);
    const clip = audio.subarray(a, Math.max(a + 1, Math.floor(e * SR)));
    const clamp = w => ({ ...w, start: Math.min(Math.max(w.start, s), e), end: Math.min(Math.max(w.end, w.start), e) });

    let chosen = g, lang = primary, ctx = null;
    if (two) {
      ctx = await encodeClip(asr, Tensor, clip);
      const odds = await languageOdds(asr, ctx, Tensor, [primary, secondary]);
      if (odds[secondary] >= prefilter) {
        const redo = (await transcribeWords(fixer, clip, secondary, s)).map(clamp);
        const redoText = redo.map(w => w.text).join(' ');
        if (redoText.trim()) {
          let switchIt = odds[secondary] >= sure;
          if (!switchIt) {
            const sMain = await scoreText(asr, ctx, Tensor, primary, g.map(w => w.text).join(' '));
            const sSec = await scoreText(asr, ctx, Tensor, secondary, redoText);
            switchIt = sSec > sMain + margin;
          }
          if (switchIt) { chosen = redo; lang = secondary; }
        }
      }
    }
    // Word list fixes only for the second language (or the only language): Whisper's main
    // language is usually right, and swapping there does more harm than good.
    const list = two ? (lang === secondary ? terms.secondary : []) : terms.primary.concat(terms.secondary);
    if (list.length) {
      const fctx = lang === secondary && asrSecondary ? await encodeClip(fixer, Tensor, clip) : (ctx || await encodeClip(asr, Tensor, clip));
      chosen = await applyGlossary(lang === secondary ? fixer : asr, fctx, Tensor, lang, chosen, list, two);
    }
    result.push(...chosen);
  }
  return result.sort((x, y) => x.start - y.start);
}

// ---------- Script mode ----------

/**
 * Puts the user's own script on the recognized timings: script words are matched to
 * recognized words (fuzzy, in order); unmatched words get times in between.
 * Line breaks in the script become caption breaks.
 */
export function alignScript(script, words, primary, secondary) {
  const tokens = [];
  for (const line of script.split('\n')) {
    const parts = line.trim().split(/\s+/).filter(Boolean);
    parts.forEach((p, i) => tokens.push({ text: p, lineEnd: i === parts.length - 1 }));
  }
  if (!tokens.length) return words;
  if (!words.length) return [];
  const S = tokens.length, R = words.length;
  const GAP = -0.4;
  const sim = Array.from({ length: S }, (_, i) => words.map(w => similarity(tokens[i].text, w.text)));
  const score = Array.from({ length: S + 1 }, () => new Float32Array(R + 1));
  const move = Array.from({ length: S + 1 }, () => new Uint8Array(R + 1)); // 1 diag, 2 up, 3 left
  for (let i = 1; i <= S; i++) { score[i][0] = i * GAP; move[i][0] = 2; }
  // Skipping recognized words before/after the script is free (the script may cover only part).
  for (let j = 1; j <= R; j++) { score[0][j] = 0; move[0][j] = 3; }
  for (let i = 1; i <= S; i++) {
    for (let j = 1; j <= R; j++) {
      const s = sim[i - 1][j - 1];
      const d = score[i - 1][j - 1] + (s >= 0.5 ? s * 2 : -1);
      const u = score[i - 1][j] + GAP, l = score[i][j - 1] + GAP;
      if (d >= u && d >= l) { score[i][j] = d; move[i][j] = 1; }
      else if (u >= l) { score[i][j] = u; move[i][j] = 2; }
      else { score[i][j] = l; move[i][j] = 3; }
    }
  }
  const match = new Array(S).fill(-1);
  let bestJ = 0;
  for (let j = 1; j <= R; j++) if (score[S][j] > score[S][bestJ]) bestJ = j;
  for (let i = S, j = bestJ; i > 0 || j > 0;) {
    const m = move[i][j];
    if (m === 1) { if (sim[i - 1][j - 1] >= 0.5) match[i - 1] = j - 1; i--; j--; }
    else if (m === 2) i--;
    else j--;
  }
  const out = tokens.map((t, i) => {
    const w = match[i] >= 0 ? words[match[i]] : null;
    return { text: t.text, lineEnd: t.lineEnd, start: w?.start, end: w?.end, lang: w?.lang };
  });
  // Fill times for unmatched runs between their matched neighbours.
  for (let i = 0; i < S;) {
    if (out[i].start != null) { i++; continue; }
    let j = i;
    while (j < S && out[j].start == null) j++;
    const from = i > 0 ? out[i - 1].end : words[0].start;
    const to = j < S ? out[j].start : words.at(-1).end;
    const step = Math.max(0.05, (to - from) / (j - i));
    for (let k = i; k < j; k++) { out[k].start = from + step * (k - i); out[k].end = out[k].start + step; }
    i = j;
  }
  // Language: from the matched word; otherwise letters that only one language uses; otherwise a neighbour.
  const hint = (text) => {
    if (secondary === 'tr' && /[ğıışçİĞŞÇ]/.test(text)) return 'tr';
    if (primary === 'de' && /[äßÄ]/.test(text)) return 'de';
    if (primary === 'tr' && /[ğıışçİĞŞÇ]/.test(text)) return 'tr';
    if (secondary === 'de' && /[äßÄ]/.test(text)) return 'de';
    return null;
  };
  for (let i = 0; i < S; i++) {
    if (!out[i].lang) out[i].lang = hint(out[i].text);
  }
  for (let i = 0; i < S; i++) {
    if (out[i].lang) continue;
    // At the start of a line or after a dash/full stop, the word belongs with what follows.
    const startsNew = i === 0 || out[i - 1].lineEnd || /[.!?–—-]$/.test(out[i - 1].text);
    if (startsNew) {
      for (let k = i + 1; k < S; k++) if (out[k].lang) { out[i].lang = out[k].lang; break; }
      if (out[i].lang) continue;
    }
    let k = 1;
    while (!out[i].lang && (i - k >= 0 || i + k < S)) {
      out[i].lang = (i - k >= 0 && out[i - k].lang) || (i + k < S && out[i + k].lang) || null;
      k++;
    }
    out[i].lang ||= primary;
  }
  for (let i = 1; i < S; i++) if (out[i].start < out[i - 1].start) out[i].start = out[i - 1].start;
  return out;
}

// ---------- Captions ----------

/** Groups words into short captions; a caption never mixes languages. Keeps word timings. */
export function buildCaptions(words, maxWords = 3) {
  const caps = [];
  let cur = [];
  const flush = () => {
    if (!cur.length) return;
    caps.push({
      id: (globalThis.crypto?.randomUUID?.() || String(Math.random()).slice(2)),
      start: cur[0].start, end: cur.at(-1).end,
      text: cur.map(w => w.text).join(' '), lang: cur[0].lang,
      words: cur.map(w => ({ text: w.text, start: w.start, end: w.end })),
    });
    cur = [];
  };
  for (const w of words) {
    const last = cur.at(-1);
    if (last && (w.lang !== last.lang || w.start - last.end > 0.6 || cur.length >= maxWords)) flush();
    cur.push(w);
    if (/[.!?,;:]$/.test(w.text) || w.lineEnd) flush();
  }
  flush();
  for (let i = 0; i < caps.length; i++) {
    const next = i + 1 < caps.length ? caps[i + 1].start : Infinity;
    if (next - caps[i].end < 0.4) caps[i].end = Math.min(next, caps[i].end + 0.4);
    if (caps[i].end - caps[i].start < 0.35) caps[i].end = Math.min(caps[i].start + 0.35, next);
  }
  return caps;
}

/** Word timings for a caption, even after its text was edited (spread evenly if counts differ). */
export function captionWords(c) {
  const parts = c.text.trim().split(/\s+/).filter(Boolean);
  if (c.words && c.words.length === parts.length) return parts.map((t, i) => ({ ...c.words[i], text: t }));
  const step = (c.end - c.start) / Math.max(1, parts.length);
  return parts.map((t, i) => ({ text: t, start: c.start + i * step, end: c.start + (i + 1) * step }));
}

export function toSRT(caps) {
  const stamp = t => {
    const ms = Math.round(t * 1000);
    const p = (n, l = 2) => String(n).padStart(l, '0');
    return `${p(Math.floor(ms / 3600000))}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)},${p(ms % 1000, 3)}`;
  };
  return caps.map((c, i) => `${i + 1}\n${stamp(c.start)} --> ${stamp(c.end)}\n${c.text}\n`).join('\n');
}
