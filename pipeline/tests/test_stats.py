"""stats.py and youtube.save_token (spec 5.6, 5.9, 5.10, 6.2, 7.7, 8.3; safety rules S17 and S18 of spec 10).
Offline: YouTube is fixtures.fake_youtube_api, Instagram a FakePlatform on a fixtures.make_root() root with made-up
media; time is injected through now= (publish._utcnow follows it, for the breaker and the probe cache)."""
from . import fixtures, guard

import ast, json, os, shutil, sqlite3, time, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import platform_fake as F
import publish as P
import schedule as S
import stats
import youtube as Y

IG = 'instagram'
UTC = timezone.utc
T0 = datetime(2026, 11, 4, 19, 40, tzinfo=UTC)          # Wed 4 Nov 22:40 Istanbul (the 8.3 example's "now")
MIN, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
SOURCES = ('youtube', 'instagram', 'instagram_limit', 'instagram_permissions')
KEYS_8_3 = ['now', 'sources', 'tiles', 'youtube_videos', 'reels', 'pairs', 'insights', 'limit']
TILE_KEYS = ['id', 'label', 'value', 'd24', 'd7', 'spark', 'at', 'stale']


def T(text: str) -> datetime:
    """'2026-11-05 11:00' Istanbul as aware UTC."""
    return datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=fixtures.IST).astimezone(UTC)


def media(n: int = 5) -> list[dict]:
    """The fake account's newest media, newest first: the Reels of new-short-n..1 with the ids and permalinks of
    the fixture's Drive records; likes 10 + k and comments k % 2 (made up)."""
    return [{'id': f'FAKE_MEDIA_{k}', 'at': S.iso(fixtures.NEW_POSTED[k - 1]), 'product': 'REELS',
             'likes': 10 + k, 'comments': k % 2, 'permalink': f'https://www.instagram.com/reel/FAKE{k}/',
             'caption': f'Hook line {k}\n\nMore text.\n#almanca'} for k in range(n, 0, -1)]


def put(platform: str, oid: str, metric: str, at: datetime, value: int, kind: str | None = None) -> None:
    """One sample (and its object) written straight into stats.db."""
    with stats._Con(write=True) as con:
        stats._object(con, platform, oid, kind or ('account' if oid == '_account' else 'reel'), S.iso(at))
        stats._sample(con, platform, oid, metric, S.iso(at), value)


class Broken:
    """A YouTube client whose every request fails (not a quota error)."""
    def videos(self):
        raise RuntimeError('made-up YouTube failure')
    channels = videos


class Case(unittest.TestCase):
    """A fresh WORK and HOME, the fixture root, a FakePlatform with five made-up Reels as the registry, the fake
    YouTube (fixtures.YT_VIDEOS, YT_CHANNEL) and publish's clock at self.now (T0)."""

    def setUp(self):
        guard.reset()
        self.root = fixtures.make_root(guard.TMP / 'stats-root')
        self.fake = fixtures.fake_platform(self.root)
        self.fake.media = media()
        P.set_registry({IG: self.fake})
        self.yt = fixtures.fake_youtube_api(fixtures.YT_VIDEOS, fixtures.YT_CHANNEL)
        self.now = T0
        clock = mock.patch.object(P, '_utcnow', lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def collect(self, at: datetime | None = None, *, root=True, yt=True) -> dict:
        """stats.collect at `at` (default self.now, which follows) with the fixture root and the fake YouTube.
        root=None: no Drive; yt=None: the default youtube.api() (blocked by the guard)."""
        if at is not None:
            self.now = at
        return stats.collect(self.root if root is True else root, self.now, youtube=self.yt if yt is True else yt)

    def rows(self, sql: str, *args) -> list[dict]:
        with stats._Con() as con:
            return [dict(r) for r in con.execute(sql, args)]

    def series(self, platform: str, oid: str, metric: str) -> dict:
        """{at: value} of one series, oldest first."""
        return {r['at']: r['value'] for r in self.rows('SELECT at, value FROM samples WHERE platform = ? AND id = ? '
                                                         'AND metric = ? ORDER BY at', platform, oid, metric)}

    def source(self, name: str) -> dict:
        return next(s for s in stats.sources() if s['source'] == name)

    def meta_calls(self) -> list[str]:
        return [c[0] for c in self.fake.calls]


class Schema(Case):
    def test_tables_pragmas_and_place(self):
        self.assertTrue(guard._inside(stats._db(), guard.WORK), stats._db())
        self.assertTrue(guard._inside(stats.OLD_CACHE, guard.WORK), stats.OLD_CACHE)
        self.assertFalse(guard._inside(stats._db(), guard.REAL_WORK))
        with stats._Con() as con:
            names = {r['name'] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertLessEqual({'meta', 'sources', 'fetches', 'objects', 'samples', 'features'}, names)
            self.assertEqual(con.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(con.execute('PRAGMA busy_timeout').fetchone()[0], 10000)
            self.assertEqual(con.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()[0], '1')
            self.assertIs(con.row_factory, stats.sqlite3.Row)
        with self.assertRaises(stats.sqlite3.Error):                     # STRICT tables
            put('youtube', '_account', 'views', T0, 'many')

    def test_imports_publish_and_schedule_and_youtube_only_lazily(self):    # spec 3.2
        tree = ast.parse(Path(stats.__file__).read_text(encoding='utf-8'))
        top = {a.name.split('.')[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
        top |= {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
        self.assertEqual(top, {'json', 'os', 're', 'sqlite3', 'datetime', 'pathlib', 'paths', 'publish', 'schedule', '__future__'})
        inner = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) and n not in tree.body for a in n.names}
        self.assertEqual(inner, {'youtube'})


class ImportOldCache(Case):
    AT = datetime(2026, 11, 4, 12, 0, 0, 250000, tzinfo=UTC).timestamp()        # the old file's '_at' (a float)
    OLD = {'FAKE_old_01': {'views': 120, 'likes': 6, 'comments': 0, 'privacy': 'public', 'title': 'Example title',
                           'published': '2026-11-01T15:00:00Z'},
           'FAKE_old_02': {'views': 45, 'likes': 3, 'comments': 1, 'privacy': 'private', 'title': 'Another example',
                           'published': '2026-11-02T15:00:00Z'},
           '_channel': {'subscribers': 60, 'views': 4000, 'videos': 20}}

    def write(self, data: dict, path=None):
        path = path or guard.WORK / 'dashboard-stats.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_imports_once_at_its_time_and_leaves_the_file(self):
        path = self.write({**self.OLD, '_at': self.AT})
        before = path.read_bytes()
        self.assertEqual(stats.import_old_cache(), 9)
        self.assertEqual({r['at'] for r in self.rows('SELECT at FROM samples')}, {'2026-11-04T12:00:00Z'})
        self.assertEqual(self.series('youtube', 'FAKE_old_01', 'views'), {'2026-11-04T12:00:00Z': 120})
        self.assertEqual(self.series('youtube', '_account', 'subscribers'), {'2026-11-04T12:00:00Z': 60})
        self.assertEqual(len(self.rows("SELECT v FROM meta WHERE k = 'old_cache_imported'")), 1)
        self.assertEqual(stats.import_old_cache(), 0)                      # once
        self.assertEqual(len(self.rows('SELECT * FROM samples')), 9)
        self.assertEqual(path.read_bytes(), before)                        # the file stays for rollback

    def test_youtube_cache_gives_the_old_numbers_back(self):
        self.write({**self.OLD, '_at': self.AT})
        stats.import_old_cache()
        self.assertEqual(stats.youtube_cache(), {**self.OLD, '_at': int(self.AT)})

    def test_missing_or_broken_file_imports_nothing_and_is_not_marked(self):
        self.assertEqual(stats.import_old_cache(), 0)
        self.write({**self.OLD, '_at': self.AT}).write_text('{"FAKE_old_01": {"views": 1', encoding='utf-8')
        self.assertEqual(stats.import_old_cache(), 0)
        self.assertEqual(self.rows("SELECT * FROM meta WHERE k = 'old_cache_imported'"), [])
        self.write({**self.OLD, '_at': self.AT})
        self.assertEqual(stats.import_old_cache(), 9)

    def test_explicit_path_and_no_at_uses_the_file_time(self):
        path = self.write(self.OLD, guard.TMP / 'old-cache' / 'dashboard-stats.json')
        os.utime(path, (self.AT, self.AT))
        self.assertEqual(stats.import_old_cache(path), 9)
        self.assertEqual({r['at'] for r in self.rows('SELECT at FROM samples')}, {'2026-11-04T12:00:00Z'})

    def test_odd_entries_are_skipped(self):
        self.write({'FAKE_old_01': self.OLD['FAKE_old_01'], 'bad id!': {'views': 1}, 'FAKE_old_03': 'not a dict',
                    'FAKE_old_04': {'views': 'many', 'likes': True, 'comments': 2, 'title': 'x', 'privacy': 'public',
                                    'published': '2026-11-03T15:00:00Z'}, '_at': self.AT})
        self.assertEqual(stats.import_old_cache(), 4)                      # 3 + FAKE_old_04's comments
        self.assertEqual(set(stats.youtube_cache()), {'FAKE_old_01', '_channel', '_at'})

    def test_the_collector_corrects_the_kind(self):
        self.write({'FAKE_yt_001': fixtures.YT_VIDEOS['FAKE_yt_001'], '_at': self.AT})
        stats.import_old_cache()
        self.assertEqual(self.rows("SELECT kind FROM objects WHERE id = 'FAKE_yt_001'"), [{'kind': 'video'}])
        self.collect()
        self.assertEqual(self.rows("SELECT kind, lesson, file FROM objects WHERE id = 'FAKE_yt_001'"),
                         [{'kind': 'short', 'lesson': 'new', 'file': 'new-short-1.mp4'}])
        self.assertEqual(len(self.series('youtube', 'FAKE_yt_001', 'views')), 2)       # the old one and the new


class YoutubeIds(Case):
    def test_the_pipeline_videos_and_the_hand_made_one(self):
        ids = stats.youtube_ids(self.root)
        self.assertEqual(set(ids), set(fixtures.YT_VIDEOS))
        self.assertEqual(len(ids), 12)
        self.assertEqual({v for v, i in ids.items() if i['kind'] == 'video'}, {'FAKE_yt_010', 'FAKE_yt_013'})
        self.assertEqual(ids['FAKE_yt_013'], {'lesson': 'ZU + INFINITIV', 'file': 'zu-infinitiv-video.mp4', 'kind': 'video'})
        self.assertEqual(ids['FAKE_yt_010'], {'lesson': 'new', 'file': 'new-video.mp4', 'kind': 'video'})
        self.assertEqual(ids['FAKE_yt_003'], {'lesson': 'new', 'file': 'new-short-3.mp4', 'kind': 'short'})
        self.assertEqual(ids['FAKE_yt_011'], {'lesson': 'Um etwas bitten', 'file': 'um-etwas-bitten-video.mp4',
                                              'kind': 'short'})               # vertical, 68 s
        self.assertEqual(ids['FAKE_yt_012']['kind'], 'short')

    def test_hand_link_only_when_youtube_complete(self):
        st = self.root / 'ZU + INFINITIV' / 'Output (Easy)' / 'extras' / 'status.json'
        data = json.loads(st.read_text(encoding='utf-8'))
        st.write_text(json.dumps({**data, 'youtube_complete': False}), encoding='utf-8')
        self.assertNotIn('FAKE_yt_013', stats.youtube_ids(self.root))

    def test_odd_records_and_hidden_folders(self):
        for name in ('xtra', '_archive'):                  # make_root() rebuilds its lessons but keeps other folders
            self.addCleanup(shutil.rmtree, self.root / name, True)
        x = self.root / 'xtra' / 'Output' / 'extras'
        x.mkdir(parents=True)
        (x / 'youtube-applied.json').write_text(json.dumps({
            'a-video.mp4': {'id': 'bad id!'}, 'b-video.mp4': 'not a dict', 'c-short.mp4': {'id': 'FAKE_yt_099'},
            'd-video.mp4': {'id': 'FAKE_yt_010'}}), encoding='utf-8')      # an id another record has: first wins
        h = self.root / '_archive' / 'Output' / 'extras'
        h.mkdir(parents=True)
        (h / 'youtube-applied.json').write_text(json.dumps({'z-video.mp4': {'id': 'FAKE_yt_098'}}), encoding='utf-8')
        ids = stats.youtube_ids(self.root)
        self.assertEqual(ids['FAKE_yt_099'], {'lesson': 'xtra', 'file': 'c-short.mp4', 'kind': 'short'})  # by name
        self.assertEqual(ids['FAKE_yt_010']['file'], 'new-video.mp4')
        self.assertNotIn('FAKE_yt_098', ids)
        self.assertEqual(len(ids), 13)


class Collect(Case):
    def test_first_collect_reads_every_source_once(self):
        self.assertEqual(self.collect(), {s: 'ok' for s in SOURCES})
        self.assertEqual(self.meta_calls(), ['read_stats', 'read_limit', 'read_permissions'])
        self.assertEqual([c[0] for c in self.yt.calls], ['videos', 'channels'])
        self.assertEqual(self.yt.calls[0][1], {'part': 'snippet,statistics,status', 'id': ','.join(sorted(fixtures.YT_VIDEOS))})
        self.assertEqual(self.yt.calls[1][1], {'part': 'statistics', 'id': Y.CHANNEL_ID})
        at = S.iso(T0)
        self.assertEqual(self.series('youtube', '_account', 'subscribers'), {at: 143})
        self.assertEqual(self.series('youtube', 'FAKE_yt_010', 'views'), {at: 812})
        self.assertEqual(self.series('youtube', 'FAKE_yt_013', 'comments'), {at: 9})
        self.assertEqual({m: self.series(IG, '_account', m) for m in ('followers', 'follows', 'media')},
                         {'followers': {at: 71}, 'follows': {at: 0}, 'media': {at: 5}})
        self.assertEqual(self.series(IG, 'FAKE_MEDIA_5', 'likes'), {at: 15})
        self.assertEqual(self.rows("SELECT kind, title, url, published, lesson, file FROM objects WHERE id = 'FAKE_MEDIA_5'"),
                         [{'kind': 'reel', 'title': 'Hook line 5', 'url': 'https://www.instagram.com/reel/FAKE5/',
                           'published': '2026-11-04T13:46:12Z', 'lesson': 'new', 'file': 'new-short-5.mp4'}])
        self.assertEqual(self.rows("SELECT kind, lesson, file, privacy FROM objects WHERE id = 'FAKE_yt_013'"),
                         [{'kind': 'video', 'lesson': 'ZU + INFINITIV', 'file': 'zu-infinitiv-video.mp4', 'privacy': 'public'}])
        self.assertEqual({r['name']: r['value'] for r in self.rows('SELECT * FROM features')},
                         {'instagram_limit': '{"used": 0, "total": 100}', 'instagram_insights': 'missing'})
        self.assertEqual(self.rows('SELECT source, ok, calls, error FROM fetches ORDER BY id'),
                         [{'source': s, 'ok': 1, 'calls': 2 if s == 'youtube' else 1, 'error': None} for s in SOURCES])
        with P._Con() as con:                                              # the media went to publish.note_media
            self.assertEqual({r['id'] for r in con.execute("SELECT id FROM media WHERE platform = 'instagram'")},
                             {f'FAKE_MEDIA_{k}' for k in range(1, 6)})

    def test_five_minutes_later_nothing_is_read(self):
        self.collect()
        calls, yt = len(self.fake.calls), len(self.yt.calls)
        self.assertEqual(self.collect(T0 + 5 * MIN), {s: 'skipped' for s in SOURCES})
        self.assertEqual((len(self.fake.calls), len(self.yt.calls)), (calls, yt))

    def test_without_drive_only_the_channel_is_read(self):
        self.assertEqual(self.collect(root=None), {s: 'ok' for s in SOURCES})
        self.assertEqual([c[0] for c in self.yt.calls], ['channels'])
        self.assertEqual(self.rows("SELECT lesson, file FROM objects WHERE id = 'FAKE_MEDIA_5'"),
                         [{'lesson': None, 'file': None}])
        self.collect(T0 + HOUR)                                            # Drive back: the details are filled in
        self.assertEqual(self.rows("SELECT lesson, file FROM objects WHERE id = 'FAKE_MEDIA_5'"),
                         [{'lesson': 'new', 'file': 'new-short-5.mp4'}])
        self.collect(T0 + 2 * HOUR, root=None)                             # and a read without Drive keeps them
        self.assertEqual(self.rows("SELECT lesson FROM objects WHERE id = 'FAKE_MEDIA_5'"), [{'lesson': 'new'}])

    def test_meta_skipped_when_not_set_up_or_missing(self):
        self.fake.ready = False
        res = self.collect()
        self.assertEqual(res, {'youtube': 'ok', **{s: 'skipped' for s in stats._META}})
        self.assertEqual(self.fake.calls, [])
        self.assertEqual([s['last_try'] for s in stats.sources() if s['source'] != 'youtube'], [None] * 3)
        P.set_registry({})
        self.assertEqual(self.collect(T0 + HOUR), {'youtube': 'ok', **{s: 'skipped' for s in stats._META}})

    def test_default_youtube_goes_through_youtube_api(self):
        res = self.collect(yt=None)                                        # the guard blocks youtube.api()
        self.assertTrue(res['youtube'].startswith('error: youtube.api()'), res['youtube'])
        self.assertEqual(res['instagram'], 'ok')

    def test_never_raises(self):
        with mock.patch.object(stats, 'due', side_effect=RuntimeError('made-up database failure')):
            self.assertEqual(self.collect(), {s: 'error: made-up database failure' for s in SOURCES})

    def test_a_failed_youtube_read_stores_nothing(self):
        class HalfBroken(fixtures.FakeYouTube):
            def channels(self):
                raise RuntimeError('made-up channel failure')
        res = self.collect(yt=HalfBroken(fixtures.YT_VIDEOS, fixtures.YT_CHANNEL))
        self.assertEqual(res['youtube'], 'error: made-up channel failure')
        self.assertEqual(self.rows("SELECT * FROM samples WHERE platform = 'youtube'"), [])
        self.assertEqual(self.rows("SELECT ok, calls FROM fetches WHERE source = 'youtube'"), [{'ok': 0, 'calls': 2}])

    def test_a_reader_that_lost_the_race_reads_nothing(self):
        with mock.patch.object(stats, 'due', return_value=True):       # both readers saw "due"
            self.assertTrue(stats._claim('youtube', T0))                 # the other one took it first
            self.assertEqual(self.collect()['youtube'], 'skipped')
        self.assertEqual(self.yt.calls, [])

    def test_media_kinds_and_matching_by_permalink(self):
        self.fake.media = [{**media()[0], 'id': 'FAKE_MEDIA_77'},           # a new id for new-short-5's permalink
                           {'id': 'FAKE_MEDIA_90', 'at': '2026-11-04T09:00:00+00:00', 'product': 'FEED', 'likes': 3,
                            'comments': 0, 'permalink': 'https://www.instagram.com/p/FAKEPOST/', 'caption': ''}]
        self.collect()
        self.assertEqual(self.rows("SELECT id, kind, title, published, lesson, file FROM objects WHERE platform = 'instagram' "
                                   "AND id != '_account' ORDER BY id"),
                         [{'id': 'FAKE_MEDIA_77', 'kind': 'reel', 'title': 'Hook line 5', 'published': '2026-11-04T13:46:12Z',
                           'lesson': 'new', 'file': 'new-short-5.mp4'},
                          {'id': 'FAKE_MEDIA_90', 'kind': 'post', 'title': None, 'published': '2026-11-04T09:00:00Z',
                           'lesson': None, 'file': None}])
        self.assertEqual([r['id'] for r in stats.summary(self.root, T0)['reels']], ['FAKE_MEDIA_77'])

    def test_long_captions_are_shortened_with_a_mark(self):
        self.fake.media = [{**media()[0], 'caption': '  Ein   sehr ' + 'langer ' * 20 + '\nsecond line'}]
        self.collect()
        title = self.rows("SELECT title FROM objects WHERE id = 'FAKE_MEDIA_5'")[0]['title']
        self.assertEqual(len(title), 60)
        self.assertTrue(title.startswith('Ein sehr langer') and title.endswith('…'), title)

    def test_bad_limit_answer_is_an_error(self):
        self.fake.read_limit = lambda: {'used': None, 'total': 100}
        self.assertEqual(self.collect()['instagram_limit'],
                         'error: the publishing limit answer has no used/total numbers')
        self.assertIsNone(stats.summary(self.root, T0)['limit'])


class Due(Case):
    def test_every_source_is_due_before_its_first_read(self):
        self.assertEqual(stats.EVERY, {'youtube': 3300, 'instagram': 3300, 'instagram_limit': 21600,
                                       'instagram_permissions': 86400})
        self.assertEqual(stats.BACKOFF, (3600, 10800, 21600))
        self.assertTrue(all(stats.due(s, T0) for s in SOURCES))

    def test_after_a_success_each_waits_its_every(self):
        self.collect()
        for s, every in stats.EVERY.items():
            self.assertFalse(stats.due(s, T0 + timedelta(seconds=every - 1)), s)
            self.assertTrue(stats.due(s, T0 + timedelta(seconds=every)), s)
            self.assertEqual(self.source(s)['next_try'], S.iso(T0 + timedelta(seconds=every)))

    def test_errors_back_off_1_3_6_hours_then_a_success_resets(self):
        t = T0
        for n, hours in enumerate((1, 3, 6, 6), 1):
            self.assertEqual(self.collect(t, yt=Broken())['youtube'], 'error: made-up YouTube failure')
            src = self.source('youtube')
            self.assertEqual((src['fails'], src['next_try'], src['error']),
                             (n, S.iso(t + hours * HOUR), 'made-up YouTube failure'))
            self.assertFalse(stats.due('youtube', t + hours * HOUR - timedelta(seconds=1)))
            t += hours * HOUR
        self.assertEqual(self.collect(t)['youtube'], 'ok')
        src = self.source('youtube')
        self.assertEqual((src['fails'], src['error'], src['last_ok'], src['next_try']),
                         (0, None, S.iso(t), S.iso(t + 55 * MIN)))

    def test_meta_sources_are_not_due_while_the_breaker_is_open(self):
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        self.assertEqual(P.breaker('meta')['state'], 'stopped')
        self.assertEqual([stats.due(s, T0) for s in SOURCES], [True, False, False, False])

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            stats.due('tiktok', T0)

    def test_a_next_try_from_a_wrong_clock_does_not_stall_the_reads(self):
        self.collect(T0 + 3 * DAY)                                         # the clock was 3 days ahead
        self.assertFalse(stats.due('youtube', T0 + 3 * DAY + MIN))
        self.assertTrue(stats.due('youtube', T0))                          # back to the right time: read again
        self.assertEqual(self.collect(T0)['youtube'], 'ok')
        self.assertEqual(self.source('youtube')['next_try'], S.iso(T0 + 55 * MIN))

    def test_the_longest_wait_still_counts(self):                         # the 25-hour Pacific day (1 Nov 2026)
        quota = fixtures.fake_youtube_api(fixtures.YT_VIDEOS, None)
        at = datetime(2026, 11, 1, 7, 0, 30, tzinfo=UTC)                   # 00:00:30 in California, summer time
        self.collect(at, yt=quota)
        self.assertEqual(self.source('youtube')['next_try'], '2026-11-02T08:00:00Z')    # 25 h later
        self.assertFalse(stats.due('youtube', at + 24 * HOUR))


class MetaGuards(Case):
    """S17: Meta reads by the collector stay within the 5.9 limits and stop while the breaker is open; collector
    errors never trip the breaker."""

    def run_for(self, hours: float, every: int = 5) -> list[tuple[str, datetime]]:
        """collect() every `every` minutes for `hours` from self.now; [(call, time)] of the platform calls
        and ('yt', time) for each YouTube channel read."""
        start, log = self.now, []
        for k in range(int(hours * 60) // every + 1):
            meta, yt = len(self.fake.calls), len(self.yt.calls)
            self.collect(start + timedelta(minutes=every * k))
            log += [(c[0], self.now) for c in self.fake.calls[meta:]]
            log += [('yt', self.now) for c in self.yt.calls[yt:] if c[0] == 'channels']
        return log

    @staticmethod
    def gaps(log, name) -> list[timedelta]:
        t = [at for n, at in log if n == name]
        return [b - a for a, b in zip(t, t[1:])]

    def test_reads_stay_within_the_limits(self):
        log = self.run_for(25)
        self.assertEqual({n for n, _ in log}, {'read_stats', 'read_limit', 'read_permissions', 'yt'})   # nothing else
        self.assertEqual(self.gaps(log, 'read_stats'), [55 * MIN] * 27)            # 0, 0:55, ... 24:45
        self.assertEqual(self.gaps(log, 'read_limit'), [6 * HOUR] * 4)
        self.assertEqual(self.gaps(log, 'read_permissions'), [24 * HOUR])
        self.assertEqual(self.gaps(log, 'yt'), [55 * MIN] * 27)

    def test_no_meta_reads_while_the_breaker_is_stopped(self):
        P.trip('meta', 'auth', 'Instagram refused the key (code 190).')
        log = self.run_for(3)
        self.assertEqual([n for n, _ in log], ['yt'] * 4)                          # YouTube goes on: 0, 55, 110, 165
        self.assertEqual(self.collect(self.now + MIN)['instagram'], 'skipped')

    def test_a_cooling_breaker_holds_meta_reads_until_it_ends(self):
        P.trip('meta', 'rate', 'Instagram says: too many calls (code 4).')         # cooling 1 h from T0
        self.assertEqual(P.breaker('meta')['until'], S.iso(T0 + HOUR))
        log = self.run_for(1.5)
        self.assertEqual({at for n, at in log if n != 'yt'}, {T0 + HOUR})         # all three, the moment it ends
        self.assertEqual(sorted(n for n, _ in log if n != 'yt'), ['read_limit', 'read_permissions', 'read_stats'])

    def test_errors_never_trip_the_breaker(self):
        for name in ('trip', '_trip'):
            p = mock.patch.object(P, name, side_effect=AssertionError('the collector tripped a breaker'))
            p.start()
            self.addCleanup(p.stop)
        t = T0
        for kind in ('auth', 'permission', 'rate', 'transient', 'unknown'):
            for method in ('read_stats', 'read_limit', 'read_permissions'):
                setattr(self.fake, method, mock.Mock(side_effect=F.FakeError(kind)))
            with stats._Con(write=True) as con:
                con.execute('DELETE FROM sources')                                 # every source due again
            res = self.collect(t)
            self.assertEqual({res[s] for s in stats._META}, {f'error: {F.FakeError(kind)}'})
            self.assertEqual(P.breaker('meta')['state'], 'closed')
            t += MIN
        with P._Con() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit WHERE kind = 'breaker.open'").fetchone()[0], 0)
            self.assertIsNone(con.execute("SELECT * FROM breakers WHERE name = 'meta'").fetchone())

    def test_F9_an_auth_error_is_kept_for_the_dashboard(self):
        """F9: the collector never trips the breaker, but an 'auth' error is kept (key_refused) until a Meta read
        works again or clear_key_refused() (a passing connection test)."""
        def fail(kind):
            for method in ('read_stats', 'read_limit', 'read_permissions'):
                setattr(self.fake, method, mock.Mock(side_effect=F.FakeError(kind)))
            with stats._Con(write=True) as con:
                con.execute('DELETE FROM sources')
        self.assertIsNone(stats.key_refused())
        for kind in ('transient', 'rate', 'permission', 'unknown'):
            fail(kind)
            self.collect(self.now + MIN)
            self.assertIsNone(stats.key_refused(), kind)
        fail('auth')
        res = self.collect(self.now + MIN)
        self.assertEqual(stats.key_refused(), res['instagram'].removeprefix('error: '))
        self.assertEqual(P.breaker('meta')['state'], 'closed')                    # S17 still holds
        fail('transient')                                                          # another error keeps it
        self.collect(self.now + MIN)
        self.assertIsNotNone(stats.key_refused())
        stats.clear_key_refused()
        self.assertIsNone(stats.key_refused())
        fail('auth')
        self.collect(self.now + MIN)
        self.assertIsNotNone(stats.key_refused())
        for method in ('read_stats', 'read_limit', 'read_permissions'):
            delattr(self.fake, method)                                             # Meta answers again
        with stats._Con(write=True) as con:
            con.execute('DELETE FROM sources')
        self.assertEqual(self.collect(self.now + MIN)['instagram'], 'ok')
        self.assertIsNone(stats.key_refused())

    def test_errors_back_off_1_3_6_hours(self):
        boom = mock.Mock(side_effect=F.FakeError('transient'))
        self.fake.read_stats = boom
        t = T0
        for n, hours in enumerate((1, 3, 6, 6), 1):
            self.assertEqual(self.collect(t)['instagram'], 'error: Instagram did not answer (HTTP 503).')
            self.assertEqual(self.collect(t + hours * HOUR - timedelta(seconds=1))['instagram'], 'skipped')
            src = self.source('instagram')
            self.assertEqual((src['fails'], src['next_try']), (n, S.iso(t + hours * HOUR)))
            t += hours * HOUR
        self.assertEqual(boom.call_count, 4)
        del self.fake.read_stats                                                   # the platform answers again
        self.assertEqual(self.collect(t)['instagram'], 'ok')
        src = self.source('instagram')
        self.assertEqual((src['fails'], src['error'], src['next_try']), (0, None, S.iso(t + 55 * MIN)))
        self.assertEqual(P.breaker('meta')['state'], 'closed')


class QuotaWait(Case):
    """S18: YouTube reads happen at most once per 55 min, and none after quotaExceeded until the next Pacific
    midnight."""

    def test_at_most_once_per_55_minutes(self):
        self.assertEqual(self.collect()['youtube'], 'ok')
        for later in (5 * MIN, 30 * MIN, 54 * MIN, 55 * MIN - timedelta(seconds=1)):
            self.assertEqual(self.collect(T0 + later)['youtube'], 'skipped')
        self.assertEqual(len(self.yt.calls), 2)
        self.assertEqual(self.collect(T0 + 55 * MIN)['youtube'], 'ok')
        self.assertEqual(len(self.yt.calls), 4)

    def test_quota_waits_until_the_next_pacific_midnight(self):
        quota = fixtures.fake_youtube_api(fixtures.YT_VIDEOS, None)
        at = datetime(2026, 11, 4, 19, 31, tzinfo=UTC)                 # 22:31 Istanbul, 11:31 in California
        self.assertEqual(self.collect(at, yt=quota)['youtube'], f'error: {stats._QUOTA}')
        self.assertEqual(len(quota.calls), 1)                          # the first request failed: no more asked
        src = self.source('youtube')
        self.assertEqual((src['next_try'], src['fails'], src['error']), ('2026-11-05T08:00:00Z', 1, stats._QUOTA))
        self.assertEqual(S.local(P._time(src['next_try'])).strftime('%a %H:%M'), 'Thu 11:00')
        t, instagram = at + 30 * MIN, []
        while t < T('2026-11-05 11:00'):
            res = self.collect(t, yt=quota)
            self.assertEqual(res['youtube'], 'skipped')
            instagram.append(res['instagram'])
            t += 30 * MIN
        self.assertEqual(instagram.count('ok'), 12)                    # Instagram is read on, hourly
        self.assertEqual(self.collect(T('2026-11-05 11:00') - timedelta(seconds=1), yt=quota)['youtube'], 'skipped')
        self.assertEqual(len(quota.calls), 1)
        self.assertEqual(self.collect(T('2026-11-05 11:00'))['youtube'], 'ok')
        self.assertEqual(P.breaker('meta')['state'], 'closed')

    def test_a_google_error_reads_plainly(self):
        raw = ('<HttpError 503 when requesting https://youtube.googleapis.com/youtube/v3/channels?part=statistics'
               '&mine=true&alt=json returned "Backend Error". Details: "[{\'reason\': \'backendError\'}]">')
        self.assertEqual(stats._short(RuntimeError(raw)), 'YouTube did not answer (error 503).')
        self.assertEqual(stats._short(TimeoutError()), 'TimeoutError')               # no message: the type
        for text in (stats._short(RuntimeError(raw)), stats._QUOTA):
            self.assertNotIn('HttpError', text)
            self.assertNotIn('http', text)

    def test_old_stored_errors_read_plainly(self):
        raw = ('<HttpError 403 when requesting https://youtube.googleapis.com/youtube/v3/channels?part=contentDetails'
               '&mine=true&alt=json returned "The request cannot be completed because you have exceeded your <a href='
               '"/youtube/data/getting-started#quota">quota</a>.". Details: "[{\'reason\': \'quotaExceeded\'}]">')
        for old, want in ((('HttpError: ' + raw)[:159] + '…', None),     # the old _short: type, message, cut
                          ('HttpError: ' + raw[:300], None),
                          ('quotaExceeded: the daily YouTube quota is used up until midnight Pacific time', stats._QUOTA)):
            with self.subTest(old=old[:40]):
                with stats._Con(write=True) as con:
                    con.execute("INSERT INTO sources (source, fails, last_error) VALUES ('youtube', 1, ?) "
                                'ON CONFLICT(source) DO UPDATE SET last_error = excluded.last_error', (old,))
                got = self.source('youtube')['error']
                for bad in ('HttpError', 'http', '<', 'quotaExceeded', '….'):
                    self.assertNotIn(bad, got)
                if want:
                    self.assertEqual(got, want)

    def test_quota_shortly_before_midnight_still_waits_55_minutes(self):
        quota = fixtures.fake_youtube_api(fixtures.YT_VIDEOS, None)
        self.collect(datetime(2026, 11, 5, 7, 40, tzinfo=UTC), yt=quota)            # 23:40 in California
        self.assertEqual(self.source('youtube')['next_try'], '2026-11-05T08:35:00Z')

    def test_summer_midnight_is_10_istanbul(self):
        quota = fixtures.fake_youtube_api(fixtures.YT_VIDEOS, None)
        self.collect(datetime(2026, 7, 1, 12, 0, tzinfo=UTC), yt=quota)
        self.assertEqual(self.source('youtube')['next_try'], '2026-07-02T07:00:00Z')
        self.assertEqual(S.local(datetime(2026, 7, 2, 7, 0, tzinfo=UTC)).strftime('%H:%M'), '10:00')


class Deltas(Case):
    def test_value_d24_d7_and_at(self):
        for back, value in ((8 * DAY, 100), (25 * HOUR, 130), (2 * HOUR, 140), (0 * HOUR, 143), (-HOUR, 200)):
            put('youtube', '_account', 'subscribers', T0 - back, value)   # the one after now is ignored
        tile = stats.tiles(T0)[0]
        self.assertEqual(list(tile), TILE_KEYS)
        self.assertEqual({k: tile[k] for k in ('id', 'label', 'value', 'd24', 'd7', 'at')},
                         {'id': 'yt_subscribers', 'label': 'YouTube subscribers', 'value': 143, 'd24': 13, 'd7': 43,
                          'at': S.iso(T0)})

    def test_no_older_sample_gives_none(self):
        put('instagram', '_account', 'followers', T0 - 2 * HOUR, 70)
        put('instagram', '_account', 'followers', T0 - 30 * MIN, 71)
        tile = {t['id']: t for t in stats.tiles(T0)}['ig_followers']
        self.assertEqual((tile['value'], tile['d24'], tile['d7'], tile['at']), (71, None, None, S.iso(T0 - 30 * MIN)))
        put('instagram', '_account', 'followers', T0 - 24 * HOUR, 65)            # exactly 24 h before counts
        self.assertEqual({t['id']: t for t in stats.tiles(T0)}['ig_followers']['d24'], 6)

    def test_instagram_likes_add_up_each_mediums_newest(self):
        put(IG, 'FAKE_MEDIA_1', 'likes', T0 - 26 * HOUR, 10)
        put(IG, 'FAKE_MEDIA_1', 'likes', T0 - HOUR, 12)
        put(IG, 'FAKE_MEDIA_2', 'likes', T0 - 26 * HOUR, 5)                     # no longer in the newest 50
        put(IG, 'FAKE_MEDIA_3', 'likes', T0 - 30 * MIN, 4)                      # new today
        put(IG, '_account', 'followers', T0, 71)                                # not a medium
        tile = {t['id']: t for t in stats.tiles(T0)}['ig_likes']
        self.assertEqual((tile['label'], tile['value'], tile['d24'], tile['d7'], tile['at']),
                         ('Instagram likes', 21, 6, None, S.iso(T0 - 30 * MIN)))

    def test_empty_database(self):
        out = stats.tiles(T0)
        self.assertEqual([t['id'] for t in out], ['yt_subscribers', 'yt_views', 'ig_followers', 'ig_likes'])
        self.assertEqual([t['label'] for t in out], ['YouTube subscribers', 'YouTube views', 'Instagram followers',
                                                     'Instagram likes'])
        for t in out:
            self.assertEqual({k: t[k] for k in ('value', 'd24', 'd7', 'at')}, dict.fromkeys(('value', 'd24', 'd7', 'at')))
            self.assertEqual(t['spark'], [None] * 14)

    def test_after_two_reads_a_day_apart(self):
        self.collect()
        more = {k: {**v, 'views': v['views'] + 7} for k, v in fixtures.YT_VIDEOS.items()}
        self.yt = fixtures.fake_youtube_api(more, {**fixtures.YT_CHANNEL, 'subscribers': 150})
        self.fake.media = [{**m, 'likes': m['likes'] + 1} for m in media()]
        self.collect(T0 + DAY)
        t = {x['id']: x for x in stats.tiles(T0 + DAY)}
        self.assertEqual((t['yt_subscribers']['value'], t['yt_subscribers']['d24']), (150, 7))
        self.assertEqual((t['ig_likes']['value'], t['ig_likes']['d24']), (70, 5))
        self.assertEqual(t['yt_views']['d24'], 0)
        videos = {v['id']: v for v in stats.summary(self.root, T0 + DAY)['youtube_videos']}
        self.assertEqual((videos['FAKE_yt_013']['views'], videos['FAKE_yt_013']['d24_views']), (1247, 7))


class Sparklines(Case):
    def test_fourteen_istanbul_days_each_days_last_value(self):
        for when, value in (('2026-10-21 12:00', 90),          # before the 14 days
                            ('2026-10-22 09:00', 100),         # the first day
                            ('2026-11-02 00:30', 135),         # 21:30 UTC on 1 Nov: an Istanbul 2 Nov sample
                            ('2026-11-03 08:00', 137), ('2026-11-03 23:59', 139),
                            ('2026-11-04 10:00', 141), ('2026-11-04 22:30', 143),
                            ('2026-11-04 23:00', 150)):        # after now (22:40)
            put('youtube', '_account', 'views', T(when), value)
        tile = {t['id']: t for t in stats.tiles(T0)}['yt_views']
        self.assertEqual(tile['spark'], [100] + [None] * 10 + [135, 139, 143])
        self.assertEqual(tile['value'], tile['spark'][-1])

    def test_likes_spark_is_the_sum_per_day(self):
        put(IG, 'FAKE_MEDIA_1', 'likes', T('2026-11-02 21:00'), 10)
        put(IG, 'FAKE_MEDIA_2', 'likes', T('2026-11-02 21:00'), 5)
        put(IG, 'FAKE_MEDIA_1', 'likes', T('2026-11-04 21:00'), 12)              # media 2 not read again
        spark = {t['id']: t for t in stats.tiles(T0)}['ig_likes']['spark']
        self.assertEqual(spark[-3:], [15, None, 17])
        self.assertEqual(spark[:-3], [None] * 11)

    def test_after_a_collect_the_last_point_is_today(self):
        self.collect()
        for t in stats.tiles(T0):
            self.assertEqual(t['spark'], [None] * 13 + [t['value']], t['id'])
            self.assertIsNotNone(t['value'], t['id'])


class Summary(Case):
    def test_the_8_3_body(self):
        self.collect()
        out = stats.summary(self.root, T0)
        self.assertEqual(list(out), KEYS_8_3)
        self.assertEqual(out['now'], '2026-11-04T19:40:00Z')
        self.assertEqual([s['source'] for s in out['sources']], list(SOURCES))
        self.assertEqual(out['sources'][0], {'source': 'youtube', 'last_ok': '2026-11-04T19:40:00Z',
                                             'last_try': '2026-11-04T19:40:00Z', 'next_try': '2026-11-04T20:35:00Z',
                                             'fails': 0, 'error': None})
        self.assertEqual([list(t) for t in out['tiles']], [TILE_KEYS] * 4)
        self.assertEqual({t['id']: t['value'] for t in out['tiles']},
                         {'yt_subscribers': 143, 'yt_views': 5230, 'ig_followers': 71, 'ig_likes': 65})
        vids = out['youtube_videos']
        self.assertEqual(len(vids), 12)
        self.assertEqual([v['id'] for v in vids[:3]], ['FAKE_yt_013', 'FAKE_yt_010', 'FAKE_yt_001'])
        self.assertEqual([v['views'] for v in vids], sorted((v['views'] for v in vids), reverse=True))
        self.assertEqual(vids[0], {'id': 'FAKE_yt_013', 'title': fixtures.ZU_TEXTS['titles'][0], 'kind': 'video',
                                   'published': '2026-10-29T17:00:00Z', 'views': 1240, 'likes': 61, 'comments': 9,
                                   'd24_views': None, 'thumb': 'https://i.ytimg.com/vi/FAKE_yt_013/mqdefault.jpg',
                                   'url': 'https://youtu.be/FAKE_yt_013'})
        self.assertEqual({v['id'] for v in vids if v['kind'] == 'short'}, set(fixtures.YT_VIDEOS) - {'FAKE_yt_010', 'FAKE_yt_013'})
        self.assertEqual([r['id'] for r in out['reels']], [f'FAKE_MEDIA_{k}' for k in range(5, 0, -1)])
        self.assertEqual(out['reels'][0], {'id': 'FAKE_MEDIA_5', 'at': '2026-11-04T13:46:12Z', 'title': 'Hook line 5',
                                           'likes': 15, 'comments': 1, 'url': 'https://www.instagram.com/reel/FAKE5/',
                                           'lesson': 'new', 'file': 'new-short-5.mp4'})
        self.assertEqual(out['insights'], {'available': False, 'checked': '2026-11-04T19:40:00Z', 'text': stats._NO_INSIGHTS})
        self.assertIn('then run instagram.py system-token on the PC.', out['insights']['text'])     # addendum C7
        self.assertEqual(out['limit'], {'used': 0, 'total': 100, 'at': '2026-11-04T19:40:00Z', 'stale': False})
        json.dumps(out)                                                    # plain JSON

    def test_pairs_join_youtube_and_instagram_on_lesson_and_file(self):
        self.collect()
        pairs = stats.summary(self.root, T0)['pairs']
        self.assertEqual([p['file'] for p in pairs], [f'new-short-{k}.mp4' for k in range(5, 0, -1)])
        self.assertEqual(pairs[0], {'lesson': 'new', 'file': 'new-short-5.mp4',
                                    'youtube': {'views': 220, 'likes': 15, 'comments': 2, 'url': 'https://youtu.be/FAKE_yt_005'},
                                    'instagram': {'likes': 15, 'comments': 1, 'url': 'https://www.instagram.com/reel/FAKE5/'}})

    def test_no_pair_without_both_sides(self):
        self.fake.media = media(2)
        self.collect(root=None)                                           # no lessons known yet: no pairs
        self.assertEqual(stats.summary(None, T0)['pairs'], [])
        self.collect(T0 + HOUR)
        self.assertEqual([p['file'] for p in stats.summary(None, T0 + HOUR)['pairs']], ['new-short-2.mp4', 'new-short-1.mp4'])

    def test_empty_database(self):
        out = stats.summary(None, T0)
        self.assertEqual(list(out), KEYS_8_3)
        self.assertEqual([out[k] for k in ('youtube_videos', 'reels', 'pairs', 'insights', 'limit')], [[], [], [], None, None])
        self.assertEqual(out['sources'], [{'source': s, 'last_ok': None, 'last_try': None, 'next_try': None, 'fails': 0,
                                           'error': None} for s in SOURCES])
        self.assertEqual(len(out['tiles']), 4)

    def test_insights_granted(self):
        self.fake.read_permissions = lambda: ['instagram_basic', 'instagram_manage_insights']
        self.collect()
        self.assertEqual(stats.summary(self.root, T0)['insights'],
                         {'available': True, 'checked': '2026-11-04T19:40:00Z', 'text': stats._HAS_INSIGHTS})

    def test_a_failing_source_shows_its_reason(self):
        self.collect()
        self.fake.read_stats = mock.Mock(side_effect=F.FakeError('auth'))
        self.collect(T0 + HOUR)
        src = {s['source']: s for s in stats.summary(self.root, T0 + HOUR)['sources']}['instagram']
        self.assertEqual((src['last_ok'], src['fails'], src['error']),
                         ('2026-11-04T19:40:00Z', 1, 'Instagram refused the key (code 190).'))

    def test_secrets_never_reach_the_reason(self):
        self.fake.read_stats = mock.Mock(side_effect=RuntimeError('GET /me?access_token=EAAFAKEFAKEFAKE123 failed'))
        self.collect()
        error = self.source('instagram')['error']
        self.assertNotIn('EAAFAKE', error)
        self.assertIn('[hidden]', error)


def old_refresh_stats(ids, yt, channel_id) -> dict:
    """v1 dashboard.refresh_stats() (removed in v2) without its time check and error print: the reference for
    the old dashboard-stats.json shape."""
    stats_ = {}
    ids = sorted(ids)
    for k in range(0, len(ids), 50):
        for v in yt.videos().list(part='snippet,statistics,status', id=','.join(ids[k:k + 50])).execute()['items']:
            s = v['statistics']
            stats_[v['id']] = {'views': int(s.get('viewCount', 0)), 'likes': int(s.get('likeCount', 0)),
                               'comments': int(s.get('commentCount', 0)), 'privacy': v['status']['privacyStatus'],
                               'title': v['snippet']['title'], 'published': v['snippet']['publishedAt']}
    ch = yt.channels().list(part='statistics', id=channel_id).execute()['items'][0]['statistics']
    stats_['_channel'] = {'subscribers': int(ch.get('subscriberCount', 0)), 'views': int(ch.get('viewCount', 0)),
                          'videos': int(ch.get('videoCount', 0))}
    stats_['_at'] = time.time()
    return stats_


class YoutubeCacheShape(Case):
    def test_same_shape_as_the_old_file(self):
        old = old_refresh_stats(fixtures.YT_VIDEOS, fixtures.fake_youtube_api(fixtures.YT_VIDEOS, fixtures.YT_CHANNEL),
                                Y.CHANNEL_ID)
        self.collect()
        new = stats.youtube_cache()
        self.assertEqual({k: v for k, v in new.items() if k != '_at'}, {k: v for k, v in old.items() if k != '_at'})
        for vid, v in old.items():
            if not vid.startswith('_'):
                self.assertEqual({k: type(x) for k, x in new[vid].items()}, {k: type(x) for k, x in v.items()}, vid)
        self.assertEqual(new['_at'], int(T0.timestamp()))

    def test_merges_as_the_old_build_did(self):
        self.collect()
        cache = stats.youtube_cache()
        it = {'youtube': 'FAKE_yt_013', 'state': 'public', 'time': None}
        s = cache.get(it.get('youtube', ''))                               # v1 build()'s merge, line for line
        if s:
            it.update(title=s['title'], privacy=s['privacy'], views=s['views'], likes=s['likes'], comments=s['comments'])
            if it['state'] == 'public' and s['privacy'] == 'public':
                it['time'] = s['published']
        channel = {'name': 'Example', **cache.get('_channel', {})}
        self.assertEqual((it['views'], it['time'], channel['subscribers']), (1240, '2026-10-29T17:00:00Z', 143))
        self.assertTrue(cache['_at'])

    def test_the_fake_youtube_takes_the_cache_back(self):
        self.collect()
        cache = stats.youtube_cache()
        again = fixtures.fake_youtube_api({k: v for k, v in cache.items() if not k.startswith('_')}, cache['_channel'])
        self.collect(T0 + HOUR, yt=again)
        self.assertEqual({k: v for k, v in stats.youtube_cache().items() if k != '_at'},
                         {k: v for k, v in cache.items() if k != '_at'})

    def test_empty_and_never_read(self):
        self.assertEqual(stats.youtube_cache(), {'_channel': {}, '_at': 0})


class FakeCreds:
    """Stands for google.oauth2.credentials.Credentials: to_json() gives `text`; refresh() swaps in `refreshed`."""
    def __init__(self, text: str, refreshed: str | None = None):
        self.text, self.refreshed, self.refresh_token = text, refreshed, 'FAKE_REFRESH'
        self.expired, self.valid = refreshed is not None, refreshed is None

    def refresh(self, request):
        self.text, self.expired, self.valid = self.refreshed, False, True

    def to_json(self) -> str:
        return self.text


class SaveToken(unittest.TestCase):
    """youtube.save_token and the last write of youtube.credentials(): only on change, atomic, retried."""
    OLD = json.dumps({'token': 'FAKE_ACCESS_1', 'refresh_token': 'FAKE_REFRESH', 'client_id': 'FAKE_CLIENT',
                      'client_secret': 'FAKE_SECRET', 'expiry': '2026-11-05T01:02:03Z'})
    NEW = json.dumps({'token': 'FAKE_ACCESS_2', 'refresh_token': 'FAKE_REFRESH', 'client_id': 'FAKE_CLIENT',
                      'client_secret': 'FAKE_SECRET', 'expiry': '2026-11-05T02:02:03.456789Z'})

    def setUp(self):
        guard.reset()
        self.assertTrue(guard._inside(Y.TOKEN, guard.HOME), Y.TOKEN)          # never the real key folder
        Y.TOKEN.parent.mkdir(parents=True, exist_ok=True)
        Y.TOKEN.write_text(self.OLD, encoding='utf-8')
        os.utime(Y.TOKEN, (1_700_000_000, 1_700_000_000))
        self.real_replace = os.replace

    def credentials(self, creds: FakeCreds):
        with mock.patch.object(Y, 'Credentials', SimpleNamespace(from_authorized_user_file=lambda path, scopes: creds)), \
             mock.patch.object(Y, 'Request', lambda: 'FAKE_REQUEST'), \
             mock.patch.object(Y, 'InstalledAppFlow', None):                # a sign-in would fail the test
            return Y.credentials()

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in Y.TOKEN.parent.iterdir() if p.name != Y.TOKEN.name)

    def test_unchanged_token_is_not_written(self):
        with mock.patch.object(Y, 'save_token', wraps=Y.save_token) as save:
            creds = FakeCreds(self.OLD)
            self.assertIs(self.credentials(creds), creds)
        save.assert_not_called()
        self.assertEqual(Y.TOKEN.stat().st_mtime, 1_700_000_000)

    def test_refreshed_token_is_written_once(self):
        with mock.patch.object(Y, 'save_token', wraps=Y.save_token) as save:
            self.credentials(FakeCreds(self.OLD, refreshed=self.NEW))
        save.assert_called_once_with(self.NEW)
        self.assertEqual(Y.TOKEN.read_text(encoding='utf-8'), self.NEW)
        self.assertEqual(self.leftovers(), [])

    def test_write_is_atomic(self):
        seen = []

        def replace(src, dst):
            seen.append((Path(src).parent == Path(dst).parent, Path(src).read_text(encoding='utf-8'),
                         Path(dst).read_text(encoding='utf-8')))
            return self.real_replace(src, dst)
        with mock.patch.object(Y.os, 'replace', replace):
            Y.save_token(self.NEW)
        self.assertEqual(seen, [(True, self.NEW, self.OLD)])            # complete temp file; old token intact until then
        self.assertEqual(Y.TOKEN.read_text(encoding='utf-8'), self.NEW)
        self.assertEqual(self.leftovers(), [])

    def test_permission_error_is_retried(self):
        fails = [PermissionError('in use'), PermissionError('in use')]

        def replace(src, dst):
            if fails:
                raise fails.pop()
            return self.real_replace(src, dst)
        with mock.patch.object(Y.os, 'replace', replace), mock.patch.object(Y.time, 'sleep') as sleep:
            Y.save_token(self.NEW)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.2, 0.4])
        self.assertEqual(Y.TOKEN.read_text(encoding='utf-8'), self.NEW)

    def test_gives_up_after_five_retries_and_keeps_the_old_token(self):
        with mock.patch.object(Y.os, 'replace', side_effect=PermissionError('in use')), \
             mock.patch.object(Y.time, 'sleep') as sleep, self.assertRaises(PermissionError):
            Y.save_token(self.NEW)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.2, 0.4, 0.6, 0.8, 1.0])   # about 3 s
        self.assertEqual(Y.TOKEN.read_text(encoding='utf-8'), self.OLD)
        self.assertEqual(self.leftovers(), [])

    def test_F40_data_reaches_disk_before_replace(self):
        order, real_fsync = [], os.fsync

        def fsync(fd):
            order.append(('fsync', os.fstat(fd).st_size, Y.TOKEN.read_text(encoding='utf-8')))
            return real_fsync(fd)

        def replace(src, dst):
            order.append(('replace',))
            return self.real_replace(src, dst)
        with mock.patch.object(Y.os, 'fsync', fsync), mock.patch.object(Y.os, 'replace', replace):
            Y.save_token(self.NEW)
        self.assertEqual(order, [('fsync', len(self.NEW.encode()), self.OLD), ('replace',)])   # the whole temp file
        self.assertEqual(Y.TOKEN.read_text(encoding='utf-8'), self.NEW)
        self.assertEqual(self.leftovers(), [])

    def test_F40_a_failed_write_keeps_the_old_token(self):
        with mock.patch.object(Y.os, 'fsync', side_effect=OSError(28, 'made-up: disk full')), \
             mock.patch.object(Y.os, 'replace') as replace, self.assertRaises(OSError):
            Y.save_token(self.NEW)
        replace.assert_not_called()
        self.assertEqual(Y.TOKEN.read_text(encoding='utf-8'), self.OLD)
        self.assertEqual(self.leftovers(), [])

    def test_missing_folder_is_made(self):
        Y.TOKEN.unlink()
        Y.TOKEN.parent.rmdir()
        Y.save_token(self.NEW)
        self.assertEqual(Y.TOKEN.read_text(encoding='utf-8'), self.NEW)


class StaleNumbers(Case):
    """F16: once no number was read after a window began, its change is unknown (None), not 'no change'; a
    value older than two reads is marked stale. F43: the same for the limit. F44: d24 is a rolling 24 h."""

    def test_F16_no_reading_inside_the_window_gives_none(self):
        put('youtube', '_account', 'subscribers', T0 - 10 * DAY, 90)
        put('youtube', '_account', 'subscribers', T0 - 3 * DAY, 100)            # the last read, 3 days ago
        tile = stats.tiles(T0)[0]
        self.assertEqual((tile['value'], tile['d24'], tile['d7'], tile['at'], tile['stale']),
                         (100, None, 10, S.iso(T0 - 3 * DAY), True))
        tile = stats.tiles(T0 + 5 * DAY)[0]
        self.assertEqual((tile['d24'], tile['d7'], tile['stale']), (None, None, True))

    def test_F16_stale_only_after_two_missed_reads(self):
        put('instagram', '_account', 'followers', T0 - 2 * DAY, 60)
        put('instagram', '_account', 'followers', T0, 71)
        for later, stale in ((0 * MIN, False), (110 * MIN, False), (111 * MIN, True)):
            tile = {t['id']: t for t in stats.tiles(T0 + later)}['ig_followers']
            self.assertEqual((tile['d24'], tile['stale']), (11, stale), later)
        self.assertEqual({t['id']: t['stale'] for t in stats.tiles(T0 + DAY)}['ig_followers'], True)
        self.assertEqual([t['stale'] for t in stats.tiles(T0 + DAY) if t['value'] is None], [False] * 3)

    def test_F16_a_dead_source_shows_no_change_nowhere(self):
        self.collect()
        self.fake.read_stats = mock.Mock(side_effect=F.FakeError('auth'))
        for k in range(1, 4 * 24):                                                # 4 days of failing reads
            self.collect(T0 + k * HOUR, yt=Broken())
        t = {x['id']: x for x in stats.tiles(self.now)}
        for tid in ('yt_subscribers', 'yt_views', 'ig_followers', 'ig_likes'):
            self.assertEqual((t[tid]['d24'], t[tid]['d7'], t[tid]['stale']), (None, None, True), tid)

    def test_F16_plus_24h_is_none_after_a_day_without_a_youtube_read(self):
        self.collect()
        self.collect(T0 + 2 * DAY, yt=Broken())
        videos = {v['id']: v for v in stats.summary(self.root, T0 + 2 * DAY)['youtube_videos']}
        self.assertEqual((videos['FAKE_yt_013']['views'], videos['FAKE_yt_013']['d24_views']), (1240, None))
        self.collect(T0 + 2 * DAY + 2 * HOUR)                                      # read again: known (0)
        videos = {v['id']: v for v in stats.summary(self.root, T0 + 2 * DAY + 2 * HOUR)['youtube_videos']}
        self.assertEqual(videos['FAKE_yt_013']['d24_views'], 0)

    def test_F43_an_old_limit_is_marked_stale(self):
        self.collect()
        self.fake.read_limit = mock.Mock(side_effect=F.FakeError('transient'))
        self.assertFalse(stats.summary(self.root, T0 + 12 * HOUR)['limit']['stale'])
        for k in range(1, 3 * 4):                                                 # the limit read fails for 2 days
            self.collect(T0 + k * 6 * HOUR)
        lim = stats.summary(self.root, self.now)['limit']
        self.assertEqual(lim, {'used': 0, 'total': 100, 'at': S.iso(T0), 'stale': True})
        src = self.source('instagram_limit')
        self.assertEqual((src['last_ok'], src['error']), (S.iso(T0), 'Instagram did not answer (HTTP 503).'))

    def test_F44_d24_is_the_last_24_hours_not_since_midnight(self):
        for when, value in (('2026-10-28 00:00', 90), ('2026-11-04 00:00', 100), ('2026-11-04 23:00', 105),
                            ('2026-11-05 00:05', 105)):
            put('youtube', '_account', 'subscribers', T(when), value)
        tile = stats.tiles(T('2026-11-05 00:30'))[0]
        self.assertEqual((tile['value'], tile['d24'], tile['d7']), (105, 5, 15))  # the page says "in 24 h", not "today"


class Damaged(Case):
    """F41: a damaged stats.db is moved aside (stats.db.corrupt-<epoch>, with its -wal/-shm) and a new one starts;
    nothing else in WORK is touched; a locked or still-open file is never moved."""

    def others(self) -> dict:
        """{name: bytes} of every file in WORK that is not stats.db's."""
        return {p.name: p.read_bytes() for p in guard.WORK.iterdir() if p.is_file() and not p.name.startswith('stats.db')}

    def setUp(self):
        super().setUp()
        quiet = mock.patch('builtins.print')                                 # the "moved aside" line
        quiet.start()
        self.addCleanup(quiet.stop)

    def corrupt(self) -> list[str]:
        return sorted(p.name for p in guard.WORK.glob('stats.db.corrupt-*'))

    def test_F41_not_a_database_is_moved_aside_and_collect_recovers(self):
        self.collect()
        junk = os.urandom(65536)
        stats._db().write_bytes(junk)
        others = self.others()
        out = stats.summary(self.root, T0)                                   # no error: a new, empty file
        self.assertEqual(list(out), KEYS_8_3)
        self.assertEqual(out['youtube_videos'], [])
        self.assertEqual(self.corrupt(), [f'stats.db.corrupt-{int(T0.timestamp())}'])
        self.assertEqual((guard.WORK / self.corrupt()[0]).read_bytes(), junk)   # kept for a look
        self.assertEqual(self.others(), others)                              # publish.db and the rest untouched
        self.assertEqual(self.collect(T0 + 2 * HOUR), {s: 'ok' for s in SOURCES})
        self.assertEqual({t['id']: t['value'] for t in stats.tiles(T0 + 2 * HOUR)},
                         {'yt_subscribers': 143, 'yt_views': 5230, 'ig_followers': 71, 'ig_likes': 65})

    def test_F41_malformed_pages_are_moved_aside_after_one_failure(self):
        self.collect()
        path = stats._db()
        self.assertFalse(Path(f'{path}-wal').exists() and Path(f'{path}-wal').stat().st_size)
        data = path.read_bytes()
        size = int.from_bytes(data[16:18], 'big')                            # page size; page 1 stays
        path.write_bytes(data[:size] + b'\xa5' * (len(data) - size))
        with self.assertRaises(sqlite3.DatabaseError) as e:
            stats.sources()
        self.assertTrue(stats._broken(e.exception), e.exception)
        self.assertEqual(len(self.corrupt()), 1)
        self.assertEqual(self.collect(T0 + 2 * HOUR), {s: 'ok' for s in SOURCES})
        self.assertEqual(self.source('youtube')['last_ok'], S.iso(T0 + 2 * HOUR))

    def test_F41_locked_is_not_damage(self):
        self.collect()
        before = stats._db().read_bytes()
        locked = sqlite3.OperationalError('database is locked')
        locked.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with self.assertRaises(sqlite3.OperationalError), stats._Con():
            raise locked
        self.assertFalse(stats._broken(locked))
        self.assertEqual((self.corrupt(), stats._db().read_bytes()), ([], before))
        self.assertEqual(self.source('youtube')['last_ok'], S.iso(T0))

    def test_F41_a_file_held_open_is_left_and_tried_again(self):
        self.collect()
        stats._db().write_bytes(b'not a database ' * 4096)
        with mock.patch.object(stats.Path, 'rename', side_effect=PermissionError('in use')), \
             mock.patch('builtins.print') as say, self.assertRaises(sqlite3.DatabaseError):
            stats.sources()
        say.assert_not_called()
        self.assertEqual(self.corrupt(), [])
        with mock.patch('builtins.print') as say:                            # free again: moved, said once
            self.assertEqual(self.collect(T0 + HOUR), {s: 'ok' for s in SOURCES})
            self.collect(T0 + 2 * HOUR)
        self.assertEqual(len(self.corrupt()), 1)
        self.assertEqual(len([c for c in say.call_args_list if 'was damaged' in str(c)]), 1)


class FakeTokenServer:
    """Stands for google.auth.transport.requests.Request: answers the token endpoint with (status, body), or
    raises TransportError when status is None (no internet). Counts the calls."""
    def __init__(self, status, body=b''):
        self.status, self.body, self.calls = status, body, 0

    def __call__(self, url=None, method='GET', body=None, headers=None, timeout=None, **kw):
        from google.auth.exceptions import TransportError
        self.calls += 1
        if self.status is None:
            raise TransportError('made-up: getaddrinfo failed')
        data = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return SimpleNamespace(status=self.status, headers={}, data=data)


class RefreshFailure(unittest.TestCase):
    """F39: only a refused sign-in asks for a login; no internet or a Google error keeps the token. F40: a
    damaged token file signs out cleanly, so the login can repair it."""
    EXPIRED = json.dumps({'token': 'FAKE_ACCESS_OLD', 'refresh_token': 'FAKE_REFRESH',
                          'token_uri': 'https://oauth2.googleapis.com/token', 'client_id': 'FAKE_CLIENT',
                          'client_secret': 'FAKE_SECRET', 'scopes': Y.SCOPES, 'expiry': '2020-01-01T00:00:00Z'})

    def setUp(self):
        guard.reset()
        self.assertTrue(guard._inside(Y.TOKEN, guard.HOME), Y.TOKEN)
        Y.TOKEN.parent.mkdir(parents=True, exist_ok=True)
        Y.TOKEN.write_text(self.EXPIRED, encoding='utf-8')
        import google.auth._exponential_backoff as backoff
        for p in (mock.patch.object(backoff.time, 'sleep', lambda s: None),
                  mock.patch.object(Y, 'InstalledAppFlow', None)):          # a sign-in would fail the test
            p.start()
            self.addCleanup(p.stop)

    def refresh(self, server) -> str:
        with mock.patch.object(Y, 'Request', lambda: server), self.assertRaises(RuntimeError) as e:
            Y.credentials()
        self.assertTrue(server.calls)
        self.assertEqual(Y.TOKEN.read_text(encoding='utf-8'), self.EXPIRED)     # the token file is kept
        return str(e.exception)

    def test_F39_no_internet_is_not_sign_in_needed(self):
        msg = self.refresh(FakeTokenServer(None))
        self.assertEqual(msg, 'YouTube not reachable right now (TransportError); the sign-in is kept')

    def test_F39_google_errors_are_not_sign_in_needed(self):
        for status, body in ((503, b'<html>Service Unavailable</html>'), (503, {'error': 'server_error'}),
                             (500, {'error': 'internal_failure'})):
            msg = self.refresh(FakeTokenServer(status, body))
            self.assertEqual(msg, 'Google could not refresh the YouTube sign-in right now; the sign-in is kept', status)
            self.assertNotIn('quota', msg)                                     # easy.youtube_step's quota branch

    def test_F39_a_refused_sign_in_still_asks_for_a_login(self):
        for body in ({'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'},
                     {'error': 'deleted_client', 'error_description': 'The OAuth client was deleted.'}):
            self.assertEqual(self.refresh(FakeTokenServer(400, body)), 'YouTube sign-in needed: run  python youtube.py login')

    def test_F39_the_stats_source_line_while_offline(self):
        root = fixtures.make_root(guard.TMP / 'stats-root')
        P.set_registry({})
        with mock.patch.object(Y, 'api', guard.REAL['youtube.api']), \
             mock.patch.object(Y, 'Request', lambda: FakeTokenServer(None)):
            self.assertEqual(stats.collect(root, T0)['youtube'],
                             'error: YouTube not reachable right now (TransportError); the sign-in is kept')
        src = next(s for s in stats.sources() if s['source'] == 'youtube')
        self.assertEqual((src['fails'], src['next_try']), (1, S.iso(T0 + HOUR)))
        self.assertNotIn('login', src['error'])

    def test_F40_damaged_token_file_signs_out_cleanly(self):
        class Flow:
            @staticmethod
            def from_client_secrets_file(*a, **k):
                raise LookupError('made-up: the browser sign-in starts')
        for junk in (b'\0' * 200, b'', b'\xff\xfe{"tok'):
            Y.TOKEN.write_bytes(junk)
            with self.assertRaises(RuntimeError) as e:
                Y.credentials()
            self.assertEqual(str(e.exception), 'YouTube sign-in needed: run  python youtube.py login')
            with mock.patch.object(Y, 'InstalledAppFlow', Flow), self.assertRaises(LookupError):
                Y.credentials(interactive=True)                                 # login is not blocked by the file
            self.assertEqual(Y.TOKEN.read_bytes(), junk)


if __name__ == '__main__':
    unittest.main()
