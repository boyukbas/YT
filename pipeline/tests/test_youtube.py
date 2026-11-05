"""youtube.py's no-spam timing (next_slot) and what a video should look like (wanted); review.py's folder lookup;
the light lesson helpers (lessons.py) and that the dashboard no longer loads the render chain. No network."""
import json, os, shutil, subprocess, sys, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from . import guard
import lessons
import paths
import youtube as Y

IST = paths.ISTANBUL


def frozen(t):
    """youtube.datetime with now() fixed at t (Istanbul)."""
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return t.astimezone(tz) if tz else t.replace(tzinfo=None)
    return mock.patch.object(Y, 'datetime', Fixed)


def at(d, hm):
    h, m = map(int, hm.split(':'))
    return datetime(2026, 11, d, h, m, tzinfo=IST)


def utc(t):
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')        # as youtube-applied.json stores it


class NextSlot(unittest.TestCase):
    """One Short a day at 19:00, after every Short already scheduled in any lesson, never within 3 h of a long video."""
    def setUp(self):
        self.root = guard.TMP / 'yt-root'
        shutil.rmtree(self.root, ignore_errors=True)
        self.out = self.lesson('this', {})

    def lesson(self, name, applied):
        out = self.root / name / 'Output'
        (out / 'extras').mkdir(parents=True)
        (out / 'extras' / Y.APPLIED).write_text(json.dumps(applied), encoding='utf-8')
        return out

    def slot(self, now, settings=None, applied=None, planned=()):
        with frozen(now):
            return Y.next_slot(self.out, applied or {}, settings or {}, planned)

    def test_nothing_scheduled_today_at_19(self):
        self.assertEqual(self.slot(at(5, '11:00')), at(5, '19:00'))

    def test_too_close_to_19_means_tomorrow(self):
        self.assertEqual(self.slot(at(5, '18:45')), at(6, '19:00'))           # under 30 minutes left
        self.assertEqual(self.slot(at(5, '20:00')), at(6, '19:00'))

    def test_after_the_last_short_of_any_lesson(self):
        self.lesson('other', {'other-short-1.mp4': {'publish_at': utc(at(8, '19:00'))}})
        self.assertEqual(self.slot(at(5, '11:00')), at(9, '19:00'))

    def test_planned_shorts_of_this_run_count(self):
        self.assertEqual(self.slot(at(5, '11:00'), planned=[at(6, '19:00')]), at(7, '19:00'))

    def test_every_two_days_and_another_time(self):
        self.lesson('other', {'other-short.mp4': {'publish_at': utc(at(6, '19:00'))}})
        self.assertEqual(self.slot(at(5, '11:00'), {'shorts_every': 2, 'shorts_time': '18:30'}), at(8, '18:30'))

    def test_never_within_3_hours_of_a_long_video(self):
        self.lesson('other', {'other-video.mp4': {'publish_at': utc(at(5, '17:30'))}})
        self.assertEqual(self.slot(at(5, '11:00')), at(5, '20:30'))           # 3 h after the long video
        self.lesson('far', {'far-video.mp4': {'publish_at': utc(at(6, '12:00'))}})
        self.assertEqual(self.slot(at(5, '11:00'), planned=[at(5, '20:30')]), at(6, '19:00'))   # 7 h away: fine

    def test_this_lessons_own_records_count(self):
        own = {'this-short-1.mp4': {'publish_at': utc(at(7, '19:00'))}}
        self.assertEqual(self.slot(at(5, '11:00'), applied=own), at(8, '19:00'))


class Wanted(unittest.TestCase):
    DATA = {'titles': ['T1', 'T2', 'T3'], 'descriptions': ['D1', 'D2'], 'tags': ['a'], 'category': 'Education (27)',
            'playlist': 'P', 'pinned_comment': ' Soru? \n'}

    def test_defaults(self):
        w = Y.wanted(self.DATA, {})
        self.assertEqual((w['title'], w['description'], w['categoryId'], w['thumbnail'], w['comment']),
                         ('T1', 'D1', '27', 'thumbnail-1.jpg', 'Soru?'))
        self.assertEqual((w['madeForKids'], w['paid'], w['ai'], w['public'], w['defaultLanguage']), (False, False, False, False, 'tr'))

    def test_choices_are_clamped(self):
        self.assertEqual(Y.wanted(self.DATA, {'title': 9, 'description': 0})['title'], 'T3')
        self.assertEqual(Y.wanted(self.DATA, {'title': 9, 'description': 0})['description'], 'D1')
        self.assertEqual(Y.wanted(self.DATA, {'title': 2, 'thumbnail': 3, 'ai': True})['title'], 'T2')

    def test_title_is_cut_at_youtubes_100(self):
        self.assertEqual(len(Y.wanted(dict(self.DATA, titles=['x' * 130]), {})['title']), 100)


class Folders(unittest.TestCase):
    def setUp(self):
        self.lesson = guard.TMP / 'folders' / 'Lesson A'
        shutil.rmtree(self.lesson.parent, ignore_errors=True)
        self.lesson.mkdir(parents=True)

    def test_finished_output_prefers_the_easy_folder_with_the_record(self):
        self.assertIsNone(paths.finished_output(self.lesson))
        (self.lesson / 'Output' / 'extras').mkdir(parents=True)
        self.assertIsNone(paths.finished_output(self.lesson))                  # someone else's Output folder
        (self.lesson / 'Output (Easy)' / 'extras').mkdir(parents=True)
        (self.lesson / 'Output (Easy)' / 'extras' / 'status.json').write_text('{}', encoding='utf-8')
        self.assertEqual(paths.finished_output(self.lesson), self.lesson / 'Output (Easy)')
        self.assertEqual(lessons.output_dir(self.lesson), self.lesson / 'Output (Easy)')

    def test_review_uses_the_test_work_folder(self):
        import review
        self.assertEqual(review.WORK_ROOT, guard.WORK)                         # it used to hard-code D:\YT\easy-work
        self.assertEqual(review.output(self.lesson), self.lesson / 'Output')

    def test_retry_marker_is_in_the_work_folder(self):
        self.assertEqual(lessons.retry_marker(self.lesson), paths.WORK / 'lesson-a' / 'retry')

    def test_dashboard_and_overview_do_not_load_the_render_chain(self):
        code = ('import sys, tests, dashboard, overview, publish, stats, platform_instagram; '
                "print(sorted(m for m in ('easy', 'cv2', 'render', 'layout', 'speech', 'thumbnail') if m in sys.modules))")
        r = subprocess.run([sys.executable, '-I', '-c', f'import sys; sys.path.insert(0, {str(Path(__file__).parents[1])!r}); ' + code],
                           capture_output=True, text=True, env=dict(os.environ), timeout=120)
        self.assertEqual(r.stdout.strip().splitlines()[-1:], ['[]'], r.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
