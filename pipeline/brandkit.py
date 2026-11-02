"""One-time channel art in the channel style: YouTube banner + watermark, Facebook Page cover,
Instagram highlight covers. Run again only when the style or the photo changes.
    python -I brandkit.py <channel folder (_channel)> <photo of Sinem> <out folder>"""
import sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
sys.path.insert(0, str(Path(__file__).parent))
from thumbnail import YELLOW, WHITE, RED, NAVY, F_BLACK, cut_out

F_BOLD = r'C:\Windows\Fonts\arialbd.ttf'
F_SYMBOL = r'C:\Windows\Fonts\seguisym.ttf'
GREEN = (47, 211, 107)
FLAG = [(0, 0, 0), (221, 0, 0), (255, 206, 0)]


def font(size, f=F_BLACK):
    return ImageFont.truetype(f, size)


def canvas(W, H, glow=None):
    bg = Image.new('RGB', (W, H), NAVY)
    if glow:
        m = Image.new('L', (W, H), 0)
        ImageDraw.Draw(m).ellipse(glow, fill=255)
        bg = Image.composite(Image.new('RGB', (W, H), (70, 60, 20)), bg, m.filter(ImageFilter.GaussianBlur(W // 20)))
    return bg.convert('RGBA')


def stripe(d, x0, x1, y, h=6):
    for i, c in enumerate(FLAG):
        d.rectangle((x0, y + i * h, x1, y + (i + 1) * h - 1), fill=c)


def clean_edges(cut):
    """Bright, colourless pixels near the outline are background (sky) caught between curls: make them see-through."""
    a = np.array(cut.split()[3])
    rgb = np.array(cut.convert('RGB')).astype(int)
    band = max(5, cut.height // 30) | 1
    inner = np.array(Image.fromarray(((a > 128) * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(band))) > 0
    mx, mn = rgb.max(2), rgb.min(2)
    sky = (mx > 170) & ((mx - mn) / np.maximum(mx, 1) < 0.18) & ~inner
    out = cut.copy()
    out.putalpha(Image.fromarray(np.where(sky, 0, a).astype(np.uint8)))
    return out


def find_face(cut):
    """Face box (x0, y0, x1, y1) in the cut-out (YuNet, as in the videos); a guess if none is found."""
    import cv2
    from layout import detector
    k = min(1, 1000 / max(cut.size))
    small = Image.new('RGB', cut.size, (128, 128, 128))
    small.paste(cut, (0, 0), cut)
    small = small.resize((int(cut.width * k), int(cut.height * k)))
    frame = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2BGR)
    _, f = detector(Path(__file__).parent / 'models' / 'face_detection_yunet_2023mar.onnx', *small.size).detect(frame)
    if f is None:
        return 0.3 * cut.width, 0.15 * cut.height, 0.7 * cut.width, 0.45 * cut.height
    r = max(f, key=lambda r: r[2] * r[3])
    return r[0] / k, r[1] / k, (r[0] + r[2]) / k, (r[1] + r[3]) / k


def person(cv, cut, face, fh, cx, top, fade=0):
    """Places the cut-out so her face is `fh` pixels tall, centred on x = cx, face top at y = top (same framing from
    any photo). Softly feathered edge (no outline: it turns curly hair into a jagged white line). fade > 0: the body
    fades out in a soft oval around head and shoulders, and the photo's own left/right edges fade too."""
    s = fh / (face[3] - face[1])
    p = cut.resize((round(cut.width * s), round(cut.height * s)), Image.LANCZOS)
    w, h = p.size
    fx0, fy0, fx1, fy1 = (v * s for v in face)
    a = p.split()[3].filter(ImageFilter.GaussianBlur(max(1, fh / 250)))
    if fade:
        fw, fcx = fx1 - fx0, (fx0 + fx1) / 2
        m = Image.new('L', p.size, 0)          # 255 inside an oval around head + shoulders
        ImageDraw.Draw(m).ellipse((fcx - 1.9 * fw, fy0 - 2.5 * fh, fcx + 1.9 * fw, fy1 + 2.2 * fh), fill=255)
        m = m.filter(ImageFilter.GaussianBlur(fade / 6))
        side = int(fade / 4)
        ramp = Image.linear_gradient('L').rotate(90, expand=True).resize((side, h))
        m.paste(Image.composite(m.crop((0, 0, side, h)), Image.new('L', (side, h), 0), ramp), (0, 0))
        m.paste(Image.composite(m.crop((w - side, 0, w, h)), Image.new('L', (side, h), 0),
                                ramp.transpose(Image.FLIP_LEFT_RIGHT)), (w - side, 0))
        bot = min(h // 3, int(fade / 2))       # and so does the photo's own bottom edge
        vr = Image.linear_gradient('L').transpose(Image.FLIP_TOP_BOTTOM).resize((w, bot))
        m.paste(Image.composite(m.crop((0, h - bot, w, h)), Image.new('L', (w, bot), 0), vr), (0, h - bot))
        a = Image.composite(a, Image.new('L', p.size, 0), m)
    p.putalpha(a)
    x, y = round(cx - (fx0 + fx1) / 2), round(top - fy0)
    layer = Image.new('RGBA', cv.size, (0, 0, 0, 0))
    layer.paste(p, (x, y))                     # may run past the picture's edges: paste clips, alpha_composite doesn't
    cv.alpha_composite(layer)


def name_block(cv, x, y, scale, logo):
    """Logo + channel name + promise line; returns the block's right edge."""
    d = ImageDraw.Draw(cv)
    ls = int(190 * scale)
    cv.alpha_composite(Image.open(logo).convert('RGBA').resize((ls, ls), Image.LANCZOS), (x, y))
    tx = x + ls + int(40 * scale)
    d.text((tx, y + int(4 * scale)), 'ALMANCA', font=font(int(80 * scale)), fill=YELLOW, stroke_width=4, stroke_fill=(0, 0, 0))
    d.text((tx, y + int(92 * scale)), 'DİL ATÖLYESİ', font=font(int(56 * scale)), fill=WHITE)
    stripe(d, tx + 4, tx + int(470 * scale), y + int(170 * scale), max(3, int(6 * scale)))
    return tx + d.textbbox((0, 0), 'ALMANCA', font=font(int(80 * scale)))[2]


def chips(d, x, y, items, size, accent=True):
    f = font(size, F_BOLD)
    for i, t in enumerate(items):
        w = d.textbbox((0, 0), t, font=f)[2]
        d.rounded_rectangle((x, y, x + w + size * 1.2, y + size * 1.8), radius=size * 0.9,
                            fill=RED if i == 0 and accent else (34, 42, 70))
        d.text((x + size * 0.6, y + size * 0.35), t, font=f, fill=WHITE)
        x += w + size * 1.2 + size * 0.5
    return x


TAGLINE = "A1'den C1'e Almanca"
TOPICS = ['Almanca gramer', 'Goethe & telc sınavları', 'Almanya\'da hayat']   # not tied to one level


def banner(cut, logo, out):
    """2560×1440; everything that matters inside the 1546×423 area every device shows."""
    W, H = 2560, 1440
    sx, sy, sw, sh = (W - 1546) // 2, (H - 423) // 2, 1546, 423
    cv = canvas(W, H, (sx + sw - 700, sy - 160, sx + sw + 160, sy + sh + 200))
    d = ImageDraw.Draw(cv)
    person(cv, cut, find_face(cut), 190, sx + sw - 290, sy + 120, fade=330)   # head inside the band every device shows
    stripe(d, 0, W, H - 18)
    name_block(cv, sx + 30, sy + 40, 1.0, logo)
    d.text((sx + 34, sy + 255), TAGLINE, font=font(50), fill=WHITE)
    chips(d, sx + 34, sy + 335, TOPICS, 30)
    cv.convert('RGB').save(out / 'youtube-banner.jpg', quality=92)


def fb_cover(cut, logo, out):
    """1640×624 (2× the 820×312 computer view); phones show the middle ~1110×624 (16:9), so all of it sits there."""
    W, H = 1640, 624
    sx = (W - 1110) // 2
    cv = canvas(W, H, (sx + 700, -100, sx + 1250, H + 100))
    d = ImageDraw.Draw(cv)
    person(cv, cut, find_face(cut), 210, sx + 870, 130, fade=300)
    stripe(d, 0, W, H - 18)
    name_block(cv, sx + 10, 120, 0.85, logo)
    d.text((sx + 14, 330), TAGLINE, font=font(42), fill=WHITE)
    chips(d, sx + 14, 395, TOPICS[:2], 22)                     # two rows: less room next to her here
    chips(d, sx + 14, 443, TOPICS[2:], 22, accent=False)
    cv.convert('RGB').save(out / 'facebook-cover.jpg', quality=92)


def highlights(out):
    """Instagram highlight covers: 1080×1920, Instagram shows a small circle from the middle: one big German word
    (yellow) over its Turkish name (white), as in the videos."""
    items = [('merhaba', [('Hallo!', YELLOW, F_BLACK)], 'TANIŞALIM'),
             ('hatalar', [('✘', RED, F_SYMBOL), ('✔', GREEN, F_SYMBOL)], 'HATALAR'),
             ('sinav', [('Prüfung', YELLOW, F_BLACK)], 'SINAV'),
             ('hayat', [('Alltag', YELLOW, F_BLACK)], "ALMANYA'DA HAYAT"),
             ('kelime', [('Wörter', YELLOW, F_BLACK)], 'KELİMELER')]
    for name, parts, small in items:
        W, H = 1080, 1920
        cv = canvas(W, H)
        d = ImageDraw.Draw(cv)
        cx, cy, r = W // 2, H // 2, 470
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=YELLOW, width=18)
        size = 330
        while True:                                               # as big as fits inside the circle
            fs = [font(size, f) for _, _, f in parts]
            ws = [d.textbbox((0, 0), t, font=f)[2] for (t, _, _), f in zip(parts, fs)]
            if sum(ws) + 40 * (len(parts) - 1) <= 2 * r - 280 or size <= 120:
                break
            size -= 10
        x = cx - (sum(ws) + 40 * (len(parts) - 1)) // 2
        for (t, c, _), f, w in zip(parts, fs, ws):
            d.text((x, cy - 40), t, font=f, fill=c, anchor='lm')
            x += w + 40
        f = font(70 if len(small) <= 10 else 52)
        d.text((cx, cy + 210), small, font=f, fill=WHITE, anchor='mm')
        stripe(d, cx - 180, cx + 180, cy + 280, 10)
        cv.convert('RGB').save(out / f'highlight-{name}.jpg', quality=92)


def face_avatar(cut, logo, out):
    """Option to test: Sinem's face as the profile picture, the TR-DE mark as a badge.
    Square JPG (what Instagram accepts); YouTube/Instagram/Facebook crop it to a circle themselves."""
    W = 1080
    cv = canvas(W, W, (140, 120, 940, 920))
    person(cv, cut, find_face(cut), 340, W // 2 + 40, 300, fade=400)
    cv.alpha_composite(Image.open(logo).convert('RGBA').resize((300, 300), Image.LANCZOS), (110, 640))
    cv.convert('RGB').save(out / 'profile-picture-face-option.jpg', quality=92)


def watermark(logo, out):
    """YouTube 'video watermark' (Studio → Customisation → Branding): 150×150, shown in the video's corner."""
    Image.open(logo).convert('RGBA').resize((150, 150), Image.LANCZOS).save(out / 'youtube-watermark.png')


def sheet(out):
    """All of it on one preview picture."""
    files = ['youtube-banner.jpg', 'facebook-cover.jpg'] + sorted(p.name for p in out.glob('highlight-*.jpg'))
    ims = [Image.open(out / f) for f in files]
    top = [im.resize((1280, int(im.height * 1280 / im.width))) for im in ims[:2]]
    hl = [im.resize((250, 444)) for im in ims[2:]]
    W = 1280
    S = Image.new('RGB', (W, sum(i.height for i in top) + 444 + 60), (128, 128, 128))
    y = 0
    for im in top:
        S.paste(im, (0, y)); y += im.height + 20
    for i, im in enumerate(hl):
        S.paste(im, (i * 256, y + 20))
    S.save(out / 'preview.jpg', quality=88)


if __name__ == '__main__':
    ch, photo, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    logo = ch / 'logo.png'
    cut = clean_edges(cut_out(photo)[0])
    banner(cut, logo, out)
    fb_cover(cut, logo, out)
    highlights(out)
    face_avatar(cut, logo, out)
    watermark(logo, out)
    sheet(out)
    print('written to', out)
