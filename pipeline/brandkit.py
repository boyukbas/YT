"""One-time channel art in the channel style: YouTube banner + watermark, Facebook Page cover,
Instagram highlight covers. Run again only when the style or the photo changes.
    python -I brandkit.py <channel folder (_channel)> <photo of Sinem> <out folder>"""
import sys
from pathlib import Path
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


def person(cv, cut, h, x, y_bottom, fade=0):
    """Cut-out with the thumbnails' white outline. Its cut bottom edge sits on the picture's bottom edge,
    or (fade > 0) the body fades out in a soft oval around the head (so a photo cropped through the shoulders shows
    no straight edges); the outline fades out first."""
    p = cut.resize((int(cut.width * h / cut.height), h), Image.LANCZOS)
    a = p.split()[3]
    line = a.filter(ImageFilter.MaxFilter(9))
    if fade:
        def oval(shrink):                      # 255 inside an oval around head + shoulders, soft edge `fade` wide
            m = Image.new('L', p.size, 0)
            w = p.width
            ImageDraw.Draw(m).ellipse((-0.05 * w + shrink, -0.6 * h + shrink, 1.05 * w - shrink, 0.88 * h - shrink), fill=255)
            return m.filter(ImageFilter.GaussianBlur(fade / 8))
        a = Image.composite(a, Image.new('L', p.size, 0), oval(0))
        line = Image.composite(line, Image.new('L', p.size, 0), oval(fade // 4))
        p.putalpha(a)
    sil = Image.new('RGBA', p.size, WHITE + (0,))
    sil.putalpha(line)
    cv.alpha_composite(sil, (x, y_bottom - h))
    cv.alpha_composite(p, (x, y_bottom - h))


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


def chips(d, x, y, items, size):
    f = font(size, F_BOLD)
    for i, t in enumerate(items):
        w = d.textbbox((0, 0), t, font=f)[2]
        d.rounded_rectangle((x, y, x + w + size * 1.2, y + size * 1.8), radius=size * 0.9,
                            fill=RED if i == 0 else (34, 42, 70))
        d.text((x + size * 0.6, y + size * 0.35), t, font=f, fill=WHITE)
        x += w + size * 1.2 + size * 0.5
    return x


TAGLINE = 'Türkçe anlatımla Almanca'
TOPICS = ['Tercüman Sinem ile', 'Her seviye', 'Sınav hazırlık', 'Almanya\'da hayat']   # not tied to one level


def banner(cut, logo, out):
    """2560×1440; everything that matters inside the 1546×423 area every device shows."""
    W, H = 2560, 1440
    sx, sy, sw, sh = (W - 1546) // 2, (H - 423) // 2, 1546, 423
    cv = canvas(W, H, (sx + sw - 700, sy - 160, sx + sw + 160, sy + sh + 200))
    d = ImageDraw.Draw(cv)
    person(cv, cut, 640, sx + sw - 540, sy + 15 + 640, fade=330)   # head inside the band, body fades out below it
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
    person(cv, cut, 640, sx + 580, H - 18, fade=300)
    stripe(d, 0, W, H - 18)
    name_block(cv, sx + 10, 120, 0.85, logo)
    d.text((sx + 14, 330), TAGLINE, font=font(42), fill=WHITE)
    chips(d, sx + 14, 410, [t for t in TOPICS if t != 'Her seviye'], 22)      # less room next to her here
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
    """Option to test: Sinem's face as the profile picture, the TR-DE mark as a badge (shows as a circle)."""
    W = 1080
    cv = canvas(W, W, (140, 120, 940, 920))
    person(cv, cut, 1000, (W - int(cut.width * 1000 / cut.height)) // 2 + 40, W + 60)
    cv.alpha_composite(Image.open(logo).convert('RGBA').resize((300, 300), Image.LANCZOS), (110, 640))
    m = Image.new('L', (W, W), 0)
    ImageDraw.Draw(m).ellipse((0, 0, W, W), fill=255)
    cv.putalpha(m)
    cv.save(out / 'profile-picture-face-option.png')


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
    cut, left = cut_out(photo)
    banner(cut, logo, out)
    fb_cover(cut, logo, out)
    highlights(out)
    face_avatar(cut, logo, out)
    watermark(logo, out)
    sheet(out)
    print('written to', out)
