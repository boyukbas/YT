// Optional cloud speech recognition with OpenAI (your own API key, stored only on this device).
// The audio of the video is sent to OpenAI; everything else stays on the device.
import { SR, splitPoints, dropRepeats } from './core.js';

const KEY = 'openaiApiKey';
export const cleanKey = (k) => (k || '').replace(/[\s"'“”‘’]/g, '');
export const getKey = () => { try { return localStorage.getItem(KEY) || ''; } catch { return ''; } };
export const setKey = (k) => { try { k ? localStorage.setItem(KEY, cleanKey(k)) : localStorage.removeItem(KEY); } catch {} };
export function keyShape(raw) {
  const k = cleanKey(raw);
  return { length: k.length, start: k.slice(0, 8), hadSpacesOrQuotes: (raw || '').length !== k.length,
    looksLikeOpenAIKey: /^sk-/.test(k) && !k.startsWith('sk-ant'), looksLikeClaudeKey: k.startsWith('sk-ant') };
}

/** Checks the key (this endpoint gives readable errors in the browser). */
export async function testKey(key) {
  let r;
  try { r = await fetch('https://api.openai.com/v1/models', { headers: { Authorization: `Bearer ${key}` } }); }
  catch { throw new Error('Could not reach OpenAI (check the internet connection)'); }
  if (r.ok) return true;
  const data = await r.json().catch(() => ({}));
  throw new Error(r.status === 401 ? 'The OpenAI key was rejected' : `OpenAI error: ${data?.error?.message || r.status}`);
}

/** 16 kHz mono float samples -> 16-bit WAV file. */
function wav(samples) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, SR, true); v.setUint32(28, SR * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, samples[i])) * 0x7fff, true);
  return new Blob([buf], { type: 'audio/wav' });
}

async function transcribePiece(key, samples, offset, prompt, primary, forceLang) {
  const fd = new FormData();
  fd.append('model', 'whisper-1');
  fd.append('file', wav(samples), 'audio.wav');
  fd.append('response_format', 'verbose_json');
  fd.append('timestamp_granularities[]', 'word');
  fd.append('timestamp_granularities[]', 'segment');
  if (prompt) fd.append('prompt', prompt);
  if (forceLang) fd.append('language', primary);
  let lastErr;
  for (let attempt = 0; attempt < 3; attempt++) {
    let r;
    try {
      r = await fetch('https://api.openai.com/v1/audio/transcriptions', { method: 'POST', headers: { Authorization: `Bearer ${key}` }, body: fd });
    } catch {
      // OpenAI hides error details from browsers on this endpoint; a wrong key looks like a network error.
      lastErr = new Error('OpenAI did not answer — check the key with “Test key” and the internet connection');
      await new Promise(res => setTimeout(res, 1500 * (attempt + 1)));
      continue;
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = data?.error?.message || `HTTP ${r.status}`;
      if (r.status === 401) throw new Error('The OpenAI key was rejected');
      if (r.status === 429) throw new Error('OpenAI: rate limit or no credit left — ' + msg);
      lastErr = new Error('OpenAI error: ' + msg);
      if (r.status < 500) throw lastErr;
      await new Promise(res => setTimeout(res, 1500 * (attempt + 1)));
      continue;
    }
    const words = (data.words || []).map(w => ({ text: w.word.trim(), start: offset + w.start, end: offset + w.end, lang: primary }))
      .filter(w => w.text);
    return { words, text: data.text || '', language: data.language };
  }
  throw lastErr;
}

/**
 * Transcribes the whole audio in the cloud (in pieces of up to ~10 minutes).
 * Word timings come from OpenAI; punctuation is taken from its full text where it matches.
 * Languages are not labeled here (all words get the main language) — Claude or the
 * line editor labels them afterwards.
 */
export async function transcribe({ key, audio, primary, secondary, names, glossary, onStatus = () => {} }) {
  const cuts = splitPoints(audio, 600, 30);
  const hint = [
    secondary ? `${names.primary} and ${names.secondary}, mixed in the same sentences.` : '',
    glossary?.trim() ? glossary.trim().split('\n').slice(0, 30).join(', ') : '',
  ].filter(Boolean).join(' ');
  const words = [];
  for (let k = 0; k < cuts.length - 1; k++) {
    onStatus(k + 1, cuts.length - 1);
    const s = cuts[k], e = cuts[k + 1];
    const piece = await transcribePiece(key, audio.subarray(Math.floor(s * SR), Math.floor(e * SR)), s, hint, primary, !secondary);
    words.push(...withPunctuation(piece.words, piece.text));
  }
  return dropRepeats(words);
}

/** whisper-1's word list has no punctuation; copy it over from the full text. */
function withPunctuation(words, text) {
  const tokens = text.split(/\s+/).filter(Boolean);
  const norm = (s) => s.toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
  let j = 0;
  return words.map(w => {
    for (let look = j; look < Math.min(tokens.length, j + 4); look++) {
      if (norm(tokens[look]) === norm(w.text)) { j = look + 1; return { ...w, text: tokens[look] }; }
    }
    return w;
  });
}
