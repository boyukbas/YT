"""A made-up Drive root with the real layout, using made-up ids only (tests and dashboard --dev --fixtures).

Build roots under guard.TMP (or a scratch folder), never under guard.WORK: guard.reset() empties WORK. A root
outside TMP is registered with the guard, so the fake probe answers for its videos too. Nothing here writes
under '<letter>:\\My Drive', D:\\YT\\easy-work or D:\\code\\YT (ValueError)."""
from __future__ import annotations
import json, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from . import guard
import tools

IST = timezone(timedelta(hours=3))                       # Istanbul: fixed UTC+3 (no tz database on the PC)
POSTED = 'instagram-posted.json'
APPLIED = 'youtube-applied.json'
REFUSED = (Path(r'D:\YT\easy-work'), Path(r'D:\code\YT'))


def answer(seconds, vertical=True, hdr=False, created=''):
    """One tools.probe() result."""
    w, h = (1080, 1920) if vertical else (1920, 1080)
    return {'duration': float(seconds), 'width': w, 'height': h, 'fps': 30.0, 'hdr': hdr,
            'transfer': 'arib-std-b67' if hdr else 'bt709', 'audio': True, 'created': created}


NEW_SECONDS = (51.03, 41.03, 45.03, 27.03, 46.03, 30.03, 34.03, 48.03, 53.03)       # new-short-1..9
PROBES: dict[str, dict] = {                # file name -> tools.probe() result; write_lesson() adds its own files
    **{f'new-short-{n}.mp4': answer(s) for n, s in enumerate(NEW_SECONDS, 1)},
    'new-video.mp4': answer(392.4, vertical=False),
    'um-etwas-bitten-video.mp4': answer(68.04),
    'sich-interessieren-f-r-video.mp4': answer(51.04),
    'zu-infinitiv-video.mp4': answer(197.2, vertical=False),
    'zu-infinitiv-short.mp4': answer(44.6),              # the hand-made Short (not a pipeline file)
}
_BASE = {k: dict(v) for k, v in PROBES.items()}

# What make_root() gives (keys as in spec 5.1), for the regression checks
WAITING = ('new/Output/new-short-6.mp4', 'new/Output/new-short-7.mp4', 'new/Output/new-short-8.mp4',
           'new/Output/new-short-9.mp4', 'Um etwas bitten/Output/um-etwas-bitten-video.mp4',
           'Sich interessieren für/Output/sich-interessieren-f-r-video.mp4')
POSTED_KEYS = tuple(f'new/Output/new-short-{n}.mp4' for n in range(1, 6))
NEW_POSTED = (datetime(2026, 11, 2, 14, 20, 7, 118000, IST), datetime(2026, 11, 2, 20, 3, 41, 530000, IST),
              datetime(2026, 11, 3, 10, 15, 52, 77000, IST), datetime(2026, 11, 3, 20, 2, 30, 904000, IST),
              datetime(2026, 11, 4, 16, 46, 12, 412000, IST))         # new-short-1..5 on Instagram
LAST_POST = NEW_POSTED[-1].astimezone(timezone.utc)                  # Wed 4 Nov 16:46: 1 post in the last 24 h
NOW = LAST_POST + timedelta(hours=5, minutes=54)                     # the owner's regression: 22:40 Istanbul


def _yt(n):
    """A made-up YouTube video id (11 characters, like a real one)."""
    return f'FAKE_yt_{n:03d}'


# ---------- made-up texts (youtube.json) ----------

_NEW_TITLE = "Almanca 'weil' mi 'denn' mi? En Sık Yapılan Hata"
_NEW_SHORTS = [   # title, first description line, cover words, cover question, kind
    ('weil fiili sona atar', "'weil' ile fiil sona gider: Ich bleibe zu Hause, weil ich müde bin.",
     ['weil'], 'FİİL NEREDE?', 'rule'),
    ('denn ile cümle kurmak', "'denn' fiili yerinde bırakır: Ich bleibe zu Hause, denn ich bin müde.",
     ['denn'], 'NASIL?', 'rule'),
    ('deshalb ile sonuç bildirmek', "'deshalb' ile fiil ikinci sırada: Deshalb bleibe ich zu Hause.",
     ['deshalb'], 'SONUÇ', 'rule'),
    ('weil mi denn mi? Hızlı test', 'İki cümleyi birleştir: Ich lerne Deutsch. Ich will in Berlin arbeiten.',
     ['weil', 'denn'], 'HANGİSİ?', 'exam'),
    ('Bu hatayı yapma: weil + fiil', 'Yanlış: weil ich bin müde. Doğru: weil ich müde bin.',
     ['weil'], 'BUNU DEME!', 'mistake'),
    ('da ile resmi sebep', "Resmi yazılarda 'da': Da ich krank war, konnte ich nicht kommen.",
     ['da'], 'RESMİ Mİ?', 'rule'),
    ("nämlich: konuşmada 'çünkü' #almanca", "'nämlich' fiilden sonra gelir: Ich bin nämlich krank.",   # a hashtag
     ['nämlich'], 'BİLİYOR MUSUN?', 'life'),                                                         # to strip
    ('darum ve deswegen', "'darum' ve 'deswegen' = 'deshalb': Darum lerne ich Deutsch.",
     ['darum'], 'AYNI MI?', 'rule'),
    ('Sebep sorusu: Warum?', "'Warum?' sorusuna 'Weil …' ile cevap verilir.", ['Warum?'], 'NEDEN?', 'rule'),
]
NEW_TEXTS = {
    'titles': [_NEW_TITLE, "weil, denn, deshalb: Almanca'da Sebep Bildirmek", "Almanca'da 'çünkü' Nasıl Denir? 3 Kalıp"],
    'short_title': '',
    'shorts': [{'version': f'short-{k}', 'title': t,
                'description': f'{line}\nDersin tamamı kanalda: {_NEW_TITLE}\n#Shorts #almanca #deutschlernen',
                'cover': {'frame': k, 'words': words, 'question': q}, 'kind': kind}
               for k, (t, line, words, q, kind) in enumerate(_NEW_SHORTS, 1)],
    'descriptions': [
        "Almanca'da 'çünkü' demenin üç yolu var: weil, denn ve deshalb. Hangisinde fiil sona gider?\n\n"
        'Ich bleibe zu Hause, weil ich müde bin. (Evde kalıyorum, çünkü yorgunum.)\n'
        'Ich bleibe zu Hause, denn ich bin müde. (Evde kalıyorum, çünkü yorgunum.)\n\n'
        '⏱️ Bölümler:\n0:00 Giriş\n0:48 weil\n2:50 denn\n4:50 deshalb\n\n'
        '🔔 Her hafta yeni Almanca dersi için abone olun.\n#almanca #almancaöğren #deutschlernen',
        'weil, denn ve deshalb: cümlede fiil nereye gider? Örneklerle.\n\n'
        '⏱️ Bölümler:\n0:00 Giriş\n0:48 weil\n2:50 denn\n4:50 deshalb\n\n#almanca #deutschlernen'],
    'chapters': [{'t': 0, 'title': 'Giriş'}, {'t': 48, 'title': 'weil'}, {'t': 170, 'title': 'denn'},
                 {'t': 290, 'title': 'deshalb'}],
    'tags': ['almanca weil', 'almanca denn', 'weil denn farkı', 'almanca bağlaçlar', 'deutsch lernen'],
    'pinned_comment': 'Bu cümleyi düzelt: Ich lerne Deutsch, weil ich will in Berlin arbeiten.',
    'level': 'A2', 'playlist': 'Almanca Bağlaçlar',
    'thumbnails': [{'frame': 2, 'words': ['weil', 'denn'], 'question': 'FARKI NE?'},
                   {'frame': 5, 'words': ['weil'], 'question': 'BUNU DEME!'},
                   {'frame': 8, 'words': ['deshalb'], 'question': '3 KALIP'}],
    'notes': [],
}
UM_TEXTS = {
    'titles': ["Almanca'da Kibarca Rica: um etwas bitten", 'Ich bitte dich um …: Rica Etmek',
               "'bitten um' Nasıl Kullanılır?"],
    'short_title': '', 'shorts': [],
    'descriptions': ["Almanca'da bir şey rica ederken 'jemanden um etwas bitten' kullanılır.\n"
                     'Ich bitte dich um Hilfe. (Senden yardım rica ediyorum.)\n'
                     '🔔 Her hafta yeni ders için abone olun.\n#almanca #deutschlernen #Shorts',
                     'bitten + um + Akkusativ: Darf ich Sie um einen Gefallen bitten?\n#almanca #Shorts'],
    'chapters': [], 'tags': ['um etwas bitten', 'almanca rica', 'bitten um'],
    'pinned_comment': 'Siz Almanca nasıl rica ediyorsunuz? Yorumlara yazın.',
    'level': 'A2', 'playlist': 'Almanca Fiiller',
    'thumbnails': [{'frame': 1, 'words': ['bitten', 'um'], 'question': 'NASIL?'},
                   {'frame': 3, 'words': ['um Hilfe'], 'question': 'KİBAR MI?'},
                   {'frame': 4, 'words': ['bitten'], 'question': 'RİCA'}],
    'notes': [],
}
SICH_TEXTS = {
    'titles': ["Almanca 'sich interessieren für' Nasıl Kullanılır?", 'Ich interessiere mich für …',
               'für mu, an mı? sich interessieren'],
    'short_title': '', 'shorts': [],
    'descriptions': ["'sich interessieren für' + Akkusativ: Ich interessiere mich für Musik. "
                     '(Müziğe ilgi duyuyorum.)\n🔔 Her hafta yeni ders için abone olun.\n#almanca #deutschlernen #Shorts',
                     'Ich interessiere mich für Fußball. (Futbolla ilgileniyorum.)\n#almanca #Shorts'],
    'chapters': [], 'tags': ['sich interessieren für', 'almanca ilgilenmek'],
    'pinned_comment': 'Siz neyle ilgileniyorsunuz? Almanca yazın: Ich interessiere mich für …',
    'level': 'A2', 'playlist': 'Almanca Fiiller',
    'thumbnails': [{'frame': 2, 'words': ['für'], 'question': 'AN MI?'},
                   {'frame': 1, 'words': ['sich', 'interessieren'], 'question': 'NASIL?'},
                   {'frame': 5, 'words': ['für'], 'question': 'FARKI NE?'}],
    'notes': [],
}
ZU_TEXTS = {
    'titles': ["Almanca zu + Infinitiv: Ne Zaman 'zu' Kullanılır?", 'zu + Infinitiv: 3 Kural, Bol Örnek',
               "Almanca'da 'zu' Hatası Yapma"],
    'short_title': '', 'shorts': [],
    'descriptions': ["Almanca'da 'zu + Infinitiv' ne zaman kullanılır?\n\n"
                     'Es ist schwer, Deutsch zu lernen. (Almanca öğrenmek zor.)\n\n'
                     '⏱️ Bölümler:\n0:00 Giriş\n0:35 zu + Infinitiv nedir?\n1:50 Örnekler\n\n#almanca #deutschlernen',
                     "'zu' ile mastar: Ich habe keine Zeit, ins Kino zu gehen.\n\n#almanca"],
    'chapters': [{'t': 0, 'title': 'Giriş'}, {'t': 35, 'title': 'zu + Infinitiv nedir?'},
                 {'t': 110, 'title': 'Örnekler'}],
    'tags': ['zu infinitiv', 'almanca zu', 'almanca mastar'],
    'pinned_comment': "Bu cümleyi 'zu' ile kurun: Ich habe keine Lust …",
    'level': 'B1', 'playlist': 'Almanca Dilbilgisi',
    'thumbnails': [{'frame': 3, 'words': ['zu +', 'Infinitiv'], 'question': 'NE ZAMAN?'},
                   {'frame': 6, 'words': ['zu'], 'question': '3 KURAL'},
                   {'frame': 9, 'words': ['zu +', 'Infinitiv'], 'question': 'BUNU DEME!'}],
    'notes': [],
}
CHANNEL = {
    'name': 'Örnek Kanal', 'handle': '@example', 'primary': 'tr', 'secondary': 'de',
    'logo': 'logo.png', 'subscribe': 'subscribe.png', 'subtitles': {'long': 'top', 'short': 'bottom'},
    'badge_suffix': 'ALMANCA',
    'youtube': {'language': 'tr', 'audio_language': 'tr', 'hashtags': ['#almanca', '#almancaöğren', '#deutschlernen'],
                'series': {'mistake': "Almanca'da Sık Yapılan Hatalar", 'life': '', 'exam': '', 'rule': ''},
                'playlists': ['Almanca Bağlaçlar', 'Almanca Fiiller', 'Almanca Dilbilgisi']},
    'palette': {name: {'hex': hex_} for name, hex_ in (('red', '#E30A17'), ('yellow', '#FFD21A'), ('white', '#FFFFFF'),
                                                      ('navy', '#101628'), ('ink', '#15171C'), ('soft', '#E8EAF0'))},
    'instagram': {'start': '2026-11-03'},
}

# The fake YouTube's answers (fake_youtube_api(YT_VIDEOS, YT_CHANNEL)) for the ids in make_root()'s records
_PUBLIC_AT = ('2026-10-31T16:00:00Z', '2026-11-01T16:00:00Z', '2026-11-02T16:00:00Z', '2026-11-03T16:00:00Z',
              '2026-11-04T16:00:00Z')                                  # new-short-1..5 (19:00 Istanbul)
YT_VIDEOS = {
    _yt(10): {'views': 812, 'likes': 44, 'comments': 7, 'privacy': 'public', 'title': _NEW_TITLE,
              'published': '2026-10-31T13:00:00Z'},
    **{_yt(k): {'views': 420 - 40 * k, 'likes': 30 - 3 * k, 'comments': k % 3,
                'privacy': 'public' if k <= 5 else 'private',
                'title': re.sub(r'\s*#\w+', '', _NEW_SHORTS[k - 1][0]).strip(),
                'published': _PUBLIC_AT[k - 1] if k <= 5 else '2026-10-30T16:20:00Z'} for k in range(1, 9)},
    _yt(11): {'views': 95, 'likes': 9, 'comments': 2, 'privacy': 'public', 'title': UM_TEXTS['titles'][0],
              'published': '2026-11-04T17:15:00Z'},
    _yt(12): {'views': 0, 'likes': 0, 'comments': 0, 'privacy': 'private', 'title': SICH_TEXTS['titles'][0],
              'published': '2026-11-04T18:02:00Z'},
    _yt(13): {'views': 1240, 'likes': 61, 'comments': 9, 'privacy': 'public', 'title': ZU_TEXTS['titles'][0],
              'published': '2026-10-29T17:00:00Z'},
}
YT_CHANNEL = {'subscribers': 143, 'views': 5230, 'videos': 25}


# ---------- the root ----------

def make_root(base: Path) -> Path:
    """Build base/'SINEM-YouTube' as it was on 2026-11-04 and return it:
    - 'new': Output with new-short-1..9 (shorts 6-9 are 30, 34, 48, 53 s) and new-video.mp4 (392 s horizontal);
      covers; shorts 1-5 posted (ids FAKE_MEDIA_1..5); status done.
    - 'Um etwas bitten' and 'Sich interessieren für': one-clip vertical videos of 68 s and 51 s, with
      thumbnail-short.jpg. Um's video is older than Sich's, but Um's youtube.json is newer (the old order bug).
    - 'ZU + INFINITIV': a hand-made Output (zu-infinitiv-short.mp4, no extras), an Output (Easy) with a 197 s
      horizontal video, and settings.json {"short": false}.
    - '_channel/channel.json' {"instagram": {"start": "2026-11-03"}} and 'INPUT' (images only).
    Videos are zero-byte placeholders; covers are small valid JPEGs made with Pillow.
    YouTube (youtube-applied.json, ZU's hand-made note): new-short-9 ready; new-short-8 and Sich uploaded (private);
    new-short-6 and 7 scheduled 1 and 2 days after the build (so they stay scheduled); the other 8 public.
    Calling it again on the same base rebuilds the root in place."""
    root = _safe(base) / 'SINEM-YouTube'
    root.mkdir(parents=True, exist_ok=True)
    _register(root)
    PROBES.update({k: dict(v) for k, v in _BASE.items()})
    ch = root / '_channel'
    _json(ch / 'channel.json', CHANNEL, _ts(2026, 11, 2, 15, 33))
    _image(ch / 'logo.png', (32, 32))
    _image(ch / 'subscribe.png', (64, 16))
    (ch / 'fonts').mkdir(exist_ok=True)
    (ch / 'fonts' / 'README.txt').write_text('Fonts for the videos go here (none in the fixture).\n', encoding='utf-8')
    _image(root / 'INPUT' / 'board-1.jpg', (160, 90))
    _image(root / 'INPUT' / 'board-2.png', (160, 90))
    (root / 'STATUS.txt').write_text('STATUS (made-up fixture root)\n', encoding='utf-8')

    zu = _lesson(root, 'ZU + INFINITIV', whole=(197.2, False), output='Output (Easy)', texts=ZU_TEXTS,
                 video_mtime=_ts(2026, 10, 31, 19, 20), youtube=(f'Made by hand: https://youtu.be/{_yt(13)}', True))
    _video(zu.parent / 'Output' / 'zu-infinitiv-short.mp4', _ts(2026, 10, 29, 22, 10))
    _json(zu.parent / 'settings.json', {'short': False}, _ts(2026, 10, 29, 21, 0))

    new = _lesson(root, 'new', shorts=9, posted=range(1, 6), whole=(392.4, False), texts=NEW_TEXTS,
                  video_mtime=_ts(2026, 10, 30, 18, 40), posted_at=NEW_POSTED,
                  youtube=(' | '.join([f'Video https://youtu.be/{_yt(10)}: done'] +
                                      [f'Short https://youtu.be/{_yt(k)}: done' for k in range(1, 8)] +
                                      [f'Short https://youtu.be/{_yt(8)} · up to date', 'Short: not uploaded yet',
                                       'In Studio: pin the comment, end screen']), False))
    rec = json.loads((tools.extras(new) / POSTED).read_text(encoding='utf-8'))
    for k in (1, 2):                                     # the Facebook Page still got Reels until 2026-11-03
        at = NEW_POSTED[k - 1] + timedelta(minutes=3)
        rec[f'new-short-{k}.mp4']['facebook'] = {'id': f'FAKE_FB_REEL_{k}', 'permalink':
                                                 f'https://www.facebook.com/reel/FAKE_FB_{k}/', **_stamp(at)}
    rec['_facebook'] = {'long': {'id': 'FAKE_FB_VIDEO_1', 'permalink': 'https://www.facebook.com/reel/FAKE_FB_9/',
                                 **_stamp(datetime(2026, 11, 3, 12, 1, 44, 250000, IST))}}
    _json(tools.extras(new) / POSTED, rec, LAST_POST.timestamp())

    um = _lesson(root, 'Um etwas bitten', whole=(68.04, True), texts=UM_TEXTS, video_mtime=_ts(2026, 11, 4, 16, 54, 51),
                 json_mtime=_ts(2026, 11, 4, 18, 49, 11),
                 youtube=(f'Video https://youtu.be/{_yt(11)}: done | In Studio: pin the comment, end screen', True))
    sich = _lesson(root, 'Sich interessieren für', whole=(51.04, True), texts=SICH_TEXTS,
                   video_mtime=_ts(2026, 11, 4, 18, 3, 4), json_mtime=_ts(2026, 11, 4, 18, 4, 49),
                   youtube=(f'Video https://youtu.be/{_yt(12)} · set: details · later: comment (posted when the video '
                            'is public) | In Studio: pin the comment, end screen', False))

    soon = lambda days: f'{(datetime.now(timezone.utc) + timedelta(days=days)).date()}T16:00:00Z'
    filled = lambda n: {'captions_tr': f'{n:012x}', 'captions_de': f'{n + 500:012x}'}
    shorts = {f'new-short-{k}.mp4': {'id': _yt(k), 'publish_at': at, **filled(k), 'playlist': 'FAKE_PLAYLIST_2',
                                     'updated': '2026-10-30 19:30', 'complete': True, 'public': False}
              for k, at in zip(range(1, 8), _PUBLIC_AT + (soon(1), soon(2)))}
    _json(tools.extras(new) / APPLIED, {
        'new-video.mp4': {'id': _yt(10), 'publish_at': '2026-10-31T13:00:00Z', 'thumbnail': f'{10:012x}', **filled(10),
                          'playlist': 'FAKE_PLAYLIST_1', 'comment': 'FAKE_COMMENT_1', 'updated': '2026-10-31 16:05',
                          'complete': True, 'public': True},
        **shorts,
        'new-short-8.mp4': {'id': _yt(8), **filled(8), 'updated': '2026-11-04 21:10', 'complete': False,
                            'public': False}}, _ts(2026, 11, 4, 21, 10))
    _json(tools.extras(um) / APPLIED, {
        'um-etwas-bitten-video.mp4': {'id': _yt(11), 'publish_at': '2026-11-04T17:15:00Z', 'thumbnail': f'{11:012x}',
                                      **filled(11), 'playlist': 'FAKE_PLAYLIST_3', 'comment': 'FAKE_COMMENT_2',
                                      'updated': '2026-11-04 20:16', 'complete': True, 'public': True}},
          _ts(2026, 11, 4, 20, 16))
    _json(tools.extras(sich) / APPLIED, {
        'sich-interessieren-f-r-video.mp4': {'id': _yt(12), 'thumbnail': f'{12:012x}', **filled(12),
                                             'updated': '2026-11-04 21:05', 'complete': False, 'public': False}},
          _ts(2026, 11, 4, 21, 5))
    return root


def write_lesson(root: Path, name: str, *, shorts: int = 0, posted: tuple[int, ...] = (),
                 whole: tuple[int, bool] | None = None, state: str = 'done', output: str = 'Output',
                 video_mtime: float | None = None) -> Path:
    """One lesson folder. whole = (seconds, vertical) for a one-clip lesson video. Returns its output folder.
    shorts=N adds <slug>-short-1..N.mp4 with covers (and a 300 s horizontal lesson video when whole is None).
    posted: part numbers on Instagram, 0 = the whole lesson video; ids continue the root's FAKE_MEDIA_<n>.
    state: 'done', 'working', 'failed' or 'waiting' (status.json; the files are there either way), or 'new'
    (only the recording, no output folder). output='Output (Easy)' also makes an empty hand-made 'Output' (the
    pipeline uses Output (Easy) only next to one). video_mtime: the videos' mtime (epoch s), which decides the order.
    The probe answers go into PROBES by file name: the lesson video gets `whole`, a Short keeps an answer already
    there. So a lesson named like a make_root() lesson changes that lesson's answers too (make_root() puts them back)."""
    if state not in ('done', 'working', 'failed', 'waiting', 'new'):
        raise ValueError(f'state {state!r}')
    return _lesson(Path(root), name, shorts=shorts, posted=posted, whole=whole, state=state, output=output,
                   video_mtime=video_mtime)


def _lesson(root, name, *, shorts=0, posted=(), whole=None, state='done', output='Output', video_mtime=None,
            texts=None, json_mtime=None, posted_at=None, youtube=('', False)):
    """write_lesson(), plus the details make_root() sets: texts, youtube.json mtime, post times, status note."""
    root = _safe(root)
    _register(root)
    lesson, slug = root / name, tools.slug(name)
    out, x = lesson / output, tools.extras(lesson / output)
    whole = whole or ((300.0, False) if shorts else None)            # a Shorts lesson always has its lesson video
    posted = tuple(posted)
    if any(not 0 <= n <= shorts or (n == 0 and not whole) for n in posted):
        raise ValueError(f'posted {posted}: parts are 1..{shorts}, 0 is the whole lesson video')
    t0 = video_mtime if video_mtime is not None else time.time()
    tj = json_mtime if json_mtime is not None else t0 + 600
    take = lesson / f'{slug}-take-1.mov'                             # the recording Sinem put in the lesson folder
    _video(take, t0 - 3600)
    if output == 'Output (Easy)':                                    # the pipeline writes there only next to a
        (lesson / 'Output').mkdir(exist_ok=True)                     # hand-made Output (easy.output_dir)
    PROBES[take.name] = answer((whole or (120, True))[0] + 60, (whole or (0, True))[1], hdr=True,
                               created=datetime.fromtimestamp(t0 - 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    if state == 'new':
        return out
    files, vertical = {}, False
    if whole:
        f = out / f'{slug}-video.mp4'
        _video(f, t0)
        PROBES[f.name] = answer(*whole)
        vertical = bool(whole[1])
        files[f.name] = _about(PROBES[f.name])
    for k in range(1, shorts + 1):
        f = out / f'{slug}-short-{k}.mp4'
        _video(f, t0 + 60 * k)
        PROBES.setdefault(f.name, answer(30 + 4 * k))                 # make_root()'s own lengths win
        files[f.name] = _about(PROBES[f.name])
        _image(x / f'cover-short-{k}.jpg', (90, 160), k, tj)
    for i in (1, 2, 3):
        _image(x / f'thumbnail-{i}.jpg', (160, 90), i, tj)
        files[f'thumbnail-{i}.jpg'] = f'16:9 thumbnail, option {i}'
    if shorts or vertical:
        _image(x / 'thumbnail-short.jpg', (90, 160), 0, tj)
        files['thumbnail-short.jpg'] = 'vertical cover for the Short'
    data = texts or _texts(name, shorts)
    _json(x / 'youtube.json', {**data, 'category': 'Education (27)', 'defaultLanguage': 'tr',
                               'defaultAudioLanguage': 'tr', 'madeForKids': False, 'files': files}, tj)
    step = {'done': ', '.join(n for n in files if n.endswith('.mp4')), 'working': 'Rendering the video',
            'failed': 'Stopped', 'waiting': 'Needs a Claude answer: texts'}.get(state, state)
    st = {'state': state, 'step': step, 'updated': _ist(tj), 'inputs': [[take.name, 0]],
          'error': 'RuntimeError: made-up failure (tests)' if state == 'failed' else '',
          'youtube': youtube[0], 'youtube_complete': youtube[1]}
    if state == 'done':
        st['report'] = {'placement': {}, 'qa': {}, 'notes': []}
    _json(x / 'status.json', st, tj)
    human = {'working': 'Working', 'done': 'Ready', 'failed': 'Failed', 'waiting': 'Waiting'}[state]
    (x / 'STATUS.txt').write_text(f'{human}: {step}\nUpdated {st["updated"]}\n', encoding='utf-8')  # as tools.Status
    (x / POSTED).unlink(missing_ok=True)
    if posted:
        first = 1 + max((int(m.group(1)) for _, o, p in tools.records(root, POSTED) if o != out
                         for r in _jload(p).values() if isinstance(r, dict)
                         for m in [re.fullmatch(r'FAKE_MEDIA_(\d+)', str(r.get('id', '')))] if m), default=0)
        recs = {}
        for i, n in enumerate(posted):
            at = posted_at[i] if posted_at else datetime(2026, 10, 20, 20, 5, tzinfo=IST) + timedelta(days=i)
            f = f'{slug}-video.mp4' if n == 0 else f'{slug}-short-{n}.mp4'
            recs[f] = {'id': f'FAKE_MEDIA_{first + i}', 'permalink': f'https://www.instagram.com/reel/FAKE{first + i}/',
                       **_stamp(at)}
        _json(x / POSTED, recs, max(_stamp_ts(r) for r in recs.values()))
    return out


def _texts(name, shorts):
    """youtube.json texts for a write_lesson() lesson."""
    title = f'{name}: Almanca ders'
    return {'titles': [title, f'{name} (2)', f'{name} (3)'], 'short_title': '',
            'shorts': [{'version': f'short-{k}', 'title': f'{name} · Short {k}',
                        'description': f'Örnek {k}: Ich lerne Deutsch.\nDersin tamamı kanalda: {title}\n#Shorts #almanca',
                        'cover': {'frame': k, 'words': ['Deutsch'], 'question': 'NEDEN?'}, 'kind': 'rule'}
                       for k in range(1, shorts + 1)],
            'descriptions': [f'{title}.\nIch lerne Deutsch. (Almanca öğreniyorum.)\n#almanca #deutschlernen'] * 2,
            'chapters': [], 'tags': ['almanca', 'deutsch lernen'], 'pinned_comment': 'Siz hangi seviyedesiniz?',
            'level': 'A2', 'playlist': 'Test Dersleri',
            'thumbnails': [{'frame': k, 'words': ['Deutsch'], 'question': 'NEDEN?'} for k in (1, 2, 3)], 'notes': []}


# ---------- Reels as FakePlatform items, the fake platform, the fake YouTube ----------

def reel_items(root: Path) -> list[dict]:
    """The Reel candidates of a fixture root as FakePlatform items (spec 7.3) in natural order (spec 5.1):
    finished lessons only, through IG.lesson_files and IG.reels (the guard's probe); hand-made files are not
    candidates. Posted ones are included (their state comes from the platform's records)."""
    import instagram as IG
    found = []
    for lesson in sorted(p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(('_', '.'))):
        try:
            _, out, data = IG.lesson_files(lesson)
        except StopIteration:
            continue
        if _jload(tools.extras(out) / 'status.json').get('state') != 'done':
            continue
        first = min((f.stat().st_mtime for f in out.glob('*.mp4')), default=0.0)
        for f, entry in IG.reels(out, data):
            m = re.search(r'-short-(\d+)$', f.stem)
            part = int(m.group(1)) if m else None
            try:
                cover = IG.cover_path(f)
            except (AttributeError, ValueError):
                cover = None
            try:
                seconds = round(tools.probe(f)['duration'])
            except Exception:
                seconds = None
            found.append({'key': f'{lesson.name}/{out.name}/{f.name}', 'lesson': lesson.name, 'out': out, 'file': f,
                          'part': part, 'title': re.sub(r'\s*#\w+', '', entry.get('title', '')).strip() or f.name,
                          'seconds': seconds, 'rank': (first, part or 0, f.name), 'entry': entry,
                          'cover': cover if cover and cover.exists() else None})
    return sorted(found, key=lambda i: i['rank'])


def fake_platform(root: Path) -> 'platform_fake.FakePlatform':
    """A FakePlatform whose items mirror make_root()'s Reels; records in guard.WORK/fake-records.json.
    The Reels posted in the root's Drive records (new 1-5) start posted: they are written through
    write_record(). Make it after guard.reset(), which empties WORK."""
    import instagram as IG
    import platform_fake
    root = Path(root)
    fake = platform_fake.FakePlatform(reel_items(root), records=guard.WORK / 'fake-records.json', root=root)
    items, _ = fake.candidates(root)
    for item in items:
        rec = IG.load(item.out).get(item.file.name)
        if rec and 'id' in rec and not item.posted:
            fake.write_record(item, rec)
    return fake


def youtube_items(root: Path) -> list[dict]:
    """The YouTube upload candidates of a fixture root (platform_youtube's own scan) as FakePlatform items, with
    the record each one has in youtube-applied.json ('applied': None when not uploaded)."""
    import platform_youtube as PY
    items, _ = PY.YouTubePlatform(client=object()).candidates(Path(root))
    yp = PY.YouTubePlatform(client=object())
    return [{'key': i.key, 'lesson': i.lesson, 'out': i.out, 'file': i.file, 'part': i.part, 'title': i.title,
             'seconds': i.seconds, 'rank': i.rank, 'entry': i.entry, 'cover': yp.cover(i), 'applied': i.posted}
            for i in items]


def fake_youtube_platform(root: Path) -> 'platform_fake.FakePlatform':
    """A FakePlatform named 'youtube' (verb 'upload', breaker 'google') over youtube_items(root); the videos
    already on YouTube in the fixture records start posted. Records in guard.WORK/fake-youtube-records.json."""
    import platform_fake
    import platform_youtube as PY
    items = youtube_items(root)
    fake = platform_fake.FakePlatform(items, name='youtube', label='YouTube', noun='video', breaker='google',
                                      records=guard.WORK / 'fake-youtube-records.json', root=Path(root))
    fake.verb, fake.setup_hint, fake.account_url = 'upload', 'youtube.py login', 'https://www.youtube.com/@example'
    fake.link_re, fake.link_hint = PY.YouTubePlatform.link_re, PY.YouTubePlatform.link_hint
    got, _ = fake.candidates(root)
    for item, d in zip(sorted(got, key=lambda i: i.key), sorted(items, key=lambda d: d['key'])):
        if d['applied'] and not item.posted:
            fake.write_record(item, d['applied'])
    return fake


def fake_youtube_api(videos: dict[str, dict], channel: dict | None) -> object:
    """An object with .videos().list(**kw).execute() and .channels().list(**kw).execute(), like the YouTube
    client, answering from videos and channel. channel None makes every call raise an error whose text
    contains 'quotaExceeded'.
    videos: {id: API item} or {id: {'views', 'likes', 'comments', 'privacy', 'title', 'published'}} (the
    stats.youtube_cache() shape, turned into API items with string counts); channel: an API item or {'subscribers',
    'views', 'videos'}. .calls lists every executed request as (resource, kwargs)."""
    return FakeYouTube(videos, channel)


class FakeYouTube:
    """The fake YouTube Data API client (see fake_youtube_api)."""

    def __init__(self, videos, channel):
        self._videos, self._channel, self.calls = dict(videos), channel, []

    def videos(self):
        return self._resource('videos')

    def channels(self):
        return self._resource('channels')

    def _resource(self, name):
        """resource.list(**kw) -> a request whose execute() (num_retries etc. ignored) answers."""
        return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda *a, **k: self._answer(name, kw)))

    def _answer(self, resource, kw):
        self.calls.append((resource, dict(kw)))
        if self._channel is None:
            raise _quota_error(resource)
        if resource == 'videos':
            ids = [i for i in str(kw.get('id', '')).split(',') if i]
            return {'items': [_video_item(i, self._videos[i]) for i in ids if i in self._videos]}
        c = self._channel
        if 'statistics' in c:
            return {'items': [{'id': 'FAKE_CHANNEL', **c}]}
        return {'items': [{'id': 'FAKE_CHANNEL', 'statistics': {
            'subscriberCount': str(c.get('subscribers', 0)), 'viewCount': str(c.get('views', 0)),
            'videoCount': str(c.get('videos', 0)), 'hiddenSubscriberCount': False}}]}


def _video_item(vid, v):
    if 'snippet' in v or 'statistics' in v:
        return {'id': vid, **v}
    return {'id': vid, 'snippet': {'title': v.get('title', ''), 'publishedAt': v.get('published', '2026-11-01T16:00:00Z')},
            'statistics': {'viewCount': str(v.get('views', 0)), 'likeCount': str(v.get('likes', 0)),
                           'favoriteCount': '0', 'commentCount': str(v.get('comments', 0))},
            'status': {'privacyStatus': v.get('privacy', 'public')}}


def _quota_error(resource):
    """The HttpError YouTube gives when the daily quota is used up (a RuntimeError without googleapiclient)."""
    body = {'error': {'code': 403, 'message': 'The request cannot be completed because you have exceeded your quota.',
                      'errors': [{'message': 'quota', 'domain': 'youtube.quota', 'reason': 'quotaExceeded'}]}}
    try:
        import httplib2
        from googleapiclient.errors import HttpError
    except ImportError:
        return RuntimeError(f'<HttpError 403 when requesting youtube/v3/{resource} returned "quotaExceeded">')
    return HttpError(httplib2.Response({'status': 403, 'reason': 'Forbidden'}), json.dumps(body).encode(),
                     uri=f'youtube/v3/{resource}')


# ---------- small helpers ----------

def _safe(path):
    """path made absolute; fixtures never write to the Drive, the real work folder or the live repo."""
    full = Path(os.path.abspath(guard._plain(path)))          # F22: also a \\?\ or \\.\ spelling
    if re.match(r'[A-Za-z]:\\My Drive(\\|$)', str(full), re.I) or any(guard._inside(full, p) for p in REFUSED):
        raise ValueError(f'fixtures never write to {full}: use a folder under guard.TMP or a scratch folder')
    return full


def _register(root):
    """Let the guard's fake probe answer for a root outside TMP (dashboard --dev --fixtures)."""
    r = Path(root).resolve()
    if not r.is_relative_to(guard.TMP) and r not in guard.FAKE_ROOTS:
        guard.FAKE_ROOTS.append(r)


def _ts(*a):
    """Epoch seconds of an Istanbul wall-clock time."""
    return datetime(*a, tzinfo=IST).timestamp()


def _ist(t):
    return datetime.fromtimestamp(t, IST).strftime('%Y-%m-%d %H:%M')


def _stamp(at):
    """The 'posted' (Istanbul) and 'posted_utc' fields of a record, like instagram.stamp()."""
    return {'posted': at.astimezone(IST).strftime('%Y-%m-%d %H:%M'), 'posted_utc': at.astimezone(timezone.utc).isoformat()}


def _stamp_ts(rec):
    return datetime.fromisoformat(rec['posted_utc']).timestamp()


def _about(p):
    """The youtube.json 'files' text of a video."""
    short = p['height'] > p['width']
    return f"{'Short' if short else 'video'} {p['width']}x{p['height']}, {tools.ts(p['duration'])}"


def _jload(p):
    try:
        return json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _json(path, data, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _video(path, mtime):
    """A zero-byte video placeholder with this mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    os.utime(path, (mtime, mtime))


def _image(path, size, n=0, mtime=None):
    """A small valid image (JPEG or PNG by suffix): navy with a yellow bar placed by n, so covers differ."""
    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = size
    im = Image.new('RGB', size, (16, 22, 40))
    y = (n * 13) % max(h - 12, 1)
    ImageDraw.Draw(im).rectangle((4, y, w - 5, y + 10), fill=(255, 210, 26))
    if path.suffix.lower() == '.png':
        im.save(path, 'PNG')
    else:
        im.save(path, 'JPEG', quality=80)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
