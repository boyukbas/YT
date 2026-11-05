"""Instagram Reels as a publish.Platform, built on instagram.py (no Graph API code of its own)."""
import json, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import publish, tools
import instagram as IG

RETRIES = (1, 2, 4, 8, 16)                   # seconds between the tries of a Drive record write (spec 5.7)
LIVE_WAITS = (5, 10, 15)                     # media_publish failed, upload still FINISHED: read it again after these
QUICK = (10, 30)                             # (connect, read) seconds for the reads a person waits for (G22)
_USER = re.compile(r'[A-Za-z0-9._]{1,30}')   # an Instagram username


class Unclear(RuntimeError):
    """media_publish failed without a clear refusal and the PC cannot tell whether the Reel is live: the upload
    says PUBLISHED but no matching Reel was found, it is still FINISHED (Meta may still publish it), or the check
    failed. classify() gives 'unknown': the job ends 'unknown' and nothing is sent again until it is settled."""


class InstagramPlatform(publish.Platform):
    """Candidates from finished lessons (IG.lesson_files + IG.reels), the pipeline's caption and cover,
    posting through IG.post_instagram, records in <lesson>/<Output>/extras/instagram-posted.json."""
    name, label, noun, breaker = 'instagram', 'Instagram', 'Reel', 'meta'
    caption_max, tags_max, tags_suggested = 2200, 30, IG.MAX_TAGS

    @property
    def account_url(self) -> str:
        """https://www.instagram.com/<username>/ when the key file names the account, else ''."""
        try:
            user = str(IG.saved().get('username') or '')
        except Exception:
            return ''
        return f'https://www.instagram.com/{user}/' if _USER.fullmatch(user) else ''

    # ----- local -----

    def set_up(self) -> bool:
        return IG.TOKEN.exists()

    def candidates(self, root: Path):
        found, problems = [], []
        for lesson in sorted(p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(('_', '.'))):
            try:
                found += self._items(lesson)
            except Exception as e:
                problems.append({'lesson': lesson.name, 'error': f'{type(e).__name__}: {e}'})
        return sorted(found, key=lambda i: i.rank), problems

    def _items(self, lesson: Path) -> list:
        """The lesson's Reels as Items; [] when the lesson is not finished. Raises when its files cannot be read."""
        try:
            _, out, data = IG.lesson_files(lesson)
        except StopIteration:
            return []
        status = tools.extras(out) / 'status.json'
        if not status.exists() or json.loads(status.read_text(encoding='utf-8')).get('state') != 'done':
            return []
        reels, posted = IG.reels(out, data, probe=publish.probe_cached), IG.load(out)
        first = min((f.stat().st_mtime for f in out.glob('*.mp4')), default=0.0)
        items = []
        for f, entry in reels:
            m = re.search(r'-short-(\d+)$', f.stem)
            part, key, rec = (int(m.group(1)) if m else None), f'{lesson.name}/{out.name}/{f.name}', posted.get(f.name)
            items.append(publish.Item(
                platform=self.name, key=key, id=publish.item_id(key), lesson=lesson.name, out=out, file=f, part=part,
                title=re.sub(r'\s*#\w+', '', entry.get('title', '')).strip() or f.name, seconds=_seconds(f),
                rank=(first, part or 0, f.name), entry=entry,
                posted=rec if isinstance(rec, dict) and 'id' in rec else None, size=f.stat().st_size))
        return items

    def default_caption(self, item, root: Path) -> str:
        return IG.caption(item.entry, IG.hashtags(root))

    def cover(self, item):
        try:
            c = IG.cover_path(item.file)
            return c if c.is_file() else None
        except (AttributeError, ValueError, OSError):        # a name without -short-N (F13)
            return None

    def prepare(self, item, log) -> None:
        IG.prepare(item.out.parent, log)

    def history(self, root: Path, problems: list | None = None) -> list:
        """Every Instagram post in the Drive records. A record file that cannot be read raises (OSError): the
        timing rules must not run on half the posts. With a problems list (page bodies only) it is listed there
        instead and skipped."""
        out = []
        for lesson, o, path in tools.records(Path(root), IG.POSTED):
            try:
                recs = _read(path)
            except OSError as e:
                if problems is None:
                    raise
                problems.append({'lesson': lesson.name, 'error': f'{type(e).__name__}: {e}'})
                continue
            for name, rec in recs.items():
                at = _when(rec.get('posted_utc')) if isinstance(rec, dict) and not name.startswith('_') else None
                if at and rec.get('id'):
                    out.append(publish.Post(at=at, by=rec.get('by') or 'old', key=f'{lesson.name}/{o.name}/{name}',
                                            id=str(rec['id'])))
        return sorted(out, key=lambda p: p.at)

    def write_record(self, item, record: dict, retry: bool = True) -> None:
        """Merge record into [file name] of the lesson's instagram-posted.json, keeping every other key (also '_...'
        keys and a nested 'facebook'), then refresh instagram.md (best effort). Only under the PostLock; a file that
        is not valid JSON is never written over, and a file that cannot be read is never taken for a missing one
        (Drive away for a moment: Path.exists() says False then). retry=False: one try instead of RETRIES.
        Raises OSError (the job then keeps record_pending)."""
        if publish.PostLock.holder() is None:
            raise OSError('the posting lock is not held, so the Drive record was not written')
        path = tools.extras(item.out) / IG.POSTED
        try:
            data = _read(path)
        except FileNotFoundError:
            if not IG.absent(path):                       # raises when the folder cannot be listed either
                raise
            data = {}
        old = data.get(item.file.name)
        data[item.file.name] = {**(old if isinstance(old, dict) else {}), **record}
        _save(path, json.dumps(data, ensure_ascii=False, indent=1), RETRIES if retry else ())
        try:
            youtube = json.loads((tools.extras(item.out) / 'youtube.json').read_text(encoding='utf-8'))
            IG.write_md(item.out.parent, item.out, youtube)
        except Exception as e:                                # instagram.md is only for reading
            print(f'instagram.md not updated: {type(e).__name__}: {e}')

    def classify(self, exc: BaseException) -> str:
        return IG.classify(exc)

    def key_warning(self, now: datetime) -> str | None:
        try:
            return IG.renew_warning(now)
        except Exception:
            return None

    # ----- NETWORK -----

    def post(self, item, caption: str, journal) -> dict:
        cover = self.cover(item)
        if cover is None:
            raise FileNotFoundError('cover missing')
        start, stages = publish._utcnow(), []

        def stage(name):
            journal.stage(name)                           # raises publish.Stopped: nothing more is sent
            stages.append(name)
        try:
            return IG.post_instagram(item.file, cover, caption, journal.log, on_stage=stage,
                                     on_container=journal.container, on_published=journal.published)
        except Exception as e:
            if isinstance(e, publish.Stopped) or stages[-1:] != ['publishing'] or journal.media_id or \
                    not journal.container_id or IG.classify(e) not in ('transient', 'unknown', 'local'):
                raise
            return self._live_anyway(item, caption, journal, start, e)

    def _live_anyway(self, item, caption: str, journal, start: datetime, err: Exception) -> dict:
        """media_publish failed without a clear refusal; Meta sometimes publishes anyway, a few seconds later.
        The upload status is read, and read again after LIVE_WAITS while it is still FINISHED or IN_PROGRESS
        (each read goes to journal.checked when it has one). PUBLISHED and the Reel found among the newest media
        (caption head, time, as reconcile does) -> it is live: journal.published and its record. ERROR ->
        ContainerError (not posted, a content problem). Anything else (still FINISHED, PUBLISHED but not found,
        EXPIRED, or the check fails) -> Unclear: the job ends 'unknown' and nothing is sent again until settled."""
        tell = getattr(journal, 'checked', None)
        try:
            for wait in (*LIVE_WAITS, None):
                status = self.container_status(journal.container_id)
                if tell:
                    tell(status)
                if status not in ('FINISHED', 'IN_PROGRESS') or wait is None:
                    break
                time.sleep(wait)
            media = self.recent_media(10) if status == 'PUBLISHED' else []
        except Exception as e:
            raise Unclear(f'{err}; checking whether it went out failed too ({e})') from err
        if status == 'ERROR':
            raise IG.ContainerError(f'{err}; Instagram could not process {item.file.name}: ERROR', 'ERROR') from err
        if status != 'PUBLISHED':
            raise Unclear(f'{err}; the upload is {status}, not published (yet): it may still go out') from err
        since = start - timedelta(seconds=60)
        for m in media:
            at = _when(m.get('at'))
            if at and at >= since and _head(m.get('caption')) == _head(caption):
                journal.published(str(m['id']))
                journal.log(f'{item.file.name}: media_publish said "{err}", but the Reel is live ({m["id"]})')
                return {'id': str(m['id']), 'permalink': m.get('permalink') or '',
                        'posted': f'{at.astimezone(IG.ISTANBUL):%Y-%m-%d %H:%M}', 'posted_utc': at.isoformat()}
        raise Unclear(f'{err}; the upload says PUBLISHED, but no matching Reel was found') from err

    def container_status(self, container_id: str) -> str:
        return IG.call('GET', container_id, fields='status_code', timeout=QUICK).get('status_code', 'UNKNOWN')

    def recent_media(self, limit: int = 10) -> list[dict]:
        rows = IG.call('GET', f'{self._uid()}/media', fields='id,timestamp,permalink,caption,media_product_type',
                       limit=str(limit), timeout=QUICK)['data']
        return [{'id': str(m['id']), 'at': _iso(m.get('timestamp')), 'permalink': m.get('permalink') or '',
                 'caption': m.get('caption') or '', 'product': m.get('media_product_type')} for m in rows]

    def test(self) -> dict:
        ig = IG.call('GET', self._uid(), fields='username,followers_count,media_count', timeout=QUICK)
        return {'account': f"@{ig.get('username') or ''}", 'followers': ig.get('followers_count'),
                'media': ig.get('media_count'), 'limit': self.read_limit()}

    def read_stats(self) -> dict:
        r = IG.call('GET', self._uid(), fields='followers_count,follows_count,media_count,media.limit(50)'
                    '{id,timestamp,media_product_type,like_count,comments_count,permalink,caption}')
        media = (r.get('media') or {}).get('data', [])
        return {'followers': r.get('followers_count'), 'follows': r.get('follows_count'),
                'media_count': r.get('media_count'),
                'media': [{'id': str(m['id']), 'at': _iso(m.get('timestamp')), 'product': m.get('media_product_type'),
                           'likes': m.get('like_count'), 'comments': m.get('comments_count'),
                           'permalink': m.get('permalink') or '', 'caption': m.get('caption') or ''} for m in media]}

    def read_limit(self) -> dict:
        lim = IG.call('GET', f'{self._uid()}/content_publishing_limit', fields='quota_usage,config',
                      timeout=QUICK)['data'][0]
        return {'used': lim.get('quota_usage'), 'total': (lim.get('config') or {}).get('quota_total')}

    def read_permissions(self) -> list[str]:
        return [p['permission'] for p in IG.call('GET', 'me/permissions')['data'] if p.get('status') == 'granted']

    def _uid(self) -> str:
        return IG.saved()['user_id']


def make() -> InstagramPlatform:
    """The factory publish.registry() calls."""
    return InstagramPlatform()


# ---------- helpers ----------

def _seconds(f: Path):
    try:
        return round(publish.probe_cached(f)['duration'])
    except Exception:
        return None


def _head(text) -> str:
    """The caption head used to match a post: the first 60 characters, whitespace collapsed."""
    return ' '.join(str(text or '').split())[:60]


def _when(s) -> datetime | None:
    """An ISO time as aware UTC; None when missing or unreadable."""
    try:
        t = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _iso(ts) -> str | None:
    """Meta's '2026-11-04T13:46:12+0000' as '2026-11-04T13:46:12Z'; None when missing or unreadable."""
    try:
        return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S%z').astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError):
        return None


def _read(path: Path) -> dict:
    """A record file's JSON object. Drive can refuse a read for a moment, so a PermissionError is tried again;
    a file that is not a JSON object raises OSError (and is never written over)."""
    for wait in (0.2, 0.5, 1, None):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            break
        except PermissionError:
            if wait is None:
                raise
            time.sleep(wait)
        except ValueError as e:
            raise OSError(f'{path.parents[2].name}: {path.name} is not valid JSON ({e})') from None
    if not isinstance(data, dict):
        raise OSError(f'{path.parents[2].name}: {path.name} is not a JSON object')
    return data


def _save(path: Path, text: str, waits=None) -> None:
    """Write text to path: a temp file + os.replace, tried again after waits (default RETRIES: 1, 2, 4, 8 and 16 s).
    When Drive still refuses the replace, the file is written in place (as v1 did) and read back (addendum C3).
    Raises OSError when that fails too."""
    tmp = path.with_name(f'{path.name}.tmp-{os.getpid()}')
    try:
        for wait in (*(RETRIES if waits is None else waits), None):
            try:
                tmp.write_text(text, encoding='utf-8')
                os.replace(tmp, path)
                return
            except OSError:
                if wait is None:
                    break
                time.sleep(wait)
        try:
            path.write_text(text, encoding='utf-8')
            same = path.read_text(encoding='utf-8') == text
        except OSError as e:
            raise OSError(f'Drive did not take {path.name}: {e}') from None
        if not same:
            raise OSError(f'{path.name} reads back different after writing it in place')
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
