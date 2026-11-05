// Home tab (spec 9.2, addendum C2): attention cards, what goes out next, counts, channel numbers.
import { api, post, el, icon, num, go, toast, confirmSheet, countStrip, onRoute, refresh, said, tileNote, btn, ext } from '/app.js';

const TAG = { sinem: ['For Sinem', 'c-yellow'], pc: ['On the PC', 'c-blue'] };
const TICKS = { reset_settings: 'Yes, reset the settings to the defaults', resume_auto: 'I checked the problem',
  auto_on: 'Start posting automatically' };
const YOUTUBE = [['ready', 'Ready to upload', '#videos/ready'], ['uploaded', 'Uploaded (private)', '#videos'],
  ['scheduled', 'Scheduled', '#videos'], ['public', 'Public', '#videos']];
const ICONS = { instagram: 'ig', youtube: 'yt' };

async function call(a, btn) {
  if (a.confirm || a.ack) {
    const yes = await confirmSheet({ title: a.label, text: a.confirm || '', okLabel: a.label,
      tick: a.ack ? TICKS[a.ack] || 'I understand' : null });
    if (!yes) return;
  }
  const body = { ...(a.body || {}) };
  if (a.ack) body.ack = [...(Array.isArray(body.ack) ? body.ack : []), a.ack]; // only after the confirmation
  btn.disabled = true;
  const r = await post(a.endpoint, body);
  toast(r.status < 400 && !r.json.message ? 'Done.' : said(r));
  refresh();
}

function action(a) {
  if (a.kind === 'link' && /^https:\/\//.test(a.href || '')) return ext(a.label, a.href, 'btn');
  if (a.kind === 'link' && /^#/.test(a.href || '')) {
    const b = Object.assign(el('a', 'btn', a.label), { href: a.href });
    b.addEventListener('click', (e) => { e.preventDefault(); go(a.href); });
    return b;
  }
  if (a.kind === 'call' && /^\/api\//.test(a.endpoint || '')) {
    const b = btn(a.label, null, () => call(a, b));
    return b;
  }
  return null;
}

function card(a) {
  const c = el('div', `panel attn edge-${a.level === 'red' ? 'red' : 'yellow'}`), tag = TAG[a.who];
  const act = a.action && action(a.action);
  if (tag) c.append(el('span', `chip ${tag[1]}`, tag[0]));
  c.append(el('div', 'title', a.title));
  if (a.text) c.append(el('p', null, a.text));
  if (act) c.appendChild(el('div', 'row')).append(act);
  return c;
}

function allGood() {
  const c = el('div', 'panel ring-green attn-ok');
  c.append(icon('check'), el('span', null, 'All good. Nothing needs you right now.'));
  return c;
}

function nextList(list) {
  const box = el('div', 'panel list');
  for (const n of list) {
    const inApp = /^#/.test(n.href || ''), row = el(inApp ? 'a' : 'div', 'li');
    row.append(icon(ICONS[n.platform] || 'clock'), el('span', null, n.text));
    if (inApp) {
      row.href = n.href;
      row.addEventListener('click', (e) => { e.preventDefault(); go(n.href); });
      row.append(icon('chev'));
    }
    box.append(row);
  }
  return box;
}

function strips(counts) {
  const out = [];
  for (const [name, c] of Object.entries(counts || {})) {
    if (name === 'youtube' || !c || !/^[a-z]{2,20}$/.test(name)) continue;
    const href = `#publish/${name}`, box = (label, key, cls) => ({ label, value: c[key], cls, href, section: key });
    const title = `${name[0].toUpperCase()}${name.slice(1)}${name === 'instagram' ? ' Reels' : ''}`;
    out.push(countStrip(title, ICONS[name], [box('Waiting', 'waiting', 'yellow'), box('Skipped', 'skipped'),
      box('Failed', 'failed', c.failed > 0 ? 'red' : ''), box('Posted', 'posted')]));
  }
  const y = counts && counts.youtube;
  if (y) out.push(countStrip('YouTube', 'yt', YOUTUBE.map(([k, label, href]) => ({ label, value: y[k], href }))));
  return out;
}

const week = (d) => (typeof d !== 'number' ? '—' : d > 0 ? `+${num(d)} in 7 days`
  : d < 0 ? `−${num(-d)} in 7 days` : 'No change in 7 days'); // F44: a rolling 7 days

function tiles(list) {
  const grid = el('div', 'stats');
  for (const t of list) {
    const a = el('a', 'stat');
    a.href = '#stats';
    a.addEventListener('click', (e) => { e.preventDefault(); go('#stats'); });
    a.append(el('b', null, num(t.value)), el('span', null, t.label),
      el('small', `delta${t.d7 > 0 ? ' up' : t.d7 < 0 ? ' down' : ''}`, tileNote(t, week(t.d7))));
    grid.append(a);
  }
  return grid;
}

onRoute('home', async (view) => {
  const d = await api('/api/home'), att = d.attention || [], out = att.length ? att.map(card) : [allGood()];
  if ((d.next || []).length) out.push(el('div', 'label', 'Next'), nextList(d.next));
  out.push(...strips(d.counts));
  if ((d.tiles || []).length) out.push(el('div', 'label', 'Numbers'), tiles(d.tiles));
  view.replaceChildren(...out);
});
