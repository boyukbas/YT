"""Lesson folders on the Drive: where a lesson's results go, its recordings, the channel profile, the Retry marker.
Light on purpose (no cv2, PIL or ffmpeg): the dashboard and the status page import this instead of easy.py.

Folder names: Sinem may name a new lesson folder '4' or 'new'. Once the lesson is finished, the watcher renames such a
placeholder name to a short, meaningful one from the thumbnail's German words ('4' -> '4 Lass uns kennenlernen'),
together with the PC's work folder, under the posting lock. Names she chose herself are never touched.
  python -I lessons.py names "<channel folder>"                 what the watcher would rename (changes nothing)
  python -I lessons.py rename "<lesson folder>" ["<new name>"]   rename one lesson now (suggested name by default)"""
import json, re, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))          # python -I doesn't add it
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


PLACEHOLDER = re.compile(r'^(\d{1,3}|new|yeni|neu|ders|lesson|video|test|untitled|new folder|yeni klas[öo]r|'
                         r'neuer ordner)(\s+\(?\d{1,3}\)?|\s*\(\d{1,3}\))?$', re.I)
_BAD = re.compile(r'[\\/:*?"<>|+!#%&{}~]')     # not in a Windows / Drive folder name (and + ? ! read badly)
VIDEO_END = '-video.mp4'


def placeholder(name) -> bool:
    """True for a name that says nothing about the lesson: a number, 'new', 'yeni', 'New folder (2)', ..."""
    return bool(PLACEHOLDER.match(str(name).strip()))


def clean_name(name) -> str:
    return ' '.join(_BAD.sub(' ', str(name)).split()).strip(' .')


def file_stem(lesson) -> str:
    """The name in front of the finished files ('4' for 4-video.mp4). The files keep it after the folder is renamed
    (their records and queue entries are keyed by file name), so a later render writes over them, not next to them."""
    out = output_dir(Path(lesson))
    if out.is_dir():
        for f in sorted(out.glob('*' + VIDEO_END)):
            return f.name[:-len(VIDEO_END)]
    return slug(Path(lesson).name)


def suggested_name(lesson) -> str | None:
    """'<Sinem's number> <the thumbnail's German words>', the words cut to 40 characters ('5 sich kümmern um
    Akkusativ'); None when the lesson has no thumbnail words yet."""
    p = extras(output_dir(Path(lesson))) / 'youtube.json'
    try:
        words = json.loads(p.read_text(encoding='utf-8'))['thumbnails'][0]['words']
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    topic = clean_name(' '.join(str(w) for w in words if isinstance(w, str)))
    if len(topic) > 40:
        topic = topic[:41].rsplit(' ', 1)[0]
    if not topic:
        return None
    n = re.match(r'^\s*(\d{1,3})\b', Path(lesson).name)
    return f'{n.group(1)} {topic}' if n else topic


def finished(lesson) -> bool:
    out = output_dir(Path(lesson))
    try:
        return json.loads((extras(out) / OURS).read_text(encoding='utf-8')).get('state') == 'done'
    except (OSError, ValueError):
        return False


def rename(lesson, new, owner='cli', log=print) -> Path:
    """Rename a lesson folder on the Drive to new, and its work folder on the PC with it, while no post or upload is
    going out (the posting lock is held; publish.Busy otherwise). The finished files keep their names; queue entries
    follow them (publish.py matches renamed folders by file name and size). Notes the old name in status.json.
    Raises ValueError (bad name), FileExistsError (taken), RuntimeError (a job is waiting), OSError (in use)."""
    import publish                                  # here: lessons.py stays light for the status page
    lesson, new = Path(lesson), clean_name(new)
    if not new or new.startswith(('_', '.')) or len(new) > 60:
        raise ValueError(f'"{new}" cannot be a lesson folder name')
    target = lesson.with_name(new)
    if target.exists() and target.resolve() != lesson.resolve():
        raise FileExistsError(f'"{new}" is already a folder')
    wo, wn = paths.WORK / slug(lesson.name), paths.WORK / slug(new)
    if wn != wo and wn.exists():
        raise FileExistsError(f'the work folder {wn} is already there')
    busy = [n for n in publish.PLATFORMS if (publish.current_job(n) or {}).get('state') in ('queued', 'running')]
    if busy:
        raise RuntimeError(f'a {", ".join(busy)} job is waiting or running')
    with publish.PostLock(owner=owner):
        moved = wn != wo and wo.exists()
        if moved:
            wo.rename(wn)
        try:
            lesson.rename(target)
        except OSError:
            if moved:
                wn.rename(wo)
            raise
    st = extras(output_dir(target)) / OURS
    try:
        d = json.loads(st.read_text(encoding='utf-8'))
        d['renamed_from'] = list(d.get('renamed_from') or []) + [lesson.name]
        st.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    except (OSError, ValueError):
        pass                                        # only a note
    log(f'folder renamed: "{lesson.name}" -> "{target.name}"')
    return target


def to_rename(root) -> list:
    """(lesson, suggested name) for every finished lesson whose folder has a placeholder name."""
    out = []
    for lesson in sorted(p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(('_', '.'))):
        if placeholder(lesson.name) and finished(lesson):
            new = suggested_name(lesson)
            if new and new != lesson.name and not lesson.with_name(new).exists():
                out.append((lesson, new))
    return out


def retry_marker(lesson):
    """Set by the dashboard's Retry button: the watcher runs a stopped lesson again even though its clips are the same."""
    return paths.WORK / slug(lesson.name) / 'retry'


def main(argv):
    if len(argv) == 2 and argv[0] == 'names':
        found = to_rename(argv[1])
        for lesson, new in found:
            print(f'"{lesson.name}" -> "{new}"')
        print(f'{len(found)} folder(s) would be renamed' if found else 'nothing to rename')
    elif len(argv) in (2, 3) and argv[0] == 'rename':
        new = argv[2] if len(argv) == 3 else suggested_name(argv[1])
        if not new:
            raise SystemExit('no suggested name (the lesson has no thumbnail words yet): give one')
        rename(argv[1], new)
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1:])
