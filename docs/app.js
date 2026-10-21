import {
  Input, Output, BlobSource, BufferTarget, Mp4OutputFormat, ALL_FORMATS,
  CanvasSink, AudioBufferSink, CanvasSource, AudioBufferSource, QUALITY_HIGH,
  getFirstEncodableVideoCodec, getFirstEncodableAudioCodec,
} from 'https://cdn.jsdelivr.net/npm/mediabunny@1.61.3/dist/bundles/mediabunny.min.mjs';
import { LANGS, SR, buildCaptions, toSRT } from './core.js';

const $ = (id) => document.getElementById(id);

// ---------- Settings (remembered between visits) ----------
const COLORS = { white: '#ffffff', yellow: '#ffd21a', cyan: '#4de6ff', green: '#73ff73', pink: '#ff80cc', orange: '#ff9e33' };
const saved = (() => { try { return JSON.parse(localStorage.getItem('settings') || '{}'); } catch { return {}; } })();
const S = {
  primary: 'de', secondary: 'tr', color1: 'white', color2: 'yellow', caps: true, wordsPer: 3, pos: 0.35, burn: true,
  removePauses: true, minPause: 0.4, ...saved,
  // per-video
  file: null, url: null, duration: 0, width: 0, height: 0,
  audio16k: null, levels: [], threshold: -38, silences: [],
  trimStart: 0, trimEnd: 0, words: [], captions: [],
  videoBlob: null, srtBlob: null,
};
function persist() {
  const keep = ['primary', 'secondary', 'color1', 'color2', 'caps', 'wordsPer', 'pos', 'burn', 'removePauses', 'minPause'];
  try { localStorage.setItem('settings', JSON.stringify(Object.fromEntries(keep.map(k => [k, S[k]])))); } catch {}
}

const langName = (() => {
  let dn = null;
  try { dn = new Intl.DisplayNames([navigator.language, 'en'], { type: 'language' }); } catch {}
  return (code) => { let n = code; try { n = dn?.of(code) || LANGS[code] || code; } catch { n = LANGS[code] || code; } if (n.length <= 3) n = LANGS[code] || n; return n.charAt(0).toUpperCase() + n.slice(1); };
})();
const colorFor = (lang) => COLORS[lang === S.primary ? S.color1 : S.color2];
const activeLangs = () => (S.secondary && S.secondary !== S.primary ? [S.primary, S.secondary] : [S.primary]);
const upper = (text, lang) => { try { return text.toLocaleUpperCase(lang); } catch { return text.toUpperCase(); } };
const fmt = (t) => {
  if (!isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  return `${m}:${(t - m * 60).toFixed(1).padStart(4, '0')}`;
};

// ---------- Timeline: which parts of the original are kept ----------
function subtract(base, cuts) {
  let res = [base];
  for (const c of cuts) {
    res = res.flatMap(r => {
      if (c.end <= r.start || c.start >= r.end) return [r];
      const parts = [];
      if (c.start > r.start) parts.push({ start: r.start, end: c.start });
      if (c.end < r.end) parts.push({ start: c.end, end: r.end });
      return parts;
    });
  }
  return res.filter(r => r.end - r.start > 0.04);
}
const kept = () => subtract({ start: S.trimStart, end: S.trimEnd }, S.removePauses ? S.silences : []);
const outLength = () => kept().reduce((a, r) => a + r.end - r.start, 0);
function toOutput(t, ranges = kept()) {
  let acc = 0;
  for (const r of ranges) {
    if (t < r.start) return acc;
    if (t <= r.end) return acc + t - r.start;
    acc += r.end - r.start;
  }
  return acc;
}
/** Captions moved onto the edited timeline (cut-out ones dropped). */
function outputCaptions() {
  const ranges = kept();
  return S.captions.map(c => ({ ...c, text: c.text.trim(), start: toOutput(c.start, ranges), end: toOutput(c.end, ranges) }))
    .filter(c => c.end - c.start > 0.05 && c.text);
}

// ---------- Silence detection (same idea as the iPhone app) ----------
const HOP = 0.02;
function computeLevels(audio) {
  const win = Math.round(SR * HOP), out = [];
  for (let i = 0; i + win <= audio.length; i += win) {
    let sum = 0;
    for (let j = i; j < i + win; j++) sum += audio[j] * audio[j];
    out.push(20 * Math.log10(Math.max(Math.sqrt(sum / win), 1e-7)));
  }
  return out;
}
function suggestThreshold(levels) {
  if (levels.length < 50) return -38;
  const s = [...levels].sort((a, b) => a - b);
  const noise = s[Math.floor(s.length * 0.1)], speech = s[Math.floor(s.length * 0.9)];
  return Math.min(-20, Math.max(-60, Math.round(noise + (speech - noise) * 0.35)));
}
function findSilences() {
  const L = S.levels, pad = 0.12, res = [];
  let run = null;
  const close = (a, b) => {
    const st = a * HOP, en = b * HOP;
    if (en - st < S.minPause) return;
    const x = a === 0 ? 0 : st + pad, y = b === L.length ? en : en - pad;
    if (y - x > 0.05) res.push({ start: x, end: y });
  };
  L.forEach((db, i) => {
    if (db < S.threshold) { if (run === null) run = i; }
    else if (run !== null) { close(run, i); run = null; }
  });
  if (run !== null) close(run, L.length);
  S.silences = res;
}

// ---------- Loading a video ----------
async function decodeAudio16k(file, input) {
  // Mono 16 kHz Float32Array, the format Whisper wants.
  let chunks = [], rate = 48000;
  try {
    const track = await input.getPrimaryAudioTrack();
    if (!track) return new Float32Array(0);
    for await (const { buffer } of new AudioBufferSink(track).buffers()) {
      rate = buffer.sampleRate;
      const mono = new Float32Array(buffer.length);
      for (let c = 0; c < buffer.numberOfChannels; c++) {
        const d = buffer.getChannelData(c);
        for (let i = 0; i < d.length; i++) mono[i] += d[i] / buffer.numberOfChannels;
      }
      chunks.push(mono);
    }
  } catch (e) {
    // Fallback: let the browser decode the whole file.
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const ab = await ctx.decodeAudioData(await file.arrayBuffer());
    ctx.close();
    rate = ab.sampleRate;
    const mono = new Float32Array(ab.length);
    for (let c = 0; c < ab.numberOfChannels; c++) {
      const d = ab.getChannelData(c);
      for (let i = 0; i < d.length; i++) mono[i] += d[i] / ab.numberOfChannels;
    }
    chunks = [mono];
  }
  const total = chunks.reduce((a, c) => a + c.length, 0);
  if (!total) return new Float32Array(0);
  const off = new OfflineAudioContext(1, Math.ceil(total * SR / rate), SR);
  const buf = off.createBuffer(1, total, rate);
  let o = 0;
  for (const c of chunks) { buf.copyToChannel(c, 0, o); o += c.length; }
  const src = off.createBufferSource();
  src.buffer = buf;
  src.connect(off.destination);
  src.start();
  return (await off.startRendering()).getChannelData(0);
}

async function loadFile(file) {
  if (!file) return;
  const st = $('loadStatus');
  st.className = 'status';
  st.textContent = 'Reading the video…';
  try {
    const input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
    const vTrack = await input.getPrimaryVideoTrack();
    if (!vTrack) throw new Error('This file has no video in it.');
    S.width = await vTrack.getDisplayWidth();
    S.height = await vTrack.getDisplayHeight();
    S.duration = await input.computeDuration();
    st.textContent = 'Reading the sound…';
    S.audio16k = await decodeAudio16k(file, input);
    S.levels = computeLevels(S.audio16k);
    S.threshold = suggestThreshold(S.levels);
    findSilences();

    if (S.url) URL.revokeObjectURL(S.url);
    S.file = file;
    S.url = URL.createObjectURL(file);
    S.trimStart = 0; S.trimEnd = S.duration;
    S.words = []; S.captions = [];
    S.videoBlob = null; S.srtBlob = null;
    $('video').src = S.url;
    $('empty').classList.add('hidden');
    $('editor').classList.remove('hidden');
    $('newBtn').classList.remove('hidden');
    $('genStatus').className = 'status';
    $('expStatus').textContent = '';
    st.textContent = '';
    syncAll();
    layoutStage();
  } catch (e) {
    st.className = 'status error';
    st.textContent = `Couldn't open this video: ${e.message}`;
  }
}
$('fileInput').addEventListener('change', e => loadFile(e.target.files[0]));
$('fileInput2').addEventListener('change', e => { loadFile(e.target.files[0]); e.target.value = ''; });

// ---------- Preview ----------
const video = $('video');
function layoutStage() {
  if (!S.width) return;
  const ar = S.width / S.height;
  const maxW = $('editor').clientWidth, maxH = window.innerHeight * 0.55;
  const h = Math.min(maxH, maxW / ar), w = h * ar;
  const stage = $('stage');
  stage.style.width = `${w}px`; stage.style.height = `${h}px`;
  const size = Math.min(w, h) * 0.075;
  const cap = $('capEl');
  cap.style.fontSize = `${size}px`;
  cap.style.webkitTextStroke = `${size * 0.18}px #000`;
}
addEventListener('resize', layoutStage);

function renderOverlay() {
  const t = video.currentTime;
  const c = S.captions.find(c => t >= c.start && t < c.end && c.text.trim());
  const el = $('capEl');
  if (!c) { el.classList.add('hidden'); }
  else {
    el.classList.remove('hidden');
    el.textContent = S.caps ? upper(c.text, c.lang) : c.text;
    el.style.color = colorFor(c.lang);
    el.style.top = `${(1 - S.pos) * 100}%`;
  }
  $('tNow').textContent = fmt(toOutput(t));
}
function tick() {
  // Skip the parts that are cut out, so the preview plays like the export.
  if (!video.paused) {
    const t = video.currentTime, ranges = kept();
    const inside = ranges.find(r => t >= r.start - 0.01 && t < r.end);
    if (!inside) {
      const next = ranges.find(r => r.start > t);
      if (next) video.currentTime = next.start; else video.pause();
    }
  }
  renderOverlay();
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
video.addEventListener('play', () => {
  const ranges = kept();
  if (ranges.length && (video.currentTime >= ranges.at(-1).end - 0.05 || video.currentTime < ranges[0].start)) {
    video.currentTime = ranges[0].start;
  }
});

// ---------- Controls ----------
function syncTrim() {
  for (const id of ['trimStart', 'trimEnd']) { $(id).max = S.duration; }
  $('trimStart').value = S.trimStart; $('trimEnd').value = S.trimEnd;
  $('trimStartOut').textContent = fmt(S.trimStart); $('trimEndOut').textContent = fmt(S.trimEnd);
}
function syncPauses() {
  $('pauseToggle').checked = S.removePauses;
  $('pauseOpts').classList.toggle('hidden', !S.removePauses);
  $('thresh').value = S.threshold; $('threshOut').textContent = `${S.threshold} dB`;
  $('minPause').value = S.minPause; $('minPauseOut').textContent = `${S.minPause.toFixed(2)} s`;
  $('pauseCount').textContent = `${S.silences.length} pauses found`;
}
function syncLength() {
  const L = outLength();
  $('tLen').textContent = `Edited ${fmt(L)} · was ${fmt(S.duration)}`;
  $('lenWarn').textContent = L > 180 ? "Over 3 minutes — YouTube won't treat it as a Short." : '';
}
function fillLangSelects() {
  const codes = Object.keys(LANGS).sort((a, b) => langName(a).localeCompare(langName(b)));
  $('primary').innerHTML = codes.map(c => `<option value="${c}">${langName(c)}</option>`).join('');
  $('secondary').innerHTML = `<option value="">None</option>` + codes.map(c => `<option value="${c}">${langName(c)}</option>`).join('');
  $('primary').value = S.primary; $('secondary').value = S.secondary;
}
function swatches(el, key) {
  el.innerHTML = '';
  for (const [name, hex] of Object.entries(COLORS)) {
    const b = document.createElement('button');
    b.className = 'swatch'; b.style.background = hex;
    b.setAttribute('aria-label', name); b.setAttribute('aria-pressed', String(S[key] === name));
    b.onclick = () => { S[key] = name; persist(); syncStyle(); };
    el.appendChild(b);
  }
}
function syncStyle() {
  const two = activeLangs().length > 1;
  $('styleBox').classList.toggle('hidden', !S.captions.length);
  $('c1Label').textContent = langName(S.primary);
  $('c2Label').textContent = two ? langName(S.secondary) : '';
  $('c2Row').classList.toggle('hidden', !two);
  swatches($('c1'), 'color1'); swatches($('c2'), 'color2');
  $('capsToggle').checked = S.caps;
  $('wpOut').textContent = S.wordsPer;
  $('pos').value = S.pos; $('posOut').textContent = `${Math.round(S.pos * 100)}%`;
  $('burnToggle').checked = S.burn;
  renderCapList();
}
function renderCapList() {
  const list = $('capList');
  list.innerHTML = '';
  const langs = activeLangs();
  S.captions.forEach((c, i) => {
    const row = document.createElement('div');
    row.className = 'cap-row';
    const t = document.createElement('span'); t.className = 't'; t.textContent = fmt(c.start);
    const ta = document.createElement('textarea'); ta.rows = 1; ta.value = c.text; ta.style.color = colorFor(c.lang);
    ta.setAttribute('aria-label', `Subtitle at ${fmt(c.start)}`);
    ta.oninput = () => { c.text = ta.value; };
    ta.onfocus = () => { video.currentTime = c.start + 0.01; };
    const tools = document.createElement('div'); tools.className = 'tools';
    if (langs.length > 1) {
      const chip = document.createElement('button');
      chip.className = 'chip'; chip.textContent = c.lang.toUpperCase(); chip.style.background = colorFor(c.lang);
      chip.setAttribute('aria-label', `Language: ${langName(c.lang)}. Tap to switch.`);
      chip.onclick = () => { c.lang = langs[(langs.indexOf(c.lang) + 1) % langs.length]; renderCapList(); };
      tools.appendChild(chip);
    }
    const del = document.createElement('button'); del.className = 'del'; del.textContent = '×';
    del.setAttribute('aria-label', 'Delete this line');
    del.onclick = () => { S.captions.splice(i, 1); renderCapList(); };
    tools.appendChild(del);
    row.append(t, ta, tools);
    list.appendChild(row);
  });
}
function syncAll() { syncTrim(); syncPauses(); syncLength(); syncStyle(); }

$('trimStart').addEventListener('input', e => {
  S.trimStart = Math.min(+e.target.value, S.trimEnd - 0.5); syncTrim(); syncLength(); video.currentTime = S.trimStart;
});
$('trimEnd').addEventListener('input', e => {
  S.trimEnd = Math.max(+e.target.value, S.trimStart + 0.5); syncTrim(); syncLength(); video.currentTime = Math.max(0, S.trimEnd - 0.05);
});
$('startHere').onclick = () => { S.trimStart = Math.min(video.currentTime, S.trimEnd - 0.5); syncTrim(); syncLength(); };
$('endHere').onclick = () => { S.trimEnd = Math.max(video.currentTime, S.trimStart + 0.5); syncTrim(); syncLength(); };
$('pauseToggle').onchange = e => { S.removePauses = e.target.checked; persist(); syncPauses(); syncLength(); };
$('thresh').oninput = e => { S.threshold = +e.target.value; findSilences(); syncPauses(); syncLength(); };
$('minPause').oninput = e => { S.minPause = +e.target.value; persist(); findSilences(); syncPauses(); syncLength(); };
$('autoLevel').onclick = () => { S.threshold = suggestThreshold(S.levels); findSilences(); syncPauses(); syncLength(); };
$('primary').onchange = e => { S.primary = e.target.value; persist(); syncStyle(); };
$('secondary').onchange = e => { S.secondary = e.target.value; persist(); syncStyle(); };
$('capsToggle').onchange = e => { S.caps = e.target.checked; persist(); };
$('wpMinus').onclick = () => { if (S.wordsPer > 1) { S.wordsPer--; persist(); rebuildCaptions(); } };
$('wpPlus').onclick = () => { if (S.wordsPer < 6) { S.wordsPer++; persist(); rebuildCaptions(); } };
$('pos').oninput = e => { S.pos = +e.target.value; persist(); $('posOut').textContent = `${Math.round(S.pos * 100)}%`; };
$('burnToggle').onchange = e => { S.burn = e.target.checked; persist(); };
function rebuildCaptions() { S.captions = buildCaptions(S.words, S.wordsPer); syncStyle(); }

// ---------- Subtitles (Whisper in a worker) ----------
let worker = null;
function setBar(id, frac) {
  const bar = $(id);
  bar.classList.toggle('hidden', frac == null);
  bar.firstElementChild.style.width = `${Math.round((frac || 0) * 100)}%`;
}
$('genBtn').onclick = () => {
  if (!S.audio16k?.length) { $('genStatus').textContent = 'This video has no sound to transcribe.'; return; }
  worker ??= new Worker('worker.js', { type: 'module' });
  const btn = $('genBtn'), st = $('genStatus');
  btn.disabled = true; st.className = 'status'; st.textContent = 'Starting…'; setBar('genBar', 0);
  video.pause();
  const second = S.secondary && S.secondary !== S.primary ? S.secondary : '';
  worker.onmessage = ({ data }) => {
    if (data.type === 'download') {
      st.textContent = `Downloading the speech model (one time)… ${Math.round(data.progress)}%`;
      setBar('genBar', data.progress / 100);
    } else if (data.type === 'status') {
      if (data.stage === 'model') { st.textContent = 'Loading the speech model…'; }
      if (data.stage === 'main') { st.textContent = `Listening in ${langName(S.primary)}…`; setBar('genBar', 0.15); }
      if (data.stage === 'detect' && data.n) {
        st.textContent = `Looking for ${langName(second)} words… ${data.i}/${data.n}`;
        setBar('genBar', 0.3 + 0.7 * data.i / data.n);
      }
    } else if (data.type === 'done') {
      S.words = data.words; rebuildCaptions();
      btn.disabled = false; btn.textContent = 'Make subtitles again'; setBar('genBar', null);
      st.textContent = S.words.length ? `${S.captions.length} subtitle lines. Check them below.` : 'No speech found in this video.';
    } else if (data.type === 'error') {
      btn.disabled = false; setBar('genBar', null);
      st.className = 'status error';
      st.textContent = `Subtitles failed: ${data.message}. Check the connection for the first download and try again.`;
    }
  };
  worker.onerror = (e) => {
    btn.disabled = false; setBar('genBar', null);
    st.className = 'status error';
    st.textContent = `The speech engine couldn't start${e.message ? `: ${e.message}` : ''}. Reload the page and try again.`;
    worker = null;
  };
  worker.postMessage({ type: 'transcribe', audio: S.audio16k, primary: S.primary, secondary: second });
};

// ---------- Export ----------
function drawCaption(ctx, cap, W, H) {
  const size = Math.min(W, H) * 0.075, maxW = W * 0.86, lh = size * 1.15;
  ctx.font = `800 ${size}px "Bricolage Grotesque", system-ui, sans-serif`;
  const text = S.caps ? upper(cap.text, cap.lang) : cap.text;
  const lines = [];
  let line = '';
  for (const word of text.split(/\s+/)) {
    const test = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(test).width > maxW) { lines.push(line); line = word; } else line = test;
  }
  if (line) lines.push(line);
  const y0 = H * (1 - S.pos) - (lines.length - 1) * lh / 2;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
  ctx.lineWidth = size * 0.18; ctx.strokeStyle = '#000'; ctx.fillStyle = colorFor(cap.lang);
  lines.forEach((l, i) => { ctx.strokeText(l, W / 2, y0 + i * lh); ctx.fillText(l, W / 2, y0 + i * lh); });
}

async function exportVideo() {
  const btn = $('exportBtn'), st = $('expStatus');
  btn.disabled = true; st.className = 'status'; st.textContent = 'Preparing…'; setBar('expBar', 0);
  video.pause();
  for (const id of ['shareVideo', 'dlVideo', 'shareSrt', 'dlSrt', 'srtNote']) $(id).classList.add('hidden');
  try {
    await document.fonts.load('800 40px "Bricolage Grotesque"').catch(() => {});
    const input = new Input({ source: new BlobSource(S.file), formats: ALL_FORMATS });
    const vTrack = await input.getPrimaryVideoTrack();
    const aTrack = await input.getPrimaryAudioTrack();

    // Keep at most 1080p-ish so long clips don't run out of memory.
    const scale = Math.min(1, 1920 / Math.max(S.width, S.height));
    const W = Math.round(S.width * scale / 2) * 2, H = Math.round(S.height * scale / 2) * 2;
    const vCodec = await getFirstEncodableVideoCodec(['avc', 'hevc', 'vp9', 'av1'], { width: W, height: H });
    if (!vCodec) throw new Error('This browser cannot encode video');
    const aCodec = aTrack ? await getFirstEncodableAudioCodec(['aac', 'opus']) : null;
    const fps = Math.round((await vTrack.computePacketStats(120)).averagePacketRate) || 30;

    const canvas = new OffscreenCanvas(W, H);
    const ctx = canvas.getContext('2d');
    const output = new Output({ format: new Mp4OutputFormat({ fastStart: 'in-memory' }), target: new BufferTarget() });
    const vSource = new CanvasSource(canvas, { codec: vCodec, bitrate: QUALITY_HIGH });
    output.addVideoTrack(vSource, { frameRate: fps });
    let aSource = null;
    if (aCodec) { aSource = new AudioBufferSource({ codec: aCodec, bitrate: QUALITY_HIGH }); output.addAudioTrack(aSource); }
    await output.start();

    const ranges = kept();
    const total = ranges.reduce((a, r) => a + r.end - r.start, 0);
    const caps = S.burn ? S.captions.filter(c => c.text.trim()) : [];

    const doVideo = async () => {
      const sink = new CanvasSink(vTrack, { width: W, height: H, fit: 'fill' });
      let acc = 0, lastOut = -1;
      for (const r of ranges) {
        for await (const f of sink.canvases(r.start, r.end)) {
          if (f.timestamp >= r.end) break;
          if (f.timestamp + f.duration <= r.start) continue;
          const srcT = Math.max(f.timestamp, r.start);
          const outT = acc + srcT - r.start;
          if (outT <= lastOut) continue;
          ctx.drawImage(f.canvas, 0, 0, W, H);
          const cap = caps.find(c => srcT >= c.start && srcT < c.end);
          if (cap) drawCaption(ctx, cap, W, H);
          const dur = Math.min(f.duration, r.end - srcT);
          await vSource.add(outT, dur > 0 ? dur : 1 / fps);
          lastOut = outT;
          st.textContent = `Exporting… ${Math.min(100, Math.round(outT / total * 100))}%`;
          setBar('expBar', outT / total);
        }
        acc += r.end - r.start;
      }
      vSource.close();
    };
    const doAudio = async () => {
      if (!aSource) return;
      const sink = new AudioBufferSink(aTrack);
      const fade = (data, from, n, up) => {
        for (let i = 0; i < n && from + i < data.length; i++) data[from + i] *= up ? i / n : 1 - i / n;
      };
      for (const r of ranges) {
        let first = true;
        for await (const { buffer, timestamp } of sink.buffers(r.start, r.end)) {
          const sr = buffer.sampleRate;
          const s0 = Math.max(0, Math.round((r.start - timestamp) * sr));
          const s1 = Math.min(buffer.length, Math.round((r.end - timestamp) * sr));
          if (s1 <= s0) continue;
          const out = new AudioBuffer({ length: s1 - s0, numberOfChannels: buffer.numberOfChannels, sampleRate: sr });
          const n = Math.round(sr * 0.005);
          const last = s1 < buffer.length || timestamp + buffer.duration >= r.end - 1e-3;
          for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
            const d = buffer.getChannelData(ch).slice(s0, s1);
            if (first) fade(d, 0, n, true);
            if (last) fade(d, Math.max(0, d.length - n), n, false);
            out.copyToChannel(d, ch);
          }
          first = false;
          await aSource.add(out);
        }
      }
      aSource.close();
    };
    await Promise.all([doVideo(), doAudio()]);
    st.textContent = 'Finishing…';
    await output.finalize();

    S.videoBlob = new Blob([output.target.buffer], { type: 'video/mp4' });
    const srtCaps = outputCaptions();
    S.srtBlob = srtCaps.length ? new Blob([toSRT(srtCaps)], { type: 'text/plain' }) : null;

    const dl = $('dlVideo');
    if (dl.href) URL.revokeObjectURL(dl.href);
    dl.href = URL.createObjectURL(S.videoBlob);
    dl.classList.remove('hidden');
    if (navigator.canShare?.({ files: [new File([S.videoBlob], 'short.mp4', { type: 'video/mp4' })] })) $('shareVideo').classList.remove('hidden');
    if (S.srtBlob) {
      const ds = $('dlSrt');
      if (ds.href) URL.revokeObjectURL(ds.href);
      ds.href = URL.createObjectURL(S.srtBlob);
      ds.classList.remove('hidden'); $('srtNote').classList.remove('hidden');
      if (navigator.canShare?.({ files: [new File([S.srtBlob], 'subtitles.srt', { type: 'text/plain' })] })) $('shareSrt').classList.remove('hidden');
    }
    st.textContent = `Done — ${fmt(total)} long. Share it to save it to Photos.`;
  } catch (e) {
    st.className = 'status error';
    st.textContent = `Export failed: ${e.message}`;
  } finally {
    btn.disabled = false; setBar('expBar', null);
  }
}
$('exportBtn').onclick = exportVideo;
const share = async (blob, name, type) => {
  try { await navigator.share({ files: [new File([blob], name, { type })] }); } catch (e) { if (e.name !== 'AbortError') $('expStatus').textContent = `Sharing failed: ${e.message}`; }
};
$('shareVideo').onclick = () => share(S.videoBlob, 'short.mp4', 'video/mp4');
$('shareSrt').onclick = () => share(S.srtBlob, 'subtitles.srt', 'text/plain');

fillLangSelects();
