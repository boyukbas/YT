# Easy mode pipeline (home PC)

Sinem drops the raw clips of a lesson into a Drive folder; this PC turns them into a finished video plus
everything YouTube needs, and writes it back to the same folder.

```
J:\My Drive\SINEM-YouTube\
  _channel\            channel profile (set once): channel.json, logo.png, subscribe.png, fonts\, music\, terms.txt, brand.png
  <lesson>\            raw clips (.mov/.mp4), optional settings.json  ({"short": false} = no Short version)
    Output\            results (or "Output (Easy)" if an Output folder made elsewhere exists)
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
   Word cards (`chips.py`) and the subscribe banner go where nothing important is.
5. **Render**: libass subtitles in the channel palette, logo (skipped when the recording already has it),
   word cards, subscribe banner, noise reduction + compression + two-pass loudness to −14 LUFS, optional ducked music.
6. **Check**: every subtitle line is checked on the finished video for face overlap; loudness measured; frame sheet.
7. **YouTube texts + thumbnails** (`texts.py`, `thumbnail.py`, Claude looks at candidate frames).

## Claude backend (`llm.py`)
- `ANTHROPIC_API_KEY` or `%USERPROFILE%\.shorts-pipeline\anthropic_key` → API
- else `claude -p` (Claude Code). For unattended runs log it in once: `claude setup-token`
- else **manual**: the lesson's STATUS says "Waiting"; a Claude Code session answers `D:\YT\easy-work\<lesson>\llm\<step>.prompt.md`
  by writing `<step>.answer.json`, then run again. Answers are cached, so re-runs never ask twice.

## Run
```
D:\YT\work\venv\Scripts\python -I easy.py run "J:\My Drive\SINEM-YouTube\<lesson>"
D:\YT\work\venv\Scripts\python -I easy.py watch "J:\My Drive\SINEM-YouTube" --once     # Task Scheduler, every 10 min
```
A lesson is picked up when its clips haven't changed for 10 minutes and differ from the last run.
Work files: `D:\YT\easy-work\<lesson>` (safe to delete; delete a step's files there to redo it).
