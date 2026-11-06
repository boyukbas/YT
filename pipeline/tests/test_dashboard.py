"""dashboard.py v2 (spec 7.8, 8 and 10; rules S12 and S20-S26): the Access login check with keys made here, the POST
guards and the limiter, every endpoint's JSON against the spec 8.3 shapes (served --dev --fixtures on a free loopback
port), the 410s, covers, the static whitelist and traversal, log tails and redaction, Home's attention cards, health
and counts, the background thread, the CLI and the --dev refusals.

Nothing leaves 127.0.0.1 and nothing is posted for real: the fake platform and fake YouTube only. GuardWorks runs
first (load_tests)."""
import base64, contextlib, http.client, io, json, os, re, shutil, socket, sqlite3, subprocess, tempfile, threading, time
import types
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from PIL import Image

from . import fixtures, guard
import dashboard
import easy
import instagram as IG
import platform_fake
import publish
import schedule as S
import tools

TILES = ('yt_subscribers', 'yt_views', 'ig_followers', 'ig_likes')                # stats.tiles(), in order
NOW = fixtures.NOW
KEY = {n: f'new/Output/new-short-{n}.mp4' for n in range(1, 10)}
ID = {n: publish.item_id(k) for n, k in KEY.items()}
UM = 'Um etwas bitten/Output/um-etwas-bitten-video.mp4'
ZU = 'ZU + INFINITIV/Output (Easy)/zu-infinitiv-video.mp4'
CSP = ("default-src 'self'; img-src 'self' https://i.ytimg.com; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
SECURITY = {'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': CSP}
SECRET = 'EAAB' + 'Qx7Zt2' * 4                     # made up, shaped like a Meta token (built here, so no secret
JWT = 'eyJhbGciOiJSUzI1NiJ9' + '.eyJzdWIiOiJleGFtcGxlIn0.' + 'c2lnbmF0dXJl'    # scanner takes the file for one)


# ---------- JSON shapes (spec 8.2 and 8.3) ----------

class Rx:
    """A string matching a pattern."""
    def __init__(self, rx):
        self.rx = re.compile(rx)

    def __repr__(self):
        return f'Rx({self.rx.pattern!r})'


TIME, DAY, HEX12 = Rx(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ'), Rx(r'\d{4}-\d\d-\d\d'), Rx(r'[0-9a-f]{12}')
T_, NUM, NUM_ = (TIME, None), (int, float), (int, float, None)
ACTION = (None, {'kind': 'link', 'label': str, 'href': str},
          {'kind': 'call', 'label': str, 'endpoint': str, 'body': dict, 'confirm': (str, None), 'ack': (str, None)})
ATTENTION = {'id': str, 'level': {'red', 'yellow'}, 'who': {'sinem', 'pc', 'anyone'}, 'title': str, 'text': str,
             'action': ACTION}
TILE = {'id': str, 'label': str, 'value': NUM_, 'd24': NUM_, 'd7': NUM_, 'spark': [NUM_], 'at': T_, 'stale': bool}
HOME = {'now': TIME, 'me': str, 'channel': {'name': str, 'url': str}, 'attention': [ATTENTION],
        'health': {'watcher': {'ok': bool, 'at': T_, 'busy_since': T_, 'text': str},
                   'drive': {'ok': bool, 'letter': (str, None), 'text': str},
                   'platforms': [{'name': str, 'ok': bool, 'set_up': bool, 'breaker': (str, None), 'text': str}],
                   'stats': {'ok': bool, 'at': T_, 'text': str}},
        'next': [{'platform': (str, None), 'text': str, 'at': T_, 'href': (str, None)}],
        'counts': {'instagram': {'waiting': int, 'skipped': int, 'failed': int, 'posted': int},
                   'youtube': {'ready': int, 'uploaded': int, 'scheduled': int, 'public': int}},
        'tiles': [TILE]}
SETTINGS = {'auto': bool, 'every_days': int, 'per_day': int, 'days': [str], 'post_after': str, 'post_before': str,
            'min_gap_hours': NUM, 'hard_cap_24h': int, 'start_date': (str, None)}
BREAKER = {'name': (str, None), 'state': {'closed', 'cooling', 'stopped'}, 'until': T_, 'kind': (str, None),
           'message': (str, None), 'since': T_}
AUTO = {'setting': bool, 'effective': bool,
        'reason': {'invalid', 'off', 'stopped_by_file', 'suspended', 'breaker', 'unresolved', 'empty', 'scheduled',
                   'no_slot'},
        'text': str, 'summary': str, 'pause_file': (None, {'since': TIME, 'by': (str, None)}),
        'suspended': (None, {'since': T_, 'why': (str, None)}),
        'next': (None, {'at': T_, 'slot': (DAY, None), 'id': (HEX12, None), 'reason': str, 'text': str})}
FORECAST = {'assumes_on': bool, 'runway': (DAY, None), 'runway_text': str,
            'rows': [{'id': HEX12, 'at': TIME, 'slot': DAY}], 'more': int}
ITEM = {'id': HEX12, 'key': str, 'lesson': str, 'file': str, 'part': (int, None), 'title': str, 'seconds': (int, None),
        'state': {'waiting', 'skipped', 'failed', 'checking', 'posted'}, 'position': (int, None), 'top': bool,
        'custom_caption': bool, 'failed': (None, {'at': T_, 'reason': str}),
        'posted': (None, {'at': T_, 'by': str, 'url': (str, None), 'likes': (int, None), 'comments': (int, None)}),
        'cover': (str, None), 'eta': T_}
QUEUE = {'rev': int, 'waiting': [ITEM], 'failed': [ITEM], 'skipped': [ITEM], 'posted': [ITEM], 'posted_total': int,
         'problems': [dict]}
JOB = {'id': Rx(r'j[0-9a-f]{10}'), 'platform': str, 'item': HEX12, 'key': str, 'title': (str, None),
       'by': {'manual', 'auto', 'cli'}, 'who': (str, None),
       'state': {'queued', 'running', 'done', 'failed', 'interrupted', 'unknown'}, 'stage': (str, None),
       'stages': [str], 'created': TIME, 'started': T_, 'finished': T_, 'url': (str, None), 'kind': (str, None),
       'error': (str, None), 'result': (str, None), 'record_pending': bool}
LAST_POST = (None, {'at': TIME, 'by': str, 'key': (str, None), 'title': (str, None), 'url': (str, None)})
PUBLISH = {'platform': 'instagram', 'label': str, 'noun': str, 'verb': str, 'setup_hint': str,
           'platforms': [{'name': str, 'label': str}], 'set_up': bool, 'account_url': (str, None), 'now': TIME,
           'settings': SETTINGS, 'settings_rev': int, 'settings_problems': [str],
           'presets': [{'id': str, 'label': str, 'values': dict}], 'auto': AUTO, 'breaker': BREAKER,
           'last_post': LAST_POST, 'posts_24h': int, 'next_possible': T_,
           'counts': {'waiting': int, 'skipped': int, 'failed': int, 'posted': int,
                      'by_lesson': [{'lesson': str, 'waiting': int}]},
           'forecast': FORECAST, 'job': (None, JOB), 'unresolved': [JOB], 'queue': QUEUE,
           'paused': (None, {'until': TIME, 'text': str})}
CHECK = {'code': str, 'level': {'block', 'warn', 'info'}, 'text': str, 'data': dict}
PREVIEW = {'platform': 'instagram', 'item': ITEM, 'caption': str, 'default_caption': str,
           'caption_limits': {'chars': int, 'tags_hard': int, 'tags_suggested': int, 'visible': int},
           'checks': [CHECK], 'check': Rx(r'[0-9a-f]{16}'), 'can_post': bool, 'blocked_by': (str, None),
           'next_possible': T_, 'last_post': LAST_POST, 'posts_24h': int}
REEL = (None, {'platform': str, 'id': HEX12, 'state': {'waiting', 'skipped', 'failed', 'checking', 'posted'}})
VIDEOS = {'generated': TIME, 'counts_at': T_,
          'channel': {'name': str, 'url': str, 'subscribers': (int, None), 'views': (int, None), 'videos': (int, None)},
          'videos': [dict]}
STATS = {'now': TIME, 'sources': [{'source': str, 'last_ok': T_, 'last_try': T_, 'next_try': T_, 'fails': int,
                                   'error': (str, None)}],
         'tiles': [TILE],
         'youtube_videos': [{'id': str, 'title': (str, None), 'kind': (str, None), 'published': T_, 'views': NUM_,
                             'likes': NUM_, 'comments': NUM_, 'd24_views': NUM_, 'thumb': (str, None),
                             'url': (str, None)}],
         'reels': [{'id': str, 'at': T_, 'title': (str, None), 'likes': NUM_, 'comments': NUM_, 'url': (str, None),
                    'lesson': (str, None), 'file': (str, None)}],
         'pairs': [{'lesson': str, 'file': str, 'youtube': dict, 'instagram': dict}],
         'insights': (None, {'available': bool, 'checked': T_, 'text': (str, None)}),      # None: never checked
         'limit': (None, {'used': NUM_, 'total': NUM_, 'at': T_, 'stale': bool})}
ACTIVITY = {'items': [{'id': int, 'at': TIME, 'who': str, 'kind': str, 'platform': (str, None), 'label': (str, None),
                       'text': str, 'url': (str, None)}],
            'more': bool, 'logs': {'watch': [str], 'instagram': [str], 'youtube': [str]}}


def conforms(test, value, schema, where='$'):
    """Fail unless value has schema's shape: dicts have exactly its keys, [x] lists hold x, tuples are choices,
    sets are allowed values, Rx a matching string, int never a bool, other values must be equal."""
    if isinstance(schema, tuple):
        errors = []
        for choice in schema:
            try:
                return conforms(test, value, choice, where)
            except AssertionError as e:
                errors.append(str(e).splitlines()[0])
        test.fail(f'{where}: {value!r} matches none of the choices ({" | ".join(errors)})')
    elif schema is None:
        test.assertIsNone(value, where)
    elif isinstance(schema, Rx):
        test.assertIsInstance(value, str, where)
        test.assertRegex(value, rf'\A(?:{schema.rx.pattern})\Z', where)
    elif isinstance(schema, dict):
        test.assertIsInstance(value, dict, where)
        test.assertEqual(sorted(value), sorted(schema), f'{where}: keys')
        for k, s in schema.items():
            conforms(test, value[k], s, f'{where}.{k}')
    elif isinstance(schema, list):
        test.assertIsInstance(value, list, where)
        for n, v in enumerate(value):
            conforms(test, v, schema[0], f'{where}[{n}]')
    elif isinstance(schema, set):
        test.assertTrue(isinstance(value, (str, int)) and value in schema, f'{where}: {value!r} not in {schema}')
    elif schema is int:
        test.assertIs(type(value), int, where)
    elif schema is bool:
        test.assertIs(type(value), bool, where)
    elif isinstance(schema, type):
        test.assertIsInstance(value, schema, where)
        test.assertNotIsInstance(value, bool, where) if schema is float else None
    else:
        test.assertEqual(value, schema, where)


# ---------- servers ----------

class Answer:
    def __init__(self, status, headers, data):
        self.status, self.headers, self.data = status, headers, data

    @property
    def json(self):
        return json.loads(self.data.decode('utf-8'))


class Server:
    """dashboard.make_server(0, ...) served from a thread on a free loopback port."""

    def __init__(self, **kw):
        self.httpd = dashboard.make_server(0, **kw)
        self.port = self.httpd.server_address[1]
        self.good = {'Content-Type': 'application/json', 'X-Dashboard': '1', 'Origin': f'http://127.0.0.1:{self.port}',
                     'Sec-Fetch-Site': 'same-origin'}               # what the page's own fetch() sends
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def call(self, method, path, body=None, headers=None, raw=None) -> Answer:
        """One request; a POST gets the page's headers unless headers replace them (None leaves one out)."""
        h = {**(self.good if method == 'POST' else {}), **(headers or {})}
        data = raw if raw is not None else None if body is None else json.dumps(body).encode('utf-8')
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            c.request(method, path, body=data, headers={k: v for k, v in h.items() if v is not None})
            r = c.getresponse()
            return Answer(r.status, r.headers, r.read())
        finally:
            c.close()

    def announced(self, path, length) -> Answer:
        """A POST that announces a Content-Length and sends no body."""
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            c.putrequest('POST', path)
            for k, v in self.good.items():
                c.putheader(k, v)
            c.putheader('Content-Length', str(length))
            c.endheaders()
            r = c.getresponse()
            return Answer(r.status, r.headers, r.read())
        finally:
            c.close()


def _handler_defaults():
    dashboard.Handler.access, dashboard.Handler.dev, dashboard.Handler.root, dashboard.Handler.youtube = (
        None, False, None, None)
    dashboard.Handler.limiter, dashboard.Handler.dry_limiter = dashboard.Limiter(), dashboard.Limiter(dashboard.DRY_RUNS)


class Case(unittest.TestCase):
    """guard.reset(); the server's log lines go to self.out; the clock is fixtures.NOW."""

    def setUp(self):
        guard.reset()
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))
        for module in (publish, dashboard):
            self.enterContext(mock.patch.object(module, '_utcnow', lambda: NOW))

    def assertError(self, a: Answer, status: int, code: str):
        self.assertEqual(a.status, status, a.data[:300])
        body = a.json
        self.assertEqual(body['error'], code, body)
        self.assertIsInstance(body['message'], str)
        return body

    def assertSecure(self, a: Answer, cache='no-store'):
        for k, v in SECURITY.items():
            self.assertEqual(a.headers.get(k), v, k)
        self.assertEqual(a.headers.get('Cache-Control'), cache)

    def pause(self):
        """The pause flag, as on the PC (before the first settings load: the migration then turns auto off)."""
        f = S.pause_file('instagram')
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('owner 2026-11-04 21:27\n', encoding='utf-8')


class DevCase(Case):
    """A --dev --fixtures server for the class. Each test starts afresh: an empty WORK, a new fake platform with
    new 1-5 posted, a new limiter, the clock at fixtures.NOW."""
    server: Server

    @classmethod
    def setUpClass(cls):
        guard.reset()
        cls.base = Path(tempfile.mkdtemp(prefix='dash-', dir=guard.TMP))
        with contextlib.redirect_stdout(io.StringIO()):
            cls.server = Server(dev=True, root=cls.base, fixtures=True)
        cls.root = dashboard.Handler.root
        cls.extra(cls.root)

    @classmethod
    def extra(cls, root):
        """More lessons for a subclass."""

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        _handler_defaults()
        publish.set_registry(None)
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        super().setUp()
        dashboard.Handler.root = self.root
        dashboard.Handler.limiter, dashboard.Handler.dry_limiter = dashboard.Limiter(), dashboard.Limiter(dashboard.DRY_RUNS)
        self.fake = fixtures.fake_platform(self.root)
        publish.set_registry({'instagram': self.fake})

    def get(self, path, **kw) -> Answer:
        return self.server.call('GET', path, **kw)

    def post(self, path, body=None, **kw) -> Answer:
        return self.server.call('POST', path, body if body is not None else {}, **kw)


# ---------- S25 ----------

class GuardWorks(unittest.TestCase):
    """S25 (runs first): no socket leaves the PC, cloudflared and schtasks never start, and the dashboard works in
    the guard's scratch folder with the fixtures' probe."""

    def setUp(self):
        guard.reset()

    def test_no_network(self):
        for address in (('192.0.2.10', 443), ('8.8.8.8', 53)):              # TEST-NET and a public resolver
            with socket.socket() as s, self.assertRaises(guard.NetworkBlocked):
                s.connect(address)
        with self.assertRaises(guard.NetworkBlocked):
            socket.getaddrinfo('example.com', 443)
        with self.assertRaises(Exception) as caught:
            dashboard.requests.get('https://example-team.cloudflareaccess.com/cdn-cgi/access/certs', timeout=2)
        chain, e = [], caught.exception
        while e is not None:
            chain.append(type(e))
            e = e.__cause__ or e.__context__
        self.assertIn(guard.NetworkBlocked, chain)

    def test_no_programs(self):
        for args in (['schtasks', '/run', '/tn', dashboard.WATCHER], ['cloudflared', 'tunnel', 'run']):
            with self.assertRaises(guard.NetworkBlocked):
                subprocess.run(args, capture_output=True)
        self.assertFalse(dashboard.Handler.dev)
        self.assertFalse(dashboard.start_watcher())                         # blocked, so the retry stays queued

    def test_scratch_folders(self):
        for p in (dashboard.CONFIG, dashboard.LOG, dashboard.PROBES, dashboard.COVER_CACHE, dashboard.HEARTBEAT,
                  dashboard.WATCH_LOCK, dashboard.WATCH_LOG, publish.WORK, S.WORK, dashboard.stats.WORK, IG.WORK):
            self.assertTrue(guard._inside(p, guard.WORK), p)
            self.assertFalse(guard._inside(p, guard.REAL_WORK), p)
        self.assertIs(dashboard.probe, guard._probe)
        self.assertTrue(guard._inside(Path.home(), guard.HOME))


# ---------- S21 ----------

TEAM, AUD, EMAIL = 'example-team.cloudflareaccess.com', 'FAKE_AUD_0123456789', 'allowed@example.com'
DROP = object()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _jwk(key, kid):
    n = key.public_key().public_numbers()
    return {'kty': 'RSA', 'kid': kid, 'alg': 'RS256', 'use': 'sig', 'e': _b64(n.e.to_bytes(3, 'big')),
            'n': _b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, 'big'))}


def _token(key, kid='key-1', head=None, **claims):
    now = int(time.time())
    body = {'aud': [AUD], 'iss': f'https://{TEAM}', 'email': EMAIL, 'iat': now, 'nbf': now - 5, 'exp': now + 600,
            **claims}
    body = {k: v for k, v in body.items() if v is not DROP}
    signing = (f'{_b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT", **(head or {})}).encode())}.'
               f'{_b64(json.dumps(body).encode())}')
    return f'{signing}.{_b64(key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))}'


class _Certs:
    """requests.get for the Access key page: answers the keys in .jwks and counts the calls."""

    def __init__(self, *jwks):
        self.jwks, self.calls, self.fail = list(jwks), [], None

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if self.fail:
            raise self.fail
        if url != f'https://{TEAM}/cdn-cgi/access/certs':
            raise AssertionError(url)
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'keys': list(self.jwks)})


class AccessEverywhere(Case):
    """S21: outside --dev every GET (page, static file, cover, API, unknown path) and every POST needs a valid Access
    JWT for an allowed email: 401 without one, 403 with a bad one, 503 when the keys cannot be read. The Access
    class is v1's; its keys are made here."""

    @classmethod
    def setUpClass(cls):
        guard.reset()
        cls.key, cls.other = (rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))
        cls.certs = _Certs(_jwk(cls.key, 'key-1'))
        dashboard.CONFIG.write_text(json.dumps({'team': TEAM, 'aud': AUD, 'emails': [' Allowed@Example.com ']}),
                                    encoding='utf-8')
        cls.patches = [mock.patch.object(dashboard.requests, 'get', cls.certs),
                       mock.patch.object(dashboard, 'drive_root', lambda: None)]      # never the real Drive
        for p in cls.patches:
            p.start()
        with contextlib.redirect_stdout(io.StringIO()):
            cls.server = Server()
        cls.access = dashboard.Handler.access

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        for p in cls.patches:
            p.stop()
        _handler_defaults()

    def setUp(self):
        super().setUp()
        dashboard.Handler.access, dashboard.Handler.limiter = self.access, dashboard.Limiter()
        publish.set_registry({'instagram': platform_fake.FakePlatform([], records=guard.WORK / 'fake.json')})
        self.certs.jwks, self.certs.fail = [_jwk(self.key, 'key-1')], None
        self.access.keys, self.access.fetched = {}, 0
        self.access.fetch()
        self.certs.calls.clear()                                            # count each test's own fetches

    def paths(self):
        return [*dashboard.STATIC, '/logo.png', '/api/home', '/api/publish?platform=instagram',
                f'/api/preview?platform=instagram&id={ID[6]}', '/api/job?id=j0123456789', '/api/videos', '/api/stats',
                '/api/activity?kind=all', f'/api/cover/instagram/{ID[6]}.jpg',
                f'/api/cover/instagram/{ID[6]}.jpg?download=1', *dashboard.GONE, '/nothing', '/..%2fdashboard.py']

    def actions(self):
        """Every action a POST can start, each a mock that fails the test if called."""
        names = ('start_post', 'queue_op', 'save_settings', 'reset_settings', 'resume_auto', 'test_platform',
                 'resolve_job')
        mocks = {n: self.enterContext(mock.patch.object(publish, n, side_effect=AssertionError(n))) for n in names}
        mocks['retry'] = self.enterContext(mock.patch.object(dashboard, 'retry_lesson', side_effect=AssertionError))
        return mocks

    def test_no_token_401_everywhere(self):
        for path in self.paths():
            with self.subTest(path=path):
                a = self.server.call('GET', path)
                self.assertError(a, 401, 'not_signed_in')
                self.assertSecure(a)

    def test_posts_need_a_token(self):
        acts = self.actions()
        for path in [*dashboard.Handler.POSTS, '/api/action']:
            with self.subTest(path=path):
                self.assertError(self.server.call('POST', path, {'platform': 'instagram', 'lesson': 'new'}), 401,
                                 'not_signed_in')
        self.assertFalse(any(m.called for m in acts.values()))

    def test_valid_token(self):
        a = self.server.call('GET', '/api/home', headers={'Cf-Access-Jwt-Assertion': _token(self.key)})
        self.assertEqual(a.status, 200, a.data[:300])
        self.assertEqual(a.json['me'], EMAIL)                               # the config's spelling does not matter
        page = self.server.call('GET', '/', headers={'Cf-Access-Jwt-Assertion': _token(self.key, email='ALLOWED@example.com')})
        self.assertEqual((page.status, page.headers['Content-Type']), (200, 'text/html; charset=utf-8'))

    def test_refused_tokens_403_everywhere(self):
        now = int(time.time())
        bad = {'wrong aud': _token(self.key, aud=['FAKE_OTHER_APP']), 'aud missing': _token(self.key, aud=DROP),
               'wrong iss': _token(self.key, iss='https://other-team.cloudflareaccess.com'),
               'expired': _token(self.key, exp=now - 120), 'not yet valid': _token(self.key, nbf=now + 600),
               'unknown kid': _token(self.other, kid='key-2'), 'email not allowed': _token(self.key, email='x@example.com'),
               'no email': _token(self.key, email=DROP), 'wrong key, known kid': _token(self.other, kid='key-1'),
               'alg none': _token(self.key, head={'alg': 'none'}), 'alg HS256': _token(self.key, head={'alg': 'HS256'}),
               'garbage': 'not-a-token', 'two parts': 'abc.def', 'tampered': _token(self.key)[:-6] + 'AAAAAA'}
        for why, token in bad.items():
            for path in ('/api/home', '/app.js', f'/api/cover/instagram/{ID[6]}.jpg'):
                with self.subTest(why=why, path=path):
                    a = self.server.call('GET', path, headers={'Cf-Access-Jwt-Assertion': token})
                    self.assertError(a, 403, 'not_allowed')
        self.assertEqual(self.certs.calls, [])                              # unknown kid: no refetch within a minute

    def test_unknown_kid_refetches_once_a_minute(self):
        self.certs.jwks.append(_jwk(self.other, 'key-2'))                  # Cloudflare rotated its keys
        token = {'Cf-Access-Jwt-Assertion': _token(self.other, kid='key-2')}
        self.assertError(self.server.call('GET', '/api/home', headers=token), 403, 'not_allowed')
        self.assertEqual(self.certs.calls, [])                              # fetched less than a minute ago
        self.access.fetched = time.time() - 61
        self.assertEqual(self.server.call('GET', '/api/home', headers=token).status, 200)
        self.assertEqual(len(self.certs.calls), 1)

    def test_key_page_down_503(self):
        self.access.fetched, self.certs.fail = 0, ConnectionError('down')
        a = self.server.call('GET', '/api/home', headers={'Cf-Access-Jwt-Assertion': _token(self.other, kid='key-9')})
        self.assertError(a, 503, 'login_unavailable')

    def test_F25_key_page_down_then_back_no_403(self):
        """After a failed key-page read a new key id gets 503 (never 403 'signed out'); 10 s later it loads."""
        self.access.fetched = time.time() - 120
        self.certs.jwks.append(_jwk(self.other, 'key-2'))                  # Cloudflare rotated its keys...
        self.certs.fail = ConnectionError('down')                          # ...while the PC cannot reach the page
        new = {'Cf-Access-Jwt-Assertion': _token(self.other, kid='key-2')}
        self.assertError(self.server.call('GET', '/api/home', headers=new), 503, 'login_unavailable')
        self.certs.fail = None                                             # the key page is back
        self.assertError(self.server.call('GET', '/api/home', headers=new), 503, 'login_unavailable')   # not 403
        self.assertEqual(self.server.call('GET', '/api/home',
                                          headers={'Cf-Access-Jwt-Assertion': _token(self.key)}).status, 200)
        self.access.fetched -= 11                                          # past the 10 s pause
        self.assertEqual(self.server.call('GET', '/api/home', headers=new).status, 200)
        self.assertEqual(len(self.certs.calls), 2)

    def test_no_login_check_outside_dev_503(self):
        dashboard.Handler.access = None
        self.assertError(self.server.call('GET', '/api/home'), 503, 'login_unavailable')

    def test_refuses_to_start_without_the_login_check(self):
        for cfg in ({'team': TEAM, 'emails': [EMAIL]}, {'team': TEAM, 'aud': AUD, 'emails': []}, None):
            with self.subTest(cfg=cfg), mock.patch.object(dashboard, '_Server',
                                                          side_effect=AssertionError('started')):
                if cfg is None:
                    dashboard.CONFIG.unlink(missing_ok=True)
                else:
                    dashboard.CONFIG.write_text(json.dumps(cfg), encoding='utf-8')
                with self.assertRaisesRegex(SystemExit, 'refusing to start without the login check'):
                    dashboard.make_server(0)


# ---------- S20 and the limiter ----------

class PostGuards(DevCase):
    """S20: a POST without every spec 8.1 guard gets 403, 413 or 429 (or 400 for a body that is not a JSON object),
    and the action never runs."""

    def actions(self):
        names = ('start_post', 'queue_op', 'save_settings', 'reset_settings', 'resume_auto', 'test_platform',
                 'resolve_job')
        mocks = {n: self.enterContext(mock.patch.object(publish, n, return_value={'ok': True, 'message': 'ran'}))
                 for n in names}
        mocks['retry'] = self.enterContext(mock.patch.object(dashboard, 'retry_lesson',
                                                             return_value={'ok': True, 'message': 'ran'}))
        return mocks

    BODIES = {'/api/post': {'platform': 'instagram', 'id': ID[6], 'caption': 'x', 'check': '0' * 16},
              '/api/queue': {'platform': 'instagram', 'op': 'top', 'id': ID[6], 'rev': 0},
              '/api/settings': {'platform': 'instagram', 'rev': 1, 'settings': {}},
              '/api/settings/reset': {'ack': ['reset_settings']},
              '/api/auto/resume': {'platform': 'instagram', 'ack': ['resume_auto']},
              '/api/platform/test': {'platform': 'instagram'},
              '/api/job/resolve': {'job': 'j0123456789', 'outcome': 'check'},
              '/api/lesson/retry': {'lesson': 'new'}}

    def test_every_post_path_is_listed(self):
        self.assertEqual(set(self.BODIES), set(dashboard.Handler.POSTS))

    def test_refused_403(self):
        port = self.server.port
        bad = {'no X-Dashboard': {'X-Dashboard': None}, 'X-Dashboard 0': {'X-Dashboard': '0'},
               'X-Dashboard true': {'X-Dashboard': 'true'}, 'no Content-Type': {'Content-Type': None},
               'text/plain': {'Content-Type': 'text/plain'},
               'json named in a parameter': {'Content-Type': 'text/plain; x=application/json'},
               'form': {'Content-Type': 'application/x-www-form-urlencoded'},
               'other site': {'Origin': 'https://evil.example'}, 'null origin': {'Origin': 'null'},
               'empty origin': {'Origin': ''}, 'other port': {'Origin': f'http://127.0.0.1:{port + 1}'},
               'https with a path': {'Origin': f'https://127.0.0.1:{port}/x'},
               'cross-site': {'Sec-Fetch-Site': 'cross-site'}, 'same-site': {'Sec-Fetch-Site': 'same-site'},
               'none': {'Sec-Fetch-Site': 'none'}}
        acts = self.actions()
        for why, headers in bad.items():
            for path, body in self.BODIES.items():
                with self.subTest(why=why, path=path):
                    self.assertError(self.post(path, body, headers=headers), 403, 'refused')
            dashboard.Handler.limiter = dashboard.Limiter()                 # refusals are not what is tested here
        self.assertEqual([n for n, m in acts.items() if m.called], [])
        self.assertIn('refused', (guard.WORK / 'dashboard.log').read_text(encoding='utf-8'))

    def test_guards_pass(self):
        acts = self.actions()
        a = self.post('/api/settings/reset', {'ack': ['reset_settings']}, headers={'Content-Type': 'application/json; charset=utf-8'})
        self.assertEqual((a.status, a.json), (200, {'ok': True, 'message': 'ran'}))
        self.assertEqual(self.post('/api/platform/test', {'platform': 'instagram'},
                                   headers={'Origin': None, 'Sec-Fetch-Site': None}).status, 200)
        self.assertEqual([n for n, m in acts.items() if m.called], ['reset_settings', 'test_platform'])
        self.assertEqual(acts['reset_settings'].call_args.args, (['reset_settings'], 'dev@localhost'))

    def test_too_large_413_unread(self):
        acts = self.actions()
        for path in self.BODIES:
            with self.subTest(path=path):
                self.assertError(self.server.announced(path, dashboard.BODY_MAX + 1), 413, 'too_large')
        self.assertError(self.server.announced('/api/settings', 10 ** 12), 413, 'too_large')
        big = json.dumps({'platform': 'instagram', 'pad': 'x' * dashboard.BODY_MAX}).encode()
        self.assertError(self.post('/api/platform/test', raw=big), 413, 'too_large')
        self.assertFalse(any(m.called for m in acts.values()))

    def test_not_a_json_object_400(self):
        acts = self.actions()
        for why, raw in {'not JSON': b'platform=instagram', 'array': b'[]', 'string': b'"x"', 'number': b'5',
                         'null': b'null', 'bad UTF-8': b'{"platform": "\xff"}', 'empty': b'',
                         'deep': b'[' * 100000 + b']' * 100000}.items():
            with self.subTest(why=why):
                self.assertError(self.post('/api/platform/test', raw=raw[:dashboard.BODY_MAX]), 400, 'bad_request')
        self.assertError(self.post('/api/platform/test', raw=b'{}', headers={'Content-Length': '+2'}), 400,
                         'bad_request')
        self.assertFalse(any(m.called for m in acts.values()))

    def test_rate_limited_429(self):
        acts = self.actions()
        for n in range(20):
            self.assertEqual(self.post('/api/platform/test', {'platform': 'instagram'}).status, 200, n)
        a = self.post('/api/platform/test', {'platform': 'instagram'})
        body = self.assertError(a, 429, 'rate_limited')
        self.assertIsInstance(body['retry_after'], int)
        self.assertTrue(1 <= body['retry_after'] <= 60)
        self.assertError(self.post('/api/lesson/retry', {'lesson': 'new'}), 429, 'rate_limited')   # every POST
        self.assertEqual(acts['test_platform'].call_count, 20)
        self.assertFalse(acts['retry'].called)
        self.assertEqual(self.get('/api/home').status, 200)                # GETs are not limited


class LimiterRules(unittest.TestCase):
    """dashboard.Limiter: a sliding window per key; refusals do not count."""

    def setUp(self):
        guard.reset()

    def test_window(self):
        lim = dashboard.Limiter(3, 60)
        self.assertEqual([lim.allow('a', t) for t in (0, 1, 2, 3, 30)], [True, True, True, False, False])
        self.assertEqual(lim.retry_after('a', 3), 57)
        self.assertTrue(lim.allow('b', 3))                                  # per key
        self.assertFalse(lim.allow('a', 59.9))
        self.assertTrue(lim.allow('a', 60.0))                               # t=0 left the window
        self.assertFalse(lim.allow('a', 60.5))
        self.assertTrue(lim.allow('a', 61.0))
        self.assertEqual(lim.retry_after('c', 0), 0)

    def test_defaults(self):
        lim = dashboard.Limiter()
        self.assertEqual((lim.n, lim.window), (20, 60))
        self.assertEqual(sum(lim.allow('x', 0.5 * k) for k in range(40)), 20)
        self.assertIsInstance(dashboard.Handler.limiter, dashboard.Limiter)

    def test_threads(self):
        lim, results = dashboard.Limiter(20, 60), []
        threads = [threading.Thread(target=lambda: results.append(lim.allow('x'))) for _ in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual((len(results), sum(results)), (60, 20))


# ---------- the endpoints (spec 8.3) ----------

class Endpoints(DevCase):
    """Every endpoint's keys and types against the spec 8.3 examples, on --dev --fixtures."""

    def test_home(self):
        a = self.get('/api/home')
        self.assertEqual(a.status, 200)
        self.assertSecure(a)
        self.assertEqual(a.headers['Content-Type'], 'application/json; charset=utf-8')
        d = a.json
        conforms(self, d, HOME)
        self.assertEqual(d['me'], 'dev@localhost')
        self.assertEqual(d['channel'], {'name': dashboard.CHANNEL_NAME, 'url': IG.CHANNEL})
        self.assertEqual(d['now'], S.iso(NOW))
        self.assertEqual([t['id'] for t in d['tiles']], list(TILES))
        self.assertEqual(d['counts'], {'instagram': {'waiting': 6, 'skipped': 0, 'failed': 0, 'posted': 5},
                                       'youtube': {'ready': 1, 'uploaded': 2, 'scheduled': 2, 'public': 8}})
        self.assertEqual(d['health']['platforms'], [{'name': 'instagram', 'ok': True, 'set_up': True,
                                                     'breaker': 'closed', 'text': 'Instagram key OK'},
                                                    {'name': 'youtube', 'ok': False, 'set_up': False, 'breaker': None, 'text': 'YouTube not available'}])     # in PLATFORMS, not in the test registry

    def test_publish(self):
        a = self.get('/api/publish?platform=instagram')
        self.assertEqual(a.status, 200, a.data[:300])
        d = a.json
        conforms(self, d, PUBLISH)
        self.assertEqual([i['key'] for i in d['queue']['waiting']], list(fixtures.WAITING))
        self.assertEqual((d['counts']['waiting'], d['counts']['posted'], d['queue']['posted_total']), (6, 5, 5))
        self.assertEqual(d['queue']['waiting'][0]['cover'], f'/api/cover/instagram/{ID[6]}.jpg')

    def test_preview(self):
        a = self.get(f'/api/preview?platform=instagram&id={ID[6]}')
        self.assertEqual(a.status, 200, a.data[:300])
        conforms(self, a.json, PREVIEW)
        self.assertEqual({c['code'] for c in a.json['checks'] if c['level'] == 'warn'}, {'gap', 'same_day'})

    def test_post_and_job(self):
        """The owner's regression over HTTP: pause flag on, the last Reel 5 h 54 min ago: the manual post goes out."""
        self.pause()
        pv = self.get(f'/api/preview?platform=instagram&id={ID[6]}').json
        warns = [c['code'] for c in pv['checks'] if c['level'] == 'warn']
        a = self.post('/api/post', {'platform': 'instagram', 'id': ID[6], 'caption': pv['caption'],
                                    'save_caption': False, 'check': pv['check'], 'override': warns})
        self.assertEqual(a.status, 202, a.data[:300])
        conforms(self, a.json, {'ok': True, 'message': str, 'job': JOB})
        done = publish._wait(a.json['job']['id'])
        self.assertEqual(done['state'], 'done', done)
        j = self.get(f'/api/job?id={done["id"]}')
        self.assertEqual(j.status, 200)
        conforms(self, j.json, {'job': JOB})
        self.assertEqual((j.json['job']['by'], j.json['job']['who']), ('manual', 'dev@localhost'))
        self.assertEqual([c[0] for c in self.fake.calls], ['post'])
        home = self.get('/api/home').json
        self.assertEqual(home['counts']['instagram'], {'waiting': 5, 'skipped': 0, 'failed': 0, 'posted': 6})
        self.assertTrue(S.pause_file('instagram').exists())

    def test_post_refusals(self):
        pv = self.get(f'/api/preview?platform=instagram&id={ID[6]}').json
        body = {'platform': 'instagram', 'id': ID[6], 'caption': pv['caption'], 'check': pv['check']}
        self.assertEqual(self.post('/api/post', body).status, 428)          # the warn checks need the override
        for bad in ({'caption': None}, {'caption': 5}, {'check': 'nope'}, {'check': None}, {'save_caption': 'yes'},
                    {'override': 'gap'}, {'override': ['gap'] * 11}, {'override': [1]}, {'id': '../../x'},
                    {'platform': 'Instagram'}, {'platform': None}):
            with self.subTest(bad=bad):
                self.assertError(self.post('/api/post', {**body, **bad}), 400, 'bad_request')
        self.assertError(self.post('/api/post', {**body, 'platform': 'tiktok'}), 404, 'unknown_platform')
        self.assertError(self.post('/api/post', {**body, 'id': publish.item_id(ZU)}), 404, 'unknown_item')
        self.assertEqual(self.fake.calls, [])

    def test_queue(self):
        a = self.post('/api/queue', {'platform': 'instagram', 'op': 'top', 'id': ID[9], 'rev': 0})
        self.assertEqual(a.status, 200, a.data[:300])
        conforms(self, a.json, {'ok': True, 'message': str, 'queue': QUEUE})
        self.assertEqual(a.json['queue']['waiting'][0]['id'], ID[9])
        stale = self.assertError(self.post('/api/queue', {'platform': 'instagram', 'op': 'untop', 'id': ID[9], 'rev': 0}),
                                 409, 'stale')
        conforms(self, stale['queue'], QUEUE)
        self.assertError(self.post('/api/queue', {'platform': 'instagram', 'op': ['top'], 'rev': 1}), 422, 'bad_op')
        self.assertError(self.post('/api/queue', {'platform': 'instagram', 'op': 'explode', 'rev': 1}), 422, 'bad_op')
        self.assertError(self.post('/api/queue', {'platform': 'instagram', 'op': 'top', 'id': 'x', 'rev': 1}), 400,
                         'bad_request')

    def test_settings(self):
        rev = self.get('/api/publish?platform=instagram').json['settings_rev']
        dry = self.post('/api/settings', {'platform': 'instagram', 'rev': rev, 'settings': {'post_after': '19:00'},
                                          'dry_run': True})
        self.assertEqual(dry.status, 200, dry.data[:300])
        conforms(self, dry.json, {'ok': True, 'dry_run': True, 'settings': SETTINGS, 'forecast': FORECAST,
                                  'needs_ack': [str]})
        a = self.post('/api/settings', {'platform': 'instagram', 'rev': rev, 'settings': {'post_after': '19:30'}})
        self.assertEqual(a.status, 200, a.data[:300])
        conforms(self, a.json, {'ok': True, 'message': str, 'settings': SETTINGS, 'rev': int, 'auto': AUTO,
                                'forecast': FORECAST})
        self.assertEqual((a.json['settings']['post_after'], a.json['rev']), ('19:30', rev + 1))
        self.assertError(self.post('/api/settings', {'platform': 'instagram', 'rev': rev, 'settings': {}}), 409, 'stale')
        for bad in ({'ack': 'auto_on'}, {'ack': [1]}, {'dry_run': 1}):
            with self.subTest(bad=bad):
                self.assertError(self.post('/api/settings', {'platform': 'instagram', 'rev': rev + 1, 'settings': {},
                                                             **bad}), 400, 'bad_request')

    def test_settings_reset(self):
        self.assertError(self.post('/api/settings/reset', {}), 428, 'confirm')
        self.assertError(self.post('/api/settings/reset', {'ack': 'reset_settings'}), 400, 'bad_request')
        a = self.post('/api/settings/reset', {'ack': ['reset_settings']})
        self.assertEqual(a.status, 200, a.data[:300])
        conforms(self, a.json, {'ok': True, 'message': str, 'settings': SETTINGS, 'rev': int})
        self.assertFalse(a.json['settings']['auto'])

    def test_auto_resume(self):
        self.assertError(self.post('/api/auto/resume', {'platform': 'instagram', 'ack': ['resume_auto']}), 409,
                         'not_suspended')
        self.assertError(self.post('/api/auto/resume', {'platform': 'nope!', 'ack': []}), 400, 'bad_request')

    def test_platform_test(self):
        a = self.post('/api/platform/test', {'platform': 'instagram'})
        self.assertEqual(a.status, 200, a.data[:300])
        conforms(self, a.json, {'ok': True, 'message': str, 'result': {'account': str, 'followers': int, 'media': int,
                                                                        'limit': {'used': int, 'total': int}},
                                'breaker': BREAKER})
        self.assertIsInstance(self.assertError(self.post('/api/platform/test', {'platform': 'instagram'}), 429,
                                               'rate_limited')['retry_after'], int)
        self.fake.ready = False
        self.assertError(self.post('/api/platform/test', {'platform': 'instagram'}), 409, 'not_set_up')

    def test_platform_test_failed_502(self):
        self.fake.test_error = 'auth'
        body = self.assertError(self.post('/api/platform/test', {'platform': 'instagram'}), 502, 'test_failed')
        self.assertEqual(body['kind'], 'auth')
        conforms(self, body['breaker'], BREAKER)

    def test_job_resolve(self):
        self.assertError(self.post('/api/job/resolve', {'job': 'j0123456789', 'outcome': 'check'}), 404, 'unknown_job')
        self.assertError(self.post('/api/job/resolve', {'job': 'J0123456789', 'outcome': 'check'}), 400, 'bad_request')
        self.assertError(self.get('/api/job?id=j0123456789'), 404, 'unknown_job')
        self.assertError(self.get('/api/job?id=nope'), 400, 'bad_request')
        self.assertError(self.get('/api/job'), 400, 'bad_request')

    def test_error_bodies(self):
        """Dev A's request: every PublishError is answered {'error', 'message', **extra}, 503 write_failed and 409
        busy included, from GET and POST routes alike."""
        failed = publish.PublishError(503, 'write_failed', 'The PC could not save that. Try again.', retry_after=5)
        with mock.patch.object(publish, 'queue_op', side_effect=failed):
            a = self.post('/api/queue', {'platform': 'instagram', 'op': 'skip', 'id': ID[9], 'rev': 0})
        self.assertEqual((a.status, a.json), (503, {'error': 'write_failed', 'retry_after': 5,
                                                    'message': 'The PC could not save that. Try again.'}))
        with mock.patch.object(publish, 'state', side_effect=failed):
            self.assertEqual(self.get('/api/publish?platform=instagram').json['error'], 'write_failed')
        pv = self.get(f'/api/preview?platform=instagram&id={ID[6]}').json
        with mock.patch.object(publish, 'start_post', side_effect=publish.Busy(None)):
            a = self.post('/api/post', {'platform': 'instagram', 'id': ID[6], 'caption': pv['caption'],
                                        'check': pv['check']})
        self.assertEqual((a.status, a.json), (409, {'error': 'busy', 'message': 'Another post is going out right now.',
                                                    'job': None}))
        self.assertEqual(self.fake.calls, [])

    def test_lesson_retry_refusals(self):
        self.assertError(self.post('/api/lesson/retry', {'lesson': 'new'}), 409, 'not_stopped')
        self.assertError(self.post('/api/lesson/retry', {'lesson': 'No such one'}), 400, 'no_such_lesson')

    def test_videos(self):
        a = self.get('/api/videos')
        self.assertEqual(a.status, 200, a.data[:300])
        d = a.json
        conforms(self, d, VIDEOS)
        for v in d['videos']:
            conforms(self, v['reel'], REEL, f'$.videos[{v.get("file")}].reel')
            self.assertTrue({'lesson', 'kind', 'state', 'time'} <= set(v), v)
        self.assertEqual(len(d['videos']), 13)
        self.assertEqual(d['channel']['name'], dashboard.CHANNEL_NAME)

    def test_stats(self):
        empty = self.get('/api/stats')                                      # nothing collected yet: nulls
        self.assertEqual(empty.status, 200, empty.data[:300])
        conforms(self, empty.json, STATS)
        self.assertEqual(len(empty.json['tiles']), 4)
        self.assertEqual(dashboard.tick(reconcile=False)['stats'], dict.fromkeys(dashboard.stats.EVERY, 'ok'))
        self.assertEqual(self.fake.calls, [('read_stats', ()), ('read_limit', ()), ('read_permissions', ())])
        d = self.get('/api/stats').json                                     # the fake YouTube's and platform's numbers
        conforms(self, d, STATS)
        self.assertTrue(d['youtube_videos'] and d['insights'] and d['limit'], d)
        self.assertEqual({t['id']: t['value'] for t in d['tiles'] if t['id'] != 'ig_likes'},
                         {'yt_subscribers': 143, 'yt_views': 5230, 'ig_followers': 71})
        home = self.get('/api/home').json
        self.assertEqual(home['health']['stats'], {'ok': True, 'at': S.iso(NOW), 'text': 'Numbers just now'})

    def test_activity(self):
        (guard.WORK / 'watch.log').write_text('==== Wed 11/04/2026 22:30:01.12\nwatch line\n', encoding='utf-8')
        self.post('/api/queue', {'platform': 'instagram', 'op': 'skip', 'id': ID[9], 'rev': 0})
        a = self.get('/api/activity?kind=all')
        self.assertEqual(a.status, 200, a.data[:300])
        conforms(self, a.json, ACTIVITY)
        self.assertEqual(a.json['logs']['watch'], ['==== Wed 11/04/2026 22:30:01.12', 'watch line'])
        self.assertIn('queue.skip', [i['kind'] for i in a.json['items']])
        before = self.get(f'/api/activity?kind=posts&before={a.json["items"][0]["id"]}')
        self.assertEqual(before.status, 200)
        self.assertEqual(sorted(before.json), ['items', 'more'])               # with before: no logs
        self.assertError(self.get('/api/activity?kind=everything'), 400, 'bad_request')
        self.assertError(self.get('/api/activity?before=abc'), 400, 'bad_request')
        self.assertError(self.get('/api/activity?before=-1'), 400, 'bad_request')

    def test_cover(self):
        a = self.get(f'/api/cover/instagram/{ID[6]}.jpg')
        self.assertEqual(a.status, 200, a.data[:300])
        self.assertSecure(a, cache='private, max-age=3600')
        self.assertEqual(a.headers['Content-Type'], 'image/jpeg')
        self.assertRegex(a.headers['ETag'], r'^"[0-9a-f]{24}"$')
        self.assertIsNone(a.headers.get('Content-Disposition'))
        self.assertEqual(Image.open(io.BytesIO(a.data)).format, 'JPEG')
        again = self.get(f'/api/cover/instagram/{ID[6]}.jpg', headers={'If-None-Match': a.headers['ETag']})
        self.assertEqual((again.status, again.data), (304, b''))
        self.assertEqual(again.headers['ETag'], a.headers['ETag'])
        full = self.get(f'/api/cover/instagram/{ID[6]}.jpg?download=1')
        self.assertEqual(full.status, 200)
        self.assertEqual(full.headers['Content-Disposition'], 'attachment; filename="cover-short-6.jpg"')

    def test_parameters_400_and_404(self):
        for path in ('/api/publish', '/api/publish?platform=', '/api/publish?platform=Instagram',
                     '/api/publish?platform=x', '/api/publish?platform=instagram%0a', '/api/preview?platform=instagram',
                     f'/api/preview?platform=instagram&id={ID[6].upper()}', '/api/preview?platform=instagram&id=../../x',
                     f'/api/preview?id={ID[6]}', '/api/home?' + '&'.join(f'k{n}=1' for n in range(30))):
            with self.subTest(path=path):
                self.assertError(self.get(path), 400, 'bad_request')
        self.assertError(self.get('/api/publish?platform=tiktok'), 404, 'unknown_platform')
        self.assertError(self.get(f'/api/preview?platform=instagram&id={publish.item_id(ZU)}'), 404, 'unknown_item')
        self.assertError(self.get('/api/nothing'), 404, 'not_found')
        self.assertError(self.post('/api/nothing', {}), 404, 'not_found')

    def test_drive_missing(self):
        dashboard.Handler.root = None
        for path in ('/api/publish?platform=instagram', f'/api/preview?platform=instagram&id={ID[6]}', '/api/videos',
                     f'/api/cover/instagram/{ID[6]}.jpg'):
            with self.subTest(path=path):
                self.assertError(self.get(path), 503, 'drive_missing')
        for path, body in (('/api/post', {'platform': 'instagram', 'id': ID[6], 'caption': '', 'check': '0' * 16}),
                           ('/api/queue', {'platform': 'instagram', 'op': 'top', 'id': ID[6], 'rev': 0}),
                           ('/api/job/resolve', {'job': 'j0123456789', 'outcome': 'check'}),
                           ('/api/lesson/retry', {'lesson': 'new'})):
            with self.subTest(path=path):
                self.assertError(self.post(path, body), 503, 'drive_missing')
        home = self.get('/api/home')
        self.assertEqual(home.status, 200)
        conforms(self, home.json, {**HOME, 'counts': {}})
        self.assertIn('drive_missing', [c['id'] for c in home.json['attention']])
        self.assertEqual(self.get('/api/stats').status, 200)
        self.assertEqual(self.get('/api/activity').status, 200)
        rev = publish.settings().doc['rev']                                 # settings need no Drive
        self.assertEqual(self.post('/api/settings', {'platform': 'instagram', 'rev': rev, 'settings': {},
                                                     'dry_run': True}).status, 200)


class FixRound(DevCase):
    """Review findings of the fix round (F8, F9, F14, F24, F26, F41, G13, G22), over HTTP on --dev --fixtures."""

    def settings(self, rev, patch, dry=False):
        return self.post('/api/settings', {'platform': 'instagram', 'rev': rev, 'settings': patch, 'dry_run': dry})

    def test_F8_dry_runs_do_not_use_up_the_press_budget(self):
        rev = self.get('/api/publish?platform=instagram').json['settings_rev']
        for n in range(25):                                                 # the sheet's live forecast
            self.assertEqual(self.settings(rev, {'min_gap_hours': 20 + n / 2}, dry=True).status, 200, n)
        a = self.settings(rev, {'min_gap_hours': 30})
        self.assertEqual((a.status, a.json.get('rev')), (200, rev + 1), a.data[:300])
        for n in range(20):
            self.assertEqual(self.settings(rev + 1, {'auto': False}, dry=True).status, 200, n)
        self.assertEqual(self.settings(rev + 1, {'auto': False}).status, 200)     # Turn off right after

    def test_F8_dry_runs_have_a_budget_too(self):
        rev = self.get('/api/publish?platform=instagram').json['settings_rev']
        with mock.patch.object(dashboard.Handler, 'dry_limiter', dashboard.Limiter(3, 60)):
            for n in range(3):
                self.assertEqual(self.settings(rev, {'post_after': '19:00'}, dry=True).status, 200)
            body = self.assertError(self.settings(rev, {'post_after': '19:00'}, dry=True), 429, 'rate_limited')
            self.assertRegex(body['message'], r'^Wait \d+ s before trying again\.$')
            self.assertEqual(self.settings(rev, {'post_after': '19:30'}).status, 200)     # a real save still goes
        for n in range(20):                                                 # real presses: still 20 a minute
            self.post('/api/platform/test', {'platform': 'nope!'})
        self.assertError(self.settings(rev + 1, {}), 429, 'rate_limited')

    def test_F14_the_wait_is_named(self):
        self.assertEqual(self.post('/api/platform/test', {'platform': 'instagram'}).status, 200)
        body = self.assertError(self.post('/api/platform/test', {'platform': 'instagram'}), 429, 'rate_limited')
        self.assertEqual(body['retry_after'], 600)
        self.assertEqual(body['message'], 'Instagram can be tested once every 10 minutes. Wait 10 min before testing '
                                          f'again (until {S.local(NOW + timedelta(minutes=10)):%H:%M}).')

    def test_F14_wait_text(self):
        at = f'{S.local(NOW + timedelta(seconds=95)):%H:%M}'
        for (msg, wait), want in ((('Wait a moment before trying again.', 45), 'Wait 45 s before trying again.'),
                                  (('Wait a moment before posting again.', 95),
                                   f'Wait 2 min before posting again (until {at}).'),
                                  (('Too many calls.', 30), 'Too many calls. Try again in 30 s.'),
                                  (('Test again at 20:15 (in 5 min).', 300), 'Test again at 20:15 (in 5 min).')):
            self.assertEqual(dashboard.wait_text(msg, wait, NOW), want)
        failed = publish.PublishError(429, 'rate_limited', 'Wait a moment before checking again.', retry_after=300)
        with mock.patch.object(publish, 'resolve_job', side_effect=failed):
            body = self.assertError(self.post('/api/job/resolve', {'job': 'j0123456789', 'outcome': 'check'}), 429,
                                    'rate_limited')
        self.assertEqual(body['message'], 'Wait 5 min before checking again '
                                          f'(until {S.local(NOW + timedelta(minutes=5)):%H:%M}).')

    def test_F24_foreign_host_refused(self):
        port = self.server.port
        for host in (f'evil.example:{port}', 'evil.example', f'127.0.0.1:{port + 1}', '', f'127.0.0.1.evil.example:{port}'):
            with self.subTest(host=host):
                for path in ('/api/home', '/', f'/api/cover/instagram/{ID[6]}.jpg'):
                    self.assertError(self.get(path, headers={'Host': host}), 403, 'refused')
                a = self.post('/api/settings', {'platform': 'instagram', 'rev': 1, 'settings': {'post_after': '21:30'}},
                              headers={'Host': host, 'Origin': f'http://{host}', 'Sec-Fetch-Site': 'same-origin'})
                self.assertError(a, 403, 'refused')
        self.assertEqual(self.get('/api/publish?platform=instagram').json['settings']['post_after'], '20:00')

    def test_F24_own_names_pass(self):
        port = self.server.port
        rev = self.get('/api/publish?platform=instagram').json['settings_rev']
        for host in (f'127.0.0.1:{port}', f'localhost:{port}', f'LOCALHOST:{port}'):
            with self.subTest(host=host):
                self.assertEqual(self.get('/api/home', headers={'Host': host}).status, 200)
                a = self.post('/api/settings', {'platform': 'instagram', 'rev': rev, 'settings': {}, 'dry_run': True},
                              headers={'Host': host, 'Origin': f'http://{host}'})
                self.assertEqual(a.status, 200, a.data[:300])

    def test_F26_unexpected_error_is_logged(self):
        with mock.patch.object(publish, 'queue_op', side_effect=sqlite3.OperationalError('database is locked')):
            self.assertError(self.post('/api/queue', {'platform': 'instagram', 'op': 'skip', 'id': ID[9], 'rev': 0}),
                             500, 'server_error')
        self.assertIn('dev@localhost POST /api/queue 500 server_error (OperationalError: database is locked)',
                      (guard.WORK / 'dashboard.log').read_text(encoding='utf-8'))

    def test_F41_broken_stats_db(self):
        with mock.patch.object(dashboard.stats, 'summary', side_effect=sqlite3.DatabaseError('file is not a database')):
            a = self.get('/api/stats')
        self.assertEqual(a.status, 200, a.data[:300])
        conforms(self, a.json, STATS)
        self.assertEqual({s['source'] for s in a.json['sources']}, set(dashboard.stats.EVERY))
        self.assertTrue(all(s['fails'] == 1 and 'not a database' in s['error'] for s in a.json['sources']))
        db = dashboard.stats._db()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(os.urandom(65536))                                   # a damaged file on the disk
        a = self.get('/api/stats')
        self.assertEqual(a.status, 200, a.data[:300])
        conforms(self, a.json, STATS)
        self.assertEqual(self.get('/api/home').status, 200)

    def test_G13_covers_scan_once(self):
        ids = [ID[n] for n in (6, 7, 8, 9)]
        with mock.patch.object(self.fake, 'candidates', wraps=self.fake.candidates) as scans:
            first = {i: self.get(f'/api/cover/instagram/{i}.jpg') for i in ids}
            for i in ids:
                again = self.get(f'/api/cover/instagram/{i}.jpg')
                self.assertEqual((again.status, again.data), (200, first[i].data))
            self.assertEqual(scans.call_count, 1)                           # one scan for the whole burst
            self.assertError(self.get('/api/cover/instagram/' + '0' * 12 + '.jpg'), 404, 'not_found')
            self.assertEqual(scans.call_count, 1)                           # an unknown id right after: no rescan
            with mock.patch.object(dashboard, 'COVER_RESCAN', 0):
                self.assertError(self.get('/api/cover/instagram/' + '0' * 12 + '.jpg'), 404, 'not_found')
            self.assertEqual(scans.call_count, 2)                           # ...later it rescans once
        self.assertTrue(all(a.status == 200 for a in first.values()))
        gone = self.fake.cover(publish.find(self.root, 'instagram', ID[6]))
        moved = gone.with_name(gone.name + '.away')
        os.replace(gone, moved)
        self.addCleanup(os.replace, moved, gone)
        self.assertError(self.get(f'/api/cover/instagram/{ID[6]}.jpg?download=1'), 404, 'not_found')   # re-checked

    def test_G22_slow_test_answers_in_time(self):
        gate = threading.Event()
        real = self.fake.test

        def slow():
            gate.wait(10)
            return real()
        self.fake.test = slow
        with mock.patch.object(dashboard, 'CALL_WAIT', 0.3):
            t = time.monotonic()
            a = self.post('/api/platform/test', {'platform': 'instagram'})
            self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(a.status, 202, a.data[:300])
        self.assertEqual(a.json['pending'], True)
        self.assertRegex(a.json['message'], r'^Instagram has not answered yet\.')
        gate.set()
        for _ in range(100):                                                # the test finishes on its own
            log = (guard.WORK / 'dashboard.log').read_text(encoding='utf-8')
            if 'connection test finished after the answer' in log:
                break
            time.sleep(0.05)
        self.assertIn('dev@localhost instagram connection test finished after the answer: Instagram answers.', log)
        self.assertIn('platform.test', [i['kind'] for i in publish.activity()['items']])

    def test_G22_capped(self):
        with mock.patch.object(dashboard, 'CALL_WAIT', 5):
            self.assertEqual(dashboard.capped(lambda: {'ok': True}, 'x', 'quick'), {'ok': True})
            def busy():
                raise publish.PublishError(409, 'busy', 'Busy.')
            with self.assertRaises(publish.PublishError):
                dashboard.capped(busy, 'x', 'refused')

    def test_F9_key_refused_by_the_collector(self):
        why = 'Instagram refused the key (code 190).'
        with mock.patch.object(dashboard.stats, 'key_refused', create=True, return_value=why), \
                mock.patch.object(dashboard.stats, 'clear_key_refused', create=True) as clear:
            home = self.get('/api/home').json
            self.assertEqual(home['health']['platforms'], [{'name': 'instagram', 'ok': False, 'set_up': True,
                                                            'breaker': 'closed',
                                                            'text': 'Instagram key refused: test the connection'},
                                                           {'name': 'youtube', 'ok': False, 'set_up': False,
                                                            'breaker': None, 'text': 'YouTube not available'}])
            card = next(c for c in home['attention'] if c['id'] == 'key_refused:instagram')
            conforms(self, card, ATTENTION)
            self.assertEqual((card['level'], card['who'], card['title']), ('red', 'pc', 'Instagram refused the key'))
            self.assertTrue(card['text'].startswith(why))
            self.assertEqual(card['action']['endpoint'], '/api/platform/test')
            self.assertEqual(self.post('/api/platform/test', {'platform': 'instagram'}).status, 200)
            self.assertEqual(clear.call_count, 1)                           # a passing test forgets it at once
            publish.trip('meta', 'auth', 'Instagram refused the key (code 190).')
            ids = [c['id'] for c in self.get('/api/home').json['attention']]
            self.assertNotIn('key_refused:instagram', ids)                  # the breaker card says it already
        home = self.get('/api/home').json
        self.assertNotIn('key_refused:instagram', [c['id'] for c in home['attention']])

    def test_F9_end_to_end_with_the_collector(self):
        if not hasattr(dashboard.stats, 'key_refused'):
            self.skipTest('stats.key_refused() is not there yet (request to the stats.py owner)')
        for kind in ('transient', 'rate', 'auth'):
            with self.subTest(kind=kind):
                for n in ('read_stats', 'read_limit', 'read_permissions'):
                    setattr(self.fake, n, mock.Mock(side_effect=platform_fake.FakeError(kind)))
                with dashboard.stats._Con(write=True) as con:
                    con.execute('DELETE FROM sources')
                dashboard.stats.collect(self.root, NOW, youtube=fixtures.fake_youtube_api(fixtures.YT_VIDEOS,
                                                                                          fixtures.YT_CHANNEL))
                self.assertEqual(publish.breaker('meta')['state'], 'closed')               # S17
                p = dashboard.health(self.root, NOW)['platforms'][0]
                ids = [c['id'] for c in dashboard.attention(self.root, NOW)]
                refused = kind == 'auth'
                self.assertEqual((p['ok'], p['text']), (not refused, 'Instagram key refused: test the connection'
                                                        if refused else 'Instagram key OK'))
                self.assertEqual('key_refused:instagram' in ids, refused)
        for n in ('read_stats', 'read_limit', 'read_permissions'):
            delattr(self.fake, n)
        with dashboard.stats._Con(write=True) as con:
            con.execute('DELETE FROM sources')
        dashboard.stats.collect(self.root, NOW, youtube=fixtures.fake_youtube_api(fixtures.YT_VIDEOS, fixtures.YT_CHANNEL))
        self.assertEqual(dashboard.health(self.root, NOW)['platforms'][0]['text'], 'Instagram key OK')


class FromA1(DevCase):
    """Server follow-ups to the posting-core fixes (F7 items_failed, G1 resolve codes, G2 IG.load raising)."""

    def test_F7_items_failed_card_order(self):
        c = lambda cid, level='yellow': dashboard._card(cid, level, 'pc', 't', 'x')
        cards = [c('queue_low:instagram'), c('auto_off_waiting:instagram'), c('items_failed:instagram'),
                 c('record_pending:j1'), c('post_failed:j2', 'red'), c('breaker:meta', 'red')]
        self.assertEqual(dashboard.RULES.index('items_failed'), dashboard.RULES.index('post_failed') + 1)
        self.assertEqual([x['id'].split(':')[0] for x in sorted(cards, key=dashboard._order)],
                         ['post_failed', 'breaker', 'items_failed', 'record_pending', 'auto_off_waiting', 'queue_low'])

    def test_G1_resolve_codes_pass_through(self):
        for code in ('published', 'check_first', 'too_early', 'not_sent', 'posted', 'not_unresolved'):
            with self.subTest(code=code):
                said = f'Example text for {code}.'
                with mock.patch.object(publish, 'resolve_job', side_effect=publish.PublishError(409, code, said)):
                    a = self.post('/api/job/resolve', {'job': 'j0123456789', 'outcome': 'not_posted'})
                self.assertEqual(self.assertError(a, 409, code)['message'], said)

    def test_G2_unreadable_record_is_no_500(self):
        def locked(out):
            raise PermissionError(13, 'Permission denied', 'instagram-posted.json')
        with mock.patch.object(dashboard.IG, 'load', locked):
            home = self.get('/api/home')
            videos = self.get('/api/videos')
        self.assertEqual(home.status, 200)
        card = next(x for x in home.json['attention'] if x['id'] == 'lessons_error')
        self.assertIn('instagram-posted.json', card['text'])
        body = self.assertError(videos, 503, 'lessons_error')
        self.assertTrue(body['message'].startswith('The lessons could not be read: PermissionError'), body)
        with mock.patch.object(dashboard, 'items', side_effect=OSError('gone')), \
                mock.patch.object(dashboard, '_drive_gone', return_value=True):
            self.assertError(self.get('/api/videos'), 503, 'drive_missing')


class FixRoundE3(DevCase):
    """Fix round E3: G29 (resume after a restart), G17 (one scan per request), F52 (key expiry on Home)."""

    def test_G29_resume_right_after_a_restart(self):
        rev = self.get('/api/publish?platform=instagram').json['settings_rev']
        self.assertEqual(self.post('/api/settings', {'platform': 'instagram', 'rev': rev, 'settings': {'auto': True},
                                                     'ack': ['auto_on']}).status, 200)
        with publish._Con(write=True) as con:
            con.execute("INSERT INTO auto_state (platform, failures, suspended, suspended_why) VALUES (?, 3, ?, ?) "
                        'ON CONFLICT(platform) DO UPDATE SET suspended = excluded.suspended',
                        ('instagram', S.iso(NOW), 'made-up'))
        with mock.patch.object(publish, '_ROOT', None):                     # no page read since the restart
            a = self.post('/api/auto/resume', {'platform': 'instagram', 'ack': ['resume_auto']})
        self.assertEqual(a.status, 200, a.data[:300])
        self.assertEqual(a.json['auto']['reason'], 'scheduled', a.json['auto'])
        self.assertIsNotNone(a.json['auto']['next']['at'])

    def test_G17_one_scan_per_request(self):
        for path in ('/api/home', '/api/videos'):
            with self.subTest(path=path), mock.patch.object(self.fake, 'candidates', wraps=self.fake.candidates) as scans:
                a = self.get(path)
                self.assertEqual(a.status, 200, a.data[:300])
                self.assertEqual(scans.call_count, 1)
                first = a.json
        with mock.patch.object(publish, 'views', side_effect=OSError('locked')), \
                mock.patch.object(self.fake, 'candidates', wraps=self.fake.candidates) as scans:
            home = self.get('/api/home').json                                # each part reads its own then
            self.assertGreater(scans.call_count, 1)
        self.assertEqual(home['counts'], self.get('/api/home').json['counts'])
        self.assertEqual(len(first['videos']), len(self.get('/api/videos').json['videos']))

    def test_F52_key_expiring_card_and_health(self):
        warn = 'WARNING: Meta access ends 2026-11-12: run  instagram.py unified-token  again (instagram-setup.md).'
        self.fake.key_warning = lambda now: warn
        home = self.get('/api/home').json
        card = next(c for c in home['attention'] if c['id'] == 'key_expiring:instagram')
        conforms(self, card, ATTENTION)
        self.assertEqual((card['level'], card['who'], card['title'], card['text'], card['action']),
                         ('yellow', 'pc', 'The Instagram key ends soon', warn.removeprefix('WARNING: '), None))
        self.assertEqual(home['health']['platforms'][0]['text'], warn.removeprefix('WARNING: '))
        self.fake.key_warning = mock.Mock(side_effect=RuntimeError('broken'))
        home = self.get('/api/home')
        self.assertEqual(home.status, 200)
        self.assertNotIn('key_expiring:instagram', [c['id'] for c in home.json['attention']])
        self.assertEqual(home.json['health']['platforms'][0]['text'], 'Instagram key OK')
        del self.fake.key_warning
        self.assertNotIn('key_expiring:instagram', [c['id'] for c in self.get('/api/home').json['attention']])


class OnePort(Case):
    """F23: a second server on a port another one listens on fails loudly (no SO_REUSEADDR on Windows)."""

    def test_F23_port_taken(self):
        first = dashboard._Server(('127.0.0.1', 0), dashboard.Handler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            dashboard._Server(('127.0.0.1', first.server_address[1]), dashboard.Handler)

    def test_F23_restart_on_the_same_port(self):
        first = dashboard._Server(('127.0.0.1', 0), dashboard.Handler)
        port = first.server_address[1]
        t = threading.Thread(target=first.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        t.start()
        for _ in range(3):
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            c.request('GET', '/api/home')
            c.getresponse().read()
            c.close()
        first.shutdown()
        first.server_close()
        t.join(5)
        again = dashboard._Server(('127.0.0.1', port), dashboard.Handler)
        again.server_close()

    def test_F23_make_server_uses_it(self):
        self.assertTrue(issubclass(dashboard._Server, dashboard.ThreadingHTTPServer))
        self.assertEqual(dashboard._Server.allow_reuse_address, os.name != 'nt')


class SettingsReadOnly(DevCase):
    """S12: hard_cap_24h cannot be changed through HTTP (any value, dry run or not); nothing is saved."""

    def test_read_only(self):
        before = self.get('/api/publish?platform=instagram').json
        rev, cap = before['settings_rev'], before['settings']['hard_cap_24h']
        for patch in ({'hard_cap_24h': 5}, {'hard_cap_24h': 10 ** 6}, {'hard_cap_24h': cap}, {'hard_cap_24h': None},
                      {'post_after': '19:00', 'hard_cap_24h': 4}):
            for dry in (False, True):
                with self.subTest(patch=patch, dry=dry):
                    body = self.assertError(self.post('/api/settings', {'platform': 'instagram', 'rev': rev,
                                                                        'settings': patch, 'dry_run': dry}),
                                            422, 'read_only')
                    self.assertEqual(body['fields'], {'hard_cap_24h': S.READ_ONLY})
                    self.assertEqual(body['message'], 'The 24-hour limit can only be changed on the PC.')
        after = self.get('/api/publish?platform=instagram').json
        self.assertEqual((after['settings_rev'], after['settings']), (rev, before['settings']))
        with self.assertRaises(publish.PublishError) as e:                  # and below HTTP too (schedule.py)
            publish.save_settings('instagram', {'hard_cap_24h': 5}, rev, [], 'dev@localhost')
        self.assertEqual(e.exception.code, 'read_only')


# ---------- S22 ----------

def _jpeg(path: Path, size=(90, 160), orientation=None, color=(200, 30, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    kw = {}
    if orientation:
        ex = Image.Exif()
        ex[0x0112] = orientation
        kw['exif'] = ex.tobytes()
    Image.new('RGB', size, color).save(path, 'JPEG', **kw)
    return path


def tools_extras(out: Path) -> Path:
    """An output folder's extras folder (made when missing)."""
    x = tools.extras(out)
    x.mkdir(parents=True, exist_ok=True)
    return x


class CoverRules(DevCase):
    """S22: only re-encoded images of candidates under the root, at most 15 MB, ending in .jpg, .jpeg or .png;
    everything else is 404 not_found. Covers are cached (at most COVER_KEEP files)."""

    def covers(self, **by_part):
        """The fake platform with these cover files for new-short-<part>."""
        found = fixtures.reel_items(self.root)
        for it in found:
            if it['part'] in {int(k[1:]) for k in by_part} and it['lesson'] == 'new':
                it['cover'] = by_part[f'p{it["part"]}']
        self.fake = platform_fake.FakePlatform(found, records=guard.WORK / 'fake-records.json', root=self.root)
        publish.set_registry({'instagram': self.fake})

    def extras(self):
        return tools_extras(self.root / 'new' / 'Output')

    def nf(self, item=6, platform='instagram'):
        with self.assertRaises(publish.PublishError) as e:
            dashboard.cover_image(self.root, platform, ID[item] if isinstance(item, int) else item)
        self.assertEqual((e.exception.status, e.exception.code), (404, 'not_found'))
        self.assertError(self.get(f'/api/cover/{platform}/{ID[item] if isinstance(item, int) else item}.jpg'), 404,
                         'not_found')

    def test_reencoded(self):
        src = _jpeg(self.extras() / 'turned.jpg', (120, 60), orientation=6)
        self.covers(p6=src)
        data, name = dashboard.cover_image(self.root, 'instagram', ID[6])
        self.assertEqual(name, 'turned.jpg')
        self.assertNotEqual(data, src.read_bytes())
        im = Image.open(io.BytesIO(data))
        self.assertEqual((im.format, im.size), ('JPEG', (60, 120)))        # turned upright, no EXIF left
        self.assertEqual(len(im.getexif()), 0)

    def test_sizes_and_download(self):
        self.covers(p6=_jpeg(self.extras() / 'cover-short-6.jpg', (1200, 2000)))
        small, _ = dashboard.cover_image(self.root, 'instagram', ID[6])
        full, name = dashboard.cover_image(self.root, 'instagram', ID[6], download=True)
        self.assertEqual(Image.open(io.BytesIO(small)).size, (540, 900))
        self.assertEqual(Image.open(io.BytesIO(full)).size, (1200, 2000))
        self.assertEqual(name, 'cover-short-6.jpg')

    def test_png_with_alpha(self):
        src = self.extras() / 'cover-alpha.png'
        Image.new('RGBA', (40, 80), (255, 0, 0, 0)).save(src)
        self.covers(p6=src)
        im = Image.open(io.BytesIO(dashboard.cover_image(self.root, 'instagram', ID[6])[0]))
        self.assertEqual((im.format, im.mode), ('JPEG', 'RGB'))
        self.assertLess(sum(abs(a - b) for a, b in zip(im.getpixel((20, 40)), dashboard.NAVY)), 12)

    def test_outside_the_root(self):
        self.covers(p6=_jpeg(guard.TMP / f'outside-{self.id()[-12:]}' / 'cover.jpg'))
        self.nf()

    def test_link_out_of_the_root(self):
        """A cover under the root whose folder is a link to a folder outside it (a junction: no admin rights)."""
        import _winapi
        outside = _jpeg(guard.TMP / f'outside-{self.id()[-12:]}' / 'cover.jpg')
        link = self.extras() / 'linked'
        try:
            _winapi.CreateJunction(str(outside.parent), str(link))
        except OSError as e:
            self.skipTest(f'no junctions here: {e}')
        self.addCleanup(os.rmdir, link)                                     # the link only, not its target
        self.assertTrue((link / 'cover.jpg').is_file())
        self.covers(p6=link / 'cover.jpg')
        self.nf()

    def test_extensions(self):
        good = _jpeg(self.extras() / 'x.jpg')
        for name in ('cover.gif', 'cover.webp', 'cover.jpg.txt', 'cover', 'cover.bmp'):
            with self.subTest(name=name):
                bad = self.extras() / name
                shutil.copyfile(good, bad)
                self.addCleanup(bad.unlink)
                self.covers(p6=bad)
                self.nf()
        for name in ('UPPER.JPG', 'cover.jpeg', 'cover.png'):
            with self.subTest(name=name):
                ok = self.extras() / name
                Image.open(good).save(ok, 'PNG' if name.endswith('png') else 'JPEG')
                self.addCleanup(ok.unlink)
                self.covers(p6=ok)
                self.assertEqual(self.get(f'/api/cover/instagram/{ID[6]}.jpg').status, 200)

    def test_too_big_or_not_an_image(self):
        big = self.extras() / 'big.jpg'
        shutil.copyfile(_jpeg(self.extras() / 'small.jpg'), big)
        with open(big, 'r+b') as f:
            f.truncate(dashboard.COVER_MAX + 1)                             # a valid JPEG start, 15 MB + 1 byte
        text = self.extras() / 'text.jpg'
        text.write_text('not an image', encoding='utf-8')
        gif = self.extras() / 'gif.png'
        Image.new('P', (10, 10)).save(gif, 'GIF')
        bomb = self.extras() / 'bomb.png'
        Image.new('1', (7000, 6000)).save(bomb, 'PNG')                      # 42 M pixels: refused before decoding
        for f in (big, text, gif, bomb):
            with self.subTest(file=f.name):
                self.covers(p6=f)
                self.nf()

    def test_not_a_candidate(self):
        self.nf(publish.item_id(ZU))                                        # a video, but not a Reel candidate
        self.nf('0' * 12)
        self.nf(6, platform='tiktok')
        self.covers(p6=None)
        self.nf(6)                                                          # a candidate without a cover
        for path in (f'/api/cover/instagram/{ID[6]}.png', f'/api/cover/instagram/{ID[6]}.jpg/x',
                     f'/api/cover/instagram/..%2f{ID[6]}.jpg', '/api/cover/instagram/new-short-6.jpg',
                     f'/api/cover/Instagram/{ID[6]}.jpg', f'/api/cover/instagram/{ID[6][:11]}.jpg'):
            with self.subTest(path=path):
                self.assertError(self.get(path), 404, 'not_found')

    def test_cache(self):
        self.covers(p6=_jpeg(self.extras() / 'c6.jpg'), p7=_jpeg(self.extras() / 'c7.jpg'),
                    p8=_jpeg(self.extras() / 'c8.jpg'))
        with mock.patch.object(dashboard, '_reencode', wraps=dashboard._reencode) as enc, \
                mock.patch.object(dashboard, 'COVER_KEEP', 2):
            first = dashboard.cover_image(self.root, 'instagram', ID[6])
            self.assertEqual(dashboard.cover_image(self.root, 'instagram', ID[6]), first)
            self.assertEqual(enc.call_count, 1)
            for n in (7, 8):
                dashboard.cover_image(self.root, 'instagram', ID[n])
                time.sleep(0.02)
        self.assertEqual(len(list(dashboard.COVER_CACHE.glob('*.jpg'))), 2)


# ---------- S24 ----------

class NoTraversal(DevCase):
    """S24: static files come from the whitelist only; ids and lesson names are matched against what exists, never
    used as paths."""

    def test_whitelist(self):
        for path, name in dashboard.STATIC.items():
            f = dashboard.PAGE / name
            with self.subTest(path=path):
                self.assertTrue(f.is_file(), f'{f}: the whitelist names a page file that is not there')
                a = self.get(path)
                self.assertEqual((a.status, a.data), (200, f.read_bytes()))
                self.assertEqual(a.headers['Content-Type'], dashboard.TYPES[f.suffix])
                self.assertSecure(a)
        logo = self.get('/logo.png')
        self.assertEqual((logo.status, logo.headers['Content-Type'], logo.data),
                         (200, 'image/png', dashboard.LOGO.read_bytes()))
        self.assertEqual(self.get('/app.js?v=2').data, (dashboard.PAGE / 'app.js').read_bytes())   # a query is ignored
        self.assertEqual(self.get('//app.js').status, 200)          # http.server folds leading slashes: still /app.js
        self.assertEqual(sorted(p.name for p in dashboard.PAGE.iterdir() if p.is_file()),
                         sorted(set(dashboard.STATIC.values())))    # every page file is served, nothing else there

    def test_traversal_404(self):
        for path in ('/..%2fdashboard.py', '/dashboard.py', '/../dashboard.py', '/%2e%2e/dashboard.py',
                     '/dashboard/app.js', '/app.js/', '/APP.JS', '/index.html/..%2f..%2fdashboard.py',
                     '/logo.png/..', '/docs/brand/logo-trde.png', '/tests/guard.py', '/app.js%00.py', '/..\\dashboard.py',
                     '/%5c..%5cdashboard.py', '/dashboard.html', '/api/../dashboard.py', '/favicon.ico'):
            with self.subTest(path=path):
                a = self.get(path)
                self.assertError(a, 404, 'not_found')
                self.assertNotIn(b'import', a.data)

    def test_ids_and_lessons(self):
        self.assertError(self.get('/api/preview?platform=instagram&id=..%2f..%2fx'), 400, 'bad_request')
        self.assertError(self.get(f'/api/preview?platform=instagram&id={publish.item_id(ZU)}'), 404, 'unknown_item')
        lesson = str(self.root / 'new')
        for name in ('..', '.', '../new', 'new/..', 'new\\..\\new', '_channel', lesson, lesson.lower(), '', None, 5,
                     ['new'], 'C:\\Windows'):
            with self.subTest(name=name):
                self.assertError(self.post('/api/lesson/retry', {'lesson': name}), 400, 'no_such_lesson')
        self.assertFalse(any((guard.WORK / d).is_dir() and (guard.WORK / d / 'retry').exists()
                             for d in os.listdir(guard.WORK)))


# ---------- S23 ----------

class Redaction(DevCase):
    """S23: no token, JWT, Authorization value or cookie reaches an answer, a log line or dashboard.log."""

    def assertClean(self, text):
        for secret in (SECRET, JWT, 'hunter2', 'abc123secret'):
            self.assertNotIn(secret, text)

    def test_log_tail(self):
        log = guard.WORK / 'instagram.log'
        log.write_text('\n'.join([f'2026-11-04 16:46 GET /me?access_token={SECRET}&fields=id',
                                  f'Authorization: Bearer {SECRET}', f'Cookie: CF_Authorization={JWT}',
                                  f'jwt {JWT} end', 'password=hunter2 next', '{"access_token": "abc123secret"}',
                                  'new-short-5.mp4 posted']) + '\n', encoding='utf-8')
        rows = dashboard.log_tail(log, 50)
        self.assertEqual(len(rows), 7)
        self.assertClean('\n'.join(rows))
        self.assertEqual(rows[-1], 'new-short-5.mp4 posted')
        self.assertTrue(all('[hidden]' in r for r in rows[:-1]), rows)

    def test_activity_logs(self):
        (guard.WORK / 'watch.log').write_text(f'token={SECRET}\nAuthorization: Bearer {JWT}\n', encoding='utf-8')
        (guard.WORK / 'instagram.log').write_text(f'Meta said {SECRET}\n', encoding='utf-8')
        a = self.get('/api/activity')
        self.assertEqual(a.status, 200)
        self.assertClean(a.data.decode('utf-8'))
        self.assertEqual(a.json['logs']['instagram'], ['Meta said [hidden]'])

    def test_errors_and_logs(self):
        with mock.patch.object(publish, 'state', side_effect=RuntimeError(f'GET x?access_token={SECRET} Bearer {JWT}')):
            body = self.assertError(self.get('/api/publish?platform=instagram'), 500, 'server_error')
        self.assertIn('[hidden]', body['message'])
        headers = {'Authorization': f'Bearer {SECRET}', 'Cookie': f'CF_Authorization={JWT}',
                   'Cf-Access-Jwt-Assertion': JWT}
        self.post('/api/queue', {'platform': 'instagram', 'op': 'skip', 'id': ID[9], 'rev': 0}, headers=headers)
        self.post('/api/queue', {'platform': 'instagram', 'op': 'skip'}, headers={**headers, 'X-Dashboard': None})
        self.assertError(self.post(f'/api/{JWT}?token={SECRET}', {}), 404, 'not_found')    # the path is logged
        self.get('/api/home', headers=headers)
        self.assertClean(self.out.getvalue())
        logged = (guard.WORK / 'dashboard.log').read_text(encoding='utf-8')
        self.assertClean(logged)
        self.assertIn('dev@localhost POST /api/[hidden] 404 not_found', logged)
        self.assertClean(json.dumps(publish.activity()))

    def test_answers_are_not_redacted_whole(self):
        """Dev A's request: redact() is for text like log tails; a whole JSON answer through it would hide every
        item's "key" (and long words in captions)."""
        a = self.get('/api/publish?platform=instagram')
        self.assertIn(f'"key": "{KEY[6]}"'.encode(), a.data)
        self.assertNotIn(b'[hidden]', a.data)
        word = 'Donaudampfschifffahrtsgesellschaftskapitaen' + 'switwe'           # one 49-letter word
        rev = a.json['queue']['rev']
        done = self.post('/api/queue', {'platform': 'instagram', 'op': 'caption', 'id': ID[7], 'rev': rev,
                                        'caption': f'Ein langes Wort: {word}'})
        self.assertEqual(done.status, 200, done.data[:300])
        pv = self.get(f'/api/preview?platform=instagram&id={ID[7]}').json
        self.assertEqual(pv['caption'], f'Ein langes Wort: {word}')

    def test_lesson_error(self):
        out = fixtures.write_lesson(self.root, 'Leaky lesson', state='failed', whole=(60, True))
        self.addCleanup(shutil.rmtree, out.parent, True)
        st = tools_extras(out) / 'status.json'
        data = json.loads(st.read_text(encoding='utf-8'))
        st.write_text(json.dumps({**data, 'error': f'MetaError: Meta 400: bad access_token={SECRET}'}), encoding='utf-8')
        home, videos = self.get('/api/home'), self.get('/api/videos')
        self.assertClean(home.data.decode('utf-8') + videos.data.decode('utf-8'))
        card = next(c for c in home.json['attention'] if c['id'] == 'lesson_stopped:Leaky lesson')
        self.assertIn('[hidden]', card['text'])


# ---------- 410 ----------

class Gone(DevCase):
    """The old endpoints answer 410 with the human text in both fields, so the v1 page shows it."""

    def test_gone(self):
        text = 'Reload this page: the dashboard was updated.'
        for a in (self.get('/api/status'), self.get('/dashboard.js'),
                  self.post('/api/action', {'action': 'reel-now', 'force': True}), self.get('/api/action')):
            self.assertEqual(a.status, 410)
            self.assertEqual(a.json, {'error': text, 'message': text})
            self.assertSecure(a)
        self.assertEqual(self.fake.calls, [])


# ---------- S26 ----------

class DevRefusals(Case):
    """S26: --dev refuses port 8790, the real work folder (as PIPELINE_WORK or any module's folder, also through a
    link) and a root on the real Drive, before anything is set up; it uses the fake platform and fake YouTube."""

    def setUp(self):
        super().setUp()
        self.blocks = [*(mock.patch.object(dashboard, n, side_effect=AssertionError(f'{n} ran'))
                         for n in ('_dev_setup', '_Server')),
                       mock.patch.object(publish, 'settings', side_effect=AssertionError('settings ran'))]
        for p in self.blocks:                                               # nothing may run after a refusal
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(_handler_defaults)

    def refused(self, why, **kw):
        with self.assertRaisesRegex(SystemExit, why):
            dashboard.make_server(**kw)

    def test_live_port(self):
        self.refused('port 8790', port=8790, dev=True, fixtures=True)
        self.refused('port 8790', dev=True)                                 # the default port is the live one

    def test_real_work_folder(self):
        for work in (r'D:\YT\easy-work', r'd:\yt\EASY-WORK', 'D:/YT/easy-work/', r'D:\YT\easy-work\sub'):
            with self.subTest(work=work), mock.patch.dict(os.environ, {'PIPELINE_WORK': work}):
                self.refused('real work folder', port=0, dev=True, fixtures=True)
        with mock.patch.dict(os.environ):
            os.environ.pop('PIPELINE_WORK')
            self.refused('real work folder', port=0, dev=True)              # unset means the real one
        for module, name in ((easy, 'WORK_ROOT'), (publish, 'WORK'), (S, 'WORK'), (IG, 'WORK')):
            with self.subTest(module=module.__name__), mock.patch.object(module, name, Path(r'D:\YT\easy-work')):
                self.refused('real work folder', port=0, dev=True)

    def test_links_are_resolved(self):
        import _winapi
        base = Path(tempfile.mkdtemp(prefix='link-', dir=guard.TMP))
        target, link = base / 'target', base / 'link'
        target.mkdir()
        try:
            _winapi.CreateJunction(str(target), str(link))
        except OSError as e:
            self.skipTest(f'no junctions here: {e}')
        self.assertTrue(dashboard._inside(link, target))
        self.assertTrue(dashboard._inside(link / 'sub', str(target)))
        self.assertFalse(dashboard._inside(base / 'other', target))
        self.assertFalse(dashboard._inside(str(target) + '-2', target))

    def test_real_drive_root(self):
        for root in (r'G:\My Drive\SINEM-YouTube', 'j:/my drive/SINEM-YouTube', r'H:\My Drive'):
            with self.subTest(root=root):
                self.refused('Google Drive', port=0, dev=True, root=root)
                self.refused('Google Drive', port=0, dev=True, root=root, fixtures=True)
        for root, on in ((r'G:\My Drive\x', True), (r'D:\My Drive Backup\x', False), (r'C:\Users\x\My Drive', False),
                         (str(guard.TMP), False)):
            self.assertEqual(dashboard.on_drive(root), on, root)

    def test_F22_namespace_spellings_of_the_work_folder(self):
        target = str(guard.TMP / 'pretend-real-work')
        B = '\\'
        spellings = (B * 2 + '?' + B + target, B * 2 + '?' + B + target.lower() + r'\sub', B * 2 + '.' + B + target,
                     '//?/' + target.replace(B, '/'), B + '??' + B + target, target + '::$INDEX_ALLOCATION',
                     target + r':$I30:$INDEX_ALLOCATION\sub', B * 2 + r'?\UNC\localhost\D$\x',
                     B * 2 + r'?\GLOBALROOT\Device\HarddiskVolume3\x', B * 2 + r'localhost\D$\x')
        with mock.patch.object(dashboard, 'REAL_WORK', target):
            for work in spellings:
                with self.subTest(work=work), mock.patch.dict(os.environ, {'PIPELINE_WORK': work}):
                    self.refused('refuses to start', port=0, dev=True, fixtures=True)
            for work in spellings[5:7]:                                     # a stream suffix is the folder itself
                self.assertTrue(dashboard._inside(work, target), work)
            self.assertTrue(dashboard._inside(spellings[0], target))
            self.assertFalse(dashboard._inside(target + '-2', target))

    def test_F22_namespace_spellings_of_a_drive_root(self):
        B = '\\'
        with mock.patch.object(dashboard.os.path, 'realpath', lambda p: p):    # no drive is touched
            for root in (B * 2 + r'?\G:\My Drive\SINEM-YouTube', B * 2 + r'.\G:\My Drive\SINEM-YouTube',
                         B * 2 + r'?\G:\My Drive', B + r'??\G:\My Drive', 'G:/My Drive/SINEM-YouTube',
                         r'G:\My Drive::$INDEX_ALLOCATION\SINEM-YouTube'):
                with self.subTest(root=root):
                    self.refused('refuses to start', port=0, dev=True, root=root)
                    self.assertTrue(dashboard.on_drive(dashboard._plain(root)), root)

    def test_root_and_fixtures_need_dev(self):
        self.refused('need --dev', port=0, root=guard.TMP)
        self.refused('need --dev', port=0, fixtures=True)

    def test_uses_the_fakes(self):
        for p in self.blocks:                                               # this one starts
            p.stop()
        base = Path(tempfile.mkdtemp(prefix='dev-', dir=guard.TMP))
        (guard.WORK / 'fake-records.json').write_text('{"marker": {"id": "x"}}', encoding='utf-8')
        (guard.WORK / 'notes.txt').write_text('mine', encoding='utf-8')
        with mock.patch.object(dashboard.subprocess, 'run', side_effect=AssertionError('a program ran')):
            httpd = dashboard.make_server(0, dev=True, root=base, fixtures=True)
            try:
                self.assertEqual(httpd.server_address[0], '127.0.0.1')
                self.assertIsInstance(publish.registry()['instagram'], platform_fake.FakePlatform)
                self.assertIsInstance(dashboard.Handler.youtube, fixtures.FakeYouTube)
                self.assertEqual((dashboard.Handler.dev, dashboard.Handler.access), (True, None))
                self.assertEqual(dashboard.Handler.root, base / 'SINEM-YouTube')
                self.assertNotIn('marker', (guard.WORK / 'fake-records.json').read_text(encoding='utf-8'))
                self.assertEqual((guard.WORK / 'notes.txt').read_text(encoding='utf-8'), 'mine')
                self.assertFalse(dashboard.start_watcher())                 # never schtasks in --dev
            finally:
                httpd.server_close()


# ---------- Home: attention, health, next, counts ----------

class HomeParts(Case):
    """attention(), health(), next and counts on a fixture root (spec 9.2, addendum C2)."""

    @classmethod
    def setUpClass(cls):
        guard.reset()
        cls.base = Path(tempfile.mkdtemp(prefix='home-', dir=guard.TMP))
        cls.root = fixtures.make_root(cls.base)
        fixtures.write_lesson(cls.root, 'Broken lesson', state='failed', whole=(60, True))
        fixtures.write_lesson(cls.root, 'Waiting lesson', state='waiting', whole=(60, True))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.fake = fixtures.fake_platform(self.root)
        publish.set_registry({'instagram': self.fake})

    def beat(self, minutes_ago, lock_minutes_ago=None):
        """watcher.json written minutes_ago (and a watch.lock)."""
        at = NOW - timedelta(minutes=minutes_ago)
        dashboard.HEARTBEAT.write_text(json.dumps({'at': S.iso(at), 'phase': 'done', 'pid': 1}), encoding='utf-8')
        if lock_minutes_ago is not None:
            dashboard.WATCH_LOCK.write_text('1', encoding='utf-8')
            t = (NOW - timedelta(minutes=lock_minutes_ago)).timestamp()
            os.utime(dashboard.WATCH_LOCK, (t, t))

    def test_lesson_cards(self):
        self.beat(4)
        cards = {c['id']: c for c in dashboard.attention(self.root, NOW)}
        stop = cards['lesson_stopped:Broken lesson']
        self.assertEqual((stop['level'], stop['who'], stop['text']), ('red', 'pc', 'RuntimeError: made-up failure (tests)'))
        self.assertEqual(stop['action'], {'kind': 'call', 'label': 'Retry', 'endpoint': '/api/lesson/retry',
                                          'body': {'lesson': 'Broken lesson'},
                                          'confirm': "Run 'Broken lesson' again from the start?", 'ack': None})
        wait = cards['lesson_waiting:Waiting lesson']
        self.assertEqual((wait['level'], wait['who'], wait['text'], wait['action']),
                         ('yellow', 'pc', 'Needs a Claude answer: texts', None))
        ready = cards['ready_upload:new']
        self.assertEqual((ready['level'], ready['who'], ready['text']),
                         ('yellow', 'sinem', '1 video ready to upload in the YouTube app'))
        self.assertEqual(ready['action'], {'kind': 'link', 'label': 'Open Videos', 'href': '#videos/ready'})
        for c in cards.values():
            conforms(self, c, ATTENTION)
        self.assertNotIn('watcher_stale', cards)
        self.assertNotIn('drive_missing', cards)

    def test_retry_asked(self):
        self.beat(4)
        marker = easy.retry_marker(self.root / 'Broken lesson')
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('dev@localhost', encoding='utf-8')
        stop = next(c for c in dashboard.attention(self.root, NOW) if c['id'] == 'lesson_stopped:Broken lesson')
        self.assertIsNone(stop['action'])
        self.assertIn('Retry is queued', stop['text'])

    def test_order(self):
        self.pause()
        ids = [c['id'] for c in dashboard.attention(self.root, NOW)]        # no watcher.json: stale
        self.assertEqual(ids, ['lesson_stopped:Broken lesson', 'watcher_stale', 'lesson_waiting:Waiting lesson',
                               'ready_upload:new', 'auto_off_waiting:instagram'])
        cards = dashboard.attention(self.root, NOW)
        rank = [(c['level'] != 'red', dashboard.RULES.index(c['id'].split(':')[0])) for c in cards]
        self.assertEqual(rank, sorted(rank))

    def test_watcher(self):
        for case, setup, want in (
                ('fresh', lambda: self.beat(4), (True, 'PC watcher 4 min ago', False)),
                ('stale', lambda: self.beat(120), (False, 'PC watcher last ran 2 h ago', False)),
                ('busy', lambda: self.beat(120, lock_minutes_ago=50), (True, None, True)),
                ('old lock', lambda: self.beat(500, lock_minutes_ago=7 * 60), (False, 'PC watcher last ran 8 h ago', False)),
                ('nothing', lambda: None, (False, 'PC watcher: no run seen yet', False))):
            with self.subTest(case=case):
                for f in (dashboard.HEARTBEAT, dashboard.WATCH_LOCK, dashboard.WATCH_LOG):
                    f.unlink(missing_ok=True)
                setup()
                w = dashboard.health(self.root, NOW)['watcher']
                self.assertEqual(w['ok'], want[0])
                if want[1]:
                    self.assertEqual(w['text'], want[1])
                self.assertEqual(w['busy_since'] is not None, want[2])
                ids = [c['id'] for c in dashboard.attention(self.root, NOW)]
                self.assertEqual('watcher_stale' in ids, not want[0])
        self.beat(120, lock_minutes_ago=50)
        self.assertEqual(dashboard.health(self.root, NOW)['watcher']['text'],
                         f'PC watcher busy since {S.local(NOW - timedelta(minutes=50)):%H:%M}')

    def test_watcher_before_the_new_easy(self):
        """No watcher.json yet (easy.py v1): watch.log's time stands in."""
        dashboard.WATCH_LOG.write_text('==== run\n', encoding='utf-8')
        t = (NOW - timedelta(minutes=3)).timestamp()
        os.utime(dashboard.WATCH_LOG, (t, t))
        w = dashboard.health(self.root, NOW)['watcher']
        self.assertEqual((w['ok'], w['text'], w['at']), (True, 'PC watcher 3 min ago', S.iso(NOW - timedelta(minutes=3))))

    def test_drive_and_platforms(self):
        h = dashboard.health(Path(r'G:\My Drive\SINEM-YouTube'), NOW)    # only its name is read
        self.assertEqual(h['drive'], {'ok': True, 'letter': 'G', 'text': 'Drive G:'})
        self.assertEqual(dashboard.health(None, NOW)['drive'], {'ok': False, 'letter': None, 'text': 'Drive not available'})
        self.assertEqual(h['platforms'][0]['text'], 'Instagram key OK')
        publish.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')
        p = dashboard.health(self.root, NOW)['platforms'][0]
        self.assertEqual((p['ok'], p['breaker']), (False, 'cooling'))
        self.assertRegex(p['text'], r'^Instagram paused until \d\d:\d\d$')
        publish.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        self.assertEqual(dashboard.health(self.root, NOW)['platforms'][0]['text'], 'Instagram stopped: test the connection')
        self.fake.ready = False
        self.assertEqual(dashboard.health(self.root, NOW)['platforms'][0],
                         {'name': 'instagram', 'ok': False, 'set_up': False, 'breaker': 'stopped',
                          'text': 'Instagram not set up'})
        publish.set_registry({})
        self.assertEqual(dashboard.health(self.root, NOW)['platforms'],
                         [{'name': 'instagram', 'ok': False, 'set_up': False, 'breaker': None,
                           'text': 'Instagram not available'}, {'name': 'youtube', 'ok': False, 'set_up': False, 'breaker': None, 'text': 'YouTube not available'}])

    def test_numbers_health(self):
        src = [{'source': 'youtube', 'last_ok': S.iso(NOW - timedelta(minutes=12)), 'last_try': None, 'next_try': None,
                'fails': 0, 'error': None},
               {'source': 'instagram', 'last_ok': S.iso(NOW - timedelta(minutes=70)), 'last_try': None,
                'next_try': None, 'fails': 2, 'error': 'Meta unreachable'}]
        with mock.patch.object(dashboard.stats, 'sources', return_value=src):
            self.assertEqual(dashboard.health(self.root, NOW)['stats'],
                             {'ok': False, 'at': S.iso(NOW - timedelta(minutes=12)),
                              'text': 'Numbers 12 min ago (Instagram not read)'})
        with mock.patch.object(dashboard.stats, 'sources', return_value=src[:1]):
            self.assertEqual(dashboard.health(self.root, NOW)['stats']['text'], 'Numbers 12 min ago')
        with mock.patch.object(dashboard.stats, 'sources', return_value=[]):
            self.assertEqual(dashboard.health(self.root, NOW)['stats'], {'ok': True, 'at': None, 'text': 'No numbers yet'})
        with mock.patch.object(dashboard.stats, 'sources', side_effect=RuntimeError('db locked')):
            self.assertEqual(dashboard.health(self.root, NOW)['stats']['ok'], False)

    def test_next_and_counts(self):
        self.pause()
        d = dashboard.home(self.root, 'dev@localhost', NOW)
        self.assertEqual(d['next'][0], {'platform': 'instagram', 'text': 'Automatic Reels are off · 6 waiting',
                                        'at': None, 'href': '#publish/instagram'})
        yt = d['next'][1]
        self.assertEqual((yt['platform'], yt['href']), ('youtube', '#videos'))
        self.assertRegex(yt['text'], r'^YouTube Short goes public \w{3} \d{1,2} \w{3} \d\d:\d\d · \S')
        self.assertEqual(d['counts'], {'instagram': {'waiting': 6, 'skipped': 0, 'failed': 0, 'posted': 5},
                                       'youtube': {'ready': 1, 'uploaded': 2, 'scheduled': 2, 'public': 8}})
        conforms(self, d, HOME)

    def test_next_when_on(self):
        d = dashboard.home(self.root, 'dev@localhost', NOW)                 # no pause flag: the migration turns it on
        self.assertRegex(d['next'][0]['text'], r'^Instagram Reel: \w{3} \d{1,2} \w{3} after \d\d:\d\d · new: ')
        self.assertRegex(d['next'][0]['at'], TIME.rx)

    def test_without_drive(self):
        d = dashboard.home(None, 'dev@localhost', NOW)
        conforms(self, d, {**HOME, 'counts': {}})
        self.assertEqual([c['id'] for c in d['attention']], ['watcher_stale', 'drive_missing'])
        self.assertEqual((d['next'], d['counts'], len(d['tiles'])), ([], {}, 4))
        self.beat(4)
        self.assertEqual([c['id'] for c in dashboard.home(None, 'x', NOW)['attention']], ['drive_missing'])

    def test_broken_scan(self):
        self.beat(4)
        with mock.patch.object(dashboard, 'items', side_effect=ValueError('broken status.json')):
            d = dashboard.home(self.root, 'dev@localhost', NOW)
        self.assertEqual(d['attention'][0]['id'], 'lessons_error')
        self.assertIn('broken status.json', d['attention'][0]['text'])
        self.assertEqual(d['counts']['instagram']['waiting'], 6)            # the rest still shows
        d = dashboard.home(self.base / 'gone' / 'SINEM-YouTube', 'dev@localhost', NOW)   # the folder went away
        self.assertIn('drive_missing', [c['id'] for c in d['attention']])
        with mock.patch.object(dashboard.stats, 'tiles', side_effect=RuntimeError('db')):
            self.assertEqual(dashboard.home(self.root, 'x', NOW)['tiles'], [])

    def test_F32_one_record_unreadable(self):
        """A Drive record held by a sync (PermissionError) is one lesson's problem, not 'Drive is not available'."""
        self.beat(4)
        held, load = self.root / 'new' / 'Output', IG.load

        def locked(out):
            if Path(out) == held:
                raise PermissionError(13, 'Permission denied', str(tools.extras(held) / 'instagram-posted.json'))
            return load(out)
        with mock.patch.object(dashboard.IG, 'load', locked):
            d = dashboard.home(self.root, 'dev@localhost', NOW)
        ids = [c['id'] for c in d['attention']]
        self.assertNotIn('drive_missing', ids)
        card = next(c for c in d['attention'] if c['id'] == 'lessons_error')
        self.assertIn('instagram-posted.json', card['text'])
        self.assertTrue(d['health']['drive']['ok'])
        self.assertEqual(d['counts']['instagram']['waiting'], 6)
        with mock.patch.object(dashboard, 'items', side_effect=OSError('one file')):    # the root is still there
            self.assertNotIn('drive_missing', [c['id'] for c in dashboard.home(self.root, 'x', NOW)['attention']])


class VideosView(Case):
    """videos_view(): v1's list merged with stats.youtube_cache(), newest first, each video with its Reel."""

    @classmethod
    def setUpClass(cls):
        guard.reset()
        cls.base = Path(tempfile.mkdtemp(prefix='videos-', dir=guard.TMP))
        cls.root = fixtures.make_root(cls.base)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        super().setUp()
        publish.set_registry({'instagram': fixtures.fake_platform(self.root)})

    def test_reels(self):
        v = {x['file']: x for x in dashboard.videos_view(self.root)['videos']}
        self.assertEqual(v['new-short-6.mp4']['reel'], {'platform': 'instagram', 'id': ID[6], 'state': 'waiting'})
        self.assertEqual(v['new-short-1.mp4']['reel'], {'platform': 'instagram', 'id': ID[1], 'state': 'posted'})
        self.assertEqual(v['um-etwas-bitten-video.mp4']['reel']['state'], 'waiting')
        self.assertIsNone(v['new-video.mp4']['reel'])                       # horizontal: not a Reel
        self.assertIsNone(v['zu-infinitiv-video.mp4']['reel'])
        publish.queue_op(self.root, 'instagram', 'skip', 0, 'dev@localhost', item_id=ID[7])
        v = {x['file']: x for x in dashboard.videos_view(self.root)['videos']}
        self.assertEqual(v['new-short-7.mp4']['reel']['state'], 'skipped')

    def test_covers_before_youtube(self):
        """A video not on YouTube yet shows the cover from Drive: YouTube's candidate first, else another platform's."""
        yt = fixtures.fake_youtube_platform(self.root)
        publish.set_registry({'instagram': fixtures.fake_platform(self.root), 'youtube': yt})
        v = {x['file']: x for x in dashboard.videos_view(self.root)['videos']}
        withcover = {f: x['cover'] for f, x in v.items() if x.get('cover')}
        self.assertTrue(withcover)
        for f, c in withcover.items():
            self.assertRegex(c['id'], r'^[0-9a-f]{12}$', f)
            p, item = dashboard._cover_item(self.root, c['platform'], c['id'])
            self.assertIsNotNone(p.cover(item), f)                       # the picture really exists
        yt_files = {it.file.name for it in publish.items(self.root, 'youtube')[0] if yt.cover(it) is not None}
        for f in yt_files & set(withcover):
            self.assertEqual(withcover[f]['platform'], 'youtube', f)

    def test_numbers_merged(self):
        um = 'FAKE_yt_011'
        cache = {um: {'views': 95, 'likes': 9, 'comments': 2, 'privacy': 'public', 'title': 'Example YouTube title',
                      'published': '2026-11-04T17:20:00Z'},
                 '_channel': {'subscribers': 143, 'views': 5230, 'videos': 25}, '_at': NOW.timestamp()}
        with mock.patch.object(dashboard.stats, 'youtube_cache', return_value=cache):
            d = dashboard.videos_view(self.root)
        u = next(x for x in d['videos'] if x.get('youtube') == um)
        self.assertEqual((u['title'], u['views'], u['likes'], u['comments'], u['privacy'], u['time']),
                         ('Example YouTube title', 95, 9, 2, 'public', '2026-11-04T17:20:00Z'))
        self.assertEqual(d['channel'], {'name': dashboard.CHANNEL_NAME, 'url': IG.CHANNEL, 'subscribers': 143,
                                        'views': 5230, 'videos': 25})
        self.assertEqual(d['counts_at'], S.iso(NOW))
        times = [x.get('time') or '' for x in d['videos']]
        self.assertEqual(times, sorted(times, reverse=True))
        conforms(self, d, VIDEOS)

    def test_no_numbers(self):
        with mock.patch.object(dashboard.stats, 'youtube_cache', side_effect=RuntimeError('no db')):
            d = dashboard.videos_view(self.root)
        self.assertEqual((d['counts_at'], d['channel']['subscribers'], len(d['videos'])), (None, None, 13))


class LogTail(Case):
    """log_tail(): the last lines, at most the last 200 KB read, the cut first line dropped, long lines shortened."""

    def test_tail(self):
        log, pad, last = guard.WORK / 'watch.log', ('log text ' * 10)[:88], 'last line, no newline'
        log.write_text(''.join(f'line {n:05d} {pad}\n' for n in range(3000)) + last, encoding='utf-8', newline='\n')
        self.assertEqual(log.stat().st_size, 300_000 + len(last))          # 100-byte lines
        self.assertEqual(dashboard.log_tail(log, 2), [f'line 02999 {pad}', last])
        rows = dashboard.log_tail(log, 5000)                                # the last 200 KB start inside line 952
        self.assertEqual((len(rows), rows[0], rows[-1]), (3000 - 953 + 1, f'line 00953 {pad}', last))
        self.assertEqual(dashboard.log_tail(log, 0), [])
        self.assertEqual(dashboard.log_tail(guard.WORK / 'missing.log', 10), [])

    def test_odd_content(self):
        log = guard.WORK / 'odd.log'
        log.write_bytes(b'caf\xe9 cp1252\r\n' + b'y ' * 1000 + b'\nlast')
        rows = dashboard.log_tail(log, 10)
        self.assertEqual(rows[0], 'caf\ufffd cp1252')
        self.assertEqual((len(rows[1]), rows[1][-1]), (1000, '…'))
        self.assertEqual(rows[2], 'last')


class Retry(DevCase):
    """POST /api/lesson/retry: v1's retry, by folder name only, with one audit row."""

    @classmethod
    def extra(cls, root):
        fixtures.write_lesson(root, 'Broken lesson', state='failed', whole=(60, True))

    def test_retry(self):
        a = self.post('/api/lesson/retry', {'lesson': 'Broken lesson'})
        self.assertEqual((a.status, a.json), (200, {'ok': True, 'message':
                                                    'Retry is queued: the PC starts on it within 10 minutes.'}))
        marker = easy.retry_marker(self.root / 'Broken lesson')
        self.assertTrue(marker.read_text(encoding='utf-8').startswith('dev@localhost '))
        rows = [i for i in publish.activity()['items'] if i['kind'] == 'lesson.retry']
        self.assertEqual([(r['who'], r['kind']) for r in rows], [('dev@localhost', 'lesson.retry')])
        card = next(c for c in self.get('/api/home').json['attention'] if c['id'] == 'lesson_stopped:Broken lesson')
        self.assertIsNone(card['action'])
        video = next(v for v in self.get('/api/videos').json['videos'] if v['lesson'] == 'Broken lesson')
        self.assertTrue(video['retry'])

    def test_watcher_started(self):
        with mock.patch.object(dashboard, 'start_watcher', return_value=True):
            a = self.post('/api/lesson/retry', {'lesson': 'Broken lesson'})
        self.assertEqual(a.json['message'], 'Retrying: the PC starts on it now.')

    def test_start_watcher_outside_dev(self):
        runs = []
        dashboard.Handler.dev = False
        self.addCleanup(setattr, dashboard.Handler, 'dev', True)
        fake = lambda args, **kw: runs.append(args) or types.SimpleNamespace(returncode=0)
        with mock.patch.object(dashboard.subprocess, 'run', fake):
            self.assertTrue(dashboard.start_watcher())
        self.assertEqual(runs, [['schtasks', '/run', '/tn', 'YT Easy mode watcher']])
        with mock.patch.object(dashboard.subprocess, 'run', side_effect=OSError('no schtasks')):
            self.assertFalse(dashboard.start_watcher())


class Background(Case):
    """The background thread: stats.collect every round, reconcile (api=True) for set-up platforms every other
    round (300 s and 600 s); it never raises."""

    def setUp(self):
        super().setUp()
        self.on = platform_fake.FakePlatform([], records=guard.WORK / 'on.json')
        self.off = platform_fake.FakePlatform([], name='other', set_up=False, records=guard.WORK / 'off.json')
        publish.set_registry({'instagram': self.on, 'other': self.off})
        self.root = guard.TMP / 'some-root'
        self.enterContext(mock.patch.object(dashboard, 'current_root', lambda: self.root))
        self.calls = []

    def collect(self, root, now=None, *, youtube=None):
        self.calls.append(('collect', root, youtube))
        return {'youtube': 'ok', 'instagram': f'error: Bearer {SECRET}'}

    def reconcile(self, root, name, api=True, now=None):
        self.calls.append(('reconcile', root, name, api))
        return [f'{name}: settled']

    def test_tick(self):
        yt = object()
        self.enterContext(mock.patch.object(dashboard.Handler, 'youtube', yt))
        with mock.patch.object(dashboard.stats, 'collect', self.collect), \
                mock.patch.object(publish, 'reconcile', self.reconcile):
            done = dashboard.tick(reconcile=True)
            self.assertEqual(self.calls, [('collect', self.root, yt), ('reconcile', self.root, 'instagram', True)])
            self.assertEqual(done['reconcile'], ['instagram'])
            self.calls.clear()
            dashboard.tick(reconcile=False)
            self.assertEqual(self.calls, [('collect', self.root, yt)])
        self.assertNotIn(SECRET, self.out.getvalue())
        self.assertIn('[hidden]', self.out.getvalue())

    def test_tick_never_raises(self):
        with mock.patch.object(dashboard.stats, 'collect', side_effect=RuntimeError('db')), \
                mock.patch.object(publish, 'reconcile', side_effect=RuntimeError('meta')):
            self.assertEqual(dashboard.tick(), {})
        with mock.patch.object(dashboard, 'current_root', side_effect=OSError('drive')), \
                mock.patch.object(dashboard.stats, 'collect', self.collect), \
                mock.patch.object(publish, 'reconcile', self.reconcile):
            dashboard.tick()
        self.assertEqual(self.calls[0][:2], ('collect', None))

    def test_loop(self):
        self.assertEqual((dashboard.STATS_EVERY, dashboard.RECONCILE_EVERY), (300, 600))
        seen, stop = [], threading.Event()

        def tick(reconcile=True):
            seen.append(reconcile)
            if len(seen) == 2:
                raise RuntimeError('a bad round')
            if len(seen) >= 5:
                stop.set()
        with mock.patch.object(dashboard, 'tick', tick), mock.patch.object(dashboard, 'STATS_EVERY', 0.01), \
                mock.patch.object(dashboard, 'RECONCILE_EVERY', 0.02):
            t = threading.Thread(target=dashboard.background, args=(stop,), daemon=True)
            t.start()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(seen, [True, False, True, False, True])


class ServeCli(Case):
    """The command line: dashboard.cmd's 'serve' keeps working; 'json' prints Home and Videos."""

    def test_serve_arguments(self):
        with mock.patch.object(dashboard, 'serve') as serve:
            dashboard.main(['serve'])                                       # what dashboard.cmd runs
            dashboard.main(['serve', '--dev', '--fixtures', '--port', '8791'])
            dashboard.main(['serve', '--dev', '--root', str(guard.TMP), '--port', '8792'])
        self.assertEqual([c.args for c in serve.call_args_list],
                         [(8790, False, None, False), (8791, True, None, True), (8792, True, guard.TMP, False)])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            dashboard.main(['serve', r'G:\My Drive\SINEM-YouTube'])

    def test_json(self):
        base = Path(tempfile.mkdtemp(prefix='cli-', dir=guard.TMP))
        root = fixtures.make_root(base)
        publish.set_registry({'instagram': fixtures.fake_platform(root)})
        dashboard.main(['json', str(root)])
        d = json.loads(self.out.getvalue()[self.out.getvalue().index('{'):])
        self.assertEqual(sorted(d), ['home', 'videos'])
        self.assertEqual(d['home']['me'], 'cli')
        self.assertEqual(len(d['videos']['videos']), 13)


def load_tests(loader, standard, pattern):
    """GuardWorks first (S25), then every other class of this module in the usual order."""
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(GuardWorks))
    for per_class in standard:
        if not any(isinstance(t, GuardWorks) for t in per_class):
            suite.addTest(per_class)
    return suite
