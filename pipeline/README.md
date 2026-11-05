# Easy mode pipeline (home PC)

Sinem drops the raw clips of a lesson into a Drive folder; this PC turns them into a finished video plus
everything YouTube needs, and writes it back to the same folder.
**When new videos come in: follow [RUNBOOK.md](RUNBOOK.md)** (status → `review.py` rule check → report).

```
J:\My Drive\SINEM-YouTube\
  _channel\            channel profile (set once): channel.json, logo.png, subscribe.png, fonts\, music\, terms.txt, brand.png,
                       brand\ (banner, Facebook cover, Instagram highlights: brandkit.py), CEKIM-REHBERI.txt (filming guide)
  STATUS.txt           the whole channel on one page for the phone (overview.py; rewritten by the PC)
  <lesson>\            raw clips (.mov/.mp4), optional settings.json  ({"short": false} = no Short version)
    Output\            the finished videos (or "Output (Easy)" if an Output folder made elsewhere exists)
      extras\          everything else: thumbnails, captions (.srt), youtube.md/json, covers, instagram.md, records, STATUS.txt
```

Results: `<lesson>-video.mp4` (and `-short.mp4`), Turkish + German caption tracks (`.srt`), `thumbnail-1..3.jpg`,
`youtube.md` / `youtube.json` (3 titles, 2 descriptions, chapters, tags, pinned comment, settings), `terms.txt`,
`check-frames-*.jpg` (subtitle positions at a glance), `STATUS.txt`.

## Steps
1. **Speech** (`speech.py`): Whisper large-v3 on the GPU, three passes per clip (forced Turkish, forced German, auto).
2. **Subtitles + edit list** (`cues.py`, Claude): proofread bilingual cues, German words marked, translations both ways,
   where each clip starts/ends, retakes to cut, Short-only cuts, word cards, the call-to-action moment.
3. **Cut** (`render.py`): kept ranges → SDR 30 fps segments (iPhone HLG tone-mapped), long silences shortened.
4. **Placement** (`layout.py`): face tracking (OpenCV YuNet, `models/`), subtitles never on eyes/nose/mouth;
   long videos prefer the top, Shorts the bottom; few blocked lines flip, many → other side, none → a text band.
   Section labels and word cards (`chips.py`) and the subscribe banner go where nothing important is.
   The lesson's pattern (`<zu lernen>` in the cues) gets a red highlighter outline. German lines are the biggest,
   Turkish smaller, the translation under a German line smaller still; deliberate wrong/right examples (`"mark"` in a
   cue) get a red ✘ / green ✔ and their colour. Vertical recordings in a long video use the studio layout (cropped
   to her upper body, face high in the card).
5. **Render**: libass subtitles in the channel palette, logo (skipped when the recording already has it; our own opaque
   logo covers a burned-in one when punch-ins move it), labels + word cards, subscribe banner, a slight punch-in on every
   second segment (jump cuts look intentional), compression-noise cleanup + sharpening for small sources, speech noise
   removal (DeepFilterNet, `D:\YT\work\tools\deepfilter`; channel.json `"denoise"`: `light` = up to 30 dB, `strong` = all,
   `off`; ffmpeg's afftdn if the tool is missing) + compression + two-pass loudness to −14 LUFS, optional ducked music,
   a 10 s end-screen card ("Sıradaki ders").
6. **Check**: every subtitle line is checked on the finished video for face overlap; loudness measured; frame sheet.
7. **YouTube texts + thumbnails** (`texts.py`, `thumbnail.py`, Claude looks at candidate frames).
   **Lesson posts** (`posts.py`, one more Claude call): `blog.md` (an article with tables, mistakes, exercises),
   `posts.md` (an X thread + a Threads post to copy, with lengths) and `posts.json`; `{VIDEO}` becomes the YouTube link
   once the details are applied. A failure here never stops the lesson. Older lessons: `python -I posts.py "<lesson>"`.
   **Folder names** (`lessons.py`, since 2026-11-05): once a lesson is finished, a folder named only `4`, `new`,
   `yeni`, `New folder (2)`, ... gets a short name from the thumbnail's German words (`4` → `4 Lass uns kennenlernen`),
   together with its work folder in `D:\YT\easy-work`, under the posting lock (never while a Reel or video goes out).
   Names Sinem chose herself are never changed. The finished files keep their names (`4-video.mp4`), so records,
   queue entries and later renders still match. Preview: `python -I lessons.py names "<channel folder>"`; by hand:
   `python -I lessons.py rename "<lesson>" ["<new name>"]`.
8. **YouTube details** (`youtube.py`): Sinem uploads the finished files in the YouTube app; the watcher finds each upload
   by its length (closest file size when Shorts have the same length) and sets title, description, tags, category,
   languages, audience, paid-promotion/AI answers, thumbnail, TR + DE captions, playlist (existing ones only; Shorts go in
   `shorts_playlist` in their order and link to the full lesson), and posts the comment once the video is public.
   `"public": true` in settings.json (or `--public`) also makes them public. Record: `Output/youtube-applied.json`;
   STATUS shows the links. Still in Studio: pin the comment, end screen. Quota: ~900 units per Short, 10,000 a day.
   One-time sign-in: `python youtube.py login` (pick Almanca Dil Atölyesi). Test without changes:
   `python youtube.py apply "<lesson>" --dry-run`.
   **Or the PC uploads** (`platform_youtube.py`, since 2026-11-05): the dashboard's Publish › YouTube lists every
   finished file not on YouTube yet. "Upload" sends it (private first, with title, description, tags and settings;
   then thumbnail, captions and playlist; after YouTube's processing it goes public) through publish.py, like an
   Instagram Reel: same lock, job journal, spacing warnings and override tick. "Upload automatically" (settings
   window, frequency, gap; **OFF** at the deploy) does the same on its own. A file already uploaded by hand is found
   first and never sent twice; the watcher's details step waits while an upload runs and posts the comment later.
   The dashboard caption is the title (first line) + the description. Upload quota: 100 a day (its own bucket).
   Needs the OAuth consent screen "In production" (in Testing the sign-in dies after 7 days).

**Instagram** (`instagram.py`: key, Graph API calls, posting, commands; `instagram_content.py`: which files are
Reels, captions, covers, records; `meta.py`: errors and the temporary public link, shared with future Meta
platforms; setup in `instagram-setup.md`): every Short also gets `cover-short-N.jpg` (its own photo +
keywords, text inside the 3:4 grid crop) and a caption in `instagram.md`; when automatic posting is on (dashboard >
Publish > Settings, `publishing.json`; default one a day after 20:00, at least 20 h apart, never more than 3 in 24 h)
the watcher posts the next waiting Reel; any Reel can also be posted by hand from the dashboard (key: a system user's token from Business settings, `instagram.py system-token`, never expires, used
on graph.facebook.com; a Facebook Page must stay linked to Instagram, though nothing is posted there since 2026-11-03:
`FACEBOOK_PAGE = False`). Instagram fetches video + cover through a temporary Cloudflare quick
tunnel (its direct upload refuses Page tokens).

**Shorts:** at most 3 (`shorts_max` in channel.json / settings.json), the strongest self-contained parts (Claude picks
them in the subtitle step: `shorts` in the answer, each with a `kind`: mistake / exam / life / rule; 20–45 s
best, 60 s max; a kind with a series in channel.json `youtube.series` gets
" | <series> #n" in its title); they go public one a day at 19:00, never within 3 h of a long video (youtube.py schedules them),
`<lesson>-short-1.mp4`, `-short-2.mp4`, … each with its own title and description in `youtube.md`. Lessons without
such parts get one condensed Short.

## Claude backend (`llm.py`)
- `ANTHROPIC_API_KEY` or `%USERPROFILE%\.shorts-pipeline\anthropic_key` → API
- else `claude -p` (Claude Code). Normal logins expire, so for unattended runs: `claude setup-token` once and save the
  printed token in `%USERPROFILE%\.shorts-pipeline\claude_token` (or `claude-token.txt`; saved 2026-11-02)
- else **manual**: the lesson's STATUS says "Waiting"; a Claude Code session answers `D:\YT\easy-work\<lesson>\llm\<step>.prompt.md`
  by writing `<step>.answer.json`, then run again. Answers are cached, so re-runs never ask twice.

## Run
```
D:\YT\work\venv\Scripts\python -I easy.py run "J:\My Drive\SINEM-YouTube\<lesson>"
D:\YT\work\venv\Scripts\python -I easy.py watch "J:\My Drive\SINEM-YouTube" --once     # Task Scheduler, every 10 min
```
Task Scheduler: **"YT Easy mode watcher"** (registered 2026-11-02) runs `watch.cmd` every 10 minutes and at logon, hidden
(`conhost --headless`), only while the user is logged on (the Drive mount `J:` needs the session); output in
`D:\YT\easy-work\watch.log`. Overlapping runs are skipped (Task Scheduler + `watch.lock`).
A lesson is picked up when its clips haven't changed for 10 minutes and differ from the last run.
**Drive gotcha:** ffmpeg misreads looped images straight from the Drive mount (`J:`) ("Failed to reallocate parser
buffer", then it stalls), so `_channel` is mirrored to `D:\YT\easy-work\_channel` and rendered from there.

Work files: `D:\YT\easy-work\<lesson>` (safe to delete; delete a step's files there to redo it).
