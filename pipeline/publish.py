"""Publishing for every platform: the queue of unposted videos, manual posts from the dashboard, the watcher's
automatic step, the safety rules (one lock, journal + reconcile, breaker, limits) and the audit log.
Storage: WORK/publish.db (SQLite, WAL). The platforms' Drive records stay the truth for "posted".
Imports schedule and tools; platform_<name> lazily in registry(). Never imports stats, easy, dashboard, overview.
Network only through Platform methods marked NETWORK."""
from __future__ import annotations
import hashlib, json, os, re, secrets, sqlite3, threading, time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paths
import schedule as S
import tools
from schedule import Check, Post

WORK = paths.WORK
DB = WORK / 'publish.db'
LOCK = WORK / 'instagram-posting.lock'          # one lock for every platform (v1 name kept)
PLATFORMS = ('instagram', 'youtube')           # UI order; platform_<name>.make() for each
LABELS = {'instagram': 'Instagram', 'youtube': 'YouTube'}     # names shown when a platform module does not load
STAGES = ('preparing', 'sharing', 'processing', 'publishing', 'saving')
KINDS = ('auth', 'permission', 'rate', 'content', 'transient', 'local', 'unknown')
LOCK_BEAT, LOCK_STALE, LOCK_STALE_V1, LOCK_HARD = 30, 300, 1800, 3600       # seconds
SLOT_ATTEMPTS, RETRY_AFTER, SUSPEND_AFTER = 2, 1800, 3
POST_SPACING, ATTEMPTS_24H = 120, 6
RECONCILE_EVERY, TEST_EVERY = 600, 600
COOL_HOURS = (1, 2, 4, 8, 16, 24)

SETTLE_WAIT = 300                               # an upload still FINISHED this soon after the post's last sign may go out
UNKNOWN_EVERY, UNKNOWN_FOR = 1800, timedelta(hours=24)  # 'unknown' jobs: re-checked on their own this often, this long
LOCK_WAIT = 5                                   # seconds a post waits for a lock held without a job (a record write)
CLEAR = ('auth', 'permission', 'rate', 'content')  # error kinds that are a clear "not posted" answer

_DB0, _LOCK0 = DB, LOCK
_JOB_STALE = 300                                # reconcile: a running job silent this long (or queued this long)
_ENDED = 600                                    # current_job(): jobs that ended in the last 10 minutes
_VISIBLE = 125                                  # caption characters shown before "more"
_DAY = timedelta(hours=24)
_ORDER = ('not_set_up', 'breaker', 'unresolved', 'busy', 'posted', 'checking', 'hard_cap', 'file_missing', 'length',
          'caption_empty', 'caption_too_long', 'caption_tags_limit', 'gap', 'same_day', 'duplicate', 'skipped',
          'failed_before',
          'not_next', 'cover_missing', 'caption_tags')                   # spec 5.3 table order
_LEVELS = ('block', 'warn', 'info')
_OPS = ('top', 'untop', 'reset_order', 'skip', 'unskip', 'caption', 'clear_caption', 'clear_failed', 'mark_posted')
_PERMALINK = re.compile(r'^https://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_-]+/?$')
_SHORTCODE = re.compile(r'instagram\.com/(?:reel|p)/([A-Za-z0-9_-]+)')
_OPEN, _UNRESOLVED = ('queued', 'running'), ('interrupted', 'unknown')
_REG = threading.Lock()
_registry: dict | None = None                   # set_registry() platforms
_real: dict | None = None                       # the real platforms, made once
_said: set = set()                              # platform modules whose import error was printed
_threads: dict = {}                             # job id -> the daemon thread running it (tests join it)


@dataclass(frozen=True)
class Item:
    """One video that can be posted on one platform."""
    platform: str
    key: str                  # '<lesson folder>/<output folder>/<file>'
    id: str                   # item_id(key)
    lesson: str               # lesson folder name
    out: Path                 # output folder (the lesson folder is out.parent)
    file: Path                # the video
    part: int | None          # N of -short-N; None for a whole one-clip lesson video
    title: str                # without hashtags; the file name when there is none
    seconds: int | None       # rounded length from probe_cached; None when unknown
    rank: tuple               # (earliest mtime of out/*.mp4, part or 0, file name)
    entry: dict               # platform data for the caption (Instagram: the youtube.json short entry)
    posted: dict | None       # the Drive record when it has an 'id'
    size: int | None = None   # the video's size in bytes (None when it cannot be read)

    @property
    def sig(self) -> str:
        """'<file name>:<size>': the same video, wherever its lesson folder is now (rename, copy). Queue rows and
        jobs store it; a row or job counts for an item only when its sig matches (a new video at an old path
        gets nothing from the old one)."""
        return f'{self.file.name}:{"?" if self.size is None else self.size}'


def item_id(key: str) -> str:
    """sha1(key, UTF-8): the first 12 hex characters."""
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]


class Platform:
    """One place to post to. Each platform is one small module platform_<name>.py with a subclass and make(),
    named in PLATFORMS. Methods marked NETWORK call the platform's API; all others are local (disk and Drive)."""
    name: str = ''                 # 'instagram'
    label: str = ''                # 'Instagram'
    noun: str = 'post'             # 'Reel'
    verb: str = 'post'             # what the page's button says: 'post' (Instagram), 'upload' (YouTube)
    setup_hint: str = ''           # the command that sets the platform up on the PC ('instagram.py system-token')
    link_re = None                 # a valid link to one post (mark_posted); None: an Instagram link
    link_hint: str = ''            # the message for a wrong link
    breaker: str = ''              # shared by one company's platforms: 'meta'
    caption_max: int = 2200        # UTF-16 code units
    tags_max: int = 30             # platform limit (block)
    tags_suggested: int = 5        # channel rule (info)
    account_url: str = ''          # the account's page for GET /api/publish ('' when unknown; not in spec 7.2)

    def set_up(self) -> bool:
        """True when this PC has the platform's key (local check)."""
        return False

    def candidates(self, root: Path) -> tuple[list[Item], list[dict]]:
        """Every postable video of finished lessons, posted ones included, sorted by Item.rank, plus
        [{'lesson', 'error'}] for lessons that could not be read. Never raises for one bad lesson."""
        return [], []

    def default_caption(self, item: Item, root: Path) -> str:
        """The caption the pipeline makes for this item."""
        return ''

    def cover(self, item: Item) -> Path | None:
        """The item's cover file when it exists, else None. Never raises."""
        return None

    def prepare(self, item: Item, log) -> None:
        """Make the lesson's missing covers (slow, local). May raise OSError or RuntimeError."""

    def preflight(self, item: Item, caption: str, fresh: bool = False) -> list[Check]:
        """Local checks for this item and caption: file_missing, length, caption_empty, caption_too_long,
        caption_tags_limit (block); cover_missing, caption_tags (info). Never 'warn'.
        fresh=True probes the video again (right before posting) instead of using probe_cached.
        (This base version is shared: length 3-900 s, the 5.3 caption checks, the cover through self.cover.)"""
        out = []
        if not item.file.is_file():
            out.append(Check('file_missing', 'block', 'The video file is missing on Drive.'))
        else:
            try:
                seconds = float((tools.probe(item.file) if fresh else probe_cached(item.file))['duration'])
            except Exception:
                out.append(Check('length', 'block', 'The video could not be read.'))
            else:
                if not 3 <= seconds <= 900:
                    out.append(Check('length', 'block', f'The video is {round(seconds)} s; {self.noun}s must be '
                                     '3 s to 15 min.', {'seconds': round(seconds)}))
        out += _caption_checks(self, caption)
        if self.cover(item) is None:
            out.append(Check('cover_missing', 'info', 'No cover yet: the PC makes one first (about a minute).'))
        return out

    def history(self, root: Path, problems: list | None = None) -> list[Post]:
        """Every post in the platform's Drive records (keys starting with '_' skipped); by = the record's
        'by', or 'old' when it has none. A record file that cannot be read raises OSError, unless problems is a
        list: then it gets {'lesson', 'error'} and the other files still count (page bodies only)."""
        return []

    def key_warning(self, now: datetime) -> str | None:
        """A warning when the platform's key ends within 14 days (local check), else None. Never raises."""
        return None

    def write_record(self, item: Item, record: dict, retry: bool = True) -> None:
        """Set the item's record in its Drive record file atomically, keeping every other key and the old
        record's extra keys. The caller holds the PostLock. Raises OSError when Drive stays unwritable after
        retries at 1, 2, 4, 8 and 16 s (retry=False: one try, for the background rewrites of pending records),
        and when the record file cannot be read (a Drive that is away is never taken for 'no record yet')."""
        raise NotImplementedError(f'{self.name}: write_record')

    def post(self, item: Item, caption: str, journal: 'Journal') -> dict:
        """NETWORK. Post the item, calling in order: journal.stage('sharing'); journal.container(id) as soon as
        the platform accepts the upload; journal.stage('processing'); journal.stage('publishing') right before
        the publish call; journal.published(id) the moment the post is live. journal.stage() raises Stopped
        when the job must not go on: let it through, sending nothing more. Returns {'id', 'permalink' (may be
        ''), 'posted', 'posted_utc'}. Raises on failure; never retries."""
        raise NotImplementedError(f'{self.name}: post')

    def container_status(self, container_id: str) -> str:
        """NETWORK. 'IN_PROGRESS' | 'FINISHED' | 'PUBLISHED' | 'ERROR' | 'EXPIRED'."""
        raise NotImplementedError(f'{self.name}: container_status')

    def recent_media(self, limit: int = 10) -> list[dict]:
        """NETWORK. Newest first: [{'id', 'at' (ISO UTC), 'permalink', 'caption', 'product'}]."""
        raise NotImplementedError(f'{self.name}: recent_media')

    def test(self) -> dict:
        """NETWORK, read-only, 2 calls: {'account', 'followers', 'media', 'limit': {'used', 'total'}}."""
        raise NotImplementedError(f'{self.name}: test')

    def read_stats(self) -> dict:
        """NETWORK, 1 call: {'followers', 'follows', 'media_count', 'media': [{'id', 'at', 'product', 'likes',
        'comments', 'permalink', 'caption'}]} for the 50 newest media."""
        raise NotImplementedError(f'{self.name}: read_stats')

    def read_limit(self) -> dict:
        """NETWORK. {'used', 'total'} for the rolling 24 h API publishing limit."""
        raise NotImplementedError(f'{self.name}: read_limit')

    def read_permissions(self) -> list[str]:
        """NETWORK. The key's granted permission names."""
        raise NotImplementedError(f'{self.name}: read_permissions')

    def classify(self, exc: BaseException) -> str:
        """One of KINDS for an exception raised by post(), container_status(), recent_media() or test()."""
        return 'local' if isinstance(exc, OSError) else 'unknown'


class Journal:
    """A running job's progress in publish.db, committed the moment each step happens, so a crash never loses
    a container id or a media id. Thread-safe; each call opens its own connection."""
    container_id: str | None
    media_id: str | None

    def __init__(self, job_id: str, platform_name: str, lock: 'PostLock | None' = None, allowed=None):
        self.job_id, self.platform_name = job_id, platform_name
        self.container_id = self.media_id = None
        self.url = ''
        self.lock, self.allowed = lock, allowed          # allowed(): None, or why the job must stop now
        self._mutex = threading.Lock()

    def stage(self, name: str) -> None:
        """Record the stage (one of STAGES) and a heartbeat. First checks that the job may go on: still
        'running', the PostLock still ours, and allowed() (automatic jobs: automatic posting still on). Raises
        Stopped otherwise, so nothing more is sent (the platform calls stage('publishing') right before
        publishing)."""
        if name not in STAGES:
            raise ValueError(f'unknown stage {name!r}')
        self.go_on()
        self._set(stage=name, result=_result(name, self.platform_name))

    def go_on(self) -> None:
        """Raise Stopped when this job must not send anything more."""
        try:
            state = _job_row(self.job_id)['state']
        except PublishError:
            state = None
        if state != 'running':
            raise Stopped('the job was settled by another process meanwhile')
        if self.lock is not None and not self.lock.mine():
            raise Stopped('another process took over the posting lock')
        why = self.allowed() if self.allowed else None
        if why:
            raise Stopped(why)

    def checked(self, status: str) -> None:
        """Record the upload status the platform just read (a 'PUBLISHED' upload is never called not posted)."""
        self._set(last_status=str(status), checked=S.iso(_utcnow()))

    def container(self, container_id: str) -> None:
        """Record the upload container id, before waiting for it."""
        self.container_id = str(container_id)
        self._set(container_id=self.container_id)

    def published(self, media_id: str) -> None:
        """Record that the post is live (media id and time). From here on the item counts as posted."""
        self.media_id = str(media_id)
        self._set(media_id=self.media_id, published=S.iso(_utcnow()))

    def permalink(self, url: str) -> None:
        """Record the post's link."""
        self.url = str(url or '')
        self._set(permalink=self.url or None)

    def log(self, line: str) -> None:
        """Write one progress line to WORK/<platform>.log (via log_line), prefixed with the job id."""
        log_line(self.platform_name, f'{self.job_id} {line}')

    def _set(self, **cols):
        cols['heartbeat'] = S.iso(_utcnow())
        with self._mutex:
            _update_job(self.job_id, **cols)


class PublishError(Exception):
    """A refusal with an HTTP meaning. dashboard.py answers .status with
    {'error': .code, 'message': .message, **.extra}."""
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


class Stopped(RuntimeError):
    """Raised by Journal.stage(): the job must stop before it sends more (the lock or the job was taken over,
    or automatic posting was turned off). Nothing more is sent; the job ends 'stale'."""


class Busy(PublishError):
    """The PostLock is held by someone else (409 'busy')."""
    def __init__(self, holder: dict | None = None):
        self.holder = holder
        super().__init__(409, 'busy', 'Another post is going out right now.', job=_job_or_none((holder or {}).get('job')))


# ---------- time, database, text ----------

def _utcnow() -> datetime:
    """Now (aware UTC). The functions without a now= parameter use it; tests patch it."""
    return datetime.now(timezone.utc)


def _now(now: datetime | None = None) -> datetime:
    return _utc(now) if now is not None else _utcnow()


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _time(s) -> datetime | None:
    """An ISO time (DB, record or platform) as aware UTC; None when missing or unreadable."""
    if not s:
        return None
    try:
        return _utc(datetime.fromisoformat(str(s).replace('Z', '+00:00')))
    except ValueError:
        return None


def _db() -> Path:
    """DB, or WORK/publish.db when only WORK was moved (tests), as schedule._path() does."""
    return DB if DB != _DB0 else WORK / DB.name


def _lockfile() -> Path:
    return LOCK if LOCK != _LOCK0 else WORK / LOCK.name


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, who TEXT NOT NULL,
  kind TEXT NOT NULL, platform TEXT, item_key TEXT, job TEXT, text TEXT NOT NULL, data TEXT) STRICT;
CREATE INDEX IF NOT EXISTS audit_kind ON audit(kind, id);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, platform TEXT NOT NULL, item_key TEXT NOT NULL, title TEXT NOT NULL,
  by TEXT NOT NULL CHECK (by IN ('auto','manual','cli')), who TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued','running','done','failed','stale','interrupted','unknown')),
  stage TEXT CHECK (stage IS NULL OR stage IN ('preparing','sharing','processing','publishing','saving')),
  slot TEXT, created TEXT NOT NULL, started TEXT, heartbeat TEXT, finished TEXT,
  container_id TEXT, media_id TEXT, published TEXT, permalink TEXT,
  record_pending INTEGER NOT NULL DEFAULT 0 CHECK (record_pending IN (0,1)), record TEXT,
  caption_sha TEXT, caption_head TEXT, fingerprint TEXT, override TEXT,
  kind TEXT CHECK (kind IS NULL OR kind IN ('auth','permission','rate','content','transient','local','unknown')),
  error TEXT, result TEXT, reconcile_at TEXT, sig TEXT, last_status TEXT, checked TEXT) STRICT;
CREATE INDEX IF NOT EXISTS jobs_open ON jobs(platform, state);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(platform, created);
CREATE TABLE IF NOT EXISTS slots (platform TEXT NOT NULL, slot TEXT NOT NULL, attempts INTEGER NOT NULL,
  last_at TEXT NOT NULL, last_kind TEXT, PRIMARY KEY (platform, slot)) STRICT;
CREATE TABLE IF NOT EXISTS auto_state (platform TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
  suspended TEXT, suspended_why TEXT, missed_checked TEXT) STRICT;
CREATE TABLE IF NOT EXISTS breakers (name TEXT PRIMARY KEY, state TEXT NOT NULL CHECK (state IN ('closed','cooling','stopped')),
  until TEXT, step INTEGER NOT NULL DEFAULT 0, kind TEXT, message TEXT, since TEXT) STRICT;
CREATE TABLE IF NOT EXISTS queue (platform TEXT NOT NULL, item_key TEXT NOT NULL, top INTEGER,
  skip INTEGER NOT NULL DEFAULT 0 CHECK (skip IN (0,1)), caption TEXT, failed TEXT, failed_at TEXT, updated TEXT NOT NULL,
  sig TEXT, unread TEXT, PRIMARY KEY (platform, item_key)) STRICT;
CREATE TABLE IF NOT EXISTS queue_meta (platform TEXT PRIMARY KEY, rev INTEGER NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS media (platform TEXT NOT NULL, id TEXT NOT NULL, at TEXT NOT NULL, permalink TEXT, caption_head TEXT,
  product TEXT, likes INTEGER, comments INTEGER, seen TEXT NOT NULL, PRIMARY KEY (platform, id)) STRICT;
CREATE TABLE IF NOT EXISTS probes (key TEXT PRIMARY KEY, duration REAL NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS stamps (k TEXT PRIMARY KEY, at TEXT NOT NULL) STRICT;
INSERT OR IGNORE INTO meta (k, v) VALUES ('schema', '2');
"""
_ADDED = {'jobs': ('sig', 'last_status', 'checked'), 'queue': ('sig', 'unread')}      # schema 2 (fix round)


def connect() -> sqlite3.Connection:
    """A new connection to publish.db, creating the schema (spec 6.1) on first use. WAL, busy_timeout 10 s,
    autocommit (writers use BEGIN IMMEDIATE), rows as sqlite3.Row. One connection per call or thread; close it."""
    path = _db()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA busy_timeout = 10000')
        con.execute('PRAGMA journal_mode = WAL')
        con.execute('PRAGMA synchronous = NORMAL')
        con.execute('PRAGMA foreign_keys = ON')
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stamps'").fetchone():
            con.executescript(_SCHEMA)          # the file is new (or was emptied): every statement is idempotent
        if con.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()['v'] != '2':
            _upgrade(con)
        _keep(path)
    except BaseException:
        con.close()
        raise
    return con


_KEEP = threading.Lock()
_keeper: tuple | None = None                    # (path, connection): see _keep()


def _keep(path: Path) -> None:
    """Hold one idle connection to publish.db for the life of the process (G23): while it is open, closing a
    per-call connection does not checkpoint and delete the WAL, which costs ~0.1-1 s per write on the D: hard
    disk. It runs one read when opened (only then does SQLite count it) and never holds a transaction."""
    global _keeper
    with _KEEP:
        if _keeper is not None and _keeper[0] == path:
            return
        _unkeep_locked()
        k = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        try:
            k.execute("SELECT v FROM meta WHERE k = 'schema'").fetchall()
        except BaseException:
            k.close()
            raise
        _keeper = (path, k)


def _unkeep_locked() -> None:
    global _keeper
    if _keeper is not None:
        try:
            _keeper[1].close()
        except Exception:
            pass
        _keeper = None


def _upgrade(con) -> None:
    """Schema 1 -> 2: the columns of _ADDED (a publish.db made before the fix round, e.g. a --dev one)."""
    con.execute('BEGIN IMMEDIATE')
    try:
        for table, cols in _ADDED.items():
            have = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
            for c in cols:
                if c not in have:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {c} TEXT')
        con.execute("UPDATE meta SET v = '2' WHERE k = 'schema'")
        con.execute('COMMIT')
    except BaseException:
        con.execute('ROLLBACK')
        raise


class _Con:
    """with _Con() as con: one connection, always closed. write=True wraps the block in BEGIN IMMEDIATE and
    COMMIT (ROLLBACK when the block raises)."""
    def __init__(self, write: bool = False):
        self.write = write

    def __enter__(self) -> sqlite3.Connection:
        self.con = connect()
        if self.write:
            try:
                self.con.execute('BEGIN IMMEDIATE')
            except BaseException:
                self.con.close()
                raise
        return self.con

    def __exit__(self, et, *_):
        try:
            if self.write and self.con.in_transaction:
                self.con.execute('ROLLBACK' if et else 'COMMIT')
        finally:
            self.con.close()


_NAMES = r'(?:access_token|refresh_token|page_token|client_secret|password|secret|token|key)'
_SECRETS = (
    (re.compile(r'(?i)((?<![A-Za-z0-9_-])(?:authorization|set-cookie|cookie)["\']?\s*[:=]\s*["\']?)[^"\'\r\n]+'),
     r'\1[hidden]'),
    (re.compile(r'(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+'), r'\1[hidden]'),
    (re.compile(r'eyJ[A-Za-z0-9_=-]+(?:\.[A-Za-z0-9_=-]+)*'), '[hidden]'),                 # JWT
    (re.compile(r'\bEAA[A-Za-z0-9]{6,}'), '[hidden]'),                                   # Meta
    (re.compile(r'\bya29\.[A-Za-z0-9._-]+'), '[hidden]'),                                # Google access
    (re.compile(r'\b1//[A-Za-z0-9._-]{6,}'), '[hidden]'),                                # Google refresh
    (re.compile(r'sk-ant-[A-Za-z0-9_-]+'), '[hidden]'),                                  # Anthropic
    (re.compile(r'(?i)(["\']' + _NAMES + r'["\']\s*:\s*")(?:\\.|[^"\\])*(")'), r'\1[hidden]\2'),
    (re.compile(r"(?i)([\"']" + _NAMES + r"[\"']\s*:\s*')(?:\\.|[^'\\])*(')"), r'\1[hidden]\2'),
    (re.compile(r'(?i)((?<![A-Za-z0-9_])' + _NAMES + r'\s*=\s*)[^\s&,;"\'<>]+'), r'\1[hidden]'),
    (re.compile(r'(?<![A-Za-z0-9_-])(?![a-z0-9]+(?:-[a-z0-9]+)+(?![A-Za-z0-9_-]))[A-Za-z0-9_-]{40,}'), '[hidden]'),
)                                   # the last one keeps lower-case file-name slugs ('trennbare-und-...-short-1')


def redact(text: str) -> str:
    """text with every secret replaced by '[hidden]': Bearer values, JWTs (eyJ...), Meta tokens (EAA...),
    Google tokens (ya29., 1//...), 'sk-ant-' keys, key=value and "key": "value" pairs named token, access_token,
    refresh_token, page_token, client_secret, secret, password or key, and runs of 40 or more [A-Za-z0-9_-]
    characters (except lower-case words joined by single hyphens: the pipeline's file names). Applied before
    anything is stored, logged or shown."""
    text = '' if text is None else str(text)
    for pattern, hidden in _SECRETS:
        text = pattern.sub(hidden, text)
    return text


def log_line(platform_name: str, text: str) -> None:
    """Append 'YYYY-MM-DD HH:MM <text>' (Istanbul time, redacted) to WORK/<platform>.log, and print it."""
    line = f'{S.local(_utcnow()):%Y-%m-%d %H:%M} {redact(text)}'
    try:
        with open(WORK / f'{platform_name}.log', 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:                                    # a console that cannot show the text
        pass


def audit(who: str, kind: str, text: str, *, platform: str | None = None, item_key: str | None = None,
          job: str | None = None, data: dict | None = None) -> int:
    """Add one audit row and return its id. who: an email, 'watcher', 'cli', 'migration' or 'dashboard'.
    text is one plain sentence for the Activity tab; data is redacted JSON."""
    with _Con(write=True) as con:
        return _audit(con, who, kind, text, platform=platform, item_key=item_key, job=job, data=data)


def _audit(con, who, kind, text, *, platform=None, item_key=None, job=None, data=None) -> int:
    """audit() inside the caller's transaction, so the change and its row commit together."""
    blob = None if data is None else redact(json.dumps(data, ensure_ascii=False, default=str, sort_keys=True))
    cur = con.execute('INSERT INTO audit (at, who, kind, platform, item_key, job, text, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                      (S.iso(_utcnow()), str(who), kind, platform, item_key, job, redact(text), blob))
    return cur.lastrowid


_FILTERS = {
    'all': '1',
    'posts': "(kind LIKE 'post.%' OR kind LIKE 'record.%' OR kind = 'queue.mark_posted')",
    'settings': "(kind LIKE 'settings.%' OR kind IN ('auto.on', 'auto.off', 'auto.resumed'))",
    'problems': "(kind IN ('post.failed', 'post.unknown', 'post.interrupted', 'auto.suspended', 'auto.missed', "
                "'item.failed') OR kind LIKE 'breaker.%')",
}


def activity(kind: str = 'all', before: int | None = None, limit: int = 50) -> dict:
    """The GET /api/activity body without 'logs': {'items': [newest first], 'more': bool}.
    kind: 'all'; 'posts' (post.*, record.*, queue.mark_posted); 'settings' (settings.*, auto.on, auto.off,
    auto.resumed); 'problems' (post.failed, post.unknown, post.interrupted, breaker.*, auto.suspended,
    auto.missed, item.failed). Raises PublishError(400, 'bad_request') for an unknown kind."""
    if kind not in _FILTERS or (before is not None and type(before) is not int) or type(limit) is not int:
        raise PublishError(400, 'bad_request', 'Unknown activity filter.')
    limit = max(1, min(limit, 200))
    sql = f'SELECT * FROM audit WHERE {_FILTERS[kind]}' + (' AND id < ?' if before is not None else '')
    with _Con() as con:
        rows = con.execute(sql + ' ORDER BY id DESC LIMIT ?', [*([before] if before is not None else []), limit + 1]).fetchall()
    items = [{'id': r['id'], 'at': r['at'], 'who': r['who'], 'kind': r['kind'], 'platform': r['platform'],
              'text': r['text'], 'url': _url_of(r['data'])} for r in rows[:limit]]
    return {'items': items, 'more': len(rows) > limit}


def _url_of(data) -> str | None:
    try:
        d = json.loads(data) if data else {}
    except ValueError:
        return None
    return d.get('url') or None if isinstance(d, dict) else None


def _head(text) -> str:
    """The caption head used to match a post: the first 60 characters, whitespace collapsed."""
    return ' '.join(str(text or '').split())[:60]


def _sha(text) -> str:
    return hashlib.sha1(str(text or '').encode('utf-8')).hexdigest()[:12]


def _utf16(text) -> int:
    return len(str(text or '').encode('utf-16-le')) // 2


def _caption_checks(p: Platform, caption: str) -> list[Check]:
    """The spec 5.3 caption checks (blocks; caption_tags is info). Never 'warn'."""
    caption = caption if isinstance(caption, str) else ''
    out = []
    if not caption.strip():
        out.append(Check('caption_empty', 'block', 'The caption is empty.'))
    n = _utf16(caption)
    if n > p.caption_max:
        out.append(Check('caption_too_long', 'block', f'The caption has {n:,} characters; the limit is '
                         f'{p.caption_max:,}.', {'chars': n}))
    tags = len(re.findall(r'#\w+', caption))
    if tags > p.tags_max:
        out.append(Check('caption_tags_limit', 'block', f'{tags} hashtags; {p.label} allows {p.tags_max}.', {'tags': tags}))
    elif tags > p.tags_suggested:
        out.append(Check('caption_tags', 'info', f'{tags} hashtags; the channel uses at most {p.tags_suggested}.',
                         {'tags': tags}))
    return out


def _result(stage: str, platform_name: str) -> str:
    """The Job 'result' line for a stage or state."""
    label = _label(platform_name)
    return {'queued': 'Waiting to start…', 'preparing': 'Getting the video ready…',
            'sharing': f'Sending the video to {label}…', 'processing': f'{label} is processing the video…',
            'publishing': f'Publishing on {label}…', 'saving': 'Saving the record…'}.get(stage, '')


def _label(platform_name: str) -> str:
    try:
        return registry()[platform_name].label or LABELS.get(platform_name, platform_name.title())
    except Exception:
        return LABELS.get(platform_name, str(platform_name).title())


# ---------- platforms ----------

def registry() -> dict[str, Platform]:
    """{name: Platform} in PLATFORMS order. Imports each platform_<name> lazily and calls its make(). A module
    that fails to import is left out, and its error is printed once. set_registry() overrides this."""
    global _real
    with _REG:
        if _registry is not None:
            return dict(_registry)
        if _real is None:
            found = {}
            for name in PLATFORMS:
                try:
                    found[name] = __import__(f'platform_{name}').make()
                except Exception as e:
                    if name not in _said:
                        _said.add(name)
                        print(f'publish: platform_{name} is not available ({type(e).__name__}: {redact(str(e))})')
            _real = found
        return dict(_real)


def set_registry(platforms: dict[str, Platform] | None) -> None:
    """Use these platforms instead of the real ones (tests, dashboard --dev). None goes back to the real ones.
    Also forgets the probe memo and closes the idle publish.db connection (tests empty WORK right after)."""
    global _registry, _real
    with _REG:
        _registry = None if platforms is None else dict(platforms)
        _real = None
    with _KEEP:
        _unkeep_locked()
    with _PROBE_MX:
        _probes.clear()
        _probe_fail.clear()
        _probe_locks.clear()
        _v1.clear()


def platform(name: str) -> Platform:
    """The platform called name. Raises PublishError(404, 'unknown_platform')."""
    p = registry().get(name) if isinstance(name, str) else None
    if p is None:
        raise PublishError(404, 'unknown_platform', 'Unknown platform.')
    return p


# ---------- settings ----------

_ROOT: Path | None = None            # the last Drive root seen (for the bodies whose call has no root)


def settings(root: Path | None = None) -> S.Loaded:
    """schedule.load_settings(root), plus an audit row 'settings.migrated' (who 'migration') when this call
    created the file."""
    loaded = S.load_settings(root)
    if loaded.migrated:
        plats = loaded.doc.get('platforms', {})
        on = any(isinstance(v, dict) and v.get('auto') for v in plats.values())
        text = ('Settings created: automatic posting on (there was no pause flag).' if on else
                'Settings created from the old pause flag: automatic posting off.')
        audit('migration', 'settings.migrated', text, platform=PLATFORMS[0] if len(PLATFORMS) == 1 else None,
              data={'platforms': plats})
    return loaded


def save_settings(platform_name: str, patch: dict, rev: int, ack: list[str], by: str, *,
                  dry_run: bool = False, root: Path | None = None) -> dict:
    """The POST /api/settings body. Saves through schedule.save_settings, which also writes or deletes the
    pause file. Audits 'settings.saved', plus 'auto.on' or 'auto.off' when the effective state changes.
    Returns {'ok', 'settings', 'rev', 'auto', 'forecast', 'message'}; with dry_run, {'ok', 'dry_run': true,
    'settings', 'forecast', 'needs_ack'} and nothing is written.
    Raises PublishError: 404 unknown_platform; 409 stale (with 'settings', 'rev') or invalid_file;
    422 invalid or read_only (with 'fields'); 428 confirm (with 'needs_ack', 'forecast')."""
    p = platform(platform_name)
    root = Path(root) if root else _ROOT
    before = S.effective_auto(settings(root), p.name)[0]
    try:
        doc = S.save_settings(p.name, patch, rev, by=by, ack=list(ack or ()), dry_run=dry_run)
    except KeyError:
        raise PublishError(404, 'unknown_platform', 'Unknown platform.') from None
    except S.StaleSettings as e:
        raise PublishError(409, 'stale', 'Someone else changed the settings. Here they are now.',
                           settings=e.doc['platforms'][p.name], rev=e.doc['rev']) from None
    except S.InvalidFile:
        raise PublishError(409, 'invalid_file', 'The settings file has a problem. Reset it first.') from None
    except S.SettingsError as e:
        text = 'The 24-hour limit can only be changed on the PC.' if e.code == 'read_only' else 'Check the marked fields.'
        raise PublishError(422, e.code, text, fields=e.fields) from None
    except S.NeedsAck as e:
        cfg = e.doc['platforms'][p.name]
        fc = _forecast(root, p, cfg, assumes_on=not cfg['auto'])
        if dry_run:
            return {'ok': True, 'dry_run': True, 'settings': cfg, 'forecast': fc, 'needs_ack': e.acks}
        text = ('Turning automatic posting on needs your confirmation.' if 'auto_on' in e.acks
                else 'A gap under 20 hours needs your confirmation.')
        raise PublishError(428, 'confirm', text, needs_ack=e.acks, forecast=fc) from None
    except OSError:
        raise PublishError(503, 'write_failed', 'The settings could not be saved. Try again.') from None
    cfg = doc['platforms'][p.name]
    if dry_run:
        return {'ok': True, 'dry_run': True, 'settings': cfg,
                'forecast': _forecast(root, p, cfg, assumes_on=not cfg['auto']), 'needs_ack': []}
    fields = ', '.join(sorted(patch)) or 'nothing'
    audit(by, 'settings.saved', f'{p.label} settings saved ({fields}).', platform=p.name,
          data={'patch': patch, 'rev': doc['rev']})
    loaded = settings(root)
    after = S.effective_auto(loaded, p.name)[0]
    if after != before:
        audit(by, 'auto.on' if after else 'auto.off', f'{p.label}: automatic posting turned {"on" if after else "off"}.',
              platform=p.name)
    auto = _auto(root, p, _utcnow(), loaded)
    if auto['effective'] and auto['next'] and auto['next']['at']:
        message = f'Saved. Automatic posting is on: next {p.noun} {_after(_time(auto["next"]["at"]))}.'
    else:
        message = f'Saved. Automatic posting is {"on" if auto["effective"] else "off"}.'
    return {'ok': True, 'settings': cfg, 'rev': doc['rev'], 'auto': auto,
            'forecast': _forecast(root, p, cfg, assumes_on=not _will_post(auto)), 'message': message}


def _will_post(auto: dict) -> bool:
    """The watcher really posts with these settings: automatic posting on, and not suspended, stopped by the
    breaker or waiting for an interrupted post (F35: else the forecast says 'assumes_on')."""
    return auto['effective'] and auto['reason'] not in ('suspended', 'breaker', 'unresolved')


def reset_settings(ack: list[str], by: str) -> dict:
    """The POST /api/settings/reset body: defaults for every platform, automatic posting off, pause files
    written. Raises PublishError(428, 'confirm') without ack 'reset_settings'. Audits 'settings.reset'."""
    try:
        doc = S.reset_settings(by=by, ack=list(ack or ()))
    except S.NeedsAck as e:
        raise PublishError(428, 'confirm', 'Resetting the settings needs your confirmation.', needs_ack=e.acks) from None
    except OSError:
        raise PublishError(503, 'write_failed', 'The settings could not be saved. Try again.') from None
    audit(by, 'settings.reset', 'Settings reset to defaults: automatic posting off.', data={'rev': doc['rev']})
    return {'ok': True, 'message': 'Settings reset to defaults. Automatic posting is off.',
            'settings': doc['platforms'].get(PLATFORMS[0]), 'rev': doc['rev']}


def resume_auto(platform_name: str, ack: list[str], by: str, *, root: Path | None = None) -> dict:
    """The POST /api/auto/resume body: clears a suspension and the failure count. root: the Drive root (else
    the last one seen; after a restart there is none yet, G29). Raises PublishError: 409 not_suspended;
    428 confirm without ack 'resume_auto'. Audits 'auto.resumed'."""
    p = platform(platform_name)
    with _Con(write=True) as con:
        row = con.execute('SELECT suspended FROM auto_state WHERE platform = ?', (p.name,)).fetchone()
        if not row or not row['suspended']:
            raise PublishError(409, 'not_suspended', 'Automatic posting is not suspended.')
        if not isinstance(ack, (list, tuple, set)) or 'resume_auto' not in ack:
            raise PublishError(428, 'confirm', 'Resuming automatic posting needs your confirmation.',
                               needs_ack=['resume_auto'])
        con.execute('UPDATE auto_state SET suspended = NULL, suspended_why = NULL, failures = 0 WHERE platform = ?',
                    (p.name,))
        _audit(con, by, 'auto.resumed', f'{p.label}: automatic posting resumed.', platform=p.name)
    root = Path(root) if root else _ROOT
    return {'ok': True, 'message': 'Automatic posting resumed.', 'auto': _auto(root, p, _utcnow())}


# ---------- items, history, captions ----------

PROBE_RETRY = 300                               # s: a file ffprobe failed on is not probed again sooner
_PROBE_MX = threading.Lock()
_probes: dict = {}                              # (db path, key) -> probe: this process's copy of the probes table
_probe_fail: dict = {}                          # (db path, key) -> (monotonic time, the error)
_probe_locks: dict = {}                         # (db path, key) -> Lock: one probe of a file at a time
_v1: dict = {}                                  # db path -> v1's dashboard-probes.json ({} when none)


def probe_cached(path: Path) -> dict:
    """tools.probe(path) reduced to {'duration', 'width', 'height'}, cached in publish.db under
    '<name>:<size>'. Calls tools.probe through the module, so tests can patch it.
    Kept in memory too (no database read per file and scan, G17). Two callers never probe the same file at
    once (the second waits and gets the first's answer), and a file ffprobe fails on raises the same error
    for PROBE_RETRY s without a new probe (G24). A file v1's dashboard-probes.json knows (same name and size)
    is not probed again after the deploy (G23): its rounded length, and 1080x1920 or 1920x1080 for its
    'vertical'."""
    path = Path(path)
    key = f'{path.name}:{path.stat().st_size}'
    db = str(_db())
    with _PROBE_MX:
        hit = _probes.get((db, key))
        if hit:
            return dict(hit)
        lock = _probe_locks.setdefault((db, key), threading.Lock())
    with lock:
        with _PROBE_MX:
            hit, fail = _probes.get((db, key)), _probe_fail.get((db, key))
        if hit:
            return dict(hit)
        if fail and time.monotonic() - fail[0] < PROBE_RETRY:
            raise fail[1]
        with _Con() as con:
            row = con.execute('SELECT duration, width, height FROM probes WHERE key = ?', (key,)).fetchone()
        if row:
            out = {'duration': row['duration'], 'width': row['width'], 'height': row['height']}
        else:
            out = _v1_probe(db, key)
            if out is None:
                try:
                    info = tools.probe(path)
                    out = {'duration': float(info['duration']), 'width': int(info['width']), 'height': int(info['height'])}
                except Exception as e:
                    with _PROBE_MX:
                        _probe_fail[(db, key)] = (time.monotonic(), e)
                    raise
            with _Con() as con:
                con.execute('INSERT OR REPLACE INTO probes (key, duration, width, height, at) VALUES (?, ?, ?, ?, ?)',
                            (key, out['duration'], out['width'], out['height'], S.iso(_utcnow())))
        with _PROBE_MX:
            _probes[(db, key)] = dict(out)
            _probe_fail.pop((db, key), None)
    return dict(out)


def _v1_probe(db: str, key: str) -> dict | None:
    """The probe v1's dashboard (WORK/dashboard-probes.json, read once) kept for key, else None."""
    with _PROBE_MX:
        if db not in _v1:
            try:
                data = json.loads((WORK / 'dashboard-probes.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                data = {}
            _v1[db] = data if isinstance(data, dict) else {}
        old = _v1[db].get(key)
    if not (isinstance(old, dict) and type(old.get('seconds')) in (int, float) and old['seconds'] > 0
            and isinstance(old.get('vertical'), bool)):
        return None
    w, h = (1080, 1920) if old['vertical'] else (1920, 1080)
    return {'duration': float(old['seconds']), 'width': w, 'height': h}


def items(root: Path, platform_name: str) -> tuple[list[Item], list[dict]]:
    """platform.candidates(root): every postable video in natural order, plus the lessons that failed to read."""
    return platform(platform_name).candidates(Path(root))


def find(root: Path, platform_name: str, item_id: str) -> Item:
    """The candidate with this id. Raises PublishError(404, 'unknown_item'). An id is never used as a path."""
    p = platform(platform_name)
    if isinstance(item_id, str):
        for it in p.candidates(Path(root))[0]:
            if it.id == item_id:
                return it
    raise PublishError(404, 'unknown_item', f'This {p.noun} is not in the list any more.')


def history(root: Path, platform_name: str) -> list[Post]:
    """Every post that counts for the timing rules, oldest first:
    - the Drive records;
    - jobs with a media id whose record is not written yet (at = published);
    - jobs in state 'unknown' (at = started);
    - media in the cache that match no record or job id (by 'outside').
    Double counting is allowed: it only makes the rules stricter."""
    return _View(root, platform(platform_name), _utcnow()).hist


def caption_for(root: Path, item: Item) -> tuple[str, bool]:
    """(caption, custom): the saved caption when there is one (for this very video: same sig), else
    platform.default_caption()."""
    with _Con() as con:
        row = con.execute('SELECT caption, sig, item_key FROM queue WHERE platform = ? AND item_key = ?',
                          (item.platform, item.key)).fetchone()
    if row and row['caption'] is not None and _same(row, item):
        return row['caption'], True
    return platform(item.platform).default_caption(item, Path(root)), False


def _same(row, item: Item) -> bool:
    """A queue row or job is about this item: the same sig (a row without one, made by hand: the same key)."""
    return row['sig'] == item.sig if row['sig'] is not None else row['item_key'] == item.key


def _code(url) -> str | None:
    """The shortcode of an Instagram permalink, or None."""
    m = _SHORTCODE.search(url or '')
    return m.group(1) if m else None


def _pending_record(j) -> dict | None:
    """The record a done job keeps until Drive takes it (record_pending), else None."""
    if not (j['state'] == 'done' and j['record_pending'] and j['record']):
        return None
    try:
        rec = json.loads(j['record'])
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _history(posts, jobs, media, codes) -> list[Post]:
    """history() from parts already read: record posts, job rows, media rows, record permalink shortcodes."""
    out = [x for x in posts if x.at]
    ids = {x.id for x in out if x.id and x.id != 'hand'}
    for j in jobs:
        rec = _pending_record(j)
        if j['media_id'] and j['published'] and j['media_id'] not in ids:
            out.append(Post(at=_time(j['published']), by=j['by'], key=j['item_key'], id=j['media_id']))
            ids.add(j['media_id'])
        elif j['state'] == 'unknown' and not j['media_id']:
            out.append(Post(at=_time(j['started'] or j['created']), by=j['by'], key=j['item_key']))
        elif rec and not j['media_id']:                      # 'It is on Instagram' whose record waits
            out.append(Post(at=_time(rec.get('posted_utc')) or _time(j['finished']), by='hand', key=j['item_key']))
    for m in media:
        if m['id'] not in ids and _code(m['permalink']) not in codes and _time(m['at']):
            out.append(Post(at=_time(m['at']), by='outside', id=m['id']))
    return sorted((x for x in out if x.at), key=lambda x: x.at)


def _index(rows) -> tuple[dict, dict]:
    """Rows (jobs) by sig, and by key for the rows without a sig; a later row wins."""
    by_sig, by_key = {}, {}
    for r in rows:
        if r['sig'] is not None:
            by_sig[r['sig']] = r
        else:
            by_key[r['item_key']] = r
    return by_sig, by_key


class _View:
    """One read of a platform's queue state: candidates, settings, queue rows, jobs, media and the history.
    Queue rows and jobs count for an item only when their sig matches (see Item.sig). A lesson folder renamed
    keeps its decisions: a queue row whose lesson folder is gone moves to the candidate with the same sig.
    strict=False (the page bodies only, never a post): a Drive record file that cannot be read goes to
    problems and the history is built from the others, instead of raising (F32)."""
    def __init__(self, root: Path, p: Platform, now: datetime, strict: bool = True):
        global _ROOT
        self.root, self.p, self.now = Path(root), p, _utc(now)
        _ROOT = self.root
        found, self.problems = p.candidates(self.root)
        self.items = sorted(found, key=lambda i: i.rank)
        self.by_key = {i.key: i for i in self.items}
        self.loaded = settings(self.root)
        self.cfg = S.platform_settings(self.loaded, p.name)
        with _Con() as con:
            self.rows = {r['item_key']: r for r in con.execute('SELECT * FROM queue WHERE platform = ?', (p.name,))}
            self.jobs = con.execute('SELECT * FROM jobs WHERE platform = ? ORDER BY created, rowid', (p.name,)).fetchall()
            self.media = {m['id']: m for m in con.execute('SELECT * FROM media WHERE platform = ?', (p.name,))}
        if self._adopt():
            with _Con() as con:
                self.rows = {r['item_key']: r for r in con.execute('SELECT * FROM queue WHERE platform = ?', (p.name,))}
        with _Con() as con:
            self.rev = _rev(con, p.name)
        self.live = _index(j for j in self.jobs if j['media_id'] or _pending_record(j))
        self.unresolved = [j for j in self.jobs if j['state'] in _UNRESOLVED]
        self._unres = _index(self.unresolved)
        self.open = [j for j in self.jobs if j['state'] in _OPEN]
        codes = {_code((i.posted or {}).get('permalink')) for i in self.items} - {None}
        if strict:
            posts = p.history(self.root)
        else:
            unread = []
            posts = p.history(self.root, problems=unread)
            bad = {x.get('lesson') for x in self.problems}
            self.problems += [x for x in unread if x.get('lesson') not in bad]
        self.hist = _history(posts, self.jobs, self.media.values(), codes)

    def _adopt(self) -> bool:
        """Move the queue rows of renamed lesson folders to their videos' new keys: a row whose video (sig) is no
        longer at its key moves to the candidate with that sig that has no row (R1: by video, not by folder, so a
        new folder under the old name does not stop it). Re-checked inside the write (R2: two views at once).
        Audits 'queue.moved' and bumps the rev. True when a row moved."""
        bad = {x.get('lesson') for x in self.problems}
        orphans = {}
        for r in sorted(self.rows.values(), key=lambda r: r['updated']):
            here = self.by_key.get(r['item_key'])
            if r['sig'] and (here is None or not _same(r, here)) and r['item_key'].split('/', 1)[0] not in bad:
                orphans[r['sig']] = r
        moves = [(i, orphans.pop(i.sig)) for i in self.items if self.row(i) is None and i.sig in orphans]
        if not moves:
            return False
        moved = False
        with _Con(write=True) as con:
            for item, r in moves:
                if not con.execute('SELECT 1 FROM queue WHERE platform = ? AND item_key = ? AND sig = ?',
                                   (self.p.name, r['item_key'], r['sig'])).fetchone():
                    continue                                 # another view moved it meanwhile
                con.execute('DELETE FROM queue WHERE platform = ? AND item_key = ? AND sig IS NOT ?',
                            (self.p.name, item.key, r['sig']))
                con.execute('UPDATE queue SET item_key = ?, updated = ? WHERE platform = ? AND item_key = ?',
                            (item.key, S.iso(_utcnow()), self.p.name, r['item_key']))
                _audit(con, 'dashboard', 'queue.moved', f'{item.file.name}: Skip, caption, order and Failed mark kept '
                       f'after the lesson folder was renamed ({r["item_key"].split("/", 1)[0]} -> {item.lesson}).',
                       platform=self.p.name, item_key=item.key, data={'from': r['item_key']})
                moved = True
            if moved:
                _bump(con, self.p.name)
        return moved

    def row(self, item: Item):
        """The item's queue row, or None (a row of another video at the same path does not count)."""
        r = self.rows.get(item.key)
        return r if r is not None and _same(r, item) else None

    def live_job(self, item: Item):
        """The newest job that put this item on the platform (a media id, or a record that waits), or None."""
        return self.live[0].get(item.sig) or self.live[1].get(item.key)

    def checking_job(self, item: Item):
        """The interrupted or unknown job of this item, or None."""
        return self._unres[0].get(item.sig) or self._unres[1].get(item.key)

    def state(self, item: Item) -> str:
        """spec 5.1: posted, checking, failed, skipped or waiting (the first match wins)."""
        row = self.row(item)
        if item.posted or self.live_job(item):
            return 'posted'
        if self.checking_job(item):
            return 'checking'
        if row and row['failed']:
            return 'failed'
        if row and row['skip']:
            return 'skipped'
        return 'waiting'

    def waiting(self) -> list[Item]:
        """Waiting items in queue order: moved to the top first (latest move first), then natural order."""
        def order(i):
            row = self.row(i)
            return (0, -row['top']) if row and row['top'] is not None else (1, 0)
        return sorted((i for i in self.items if self.state(i) == 'waiting'), key=order)

    def posted_info(self, item: Item) -> dict | None:
        rec, j = item.posted, None if item.posted else self.live_job(item)
        if rec:
            at, by, url, mid = _time(rec.get('posted_utc')), rec.get('by') or 'old', rec.get('permalink'), rec.get('id')
        elif j and j['media_id']:
            at, by, url, mid = _time(j['published']), j['by'], j['permalink'], j['media_id']
        elif j:
            rec = _pending_record(j) or {}
            at, by, url, mid = _time(rec.get('posted_utc')) or _time(j['finished']), 'hand', rec.get('permalink'), None
        else:
            return None
        m = self.media.get(mid) if mid else None
        return {'at': S.iso(at) if at else None, 'by': by, 'url': url or (m['permalink'] if m else None) or None,
                'likes': m['likes'] if m else None, 'comments': m['comments'] if m else None}

    def duplicate(self, item: Item, caption: str) -> str | None:
        """The ISO time of a post that is already out with this caption (the same Short under another path: a
        copied or re-rendered lesson), or None. Compares the caption head with the jobs that have a media id and
        with the media cache, and the caption sha with the Drive records."""
        head, sha = _head(caption), _sha(caption)
        if not head or self.state(item) == 'posted':
            return None
        for j in self.jobs:
            if j['media_id'] and (j['caption_head'] == head or j['caption_sha'] == sha) and not _same(j, item):
                return j['published'] or j['started']
        for m in self.media.values():
            if m['caption_head'] == head:
                return m['at']
        for i in self.items:
            if i.posted and i.key != item.key and i.posted.get('caption_sha') == sha:
                return i.posted.get('posted_utc')
        return None

    def item_json(self, item: Item, position: int | None = None, eta: str | None = None) -> dict:
        """The Item object of spec 8.2."""
        row, state = self.row(item), self.state(item)
        return {'id': item.id, 'key': item.key, 'lesson': item.lesson, 'file': item.file.name, 'part': item.part,
                'title': item.title, 'seconds': item.seconds, 'state': state, 'position': position,
                'top': bool(row and row['top'] is not None), 'custom_caption': bool(row and row['caption'] is not None),
                'failed': {'at': row['failed_at'], 'reason': row['failed']} if row and row['failed'] else None,
                'posted': self.posted_info(item) if state == 'posted' else None,
                'cover': f'/api/cover/{self.p.name}/{item.id}.jpg' if _cover(self.p, item) else None, 'eta': eta}

    def last_post(self) -> dict | None:
        """{'at', 'by', 'key', 'title', 'url'} of the newest counted post, or None."""
        last = S.last_post(self.hist)
        if not last:
            return None
        item = self.by_key.get(last.key) if last.key else None
        info = self.posted_info(item) if item else None
        m = self.media.get(last.id) if last.id else None
        url = (info or {}).get('url') or (m['permalink'] if m else None)
        return {'at': S.iso(last.at), 'by': last.by, 'key': last.key, 'title': item.title if item else None,
                'url': url or None}


def _cover(p: Platform, item: Item) -> Path | None:
    try:
        return p.cover(item)
    except Exception:
        return None


# ---------- job rows ----------

def _rev(con, name: str) -> int:
    row = con.execute('SELECT rev FROM queue_meta WHERE platform = ?', (name,)).fetchone()
    return row['rev'] if row else 0


def _set_queue(con, name: str, key: str, sig: str | None, *, bump: bool = True, **cols) -> None:
    """Change one queue row (created when missing) and bump the queue rev. sig is the video's (Item.sig): a row
    left by another video at this path is cleared first, so the new one gets none of its decisions."""
    now = S.iso(_utcnow())
    old = con.execute('SELECT sig FROM queue WHERE platform = ? AND item_key = ?', (name, key)).fetchone()
    if old is not None and sig is not None and old['sig'] is not None and old['sig'] != sig:
        con.execute('DELETE FROM queue WHERE platform = ? AND item_key = ?', (name, key))
    con.execute('INSERT INTO queue (platform, item_key, updated, sig) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(platform, item_key) DO NOTHING', (name, key, now, sig))
    con.execute(f'UPDATE queue SET {"".join(f"{k} = ?, " for k in cols)}sig = COALESCE(?, sig), updated = ? '
                'WHERE platform = ? AND item_key = ?', (*cols.values(), sig, now, name, key))
    if bump:
        _bump(con, name)


def _job_queue_key(con, name: str, r) -> str:
    """Where a job's item has its queue row now: the row with the job's sig (the lesson folder may have been
    renamed since), else the job's own key."""
    if r['sig'] is not None:
        row = con.execute('SELECT item_key FROM queue WHERE platform = ? AND sig = ? ORDER BY updated DESC LIMIT 1',
                          (name, r['sig'])).fetchone()
        if row:
            return row['item_key']
    return r['item_key']


def _update_job(job_id: str, **cols) -> None:
    with _Con(write=True) as con:
        con.execute(f'UPDATE jobs SET {", ".join(f"{k} = ?" for k in cols)} WHERE id = ?', (*cols.values(), job_id))


def _note_status(job_id: str, status: str) -> None:
    """Remember the upload status a check read (resolve_job never calls a PUBLISHED upload 'not posted')."""
    _update_job(job_id, last_status=str(status), checked=S.iso(_utcnow()))


def _job_row(job_id: str) -> sqlite3.Row:
    row = None
    if isinstance(job_id, str):
        with _Con() as con:
            row = con.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
    if row is None:
        raise PublishError(404, 'unknown_job', 'Unknown job.')
    return row


def _job_json(r) -> dict:
    """The Job object of spec 8.2."""
    return {'id': r['id'], 'platform': r['platform'], 'item': item_id(r['item_key']), 'key': r['item_key'],
            'title': r['title'], 'by': r['by'], 'who': r['who'], 'state': r['state'], 'stage': r['stage'],
            'stages': list(STAGES), 'created': r['created'], 'started': r['started'], 'finished': r['finished'],
            'url': r['permalink'] or None, 'kind': r['kind'], 'error': r['error'], 'result': r['result'],
            'record_pending': bool(r['record_pending'])}


def job(job_id: str) -> dict:
    """The Job object (spec 8.2). Raises PublishError(404, 'unknown_job')."""
    return _job_json(_job_row(job_id))


def _job_or_none(job_id) -> dict | None:
    try:
        return job(job_id) if job_id else None
    except Exception:
        return None


def current_job(platform_name: str) -> dict | None:
    """The queued or running job; else the newest job that ended in the last 10 minutes; else None."""
    with _Con() as con:
        row = con.execute("SELECT * FROM jobs WHERE platform = ? AND state IN ('queued', 'running') "
                          'ORDER BY created DESC, rowid DESC LIMIT 1', (platform_name,)).fetchone()
        if row is None:
            since = S.iso(_utcnow() - timedelta(seconds=_ENDED))
            row = con.execute('SELECT * FROM jobs WHERE platform = ? AND finished >= ? '
                              'ORDER BY finished DESC, rowid DESC LIMIT 1', (platform_name, since)).fetchone()
    return _job_json(row) if row else None


def unresolved_jobs(platform_name: str) -> list[dict]:
    """Jobs in state 'interrupted' or 'unknown', oldest first."""
    with _Con() as con:
        rows = con.execute("SELECT * FROM jobs WHERE platform = ? AND state IN ('interrupted', 'unknown') "
                           'ORDER BY created, rowid', (platform_name,)).fetchall()
    return [_job_json(r) for r in rows]


# ---------- forecast, the auto object, checks ----------

def _after(t: datetime | None) -> str:
    """'Thu 5 Nov after 20:00' (Istanbul)."""
    return f'{S.fmt_day(t)} after {S.local(t):%H:%M}' if t else ''


def _forecast(root, p: Platform, cfg: dict, assumes_on: bool, v: _View | None = None) -> dict:
    """The 'forecast' object: where the waiting items would go with these settings if every post went out.
    rows: those within the search horizon; more: how many waiting items they leave out; runway: the last
    item's slot over the whole queue ('at least' in the text when even that stops early)."""
    try:
        v = v or _View(root, p, _utcnow(), strict=False)
        wait = v.waiting()
        full, rows = _plan(v, cfg, wait)
    except Exception:
        return {'assumes_on': bool(assumes_on), 'runway': None, 'runway_text': '', 'rows': [], 'more': 0}
    ids = {i.key: i.id for i in wait}
    if full:
        text = f'Enough {p.noun}s until {"at least " if len(full) < len(wait) else ""}{S.fmt_day(full[-1]["at"])}'
    else:
        text = f'No {p.noun}s are waiting.' if not wait else f'No slot in the next {S.SEARCH_DAYS} days.'
    return {'assumes_on': bool(assumes_on), 'runway': S.runway(full), 'runway_text': text,
            'rows': [{'id': ids[r['key']], 'at': S.iso(r['at']), 'slot': r['slot']} for r in rows],
            'more': len(wait) - len(rows)}


def _auto_state(name: str):
    with _Con() as con:
        return con.execute('SELECT * FROM auto_state WHERE platform = ?', (name,)).fetchone()


def _not_before(name: str, now: datetime) -> datetime:
    """The earliest time auto_step may try again (its steps 5 and 6): now; after a failed try, 30 min later
    when one retry is left (transient or local), else the next Istanbul midnight (today's slot is used up); and
    not before the 24 h attempt limit frees a try."""
    now = _utc(now)
    with _Con() as con:
        row = con.execute('SELECT * FROM slots WHERE platform = ? AND slot = ?', (name, S.local_date(now))).fetchone()
        times = _attempt_times(con, name, now)
    t = now
    if len(times) >= ATTEMPTS_24H:
        t = max(t, times[len(times) - ATTEMPTS_24H] + _DAY)
    if row and row['attempts'] < SLOT_ATTEMPTS and row['last_kind'] in ('transient', 'local') and _time(row['last_at']):
        t = max(t, _time(row['last_at']) + timedelta(seconds=RETRY_AFTER))
    elif row:
        t = max(t, S.at_local((S.local(now).date() + timedelta(days=1)).isoformat(), '00:00'))
    return t


def _plan(v: '_View', cfg: dict, wait: list[Item]) -> tuple[list[dict], list[dict]]:
    """(the whole simulated queue, its rows within the search horizon): S.forecast from the time auto_step may
    try next, up to a year, so the runway counts every waiting item (addendum C5: the list says how many it
    leaves out)."""
    t = _not_before(v.p.name, v.now)
    full = S.forecast(cfg, v.hist, t, [i.key for i in wait], limit_days=366)
    last = (S.local(t).date() + timedelta(days=S.SEARCH_DAYS)).isoformat()
    return full, [r for r in full if r['slot'] <= last]


def _auto(root, p: Platform, now: datetime, loaded: S.Loaded | None = None, v: _View | None = None) -> dict:
    """The 'auto' object of GET /api/publish (spec 8.3)."""
    loaded = loaded or (v.loaded if v else settings(root))
    cfg = S.platform_settings(loaded, p.name)
    on, reason = S.effective_auto(loaded, p.name)
    st, nxt = _auto_state(p.name), None
    if on:
        if st and st['suspended']:
            reason = 'suspended'
        elif breaker(p.breaker)['state'] != 'closed':
            reason = 'breaker'
        elif unresolved_jobs(p.name):
            reason = 'unresolved'
        else:
            try:
                v = v or _View(root, p, now, strict=False)
                wait = v.waiting()
                at, slot, reason = S.next_auto(cfg, v.hist, _not_before(p.name, now), len(wait))
            except Exception:
                at, slot, reason, wait = None, None, 'no_slot', []
            text = {'scheduled': f'Next automatic {p.noun}: {_after(at)}', 'empty': f'No {p.noun}s are waiting.'
                    }.get(reason, f'No slot in the next {S.SEARCH_DAYS} days.')
            nxt = {'at': S.iso(at) if at else None, 'slot': slot, 'id': wait[0].id if at and wait else None,
                   'reason': reason, 'text': text}
    return {'setting': bool(cfg['auto']), 'effective': on, 'reason': reason,
            'text': 'Automatic posting is on.' if on else 'Automatic posting is off.',
            'summary': S.summary(cfg, p.noun), 'pause_file': S.pause_info(p.name),
            'suspended': {'since': st['suspended'], 'why': st['suspended_why']} if st and st['suspended'] else None,
            'next': nxt}


def _check_json(c: Check) -> dict:
    return {'code': c.code, 'level': c.level, 'text': c.text, 'data': dict(c.data)}


def _checks(v: _View, item: Item, caption: str, *, fresh: bool = False, busy: bool | None = None) -> list[Check]:
    """Every spec 5.3 check for posting item with caption now: blocks (table order), then warns, then infos."""
    p, out = v.p, []
    if not p.set_up():
        out.append(Check('not_set_up', 'block', f'{p.label} is not set up on this PC.'))
    b = breaker(p.breaker)
    if b['state'] != 'closed':
        out.append(Check('breaker', 'block', f'Posting is stopped: {b["message"]}', {'state': b['state'], 'until': b['until']}))
    if any(not _same(j, item) for j in v.unresolved):
        out.append(Check('unresolved', 'block', 'An earlier post was interrupted. Check it first.'))
    if busy is None:
        busy = bool(v.open) or bool((PostLock.holder() or {}).get('job'))     # a record write is not a post
    if busy:
        out.append(Check('busy', 'block', f'A {p.noun} is being posted right now.'))
    state = v.state(item)
    if state == 'posted':
        at = (v.posted_info(item) or {}).get('at')
        out.append(Check('posted', 'block', f'Already on {p.label} ({S.fmt(_time(at))}).' if at else
                         f'Already on {p.label}.', {'at': at}))
    elif state == 'checking':
        out.append(Check('checking', 'block', f"This {p.noun}'s last post was interrupted. Check it first."))
    out += S.spacing_checks(v.cfg, v.hist, v.now, p.noun)
    out += p.preflight(item, caption, fresh)
    dup = v.duplicate(item, caption)
    if dup:
        out.append(Check('duplicate', 'warn', f'A {p.noun} with this caption is already on {p.label} '
                         f'({S.fmt(_time(dup))}): the same {p.noun} in another folder?', {'at': dup}))
    if state == 'skipped':
        out.append(Check('skipped', 'warn', f'This {p.noun} is marked Skip.'))
    elif state == 'failed':
        out.append(Check('failed_before', 'warn', f'The last try failed: {v.row(item)["failed"]}'))
    wait = v.waiting()
    if state != 'posted' and wait and wait[0].key != item.key:
        out.append(Check('not_next', 'info', f'Not the next in the queue (next: {wait[0].title}).', {'next': wait[0].id}))
    return sorted(out, key=lambda c: (_LEVELS.index(c.level) if c.level in _LEVELS else 3,
                                      _ORDER.index(c.code) if c.code in _ORDER else len(_ORDER)))


def fingerprint(platform_name: str, item: Item, history: list[Post], now: datetime, checks: list[Check]) -> str:
    """sha1('platform|key|last post ISO|posts in 24 h|posted or waiting|sorted warn codes')[:16]."""
    last = S.last_post(history)
    posted = 'posted' if item.posted or any(c.code == 'posted' for c in checks) else 'waiting'
    warns = ','.join(sorted(c.code for c in checks if c.level == 'warn'))
    raw = f'{platform_name}|{item.key}|{S.iso(last.at) if last else ""}|{S.posts_in_24h(history, now)}|{posted}|{warns}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def _stopped(b: dict) -> str:
    """The 423 message for an open breaker."""
    hint = (' Test the connection in Publish.' if b['state'] == 'stopped' else
            f' Posting starts again after {S.fmt(_time(b["until"]))}.')
    return f'Posting is stopped: {b["message"]}{hint}'


# ---------- the queue ----------

def queue(root: Path, platform_name: str, now: datetime | None = None, v: '_View | None' = None) -> dict:
    """The 'queue' object of GET /api/publish: {'rev', 'waiting', 'failed', 'skipped', 'posted' (newest 10),
    'posted_total', 'problems'}. Waiting order: items moved to the top (latest move first), then Item.rank.
    v: the platform's entry of views() to reuse."""
    return _queue(v or _View(root, platform(platform_name), _now(now), strict=False))


def _queue(v: _View) -> dict:
    wait = v.waiting()
    try:
        eta = {r['key']: S.iso(r['at']) for r in _plan(v, v.cfg, wait)[1]}
    except Exception:
        eta = {}
    groups = {'failed': [], 'skipped': [], 'posted': []}
    for i in v.items:
        st = v.state(i)
        if st != 'waiting':
            groups['failed' if st == 'checking' else st].append(v.item_json(i))
    posted = sorted(groups['posted'], key=lambda j: (j['posted'] or {}).get('at') or '', reverse=True)
    return {'rev': v.rev, 'waiting': [v.item_json(i, n, eta.get(i.key)) for n, i in enumerate(wait, 1)],
            'failed': groups['failed'], 'skipped': groups['skipped'], 'posted': posted[:10],
            'posted_total': len(posted), 'problems': list(v.problems)}


_DONE = {'top': 'Moved to the top.', 'untop': 'Back in its normal place.', 'reset_order': 'Back to the normal order.',
         'skip': 'Marked Skip.', 'unskip': 'Back in the queue.', 'caption': 'Caption saved.',
         'clear_caption': 'Back to the default caption.', 'clear_failed': 'Ready to try again.',
         'mark_posted': 'Marked as posted.'}
_DID = {'top': 'moved to the top', 'untop': 'back in its normal place', 'skip': 'marked Skip', 'unskip': 'Skip removed',
        'caption': 'caption changed', 'clear_caption': 'caption back to the default', 'clear_failed': 'Failed mark cleared',
        'mark_posted': 'marked as posted by hand'}


def queue_op(root: Path, platform_name: str, op: str, rev: int, by: str, *, item_id: str | None = None,
             caption: str | None = None, permalink: str | None = None, at: str | None = None) -> dict:
    """The POST /api/queue body {'ok', 'queue', 'message'}.
    ops: top, untop, reset_order, skip, unskip, caption, clear_caption, clear_failed, and mark_posted (P1:
    writes a hand record under the PostLock). rev must equal the current queue rev, else PublishError(409,
    'stale', queue=<current>). Every op bumps rev and is audited 'queue.<op>'.
    Raises PublishError: 404 unknown_item; 409 posted, unresolved or busy; 422 bad_op or problems."""
    p, root = platform(platform_name), Path(root)
    if op not in _OPS:
        raise PublishError(422, 'bad_op', 'Unknown queue operation.')
    if op != 'reset_order' and not isinstance(item_id, str):
        raise PublishError(422, 'bad_op', 'This operation needs an id.')
    v = _View(root, p, _utcnow())
    item = None
    if op != 'reset_order':
        item = next((i for i in v.items if i.id == item_id), None)
        if item is None:
            raise PublishError(404, 'unknown_item', f'This {p.noun} is not in the list any more.')
    if type(rev) is not int or rev != v.rev:
        raise PublishError(409, 'stale', 'The queue changed on another phone. Here is the new one.', queue=_queue(v))
    state = v.state(item) if item else None
    if state == 'posted':
        raise PublishError(409, 'posted', f'This {p.noun} is already on {p.label}.',
                           url=(v.posted_info(item) or {}).get('url'))
    if state == 'checking' and op in ('clear_failed', 'mark_posted'):
        raise PublishError(409, 'unresolved', 'An earlier post was interrupted. Check it first.',
                           job=_job_json(v.checking_job(item)))
    cols = {}
    if op == 'caption':
        if not isinstance(caption, str):
            raise PublishError(422, 'bad_op', 'Send the caption as text.')
        blocks = [c for c in _caption_checks(p, caption) if c.level == 'block']
        if blocks:
            raise PublishError(422, 'problems', 'Fix these first.', checks=[_check_json(c) for c in blocks])
        cols['caption'] = None if caption.strip() == (p.default_caption(item, root) or '').strip() else caption
    elif op == 'mark_posted':
        return _mark_posted(v, item, rev, by, permalink, at)
    else:
        cols = {'top': 'next', 'untop': {'top': None}, 'skip': {'skip': 1}, 'unskip': {'skip': 0},
                'clear_caption': {'caption': None}, 'clear_failed': {'failed': None, 'failed_at': None},
                'reset_order': {}}[op]
    with _Con(write=True) as con:
        fresh = _rev(con, p.name) == rev
        if fresh:
            if op == 'reset_order':
                con.execute('UPDATE queue SET top = NULL, updated = ? WHERE platform = ? AND top IS NOT NULL',
                            (S.iso(_utcnow()), p.name))
                _bump(con, p.name)
            else:
                if cols == 'next':
                    cols = {'top': con.execute('SELECT COALESCE(MAX(top), 0) + 1 AS n FROM queue WHERE platform = ?',
                                               (p.name,)).fetchone()['n']}
                _set_queue(con, p.name, item.key, item.sig, **cols)
            text = f'{item.file.name}: {_DID[op]}.' if item else f'{p.label}: queue back to the normal order.'
            _audit(con, by, f'queue.{op}', text, platform=p.name, item_key=item.key if item else None)
    if not fresh:
        raise PublishError(409, 'stale', 'The queue changed on another phone. Here is the new one.',
                           queue=queue(root, p.name))
    return {'ok': True, 'queue': queue(root, p.name), 'message': _DONE[op]}


def _bump(con, name: str) -> None:
    con.execute('INSERT INTO queue_meta (platform, rev) VALUES (?, 1) '
                'ON CONFLICT(platform) DO UPDATE SET rev = rev + 1', (name,))


def _stamp(t: datetime) -> dict:
    """A record's 'posted' (Istanbul 'YYYY-MM-DD HH:MM') and 'posted_utc' (isoformat) fields."""
    return {'posted': f'{S.local(t):%Y-%m-%d %H:%M}', 'posted_utc': _utc(t).isoformat()}


def _mark_posted(v: _View, item: Item, rev: int, by: str, permalink, at) -> dict:
    """queue_op 'mark_posted' (P1): a hand record for a Reel posted in the app, written under the PostLock."""
    p = v.p
    permalink = (permalink or '').strip() if isinstance(permalink, str) or permalink is None else None
    if permalink is None or (permalink and not (p.link_re or _PERMALINK).match(permalink)):
        raise PublishError(422, 'bad_op', p.link_hint or 'Use a link like https://www.instagram.com/reel/…/, or leave it empty.')
    t = _time(at) if at else v.now
    if t is None or not v.now - timedelta(days=30) <= t <= v.now + timedelta(minutes=5):
        raise PublishError(422, 'bad_op', 'Use a time in the last 30 days.')
    record = {'id': 'hand', 'permalink': permalink, **_stamp(t), 'by': 'hand'}
    with PostLock(owner='dashboard'):
        try:
            p.write_record(item, record)
        except OSError:
            raise PublishError(503, 'write_failed', 'Drive did not take the record. Try again.') from None
    with _Con(write=True) as con:
        _set_queue(con, p.name, item.key, item.sig, failed=None, failed_at=None)
        _audit(con, by, 'queue.mark_posted', f'{item.file.name}: {_DID["mark_posted"]}.', platform=p.name,
               item_key=item.key, data={'url': permalink or None, 'at': S.iso(t)})
    return {'ok': True, 'queue': queue(v.root, p.name), 'message': _DONE['mark_posted']}


# ---------- preview and manual posts ----------

def preview(root: Path, platform_name: str, item_id: str, now: datetime | None = None) -> dict:
    """The GET /api/preview body: the item, caption, default caption, caption limits, every check (block,
    warn, info), the fingerprint 'check', 'can_post', 'blocked_by', 'next_possible', 'last_post', 'posts_24h'.
    Runs reconcile(api=False) first. Local only."""
    p, root, now = platform(platform_name), Path(root), _now(now)
    _reconcile(root, p, False, now, 'dashboard')
    v = _View(root, p, now)
    item = next((i for i in v.items if isinstance(item_id, str) and i.id == item_id), None)
    if item is None:
        raise PublishError(404, 'unknown_item', f'This {p.noun} is not in the list any more.')
    return _preview(v, item)


def _preview(v: _View, item: Item) -> dict:
    p = v.p
    caption, _ = caption_for(v.root, item)
    checks = _checks(v, item, caption)
    blocks = [c for c in checks if c.level == 'block']
    cap = next((c for c in checks if c.code == 'hard_cap'), None)
    pos = next((n for n, i in enumerate(v.waiting(), 1) if i.key == item.key), None)
    return {'platform': p.name, 'item': v.item_json(item, pos), 'caption': caption,
            'default_caption': p.default_caption(item, v.root),
            'caption_limits': {'chars': p.caption_max, 'tags_hard': p.tags_max, 'tags_suggested': p.tags_suggested,
                               'visible': _VISIBLE},
            'checks': [_check_json(c) for c in checks], 'check': fingerprint(p.name, item, v.hist, v.now, checks),
            'can_post': not blocks, 'blocked_by': blocks[0].code if blocks else None,
            'next_possible': cap.data.get('next_possible') if cap else None,
            'last_post': v.last_post(), 'posts_24h': S.posts_in_24h(v.hist, v.now)}


def start_post(root: Path, platform_name: str, item_id: str, caption: str, save_caption: bool, check: str,
               override: list[str], by: str) -> dict:
    """The POST /api/post 202 body {'ok', 'job', 'message'}. Runs the checks in the order of spec 5.4, creates
    the job in one BEGIN IMMEDIATE transaction (no other queued or running job), audits 'post.requested', and
    posts in a daemon thread (run_post).
    Raises PublishError: 404 unknown_platform or unknown_item; 409 not_set_up, posted, unresolved, busy or
    stale (with 'preview'); 422 problems; 423 breaker; 428 confirm; 429 hard_cap or rate_limited."""
    p = platform(platform_name)                                                     # 1
    if not p.set_up():                                                              # 2
        raise PublishError(409, 'not_set_up', f'{p.label} is not set up on this PC.')
    root, now = Path(root), _utcnow()
    _reconcile(root, p, False, now, 'dashboard')                  # local only; then one Drive scan (G17)
    v = _View(root, p, now)
    item = next((i for i in v.items if isinstance(item_id, str) and i.id == item_id), None)
    if item is None:                                                                # 3
        raise PublishError(404, 'unknown_item', f'This {p.noun} is not in the list any more.')
    state = v.state(item)
    if state == 'posted':                                                           # 4
        raise PublishError(409, 'posted', f'This {p.noun} is already on {p.label}.',
                           url=(v.posted_info(item) or {}).get('url'))
    if state == 'checking' or v.unresolved:                                         # 5
        raise PublishError(409, 'unresolved', 'An earlier post was interrupted. Check it first.',
                           job=_job_json(v.unresolved[0]))
    b = breaker(p.breaker)
    if b['state'] != 'closed':                                                      # 6
        raise PublishError(423, 'breaker', _stopped(b), breaker=b)
    if v.open or _holder_after_wait() is not None:                                  # 7
        raise PublishError(409, 'busy', f'A {p.noun} is being posted right now.',
                           job=_job_json(v.open[-1]) if v.open else None)
    with _Con() as con:                                                             # 8
        mate = con.execute("SELECT * FROM jobs WHERE platform = ? AND state IN ('queued', 'running') LIMIT 1",
                           (p.name,)).fetchone()                  # a job made since the view: still busy (S2)
        wait = None if mate else _rate_wait(con, p.name, now)
    if mate:
        raise PublishError(409, 'busy', f'A {p.noun} is being posted right now.', job=_job_json(mate))
    if wait:
        raise PublishError(429, 'rate_limited', 'Wait a moment before posting again.', retry_after=wait)
    caption = caption if isinstance(caption, str) else ''
    checks = _checks(v, item, caption, busy=False)
    bad = [c for c in checks if c.code in ('caption_empty', 'caption_too_long', 'caption_tags_limit')]
    if bad:                                                                         # 9
        raise PublishError(422, 'problems', 'Fix these first.', checks=[_check_json(c) for c in bad])
    cap = next((c for c in checks if c.code == 'hard_cap'), None)
    if cap:                                                                         # 10
        n = S.posts_in_24h(v.hist, now)
        raise PublishError(429, 'hard_cap', f'{n} {p.noun}s in the last 24 hours: that is the limit.',
                           next_possible=cap.data.get('next_possible'))
    bad = [c for c in checks if c.code in ('file_missing', 'length')]
    if bad:
        raise PublishError(422, 'problems', 'Fix these first.', checks=[_check_json(c) for c in bad])
    fp = fingerprint(p.name, item, v.hist, now, checks)
    if check != fp:                                                                 # 11
        raise PublishError(409, 'stale', 'Something changed since you opened this. Check again.',
                           preview=_preview(v, item))
    warns = [c for c in checks if c.level == 'warn']
    allowed = set(x for x in override if isinstance(x, str)) if isinstance(override, (list, tuple, set)) else set()
    if any(c.code not in allowed for c in warns):                                   # 12
        raise PublishError(428, 'confirm', "This breaks the posting rules. Tick 'I understand, post anyway' to post.",
                           checks=[_check_json(c) for c in warns], check=fp)
    keep = None                                                                     # 13
    if save_caption:
        keep = (None if caption.strip() == (p.default_caption(item, root) or '').strip() else caption,)
    job_id = _new_job(p, item, 'manual', by, caption, now, fingerprint=fp, override=sorted(c.code for c in warns),
                      keep_caption=keep)
    body = {'ok': True, 'job': job(job_id), 'message': f'Posting {item.file.stem}… (takes 1–3 minutes)'}
    for old in [k for k, t in _threads.items() if not t.is_alive()]:
        _threads.pop(old, None)
    t = threading.Thread(target=run_post, args=(job_id, root, caption), name=f'post-{job_id}', daemon=True)
    _threads[job_id] = t
    t.start()
    return body


def _attempt_times(con, name: str, now: datetime) -> list[datetime]:
    """Start times of the jobs that reached 'sharing' in the 24 h before now, oldest first (spec 5.9). A time
    after now (the clock was set back) counts too."""
    rows = con.execute("SELECT COALESCE(started, created) AS t FROM jobs WHERE platform = ? AND stage IN "
                       "('sharing', 'processing', 'publishing', 'saving')", (name,)).fetchall()
    return sorted(t for t in (_time(r['t']) for r in rows) if t and now - _DAY < t)


def _rate_wait(con, name: str, now: datetime) -> int | None:
    """Seconds until the manual limits allow a post: one accepted manual post per POST_SPACING s, and fewer
    than ATTEMPTS_24H attempts in 24 h. None when a post may start now."""
    waits = []
    row = con.execute("SELECT created FROM jobs WHERE platform = ? AND by = 'manual' AND state != 'stale' "
                      'ORDER BY created DESC LIMIT 1', (name,)).fetchone()
    if row and _time(row['created']):
        waits.append(POST_SPACING - (now - _time(row['created'])).total_seconds())
    times = _attempt_times(con, name, now)
    if len(times) >= ATTEMPTS_24H:
        waits.append((times[len(times) - ATTEMPTS_24H] + _DAY - now).total_seconds())
    waits = [w for w in waits if w > 0]
    return max(1, int(max(waits) + 0.999)) if waits else None


def _new_job(p: Platform, item: Item, by: str, who: str, caption: str, now: datetime, *, slot: str | None = None,
             fingerprint: str | None = None, override=(), keep_caption: tuple | None = None, before=None) -> str:
    """Insert a queued job in one BEGIN IMMEDIATE transaction and audit 'post.requested'. Refuses with 409 busy
    when another job of the platform is queued or running, 409 posted when a job already has a media id for
    the item, and (manual) 429 rate_limited. keep_caption=(caption or None,) saves the caption too;
    before(con) runs first inside the same transaction (the automatic step counts its attempt there)."""
    job_id = 'j' + secrets.token_hex(5)
    with _Con(write=True) as con:
        busy = con.execute("SELECT * FROM jobs WHERE platform = ? AND state IN ('queued', 'running') LIMIT 1",
                           (p.name,)).fetchone()
        if busy:
            raise PublishError(409, 'busy', f'A {p.noun} is being posted right now.', job=_job_json(busy))
        if con.execute('SELECT 1 FROM jobs WHERE platform = ? AND media_id IS NOT NULL AND '
                       '(sig = ? OR (sig IS NULL AND item_key = ?))', (p.name, item.sig, item.key)).fetchone():
            raise PublishError(409, 'posted', f'This {p.noun} is already on {p.label}.')
        if by == 'manual' and _rate_wait(con, p.name, now):
            raise PublishError(429, 'rate_limited', 'Wait a moment before posting again.',
                               retry_after=_rate_wait(con, p.name, now))
        if before:
            before(con)
        if keep_caption is not None:                    # saved and audited only when it really changes (S29)
            old = con.execute('SELECT caption, sig, item_key FROM queue WHERE platform = ? AND item_key = ?',
                              (p.name, item.key)).fetchone()
            if (old['caption'] if old and _same(old, item) else None) != keep_caption[0]:
                _set_queue(con, p.name, item.key, item.sig, caption=keep_caption[0])
                _audit(con, who, 'queue.caption', f'{item.file.name}: {_DID["caption"]}.', platform=p.name,
                       item_key=item.key)
        con.execute('INSERT INTO jobs (id, platform, item_key, title, by, who, state, slot, created, caption_sha, '
                    'caption_head, fingerprint, override, result, sig) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (job_id, p.name, item.key, item.title, by, who, 'queued', slot, S.iso(now), _sha(caption),
                     _head(caption), fingerprint, json.dumps(list(override)), _result('queued', p.name), item.sig))
        extra = f' (rules overridden: {", ".join(override)})' if override else ''
        _audit(con, who, 'post.requested', f'{item.file.name}: post requested{extra}.', platform=p.name,
               item_key=item.key, job=job_id, data={'by': by, 'slot': slot, 'override': list(override)})
    return job_id


# ---------- the posting lock ----------

class PostLock:
    """The one posting lock (LOCK), shared by the dashboard, the watcher and the CLI (spec 5.8).
    File content: {"v": 2, "token", "pid", "owner", "job", "started"}. A daemon thread touches the file and the
    job's heartbeat every LOCK_BEAT s. Stale when: v2 and untouched for LOCK_STALE s; v1 and older than
    LOCK_STALE_V1 s; or started more than LOCK_HARD s ago. A takeover re-reads the file, renames it to
    '<name>.stale-<epoch>' and creates a new one with O_EXCL. Released only while the file still holds our
    token. Never uses os.kill. __enter__ raises Busy."""
    def __init__(self, owner: str, job: str | None = None):
        self.owner, self.job = owner, job
        self.token = secrets.token_hex(16)
        self.held = False
        self._stop = threading.Event()
        self._beat = None

    def __enter__(self) -> 'PostLock':
        path = _lockfile()
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            if self._create(path):
                break
            raw = _read_raw(path)
            if raw is None:
                continue                                  # released meanwhile: try again
            if not _stale(path, raw) or not self._take_over(path, raw):
                raise Busy(_lock_info(raw))
            break
        else:
            raise Busy(None)
        self.held = True
        self._stop.clear()
        self._beat = threading.Thread(target=self._beating, name='postlock-beat', daemon=True)
        self._beat.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._beat and self._beat is not threading.current_thread():
            self._beat.join(5)
        if not self.held:
            return
        self.held = False
        path = _lockfile()
        for wait in (0.05, 0.1, 0.2, 0.4, None):
            try:
                if self._mine(path):
                    os.remove(path)
                return
            except FileNotFoundError:
                return
            except PermissionError:                       # another process is reading it this moment
                if wait is None:
                    return
                time.sleep(wait)

    def set_job(self, job_id: str) -> None:
        """Name the job this lock is held for (the automatic step takes the lock before it creates its job).
        Never raises: the job id in the file is only information (another process reading the file can make
        Windows refuse the replace for a moment, so it is tried a few times)."""
        self.job = job_id
        path = _lockfile()
        tmp = path.with_name(f'{path.name}.tmp-{self.token[:8]}')
        try:
            if not (self.held and self._mine(path)):
                return
            info = {**_lock_info(_read_raw(path) or b'{}'), 'job': job_id}
            tmp.write_bytes(json.dumps(info).encode('utf-8'))
            for wait in (0.05, 0.1, 0.2, 0.4, None):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if wait is None:
                        raise
                    time.sleep(wait)
        except OSError as e:
            print(f'publish: the lock file does not name job {job_id} ({type(e).__name__}); posting goes on')
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def mine(self) -> bool:
        """True while this lock is held and the file still holds our token (no one took it over)."""
        return self.held and self._mine(_lockfile())

    @staticmethod
    def holder() -> dict | None:
        """The lock file's content when it exists and is not stale ({'v': 1, 'pid'} for a v1 lock), else None."""
        path = _lockfile()
        raw = _read_raw(path)
        return None if raw is None or _stale(path, raw) else _lock_info(raw)

    def _create(self, path: Path) -> bool:
        data = json.dumps({'v': 2, 'token': self.token, 'pid': os.getpid(), 'owner': self.owner, 'job': self.job,
                           'started': S.iso(datetime.now(timezone.utc))}).encode('utf-8')
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0))
        except (FileExistsError, PermissionError):
            return False
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return True

    def _take_over(self, path: Path, raw: bytes) -> bool:
        """Replace a stale lock: confirm the same content, rename it aside, then create ours with O_EXCL."""
        if _read_raw(path) != raw:
            return False
        aside = path.with_name(f'{path.name}.stale-{int(time.time())}')
        try:
            os.replace(path, aside)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        else:
            if _read_raw(aside) != raw:                   # a fresh lock came between the check and the rename
                try:
                    os.rename(aside, path)                # put it back (fails when yet another lock exists)
                except OSError:
                    pass
                return False
        return self._create(path)

    def _mine(self, path: Path) -> bool:
        raw = _read_raw(path)
        return raw is not None and _lock_info(raw).get('token') == self.token

    def _beating(self) -> None:
        while not self._stop.wait(LOCK_BEAT):
            try:
                path = _lockfile()
                if self._mine(path):
                    os.utime(path)
                if self.job:
                    with _Con(write=True) as con:
                        con.execute("UPDATE jobs SET heartbeat = ? WHERE id = ? AND state = 'running'",
                                    (S.iso(_utcnow()), self.job))
            except Exception:                             # a missed beat is fine; the next one tries again
                pass


def _holder_after_wait(seconds: float | None = None) -> dict | None:
    """PostLock.holder(), after waiting up to LOCK_WAIT s while the holder has no job (a record write or a
    check takes a moment; only a post holds it long)."""
    end = time.monotonic() + (LOCK_WAIT if seconds is None else seconds)
    while True:
        h = PostLock.holder()
        if h is None or h.get('job') or time.monotonic() >= end:
            return h
        time.sleep(0.2)


def _read_raw(path: Path) -> bytes | None:
    """The lock file's bytes; None when there is no file; b'' when it exists but cannot be read now."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return b''


def _lock_info(raw: bytes) -> dict:
    """{'v': 2, ...} for a v2 lock; {'v': 1, 'pid'} for a v1 lock (a bare pid) or unreadable content."""
    text = raw.decode('utf-8', 'replace').strip()
    try:
        info = json.loads(text)
    except ValueError:
        info = None
    if isinstance(info, dict) and info.get('v') == 2:
        return info
    return {'v': 1, 'pid': int(text) if text.isdigit() else None}


def _stale(path: Path, raw: bytes) -> bool:
    info = _lock_info(raw)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if info['v'] != 2:
        return age > LOCK_STALE_V1
    started = _time(info.get('started'))
    if started and (datetime.now(timezone.utc) - started).total_seconds() > LOCK_HARD:
        return True
    return age > LOCK_STALE


# ---------- running a job ----------

_OWNERS = {'manual': 'dashboard', 'auto': 'watcher', 'cli': 'cli'}


def run_post(job_id: str, root: Path, caption: str, lock: PostLock | None = None) -> dict:
    """Run one queued job to its end and return the job object. Takes the PostLock unless a held lock is
    given (waiting up to LOCK_WAIT s while it is held without a job). Under the lock it checks the rules again
    (manual: the fingerprint; automatic: the slot and automatic posting still on; CLI: the spacing), then runs
    preparing (cover, fresh probe) -> platform.post() -> saving (write_record, or record_pending on failure).
    Every stage first checks that the job may go on (Journal.stage), so a job whose lock or row was taken over,
    or an automatic job whose posting was turned off, stops before it sends more ('stale'). A failure after
    journal.published() still ends 'done' with the record pending, never 'failed'. An unclear error while
    publishing ends 'unknown' (a person checks). Never raises."""
    try:
        row = _job_row(job_id)
        p = platform(row['platform'])
    except Exception as e:
        print(f'publish: run_post {job_id}: {redact(str(e))}')
        return _job_or_none(job_id) or {}
    own = lock is None
    try:
        if own:
            lock = PostLock(owner=_OWNERS.get(row['by'], 'dashboard'), job=job_id)
            if not _enter(lock):
                _end(p, row, 'stale', error='Another post started at the same moment. Nothing was posted; check again.',
                     expect=_OPEN)
                return job(job_id)
        else:
            lock.set_job(job_id)
        try:
            _post_locked(p, row, Path(root), caption, lock)
        finally:
            if own:
                lock.__exit__(None, None, None)
    except Exception as e:                                # a bug or a broken database: never raise from here
        try:
            now = _job_row(job_id)
            if now['state'] in _OPEN:
                if now['media_id']:
                    _end(p, now, 'done', record_pending=1, error=redact(str(e)))
                else:
                    _end(p, now, 'failed', kind='local', error=f'Unexpected error: {redact(str(e))}', expect=_OPEN)
        except Exception:
            pass
    return _job_or_none(job_id) or {}


def _enter(lock: PostLock) -> bool:
    """Take the lock; while it is held without a job (a record write), wait up to LOCK_WAIT s. False when busy."""
    end = time.monotonic() + LOCK_WAIT
    while True:
        try:
            lock.__enter__()
            return True
        except Busy as e:
            if (e.holder or {}).get('job') or time.monotonic() >= end:
                return False
            time.sleep(0.2)


def _auto_why(root: Path, p: Platform, loaded: S.Loaded | None = None) -> str | None:
    """Why an automatic job must not go on now (automatic posting turned off by the setting or the pause file,
    suspended, or the breaker open), else None. Read fresh each time: the owner can turn it off mid-post."""
    if not S.effective_auto(loaded or settings(root), p.name)[0]:
        return 'Automatic posting was turned off'
    st = _auto_state(p.name)
    if st and st['suspended']:
        return 'Automatic posting is suspended'
    if breaker(p.breaker)['state'] != 'closed':
        return 'Posting is stopped (breaker)'
    return None


def _post_locked(p: Platform, row, root: Path, caption: str, lock: PostLock | None = None) -> None:
    """run_post steps 2-6, with the PostLock held."""
    now = _utcnow()
    with _Con(write=True) as con:
        started = con.execute("UPDATE jobs SET state = 'running', started = ?, heartbeat = ?, result = ? WHERE id = ? "
                              "AND state = 'queued'", (S.iso(now), S.iso(now), _result('preparing', p.name), row['id']))
        if started.rowcount != 1:                         # settled by another process meanwhile
            return
    name = Path(row['item_key']).name
    audit(row['who'], 'post.started', f'{name}: posting started.', platform=p.name, item_key=row['item_key'], job=row['id'])
    v = _View(root, p, now)
    item = v.by_key.get(row['item_key'])
    if item is None or not _same(row, item):
        item = next((i for i in v.items if row['sig'] is not None and i.sig == row['sig']), None)
    why = _recheck(v, item, row, caption)
    if why:
        _end(p, row, 'stale', error=why, expect=_OPEN)
        return
    allowed = (lambda: _auto_why(root, p)) if row['by'] == 'auto' else None
    journal = Journal(row['id'], p.name, lock=lock, allowed=allowed)
    try:
        journal.stage('preparing')
        if _cover(p, item) is None:
            journal.log(f'{name}: making the cover first')
            p.prepare(item, journal.log)
        blocks = [c for c in p.preflight(item, caption, fresh=True) if c.level == 'block']
    except Stopped as e:
        _stop_job(p, row, e)
        return
    except Exception as e:
        blocks = [Check('local', 'block', redact(str(e)) or type(e).__name__)]
    if blocks:
        _end(p, row, 'failed', kind='local', error=blocks[0].text, expect=_OPEN)
        return
    journal.log(f'{name}: posting ({row["by"]}, {row["who"]})')
    try:
        res = p.post(item, caption, journal)
    except Exception as e:
        if journal.media_id:                              # live already: the record is all that is missing
            journal.log(f'{name}: error after the post went live: {redact(str(e))}')
            _live(p, row, item, journal, None)
        elif isinstance(e, Stopped):
            _stop_job(p, row, e)
        elif journal.container_id and _kind(p, e) not in CLEAR and _job_row(row['id'])['stage'] == 'publishing':
            _end(p, row, 'unknown', kind='unknown',                  # may be live: never sent again (spec 5.7)
                 error=(redact(str(e)) or type(e).__name__)[:500], expect=_OPEN)
        else:
            _failed(p, row, item, e)
        return
    if not journal.media_id:
        journal.published(res['id'])
    if res.get('permalink') and not journal.url:
        journal.permalink(res['permalink'])
    _live(p, row, item, journal, res)


def _stop_job(p: Platform, row, e: Stopped) -> None:
    """The job stopped before sending more (Journal.stage): 'stale', unless it was settled meanwhile."""
    if not _end(p, row, 'stale', error=f'Stopped before posting: {e}. Nothing was posted.', expect=_OPEN):
        log_line(p.name, f'{row["id"]} {Path(row["item_key"]).name}: stopped ({e}); the job was settled meanwhile')


def _recheck(v: _View, item: Item | None, row, caption: str) -> str | None:
    """Why the job must not post now (run_post step 3), or None. No network."""
    p = v.p
    if item is None:
        return f'The {p.noun} is not in the list any more.'
    if v.state(item) == 'posted':
        return f'The {p.noun} is already on {p.label}.'
    if v.unresolved:
        return 'An earlier post was interrupted. Check it first.'
    b = breaker(p.breaker)
    if b['state'] != 'closed':
        return f'Posting is stopped: {b["message"]}'
    n = S.posts_in_24h(v.hist, v.now)
    if n >= v.cfg['hard_cap_24h']:
        return f'{n} {p.noun}s in the last 24 hours: that is the limit.'
    if row['by'] == 'manual':
        if fingerprint(p.name, item, v.hist, v.now, _checks(v, item, caption, busy=False)) != row['fingerprint']:
            return 'Something changed since you opened this. Check again.'
    elif row['by'] == 'auto':
        why = _auto_why(v.root, p, v.loaded)
        if why:
            return f'{why}: nothing was posted.'
        ok, slot = S.slot_open(v.cfg, v.hist, v.now)
        if not ok or slot != row['slot']:
            return 'The automatic slot is not open any more.'
    else:
        warns = {c.code for c in S.spacing_checks(v.cfg, v.hist, v.now) if c.level == 'warn'}
        if not warns <= set(json.loads(row['override'] or '[]')):
            return 'The spacing changed since the check. Run it again.'
    return None


def _kind(p: Platform, exc: BaseException) -> str:
    try:
        kind = p.classify(exc)
    except Exception:
        kind = 'unknown'
    return kind if kind in KINDS else 'unknown'


def _failed(p: Platform, row, item: Item, exc: Exception) -> None:
    """The post failed before it went live: classify, trip the breaker or mark the item failed, end the job."""
    kind = _kind(p, exc)
    error = (redact(str(exc)) or type(exc).__name__)[:500]
    if kind in ('auth', 'permission', 'rate') and p.breaker:
        _trip(p.breaker, kind, error, row['who'])
    _end(p, row, 'failed', kind=kind, error=error, item_failed=error if kind == 'content' else None,
         queue_key=item.key if item else None, expect=_OPEN)


def _live(p: Platform, row, item: Item, journal: Journal, res: dict | None) -> None:
    """The post is live: save the Drive record, or keep it pending; the job ends done either way (S4, S5),
    even when another process settled it meanwhile (it went out after all)."""
    try:
        journal.stage('saving')
    except Exception:
        pass
    at = _time((res or {}).get('posted_utc')) or _time(_job_row(row['id'])['published']) or _utcnow()
    record = {'id': journal.media_id, 'permalink': (res or {}).get('permalink') or journal.url or '', **_stamp(at),
              'by': row['by'], 'job': row['id'], 'caption_sha': row['caption_sha'], 'reconciled': False}
    for k in ('posted', 'posted_utc'):
        if (res or {}).get(k):
            record[k] = res[k]
    try:
        if journal.lock is not None and not journal.lock.mine():     # R3: taken over; _write_pending saves it later
            raise OSError('the posting lock was taken over meanwhile')
        p.write_record(item, record)
        pending = 0
    except Exception as e:
        journal.log(f'{item.file.name}: the Drive record is not saved yet ({redact(str(e))})')
        pending = 1
    _end(p, row, 'done', record_pending=pending, record=json.dumps(record) if pending else None,
         url=record['permalink'] or None)
    _success(p)


def _success(p: Platform) -> None:
    """A post went out: the automatic failure count and the breaker's rate step go back to 0."""
    now = S.iso(_utcnow())
    with _Con(write=True) as con:
        con.execute('INSERT INTO auto_state (platform, failures) VALUES (?, 0) '
                    'ON CONFLICT(platform) DO UPDATE SET failures = 0', (p.name,))
        if p.breaker:
            con.execute("UPDATE breakers SET state = 'closed', until = NULL, step = 0 WHERE name = ? AND "
                        "(state = 'closed' OR (state = 'cooling' AND until <= ?))", (p.breaker, now))


def _end(p: Platform, row, state: str, *, kind: str | None = None, error: str | None = None,
         item_failed: str | None = None, record_pending: int = 0, record: str | None = None, url: str | None = None,
         audit_kind: str | None = None, who: str | None = None, expect: tuple | None = None,
         clear_failed: bool = False, queue_key: str | None = None) -> bool:
    """End a job (state, finished, kind, error, result) with one audit row (post.<state> unless audit_kind), plus
    'record.pending' when the record is pending. item_failed marks the item failed with that reason;
    clear_failed clears its Failed mark (queue_key: the item's key now, else the queue row with the job's sig).
    expect: the states the job must still be in (compare-and-set: two processes or two phones never both end
    it); returns False, changing nothing, when it is not."""
    name, label = Path(row['item_key']).name, p.label
    if state == 'done':
        result = f'Posted on {label}.' + (' The Drive record is saved later.' if record_pending else '')
        text = f'{name}: posted on {label}.'
    elif state == 'failed' and item_failed:
        result = f'Not posted. The {p.noun} is marked Failed; check the file, then Try again.'
        text = f'{name}: not posted: {error}'
    elif state == 'unknown':
        result = text = f'{name}: not clear whether it went out. Check {label}.'
    elif state == 'interrupted':
        result, text = 'The post was interrupted. Check it first.', f'{name}: the post was interrupted.'
    else:
        result, text = f'Not posted: {error}', f'{name}: not posted: {error}'
    now = S.iso(_utcnow())
    guard = f' AND state IN ({", ".join("?" * len(expect))})' if expect else ''
    with _Con(write=True) as con:
        cur = con.execute('UPDATE jobs SET state = ?, finished = ?, kind = ?, error = ?, result = ?, record_pending = ?, '
                          'record = ?, permalink = COALESCE(?, permalink) WHERE id = ?' + guard,
                          (state, now, kind, error, result, record_pending, record, url, row['id'], *(expect or ())))
        if cur.rowcount != 1:
            return False
        if item_failed or clear_failed:
            key = queue_key or _job_queue_key(con, p.name, row)
            old = con.execute('SELECT sig FROM queue WHERE platform = ? AND item_key = ?', (p.name, key)).fetchone()
            if queue_key or not (old and old['sig'] and row['sig'] and old['sig'] != row['sig']):  # not another video's
                _set_queue(con, p.name, key, row['sig'], **({'failed': item_failed, 'failed_at': now} if item_failed
                                                              else {'failed': None, 'failed_at': None}))
        _audit(con, who or row['who'], audit_kind or f'post.{state}', text, platform=p.name, item_key=row['item_key'],
               job=row['id'], data={'kind': kind, 'url': url, 'by': row['by']})
        if record_pending:
            _audit(con, who or row['who'], 'record.pending', f'{name}: the Drive record is not saved yet.',
                   platform=p.name, item_key=row['item_key'], job=row['id'])
    return True


# ---------- breaker and connection test ----------

_CLOSED = {'until': None, 'kind': None, 'message': None, 'since': None}


def breaker(name: str) -> dict:
    """{'name', 'state': 'closed' | 'cooling' | 'stopped', 'until', 'kind', 'message', 'since'}. A cooling
    breaker whose 'until' has passed reads as closed."""
    row = None
    if name:
        with _Con() as con:
            row = con.execute('SELECT * FROM breakers WHERE name = ?', (name,)).fetchone()
    if not row or row['state'] == 'closed' or (row['state'] == 'cooling' and
                                               (_time(row['until']) or _utcnow()) <= _utcnow()):
        return {'name': name, 'state': 'closed', **_CLOSED}
    return {'name': name, 'state': row['state'], 'until': row['until'], 'kind': row['kind'],
            'message': row['message'], 'since': row['since']}


def trip(name: str, kind: str, message: str) -> dict:
    """Open the breaker after an error: kind 'auth' or 'permission' -> stopped; 'rate' -> cooling for
    COOL_HOURS[step] hours, then step + 1. Audits 'breaker.open'. Returns breaker(name)."""
    return _trip(name, kind, message, 'dashboard')


def _sentence(text: str) -> str:
    text = ' '.join(str(text or '').split())[:300]
    return text if not text or text[-1] in '.!?' else text + '.'


def _trip(name: str, kind: str, message: str, who: str) -> dict:
    if not name or kind not in ('auth', 'permission', 'rate'):
        return breaker(name)
    now, message = _utcnow(), _sentence(redact(message))
    with _Con(write=True) as con:
        row = con.execute('SELECT * FROM breakers WHERE name = ?', (name,)).fetchone()
        if kind == 'rate' and row and row['state'] == 'stopped':
            return breaker(name)                          # a rate error never shortens or renames a stop
        step = row['step'] if row else 0
        if kind == 'rate':
            state, until, step = 'cooling', S.iso(now + timedelta(hours=COOL_HOURS[min(step, len(COOL_HOURS) - 1)])), step + 1
        else:
            state, until = 'stopped', None
        con.execute('INSERT INTO breakers (name, state, until, step, kind, message, since) VALUES (?, ?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT(name) DO UPDATE SET state = excluded.state, until = excluded.until, step = excluded.step, '
                    'kind = excluded.kind, message = excluded.message, since = excluded.since',
                    (name, state, until, step, kind, message, S.iso(now)))
        _audit(con, who, 'breaker.open', f'Posting stopped ({kind}): {message}', data={'name': name, 'state': state,
                                                                                         'until': until, 'step': step})
    return breaker(name)


def test_platform(platform_name: str, by: str) -> dict:
    """The POST /api/platform/test body {'ok', 'result', 'breaker', 'message'}. platform.test() runs at most
    once per TEST_EVERY per platform (else 429 rate_limited). Success closes a 'stopped' breaker (audit
    'breaker.closed'); a 'cooling' one (rate errors) keeps its time and step (S9: a read that works does not
    prove the posting limit is over). Failure raises PublishError(502, 'test_failed', kind=..., breaker=...)
    and trips the breaker for auth, permission or rate. Raises 409 not_set_up. Audits 'platform.test'."""
    p = platform(platform_name)
    if not p.set_up():
        raise PublishError(409, 'not_set_up', f'{p.label} is not set up on this PC.')
    now, key = _utcnow(), f'test:{p.name}'
    with _Con(write=True) as con:
        row = con.execute('SELECT at FROM stamps WHERE k = ?', (key,)).fetchone()
        last = _time(row['at']) if row else None
        if last and (now - last).total_seconds() < TEST_EVERY:
            raise PublishError(429, 'rate_limited', 'Wait a moment before testing again.',
                               retry_after=max(1, int(TEST_EVERY - (now - last).total_seconds() + 0.999)))
        con.execute('INSERT INTO stamps (k, at) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET at = excluded.at',
                    (key, S.iso(now)))
    try:
        result = p.test()
    except Exception as e:
        kind = _kind(p, e)
        msg = _sentence(redact(str(e)) or type(e).__name__)
        b = _trip(p.breaker, kind, msg, by)
        audit(by, 'platform.test', f'{p.label} connection test failed ({kind}): {msg}', platform=p.name,
              data={'kind': kind})
        text = f'{msg} Make a new key on the PC (instagram.py system-token).' if kind == 'auth' else msg
        raise PublishError(502, 'test_failed', text, kind=kind, breaker=b, retry_after=TEST_EVERY) from None
    with _Con(write=True) as con:
        if p.breaker and con.execute("SELECT 1 FROM breakers WHERE name = ? AND state = 'stopped'",
                                     (p.breaker,)).fetchone():
            con.execute("UPDATE breakers SET state = 'closed', until = NULL, step = 0, kind = NULL, message = NULL, "
                        'since = ? WHERE name = ?', (S.iso(now), p.breaker))
            _audit(con, by, 'breaker.closed', f'{p.label} answers again: posting allowed.', platform=p.name,
                   data={'name': p.breaker})
        _audit(con, by, 'platform.test', f'{p.label} connection test OK.', platform=p.name)
    b = breaker(p.breaker)
    if b['state'] == 'cooling':
        message = (f'{p.label} answers, but it asked for a pause (too many calls). Posting starts again after '
                   f'{S.fmt(_time(b["until"]))}.')
    else:
        message = f'{p.label} answers. Posting is allowed again.'
    return {'ok': True, 'result': result, 'breaker': b, 'message': message}


# ---------- reconcile ----------

def reconcile(root: Path, platform_name: str, api: bool = True, now: datetime | None = None) -> list[str]:
    """Settle jobs whose process went away and write pending records (spec 5.7). api=False only marks stale
    jobs interrupted: no network, no Drive write and no lock (it runs on every page read). Never raises; never
    publishes."""
    try:
        p = platform(platform_name)
    except Exception as e:
        return [f'{platform_name}: {redact(str(e))}']
    return _reconcile(Path(root) if root else None, p, api, _now(now), 'dashboard')


def _reconcile(root, p: Platform, api: bool, now: datetime, owner: str) -> list[str]:
    """reconcile() for a known platform; owner ('dashboard', 'watcher', 'cli') names the lock and audit rows."""
    lines = []
    for step in (_mark_interrupted,) + ((_write_pending, _settle) if api else ()):
        try:
            lines += step(root, p, now, owner)
        except Exception as e:
            lines.append(f'{p.label}: reconcile: {redact(str(e))}')
    return lines


def _mark_interrupted(root, p: Platform, now: datetime, owner: str) -> list[str]:
    """Running jobs silent for _JOB_STALE s and queued jobs older than that become interrupted, unless the
    lock belongs to that job."""
    cutoff = S.iso(now - timedelta(seconds=_JOB_STALE))
    with _Con() as con:
        rows = con.execute("SELECT * FROM jobs WHERE platform = ? AND ((state = 'running' AND "
                           'COALESCE(heartbeat, started, created) < ?) OR (state = ? AND created < ?)) ORDER BY created',
                           (p.name, cutoff, 'queued', cutoff)).fetchall()
    holder, lines = PostLock.holder() or {}, []
    for r in rows:
        if holder.get('job') == r['id']:
            continue
        with _Con(write=True) as con:
            cur = con.execute("UPDATE jobs SET state = 'interrupted', finished = ?, result = ? WHERE id = ? AND "
                              "state IN ('queued', 'running')", (S.iso(now), 'The post was interrupted. Check it first.', r['id']))
            if cur.rowcount:
                _audit(con, owner, 'post.interrupted', f'{Path(r["item_key"]).name}: the post was interrupted.',
                       platform=p.name, item_key=r['item_key'], job=r['id'])
                lines.append(f'{p.label}: {Path(r["item_key"]).name}: the post was interrupted (job {r["id"]}); '
                             'it is checked before anything else is posted.')
    return lines


def _record_of(r, reconciled: bool, media_id: str | None = None, permalink: str | None = None,
               at: datetime | None = None) -> dict:
    """The Drive record for a job's live post (spec 6.3)."""
    t = at or _time(r['published']) or _time(r['started']) or _time(r['created'])
    return {'id': media_id or r['media_id'], 'permalink': permalink if permalink is not None else (r['permalink'] or ''),
            **_stamp(t), 'by': r['by'], 'job': r['id'], 'caption_sha': r['caption_sha'], 'reconciled': reconciled}


def _has_real_record(item: Item | None) -> bool:
    """The item's Drive record has a real id (not a hand record)."""
    return bool(item and item.posted and item.posted.get('id') not in (None, '', 'hand'))


def _write_pending(root, p: Platform, now: datetime, owner: str) -> list[str]:
    """Write the records that are still pending, under the PostLock (skipped when it is busy): one try each,
    without the retry ladder (the lock is not held for long; the next round tries again). A video whose
    lesson folder was renamed is found by its sig. A hand record never replaces a real one."""
    with _Con() as con:
        rows = con.execute('SELECT * FROM jobs WHERE platform = ? AND record_pending = 1 ORDER BY created',
                           (p.name,)).fetchall()
    if not rows or root is None:
        return []
    try:
        lock = PostLock(owner=owner).__enter__()
    except Busy:
        return []
    lines = []
    try:
        items = p.candidates(root)[0]
        for r in rows:
            name, item = Path(r['item_key']).name, _match_item(items, r)
            if item is None:
                lines.append(f'{p.label}: {name}: the Drive record waits (the video is not in the list).')
                continue
            record = json.loads(r['record']) if r['record'] else _record_of(r, True)
            try:
                if not (record.get('id') == 'hand' and _has_real_record(item)):
                    p.write_record(item, record, retry=False)
            except Exception as e:
                lines.append(f'{p.label}: {name}: the Drive record is still not saved ({redact(str(e))}).')
                continue
            with _Con(write=True) as con:
                con.execute('UPDATE jobs SET record_pending = 0, item_key = ? WHERE id = ?', (item.key, r['id']))
                _audit(con, owner, 'record.written', f'{name}: Drive record saved.', platform=p.name,
                       item_key=item.key, job=r['id'])
            lines.append(f'{p.label}: {name}: the Drive record is saved now.')
    finally:
        lock.__exit__(None, None, None)
    return lines


def _match_item(items: list[Item], r) -> Item | None:
    """The candidate a job row is about: the same key and sig, else the same sig under another key."""
    item = next((i for i in items if i.key == r['item_key'] and _same(r, i)), None)
    if item is None and r['sig'] is not None:
        item = next((i for i in items if i.sig == r['sig']), None)
    return item


def _item_for(p: Platform, root: Path, r) -> Item | None:
    """_match_item over the current candidates; when the video moved (lesson folder renamed), the job row's
    item_key follows."""
    item = _match_item(p.candidates(Path(root))[0], r)
    if item is not None and item.key != r['item_key']:
        _update_job(r['id'], item_key=item.key)
    return item


def _settle(root, p: Platform, now: datetime, owner: str) -> list[str]:
    """Check the interrupted jobs, and the 'unknown' ones of the last UNKNOWN_FOR on their own too (at most
    once per UNKNOWN_EVERY s each), so a Reel that shows up late settles without a person."""
    with _Con() as con:
        rows = con.execute("SELECT * FROM jobs WHERE platform = ? AND (state = 'interrupted' OR (state = 'unknown' "
                           'AND created >= ?)) ORDER BY created, rowid', (p.name, S.iso(now - UNKNOWN_FOR))).fetchall()
    lines = []
    for r in rows:
        lines += _settle_one(root, p, r, now, owner)
    return lines


def _claim(r, now: datetime, every: int = RECONCILE_EVERY) -> bool:
    """One API check per job per `every` s, claimed in one UPDATE (the dashboard and the watcher never both
    check the same job)."""
    with _Con(write=True) as con:
        return con.execute('UPDATE jobs SET reconcile_at = ? WHERE id = ? AND (reconcile_at IS NULL OR reconcile_at <= ?)',
                           (S.iso(now), r['id'], S.iso(now - timedelta(seconds=every)))).rowcount == 1


def _last_sign(r) -> datetime | None:
    """The job's last sign of life (heartbeat, else start): an upload is judged SETTLE_WAIT s after it."""
    return _time(r['heartbeat']) or _time(r['started']) or _time(r['created'])


def _settle_one(root, p: Platform, r, now: datetime, owner: str, by: str | None = None) -> list[str]:
    """Settle one interrupted or unknown job by the spec 5.7 table (by: a person asked). Never publishes: a
    FINISHED container is never sent. Every ending is a compare-and-set on the state read, so a person's
    answer that came in meanwhile is never overwritten."""
    name, who, kind = Path(r['item_key']).name, by or owner, 'post.resolved' if by else None
    if r['media_id']:
        return _settle_live(root, p, r, _record_of(r, True), owner, who, kind)
    if not r['container_id']:
        if r['state'] != 'interrupted' or not _end(p, r, 'failed', kind='local', who=who, audit_kind=kind,
                                                   error='Interrupted before sending; nothing was posted',
                                                   expect=('interrupted',)):
            return []
        return [f'{p.label}: {name}: interrupted before sending; nothing was posted.']
    every = UNKNOWN_EVERY if r['state'] == 'unknown' and by is None else RECONCILE_EVERY
    if breaker(p.breaker)['state'] != 'closed' or not _claim(r, now, every):
        return []
    try:
        status = p.container_status(r['container_id'])
        media = p.recent_media(10) if status == 'PUBLISHED' else []
    except Exception as e:
        k = _kind(p, e)
        _trip(p.breaker, k, redact(str(e)), owner)
        return [f'{p.label}: {name}: could not check the post ({k}): {redact(str(e))}']
    _note_status(r['id'], status)
    if status == 'PUBLISHED':
        start = (_time(r['started']) or _time(r['created'])) - timedelta(seconds=60)
        m = next((m for m in media if _head(m.get('caption')) == (r['caption_head'] or '')
                  and _time(m.get('at')) and _time(m.get('at')) >= start), None)
        if m:
            rec = _record_of(r, True, media_id=str(m['id']), permalink=m.get('permalink') or '', at=_time(m['at']))
            return _settle_live(root, p, r, rec, owner, who, kind)
        if r['state'] == 'unknown':
            return [f'{p.label}: {name}: the upload says PUBLISHED; the {p.noun} is still not in the list.']
        if not _end(p, r, 'unknown', who=who, audit_kind=kind, expect=('interrupted',)):
            return []
        return [f'{p.label}: {name}: the upload says PUBLISHED, but no matching {p.noun} was found. Check {p.label}.']
    if status in ('FINISHED', 'IN_PROGRESS'):
        last = _last_sign(r)
        if last and now - last < timedelta(seconds=SETTLE_WAIT):
            return [f'{p.label}: {name}: the upload is {status}; it may still go out, so it is checked again later.']
        why = f'The post was interrupted before it went out; check {p.label}, then Try again.'
        line = f'{p.label}: {name}: interrupted before it went out ({status}); marked Failed, never sent later.'
        kind_end = 'local'
    elif status in ('ERROR', 'EXPIRED'):
        why = f'{p.label} could not process {name}: {status}'
        line, kind_end = f'{p.label}: {why}; marked Failed.', 'content' if status == 'ERROR' else 'transient'
    else:
        _update_job(r['id'], result=f'{p.label} answered {status!r} for the upload; the PC checks again later.')
        return [f'{p.label}: {name}: unexpected upload status {status!r}; checked again later.']
    item = _item_for(p, root, r) if root is not None else None
    if not _end(p, r, 'failed', kind=kind_end, error=why, item_failed=why, who=who, audit_kind=kind,
                queue_key=item.key if item else None, expect=(r['state'],)):
        return []
    return [line]


def _settle_live(root, p: Platform, r, record: dict, owner: str, who: str, kind: str | None) -> list[str]:
    """A settled job whose post is live: the record is written and the job ends done, all under the PostLock
    (a person's 'It is on Instagram' waits for it). The job ends done whatever a person said meanwhile: the
    post was found."""
    name = Path(r['item_key']).name
    if root is None:
        return []
    try:
        lock = PostLock(owner=owner).__enter__()
    except Busy:
        return [f'{p.label}: {name} is live; its record is written once the lock is free.']
    try:
        item = _item_for(p, root, r)
        try:
            if item is None:
                raise OSError('the video is not in the list')
            p.write_record(item, record, retry=False)
            pending = 0
        except Exception:
            pending = 1
        _update_job(r['id'], media_id=record['id'], published=S.iso(_time(record['posted_utc'])),
                    permalink=record['permalink'] or None)
        ended = _end(p, r, 'done', record_pending=pending, record=json.dumps(record) if pending else None,
                     url=record['permalink'] or None, who=who, audit_kind=kind or 'post.reconciled',
                     expect=tuple(s for s in ('queued', 'running', 'interrupted', 'unknown', 'failed', 'stale')))
    finally:
        lock.__exit__(None, None, None)
    if not ended:
        return []
    _success(p)
    return [f'{p.label}: {name}: the post went out; record {"pending" if pending else "saved"}.']


def resolve_job(root: Path, job_id: str, outcome: str, by: str) -> dict:
    """The POST /api/job/resolve body {'ok', 'job', 'message'}, for 'interrupted' or 'unknown' jobs only
    (else 409 not_unresolved). outcome:
    - 'check': reconcile this job now (429 rate_limited within RECONCILE_EVERY; 423 breaker);
    - 'posted': write a hand record for the item (kept pending in the job when the video is not in the list);
      the job ends done. 409 not_sent when nothing was sent (no upload);
    - 'not_posted': the job ends failed and the item goes back to waiting. Refused when the PC knows or may
      still learn that it went out: 409 posted (a media id), published (the upload answered PUBLISHED),
      check_first (an upload never checked), too_early (the upload may still be published).
    Every ending is a compare-and-set: a job settled meanwhile gives 409 not_unresolved. Audits 'post.resolved'."""
    r = _job_row(job_id)
    p = platform(r['platform'])
    if r['state'] not in _UNRESOLVED:
        raise PublishError(409, 'not_unresolved', 'This post is already settled.')
    if outcome not in ('check', 'posted', 'not_posted'):
        raise PublishError(400, 'bad_request', 'Unknown outcome.')
    root, now = Path(root), _utcnow()
    if outcome == 'check':
        return _check_job(root, p, r, now, by)
    if outcome == 'not_posted':
        if r['media_id']:
            raise PublishError(409, 'posted', f'This {p.noun} went out: {p.label} gave it an id.')
        if r['last_status'] == 'PUBLISHED':
            raise PublishError(409, 'published', f'{p.label} reported this upload as published, so the {p.noun} is '
                               f"on {p.label}. Choose 'It is on {p.label}'.")
        if r['container_id'] and not r['checked']:
            raise PublishError(409, 'check_first', f'Press Check again first: the PC has not asked {p.label} about '
                               'this upload yet.')
        ready = (_last_sign(r) or now) + timedelta(seconds=SETTLE_WAIT)
        if r['last_status'] in ('FINISHED', 'IN_PROGRESS') and now < ready:
            raise PublishError(409, 'too_early', f'{p.label} may still publish it. Check again after {S.fmt(ready)}.')
        if not _end(p, r, 'failed', kind=r['kind'] or 'unknown', error='Marked as not posted.', who=by,
                    audit_kind='post.resolved', expect=(r['state'],), clear_failed=True):
            raise PublishError(409, 'not_unresolved', 'This post is already settled.')
        _update_job(job_id, result=f'Marked as not posted: the {p.noun} is back in the queue.')
        return {'ok': True, 'job': job(job_id), 'message': f'Saved: the {p.noun} is back in the queue.'}
    if not r['container_id'] and not r['media_id']:
        raise PublishError(409, 'not_sent', f'Nothing was sent to {p.label} for this post, so it is not there. '
                           f"Choose 'It is not on {p.label}'.")
    with PostLock(owner='dashboard'):                    # a running check finishes first; then this row is fresh
        r = _job_row(job_id)
        if r['state'] not in _UNRESOLVED:
            raise PublishError(409, 'not_unresolved', 'This post is already settled.')
        item = _item_for(p, root, r)
        record = _record_of(r, True) if r['media_id'] else {
            'id': 'hand', 'permalink': r['permalink'] or '', **_stamp(_time(r['started']) or _time(r['created'])),
            'by': 'hand', 'job': r['id']}
        pending = 1 if item is None else 0
        if item is not None and not _has_real_record(item):
            try:
                p.write_record(item, record)
            except OSError:
                raise PublishError(503, 'write_failed', 'Drive did not take the record. Try again.') from None
        if not _end(p, r, 'done', url=record['permalink'] or None, who=by, audit_kind='post.resolved',
                    record_pending=pending, record=json.dumps(record) if pending else None, expect=(r['state'],)):
            raise PublishError(409, 'not_unresolved', 'This post is already settled.')
    later = ' Its Drive record is saved when the video is in the list again.' if pending else ''
    return {'ok': True, 'job': job(job_id), 'message': f'Saved: the {p.noun} counts as posted.{later}'}


def _check_job(root: Path, p: Platform, r, now: datetime, by: str) -> dict:
    """resolve_job 'check'."""
    b = breaker(p.breaker)
    if b['state'] != 'closed':
        raise PublishError(423, 'breaker', _stopped(b), breaker=b)
    last = _time(r['reconcile_at'])
    if last and (now - last).total_seconds() < RECONCILE_EVERY:
        raise PublishError(429, 'rate_limited', 'Wait a moment before checking again.',
                           retry_after=max(1, int(RECONCILE_EVERY - (now - last).total_seconds() + 0.999)))
    lines = _settle_one(root, p, r, now, 'dashboard', by=by)
    row, j = _job_row(r['id']), job(r['id'])
    fresh = row['checked'] is not None and row['checked'] != r['checked']
    later = f'The PC checks again by itself in {RECONCILE_EVERY // 60} minutes.'
    if j['state'] == 'done':
        message = f'Checked: the {p.noun} is on {p.label}. Record {"saved later" if j["record_pending"] else "saved"}.'
    elif j['state'] == 'failed' and not r['container_id']:
        message = f'Checked: nothing was posted. The {p.noun} is back in the queue.'
    elif j['state'] == 'failed':
        message = f'Checked: the {p.noun} did not go out. It is marked Failed; Try again when ready.'
    elif fresh and row['last_status'] == 'PUBLISHED':
        message = (f'Checked: {p.label} says the upload was published, but the {p.noun} is not in its list yet. '
                   f"Look in the app; if it is there, choose 'It is on {p.label}'. The PC also checks again by itself.")
    elif fresh and row['last_status'] in ('FINISHED', 'IN_PROGRESS'):
        ready = (_last_sign(row) or now) + timedelta(seconds=SETTLE_WAIT)
        message = f'Checked: {p.label} has not published the upload yet; it may still go out. Check again after {S.fmt(ready)}.'
    elif fresh:                                          # a status the PC does not know (G27): say what it was
        message = f'Checked: {p.label} answered {row["last_status"]!r} for the upload, which the PC does not know. {later}'
    else:
        m = re.search(r'could not check the post \(\w+\): (.+)$', lines[0]) if lines else None
        message = f'Could not check right now{": " + m.group(1).rstrip(".") if m else ""}. {later}'
    body = {'ok': True, 'job': j, 'message': message}
    if j['state'] in _UNRESOLVED:
        body['retry_after'] = RECONCILE_EVERY            # the next Check again is allowed then (429 before)
    return body


# ---------- the automatic step ----------

def auto_step(root: Path, log=print, now: datetime | None = None) -> list[str]:
    """The watcher's automatic step for every set-up platform (spec 5.5). Returns one line per thing that
    happened; each line is also written with log_line. Never raises. Makes no network call unless a slot is
    open or an interrupted job needs checking."""
    lines = []
    try:
        plats = registry()
    except Exception as e:
        return [f'publish: {redact(str(e))}']
    for name, p in plats.items():
        try:
            if p.set_up():
                lines += _auto_one(Path(root), p, log, _now(now))
        except Exception as e:
            line = f'{p.label}: automatic step failed: {redact(str(e))}'
            log_line(name, line)
            lines.append(line)
    return lines


def _auto_one(root: Path, p: Platform, log, now: datetime) -> list[str]:
    out = []

    def say(line):
        log_line(p.name, line)
        out.append(line)
    for line in _reconcile(root, p, True, now, 'watcher'):                         # 1
        say(line)
    if unresolved_jobs(p.name):                                                     # 2
        return out
    loaded, st = settings(root), _auto_state(p.name)
    if breaker(p.breaker)['state'] != 'closed' or (st and st['suspended']) or not S.effective_auto(loaded, p.name)[0]:
        return out                                                                  # 3
    cfg = S.platform_settings(loaded, p.name)
    if S.slot_check(cfg, [], now) in ('day', 'start', 'window'):                    # 4: no Drive scan then (G17)
        return out
    ok, slot = S.slot_open(cfg, history(root, p.name), now)
    if not ok:
        return out
    with _Con() as con:
        if len(_attempt_times(con, p.name, now)) >= ATTEMPTS_24H:                   # 5
            return out
        row = con.execute('SELECT * FROM slots WHERE platform = ? AND slot = ?', (p.name, slot)).fetchone()
    if row and (row['attempts'] >= SLOT_ATTEMPTS or (row['attempts'] == 1 and not (                  # 6
            row['last_kind'] in ('transient', 'local') and (now - _time(row['last_at'])).total_seconds() >= RETRY_AFTER))):
        return out
    try:
        lock = PostLock(owner='watcher').__enter__()                                # 7
    except Busy as e:
        log_line(p.name, f'{p.label}: the posting lock is busy ({(e.holder or {}).get("owner") or "?"}); '
                 'the next run tries again.')
        return out
    try:
        _auto_locked(root, p, log, now, slot, lock, say)
    finally:
        lock.__exit__(None, None, None)
    return out


def _auto_locked(root: Path, p: Platform, log, now: datetime, slot: str, lock: PostLock, say) -> None:
    """auto_step steps 8-12, with the PostLock held."""
    while True:
        v = _View(root, p, now)                                                     # 8
        ok, again = S.slot_open(v.cfg, v.hist, now)
        if not ok or again != slot:
            return
        wait = v.waiting()
        if not wait:
            _missed(p, slot, say)
            return
        item = wait[0]
        caption, _ = caption_for(root, item)
        try:                                                                        # 9
            blocks = [c for c in p.preflight(item, caption, fresh=True) if c.level == 'block']
        except Exception as e:
            blocks = [Check('local', 'block', redact(str(e)) or type(e).__name__)]
        unread = [c for c in blocks if _unreadable(c)]
        row = v.row(item)
        if unread and not (row and row['unread'] and (now - _time(row['unread'])).total_seconds() >= RETRY_AFTER):
            if not (row and row['unread']):                  # Drive may not serve the file for a moment
                with _Con(write=True) as con:
                    _set_queue(con, p.name, item.key, item.sig, bump=False, unread=S.iso(now))
            say(f'{p.label}: {item.file.name} could not be read ({unread[0].text}); tried again in the next run, '
                'marked Failed if it still cannot be read 30 min later.')
            return
        dup = None if blocks else v.duplicate(item, caption)
        if dup:
            blocks = [Check('duplicate', 'block', f'A {p.noun} with this caption is already on {p.label} '
                            f'({S.fmt(_time(dup))}); check it, then Try again to post it anyway.')]
        if blocks:
            with _Con(write=True) as con:
                _set_queue(con, p.name, item.key, item.sig, failed=blocks[0].text, failed_at=S.iso(now), unread=None)
                _audit(con, 'watcher', 'item.failed', f'{item.file.name}: marked Failed: {blocks[0].text}',
                       platform=p.name, item_key=item.key)
            say(f'{p.label}: {item.file.name} marked Failed before posting: {blocks[0].text}')
            continue
        if row and row['unread']:
            with _Con(write=True) as con:
                _set_queue(con, p.name, item.key, item.sig, bump=False, unread=None)

        def count(con):                                                             # 10
            con.execute('INSERT INTO slots (platform, slot, attempts, last_at) VALUES (?, ?, 1, ?) ON CONFLICT(platform, '
                        'slot) DO UPDATE SET attempts = attempts + 1, last_at = excluded.last_at', (p.name, slot, S.iso(now)))
        try:
            job_id = _new_job(p, item, 'auto', 'watcher', caption, now, slot=slot, before=count)
        except PublishError as e:
            say(f'{p.label}: not posted: {e.message}')
            return
        say(f'{p.label}: posting {item.file.name} (automatic, slot {slot})')
        try:
            warn = p.key_warning(now)                    # at most once a day: one automatic post a day (F52)
        except Exception:
            warn = None
        if warn:
            say(f'{p.label}: {warn}')
        _auto_outcome(p, run_post(job_id, root, caption, lock=lock), slot, now, say)      # 11, 12
        return


def _unreadable(c: Check) -> bool:
    """A preflight block that may be Drive not serving the file for a moment (missing, or not readable), not a
    problem of the video itself (length, caption)."""
    return c.code in ('file_missing', 'local') or (c.code == 'length' and 'seconds' not in c.data)


def _auto_outcome(p: Platform, j: dict, slot: str, now: datetime, say) -> None:
    """auto_step step 12: failures and suspension (a done job already reset the failures)."""
    name = Path(j.get('key') or '').name
    if j.get('state') == 'done':
        say(f'{p.label}: {name} posted {j.get("url") or "(no link yet)"}')
        return
    if j.get('state') == 'unknown':                                  # may be live: no "not posted" here
        say(f'{p.label}: {j.get("result")}')
        return
    if j.get('state') != 'failed':
        say(f'{p.label}: {name} not posted: {j.get("error")}')
        return
    with _Con(write=True) as con:
        con.execute('UPDATE slots SET last_kind = ? WHERE platform = ? AND slot = ?', (j['kind'], p.name, slot))
        con.execute('INSERT INTO auto_state (platform, failures) VALUES (?, 1) '
                    'ON CONFLICT(platform) DO UPDATE SET failures = failures + 1', (p.name,))
        n = con.execute('SELECT failures FROM auto_state WHERE platform = ?', (p.name,)).fetchone()['failures']
        stop = n >= SUSPEND_AFTER and con.execute(
            'UPDATE auto_state SET suspended = ?, suspended_why = ? WHERE platform = ? AND suspended IS NULL',
            (S.iso(now), j['error'], p.name)).rowcount == 1
        if stop:
            _audit(con, 'watcher', 'auto.suspended', f'{p.label}: automatic posting suspended after {n} failed tries '
                   f'in a row: {j["error"]}', platform=p.name, data={'failures': n})
    say(f'{p.label}: {name} not posted ({j["kind"]}): {j["error"]}')
    if stop:
        say(f'{p.label}: automatic posting is SUSPENDED after {n} failed tries; resume it in the dashboard.')


def _missed(p: Platform, slot: str, say) -> None:
    """P1: a slot opened while nothing was waiting: one 'auto.missed' row per slot."""
    with _Con(write=True) as con:
        st = con.execute('SELECT missed_checked FROM auto_state WHERE platform = ?', (p.name,)).fetchone()
        if st and st['missed_checked'] and st['missed_checked'] >= slot:
            return
        con.execute('INSERT INTO auto_state (platform, missed_checked) VALUES (?, ?) '
                    'ON CONFLICT(platform) DO UPDATE SET missed_checked = excluded.missed_checked', (p.name, slot))
        text = f'{p.label}: the slot of {S.fmt_day(S.at_local(slot, "12:00"))} passed without a {p.noun}: none was waiting.'
        _audit(con, 'watcher', 'auto.missed', text, platform=p.name, data={'slot': slot})
    say(text)


# ---------- media from the stats collector ----------

def _int(x) -> int | None:
    return x if type(x) is int else None


def note_media(root: Path | None, platform_name: str, media: list[dict]) -> int:
    """Called by the stats collector with read_stats()['media']. Updates the media cache (time, permalink,
    caption head, product, likes, comments). With root and the PostLock (skipped when busy), it also fills
    empty permalinks in records whose id matches, and (P1) replaces a hand record's id 'hand' with the real
    id (and link) when the permalink shortcode matches, or, for a hand record without a link, when one media
    has the post's caption head (its job's, else the item's caption) and a time near the record's (G15).
    Returns how many records changed. Never raises; no network."""
    try:
        p = platform(platform_name)
        rows = [m for m in media or () if isinstance(m, dict) and m.get('id') and _time(m.get('at'))]
        seen = S.iso(_utcnow())
        with _Con(write=True) as con:
            for m in rows:
                con.execute('INSERT INTO media (platform, id, at, permalink, caption_head, product, likes, comments, seen) '
                            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(platform, id) DO UPDATE SET at = excluded.at, '
                            'permalink = COALESCE(excluded.permalink, permalink), caption_head = excluded.caption_head, '
                            'product = excluded.product, likes = excluded.likes, comments = excluded.comments, '
                            'seen = excluded.seen',
                            (p.name, str(m['id']), S.iso(_time(m['at'])), m.get('permalink') or None, _head(m.get('caption')),
                             m.get('product'), _int(m.get('likes')), _int(m.get('comments')), seen))
        if root is None:
            return 0
        by_id = {str(m['id']): m for m in rows}
        by_code = {_code(m.get('permalink')): m for m in rows if _code(m.get('permalink'))}
        todo = []
        items = p.candidates(Path(root))[0]
        used = {str((i.posted or {}).get('id')) for i in items}
        for item in items:
            rec = item.posted or {}
            m = by_id.get(str(rec.get('id')))
            if m and not rec.get('permalink') and m.get('permalink'):
                todo.append((item, {**rec, 'permalink': m['permalink']}, 'link'))
            elif rec.get('id') == 'hand' and _code(rec.get('permalink')) in by_code:
                todo.append((item, {**rec, 'id': str(by_code[_code(rec['permalink'])]['id'])}, 'id'))
            elif rec.get('id') == 'hand' and not _code(rec.get('permalink')):
                m = _hand_match(root, item, rec, [m for m in rows if str(m['id']) not in used])
                if m:
                    used.add(str(m['id']))
                    todo.append((item, {**rec, 'id': str(m['id']), 'permalink': m.get('permalink') or ''}, 'id'))
        if not todo:
            return 0
        try:
            lock = PostLock(owner='dashboard').__enter__()
        except Busy:
            return 0
        changed = 0
        try:
            for item, rec, what in todo:
                try:
                    p.write_record(item, rec, retry=False)
                except Exception:
                    continue
                changed += 1
                audit('dashboard', 'record.written', f'{item.file.name}: Drive record updated with the {p.label} {what}.',
                      platform=p.name, item_key=item.key)
        finally:
            lock.__exit__(None, None, None)
        return changed
    except Exception as e:
        print(f'publish: note_media: {redact(str(e))}')
        return 0


def _hand_match(root: Path, item: Item, rec: dict, media: list[dict]) -> dict | None:
    """The one media that is this hand record's post: the same caption head and a time near the post's: from
    60 s before to 2 h after its job's start (a resolved job), else within 2 h of the record's time with the
    item's caption now (Marked as posted). None when there is none or more than one."""
    head, t, before = None, _time(rec.get('posted_utc')), timedelta(hours=2)
    try:
        j = _job_row(rec['job']) if rec.get('job') else None
    except PublishError:
        j = None
    if j is not None:
        head, t, before = j['caption_head'], _time(j['started']) or _time(j['created']) or t, timedelta(seconds=60)
    else:
        head = _head(caption_for(root, item)[0])
    if not head or t is None:
        return None
    found = [m for m in media if _head(m.get('caption')) == head and
             t - before <= _time(m.get('at')) <= t + timedelta(hours=2)]
    return found[0] if len(found) == 1 else None


# ---------- page bodies ----------

def state(root: Path, platform_name: str, now: datetime | None = None, with_queue: bool = True) -> dict:
    """The GET /api/publish body (spec 8.3). with_queue=False leaves out 'queue' and 'forecast' (Home).
    Local only."""
    p, root, now = platform(platform_name), Path(root), _now(now)
    _reconcile(root, p, False, now, 'dashboard')
    v = _View(root, p, now, strict=False)
    auto = _auto(root, p, now, v.loaded, v)
    cap = next((c for c in S.spacing_checks(v.cfg, v.hist, now) if c.code == 'hard_cap'), None)
    wait = v.waiting()
    counts, lessons = {'waiting': len(wait), 'skipped': 0, 'failed': 0, 'posted': 0}, {}
    for i in v.items:
        st = v.state(i)
        if st != 'waiting':
            counts['failed' if st == 'checking' else st] += 1
    for i in wait:
        lessons[i.lesson] = lessons.get(i.lesson, 0) + 1
    counts['by_lesson'] = [{'lesson': k, 'waiting': n} for k, n in lessons.items()]
    body = {'platform': p.name, 'label': p.label, 'noun': p.noun, 'verb': p.verb, 'setup_hint': p.setup_hint,
            'platforms': [{'name': n, 'label': x.label} for n, x in registry().items()], 'set_up': p.set_up(),
            'account_url': p.account_url or None, 'now': S.iso(now),
            'settings': v.cfg, 'settings_rev': v.loaded.doc.get('rev'), 'settings_problems': list(v.loaded.problems),
            'presets': [{'id': k, 'label': d['label'], 'values': {f: x for f, x in d.items() if f != 'label'}}
                        for k, d in S.PRESETS.items()],
            'auto': auto, 'breaker': breaker(p.breaker), 'last_post': v.last_post(),
            'posts_24h': S.posts_in_24h(v.hist, now), 'next_possible': cap.data.get('next_possible') if cap else None,
            'counts': counts}
    if with_queue:
        body['forecast'] = _forecast(root, p, v.cfg, not _will_post(auto), v)
    body['job'] = current_job(p.name)
    body['unresolved'] = [_job_json(j) for j in v.unresolved]       # interrupted or unknown, oldest first
    if with_queue:
        body['queue'] = _queue(v)
    return body


def next_run(root: Path, now: datetime | None = None, views: dict | None = None) -> list[dict]:
    """One entry per set-up platform for STATUS.txt and Home: {'platform', 'label', 'effective', 'reason',
    'at', 'slot', 'waiting', 'text', 'footer'}. text is e.g. 'Instagram Reel: Thu 5 Nov after 20:00 · new:
    <title>' or 'Instagram: automatic posting OFF (6 waiting)'; footer is schedule.summary() or 'Instagram:
    automatic posting is off; Reels are posted from the dashboard.'. An open breaker or an unresolved post
    (they stop manual posts too) is named whatever the setting. views: {name: _View} from views() to reuse
    (one Drive scan per request). Never raises: an error becomes the text."""
    out = []
    try:
        plats = registry()
    except Exception as e:
        return [{'platform': None, 'label': '', 'effective': False, 'reason': 'error', 'at': None, 'slot': None,
                 'waiting': 0, 'text': f'Publishing: {redact(str(e))}', 'footer': ''}]
    for name, p in plats.items():
        entry = {'platform': name, 'label': p.label, 'effective': False, 'reason': 'error', 'at': None, 'slot': None,
                 'waiting': 0, 'text': '', 'footer': ''}
        try:
            if not p.set_up():
                continue
            t = _now(now)
            v = (views or {}).get(name) or _View(Path(root), p, t, strict=False)
            auto = _auto(root, p, t, v.loaded, v)
            wait, nxt = v.waiting(), auto['next'] or {}
            n, at = len(wait), _time(nxt.get('at'))
            blk = 'breaker' if breaker(p.breaker)['state'] != 'closed' else 'unresolved' if v.unresolved else None
            if auto['effective'] and at:
                text = f'{p.label} {p.noun}: {_after(at)} · {f"{wait[0].lesson}: {wait[0].title}" if wait else ""}'
            else:
                text = {'off': f'{p.label}: automatic posting OFF ({n} waiting)',
                        'stopped_by_file': f'{p.label}: automatic posting OFF ({n} waiting)',
                        'invalid': f'{p.label}: automatic posting OFF ({n} waiting)',
                        'suspended': f'{p.label}: automatic posting SUSPENDED ({n} waiting)',
                        'breaker': f'{p.label}: posting stopped: {breaker(p.breaker)["message"]}',
                        'unresolved': f'{p.label}: an interrupted post needs a check ({n} waiting)',
                        'empty': f'{p.label}: no {p.noun}s waiting'}.get(
                    blk or auto['reason'], f'{p.label}: no automatic slot in the next {S.SEARCH_DAYS} days ({n} waiting)')
            if blk:                                       # blocks manual posts too (G14): never 'posted from the dashboard'
                footer = (f'{p.label}: nothing is posted until the interrupted post is checked on the dashboard.'
                          if blk == 'unresolved' else f'{p.label}: nothing is posted until posting starts again '
                          '(see the dashboard).')
            elif auto['effective']:
                footer = S.summary(v.cfg, p.noun)
            else:
                footer = f'{p.label}: automatic posting is off; {p.noun}s are posted from the dashboard.'
            entry.update(effective=auto['effective'], reason=auto['reason'], at=nxt.get('at'), slot=nxt.get('slot'),
                         waiting=n, text=text, footer=footer)
        except Exception as e:
            entry['text'] = f'{p.label}: {redact(str(e))}'
        out.append(entry)
    return out


def _card(cid: str, level: str, who: str, title: str, text: str, action: dict | None = None) -> dict:
    return {'id': cid, 'level': level, 'who': who, 'title': title, 'text': text, 'action': action}


def views(root: Path, now: datetime | None = None) -> dict:
    """{platform name: one read of its queue state} for every platform, to share between attention(),
    next_run() and queue() in one request (one Drive scan each, not three; G17). Page bodies only."""
    t = _now(now)
    return {name: _View(Path(root), p, t, strict=False) for name, p in registry().items()}


def attention(root: Path, now: datetime | None = None, views: dict | None = None) -> list[dict]:
    """The publishing attention items (spec 9.2, rules 4-11, and P1 rule 14) in the Attention shape. views:
    see views()."""
    now, out, given, views = _now(now), [], views, {}
    try:
        loaded = settings(root)
        for name, p in registry().items():
            views[name] = (given or {}).get(name) or _View(Path(root), p, now, strict=False)
    except Exception as e:
        return [_card('publish_error', 'red', 'pc', 'Publishing could not be read', redact(str(e)))]
    link = lambda name: {'kind': 'link', 'label': 'Open Publish', 'href': f'#publish/{name}'}
    for name, v in views.items():                                                   # 4
        for j in v.unresolved:
            out.append(_card(f'post_unresolved:{j["id"]}', 'red', 'anyone', 'A post was interrupted',
                             f'{Path(j["item_key"]).name}: check whether it went out. Nothing else is posted '
                             'until then.', link(name)))
    for name, v in views.items():                                                   # 5
        ended = [j for j in v.jobs if j['state'] in ('done', 'failed') and j['finished']]
        last = max(ended, key=lambda j: j['finished'], default=None)
        if last and last['state'] == 'failed' and _time(last['finished']) > now - _DAY:
            out.append(_card(f'post_failed:{last["id"]}', 'red', 'anyone', f'The last {v.p.noun} was not posted',
                             f'{Path(last["item_key"]).name}: {last["error"]}', link(name)))
    done = set()
    for name, v in views.items():                                                   # 6
        b = breaker(v.p.breaker)
        if v.p.breaker and b['state'] != 'closed' and v.p.breaker not in done:
            done.add(v.p.breaker)
            out.append(_card(f'breaker:{v.p.breaker}', 'red', 'pc', 'Posting is stopped', _stopped(b),
                             {'kind': 'call', 'label': 'Test connection', 'endpoint': '/api/platform/test',
                              'body': {'platform': name}, 'confirm': None, 'ack': None}
                             if b['state'] == 'stopped' else None))   # cooling: it ends on its own (S9)
    for name, v in views.items():                                                   # 7
        st = _auto_state(name)
        if st and st['suspended']:
            out.append(_card(f'auto_suspended:{name}', 'red', 'anyone', 'Automatic posting is suspended',
                             f'After {SUSPEND_AFTER} failed tries in a row: {st["suspended_why"]}', link(name)))
    if loaded.problems:                                                             # 8
        out.append(_card('settings_invalid', 'red', 'pc', 'The settings file has a problem',
                         ' '.join(loaded.problems) + ' Automatic posting is off until it is reset.',
                         {'kind': 'call', 'label': 'Reset to defaults', 'endpoint': '/api/settings/reset', 'body': {},
                          'confirm': 'Reset every publishing setting to the defaults? Automatic posting will be off.',
                          'ack': 'reset_settings'}))
    for name, v in views.items():                                                   # 9
        for j in v.jobs:
            if j['record_pending']:
                out.append(_card(f'record_pending:{j["id"]}', 'yellow', 'pc', 'A record is not saved yet',
                                 f'{Path(j["item_key"]).name} is on {v.p.label}, but its Drive record is not saved yet. '
                                 'The PC tries again.'))
    for name, v in views.items():                                                   # F7: Failed items wait
        bad = [i for i in v.items if v.state(i) == 'failed']
        if bad:
            out.append(_card(f'items_failed:{name}', 'yellow', 'anyone', f'{len(bad)} {v.p.noun}'
                             f'{"s are" if len(bad) != 1 else " is"} marked Failed',
                             f'{bad[0].file.name}: {v.row(bad[0])["failed"]} Failed {v.p.noun}s are never posted '
                             'automatically: check them in Publish, then Try again.', link(name)))
    for name, v in views.items():                                                   # 10, 11
        n, on = len(v.waiting()), S.effective_auto(v.loaded, name)
        if not on[0] and on[1] in ('off', 'stopped_by_file') and n:
            out.append(_card(f'auto_off_waiting:{name}', 'yellow', 'anyone', f'Automatic {v.p.noun}s are off',
                             f'{n} {v.p.noun}{"s are" if n != 1 else " is"} waiting. Post one from Publish, or turn '
                             'automatic posting on.', link(name)))
    for name, v in views.items():
        n = len(v.waiting())
        if n <= 2:
            rows = _forecast(root, v.p, v.cfg, True, v)['rows']
            until = f' (until {S.fmt_day(_time(rows[-1]["at"]))})' if rows else ''
            text = (f'Only {n} {v.p.noun}{"s" if n != 1 else ""} left{until}. Record the next lesson.' if n else
                    f'No {v.p.noun}s left. Record the next lesson.')
            out.append(_card(f'queue_low:{name}', 'yellow', 'sinem', f'Few {v.p.noun}s left', text))
    with _Con() as con:                                                             # 14 (P1)
        rows = con.execute("SELECT * FROM audit WHERE kind = 'auto.missed' AND at >= ? ORDER BY id",
                           (S.iso(now - 2 * _DAY),)).fetchall()
    for r in rows:
        slot = json.loads(r['data'] or '{}').get('slot')
        if r['platform'] in views and slot:
            out.append(_card(f'auto_missed:{r["platform"]}:{slot}', 'yellow', 'pc', 'A slot passed without a post',
                             r['text']))
    return out


def cli_post(lesson: Path, dry: bool = False, force: bool = False, log=print) -> str:
    """instagram.py post "<lesson>": the lesson's first waiting Reel in queue order, through the same rules
    and lock, synchronously, by 'cli' (root = lesson.parent). Refuses, returning the reason as text, on block
    checks, the hard cap, an open breaker or an unresolved job. Warn checks refuse unless force.
    dry: return what would be posted (item, cover, caption, checks); no network."""
    lesson = Path(lesson)
    root = lesson.parent
    try:
        p = platform('instagram')
        if not p.set_up():
            return f'{p.label} is not set up on this PC.'
        now = _utcnow()
        _reconcile(root, p, False, now, 'cli')
        v = _View(root, p, now)
        want = os.path.normcase(lesson.name)                 # Windows folder names: any case, as in v1 (F50)
        mine = [i for i in v.waiting() if os.path.normcase(i.lesson) == want]
        if not mine:
            return f'No waiting {p.noun} in {lesson.name}.'
        item = mine[0]
        caption, _ = caption_for(root, item)
        checks = _checks(v, item, caption)
        blocks, warns = [c for c in checks if c.level == 'block'], [c for c in checks if c.level == 'warn']
        if dry:
            lines = [f'Would post: {item.key} ({item.seconds} s)', f'Cover: {_cover(p, item) or "none yet (made first)"}',
                     'Caption:', caption, 'Checks:'] + [f'  {c.level}: {c.text}' for c in checks]
            return '\n'.join(lines + ([] if checks else ['  none']))
        if blocks:
            return f'Not posted: {blocks[0].text}'
        if warns and not force:
            return 'Not posted: ' + ' '.join(c.text for c in warns) + ' Use --force to post anyway.'
        with _Con() as con:
            if len(_attempt_times(con, p.name, now)) >= ATTEMPTS_24H:
                return f'Not posted: {ATTEMPTS_24H} tries in the last 24 hours. Try again later.'
        job_id = _new_job(p, item, 'cli', 'cli', caption, now, override=sorted(c.code for c in warns))
        try:
            log(f'Posting {item.file.name}…')
        except Exception:                                 # a console that cannot show the text: post anyway
            pass
        j = run_post(job_id, root, caption)
        if j.get('state') == 'done':
            later = ' (the Drive record is saved later)' if j.get('record_pending') else ''
            return f'Posted {item.file.name}: {j.get("url") or "(no link yet)"}{later}'
        if j.get('state') == 'unknown':                              # may be live: check before posting again
            return f'{j.get("result")} Settle it in the dashboard (Publish) before posting again.'
        return f'Not posted: {j.get("error") or j.get("result")}'
    except PublishError as e:
        return f'Not posted: {e.message}'
    except Exception as e:
        return f'Not posted: {redact(str(e))}'


def _wait(job_id: str, timeout: float = 10.0) -> dict:
    """Wait until a job has ended (tests and --dev): join its thread, then return the Job object."""
    t = _threads.get(job_id)
    if t:
        t.join(timeout)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        j = job(job_id)
        if j['state'] not in _OPEN:
            return j
        time.sleep(0.02)
    return job(job_id)
