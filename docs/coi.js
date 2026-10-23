// Service worker with two jobs:
// 1. Adds the headers that let Whisper use several CPU cores (GitHub Pages can't send them).
// 2. Keeps the app on the device so it opens and works offline (the speech model itself is
//    cached separately by the speech library after its first download).
const CACHE = 'shorts-editor-v2';
const SHELL = ['./', 'index.html', 'app.js', 'core.js', 'render.js', 'store.js', 'worker.js', 'coi.js',
  'manifest.webmanifest', 'icon.svg', 'icon-180.png', 'icon-192.png', 'icon-512.png',
  'lib/transformers.min.js', 'lib/mediabunny.min.mjs'];
const FONT_HOSTS = ['fonts.googleapis.com', 'fonts.gstatic.com'];

if (typeof window === 'undefined') {
  const withHeaders = (res) => {
    if (!res || res.status === 0 || res.type === 'opaque') return res;
    const headers = new Headers(res.headers);
    headers.set('Cross-Origin-Embedder-Policy', 'require-corp');
    headers.set('Cross-Origin-Opener-Policy', 'same-origin');
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
  };
  // Store a copy in the background; a failed store (e.g. storage full) must never break the app.
  const put = (req, res) => {
    if (res && res.ok && res.status === 200) {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
    }
    return res;
  };
  const cacheFirst = async (req) => (await caches.match(req, { ignoreSearch: true })) || put(req, await fetch(req));
  const networkFirst = async (req) => {
    try {
      return await put(req, await fetch(req));
    } catch (e) {
      const hit = await caches.match(req, { ignoreSearch: true })
        || (req.mode === 'navigate' ? await caches.match('./') : null);
      if (hit) return hit;
      throw e;
    }
  };

  self.addEventListener('install', (e) => {
    e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
  });
  self.addEventListener('activate', (e) => {
    e.waitUntil((async () => {
      for (const k of await caches.keys()) if (k.startsWith('shorts-editor-') && k !== CACHE) await caches.delete(k);
      await self.clients.claim();
    })());
  });
  self.addEventListener('fetch', (e) => {
    const req = e.request;
    if (req.method !== 'GET') return;
    if (req.cache === 'only-if-cached' && req.mode !== 'same-origin') return;
    const url = new URL(req.url);
    if (url.origin === self.location.origin) {
      // Big engine files rarely change: cache first. App files: fresh when online, cached when not.
      const strategy = url.pathname.includes('/lib/') ? cacheFirst : networkFirst;
      e.respondWith(strategy(req).then(withHeaders));
    } else if (FONT_HOSTS.includes(url.hostname)) {
      e.respondWith(cacheFirst(req));
    }
    // Everything else (the speech model download) goes straight to the network;
    // the speech library stores the model itself.
  });
} else if (window.isSecureContext && 'serviceWorker' in navigator) {
  navigator.serviceWorker.register(document.currentScript.src).then((reg) => {
    if (window.crossOriginIsolated) return;
    const reloaded = sessionStorage.getItem('coiReloaded');
    if (reg.active && !navigator.serviceWorker.controller && !reloaded) {
      sessionStorage.setItem('coiReloaded', '1');
      location.reload();
    } else if (!reg.active) {
      reg.addEventListener('updatefound', () => {
        reg.installing?.addEventListener('statechange', (ev) => {
          if (ev.target.state === 'activated' && !sessionStorage.getItem('coiReloaded')) {
            sessionStorage.setItem('coiReloaded', '1');
            location.reload();
          }
        });
      });
    }
  }).catch(() => {});
}
