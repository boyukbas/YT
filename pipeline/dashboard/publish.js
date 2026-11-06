// Publish tab (spec 9.3, 9.4): status, job banner, queue, sheets. Only the post sheet's button posts.
import { api, post, el, icon, num, len, fmt, fmtDay, ago, go, toast, openSheet, confirmSheet, tickBox, countStrip,
  noteJob, poll, onRoute, refresh, said, clock, when, btn, https, ext, many } from '/app.js';

const ST = ['preparing', 'sharing', 'processing', 'publishing', 'saving'], DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const KEYS = ['auto', 'every_days', 'per_day', 'days', 'post_after', 'post_before', 'min_gap_hours', 'start_date'];
const LIM = { chars: 2200, tags_hard: 30, tags_suggested: 5, visible: 125 }, LV = { block: 0, warn: 1 }, CAP = /^caption_/, TAG = /#[\p{L}\p{N}_]+/gu;
let P, D, V, L, N, JB, F, ps, all, gone, busy, uid = 0;
const WAIT = {}, MEM = {}, IST = ' (Istanbul time)';

const live = (j) => /^(queued|running)$/.test(j?.state);
const up1 = (s) => s[0].toUpperCase() + s.slice(1);
const ICON = { instagram: 'ig', youtube: 'yt' };
const VB = () => (/^[a-z]{2,12}$/.test(D?.verb || '') ? D.verb : 'post'), Vb = () => up1(VB()); // 'post' / 'upload'
const done = () => `${VB()}ed`.replace(/ee?d$/, 'ed');                                            // 'posted' / 'uploaded'
const wide = (it) => P === 'youtube' && !it.part;                                                 // a lesson video: 16:9 thumbnail
const stem = (k) => String(k || '').split('/').pop().replace(/\.\w+$/, '');
const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
const line = (it) => [it.lesson, it.part ? `Short ${it.part}` : 'Lesson video', len(it.seconds)].filter(Boolean).join(' · ');
const pic = (it) => /^\/api\/cover\/\w+\/\w+\.jpg$/.test(it.cover);
const cover = (it, c) => (pic(it) ? Object.assign(el('img', c), { alt: '', loading: 'lazy', src: it.cover }) : el('div', c));
function switcher(d) { // one link per platform when there is more than one (Instagram · YouTube)
  const ps = (d.platforms || []).filter((x) => /^[a-z]{2,20}$/.test(x.name));
  if (ps.length < 2) return '';
  const nav = el('div', 'chips seg');
  nav.setAttribute('role', 'tablist');
  for (const x of ps) {
    const a = Object.assign(el('a', `chip${x.name === P ? ' on' : ''}`, x.label || x.name), { href: `#publish/${x.name}` });
    a.setAttribute('role', 'tab');
    a.setAttribute('aria-selected', String(x.name === P));
    nav.append(a);
  }
  return nav;
}
const checking = () => `This ${N}'s last ${VB()} was interrupted. Check it first.`;
const later = (a, b) => { // the later of two ISO times, or null
  const x = Date.parse(a) || 0, y = Date.parse(b) || 0;
  return x || y ? new Date(Math.max(x, y)).toISOString() : null;
};
const held = (d = D) => !!((d.breaker?.state && d.breaker.state !== 'closed') || d.paused); // then waiting buttons are not primary
const holdUntil = (d = D) => later(d.breaker?.state === 'cooling' && d.breaker.until, d.paused?.until);
const reload = () => go(`#publish/${P}`);
const toPost = (it) => (e) => { e.currentTarget.disabled = true; go(`#publish/${P}/post/${it.id}`); };
const beyond = (x) => x.more > 0 && x.rows?.length && `+${many(x.more, `more ${N}`)} after ${fmtDay(x.rows[x.rows.length - 1].at)}`; // F37
const waits = (k) => WAIT[k] > Date.now() && WAIT[k]; // F14, G27: the server's retry_after

async function hold(fn) { // F13: one action at a time; the pressed button is off until the answer
  if (busy) return;
  const b = (busy = btn.last || {});
  b.disabled = true;
  try { return await fn(); } finally { [busy, b.disabled] = [null, false]; }
}

const act = (path, body, ask) => hold(async () => {
  if (ask && !(await confirmSheet(ask))) return;
  const r = await post(path, body);
  if (r.json.retry_after) WAIT[path + (body.job || '')] = Date.now() + r.json.retry_after * 1e3; // per job: Check again
  toast(said(r));
  reload();
});

const op = (o, it, x, keep) => hold(async () => { // keep: a sheet with typed text stays open on 409 stale and 422
  const r = await post('/api/queue', { platform: P, op: o, id: it?.id, ...x, rev: D.queue.rev }), stale = r.json.error === 'stale';
  if (stale) D.queue.rev = r.json.queue?.rev ?? D.queue.rev;
  toast(keep && stale ? 'The list changed on another phone. Your text is kept: tap the button again.' : said(r));
  if (r.status < 300 || r.status === 404 || (r.status === 409 && !(keep && stale))) reload();
  return r;
});

const draft = (id, v) => (v === undefined ? MEM[P + id] : (MEM[P + id] = v)); // F46: an item's unsaved caption (null: none)

function since(j) {
  const t0 = Date.parse(j.started || j.created), e = el('span', 'elapsed', len(Math.max(0, (Date.parse(j.finished) || Date.now()) - t0) / 1e3));
  if (live(j) && !gone) e.dataset.since = t0;
  e.ariaHidden = 'true';
  return e;
}
const tock = () => document.querySelectorAll('[data-since]').forEach((e) => { e.textContent = len(Math.max(0, Date.now() - e.dataset.since) / 1e3); });

function result(j) { // the reason once, the time, and what to do next
  const ok = j.state === 'done', r = el('div', `result ${ok ? 'ok' : 'bad'}`);
  const head = ok ? `${up1(done())}.` : j.result || `Not ${done()}.`;
  const x = ok ? j.record_pending && 'The record on Drive is written later.'
    : j.error && !String(head).includes(j.error) ? j.error : '';
  r.append(el('b', null, head), j.finished ? el('span', 'muted', ` (${clock(j.finished)})`) : '');
  if (ok && https(j.url)) r.append(' ', ext(`Open on ${L}`, j.url, 'btn link'));
  if (x) r.append(el('div', 'small', x));
  const t = holdUntil(), again = 'Nothing was published. You can try again now.'; // the hint only while its cause lasts
  const test = D.breaker?.state === 'stopped' ? 'Nothing was published. Test the connection on this page.' : again;
  const next = !ok && { rate: t ? `Nothing was published. Try again after ${when(t)}.` : again,
    auth: test, permission: test, transient: 'Nothing was published. Try again in a few minutes.' }[j.kind];
  if (next) r.append(el('div', 'small', next));
  return r;
}

const LOST = 'Lost contact with the PC; the post may still be running.';
function jobBox(j) {
  const k = j ? j.id + j.state + j.stage + !!gone : '', i = ST.indexOf(j?.stage);
  if (!JB || JB.dataset.k === k) return;
  JB.dataset.k = k;
  JB.replaceChildren();
  if (!j || /^(interrupted|unknown)$/.test(j.state)) return;
  if (!live(j)) return JB.append(result(j));
  const b = JB.appendChild(el('div', 'panel job')), dots = el('div', 'dots');
  b.appendChild(el('div', 'job-line', `${up1(VB())}ing ${stem(j.key)} · ${i < 0 ? j.result || 'Waiting to start…'
    : `${i === 2 ? `Processing on ${L}` : up1(j.stage)} (step ${i + 1} of 5)`} · `)).append(since(j));
  dots.append(...ST.map((_, n) => el('i', n < i ? 'done' : n === i && 'now')));
  b.append(dots, gone ? el('p', 'err', LOST) : '');
}

function follow(j, fresh) {
  noteJob(j);
  const end = !fresh && F && F.id === j?.id && !live(j);
  gone = 0;
  if (F && (fresh || !live(j) || F.id !== j.id)) F = F.stop();
  if (live(j) && !F) { // F48: when the PC cannot be reached, say so and poll after 3 s, 15 s, then every 60 s
    const a = poll(() => Date.now() < F.next || api(`/api/job?id=${j.id}`).then((r) => follow(r.job), (e) => {
      if (e.status > 399 && e.status < 500) return follow(null);
      F.next = Date.now() + (F.bad++ < 1 ? 0 : F.bad < 4 ? 12e3 : 57e3);
      if (F.bad === 2) {
        gone = 1;
        jobBox(F.j);
        ps?.job(F.j);
      }
    }), 3000);
    const b = poll(tock, 1000);
    F = { id: j.id, bad: 0, next: 0, stop: () => { a(); b(); } };
  }
  if (F) F.j = j;
  jobBox(j);
  if (j) ps?.job(j);
  if (end) refresh();
}

function statusCard(d) {
  const a = d.auto, b = d.breaker, open = b.state && b.state !== 'closed', c = el('div', 'panel'), row = el('div', 'row'), lp = d.last_post || {};
  const p = (t, k = 'muted') => t && c.append(el('p', k, t)), add = (...x) => row.append(btn(...x)), probs = d.settings_problems || [], pf = a.pause_file?.since;
  const set = (s, ack, ask) => () => act('/api/settings', { platform: P, rev: d.settings_rev, settings: s, ack }, ask), V1 = VB(), AU = `Automatic ${V1}ing`;
  const why = { rate: `${L} is paused`, auth: `${L} refused the PC's key`, permission: `${L} refused a permission` }[b.kind] || `${L} stopped answering`;
  const pz = d.paused?.text ? d.paused : null; // one cause (the daily limit): one line, one time, unless the breaker waits longer
  const one = pz && open && b.kind === 'rate' && !(Date.parse(b.until) > Date.parse(pz.until));
  const bm = `${why}: ${one ? pz.text : b.message}`;
  const big = { off: `${AU}: OFF`, invalid: `${AU}: OFF (the settings file has a problem)`,
    stopped_by_file: `ON in settings, but stopped by the pause file${pf ? ` (since ${when(pf)})` : ''}`,
    suspended: `${AU}: SUSPENDED after 3 failed tries`, breaker: bm,
    unresolved: `ON, but waiting: an earlier ${V1} was interrupted` }[a.reason];
  c.append(el('div', `big ${/^(suspended|breaker|invalid)$/.test(a.reason) ? 'is-bad' : big || pz ? '' : 'is-on'}`, big || `ON · ${a.summary}`));
  if (a.suspended) p(`Last try ${when(a.suspended.since)}: ${a.suspended.why}`, 'err');
  p(a.next?.text && a.next.text + IST);
  if (open && a.reason !== 'breaker') p(bm, 'err');
  if (pz && !one) p(pz.text, 'err');
  probs.forEach((x) => p(x, 'err'));
  if (lp.at) {
    const t = `Last ${N}: ${fmt(lp.at)} (${ago(lp.at)})`;
    c.appendChild(el('p', 'muted')).append(https(lp.url) ? ext(t, lp.url, 'tall') : t);
  }
  if (probs.length) add('Reset to defaults', null, () => act('/api/settings/reset', { ack: ['reset_settings'] }, { title: 'Reset the settings', okLabel: 'Reset', tick: 'Yes, reset the settings to the defaults' }));
  else {
    add('Settings', null, () => settings(), 'sliders');
    if (!a.setting) add('Turn on', null, () => settings(1));
    else add('Turn off', null, set({ auto: false }, [], { title: `Turn off automatic ${V1}ing`, okLabel: 'Turn off', danger: true,
      text: `Stop automatic ${V1}ing? Nothing is ${done()} until you ${V1} a ${N} yourself or turn it on again.` }));
  }
  if (a.reason === 'stopped_by_file') {
    add('Remove the pause file', null, set({ auto: true }, ['auto_on'], { title: 'Remove the pause file', okLabel: 'Remove', tick: `Start ${V1}ing automatically`,
      text: `${AU} starts again: ${a.summary}.` }));
  }
  if (a.suspended) {
    add('Resume…', null, () => act('/api/auto/resume', { platform: P, ack: ['resume_auto'] }, { title: `Resume automatic ${V1}ing`, okLabel: 'Resume',
      text: `${AU} stopped after 3 failed tries (last: ${a.suspended.why}).`, tick: 'I checked the problem' }));
  }
  if (b.state === 'cooling') { if (!one) p(`${up1(V1)}ing starts again after ${fmt(holdUntil(d))}${IST}.`); } // S9: ends on its own; one: the line above has the time
  else if (open) { // F14: after a 429 the button waits until the server allows the next test
    const w = waits('/api/platform/test');
    row.appendChild(btn(w ? `Test again at ${clock(w)}` : 'Test connection', null, () => act('/api/platform/test', { platform: P }))).disabled = !!w;
  }
  c.append(row);
  return c;
}

function unresolved(j) {
  const c = el('div', 'panel edge-red'), row = el('div', 'row'), ask = (o, t, k) => btn(t, k, () => act('/api/job/resolve', { job: j.id, outcome: o },
    { title: t, okLabel: t, text: o !== 'check' && `Only press this after you looked at ${L}.` }));
  c.append(el('div', 'title', `A post was interrupted (${stem(j.key)}, ${fmt(j.started || j.created)}). The PC could not confirm whether it went out.`),
    el('p', 'muted', `Press Check again first: the PC asks ${L} (it also checks by itself every 10 minutes). Answer by hand only if that does not settle it.`), row);
  const w = waits(`/api/job/resolve${j.id}`);
  row.append(Object.assign(ask('check', w ? `Check again at ${clock(w)}` : 'Check again', 'primary'), { disabled: !!w }), ask('posted', `It is on ${L}`), ask('not_posted', `It is not on ${L}`));
  if (https(D.account_url)) row.append(ext(`Open the ${L} profile`, D.account_url, 'btn link'));
  return c;
}

function card(it, right, more) {
  const c = el('div', 'qcard'), b = el('div', 'qbody'), ch = el('div', 'chips'), n = it.position;
  if (n) ch.append(el('span', n > 1 ? 'chip' : 'chip c-yellow', n > 1 ? `#${n}` : '#1 Next'));
  if (it.top) ch.append(el('span', 'chip c-blue', 'Moved to top'));
  if (it.custom_caption) ch.append(el('span', 'chip', 'Custom caption'));
  if (ch.firstChild) b.append(ch);
  b.append(el('div', 'title', it.title || it.file), el('div', 'meta', [line(it), !D.forecast.assumes_on && it.eta && fmtDay(it.eta)].filter(Boolean).join(' · ')), more || '');
  if (it.state !== 'posted') Object.assign(b, { tabIndex: 0, role: 'button', onclick: () => itemSheet(it), onkeydown: (e) => e.key === 'Enter' && itemSheet(it) });
  c.append(cover(it, 'cover'), b, right || '');
  return c;
}

function draw() {
  const d = D, q = d.queue, c = d.counts, fc = d.forecast, W = q.waiting || [], PO = q.posted || [], pr = q.problems || [], h = el('h2', 'inline'), out = [h];
  const tot = q.posted_total ?? PO.length, box = (label, k, cls) => ({ label, value: c[k], cls, href: `#publish/${P}`, section: k });
  const list = (k, t, items, f, x) => {
    const s = el('div'), hd = s.appendChild(el('div', 'list-head'));
    s.dataset.section = k;
    hd.append(el('div', 'label', `${t} (${num(k === 'posted' ? tot : items.length)})`), x || '');
    s.append(...items.map(f));
    if (items.length || k === 'waiting') out.push(s);
    return s;
  };
  h.append(icon(ICON[P] || 'publish'), `${L} ${N}s`);
  out.unshift(switcher(d));
  if (!d.set_up) out.push(el('div', 'panel ring-red', `${L} is not set up on this PC${d.setup_hint ? ` (${d.setup_hint})` : ''}.`));
  out.push(statusCard(d), (JB = el('div')));
  JB.setAttribute('aria-live', 'polite');
  for (const j of d.unresolved || (/^(interrupted|unknown)$/.test(d.job?.state) ? [d.job] : [])) out.push(unresolved(j));
  out.push(countStrip(up1(`${N}s`), null, [box('Waiting', 'waiting', 'yellow'), box('Skipped', 'skipped'), box('Failed', 'failed', c.failed > 0 && 'red'), box(up1(done()), 'posted')]));
  for (const t of [(c.by_lesson || []).map((x) => `${x.lesson} ${x.waiting}`).join(' · '), fc.runway_text && fc.runway_text + (fc.assumes_on ? (d.auto.effective ? ` (once automatic ${VB()}ing runs again)` : ` (if automatic ${VB()}ing were on)`) : ''), beyond(fc),
    pr.length && `${many(pr.length, 'lesson')} could not be read: ${pr.map((x) => `${x.lesson}: ${x.error}`).join('; ')}`]) if (t) out.push(el('p', 'note', t));
  const hd = held(d), ht = hd && holdUntil(d); // paused: the buttons still open the sheet, which says why
  const ws = list('waiting', 'Waiting', W, (it) => card(it, Object.assign(btn(Vb(), hd ? 'small' : 'small primary', toPost(it)), { ariaLabel: `${Vb()} ${it.title || it.file}` })),
    W.some((i) => i.top) && btn('Reset order', 'small', () => op('reset_order')));
  if (ht) ws.querySelector('.list-head .label').after(el('span', 'chip c-yellow', `Paused until ${when(ht)}`));
  if (!W.length) ws.append(el('div', 'panel empty', `Nothing waiting. New ${N}s appear here when a lesson is finished.`));
  list('failed', 'Failed', q.failed || [], (it) => (it.state === 'checking' ? card(it, 0, el('div', 'err', checking()))
    : card(it, btn('Try again', 'small', () => op('clear_failed', it)), el('div', 'err', `${it.failed?.reason || 'Failed'}${it.failed?.at ? ` (${fmt(it.failed.at)})` : ''}`))));
  list('skipped', 'Skipped', q.skipped || [], (it) => card(it, btn('Unskip', 'small', () => op('unskip', it))));
  const pl = list('posted', up1(done()), all ? PO : PO.slice(0, 3), (it) => {
    const p = it.posted || {};
    return card(it, https(p.url) && ext('open', p.url), el('div', 'meta', [fmt(p.at), p.likes != null && many(p.likes, 'like'), p.comments != null && many(p.comments, 'comment')].filter(Boolean).join(' · ')));
  });
  const cut = tot > PO.length; // F54: the server sends the newest 10
  if (!all && tot > 3) pl.append(btn(cut ? `Show the latest ${PO.length} of ${num(tot)}` : `Show all (${num(tot)})`, 'link', () => { all = 1; draw(); }));
  else if (all && cut) pl.appendChild(el('p', 'note', 'Every post is listed in ')).append(Object.assign(el('a', null, 'Activity › Posts'), { href: '#activity/posts' }), '.');
  V.replaceChildren(...out);
  jobBox(d.job);
}

onRoute('publish', async (view, parts, why) => {
  P = /^[a-z]{2,20}$/.test(parts[0] || '') ? parts[0] : 'instagram';
  const d = await api(`/api/publish?platform=${P}`), id = parts[1] === 'post' && /^[0-9a-f]{12}$/.test(parts[2] || '') && parts[2];
  for (const k of ['auto', 'breaker', 'counts', 'forecast', 'queue']) d[k] ||= {};
  [V, D, L, N] = [view, d, d.label, d.noun];
  draw();
  if (!location.hash.startsWith(`#publish/${P}`)) return; // F4: left during the load (Back or another tab)
  follow(d.job, 1);
  if (id && why !== 'poll' && ps?.id !== id && location.hash === `#publish/${P}/post/${id}`) postSheet(id);
});

function capChecks(s, lim) {
  const n = s.length, t = (s.match(TAG) || []).length;
  return [!s.trim() && ['caption_empty', 'block', 'The caption is empty.'],
    n > lim.chars && ['caption_too_long', 'block', `The caption has ${num(n)} characters; the limit is ${num(lim.chars)}.`],
    t > lim.tags_hard ? ['caption_tags_limit', 'block', `${t} hashtags; ${L} allows ${lim.tags_hard}.`]
      : t > lim.tags_suggested && ['caption_tags', 'info', `${t} hashtags; the channel uses at most ${lim.tags_suggested}.`]]
    .filter(Boolean).map(([code, level, text]) => ({ code, level, text }));
}

function capBlock(id, text, def, lim, onInput) { // text: the saved caption; an unsaved draft of item id wins (F46)
  const f = el('div', 'field'), lab = el('label', null, 'Caption'), ta = el('textarea', 'caption'), cnt = el('div', 'counter');
  const err = el('div', 'field-err'), pf = el('div', 'field'), pre = el('div', 'preview'), dr = draft(id), back = el('p', 'note warn', 'Your changes are not saved yet. ');
  const upd = (quiet) => {
    const s = ta.value, n = s.length, t = (s.match(TAG) || []).length, w = s.trim(), first = w.split('\n')[0].slice(0, lim.visible);
    cnt.replaceChildren(el('span', n > lim.chars && 'over', `${num(n)} / ${num(lim.chars)} characters`), ' · ',
      el('span', t > lim.tags_hard ? 'over' : t > lim.tags_suggested && 'warn', `${many(t, 'hashtag')} (${lim.tags_suggested} suggested)`));
    pre.replaceChildren(first, w.length > first.length ? el('span', 'more', ' … more') : '');
    back.hidden = s === text;
    if (quiet) return;
    draft(id, s === text ? null : s);
    onInput();
  };
  text ||= '';
  Object.assign(ta, { id: `cap${++uid}`, rows: 10, value: dr ?? text, oninput: () => upd() });
  lab.htmlFor = ta.id;
  back.append(btn('Undo my changes', 'link small', () => { ta.value = text; upd(); }));
  f.append(lab, back, ta, cnt, err, btn('Reset to default', 'link small', () => { ta.value = def || ''; upd(); }));
  pf.append(el('label', null, 'What followers see first'), pre);
  upd(1);
  return { nodes: [f, pf], ta, err };
}

function itemSheet(it) {
  openSheet((s) => {
    const w = it.state === 'waiting', st = el('div', 'stack'), e = btn('Edit caption', null, () => capSheet(it, pv), 'edit');
    const cp = btn('Copy caption', null, () => copyCap(pv.caption, s), 'copy'), sv = Object.assign(el('a', 'btn', 'Save cover'), { href: `${it.cover}?download=1`, download: '' });
    let pv;
    s.title.textContent = it.title || it.file;
    if (pic(it)) s.body.append(cover(it, wide(it) ? 'cover-wide' : 'cover-lg'));
    s.body.append(el('p', 'muted', line(it)), st);
    if (it.state === 'checking') st.before(el('p', 'err', checking()));
    else st.append(btn(`${Vb()} now`, held() ? null : 'primary', toPost(it), 'publish'));
    if (w) st.append(btn(it.top ? 'Back to normal order' : 'Move to top', null, () => op(it.top ? 'untop' : 'top', it), 'up'));
    if (w || it.state === 'skipped') st.append(btn(w ? `Skip this ${N}` : 'Unskip', null, () => op(w ? 'skip' : 'unskip', it)));
    e.disabled = cp.disabled = true;
    sv.prepend(icon('download'));
    st.append(e, cp, pic(it) ? sv : '');
    if (it.state === 'failed') st.append(btn('Try again', null, () => op('clear_failed', it), 'refresh'));
    if (it.state !== 'checking') st.append(btn(`Mark as ${done()}…`, null, () => markSheet(it), 'check'));
    api(`/api/preview?platform=${P}&id=${it.id}`).then((r) => { pv = r; e.disabled = cp.disabled = false; }, (x) => s.body.append(el('p', 'err', x.message)));
  });
}

function markSheet(it) { // F5 (P1): a Reel posted by hand in the app: a tick, an optional link, the time (Istanbul is UTC+3)
  openSheet((s) => {
    const [[uf, u], [tf, t]] = [['url', 'Link (optional)'], ['datetime-local', `When${IST}`]].map(([type, x]) => {
      const f = el('div', 'field'), i = Object.assign(el('input'), { type, ariaLabel: x });
      f.append(el('label', null, x), i);
      return [f, i];
    }), err = el('div', 'field-err');
    const [tl, tb] = tickBox(`Yes, I ${done()} it by hand on ${L}`), ok = btn(`Mark as ${done()}`, 'primary', async () => {
      const r = await op('mark_posted', it, { permalink: u.value.trim(), at: t.value ? `${t.value}:00+03:00` : null }, 1);
      if (r?.status === 422) err.textContent = r.json.message;
    });
    t.value = new Date(Date.now() + 108e5).toISOString().slice(0, 16);
    ok.disabled = true;
    tb.onchange = () => { ok.disabled = !tb.checked; };
    s.title.textContent = `Mark as ${done()}`;
    s.body.append(el('p', null, `Only for a ${N} already on ${L} (${done()} by hand in the app). The PC will not ${VB()} it again.`), el('div', 'title', it.title || it.file),
      uf, tf, err, tl);
    s.foot.append(btn('Cancel', null, () => s.close()), ok);
  });
}

function copyCap(text, s) {
  const pick = () => {
    const t = s.body.querySelector('textarea') || s.body.appendChild(el('textarea', 'caption'));
    Object.assign(t, { readOnly: true, value: text });
    t.focus();
    t.select();
    toast('Press and hold to copy');
  };
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('Caption copied.'), pick);
  else pick();
}

function capSheet(it, pv) {
  openSheet((s) => {
    const lim = { ...LIM, ...pv.caption_limits }, cb = capBlock(it.id, pv.caption, pv.default_caption, lim, () => chk()), save = btn('Save caption', 'primary', async () => {
      const t = cb.ta.value, clear = t === pv.default_caption;
      if (t === pv.caption) return s.close();
      const r = await op(clear ? 'clear_caption' : 'caption', it, !clear && { caption: t }, 1);
      if (r?.status < 300) draft(it.id, null);
      if (r?.status === 422) cb.err.textContent = r.json.checks?.map((c) => c.text).join(' ') || r.json.message;
    }), chk = () => {
      const bl = capChecks(cb.ta.value, lim).filter((c) => c.level === 'block');
      cb.err.textContent = bl.map((c) => c.text).join(' ');
      save.disabled = !!bl.length;
    };
    s.title.textContent = 'Edit caption';
    s.body.append(...cb.nodes);
    s.foot.append(btn('Cancel', null, () => s.close()), save);
    chk();
  });
}

function postSheet(id) { // spec 9.4; S10: warns post only with the tick, which sends them as override
  const st = {}, lim = {}, s = openSheet((s) => {
    s.title.textContent = `${Vb()} ${P === 'youtube' ? 'to' : 'on'} ${L}`;
    s.body.append(el('p', 'loading', 'Loading…'));
  });
  let ta, rules, tL, tick, msg, send;
  ps = { id, job: (j) => j.id === st.jid && stages(j) };
  s.onClose = () => { ps = null; };
  if (live(D.job) && D.job.item === id) {
    st.jid = D.job.id;
    return stages(D.job);
  }
  api(`/api/preview?platform=${P}&id=${id}`).then(show, (e) => s.body.replaceChildren(el('p', 'err', e.message)));

  function show(pv) {
    const it = pv.item || {}, out = [], cb = capBlock(id, pv.caption, pv.default_caption, Object.assign(lim, LIM, pv.caption_limits), () => check());
    Object.assign(st, { pv, extra: [], stop: 0 });
    if (pic(it) && P !== 'instagram') out.push(cover(it, wide(it) ? 'cover-wide' : 'cover-lg'));
    else if (pic(it)) {
      const f = el('div', 'cover-916'), [l, b] = tickBox('Show the profile grid crop (3:4)');
      f.append(cover(it), el('div', 'crop'));
      b.onchange = () => f.classList.toggle('show-crop', b.checked);
      out.push(f, l);
    }
    [ta, [tL, tick]] = [cb.ta, tickBox(`I understand, ${VB()} anyway`)];
    tick.onchange = check;
    // F15: the rules first and the tick beside the button, so the reason the button is off is on the first screen
    s.body.replaceChildren((rules = el('div', 'rules')), (msg = el('p')), ...out, el('div', 'title', it.title || it.file), el('p', 'muted small', line(it)), ...cb.nodes);
    s.foot.replaceChildren(tL, btn('Cancel', null, () => s.close()), (send = btn(`${Vb()} ${P === 'youtube' ? 'to' : 'on'} ${L} now`, 'primary', fire)));
    check();
  }

  function check() {
    const list = [...st.extra, ...st.pv.checks || []].filter((c, i, a) => !CAP.test(c.code) && a.findIndex((x) => x.code === c.code) === i)
      .concat(capChecks(ta.value, lim)).sort((a, b) => (LV[a.level] ?? 2) - (LV[b.level] ?? 2)), blocks = list.filter((c) => c.level === 'block');
    st.warns = list.filter((c) => c.level === 'warn').map((c) => c.code);
    rules.replaceChildren(...list.map((c) => el('div', `rule ${c.level}`, c.text)));
    rules.hidden = !list.length;
    tL.hidden = !st.warns.length || blocks.some((c) => !CAP.test(c.code));
    send.disabled = !!(st.stop || st.busy || blocks.length || (st.warns.length && !tick.checked));
  }

  async function fire() {
    st.busy = 1;
    check();
    const r = await post('/api/post', { platform: P, id, caption: ta.value, save_caption: ta.value !== st.pv.caption, // F53
      check: st.pv.check, override: tick.checked ? st.warns : [] }), j = r.json, e = j.error;
    st.busy = 0;
    if (r.status === 202 && j.job) {
      st.jid = j.job.id;
      draft(id, null);
      return follow(j.job);
    }
    if (e === 'stale' && j.preview) show(j.preview);
    else if (/^(confirm|problems)$/.test(e)) { // the server's newer checks (and fingerprint): tick again
      [st.extra, st.pv.check, tick.checked] = [j.checks || [], j.check || st.pv.check, false];
    } else if ([404, 409, 423, 429].includes(r.status)) st.stop = 1;
    if (j.job) follow(j.job);
    msg.className = /^(stale|confirm)$/.test(e) ? 'note warn' : 'err';
    msg.textContent = e === 'stale' ? `Something changed since you opened this (a ${N} just went out). Check again.` : j.message || `Error ${r.status}`;
    if (https(j.url)) msg.append(' ', ext('Open', j.url, 'btn link'));
    check();
  }

  function stages(j) {
    const i = ST.indexOf(j.stage), on = live(j), ok = j.state === 'done', ol = el('ol', 'stages'), t = el('p', 'elapsed', on ? 'Running for ' : 'Took ');
    ol.append(...ST.map((k, n) => el('li', ok || n < i ? 'done' : n === i && (on ? 'now' : 'fail'), up1(k))));
    t.append(since(j));
    s.body.setAttribute('aria-live', 'polite');
    s.body.replaceChildren(el('div', 'title', j.title || stem(j.key)), ol, t, ...(on ? [el('p', 'job-line', j.result),
      gone ? el('p', 'err', LOST) : el('p', 'note', `${P === 'youtube' ? 'Takes a few minutes (the upload, then YouTube’s processing)' : 'Takes 1–3 minutes'}. You can close this; it keeps going on the PC.`)] : [result(j)]));
    s.foot.replaceChildren(btn(on ? 'Close' : 'Done', !on && 'primary', () => s.close()));
  }
}

function stepper(min, max, step, v, show, set) {
  const w = el('div', 'stepper'), o = el('output'), b = [-1, 1].map((k) => Object.assign(btn(k < 0 ? '−' : '+', null, () => {
    v = Math.min(max, Math.max(min, Math.round((v + k * step) / step) * step));
    paint();
    set(v);
  }), { ariaLabel: k < 0 ? 'Less' : 'More' }));
  const paint = () => {
    o.textContent = show(v);
    [b[0].disabled, b[1].disabled] = [v <= min, v >= max];
  };
  paint();
  w.append(b[0], o, b[1]);
  return w;
}

function settings(on) { // S14: the auto_on and gap_under_20 ticks; a dry run 400 ms after a change
  let cur = D.settings || {}, rev = D.settings_rev, f, fx = D.forecast, need = [], timer, n = 0, shut, gL, gB, oL, oB, warn, fc, pb = [];
  const errs = {}, save = btn('Save', 'primary', () => send()), s = openSheet((s) => {
    s.title.textContent = `Automatic ${VB()}ing: ${L}`;
    s.foot.append(btn('Cancel', null, () => s.close()), save);
  });
  const needOn = () => f.auto && (!D.auto.effective || need.includes('auto_on'));
  const fresh = () => { f = { ...cur, days: [...(cur.days || [])] }; };
  s.onClose = () => { shut = 1; clearTimeout(timer); };
  fresh();
  if (on) f.auto = true;
  build();

  function build() {
    const fld = (k, label, ...x) => {
      const w = el('div', 'field');
      w.append(el('label', null, label), ...x, (errs[k] = el('div', 'field-err')));
      return w;
    };
    const ch = (k, v) => { f[k] = v; changed(); }, press = (b, x) => (b.setAttribute('aria-pressed', x), b), [aL, aB] = tickBox(`${Vb()} automatically`);
    const pre = el('div', 'filters'), days = el('div', 'days'), win = el('div', 'inline'), sd = el('div', 'inline');
    pb = (D.presets || []).map((p) => [pre.appendChild(btn(p.label, null, () => { Object.assign(f, p.values); build(); })), p.values]);
    for (const d of DAYS) {
      const b = days.appendChild(press(btn(up1(d), null, () => { ch('days', DAYS.filter((x) => (x === d) !== f.days.includes(x))); press(b, f.days.includes(d)); }), f.days.includes(d)));
    }
    const [t1, t2, dt] = ['post_after', 'post_before', 'start_date'].map((k, i) => Object.assign(el('input'), { type: i > 1 ? 'date' : 'time',
      ariaLabel: ['From', 'Until', 'Start date'][i], value: f[k] || '', onchange: (e) => ch(k, e.target.value || (i > 1 ? null : '')) }));
    Object.assign(aB, { checked: !!f.auto, onchange: () => ch('auto', aB.checked) });
    win.append(t1, 'and', t2);
    sd.append(dt, btn('Clear', 'small', () => { dt.value = ''; ch('start_date', null); }));
    warn = el('p', 'note warn', `Less than 20 h between ${N}s can flood followers.`);
    [[gL, gB], [oL, oB]] = [tickBox(`Yes, ${VB()} ${N}s less than 20 h apart`), tickBox(`Start ${VB()}ing automatically`)];
    gB.onchange = oB.onchange = sync;
    s.body.replaceChildren(el('p', 'muted small', 'Each platform has its own settings.'), aL, (errs.auto = el('div', 'field-err')), pre, fld('every_days', `${Vb()} every`, stepper(1, 7, 1, f.every_days, (v) => many(v, 'day'), (v) => ch('every_days', v))),
      fld('per_day', `${up1(N)}s on such a day`, stepper(1, 5, 1, f.per_day ?? 1, (v) => `up to ${v}`, (v) => ch('per_day', v))),
      fld('days', 'On these days', days), fld('post_after', `${Vb()} between${IST}`, win, (errs.post_before = el('div', 'field-err'))),
      fld('min_gap_hours', `At least this long between ${N}s`, stepper(1, 168, 0.5, f.min_gap_hours, (v) => `${v} h`, (v) => ch('min_gap_hours', v)), warn), gL,
      el('p', 'note', `At most ${cur.hard_cap_24h} ${N}s in any 24 hours (change on the PC in publishing.json).`), fld('start_date', 'Start date', sd),
      fld('forecast', 'Forecast', (fc = el('div'))));
    s.foot.querySelector('.tick')?.remove();
    s.foot.prepend(oL);
    showFc(fx);
    changed();
  }

  function changed() {
    sync();
    clearTimeout(timer);
    timer = setTimeout(() => send(1), 400);
  }

  function sync() {
    const g = f.min_gap_hours < 20, ng = (g && f.min_gap_hours !== cur.min_gap_hours) || need.includes('gap_under_20'), no = needOn();
    [warn.hidden, gL.hidden, oL.hidden] = [!g, !ng, !no];
    for (const [b, v] of pb) b.setAttribute('aria-pressed', Object.entries(v).every(([k, x]) => same(f[k], x)));
    save.textContent = no ? 'Save and start' : 'Save';
    save.disabled = (no && !oB.checked) || (ng && !gB.checked);
    return [no, ng];
  }

  function showFc(x = {}) {
    const rows = x.rows || [], name = Object.fromEntries((D.queue.waiting || []).map((i) => [i.id, stem(i.key)])), ol = el('ol', 'forecast');
    fx = x;
    if (!rows.length) return fc.replaceChildren(el('p', 'note', D.counts.waiting ? `No ${N} could go out in the next 15 days with these settings.` : 'Nothing is waiting.'));
    ol.append(...rows.map((r) => el('li', null, `${fmt(r.at)} · ${name[r.id] || r.id}`)));
    fc.replaceChildren(el('p', 'note', `With these settings${x.assumes_on ? ` (if automatic ${VB()}ing were on)` : ''}${IST}:`), ol, x.runway ? el('p', 'note', `Enough until ${fmtDay(x.runway)}.`) : '', beyond(x) ? el('p', 'note', beyond(x)) : '');
  }

  async function send(dry) {
    const p = Object.fromEntries(KEYS.filter((k) => !same(f[k], cur[k])).map((k) => [k, f[k] ?? null])), [no, ng] = sync(), k = ++n;
    if (needOn()) p.auto = true;
    if (!dry && !Object.keys(p).length) return s.close();
    if (!dry) {
      save.disabled = true;
      clearTimeout(timer);
    }
    const r = await post('/api/settings', { platform: P, rev, settings: p, dry_run: !!dry,
      ack: dry ? [] : [no && oB.checked && 'auto_on', ng && gB.checked && 'gap_under_20'].filter(Boolean) }), j = r.json;
    if (shut || k !== n) return;
    if (j.error === 'stale' && j.settings) {
      toast('Someone else changed the settings; reloaded.');
      [cur, rev, need] = [j.settings, j.rev, []];
      fresh();
      return build();
    }
    Object.values(errs).forEach((e) => { e.textContent = ''; });
    for (const [x, m] of Object.entries(j.fields || {})) (errs[x] || errs.auto).textContent = m;
    if (j.forecast) showFc(j.forecast);
    else if (dry) fc.replaceChildren(el('p', 'err', j.fields ? 'Fix the marked fields to see the forecast.' : j.message || `Error ${r.status}`));
    need = j.needs_ack || need;
    if (!dry) toast(j.message || `Error ${r.status}`);
    if (!dry && r.status === 200) return reload();
    sync();
  }
}
