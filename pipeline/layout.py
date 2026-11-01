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
    # a box far taller than usual is a misdetection (hand, two faces merged): treat it as missing
    hs = sorted(b[3] - b[1] for b in out if b)
    if hs:
        med = hs[len(hs) // 2]
        out = [b if b and b[3] - b[1] <= 1.6 * med else None for b in out]
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
        self.lifted = 0
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

    studio = False
    panel = None

    def to_canvas(self, box):
        cx, cy, cw, ch = self.crop
        px, py, pw, ph = self.pic
        sx, sy = pw / cw, ph / ch
        return ((box[0] - cx) * sx + px, (box[1] - cy) * sy + py, (box[2] - cx) * sx + px, (box[3] - cy) * sy + py)


def studio(name, src_w, src_h):
    """Long video from a vertical recording: 16:9 canvas on the animated channel background, her picture as a card on
    the left, a panel on the right for the section header, word cards and subtitles (text never touches her)."""
    W, H = 1920, 1080
    ph = int(0.926 * H)
    pw = int(ph * src_w / src_h)
    s = Spec(name, W, H, False, src_w, src_h, pic=(110, (H - ph) // 2, pw, ph))
    s.studio = True
    s.panel = (110 + pw + 90, W - 70)                    # x range of the text panel
    s.margin_l, s.margin_r = s.panel[0], W - s.panel[1]
    s.maxw = s.panel[1] - s.panel[0] - 40
    s.size, s.tsize = 64, 46
    s.top, s.bottom = int(0.30 * H), int(0.90 * H)
    return s


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


def clean(text):
    """Text without the markers: [German inside Turkish], <the lesson's pattern>."""
    return ' '.join(text.translate(str.maketrans('', '', '[]<>')).split())


def lines_of(text, f, maxw, de=False):
    """'Turkish [Deutsch <zu lernen>] text' → wrapped lines of [(word, is_german, is_pattern)]."""
    words, hl = [], False
    for tok in text.replace('[', ' [ ').replace(']', ' ] ').replace('<', ' < ').replace('>', ' > ').split():
        if tok in ('[', ']'):
            de = tok == '['
        elif tok in ('<', '>'):
            hl = tok == '<'
        elif words and all(ch in '.,;:!?…' for ch in tok):
            words[-1] = (words[-1][0] + tok,) + words[-1][1:]      # "<zu lernen>." → "lernen."
        else:
            words.append((tok, de, hl))
    rows = wrap([w[0] for w in words], maxw, f.getlength)
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
            main = lines_of(c['text'].replace('[', '').replace(']', ''), fm, spec.maxw, de=True)
            trn = lines_of(clean(c['tr']), ft, spec.maxw) if c.get('tr') else []
        else:
            main, trn = lines_of(c['text'], fm, spec.maxw), []
        width = max([fm.getlength(' '.join(w[0] for w in l)) for l in main] + [ft.getlength(' '.join(w[0] for w in l)) for l in trn])
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


def lift(spec, faces, blks, clear):
    """Shorts: move the picture up just enough that every subtitle fits under her chin at the bottom anchor. The strip
    it leaves at the bottom sits under YouTube's title area. clear: her face may not rise above this canvas y (the
    logo + hook row). Returns True when it fits."""
    need = float(max((no_go(spec.to_canvas(f))[3] - text_box(spec, b, "bottom")[1] for b, f in zip(blks, faces) if f), default=0))
    room = min(spec.to_canvas(f)[1] for f in faces if f) - clear if any(faces) else 0
    if need <= 0:
        return True
    if need + 8 > min(room, 0.16 * spec.H):
        return False
    px, py, pw, ph = spec.pic
    spec.pic = (px, py - need - 8, pw, ph)
    spec.lifted = need + 8
    return True


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


def board(spec, cues, sections, total, colors, fonts_dir):
    """Studio panel: the section's example sentences build up as she says them (German + Turkish; red bar = question,
    yellow bar = answer) and clear at the next section, so the panel shows what this part taught. Short complete
    German sentences only. Returns ASS dialogue lines."""
    x0, y0, y1 = spec.panel[0], int(0.215 * spec.H), int(0.69 * spec.H)
    gs, ts_ = 50, 36
    fg, ft = font(fonts_dir, 1, 0, gs), font(fonts_dir, 0, 1, ts_)
    maxw = spec.panel[1] - x0 - 40
    starts = sorted({0.0} | {x['s'] for x in sections})
    items, seen = [], set()
    for c in cues:
        de, tr = clean(c['text']), clean(c.get('tr', ''))
        part = max(t for t in starts if t <= c['s'] + 0.01)
        if c['lang'] != 'de' or not tr or not de[:1].isupper() or not de.endswith(('?', '.', '!', '…')) \
                or len(de.split()) > 6 or (part, de) in seen:
            continue
        seen.add((part, de))
        dl, trl = wrap(de.split(), maxw, fg.getlength), wrap(tr.split(), maxw, ft.getlength)
        items.append({'s': c['s'], 'part': part, 'de': de, 'q': de.endswith('?'), 'h': (len(dl) * gs + len(trl) * ts_) * 1.2 + 22,
                      'dl': r'\N'.join(map(' '.join, dl)), 'tl': r'\N'.join(map(' '.join, trl))})
    yel, red, tl = ass_color(colors['de']), ass_color('#E30A17'), ass_color(colors['translation'])
    out, prev = [], {}
    for k, it in enumerate(items):                       # one layout per arrival, until the next arrival / section end
        end = min([x['s'] for x in items[k + 1:k + 2]] + [t for t in starts if t > it['s']] + [total])
        shown = [x for x in items[:k + 1] if x['part'] == it['part']]
        while sum(x['h'] for x in shown) > y1 - y0 and len(shown) > 1:
            shown = shown[1:]                            # the oldest scrolls away
        y, now = y0, {}
        for x in shown:
            now[x['de']] = y
            new, was, tx = x is it, prev.get(x['de'], y), x0 + 28
            pos = (rf'\move({tx + 40},{y},{tx},{y},0,300)' if new else
                   rf'\move({tx},{was},{tx},{y},0,300)' if was != y else rf'\pos({tx},{y})')
            bpos = rf'\move({x0},{was + 6},{x0},{y + 6},0,300)' if was != y else rf'\pos({x0},{y + 6})'
            fade = r'\fad(250,0)' if new else ''
            bh = int(x['h'] - 34)
            bar = rf'{{\an7{bpos}{fade}\bord0\shad0\1c{red if x["q"] else yel}&\p1}}m 0 0 l 10 0 10 {bh} 0 {bh}{{\p0}}'
            txt = rf'{{\an7{pos}{fade}\q2\fs{gs}\b1\c{yel}&\bord2\shad1}}{x["dl"]}\N{{\fs{ts_}\b0\i1\c{tl}&}}{x["tl"]}'
            out.append(f'Dialogue: 0,{ts_ass(it["s"])},{ts_ass(end)},board,,0,0,0,,{bar}\n')
            out.append(f'Dialogue: 0,{ts_ass(it["s"])},{ts_ass(end)},board,,0,0,0,,{txt}\n')
            y += int(x['h'])
        prev = now
    return out


def write_ass(path, spec, cues, blks, sides, colors, extra=()):
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
Style: board,Arial,46,{de},{de},&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,2,1,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""]
    bord = round(spec.size / 12.8, 1)
    hl = ass_color(colors['pattern'])

    def line(words):
        """One subtitle row; tags only where the word kind changes. The lesson's pattern gets a thick outline in the
        accent colour, like a highlighter."""
        s, state = '', None
        for j, (w, isde, mark) in enumerate(words):
            if (isde, mark) != state:
                look = (r'\3c' + hl + r'&\bord' + str(round(bord * 1.9, 1))) if mark else (r'\3c&H000000&\bord' + str(bord))
                s += '{' + r'\c' + (de if isde else tr) + '&' + look + '}'
                state = (isde, mark)
            s += (' ' if j else '') + w
        return s

    for c, b, side in zip(cues, blks, sides):
        body = r'\N'.join(line(l) for l in b['main'])
        if b['trn']:
            body += r'\N{\fs' + str(spec.tsize) + r'\b0\i1\c' + tl + r'&\3c&H000000&\bord' + str(round(bord * 0.75, 1)) + '}' + \
                r'\N'.join(' '.join(w[0] for w in l) for l in b['trn'])
        out.append(f'Dialogue: 0,{ts_ass(c["s"])},{ts_ass(c["e"])},{side},,0,0,0,,{{\\fad(120,60)}}{body}\n')
    out += extra
    path.write_text(''.join(out), encoding='utf-8-sig')


def write_srt(path, cues, lang):
    """lang 'tr': all Turkish (German lines → their translation); 'de': all German; 'spoken': as spoken."""
    rows = []
    for c in cues:
        plain = clean(c['text'])
        if lang == 'spoken':
            t = plain
        elif lang == c['lang']:
            t = plain
        else:
            t = clean(c.get('tr' if lang == 'tr' else 'de') or '') or plain
        words = t.split()
        t = '\n'.join(' '.join(x) for x in wrap(words, 42, len))
        rows.append(f'{len(rows) + 1}\n{ts_srt(c["s"])} --> {ts_srt(c["e"])}\n{t}\n')
    path.write_text('\n'.join(rows), encoding='utf-8')


def save_plan(path, spec, sides, report):
    path.write_text(json.dumps({'canvas': [spec.W, spec.H], 'pic': spec.pic, 'crop': spec.crop, 'band': spec.band,
                                'top': spec.top, 'bottom': spec.bottom, 'sides': sides, **report}, indent=1), encoding='utf-8')
