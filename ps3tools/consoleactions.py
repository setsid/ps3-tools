"""The one HTTP client in this program that asks a console to *do* something.

ps3diag.transport is the read-only client and stays that way. This module is
its opposite number, kept in a separate file for the same reason the write FTP
client is: a promise that a code path cannot act is only worth something while
there is no way for it to reach one that can.

webMAN has no API. The same GET-only URL space that reports the temperature
also mounts discs, deletes files, rebuilds the database and restores the
console to factory settings, and it does all of it over a plain GET with no
confirmation of any kind. Two of those live one path segment away from the only
call this program needs:

    /install.ps3/dev_hdd0/packages   install everything in that folder
    /recovery.ps3                    boot into recovery
    /rebuild.ps3                     rebuild the database

So the allowlist here is **exact match against a frozen set of complete
paths**. Not a prefix, not a regular expression, not a starts-with, and not a
"clean the path and see". A prefix test on "/install.ps3" accepts
"/install.ps3/../recovery.ps3" and a starts-with test on "/install" accepts
anything at all that begins with those eight characters. Exact match is the
only shape of this check that cannot be talked round, which is why it is the
shape it has.

There is one entry on it. Adding a second one is a decision somebody makes
deliberately, in this file, with the tests below in front of them.

Everything that leaves this machine goes through an injected opener. The
default builds a urllib opener that refuses redirects; the tests pass one of
their own and never get near urllib, because there is a live console on this
network and tests/check-no-network.py fails the build if a single packet
leaves it.
"""

import socket
import urllib.error
import urllib.request

from ps3diag.transport import NoRedirects

#: webMAN answers slowly while it is busy, and installing is busy. Long enough
#: that a console mid-install is not mistaken for a console that has gone.
DEFAULT_TIMEOUT = 30.0

USER_AGENT = "ps3-tools-updates"

#: The folder webMAN installs from. The endpoint installs the whole folder;
#: there is no form of this call that names one file, which is why the flow in
#: ps3tools.updates lists the folder first and refuses to go on when it holds
#: something this program did not put there.
PACKAGES_PATH = "/dev_hdd0/packages"

#: The install call, written out whole. See the module docstring: this is
#: compared with == and with nothing else.
INSTALL_PACKAGES = "/install.ps3/dev_hdd0/packages"

#: Every path this client may ever request. Exact match, complete paths.
#:
#: Never a prefix. /recovery.ps3 and /rebuild.ps3 are one segment away from the
#: entry above and neither of them is recoverable by the person this program is
#: written for.
ALLOWED_ACTIONS = frozenset({INSTALL_PACKAGES})


class ActionRefused(Exception):
    """The path was not on the allowlist. Raised before anything is sent."""


class ActionFailed(Exception):
    """The console did not do it, or did not say that it had.

    The message is already fit to show somebody with no technical knowledge.
    """


def assert_allowed(path):
    """The only gate. Returns the path unchanged, or raises ActionRefused.

    The three checks before the allowlist are not what makes this safe -- the
    exact match is -- but they turn a refusal into a sentence that says which
    kind of mistake was made, and they mean a future edit that loosens the
    allowlist into a pattern still cannot smuggle a query string or a "..".
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise ActionRefused(f"not a rooted path: {path!r}")
    if "?" in path or "#" in path or "&" in path:
        raise ActionRefused(f"query strings are never sent: {path!r}")
    if ".." in path:
        raise ActionRefused(f"path traversal: {path!r}")
    if path not in ALLOWED_ACTIONS:
        raise ActionRefused(f"not on the action allowlist: {path!r}")
    return path


class ActionResponse:
    """What came back. Kept whole so a caller can say what was unexpected."""

    def __init__(self, path, status, body=""):
        self.path = path
        self.status = status
        self.body = body or ""

    @property
    def ok(self):
        return self.status == 200

    def __repr__(self):
        return (f"<ActionResponse {self.path} status={self.status} "
                f"bytes={len(self.body)}>")


class ConsoleActions:
    """Asks one console to do one thing, over one allowlisted path.

    opener is the seam. Anything with .open(request, timeout=...) will do, and
    every test passes one, so no test can reach a console by accident.
    """

    def __init__(self, host, timeout=DEFAULT_TIMEOUT, opener=None, log=None):
        self.host = (host or "").strip()
        self.timeout = timeout
        self.log = log
        self.requests = []
        self._opener = opener or urllib.request.build_opener(NoRedirects)

    def _get(self, path):
        assert_allowed(path)
        if not self.host:
            raise ActionFailed(
                "No console address has been entered, so there is nothing to "
                "send this to.")
        url = f"http://{self.host}{path}"
        request = urllib.request.Request(
            url, method="GET",
            headers={"User-Agent": USER_AGENT, "Connection": "close",
                     "Accept": "*/*"})
        self.requests.append(path)
        try:
            with self._opener.open(request, timeout=self.timeout) as handle:
                raw = handle.read()
                status = getattr(handle, "status", 200)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            return ActionResponse(path, exc.code, body)
        except (urllib.error.URLError, socket.timeout, OSError,
                ValueError) as exc:
            # The console being switched off partway through is the expected
            # way this fails, so it is a sentence rather than a traceback.
            raise ActionFailed(
                f"The console stopped answering. Check it is still switched "
                f"on, still on the same network and still sitting on its main "
                f"menu. ({exc.__class__.__name__})") from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if self.log:
            self.log.event("console_action", path=path, status=status)
        return ActionResponse(path, status, raw)

    def install_packages(self):
        """Ask the console to install everything in /dev_hdd0/packages.

        UNTESTED AGAINST A CONSOLE. Nobody has ever fired this call. It is
        written from webMAN's documented endpoint list and it has never been
        watched working, in the same way the scetool invocation was written
        from two readmes and never run on the machine these were written on.
        Treat a success here as "the console answered 200", which is all it
        is: webMAN answers before the installer has finished, and there is no
        endpoint that reports how an install went.

        It installs the *whole folder*. The caller is responsible for knowing
        what is in it -- see ps3tools.updates.inspect_packages_folder.
        """
        response = self._get(INSTALL_PACKAGES)
        if not response.ok:
            raise ActionFailed(
                f"The console answered {response.status} when it was asked to "
                f"install the update. The update file has been copied across "
                f"and is still there: you can install it yourself from the "
                f"console, under Package Manager.")
        return response
