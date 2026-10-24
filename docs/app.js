import {
  Input, Output, BlobSource, BufferTarget, StreamTarget, Mp4OutputFormat, ALL_FORMATS,
  CanvasSink, AudioBufferSink, CanvasSource, AudioBufferSource, QUALITY_HIGH,
  getFirstEncodableVideoCodec, getFirstEncodableAudioCodec,
} from './lib/mediabunny.min.mjs';
import { LANGS, SR, buildCaptions, toSRT } from './core.js';
import { blocksAt, drawCaptions, drawHook, drawSafeZones, FONTS, fontCss } from './render.js';
import { saveFile, saveProject, listProjects, loadProject, deleteProject } from './store.js';
import * as L from './log.js';

L.install();

const $ = (id) => document.getElementById(id);

// ---------- Settings (remembered between visits) ----------
const COLORS = { white: '#ffffff', yellow: '#ffd21a', cyan: '#4de6ff', green: '#73ff73', pink: '#ff80cc', orange: '#ff9e33' };
const SETTINGS = ['primary', 'secondary', 'color1', 'color2', 'caps', 'wordsPer', 'pos', 'burn', 'removePauses',
  'minPause', 'layout', 'size', 'box', 'highlight', 'accurate', 'gpu', 'posX', 'font', 'anim', 'hookSecs', 'accuracy'];
const saved = (() => { try { return JSON.parse(localStorage.getItem('settings') || '{}'); } catch { return {}; } })();
const S = {
  primary: 'de', secondary: 'tr', color1: 'white', color2: 'yellow', caps: true, wordsPer: 3, pos: 0.35, burn: true,
  removePauses: true, minPause: 0.4, layout: 'single', size: 0.075, box: false, highlight: false, accurate: false, gpu: true,
  posX: 0.5, font: 'modern', anim: false, hookSecs: 3, accuracy: saved.accurate ? 'second' : 'normal',
  ...saved,
  // per project
  id: null, name: '', file: null, url: null, duration: 0, width: 0, height: 0,
  audio16k: null, levels: [], threshold: -38, silences: [], cuts: [], cutFrom: null,
  trimStart: 0, trimEnd: 0, words: [], captions: [], script: '', glossary: '', hook: '', dragMode: false, job: null,
  videoBlob: null, srtBlob: null,
};
function persist() {
  try { localStorage.setItem('settings', JSON.stringify(Object.fromEntries(SETTINGS.map(k => [k, S[k]])))); } catch {}
}

const langName = (() => {
  let dn = null;
  try { dn = new Intl.DisplayNames([navigator.language, 'en'], { type: 'language' }); } catch {}
  return (code) => {
    let n = code;
    try { n = dn?.of(code) || LANGS[code] || code; } catch { n = LANGS[code] || code; }
    if (n.length <= 3) n = LANGS[code] || n;
    return n.charAt(0).toUpperCase() + n.slice(1);
  };
})();
const colorFor = (lang) => COLORS[lang === S.primary ? S.color1 : S.color2];
const activeLangs = () => (S.secondary && S.secondary !== S.primary ? [S.primary, S.secondary] : [S.primary]);
const style = () => ({ size: S.size, pos: S.pos, posX: S.posX, box: S.box, highlight: S.highlight, caps: S.caps, font: S.font, anim: S.anim, colorFor });
const fmt = (t) => {
  if (!isFinite(t) || t < 0) t = 0;
  const m = Math.floor(t / 60);
  return `${m}:${(t - m * 60).toFixed(1).padStart(4, '0')}`;
};

// ---------- Autosave ----------
let saveTimer = null;
function dirty(now = false) {
  if (!S.id) return;
  $('saved').textContent = '';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      await saveProject({
        id: S.id, name: S.name, updatedAt: Date.now(), duration: S.duration, width: S.width, height: S.height,
        trimStart: S.trimStart, trimEnd: S.trimEnd, cuts: S.cuts, threshold: S.threshold,
        words: S.words, captions: S.captions, script: S.script, glossary: S.glossary, hook: S.hook, job: S.job,
      });
      $('saved').textContent = 'Saved';
    } catch { $('saved').textContent = 'Not saved'; }
  }, now ? 0 : 700);
}

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
const kept = () => subtract({ start: S.trimStart, end: S.trimEnd }, [...S.cuts, ...(S.removePauses ? S.silences : [])]);
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
function outputCaptions() {
  const ranges = kept();
  return S.captions.map(c => ({ ...c, text: c.text.trim(), start: toOutput(c.start, ranges), end: toOutput(c.end, ranges) }))
    .filter(c => c.end - c.start > 0.05 && c.text);
}

// ---------- Silence detection ----------
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

// ---------- Loading a video or a saved project ----------
async function decodeAudio16k(file, input) {
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
  } catch {
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
  src.buffer = buf; src.connect(off.destination); src.start();
  return (await off.startRendering()).getChannelData(0);
}

async function openVideo(file, project = null) {
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
    L.info(project ? 'Opening project' : 'Opening video', { type: file.type, sizeMB: +(file.size / 1e6).toFixed(1), w: S.width, h: S.height, secs: +S.duration.toFixed(1) });
    st.textContent = 'Reading the sound…';
    S.audio16k = await decodeAudio16k(file, input);
    S.levels = computeLevels(S.audio16k);

    if (S.url) URL.revokeObjectURL(S.url);
    S.file = file;
    S.url = URL.createObjectURL(file);
    S.videoBlob = null; S.srtBlob = null; S.cutFrom = null; S.dragMode = false;
    $('stage').classList.remove('dragging'); $('dragHint').classList.add('hidden'); $('dragDone').classList.add('hidden');
    if (project) {
      Object.assign(S, {
        id: project.id, name: project.name, trimStart: project.trimStart, trimEnd: project.trimEnd,
        cuts: project.cuts || [], threshold: project.threshold ?? suggestThreshold(S.levels), words: project.words || [],
        captions: project.captions || [], script: project.script || '', glossary: project.glossary || '', hook: project.hook || '', job: project.job || null,
      });
    } else {
      Object.assign(S, {
        id: crypto.randomUUID(), name: file.name || 'Video', trimStart: 0, trimEnd: S.duration, cuts: [],
        threshold: suggestThreshold(S.levels), words: [], captions: [], script: '', glossary: '', hook: '', job: null,
      });
      try {
        await saveFile(S.id, file);
        const all = await listProjects();
        for (const old of all.slice(9)) await deleteProject(old.id);
      } catch {}
      dirty();
    }
    findSilences();
    $('video').src = S.url;
    $('empty').classList.add('hidden');
    $('editor').classList.remove('hidden');
    $('newBtn').classList.remove('hidden');
    $('genStatus').className = 'status';
    $('genStatus').textContent = S.captions.length ? `${S.captions.length} subtitle lines.` : 'First run downloads the speech model (about 250 MB) — use Wi‑Fi. It stays on the device afterwards.';
    if (crashed && crashed.id === S.id) {
      $('genStatus').className = 'status error';
      $('genStatus').textContent = S.job
        ? 'Last time the page stopped while making subtitles — the device most likely ran out of memory. Your progress is saved: tap Continue. If it stops again, choose “Fast” accuracy and close other apps.'
        : 'Last time the page stopped before subtitles got going. Downloaded parts are kept, so it continues where it stopped. If it stops again, choose “Fast” accuracy and close other apps.';
      crashed = null;
    }
    $('expStatus').textContent = '';
    for (const id of ['shareVideo', 'dlVideo', 'shareSrt', 'dlSrt', 'srtNote']) $(id).classList.add('hidden');
    st.textContent = '';
    syncAll();
    layoutStage();
    scrollTo(0, 0);
  } catch (e) {
    st.className = 'status error';
    st.textContent = `Couldn't open this video: ${String(e.message).replace(/\.$/, '')}. (Tap ? → Copy log)`;
    L.error('Open video failed: ' + e.message, { type: file?.type });
  }
}
$('fileInput').addEventListener('change', e => { const f = e.target.files[0]; e.target.value = ''; if (f) openVideo(f); });

// If the page died during a subtitle job (usually memory), remember it to explain and resume.
let crashed = null;
try {
  const r = JSON.parse(localStorage.getItem('jobRunning') || 'null');
  if (r) { crashed = r; localStorage.removeItem('jobRunning'); L.warn('Previous subtitle job did not finish (page closed or ran out of memory)', r); }
} catch {}
let wakeLock = null;
async function keepAwake(on) {
  try {
    if (on && !wakeLock) wakeLock = await navigator.wakeLock?.request('screen');
    if (!on && wakeLock) { await wakeLock.release(); wakeLock = null; }
  } catch {}
}

async function showProjects() {
  video.pause();
  $('editor').classList.add('hidden');
  $('newBtn').classList.add('hidden');
  $('empty').classList.remove('hidden');
  let list = [];
  try { list = await listProjects(); } catch {}
  $('projects').classList.toggle('hidden', !list.length);
  const box = $('projList');
  box.innerHTML = '';
  for (const p of list) {
    const row = document.createElement('div');
    row.className = 'proj';
    const name = document.createElement('div');
    name.className = 'name';
    const b = document.createElement('b'); b.textContent = p.name;
    const small = document.createElement('span'); small.className = 'muted';
    small.textContent = `${fmt(p.duration)} · ${new Date(p.updatedAt).toLocaleDateString()} · ${(p.captions || []).length} lines`;
    name.append(b, small);
    const open = document.createElement('button'); open.className = 'btn'; open.textContent = 'Open';
    open.onclick = async () => {
      $('loadStatus').textContent = 'Opening…';
      const full = await loadProject(p.id).catch(() => null);
      if (full) openVideo(full.file, full); else $('loadStatus').textContent = "This project's video is missing.";
    };
    const del = document.createElement('button'); del.className = 'del'; del.textContent = '×';
    del.setAttribute('aria-label', `Delete ${p.name}`);
    del.onclick = async () => { if (confirm(`Delete “${p.name}”?`)) { await deleteProject(p.id); showProjects(); } };
    row.append(name, open, del);
    box.appendChild(row);
  }
}
$('newBtn').onclick = showProjects;

// ---------- Preview ----------
const video = $('video');
const overlay = $('overlay');
function layoutStage() {
  if (!S.width) return;
  const ar = S.width / S.height;
  const maxW = $('editor').clientWidth, maxH = window.innerHeight * 0.55;
  const h = Math.min(maxH, maxW / ar), w = h * ar;
  const stage = $('stage');
  stage.style.width = `${w}px`; stage.style.height = `${h}px`;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  overlay.width = Math.round(w * dpr); overlay.height = Math.round(h * dpr);
}
addEventListener('resize', layoutStage);

function tick() {
  if (!$('editor').classList.contains('hidden')) {
    if (!video.paused) {
      const t = video.currentTime, ranges = kept();
      const inside = ranges.find(r => t >= r.start - 0.01 && t < r.end);
      if (!inside) {
        const next = ranges.find(r => r.start > t);
        if (next) video.currentTime = next.start; else video.pause();
      }
    }
    const t = video.currentTime;
    const ctx = overlay.getContext('2d');
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    const W = overlay.width, H = overlay.height;
    if (S.dragMode) drawSafeZones(ctx, W, H);
    let blocks = blocksAt(t, S.captions, S.layout, S.primary);
    if (S.dragMode && !blocks.length) {
      // Something to aim with while placing the subtitles.
      blocks = [{ id: 'demo', start: t - 1, end: t + 1, text: S.captions[0]?.text || 'Subtitles here', lang: S.captions[0]?.lang || S.primary }];
    }
    drawCaptions(ctx, W, H, t, blocks, style());
    const outT = toOutput(t);
    if (S.hook.trim() && outT < S.hookSecs) drawHook(ctx, W, H, S.hook, outT, style());
    $('tNow').textContent = fmt(toOutput(t));
  }
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
  for (const id of ['trimStart', 'trimEnd']) $(id).max = S.duration;
  $('trimStart').value = S.trimStart; $('trimEnd').value = S.trimEnd;
  $('trimStartOut').textContent = fmt(S.trimStart); $('trimEndOut').textContent = fmt(S.trimEnd);
  $('cutOut').disabled = S.cutFrom == null;
  $('cutIn').textContent = S.cutFrom == null ? 'Cut from here' : `Cut from ${fmt(S.cutFrom)}`;
  const list = $('cutList');
  list.innerHTML = '';
  S.cuts.forEach((c, i) => {
    const row = document.createElement('div'); row.className = 'item';
    const label = document.createElement('span'); label.textContent = `Cut ${fmt(c.start)} – ${fmt(c.end)}`;
    const del = document.createElement('button'); del.className = 'del'; del.textContent = '×';
    del.setAttribute('aria-label', 'Remove this cut');
    del.onclick = () => { S.cuts.splice(i, 1); syncTrim(); syncLength(); dirty(); };
    row.append(label, del); list.appendChild(row);
  });
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
  $('tLen').textContent = `${fmt(L)} of ${fmt(S.duration)}`;
  $('lenWarn').textContent = L > 180 ? "Over 3 minutes — YouTube won't treat it as a Short." : '';
}
function fillLangSelects() {
  const codes = Object.keys(LANGS).sort((a, b) => langName(a).localeCompare(langName(b)));
  $('primary').innerHTML = codes.map(c => `<option value="${c}">${langName(c)}</option>`).join('');
  $('secondary').innerHTML = `<option value="">None</option>` + codes.map(c => `<option value="${c}">${langName(c)}</option>`).join('');
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
function syncPos() {
  const side = S.posX < 0.42 ? ', left' : S.posX > 0.58 ? ', right' : '';
  $('posOut').textContent = `${Math.round(S.pos * 100)}% up${side}`;
}
function syncHook() {
  $('hook').value = S.hook;
  $('hookSecs').value = S.hookSecs; $('hookSecsOut').textContent = `${S.hookSecs} s`;
}
function syncSubs() {
  queueMicrotask(syncGenButton);
  $('primary').value = S.primary; $('secondary').value = S.secondary;
  $('script').value = S.script; $('glossary').value = S.glossary;
  $('scriptHint').textContent = S.script.trim() ? 'added' : 'optional';
  $('glossHint').textContent = S.glossary.trim() ? `${S.glossary.trim().split('\n').length} words` : 'optional';
  for (const b of $('accSeg').children) b.setAttribute('aria-pressed', String(b.dataset.v === S.accuracy));
  $('accHint').textContent = {
    normal: 'Small model (about 250 MB once). Good for most videos.',
    second: 'Adds the big model (about 760 MB once) just for the second-language words.',
    all: 'Big model for everything (about 760 MB once). Slowest; best on iPad or with the graphics chip.',
  }[S.accuracy];
  $('gpuToggle').checked = S.gpu;
}
function syncStyle() {
  const two = activeLangs().length > 1, has = S.captions.length > 0;
  $('styleBox').classList.toggle('hidden', !has);
  $('linesBox').classList.toggle('hidden', !has);
  for (const b of $('layoutSeg').children) b.setAttribute('aria-pressed', String(b.dataset.v === S.layout));
  $('c1Label').textContent = langName(S.primary);
  $('c2Label').textContent = two ? langName(S.secondary) : '';
  $('c2Row').classList.toggle('hidden', !two);
  swatches($('c1'), 'color1'); swatches($('c2'), 'color2');
  $('size').value = S.size; $('sizeOut').textContent = `${Math.round(S.size / 0.075 * 100)}%`;
  $('pos').value = S.pos; syncPos();
  for (const b of $('posSeg').children) b.setAttribute('aria-pressed', String(b.dataset.v === 'drag' ? S.dragMode : false));
  const fs = $('fontSeg');
  if (!fs.children.length) {
    for (const [k, f] of Object.entries(FONTS)) {
      const b = document.createElement('button');
      b.dataset.v = k; b.textContent = f.label; b.style.font = fontCss(k, 15);
      b.onclick = () => { S.font = k; persist(); syncStyle(); };
      fs.appendChild(b);
    }
  }
  for (const b of fs.children) b.setAttribute('aria-pressed', String(b.dataset.v === S.font));
  $('animToggle').checked = S.anim;
  $('boxToggle').checked = S.box; $('hlToggle').checked = S.highlight; $('capsToggle').checked = S.caps;
  $('wpOut').textContent = S.wordsPer;
  $('burnToggle').checked = S.burn;
  renderCapList();
}
function renderCapList() {
  const list = $('capList');
  list.innerHTML = '';
  const langs = activeLangs();
  const mk = (tag, cls, text, label, onclick) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    if (label) el.setAttribute('aria-label', label);
    if (onclick) el.onclick = onclick;
    return el;
  };
  S.captions.forEach((c, i) => {
    const row = mk('div', 'cap-row');
    const top = mk('div', 'top');
    const t = mk('span', 't', `${fmt(c.start)}–${fmt(c.end)}`);
    const grow = mk('span', 'grow');
    const play = mk('button', 'del', '▶', 'Play this line', () => { video.currentTime = c.start + 0.01; video.play(); });
    top.append(t, grow);
    if (langs.length > 1) {
      const chip = mk('button', 'chip', c.lang.toUpperCase(), `Language: ${langName(c.lang)}. Tap to switch.`, () => {
        c.lang = langs[(langs.indexOf(c.lang) + 1) % langs.length]; renderCapList(); dirty();
      });
      chip.style.background = colorFor(c.lang);
      top.appendChild(chip);
    }
    top.append(play, mk('button', 'del', '×', 'Delete this line', () => { S.captions.splice(i, 1); renderCapList(); dirty(); }));

    const ta = mk('textarea'); ta.rows = 1; ta.value = c.text; ta.style.color = colorFor(c.lang);
    ta.setAttribute('aria-label', `Subtitle at ${fmt(c.start)}`);
    ta.oninput = () => { c.text = ta.value; dirty(); };
    ta.onfocus = () => { video.pause(); video.currentTime = c.start + 0.01; };

    const nudge = mk('div', 'nudge');
    const move = (which, d) => () => {
      const prev = S.captions[i - 1], next = S.captions[i + 1];
      if (which === 'start') c.start = Math.max(prev ? prev.end : 0, Math.min(c.end - 0.1, c.start + d));
      else c.end = Math.min(next ? next.start : S.duration, Math.max(c.start + 0.1, c.end + d));
      t.textContent = `${fmt(c.start)}–${fmt(c.end)}`;
      video.pause(); video.currentTime = (which === 'start' ? c.start : c.end - 0.05) + 0.01;
      dirty();
    };
    nudge.append(
      mk('span', null, 'Start'), mk('button', null, '−', 'Start earlier', move('start', -0.1)), mk('button', null, '+', 'Start later', move('start', 0.1)),
      mk('span', null, ' End'), mk('button', null, '−', 'End earlier', move('end', -0.1)), mk('button', null, '+', 'End later', move('end', 0.1)),
    );
    row.append(top, ta, nudge);
    list.appendChild(row);
  });
}
function syncAll() { syncTrim(); syncPauses(); syncLength(); syncSubs(); syncStyle(); syncHook(); }

const nowT = () => video.currentTime;
$('trimStart').addEventListener('input', e => {
  S.trimStart = Math.min(+e.target.value, S.trimEnd - 0.5); syncTrim(); syncLength(); video.currentTime = S.trimStart; dirty();
});
$('trimEnd').addEventListener('input', e => {
  S.trimEnd = Math.max(+e.target.value, S.trimStart + 0.5); syncTrim(); syncLength(); video.currentTime = Math.max(0, S.trimEnd - 0.05); dirty();
});
$('startHere').onclick = () => { S.trimStart = Math.min(nowT(), S.trimEnd - 0.5); syncTrim(); syncLength(); dirty(); };
$('endHere').onclick = () => { S.trimEnd = Math.max(nowT(), S.trimStart + 0.5); syncTrim(); syncLength(); dirty(); };
$('cutIn').onclick = () => { S.cutFrom = nowT(); syncTrim(); };
$('cutOut').onclick = () => {
  const a = Math.min(S.cutFrom, nowT()), b = Math.max(S.cutFrom, nowT());
  if (b - a >= 0.1) { S.cuts.push({ start: a, end: b }); S.cuts.sort((x, y) => x.start - y.start); }
  S.cutFrom = null; syncTrim(); syncLength(); dirty();
};
$('pauseToggle').onchange = e => { S.removePauses = e.target.checked; persist(); syncPauses(); syncLength(); };
$('thresh').oninput = e => { S.threshold = +e.target.value; findSilences(); syncPauses(); syncLength(); dirty(); };
$('minPause').oninput = e => { S.minPause = +e.target.value; persist(); findSilences(); syncPauses(); syncLength(); };
$('autoLevel').onclick = () => { S.threshold = suggestThreshold(S.levels); findSilences(); syncPauses(); syncLength(); dirty(); };
$('primary').onchange = e => { S.primary = e.target.value; persist(); syncStyle(); syncGenButton(); };
$('secondary').onchange = e => { S.secondary = e.target.value; persist(); syncStyle(); syncGenButton(); };
$('script').oninput = e => { S.script = e.target.value; $('scriptHint').textContent = S.script.trim() ? 'added' : 'optional'; dirty(); };
$('glossary').oninput = e => { S.glossary = e.target.value; dirty(); syncSubs(); };
for (const b of $('accSeg').children) b.onclick = () => { S.accuracy = b.dataset.v; persist(); syncSubs(); };

// ---------- Copy the transcript (e.g. to let Claude fix it) ----------
/** The subtitles as plain text: a new line after each sentence and at each language switch. */
function transcriptText() {
  const lines = [];
  let cur = '', lang = null;
  for (const c of S.captions) {
    const text = c.text.trim();
    if (!text) continue;
    if (cur && lang !== c.lang) { lines.push(cur); cur = ''; }
    cur = cur ? `${cur} ${text}` : text;
    lang = c.lang;
    if (/[.!?…]$/.test(text)) { lines.push(cur); cur = ''; }
  }
  if (cur) lines.push(cur);
  return lines.join('\n');
}
function claudePrompt() {
  const langs = activeLangs().map(langName).join(' and ');
  return `This is an automatic transcript of my short video, spoken in ${langs}. ` +
    `Speech recognition misheard some words${activeLangs().length > 1 ? `, especially the ${langName(S.secondary)} ones` : ''}. ` +
    `Please fix the mistakes using the context. Keep my words and their order, keep one line per line ` +
    `(a new line where a sentence ends or the language changes), don't translate or add anything, ` +
    `and reply with only the corrected text.` +
    (S.glossary.trim() ? `\n\nWords I teach in this video:\n${S.glossary.trim()}` : '') +
    `\n\nTranscript:\n${transcriptText()}`;
}
async function copy(text, btn) {
  const old = btn.textContent;
  try { await navigator.clipboard.writeText(text); btn.textContent = 'Copied ✓'; }
  catch {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); btn.textContent = 'Copied ✓'; } catch { btn.textContent = 'Copy failed'; }
    ta.remove();
  }
  setTimeout(() => { btn.textContent = old; }, 1800);
}
$('copyText').onclick = () => copy(transcriptText(), $('copyText'));
$('copyClaude').onclick = () => copy(claudePrompt(), $('copyClaude'));
$('pasteScript').onclick = async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text.trim()) { S.script = text.trim(); syncSubs(); dirty(); }
  } catch { $('script').focus(); }
};
$('clearScript').onclick = () => { S.script = ''; syncSubs(); dirty(); };
$('gpuToggle').onchange = e => { S.gpu = e.target.checked; persist(); };
for (const b of $('layoutSeg').children) b.onclick = () => { S.layout = b.dataset.v; persist(); syncStyle(); };
$('size').oninput = e => { S.size = +e.target.value; persist(); $('sizeOut').textContent = `${Math.round(S.size / 0.075 * 100)}%`; };
$('pos').oninput = e => { S.pos = +e.target.value; persist(); syncPos(); };
const PRESETS = { top: 0.8, middle: 0.5, bottom: 0.3 };
function setDrag(on) {
  S.dragMode = on;
  $('stage').classList.toggle('dragging', on);
  $('dragHint').classList.toggle('hidden', !on);
  $('dragDone').classList.toggle('hidden', !on);
  if (on) { video.pause(); $('stage').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  syncStyle();
}
$('dragDone').onclick = () => setDrag(false);
for (const b of $('posSeg').children) b.onclick = () => {
  const v = b.dataset.v;
  if (v === 'drag') { setDrag(!S.dragMode); return; }
  S.pos = PRESETS[v]; S.posX = 0.5; persist(); setDrag(false);
};
// Drag on the video to place the subtitles (center of the text follows the finger).
let dragging = false;
const placeAt = (e) => {
  const r = overlay.getBoundingClientRect();
  S.posX = Math.min(0.85, Math.max(0.15, (e.clientX - r.left) / r.width));
  S.pos = Math.min(0.92, Math.max(0.08, 1 - (e.clientY - r.top) / r.height));
  $('pos').value = S.pos; syncPos();
};
overlay.addEventListener('pointerdown', (e) => { if (!S.dragMode) return; dragging = true; overlay.setPointerCapture(e.pointerId); placeAt(e); });
overlay.addEventListener('pointermove', (e) => { if (dragging) placeAt(e); });
const endDrag = () => { if (dragging) { dragging = false; persist(); } };
overlay.addEventListener('pointerup', endDrag);
overlay.addEventListener('pointercancel', endDrag);
$('animToggle').onchange = e => { S.anim = e.target.checked; persist(); };
$('hook').oninput = e => { S.hook = e.target.value; dirty(); };
$('hookSecs').oninput = e => { S.hookSecs = +e.target.value; persist(); syncHook(); };
$('boxToggle').onchange = e => { S.box = e.target.checked; persist(); };
$('hlToggle').onchange = e => { S.highlight = e.target.checked; persist(); };
$('capsToggle').onchange = e => { S.caps = e.target.checked; persist(); };
$('wpMinus').onclick = () => { if (S.wordsPer > 1) { S.wordsPer--; persist(); rebuildCaptions(); } };
$('wpPlus').onclick = () => { if (S.wordsPer < 6) { S.wordsPer++; persist(); rebuildCaptions(); } };
$('burnToggle').onchange = e => { S.burn = e.target.checked; persist(); };
function rebuildCaptions() { S.captions = buildCaptions(S.words, S.wordsPer); syncStyle(); dirty(); }

// ---------- Subtitles (Whisper in a worker) ----------
let worker = null;
function setBar(id, frac) {
  const bar = $(id);
  bar.classList.toggle('hidden', frac == null);
  bar.firstElementChild.style.width = `${Math.round((frac || 0) * 100)}%`;
}
const secondLang = () => (S.secondary && S.secondary !== S.primary ? S.secondary : '');
const jobKey = () => JSON.stringify({ p: S.primary, s: secondLang(), a: S.accuracy, g: S.glossary.trim(), n: S.audio16k?.length || 0 });
const jobPct = (st) => !st ? 0 : st.stage === 'main'
  ? Math.round(30 * st.mainDone / st.mainTotal)
  : Math.round(30 + 70 * (st.detectDone || 0) / (st.detectTotal || 1));
function syncGenButton() {
  const btn = $('genBtn');
  if (btn.disabled) return;
  const resumable = S.job && S.job.key === jobKey();
  btn.textContent = resumable ? `Continue subtitles (${jobPct(S.job.state)}% done)` : S.captions.length ? 'Make subtitles again' : 'Make subtitles';
  $('genRestart').classList.toggle('hidden', !resumable);
}
$('genRestart').onclick = () => { S.job = null; dirty(); syncGenButton(); };

$('genBtn').onclick = () => {
  if (!S.audio16k?.length) { $('genStatus').textContent = 'This video has no sound to transcribe.'; return; }
  const resume = S.job && S.job.key === jobKey() ? S.job.state : null;
  if (!resume) S.job = null;
  $('genRestart').classList.add('hidden');
  try { localStorage.setItem('jobRunning', JSON.stringify({ id: S.id, at: new Date().toISOString(), accuracy: S.accuracy, gpu: S.gpu })); } catch {}
  keepAwake(true);
  if (resume) L.info('Resuming subtitles', { pct: jobPct(resume) });
  worker ??= new Worker('worker.js', { type: 'module' });
  const btn = $('genBtn'), st = $('genStatus');
  btn.disabled = true; st.className = 'status'; st.textContent = 'Starting…'; setBar('genBar', 0);
  video.pause();
  const second = S.secondary && S.secondary !== S.primary ? S.secondary : '';
  const started = Date.now();
  let note = '';
  L.info('Subtitles started', { primary: S.primary, secondary: second, accuracy: S.accuracy, gpu: S.gpu, script: !!S.script.trim(), glossary: !!S.glossary.trim(), audioSecs: +(S.audio16k.length / 16000).toFixed(1) });
  let lastDl = -10;
  worker.onmessage = ({ data }) => {
    if (data.type === 'log') { L.log(data.level, 'engine: ' + data.message); return; }
    if (data.type === 'checkpoint') { S.job = { key: jobKey(), state: data.state }; dirty(true); return; }
    if (data.type === 'download' && data.progress - lastDl >= 25) { lastDl = data.progress; L.info(`Downloading ${data.label} ${Math.round(data.progress)}%`); }
    if (data.type === 'status' && data.stage !== 'detect') L.info('Subtitles: ' + data.stage, { device: data.device, threads: data.threads });
    if (data.type === 'download') {
      st.textContent = `Downloading the ${data.label} (one time)… ${Math.round(data.progress)}%`;
      setBar('genBar', data.progress / 100);
    } else if (data.type === 'gpuFailed') {
      L.warn('Graphics chip failed, using normal mode: ' + data.message);
      S.gpu = false; persist(); syncSubs();
      note = " The graphics chip didn't work here, so it used normal mode (now remembered).";
    } else if (data.type === 'status') {
      if (data.stage === 'model') st.textContent = `Loading the speech model${data.device === 'webgpu' ? ' on the graphics chip' : ''}…`;
      if (data.stage === 'loading') st.textContent = `Loading the ${data.label}…`;
      if (data.stage === 'main') {
        st.textContent = `Listening in ${langName(S.primary)}… ${data.n > 1 ? `part ${data.i}/${data.n}` : ''}`;
        setBar('genBar', 0.3 * (data.i - 1) / (data.n || 1));
      }
      if (data.stage === 'detect' && data.n) {
        st.textContent = second ? `Checking for ${langName(second)} words… ${data.i}/${data.n}` : `Checking words… ${data.i}/${data.n}`;
        setBar('genBar', 0.3 + 0.7 * data.i / data.n);
      }
    } else if (data.type === 'done') {
      localStorage.removeItem('jobRunning'); keepAwake(false);
      S.job = null;
      S.words = data.words; rebuildCaptions();
      btn.disabled = false; syncGenButton(); setBar('genBar', null);
      const secs = Math.round((Date.now() - started) / 1000);
      L.info('Subtitles done', { secs, words: S.words.length, lines: S.captions.length });
      st.textContent = (S.words.length ? `${S.captions.length} subtitle lines in ${secs} s. Check them below.` : 'No speech found in this video.') + note;
    } else if (data.type === 'error') {
      localStorage.removeItem('jobRunning'); keepAwake(false);
      btn.disabled = false; syncGenButton(); setBar('genBar', null);
      st.className = 'status error';
      st.textContent = `Subtitles failed: ${String(data.message).replace(/\.$/, '')}. Your progress is saved — tap the button to continue. (Tap ? → Copy log)`;
      L.error('Subtitles failed: ' + data.message);
    }
  };
  worker.onerror = (e) => {
    localStorage.removeItem('jobRunning'); keepAwake(false);
    btn.disabled = false; syncGenButton(); setBar('genBar', null);
    st.className = 'status error';
    st.textContent = `The speech engine couldn't start${e.message ? `: ${e.message}` : ''}. Reload the page and try again. (Tap ? → Copy log)`;
    L.error('Speech engine could not start: ' + (e.message || 'no message'), { file: (e.filename || '').split('/').pop(), line: e.lineno });
    worker = null;
  };
  worker.postMessage({
    type: 'transcribe', audio: S.audio16k, primary: S.primary, secondary: second,
    script: S.script, glossary: S.glossary, accuracy: S.accuracy, gpu: S.gpu, resume,
  });
};

// ---------- Export ----------
async function exportVideo() {
  const btn = $('exportBtn'), st = $('expStatus');
  btn.disabled = true; st.className = 'status'; st.textContent = 'Preparing…'; setBar('expBar', 0);
  video.pause();
  for (const id of ['shareVideo', 'dlVideo', 'shareSrt', 'dlSrt', 'srtNote']) $(id).classList.add('hidden');
  try {
    await document.fonts.load(fontCss(S.font, 40)).catch(() => {});
    const input = new Input({ source: new BlobSource(S.file), formats: ALL_FORMATS });
    const vTrack = await input.getPrimaryVideoTrack();
    const aTrack = await input.getPrimaryAudioTrack();

    const scale = Math.min(1, 1920 / Math.max(S.width, S.height));
    const W = Math.round(S.width * scale / 2) * 2, H = Math.round(S.height * scale / 2) * 2;
    const vCodec = await getFirstEncodableVideoCodec(['avc', 'hevc', 'vp9', 'av1'], { width: W, height: H });
    if (!vCodec) throw new Error('This browser cannot encode video');
    const aCodec = aTrack ? await getFirstEncodableAudioCodec(['aac', 'opus']) : null;
    const fps = Math.round((await vTrack.computePacketStats(120)).averagePacketRate) || 30;
    L.info('Export started', { W, H, vCodec, aCodec, fps, burn: S.burn, layout: S.layout, secs: +kept().reduce((a, r) => a + r.end - r.start, 0).toFixed(1) });
    const exportStart = Date.now();

    const canvas = new OffscreenCanvas(W, H);
    const ctx = canvas.getContext('2d');
    // Write the video straight to the device's storage when possible, so a long export
    // doesn't have to fit in memory. Falls back to memory on browsers without it.
    let fileHandle = null, target;
    try {
      const root = await navigator.storage.getDirectory();
      try { await root.removeEntry('export.mp4'); } catch {}
      fileHandle = await root.getFileHandle('export.mp4', { create: true });
      target = new StreamTarget(await fileHandle.createWritable(), { chunked: true });
    } catch { fileHandle = null; target = new BufferTarget(); }
    L.info('Export target: ' + (fileHandle ? 'device storage' : 'memory'));
    keepAwake(true);
    const output = new Output({ format: new Mp4OutputFormat({ fastStart: fileHandle ? false : 'in-memory' }), target });
    const vSource = new CanvasSource(canvas, { codec: vCodec, bitrate: QUALITY_HIGH });
    output.addVideoTrack(vSource, { frameRate: fps });
    let aSource = null;
    if (aCodec) { aSource = new AudioBufferSource({ codec: aCodec, bitrate: QUALITY_HIGH }); output.addAudioTrack(aSource); }
    await output.start();

    const ranges = kept();
    const total = ranges.reduce((a, r) => a + r.end - r.start, 0);
    const caps = S.burn ? S.captions.filter(c => c.text.trim()) : [];
    const st0 = style();
    const hook = S.hook.trim() ? S.hook : '';

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
          if (caps.length) drawCaptions(ctx, W, H, srcT, blocksAt(srcT, caps, S.layout, S.primary), st0);
          if (hook && outT < S.hookSecs) drawHook(ctx, W, H, hook, outT, st0);
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

    S.videoBlob = fileHandle ? await fileHandle.getFile() : new Blob([output.target.buffer], { type: 'video/mp4' });
    const srtCaps = outputCaptions();
    S.srtBlob = srtCaps.length ? new Blob([toSRT(srtCaps)], { type: 'text/plain' }) : null;
    const base = (S.name || 'short').replace(/\.[^.]+$/, '');

    const dl = $('dlVideo');
    if (dl.href) URL.revokeObjectURL(dl.href);
    dl.href = URL.createObjectURL(S.videoBlob); dl.download = `${base}-edited.mp4`;
    dl.classList.remove('hidden');
    if (navigator.canShare?.({ files: [new File([S.videoBlob], 'short.mp4', { type: 'video/mp4' })] })) $('shareVideo').classList.remove('hidden');
    if (S.srtBlob) {
      const ds = $('dlSrt');
      if (ds.href) URL.revokeObjectURL(ds.href);
      ds.href = URL.createObjectURL(S.srtBlob); ds.download = `${base}.srt`;
      ds.classList.remove('hidden'); $('srtNote').classList.remove('hidden');
      if (navigator.canShare?.({ files: [new File([S.srtBlob], 'subtitles.srt', { type: 'text/plain' })] })) $('shareSrt').classList.remove('hidden');
    }
    st.textContent = `Done — ${fmt(total)} long. Share it to save it to Photos.`;
    L.info('Export done', { secs: Math.round((Date.now() - exportStart) / 1000), sizeMB: +(S.videoBlob.size / 1e6).toFixed(1) });
  } catch (e) {
    st.className = 'status error';
    st.textContent = `Export failed: ${String(e.message).replace(/\.$/, '')}. (Tap ? → Copy log)`;
    L.error('Export failed: ' + e.message);
  } finally {
    keepAwake(false);
    btn.disabled = false; setBar('expBar', null);
  }
}
$('exportBtn').onclick = exportVideo;
const share = async (blob, name, type) => {
  try { await navigator.share({ files: [new File([blob], name, { type })] }); } catch (e) { if (e.name !== 'AbortError') { $('expStatus').textContent = `Sharing failed: ${e.message}`; L.error('Share failed: ' + e.message); } }
};
$('shareVideo').onclick = () => share(S.videoBlob, `${(S.name || 'short').replace(/\.[^.]+$/, '')}-edited.mp4`, 'video/mp4');
$('shareSrt').onclick = () => share(S.srtBlob, 'subtitles.srt', 'text/plain');

// ---------- Help & diagnostics ----------
async function openHelp() {
  const t = await L.text();
  $('logView').textContent = t;
  const n = L.errorCount();
  $('logSummary').textContent = n ? `${n} error${n > 1 ? 's' : ''} recorded.` : 'No errors recorded.';
  $('helpDlg').showModal();
  $('logView').scrollTop = $('logView').scrollHeight;
}
$('helpBtn').onclick = openHelp;
$('closeHelp').onclick = () => $('helpDlg').close();
$('copyLog').onclick = async () => copy(await L.text(), $('copyLog'));
$('clearLog').onclick = async () => { L.clear(); L.info('Log cleared'); openHelp(); };
$('reportLog').onclick = async () => {
  const t = await L.text();
  // Keep the link short enough for browsers: the end of the log matters most.
  const body = 'What happened:\n\n\n```\n' + (t.length > 5500 ? t.slice(0, 900) + '\n…\n' + t.slice(-4500) : t) + '\n```';
  open(`https://github.com/boyukbas/YT/issues/new?title=${encodeURIComponent('Problem in Shorts Editor')}&body=${encodeURIComponent(body)}`, '_blank');
};

window.__editor = S; // handy for debugging in the console
fillLangSelects();
for (const k of Object.keys(FONTS)) document.fonts.load(fontCss(k, 40)).catch(() => {});
showProjects();
