// PDF slides as the background: each page becomes a picture, shown from its start time on.
export async function pdfToImages(file, onProgress = () => {}) {
  const pdfjs = await import('./lib/pdfjs/pdf.min.mjs');
  pdfjs.GlobalWorkerOptions.workerSrc = new URL('./lib/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
  const doc = await pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
  const n = Math.min(doc.numPages, 80);
  const blobs = [];
  for (let i = 1; i <= n; i++) {
    onProgress(i, n);
    const page = await doc.getPage(i);
    const base = page.getViewport({ scale: 1 });
    const scale = Math.min(1920 / Math.max(base.width, base.height), 3);
    const vp = page.getViewport({ scale });
    const c = document.createElement('canvas'); c.width = Math.round(vp.width); c.height = Math.round(vp.height);
    const ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);
    await page.render({ canvasContext: ctx, viewport: vp, canvas: c }).promise;
    blobs.push(await new Promise(res => c.toBlob(res, 'image/jpeg', 0.88)));
    page.cleanup?.();
  }
  await doc.destroy?.();
  return blobs;
}

/** Which slide is showing at time t (times: when slides 2, 3, … start; slide 1 starts at 0). */
export function slideAt(times, t) {
  let i = 0;
  for (let k = 0; k < times.length; k++) if (times[k] <= t + 1e-6) i = k + 1;
  return i;
}
