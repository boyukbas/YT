// Combines several videos into one, so the result can be edited like any single video.
import {
  Input, Output, BlobSource, BufferTarget, StreamTarget, Mp4OutputFormat, ALL_FORMATS,
  CanvasSink, AudioBufferSink, CanvasSource, AudioBufferSource, QUALITY_HIGH,
  getFirstEncodableVideoCodec, getFirstEncodableAudioCodec,
} from './lib/mediabunny.min.mjs';

/** Basic facts and a small picture of one video. */
export async function probe(file) {
  const input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
  const v = await input.getPrimaryVideoTrack();
  if (!v) throw new Error(`${file.name}: no video in this file`);
  const a = await input.getPrimaryAudioTrack();
  const duration = await input.computeDuration();
  const width = await v.getDisplayWidth(), height = await v.getDisplayHeight();
  let thumb = null;
  try {
    const f = await new CanvasSink(v, { width: 160, fit: 'contain' }).getCanvas(Math.min(1, duration / 4));
    if (f) { thumb = document.createElement('canvas'); thumb.width = f.canvas.width; thumb.height = f.canvas.height; thumb.getContext('2d').drawImage(f.canvas, 0, 0); }
  } catch {}
  return { file, name: file.name, duration, width, height, hasAudio: !!a, thumb };
}

const blur = new OffscreenCanvas(48, 48), mid = new OffscreenCanvas(192, 192);
/** Same size and shape: draw as is. Otherwise fit it on a blurred copy of itself. */
function draw(ctx, src, sw, sh, W, H) {
  if (Math.abs(sw / sh - W / H) < 0.01) { ctx.drawImage(src, 0, 0, W, H); return; }
  const cover = Math.max(W / sw, H / sh), cw = W / cover, ch = H / cover;
  blur.width = 48; blur.height = Math.max(8, Math.round(48 * H / W));
  blur.getContext('2d').drawImage(src, (sw - cw) / 2, (sh - ch) / 2, cw, ch, 0, 0, blur.width, blur.height);
  // Two-step enlarge: smoother blur than one big stretch.
  mid.width = blur.width * 4; mid.height = blur.height * 4;
  const mc = mid.getContext('2d'); mc.imageSmoothingEnabled = true; mc.imageSmoothingQuality = 'high';
  mc.drawImage(blur, 0, 0, mid.width, mid.height);
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(mid, 0, 0, W, H);
  ctx.fillStyle = 'rgba(0,0,0,0.3)'; ctx.fillRect(0, 0, W, H);
  const fit = Math.min(W / sw, H / sh);
  ctx.drawImage(src, (W - sw * fit) / 2, (H - sh * fit) / 2, sw * fit, sh * fit);
}

/** Whole audio of one clip, converted to the output's sample rate and channel count. */
async function clipAudio(file, duration, rate, channels) {
  const frames = Math.max(1, Math.ceil(duration * rate));
  const input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
  const track = await input.getPrimaryAudioTrack();
  if (!track) return Array.from({ length: channels }, () => new Float32Array(frames)); // silence
  const parts = [];
  let srcRate = rate, srcCh = 1, total = 0;
  for await (const { buffer } of new AudioBufferSink(track).buffers()) {
    srcRate = buffer.sampleRate; srcCh = buffer.numberOfChannels;
    parts.push(Array.from({ length: srcCh }, (_, c) => buffer.getChannelData(c).slice()));
    total += buffer.length;
  }
  const off = new OfflineAudioContext(channels, frames, rate);
  const buf = off.createBuffer(srcCh, Math.max(1, total), srcRate);
  let o = 0;
  for (const p of parts) { p.forEach((d, c) => buf.copyToChannel(d, c, o)); o += p[0].length; }
  const src = off.createBufferSource(); src.buffer = buf; src.connect(off.destination); src.start();
  const out = await off.startRendering();
  return Array.from({ length: channels }, (_, c) => out.getChannelData(Math.min(c, out.numberOfChannels - 1)));
}

/**
 * Joins the clips in order. The output takes the first clip's shape (max 1080p);
 * clips of a different shape are fitted on a blurred background.
 */
export async function mergeClips(clips, { onProgress = () => {}, log = () => {} } = {}) {
  const first = clips[0];
  const scale = Math.min(1, 1920 / Math.max(first.width, first.height));
  const W = Math.round(first.width * scale / 2) * 2, H = Math.round(first.height * scale / 2) * 2;
  const vCodec = await getFirstEncodableVideoCodec(['avc', 'hevc', 'vp9', 'av1'], { width: W, height: H });
  if (!vCodec) throw new Error('This browser cannot encode video');
  const aCodec = await getFirstEncodableAudioCodec(['aac', 'opus']);
  const rate = 48000, channels = 2;
  const total = clips.reduce((a, c) => a + c.duration, 0);

  // Straight to device storage when possible (long videos don't fit in memory).
  let handle = null, target, writable = null;
  try {
    const root = await navigator.storage.getDirectory();
    for await (const name of root.keys()) if (name.startsWith('merged-')) { try { await root.removeEntry(name); } catch {} }
    handle = await root.getFileHandle(`merged-${Date.now()}.mp4`, { create: true });
    writable = await handle.createWritable();
    target = new StreamTarget(writable, { chunked: true });
  } catch { handle = null; target = new BufferTarget(); }

  const canvas = new OffscreenCanvas(W, H), ctx = canvas.getContext('2d');
  const output = new Output({ format: new Mp4OutputFormat({ fastStart: handle ? false : 'in-memory' }), target });
  const vSource = new CanvasSource(canvas, { codec: vCodec, bitrate: QUALITY_HIGH });
  output.addVideoTrack(vSource, { frameRate: 30 });
  const aSource = aCodec ? new AudioBufferSource({ codec: aCodec, bitrate: QUALITY_HIGH }) : null;
  if (aSource) output.addAudioTrack(aSource);
  await output.start();
  log({ W, H, vCodec, aCodec, clips: clips.length, secs: +total.toFixed(1), target: handle ? 'device storage' : 'memory' });

  const doVideo = async () => {
    let offset = 0, last = -1;
    for (const c of clips) {
      const input = new Input({ source: new BlobSource(c.file), formats: ALL_FORMATS });
      const track = await input.getPrimaryVideoTrack();
      const sw = Math.round(c.width * Math.min(1, 1920 / Math.max(c.width, c.height)));
      const sh = Math.round(c.height * Math.min(1, 1920 / Math.max(c.width, c.height)));
      for await (const f of new CanvasSink(track, { width: sw, height: sh, fit: 'fill' }).canvases()) {
        const t = offset + Math.max(0, f.timestamp);
        if (t <= last || f.timestamp >= c.duration) continue;
        draw(ctx, f.canvas, sw, sh, W, H);
        await vSource.add(t, Math.max(1 / 60, Math.min(f.duration, c.duration - f.timestamp)));
        last = t;
        onProgress(Math.min(1, t / total));
      }
      offset += c.duration;
    }
    vSource.close();
  };
  const doAudio = async () => {
    if (!aSource) return;
    for (const c of clips) {
      const data = await clipAudio(c.file, c.duration, rate, channels);
      const n = data[0].length, step = rate; // 1-second pieces
      for (let i = 0; i < n; i += step) {
        const len = Math.min(step, n - i);
        const b = new AudioBuffer({ length: len, numberOfChannels: channels, sampleRate: rate });
        for (let ch = 0; ch < channels; ch++) b.copyToChannel(data[ch].subarray(i, i + len), ch);
        await aSource.add(b);
      }
    }
    aSource.close();
  };
  await Promise.all([doVideo(), doAudio()]);
  await output.finalize();
  const name = `combined-${clips.length}-videos.mp4`;
  if (handle) {
    if (writable) { try { await writable.close(); } catch {} }
    const f = await handle.getFile();
    return new File([f], name, { type: 'video/mp4' });
  }
  return new File([output.target.buffer], name, { type: 'video/mp4' });
}
