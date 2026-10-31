"""Word cards: a harder German word + its Turkish meaning, shown for a few seconds where nothing important is."""
from PIL import Image, ImageDraw, ImageFont
import layout as L


def rgb(hex_):
    h = hex_.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def draw(card, palette, H, path, fonts_dir):
    """Navy card, red edge, small 'KELİME' label, German word in yellow, meaning in white. Returns (w, h)."""
    s = H / 1080 if H < 1500 else 1920 / 1080 * 0.62      # vertical canvases: a little smaller than long videos
    f_lab = ImageFont.truetype(str(fonts_dir / 'arialbd.ttf'), int(22 * s))
    f_word = ImageFont.truetype(str(fonts_dir / 'arialbd.ttf'), int(52 * s))
    f_tr = ImageFont.truetype(str(fonts_dir / 'arial.ttf'), int(38 * s))
    pad, edge = int(26 * s), int(10 * s)
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    ww = max(probe.textlength(card['word'], font=f_word), probe.textlength(card['tr'], font=f_tr), probe.textlength('KELİME', font=f_lab))
    w = int(ww + 2 * pad + edge)
    h = int(pad * 2 + 22 * s + 10 * s + 52 * s * 1.15 + 38 * s * 1.2)
    im = Image.new('RGBA', (w + 12, h + 12), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 10, w + 8, h + 10), radius=int(22 * s), fill=(0, 0, 0, 90))            # soft shadow
    d.rounded_rectangle((0, 0, w, h), radius=int(22 * s), fill=rgb(palette['navy']['hex']) + (238,))
    d.rounded_rectangle((0, 0, edge * 2, h), radius=int(22 * s), fill=rgb(palette['red']['hex']) + (255,))
    d.rectangle((edge, 0, edge * 2, h), fill=rgb(palette['navy']['hex']) + (238,))
    x, y = edge + pad, pad
    d.text((x, y), 'KELİME', font=f_lab, fill=rgb(palette['red']['hex']))
    y += int(22 * s + 10 * s)
    d.text((x, y), card['word'], font=f_word, fill=rgb(palette['yellow']['hex']))
    y += int(52 * s * 1.15)
    d.text((x, y), card['tr'], font=f_tr, fill=rgb(palette['white']['hex']))
    im.save(path)
    return im.size


def draw_label(sec, palette, H, path, fonts_dir):
    """Section label: navy pill, red square with the part number (or "!"), title in white. Returns (w, h)."""
    s = H / 1080 if H < 1500 else 1920 / 1080 * 0.62
    f_tag = ImageFont.truetype(str(fonts_dir / 'arialbd.ttf'), int(44 * s))
    f_t = ImageFont.truetype(str(fonts_dir / 'arialbd.ttf'), int(40 * s))
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    box, pad = int(64 * s), int(14 * s)
    w = int(pad + box + 22 * s + probe.textlength(sec['title'], font=f_t) + 30 * s)
    h = box + 2 * pad
    im = Image.new('RGBA', (w + 12, h + 12), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 10, w + 8, h + 10), radius=h // 2, fill=(0, 0, 0, 90))
    d.rounded_rectangle((0, 0, w, h), radius=h // 2, fill=rgb(palette['navy']['hex']) + (238,))
    d.rounded_rectangle((pad, pad, pad + box, pad + box), radius=int(14 * s), fill=rgb(palette['red']['hex']))
    d.text((pad + box / 2, pad + box / 2), sec['tag'], font=f_tag, fill=(255, 255, 255), anchor='mm')
    d.text((pad + box + 22 * s, h / 2), sec['title'], font=f_t, fill=rgb(palette['white']['hex']), anchor='lm')
    im.save(path)
    return im.size


def place(spec, size, card, cues, blks, sides, faces, banner, taken=()):
    """First free spot (canvas pixels) for the card during its time, or None.
    Never on her face, the subtitles on screen, the logo, the subscribe banner, YouTube's Shorts buttons or another
    card/label shown at the same time (taken: [(box, start, end)])."""
    w, h = size
    W, H = spec.W, spec.H
    m = int(0.04 * min(W, H))
    if spec.short:
        spots = [(m, int(0.40 * H)), (m, int(0.55 * H)), (m, int(0.25 * H)), (W - w - 190, int(0.25 * H))]
    else:
        mid, low = int(0.42 * H), int(0.62 * H)
        spots = [(m, mid), (W - w - m, mid), (m, low), (W - w - m, low)]
    now = [i for i, c in enumerate(cues) if c['e'] > card['s'] and c['s'] < card['e']]
    blocked = [L.no_go(spec.to_canvas(faces[i])) for i in now if faces[i]]
    blocked += [L.text_box(spec, blks[i], sides[i]) for i in now]
    if spec.logo:
        blocked.append(spec.logo)
    if banner and banner[1] < card['e']:
        blocked.append(banner[0])
    blocked += [b for b, s0, s1 in taken if s0 < card['e'] and s1 > card['s']]
    if spec.short:                                  # Shorts: like/comment/share column, title area, top bar
        blocked += [(907, 806, W, 1517), (0, 1517, W, H), (0, 0, W, int(0.08 * H))]
    for x, y in spots:
        box = (x - 8, y - 8, x + w + 8, y + h + 8)
        if box[0] >= 0 and box[2] <= W and box[3] <= H and not any(L.overlaps(box, b) for b in blocked):
            return x, y
    return None
