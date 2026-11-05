"""The test guard (S25: tests never open a non-loopback socket and never start cloudflared or schtasks) and the
fixture root of spec 7.9 that the other tests build on. Runs in the suite only (tests/__init__.py installs the guard):
python -I -m unittest discover -s <pipeline>\\tests -t <pipeline> -p test_guard.py"""
import contextlib, http.server, importlib.util, io, json, os, shutil, socket, stat, subprocess, sys, tempfile
import threading, time, types, unittest, urllib.request, webbrowser
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import httplib2
import requests
from PIL import Image

from . import fixtures, guard
import instagram as IG
import meta
import instagram_content as IGC
import tools
import youtube
import paths as shared_paths

NOWHERE = r'C:\nowhere-yt-tests'          # programs named in the start tests do not exist: a broken guard starts nothing
OUTSIDE = (('192.0.2.1', 443), ('graph.facebook.com', 443), ('graph.instagram.com', 443),
           ('www.googleapis.com', 443), ('8.8.8.8', 53))
CHILD = r'''
import json, socket, subprocess, sys
sys.path.insert(0, sys.argv[1])
{before}
from tests import guard
import instagram
blocked = []
for what, run in (('connect', lambda: socket.create_connection(('192.0.2.1', 443), timeout=1)),
                  ('popen', lambda: subprocess.run([sys.argv[2] + r'\schtasks.exe', '/query']))):
    try:
        run()
    except guard.NetworkBlocked:
        blocked.append(what)
print(json.dumps({'tmp': str(guard.TMP), 'work': str(guard.WORK), 'home': str(guard.HOME), 'ig_work': str(instagram.WORK),
                  'token': str(instagram.TOKEN), 'blocked': blocked, 'fake_call': instagram.call is guard._no_meta}))
'''


def child(before='', **env):
    """Run CHILD in a new Python (the guard installed by importing tests there); its JSON line."""
    r = subprocess.run([sys.executable, '-I', '-c', CHILD.replace('{before}', before), str(guard.PIPELINE), NOWHERE],
                       capture_output=True, text=True, encoding='utf-8', timeout=180, env={**os.environ, **env})
    if r.returncode:
        raise AssertionError(f'child failed ({r.returncode}):\n{r.stderr}')
    return json.loads(r.stdout.strip().splitlines()[-1])


class GuardWorks(unittest.TestCase):
    """S25 and the rest of the guard."""

    def setUp(self):
        guard.reset()
        self.addCleanup(guard.reset)

    def test_s25_outside_connect_raises(self):
        for host, port in OUTSIDE:
            with self.subTest(host=host):
                with socket.socket() as s, self.assertRaises(guard.NetworkBlocked):
                    s.connect((host, port))
                with socket.socket() as s, self.assertRaises(guard.NetworkBlocked):
                    s.connect_ex((host, port))
                with self.assertRaises(guard.NetworkBlocked):
                    socket.create_connection((host, port), timeout=1)
        try:
            six = socket.socket(socket.AF_INET6)
        except OSError:                                  # no IPv6 on this PC
            six = None
        if six:
            with six, self.assertRaises(guard.NetworkBlocked):
                six.connect(('2001:db8::1', 443, 0, 0))
        with socket.socket(type=socket.SOCK_DGRAM) as s, self.assertRaises(guard.NetworkBlocked):
            s.sendto(b'x', ('8.8.8.8', 53))
        for host in ('0.0.0.0', ''):                     # listening on every interface is refused too
            with socket.socket() as s, self.assertRaises(guard.NetworkBlocked):
                s.bind((host, 0))

    def test_s25_outside_lookup_raises(self):
        for name in ('graph.facebook.com', 'www.googleapis.com', 'example.com', '192.0.2.1'):
            for lookup in (socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex, socket.gethostbyaddr):
                with self.subTest(name=name, lookup=lookup.__name__), self.assertRaises(guard.NetworkBlocked):
                    lookup(name, 443) if lookup is socket.getaddrinfo else lookup(name)
        with self.assertRaises(guard.NetworkBlocked):
            socket.getnameinfo(('8.8.8.8', 53), 0)

    def test_s25_http_clients_fail_fast(self):
        """requests, urllib and httplib2 (the Google client) stop at once: no timeout, no retry, no proxy."""
        start = time.monotonic()
        for call in (lambda: requests.get('https://graph.instagram.com/v26.0/me', timeout=30),
                     lambda: requests.post('https://graph.facebook.com/v26.0/FAKE_IG_1/media', data={'a': 1}, timeout=30),
                     lambda: requests.Session().get('http://192.0.2.1/', timeout=30),
                     lambda: urllib.request.urlopen('https://www.googleapis.com/youtube/v3/videos', timeout=30),
                     lambda: httplib2.Http(timeout=30).request('https://www.googleapis.com/youtube/v3/channels')):
            with self.assertRaises(guard.NetworkBlocked):
                call()
        self.assertLess(time.monotonic() - start, 2)

    def test_s25_loopback_works(self):
        with socket.socket() as srv:
            srv.bind(('127.0.0.1', 0))
            srv.listen(1)
            with socket.create_connection(srv.getsockname(), timeout=5) as cli:
                conn, _ = srv.accept()
                with conn:
                    cli.sendall(b'ping')
                    self.assertEqual(conn.recv(4), b'ping')
        try:
            six = socket.socket(socket.AF_INET6)
            six.bind(('::1', 0))
        except OSError:                                  # no IPv6 on this PC
            six = None
        if six:
            with six:
                six.listen(1)
                with socket.create_connection(six.getsockname()[:2], timeout=5):
                    six.accept()[0].close()
        a, b = socket.socketpair()
        with a, b:
            a.sendall(b'x')
            self.assertEqual(b.recv(1), b'x')
        self.assertTrue(socket.getaddrinfo('localhost', 80))
        self.assertTrue(socket.getaddrinfo(None, 8080, flags=socket.AI_PASSIVE))     # a local wildcard: no lookup

        class Hello(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'hello')

            def log_message(self, *args):
                pass
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Hello)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            r = requests.get(f'http://127.0.0.1:{httpd.server_address[1]}/', timeout=10)
            self.assertEqual((r.status_code, r.text), (200, 'hello'))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_s25_loopback_names(self):
        for host in ('localhost', 'LocalHost.', '127.0.0.1', '::1', '[::1]', '::1%1', '::ffff:127.0.0.1', b'localhost'):
            self.assertTrue(guard._loopback(host), host)
        for host in ('127.0.0.2', '0.0.0.0', '', None, '::', 'localhost.example.com', 'fe80::1%3', '192.0.2.1',
                     'graph.facebook.com', 80):
            self.assertFalse(guard._loopback(host), host)

    def test_s25_programs_blocked(self):
        """Refused before anything starts, in every way of starting a program."""
        cf, st = fr'{NOWHERE}\cloudflared.exe', fr'{NOWHERE}\schtasks.exe'
        for args, kw in (([cf, 'tunnel', '--url', 'http://127.0.0.1:9'], {}), ([st, '/query'], {}),
                         (f'"{cf}" --version', {}), (f'{st} /query', {'shell': True}),
                         (['cmd', '/c', f'{st} /query'], {}), ([sys.executable, '-c', 'pass'], {'executable': cf})):
            with self.subTest(args=args), self.assertRaises(guard.NetworkBlocked):
                subprocess.run(args, capture_output=True, timeout=30, **kw)
        with self.assertRaises(guard.NetworkBlocked):
            subprocess.Popen(args=[cf])
        with self.assertRaises(guard.NetworkBlocked):
            subprocess.check_output(f'{cf} tunnel list', shell=True)
        with self.assertRaises(guard.NetworkBlocked):
            os.system(f'{st} /query')
        if hasattr(os, 'startfile'):
            with self.assertRaises(guard.NetworkBlocked):
                os.startfile(fr'{NOWHERE}\clip.mp4')
        with mock.patch('webbrowser.get', side_effect=AssertionError('a browser would start')):
            for open_ in (webbrowser.open, webbrowser.open_new, webbrowser.open_new_tab):
                with self.assertRaises(guard.NetworkBlocked):
                    open_('about:blank')
        r = subprocess.run([sys.executable, '-I', '-c', 'print(6 * 7)'], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), '42')                     # other programs still run

    def test_s25_program_names(self):
        """Every spelling of the names, also inside cmd and PowerShell (only checked here, never started)."""
        for args in (['cloudflared', '--version'], ['CLOUDFLARED.EXE'], [str(IG.CLOUDFLARED), 'tunnel', 'run'],
                     ['schtasks', '/run', '/tn', 'FAKE'], [r'C:\Windows\System32\schtasks.exe', '/query'],
                     'schtasks /query', '"C:\\Program Files\\cloudflared\\cloudflared.exe" tunnel run',
                     ['cmd', '/c', 'schtasks /end /tn FAKE'], 'cmd /c "start cloudflared"', 'cmd /c "echo hi&&schtasks"',
                     ['pwsh', '-Command', 'Start-ScheduledTask FAKE'], ['powershell', '-c', "& 'C:\\x\\cloudflared.exe'"],
                     ['powershell.exe', '-c', 'Get-ScheduledTask'], [b'schtasks'], ['claude', '-p', 'Reply with OK'],
                     [r'C:\Users\x\.local\bin\claude.exe', '--version']):
            with self.subTest(args=args), self.assertRaises(guard.NetworkBlocked):
                guard._program(args, None, False)
        with self.assertRaises(guard.NetworkBlocked):
            guard._program('echo hi && schtasks /query', None, True)
        for args in (['findstr', 'cloudflared', 'watch.log'], ['ffprobe', 'schtasks.mp4'], 'notepad cloudflared.yml',
                     [sys.executable, '-c', 'pass']):
            guard._program(args, None, False)                         # a program's arguments are not checked

    def test_s25_environment_points_into_tmp(self):
        import easy, llm
        self.assertEqual(Path(os.environ['PIPELINE_WORK']), guard.WORK)
        self.assertEqual(Path(os.environ['USERPROFILE']), guard.HOME)
        self.assertEqual(Path(os.environ['HOME']), guard.HOME)
        self.assertEqual(Path.home(), guard.HOME)
        paths = {'guard.WORK': guard.WORK, 'guard.HOME': guard.HOME, 'instagram.KEYS': IG.KEYS,
                 'instagram.TOKEN': IG.TOKEN, 'instagram.WORK': IG.WORK, 'instagram.PAUSED': IG.PAUSED,
                 'instagram.POSTING': IG.POSTING, 'youtube.KEYS': youtube.KEYS, 'easy.WORK_ROOT': easy.WORK_ROOT,
                 'paths.WORK': shared_paths.WORK, 'paths.KEYS': shared_paths.KEYS,
                 'llm.KEY_FILE': llm.KEY_FILE, **{f'llm.CLI_TOKENS[{i}]': p for i, p in enumerate(llm.CLI_TOKENS)},
                 **{f'youtube.{n}': getattr(youtube, n) for n in ('CLIENT', 'TOKEN') if hasattr(youtube, n)}}
        for name, p in paths.items():
            with self.subTest(name):
                self.assertTrue(guard._inside(p, guard.TMP), p)
        self.assertTrue((guard.TMP / guard.MARK).is_file())
        for real in (guard.REAL_WORK, Path(r'D:\code\YT')):
            self.assertFalse(guard._inside(guard.TMP, real))
        self.assertFalse({'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'} &
                         {k.upper() for k in os.environ})
        self.assertEqual((os.environ['NO_PROXY'], os.environ['no_proxy']), ('*', '*'))
        self.assertEqual(llm.backend(), 'manual')                   # Claude is never asked

    def test_s25_meta_and_youtube_blocked_unscripted(self):
        with self.assertRaises(guard.NetworkBlocked):
            IG.call('GET', 'me', fields='user_id,username')
        video = guard.WORK / 'share-test.mp4'
        video.write_bytes(b'')
        with self.assertRaises(guard.NetworkBlocked):
            with IG.Share([video]):
                self.fail('Share must not open')
        for kw in ({}, {'interactive': True}):
            with self.assertRaises(guard.NetworkBlocked):
                youtube.api(**kw)
        IG.KEYS.mkdir(parents=True, exist_ok=True)                   # a made-up key in HOME: check() calls Meta
        IG.TOKEN.write_text(json.dumps({'mode': 'unified', 'token': 'FAKE_TOKEN', 'user_id': 'FAKE_IG_1',
                                        'username': 'fake_account'}), encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(guard.NetworkBlocked):
            IG.check()

    def test_s25_real_share_cannot_start_cloudflared(self):
        """Even the real instagram.Share stops at the Popen guard (its local server is closed again here)."""
        video = guard.WORK / 'share-test.mp4'
        video.write_bytes(b'')
        share = guard.REAL['instagram.Share']([video])
        with mock.patch.object(meta, 'CLOUDFLARED', Path(NOWHERE) / 'cloudflared.exe'):
            try:
                with self.assertRaises(guard.NetworkBlocked):
                    share.__enter__()
            finally:
                if getattr(share, 'httpd', None):
                    share.httpd.shutdown()
                    share.httpd.server_close()
                if getattr(share, 'tmp', None):
                    shutil.rmtree(share.tmp, ignore_errors=True)

    def test_script_meta(self):
        calls = guard.script_meta([{'id': 'FAKE_CONTAINER_1'}, ValueError('made-up failure'),
                                   {'status_code': 'FINISHED'}, RuntimeError, {'id': 'FAKE_MEDIA_9'}])
        self.assertEqual(IG.call('POST', 'FAKE_IG_1/media', media_type='REELS', video_url='https://x.example/v.mp4'),
                         {'id': 'FAKE_CONTAINER_1'})
        with self.assertRaisesRegex(ValueError, 'made-up failure'):
            IG.call('GET', 'FAKE_CONTAINER_1', fields='status_code')
        answer = IG.call('GET', 'FAKE_CONTAINER_1', fields='status_code')
        answer['status_code'] = 'changed'                            # a copy: the script stays as it was
        with self.assertRaises(RuntimeError):
            IG.call('GET', 'FAKE_CONTAINER_1', fields='status_code')
        self.assertEqual(IG.call('POST', 'FAKE_IG_1/media_publish', files={'f': b'x'}, creation_id='FAKE_CONTAINER_1'),
                         {'id': 'FAKE_MEDIA_9'})
        with self.assertRaises(guard.NetworkBlocked):                # no answers left
            IG.call('GET', 'me')
        self.assertEqual([c[:2] for c in calls], [('POST', 'FAKE_IG_1/media'), ('GET', 'FAKE_CONTAINER_1'),
                                                 ('GET', 'FAKE_CONTAINER_1'), ('GET', 'FAKE_CONTAINER_1'),
                                                 ('POST', 'FAKE_IG_1/media_publish'), ('GET', 'me')])
        self.assertEqual(calls[0][2], {'media_type': 'REELS', 'video_url': 'https://x.example/v.mp4'})
        self.assertEqual(calls[4][2], {'creation_id': 'FAKE_CONTAINER_1', 'files': {'f': b'x'}})
        guard.reset()
        with self.assertRaises(guard.NetworkBlocked):
            IG.call('GET', 'me')

    def test_script_meta_reaches_module_functions(self):
        IG.KEYS.mkdir(parents=True, exist_ok=True)
        IG.TOKEN.write_text(json.dumps({'mode': 'unified', 'token': 'FAKE_TOKEN', 'user_id': 'FAKE_IG_1',
                                        'username': 'fake_account'}), encoding='utf-8')
        calls = guard.script_meta([{'username': 'fake_account', 'media_count': 3, 'followers_count': 10},
                                   {'data': [{'quota_usage': 1, 'config': {'quota_total': 100}}]}])
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(IG.check(), {'user_id': 'FAKE_IG_1', 'username': 'fake_account'})
        self.assertIn('1 of 100', printed.getvalue())
        self.assertEqual([c[1] for c in calls], ['FAKE_IG_1', 'FAKE_IG_1/content_publishing_limit'])

    def test_fake_share(self):
        files = [guard.WORK / 'a.mp4', guard.WORK / 'a.jpg']
        guard.fake_share()
        with IG.Share(files) as urls:
            self.assertEqual(urls, ['https://fake.trycloudflare.com/k/video.mp4',
                                    'https://fake.trycloudflare.com/k/cover.jpg'])
        self.assertEqual(IG.Share.shared, [files])
        guard.fake_share(('https://x.example/v.mp4', 'https://x.example/c.jpg'))
        with IG.Share(files) as urls:
            self.assertEqual(urls, ['https://x.example/v.mp4', 'https://x.example/c.jpg'])
        guard.reset()
        self.assertIs(IG.Share, guard.BlockedShare)

    def test_reset(self):
        for folder in (guard.WORK, guard.HOME):
            (folder / 'sub' / 'deeper').mkdir(parents=True)
            for f in (folder / 'sub' / 'deeper' / 'f.txt', folder / 'read-only.txt', folder / 'sub' / 'read-only.txt'):
                f.write_text('x', encoding='utf-8')
                if f.name == 'read-only.txt':
                    os.chmod(f, stat.S_IREAD)
        seen, old = [], sys.modules.get('publish')
        sys.modules['publish'] = types.SimpleNamespace(set_registry=seen.append)
        try:
            guard.reset()
        finally:
            if old is None:
                sys.modules.pop('publish', None)
            else:
                sys.modules['publish'] = old
        self.assertEqual(seen, [None])
        for folder in (guard.WORK, guard.HOME):
            self.assertTrue(folder.is_dir())
            self.assertEqual(list(folder.iterdir()), [])
        IG.call, youtube.api = print, print
        guard.reset()
        self.assertEqual((IG.call, IG.Share, youtube.api), (guard._no_meta, guard.BlockedShare, guard._no_youtube))

    def test_install_is_idempotent(self):
        def state():
            return (socket.socket.connect, socket.getaddrinfo, subprocess.Popen.__init__, os.system, tools.probe,
                    IGC.probe, IG.call, guard.TMP, guard.WORK, guard.HOME)
        before, env = state(), dict(os.environ)
        guard.install()
        for a, b in zip(before, state()):
            self.assertIs(a, b)
        self.assertEqual(dict(os.environ), env)

    def test_probe(self):
        f = guard.WORK / 'new-short-6.mp4'
        f.write_bytes(b'')
        p = tools.probe(f)
        self.assertEqual(p, fixtures.PROBES['new-short-6.mp4'])
        self.assertEqual(set(p), {'duration', 'width', 'height', 'fps', 'hdr', 'transfer', 'audio', 'created'})
        p['duration'] = 1
        self.assertEqual(tools.probe(str(f))['duration'], 30.03)     # answers are copies
        self.assertIs(IGC.probe, guard._probe)
        unknown = guard.WORK / 'not-in-probes.mp4'
        unknown.write_bytes(b'')
        with self.assertRaisesRegex(RuntimeError, 'PROBES'):
            tools.probe(unknown)
        with self.assertRaisesRegex(RuntimeError, 'No such file'):
            tools.probe(guard.WORK / 'missing.mp4')
        seen = []
        with mock.patch.dict(guard.REAL, {'tools.probe': lambda path: seen.append(path) or {'real': True}}):
            self.assertEqual(tools.probe(fr'{NOWHERE}\clip.mp4'), {'real': True})    # elsewhere: the real ffprobe
        self.assertEqual(seen, [fr'{NOWHERE}\clip.mp4'])

    def test_probe_in_dashboard(self):
        import dashboard
        self.assertIs(dashboard.probe, guard._probe)

    def test_child_process_shares_tmp(self):
        c = child()
        self.assertEqual((Path(c['tmp']), Path(c['work']), Path(c['home'])), (guard.TMP, guard.WORK, guard.HOME))
        self.assertEqual(Path(c['token']), guard.HOME / '.shorts-pipeline' / 'instagram.json')
        self.assertEqual(c['blocked'], ['connect', 'popen'])
        self.assertTrue(c['fake_call'])
        self.assertTrue((guard.TMP / guard.MARK).is_file())          # the child left TMP to its parent

    def test_child_with_modules_imported_first(self):
        """dashboard.py serve --dev imports pipeline modules before the guard: their scratch PIPELINE_WORK stays,
        their key paths move into HOME."""
        scratch, home = guard.WORK / 'dev-work', guard.WORK / 'dev-home'
        c = child('import instagram', PIPELINE_WORK=str(scratch), USERPROFILE=str(home), HOME=str(home))
        self.assertEqual((Path(c['work']), Path(c['ig_work']), Path(c['home'])), (scratch, scratch, guard.HOME))
        self.assertEqual(Path(c['token']), guard.HOME / '.shorts-pipeline' / 'instagram.json')
        self.assertEqual(c['blocked'], ['connect', 'popen'])

    def test_modules_imported_first_are_checked(self):
        early = types.ModuleType('early_example')
        early.WORK, early.LOG = guard.REAL_WORK, guard.REAL_WORK / 'watch.log'
        with self.assertRaisesRegex(RuntimeError, 'real work folder'):
            guard._redirect([early], guard.WORK / 'old-home' / '.shorts-pipeline')
        old, keys = guard.WORK / 'old-home' / '.shorts-pipeline', guard.HOME / '.shorts-pipeline'
        early = types.ModuleType('early_example')
        early.KEYS, early.TOKEN, early.TOKENS, early.OTHER = old, old / 'a.json', [old / 'b', Path(NOWHERE)], guard.WORK
        guard._redirect([early], old)
        self.assertEqual((early.KEYS, early.TOKEN, early.TOKENS, early.OTHER),
                         (keys, keys / 'a.json', [keys / 'b', Path(NOWHERE)], guard.WORK))

    def test_F22_namespace_spellings(self):
        """\\\\?\\, \\\\.\\, \\??\\, '//?/' and stream suffixes name the same folder: _inside sees through them."""
        real, B = str(guard.REAL_WORK), '\\'
        for p in (B * 2 + '?' + B + real, B * 2 + '.' + B + real + r'\publish.db', B + '??' + B + real.lower(),
                  '//?/' + real.replace(B, '/'), real + '::$INDEX_ALLOCATION', real + r':$I30:$INDEX_ALLOCATION\x',
                  B * 2 + '?' + B + real + B):
            with self.subTest(p=p):
                self.assertTrue(guard._inside(p, guard.REAL_WORK))
        self.assertTrue(guard._inside(guard.REAL_WORK, B * 2 + '?' + B + real))           # either side
        self.assertFalse(guard._inside(B * 2 + '?' + B + real + '-2', guard.REAL_WORK))
        self.assertEqual(guard._plain(B * 2 + r'?\UNC\srv\share\x'), B * 2 + r'srv\share\x')
        for p, odd in ((B * 2 + r'?\GLOBALROOT\Device\HarddiskVolume3\x', True), (B * 2 + r'srv\share\x', True),
                       (B * 2 + r'.\Volume{0000}\x', True), (B * 2 + '?' + B + real, False), (real, False),
                       ('relative\\x', False)):
            self.assertEqual(guard._odd(p), odd, p)
        early = types.ModuleType('early_example')
        early.WORK = Path(B * 2 + '?' + B + real)
        with self.assertRaisesRegex(RuntimeError, 'real work folder'):
            guard._redirect([early], guard.WORK / 'old-home' / '.shorts-pipeline')

    def test_F22_child_refuses_a_namespace_work_folder(self):
        """dashboard --dev's path: modules imported first with a PIPELINE_WORK the guard cannot vouch for."""
        work = '\\\\?\\GLOBALROOT\\Device\\NoSuchDevice\\x'
        r = subprocess.run([sys.executable, '-I', '-c', CHILD.replace('{before}', 'import instagram'),
                            str(guard.PIPELINE), NOWHERE], capture_output=True, text=True, encoding='utf-8',
                           timeout=180, env={**os.environ, 'PIPELINE_WORK': work})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('network or namespace path', r.stderr)


class FixtureRoot(unittest.TestCase):
    """fixtures.make_root() and friends, read by the real pipeline code."""

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix='fixture-', dir=guard.TMP))
        cls.root = fixtures.make_root(cls.base)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        guard.reset()

    def out(self, lesson, output='Output'):
        return self.root / lesson / output

    def test_layout(self):
        import easy
        r = self.root
        self.assertEqual(r, self.base / 'SINEM-YouTube')
        self.assertEqual(sorted(p.name for p in r.iterdir()), sorted(
            ['INPUT', 'STATUS.txt', 'Sich interessieren für', 'Um etwas bitten', 'ZU + INFINITIV', '_channel', 'new']))
        self.assertTrue(all(p.suffix in ('.jpg', '.png') for p in (r / 'INPUT').iterdir()))
        channel = IG.profile(r)
        self.assertEqual(channel['instagram'], {'start': '2026-11-03'})
        self.assertEqual(IG.hashtags(r), ['#almanca', '#almancaöğren', '#deutschlernen'])
        self.assertTrue((r / '_channel' / channel['logo']).is_file())
        new = self.out('new')
        self.assertEqual(sorted(f.name for f in new.glob('*.mp4')),
                         sorted(['new-video.mp4'] + [f'new-short-{k}.mp4' for k in range(1, 10)]))
        for name in (['youtube.json', 'status.json', 'STATUS.txt', 'instagram-posted.json', 'youtube-applied.json',
                      'thumbnail-short.jpg'] + [f'thumbnail-{i}.jpg' for i in (1, 2, 3)] +
                     [f'cover-short-{k}.jpg' for k in range(1, 10)]):
            self.assertTrue((tools.extras(new) / name).is_file(), name)
        for lesson, slug in (('Um etwas bitten', 'um-etwas-bitten'), ('Sich interessieren für', 'sich-interessieren-f-r')):
            x = tools.extras(self.out(lesson))
            self.assertEqual([f.name for f in self.out(lesson).glob('*.mp4')], [f'{slug}-video.mp4'])
            self.assertTrue((x / 'thumbnail-short.jpg').is_file())
            self.assertEqual(list(x.glob('cover-short-*.jpg')), [])
            self.assertFalse((x / 'instagram-posted.json').exists())
        zu = r / 'ZU + INFINITIV'
        self.assertEqual([p.name for p in (zu / 'Output').iterdir()], ['zu-infinitiv-short.mp4'])    # by hand
        self.assertEqual([f.name for f in (zu / 'Output (Easy)').glob('*.mp4')], ['zu-infinitiv-video.mp4'])
        self.assertEqual(json.loads((zu / 'settings.json').read_text(encoding='utf-8')), {'short': False})
        self.assertEqual((easy.output_dir(zu), easy.output_dir(r / 'new')), (zu / 'Output (Easy)', new))
        for lesson in ('new', 'Um etwas bitten', 'Sich interessieren für', 'ZU + INFINITIV'):
            self.assertEqual(len(easy.lesson_videos(r / lesson)), 1, lesson)                  # the recording

    def test_videos_empty_covers_valid(self):
        self.assertTrue(all(f.stat().st_size == 0 for f in self.root.rglob('*.mp4')))
        images = list(self.root.rglob('*.jpg'))
        self.assertEqual(len(images), 25)
        for f in images:
            with Image.open(f) as im:
                self.assertEqual(im.format, 'JPEG', f)
                im.verify()
        with Image.open(tools.extras(self.out('new')) / 'cover-short-6.jpg') as im:
            self.assertEqual(im.size, (90, 160))

    def test_natural_order(self):
        """Spec 5.1 order (earliest *.mp4 per output folder): new, Um, Sich. Um's youtube.json is newer than Sich's,
        so the old order by youtube.json (instagram.queue) puts Sich first: the bug this fixture keeps."""
        first = {l: min(f.stat().st_mtime for f in self.out(l).glob('*.mp4'))
                 for l in ('new', 'Um etwas bitten', 'Sich interessieren für')}
        self.assertLess(first['new'], first['Um etwas bitten'])
        self.assertLess(first['Um etwas bitten'], first['Sich interessieren für'])
        made = lambda l: (tools.extras(self.out(l)) / 'youtube.json').stat().st_mtime
        self.assertGreater(made('Um etwas bitten'), made('Sich interessieren für'))
        old = [l.name for l, _, p in sorted(tools.records(self.root, 'youtube.json'), key=lambda r: r[2].stat().st_mtime)
               if not l.name.startswith('_')]                         # v1's instagram.queue (removed in v2)
        self.assertEqual(old, ['new', 'ZU + INFINITIV', 'Sich interessieren für', 'Um etwas bitten'])
        self.assertEqual(list(dict.fromkeys(i['lesson'] for i in fixtures.reel_items(self.root))),
                         ['new', 'Um etwas bitten', 'Sich interessieren für'])           # the v2 order (spec 5.1)
        shorts = sorted(self.out('new').glob('new-short-*.mp4'), key=lambda f: f.stat().st_mtime)
        self.assertEqual([f.name for f in shorts], [f'new-short-{k}.mp4' for k in range(1, 10)])

    def test_status_files(self):
        for lesson, output in (('new', 'Output'), ('Um etwas bitten', 'Output'), ('Sich interessieren für', 'Output'),
                               ('ZU + INFINITIV', 'Output (Easy)')):
            with self.subTest(lesson):
                st = json.loads((tools.extras(self.out(lesson, output)) / 'status.json').read_text(encoding='utf-8'))
                self.assertEqual(st['state'], 'done')
                self.assertLessEqual({'state', 'step', 'updated', 'inputs', 'error', 'youtube', 'youtube_complete',
                                      'report'}, set(st))
                self.assertRegex(st['updated'], r'^\d{4}-\d\d-\d\d \d\d:\d\d$')
                self.assertEqual(st['inputs'], [[f'{tools.slug(lesson)}-take-1.mov', 0]])
        zu = json.loads((tools.extras(self.out('ZU + INFINITIV', 'Output (Easy)')) / 'status.json').read_text('utf-8'))
        self.assertTrue(zu['youtube_complete'])
        self.assertIn('https://youtu.be/FAKE_yt_013', zu['youtube'])

    def test_posted_records(self):
        rec = IG.load(self.out('new'))
        self.assertEqual({k: v['id'] for k, v in rec.items() if not k.startswith('_')},
                         {f'new-short-{k}.mp4': f'FAKE_MEDIA_{k}' for k in range(1, 6)})
        for k in range(1, 6):
            r = rec[f'new-short-{k}.mp4']
            self.assertEqual(r['permalink'], f'https://www.instagram.com/reel/FAKE{k}/')
            self.assertEqual(datetime.fromisoformat(r['posted_utc']), fixtures.NEW_POSTED[k - 1])
            self.assertEqual(r['posted'], fixtures.NEW_POSTED[k - 1].strftime('%Y-%m-%d %H:%M'))
        self.assertIn('long', rec['_facebook'])
        self.assertEqual((IG.load(self.out('Um etwas bitten')), IG.load(self.out('Sich interessieren für'))), ({}, {}))
        self.assertEqual(IG.last_post(self.root), fixtures.LAST_POST)
        times = [datetime.fromisoformat(t) for _, _, p in tools.records(self.root, 'instagram-posted.json')
                 for r in json.loads(p.read_text(encoding='utf-8')).values()
                 for t in (r.get('posted_utc'), r.get('facebook', {}).get('posted_utc')) if t]
        self.assertEqual(len(times), 7)                                # 5 Reels + 2 on the old Facebook Page
        self.assertEqual(sum(timedelta(0) <= fixtures.NOW - t < timedelta(hours=24) for t in times), 1)

    def test_lesson_files_and_reels(self):
        _, out, data = IG.lesson_files(self.root / 'new')
        self.assertEqual(out, self.out('new'))
        reels = IG.reels(out, data)
        self.assertEqual([f.name for f, _ in reels], [f'new-short-{k}.mp4' for k in range(1, 10)])
        self.assertEqual([e['version'] for _, e in reels], [f'short-{k}' for k in range(1, 10)])
        self.assertTrue(all(IG.cover_path(f).is_file() for f, _ in reels))
        cap = IG.caption(reels[6][1], IG.hashtags(self.root))
        self.assertTrue(cap.startswith("nämlich: konuşmada 'çünkü'\n"), cap)
        self.assertIn(IG.YOUTUBE_LINE, cap)
        for name, slug, seconds, texts in (('Um etwas bitten', 'um-etwas-bitten', 68.04, fixtures.UM_TEXTS),
                                           ('Sich interessieren für', 'sich-interessieren-f-r', 51.04, fixtures.SICH_TEXTS)):
            with self.subTest(name):
                _, out, data = IG.lesson_files(self.root / name)
                (f, entry), = IG.reels(out, data)
                self.assertEqual(f.name, f'{slug}-video.mp4')
                self.assertEqual((entry['version'], entry['whole'], entry['title']), ('video', True, texts['titles'][0]))
                self.assertEqual(tools.probe(f)['duration'], seconds)
                self.assertTrue(IG.cover_path(f).is_file())
                cap = IG.caption(entry, IG.hashtags(self.root))
                self.assertNotIn('🔔', cap)
                self.assertNotIn(IG.YOUTUBE_LINE, cap)
        _, out, data = IG.lesson_files(self.root / 'ZU + INFINITIV')
        self.assertEqual(out.name, 'Output (Easy)')
        self.assertEqual(IG.reels(out, data), [])                      # a 197 s horizontal video is no Reel
        for name in ('INPUT', '_channel'):
            with self.assertRaises(StopIteration):
                IG.lesson_files(self.root / name)

    def test_probe_answers(self):
        new = self.out('new')
        self.assertEqual([round(tools.probe(new / f'new-short-{k}.mp4')['duration']) for k in range(6, 10)],
                         [30, 34, 48, 53])
        for f, seconds, vertical in ((new / 'new-video.mp4', 392, False),
                                     (self.out('ZU + INFINITIV', 'Output (Easy)') / 'zu-infinitiv-video.mp4', 197, False),
                                     (self.out('ZU + INFINITIV') / 'zu-infinitiv-short.mp4', 45, True)):
            p = tools.probe(f)
            self.assertEqual((round(p['duration']), p['height'] > p['width']), (seconds, vertical), f.name)
        self.assertTrue(tools.probe(self.root / 'new' / 'new-take-1.mov')['hdr'])

    def test_reel_items(self):
        items = fixtures.reel_items(self.root)
        self.assertEqual([i['key'] for i in items], list(fixtures.POSTED_KEYS + fixtures.WAITING))
        self.assertEqual(set(items[0]), {'key', 'lesson', 'out', 'file', 'part', 'title', 'seconds', 'rank', 'entry',
                                         'cover'})
        waiting = [i for i in items if 'id' not in IG.load(i['out']).get(i['file'].name, {})]
        self.assertEqual([i['key'] for i in waiting], list(fixtures.WAITING))
        self.assertEqual([(i['part'], i['seconds']) for i in waiting],
                         [(6, 30), (7, 34), (8, 48), (9, 53), (None, 68), (None, 51)])
        self.assertEqual(waiting[1]['title'], "nämlich: konuşmada 'çünkü'")
        self.assertEqual(waiting[4]['title'], fixtures.UM_TEXTS['titles'][0])
        self.assertTrue(all(i['cover'] and i['cover'].is_file() for i in items))
        self.assertEqual([i['rank'] for i in items], sorted(i['rank'] for i in items))
        self.assertEqual((items[0]['rank'][1:], waiting[4]['rank'][1:]),
                         ((1, 'new-short-1.mp4'), (0, 'um-etwas-bitten-video.mp4')))

    def test_youtube_states(self):
        """dashboard.items(): ready 1 (new-short-9), uploaded 2 (new-short-8, Sich), scheduled 2 (new-short-6, 7),
        public 8 (new's video, new-short-1..5, Um, ZU by hand)."""
        import dashboard
        found = dashboard.items(self.root, {})
        self.assertEqual(Counter(i['state'] for i in found), Counter(ready=1, uploaded=2, scheduled=2, public=8))
        by = {i['file']: i for i in found}
        self.assertEqual([by[f'new-short-{k}.mp4']['state'] for k in (6, 7, 8, 9)],
                         ['scheduled', 'scheduled', 'uploaded', 'ready'])
        self.assertEqual(by['sich-interessieren-f-r-video.mp4']['state'], 'uploaded')
        self.assertEqual((by['zu-infinitiv-video.mp4']['state'], by['zu-infinitiv-video.mp4']['youtube']),
                         ('public', 'FAKE_yt_013'))
        self.assertEqual(sorted(f for f, i in by.items() if 'instagram' in i), [f'new-short-{k}.mp4' for k in range(1, 6)])
        self.assertLessEqual({i['youtube'] for i in found if 'youtube' in i}, set(fixtures.YT_VIDEOS))

    def test_status_page(self):
        import overview
        text = overview.build(self.root, idle=True)
        self.assertIn('Instagram: 5 of 9 Reels posted', text)
        self.assertEqual(text.count('Instagram: 0 of 1 Reels posted'), 2)
        self.assertIn('Shorts: 5 of 9 public', text)
        self.assertIn('YouTube: Made by hand: https://youtu.be/FAKE_yt_013', text)

    def test_fake_youtube_api(self):
        yt = fixtures.fake_youtube_api(fixtures.YT_VIDEOS, fixtures.YT_CHANNEL)
        (item,) = yt.videos().list(part='snippet,statistics,status', id='FAKE_yt_001,FAKE_yt_999').execute()['items']
        self.assertEqual((item['id'], item['statistics']['viewCount'], item['status']['privacyStatus'],
                          item['snippet']['title']), ('FAKE_yt_001', '380', 'public', 'weil fiili sona atar'))
        stats = yt.channels().list(part='statistics', mine=True).execute()['items'][0]['statistics']
        self.assertEqual(stats['subscriberCount'], '143')
        self.assertEqual(yt.calls, [('videos', {'part': 'snippet,statistics,status', 'id': 'FAKE_yt_001,FAKE_yt_999'}),
                                    ('channels', {'part': 'statistics', 'mine': True})])
        raw = fixtures.fake_youtube_api({'FAKE_yt_100': {'statistics': {'viewCount': '7'}}},
                                        {'statistics': {'subscriberCount': '9'}})
        self.assertEqual(raw.videos().list(id='FAKE_yt_100').execute()['items'],
                         [{'id': 'FAKE_yt_100', 'statistics': {'viewCount': '7'}}])
        self.assertEqual(raw.channels().list(mine=True).execute()['items'][0]['statistics'], {'subscriberCount': '9'})
        from googleapiclient.errors import HttpError
        down = fixtures.fake_youtube_api(fixtures.YT_VIDEOS, None)
        for call in (down.videos().list(id='FAKE_yt_001').execute, down.channels().list(mine=True).execute):
            with self.assertRaises(HttpError) as cm:
                call()
            self.assertIn('quotaExceeded', str(cm.exception))
            self.assertEqual(cm.exception.resp.status, 403)
        self.assertEqual(len(down.calls), 2)
        ids = {r['id'] for _, _, p in tools.records(self.root, 'youtube-applied.json')
               for r in json.loads(p.read_text(encoding='utf-8')).values()}
        self.assertEqual(ids | {'FAKE_yt_013'}, set(fixtures.YT_VIDEOS))     # every record has an answer

    def test_fake_platform(self):
        if importlib.util.find_spec('platform_fake') is None:
            self.skipTest('platform_fake.py is not there yet (Dev A)')
        fake = fixtures.fake_platform(self.root)
        items, problems = fake.candidates(self.root)
        self.assertEqual(problems, [])
        self.assertEqual([i.key for i in items], list(fixtures.POSTED_KEYS + fixtures.WAITING))
        self.assertEqual([i.key for i in items if not i.posted], list(fixtures.WAITING))
        self.assertTrue((guard.WORK / 'fake-records.json').is_file())

    def test_write_lesson(self):
        import dashboard
        r = Path(tempfile.mkdtemp(prefix='lessons-', dir=guard.TMP)) / 'SINEM-YouTube'
        self.addCleanup(shutil.rmtree, r.parent, True)
        start = fixtures._ts(2026, 11, 1, 12, 0)
        out = fixtures.write_lesson(r, 'Dativ Fiiller', shorts=2, posted=(1,), video_mtime=start)
        self.assertEqual(out, r / 'Dativ Fiiller' / 'Output')
        self.assertEqual(sorted(f.name for f in out.glob('*.mp4')),
                         ['dativ-fiiller-short-1.mp4', 'dativ-fiiller-short-2.mp4', 'dativ-fiiller-video.mp4'])
        self.assertEqual(min(f.stat().st_mtime for f in out.glob('*.mp4')), start)
        self.assertEqual(IG.load(out)['dativ-fiiller-short-1.mp4']['id'], 'FAKE_MEDIA_1')
        _, o, data = IG.lesson_files(r / 'Dativ Fiiller')
        self.assertEqual([f.name for f, _ in IG.reels(o, data)], ['dativ-fiiller-short-1.mp4', 'dativ-fiiller-short-2.mp4'])
        self.assertEqual(tools.probe(out / 'dativ-fiiller-video.mp4')['width'], 1920)
        one = fixtures.write_lesson(r, 'Tek Video', whole=(40, True), posted=(0,))
        self.assertEqual(IG.load(one)['tek-video-video.mp4']['id'], 'FAKE_MEDIA_2')     # ids go on in the root
        self.assertTrue((tools.extras(one) / 'thumbnail-short.jpg').is_file())
        self.assertEqual(fixtures.write_lesson(r, 'Kolay', whole=(70, True), output='Output (Easy)').name, 'Output (Easy)')
        for state in ('working', 'failed', 'waiting'):
            st = json.loads((tools.extras(fixtures.write_lesson(r, f'Ders {state}', whole=(50, True), state=state)) /
                             'status.json').read_text(encoding='utf-8'))
            self.assertEqual((st['state'], bool(st['error'])), (state, state == 'failed'))
        self.assertFalse(fixtures.write_lesson(r, 'Yeni Kayıt', state='new').exists())
        self.assertEqual([p.name for p in (r / 'Yeni Kayıt').iterdir()], ['yeni-kay-t-take-1.mov'])
        self.assertEqual({i['lesson']: i['state'] for i in dashboard.items(r, {}) if i['kind'] == 'lesson'},
                         {'Ders working': 'making', 'Ders failed': 'stopped', 'Ders waiting': 'waiting',
                          'Yeni Kayıt': 'new'})
        fixtures.write_lesson(r, 'Tek Video', whole=(40, True))       # again without posts: the record is gone
        self.assertEqual(IG.load(one), {})
        for kw in ({'shorts': 2, 'posted': (3,)}, {'posted': (0,)}, {'state': 'finished'}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                fixtures.write_lesson(r, 'Bad', **kw)
        self.assertFalse((r / 'Bad').exists())

    def test_make_root_again(self):
        """A second make_root() on the same base rebuilds the root in place, also after a test changed it."""
        base = Path(tempfile.mkdtemp(prefix='fixture-again-', dir=guard.TMP))
        self.addCleanup(shutil.rmtree, base, True)
        root = fixtures.make_root(base)
        self.addCleanup(fixtures.PROBES.update, {k: dict(v) for k, v in fixtures._BASE.items()})

        def snapshot():
            return {str(p.relative_to(root)): (p.stat().st_mtime, p.read_bytes() if p.suffix == '.json' else None)
                    for p in root.rglob('*') if p.suffix in ('.json', '.mp4', '.mov')}
        first = snapshot()
        fixtures.write_lesson(root, 'new', shorts=2)
        self.assertEqual(fixtures.PROBES['new-video.mp4']['duration'], 300.0)
        self.assertEqual(fixtures.make_root(base), root)
        self.assertEqual(snapshot(), first)
        self.assertEqual(fixtures.PROBES['new-video.mp4']['duration'], 392.4)

    def test_refuses_real_places(self):
        for base in (Path(r'G:\My Drive'), Path(r'j:\my drive\SINEM-YouTube'), guard.REAL_WORK / 'x',
                     Path(r'D:\code\YT\pipeline')):
            with self.subTest(base=base):
                with self.assertRaises(ValueError):
                    fixtures.make_root(base)
                with self.assertRaises(ValueError):
                    fixtures.write_lesson(base, 'Lesson')

    def test_roots_outside_tmp_are_registered(self):
        outside = Path(NOWHERE) / 'fixture'                          # only named, never created
        with mock.patch.object(guard, 'FAKE_ROOTS', []):
            for p in (outside, outside, guard.TMP / 'inside'):
                fixtures._register(p)
            self.assertEqual(guard.FAKE_ROOTS, [outside.resolve()])
