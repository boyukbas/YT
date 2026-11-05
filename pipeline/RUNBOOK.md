# New video runbook (what Claude does when the user says "new video(s)")

Everything runs on the home PC; most of it happens by itself. Claude's job is to check, fix and report, quickly.
Paths: lessons in `J:\My Drive\SINEM-YouTube\<lesson>`, work files in `D:\YT\easy-work\<lesson-slug>`, Python
`D:\YT\work\venv\Scripts\python -I` (run from `D:\code\YT\pipeline`).

## 1. Where is it? (1 minute)
The whole channel on one page: `SINEM-YouTube\STATUS.txt` (`overview.py`; the user reads it in the Drive app on the
phone). Rewritten by the watcher every run and on every lesson status change; it calls no API.
The private dashboard https://yt.boyukbas.com (Google login, the user + Sinem; `dashboard.py`, page in `dashboard/`)
is the management page: **Home** (what needs attention, what goes out next, counts waiting for Instagram and YouTube,
numbers), **Publish** (every waiting Reel: post any one now, move to top, skip, edit caption, mark as posted by hand;
automatic posting on/off with frequency, days, time window and gap), **Videos**, **Stats** (YouTube + Instagram numbers
saved hourly in `D:\YT\easy-work\stats.db`), **Activity** (who did what; also `publish.db`). Every press is logged in
`D:\YT\easy-work\dashboard.log`. Its data by hand: `python -I dashboard.py json "J:\My Drive\SINEM-YouTube"`.
```
type "J:\My Drive\SINEM-YouTube\STATUS.txt"
type "J:\My Drive\SINEM-YouTube\<lesson>\Output\extras\STATUS.txt"
powershell Get-Content D:\YT\easy-work\watch.log -Tail 30
```
- **No Output folder yet**: the watcher waits until the clips haven't changed for 10 minutes (Drive still uploading),
  then starts within 10 minutes. To start now: `python -I easy.py run "J:\My Drive\SINEM-YouTube\<lesson>"` (in the
  background; a 6-minute recording takes ~30 minutes on the RTX 5070; redoing only the texts ~5 minutes).
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
It checks the channel rules: Shorts ≤ 60 s and at most 3, vertical; main video horizontal; titles
without `#`; ≤ 3 hashtags per description; chapters, pinned question, playlist; subtitle lines on her face (QA);
Instagram covers; upcoming publish times one a day and 3 h apart. Then **look** (Read tool) at
`Output\extras\`: `check-frames-*.jpg`, `thumbnail-1..3.jpg`, `cover-short-*.jpg`, and read `youtube.md`
(`Output` itself holds only the finished videos; everything else is in `Output\extras`).

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
- Instagram (`publish.py` + `platform_instagram.py`; log `D:\YT\easy-work\instagram.log`): Reels are made from the
  lesson's `-short-N.mp4` files (a one-clip Short lesson is one Reel itself). **Automatic posting** is a setting on the
  dashboard (Publish > Settings, stored in `D:\YT\easy-work\publishing.json`): on/off, every N days, weekdays, time
  window (default 20:00-23:59 Istanbul), minimum gap (default 20 h); never more than 3 in 24 h (`hard_cap_24h`, only in
  the file). It was OFF at the v2 deploy (2026-11-05). `D:\YT\easy-work\instagram-paused` is only an emergency brake:
  while it exists nothing is posted automatically, but deleting it does NOT turn automatic posting on. Posting by hand:
  Publish > any Reel > Post (warns about the one-a-day gap; needs a tick to override), or
  `python -I instagram.py post "<lesson folder, exact name>"` (refuses on gap warnings unless `--force`; never passes the
  3-in-24-h cap; refuses while a post is unresolved). A post that was interrupted shows a red card: press **Check again**
  (the PC also re-checks by itself) before answering by hand. Nothing is posted on Facebook, but the empty Page must
  stay linked to Instagram. Plan: `Output\extras\instagram.md`; records: `Output\extras\instagram-posted.json`.
  Key: a system user's token (never expires). "API access blocked" (code 200) on every call = Meta flagged the developer account: the user confirms it
  on developers.facebook.com, or it clears by itself; try once, then wait, don't hammer it.

## Going back to the old dashboard (rollback of v2, only if v2 is broken)
v1 reads only the Drive records and the pause file; it knows nothing of publish.db (skips, edited captions, order,
interrupted posts). So, in this order, while v2 still runs:
1. Stop automatic posting: Publish > Turn off, and make sure `D:\YT\easy-work\instagram-paused` exists
   (`type nul > D:\YT\easy-work\instagram-paused`). v1 posts every evening when that file is missing.
2. No post in flight: no red "post was interrupted" card (press Check again until it settles), no "record not saved
   yet" card. A Reel v2 posted without a Drive record would be posted again by v1.
3. Note the skipped Reels and edited captions (Publish list) — v1 will not respect them; don't press v1's "Post next
   Reel now" for a skipped one.
4. `git revert` the v2 commits in `D:\code\YT`, then right away stop the `dashboard.py serve` processes (dashboard.cmd
   restarts the server in 30 s on the old code), then push.
5. If `D:\YT\easy-work\instagram-posting.lock` is left and no python is posting, delete it (v1 refuses to post while
   it exists). publish.db, stats.db and publishing.json can stay; v1 ignores them.

## Gotchas seen so far
- **Same recording in two lesson folders** (copied by mistake): the second one stops with "is also in the lesson …".
- **A clip replaced** in a lesson: the PC starts that lesson fresh (the old work folder is kept as `<slug>.old-<time>`).
- **One-clip vertical lesson (≤ 3 min)**: the main video *is* a Short: vertical cover, no chapters, no separate Shorts;
  on Instagram it is one Reel.
- **Playlists** named in `youtube.json` that don't exist yet are created (public) when the video is filled in.
- **Series numbers** ("Almanca'da Sık Yapılan Hatalar #n") are counted from the other lessons' `youtube.json`.
- Making a video public posts the exercise comment in the same run; pinning it stays in Studio.

## Rules the pipeline follows (from the growth plan, Nov 2026)
- Captions: German biggest (yellow), Turkish smaller (white), translation under German lines; wrong/right pairs get
  ✘ red / ✔ green (`mark` in the cues). Long videos prefer the top, Shorts the bottom; never on eyes/nose/mouth.
- Shorts: one point each, hook in the first second, Turkish explanation, 20–45 s best, 60 s max, loop ending.
  Kinds: `mistake` (series "Almanca'da Sık Yapılan Hatalar #n"), `exam`, `life` ("Almanya'da Hayat #n"), `rule`.
- The channel is not tied to one level or exam (decided 2026-11-02): all levels, exams, work and life in Germany.
- Titles in Turkish, a promise or a mistake, keyword first, never `#`; hashtags only in descriptions, at most 3.
- One upload a day at 19:00; Sunday: long video first, its teaser Short 3 h later. Max 3 Shorts per lesson.
- Thumbnails: V1 format (`thumbnail.py`), the question adds to the title ("3 HATA", "HAZIR CEVAP", "BUNU DEME!").
- Channel art: `_channel\brand\` (made by `brandkit.py`), filming guide for Sinem: `_channel\CEKIM-REHBERI.txt`.
