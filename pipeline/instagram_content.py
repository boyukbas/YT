"""Instagram's lesson side: which finished files are Reels (reels), their captions made from the YouTube texts
(caption), their covers (make_covers, prepare), the lesson's instagram.md, and the lesson's posting record
(<lesson>/Output/extras/instagram-posted.json: load; written by platform_instagram.py under the posting lock).
Nothing here talks to Meta (instagram.py does); instagram.py re-exports these names."""
import json, os, re
from datetime import datetime, timezone
from pathlib import Path

import paths
from tools import extras, probe, records

POSTED = 'instagram-posted.json'
MAX_TAGS = 5                                             # Instagram shows few hashtags; more looks like spam
SAVE_LINE = '💾 Kaydet, sonra tekrar et!'
YOUTUBE_LINE = "▶️ Dersin tamamı YouTube'da: Almanca Dil Atölyesi (link profilde)"
CHANNEL = 'https://www.youtube.com/@sinembb'


def lesson_files(lesson):
    lesson = Path(lesson)
    out = next(o for o in (lesson / 'Output (Easy)', lesson / 'Output') if (extras(o) / 'youtube.json').exists())
    return lesson, out, json.loads((extras(out) / 'youtube.json').read_text(encoding='utf-8'))


def reels(out, data, probe=None):
    """[(mp4, short entry)] in Short order. A one-clip Short lesson (no -short-N files; the main video is vertical and
    at most 3 min) is one Reel itself, with the main video's first title/description (decided 2026-11-04).
    probe: a callable like tools.probe; None means this module's probe, looked up now (tests patch it)."""
    probe = probe or globals()['probe']
    num = lambda f: int(re.search(r'-short-(\d+)$', f.stem).group(1))
    by_version = {s['version']: s for s in data.get('shorts', [])}
    found = [(f, by_version.get(f'short-{num(f)}', {})) for f in sorted(out.glob('*-short-*.mp4'), key=num)]
    if found:
        return found
    for f in out.glob('*-video.mp4'):
        p = probe(f)
        if p['height'] > p['width'] and p['duration'] <= 180 and data.get('titles'):
            import texts
            return [(f, {'version': 'video', 'whole': True, 'title': data['titles'][0],
                         'description': texts.drop_chapters(data.get('descriptions', [''])[0])})]
    return []


def profile(root):
    ch = Path(root) / '_channel' / 'channel.json'
    return json.loads(ch.read_text(encoding='utf-8')) if ch.exists() else {}


def hashtags(root):
    p = profile(root)
    return p.get('instagram', {}).get('hashtags') or p.get('youtube', {}).get('hashtags') or ['#almanca']


def caption(sh, base_tags):
    """The YouTube Short's texts, made for Instagram: hook first (only ~1 line shows before "more"), the lesson in
    2-3 lines, a save prompt (saves count a lot on Instagram), the YouTube pointer (links in captions aren't
    clickable), at most MAX_TAGS hashtags."""
    title = re.sub(r'\s*#\w+', '', sh.get('title', '')).strip()
    body = []
    for line in sh.get('description', '').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or (sh.get('whole') and line.startswith('🔔')):   # 🔔 = YouTube subscribe
            continue
        body.append(YOUTUBE_LINE if line.startswith('Dersin tamamı') else line)
    if YOUTUBE_LINE not in body and not sh.get('whole'):     # a whole one-clip lesson: no "full lesson on YouTube"
        body.append(YOUTUBE_LINE)
    own = [t for t in re.findall(r'#\w+', sh.get('description', '')) if t.lower() != '#shorts']
    tags = list(dict.fromkeys(t.lower() for t in base_tags + own))[:MAX_TAGS]
    return '\n\n'.join([title, '\n'.join(body), SAVE_LINE, ' '.join(tags)])[:2200]


def youtube_link(out):
    """The full lesson on YouTube (youtube.py's record), else the channel."""
    p = extras(out) / 'youtube-applied.json'
    recs = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    vid = next((r.get('id') for name, r in recs.items() if name.endswith('-video.mp4') and r.get('id')), None)
    return f'https://youtu.be/{vid}' if vid else CHANNEL


def fb_caption(cap, out):
    """The Facebook text: same as Instagram, but links work there."""
    return cap.replace(YOUTUBE_LINE, f"▶️ Dersin tamamı YouTube'da: {youtube_link(out)}")


def cover_path(mp4):
    if mp4.stem.endswith('-video'):                        # a one-clip Short lesson: its vertical YouTube cover
        return extras(mp4.parent) / 'thumbnail-short.jpg'
    return extras(mp4.parent) / ('cover-' + re.search(r'short-\d+$', mp4.stem).group(0) + '.jpg')     # cover-short-N.jpg


def make_covers(out, data, frames, logo, palette, log=print):
    """cover-short-N.jpg per Short (1080x1920, words inside the middle 3:4 that the profile grid shows).
    frames: {candidate number: frame image}. Skips covers that already exist."""
    import thumbnail
    badge = f'{data.get("level", "")} ALMANCA'.strip()
    first = data.get('thumbnails', [{}])[0]
    cuts = {}
    for mp4, sh in reels(out, data):
        dst = cover_path(mp4)
        if dst.exists():
            continue
        c = sh.get('cover') or first
        frame = frames.get(c.get('frame')) or next(iter(frames.values()))
        if frame not in cuts:
            cuts[frame] = thumbnail.cut_out(frame)
        thumbnail.make(frame, c['words'], c['question'], badge, logo, dst, vertical=True, cut=cuts[frame], palette=palette)
        Path(str(dst).replace('.jpg', '_small_preview.jpg')).unlink(missing_ok=True)
        log(f'{dst.name}: {" / ".join(c["words"])} · {c["question"]}')


def write_md(lesson, out, data):
    posted = load(out)
    tags = hashtags(lesson.parent)
    L = ['# Instagram Reels', '',
         'Posted from the private dashboard (automatically when it is on: Publish → Settings). To post one by hand '
         'instead: Instagram → + → Reel → pick the video → Edit cover → Add from camera roll → the cover → paste the '
         'caption.', '']
    lv = posted.get('_facebook', {}).get('long')
    if lv:
        L += [f"Long video on the Facebook Page: posted {lv['posted']}: {lv['permalink']}", '']
    for mp4, sh in reels(out, data):
        p = posted.get(mp4.name, {})
        state = f"posted {p['posted']}: {p.get('permalink', '')}" if 'id' in p else 'not posted yet'
        if 'facebook' in p:
            state += f"; Facebook: {p['facebook']['permalink']}"
        L += [(f'## The lesson video ({state})' if sh.get('whole') else f'## Short {mp4.stem.rsplit("-", 1)[1]} ({state})'),
              f'Video `{mp4.name}` · cover `{cover_path(mp4).name}`', '', '```', caption(sh, tags), '```', '']
    (extras(out) / 'instagram.md').write_text('\n'.join(L), encoding='utf-8')


def prepare(lesson, log=print):
    """Covers + captions for a finished lesson (easy.py does this right after the YouTube texts)."""
    import lessons
    from tools import slug
    lesson, out, data = lesson_files(lesson)
    profile, assets = lessons.load_profile(lesson.parent)
    thumbs = paths.WORK / slug(lesson.name) / 'thumbs'
    saved = thumbs / 'candidates.json'
    frames = {c['n']: thumbs / c['file'] for c in json.loads(saved.read_text(encoding='utf-8'))} if saved.exists() else {}
    if frames:
        make_covers(out, data, frames, assets['logo'], profile['palette'], log)
    write_md(lesson, out, data)


def load(out):
    """The lesson's instagram-posted.json ({} when it has none). Raises OSError when Drive cannot be read: a Drive
    that is away for a moment is never taken for 'nothing posted yet' (Path.exists() says False then)."""
    p = extras(out) / POSTED
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except FileNotFoundError:
        if absent(p):
            return {}
        raise


def absent(path):
    """True only when path's folder (or the folder above it) can be listed and path is not in it: a file that
    really does not exist. Raises OSError when neither folder can be listed (Drive away)."""
    path = Path(path)
    try:
        return path.name not in os.listdir(path.parent)
    except FileNotFoundError:
        return path.parent.name not in os.listdir(path.parent.parent)


def last_post(root):
    times = [datetime.fromisoformat(t) for _, _, p in records(root, POSTED)
             for r in json.loads(p.read_text(encoding='utf-8')).values()
             for t in (r.get('posted_utc'), r.get('facebook', {}).get('posted_utc')) if t]
    return max(times, default=None)


def long_video(lesson):
    """(mp4, youtube.json data, YouTube record) of the lesson's long video, when YouTube has it public; else None."""
    lesson, out, data = lesson_files(lesson)
    mp4 = next(out.glob('*-video.mp4'), None)
    p = extras(out) / 'youtube-applied.json'
    rec = (json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}).get(mp4.name if mp4 else '', {})
    at = rec.get('publish_at')
    if not mp4 or not rec.get('public') or not at or datetime.fromisoformat(at.replace('Z', '+00:00')) > datetime.now(timezone.utc):
        return None
    return mp4, data, rec
