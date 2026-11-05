"""Lesson texts for sharing outside YouTube: a blog post (Markdown) and short posts for X and Threads, made in one
Claude call per lesson from the final subtitles and the YouTube texts. Nothing is posted here.

    python -I posts.py "<lesson folder>"            # make them for a finished lesson (cached: a re-run never asks twice)
    python -I posts.py "<lesson folder>" --again    # ask Claude again (the old answer is kept as posts.answer.old.json)

Results in <lesson>/Output/extras/: blog.md (front matter + article), posts.md (to copy/paste, with lengths),
posts.json (the same for the publishing plug-ins later). The video link is filled in once youtube.py has applied the
details (youtube-applied.json); before that the texts say {VIDEO}. The watcher fills the link in by itself
(easy.youtube_step → relink); by hand: --link-only."""
import json, os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import llm
from tools import extras, slug

X_MAX, THREADS_MAX, URL_LEN = 280, 500, 23      # X counts every link as 23 characters
VIDEO = '{VIDEO}'
WORK_ROOT = Path(os.environ.get('PIPELINE_WORK', r'D:\YT\easy-work'))      # same as easy.py

PROMPT = """You write the texts that share one lesson of the YouTube channel "{channel}" ({handle}) outside YouTube:
German lessons for Turkish speakers by Sinem, a German teacher and translator (Turkish native speaker) who explains German
by comparing it with Turkish. Write in Turkish (German examples stay German), address readers with "siz".

Lesson: "{title}"   Level: {level}
What the YouTube description says:
{description}

Final subtitles of the video (time  lang  text  [translation]):
{cues}

Rules for everything:
- Only what the lesson teaches: no rules, exceptions or examples it does not contain (you may add a translation or
  split a sentence into a table). Check every German sentence: it must be correct standard German.
- The link to the video is written exactly as {video} (it is filled in later). No other links.
- No "#" in titles; hashtags only where said below.

Return ONE JSON object, nothing else:
{{
 "blog": {{
   "title": "...",          // <= 65 characters, what people search on Google ("Almanca ... nasıl kullanılır?"), not clickbait
   "slug": "...",           // lower-case ASCII words with '-', e.g. "almanca-zu-infinitiv"
   "description": "...",    // meta description, <= 155 characters
   "keywords": ["..."],     // 5-10 search phrases, Turkish + German
   "body": "..."            // Markdown, 500-900 words: a 2-3 sentence intro (the problem a Turkish speaker has with
                            // this), then "**Videoyu izleyin:** {video}", then one "## " section per part of the lesson
                            // (its rule in plain words + a table "| Almanca | Türkçe |" with the lesson's examples),
                            // "## Sık yapılan hatalar" with ✘ wrong / ✔ right pairs if the lesson has any,
                            // "## Kendinizi test edin": 3-5 short exercises from the lesson, "## Cevaplar" with the
                            // answers, and a last line inviting readers to the channel with {video}. No H1 (the title is
                            // the H1).
 }},
 "x": ["...", "..."],       // a thread of 3-6 posts, each <= 270 characters ({video} counts as 23): post 1 is the
                            // hook (a question or a typical mistake, no link), the middle posts each give one rule +
                            // one example "DE → TR", the last post sends to the video with {video} and at most 1 hashtag
 "threads": "...",          // one Threads post, <= 480 characters: hook, the core rule with 2 examples, {video}, at
                            // most 1 hashtag
 "notes": []                // anything Sinem should check before sharing (English)
}}
"""


def cue_block(prompt_file):
    """The subtitle lines the YouTube-texts prompt was given (for lessons made before this step existed)."""
    text = Path(prompt_file).read_text(encoding='utf-8')
    m = re.search(r'Final subtitles of the main video[^\n]*\n(.*?)\n\s*\n', text, re.S)
    if not m:
        raise ValueError(f'no subtitle block in {prompt_file}')
    return m.group(1).strip()


def video_link(x, name):
    """https://youtu.be/<id> of the lesson's main video once youtube.py applied its details, else None."""
    p = Path(x) / 'youtube-applied.json'
    rec = json.loads(p.read_text(encoding='utf-8')).get(f'{name}-video.mp4', {}) if p.exists() else {}
    return f'https://youtu.be/{rec["id"]}' if rec.get('id') else None


def x_length(text):
    return len(re.sub(r'https?://\S+', 'x' * URL_LEN, text.replace(VIDEO, 'x' * URL_LEN)))


def check(data):
    """Length and format problems, as notes (the texts are still written: Sinem can shorten one by hand)."""
    out = []
    posts = data.get('x') or []
    if not 1 <= len(posts) <= 8:
        out.append(f'X thread has {len(posts)} posts')
    out += [f'X post {i} is {x_length(p)} characters (max {X_MAX})' for i, p in enumerate(posts, 1) if x_length(p) > X_MAX]
    if x_length(data.get('threads', '')) > THREADS_MAX:
        out.append(f'Threads post is {x_length(data["threads"])} characters (max {THREADS_MAX})')
    b = data.get('blog') or {}
    for k in ('title', 'slug', 'description', 'body'):
        if not b.get(k):
            out.append(f'blog {k} is missing')
    if '#' in b.get('title', ''):
        out.append('blog title has a "#"')
    return out


def build_prompt(profile, yt, cues):
    return PROMPT.format(channel=profile['name'], handle=profile.get('handle', ''), title=(yt.get('titles') or [''])[0],
                         level=yt.get('level', ''), description=(yt.get('descriptions') or [''])[0], cues=cues,
                         video=VIDEO)


def fill(text, link):
    return text.replace(VIDEO, link) if link else text


def write(x, data, link):
    """blog.md, posts.md and posts.json in the lesson's extras folder; {VIDEO} replaced when the link is known."""
    x = Path(x)
    b = data['blog']
    front = ['---', f'title: "{b["title"]}"', f'slug: {b["slug"]}', f'description: "{b["description"]}"',
             f'keywords: [{", ".join(json.dumps(k, ensure_ascii=False) for k in b.get("keywords", []))}]',
             f'video: {link or VIDEO}', '---', '']
    (x / 'blog.md').write_text('\n'.join(front) + f'# {b["title"]}\n\n' + fill(b['body'], link).strip() + '\n',
                               encoding='utf-8')
    posts = [fill(p, link) for p in data.get('x', [])]
    threads = fill(data.get('threads', ''), link)
    L = ['# Lesson posts', '', 'Copy each block as it is.' +
         ('' if link else f' **{VIDEO} is not filled in yet**: the video is not on YouTube yet; the PC fills it in '
                           'after the upload.'), '']
    L += [f'## X thread ({len(posts)} posts, reply each one to the one before)', '']
    for i, p in enumerate(posts, 1):
        L += [f'### {i}/{len(posts)} ({x_length(p)} / {X_MAX})', '', p, '']
    L += [f'## Threads ({x_length(threads)} / {THREADS_MAX})', '', threads, '']
    if data.get('notes'):
        L += ['## Please check'] + [f'- {n}' for n in data['notes']] + ['']
    (x / 'posts.md').write_text('\n'.join(L), encoding='utf-8')
    (x / 'posts.json').write_text(json.dumps({'link': link, 'blog': {**b, 'body': fill(b['body'], link)}, 'x': posts,
                                              'threads': threads, 'notes': data.get('notes', [])},
                                             ensure_ascii=False, indent=1), encoding='utf-8')


def make(work, x, name, profile, cues):
    """One Claude call (cached in work/llm/posts.answer.json) → the three files. Returns the notes."""
    yt = json.loads((Path(x) / 'youtube.json').read_text(encoding='utf-8'))
    data = llm.ask(work, 'posts', build_prompt(profile, yt, cues))
    data['notes'] = list(data.get('notes') or []) + check(data)
    write(x, data, video_link(x, name))
    return data['notes']


def relink(work, x, name):
    """Write the files again from the cached answer when the video link changed (e.g. the video is now on YouTube).
    Returns True when it wrote them."""
    ans, pj = Path(work) / 'llm' / 'posts.answer.json', Path(x) / 'posts.json'
    if not ans.exists() or not pj.exists():
        return False
    link = video_link(x, name)
    if json.loads(pj.read_text(encoding='utf-8')).get('link') == link:
        return False
    data = json.loads(ans.read_text(encoding='utf-8'))
    data['notes'] = list(data.get('notes') or []) + check(data)
    write(x, data, link)
    return True


def _output(lesson):
    for n in ('Output (Easy)', 'Output'):
        if (extras(lesson / n) / 'status.json').exists():
            return lesson / n
    raise SystemExit(f'{lesson}: no finished Easy-mode output')


def main(argv):
    args = [a for a in argv if not a.startswith('--')]
    if len(args) != 1:
        raise SystemExit(__doc__)
    lesson = Path(args[0])
    name, work = slug(lesson.name), WORK_ROOT / slug(lesson.name)
    x = extras(_output(lesson))
    ans = work / 'llm' / 'posts.answer.json'
    if '--link-only' in argv:
        if not ans.exists():
            raise SystemExit('no posts yet: run without --link-only first')
        print('written again' if relink(work, x, name) else 'link unchanged')
    else:
        if '--again' in argv and ans.exists():
            ans.replace(ans.with_name('posts.answer.old.json'))
        profile = json.loads((lesson.parent / '_channel' / 'channel.json').read_text(encoding='utf-8'))
        make(work, x, name, profile, cue_block(work / 'llm' / 'texts.prompt.md'))
    print(f'{x / "blog.md"}\n{x / "posts.md"}\nlink: {video_link(x, name) or "not on YouTube yet"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1:])
