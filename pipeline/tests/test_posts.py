"""posts.py: blog/X/Threads texts of a lesson (no Claude call: the answer is cached in the work folder)."""
import json, tempfile, unittest
from pathlib import Path
import tests  # noqa: F401  (network guard)
import posts

ANSWER = {'blog': {'title': 'Almanca um etwas bitten nasıl kullanılır?', 'slug': 'almanca-um-etwas-bitten',
                   'description': 'Kibarca bir şey istemek.', 'keywords': ['um etwas bitten', 'rica etmek'],
                   'body': 'Giriş.\n\n**Videoyu izleyin:** {VIDEO}\n\n## Kural\n\n| Almanca | Türkçe |\n|---|---|\n'
                           '| Ich bitte dich um Hilfe. | Senden yardım rica ediyorum. |\n\nKanala gelin: {VIDEO}'},
          'x': ['Almancada kibarca nasıl bir şey istenir?', 'bitten um + Akkusativ: Ich bitte um Hilfe → Yardım rica ediyorum.',
                'Videonun tamamı: {VIDEO} #Almanca'],
          'threads': 'Kibarca istemek: bitten um + Akkusativ. {VIDEO}', 'notes': []}
PROMPT = ('header\n\nFinal subtitles of the main video (time  lang  text  [translation]):\n0:00  tr  Merhaba.\n'
          '0:02  de  Ich bitte um Hilfe.  [Yardım rica ediyorum.]\n\nThe attached image shows 12 frames\n')


class Posts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work, self.x = self.tmp / 'work', self.tmp / 'Output' / 'extras'
        (self.work / 'llm').mkdir(parents=True)
        self.x.mkdir(parents=True)
        (self.work / 'llm' / 'posts.answer.json').write_text(json.dumps(ANSWER), encoding='utf-8')
        (self.work / 'llm' / 'texts.prompt.md').write_text(PROMPT, encoding='utf-8')
        (self.x / 'youtube.json').write_text(json.dumps({'titles': ['T'], 'descriptions': ['D'], 'level': 'A2'}),
                                             encoding='utf-8')

    def make(self):
        return posts.make(self.work, self.x, 'um', {'name': 'Kanal'}, posts.cue_block(self.work / 'llm' / 'texts.prompt.md'))

    def test_cue_block_is_the_subtitles_only(self):
        self.assertEqual(posts.cue_block(self.work / 'llm' / 'texts.prompt.md'),
                         '0:00  tr  Merhaba.\n0:02  de  Ich bitte um Hilfe.  [Yardım rica ediyorum.]')

    def test_without_upload_the_link_stays_a_placeholder(self):
        self.assertEqual(self.make(), [])
        self.assertIn('{VIDEO}', (self.x / 'blog.md').read_text(encoding='utf-8'))
        self.assertIn('not filled in yet', (self.x / 'posts.md').read_text(encoding='utf-8'))
        self.assertIsNone(json.loads((self.x / 'posts.json').read_text(encoding='utf-8'))['link'])

    def test_after_upload_every_placeholder_is_the_video_link(self):
        (self.x / 'youtube-applied.json').write_text(json.dumps({'um-video.mp4': {'id': 'abc123'}}), encoding='utf-8')
        self.make()
        for f in ('blog.md', 'posts.md', 'posts.json'):
            text = (self.x / f).read_text(encoding='utf-8')
            self.assertNotIn('{VIDEO}', text, f)
            self.assertIn('https://youtu.be/abc123', text, f)
        blog = (self.x / 'blog.md').read_text(encoding='utf-8')
        self.assertTrue(blog.startswith('---\ntitle: "Almanca um etwas bitten nasıl kullanılır?"'))
        self.assertIn('\n# Almanca um etwas bitten', blog)

    def test_long_posts_become_notes(self):
        long = dict(ANSWER, x=['a' * 281, 'kısa {VIDEO}' + 'b' * 250], threads='c' * 501,
                    blog=dict(ANSWER['blog'], title='#Almanca'))
        (self.work / 'llm' / 'posts.answer.json').write_text(json.dumps(long), encoding='utf-8')
        notes = self.make()
        self.assertIn('X post 1 is 281 characters (max 280)', notes)
        self.assertFalse([n for n in notes if 'post 2' in n])            # 5 + 23 (the link) + 250 = 278: fits
        self.assertTrue(any('Threads post is 501' in n for n in notes))
        self.assertIn('blog title has a "#"', notes)

    def test_relink_fills_the_link_in_after_the_upload_once(self):
        self.make()
        self.assertFalse(posts.relink(self.work, self.x, 'um'))                  # still not on YouTube: nothing to do
        (self.x / 'youtube-applied.json').write_text(json.dumps({'um-video.mp4': {'id': 'abc123'}}), encoding='utf-8')
        self.assertTrue(posts.relink(self.work, self.x, 'um'))
        self.assertIn('https://youtu.be/abc123', (self.x / 'posts.md').read_text(encoding='utf-8'))
        self.assertFalse(posts.relink(self.work, self.x, 'um'))                  # written once, not every 10 min

    def test_relink_without_posts_does_nothing(self):
        self.assertFalse(posts.relink(self.work, self.x, 'um'))
        self.assertFalse((self.x / 'posts.md').exists())

    def test_x_counts_a_link_as_23(self):
        self.assertEqual(posts.x_length('ab https://youtu.be/abcdefghijk'), 3 + 23)
        self.assertEqual(posts.x_length('ab {VIDEO}'), 3 + 23)


if __name__ == '__main__':
    unittest.main()
