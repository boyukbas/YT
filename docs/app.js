import {
  Input, Output, BlobSource, BufferTarget, StreamTarget, Mp4OutputFormat, ALL_FORMATS,
  CanvasSink, AudioBufferSink, CanvasSource, AudioBufferSource, QUALITY_HIGH,
  getFirstEncodableVideoCodec, getFirstEncodableAudioCodec,
} from './lib/mediabunny.min.mjs';
import { LANGS, SR, buildCaptions, toSRT, alignScript } from './core.js';
import * as C from './claude.js';
import * as O from './openai.js';
import * as P from './publish.js';
import * as M from './merge.js';
import * as LY from './layers.js';
import { blocksAt, drawCaptions, drawHook, drawSafeZones, FONTS, fontCss } from './render.js';
import { saveFile, saveProject, listProjects, loadProject, deleteProject, loadFile } from './store.js';
import * as L from './log.js';

L.install();
document.getElementById('appVersion').textContent = `Version ${L.APP_VERSION}`;

const $ = (id) => document.getElementById(id);

// ---------- Settings (remembered between visits) ----------
const COLORS = { white: '#ffffff', yellow: '#ffd21a', cyan: '#4de6ff', green: '#73ff73', pink: '#ff80cc', orange: '#ff9e33' };
const SETTINGS = ['primary', 'secondary', 'color1', 'color2', 'caps', 'wordsPer', 'pos', 'burn', 'removePauses',
  'minPause', 'layout', 'size', 'box', 'highlight', 'accurate', 'gpu', 'posX', 'font', 'anim', 'hookSecs', 'accuracy', 'claudeModel', 'claudeFast', 'claudeAuto', 'engine', 'format', 'pauseMode'];
const saved = (() => { try { return JSON.parse(localStorage.getItem('settings') || '{}'); } catch { return {}; } })();
const S = {
  primary: 'de', secondary: 'tr', color1: 'white', color2: 'yellow', caps: true, wordsPer: 3, pos: 0.35, burn: true,
  removePauses: true, minPause: 0.4, layout: 'single', size: 0.075, box: false, highlight: false, accurate: false, gpu: true,
  posX: 0.5, font: 'modern', anim: false, hookSecs: 3, claudeModel: 'claude-sonnet-5-5', claudeFast: false, claudeAuto: true, engine: 'device', format: 'original', pauseMode: 0, accuracy: saved.accurate ? 'second' : 'normal',
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
let savePending = false;
async function saveNow() {
  clearTimeout(saveTimer);
  if (!S.id) return;
  savePending = false;
  try {
    await saveProject({
      id: S.id, name: S.name, updatedAt: Date.now(), duration: S.duration, width: S.width, height: S.height,
      trimStart: S.trimStart, trimEnd: S.trimEnd, cuts: S.cuts, threshold: S.threshold,
      words: S.words, captions: S.captions, script: S.script, glossary: S.glossary, hook: S.hook, job: S.job, pub: S.pub, thumb: S.thumb, layers: S.layers,
    });
    $('saved').textContent = 'Saved';
  } catch { $('saved').textContent = 'Not saved'; }
}
function dirty(now = false) {
  if (!S.id) return;
  $('saved').textContent = '';
  savePending = true;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, now ? 0 : 700);
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
/**
 * The parts of the original that make it into the video, each with a speed (1 = normal).
 * Cuts and pauses are either removed or sped up, depending on their setting.
 */
function kept() {
  const pauseSpeed = S.pauseMode > 1 ? S.pauseMode : 0;
  const removed = [...S.cuts.filter(c => !(c.speed > 1)), ...(S.removePauses && !pauseSpeed ? S.silences : [])];
  const fast = [...S.cuts.filter(c => c.speed > 1).map(c => ({ start: c.start, end: c.end, speed: c.speed })),
    ...(S.removePauses && pauseSpeed ? S.silences.map(p => ({ ...p, speed: pauseSpeed })) : [])];
  let ranges = subtract({ start: S.trimStart, end: S.trimEnd }, removed).map(r => ({ ...r, speed: 1 }));
  for (const f of fast) {
    ranges = ranges.flatMap(r => {
      if (f.end <= r.start || f.start >= r.end) return [r];
      const out = [];
      if (f.start > r.start) out.push({ start: r.start, end: f.start, speed: r.speed });
      out.push({ start: Math.max(r.start, f.start), end: Math.min(r.end, f.end), speed: Math.max(r.speed, f.speed) });
      if (f.end < r.end) out.push({ start: f.end, end: r.end, speed: r.speed });
      return out;
    });
  }
  return ranges.filter(r => r.end - r.start > 0.02);
}
const outLen = (r) => (r.end - r.start) / (r.speed || 1);
const outLength = () => kept().reduce((a, r) => a + outLen(r), 0);
function toOutput(t, ranges = kept()) {
  let acc = 0;
  for (const r of ranges) {
    if (t < r.start) return acc;
    if (t <= r.end) return acc + (t - r.start) / (r.speed || 1);
    acc += outLen(r);
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
        captions: project.captions || [], script: project.script || '', glossary: project.glossary || '', hook: project.hook || '', job: project.job || null, pub: { ...newPub(), ...(project.pub || {}) }, thumb: project.thumb || null, layers: { ...LY.defaultLayers(), ...(project.layers || {}) },
      });
    } else {
      Object.assign(S, {
        id: crypto.randomUUID(), name: file.name || 'Video', trimStart: 0, trimEnd: S.duration, cuts: [],
        threshold: suggestThreshold(S.levels), words: [], captions: [], script: '', glossary: '', hook: '', job: null, pub: newPub(), thumb: null, layers: LY.defaultLayers(),
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
    if (!S.thumb) captureProjectThumb();
    S.presDrag = false; lastMask = null; loadBgForProject();
    setTab(S.captions.length ? 'subs' : 'video');
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
    for (const id of ['shareSrt', 'dlSrt', 'srtNote']) $(id).classList.add('hidden');
    st.textContent = '';
    syncAll();
    layoutStage();
    scrollTo(0, 0);
    if (history.state?.view !== 'editor') history.pushState({ view: 'editor' }, '');
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
    if (p.thumb) { const img = document.createElement('img'); img.src = p.thumb; img.alt = ''; row.appendChild(img); }
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
// ---------- Back button: go back to the projects screen instead of closing the app ----------
let busy = null;   // 'subtitles' | 'cloud' | 'claude' | 'export' while a job runs
function leaveEditor() {
  if (busy && busy !== 'subtitles') {
    // Cloud and export jobs can't be paused safely; stay until they finish.
    $(busy === 'export' ? 'expStatus' : 'genStatus').textContent += ' — please wait for this to finish before leaving.';
    return false;
  }
  if (busy === 'subtitles' && worker) {
    // Stop on-device subtitles; their progress is saved and can be continued later.
    worker.terminate(); worker = null; busy = null;
    localStorage.removeItem('jobRunning'); keepAwake(false);
    $('genBtn').disabled = false; setBar('genBar', null);
    L.info('Subtitles paused (left the project); progress saved');
  }
  // Save any pending edits first, so the projects list is up to date.
  (savePending ? saveNow() : Promise.resolve()).then(showProjects);
  return true;
}
$('newBtn').onclick = () => { if (history.state?.view === 'editor') history.back(); else leaveEditor(); };
let ignorePop = false;
addEventListener('popstate', () => {
  if (ignorePop) { ignorePop = false; return; }
  if ($('helpDlg').open) { $('helpDlg').close(); return; }
  if ($('settingsDlg').open) { $('settingsDlg').close(); return; }
  if ($('mergeDlg').open) { if (!merging) $('mergeDlg').close(); else history.pushState({ view: 'merge' }, ''); return; }
  if (!$('editor').classList.contains('hidden')) {
    if (!leaveEditor()) history.pushState({ view: 'editor' }, '');   // stay
  }
});

// ---------- Preview ----------
const video = $('video');
const overlay = $('overlay');
function layoutStage() {
  if (!S.width) return;
  const ar = S.width / S.height;
  const maxW = $('editor').clientWidth, maxH = window.innerHeight * 0.36;
  const h = Math.min(maxH, maxW / ar), w = h * ar;  // compact: the preview stays on screen above the tabs
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
      const rate = inside?.speed || 1;
      if (video.playbackRate !== rate) video.playbackRate = rate;
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
    if (S.format !== 'original') drawFormatPreview();
    drawTimeline();
    drawComposite();
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
    const row = document.createElement('div'); row.className = 'item'; row.style.flexWrap = 'wrap'; row.style.gap = '6px';
    const label = document.createElement('span'); label.style.flex = '1 1 100%';
    label.textContent = `${c.capId ? 'Line' : 'Part'} ${fmt(c.start)} – ${fmt(c.end)} (${(c.end - c.start).toFixed(1)} s): ${c.speed > 1 ? `played ${c.speed}× faster` : 'cut out'}`;
    const b = (txt, aria, fn) => { const x = document.createElement('button'); x.className = 'btn'; x.style.padding = '6px 10px'; x.textContent = txt; x.setAttribute('aria-label', aria); x.onclick = fn; return x; };
    const move = (edge, d) => () => {
      if (edge === 'start') c.start = Math.max(0, Math.min(c.end - 0.1, c.start + d)); else c.end = Math.min(S.duration, Math.max(c.start + 0.1, c.end + d));
      video.pause(); video.currentTime = edge === 'start' ? c.start : c.end;
      syncTrim(); syncLength(); dirty();
    };
    const modes = document.createElement('div'); modes.className = 'seg'; modes.style.flex = '1 1 100%';
    for (const [v, l] of [[0, 'Cut'], [2, '2×'], [4, '4×'], [8, '8×']]) {
      const m = document.createElement('button'); m.textContent = l; m.setAttribute('aria-pressed', String((c.speed || 0) === v));
      m.onclick = () => { c.speed = v || undefined; syncTrim(); syncLength(); dirty(); renderCapList(); };
      modes.appendChild(m);
    }
    row.append(label, modes,
      b('Start −', 'Cut starts earlier', move('start', -0.1)), b('Start +', 'Cut starts later', move('start', 0.1)),
      b('End −', 'Cut ends earlier', move('end', -0.1)), b('End +', 'Cut ends later', move('end', 0.1)),
      b('▶ Check', 'Play around this cut', () => { video.currentTime = Math.max(0, c.start - 2); video.play(); }),
      b('×', 'Remove this cut', () => { S.cuts.splice(i, 1); syncTrim(); syncLength(); dirty(); renderCapList(); }));
    list.appendChild(row);
  });
}
function syncPauses() {
  $('pauseToggle').checked = S.removePauses;
  $('pauseOpts').classList.toggle('hidden', !S.removePauses);
  $('thresh').value = S.threshold; $('threshOut').textContent = `${S.threshold} dB`;
  $('minPause').value = S.minPause; $('minPauseOut').textContent = `${S.minPause.toFixed(2)} s`;
  $('pauseCount').textContent = `${S.silences.length} pauses found`;
  for (const b of $('pauseModeSeg').children) b.setAttribute('aria-pressed', String(+b.dataset.v === (S.pauseMode || 0)));
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
  $('lookHint').classList.toggle('hidden', has);
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
    const cutIdx = S.cuts.findIndex(x => x.capId === c.id);
    const scissors = mk('button', 'del', '✂', cutIdx >= 0 ? 'Keep this part of the video' : 'Cut this part out of the video', () => {
      if (cutIdx >= 0) S.cuts.splice(cutIdx, 1);
      else { S.cuts.push({ start: snapToQuiet(Math.max(0, c.start - 0.05)), end: snapToQuiet(Math.min(S.duration, c.end + 0.05)), capId: c.id }); S.cuts.sort((x, y) => x.start - y.start); }
      renderCapList(); syncTrim(); syncLength(); dirty();
    });
    if (cutIdx >= 0) scissors.style.color = 'var(--danger)';
    top.append(scissors, play, mk('button', 'del', '×', 'Delete this subtitle line (the video stays)', () => { S.captions.splice(i, 1); renderCapList(); dirty(); }));

    const ta = mk('textarea'); ta.rows = 1; ta.value = c.text; ta.style.color = colorFor(c.lang);
    if (cutIdx >= 0) { ta.style.textDecoration = 'line-through'; ta.style.opacity = '0.5'; }
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
function syncAll() {
  syncTrim(); syncPauses(); syncLength(); syncSubs(); syncStyle(); syncHook(); syncFormat(); $('exportList').innerHTML = '';
  frames = []; thumbFrame = null; $('frameGrid').innerHTML = ''; $('ytStatus').textContent = ''; syncPub();
  if (S.pub.t != null) selectFrame(S.pub.t);
}

const nowT = () => video.currentTime;
$('trimStart').addEventListener('input', e => {
  S.trimStart = Math.min(+e.target.value, S.trimEnd - 0.5); syncTrim(); syncLength(); video.currentTime = S.trimStart; dirty();
});
$('trimEnd').addEventListener('input', e => {
  S.trimEnd = Math.max(+e.target.value, S.trimStart + 0.5); syncTrim(); syncLength(); video.currentTime = Math.max(0, S.trimEnd - 0.05); dirty();
});
$('startHere').onclick = () => { S.trimStart = Math.min(nowT(), S.trimEnd - 0.5); syncTrim(); syncLength(); dirty(); };
$('endHere').onclick = () => { S.trimEnd = Math.max(nowT(), S.trimStart + 0.5); syncTrim(); syncLength(); dirty(); };
/** Moves a cut edge to the nearest quiet moment within 0.25 s, so cuts don't clip words. */
function snapToQuiet(t) {
  if (!S.levels.length) return t;
  const i0 = Math.round(t / HOP), r = Math.round(0.25 / HOP);
  let best = i0, bestDb = Infinity;
  for (let i = Math.max(0, i0 - r); i <= Math.min(S.levels.length - 1, i0 + r); i++) {
    const db = S.levels[i] + Math.abs(i - i0) * 0.4;   // prefer quiet, then close
    if (db < bestDb) { bestDb = db; best = i; }
  }
  return Math.max(0, Math.min(S.duration, best * HOP));
}
$('cutIn').onclick = () => { S.cutFrom = nowT(); syncTrim(); };
$('cutOut').onclick = () => {
  const a = Math.min(S.cutFrom, nowT()), b = Math.max(S.cutFrom, nowT());
  if (b - a >= 0.1) { S.cuts.push({ start: snapToQuiet(a), end: snapToQuiet(b) }); S.cuts.sort((x, y) => x.start - y.start); }
  S.cutFrom = null; syncTrim(); syncLength(); dirty();
};
$('pauseToggle').onchange = e => { S.removePauses = e.target.checked; persist(); syncPauses(); syncLength(); };
for (const b of $('pauseModeSeg').children) b.onclick = () => { S.pauseMode = +b.dataset.v; persist(); syncPauses(); syncLength(); };
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
  if (S.presDrag) {
    S.layers.x = Math.min(0.95, Math.max(0.05, (e.clientX - r.left) / r.width));
    S.layers.y = Math.min(0.95, Math.max(0.05, (e.clientY - r.top) / r.height));
    if (S.layers.size >= 0.999) S.layers.size = 0.5;
    syncLayers(); dirty(); return;
  }
  S.posX = Math.min(0.85, Math.max(0.15, (e.clientX - r.left) / r.width));
  S.pos = Math.min(0.92, Math.max(0.08, 1 - (e.clientY - r.top) / r.height));
  $('pos').value = S.pos; syncPos();
};
overlay.addEventListener('pointerdown', (e) => { if (!S.dragMode && !S.presDrag) return; dragging = true; overlay.setPointerCapture(e.pointerId); placeAt(e); });
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
const claudeHandlesLangs = () => !!(S.claudeFast && C.getKey() && secondLang());
const jobKey = () => JSON.stringify({ p: S.primary, s: secondLang(), a: S.accuracy, g: S.glossary.trim(), n: S.audio16k?.length || 0, d: !claudeHandlesLangs() });
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
  if (S.engine === 'openai') { runCloud(); return; }
  const resume = S.job && S.job.key === jobKey() ? S.job.state : null;
  if (!resume) S.job = null;
  $('genRestart').classList.add('hidden');
  try { localStorage.setItem('jobRunning', JSON.stringify({ id: S.id, at: new Date().toISOString(), accuracy: S.accuracy, gpu: S.gpu })); } catch {}
  keepAwake(true);
  if (resume) L.info('Resuming subtitles', { pct: jobPct(resume) });
  worker ??= new Worker('worker.js', { type: 'module' });
  const btn = $('genBtn'), st = $('genStatus');
  btn.disabled = true; st.className = 'status'; st.textContent = 'Starting…'; setBar('genBar', 0);
  busy = 'subtitles';
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
    if (data.type === 'status' && data.stage === 'retry') { L.warn(`Subtitles: part ${data.i}/${data.n} re-read in short slices`, data.info); return; }
    if (data.type === 'status' && data.stage !== 'detect') L.info('Subtitles: ' + data.stage, { device: data.device, threads: data.threads, part: data.n ? `${data.i}/${data.n}` : undefined });
    if (data.type === 'status' && data.stage === 'detect' && data.n && (data.i % 10 === 0 || data.i === data.n)) {
      L.info(`Subtitles: checked ${data.i}/${data.n} phrases`, { secs: Math.round((Date.now() - started) / 1000) });
    }
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
      busy = null;
      localStorage.removeItem('jobRunning'); keepAwake(false);
      S.job = null;
      S.words = data.words; rebuildCaptions();
      btn.disabled = false; syncGenButton(); setBar('genBar', null);
      const secs = Math.round((Date.now() - started) / 1000);
      L.info('Subtitles done', { secs, words: S.words.length, lines: S.captions.length });
      st.textContent = (S.words.length ? `${S.captions.length} subtitle lines in ${secs} s. Check them below.` : 'No speech found in this video.') + note;
      if (S.words.length && C.getKey() && (S.claudeAuto || claudeHandlesLangs())) runClaudeFix();
    } else if (data.type === 'error') {
      busy = null;
      localStorage.removeItem('jobRunning'); keepAwake(false);
      btn.disabled = false; syncGenButton(); setBar('genBar', null);
      st.className = 'status error';
      st.textContent = `Subtitles failed: ${String(data.message).replace(/\.$/, '')}. Your progress is saved — tap the button to continue. (Tap ? → Copy log)`;
      L.error('Subtitles failed: ' + data.message);
    }
  };
  worker.onerror = (e) => {
    busy = null;
    localStorage.removeItem('jobRunning'); keepAwake(false);
    btn.disabled = false; syncGenButton(); setBar('genBar', null);
    st.className = 'status error';
    st.textContent = `The speech engine couldn't start${e.message ? `: ${e.message}` : ''}. Reload the page and try again. (Tap ? → Copy log)`;
    L.error('Speech engine could not start: ' + (e.message || 'no message'), { file: (e.filename || '').split('/').pop(), line: e.lineno });
    worker = null;
  };
  worker.postMessage({
    type: 'transcribe', audio: S.audio16k, primary: S.primary, secondary: second,
    script: S.script, glossary: S.glossary, accuracy: S.accuracy, gpu: S.gpu, resume, detect: !claudeHandlesLangs(),
  });
};

// ---------- Export ----------
async function exportVideo(format = S.format) {
  const btn = $('exportBtn'), st = $('expStatus');
  const vertical = format !== 'original' && S.width > S.height * 0.6;
  // Guard against exporting almost nothing by accident (trim, cuts or pause settings).
  const L0 = outLength();
  if (S.duration > 10 && L0 < S.duration * 0.2) {
    const why = [S.trimStart > 0.5 || S.trimEnd < S.duration - 0.5 ? `trim ${fmt(S.trimStart)}–${fmt(S.trimEnd)}` : '',
      S.cuts.length ? `${S.cuts.length} cut(s)` : '', S.removePauses ? `pause removal (${S.silences.length} pauses)` : ''].filter(Boolean).join(', ');
    L.warn('Export would be very short', { secs: +L0.toFixed(1), duration: +S.duration.toFixed(1), why });
    if (!confirm(`The edited video would be only ${fmt(L0)} of ${fmt(S.duration)}. Check: ${why || 'your edits'}. Export anyway?`)) return;
  }
  btn.disabled = true; st.className = 'status'; st.textContent = 'Preparing…'; setBar('expBar', 0);
  busy = 'export';
  video.pause();
  for (const id of ['shareSrt', 'dlSrt', 'srtNote']) $(id).classList.add('hidden');
  try {
    await document.fonts.load(fontCss(S.font, 40)).catch(() => {});
    const input = new Input({ source: new BlobSource(S.file), formats: ALL_FORMATS });
    const vTrack = await input.getPrimaryVideoTrack();
    const aTrack = await input.getPrimaryAudioTrack();

    const scale = Math.min(1, 1920 / Math.max(S.width, S.height));
    // Source frames at (at most) 1080p; the output is either the same or a 1080×1920 Short.
    const SW = Math.round(S.width * scale / 2) * 2, SH = Math.round(S.height * scale / 2) * 2;
    const W = vertical ? 1080 : SW, H = vertical ? 1920 : SH;
    const vCodec = await getFirstEncodableVideoCodec(['avc', 'hevc', 'vp9', 'av1'], { width: W, height: H });
    if (!vCodec) throw new Error('This browser cannot encode video');
    const aCodec = aTrack ? await getFirstEncodableAudioCodec(['aac', 'opus']) : null;
    const fps = Math.round((await vTrack.computePacketStats(120)).averagePacketRate) || 30;
    L.info('Export started', { layers: LY.layersActive(S.layers) ? { bg: S.layers.bg, cut: S.layers.cut, size: S.layers.size } : 'off', W, H, vCodec, aCodec, fps, burn: S.burn, layout: S.layout, secs: +outLength().toFixed(1),
      trim: [+S.trimStart.toFixed(1), +S.trimEnd.toFixed(1)], cuts: S.cuts.length, removePauses: S.removePauses, pauses: S.silences.length, threshold: S.threshold, duration: +S.duration.toFixed(1) });
    const exportStart = Date.now();

    const canvas = new OffscreenCanvas(W, H);
    const ctx = canvas.getContext('2d');
    // Write the video straight to the device's storage when possible, so a long export
    // doesn't have to fit in memory. Falls back to memory on browsers without it.
    let fileHandle = null, target;
    try {
      const root = await navigator.storage.getDirectory();
      const fname = `export-${vertical ? 'short' : 'video'}.mp4`;
      try { await root.removeEntry(fname); } catch {}
      fileHandle = await root.getFileHandle(fname, { create: true });
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
    const total = ranges.reduce((a, r) => a + outLen(r), 0);
    const caps = S.burn ? S.captions.filter(c => c.text.trim()) : [];
    const st0 = style();
    // Layers for export: person detector, background picture or video.
    const Lx = S.layers, layersOn = LY.layersActive(Lx);
    if (layersOn && Lx.cut && !(await ensureSegmenter())) throw new Error('the person detector could not load');
    const exMasker = layersOn && Lx.cut ? new LY.Masker() : null;
    const exBgVideo = layersOn && Lx.bg === 'file' && bgIsVideo && S.bgFile ? await new LY.BgVideo().open(S.bgFile) : null;
    const layerCanvas = layersOn ? new OffscreenCanvas(SW, SH) : null;
    const hook = S.hook.trim() ? S.hook : '';

    const doVideo = async () => {
      const sink = new CanvasSink(vTrack, { width: SW, height: SH, fit: 'fill' });
      let acc = 0, lastOut = -1;
      for (const r of ranges) {
        for await (const f of sink.canvases(r.start, r.end)) {
          if (f.timestamp >= r.end) break;
          if (f.timestamp + f.duration <= r.start) continue;
          const srcT = Math.max(f.timestamp, r.start);
          const sp = r.speed || 1;
          const outT = acc + (srcT - r.start) / sp;
          // Sped-up parts: skip frames that would come too close together.
          if (outT <= lastOut || (sp > 1 && outT - lastOut < 0.9 / fps)) continue;
          if (layersOn) {
            const bgFrame = Lx.bg === 'file' ? (exBgVideo ? await exBgVideo.frameAt(outT) : bgImage) : null;
            LY.composite(layerCanvas.getContext('2d'), SW, SH, f.canvas, SW, SH, exMasker ? exMasker.mask(f.canvas) : null, bgFrame, Lx);
            composeFrame(ctx, layerCanvas, SW, SH, W, H, vertical ? format : 'original');
          } else composeFrame(ctx, f.canvas, SW, SH, W, H, vertical ? format : 'original');
          if (caps.length) drawCaptions(ctx, W, H, srcT, blocksAt(srcT, caps, S.layout, S.primary), st0);
          if (hook && outT < S.hookSecs) drawHook(ctx, W, H, hook, outT, st0);
          const dur = Math.min(f.duration, r.end - srcT) / sp;
          await vSource.add(outT, dur > 0 ? Math.max(dur, 1 / fps) : 1 / fps);
          lastOut = outT;
          st.textContent = `Exporting… ${Math.min(100, Math.round(outT / total * 100))}%`;
          setBar('expBar', outT / total);
        }
        acc += outLen(r);
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
        if ((r.speed || 1) > 1) { await addFastAudio(sink, r); continue; }
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
    // A sped-up part: 2× keeps the sound (faster and higher), faster than that is silent.
    const addFastAudio = async (sink, r) => {
      const chunks = []; let sr = 48000, chs = 2, len = 0;
      for await (const { buffer, timestamp } of sink.buffers(r.start, r.end)) {
        sr = buffer.sampleRate; chs = buffer.numberOfChannels;
        const s0 = Math.max(0, Math.round((r.start - timestamp) * sr)), s1 = Math.min(buffer.length, Math.round((r.end - timestamp) * sr));
        if (s1 <= s0) continue;
        chunks.push(Array.from({ length: chs }, (_, c) => buffer.getChannelData(c).slice(s0, s1))); len += s1 - s0;
      }
      const outFrames = Math.max(1, Math.round(len / r.speed));
      let out;
      if (r.speed <= 2 && len) {
        const off = new OfflineAudioContext(chs, outFrames, sr);
        const src = off.createBuffer(chs, len, sr);
        let o = 0; for (const c of chunks) { c.forEach((d, ch) => src.copyToChannel(d, ch, o)); o += c[0].length; }
        const node = off.createBufferSource(); node.buffer = src; node.playbackRate.value = r.speed; node.connect(off.destination); node.start();
        out = await off.startRendering();
      } else {
        out = new AudioBuffer({ length: outFrames, numberOfChannels: chs, sampleRate: sr });
      }
      await aSource.add(out);
    };
    await Promise.all([doVideo(), doAudio()]);
    st.textContent = 'Finishing…';
    await output.finalize();

    S.videoBlob = fileHandle ? await fileHandle.getFile() : new Blob([output.target.buffer], { type: 'video/mp4' });
    const srtCaps = outputCaptions();
    S.srtBlob = srtCaps.length ? new Blob([toSRT(srtCaps)], { type: 'text/plain' }) : null;
    const base = (S.name || 'short').replace(/\.[^.]+$/, '');
    addExportResult(S.videoBlob, `${base}-${vertical ? 'short' : 'video'}.mp4`, vertical ? 'Short (9:16)' : 'Video', total);
    if (S.srtBlob) {
      const ds = $('dlSrt');
      if (ds.href) URL.revokeObjectURL(ds.href);
      ds.href = URL.createObjectURL(S.srtBlob); ds.download = `${base}.srt`;
      ds.classList.remove('hidden'); $('srtNote').classList.remove('hidden');
      if (navigator.canShare?.({ files: [new File([S.srtBlob], 'subtitles.srt', { type: 'text/plain' })] })) $('shareSrt').classList.remove('hidden');
    }
    st.textContent = `Done — ${vertical ? 'Short' : 'video'}, ${fmt(total)} long. Share it to save it to Photos.`;
    if (vertical && total > 180) st.textContent += ' Note: Shorts can be at most 3 minutes.';
    L.info('Export done', { format, secs: Math.round((Date.now() - exportStart) / 1000), sizeMB: +(S.videoBlob.size / 1e6).toFixed(1) });
  } catch (e) {
    st.className = 'status error';
    st.textContent = `Export failed: ${String(e.message).replace(/\.$/, '')}. (Tap ? → Copy log)`;
    L.error('Export failed: ' + e.message);
  } finally {
    busy = null; keepAwake(false);
    btn.disabled = false; setBar('expBar', null);
  }
}
$('exportBtn').onclick = () => exportVideo(S.format);
$('exportBoth').onclick = async () => {
  await exportVideo('original');
  if (!$('expStatus').classList.contains('error')) await exportVideo(S.format === 'original' ? 'blur' : S.format);
};

/** Draws one source frame into the output: as is, cropped to 9:16, or fitted on a blurred 9:16 background. */
const blurCanvas = new OffscreenCanvas(54, 96), blurMid = new OffscreenCanvas(216, 384);
function composeFrame(ctx, src, sw, sh, W, H, format) {
  if (format === 'original') { ctx.drawImage(src, 0, 0, W, H); return; }
  const coverScale = Math.max(W / sw, H / sh);
  const cw = W / coverScale, ch = H / coverScale;
  if (format === 'crop') { ctx.drawImage(src, (sw - cw) / 2, (sh - ch) / 2, cw, ch, 0, 0, W, H); return; }
  // Blurred background: draw tiny, then stretch (a cheap blur that works in every browser).
  const bc = blurCanvas.getContext('2d');
  bc.drawImage(src, (sw - cw) / 2, (sh - ch) / 2, cw, ch, 0, 0, 54, 96);
  const mc = blurMid.getContext('2d'); mc.imageSmoothingEnabled = true; mc.imageSmoothingQuality = 'high';
  mc.drawImage(blurCanvas, 0, 0, 54, 96, 0, 0, 216, 384);
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(blurMid, 0, 0, 216, 384, 0, 0, W, H);
  ctx.fillStyle = 'rgba(0,0,0,0.3)'; ctx.fillRect(0, 0, W, H);
  const fit = Math.min(W / sw, H / sh), fw = sw * fit, fh = sh * fit;
  ctx.drawImage(src, (W - fw) / 2, (H - fh) / 2, fw, fh);
}

function addExportResult(blob, name, label, secs) {
  const row = document.createElement('div');
  row.className = 'cutlist';
  const info = document.createElement('p'); info.className = 'muted';
  info.textContent = `${label} · ${fmt(secs)} · ${(blob.size / 1e6).toFixed(1)} MB`;
  const pair = document.createElement('div'); pair.className = 'pair';
  if (navigator.canShare?.({ files: [new File([blob], name, { type: 'video/mp4' })] })) {
    const sh = document.createElement('button'); sh.className = 'btn'; sh.textContent = 'Share or save';
    sh.onclick = () => share(blob, name, 'video/mp4'); pair.appendChild(sh);
  }
  const dl = document.createElement('a'); dl.className = 'btn'; dl.textContent = 'Download';
  dl.style.textAlign = 'center'; dl.style.textDecoration = 'none';
  dl.href = URL.createObjectURL(blob); dl.download = name; pair.appendChild(dl);
  row.append(info, pair);
  $('exportList').prepend(row);
}

// Still preview of the chosen format (from the video at the playhead).
let fmtPreviewAt = 0;
function drawFormatPreview(force = false) {
  const cv = $('fmtPreview');
  const show = S.format !== 'original' && S.width > S.height * 0.6;
  cv.classList.toggle('hidden', !show);
  if (!show || !video.videoWidth || (!force && performance.now() - fmtPreviewAt < 400)) return;
  fmtPreviewAt = performance.now();
  const W = 270, H = 480, ctx = cv.getContext('2d');
  cv.width = W; cv.height = H;
  const srcEl = LY.layersActive(S.layers) && comp.width ? comp : video;
  composeFrame(ctx, srcEl, srcEl === comp ? comp.width : video.videoWidth, srcEl === comp ? comp.height : video.videoHeight, W, H, S.format);
  const t = video.currentTime;
  drawCaptions(ctx, W, H, t, blocksAt(t, S.captions, S.layout, S.primary), style());
  const outT = toOutput(t);
  if (S.hook.trim() && outT < S.hookSecs) drawHook(ctx, W, H, S.hook, outT, style());
}
function syncFormat() {
  for (const b of $('fmtSeg').children) b.setAttribute('aria-pressed', String(b.dataset.v === S.format));
  $('exportBoth').textContent = S.format === 'original' ? 'Export both: video + Short (blur)' : 'Export both: video + this Short';
  drawFormatPreview(true);
}
for (const b of $('fmtSeg').children) b.onclick = () => { S.format = b.dataset.v; persist(); syncFormat(); };

const share = async (blob, name, type) => {
  try { await navigator.share({ files: [new File([blob], name, { type })] }); } catch (e) { if (e.name !== 'AbortError') { $('expStatus').textContent = `Sharing failed: ${e.message}`; L.error('Share failed: ' + e.message); } }
};
$('shareSrt').onclick = () => share(S.srtBlob, 'subtitles.srt', 'text/plain');

// ---------- OpenAI cloud speech recognition (optional, own API key) ----------
function syncEngine() {
  for (const b of $('engineSeg').children) b.setAttribute('aria-pressed', String(b.dataset.v === S.engine));
  $('openaiBox').classList.toggle('hidden', S.engine !== 'openai');
  $('deviceOpts').classList.toggle('hidden', S.engine === 'openai');
  const k = O.getKey();
  $('openaiKey').value = k ? '••••••••••••' + k.slice(-4) : '';
}
for (const b of $('engineSeg').children) b.onclick = () => { S.engine = b.dataset.v; persist(); syncEngine(); };
$('openaiKey').onfocus = (e) => { if (e.target.value.startsWith('••')) e.target.value = ''; };
$('openaiKey').onchange = (e) => {
  const v = e.target.value.trim();
  if (v && !v.startsWith('••')) {
    const shape = O.keyShape(v);
    O.setKey(v); L.info('OpenAI key saved', shape);
    $('openaiStatus').textContent = shape.looksLikeClaudeKey
      ? 'This is a Claude key (sk-ant-…) — it goes in the Claude settings below. OpenAI keys start with sk-proj-.'
      : shape.looksLikeOpenAIKey ? 'Key saved. Tap “Test key”.' : 'This doesn’t look like an OpenAI key (they start with sk-). Check that you copied all of it.';
  }
  syncEngine();
};
$('openaiRemove').onclick = () => { O.setKey(''); L.info('OpenAI key removed'); $('openaiStatus').textContent = 'Key removed from this device.'; syncEngine(); };
$('openaiTest').onclick = async () => {
  const st = $('openaiStatus');
  if (!O.getKey()) { st.textContent = 'Paste your OpenAI key first.'; return; }
  st.className = 'status'; st.textContent = 'Testing…';
  try { await O.testKey(O.getKey()); st.textContent = 'The key works ✓'; L.info('OpenAI key test ok'); }
  catch (e) { st.className = 'status error'; st.textContent = e.message; L.error('OpenAI key test failed: ' + e.message); }
};

async function runCloud() {
  const btn = $('genBtn'), st = $('genStatus');
  if (!O.getKey()) { st.className = 'status error'; st.textContent = 'Add your OpenAI key first (Speech recognition → OpenAI).'; return; }
  btn.disabled = true; st.className = 'status'; st.textContent = 'Sending the sound to OpenAI…'; setBar('genBar', 0.1);
  busy = 'cloud';
  video.pause(); keepAwake(true);
  const second = secondLang();
  const t0 = Date.now();
  L.info('Subtitles started (OpenAI)', { primary: S.primary, secondary: second, script: !!S.script.trim(), glossary: !!S.glossary.trim(), audioSecs: +(S.audio16k.length / 16000).toFixed(1) });
  try {
    let words = await O.transcribe({
      key: O.getKey(), audio: S.audio16k, primary: S.primary, secondary: second,
      names: { primary: langName(S.primary), secondary: second ? langName(second) : '' }, glossary: S.glossary,
      onStatus: (i, n) => { st.textContent = `OpenAI is transcribing… ${n > 1 ? `part ${i}/${n}` : ''}`; setBar('genBar', 0.1 + 0.8 * (i - 1) / n); },
    });
    if (S.script.trim()) words = alignScript(S.script, words, S.primary, second);
    S.words = words; S.job = null; rebuildCaptions();
    const secs = Math.round((Date.now() - t0) / 1000);
    L.info('Subtitles done (OpenAI)', { secs, words: words.length, lines: S.captions.length });
    const needLangs = second && !S.script.trim();
    st.textContent = `${S.captions.length} subtitle lines in ${secs} s.` + (needLangs && !C.getKey()
      ? ` All lines are marked as ${langName(S.primary)} — add a Claude key to label languages automatically, or tap the tags below.` : '');
    if (words.length && C.getKey() && (needLangs || S.claudeAuto)) { btn.disabled = false; await runClaudeFix(); }
  } catch (e) {
    st.className = 'status error';
    st.textContent = `Subtitles failed: ${String(e.message).replace(/\.$/, '')}. (Tap ? → Copy log)`;
    L.error('OpenAI transcription failed: ' + e.message);
  } finally {
    busy = null; keepAwake(false); btn.disabled = false; syncGenButton(); setBar('genBar', null);
  }
}
syncEngine();

// ---------- Claude (optional, own API key) ----------
function syncClaude() {
  const has = !!C.getKey();
  $('claudeHint').textContent = has ? 'on' : 'not set up';
  $('claudeKey').value = has ? '••••••••••••' + C.getKey().slice(-4) : '';
  $('claudeModel').value = S.claudeModel;
  $('claudeFast').checked = S.claudeFast; $('claudeAuto').checked = S.claudeAuto;
  $('fixClaude').classList.toggle('hidden', !has);
}
$('claudeModel').innerHTML = Object.entries(C.CLAUDE_MODELS).map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
$('claudeKey').onchange = (e) => {
  const v = e.target.value.trim();
  if (v && !v.startsWith('••')) {
    const shape = C.keyShape(v);
    C.setKey(v); L.info('Claude key saved', shape);
    if (shape.looksLikeOpenAIKey) $('claudeStatus').textContent = 'This is an OpenAI key (sk-proj-…), not a Claude key. Create a key at console.anthropic.com — it starts with sk-ant-api.';
    else if (shape.looksLikeAdminKey) $('claudeStatus').textContent = 'This looks like an Admin key — use a normal API key (starts with sk-ant-api).';
    else if (!shape.looksLikeApiKey) $('claudeStatus').textContent = 'This doesn’t look like a Claude API key (they start with sk-ant-api). Check that you copied all of it.';
    else $('claudeStatus').textContent = 'Key saved. Tap “Test key”.';
  }
  syncClaude(); syncGenButton();
};
$('claudeKey').onfocus = (e) => { if (e.target.value.startsWith('••')) e.target.value = ''; };
$('claudeModel').onchange = (e) => { S.claudeModel = e.target.value; persist(); };
$('claudeFast').onchange = (e) => { S.claudeFast = e.target.checked; persist(); syncGenButton(); };
$('claudeAuto').onchange = (e) => { S.claudeAuto = e.target.checked; persist(); };
$('claudeRemove').onclick = () => { C.setKey(''); L.info('Claude key removed'); $('claudeStatus').textContent = 'Key removed from this device.'; syncClaude(); };
$('claudeTest').onclick = async () => {
  const st = $('claudeStatus');
  if (!C.getKey()) { st.textContent = 'Paste your API key first.'; return; }
  st.className = 'status'; st.textContent = 'Testing…';
  try { await C.testKey(C.getKey(), S.claudeModel); st.textContent = 'The key works ✓'; L.info('Claude key test ok', { model: S.claudeModel }); }
  catch (e) { st.className = 'status error'; st.textContent = e.message; L.error('Claude key test failed: ' + e.message); }
};

let fixing = false;
async function runClaudeFix() {
  if (fixing || !S.captions.length) return;
  fixing = true; const prevBusy = busy; busy = 'claude';
  const st = $('genStatus'), btn = $('fixClaude');
  btn.disabled = true;
  const before = st.textContent;
  st.className = 'status'; st.textContent = 'Claude is fixing the words…';
  const t0 = Date.now();
  try {
    const transcript = transcriptText();
    const { text, usage, parts } = await C.fixTranscript({
      key: C.getKey(), model: S.claudeModel, primary: S.primary, secondary: secondLang(),
      names: { primary: langName(S.primary), secondary: secondLang() ? langName(secondLang()) : '' },
      glossary: S.glossary, transcript,
      onPart: (i, n) => { if (n > 1) st.textContent = `Claude is fixing the words… part ${i}/${n}`; },
    });
    if (!text.trim()) throw new Error('Claude returned no text');
    // Safety check: the fix must cover about as much as the original, or it's not applied.
    const count = (t) => t.replace(/^\s*\[[a-z]{2,3}\]/gim, '').split(/\s+/).filter(Boolean).length;
    const before = count(transcript), after = count(text);
    L.info('Claude answer', { parts, wordsBefore: before, wordsAfter: after });
    if (after < before * 0.7) throw new Error(`Claude's text was much shorter (${after} vs ${before} words) — nothing was changed`);
    S.undo = { words: S.words, captions: S.captions, script: S.script };
    S.script = text;
    S.words = alignScript(text, S.words, S.primary, secondLang());
    rebuildCaptions(); syncSubs();
    $('undoClaude').classList.remove('hidden');
    st.textContent = `Fixed by Claude: ${S.captions.length} lines. The corrected text is in “My script”.`;
    L.info('Claude fix done', { model: S.claudeModel, secs: Math.round((Date.now() - t0) / 1000), inTokens: usage.input_tokens, outTokens: usage.output_tokens, lines: S.captions.length, endsAt: +(S.captions.at(-1)?.end || 0).toFixed(1) });
  } catch (e) {
    st.className = 'status error';
    st.textContent = `Claude fix failed: ${String(e.message).replace(/\.$/, '')}. Your subtitles are unchanged. (Tap ? → Copy log)`;
    L.error('Claude fix failed: ' + e.message);
  } finally {
    fixing = false; btn.disabled = false; busy = prevBusy === 'claude' ? null : prevBusy;
  }
}
$('fixClaude').onclick = runClaudeFix;
$('undoClaude').onclick = () => {
  if (!S.undo) return;
  Object.assign(S, { words: S.undo.words, script: S.undo.script });
  S.captions = S.undo.captions; S.undo = null;
  syncStyle(); syncSubs(); dirty();
  $('undoClaude').classList.add('hidden');
  $('genStatus').textContent = 'Claude’s changes were undone.';
  L.info('Claude fix undone');
};
syncClaude();

// ---------- YouTube: thumbnail, titles, description, hashtags ----------
const newPub = () => ({ t: null, l1: '', l2: '', top: false, fmt: 'wide', titles: [], title: '', shortTitle: '', description: '', hashtags: '', ideas: [], layout: 'bottom', bg: 'blur', cut: false, scale: 0.95 });
let cutout = null, cutoutFor = null, uploaded = null;
let frames = [], thumbFrame = null;
function syncPub() {
  const p = S.pub;
  $('descLang').innerHTML = [S.primary, secondLang(), 'en'].filter((v, i, a) => v && a.indexOf(v) === i)
    .map(c => `<option value="${c}">${langName(c)}</option>`).join('');
  $('descLang').value = p.descLang || S.primary;
  $('prepClaude').classList.toggle('hidden', !C.getKey());
  $('thumbBox').classList.toggle('hidden', p.t == null);
  $('thumbL1').value = p.l1; $('thumbL2').value = p.l2; $('thumbTop').checked = p.top;
  for (const b of $('thumbFmt').children) b.setAttribute('aria-pressed', String(b.dataset.v === p.fmt));
  $('cutToggle').checked = !!p.cut; $('cutOpts').classList.toggle('hidden', !p.cut);
  $('coverScale').value = p.scale ?? 0.95;
  const bgs = $('bgSeg');
  if (!bgs.children.length) for (const [k, l] of Object.entries(P.BACKGROUNDS)) {
    const b = document.createElement('button'); b.dataset.v = k; b.textContent = l;
    b.onclick = () => { S.pub.bg = k; syncPub(); renderThumb(); dirty(); }; bgs.appendChild(b);
  }
  for (const b of bgs.children) b.setAttribute('aria-pressed', String(b.dataset.v === (p.bg || 'blur')));
  for (const b of $('layoutCover').children) b.setAttribute('aria-pressed', String(b.dataset.v === (p.layout || (p.top ? 'top' : 'bottom'))));
  $('thumbIdeas').innerHTML = '';
  const free = P.freeIdeas({ hook: S.hook, glossary: S.glossary, titles: p.titles, captions: S.captions, primary: S.primary, secondary: secondLang() });
  for (const [a, b] of [...(p.ideas || []), ...free].filter((x, i, arr) => arr.findIndex(y => y.join() === x.join()) === i).slice(0, 8)) {
    const btn = document.createElement('button'); btn.className = 'btn titleopt'; btn.textContent = `${a} / ${b}`;
    btn.onclick = () => { p.l1 = a; p.l2 = b; syncPub(); renderThumb(); dirty(); };
    $('thumbIdeas').appendChild(btn);
  }
  const has = p.titles.length || p.description;
  $('textBox').classList.toggle('hidden', !has);
  $('titleList').innerHTML = '';
  for (const t of p.titles) {
    const btn = document.createElement('button'); btn.className = 'btn titleopt'; btn.textContent = t;
    btn.setAttribute('aria-pressed', String(t === p.title));
    btn.onclick = () => { p.title = t; syncPub(); dirty(); };
    $('titleList').appendChild(btn);
  }
  $('shortTitle').value = p.shortTitle; $('desc').value = p.description; $('tags').value = p.hashtags;
}
function showFrames() {
  const grid = $('frameGrid'); grid.innerHTML = '';
  for (const f of frames) {
    const c = f.canvas; c.setAttribute('aria-pressed', String(Math.abs((S.pub.t ?? -9) - f.t) < 0.05));
    c.title = fmt(toOutput(f.t)); c.onclick = () => selectFrame(f.t);
    grid.appendChild(c);
  }
}
async function selectFrame(t) {
  uploaded = null;
  S.pub.t = t; showFrames(); syncPub(); dirty();
  $('ytStatus').textContent = 'Loading the frame…';
  thumbFrame = await P.grabFrame(S.file, t).catch(() => null);
  $('ytStatus').textContent = thumbFrame ? '' : 'Could not read that frame.';
  renderThumb();
}
async function ensureCutout(src) {
  if (!S.pub.cut || !src) return null;
  if (cutoutFor === src && cutout) return cutout;
  const st = $('ytStatus'); st.className = 'status';
  try {
    cutout = await P.removeBackground(src, (m) => { st.textContent = m; });
    cutoutFor = src; st.textContent = '';
    L.info('Background removed', { w: src.width, h: src.height });
  } catch (e) {
    st.className = 'status error'; st.textContent = `Background removal failed: ${e.message}`; L.error('Background removal failed: ' + e.message);
    cutout = null;
  }
  return cutout;
}
async function renderThumb() {
  const src = uploaded || thumbFrame;
  if (!src) return;
  const p = S.pub, cv = $('thumbCanvas');
  const [W, H] = p.fmt === 'tall' ? [1080, 1920] : [1280, 720];
  const cut = await ensureCutout(src);
  cv.width = W; cv.height = H;
  const up = (t, l) => { try { return t.toLocaleUpperCase(l); } catch { return t.toUpperCase(); } };
  P.drawCover(cv.getContext('2d'), W, H, {
    photo: src, cutout: p.cut ? cut : null, bg: p.bg || 'blur', layout: p.layout || (p.top ? 'top' : 'bottom'), scale: p.scale ?? 0.95,
    // Capitals: Turkish rules (i→İ) only for lines that are clearly Turkish.
    lines: [{ text: up(p.l1 || '', /[ğışçİĞŞÇ]/.test(p.l1 || '') ? 'tr' : 'de') }, { text: up(p.l2 || '', /[ğışçİĞŞÇ]/.test(p.l2 || '') ? 'tr' : 'de') }],
    font: S.font, colorA: COLORS[S.color2], colorB: COLORS[S.color1], langA: COLORS[S.color2], langB: COLORS[S.color1] === '#ffffff' ? '#4de6ff' : COLORS[S.color1],
  });
  $('thumbBox').classList.remove('hidden');
  cv.style.maxWidth = p.fmt === 'tall' ? '240px' : '100%'; cv.style.display = 'block'; cv.style.margin = '10px auto 0';
  cv.toBlob(b => {
    if (!b) return;
    const a = $('thumbDl'); if (a.href) URL.revokeObjectURL(a.href);
    a.href = URL.createObjectURL(b); a.download = `cover-${p.fmt === 'tall' ? 'short' : 'video'}.png`;
    S.thumbBlob = b;
  }, 'image/png');
}
$('cutToggle').onchange = (e) => { S.pub.cut = e.target.checked; if (S.pub.cut && (S.pub.bg || 'blur') === 'photo') S.pub.bg = 'blur'; syncPub(); renderThumb(); dirty(); };
$('coverScale').oninput = (e) => { S.pub.scale = +e.target.value; renderThumb(); dirty(); };
for (const b of $('layoutCover').children) b.onclick = () => { S.pub.layout = b.dataset.v; syncPub(); renderThumb(); dirty(); };
$('useCurrent').onclick = async () => { uploaded = null; await selectFrame(video.currentTime); };
$('coverUpload').addEventListener('change', async (e) => {
  const f = e.target.files[0]; e.target.value = ''; if (!f) return;
  const img = await createImageBitmap(f).catch(() => null);
  if (!img) { $('ytStatus').textContent = 'Could not open that picture.'; return; }
  const c = document.createElement('canvas'); const k = Math.min(1, 1920 / Math.max(img.width, img.height));
  c.width = Math.round(img.width * k); c.height = Math.round(img.height * k); c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
  uploaded = c; S.pub.t = S.pub.t ?? -1; $('thumbBox').classList.remove('hidden'); L.info('Cover photo uploaded', { w: c.width, h: c.height });
  renderThumb();
});
async function loadFrames() {
  $('ytStatus').className = 'status'; $('ytStatus').textContent = 'Looking for sharp, well-lit frames…';
  $('findFrames').disabled = true;
  try {
    frames = await P.findFrames(S.file, kept());
    showFrames();
    $('ytStatus').textContent = 'Tap a frame to make the thumbnail.';
    L.info('Thumbnail frames found', { count: frames.length });
    if (S.pub.t == null && frames.length) await selectFrame(frames[0].t);
  } catch (e) {
    $('ytStatus').className = 'status error'; $('ytStatus').textContent = `Could not read frames: ${e.message}`;
    L.error('Find frames failed: ' + e.message);
  } finally { $('findFrames').disabled = false; }
}
$('findFrames').onclick = loadFrames;
$('thumbL1').oninput = (e) => { S.pub.l1 = e.target.value; renderThumb(); dirty(); };
$('thumbL2').oninput = (e) => { S.pub.l2 = e.target.value; renderThumb(); dirty(); };

for (const b of $('thumbFmt').children) b.onclick = () => { S.pub.fmt = b.dataset.v; syncPub(); renderThumb(); dirty(); };
$('thumbShare').onclick = () => S.thumbBlob && share(S.thumbBlob, $('thumbDl').download, 'image/png');
$('descLang').onchange = (e) => { S.pub.descLang = e.target.value; dirty(); };
$('shortTitle').oninput = (e) => { S.pub.shortTitle = e.target.value; dirty(); };
$('desc').oninput = (e) => { S.pub.description = e.target.value; dirty(); };
$('tags').oninput = (e) => { S.pub.hashtags = e.target.value; dirty(); };
$('copyTitle').onclick = () => copy(S.pub.title || S.pub.titles[0] || '', $('copyTitle'));
$('copyDesc').onclick = () => copy(`${S.pub.description}\n\n${S.pub.hashtags}`.trim(), $('copyDesc'));
$('prepClaude').onclick = async () => {
  if (!S.captions.length) { $('ytStatus').textContent = 'Make the subtitles first — Claude writes from them.'; return; }
  const btn = $('prepClaude'), st = $('ytStatus');
  btn.disabled = true;
  try {
    if (!frames.length) await loadFrames();
    st.className = 'status'; st.textContent = 'Claude is looking at the frames and writing…';
    const t0 = Date.now();
    const descLang = langName(S.pub.descLang || S.primary);
    const r = await P.prepareWithClaude((content, max) => C.call(C.getKey(), S.claudeModel, '', content, max), {
      frames, transcript: transcriptText(), descLang, glossary: S.glossary,
      langs: activeLangs().map(langName).join(' and '),
    });
    const p = S.pub;
    p.titles = r.titles || []; p.title = p.titles[0] || '';
    p.shortTitle = r.shortTitle || ''; p.description = r.description || '';
    p.hashtags = (r.hashtags || []).map(h => '#' + String(h).replace(/^#/, '').replace(/\s+/g, '')).join(' ');
    p.ideas = (r.thumbnailTexts || []).filter(x => Array.isArray(x));
    if (p.ideas[0]) { [p.l1, p.l2] = p.ideas[0]; }
    const best = frames[(r.bestFrame || 1) - 1];
    syncPub(); dirty();
    if (best) await selectFrame(best.t); else renderThumb();
    st.textContent = `Ready. Claude picked frame ${r.bestFrame}${r.frameReason ? ` — ${r.frameReason}` : ''}.`;
    L.info('Claude YouTube prep done', { secs: Math.round((Date.now() - t0) / 1000), titles: p.titles.length, bestFrame: r.bestFrame });
  } catch (e) {
    st.className = 'status error'; st.textContent = `Claude couldn't prepare it: ${String(e.message).replace(/\.$/, '')}. (Tap ? → Copy log)`;
    L.error('Claude YouTube prep failed: ' + e.message);
  } finally { btn.disabled = false; }
};

// ---------- Help & diagnostics ----------
async function openHelp() {
  const t = await L.text();
  $('logView').textContent = t;
  const n = L.errorCount();
  $('logSummary').textContent = n ? `${n} error${n > 1 ? 's' : ''} recorded.` : 'No errors recorded.';
  $('helpDlg').showModal();
  history.pushState({ view: 'help' }, '');
  $('logView').scrollTop = $('logView').scrollHeight;
}
$('helpBtn').onclick = openHelp;
$('closeHelp').onclick = () => $('helpDlg').close();
$('helpDlg').addEventListener('close', () => { if (history.state?.view === 'help') { ignorePop = true; history.back(); } });
$('copyLog').onclick = async () => copy(await L.text(), $('copyLog'));
$('clearLog').onclick = async () => { L.clear(); L.info('Log cleared'); openHelp(); };
$('reportLog').onclick = async () => {
  const t = await L.text();
  // Keep the link short enough for browsers: the end of the log matters most.
  const body = 'What happened:\n\n\n```\n' + (t.length > 5500 ? t.slice(0, 900) + '\n…\n' + t.slice(-4500) : t) + '\n```';
  open(`https://github.com/boyukbas/YT/issues/new?title=${encodeURIComponent('Problem in Shorts Editor')}&body=${encodeURIComponent(body)}`, '_blank');
};

// ---------- Background & presenter (layers) ----------
const comp = $('composite'), bgVideo = $('bgVideo');
let bgImage = null, bgIsVideo = false, bgURL = null, masker = null, maskAt = 0, lastMask = null;
const BG_COLORS = ['#1e7a46', '#f4f6f8', '#2b2f3a', '#0b3d91', '#7a1e3a', '#00b140'];
function Lr() { return S.layers; }
function syncLayers() {
  const L = Lr();
  for (const b of $('bgModeSeg').children) b.setAttribute('aria-pressed', String(b.dataset.v === L.bg));
  $('bgColors').classList.toggle('hidden', L.bg !== 'color');
  $('bgGrads').classList.toggle('hidden', L.bg !== 'grad');
  $('bgFileRow').classList.toggle('hidden', L.bg !== 'file');
  const sw = $('bgColors');
  if (!sw.children.length) for (const c of BG_COLORS) { const b = document.createElement('button'); b.className = 'swatch'; b.style.background = c; b.dataset.v = c; b.setAttribute('aria-label', c); b.onclick = () => { S.layers.color = c; syncLayers(); dirty(); }; sw.appendChild(b); }
  for (const b of sw.children) b.setAttribute('aria-pressed', String(b.dataset.v === L.color));
  const gs = $('bgGrads');
  if (!gs.children.length) for (const k of Object.keys(LY.GRADIENTS)) { const b = document.createElement('button'); b.dataset.v = k; b.textContent = k[0].toUpperCase() + k.slice(1); b.onclick = () => { S.layers.grad = k; syncLayers(); dirty(); }; gs.appendChild(b); }
  for (const b of gs.children) b.setAttribute('aria-pressed', String(b.dataset.v === L.grad));
  $('bgFileInfo').textContent = L.hasFile ? `Using your ${L.fileIsVideo ? 'video (it loops; its sound is not used)' : 'picture'}.` : 'No picture or video chosen yet.';
  $('cutMe').checked = L.cut;
  $('presOpts').classList.toggle('hidden', L.bg === 'none' && !L.cut);
  $('presSize').value = L.size; $('presSizeOut').textContent = L.size >= 0.999 ? 'Full' : `${Math.round(L.size * 100)}%`;
  $('presShape').classList.toggle('hidden', L.cut || L.size >= 0.999);
  for (const b of $('presShape').children) b.setAttribute('aria-pressed', String(b.dataset.v === L.shape));
  for (const b of $('presPos').children) b.setAttribute('aria-pressed', String(b.dataset.v === 'drag' ? !!S.presDrag : false));
  const active = LY.layersActive(L);
  comp.classList.toggle('hidden', !active);
  video.controls = !active;   // the picture is drawn on top; play/pause with a tap or the timeline buttons
  if (L.cut) ensureSegmenter();
}
async function ensureSegmenter() {
  const st = $('layersStatus');
  if (masker) return true;
  st.className = 'status'; st.textContent = 'Loading the person detector (about 13 MB, one time)…';
  try { await LY.loadSegmenter(S.width >= S.height); masker = new LY.Masker(); st.textContent = ''; L.info('Person detector ready'); return true; }
  catch (e) { st.className = 'status error'; st.textContent = `Could not load the person detector: ${e.message}`; L.error('Segmenter failed: ' + e.message); return false; }
}
async function setBgFile(file, save = true) {
  if (bgURL) URL.revokeObjectURL(bgURL);
  bgImage = null; bgIsVideo = file.type.startsWith('video/');
  bgURL = URL.createObjectURL(file);
  if (bgIsVideo) { bgVideo.src = bgURL; bgVideo.load(); }
  else bgImage = await createImageBitmap(file);
  S.bgFile = file;
  S.layers.hasFile = true; S.layers.fileIsVideo = bgIsVideo;
  if (save) { try { await saveFile(S.id + ':bg', file); } catch {} dirty(); }
  syncLayers();
}
async function loadBgForProject() {
  bgImage = null; bgIsVideo = false; S.bgFile = null; bgVideo.removeAttribute('src');
  if (S.layers.hasFile) { const f = await loadFile(S.id + ':bg').catch(() => null); if (f) await setBgFile(f, false); else S.layers.hasFile = false; }
  syncLayers();
}
for (const b of $('bgModeSeg').children) b.onclick = () => { S.layers.bg = b.dataset.v; if (b.dataset.v === 'file' && !S.layers.hasFile) $('bgFile').click(); syncLayers(); dirty(); };
$('bgFile').addEventListener('change', async (e) => { const f = e.target.files[0]; e.target.value = ''; if (!f) return; await setBgFile(f); S.layers.bg = 'file'; syncLayers(); L.info('Background chosen', { type: f.type, sizeMB: +(f.size / 1e6).toFixed(1) }); });
$('cutMe').onchange = (e) => { S.layers.cut = e.target.checked; if (S.layers.cut && S.layers.bg === 'none') S.layers.bg = 'blur'; syncLayers(); dirty(); };
$('presSize').oninput = (e) => { S.layers.size = +e.target.value; syncLayers(); dirty(); };
for (const b of $('presShape').children) b.onclick = () => { S.layers.shape = b.dataset.v; syncLayers(); dirty(); };
const POS = { full: [0.5, 0.5, 1], bl: [0.2, 0.75, 0.45], br: [0.8, 0.75, 0.45], tr: [0.8, 0.25, 0.4], c: [0.5, 0.55, 0.7] };
for (const b of $('presPos').children) b.onclick = () => {
  if (b.dataset.v === 'drag') { S.presDrag = !S.presDrag; $('stage').classList.toggle('dragging', S.presDrag); if (S.presDrag) { video.pause(); $('stage').scrollIntoView({ block: 'start' }); } syncLayers(); return; }
  const [x, y0, size] = POS[b.dataset.v];
  const y = b.dataset.v === 'bl' || b.dataset.v === 'br' ? 1 - size / 2 : y0;   // stand on the bottom edge
  Object.assign(S.layers, { x, y, size }); S.presDrag = false; $('stage').classList.remove('dragging'); syncLayers(); dirty();
};
$('stage').addEventListener('click', (e) => {
  if (!LY.layersActive(S.layers) || S.presDrag || S.dragMode || e.target.closest('button')) return;
  if (video.paused) video.play(); else video.pause();
});

/** Live preview of the layers (runs every animation frame while they're on). */
function drawComposite() {
  const L = S.layers;
  if (!LY.layersActive(L) || !video.videoWidth) return;
  const dpr = Math.min(2, devicePixelRatio || 1), W = Math.round(comp.clientWidth * dpr), H = Math.round(comp.clientHeight * dpr);
  if (!W || !H) return;
  if (comp.width !== W || comp.height !== H) { comp.width = W; comp.height = H; }
  if (L.cut && masker && busy !== 'export' && performance.now() - maskAt > 45) { lastMask = masker.mask(video); maskAt = performance.now(); }
  let bg = null;
  if (L.bg === 'file') {
    if (bgIsVideo && bgVideo.readyState >= 2) {
      const target = toOutput(video.currentTime) % (bgVideo.duration || 1);
      if (Math.abs(bgVideo.currentTime - target) > 0.35) bgVideo.currentTime = target;
      if (!video.paused && bgVideo.paused) bgVideo.play().catch(() => {});
      if (video.paused && !bgVideo.paused) bgVideo.pause();
      bg = bgVideo;
    } else bg = bgImage;
  }
  LY.composite(comp.getContext('2d'), W, H, video, video.videoWidth, video.videoHeight, L.cut ? lastMask : null, bg, L);
}
// ---------- Tabs, settings, project thumbnails ----------
function setTab(name) {
  for (const b of $('tabs').children) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  for (const p of document.querySelectorAll('.panel')) p.classList.toggle('active', p.dataset.panel === name);
  if (name === 'share') drawFormatPreview(true);
}
for (const b of $('tabs').children) b.onclick = () => { setTab(b.dataset.tab); scrollTo({ top: 0 }); };

function syncEngineNote() {
  const eng = S.engine === 'openai' ? 'OpenAI (cloud)' : `on this device (${{ normal: 'Fast', second: 'Better 2nd language', all: 'Best' }[S.accuracy]})`;
  $('engineNote').textContent = `Speech recognition: ${eng}. Claude: ${C.getKey() ? 'on' : 'off'}. Change these in ⚙︎ Settings.`;
}
$('settingsBtn').onclick = () => { $('settingsDlg').showModal(); history.pushState({ view: 'settings' }, ''); };
$('closeSettings').onclick = () => $('settingsDlg').close();
$('settingsDlg').addEventListener('close', () => {
  syncEngineNote(); syncGenButton(); syncPub(); syncClaude();
  if (history.state?.view === 'settings') { ignorePop = true; history.back(); }
});
syncEngineNote();

/** A small square picture of the video for the projects list. */
function captureProjectThumb() {
  const v = $('video');
  const grab = () => {
    try {
      const c = document.createElement('canvas'); c.width = c.height = 128;
      const s = Math.min(v.videoWidth, v.videoHeight);
      c.getContext('2d').drawImage(v, (v.videoWidth - s) / 2, (v.videoHeight - s) / 2, s, s, 0, 0, 128, 128);
      S.thumb = c.toDataURL('image/jpeg', 0.7); dirty();
    } catch {}
    v.currentTime = S.trimStart || 0;
  };
  v.addEventListener('loadeddata', () => {
    v.addEventListener('seeked', grab, { once: true });
    v.currentTime = Math.min(1, (v.duration || 2) / 4);
  }, { once: true });
}

// ---------- Timeline: waveform, cuts and playhead; drag to scrub, zoom for precision ----------
let zoom = 1;
const tl = () => $('timeline');
function tlWindow() {
  const span = S.duration / zoom;
  let v0 = Math.max(0, Math.min(S.duration - span, video.currentTime - span / 2));
  return [v0, v0 + span];
}
function drawTimeline() {
  const cv = tl(); if (!cv || !S.duration || cv.offsetParent === null) return;
  const dpr = Math.min(2, devicePixelRatio || 1), W = Math.round(cv.clientWidth * dpr), H = Math.round(cv.clientHeight * dpr);
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const ctx = cv.getContext('2d'), [v0, v1] = tlWindow(), x = (t) => (t - v0) / (v1 - v0) * W;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0f131c'; ctx.fillRect(0, 0, W, H);
  // Waveform from the loudness levels.
  ctx.fillStyle = '#5b6680';
  for (let px = 0; px < W; px += 2 * dpr) {
    const a = Math.floor((v0 + px / W * (v1 - v0)) / HOP), b = Math.max(a + 1, Math.floor((v0 + (px + 2 * dpr) / W * (v1 - v0)) / HOP));
    let m = -90; for (let i = a; i < b && i < S.levels.length; i++) m = Math.max(m, S.levels[i]);
    const h = Math.max(1, (m + 60) / 60) * H * 0.8;
    ctx.fillRect(px, (H - h) / 2, dpr, h);
  }
  const shade = (s, e, col) => { if (e <= v0 || s >= v1) return; ctx.fillStyle = col; ctx.fillRect(x(Math.max(s, v0)), 0, x(Math.min(e, v1)) - x(Math.max(s, v0)), H); };
  shade(0, S.trimStart, 'rgba(0,0,0,0.65)'); shade(S.trimEnd, S.duration, 'rgba(0,0,0,0.65)');
  if (S.removePauses) for (const p of S.silences) shade(p.start, p.end, S.pauseMode > 1 ? 'rgba(77,170,255,0.22)' : 'rgba(160,170,190,0.18)');
  for (const c of S.cuts) shade(c.start, c.end, c.speed > 1 ? 'rgba(77,170,255,0.4)' : 'rgba(255,90,90,0.45)');
  if (S.cutFrom != null) { ctx.fillStyle = '#ff7a6b'; ctx.fillRect(x(S.cutFrom) - dpr, 0, 2 * dpr, H); }
  ctx.fillStyle = '#ffd21a'; ctx.fillRect(x(video.currentTime) - dpr, 0, 2 * dpr, H);
  $('tlRange').textContent = `${fmt(v0)} – ${fmt(v1)}`;
}
{
  let down = null;
  const seekAt = (e) => { const r = tl().getBoundingClientRect(), [v0, v1] = tlWindow(); video.currentTime = Math.max(0, Math.min(S.duration, v0 + (e.clientX - r.left) / r.width * (v1 - v0))); };
  tl().addEventListener('pointerdown', (e) => { down = { x: e.clientX, y: e.clientY, t: video.currentTime, moved: false }; video.pause(); });
  tl().addEventListener('pointermove', (e) => {
    if (!down) return;
    const dx = e.clientX - down.x;
    if (!down.moved && Math.abs(dx) < 6) return;
    down.moved = true;
    // Dragging moves the film under the playhead (like phone video editors), scaled by zoom.
    const r = tl().getBoundingClientRect(), [v0, v1] = [0, S.duration / zoom];
    video.currentTime = Math.max(0, Math.min(S.duration, down.t - dx / r.width * (v1 - v0)));
  });
  const up = (e) => { if (down && !down.moved && e.type === 'pointerup') seekAt(e); down = null; };
  tl().addEventListener('pointerup', up); tl().addEventListener('pointercancel', up);
}
for (const b of $('zoomSeg').children) b.onclick = () => { zoom = +b.dataset.v; for (const x of $('zoomSeg').children) x.setAttribute('aria-pressed', String(x === b)); };
for (const b of document.querySelectorAll('[data-step]')) b.onclick = () => { video.pause(); video.currentTime = Math.max(0, Math.min(S.duration, video.currentTime + +b.dataset.step)); };
$('playPause').onclick = () => { if (video.paused) video.play(); else video.pause(); };

// ---------- Combine several videos ----------
let clips = [], merging = false;
function renderClips() {
  const list = $('clipList'); list.innerHTML = '';
  clips.forEach((c, i) => {
    const row = document.createElement('div'); row.className = 'clip';
    if (c.thumb) { c.thumb.style.width = '56px'; c.thumb.style.height = '56px'; c.thumb.style.objectFit = 'cover'; row.appendChild(c.thumb); }
    const name = document.createElement('div'); name.className = 'name';
    const b = document.createElement('b'); b.textContent = `${i + 1}. ${c.name}`;
    const m = document.createElement('span'); m.className = 'muted'; m.textContent = `${fmt(c.duration)} · ${c.width}×${c.height}${c.hasAudio ? '' : ' · no sound'}`;
    name.append(b, m); row.appendChild(name);
    const btn = (txt, label, fn, off) => { const x = document.createElement('button'); x.textContent = txt; x.setAttribute('aria-label', label); x.disabled = off || merging; x.onclick = fn; row.appendChild(x); };
    btn('↑', 'Move up', () => { [clips[i - 1], clips[i]] = [clips[i], clips[i - 1]]; renderClips(); }, i === 0);
    btn('↓', 'Move down', () => { [clips[i + 1], clips[i]] = [clips[i], clips[i + 1]]; renderClips(); }, i === clips.length - 1);
    btn('×', 'Remove', () => { clips.splice(i, 1); renderClips(); });
    list.appendChild(row);
  });
  const total = clips.reduce((a, c) => a + c.duration, 0);
  const shapes = new Set(clips.map(c => (c.width / c.height).toFixed(2)));
  $('mergeInfo').textContent = clips.length
    ? `${clips.length} videos · ${fmt(total)} in total.` + (shapes.size > 1 ? ' Different shapes: they will match the first video, with a blurred background where needed.' : '')
    : 'Add at least two videos.';
  $('mergeGo').disabled = merging || clips.length < 2;
}
async function addClips(files) {
  $('mergeStatus').className = 'status'; $('mergeStatus').textContent = 'Reading the videos…';
  for (const f of files) {
    try { clips.push(await M.probe(f)); renderClips(); }
    catch (e) { $('mergeStatus').className = 'status error'; $('mergeStatus').textContent = e.message; L.error('Combine: ' + e.message); }
  }
  if ($('mergeStatus').className === 'status') $('mergeStatus').textContent = '';
}
$('mergeInput').addEventListener('change', async (e) => {
  const files = [...e.target.files]; e.target.value = '';
  if (!files.length) return;
  clips = []; renderClips();
  $('mergeDlg').showModal(); history.pushState({ view: 'merge' }, '');
  await addClips(files);
});
$('mergeMore').addEventListener('change', async (e) => { const files = [...e.target.files]; e.target.value = ''; await addClips(files); });
$('mergeCancel').onclick = () => { if (!merging) $('mergeDlg').close(); };
$('mergeDlg').addEventListener('cancel', (e) => { if (merging) e.preventDefault(); });
$('mergeDlg').addEventListener('close', () => { if (history.state?.view === 'merge') { ignorePop = true; history.back(); } });
$('mergeGo').onclick = async () => {
  if (clips.length < 2) return;
  merging = true; renderClips(); keepAwake(true);
  const st = $('mergeStatus'), t0 = Date.now();
  st.className = 'status'; st.textContent = 'Combining…'; setBar('mergeBar', 0);
  try {
    const file = await M.mergeClips(clips, {
      onProgress: (f) => { st.textContent = `Combining… ${Math.round(f * 100)}%`; setBar('mergeBar', f); },
      log: (d) => L.info('Combine started', d),
    });
    L.info('Combine done', { secs: Math.round((Date.now() - t0) / 1000), sizeMB: +(file.size / 1e6).toFixed(1) });
    merging = false; $('mergeDlg').close(); setBar('mergeBar', null); st.textContent = '';
    await openVideo(file);
    S.name = `Combined ${clips.length} videos · ${new Date().toLocaleDateString()}`; dirty();
  } catch (e) {
    st.className = 'status error'; st.textContent = `Combining failed: ${String(e.message).replace(/\.$/, '')}. (Tap ? → Copy log)`;
    L.error('Combine failed: ' + e.message);
  } finally { merging = false; keepAwake(false); setBar('mergeBar', null); renderClips(); }
};

// ---------- Sliders: don't change them by accident while scrolling ----------
// A swipe that starts on a slider used to move it. Now: if the browser takes the touch for
// scrolling (pointercancel) or the finger moves mostly up/down, the slider snaps back.
for (const r of document.querySelectorAll('input[type=range]')) {
  let start = null, sx = 0, sy = 0, vertical = false;
  const restore = () => {
    if (start !== null && r.value !== start) { r.value = start; r.dispatchEvent(new Event('input', { bubbles: true })); }
    start = null;
  };
  r.addEventListener('pointerdown', (e) => { start = r.value; sx = e.clientX; sy = e.clientY; vertical = false; }, true);
  r.addEventListener('pointermove', (e) => {
    if (start !== null && Math.abs(e.clientY - sy) > Math.abs(e.clientX - sx) + 8) vertical = true;
  });
  r.addEventListener('pointercancel', restore);
  r.addEventListener('pointerup', () => { if (vertical) restore(); else start = null; });
}

window.__editor = S; // handy for debugging in the console
fillLangSelects();
for (const k of Object.keys(FONTS)) document.fonts.load(fontCss(k, 40)).catch(() => {});
showProjects();
