# New video runbook (what Claude does when the user says "new video(s)")

Everything runs on the home PC; most of it happens by itself. Claude's job is to check, fix and report, quickly.
Paths: lessons in `J:\My Drive\SINEM-YouTube\<lesson>`, work files in `D:\YT\easy-work\<lesson-slug>`, Python
`D:\YT\work\venv\Scripts\python -I` (run from `D:\code\YT\pipeline`).

## 1. Where is it? (1 minute)
```
type "J:\My Drive\SINEM-YouTube\<lesson>\Output\STATUS.txt"
powershell Get-Content D:\YT\easy-work\watch.log -Tail 30
```
- **No Output folder yet**: the watcher waits until the clips haven't changed for 10 minutes (Drive still uploading),
  then starts within 10 minutes. To start now: `python -I easy.py run "J:\My Drive\SINEM-YouTube\<lesson>"` (in the
  background; a 6-minute recording takes ~25 minutes on the RTX 5070).
- **Working**: STATUS shows the step. Nothing to do.
- **Waiting: needs a Claude answer**: the Claude token stopped working. Answer the named `llm\<step>.prompt.md` by
  writing `<step>.answer.json` next to it (same JSON shape the prompt asks for), run again; then tell the user to
  renew the token (`claude setup-token` → `%USERPROFILE%\.shorts-pipeline\claude-token.txt`).
- **Failed**: read `D:\YT\easy-work\<slug>\error.txt`, fix, run again. A failed lesson is not retried by the watcher
  until its clips change.
- Watcher itself: `schtasks /query /tn "YT Easy mode watcher" /v /fo list` (Last Run Time, Last Result 0).

## 2. Check the result (5 minutes)
```
python -I review.py "J:\My Drive\SINEM-YouTube\<lesson>"
```
It checks the channel rules: Shorts ≤ 60 s (kind `ege` ≤ 90 s) and at most 3, vertical; main video horizontal; titles
without `#`; ≤ 3 hashtags per description; chapters, pinned question, playlist; subtitle lines on her face (QA);
Instagram covers; upcoming publish times one a day and 3 h apart. Then **look** (Read tool) at
`Output\check-frames-*.jpg`, `thumbnail-1..3.jpg`, `cover-short-*.jpg`, and read `youtube.md`.

Fixes (each step is cached; delete its files in `D:\YT\easy-work\<slug>` and run `easy.py run` again):
| Problem | Redo |
|---|---|
| Titles / descriptions / thumbnails | delete `llm\texts.answer.json` (frames stay: `thumbs\candidates.json`) |
| Subtitles, cuts, which Shorts | delete `llm\cues.answer.json` (everything after it re-renders) |
| One word wrong in a subtitle | edit `llm\cues.answer.json` by hand, run again |
| Too many Shorts | `settings.json` in the lesson folder: `{"shorts_max": 2}` |
| No Short wanted | `settings.json`: `{"short": false}` |
Never touch a lesson that is already on YouTube in a way that changes the video files (V1 is published: leave it).

## 3. Tell the user (and Sinem) what to do
Short message, in English, with what was verified:
- Ready: `<lesson>-video.mp4` + `-short-1..3.mp4` in `Output`.
- **Sinem uploads them in the YouTube app** (any title, private is fine). The watcher finds each upload by its length
  and fills in title, description, tags, thumbnail, captions (TR + DE), playlist, schedule. Shorts go public one a day
  at 19:00, never within 3 h of a long video. Main video: Sinem makes it public (Sunday is best), or `"public": true`.
- In Studio by hand (no API): pin the comment, end screen (video + subscribe, last 10 s), each Short's **Related
  video** → the full lesson.
- Paid promotion / AI answers come from `channel.json`; changing them is the user's call.

## 4. After the upload (automatic, check next day)
- STATUS.txt "YouTube:" lines show each video's link and what was set. Quota: ~900 units per Short, 10,000 a day;
  "quota used up" → it resets at 11:00 Istanbul (winter) / 10:00 (summer); the watcher retries by itself.
- Instagram + Facebook Page (`instagram.py`, log `D:\YT\easy-work\instagram.log`): one Reel a day after 20:00 (same Reel
  on the Page), the long video on the Page between 12:00 and 17:00 once it is public on YouTube. Plan: `Output\instagram.md`.
  `python -I instagram.py check` shows the token; Meta's data access needs a renewal every 90 days (next: 2027-01-31,
  steps in `instagram-setup.md`).

## Rules the pipeline follows (from the growth plan, Nov 2026)
- Captions: German biggest (yellow), Turkish smaller (white), translation under German lines; wrong/right pairs get
  ✘ red / ✔ green (`mark` in the cues). Long videos prefer the top, Shorts the bottom; never on eyes/nose/mouth.
- Shorts: one point each, hook in the first second, Turkish explanation, 20–45 s best, 60 s max ("Ege'de Almanca"
  stories up to 90 s), loop ending. Kinds: `mistake` (series "Türklerin Almanca Hataları #n"), `exam`, `life`
  ("Almanya'da Hayat"), `ege`, `rule`.
- Titles in Turkish, a promise or a mistake, keyword first, never `#`; hashtags only in descriptions, at most 3.
- One upload a day at 19:00; Sunday: long video first, its teaser Short 3 h later. Max 3 Shorts per lesson.
- Thumbnails: V1 format (`thumbnail.py`), the question adds to the title ("3 HATA", "HAZIR CEVAP", "BUNU DEME!").
- Channel art: `_channel\brand\` (made by `brandkit.py`), filming guide for Sinem: `_channel\CEKIM-REHBERI.txt`.
