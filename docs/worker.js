// Runs Whisper off the main thread so the page stays responsive.
// Served from this site (not a CDN): Safari refuses cross-site scripts inside an isolated worker.
import { pipeline, Tensor, env } from './lib/transformers.min.js';
import { transcribeMixed, alignScript } from './core.js';
import { ensureModel } from './download.js';

const MODELS = {
  small: 'onnx-community/whisper-small_timestamped',
  turbo: 'onnx-community/whisper-large-v3-turbo_timestamped',
};
env.allowLocalModels = false;
env.backends.onnx.wasm.wasmPaths = new URL('./lib/', import.meta.url).href;
self.addEventListener('unhandledrejection', (e) => postMessage({ type: 'error', message: e.reason?.message || String(e.reason) }));
// Forward the engine's own warnings and errors to the page's diagnostics log.
for (const level of ['error', 'warn']) {
  const orig = console[level].bind(console);
  console[level] = (...args) => {
    try { postMessage({ type: 'log', level, message: args.map(a => a?.message || String(a)).join(' ').slice(0, 500) }); } catch {}
    orig(...args);
  };
}
if (self.crossOriginIsolated) env.backends.onnx.wasm.numThreads = Math.min(4, navigator.hardwareConcurrency || 2);

const loaded = {};   // "small|webgpu" -> Promise<pipeline>

async function gpuInfo() {
  try {
    const adapter = await navigator.gpu?.requestAdapter();
    return adapter ? { ok: true, f16: adapter.features.has('shader-f16') } : { ok: false };
  } catch { return { ok: false }; }
}

function dtypeFor(kind, device, f16) {
  if (device === 'webgpu') {
    if (kind === 'small') return { encoder_model: f16 ? 'fp16' : 'fp32', decoder_model_merged: 'q4' };
    return { encoder_model: f16 ? 'fp16' : 'q4', decoder_model_merged: 'q4' };
  }
  return kind === 'small'
    ? { encoder_model: 'q8', decoder_model_merged: 'q8' }
    : { encoder_model: 'q4', decoder_model_merged: 'q4' };
}

const SUFFIX = { q8: '_quantized', q4: '_q4', fp16: '_fp16', fp32: '' };

function load(kind, device, f16, label) {
  const key = `${kind}|${device}`;
  loaded[key] ??= (async () => {
    const dtype = dtypeFor(kind, device, f16);
    // Big files first, resumably (a dropped connection continues where it stopped).
    const files = Object.entries(dtype).map(([name, d]) => `onnx/${name}${SUFFIX[d]}.onnx`);
    let last = -1;
    await ensureModel(MODELS[kind], files, (pct) => {
      if (Math.floor(pct) !== last) { last = Math.floor(pct); postMessage({ type: 'download', label, progress: pct }); }
    });
    postMessage({ type: 'status', stage: 'loading', label });
    return pipeline('automatic-speech-recognition', MODELS[kind], { device, dtype });
  })().catch((e) => { delete loaded[key]; throw e; });
  return loaded[key];
}

/** Frees the models' memory once a job is over (they reload quickly from the device). */
async function unloadAll() {
  for (const [key, p] of Object.entries(loaded)) {
    delete loaded[key];
    try { await (await p).dispose(); } catch {}
  }
}

let checkpoint = null;

async function run(data, device, f16) {
  postMessage({ type: 'status', stage: 'model', device, threads: env.backends.onnx.wasm.numThreads });
  // accuracy: "normal" = small model; "second" = big model only for second-language words;
  // "all" = big model for everything (slowest, best).
  const all = data.accuracy === 'all';
  const asr = await load(all ? 'turbo' : 'small', device, f16, all ? 'accurate model' : 'speech model');
  const asrSecondary = data.accuracy === 'second' && data.secondary ? await load('turbo', device, f16, 'accurate model') : null;
  let words = await transcribeMixed({
    asr, asrSecondary, Tensor, audio: data.audio, primary: data.primary, secondary: data.secondary,
    glossary: data.script ? '' : data.glossary,
    onStatus: (stage, i, n, info) => postMessage({ type: 'status', stage, i, n, info }),
    resume: checkpoint, detect: data.detect !== false,
    onCheckpoint: (state) => { checkpoint = state; postMessage({ type: 'checkpoint', state }); },
  });
  if (data.script?.trim()) words = alignScript(data.script, words, data.primary, data.secondary);
  return words;
}

self.onmessage = async ({ data }) => {
  if (data.type !== 'transcribe') return;
  checkpoint = data.resume || null;
  try {
    const gpu = data.gpu ? await gpuInfo() : { ok: false };
    let words;
    if (gpu.ok) {
      try {
        words = await run(data, 'webgpu', gpu.f16);
      } catch (e) {
        // GPU path failed on this device: fall back to the CPU and say so.
        postMessage({ type: 'gpuFailed', message: e?.message || String(e) });
        words = await run(data, 'wasm', false);
      }
    } else {
      words = await run(data, 'wasm', false);
    }
    postMessage({ type: 'done', words });
  } catch (e) {
    postMessage({ type: 'error', message: e?.message || String(e) });
  } finally {
    await unloadAll();
  }
};
