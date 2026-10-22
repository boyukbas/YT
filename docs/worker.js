// Runs Whisper off the main thread so the page stays responsive.
import { pipeline, Tensor, env } from 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.1/dist/transformers.min.js';
import { transcribeMixed, alignScript } from './core.js';

const MODELS = {
  small: 'onnx-community/whisper-small_timestamped',
  turbo: 'onnx-community/whisper-large-v3-turbo_timestamped',
};
env.allowLocalModels = false;
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

function load(kind, device, f16, label) {
  const key = `${kind}|${device}`;
  loaded[key] ??= pipeline('automatic-speech-recognition', MODELS[kind], {
    device, dtype: dtypeFor(kind, device, f16),
    progress_callback: (p) => {
      if (p.status === 'progress_total') postMessage({ type: 'download', label, progress: p.progress });
    },
  }).catch((e) => { delete loaded[key]; throw e; });
  return loaded[key];
}

async function run(data, device, f16) {
  postMessage({ type: 'status', stage: 'model', device });
  const asr = await load('small', device, f16, 'speech model');
  const asrSecondary = data.accurate && data.secondary ? await load('turbo', device, f16, 'accurate model') : null;
  let words = await transcribeMixed({
    asr, asrSecondary, Tensor, audio: data.audio, primary: data.primary, secondary: data.secondary,
    glossary: data.script ? '' : data.glossary,
    onStatus: (stage, i, n) => postMessage({ type: 'status', stage, i, n }),
  });
  if (data.script?.trim()) words = alignScript(data.script, words, data.primary, data.secondary);
  return words;
}

self.onmessage = async ({ data }) => {
  if (data.type !== 'transcribe') return;
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
  }
};
