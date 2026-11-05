// Dashboard UI smoke test (spec 11, addendum C6). Run it only against the dev server, which needs no login and posts
// through a fake platform:
//   set PIPELINE_WORK=<a scratch folder>
//   D:\YT\work\venv\Scripts\python.exe -I pipeline\dashboard.py serve --dev --fixtures --port 8791
//   node pipeline\tests\smoke_ui.cjs
// Env: DASH_URL (default http://127.0.0.1:8791), SMOKE_SHOTS (screenshots; default <temp>\dashboard-smoke),
// PLAYWRIGHT_DIR (default <home>\node_modules\playwright).
// At 412x915 it loads every tab, posts the first waiting Reel from the post sheet and waits for "Posted.", then opens
// the settings sheet and waits for the dry-run forecast. At 1280x800 it loads every tab and opens (and cancels) the post
// and settings sheets; it does not post again (manual posts are limited to one per 2 minutes).
// First, at 360x760, the regression checks for the review findings (REGRESS below): every POST in them is answered by
// the test itself and never reaches the server. SMOKE_ONLY=all (or F4,F13,...) runs only those, at SMOKE_W x SMOKE_H
// (default 412x915), or on WebKit iPhone 13 with SMOKE_WEBKIT=1.
// Every page: no console errors, no horizontal scroll. Nothing leaves 127.0.0.1: Google Fonts and YouTube thumbnails are
// answered here with empty stand-ins; any other request is blocked and fails the run. The only changes it may make are
// that one post and settings dry runs (checked at the end).
// Safety: it refuses an address that is not loopback, port 8790 (the live dashboard), and any server that does not say
// it is the dev server (me: dev@localhost), so it can never post on the real account.
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require(process.env.PLAYWRIGHT_DIR || path.join(os.homedir(), 'node_modules', 'playwright'));

const BASE = new URL(process.env.DASH_URL || 'http://127.0.0.1:8791');
const ROOT = BASE.origin;
const SHOTS = process.env.SMOKE_SHOTS || path.join(os.tmpdir(), 'dashboard-smoke');
const TABS = { home: '/api/home', publish: '/api/publish', videos: '/api/videos', stats: '/api/stats', activity: '/api/activity' };
const PUB = 'section.view[data-view="publish"]';
const DLG = 'dialog[open]';
const THUMB = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#101628"/></svg>';
const results = [];
const offsite = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function check(name, ok, info = '') {
  results.push({ name, ok: !!ok });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !info ? '' : ` :: ${String(info).slice(0, 400)}`}`);
  return !!ok;
}

function refuse(why) {
  console.error(`smoke_ui.cjs refuses to run: ${why}`);
  process.exit(2);
}

async function safe() {
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(BASE.hostname)) refuse(`${ROOT} is not a loopback address.`);
  if (BASE.port === '8790') refuse('port 8790 is the live dashboard.');
  let me;
  try {
    me = (await (await fetch(`${ROOT}/api/home`)).json()).me;
  } catch (e) {
    refuse(`no dev server answers at ${ROOT} (${e.message}). Start one: dashboard.py serve --dev --fixtures --port 8791`);
  }
  if (me !== 'dev@localhost') refuse(`${ROOT} is not a --dev server (it answers as ${me || 'nobody'}).`);
}

async function until(page, fn, ms, arg) { // page.waitForFunction needs eval, which the page's CSP refuses
  for (const end = Date.now() + ms; Date.now() < end; await sleep(150)) {
    if (await page.evaluate(fn, arg).catch(() => false)) return true;
  }
  return false;
}

async function context(browser, vp) {
  const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, isMobile: vp.mobile, hasTouch: vp.mobile });
  await ctx.route('**/*', (route) => {
    const u = new URL(route.request().url());
    if (u.origin === ROOT) return route.continue();
    if (u.hostname === 'fonts.googleapis.com') return route.fulfill({ status: 200, contentType: 'text/css', body: '' });
    if (u.hostname === 'i.ytimg.com') return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: THUMB });
    offsite.push(u.href);
    return route.abort();
  });
  const page = await ctx.newPage();
  Object.assign(page, { errors: [], posts: [] });
  page.on('console', (m) => { if (m.type() === 'error') page.errors.push(`${m.text()} @ ${(m.location() || {}).url || ''}`); });
  page.on('pageerror', (e) => page.errors.push(`page error: ${e.message}`));
  page.on('request', (q) => { if (q.method() === 'POST') page.posts.push([new URL(q.url()).pathname, q.postData() || '']); });
  return [ctx, page];
}

async function shot(page, vp, name) {
  fs.mkdirSync(SHOTS, { recursive: true });
  const sheet = (await page.locator(DLG).count()) > 0;
  if (sheet) await sleep(350); // the sheet slides in
  await page.screenshot({ path: path.join(SHOTS, `${vp.name}-${name}.png`), fullPage: !sheet });
}

function noErrors(page, label) {
  check(`${label}: no console errors`, !page.errors.length, page.errors.join(' | '));
  page.errors.length = 0;
}

async function noHScroll(page, label) {
  const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
  check(`${label}: no horizontal scroll`, sw <= iw, `scrollWidth ${sw} > innerWidth ${iw}`);
}

async function loadTab(page, vp, name) { // navigate, then wait for the tab's API answer and its render
  const label = `${vp.name} #${name}`;
  const answer = page.waitForResponse((r) => new URL(r.url()).pathname === TABS[name], { timeout: 20000 }).catch(() => null);
  await page.goto(`${ROOT}/#${name}`);
  const r = await answer;
  const shown = await until(page, (t) => {
    const v = document.querySelector(`section.view[data-view="${t}"]`);
    return !!v && !v.hidden && !!v.firstElementChild && !v.querySelector(':scope > .loading');
  }, 20000, name);
  const [state, text] = await page.evaluate((t) => [document.getElementById('state').innerText.trim(),
    (document.querySelector(`section.view[data-view="${t}"]`) || {}).innerText || ''], name);
  check(`${label}: loads`, r && r.status() === 200 && shown && !state && !/not ready yet/.test(text),
    `${r ? r.status() : 'no answer'} ${state || text.slice(0, 160)}`);
  await noHScroll(page, label);
  noErrors(page, label);
  await shot(page, vp, name);
}

async function openPostSheet(page, vp) { // the first waiting Reel's [Post]; false when it cannot open
  const label = `${vp.name} post sheet`;
  const card = page.locator(`${PUB} [data-section="waiting"] .qcard`).first();
  if (!check(`${label}: a Reel is waiting`, await card.count(), 'nothing waiting: restart the dev server for fresh fixtures')) return false;
  await card.locator('button', { hasText: 'Post' }).click();
  const open = await until(page, () => !!document.querySelector('dialog[open] .rules'), 15000);
  if (!check(`${label}: opens on its own address`, open && /#publish\/[a-z]+\/post\/[0-9a-f]{12}$/.test(page.url()), page.url())) return false;
  const blocks = await page.locator(`${DLG} .rule.block`).allInnerTexts();
  check(`${label}: no rule blocks the post`, !blocks.length, `${blocks.join(' ')} (restart the dev server for fresh fixtures)`);
  return !blocks.length;
}

async function post(page, vp) { // phone pass only: the one post of the run
  const label = `${vp.name} post`;
  await loadTab(page, vp, 'publish');
  if (!(await openPostSheet(page, vp))) return;
  const tick = page.locator(`${DLG} label.tick`, { hasText: 'I understand, post anyway' });
  if (await tick.isVisible()) await tick.click();                 // only when the rules warn
  const send = page.locator(`${DLG} .sheet-foot button.primary`);
  if (!check(`${label}: the post button is enabled`, await send.isEnabled())) return;
  await shot(page, vp, 'post-sheet');
  const answer = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/post', { timeout: 15000 }).catch(() => null);
  await send.click();
  const r = await answer;
  if (!check(`${label}: 202 with a job`, r && r.status() === 202, r ? `${r.status()} ${await r.text()}` : 'no answer')) return;
  check(`${label}: live stages`, await until(page, () => !!document.querySelector('dialog[open] ol.stages'), 10000));
  await until(page, () => !!document.querySelector('dialog[open] .result'), 120000);
  const res = await page.locator(`${DLG} .result`).first().innerText().catch(() => 'no result after 2 minutes');
  check(`${label}: "Posted."`, /^Posted\./.test(res), res);
  await shot(page, vp, 'posted');
  await page.locator(`${DLG} .sheet-foot button`).last().click();  // Done
  check(`${label}: Done closes the sheet`, await until(page, () => !document.querySelector('dialog[open]'), 5000));
  noErrors(page, label);
}

async function peek(page, vp) { // wide pass: open the post sheet, then Cancel
  const label = `${vp.name} post sheet`;
  await loadTab(page, vp, 'publish');
  if (await openPostSheet(page, vp)) await shot(page, vp, 'post-sheet');
  await page.locator(`${DLG} .sheet-foot button`, { hasText: 'Cancel' }).click().catch(() => {});
  check(`${label}: Cancel closes it`, await until(page, () => !document.querySelector('dialog[open]'), 5000));
  noErrors(page, label);
}

async function settings(page, vp) { // the dry run 400 ms after the sheet opens; Cancel saves nothing
  const label = `${vp.name} settings`;
  const dry = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/settings' && /"dry_run":\s*true/.test(r.request().postData() || ''),
    { timeout: 15000 }).catch(() => null);
  await page.locator(`${PUB} .panel button`, { hasText: 'Settings' }).first().click();
  const r = await dry;
  check(`${label}: dry run answered`, r && r.status() === 200, r ? `${r.status()} ${(await r.text()).slice(0, 200)}` : 'no dry run within 15 s');
  const shown = await until(page, () => {
    const f = [...document.querySelectorAll('dialog[open] .field')].find((x) => /^Forecast/.test(x.innerText));
    return !!f && !f.querySelector('.err') && (!!f.querySelector('ol.forecast li') || /could go out|Nothing is waiting/.test(f.innerText));
  }, 5000);
  check(`${label}: the forecast shows`, shown);
  await shot(page, vp, 'settings');
  await page.locator(`${DLG} .sheet-foot button`, { hasText: 'Cancel' }).click();
  check(`${label}: Cancel closes it`, await until(page, () => !document.querySelector('dialog[open]'), 5000));
  noErrors(page, label);
}

// Regression checks for the review findings (F4, F5, F12-F16, F38, F44, F46-F49, F54, G25, G26, G28). In these pages
// every POST is answered here and never reaches the server; GET answers may be changed (fx), replaced (raw), delayed
// (slow) or failed (dead) here.
const SHEETS = () => [...document.querySelectorAll('dialog[open]')].map((d) => {
  const t = d.querySelector('.sheet-body .title');
  return `${d.querySelector('.sheet-title').innerText}|${t ? t.innerText : ''}`;
});

async function rpage(browser, dev, o = {}) {
  const ctx = await browser.newContext(dev);
  const page = await ctx.newPage();
  Object.assign(page, { errors: [], posts: [], gets: [], o: { fx: {}, raw: {}, slow: {}, dead: {}, ...o } });
  page.on('pageerror', (e) => page.errors.push(`page error: ${e.message}`));
  await ctx.route('**/*', async (route) => {
    const q = route.request(), u = new URL(q.url()), O = page.o;
    if (u.origin !== ROOT) {
      if (u.hostname === 'fonts.googleapis.com') return route.fulfill({ status: 200, contentType: 'text/css', body: '' });
      if (u.hostname === 'i.ytimg.com') return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: THUMB });
      offsite.push(u.href);
      return route.abort();
    }
    const json = (s, j) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(j) });
    if (q.method() !== 'GET') {
      const body = JSON.parse(q.postData() || '{}');
      page.posts.push([u.pathname, body]);
      const a = (O.answer && (await O.answer(u.pathname, body))) || [200, { ok: true, message: 'Done.' }];
      if (a[2]) await sleep(a[2]);
      return json(a[0], a[1]).catch(() => {});
    }
    page.gets.push([Date.now(), u.pathname]);
    const p = u.pathname, raw = O.raw[p] || (p.startsWith('/api/') && O.raw['/api/*']);
    if (O.slow[p]) await sleep(O.slow[p]);
    if (O.dead[p]) return route.abort().catch(() => {});
    if (raw) return json(...raw(u)).catch(() => {});
    if (!O.fx[p]) return route.continue().catch(() => {});
    const r = await route.fetch(), j = await r.json();
    return json(r.status(), O.fx[p](j, u) || j).catch(() => {});
  });
  return [ctx, page];
}

async function at(page, hash, tab = hash.slice(1).split('/')[0]) {
  await page.goto(`${ROOT}/${hash}`);
  return until(page, (t) => {
    const v = document.querySelector(`section.view[data-view="${t}"]`);
    return !!v && !v.hidden && !!v.firstElementChild && !v.querySelector(':scope > .loading');
  }, 20000, tab);
}

const waitingCard = (page, n) => page.locator(`${PUB} [data-section="waiting"] .qcard`).nth(n);
const sheetCount = (page) => page.locator(DLG).count();
const inView = (page, sel, re = '') => page.evaluate(([s, t]) => { // fully inside the window without scrolling the sheet
  const e = [...document.querySelectorAll(s)].find((x) => x.offsetParent && new RegExp(t).test(x.innerText)), r = e && e.getBoundingClientRect();
  return !!r && r.top >= 0 && r.bottom <= innerHeight;
}, [sel, re]);
const tapTwice = async (page, loc) => { // a real double tap at the button's spot
  await loc.scrollIntoViewIfNeeded();
  const b = await loc.boundingBox();
  await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
  await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
};
const openItem = async (page, n = 0) => {
  await waitingCard(page, n).locator('.qbody').click();
  return until(page, () => [...document.querySelectorAll('dialog[open] button')].some((b) => /Edit caption/.test(b.innerText) && !b.disabled), 10000);
};
const sheetButton = (page, text) => page.locator(`${DLG} button`, { hasText: text }).last();
const hasText = (page, sel, re) => until(page, ([s, r]) => [...document.querySelectorAll(s)].some((e) => new RegExp(r).test(e.innerText)), 6000, [sel, re]);

const REGRESS = {
  async F4(page) { // a slow PC: the post sheet must never open for a route the user already left
    await at(page, '#publish/instagram');
    const second = await waitingCard(page, 1).locator('.title').innerText();
    page.o.slow['/api/publish'] = 2000;
    await waitingCard(page, 0).locator('button', { hasText: 'Post' }).click();
    await sleep(300);
    await page.goBack();
    await sleep(3500);
    check('F4 Back during a slow load: no sheet opens afterwards', !(await sheetCount(page)), JSON.stringify(await page.evaluate(SHEETS)));
    await waitingCard(page, 0).locator('button', { hasText: 'Post' }).click();
    await sleep(300);
    await page.locator('nav.tabs a[data-tab="home"]').click();
    await sleep(4000);
    check('F4 another tab during a slow load: no sheet', !(await sheetCount(page)), JSON.stringify(await page.evaluate(SHEETS)));
    await page.locator('nav.tabs a[data-tab="publish"]').click();
    await sleep(3000);
    await waitingCard(page, 0).locator('button', { hasText: 'Post' }).click();
    await sleep(300);
    await page.goBack();
    await sleep(300);
    await waitingCard(page, 1).locator('button', { hasText: 'Post' }).click();
    await sleep(6000);
    const s = await page.evaluate(SHEETS);
    check('F4 Post on Reel 1, Back, Post on Reel 2: only Reel 2\'s sheet', s.length === 1 && s[0].endsWith(`|${second}`), JSON.stringify(s));
    await page.locator(`${DLG} .sheet-foot button`, { hasText: 'Cancel' }).last().click().catch(() => {});
    await sleep(1500);
    check('F4 ... and Cancel leaves no sheet', !(await sheetCount(page)), JSON.stringify(await page.evaluate(SHEETS)));
  },
  async F13(page) { // a double tap sends one POST; Test connection shows the real wait (F14)
    page.o.fx['/api/publish'] = (j) => { j.breaker = { name: 'meta', state: 'open', kind: 'auth', message: 'The key was refused (code 190).' }; };
    page.o.answer = (p, b) => (p === '/api/queue' ? [200, { ok: true, message: 'Moved to the top.' }, 700]
      : p === '/api/platform/test' ? (page.posts.filter((x) => x[0] === p).length < 2
        ? [502, { error: 'test_failed', message: 'Instagram refused the key (code 190).' }, 700]
        : [429, { error: 'rate_limited', message: 'Wait a moment before testing again.', retry_after: 420 }]) : null);
    await at(page, '#publish/instagram');
    await openItem(page);
    await tapTwice(page, sheetButton(page, 'Move to top'));
    await sleep(1500);
    check('F13 double tap on Move to top sends one POST', page.posts.filter((x) => x[0] === '/api/queue').length === 1, JSON.stringify(page.posts));
    await until(page, () => !document.querySelector('dialog[open]'), 5000);
    const test = () => page.locator(`${PUB} button`, { hasText: /^Test/ });
    await tapTwice(page, test());
    await sleep(1500);
    check('F13 double tap on Test connection sends one POST', page.posts.filter((x) => x[0] === '/api/platform/test').length === 1, JSON.stringify(page.posts));
    await sleep(1500);
    await test().click();
    check('F14 the cooldown toast says when', await hasText(page, '.toast', 'at \\d\\d:\\d\\d'), await page.locator('.toast').allInnerTexts());
    await sleep(2000);
    check('F14 Test connection waits until then', await test().isDisabled() && /\d\d:\d\d/.test(await test().innerText()), await test().innerText());
  },
  async F12(page) { // a stale answer keeps the typed caption; Save again uses the new rev
    page.o.answer = (p, b) => (p === '/api/queue' && b.rev !== 99
      ? [409, { error: 'stale', message: 'The queue changed on another phone. Here is the new one.', queue: { rev: 99 } }] : null);
    await at(page, '#publish/instagram');
    await openItem(page);
    await sheetButton(page, 'Edit caption').click();
    const ta = page.locator(`${DLG} textarea`).last();
    await ta.focus();
    await page.keyboard.press('End');
    await ta.press('Control+End');
    await ta.type(' F12-extra');
    await sheetButton(page, 'Save caption').click();
    await sleep(1200);
    const kept = (await page.locator(`${DLG} textarea`).last().inputValue().catch(() => '')).includes('F12-extra');
    check('F12 a 409 stale keeps the caption sheet and the text', (await sheetCount(page)) === 2 && kept, JSON.stringify(await page.evaluate(SHEETS)));
    await sheetButton(page, 'Save caption').click().catch(() => {});
    await sleep(1200);
    const q = page.posts.filter((x) => x[0] === '/api/queue');
    check('F12 Save again sends the new rev', q.length === 2 && q[1][1].rev === 99 && q[1][1].caption.includes('F12-extra'), JSON.stringify(q));
  },
  async F46(page) { // an edited caption survives Escape, X and Back
    await at(page, '#publish/instagram');
    for (const how of ['Escape', 'Back', 'X']) {
      await waitingCard(page, 0).locator('button', { hasText: 'Post' }).click();
      await until(page, () => !!document.querySelector('dialog[open] textarea'), 10000);
      const ta = page.locator(`${DLG} textarea`).last();
      await ta.press('Control+End');
      await ta.type(` F46-${how}`);
      if (how === 'Escape') await page.keyboard.press('Escape');
      else if (how === 'Back') await page.goBack();
      else await page.locator(`${DLG} .sheet-head button`).click();
      await until(page, () => !document.querySelector('dialog[open]'), 5000);
      await sleep(800);
      await waitingCard(page, 0).locator('button', { hasText: 'Post' }).click();
      await until(page, () => !!document.querySelector('dialog[open] textarea'), 10000);
      const v = await page.locator(`${DLG} textarea`).last().inputValue();
      check(`F46 after ${how} the edited caption comes back`, v.includes(`F46-${how}`), v.slice(-60));
      await page.locator(`${DLG} .sheet-foot button`, { hasText: 'Cancel' }).click();
      await until(page, () => !document.querySelector('dialog[open]'), 5000);
      await sleep(800);
    }
  },
  async F15(page) { // the reason a main button is off is on the first screen
    await at(page, '#publish/instagram');
    await waitingCard(page, 0).locator('button', { hasText: 'Post' }).click();
    await until(page, () => !!document.querySelector('dialog[open] .rules'), 10000);
    await sleep(400);
    // the fixture's warnings depend on the time of day: with none, there is no rule box and the button is on
    const warned = await page.evaluate(() => !document.querySelector('dialog[open] .rules').hidden);
    check('F15 post sheet: the "I understand" tick is on screen', !warned ||
      await inView(page, 'dialog[open] label.tick', 'I understand'));
    check(warned ? 'F15 post sheet: the rule box is on screen' : 'F15 post sheet: no warning, so the post button is on',
      warned ? await inView(page, 'dialog[open] .rules') : await page.evaluate(() =>
        ![...document.querySelectorAll('dialog[open] .sheet-foot button')].find((b) => /Post on/.test(b.textContent)).disabled));
    await page.locator(`${DLG} .sheet-foot button`, { hasText: 'Cancel' }).click();
    await until(page, () => !document.querySelector('dialog[open]'), 5000);
    page.o.fx['/api/publish'] = (j) => { j.settings.auto = false; Object.assign(j.auto, { setting: false, effective: false, reason: 'off' }); };
    await page.locator('nav.tabs a[data-tab="publish"]').click();
    await sleep(1500);
    await page.locator(`${PUB} .panel button`, { hasText: 'Turn on' }).click();
    await sleep(1200);
    check('F15 settings: the "Start posting automatically" tick is on screen', await inView(page, 'dialog[open] label.tick', 'Start posting'));
    check('F38 settings: the window and forecast say Istanbul time', await hasText(page, 'dialog[open] .sheet-body', 'Istanbul time'));
  },
  async F47(page) { // the preset chips follow the form
    await at(page, '#publish/instagram');
    await page.locator(`${PUB} .panel button`, { hasText: 'Settings' }).first().click();
    await sleep(800);
    await page.locator(`${DLG} .stepper button[aria-label="More"]`).first().click();
    await sleep(300);
    const pressed = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('dialog[open] .filters button')].map((b) => [b.innerText, b.getAttribute('aria-pressed')])));
    check('F47 after + on "Post every": "Every other day" lit, "Every evening" not', pressed['Every other day'] === 'true' && pressed['Every evening'] === 'false', JSON.stringify(pressed));
  },
  async F5(page) { // Mark as posted: confirm tick, optional link, a time
    await at(page, '#publish/instagram');
    await openItem(page);
    await sheetButton(page, 'Mark as posted').click();
    await sleep(500);
    const ok = page.locator(`${DLG} .sheet-foot button.primary`).last();
    check('F5 Mark as posted asks first (button off until the tick)', await ok.isDisabled());
    await page.locator(`${DLG} input[type=url]`).fill('https://www.instagram.com/reel/FAKE9/');
    check('F5 the time field says Istanbul time', await hasText(page, 'dialog[open] .sheet-body', 'Istanbul'));
    await page.locator(`${DLG} label.tick`).last().click();
    await ok.click();
    await sleep(1200);
    const q = page.posts.filter((x) => x[0] === '/api/queue').map((x) => x[1]);
    const t = q[0] && Date.parse(q[0].at);
    check('F5 sends mark_posted with the link and a time', q.length === 1 && q[0].op === 'mark_posted' && q[0].permalink === 'https://www.instagram.com/reel/FAKE9/'
      && Math.abs(Date.now() - t) < 5 * 60e3 && typeof q[0].rev === 'number', JSON.stringify(q));
  },
  async F48(page) { // the PC goes away mid-post: say so, stop counting, poll less
    const t0 = new Date(Date.now() - 50e3).toISOString();
    page.o.fx['/api/publish'] = (j) => { j.job = { id: 'FAKEJOB00001', state: 'running', stage: 'processing', key: 'new/Output/new-short-6.mp4', item: j.queue.waiting[0].id,
      created: t0, started: t0, result: 'Instagram is processing the video…' }; };
    page.o.dead['/api/job'] = 1;
    await at(page, '#publish/instagram');
    const t1 = Date.now();
    await sleep(21000);
    const n = page.gets.filter(([t, p]) => p === '/api/job' && t >= t1).length;
    check('F48 polls less while the PC cannot be reached', n <= 4, `${n} job polls in 21 s`);
    check('F48 says contact was lost', await hasText(page, `${PUB} .job`, 'Lost contact'));
    const e1 = await page.locator(`${PUB} .job .elapsed`).innerText();
    await sleep(2100);
    check('F48 the elapsed time stops', e1 === await page.locator(`${PUB} .job .elapsed`).innerText(), e1);
  },
  async F54(page) { // a cut posted list says so
    page.o.fx['/api/publish'] = (j) => {
      const p = j.queue.posted;
      j.queue.posted = Array.from({ length: 10 }, (_, i) => ({ ...p[i % p.length], id: `${String(i).padStart(2, '0')}aaaaaaaaaa` }));
      j.queue.posted_total = 14;
    };
    await at(page, '#publish/instagram');
    const b = page.locator(`${PUB} [data-section="posted"] button`);
    const label = await b.innerText().catch(() => '');
    await b.click().catch(() => {});
    check('F54 the posted list says it shows the latest 10 of 14', /10 of 14/.test(label) && await hasText(page, `${PUB} [data-section="posted"]`, 'Activity'), label);
  },
  async A1(page) { // the posting-core follow-ups: a cooling breaker, the unknown-job card, F53, the duplicate warning
    const t0 = new Date(Date.now() - 30 * 60e3).toISOString(), upto = new Date(Date.now() + 2 * 3600e3).toISOString();
    page.o.fx['/api/publish'] = (j) => {
      j.breaker = { name: 'meta', state: 'cooling', until: upto, kind: 'rate', message: 'Instagram asked for a pause (too many calls).' };
      j.unresolved = [{ id: 'j0123456789', state: 'unknown', key: 'new/Output/new-short-6.mp4', created: t0, started: t0 }];
    };
    page.o.fx['/api/preview'] = (j) => {
      Object.assign(j, { caption: 'A1 saved caption', default_caption: 'A1 default caption' });
      j.checks = (j.checks || []).concat({ code: 'duplicate', level: 'warn', text: 'A Reel with this caption is already on Instagram (Mon 2 Nov 20:05).' });
    };
    page.o.answer = (p) => (p === '/api/job/resolve' ? [409, { error: 'check_first', message: 'Press Check again first: the PC has not asked Instagram about this upload yet.' }]
      : p === '/api/post' ? [503, { error: 'busy', message: 'A1 example answer.' }] : null);
    await at(page, '#publish/instagram');
    const v = await page.locator(PUB).innerText();
    check('A1 cooling: no Test button, says when posting starts again', !(await page.locator(`${PUB} button`, { hasText: /^Test/ }).count()) && /Posting starts again after \w{3} \d/.test(v), v.slice(0, 300));
    const row = await page.locator(`${PUB} .edge-red button`).allInnerTexts(), first = page.locator(`${PUB} .edge-red button`).first();
    check('A1 unknown job: Check again first (primary), and the PC checks by itself', /^Check again/.test(row[0]) && /primary/.test(await first.getAttribute('class')) && /checks by itself/.test(v), JSON.stringify(row));
    await page.locator(`${PUB} .edge-red button`, { hasText: 'It is not on' }).click();
    await page.locator(`${DLG} .sheet-foot button.primary`).click();
    check('A1 a 409 check_first shows its message', await hasText(page, '.toast', 'Press Check again first'), await page.locator('.toast').allInnerTexts());
    await sleep(1500);
    await waitingCard(page, 0).locator('button', { hasText: 'Post' }).click();
    await until(page, () => !!document.querySelector('dialog[open] textarea'), 10000);
    check('A1 the duplicate warning shows', await hasText(page, 'dialog[open] .rule.warn', 'already on Instagram'));
    await page.locator(`${DLG} label.tick`, { hasText: 'I understand' }).click();
    const send = page.locator(`${DLG} .sheet-foot button.primary`);
    await send.click();
    await sleep(800);
    await page.locator(`${DLG} textarea`).last().type(' edited');
    await send.click();
    await sleep(800);
    const q = page.posts.filter((x) => x[0] === '/api/post').map((x) => x[1]);
    check('F53 save_caption only when the text differs from the saved caption', q.length === 2 && q[0].save_caption === false && q[1].save_caption === true
      && q.every((b) => b.override.includes('duplicate')), JSON.stringify(q));
  },
  async E3(page) { // F35 no card dates while posting is suspended; F37 the Reels past the list; G27 Check again waits
    const t0 = new Date(Date.now() - 30 * 60e3).toISOString();
    page.o.fx['/api/publish'] = (j) => {
      const W = j.queue.waiting, at = new Date(Date.now() + 864e5).toISOString();
      W.forEach((i) => { i.eta = at; });
      Object.assign(j.auto, { setting: true, effective: true, reason: 'suspended', suspended: { since: t0, why: 'E3 example.' } });
      Object.assign(j.forecast, { assumes_on: true, runway_text: 'Enough Reels until at least Fri 20 Nov', more: W.length - 2,
        rows: W.slice(0, 2).map((i) => ({ id: i.id, at, slot: at.slice(0, 10) })) });
      j.unresolved = [{ id: 'j0123456789', state: 'interrupted', key: 'new/Output/new-short-6.mp4', created: t0, started: t0 }];
    };
    page.o.answer = (p) => (p === '/api/job/resolve' ? [429, { error: 'rate_limited', message: 'Wait a moment before checking again.', retry_after: 360 }] : null);
    await at(page, '#publish/instagram');
    const v = await page.locator(PUB).innerText(), meta = await page.locator(`${PUB} [data-section="waiting"] .qcard .meta`).allInnerTexts();
    check('F35 suspended: no dates on the waiting cards', meta.length > 2 && meta.every((m) => !/\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d/.test(m)), JSON.stringify(meta));
    check('F35 the runway says it waits for automatic posting', /at least Fri 20 Nov \(once automatic posting runs again\)/.test(v), v.slice(0, 600));
    check('F37 the Reels past the list are counted', /\+\d+ more Reels after \w{3} \d/.test(v), v.slice(0, 600));
    const chk = () => page.locator(`${PUB} .edge-red button`).first();
    await chk().click();
    await page.locator(`${DLG} .sheet-foot button.primary`).click();
    check('G27 a 429 on Check again says when', await hasText(page, '.toast', 'try again at \\d\\d:\\d\\d'), await page.locator('.toast').allInnerTexts());
    await sleep(2000);
    check('G27 Check again waits until then', await chk().isDisabled() && /^Check again at \d\d:\d\d/.test(await chk().innerText()), await chk().innerText());
  },
  async stats(page) { // F16, F44, F49, G26
    page.o.fx['/api/stats'] = (j) => {
      j.tiles[0] = { ...j.tiles[0], d24: null, d7: null, stale: true, at: new Date(Date.now() - 3 * 864e5).toISOString() };
      j.tiles[1] = { ...j.tiles[1], d24: 2, d7: 5 };
      const v = j.youtube_videos[0] || { id: 'FAKEVID0001', title: 'Video' };
      j.youtube_videos = Array.from({ length: 60 }, (_, i) => ({ ...v, id: `FAKEVID${i}`, title: `Video ${i} with a long title`, views: 1234567 - i, likes: 12345, comments: 1234, d24_views: 12345 }));
    };
    await at(page, '#stats');
    const txt = await page.locator('section.view[data-view="stats"]').innerText();
    check('F16 a stale tile says "as of"', /as of/.test(txt));
    check('F44 the 24 h change is not called "today"', /\+2 in 24 h/.test(txt) && !/today/.test(txt), txt.slice(0, 300));
    const rows = await page.locator('section.view[data-view="stats"] .tbl-wrap').first().locator('tbody tr').count();
    check('G26 the YouTube table shows 20 rows and Show all', rows === 20 && /Show all \(60\)/.test(txt), `${rows} rows`);
    const order = await page.evaluate(() => {
      const v = document.querySelector('section.view[data-view="stats"]'), t = v.querySelector('.tbl-wrap'), l = [...v.querySelectorAll('.note')].find((n) => /API posts/.test(n.innerText));
      return !!t && !!l && l.getBoundingClientRect().top < t.getBoundingClientRect().top;
    });
    check('G26 the API limit line comes before the tables', order);
    const fit = await page.evaluate(() => { const w = document.querySelector('section.view[data-view="stats"] .tbl-wrap'); w.scrollLeft = 40; w.dataset.old = 1; return [w.scrollWidth, w.clientWidth, w.scrollLeft]; });
    await page.locator('nav.tabs a[data-tab="stats"]').click();
    await until(page, () => !document.querySelector('section.view[data-view="stats"] .tbl-wrap[data-old]'), 8000);
    await sleep(300);
    const left = await page.evaluate(() => document.querySelector('section.view[data-view="stats"] .tbl-wrap').scrollLeft);
    check('F49 a refresh keeps the table\'s sideways scroll', fit[2] === 0 || left === fit[2], `${JSON.stringify(fit)} -> ${left}`);
    page.o.fx['/api/stats'] = null;
    await page.locator('nav.tabs a[data-tab="stats"]').click();
    await sleep(1500);
    const w = await page.evaluate(() => [...document.querySelectorAll('section.view[data-view="stats"] .tbl-wrap')].map((x) => x.scrollWidth - x.clientWidth));
    check('F49 the fixture tables fit without a sideways swipe', w.every((x) => x <= 0), JSON.stringify(w));
  },
  async G25(page) { // a poll after Load more never leaves a hole
    let N = 120;
    page.o.raw['/api/activity'] = (u) => {
      const before = Number(u.searchParams.get('before')) || N + 1, ids = [];
      for (let i = Math.min(N, before - 1); i > 0 && ids.length < 50; i--) ids.push(i);
      return [200, { items: ids.map((id) => ({ id, at: new Date().toISOString(), who: 'pc', kind: 'post.done', text: `row ${id}` })),
        more: ids.length > 0 && ids[ids.length - 1] > 1, logs: {} }];
    };
    await at(page, '#activity');
    await page.locator('section.view[data-view="activity"] button', { hasText: 'Load more' }).click();
    await sleep(800);
    N = 180;
    await page.evaluate(() => { const n = Date.now; Date.now = () => n() + 70e3; document.dispatchEvent(new Event('visibilitychange')); });
    await until(page, () => /row 180/.test(document.querySelector('section.view[data-view="activity"]').innerText), 8000);
    const ids = await page.evaluate(() => [...document.querySelectorAll('section.view[data-view="activity"] .act-x')].map((e) => Number(e.innerText.replace(/\D+/g, ''))));
    const holes = ids.slice(1).filter((x, i) => x !== ids[i] - 1).length;
    check('G25 the activity list has no hole after a poll', ids[0] === 180 && !holes, `${ids.length} rows, ${holes} holes`);
  },
  async G28(page) { // after a rollback: ask for a reload, not "Could not reach the PC"
    await at(page, '#home');
    page.o.raw['/api/*'] = () => [404, { error: 'Not found.' }];
    page.o.raw['/activity.js'] = () => [404, { error: 'Not found.' }];
    await page.locator('nav.tabs a[data-tab="home"]').click();
    check('G28 an old server: "The dashboard was updated." with Reload', await hasText(page, '#state', 'dashboard was updated'), await page.locator('#state').innerText());
    page.o.raw['/api/*'] = null;
    await page.locator('nav.tabs a[data-tab="activity"]').click();
    check('G28 a tab that cannot load asks for a reload', await hasText(page, '#state', 'Reload'), await page.locator('#state').innerText());
  },
};

async function regress(type, dev, only) {
  const browser = await type.launch();
  try {
    for (const [name, fn] of Object.entries(REGRESS)) {
      if (only && !only.includes(name)) continue;
      const [ctx, page] = await rpage(browser, dev);
      try {
        await fn(page);
      } catch (e) {
        check(`${name}: ran to the end`, false, e.message.split('\n')[0]);
      }
      if (process.env.SMOKE_SHOTS_ALL) await shot(page, { name: `r-${dev.viewport.width}` }, name);
      check(`${name}: no page errors`, !page.errors.length, page.errors.join(' | '));
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
}

(async () => {
  await safe();
  const pw = require(process.env.PLAYWRIGHT_DIR || path.join(os.homedir(), 'node_modules', 'playwright'));
  const only = process.env.SMOKE_ONLY && process.env.SMOKE_ONLY.split(',');
  if (only) { // SMOKE_ONLY=F4,F13,... or SMOKE_ONLY=all: only the regression checks (SMOKE_WEBKIT=1: iPhone 13 WebKit)
    const dev = process.env.SMOKE_WEBKIT ? pw.devices['iPhone 13'] : { viewport: { width: Number(process.env.SMOKE_W) || 412, height: Number(process.env.SMOKE_H) || 915 }, isMobile: true, hasTouch: true };
    await regress(process.env.SMOKE_WEBKIT ? pw.webkit : chromium, dev, only[0] === 'all' ? null : only);
    return finish();
  }
  await regress(chromium, { viewport: { width: 360, height: 760 }, isMobile: true, hasTouch: true }, null);
  const browser = await chromium.launch();
  let posts = [];
  try {
    for (const vp of [{ name: 'phone', w: 412, h: 915, mobile: true }, { name: 'wide', w: 1280, h: 800, mobile: false }]) {
      const [ctx, page] = await context(browser, vp);
      try {
        for (const t of Object.keys(TABS)) await loadTab(page, vp, t);
        await (vp.name === 'phone' ? post : peek)(page, vp);
        await settings(page, vp);
      } catch (e) {
        check(`${vp.name}: ran to the end`, false, e.message.split('\n')[0]);
      }
      noErrors(page, vp.name);
      posts = posts.concat(page.posts);
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
  const other = posts.filter(([p, body]) => !(p === '/api/settings' && /"dry_run":\s*true/.test(body)) && p !== '/api/post');
  check('only the one post and settings dry runs were sent', posts.filter(([p]) => p === '/api/post').length <= 1 && !other.length,
    posts.map(([p, b]) => `${p} ${b.slice(0, 80)}`).join(' | '));
  finish();
})();

function finish() {
  check('no request left 127.0.0.1', !offsite.length, offsite.join(' '));
  const failed = results.filter((r) => !r.ok);
  console.log(`\nScreenshots: ${SHOTS}`);
  console.log(`${results.length - failed.length}/${results.length} checks passed${failed.length ? `; FAILED: ${failed.map((f) => f.name).join('; ')}` : ''}`);
  process.exit(failed.length ? 1 : 0);
}
