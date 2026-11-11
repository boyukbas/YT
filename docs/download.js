// Resumable model downloads. Big files are fetched in pieces and each piece is stored as it
// arrives, so a dropped connection or a closed page continues where it stopped instead of
// starting over. Finished files go into the speech library's own cache.
const CHUNK = 8 * 1024 * 1024;
const LIB_CACHE = 'transformers-cache';
const HOST = 'https://huggingface.co/';

let dbp = null;
function db() {
  dbp ??= new Promise((res, rej) => {
    const r = indexedDB.open('shorts-downloads', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('chunks');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
  return dbp;
}
const op = async (mode, fn) => {
  const d = await db();
  return new Promise((res, rej) => {
    const t = d.transaction('chunks', mode);
    const r = fn(t.objectStore('chunks'));
    t.oncomplete = () => res(r?.result);
    t.onerror = () => rej(t.error);
    t.onabort = () => rej(t.error);
  });
};
const getChunk = (key) => op('readonly', s => s.get(key));
const putChunk = (key, val) => op('readwrite', s => s.put(val, key));
const delPrefix = (prefix) => op('readwrite', s => s.delete(IDBKeyRange.bound(prefix, prefix + '\uffff')));

export const modelFileURL = (model, file) => `${HOST}${model}/resolve/main/${file}`;

async function fetchRange(url, start, end) {
  let lastErr;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      const r = await fetch(url, { headers: { Range: `bytes=${start}-${end}` } });
      if (r.status === 206 || r.status === 200) return r;
      lastErr = new Error(`HTTP ${r.status}`);
    } catch (e) { lastErr = e; }
    await new Promise(res => setTimeout(res, 1000 * 2 ** attempt)); // 1, 2, 4, 8, 16 s
  }
  throw new Error(`Download failed after retries: ${lastErr?.message || lastErr}`);
}

/** Makes sure one file is fully in the cache. onBytes(loadedSoFar, total) reports progress. */
export async function ensureFile(url, onBytes = () => {}) {
  const cache = await caches.open(LIB_CACHE);
  if (await cache.match(url)) return 'cached';
  const meta = await getChunk(url + '|meta');
  let total = meta?.total;
  if (!total) {
    const r = await fetchRange(url, 0, CHUNK - 1);
    if (r.status === 200) { // server ignored ranges: take the whole file in one go
      await cache.put(url, new Response(await r.blob()));
      return 'downloaded';
    }
    total = Number((r.headers.get('content-range') || '').split('/')[1]);
    if (!total) throw new Error('Unknown file size');
    await putChunk(url + '|meta', { total });
    await putChunk(url + '|0', await r.blob());
  }
  const n = Math.ceil(total / CHUNK);
  let loaded = 0;
  const parts = [];
  for (let i = 0; i < n; i++) {
    let blob = await getChunk(`${url}|${i}`);
    if (!blob) {
      const r = await fetchRange(url, i * CHUNK, Math.min(total, (i + 1) * CHUNK) - 1);
      blob = await r.blob();
      await putChunk(`${url}|${i}`, blob);
    }
    parts.push(blob);
    loaded += blob.size;
    onBytes(loaded, total);
  }
  const whole = new Blob(parts, { type: 'application/octet-stream' });
  if (whole.size !== total) { await delPrefix(url + '|'); throw new Error('Downloaded file is incomplete, retrying next time'); }
  await cache.put(url, new Response(whole, { headers: { 'Content-Type': 'application/octet-stream', 'Content-Length': String(total) } }));
  await delPrefix(url + '|');
  return 'downloaded';
}

/** Downloads the big model files of one model, resumably. Small config files load normally. */
export async function ensureModel(model, files, onProgress) {
  const totals = new Map(), loaded = new Map();
  const report = () => {
    const t = [...totals.values()].reduce((a, b) => a + b, 0);
    const l = [...loaded.values()].reduce((a, b) => a + b, 0);
    if (t) onProgress(Math.min(100, (l / t) * 100), l, t);
  };
  for (const f of files) {
    const url = modelFileURL(model, f);
    await ensureFile(url, (l, t) => { totals.set(url, t); loaded.set(url, l); report(); });
  }
}
