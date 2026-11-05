"""easy.watch() with its publishing step and heartbeat, and overview.py's NEXT and footer lines (spec 7.6; S31 of
spec 10). Offline: lessons from the fixture root, posts to FakePlatform (or the real InstagramPlatform, which makes
no Meta call here), and a render that fails on purpose."""
from . import fixtures, guard

import contextlib, io, json, os, shutil, sys, threading, time, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import easy
import instagram as IG
import overview
import publish as P
import schedule as S
import tools

LESSONS = {'INPUT', 'Sich interessieren für', 'Um etwas bitten', 'ZU + INFINITIV', 'new'}   # the root's folders
FAILURE = 'RuntimeError: made-up render failure'


def T(text: str) -> datetime:
    """'2026-11-05 20:05' Istanbul as aware UTC."""
    return datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=fixtures.IST).astimezone(timezone.utc)


def lessons(root) -> list[str]:
    """The root's lesson folders in the watcher's order: sorted paths (case-insensitive on Windows)."""
    return [p.name for p in sorted(Path(root).iterdir()) if p.is_dir() and not p.name.startswith(('_', '.'))]


def key_file():
    """A made-up Instagram key in the test HOME, so the real platform counts as set up."""
    IG.KEYS.mkdir(parents=True, exist_ok=True)
    IG.TOKEN.write_text(json.dumps({'mode': 'unified', 'token': 'FAKE_TOKEN', 'user_id': 'FAKE_IG_1',
                                    'username': 'fake_account'}), encoding='utf-8')


class Case(unittest.TestCase):
    """A fresh WORK and HOME and a fresh fixture root; what the watcher prints is kept in self.out."""
    def setUp(self):
        guard.reset()
        self.assertEqual(easy.WORK_ROOT, guard.WORK)                   # the watcher's files stay in the test WORK
        base = guard.TMP / 'watch-root'
        shutil.rmtree(base, ignore_errors=True)                         # lessons one test adds never reach the next
        self.root = fixtures.make_root(base)
        self.order = lessons(self.root)
        self.lock, self.beat = guard.WORK / 'watch.lock', guard.WORK / 'watcher.json'
        self.out = io.StringIO()
        self.addCleanup(self._join)

    def _join(self):
        for t in list(P._threads.values()):
            t.join(10)

    def patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def watch(self, root=None) -> str:
        """One watcher run (watch --once), as the scheduled task starts it; returns what it printed."""
        with contextlib.redirect_stdout(self.out):
            easy.watch(str(root or self.root), True)
        return self.out.getvalue()


class LessonFailureIsolated(Case):
    """S31: a watcher run in which one lesson fails still runs the publishing step (before and after the lessons)
    and writes STATUS.txt. The failure is printed, kept in the lesson's status and error.txt, and the lessons after
    it are still looked at."""
    def setUp(self):
        super().setUp()
        fixtures.write_lesson(self.root, 'Broken render', state='new')     # a new recording: sorts first
        self.assertEqual(lessons(self.root), ['Broken render', *self.order])
        self.fake = fixtures.fake_platform(self.root)
        P.set_registry({'instagram': self.fake})
        self.patch(P, '_utcnow', lambda: T('2026-11-05 20:05'))           # Thu: the 20:00 slot is open; auto is on
        self.rendered, self.looked_at, real_youtube = [], [], easy.youtube_step

        def process(lesson, root, log_only=False):
            self.rendered.append(Path(lesson).name)
            raise RuntimeError('made-up render failure')

        def youtube_step(lesson):
            self.looked_at.append(lesson.name)
            return real_youtube(lesson)
        self.patch(easy, 'process', process)
        self.patch(easy, 'youtube_step', youtube_step)
        self.steps = mock.Mock(wraps=P.auto_step)
        self.patch(P, 'auto_step', self.steps)

    def test_the_run_goes_on(self):
        printed = self.watch()
        self.assertEqual(self.rendered, ['Broken render'])
        self.assertEqual([l for l in printed.splitlines() if l.startswith('lesson ')], [f'lesson Broken render: {FAILURE}'])
        self.assertEqual(self.looked_at, ['Broken render', *self.order])   # the ones after it too
        self.assertEqual(self.steps.call_count, 2)                          # before and after the lessons
        self.assertEqual([args[0] for name, args in self.fake.calls if name == 'post'], [fixtures.WAITING[0]])
        status = (self.root / overview.NAME).read_text(encoding='utf-8')
        self.assertTrue(status.startswith('ALMANCA DİL ATÖLYESİ · STATUS\nUpdated '), status[:80])
        self.assertIn(f'■ Broken render\n  ❌ Stopped: {FAILURE}\n', status)
        out = tools.extras(self.root / 'Broken render' / 'Output')
        st = json.loads((out / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual((st['state'], st['error']), ('failed', FAILURE))
        error = (guard.WORK / tools.slug('Broken render') / 'error.txt').read_text(encoding='utf-8')
        self.assertTrue(error.startswith('Traceback') and error.rstrip().endswith(FAILURE), error[-200:])
        self.assertFalse(self.lock.exists())
        self.assertEqual(json.loads(self.beat.read_text(encoding='utf-8'))['phase'], 'done')

    def test_safe_process_still_raises(self):
        """easy.py run still exits with an error: only watch() catches."""
        with contextlib.redirect_stdout(self.out), self.assertRaisesRegex(RuntimeError, '^made-up render failure$'):
            easy.safe_process(self.root / 'Broken render', self.root)
        st = json.loads((tools.extras(self.root / 'Broken render' / 'Output') / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(st['state'], 'failed')

    def test_a_failing_youtube_step_is_isolated_too(self):
        def youtube_step(lesson):
            self.looked_at.append(lesson.name)
            if lesson.name == 'Um etwas bitten':
                raise OSError('made-up Drive error')
        self.patch(easy, 'youtube_step', youtube_step)
        printed = self.watch()
        self.assertEqual([l for l in printed.splitlines() if l.startswith('lesson ')],
                         [f'lesson Broken render: {FAILURE}', 'lesson Um etwas bitten: OSError: made-up Drive error'])
        self.assertEqual(self.looked_at, ['Broken render', *self.order])
        self.assertEqual(self.steps.call_count, 2)
        self.assertTrue((self.root / overview.NAME).read_text(encoding='utf-8').startswith('ALMANCA DİL ATÖLYESİ'))


class PublishStepPlaces(Case):
    """Where the publishing step and the heartbeat run: before and after the lessons; on the busy path (another
    run holds a fresh watch.lock) only the publishing step; nothing but the heartbeat while Drive is missing."""
    def setUp(self):
        super().setUp()
        self.events = []
        say = self.events.append
        self.patch(P, 'auto_step', lambda root, *a, **k: say('publish') or [])
        self.patch(easy, 'changed', lambda lesson: say(f'changed {lesson.name}') or False)
        self.patch(easy, 'youtube_step', lambda lesson: say(f'youtube {lesson.name}'))
        self.patch(overview, 'write', lambda root, idle=False: say(f'status idle={idle}'))
        self.patch(easy, 'heartbeat', lambda phase: say(f'heartbeat {phase}'))

    def test_a_normal_run(self):
        self.watch()
        self.assertEqual(set(self.order), LESSONS)
        each = [e for name in self.order for e in (f'changed {name}', f'youtube {name}')]
        self.assertEqual(self.events, ['heartbeat start', 'publish', *each, 'publish', 'status idle=True',
                                       'heartbeat done'])
        self.assertFalse(self.lock.exists())

    def test_busy(self):
        self.lock.write_text('4242')                                       # a long render in another run
        self.watch()
        self.assertEqual(self.events, ['heartbeat busy', 'publish'])
        self.assertEqual(self.lock.read_text(), '4242')                    # the other run's lock stays

    def test_a_lock_older_than_6_hours_is_ignored(self):
        self.lock.write_text('4242')
        old = time.time() - 7 * 3600
        os.utime(self.lock, (old, old))
        self.watch()
        self.assertEqual(self.events[:2], ['heartbeat start', 'publish'])
        self.assertEqual(self.events[-3:], ['publish', 'status idle=True', 'heartbeat done'])
        self.assertFalse(self.lock.exists())

    def test_drive_missing(self):
        missing = guard.TMP / 'no-drive' / 'SINEM-YouTube'
        self.assertIn('not available yet; next try in 10 minutes', self.watch(missing))
        self.assertEqual(self.events, ['heartbeat start'])                 # alive, so no false 'watcher stale'
        self.assertFalse(self.lock.exists())
        self.lock.write_text('4242')
        self.events.clear()
        self.watch(missing)
        self.assertEqual(self.events, ['heartbeat busy'])

    def test_publish_step_never_raises(self):
        self.patch(P, 'auto_step', mock.Mock(side_effect=RuntimeError('made up')))
        with contextlib.redirect_stdout(self.out):
            easy.publish_step(self.root)
            with mock.patch.dict(sys.modules, {'publish': None}):
                easy.publish_step(self.root)
        self.assertEqual(self.out.getvalue().splitlines()[0], 'publish step: RuntimeError: made up')
        self.assertTrue(self.out.getvalue().splitlines()[1].startswith('publish step: ModuleNotFoundError: '))
        self.watch()
        self.assertEqual(self.events[-2:], ['status idle=True', 'heartbeat done'])     # the run went on


class PublishWhileRendering(Case):
    """F11: the scheduled task starts no second run while one renders (IgnoreNew), so the one run posts a Reel
    that becomes due during a long render, from a side thread, and only once."""
    def test_F11_post_during_the_render(self):
        clock = [T('2026-11-05 19:50')]
        self.patch(P, '_utcnow', lambda: clock[0])
        fixtures.write_lesson(self.root, 'Long render', state='new')
        fake = fixtures.fake_platform(self.root)
        P.set_registry({'instagram': fake})
        self.patch(easy, 'PUBLISH_EVERY', 0.05)
        self.patch(easy, 'youtube_step', lambda lesson: None)
        seen = []

        def process(lesson, root, log_only=False):
            clock[0] = T('2026-11-05 20:05')                   # the slot opens while it renders
            end = time.monotonic() + 5
            while time.monotonic() < end and not any(c[0] == 'post' for c in fake.calls):
                time.sleep(0.02)
            seen.append(sum(c[0] == 'post' for c in fake.calls))
        self.patch(easy, 'process', process)
        self.watch()
        self.assertEqual(seen, [1])                                     # posted during the render
        self.assertEqual(sum(c[0] == 'post' for c in fake.calls), 1)    # and never twice (same_day)
        rec = json.loads(fake.records.read_text(encoding='utf-8'))
        self.assertEqual(rec[fixtures.WAITING[0]]['posted'], '2026-11-05 20:05')
        self.assertEqual([t.name for t in threading.enumerate() if t.name == 'publish-while-rendering'], [])


class Heartbeat(Case):
    """watcher.json {'at', 'phase', 'pid'}: written atomically at the start and at the end of a run, and never
    raises."""
    def test_phases_of_a_run(self):
        seen, real = [], easy.heartbeat

        def heartbeat(phase):
            real(phase)
            seen.append(json.loads(self.beat.read_text(encoding='utf-8')))
        self.patch(easy, 'heartbeat', heartbeat)
        self.patch(P, 'auto_step', lambda root, *a, **k: [])
        self.patch(easy, 'youtube_step', lambda lesson: None)
        self.watch()
        self.assertEqual([b['phase'] for b in seen], ['start', 'done'])
        for b in seen:
            self.assertEqual((set(b), b['pid']), ({'at', 'phase', 'pid'}, os.getpid()))
            at = datetime.strptime(b['at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            self.assertLess(abs((datetime.now(timezone.utc) - at).total_seconds()), 120)

    def test_atomic(self):
        done, real = [], os.replace

        def replace(src, dst):
            done.append((Path(src).name, Path(dst).name))
            return real(src, dst)
        with mock.patch.object(easy.os, 'replace', replace):
            easy.heartbeat('busy')
        self.assertEqual(done, [(f'watcher.json.tmp-{os.getpid()}', 'watcher.json')])
        self.assertEqual(json.loads(self.beat.read_text(encoding='utf-8'))['phase'], 'busy')
        self.assertEqual(list(guard.WORK.glob('watcher.json.tmp-*')), [])

    def test_never_raises(self):
        self.beat.mkdir(parents=True)                       # a folder where the file goes: the replace is refused
        with contextlib.redirect_stdout(self.out):
            easy.heartbeat('start')
        self.assertRegex(self.out.getvalue(), r'^heartbeat not written: \w+Error: ')
        self.assertEqual(list(guard.WORK.glob('watcher.json.tmp-*')), [])
        self.assertTrue(self.beat.is_dir())


class HeartbeatWhileRendering(Case):
    """E3: the side thread refreshes the heartbeat during a long render, so Home never says 'The PC watcher is not
    running' while a lesson renders, even when watch.lock does not count (older than 6 h)."""
    def test_E3_heartbeat_during_a_long_render(self):
        import dashboard
        self.assertLessEqual(2 * easy.PUBLISH_EVERY, dashboard.WATCHER_STALE)   # one late beat is still green
        fixtures.write_lesson(self.root, 'Long render', state='new')
        self.patch(easy, 'PUBLISH_EVERY', 0.05)
        self.patch(easy, 'youtube_step', lambda lesson: None)
        self.patch(P, 'auto_step', lambda root, *a, **k: [])
        seen = []

        def process(lesson, root, log_only=False):
            old = datetime.now(timezone.utc) - timedelta(minutes=45)      # the render started 45 min ago
            self.beat.write_text(json.dumps({'at': S.iso(old), 'phase': 'start', 'pid': 1}), encoding='utf-8')
            t = time.time() - 7 * 3600
            os.utime(self.lock, (t, t))                                    # a lock that no longer counts as busy
            seen.append(dashboard.watcher()['stale'])
            end = time.monotonic() + 5
            while time.monotonic() < end and json.loads(self.beat.read_text(encoding='utf-8'))['phase'] != 'rendering':
                time.sleep(0.02)
            seen.append(dashboard.watcher())
        self.patch(easy, 'process', process)
        self.watch()
        self.assertTrue(seen[0])                                           # without a fresh beat it would be red
        self.assertEqual((seen[1]['ok'], seen[1]['busy_since']), (True, None), seen[1])
        self.assertEqual(json.loads(self.beat.read_text(encoding='utf-8'))['phase'], 'done')


class StatusNext(Case):
    """STATUS.txt's NEXT line and footer sentence come from publish.next_run with the real Instagram platform
    (no Meta call): OFF with the number waiting when automatic posting is off, the next time when it is on, nothing
    when Instagram is not set up. The lesson lines stay as they were."""
    FOOT_OFF = 'Instagram: automatic posting is off; Reels are posted from the dashboard.'
    FOOT_ON = 'Instagram: One Reel a day after 20:00, at least 20 h apart.'

    def setUp(self):
        super().setUp()
        key_file()
        self.calls = guard.script_meta([])

    def build(self) -> list[str]:
        with contextlib.redirect_stdout(self.out):
            return overview.build(self.root, idle=True, now=fixtures.NOW).splitlines()

    @staticmethod
    def sections(lines):
        """(the NEXT lines, the footer lines between 'How it works' and the last line)."""
        nxt = lines[lines.index('NEXT') + 1:lines.index('LESSONS (newest first)') - 1] if 'NEXT' in lines else []
        how = next(i for i, l in enumerate(lines) if l.startswith('How it works: '))
        return nxt, lines[how + 2:-1]

    def test_off(self):
        S.pause_file('instagram').write_text('tester 2026-11-04 21:27', encoding='utf-8')
        lines = self.build()
        nxt, foot = self.sections(lines)
        self.assertEqual(nxt[0], '  Instagram: automatic posting OFF (6 waiting)')
        self.assertTrue(all(l.startswith('  YouTube: ') for l in nxt[1:]), nxt)
        self.assertEqual(foot, [self.FOOT_OFF])
        self.assertIn('  Instagram: 5 of 9 Reels posted', lines)
        self.assertEqual(lines[-1], 'This page is rewritten by the PC; changes here are lost.')
        self.assertEqual(self.calls, [])

    def test_on(self):
        lines = self.build()
        nxt, foot = self.sections(lines)
        self.assertEqual(nxt[0], '  Instagram Reel: Thu 5 Nov after 20:00 · new: da ile resmi sebep')
        self.assertEqual(foot, [self.FOOT_ON])
        self.assertIn('  Instagram: 5 of 9 Reels posted', lines)
        self.assertEqual(self.calls, [])

    def test_G14_off_but_an_interrupted_post(self):
        """G14: with automatic posting off, an interrupted post (it stops manual posts too) is still named."""
        S.pause_file('instagram').write_text('tester 2026-11-04 21:27', encoding='utf-8')
        old = S.iso(fixtures.NOW - timedelta(minutes=20))
        with P._Con(write=True) as con:
            con.execute("INSERT INTO jobs (id, platform, item_key, title, by, who, state, created, started, heartbeat, "
                        "container_id) VALUES ('j00000000aa', 'instagram', ?, 't', 'manual', 'tester', 'interrupted', "
                        "?, ?, ?, 'c-60')", (fixtures.WAITING[0], old, old, old))
        nxt, foot = self.sections(self.build())
        self.assertEqual(nxt[0], '  Instagram: an interrupted post needs a check (5 waiting)')
        self.assertEqual(foot, ['Instagram: nothing is posted until the interrupted post is checked on the dashboard.'])
        self.assertEqual(self.calls, [])

    def test_G14_off_but_the_breaker_is_open(self):
        S.pause_file('instagram').write_text('tester 2026-11-04 21:27', encoding='utf-8')
        P._trip('meta', 'auth', 'Instagram refused the key (code 190).', 'tester')
        nxt, foot = self.sections(self.build())
        self.assertEqual(nxt[0], '  Instagram: posting stopped: Instagram refused the key (code 190).')
        self.assertNotEqual(foot, [self.FOOT_OFF])
        self.assertEqual(self.calls, [])

    def test_not_set_up(self):
        IG.TOKEN.unlink()
        lines = self.build()
        nxt, foot = self.sections(lines)
        self.assertEqual(([l for l in nxt if 'Instagram' in l], foot), ([], []))
        self.assertIn('  Instagram: 5 of 9 Reels posted', lines)


class OverviewNeverRaises(Case):
    """overview.write() never raises: a publish failure becomes one NEXT line; anything else is printed and the
    page is left as it was."""
    def setUp(self):
        super().setUp()
        key_file()
        guard.script_meta([])

    def test_next_run_fails(self):
        with mock.patch.object(P, 'next_run', side_effect=RuntimeError('made up')):
            text = overview.write(self.root, idle=True)
        self.assertIn('NEXT\n  Instagram: status not available (RuntimeError)\n', text)
        self.assertNotIn('automatic posting is off', text)
        self.assertEqual((self.root / overview.NAME).read_text(encoding='utf-8'), text)

    def test_publish_cannot_be_imported(self):
        with mock.patch.dict(sys.modules, {'publish': None}):
            text = overview.write(self.root, idle=True)
        self.assertIn('NEXT\n  Instagram: status not available (ModuleNotFoundError)\n', text)
        self.assertIn('  Instagram: 5 of 9 Reels posted', text)

    def test_anything_else_is_printed(self):
        before = (self.root / overview.NAME).read_text(encoding='utf-8')
        with mock.patch.object(overview, 'build', side_effect=OSError('made-up Drive error')), \
                contextlib.redirect_stdout(self.out):
            self.assertIsNone(overview.write(self.root, idle=True))
        self.assertEqual(self.out.getvalue(), 'overview: OSError: made-up Drive error\n')
        self.assertEqual((self.root / overview.NAME).read_text(encoding='utf-8'), before)


if __name__ == '__main__':
    unittest.main()
