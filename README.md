# Shorts Editor (iOS 18+, iPhone)

Trim, cut, remove silences, auto-subtitle (on-device), export to Photos + .srt.

## Install — with a Mac
1. `brew install xcodegen` then `xcodegen generate` in this folder.
2. Open `ShortsEditor.xcodeproj`, pick your Apple ID team under Signing, change the bundle ID.
3. Plug in the iPhone, press Run. On the phone: Settings → Privacy & Security → Developer Mode → On.

## Install — without a Mac
1. Push this folder to a GitHub repo. The "Build IPA" action runs on a free macOS runner.
2. Download `ShortsEditor.ipa` from the run's Artifacts.
3. Sideload with a free Apple ID (e.g. Sideloadly on Windows/Mac, or a Linux sideloader).
   Free-ID installs expire after 7 days — just re-sideload.

## Tips
- Subtitles run on-device with WhisperKit (Whisper). First use downloads the model once (Fast ~220 MB, Best ~630 MB) — use Wi-Fi; after that it works offline.
- Two languages in one video (e.g. German with Turkish explanations): set "Spoken language" to the main one and "Words in" to the other. Each subtitle line gets its language's color; fix any line's language with the tag button in "Fix subtitle text or language".
- The .srt is always created too. YouTube shows .srt in a single color.
- Silences: "Auto level" adapts to your mic/room. Raise "Keep around speech" if cuts feel abrupt.
- Burned-in subtitles OR upload the .srt to YouTube Studio — not both.

## Web version (no install, no 7-day expiry)

`docs/` is a web app with the same core features: trim, remove pauses, on-device Whisper
subtitles in two languages with a color per language, burned-in export and .srt.
Hosted with GitHub Pages at https://boyukbas.github.io/YT/ (Settings → Pages → Branch `main`, folder `/docs`).
On iPhone: open it in Safari → Share → Add to Home Screen.
First subtitle run downloads the speech model (~250 MB, cached afterwards).
