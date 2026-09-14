"""HTTP and FTP, with writing to the console made unreachable rather than avoided.

webMAN has no separate API. The same URL space that reports the temperature also
mounts discs, deletes files, restarts the system and toggles syscalls, and it
does all of that over GET. A typo in an endpoint name is therefore not a failed
request, it is an action on someone else's console.

So requests are allowlisted, not denylisted. A path that is not on SAFE_PATHS
cannot be fetched at all, whatever calls it, and the allowlist holds only paths
that read. The denied verb check after it is a second net for the case where a
future edit adds something careless to the allowlist.

/syscall8.ps3 is deliberately absent. On several builds fetching it toggles the
syscall state rather than reporting it, and the syscall state is recoverable
from the root page anyway.
"""

import ftplib
import re
import socket
import time
import urllib.error
import urllib.request

from . import VERSION
from .parsers import recode_ftp_line

USER_AGENT = f"ps3-diag/{VERSION}"

DEFAULT_HTTP_TIMEOUT = 20.0
DEFAULT_FTP_TIMEOUT = 30.0


class UnsafeRequest(Exception):
    """Raised before anything leaves this machine."""


# Paths that only report. Ordered roughly as the collectors use them. Anything
# here must be verified to read rather than act before it is added; the README
# lists which of these are still unconfirmed against a real console.
SAFE_PATHS = frozenset({
    "/",
    "/index.ps3",
    "/cpursx.ps3",
    "/setup.ps3",
    "/dev_hdd0",
    "/dev_hdd0/",
})

# Directory listings are open ended, so they are matched by shape instead of
# being listed one by one. Only device roots and paths beneath them, only
# characters that appear in real paths, and no query string.
SAFE_LISTING = re.compile(
    r"^/dev_(?:hdd\d|usb\d{1,3}|sd|ms|cf|bdvd|ntfs\d*)"
    r"(?:/[A-Za-z0-9 _.,'&()\[\]+@!~-]+)*/?$"
)

# Second net. Any of these appearing anywhere in the path means the request is
# refused even if something has put it on the allowlist above.
DENIED_TOKENS = (
    "mount", "umount", "unmount", "delete", "del.", "copy", "paste", "cut",
    "rename", "move", "mkdir", "rmdir", "chmod", "shutdown", "restart",
    "reboot", "poweroff", "standby", "suspend", "eject", "insert", "syscall",
    "install", "uninstall", "refresh", "rebuild", "format", "write", "upload",
    "toggle", "play", "boot", "exec", "launch", "kill", "wake", "update",
    "flash", "blind", "lock", "unlock", "led", "buzzer", "fan.ps3", "set.ps3",
    "dl.ps3", "wm_", "action", "cmd", "pad.ps3", "quit", "exit",
)


def assert_safe_path(path):
    """The only gate. Raises UnsafeRequest, or returns the path unchanged."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise UnsafeRequest(f"not a rooted path: {path!r}")
    if "?" in path or "#" in path or "&" in path.split("/", 1)[0]:
        raise UnsafeRequest(f"query strings are never sent: {path!r}")
    if ".." in path:
        raise UnsafeRequest(f"path traversal: {path!r}")
    lowered = path.lower()
    for token in DENIED_TOKENS:
        if token in lowered:
            raise UnsafeRequest(f"path contains the denied token {token!r}: "
                                f"{path!r}")
    if path in SAFE_PATHS or SAFE_LISTING.match(path):
        return path
    raise UnsafeRequest(f"path is not on the read-only allowlist: {path!r}")


def force_byte_safe(ftp):
    """Make an ftplib connection incapable of a decode error.

    Setting ftp.encoding on its own is not enough. ftplib builds its reader
    once, in connect(), with the encoding in force at that moment:

        self.file = self.sock.makefile("r", encoding=self.encoding)

    so a connection built by somebody else is already wrapped in a strict UTF-8
    decoder and will still die on webMANftpd's 0xb0. The reader has to be
    rebuilt as well. Safe to call straight after login, when nothing is buffered
    in the old wrapper.

    latin-1 is chosen because it maps all 256 byte values, so no reply can fail
    to decode. Filenames are put back through recode_ftp_line afterwards.
    """
    if ftp is None:
        return ftp
    ftp.encoding = "latin-1"
    sock = getattr(ftp, "sock", None)
    if sock is not None:
        try:
            ftp.file = sock.makefile("r", encoding="latin-1")
        except OSError:
            pass
    return ftp


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a request to a URL nobody checked. Refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Response:
    def __init__(self, path, status, body, content_type="", elapsed=0.0,
                 error=None):
        self.path = path
        self.status = status
        self.body = body or ""
        self.content_type = content_type
        self.elapsed = elapsed
        self.error = error

    @property
    def ok(self):
        return self.error is None and self.status == 200 and bool(self.body)

    def __repr__(self):
        return (f"<Response {self.path} status={self.status} "
                f"bytes={len(self.body)} error={self.error!r}>")


class HttpProbe:
    """GET only, one host, allowlisted paths, no redirects, no cookies.

    Every response is kept in self.attempts whether it parsed or not, which is
    what endpoints.txt in the zip is built from. A helper looking at a console
    running a webMAN nobody here has seen gets the raw text even when every
    parser in this package shrugged at it.
    """

    def __init__(self, host, timeout=DEFAULT_HTTP_TIMEOUT, opener=None,
                 log=None):
        self.host = host
        self.timeout = timeout
        self.attempts = []
        self.log = log
        self._opener = opener or urllib.request.build_opener(NoRedirects)

    def get(self, path):
        assert_safe_path(path)
        url = f"http://{self.host}{path}"
        started = time.monotonic()
        request = urllib.request.Request(
            url, method="GET",
            headers={"User-Agent": USER_AGENT, "Connection": "close",
                     "Accept": "*/*"})
        try:
            with self._opener.open(request, timeout=self.timeout) as handle:
                raw = handle.read()
                response = Response(
                    path, getattr(handle, "status", 200),
                    raw.decode("utf-8", errors="replace"),
                    handle.headers.get("Content-Type", ""),
                    time.monotonic() - started)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            response = Response(path, exc.code, body, "",
                                time.monotonic() - started)
        except (urllib.error.URLError, socket.timeout, OSError,
                ValueError) as exc:
            response = Response(path, 0, "", "", time.monotonic() - started,
                                error=str(exc))
        self.attempts.append(response)
        if self.log:
            self.log.event("http", path=path, status=response.status,
                           bytes=len(response.body),
                           content_type=response.content_type,
                           seconds=round(response.elapsed, 2),
                           error=response.error)
        return response

    def try_all(self, paths):
        """Every path that is allowed, skipping the ones that are not.

        A path being off the allowlist is a bug in the collector, not a console
        problem, so it is recorded and stepped over rather than raised: one bad
        candidate must not lose the seven good ones beside it.
        """
        out = []
        for path in paths:
            try:
                assert_safe_path(path)
            except UnsafeRequest as exc:
                if self.log:
                    self.log.event("refused", path=path, reason=str(exc))
                continue
            out.append(self.get(path))
        return out


# --- FTP -------------------------------------------------------------------

# The only paths whose contents may be fetched. Listings are unrestricted;
# actually pulling bytes down is not. Game ISOs are enormous and their contents
# are none of a diagnostic's business, so nothing outside this list is ever
# downloaded, only counted and sized.
DOWNLOADABLE = (
    re.compile(r"^/dev_hdd0/crash_report/[^/]+$"),
    re.compile(r"^/dev_hdd0/boot_plugins(?:_nocobra)?\.txt$"),
    re.compile(r"^/dev_flash/vsh/etc/version\.txt$"),
    re.compile(r"^/dev_hdd0/tmp/wm(?:an|tmp)?[^/]*\.txt$"),
)

MAX_DOWNLOAD_BYTES = 512 * 1024


def may_download(path):
    return any(pattern.match(path) for pattern in DOWNLOADABLE)


# Ranged reads, not downloads. Identifying a disc image reads a few aligned 32 KB
# blocks out of it and aborts the transfer; it never pulls the file. That is a
# different permission from DOWNLOADABLE and is kept separate so download_text()
# carries on refusing game images outright.
RANGED_READABLE = (
    re.compile(r"(?i)^/dev_(?:hdd\d|usb\d{1,3}|sd|ms|cf|ntfs\d*)"
               r"(?:/[^/]+)*/[^/]+\.iso$"),
)


def may_range_read(path):
    return any(pattern.match(path) for pattern in RANGED_READABLE)


# Small binary files that may be read whole. Separate from DOWNLOADABLE because
# that one decodes what it fetches as text, which corrupts a structured file:
# PARAM.SFO is a binary key/value blob and the only place a title update version
# is written down anywhere on the console's disk.
BYTE_READABLE = (
    re.compile(r"(?i)^/dev_hdd0/game/[A-Z]{4}\d{5}/PARAM\.SFO$"),
    re.compile(r"(?i)^/dev_hdd0/game/[A-Z]{4}\d{5}/USRDIR/PARAM\.SFO$"),
)

MAX_BYTE_READ = 64 * 1024


def may_read_bytes(path):
    return any(pattern.match(path) for pattern in BYTE_READABLE)


class FtpLister:
    """LIST and a tightly limited RETR. Nothing that changes anything.

    ftplib will happily send DELE or STOR if asked. Nothing here asks, and the
    only two methods exposed are the two that read, so a future collector cannot
    reach a writing command without adding one to this class on purpose.
    """

    def __init__(self, host, timeout=DEFAULT_FTP_TIMEOUT, factory=None,
                 log=None):
        self.host = host
        self.timeout = timeout
        self.log = log
        # webMANftpd announces its own version and its NTFS mount state in the
        # greeting, which is a free version fingerprint costing no request.
        self.banner = ""
        self._factory = factory or self._connect
        self._ftp = None

    def _connect(self):
        """Anonymous, and only anonymous.

        webMANftpd answers USER anonymous with 331 and accepts any password.
        The previous version of this tried three credential pairs in turn, which
        was worse than useless: a server that refuses a login usually drops the
        control connection with it, so the second and third attempts failed
        against a dead socket and the real reason was replaced by a made up
        "no accepted login". One attempt, and whatever the server actually said
        comes back to the caller.
        """
        # latin-1, not UTF-8. webMANftpd puts a 0xb0 byte in its own status
        # messages, and ftplib has decoded the control connection as strict
        # UTF-8 since Python 3.9, so every single LIST died with
        # "'utf-8' codec can't decode byte 0xb0" before it returned a line.
        # latin-1 cannot fail: it maps all 256 byte values. Filenames are put
        # back through recode_ftp_line afterwards.
        ftp = ftplib.FTP(encoding="latin-1")
        ftp.connect(self.host, 21, timeout=self.timeout)
        self.banner = (ftp.getwelcome() or "").strip()
        ftp.login("anonymous", "anonymous@")
        if self.log:
            self.log.event("ftp_login", banner=self.banner)
        return ftp

    def open(self):
        if self._ftp is None:
            # Forced here rather than only in _connect, because the factory is
            # injectable and a caller that built the connection itself would
            # otherwise get ftplib's strict UTF-8 default back. One byte of
            # webMANftpd status text is enough to break every listing, so this
            # is not a setting to leave to whoever happens to construct it.
            self._ftp = force_byte_safe(self._factory())
            # Read here rather than only in _connect, so the greeting is
            # captured whoever built the connection. The GUI uses _connect; the
            # tests inject a factory of their own and must not lose it.
            if not self.banner:
                self.banner = recode_ftp_line(
                    (self._ftp.getwelcome() or "").strip())
        return self._ftp

    def _command(self, run):
        """One FTP command, with a single reconnect if the link has died.

        A console that has been sat idle, or that is busy with something else,
        drops the control connection partway through a run. Without this the
        first collector to hit that takes every later one down with it, which
        looks exactly like the console having nothing to report. One retry on a
        fresh connection, and a second failure is real.
        """
        try:
            return run(self.open())
        except ftplib.error_perm:
            # A refusal is an answer: the path is not there, or is not allowed.
            # Reconnecting would ask the same question and get the same reply.
            raise
        except ftplib.all_errors as exc:
            if self.log:
                self.log.event("ftp_reconnect", reason=exc.__class__.__name__)
            self.close()
            return run(self.open())

    def list_dir(self, path):
        """Raw LIST output for one directory, as text. Raises on failure."""
        started = time.monotonic()

        def run(ftp):
            lines = []
            ftp.retrlines(f"LIST {path}", lines.append)
            return lines

        lines = [recode_ftp_line(line) for line in self._command(run)]
        if self.log:
            self.log.event("ftp_list", path=path, lines=len(lines),
                           seconds=round(time.monotonic() - started, 2))
        return "\n".join(lines)

    def download_text(self, path, max_bytes=MAX_DOWNLOAD_BYTES):
        """Contents of one small text file, if it is on the download allowlist."""
        if not may_download(path):
            raise UnsafeRequest(f"not on the download allowlist: {path!r}")
        chunks = []
        total = 0
        truncated = False

        def take(block):
            nonlocal total, truncated
            if total >= max_bytes:
                truncated = True
                return
            chunks.append(block[:max_bytes - total])
            total += len(block)

        self._command(lambda ftp: ftp.retrbinary(f"RETR {path}", take,
                                                 blocksize=8192))
        text = b"".join(chunks).decode("utf-8", errors="replace")
        if truncated or total > max_bytes:
            text += f"\n\n[truncated by ps3-diag at {max_bytes} bytes]\n"
        if self.log:
            self.log.event("ftp_read", path=path, bytes=total,
                           truncated=truncated)
        return text

    def download_bytes(self, path, max_bytes=MAX_BYTE_READ):
        """One small binary file, verbatim, if it is on the byte allowlist.

        Kept apart from download_text because that decodes as UTF-8 with
        replacement, which silently mangles anything that is not text. A
        PARAM.SFO read that way parses as nonsense rather than failing, which is
        the worst of the available outcomes.
        """
        if not may_read_bytes(path):
            raise UnsafeRequest(f"not on the byte-read allowlist: {path!r}")
        chunks = []
        total = 0

        def take(block):
            nonlocal total
            if total >= max_bytes:
                return
            chunks.append(block[:max_bytes - total])
            total += len(block)

        self._command(lambda ftp: ftp.retrbinary(f"RETR {path}", take,
                                                 blocksize=8192))
        if self.log:
            self.log.event("ftp_read_bytes", path=path, bytes=total)
        return b"".join(chunks)

    def close(self):
        if self._ftp is not None:
            # all_errors is itself a tuple of (Error, OSError, EOFError), and a
            # tuple nested inside an except clause is a TypeError at the moment
            # it fires rather than at import, so this went unnoticed until a
            # connection actually had to be closed after dying.
            try:
                self._ftp.quit()
            except ftplib.all_errors:
                pass
            try:
                self._ftp.close()
            except ftplib.all_errors:
                pass
            self._ftp = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def tcp_open(host, port=80, timeout=0.4):
    """True if something accepts a connection. Used only by discovery."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
