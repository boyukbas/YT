# Shorts Editor — project notes for Claude Code

Video editor for the YouTube channel **Almanca Dil Atölyesi** (youtube.com/@sinembb): German lessons
for Turkish speakers. Videos mix Turkish (main language) and German. Goal: cover all editing needs of
an education channel — Shorts and normal videos from the same recording.

The owner talks to Claude in **Turkish**; the app UI is in **English** (deliberate). They mostly use an
Android phone (Chrome, 8 GB, no WebGPU) and sometimes an iPad Pro or a PC with an RTX 5060 Ti.
They like step-by-step answers, visuals, and clear "what was verified vs. assumed".

## Layout
- `docs/` — **the web app (live)**, served by GitHub Pages from `main` / `docs`:
  https://boyukbas.github.io/YT/  (installable PWA, works offline after first use)
- `ShortsEditor/`, `project.yml`, `.github/workflows/build-ipa.yml` — native iOS app (SwiftUI + WhisperKit).
  **Shelved**: sideloading failed (Apple login / anisette). Builds still pass in CI; don't invest further unless asked.
- The iOS workflow ignores `docs/**` and `README.md` changes.

## Web app files (`docs/`)
| File | What |
|---|---|
| `index.html` | All markup + CSS. Tabs: Video / Subtitles / Look / Share; Settings, Help, Combine dialogs |
| `app.js` | UI + state (`S`), timeline, export pipeline, projects, settings, everything glue |
| `core.js` | Speech logic: Whisper chunked pass, 2-language detection per phrase, word list, script alignment, captions, SRT |
| `worker.js` | Runs Whisper (transformers.js) off the main thread; checkpoints/resume; GPU→CPU fallback |
| `download.js` | Resumable chunked model downloads (Range requests → IndexedDB → Cache) |
| `render.js` | Caption drawing (shared by preview + export), hook title, safe zones, branding overlay |
| `layers.js` | Background & presenter: MediaPipe selfie segmentation, compositing, background video reader |
| `slides.js` | PDF → slide images (pdf.js legacy build), slide timing |
| `merge.js` | Combine several videos into one |
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
  tasks-vision 1.1.0 (+ selfie segmenter models), pdf.js 6.4.299 **legacy** build (modern build needs
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

## Backlog / ideas (not done)
- Direct YouTube upload: blocked by Google (unverified apps' uploads are locked private). Option discussed:
  user uploads in the YouTube app, then the app applies title/description/thumbnail via the API (needs a
  Google Cloud OAuth client). User postponed the decision.
- Background music with automatic ducking; slow zoom-in effects; intro/outro templates.
- Subscribe reminder can overlap a presenter box in the bottom corner — could avoid it.
- Slide info text doesn't refresh while playing.
