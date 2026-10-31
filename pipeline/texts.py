"""YouTube texts (titles, descriptions, chapters, tags, pinned comment) + thumbnail choices, in one Claude call."""
import json
import cv2
from PIL import Image, ImageDraw, ImageFont
from tools import ts

PROMPT = """You are the YouTube producer of the channel "{channel}" ({handle}): German lessons for Turkish speakers,
presented by Sinem. Write everything for this new video in Turkish (German examples stay German).

Lesson folder: "{lesson}"   Topic: {topic}
Versions: {versions}
Channel defaults: category Education, video + audio language Turkish, not made for kids.
Always-used hashtags: {hashtags}

Final subtitles of the main video (time  lang  text  [translation]):
{cues}

The attached image shows {ncand} numbered candidate frames for the thumbnail (she is cut out of the background, the German
keywords go big next to her). Prefer a natural smile, eyes open, looking at the camera, sharp, mouth not mid-word.

Return ONE JSON object, nothing else:
{{
 "titles": ["...", "...", "..."],           // 3 different angles; <= 70 characters; main keyword early (what people search)
 "short_title": "...",                      // for the Short (<= 60 chars, may end with #Shorts); "" if no Short
 "descriptions": ["...", "..."],            // 2 complete descriptions: hook question, what the video teaches, the
                                            // example sentences with Turkish translations, a tip, the chapters block
                                            // (if chapters), a short call to subscribe, hashtags last line (max 10)
 "chapters": [{{"t": 0, "title": "Giriş"}}, ...],   // main video only; first at 0, >= 3 chapters, >= 10 s apart, use real
                                            // topic changes from the subtitles; [] if the video is under 60 s
 "tags": ["...", ...],                      // 10-20 search phrases, Turkish + German, no '#'
 "pinned_comment": "...",                   // a question that invites comments, e.g. a mini exercise from the lesson
 "level": "A2",                             // CEFR level of the lesson
 "playlist": "...",                         // a playlist name this belongs in (short, Turkish)
 "thumbnails": [                            // 3 different proposals
   {{"frame": 3, "words": ["zu +", "Infinitiv"], "question": "NE ZAMAN?"}},
   ...],                                    // words: 1-3 short German keyword lines (<= 12 chars each, what people
                                            // search); question: <= 14 chars, Turkish, upper case, a hook
 "notes": []                                // anything the presenter should check (English)
}}
"""


def candidates(segs, inter_path, model, work, n=12):
    """Frames where her face is big, frontal and sharp, spread over the video → numbered contact sheet."""
    from layout import detector
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
    return files, d / 'candidates.jpg'


def build_prompt(profile, lesson, topic, cues, versions, ncand):
    rows = [f'{ts(c["s"])}  {c["lang"]}  {c["text"]}' + (f'  [{c["tr"]}]' if c['lang'] == 'de' and c.get('tr') else '') for c in cues]
    return PROMPT.format(channel=profile['name'], handle=profile.get('handle', ''), lesson=lesson, topic=topic,
                         versions=versions, hashtags=' '.join(profile['youtube'].get('hashtags', [])), cues='\n'.join(rows), ncand=ncand)


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
