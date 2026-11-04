"""Rule check of a finished lesson, before Sinem uploads (RUNBOOK.md step 3):
    python -I review.py "<lesson folder>" ["<channel root>" (default: the lesson's parent)]
Prints OK / CHECK lines for the channel's rules (Shorts length and count, titles, hashtags, face overlap, files,
Instagram covers, publish times) and exits with 1 when something needs a look."""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from tools import extras, probe, records, slug

WORK_ROOT = Path(r'D:\YT\easy-work')
problems = []


def say(ok, text):
    print(('  OK     ' if ok else '  CHECK  ') + text)
    if not ok:
        problems.append(text)


def hashtags(text):
    return re.findall(r'(?<!\w)#\w+', text)


def output(lesson):
    for name in ('Output (Easy)', 'Output'):
        if (extras(lesson / name) / 'status.json').exists():
            return lesson / name
    return lesson / 'Output'


def schedule(root):
    """Publish times still ahead, over all lessons: the channel posts at most one upload a day, 3 h apart."""
    now = datetime.utcnow()
    times = []
    for les, _, p in records(root, 'youtube-applied.json'):
        for f, r in json.loads(p.read_text(encoding='utf-8')).items():
            t = r.get('publish_at')
            if t and not r.get('public'):
                t = datetime.fromisoformat(t.replace('Z', ''))
                if t > now:
                    times.append((t, f'{les.name}/{f}'))
    times.sort()
    for (a, fa), (b, fb) in zip(times, times[1:]):
        say(b - a >= timedelta(hours=3), f'{fa} and {fb} go public {(b - a).total_seconds() / 3600:.1f} h apart')
    days = [t.date() for t, _ in times]
    say(len(days) == len(set(days)), f'{len(times)} scheduled upload(s), at most one a day')


def main(lesson, root=None):
    lesson = Path(lesson)
    root = Path(root or lesson.parent)
    out = output(lesson)
    print(f'{lesson.name}  ({out})')
    x = extras(out)                                        # everything but the videos
    st = json.loads((x / 'status.json').read_text(encoding='utf-8')) if (x / 'status.json').exists() else {}
    if st.get('state') != 'done':
        say(False, f"not finished: {st.get('state', 'not started')} {st.get('step', '')} {st.get('error', '')}".strip())
        return
    meta = json.loads((x / 'youtube.json').read_text(encoding='utf-8'))
    profile = json.loads((root / '_channel' / 'channel.json').read_text(encoding='utf-8'))
    shorts_max = profile.get('shorts_max', 3)
    kinds = {sh['version']: sh.get('kind', '') for sh in meta.get('shorts', [])}

    print('Files')
    mp4s = sorted(out.glob('*.mp4'))
    video = [m for m in mp4s if m.stem.endswith('-video')]
    shorts = [m for m in mp4s if re.search(r'-short(-\d+)?$', m.stem)]
    say(bool(video), f'main video: {video[0].name if video else "missing"}')
    for m in video:
        p = probe(str(m))
        say(p['width'] > p['height'], f"{m.name}: {p['width']}x{p['height']}, {p['duration'] / 60:.1f} min"
            + ('' if p['width'] > p['height'] else ' (vertical long video: fine for a Short-style lesson)'))
    say(len(shorts) <= shorts_max, f'{len(shorts)} Short(s) (max {shorts_max}, one a day)')
    for m in shorts:
        p = probe(str(m))
        v = m.stem.split('-short')[-1]
        kind = kinds.get('short' + v, '')
        cap = 60
        say(p['height'] > p['width'] and p['duration'] <= cap,
            f"{m.name}: {p['width']}x{p['height']}, {p['duration']:.0f} s (kind {kind or '-'}, best under {cap} s)")
    for f in ['thumbnail-1.jpg', 'thumbnail-2.jpg', 'thumbnail-3.jpg', 'youtube.md']:
        say((x / f).exists(), f)
    for m in video + shorts:
        for lang in ('tr', 'de'):
            say((x / f'{m.stem}-subtitles-{lang}.srt').exists(), f'{m.stem}-subtitles-{lang}.srt')

    print('Texts')
    for t in meta['titles']:
        say('#' not in t and len(t) <= 100, f'title ({len(t)} chars): {t}')
    for sh in meta.get('shorts', []):
        base = sh['title'].split(' | ')[0]
        say('#' not in sh['title'] and len(base) <= 60 and len(sh['title']) <= 100, f"{sh['version']} title: {sh['title']}")
        n = len(hashtags(sh['description']))
        say(n <= 3, f"{sh['version']} description: {n} hashtag(s)")
    for i, d in enumerate(meta['descriptions'], 1):
        n = len(hashtags(d))
        say(n <= 3, f'description {i}: {n} hashtag(s)')
    say(len(meta.get('chapters', [])) >= 3 or not video, f"{len(meta.get('chapters', []))} chapters")
    say(bool(meta.get('pinned_comment')), f"pinned comment: {meta.get('pinned_comment', '')[:80]}")
    say(bool(meta.get('playlist')), f"playlist: {meta.get('playlist', '')}")

    print('Picture')
    rep = WORK_ROOT / slug(lesson.name) / 'report.json'
    if rep.exists():
        qa = json.loads(rep.read_text(encoding='utf-8')).get('qa', {})
        for v, q in qa.items():
            say(not q.get('face_covered'), f"{v}: text on her face in {len(q.get('face_covered', []))} line(s)")
    else:
        say(False, f'no QA report at {rep}')
    print(f"  look   {', '.join(p.name for p in sorted(x.glob('check-frames-*.jpg')))}")

    print('Instagram')
    for m in shorts:
        n = m.stem.split('-short')[-1]
        say((x / f'cover-short{n}.jpg').exists(), f'cover-short{n}.jpg')
    say((x / 'instagram.md').exists() or not shorts, 'instagram.md')

    print('Schedule (all lessons)')
    schedule(root)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(*sys.argv[1:3])
    print(f'\n{len(problems)} thing(s) to check' if problems else '\nAll rules OK')
    sys.exit(1 if problems else 0)
