// Dashboard v2 shell (F1): router, fetch, sheets, toasts, times, polling. A tab module calls onRoute(name, render);
// render(view, parts, why) fills its <section> (why: 'open', 'poll' or 'retry'). api() throws ApiError.
const TABS = ['home', 'publish', 'videos', 'stats', 'activity'];
const LOAD = { home: () => import('/home.js'), publish: () => import('/publish.js'), videos: () => import('/videos.js'),
  stats: () => import('/stats.js'), activity: () => import('/activity.js') };
const NET = 'Could not reach the PC. Is it on?';
const $ = (id) => document.getElementById(id);
const S = { tab: null, navs: 0, homeAt: 0, job: null, jobAt: 0, jobFails: 0, reds: 0, failed: false, section: null, global: false };
const routes = {}, mods = {}, views = {}, polls = new Set(), stack = [];

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function icon(name, cls = 'ic') {
  const ns = 'http://www.w3.org/2000/svg', s = document.createElementNS(ns, 'svg'), u = document.createElementNS(ns, 'use');
  s.setAttribute('class', cls);
  s.setAttribute('aria-hidden', 'true');
  u.setAttribute('href', `#i-${name}`);
  s.append(u);
  return s;
}

export function btn(t, c, f, i) { // btn.last: the last button pressed (publish.js's in-flight guard)
  const b = Object.assign(el('button', c, t), { type: 'button', onclick: (e) => { btn.last = b; return f(e); } });
  if (i) b.prepend(icon(i));
  return b;
}
export const https = (u) => /^https:\/\//.test(u || '');
export const ext = (t, href, c = 'btn small') => Object.assign(el('a', c, t), { href, target: '_blank', rel: 'noopener' });

export function tickBox(text) {
  const label = el('label', 'tick'), box = el('input');
  box.type = 'checkbox';
  label.append(box, el('span', null, text));
  return [label, box];
}

const NF = new Intl.NumberFormat('en');
export const num = (n) => (typeof n === 'number' && isFinite(n) ? NF.format(n) : '—');
export const many = (n, w) => `${num(n)} ${w}${n === 1 ? '' : 's'}`;
export const len = (s) => (typeof s === 'number' && isFinite(s)
  ? `${Math.floor(Math.round(s) / 60)}:${String(Math.round(s) % 60).padStart(2, '0')}` : '');

const TF = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Istanbul', weekday: 'short', day: 'numeric',
  month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
function date(t) { // ISO, epoch ms or Date; an Istanbul date 'YYYY-MM-DD' means noon there
  const d = t == null || t === '' ? NaN : new Date(/^\d{4}-\d\d-\d\d$/.test(t) ? `${t}T09:00:00Z` : t);
  return isNaN(d) ? null : d;
}
function tparts(t) {
  const d = date(t), p = {};
  if (d) for (const x of TF.formatToParts(d)) p[x.type] = x.value;
  return d && p;
}
export function fmt(t) { const p = tparts(t); return p ? `${p.weekday} ${p.day} ${p.month} ${p.hour}:${p.minute}` : ''; }
export function fmtDay(t) { const p = tparts(t); return p ? `${p.weekday} ${p.day} ${p.month}` : ''; }
const span = (m) => (m < 60 ? `${m} min` : m < 2880 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} days`);
export function ago(t) {
  const d = date(t), m = d && Math.round((Date.now() - d) / 60000);
  return !d ? '' : m < -1 ? `in ${span(-m)}` : m < 1 ? 'just now' : `${span(m)} ago`;
}

export class ApiError extends Error {
  constructor(status, json) {
    super(json.message || NET);
    Object.assign(this, { name: 'ApiError', status, json, code: json.error || null });
  }
}

async function send(path, init) {
  let status = 0, json = null;
  try {
    const r = await fetch(path, { credentials: 'same-origin', cache: 'no-store', redirect: 'manual', ...init });
    if (r.type === 'opaqueredirect') status = 401; // Access sends a new login
    else [status, json] = [r.status, await r.json().catch(() => null)];
  } catch (e) { /* not reachable */ }
  if (!json || typeof json !== 'object' || Array.isArray(json)) {
    if (status === 401 || status === 403) json = { error: 'not_signed_in', message: 'You are signed out.' }; // Access
    else if (status === 524) json = { error: 'timeout', message: 'The PC is still working on it; reload in a minute.' }; // Cloudflare's timeout
    else if (!status || status >= 500) [status, json] = [0, { error: 'network', message: NET }]; // Cloudflare's pages
    else [status, json] = [502, { error: 'bad_answer', message: `The PC sent an unexpected answer (${status}).` }];
  }
  if (status === 404 && !json.message) status = 410; // an older server (after a rollback): reload
  if (status === 401 || status === 410 || (status === 403 && json.error !== 'refused')) signedOut(status);
  return { status, json };
}

export async function api(path) {
  const r = await send(path, { headers: { Accept: 'application/json' } });
  if (r.status < 200 || r.status > 299) throw new ApiError(r.status, r.json);
  if (S.global) clearState();
  if (path.startsWith('/api/home')) header(r.json);
  return r.json;
}

export const clock = (t) => fmt(t).slice(-5);
export const when = (t) => (fmtDay(t) === fmtDay(Date.now()) ? clock(t) : fmt(t));
export const tileNote = (t, a, b) => `${a}${b ? ` · ${b}` : ''}${t.stale && t.at ? ` · as of ${when(t.at)}` : ''}`; // F16
export const said = ({ status, json: j }) => `${j.message || `Error ${status}`}${j.retry_after > 0 && !/\d/.test(j.message)
  ? ` You can try again at ${clock(Date.now() + j.retry_after * 1e3)}.` : ''}`; // F14: the real wait, unless the message names it

export const post = (path, body) => send(path, { method: 'POST', body: JSON.stringify(body || {}),
  headers: { 'Content-Type': 'application/json', 'X-Dashboard': '1' } });

const tabLink = (name) => document.querySelector(`.tabs a[data-tab="${name}"]`);

function header(h) {
  const ch = h.channel || {}, hp = h.health || {}, line = $('health'), link = $('chan');
  const first = (hp.platforms || [])[0];
  S.homeAt = Date.now();
  if (ch.name) $('name').textContent = ch.name;
  $('me').textContent = h.me || '';
  if (https(ch.url)) [link.href, link.hidden] = [ch.url, false];
  line.replaceChildren();
  [hp.watcher, hp.drive, ...(hp.platforms || []), hp.stats].filter((b) => b && b.text).forEach((b, i) => {
    line.append(i ? ' · ' : '', el('span', b.ok === false ? 'bad' : null, b.text));
  });
  if (first && /^[a-z]{2,20}$/.test(first.name)) tabLink('publish').href = `#publish/${first.name}`;
  S.reds = (h.attention || []).filter((a) => a.level === 'red').length;
  S.failed = Object.entries(h.counts || {}).some(([k, c]) => k !== 'youtube' && c && c.failed > 0);
  badges();
}

function badge(name, cls, text, label) {
  const b = tabLink(name).querySelector('.badge');
  b.hidden = !cls;
  if (cls) Object.assign(b, { className: `badge ${cls}`, textContent: text, role: 'img', ariaLabel: label });
}

function badges() {
  badge('home', S.reds ? 'red' : '', String(S.reds), `${S.reds} red attention items`);
  if (S.job) badge('publish', 'dot yellow', '', 'Posting now');
  else badge('publish', S.failed ? 'dot' : '', '', 'Some items failed');
}

const live = (j) => !!j && (j.state === 'queued' || j.state === 'running');
export function cut(t, n) { // at most n characters, cut at a space, with '…'
  const s = String(t ?? '').replace(/\s+/g, ' ').trim();
  if (s.length <= n) return s;
  const i = s.lastIndexOf(' ', n - 1);
  return `${s.slice(0, i > n / 2 ? i : n - 1).replace(/[\s.,;:]+$/, '')}…`;
}
export function noteJob(job) {
  S.job = live(job) ? job : null;
  badges();
}

function watchJob() {
  S.jobBusy = true;
  S.jobAt = Date.now();
  api(`/api/job?id=${encodeURIComponent(S.job.id)}`).then((r) => {
    if (S.tab !== 'publish' && r.job && !live(r.job)) toast(`${cut(r.job.result || 'The post has ended.', 120)} See Publish.`);
    S.jobFails = 0;
    noteJob(r.job);
  }, (e) => {
    if (e.status >= 400 && e.status < 500) noteJob(null);
    else S.jobAt += S.jobFails++ < 1 ? 0 : S.jobFails < 4 ? 12e3 : 57e3; // the PC is away: 3 s, 15 s, then 60 s
  }).finally(() => { S.jobBusy = false; });
}

function stamp() {
  const v = views[S.tab] || {};
  $('updated').textContent = v.okAt ? `Updated ${ago(v.okAt)}` : v.at ? 'Not updated yet' : 'Loading…';
}

function banner(text, label, act, cls) {
  const b = el('div', `banner ${cls}`);
  b.setAttribute('role', 'alert');
  b.append(el('p', null, text), btn(label, null, act));
  $('state').replaceChildren(b);
}

function signedOut(status) {
  S.global = true;
  banner(status === 410 ? 'The dashboard was updated.' : 'You are signed out.',
    status === 410 ? 'Reload' : 'Sign in again', () => location.reload(), 'calm');
}

function clearState() {
  S.global = false;
  $('state').replaceChildren();
}

function fail(e, v) {
  if (e && e.load) banner('This part of the page did not load. Reload the page.', 'Reload', () => location.reload(), 'bad');
  else if (!(e instanceof ApiError)) {
    console.error(e);
    banner(`Something went wrong on this page: ${(e && e.message) || e}`, 'Try again', refresh, 'bad');
  } else if (e.status === 401 || e.status === 410 || (e.status === 403 && e.code !== 'refused')) signedOut(e.status);
  else banner(e.status === 0 ? NET : e.code === 'drive_missing' ? 'Google Drive is not available on the PC right now.'
    : e.code === 'timeout' ? e.message : `Something went wrong on the PC: ${e.message}`, 'Try again', refresh, 'bad');
  if (v.el.querySelector(':scope > :not(.loading)')) v.el.classList.add('stale');
}

export function countStrip(title, iconName, boxes) {
  const w = el('div', 'strip'), head = w.appendChild(el('div', 'strip-head')), row = w.appendChild(el('div', 'counts'));
  if (iconName) head.append(icon(iconName));
  head.append(el('span', null, title));
  for (const b of boxes) {
    const a = row.appendChild(el('a', b.cls ? `count ${b.cls}` : 'count'));
    a.href = b.href;
    a.append(el('b', null, num(b.value)), el('span', null, b.label));
    a.addEventListener('click', (e) => { e.preventDefault(); go(b.href, b.section); });
  }
  return w;
}

export function toast(text) {
  for (const t of document.querySelectorAll('.toast')) t.remove();
  const t = el('div', 'toast', text);
  t.setAttribute('role', 'status');
  t.onclick = () => t.remove(); // a tap dismisses it
  (stack.length ? stack[stack.length - 1].dialog : document.body).append(t);
  setTimeout(() => t.remove(), 3500 + Math.min(4000, String(text).length * 40));
}

// Closing sheets goes back in history as one history.go(-n); history changes asked for meanwhile wait for it.
let seq = 0, backs = 0, waiting = 0, later = [];
const sheetHash = (h) => h.slice(1).split('/').filter(Boolean).length >= 3;

function hist(fn) {
  if (backs || waiting) later.push(fn);
  else fn();
}

function back() {
  if (backs++) return;
  setTimeout(() => {
    const n = backs, id = (waiting = ++seq);
    backs = 0;
    history.go(-n);
    setTimeout(() => { if (waiting === id) settle(); }, 700);
  });
}

function settle() {
  const fns = later;
  [waiting, later] = [0, []];
  fns.forEach((fn) => hist(fn));
}

export function openSheet(build) {
  const d = el('dialog', 'sheet'), head = el('div', 'sheet-head'), x = el('button', 'icon-btn'), h = location.hash;
  const s = { dialog: d, title: el('h2', 'sheet-title'), body: el('div', 'sheet-body'), foot: el('div', 'sheet-foot'),
    onClose: null, close: () => closeSheet(s) };
  s.title.id = `sheet-${++seq}`;
  d.setAttribute('aria-labelledby', s.title.id);
  [d.tabIndex, d.autofocus] = [-1, true]; // the sheet takes the focus, not its X (WebKit rings the X after a tap)
  Object.assign(x, { type: 'button', ariaLabel: 'Close' });
  x.append(icon('x'));
  x.addEventListener('click', s.close);
  head.append(s.title, x);
  d.append(head, s.body, s.foot);
  d.addEventListener('cancel', (e) => { e.preventDefault(); s.close(); }); // Esc, Android Back
  d.addEventListener('close', () => { if (stack.includes(s)) s.close(); });
  build(s);
  document.body.append(d);
  if (sheetHash(h) && !stack.some((t) => t.hash === h)) Object.assign(s, { hash: h, pushed: S.navs > 0 });
  else s.token = ++seq;
  stack.push(s);
  if (!s.pushed) {
    hist(() => { // an entry for Back to pop; a hash sheet on page load gets its tab below it
      if (!stack.includes(s) || (s.hash && location.hash !== s.hash)) return; // closed before its turn
      if (s.hash) history.replaceState(null, '', s.hash.split('/').slice(0, 2).join('/'));
      history.pushState(s.hash ? null : { sheet: s.token }, '', s.hash || location.href);
      s.pushed = true;
    });
  }
  d.showModal();
  return s;
}

function finish(s) { // close without touching history; false when it was not open
  const i = stack.indexOf(s);
  if (i < 0) return false;
  stack.splice(i, 1);
  for (const t of s.dialog.querySelectorAll('.toast')) (stack.length ? stack[stack.length - 1].dialog : document.body).append(t);
  if (s.dialog.open) s.dialog.close();
  s.dialog.remove();
  try {
    if (s.onClose) s.onClose();
  } catch (e) { console.error(e); }
  return true;
}

export function closeSheet(s = stack[stack.length - 1]) {
  if (!s || !finish(s)) return;
  if (s.pushed && (s.token || location.hash === s.hash)) back();
  else if (s.hash && location.hash === s.hash) { // closed before its entry was made
    hist(() => {
      history.replaceState(null, '', s.hash.split('/').slice(0, 2).join('/'));
      run('open');
    });
  }
}

export function confirmSheet({ title, text, tick, okLabel = 'OK', danger = false } = {}) {
  return new Promise((resolve) => {
    let yes = false;
    const s = openSheet((s) => {
      const ok = btn(okLabel, danger ? 'danger' : 'primary', () => { yes = true; s.close(); }), no = btn('Cancel', null, () => s.close());
      s.title.textContent = title || 'Are you sure?';
      if (text) s.body.append(el('p', null, text));
      if (tick) {
        const [label, box] = tickBox(tick);
        s.body.append(label);
        ok.disabled = true;
        box.addEventListener('change', () => { ok.disabled = !box.checked; });
      }
      s.foot.append(no, ok);
    });
    s.onClose = () => resolve(yes);
  });
}

export function onRoute(name, render) { routes[name] = render; }

function where() {
  let p = [];
  try { p = decodeURIComponent(location.hash.slice(1)).split('/').filter(Boolean); } catch (e) { /* bad escape */ }
  return TABS.includes(p[0]) ? { tab: p[0], parts: p.slice(1) } : { tab: 'home', parts: [] };
}

function route() {
  const { tab, parts } = where(), old = views[S.tab];
  if (tab === 'publish' && !parts.length) history.replaceState(null, '', tabLink('publish').getAttribute('href'));
  if (tab !== S.tab) {
    if (old) [old.y, old.el.hidden] = [scrollY, true];
    S.tab = tab;
    for (const p of polls) if (p.tab !== tab) polls.delete(p);
    if (!S.global) $('state').replaceChildren();
    const v = views[tab] || (views[tab] = { el: $('views').appendChild(el('section', 'view')) });
    v.el.dataset.view = tab;
    v.el.hidden = false;
    for (const a of document.querySelectorAll('.tabs a')) a.toggleAttribute('aria-current', a.dataset.tab === tab);
    tabLink(tab).setAttribute('aria-current', 'page');
    scrollTo(0, v.y || 0);
    stamp();
  }
  run('open');
}

async function run(why) {
  const name = S.tab, v = views[name];
  if (v.busy) return void (v.again = v.again === 'open' ? 'open' : why);
  v.busy = true;
  let slow = 0;
  if (!v.el.firstChild) {
    const note = v.el.appendChild(el('p', 'loading', 'Loading…'));
    slow = setTimeout(() => { note.textContent = 'Still loading: the PC may be busy.'; }, 10000);
  }
  try {
    const render = routes[name] || await (mods[name] || (mods[name] = LOAD[name]().then(() => routes[name], (e) => {
      delete mods[name];
      throw Object.assign(e, { load: 1 });
    })));
    const w = where();
    if (w.tab !== name) return;
    await render(v.el, w.parts, why);
    v.okAt = Date.now();
    v.el.classList.remove('stale');
    if (S.tab === name && $('state').firstChild) clearState();
    const t = S.section && why !== 'poll' && v.el.querySelector(`[data-section="${CSS.escape(S.section)}"]`);
    if (why !== 'poll') S.section = null;
    if (t) t.scrollIntoView({ block: 'start' });
  } catch (e) {
    if (S.tab === name) fail(e, v);
  } finally {
    clearTimeout(slow);
    for (const n of v.el.querySelectorAll(':scope > .loading')) n.remove();
    const again = v.again;
    Object.assign(v, { busy: false, at: Date.now(), again: null });
    stamp();
    if (again && S.tab === name) run(again);
  }
}

export function refresh() {
  if (S.tab) run('retry');
}

export function go(hash, section) {
  S.section = section || null;
  for (const s of [...stack].reverse()) closeSheet(s);
  hist(() => {
    if (location.hash === hash || (!location.hash && hash === '#home')) run('open');
    else location.hash = hash;
  });
}

export function poll(fn, ms) {
  const p = { fn, ms, tab: S.tab, at: Date.now() };
  polls.add(p);
  return () => polls.delete(p);
}

function homeBeat() {
  [S.homeBusy, S.homeAt] = [true, Date.now()];
  api('/api/home').catch(() => {}).finally(() => { S.homeBusy = false; });
}

function tick() {
  stamp();
  if (document.hidden || !S.tab) return;
  const now = Date.now(), v = views[S.tab];
  for (const p of polls) {
    if (p.tab !== S.tab) polls.delete(p);
    else if (!p.busy && now - p.at >= p.ms - 100) {
      [p.busy, p.at] = [true, now];
      Promise.resolve().then(p.fn).catch((e) => { if (!(e instanceof ApiError)) console.error(e); })
        .finally(() => { p.busy = false; });
    }
  }
  if (!v.busy && !stack.length && now - v.at >= 59900) run('poll');
  if (S.tab !== 'home' && !S.homeBusy && now - S.homeAt >= 59900) homeBeat();
  if (S.job && S.tab !== 'publish' && !S.jobBusy && now - S.jobAt >= 2900) watchJob();
}

addEventListener('popstate', (e) => {
  const tok = (e.state && e.state.sheet) || 0;
  for (const s of [...stack].reverse()) if (s.token ? s.pushed && s.token > tok : s.hash !== location.hash) finish(s);
  settle();
});
addEventListener('hashchange', () => {
  S.navs++;
  for (const s of [...stack].reverse()) if (s.hash !== location.hash) finish(s);
  route();
});
document.addEventListener('visibilitychange', tick);
$('tabs').addEventListener('click', (e) => {
  const a = e.target.closest('a[data-tab]');
  if (!a || a.getAttribute('href') !== location.hash) return;
  e.preventDefault();
  scrollTo(0, 0);
  refresh();
});
history.scrollRestoration = 'manual';
setInterval(tick, 1000);
route();
if (S.tab !== 'home') homeBeat();
