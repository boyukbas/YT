"""Channel thumbnail in the approved format (first made for V1):
dark navy + soft yellow glow, cut-out presenter with white outline, red level badge,
German keywords big in yellow, a white Turkish question, TR-DE logo, German-flag stripe."""
from collections import deque
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

YELLOW, WHITE, RED, NAVY = (255, 210, 26), (255, 255, 255), (227, 10, 23), (16, 22, 40)
F_BLACK = r'C:\Windows\Fonts\ariblk.ttf'      # Arial Black: has ç ğ ı ş ö ü ä ß
_session = None


def cut_out(path):
    """Presenter without background; keeps only the largest connected part.
    Also says whether she touches the left/right frame edge (→ mirror so the straight edge runs off the canvas)."""
    global _session
    from rembg import remove, new_session
    _session = _session or new_session('isnet-general-use')
    cut = remove(Image.open(path).convert('RGB'), session=_session, post_process_mask=True)
    a = np.array(cut.split()[3])
    small = np.array(Image.fromarray(a).resize((a.shape[1] // 4, a.shape[0] // 4))) > 100
    h, w = small.shape
    label = np.zeros(small.shape, dtype=np.int32)
    sizes, n = {}, 0
    for y0 in range(h):
        for x0 in range(w):
            if small[y0, x0] and not label[y0, x0]:
                n += 1; q = deque([(y0, x0)]); label[y0, x0] = n; cnt = 0
                while q:
                    y, x = q.popleft(); cnt += 1
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        yy, xx = y + dy, x + dx
                        if 0 <= yy < h and 0 <= xx < w and small[yy, xx] and not label[yy, xx]:
                            label[yy, xx] = n; q.append((yy, xx))
                sizes[n] = cnt
    keep = Image.fromarray(((label == max(sizes, key=sizes.get)) * 255).astype(np.uint8)).resize(cut.size).filter(ImageFilter.MaxFilter(9))
    a = np.minimum(a, np.array(keep))
    top = a[: int(0.75 * a.shape[0])]                   # the bottom edge is always cut (shoulders); look above it
    touches_left = (top[:, :3] > 128).sum() > 20
    cut.putalpha(Image.fromarray(a))
    return cut.crop(cut.getbbox()), touches_left


def place_person(canvas, cut, ph, x, y):
    pw = int(cut.width * ph / cut.height)
    person = cut.resize((pw, ph), Image.LANCZOS)
    outline = person.split()[3].filter(ImageFilter.MaxFilter(15))
    sil = Image.new('RGBA', person.size, WHITE + (0,)); sil.putalpha(outline)
    shadow = Image.new('RGBA', person.size, (0, 0, 0, 0)); shadow.putalpha(outline.filter(ImageFilter.GaussianBlur(18)).point(lambda v: v * 0.6))
    x = x if x >= 0 else canvas.width - pw + (-x)   # negative x = offset past the right edge
    canvas.alpha_composite(shadow, (x + 10, y + 10))
    canvas.alpha_composite(sil, (x, y))
    canvas.alpha_composite(person, (x, y))


def background(W, H, glow_box):
    bg = Image.new('RGB', (W, H), NAVY)
    glow = Image.new('L', (W, H), 0)
    ImageDraw.Draw(glow).ellipse(glow_box, fill=255)
    bg = Image.composite(Image.new('RGB', (W, H), (70, 60, 20)), bg, glow.filter(ImageFilter.GaussianBlur(120)))
    d = ImageDraw.Draw(bg)
    for i, c in enumerate([(0, 0, 0), (221, 0, 0), (255, 206, 0)]):   # German flag stripe
        d.rectangle((0, H - 18 + i * 6, W, H - 12 + i * 6), fill=c)
    return bg.convert('RGBA')


def fit(d, s, max_w, start):
    size = start
    while size > 40 and d.textbbox((0, 0), s, font=ImageFont.truetype(F_BLACK, size), stroke_width=10)[2] > max_w:
        size -= 4
    return size


def make(photo, words, question, badge, logo, out, vertical=False, cut=None, palette=None):
    """words: 1-3 German keywords. Writes out (+ a 1/4 size preview). `cut`: reuse a cut-out (cut_out(photo))."""
    global YELLOW, WHITE, RED, NAVY
    if palette:
        h = lambda k: tuple(int(palette[k]['hex'].lstrip('#')[i:i + 2], 16) for i in (0, 2, 4))
        YELLOW, WHITE, RED, NAVY = h('yellow'), h('white'), h('red'), h('navy')
    cut, touches_left = cut or cut_out(photo)
    mirror = touches_left
    if mirror:
        cut = cut.transpose(Image.FLIP_LEFT_RIGHT)
    if vertical:
        W, H = 1080, 1920
        canvas = background(W, H, (40, 800, 1040, 1900))
        place_person(canvas, cut, 1080, -80 if mirror else (W - int(cut.width * 1080 / cut.height)) // 2, H - 1080 + 40)
        left, top, max_w = 70, 190, 940
    else:
        W, H = 1280, 720
        canvas = background(W, H, (700, 40, 1300, 760))
        left, top, max_w = 44, 40, 660
        if cut.height / cut.width >= 1.3:                  # tall cut-out (phone recording): V1 layout, off the bottom
            place_person(canvas, cut, 980, -100, 22)
        else:                                              # wide cut-out (landscape recording): right side, on the bottom
            ph = int(580 * cut.height / cut.width)
            place_person(canvas, cut, ph, W - 580 - 12, H - ph + 4)
            max_w = 620
    d = ImageDraw.Draw(canvas)

    def text(xy, s, size, fill, stroke):
        f = ImageFont.truetype(F_BLACK, size)
        d.text(xy, s, font=f, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
        return d.textbbox(xy, s, font=f, stroke_width=stroke)

    k = 1.4 if vertical else 1
    f = ImageFont.truetype(F_BLACK, int(40 * k))
    tb = d.textbbox((0, 0), badge, font=f)
    d.rounded_rectangle((left + 4, top, left + 4 + tb[2] + 40 * k, top + 66 * k), radius=16 * k, fill=RED)
    d.text((left + 4 + 20 * k, top + 8 * k), badge, font=f, fill=WHITE)
    size = min(fit(d, w, max_w, int((150 if len(words) <= 2 else 128) * k)) for w in words)
    qsize = fit(d, question, max_w, int(84 * k))
    f_k = ImageFont.truetype(F_BLACK, size)
    block = sum(d.textbbox((0, 0), w, font=f_k, stroke_width=10)[3] - 14 for w in words) + 14 \
        + d.textbbox((0, 0), question, font=ImageFont.truetype(F_BLACK, qsize), stroke_width=9)[3]
    space_top, space_bottom = top + int(88 * k), (H - 40 if not vertical else 780)
    y = space_top + max(0, (space_bottom - space_top - block) // 2)
    for w in words:
        y = text((left, y), w, size, YELLOW, 10)[3] - 14
    text((left + 6, y + 14), question, qsize, WHITE, 9)
    lg = Image.open(logo).convert('RGBA').resize((96, 96), Image.LANCZOS)
    canvas.alpha_composite(lg, (W - 96 - 28, (H - 96 - 34) if not vertical else 190))
    img = canvas.convert('RGB')
    img.save(out, quality=92)
    img.resize((W // 4, H // 4), Image.LANCZOS).save(str(out).replace('.jpg', '_small_preview.jpg'), quality=90)
