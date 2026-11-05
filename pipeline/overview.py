"""One status page for the phone: SINEM-YouTube/STATUS.txt (open it in the Google Drive app).

Every lesson (newest first) with its state, YouTube (uploaded / scheduled / public, with links), Instagram (Reels
posted / queued), what goes out next (publish.next_run: the dashboard's settings, so a pause shows), and what the PC
is doing. Built only from the files the pipeline already keeps (status.json, youtube-applied.json,
instagram-posted.json, publishing.json, publish.db, the watcher log); it calls no API.
Written by the watcher on every run and whenever a lesson's status changes.

  python overview.py "<Drive root>"      write it now and print it
"""
import json, re, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))          # python -I (as the watcher runs it) doesn't add it
import lessons
import paths
import instagram as IG
from tools import Status, extras

NAME = 'STATUS.txt'
ISTANBUL = IG.ISTANBUL
WATCH_LOG = paths.WORK / 'watch.log'


def when(utc_iso):
    """'Wed 04 Nov 19:00' in Istanbul time."""
    t = datetime.fromisoformat(utc_iso.replace('Z', '+00:00'))
    return t.astimezone(ISTANBUL).strftime('%a %d %b %H:%M')


def jload(p):
    try:
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def yt_state(rec, now):
    """'public' / 'scheduled Wed 05 Nov 19:00' / 'uploaded, private' for one youtube-applied record."""
    at = rec.get('publish_at')
    if at and datetime.fromisoformat(at.replace('Z', '+00:00')) > now:
        return f'scheduled {when(at)}'
    if rec.get('public') or at:
        return 'public'
    return 'uploaded, private'


def lesson_block(lesson, now):
    """(sort key, lines) for one lesson folder, or None when it holds nothing of ours."""
    videos = lessons.lesson_videos(lesson)
    out = lessons.output_dir(lesson)
    st = jload(extras(out) / 'status.json')
    if not videos and not st:
        return None
    L = [f'■ {lesson.name}']
    if not st:
        newest = max(v.stat().st_mtime for v in videos)
        L.append('  New recording: the PC starts within 10-20 minutes'
                 if now.timestamp() - newest < 1800 else '  Recording here, not made yet (next watcher run)')
        return newest, L
    state, step, updated = st.get('state'), st.get('step', ''), st.get('updated', '')
    key = time.mktime(time.strptime(updated, '%Y-%m-%d %H:%M')) if updated else 0
    if state == 'working':
        L.append(f'  ⏳ Working: {step} (since {updated[11:]})')
        return key, L
    if state == 'failed':
        L += [f'  ❌ Stopped: {st.get("error", "")[:300]}', '  (Claude can look at it: "check the lesson")']
        return key, L
    if state == 'waiting':
        L.append(f'  ⏸ Waiting: {step[:200]}')
        return key, L

    # done: the files are ready; what happened on YouTube and Instagram
    applied = jload(extras(out) / 'youtube-applied.json')
    data = jload(extras(out) / 'youtube.json')
    main = next(iter(sorted(out.glob('*-video.mp4'))), None)
    shorts = sorted(out.glob('*-short-*.mp4'), key=lambda f: int(re.search(r'(\d+)$', f.stem).group(1)))
    if main:
        rec = applied.get(main.name)
        if rec:
            L.append(f'  YouTube: {yt_state(rec, now)} · https://youtu.be/{rec["id"]}')
        elif st.get('youtube_complete'):                 # e.g. a lesson made by hand: a note instead of a record
            L.append(f'  YouTube: {st.get("youtube", "done")[:200]}')
        else:
            L.append(f'  ✅ Ready ({updated}): waiting for Sinem to upload {main.name} in the YouTube app')
    if shorts:
        recs = [applied.get(s.name) for s in shorts]
        states = [yt_state(r, now) for r in recs if r]
        pub = sum(s == 'public' for s in states)
        sched = sorted(r['publish_at'] for r in recs if r and r.get('publish_at') and yt_state(r, now) != 'public')
        line = f'  Shorts: {pub} of {len(shorts)} public'
        if sched:
            line += f', next {when(sched[0])}'
        if len(states) < len(shorts):
            line += f', {len(shorts) - len(states)} not uploaded yet'
        L.append(line)
    if data:
        reels = IG.reels(out, data)
        posted = IG.load(out)
        done = sum('id' in posted.get(m.name, {}) for m, _ in reels)
        if reels:
            L.append(f'  Instagram: {done} of {len(reels)} Reels posted')
    # a long video just went public: the Studio steps the API can't do
    if main and applied.get(main.name, {}).get('publish_at'):
        t = datetime.fromisoformat(applied[main.name]['publish_at'].replace('Z', '+00:00'))
        if timedelta(0) <= now - t < timedelta(days=3):
            L.append('  By hand in Studio (if not done yet): pin the comment' +
                     (', end screen' if (data.get('chapters') or not data) else ''))
    if st.get('youtube', '').startswith(("YouTube's daily API limit", 'YouTube step failed')):
        L.append(f'  ⚠ {st["youtube"][:200]}')
    return key, L


def next_lines(root, now):
    """(NEXT lines, footer lines) for the publishing platforms, from publish.next_run(root, now) (lazy import).
    On any error: (['  Instagram: status not available (<Type>)'], [])."""
    try:
        import publish
        runs = publish.next_run(Path(root), now)
        foot = [r['footer'] if r['footer'].startswith(f"{r['label']}:") else f"{r['label']}: {r['footer']}."
                for r in runs if r.get('footer')]
        return [f"  {r['text']}" for r in runs if r.get('text')], foot
    except Exception as e:
        return [f'  Instagram: status not available ({type(e).__name__})'], []


def pc_state(now):
    lock = paths.WORK / 'watch.lock'
    if lock.exists() and time.time() - lock.stat().st_mtime < 6 * 3600:
        return 'working'
    runs = []
    if WATCH_LOG.exists():
        runs = re.findall(r'^==== (\S+ \d+:\d+)', WATCH_LOG.read_text(encoding='utf-8', errors='replace')[-20000:], re.M)
    return 'idle' + (f' (last check {runs[-1].split()[-1]}, every 10 min)' if runs else '')


def build(root, idle=False, now=None):
    """The page's text. now: aware UTC (tests); default the current time."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    pc = 'idle (checks every 10 min)' if idle else pc_state(now)
    blocks = [b for l in root.iterdir() if l.is_dir() and not l.name.startswith(('_', '.'))
              for b in [lesson_block(l, now)] if b]
    L = ['ALMANCA DİL ATÖLYESİ · STATUS', f'Updated {now.astimezone(ISTANBUL).strftime("%a %d %b %H:%M")} '
         f'(Istanbul) · PC {pc}', '']
    nxt, foot = next_lines(root, now)
    sched = sorted((rec['publish_at'], l.name) for l in root.iterdir() if l.is_dir()
                   for rec in jload(extras(lessons.output_dir(l)) / 'youtube-applied.json').values()
                   if isinstance(rec, dict) and rec.get('publish_at') and yt_state(rec, now) != 'public')
    for at, name in sched[:3]:
        nxt.append(f'  YouTube: {when(at)} · {name}')
    if nxt:
        L += ['NEXT'] + nxt + ['']
    L.append('LESSONS (newest first)')
    for _, lines in sorted(blocks, key=lambda b: -b[0]):
        L += lines + ['']
    L += ['How it works: Sinem puts a recording in a lesson folder → the PC makes the video (~10 min) → she uploads it',
          'in the YouTube app → the PC fills in title, captions, thumbnail, playlist.', *foot,
          'This page is rewritten by the PC; changes here are lost.']
    return '\n'.join(L) + '\n'


def write(root, idle=False):
    """Write STATUS.txt (every time: its 'Updated' time also shows the PC is alive; ~1 KB). Never raises.
    idle: called by the watcher at the end of its run (its lock is still there, but nothing is being made)."""
    try:
        text = build(root, idle)
        (Path(root) / NAME).write_text(text, encoding='utf-8')
        return text
    except Exception as e:                       # the status page must never stop the pipeline
        print(f'overview: {type(e).__name__}: {e}')
        return None


def hook(out_dir):
    """Status.after_set: a lesson's status changed (out_dir = <root>/<lesson>/Output)."""
    write(Path(out_dir).parent.parent)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(write(sys.argv[1]) or '')
