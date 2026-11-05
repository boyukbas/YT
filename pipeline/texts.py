"""YouTube texts (titles, descriptions, chapters, tags, pinned comment) + thumbnail choices, in one Claude call."""
import json, re
from pathlib import Path
import cv2
from PIL import Image, ImageDraw, ImageFont
from tools import records, ts

PROMPT = """You are the YouTube producer of the channel "{channel}" ({handle}): German lessons for Turkish speakers,
presented by Sinem. Write everything for this new video in Turkish (German examples stay German).

Lesson folder: "{lesson}"   Topic: {topic}
Versions: {versions}
Channel defaults: category Education, video + audio language Turkish, not made for kids.
Channel hashtags (pick from these first): {hashtags}
Shorts series (a Short of that kind gets the series in its title, with the number given): {series}
Course playlists (pick the one that fits; a new name only if none does): {playlists}

The channel's audience: Turkish speakers learning German at any level (A1 to B2/C1): for an exam (Goethe, telc), for
work in Germany (e.g. health professionals), or for moving to / living in Germany. Sinem is a German teacher (Turkish
native speaker): she takes professionals who want to work in Germany (many in health care) from zero to B2 and prepares
them for the Goethe and telc B2 exams. She is also an experienced translator/interpreter, so she explains German "with a
translator's eye": comparing it with Turkish to find the easiest way to build a sentence. Her aim: not just passing
the exam but using German with confidence at work and in daily life. Address viewers with "siz", as she does.
The channel is not tied to one level or exam: name the level or exam only when the lesson really is about it.
Titles promise a result or name a mistake/exam moment, they don't just name the grammar topic:
"Almanca Kendini Tanıtma: Türklerin En Çok Yaptığı 3 Hata (+ Hazır Cevap)" beats "Kendini Tanıtma 7 Başlık".
Question titles get more clicks on this channel than level labels like "A2 Almanca".

Final subtitles of the main video (time  lang  text  [translation]):
{cues}

The attached image shows {ncand} numbered candidate frames for the thumbnail (she is cut out of the background, the German
keywords go big next to her). Prefer a natural smile, eyes open, looking at the camera, sharp, mouth not mid-word.

Return ONE JSON object, nothing else:
{{
 "titles": ["...", "...", "..."],           // 3 different angles for Test & Compare; <= 70 characters; Turkish; main
                                            // keyword early (what people search: "Almanca kendini tanıtma", "Anmeldung");
                                            // never a hashtag or '#' in a title
 "short_title": "...",                      // for a single version called "short" (<= 60 chars); "" otherwise
 "shorts": [{{"version": "short-1", "title": "...", "description": "...",
             "cover": {{"frame": 3, "words": ["Vorname", "Nachname"], "question": "FARKI NE?"}}}}],
                                            // one per numbered Short version (short-1, short-2, ...): a title that
                                            // works without the lesson (<= 60 chars without the series part, a hook or
                                            // question, main keyword early, NO hashtags) + " | <series> #<n>" when its
                                            // kind has a series; 2-3 lines of description (what it teaches, one line
                                            // "Dersin tamamı kanalda: <the long video's title>", last line at most 3
                                            // hashtags incl. #Shorts); "cover": its own Reel cover, same rules as
                                            // thumbnails, a different frame per Short where possible; [] if none
 "descriptions": ["...", "..."],            // 2 complete descriptions: hook question, what the video teaches, the
                                            // example sentences with Turkish translations, a tip (exam topics: a
                                            // concrete exam tactic, "sınav taktiği"), the chapters block (if chapters), why to subscribe (what comes next / how often), hashtags
                                            // last line (at most 3)
 "chapters": [{{"t": 0, "title": "Giriş"}}, ...],   // main video only; first at 0, >= 3 chapters, >= 10 s apart, use real
                                            // topic changes from the subtitles; [] if the video is under 60 s
 "tags": ["...", ...],                      // 10-20 search phrases, Turkish + German, no '#'
 "pinned_comment": "...",                   // a question about the viewer that invites answers, e.g. "Sınavın ne
                                            // zaman? Yorumlara yaz, birlikte hazırlanalım." or a mini exercise from the
                                            // lesson ("Bu cümleyi düzelt: ...")
 "level": "A2",                             // CEFR level of the lesson
 "playlist": "...",                         // the course playlist this belongs in (see the list above)
 "thumbnails": [                            // 3 different proposals (tested against each other on YouTube)
   {{"frame": 3, "words": ["zu +", "Infinitiv"], "question": "NE ZAMAN?"}},
   ...],                                    // words: 1-3 short German keyword lines (<= 12 chars each, what people
                                            // search); question: <= 14 chars, Turkish, upper case, a hook that ADDS to
                                            // the title (a result or a mistake: "3 HATA", "HAZIR CEVAP", "BUNU DEME!"),
                                            // not a repeat of it. Pick frames with a clear, lively expression.
 "notes": []                                // anything the presenter should check (English)
}}
"""


def candidates(segs, inter_path, model, work, n=12):
    """Frames where her face is big, frontal and sharp, spread over the video → numbered contact sheet.
    Made once per lesson: the numbers Claude picked must keep pointing at the same pictures after a re-cut."""
    import json
    from layout import detector
    saved = work / 'thumbs' / 'candidates.json'
    if saved.exists():
        files = [{**c, 'file': work / 'thumbs' / c['file']} for c in json.loads(saved.read_text(encoding='utf-8'))]
        return files, work / 'thumbs' / 'candidates.jpg'
    pool = []
    for k, g in enumerate(segs):
        cap = cv2.VideoCapture(str(inter_path(k)))
        dur = g['b'] - g['a']
        t = 0.3
        while t < dur - 0.2:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, fr = cap.read()
            if ok:
                h, w = fr.shape[:2]
                _, f = detector(model, w, h).detect(fr)
                if f is not None:
                    r = max(f, key=lambda r: r[2] * r[3])
                    eyes = abs(r[4] - r[6])                         # eye distance: frontal faces have wide eyes
                    sharp = cv2.Laplacian(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                    pool.append((r[14] * eyes * min(sharp, 400) ** 0.3, g['start'] + t, k, t))
            t += 0.5
    pool.sort(reverse=True)
    pick = []
    for p in pool:
        if all(abs(p[1] - q[1]) > 4 for q in pick):
            pick.append(p)
        if len(pick) == n:
            break
    pick.sort(key=lambda p: p[1])
    d = work / 'thumbs'
    d.mkdir(exist_ok=True)
    tiles, files = [], []
    for i, (_, t_out, k, t) in enumerate(pick, 1):
        cap = cv2.VideoCapture(str(inter_path(k)))
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        _, fr = cap.read()
        f = d / f'cand_{i:02d}.png'
        cv2.imwrite(str(f), fr)
        files.append({'n': i, 'file': f, 't': t_out})
        im = Image.open(f).convert('RGB')
        im.thumbnail((480, 480))
        dr = ImageDraw.Draw(im)
        dr.rectangle((0, 0, 70, 56), fill=(0, 0, 0))
        dr.text((10, 4), str(i), font=ImageFont.truetype(r'C:\Windows\Fonts\arialbd.ttf', 44), fill=(255, 210, 26))
        tiles.append(im)
    cols = 4
    tw, th = tiles[0].size
    sheet = Image.new('RGB', (cols * tw, ((len(tiles) + cols - 1) // cols) * th))
    for i, im in enumerate(tiles):
        sheet.paste(im, ((i % cols) * tw, (i // cols) * th))
    sheet.save(d / 'candidates.jpg', quality=88)
    saved.write_text(json.dumps([{**c, 'file': c['file'].name} for c in files]), encoding='utf-8')
    return files, d / 'candidates.jpg'


def series_numbers(root, profile, lesson=None):
    """{kind: 'Almanca'da Sık Yapılan Hatalar #4'}: each series' next number, counted over the other lessons: each Short
    title in the series, plus a lesson whose main video is in it (a one-clip Short lesson; any of its title options)."""
    names = profile['youtube'].get('series', {})
    datas = [json.loads(p.read_text(encoding='utf-8')) for l, _, p in records(root, 'youtube.json')
             if lesson is None or l.resolve() != Path(lesson).resolve()]
    count = lambda n: sum(sum(n in s.get('title', '') for s in d.get('shorts', [])) +
                          any(n in t for t in d.get('titles', [])) for d in datas)
    return {k: f'{n} #{count(n) + 1}' for k, n in names.items() if n}


def drop_chapters(text):
    """Remove a chapters block ("⏱️ Bölümler:" + timestamp lines) from a description (Shorts show no chapters)."""
    text = re.sub(r'\n*[^\n]*(?:⏱|Bölümler)[^\n]*\n(?:\s*\d{1,2}:\d{2}[^\n]*(?:\n|$))+', '\n\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()            # no trailing newline: YouTube drops it (no false diff)


def cue_rows(cues):
    """The final subtitles as prompt lines: time  lang  text  [translation]  (mark)."""
    return '\n'.join(f'{ts(c["s"])}  {c["lang"]}  {c["text"].replace("<", "").replace(">", "")}' +
                     (f'  [{c["tr"]}]' if c['lang'] == 'de' and c.get('tr') else '') +
                     (f'  ({c["mark"]})' if c.get('mark') else '') for c in cues)


def build_prompt(profile, lesson, topic, cues, versions, ncand, series=None):
    yt = profile['youtube']
    return PROMPT.format(channel=profile['name'], handle=profile.get('handle', ''), lesson=lesson, topic=topic,
                         versions=versions, hashtags=' '.join(yt.get('hashtags', [])), cues=cue_rows(cues), ncand=ncand,
                         series=', '.join(f'{k}: "{v}"' for k, v in (series or {}).items()) or '-',
                         playlists=', '.join(f'"{p}"' for p in yt.get('playlists', [])) or '-')


def check_chapters(ch, total):
    """YouTube's rules: first at 0:00, at least 3, each at least 10 s long."""
    if not ch:
        return []
    ch = sorted(ch, key=lambda c: c['t'])
    ch[0]['t'] = 0
    out = [ch[0]]
    for c in ch[1:]:
        if c['t'] - out[-1]['t'] >= 10 and total - c['t'] >= 10:
            out.append(c)
    return out if len(out) >= 3 else []


def write(out_dir, slug, data, profile, files):
    yt = profile['youtube']
    meta = {**data, 'category': 'Education (27)', 'defaultLanguage': yt.get('language', 'tr'),
            'defaultAudioLanguage': yt.get('audio_language', 'tr'), 'madeForKids': False, 'files': files}
    (out_dir / 'youtube.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    L = ['# YouTube texts', '']
    L += ['## Title options (pick one)'] + [f'{i}. {t}' for i, t in enumerate(data['titles'], 1)] + ['']
    if data.get('short_title'):
        L += ['## Short title', data['short_title'], '']
    for sh in data.get('shorts', []):
        f = next((k for k in files if k.endswith(f"-{sh['version']}.mp4")), sh['version'])
        L += [f"## {sh['version'].replace('short', 'Short')} (`{f}`)", f"**{sh['title']}**", '', sh['description'], '']
    for i, d in enumerate(data['descriptions'], 1):
        L += [f'## Description option {i}', '', d, '']
    if data.get('chapters'):
        L += ['## Chapters'] + [f'{ts(c["t"])} {c["title"]}' for c in data['chapters']] + ['']
    L += ['## Tags (paste into the Tags field)', ', '.join(data['tags']), '']
    L += ['## Pinned comment', data['pinned_comment'], '']
    L += ['## Settings', f'- Playlist: {data.get("playlist", "")}', '- Category: Education', '- Video language: Turkish',
          '- Audio language: Turkish', '- Made for kids: No', f'- Level: {data.get("level", "")}', '']
    L += ['## Files'] + [f'- `{k}`: {v}' for k, v in files.items()] + ['']
    if data.get('notes'):
        L += ['## Please check'] + [f'- {n}' for n in data['notes']]
    (out_dir / 'youtube.md').write_text('\n'.join(L) + '\n', encoding='utf-8')
