"""Automatic lighting per clip: measured on her face, so a dark or backlit recording (window behind her) still gets a
well-lit face, real blacks and clean whites. A clip that is already fine gets no filter.

measure(): 8 frames → face brightness (middle of the face box), face colour (B/G) and the frame's 1st / 99.5th percentile
grade():   → ffmpeg filter: a smooth (pchip) curve black point → face → white point, plus a little warmth when the skin
           is grey/blue, light denoise when the shadows are lifted a lot, gentle colour and sharpening.
"""
import json
import cv2
import numpy as np
import layout as L

FACE_TARGET = 0.56          # skin brightness (0-1, Rec.709 luma) that reads as well lit
MAX_LIFT = 0.12             # never brighten the face by more than this: 0.40 → 0.58 washed out a sunlit face


def measure(c, size, model, frame, n=8):
    """frame(t) → BGR picture frame without grading. Returns {face, bg_ratio, p1, p995} or None (no face found)."""
    faces, bgs, lows, highs = [], [], [], []
    for t in np.linspace(min(5, c['duration'] * 0.1), c['duration'] * 0.95, n):
        fr = frame(t)
        if fr is None:
            continue
        h, w = fr.shape[:2]
        Y = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(float) / 255
        lows.append(np.percentile(Y, 1))
        highs.append(np.percentile(Y, 99.5))
        _, f = L.detector(model, w, h).detect(fr)
        if f is None:
            continue
        x, y, fw, fh = (int(v) for v in max(f, key=lambda r: r[2] * r[3])[:4])
        skin = fr[max(0, y + fh // 5):y + fh * 4 // 5, max(0, x + fw // 5):x + fw * 4 // 5].reshape(-1, 3).astype(float) / 255
        if len(skin):
            b, g, r = skin.mean(0)
            faces.append(0.299 * r + 0.587 * g + 0.114 * b)
            bgs.append(b / max(g, 1e-3))
    if not faces:
        return None
    return {'face': float(np.median(faces)), 'bg_ratio': float(np.median(bgs)),
            'p1': float(np.median(lows)), 'p995': float(np.median(highs))}


def grade(m):
    """Measurement → ffmpeg filter string ('' when the clip is already fine)."""
    if not m:
        return ''
    lo, hi, face = min(m['p1'], 0.12), m['p995'], m['face']
    pts = [(0.0, 0.0)]
    if lo > 0.03:                                   # greyish blacks → real black
        pts.append((lo, 0.02))
    want = min(FACE_TARGET, face + MAX_LIFT)
    lift = want - face
    if lift > 0.03 and pts[-1][0] < face < hi:
        pts.append((face, want))
    if hi < 0.95:                                   # dull whites (often after HDR → SDR) → clean white
        pts.append((hi, min(0.975, hi + 0.10)))
    pts.append((1.0, 1.0))
    if len(pts) == 2:
        return ''
    curve = ' '.join(f'{x:.3f}/{y:.3f}' for x, y in pts)
    vf = []
    if lift > 0.08:                                 # lifted shadows show sensor noise
        vf.append('hqdn3d=1:1:3:3')
    vf.append(f"curves=interp=pchip:all='{curve}'")
    if m['bg_ratio'] > 0.93:                        # grey/bluish skin → a touch warmer
        b = min(0.06, m['bg_ratio'] - 0.90)
        vf.append(f'colorbalance=rm={b / 2:.3f}:bm={-b:.3f}')
    vf += ['vibrance=intensity=0.10', 'cas=strength=0.4']
    return ','.join(vf)


def for_clips(clips, size, model, frame, work, log):
    """Adds c['grade'] to every clip (cached in work/light.json)."""
    cache_p = work / 'light.json'
    cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    for c in clips:
        key = f"{c['path']}|{size[0]}x{size[1]}"
        if key not in cache:
            cache[key] = measure(c, size, model, lambda t: frame(c, t))
        m = cache[key]
        c['grade'] = grade(m)
        log(f"light {c['name']}: " + (f"face {m['face']:.2f}, black {m['p1']:.2f}, white {m['p995']:.2f}, B/G {m['bg_ratio']:.2f}"
                                      if m else 'no face found') + (' → corrected' if c['grade'] else ' → fine as is'))
    cache_p.write_text(json.dumps(cache, indent=1))
