// Activity tab (spec 9.7): the audit list with filters and Load more, and the PC's log tails.
import { api, el, fmt, toast, onRoute, refresh, btn, https, ext } from '/app.js';

const KINDS = { all: 'All', posts: 'Posts', settings: 'Settings', problems: 'Problems' };
const LOGS = { watch: 'Watcher log (last 100 lines)', instagram: 'Instagram log (last 50 lines)' };
let kind = 'all', shown, items = [], more, logs = {};
const opened = {};

function row(a) {
  const r = el('div', 'act'), x = el('div', 'act-x', a.text);
  r.append(el('div', 'act-t', [fmt(a.at), a.label, a.who].filter(Boolean).join(' · ')), x);
  if (https(a.url)) x.append(' ', ext('open', a.url, null));
  return r;
}

async function loadMore(view, b) {
  b.disabled = true;
  try {
    const k = shown, r = await api(`/api/activity?kind=${k}&before=${items[items.length - 1].id}`);
    if (k !== shown) return;
    [items, more] = [items.concat(r.items || []), r.more];
    draw(view);
  } catch (e) {
    toast(e.message);
    b.disabled = false;
  }
}

function draw(view) {
  const bar = el('div', 'filters'), list = el('div', 'panel'), out = [bar, list];
  for (const k in KINDS) {
    const b = bar.appendChild(btn(KINDS[k], null, () => {
      kind = k;
      if (location.hash !== '#activity') history.replaceState(null, '', '#activity');
      refresh();
    }));
    b.setAttribute('aria-pressed', k === shown);
  }
  list.append(...(items.length ? items.map(row) : [el('p', 'empty', 'No activity yet.')]));
  if (more) {
    const b = btn('Load more', 'small', () => loadMore(view, b));
    out.push(el('div', 'row'));
    out[2].append(b);
  }
  out.push(el('div', 'label', 'PC logs'));
  for (const k in LOGS) {
    const d = el('details', 'log'), lines = logs[k] || [];
    Object.assign(d, { open: !!opened[k], ontoggle: () => { opened[k] = d.open; } });
    d.append(el('summary', null, LOGS[k]), el('pre', null, lines.length ? lines.join('\n') : 'Nothing logged yet.'));
    out.push(d);
  }
  view.replaceChildren(...out);
}

onRoute('activity', async (view, parts, why) => {
  if (KINDS[parts[0]]) kind = parts[0]; // #activity/posts
  const k = kind, d = await api(`/api/activity?kind=${k}`), fresh = d.items || [], low = fresh.length ? fresh[fresh.length - 1].id : Infinity;
  if (why === 'poll' && k === shown && items.length > fresh.length && low <= items[0].id) { // keep what Load more added
    items = fresh.concat(items.filter((a) => a.id < low));
  } else [items, more] = [fresh, d.more]; // G25: no overlap with the old list: start again from the newest
  [shown, logs] = [k, d.logs || logs];
  draw(view);
});
