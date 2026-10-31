"""Where things go on screen: face tracking, the subtitle position chosen from the free space, ASS + SRT files.

Rules (agreed with the channel):
- never cover eyes, nose or mouth (the mouth shows pronunciation); hair gets a margin too
- choose once per video: long videos prefer the top, Shorts the bottom (under the chin, above YouTube's buttons)
- if the preferred side fails for a few lines only, just those lines use the other side; if it fails often,
  the other side is used for the whole video; if neither fits, the picture shrinks and the text gets its own band
"""
import json
import cv2
from PIL import ImageFont

YUNET = None


def detector(model, w, h):
    global YUNET
    if YUNET is None:
        YUNET = cv2.FaceDetectorYN.create(str(model), '', (w, h), 0.6)
    YUNET.setInputSize((w, h))
    return YUNET


def track_faces(segs, inter_path, cues, model):
    """For every cue: the face box (x0, y0, x1, y1) in picture pixels, union over 3 frames, or None."""
    caps, out = {}, []
    for c in cues:
        boxes = []
        for t in (c['s'] + 0.15, (c['s'] + c['e']) / 2, c['e'] - 0.15):
            seg = next((g for g in segs if g['start'] <= t < g['start'] + g['b'] - g['a']), segs[-1])
            k = segs.index(seg)
            cap = caps.get(k) or caps.setdefault(k, cv2.VideoCapture(str(inter_path(k))))
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0, t - seg['start']) * 1000)
            ok, fr = cap.read()
            if not ok:
                continue
            h, w = fr.shape[:2]
            _, f = detector(model, w, h).detect(fr)
            if f is not None:
                x, y, fw, fh = max(f, key=lambda r: r[2] * r[3])[:4]
                boxes.append((x, y, x + fw, y + fh))
        out.append(None if not boxes else tuple(f(b[i] for b in boxes) for i, f in enumerate((min, min, max, max))))
    # frames without a face (looking away): borrow the nearest cue's face
    for i, b in enumerate(out):
        if b is None:
            near = [out[j] for j in sorted(range(len(out)), key=lambda j: abs(j - i)) if out[j] is not None]
            out[i] = near[0] if near else None
    return out


def no_go(box):
    """Face box → area subtitles must not touch: hair above, chin below, a little at the sides."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    return (x0 - 0.15 * w, y0 - 0.40 * h, x1 + 0.15 * w, y1 + 0.08 * h)


class Spec:
    """One output video: canvas size, where the picture sits on it, and the text geometry."""

    def __init__(self, name, W, H, short, src_w, src_h, crop=None, pic=None):
        self.name, self.W, self.H, self.short = name, W, H, short
        self.crop = crop or (0, 0, src_w, src_h)          # part of the picture (inter clip pixels) that is shown
        self.pic = pic or (0, 0, W, H)                    # where that part lands on the canvas
        self.band = None
        self.src_size = (src_w, src_h)
        vertical = H > W
        self.size = 64 if vertical else round(0.056 * H)
        self.tsize = 46 if vertical else round(0.043 * H)
        self.margin_l, self.margin_r = (60, 180) if short else (int(0.1 * W), int(0.1 * W))
        self.maxw = 820 if vertical else int(0.62 * W)
        # text anchors: top edge of a top block, bottom edge of a bottom block (Shorts: below the top bar,
        # above the title/channel area)
        self.top = int(0.09 * H) if short else int(0.045 * H)
        self.bottom = int(0.79 * H) if short else int(0.95 * H)
        self.logo = None

    def to_canvas(self, box):
        cx, cy, cw, ch = self.crop
        px, py, pw, ph = self.pic
        sx, sy = pw / cw, ph / ch
        return ((box[0] - cx) * sx + px, (box[1] - cy) * sy + py, (box[2] - cx) * sx + px, (box[3] - cy) * sy + py)


# ---- text layout (same wrapping as V1/V2) ----
FONTS = {}


def font(fonts_dir, bold, italic, size):
    name = {(1, 0): 'arialbd.ttf', (0, 1): 'ariali.ttf', (1, 1): 'arialbi.ttf', (0, 0): 'arial.ttf'}[(bold, italic)]
    k = (name, size)
    if k not in FONTS:
        FONTS[k] = ImageFont.truetype(str(fonts_dir / name), size)
    return FONTS[k]


def wrap(words, maxw, meas):
    if meas(' '.join(words)) <= maxw or len(words) == 1:
        return [words]
    best = None
    for i in range(1, len(words)):
        a, b = ' '.join(words[:i]), ' '.join(words[i:])
        cost = max(meas(a), meas(b)) - (0.08 * maxw if a.endswith((',', '.', ':', '?', '!')) else 0)
        if best is None or cost < best[0]:
            best = (cost, i)
    i = best[1]
    if meas(' '.join(words[:i])) > maxw:
        j = 1
        while j < len(words) and meas(' '.join(words[:j + 1])) <= maxw:
            j += 1
        return [words[:j]] + wrap(words[j:], maxw, meas)
    return [words[:i]] + wrap(words[i:], maxw, meas)


def lines_of(text, f, maxw):
    """'Turkish [Deutsch] text' → wrapped lines of [(word, is_german)]."""
    words, de = [], False
    for tok in text.replace('[', ' [ ').replace(']', ' ] ').split():
        if tok == '[':
            de = True
        elif tok == ']':
            de = False
        else:
            words.append((tok, de))
    rows = wrap([w for w, _ in words], maxw, f.getlength)
    out, k = [], 0
    for r in rows:
        out.append(words[k:k + len(r)])
        k += len(r)
    return out


def blocks(cues, spec, fonts_dir):
    """Per cue: wrapped main lines, translation lines and the block height in canvas pixels."""
    fm, ft = font(fonts_dir, 1, 0, spec.size), font(fonts_dir, 0, 1, spec.tsize)
    out = []
    for c in cues:
        if c['lang'] == 'de':
            main = lines_of(c['text'].replace('[', '').replace(']', ''), fm, spec.maxw)
            trn = lines_of(c['tr'], ft, spec.maxw) if c.get('tr') else []
        else:
            main, trn = lines_of(c['text'], fm, spec.maxw), []
        width = max([fm.getlength(' '.join(w for w, _ in l)) for l in main] + [ft.getlength(' '.join(w for w, _ in l)) for l in trn])
        out.append({'main': main, 'trn': trn, 'h': len(main) * spec.size * 1.18 + len(trn) * spec.tsize * 1.18 + 10, 'w': width})
    return out


def text_box(spec, b, side):
    """Canvas rectangle a subtitle block covers."""
    cx = spec.margin_l + (spec.W - spec.margin_l - spec.margin_r) / 2
    x0, x1 = cx - b['w'] / 2 - 12, cx + b['w'] / 2 + 12
    return (x0, spec.top - 6, x1, spec.top + b['h']) if side == 'top' else (x0, spec.bottom - b['h'], x1, spec.bottom + 6)


def overlaps(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def place(spec, faces, blks, prefer):
    """Chooses 'top' / 'bottom' per cue, or a band. Returns (sides, report)."""
    def hits(b, f, side):
        if f is None:
            return False
        a, n = text_box(spec, b, side), no_go(spec.to_canvas(f))
        return overlaps(a, n) or bool(spec.logo and overlaps(a, spec.logo))

    other = 'bottom' if prefer == 'top' else 'top'
    fail = {s: [i for i, (b, f) in enumerate(zip(blks, faces)) if hits(b, f, s)] for s in (prefer, other)}
    few = max(1, int(0.05 * len(blks)))
    if not fail[prefer]:
        return [prefer] * len(blks), {'side': prefer, 'flipped': 0}
    if len(fail[prefer]) <= few and not set(fail[prefer]) & set(fail[other]):
        sides = [other if i in fail[prefer] else prefer for i in range(len(blks))]
        return sides, {'side': prefer, 'flipped': len(fail[prefer])}
    if not fail[other]:
        return [other] * len(blks), {'side': other, 'flipped': 0, 'why': f'{prefer} blocked in {len(fail[prefer])} lines'}
    return None, {'side': 'band', 'why': f'top blocked in {len(fail["top"])}, bottom in {len(fail["bottom"])} lines'}


def make_band(spec, blks, prefer):
    """Shrink the picture so the subtitles get their own strip on the preferred side."""
    need = max(b['h'] for b in blks) + 2 * int(0.03 * spec.H)
    f = (spec.H - need) / spec.H
    px, py, pw, ph = spec.pic
    nw, nh = pw * f, ph * f
    spec.pic = (px + (pw - nw) / 2, py + (ph - nh if prefer == 'top' else 0), nw, nh)
    spec.band = prefer
    if prefer == 'top':
        spec.top = int(0.03 * spec.H)
    else:
        spec.bottom = spec.H - int(0.03 * spec.H)
    return [prefer] * len(blks)


# ---- files ----
def ass_color(hex_rgb):
    h = hex_rgb.lstrip('#')
    return f'&H00{h[4:6]}{h[2:4]}{h[0:2]}'.upper()


def ts_ass(t):
    cs = int(round(t * 100))
    return f'{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}'


def ts_srt(t):
    ms = int(round(t * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def write_ass(path, spec, cues, blks, sides, colors):
    tr, de, tl = ass_color(colors['tr']), ass_color(colors['de']), ass_color(colors['translation'])
    out = [f"""[Script Info]
ScriptType: v4.00+
PlayResX: {spec.W}
PlayResY: {spec.H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: top,Arial,{spec.size},{tr},{tr},&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{round(spec.size / 12.8, 1)},2,8,{spec.margin_l},{spec.margin_r},{spec.top},1
Style: bottom,Arial,{spec.size},{tr},{tr},&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{round(spec.size / 12.8, 1)},2,2,{spec.margin_l},{spec.margin_r},{spec.H - spec.bottom},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""]
    for c, b, side in zip(cues, blks, sides):
        if c['lang'] == 'de':
            body = r'\N'.join(' '.join(w for w, _ in l) for l in b['main'])
            body = '{\\c' + de + '&}' + body
            if b['trn']:
                body += r'\N{\fs' + str(spec.tsize) + r'\b0\i1\c' + tl + '&}' + r'\N'.join(' '.join(w for w, _ in l) for l in b['trn'])
        else:
            rows = []
            for l in b['main']:
                s, state = '', False
                for j, (w, isde) in enumerate(l):
                    if isde != state:
                        s += '{\\c' + (de if isde else tr) + '&}'
                        state = isde
                    s += (' ' if j else '') + w
                rows.append(s + ('{\\c' + tr + '&}' if state else ''))
            body = r'\N'.join(rows)
        out.append(f'Dialogue: 0,{ts_ass(c["s"])},{ts_ass(c["e"])},{side},,0,0,0,,{body}\n')
    path.write_text(''.join(out), encoding='utf-8-sig')


def write_srt(path, cues, lang):
    """lang 'tr': all Turkish (German lines → their translation); 'de': all German; 'spoken': as spoken."""
    rows = []
    for c in cues:
        plain = c['text'].replace('[', '').replace(']', '')
        if lang == 'spoken':
            t = plain
        elif lang == c['lang']:
            t = plain
        else:
            t = c.get('tr' if lang == 'tr' else 'de') or plain
        words = t.split()
        t = '\n'.join(' '.join(x) for x in wrap(words, 42, len))
        rows.append(f'{len(rows) + 1}\n{ts_srt(c["s"])} --> {ts_srt(c["e"])}\n{t}\n')
    path.write_text('\n'.join(rows), encoding='utf-8')


def save_plan(path, spec, sides, report):
    path.write_text(json.dumps({'canvas': [spec.W, spec.H], 'pic': spec.pic, 'crop': spec.crop, 'band': spec.band,
                                'top': spec.top, 'bottom': spec.bottom, 'sides': sides, **report}, indent=1), encoding='utf-8')
