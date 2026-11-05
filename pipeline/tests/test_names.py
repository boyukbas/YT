"""Lesson folder names (lessons.py): a finished lesson in a folder named '4' or 'new' gets a short, meaningful name
from its thumbnail words, together with the PC's work folder, only while nothing is being posted; names Sinem chose
are never touched; the finished files keep their names, so records and queue entries still match."""
import json, shutil, unittest
from pathlib import Path
from unittest import mock
from . import fixtures, guard
import easy
import lessons
import paths
import publish as P
import tools


class Names(unittest.TestCase):
    def setUp(self):
        guard.reset()
        base = guard.TMP / 'names-root'
        shutil.rmtree(base, ignore_errors=True)
        self.root = fixtures.make_root(base)
        self.new = self.root / 'new'
        self.work = paths.WORK / 'new'
        (self.work / 'llm').mkdir(parents=True, exist_ok=True)
        (self.work / 'llm' / 'cues.answer.json').write_text('{}', encoding='utf-8')
        idle = mock.patch.object(P, 'current_job', lambda name: None)
        idle.start()
        self.addCleanup(idle.stop)

    def test_placeholders(self):
        for name in ('1', '4', '12', 'new', 'New', 'yeni', 'Yeni klasör', 'New folder (2)', 'neu', 'test 3'):
            with self.subTest(name=name):
                self.assertTrue(lessons.placeholder(name))
        for name in ('Um etwas bitten', 'ZU + INFINITIV', 'Sich interessieren für', '4 Lass uns kennenlernen',
                     'INPUT', 'new words', '2026'):
            with self.subTest(name=name):
                self.assertFalse(lessons.placeholder(name))

    def test_suggested_names(self):
        self.assertEqual(lessons.suggested_name(self.new), 'weil denn')
        four = self.new.rename(self.root / '4')
        self.assertEqual(lessons.suggested_name(four), '4 weil denn')            # her number stays in front
        x = tools.extras(four / 'Output') / 'youtube.json'
        d = json.loads(x.read_text(encoding='utf-8'))
        d['thumbnails'][0]['words'] = ['sich kümmern', 'um +', 'Akkusativ?']
        x.write_text(json.dumps(d), encoding='utf-8')
        self.assertEqual(lessons.suggested_name(four), '4 sich kümmern um Akkusativ')
        d['thumbnails'][0]['words'] = ['x' * 30, 'y' * 30]
        x.write_text(json.dumps(d), encoding='utf-8')
        self.assertEqual(lessons.suggested_name(four), '4 ' + 'x' * 30)          # cut at a word, at most 40

    def test_only_finished_placeholder_lessons(self):
        self.assertEqual([(l.name, n) for l, n in lessons.to_rename(self.root)], [('new', 'weil denn')])
        st = tools.extras(self.new / 'Output') / 'status.json'
        d = json.loads(st.read_text(encoding='utf-8'))
        st.write_text(json.dumps({**d, 'state': 'working'}), encoding='utf-8')
        self.assertEqual(lessons.to_rename(self.root), [])                        # still being made

    def test_rename_moves_the_work_folder_and_keeps_the_files(self):
        before = sorted(f.name for f in (self.new / 'Output').glob('*.mp4'))
        target = lessons.rename(self.new, 'weil denn', log=lambda line: None)
        self.assertEqual(target, self.root / 'weil denn')
        self.assertFalse(self.new.exists())
        self.assertFalse(self.work.exists())
        self.assertTrue((paths.WORK / 'weil-denn' / 'llm' / 'cues.answer.json').exists())
        self.assertEqual(sorted(f.name for f in (target / 'Output').glob('*.mp4')), before)
        self.assertEqual(lessons.file_stem(target), 'new')                        # a new render writes over them
        st = json.loads((tools.extras(target / 'Output') / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual((st['state'], st['renamed_from']), ('done', ['new']))
        self.assertIsNone(P.PostLock.holder())

    def test_refused_while_a_post_is_going_out(self):
        with P.PostLock(owner='dashboard'):
            with self.assertRaises(P.Busy):
                lessons.rename(self.new, 'weil denn', log=lambda line: None)
        with mock.patch.object(P, 'current_job', lambda name: {'state': 'queued'} if name == 'youtube' else None):
            with self.assertRaises(RuntimeError):
                lessons.rename(self.new, 'weil denn', log=lambda line: None)
        self.assertTrue(self.new.exists() and self.work.exists())

    def test_taken_or_bad_names(self):
        with self.assertRaises(FileExistsError):
            lessons.rename(self.new, 'Um etwas bitten', log=lambda line: None)
        for bad in ('', '...', '_hidden', 'x' * 61):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                lessons.rename(self.new, bad, log=lambda line: None)
        self.assertEqual(lessons.clean_name('a/b: c?'), 'a b c')

    def test_drive_folder_in_use_puts_the_work_folder_back(self):
        real = Path.rename

        def in_use(path, target):
            if Path(path) == self.new:
                raise PermissionError(32, 'in use')
            return real(path, target)
        with mock.patch.object(Path, 'rename', in_use), self.assertRaises(PermissionError):
            lessons.rename(self.new, 'weil denn', log=lambda line: None)
        self.assertTrue(self.new.exists() and self.work.exists())
        self.assertFalse((paths.WORK / 'weil-denn').exists())

    def test_watcher_step(self):
        um = self.root / 'Um etwas bitten'
        easy.name_step(um)                                                        # her own name: untouched
        self.assertTrue(um.exists())
        easy.name_step(self.new)
        self.assertTrue((self.root / 'weil denn').exists())
        self.assertIn('folder renamed: "new" -> "weil denn"',
                      (paths.WORK / 'weil-denn' / 'pipeline.log').read_text(encoding='utf-8'))

    def test_watcher_step_waits_when_busy(self):
        with P.PostLock(owner='dashboard'):
            easy.name_step(self.new)
        self.assertTrue(self.new.exists())

    def test_queue_entries_follow_the_renamed_folder(self):
        import platform_instagram as PI
        P.set_registry(None)
        self.addCleanup(P.set_registry, None)
        ig = PI.make()
        key = 'new/Output/new-short-6.mp4'
        item = {i.key: i for i in ig.candidates(self.root)[0]}[key]
        lessons.rename(self.new, 'weil denn', log=lambda line: None)
        moved = {i.key: i for i in ig.candidates(self.root)[0]}['weil denn/Output/new-short-6.mp4']
        self.assertEqual((moved.sig, bool(moved.posted)), (item.sig, bool(item.posted)))
