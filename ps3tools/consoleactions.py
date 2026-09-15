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

    /install.ps3/dev_hdd0/packages/NAME.pkg   install that one package
    /recovery.ps3                             boot into recovery
    /rebuild.ps3                              rebuild the database

So the allowlist here is **one complete prefix, written out whole, followed by
one path segment that has to look exactly like a package name**. Not a prefix
test on its own, not a general regular expression over the whole path, and not
a "clean the path and see". A prefix test alone accepts
"/install.ps3/dev_hdd0/packages/../../recovery.ps3", which is why the segment
after the prefix is matched against a pattern that cannot contain a slash, a
dot pair, or anything but the characters a PS3 package name is made of.

Everything the check rejects, it rejects before a socket is opened.

The call names **one file**. It used to name the folder, which was read off
webMAN's documented endpoint list and turned out to do nothing at all: that URL
returns a page with a dropdown, and the browser appends the chosen filename in
JavaScript before fetching it --

    install.ps3/dev_hdd0/packages/'+this.value;

-- which is how the real shape of the call was established, on hardware.

Everything that leaves this machine goes through an injected opener. The
default builds a urllib opener that refuses redirects; the tests pass one of
their own and never get near urllib, because there is a live console on this
network and tests/check-no-network.py fails the build if a single packet
leaves it.
"""

import re
import socket
import urllib.error
import urllib.request

from ps3diag.transport import NoRedirects

#: webMAN answers slowly while it is busy, and installing is busy. Long enough
#: that a console mid-install is not mistaken for a console that has gone.
DEFAULT_TIMEOUT = 30.0

#: Enough of the answer to read a reason out of, without putting a whole page
#: into the log. webMAN's replies to this call are a couple of lines.
MAX_LOGGED_BODY = 2000

USER_AGENT = "ps3-tools-updates"

#: The folder webMAN installs from. The endpoint installs the whole folder;
#: there is no form of this call that names one file, which is why the flow in
#: ps3tools.updates lists the folder first and refuses to go on when it holds
#: something this program did not put there.
PACKAGES_PATH = "/dev_hdd0/packages"

#: The one call this client makes, up to but not including the file name.
#: Written out whole. Everything before the last slash is fixed.
INSTALL_PREFIX = "/install.ps3" + PACKAGES_PATH + "/"

#: What may follow it: one segment, and it has to look like a package.
#:
#: No slash, so it cannot walk anywhere. No leading dot, so "..", ".." dressed
#: up, and hidden files are all out. It must end .pkg, which is the only kind
#: of file this call is for. /recovery.ps3 and /rebuild.ps3 are one segment
#: away from this prefix and neither of them is recoverable by the person this
#: program is written for, so the gate is deliberately tighter than it needs
#: to be rather than looser.
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}\.pkg$")


def install_path(filename):
    """The install URL for one package. Raises ActionRefused if it is not one.

    Built here rather than by a caller sticking strings together, so there is
    one place that decides what this URL may look like.
    """
    name = (filename or "").strip()
    if not PACKAGE_NAME.match(name):
        raise ActionRefused(f"not a package file name: {filename!r}")
    return INSTALL_PREFIX + name


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
    if not path.startswith(INSTALL_PREFIX):
        raise ActionRefused(f"not on the action allowlist: {path!r}")
    if not PACKAGE_NAME.match(path[len(INSTALL_PREFIX):]):
        raise ActionRefused(f"not a package file name: {path!r}")
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
            # The body is recorded, not just the status. webMAN answers 200 to
            # the install call and then does nothing, so whatever reason it
            # gives is in the body or nowhere, and a diagnostic taken
            # afterwards is the only place anybody will see it.
            self.log.event("console_action", path=path, status=status,
                           body=raw[:MAX_LOGGED_BODY])
        return ActionResponse(path, status, raw)

    def install_package(self, filename):
        """Ask the console to install one package out of its packages folder.

        Confirmed on hardware: the console puts the install up on screen, and
        the user presses O when it finishes.

        The earlier version of this named the folder and nothing else, which
        did nothing at all -- see the module docstring. A 200 means the console
        accepted the request; what happens on the television after that is the
        console's business and there is no endpoint that reports on it.

        What happens if a second request arrives while the first install
        dialog is still up is NOT established: it may queue, it may be
        ignored, it may stack. Until somebody has watched it, callers install
        one package at a time and wait for the user to say the console has
        finished.
        """
        path = install_path(filename)
        response = self._get(path)
        if not response.ok:
            raise ActionFailed(
                f"The console answered {response.status} when it was asked to "
                f"install {filename}. The file has been copied across and is "
                f"still there: you can install it yourself from the console, "
                f"under Package Manager.")
        return response
