"""Cutting, composing and encoding: intermediate segments → final videos (subtitles, logo, subscribe banner,
cleaned + loudness-normalised audio, optional ducked music)."""
import hashlib, json, re, shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import cv2
import numpy as np
from tools import FFMPEG, ffmpeg, run, tonemap

FPS = 30


def inter_size(src_w, src_h):
    return (1920, 1080) if src_w >= src_h else (1080, 1920)


def make_inter(segs, clips, work, size, status, tag='main'):
    """One short lossless-ish file per kept range: SDR, 30 fps, picture size, 48 kHz audio with tiny fades."""
    d = work / ('inter' if tag == 'main' else f'inter-{tag}')
    d.mkdir(exist_ok=True)
    by_id = {c['id']: c for c in clips}
    W, H = size

    def one(k):
        g = segs[k]
        dst = d / f'{k:03d}.mkv'
        if dst.exists():
            return
        c = by_id[g['clip']]
        dur = g['b'] - g['a']
        vf = (tonemap(c['transfer']) + ',' if c['hdr'] else '') + \
            f'fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},format=yuv420p,setsar=1'
        af = f'aresample=48000,afade=t=in:d=0.04,afade=t=out:st={max(0, dur - 0.08):.3f}:d=0.08'
        tmp = d / f'{k:03d}.part.mkv'
        ffmpeg('-ss', f'{g["a"]:.3f}', '-i', c['path'], '-t', f'{dur:.3f}', '-map', '0:v:0', '-map', '0:a:0', '-vf', vf,
               '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv',
               '-c:v', 'libx264', '-crf', '12', '-preset', 'veryfast', '-af', af, '-c:a', 'pcm_s16le', '-ar', '48000', tmp)
        tmp.rename(dst)

    status.log(f'cutting {len(segs)} segments')
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(one, range(len(segs))))
    lst = d / 'list.txt'
    lst.write_text(''.join(f"file '{(d / f'{k:03d}.mkv').as_posix()}'\n" for k in range(len(segs))), encoding='utf-8')
    return lst, (lambda k: d / f'{k:03d}.mkv')


def find_logo(inter_path, n, logo_png):
    """Is the channel logo already burned into the recording? Template match in the top corners of a few frames.
    Returns its box in picture pixels or None."""
    tpl = cv2.imread(str(logo_png), cv2.IMREAD_UNCHANGED)
    found = []
    for k in sorted({0, n // 2, n - 1}):
        cap = cv2.VideoCapture(str(inter_path(k)))
        ok, fr = cap.read()
        if not ok:
            continue
        H, W = fr.shape[:2]
        gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        best = (0, None)
        for size in range(int(0.04 * H), int(0.16 * H), max(2, int(0.005 * H))):
            t = cv2.resize(tpl, (size, size), interpolation=cv2.INTER_AREA)
            tg = cv2.cvtColor(t[:, :, :3], cv2.COLOR_BGR2GRAY)
            mask = (t[:, :, 3] > 128).astype(np.uint8) * 255
            for x0 in (0, W - int(0.25 * W)):              # top-left and top-right quarter
                roi = gray[0:int(0.3 * H), x0:x0 + int(0.25 * W)]
                if roi.shape[0] < size or roi.shape[1] < size:
                    continue
                r = cv2.matchTemplate(roi, tg, cv2.TM_CCORR_NORMED, mask=mask)
                _, v, _, p = cv2.minMaxLoc(r)
                if v > best[0]:
                    best = (v, (x0 + p[0], p[1], x0 + p[0] + size, p[1] + size))
        found.append(best)
    good = [b for v, b in found if v > 0.93]
    return good[0] if len(good) >= max(1, len(found) - 1) else None


def find_banner(inter_path, segs, banner):
    """Is a subscribe banner already burned into the end of the recording? Multi-scale template match on a frame
    ~3 s before the end. Returns True/False."""
    k = len(segs) - 1
    cap = cv2.VideoCapture(str(inter_path(k)))
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0, segs[k]['b'] - segs[k]['a'] - 3) * 1000)
    ok, fr = cap.read()
    if not ok:
        return False
    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    tpl = cv2.imread(str(banner), cv2.IMREAD_UNCHANGED)
    best = 0.0
    for bw in range(int(0.15 * W), int(0.95 * W), max(4, int(0.02 * W))):
        bh = int(tpl.shape[0] * bw / tpl.shape[1])
        t = cv2.resize(tpl, (bw, bh), interpolation=cv2.INTER_AREA)
        if bh >= H:
            break
        r = cv2.matchTemplate(gray, cv2.cvtColor(t[:, :, :3], cv2.COLOR_BGR2GRAY), cv2.TM_CCOEFF_NORMED,
                              mask=(t[:, :, 3] > 128).astype(np.uint8) * 255)
        best = max(best, float(np.nan_to_num(r).max()))
    return best > 0.55          # real banner (re-encoded, scaled) ≈ 0.67; plain frames ≈ 0.3


def banner_png(subscribe_png, work):
    """The subscribe element cut out of the full-frame overlay the app draws."""
    from PIL import Image
    dst = work / 'banner.png'
    if not dst.exists():
        im = Image.open(subscribe_png).convert('RGBA')
        im.crop(im.split()[3].getbbox()).save(dst)
    return dst


def audio_chain(profile):
    nr = {'off': 0, 'light': 10, 'strong': 20}.get(profile.get('denoise', 'light'), 10)
    chain = 'highpass=f=70,' + (f'afftdn=nr={nr}:nf=-60:tn=1,' if nr else '')
    return chain + 'acompressor=threshold=0.1:ratio=2.5:attack=8:release=150:knee=4,alimiter=limit=0.15:attack=4:release=80:level=disabled'


def measure_loudness(lst, chain, work):
    p = work / f'loudnorm-{Path(lst).parent.name}.json'
    if p.exists():
        return json.loads(p.read_text())
    r = run([FFMPEG, '-hide_banner', '-f', 'concat', '-safe', '0', '-i', lst, '-vn', '-af',
             chain + ',loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'])
    m = json.loads(re.search(r'\{[^{}]*"input_i"[^{}]*\}', r.stderr, re.S).group(0))
    p.write_text(json.dumps(m))
    return m


def render(spec, lst, ass, profile, assets, total, cta, add_logo, banner_y, work, out_path, music=None, status=None, cards=()):
    """add_logo: draw the channel logo at spec.logo (False when the recording already shows it).
    banner_y: top edge of the subscribe banner (chosen away from her face and the subtitles).
    cards: word cards [(png, x, y, start, end)]."""
    W, H = spec.W, spec.H
    inputs = ['-f', 'concat', '-safe', '0', '-i', lst]
    fc = []
    cx, cy, cw, ch = spec.crop
    px, py, pw, ph = (int(round(v)) for v in spec.pic)
    if (px, py, pw, ph) == (0, 0, W, H) and tuple(spec.crop) == (0, 0, *spec.src_size):
        fc.append('[0:v]null[pic]')
    else:
        fc.append(f'[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40,'
                  f'eq=brightness=-0.06[bg];[b]crop={int(cw)}:{int(ch)}:{int(cx)}:{int(cy)},scale={pw}:{ph}:flags=lanczos[fg];'
                  f'[bg][fg]overlay={px}:{py}[pic]')
    esc = lambda p: Path(p).as_posix().replace(':', '\\:')      # D:/x → D\:/x inside the filter graph
    fc.append(f"[pic]subtitles='{esc(ass)}':fontsdir='{esc(assets['fonts'])}'[s]")
    last, n = '[s]', 1
    if add_logo:
        lx, ly, lx1, _ = (int(v) for v in spec.logo)
        inputs += ['-loop', '1', '-framerate', str(FPS), '-i', assets['logo']]
        fc.append(f'[{n}:v]scale={lx1 - lx}:-1,format=rgba,colorchannelmixer=aa=0.85[l];{last}[l]overlay={lx}:{ly}:shortest=1[sl]')
        last, n = '[sl]', n + 1
    for png, x, y, c0, c1 in cards:
        inputs += ['-loop', '1', '-framerate', str(FPS), '-t', f'{total:.2f}', '-i', png]
        fc.append(f'[{n}:v]format=rgba,fade=t=in:st={c0:.2f}:d=0.3:alpha=1,fade=t=out:st={c1 - 0.3:.2f}:d=0.3:alpha=1[k{n}];'
                  f"{last}[k{n}]overlay={int(x)}:{int(y)}:enable='between(t,{c0 - 0.05:.2f},{c1:.2f})'[w{n}]")
        last, n = f'[w{n}]', n + 1
    # subscribe banner from the call to action to the end
    b0 = max(0.0, min(cta if cta is not None else total - 10, total - 6))
    b1 = total - 0.9
    if banner_y is None:                                  # the recording already shows one
        fc.append(f'{last}format=yuv420p[v]')
    else:
        inputs += ['-loop', '1', '-framerate', str(FPS), '-i', assets['banner']]
        fc.append(f'[{n}:v]scale={banner_width(spec)}:-1,format=rgba,fade=t=in:st={b0:.2f}:d=0.4:alpha=1,fade=t=out:st={b1 - 0.4:.2f}:d=0.4:alpha=1[bn];'
                  f"{last}[bn]overlay=(W-w)/2:{int(banner_y)}:shortest=1:enable='between(t,{b0 - 0.1:.2f},{b1:.2f})',format=yuv420p[v]")
        n += 1
    # audio
    chain = audio_chain(profile)
    m = measure_loudness(lst, chain, work)
    norm = (f"loudnorm=I={profile.get('loudness', -14)}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    if music:
        inputs += ['-stream_loop', '-1', '-i', music]
        vol = profile.get('music', {}).get('volume', 0.12)
        fc.append(f'[0:a]{chain},{norm},aresample=48000,asplit[voice][key];'
                  f'[{n}:a]aresample=48000,volume={vol},afade=t=in:d=1.5,afade=t=out:st={total - 3:.2f}:d=3,atrim=0:{total:.3f}[mus];'
                  f'[mus][key]sidechaincompress=threshold=0.02:ratio=8:attack=60:release=600[duck];'
                  f'[voice][duck]amix=inputs=2:duration=first:normalize=0[a]')
    else:
        fc.append(f'[0:a]{chain},{norm},aresample=48000[a]')
    # same inputs as last time and the file is still there → nothing to do
    key = hashlib.md5(json.dumps([inputs, fc, ass.read_text(encoding='utf-8-sig'), total], default=str).encode()).hexdigest()
    key_file = work / f'render-{out_path.stem}.key'
    if out_path.exists() and key_file.exists() and key_file.read_text() == key:
        status.log(f'{out_path.name} is up to date')
        return
    status.log(f'rendering {out_path.name}')
    tmp = work / f'{out_path.stem}.part.mp4'          # render on the PC; Drive only gets the finished file
    ffmpeg(*inputs, '-filter_complex', ';'.join(fc), '-map', '[v]', '-map', '[a]', '-t', f'{total:.3f}',
           '-c:v', 'libx264', '-preset', 'slow', '-crf', '19', '-profile:v', 'high', '-pix_fmt', 'yuv420p', '-r', str(FPS), '-g', '60',
           '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv',
           '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2', '-movflags', '+faststart', tmp)
    shutil.copyfile(tmp, out_path)
    tmp.unlink()
    key_file.write_text(key)


def banner_width(spec):
    return int(0.9 * spec.W) if spec.H > spec.W else int(0.45 * spec.W)


def banner_height(spec):
    return banner_width(spec) * 216 / 1070          # the element is 1070x216


def logo_box(spec):
    """Where our logo goes: Shorts top-left under YouTube's top bar (as in V1/V2), long videos top-right."""
    lw = int(0.075 * min(spec.W, spec.H) * (1.8 if spec.H > spec.W else 1.3))
    lx, ly = (40, int(0.09 * spec.H)) if spec.short else (spec.W - lw - int(0.025 * spec.W), int(0.035 * spec.H))
    return (lx, ly, lx + lw, ly + lw)
