"""Instagram: the lesson's Shorts as Reels, with their own cover and caption, posted one a day
(see instagram-setup.md for the one-time setup).

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
  python instagram.py post "<lesson>" [--dry-run]   post the lesson's next Reel now (refuses if one went out < 20 h ago
                                                    unless --force: one Reel a day, so followers aren't flooded)
  python instagram.py daily "<root>"                the watcher's step: after POST_TIME, post the next Reel of the
                                                    oldest lesson that has some left, if none went out in the last 20 h

Instagram downloads the video + cover from a public URL (its direct upload refuses Page tokens), so while one Reel is
being posted, the PC serves just those two files under a random path through a temporary Cloudflare quick tunnel
(cloudflared, no account), and closes it as soon as Instagram has them. Posting to the Facebook Page as well
(FACEBOOK_PAGE) is off since 2026-11-03: the Page got few views and was deleted; Instagram does well.
Token: %USERPROFILE%\\.shorts-pipeline\\instagram.json (Page / Unified-login key: no expiry; Instagram token: 60 days, refreshed
here every week). Record: <lesson>/Output/extras/instagram-posted.json."""
import argparse, getpass, http.server, json, re, secrets, shutil, subprocess, sys, tempfile, threading, time, webbrowser
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from tools import EXTRAS, extras, probe, records

KEYS = Path.home() / '.shorts-pipeline'
TOKEN = KEYS / 'instagram.json'
API = 'https://graph.instagram.com/v26.0'
FB_API = 'https://graph.facebook.com/v26.0'
RUPLOAD = 'https://rupload.facebook.com'
FB_SCOPES = ['pages_show_list', 'pages_read_engagement', 'pages_manage_posts', 'instagram_basic',
             'instagram_content_publish', 'business_management']
CLOUDFLARED = Path(r'D:\YT\tools\cloudflared.exe')
ISTANBUL = timezone(timedelta(hours=3))
POSTED = 'instagram-posted.json'
POST_TIME = '20:00'                                      # Istanbul; the daily Reel goes out after this time
GAP = timedelta(hours=20)                                # at most one Reel per day
FACEBOOK_PAGE = False                                    # also post Reels + long videos to the Page (off: deleted 2026-11-03)
LONG_HOURS = (12, 17)                                    # Facebook long videos: from 12:00, last start 16:59
MAX_TAGS = 5                                             # Instagram shows few hashtags; more looks like spam
SAVE_LINE = '💾 Kaydet, sonra tekrar et!'
YOUTUBE_LINE = "▶️ Dersin tamamı YouTube'da: Almanca Dil Atölyesi (link profilde)"
CHANNEL = 'https://www.youtube.com/@sinembb'


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


def call(method, path, files=None, **params):
    """One Graph API call. The token goes in a header, so it never shows up in an error message or log."""
    url = path if path.startswith('https://') else f"{FB_API if fb_mode() else API}/{path}"
    try:
        r = requests.request(method, url, timeout=600, headers={'Authorization': f'Bearer {token()}'},
                             **({'data': params, 'files': files} if files else {'params': params}))
    except requests.RequestException as e:
        raise RuntimeError(f'Meta unreachable: {type(e).__name__}') from None
    return answer(r)


def answer(r):
    j = r.json() if r.headers.get('content-type', '').startswith(('application/json', 'text/javascript')) else {}
    if not r.ok or 'error' in j:
        err = j.get('error', {})
        err = err if isinstance(err, dict) else {'message': str(err)}
        raise RuntimeError(f"Meta {r.status_code}: {err.get('message', r.text[:200])} "
                           f"(code {err.get('code')}, {err.get('error_user_msg', '')})")
    return j


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


def renew_warning():
    """Meta lets a token read data for 90 days after the last login; then fb-token has to be run again."""
    t = saved()
    for until in (t.get('data_access_until'), t.get('expires_at')):
        if until and datetime.fromtimestamp(until, timezone.utc) - datetime.now(timezone.utc) < timedelta(days=14):
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


def reels(out, data):
    """[(mp4, short entry)] in Short order. A one-clip Short lesson (no -short-N files; the main video is vertical and
    at most 3 min) is one Reel itself, with the main video's first title/description (decided 2026-11-04)."""
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
         'Posted automatically, one Reel a day after ' + POST_TIME + ' (Istanbul). To post one by hand instead: '
         'Instagram → + → Reel → pick the video → Edit cover → Add from camera roll → the cover → paste the caption.', '']
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
    """Serves the shared files only: no folder listings (they would reveal the random path)."""
    def log_message(self, *a):
        pass

    def list_directory(self, path):
        self.send_error(404)


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
        end = time.time() + 180                      # it took 73 s once (2026-11-02)
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
    p = extras(out) / POSTED
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}


def last_post(root):
    times = [datetime.fromisoformat(t) for _, _, p in records(root, POSTED)
             for r in json.loads(p.read_text(encoding='utf-8')).values()
             for t in (r.get('posted_utc'), r.get('facebook', {}).get('posted_utc')) if t]
    return max(times, default=None)


def stamp():
    return {'posted': datetime.now(ISTANBUL).strftime('%Y-%m-%d %H:%M'), 'posted_utc': datetime.now(timezone.utc).isoformat()}


def wait_container(cid, name):
    for _ in range(60):                                   # Instagram fetches + processes the video (usually < 1 min)
        time.sleep(10)
        s = call('GET', cid, fields='status_code,status')
        if s.get('status_code') == 'FINISHED':
            return
        if s.get('status_code') in ('ERROR', 'EXPIRED'):
            raise RuntimeError(f"Instagram could not process {name}: {s.get('status')}")
    raise RuntimeError(f'{name}: Instagram still processing after 10 min')


def post_instagram(mp4, cover, cap, log):
    uid = saved().get('user_id') or check()['user_id']
    log(f'{mp4.name}: sending to Instagram')
    # Instagram's direct upload (rupload ig-api-upload) refuses Page tokens (ProcessingFailedError, 2026-11-02), and
    # user tokens expire after 60 days, so Instagram fetches the files through the temporary link in both modes.
    with Share([mp4, cover]) as (video_url, cover_url):
        c = call('POST', f'{uid}/media', media_type='REELS', video_url=video_url, cover_url=cover_url, caption=cap,
                 share_to_feed='true')
        wait_container(c['id'], mp4.name)
    m = call('POST', f'{uid}/media_publish', creation_id=c['id'])
    info = call('GET', m['id'], fields='permalink,timestamp')
    return {'id': m['id'], 'permalink': info.get('permalink', ''), **stamp()}


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


def post_next(lesson, dry=False, force=False, log=print):
    """Post the lesson's next Reel. Returns a one-line summary, or None when it has none left."""
    lesson, out, data = lesson_files(lesson)
    posted = load(out)
    fb = fb_mode() and FACEBOOK_PAGE and 'page_id' in saved()
    done = lambda r: bool(r) and 'id' in r and (not fb or 'facebook' in r)
    todo = [(m, sh) for m, sh in reels(out, data) if not done(posted.get(m.name))]
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
        return f'would post {mp4.name} with {cover.name}:\n{cap}' + \
            (f'\n\nFacebook Page text:\n{fb_caption(cap, out)}' if fb else '')
    length = probe(mp4)['duration']
    if not 3 <= length <= 900:
        raise RuntimeError(f'{mp4.name}: {length:.0f} s, Reels must be 3 s to 15 min')
    rec = dict(posted.get(mp4.name, {}))

    def save():
        posted[mp4.name] = rec
        (extras(out) / POSTED).write_text(json.dumps(posted, ensure_ascii=False, indent=1), encoding='utf-8')
        write_md(lesson, out, data)
    said = []
    if 'id' not in rec:
        rec.update(post_instagram(mp4, cover, cap, log))
        save()
        said.append(f"Instagram {rec['permalink']}")
    if fb and 'facebook' not in rec:
        try:
            rec['facebook'] = post_facebook(mp4, cover, fb_caption(cap, out), log)
            save()
            said.append(f"Facebook {rec['facebook']['permalink']}")
        except RuntimeError as e:                         # Instagram has it; the Page gets it with the next daily run
            said.append(f'WARNING: Facebook Page failed, tries again tomorrow: {e}')
    return f"{mp4.name} posted: {'; '.join(said)} ({len(todo) - 1} left in this lesson)"


def daily(root, log=print):
    """Watcher step: at most one Reel a day, after POST_TIME, oldest lesson first, not before channel.json's
    "instagram": {"start": "YYYY-MM-DD"} (a new account should be used by hand for a few days first).
    With FACEBOOK_PAGE on, Facebook-login mode also puts each lesson's long video on the Page once it is public on YouTube: one a day, between
    LONG_HOURS, so it never lands within 3 hours of the evening Reel."""
    if not TOKEN.exists():
        return None
    now = datetime.now(ISTANBUL)
    start = profile(root).get('instagram', {}).get('start')
    if start and now.strftime('%Y-%m-%d') < start:
        return None
    lessons = [l for l, _, p in sorted(records(root, 'youtube.json'), key=lambda r: r[2].stat().st_mtime)
               if not l.name.startswith('_')]
    if fb_mode() and FACEBOOK_PAGE and 'page_id' in saved() and LONG_HOURS[0] <= now.hour < LONG_HOURS[1]:
        longs = [datetime.fromisoformat(r['long']['posted_utc']) for l in lessons for o in l.glob(f'*/{EXTRAS}/{POSTED}')
                 for r in [json.loads(o.read_text(encoding='utf-8')).get('_facebook', {})] if 'long' in r]
        if not longs or datetime.now(timezone.utc) - max(longs) > GAP:
            for lesson in lessons:
                r = post_long_facebook(lesson, log=log)
                if r:
                    log(r)
                    return r
    hh, mm = map(int, POST_TIME.split(':'))
    last = last_post(root)
    if (now.hour, now.minute) < (hh, mm) or (last and datetime.now(timezone.utc) - last < GAP):
        return None
    warn = renew_warning()
    if warn:
        log(warn)
    for lesson in lessons:
        r = post_next(lesson, log=log)
        if r:
            log(r)
            return r
    return None


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['system-token', 'fb-token', 'unified-token', 'token', 'check', 'prepare', 'post', 'daily'])
    ap.add_argument('path', nargs='?')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='post even if a Reel went out less than 20 h ago')
    ap.add_argument('--app-id', default='', help='unified-token: the Meta app ID (asked if not given)')
    ap.add_argument('--config-id', default='', help='unified-token: the login configuration ID (asked if not given)')
    a = ap.parse_args()
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
        print(post_next(a.path, a.dry_run, a.force) or 'Nothing left to post in this lesson.')
    else:
        print(daily(a.path) or 'Nothing to post now.')
