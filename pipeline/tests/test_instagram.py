"""instagram.py and platform_instagram.py (spec 7.4, 7.5; safety rules S11 (CLI), S19 and S27 of spec 10).
Offline: Meta answers come from guard.script_meta(), the public link from guard.fake_share(), videos from the
fixture root; the clock is publish._utcnow, patched to fixtures.NOW (Wed 4 Nov 22:40 Istanbul)."""
from . import fixtures, guard

import ast, contextlib, dataclasses, io, json, os, shutil, stat, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

import instagram as IG
import platform_instagram as PI
import publish as P
import schedule as S
import tools

NEW6 = fixtures.WAITING[0]
LINK6 = 'https://www.instagram.com/reel/FAKE6/'
VIDEO_URL, COVER_URL = 'https://fake.trycloudflare.com/k/video.mp4', 'https://fake.trycloudflare.com/k/cover.jpg'


def key_file(username='fake_account'):
    """A made-up Instagram key in the test HOME (guard.reset() removes it again)."""
    IG.KEYS.mkdir(parents=True, exist_ok=True)
    IG.TOKEN.write_text(json.dumps({'mode': 'unified', 'token': 'FAKE_TOKEN', 'user_id': 'FAKE_IG_1',
                                    'username': username}), encoding='utf-8')


def meta_time(t: datetime) -> str:
    """t the way Meta writes timestamps: '2026-11-04T19:40:00+0000'."""
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+0000')


def run_main(argv) -> str:
    """instagram.py's command line in this process; returns what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        IG.main(argv)
    return out.getvalue().strip()


class Response:
    """Enough of a requests.Response for instagram.answer()."""
    def __init__(self, status, body=None, ctype='application/json; charset=UTF-8', text=None):
        self.status_code, self.ok = status, status < 400
        self.headers = {'content-type': ctype}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class Journal:
    """Records what InstagramPlatform.post() tells publish's Journal."""
    def __init__(self):
        self.events, self.container_id, self.media_id = [], None, None

    def stage(self, name):
        self.events.append(('stage', name))

    def container(self, cid):
        self.container_id = cid
        self.events.append(('container', cid))

    def published(self, mid):
        self.media_id = mid
        self.events.append(('published', mid))

    def log(self, line):
        self.events.append(('log', line))


class Case(unittest.TestCase):
    """A fresh WORK and HOME, the fixture root, the real platform registry, the clock at fixtures.NOW and
    no 10 s waits between upload checks."""
    def setUp(self):
        guard.reset()
        base = guard.TMP / 'instagram-root'
        shutil.rmtree(base, ignore_errors=True)                  # lessons one test adds never reach the next
        self.root = fixtures.make_root(base)
        self.now = fixtures.NOW
        for target, name, value in ((P, '_utcnow', lambda: self.now), (IG, 'POLL', 0)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._join)
        self.p = PI.make()

    def _join(self):
        for t in list(P._threads.values()):
            t.join(10)

    def items(self) -> dict:
        return {i.key: i for i in self.p.candidates(self.root)[0]}

    def jobs(self) -> list:
        with P._Con() as con:
            return [dict(r) for r in con.execute('SELECT * FROM jobs')]


# ---------- instagram.py ----------

class MetaErrors(Case):
    """MetaError keeps v1's text ('Meta <status>: <message> (code N, <user message>)') and adds the subcode."""
    def test_subcode_in_the_text(self):
        r = Response(400, {'error': {'message': 'The user is not linked to a Page', 'type': 'OAuthException',
                                     'code': 100, 'error_subcode': 2207085, 'error_user_msg': 'Link a Page',
                                     'fbtrace_id': 'FAKE_TRACE'}})
        with self.assertRaises(IG.MetaError) as e:
            IG.answer(r)
        self.assertEqual(str(e.exception), 'Meta 400: The user is not linked to a Page (code 100, subcode 2207085, '
                                           'Link a Page)')
        self.assertEqual((e.exception.status, e.exception.code, e.exception.subcode), (400, 100, 2207085))
        self.assertIsInstance(e.exception, RuntimeError)                    # v1 callers catch RuntimeError
        self.assertEqual(IG.classify(e.exception), 'permission')

    def test_old_text_without_subcode(self):
        err = {'message': 'Invalid parameter', 'code': 100}
        with self.assertRaises(IG.MetaError) as e:
            IG.answer(Response(400, {'error': err}))
        old = f"Meta 400: {err.get('message')} (code {err.get('code')}, {err.get('error_user_msg', '')})"   # v1's text
        self.assertEqual(str(e.exception), old)
        self.assertEqual((e.exception.status, e.exception.code, e.exception.subcode), (400, 100, None))

    def test_bodies_that_are_not_json(self):
        cases = [(Response(502, ctype='text/html', text='<html>Bad Gateway</html>'),
                  'Meta 502: <html>Bad Gateway</html> (code None, )'),
                 (Response(500, text='not json'), 'Meta 500: not json (code None, )'),
                 (Response(200, {'error': {'message': 'Please retry', 'code': 2}}), 'Meta 200: Please retry (code 2, )')]
        for r, text in cases:
            with self.subTest(text), self.assertRaises(IG.MetaError) as e:
                IG.answer(r)
            self.assertEqual(str(e.exception), text)
            self.assertEqual(IG.classify(e.exception), 'transient')
        self.assertEqual(IG.answer(Response(200, {'id': 'FAKE_MEDIA_1'})), {'id': 'FAKE_MEDIA_1'})
        self.assertEqual(IG.answer(Response(200, ctype='text/plain', text='ok')), {})

    def test_call_unreachable_and_the_token_only_in_the_header(self):
        key_file()
        real_call = guard.REAL['instagram.call']
        with mock.patch.object(IG.requests, 'request', side_effect=requests.ConnectTimeout('timed out')):
            with self.assertRaises(IG.MetaError) as e:
                real_call('GET', 'FAKE_IG_1', fields='username')
        self.assertEqual(str(e.exception), 'Meta unreachable: ConnectTimeout')
        self.assertEqual((e.exception.status, e.exception.code), (None, None))
        self.assertTrue(e.exception.__suppress_context__)
        self.assertEqual(IG.classify(e.exception), 'transient')
        seen = []

        def request(method, url, **kw):
            seen.append((method, url, kw))
            return Response(200, {'username': 'fake_account'})
        with mock.patch.object(IG.requests, 'request', request):
            self.assertEqual(real_call('GET', 'FAKE_IG_1', fields='username'), {'username': 'fake_account'})
        (method, url, kw), = seen
        self.assertEqual((method, url), ('GET', f'{IG.FB_API}/FAKE_IG_1'))
        self.assertEqual(kw['headers'], {'Authorization': 'Bearer FAKE_TOKEN'})
        self.assertEqual(kw['params'], {'fields': 'username'})
        self.assertNotIn('FAKE_TOKEN', url + json.dumps(kw['params']))

    def test_G22_call_timeout(self):
        """G22: call() waits 600 s by default (posting), or the timeout it is given (never sent as a parameter)."""
        key_file()
        real_call, seen = guard.REAL['instagram.call'], []

        def request(method, url, **kw):
            seen.append(kw)
            return Response(200, {'id': 'x'})
        with mock.patch.object(IG.requests, 'request', request):
            real_call('POST', 'FAKE_IG_1/media_publish', creation_id='c')
            real_call('GET', 'c', fields='status_code', timeout=(10, 30))
        self.assertEqual([(k['timeout'], k['params']) for k in seen],
                         [(600, {'creation_id': 'c'}), ((10, 30), {'fields': 'status_code'})])


class Classify(Case):
    """The spec 7.5 classify order; every answer is one of publish.KINDS."""
    def test_table(self):
        M = IG.MetaError
        table = [(IG.ContainerError('x', 'ERROR'), 'content'), (IG.ContainerError('x', 'EXPIRED'), 'transient'),
                 (IG.ContainerError('x', 'TIMEOUT'), 'transient'), (IG.ShareError('no address'), 'local'),
                 (M('Meta unreachable: ReadTimeout'), 'transient'),
                 (M('m', 400, 190), 'auth'), (M('m', 401, 102), 'auth'), (M('m', 400, 190, 2207085), 'auth'),
                 (M('m', 400, 100, 2207085), 'permission'), (M('m', 403, 10), 'permission'),
                 (M('m', 400, 368), 'permission'), (M('m', 400, 200), 'permission'), (M('m', 400, 299), 'permission'),
                 (M('m', 400, 4), 'rate'), (M('m', 400, 17), 'rate'), (M('m', 400, 32), 'rate'),
                 (M('m', 400, 613), 'rate'), (M('m', 400, 80002), 'rate'), (M('m', 429), 'rate'),
                 (M('m', 500, 1), 'transient'), (M('m', 400, 2), 'transient'), (M('m', 503), 'transient'),
                 (M('m', 500, 100), 'transient'),
                 (M('m', 400, 100), 'unknown'), (M('m', 400, 100, 2207026), 'unknown'), (M('m', 400, 300), 'unknown'),
                 (M('m', 400), 'unknown'),
                 (OSError('disk'), 'local'), (FileNotFoundError('cover missing'), 'local'),
                 (ValueError('x'), 'unknown'), (KeyError('id'), 'unknown'), (RuntimeError('x'), 'unknown'),
                 (PI.Unclear('x'), 'unknown')]
        for exc, kind in table:
            with self.subTest(exc=repr(exc), status=getattr(exc, 'status', None), code=getattr(exc, 'code', None)):
                self.assertEqual(IG.classify(exc), kind)
                self.assertEqual(self.p.classify(exc), kind)
                self.assertIn(kind, P.KINDS)


class PostInstagram(Case):
    """post_instagram's order with script_meta and fake_share; the container errors; ShareError."""
    def setUp(self):
        super().setUp()
        key_file()
        self.mp4 = self.root / 'new' / 'Output' / 'new-short-6.mp4'
        self.cover = IG.cover_path(self.mp4)
        guard.fake_share()

    def post(self, answers, **kw):
        self.calls, self.events = guard.script_meta(answers), []
        tell = lambda what: lambda *a: self.events.append((what, *a, len(self.calls)))
        return IG.post_instagram(self.mp4, self.cover, 'A caption', tell('log'), on_stage=tell('stage'),
                                 on_container=tell('container'), on_published=tell('published'), **kw)

    def test_callback_order(self):
        rec = self.post([{'id': 'FAKE_CONTAINER_6'}, {'status_code': 'IN_PROGRESS', 'status': 'In Progress'},
                         {'status_code': 'FINISHED', 'status': 'Finished'}, {'id': 'FAKE_MEDIA_6'}, {'permalink': LINK6}])
        self.assertEqual([e for e in self.events if e[0] != 'log'],
                         [('stage', 'sharing', 0), ('container', 'FAKE_CONTAINER_6', 1), ('stage', 'processing', 1),
                          ('stage', 'publishing', 3), ('published', 'FAKE_MEDIA_6', 4)])
        self.assertEqual([c[:2] for c in self.calls], [('POST', 'FAKE_IG_1/media'), ('GET', 'FAKE_CONTAINER_6'),
                                                       ('GET', 'FAKE_CONTAINER_6'), ('POST', 'FAKE_IG_1/media_publish'),
                                                       ('GET', 'FAKE_MEDIA_6')])
        self.assertEqual(self.calls[0][2], {'media_type': 'REELS', 'video_url': VIDEO_URL, 'cover_url': COVER_URL,
                                            'caption': 'A caption', 'share_to_feed': 'true'})
        self.assertEqual(self.calls[3][2], {'creation_id': 'FAKE_CONTAINER_6'})
        self.assertEqual(IG.Share.shared, [[self.mp4, self.cover]])
        self.assertEqual(set(rec), {'id', 'permalink', 'posted', 'posted_utc'})
        self.assertEqual((rec['id'], rec['permalink']), ('FAKE_MEDIA_6', LINK6))
        self.assertRegex(rec['posted'], r'^\d{4}-\d\d-\d\d \d\d:\d\d$')
        self.assertIsNotNone(datetime.fromisoformat(rec['posted_utc']).tzinfo)

    def test_permalink_failure_still_returns_the_record(self):
        rec = self.post([{'id': 'FAKE_CONTAINER_6'}, {'status_code': 'FINISHED'}, {'id': 'FAKE_MEDIA_6'},
                         IG.MetaError('Meta 500: try later (code 1, )', 500, 1)])
        self.assertEqual((rec['id'], rec['permalink']), ('FAKE_MEDIA_6', ''))
        self.assertIn(('published', 'FAKE_MEDIA_6', 3), self.events)

    def test_container_errors(self):
        for status, kind in (('ERROR', 'content'), ('EXPIRED', 'transient')):
            with self.subTest(status), self.assertRaises(IG.ContainerError) as e:
                self.post([{'id': 'FAKE_CONTAINER_6'}, {'status_code': status, 'status': f'{status}: made up'}])
            self.assertEqual(e.exception.status, status)
            self.assertEqual(str(e.exception), f'Instagram could not process new-short-6.mp4: {status}: made up')
            self.assertEqual(IG.classify(e.exception), kind)
            self.assertEqual([e[1] for e in self.events if e[0] == 'stage'], ['sharing', 'processing'])
        with self.assertRaises(IG.ContainerError) as e:
            self.post([{'id': 'FAKE_CONTAINER_6'}] + [{'status_code': 'IN_PROGRESS'}] * 60)
        self.assertEqual(e.exception.status, 'TIMEOUT')
        self.assertEqual(len(self.calls), 61)                    # the 60th check gives up; nothing is published
        self.assertFalse(any(c[1].endswith('media_publish') for c in self.calls))

    def test_share_error(self):
        class Refused:
            def __init__(self, files):
                pass

            def __enter__(self):
                raise IG.ShareError('cloudflared gave no public address')

            def __exit__(self, *exc):
                return False
        with mock.patch.object(IG, 'Share', Refused), self.assertRaises(IG.ShareError):
            self.post([])
        self.assertEqual((self.calls, [e for e in self.events if e[0] != 'log']), ([], [('stage', 'sharing', 0)]))

    def test_wait_container_polls_every_poll_seconds(self):
        self.calls, waits = guard.script_meta([{'status_code': 'IN_PROGRESS'}, {'status_code': 'FINISHED'}]), []
        with mock.patch.object(IG, 'POLL', 10), mock.patch.object(IG.time, 'sleep', waits.append):
            IG.wait_container('FAKE_CONTAINER_6', 'new-short-6.mp4')
        self.assertEqual(waits, [10, 10])
        self.assertEqual(self.calls[0], ('GET', 'FAKE_CONTAINER_6', {'fields': 'status_code,status'}))


class RealShare(Case):
    """instagram.Share itself: a cloudflared that cannot start is a ShareError, and the local server and the
    temporary folder are gone again (a made-up program name, so nothing starts)."""
    def test_missing_program(self):
        video = guard.WORK / 'share-test.mp4'
        video.write_bytes(b'')
        share = guard.REAL['instagram.Share']([video])
        with mock.patch.object(IG, 'CLOUDFLARED', guard.TMP / 'no-such-folder' / 'missing-tunnel.exe'):
            with self.assertRaisesRegex(IG.ShareError, r'could not start \(FileNotFoundError\)') as e:
                share.__enter__()
        self.assertEqual(IG.classify(e.exception), 'local')
        self.assertFalse(share.tmp.exists())
        self.assertEqual(share.httpd.socket.fileno(), -1)          # closed


class Reels(Case):
    """reels(out, data, probe=...) uses the probe it is given; None means instagram.probe at call time."""
    def test_probe_injection(self):
        _, out, data = IG.lesson_files(self.root / 'Um etwas bitten')
        seen = []

        def probe(path, seconds=68.0, w=1080, h=1920):
            seen.append(Path(path).name)
            return {'duration': seconds, 'width': w, 'height': h}
        (f, entry), = IG.reels(out, data, probe=probe)
        self.assertEqual((f.name, entry['whole'], seen), ('um-etwas-bitten-video.mp4', True, ['um-etwas-bitten-video.mp4']))
        self.assertEqual(IG.reels(out, data, probe=lambda p: probe(p, w=1920, h=1080)), [])        # horizontal
        self.assertEqual(IG.reels(out, data, probe=lambda p: probe(p, seconds=181.0)), [])         # over 3 min
        with mock.patch.object(IG, 'probe', probe):
            IG.reels(out, data)
        self.assertEqual(len(seen), 4)
        _, out, data = IG.lesson_files(self.root / 'new')
        self.assertEqual(len(IG.reels(out, data, probe=lambda p: self.fail('a Shorts lesson needs no probe'))), 9)


# ---------- platform_instagram.py: local ----------

class Candidates(Case):
    """candidates() on the fixtures: spec 5.1 keys, ids and order; posted from the Drive records; lessons not done
    are skipped quietly, unreadable ones go to the problems; hand-made files are not candidates."""
    def test_fixture_items(self):
        items, problems = self.p.candidates(self.root)
        self.assertEqual(problems, [])
        want = fixtures.reel_items(self.root)
        self.assertEqual([(i.key, i.lesson, i.out, i.file, i.part, i.title, i.seconds, i.rank, i.entry) for i in items],
                         [(d['key'], d['lesson'], d['out'], d['file'], d['part'], d['title'], d['seconds'], d['rank'],
                           d['entry']) for d in want])
        self.assertEqual([i.key for i in items if not i.posted], list(fixtures.WAITING))
        self.assertEqual([(i.key, i.posted['id']) for i in items if i.posted],
                         [(k, f'FAKE_MEDIA_{n}') for n, k in enumerate(fixtures.POSTED_KEYS, 1)])
        new6 = items[5]
        self.assertEqual((new6.key, new6.id, new6.part, new6.seconds, new6.platform), (NEW6, P.item_id(NEW6), 6, 30, 'instagram'))
        self.assertEqual(items[6].title, "nämlich: konuşmada 'çünkü'")          # the hashtag is gone
        self.assertEqual((items[9].part, items[9].rank[1]), (None, 0))           # Um: the whole lesson video
        self.assertFalse([i for i in items if i.lesson == 'ZU + INFINITIV'])      # hand-made Short, horizontal video

    def test_skips_and_problems(self):
        fixtures.write_lesson(self.root, 'A working lesson', shorts=2, state='working')
        fixtures.write_lesson(self.root, 'A failed lesson', shorts=1, state='failed')
        fixtures.write_lesson(self.root, 'Just recorded', state='new')
        bad = {'Broken texts': 'youtube.json', 'Broken record': IG.POSTED}
        for lesson, name in bad.items():
            fixtures.write_lesson(self.root, lesson, shorts=1)
            (tools.extras(self.root / lesson / 'Output') / name).write_text('{"made up": ', encoding='utf-8')
        items, problems = self.p.candidates(self.root)
        self.assertEqual({i.lesson for i in items}, {'new', 'Um etwas bitten', 'Sich interessieren für'})
        self.assertEqual([p['lesson'] for p in problems], ['Broken record', 'Broken texts'])
        for p in problems:
            self.assertEqual(set(p), {'lesson', 'error'})
            self.assertTrue(p['error'].startswith('JSONDecodeError: '), p['error'])

    def test_default_caption_and_prepare(self):
        item = self.items()[NEW6]
        self.assertEqual(self.p.default_caption(item, self.root), IG.caption(item.entry, IG.hashtags(self.root)))
        with mock.patch.object(IG, 'prepare') as prepare:
            self.p.prepare(item, print)
        prepare.assert_called_once_with(self.root / 'new', print)

    def test_history(self):
        hist = self.p.history(self.root)
        self.assertEqual([(h.key, h.id, h.by, h.at) for h in hist],
                         [(k, f'FAKE_MEDIA_{n}', 'old', t) for n, (k, t) in
                          enumerate(zip(fixtures.POSTED_KEYS, fixtures.NEW_POSTED), 1)])     # no Facebook entries
        self.assertTrue(all(h.at.tzinfo == timezone.utc for h in hist))
        path = tools.extras(self.root / 'new' / 'Output') / IG.POSTED
        path.write_text(path.read_text(encoding='utf-8')[:-40], encoding='utf-8')
        with self.assertRaisesRegex(OSError, r'^new: instagram-posted\.json is not valid JSON'):
            self.p.history(self.root)                     # the timing rules never run on half the posts

    def test_set_up_and_account_url(self):
        self.assertEqual((self.p.set_up(), self.p.account_url), (False, ''))
        key_file()
        self.assertEqual((self.p.set_up(), self.p.account_url), (True, 'https://www.instagram.com/fake_account/'))
        key_file('not a/name')
        self.assertEqual(self.p.account_url, '')
        self.assertEqual(P.registry()['instagram'].name, 'instagram')


class Cover(Case):
    """cover() returns the pipeline's cover when it exists, else None; never raises for odd names (F13)."""
    def test_odd_names(self):
        items = self.items()
        new6 = items[NEW6]
        self.assertEqual(self.p.cover(new6), tools.extras(new6.out) / 'cover-short-6.jpg')
        um = items['Um etwas bitten/Output/um-etwas-bitten-video.mp4']
        self.assertEqual(self.p.cover(um), tools.extras(um.out) / 'thumbnail-short.jpg')
        hand = self.root / 'ZU + INFINITIV' / 'Output' / 'zu-infinitiv-short.mp4'
        for f in (hand, new6.out / 'new-short-12.mp4', new6.out / 'odd name.mp4'):
            with self.subTest(f.name):
                self.assertIsNone(self.p.cover(dataclasses.replace(new6, file=f)))
        (tools.extras(new6.out) / 'cover-short-6.jpg').unlink()
        self.assertIsNone(self.p.cover(new6))


class WriteRecord(Case):
    """S27: every write to instagram-posted.json happens under the PostLock, atomically (temp file + os.replace,
    tried again after 1, 2, 4, 8, 16 s), keeping keys starting with '_' and nested facebook entries; when Drive
    refuses the replace it is written in place and read back (addendum C3); a broken file is never written over."""
    REC = {'id': 'FAKE_MEDIA_6', 'permalink': LINK6, 'posted': '2026-11-05 20:04',
           'posted_utc': '2026-11-05T17:04:31.120000+00:00', 'by': 'auto', 'job': 'jFAKE000001',
           'caption_sha': '3a9f0c2b7d11', 'reconciled': False}

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(PI, 'RETRIES', (0, 0, 0, 0, 0))
        patch.start()
        self.addCleanup(patch.stop)
        self.item = self.items()[NEW6]
        self.path = tools.extras(self.item.out) / IG.POSTED

    def write(self, item=None, record=None):
        with P.PostLock(owner='cli'), contextlib.redirect_stdout(io.StringIO()):
            self.p.write_record(item or self.item, record or self.REC)

    def tmp_files(self):
        return list(self.path.parent.glob('*.tmp-*'))

    @staticmethod
    def refuse_replace(tries):
        real = os.replace

        def replace(src, dst):
            if Path(dst).name == IG.POSTED:
                tries.append(Path(src).name)
                raise PermissionError(13, 'Access is denied (made up)')
            return real(src, dst)
        return replace

    def test_G2_drive_away_is_never_taken_for_no_record(self):
        before = self.path.read_text(encoding='utf-8')
        def away(*a, **k):
            raise FileNotFoundError(2, 'Drive is away (made up)')
        with P.PostLock(owner='cli'), contextlib.redirect_stdout(io.StringIO()),                 mock.patch.object(PI, '_read', away), mock.patch.object(IG.os, 'listdir', away),                 mock.patch.object(Path, 'exists', lambda self, *a, **k: False):
            with self.assertRaises(OSError):
                self.p.write_record(self.item, self.REC)
            with mock.patch.object(Path, 'read_text', away), self.assertRaises(OSError):
                IG.load(self.item.out)
        self.assertEqual(self.path.read_text(encoding='utf-8'), before)
        self.assertEqual(self.tmp_files(), [])

    def test_G2_a_lesson_without_a_record_file_starts_one(self):
        self.path.unlink()
        self.assertEqual(IG.load(self.item.out), {})
        self.write()
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {'new-short-6.mp4': self.REC})

    def test_atomic_and_keeps_every_other_key(self):
        before, done, real = json.loads(self.path.read_text(encoding='utf-8')), [], os.replace

        def replace(src, dst):
            done.append((Path(src).name, Path(dst).name))
            return real(src, dst)
        with mock.patch.object(PI.os, 'replace', replace):
            self.write()
        self.assertEqual(done, [(f'{IG.POSTED}.tmp-{os.getpid()}', IG.POSTED)])
        after = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(after.pop('new-short-6.mp4'), self.REC)
        self.assertEqual(after, before)                               # the other Reels, '_facebook', nested facebook
        self.assertIn('long', after['_facebook'])
        self.assertEqual(after['new-short-1.mp4']['facebook']['id'], 'FAKE_FB_REEL_1')
        self.assertEqual(self.tmp_files(), [])
        md = (self.path.parent / 'instagram.md').read_text(encoding='utf-8')
        self.assertIn(f'## Short 6 (posted 2026-11-05 20:04: {LINK6})', md)
        self.assertIn('Posted from the private dashboard (automatically when it is on: Publish → Settings).', md)

    def test_merge_keeps_the_old_records_extra_keys(self):
        new1 = self.items()[fixtures.POSTED_KEYS[0]]
        old = json.loads(self.path.read_text(encoding='utf-8'))['new-short-1.mp4']
        self.write(new1, {**old, 'permalink': 'https://www.instagram.com/reel/FAKE1b/'})   # like publish.note_media
        rec = json.loads(self.path.read_text(encoding='utf-8'))['new-short-1.mp4']
        self.assertEqual(rec, {**old, 'permalink': 'https://www.instagram.com/reel/FAKE1b/'})
        self.assertIn('facebook', rec)

    def test_a_lesson_without_a_record_file(self):
        um = self.items()['Um etwas bitten/Output/um-etwas-bitten-video.mp4']
        path = tools.extras(um.out) / IG.POSTED
        self.assertFalse(path.exists())
        self.write(um)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'um-etwas-bitten-video.mp4': self.REC})
        self.assertIn('## The lesson video (posted 2026-11-05 20:04', (path.parent / 'instagram.md').read_text(encoding='utf-8'))

    def test_only_under_the_post_lock(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(OSError, 'lock is not held'):
            self.p.write_record(self.item, self.REC)
        self.assertEqual(self.path.read_bytes(), before)
        stale = P._lockfile()                       # a lock untouched for LOCK_STALE s is stale, and no lock at all
        stale.write_text(json.dumps({'v': 2, 'token': 'f' * 32, 'pid': 1, 'owner': 'watcher', 'job': None,
                                     'started': S.iso(datetime.now(timezone.utc))}), encoding='utf-8')
        os.utime(stale, (stale.stat().st_mtime - P.LOCK_STALE - 60,) * 2)
        with self.assertRaises(OSError):
            self.p.write_record(self.item, self.REC)
        self.assertEqual(self.path.read_bytes(), before)

    def test_retry_waits(self):
        waits, tries = [], []
        with mock.patch.object(PI, 'RETRIES', (1, 2, 4, 8, 16)), mock.patch.object(PI.time, 'sleep', waits.append), \
                mock.patch.object(PI.os, 'replace', self.refuse_replace(tries)):
            self.write()
        self.assertEqual((waits, len(tries)), ([1, 2, 4, 8, 16], 6))

    def test_replace_refused_falls_back_in_place(self):
        tries = []
        with mock.patch.object(PI.os, 'replace', self.refuse_replace(tries)):
            self.write()
        self.assertEqual(tries, [f'{IG.POSTED}.tmp-{os.getpid()}'] * 6)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['new-short-6.mp4'], self.REC)
        self.assertEqual(self.tmp_files(), [])

    @unittest.skipUnless(os.name == 'nt', 'only Windows refuses os.replace onto a read-only file')
    def test_both_refused_raises_and_keeps_the_file(self):
        before = self.path.read_bytes()
        os.chmod(self.path, stat.S_IREAD)                 # Windows refuses both the replace and the write in place
        self.addCleanup(os.chmod, self.path, stat.S_IREAD | stat.S_IWRITE)
        with self.assertRaisesRegex(OSError, r'^Drive did not take instagram-posted\.json'):
            self.write()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.tmp_files(), [])

    def test_a_broken_file_is_never_written_over(self):
        for text in ('{"new-short-1.mp4": {"id": ', '["not", "an", "object"]'):
            with self.subTest(text):
                self.path.write_text(text, encoding='utf-8')
                with self.assertRaises(OSError):
                    self.write()
                self.assertEqual(self.path.read_text(encoding='utf-8'), text)

    def test_no_other_writer(self):
        """Nothing else writes the record file: in instagram.py only the uncalled post_long_facebook does (S19)."""
        tree = ast.parse((guard.PIPELINE / 'instagram.py').read_text(encoding='utf-8'))
        writers = sorted({fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                          for node in ast.walk(fn) if isinstance(node, ast.Call)
                          and getattr(node.func, 'attr', '') in ('write_text', 'write_bytes', 'replace')
                          and 'POSTED' in ast.unparse(node.func)})
        self.assertEqual(writers, ['post_long_facebook'])


# ---------- platform_instagram.py: NETWORK (scripted) ----------

class Network(Case):
    """The read calls (spec 7.4, 5.10) and post() with a journal."""
    def setUp(self):
        super().setUp()
        key_file()
        guard.fake_share()

    def test_reads(self):
        at = meta_time(self.now)
        media = {'id': '17900000000000006', 'timestamp': at, 'permalink': LINK6, 'caption': 'A caption',
                 'media_product_type': 'REELS', 'like_count': 4, 'comments_count': 1}
        calls = guard.script_meta([
            {'status_code': 'PUBLISHED', 'id': 'FAKE_CONTAINER_6'}, {'id': 'FAKE_CONTAINER_7'},
            {'data': [{k: media[k] for k in ('id', 'timestamp', 'permalink', 'caption', 'media_product_type')}]},
            {'username': 'fake_account', 'followers_count': 71, 'media_count': 6, 'id': 'FAKE_IG_1'},
            {'data': [{'quota_usage': 1, 'config': {'quota_total': 100, 'quota_duration': 86400}}]},
            {'followers_count': 71, 'follows_count': 3, 'media_count': 6, 'media': {'data': [media]}, 'id': 'FAKE_IG_1'},
            {'data': [{'quota_usage': 2, 'config': {'quota_total': 100}}]},
            {'data': [{'permission': 'instagram_basic', 'status': 'granted'},
                      {'permission': 'instagram_manage_insights', 'status': 'declined'},
                      {'permission': 'instagram_content_publish', 'status': 'granted'}]}])
        z = S.iso(self.now)
        self.assertEqual(self.p.container_status('FAKE_CONTAINER_6'), 'PUBLISHED')
        self.assertEqual(self.p.container_status('FAKE_CONTAINER_7'), 'UNKNOWN')
        self.assertEqual(self.p.recent_media(5), [{'id': '17900000000000006', 'at': z, 'permalink': LINK6,
                                                   'caption': 'A caption', 'product': 'REELS'}])
        self.assertEqual(self.p.test(), {'account': '@fake_account', 'followers': 71, 'media': 6,
                                         'limit': {'used': 1, 'total': 100}})
        self.assertEqual(self.p.read_stats(), {'followers': 71, 'follows': 3, 'media_count': 6, 'media': [
            {'id': '17900000000000006', 'at': z, 'product': 'REELS', 'likes': 4, 'comments': 1, 'permalink': LINK6,
             'caption': 'A caption'}]})
        self.assertEqual(self.p.read_limit(), {'used': 2, 'total': 100})
        self.assertEqual(self.p.read_permissions(), ['instagram_basic', 'instagram_content_publish'])
        quick = {'timeout': (10, 30)}                       # G22: the reads a person waits for stay short
        self.assertEqual(calls, [
            ('GET', 'FAKE_CONTAINER_6', {'fields': 'status_code', **quick}),
            ('GET', 'FAKE_CONTAINER_7', {'fields': 'status_code', **quick}),
            ('GET', 'FAKE_IG_1/media', {'fields': 'id,timestamp,permalink,caption,media_product_type', 'limit': '5',
                                        **quick}),
            ('GET', 'FAKE_IG_1', {'fields': 'username,followers_count,media_count', **quick}),
            ('GET', 'FAKE_IG_1/content_publishing_limit', {'fields': 'quota_usage,config', **quick}),
            ('GET', 'FAKE_IG_1', {'fields': 'followers_count,follows_count,media_count,media.limit(50){id,timestamp,'
                                            'media_product_type,like_count,comments_count,permalink,caption}'}),
            ('GET', 'FAKE_IG_1/content_publishing_limit', {'fields': 'quota_usage,config', **quick}),
            ('GET', 'me/permissions', {})])
        self.assertEqual((PI._iso('2026-11-04T13:46:12+0000'), PI._iso(None), PI._iso('yesterday')),
                         ('2026-11-04T13:46:12Z', None, None))

    def test_post_tells_the_journal_in_order(self):
        calls = guard.script_meta([{'id': 'FAKE_CONTAINER_6'}, {'status_code': 'FINISHED'}, {'id': 'FAKE_MEDIA_6'},
                                   {'permalink': LINK6}])
        j, item = Journal(), self.items()[NEW6]
        rec = self.p.post(item, 'A caption', j)
        self.assertEqual([e for e in j.events if e[0] != 'log'],
                         [('stage', 'sharing'), ('container', 'FAKE_CONTAINER_6'), ('stage', 'processing'),
                          ('stage', 'publishing'), ('published', 'FAKE_MEDIA_6')])
        self.assertEqual((rec['id'], rec['permalink']), ('FAKE_MEDIA_6', LINK6))
        self.assertEqual(IG.Share.shared, [[item.file, tools.extras(item.out) / 'cover-short-6.jpg']])
        self.assertEqual(len(calls), 4)

    def test_post_without_cover(self):
        calls, item = guard.script_meta([]), self.items()[NEW6]
        (tools.extras(item.out) / 'cover-short-6.jpg').unlink()
        with self.assertRaisesRegex(FileNotFoundError, '^cover missing$') as e:
            self.p.post(item, 'A caption', Journal())
        self.assertEqual((calls, self.p.classify(e.exception)), ([], 'local'))

    def published_fails(self, error, *after, caption='A caption'):
        """post() where media_publish raises error; after: the answers for the check that follows."""
        self.calls = guard.script_meta([{'id': 'FAKE_CONTAINER_6'}, {'status_code': 'FINISHED'}, error, *after])
        self.journal = Journal()
        return self.p.post(self.items()[NEW6], caption, self.journal)

    def test_media_publish_failed_but_the_reel_is_live(self):
        other = {'id': 'FAKE_MEDIA_OLD', 'timestamp': meta_time(self.now - timedelta(days=1)), 'caption': 'A caption'}
        live = {'id': 'FAKE_MEDIA_6', 'timestamp': meta_time(self.now + timedelta(seconds=20)), 'permalink': LINK6,
                'caption': ' A   caption ', 'media_product_type': 'REELS'}
        rec = self.published_fails(IG.MetaError('Meta unreachable: ReadTimeout'), {'status_code': 'PUBLISHED'},
                                   {'data': [live, other]})
        self.assertEqual((rec['id'], rec['permalink'], self.journal.media_id), ('FAKE_MEDIA_6', LINK6, 'FAKE_MEDIA_6'))
        self.assertEqual(datetime.fromisoformat(rec['posted_utc']), self.now.replace(microsecond=0) + timedelta(seconds=20))
        self.assertEqual([c[1] for c in self.calls[3:]], ['FAKE_CONTAINER_6', 'FAKE_IG_1/media'])

    def test_media_publish_failed_and_unclear(self):
        old = {'id': 'FAKE_MEDIA_OLD', 'timestamp': meta_time(self.now - timedelta(minutes=5)), 'caption': 'A caption'}
        for after in (({'status_code': 'PUBLISHED'}, {'data': [old]}),                     # too old: not this post
                      ({'status_code': 'PUBLISHED'}, {'data': [{**old, 'timestamp': meta_time(self.now),
                                                                'caption': 'Another caption'}]}),
                      (IG.MetaError('Meta unreachable: ConnectionError'),)):                # the check fails too
            with self.subTest(after=after), self.assertRaises(PI.Unclear) as e:
                self.published_fails(IG.MetaError('Meta 500: An unexpected error (code 2, )', 500, 2), *after)
            self.assertEqual(self.p.classify(e.exception), 'unknown')
            self.assertIsNone(self.journal.media_id)

    def test_media_publish_refused_is_not_checked(self):
        for error in (IG.MetaError('Meta 400: expired (code 190, )', 400, 190),
                      IG.MetaError('Meta 400: limit (code 4, )', 400, 4)):
            with self.subTest(str(error)), self.assertRaises(IG.MetaError) as e:
                self.published_fails(error)
            self.assertIs(e.exception, error)                     # the original error, kind unchanged
            self.assertEqual(len(self.calls), 3)                  # a clear refusal makes no extra call

    def test_F1_publish_error_while_the_upload_is_still_finished(self):
        """F1: no clear answer and the upload stays FINISHED: Meta may still publish it, so it is Unclear (the job
        ends 'unknown', nothing is sent again), after reading the status again at LIVE_WAITS."""
        waits = []
        with mock.patch.object(PI.time, 'sleep', waits.append), self.assertRaises(PI.Unclear) as e:
            self.published_fails(IG.MetaError('Meta unreachable: ReadTimeout'), *[{'status_code': 'FINISHED'}] * 4)
        self.assertEqual((self.p.classify(e.exception), waits[-3:]), ('unknown', list(PI.LIVE_WAITS)))
        self.assertEqual(len(self.calls), 3 + 4)
        self.assertIsNone(self.journal.media_id)

    def test_F1_publish_error_then_published(self):
        live = {'id': 'FAKE_MEDIA_6', 'timestamp': meta_time(self.now + timedelta(seconds=20)), 'permalink': LINK6,
                'caption': 'A caption', 'media_product_type': 'REELS'}
        with mock.patch.object(PI.time, 'sleep', lambda s: None):
            rec = self.published_fails(IG.MetaError('Meta 500: oops (code 2, )', 500, 2), {'status_code': 'FINISHED'},
                                       {'status_code': 'PUBLISHED'}, {'data': [live]})
        self.assertEqual((rec['id'], self.journal.media_id), ('FAKE_MEDIA_6', 'FAKE_MEDIA_6'))

    def test_F1_publish_error_and_upload_error(self):
        with mock.patch.object(PI.time, 'sleep', lambda s: None), self.assertRaises(IG.ContainerError) as e:
            self.published_fails(IG.MetaError('Meta unreachable: ReadTimeout'), {'status_code': 'ERROR'})
        self.assertEqual(self.p.classify(e.exception), 'content')


# ---------- the command line ----------

class Cli(Case):
    """instagram.py post and daily go through publish; --dry-run makes no call and no job."""
    def setUp(self):
        super().setUp()
        key_file()
        self.calls = guard.script_meta([])

    def test_dry_run_makes_no_call(self):
        lines = run_main(['post', str(self.root / 'new'), '--dry-run']).splitlines()
        self.assertEqual(lines[0], 'Would post: new/Output/new-short-6.mp4 (30 s)')
        self.assertTrue(lines[1].startswith('Cover: ') and lines[1].endswith('cover-short-6.jpg'), lines[1])
        self.assertEqual(lines[3], 'da ile resmi sebep')
        self.assertIn('  warn: A Reel already went out today at 16:46.', lines)
        self.assertEqual((self.calls, self.jobs()), ([], []))

    def test_daily(self):
        S.pause_file('instagram').write_text('tester 2026-11-04 21:27', encoding='utf-8')
        self.assertEqual(run_main(['daily', str(self.root)]), 'Nothing to post now.')
        self.assertEqual(self.calls, [])

    def test_F51_daily_prints_each_line_once(self):
        fake = fixtures.fake_platform(self.root)
        P.set_registry({'instagram': fake})
        self.now = datetime(2026, 11, 5, 17, 5, tzinfo=timezone.utc)          # Thu 20:05: the slot is open
        out = run_main(['daily', str(self.root)]).splitlines()
        for line in ('Instagram: posting new-short-6.mp4 (automatic, slot 2026-11-05)',
                     'Instagram: new-short-6.mp4 posted https://www.instagram.com/reel/FAKE6/'):
            self.assertEqual(sum(l.endswith(line) for l in out), 1, out)
        self.assertEqual(len([c for c in fake.calls if c[0] == 'post']), 1)
        self.assertNotIn('Nothing to post now.', out)
        self.now = fixtures.NOW
        self.assertEqual(run_main(['daily', str(self.root)]), 'Nothing to post now.')


class RenewWarning(Case):
    """F52: the key expiry warning is safe without a key file and takes a clock; the platform hands it on."""
    def key(self, **extra):
        IG.KEYS.mkdir(parents=True, exist_ok=True)
        IG.TOKEN.write_text(json.dumps({'mode': 'unified', 'token': 'FAKE_TOKEN', 'user_id': 'FAKE_IG_1',
                                        'username': 'fake_account', 'expires_at': None, **extra}), encoding='utf-8')

    def test_F52_renew_warning(self):
        self.assertIsNone(IG.renew_warning(self.now))                        # no key file: no error either
        self.assertIsNone(self.p.key_warning(self.now))
        self.key()                                                           # a system-user key never ends
        self.assertIsNone(IG.renew_warning(self.now))
        self.key(expires_at=int((self.now + timedelta(days=30)).timestamp()))
        self.assertIsNone(IG.renew_warning(self.now))
        self.key(expires_at=int((self.now + timedelta(days=5)).timestamp()))
        warn = IG.renew_warning(self.now)
        self.assertRegex(warn, r'^WARNING: Meta access ends 2026-11-\d\d: run  instagram.py unified-token  again')
        self.assertEqual(self.p.key_warning(self.now), warn)
        self.assertIsNone(IG.renew_warning(self.now - timedelta(days=30)))   # the clock is the one given

    def test_a_folder_is_needed(self):
        for cmd in ('post', 'daily', 'prepare'):
            with self.subTest(cmd), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                IG.main([cmd])
            self.assertEqual(e.exception.code, 2)

    def test_posting_is_the_post_lock(self):
        self.assertEqual(IG.POSTING, P._lockfile())
        with IG.Posting():
            self.assertEqual(P.PostLock.holder()['owner'], 'cli')
            with self.assertRaises(IG.Busy):
                with IG.Posting():
                    self.fail('a second poster got the lock')
        self.assertIsNone(P.PostLock.holder())


class CliForce(Case):
    """S11 for the CLI: at the hard cap (3 Reels in the rolling 24 h) instagram.py post refuses with and without
    --force, and nothing is sent; below it, --force passes the gap and same-day warnings and posts under the
    PostLock, by 'cli', with the real platform writing the Drive record."""
    def setUp(self):
        super().setUp()
        key_file()
        guard.fake_share()

    def test_hard_cap_refuses_even_with_force(self):
        path = tools.extras(self.root / 'new' / 'Output') / IG.POSTED
        recs = json.loads(path.read_text(encoding='utf-8'))
        for n, hours in ((3, 3), (4, 2)):                       # with new-short-5 (16:46): 3 in the last 24 h
            at = self.now - timedelta(hours=hours)
            recs[f'new-short-{n}.mp4'].update(posted=f'{S.local(at):%Y-%m-%d %H:%M}', posted_utc=at.isoformat())
        path.write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding='utf-8')
        calls = guard.script_meta([])
        for force in ([], ['--force']):
            with self.subTest(force=force):
                self.assertRegex(run_main(['post', str(self.root / 'new'), *force]),
                                 r'^Not posted: 3 Reels in the last 24 hours')
        self.assertEqual((calls, IG.Share.shared, self.jobs()), ([], [], []))

    def test_force_passes_the_warnings_only(self):
        calls = guard.script_meta([{'id': 'FAKE_CONTAINER_6'}, {'status_code': 'FINISHED'}, {'id': 'FAKE_MEDIA_6'},
                                   {'permalink': LINK6}])
        out = run_main(['post', str(self.root / 'new')])
        self.assertTrue(out.endswith('A Reel already went out today at 16:46. Use --force to post anyway.'), out)
        self.assertEqual(calls, [])
        held, real = [], PI.InstagramPlatform.write_record

        def write_record(p, item, record):
            held.append(P.PostLock.holder())
            return real(p, item, record)
        with mock.patch.object(PI.InstagramPlatform, 'write_record', write_record):
            out = run_main(['post', str(self.root / 'new'), '--force'])
        self.assertEqual(out.splitlines()[-1], f'Posted new-short-6.mp4: {LINK6}')
        self.assertEqual([c[:2] for c in calls], [('POST', 'FAKE_IG_1/media'), ('GET', 'FAKE_CONTAINER_6'),
                                                  ('POST', 'FAKE_IG_1/media_publish'), ('GET', 'FAKE_MEDIA_6')])
        self.assertEqual(held[0]['owner'], 'cli')
        self.assertIsNone(P.PostLock.holder())
        rec = IG.load(self.root / 'new' / 'Output')['new-short-6.mp4']
        job, = self.jobs()
        self.assertEqual({k: rec[k] for k in ('id', 'permalink', 'by', 'job', 'caption_sha', 'reconciled')},
                         {'id': 'FAKE_MEDIA_6', 'permalink': LINK6, 'by': 'cli', 'job': job['id'],
                          'caption_sha': job['caption_sha'], 'reconciled': False})
        self.assertEqual((job['state'], job['by'], json.loads(job['override'])), ('done', 'cli', ['gap', 'same_day']))
        self.assertEqual(self.p.history(self.root)[-1].id, 'FAKE_MEDIA_6')


# ---------- S19 ----------

class NoFacebook(unittest.TestCase):
    """S19: no code path posts to Facebook or uploads a video to YouTube. FACEBOOK_PAGE is False; nothing calls
    post_facebook, post_long_facebook or fb_caption; page_video and upload are called only inside those two; the
    Facebook video endpoints appear only there; no module calls videos().insert or uploads a video file."""
    FB = {'post_facebook', 'post_long_facebook', 'fb_caption', 'page_video'}
    INSIDE = {('page_video', 'post_long_facebook'), ('upload', 'post_facebook')}       # instagram.py only

    def setUp(self):
        guard.reset()
        self.sources = {p.name: p.read_text(encoding='utf-8') for p in sorted(guard.PIPELINE.glob('*.py'))}
        self.assertIn('instagram.py', self.sources)

    @staticmethod
    def nodes(text):
        """(node, name of the function it is in, or None) for every node of a module's source."""
        out = []

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                out.append((child, fn))
                walk(child, child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn)
        walk(ast.parse(text), None)
        return out

    def calls(self, text, names):
        """[(called name, enclosing function)] for calls of these names (f() and x.f())."""
        return [(n, fn) for node, fn in self.nodes(text) if isinstance(node, ast.Call)
                for n in [getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)] if n in names]

    def strings(self, text, words):
        """[(word, enclosing function)] for string constants (also f-string pieces) containing these words."""
        return [(w, fn) for node, fn in self.nodes(text) if isinstance(node, ast.Constant) and isinstance(node.value, str)
                for w in words if w in node.value]

    def test_the_page_switch_is_off(self):
        self.assertIs(IG.FACEBOOK_PAGE, False)
        self.assertRegex(self.sources['instagram.py'], r'(?m)^FACEBOOK_PAGE = False\b')

    def test_no_caller_of_the_facebook_posts(self):
        for name, text in self.sources.items():
            with self.subTest(name):
                if name == 'instagram.py':
                    found = self.calls(text, self.FB | {'upload'})
                    self.assertEqual(set(found), self.INSIDE)            # the two inner calls exist, and only they
                else:
                    self.assertEqual(self.calls(text, self.FB), [])

    def test_facebook_endpoints_only_in_the_uncalled_functions(self):
        hits = self.strings(self.sources['instagram.py'], ('/video_reels', '/videos', 'graph-video.facebook'))
        self.assertEqual(sorted(set(hits)), [('/video_reels', 'post_facebook'), ('/videos', 'page_video'),
                                             ('graph-video.facebook', 'page_video')])
        for name, text in self.sources.items():
            if name != 'instagram.py':
                with self.subTest(name):
                    self.assertNotRegex(text, r'video_reels|graph-video\.facebook')

    def test_no_youtube_upload(self):
        for name, text in self.sources.items():
            with self.subTest(name):
                self.assertNotRegex(text, r'videos\(\)\s*\.\s*insert')
                self.assertNotRegex(text, r"""mimetype\s*=\s*['"]video/""")


if __name__ == '__main__':
    unittest.main()
