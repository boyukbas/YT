"""The private channel dashboard (v2): https://yt.boyukbas.com (behind Cloudflare Access: Google login, allowed emails
only).

Five tabs (the page is in dashboard/): Home (what needs a person, what goes out next, the counts and numbers), Publish
(the Reel queue, manual posts, settings), Videos (every video of every lesson), Stats and Activity. The rules live in
publish.py, schedule.py and stats.py; this file checks the login, answers the HTTP API (spec 8), serves the page files
and the covers, and builds Home (attention cards, health line). One background thread reads the numbers every 5 min
(stats.collect) and settles interrupted posts every 10 min (publish.reconcile).

How it is reached: this server listens on 127.0.0.1 only; cloudflared (a named tunnel, started next to it by
dashboard.cmd) carries https://yt.boyukbas.com to it. Cloudflare Access sits in front: nobody reaches the tunnel without
signing in with an allowed Google account. This server checks that again on every request: Cloudflare's signed login
token (Cf-Access-Jwt-Assertion, RS256) must verify against the team's public keys, be for this app (aud) and name an
allowed email. Settings (not in the repo, the repo is public): D:\\YT\\easy-work\\dashboard.json
  {"team": "<team>.cloudflareaccess.com", "aud": "<Application Audience tag>", "emails": ["a@x.com", "b@y.com"]}
Every POST is logged with the email in D:\\YT\\easy-work\\dashboard.log; every change also writes an audit row.

  python dashboard.py serve [--port 8790]           what dashboard.cmd runs
  python dashboard.py serve --dev --fixtures --port 8791
                                                    local test: no login check, the test guard (no network), a fake
                                                    platform, fake YouTube and a fresh fixture channel folder; needs
                                                    PIPELINE_WORK set to a scratch folder (--root PATH: that scratch
                                                    channel folder instead of the fixtures)
  python dashboard.py json ["<Drive root>"]         print the Home and Videos data
"""
import argparse, base64, hashlib, io, json, math, os, re, subprocess, sys, threading, time, traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).parent))          # python -I (as the scheduled task runs it) doesn't add it
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from PIL import Image, ImageOps

import lessons
import paths
import instagram as IG
import publish
import schedule as S
import stats
from tools import extras, probe

HERE = Path(__file__).parent
PAGE = HERE / 'dashboard'                               # the page: index.html, app.css and the tab modules
LOGO = HERE.parent / 'docs' / 'brand' / 'logo-trde.png'
CONFIG = paths.WORK / 'dashboard.json'
LOG = paths.WORK / 'dashboard.log'
PROBES = paths.WORK / 'dashboard-probes.json'       # cached video lengths (probing Drive files is slow)
COVER_CACHE = paths.WORK / 'cover-cache'
HEARTBEAT = paths.WORK / 'watcher.json'
WATCH_LOCK = paths.WORK / 'watch.lock'
WATCH_LOG = paths.WORK / 'watch.log'
REAL_WORK = r'D:\YT\easy-work'                          # --dev never runs on it (a str: the test guard refuses a
                                                        # Path into it in modules imported before it)
WATCHER = 'YT Easy mode watcher'
CHANNEL_NAME = 'Almanca Dil Atölyesi'

STATIC = {'/': 'index.html', '/index.html': 'index.html', '/app.css': 'app.css', '/app.js': 'app.js',
          '/home.js': 'home.js', '/publish.js': 'publish.js', '/videos.js': 'videos.js', '/stats.js': 'stats.js',
          '/activity.js': 'activity.js'}            # files in PAGE; '/logo.png' -> docs/brand/logo-trde.png
GONE = ('/api/status', '/api/action', '/dashboard.js')
GONE_TEXT = 'Reload this page: the dashboard was updated.'
TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.js': 'text/javascript; charset=utf-8', '.png': 'image/png'}
PATTERNS = {'platform': re.compile(r'[a-z]{2,20}'), 'id': re.compile(r'[0-9a-f]{12}'),
            'job': re.compile(r'j[0-9a-f]{10}'), 'check': re.compile(r'[0-9a-f]{16}')}     # spec 8.1 (fullmatch)
COVER = re.compile(r'/api/cover/([a-z]{2,20})/([0-9a-f]{12})\.jpg')
RULES = ('lesson_stopped', 'lesson_waiting', 'ready_upload', 'post_unresolved', 'post_failed', 'items_failed',
         'breaker', 'paused', 'key_refused', 'key_expiring', 'auto_suspended', 'settings_invalid', 'record_pending',
         'auto_off_waiting', 'queue_low', 'watcher_stale', 'drive_missing', 'auto_missed')                  # spec 9.2, in order
RANK = {**{r: n for n, r in enumerate(RULES)}, 'lessons_error': 0, 'publish_error': 3}
SOURCES = {'youtube': 'YouTube', 'instagram': 'Instagram', 'instagram_limit': 'Instagram limit',
           'instagram_permissions': 'Instagram permissions'}
DEV_STATE = ('fake-records.json', 'publish.db', 'publish.db-wal', 'publish.db-shm', 'publishing.json',
             'stats.db', 'stats.db-wal', 'stats.db-shm', 'instagram-paused', 'instagram-posting.lock',
             'dashboard-probes.json')                    # --dev --fixtures starts these afresh in the scratch folder

BODY_MAX = 16384                                        # bytes of a POST body
DRY_RUNS = 120                                          # settings dry runs per minute per email (own budget)
CALL_WAIT = 40                                          # seconds a Meta check may hold a request (Cloudflare: 100 s)
COVER_RESCAN, COVER_MAP_AGE = 5, 300                    # cover id map: an unknown id rescans at most every 5 s;
                                                        # the map is rebuilt after 5 min (seconds)
COVER_MAX = 15 * 1024 * 1024
COVER_KEEP = 200                                        # files in COVER_CACHE
COVER_PIXELS = 40_000_000                               # larger images are refused before decoding
NAVY = (16, 22, 40)                                     # behind transparent covers
TAIL_BYTES = 200 * 1024
STATS_EVERY, RECONCILE_EVERY = 300, 600                 # seconds (background thread)
WATCHER_STALE = 1800                                    # watcher.json older than this: the watcher_stale card
LOCK_FRESH = 6 * 3600                                   # a watch.lock younger than this means a run is busy (easy.watch)


# ---------- time and small helpers ----------

def _utcnow() -> datetime:
    """Now (aware UTC). Everything without a now= parameter uses it; tests patch it."""
    return datetime.now(timezone.utc)


def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return _utcnow()
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _time(s) -> datetime | None:
    """An ISO time as aware UTC; None when missing or unreadable."""
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _iso(t: datetime | None) -> str | None:
    return S.iso(t) if t else None


def ago(t: datetime | None, now: datetime) -> str:
    """'just now', '4 min ago', '3 h ago' or '2 days ago' (the page's own wording)."""
    if t is None:
        return ''
    m = int((now - t).total_seconds() / 60 + 0.5)
    if m < 1:
        return 'just now'
    return f'{m} min ago' if m < 60 else f'{int(m / 60 + 0.5)} h ago' if m < 2880 else f'{int(m / 1440 + 0.5)} days ago'


def _short(e: BaseException) -> str:
    return publish.redact(f'{type(e).__name__}: {e}')[:300]


def _say(what: str, e: BaseException) -> None:
    """One redacted line in dashboard-server.log; the dashboard goes on."""
    print(f'dashboard: {what} ({_short(e)})', flush=True)


def _registry() -> dict:
    """publish.registry(), or {} when it cannot be read (the error is printed)."""
    try:
        return publish.registry()
    except Exception as e:
        _say('platforms not available', e)
        return {}


def _write_json(path: Path, data) -> None:
    """A cache file, written atomically; a failure only costs time later."""
    tmp = path.with_name(f'{path.name}.tmp-{os.getpid()}-{threading.get_ident()}')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)
    except OSError as e:
        _say(f'{path.name} not saved', e)
        _drop(tmp)


def _drop(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------- data ----------

def drive_root():
    """The channel folder on whatever letter Google Drive has today (as watch.cmd does)."""
    for letter in 'JGHIKLMNOPQRSTUVWXYZEF':
        p = Path(f'{letter}:/My Drive/SINEM-YouTube')
        if p.is_dir():
            return p
    return None


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


def planned_title(lesson, data, mp4):
    """The title youtube.py will give this video (same choice as youtube.wanted / apply)."""
    titles = data.get('titles') or []
    if not titles:
        return None
    settings = jload(lesson / 'settings.json')
    main = titles[min(max(int(settings.get('title', 1)), 1), len(titles)) - 1]
    if re.search(r'-short(-\d+)?$', mp4.stem):
        own = next((s for s in data.get('shorts', []) if s.get('version') and mp4.stem.endswith('-' + s['version'])), {})
        return own.get('title') or data.get('short_title') or main
    return main


def items(root, cache):
    """[{...}] one per video (or per lesson while it is being made / stopped), without YouTube counts."""
    found = []
    for lesson in sorted(p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(('_', '.'))):
        videos = lessons.lesson_videos(lesson)
        out = lessons.output_dir(lesson)
        x = extras(out)
        st = jload(x / 'status.json')
        base = {'lesson': lesson.name}
        if not st:
            if videos:
                newest = max(v.stat().st_mtime for v in videos)
                found.append({**base, 'kind': 'lesson', 'state': 'new', 'clips': len(videos),
                              'time': datetime.fromtimestamp(newest, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
            continue
        state, updated = st.get('state'), iso(st.get('updated'))
        if state != 'done':
            it = {**base, 'kind': 'lesson', 'time': updated, 'step': st.get('step', ''),
                  'state': {'working': 'making', 'failed': 'stopped', 'waiting': 'waiting'}.get(state, state)}
            if st.get('error'):
                it['error'] = st['error']
            if state == 'failed':
                it['retry'] = lessons.retry_marker(lesson).exists()             # pressed, waiting for the watcher
            found.append(it)
            continue
        applied = jload(x / 'youtube-applied.json')
        plan = jload(x / 'youtube.json')
        posted = IG.load(out)
        hand = re.search(r'https://youtu\.be/([\w-]{11})', st.get('youtube', '')) if st.get('youtube_complete') else None
        for mp4 in sorted(out.glob('*.mp4')):
            info = video_info(mp4, cache)
            short = info['vertical'] and info['seconds'] <= 180
            it = {**base, 'kind': 'short' if short else 'video', 'seconds': info['seconds'], 'file': mp4.name}
            m = re.search(r'-short-(\d+)$', mp4.stem)
            if m:
                it['part'] = int(m.group(1))
            title = planned_title(lesson, plan, mp4)
            if title:
                it['title'] = title                                   # replaced by YouTube's own once uploaded
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
            found.append(it)
    return found


_build_lock = threading.Lock()


def scan(root: Path) -> list[dict]:
    """items(root) with the probe cache in PROBES, one scan at a time (two phones refreshing at once)."""
    with _build_lock:
        cache = jload(PROBES)
        cache = cache if isinstance(cache, dict) else {}
        found = items(root, cache)
        _write_json(PROBES, cache)
    return found


def youtube_numbers() -> dict:
    """stats.youtube_cache() (the old dashboard-stats.json shape), or {} when the numbers cannot be read."""
    try:
        numbers = stats.youtube_cache()
    except Exception as e:
        _say('YouTube numbers not available', e)
        return {}
    return numbers if isinstance(numbers, dict) else {}


def merged(root: Path) -> tuple[list[dict], dict]:
    """(scan(root) with the latest YouTube numbers merged in as v1's build() did, newest first; the numbers).
    A stopped lesson's error text is redacted."""
    found, numbers = scan(root), youtube_numbers()
    for it in found:
        s = numbers.get(it.get('youtube', ''))
        if isinstance(s, dict) and s:
            it.update({k: s[k] for k in ('title', 'privacy', 'views', 'likes', 'comments') if k in s})
            if it['state'] == 'public' and s.get('privacy') == 'public' and s.get('published'):
                it['time'] = iso(s['published'])                          # YouTube's own publish time
        if it.get('error'):
            it['error'] = publish.redact(it['error'])
    found.sort(key=lambda i: i.get('time') or '', reverse=True)
    return found, numbers


def reel_states(root: Path, views: dict | None = None) -> dict[tuple[str, str], dict]:
    """{(lesson, file name): {'platform', 'id', 'state'}} for every publish candidate (the first platform wins).
    The queue lists waiting, failed (and checking) and skipped items in full; every other candidate is posted.
    One candidates scan per platform (views: see _views)."""
    out, views = {}, _views(root) if views is None else views
    for name in _registry():
        try:
            v = views.get(name)
            found = v.items if v is not None else publish.items(root, name)[0]
            q = publish.queue(root, name, v=v)
        except Exception as e:
            _say(f'{name} queue not read', e)
            continue
        states = {j['id']: j['state'] for g in ('waiting', 'failed', 'skipped', 'posted') for j in q.get(g) or []}
        for it in found:
            out.setdefault((it.lesson, it.file.name), {'platform': name, 'id': it.id,
                                                       'state': states.get(it.id, 'posted')})
    return out


def cover_ids(root: Path, views: dict | None = None) -> dict[tuple[str, str], dict]:
    """{(lesson, file name): {'platform', 'id'}} of a publish candidate whose cover file exists on Drive, YouTube's
    first (the picture YouTube will get), for the Videos tab before YouTube has its own thumbnail (2026-11-06)."""
    out, views = {}, _views(root) if views is None else views
    for name in sorted(_registry(), key=lambda n: n != 'youtube'):
        try:
            v = views.get(name)
            p = publish.platform(name)
            for it in (v.items if v is not None else publish.items(root, name)[0]):
                if (it.lesson, it.file.name) not in out and p.cover(it) is not None:
                    out[(it.lesson, it.file.name)] = {'platform': name, 'id': it.id}
        except Exception as e:
            _say(f'{name} covers not read', e)
    return out


def videos_view(root: Path) -> dict:
    """The GET /api/videos body. items() merged with stats.youtube_cache() exactly as build() merged the old
    cache, sorted newest first. Each video gets 'reel' {'platform', 'id', 'state'} when it is a publish
    candidate (matched on lesson and file name), else null, and 'cover' {'platform', 'id'} (for
    /api/cover/<platform>/<id>.jpg) when one of its candidates has a cover on Drive, else null."""
    found, numbers = merged(root)
    views = _views(root)
    reels, covers = reel_states(root, views), cover_ids(root, views)
    for it in found:
        key = (it['lesson'], it['file']) if it.get('file') else None
        it['reel'] = reels.get(key) if key else None
        it['cover'] = covers.get(key) if key else None
    ch = numbers.get('_channel') if isinstance(numbers.get('_channel'), dict) else {}
    at = numbers.get('_at')
    return {'generated': S.iso(_utcnow()),
            'counts_at': S.iso(datetime.fromtimestamp(at, timezone.utc)) if isinstance(at, (int, float)) and at > 0
            else None,
            'channel': {'name': CHANNEL_NAME, 'url': IG.CHANNEL, **{k: ch.get(k) for k in ('subscribers', 'views', 'videos')}},
            'videos': found}


# ---------- Home ----------

def _views(root: Path, now: datetime | None = None) -> dict:
    """publish.views(root, now): one read of each platform's queue state for the whole request (G17). {} when it
    fails: then each part reads its own and reports its error as before."""
    try:
        return publish.views(root, now)
    except Exception as e:
        _say('queues not read', e)
        return {}


def _card(cid: str, level: str, who: str, title: str, text: str, action: dict | None = None) -> dict:
    return {'id': cid, 'level': level, 'who': who, 'title': title, 'text': text, 'action': action}


def _order(card: dict) -> tuple:
    """Red first; within a level, the rule order of spec 9.2."""
    return card['level'] != 'red', RANK.get(str(card['id']).split(':')[0], len(RULES))


def lesson_cards(found: list[dict]) -> list[dict]:
    """Attention rules 1-3 (spec 9.2) from items(): stopped lessons, lessons waiting for Claude, videos to upload."""
    cards, ready = [], {}
    for it in found:
        name, state, lesson = it['lesson'], it.get('state'), it.get('kind') == 'lesson'
        if lesson and state == 'stopped':
            error = it.get('error') or 'The PC stopped on this lesson.'
            if it.get('retry'):
                cards.append(_card(f'lesson_stopped:{name}', 'red', 'pc', f"'{name}' stopped",
                                   f'{error} Retry is queued: the PC starts on it within 10 minutes.'))
            else:
                cards.append(_card(f'lesson_stopped:{name}', 'red', 'pc', f"'{name}' stopped", error,
                                   {'kind': 'call', 'label': 'Retry', 'endpoint': '/api/lesson/retry',
                                    'body': {'lesson': name}, 'confirm': f"Run '{name}' again from the start?",
                                    'ack': None}))
        elif lesson and state == 'waiting':
            cards.append(_card(f'lesson_waiting:{name}', 'yellow', 'pc', f"'{name}' is waiting",
                               it.get('step') or 'Waiting for a Claude answer.'))
        elif state == 'ready':
            ready[name] = ready.get(name, 0) + 1
    for name, n in ready.items():
        cards.append(_card(f'ready_upload:{name}', 'yellow', 'sinem', f"'{name}' is ready",
                           f'{n} video{"s" if n != 1 else ""} ready to upload in the YouTube app',
                           {'kind': 'link', 'label': 'Open Videos', 'href': '#videos/ready'}))
    return cards


def watcher(now: datetime | None = None) -> dict:
    """{'ok', 'at', 'busy_since', 'text', 'stale'}: the watcher's last sign of life (watcher.json; before the new
    watcher writes one, watch.log's time) and a fresh watch.lock (a long render is busy, not stale)."""
    now, beat = _now(now), jload(HEARTBEAT)
    at = (_time(beat.get('at')) if isinstance(beat, dict) else None) or _mtime(HEARTBEAT) or _mtime(WATCH_LOG)
    lock = _mtime(WATCH_LOCK)
    busy = lock if lock and (now - lock).total_seconds() < LOCK_FRESH else None
    stale = busy is None and (at is None or (now - at).total_seconds() > WATCHER_STALE)
    text = (f'PC watcher busy since {S.local(busy):%H:%M}' if busy else 'PC watcher: no run seen yet' if at is None
            else f'PC watcher last ran {ago(at, now)}' if stale else f'PC watcher {ago(at, now)}')
    return {'ok': not stale, 'at': _iso(at), 'busy_since': _iso(busy), 'text': text, 'stale': stale}


def attention(root: Path | None, now: datetime | None = None, found: list[dict] | None = None,
              views: dict | None = None) -> list[dict]:
    """Home's attention list: lesson rules 1-3, watcher_stale and drive_missing (spec 9.2), then
    publish.attention(root). Red items first; within a level, the rule order. found: items() already read;
    views: see _views."""
    now = _now(now)
    cards = lesson_cards(found if found is not None else merged(root)[0]) if root is not None else []
    w = watcher(now)
    if w['stale']:
        seen = f'Its last run was {ago(_time(w["at"]), now)}.' if w['at'] else 'No run of it was seen yet.'
        cards.append(_card('watcher_stale', 'red', 'pc', 'The PC watcher is not running',
                           f'{seen} Lessons and automatic posts wait until it runs again: check that the PC is on '
                           f"and the task '{WATCHER}' runs."))
    if root is None:
        cards.append(_card('drive_missing', 'red', 'pc', 'Google Drive is not available on the PC',
                           'The PC cannot see the SINEM-YouTube folder: lessons and posts wait until it is back. '
                           'Check that Google Drive is running on the PC.'))
    else:
        try:
            cards += publish.attention(root, now, views=views)
        except Exception as e:
            cards.append(_card('publish_error', 'red', 'pc', 'Publishing could not be read', _short(e)))
    cards += key_cards(now)
    return sorted(cards, key=_order)


def drive_health(root: Path | None) -> dict:
    if root is None:
        return {'ok': False, 'letter': None, 'text': 'Drive not available'}
    d = Path(root).drive
    letter = d[0].upper() if re.fullmatch(r'[A-Za-z]:', d) else None
    return {'ok': True, 'letter': letter, 'text': f'Drive {letter}:' if letter else 'Drive OK'}


def platform_health(now: datetime | None = None) -> list[dict]:
    """Each platform's set_up() and breaker as {'name', 'ok', 'set_up', 'breaker', 'text'}; a platform module that
    does not load shows as not available. A key that ends soon (key_warning) shows instead of 'key OK' (F52), and
    a platform that says wait (publish's paused hook: YouTube's used-up daily quota) instead of both."""
    plats, out = _registry(), []
    for name in [*plats, *(n for n in publish.PLATFORMS if n not in plats)]:
        p = plats.get(name)
        try:
            if p is None:
                raise LookupError('not available')
            up, b = bool(p.set_up()), publish.breaker(p.breaker)
        except Exception as e:
            label = getattr(p, 'label', None) or publish.LABELS.get(name, name.title())
            out.append({'name': name, 'ok': False, 'set_up': False, 'breaker': None,
                        'text': f'{label} not available' if p is None else f'{label}: {_short(e)}'})
            continue
        state, until = b['state'], _time(b.get('until'))
        refused = up and state == 'closed' and _refused(p) is not None
        held = state == 'closed' or (state == 'cooling' and b.get('kind') == 'rate')   # one cause, one time
        wait = _time((publish._paused(p, _now(now)) or {}).get('until')) if up and held else None
        wait = max(wait, until) if wait and until else wait
        text = (f'{p.label} not set up' if not up else f'{p.label} stopped: test the connection' if state == 'stopped'
                else f'{p.label} daily limit used up · back {_when(wait, now)}' if wait and state == 'cooling'
                else f'{p.label} paused until {S.local(until):%H:%M}' if state == 'cooling' and until
                else f'{p.label} paused' if state != 'closed'
                else f'{p.label} key refused: test the connection' if refused
                else f'{p.label} daily limit used up · back {_when(wait, now)}' if wait
                else _key_warning(p, now) or f'{p.label} key OK')
        out.append({'name': name, 'ok': up and state == 'closed' and not refused and not wait, 'set_up': up,
                    'breaker': state, 'text': text})
    return out


def _when(t: datetime, now: datetime | None = None) -> str:
    """'11:00' today, else 'Sat 7 Nov 11:00' (Istanbul time), so a time tomorrow never reads as one already past."""
    return f'{S.local(t):%H:%M}' if S.local(t).date() == S.local(_now(now)).date() else S.fmt(t)


def _refused(p) -> str | None:
    """Why the numbers' collector saw Meta refuse this platform's key (an auth error; S17: the collector never
    trips the breaker), or None. Only for the platform whose breaker guards the collector's Meta reads."""
    return _key_refused() if p.breaker and p.breaker == getattr(stats, '_BREAKER', 'meta') else None


def _key_warning(p, now: datetime | None = None) -> str | None:
    """p.key_warning(now) without its 'WARNING: ' prefix (the key ends within 14 days), else None. Never raises."""
    try:
        warn = p.key_warning(_now(now))
    except Exception as e:
        _say(f'{p.name} key expiry not read', e)
        return None
    return publish.redact(warn.removeprefix('WARNING: ')) if isinstance(warn, str) and warn.strip() else None


def key_cards(now: datetime | None = None) -> list[dict]:
    """key_refused: a set-up platform with a closed breaker whose key the numbers' collector saw refused.
    key_expiring: a set-up platform whose key ends within 14 days (yellow, F52)."""
    cards = []
    for name, p in _registry().items():
        try:
            up = p.set_up()
            why = _refused(p) if up and publish.breaker(p.breaker)['state'] == 'closed' else None
        except Exception as e:
            _say(f'{name} key state not read', e)
            continue
        warn = _key_warning(p, now) if up else None
        if warn:
            cards.append(_card(f'key_expiring:{name}', 'yellow', 'pc', f'The {p.label} key ends soon', warn))
        if why:
            cards.append(_card(f'key_refused:{name}', 'red', 'pc', f'{p.label} refused the key',
                               f'{publish.redact(why)} Make a new key on the PC (instagram.py system-token), then '
                               'test the connection.',
                               {'kind': 'call', 'label': 'Test connection', 'endpoint': '/api/platform/test',
                                'body': {'platform': name}, 'confirm': None, 'ack': None}))
    return cards


def stats_health(now: datetime) -> dict:
    """{'ok', 'at', 'text'} from stats.sources(): the newest good read; ok is False while a source fails."""
    try:
        src = [s for s in stats.sources() if isinstance(s, dict)]
    except Exception as e:
        _say('number sources not available', e)
        return {'ok': False, 'at': None, 'text': 'Numbers not available'}
    at = max((t for t in (_time(s.get('last_ok')) for s in src) if t), default=None)
    bad = [SOURCES.get(s.get('source'), str(s.get('source'))) for s in src if s.get('fails')]
    text = f'Numbers {ago(at, now)}' if at else 'No numbers yet'
    return {'ok': not bad, 'at': _iso(at), 'text': f'{text} ({", ".join(bad)} not read)' if bad else text}


def health(root: Path | None, now: datetime | None = None) -> dict:
    """The 'health' object of GET /api/home, from watcher.json, watch.lock, drive_root(), each platform's
    set_up() and breaker, and stats.sources()."""
    now = _now(now)
    w = watcher(now)
    return {'watcher': {k: w[k] for k in ('ok', 'at', 'busy_since', 'text')}, 'drive': drive_health(root),
            'platforms': platform_health(now), 'stats': stats_health(now)}


def next_lines(root: Path, now: datetime, found: list[dict], views: dict | None = None) -> list[dict]:
    """Home's 'next': each set-up platform's next automatic post (publish.next_run), then the next YouTube
    video to go public. views: see _views."""
    out, plats = [], _registry()
    for r in publish.next_run(root, now, views=views):
        name, p = r.get('platform'), plats.get(r.get('platform'))
        text = (f'Automatic {p.noun}s are off · {r.get("waiting", 0)} waiting'
                if p and r.get('reason') in ('off', 'stopped_by_file', 'invalid') else r.get('text') or '')
        out.append({'platform': name, 'text': text, 'at': r.get('at'), 'href': f'#publish/{name}' if name else None})
    soon = sorted((i for i in found if i.get('state') == 'scheduled' and i.get('time')), key=lambda i: i['time'])
    if soon:
        i = soon[0]
        text = f'YouTube {"Short" if i.get("kind") == "short" else "video"} goes public {S.fmt(_time(i["time"]))}'
        out.append({'platform': 'youtube', 'text': f'{text} · {i["title"]}' if i.get('title') else text,
                    'at': i['time'], 'href': '#videos'})
    return out


def counts(root: Path, found: list[dict], views: dict | None = None, now: datetime | None = None) -> dict:
    """Home's count strips: waiting, skipped, failed and posted per platform (failed includes checking), and
    the YouTube states of every video and Short (addendum C2). views: see _views."""
    out = {}
    for name in _registry():
        if name == 'youtube':            # its own strip below (ready / uploaded / scheduled / public) says more
            continue
        try:
            q = publish.queue(root, name, now, v=(views or {}).get(name))
        except Exception as e:
            _say(f'{name} counts not read', e)
            continue
        out[name] = {'waiting': len(q['waiting']), 'skipped': len(q['skipped']), 'failed': len(q['failed']),
                     'posted': q['posted_total']}
    yt = dict.fromkeys(('ready', 'uploaded', 'scheduled', 'public'), 0)
    for i in found:
        if i.get('kind') != 'lesson' and i.get('state') in yt:
            yt[i['state']] += 1
    out['youtube'] = yt
    return out


def _drive_gone(root: Path) -> bool:
    """True when the channel folder itself cannot be seen any more (Drive stopped or unmounted)."""
    try:
        return not Path(root).is_dir()
    except OSError:
        return True


def home(root: Path | None, me: str, now: datetime | None = None) -> dict:
    """The GET /api/home body (spec 8.3). Works without Drive: the drive_missing card and stats tiles only."""
    now, found, broken = _now(now), [], None
    if root is not None:
        try:
            found = merged(root)[0]
        except Exception as e:
            if isinstance(e, OSError) and _drive_gone(root):        # the Drive went away during the scan
                _say('Drive not readable', e)
                root = None
            else:                                       # one unreadable lesson or file (a sync lock): the rest shows
                _say('lessons not read', e)
                broken = _card('lessons_error', 'red', 'pc', 'The lessons could not be read', _short(e))
    views = _views(root, now) if root is not None else {}            # one candidates scan per platform (G17)
    cards = attention(root, now, found, views)
    try:
        tiles = stats.tiles(now)
    except Exception as e:
        _say('numbers not available', e)
        tiles = []
    return {'now': S.iso(now), 'me': me, 'channel': {'name': CHANNEL_NAME, 'url': IG.CHANNEL},
            'attention': sorted([broken, *cards], key=_order) if broken else cards, 'health': health(root, now),
            'next': next_lines(root, now, found, views) if root is not None else [],
            'counts': counts(root, found, views, now) if root is not None else {}, 'tiles': tiles}


def log_tail(path: Path, lines: int) -> list[str]:
    """The last `lines` lines of a log file (reads at most its last 200 KB), each passed through publish.redact."""
    try:
        with open(path, 'rb') as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read(TAIL_BYTES)
    except OSError:
        return []
    rows = data.decode('utf-8', errors='replace').splitlines()
    if size > TAIL_BYTES:
        rows = rows[1:]                                  # the first line was cut
    out = []
    for row in rows[-lines:] if lines > 0 else []:
        row = publish.redact(row)                        # before shortening, so no secret is cut below the patterns
        out.append(row if len(row) <= 1000 else row[:999] + '…')
    return out


# ---------- covers ----------

def _not_found() -> publish.PublishError:
    return publish.PublishError(404, 'not_found', 'Not found.')


_cover_lock = threading.Lock()
_cover_items = {}         # platform name -> (platform object, root, monotonic time of the scan, {item id: Item})


def _cover_item(root: Path, platform_name: str, item_id: str):
    """(platform, Item) for a cover, without a Drive scan per cover: one candidates scan fills a map of every
    item, and later covers look their id up there (a page's burst of covers waits for that one scan). An id not
    in the map rescans, at most once per COVER_RESCAN seconds; the map is rebuilt after COVER_MAP_AGE, and a new
    platform object or root starts afresh.
    Raises PublishError(404) for an unknown id. cover() and the path checks still run on every request."""
    p = publish.platform(platform_name)
    with _cover_lock:
        m = _cover_items.get(platform_name)
        age = time.monotonic() - m[2] if m is not None and m[0] is p and m[1] == str(root) else None
        if age is not None and age < COVER_MAP_AGE and item_id in m[3]:
            return p, m[3][item_id]
        if age is not None and age < COVER_RESCAN:
            raise _not_found()
        found = p.candidates(Path(root))[0]
        _cover_items[platform_name] = (p, str(root), time.monotonic(), {i.id: i for i in found})
        item = _cover_items[platform_name][3].get(item_id)
    if item is None:
        raise _not_found()
    return p, item


def cover_image(root: Path, platform_name: str, item_id: str, download: bool = False) -> tuple[bytes, str]:
    """(JPEG bytes, file name) of a candidate's cover. The path must resolve under root, end in .jpg, .jpeg or
    .png, and be at most 15 MB. It is re-encoded with Pillow (540x960 quality 82, or full size quality 92 for
    download) and cached in COVER_CACHE (at most 200 files; the oldest are removed). Raises
    publish.PublishError(404, 'not_found') otherwise."""
    try:
        p, item = _cover_item(root, platform_name, item_id)
        src = p.cover(item)
        full, base = Path(src).resolve(strict=True), Path(root).resolve(strict=True)
        st = full.stat()
    except Exception:
        raise _not_found() from None
    if not full.is_relative_to(base) or full.suffix.lower() not in ('.jpg', '.jpeg', '.png') or not full.is_file() \
            or st.st_size > COVER_MAX:
        raise _not_found()
    name = f'{re.sub(r"[^A-Za-z0-9._-]+", "-", full.stem).strip(".-")[:80] or "cover"}.jpg'
    key = f'{full}|{st.st_size}|{st.st_mtime_ns}|{bool(download)}'
    cached = COVER_CACHE / f'{hashlib.sha1(key.encode()).hexdigest()}.jpg'
    try:
        data = cached.read_bytes()
    except OSError:
        data = None
    if data:
        try:
            os.utime(cached)                             # recently used: pruned last
        except OSError:
            pass
        return data, name
    data = _reencode(full, download)
    _cache_put(cached, data)
    return data, name


def _reencode(path: Path, full_size: bool) -> bytes:
    """A fresh JPEG of an image file (no EXIF or other metadata); 404 not_found for anything Pillow refuses."""
    try:
        with Image.open(path) as im:
            if im.format not in ('JPEG', 'PNG', 'MPO') or im.width * im.height > COVER_PIXELS:
                raise ValueError(f'{im.format} {im.width}x{im.height}')
            im = ImageOps.exif_transpose(im)
            if im.mode in ('RGBA', 'LA', 'P', 'PA'):
                im = im.convert('RGBA')
                flat = Image.new('RGB', im.size, NAVY)
                flat.paste(im, mask=im.getchannel('A'))
                im = flat
            else:
                im = im.convert('RGB')
            if not full_size:
                im.thumbnail((540, 960), Image.Resampling.LANCZOS)
            im.info = {}                                 # no EXIF, ICC or comments from the source
            buf = io.BytesIO()
            im.save(buf, 'JPEG', quality=92 if full_size else 82, optimize=True)
            return buf.getvalue()
    except Exception:
        raise _not_found() from None


def _cache_put(path: Path, data: bytes) -> None:
    """Store one cover and remove the oldest beyond COVER_KEEP. Never raises."""
    tmp = path.with_name(f'{path.stem}.tmp-{os.getpid()}-{threading.get_ident()}')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, path)
        files = sorted(path.parent.glob('*.jpg'), key=lambda f: f.stat().st_mtime)
        for old in files[:max(0, len(files) - COVER_KEEP)]:
            _drop(old)
    except OSError as e:
        _say('cover not cached', e)
        _drop(tmp)


# ---------- login check (Cloudflare Access) ----------

def b64(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


class Access:
    """Verifies Cloudflare Access's login token. Keys: https://<team>/cdn-cgi/access/certs (cached, refetched for an
    unknown key id, at most once a minute)."""

    def __init__(self, cfg):
        self.team, self.aud = cfg['team'], cfg['aud']
        self.emails = {e.strip().lower() for e in cfg['emails']}
        self.keys, self.fetched, self.failed, self.lock = {}, 0, False, threading.Lock()

    def fetch(self):
        """Read the key page (at most once a minute; 10 s after a failed read). While the last read failed this
        raises, so a new key id gets 503 login_unavailable, never 403 'unknown signing key'. One read at a time:
        a request arriving during a read waits for it."""
        with self.lock:
            if time.time() - self.fetched < 60:
                if self.failed:
                    raise RuntimeError('Cloudflare key page unreachable')
                return
            self.fetched = time.time()
            try:
                r = requests.get(f'https://{self.team}/cdn-cgi/access/certs', timeout=10)
                r.raise_for_status()
                keys = {k['kid']: rsa.RSAPublicNumbers(int.from_bytes(b64(k['e']), 'big'),
                                                       int.from_bytes(b64(k['n']), 'big')).public_key()
                        for k in r.json()['keys'] if k.get('kty') == 'RSA'}
            except Exception:
                self.failed, self.fetched = True, time.time() - 50      # try again 10 s later
                raise
            self.keys, self.failed = keys, False

    def email(self, token):
        """The signed-in, allowed email; raises PermissionError otherwise."""
        try:
            head, body, sig = token.split('.')
            h, claims = json.loads(b64(head)), json.loads(b64(body))
        except Exception:
            raise PermissionError('bad login token') from None
        if h.get('alg') != 'RS256':
            raise PermissionError('unexpected token type')
        if h.get('kid') not in self.keys:
            self.fetch()
        key = self.keys.get(h.get('kid'))
        if not key:
            raise PermissionError('unknown signing key')
        try:
            key.verify(b64(sig), f'{head}.{body}'.encode(), padding.PKCS1v15(), hashes.SHA256())
        except Exception:
            raise PermissionError('bad signature') from None
        now = time.time()
        aud = claims.get('aud')
        if self.aud not in (aud if isinstance(aud, list) else [aud]):
            raise PermissionError('token is for another app')
        if claims.get('iss') != f'https://{self.team}':
            raise PermissionError('token from another team')
        if not (claims.get('exp', 0) > now - 30 and claims.get('nbf', 0) <= now + 30):
            raise PermissionError('login expired')
        email = (claims.get('email') or '').lower()
        if email not in self.emails:
            raise PermissionError('this account is not allowed')
        return email


class Limiter:
    """A sliding window: at most n events per `window` seconds per key (thread-safe)."""

    def __init__(self, n: int = 20, window: float = 60):
        self.n, self.window = n, window
        self._seen, self._lock = {}, threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._seen.setdefault(key, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            return True

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Whole seconds until allow(key) can say yes again (0: now)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = [t for t in self._seen.get(key, ()) if t > now - self.window]
            return 0 if len(q) < self.n else max(1, math.ceil(q[0] + self.window - now))


# ---------- actions ----------

def log(email, what):
    """One line in dashboard.log (and the server log), redacted (a request path is the client's text). Never raises."""
    line = publish.redact(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {email} {what}")
    try:
        print(line, flush=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def capped(fn, by: str, what: str):
    """fn() (a Meta check: Check again, Test connection) in its own thread, waiting at most CALL_WAIT seconds, so
    the answer comes well before Cloudflare gives up (100 s). Returns fn's answer (re-raises its error), or None
    when it is still running: it then finishes on its own and its outcome is logged in dashboard.log."""
    box, done = {}, threading.Event()

    def run():
        try:
            box['answer'] = fn()
        except BaseException as e:
            box['error'] = e
        done.set()
        if box.get('late'):
            a, e = box.get('answer'), box.get('error')
            log(by, f'{what} finished after the answer: '
                    + (a.get('message', 'done') if isinstance(a, dict) else getattr(e, 'message', None) or _short(e)))
    threading.Thread(target=run, name=f'dashboard {what}', daemon=True).start()
    if not done.wait(CALL_WAIT):
        box['late'] = True
        if not done.wait(0):
            return None
    if 'error' in box:
        raise box['error']
    return box['answer']


def _key_refused() -> str | None:
    """The reason the numbers' collector last saw Meta refuse the key (stats.key_refused()), or None."""
    try:
        f = getattr(stats, 'key_refused', None)
        return f() if f else None
    except Exception as e:
        _say('key state not read', e)
        return None


def _key_accepted() -> None:
    """A connection test passed: forget the collector's refusal at once (not after the next good read)."""
    try:
        f = getattr(stats, 'clear_key_refused', None)
        if f:
            f()
    except Exception as e:
        _say('key state not cleared', e)


def start_watcher() -> bool:
    """Run the watcher task now (schtasks /run). False when that fails, and always in --dev (the watcher picks a
    retry up within 10 minutes anyway)."""
    if Handler.dev:
        return False
    try:
        return subprocess.run(['schtasks', '/run', '/tn', WATCHER], capture_output=True, text=True,
                              timeout=30).returncode == 0
    except Exception:
        return False


def retry_lesson(root: Path, name, by: str) -> dict:
    """POST /api/lesson/retry (v1's retry action): the retry marker for a stopped lesson, then the watcher. The name
    must be one of the root's lesson folders; it is matched against the folder list, never joined into a path."""
    folders = {p.name: p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(('_', '.'))}
    lesson = folders.get(name) if isinstance(name, str) else None
    if lesson is None:
        raise publish.PublishError(400, 'no_such_lesson', 'No such lesson.')
    st = jload(extras(lessons.output_dir(lesson)) / 'status.json')
    if not isinstance(st, dict) or st.get('state') != 'failed':
        raise publish.PublishError(409, 'not_stopped', 'That lesson is not stopped.')
    m = lessons.retry_marker(lesson)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(f'{by} {time.strftime("%Y-%m-%d %H:%M")}\n', encoding='utf-8')
    started = start_watcher()
    log(by, f'retry {name} (watcher started: {started})')
    publish.audit(by, 'lesson.retry', f"'{name}': retry asked" + (' (the PC started on it).' if started else '.'),
                  data={'lesson': name, 'watcher_started': started})
    return {'ok': True, 'message': 'Retrying: the PC starts on it now.' if started else
            'Retry is queued: the PC starts on it within 10 minutes.'}


# ---------- server ----------

def wait_text(message: str, wait: int, now: datetime | None = None) -> str:
    """A 429 message with the real wait: 'Wait a moment before testing again.' becomes 'Wait 5 min before testing
    again (until 20:15).' (under 90 s: 'Wait 40 s before ...'). A message that already names a time is kept."""
    if re.search(r'\b\d\d:\d\d\b', message):
        return message
    wait = max(1, int(wait))
    span = f'{wait} s' if wait < 90 else f'{-(-wait // 60)} min'
    until = f' (until {S.local(_now(now) + timedelta(seconds=wait)):%H:%M})' if wait >= 90 else ''
    m = re.fullmatch(r'(.*?)Wait a moment (before .+?)\.(.*)', message, re.S)
    if m:
        return f'{m.group(1)}Wait {span} {m.group(2)}{until}.{m.group(3)}'
    return f'{message} Try again in {span}{until}.'.strip()


def _bad(what: str = 'request') -> publish.PublishError:
    return publish.PublishError(400, 'bad_request', f'Bad {what}.')


def _param(value, kind: str) -> str:
    """value when it matches the spec 8.1 pattern for kind, else 400 bad_request."""
    if not isinstance(value, str) or not PATTERNS[kind].fullmatch(value):
        raise _bad(kind)
    return value


def _strings(value, most: int | None = None) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value) and (most is None or len(value) <= most)


def _json_object(raw: bytes | None) -> dict | None:
    try:
        body = json.loads(raw.decode('utf-8'))
    except (AttributeError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def current_root() -> Path | None:
    """The channel folder this server works on: --dev's own root, else Google Drive's (None when not mounted)."""
    return Handler.root if Handler.dev else drive_root()


class Handler(BaseHTTPRequestHandler):
    server_version = 'dashboard'
    sys_version = ''
    timeout = 60                                        # a client that stops sending is dropped
    access = None                                       # Access(...) outside --dev
    dev = False                                         # --dev: no login check, the fake platform
    root = None                                         # --dev: the fixture or --root folder (else drive_root())
    youtube = None                                      # --dev: the fake YouTube client for stats.collect
    limiter = Limiter()                                 # POSTs per signed-in email (spec 5.9)
    dry_limiter = Limiter(DRY_RUNS, 60)                 # settings dry runs: the sheet's live forecast, never a write
    GETS = {'/api/home': 'get_home', '/api/publish': 'get_publish', '/api/preview': 'get_preview',
            '/api/job': 'get_job', '/api/videos': 'get_videos', '/api/stats': 'get_stats',
            '/api/activity': 'get_activity'}
    POSTS = {'/api/post': 'post_post', '/api/queue': 'post_queue', '/api/settings': 'post_settings',
             '/api/settings/reset': 'post_reset', '/api/auto/resume': 'post_resume',
             '/api/platform/test': 'post_test', '/api/job/resolve': 'post_resolve', '/api/lesson/retry': 'post_retry'}

    def log_message(self, fmt, *args):                  # requests aren't logged (POSTs are, in dashboard.log)
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8', headers=None):
        data = b'' if code == 304 else body if isinstance(body, bytes) else (
            json.dumps(body, ensure_ascii=False, default=str) if not isinstance(body, str) else body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        if code != 304:
            self.send_header('Content-Length', str(len(data)))
        for k, v in {'Cache-Control': 'no-store', **(headers or {})}.items():
            self.send_header(k, v)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; img-src 'self' https://i.ytimg.com; style-src 'self' 'unsafe-inline' "
                         "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def error(self, status, code, message, **extra):
        self.send(status, {'error': code, 'message': message, **extra})

    def who(self):
        """The signed-in email, or None after answering 401, 403 or 503."""
        if self.dev:                                    # no login check: answer only this server's own names
            port = self.server.server_address[1]        # (another name resolving to 127.0.0.1 = DNS rebinding)
            if (self.headers.get('Host') or '').strip().lower() not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                self.error(403, 'refused', 'Refused.')
                return None
            return 'dev@localhost'
        if self.access is None:                         # never without the login check outside --dev
            self.error(503, 'login_unavailable', 'The login check is not set up on the PC.')
            return None
        token = self.headers.get('Cf-Access-Jwt-Assertion', '')
        if not token:
            self.error(401, 'not_signed_in', 'Not signed in.')
            return None
        try:
            return self.access.email(token)
        except PermissionError as e:
            self.error(403, 'not_allowed', f'Not allowed: {e}.')
            return None
        except Exception as e:                          # Cloudflare's key page unreachable: refuse, never allow
            self.error(503, 'login_unavailable', f'Login check unavailable: {type(e).__name__}.')
            return None

    def do_GET(self):
        self.answer(self.get)

    def do_POST(self):
        self.answer(self.post)

    def answer(self, fn):
        """Run one request: a PublishError becomes its own answer, anything else 500 server_error."""
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                        # the phone went away
        except publish.PublishError as e:
            self.send(e.status, {'error': e.code, 'message': e.message, **e.extra})
        except Exception as e:
            print(publish.redact(traceback.format_exc()), flush=True)
            try:
                self.error(500, 'server_error', _short(e))
            except OSError:
                pass

    def get(self):
        email = self.who()
        if not email:
            return
        path, _, qs = self.path.partition('?')
        if path in GONE:
            return self.send(410, {'error': GONE_TEXT, 'message': GONE_TEXT})
        if path in STATIC or path == '/logo.png':
            return self.static(PAGE / STATIC[path] if path in STATIC else LOGO)
        m = COVER.fullmatch(path)
        if m:
            return self.cover(m.group(1), m.group(2), self.query(qs))
        if path not in self.GETS:
            return self.error(404, 'not_found', 'Not found.')
        self.send(200, getattr(self, self.GETS[path])(email, self.query(qs)))

    def post(self):
        raw, problem = self.raw_body()                  # read first, so a refusal never resets the connection
        email = self.who()
        if not email:
            return
        path = self.path.partition('?')[0]
        try:
            status, body = self.post_answer(email, path, raw, problem)
        except publish.PublishError as e:
            status, body = e.status, {'error': e.code, 'message': e.message, **e.extra}
        except Exception as e:                          # still logged; answer() sends the 500
            log(email, f'POST {path} 500 server_error ({_short(e)})')
            raise
        if status == 429 and isinstance(body.get('retry_after'), int):
            body = {**body, 'message': wait_text(body.get('message') or '', body['retry_after'])}
        log(email, f'POST {path} {status}' + (f' {body.get("error")}' if status >= 400 else ''))
        self.send(status, body)

    def post_answer(self, email, path, raw, problem):
        """(status, body) of one POST after the spec 8.1 guards (403 refused, 429, 413, 400). Settings dry runs
        (the sheet's live forecast; they never write) have their own, larger budget."""
        if not self.same_origin():
            return 403, {'error': 'refused', 'message': 'Refused.'}
        body = _json_object(raw) if problem is None else None
        dry = path == '/api/settings' and isinstance(body, dict) and body.get('dry_run') is True
        lim = self.dry_limiter if dry else self.limiter
        if not lim.allow(email):
            return 429, {'error': 'rate_limited', 'message': 'Wait a moment before trying again.',
                         'retry_after': lim.retry_after(email)}
        if problem == 'large':
            return 413, {'error': 'too_large', 'message': 'The request is too large.'}
        if body is None:
            raise _bad()
        if path in GONE:
            return 410, {'error': GONE_TEXT, 'message': GONE_TEXT}
        if path not in self.POSTS:
            return 404, {'error': 'not_found', 'message': 'Not found.'}
        return getattr(self, self.POSTS[path])(email, body)

    def raw_body(self) -> tuple[bytes | None, str | None]:
        """(body, None) when Content-Length is valid and at most BODY_MAX; (None, 'large' or 'bad') otherwise.
        A larger body is never read."""
        sizes = {v.strip() for v in self.headers.get_all('Content-Length') or []}
        if self.headers.get('Transfer-Encoding') or len(sizes) > 1:
            return None, 'bad'
        if not sizes:
            return b'', None
        n = sizes.pop()
        if not re.fullmatch(r'\d+', n):
            return None, 'bad'
        if len(n.lstrip('0')) > 9 or int(n) > BODY_MAX:  # a long number is too large (and never parsed)
            return None, 'large'
        return self.rfile.read(int(n)), None

    def same_origin(self) -> bool:
        """The spec 8.1 POST guards: only the page's own fetch() sends all of these (forms and other sites cannot)."""
        h = self.headers
        origin, host, site = h.get('Origin'), h.get('Host', ''), h.get('Sec-Fetch-Site')
        ctype = (h.get('Content-Type') or '').split(';')[0].strip().lower()
        return (h.get('X-Dashboard') == '1' and ctype == 'application/json'
                and (origin is None or (bool(host) and origin in (f'https://{host}', f'http://{host}')))
                and (site is None or site == 'same-origin'))

    def query(self, qs: str) -> dict:
        try:
            return {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True, max_num_fields=20).items()}
        except ValueError:
            raise _bad() from None

    def drive(self) -> Path:
        """The channel folder, or 503 drive_missing."""
        root = current_root()
        if root is None:
            raise publish.PublishError(503, 'drive_missing', 'Google Drive is not available on the PC right now.')
        return root

    def platform(self, d: dict) -> str:
        """d['platform'] when it names a platform: 400 bad_request, 404 unknown_platform otherwise."""
        name = _param(d.get('platform'), 'platform')
        publish.platform(name)
        return name

    def static(self, f: Path):
        try:
            data = f.read_bytes()
        except OSError:
            return self.error(404, 'not_found', 'Not found.')
        self.send(200, data, TYPES.get(f.suffix, 'application/octet-stream'))

    def cover(self, name, item_id, q):
        download = q.get('download') == '1'
        data, fname = cover_image(self.drive(), name, item_id, download)
        etag = f'"{hashlib.sha1(data).hexdigest()[:24]}"'
        headers = {'Cache-Control': 'private, max-age=3600', 'ETag': etag}
        if download:
            headers['Content-Disposition'] = f'attachment; filename="{fname}"'
        elif etag in [t.strip() for t in self.headers.get('If-None-Match', '').split(',')]:
            return self.send(304, b'', 'image/jpeg', headers)
        self.send(200, data, 'image/jpeg', headers)

    # ----- GET /api/... (each returns the 200 body) -----

    def get_home(self, email, q):
        return home(current_root(), email)

    def get_publish(self, email, q):
        name = self.platform(q)
        return publish.state(self.drive(), name)

    def get_preview(self, email, q):
        name, item = self.platform(q), _param(q.get('id'), 'id')
        return publish.preview(self.drive(), name, item)

    def get_job(self, email, q):
        return {'job': publish.job(_param(q.get('id'), 'job'))}

    def get_videos(self, email, q):
        root = self.drive()
        try:
            return videos_view(root)
        except (OSError, ValueError) as e:               # a record held by a sync or damaged, or the Drive went away (G2)
            if _drive_gone(root):
                raise publish.PublishError(503, 'drive_missing', 'Google Drive is not available on the PC right now.') from None
            raise publish.PublishError(503, 'lessons_error', f'The lessons could not be read: {_short(e)}') from None

    def get_stats(self, email, q):
        try:
            return stats.summary(current_root())
        except Exception as e:                           # spec 8.3: no errors; the reason shows on the sources line
            _say('numbers not available', e)
            return {'now': S.iso(_utcnow()), 'tiles': [], 'youtube_videos': [], 'reels': [], 'pairs': [],
                    'insights': None, 'limit': None,
                    'sources': [{'source': s, 'last_ok': None, 'last_try': None, 'next_try': None, 'fails': 1,
                                 'error': _short(e)} for s in stats.EVERY]}

    def get_activity(self, email, q):
        kind, before = q.get('kind') or 'all', q.get('before') or None
        if before is not None and not re.fullmatch(r'\d{1,12}', before):
            raise _bad('before')
        body = publish.activity(kind, int(before) if before else None)
        if before is None:
            body['logs'] = {'watch': log_tail(WATCH_LOG, 100),
                            **{n: log_tail(paths.WORK / f'{n}.log', 50) for n in publish.PLATFORMS}}
        return body

    # ----- POST /api/... (each returns (status, body)) -----

    def post_post(self, email, b):
        name, item = self.platform(b), _param(b.get('id'), 'id')
        caption, save, override = b.get('caption'), b.get('save_caption', False), b.get('override', [])
        if not isinstance(caption, str) or not isinstance(save, bool) or not _strings(override, 10):
            raise _bad()
        check = _param(b.get('check'), 'check')
        return 202, publish.start_post(self.drive(), name, item, caption, save, check, override, email)

    def post_queue(self, email, b):
        name, op, item = self.platform(b), b.get('op'), b.get('id')
        if item is not None:
            _param(item, 'id')
        if not isinstance(op, str):
            raise publish.PublishError(422, 'bad_op', 'Unknown queue operation.')
        return 200, publish.queue_op(self.drive(), name, op, b.get('rev'), email, item_id=item,
                                     caption=b.get('caption'), permalink=b.get('permalink'), at=b.get('at'))

    def post_settings(self, email, b):
        name, patch, ack, dry = self.platform(b), b.get('settings'), b.get('ack', []), b.get('dry_run', False)
        if not _strings(ack) or not isinstance(dry, bool):
            raise _bad()
        if isinstance(patch, dict) and 'hard_cap_24h' in patch:     # S12: read-only over HTTP, whatever the value
            raise publish.PublishError(422, 'read_only', 'The 24-hour limit can only be changed on the PC.',
                                       fields={'hard_cap_24h': S.READ_ONLY})
        return 200, publish.save_settings(name, patch, b.get('rev'), ack, email, dry_run=dry, root=current_root())

    def post_reset(self, email, b):
        ack = b.get('ack', [])
        if not _strings(ack):
            raise _bad()
        return 200, publish.reset_settings(ack, email)

    def post_resume(self, email, b):
        name, ack = self.platform(b), b.get('ack', [])
        if not _strings(ack):
            raise _bad()
        return 200, publish.resume_auto(name, ack, email, root=current_root())

    def post_test(self, email, b):
        name = self.platform(b)
        label = publish.platform(name).label

        def test():
            done = publish.test_platform(name, email)
            _key_accepted()                             # also when it passes after the answer went out
            return done
        try:
            done = capped(test, email, f'{name} connection test')
        except publish.PublishError as e:
            if e.status == 429 and e.code == 'rate_limited':      # say why, and (in post()) for how long
                e.message = f'{label} can be tested once every {publish.TEST_EVERY // 60} minutes. {e.message}'
            raise
        if done is None:
            return 202, {'ok': True, 'pending': True, 'message': f'{label} has not answered yet. The PC keeps '
                         'waiting: the result shows on Home and in Activity in a few minutes.'}
        return 200, done

    def post_resolve(self, email, b):
        job, outcome, root = _param(b.get('job'), 'job'), b.get('outcome'), self.drive()
        done = capped(lambda: publish.resolve_job(root, job, outcome, email), email, f'check of {job}')
        if done is None:
            return 202, {'ok': True, 'pending': True, 'message': 'The platform has not answered yet. The PC keeps '
                         'checking: reload in a few minutes to see the result.'}
        return 200, done

    def post_retry(self, email, b):
        return 200, retry_lesson(self.drive(), b.get('lesson'), email)


# ---------- background ----------

def tick(reconcile: bool = True) -> dict:
    """One background round: stats.collect(root), then (when asked) publish.reconcile(root, name, api=True) for
    each set-up platform. Returns {'stats': collect's answer, 'reconcile': [platform names]}. Never raises."""
    done = {}
    try:
        root = current_root()
    except Exception:
        root = None
    try:
        res = stats.collect(root, youtube=Handler.youtube)
        done['stats'] = res
        bad = {k: v for k, v in (res or {}).items() if str(v).startswith('error')}
        if bad:
            print(f'dashboard: numbers not read: {publish.redact(json.dumps(bad, ensure_ascii=False))[:300]}', flush=True)
    except Exception as e:
        _say('numbers not read', e)
    if reconcile:
        for name, p in _registry().items():
            try:
                if p.set_up():
                    for line in publish.reconcile(root, name, api=True):
                        print(f'dashboard reconcile: {publish.redact(line)}', flush=True)
                    done.setdefault('reconcile', []).append(name)
            except Exception as e:
                _say(f'{name} reconcile failed', e)
    return done


def background(stop: threading.Event) -> None:
    """The dashboard's one background thread: every 300 s stats.collect(drive_root()); every 600 s
    publish.reconcile(root, name, api=True) for each set-up platform. Never raises."""
    every, n = max(1, round(RECONCILE_EVERY / STATS_EVERY)), 0
    while not stop.is_set():
        try:
            tick(reconcile=n % every == 0)
        except Exception as e:                           # tick never raises; this keeps the thread alive anyway
            _say('background round failed', e)
        n += 1
        stop.wait(STATS_EVERY)


# ---------- start ----------

def _plain(path) -> str:
    """A path's ordinary spelling, without disk access: '/' as '\\', the \\\\?\\, \\\\.\\ and \\??\\ prefixes dropped
    (\\\\?\\UNC\\srv\\x -> \\\\srv\\x), and stream suffixes cut (D:\\x::$INDEX_ALLOCATION -> D:\\x)."""
    s = os.fspath(path).replace('/', '\\')
    if s[:8].upper() == '\\\\?\\UNC\\':
        s = '\\' + s[7:]
    elif s[:4] in ('\\\\?\\', '\\\\.\\', '\\??\\'):
        s = s[4:]
    head = s[:2] if re.match(r'[A-Za-z]:', s) else ''
    return head + re.sub(r':[^\\]*', '', s[len(head):])


def _odd(path) -> bool:
    """True for a namespace or network spelling (\\\\?\\..., \\\\.\\..., \\??\\..., \\\\server\\share): --dev refuses
    those outright, since the same folder can hide behind them."""
    s = os.fspath(path).replace('/', '\\')
    return s.startswith('\\\\') or s.startswith('\\??\\')


def _inside(path, folder) -> bool:
    """True when path is folder or inside it, as written (in its plain spelling) or once links are resolved
    (folder itself is never resolved: the real work folder is not touched). A path that cannot be resolved counts
    as inside."""
    want = os.path.normcase(os.path.abspath(_plain(folder))).rstrip('\\/')
    for full in (os.path.abspath, os.path.realpath):    # as written first: a refusal needs no disk access
        try:
            p = os.path.normcase(_plain(full(_plain(path))))
        except (OSError, ValueError):
            return True
        if p == want or p.startswith(want + os.sep):
            return True
    return False


def on_drive(path) -> bool:
    """True for a path under '<letter>:\\My Drive' (in its plain spelling, as written or once links are resolved)."""
    def spelled(full):
        try:
            return _plain(full(_plain(path)))
        except (OSError, ValueError):
            return 'X:\\My Drive'                       # cannot be resolved: treated as on the Drive
    return any(re.match(r'[A-Za-z]:\\My Drive(\\|$)', spelled(full), re.I) for full in (os.path.abspath, os.path.realpath))


def dev_refusal(port: int, root) -> str | None:
    """Why --dev must not start, or None: the live port, a namespace or network spelling of PIPELINE_WORK or the
    root, the real work folder (in PIPELINE_WORK or any module's folder), or a root on the real Drive."""
    if port == 8790:
        return 'port 8790 is the live dashboard; use another one (--port 8791).'
    works = [os.environ.get('PIPELINE_WORK') or REAL_WORK, paths.WORK, getattr(sys.modules.get('easy'), 'WORK_ROOT', None), publish.WORK, S.WORK,
             getattr(stats, 'WORK', None), getattr(IG, 'WORK', None)]
    if any(_odd(w) for w in [*works, root] if w is not None):
        return 'PIPELINE_WORK and --root must be plain drive paths (D:\\...), not \\\\?\\, \\\\.\\ or network paths.'
    if any(w is not None and _inside(w, REAL_WORK) for w in works):
        return f'PIPELINE_WORK is (or resolves to) the real work folder {REAL_WORK}: set it to a scratch folder.'
    if root is not None and on_drive(root):
        return f'{root} is on Google Drive: use --fixtures or a scratch copy.'
    return None


def _dev_setup(root, fixtures: bool) -> None:
    """--dev: the test guard first (no network, no cloudflared or schtasks, the fixtures' probe), then the fake
    platform and fake YouTube, and the root (--fixtures: a fresh fixture root in --root or the guard's temp folder,
    with the scratch folder's publishing state started afresh)."""
    import tests                                         # tests/__init__.py installs tests.guard
    from tests import fixtures as F, guard
    import platform_fake
    if fixtures:
        for name in DEV_STATE:
            _drop(guard.WORK / name)
        root = F.make_root(Path(root) if root is not None else guard.TMP / 'dev-fixtures')
    elif root is not None:
        root = Path(os.path.abspath(root))
        if not root.is_dir():
            sys.exit(f'--root {root} is not a folder.')
    fake = (F.fake_platform(root) if root is not None else
            platform_fake.FakePlatform([], records=guard.WORK / 'fake-records.json'))
    reg = {fake.name: fake}
    if root is not None:
        try:
            reg['youtube'] = F.fake_youtube_platform(root)
        except Exception as e:                       # the dev page still works with Instagram alone
            _say('fake YouTube not made', e)
    publish.set_registry(reg)
    Handler.access, Handler.dev, Handler.root = None, True, root
    Handler.youtube = F.fake_youtube_api(F.YT_VIDEOS, F.YT_CHANNEL)


class _Server(ThreadingHTTPServer):
    """No SO_REUSEADDR on Windows: there it lets a second server bind a port another one listens on (the old one
    keeps the traffic). A second start then fails with WinError 10048 and dashboard.cmd retries."""
    allow_reuse_address = os.name != 'nt'


def make_server(port: int = 8790, dev: bool = False, root: Path | None = None,
                fixtures: bool = False) -> ThreadingHTTPServer:
    """Everything serve() does before serving: the --dev checks and setup, or the login check; then
    publish.settings(root) and stats.import_old_cache(); then the server on 127.0.0.1 (port 0: any free port)."""
    if not dev and (root is not None or fixtures):
        sys.exit('--root and --fixtures need --dev.')
    if dev:
        why = dev_refusal(port, root)
        if why:
            sys.exit(f'dashboard --dev refuses to start: {why}')
        _dev_setup(root, fixtures)
    else:
        cfg = jload(CONFIG)
        cfg = cfg if isinstance(cfg, dict) else {}
        missing = [k for k in ('team', 'aud', 'emails') if not cfg.get(k)]
        if missing:
            sys.exit(f'{CONFIG} is missing {", ".join(missing)}: refusing to start without the login check.')
        Handler.access, Handler.dev, Handler.root, Handler.youtube = Access(cfg), False, None, None
        Handler.access.fetch()                          # fail now if the team name is wrong
    Handler.limiter, Handler.dry_limiter = Limiter(), Limiter(DRY_RUNS, 60)
    root = current_root()
    for what, step in (('settings', lambda: publish.settings(root)), ('old numbers', stats.import_old_cache)):
        try:
            step()
        except Exception as e:
            _say(f'{what} not loaded', e)
    return _Server(('127.0.0.1', port), Handler)


def serve(port: int = 8790, dev: bool = False, root: Path | None = None, fixtures: bool = False) -> None:
    """Start the server on 127.0.0.1 after publish.settings(drive_root()) and stats.import_old_cache().
    --dev refuses port 8790, refuses a PIPELINE_WORK that resolves to D:\\YT\\easy-work, refuses a root under
    '<letter>:\\My Drive', and uses tests.fixtures (fake platform, fake YouTube, a fixture root when --fixtures)."""
    httpd = make_server(port, dev, root, fixtures)
    stop = threading.Event()
    threading.Thread(target=background, args=(stop,), name='dashboard-background', daemon=True).start()
    port = httpd.server_address[1]
    if dev:
        print(f'DEV mode: no login check, fake platform, root {Handler.root or "none"}. http://127.0.0.1:{port}/',
              flush=True)
    else:
        print(f'dashboard on 127.0.0.1:{port} for {len(Handler.access.emails)} allowed account(s)', flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description='The private channel dashboard.')
    ap.add_argument('cmd', choices=['serve', 'json'])
    ap.add_argument('path', nargs='?', help='json: the Drive root (default: the one Google Drive has)')
    ap.add_argument('--port', type=int, default=8790)
    ap.add_argument('--dev', action='store_true', help='local test: no login check, the test guard, fake platform')
    ap.add_argument('--root', help='--dev: a scratch channel folder')
    ap.add_argument('--fixtures', action='store_true', help='--dev: a fresh fixture channel folder (in --root if given)')
    a = ap.parse_args(argv)
    if a.cmd == 'serve':
        if a.path:
            ap.error('serve takes no root: use --dev --root PATH')
        serve(a.port, a.dev, Path(a.root) if a.root else None, a.fixtures)
    else:
        root = Path(a.path) if a.path else drive_root()
        print(json.dumps({'home': home(root, 'cli'), 'videos': videos_view(root) if root else None},
                         ensure_ascii=False, indent=1, default=str))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
