// GitHub Pages can't send the headers that let Whisper use several CPU cores
// (cross-origin isolation). This tiny service worker adds them. If it can't, the app
// still works, just slower.
if (typeof window === 'undefined') {
  self.addEventListener('install', () => self.skipWaiting());
  self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
  self.addEventListener('fetch', (e) => {
    const req = e.request;
    if (req.cache === 'only-if-cached' && req.mode !== 'same-origin') return;
    // Only touch our own files; cross-origin requests (CDN, model files) pass through untouched.
    if (new URL(req.url).origin !== self.location.origin) return;
    e.respondWith(fetch(req).then((res) => {
      if (res.status === 0) return res;
      const headers = new Headers(res.headers);
      headers.set('Cross-Origin-Embedder-Policy', 'require-corp');
      headers.set('Cross-Origin-Opener-Policy', 'same-origin');
      return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
    }));
  });
} else if (!window.crossOriginIsolated && window.isSecureContext && 'serviceWorker' in navigator) {
  navigator.serviceWorker.register(document.currentScript.src).then((reg) => {
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
