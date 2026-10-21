// Speech-to-subtitle logic shared by the browser worker (and testable in Node).
// Works on 16 kHz mono Float32Array audio.

export const SR = 16000;

/** Whisper language codes -> names the pipeline accepts. */
export const LANGS = {
  de: 'german', tr: 'turkish', en: 'english', fr: 'french', es: 'spanish', it: 'italian',
  nl: 'dutch', pt: 'portuguese', pl: 'polish', ru: 'russian', uk: 'ukrainian', ar: 'arabic',
  fa: 'persian', ku: 'kurdish', az: 'azerbaijani', el: 'greek', sv: 'swedish', ja: 'japanese',
  ko: 'korean', zh: 'chinese', hi: 'hindi',
};

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

/**
 * Asks Whisper "which of these languages is this clip?" restricted to the given codes.
 * One encoder pass + one decoder step. Returns {code: probability}.
 */
export async function languageOdds(model, processor, tokenizer, Tensor, clip, codes) {
  const { input_features } = await processor(clip);
  const sot = tokenizer.convert_tokens_to_ids(['<|startoftranscript|>'])[0];
  const ids = tokenizer.convert_tokens_to_ids(codes.map(c => `<|${c}|>`));
  const decoder_input_ids = new Tensor('int64', BigInt64Array.from([BigInt(sot)]), [1, 1]);
  const { logits } = await model({ input_features, decoder_input_ids });
  const vocab = logits.dims.at(-1);
  const data = logits.data;
  const base = data.length - vocab;
  const raw = ids.map(id => Number(data[base + id]));
  const max = Math.max(...raw);
  const exp = raw.map(v => Math.exp(v - max));
  const sum = exp.reduce((a, b) => a + b, 0);
  return Object.fromEntries(codes.map((c, i) => [c, exp[i] / sum]));
}

/**
 * How naturally Whisper would write `text` for this clip in `lang`: average log-probability
 * of the text's tokens when forced through the decoder (higher = more confident).
 */
export async function scoreText(model, processor, tokenizer, Tensor, clip, lang, text) {
  const body = tokenizer.encode(' ' + text.trim(), { add_special_tokens: false });
  if (!body.length) return -Infinity;
  const prefix = tokenizer.convert_tokens_to_ids(
    ['<|startoftranscript|>', `<|${lang}|>`, '<|transcribe|>', '<|notimestamps|>']);
  const eot = tokenizer.convert_tokens_to_ids(['<|endoftext|>'])[0];
  const seq = [...prefix, ...body, eot];
  const { input_features } = await processor(clip);
  const decoder_input_ids = new Tensor('int64', BigInt64Array.from(seq.slice(0, -1).map(BigInt)), [1, seq.length - 1]);
  const { logits } = await model({ input_features, decoder_input_ids });
  const V = logits.dims.at(-1);
  const data = logits.data;
  let total = 0, n = 0;
  for (let pos = prefix.length - 1; pos < seq.length - 1; pos++) {
    const row = pos * V;
    let max = -Infinity;
    for (let v = 0; v < V; v++) if (data[row + v] > max) max = data[row + v];
    let sum = 0;
    for (let v = 0; v < V; v++) sum += Math.exp(data[row + v] - max);
    total += data[row + seq[pos + 1]] - max - Math.log(sum);
    n++;
  }
  return total / n;
}

/**
 * Full pipeline: main-language pass, then per-phrase language check, then re-transcribe
 * second-language phrases in that language.
 * `prefilter`: phrases below this second-language chance are kept as-is (saves time).
 * `sure`: above this chance, trust the language guess without the extra scoring pass.
 * `margin`: how much more confident the second-language version must be to win.
 */
export async function transcribeMixed({ asr, Tensor, audio, primary, secondary, prefilter = 0.1, sure = 0.97, margin = 0.3, onStatus = () => {} }) {
  onStatus('main');
  const words = await transcribeWords(asr, audio, primary);
  if (!secondary || secondary === primary || !words.length) return words;

  onStatus('detect');
  const groups = phrases(words);
  const duration = audio.length / SR;
  const result = [];
  for (let i = 0; i < groups.length; i++) {
    onStatus('detect', i + 1, groups.length);
    const g = groups[i];
    // Pad the clip a little, but never into the neighbouring phrases' words.
    const prevEnd = i > 0 ? groups[i - 1].at(-1).end : 0;
    const nextStart = i + 1 < groups.length ? groups[i + 1][0].start : duration;
    const s = Math.max(0, prevEnd, g[0].start - 0.15);
    const e = Math.min(duration, nextStart, g.at(-1).end + 0.15);
    const clip = audio.subarray(Math.floor(s * SR), Math.max(Math.floor(s * SR) + 1, Math.floor(e * SR)));
    const odds = await languageOdds(asr.model, asr.processor, asr.tokenizer, Tensor, clip, [primary, secondary]);
    let switched = false;
    if (odds[secondary] >= prefilter) {
      // Candidate: transcribe it in the second language, then let Whisper score both versions.
      let redo = await transcribeWords(asr, clip, secondary, s);
      const redoText = redo.map(w => w.text).join(' ');
      if (redoText.trim() && odds[secondary] >= sure) {
        // Whisper is already very sure: skip the slower double check.
        result.push(...redo.map(w => ({ ...w, start: Math.min(Math.max(w.start, s), e), end: Math.min(Math.max(w.end, w.start), e) })));
        switched = true;
      } else if (redoText.trim()) {
        const mainText = g.map(w => w.text).join(' ');
        const [sMain, sSec] = [
          await scoreText(asr.model, asr.processor, asr.tokenizer, Tensor, clip, primary, mainText),
          await scoreText(asr.model, asr.processor, asr.tokenizer, Tensor, clip, secondary, redoText),
        ];
        if (sSec > sMain + margin) {
          redo = redo.map(w => ({ ...w, start: Math.min(Math.max(w.start, s), e), end: Math.min(Math.max(w.end, w.start), e) }));
          result.push(...redo);
          switched = true;
        }
      }
    }
    if (!switched) result.push(...g);
  }
  return result.sort((a, b) => a.start - b.start);
}

/** Groups words into short captions; a caption never mixes languages. */
export function buildCaptions(words, maxWords = 3) {
  const caps = [];
  let cur = [];
  const flush = () => {
    if (!cur.length) return;
    caps.push({ id: crypto.randomUUID(), start: cur[0].start, end: cur.at(-1).end,
                text: cur.map(w => w.text).join(' '), lang: cur[0].lang });
    cur = [];
  };
  for (const w of words) {
    const last = cur.at(-1);
    if (last && (w.lang !== last.lang || w.start - last.end > 0.6 || cur.length >= maxWords)) flush();
    cur.push(w);
    if (/[.!?,;:]$/.test(w.text)) flush();
  }
  flush();
  for (let i = 0; i < caps.length; i++) {
    const next = i + 1 < caps.length ? caps[i + 1].start : Infinity;
    if (next - caps[i].end < 0.4) caps[i].end = Math.min(next, caps[i].end + 0.4);
    if (caps[i].end - caps[i].start < 0.35) caps[i].end = Math.min(caps[i].start + 0.35, next);
  }
  return caps;
}

export function toSRT(caps) {
  const stamp = t => {
    const ms = Math.round(t * 1000);
    const p = (n, l = 2) => String(n).padStart(l, '0');
    return `${p(Math.floor(ms / 3600000))}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)},${p(ms % 1000, 3)}`;
  };
  return caps.map((c, i) => `${i + 1}\n${stamp(c.start)} --> ${stamp(c.end)}\n${c.text}\n`).join('\n');
}
