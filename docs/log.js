// A small diagnostics log kept on this device only (localStorage). Nothing is sent anywhere
// unless you copy it or choose to report it. Video content and subtitle text are never logged.
export const APP_VERSION = '2026-10-29.1';
const KEY = 'shorts-log';
const MAX = 400;

let entries = [];
try { entries = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch {}
let saveTimer = null;
const save = () => {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { try { localStorage.setItem(KEY, JSON.stringify(entries.slice(-MAX))); } catch {} }, 300);
};

export function log(level, msg, data) {
  const e = { t: new Date().toISOString(), level, msg: String(msg).slice(0, 500) };
  if (data !== undefined) { try { e.data = JSON.stringify(data).slice(0, 800); } catch {} }
  entries.push(e);
  if (entries.length > MAX) entries = entries.slice(-MAX);
  save();
}
export const info = (m, d) => log('info', m, d);
export const warn = (m, d) => log('warn', m, d);
export const error = (m, d) => log('error', m, d);
export function clear() { entries = []; save(); }
export const errorCount = () => entries.filter(e => e.level === 'error').length;

export async function environment() {
  let gpu = 'none';
  try {
    const a = await navigator.gpu?.requestAdapter();
    gpu = a ? `yes${a.features.has('shader-f16') ? ' (f16)' : ''}` : (navigator.gpu ? 'no adapter' : 'none');
  } catch (e) { gpu = 'error: ' + e.message; }
  let storage = '?';
  try {
    const est = await navigator.storage?.estimate?.();
    if (est) storage = `${Math.round(est.usage / 1e6)} MB used of ${Math.round(est.quota / 1e6)} MB`;
  } catch {}
  return {
    version: APP_VERSION, userAgent: navigator.userAgent,
    standalone: matchMedia('(display-mode: standalone)').matches || navigator.standalone === true,
    isolated: self.crossOriginIsolated, cores: navigator.hardwareConcurrency, memoryGB: navigator.deviceMemory,
    gpu, storage, screen: `${screen.width}x${screen.height}@${devicePixelRatio}`, online: navigator.onLine,
  };
}

export async function text() {
  const env = await environment();
  const head = Object.entries(env).map(([k, v]) => `${k}: ${v}`).join('\n');
  const body = entries.map(e => `${e.t.slice(5, 19).replace('T', ' ')} ${e.level.toUpperCase().padEnd(5)} ${e.msg}${e.data ? ' ' + e.data : ''}`).join('\n');
  return `Shorts Editor diagnostics\n${head}\n\n${body || '(no entries)'}`;
}

/** Catch everything that goes wrong on the page. */
export function install() {
  addEventListener('error', (e) => error('Page error: ' + (e.message || e.error?.message || 'unknown'),
    { file: (e.filename || '').split('/').pop(), line: e.lineno, col: e.colno }));
  addEventListener('unhandledrejection', (e) => error('Unhandled: ' + (e.reason?.message || String(e.reason))));
  for (const level of ['error', 'warn']) {
    const orig = console[level].bind(console);
    console[level] = (...args) => {
      log(level, 'console: ' + args.map(a => a instanceof Error ? a.message : typeof a === 'string' ? a : (() => { try { return JSON.stringify(a); } catch { return String(a); } })()).join(' '));
      orig(...args);
    };
  }
  addEventListener('online', () => info('Back online'));
  addEventListener('offline', () => info('Went offline'));
  info('App opened');
}
