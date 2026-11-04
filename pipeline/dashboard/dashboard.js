// The private dashboard (pipeline/dashboard.py serves this behind Cloudflare Access).
const STATES = {
  public: 'Public', scheduled: 'Scheduled', uploaded: 'Uploaded (private)', ready: 'Ready to upload',
  making: 'Being made', new: 'New recording', waiting: 'Waiting', stopped: 'Stopped',
};
const FILTERS = [['all', 'All'], ['public', 'Public'], ['coming', 'Coming up'], ['short', 'Shorts'], ['video', 'Videos']];
let data = null, filter = 'all', busy = false;
try { filter = localStorage.getItem('dashFilter') || 'all'; } catch (e) {}

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const num = (n) => new Intl.NumberFormat('en').format(n);
const len = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
const date = (t) => new Date(t);
const dayName = (t) => date(t).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
const clock = (t) => date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
const when = (t) => date(t).toLocaleString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const ago = (t) => {
  const m = Math.round((Date.now() - date(t)) / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} days ago`;
};

async function action(body, msgEl, confirmText) {
  if (busy || (confirmText && !confirm(confirmText))) return;
  busy = true;
  document.querySelectorAll('button[data-act]').forEach((b) => { b.disabled = true; });
  msgEl.textContent = 'Working…';
  try {
    const r = await fetch('/api/action', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Dashboard': '1' }, body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 428 && j.confirm) {               // breaks the one-a-day rule: ask, then send again with force
      busy = false;
      msgEl.textContent = '';
      if (confirm(`${j.confirm}\n\nPost anyway?`)) return action({ ...body, force: true }, msgEl);
      msgEl.textContent = 'Not posted.';
      await load();
      return;
    }
    msgEl.textContent = j.message || j.error || `Error ${r.status}`;
  } catch (e) {
    msgEl.textContent = 'Could not reach the PC. Is it on?';
  }
  busy = false;
  await load();
}

function pick(v) {
  if (filter === 'public') return v.state === 'public';
  if (filter === 'coming') return v.state !== 'public';
  if (filter === 'short' || filter === 'video') return v.kind === filter;
  return true;
}

function card(v) {
  const c = el('div', 'card');
  const th = el('div', 'thumb');
  if (v.youtube) th.style.backgroundImage = `url(https://i.ytimg.com/vi/${v.youtube}/mqdefault.jpg)`;
  else th.textContent = v.kind === 'lesson' ? (v.state === 'stopped' ? '⚠' : '⏳') : (v.kind === 'short' ? 'Short' : 'Video');
  const body = el('div');
  const top = el('div', 'top');
  top.append(el('span', `chip st-${v.state}`, STATES[v.state] || v.state));
  if (v.kind !== 'lesson') top.append(el('span', 'chip', v.kind === 'short' ? `Short${v.part ? ' ' + v.part : ''}` : 'Video'));
  if (v.privacy && v.privacy !== 'public') top.append(el('span', 'chip', v.privacy));
  if (v.time) top.append(el('span', 'when', (v.state === 'scheduled' ? 'goes public ' : '') + clock(v.time)));
  body.append(top);
  const t = el('div', 'title');
  const text = v.title || v.lesson;
  if (v.youtube) {
    const a = el('a', null, text); a.href = `https://youtu.be/${v.youtube}`; a.target = '_blank'; a.rel = 'noopener';
    t.append(a);
  } else t.textContent = text;
  body.append(t);
  const meta = el('div', 'meta');
  meta.append(el('span', null, v.lesson));
  if (v.step && v.kind === 'lesson') meta.append(el('span', null, v.step));
  if (v.clips) meta.append(el('span', null, `${v.clips} clip${v.clips === 1 ? '' : 's'}`));
  if (v.seconds) meta.append(el('span', null, len(v.seconds)));
  const count = (n, word) => { const s = el('span'); s.append(el('b', null, num(n)), ` ${word}${n === 1 ? '' : 's'}`); return s; };
  if (v.views != null) {
    meta.append(count(v.views, 'view'));
    if (v.likes) meta.append(count(v.likes, 'like'));
    if (v.comments) meta.append(count(v.comments, 'comment'));
  }
  if (v.instagram && v.instagram.url) {
    const a = el('a', null, 'Instagram Reel'); a.href = v.instagram.url; a.target = '_blank'; a.rel = 'noopener';
    meta.append(a);
  }
  body.append(meta);
  if (v.error) body.append(el('div', 'err', v.error));
  if (v.state === 'stopped') {
    const msg = el('div', 'msg');
    const b = el('button', 'danger', v.retry ? 'Retry asked: waiting for the PC' : 'Retry this lesson');
    b.dataset.act = '1';
    b.disabled = !!v.retry;
    b.onclick = () => action({ action: 'retry', lesson: v.lesson }, msg, `Run "${v.lesson}" again from the start?`);
    body.append(b, msg);
  }
  c.append(th, body);
  return c;
}

function renderInstagram(ig, job) {
  $('ig').hidden = !ig.set_up;
  if (!ig.set_up) return;
  const info = $('igInfo'); info.replaceChildren();
  const line = (text, cls) => info.append(el('p', cls, text));
  line(ig.paused ? 'Daily Reels: PAUSED' : `Daily Reels: on, one a day after ${ig.daily_time}`);
  line(ig.next ? `Next: ${ig.next.lesson} · ${ig.next.file}` : 'Next: nothing left to post', 'muted');
  if (ig.last) line(`Last Reel: ${when(ig.last)} (${ago(ig.last)})`, 'muted');
  const wait = ig.allowed_from && date(ig.allowed_from) > Date.now();
  if (wait) line(`Next one possible from ${when(ig.allowed_from)} (one a day)`, 'muted');
  const posting = ig.posting || (job && job.running);
  const now = $('reelNow');
  now.dataset.act = '1';
  now.disabled = busy || posting || !ig.next;
  now.textContent = posting ? 'Posting…' : (wait ? 'Post next Reel now (early)' : 'Post next Reel now');
  now.onclick = () => action({ action: 'reel-now' }, $('igMsg'),
    `Post this Reel on Instagram now?\n\n${ig.next.lesson} · ${ig.next.file}`);
  const p = $('pause');
  p.dataset.act = '1';
  p.disabled = busy;
  p.textContent = ig.paused ? 'Resume daily Reels' : 'Pause daily Reels';
  p.onclick = () => action({ action: ig.paused ? 'resume' : 'pause' }, $('igMsg'));
  if (job && job.result && !busy) $('igMsg').textContent = `${job.running ? '' : when(job.at) + ': '}${job.result}`;
}

function render() {
  const vs = data.videos || [];
  const ch = data.channel || {};
  $('name').textContent = ch.name || 'Channel';
  $('me').textContent = data.me || '';
  $('updated').textContent = `Updated ${ago(data.generated)}` + (data.counts_at ? ` · views from ${clock(data.counts_at)}` : '');
  const pub = vs.filter((v) => v.state === 'public');
  const stats = [
    [ch.subscribers, 'subscribers'],
    [pub.reduce((a, v) => a + (v.views || 0), 0), 'views on these videos'],
    [pub.length, 'public'],
    [vs.filter((v) => v.state !== 'public').length, 'coming up'],
  ];
  const s = $('stats'); s.replaceChildren();
  for (const [n, label] of stats) if (n != null) { const d = el('div', 'stat'); d.append(el('b', null, num(n)), el('span', null, label)); s.append(d); }
  renderInstagram(data.instagram || {}, (data.jobs || {}).reel);
  const f = $('filters'); f.replaceChildren();
  for (const [k, label] of FILTERS) {
    const b = el('button', null, label); b.setAttribute('aria-pressed', String(k === filter));
    b.onclick = () => { filter = k; try { localStorage.setItem('dashFilter', k); } catch (e) {} render(); };
    f.append(b);
  }
  const list = $('list'); list.replaceChildren();
  const shown = vs.filter(pick);
  if (!shown.length) { list.append(el('div', 'empty', 'Nothing here yet.')); return; }
  let day = '';
  for (const v of shown) {
    const d = v.time ? dayName(v.time) : 'No date';
    if (d !== day) { day = d; list.append(el('div', 'day', d)); }
    list.append(card(v));
  }
}

async function load() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store', credentials: 'same-origin' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `Error ${r.status}`);
    data = j;
    render();
  } catch (e) {
    const text = String(e.message || e).includes('fetch') ? 'Could not reach the PC. Is it on?' : String(e.message || e);
    if (!data) $('list').replaceChildren(el('div', 'error', text));
    else $('updated').textContent = text;
  }
}
load();
setInterval(() => { if (!busy) load(); }, 60 * 1000);
setInterval(() => { if (data && (data.jobs || {}).reel?.running && !busy) load(); }, 10 * 1000);
