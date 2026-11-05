// Videos tab (spec 9.5): the v1 list with each video's Reel; a picked filter is remembered here.
import { api, post, el, icon, num, len, fmt, go, toast, confirmSheet, onRoute, refresh, said, btn, https, ext } from '/app.js';

const STATES = { public: 'Public', scheduled: 'Scheduled', uploaded: 'Uploaded (private)', ready: 'Ready to upload',
  making: 'Being made', new: 'New recording', waiting: 'Waiting', stopped: 'Stopped' };
const FILTERS = [['all', 'All'], ['public', 'Public'], ['coming', 'Coming up'], ['ready', 'Ready to upload'],
  ['short', 'Shorts'], ['video', 'Videos']];
const REEL = { skipped: 'Reel skipped', failed: 'Reel failed', checking: 'Reel being checked' };
const KEY = 'dashFilter', TZ = 'Europe/Istanbul', YT_ID = /^[\w-]{11}$/;
const DAY = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
const CLOCK = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
let saved = 'all', data = null;
try { saved = localStorage.getItem(KEY) || 'all'; } catch (e) { /* storage blocked */ }
if (!FILTERS.some(([k]) => k === saved)) saved = 'all';

function dayName(t) {
  const d = new Date(t), p = {};
  if (isNaN(d)) return 'No date';
  for (const x of DAY.formatToParts(d)) p[x.type] = x.value;
  return `${p.weekday} ${p.day} ${p.month} ${p.year}`;
}

const pick = (v, f) => (f === 'public' ? v.state === 'public' : f === 'coming' ? v.state !== 'public' : f === 'ready'
  ? v.state === 'ready' : f === 'short' || f === 'video' ? v.kind === f : true);
const reelOk = (r) => !!r && /^[a-z]{2,20}$/.test(r.platform) && /^[0-9a-f]{12}$/.test(r.id);

function thumb(v) {
  const th = el('div', 'thumb'), word = v.kind === 'short' ? 'Short' : 'Video';
  if (YT_ID.test(v.youtube || '')) {
    th.append(Object.assign(el('img'), { alt: '', loading: 'lazy', onerror: () => { th.textContent = word; },
      src: `https://i.ytimg.com/vi/${v.youtube}/mqdefault.jpg` }));
  } else if (v.kind === 'lesson') th.append(icon(v.state === 'stopped' ? 'warn' : 'clock'));
  else th.textContent = word;
  return th;
}

function reel(v) {
  const r = v.reel, url = https(v.instagram?.url) && v.instagram.url, state = r ? r.state : url && 'posted', line = el('div', 'reel');
  if (!state) return '';
  line.append(icon('ig'));
  if (state === 'posted') line.append(el('span', null, url ? 'Reel posted ·' : 'Reel posted'), url ? ext('open', url) : '');
  else if (state === 'waiting' && reelOk(r)) {
    line.append(el('span', null, 'Reel waiting ·'), btn('Post', 'small primary', () => go(`#publish/${r.platform}/post/${r.id}`)));
  } else {
    line.append(el('span', null, REEL[state] || `Reel ${state}`),
      reelOk(r) ? Object.assign(el('a', 'btn small', 'Open Publish'), { href: `#publish/${r.platform}` }) : '');
  }
  return line;
}

function retry(v) {
  const b = btn(v.retry ? 'Retry asked: waiting for the PC' : 'Retry this lesson', 'danger small', async () => {
    if (!(await confirmSheet({ title: 'Retry this lesson', text: `Run '${v.lesson}' again from the start?`, okLabel: 'Retry' }))) return;
    b.disabled = true;
    toast(said(await post('/api/lesson/retry', { lesson: v.lesson })));
    refresh();
  });
  b.disabled = !!v.retry;
  return b;
}

function count(n, word) {
  const s = el('span');
  s.append(el('b', null, num(n)), ` ${word}${n === 1 ? '' : 's'}`);
  return s;
}

function card(v) {
  const c = el('div', 'card'), body = el('div'), top = el('div', 'chips'), t = el('div', 'title'), meta = el('div', 'meta');
  top.append(el('span', `chip st-${v.state}`, STATES[v.state] || v.state));
  if (v.kind !== 'lesson') top.append(el('span', 'chip', v.kind === 'short' ? `Short${v.part ? ` ${v.part}` : ''}` : 'Video'));
  if (v.privacy && v.privacy !== 'public') top.append(el('span', 'chip', v.privacy));
  if (v.time && !isNaN(new Date(v.time))) {
    top.append(el('span', 'when', `${v.state === 'scheduled' ? 'goes public ' : ''}${CLOCK.format(new Date(v.time))}`));
  }
  t.append(YT_ID.test(v.youtube || '') ? ext(v.title || v.lesson, `https://youtu.be/${v.youtube}`, null) : v.title || v.lesson);
  meta.append(el('span', null, v.lesson));
  if (v.step && v.kind === 'lesson') meta.append(el('span', null, v.step));
  if (v.clips) meta.append(el('span', null, `${v.clips} clip${v.clips === 1 ? '' : 's'}`));
  if (v.seconds) meta.append(el('span', null, len(v.seconds)));
  if (v.views != null) meta.append(count(v.views, 'view'), v.likes ? count(v.likes, 'like') : '', v.comments ? count(v.comments, 'comment') : '');
  body.append(top, t, meta, reel(v), v.error ? el('div', 'err', v.error) : '');
  if (v.state === 'stopped') body.appendChild(el('div', 'row')).append(retry(v));
  c.append(thumb(v), body);
  return c;
}

function draw(view, deep) {
  const f = deep || saved, ch = data.channel || {}, bits = [], bar = el('div', 'filters'), list = el('div');
  if (data.counts_at) bits.push(`YouTube numbers from ${fmt(data.counts_at)}`);
  if (typeof ch.videos === 'number') bits.push(`${num(ch.videos)} videos on the channel`);
  for (const [k, label] of FILTERS) {
    bar.appendChild(btn(label, null, () => {
      saved = k;
      try { localStorage.setItem(KEY, k); } catch (e) { /* storage blocked */ }
      if (location.hash !== '#videos') history.replaceState(null, '', '#videos');
      draw(view, null);
    })).setAttribute('aria-pressed', String(k === f));
  }
  const shown = (data.videos || []).filter((v) => pick(v, f));
  if (!shown.length) list.append(el('div', 'empty', 'Nothing here yet.'));
  let day = '';
  for (const v of shown) {
    const d = v.time ? dayName(v.time) : 'No date';
    if (d !== day) list.append(el('div', 'day', (day = d)));
    list.append(card(v));
  }
  view.replaceChildren(...(bits.length ? [el('p', 'muted small', bits.join(' · '))] : []), bar, list);
}

onRoute('videos', async (view, parts) => {
  data = await api('/api/videos');
  draw(view, FILTERS.some(([k]) => k === parts[0]) ? parts[0] : null);
});
