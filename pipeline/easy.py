"""Easy mode on the home PC: a lesson folder in Drive in, a finished video + everything for YouTube out.

  python easy.py run  "J:\\My Drive\\SINEM-YouTube\\<lesson>"      process one lesson now
  python easy.py watch "J:\\My Drive\\SINEM-YouTube" [--once]      look for new lessons (Task Scheduler: --once every 10 min)
                                                                   and fill in YouTube details of uploaded ones (youtube.py),
                                                                   and run the automatic posting step (publish.py: Reels
                                                                   when automatic posting is on in the dashboard)

Drive layout:  SINEM-YouTube/_channel/channel.json (+ logo, banner, fonts, music/)  ·  SINEM-YouTube/<lesson>/*.mp4|mov
Results go to <lesson>/Output/ (or "Output (Easy)" when an Output folder made elsewhere already exists): the finished
videos there, everything else (thumbnails, captions, texts, covers, records, status) in Output/extras/.
Optional <lesson>/settings.json, e.g. {"short": false} = no Short version for this lesson, {"studio": true} = the
long video in the studio layout (picture card + example-sentence board), also from a horizontal recording.
Work files stay on the PC in D:\\YT\\easy-work\\<lesson>.
"""
import argparse, hashlib, json, os, re, shutil, statistics, sys, threading, time, traceback
from pathlib import Path
import cv2
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import chips
import cues as C
import layout as L
import light
import llm
import render as R
import speech
import paths
import texts as T
from lessons import OURS, file_stem, lesson_videos, load_profile, local_copy, output_dir, retry_marker   # noqa: F401 (also used as easy.*)
import thumbnail
from tools import HERE, Status, VIDEO_EXT, extras, ffmpeg, probe, run, FFMPEG, slug, ts

WORK_ROOT = paths.WORK
FACE_MODEL = HERE / 'models' / 'face_detection_yunet_2023mar.onnx'
SHORT_MAX = 180.0


def fingerprint(videos):
    return [[v.name, v.stat().st_size] for v in videos]


ZOOM = 1.08


def plan_zoom(segs, frame, size, logo, banner):
    """Every second segment is a slight punch-in, so jump cuts look intentional. Anchored at the corner of a logo that
    is burned into the recording (so it stays in the picture), otherwise centred on her face. Not for very short
    segments or the last one when a subscribe banner is burned into it. Sets g['zoom']; returns the zooms used."""
    W, H = size
    cw, ch = W / ZOOM, H / ZOOM
    for k, g in enumerate(segs):
        g.pop('zoom', None)
        if k % 2 == 0 or g['b'] - g['a'] < 2.0 or (banner and k == len(segs) - 1):
            continue
        if logo:
            x0, y0 = (W - cw if (logo[0] + logo[2]) / 2 > W / 2 else 0), 0
        else:
            fr = frame(g, (g['b'] - g['a']) / 2)
            _, f = L.detector(FACE_MODEL, W, H).detect(fr) if fr is not None else (None, None)
            fx, fy = ((f[0][0] + f[0][2] / 2), (f[0][1] + f[0][3] / 2)) if f is not None else (W / 2, H / 2)
            x0, y0 = min(max(0, fx - cw / 2), W - cw), min(max(0, fy - ch * 0.45), H - ch)
        g['zoom'] = [ZOOM, round(x0), round(y0)]
    return [g['zoom'] for g in segs if g.get('zoom')]


def use_studio(profile, settings, landscape):
    """The long video in the studio layout? The lesson's settings.json {"studio": true/false} decides; else
    channel.json: "studio" for vertical recordings (on unless set false), "studio_landscape" for horizontal ones
    (on unless set false: the default for every long video since 2026-11-05, the owner's choice)."""
    if 'studio' in settings:
        return bool(settings['studio'])
    return bool(profile.get('studio_landscape', True)) if landscape else bool(profile.get('studio', True))


def studio_size(profile):
    """(crop, height) for layout.studio from channel.json "studio_size": {"crop": share of the recording's height
    shown, "height": the card's share of the canvas height}; None = layout's default. Out-of-range values are ignored."""
    size = profile.get('studio_size') if isinstance(profile.get('studio_size'), dict) else {}
    ok = lambda v, lo, hi: float(v) if isinstance(v, (int, float)) and lo <= v <= hi else None
    return ok(size.get('crop'), 0.5, 1.0), ok(size.get('height'), 0.5, 0.95)


def cover_logo(logo, zooms):
    """Square where our own (opaque) logo hides the burned-in one at its normal and its punched-in positions."""
    boxes = [logo] + [((logo[0] - x0) * z, (logo[1] - y0) * z, (logo[2] - x0) * z, (logo[3] - y0) * z) for z, x0, y0 in zooms]
    cx = sum((b[0] + b[2]) / 2 for b in boxes) / len(boxes)
    cy = sum((b[1] + b[3]) / 2 for b in boxes) / len(boxes)
    side = max(2 * ((((b[0] + b[2]) / 2 - cx) ** 2 + ((b[1] + b[3]) / 2 - cy) ** 2) ** 0.5) + (b[2] - b[0]) for b in boxes) + 8
    return (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)


def choose_banner_y(spec, cues, blks, sides, faces, b0):
    """Top edge for the subscribe banner during [b0, end]: away from her face and the subtitles on screen."""
    bh = R.banner_height(spec)
    bw = R.banner_width(spec)
    x0 = R.banner_x(spec)
    if spec.studio:     # top of the panel: the section header makes room for it (the board and subtitles stay)
        return int(0.03 * spec.H)
    late = [i for i, c in enumerate(cues) if c['e'] > b0]
    if spec.short:      # above the subtitles (the bottom is YouTube's title area); nowhere free → no banner
        above = min((L.text_box(spec, blks[i], sides[i])[1] for i in late), default=spec.bottom) - 16 - bh
        cands = [int(above), int(spec.bottom - bh)]
    else:
        cands = [int(spec.H * 0.95 - bh), int(0.6 * spec.H), int(spec.top + 0.12 * spec.H), int(0.5 * spec.H - bh / 2)]
    for y in cands:
        box = (x0, y, x0 + bw, y + bh)
        if spec.logo and L.overlaps(box, spec.logo):
            continue
        if all(not L.overlaps(box, L.no_go(spec.to_canvas(faces[i]))) for i in late if faces[i]) and \
                all(not L.overlaps(box, L.text_box(spec, blks[i], sides[i])) for i in late):
            return y
    return None if spec.short else cands[0]


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
    x = extras(out)                                   # everything but the finished videos
    work = WORK_ROOT / slug(lesson.name)
    videos = lesson_videos(lesson)
    # The cache is keyed by clip number (c1, c2...): when the clips change (a clip replaced, added or removed),
    # move the old work folder aside so a new clip never gets the old one's transcript or subtitles.
    if (work / 'clips.json').exists():
        old = json.loads((work / 'clips.json').read_text(encoding='utf-8'))
        now = {v.name: v.stat().st_size for v in videos}
        if sorted(c['name'] for c in old) != sorted(now) or any(c.get('size', now[c['name']]) != now[c['name']] for c in old):
            work.rename(work.with_name(f'{work.name}.old-{time.strftime("%Y%m%d-%H%M%S")}'))
    work.mkdir(parents=True, exist_ok=True)
    st = Status(out, work)
    st.set('working', f'Starting: {len(videos)} video(s)', inputs=fingerprint(videos), error='', youtube='', youtube_complete=False)
    name = file_stem(lesson)                          # a renamed folder keeps its files' names
    # the same recording in two lesson folders (copied by mistake): stop instead of making the video twice
    for other in Path(root).iterdir():
        if other.is_dir() and other.resolve() != lesson.resolve() and not other.name.startswith(('_', '.')):
            same = {(v.name, v.stat().st_size) for v in lesson_videos(other)} & {(v.name, v.stat().st_size) for v in videos}
            if same:
                raise RuntimeError(f'{", ".join(n for n, _ in same)} is also in the lesson "{other.name}". '
                                   'Put this lesson\'s own recording here (the PC starts again when the clips change).')

    # 1. clips
    clips = []
    for i, v in enumerate(sorted(videos, key=lambda v: (probe(v)['created'] or '~', v.name)), 1):
        clips.append({'id': f'c{i}', 'path': str(v), 'name': v.name, 'size': v.stat().st_size, **probe(v)})
    (work / 'clips.json').write_text(json.dumps(clips, indent=1), encoding='utf-8')

    # 2. speech
    st.set('working', 'Listening (speech to text)')
    speech.transcribe(clips, work, profile['whisper_prompt'], st)

    # 3. proofread cues + edit list (Claude)
    st.set('working', 'Writing the subtitles')
    ans = llm.ask(work, 'cues', C.build_prompt(clips, work, profile))
    landscape = sum(c['width'] >= c['height'] for c in clips) * 2 >= len(clips)
    src = R.inter_size(1, 0) if landscape else R.inter_size(0, 1)
    if profile.get('light', 'auto') == 'auto':                       # lighting measured on her face, per clip
        st.set('working', 'Checking the lighting')
        light.for_clips(clips, src, FACE_MODEL, lambda c, t: R.source_frame(c, t, src), work, st.log)

    # versions: the main video, plus Shorts (<= 3:00) when the main video isn't one itself: one per self-contained part
    # (ans["shorts"]), or else one condensed Short of the whole lesson
    main = C.timeline(ans, clips)
    versions = [('video', main, not landscape and main[2] <= SHORT_MAX)]
    settings = json.loads((lesson / 'settings.json').read_text(encoding='utf-8')) if (lesson / 'settings.json').exists() else {}
    ideas, kinds = {}, {}
    if settings.get('short', profile.get('short', True)) and (landscape or main[2] > SHORT_MAX):
        cap = int(settings.get('shorts_max', profile.get('shorts_max', 3)))
        if len(ans.get('shorts', [])) > cap:        # few good Shorts, published days apart, don't flood subscribers
            st.log(f"WARNING: {len(ans['shorts'])} Shorts proposed, keeping the first {cap} (shorts_max)")
        parts = [(f'short-{i}', C.part(ans, sh), sh.get('idea', '')) for i, sh in enumerate(ans.get('shorts', [])[:cap], 1)]
        kinds = {f'short-{i}': sh.get('kind', '') for i, sh in enumerate(ans.get('shorts', [])[:cap], 1)}
        for i, sh in enumerate(ans.get('shorts', [])[:cap], 1):
            n = C.timeline(C.part(ans, sh), clips, short=True)[2]
            if n > 60:
                st.log(f'WARNING: short-{i} is {n:.0f} s; lesson Shorts work best under 60 s (viewers swipe at ~30 s)')
        if not parts:
            parts = [('short', ans if main[2] > SHORT_MAX else None, '')]
        for vname, a, idea in parts:
            short = C.timeline(a, clips, short=True) if a else main
            if short[2] <= SHORT_MAX:
                versions.append((vname, short, True))
                ideas[vname] = idea
            else:
                ans.setdefault('notes', []).append(f'No {vname}: {ts(short[2])} is over the 3:00 limit')
    music = sorted(p for p in assets['music'].glob('*') if p.suffix.lower() in ('.mp3', '.m4a', '.wav', '.ogg'))         if profile.get('music', {}).get('enabled') and assets['music'].exists() else []
    track = str(music[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(music)]) if music else None
    assets['banner'] = R.banner_png(assets['subscribe'], work)
    files, report = {}, {'placement': {}, 'qa': {}, 'notes': ans.get('notes', [])}
    src_logo = src_banner = None
    for vname, (segs, cues, total, cta, cards, sections), is_short in versions:
        tag = 'main' if (segs, cues) == (main[0], main[1]) else vname
        (work / f'timeline-{vname}.json').write_text(json.dumps({'segs': segs, 'cues': cues, 'total': total, 'cta': cta,
                                                                 'cards': cards}, ensure_ascii=False, indent=1), encoding='utf-8')
        st.log(f'{vname}: {len(segs)} segments, {len(cues)} lines, {total:.1f} s')
        by_id = {c['id']: c for c in clips}
        frame = lambda g, t, zoom=None: R.source_frame(by_id[g['clip']], g['a'] + t, src, zoom)
        if src_logo is None:                                         # burned into the recording already?
            src_logo = R.find_logo([frame(g, 0.5) for g in (segs[0], segs[len(segs) // 2], segs[-1])], assets['logo']) or False
            src_banner = R.find_banner(frame(segs[-1], max(0, segs[-1]['b'] - segs[-1]['a'] - 3)), assets['banner'])
            st.log(f'already in the recording: logo {src_logo}, subscribe banner {src_banner}')
        zoomed = plan_zoom(segs, frame, src, src_logo, src_banner) if profile.get('zoom_cuts', True) and not is_short else []
        st.set('working', f'Cutting the {vname.replace("short", "Short") if vname.startswith("short") else "video"}')
        lst, inter = R.make_inter(segs, clips, work, src, st, tag)
        st.set('working', 'Finding the best place for the subtitles')
        faces = L.track_faces(segs, inter, cues, FACE_MODEL)
        if is_short and landscape:                                   # square crop around her, text below
            fx = statistics.median((f[0] + f[2]) / 2 for f in faces if f)
            side = src[1]
            cx = min(max(0, fx - side / 2), src[0] - side)
            spec = L.Spec(vname, 1080, 1920, short=True, src_w=src[0], src_h=src[1], crop=(cx, 0, side, side), pic=(0, 200, 1080, 1080))
        elif not is_short and use_studio(profile, settings, landscape):           # long video
            spec = L.studio(vname, *src, faces, *studio_size(profile))
        else:
            spec = L.Spec(vname, *src, short=is_short, src_w=src[0], src_h=src[1])
        visible = bool(src_logo) and spec.crop[0] <= src_logo[0] and src_logo[2] <= spec.crop[0] + spec.crop[2]
        spec.logo = spec.to_canvas(src_logo) if visible else R.logo_box(spec)
        if visible and zoomed:
            # punch-ins move/enlarge the burned-in logo: our own logo covers both positions, so it never jumps
            spec.logo = cover_logo(src_logo, zoomed)
            spec.logo_opaque = True
            visible = False
        blks = L.blocks(cues, spec, assets['fonts'])
        prefer = 'bottom' if spec.studio else profile['subtitles']['short' if spec.short else 'long']
        sides, rep = L.place(spec, faces, blks, prefer)
        hook_row = spec.logo[3] + 14 if spec.short else 0            # Shorts: logo + hook title share the top row
        if sides is None and spec.short and prefer == 'bottom' and L.lift(spec, faces, blks, hook_row):
            sides, rep = L.place(spec, faces, blks, prefer)
            rep['lifted'] = int(spec.lifted)
        if sides is None:
            sides = L.make_band(spec, blks, prefer)
            blks = L.blocks(cues, spec, assets['fonts'])
        report['placement'][vname] = rep
        L.save_plan(work / f'plan-{vname}.json', spec, sides, rep)
        ass = work / f'subs-{vname}.ass'
        L.write_ass(ass, spec, cues, blks, sides, profile['colors'],
                    L.board(spec, cues, sections, total, profile['colors'], assets['fonts']) if spec.studio else ())
        b0 = max(0.0, min(cta if cta is not None else total - 10, total - 6))
        by = None if src_banner else choose_banner_y(spec, cues, blks, sides, faces, b0)
        banner = None if by is None else (R.banner_x(spec), by, R.banner_x(spec) + R.banner_width(spec), by + R.banner_height(spec))
        placed, taken = [], []
        items = [('label', k, x) for k, x in enumerate(sections)] + [('card', k, x) for k, x in enumerate(cards)]
        for kind, k, item in items:                                  # section labels first, then word cards
            png = work / f'{kind}-{vname}-{k}.png'
            draw = chips.draw_label if kind == 'label' else chips.draw
            if spec.short and kind == 'label' and ideas.get(vname):  # Shorts: the hook stays on top, next to the logo
                lx = int(spec.logo[2]) + 20
                size = chips.draw_label(item, profile['palette'], spec.H, png, assets['fonts'], maxw=spec.W - lx - 30)
                item = {**item, 'e': total - 0.2}
                xy = (lx, int((spec.logo[1] + spec.logo[3] - size[1]) / 2))
            elif spec.studio and kind == 'label':                     # studio: the part's title stays on as a header
                size = draw(item, profile['palette'], spec.H, png, assets['fonts'])
                nxt = [x['s'] for x in sections if x['s'] > item['s']]
                item = {**item, 'e': min((nxt[0] if nxt else total) - 0.2, b0 if banner else total)}
                xy = (spec.panel[0], int(0.055 * spec.H)) if item['e'] - item['s'] > 1 else None
            else:
                fit = {'maxw': int(spec.panel[0] - spec.pic[0] - 30)} if spec.studio and kind == 'card' else {}   # her column
                size = draw(item, profile['palette'], spec.H, png, assets['fonts'], **fit)
                xy = chips.place(spec, size, item, cues, blks, sides, faces, banner and (banner, b0), taken)
            if xy:
                placed.append((png, *xy, item['s'], item['e']))
                taken.append(((xy[0], xy[1], xy[0] + size[0], xy[1] + size[1]), item['s'], item['e']))
        rep['cards'] = f'{len(placed)}/{len(items)} (labels + word cards)'
        dst = out / f'{name}-{vname}.mp4'
        st.set('working', f'Rendering the {vname.replace("short", "Short") if vname.startswith("short") else "video"}')
        outro = None if spec.short or not profile.get('outro', True) else             R.outro_png(spec, profile['palette'], assets, ans.get('next', ''), work / f'outro-{vname}.png')
        R.render(spec, R.with_outro(lst, outro, src) if outro else lst, ass, profile, assets, total, cta, not visible, by, work,
                 dst, music=track, status=st, cards=placed, outro=bool(outro), outro_png=outro)
        qa, sheet = qa_check(dst, spec, cues, blks, sides, work, vname)
        report['qa'][vname] = qa
        files[dst.name] = f'{"Short" if spec.short else "video"} {spec.W}x{spec.H}, {ts(total + (10 if outro else 0))}' +             (' (last 10 s: end-screen card)' if outro else '')
        (x / f'check-frames-{vname}.jpg').write_bytes(sheet.read_bytes())
        st.log(f'{vname}: placement {rep}, covered {len(qa["face_covered"])}, {qa["loudness_lufs"]} LUFS')
        for lang in ('tr', 'de'):
            srt = f'{name}-{vname}-subtitles-{lang}.srt'
            L.write_srt(x / srt, cues, lang)
            files[srt] = f'caption track for the {vname} ({"Turkish" if lang == "tr" else "German"})'
        if vname == 'video':
            segs_main, cues_main, total_main, inter_main = segs, cues, total, inter
    segs, cues, total, inter = segs_main, cues_main, total_main, inter_main
    specs_info = versions

    # 6. YouTube texts + thumbnails (Claude looks at candidate frames)
    st.set('working', 'Writing titles and descriptions')
    cand, sheet = T.candidates(segs, inter, FACE_MODEL, work)
    vinfo = ', '.join(f'{v} ({"Short" if sh else "normal video"}, {ts(tl[2])}' + (f', idea: {ideas[v]}' if ideas.get(v) else '') +
                      (f', kind: {kinds[v]}' if kinds.get(v) else '') + ')' for v, tl, sh in specs_info)
    data = llm.ask(work, 'texts', T.build_prompt(profile, lesson.name, ans.get('title_hint', lesson.name), cues, vinfo, len(cand),
                                                 T.series_numbers(lesson.parent, profile, lesson)), images=[sheet])
    data['chapters'] = T.check_chapters(data.get('chapters', []), total) if not specs_info[0][2] else []
    if not data['chapters']:                         # a Short (or no valid chapters): no chapters block in the text
        data['descriptions'] = [T.drop_chapters(d) for d in data.get('descriptions', [])]
    for sh in data.get('shorts', []):
        sh['kind'] = kinds.get(sh.get('version'), '')                # for review.py (length rule) and the series
    st.set('working', 'Making thumbnails')
    cuts = {}
    for i, th in enumerate(data['thumbnails'][:3], 1):
        frame = next((c for c in cand if c['n'] == th['frame']), cand[0])['file']
        cuts.setdefault(frame, thumbnail.cut_out(frame))
        badge = f'{data.get("level", "")} {profile.get("badge_suffix", "ALMANCA")}'.strip()
        thumbnail.make(frame, th['words'], th['question'], badge, assets['logo'], x / f'thumbnail-{i}.jpg', cut=cuts[frame],
                       palette=profile['palette'])
        files[f'thumbnail-{i}.jpg'] = f'16:9 thumbnail, option {i}'
        if i == 1 and any(v[2] for v in specs_info):
            thumbnail.make(frame, th['words'], th['question'], badge, assets['logo'], x / 'thumbnail-short.jpg',
                           vertical=True, cut=cuts[frame], palette=profile['palette'])
            files['thumbnail-short.jpg'] = 'vertical cover for the Short'
    for p in x.glob('*_small_preview.jpg'):
        p.unlink()
    terms = sorted(set(ans.get('terms', [])))
    (x / 'terms.txt').write_text('\n'.join(terms) + '\n', encoding='utf-8')
    data['notes'] = report['notes'] + data.get('notes', [])
    T.write(x, name, data, profile, files)
    if any(v[2] for v in specs_info[1:]):                    # Instagram: a cover + caption per Short (posted by the watcher)
        import instagram
        instagram.make_covers(out, data, {c['n']: c['file'] for c in cand}, assets['logo'], profile['palette'], st.log)
        instagram.write_md(lesson, out, data)
    try:                                       # blog post + X/Threads texts (posts.py): a problem never stops the lesson
        import posts
        for n in posts.make(work, x, name, profile, T.cue_rows(cues)):
            st.log(f'posts: {n}')
    except Exception as e:
        st.log(f'posts: not made ({type(e).__name__}: {str(e)[:200]}); later: python -I posts.py "{lesson}"')
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
    if out.exists() and not (extras(out) / OURS).exists() and not (lesson / 'Output (Easy)').exists():
        return False                          # made elsewhere (by hand): leave it alone
    st = extras(output_dir(lesson)) / OURS
    if not st.exists():
        return True
    s = json.loads(st.read_text(encoding='utf-8'))
    if s.get('state') == 'waiting' and s.get('memory'):
        return True                           # waits for free memory: safe_process looks again every run
    if s.get('state') == 'failed' and s.get('inputs') == fingerprint(videos):
        if out_of_memory(s.get('error')) and not s.get('memory_gave_up'):
            return True                       # stopped by a full memory (before 2026-11-06 too): try again
        return retry_marker(lesson).exists()  # don't retry the same failure every 10 minutes (unless asked to)
    return s.get('inputs') != fingerprint(videos)


PUBLISH_EVERY = 600                                   # s: the publishing step while the lessons are made


def _publish_while(root, stop):
    """While the lesson loop runs: the heartbeat ('rendering') and the publishing step every PUBLISH_EVERY s. The
    scheduled task starts no second run while one is running (IgnoreNew), so a long render would otherwise hold a
    due Reel back (F11) and leave the heartbeat old (the dashboard's watcher_stale card)."""
    while not stop.wait(PUBLISH_EVERY):
        heartbeat('rendering')
        publish_step(root)


def watch(root, once):
    """One watcher run (or a run every 10 min without once): new lessons, YouTube details, the publishing step
    before and after them and every PUBLISH_EVERY s in between (from a side thread, so a Reel never waits for a
    long render), STATUS.txt and the heartbeat. A failing lesson is printed and the run goes on. When watch.lock
    is there and fresh (a run that is still going, or one that was killed), only the publishing step runs."""
    lock = WORK_ROOT / 'watch.lock'
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime < 6 * 3600:
        heartbeat('busy')
        if Path(root).is_dir():
            publish_step(root)
        return
    heartbeat('start')
    if not Path(root).is_dir():                       # Google Drive not mounted yet (e.g. right after waking up)
        print(f'{root} not available yet; next try in 10 minutes')
        return
    try:
        lock.write_text(str(os.getpid()))
        while True:
            publish_step(root)
            stop = threading.Event()
            side = threading.Thread(target=_publish_while, args=(root, stop), name='publish-while-rendering',
                                    daemon=True)
            side.start()
            try:
                for lesson in sorted(Path(root).iterdir()):
                    if not lesson.is_dir() or lesson.name.startswith(('_', '.')):
                        continue
                    for step in (lambda: changed(lesson) and safe_process(lesson, root), lambda: youtube_step(lesson),
                                 lambda: name_step(lesson)):
                        try:
                            step()
                        except Exception as e:                 # one lesson's problem never stops the others
                            print(f'lesson {lesson.name}: {type(e).__name__}: {e}')
            finally:
                stop.set()
                side.join()                                    # never two publishing steps at once
            publish_step(root)
            import overview                                    # (imports easy: not at the top)
            overview.write(root, idle=True)                    # SINEM-YouTube/STATUS.txt for the phone
            heartbeat('done')
            if once:
                break
            time.sleep(600)
            heartbeat('start')
    finally:
        lock.unlink(missing_ok=True)


def publish_step(root):
    """Automatic posting for every set-up platform (publish.auto_step). Its lines reach the watcher log through
    publish.log_line, which prints each one (and adds it to WORK_ROOT/<platform>.log). Never raises. Replaces
    instagram_step."""
    try:
        import publish
        publish.auto_step(Path(root))
    except Exception as e:                                     # auto_step never raises: this is an import error
        print(f'publish step: {type(e).__name__}: {e}')


def heartbeat(phase):
    """Write WORK_ROOT/watcher.json {'at': ISO UTC, 'phase': 'start' | 'rendering' | 'done' | 'busy', 'pid'} atomically
    (temp file + os.replace), so the dashboard can tell the watcher is alive. Never raises."""
    path = WORK_ROOT / 'watcher.json'
    tmp = path.with_name(f'{path.name}.tmp-{os.getpid()}')
    try:
        tmp.write_text(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'phase': phase,
                                   'pid': os.getpid()}), encoding='utf-8')
        os.replace(tmp, path)
    except Exception as e:
        print(f'heartbeat not written: {type(e).__name__}: {e}')
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _uploading():
    """True while the PC is uploading a video (publish.py's YouTube job): the details step waits for the next run,
    so the two never look for or write the same video at once."""
    try:
        import publish
        j = publish.current_job('youtube')
        return bool(j and j.get('state') in ('queued', 'running'))
    except Exception:
        return False


def youtube_step(lesson):
    """After Sinem uploads the finished video/Shorts in the YouTube app: fill in their details (youtube.py).
    Runs for finished lessons until everything is set (the comment waits until the video is public)."""
    out = output_dir(lesson)
    p = extras(out) / OURS
    if not p.exists():
        return
    s = json.loads(p.read_text(encoding='utf-8'))
    if s.get('state') != 'done' or s.get('youtube_complete') or not (extras(out) / 'youtube.json').exists():
        return
    import youtube as Y
    if not Y.TOKEN.exists() or _uploading() or Y.quota_until():     # quota: no request until it is back
        return
    st = Status(out, WORK_ROOT / slug(lesson.name))
    try:
        r = Y.apply(lesson, log=st.log)
    except Exception as e:
        if 'quota' in str(e):
            r = (Y.quota_text(Y.quota_used_up()) + ' The watcher tries again then.', False)
        else:
            r = (f'YouTube step failed: {type(e).__name__}: {e}'[:300], False)
    if r and (r[0] != s.get('youtube') or r[1]):
        st.set('done', s.get('step', ''), youtube=r[0], youtube_complete=r[1])
    try:                                       # the lesson posts get the video link once it is on YouTube
        import posts
        if posts.relink(WORK_ROOT / slug(lesson.name), extras(out), file_stem(lesson)):
            st.log('posts: video link filled in')
    except Exception as e:
        st.log(f'posts: link not filled in ({type(e).__name__}: {str(e)[:200]})')


def name_step(lesson):
    """A finished lesson in a folder named '4' or 'new' gets a meaningful name (lessons.rename). Tried again on the
    next run when a post is going out or a file is in use."""
    import lessons
    if not lessons.placeholder(lesson.name) or not lessons.finished(lesson) or retry_marker(lesson).exists():
        return
    new = lessons.suggested_name(lesson)
    if not new or new == lesson.name or lesson.with_name(new).exists():
        return
    try:
        target = lessons.rename(lesson, new, owner='watcher', log=lambda line: None)
    except Exception as e:
        print(f'lesson {lesson.name}: not renamed yet ({type(e).__name__}: {e})')
        return
    (WORK_ROOT / slug(target.name)).mkdir(parents=True, exist_ok=True)
    Status(output_dir(target), WORK_ROOT / slug(target.name)).log(f'folder renamed: "{lesson.name}" -> "{target.name}"')


# A full memory is the PC's state, not the lesson's fault (2026-11-06: P1 and P2 stopped with 'CUDA ... out of
# memory' and 'mkl_malloc: failed to allocate memory' while Chrome held 17 GB). So the watcher waits for free
# memory instead of stopping: a lesson starts only with MEMORY_NEED_GB free, and one that runs out anyway waits
# and tries again next run, MEMORY_TRIES times before it stops for good (Retry starts the count again).
MEMORY_NEED_GB = 6.0
MEMORY_TRIES = 6
_OOM = re.compile(r'out of memory|mkl_malloc|failed to allocate|unable to allocate|not enough memory|\bMemoryError\b',
                  re.I)


def out_of_memory(text) -> bool:
    """True for an error text that says the memory (RAM or the GPU's) was full."""
    return bool(_OOM.search(str(text or '')))


def free_memory_gb() -> float | None:
    """Windows: how much memory the PC can still hand out (its commit limit, RAM + page file, minus what is
    committed), in GB. None elsewhere or when Windows does not say."""
    if os.name != 'nt':
        return None
    try:
        import ctypes

        class Status_(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong)] + [
                (n, ctypes.c_ulonglong) for n in ('ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile',
                                                  'ullAvailPageFile', 'ullTotalVirtual', 'ullAvailVirtual',
                                                  'ullAvailExtendedVirtual')]
        m = Status_()
        m.dwLength = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return m.ullAvailPageFile / 2 ** 30
    except Exception:
        return None


def _memory_tries(work, add=0) -> int:
    """The lesson's out-of-memory count (work/memory-tries.txt), raised by add and written back."""
    f = work / 'memory-tries.txt'
    try:
        n = int(f.read_text(encoding='utf-8').strip() or 0)
    except (OSError, ValueError):
        n = 0
    if add:
        n += add
        work.mkdir(parents=True, exist_ok=True)
        f.write_text(f'{n}\n', encoding='utf-8')
    return n


HINT = 'Closing some programs on the PC (Chrome tabs) helps.'


def safe_process(lesson, root):
    retry_marker(lesson).unlink(missing_ok=True)
    work = WORK_ROOT / slug(lesson.name)
    free = free_memory_gb()
    if free is not None and free < MEMORY_NEED_GB:
        work.mkdir(parents=True, exist_ok=True)
        Status(output_dir(lesson), work).set(
            'waiting', f'Waiting for free memory on the PC: {free:.1f} GB free, {MEMORY_NEED_GB:g} GB needed. It starts '
            f'by itself; {HINT[0].lower()}{HINT[1:]}', memory=True, memory_gave_up=False)
        print(f'lesson {lesson.name}: waits for memory ({free:.1f} GB free)')
        return
    try:
        process(lesson, root)
    except llm.Pending as p:
        Status(output_dir(lesson), work).set('waiting', f'Needs a Claude answer: {p}', memory=False)
    except Exception as e:
        work.mkdir(parents=True, exist_ok=True)
        (work / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        text = f'{type(e).__name__}: {e}'[:400]
        st = Status(output_dir(lesson), work)
        if isinstance(e, MemoryError) or out_of_memory(text):
            n = _memory_tries(work, 1)
            if n < MEMORY_TRIES:
                st.set('waiting', f'The PC ran out of memory (try {n} of {MEMORY_TRIES}); it tries again in 10 minutes. '
                       f'{HINT}', memory=True, memory_gave_up=False, error=text)
                print(f'lesson {lesson.name}: out of memory, try {n} of {MEMORY_TRIES}')
                return
            (work / 'memory-tries.txt').unlink(missing_ok=True)
            st.set('failed', 'Stopped', memory=False, memory_gave_up=True,
                   error=f'The PC ran out of memory {MEMORY_TRIES} times. Close some programs on the PC, then press '
                         f'Retry. ({text})'[:400])
            raise
        st.set('failed', 'Stopped', error=text, memory=False)
        raise
    else:
        (work / 'memory-tries.txt').unlink(missing_ok=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['run', 'watch'])
    ap.add_argument('path')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--root', help='channel root (default: parent of the lesson)')
    a = ap.parse_args()
    import overview
    Status.after_set = overview.hook                   # every status change also refreshes SINEM-YouTube/STATUS.txt
    if a.cmd == 'run':
        safe_process(Path(a.path), a.root or Path(a.path).parent)
    else:
        watch(a.path, a.once)
