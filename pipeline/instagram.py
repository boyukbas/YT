"""Instagram: the lesson's Shorts as Reels, with their own cover and caption, posted one a day (Instagram API with
Instagram Login; see instagram-setup.md for the one-time setup).

  python instagram.py token                         paste the access token once (Meta app dashboard → Generate token)
  python instagram.py check                         read-only: account, posts left in the 24 h limit
  python instagram.py prepare "<lesson>"            covers (cover-short-N.jpg) + captions (instagram.md); posts nothing
  python instagram.py post "<lesson>" [--dry-run]   post the lesson's next Reel now (refuses if one went out < 20 h ago
                                                    unless --force: one Reel a day, so followers aren't flooded)
  python instagram.py daily "<root>"                the watcher's step: after POST_TIME, post the next Reel of the
                                                    oldest lesson that has some left, if none went out in the last 20 h

Meta downloads the video from a public URL (with Instagram Login there is no direct upload), so while one Reel is being
posted, the PC serves just that video + its cover under a random path through a temporary Cloudflare quick tunnel
(cloudflared, no account), and closes it as soon as Instagram has the file.
Token: %USERPROFILE%\\.shorts-pipeline\\instagram.json (long-lived, 60 days; refreshed here every week).
Record: <lesson>/Output/instagram-posted.json."""
import argparse, getpass, http.server, json, re, secrets, shutil, subprocess, sys, tempfile, threading, time
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from tools import probe

KEYS = Path.home() / '.shorts-pipeline'
TOKEN = KEYS / 'instagram.json'
API = 'https://graph.instagram.com/v26.0'
CLOUDFLARED = Path(r'D:\YT\tools\cloudflared.exe')
ISTANBUL = timezone(timedelta(hours=3))
POSTED = 'instagram-posted.json'
POST_TIME = '20:00'                                      # Istanbul; the daily Reel goes out after this time
GAP = timedelta(hours=20)                                # at most one Reel per day
MAX_TAGS = 5                                             # Instagram shows few hashtags; more looks like spam
SAVE_LINE = '💾 Kaydet, sonra tekrar et!'
YOUTUBE_LINE = "▶️ Dersin tamamı YouTube'da: Almanca Dil Atölyesi (link profilde)"


# ---------- token + API ----------

def token():
    if not TOKEN.exists():
        raise RuntimeError('Instagram token missing: run  python instagram.py token')
    t = json.loads(TOKEN.read_text(encoding='utf-8'))
    if datetime.now(timezone.utc) - datetime.fromisoformat(t['refreshed']) > timedelta(days=7):
        r = requests.get('https://graph.instagram.com/refresh_access_token',
                         params={'grant_type': 'ig_refresh_token', 'access_token': t['token']}, timeout=30)
        if r.ok and 'access_token' in r.json():
            t.update(token=r.json()['access_token'], refreshed=datetime.now(timezone.utc).isoformat())
            TOKEN.write_text(json.dumps(t), encoding='utf-8')
        elif datetime.now(timezone.utc) - datetime.fromisoformat(t['refreshed']) > timedelta(days=55):
            raise RuntimeError('Instagram token expired or about to: make a new one (instagram-setup.md, step 5)')
    return t['token']


def call(method, path, **params):
    """One Graph API call. The token goes in a header, so it never shows up in an error message or log."""
    try:
        r = requests.request(method, f'{API}/{path}', params=params, timeout=120,
                             headers={'Authorization': f'Bearer {token()}'})
    except requests.RequestException as e:
        raise RuntimeError(f'Instagram unreachable: {type(e).__name__}') from None
    j = r.json() if r.headers.get('content-type', '').startswith(('application/json', 'text/javascript')) else {}
    if not r.ok or 'error' in j:
        err = j.get('error', {})
        raise RuntimeError(f"Instagram {r.status_code}: {err.get('message', r.text[:200])} "
                           f"(code {err.get('code')}, {err.get('error_user_msg', '')})")
    return j


def save_token():
    t = getpass.getpass('Paste the Instagram access token (it is not shown): ').strip()
    if not t:
        raise SystemExit('No token given.')
    KEYS.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({'token': t, 'refreshed': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
    check()


def check():
    me = call('GET', 'me', fields='user_id,username,account_type,media_count')
    print(f"Instagram: @{me.get('username')} ({me.get('account_type')}), {me.get('media_count', 0)} posts, id {me.get('user_id')}")
    t = json.loads(TOKEN.read_text(encoding='utf-8'))
    if t.get('user_id') != me.get('user_id'):
        TOKEN.write_text(json.dumps({**t, 'user_id': me.get('user_id'), 'username': me.get('username')}), encoding='utf-8')
    try:
        lim = call('GET', f"{me['user_id']}/content_publishing_limit", fields='quota_usage,config')['data'][0]
        print(f"Published by the API in the last 24 h: {lim.get('quota_usage')} of {lim.get('config', {}).get('quota_total')}")
    except (RuntimeError, KeyError, IndexError) as e:
        print(f'Publishing limit not readable: {e}')
    return me


# ---------- covers + captions ----------

def lesson_files(lesson):
    lesson = Path(lesson)
    out = next(o for o in (lesson / 'Output (Easy)', lesson / 'Output') if (o / 'youtube.json').exists())
    return lesson, out, json.loads((out / 'youtube.json').read_text(encoding='utf-8'))


def reels(out, data):
    """[(mp4, short entry)] in Short order."""
    num = lambda f: int(re.search(r'-short-(\d+)$', f.stem).group(1))
    by_version = {s['version']: s for s in data.get('shorts', [])}
    return [(f, by_version.get(f'short-{num(f)}', {})) for f in sorted(out.glob('*-short-*.mp4'), key=num)]


def hashtags(root):
    ch = Path(root) / '_channel' / 'channel.json'
    p = json.loads(ch.read_text(encoding='utf-8')) if ch.exists() else {}
    return p.get('instagram', {}).get('hashtags') or p.get('youtube', {}).get('hashtags') or ['#almanca']


def caption(sh, base_tags):
    """The YouTube Short's texts, made for Instagram: hook first (only ~1 line shows before "more"), the lesson in
    2-3 lines, a save prompt (saves count a lot on Instagram), the YouTube pointer (links in captions aren't
    clickable), at most MAX_TAGS hashtags."""
    title = re.sub(r'\s*#\w+', '', sh.get('title', '')).strip()
    body = []
    for line in sh.get('description', '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        body.append(YOUTUBE_LINE if line.startswith('Dersin tamamı') else line)
    if YOUTUBE_LINE not in body:
        body.append(YOUTUBE_LINE)
    own = [t for t in re.findall(r'#\w+', sh.get('description', '')) if t.lower() != '#shorts']
    tags = list(dict.fromkeys(t.lower() for t in base_tags + own))[:MAX_TAGS]
    return '\n\n'.join([title, '\n'.join(body), SAVE_LINE, ' '.join(tags)])[:2200]


def cover_path(mp4):
    return mp4.with_name('cover-' + re.search(r'short-\d+$', mp4.stem).group(0) + '.jpg')      # cover-short-N.jpg


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
         'Posted automatically, one Reel a day after ' + POST_TIME + ' (Istanbul). To post one by hand instead: '
         'Instagram → + → Reel → pick the video → Edit cover → Add from camera roll → the cover → paste the caption.', '']
    for mp4, sh in reels(out, data):
        p = posted.get(mp4.name)
        state = f"posted {p['posted']}: {p.get('permalink', '')}" if p else 'not posted yet'
        L += [f'## Short {mp4.stem.rsplit("-", 1)[1]} ({state})',
              f'Video `{mp4.name}` · cover `{cover_path(mp4).name}`', '', '```', caption(sh, tags), '```', '']
    (out / 'instagram.md').write_text('\n'.join(L), encoding='utf-8')


def prepare(lesson, log=print):
    """Covers + captions for a finished lesson (easy.py does this right after the YouTube texts)."""
    import easy
    lesson, out, data = lesson_files(lesson)
    profile, assets = easy.load_profile(lesson.parent)
    thumbs = easy.WORK_ROOT / easy.slug(lesson.name) / 'thumbs'
    saved = thumbs / 'candidates.json'
    frames = {c['n']: thumbs / c['file'] for c in json.loads(saved.read_text(encoding='utf-8'))} if saved.exists() else {}
    if frames:
        make_covers(out, data, frames, assets['logo'], profile['palette'], log)
    write_md(lesson, out, data)


# ---------- posting ----------

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class Share:
    """Serve a few files at https://<random>.trycloudflare.com/<random>/<name> while the block runs."""

    def __init__(self, files):
        self.files = [Path(f) for f in files]

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ig-'))
        self.key = secrets.token_urlsafe(16)
        (self.tmp / self.key).mkdir()
        for f in self.files:
            shutil.copy2(f, self.tmp / self.key / f.name)
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(self.tmp)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        port = self.httpd.server_address[1]
        self.proc = subprocess.Popen([str(CLOUDFLARED), 'tunnel', '--url', f'http://127.0.0.1:{port}', '--no-autoupdate'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors='replace')
        found = []

        def read():
            for line in self.proc.stderr:
                m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if m and not found:
                    found.append(m.group(0))
        threading.Thread(target=read, daemon=True).start()
        end = time.time() + 90
        while not found and time.time() < end:
            time.sleep(0.5)
        if not found:
            self.__exit__()
            raise RuntimeError('cloudflared gave no public address')
        self.urls = [f'{found[0]}/{self.key}/{f.name}' for f in self.files]
        while time.time() < end:                     # the new address takes a few seconds to reach the internet
            try:
                if requests.head(self.urls[0], timeout=10).status_code == 200:
                    return self.urls
            except requests.RequestException:
                pass
            time.sleep(3)
        self.__exit__()
        raise RuntimeError('the temporary public address did not answer')

    def __exit__(self, *exc):
        if getattr(self, 'proc', None):
            self.proc.terminate()
        self.httpd.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)


def load(out):
    p = out / POSTED
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}


def last_post(root):
    times = [datetime.fromisoformat(r['posted_utc']) for p in Path(root).glob(f'*/*/{POSTED}')
             for r in json.loads(p.read_text(encoding='utf-8')).values()]
    return max(times, default=None)


def post_next(lesson, dry=False, force=False, log=print):
    """Post the lesson's next Reel. Returns a one-line summary, or None when it has none left."""
    lesson, out, data = lesson_files(lesson)
    posted = load(out)
    todo = [(m, sh) for m, sh in reels(out, data) if m.name not in posted]
    if not todo:
        return None
    last = last_post(lesson.parent)
    if last and datetime.now(timezone.utc) - last < GAP and not force:
        return (f'WARNING: a Reel went out {(datetime.now(timezone.utc) - last).seconds // 3600} h ago; one a day keeps '
                f'followers from being flooded. Use --force to post anyway.')
    mp4, sh = todo[0]
    cover = cover_path(mp4)
    if not cover.exists():
        prepare(lesson, log)
    cap = caption(sh, hashtags(lesson.parent))
    if dry:
        return f'would post {mp4.name} with {cover.name}:\n{cap}'
    length = probe(mp4)['duration']
    if not 3 <= length <= 900:
        raise RuntimeError(f'{mp4.name}: {length:.0f} s, Reels must be 3 s to 15 min')
    uid = json.loads(TOKEN.read_text(encoding='utf-8')).get('user_id') or check()['user_id']
    with Share([mp4, cover]) as (video_url, cover_url):
        log(f'{mp4.name}: sending to Instagram')
        c = call('POST', f'{uid}/media', media_type='REELS', video_url=video_url, cover_url=cover_url, caption=cap,
                 share_to_feed='true')
        for _ in range(60):                           # Instagram fetches + processes the video (usually < 1 min)
            time.sleep(10)
            s = call('GET', c['id'], fields='status_code,status')
            if s.get('status_code') == 'FINISHED':
                break
            if s.get('status_code') in ('ERROR', 'EXPIRED'):
                raise RuntimeError(f"Instagram could not process {mp4.name}: {s.get('status')}")
        else:
            raise RuntimeError(f'{mp4.name}: Instagram still processing after 10 min')
    m = call('POST', f'{uid}/media_publish', creation_id=c['id'])
    info = call('GET', m['id'], fields='permalink,timestamp')
    posted[mp4.name] = {'id': m['id'], 'permalink': info.get('permalink', ''),
                        'posted': datetime.now(ISTANBUL).strftime('%Y-%m-%d %H:%M'),
                        'posted_utc': datetime.now(timezone.utc).isoformat()}
    (out / POSTED).write_text(json.dumps(posted, ensure_ascii=False, indent=1), encoding='utf-8')
    write_md(lesson, out, data)
    left = len(todo) - 1
    return f"Instagram: {mp4.name} posted {info.get('permalink', '')} ({left} left in this lesson)"


def daily(root, log=print):
    """Watcher step: at most one Reel a day, after POST_TIME, oldest lesson first."""
    if not TOKEN.exists():
        return None
    now = datetime.now(ISTANBUL)
    hh, mm = map(int, POST_TIME.split(':'))
    last = last_post(root)
    if (now.hour, now.minute) < (hh, mm) or (last and datetime.now(timezone.utc) - last < GAP):
        return None
    lessons = sorted((p.parent.parent for p in Path(root).glob('*/*/youtube.json') if not p.parent.parent.name.startswith('_')),
                     key=lambda l: next(l.glob('*/youtube.json')).stat().st_mtime)
    for lesson in lessons:
        r = post_next(lesson, log=log)
        if r:
            log(r)
            return r
    return None


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['token', 'check', 'prepare', 'post', 'daily'])
    ap.add_argument('path', nargs='?')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='post even if a Reel went out less than 20 h ago')
    a = ap.parse_args()
    if a.cmd == 'token':
        save_token()
    elif a.cmd == 'check':
        check()
    elif a.cmd == 'prepare':
        prepare(a.path)
    elif a.cmd == 'post':
        print(post_next(a.path, a.dry_run, a.force) or 'Nothing left to post in this lesson.')
    else:
        print(daily(a.path) or 'Nothing to post now.')
