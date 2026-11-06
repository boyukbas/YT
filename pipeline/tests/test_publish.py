"""publish.py and platform_fake.py (spec 5, 7.2, 7.3; safety rules S1-S11, S15, S16, S23, S28-S30 of spec 10).
Offline: every test uses FakePlatform on a fixtures.make_root() root; the clock is publish._utcnow, patched to
fixtures.NOW (Wed 4 Nov 22:40 Istanbul, the last Reel 5 h 54 min before) unless a test moves it."""
from . import fixtures, guard

import contextlib, io, json, os, re, shutil, threading, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import platform_fake as F
import publish as P
import schedule as S

IG = 'instagram'
WAITING = fixtures.WAITING
NEW6, NEW7 = WAITING[0], WAITING[1]
WHO = 'tester'


def T(text: str) -> datetime:
    """'2026-11-05 20:05' Istanbul as aware UTC."""
    return datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=fixtures.IST).astimezone(timezone.utc)


def unclear(*stages):
    """A FakePlatform.post that commits container c-70, reaches these stages (default: up to 'publishing'), then
    raises an 'unknown' error: Meta's answer to media_publish was not clear."""
    def post(item, caption, journal):
        journal.stage('sharing')
        journal.container('c-70')
        for s in stages or ('processing', 'publishing'):
            journal.stage(s)
        raise F.FakeError('unknown')
    return post


class Case(unittest.TestCase):
    """A fresh WORK, the fixture root, a FakePlatform as the registry and the clock at fixtures.NOW.
    paused=True writes the old pause flag before the first settings load (so the migration says off)."""
    paused = False

    def setUp(self):
        guard.reset()
        self.root = fixtures.make_root(guard.TMP / 'publish-root')
        if self.paused:
            S.pause_file(IG).write_text('tester 2026-11-04 21:27', encoding='utf-8')
        self.fake = fixtures.fake_platform(self.root)
        P.set_registry({IG: self.fake})
        self.now = fixtures.NOW
        clock = mock.patch.object(P, '_utcnow', lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.addCleanup(self._join)

    def _join(self):
        for t in list(P._threads.values()):
            t.join(10)

    # ----- helpers -----
    def item(self, key=NEW6) -> P.Item:
        return {i.key: i for i in self.fake.candidates(self.root)[0]}[key]

    def preview(self, key=NEW6) -> dict:
        return P.preview(self.root, IG, P.item_id(key), now=self.now)

    def start(self, key=NEW6, caption=None, override=None, check=None, save=False) -> dict:
        """start_post with the preview's caption, fingerprint and warn codes unless given."""
        pv = self.preview(key)
        warns = [c['code'] for c in pv['checks'] if c['level'] == 'warn']
        return P.start_post(self.root, IG, P.item_id(key), pv['caption'] if caption is None else caption, save,
                            pv['check'] if check is None else check, warns if override is None else override, WHO)

    def post(self, key=NEW6, **kw) -> dict:
        """A manual post to its end; returns the Job."""
        return P._wait(self.start(key, **kw)['job']['id'])

    def posts(self) -> list:
        return [c for c in self.fake.calls if c[0] == 'post']

    def records(self) -> dict:
        try:
            return json.loads((guard.WORK / 'fake-records.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}

    def rows(self, sql, *args) -> list:
        with P._Con() as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def kinds(self) -> list[str]:
        return [r['kind'] for r in self.rows('SELECT kind FROM audit ORDER BY id')]

    def cli_job(self, key=NEW6, override=('gap', 'same_day')) -> tuple[str, str]:
        """A queued CLI job (run_post re-checks only the spacing for it): (job id, caption)."""
        caption, _ = P.caption_for(self.root, self.item(key))
        return P._new_job(self.fake, self.item(key), 'cli', 'cli', caption, self.now, override=list(override)), caption

    def crashed(self, outcome, key=NEW6) -> str:
        """A job whose process 'died' (FakeCrash) at that point; the clock then moves 6 minutes on."""
        jid, caption = self.cli_job(key)
        self.fake.script = [outcome]
        with self.assertRaises(F.FakeCrash):
            P.run_post(jid, self.root, caption)
        self.assertEqual(P.job(jid)['state'], 'running')
        self.now += timedelta(minutes=6)
        return jid

    def add_job(self, key=NEW6, state='running', by='manual', **cols) -> str:
        """A job row written directly (a job some other process left behind)."""
        jid = 'j' + os.urandom(5).hex()
        old = S.iso(self.now - timedelta(minutes=20))
        row = {'id': jid, 'platform': IG, 'item_key': key, 'title': 'made-up', 'by': by, 'who': WHO, 'state': state,
               'created': old, 'started': old, 'heartbeat': old, 'caption_head': 'made-up caption',
               'caption_sha': 'f' * 12, **cols}
        with P._Con(write=True) as con:
            con.execute(f'INSERT INTO jobs ({", ".join(row)}) VALUES ({", ".join("?" * len(row))})', tuple(row.values()))
        return jid

    def lock_file(self) -> Path:
        return P._lockfile()

    def auto_on(self):
        """Automatic posting effective, now in Thursday's slot (20:05, 27 h after the last Reel)."""
        self.assertTrue(S.effective_auto(P.settings(self.root), IG)[0])
        self.now = T('2026-11-05 20:05')


# ---------- queue ----------

class QueueOrder(Case):
    """spec 5.1 and the regression: new 6, 7, 8, 9, Um, Sich; ZU absent; the old order put Sich before Um."""
    def test_waiting_order(self):
        q = P.queue(self.root, IG, now=self.now)
        self.assertEqual([i['key'] for i in q['waiting']], list(WAITING))
        self.assertEqual([i['position'] for i in q['waiting']], [1, 2, 3, 4, 5, 6])
        self.assertEqual(q['posted_total'], 5)
        self.assertEqual(q['posted'][0]['key'], fixtures.POSTED_KEYS[-1])
        self.assertEqual(q['posted'][0]['posted']['by'], 'old')
        self.assertFalse(any('ZU' in i['key'] for i in q['waiting'] + q['posted']))
        self.assertEqual(q['problems'], [])
        um, sich = WAITING.index('Um etwas bitten/Output/um-etwas-bitten-video.mp4'), len(WAITING) - 1
        self.assertLess(um, sich)

    def test_item_json(self):
        first = P.queue(self.root, IG, now=self.now)['waiting'][0]
        self.assertEqual(set(first), {'id', 'key', 'lesson', 'file', 'part', 'title', 'seconds', 'state', 'position',
                                      'top', 'custom_caption', 'failed', 'posted', 'cover', 'eta'})
        self.assertEqual((first['file'], first['part'], first['seconds'], first['state']), ('new-short-6.mp4', 6, 30, 'waiting'))
        self.assertEqual(first['cover'], f'/api/cover/instagram/{first["id"]}.jpg')
        self.assertEqual(first['eta'], '2026-11-05T17:00:00Z')

    def test_item_ids(self):
        self.assertEqual(P.item_id(NEW6), __import__('hashlib').sha1(NEW6.encode()).hexdigest()[:12])
        with self.assertRaises(P.PublishError) as e:
            P.find(self.root, IG, '..\\..\\dashboard.py')
        self.assertEqual((e.exception.status, e.exception.code), (404, 'unknown_item'))


class QueueOps(Case):
    """Every queue op: rev, stale, one audit row each, and the refusals."""
    def op(self, op, key=NEW7, **kw):
        rev = P.queue(self.root, IG, now=self.now)['rev']
        return P.queue_op(self.root, IG, op, rev, WHO, item_id=P.item_id(key) if key else None, **kw)

    def waiting(self):
        return [i['key'] for i in P.queue(self.root, IG, now=self.now)['waiting']]

    def test_top_untop_reset(self):
        self.assertEqual(self.op('top', WAITING[3])['message'], 'Moved to the top.')
        self.op('top', WAITING[5])
        self.assertEqual(self.waiting()[:3], [WAITING[5], WAITING[3], WAITING[0]])
        self.op('untop', WAITING[5])
        self.assertEqual(self.waiting()[:2], [WAITING[3], WAITING[0]])
        self.op('reset_order', None)
        self.assertEqual(self.waiting(), list(WAITING))

    def test_skip_caption_failed(self):
        self.op('skip')
        q = P.queue(self.root, IG, now=self.now)
        self.assertEqual([i['key'] for i in q['skipped']], [NEW7])
        self.op('unskip')
        self.op('caption', caption='Neue Caption #almanca')
        self.assertEqual(P.caption_for(self.root, self.item(NEW7)), ('Neue Caption #almanca', True))
        self.op('clear_caption')
        self.assertFalse(P.caption_for(self.root, self.item(NEW7))[1])
        with P._Con(write=True) as con:
            P._set_queue(con, IG, NEW7, self.item(NEW7).sig, failed='made-up failure', failed_at=S.iso(self.now))
        self.assertEqual(P.queue(self.root, IG, now=self.now)['failed'][0]['failed']['reason'], 'made-up failure')
        self.op('clear_failed')
        self.assertIn(NEW7, self.waiting())
        self.assertEqual(self.kinds()[-5:], ['queue.skip', 'queue.unskip', 'queue.caption', 'queue.clear_caption',
                                             'queue.clear_failed'])

    def test_rev_and_stale(self):
        rev = P.queue(self.root, IG, now=self.now)['rev']
        P.queue_op(self.root, IG, 'skip', rev, WHO, item_id=P.item_id(NEW7))
        with self.assertRaises(P.PublishError) as e:
            P.queue_op(self.root, IG, 'top', rev, WHO, item_id=P.item_id(NEW6))
        self.assertEqual((e.exception.status, e.exception.code), (409, 'stale'))
        self.assertEqual(e.exception.message, 'The queue changed on another phone. Here is the new one.')
        self.assertEqual(e.exception.extra['queue']['rev'], rev + 1)

    def test_refusals(self):
        rev = P.queue(self.root, IG, now=self.now)['rev']
        for op, kw, want in (('dance', {'item_id': P.item_id(NEW6)}, (422, 'bad_op')),
                             ('top', {}, (422, 'bad_op')),
                             ('top', {'item_id': 'ffffffffffff'}, (404, 'unknown_item')),
                             ('skip', {'item_id': P.item_id(fixtures.POSTED_KEYS[0])}, (409, 'posted')),
                             ('caption', {'item_id': P.item_id(NEW6), 'caption': ' '}, (422, 'problems'))):
            with self.subTest(op=op, kw=kw), self.assertRaises(P.PublishError) as e:
                P.queue_op(self.root, IG, op, rev, WHO, **kw)
            self.assertEqual((e.exception.status, e.exception.code), want)
        self.assertEqual(P.queue(self.root, IG, now=self.now)['rev'], rev)

    def test_mark_posted(self):
        """P1: a hand record under the PostLock; the item counts as posted; a held lock gives 409 busy."""
        link = 'https://www.instagram.com/reel/FAKEHAND1/'
        with P.PostLock(owner='cli'), self.assertRaises(P.Busy):
            self.op('mark_posted', NEW7, permalink=link)
        with self.assertRaises(P.PublishError) as e:
            self.op('mark_posted', NEW7, permalink='https://example.com/x')
        self.assertEqual(e.exception.code, 'bad_op')
        self.op('mark_posted', NEW7, permalink=link, at=S.iso(self.now - timedelta(hours=1)))
        rec = self.records()[NEW7]
        self.assertEqual((rec['id'], rec['by'], rec['permalink']), ('hand', 'hand', link))
        self.assertNotIn(NEW7, self.waiting())
        self.assertEqual(self.kinds()[-1], 'queue.mark_posted')
        self.assertEqual(self.posts(), [])


# ---------- manual posts ----------

class Fingerprint(Case):
    """The fingerprint ignores the caption and changes with the last post."""
    def test_stable_over_caption(self):
        v = P._View(self.root, self.fake, self.now)
        item = self.item()
        a = P.fingerprint(IG, item, v.hist, self.now, P._checks(v, item, 'short'))
        b = P.fingerprint(IG, item, v.hist, self.now, P._checks(v, item, 'x' * 2300 + ' #a' * 40))
        self.assertEqual(a, b)
        self.assertEqual(a, self.preview()['check'])
        self.assertRegex(a, r'^[0-9a-f]{16}$')

    def test_changes_with_last_post(self):
        before = self.preview()['check']
        self.add_job(key='other/Output/other-short-1.mp4', state='done', media_id='m-90',
                     published=S.iso(self.now - timedelta(minutes=5)))
        self.assertNotEqual(self.preview()['check'], before)


class RefusalOrder(Case):
    """spec 5.4: one test per refusal; each sets up its own condition plus later ones, and its code wins."""
    def refused(self, want, key=NEW6, platform=IG, item=None, **kw):
        with self.assertRaises(P.PublishError) as e:
            P.start_post(self.root, platform, item or P.item_id(key), kw.pop('caption', 'Caption #almanca'), False,
                         kw.pop('check', '0' * 16), kw.pop('override', []), WHO)
        self.assertEqual((e.exception.status, e.exception.code), want)
        self.assertEqual(self.posts(), [])
        return e.exception

    def cap_full(self):
        for n, hours in ((91, 2), (92, 1)):
            self.add_job(key=f'other/Output/other-short-{n}.mp4', state='done', media_id=f'm-{n}',
                         published=S.iso(self.now - timedelta(hours=hours)))

    def test_01_unknown_platform(self):
        self.fake.ready = False
        self.refused((404, 'unknown_platform'), platform='tiktok')

    def test_02_not_set_up(self):
        self.fake.ready = False
        e = self.refused((409, 'not_set_up'), item='ffffffffffff')
        self.assertEqual(e.message, 'Instagram is not set up on this PC.')

    def test_03_unknown_item(self):
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        self.refused((404, 'unknown_item'), item='ffffffffffff')

    def test_04_posted(self):
        self.add_job(key=NEW7, state='interrupted')
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        with P.PostLock(owner='cli'):
            e = self.refused((409, 'posted'), key=fixtures.POSTED_KEYS[0])
        self.assertEqual(e.message, 'This Reel is already on Instagram.')
        self.assertEqual(e.extra['url'], 'https://www.instagram.com/reel/FAKE1/')

    def test_05_unresolved(self):
        jid = self.add_job(key=NEW7, state='running', heartbeat=S.iso(self.now - timedelta(minutes=6)))
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        e = self.refused((409, 'unresolved'))                 # reconcile(api=False) marked it interrupted first
        self.assertEqual(e.extra['job']['id'], jid)
        self.assertEqual(P.job(jid)['state'], 'interrupted')

    def test_06_breaker(self):
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        with P.PostLock(owner='cli'):
            e = self.refused((423, 'breaker'), caption='')
        self.assertEqual(e.message, 'Posting is stopped: Instagram refused the key (code 190). Test the connection in Publish.')
        self.assertEqual(e.extra['breaker']['state'], 'stopped')

    def test_07_busy(self):
        self.add_job(key=NEW7, state='done', created=S.iso(self.now - timedelta(seconds=30)))
        with P.PostLock(owner='cli'):
            self.refused((409, 'busy'), caption='')
        self.add_job(key=NEW7, state='running', heartbeat=S.iso(self.now))
        e = self.refused((409, 'busy'), caption='')
        self.assertEqual(e.extra['job']['state'], 'running')

    def test_08_rate_limited(self):
        self.add_job(key=NEW7, state='done', created=S.iso(self.now - timedelta(seconds=30)))
        self.cap_full()
        e = self.refused((429, 'rate_limited'), caption='')
        self.assertEqual(e.extra['retry_after'], 90)

    def test_09_caption_problems(self):
        self.cap_full()
        e = self.refused((422, 'problems'), caption='x' * 2310)
        self.assertEqual(e.extra['checks'], [{'code': 'caption_too_long', 'level': 'block', 'data': {'chars': 2310},
                                              'text': 'The caption has 2,310 characters; the limit is 2,200.'}])

    def test_10_hard_cap(self):
        self.cap_full()
        self.item().file.unlink()
        e = self.refused((429, 'hard_cap'))
        free = fixtures.LAST_POST + timedelta(hours=24)
        self.assertEqual(e.message, f'3 Reels in the last 24 hours: that is the limit. Next possible {S.fmt(free)}.')
        self.assertEqual(e.extra['next_possible'], S.iso(free))

    def test_10_file_missing(self):
        self.item().file.unlink()
        e = self.refused((422, 'problems'))
        self.assertEqual([c['code'] for c in e.extra['checks']], ['file_missing'])

    def test_11_stale(self):
        e = self.refused((409, 'stale'))
        self.assertEqual(e.message, 'Something changed since you opened this. Check again.')
        self.assertEqual(e.extra['preview']['check'], self.preview()['check'])

    def test_12_confirm(self):
        pv = self.preview()
        e = self.refused((428, 'confirm'), check=pv['check'], override=['gap'])
        self.assertEqual([c['code'] for c in e.extra['checks']], ['gap', 'same_day'])
        self.assertEqual(e.extra['check'], pv['check'])

    def test_13_accept(self):
        body = self.start(save=True, caption='Eigene Caption #almanca')
        self.assertEqual(body['message'], 'Posting new-short-6… (takes 1–3 minutes)')
        self.assertEqual((body['job']['state'], body['job']['by'], body['job']['who']), ('queued', 'manual', WHO))
        self.assertEqual(body['job']['result'], 'Waiting to start…')
        self.assertEqual(P._wait(body['job']['id'])['state'], 'done')
        self.assertEqual(P.caption_for(self.root, self.item())[0], 'Eigene Caption #almanca')
        self.assertEqual(self.posts()[0][1][1], 'Eigene Caption #almanca')
        self.assertIn('post.requested', self.kinds())


class OwnerRegression(Case):
    """S15 and spec 11: pause flag present, settings migrated (auto off), the last Reel 5 h 54 min ago."""
    paused = True

    def test_post_next_reel_with_the_pause_flag(self):
        pv = self.preview()
        warns = [c for c in pv['checks'] if c['level'] == 'warn']
        self.assertEqual([c['code'] for c in warns], ['gap', 'same_day'])
        self.assertEqual(warns[0]['text'], 'The last Reel went out 5 h 54 min ago. The rule is at least 20 h between Reels.')
        self.assertEqual(warns[1]['text'], 'A Reel already went out today at 16:46.')
        self.assertTrue(pv['can_post'])
        body = P.start_post(self.root, IG, pv['item']['id'], pv['caption'], False, pv['check'], ['gap', 'same_day'], WHO)
        j = P._wait(body['job']['id'])
        self.assertEqual((j['state'], j['url'], j['result']), ('done', 'https://www.instagram.com/reel/FAKE6/',
                                                               'Posted on Instagram.'))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.records()[NEW6]['by'], 'manual')
        st = P.state(self.root, IG, now=self.now)
        self.assertEqual((st['counts']['waiting'], st['counts']['posted']), (5, 6))
        self.assertTrue(S.pause_file(IG).exists())
        self.assertEqual((st['auto']['effective'], st['auto']['reason']), (False, 'off'))
        self.assertIn('Settings created from the old pause flag: automatic posting off.',
                      [i['text'] for i in P.activity('settings')['items']])


class FakeOutcomes(Case):
    """Every FakePlatform outcome through run_post (CLI jobs: no manual spacing between them)."""
    def go(self, outcome, key=NEW6):
        jid, caption = self.cli_job(key)
        self.fake.script = [outcome]
        return P.run_post(jid, self.root, caption)

    def test_ok_and_permalink_fails(self):
        j = self.go('ok')
        self.assertEqual((j['state'], j['stage'], j['url']), ('done', 'saving', 'https://www.instagram.com/reel/FAKE6/'))
        rec = self.records()[NEW6]
        self.assertEqual((rec['id'], rec['by'], rec['job'], rec['reconciled']), ('m-6', 'cli', j['id'], False))
        self.assertEqual(rec['posted_utc'], self.now.isoformat())
        j = self.go('permalink_fails', NEW7)
        self.assertEqual((j['state'], j['url']), ('done', None))
        self.assertEqual(self.records()[NEW7]['permalink'], '')

    def test_errors_before_any_container(self):
        for kind in ('auth', 'permission', 'rate', 'transient', 'local', 'unknown'):
            with self.subTest(kind=kind):
                with P._Con(write=True) as con:
                    con.execute('DELETE FROM breakers')
                j = self.go(kind)
                row = self.rows('SELECT * FROM jobs WHERE id = ?', j['id'])[0]
                self.assertEqual((j['state'], j['kind'], row['container_id'], j['stage']), ('failed', kind, None, 'sharing'))
                self.assertEqual(j['result'], f'Not posted: {j["error"]}')
                self.assertEqual(P.breaker('meta')['state'],
                                 {'auth': 'stopped', 'permission': 'stopped', 'rate': 'cooling'}.get(kind, 'closed'))
                self.assertEqual(P.queue(self.root, IG, now=self.now)['waiting'][0]['key'], NEW6)

    def test_a_platform_pause_leaves_the_breaker_closed(self):
        """Item 16: a failure whose exception carries 'until' (the platform itself says wait: YouTube's used-up
        quota) ends the job failed as before but never trips the breaker; a plain 'rate' failure still cools for
        COOL_HOURS[0] = 1 h, and auth still stops."""
        held = F.FakeError('rate')
        held.until = self.now + timedelta(hours=12)
        jid, caption = self.cli_job()
        with mock.patch.object(self.fake, 'post', side_effect=held):
            j = P.run_post(jid, self.root, caption)
        self.assertEqual((j['state'], j['kind'], j['error']), ('failed', 'rate', str(held)))
        self.assertEqual(j['result'], f'Not posted: {held}')
        self.assertEqual(P.breaker('meta')['state'], 'closed')
        with P._Con() as con:
            self.assertIsNone(con.execute("SELECT * FROM breakers WHERE name = 'meta'").fetchone())   # no step either
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit WHERE kind = 'breaker.open'").fetchone()[0], 0)
        self.assertEqual(P.queue(self.root, IG, now=self.now)['waiting'][0]['key'], NEW6)              # still waiting
        j = self.go('rate')
        b = P.breaker('meta')
        self.assertEqual((j['kind'], b['state'], b['until']), ('rate', 'cooling', S.iso(self.now + timedelta(hours=1))))
        with P._Con(write=True) as con:
            con.execute('DELETE FROM breakers')
        none = F.FakeError('auth')
        none.until = None                                   # 'until' None is not a pause: the breaker trips as before
        jid, caption = self.cli_job()
        with mock.patch.object(self.fake, 'post', side_effect=none):
            P.run_post(jid, self.root, caption)
        self.assertEqual(P.breaker('meta')['state'], 'stopped')

    def test_content_marks_the_item_failed(self):
        j = self.go('content')
        self.assertEqual((j['state'], j['kind'], j['error']), ('failed', 'content', 'Instagram could not process the video: ERROR'))
        self.assertEqual(j['result'], 'Not posted. The Reel is marked Failed; check the file, then Try again.')
        q = P.queue(self.root, IG, now=self.now)
        self.assertEqual([i['key'] for i in q['failed']], [NEW6])
        self.assertEqual(self.fake.container_states['c-6'], 'ERROR')

    def test_crash_after_container(self):
        jid = self.crashed('crash_after_container')
        self.assertEqual(self.rows('SELECT container_id, media_id FROM jobs WHERE id = ?', jid)[0],
                         {'container_id': 'c-6', 'media_id': None})
        self.assertIsNone(P.PostLock.holder())               # the finally released our lock

    def test_crash_after_publish(self):
        jid = self.crashed('crash_after_publish', NEW7)
        self.assertEqual(self.rows('SELECT media_id, stage FROM jobs WHERE id = ?', jid)[0],
                         {'media_id': 'm-6', 'stage': 'publishing'})

    def test_unclear_publish_waits_for_a_person(self):
        """An unknown error after media_publish was sent may be live (spec 5.7): the job ends 'unknown' and blocks
        posting. The same error before that stage is a plain failure and the item stays waiting."""
        with mock.patch.object(self.fake, 'post', unclear('processing')):
            j = self.go('ok')
        self.assertEqual((j['state'], j['kind']), ('failed', 'unknown'))
        self.assertEqual(P.queue(self.root, IG, now=self.now)['waiting'][0]['key'], NEW6)
        with mock.patch.object(self.fake, 'post', unclear()):
            j = self.go('ok')
        self.assertEqual((j['state'], j['kind'], j['result']),
                         ('unknown', 'unknown', 'new-short-6.mp4: not clear whether it went out. Check Instagram.'))
        self.assertEqual([x['id'] for x in P.unresolved_jobs(IG)], [j['id']])
        self.assertEqual(self.preview(NEW7)['blocked_by'], 'unresolved')
        self.assertEqual(self.kinds()[-1], 'post.unknown')


class LockHeldDuringPost(Case):
    """S1: post() runs only while the PostLock file holds a live v2 lock for that very job."""
    def setUp(self):
        super().setUp()
        self.seen, real = [], self.fake.post

        def post(item, caption, journal):
            self.seen.append((P.PostLock.holder(), journal.job_id))
            return real(item, caption, journal)
        self.fake.post = post

    def check(self, owner):
        holder, jid = self.seen[-1]
        self.assertEqual((holder['v'], holder['owner'], holder['job']), (2, owner, jid))
        self.assertRegex(holder['token'], r'^[0-9a-f]{32}$')

    def test_manual_auto_cli(self):
        self.post()
        self.check('dashboard')
        self.assertEqual(P.cli_post(self.root / 'new', force=True)[:6], 'Posted')
        self.check('cli')
        self.now = T('2026-11-06 20:05')
        P.auto_step(self.root, now=self.now)
        self.check('watcher')
        self.assertEqual(len(self.seen), 3)
        self.assertIsNone(P.PostLock.holder())


class ManualOverride(Case):
    """S10: warn checks need every code in override and the current check; otherwise 428 / 409 and no post()."""
    def test_override(self):
        pv = self.preview()
        for override, check, want in ((['gap'], pv['check'], 428), ([], pv['check'], 428),
                                      (['gap', 'same_day'], 'f' * 16, 409)):
            with self.subTest(override=override), self.assertRaises(P.PublishError) as e:
                P.start_post(self.root, IG, pv['item']['id'], pv['caption'], False, check, override, WHO)
            self.assertEqual(e.exception.status, want)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.post()['state'], 'done')
        self.assertEqual(len(self.posts()), 1)

    def test_change_under_the_lock_makes_it_stale(self):
        """run_post re-checks under the lock: a post that appeared after the preview stops the job, no post()."""
        pv = self.preview()
        jid = P._new_job(self.fake, self.item(), 'manual', WHO, pv['caption'], self.now, fingerprint=pv['check'],
                         override=['gap', 'same_day'])
        self.add_job(key='other/Output/other-short-1.mp4', state='done', media_id='m-90',
                     published=S.iso(self.now - timedelta(minutes=1)))
        j = P.run_post(jid, self.root, pv['caption'])
        self.assertEqual((j['state'], j['error']), ('stale', 'Something changed since you opened this. Check again.'))
        self.assertEqual(self.posts(), [])


class HardCap(Case):
    """S11: no post at or above hard_cap_24h: manual 429, automatic skips, the CLI refuses even with --force."""
    def setUp(self):
        super().setUp()
        for n, hours in ((91, 2), (92, 1)):
            self.add_job(key=f'other/Output/other-short-{n}.mp4', state='done', media_id=f'm-{n}',
                         published=S.iso(self.now - timedelta(hours=hours)))

    def test_every_path_refuses(self):
        pv = self.preview()
        self.assertEqual(pv['blocked_by'], 'hard_cap')
        self.assertEqual(pv['next_possible'], S.iso(fixtures.LAST_POST + timedelta(hours=24)))
        with self.assertRaises(P.PublishError) as e:
            P.start_post(self.root, IG, pv['item']['id'], pv['caption'], False, pv['check'], ['gap', 'same_day'], WHO)
        self.assertEqual((e.exception.status, e.exception.code), (429, 'hard_cap'))
        self.assertRegex(P.cli_post(self.root / 'new', force=True), r'^Not posted: 3 Reels in the last 24 hours')
        for by in ('cli', 'auto'):                              # jobs made by hand stop under the lock too
            caption, _ = P.caption_for(self.root, self.item())
            jid = P._new_job(self.fake, self.item(), by, by, caption, self.now, slot='2026-11-04',
                             override=['gap', 'same_day'])
            self.assertRegex(P.run_post(jid, self.root, caption)['error'], r'^3 Reels in the last 24 hours')
        rev = P.settings(self.root).doc['rev']                  # automatic: a 1 h gap, so only the cap is left
        S.save_settings(IG, {'min_gap_hours': 1, 'post_after': '00:00'}, rev, by=WHO, ack=['gap_under_20'])
        self.now = T('2026-11-05 00:30')
        cfg = S.platform_settings(P.settings(self.root), IG)
        self.assertEqual(S.slot_open(cfg, P.history(self.root, IG), self.now), (False, 'cap'))
        self.assertEqual(P.auto_step(self.root, now=self.now), [])
        self.assertEqual(self.posts(), [])


class RateLimits(Case):
    """S30: one accepted manual post per 120 s; at most 6 attempts (jobs that reached sharing) per 24 h."""
    def test_spacing(self):
        self.assertEqual(self.post()['state'], 'done')
        self.now += timedelta(seconds=45)
        with self.assertRaises(P.PublishError) as e:
            self.start(NEW7)
        self.assertEqual((e.exception.code, e.exception.extra['retry_after']), ('rate_limited', 75))
        self.now += timedelta(seconds=75)
        self.assertEqual(self.post(NEW7)['state'], 'done')

    def test_stale_jobs_do_not_count(self):
        self.add_job(key=NEW7, state='stale', created=S.iso(self.now - timedelta(seconds=10)))
        self.assertEqual(self.post()['state'], 'done')

    def test_six_attempts(self):
        for n in range(6):
            t = S.iso(self.now - timedelta(hours=23, minutes=50 - n))
            self.add_job(key=NEW7, state='failed', stage='sharing', kind='transient', created=t, started=t)
        with self.assertRaises(P.PublishError) as e:
            self.start()
        self.assertEqual((e.exception.code, e.exception.extra['retry_after']), ('rate_limited', 600))
        self.add_job(key=NEW7, state='failed', stage='preparing', created=S.iso(self.now - timedelta(hours=1)))
        self.now += timedelta(minutes=10)                       # the oldest attempt left the 24 h window
        self.assertEqual(self.post()['state'], 'done')
        self.assertRegex(P.cli_post(self.root / 'new', force=True), r'^Not posted: 6 tries')


# ---------- crash safety ----------

class UnresolvedBlocks(Case):
    """S3: while any job of the platform is interrupted or unknown, nothing posts: manual, CLI, a queued job,
    or the automatic step (which only checks the job)."""
    def test_blocks_every_path(self):
        jid = self.add_job(key=NEW7, state='interrupted', container_id='c-99')
        self.fake.container_states['c-99'] = F.FakeError('transient')        # the check cannot settle it
        self.assertEqual(self.preview()['blocked_by'], 'unresolved')
        self.assertEqual(self.preview(NEW7)['blocked_by'], 'checking')
        with self.assertRaises(P.PublishError) as e:
            self.start()
        self.assertEqual((e.exception.status, e.exception.code), (409, 'unresolved'))
        self.assertEqual(P.cli_post(self.root / 'new', force=True), 'Not posted: An earlier post was interrupted. Check it first.')
        queued, caption = self.cli_job()
        j = P.run_post(queued, self.root, caption)
        self.assertEqual((j['state'], j['error']), ('stale', 'An earlier post was interrupted. Check it first.'))
        self.auto_on()
        lines = P.auto_step(self.root, now=self.now)
        self.assertEqual(lines, ['Instagram: new-short-7.mp4: could not check the post (transient): '
                                 'Instagram did not answer (HTTP 503).'])
        self.assertEqual(self.posts(), [])
        with self.assertRaises(P.PublishError) as e:              # G1: never asked about the upload yet
            P.resolve_job(self.root, jid, 'not_posted', WHO)
        self.assertEqual(e.exception.code, 'check_first')
        self.fake.container_states['c-99'] = 'EXPIRED'
        self.now += timedelta(minutes=11)
        self.assertEqual(P.resolve_job(self.root, jid, 'check', WHO)['job']['state'], 'failed')
        self.assertEqual(self.post()['state'], 'done')

    def test_unknown_blocks_until_resolved(self):
        """An unknown job blocks posting; the PC checks it again by itself every UNKNOWN_EVERY s."""
        self.auto_on()
        jid = self.add_job(key=NEW7, state='unknown', container_id='c-99', heartbeat=S.iso(self.now))
        self.fake.container_states['c-99'] = 'IN_PROGRESS'
        self.assertEqual(P.auto_step(self.root, now=self.now),
                         ['Instagram: new-short-7.mp4: the upload is IN_PROGRESS; it may still go out, so it is '
                          'checked again later.'])
        self.assertEqual(self.posts(), [])
        self.assertEqual([j['id'] for j in P.unresolved_jobs(IG)], [jid])
        self.assertEqual([j['id'] for j in P.state(self.root, IG, now=self.now)['unresolved']], [jid])
        with self.assertRaises(P.PublishError) as e:              # G1: Meta may still publish it
            P.resolve_job(self.root, jid, 'not_posted', WHO)
        self.assertEqual(e.exception.code, 'too_early')
        calls = len(self.fake.calls)
        self.now += timedelta(minutes=10)
        self.assertEqual(P.auto_step(self.root, now=self.now), [])
        self.assertEqual(len(self.fake.calls), calls)             # not asked again within UNKNOWN_EVERY
        self.fake.container_states['c-99'] = 'EXPIRED'
        self.now += timedelta(minutes=21)
        lines = P.auto_step(self.root, now=self.now)
        self.assertEqual(lines[0], 'Instagram: Instagram could not process new-short-7.mp4: EXPIRED; marked Failed.')
        self.assertEqual([c[1][0] for c in self.posts()], [NEW6])


class CrashAfterPublish(Case):
    """S4: once journal.published() ran, the item counts as posted at once and is never posted again."""
    def test_never_posted_twice(self):
        jid = self.crashed('crash_after_publish')
        q = P.queue(self.root, IG, now=self.now)
        self.assertNotIn(NEW6, [i['key'] for i in q['waiting']])
        self.assertEqual((q['posted'][0]['key'], q['posted'][0]['posted']['by']), (NEW6, 'cli'))
        self.assertEqual(P.reconcile(self.root, IG, api=False, now=self.now),
                         [f'Instagram: new-short-6.mp4: the post was interrupted (job {jid}); it is checked before '
                          'anything else is posted.'])
        with self.assertRaises(P.PublishError) as e:
            self.start(NEW6)
        self.assertEqual(e.exception.code, 'posted')
        P.reconcile(self.root, IG, now=self.now)
        j = P.job(jid)
        self.assertEqual((j['state'], j['record_pending']), ('done', False))
        rec = self.records()[NEW6]
        self.assertEqual((rec['id'], rec['by'], rec['job'], rec['reconciled']), ('m-6', 'cli', jid, True))
        self.auto_on()
        P.auto_step(self.root, now=self.now)                    # posts new-short-7, never 6 again
        self.assertEqual([c[1][0] for c in self.posts()], [NEW6, NEW7])


class RecordFails(Case):
    """S4, S5: a record write failing after the post is live ends the job done with the record pending; the
    item is posted, never failed; reconcile writes the record later; an automatic run counts no failure."""
    def test_manual(self):
        self.fake.script = ['record_fails']
        j = self.post()
        self.assertEqual((j['state'], j['record_pending'], j['kind']), ('done', True, None))
        self.assertEqual(j['result'], 'Posted on Instagram. The Drive record is saved later.')
        self.assertNotIn(NEW6, self.records())
        q = P.queue(self.root, IG, now=self.now)
        self.assertEqual((q['posted'][0]['key'], q['failed']), (NEW6, []))
        self.assertIn(f'record_pending:{j["id"]}', [a['id'] for a in P.attention(self.root, now=self.now)])
        self.assertEqual(self.kinds()[-2:], ['post.done', 'record.pending'])
        self.assertEqual(S.posts_in_24h(P.history(self.root, IG), self.now), 2)     # counted from the job
        self.assertEqual(P.reconcile(self.root, IG, api=False, now=self.now), [])    # G9: a GET writes nothing
        self.assertEqual(P.reconcile(self.root, IG, now=self.now),
                         ['Instagram: new-short-6.mp4: the Drive record is saved now.'])
        self.assertEqual(self.records()[NEW6]['id'], 'm-6')
        self.assertFalse(P.job(j['id'])['record_pending'])
        self.assertEqual(self.kinds()[-1], 'record.written')
        self.assertEqual(P.state(self.root, IG, now=self.now)['posts_24h'], 2)    # not counted twice
        self.assertEqual(len(self.posts()), 1)

    def test_automatic(self):
        self.auto_on()
        self.fake.script = ['record_fails']
        P.auto_step(self.root, now=self.now)
        self.assertEqual(self.rows('SELECT failures, suspended FROM auto_state'), [{'failures': 0, 'suspended': None}])
        self.assertEqual(self.rows('SELECT state, record_pending FROM jobs'), [{'state': 'done', 'record_pending': 1}])
        self.now += timedelta(minutes=10)
        lines = P.auto_step(self.root, now=self.now)            # step 1 of the next run writes the record
        self.assertEqual(lines, ['Instagram: new-short-6.mp4: the Drive record is saved now.'])
        self.assertEqual(len(self.posts()), 1)


class ReconcileTable(Case):
    """S6 and the spec 5.7 table, row by row. Reconcile never publishes: post() is never called again and a
    FINISHED or IN_PROGRESS container is never sent later."""
    def settle(self, jid):
        before = len(self.posts())
        P.reconcile(self.root, IG, now=self.now)
        self.assertEqual(len(self.posts()), before)
        return P.job(jid)

    def container(self, status):
        """A job that died after its container; the container now reads status."""
        jid = self.crashed('crash_after_container')
        self.fake.container_states['c-6'] = status
        return jid

    def test_media_id(self):
        j = self.settle(self.crashed('crash_after_publish'))
        self.assertEqual((j['state'], self.records()[NEW6]['reconciled']), ('done', True))
        self.assertEqual(self.kinds()[-2:], ['post.interrupted', 'post.reconciled'])
        self.assertEqual([c[0] for c in self.fake.calls], ['post'])         # no API call needed

    def test_published_with_a_match(self):
        jid = self.container('PUBLISHED')
        caption, _ = P.caption_for(self.root, self.item())
        self.fake.media[:0] = [{'id': 'm-other', 'at': S.iso(self.now), 'caption': 'Something else', 'permalink': ''},
                               {'id': 'm-77', 'at': S.iso(self.now - timedelta(minutes=5)), 'caption': caption + ' ',
                                'permalink': 'https://www.instagram.com/reel/FAKE77/', 'product': 'REELS'}]
        j = self.settle(jid)
        self.assertEqual((j['state'], j['url']), ('done', 'https://www.instagram.com/reel/FAKE77/'))
        self.assertEqual(self.records()[NEW6]['id'], 'm-77')
        self.assertEqual([c[0] for c in self.fake.calls], ['post', 'container_status', 'recent_media'])

    def test_published_without_a_match(self):
        jid = self.container('PUBLISHED')
        self.fake.media[:0] = [{'id': 'm-old', 'at': S.iso(self.now - timedelta(hours=2)), 'caption': 'old'}]
        self.assertEqual(self.settle(jid)['state'], 'unknown')
        self.assertIn(f'post_unresolved:{jid}', [a['id'] for a in P.attention(self.root, now=self.now)])
        started = P._time(self.rows('SELECT started FROM jobs WHERE id = ?', jid)[0]['started'])
        self.assertIn(started, [p.at for p in P.history(self.root, IG)])   # counts for the timing rules
        self.now += timedelta(minutes=20)
        P.reconcile(self.root, IG, now=self.now)                # an unknown job waits for a person
        self.assertEqual(len(self.fake.calls), 3)

    def never_sent(self, status):
        j = self.settle(self.container(status))
        self.assertEqual((j['state'], j['kind']), ('failed', 'local'))
        failed = P.queue(self.root, IG, now=self.now)['failed'][0]
        self.assertEqual((failed['key'], failed['failed']['reason']),
                         (NEW6, 'The post was interrupted before it went out; check Instagram, then Try again.'))
        self.auto_on()
        P.auto_step(self.root, now=self.now)                    # the next run posts the next Reel, not this one
        self.assertEqual([c[1][0] for c in self.posts()], [NEW6, NEW7])
        self.assertEqual(self.fake.container_states['c-6'], status)

    def test_finished(self):
        self.never_sent('FINISHED')

    def test_in_progress(self):
        self.never_sent('IN_PROGRESS')

    def processing_failed(self, status, kind):
        j = self.settle(self.container(status))
        self.assertEqual((j['state'], j['kind'], j['error']),
                         ('failed', kind, f'Instagram could not process new-short-6.mp4: {status}'))
        self.assertEqual(P.queue(self.root, IG, now=self.now)['failed'][0]['key'], NEW6)

    def test_error(self):
        self.processing_failed('ERROR', 'content')

    def test_expired(self):
        self.processing_failed('EXPIRED', 'transient')

    def test_no_container(self):
        jid = self.add_job(stage='preparing')
        j = self.settle(jid)
        self.assertEqual((j['state'], j['kind'], j['error']), ('failed', 'local', 'Interrupted before sending; nothing was posted'))
        self.assertEqual(P.queue(self.root, IG, now=self.now)['waiting'][0]['key'], NEW6)
        self.assertEqual(self.fake.calls, [])

    def test_api_rules(self):
        """api=False makes no call; one check per job per 10 min; none while the breaker is open; auth trips it."""
        jid = self.container(F.FakeError('transient'))
        P.reconcile(self.root, IG, api=False, now=self.now)
        self.assertEqual(len(self.fake.calls), 1)
        P.reconcile(self.root, IG, now=self.now)
        P.reconcile(self.root, IG, now=self.now + timedelta(minutes=5))
        self.assertEqual([c[0] for c in self.fake.calls], ['post', 'container_status'])
        self.assertEqual((P.job(jid)['state'], P.breaker('meta')['state']), ('interrupted', 'closed'))
        self.fake.container_states['c-6'] = F.FakeError('auth')
        P.reconcile(self.root, IG, now=self.now + timedelta(minutes=11))
        self.assertEqual(P.breaker('meta')['state'], 'stopped')
        P.reconcile(self.root, IG, now=self.now + timedelta(minutes=30))
        self.assertEqual(len(self.fake.calls), 3)

    def test_running_job_with_its_lock_is_left_alone(self):
        jid = self.add_job()
        with mock.patch.object(P, 'LOCK_BEAT', 60), P.PostLock('watcher', job=jid):
            P.reconcile(self.root, IG, api=False, now=self.now)
            self.assertEqual(P.job(jid)['state'], 'running')
        P.reconcile(self.root, IG, api=False, now=self.now)
        self.assertEqual(P.job(jid)['state'], 'interrupted')


class TwoThreadRace(Case):
    """S2: two simultaneous start_post calls make exactly one job (the other gets 409 busy); two run_post calls
    at once post once (the other ends stale); a lock held by another process means busy."""
    def setUp(self):
        super().setUp()
        self.gate, real = threading.Event(), self.fake.post

        def post(item, caption, journal):                     # post() waits until the test opens the gate
            self.gate.wait(2)
            return real(item, caption, journal)
        self.fake.post = post
        self.addCleanup(self.gate.set)

    def test_start_post(self):
        pv, barrier, results = self.preview(), threading.Barrier(2), []
        warns = [c['code'] for c in pv['checks'] if c['level'] == 'warn']

        def go():
            barrier.wait()
            try:
                results.append(P.start_post(self.root, IG, pv['item']['id'], pv['caption'], False, pv['check'],
                                            warns, WHO)['job']['state'])
            except P.PublishError as e:
                results.append(e.code)
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(results), ['busy', 'queued'])
        self.assertEqual(len(self.rows('SELECT id FROM jobs')), 1)
        self.gate.set()
        self._join()
        self.assertEqual(len(self.posts()), 1)

    def test_run_post(self):
        a, cap6 = self.cli_job()
        cap7, _ = P.caption_for(self.root, self.item(NEW7))
        b = self.add_job(key=NEW7, state='queued', by='cli', created=S.iso(self.now), started=None, heartbeat=None,
                         override='["gap", "same_day"]', caption_sha=P._sha(cap7), caption_head=P._head(cap7))
        barrier = threading.Barrier(2)

        def go(jid, caption):
            barrier.wait()
            P.run_post(jid, self.root, caption)
        threads = [threading.Thread(target=go, args=x) for x in ((a, cap6), (b, cap7))]
        for t in threads:
            t.start()
        for _ in range(200):                                    # the loser ends at once; the winner waits
            if 'stale' in (P.job(a)['state'], P.job(b)['state']):
                break
            threading.Event().wait(0.01)
        self.gate.set()
        for t in threads:
            t.join(10)
        jobs = sorted((P.job(a), P.job(b)), key=lambda j: j['state'])
        self.assertEqual([j['state'] for j in jobs], ['done', 'stale'])
        self.assertEqual(jobs[1]['error'], 'Another post started at the same moment. Nothing was posted; check again.')
        self.assertEqual(len(self.posts()), 1)

    def test_lock_of_another_process(self):
        self.gate.set()
        self.lock_file().write_text(json.dumps({'v': 2, 'token': 'b' * 32, 'pid': 99999, 'owner': 'watcher',
                                                'job': None, 'started': S.iso(datetime.now(timezone.utc))}))
        with self.assertRaises(P.PublishError) as e:
            self.start()
        self.assertEqual(e.exception.code, 'busy')
        jid, caption = self.cli_job()
        self.assertEqual(P.run_post(jid, self.root, caption)['state'], 'stale')
        self.assertEqual(self.posts(), [])
        self.assertEqual(json.loads(self.lock_file().read_text())['token'], 'b' * 32)    # never removed


# ---------- the lock ----------

class LockTakeover(Case):
    """S28: takeover follows the stale rules; only the holder with the matching token deletes the file;
    os.kill is never used."""
    def write(self, content, age):
        f = self.lock_file()
        f.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
        t = datetime.now(timezone.utc).timestamp() - age
        os.utime(f, (t, t))

    def v2(self, started_age=10):
        return {'v': 2, 'token': 'a' * 32, 'pid': 99999, 'owner': 'watcher', 'job': None,
                'started': S.iso(datetime.now(timezone.utc) - timedelta(seconds=started_age))}

    def busy(self):
        with self.assertRaises(P.Busy):
            P.PostLock('cli').__enter__()

    def taken(self, old):
        with P.PostLock('cli') as lock:
            self.assertEqual(json.loads(self.lock_file().read_text())['token'], lock.token)
            aside = list(self.lock_file().parent.glob('instagram-posting.lock.stale-*'))
            self.assertEqual(len(aside), 1)
            self.assertIn(old, aside[0].read_text())
        self.assertFalse(self.lock_file().exists())

    def test_v2(self):
        self.write(self.v2(), 290)
        self.busy()
        self.assertEqual(P.PostLock.holder()['token'], 'a' * 32)
        self.write(self.v2(), 310)
        self.assertIsNone(P.PostLock.holder())
        self.taken('a' * 32)

    def test_v1(self):
        self.write(b'1234', 1790)
        self.assertEqual(P.PostLock.holder(), {'v': 1, 'pid': 1234})
        self.busy()
        self.write(b'1234', 1810)
        self.taken('1234')

    def test_hard_limit(self):
        self.write(self.v2(started_age=3590), 5)
        self.busy()
        self.write(self.v2(started_age=3610), 5)                # touched just now, but started over an hour ago
        self.taken('a' * 32)

    def test_foreign_token_kept(self):
        lock = P.PostLock('cli').__enter__()
        self.write(self.v2(), 0)                                # someone else's lock replaced ours
        lock.__exit__(None, None, None)
        self.assertEqual(json.loads(self.lock_file().read_text())['token'], 'a' * 32)

    def test_no_os_kill(self):
        for name in ('publish.py', 'platform_fake.py'):
            src = (guard.PIPELINE / name).read_text(encoding='utf-8')
            self.assertIsNone(re.search(r'\bos\s*\.\s*kill\s*\(|\bkillpg\b|\bimport\s+signal\b', src), name)


class Heartbeat(Case):
    """The lock's thread touches the lock file and the job's heartbeat every LOCK_BEAT s (patched to 0.05 s)."""
    def test_beats(self):
        jid = self.add_job(heartbeat=S.iso(self.now - timedelta(minutes=10)))
        beat = lambda: self.rows('SELECT heartbeat FROM jobs WHERE id = ?', jid)[0]['heartbeat']
        with mock.patch.object(P, 'LOCK_BEAT', 0.05), P.PostLock('watcher', job=jid):
            old = datetime.now(timezone.utc).timestamp() - 200
            os.utime(self.lock_file(), (old, old))
            for _ in range(100):
                if self.lock_file().stat().st_mtime > old + 100 and beat() == S.iso(self.now):
                    break
                threading.Event().wait(0.02)
            self.assertGreater(self.lock_file().stat().st_mtime, old + 100)
            self.assertEqual(beat(), S.iso(self.now))
            self.assertEqual(P.PostLock.holder()['job'], jid)


# ---------- the automatic step ----------

class AutoIdleNoNetwork(Case):
    """S16: no open slot and no interrupted job -> auto_step makes zero NETWORK calls."""
    def test_idle(self):
        for t in (fixtures.NOW, T('2026-11-05 12:00'), T('2026-11-05 19:59')):
            self.now = t
            self.assertEqual(P.auto_step(self.root, now=t), [])
        self.assertEqual(self.fake.calls, [])

    def test_interrupted_job_checked_once_per_10_min(self):
        self.crashed('crash_after_container')
        self.fake.container_states['c-6'] = F.FakeError('transient')
        for minutes in (0, 5, 9, 11):
            P.auto_step(self.root, now=self.now + timedelta(minutes=minutes))
        self.assertEqual([c[0] for c in self.fake.calls], ['post', 'container_status', 'container_status'])


class SlotAttempts(Case):
    """S7: at most 2 automatic attempts per platform per Istanbul day; the second only after transient or
    local and at least 30 min after the first."""
    def run_at(self, text):
        self.now = T(text)
        return P.auto_step(self.root, now=self.now)

    def test_retry_after_30_min_once(self):
        self.fake.script = ['transient'] * 3
        self.run_at('2026-11-05 20:05')
        self.assertEqual(self.rows('SELECT attempts, last_kind FROM slots'), [{'attempts': 1, 'last_kind': 'transient'}])
        self.run_at('2026-11-05 20:15')
        self.run_at('2026-11-05 20:34')
        self.assertEqual(len(self.posts()), 1)
        self.run_at('2026-11-05 20:36')                         # 31 min later: the second try
        self.assertEqual(len(self.posts()), 2)
        for t in ('2026-11-05 21:10', '2026-11-05 23:30'):
            self.run_at(t)
        self.assertEqual(len(self.posts()), 2)                  # never a third
        self.assertEqual(self.rows('SELECT attempts FROM slots')[0]['attempts'], 2)
        self.assertEqual(self.rows('SELECT failures FROM auto_state')[0]['failures'], 2)
        self.run_at('2026-11-06 20:05')                         # the next day is a new slot
        self.assertEqual(len(self.posts()), 3)

    def no_second_try(self, kind):
        self.fake.script = [kind]
        self.run_at('2026-11-05 20:05')
        self.run_at('2026-11-05 21:00')
        self.assertEqual(len(self.posts()), 1)

    def test_unknown_gets_no_second_try(self):
        self.no_second_try('unknown')

    def test_content_gets_no_second_try(self):
        self.no_second_try('content')
        self.assertEqual(P.queue(self.root, IG, now=self.now)['failed'][0]['key'], NEW6)

    def test_local_may_retry(self):
        self.fake.script = ['local']
        self.run_at('2026-11-05 20:05')
        self.run_at('2026-11-05 20:40')
        self.assertEqual([j['state'] for j in self.rows('SELECT state FROM jobs ORDER BY created')], ['failed', 'done'])


class Suspension(Case):
    """S8: after 3 failed automatic attempts in a row automatic posting is suspended until resume_auto with
    ack 'resume_auto'; any successful post resets the count."""
    def test_three_failures_suspend(self):
        self.fake.script = ['unknown'] * 3
        lines = []
        for day in ('2026-11-05', '2026-11-06', '2026-11-07'):
            self.now = T(f'{day} 20:05')
            lines = P.auto_step(self.root, now=self.now)
        self.assertEqual(len(self.posts()), 3)
        self.assertIn('Instagram: automatic posting is SUSPENDED after 3 failed tries; resume it in the dashboard.', lines)
        st = self.rows('SELECT failures, suspended FROM auto_state')[0]
        self.assertEqual((st['failures'], st['suspended']), (3, S.iso(self.now)))
        self.assertEqual(self.kinds().count('auto.suspended'), 1)
        self.now = T('2026-11-08 20:05')
        self.assertEqual(P.auto_step(self.root, now=self.now), [])
        self.assertEqual(len(self.posts()), 3)
        auto = P.state(self.root, IG, now=self.now)['auto']
        self.assertEqual((auto['effective'], auto['reason'], auto['suspended']['since']), (True, 'suspended', st['suspended']))
        self.assertEqual(P.next_run(self.root, now=self.now)[0]['text'], 'Instagram: automatic posting SUSPENDED (6 waiting)')
        self.assertIn('auto_suspended:instagram', [a['id'] for a in P.attention(self.root, now=self.now)])
        with self.assertRaises(P.PublishError) as e:
            P.resume_auto(IG, [], WHO)
        self.assertEqual((e.exception.status, e.exception.code, e.exception.extra['needs_ack']), (428, 'confirm', ['resume_auto']))
        body = P.resume_auto(IG, ['resume_auto'], WHO)
        self.assertEqual((body['message'], body['auto']['reason'], body['auto']['suspended']),
                         ('Automatic posting resumed.', 'scheduled', None))
        self.assertEqual(self.rows('SELECT failures, suspended FROM auto_state')[0], {'failures': 0, 'suspended': None})
        self.assertEqual(self.kinds()[-1], 'auto.resumed')
        with self.assertRaises(P.PublishError) as e:
            P.resume_auto(IG, ['resume_auto'], WHO)
        self.assertEqual((e.exception.status, e.exception.code), (409, 'not_suspended'))
        P.auto_step(self.root, now=self.now)
        self.assertEqual(len(self.posts()), 4)

    def test_suspension_stores_the_raw_error_not_the_page_text(self):
        """plain() is for the page only: suspended_why, the audit row and the log keep Meta's whole text."""
        long = 'Meta 400: ' + 'Kelime uzun ' * 30 + 'see https://developers.facebook.com/docs/graph-api (code 100)'
        self.fake.script = ['unknown'] * 3
        lines = []
        with mock.patch.dict(F._TEXTS, {'unknown': long}):
            for day in ('2026-11-05', '2026-11-06', '2026-11-07'):
                self.now = T(f'{day} 20:05')
                lines = P.auto_step(self.root, now=self.now)
        raw = self.rows('SELECT error FROM jobs ORDER BY created DESC')[0]['error']
        self.assertIn('(code 100)', raw)
        self.assertEqual(self.rows('SELECT suspended_why FROM auto_state')[0]['suspended_why'], raw)
        self.assertIn(raw, self.rows("SELECT text FROM audit WHERE kind = 'auto.suspended'")[0]['text'])
        self.assertTrue(any(line.endswith(raw) for line in lines), lines)

    def test_a_success_resets_the_count(self):
        self.fake.script = ['transient', 'transient']
        for t in ('2026-11-05 20:05', '2026-11-05 20:40'):
            self.now = T(t)
            P.auto_step(self.root, now=self.now)
        self.assertEqual(self.rows('SELECT failures FROM auto_state')[0]['failures'], 2)
        self.now = T('2026-11-05 21:00')
        self.assertEqual(self.post()['state'], 'done')          # a manual post
        self.assertEqual(self.rows('SELECT failures FROM auto_state')[0]['failures'], 0)


class BreakerKinds(Case):
    """S9: auth or permission errors stop every post until a connection test succeeds; rate errors stop them
    for 1, 2, 4, 8, 16, then 24 h; a successful post resets the step."""
    def test_stopped_until_a_test_succeeds(self):
        P.trip('meta', 'permission', 'Instagram refused the permission (code 10).')
        self.now = T('2026-11-20 20:05')                        # two weeks later: still stopped
        b = P.breaker('meta')
        self.assertEqual((b['state'], b['until'], b['kind']), ('stopped', None, 'permission'))
        with self.assertRaises(P.PublishError) as e:
            self.start()
        self.assertEqual((e.exception.status, e.exception.code), (423, 'breaker'))
        self.assertEqual(P.auto_step(self.root, now=self.now), [])
        self.assertEqual(P.cli_post(self.root / 'new', force=True),
                         'Not posted: Posting is stopped: Instagram refused the permission (code 10). '
                         'Test the connection in Publish.')
        self.assertEqual(self.fake.calls, [])
        P.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')
        b = P.breaker('meta')                                   # a rate error never shortens a stop
        self.assertEqual((b['state'], b['kind']), ('stopped', 'permission'))
        self.fake.test_error = 'auth'
        with self.assertRaises(P.PublishError) as e:
            P.test_platform(IG, WHO)
        self.assertEqual((e.exception.status, e.exception.code, e.exception.extra['kind']), (502, 'test_failed', 'auth'))
        self.assertEqual(e.exception.message, 'Instagram refused the key (code 190). Make a new key on the PC '
                                              '(instagram.py system-token).')
        self.assertEqual(e.exception.extra['breaker']['state'], 'stopped')
        self.now += timedelta(minutes=5)
        with self.assertRaises(P.PublishError) as e:
            P.test_platform(IG, WHO)
        self.assertEqual((e.exception.status, e.exception.code, e.exception.extra['retry_after']), (429, 'rate_limited', 300))
        self.now += timedelta(minutes=5)
        self.fake.test_error = None
        body = P.test_platform(IG, WHO)
        self.assertEqual((body['message'], body['breaker']['state']), ('Instagram answers. Posting is allowed again.', 'closed'))
        self.assertEqual(self.kinds()[-2:], ['breaker.closed', 'platform.test'])
        P.auto_step(self.root, now=self.now)
        self.assertEqual([c[0] for c in self.fake.calls], ['test', 'test', 'post'])
        self.fake.ready = False
        with self.assertRaises(P.PublishError) as e:
            P.test_platform(IG, WHO)
        self.assertEqual((e.exception.status, e.exception.code), (409, 'not_set_up'))

    def test_rate_cooling_steps(self):
        for step, hours in enumerate((1, 2, 4, 8, 16, 24, 24), 1):
            b = P.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')
            self.assertEqual((b['state'], b['until']), ('cooling', S.iso(self.now + timedelta(hours=hours))))
            self.assertEqual(self.rows('SELECT step FROM breakers')[0]['step'], step)
            self.now = P._time(b['until']) - timedelta(seconds=1)
            self.assertEqual(P.breaker('meta')['state'], 'cooling')
            with self.assertRaises(P.PublishError) as e:
                P.start_post(self.root, IG, P.item_id(NEW6), 'Caption', False, '0' * 16, [], WHO)
            self.assertEqual(e.exception.code, 'breaker')
            self.now += timedelta(seconds=1)
            self.assertEqual(P.breaker('meta')['state'], 'closed')
        self.assertEqual(self.posts(), [])

    def test_a_post_resets_the_step(self):
        for _ in range(2):
            P.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')
        self.now = T('2026-11-05 21:00')
        self.assertEqual(self.post()['state'], 'done')
        self.assertEqual(self.rows('SELECT state, step FROM breakers'), [{'state': 'closed', 'step': 0}])
        b = P.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')
        self.assertEqual(b['until'], S.iso(self.now + timedelta(hours=1)))


class AutoStep(Case):
    """auto_step: a preflight-failed item is marked failed without counting an attempt and the next item goes;
    P1: a slot with nothing waiting writes one 'auto.missed' row and a Home card."""
    def test_preflight_failed_item_is_skipped(self):
        self.auto_on()
        self.item().file.unlink()
        self.assertEqual(P.auto_step(self.root, now=self.now),       # F7: Drive may be away for a moment
                         ['Instagram: new-short-6.mp4 could not be read (The video file is missing on Drive.); tried '
                          'again in the next run, marked Failed if it still cannot be read 30 min later.'])
        self.assertEqual((self.posts(), P.queue(self.root, IG, now=self.now)['failed']), ([], []))
        self.now += timedelta(minutes=30)
        lines = P.auto_step(self.root, now=self.now)
        self.assertEqual(lines[0], 'Instagram: new-short-6.mp4 marked Failed before posting: The video file is missing on Drive.')
        self.assertEqual([c[1][0] for c in self.posts()], [NEW7])
        self.assertEqual(self.rows('SELECT attempts FROM slots')[0]['attempts'], 1)
        failed = P.queue(self.root, IG, now=self.now)['failed']
        self.assertEqual([(i['key'], i['failed']['reason']) for i in failed], [(NEW6, 'The video file is missing on Drive.')])
        self.assertIn('item.failed', self.kinds())

    def test_missed_slot(self):
        for key in WAITING:
            P.queue_op(self.root, IG, 'skip', P.queue(self.root, IG, now=self.now)['rev'], WHO, item_id=P.item_id(key))
        self.auto_on()
        self.assertEqual(P.auto_step(self.root, now=self.now),
                         ['Instagram: the slot of Thu 5 Nov passed without a Reel: none was waiting.'])
        self.now += timedelta(minutes=30)
        self.assertEqual(P.auto_step(self.root, now=self.now), [])
        self.assertEqual(self.kinds().count('auto.missed'), 1)
        self.assertIn('auto_missed:instagram:2026-11-05', [a['id'] for a in P.attention(self.root, now=self.now)])
        self.assertEqual(self.fake.calls, [])

    def test_unclear_post(self):
        """An automatic post that may be live: no 'not posted' line, no failure count, nothing more in that slot."""
        self.auto_on()
        with mock.patch.object(self.fake, 'post', unclear()):
            lines = P.auto_step(self.root, now=self.now)
        self.assertEqual(lines[-1], 'Instagram: new-short-6.mp4: not clear whether it went out. Check Instagram.')
        self.assertFalse(any(r['failures'] for r in self.rows('SELECT failures FROM auto_state')))
        self.fake.container_states['c-70'] = 'PUBLISHED'           # live, but not in the list yet
        self.now += timedelta(minutes=40)
        self.assertEqual(P.auto_step(self.root, now=self.now),
                         ['Instagram: new-short-6.mp4: the upload says PUBLISHED; the Reel is still not in the list.'])
        self.assertEqual(self.rows('SELECT by, state FROM jobs'), [{'by': 'auto', 'state': 'unknown'}])


class NoteMedia(Case):
    """note_media: the media cache; empty permalinks filled under the lock; outside posts count for the rules;
    P1: a hand record's id replaced by the real one. Never raises."""
    def live_without_link(self) -> str:
        self.fake.script = ['permalink_fails']
        jid, caption = self.cli_job()
        P.run_post(jid, self.root, caption)
        self.assertEqual(self.records()[NEW6]['permalink'], '')
        return caption

    def test_permalink_fill(self):
        caption = self.live_without_link()
        media = [{'id': 'm-6', 'at': S.iso(self.now), 'permalink': 'https://www.instagram.com/reel/FAKE6/',
                  'caption': caption, 'product': 'REELS', 'likes': 3, 'comments': 1}]
        self.assertEqual(P.note_media(self.root, IG, media), 1)
        self.assertEqual(self.records()[NEW6]['permalink'], 'https://www.instagram.com/reel/FAKE6/')
        self.assertEqual(P.note_media(self.root, IG, media), 0)
        info = P.queue(self.root, IG, now=self.now)['posted'][0]['posted']
        self.assertEqual((info['likes'], info['comments']), (3, 1))
        self.assertEqual(S.posts_in_24h(P.history(self.root, IG), self.now), 2)    # not counted twice
        self.assertEqual(self.kinds()[-1], 'record.written')

    def test_busy_lock_skips_the_records(self):
        self.live_without_link()
        with P.PostLock('cli'):
            self.assertEqual(P.note_media(self.root, IG, [{'id': 'm-6', 'at': S.iso(self.now),
                                                           'permalink': 'https://www.instagram.com/reel/FAKE6/'}]), 0)
        self.assertEqual(self.records()[NEW6]['permalink'], '')
        self.assertEqual(self.rows('SELECT permalink FROM media WHERE id = ?', 'm-6')[0]['permalink'],
                         'https://www.instagram.com/reel/FAKE6/')

    def test_outside_posts_count(self):
        P.note_media(None, IG, [{'id': 'm-app', 'at': S.iso(self.now - timedelta(hours=1)), 'caption': 'Posted in the app',
                                 'permalink': 'https://www.instagram.com/reel/OUTSIDE1/'},
                                {'id': 'FAKE_MEDIA_X', 'at': S.iso(fixtures.LAST_POST),
                                 'permalink': 'https://www.instagram.com/reel/FAKE5/'}])      # = a record's link
        hist = P.history(self.root, IG)
        self.assertEqual(S.posts_in_24h(hist, self.now), 2)
        self.assertEqual((hist[-1].by, hist[-1].id), ('outside', 'm-app'))
        self.assertEqual(P.preview(self.root, IG, P.item_id(NEW6), now=self.now)['checks'][0]['text'],
                         'The last Reel went out 1 h 0 min ago. The rule is at least 20 h between Reels.')

    def test_hand_id_swap(self):
        link = 'https://www.instagram.com/reel/FAKEHAND7/'
        P.queue_op(self.root, IG, 'mark_posted', P.queue(self.root, IG, now=self.now)['rev'], WHO,
                   item_id=P.item_id(NEW7), permalink=link)
        self.assertEqual(self.records()[NEW7]['id'], 'hand')
        self.assertEqual(P.note_media(self.root, IG, [{'id': 'm-real-7', 'at': S.iso(self.now), 'permalink': link}]), 1)
        self.assertEqual((self.records()[NEW7]['id'], self.records()[NEW7]['permalink']), ('m-real-7', link))

    def test_never_raises(self):
        self.assertEqual(P.note_media(self.root, 'tiktok', [{'id': 'x', 'at': S.iso(self.now)}]), 0)
        self.assertEqual(P.note_media(self.root, IG, [None, {'id': 'no-time'}, {'at': 'bad'}]), 0)
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM media')[0]['n'], 0)


class CliPost(Case):
    """cli_post: warn checks refuse without --force; --dry-run makes no call and no job; posts by 'cli'.
    (The hard cap with --force: HardCap; an unresolved job: UnresolvedBlocks; the breaker: BreakerKinds.)"""
    def test_warns_need_force(self):
        self.assertEqual(P.cli_post(self.root / 'new'), 'Not posted: The last Reel went out 5 h 54 min ago. The rule is at '
                         'least 20 h between Reels. A Reel already went out today at 16:46. Use --force to post anyway.')
        self.assertEqual(self.posts(), [])
        self.assertEqual(P.cli_post(self.root / 'new', force=True), 'Posted new-short-6.mp4: https://www.instagram.com/reel/FAKE6/')
        j = self.rows('SELECT by, who, override FROM jobs')[0]
        self.assertEqual((j['by'], j['who'], json.loads(j['override'])), ('cli', 'cli', ['gap', 'same_day']))
        self.assertEqual(self.records()[NEW6]['by'], 'cli')

    def test_dry_run(self):
        lines = P.cli_post(self.root / 'new', dry=True).splitlines()
        self.assertEqual(lines[0], 'Would post: new/Output/new-short-6.mp4 (30 s)')
        self.assertTrue(lines[1].startswith('Cover: ') and lines[1].endswith('.jpg'), lines[1])
        self.assertIn('  warn: A Reel already went out today at 16:46.', lines)
        self.assertEqual((self.fake.calls, self.rows('SELECT id FROM jobs')), ([], []))

    def test_unclear_post_is_not_called_not_posted(self):
        with mock.patch.object(self.fake, 'post', unclear()):
            self.assertEqual(P.cli_post(self.root / 'new', force=True),
                             'new-short-6.mp4: not clear whether it went out. Check Instagram. Settle it in the '
                             'dashboard (Publish) before posting again.')

    def test_lesson_and_refusals(self):
        self.now = T('2026-11-05 20:05')
        self.assertEqual(P.cli_post(self.root / 'Um etwas bitten')[:36], 'Posted um-etwas-bitten-video.mp4: ht')
        self.assertEqual(P.cli_post(self.root / 'ZU + INFINITIV'), 'No waiting Reel in ZU + INFINITIV.')
        self.fake.ready = False
        self.assertEqual(P.cli_post(self.root / 'new'), 'Instagram is not set up on this PC.')


class Redact(Case):
    """S23: every secret pattern becomes '[hidden]'; item keys and ids stay; a token inside an error never
    reaches the job row, the audit table, the log or activity()."""
    def test_patterns(self):
        jwt = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl'
        for raw, want in (
                ('Authorization: Bearer abc.DEF-123', 'Authorization: [hidden]'),
                ('header bearer abc.DEF-123 end', 'header bearer [hidden] end'),
                ('Cookie: CF_Authorization=abc; other=1', 'Cookie: [hidden]'),
                (f'jwt {jwt} end', 'jwt [hidden] end'),
                ('meta EAABwzLixnjYBO1234 end', 'meta [hidden] end'),
                ('google ya29.a0AfH6SMBx-y_z end', 'google [hidden] end'),
                ('refresh 1//0gLdTXr-ABCDEF end', 'refresh [hidden] end'),
                ('claude sk-ant-api03-abc_DEF end', 'claude [hidden] end'),
                ('{"access_token": "abc123", "key": "v"}', '{"access_token": "[hidden]", "key": "[hidden]"}'),
                ("{'client_secret': 'xyz'}", "{'client_secret': '[hidden]'}"),
                ('GET /me?access_token=abc123&fields=id', 'GET /me?access_token=[hidden]&fields=id'),
                ('password=hunter2 next', 'password=[hidden] next'),
                ('x' * 40, '[hidden]'),
                ('x' * 39, 'x' * 39),
                (f'item_key={NEW6} job j0123456789 FAKE_MEDIA_5', f'item_key={NEW6} job j0123456789 FAKE_MEDIA_5'),
                (f'"item_key": "{NEW6}", "monkey=1"', f'"item_key": "{NEW6}", "monkey=1"'),
                (None, '')):
            with self.subTest(raw=raw):
                self.assertEqual(P.redact(raw), want)

    def test_a_token_in_an_error_is_hidden_everywhere(self):
        secret = 'EAAB' + 'x1y2z3' * 4
        jid, caption = self.cli_job()

        def post(item, caption, journal):
            journal.stage('sharing')
            raise RuntimeError(f'GET /v21.0/me?access_token={secret} failed: Bearer {secret}')
        self.fake.post = post
        j = P.run_post(jid, self.root, caption)
        self.assertEqual(j['state'], 'failed')
        self.assertIn('[hidden]', j['error'])
        P.log_line(IG, f'debug token={secret}')
        P.audit(WHO, 'platform.test', f'Bearer {secret}', data={'access_token': secret, 'note': secret})
        dumps = [json.dumps(j), json.dumps(self.rows('SELECT * FROM jobs')), json.dumps(self.rows('SELECT * FROM audit')),
                 (guard.WORK / 'instagram.log').read_text(encoding='utf-8'), json.dumps(P.activity())]
        for text in dumps:
            self.assertNotIn(secret, text)
            self.assertNotIn('x1y2z3x1y2', text)


class AuditEveryChange(Case):
    """S29: each state change writes its audit row, naming who and when, in the change's own transaction."""
    def test_rows(self):
        rev = P.queue(self.root, IG, now=self.now)['rev']        # loads (and migrates) the settings first
        start = len(self.kinds())
        P.queue_op(self.root, IG, 'top', rev, WHO, item_id=P.item_id(NEW7))
        P.save_settings(IG, {'auto': False}, P.settings(self.root).doc['rev'], [], WHO, root=self.root)
        self.post(NEW7)
        P.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')
        P.test_platform(IG, WHO)
        with P._Con(write=True) as con:
            con.execute("INSERT INTO auto_state (platform, failures, suspended, suspended_why) VALUES (?, 0, ?, ?) "
                        'ON CONFLICT(platform) DO UPDATE SET suspended = excluded.suspended',
                        (IG, S.iso(self.now), 'made-up'))
        P.resume_auto(IG, ['resume_auto'], WHO)
        P.reset_settings(['reset_settings'], WHO)
        rows = self.rows('SELECT who, kind, at FROM audit ORDER BY id')[start:]
        self.assertEqual([(r['kind'], r['who']) for r in rows],
                         [('queue.top', WHO), ('settings.saved', WHO), ('auto.off', WHO), ('post.requested', WHO),
                          ('post.started', WHO), ('post.done', WHO), ('breaker.open', 'dashboard'),
                          ('platform.test', WHO), ('auto.resumed', WHO), ('settings.reset', WHO)])  # G10: still cooling
        self.assertEqual({r['at'] for r in rows}, {S.iso(self.now)})


class SettingsApi(Case):
    """save_settings, reset_settings and their PublishError mapping (spec 8.3 POST /api/settings)."""
    def rev(self):
        return P.settings(self.root).doc['rev']

    def save(self, patch, ack=(), **kw):
        return P.save_settings(IG, patch, kw.pop('rev', None) or self.rev(), list(ack), WHO, root=self.root, **kw)

    def refused(self, want, patch, ack=(), **kw):
        with self.assertRaises(P.PublishError) as e:
            self.save(patch, ack, **kw)
        self.assertEqual((e.exception.status, e.exception.code), want)
        return e.exception

    def test_off_and_on(self):
        self.assertEqual(self.save({'auto': False})['message'], 'Saved. Automatic posting is off.')
        e = self.refused((428, 'confirm'), {'auto': True})
        self.assertEqual((e.message, e.extra['needs_ack']), ('Turning automatic posting on needs your confirmation.', ['auto_on']))
        self.assertEqual(e.extra['forecast']['rows'][0]['slot'], '2026-11-05')
        dry = self.save({'auto': True}, dry_run=True)
        self.assertEqual((set(dry), dry['needs_ack'], dry['settings']['auto']),
                         ({'ok', 'dry_run', 'settings', 'forecast', 'needs_ack'}, ['auto_on'], True))
        self.assertFalse(S.effective_auto(P.settings(self.root), IG)[0])            # nothing written
        body = self.save({'auto': True}, ['auto_on'])
        self.assertEqual(body['message'], 'Saved. Automatic posting is on: next Reel Thu 5 Nov after 20:00.')
        self.assertEqual(body['auto']['next'], {'at': '2026-11-05T17:00:00Z', 'slot': '2026-11-05', 'id': P.item_id(NEW6),
                                                'reason': 'scheduled', 'text': 'Next automatic Reel: Thu 5 Nov after 20:00'})
        self.assertEqual(set(body), {'ok', 'settings', 'rev', 'auto', 'forecast', 'message'})
        self.assertEqual(self.kinds()[-2:], ['settings.saved', 'auto.on'])

    def test_errors(self):
        rev = self.rev()
        e = self.refused((409, 'stale'), {'every_days': 2}, rev=rev + 5)      # a plain stop is never stale (F21)
        self.assertEqual((e.extra['rev'], e.extra['settings']['auto']), (rev, True))
        e = self.refused((422, 'read_only'), {'hard_cap_24h': 4})
        self.assertEqual(e.message, 'The 24-hour limit can only be changed on the PC.')
        e = self.refused((422, 'invalid'), {'post_before': '20:10'})
        self.assertEqual((e.message, list(e.extra['fields'])), ('Check the marked fields.', ['post_before']))
        e = self.refused((428, 'confirm'), {'min_gap_hours': 10})
        self.assertEqual((e.message, e.extra['needs_ack']), ('A gap under 20 hours needs your confirmation.', ['gap_under_20']))
        self.assertEqual(self.save({'min_gap_hours': 10}, dry_run=True)['needs_ack'], ['gap_under_20'])
        with mock.patch.object(S, 'save_settings', side_effect=KeyError('tiktok')):
            self.refused((404, 'unknown_platform'), {'auto': False})
        with mock.patch.object(S, 'save_settings', side_effect=OSError('disk full')):
            self.refused((503, 'write_failed'), {'auto': False})
        self.assertEqual(self.rev(), rev)

    def test_invalid_file_and_reset(self):
        P.settings(self.root)
        (guard.WORK / 'publishing.json').write_text('{not json', encoding='utf-8')
        self.refused((409, 'invalid_file'), {'auto': False}, rev=1)
        self.assertIn('settings_invalid', [a['id'] for a in P.attention(self.root, now=self.now)])
        with self.assertRaises(P.PublishError) as e:
            P.reset_settings([], WHO)
        self.assertEqual((e.exception.status, e.exception.code), (428, 'confirm'))
        body = P.reset_settings(['reset_settings'], WHO)
        self.assertEqual((body['message'], body['settings']['auto']), ('Settings reset to defaults. Automatic posting is off.', False))
        self.assertEqual(S.effective_auto(P.settings(self.root), IG), (False, 'off'))
        self.assertEqual(self.kinds()[-1], 'settings.reset')
        with mock.patch.object(S, 'reset_settings', side_effect=OSError('disk full')), self.assertRaises(P.PublishError) as e:
            P.reset_settings(['reset_settings'], WHO)
        self.assertEqual(e.exception.status, 503)

    def test_migration_row(self):
        P.settings(self.root)
        P.settings(self.root)
        rows = self.rows("SELECT who, text FROM audit WHERE kind = 'settings.migrated'")
        self.assertEqual(rows, [{'who': 'migration', 'text': 'Settings created: automatic posting on (there was no pause flag).'}])


class ResolveJob(Case):
    """resolve_job: check, posted and not_posted, only for interrupted or unknown jobs."""
    def interrupted(self, outcome='crash_after_container', key=NEW6):
        jid = self.crashed(outcome, key)
        P.reconcile(self.root, IG, api=False, now=self.now)
        return jid

    def refused(self, want, jid, outcome='check'):
        with self.assertRaises(P.PublishError) as e:
            P.resolve_job(self.root, jid, outcome, WHO)
        self.assertEqual((e.exception.status, e.exception.code), want)
        return e.exception

    def test_check_finds_the_post(self):
        jid = self.interrupted()
        caption, _ = P.caption_for(self.root, self.item())
        self.fake.container_states['c-6'] = 'PUBLISHED'
        self.fake.media.insert(0, {'id': 'm-60', 'at': S.iso(self.now - timedelta(minutes=5)), 'caption': caption,
                                   'permalink': 'https://www.instagram.com/reel/FAKE60/'})
        body = P.resolve_job(self.root, jid, 'check', WHO)
        self.assertEqual((body['message'], body['job']['state']), ('Checked: the Reel is on Instagram. Record saved.', 'done'))
        self.assertEqual(self.rows('SELECT who, kind FROM audit ORDER BY id DESC LIMIT 1')[0], {'who': WHO, 'kind': 'post.resolved'})
        self.refused((409, 'not_unresolved'), jid)

    def test_check_limits(self):
        jid = self.interrupted()
        self.fake.container_states['c-6'] = F.FakeError('transient')
        P.reconcile(self.root, IG, now=self.now)                # checked (and claimed) just now
        self.assertEqual(self.refused((429, 'rate_limited'), jid).extra['retry_after'], 600)
        self.now += timedelta(minutes=10)
        body = P.resolve_job(self.root, jid, 'check', WHO)
        self.assertEqual((body['message'], body['job']['state']),
                         ('Could not check right now: Instagram did not answer (HTTP 503). The PC checks again by itself '
                          'in 10 minutes.', 'interrupted'))
        self.assertEqual(body['retry_after'], 600)                            # G27
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        self.now += timedelta(minutes=10)
        self.refused((423, 'breaker'), jid)

    def test_check_unknown(self):
        jid = self.interrupted()
        self.fake.container_states['c-6'] = 'PUBLISHED'
        P.reconcile(self.root, IG, now=self.now)
        self.assertEqual(P.job(jid)['state'], 'unknown')
        self.now += timedelta(minutes=11)
        self.assertEqual(P.resolve_job(self.root, jid, 'check', WHO)['message'],
                         "Checked: Instagram says the upload was published, but the Reel is not in its list yet. Look "
                         "in the app; if it is there, choose 'It is on Instagram'. The PC also checks again by itself.")

    def test_posted_and_not_posted(self):
        a = self.add_job(state='interrupted', container_id='c-60')
        with P.PostLock('cli'), self.assertRaises(P.Busy):
            P.resolve_job(self.root, a, 'posted', WHO)
        body = P.resolve_job(self.root, a, 'posted', WHO)
        self.assertEqual((body['message'], body['job']['state']), ('Saved: the Reel counts as posted.', 'done'))
        rec = self.records()[NEW6]
        self.assertEqual((rec['id'], rec['by'], rec['job']), ('hand', 'hand', a))
        b = self.add_job(key=NEW7, state='unknown', container_id='c-70', checked=S.iso(self.now),
                         last_status='EXPIRED')
        body = P.resolve_job(self.root, b, 'not_posted', WHO)
        self.assertEqual((body['message'], body['job']['state']), ('Saved: the Reel is back in the queue.', 'failed'))
        self.assertEqual(body['job']['result'], 'Marked as not posted: the Reel is back in the queue.')
        self.assertEqual(P.queue(self.root, IG, now=self.now)['waiting'][0]['key'], NEW7)
        c = self.interrupted('crash_after_publish', WAITING[2])
        self.refused((409, 'posted'), c, 'not_posted')
        self.refused((400, 'bad_request'), c, 'maybe')
        self.refused((404, 'unknown_job'), 'jffffffffff')
        self.assertEqual(self.kinds().count('post.resolved'), 2)


class StateBodies(Case):
    """state(), next_run(), attention() and activity() on the fixtures (spec 8.3, 9.2)."""
    def test_state(self):
        st = P.state(self.root, IG, now=self.now)
        self.assertEqual(set(st), {'platform', 'label', 'noun', 'verb', 'setup_hint', 'platforms', 'set_up', 'account_url', 'now', 'settings', 'settings_rev',
                                   'settings_problems', 'presets', 'auto', 'breaker', 'last_post', 'posts_24h',
                                   'next_possible', 'counts', 'forecast', 'job', 'unresolved', 'queue', 'paused'})
        self.assertEqual((st['account_url'], st['now'], st['posts_24h'], st['next_possible'], st['job']),
                         ('https://www.instagram.com/example_account/', S.iso(self.now), 1, None, None))
        self.assertEqual(st['counts'], {'waiting': 6, 'skipped': 0, 'failed': 0, 'posted': 5, 'by_lesson': [
            {'lesson': 'new', 'waiting': 4}, {'lesson': 'Um etwas bitten', 'waiting': 1},
            {'lesson': 'Sich interessieren für', 'waiting': 1}]})
        last = self.item(fixtures.POSTED_KEYS[-1])
        self.assertEqual(st['last_post'], {'at': S.iso(fixtures.LAST_POST), 'by': 'old', 'key': last.key,
                                           'title': last.title, 'url': 'https://www.instagram.com/reel/FAKE5/'})
        fc = st['forecast']
        self.assertEqual((fc['assumes_on'], fc['runway'], fc['runway_text']), (False, '2026-11-10', 'Enough Reels until Tue 10 Nov'))
        self.assertEqual(fc['rows'][0], {'id': P.item_id(NEW6), 'at': '2026-11-05T17:00:00Z', 'slot': '2026-11-05'})
        self.assertEqual((st['auto']['effective'], st['auto']['reason'], st['auto']['next']['text']),
                         (True, 'scheduled', 'Next automatic Reel: Thu 5 Nov after 20:00'))
        self.assertEqual(st['presets'][0]['id'], next(iter(S.PRESETS)))
        home = P.state(self.root, IG, now=self.now, with_queue=False)
        self.assertNotIn('queue', home)
        self.assertNotIn('forecast', home)

    def test_next_run(self):
        e = P.next_run(self.root, now=self.now)[0]
        self.assertEqual(e['text'], f'Instagram Reel: Thu 5 Nov after 20:00 · new: {self.item().title}')
        self.assertEqual((e['effective'], e['reason'], e['at'], e['slot'], e['waiting'], e['footer']),
                         (True, 'scheduled', '2026-11-05T17:00:00Z', '2026-11-05', 6,
                          'One Reel a day after 20:00, at least 20 h apart'))
        P.save_settings(IG, {'auto': False}, P.settings(self.root).doc['rev'], [], WHO, root=self.root)
        e = P.next_run(self.root, now=self.now)[0]
        self.assertEqual((e['text'], e['footer']), ('Instagram: automatic posting OFF (6 waiting)',
                                                    'Instagram: automatic posting is off; Reels are posted from the dashboard.'))
        self.fake.ready = False
        self.assertEqual(P.next_run(self.root, now=self.now), [])

    def test_attention(self):
        self.assertEqual(P.attention(self.root, now=self.now), [])
        P.save_settings(IG, {'auto': False}, P.settings(self.root).doc['rev'], [], WHO, root=self.root)
        self.assertEqual(P.attention(self.root, now=self.now), [])     # item 12: off is a chosen setting, no card
        for key in WAITING[2:]:
            P.queue_op(self.root, IG, 'skip', P.queue(self.root, IG, now=self.now)['rev'], WHO, item_id=P.item_id(key))
        self.assertEqual(P.attention(self.root, now=self.now), [])     # 'few left' is dashboard.queue_low's now
        self.assertEqual(P.runways(self.root, now=self.now), [{'name': IG, 'label': 'Instagram', 'noun': 'Reel',
                                                              'verb': 'post', 'waiting': 2, 'runway': '2026-11-06'}])
        self.fake.ready = False                                         # not set up: left out
        self.assertEqual(P.runways(self.root, now=self.now), [])
        self.fake.ready = True
        self.fake.script = ['transient']
        jid, caption = self.cli_job()
        P.run_post(jid, self.root, caption)
        card = next(a for a in P.attention(self.root, now=self.now) if a['id'] == f'post_failed:{jid}')
        self.assertEqual((card['level'], card['title'], card['text']),
                         ('red', 'The last Instagram Reel was not posted',
                          'new-short-6.mp4 (Wed 4 Nov 22:40): Instagram did not answer (HTTP 503).'))

    def test_activity(self):
        self.post()
        posts = P.activity('posts')['items']
        self.assertEqual([i['kind'] for i in posts], ['post.done', 'post.started', 'post.requested'])
        self.assertEqual(posts[0]['url'], 'https://www.instagram.com/reel/FAKE6/')
        self.assertEqual(P.activity('settings')['items'][0]['kind'], 'settings.migrated')
        page = P.activity('all', limit=2)
        self.assertTrue(page['more'])
        rest = P.activity('all', before=page['items'][-1]['id'])
        self.assertEqual(len(page['items']) + len(rest['items']), len(self.kinds()))
        self.assertEqual(P.activity('problems'), {'items': [], 'more': False})
        for bad in ({'kind': 'dance'}, {'before': '3'}, {'limit': '9'}):
            with self.subTest(bad=bad), self.assertRaises(P.PublishError) as e:
                P.activity(**bad)
            self.assertEqual((e.exception.status, e.exception.code, e.exception.message),
                             (400, 'bad_request', 'Unknown activity filter.'))


# The 2026-11-06 09:48 upload: Google's answer as str(HttpError) (a quotaExceeded at the first request).
RAW = ('<HttpError 403 when requesting https://youtube.googleapis.com/youtube/v3/channels?part=contentDetails&mine=true'
       '&alt=json returned "The request cannot be completed because you have exceeded your <a href="/youtube/v3/'
       'getting-started#quota">quota</a>.". Details: "[{\'message\': \'The request cannot be completed because you have '
       'exceeded your <a href="/youtube/v3/getting-started#quota">quota</a>.\', \'domain\': \'youtube.quota\', '
       '\'reason\': \'quotaExceeded\'}]">')
QUOTA = "YouTube's daily limit for the PC's requests was used up."


class Plain(unittest.TestCase):
    """publish.plain(): Google's raw HttpError text as one short sentence, for the page only."""
    def test_plain(self):
        for raw in (RAW, RAW[:500], RAW[:300], P._sentence(RAW)):
            with self.subTest(n=len(raw)):
                got = P.plain(raw)
                self.assertEqual(got, QUOTA)
                for bad in ('http', '<', 'Details'):
                    self.assertNotIn(bad, got)
        for raw, want in (
                ('<HttpError 401 when requesting https://x/y returned "Request had invalid authentication '
                 'credentials.". Details: "[{\'reason\': \'authError\'}]">', "YouTube refused the PC's sign-in."),
                ('<HttpError 503 when requesting https://x/y returned "Backend Error">', 'YouTube did not answer (error 503).'),
                ('<HttpError 403 when requesting https://x/y returned "The caller does not have <b>permission</b>.". '
                 'Details: "[{\'message\': \'x\', \'domain\': \'youtube\', \'reason\': \'forbidden\'}]">',
                 'YouTube refused the request (error 403): The caller does not have permission.'),
                ('<HttpError 403 when requesting https://x/y returned "Too many". Details: "[{\'reason\': '
                 '\'rateLimitExceeded\'}]">', 'YouTube asked for a pause (too many requests).'),
                ('<HttpError 403 "x">', 'YouTube refused the request (error 403): x.')):
            with self.subTest(raw=raw[:40]):
                self.assertEqual(P.plain(raw), want)
        meta = 'Meta 400: Invalid parameter (code 100)'
        self.assertEqual(P.plain(meta), meta)
        self.assertEqual(P.plain(None), '')
        long = P.plain('word ' * 100)
        self.assertTrue(long.endswith('…') and len(long) <= 200, long)
        self.assertEqual(P.plain('x' * 300, 50), 'x' * 49 + '…')
        for x in (RAW, meta, 'word ' * 100, 'a <b>b</b> https://example.com/x c'):
            self.assertEqual(P.plain(P.plain(x)), P.plain(x))

    def test_sentence_cuts_at_a_word(self):
        got = P._sentence('word ' * 100)
        self.assertTrue(got.endswith('word…') and len(got) <= 300, got)
        self.assertEqual(P._sentence('short'), 'short.')


class PlainPages(Case):
    """Rows stored before plain() (raw Google text in publish.db) read plainly on every page body."""
    def test_old_raw_rows_read_plainly(self):
        jid = self.add_job(state='failed', kind='rate', error=RAW[:500], result=f'Not posted: {RAW[:500]}',
                           finished=S.iso(self.now - timedelta(minutes=5)))
        P.trip('meta', 'rate', RAW)                                  # stored as _sentence(): cut at 300
        P.audit('watcher', 'post.failed', f'new-short-6.mp4: not posted: {RAW}', platform=IG)
        j = P.job(jid)
        self.assertEqual((j['error'], j['result']), (QUOTA, f'Not posted: {QUOTA}'))
        texts = [json.dumps(x) for x in (j, P.state(self.root, IG, now=self.now)['breaker'],
                                         P.attention(self.root, now=self.now), P.activity()['items'])]
        for t in texts:
            self.assertNotIn('HttpError', t)
            self.assertNotIn('http', t)
        cards = {c['id'].split(':')[0]: c for c in P.attention(self.root, now=self.now)}
        self.assertNotIn('post_failed', cards)                       # a rate failure: the breaker card says it
        self.assertEqual(cards['breaker']['text'], f'{QUOTA} Posting starts again after Wed 4 Nov 23:40.')


class FailedCard(Case):
    """Home's 'The last ... was not posted' card: only while the video still waits; not for breaker kinds."""
    def test_failed_card_hides_for_rate_and_after_mark_posted(self):
        self.fake.script = ['transient']
        jid, caption = self.cli_job()
        P.run_post(jid, self.root, caption)
        self.assertIn(f'post_failed:{jid}', [c['id'] for c in P.attention(self.root, now=self.now)])
        P.queue_op(self.root, IG, 'mark_posted', P.queue(self.root, IG, now=self.now)['rev'], WHO,
                   item_id=P.item_id(NEW6), permalink='https://www.instagram.com/reel/FAKEHAND1/')
        self.assertNotIn(f'post_failed:{jid}', [c['id'] for c in P.attention(self.root, now=self.now)])
        self.fake.script = ['rate']
        jid, caption = self.cli_job(NEW7, override=('gap', 'same_day', 'hard_cap'))
        self.assertEqual(P.run_post(jid, self.root, caption)['kind'], 'rate')
        cards = {c['id']: c for c in P.attention(self.root, now=self.now)}
        self.assertNotIn(f'post_failed:{jid}', cards)
        b = cards['breaker:meta']
        self.assertEqual((b['level'], b['who'], b['title'], b['action']), ('yellow', 'anyone', 'Instagram is paused', None))
        self.assertTrue(b['text'].endswith('Posting starts again after Wed 4 Nov 23:40.'), b['text'])
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        b = {c['id']: c for c in P.attention(self.root, now=self.now)}['breaker:meta']
        self.assertEqual((b['level'], b['who'], b['title'], b['text']),
                         ('red', 'pc', 'Instagram is stopped', 'Instagram refused the key (code 190). Test the connection '
                          'in Publish.'))

    def test_activity_rows_carry_the_platform_label(self):
        self.post()
        P.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')
        rows = P.activity()['items']
        b = next(r for r in rows if r['kind'] == 'breaker.open')
        self.assertEqual((b['platform'], b['label']), (None, 'Instagram'))
        self.assertEqual(b['text'], 'Posting paused (until 23:40): Instagram says: too many calls (code 4).')
        self.assertEqual(next(r for r in rows if r['kind'] == 'post.done')['label'], 'Instagram')
        self.assertIsNone(next(r for r in rows if r['kind'] == 'settings.migrated')['label'])


class Isolation(Case):
    """The database, the lock, the logs, the settings and the fake records all live in the test's WORK."""
    def test_paths(self):
        self.post()
        paths = [P._db(), P._lockfile(), P.WORK / 'instagram.log', S._path(), self.fake.records]
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(Path(path).resolve().is_relative_to(guard.WORK.resolve()), path)
                self.assertFalse(Path(path).resolve().is_relative_to(guard.REAL_WORK), path)
        self.assertTrue(P._db().exists() and (P.WORK / 'instagram.log').exists())


@unittest.skipUnless(os.environ.get('REAL_DRIVE_ROOT'), 'set REAL_DRIVE_ROOT for the read-only real-data dry run')
class RealData(unittest.TestCase):
    """spec 11, the real-data dry run (run by hand only): the real Instagram platform on the real Drive root at
    fixtures.NOW, with the network blocked. Read-only: every file under the root keeps its size and mtime."""
    def setUp(self):
        guard.reset()                                           # also P.set_registry(None): the real platform
        self.root = Path(os.environ['REAL_DRIVE_ROOT'])

    def snapshot(self) -> dict:
        return {str(f): (f.stat().st_size, f.stat().st_mtime_ns) for f in self.root.rglob('*') if f.is_file()}

    def test_order_and_forecast(self):
        before = self.snapshot()
        q = P.queue(self.root, IG, now=fixtures.NOW)
        self.assertEqual([(i['lesson'], i['file']) for i in q['waiting']],
                         [(k.split('/')[0], k.split('/')[-1]) for k in WAITING])
        self.assertEqual((len(q['waiting']), q['posted_total']), (6, 5))
        self.assertFalse(any(i['lesson'] == 'ZU + INFINITIV' for i in q['waiting'] + q['posted'] + q['skipped']))
        self.assertEqual(P.state(self.root, IG, now=fixtures.NOW)['forecast']['runway'], '2026-11-10')
        self.assertEqual(self.snapshot(), before)


# ---------- review fixes (fixer A1): posting safety ----------

LINK60 = 'https://www.instagram.com/reel/FAKE60/'


class PostingSafety(Case):
    """F1, F2, F3, G1, G8, G9, G10, F20: a post that may be live is never sent again, a job that must stop sends
    nothing more, and reading a page never takes the posting lock."""
    def setUp(self):
        super().setUp()
        self.sent = []

    def posting(self, before=None, cid='c-80'):
        """A FakePlatform.post that commits upload cid (FINISHED), runs before(journal), reaches 'publishing',
        then 'calls media_publish' (self.sent) and gets no clear answer (HTTP 503)."""
        def post(item, caption, journal):
            journal.stage('sharing')
            journal.container(cid)
            self.fake.container_states[cid] = 'FINISHED'
            if before:
                before(journal)
            journal.stage('processing')
            journal.stage('publishing')
            self.sent.append(item.key)
            raise F.FakeError('transient')
        return post

    def test_F1_publish_error_after_the_upload_ends_unknown(self):
        caption = P.caption_for(self.root, self.item())[0]
        with mock.patch.object(self.fake, 'post', self.posting()):
            j = self.post()
        self.assertEqual(j['state'], 'unknown')                  # never 'failed': it may be live
        self.assertEqual(self.preview(NEW7)['blocked_by'], 'unresolved')
        self.fake.container_states['c-80'] = 'PUBLISHED'           # Meta published it a moment later
        self.fake.media.insert(0, {'id': 'm-80', 'at': S.iso(self.now), 'caption': caption,
                                   'permalink': 'https://www.instagram.com/reel/FAKE80/'})
        self.auto_on()
        P.auto_step(self.root, now=self.now)                      # checks the job by itself
        self.assertEqual(P.job(j['id'])['state'], 'done')
        self.assertEqual(self.records()[NEW6]['id'], 'm-80')
        self.assertEqual(self.sent, [NEW6])
        self.assertNotIn(NEW6, [c[1][0] for c in self.posts()])

    def test_F1_fake_publish_error_outcomes(self):
        for outcome, want in (('publish_error', 'unknown'), ('publish_error_live', 'unknown')):
            with self.subTest(outcome):
                self.fake.script = [outcome]
                key = NEW6 if outcome == 'publish_error' else NEW7
                jid, caption = self.cli_job(key, override=('gap', 'same_day'))
                self.assertEqual(P.run_post(jid, self.root, caption)['state'], want)
                with P._Con(write=True) as con:                  # out of the way for the next one
                    con.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (jid,))

    def test_F2_lock_taken_over_mid_post_sends_nothing_more(self):
        def take_over(journal):
            path = P._lockfile()
            path.write_text(json.dumps({**json.loads(path.read_bytes()), 'token': 'f' * 32}), encoding='utf-8')
        with mock.patch.object(self.fake, 'post', self.posting(take_over)):
            j = self.post()
        self.assertEqual((self.sent, j['state']), ([], 'stale'))
        self.assertIn('Nothing was posted', j['error'])

    def test_F2_job_settled_meanwhile_sends_nothing_more(self):
        def settle(journal):
            with P._Con(write=True) as con:
                con.execute("UPDATE jobs SET state = 'interrupted' WHERE id = ?", (journal.job_id,))
        with mock.patch.object(self.fake, 'post', self.posting(settle)):
            j = self.post()
        self.assertEqual((self.sent, j['state']), ([], 'interrupted'))  # the other process's answer is kept

    def test_F3_automatic_posting_turned_off_mid_post_sends_nothing(self):
        self.auto_on()
        def turn_off(journal):
            P.save_settings(IG, {'auto': False}, P.settings(self.root).doc['rev'], [], WHO, root=self.root)
        with mock.patch.object(self.fake, 'post', self.posting(turn_off)):
            P.auto_step(self.root, now=self.now)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.rows('SELECT state FROM jobs'), [{'state': 'stale'}])

    def refused(self, jid, outcome, code):
        with self.assertRaises(P.PublishError) as e:
            P.resolve_job(self.root, jid, outcome, WHO)
        self.assertEqual((e.exception.status, e.exception.code), (409, code))

    def test_G1_not_posted_is_refused_while_it_may_be_live(self):
        now = S.iso(self.now)
        self.refused(self.add_job(state='unknown', container_id='c-60', checked=now, last_status='PUBLISHED'),
                     'not_posted', 'published')
        self.refused(self.add_job(key=NEW7, state='interrupted', container_id='c-70'), 'not_posted', 'check_first')
        c = self.add_job(key=WAITING[2], state='unknown', container_id='c-71', checked=now, last_status='FINISHED',
                         heartbeat=now)
        self.refused(c, 'not_posted', 'too_early')
        self.now += timedelta(seconds=P.SETTLE_WAIT)
        self.assertEqual(P.resolve_job(self.root, c, 'not_posted', WHO)['job']['state'], 'failed')

    def test_G1_posted_is_refused_when_nothing_was_sent(self):
        self.refused(self.add_job(state='interrupted'), 'posted', 'not_sent')
        self.assertNotIn(NEW6, self.records())

    def test_G8_endings_are_compare_and_set(self):
        jid = self.add_job(state='interrupted', container_id='c-60')
        r = P._job_row(jid)
        with P._Con(write=True) as con:                          # settled by another process meanwhile
            con.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (jid,))
        self.assertFalse(P._end(self.fake, r, 'failed', kind='local', error='made-up', expect=('interrupted',)))
        self.assertEqual(P.job(jid)['state'], 'done')

    def test_G8_hand_answer_never_overwrites_a_real_record(self):
        jid = self.add_job(state='interrupted', container_id='c-60')
        self.fake.write_record(self.item(), {'id': 'm-60', 'permalink': LINK60, 'posted_utc': S.iso(self.now),
                                             'by': 'cli'})
        self.assertEqual(P.resolve_job(self.root, jid, 'posted', WHO)['job']['state'], 'done')
        self.assertEqual((self.records()[NEW6]['id'], self.records()[NEW6]['permalink']), ('m-60', LINK60))

    def test_G9_reading_the_page_takes_no_lock_and_writes_nothing(self):
        self.fake.script = ['record_fails']
        j = self.post()
        taken, enter = [], P.PostLock.__enter__
        with mock.patch.object(P.PostLock, '__enter__', lambda s: taken.append(s.owner) or enter(s)):
            P.state(self.root, IG, now=self.now)
            self.preview(NEW7)
        self.assertEqual(taken, [])
        self.assertTrue(P.job(j['id'])['record_pending'])

    def test_G9_a_lock_without_a_job_is_not_a_post_in_progress(self):
        with P.PostLock('dashboard'):
            self.assertNotEqual(self.preview()['blocked_by'], 'busy')
        with P.PostLock('dashboard', job=self.add_job(key=NEW7, state='running')):
            self.assertEqual(self.preview()['blocked_by'], 'busy')

    def test_G9_pending_records_get_one_try_in_the_background(self):
        self.fake.script = ['record_fails']
        self.post()
        tries = []
        with mock.patch.object(self.fake, 'write_record', lambda item, rec, retry=True: tries.append(retry)):
            P.reconcile(self.root, IG, now=self.now)
        self.assertEqual(tries, [False])

    def test_G10_a_working_test_keeps_the_rate_pause(self):
        b = P.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')
        body = P.test_platform(IG, WHO)
        self.assertEqual((body['breaker']['state'], body['breaker']['until']), ('cooling', b['until']))
        self.assertTrue(body['message'].startswith('Instagram answers, but it asked for a pause'), body['message'])
        self.assertNotIn('breaker.closed', self.kinds())
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        self.now += timedelta(seconds=P.TEST_EVERY)
        self.assertEqual(P.test_platform(IG, WHO)['breaker']['state'], 'closed')   # 'stopped' ends with a test

    def test_F20_lock_file_busy_is_no_failure(self):
        real, tries = os.replace, []
        def replace(src, dst):
            if Path(dst) == P._lockfile():
                tries.append(Path(src).name)
                raise PermissionError(13, 'Access is denied (made up)')
            return real(src, dst)
        self.auto_on()
        with mock.patch.object(P.os, 'replace', replace), contextlib.redirect_stdout(io.StringIO()):
            P.auto_step(self.root, now=self.now)
        self.assertEqual(len(tries), 5)                          # tried again, then posting went on
        self.assertEqual(self.rows('SELECT state FROM jobs'), [{'state': 'done'}])
        self.assertEqual(list(P._lockfile().parent.glob(f'{P._lockfile().name}.tmp-*')), [])


# ---------- review fixes (fixer A1): the same video across renames ----------

NEWER = 'new-renamed'


def moved(key: str) -> str:
    """key after the lesson folder 'new' was renamed to NEWER."""
    return NEWER + key[len('new'):]


class SameVideo(Case):
    """G3, G4, G5, G7: 'the same video' is its file name and size (Item.sig), not its path."""
    def setUp(self):
        super().setUp()
        for lesson in (NEWER, 'new copy'):                       # make_root() rebuilds in place: no leftovers
            self.addCleanup(shutil.rmtree, self.root / lesson, True)

    def q(self) -> dict:
        return P.queue(self.root, IG, now=self.now)

    def op(self, op, key, **kw):
        return P.queue_op(self.root, IG, op, self.q()['rev'], WHO, item_id=P.item_id(key), **kw)

    def keys(self, part) -> list:
        return [i['key'] for i in self.q()[part]]

    def copy(self, key=NEW6, lesson='new copy', extra=b'') -> str:
        """The item's video copied into another lesson (extra bytes: a re-render); returns the copy's key."""
        d = dict(next(i for i in self.fake._items if i['key'] == key))
        out = self.root / lesson / 'Output'
        out.mkdir(parents=True, exist_ok=True)
        f = out / d['file'].name
        f.write_bytes(d['file'].read_bytes() + extra)
        d.update(key=f'{lesson}/Output/{f.name}', lesson=lesson, out=out, file=f, cover=None)
        self.fake._items.append(d)
        return d['key']

    def test_G3_renamed_lesson_keeps_skip_caption_order_and_failed(self):
        self.op('skip', NEW7)
        self.op('caption', WAITING[2], caption='Eigene Caption #almanca')
        self.op('top', WAITING[3])
        with P._Con(write=True) as con:
            P._set_queue(con, IG, NEW6, self.item().sig, failed='made-up failure', failed_at=S.iso(self.now))
        self.fake.rename('new', NEWER)
        self.assertEqual(self.keys('skipped'), [moved(NEW7)])
        self.assertEqual(self.keys('failed'), [moved(NEW6)])
        self.assertEqual(self.keys('waiting')[0], moved(WAITING[3]))
        self.assertEqual(P.caption_for(self.root, self.item(moved(WAITING[2]))), ('Eigene Caption #almanca', True))
        self.assertIn(moved('new/Output/new-short-5.mp4'), self.keys('posted'))      # the records moved too
        self.assertEqual(self.kinds().count('queue.moved'), 4)
        self.q()
        self.assertEqual(self.kinds().count('queue.moved'), 4)                      # once

    def test_G3_renamed_lesson_keeps_its_unresolved_job(self):
        self.add_job(key=NEW7, state='interrupted', container_id='c-99', sig=self.item(NEW7).sig)
        self.fake.rename('new', NEWER)
        self.assertEqual(self.preview(moved(NEW7))['blocked_by'], 'checking')

    def test_G4_pending_record_is_written_after_a_rename(self):
        self.fake.script = ['record_fails']
        j = self.post()
        self.fake.rename('new', NEWER)
        self.assertIn(moved(NEW6), self.keys('posted'))
        self.assertEqual(P.reconcile(self.root, IG, now=self.now),
                         ['Instagram: new-short-6.mp4: the Drive record is saved now.'])
        self.assertEqual(self.records()[moved(NEW6)]['id'], 'm-6')
        self.assertFalse(P.job(j['id'])['record_pending'])

    def test_G5_a_new_video_at_an_old_path_starts_fresh(self):
        self.op('skip', NEW7)
        self.op('caption', NEW7, caption='Eigene Caption #almanca')
        self.fake.script = ['record_fails']
        self.post()                                              # NEW6 is live, its record still pending
        for key in (NEW6, NEW7):
            f = self.item(key).file
            f.write_bytes(f.read_bytes() + b' a new render')
        self.assertEqual(self.keys('waiting')[:2], [NEW6, NEW7])
        self.assertFalse(P.caption_for(self.root, self.item(NEW7))[1])
        self.assertEqual(P.reconcile(self.root, IG, now=self.now),                    # not written onto the new one
                         ['Instagram: new-short-6.mp4: the Drive record waits (the video is not in the list).'])
        self.assertNotIn(NEW6, self.records())

    def test_G7_a_copy_of_a_posted_short_is_not_posted_again(self):
        self.post()
        twin = self.copy()
        self.assertIn(twin, self.keys('posted'))
        self.assertNotIn(twin, self.keys('waiting'))

    def test_G7_same_caption_as_a_live_post(self):
        self.post()
        twin = self.copy(extra=b' a new render')
        self.assertIn('duplicate', [c['code'] for c in self.preview(twin)['checks'] if c['level'] == 'warn'])
        self.op('top', twin)
        self.auto_on()
        P.auto_step(self.root, now=self.now)
        self.assertIn(twin, self.keys('failed'))
        self.assertNotIn(twin, [c[1][0] for c in self.posts()])


class FixA1Misc(Case):
    """F7, G15, F53, F27."""
    def test_F7_drive_away_for_a_moment_marks_nothing_failed(self):
        self.auto_on()
        f = self.item().file
        data = f.read_bytes()
        f.unlink()
        P.auto_step(self.root, now=self.now)
        self.assertEqual((P.queue(self.root, IG, now=self.now)['failed'], self.posts()), ([], []))
        f.write_bytes(data)                                      # Drive serves it again
        self.now += timedelta(minutes=10)
        P.auto_step(self.root, now=self.now)
        self.assertEqual([c[1][0] for c in self.posts()], [NEW6])

    def test_G15_hand_record_gets_the_real_id_by_caption(self):
        caption = P.caption_for(self.root, self.item())[0]
        jid = self.add_job(state='interrupted', container_id='c-60', caption_head=P._head(caption))
        P.resolve_job(self.root, jid, 'posted', WHO)
        self.assertEqual((self.records()[NEW6]['id'], self.records()[NEW6]['permalink']), ('hand', ''))
        media = [{'id': 'm-60', 'at': S.iso(self.now - timedelta(minutes=19)), 'permalink': LINK60, 'caption': caption}]
        self.assertEqual(P.note_media(self.root, IG, media), 1)
        self.assertEqual((self.records()[NEW6]['id'], self.records()[NEW6]['permalink']), ('m-60', LINK60))
        self.assertEqual(S.posts_in_24h(P.history(self.root, IG), self.now), 2)    # counted once, not twice

    def test_F53_unchanged_custom_caption_is_not_saved_again(self):
        rev = P.queue(self.root, IG, now=self.now)['rev']
        P.queue_op(self.root, IG, 'caption', rev, WHO, item_id=P.item_id(NEW6), caption='Eigene Caption #almanca')
        self.assertEqual(self.post(caption='Eigene Caption #almanca', save=True)['state'], 'done')
        self.assertEqual(self.kinds().count('queue.caption'), 1)

    def test_F27_long_file_names_survive_and_tokens_do_not(self):
        name = 'sich-interessieren-fuer-etwas-almanca-dersi-a1-a2-short-3.mp4'
        self.assertEqual(P.redact(f'{name}: posted'), f'{name}: posted')
        token = 'FAKEtoken' + 'aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5'
        self.assertEqual(P.redact(f'key {token} refused'), 'key [hidden] refused')


class FixA2(Case):
    """Fix round A2: timing, forecast and status texts (F10, F33, F35, F36, F37, G14, G27, G29), the CLI (F50),
    the key warning (F52), Drive scans and the probe cache (F32, G17, G23, G24) and the 502 retry_after."""

    def run_at(self, text):
        self.now = T(text)
        return P.auto_step(self.root, now=self.now)

    def save(self, patch, **kw):
        return P.save_settings(IG, patch, P.settings(self.root).doc['rev'], [], WHO, root=self.root, **kw)

    # ----- F10: a used-up slot -----
    def used_slot(self, script, tries):
        self.fake.script = list(script)
        for t in tries:
            self.run_at(t)
        self.assertEqual(len(self.posts()), len(tries))
        self.now = T('2026-11-05 20:50')
        st = P.state(self.root, IG, now=self.now)
        self.assertEqual(st['auto']['next']['text'], 'Next automatic Reel: Fri 6 Nov after 20:00')
        self.assertEqual(st['queue']['waiting'][0]['eta'], '2026-11-06T17:00:00Z')
        self.assertEqual(st['forecast']['rows'][0]['slot'], '2026-11-06')
        self.assertTrue(P.next_run(self.root, now=self.now)[0]['text'].startswith('Instagram Reel: Fri 6 Nov after 20:00'))
        for m in range(0, 190, 10):                                    # 20:50 .. 23:50: nothing goes out
            self.run_at(f'2026-11-05 {20 + (50 + m) // 60:02d}:{(50 + m) % 60:02d}')
        self.assertEqual(len(self.posts()), len(tries))
        self.run_at('2026-11-06 20:00')
        self.assertEqual(len(self.posts()), len(tries) + 1)                 # the time shown was the real one

    def test_F10_next_after_a_content_failure(self):
        self.used_slot(['content'], ['2026-11-05 20:05'])

    def test_F10_next_after_two_transient_failures(self):
        self.used_slot(['transient', 'transient'], ['2026-11-05 20:05', '2026-11-05 20:40'])

    def test_F10_next_waits_for_the_retry(self):
        self.fake.script = ['transient']
        self.run_at('2026-11-05 20:05')
        self.now = T('2026-11-05 20:10')
        self.assertEqual(P.state(self.root, IG, now=self.now)['auto']['next']['text'],
                         'Next automatic Reel: Thu 5 Nov after 20:35')

    # ----- F33: the shortest window fits the retry -----
    def test_F33_retry_fits_the_shortest_window(self):
        for m in (0, 1, 5, 9):
            with self.subTest(m=m):
                self.setUp()
                self.save({'post_after': f'20:0{m}', 'post_before': f'20:{40 + m}'})
                self.fake.script = ['transient', 'ok']
                t = T('2026-11-05 19:50') + timedelta(seconds=2)
                while t < T('2026-11-05 21:10'):
                    self.now = t
                    P.auto_step(self.root, now=t)
                    t += timedelta(minutes=10)
                times = [S.local(_t) for _t in (P._time(j['created']) for j in
                                                self.rows("SELECT created FROM jobs WHERE by = 'auto' ORDER BY created"))]
                self.assertEqual(len(times), 2)
                self.assertGreaterEqual((times[1] - times[0]).total_seconds(), P.RETRY_AFTER)
                self.assertLessEqual(f'{times[1]:%H:%M}', f'20:{40 + m}')
                self.assertEqual(self.rows('SELECT state FROM jobs ORDER BY created')[-1]['state'], 'done')

    # ----- F35: the forecast says when the watcher will not post -----
    def test_F35_assumes_on_while_posting_is_held(self):
        self.assertIs(P.state(self.root, IG, now=self.now)['forecast']['assumes_on'], False)
        with P._Con(write=True) as con:
            con.execute("INSERT INTO auto_state (platform, failures, suspended, suspended_why) VALUES (?, 3, ?, 'x')",
                        (IG, S.iso(self.now)))
        st = P.state(self.root, IG, now=self.now)
        self.assertEqual((st['auto']['reason'], st['forecast']['assumes_on']), ('suspended', True))
        P.resume_auto(IG, ['resume_auto'], WHO, root=self.root)
        self.assertIs(P.state(self.root, IG, now=self.now)['forecast']['assumes_on'], False)
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        st = P.state(self.root, IG, now=self.now)
        self.assertEqual((st['auto']['reason'], st['forecast']['assumes_on']), ('breaker', True))

    # ----- F36, F37: one horizon, and the runway over the whole queue -----
    def test_F36_status_and_runway_agree_at_the_horizon(self):
        self.now = T('2026-11-05 06:10')
        self.save({'start_date': '2026-11-19'})
        st = P.state(self.root, IG, now=self.now)
        self.assertEqual(st['auto']['next']['text'], 'Next automatic Reel: Thu 19 Nov after 20:00')
        self.assertEqual(st['forecast']['rows'][0]['slot'], '2026-11-19')
        self.assertEqual(st['queue']['waiting'][0]['eta'], st['auto']['next']['at'])
        self.assertTrue(self.save({}, dry_run=True)['forecast']['rows'])
        self.save({'start_date': '2026-11-21'})
        st = P.state(self.root, IG, now=self.now)
        self.assertEqual(st['auto']['next']['text'], 'No slot in the next 15 days.')
        self.assertEqual((st['forecast']['runway_text'], st['forecast']['rows']), ('No slot in the next 15 days.', []))

    def test_F37_runway_counts_the_whole_queue(self):
        self.now = T('2026-11-05 06:30')
        fc = self.save({'every_days': 3}, dry_run=True)['forecast']
        self.assertEqual((fc['runway'], fc['runway_text']), ('2026-11-22', 'Enough Reels until Sun 22 Nov'))
        self.assertEqual(([r['slot'] for r in fc['rows']], fc['more']),
                         (['2026-11-07', '2026-11-10', '2026-11-13', '2026-11-16', '2026-11-19'], 1))
        fc = P.state(self.root, IG, now=self.now)['forecast']
        self.assertEqual((fc['runway_text'], fc['more']), ('Enough Reels until Tue 10 Nov', 0))
        for name in ('Lesson A', 'Lesson B'):
            self.addCleanup(shutil.rmtree, self.root / name, True)     # the fixture root is shared by the tests
            fixtures.write_lesson(self.root, name, shorts=9)
        self.fake = fixtures.fake_platform(self.root)
        P.set_registry({IG: self.fake})
        st = P.state(self.root, IG, now=self.now)
        n, fc = st['counts']['waiting'], st['forecast']
        self.assertGreaterEqual(n, 24)
        self.assertEqual(fc['runway'], (datetime(2026, 11, 5) + timedelta(days=n - 1)).date().isoformat())
        self.assertEqual((len(fc['rows']), fc['more']), (16, n - 16))           # 5 .. 20 Nov, and how many more

    # ----- G14: STATUS.txt names a block whatever the setting -----
    def test_G14_next_run_names_blocks_with_auto_off(self):
        self.save({'auto': False})
        self.add_job(state='interrupted', container_id='c-60')
        e = P.next_run(self.root, now=self.now)[0]
        self.assertEqual((e['text'], e['reason']), ('Instagram: an interrupted post needs a check (5 waiting)', 'off'))
        self.assertNotIn('posted from the dashboard', e['footer'])
        with P._Con(write=True) as con:
            con.execute("UPDATE jobs SET state = 'failed'")
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        e = P.next_run(self.root, now=self.now)[0]
        self.assertEqual(e['text'], 'Instagram: posting stopped: Instagram refused the key (code 190).')
        self.assertNotIn('posted from the dashboard', e['footer'])

    # ----- G27: an upload status the PC does not know -----
    def test_G27_check_unexpected_status(self):
        jid = self.crashed('crash_after_container')
        P.reconcile(self.root, IG, api=False, now=self.now)
        self.fake.container_states['c-6'] = 'SCHEDULED'
        body = P.resolve_job(self.root, jid, 'check', WHO)
        self.assertIn("'SCHEDULED'", body['message'])
        self.assertNotIn('a few minutes', body['message'])
        self.assertEqual((body['job']['state'], body['retry_after']), ('interrupted', 600))
        self.assertIn("'SCHEDULED'", body['job']['result'])

    # ----- G29: resume right after a restart -----
    def test_G29_resume_after_a_restart(self):
        with P._Con(write=True) as con:
            con.execute("INSERT INTO auto_state (platform, failures, suspended, suspended_why) VALUES (?, 3, ?, 'x')",
                        (IG, S.iso(self.now)))
        with mock.patch.object(P, '_ROOT', None):
            body = P.resume_auto(IG, ['resume_auto'], WHO, root=self.root)
        self.assertEqual((body['auto']['reason'], body['auto']['next']['at']), ('scheduled', '2026-11-05T17:00:00Z'))

    # ----- F50: the CLI's lesson name in any case -----
    def test_F50_lesson_name_any_case(self):
        self.now = T('2026-11-05 20:05')
        self.assertEqual(P.cli_post(self.root / 'NEW', dry=True).splitlines()[0],
                         'Would post: new/Output/new-short-6.mp4 (30 s)')
        self.assertEqual(P.cli_post(self.root / 'um etwas bitten', dry=True).splitlines()[0][:40],
                         'Would post: Um etwas bitten/Output/um-et')
        self.assertEqual(P.cli_post(self.root / 'nothing here'), 'No waiting Reel in nothing here.')
        self.assertTrue(P.cli_post(self.root / 'NEW').startswith('Posted new-short-6.mp4: '))
        self.assertEqual(len(self.posts()), 1)

    # ----- F52: the key warning reaches the watcher log before an automatic post -----
    def test_F52_key_warning_before_an_automatic_post(self):
        self.assertIsNone(P.Platform().key_warning(self.now))
        self.fake.key_warning = lambda now: 'WARNING: Meta access ends 2026-11-10: run it again.'
        lines = self.run_at('2026-11-05 20:05')
        self.assertIn('Instagram: WARNING: Meta access ends 2026-11-10: run it again.', lines)
        self.assertEqual(len(self.posts()), 1)
        self.fake.key_warning = mock.Mock(side_effect=RuntimeError('broken'))
        self.run_at('2026-11-06 20:05')
        self.assertEqual(len(self.posts()), 2)                           # a broken check never stops a post

    # ----- F32: a record file that cannot be read -----
    def test_F32_unreadable_record_is_a_problem_on_the_page(self):
        self.fake.history_error = 'new/Output/extras/instagram-posted.json is locked'
        st = P.state(self.root, IG, now=self.now)
        self.assertIn({'lesson': 'new', 'error': 'OSError: new/Output/extras/instagram-posted.json is locked'},
                      st['queue']['problems'])
        self.assertTrue(P.next_run(self.root, now=self.now)[0]['text'].startswith('Instagram Reel: '))
        self.assertIsInstance(P.attention(self.root, now=self.now), list)
        with self.assertRaises(OSError):                                 # posting never runs on half the posts
            P.history(self.root, IG)
        line = self.run_at('2026-11-05 20:05')
        self.assertEqual((self.posts(), len(line)), ([], 1))
        self.assertIn('automatic step failed', line[0])

    # ----- G17: one Drive scan per request, none outside the slot -----
    def test_G17_one_scan_per_request(self):
        scans = mock.Mock(wraps=self.fake.candidates)
        self.fake.candidates = scans
        v = P.views(self.root, self.now)
        P.attention(self.root, self.now, views=v)
        P.next_run(self.root, self.now, views=v)
        P.queue(self.root, IG, self.now, v=v[IG])
        self.assertEqual(scans.call_count, 1)
        scans.reset_mock()
        self.post()
        self.assertEqual(scans.call_count, 3)            # preview, start_post and run_post (find() is gone)
        scans.reset_mock()
        for t in ('2026-11-05 12:00', '2026-11-05 19:50', '2026-11-06 01:00'):
            self.run_at(t)
        self.assertEqual(scans.call_count, 0)            # outside the window: no scan at all

    def test_G17_probes_from_memory(self):
        item = self.item()
        P.probe_cached(item.file)
        with mock.patch.object(P, 'connect', side_effect=AssertionError('a database read')):
            for _ in range(3):
                self.assertEqual(round(P.probe_cached(item.file)['duration']), 30)

    # ----- G23: the WAL stays; v1's probe cache is used -----
    def test_G23_wal_is_kept_between_connections(self):
        P.audit(WHO, 'test', 'one')
        P.audit(WHO, 'test', 'two')
        self.assertTrue((guard.WORK / 'publish.db-wal').exists())
        P.set_registry({IG: self.fake})                  # the idle connection is closed: WORK can be emptied
        self.assertIsNone(P._keeper)

    def test_G23_v1_probe_cache_is_reused(self):
        f = guard.TMP / 'g23' / 'old-short-1.mp4'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b'x' * 1234)
        (guard.WORK / 'dashboard-probes.json').write_text(json.dumps({
            'old-short-1.mp4:1234': {'seconds': 41, 'vertical': True}, 'bad:1': {'seconds': 'x'}}), encoding='utf-8')
        with mock.patch.object(P.tools, 'probe', side_effect=AssertionError('probed again')):
            self.assertEqual(P.probe_cached(f), {'duration': 41.0, 'width': 1080, 'height': 1920})
        with P._Con() as con:
            self.assertEqual(con.execute("SELECT duration FROM probes WHERE key = 'old-short-1.mp4:1234'").fetchone()[0], 41.0)

    # ----- G24: one probe per file at a time; failures are not retried at once -----
    def test_G24_one_probe_per_file(self):
        f = guard.TMP / 'g24' / 'some-short-1.mp4'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b'x' * 999)
        n, go = [], threading.Event()

        def probe(path):
            n.append(path)
            go.wait(5)
            return {'duration': 30.0, 'width': 1080, 'height': 1920}
        with mock.patch.object(P.tools, 'probe', probe):
            out = []
            threads = [threading.Thread(target=lambda: out.append(P.probe_cached(f))) for _ in range(5)]
            for t in threads:
                t.start()
            time_out = threading.Timer(0.3, go.set)
            time_out.start()
            for t in threads:
                t.join(10)
        self.assertEqual((len(n), len(out)), (1, 5))
        g = guard.TMP / 'g24' / 'broken-short-1.mp4'
        g.write_bytes(b'y' * 10)
        bad = mock.Mock(side_effect=RuntimeError('ffprobe.exe failed (1)'))
        with mock.patch.object(P.tools, 'probe', bad):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'ffprobe'):
                    P.probe_cached(g)
            self.assertEqual(bad.call_count, 1)
            with mock.patch.object(P, 'PROBE_RETRY', 0):
                with self.assertRaises(RuntimeError):
                    P.probe_cached(g)
            self.assertEqual(bad.call_count, 2)

    # ----- request C6: a failed test says when the next one is allowed -----
    def test_C6_failed_test_has_retry_after(self):
        self.fake.test_error = 'transient'
        with self.assertRaises(P.PublishError) as e:
            P.test_platform(IG, WHO)
        self.assertEqual((e.exception.status, e.exception.code, e.exception.extra['retry_after']), (502, 'test_failed', 600))

    # ----- F22: the fixtures' write guard also knows the \\?\ spelling of a path -----
    def test_F22_fixtures_refuse_long_path_spellings(self):
        for bad in (r'\\?\G:\My Drive\SINEM-YouTube', '\\\\?\\' + str(guard.REAL_WORK),
                    '\\\\.\\' + str(guard.REAL_WORK / 'x')):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                fixtures._safe(bad)
        self.assertEqual(fixtures._safe('\\\\?\\' + str(guard.TMP / 'ok')), guard.TMP / 'ok')


class SameVideoAgain(SameVideo):
    """Re-review R1, R2: adoption after a rename goes by video, and two views at once keep the row."""
    def test_R1_new_folder_with_the_old_name(self):
        self.op('skip', NEW6)
        self.op('caption', NEW7, caption='Eigene Caption #almanca')
        self.fake.rename('new', NEWER)
        (self.root / 'new').mkdir()                    # the next recording goes into a fresh 'new' folder
        self.addCleanup(shutil.rmtree, self.root / 'new', True)
        self.assertEqual(self.keys('skipped'), [moved(NEW6)])
        self.assertEqual(P.caption_for(self.root, self.item(moved(NEW7))), ('Eigene Caption #almanca', True))

    def test_R2_two_views_adopt_at_once(self):
        self.op('skip', NEW6)
        self.fake.rename('new', NEWER)
        real = P._View._adopt
        with mock.patch.object(P._View, '_adopt', lambda self: False):     # both read the rows before either moves
            a = P._View(self.root, self.fake, self.now, strict=False)
            b = P._View(self.root, self.fake, self.now, strict=False)
        self.assertTrue(real(a))
        self.assertFalse(real(b))
        self.assertEqual(self.keys('skipped'), [moved(NEW6)])
        self.assertEqual(self.kinds().count('queue.moved'), 1)
