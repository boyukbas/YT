"""platform_youtube: the PC's YouTube uploads through publish.py's posting core (lock, job journal, spacing rules).
YouTube itself is faked: youtube.upload / find_upload / video_state / make_public / apply_video are replaced, so
no request leaves the PC (tests.guard also blocks the network)."""
import json, shutil, unittest
from pathlib import Path
from unittest import mock
from . import fixtures, guard
import publish as P
import platform_youtube as PY
import tools
import youtube as Y

NEW9 = 'new/Output/new-short-9.mp4'
VID = 'FAKE_VID_09'
WHO = 'test@example.com'


class FakeTube:
    """The youtube.py calls platform_youtube makes, recorded in .calls. states: what video_state answers, one per call
    (the last one repeats); found: what find_upload answers."""
    def __init__(self, states=None, found=None, uploaded=VID):
        self.calls, self.found, self.uploaded = [], found, uploaded
        self.states = list(states or [{'privacy': 'private', 'upload': 'processed', 'publish_at': None,
                                       'failure': None}])

    def patches(self):
        return [mock.patch.object(Y, 'upload', self.upload), mock.patch.object(Y, 'find_upload', self.find_upload),
                mock.patch.object(Y, 'video_state', self.video_state), mock.patch.object(Y, 'make_public', self.public),
                mock.patch.object(Y, 'apply_video', self.apply_video), mock.patch.object(Y, 'taken_elsewhere',
                                                                                         lambda out: set())]

    def upload(self, yt, mp4, w, on_part=None, part=0):
        self.calls.append(('upload', Path(mp4).name, w['title'], w['description']))
        if on_part:
            on_part(1, 1)
        return self.uploaded

    def find_upload(self, yt, mp4, taken):
        self.calls.append(('find_upload', Path(mp4).name))
        return self.found

    def video_state(self, yt, vid):
        self.calls.append(('video_state', vid))
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return None if st is None else dict(st)

    def public(self, yt, vid):
        self.calls.append(('make_public', vid))

    def apply_video(self, yt, vid, out, mp4, w, rec, dry):
        self.calls.append(('apply_video', vid, w['public']))
        rec['thumbnail'] = 'set'
        return ['thumbnail'], None, []


class Case(unittest.TestCase):
    def setUp(self):
        guard.reset()
        base = guard.TMP / 'yt-upload-root'
        shutil.rmtree(base, ignore_errors=True)
        self.root = fixtures.make_root(base)
        self.now = fixtures.NOW
        self.p = PY.YouTubePlatform(client=object())
        self.tube = FakeTube()
        for patch in [mock.patch.object(P, '_utcnow', lambda: self.now), mock.patch.object(PY, 'PROCESS_EVERY', 0),
                      mock.patch.object(PY.time, 'sleep', lambda s: None),
                      mock.patch.object(PY.YouTubePlatform, 'set_up', lambda self: True)]:
            patch.start()
            self.addCleanup(patch.stop)
        P.set_registry({'youtube': self.p})
        self.addCleanup(P.set_registry, None)
        self.addCleanup(self._join)

    def _join(self):
        for t in list(P._threads.values()):
            t.join(10)

    def faked(self, tube=None):
        self.tube = tube or self.tube
        for patch in self.tube.patches():
            patch.start()
            self.addCleanup(patch.stop)

    def items(self) -> dict:
        return {i.key: i for i in self.p.candidates(self.root)[0]}

    def applied(self, key=NEW9) -> dict:
        out = self.root / Path(key).parent
        return json.loads((tools.extras(out) / Y.APPLIED).read_text(encoding='utf-8'))

    def upload(self, key=NEW9, caption=None):
        """A manual upload from the dashboard to its end; returns the job."""
        pv = P.preview(self.root, 'youtube', P.item_id(key), now=self.now)
        warns = [c['code'] for c in pv['checks'] if c['level'] == 'warn']
        body = P.start_post(self.root, 'youtube', P.item_id(key), pv['caption'] if caption is None else caption,
                            False, pv['check'], warns, WHO)
        return P._wait(body['job']['id'])


class Candidates(Case):
    def test_only_files_without_an_upload_wait(self):
        items = self.items()
        self.assertIsNone(items[NEW9].posted)
        self.assertIsNotNone(items['new/Output/new-video.mp4'].posted)
        self.assertNotIn('ZU + INFINITIV/Output/zu-infinitiv-short.mp4', items)          # hand-made: no youtube.json

    def test_caption_is_title_then_description(self):
        it = self.items()[NEW9]
        cap = self.p.default_caption(it, self.root)
        self.assertEqual(PY.split_caption(cap), (it.entry['title'], it.entry['description']))

    def test_scheduled_videos_count_once_their_time_has_come(self):
        keys = [p.key for p in self.p.history(self.root)]
        self.assertIn('new/Output/new-short-5.mp4', keys)
        self.assertNotIn('new/Output/new-short-6.mp4', keys)                           # scheduled after NOW

    def test_timing_warnings_say_video(self):
        texts = ' '.join(c['text'] for c in P.preview(self.root, 'youtube', P.item_id(NEW9), now=self.now)['checks']
                         if c['code'] in ('gap', 'same_day', 'hard_cap'))
        self.assertIn('video', texts)
        self.assertNotIn('Reel', texts)

    def test_a_pending_record_still_waits(self):
        path = tools.extras(self.root / 'new' / 'Output') / Y.APPLIED
        data = json.loads(path.read_text(encoding='utf-8'))
        data['new-short-9.mp4'] = {'id': VID, 'publish_pending': True}
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assertIsNone(self.items()[NEW9].posted)
        self.assertNotIn(VID, [p.id for p in self.p.history(self.root)])


class Upload(Case):
    def test_upload_processing_public_and_the_record(self):
        self.faked()
        job = self.upload()
        self.assertEqual(job['state'], 'done', job)
        title, desc = PY.split_caption(self.p.default_caption(self.items()[NEW9], self.root))
        self.assertEqual([c[0] for c in self.tube.calls],
                         ['find_upload', 'upload', 'apply_video', 'video_state', 'make_public'])
        self.assertEqual(self.tube.calls[1], ('upload', 'new-short-9.mp4', title, desc))
        self.assertEqual(self.tube.calls[2], ('apply_video', VID, False))          # details while still private
        rec = self.applied()['new-short-9.mp4']
        self.assertEqual((rec['id'], rec['publish_pending'], rec['title'], rec['thumbnail'], rec['by']),
                         (VID, False, title, 'set', 'manual'))            # a dashboard upload
        self.assertEqual(rec['publish_at'], PY._z(self.now))
        self.assertEqual(rec['permalink'], f'https://youtube.com/shorts/{VID}')
        self.assertIsNotNone(self.items()[NEW9].posted)
        self.assertEqual(self.p.history(self.root)[-1].id, VID)

    def test_edited_caption_is_what_goes_up(self):
        self.faked()
        job = self.upload(caption='Kısa başlık\n\nYeni açıklama #almanca')
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(self.tube.calls[1][2:], ('Kısa başlık', 'Yeni açıklama #almanca'))

    def test_a_file_already_on_youtube_is_not_uploaded_again(self):
        self.faked(FakeTube(found={'id': 'HAND_UPLOAD', 'status': {'privacyStatus': 'private'}}))
        job = self.upload()
        self.assertEqual(job['state'], 'done', job)
        self.assertNotIn('upload', [c[0] for c in self.tube.calls])
        self.assertEqual(self.applied()['new-short-9.mp4']['id'], 'HAND_UPLOAD')

    def test_an_already_public_video_is_not_touched(self):
        self.faked(FakeTube(states=[{'privacy': 'public', 'upload': 'processed', 'publish_at': None, 'failure': None}]))
        self.assertEqual(self.upload()['state'], 'done')
        self.assertNotIn('make_public', [c[0] for c in self.tube.calls])

    def test_a_cut_off_upload_is_finished_not_sent_twice(self):
        path = tools.extras(self.root / 'new' / 'Output') / Y.APPLIED
        data = json.loads(path.read_text(encoding='utf-8'))
        data['new-short-9.mp4'] = {'id': VID, 'publish_pending': True}
        path.write_text(json.dumps(data), encoding='utf-8')
        self.faked()
        self.assertEqual(self.upload()['state'], 'done')
        names = [c[0] for c in self.tube.calls]
        self.assertNotIn('upload', names)
        self.assertNotIn('find_upload', names)
        self.assertIn('make_public', names)

    def test_rejected_by_youtube_fails_and_stays_private(self):
        self.faked(FakeTube(states=[{'privacy': 'private', 'upload': 'rejected', 'publish_at': None,
                                     'failure': 'duplicate'}]))
        job = self.upload()
        self.assertEqual(job['state'], 'failed', job)
        self.assertNotIn('make_public', [c[0] for c in self.tube.calls])
        rec = self.applied()['new-short-9.mp4']
        self.assertEqual((rec['id'], rec['publish_pending']), (VID, True))
        self.assertIsNone(self.items()[NEW9].posted)

    def test_used_up_quota_reads_plainly_and_blocks_the_next_upload(self):
        class Used(FakeTube):
            def find_upload(self, yt, mp4, taken):
                self.calls.append(('find_upload', mp4.name))
                raise RuntimeError('<HttpError 403 when requesting youtube/v3/channels returned "The request cannot be '
                                   'completed because you have exceeded your quota.". Details: "quotaExceeded">')
        self.faked(Used())
        self.addCleanup((guard.WORK / Y.QUOTA_FILE).unlink, missing_ok=True)
        job = self.upload()
        self.assertEqual((job['state'], job['kind']), ('failed', 'rate'), job)
        self.assertTrue(job['error'].startswith("YouTube's daily API limit is used up; it is back at "), job['error'])
        self.assertNotIn('HttpError', job['error'])
        self.assertEqual([c[0] for c in self.tube.calls], ['find_upload'])                # nothing went up
        quota = [c for c in P.preview(self.root, 'youtube', P.item_id(NEW9), now=self.now)['checks'] if c['code'] == 'quota']
        self.assertEqual([c['level'] for c in quota], ['block'])                  # the button says so before trying

    def test_quota_note_from_the_watcher_blocks_upload_too(self):
        self.addCleanup((guard.WORK / Y.QUOTA_FILE).unlink, missing_ok=True)
        Y.quota_used_up(self.now)
        checks = P.preview(self.root, 'youtube', P.item_id(NEW9), now=self.now)['checks']
        self.assertEqual([(c['code'], c['level']) for c in checks][0], ('quota', 'block'))
        self.assertIn('back at', checks[0]['text'])

    def test_slow_processing_goes_public_after_the_wait(self):
        busy = {'privacy': 'private', 'upload': 'uploaded', 'publish_at': None, 'failure': None}
        self.faked(FakeTube(states=[busy]))
        with mock.patch.object(PY, 'PROCESS_WAIT', 30), mock.patch.object(PY, 'PROCESS_EVERY', 15):
            self.assertEqual(self.upload()['state'], 'done')
        self.assertEqual([c[0] for c in self.tube.calls].count('video_state'), 3)          # 0, 15, 30 s

    def test_details_failing_does_not_stop_the_upload(self):
        self.faked()

        def broken(*a):
            raise RuntimeError('thumbnail refused')
        with mock.patch.object(Y, 'apply_video', broken):
            job = self.upload()
        self.assertEqual(job['state'], 'done', job)
        self.assertIn('make_public', [c[0] for c in self.tube.calls])


class Records(Case):
    def test_writes_only_under_the_lock(self):
        it = self.items()[NEW9]
        with self.assertRaises(OSError):
            self.p.write_record(it, {'id': VID})
        with P.PostLock(owner='cli'):
            self.p.write_record(it, {'id': VID, 'publish_pending': True})
        self.assertEqual(self.applied()['new-short-9.mp4']['id'], VID)

    def test_hand_mark_never_replaces_a_real_id(self):
        it = self.items()[NEW9]
        with P.PostLock(owner='cli'):
            self.p.write_record(it, {'id': VID, 'publish_pending': True})
            self.p.write_record(it, {'id': 'hand', 'posted_utc': self.now.isoformat()})
        self.assertEqual(self.applied()['new-short-9.mp4']['id'], VID)

    def test_other_files_records_are_kept(self):
        before = self.applied()
        with P.PostLock(owner='cli'):
            self.p.write_record(self.items()[NEW9], {'id': VID})
        after = self.applied()
        self.assertEqual({k: v for k, v in after.items() if k != 'new-short-9.mp4'}, before)


class Status(Case):
    def test_container_states(self):
        for st, want in (({'privacy': 'public', 'upload': 'processed'}, 'PUBLISHED'),
                         ({'privacy': 'private', 'upload': 'processed'}, 'FINISHED'),
                         ({'privacy': 'private', 'upload': 'uploaded'}, 'IN_PROGRESS'),
                         ({'privacy': 'private', 'upload': 'rejected'}, 'ERROR'), (None, 'EXPIRED')):
            with self.subTest(want=want), mock.patch.object(Y, 'video_state', lambda yt, vid, st=st: st):
                self.assertEqual(self.p.container_status(f'video:{VID}'), want)
        self.assertEqual(self.p.container_status('video:not an id'), 'EXPIRED')

    def test_error_kinds(self):
        from googleapiclient.errors import HttpError

        class Resp(dict):
            def __init__(self, status):
                super().__init__(status=str(status))
                self.status, self.reason = status, 'x'

        def http(status, reason=''):
            return HttpError(Resp(status), json.dumps({'error': {'errors': [{'reason': reason}]}}).encode())
        for exc, kind in ((http(401), 'auth'), (http(403, 'quotaExceeded'), 'rate'), (http(403), 'permission'),
                          (http(503), 'transient'), (http(400), 'content'), (PY.Rejected('x'), 'content'),
                          (ConnectionResetError(), 'transient'), (FileNotFoundError(), 'local'),
                          (ValueError(), 'unknown')):
            with self.subTest(exc=repr(exc)):
                self.assertEqual(PY.classify(exc), kind)


class Checks(Case):
    def codes(self, caption):
        return {(c.code, c.level) for c in self.p.preflight(self.items()[NEW9], caption)}

    def test_title_and_description_rules(self):
        self.assertFalse({lv for _, lv in self.codes('Başlık\n\nAçıklama')} & {'block'})
        self.assertIn(('caption_empty', 'block'), self.codes('   '))
        self.assertIn(('caption_too_long', 'block'), self.codes('x' * 101))
        self.assertIn(('caption_too_long', 'block'), self.codes('Title\n\n' + 'ş' * 2600))      # 5,200 bytes
        self.assertIn(('caption_empty', 'block'), self.codes('a <b> title'))
        self.assertIn(('caption_tags', 'info'), self.codes('T\n\n#a #b #c #d'))


class WatcherWaits(unittest.TestCase):
    """easy.youtube_step leaves the lesson alone while the PC is uploading (no double find_upload, no write race)."""
    def test_uploading(self):
        import easy
        for job, want in (({'state': 'running'}, True), ({'state': 'queued'}, True), ({'state': 'done'}, False),
                          (None, False)):
            with self.subTest(job=job), mock.patch.object(P, 'current_job', lambda name, job=job: job):
                self.assertEqual(easy._uploading(), want)


class _Call:
    def __init__(self, log, name, answer):
        self.log, self.name, self.answer = log, name, answer

    def __call__(self, **kw):
        self.log.append(self.name)
        return self

    def execute(self):
        return self.answer


class QuotaTube:
    """A YouTube client that records which list calls youtube.apply_video makes (no writes expected)."""
    def __init__(self, snippet, status):
        self.calls = []
        self.video = {'snippet': snippet, 'status': status, 'paidProductPlacementDetails': {'hasPaidProductPlacement': False}}

    def videos(self):
        return mock.Mock(list=_Call(self.calls, 'videos.list', {'items': [self.video]}))

    def captions(self):
        return mock.Mock(list=_Call(self.calls, 'captions.list', {'items': []}))


class QuotaSaving(unittest.TestCase):
    """2026-11-06: the watcher's 10-minute re-check of one private video (its comment waits until it is public) used
    most of the daily YouTube quota: 50 units each time for the caption-track list. Now the list is read only when a
    track has to go up, and after quotaExceeded the watcher leaves YouTube alone until the quota is back."""
    def setUp(self):
        self.tmp = guard.TMP / f'yt-quota-{self._testMethodName}'
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.out = self.tmp / 'lesson' / 'Output'
        tools.extras(self.out).mkdir(parents=True)

    def wanted(self):
        return {'title': 'T', 'description': 'D', 'tags': ['a'], 'categoryId': '27', 'defaultLanguage': 'tr',
                'defaultAudioLanguage': 'tr', 'madeForKids': False, 'ai': False, 'public': False, 'publish': None,
                'paid': False, 'thumbnail': None, 'playlist': None, 'comment': 'Alıştırma'}

    def tube(self):
        w = self.wanted()
        return QuotaTube({k: w[k] for k in ('title', 'description', 'tags', 'categoryId', 'defaultLanguage',
                                            'defaultAudioLanguage')} | {'publishedAt': '2026-11-05T10:00:00Z'},
                         {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False})

    def test_a_video_waiting_to_be_public_costs_one_unit(self):
        mp4 = self.out / '3-video.mp4'
        rec = {}
        for lang in ('tr', 'de'):
            srt = tools.extras(self.out) / f'3-video-subtitles-{lang}.srt'
            srt.write_text(f'1\n00:00:00,000 --> 00:00:01,000\n{lang}\n', encoding='utf-8')
            rec[f'captions_{lang}'] = Y.sha(srt)
        yt = self.tube()
        done, waiting, problems = Y.apply_video(yt, 'FAKE_VID_03', self.out, mp4, self.wanted(), rec, dry=False)
        self.assertEqual((done, waiting, problems), ([], ['comment (posted when the video is public)'], []))
        self.assertEqual(yt.calls, ['videos.list'])                  # no captions.list (50 units)

    def test_a_changed_track_still_reads_the_list(self):
        srt = tools.extras(self.out) / '3-video-subtitles-tr.srt'
        srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nyeni\n', encoding='utf-8')
        yt = self.tube()
        done, _, _ = Y.apply_video(yt, 'FAKE_VID_03', self.out, self.out / '3-video.mp4', self.wanted(),
                                   {'captions_tr': 'old'}, dry=True)
        self.assertEqual(yt.calls, ['videos.list', 'captions.list'])
        self.assertEqual(done, ['TR captions (added)'])

    def test_quota_note_lasts_until_midnight_pacific(self):
        from datetime import datetime, timezone
        self.addCleanup((guard.WORK / Y.QUOTA_FILE).unlink, missing_ok=True)
        self.assertIsNone(Y.quota_until())
        back = Y.quota_used_up(datetime(2026, 11, 6, 3, 42, tzinfo=timezone.utc))          # 05:42 Istanbul
        self.assertEqual(back, datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc))         # 11:00 Istanbul
        self.assertEqual(Y.quota_until(datetime(2026, 11, 6, 7, 59, tzinfo=timezone.utc)), back)
        self.assertIsNone(Y.quota_until(datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)))
        self.assertEqual(Y.quota_text(back), "YouTube's daily API limit is used up; it is back at 11:00 "
                                             "(midnight Pacific time).")
        (guard.WORK / Y.QUOTA_FILE).write_text('garbage', encoding='utf-8')
        self.assertIsNone(Y.quota_until())

    def test_watcher_skips_youtube_while_the_quota_is_used_up(self):
        import easy
        self.addCleanup((guard.WORK / Y.QUOTA_FILE).unlink, missing_ok=True)
        lesson = self.tmp / 'lesson'
        (tools.extras(self.out) / easy.OURS).write_text(json.dumps({'state': 'done'}), encoding='utf-8')
        (tools.extras(self.out) / 'youtube.json').write_text('{}', encoding='utf-8')
        token = self.tmp / 'token.json'
        token.write_text('{}', encoding='utf-8')
        calls = []

        def apply(lesson, log=print):
            calls.append(lesson.name)
            raise RuntimeError('<HttpError 403 when requesting youtube/v3/videos returned "quotaExceeded">')
        with mock.patch.object(easy, 'WORK_ROOT', self.tmp / 'work'), mock.patch.object(Y, 'TOKEN', token),                 mock.patch.object(Y, 'apply', apply), mock.patch.object(easy, '_uploading', lambda: False),                 mock.patch.object(easy, 'output_dir', lambda lesson: self.out):
            (self.tmp / 'work' / 'lesson').mkdir(parents=True)                # the lesson's work folder
            easy.youtube_step(lesson)
            self.assertEqual(calls, ['lesson'])
            self.assertTrue(Y.quota_until())
            said = json.loads((tools.extras(self.out) / easy.OURS).read_text(encoding='utf-8'))['youtube']
            self.assertIn("daily API limit is used up; it is back at", said)
            easy.youtube_step(lesson)                                    # the next run: no request at all
            self.assertEqual(calls, ['lesson'])
