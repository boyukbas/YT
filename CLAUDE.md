# Shorts Editor — project notes for Claude Code

Video editor for the YouTube channel **Almanca Dil Atölyesi** (youtube.com/@sinembb): German lessons
for Turkish speakers. Videos mix Turkish (main language) and German. Goal: cover all editing needs of
an education channel — Shorts and normal videos from the same recording.

The owner talks to Claude in **Turkish**; the app UI is in **English** (deliberate). They mostly use an
Android phone (Chrome, 8 GB, no WebGPU) and sometimes an iPad Pro or a PC with an RTX 5070.
They like step-by-step answers, visuals, and clear "what was verified vs. assumed".

## Layout
- `docs/` — **the web app (live)**, served by GitHub Pages from `main` / `docs`:
  https://boyukbas.github.io/YT/  (installable PWA, works offline after first use)
- `ShortsEditor/`, `project.yml`, `.github/workflows/build-ipa.yml` — native iOS app (SwiftUI + WhisperKit).
  **Shelved**: sideloading failed (Apple login / anisette). Builds still pass in CI; don't invest further unless asked.
- `pipeline/` — **Easy mode on the home PC** (Python): a lesson folder in Google Drive (`G:\My Drive\SINEM-YouTube\<lesson>`, letter can change)
  in → finished video, caption tracks, 3 thumbnails, YouTube texts out. See `pipeline/README.md`. Decided 2026-10-31:
  Sinem's channel only; the PC is the worker; she uploads in the YouTube app (or the PC uploads, see backlog) and
  `youtube.py` fills in the details.
  Max 3 Shorts per lesson, published one a day (19:00); Instagram Reels by `publish.py` (the publishing
  core: queue, timing rules, posting lock, audit; platforms are plug-ins: `platform_instagram.py`, `platform_fake.py`
  for tests) on the schedule set on the dashboard (`schedule.py`, `D:\YT\easy-work\publishing.json`; default one a day
  20:00-23:59, 20 h gap, never > 3 in 24 h; automatic posting was OFF at the v2 deploy; since 2026-11-05 the owner
  wants it more frequent: `per_day` (up to N on a slot day, each `min_gap_hours` apart; preset "Three a day") and
  the 24 h hard cap (file only, 1-10, blocks manual posts too) set to 3 a day, 4 h, cap 6 for both platforms) or by hand from the dashboard,
  via `instagram.py` (setup: `pipeline/instagram-setup.md`; Meta app "Almanca Dil Atölyesi", Facebook-login mode). No Facebook
  posts since 2026-11-03 (Page did poorly and was deleted, Instagram well): `FACEBOOK_PAGE = False`. Publishing still
  needs *a* Page linked to Instagram (a new empty one is; without it media_publish fails "Fatal" 2207085). Key: a
  system user's token from Business settings (`instagram.py system-token`). Go slowly with Meta logins/keys: a burst of
  them got the developer account flagged ("API access blocked") on 2026-11-03.
  Watcher: Task Scheduler "YT Easy mode watcher" every 10 min (`pipeline/watch.cmd`, log `D:\YT\easy-work\watch.log`).
  Status page for the phone: `SINEM-YouTube\STATUS.txt` (`pipeline/overview.py`), rewritten every run and on each status change.
  Private dashboard: https://yt.boyukbas.com (`pipeline/dashboard.py` + `pipeline/dashboard/`), served by the PC on
  127.0.0.1:8790 through a Cloudflare named tunnel, behind Cloudflare Access (Google login; the user + Sinem only).
  The server re-checks Access's signed token (aud, team, email) on every request; settings (team, aud, emails) live in
  `D:\YT\easy-work\dashboard.json`, never in this public repo. Tabs: Home, Publish (post
  any waiting Reel with warnings + override tick, reorder/skip/caption/mark as posted, automatic-posting settings),
  Videos, Stats (`stats.py` → `stats.db`, hourly YouTube + Instagram numbers), Activity (`publish.db`); each press logged
  in `dashboard.log`. `instagram-paused` is only an emergency brake now (deleting it does not turn posting on).
  Dev copy, offline with fake data: `PIPELINE_WORK=<scratch> python -I dashboard.py serve --dev --fixtures --port 8791`
  (refuses the real work folder). Tests: `python -I -m unittest discover -s pipeline/tests -t pipeline` (network
  blocked by `tests/guard.py`) and `node pipeline/tests/smoke_ui.cjs` against the dev server.
  The dashboard is used mostly on an **iPhone 16 (Safari)**: the smoke test also runs an iPhone pass in WebKit when it is
  installed (no sideways scroll, fields >= 16 px); keep safe areas, dvh and 44 px taps. Errors on the page go through
  `publish.plain()` (display only; raw text stays stored); YouTube's used-up quota shows as a pause (`Platform.paused`), not a failure.
  Videos tab: a video not on YouTube yet shows its Drive cover (`dashboard.cover_ids`).
  Started at logon by Task Scheduler "YT dashboard" (`pipeline/dashboard.cmd`: tunnel `yt-dashboard` with config
  `D:\YT\easy-work\cloudflared.yml` + server, each restarted 30 s after it stops; logs `tunnel.log`, `dashboard-server.log`).
  Zero Trust team `frosty-bar-63c8` (Free); boyukbas.com DNS on Cloudflare since 2026-11-04 (Spaceship email forwarding
  MX/SPF kept). A new allowed email goes in BOTH the Access policy "Me and Sinem" and dashboard.json (then restart the task).
  Keep status info out of public places (a public GitHub status page was tried and removed on 2026-11-04).
  Lesson Output folders hold only the finished videos; everything else is in `Output/extras/` (`tools.extras()`).
  Google Drive's drive letter can change after a restart (J: → G: on 2026-11-03); `watch.cmd` looks for
  `<letter>:\My Drive\SINEM-YouTube` on every letter, so use whatever letter has it in manual commands too.
  YouTube API quota resets at midnight Pacific = 11:00 Istanbul in winter, 10:00 in summer. After a quotaExceeded every part (watcher,
  stats, Upload) waits for the shared note `easy-work\youtube-quota-until.txt` (`youtube.quota_used_up`/`quota_until`);
  the watcher's re-check of a video waiting to be public costs 1 unit (caption list only when a track changes; 2026-11-06).
  PC folders (cleaned 2026-11-05): `D:\YT` holds only `easy-work` (databases, settings, transcripts, Claude answers),
  `work\venv` (the pipeline's Python), `work\tools`, `tools` (cloudflared) and `voice-samples`. Old hand-made videos and
  build docs are in `<letter>:\My Drive\YT archive (PC)` (outside SINEM-YouTube, so the watcher ignores it).
  **User says "new video(s)" → follow `pipeline/RUNBOOK.md`** (STATUS → `review.py` rule check → look at frames → report).
- The iOS workflow ignores `docs/**`, `pipeline/**`, `README.md` and `CLAUDE.md` changes.

## Channel style (same everywhere: video, thumbnails, app)
Palette in `_channel/channel.json` (Drive), card in `_channel/brand.png`: **German = yellow #FFD21A, Turkish = white,
accent = red #E30A17, surfaces = navy #101628**, ink #15171C, translation lines #E8EAF0. Thumbnails: the V1 format
(`pipeline/thumbnail.py`). Subtitles never cover eyes/nose/mouth; long videos prefer the top, Shorts the bottom.
German lines are the biggest, Turkish smaller; wrong/right examples get ✘ red / ✔ green. Shorts 20–45 s (60 max), one
point, hook first; titles without '#', ≤ 3 hashtags; never two uploads within 3 h (growth plan, Nov 2026).
Slogan: "A1'den C1'e Almanca" (Sinem as a German teacher, all levels; not interpreter, not A1-only). Channel art in `_channel/brand/` (`pipeline/brandkit.py`); Sinem's filming guide `_channel/CEKIM-REHBERI.txt`.
Word cards ("KELİME"): one harder word every 30–60 s. Section labels at each numbered part; the lesson's pattern is
highlighted (yellow text, red outline); 10 s end-screen card "Sıradaki ders". Pipeline gotcha: never let ffmpeg read
looped images from the Drive mount (it stalls) — the pipeline renders from a local copy of `_channel`.

## Web app files (`docs/`)
| File | What |
|---|---|
| `index.html` | All markup + CSS. Tabs: Video / Subtitles / Look / Share; Settings, Help, Combine dialogs |
| `app.js` | UI + state (`S`), timeline, export pipeline, projects, settings, everything glue |
| `core.js` | Speech logic: Whisper chunked pass, 2-language detection per phrase, word list, script alignment, captions (+ per-word colors: `wl` auto, `wm` set by hand), SRT |
| `worker.js` | Runs Whisper (transformers.js) off the main thread; checkpoints/resume; GPU→CPU fallback |
| `download.js` | Resumable chunked model downloads (Range requests → IndexedDB → Cache) |
| `render.js` | Caption drawing (shared by preview + export), hook title, safe zones, branding overlay |
| `layers.js` | Background & presenter: MediaPipe selfie segmentation, compositing, background video reader |
| `slides.js` | PDF → slide images (pdf.js legacy build), slide timing |
| `merge.js` | Combine several videos into one |
| `audio.js` | Background music (library in IndexedDB `music:<id>`, ducking under speech, loop + fades) and noise removal (RNNoise, streamed, 48 kHz) |
| `publish.js` | Thumbnail frames, cover maker (MODNet cut-out), Claude prep, chapters |
| `claude.js` / `openai.js` | Optional APIs with the user's own keys (stored only in localStorage, never logged) |
| `store.js` | IndexedDB projects + files (`<id>`, `<id>:bg`, `<id>:slides`) |
| `log.js` | On-device diagnostics log, `APP_VERSION` |
| `coi.js` | Service worker: adds COOP/COEP headers (multi-threaded WASM) + offline cache |
| `lib/` | Self-hosted libraries (see below) |
| `brand/` | Built-in channel logos (256 px for the app, 900 px for the channel picture) |

## Must-know gotchas
- **Bump on every release:** `APP_VERSION` in `log.js`, and `CACHE` in `coi.js` when adding/removing files
  (also add new files to `SHELL` there). The start screen shows the version.
- **Self-host libraries.** Under COEP, Safari refuses cross-origin scripts inside workers. Everything in
  `lib/` is self-hosted: transformers.js 4.3.1 (+ onnxruntime wasm), mediabunny 1.61.3, MediaPipe
  tasks-vision 1.1.0 (+ selfie segmenter models), RNNoise (`@shiguredo/rnnoise-wasm` 2025.1.5, wasm inlined, loaded only when used), pdf.js 6.4.299 **legacy** build (modern build needs
  `Map.getOrInsertComputed`, missing in current browsers). Pin versions ≥ 14 days old.
- **GitHub secret scanning** flagged `Mistral3ForConditionalGeneration` in transformers.min.js as a Mistral
  key; it is rewritten with a `\u0061` escape. Re-apply if that file is ever replaced.
- **OPFS exports:** close the `FileSystemWritableFileStream` before `getFile()`, otherwise the File becomes
  unreadable (NotReadableError) later.
- `fetch()` in the service worker for app files uses `cache: 'no-cache'` so updates show up immediately.
- OpenAI `/audio/transcriptions` hides error bodies from browsers (no CORS on errors) → use "Test key"
  (`/v1/models`) for readable errors.
- Claude answers for Turkish text are long: fixes go in 20-line parts with a 16k token limit, split on
  `max_tokens`, refuse answers much shorter than the input, and offer Undo.
- Whisper on phones loops/skips: `transcribeWords` drops repeats; parts with loops, too few words or
  4 s+ speech gaps are re-read in ~9 s slices.

## Testing (how changes were verified so far)
- Serve `docs/` locally (`python -m http.server 8765`) and drive it with Playwright (Chromium; WebKit for
  Safari issues). Inject state via `window.__editor` (the `S` object) to skip slow steps.
- Speech logic is testable in Node with `@huggingface/transformers` and `core.js` directly.
- Use real WebM/VP9 test clips in headless Chromium (no H.264 decoder there).
- Always check exported files with ffprobe/ffmpeg frames, not just UI text.

## Easy mode backlog / decisions (pipeline, 2026-11-05)
**Next items, in order:** (1) the owner's first real YouTube upload from the dashboard, and see whether Google keeps it
locked private (see below); (2) look at the 2 private videos "2 abfahren" (rOf_Wc6WJME) and "3 am oder im Bahnhof"
(jH5r-IRzkis) — deferred by the owner; (3) Threads as the next platform plug-in (reuse `meta.py`; extend `tests/guard.py`'s Share fake to it); (4) ~~split
instagram.py~~ done; (5) ~~studio layout for horizontal recordings~~ done;
(6) the "turn your phone" idea.
- **Studio layout: the owner likes it** (video on one side, a "board" with the lesson's example sentences building up
  on the other: `layout.studio` + `layout.board`). Since 2026-11-05 it is the default for EVERY long video (the owner's
  choice), vertical and horizontal (`easy.use_studio`; horizontal: a card cut around her, `layout._studio_columns`), and
  smaller: the owner picked size option F of 7 previews (her face ~52 % of before: `STUDIO_CROP` 1.0 = whole recording
  height, `STUDIO_HEIGHT` 0.62 = card height on the canvas; per channel `channel.json "studio_size"`). Off per lesson
  with `settings.json {"studio": false}`. The small card sits level with the section header, and word cards go
  under it (`chips.place`; on her picture they never fit beside her face). Recordings with a burned-in subscribe
  banner get it cut by the card.
- **Idea: horizontal videos as they are + a "turn your phone" animation** (a phone rotating, 1-2 s) at the start, instead
  of cropping. Caveats: YouTube counts only vertical/square ≤ 3 min as a Short (a horizontal one is a normal video), and
  Reels show a horizontal video small between bars, so it fits the long video best. Not started.
- **YouTube uploads by the PC (done 2026-11-05, auto OFF at the deploy):** `platform_youtube.py` on publish.py
  (verb 'upload', breaker 'google'): sharing = find an existing upload of the file (record, else `youtube.find_upload`)
  or a resumable private upload; processing = record id at once (`publish_pending`), `youtube.apply_video` details, wait
  for processing (≤ 15 min); publishing = `make_public`. Dashboard: Publish has an Instagram/YouTube switch; "Upload"
  button with the same warnings + override tick; automatic uploads use the same settings window as Instagram.
  `easy.youtube_step` skips while a YouTube job is queued/running. OAuth consent screen is "In production" (checked and
  `youtube.py login` renewed 2026-11-05; Testing would kill the refresh token after 7 days). Not tried for real yet: the
  first real upload is the owner's. Risk: Google restricts uploads from unaudited API projects to private; if
  `make_public` can't lift it, either send the free YouTube API audit form or keep uploading in the app (the PC still
  finds and fills those uploads).
- **Lesson folder names (done 2026-11-05):** the watcher's `name_step` renames finished lessons whose folder name is a
  placeholder (`lessons.placeholder`: a number, new, yeni, ...) to `<her number> <thumbnail words>` with
  `lessons.rename` (work folder moved too, PostLock held, no queued/running job). Files keep their stem
  (`lessons.file_stem`), so records/queue (publish `_adopt` by sig)/re-renders still match. Folders 1-5 and `new`
  were renamed that day.
- **Platforms (research 2026-11-05):** Threads = worth it (official API, video, same Meta business; back in Turkey since
  2026-06-17; needs the Threads use case on a Meta app, go slowly with logins); TikTok = by hand / TikTok Studio only
  (its API posts SELF_ONLY until audited, and audits refuse own-account tools); X = maybe later (pay per use:
  $0.015 a post, $0.20 with a link; throttled in Turkey at times); LinkedIn, Telegram, own blog = maybe later;
  Facebook, Pinterest, Bluesky = given up.
- **Lesson posts (`posts.py`, done 2026-11-05):** blog.md + X thread + Threads post per lesson in `Output/extras/`
  (posts.md to copy, posts.json for future plug-ins); the link fills in after the YouTube upload.
- **Architecture plan** (review 2026-11-05; publish.py's lock/journal/reconcile/breaker must NOT be refactored):
  done 2026-11-05: `paths.py` (WORK, keys folder, Istanbul TZ, output lookup; old names kept as aliases), light
  `lessons.py` (dashboard/overview no longer import easy.py), tests for `youtube.next_slot`/`wanted`. Done 2026-11-05 too:
  instagram.py split into `meta.py` (Meta errors, classify, answer, Share: for Threads too), `instagram_content.py`
  (reels, captions, covers, records) and `instagram.py` (key, calls, posting, CLI; re-exports the rest, so
  `instagram.<name>` still works; tests patch CLOUDFLARED on meta and probe on instagram_content). With the next platform: platform link patterns / settle hook / optional
  `Item.file` in the Platform interface, `schedule.KNOWN` from the registry, stats + page JS driven by the registry,
  a shared candidate scanner; multi-part posts (X threads) need per-part journal state first.

## Backlog / ideas (not done)
- **Home page grouping (owner, 2026-11-06, "for later"):** put the Instagram Reels counts together with Instagram followers
  and likes in one group under the real Instagram logo; the same for YouTube (its video counts + subscribers/views).
- **Thumbnail maker in the Share tab, in the approved format** (user asked for it, "not now", 2026-10-29).
  Reference (archived 2026-11-05 to `G:\My Drive\YT archive (PC)`): `work (first hand-made videos)\scripts\thumbnail.py` → `V1\output\thumbnail_youtube.jpg`; the pipeline's own version is `pipeline/thumbnail.py`. 1280×720, dark navy
  + soft yellow glow, cut-out presenter right (white outline), red level badge ("B2 ALMANCA"), the lesson's German
  keywords stacked huge in yellow, a white Turkish hook question ("FARKI NE?"), TR-DE logo, German-flag stripe.
  The app's cover maker (`publish.js`, MODNet cut-out) is the natural place: add this as a layout, keywords from
  the word list/title.
- **Combine videos runs out of RAM on the phone** (Android, 8 GB; 7 iPhone clips, ~500 MB, Oct 2026 — that
  video was made on the PC instead). Likely causes in `merge.js`: `clipAudio` holds each clip's whole audio
  2–3× (decoded parts + OfflineAudioContext input + output); every frame is decoded and re-encoded at up
  to 1080p (iPhone 4K HEVC decode is heavy); the result is then re-encoded again on export and copied into
  IndexedDB. Ideas, best first: (1) when clips share codec/size/fps, **copy packets without re-encoding**
  (mediabunny `EncodedPacketSink` → `EncodedVideoPacketSource`/`EncodedAudioPacketSource`) — fast, tiny RAM;
  (2) **multi-clip projects**: keep the clips separate and only render once at export (no merge step);
  (3) stream audio per second instead of whole clips. Check the phone's diagnostics log first.
- Direct YouTube upload from the web app: not needed now — the home PC uploads and fills details (Easy mode).
- Slow zoom-in effects; intro/outro templates. (Background music + ducking: done 2026-10-29.)
- Music can't be browsed inside the app: YouTube's Audio Library has no API and needs a login, so the app links to it (`youtube.com/audiolibrary`) and the user adds downloaded files.
- Subscribe reminder can overlap a presenter box in the bottom corner — could avoid it.
- Slide info text doesn't refresh while playing.
