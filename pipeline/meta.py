"""Meta's Graph API pieces that every Meta platform shares (Instagram now; Threads next): the error types and how
publish.py's retry and stop rules read them (classify), reading an answer (answer), and the temporary public link
Meta fetches a video from (Share: a Cloudflare quick tunnel, no account). The account, its key and the posting
itself live in each platform's module (instagram.py)."""
import http.server, re, secrets, shutil, subprocess, tempfile, threading, time
from functools import partial
from pathlib import Path

import requests

CLOUDFLARED = Path(r'D:\YT\tools\cloudflared.exe')


class MetaError(RuntimeError):
    """A Graph API refusal, or Meta unreachable. str() keeps the old text and adds 'subcode N' when one is given:
    'Meta 400: <message> (code 100, subcode 2207085, <user message>)' or 'Meta unreachable: ConnectTimeout'."""
    def __init__(self, message: str, status: int | None = None, code: int | None = None,
                 subcode: int | None = None):
        super().__init__(message)
        self.status, self.code, self.subcode = status, code, subcode


class ContainerError(RuntimeError):
    """Instagram did not finish the upload. .status is 'ERROR', 'EXPIRED' or 'TIMEOUT' (still processing
    after 10 min)."""
    def __init__(self, message: str, status: str):
        super().__init__(message)
        self.status = status


class ShareError(RuntimeError):
    """The temporary public link (cloudflared quick tunnel) could not be made: a problem on the PC."""


def classify(exc: BaseException) -> str:
    """The error kind for the publish module's retry and stop rules, checked in this order:
    ContainerError: 'content' for ERROR, else 'transient'.
    ShareError: 'local'.
    MetaError without status and code (unreachable): 'transient'.
    code 190 or 102: 'auth'.
    subcode 2207085, code 10, code 368, or code 200-299: 'permission'.
    code 4, 17, 32, 613 or 80002, or HTTP 429: 'rate'.
    code 1 or 2, or HTTP 500 and above: 'transient'.
    Any other MetaError (including every other 2207xxx subcode, an assumption): 'unknown'.
    OSError: 'local'.
    Anything else: 'unknown'."""
    if isinstance(exc, ContainerError):
        return 'content' if exc.status == 'ERROR' else 'transient'
    if isinstance(exc, ShareError):
        return 'local'
    if isinstance(exc, MetaError):
        code, status = exc.code, exc.status
        if status is None and code is None:
            return 'transient'
        if code in (190, 102):
            return 'auth'
        if exc.subcode == 2207085 or code in (10, 368) or (code is not None and 200 <= code <= 299):
            return 'permission'
        if code in (4, 17, 32, 613, 80002) or status == 429:
            return 'rate'
        if code in (1, 2) or (status or 0) >= 500:
            return 'transient'
        return 'unknown'
    return 'local' if isinstance(exc, OSError) else 'unknown'


def answer(r):
    """The JSON of a Graph API answer. Raises MetaError with the same text as before, plus the subcode."""
    try:
        j = r.json() if r.headers.get('content-type', '').startswith(('application/json', 'text/javascript')) else {}
    except ValueError:                                    # a broken body: judged by the HTTP status alone
        j = {}
    j = j if isinstance(j, dict) else {}
    if not r.ok or 'error' in j:
        err = j.get('error', {})
        err = err if isinstance(err, dict) else {'message': str(err)}
        sub = err.get('error_subcode')
        raise MetaError(f"Meta {r.status_code}: {err.get('message', r.text[:200])} (code {err.get('code')}"
                        f"{'' if sub is None else f', subcode {sub}'}, {err.get('error_user_msg', '')})",
                        r.status_code, _number(err.get('code')), _number(sub))
    return j


def _number(x):
    """Meta's code or subcode as an int, else None."""
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


class Quiet(http.server.SimpleHTTPRequestHandler):
    """Serves the shared files only: no folder listings (they would reveal the random path)."""
    def log_message(self, *a):
        pass

    def list_directory(self, path):
        self.send_error(404)


class Share:
    """Serve a few files at https://<random>.trycloudflare.com/<random>/<name> while the block runs.
    __enter__ raises ShareError when cloudflared.exe is missing, gives no address within 180 s, or the address
    does not answer; whatever it started is stopped again."""

    def __init__(self, files):
        self.files = [Path(f) for f in files]
        self.proc = self.httpd = None

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ig-'))
        try:
            return self._open()
        except BaseException:
            self.__exit__()
            raise

    def _open(self):
        self.key = secrets.token_urlsafe(16)
        (self.tmp / self.key).mkdir()
        for f in self.files:
            shutil.copy2(f, self.tmp / self.key / f.name)
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(self.tmp)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        port = self.httpd.server_address[1]
        try:
            self.proc = subprocess.Popen([str(CLOUDFLARED), 'tunnel', '--url', f'http://127.0.0.1:{port}',
                                          '--no-autoupdate'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         text=True, errors='replace')
        except OSError as e:
            raise ShareError(f'cloudflared could not start ({type(e).__name__}): is {CLOUDFLARED} there?') from None
        found = []

        def read():
            for line in self.proc.stderr:
                m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if m and not found:
                    found.append(m.group(0))
        threading.Thread(target=read, daemon=True).start()
        end = time.time() + 180                      # it took 73 s once (2026-11-02)
        while not found and time.time() < end:
            time.sleep(0.5)
        if not found:
            raise ShareError('cloudflared gave no public address')
        self.urls = [f'{found[0]}/{self.key}/{f.name}' for f in self.files]
        while time.time() < end:                     # the new address takes a few seconds to reach the internet
            try:
                if requests.head(self.urls[0], timeout=10).status_code == 200:
                    return self.urls
            except requests.RequestException:
                pass
            time.sleep(3)
        raise ShareError('the temporary public address did not answer')

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)
