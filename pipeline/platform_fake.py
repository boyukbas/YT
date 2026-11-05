"""A publish.Platform that never touches the network or the real Drive (tests and dashboard --dev)."""
import json, os, re, threading
from pathlib import Path

import publish

_DRIVE = re.compile(r'^[A-Za-z]:\\My Drive(\\|$)', re.I)
_OUTCOMES = ('ok', 'permalink_fails', 'record_fails', 'auth', 'permission', 'rate', 'transient', 'local', 'unknown',
             'content', 'crash_after_container', 'crash_after_publish', 'publish_error', 'publish_error_live')
_BEFORE = ('auth', 'permission', 'rate', 'transient', 'local', 'unknown')      # raised before any container
_TEXTS = {'auth': 'Instagram refused the key (code 190).', 'permission': 'Instagram refused the permission (code 10).',
          'rate': 'Instagram says: too many calls (code 4).', 'content': 'Instagram could not process the video: ERROR',
          'transient': 'Instagram did not answer (HTTP 503).', 'local': 'The video could not be shared (fake).',
          'unknown': 'Something unexpected happened (fake).'}


class FakeError(RuntimeError):
    """Raised by FakePlatform; .kind is the error kind it stands for."""
    def __init__(self, kind: str):
        super().__init__(_TEXTS.get(kind, f'fake {kind} error'))
        self.kind = kind


class FakeCrash(BaseException):
    """Stands for the process dying mid-post; it passes through 'except Exception'."""


class FakePlatform(publish.Platform):
    """script: outcomes taken one per post() ('ok' when the list is empty):
    'ok' | 'permalink_fails' | 'record_fails' | 'auth' | 'permission' | 'rate' | 'transient' | 'local' |
    'unknown' (each raised before any container) | 'content' (raised after the container) |
    'crash_after_container' | 'crash_after_publish' |
    'publish_error' (the publish call answers 'transient' and the upload stays FINISHED) |
    'publish_error_live' (the same answer, but the post went live: upload PUBLISHED, the media listed).
    container_states: {container id: status}. media: what recent_media() and read_stats() return.
    calls: every NETWORK method call as (method, args). Records live in a JSON file (records).
    __init__ raises ValueError for a root or item file under '<letter>:\\My Drive'."""
    script: list[str]
    container_states: dict[str, str]
    media: list[dict]
    calls: list[tuple]

    def __init__(self, items: list[dict], *, name: str = 'instagram', label: str = 'Instagram', noun: str = 'Reel',
                 breaker: str = 'meta', records: Path | None = None, root: Path | None = None,
                 set_up: bool = True):
        records = Path(records) if records else publish.WORK / f'fake-{name}-records.json'
        for p in [root, records, *(i['file'] for i in items)]:
            if p is not None and _DRIVE.match(os.path.abspath(p)):
                raise ValueError(f'FakePlatform never uses the real Drive: {p}')
        self.name, self.label, self.noun, self.breaker = name, label, noun, breaker
        self.account_url = 'https://www.instagram.com/example_account/'
        self.root, self.records, self.ready = (Path(root) if root else None), records, set_up
        self._items = [dict(i) for i in items]
        self.script, self.container_states, self.media, self.calls = [], {}, [], []
        self.problems: list[dict] = []           # what candidates() reports as unreadable lessons
        self.prepared: list[str] = []            # keys prepare() was called for
        self.test_error: str | None = None       # a kind: test() raises FakeError(kind)
        self.history_error: str | None = None    # history() finds a record file it cannot read (lesson 'new')
        self._fail_writes = 0                    # 'record_fails': the next write_record raises OSError
        self._n = 0
        self._io = threading.RLock()

    # ----- local -----

    def set_up(self) -> bool:
        return bool(self.ready)

    def candidates(self, root: Path):
        recs = self._load()
        found = []
        for d in self._items:
            rec = recs.get(d['key'])
            found.append(publish.Item(platform=self.name, key=d['key'], id=publish.item_id(d['key']), lesson=d['lesson'],
                                      out=Path(d['out']), file=Path(d['file']), part=d['part'], title=d['title'],
                                      seconds=d['seconds'], rank=tuple(d['rank']), entry=dict(d.get('entry') or {}),
                                      posted=dict(rec) if isinstance(rec, dict) and 'id' in rec else None,
                                      size=_size(Path(d['file']))))
        return sorted(found, key=lambda i: i.rank), [dict(p) for p in self.problems]

    def rename(self, old: str, new: str) -> None:
        """Rename lesson folder old to new, as Sinem would on Drive: the folder on disk (when it exists), the
        items' keys and paths, and the records (they move with the folder, like instagram-posted.json)."""
        with self._io:
            for d in self._items:
                if d['lesson'] == old:
                    d['lesson'], d['key'] = new, new + d['key'][len(old):]
                    for k in ('out', 'file', 'cover'):
                        if d.get(k):
                            parts = list(Path(d[k]).parts)
                            at = len(parts) - 1 - parts[::-1].index(old)        # the lesson folder, not a parent
                            d[k] = Path(*parts[:at], new, *parts[at + 1:])
            if self.root and (self.root / old).is_dir():
                os.rename(self.root / old, self.root / new)
            data = self._load()
            data = {(new + k[len(old):] if k.startswith(old + '/') else k): v for k, v in data.items()}
            self.records.parent.mkdir(parents=True, exist_ok=True)
            self.records.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')

    def default_caption(self, item, root: Path) -> str:
        desc = str((item.entry or {}).get('description') or '').strip()
        return f'{item.title}\n\n{desc}' if desc else item.title

    def cover(self, item):
        c = next((d.get('cover') for d in self._items if d['key'] == item.key), None)
        return Path(c) if c and Path(c).is_file() else None

    def prepare(self, item, log) -> None:
        self.prepared.append(item.key)

    def history(self, root: Path, problems: list | None = None) -> list:
        if self.history_error:
            if problems is None:
                raise OSError(self.history_error)
            problems.append({'lesson': 'new', 'error': f'OSError: {self.history_error}'})
        out = []
        for key, rec in self._load().items():
            if self.history_error and key.startswith('new/'):
                continue
            at = publish._time(rec.get('posted_utc')) if isinstance(rec, dict) and not key.startswith('_') else None
            if at and rec.get('id'):
                out.append(publish.Post(at=at, by=rec.get('by') or 'old', key=key, id=str(rec['id'])))
        return sorted(out, key=lambda p: p.at)

    def write_record(self, item, record: dict, retry: bool = True) -> None:
        with self._io:
            if self._fail_writes > 0:
                self._fail_writes -= 1
                raise OSError('Drive is not writable right now (fake)')
            data = self._load()
            data[item.key] = {**(data.get(item.key) or {}), **record}
            tmp = self.records.with_name(f'{self.records.name}.tmp-{os.getpid()}')
            self.records.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp, self.records)

    def classify(self, exc: BaseException) -> str:
        if isinstance(exc, FakeError):
            return exc.kind if exc.kind in publish.KINDS else 'unknown'
        return 'local' if isinstance(exc, OSError) else 'unknown'

    # ----- NETWORK (fake) -----

    def post(self, item, caption: str, journal) -> dict:
        self.calls.append(('post', (item.key, caption)))
        outcome = self.script.pop(0) if self.script else 'ok'
        if outcome not in _OUTCOMES:
            raise ValueError(f'unknown script outcome {outcome!r}')
        journal.stage('sharing')
        if outcome in _BEFORE:
            raise FakeError(outcome)
        n = self._next()
        cid = f'c-{n}'
        self.container_states[cid] = 'IN_PROGRESS'
        journal.container(cid)
        journal.stage('processing')
        if outcome == 'crash_after_container':
            raise FakeCrash(f'the process died after container {cid}')
        if outcome == 'content':
            self.container_states[cid] = 'ERROR'
            raise FakeError('content')
        self.container_states[cid] = 'FINISHED'
        journal.stage('publishing')
        if outcome == 'publish_error':
            raise FakeError('transient')
        now = publish._utcnow()
        mid, link = f'm-{n}', f'https://www.instagram.com/reel/FAKE{n}/'
        self.container_states[cid] = 'PUBLISHED'
        self.media.insert(0, {'id': mid, 'at': publish.S.iso(now), 'permalink': link, 'caption': caption,
                              'product': 'REELS', 'likes': 0, 'comments': 0})
        if outcome == 'publish_error_live':
            raise FakeError('transient')
        journal.published(mid)
        if outcome == 'crash_after_publish':
            raise FakeCrash(f'the process died after media {mid} went live')
        if outcome == 'record_fails':
            self._fail_writes += 1
        return {'id': mid, 'permalink': '' if outcome == 'permalink_fails' else link,
                'posted': f'{publish.S.local(now):%Y-%m-%d %H:%M}', 'posted_utc': now.isoformat()}

    def container_status(self, container_id: str) -> str:
        self.calls.append(('container_status', (container_id,)))
        state = self.container_states.get(container_id, 'EXPIRED')
        if isinstance(state, BaseException):           # tests may put an error here instead of a status
            raise state
        return state

    def recent_media(self, limit: int = 10) -> list[dict]:
        self.calls.append(('recent_media', (limit,)))
        return [{k: m.get(k) for k in ('id', 'at', 'permalink', 'caption', 'product')} for m in self.media[:limit]]

    def test(self) -> dict:
        self.calls.append(('test', ()))
        if self.test_error:
            raise FakeError(self.test_error)
        return {'account': '@example_account', 'followers': 71, 'media': len(self.media), 'limit': {'used': 0, 'total': 100}}

    def read_stats(self) -> dict:
        self.calls.append(('read_stats', ()))
        media = [{k: m.get(k) for k in ('id', 'at', 'product', 'likes', 'comments', 'permalink', 'caption')}
                 for m in self.media[:50]]
        return {'followers': 71, 'follows': 0, 'media_count': len(self.media), 'media': media}

    def read_limit(self) -> dict:
        self.calls.append(('read_limit', ()))
        return {'used': 0, 'total': 100}

    def read_permissions(self) -> list[str]:
        self.calls.append(('read_permissions', ()))
        return ['instagram_basic', 'instagram_content_publish', 'pages_show_list', 'business_management']

    # ----- helpers -----

    def _load(self) -> dict:
        with self._io:
            try:
                data = json.loads(self.records.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

    def _next(self) -> int:
        """The next number for c-<n>, m-<n> and FAKE<n>, skipping numbers the records already use."""
        with self._io:
            used = {m.group(1) for r in self._load().values() if isinstance(r, dict)
                    for m in [re.search(r'/reel/FAKE(\d+)/', str(r.get('permalink') or ''))] if m}
            self._n += 1
            while str(self._n) in used or f'c-{self._n}' in self.container_states:
                self._n += 1
            return self._n


def _size(f: Path):
    """The file's size in bytes, or None."""
    try:
        return f.stat().st_size
    except OSError:
        return None
