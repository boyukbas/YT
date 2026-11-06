"""YouTube uploads as a publish.Platform, built on youtube.py (the upload, details and record code live there).

A post here = the PC uploads a finished file (the lesson video or a Short) and makes it public:
  sharing      a resumable upload as a PRIVATE video with its title, description, tags and settings (an upload cut
               off midway makes no video). Before it, an upload of the same file already on YouTube is looked for
               (one made by hand in the YouTube app, or an earlier PC upload whose job was cut off): that one is used,
               so a file never goes up twice.
  processing   the video id is recorded at once (the job's container 'video:<id>' and the lesson's
               youtube-applied.json, marked publish_pending); then thumbnail, caption tracks and playlist
               (youtube.apply_video); then the PC waits for YouTube's processing (at most PROCESS_WAIT s).
  publishing   the video is made public (youtube.make_public).
Afterwards the watcher's YouTube step (youtube.apply) posts the exercise comment as it does for a hand upload.
The caption edited on the dashboard is the title (first line) and the description (after an empty line)."""
import json, re, time
from datetime import datetime, timezone
from pathlib import Path

import publish, tools
import youtube as Y
from platform_instagram import _read, _save, RETRIES

PROCESS_WAIT, PROCESS_EVERY = 900, 15        # s: the longest wait for YouTube's processing, and how often it is read
TITLE_MAX, DESCRIPTION_MAX = 100, 5000       # YouTube's limits (characters; the description's is in bytes, see checks)
_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
_BAD = re.compile(r'[<>]')                   # YouTube refuses these in a title or description


class QuotaUsedUp(RuntimeError):
    """YouTube's daily API quota is used up (a 'rate' error with a readable message)."""


class Refused(RuntimeError):
    """A Google error, worded plainly; the original is __cause__."""


class Rejected(RuntimeError):
    """YouTube rejected or failed the upload (its status says so): a content problem, not sent again."""


class YouTubePlatform(publish.Platform):
    """Candidates: every finished file (lesson video and Shorts) of finished lessons; posted = it has an id in
    youtube-applied.json (a PC upload, or a hand upload youtube.apply matched), unless that record still waits to be
    made public (publish_pending: a PC upload whose job stopped before publishing)."""
    name, label, noun, breaker = 'youtube', 'YouTube', 'video', 'google'
    verb, setup_hint = 'upload', 'youtube.py login'
    caption_max, tags_max, tags_suggested = TITLE_MAX + 2 + DESCRIPTION_MAX, 15, 3
    link_re = re.compile(r'^https://(www\.)?(youtu\.be/|youtube\.com/(watch\?v=|shorts/))[A-Za-z0-9_-]{11}([?&/].*)?$')
    link_hint = 'Use a link like https://youtu.be/…, or leave it empty.'
    account_url = 'https://www.youtube.com/@sinembb'

    def __init__(self, client=None):
        self._client = client                        # tests: a fake youtube.api() result

    def yt(self):
        return self._client if self._client is not None else Y.api()

    # ----- local -----

    def set_up(self) -> bool:
        return Y.TOKEN.exists()

    def candidates(self, root: Path):
        found, problems = [], []
        for lesson in sorted(p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(('_', '.'))):
            try:
                found += self._items(lesson)
            except Exception as e:
                problems.append({'lesson': lesson.name, 'error': f'{type(e).__name__}: {e}'})
        return sorted(found, key=lambda i: i.rank), problems

    def _items(self, lesson: Path) -> list:
        out = next((Path(lesson) / n for n in ('Output (Easy)', 'Output')
                    if (tools.extras(Path(lesson) / n) / 'youtube.json').exists()), None)
        if out is None:
            return []
        status = tools.extras(out) / 'status.json'
        if not status.exists() or json.loads(status.read_text(encoding='utf-8')).get('state') != 'done':
            return []
        data = json.loads((tools.extras(out) / 'youtube.json').read_text(encoding='utf-8'))
        applied = _applied(out)
        files = sorted(out.glob('*.mp4'), key=lambda f: (bool(Y.is_short(f)), Y.short_number(f), f.name))
        first = min((f.stat().st_mtime for f in files), default=0.0)
        items = []
        for f in files:
            short, key, rec = Y.is_short(f), f'{lesson.name}/{out.name}/{f.name}', applied.get(f.name)
            part = Y.short_number(f) if short else None
            w = Y.file_wanted(f, data, _settings(lesson), applied)
            posted = rec if isinstance(rec, dict) and rec.get('id') and not rec.get('publish_pending') else None
            items.append(publish.Item(
                platform=self.name, key=key, id=publish.item_id(key), lesson=lesson.name, out=out, file=f,
                part=(part or None) if short else None, title=w['title'] or f.name, seconds=_seconds(f),
                rank=(first, part or 0 if short else -1, f.name), entry={'title': w['title'], 'description': w['description']},
                posted=posted, size=f.stat().st_size))
        return items

    def default_caption(self, item, root: Path) -> str:
        return f"{item.entry.get('title', '')}\n\n{item.entry.get('description', '')}".strip()

    def cover(self, item):
        """The thumbnail YouTube gets (lesson video; a vertical one gets the vertical cover, as youtube.apply_video
        does), or the Short's vertical cover for the preview; None if none."""
        x = tools.extras(item.out)
        if Y.is_short(item.file):
            names = [f'cover-short-{item.part}.jpg', 'thumbnail-short.jpg']
        else:
            names = ['thumbnail-1.jpg']
            try:
                p = publish.probe_cached(item.file)
                if int(p['height']) > int(p['width']):
                    names.insert(0, 'thumbnail-short.jpg')
            except Exception:                            # unreadable: the lesson thumbnail
                pass
        return next((x / n for n in names if (x / n).is_file()), None)

    def preflight(self, item, caption: str, fresh: bool = False) -> list:
        """The file is there and readable (up to 12 h; more than 15 min needs a verified channel); a title on the
        first line (1-100 characters), a description under 5,000 bytes, no < or > (YouTube refuses them)."""
        C, out = publish.Check, []
        until = Y.quota_until(publish._utcnow())
        if until:                                       # it would fail on the first request (2026-11-06)
            out.append(C('quota', 'block', Y.quota_text(until), {'until': until.isoformat()}))
        if not item.file.is_file():
            out.append(C('file_missing', 'block', 'The video file is missing on Drive.'))
        else:
            try:
                seconds = float((tools.probe(item.file) if fresh else publish.probe_cached(item.file))['duration'])
            except Exception:
                out.append(C('length', 'block', 'The video could not be read.'))
            else:
                if not 1 <= seconds <= 12 * 3600:
                    out.append(C('length', 'block', f'The video is {round(seconds)} s; YouTube takes up to 12 hours.',
                                 {'seconds': round(seconds)}))
                elif seconds > 15 * 60:
                    out.append(C('length', 'info', 'Longer than 15 minutes: this needs a verified YouTube channel.',
                                 {'seconds': round(seconds)}))
        title, description = split_caption(caption)
        if not title:
            out.append(C('caption_empty', 'block', 'The first line is the title: it is empty.'))
        elif len(title) > TITLE_MAX:
            out.append(C('caption_too_long', 'block', f'The title (first line) is {len(title)} characters; '
                         f'YouTube takes {TITLE_MAX}.', {'length': len(title), 'max': TITLE_MAX}))
        if len(description.encode('utf-8')) > DESCRIPTION_MAX:
            out.append(C('caption_too_long', 'block', f'The description is {len(description.encode("utf-8"))} bytes; '
                         f'YouTube takes {DESCRIPTION_MAX}.', {'length': len(description.encode('utf-8')), 'max': DESCRIPTION_MAX}))
        if _BAD.search(title + description):
            out.append(C('caption_empty', 'block', 'YouTube does not take < or > in a title or description.'))
        tags = re.findall(r'(?<![\w&])#\w+', description)
        if len(tags) > self.tags_suggested:
            out.append(C('caption_tags', 'info', f'{len(tags)} hashtags; the channel uses at most {self.tags_suggested}.',
                         {'count': len(tags), 'max': self.tags_suggested}))
        return out

    def history(self, root: Path, problems: list | None = None) -> list:
        """Every video that went public (scheduled ones once their time has come): the time from its record (posted_utc of a PC upload, else publish_at, which
        youtube.apply writes when a hand upload went public or got its time). A record file that cannot be read
        raises (OSError), unless problems is a list."""
        out, now = [], publish._utcnow()
        for lesson, o, path in tools.records(Path(root), Y.APPLIED):
            try:
                recs = _read(path)
            except OSError as e:
                if problems is None:
                    raise
                problems.append({'lesson': lesson.name, 'error': f'{type(e).__name__}: {e}'})
                continue
            for name, rec in recs.items():
                if name.startswith('_') or not isinstance(rec, dict) or not rec.get('id') or rec.get('publish_pending'):
                    continue
                at = _when(rec.get('posted_utc')) or _when(rec.get('publish_at'))
                if at and at <= now:                   # a video scheduled for later has not gone out yet
                    out.append(publish.Post(at=at, by=rec.get('by') or 'old', key=f'{lesson.name}/{o.name}/{name}',
                                            id=str(rec['id'])))
        return sorted(out, key=lambda p: p.at)

    def write_record(self, item, record: dict, retry: bool = True) -> None:
        """Merge record into [file name] of the lesson's youtube-applied.json (every other key kept). A record with a
        time (the video went public) clears publish_pending and sets publish_at, which youtube.apply and its Shorts
        schedule read. A hand record (id 'hand') never replaces a real id. Only under the PostLock."""
        if publish.PostLock.holder() is None:
            raise OSError('the posting lock is not held, so the Drive record was not written')
        path = tools.extras(item.out) / Y.APPLIED
        try:
            data = _read(path)
        except FileNotFoundError:
            data = {}
        old = data.get(item.file.name)
        old = old if isinstance(old, dict) else {}
        if record.get('id') == 'hand' and old.get('id') not in (None, '', 'hand'):
            return
        new = {**old, **record}
        if record.get('posted_utc'):                 # it went public now (a scheduled time is gone)
            new['publish_pending'] = False
            new['publish_at'] = _z(_when(record['posted_utc']))
        data[item.file.name] = new
        _save(path, json.dumps(data, ensure_ascii=False, indent=1), RETRIES if retry else ())

    def classify(self, exc: BaseException) -> str:
        return classify(exc)

    def paused(self, now) -> dict | None:
        """While YouTube's daily quota is used up (the shared note): {'until', 'text'}, else None."""
        until = Y.quota_until(now)
        return {'until': publish.S.iso(until), 'text': Y.quota_text(until)} if until else None

    # ----- NETWORK -----

    def post(self, item, caption: str, journal) -> dict:
        """_post, with YouTube's quotaExceeded turned into a plain sentence (and the shared quota note), and any
        other Google error into Refused (plain words; the raw text goes to youtube.log through the journal)."""
        try:
            return self._post(item, caption, journal)
        except Exception as e:
            if 'quotaExceeded' in str(e):
                raise QuotaUsedUp(Y.quota_text(Y.quota_used_up(publish._utcnow()))) from e
            if not _google(e):
                raise
            journal.log(f'{item.file.name}: YouTube said: {publish.redact(str(e))[:500]}')
            raise Refused(publish.plain(publish.redact(str(e)))) from e

    def _post(self, item, caption: str, journal) -> dict:
        yt, name = self.yt(), item.file.name
        title, description = split_caption(caption)
        journal.stage('sharing')
        data = json.loads((tools.extras(item.out) / 'youtube.json').read_text(encoding='utf-8'))
        settings, applied = _settings(item.out.parent), _applied(item.out)
        w = Y.file_wanted(item.file, data, settings, applied)
        w.update(title=title, description=description)
        rec = applied.get(name) if isinstance(applied.get(name), dict) else {}
        vid = rec.get('id') if rec.get('id') not in (None, '', 'hand') else None
        if vid and Y.video_state(yt, vid) is None:
            vid = None                                   # deleted on YouTube: upload it again
        if not vid:
            found = Y.find_upload(yt, item.file, Y.taken_elsewhere(item.out) |
                                  {r['id'] for r in applied.values() if isinstance(r, dict) and r.get('id')})
            if found:
                vid = found['id']
                journal.log(f'{name}: already on YouTube ({vid}, {found["status"]["privacyStatus"]}): no new upload')
        if not vid:
            journal.log(f'{name}: uploading ({item.file.stat().st_size // 1_000_000} MB)')
            vid = Y.upload(yt, item.file, w, on_part=lambda sent, total: journal.stage('sharing'))
            journal.log(f'{name}: uploaded as {vid} (private)')
        journal.container(f'video:{vid}')
        self.write_record(item, {'id': vid, 'title': title, 'description': description, 'publish_pending': True,
                                 'uploaded_utc': _z(publish._utcnow())})
        journal.stage('processing')
        applied = _applied(item.out)
        rec = dict(applied.get(name) or {'id': vid})
        w = Y.file_wanted(item.file, data, settings, applied)
        w.update(public=False, publish=None)
        try:
            done, _, problems = Y.apply_video(yt, vid, item.out, item.file, w, rec, False)
            journal.log(f'{name}: ' + (', '.join(done) or 'details up to date') + (f' ({"; ".join(problems)})' if problems else ''))
            self.write_record(item, {k: rec[k] for k in rec if k not in ('publish_at',)})
        except Exception as e:                           # the watcher's YouTube step sets what is missing later
            if classify(e) in ('auth', 'rate'):
                raise
            journal.log(f'{name}: details not all set yet ({type(e).__name__}: {publish.redact(str(e))[:200]})')
        state = self._processed(yt, vid, journal)
        journal.stage('publishing')
        if state['privacy'] != 'public':
            Y.make_public(yt, vid)
        journal.published(vid)
        at = publish._utcnow()
        link = f'https://youtube.com/shorts/{vid}' if Y.is_short(item.file) else f'https://youtu.be/{vid}'
        journal.permalink(link)
        journal.log(f'{name}: public: {link}')
        return {'id': vid, 'permalink': link, 'posted': f'{at.astimezone(Y.ISTANBUL):%Y-%m-%d %H:%M}',
                'posted_utc': at.isoformat()}

    def _processed(self, yt, vid: str, journal) -> dict:
        """Wait until YouTube has processed the upload (or PROCESS_WAIT s went by: it goes public anyway and shows
        once ready). Raises Rejected when YouTube failed or rejected it."""
        waited = 0
        while True:
            st = Y.video_state(yt, vid)
            if st is None:
                raise Rejected(f'YouTube has no video {vid} any more')
            journal.checked(st['upload'] or 'unknown')
            if st['upload'] in ('failed', 'rejected'):
                raise Rejected(f'YouTube {st["upload"]} the upload: {st["failure"] or "no reason given"}')
            if st['upload'] == 'processed' or waited >= PROCESS_WAIT:
                return st
            time.sleep(PROCESS_EVERY)
            waited += PROCESS_EVERY
            journal.stage('processing')

    def container_status(self, container_id: str) -> str:
        """'video:<id>' -> PUBLISHED (public, or scheduled), FINISHED (private, processed), IN_PROGRESS (still
        processing), ERROR (failed or rejected), EXPIRED (no such video any more)."""
        vid = str(container_id).removeprefix('video:')
        if not _ID.match(vid):
            return 'EXPIRED'
        st = Y.video_state(self.yt(), vid)
        if st is None:
            return 'EXPIRED'
        if st['upload'] in ('failed', 'rejected'):
            return 'ERROR'
        if st['privacy'] == 'public':
            return 'PUBLISHED'
        return 'FINISHED' if st['upload'] == 'processed' else 'IN_PROGRESS'

    def recent_media(self, limit: int = 10) -> list[dict]:
        """The newest uploads: [{'id', 'at' (upload time), 'permalink', 'caption' (title + description), 'product'}]."""
        out = []
        for v in Y.recent_uploads(self.yt(), limit):
            sn = v['snippet']
            out.append({'id': v['id'], 'at': sn.get('publishedAt'), 'permalink': f'https://youtu.be/{v["id"]}',
                        'caption': f"{sn.get('title', '')}\n\n{sn.get('description', '')}".strip(),
                        'product': v['status'].get('privacyStatus')})
        return out

    def test(self) -> dict:
        ch = self.yt().channels().list(part='snippet,statistics', mine=True).execute()['items'][0]
        return {'account': ch['snippet']['title'], 'followers': _int(ch['statistics'].get('subscriberCount')),
                'media': _int(ch['statistics'].get('videoCount')), 'limit': self.read_limit()}

    def read_limit(self) -> dict:
        """YouTube's upload limit is 100 a day per project (its own quota bucket); used = PC uploads in 24 h."""
        cutoff = publish._utcnow().timestamp() - 86400
        with publish._Con() as con:
            rows = con.execute("SELECT published FROM jobs WHERE platform = 'youtube' AND published IS NOT NULL").fetchall()
        used = sum(1 for r in rows if (_when(r['published']) or datetime.min.replace(tzinfo=timezone.utc)).timestamp() >= cutoff)
        return {'used': used, 'total': 100}

    def read_permissions(self) -> list[str]:
        return list(Y.SCOPES)


def make() -> YouTubePlatform:
    """The factory publish.registry() calls."""
    return YouTubePlatform()


# ---------- helpers ----------

def split_caption(caption: str) -> tuple[str, str]:
    """(title, description): the first line, and the rest after it (leading empty lines dropped)."""
    first, _, rest = str(caption or '').strip().partition('\n')
    return first.strip(), rest.strip('\n').strip()


def classify(exc: BaseException) -> str:
    """The error kind of a YouTube API error: 401 or a refused sign-in 'auth'; quota or rate limits 'rate';
    403 'permission'; 400 'content'; 5xx, timeouts and connection errors 'transient'; Rejected 'content';
    a local file problem 'local'; anything else 'unknown'."""
    from googleapiclient.errors import HttpError
    if isinstance(exc, Refused) and exc.__cause__ is not None:
        return classify(exc.__cause__)
    if isinstance(exc, Rejected):
        return 'content'
    if isinstance(exc, QuotaUsedUp):
        return 'rate'
    if isinstance(exc, HttpError):
        status = exc.resp.status
        text = f'{exc} {getattr(exc, "error_details", "")} {(getattr(exc, "content", b"") or b"")[:4000]!r}'
        if status == 401:
            return 'auth'
        if re.search(r'quotaExceeded|rateLimitExceeded|uploadLimitExceeded|userRateLimitExceeded', text) or status == 429:
            return 'rate'
        if status == 403:
            return 'permission'
        if status >= 500:
            return 'transient'
        if status in (400, 404):
            return 'content'
        return 'unknown'
    text = f'{type(exc).__name__}: {exc}'
    if 'sign-in needed' in text or 'RefreshError' in text:
        return 'auth'
    if isinstance(exc, (TimeoutError, ConnectionError)) or re.search(r'not reachable|timed out|ServerNotFound|'
                                                                       r'Connection(Reset|Aborted|Refused)', text):
        return 'transient'
    return 'local' if isinstance(exc, OSError) else 'unknown'


def _google(e: BaseException) -> bool:
    """True for a Google API error (an HttpError, or its text)."""
    from googleapiclient.errors import HttpError
    return isinstance(e, HttpError) or str(e).startswith('<HttpError')


def _applied(out: Path) -> dict:
    path = tools.extras(out) / Y.APPLIED
    try:
        return _read(path)
    except FileNotFoundError:
        return {}


def _settings(lesson: Path) -> dict:
    p = Path(lesson) / 'settings.json'
    try:
        s = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    except ValueError:
        s = {}
    return s if isinstance(s, dict) else {}


def _seconds(f: Path):
    try:
        return round(publish.probe_cached(f)['duration'])
    except Exception:
        return None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _when(s) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _z(t: datetime | None) -> str | None:
    """'2026-11-05T16:00:00Z', as youtube-applied.json keeps times."""
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if t else None
