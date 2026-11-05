// Stats tab (spec 9.6, addendum C7): sources, tiles, the YouTube and Reels tables, pairs, insights.
import { api, el, num, fmtDay, onRoute, when, tileNote, many, https, ext } from '/app.js';

const more = {}; // the tables opened with Show all (G26)

const NAME = { youtube: 'YouTube', instagram: 'Instagram', instagram_limit: 'Instagram limit', instagram_permissions: 'Instagram key check' };
const sign = (v) => (typeof v !== 'number' ? '—' : v > 0 ? `+${num(v)}` : v < 0 ? `−${num(-v)}` : '0');
const delta = (v, w) => (typeof v !== 'number' ? '—' : v ? `${sign(v)} ${w}` : `No change ${w}`);
const link = (t, href) => (https(href) ? ext(t, href, null) : t);

function svg(tag, at, ...kids) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const k in at) e.setAttribute(k, at[k]);
  e.append(...kids);
  return e;
}

function spark(vals = []) {
  const pts = vals.map((v, i) => [i, v]).filter((p) => typeof p[1] === 'number'), ys = pts.map((p) => p[1]);
  const lo = Math.min(...ys), hi = Math.max(...ys), n = Math.max(vals.length - 1, 1);
  const xy = ([i, v]) => [(i / n) * 100, hi === lo ? 14 : 25 - ((v - lo) / (hi - lo)) * 22].map((x) => x.toFixed(1));
  const s = svg('svg', { class: 'spark', viewBox: '0 0 100 28', 'aria-hidden': 'true' }), [cx, cy] = pts.length ? xy(pts[pts.length - 1]) : [];
  if (pts.length > 1) s.append(svg('polyline', { points: pts.map((p) => xy(p).join(',')).join(' ') }));
  if (cx) s.append(svg('circle', { cx, cy, r: 2.5 }));
  return s;
}

function sources(list = []) {
  const line = el('div', 'srcline');
  list.filter((s) => NAME[s.source] && (!/_/.test(s.source) || s.fails > 0)).forEach((s, i) => { // limit and key check: only while failing
    const name = NAME[s.source];
    if (i) line.append(' · ');
    line.append(s.fails > 0 || (s.error && !s.last_ok)
      ? el('span', 'err', `${name}${/_/.test(s.source) ? '' : ' numbers'} not read ${s.last_ok ? `since ${when(s.last_ok)}` : 'yet'}: ${s.error || 'unknown error'}`)
      : s.last_ok ? `${name}${i ? '' : ' numbers'} from ${when(s.last_ok)}` : `${name}: not read yet`);
  });
  return line;
}

function table(k, heads, rows, redraw) { // G26: 20 rows, then Show all
  const w = el('div', 'tbl-wrap'), t = w.appendChild(el('table', 'tbl')), hr = t.createTHead().insertRow(), body = t.createTBody();
  for (const [h, c] of heads) hr.append(el('th', c, h));
  for (const [cells, url] of more[k] ? rows : rows.slice(0, 20)) {
    const tr = body.insertRow();
    cells.forEach((x, i) => tr.appendChild(el('td', heads[i][1])).append(x));
    if (https(url)) Object.assign(tr, { className: 'tap', onclick: (e) => e.target.closest('a') || open(url, '_blank', 'noopener') });
  }
  return rows.length > 20 && !more[k] ? [w, Object.assign(el('button', 'link', `Show all (${num(rows.length)})`), { type: 'button', onclick: () => { more[k] = 1; redraw(); } })] : [w];
}

onRoute('stats', async (view) => draw(view, await api('/api/stats')));

function draw(view, d) {
  const out = [sources(d.sources)], tiles = el('div', 'tiles'), T = d.tiles || [], lim = d.limit || {}, ins = d.insights;
  const yt = [...(d.youtube_videos || [])].sort((a, b) => (b.views || 0) - (a.views || 0)), reels = d.reels || [], pairs = d.pairs || [];
  const part = (title, ...x) => out.push(el('div', 'label', title), ...x), redraw = () => draw(view, d);
  for (const t of T) {
    const d7 = t.d7 ?? t.d24;
    tiles.appendChild(el('div', 'tile')).append(el('b', null, num(t.value)), el('span', null, t.label), spark(t.spark),
      el('small', `delta${d7 > 0 ? ' up' : d7 < 0 ? ' down' : ''}`, tileNote(t, delta(t.d24, 'in 24 h'), delta(t.d7, 'in 7 days'))));
  }
  out.push(T.some((t) => typeof t.value === 'number') || yt.length || reels.length ? tiles : el('div', 'panel empty', 'No numbers yet: the PC reads them once an hour.'));
  // G26: the API limit and the insights card before the long tables
  const ck = (d.sources || []).find((x) => x.source === 'instagram_permissions');
  out.push(el('p', 'note', `Instagram API posts in the last 24 h: ${typeof lim.used === 'number' ? `${num(lim.used)} of ${num(lim.total)}${lim.stale && lim.at ? ` (read ${when(lim.at)})` : ''}` : 'not read yet'}.`));
  const card = el('div', 'panel'), p = (t, c) => card.append(el('p', c, t));
  card.append(el('div', 'title', 'Instagram insights'));
  if (!ins) p(ck?.fails > 0 ? 'Not checked: the key check failed (see above).' : 'Not checked yet: the PC checks the key once a day.', 'muted');
  else if (ins.available) p('The key has instagram_manage_insights. Reach and plays are not collected in this build.');
  else {
    p(`Reach and plays are not available: the key lacks instagram_manage_insights${ins.checked ? ` (checked ${fmtDay(ins.checked)})` : ''}.`);
    p('Make a new system-user key with instagram_manage_insights ticked, then run instagram.py system-token on the PC.');
    p("Adding it needs a new key; that is the owner's call.", 'muted');
  }
  out.push(card);
  if (yt.length) {
    part('YouTube videos', ...table('yt', [['Title', 't'], ['Views', 'n'], ['Likes', 'n'], ['Comments', 'n'], ['+24 h', 'n']],
      yt.map((v) => [[link(v.title || v.id, v.url), num(v.views), num(v.likes), num(v.comments), sign(v.d24_views)], v.url]), redraw));
  }
  if (reels.length) {
    part('Reels', ...table('reels', [['Date'], ['Caption', 't'], ['Likes', 'n'], ['Comments', 'n'], ['']],
      reels.map((r) => [[fmtDay(r.at).replace(/ /g, ' '), r.title || r.file || '', num(r.likes), num(r.comments), link('open', r.url)], r.url]), redraw));
  }
  if (pairs.length) {
    const box = el('div', 'panel');
    for (const p of more.pairs ? pairs : pairs.slice(0, 20)) {
      const y = p.youtube || {}, i = p.instagram || {};
      box.append(el('div', 'pair', `${String(p.file || p.lesson).replace(/\.\w+$/, '')}: YouTube ${many(y.views, 'view')}, ${many(y.likes, 'like')}`
        + ` · Instagram ${many(i.likes, 'like')}, ${many(i.comments, 'comment')}`));
    }
    if (pairs.length > 20 && !more.pairs) box.append(Object.assign(el('button', 'link', `Show all (${num(pairs.length)})`), { type: 'button', onclick: () => { more.pairs = 1; redraw(); } }));
    part('YouTube vs Instagram', box);
  }
  const sl = [...view.querySelectorAll('.tbl-wrap')].map((w) => w.scrollLeft); // F49: a refresh keeps the sideways scroll
  view.replaceChildren(...out);
  view.querySelectorAll('.tbl-wrap').forEach((w, i) => { w.scrollLeft = sl[i] || 0; });
}
