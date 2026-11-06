"""Channel numbers over time in WORK/stats.db: YouTube (Data API: the channel plus the pipeline's videos) and
each platform's account and media counts (Platform.read_stats). The dashboard server calls collect() from one
background thread; nothing else here uses the network. Imports publish and schedule; youtube lazily."""
from __future__ import annotations
import json, os, re, sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import paths
import publish
import schedule as S

WORK = paths.WORK
DB = WORK / 'stats.db'
OLD_CACHE = WORK / 'dashboard-stats.json'
EVERY = {'youtube': 3300, 'instagram': 3300, 'instagram_limit': 21600, 'instagram_permissions': 86400}
BACKOFF = (3600, 10800, 21600)

_DB0, _OLD0 = DB, OLD_CACHE
_LONGEST = timedelta(hours=26)              # longer than any wait a rule sets (24 h; a Pacific day can last 25 h)
_IG, _BREAKER = 'instagram', 'meta'
_META = ('instagram', 'instagram_limit', 'instagram_permissions')    # Meta reads: none while the breaker is open
_CHANNEL = (('subscribers', 'subscriberCount'), ('views', 'viewCount'), ('videos', 'videoCount'))
_VIDEO = (('views', 'viewCount'), ('likes', 'likeCount'), ('comments', 'commentCount'))
_ACCOUNT = (('followers', 'followers'), ('follows', 'follows'), ('media', 'media_count'))
_TILES = {'yt_subscribers': ('YouTube subscribers', 'youtube', 'subscribers'),
          'yt_views': ('YouTube views', 'youtube', 'views'),
          'ig_followers': ('Instagram followers', 'instagram', 'followers'),
          'ig_likes': ('Instagram likes', 'instagram', 'likes')}        # ig_likes: the sum over the cached media
_COLS = ('title', 'url', 'published', 'privacy', 'lesson', 'file')
_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_HAND = re.compile(r'https://youtu\.be/([\w-]{11})')                  # a hand-made lesson's note (as dashboard.items)
_QUOTA = "YouTube's daily limit is used up"
_HAS_INSIGHTS = 'The key has instagram_manage_insights. Reach and plays are not collected in this build.'
_NO_INSIGHTS = ('Reach and plays need the instagram_manage_insights permission, which this key does not have. '
                'Make a new system-user key with instagram_manage_insights ticked, then run instagram.py '
                'system-token on the PC.')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY, last_ok TEXT, last_try TEXT,
  fails INTEGER NOT NULL DEFAULT 0, next_try TEXT, last_error TEXT) STRICT;
CREATE TABLE IF NOT EXISTS fetches (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, at TEXT NOT NULL,
  ok INTEGER NOT NULL, calls INTEGER NOT NULL, error TEXT) STRICT;
CREATE TABLE IF NOT EXISTS objects (platform TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT, url TEXT,
  published TEXT, privacy TEXT, lesson TEXT, file TEXT, updated TEXT NOT NULL, PRIMARY KEY (platform, id)) STRICT;
CREATE TABLE IF NOT EXISTS samples (platform TEXT NOT NULL, id TEXT NOT NULL, metric TEXT NOT NULL, at TEXT NOT NULL,
  value INTEGER NOT NULL, PRIMARY KEY (platform, id, metric, at)) STRICT;
CREATE INDEX IF NOT EXISTS samples_at ON samples(at);
CREATE TABLE IF NOT EXISTS features (name TEXT PRIMARY KEY, value TEXT NOT NULL, at TEXT NOT NULL) STRICT;
INSERT OR IGNORE INTO meta (k, v) VALUES ('schema', '1');
"""


# ---------- database ----------

def _db() -> Path:
    """DB, or WORK/stats.db when only WORK was moved (tests), as publish._db() does."""
    return DB if DB != _DB0 else WORK / DB.name


def _old_cache() -> Path:
    return OLD_CACHE if OLD_CACHE != _OLD0 else WORK / OLD_CACHE.name


def connect() -> sqlite3.Connection:
    """A new connection to stats.db (schema of spec 6.2 on first use): WAL, busy_timeout 10 s, rows as Row.
    A damaged file is moved aside first (_set_aside) and a new one is made."""
    try:
        return _open(_db())
    except sqlite3.DatabaseError as e:
        if not _set_aside(e):
            raise
    return _open(_db())


def _broken(e: BaseException) -> bool:
    """True for 'file is not a database' and 'database disk image is malformed'; never for busy or locked."""
    code = getattr(e, 'sqlite_errorcode', None)
    return isinstance(e, sqlite3.DatabaseError) and code is not None and code & 0xFF in (sqlite3.SQLITE_NOTADB,
                                                                                          sqlite3.SQLITE_CORRUPT)


def _set_aside(e: BaseException | None) -> bool:
    """When e says stats.db is damaged: rename stats.db (and its -wal and -shm) to stats.db.corrupt-<epoch>...,
    so the next connect() starts a new file (only the history is lost; the old file stays for a look). Returns
    True when moved. A file still held open (Windows) is left alone: the next failing use tries again."""
    if e is None or not _broken(e):
        return False
    path, stamp = _db(), int(publish._utcnow().timestamp())
    try:
        for side in ('-wal', '-shm', ''):                # the side files first: never an old WAL next to a new file
            p = Path(f'{path}{side}')
            if p.exists():
                p.rename(p.with_name(f'{path.name}.corrupt-{stamp}{side}'))
    except OSError:
        return False
    print(f'stats: {path.name} was damaged ({_short(e)}); moved aside as {path.name}.corrupt-{stamp}, '
          'a new one starts', flush=True)
    return True


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA busy_timeout = 10000')
        con.execute('PRAGMA journal_mode = WAL')
        con.execute('PRAGMA synchronous = NORMAL')
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'features'").fetchone():
            con.executescript(_SCHEMA)          # a new (or emptied) file: every statement is idempotent
    except BaseException:
        con.close()
        raise
    return con


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
            except BaseException as e:
                self.con.close()
                _set_aside(e)
                raise
        return self.con

    def __exit__(self, et, ev, tb):
        try:
            if self.write and self.con.in_transaction:
                self.con.execute('ROLLBACK' if et else 'COMMIT')
        except BaseException as e:
            ev = e
            raise
        finally:
            self.con.close()
            _set_aside(ev)                               # a damaged page that only a query found


def _object(con, platform: str, oid: str, kind: str, at: str, *, keep: bool = False, **cols) -> None:
    """Insert or update one object. A None detail never overwrites a known one; keep=True leaves a row alone."""
    change = 'NOTHING' if keep else ('UPDATE SET kind = excluded.kind, ' + ', '.join(
        f'{c} = COALESCE(excluded.{c}, {c})' for c in _COLS) + ', updated = excluded.updated')
    con.execute(f'INSERT INTO objects (platform, id, kind, {", ".join(_COLS)}, updated) VALUES ({", ".join("?" * 10)}) '
                f'ON CONFLICT(platform, id) DO {change}', (platform, oid, kind, *(cols.get(c) for c in _COLS), at))


def _sample(con, platform: str, oid: str, metric: str, at: str, value: int | None, *, keep: bool = False) -> int:
    """Store one number (None is skipped). Returns 1 when a row was written."""
    if value is None:
        return 0
    verb = 'INSERT OR IGNORE' if keep else 'INSERT OR REPLACE'
    return con.execute(f'{verb} INTO samples (platform, id, metric, at, value) VALUES (?, ?, ?, ?, ?)',
                       (platform, oid, metric, at, value)).rowcount


def _latest(con, platform: str, metrics: tuple[str, ...], t: str | None = None) -> dict[str, sqlite3.Row]:
    """{object id: its objects row plus, per metric, the newest value at or before t (any time when t is None)}."""
    cond = ' AND s.at <= :t' if t else ''
    cols = ''.join(f", (SELECT s.value FROM samples s WHERE s.platform = o.platform AND s.id = o.id AND s.metric = '{m}'"
                   f"{cond} ORDER BY s.at DESC LIMIT 1) AS {m}" for m in metrics)
    return {r['id']: r for r in con.execute(f'SELECT o.*{cols} FROM objects o WHERE o.platform = :p ORDER BY o.id',
                                            {'p': platform, 't': t})}


# ---------- small helpers ----------

def _num(x) -> int | None:
    """A count from an API answer or a file: an int (not a bool) or a string of digits; else None."""
    if type(x) is int:
        return x
    return int(x) if isinstance(x, str) and x.isascii() and x.isdigit() else None


def _iso(x) -> str | None:
    t = publish._time(x)
    return S.iso(t) if t else None


def _text(x) -> str | None:
    return x if isinstance(x, str) else None


def _title(caption) -> str | None:
    """A caption's first non-empty line, whitespace collapsed, at most 60 characters."""
    line = next((' '.join(x.split()) for x in str(caption or '').splitlines() if x.strip()), '')
    return (line if len(line) <= 60 else line[:59].rstrip() + '…') or None


def _jload(p: Path) -> dict:
    try:
        data = json.loads(Path(p).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _short(e: BaseException) -> str:
    """A one-line reason for the Stats source line: the message in plain words (publish.plain: Google's raw
    errors as one sentence), secrets hidden, at most 160 characters; the error's type when it has no message."""
    msg = publish.plain(publish.redact(' '.join(str(e).split())), 160)
    return msg or type(e).__name__


# ---------- the old cache, the pipeline's YouTube ids ----------

def import_old_cache(path: Path = OLD_CACHE) -> int:
    """Once (meta 'old_cache_imported'): dashboard-stats.json's numbers become objects and samples at its
    '_at' time. Returns the number of samples added (0 when already imported or missing). Leaves the file."""
    path = _old_cache() if Path(path) == _OLD0 else Path(path)      # the default follows a moved WORK (tests)
    try:
        data, mtime = json.loads(path.read_text(encoding='utf-8')), path.stat().st_mtime
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    t = data.get('_at')
    try:                                                 # no usable '_at': the time the file was last written
        at = datetime.fromtimestamp(t if type(t) in (int, float) and t > 0 else mtime, timezone.utc)
    except (OverflowError, OSError, ValueError):
        at = datetime.fromtimestamp(mtime, timezone.utc)
    stamp, added = S.iso(at), 0
    with _Con(write=True) as con:
        if con.execute("SELECT 1 FROM meta WHERE k = 'old_cache_imported'").fetchone():
            return 0
        for vid, v in data.items():
            if vid.startswith('_') or not isinstance(v, dict) or not _ID.fullmatch(vid):
                continue
            _object(con, 'youtube', vid, 'video', stamp, keep=True, title=_text(v.get('title')),
                    url=f'https://youtu.be/{vid}', published=_iso(v.get('published')), privacy=_text(v.get('privacy')))
            added += sum(_sample(con, 'youtube', vid, m, stamp, _num(v.get(m)), keep=True)
                         for m in ('views', 'likes', 'comments'))
        ch = data.get('_channel')
        if isinstance(ch, dict):
            _object(con, 'youtube', '_account', 'account', stamp, keep=True)
            added += sum(_sample(con, 'youtube', '_account', m, stamp, _num(ch.get(m)), keep=True)
                         for m, _ in _CHANNEL)
        con.execute("INSERT INTO meta (k, v) VALUES ('old_cache_imported', ?)", (S.iso(publish._utcnow()),))
    return added


def youtube_ids(root: Path) -> dict[str, dict]:
    """{video id: {'lesson', 'file', 'kind': 'short' | 'video'}} from every youtube-applied.json, plus the
    youtu.be link of hand-made lessons (status.json 'youtube' note when youtube_complete). Local only."""
    found = {}
    for p in sorted(Path(root).glob('*/*/extras/youtube-applied.json')):
        out, lesson = p.parents[1], p.parents[2].name
        if lesson.startswith(('_', '.')):
            continue
        for name, rec in _jload(p).items():
            vid = str(rec.get('id') or '') if isinstance(rec, dict) else ''
            if _ID.fullmatch(vid) and vid not in found:
                found[vid] = {'lesson': lesson, 'file': name, 'kind': _kind(out / name)}
    for p in sorted(Path(root).glob('*/*/extras/status.json')):
        out, lesson, st = p.parents[1], p.parents[2].name, _jload(p)
        m = _HAND.search(str(st.get('youtube') or '')) if st.get('youtube_complete') else None
        if not m or m.group(1) in found or lesson.startswith(('_', '.')):
            continue
        applied = _jload(p.with_name('youtube-applied.json'))
        video = next((f for f in sorted(out.glob('*-video.mp4')) if f.name not in applied), None)
        if video:                                        # the note belongs to the lesson video, as in dashboard.items
            found[m.group(1)] = {'lesson': lesson, 'file': video.name, 'kind': _kind(video)}
    return found


def _kind(f: Path) -> str:
    """'short' for a vertical video of at most 180 s (dashboard.items' rule), else 'video'. A file that cannot be
    probed is judged by its name (-short, -short-N)."""
    try:
        p = publish.probe_cached(f)
        return 'short' if p['height'] > p['width'] and round(p['duration']) <= 180 else 'video'
    except Exception:
        return 'short' if re.search(r'-short(-\d+)?$', f.stem) else 'video'


# ---------- collecting ----------

def due(source: str, now: datetime) -> bool:
    """True when the source never ran or its next_try has passed. After a success: next_try = now + EVERY.
    After the n-th error in a row: now + BACKOFF[min(n, 3) - 1]. After YouTube quotaExceeded: the next Pacific
    midnight. Meta sources are never due while publish.breaker('meta') is open."""
    if source not in EVERY:
        raise ValueError(f'unknown stats source {source!r}')
    if source in _META and publish.breaker(_BREAKER)['state'] != 'closed':
        return False
    with _Con() as con:
        row = con.execute('SELECT next_try FROM sources WHERE source = ?', (source,)).fetchone()
    return not _waits(row, publish._now(now))


def _waits(row, now: datetime) -> bool:
    """True while the source's next_try is still ahead. One more than 26 h ahead (no rule waits that long) came
    from a wrong clock, and does not count."""
    t = publish._time(row['next_try']) if row else None
    return t is not None and now < t <= now + _LONGEST


def collect(root: Path | None, now: datetime | None = None, *, youtube=None) -> dict[str, str]:
    """Read every due source and store samples. Returns {source: 'ok' | 'skipped' | 'error: <short>'}.
    youtube: an object like youtube.api() (tests, --dev); by default youtube.api() is imported lazily.
    Instagram comes from publish.platform('instagram').read_stats(); its media also go to
    publish.note_media(root, ...). root None skips the YouTube video reads. Errors never trip a breaker.
    Never raises."""
    now, out = publish._now(now), {}
    for source in EVERY:
        try:
            out[source] = _collect(source, root, now, youtube)
        except Exception as e:                           # the background thread keeps going
            out[source] = f'error: {_short(e)}'
    return out


def _collect(source: str, root, now: datetime, youtube) -> str:
    if not due(source, now):
        return 'skipped'
    p = None
    if source in _META:
        try:
            p = publish.platform(_IG)
        except publish.PublishError:
            return 'skipped'
        if not p.set_up():
            return 'skipped'
    if not _claim(source, now):
        return 'skipped'
    calls = [0]
    try:
        if source == 'youtube':
            _read_youtube(root, now, youtube, calls)
        elif source == 'instagram':
            _read_instagram(p, root, now, calls)
        elif source == 'instagram_limit':
            _read_limit(p, now, calls)
        else:
            _read_permissions(p, now, calls)
    except Exception as e:
        kind = publish._kind(p, e) if p is not None else None
        return f'error: {_failed(source, now, calls[0], e, kind)}'
    return 'ok'


def _claim(source: str, now: datetime) -> bool:
    """Take the source for this read (last_try = now, next_try = now + EVERY), unless another reader took it
    after due() said yes. So two threads or processes never read one source twice."""
    with _Con(write=True) as con:
        if _waits(con.execute('SELECT next_try FROM sources WHERE source = ?', (source,)).fetchone(), now):
            return False
        con.execute('INSERT INTO sources (source, last_try, next_try) VALUES (?, ?, ?) ON CONFLICT(source) DO UPDATE '
                    'SET last_try = excluded.last_try, next_try = excluded.next_try',
                    (source, S.iso(now), S.iso(now + timedelta(seconds=EVERY[source]))))
    return True


def _ok(con, source: str, now: datetime, calls: int) -> None:
    at = S.iso(now)
    con.execute('INSERT INTO sources (source, last_ok, last_try, fails, next_try, last_error) VALUES (?, ?, ?, 0, ?, NULL) '
                'ON CONFLICT(source) DO UPDATE SET last_ok = excluded.last_ok, last_try = excluded.last_try, fails = 0, '
                'next_try = excluded.next_try, last_error = NULL',
                (source, at, at, S.iso(now + timedelta(seconds=EVERY[source]))))
    con.execute('INSERT INTO fetches (source, at, ok, calls, error) VALUES (?, ?, 1, ?, NULL)', (source, at, calls))
    if source in _META:
        con.execute("DELETE FROM features WHERE name = 'meta_auth'")      # Meta took the key again


def _failed(source: str, now: datetime, calls: int, e: BaseException, kind: str | None = None) -> str:
    """Store a failed read: the n-th error in a row waits BACKOFF[min(n, 3) - 1]; YouTube quotaExceeded waits
    until the next Pacific midnight (and at least EVERY). Never trips a breaker (S17); an 'auth' error (kind,
    publish's classification) is kept as the feature 'meta_auth' for key_refused(). Returns the short reason."""
    quota = source == 'youtube' and 'quotaExceeded' in str(e)
    why = _QUOTA if quota else _short(e)
    with _Con(write=True) as con:
        row = con.execute('SELECT fails FROM sources WHERE source = ?', (source,)).fetchone()
        n = (row['fails'] if row else 0) + 1
        wait = now + timedelta(seconds=BACKOFF[min(n, len(BACKOFF)) - 1])
        if quota:
            wait = max(S.pacific_midnight_after(now), now + timedelta(seconds=EVERY[source]))
            import youtube as Y                          # the shared note: the watcher and Upload wait too
            Y.quota_used_up(now)
        con.execute('INSERT INTO sources (source, last_try, fails, next_try, last_error) VALUES (?, ?, ?, ?, ?) '
                    'ON CONFLICT(source) DO UPDATE SET last_try = excluded.last_try, fails = excluded.fails, '
                    'next_try = excluded.next_try, last_error = excluded.last_error',
                    (source, S.iso(now), n, S.iso(wait), why))
        con.execute('INSERT INTO fetches (source, at, ok, calls, error) VALUES (?, ?, 0, ?, ?)',
                    (source, S.iso(now), calls, why))
        if kind == 'auth':
            _feature(con, 'meta_auth', why, now)
    return why


def key_refused() -> str | None:
    """Why Meta last refused the key in a numbers read (an 'auth' error), or None since a read worked again."""
    with _Con() as con:
        row = con.execute("SELECT value FROM features WHERE name = 'meta_auth'").fetchone()
    return row['value'] if row else None


def clear_key_refused() -> None:
    """Forget key_refused() (a connection test passed)."""
    with _Con(write=True) as con:
        con.execute("DELETE FROM features WHERE name = 'meta_auth'")


def _read_youtube(root, now: datetime, yt, calls: list) -> None:
    """One YouTube read (about 2 units): the pipeline's videos, 50 per call (none when root is None), then the
    channel. All or nothing: when a call fails, nothing is stored."""
    import youtube as Y
    yt = yt if yt is not None else Y.api()
    known = youtube_ids(root) if root is not None else {}
    ids, videos = sorted(known), []
    for k in range(0, len(ids), 50):
        calls[0] += 1
        videos += yt.videos().list(part='snippet,statistics,status', id=','.join(ids[k:k + 50])).execute().get('items', [])
    calls[0] += 1
    ch = yt.channels().list(part='statistics', id=Y.CHANNEL_ID).execute()['items'][0]['statistics']
    at = S.iso(now)
    with _Con(write=True) as con:
        _object(con, 'youtube', '_account', 'account', at)
        for metric, key in _CHANNEL:
            _sample(con, 'youtube', '_account', metric, at, _num(ch.get(key, 0)))
        for v in videos:
            vid, sn, st = str(v['id']), v.get('snippet') or {}, v.get('statistics') or {}
            info = known.get(vid, {})
            _object(con, 'youtube', vid, info.get('kind', 'video'), at, title=_text(sn.get('title')),
                    url=f'https://youtu.be/{vid}', published=_iso(sn.get('publishedAt')),
                    privacy=_text((v.get('status') or {}).get('privacyStatus')), lesson=info.get('lesson'),
                    file=info.get('file'))
            for metric, key in _VIDEO:
                _sample(con, 'youtube', vid, metric, at, _num(st.get(key, 0)))
        _ok(con, 'youtube', now, calls[0])


def _posted(root) -> tuple[dict, dict]:
    """({media id: (lesson, file)}, {permalink shortcode: (lesson, file)}) from the platform's Drive records."""
    by_id, by_code = {}, {}
    if root is None:
        return by_id, by_code
    try:
        found, _ = publish.items(root, _IG)
    except Exception:
        return by_id, by_code
    for item in found:
        rec, where = item.posted or {}, (item.lesson, item.file.name)
        code = publish._code(rec.get('permalink'))
        if rec.get('id'):
            by_id[str(rec['id'])] = where
        if code:
            by_code[code] = where
    return by_id, by_code


def _read_instagram(p, root, now: datetime, calls: list) -> None:
    """One read_stats() call: the account's counts and the newest media (matched to lessons through the Drive
    records), then publish.note_media."""
    calls[0] += 1
    data = p.read_stats()
    media = [m for m in data.get('media') or () if isinstance(m, dict) and m.get('id')]
    by_id, by_code = _posted(root)
    at = S.iso(now)
    with _Con(write=True) as con:
        _object(con, _IG, '_account', 'account', at)
        for metric, key in _ACCOUNT:
            _sample(con, _IG, '_account', metric, at, _num(data.get(key)))
        for m in media:
            mid = str(m['id'])
            lesson, file = by_id.get(mid) or by_code.get(publish._code(m.get('permalink'))) or (None, None)
            _object(con, _IG, mid, 'reel' if str(m.get('product') or '').upper() == 'REELS' else 'post', at,
                    title=_title(m.get('caption')), url=_text(m.get('permalink')) or None, published=_iso(m.get('at')),
                    lesson=lesson, file=file)
            for metric in ('likes', 'comments'):
                _sample(con, _IG, mid, metric, at, _num(m.get(metric)))
        _ok(con, 'instagram', now, calls[0])
    publish.note_media(root, _IG, media)


def _feature(con, name: str, value: str, now: datetime) -> None:
    con.execute('INSERT OR REPLACE INTO features (name, value, at) VALUES (?, ?, ?)', (name, value, S.iso(now)))


def _read_limit(p, now: datetime, calls: list) -> None:
    calls[0] += 1
    d = p.read_limit()
    used, total = _num(d.get('used')), _num(d.get('total'))
    if used is None or total is None:
        raise ValueError('the publishing limit answer has no used/total numbers')
    with _Con(write=True) as con:
        _feature(con, 'instagram_limit', json.dumps({'used': used, 'total': total}), now)
        _ok(con, 'instagram_limit', now, calls[0])


def _read_permissions(p, now: datetime, calls: list) -> None:
    calls[0] += 1
    names = set(p.read_permissions() or ())
    with _Con(write=True) as con:
        _feature(con, 'instagram_insights', 'granted' if 'instagram_manage_insights' in names else 'missing', now)
        _ok(con, 'instagram_permissions', now, calls[0])


# ---------- reading the numbers ----------

def youtube_cache() -> dict:
    """The latest YouTube numbers in the old dashboard-stats.json shape, for dashboard.items():
    {<video id>: {'views', 'likes', 'comments', 'privacy', 'title', 'published'},
     '_channel': {'subscribers', 'views', 'videos'}, '_at': epoch seconds of the last good read, or 0}."""
    with _Con() as con:
        rows = _latest(con, 'youtube', ('views', 'likes', 'comments', 'subscribers', 'videos'))
        src = con.execute("SELECT last_ok FROM sources WHERE source = 'youtube'").fetchone()
        last = src['last_ok'] if src else None
        if not last:                                     # never read yet: the imported old numbers' time
            row = con.execute("SELECT at FROM samples INDEXED BY samples_at WHERE platform = 'youtube' "
                              'ORDER BY at DESC LIMIT 1').fetchone()
            last = row[0] if row else None
    out = {}
    for vid, r in rows.items():
        v = {k: r[k] for k in ('views', 'likes', 'comments', 'privacy', 'title', 'published')}
        if vid != '_account' and None not in v.values():
            out[vid] = v
    acc = rows.get('_account')
    out['_channel'] = {m: acc[m] for m, _ in _CHANNEL if acc[m] is not None} if acc else {}
    t = publish._time(last)
    out['_at'] = int(t.timestamp()) if t else 0
    return out


def _value(con, tile: str, t: str) -> tuple[int | None, str | None]:
    """(value, sample time) of a tile at time t: the newest sample at or before t. ig_likes adds up each
    medium's newest likes (its time is the newest of those samples)."""
    if tile == 'ig_likes':
        pick = ("FROM samples s WHERE s.platform = o.platform AND s.id = o.id AND s.metric = 'likes' AND s.at <= :t "
                'ORDER BY s.at DESC LIMIT 1')
        row = con.execute(f'SELECT SUM(v), MAX(a) FROM (SELECT (SELECT s.value {pick}) AS v, (SELECT s.at {pick}) AS a '
                          "FROM objects o WHERE o.platform = 'instagram' AND o.id != '_account')", {'t': t}).fetchone()
    else:
        _, platform, metric = _TILES[tile]
        row = con.execute("SELECT value, at FROM samples WHERE platform = ? AND id = '_account' AND metric = ? "
                          'AND at <= ? ORDER BY at DESC LIMIT 1', (platform, metric, t)).fetchone()
    return (row[0], row[1]) if row and row[0] is not None else (None, None)


def _spark(con, tile: str, now: datetime) -> list:
    """14 Istanbul days, oldest first: each day's last value (samples up to now), None for a day without one."""
    _, platform, metric = _TILES[tile]
    today, out = date.fromisoformat(S.local_date(now)), []
    for k in range(13, -1, -1):
        lo = S.at_local((today - timedelta(days=k)).isoformat(), '00:00')
        span = (S.iso(lo), S.iso(min(lo + timedelta(days=1, seconds=-1), now)))
        if tile == 'ig_likes':          # samples_at: one day of rows (the planner alone would read every Instagram row)
            row = con.execute("SELECT at FROM samples INDEXED BY samples_at WHERE platform = 'instagram' AND id != '_account' "
                              "AND metric = 'likes' AND at >= ? AND at <= ? ORDER BY at DESC LIMIT 1", span).fetchone()
            out.append(_value(con, tile, row[0])[0] if row else None)
        else:
            row = con.execute("SELECT value FROM samples WHERE platform = ? AND id = '_account' AND metric = ? AND at >= ? "
                              'AND at <= ? ORDER BY at DESC LIMIT 1', (platform, metric, *span)).fetchone()
            out.append(row[0] if row else None)
    return out


def _stale(at: str | None, source: str, now: datetime) -> bool:
    """True when a number read at `at` is older than two of its source's EVERY (a read or more was missed)."""
    t = publish._time(at)
    return t is not None and now - t > timedelta(seconds=2 * EVERY[source])


def tiles(now: datetime | None = None) -> list[dict]:
    """The four tiles yt_subscribers, yt_views, ig_followers and ig_likes (sum over the cached media), each
    {'id', 'label', 'value', 'd24', 'd7', 'spark', 'at', 'stale'}. d24/d7 = the change in the last 24 h / 7 days
    (rolling, not since midnight): value now minus the newest sample at or before now - 24 h / 7 d; None when
    there is none, or when no number was read after that moment (unknown, not 'no change'). stale: the value
    is older than two reads (_stale): show its time. spark = 14 Istanbul days, oldest first: each day's last
    value, None where unknown."""
    now = publish._now(now)
    out = []
    with _Con() as con:
        for tile, (label, source, _) in _TILES.items():
            value, at = _value(con, tile, S.iso(now))
            deltas = []
            for d in (timedelta(hours=24), timedelta(days=7)):
                start = S.iso(now - d)
                old = _value(con, tile, start)[0]
                deltas.append(None if value is None or old is None or at <= start else value - old)
            out.append({'id': tile, 'label': label, 'value': value, 'd24': deltas[0], 'd7': deltas[1],
                        'spark': _spark(con, tile, now), 'at': at, 'stale': _stale(at, source, now)})
    return out


def summary(root: Path | None, now: datetime | None = None) -> dict:
    """The GET /api/stats body (spec 8.3). A video's d24_views is None when its views were not read in the
    last 24 h (unknown, not 0)."""
    now = publish._now(now)
    t, start = S.iso(now), S.iso(now - timedelta(hours=24))
    with _Con() as con:
        yt = _latest(con, 'youtube', ('views', 'likes', 'comments'), t)
        day = _latest(con, 'youtube', ('views',), start)
        read = {r['id']: r['at'] for r in con.execute(     # each video's newest views time
            "SELECT o.id, (SELECT s.at FROM samples s WHERE s.platform = o.platform AND s.id = o.id AND s.metric = 'views' "
            "AND s.at <= ? ORDER BY s.at DESC LIMIT 1) AS at FROM objects o WHERE o.platform = 'youtube'", (t,))}
        ig = _latest(con, _IG, ('likes', 'comments'), t)
        feats = {r['name']: r for r in con.execute('SELECT name, value, at FROM features')}
    videos = []
    for vid, r in yt.items():
        if vid == '_account' or (r['views'], r['likes'], r['comments']) == (None, None, None):
            continue                                     # no number yet at `now`
        old = day[vid]['views'] if vid in day else None
        fresh = (read.get(vid) or '') > start            # read in the last 24 h; else the change is unknown
        videos.append({'id': vid, 'title': r['title'], 'kind': r['kind'], 'published': r['published'],
                       'views': r['views'], 'likes': r['likes'], 'comments': r['comments'],
                       'd24_views': None if r['views'] is None or old is None or not fresh else r['views'] - old,
                       'thumb': f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg', 'url': r['url'] or f'https://youtu.be/{vid}'})
    videos.sort(key=lambda v: (v['views'] is None, -(v['views'] or 0), v['title'] or '', v['id']))
    reels = [{'id': mid, 'at': r['published'], 'title': r['title'], 'likes': r['likes'], 'comments': r['comments'],
              'url': r['url'], 'lesson': r['lesson'], 'file': r['file']}
             for mid, r in ig.items() if r['kind'] == 'reel' and (r['likes'], r['comments']) != (None, None)]
    reels.sort(key=lambda r: (r['at'] or '', r['id']), reverse=True)
    return {'now': t, 'sources': sources(), 'tiles': tiles(now), 'youtube_videos': videos, 'reels': reels,
            'pairs': _pairs(videos, yt, reels), 'insights': _insights(feats.get('instagram_insights')),
            'limit': _limit(feats.get('instagram_limit'), now)}


def _pairs(videos: list[dict], yt: dict, reels: list[dict]) -> list[dict]:
    """The same file on YouTube and as a Reel (joined on lesson and file name), newest Reel first."""
    on_yt = {}
    for v in sorted(videos, key=lambda v: yt[v['id']]['updated']):     # the newest object wins for a file
        r = yt[v['id']]
        if r['lesson'] and r['file']:
            on_yt[(r['lesson'], r['file'])] = v
    out, seen = [], set()
    for reel in reels:
        key = (reel['lesson'], reel['file'])
        v = on_yt.get(key)
        if v and key not in seen:
            seen.add(key)
            out.append({'lesson': key[0], 'file': key[1],
                        'youtube': {'views': v['views'], 'likes': v['likes'], 'comments': v['comments'], 'url': v['url']},
                        'instagram': {'likes': reel['likes'], 'comments': reel['comments'], 'url': reel['url']}})
    return out


def _insights(row) -> dict | None:
    if not row:
        return None                                      # never checked
    ok = row['value'] == 'granted'
    return {'available': ok, 'checked': row['at'], 'text': _HAS_INSIGHTS if ok else _NO_INSIGHTS}


def _limit(row, now: datetime) -> dict | None:
    """{'used', 'total', 'at', 'stale'}: Meta's 24 h posting count as read at `at`; stale when older than two
    reads (12 h), so the page shows its time. None before the first good read."""
    try:
        d = json.loads(row['value']) if row else None
        return {'used': d['used'], 'total': d['total'], 'at': row['at'],
                'stale': _stale(row['at'], 'instagram_limit', now)} if d else None
    except (ValueError, KeyError, TypeError):
        return None


def sources() -> list[dict]:
    """[{'source', 'last_ok', 'last_try', 'next_try', 'fails', 'error'}] for the health line and Stats."""
    with _Con() as con:
        rows = {r['source']: r for r in con.execute('SELECT * FROM sources')}
    out = []
    for s in EVERY:                                      # every source, also one that never ran (nulls)
        r = rows.get(s)
        out.append({'source': s, 'last_ok': r['last_ok'] if r else None, 'last_try': r['last_try'] if r else None,
                    'next_try': r['next_try'] if r else None, 'fails': r['fails'] if r else 0,
                    'error': _shown(r['last_error']) if r else None})
    return out


def _shown(text: str | None) -> str | None:
    """Display only: an error the old _short stored ('HttpError: <HttpError ...', 'quotaExceeded: ...') in
    today's words; newer ones come back as they are."""
    if not text:
        return text
    if text.startswith('quotaExceeded:'):
        return _QUOTA
    return publish.plain(re.sub(r'^\w+: (?=<HttpError )', '', text), 160)
