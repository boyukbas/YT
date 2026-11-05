"""Imported first (tests/__init__.py): every test runs offline and away from the real PC state.
- PIPELINE_WORK, USERPROFILE and HOME point into a fresh temp folder; set before any pipeline import.
- Sockets may connect to loopback addresses only; anything else raises NetworkBlocked.
- Subprocesses named cloudflared or schtasks raise NetworkBlocked.
- instagram.call, instagram.Share and youtube.api are replaced by fakes that raise NetworkBlocked unless a
  test scripts them.
- tools.probe (and instagram.probe, dashboard.probe) answer from fixtures.PROBES for files under TMP; any
  other path uses the real ffprobe (only the real-data dry run).

Blocked the same way: DNS lookups of other names, binding a socket to a non-loopback address, those programs
started through cmd, PowerShell (also the ScheduledTask cmdlets) or os.system, os.startfile and webbrowser.open,
and the claude CLI (llm.py's cli backend would make a real call). llm.py runs in manual mode (PIPELINE_LLM=manual;
ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN are dropped). Proxy variables are dropped too (NO_PROXY=*), so a
local proxy cannot carry a request out.
A child Python process inherits PIPELINE_WORK, USERPROFILE, HOME and PIPELINE_TEST_TMP: importing the tests
package there (pipeline folder on sys.path) installs the guard on the same TMP.
Pipeline modules imported before the guard (dashboard.py serve --dev) keep their PIPELINE_WORK, which must not be
the real work folder (install() refuses); their key paths move into HOME.
TMP is deleted when the run ends, unless KEEP_TEST_TMP is set."""
import atexit, copy, ipaddress, os, re, shutil, socket, stat, subprocess, sys, tempfile, threading, time, webbrowser
from pathlib import Path

if __name__ != 'tests.guard':            # one guard per process, installed by the tests package
    raise ImportError('import the guard as tests.guard: python -I -m unittest discover -s <pipeline>\\tests '
                      '-t <pipeline>')

PIPELINE = Path(__file__).resolve().parents[1]
REAL_WORK = Path(r'D:\YT\easy-work')                 # the PC's real work folder: tests never use it
BLOCKED = ('cloudflared', 'schtasks', 'claude')      # program names, without folder and .exe
SHELLS = ('cmd', 'powershell', 'pwsh', 'bash', 'sh', 'wsl', 'conhost', 'start', 'env')
LOOPBACK = (ipaddress.ip_address('127.0.0.1'), ipaddress.ip_address('::1'))
MARK = '.pipeline-test-tmp'                          # marks a TMP made here (a child process may reuse it)

TMP: Path
WORK: Path
HOME: Path
REAL = {}            # the originals replaced here: 'tools.probe', 'instagram.call', 'instagram.Share', 'youtube.api'
FAKE_ROOTS = []      # more folders the fake probe answers for (fixtures.make_root() roots outside TMP)
_real = {}           # the original socket, subprocess and os functions
_installed = False


class NetworkBlocked(RuntimeError):
    """A test tried to reach the network, or to start cloudflared, schtasks or claude."""


def install() -> None:
    """Apply everything above. Idempotent."""
    global TMP, WORK, HOME, _installed
    if _installed:
        return
    early = [m for m in list(sys.modules.values()) if _from_pipeline(m)]
    real_keys = Path.home() / '.shorts-pipeline'
    TMP = _tmp()
    work = os.environ.get('PIPELINE_WORK')
    if early and work and _odd(work):
        raise RuntimeError(f'guard: PIPELINE_WORK {work!r} is a network or namespace path (modules were imported '
                           'before the guard); set it to a plain scratch folder (D:\\...)')
    if early and work and not _inside(work, REAL_WORK):
        WORK = Path(_plain(work)).resolve()          # the folder those modules already use (dashboard --dev)
        if _inside(WORK, REAL_WORK):                 # through a link: still the real one
            raise RuntimeError(f'guard: PIPELINE_WORK {work!r} resolves to the real work folder {REAL_WORK}; '
                               'set it to a scratch folder first')
    else:
        WORK = TMP / 'work'
    HOME = TMP / 'home'
    for p in (WORK, HOME):
        p.mkdir(parents=True, exist_ok=True)
    os.environ.update(PIPELINE_WORK=str(WORK), USERPROFILE=str(HOME), HOME=str(HOME), PIPELINE_TEST_TMP=str(TMP))
    for k in [k for k in os.environ if k.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'anthropic_api_key',
                                                     'claude_code_oauth_token')]:
        del os.environ[k]
    os.environ.update(NO_PROXY='*', no_proxy='*', PIPELINE_LLM='manual')
    _block_network()
    _block_programs()
    if str(PIPELINE) not in sys.path:
        sys.path.insert(0, str(PIPELINE))
    import tools                                     # stable: a failure here stops the run
    for name in ('instagram', 'youtube'):            # being edited by other developers: a broken one fails only
        try:                                         # the tests that use it; reset() fakes it once it imports
            __import__(name)
        except Exception as e:
            print(f'guard: {name} not importable ({type(e).__name__}: {e})', file=sys.stderr)
    _redirect(early, real_keys)
    _apply_fakes()
    _installed = True


def script_meta(responses: list) -> list[tuple]:
    """Make instagram.call answer from responses in order (a dict is returned, an exception is raised).
    Returns the list that records every (method, path, params) call."""
    import instagram
    calls, todo, lock = [], list(responses), threading.Lock()

    def call(method, path, files=None, **params):
        with lock:
            calls.append((method, path, {**params, **({'files': files} if files is not None else {})}))
            if not todo:
                raise NetworkBlocked(f'instagram.call({method} {path}): no scripted answer left')
            r = todo.pop(0)
        if isinstance(r, BaseException) or (isinstance(r, type) and issubclass(r, BaseException)):
            raise r
        return copy.deepcopy(r)
    instagram.call = call
    return calls


def fake_share(urls: tuple[str, str] = ('https://fake.trycloudflare.com/k/video.mp4',
                                        'https://fake.trycloudflare.com/k/cover.jpg')) -> None:
    """Make instagram.Share a context manager that yields urls and starts nothing."""
    import instagram

    class FakeShare:
        shared = []                                  # the files of every Share block, for checks

        def __init__(self, files):
            self.files = [Path(f) for f in files]

        def __enter__(self):
            FakeShare.shared.append(self.files)
            return list(urls)

        def __exit__(self, *exc):
            return False
    instagram.Share = FakeShare


def reset() -> None:
    """Undo scripted fakes, call publish.set_registry(None), and empty WORK (use in setUp).
    HOME is emptied too, so a fake key file written by one test never reaches the next."""
    _apply_fakes()
    publish = sys.modules.get('publish')             # its registry exists only once a test imported it
    if publish is not None and hasattr(publish, 'set_registry'):
        publish.set_registry(None)
    for folder in (WORK, HOME):
        _empty(folder)


# ---------- the fakes ----------

def _no_meta(method, path, files=None, **params):
    """instagram.call until a test scripts it."""
    raise NetworkBlocked(f'instagram.call({method} {path}): no Meta in tests; script it with guard.script_meta()')


class BlockedShare:
    """instagram.Share until fake_share(): entering it would start cloudflared."""
    def __init__(self, files):
        self.files = [Path(f) for f in files]

    def __enter__(self):
        raise NetworkBlocked('instagram.Share would start cloudflared: call guard.fake_share() first')

    def __exit__(self, *exc):
        return False


def _no_youtube(interactive=False):
    """youtube.api until a test replaces it."""
    raise NetworkBlocked('youtube.api(): no Google in tests; use fixtures.fake_youtube_api()')


def _probe(path):
    """tools.probe in tests: fixtures.PROBES (by file name) for videos under TMP or a fixture root, else the real
    ffprobe. A missing or unknown fixture file fails like ffprobe does (RuntimeError)."""
    p = Path(path)
    full = p.resolve()
    if not any(full.is_relative_to(r) for r in (TMP, *FAKE_ROOTS)):
        return REAL['tools.probe'](path)
    from . import fixtures
    if not full.is_file():
        raise RuntimeError(f'ffprobe.exe failed (1):\n{p}: No such file or directory')
    if p.name not in fixtures.PROBES:
        raise RuntimeError(f'ffprobe.exe failed (1):\n{p}: Invalid data found when processing input '
                           '(no entry in tests.fixtures.PROBES)')
    return dict(fixtures.PROBES[p.name])


def _apply_fakes():
    """The blocking fakes and the fake probe, in every module that has them (install and reset)."""
    tools, ig, yt = (sys.modules.get(n) for n in ('tools', 'instagram', 'youtube'))
    if tools:
        real = REAL.setdefault('tools.probe', tools.probe)
        for mod in list(sys.modules.values()):       # tools, and "from tools import probe" (instagram, dashboard, ...)
            d = getattr(mod, '__dict__', None)
            if isinstance(d, dict) and d.get('probe') is real:
                d['probe'] = _probe
    if ig:
        REAL.setdefault('instagram.call', ig.call)
        REAL.setdefault('instagram.Share', ig.Share)
        ig.call, ig.Share = _no_meta, BlockedShare
    if yt:
        REAL.setdefault('youtube.api', yt.api)
        yt.api = _no_youtube


# ---------- network and programs ----------

def _loopback(host):
    """True for localhost, 127.0.0.1 and ::1 (any spelling of those addresses)."""
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    if not isinstance(host, str):
        return False
    h = host.strip().strip('[]').rstrip('.').lower().split('%')[0]
    if h == 'localhost':
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (getattr(ip, 'ipv4_mapped', None) or ip) in LOOPBACK


def _host(host, what):
    if not _loopback(host):
        raise NetworkBlocked(f'tests may only {what} localhost, 127.0.0.1 or ::1, not {host!r}')


def _check(sock, address, what):
    if sock.family in (socket.AF_INET, socket.AF_INET6):         # AF_UNIX and friends stay on the PC
        _host(address[0] if isinstance(address, tuple) and address else address, what)


def _block_network():
    S = socket.socket
    _real.update(connect=S.connect, connect_ex=S.connect_ex, bind=S.bind, sendto=S.sendto,
                 getaddrinfo=socket.getaddrinfo, getnameinfo=socket.getnameinfo,
                 gethostbyname=socket.gethostbyname, gethostbyname_ex=socket.gethostbyname_ex,
                 gethostbyaddr=socket.gethostbyaddr)

    def connect(self, address):
        _check(self, address, 'connect to')
        return _real['connect'](self, address)

    def connect_ex(self, address):
        _check(self, address, 'connect to')
        return _real['connect_ex'](self, address)

    def bind(self, address):
        _check(self, address, 'listen on')
        return _real['bind'](self, address)

    def sendto(self, data, *args):
        _check(self, args[-1] if args else None, 'send to')
        return _real['sendto'](self, data, *args)

    def getaddrinfo(host, *args, **kw):
        if host is not None:                         # None: a local wildcard, no lookup
            _host(host, 'look up')
        return _real['getaddrinfo'](host, *args, **kw)

    def getnameinfo(sockaddr, flags):
        _host(sockaddr[0], 'look up')
        return _real['getnameinfo'](sockaddr, flags)

    def lookup(name):
        def by_name(host, *args):
            _host(host, 'look up')
            return _real[name](host, *args)
        return by_name

    S.connect, S.connect_ex, S.bind, S.sendto = connect, connect_ex, bind, sendto
    socket.getaddrinfo, socket.getnameinfo = getaddrinfo, getnameinfo
    for name in ('gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
        setattr(socket, name, lookup(name))


def _words(text):
    return [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', text)]


def _name(word):
    """The program name in a word: 'D:\\YT\\tools\\cloudflared.exe' -> 'cloudflared'."""
    base = re.split(r'[\\/]', str(word).strip().strip('"\''))[-1].lower()
    stem, ext = os.path.splitext(base)
    return stem if ext in ('.exe', '.com', '.bat', '.cmd', '.ps1') else base


def _program(args, executable, shell):
    """NetworkBlocked when a command would start cloudflared or schtasks (also through a shell: there every
    word counts, split at spaces, quotes and the shell's separators, so 'cmd /c "x && schtasks"' is caught)."""
    one = isinstance(args, (str, bytes, os.PathLike))
    parts = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in ([args] if one else args)]
    first = (_words(parts[0])[:1] if one else parts[:1]) if parts else []
    prog = _name(executable) if executable else _name(first[0]) if first else ''
    words = [w for p in parts for w in _words(p) + re.split(r'[\s&|;()<>^"\'`,]+', p) if w]
    names = [prog] + ([_name(w) for w in words] if shell or prog in SHELLS else [])
    hit = next((n for n in names if n in BLOCKED or 'scheduledtask' in n), None)
    if hit:
        raise NetworkBlocked(f'tests never start {hit} (cloudflared, schtasks and claude are blocked)')


def _block_programs():
    _real.update(popen_init=subprocess.Popen.__init__, system=os.system)

    def popen_init(self, args, *rest, **kw):
        if not isinstance(args, (str, bytes, os.PathLike, list, tuple)):
            args = list(args)
        _program(args, kw.get('executable', rest[1] if len(rest) > 1 else None),
                 kw.get('shell', rest[7] if len(rest) > 7 else False))
        _real['popen_init'](self, args, *rest, **kw)

    def system(command):
        _program(command, None, True)
        return _real['system'](command)

    def refuse(what):
        def no(target, *args, **kw):
            raise NetworkBlocked(f'{what}({target!r}): tests never open a browser or hand things to the shell')
        return no

    subprocess.Popen.__init__ = popen_init
    os.system = system
    if hasattr(os, 'startfile'):
        os.startfile = refuse('os.startfile')
    webbrowser.open = webbrowser.open_new = webbrowser.open_new_tab = refuse('webbrowser.open')


# ---------- folders ----------

def _plain(path):
    """A path's ordinary spelling, without disk access: '/' as '\\', the \\\\?\\, \\\\.\\ and \\??\\ prefixes dropped
    (\\\\?\\UNC\\srv\\x -> \\\\srv\\x), stream suffixes cut (D:\\x::$INDEX_ALLOCATION -> D:\\x)."""
    s = os.fspath(path).replace('/', '\\')
    if s[:8].upper() == '\\\\?\\UNC\\':
        s = '\\' + s[7:]
    elif s[:4] in ('\\\\?\\', '\\\\.\\', '\\??\\'):
        s = s[4:]
    head = s[:2] if re.match(r'[A-Za-z]:', s) else ''
    return head + re.sub(r':[^\\]*', '', s[len(head):])


def _odd(path):
    """True for a spelling _plain cannot vouch for: a network path (\\\\server\\share) or a namespace one that does
    not name a drive (\\\\?\\GLOBALROOT\\..., \\\\.\\Volume{...}\\...)."""
    raw = os.fspath(path).replace('/', '\\')
    return (raw.startswith('\\\\') or raw.startswith('\\??\\')) and not re.match(r'[A-Za-z]:\\', _plain(raw))


def _inside(path, folder):
    """True when path is folder or below it, in any spelling of either (see _plain). No disk access, so the real
    key folder is never opened."""
    p, f = (os.path.normcase(os.path.abspath(_plain(x))) for x in (path, folder))
    return p == f or p.startswith(f.rstrip('\\/') + os.sep)


def _tmp():
    """The parent test process's TMP (inherited PIPELINE_TEST_TMP), else a fresh one, deleted at exit."""
    old = os.environ.get('PIPELINE_TEST_TMP')
    if old and (Path(old) / MARK).is_file():
        return Path(old).resolve()
    tmp = Path(tempfile.mkdtemp(prefix='yt-tests-')).resolve()
    (tmp / MARK).write_text('made by pipeline/tests/guard.py; deleted after the test run\n', encoding='utf-8')
    atexit.register(lambda: os.environ.get('KEEP_TEST_TMP') or shutil.rmtree(tmp, ignore_errors=True))
    return tmp


def _from_pipeline(mod):
    f = getattr(mod, '__file__', None)
    return bool(f) and Path(f).resolve().parent == PIPELINE


def _redirect(mods, real_keys):
    """Modules imported before the guard: a path in the real work folder refuses; key paths move into HOME."""
    keys = HOME / '.shorts-pipeline'
    for mod in mods:
        for name, val in list(vars(mod).items()):
            vals = val if isinstance(val, list) else [val]
            if not vals or not all(isinstance(v, Path) for v in vals):
                continue
            if any(_inside(v, REAL_WORK) for v in vals):
                raise RuntimeError(f'guard: {mod.__name__}.{name} is in the real work folder {REAL_WORK} (imported '
                                   'before the guard); set PIPELINE_WORK to a scratch folder first')
            moved = [keys / os.path.relpath(v, real_keys) if _inside(v, real_keys) else v for v in vals]
            if moved != vals:
                setattr(mod, name, moved if isinstance(val, list) else moved[0])


def _writable(func, path, exc_info):
    """shutil.rmtree helper: a read-only file is made writable and removed; one already gone is fine."""
    if issubclass(exc_info[0], FileNotFoundError):
        return
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _empty(folder):
    """Delete everything in folder. Windows can hold a just-closed file for a moment, so retry briefly."""
    folder.mkdir(parents=True, exist_ok=True)
    for wait in (0.05, 0.1, 0.2, 0.4, 0.8, None):
        try:
            for p in list(folder.iterdir()):
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, onerror=_writable)
                    continue
                try:
                    p.unlink(missing_ok=True)
                except PermissionError:
                    os.chmod(p, stat.S_IWRITE)
                    p.unlink(missing_ok=True)
            return
        except PermissionError as e:
            if wait is None:
                raise RuntimeError(f'guard.reset: cannot empty {folder}: {e} (is a file there still open?)') from None
            time.sleep(wait)
