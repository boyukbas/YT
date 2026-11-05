"""Publishing settings (WORK/publishing.json) and the pure timing rules for automatic and manual posts.
Istanbul time is a fixed UTC+3 (Turkey has no daylight saving time; this PC's Python has no tz database).
No network; imports only the standard library."""
from __future__ import annotations
import json, os, re, secrets, threading, time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WORK = Path(os.environ.get('PIPELINE_WORK', r'D:\YT\easy-work'))
SETTINGS = WORK / 'publishing.json'
ISTANBUL = timezone(timedelta(hours=3))
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
KNOWN = ('instagram',)                                   # platforms that may have settings (= publish.PLATFORMS)
FIELDS = ('auto', 'every_days', 'days', 'post_after', 'post_before', 'min_gap_hours', 'hard_cap_24h', 'start_date')
DEFAULTS = {'auto': False, 'every_days': 1, 'days': list(DAYS), 'post_after': '20:00', 'post_before': '23:59',
            'min_gap_hours': 20, 'hard_cap_24h': 3, 'start_date': None}
PRESETS = {
    'every_evening':   {'label': 'Every evening', 'every_days': 1, 'days': list(DAYS),
                        'post_after': '20:00', 'post_before': '23:59', 'min_gap_hours': 20},
    'every_other_day': {'label': 'Every other day', 'every_days': 2, 'days': list(DAYS),
                        'post_after': '20:00', 'post_before': '23:59', 'min_gap_hours': 20},
    'weekdays':        {'label': 'Weekdays', 'every_days': 1, 'days': ['mon', 'tue', 'wed', 'thu', 'fri'],
                        'post_after': '20:00', 'post_before': '23:59', 'min_gap_hours': 20},
    'mon_wed_fri':     {'label': 'Mon, Wed, Fri', 'every_days': 1, 'days': ['mon', 'wed', 'fri'],
                        'post_after': '20:00', 'post_before': '23:59', 'min_gap_hours': 20},
}
ACKS = ('auto_on', 'gap_under_20', 'resume_auto', 'reset_settings')
SEARCH_DAYS = 15
WINDOW_MIN = 40                                          # minutes: one retry (30 min) + one watcher run (10)
AUTO_SLACK = timedelta(minutes=10)                       # one watcher run: an automatic post may come this much early

MESSAGES = {                                             # spec 4.1, shown under the field
    'auto': 'Must be on or off.',
    'every_days': 'Choose 1 to 7 days.',
    'days': 'Choose at least one day.',
    'post_after': 'Use HH:MM between 00:00 and 23:19.',
    'post_before': 'Must be 40 minutes or more after the start, and 23:59 at the latest.',
    'min_gap_hours': 'Choose 1 to 168 hours, in half hours.',
    'hard_cap_24h': 'Can only be changed in publishing.json on the PC.',
    'start_date': 'Use a date like 2026-11-03, or leave it empty.',
}
UNKNOWN_DAY = 'Unknown day.'
UNKNOWN_SETTING = 'Unknown setting.'
READ_ONLY = MESSAGES['hard_cap_24h']                     # hard_cap_24h changed through the API (code 'read_only')
_IN_FILE = {'hard_cap_24h': 'Choose 1 to 5, in publishing.json on the PC.'}     # a bad value in the file itself

_DAY = timedelta(hours=24)
_RETRIES = (0.2, 0.4, 0.6, 0.8, 1.0)                     # waits before the 5 retries of a refused write (3 s in all)
_SAVING = threading.Lock()                               # one settings write at a time in this process (the dashboard)
_SETTINGS0 = SETTINGS
_HHMM = re.compile(r'([01][0-9]|2[0-3]):[0-5][0-9]')
_YMD = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


@dataclass(frozen=True)
class Post:
    """One post that counts for the timing rules (from a record, the journal, a hand mark or the account)."""
    at: datetime                 # aware UTC
    by: str                      # 'auto' | 'manual' | 'cli' | 'hand' | 'outside' | 'old'
    key: str | None = None       # item key when known
    id: str | None = None        # platform media id when known ('hand' for a hand mark)


@dataclass(frozen=True)
class Check:
    """One rule result. level: 'block' (cannot post), 'warn' (needs the user's override), 'info' (shown only)."""
    code: str
    level: str
    text: str
    data: dict = field(default_factory=dict)


@dataclass
class Loaded:
    """Result of load_settings()."""
    doc: dict                    # whole document; platforms filled with defaults; every auto False when invalid
    problems: list[str]          # plain messages; non-empty means the file is unreadable or invalid
    migrated: bool               # True when this call created the file from the old pause flag


class SettingsError(ValueError):
    """Validation failed. .fields = {field: message}; .code = 'invalid' | 'read_only'."""
    def __init__(self, fields: dict[str, str], code: str = 'invalid'):
        super().__init__('; '.join(f'{k}: {m}' for k, m in fields.items()))
        self.fields, self.code = dict(fields), code


class StaleSettings(Exception):
    """The client's rev is not the file's rev. .doc = the current document."""
    def __init__(self, doc: dict):
        super().__init__(f'The settings changed (rev {doc.get("rev")}).')
        self.doc = doc


class NeedsAck(Exception):
    """The change needs acknowledgements. .acks = the missing ack codes (subset of ACKS)."""
    def __init__(self, acks: list[str], doc: dict | None = None):
        super().__init__('Needs confirmation: ' + ', '.join(acks))
        self.acks = list(acks)
        self.doc = doc               # the merged document that would be saved (None for reset_settings)


class InvalidFile(Exception):
    """The settings file is unreadable or invalid; only reset_settings() may replace it. .problems = messages."""
    def __init__(self, problems: list[str]):
        super().__init__(' '.join(problems))
        self.problems = list(problems)


# ---------- settings file ----------

def load_settings(root: Path | None = None) -> Loaded:
    """Read publishing.json. When it does not exist, migrate once: auto = not (WORK/instagram-paused exists),
    post_after '20:00', post_before '23:59', min_gap_hours 20, every_days 1, all days, hard_cap_24h 3,
    start_date = root/_channel/channel.json instagram.start when it is a valid date (None when root is None).
    The file is created only if absent (temp file + os.link; FileExistsError means another process migrated
    first and its file is read). An unreadable or invalid file is never overwritten here."""
    path, migrated = _path(), False
    try:
        text = _read_text(path)
    except FileNotFoundError:
        try:
            migrated = _migrate(path, root)
        except OSError as e:
            return _unusable('created', e, False)
        try:
            text = _read_text(path)
        except (OSError, UnicodeError) as e:
            return _unusable('read', e, migrated)
    except (OSError, UnicodeError) as e:
        return _unusable('read', e, False)
    return Loaded(*_parse(text), migrated)


def validate(values: dict) -> dict[str, str]:
    """Check one platform's complete settings. Returns {field: message} ({} when valid), with the messages of
    spec section 4.1; unknown fields give 'Unknown setting.'; bools are not accepted as numbers."""
    v = values
    out = {k: UNKNOWN_SETTING for k in v if k not in FIELDS}
    if not isinstance(v.get('auto'), bool):
        out['auto'] = MESSAGES['auto']
    if not (_whole(v.get('every_days')) and 1 <= v['every_days'] <= 7):
        out['every_days'] = MESSAGES['every_days']
    days = v.get('days')
    if not isinstance(days, (list, tuple)) or not days:
        out['days'] = MESSAGES['days']
    elif not all(isinstance(d, str) and d in DAYS for d in days) or len(set(days)) < len(days):
        out['days'] = UNKNOWN_DAY
    start, end = _minutes(v.get('post_after')), _minutes(v.get('post_before'))
    if start is None or start > 23 * 60 + 59 - WINDOW_MIN:
        out['post_after'], start = MESSAGES['post_after'], None     # then the end is checked on its own
    if end is None or (start is not None and end < start + WINDOW_MIN):
        out['post_before'] = MESSAGES['post_before']
    gap = v.get('min_gap_hours')
    if not (type(gap) in (int, float) and 1 <= gap <= 168 and float(gap * 2).is_integer()):
        out['min_gap_hours'] = MESSAGES['min_gap_hours']
    if not (_whole(v.get('hard_cap_24h')) and 1 <= v['hard_cap_24h'] <= 5):
        out['hard_cap_24h'] = MESSAGES['hard_cap_24h']
    if v.get('start_date') not in (None, '') and not _date(v.get('start_date')):
        out['start_date'] = MESSAGES['start_date']
    return out


def platform_settings(loaded: Loaded, platform: str) -> dict:
    """The platform's settings with defaults filled in; DEFAULTS (auto False) when the platform is missing or
    the file is invalid."""
    values = loaded.doc.get('platforms', {}).get(platform)
    if loaded.problems or not values:
        return _defaults()
    out = {**_defaults(), **values}
    out['days'] = list(out['days'])
    return out


def save_settings(platform: str, patch: dict, rev: int, by: str, ack: list[str] | tuple = (), *,
                  dry_run: bool = False) -> dict:
    """Merge patch into the platform's settings, validate, and (unless dry_run) write the whole document:
    rev + 1, updated, updated_by = by; temp file + os.replace retried on PermissionError 5 times over ~3 s.
    The pause file follows auto: saved False writes it ('<by> <YYYY-MM-DD HH:MM>'), saved True deletes it.
    Returns the new document (dry_run: the merged document, not written).
    Raises SettingsError (unknown field, bad value, or hard_cap_24h different from the current value: code
    'read_only'), StaleSettings (rev differs; never for the patch {'auto': False}: stopping is always safe), NeedsAck ('auto_on' when the effective state goes off -> on;
    'gap_under_20' when min_gap_hours changes to under 20), InvalidFile, KeyError (platform not in KNOWN)."""
    if platform not in KNOWN:
        raise KeyError(platform)
    _who(by)
    with _SAVING:
        loaded = load_settings()
        if loaded.problems:
            raise InvalidFile(loaded.problems)
        doc = loaded.doc
        if (type(rev) is not int or rev != doc['rev']) and patch != {'auto': False}:
            raise StaleSettings(doc)                     # a plain stop is never stale (F21)
        if not isinstance(patch, dict):
            raise SettingsError({'settings': 'Send the changed settings as an object.'})
        current = doc['platforms'][platform]
        cap = patch.get('hard_cap_24h', current['hard_cap_24h'])
        if not (_whole(cap) and cap == current['hard_cap_24h']):
            raise SettingsError({'hard_cap_24h': READ_ONLY}, 'read_only')
        merged = {**current, **patch}
        errors = validate(merged)
        if errors:
            raise SettingsError(errors)
        new = _normal(merged)
        needs = []
        if new['auto'] and not effective_auto(loaded, platform)[0]:
            needs.append('auto_on')                      # the effective state goes from off to on
        if new['min_gap_hours'] < 20 and new['min_gap_hours'] != current['min_gap_hours']:
            needs.append('gap_under_20')
        out = {**doc, 'platforms': {**doc['platforms'], platform: new}}
        missing = [a for a in needs if a not in _acks(ack)]
        if missing:
            raise NeedsAck(missing, out)
        if dry_run:
            return out
        now = datetime.now(timezone.utc)
        out.update(rev=doc['rev'] + 1, updated=iso(now), updated_by=by)
        if not new['auto']:
            _pause(platform, by, now)                    # first: even if the write fails, automatic posting stops
        _write(_path(), out)
        if new['auto']:
            _retry(lambda: pause_file(platform).unlink(missing_ok=True))
        return out


def reset_settings(by: str, ack: list[str] | tuple) -> dict:
    """Replace the whole file with DEFAULTS for every KNOWN platform (auto False), rev = old rev + 1 (or 1),
    and write every pause file. Raises NeedsAck(['reset_settings']) without that ack. Returns the document."""
    _who(by)
    if 'reset_settings' not in _acks(ack):
        raise NeedsAck(['reset_settings'])
    with _SAVING:
        path = _path()
        try:
            old = json.loads(_read_text(path))
        except (OSError, ValueError, RecursionError):
            old = {}
        now = datetime.now(timezone.utc)
        doc = {'version': 1, 'rev': _head(old)['rev'] + 1, 'updated': iso(now), 'updated_by': by,
               'platforms': {p: _defaults() for p in KNOWN}}
        for p in KNOWN:
            _pause(p, by, now)
        _write(path, doc)
        return doc


def pause_file(platform: str) -> Path:
    """WORK/'<platform>-paused' (Instagram: the existing instagram-paused)."""
    return WORK / f'{platform}-paused'


def pause_info(platform: str) -> dict | None:
    """{'since': ISO UTC of the file's mtime, 'by': first word of its text or None} when it exists, else None."""
    p = pause_file(platform)
    try:
        since = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    except OSError:
        return None
    try:
        with p.open('rb') as f:
            head = f.read(512)
    except OSError:
        head = b''
    # 'type nul >' makes it empty; PowerShell writes UTF-16 with a BOM; the dashboard writes UTF-8
    words = head.decode('utf-16' if head[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig', 'replace').split()
    return {'since': iso(since), 'by': words[0] if words else None}


def effective_auto(loaded: Loaded, platform: str) -> tuple[bool, str]:
    """(on, reason) with reason 'on' | 'invalid' | 'off' | 'stopped_by_file', checked in that order."""
    if loaded.problems:
        return False, 'invalid'
    if not platform_settings(loaded, platform)['auto']:
        return False, 'off'
    if pause_file(platform).exists():
        return False, 'stopped_by_file'
    return True, 'on'


def _path() -> Path:
    """SETTINGS, or WORK/publishing.json when only WORK was moved (tests)."""
    return SETTINGS if SETTINGS != _SETTINGS0 else WORK / SETTINGS.name


def _defaults() -> dict:
    return {**DEFAULTS, 'days': list(DAYS)}


def _who(by) -> None:
    """by (who saves) must be a string. publish.save_settings and reset_settings take ack before by, so a call
    with the two swapped fails here, before anything is written."""
    if not isinstance(by, str):
        raise TypeError(f'by must be a str (who saves), not {type(by).__name__}')


def _acks(ack) -> set:
    """The given ack codes; anything but a list, tuple or set (a bare string, None) gives none."""
    return {a for a in ack if isinstance(a, str)} if isinstance(ack, (list, tuple, set, frozenset)) else set()


def _unusable(verb: str, e: BaseException, migrated: bool) -> Loaded:
    """The Loaded for a settings file that could not be created or read: invalid, so automatic posting is off."""
    return Loaded(_blank({}), [f'publishing.json could not be {verb} ({type(e).__name__}).'], migrated)


def _blank(raw) -> dict:
    """The document used while the file is invalid: its rev and stamps, DEFAULTS (auto False) everywhere."""
    return {**_head(raw), 'platforms': {p: _defaults() for p in KNOWN}}


def _head(raw) -> dict:
    """version, rev, updated and updated_by of a parsed file (rev 0 when it is missing or not a whole number)."""
    raw = raw if isinstance(raw, dict) else {}
    rev = raw.get('rev')
    text = lambda k: raw[k] if isinstance(raw.get(k), str) else None
    return {'version': 1, 'rev': rev if _whole(rev) and rev >= 0 else 0,
            'updated': text('updated'), 'updated_by': text('updated_by')}


def _parse(text: str) -> tuple[dict, list[str]]:
    """(document, problems) of the file's text. Unknown top-level keys and platforms are ignored (and dropped by
    the next save); a platform missing from the file gets DEFAULTS."""
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as e:
        return _blank({}), [f'publishing.json is not valid JSON ({e}).']
    if not isinstance(raw, dict):
        return _blank({}), ['publishing.json must hold one JSON object.']
    problems, platforms = [], {}
    if not _whole(raw.get('version')) or raw['version'] != 1:
        problems.append('publishing.json: "version" must be 1.')
    found = raw.get('platforms')
    if not isinstance(found, dict):
        problems.append('publishing.json: "platforms" must be an object.')
        found = {}
    for name in KNOWN:
        values = found.get(name, {})
        if not isinstance(values, dict):
            problems.append(f'publishing.json: "{name}" must be an object.')
            continue
        merged = {**_defaults(), **values}
        errors = validate(merged)
        problems += [f'{name} {k}: {_IN_FILE.get(k, m)}' for k, m in errors.items()]
        if not errors:
            platforms[name] = _normal(merged)
    if problems:
        return _blank(raw), problems
    return {**_head(raw), 'platforms': platforms}, []


def _normal(values: dict) -> dict:
    """Valid settings as they are stored: FIELDS order, days in week order, whole hours as int, '' as no date."""
    out = {k: values[k] for k in FIELDS}
    gap = out['min_gap_hours']
    out.update(days=[d for d in DAYS if d in out['days']], min_gap_hours=int(gap) if float(gap).is_integer() else gap,
               start_date=out['start_date'] or None)
    return out


def _migrate(path: Path, root: Path | None) -> bool:
    """Create publishing.json from today's state (spec 4.4). True when this call created it, False when another
    process was first (its file is then read). Raises OSError when it cannot be written."""
    now = datetime.now(timezone.utc)
    doc = {'version': 1, 'rev': 1, 'updated': iso(now), 'updated_by': 'migration',
           'platforms': {p: {**_defaults(), 'auto': not pause_file(p).exists(), 'start_date': _channel_start(root, p)}
                         for p in KNOWN}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp(path, doc)
    try:
        os.link(tmp, path)                               # creates publishing.json only if it is absent
        return True
    except FileExistsError:
        return False
    except OSError:                                      # no hard links on this drive: replace, unless someone was first
        if path.exists():
            return False
        _retry(lambda: os.replace(tmp, path))
        return True
    finally:
        _drop(tmp)


def _channel_start(root: Path | None, platform: str) -> str | None:
    """root/_channel/channel.json <platform>.start when it is a real 'YYYY-MM-DD' date, else None."""
    if root is None:
        return None
    try:
        p = Path(root) / '_channel' / 'channel.json'
        start = json.loads(p.read_text(encoding='utf-8-sig'))[platform]['start']
    except (OSError, ValueError, LookupError, TypeError, RecursionError):
        return None
    return start if _date(start) else None


def _write(path: Path, doc: dict) -> None:
    """doc as JSON, atomically: a temp file in the same folder, then os.replace (retried, see _retry)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp(path, doc)
    try:
        _retry(lambda: os.replace(tmp, path))
    finally:
        _drop(tmp)


def _temp(path: Path, doc: dict) -> Path:
    """A new temp file next to path holding doc (same folder, so os.replace and os.link stay on one drive)."""
    tmp = path.with_name(f'{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}')
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    except BaseException:
        _drop(tmp)
        raise
    return tmp


def _drop(p: Path) -> None:
    """Delete a temp file if it is still there; never raises."""
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _retry(act):
    """act(), retried on PermissionError (Windows refuses while another process has the file open) 5 times over
    about 3 s; the last PermissionError is raised."""
    for wait in _RETRIES + (None,):
        try:
            return act()
        except PermissionError:
            if wait is None:
                raise
            time.sleep(wait)


def _read_text(path: Path) -> str:
    """The file's text (a UTF-8 BOM from a hand edit is fine). PermissionError, which Windows gives for a moment
    while another process replaces the file, is retried briefly."""
    for wait in (0.05, 0.1, 0.2, None):
        try:
            return path.read_text(encoding='utf-8-sig')
        except PermissionError:
            if wait is None:
                raise
            time.sleep(wait)


def _pause(platform: str, by: str, now: datetime) -> None:
    """Create the platform's pause file ('<by> <YYYY-MM-DD HH:MM>', Istanbul) unless it exists (then it is kept)."""
    p = pause_file(platform)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open('x', encoding='utf-8') as f:
            f.write(f'{by} {local(now):%Y-%m-%d %H:%M}\n')
    except FileExistsError:
        pass


def _whole(x) -> bool:
    return type(x) is int                                # bool is a subclass of int, so it is excluded here


def _minutes(hhmm) -> int | None:
    """'HH:MM' as minutes after midnight, or None when it is not a valid time of day."""
    if isinstance(hhmm, str) and _HHMM.fullmatch(hhmm):
        return int(hhmm[:2]) * 60 + int(hhmm[3:])
    return None


def _date(s) -> date | None:
    """'YYYY-MM-DD' as a date, or None when it is not a real date written that way."""
    if isinstance(s, str) and _YMD.fullmatch(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            pass
    return None


# ---------- time ----------

def iso(t: datetime) -> str:
    """t as ISO UTC 'YYYY-MM-DDTHH:MM:SSZ' (the API's time format)."""
    return _utc(t).strftime('%Y-%m-%dT%H:%M:%SZ')


def local(t: datetime) -> datetime:
    """t in Istanbul time (aware, UTC+3)."""
    return _utc(t).astimezone(ISTANBUL)


def local_date(t: datetime) -> str:
    """t's Istanbul date as 'YYYY-MM-DD'."""
    return local(t).date().isoformat()


def at_local(day: str, hhmm: str) -> datetime:
    """Istanbul 'YYYY-MM-DD' + 'HH:MM' as an aware UTC datetime."""
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[3:5]), tzinfo=ISTANBUL).astimezone(timezone.utc)


def fmt(t: datetime | None) -> str:
    """'Thu 5 Nov 20:00' in Istanbul time ('' for None); built with f-strings (no %-d on Windows)."""
    return f'{fmt_day(t)} {local(t):%H:%M}' if t else ''


def fmt_day(t: datetime | None) -> str:
    """'Thu 5 Nov' in Istanbul time ('' for None)."""
    if not t:
        return ''
    t = local(t)
    return f'{DAYS[t.weekday()].title()} {t.day} {_MONTHS[t.month - 1]}'


def pacific_offset(t: datetime) -> timedelta:
    """US Pacific UTC offset at t: -7 h from the second Sunday of March 10:00 UTC to the first Sunday of November
    09:00 UTC, else -8 h (US rule since 2007)."""
    t = _utc(t)
    begin, end = _sunday(t.year, 3, 2), _sunday(t.year, 11, 1)
    summer = (datetime(begin.year, begin.month, begin.day, 10, tzinfo=timezone.utc) <= t
              < datetime(end.year, end.month, end.day, 9, tzinfo=timezone.utc))
    return timedelta(hours=-7 if summer else -8)


def pacific_midnight_after(t: datetime) -> datetime:
    """The next midnight in US Pacific time after t, as aware UTC (YouTube quota reset: 11:00 Istanbul in
    winter, 10:00 in summer)."""
    t = _utc(t)
    wall = t + pacific_offset(t)                         # Pacific clock time (still tagged UTC)
    midnight = datetime(wall.year, wall.month, wall.day, tzinfo=timezone.utc) + timedelta(days=1)
    guess = midnight - pacific_offset(t)
    return midnight - pacific_offset(guess)              # the clock may change between t and that midnight (at 2:00)


def _utc(t: datetime) -> datetime:
    """t as aware UTC (a naive t is taken as UTC)."""
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _sunday(year: int, month: int, n: int) -> date:
    """The n-th Sunday of the month."""
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


# ---------- timing rules (pure) ----------

def last_post(history: list[Post]) -> Post | None:
    """The newest post, or None."""
    return max(history, key=lambda p: _utc(p.at), default=None)


def posts_in_24h(history: list[Post], now: datetime) -> int:
    """Posts with at > now - 24 h. A post timed after now counts too (Meta's clock ahead of the PC's, or the PC
    clock set back): the hard cap never misses a post."""
    now = _utc(now)
    return sum(1 for p in history if now - _DAY < _utc(p.at))


def cap_free_at(cfg: dict, history: list[Post], now: datetime) -> datetime | None:
    """None when fewer than hard_cap_24h posts are in the 24 h before now; else the time the count drops below
    the cap (the oldest counted post that must leave the window: its at + 24 h)."""
    now = _utc(now)
    inside = sorted(_utc(p.at) for p in history if now - _DAY < _utc(p.at))       # later than now counts too
    cap = cfg['hard_cap_24h']
    return inside[len(inside) - cap] + _DAY if len(inside) >= cap else None


def slot_check(cfg: dict, history: list[Post], t: datetime) -> str | None:
    """Why an automatic post at t breaks the rules ('day', 'start', 'window', 'every_days', 'gap', 'same_day',
    'cap', checked in that order), or None when it is allowed. The gap may be short by AUTO_SLACK (one watcher
    run): a post goes live minutes after its run, so an exact gap would push every next post one run later."""
    t = _utc(t)
    lt = local(t)
    if DAYS[lt.weekday()] not in cfg['days']:
        return 'day'
    if cfg['start_date'] and lt.date().isoformat() < cfg['start_date']:
        return 'start'
    if not cfg['post_after'] <= f'{lt:%H:%M}' <= cfg['post_before']:    # the end minute counts as inside
        return 'window'
    last = last_post(history)
    if last:
        if lt.date() < local(last.at).date() + timedelta(days=cfg['every_days']):
            return 'every_days'
        if t - _utc(last.at) < timedelta(hours=cfg['min_gap_hours']) - AUTO_SLACK:
            return 'gap'
    if any(local(p.at).date() == lt.date() for p in history):
        return 'same_day'
    if posts_in_24h(history, t) >= cfg['hard_cap_24h']:
        return 'cap'
    return None


def slot_open(cfg: dict, history: list[Post], now: datetime) -> tuple[bool, str]:
    """(True, Istanbul slot date) when an automatic post is allowed now, else (False, slot_check reason)."""
    why = slot_check(cfg, history, now)
    return (True, local_date(now)) if why is None else (False, why)


def next_auto(cfg: dict, history: list[Post], now: datetime, waiting: int, blocked: str | None = None
              ) -> tuple[datetime | None, str | None, str]:
    """(at, slot, reason). blocked ('invalid', 'off', 'stopped_by_file', 'suspended', 'breaker', 'unresolved')
    is returned as the reason with at = slot = None; waiting == 0 gives 'empty'. Otherwise the earliest
    t >= now that passes slot_check, trying today and the next SEARCH_DAYS days with
    t0 = max(now, window start, last + gap - AUTO_SLACK, cap_free_at): reason 'scheduled', or (None, None,
    'no_slot')."""
    if blocked:
        return None, None, blocked
    if not waiting or waiting < 0:
        return None, None, 'empty'
    now = _utc(now)
    last = last_post(history)
    floor = max(t for t in (now, last and _utc(last.at) + timedelta(hours=cfg['min_gap_hours']) - AUTO_SLACK,
                            cap_free_at(cfg, history, now)) if t)
    today = local(now).date()
    for n in range(SEARCH_DAYS + 1):
        day = (today + timedelta(days=n)).isoformat()
        t0 = max(floor, at_local(day, cfg['post_after']))
        if local_date(t0) == day and slot_check(cfg, history, t0) is None:
            return t0, day, 'scheduled'
    return None, None, 'no_slot'


def forecast(cfg: dict, history: list[Post], now: datetime, keys: list[str], limit_days: int = SEARCH_DAYS
             ) -> list[dict]:
    """Where the waiting keys (queue order) would go if automatic posting were on and every post succeeded at
    its earliest time: [{'key', 'at', 'slot'}], each simulated post added to the history; stops at a slot after
    today + limit_days (Istanbul dates: next_auto's horizon) or when next_auto finds no slot."""
    now = _utc(now)
    last_day = (local(now).date() + timedelta(days=limit_days)).isoformat()
    hist, rows, t = list(history), [], now
    for key in keys:
        at, slot, _ = next_auto(cfg, hist, t, 1)
        if at is None or slot > last_day:
            break
        rows.append({'key': key, 'at': at, 'slot': slot})
        hist.append(Post(at=at, by='auto', key=key))
        t = at
    return rows


def runway(rows: list[dict]) -> str | None:
    """The last forecast row's slot ('YYYY-MM-DD'), or None when rows is empty."""
    return rows[-1]['slot'] if rows else None


def spacing_checks(cfg: dict, history: list[Post], now: datetime) -> list[Check]:
    """Timing checks for a manual post now, in order: 'hard_cap' (block, data {'next_possible': ISO}), 'gap'
    (warn, data {'last': ISO, 'hours': float}), 'same_day' (warn, data {'at': ISO}). Ignores days, window,
    every_days and start_date."""
    now = _utc(now)
    out = []
    n = posts_in_24h(history, now)
    if n >= cfg['hard_cap_24h']:
        free = cap_free_at(cfg, history, now)
        out.append(Check('hard_cap', 'block', f'{n} Reel{"s" if n != 1 else ""} in the last 24 hours: that is the '
                         f'limit. Next possible {fmt(free)}.', {'next_possible': iso(free)}))
    last = last_post(history)
    if last and now - _utc(last.at) < timedelta(hours=cfg['min_gap_hours']):
        ago = max(now - _utc(last.at), timedelta(0))
        h, m = divmod(int(ago.total_seconds() // 60), 60)
        out.append(Check('gap', 'warn', f'The last Reel went out {h} h {m} min ago. The rule is at least '
                         f'{_hours(cfg["min_gap_hours"])} h between Reels.',
                         {'last': iso(last.at), 'hours': round(ago.total_seconds() / 3600, 1)}))
    today = [p for p in history if local_date(p.at) == local_date(now)]
    if today:
        p = last_post(today)
        out.append(Check('same_day', 'warn', f'A Reel already went out today at {local(p.at):%H:%M}.',
                         {'at': iso(p.at)}))
    return out


def summary(cfg: dict, noun: str = 'Reel') -> str:
    """One line for people, e.g. 'One Reel a day after 20:00, at least 20 h apart' or 'One Reel every 2 days
    (Mon, Wed, Fri) between 20:00 and 23:00, at least 20 h apart'. A gap longer than every_days allows says so
    (', so less often than that')."""
    n = cfg['every_days']
    line = f'One {noun} a day' if n == 1 else f'One {noun} every {n} days'
    if set(cfg['days']) != set(DAYS):
        line += ' (' + ', '.join(d.title() for d in DAYS if d in cfg['days']) + ')'
    line += (f' after {cfg["post_after"]}' if cfg['post_before'] == '23:59'
             else f' between {cfg["post_after"]} and {cfg["post_before"]}')
    less = ', so less often than that' if cfg['min_gap_hours'] > 24 * n else ''
    return f'{line}, at least {_hours(cfg["min_gap_hours"])} h apart{less}'


def _hours(x) -> str:
    """20 -> '20', 20.5 -> '20.5'."""
    return f'{x:g}'
