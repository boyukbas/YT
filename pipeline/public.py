"""The public status page: https://boyukbas.github.io/YT/status.html (docs/status.html reads status.json from here).

Every video of every lesson, newest first: kind (video / Short), length, state (being made, ready to upload, uploaded,
scheduled, public) with its date/time, and for public videos the YouTube title, link and view/like counts, plus the
Instagram Reel when there is one. Lesson folder names are masked (first 2 letters); nothing private goes out: no
errors, no unpublished titles, no file paths.

The data goes to the repo's own `status` branch (read via raw.githubusercontent.com/boyukbas/YT/refs/heads/status/; the short /status/ form 404s) (one commit, force-pushed), never to main: main's history stays clean
and the iOS workflow (main only) doesn't run. Local clone of that branch: D:\\YT\\status-site.
Pushed when something changed, and at least hourly (with fresh view counts) so "updated" shows the PC is alive.
YouTube API: one videos.list + one channels.list per hour (~2 of the 10,000 daily units).

  python public.py "<Drive root>" [--push]      build (and publish) now, print the JSON
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))          # python -I (as the watcher runs it) doesn't add it
import easy
import instagram as IG
from tools import extras, probe

SITE = Path(os.environ.get('STATUS_SITE', r'D:\YT\status-site'))
REMOTE = 'https://github.com/boyukbas/YT.git'
BRANCH = 'status'
STATS = easy.WORK_ROOT / 'public-stats.json'          # cached YouTube counts + public titles
PROBES = easy.WORK_ROOT / 'public-probes.json'        # cached video sizes/lengths (probing Drive files is slow)
STATS_EVERY = 3600                                    # seconds between YouTube refreshes
PUSH_EVERY = 3600                                     # push at least this often even when nothing changed


def mask(name):
    """'Um etwas bitten' -> 'Um•••••••••••••': the first 2 letters, the rest hidden."""
    return name[:2] + '•' * max(len(name) - 2, 0)


def jload(p):
    try:
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def iso(t):
    """Any of the pipeline's time formats -> ISO UTC ('2026-11-04T14:27:36Z')."""
    if not t:
        return None
    if re.match(r'^\d{4}-\d\d-\d\d \d\d:\d\d$', t):                       # status.json / records: Istanbul local time
        d = datetime.strptime(t, '%Y-%m-%d %H:%M').replace(tzinfo=IG.ISTANBUL)
    else:
        d = datetime.fromisoformat(t.replace('Z', '+00:00'))
    return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def video_info(mp4, cache):
    key = f'{mp4.name}:{mp4.stat().st_size}'
    if key not in cache:
        p = probe(mp4)
        cache[key] = {'seconds': round(p['duration']), 'vertical': p['height'] > p['width']}
    return cache[key]


def items(root, cache):
    """[{...}] one per video (or per lesson while it is being made), without YouTube counts."""
    out_items = []
    now = time.time()
    for lesson in sorted(p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(('_', '.'))):
        videos = easy.lesson_videos(lesson)
        out = easy.output_dir(lesson)
        x = extras(out)
        st = jload(x / 'status.json')
        base = {'lesson': mask(lesson.name)}
        if not st:
            if videos:
                newest = max(v.stat().st_mtime for v in videos)
                out_items.append({**base, 'kind': 'lesson', 'state': 'new',
                                  'time': datetime.fromtimestamp(newest, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
            continue
        state, updated = st.get('state'), iso(st.get('updated'))
        if state != 'done':
            out_items.append({**base, 'kind': 'lesson', 'time': updated,
                              'state': {'working': 'making', 'failed': 'stopped', 'waiting': 'waiting'}.get(state, state),
                              **({'step': st.get('step', '')[:60]} if state == 'working' else {})})
            continue
        applied = jload(x / 'youtube-applied.json')
        posted = IG.load(out)
        hand = re.search(r'https://youtu\.be/([\w-]{11})', st.get('youtube', '')) if st.get('youtube_complete') else None
        for mp4 in sorted(out.glob('*.mp4')):
            info = video_info(mp4, cache)
            short = info['vertical'] and info['seconds'] <= 180
            it = {**base, 'kind': 'short' if short else 'video', 'seconds': info['seconds']}
            m = re.search(r'-short-(\d+)$', mp4.stem)
            if m:
                it['part'] = int(m.group(1))
            rec = applied.get(mp4.name)
            if not rec and hand and mp4.stem.endswith('-video'):      # made by hand: the record is only a note
                rec = {'id': hand.group(1), 'public': True}
            if not rec:
                it.update(state='ready', time=updated)
            else:
                at = rec.get('publish_at')
                it['youtube'] = rec['id']
                if at and datetime.fromisoformat(at.replace('Z', '+00:00')) > datetime.now(timezone.utc):
                    it.update(state='scheduled', time=iso(at))
                elif rec.get('public') or at:
                    it.update(state='public', time=iso(at) if at else None)
                else:
                    it.update(state='uploaded', time=iso(rec.get('updated')) or updated)
            reel = posted.get(mp4.name, {})
            if 'id' in reel:
                it['instagram'] = {'url': reel.get('permalink'), 'time': iso(reel.get('posted_utc'))}
            out_items.append(it)
    return out_items


def refresh_stats(found, stats):
    """Views/likes/comments, the public title and publish time of every public video; the channel's subscribers.
    At most once an hour; on any API problem the old numbers stay."""
    if time.time() - stats.get('_at', 0) < STATS_EVERY:
        return stats
    ids = sorted({i['youtube'] for i in found if i.get('youtube')})
    try:
        import youtube as Y
        yt = Y.api()
        for k in range(0, len(ids), 50):
            for v in yt.videos().list(part='snippet,statistics,status', id=','.join(ids[k:k + 50])).execute()['items']:
                s = v['statistics']
                stats[v['id']] = {'views': int(s.get('viewCount', 0)), 'likes': int(s.get('likeCount', 0)),
                                  'comments': int(s.get('commentCount', 0)), 'privacy': v['status']['privacyStatus'],
                                  'title': v['snippet']['title'], 'published': v['snippet']['publishedAt']}
        ch = yt.channels().list(part='statistics', id=Y.CHANNEL_ID).execute()['items'][0]['statistics']
        stats['_channel'] = {'subscribers': int(ch.get('subscriberCount', 0)), 'views': int(ch.get('viewCount', 0)),
                             'videos': int(ch.get('videoCount', 0))}
        stats['_at'] = time.time()
    except Exception as e:
        print(f'public: YouTube counts not refreshed: {type(e).__name__}: {str(e)[:200]}')
    return stats


def build(root):
    cache = jload(PROBES)
    found = items(root, cache)
    PROBES.write_text(json.dumps(cache), encoding='utf-8')
    stats = refresh_stats(found, jload(STATS))
    STATS.write_text(json.dumps(stats, ensure_ascii=False), encoding='utf-8')
    for it in found:
        s = stats.get(it.get('youtube', ''))
        if s and it['state'] == 'public' and s['privacy'] == 'public':        # only what YouTube shows publicly anyway
            it.update(title=s['title'], views=s['views'], likes=s['likes'], comments=s['comments'],
                      time=iso(s['published']))                               # YouTube's own publish time
        elif it.get('state') != 'public' or (s and s['privacy'] != 'public'):
            it.pop('youtube', None)                                         # no links to unlisted/private videos
    found.sort(key=lambda i: i.get('time') or '', reverse=True)
    return {'channel': {'name': 'Almanca Dil Atölyesi', 'url': IG.CHANNEL, **stats.get('_channel', {})},
            'videos': found,
            'counts_at': datetime.fromtimestamp(stats['_at'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            if stats.get('_at') else None}


def git(*args, timeout=60):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}   # never wait for a login prompt
    return subprocess.run(['git', '-C', str(SITE), *args], capture_output=True, text=True, timeout=timeout, env=env)


def publish(data):
    """Write status.json in the status-branch clone; commit (amend: the branch keeps one commit) and force-push when
    the content changed or the last push is an hour old. Returns a short note, or None when nothing was pushed."""
    if not (SITE / '.git').exists():
        SITE.mkdir(parents=True, exist_ok=True)
        git('init', '-b', BRANCH)
        git('remote', 'add', 'origin', REMOTE)
    p = SITE / 'status.json'
    old = jload(p)
    strip = lambda d: {k: v for k, v in d.items() if k not in ('generated', 'counts_at')}
    last = old.get('generated')
    stale = not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace('Z', '+00:00'))).total_seconds() >= PUSH_EVERY
    if old and strip(old) == strip(data) and not stale:
        return None
    data = {**data, 'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    git('add', 'status.json')
    has_commit = git('rev-parse', '--verify', 'HEAD').returncode == 0
    r = git('commit', '-q', '--amend' if has_commit else '-m', *(['--no-edit'] if has_commit else ['status page data']))
    if r.returncode and 'nothing' not in (r.stdout + r.stderr):
        return f'public: commit failed: {r.stderr.strip()[:200]}'
    r = git('push', '-q', '--force', 'origin', f'HEAD:{BRANCH}', timeout=120)
    return 'public: pushed' if r.returncode == 0 else f'public: push failed: {r.stderr.strip()[:200]}'


def update(root, push=True):
    """Watcher step: build + publish. Never raises (the status page must not stop the pipeline)."""
    try:
        data = build(root)
        note = publish(data) if push else None
        if note and 'failed' in note:
            print(note)
        return data
    except Exception as e:
        print(f'public: {type(e).__name__}: {str(e)[:300]}')
        return None


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    d = build(sys.argv[1])
    print(json.dumps(d, ensure_ascii=False, indent=1))
    if '--push' in sys.argv:
        print(publish(d))
