"""Easy mode on the home PC: a lesson folder in Drive in, a finished video + everything for YouTube out.

  python easy.py run  "J:\\My Drive\\SINEM-YouTube\\<lesson>"      process one lesson now
  python easy.py watch "J:\\My Drive\\SINEM-YouTube" [--once]      look for new lessons (Task Scheduler: --once every 10 min)

Drive layout:  SINEM-YouTube/_channel/channel.json (+ logo, banner, fonts, music/)  ·  SINEM-YouTube/<lesson>/*.mp4|mov
Results go to <lesson>/Output/ (or "Output (Easy)" when an Output folder made elsewhere already exists).
Optional <lesson>/settings.json, e.g. {"short": false} = no Short version for this lesson.
Work files stay on the PC in D:\\YT\\easy-work\\<lesson>.
"""
import argparse, hashlib, json, os, statistics, sys, time, traceback
from pathlib import Path
import cv2
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import chips
import cues as C
import layout as L
import llm
import render as R
import speech
import texts as T
import thumbnail
from tools import HERE, Status, VIDEO_EXT, ffmpeg, probe, run, FFMPEG, slug, ts

WORK_ROOT = Path(os.environ.get('PIPELINE_WORK', r'D:\YT\easy-work'))
FACE_MODEL = HERE / 'models' / 'face_detection_yunet_2023mar.onnx'
SHORT_MAX = 180.0
OURS = 'status.json'


def load_profile(root):
    ch = Path(root) / '_channel'
    p = json.loads((ch / 'channel.json').read_text(encoding='utf-8'))
    assets = {'logo': ch / p['logo'], 'subscribe': ch / p['subscribe'], 'fonts': ch / 'fonts', 'music': ch / 'music'}
    pal = p['palette']
    p['colors'] = {'tr': pal['white']['hex'], 'de': pal['yellow']['hex'], 'translation': pal['soft']['hex']}
    terms = ch / 'terms.txt'
    p['terms'] = [t.strip() for t in terms.read_text(encoding='utf-8').splitlines() if t.strip()] if terms.exists() else []
    return p, assets


def output_dir(lesson):
    out = lesson / 'Output'
    if out.exists() and not (out / OURS).exists():
        return lesson / 'Output (Easy)'
    return out


def lesson_videos(lesson):
    return sorted(p for p in lesson.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT)


def fingerprint(videos):
    return [[v.name, v.stat().st_size] for v in videos]


def choose_banner_y(spec, cues, blks, sides, faces, b0):
    """Top edge for the subscribe banner during [b0, end]: away from her face and the subtitles on screen."""
    bh = R.banner_height(spec)
    bw = R.banner_width(spec)
    x0 = (spec.W - bw) / 2
    cands = [int(spec.H * 0.95 - bh), int(spec.bottom - bh) if spec.short else int(0.6 * spec.H),
             int(spec.top + 0.12 * spec.H), int(0.5 * spec.H - bh / 2)]
    late = [i for i, c in enumerate(cues) if c['e'] > b0]
    for y in cands:
        box = (x0, y, x0 + bw, y + bh)
        if spec.logo and L.overlaps(box, spec.logo):
            continue
        if all(not L.overlaps(box, L.no_go(spec.to_canvas(faces[i]))) for i in late if faces[i]) and \
                all(not L.overlaps(box, L.text_box(spec, blks[i], sides[i])) for i in late):
            return y
    return cands[0]


def qa_check(video, spec, cues, blks, sides, work, name):
    """Looks at the finished video: in every subtitle line, does the text touch her face? + loudness + frame sheet."""
    cap = cv2.VideoCapture(str(video))
    bad = []
    for i, (c, b, s) in enumerate(zip(cues, blks, sides)):
        cap.set(cv2.CAP_PROP_POS_MSEC, (c['s'] + c['e']) / 2 * 1000)
        ok, fr = cap.read()
        if not ok:
            continue
        h, w = fr.shape[:2]
        _, f = L.detector(FACE_MODEL, w, h).detect(fr)
        if f is None:
            continue
        x, y, fw, fh = max(f, key=lambda r: r[2] * r[3])[:4]
        if L.overlaps(L.text_box(spec, b, s), L.no_go((x, y, x + fw, y + fh))):
            bad.append({'t': ts(c['s']), 'text': c['text']})
    r = run([FFMPEG, '-hide_banner', '-i', video, '-vn', '-af', 'ebur128=framelog=quiet', '-f', 'null', '-'])
    lufs = float(r.stderr.rsplit('I:', 1)[1].split('LUFS')[0])
    # frame sheet: 8 subtitle moments spread over the video
    picks = [cues[int(i * (len(cues) - 1) / 7)] for i in range(8)] if len(cues) >= 8 else cues
    tiles = []
    for c in picks:
        cap.set(cv2.CAP_PROP_POS_MSEC, (c['s'] + c['e']) / 2 * 1000)
        ok, fr = cap.read()
        if ok:
            im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            im.thumbnail((640, 640))
            tiles.append(im)
    cols = 4
    tw, th = tiles[0].size
    sheet = Image.new('RGB', (cols * tw + (cols - 1) * 8, ((len(tiles) + cols - 1) // cols) * (th + 8)), (255, 255, 255))
    for i, im in enumerate(tiles):
        sheet.paste(im, ((i % cols) * (tw + 8), (i // cols) * (th + 8)))
    sheet_path = work / f'check-{name}.jpg'
    sheet.save(sheet_path, quality=88)
    return {'lines': len(cues), 'face_covered': bad, 'loudness_lufs': lufs}, sheet_path


def process(lesson, root, log_only=False):
    lesson = Path(lesson)
    profile, assets = load_profile(root)
    out = output_dir(lesson)
    work = WORK_ROOT / slug(lesson.name)
    work.mkdir(parents=True, exist_ok=True)
    st = Status(out, work)
    videos = lesson_videos(lesson)
    st.set('working', f'Starting: {len(videos)} video(s)', inputs=fingerprint(videos), error='')
    name = slug(lesson.name)

    # 1. clips
    clips = []
    for i, v in enumerate(sorted(videos, key=lambda v: (probe(v)['created'] or '~', v.name)), 1):
        clips.append({'id': f'c{i}', 'path': str(v), 'name': v.name, **probe(v)})
    (work / 'clips.json').write_text(json.dumps(clips, indent=1), encoding='utf-8')

    # 2. speech
    st.set('working', 'Listening (speech to text)')
    speech.transcribe(clips, work, profile['whisper_prompt'], st)

    # 3. proofread cues + edit list (Claude)
    st.set('working', 'Writing the subtitles')
    ans = llm.ask(work, 'cues', C.build_prompt(clips, work, profile))
    landscape = sum(c['width'] >= c['height'] for c in clips) * 2 >= len(clips)
    src = R.inter_size(1, 0) if landscape else R.inter_size(0, 1)

    # versions: the main video, plus a Short (<= 3:00) when the main video isn't one itself
    main = C.timeline(ans, clips)
    versions = [('video', main, not landscape and main[2] <= SHORT_MAX)]
    settings = json.loads((lesson / 'settings.json').read_text(encoding='utf-8')) if (lesson / 'settings.json').exists() else {}
    if settings.get('short', profile.get('short', True)) and (landscape or main[2] > SHORT_MAX):
        short = main if main[2] <= SHORT_MAX else C.timeline(ans, clips, short=True)
        if short[2] <= SHORT_MAX:
            versions.append(('short', short, True))
        else:
            ans.setdefault('notes', []).append(f'No Short: even without side remarks the video is {ts(short[2])} (limit 3:00)')
    music = sorted(p for p in assets['music'].glob('*') if p.suffix.lower() in ('.mp3', '.m4a', '.wav', '.ogg'))         if profile.get('music', {}).get('enabled') and assets['music'].exists() else []
    track = str(music[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(music)]) if music else None
    assets['banner'] = R.banner_png(assets['subscribe'], work)
    files, report = {}, {'placement': {}, 'qa': {}, 'notes': ans.get('notes', [])}
    src_logo = src_banner = None
    for vname, (segs, cues, total, cta, cards), is_short in versions:
        tag = 'main' if (segs, cues) == (main[0], main[1]) else vname
        (work / f'timeline-{vname}.json').write_text(json.dumps({'segs': segs, 'cues': cues, 'total': total, 'cta': cta,
                                                                 'cards': cards}, ensure_ascii=False, indent=1), encoding='utf-8')
        st.log(f'{vname}: {len(segs)} segments, {len(cues)} lines, {total:.1f} s')
        st.set('working', f'Cutting the {"Short" if vname == "short" else "video"}')
        lst, inter = R.make_inter(segs, clips, work, src, st, tag)
        st.set('working', 'Finding the best place for the subtitles')
        faces = L.track_faces(segs, inter, cues, FACE_MODEL)
        if src_logo is None:
            src_logo = R.find_logo(inter, len(segs), assets['logo']) or False
            src_banner = R.find_banner(inter, segs, assets['banner'])
            st.log(f'already in the recording: logo {src_logo}, subscribe banner {src_banner}')
        if is_short and landscape:                                   # square crop around her, text below
            fx = statistics.median((f[0] + f[2]) / 2 for f in faces if f)
            side = src[1]
            cx = min(max(0, fx - side / 2), src[0] - side)
            spec = L.Spec(vname, 1080, 1920, short=True, src_w=src[0], src_h=src[1], crop=(cx, 0, side, side), pic=(0, 200, 1080, 1080))
        else:
            spec = L.Spec(vname, *src, short=is_short, src_w=src[0], src_h=src[1])
        visible = bool(src_logo) and spec.crop[0] <= src_logo[0] and src_logo[2] <= spec.crop[0] + spec.crop[2]
        spec.logo = spec.to_canvas(src_logo) if visible else R.logo_box(spec)
        blks = L.blocks(cues, spec, assets['fonts'])
        prefer = profile['subtitles']['short' if spec.short else 'long']
        sides, rep = L.place(spec, faces, blks, prefer)
        if sides is None:
            sides = L.make_band(spec, blks, prefer)
            blks = L.blocks(cues, spec, assets['fonts'])
        report['placement'][vname] = rep
        L.save_plan(work / f'plan-{vname}.json', spec, sides, rep)
        ass = work / f'subs-{vname}.ass'
        L.write_ass(ass, spec, cues, blks, sides, profile['colors'])
        b0 = max(0.0, min(cta if cta is not None else total - 10, total - 6))
        by = None if src_banner else choose_banner_y(spec, cues, blks, sides, faces, b0)
        banner = None if by is None else ((spec.W - R.banner_width(spec)) / 2, by, (spec.W + R.banner_width(spec)) / 2,
                                          by + R.banner_height(spec))
        placed = []
        for k, card in enumerate(cards):
            png = work / f'card-{vname}-{k}.png'
            size = chips.draw(card, profile['palette'], spec.H, png, assets['fonts'])
            xy = chips.place(spec, size, card, cues, blks, sides, faces, banner and (banner, b0))
            if xy:
                placed.append((png, *xy, card['s'], card['e']))
        rep['cards'] = f'{len(placed)}/{len(cards)}'
        dst = out / f'{name}-{vname}.mp4'
        st.set('working', f'Rendering the {"Short" if vname == "short" else "video"}')
        R.render(spec, lst, ass, profile, assets, total, cta, not visible, by, work, dst, music=track, status=st, cards=placed)
        qa, sheet = qa_check(dst, spec, cues, blks, sides, work, vname)
        report['qa'][vname] = qa
        files[dst.name] = f'{"Short" if spec.short else "video"} {spec.W}x{spec.H}, {ts(total)}'
        (out / f'check-frames-{vname}.jpg').write_bytes(sheet.read_bytes())
        st.log(f'{vname}: placement {rep}, covered {len(qa["face_covered"])}, {qa["loudness_lufs"]} LUFS')
        for lang in ('tr', 'de'):
            srt = f'{name}-{vname}-subtitles-{lang}.srt'
            L.write_srt(out / srt, cues, lang)
            files[srt] = f'caption track for the {vname} ({"Turkish" if lang == "tr" else "German"})'
        if vname == 'video':
            segs_main, cues_main, total_main, inter_main = segs, cues, total, inter
    segs, cues, total, inter = segs_main, cues_main, total_main, inter_main
    specs_info = versions

    # 6. YouTube texts + thumbnails (Claude looks at candidate frames)
    st.set('working', 'Writing titles and descriptions')
    cand, sheet = T.candidates(segs, inter, FACE_MODEL, work)
    vinfo = ', '.join(f'{v} ({"Short" if sh else "normal video"}, {ts(tl[2])})' for v, tl, sh in specs_info)
    data = llm.ask(work, 'texts', T.build_prompt(profile, lesson.name, ans.get('title_hint', lesson.name), cues, vinfo, len(cand)),
                   images=[sheet])
    data['chapters'] = T.check_chapters(data.get('chapters', []), total) if not specs_info[0][2] else []
    st.set('working', 'Making thumbnails')
    cuts = {}
    for i, th in enumerate(data['thumbnails'][:3], 1):
        frame = next((c for c in cand if c['n'] == th['frame']), cand[0])['file']
        cuts.setdefault(frame, thumbnail.cut_out(frame))
        badge = f'{data.get("level", "")} {profile.get("badge_suffix", "ALMANCA")}'.strip()
        thumbnail.make(frame, th['words'], th['question'], badge, assets['logo'], out / f'thumbnail-{i}.jpg', cut=cuts[frame],
                       palette=profile['palette'])
        files[f'thumbnail-{i}.jpg'] = f'16:9 thumbnail, option {i}'
        if i == 1 and any(v[2] for v in specs_info):
            thumbnail.make(frame, th['words'], th['question'], badge, assets['logo'], out / 'thumbnail-short.jpg',
                           vertical=True, cut=cuts[frame], palette=profile['palette'])
            files['thumbnail-short.jpg'] = 'vertical cover for the Short'
    for p in out.glob('*_small_preview.jpg'):
        p.unlink()
    terms = sorted(set(ans.get('terms', [])))
    (out / 'terms.txt').write_text('\n'.join(terms) + '\n', encoding='utf-8')
    data['notes'] = report['notes'] + data.get('notes', [])
    T.write(out, name, data, profile, files)
    (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    covered = sum(len(q['face_covered']) for q in report['qa'].values())
    st.set('done', f'{", ".join(f for f in files if f.endswith(".mp4"))}' +
           (f' (CHECK: text touches her face in {covered} line(s))' if covered else ''), report=report)


def changed(lesson):
    """New/changed lesson that is not being uploaded right now (no file changed in the last 10 minutes)."""
    videos = lesson_videos(lesson)
    if not videos:
        return False
    if time.time() - max(v.stat().st_mtime for v in videos) < 600:
        return False
    out = lesson / 'Output'
    if out.exists() and not (out / OURS).exists() and not (lesson / 'Output (Easy)').exists():
        return False                          # made elsewhere (by hand): leave it alone
    st = output_dir(lesson) / OURS
    if not st.exists():
        return True
    s = json.loads(st.read_text(encoding='utf-8'))
    if s.get('state') == 'failed' and s.get('inputs') == fingerprint(videos):
        return False                          # don't retry the same failure every 10 minutes
    return s.get('inputs') != fingerprint(videos)


def watch(root, once):
    lock = WORK_ROOT / 'watch.lock'
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime < 6 * 3600:
        return
    try:
        lock.write_text(str(os.getpid()))
        while True:
            for lesson in sorted(Path(root).iterdir()):
                if lesson.is_dir() and not lesson.name.startswith(('_', '.')) and changed(lesson):
                    safe_process(lesson, root)
            if once:
                break
            time.sleep(600)
    finally:
        lock.unlink(missing_ok=True)


def safe_process(lesson, root):
    try:
        process(lesson, root)
    except llm.Pending as p:
        Status(output_dir(lesson), WORK_ROOT / slug(lesson.name)).set('waiting', f'Needs a Claude answer: {p}')
    except Exception as e:
        work = WORK_ROOT / slug(lesson.name)
        work.mkdir(parents=True, exist_ok=True)
        (work / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        Status(output_dir(lesson), work).set('failed', 'Stopped', error=f'{type(e).__name__}: {e}'[:400])
        raise


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['run', 'watch'])
    ap.add_argument('path')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--root', help='channel root (default: parent of the lesson)')
    a = ap.parse_args()
    if a.cmd == 'run':
        safe_process(Path(a.path), a.root or Path(a.path).parent)
    else:
        watch(a.path, a.once)
