"""Speech: three Whisper passes per clip (forced Turkish, forced German, auto) with word timings.
The Claude step merges them: Turkish timing is the backbone, the German pass spells the German parts right."""
import json
from pathlib import Path
from tools import cuda_dlls, ffmpeg

PASSES = ('tr', 'de', 'auto')


def extract_wav(src, dst):
    if not Path(dst).exists():
        ffmpeg('-i', src, '-map', '0:a:0', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', dst)


def transcribe(clips, work, prompt, status):
    """clips: [{'id', 'path'}]. Writes work/tx/<id>.<pass>.json; skips what already exists."""
    todo = [(c, p) for c in clips for p in PASSES if not (work / 'tx' / f'{c["id"]}.{p}.json').exists()]
    if not todo:
        return
    (work / 'tx').mkdir(exist_ok=True)
    (work / 'wav').mkdir(exist_ok=True)
    cuda_dlls()
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel('large-v3', device='cuda', compute_type='float16')
    except Exception as e:                      # no GPU: slow but works
        status.log(f'GPU not available ({e}); using CPU')
        model = WhisperModel('large-v3', device='cpu', compute_type='int8')
    for c, p in todo:
        wav = work / 'wav' / f'{c["id"]}.wav'
        extract_wav(c['path'], wav)
        status.log(f'speech: {c["id"]} pass {p}')
        segs, info = model.transcribe(str(wav), language=None if p == 'auto' else p, word_timestamps=True, beam_size=5,
                                      vad_filter=False, condition_on_previous_text=False, initial_prompt=prompt)
        out = [{'s': round(s.start, 2), 'e': round(s.end, 2), 'text': s.text.strip(),
                'words': [{'s': round(w.start, 2), 'e': round(w.end, 2), 'w': w.word.strip(), 'p': round(w.probability, 2)}
                          for w in s.words]} for s in segs]
        (work / 'tx' / f'{c["id"]}.{p}.json').write_text(
            json.dumps({'lang': info.language, 'segs': out}, ensure_ascii=False, indent=1), encoding='utf-8')
