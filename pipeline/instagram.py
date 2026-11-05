"""Instagram: the lesson's Shorts as Reels, with their own cover and caption (see instagram-setup.md for the one-time
setup). What goes out when is decided by publish.py (the dashboard's Publish tab and its settings, one posting lock,
the safety rules); this module holds the key, the Graph API calls, the covers and the captions.

  python instagram.py system-token                  the key in use (2026-11-03): paste a system user's token from
                                                    Business settings; the Instagram account must be assigned to it
  python instagram.py fb-token                      Facebook-login mode: app ID + secret + a Graph API Explorer token →
                                                    a Page token that doesn't expire (the Page only links
                                                    the app to Instagram; nothing is posted there)
  python instagram.py unified-token                 Unified-login mode: app ID + secret +
                                                    the login configuration's ID → log in in the browser → a key
                                                    owned by the business portfolio
  python instagram.py token                         Instagram-login mode: paste the token from the app dashboard
  python instagram.py check                         read-only: account, posts left in the 24 h limit
  python instagram.py prepare "<lesson>"            covers (cover-short-N.jpg) + captions (instagram.md); posts nothing
  python instagram.py post "<lesson>" [--dry-run] [--force]
                                                    post the lesson's next waiting Reel now, with the dashboard's
                                                    rules and lock (publish.cli_post): a gap or same-day warning
                                                    refuses unless --force; nothing passes the 24 h hard cap
  python instagram.py daily "<root>"                the watcher's automatic step by hand (publish.auto_step): posts
                                                    only when automatic posting is on and its slot is open

Instagram downloads the video + cover from a public URL (its direct upload refuses Page tokens), so while one Reel is
being posted, the PC serves just those two files under a random path through a temporary Cloudflare quick tunnel
(cloudflared, no account), and closes it as soon as Instagram has them. Posting to the Facebook Page as well
(FACEBOOK_PAGE) is off since 2026-11-03: the Page got few views and was deleted; Instagram does well (the Facebook
functions stay, uncalled).
Token: %USERPROFILE%\\.shorts-pipeline\\instagram.json (Page / Unified-login key: no expiry; Instagram token: 60 days, refreshed
here every week). Graph API calls send it in the Authorization header, so it never shows up in an error message or log.
Record: <lesson>/Output/extras/instagram-posted.json (written by platform_instagram.py under the posting lock)."""
import argparse, getpass, http.server, json, os, re, secrets, shutil, subprocess, sys, tempfile, threading, time, webbrowser
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import paths
from tools import extras, probe, records

KEYS = paths.KEYS
TOKEN = KEYS / 'instagram.json'
API = 'https://graph.instagram.com/v26.0'
FB_API = 'https://graph.facebook.com/v26.0'
RUPLOAD = 'https://rupload.facebook.com'
FB_SCOPES = ['pages_show_list', 'pages_read_engagement', 'pages_manage_posts', 'instagram_basic',
             'instagram_content_publish', 'business_management']
CLOUDFLARED = Path(r'D:\YT\tools\cloudflared.exe')
ISTANBUL = paths.ISTANBUL
POSTED = 'instagram-posted.json'
POST_TIME = '20:00'                                      # v1's daily time and gap; v2's timing is in publishing.json
GAP = timedelta(hours=20)
FACEBOOK_PAGE = False                                    # also post Reels + long videos to the Page (off: deleted 2026-11-03)
MAX_TAGS = 5                                             # Instagram shows few hashtags; more looks like spam
SAVE_LINE = '💾 Kaydet, sonra tekrar et!'
YOUTUBE_LINE = "▶️ Dersin tamamı YouTube'da: Almanca Dil Atölyesi (link profilde)"
CHANNEL = 'https://www.youtube.com/@sinembb'
WORK = paths.WORK
PAUSED = WORK / 'instagram-paused'                       # exists = no automatic Reels (the emergency brake)
POSTING = WORK / 'instagram-posting.lock'                # publish.PostLock's file: one poster at a time
POLL = 10                                                # seconds between upload checks (wait_container)


class MetaError(RuntimeError):
    """A Graph API refusal, or Meta unreachable. str() keeps the old text and adds 'subcode N' when one is given:
    'Meta 400: <message> (code 100, subcode 2207085, <user message>)' or 'Meta unreachable: ConnectTimeout'."""
    def __init__(self, message: str, status: int | None = None, code: int | None = None,
                 subcode: int | None = None):
        super().__init__(message)
        self.status, self.code, self.subcode = status, code, subcode


class ContainerError(RuntimeError):
    """Instagram did not finish the upload. .status is 'ERROR', 'EXPIRED' or 'TIMEOUT' (still processing
    after 10 min)."""
    def __init__(self, message: str, status: str):
        super().__init__(message)
        self.status = status


class ShareError(RuntimeError):
    """The temporary public link (cloudflared quick tunnel) could not be made: a problem on the PC."""


def classify(exc: BaseException) -> str:
    """The error kind for the publish module's retry and stop rules, checked in this order:
    ContainerError: 'content' for ERROR, else 'transient'.
    ShareError: 'local'.
    MetaError without status and code (unreachable): 'transient'.
    code 190 or 102: 'auth'.
    subcode 2207085, code 10, code 368, or code 200-299: 'permission'.
    code 4, 17, 32, 613 or 80002, or HTTP 429: 'rate'.
    code 1 or 2, or HTTP 500 and above: 'transient'.
    Any other MetaError (including every other 2207xxx subcode, an assumption): 'unknown'.
    OSError: 'local'.
    Anything else: 'unknown'."""
    if isinstance(exc, ContainerError):
        return 'content' if exc.status == 'ERROR' else 'transient'
    if isinstance(exc, ShareError):
        return 'local'
    if isinstance(exc, MetaError):
        code, status = exc.code, exc.status
        if status is None and code is None:
            return 'transient'
        if code in (190, 102):
            return 'auth'
        if exc.subcode == 2207085 or code in (10, 368) or (code is not None and 200 <= code <= 299):
            return 'permission'
        if code in (4, 17, 32, 613, 80002) or status == 429:
            return 'rate'
        if code in (1, 2) or (status or 0) >= 500:
            return 'transient'
        return 'unknown'
    return 'local' if isinstance(exc, OSError) else 'unknown'


class Posting:
    """Deprecated wrapper around publish.PostLock(owner='cli') (lazy import); raises Busy when it is held."""
    def __enter__(self):
        import publish
        self.lock = publish.PostLock(owner='cli')
        try:
            self.lock.__enter__()
        except publish.Busy:
            raise Busy('a Reel is being posted right now') from None
        return self

    def __exit__(self, *exc):
        self.lock.__exit__(*exc)


class Busy(RuntimeError):
    """Posting: another poster (dashboard, watcher or CLI) holds the lock."""


# ---------- token + API ----------

def saved():
    if not TOKEN.exists():
        raise RuntimeError('Instagram token missing: run  python instagram.py fb-token  (or token)')
    return json.loads(TOKEN.read_text(encoding='utf-8'))


def fb_mode():
    """Keys from a Facebook or Unified login: they work on graph.facebook.com (graph.instagram.com refuses them)."""
    return TOKEN.exists() and saved().get('mode') in ('facebook', 'unified')


def token():
    t = saved()
    if t.get('mode') == 'facebook':
        return t['page_token']
    if t.get('mode') == 'unified':
        return t['token']
    if datetime.now(timezone.utc) - datetime.fromisoformat(t['refreshed']) > timedelta(days=7):
        r = requests.get('https://graph.instagram.com/refresh_access_token',
                         params={'grant_type': 'ig_refresh_token', 'access_token': t['token']}, timeout=30)
        if r.ok and 'access_token' in r.json():
            t.update(token=r.json()['access_token'], refreshed=datetime.now(timezone.utc).isoformat())
            TOKEN.write_text(json.dumps(t), encoding='utf-8')
        elif datetime.now(timezone.utc) - datetime.fromisoformat(t['refreshed']) > timedelta(days=55):
            raise RuntimeError('Instagram token expired or about to: make a new one (instagram-setup.md, step 5)')
    return t['token']


def call(method, path, files=None, timeout=600, **params):
    """One Graph API call. The token goes in a header, so it never shows up in an error message or log.
    timeout: requests' seconds (or (connect, read)); short ones for the reads a person waits for. A network
    failure (a timeout too) raises MetaError('Meta unreachable: <Type>')."""
    url = path if path.startswith('https://') else f"{FB_API if fb_mode() else API}/{path}"
    try:
        r = requests.request(method, url, timeout=timeout, headers={'Authorization': f'Bearer {token()}'},
                             **({'data': params, 'files': files} if files else {'params': params}))
    except requests.RequestException as e:
        raise MetaError(f'Meta unreachable: {type(e).__name__}') from None
    return answer(r)


def answer(r):
    """The JSON of a Graph API answer. Raises MetaError with the same text as before, plus the subcode."""
    try:
        j = r.json() if r.headers.get('content-type', '').startswith(('application/json', 'text/javascript')) else {}
    except ValueError:                                    # a broken body: judged by the HTTP status alone
        j = {}
    j = j if isinstance(j, dict) else {}
    if not r.ok or 'error' in j:
        err = j.get('error', {})
        err = err if isinstance(err, dict) else {'message': str(err)}
        sub = err.get('error_subcode')
        raise MetaError(f"Meta {r.status_code}: {err.get('message', r.text[:200])} (code {err.get('code')}"
                        f"{'' if sub is None else f', subcode {sub}'}, {err.get('error_user_msg', '')})",
                        r.status_code, _number(err.get('code')), _number(sub))
    return j


def _number(x):
    """Meta's code or subcode as an int, else None."""
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def upload(url, path):
    """Send a whole file to Meta's upload host (resumable upload, in one go)."""
    try:
        with open(path, 'rb') as f:
            r = requests.post(url, data=f, timeout=1800, headers={
                'Authorization': f'OAuth {token()}', 'offset': '0', 'file_size': str(path.stat().st_size)})
    except requests.RequestException as e:
        raise RuntimeError(f'Meta upload failed: {type(e).__name__}') from None
    j = answer(r)
    if j.get('success') is False:
        raise RuntimeError(f'Meta did not accept the upload of {path.name}: {j}')
    return j


def save_token():
    t = getpass.getpass('Paste the Instagram access token (it is not shown): ').strip()
    if not t:
        raise SystemExit('No token given.')
    KEYS.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({'token': t, 'refreshed': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
    check()


def save_fb_token():
    """Facebook-login mode: short user token (Graph API Explorer) → long-lived user token → Page token (no expiry).
    The app secret and the user tokens are used once here and not stored."""
    app_id = input('App ID (Meta app → App settings → Basic): ').strip()
    secret = getpass.getpass('App secret (same page → Show; it is not shown here): ').strip()
    short = getpass.getpass('User token from the Graph API Explorer (not shown): ').strip()
    if not (app_id and secret and short):
        raise SystemExit('App ID, app secret and token are all needed.')
    try:
        r = requests.get(f'{FB_API}/oauth/access_token', timeout=30, params={
            'grant_type': 'fb_exchange_token', 'client_id': app_id, 'client_secret': secret, 'fb_exchange_token': short})
        long = r.json().get('access_token')
        if not long:
            raise SystemExit(f"Token exchange failed: {r.json().get('error', {}).get('message', r.status_code)}")
        pages = requests.get(f'{FB_API}/me/accounts', timeout=30, headers={'Authorization': f'Bearer {long}'},
                             params={'fields': 'id,name,access_token,instagram_business_account{id,username}'}).json()
        if 'error' in pages:
            raise SystemExit(f"Reading your Pages failed: {pages['error'].get('message')}")
        pages = pages.get('data', [])
        linked = [p for p in pages if p.get('instagram_business_account')]
        if not pages:
            raise SystemExit('No Facebook Page in this token: tick pages_show_list and pick the Page when Facebook asks.')
        if not linked:
            raise SystemExit(f"No Instagram account linked to {', '.join(p['name'] for p in pages)}: "
                             'Page → Settings → Linked accounts → Instagram → Connect, then make a new token.')
        p = next((p for p in linked if 'almanca' in p['name'].lower()), linked[0])
        dbg = requests.get(f'{FB_API}/debug_token', timeout=30, params={
            'input_token': p['access_token'], 'access_token': f'{app_id}|{secret}'}).json().get('data', {})
    except requests.RequestException as e:
        raise SystemExit(f'Meta unreachable: {type(e).__name__}') from None
    ig = p['instagram_business_account']
    KEYS.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({'mode': 'facebook', 'page_id': p['id'], 'page_name': p['name'],
                                 'page_token': p['access_token'], 'user_id': ig['id'], 'username': ig.get('username'),
                                 'data_access_until': dbg.get('data_access_expires_at'),
                                 'refreshed': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
    exp = dbg.get('expires_at')
    print(f"Page: {p['name']} (id {p['id']}), token " + ('never expires' if exp == 0 else
          f'expires {datetime.fromtimestamp(exp):%Y-%m-%d}' if exp else 'expiry unknown'))
    print('Permissions: ' + ', '.join(dbg.get('scopes', [])))
    missing = [s for s in FB_SCOPES if s not in dbg.get('scopes', [])]
    if missing:
        print(f"WARNING: missing {', '.join(missing)}: tick them in the Graph API Explorer and make a new token, "
              'or posting will fail.')
    check()


REDIRECT = 'https://boyukbas.github.io/YT/ig-login.html'  # registered in the app; the page hands the answer on to LOCAL
LOCAL = 8765


def browser_login(app_id, config_id):
    """Meta's login dialog in the browser. Meta only sends its answer to a registered https address (REDIRECT, a page of
    the web app), which forwards it to a one-shot server here on localhost:LOCAL. Returns the code."""
    from urllib.parse import parse_qs, urlencode, urlparse
    state, got = secrets.token_urlsafe(16), {}

    class Back(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if q.get('state') == state:
                got.update(q)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            msg = 'Done: close this tab and go back to the PC window.' if 'code' in got else \
                'Meta said no: ' + got.get('error_description', 'see the PC window')
            self.wfile.write(f'<h2>{msg}</h2>'.encode())

    srv = http.server.HTTPServer(('127.0.0.1', LOCAL), Back)
    srv.timeout = 5
    url = 'https://www.facebook.com/v26.0/dialog/oauth?' + urlencode({
        'client_id': app_id, 'config_id': config_id, 'redirect_uri': REDIRECT, 'response_type': 'code', 'state': state})
    print("Opening Meta's login in the browser (if nothing opens, copy this address into it):\n" + url)
    webbrowser.open(url)
    end = time.time() + 600
    while not got and time.time() < end:
        srv.handle_request()
    srv.server_close()
    if 'code' not in got:
        raise SystemExit(f"No login: {got.get('error_description') or got.get('error') or 'timed out after 10 min'}")
    return got['code']


def save_unified_token(app_id='', config_id=''):
    """Unified-login mode (no Facebook Page): the app's login configuration → browser login → a key owned by the
    business portfolio (reported as a "system user" key without expiry), used on graph.facebook.com.
    The app secret and the login code are used once here and not stored."""
    app_id = app_id or input('App ID (Meta app → App settings → Basic): ').strip()
    secret = getpass.getpass('App secret (Meta app → App settings → Basic → Show; it is not shown here): ').strip()
    config_id = config_id or input('Configuration ID (Use cases → Instagram → API setup with Unified login): ').strip()
    if not (app_id and secret and config_id):
        raise SystemExit('App ID, app secret and configuration ID are all needed.')
    code = browser_login(app_id, config_id)
    app = f'{app_id}|{secret}'
    debug = lambda tok: requests.get(f'{FB_API}/debug_token', timeout=30,
                                     params={'input_token': tok, 'access_token': app}).json().get('data', {})
    try:
        r = requests.get(f'{FB_API}/oauth/access_token', timeout=30, params={
            'client_id': app_id, 'client_secret': secret, 'redirect_uri': REDIRECT, 'code': code}).json()
        tok = r.get('access_token')
        if not tok:
            raise SystemExit(f"Token exchange failed: {r.get('error', {}).get('message', r)}")
        dbg = debug(tok)
        if dbg.get('type') == 'USER' and dbg.get('expires_at'):            # a person's key: make it long-lived (60 days)
            long = requests.get(f'{FB_API}/oauth/access_token', timeout=30, params={
                'grant_type': 'fb_exchange_token', 'client_id': app_id, 'client_secret': secret,
                'fb_exchange_token': tok}).json().get('access_token')
            if long:
                tok, dbg = long, debug(long)
        auth = {'Authorization': f'Bearer {tok}'}
        ids = [i for g in dbg.get('granular_scopes', []) if g.get('scope') == 'instagram_content_publish'
               for i in g.get('target_ids', [])]
        if not ids:                                                         # fallback: the accounts given to the key
            ids = [a['id'] for a in requests.get(f'{FB_API}/me/assigned_instagram_accounts', timeout=30, headers=auth,
                                                 params={'fields': 'id'}).json().get('data', [])]
        if not ids and TOKEN.exists():                                      # and the account the PC already knows
            ids = [saved().get('user_id')]
        accounts = [requests.get(f'{FB_API}/{i}', timeout=30, headers=auth, params={'fields': 'id,username'}).json()
                    for i in ids]
    except requests.RequestException as e:
        raise SystemExit(f'Meta unreachable: {type(e).__name__}') from None
    accounts = [a for a in accounts if 'username' in a]
    if not accounts:                    # keep the key (private folder) so it can be looked at without another login
        KEYS.mkdir(parents=True, exist_ok=True)
        (KEYS / 'instagram-pending.json').write_text(json.dumps({'token': tok, 'type': dbg.get('type')}), encoding='utf-8')
        raise SystemExit(f"The key reaches no Instagram account (permissions: {', '.join(dbg.get('scopes', []))}). "
                         'Pick the Instagram account in the login, and tick instagram_content_publish in the configuration.')
    ig = next((a for a in accounts if 'almanca' in a['username']), accounts[0])
    KEYS.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({'mode': 'unified', 'token': tok, 'user_id': ig['id'], 'username': ig['username'],
                                 'expires_at': dbg.get('expires_at') or None,
                                 'data_access_until': dbg.get('data_access_expires_at') or None,
                                 'refreshed': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
    exp = dbg.get('expires_at')
    print(f"Instagram @{ig['username']} (id {ig['id']}), key type {dbg.get('type')}, " +
          ('never expires' if not exp else f'expires {datetime.fromtimestamp(exp):%Y-%m-%d}'))
    if 'instagram_content_publish' not in dbg.get('scopes', []):
        print('WARNING: instagram_content_publish is missing: add it to the configuration and run this again.')
    check()


def save_system_token():
    """The route that works (2026-11-03): a system user's key from Business settings → System users → Generate token,
    with the Instagram account assigned to that system user. Used on graph.facebook.com like a Unified-login key."""
    tok = getpass.getpass('System user token (Business settings → System users → Generate token; not shown): ').strip()
    if not tok:
        raise SystemExit('No token given.')
    auth = {'Authorization': f'Bearer {tok}'}
    try:
        me = requests.get(f'{FB_API}/me', timeout=30, headers=auth, params={'fields': 'id,name'}).json()
        accounts = requests.get(f'{FB_API}/me/assigned_instagram_accounts', timeout=30, headers=auth,
                                params={'fields': 'id,username'}).json().get('data', [])
        scopes = [p['permission'] for p in requests.get(f'{FB_API}/me/permissions', timeout=30, headers=auth).json()
                  .get('data', []) if p.get('status') == 'granted']
    except requests.RequestException as e:
        raise SystemExit(f'Meta unreachable: {type(e).__name__}') from None
    if 'error' in me:
        raise SystemExit(f"Meta refused the token: {me['error'].get('message')}")
    if not accounts:
        raise SystemExit(f"System user '{me.get('name')}' has no Instagram account: Business settings → System users → "
                         'Assign assets → Instagram profiles → almancadilatolyesi (full control), then try again.')
    ig = next((a for a in accounts if 'almanca' in a['username']), accounts[0])
    KEYS.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({'mode': 'unified', 'token': tok, 'user_id': ig['id'], 'username': ig['username'],
                                 'expires_at': None, 'data_access_until': None,
                                 'refreshed': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
    print(f"Instagram @{ig['username']} (id {ig['id']}) through system user '{me.get('name')}'")
    if 'instagram_content_publish' not in scopes:
        print('WARNING: instagram_content_publish is missing: tick it when generating the token and run this again.')
    check()


def renew_warning(now=None):
    """Meta lets a token read data for 90 days after the last login; then fb-token has to be run again. The
    warning from 14 days before the key's expires_at or data_access_until, else None (also without a key file).
    now: aware UTC (tests); default the current time."""
    if not TOKEN.exists():
        return None
    t, now = saved(), now or datetime.now(timezone.utc)
    for until in (t.get('data_access_until'), t.get('expires_at')):
        if isinstance(until, (int, float)) and datetime.fromtimestamp(until, timezone.utc) - now < timedelta(days=14):
            how = 'run  instagram.py unified-token  again' if t.get('mode') == 'unified' else \
                'make a new token in the Graph API Explorer and run  instagram.py fb-token'
            return f"WARNING: Meta access ends {datetime.fromtimestamp(until):%Y-%m-%d}: {how} (instagram-setup.md)."
    return None


def check():
    t = saved()
    if t.get('mode') in ('facebook', 'unified'):
        ig = call('GET', t['user_id'], fields='username,media_count,followers_count')
        print(f"Instagram: @{ig.get('username')}, {ig.get('media_count', 0)} posts, {ig.get('followers_count', 0)} followers")
        if FACEBOOK_PAGE and 'page_id' in t:
            page = call('GET', t['page_id'], fields='name,followers_count')
            print(f"Facebook Page: {page.get('name')}, {page.get('followers_count', 0)} followers")
        me = {'user_id': t['user_id'], 'username': ig.get('username')}
    else:
        me = call('GET', 'me', fields='user_id,username,account_type,media_count')
        print(f"Instagram: @{me.get('username')} ({me.get('account_type')}), {me.get('media_count', 0)} posts, id {me.get('user_id')}")
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


# ---------- posting ----------

class Quiet(http.server.SimpleHTTPRequestHandler):
    """Serves the shared files only: no folder listings (they would reveal the random path)."""
    def log_message(self, *a):
        pass

    def list_directory(self, path):
        self.send_error(404)


class Share:
    """Serve a few files at https://<random>.trycloudflare.com/<random>/<name> while the block runs.
    __enter__ raises ShareError when cloudflared.exe is missing, gives no address within 180 s, or the address
    does not answer; whatever it started is stopped again."""

    def __init__(self, files):
        self.files = [Path(f) for f in files]
        self.proc = self.httpd = None

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ig-'))
        try:
            return self._open()
        except BaseException:
            self.__exit__()
            raise

    def _open(self):
        self.key = secrets.token_urlsafe(16)
        (self.tmp / self.key).mkdir()
        for f in self.files:
            shutil.copy2(f, self.tmp / self.key / f.name)
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(self.tmp)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        port = self.httpd.server_address[1]
        try:
            self.proc = subprocess.Popen([str(CLOUDFLARED), 'tunnel', '--url', f'http://127.0.0.1:{port}',
                                          '--no-autoupdate'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         text=True, errors='replace')
        except OSError as e:
            raise ShareError(f'cloudflared could not start ({type(e).__name__}): is {CLOUDFLARED} there?') from None
        found = []

        def read():
            for line in self.proc.stderr:
                m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if m and not found:
                    found.append(m.group(0))
        threading.Thread(target=read, daemon=True).start()
        end = time.time() + 180                      # it took 73 s once (2026-11-02)
        while not found and time.time() < end:
            time.sleep(0.5)
        if not found:
            raise ShareError('cloudflared gave no public address')
        self.urls = [f'{found[0]}/{self.key}/{f.name}' for f in self.files]
        while time.time() < end:                     # the new address takes a few seconds to reach the internet
            try:
                if requests.head(self.urls[0], timeout=10).status_code == 200:
                    return self.urls
            except requests.RequestException:
                pass
            time.sleep(3)
        raise ShareError('the temporary public address did not answer')

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)


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


def stamp():
    return {'posted': datetime.now(ISTANBUL).strftime('%Y-%m-%d %H:%M'), 'posted_utc': datetime.now(timezone.utc).isoformat()}


def wait_container(cid, name):
    """Poll status_code every POLL (10) s, at most 60 times. ERROR or EXPIRED raise ContainerError with that status;
    the 60th poll raises ContainerError(..., 'TIMEOUT')."""
    for _ in range(60):                                   # Instagram fetches + processes the video (usually < 1 min)
        time.sleep(POLL)
        s = call('GET', cid, fields='status_code,status')
        if s.get('status_code') == 'FINISHED':
            return
        if s.get('status_code') in ('ERROR', 'EXPIRED'):
            raise ContainerError(f"Instagram could not process {name}: {s.get('status')}", s['status_code'])
    raise ContainerError(f'{name}: Instagram still processing after 10 min', 'TIMEOUT')


def post_instagram(mp4, cover, cap, log, on_stage=None, on_container=None, on_published=None):
    """Post one Reel, calling in order: on_stage('sharing') -> Share -> POST {uid}/media -> on_container(id) ->
    on_stage('processing') -> wait_container -> on_stage('publishing') -> POST media_publish ->
    on_published(media_id) -> GET permalink. The permalink read is best effort: a MetaError there gives ''.
    Returns {'id', 'permalink', 'posted', 'posted_utc'}. Raises MetaError, ContainerError or ShareError;
    never retries."""
    tell = lambda f, *a: f and f(*a)
    uid = saved().get('user_id') or check()['user_id']
    log(f'{mp4.name}: sending to Instagram')
    tell(on_stage, 'sharing')
    # Instagram's direct upload (rupload ig-api-upload) refuses Page tokens (ProcessingFailedError, 2026-11-02), and
    # user tokens expire after 60 days, so Instagram fetches the files through the temporary link in both modes.
    with Share([mp4, cover]) as (video_url, cover_url):
        c = call('POST', f'{uid}/media', media_type='REELS', video_url=video_url, cover_url=cover_url, caption=cap,
                 share_to_feed='true')
        tell(on_container, c['id'])
        tell(on_stage, 'processing')
        wait_container(c['id'], mp4.name)
    tell(on_stage, 'publishing')
    m = call('POST', f'{uid}/media_publish', creation_id=c['id'])
    tell(on_published, m['id'])
    when = stamp()                                        # the moment it went live
    try:
        link = call('GET', m['id'], fields='permalink').get('permalink', '')
    except MetaError as e:
        log(f'{mp4.name}: posted; its link is read later ({e})')
        link = ''
    return {'id': m['id'], 'permalink': link, **when}


def post_facebook(mp4, cover, text, log):
    """The same Reel on the Facebook Page (Reels API: start → upload → finish, then wait until it is published)."""
    page = saved()['page_id']
    log(f'{mp4.name}: sending to the Facebook Page')
    v = call('POST', f'{page}/video_reels', upload_phase='start')
    upload(v.get('upload_url') or f"{RUPLOAD}/video-upload/v26.0/{v['video_id']}", mp4)
    try:
        with open(cover, 'rb') as f:
            call('POST', f"{v['video_id']}/thumbnails", files={'source': (cover.name, f, 'image/jpeg')}, is_preferred='true')
    except RuntimeError as e:
        log(f'Facebook cover not set (the Reel keeps a frame of the video): {e}')
    call('POST', f'{page}/video_reels', upload_phase='finish', video_id=v['video_id'], video_state='PUBLISHED',
         description=text)
    for _ in range(60):
        time.sleep(10)
        st = call('GET', v['video_id'], fields='status').get('status', {})
        phases = [st.get('video_status')] + [st.get(k, {}).get('status') for k in ('uploading_phase', 'processing_phase', 'publishing_phase')]
        if 'error' in phases:
            raise RuntimeError(f'Facebook could not publish {mp4.name}: {st}')
        if st.get('publishing_phase', {}).get('status') == 'complete':
            break
    else:
        raise RuntimeError(f'{mp4.name}: Facebook still processing after 10 min')
    link = call('GET', v['video_id'], fields='permalink_url').get('permalink_url', '')
    return {'id': v['video_id'], 'permalink': 'https://www.facebook.com' + link if link.startswith('/') else link, **stamp()}


def page_video(mp4, **fields):
    """Chunked upload of a video to the Page (a single upload of 158 MB was refused with 413). Returns {'id': ...}."""
    url = f"https://graph-video.facebook.com/v26.0/{saved()['page_id']}/videos"
    s = call('POST', url, upload_phase='start', file_size=str(mp4.stat().st_size))
    start, end = int(s['start_offset']), int(s['end_offset'])
    with open(mp4, 'rb') as f:
        while start < end:
            f.seek(start)
            r = call('POST', url, files={'video_file_chunk': (mp4.name, f.read(end - start), 'application/octet-stream')},
                     upload_phase='transfer', upload_session_id=s['upload_session_id'], start_offset=str(start))
            start, end = int(r['start_offset']), int(r['end_offset'])
    call('POST', url, upload_phase='finish', upload_session_id=s['upload_session_id'], **fields)
    return {'id': s['video_id']}


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


def post_long_facebook(lesson, dry=False, log=print):
    """The lesson's long video as a normal Facebook Page video (16:9 suits Facebook; Instagram gets the Reels only),
    once it is public on YouTube. Same title and description as on YouTube (chapters work on Facebook too)."""
    found = long_video(lesson)
    lesson, out, _ = lesson_files(lesson)
    posted = load(out)
    if not found or 'long' in posted.get('_facebook', {}):
        return None
    mp4, data, rec = found
    title = data['titles'][0][:255]
    text = data['descriptions'][0].replace('\r', '')
    if dry:
        return f'would post {mp4.name} to the Facebook Page: {title}'
    log(f'{mp4.name}: sending to the Facebook Page as a video')
    v = page_video(mp4, title=title, description=text, published='true')
    link = ''
    for _ in range(30):                                   # Facebook processes the video; the link comes with it
        time.sleep(10)
        link = call('GET', v['id'], fields='permalink_url').get('permalink_url', '')
        if link:
            break
    posted.setdefault('_facebook', {})['long'] = {'id': v['id'], 'permalink': 'https://www.facebook.com' + link
                                                  if link.startswith('/') else link, **stamp()}
    (extras(out) / POSTED).write_text(json.dumps(posted, ensure_ascii=False, indent=1), encoding='utf-8')
    write_md(lesson, out, data)
    return f"Facebook Page: {mp4.name} posted {posted['_facebook']['long']['permalink']}"


def main(argv=None):
    """The command line (see the module docstring). post and daily go through publish (lazy import)."""
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['system-token', 'fb-token', 'unified-token', 'token', 'check', 'prepare', 'post', 'daily'])
    ap.add_argument('path', nargs='?')
    ap.add_argument('--dry-run', action='store_true', help='post: show what would be posted; nothing is sent')
    ap.add_argument('--force', action='store_true',
                    help='post despite the gap and same-day warnings (never past the 24 h hard cap)')
    ap.add_argument('--app-id', default='', help='unified-token: the Meta app ID (asked if not given)')
    ap.add_argument('--config-id', default='', help='unified-token: the login configuration ID (asked if not given)')
    a = ap.parse_args(argv)
    if a.cmd in ('prepare', 'post', 'daily') and not a.path:
        ap.error(f'{a.cmd} needs a folder')
    if a.cmd == 'system-token':
        save_system_token()
    elif a.cmd == 'fb-token':
        save_fb_token()
    elif a.cmd == 'unified-token':
        save_unified_token(a.app_id, a.config_id)
    elif a.cmd == 'token':
        save_token()
    elif a.cmd == 'check':
        check()
    elif a.cmd == 'prepare':
        prepare(a.path)
    elif a.cmd == 'post':
        import publish
        print(publish.cli_post(Path(a.path), a.dry_run, a.force))
    else:
        import publish
        if not publish.auto_step(Path(a.path)):          # each line was already printed by publish.log_line
            print('Nothing to post now.')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
