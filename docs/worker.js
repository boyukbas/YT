// Runs Whisper off the main thread so the page stays responsive.
import { pipeline, Tensor, env } from 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.1/dist/transformers.min.js';
import { transcribeMixed } from './core.js';

const MODEL = 'onnx-community/whisper-small_timestamped';
env.allowLocalModels = false;
if (self.crossOriginIsolated) {
  env.backends.onnx.wasm.numThreads = Math.min(4, navigator.hardwareConcurrency || 2);
}

let asrPromise = null;

function loadModel() {
  if (!asrPromise) {
    asrPromise = pipeline('automatic-speech-recognition', MODEL, {
      device: 'wasm',
      dtype: { encoder_model: 'q8', decoder_model_merged: 'q8' },
      progress_callback: (p) => {
        if (p.status === 'progress_total') postMessage({ type: 'download', progress: p.progress });
      },
    }).catch((e) => { asrPromise = null; throw e; });
  }
  return asrPromise;
}

self.onmessage = async ({ data }) => {
  if (data.type !== 'transcribe') return;
  try {
    postMessage({ type: 'status', stage: 'model' });
    const asr = await loadModel();
    const words = await transcribeMixed({
      asr, Tensor, audio: data.audio, primary: data.primary, secondary: data.secondary,
      onStatus: (stage, i, n) => postMessage({ type: 'status', stage, i, n }),
    });
    postMessage({ type: 'done', words });
  } catch (e) {
    postMessage({ type: 'error', message: e?.message || String(e) });
  }
};
