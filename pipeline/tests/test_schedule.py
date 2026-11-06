"""schedule.py: the settings file (validate, save, migrate, the pause file) and the pure timing rules
(spec 4, 5.2, 5.3; safety rules S12, S13, S14, S32). Every test works in its own temp folder.
Runs in the suite (tests/__init__.py installs the guard) or alone: python -I pipeline/tests/test_schedule.py"""
import atexit, os, shutil, sys, tempfile
if 'tests.guard' not in sys.modules:                     # alone: no guard, so WORK and HOME go to a temp folder
    _TMP = tempfile.mkdtemp(prefix='test-schedule-')
    atexit.register(shutil.rmtree, _TMP, True)
    os.environ['PIPELINE_WORK'] = os.path.join(_TMP, 'work')
    os.environ['USERPROFILE'] = os.environ['HOME'] = os.path.join(_TMP, 'home')
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ast, json, threading, types, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import schedule as S

GUARD = sys.modules.get('tests.guard')
UTC = timezone.utc
REAL_WORK = Path(r'D:\YT\easy-work')
NO_SLEEP = types.SimpleNamespace(sleep=lambda s: None)
ALL_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
SPEC_EXAMPLE = {                                         # the document of spec 4.1
    'version': 1, 'rev': 1, 'updated': '2026-11-05T07:12:00Z', 'updated_by': 'migration',
    'platforms': {'instagram': {'auto': False, 'every_days': 1, 'per_day': 1, 'days': ALL_DAYS, 'post_after': '20:00',
                                'post_before': '23:59', 'min_gap_hours': 20, 'hard_cap_24h': 3,
                                'start_date': '2026-11-03'},
                  'youtube': {'auto': False, 'every_days': 1, 'per_day': 1, 'days': ALL_DAYS, 'post_after': '20:00',  # newer
                              'post_before': '23:59', 'min_gap_hours': 20, 'hard_cap_24h': 3, 'start_date': None}}}


def T(s):
    """'2026-11-04 16:46' in Istanbul time (UTC+3) as an aware UTC datetime."""
    return datetime.strptime(s, '%Y-%m-%d %H:%M').replace(tzinfo=timezone(timedelta(hours=3))).astimezone(UTC)


def P(s, by='old'):
    return S.Post(at=T(s), by=by)


def cfg(**change):
    """The default settings (daily, all days, 20:00-23:59, gap 20, cap 3) with these changes."""
    return {'auto': False, 'every_days': 1, 'per_day': 1, 'days': list(ALL_DAYS), 'post_after': '20:00', 'post_before': '23:59',
            'min_gap_hours': 20, 'hard_cap_24h': 3, 'start_date': None, **change}


def doc(rev=1, **instagram):
    """A valid publishing.json with these Instagram settings."""
    return {'version': 1, 'rev': rev, 'updated': '2026-11-05T07:12:00Z', 'updated_by': 'migration',
            'platforms': {'instagram': cfg(**instagram), 'youtube': cfg()}}


class Case(unittest.TestCase):
    """A fresh WORK folder for every test: schedule.WORK and schedule.SETTINGS point into it."""

    def setUp(self):
        if GUARD:
            GUARD.reset()
        base = Path(tempfile.mkdtemp(prefix='schedule-', dir=GUARD.TMP if GUARD else None))
        self.addCleanup(shutil.rmtree, base, True)
        self.work, self.root = base / 'work', base / 'SINEM-YouTube'
        self.work.mkdir()
        patch = mock.patch.multiple(S, WORK=self.work, SETTINGS=self.work / 'publishing.json')
        patch.start()
        self.addCleanup(patch.stop)
        self.file, self.flag = self.work / 'publishing.json', self.work / 'instagram-paused'
        self.assertNotEqual(S._path().resolve(), (REAL_WORK / 'publishing.json').resolve())

    def put(self, content):
        """Write publishing.json: a dict as JSON, a str as it is."""
        self.file.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')

    def saved(self):
        return json.loads(self.file.read_text(encoding='utf-8'))

    def channel(self, start):
        p = self.root / '_channel' / 'channel.json'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({'instagram': {'start': start, 'hashtags': ['#almanca']}}), encoding='utf-8')

    def temps(self):
        return sorted(p.name for p in self.work.glob('*.tmp*'))


# ---------- settings ----------

class Validation(Case):
    """Spec 4.1: one case per message, the cross-field window rule, unknown fields, bools are not numbers."""

    def bad(self, field, values, message, **other):
        for v in values:
            with self.subTest(field=field, value=v):
                self.assertEqual(S.validate(cfg(**other, **{field: v})).get(field), message)

    def good(self, **values):
        self.assertEqual(S.validate(cfg(**values)), {}, values)

    def test_defaults_example_and_presets_are_valid(self):
        self.good()
        self.assertEqual(S.validate(SPEC_EXAMPLE['platforms']['instagram']), {})
        for name, p in S.PRESETS.items():
            self.good(**{k: v for k, v in p.items() if k != 'label'})

    def test_auto(self):
        self.bad('auto', ('true', 1, 0, None, 'on'), 'Must be on or off.')
        self.good(auto=True)

    def test_every_days(self):
        self.bad('every_days', (0, 8, -1, 2.0, '2', None, True), 'Choose 1 to 7 days.')
        self.good(every_days=1)
        self.good(every_days=7)

    def test_days_at_least_one(self):
        self.bad('days', ([], (), None, '', 'mon'), 'Choose at least one day.')

    def test_days_unknown(self):
        self.bad('days', (['mon', 'funday'], ['Mon'], ['mon', 'mon'], [1], ['mon', None], [['mon']]), 'Unknown day.')
        self.good(days=['fri', 'mon'])                   # any order (stored in week order)

    def test_post_after(self):
        self.bad('post_after', ('23:20', '23:30', '23:59', '24:00', '8:00', '20:60', '20.00', '20:00:00', ' 20:00', 2000, None),
                 'Use HH:MM between 00:00 and 23:19.')
        self.good(post_after='00:00', post_before='00:40')
        self.good(post_after='23:19', post_before='23:59')

    def test_post_before(self):
        self.bad('post_before', ('20:39', '20:30', '20:00', '19:00', '24:00', '23:60', '2359', 2359, None),
                 'Must be 40 minutes or more after the start, and 23:59 at the latest.')
        self.good(post_before='20:40')

    def test_window_is_checked_across_fields(self):
        msg = 'Must be 40 minutes or more after the start, and 23:59 at the latest.'
        self.assertEqual(S.validate(cfg(post_after='22:00', post_before='22:39')), {'post_before': msg})
        self.assertEqual(S.validate(cfg(post_after='22:00', post_before='22:40')), {})
        self.assertEqual(S.validate(cfg(post_after='22:00', post_before='21:00')), {'post_before': msg})
        # a bad start is reported once, not again under the end
        self.assertEqual(S.validate(cfg(post_after='23:45')), {'post_after': 'Use HH:MM between 00:00 and 23:19.'})

    def test_F33_window_fits_a_retry(self):
        """F33: the shortest window is 40 minutes (the retry after 30 min plus one watcher run)."""
        msg = 'Must be 40 minutes or more after the start, and 23:59 at the latest.'
        self.assertEqual(S.validate(cfg(post_after='20:15', post_before='20:45')), {'post_before': msg})
        self.assertEqual(S.validate(cfg(post_after='20:15', post_before='20:55')), {})
        self.assertEqual(S.validate(cfg(post_after='23:20')), {'post_after': 'Use HH:MM between 00:00 and 23:19.'})

    def test_min_gap(self):
        self.bad('min_gap_hours', (0, 0.5, 168.5, 169, 20.25, 19.9, '20', None, True, float('nan'), float('inf')),
                 'Choose 1 to 168 hours, in half hours.')
        for v in (1, 1.5, 20, 20.0, 167.5, 168):
            self.good(min_gap_hours=v)

    def test_hard_cap(self):
        self.bad('hard_cap_24h', (0, 11, 2.5, '3', None, True), 'Can only be changed in publishing.json on the PC.')
        self.good(hard_cap_24h=1)
        self.good(hard_cap_24h=10)

    def test_start_date(self):
        self.bad('start_date', ('2026-02-30', '2026-11-3', '03.11.2026', '20261103', '2026-W45-3', '2026-11-03T00:00',
                                20261103, [], False), 'Use a date like 2026-11-03, or leave it empty.')
        for v in (None, '', '2026-11-03', '2028-02-29'):
            self.good(start_date=v)

    def test_unknown_setting(self):
        self.assertEqual(S.validate({**cfg(), 'colour': 'red'}), {'colour': 'Unknown setting.'})

    def test_bools_are_not_numbers(self):
        errors = S.validate(cfg(every_days=True, min_gap_hours=True, hard_cap_24h=True))
        self.assertEqual(set(errors), {'every_days', 'min_gap_hours', 'hard_cap_24h'})

    def test_missing_field(self):
        values = cfg()
        del values['auto']
        self.assertEqual(S.validate(values), {'auto': 'Must be on or off.'})


class ReadOnly(Case):
    """S12: hard_cap_24h cannot be changed through the settings API, only in publishing.json on the PC."""

    def test_a_different_cap_is_refused(self):
        self.put(doc())
        before = self.file.read_bytes()
        for v in (5, 1, 4, 3.0, '3', True, None):
            for dry in (False, True):
                with self.subTest(value=v, dry_run=dry), self.assertRaises(S.SettingsError) as e:
                    S.save_settings('instagram', {'hard_cap_24h': v, 'every_days': 2}, 1, 'dev@localhost', dry_run=dry)
                self.assertEqual(e.exception.code, 'read_only')
                self.assertEqual(e.exception.fields, {'hard_cap_24h': 'Can only be changed in publishing.json on the PC.'})
        self.assertEqual(self.file.read_bytes(), before)
        self.assertFalse(self.flag.exists())

    def test_the_current_value_may_be_sent(self):
        self.put(doc())
        out = S.save_settings('instagram', {'hard_cap_24h': 3, 'every_days': 2}, 1, 'dev@localhost')
        self.assertEqual((out['rev'], out['platforms']['instagram']['hard_cap_24h']), (2, 3))

    def test_a_cap_set_on_the_pc_is_kept(self):
        self.put(doc(hard_cap_24h=5))
        S.save_settings('instagram', {'every_days': 2}, 1, 'dev@localhost')
        self.assertEqual(self.saved()['platforms']['instagram']['hard_cap_24h'], 5)


class EffectiveTable(Case):
    """S13: on only with a valid file, auto true and no pause file; a missing, unreadable or invalid file means off."""

    def test_table(self):
        rows = [
            ('valid, auto on, no pause file', doc(auto=True), False, (True, 'on')),
            ('valid, auto on, pause file', doc(auto=True), True, (False, 'stopped_by_file')),
            ('valid, auto off, no pause file', doc(auto=False), False, (False, 'off')),
            ('valid, auto off, pause file', doc(auto=False), True, (False, 'off')),
            ('platform missing', {**doc(), 'platforms': {}}, False, (False, 'off')),
            ('not JSON', '{"version": 1, "platforms": {"instagram": {"auto": true', False, (False, 'invalid')),
            ('bad value, auto on', doc(auto=True, post_before='20:10'), False, (False, 'invalid')),
            ('unknown field, auto on', doc(auto=True, colour='red'), False, (False, 'invalid')),
            ('version 2, auto on', {**doc(auto=True), 'version': 2}, False, (False, 'invalid')),
        ]
        for name, content, flag, want in rows:
            with self.subTest(name):
                self.flag.unlink(missing_ok=True)
                self.put(content)
                if flag:
                    self.flag.write_bytes(b'')
                loaded = S.load_settings()
                self.assertEqual(S.effective_auto(loaded, 'instagram'), want)
                self.assertEqual(bool(loaded.problems), want[1] == 'invalid')
                if want[1] == 'invalid':                 # every auto False when invalid
                    self.assertEqual(loaded.doc['platforms'], {'instagram': cfg(), 'youtube': cfg()})
                    self.assertEqual(S.platform_settings(loaded, 'instagram'), cfg())

    def test_unreadable_file_means_off(self):
        self.file.mkdir()                                # a folder where the file should be
        with mock.patch.object(S, 'time', NO_SLEEP):
            loaded = S.load_settings()
        self.assertEqual(S.effective_auto(loaded, 'instagram'), (False, 'invalid'))
        self.assertRegex(loaded.problems[0], r'^publishing\.json could not be read \(\w+\)\.$')

    def test_missing_file_that_cannot_be_created_means_off(self):
        denied = mock.Mock(side_effect=PermissionError(13, 'Access is denied'))
        with mock.patch('os.link', denied), mock.patch('os.replace', denied), mock.patch.object(S, 'time', NO_SLEEP):
            loaded = S.load_settings(self.root)
        self.assertEqual(S.effective_auto(loaded, 'instagram'), (False, 'invalid'))
        self.assertEqual(loaded.problems, ['publishing.json could not be created (PermissionError).'])
        self.assertFalse(loaded.migrated)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.temps(), [])

    def test_unknown_platform_is_off(self):
        self.put(doc(auto=True))
        self.assertEqual(S.effective_auto(S.load_settings(), 'tiktok'), (False, 'off'))


class Acks(Case):
    """S14: turning automatic posting on needs auto_on; a gap under 20 h needs gap_under_20."""

    def test_off_to_on_needs_auto_on(self):
        self.put(doc())
        self.flag.write_text('dev@localhost 2026-11-04 21:27\n', encoding='utf-8')
        before = self.file.read_bytes()
        with self.assertRaises(S.NeedsAck) as e:
            S.save_settings('instagram', {'auto': True}, 1, 'dev@localhost')
        self.assertEqual(e.exception.acks, ['auto_on'])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertTrue(self.flag.exists())
        out = S.save_settings('instagram', {'auto': True}, 1, 'dev@localhost', ['auto_on'])
        self.assertTrue(out['platforms']['instagram']['auto'])
        self.assertFalse(self.flag.exists())             # saved True deletes the pause file
        self.assertEqual(S.effective_auto(S.load_settings(), 'instagram'), (True, 'on'))

    def test_stopped_by_file_to_on_needs_auto_on(self):
        self.put(doc(auto=True))
        self.flag.write_bytes(b'')
        for patch in ({'auto': True}, {'every_days': 2}, {}):
            with self.subTest(patch=patch), self.assertRaises(S.NeedsAck) as e:
                S.save_settings('instagram', patch, 1, 'dev@localhost')
            self.assertEqual(e.exception.acks, ['auto_on'])
        S.save_settings('instagram', {'auto': True}, 1, 'dev@localhost', ['auto_on'])    # "Remove the pause file"
        self.assertFalse(self.flag.exists())

    def test_on_stays_on_and_off_needs_no_ack(self):
        self.put(doc(auto=True))
        S.save_settings('instagram', {'every_days': 2}, 1, 'dev@localhost')
        S.save_settings('instagram', {'auto': False}, 2, 'dev@localhost')
        self.assertEqual(S.effective_auto(S.load_settings(), 'instagram'), (False, 'off'))
        self.assertRegex(self.flag.read_text(encoding='utf-8'), r'^dev@localhost \d{4}-\d\d-\d\d \d\d:\d\d\n$')

    def test_gap_under_20_needs_gap_under_20(self):
        self.put(doc(auto=True))
        with self.assertRaises(S.NeedsAck) as e:
            S.save_settings('instagram', {'min_gap_hours': 12}, 1, 'dev@localhost')
        self.assertEqual(e.exception.acks, ['gap_under_20'])
        S.save_settings('instagram', {'min_gap_hours': 12}, 1, 'dev@localhost', ['gap_under_20'])
        S.save_settings('instagram', {'min_gap_hours': 12, 'every_days': 2}, 2, 'dev@localhost')     # unchanged: no ack
        with self.assertRaises(S.NeedsAck):
            S.save_settings('instagram', {'min_gap_hours': 19.5}, 3, 'dev@localhost')            # another value under 20
        S.save_settings('instagram', {'min_gap_hours': 20}, 3, 'dev@localhost')
        self.assertEqual(self.saved()['rev'], 4)

    def test_missing_acks_come_in_acks_order(self):
        self.put(doc())
        patch = {'auto': True, 'min_gap_hours': 10}
        with self.assertRaises(S.NeedsAck) as e:
            S.save_settings('instagram', patch, 1, 'dev@localhost', ['resume_auto'])
        self.assertEqual(e.exception.acks, ['auto_on', 'gap_under_20'])
        with self.assertRaises(S.NeedsAck) as e:
            S.save_settings('instagram', patch, 1, 'dev@localhost', ['gap_under_20'])
        self.assertEqual(e.exception.acks, ['auto_on'])
        out = S.save_settings('instagram', patch, 1, 'dev@localhost', ['gap_under_20', 'auto_on'])
        self.assertEqual(out['platforms']['instagram']['min_gap_hours'], 10)

    def test_dry_run_asks_too_and_writes_nothing(self):
        self.put(doc())
        self.flag.write_bytes(b'')
        before = self.file.read_bytes()
        with self.assertRaises(S.NeedsAck) as e:
            S.save_settings('instagram', {'auto': True}, 1, 'dev@localhost', dry_run=True)
        self.assertEqual(e.exception.acks, ['auto_on'])
        self.assertTrue(e.exception.doc['platforms']['instagram']['auto'])    # the merged document comes along
        out = S.save_settings('instagram', {'auto': True}, 1, 'dev@localhost', ['auto_on'], dry_run=True)
        self.assertEqual((out['rev'], out['platforms']['instagram']['auto']), (1, True))
        self.assertEqual(self.file.read_bytes(), before)
        self.assertTrue(self.flag.exists())

    def test_acks_must_be_a_list(self):
        self.put(doc())
        for ack in ('auto_on', 'auto_on_and_more', None, [['auto_on']]):
            with self.subTest(ack=ack), self.assertRaises(S.NeedsAck):
                S.save_settings('instagram', {'auto': True}, 1, 'dev@localhost', ack)
        S.save_settings('instagram', {'auto': True}, 1, 'dev@localhost', ('auto_on',))

    def test_reset_needs_reset_settings(self):
        for ack in ((), ['auto_on'], None, 'reset_settings'):
            with self.assertRaises(S.NeedsAck) as e:
                S.reset_settings('dev@localhost', ack)
            self.assertEqual(e.exception.acks, ['reset_settings'])
        self.assertFalse(self.file.exists())


class MigrationOff(Case):
    """S32: migrating the real state (pause file present, no publishing.json) leaves automatic posting off."""

    def test_real_state(self):
        self.flag.write_text('dev@localhost 2026-11-04 21:27\n', encoding='utf-8')
        flag = self.flag.read_bytes()
        self.channel('2026-11-03')
        loaded = S.load_settings(self.root)
        self.assertTrue(loaded.migrated)
        self.assertEqual(loaded.problems, [])
        self.assertEqual(S.effective_auto(loaded, 'instagram'), (False, 'off'))
        self.assertEqual(self.flag.read_bytes(), flag)   # the brake stays
        saved = self.saved()
        self.assertEqual({k: saved[k] for k in ('version', 'rev', 'updated_by')},
                         {'version': 1, 'rev': 1, 'updated_by': 'migration'})
        self.assertRegex(saved['updated'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(saved['platforms'], SPEC_EXAMPLE['platforms'])
        self.assertEqual(loaded.doc, saved)
        again = S.load_settings(self.root)
        self.assertFalse(again.migrated)
        self.assertEqual(again.doc, loaded.doc)
        self.assertEqual(self.temps(), [])


class Migration(Case):
    """Spec 4.4: no pause file -> on; the channel.json start date; the race; os.link and its fallback."""

    def test_no_pause_file_means_on(self):
        loaded = S.load_settings(self.root)
        self.assertTrue(loaded.migrated)
        self.assertEqual(S.effective_auto(loaded, 'instagram'), (True, 'on'))
        self.assertEqual(loaded.doc['platforms']['instagram'], cfg(auto=True))

    def test_start_date_from_channel_json(self):
        for start, want in (('2026-11-03', '2026-11-03'), ('2026-02-30', None), ('3.11.2026', None),
                            (20261103, None), (None, None)):
            with self.subTest(start=start):
                self.file.unlink(missing_ok=True)
                self.channel(start)
                self.assertEqual(S.load_settings(self.root).doc['platforms']['instagram']['start_date'], want)
        start = lambda: S.load_settings(self.root).doc['platforms']['instagram']['start_date']
        self.file.unlink()
        self.assertIsNone(S.load_settings(None).doc['platforms']['instagram']['start_date'])   # root unknown
        self.file.unlink()
        (self.root / '_channel' / 'channel.json').write_text('{"instagram": ', encoding='utf-8')
        self.assertIsNone(start())                       # broken channel.json
        self.file.unlink()
        (self.root / '_channel' / 'channel.json').write_text('\ufeff{"instagram": {"start": "2026-11-03"}}',
                                                             encoding='utf-8')
        self.assertEqual(start(), '2026-11-03')          # a BOM from Notepad is fine
        self.file.unlink()
        shutil.rmtree(self.root / '_channel')
        self.assertIsNone(start())                       # no channel.json

    def test_two_thread_race(self):
        """Both threads see no file and reach os.link together: one file, exactly one migrated=True."""
        real_link, both = os.link, threading.Barrier(2, timeout=10)

        def link(src, dst, *a, **kw):
            both.wait()                                  # each has its temp file and saw no publishing.json
            return real_link(src, dst, *a, **kw)
        results, errors = [], []

        def run():
            try:
                results.append(S.load_settings(self.root))
            except BaseException as e:
                errors.append(e)
        with mock.patch('os.link', link):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r.migrated for r in results), [False, True])
        self.assertEqual(results[0].doc, results[1].doc)
        self.assertEqual([r.problems for r in results], [[], []])
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ['publishing.json'])

    def test_many_threads(self):
        start, results = threading.Barrier(8, timeout=10), []

        def run():
            start.wait()
            results.append(S.load_settings(self.root).migrated)
        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual((len(results), results.count(True)), (8, 1))
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ['publishing.json'])

    def test_os_link_refuses_an_existing_file(self):
        """Spec 1.3: os.link fails with FileExistsError when the target exists (NTFS on this PC)."""
        a, b = self.work / 'a.txt', self.work / 'b.txt'
        a.write_text('a', encoding='utf-8')
        b.write_text('b', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            os.link(a, b)
        self.assertEqual(b.read_text(encoding='utf-8'), 'b')
        os.link(a, self.work / 'c.txt')
        self.assertEqual((self.work / 'c.txt').read_text(encoding='utf-8'), 'a')

    def test_no_hard_links_falls_back_to_replace(self):
        with mock.patch('os.link', side_effect=OSError('hard links are not supported here')):
            loaded = S.load_settings(self.root)
        self.assertTrue(loaded.migrated)
        self.assertEqual(loaded.problems, [])
        self.assertTrue(self.file.is_file())
        self.assertEqual(self.temps(), [])

    def test_an_existing_file_is_not_migrated(self):
        self.put(doc(auto=True))
        self.flag.write_bytes(b'')
        loaded = S.load_settings(self.root)
        self.assertFalse(loaded.migrated)
        self.assertEqual(S.effective_auto(loaded, 'instagram'), (False, 'stopped_by_file'))

    def test_paths_follow_work_when_only_work_moves(self):
        other = self.work / 'other'
        with mock.patch.object(S, 'SETTINGS', S._SETTINGS0), mock.patch.object(S, 'WORK', other):
            self.assertTrue(S.load_settings().migrated)
            self.assertEqual(S.pause_file('instagram'), other / 'instagram-paused')
        self.assertTrue((other / 'publishing.json').is_file())


class InvalidFileKept(Case):
    """Spec 4.1: an invalid file is reported, means off, and is never overwritten except by reset_settings."""
    BAD = {
        'not JSON': '{"version": 1, "rev": 1,',
        'empty': '',
        'a list': '[]',
        'version 2': json.dumps({**doc(auto=True), 'version': 2}),
        'no version': json.dumps({k: v for k, v in doc(auto=True).items() if k != 'version'}),
        'version true': json.dumps({**doc(auto=True), 'version': True}),
        'platforms is a list': json.dumps({**doc(), 'platforms': []}),
        'instagram is null': json.dumps({**doc(), 'platforms': {'instagram': None}}),
        'unknown field': json.dumps(doc(auto=True, colour='red')),
        'bad value': json.dumps(doc(auto=True, post_before='20:10')),
        'bool as a number': json.dumps(doc(auto=True, every_days=True)),
        'hard cap 11': json.dumps(doc(auto=True, hard_cap_24h=11)),
    }

    def test_never_overwritten(self):
        for name, content in self.BAD.items():
            with self.subTest(name):
                self.put(content)
                before = self.file.read_bytes()
                loaded = S.load_settings(self.root)
                self.assertTrue(loaded.problems)
                self.assertFalse(loaded.migrated)
                self.assertEqual(loaded.doc['platforms'], {'instagram': cfg(), 'youtube': cfg()})
                self.assertEqual(S.effective_auto(loaded, 'instagram'), (False, 'invalid'))
                for dry in (False, True):
                    with self.assertRaises(S.InvalidFile) as e:
                        S.save_settings('instagram', {'every_days': 2}, loaded.doc['rev'], 'dev@localhost', dry_run=dry)
                    self.assertEqual(e.exception.problems, loaded.problems)
                S.load_settings(self.root)
                self.assertEqual(self.file.read_bytes(), before)
                self.assertFalse(self.flag.exists())

    def test_problem_texts(self):
        self.put(doc(post_before='20:10'))
        self.assertEqual(S.load_settings().problems,
                         ['instagram post_before: Must be 40 minutes or more after the start, and 23:59 at the latest.'])
        self.put(doc(colour='red'))
        self.assertEqual(S.load_settings().problems, ['instagram colour: Unknown setting.'])
        self.put(doc(hard_cap_24h=11))                   # the file's own problem says what is allowed
        self.assertEqual(S.load_settings().problems, ['instagram hard_cap_24h: Choose 1 to 10, in publishing.json on the PC.'])
        self.put(self.BAD['not JSON'])
        self.assertRegex(S.load_settings().problems[0], r'^publishing\.json is not valid JSON \(.+\)\.$')
        self.put(self.BAD['version 2'])
        self.assertEqual(S.load_settings().problems, ['publishing.json: "version" must be 1.'])

    def test_reset_replaces_it(self):
        self.put({**doc(rev=7, auto=True), 'version': 2})
        out = S.reset_settings('dev@localhost', ['reset_settings'])
        self.assertEqual((out['rev'], out['updated_by'], out['platforms']), (8, 'dev@localhost', {'instagram': cfg(), 'youtube': cfg()}))
        self.assertEqual(self.saved(), out)
        loaded = S.load_settings()
        self.assertEqual(loaded.problems, [])
        self.assertEqual(S.effective_auto(loaded, 'instagram'), (False, 'off'))
        self.assertRegex(self.flag.read_text(encoding='utf-8'), r'^dev@localhost \d{4}-\d\d-\d\d \d\d:\d\d\n$')
        self.put(self.BAD['not JSON'])
        self.assertEqual(S.reset_settings('dev@localhost', ['reset_settings'])['rev'], 1)
        self.file.unlink()
        self.assertEqual(S.reset_settings('dev@localhost', ['reset_settings'])['rev'], 1)
        self.assertEqual(self.temps(), [])


class ValidFiles(Case):
    """What a valid file may hold: unknown top-level keys, missing platforms or fields, any day order, a BOM."""

    def test_spec_example(self):
        self.put(SPEC_EXAMPLE)
        loaded = S.load_settings()
        self.assertEqual((loaded.doc, loaded.problems, loaded.migrated), (SPEC_EXAMPLE, [], False))

    def test_unknown_keys_and_platforms_are_dropped_on_save(self):
        self.put({**doc(auto=True), 'note': 'hand edit', 'platforms': {'instagram': cfg(auto=True), 'tiktok': {'x': 1}}})
        loaded = S.load_settings()
        self.assertEqual(loaded.problems, [])
        self.assertNotIn('note', loaded.doc)
        self.assertEqual(list(loaded.doc['platforms']), ['instagram', 'youtube'])
        S.save_settings('instagram', {}, 1, 'dev@localhost')
        self.assertNotIn('note', self.saved())
        self.assertEqual(list(self.saved()['platforms']), ['instagram', 'youtube'])

    def test_missing_platform_and_fields_get_defaults(self):
        self.put({**doc(), 'platforms': {}})
        self.assertEqual(S.load_settings().doc['platforms'], {'instagram': cfg(), 'youtube': cfg()})
        self.put({**doc(), 'platforms': {'instagram': {'auto': True, 'days': ['fri', 'mon']}}})
        loaded = S.load_settings()
        self.assertEqual(loaded.problems, [])
        self.assertEqual(S.platform_settings(loaded, 'instagram'), cfg(auto=True, days=['mon', 'fri']))

    def test_bom_and_odd_rev(self):
        self.file.write_text('\ufeff' + json.dumps(doc(rev=5)), encoding='utf-8')
        self.assertEqual(S.load_settings().doc['rev'], 5)
        self.put({**doc(), 'rev': 'x'})                  # an unreadable rev counts as 0
        loaded = S.load_settings()
        self.assertEqual((loaded.problems, loaded.doc['rev']), ([], 0))
        self.assertEqual(S.save_settings('instagram', {}, 0, 'dev@localhost')['rev'], 1)

    def test_platform_settings_is_a_copy(self):
        self.put(doc())
        loaded = S.load_settings()
        S.platform_settings(loaded, 'instagram')['days'].append('funday')
        self.assertEqual(S.platform_settings(loaded, 'instagram'), cfg())
        self.assertEqual(S.platform_settings(loaded, 'tiktok'), cfg())


class Save(Case):
    """save_settings: merge, validate, rev, stamps, the stored form, stale and concurrent saves, the pause file."""

    def test_round_trip(self):
        self.put(doc(auto=True))
        out = S.save_settings('instagram', {'every_days': 2, 'days': ['fri', 'mon'], 'min_gap_hours': 20.5,
                                            'post_after': '19:00', 'post_before': '22:00', 'start_date': ''},
                              1, 'dev@localhost')
        self.assertEqual((out['version'], out['rev'], out['updated_by']), (1, 2, 'dev@localhost'))
        self.assertRegex(out['updated'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(out['platforms']['instagram'],
                         cfg(auto=True, every_days=2, days=['mon', 'fri'], min_gap_hours=20.5, post_after='19:00',
                             post_before='22:00', start_date=None))
        self.assertEqual(self.saved(), out)
        self.assertEqual(S.load_settings().doc, out)
        self.assertEqual(list(out['platforms']['instagram']), list(S.FIELDS))

    def test_whole_hours_are_stored_as_int(self):
        self.put(doc(auto=True))
        out = S.save_settings('instagram', {'min_gap_hours': 22.0}, 1, 'dev@localhost')
        self.assertIs(type(out['platforms']['instagram']['min_gap_hours']), int)

    def test_F21_stale_turn_off_still_stops(self):
        """F21: turning automatic posting off is never refused as stale; any other change still is."""
        self.put(doc(rev=4, auto=True))
        out = S.save_settings('instagram', {'auto': False}, 3, 'owner')
        self.assertEqual((out['rev'], out['platforms']['instagram']['auto']), (5, False))
        self.assertTrue(self.flag.exists())
        self.assertEqual(S.effective_auto(S.load_settings(), 'instagram'), (False, 'off'))
        for patch in ({'auto': False, 'every_days': 2}, {'auto': True}, {'every_days': 2}):
            with self.subTest(patch=patch), self.assertRaises(S.StaleSettings):
                S.save_settings('instagram', patch, 3, 'owner', ['auto_on'])

    def test_stale_rev(self):
        self.put(doc(rev=4, auto=True))
        before = self.file.read_bytes()
        for rev in (3, 5, '4', True, None):
            with self.subTest(rev=rev), self.assertRaises(S.StaleSettings) as e:
                S.save_settings('instagram', {'every_days': 2}, rev, 'dev@localhost')
            self.assertEqual(e.exception.doc, S.load_settings().doc)
        self.assertEqual(self.file.read_bytes(), before)

    def test_unknown_platform(self):
        self.put(doc())
        with self.assertRaises(KeyError):
            S.save_settings('tiktok', {}, 1, 'dev@localhost')

    def test_by_and_ack_swapped(self):
        """publish.save_settings(..., ack, by) and publish.reset_settings(ack, by) take them the other way round."""
        self.put(doc(auto=True))
        before = self.file.read_bytes()
        with self.assertRaises(TypeError):
            S.save_settings('instagram', {'auto': False}, 1, ['auto_on'], 'dev@localhost')
        with self.assertRaises(TypeError):
            S.reset_settings(['reset_settings'], 'dev@localhost')
        self.assertEqual(self.file.read_bytes(), before)
        self.assertFalse(self.flag.exists())

    def test_bad_values(self):
        self.put(doc(post_before='21:00'))
        msg = 'Must be 40 minutes or more after the start, and 23:59 at the latest.'
        for patch, fields in (({'post_after': '20:45'}, {'post_before': msg}),          # merged with the file's end
                              ({'post_after': '23:00', 'post_before': '23:15'}, {'post_before': msg}),
                              ({'colour': 'red'}, {'colour': 'Unknown setting.'}),
                              ({'days': []}, {'days': 'Choose at least one day.'})):
            with self.subTest(patch=patch), self.assertRaises(S.SettingsError) as e:
                S.save_settings('instagram', patch, 1, 'dev@localhost')
            self.assertEqual((e.exception.code, e.exception.fields), ('invalid', fields))
        with self.assertRaises(S.SettingsError):
            S.save_settings('instagram', ['auto'], 1, 'dev@localhost')

    def test_concurrent_saves_with_the_same_rev(self):
        self.put(doc(auto=True))
        start, results = threading.Barrier(2, timeout=10), []

        def run(n):
            start.wait()
            try:
                S.save_settings('instagram', {'every_days': n}, 1, f'user{n}')
                results.append('saved')
            except S.StaleSettings:
                results.append('stale')
        threads = [threading.Thread(target=run, args=(n,)) for n in (2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(sorted(results), ['saved', 'stale'])
        self.assertEqual(self.saved()['rev'], 2)

    def test_pause_file_follows_auto(self):
        self.put(doc(auto=True))
        S.save_settings('instagram', {'auto': False}, 1, 'dev@localhost')
        text = self.flag.read_text(encoding='utf-8')
        self.assertRegex(text, r'^dev@localhost \d{4}-\d\d-\d\d \d\d:\d\d\n$')
        S.save_settings('instagram', {'every_days': 2}, 2, 'someone-else')   # still off: the old text stays
        self.assertEqual(self.flag.read_text(encoding='utf-8'), text)
        S.save_settings('instagram', {'auto': True}, 3, 'dev@localhost', ['auto_on'])
        self.assertFalse(self.flag.exists())

    def test_turning_off_stops_even_if_the_write_fails(self):
        self.put(doc(auto=True))
        with mock.patch('os.replace', side_effect=PermissionError(13, 'in use')), \
                mock.patch.object(S, 'time', NO_SLEEP), self.assertRaises(PermissionError):
            S.save_settings('instagram', {'auto': False}, 1, 'dev@localhost')
        self.assertEqual(S.effective_auto(S.load_settings(), 'instagram'), (False, 'stopped_by_file'))


class AtomicWrite(Case):
    """Writes go to a temp file in the same folder, then os.replace, retried on PermissionError 5 times over ~3 s."""

    def setUp(self):
        super().setUp()
        self.waits = []
        patch = mock.patch.object(S, 'time', types.SimpleNamespace(sleep=self.waits.append))
        patch.start()
        self.addCleanup(patch.stop)
        self.put(doc(auto=True))

    def flaky(self, failures):
        real, calls = os.replace, []

        def replace(src, dst):
            calls.append((Path(src), Path(dst)))
            if len(calls) <= failures:
                raise PermissionError(13, 'The process cannot access the file')
            return real(src, dst)
        return replace, calls

    def test_retried_until_it_works(self):
        replace, calls = self.flaky(3)
        with mock.patch('os.replace', replace):
            out = S.save_settings('instagram', {'every_days': 2}, 1, 'dev@localhost')
        self.assertEqual(len(calls), 4)
        for src, dst in calls:
            self.assertEqual((src.parent, dst), (self.work, self.file))
            self.assertTrue(src.name.startswith('publishing.json.tmp-'))
        self.assertEqual(self.saved(), out)
        self.assertEqual(len(self.waits), 3)
        self.assertEqual(self.temps(), [])

    def test_gives_up_after_five_retries(self):
        before = self.file.read_bytes()
        replace, calls = self.flaky(99)
        with mock.patch('os.replace', replace), self.assertRaises(PermissionError):
            S.save_settings('instagram', {'every_days': 2}, 1, 'dev@localhost')
        self.assertEqual(len(calls), 6)
        self.assertAlmostEqual(sum(self.waits), 3.0, delta=0.5)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.temps(), [])

    def test_reset_writes_the_same_way(self):
        replace, calls = self.flaky(1)
        with mock.patch('os.replace', replace):
            S.reset_settings('dev@localhost', ['reset_settings'])
        self.assertEqual((len(calls), calls[-1][1]), (2, self.file))
        self.assertEqual(self.saved()['rev'], 2)


class PauseFile(Case):
    """pause_file and pause_info: the emergency brake made by the dashboard, by hand or by PowerShell."""

    def test_info(self):
        self.assertEqual(S.pause_file('instagram'), self.work / 'instagram-paused')
        self.assertIsNone(S.pause_info('instagram'))
        self.flag.write_text('dev@localhost 2026-11-04 21:27\n', encoding='utf-8')
        t = datetime(2026, 11, 4, 18, 27, 51, tzinfo=UTC).timestamp()
        os.utime(self.flag, (t, t))
        self.assertEqual(S.pause_info('instagram'), {'since': '2026-11-04T18:27:51Z', 'by': 'dev@localhost'})
        self.flag.write_bytes(b'')                       # type nul > instagram-paused
        self.assertIsNone(S.pause_info('instagram')['by'])
        self.flag.write_text('dev@localhost 2026-11-04 21:27', encoding='utf-16')
        self.assertEqual(S.pause_info('instagram')['by'], 'dev@localhost')


# ---------- timing rules ----------

class NextAutoTable(Case):
    """Spec 5.2: the 13 next_auto rows. Defaults: daily, all days, 20:00-23:59, gap 20, cap 3."""
    ROWS = (                # (row, change from defaults, history, now, expected at and slot); all Istanbul times
        (1, {}, ['2026-11-04 16:46'], '2026-11-04 22:31', ('2026-11-05 20:00', '2026-11-05')),
        (2, {}, ['2026-11-04 16:46'], '2026-11-05 12:00', ('2026-11-05 20:00', '2026-11-05')),
        (3, {}, ['2026-11-05 01:00'], '2026-11-05 12:00', ('2026-11-06 20:00', '2026-11-06')),
        (4, {}, ['2026-11-04 23:30'], '2026-11-05 18:00', ('2026-11-05 20:00', '2026-11-05')),
        (5, {'min_gap_hours': 22}, ['2026-11-04 23:30'], '2026-11-05 18:00', ('2026-11-05 21:20', '2026-11-05')),  # F34
        (6, {'every_days': 2}, ['2026-11-04 16:46'], '2026-11-05 12:00', ('2026-11-06 20:00', '2026-11-06')),
        (7, {'days': ['mon', 'wed', 'fri']}, ['2026-11-04 16:46'], '2026-11-04 22:31', ('2026-11-06 20:00', '2026-11-06')),
        (8, {'start_date': '2026-11-10'}, [], '2026-11-04 22:31', ('2026-11-10 20:00', '2026-11-10')),
        (9, {'post_before': '21:00'}, ['2026-11-03 18:07'], '2026-11-04 21:30', ('2026-11-05 20:00', '2026-11-05')),
        (10, {'min_gap_hours': 1, 'hard_cap_24h': 3}, ['2026-11-04 21:00', '2026-11-04 22:00', '2026-11-04 23:00'],
         '2026-11-05 19:00', ('2026-11-05 21:00', '2026-11-05')),
    )

    def test_rows_1_to_10(self):
        for n, change, history, now, (at, slot) in self.ROWS:
            with self.subTest(row=n):
                c, h = cfg(**change), [P(x) for x in history]
                got = S.next_auto(c, h, T(now), 6)
                self.assertEqual(got, (T(at), slot, 'scheduled'))
                self.assertEqual(S.slot_open(c, h, got[0]), (True, slot))
                self.assertFalse(S.slot_open(c, h, got[0] - timedelta(minutes=1))[0])   # it is the earliest

    def test_row_3_reason(self):
        self.assertEqual(S.slot_open(cfg(), [P('2026-11-05 01:00')], T('2026-11-05 21:00')), (False, 'every_days'))

    def test_row_11_nothing_waiting(self):
        for history, now in (([P('2026-11-04 16:46')], '2026-11-04 22:31'), ([], '2026-11-05 20:30')):
            for waiting in (0, None):
                self.assertEqual(S.next_auto(cfg(), history, T(now), waiting), (None, None, 'empty'))

    def test_row_12_blocked(self):
        for reason in ('invalid', 'off', 'stopped_by_file', 'suspended', 'breaker', 'unresolved'):
            for waiting in (6, 0):
                self.assertEqual(S.next_auto(cfg(), [P('2026-11-04 16:46')], T('2026-11-04 22:31'), waiting, reason),
                                 (None, None, reason))

    def test_row_13_no_slot(self):
        self.assertEqual(S.next_auto(cfg(days=['sun'], start_date='2026-12-31'), [], T('2026-11-04 22:31'), 6),
                         (None, None, 'no_slot'))

    def test_search_is_today_plus_15_days(self):
        now = T('2026-11-04 22:31')
        self.assertEqual(S.next_auto(cfg(start_date='2026-11-19'), [], now, 1)[:2], (T('2026-11-19 20:00'), '2026-11-19'))
        self.assertEqual(S.next_auto(cfg(start_date='2026-11-20'), [], now, 1), (None, None, 'no_slot'))

    def test_now_inside_the_window(self):
        self.assertEqual(S.next_auto(cfg(), [], T('2026-11-05 21:10'), 1), (T('2026-11-05 21:10'), '2026-11-05', 'scheduled'))


class SlotCheck(Case):
    """slot_check gives the first failing rule, in the order of spec 5.2."""

    def test_order(self):
        rows = (   # (settings, history, t, reason)
            (cfg(days=['fri']), ['2026-11-04 16:46'], '2026-11-05 10:00', 'day'),          # Thu, also outside the window
            (cfg(start_date='2026-11-06'), ['2026-11-04 16:46'], '2026-11-05 10:00', 'start'),
            (cfg(), ['2026-11-04 16:46'], '2026-11-05 10:00', 'window'),                     # also under the gap
            (cfg(every_days=2), ['2026-11-04 16:46'], '2026-11-05 20:00', 'every_days'),
            (cfg(min_gap_hours=22), ['2026-11-04 23:30'], '2026-11-05 20:00', 'gap'),
            (cfg(min_gap_hours=22), ['2026-11-04 23:30'], '2026-11-05 21:19', 'gap'),
            (cfg(min_gap_hours=22), ['2026-11-04 23:30'], '2026-11-05 21:20', None),        # F34: one run early
            (cfg(min_gap_hours=1, hard_cap_24h=2), ['2026-11-04 21:00', '2026-11-04 23:00'], '2026-11-05 20:00', 'cap'),
            (cfg(), ['2026-11-04 16:46'], '2026-11-05 20:00', None),
            (cfg(post_before='21:00'), ['2026-11-04 16:46'], '2026-11-05 21:00', None),     # the end minute is inside
            (cfg(post_before='21:00'), ['2026-11-04 16:46'], '2026-11-05 21:01', 'window'),
            (cfg(), [], '2026-11-05 19:59', 'window'),
        )
        for c, history, t, why in rows:
            with self.subTest(t=t, why=why):
                self.assertEqual(S.slot_check(c, [P(x) for x in history], T(t)), why)

    def test_slot_open(self):
        h = [P('2026-11-04 16:46')]
        self.assertEqual(S.slot_open(cfg(), h, T('2026-11-05 20:00')), (True, '2026-11-05'))
        self.assertEqual(S.slot_open(cfg(), h, T('2026-11-05 23:59')), (True, '2026-11-05'))
        self.assertEqual(S.slot_open(cfg(), h, T('2026-11-05 12:00')), (False, 'window'))
        self.assertEqual(S.slot_open(cfg(), h, T('2026-11-04 22:31')), (False, 'every_days'))


class CapFreeAt(Case):
    """posts_in_24h, cap_free_at and last_post."""

    def test_cap(self):
        now = T('2026-11-05 19:00')
        h = [P('2026-11-04 21:00'), P('2026-11-04 22:00'), P('2026-11-04 23:00')]
        self.assertEqual(S.cap_free_at(cfg(), h, now), T('2026-11-05 21:00'))                    # oldest + 24 h
        self.assertIsNone(S.cap_free_at(cfg(), h[:2], now))
        self.assertEqual(S.cap_free_at(cfg(hard_cap_24h=2), h, now), T('2026-11-05 22:00'))      # 2 must leave
        self.assertEqual(S.cap_free_at(cfg(hard_cap_24h=1), h, now), T('2026-11-05 23:00'))

    def test_window_is_open_on_the_left(self):
        now = T('2026-11-05 19:00')
        h = [P('2026-11-04 19:00'), P('2026-11-04 21:00'), P('2026-11-04 22:00'), P('2026-11-05 19:00')]
        self.assertEqual(S.posts_in_24h(h, now), 3)      # exactly 24 h ago is outside; now itself is inside
        self.assertEqual(S.cap_free_at(cfg(), h, now), T('2026-11-05 21:00'))
        self.assertEqual(S.posts_in_24h(h, T('2026-11-05 18:59')), 4)      # F18: a post after 'now' counts too
        self.assertEqual(S.last_post(h), h[-1])
        self.assertIsNone(S.last_post([]))

    def test_F18_a_post_later_than_now_still_counts(self):
        """A clock that is a little behind (or a post time from Meta's clock) never frees a slot of the cap."""
        h = [P('2026-11-05 19:00')]
        now = T('2026-11-05 18:59')
        self.assertEqual(S.posts_in_24h(h, now), 1)
        self.assertEqual(S.cap_free_at(cfg(hard_cap_24h=1), h, now), T('2026-11-06 19:00'))


class ForecastExample(Case):
    """Spec 5.2: defaults, history row 1, 6 waiting keys -> Thu 5 .. Tue 10 Nov at 20:00; runway 2026-11-10."""
    KEYS = [f'new/Output/new-short-{n}.mp4' for n in (6, 7, 8, 9)] + [
        'Um etwas bitten/Output/um-etwas-bitten-video.mp4', 'Sich interessieren für/Output/sich-video.mp4']

    def test_example(self):
        rows = S.forecast(cfg(), [P('2026-11-04 16:46')], T('2026-11-04 22:31'), self.KEYS)
        self.assertEqual(rows, [{'key': k, 'at': T(f'2026-11-{d:02d} 20:00'), 'slot': f'2026-11-{d:02d}'}
                                for k, d in zip(self.KEYS, range(5, 11))])
        self.assertEqual(S.runway(rows), '2026-11-10')
        self.assertEqual(S.fmt_day(rows[-1]['at']), 'Tue 10 Nov')

    def test_F36_forecast_agrees_with_next_auto_at_the_horizon(self):
        """F36: the forecast stops on the same Istanbul dates next_auto searches (today + SEARCH_DAYS)."""
        now = T('2026-11-05 06:10')
        for start in ('2026-11-18', '2026-11-19', '2026-11-20', '2026-11-21'):
            with self.subTest(start=start):
                c = cfg(start_date=start)
                at, slot, why = S.next_auto(c, [], now, 2)
                rows = S.forecast(c, [], now, ['a', 'b'])
                self.assertEqual(bool(rows), why == 'scheduled')
                if rows:
                    self.assertEqual((rows[0]['at'], rows[0]['slot']), (at, slot))

    def test_stops(self):
        h, now = [P('2026-11-04 16:46')], T('2026-11-04 22:31')
        self.assertEqual([r['slot'] for r in S.forecast(cfg(), h, now, self.KEYS, limit_days=3)],
                         ['2026-11-05', '2026-11-06', '2026-11-07'])
        self.assertEqual(S.forecast(cfg(start_date='2026-12-31'), h, now, self.KEYS), [])   # no slot
        self.assertEqual(S.forecast(cfg(), h, now, []), [])
        self.assertIsNone(S.runway([]))
        self.assertEqual([r['slot'] for r in S.forecast(cfg(every_days=2), h, now, self.KEYS[:3])],
                         ['2026-11-06', '2026-11-08', '2026-11-10'])
        self.assertEqual(len(h), 1)                      # the caller's history is not changed


class GapDrift(Case):
    """F34: with a gap of 24 h x every_days, posts that go live a minute or two after their watcher run do not
    drift one run later each time (and lose days); the forecast dates match."""

    def simulate(self, c, days=14):
        hist, posted = [P('2026-11-04 16:46')], []
        rows = S.forecast(c, list(hist), T('2026-11-05 12:00'), [str(n) for n in range(days)], limit_days=days)
        t, end = T('2026-11-05 00:00') + timedelta(seconds=3), T('2026-11-05 00:00') + timedelta(days=days)
        while t < end:
            for check in (t, t + timedelta(seconds=8)):
                if S.slot_open(c, hist, check)[0]:
                    hist.append(S.Post(at=check + timedelta(seconds=90), by='auto'))
                    posted.append(check)
            t += timedelta(minutes=10)
        return posted, [r['slot'] for r in rows if r['at'] < end]

    def test_F34_no_drift(self):
        for change in ({'min_gap_hours': 24}, {'every_days': 2, 'min_gap_hours': 48, 'post_before': '21:00'},
                       {'min_gap_hours': 24, 'post_before': '20:40'}):
            with self.subTest(change=change):
                posted, slots = self.simulate(cfg(**change))
                self.assertGreaterEqual(len(posted), 7)
                for at in posted:
                    self.assertTrue('20:00' <= f'{S.local(at):%H:%M}' <= '20:10', S.fmt(at))
                self.assertEqual([S.local_date(at) for at in posted], slots)

    def test_F34_summary_says_a_long_gap_posts_less_often(self):
        self.assertEqual(S.summary(cfg(min_gap_hours=30)), 'One Reel a day after 20:00, at least 30 h apart, '
                                                          'so less often than that')
        self.assertEqual(S.summary(cfg(min_gap_hours=24)), 'One Reel a day after 20:00, at least 24 h apart')
        self.assertEqual(S.summary(cfg(every_days=2, min_gap_hours=48)), 'One Reel every 2 days after 20:00, at least '
                                                                         '48 h apart')


class SpacingChecks(Case):
    """Spec 5.2/5.3: manual-post timing checks, in order hard_cap, gap, same_day."""

    def test_example(self):
        checks = S.spacing_checks(cfg(), [P('2026-11-04 16:46')], T('2026-11-04 22:31'))
        self.assertEqual(checks, [
            S.Check('gap', 'warn', 'The last Reel went out 5 h 45 min ago. The rule is at least 20 h between Reels.',
                    {'last': '2026-11-04T13:46:00Z', 'hours': 5.8}),
            S.Check('same_day', 'warn', 'A Reel already went out today at 16:46.', {'at': '2026-11-04T13:46:00Z'})])
        gap = S.spacing_checks(cfg(), [P('2026-11-04 16:46')], T('2026-11-04 22:40'))[0]   # the spec 8.2 example
        self.assertEqual((gap.text.split('. ')[0], gap.data['hours']), ('The last Reel went out 5 h 54 min ago', 5.9))

    def test_hard_cap_comes_first(self):
        h = [P('2026-11-03 23:00'), P('2026-11-04 10:00'), P('2026-11-04 16:46')]
        checks = S.spacing_checks(cfg(), h, T('2026-11-04 22:31'))
        self.assertEqual([(c.code, c.level) for c in checks], [('hard_cap', 'block'), ('gap', 'warn'), ('same_day', 'warn')])
        self.assertEqual(checks[0].text, '3 Reels in the last 24 hours: that is the limit. Next possible Wed 4 Nov 23:00.')
        self.assertEqual(checks[0].data, {'next_possible': '2026-11-04T20:00:00Z'})

    def test_ignores_days_window_every_days_and_start(self):
        h, now = [P('2026-11-04 16:46')], T('2026-11-04 22:31')
        other = cfg(days=['sun'], post_after='08:00', post_before='09:00', every_days=7, start_date='2026-12-31')
        self.assertEqual(S.spacing_checks(other, h, now), S.spacing_checks(cfg(), h, now))

    def test_each_on_its_own(self):
        self.assertEqual([c.code for c in S.spacing_checks(cfg(), [P('2026-11-03 23:00')], T('2026-11-04 12:00'))],
                         ['gap'])
        self.assertEqual([c.code for c in S.spacing_checks(cfg(min_gap_hours=1), [P('2026-11-04 08:00')],
                                                            T('2026-11-04 12:00'))], ['same_day'])
        self.assertEqual(S.spacing_checks(cfg(), [P('2026-11-03 12:00')], T('2026-11-04 12:00')), [])
        self.assertEqual(S.spacing_checks(cfg(), [], T('2026-11-04 12:00')), [])
        gap = S.spacing_checks(cfg(min_gap_hours=20.5), [P('2026-11-04 08:00')], T('2026-11-04 12:00'))[0]
        self.assertEqual(gap.text, 'The last Reel went out 4 h 0 min ago. The rule is at least 20.5 h between Reels.')


class Pacific(Case):
    """pacific_offset and pacific_midnight_after around the 2026 clock changes (8 Mar and 1 Nov)."""

    def test_offset(self):
        U = lambda *a: datetime(*a, tzinfo=UTC)
        for t, hours in ((U(2026, 3, 8, 9, 59), -8), (U(2026, 3, 8, 10), -7), (U(2026, 11, 1, 8, 59), -7),
                         (U(2026, 11, 1, 9), -8), (U(2026, 1, 15), -8), (U(2026, 7, 1), -7)):
            self.assertEqual(S.pacific_offset(t), timedelta(hours=hours), t)

    def test_midnight_after(self):
        U = lambda *a: datetime(*a, tzinfo=UTC)
        for t, want in ((U(2026, 3, 7, 12), U(2026, 3, 8, 8)),      # Sat 04:00 PST -> Sun 00:00 PST
                        (U(2026, 3, 8, 9), U(2026, 3, 9, 7)),       # Sun 01:00 PST (before the change) -> Mon 00:00 PDT
                        (U(2026, 3, 8, 12), U(2026, 3, 9, 7)),
                        (U(2026, 10, 31, 12), U(2026, 11, 1, 7)),   # Sat 05:00 PDT -> Sun 00:00 PDT
                        (U(2026, 11, 1, 8), U(2026, 11, 2, 8)),     # Sun 01:00 PDT (before the change) -> Mon 00:00 PST
                        (U(2026, 11, 1, 12), U(2026, 11, 2, 8)),
                        (U(2026, 11, 2, 8), U(2026, 11, 3, 8))):    # exactly midnight -> the next one
            with self.subTest(t=t):
                got = S.pacific_midnight_after(t)
                self.assertEqual(got, want)
                self.assertEqual(got.utcoffset(), timedelta(0))
        self.assertEqual(S.local(S.pacific_midnight_after(U(2026, 11, 4, 19, 31))).hour, 11)    # winter: 11:00 Istanbul
        self.assertEqual(S.local(S.pacific_midnight_after(U(2026, 7, 1, 12))).hour, 10)        # summer: 10:00


class TimeHelpers(Case):
    """Istanbul is a fixed UTC+3; fmt has no leading zero on the day."""

    def test_fmt(self):
        self.assertEqual(S.fmt(T('2026-11-05 20:00')), 'Thu 5 Nov 20:00')
        self.assertEqual(S.fmt(datetime(2026, 11, 5, 17, 0, tzinfo=UTC)), 'Thu 5 Nov 20:00')
        self.assertEqual(S.fmt(T('2026-12-01 09:05')), 'Tue 1 Dec 09:05')
        self.assertEqual(S.fmt_day(T('2026-11-10 20:00')), 'Tue 10 Nov')
        self.assertEqual((S.fmt(None), S.fmt_day(None)), ('', ''))

    def test_istanbul(self):
        self.assertEqual(S.ISTANBUL.utcoffset(None), timedelta(hours=3))
        self.assertEqual(S.local(datetime(2026, 7, 1, 12, tzinfo=UTC)).hour, 15)     # no summer time
        self.assertEqual(S.local(datetime(2026, 7, 1, 12)).hour, 15)                 # a naive time counts as UTC
        self.assertEqual(S.local_date(datetime(2026, 11, 4, 20, 59, 59, tzinfo=UTC)), '2026-11-04')
        self.assertEqual(S.local_date(datetime(2026, 11, 4, 21, 0, tzinfo=UTC)), '2026-11-05')
        at = S.at_local('2026-11-05', '20:00')
        self.assertEqual((at, at.utcoffset()), (datetime(2026, 11, 5, 17, 0, tzinfo=UTC), timedelta(0)))
        self.assertEqual(S.iso(at), '2026-11-05T17:00:00Z')


class Summary(Case):
    def test_examples(self):
        self.assertEqual(S.summary(cfg()), 'One Reel a day after 20:00, at least 20 h apart')
        self.assertEqual(S.summary(cfg(every_days=2, days=['mon', 'wed', 'fri'], post_before='23:00')),
                         'One Reel every 2 days (Mon, Wed, Fri) between 20:00 and 23:00, at least 20 h apart')
        self.assertEqual(S.summary(cfg(min_gap_hours=20.5, days=['fri', 'mon']), noun='video'),
                         'One video a day (Mon, Fri) after 20:00, at least 20.5 h apart')


class Module(Case):
    """The constants of spec 7.1, and schedule.py imports only the standard library."""

    def test_constants(self):
        self.assertEqual(S.DEFAULTS, cfg())
        self.assertEqual(tuple(S.DEFAULTS), S.FIELDS)
        self.assertEqual(S.DAYS, tuple(ALL_DAYS))
        self.assertEqual((S.KNOWN, S.SEARCH_DAYS), (('instagram', 'youtube'), 15))
        self.assertEqual(S.ACKS, ('auto_on', 'gap_under_20', 'resume_auto', 'reset_settings'))
        self.assertEqual(list(S.PRESETS), ['every_evening', 'every_other_day', 'weekdays', 'mon_wed_fri', 'three_a_day'])
        for p in S.PRESETS.values():
            self.assertTrue(set(p) - {'label'} <= set(S.FIELDS))

    def test_standard_library_only(self):                          # paths.py (shared folders) is standard library only too
        for f in (S.__file__, Path(S.__file__).with_name('paths.py')):
            tree = ast.parse(Path(f).read_text(encoding='utf-8'))
            names = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            names |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            self.assertLessEqual(names, set(sys.stdlib_module_names) | {'__future__', 'paths'}, f)


if __name__ == '__main__':
    unittest.main()


class PerDay(unittest.TestCase):
    """per_day (2026-11-05): several automatic posts on one day, each min_gap_hours apart; manual posts warn
    'same_day' only once per_day went out today. The hard cap (in publishing.json) still blocks."""
    three = staticmethod(lambda **c: cfg(**{'per_day': 3, 'post_after': '12:00', 'min_gap_hours': 4, 'hard_cap_24h': 6, **c}))

    def test_slots_on_one_day(self):
        h = [P('2026-11-04 12:00')]
        self.assertIsNone(S.slot_check(self.three(), h, T('2026-11-04 16:00')))
        self.assertEqual(S.slot_check(self.three(), h, T('2026-11-04 14:00')), 'gap')
        h.append(P('2026-11-04 16:00'))
        self.assertIsNone(S.slot_check(self.three(), h, T('2026-11-04 20:00')))
        h.append(P('2026-11-04 20:00'))
        self.assertEqual(S.slot_check(self.three(), h, T('2026-11-05 00:30')), 'window')
        self.assertEqual(S.slot_check(self.three(post_after='00:00'), h,
                                      T('2026-11-04 23:59')), 'every_days')            # 3 out today: the day is full
        self.assertEqual(S.slot_check(cfg(), [P('2026-11-04 12:00')], T('2026-11-04 23:00')), 'every_days')   # 1 a day, as before

    def test_every_days_still_counts_between_days(self):
        h = [P('2026-11-04 12:00')]
        self.assertEqual(S.slot_check(self.three(every_days=2), h, T('2026-11-05 12:00')), 'every_days')
        self.assertIsNone(S.slot_check(self.three(every_days=2), h, T('2026-11-04 16:00')))   # same day: room left

    def test_forecast_three_a_day(self):
        rows = S.forecast(self.three(), [], T('2026-11-04 09:00'), ['a', 'b', 'c', 'd'])
        self.assertEqual([S.local(r['at']).strftime('%d %H:%M') for r in rows], ['04 12:00', '04 16:00', '04 20:00', '05 12:00'])   # the full gap, never AUTO_SLACK early

    def test_forecast_never_closer_than_the_gap(self):
        """Item 15: every shown pair of rows is at least min_gap_hours apart; the first row is next_auto's time and
        next_auto itself still allows AUTO_SLACK."""
        now = T('2026-11-04 09:00')
        for c in (self.three(), self.three(min_gap_hours=5), cfg(), cfg(min_gap_hours=26), cfg(every_days=2)):
            with self.subTest(c=c):
                rows = S.forecast(c, [], now, [str(n) for n in range(8)])
                self.assertGreater(len(rows), 2)
                gaps = [(b['at'] - a['at']) / timedelta(hours=1) for a, b in zip(rows, rows[1:])]
                self.assertTrue(all(g >= c['min_gap_hours'] for g in gaps), gaps)
                self.assertEqual(rows[0]['at'], S.next_auto(c, [], now, 1)[0])
                for r in rows:
                    self.assertIsNone(S.slot_check(c, [], r['at']))
        h = [P('2026-11-04 12:00')]
        self.assertIsNone(S.slot_check(self.three(), h, T('2026-11-04 15:50')))         # next_auto/slot_check unchanged

    def test_manual_warnings(self):
        h = [P('2026-11-04 12:00'), P('2026-11-04 16:00')]
        codes = lambda c, now: [x.code for x in S.spacing_checks(c, h, T(now))]
        self.assertEqual(codes(self.three(), '2026-11-04 20:30'), [])                 # 2 of 3, 4 h apart: fine
        self.assertEqual(codes(self.three(), '2026-11-04 18:00'), ['gap'])
        self.assertEqual(codes(self.three(per_day=2), '2026-11-04 20:30'), ['same_day'])
        text = S.spacing_checks(self.three(per_day=2), h, T('2026-11-04 20:30'))[0].text
        self.assertEqual(text, '2 Reels already went out today (the last at 16:00); the setting is 2 a day.')

    def test_hard_cap_blocks_manual_posts(self):
        h = [P(f'2026-11-04 {x}:00') for x in (8, 10, 12, 14, 16, 18)]
        self.assertEqual(S.spacing_checks(self.three(), h, T('2026-11-04 20:00'))[0].code, 'hard_cap')
        self.assertNotIn('hard_cap', [c.code for c in S.spacing_checks(self.three(), h[1:], T('2026-11-04 20:00'))])

    def test_validation_and_summary(self):
        self.assertEqual(S.validate(self.three()), {})
        for bad in (0, 6, 1.5, True, None):
            self.assertEqual(S.validate(cfg(per_day=bad)).get('per_day'), 'Choose 1 to 5 a day.')
        self.assertEqual(S.summary(self.three()), 'Up to 3 Reels a day after 12:00, at least 4 h apart')
        self.assertEqual(S.summary(cfg(), 'video'), 'One video a day after 20:00, at least 20 h apart')
