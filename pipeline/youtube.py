"""YouTube Data API: Sinem uploads the finished video in the YouTube app; this fills in everything else.

  python youtube.py login                          sign in once (browser on this PC), pick Almanca Dil Atölyesi
  python youtube.py check                          read-only: channel + latest uploads
  python youtube.py apply "<lesson>" [--video ID] [--dry-run]

apply finds the upload by its length (an upload made after the render, same duration ±2 s), then sets title, description,
tags, category, languages, audience, paid-promotion/AI declarations, thumbnail, TR + DE caption tracks, playlist, and
posts the comment once the video is public (YouTube has no comments on private videos). Safe to run again: every part is
checked first and done once (record in Output/youtube-applied.json). The API cannot pin the comment, add an end screen
or make a premiere; those stay in Studio and are listed in STATUS.
Choices in <lesson>/settings.json: {"title": 1-3, "description": 1-2, "thumbnail": 1-3,
"publish": "2026-11-05 21:15" (Istanbul time; scheduled publish of a private video), "paid": false, "ai": false,
"public": true (make the main video public once filled in; same as --public),
"shorts_time": "19:00", "shorts_every": 1 (Shorts are never published together: each private Short is scheduled
for the next free day across all lessons, at this Istanbul time; upload Shorts as Private),
"shorts_playlist": "<name>" (the Shorts go in it in their order)}. A playlist that doesn't exist yet is created (public).
Quota: 10,000 units a day; a Short with two caption tracks costs ~900, so a lesson with many Shorts can need two days
(it resumes where it stopped). The quota resets at midnight Pacific time (11:00 Istanbul in winter, 10:00 in summer).

Client: %USERPROFILE%\\.shorts-pipeline\\youtube_client.json (Desktop OAuth client, project "shorts0editor").
Token:  %USERPROFILE%\\.shorts-pipeline\\youtube_token.json (refreshed automatically; delete it to sign in again)."""
import argparse, hashlib, json, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

sys.path.insert(0, str(Path(__file__).parent))
from tools import probe

KEYS = Path.home() / '.shorts-pipeline'
CLIENT, TOKEN = KEYS / 'youtube_client.json', KEYS / 'youtube_token.json'
SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']
CHANNEL_ID = 'UCivLHbvcLIKKGkLjnOrMaHA'                  # Almanca Dil Atölyesi (@sinembb)
ISTANBUL = timezone(timedelta(hours=3))                  # Turkey has no daylight saving time
APPLIED = 'youtube-applied.json'
SHORTS_TIME = '19:00'                                    # Istanbul; Shorts go public one a day at this time
SHORTS_WARN = 3                                          # more Shorts than this per lesson → a warning in STATUS
MANUAL = ['pin the comment', 'end screen (video + subscribe on the right half, last 10 s)']


def credentials(interactive=False):
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None                                  # revoked / expired (Testing mode) → sign in again
    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError('YouTube sign-in needed: run  python youtube.py login')
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
        creds = flow.run_local_server(port=0, prompt='select_account consent', access_type='offline',
                                      success_message='Signed in. You can close this tab and go back to Claude.')
        ch = build('youtube', 'v3', credentials=creds, cache_discovery=False).channels().list(part='snippet', mine=True).execute()
        got = [(c['id'], c['snippet']['title']) for c in ch.get('items', [])]
        if not any(i == CHANNEL_ID for i, _ in got):
            TOKEN.unlink(missing_ok=True)
            raise SystemExit(f'Signed in to {got or "no channel"}, not Almanca Dil Atölyesi. Run login again and pick that channel.')
    TOKEN.write_text(creds.to_json())
    return creds


def api(interactive=False):
    return build('youtube', 'v3', credentials=credentials(interactive), cache_discovery=False)


def check(yt):
    """Read-only: which channel we are signed in to, and its latest uploads."""
    ch = yt.channels().list(part='snippet,statistics,contentDetails', mine=True).execute()['items'][0]
    print(f"Channel: {ch['snippet']['title']} ({ch['id']}) — {ch['statistics'].get('subscriberCount', '?')} subscribers, "
          f"{ch['statistics']['videoCount']} videos")
    uploads = ch['contentDetails']['relatedPlaylists']['uploads']
    items = yt.playlistItems().list(part='contentDetails', playlistId=uploads, maxResults=5).execute()['items']
    ids = [i['contentDetails']['videoId'] for i in items]
    for v in yt.videos().list(part='snippet,status', id=','.join(ids)).execute()['items']:
        st = v['status']
        when = f" (publishes {st['publishAt']})" if st.get('publishAt') else ''
        print(f"  {v['id']}  {st['privacyStatus']:8s}{when}  {v['snippet']['title']}")


def seconds(iso):
    """'PT3M17S' → 197."""
    h, m, s = (int(x or 0) for x in re.fullmatch(r'P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso).groups())
    return h * 3600 + m * 60 + s


def sha(path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:12]


def recent_uploads(yt, n=25):
    up = yt.channels().list(part='contentDetails', mine=True).execute()['items'][0]['contentDetails']['relatedPlaylists']['uploads']
    ids = [i['contentDetails']['videoId'] for i in
           yt.playlistItems().list(part='contentDetails', playlistId=up, maxResults=n).execute()['items']]
    return yt.videos().list(part='snippet,status,contentDetails,fileDetails', id=','.join(ids)).execute()['items'] if ids else []


def find_upload(yt, mp4, taken):
    """The upload of this file: made after the render, same length (±2 s), not used for another lesson.
    Several Shorts of one lesson can have the same length, so the closest file size wins (the YouTube app usually
    uploads the file unchanged: same size to the byte)."""
    length, made = probe(mp4)['duration'], datetime.fromtimestamp(mp4.stat().st_mtime, timezone.utc) - timedelta(minutes=10)
    hits = [v for v in recent_uploads(yt) if v['id'] not in taken
            and abs(seconds(v['contentDetails']['duration']) - length) <= 2
            and datetime.fromisoformat(v['snippet']['publishedAt'].replace('Z', '+00:00')) >= made]
    size = mp4.stat().st_size
    return min(hits, key=lambda v: abs(int(v.get('fileDetails', {}).get('fileSize', 0)) - size)) if hits else None


def taken_elsewhere(out):
    """Video IDs already filled in for other lessons (siblings of this lesson in the Drive root)."""
    ids = set()
    for p in out.parent.parent.glob(f'*/*/{APPLIED}'):
        if p.parent != out:
            ids |= {r['id'] for r in json.loads(p.read_text(encoding='utf-8')).values()}
    return ids


def next_slot(out, applied, settings, planned=()):
    """When the next Short goes public: one Short a day (settings "shorts_every" days apart) at "shorts_time"
    (Istanbul), after every Short already out or scheduled in any lesson, never in the past, and never within 3 hours
    of a long video going public (then 3 hours after it, like the Sunday teaser).
    Subscribers get a notification per video, so Shorts are spread out instead of published together."""
    recs = list(applied.items())
    for p in out.parent.parent.glob(f'*/*/{APPLIED}'):
        if p.parent != out:
            recs += json.loads(p.read_text(encoding='utf-8')).items()
    when = lambda r: datetime.fromisoformat(r['publish_at'].replace('Z', '+00:00'))
    times = [when(r) for f, r in recs if re.search(r'-short(-\d+)?\.mp4$', f) and r.get('publish_at')] + list(planned)
    longs = [when(r) for f, r in recs if f.endswith('-video.mp4') and r.get('publish_at')]
    hh, mm = map(int, settings.get('shorts_time', SHORTS_TIME).split(':'))
    now = datetime.now(ISTANBUL)
    day = max(times).astimezone(ISTANBUL).date() + timedelta(days=int(settings.get('shorts_every', 1))) if times else now.date()
    at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ISTANBUL)
    while at < now + timedelta(minutes=30):
        at += timedelta(days=1)
    near = [t for t in longs if abs(t - at) < timedelta(hours=3)]
    if near:
        at = max(near).astimezone(ISTANBUL) + timedelta(hours=3)
    return at


def wanted(data, settings):
    """What the video should look like, from youtube.json + the lesson's choices."""
    pick = lambda key, lst: lst[min(max(int(settings.get(key, 1)), 1), len(lst)) - 1]
    return {
        'title': pick('title', data['titles'])[:100],
        'description': pick('description', data['descriptions']),
        'tags': data.get('tags', []),
        'categoryId': re.search(r'\d+', str(data.get('category', '27'))).group(0),
        'defaultLanguage': data.get('defaultLanguage', 'tr'),
        'defaultAudioLanguage': data.get('defaultAudioLanguage', 'tr'),
        'madeForKids': bool(data.get('madeForKids', False)),
        'paid': bool(settings.get('paid', False)),
        'ai': bool(settings.get('ai', False)),
        'publish': settings.get('publish'),
        'thumbnail': f"thumbnail-{settings.get('thumbnail', 1)}.jpg",
        'playlist': data.get('playlist'),
        'comment': data.get('pinned_comment', '').strip(),
        'public': False,
    }


def apply_video(yt, vid, out, mp4, w, rec, dry):
    """Bring one uploaded video up to date. Returns (done, waiting, problems) as short phrases."""
    done, waiting, problems = [], [], []
    v = yt.videos().list(part='snippet,status,paidProductPlacementDetails', id=vid).execute()['items'][0]
    sn, st = v['snippet'], v['status']

    # 1. texts, category, languages, audience, declarations, publish time (one update = 50 units)
    want_sn = {k: w[k] for k in ('title', 'description', 'tags', 'categoryId', 'defaultLanguage', 'defaultAudioLanguage')}
    same = lambda k, val: sorted(sn.get(k) or []) == sorted(val) if k == 'tags' else sn.get(k) == val   # YouTube sorts tags
    diff = [k for k, val in want_sn.items() if not same(k, val)]
    new_st = {k: st[k] for k in ('privacyStatus', 'embeddable', 'license', 'publicStatsViewable') if k in st}
    if st.get('publishAt'):
        new_st['publishAt'] = st['publishAt']
    new_st.update(selfDeclaredMadeForKids=w['madeForKids'], containsSyntheticMedia=w['ai'])
    if w['public'] and st['privacyStatus'] != 'public':
        new_st['privacyStatus'] = 'public'
        new_st.pop('publishAt', None)
        diff.append('public')
    if w['publish'] and new_st['privacyStatus'] == 'private':
        at = datetime.strptime(w['publish'], '%Y-%m-%d %H:%M').replace(tzinfo=ISTANBUL)
        if at > datetime.now(timezone.utc) + timedelta(minutes=15):
            new_st['publishAt'] = at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            problems.append(f"publish time {w['publish']} has passed; not scheduled")
    if st.get('selfDeclaredMadeForKids', st.get('madeForKids')) != w['madeForKids']:
        diff.append('madeForKids')
    if bool(st.get('containsSyntheticMedia')) != w['ai']:             # the API leaves it out when the answer is No
        diff.append('ai')
    if new_st.get('publishAt') != st.get('publishAt'):
        diff.append('publishAt')
    if v.get('paidProductPlacementDetails', {}).get('hasPaidProductPlacement') != w['paid']:
        diff.append('paid')
    if new_st['privacyStatus'] == 'public':            # remembered for the Shorts schedule (one a day)
        rec['publish_at'] = sn['publishedAt'] if st['privacyStatus'] == 'public' else \
            datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    elif new_st.get('publishAt'):
        rec['publish_at'] = new_st['publishAt']
    if diff:
        if not dry:
            yt.videos().update(part='snippet,status,paidProductPlacementDetails', body={
                'id': vid, 'snippet': want_sn, 'status': new_st,
                'paidProductPlacementDetails': {'hasPaidProductPlacement': w['paid']}}).execute()
        done.append('details (' + ', '.join(diff) + ')')

    # 2. thumbnail (50 units); YouTube gives no way to compare, so the file hash is remembered
    th = out / w['thumbnail'] if w['thumbnail'] else None
    if th and (out / 'thumbnail-short.jpg').exists():   # a vertical main video (one-clip Short lesson): vertical cover
        p = probe(mp4)
        if p['height'] > p['width']:
            th = out / 'thumbnail-short.jpg'
    if th and th.exists() and rec.get('thumbnail') != sha(th):
        if not dry:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(th), mimetype='image/jpeg')).execute()
            rec['thumbnail'] = sha(th)
        done.append(f'thumbnail ({th.name})')

    # 3. caption tracks (insert 400 / update 450 units), replacing an earlier track in the same language
    tracks = yt.captions().list(part='snippet', videoId=vid).execute()['items']
    for lang in ('tr', 'de'):
        srt = out / f'{mp4.stem}-subtitles-{lang}.srt'
        if not srt.exists() or rec.get(f'captions_{lang}') == sha(srt):
            continue
        old = next((t for t in tracks if t['snippet']['language'] == lang and t['snippet']['trackKind'] == 'standard'), None)
        if not dry:
            media = MediaFileUpload(str(srt), mimetype='application/octet-stream')
            if old:
                yt.captions().update(part='snippet', body={'id': old['id'], 'snippet': {'isDraft': False}}, media_body=media).execute()
            else:
                yt.captions().insert(part='snippet', body={'snippet': {'videoId': vid, 'language': lang, 'name': '', 'isDraft': False}},
                                     media_body=media).execute()
            rec[f'captions_{lang}'] = sha(srt)
        done.append(f'{lang.upper()} captions ({"replaced" if old else "added"})')

    # 4. playlist; a missing one is created (public, 50 units), decided 2026-11-04
    if w['playlist'] and not rec.get('playlist'):
        pls = yt.playlists().list(part='snippet', mine=True, maxResults=50).execute()['items']
        pl = next((p for p in pls if p['snippet']['title'].casefold() == w['playlist'].casefold()), None)
        if not pl and not dry:
            pl = yt.playlists().insert(part='snippet,status', body={
                'snippet': {'title': w['playlist'][:150], 'defaultLanguage': w['defaultLanguage']},
                'status': {'privacyStatus': 'public'}}).execute()
            done.append(f"new playlist {pl['snippet']['title']}")
        if not pl:
            done.append(f"new playlist {w['playlist']}")
        elif yt.playlistItems().list(part='id', playlistId=pl['id'], videoId=vid).execute()['items']:
            rec['playlist'] = pl['id']
        else:
            body = {'playlistId': pl['id'], 'resourceId': {'kind': 'youtube#video', 'videoId': vid}}
            if w.get('before'):           # Shorts keep their order: after the lower-numbered ones already in the list
                items = yt.playlistItems().list(part='contentDetails', playlistId=pl['id'], maxResults=50).execute()['items']
                body['position'] = sum(i['contentDetails']['videoId'] in w['before'] for i in items)
            if not dry:
                yt.playlistItems().insert(part='snippet', body={'snippet': body}).execute()
                rec['playlist'] = pl['id']
            done.append(f"playlist {pl['snippet']['title']}")

    # 5. the exercise comment, as the channel, once the video is public
    if w['comment'] and not rec.get('comment'):
        public = st['privacyStatus'] == 'public' or (new_st['privacyStatus'] == 'public' and not dry)   # made public above
        mine = []
        if public:
            try:
                mine = [c for c in yt.commentThreads().list(part='snippet', videoId=vid, maxResults=100,
                                                            searchTerms=w['comment'][:40]).execute()['items']
                        if c['snippet']['topLevelComment']['snippet'].get('authorChannelId', {}).get('value') == CHANNEL_ID]
            except HttpError as e:
                problems.append(f'comments unavailable ({e.status_code})')
                public = False
        if not public:
            waiting.append('comment (posted when the video is public)')
        elif mine:
            rec['comment'] = mine[0]['id']
        else:
            if not dry:
                c = yt.commentThreads().insert(part='snippet', body={'snippet': {'videoId': vid, 'topLevelComment': {
                    'snippet': {'textOriginal': w['comment']}}}}).execute()
                rec['comment'] = c['id']
            done.append('comment posted')
    return done, waiting, problems


def apply(lesson, video_id=None, dry=False, log=print, public=False):
    """Fill in the YouTube details for a finished lesson (the video and its Shorts).
    public: also make them public (the video, and each Short once its details are set).
    Returns (summary, complete), or None while no matching upload exists yet."""
    lesson = Path(lesson)
    out = next(o for o in (lesson / 'Output (Easy)', lesson / 'Output') if (o / 'youtube.json').exists())
    data = json.loads((out / 'youtube.json').read_text(encoding='utf-8'))
    sp = lesson / 'settings.json'
    settings = json.loads(sp.read_text(encoding='utf-8')) if sp.exists() else {}
    applied = json.loads((out / APPLIED).read_text(encoding='utf-8')) if (out / APPLIED).exists() else {}
    public = public or bool(settings.get('public'))
    yt = api()
    lines, complete = [], True
    is_short = lambda f: re.search(r'-short(-\d+)?$', f.stem)
    num = lambda f: int(m.group(1)[1:]) if (m := is_short(f)) and m.group(1) else 0
    main, planned = None, []
    n_shorts = sum(bool(is_short(f)) for f in out.glob('*.mp4'))
    if n_shorts > SHORTS_WARN:
        lines.append(f'WARNING: {n_shorts} Shorts in one lesson; they go public one a day, so the last one is '
                     f'{n_shorts} days away. Next time keep it to {SHORTS_WARN} or fewer (shorts_max)')
    for mp4 in sorted(out.glob('*.mp4'), key=lambda f: (bool(is_short(f)), num(f), f.name)):      # the video first
        short = is_short(mp4)
        w = wanted(data, settings)
        w['public'] = public
        if short:                     # Shorts: own title + description + Shorts playlist, no custom thumbnail / comment
            own = next((s for s in data.get('shorts', []) if mp4.stem.endswith('-' + s['version'])), {})
            desc = own.get('description') or data['descriptions'][-1]
            if main:                  # the full lesson as a link, once the video's ID is known
                desc = desc.replace('Dersin tamamı kanalda.', f'Dersin tamamı: https://youtu.be/{main}')
            w.update(title=(own.get('title') or data.get('short_title') or w['title'])[:100], description=desc,
                     thumbnail=None, playlist=settings.get('shorts_playlist') or data.get('shorts_playlist'), comment='',
                     before={r['id'] for f, r in applied.items() if is_short(Path(f)) and num(Path(f)) < num(mp4)})
        rec = applied.get(mp4.name)
        if video_id and not short:
            rec = rec if rec and rec['id'] == video_id else {'id': video_id}
        elif not rec:
            v = find_upload(yt, mp4, taken_elsewhere(out) | {r['id'] for r in applied.values()})
            if not v:
                lines.append(f'{"Short" if short else "Video"}: not uploaded yet')
                complete = False
                continue
            rec = {'id': v['id']}
            log(f"{mp4.name} → {v['id']} ({v['snippet']['title']}, {v['status']['privacyStatus']})")
        if not short:
            main = rec['id']
        else:                         # Shorts never go public all at once: each gets the next free day
            w['public'] = False
            w['publish'] = None if rec.get('publish_at') else next_slot(out, applied, settings, planned).strftime('%Y-%m-%d %H:%M')
        if rec.get('complete') and not dry and (rec.get('public') or not public or short):
            lines.append(f"{'Short' if short else 'Video'} https://youtu.be/{rec['id']}: done")
            continue
        done, waiting, problems = apply_video(yt, rec['id'], out, mp4, w, rec, dry)
        if short and rec.get('publish_at'):
            planned.append(datetime.fromisoformat(rec['publish_at'].replace('Z', '+00:00')))
            if 'publishAt' in ' '.join(done):
                done.append('public on ' + planned[-1].astimezone(ISTANBUL).strftime('%a %d %b %H:%M'))
        if not dry:
            rec['updated'] = datetime.now(ISTANBUL).strftime('%Y-%m-%d %H:%M')
            rec['complete'] = not waiting
            rec['public'] = rec.get('public') or public
            applied[mp4.name] = rec
            (out / APPLIED).write_text(json.dumps(applied, ensure_ascii=False, indent=1), encoding='utf-8')
        complete &= not waiting
        parts = [f"{'Short' if short else 'Video'} https://youtu.be/{rec['id']}"]
        parts.append(('would set: ' if dry else 'set: ') + ', '.join(done) if done else 'up to date')
        if waiting:
            parts.append('later: ' + ', '.join(waiting))
        if problems:
            parts.append('CHECK: ' + '; '.join(problems))
        lines.append(' · '.join(parts))
    if not any('youtu.be' in l for l in lines):
        return None
    lines.append('In Studio: ' + ', '.join(MANUAL))
    summary = ' | '.join(lines)
    log(summary)
    return summary, complete


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['login', 'check', 'apply'], nargs='?', default='check')
    ap.add_argument('lesson', nargs='?')
    ap.add_argument('--video', help='video ID (skip the search)')
    ap.add_argument('--dry-run', action='store_true', help='show what would change, change nothing')
    ap.add_argument('--public', action='store_true', help='also make the video and its Shorts public')
    a = ap.parse_args()
    if a.cmd == 'apply':
        try:
            apply(a.lesson, a.video, a.dry_run, public=a.public)
        except HttpError as e:
            if 'quotaExceeded' not in str(e):
                raise
            sys.exit('YouTube API quota used up for today; run again after midnight Pacific time: 11:00 Istanbul in winter, 10:00 in summer (done parts are kept).')
    else:
        check(api(interactive=a.cmd == 'login'))
