"""layout.studio: the picture card for vertical and horizontal recordings, and easy.use_studio's on/off rules."""
from . import guard  # noqa: F401 (no network, no programs)

import unittest

import easy
import layout as L

V, H = (1080, 1920), (1920, 1080)


def face(cx, cy=500, w=150):
    return (cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2)


class Vertical(unittest.TestCase):
    def test_whole_width_and_the_same_card_as_before(self):
        s = L.studio('video', *V, [face(540, 700)] * 5)
        ch = int(1920 * L.STUDIO_CROP)
        self.assertEqual((s.crop[0], s.crop[2], s.crop[3]), (0, 1080, ch))
        self.assertEqual(s.pic[2], int(int(0.926 * 1080) * 1080 / ch))
        self.assertTrue(s.studio)
        self.assertEqual(s.panel, (110 + s.pic[2] + 90, 1850))


class Horizontal(unittest.TestCase):
    ch = int(1080 * L.STUDIO_CROP)

    def test_still_face_gives_the_vertical_cards_shape_around_her(self):
        s = L.studio('video', *H, [face(1100)] * 9)
        x, _, w, h = s.crop
        self.assertEqual((w, h), (int(0.72 * self.ch), self.ch))
        self.assertAlmostEqual(x + w / 2, 1100, delta=1)
        self.assertAlmostEqual(s.pic[2] / s.pic[3], w / h, places=2)
        self.assertGreaterEqual(s.panel[1] - s.panel[0], 700)              # room for the board and subtitles

    def test_a_moving_face_widens_the_card_up_to_the_limit(self):
        some = L.studio('video', *H, [face(900 + 25 * i) for i in range(9)])        # 200 px of movement
        lots = L.studio('video', *H, [face(400 + 150 * i) for i in range(9)])       # all over the frame
        self.assertGreater(some.crop[2], int(0.72 * self.ch))
        self.assertLess(some.crop[2], int(0.9 * self.ch))
        for k in range(8):           # her face stays inside, with room (the last 10 %, k=8, is trimmed as a stray)
            self.assertLessEqual(some.crop[0], 900 + 25 * k - 75 - 150)
            self.assertGreaterEqual(some.crop[0] + some.crop[2], 900 + 25 * k + 75 + 150)
        self.assertEqual(lots.crop[2], int(0.9 * self.ch))

    def test_one_stray_detection_does_not_widen_it(self):
        s = L.studio('video', *H, [face(1100)] * 19 + [face(200)])
        self.assertEqual(s.crop[2], int(0.72 * self.ch))

    def test_inside_the_frame_at_the_edges(self):
        for cx in (60, 1880):
            x, _, w, _ = L.studio('video', *H, [face(cx)] * 5).crop
            self.assertTrue(0 <= x and x + w <= 1920, (cx, x, w))

    def test_no_face_found_gives_the_middle(self):
        x, _, w, _ = L.studio('video', *H, [None, None]).crop
        self.assertEqual(x, (1920 - w) // 2)


class UseStudio(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(easy.use_studio({}, {}, landscape=False))                     # vertical: on by default
        self.assertFalse(easy.use_studio({'studio': False}, {}, landscape=False))
        self.assertFalse(easy.use_studio({}, {}, landscape=True))                     # horizontal: off by default
        self.assertTrue(easy.use_studio({'studio_landscape': True}, {}, landscape=True))
        self.assertTrue(easy.use_studio({}, {'studio': True}, landscape=True))        # the lesson decides
        self.assertFalse(easy.use_studio({'studio_landscape': True}, {'studio': False}, landscape=True))
        self.assertFalse(easy.use_studio({}, {'studio': False}, landscape=False))


if __name__ == '__main__':
    unittest.main()
