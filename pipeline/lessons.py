"""Lesson folders on the Drive: where a lesson's results go, its recordings, the channel profile, the Retry marker.
Light on purpose (no cv2, PIL or ffmpeg): the dashboard and the status page import this instead of easy.py."""
import json, shutil
from pathlib import Path
import paths
from tools import VIDEO_EXT, extras, slug

OURS = 'status.json'                     # an output folder with extras/status.json was made by Easy mode


def local_copy(src, dst):
    """Mirror a Drive folder to the PC. ffmpeg misreads looped images straight from the Drive mount (J:), so every
    asset the renderer touches comes from this local copy."""
    for f in Path(src).rglob('*'):
        if f.is_file():
            t = Path(dst) / f.relative_to(src)
            if not t.exists() or t.stat().st_size != f.stat().st_size or t.stat().st_mtime < f.stat().st_mtime:
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
    return Path(dst)


def load_profile(root):
    ch = local_copy(Path(root) / '_channel', paths.WORK / '_channel')
    sym = ch / 'fonts' / 'seguisym.ttf'                  # ✗ / ✓ on marked examples (layout.MARKS); Windows' own font
    if not sym.exists() and Path(r'C:\Windows\Fonts\seguisym.ttf').exists():
        shutil.copy2(r'C:\Windows\Fonts\seguisym.ttf', sym)
    p = json.loads((ch / 'channel.json').read_text(encoding='utf-8'))
    assets = {'logo': ch / p['logo'], 'subscribe': ch / p['subscribe'], 'fonts': ch / 'fonts', 'music': ch / 'music'}
    pal = p['palette']
    p['colors'] = {'tr': pal['white']['hex'], 'de': pal['yellow']['hex'], 'translation': pal['soft']['hex'], 'pattern': pal['red']['hex']}
    terms = ch / 'terms.txt'
    p['terms'] = [t.strip() for t in terms.read_text(encoding='utf-8').splitlines() if t.strip()] if terms.exists() else []
    return p, assets


def output_dir(lesson):
    out = lesson / 'Output'
    if out.exists() and not (extras(out) / OURS).exists():
        return lesson / 'Output (Easy)'
    return out


def lesson_videos(lesson):
    return sorted(p for p in lesson.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT)


def retry_marker(lesson):
    """Set by the dashboard's Retry button: the watcher runs a stopped lesson again even though its clips are the same."""
    return paths.WORK / slug(lesson.name) / 'retry'
