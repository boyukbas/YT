// Sound: background music (with automatic lowering under speech) and background-noise
// removal (RNNoise, on the device). Shared by the preview and the export.

// ---------- Music: how loud it is at each moment ----------
export const DUCK = { lot: 0.2, little: 0.45, none: 1 };
export const defaultMusic = () => ({ id: null, on: false, vol: 0.25, duck: 'lot' });

/**
 * How much of the music volume to keep at each point of the original video (one value per
 * `hop` seconds): 1 in pauses, `keep` while speaking. It goes down a moment before speech
 * starts and comes back up gently after it ends, so it never jumps.
 */
export function duckEnvelope(levels, threshold, hop, keep) {
  const n = levels.length, env = new Float32Array(n).fill(1);
  if (!n || keep >= 1) return env;
  const before = Math.round(0.3 / hop), after = Math.round(0.6 / hop);
  // Speech nearby (a little before and after) counts as speech, so short gaps between words don't pump.
  const near = new Uint8Array(n);
  let last = -1e9;
  for (let i = 0; i < n; i++) {
    if (levels[i] >= threshold) last = i;
    if (i - last <= after) near[i] = 1;
  }
  let next = 1e9;
  for (let i = n - 1; i >= 0; i--) {
    if (levels[i] >= threshold) next = i;
    if (next - i <= before) near[i] = 1;
  }
  // Smooth the steps into ramps (moving average over ~0.3 s).
  const w = Math.max(1, Math.round(0.15 / hop));
  let sum = 0;
  const pre = new Float32Array(n + 1);
  for (let i = 0; i < n; i++) { sum += near[i] ? keep : 1; pre[i + 1] = sum; }
  for (let i = 0; i < n; i++) {
    const a = Math.max(0, i - w), b = Math.min(n, i + w + 1);
    env[i] = (pre[b] - pre[a]) / (b - a);
  }
  return env;
}
export const envAt = (env, hop, t) => env.length ? env[Math.min(env.length - 1, Math.max(0, Math.floor(t / hop)))] : 1;

/** Fade the music in at the start and out at the end of the video. */
export const musicFade = (outT, total) => Math.min(1, outT / 1.5, Math.max(0, total - outT) / 3);

/** Decode a music file to the export's sample rate (the browser does the resampling). */
export async function decodeMusic(blob, sampleRate) {
  const ctx = new OfflineAudioContext(2, 1, sampleRate);
  return ctx.decodeAudioData(await blob.arrayBuffer());
}

/**
 * Adds looping music to an audio buffer. `startFrame` is where this buffer sits in the output
 * (counted in samples), `srcAt(i)` the original-video time of sample i (for the lowering),
 * `gainAt(srcT, outT)` the music volume at that point.
 */
export function mixMusic(out, music, startFrame, srcAt, gainAt) {
  const sr = out.sampleRate, mlen = music.length, mch = music.numberOfChannels;
  if (!mlen) return;
  const chans = Array.from({ length: out.numberOfChannels }, (_, c) => out.getChannelData(c));
  const mus = Array.from({ length: mch }, (_, c) => music.getChannelData(c));
  // Volume changes slowly, so compute it every 256 samples.
  let g = 0;
  for (let i = 0; i < out.length; i++) {
    if ((i & 255) === 0) g = gainAt(srcAt(i), (startFrame + i) / sr);
    if (g <= 0) continue;
    const m = (startFrame + i) % mlen;
    for (let c = 0; c < chans.length; c++) {
      const v = chans[c][i] + mus[c % mch][m] * g;
      chans[c][i] = v > 1 ? 1 : v < -1 ? -1 : v;
    }
  }
}

// ---------- Noise removal ----------
export const DENOISE = { off: 0, light: 0.8, strong: 1 };
let rnnoise = null;
export async function loadDenoiser() {
  if (!rnnoise) {
    const { Rnnoise } = await import('./lib/rnnoise/rnnoise.js');
    rnnoise = await Rnnoise.load();
  }
  return rnnoise;
}

// Streaming linear resampler (only used when the video's sound isn't 48 kHz, which RNNoise needs).
class Resampler {
  constructor(from, to) { this.step = from / to; this.pos = 0; this.prev = 0; }
  push(x) {
    const out = [];
    const at = (k) => (k === 0 ? this.prev : x[k - 1]);
    const len = x.length + 1;
    while (Math.floor(this.pos) + 1 < len) {
      const i = Math.floor(this.pos), f = this.pos - i;
      out.push(at(i) * (1 - f) + at(i + 1) * f);
      this.pos += this.step;
    }
    this.pos -= len - 1;
    if (x.length) this.prev = x[x.length - 1];
    return out;
  }
}
class Fifo {
  constructor(prefill = 0) { this.buf = new Float32Array(Math.max(1024, prefill * 2)); this.n = prefill; }
  push(arr) {
    if (this.n + arr.length > this.buf.length) {
      const b = new Float32Array((this.n + arr.length) * 2); b.set(this.buf.subarray(0, this.n)); this.buf = b;
    }
    this.buf.set(arr, this.n); this.n += arr.length;
  }
  take(k) {
    const out = new Float32Array(k), m = Math.min(k, this.n);
    out.set(this.buf.subarray(0, m)); this.buf.copyWithin(0, m, this.n); this.n -= m;
    return out;
  }
}

/**
 * Removes background noise from a stream of AudioBuffers. The output is the same length as
 * the input but `latency` seconds late, so read the source that much further and shift
 * timestamps back (see cleanBuffers).
 */
export class Denoiser {
  constructor(rn, sampleRate, channels, strength) {
    this.state = rn.createDenoiseState(); this.size = rn.frameSize;
    this.sr = sampleRate; this.chs = channels; this.wet = strength;
    this.up = sampleRate === 48000 ? null : new Resampler(sampleRate, 48000);
    this.down = sampleRate === 48000 ? null : new Resampler(48000, sampleRate);
    // Our buffering delays by `lag`; RNNoise itself adds two frames (960 samples at 48 kHz, measured).
    // The untouched sound is delayed by both, so the two can be mixed in step.
    const lag = Math.ceil(this.size * sampleRate / 48000) + 8, own = Math.round(2 * this.size * sampleRate / 48000);
    this.latency = (lag + own) / sampleRate;
    this.in48 = new Fifo(); this.wetOut = new Fifo(lag);
    this.dry = Array.from({ length: channels }, () => new Fifo(lag + own));
    this.frame = new Float32Array(this.size);
  }
  process(buffer) {
    const n = buffer.length, chs = buffer.numberOfChannels;
    const mono = new Float32Array(n);
    for (let c = 0; c < chs; c++) {
      const d = buffer.getChannelData(c);
      for (let i = 0; i < n; i++) mono[i] += d[i] / chs;
      this.dry[Math.min(c, this.chs - 1)].push(d);
    }
    this.in48.push(this.up ? this.up.push(mono) : mono);
    const done = [];
    while (this.in48.n >= this.size) {
      const f = this.in48.take(this.size);
      for (let i = 0; i < f.length; i++) this.frame[i] = f[i] * 32768;
      this.state.processFrame(this.frame);
      const o = new Float32Array(this.size);
      for (let i = 0; i < o.length; i++) o[i] = this.frame[i] / 32768;
      done.push(o);
    }
    for (const o of done) this.wetOut.push(this.down ? this.down.push(o) : o);
    const wet = this.wetOut.take(n);
    const out = new AudioBuffer({ length: n, numberOfChannels: chs, sampleRate: buffer.sampleRate });
    for (let c = 0; c < chs; c++) {
      const dry = this.dry[Math.min(c, this.chs - 1)].take(n);
      if (this.wet < 1) for (let i = 0; i < n; i++) dry[i] = wet[i] * this.wet + dry[i] * (1 - this.wet);
      else dry.set(wet);
      out.copyToChannel(dry, c);
    }
    return out;
  }
  destroy() { try { this.state.destroy(); } catch {} }
}

/**
 * Like `sink.buffers(start, end)`, but with the noise removed. Reads a little further on
 * both sides so the delay of the noise removal is hidden.
 */
export async function* cleanBuffers(sink, start, end, rn, strength) {
  let dn = null;
  try {
    for await (const { buffer, timestamp } of sink.buffers(Math.max(0, start - 0.05), end + 0.05)) {
      dn ??= new Denoiser(rn, buffer.sampleRate, buffer.numberOfChannels, strength);
      yield { buffer: dn.process(buffer), timestamp: timestamp - dn.latency };
    }
  } finally { dn?.destroy(); }
}
