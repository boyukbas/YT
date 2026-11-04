"""The private channel dashboard: https://yt.boyukbas.com (behind Cloudflare Access: Google login, allowed emails only).

Every video of every lesson, newest first, with its full lesson name, state, date/time, YouTube title/link and counts,
the Instagram Reel; plus three buttons: post the next Reel now, pause/resume the daily Reels, retry a stopped lesson.

How it is reached: this server listens on 127.0.0.1 only; cloudflared (a named tunnel, started next to it by
dashboard.cmd) carries https://yt.boyukbas.com to it. Cloudflare Access sits in front: nobody reaches the tunnel without
signing in with an allowed Google account. This server checks that again on every request: Cloudflare's signed login
token (Cf-Access-Jwt-Assertion, RS256) must verify against the team's public keys, be for this app (aud) and name an
allowed email. Settings (not in the repo, the repo is public): D:\\YT\\easy-work\\dashboard.json
  {"team": "<team>.cloudflareaccess.com", "aud": "<Application Audience tag>", "emails": ["a@x.com", "b@y.com"]}
Every button press is logged with the email in D:\\YT\\easy-work\\dashboard.log.

  python dashboard.py serve [--port 8790]            what dashboard.cmd runs
  python dashboard.py serve --dev --port 8791        local test without Cloudflare (no login check; 127.0.0.1 only)
  python dashboard.py json "<Drive root>"            print the data
"""
import argparse, base64, json, os, re, subprocess, sys, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))          # python -I (as the scheduled task runs it) doesn't add it
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import easy
import instagram as IG
from tools import extras, probe

HERE = Path(__file__).parent
PAGE = HERE / 'dashboard'                               # dashboard.html + dashboard.js
CONFIG = easy.WORK_ROOT / 'dashboard.json'
LOG = easy.WORK_ROOT / 'dashboard.log'
STATS = easy.WORK_ROOT / 'dashboard-stats.json'         # cached YouTube counts + titles
PROBES = easy.WORK_ROOT / 'dashboard-probes.json'       # cached video lengths (probing Drive files is slow)
STATS_EVERY = 3600                                      # seconds between YouTube refreshes (~2 of 10,000 daily units)
WATCHER = 'YT Easy mode watcher'


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
        videos = easy.lesson_videos(lesson)
        out = easy.output_dir(lesson)
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
                it['retry'] = easy.retry_marker(lesson).exists()             # pressed, waiting for the watcher
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


def refresh_stats(found, stats):
    """Views/likes/comments, title, privacy and publish time of every uploaded video; the channel's subscribers.
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
        print(f'dashboard: YouTube counts not refreshed: {type(e).__name__}: {str(e)[:200]}', flush=True)
    return stats


def instagram_info(root):
    last = IG.last_post(root)
    nxt = IG.next_reel(root)
    gap_end = last + IG.GAP if last else None
    return {'paused': IG.PAUSED.exists(), 'posting': IG.POSTING.exists(), 'set_up': IG.TOKEN.exists(),
            'last': last.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if last else None,
            'allowed_from': gap_end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if gap_end else None,
            'daily_time': IG.POST_TIME,
            'next': {'lesson': nxt[0].name, 'file': nxt[1].name} if nxt else None}


_build_lock = threading.Lock()


def build(root):
    with _build_lock:                                   # one build at a time (two phones refreshing at once)
        cache = jload(PROBES)
        found = items(root, cache)
        PROBES.write_text(json.dumps(cache), encoding='utf-8')
        stats = refresh_stats(found, jload(STATS))
        STATS.write_text(json.dumps(stats, ensure_ascii=False), encoding='utf-8')
    for it in found:
        s = stats.get(it.get('youtube', ''))
        if s:
            it.update(title=s['title'], privacy=s['privacy'], views=s['views'], likes=s['likes'], comments=s['comments'])
            if it['state'] == 'public' and s['privacy'] == 'public':
                it['time'] = iso(s['published'])                          # YouTube's own publish time
    found.sort(key=lambda i: i.get('time') or '', reverse=True)
    return {'channel': {'name': 'Almanca Dil Atölyesi', 'url': IG.CHANNEL, **stats.get('_channel', {})},
            'videos': found, 'instagram': instagram_info(root), 'jobs': JOBS,
            'counts_at': datetime.fromtimestamp(stats['_at'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            if stats.get('_at') else None,
            'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}


# ---------- login check (Cloudflare Access) ----------

def b64(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


class Access:
    """Verifies Cloudflare Access's login token. Keys: https://<team>/cdn-cgi/access/certs (cached, refetched for an
    unknown key id, at most once a minute)."""

    def __init__(self, cfg):
        self.team, self.aud = cfg['team'], cfg['aud']
        self.emails = {e.strip().lower() for e in cfg['emails']}
        self.keys, self.fetched = {}, 0

    def fetch(self):
        if time.time() - self.fetched < 60:
            return
        self.fetched = time.time()
        r = requests.get(f'https://{self.team}/cdn-cgi/access/certs', timeout=10)
        r.raise_for_status()
        self.keys = {k['kid']: rsa.RSAPublicNumbers(int.from_bytes(b64(k['e']), 'big'),
                                                    int.from_bytes(b64(k['n']), 'big')).public_key()
                     for k in r.json()['keys'] if k.get('kty') == 'RSA'}

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


# ---------- actions ----------

JOBS = {}                                               # 'reel': {'running', 'result', 'by', 'at'}


def log(email, what):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {email} {what}"
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def act(action, body, email, root):
    """Runs one button press. Returns (http status, message)."""
    if action == 'reel-now':
        if JOBS.get('reel', {}).get('running') or IG.POSTING.exists():
            return 409, 'A Reel is being posted right now.'
        force = body.get('force') is True
        warn = IG.too_soon(root)
        if warn and not force:                           # the page asks "post anyway?" and sends force: true
            return 428, warn
        JOBS['reel'] = {'running': True, 'by': email, 'at': now_iso(), 'result': 'Posting…'}

        def run():
            try:
                r = IG.post_now(root, force=force, log=lambda m: print(m, flush=True))
            except IG.Busy:
                r = 'A Reel is being posted right now (by the watcher).'
            except Exception as e:
                r = f'Failed: {type(e).__name__}: {str(e)[:300]}'
            JOBS['reel'] = {'running': False, 'by': email, 'at': now_iso(), 'result': r}
            log(email, f'reel-now -> {r}')
            with open(easy.WORK_ROOT / 'instagram.log', 'a', encoding='utf-8') as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M')} dashboard ({email}): {r}\n")
        threading.Thread(target=run, daemon=True).start()
        log(email, 'reel-now started' + (' (early: confirmed despite the daily rule)' if warn else ''))
        return 202, 'Posting the next Reel… (takes 1–3 minutes)'
    if action in ('pause', 'resume'):
        if action == 'pause':
            IG.PAUSED.write_text(f'{email} {time.strftime("%Y-%m-%d %H:%M")}\n', encoding='utf-8')
        else:
            IG.PAUSED.unlink(missing_ok=True)
        log(email, action)
        return 200, 'Daily Reels paused.' if action == 'pause' else f'Daily Reels on again (after {IG.POST_TIME}).'
    if action == 'retry':
        name = str(body.get('lesson', ''))
        lesson = Path(root) / name
        if not name or '/' in name or '\\' in name or name.startswith(('.', '_')) or not lesson.is_dir():
            return 400, 'No such lesson.'
        st = jload(extras(easy.output_dir(lesson)) / 'status.json')
        if st.get('state') != 'failed':
            return 409, 'That lesson is not stopped.'
        m = easy.retry_marker(lesson)
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(f'{email} {time.strftime("%Y-%m-%d %H:%M")}\n', encoding='utf-8')
        r = subprocess.run(['schtasks', '/run', '/tn', WATCHER], capture_output=True, text=True, timeout=30)
        log(email, f'retry {name} (watcher started: {r.returncode == 0})')
        return 200, ('Retrying: the PC starts on it now.' if r.returncode == 0 else
                     'Retry is queued: the PC starts on it within 10 minutes.')
    return 400, 'Unknown action.'


# ---------- server ----------

class Handler(BaseHTTPRequestHandler):
    server_version = 'dashboard'
    sys_version = ''
    access = None                                       # Access(...) or None in --dev mode

    def log_message(self, fmt, *args):                  # requests aren't logged (only actions are)
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else (
            json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; img-src 'self' https://i.ytimg.com; style-src 'self' 'unsafe-inline' "
                         "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def who(self):
        """The signed-in email, or None after answering 401/403."""
        if self.access is None:
            return 'dev@localhost'
        token = self.headers.get('Cf-Access-Jwt-Assertion', '')
        if not token:
            self.send(401, {'error': 'Not signed in.'})
            return None
        try:
            return self.access.email(token)
        except PermissionError as e:
            self.send(403, {'error': f'Not allowed: {e}.'})
            return None
        except Exception as e:                          # Cloudflare's key page unreachable: refuse, never allow
            self.send(503, {'error': f'Login check unavailable: {type(e).__name__}.'})
            return None

    def do_GET(self):
        email = self.who()
        if not email:
            return
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            return self.send(200, (PAGE / 'dashboard.html').read_bytes(), 'text/html; charset=utf-8')
        if path == '/dashboard.js':
            return self.send(200, (PAGE / 'dashboard.js').read_bytes(), 'text/javascript; charset=utf-8')
        if path == '/logo.png':
            return self.send(200, (HERE.parent / 'docs' / 'brand' / 'logo-trde.png').read_bytes(), 'image/png')
        if path == '/api/status':
            root = drive_root()
            if not root:
                return self.send(503, {'error': 'Google Drive is not available on the PC right now.'})
            try:
                return self.send(200, {**build(root), 'me': email})
            except Exception as e:
                return self.send(500, {'error': f'{type(e).__name__}: {str(e)[:200]}'})
        self.send(404, {'error': 'Not found.'})

    def do_POST(self):
        email = self.who()
        if not email:
            return
        # Only the page's own fetch() can send this: a custom header + JSON body (forms on other sites can't),
        # and if the browser says where it comes from, it must be this site.
        origin, host = self.headers.get('Origin'), self.headers.get('Host', '')
        if (self.headers.get('X-Dashboard') != '1' or 'application/json' not in self.headers.get('Content-Type', '')
                or (origin and origin not in (f'https://{host}', f'http://{host}'))):
            return self.send(403, {'error': 'Refused.'})
        if self.path.split('?')[0] != '/api/action':
            return self.send(404, {'error': 'Not found.'})
        try:
            n = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(min(n, 10_000)) or b'{}')
        except ValueError:
            return self.send(400, {'error': 'Bad request.'})
        root = drive_root()
        if not root:
            return self.send(503, {'error': 'Google Drive is not available on the PC right now.'})
        try:
            code, msg = act(str(body.get('action', '')), body, email, root)
        except Exception as e:
            code, msg = 500, f'{type(e).__name__}: {str(e)[:200]}'
        self.send(code, {'message': msg} if code < 400 else
                  {'confirm': msg} if code == 428 else {'error': msg})      # 428: needs "post anyway?"


def serve(port, dev=False):
    if dev:
        Handler.access = None
        print(f'DEV mode: no login check. http://127.0.0.1:{port}/', flush=True)
    else:
        cfg = jload(CONFIG)
        missing = [k for k in ('team', 'aud', 'emails') if not cfg.get(k)]
        if missing:
            sys.exit(f'{CONFIG} is missing {", ".join(missing)}: refusing to start without the login check.')
        Handler.access = Access(cfg)
        Handler.access.fetch()                          # fail now if the team name is wrong
        print(f'dashboard on 127.0.0.1:{port} for {len(Handler.access.emails)} allowed account(s)', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['serve', 'json'])
    ap.add_argument('root', nargs='?')
    ap.add_argument('--port', type=int, default=8790)
    ap.add_argument('--dev', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'serve':
        serve(a.port, a.dev)
    else:
        print(json.dumps(build(a.root or drive_root()), ensure_ascii=False, indent=1))
