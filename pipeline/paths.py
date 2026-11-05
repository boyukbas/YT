"""Where things live on this PC, in one place. Modules keep their old names as aliases (easy.WORK_ROOT, publish.WORK,
instagram.KEYS, ...): tests, the guard and the dashboard's --dev checks read those.

WORK is read from PIPELINE_WORK when this module is first imported (tests and --dev set it to a scratch folder first)."""
import os
from datetime import timedelta, timezone
from pathlib import Path

WORK = Path(os.environ.get('PIPELINE_WORK', r'D:\YT\easy-work'))    # per-lesson work folders, posting state, logs
KEYS = Path.home() / '.shorts-pipeline'                             # API keys and tokens (never in the repo)
ISTANBUL = timezone(timedelta(hours=3))                              # Turkey has no daylight saving time
EXTRAS = 'extras'          # <lesson>/Output/extras/: everything except the finished videos (decided 2026-11-04)
OUTPUTS = ('Output (Easy)', 'Output')    # "Output (Easy)" when an Output folder made elsewhere already existed


def finished_output(lesson, need='status.json'):
    """The lesson's Easy-mode output folder (the one whose extras/ has `need`), or None."""
    return next((Path(lesson) / n for n in OUTPUTS if (Path(lesson) / n / EXTRAS / need).exists()), None)
