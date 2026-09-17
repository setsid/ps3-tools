"""The update check. Pure logic: no Qt, no threads, no side effects on import.

This is the first outbound call the program has ever made. The diagnostic half
of it is built on a promise that it only reads, and the patcher's promise is
that it writes to one folder on one console; neither of them talks to anything
on the internet. So this module is deliberately narrow, and every rule below
exists because the alternative would quietly widen it:

  * One host, one URL, built from one constant. Nothing here takes a URL from
    a server's answer and fetches it as part of the check.
  * Every network call goes through an injected fetcher. The default is
    urllib, but no test ever gets that far: the suite injects, so nothing in
    it can reach the network even by mistake. There is a live console on this
    LAN and tests/check-no-network.py fails the build if anything leaves it.
  * It never downloads on its own and it never replaces the running exe. A
    process rewriting its own image on Windows needs the same tricks malware
    uses, and a user who has been told the program updated itself has no way
    to tell the two apart. The file goes to the Desktop and the user runs it.
  * It fails silently. No network, GitHub down, rate limited, half a JSON
    document: the program carries on and says nothing. An update check that
    interrupts somebody mid-diagnosis has made the program worse.
"""

import hashlib
import json
import os
import re
import time
import urllib.request

from ps3diag import config

from . import APP_NAME, PROJECT_URL, VERSION

# --- where releases come from -----------------------------------------------

#: Confirmed. Change this and the About screen's link to this program follows,
#: because it is built from the same constant.
REPOSITORY = "setsid/ps3-tools"

#: Built from REPOSITORY, and the only URL this module ever fetches.
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"

# GitHub asks for a User-Agent and answers 403 without one. Naming the program
# and its version means a rate limit or a block can be traced to this program
# rather than to "some Python script".
USER_AGENT = f"{APP_NAME.replace(' ', '')}/{VERSION} (+{PROJECT_URL})"

#: Short on purpose. This runs at launch, and a launch that waits on GitHub is
#: a launch that hangs when the network is up but GitHub is not.
TIMEOUT = 8.0

#: Unauthenticated GitHub allows 60 requests an hour per IP, shared with every
#: other program on the same connection. Once a day is plenty for a program
#: that is released a few times a year.
CACHE_SECONDS = 24 * 60 * 60

#: How long a check that found nothing is believed for. Much shorter than a
#: check that found something, and the difference matters:
#:
#: the cache records the time even when the answer was "no release" or "the
#: fetch failed", so that a machine with no internet does not ask at every
#: launch. But with one TTL for both, a release published an hour after a
#: negative check stayed invisible for the rest of the day, and the only way
#: to see it was Check now in About -- which forces past the cache, finds it,
#: and stores it, after which every later launch shows it. That is exactly the
#: "it only works after I check manually once" report, and it was the cache,
#: not the banner.
#:
#: An hour still keeps a machine with no internet down to a handful of
#: attempts a day, well inside GitHub's 60 an hour.
EMPTY_CACHE_SECONDS = 60 * 60

#: Keys in the shell's settings dict (services.settings). It is persisted by
#: the shell into the file ps3diag.config works out the location of -- beside
#: the exe, falling back to per-user -- and takes arbitrary JSON values. Both
#: keys are new: reusing one of the diagnostic tool's would mean two features
#: fighting over one value.
SETTING_ENABLED = "update_check_enabled"
SETTING_CACHE = "update_check_cache"

#: On by default. The check is one request a day to one host and the program
#: is handed out as a single file with no other way of hearing about a fix,
#: but it is stated on the About screen and can be turned off there.
ENABLED_BY_DEFAULT = True

#: Enough of a release to tell the user about it without another request.
_CACHE_FIELDS = ("tag_name", "name", "body", "html_url", "assets")

_HASH = re.compile(r"\b([0-9a-fA-F]{64})\b")
_SEGMENT = re.compile(r"^\d+$")


# --- versions ---------------------------------------------------------------

def parse_version(text):
    """"v2.1" -> (2, 1). None when it is not a version this can be sure of.

    Deliberately strict. A tag this cannot read confidently -- "2.1-rc1",
    "latest", "" -- returns None and the caller shows nothing, which is the
    safe direction: the cost of not offering an update that exists is one
    quiet day, and the cost of getting the comparison wrong is telling
    everybody to download an older build.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    if not cleaned:
        return None
    parts = cleaned.split(".")
    if not all(_SEGMENT.match(part) for part in parts):
        return None
    return tuple(int(part) for part in parts)


def compare_versions(first, second):
    """-1, 0 or 1. Either side may be a string or an already parsed tuple.

    Segment counts differ in practice -- this program is "0.9" and a fix for
    it would be "0.9.1" -- so the shorter is padded with zeros, which makes
    1.0 and 1.0.0 the same version rather than one newer than the other.
    """
    left = first if isinstance(first, tuple) else parse_version(first)
    right = second if isinstance(second, tuple) else parse_version(second)
    if left is None or right is None:
        raise ValueError("cannot compare an unreadable version")
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    if left < right:
        return -1
    return 1 if left > right else 0


def is_newer(candidate, current=VERSION):
    """True only when candidate is readable, current is readable and candidate
    is the greater of the two. Anything else is False, including equal."""
    try:
        return compare_versions(candidate, current) > 0
    except ValueError:
        return False


# --- what a release turned out to be ----------------------------------------

class Release:
    """One GitHub release, reduced to what the banner needs.

    Holds no network handle and nothing unserialisable, so the raw payload it
    came from can be cached as it stands.
    """

    def __init__(self, payload):
        self.tag = (payload.get("tag_name") or "").strip()
        self.version = parse_version(self.tag)
        self.name = (payload.get("name") or "").strip() or self.tag
        self.notes = (payload.get("body") or "").strip()
        self.page_url = (payload.get("html_url") or "").strip()
        self.asset_name = ""
        self.asset_url = ""
        self.asset_size = 0
        assets = payload.get("assets")
        if isinstance(assets, list):
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                name = (asset.get("name") or "").strip()
                url = (asset.get("browser_download_url") or "").strip()
                # The exe is what a user is being asked to run. A source
                # tarball is not an update to this program in any useful sense.
                if name.lower().endswith(".exe") and url:
                    self.asset_name, self.asset_url = name, url
                    self.asset_size = asset.get("size") or 0
                    break
        self.sha256 = sha256_from_notes(self.notes, self.asset_name)

    @property
    def verifiable(self):
        """Whether the release notes publish a hash for the download."""
        return bool(self.sha256)

    def summary(self):
        """One line for the banner heading."""
        return f"Version {self.tag.lstrip('vV')} is available"


def sha256_from_notes(notes, asset_name=""):
    """The sha256 the release notes publish for the download, or "".

    Returning "" when there are several and none of them is clearly the one
    for this file is the point of the function. A hash picked at random out of
    a document is worse than no hash: it turns "we could not verify this" into
    "this failed verification", and the user has no way to tell which happened.
    """
    if not isinstance(notes, str) or not notes:
        return ""
    found = []
    for line in notes.splitlines():
        for match in _HASH.finditer(line):
            found.append((line, match.group(1).lower()))
    if not found:
        return ""
    if len(found) == 1:
        return found[0][1]
    if asset_name:
        named = [digest for line, digest in found if asset_name.lower()
                 in line.lower()]
        if len(named) == 1:
            return named[0]
    return ""


# --- fetching ---------------------------------------------------------------

def urllib_fetcher(url, timeout=TIMEOUT):
    """The default fetcher. Bytes, or an exception.

    The only place in this program that opens an internet connection. Tests
    never reach it: they pass a fetcher of their own, and the suite is run
    under an audit hook that fails on anything off loopback.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_release(fetcher=None, url=None):
    """The latest release, or None. Never raises.

    None covers every way this goes wrong and they are deliberately not told
    apart: no network, DNS gone, 404 because REPOSITORY is still a guess, 403
    because the address is rate limited, HTML from a captive portal instead of
    JSON, JSON with no tag in it. The caller does the same thing in all of
    them, which is nothing.
    """
    fetcher = urllib_fetcher if fetcher is None else fetcher
    try:
        raw = fetcher(RELEASES_URL if url is None else url, timeout=TIMEOUT)
    except Exception:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    # A rate limited or missing repository answers with a perfectly good JSON
    # document that simply has no release in it.
    if not (payload.get("tag_name") or "").strip():
        return None
    return payload


# --- the check itself --------------------------------------------------------

def enabled(settings):
    value = (settings or {}).get(SETTING_ENABLED, ENABLED_BY_DEFAULT)
    return bool(value) if isinstance(value, bool) else ENABLED_BY_DEFAULT


def set_enabled(settings, value):
    """The shell writes the settings dict out when it closes."""
    settings[SETTING_ENABLED] = bool(value)


def _cache(settings):
    stored = (settings or {}).get(SETTING_CACHE)
    return stored if isinstance(stored, dict) else {}


def _cache_fresh(settings, now):
    stored = _cache(settings)
    if "checked" not in stored:
        return False
    # A remembered release worth telling the user about is believed for a day.
    # Everything else is believed for an hour, because everything else goes
    # stale the moment a release is published and there is no way for this end
    # to be told.
    #
    # A remembered release the user has since installed belongs in the second
    # group. It was in the first, and that is why a 1.2.0 build stayed quiet:
    # the cache from the day it was offered still held 1.2.0, the check read
    # it back at every launch, found it was not newer than the build asking,
    # and said nothing without ever asking GitHub. A release already installed
    # can never produce news, so keeping it for a day only stops the question
    # being asked.
    window = (CACHE_SECONDS if _cache_still_useful(stored)
              else EMPTY_CACHE_SECONDS)
    try:
        checked = float(stored["checked"])
    except (TypeError, ValueError):
        return False
    # A clock that has gone backwards -- a laptop returning from a different
    # timezone, a machine with no RTC -- would otherwise pin the cache as
    # fresh for as long as the clock is wrong.
    return 0 <= (now - checked) < window


def _cache_still_useful(stored):
    """Whether what is remembered could still be news for this build."""
    release = stored.get("release")
    if not isinstance(release, dict):
        return False
    return is_newer(Release(release).tag)


def _store(settings, payload, now):
    trimmed = None
    if isinstance(payload, dict):
        trimmed = {key: payload[key] for key in _CACHE_FIELDS
                   if key in payload}
    settings[SETTING_CACHE] = {"checked": now, "release": trimmed}


def check(settings, fetcher=None, now=None, force=False,
          whatever_the_setting=False):
    """The whole check. Returns a Release worth telling the user about, or
    None.

    None means "say nothing", and it covers a switched-off setting, a cache
    that was checked today, no network, a bad answer, and a release that is
    not newer than this build. Never raises: the caller is the launch path.

    force skips the cache and is what the About screen's Check now button
    uses. That button still respects the setting, because a button that
    quietly does the thing the checkbox turned off is a lie about the
    checkbox.

    whatever_the_setting is the launch path and is the one caller that
    overrides it. Every start of the program asks GitHub whether there is a
    newer release, which is what the program has always been meant to do, and
    it is the only way somebody running an old build ever hears about a fix.
    Nothing about it is louder than the banner: a check that finds nothing,
    or cannot run at all, still says nothing at all.
    """
    if settings is None:
        settings = {}
    if not enabled(settings) and not whatever_the_setting:
        return None
    now = time.time() if now is None else now

    if not force and _cache_fresh(settings, now):
        # Cached, so the banner survives a restart without a second request.
        cached = _cache(settings).get("release")
        payload = cached if isinstance(cached, dict) else None
    else:
        payload = fetch_release(fetcher=fetcher)
        # The time is recorded even when the fetch failed. A machine with no
        # internet would otherwise try again at every launch, and an address
        # that is rate limited would stay rate limited by being asked.
        _store(settings, payload, now)

    if payload is None:
        return None
    release = Release(payload)
    if not is_newer(release.tag):
        return None
    return release


def clear_cache(settings):
    """Forget the last check. Used by the setting being turned back on."""
    if settings is not None:
        settings.pop(SETTING_CACHE, None)


# --- downloading, which is never automatic ----------------------------------

class Download:
    """What became of a download the user asked for.

    verified is three-valued on purpose. True means the file matched a hash
    published in the release notes; False means it did not and the file has
    been deleted; None means the notes published no hash for it, and the user
    is told exactly that rather than being told it is fine.
    """

    def __init__(self, path="", verified=None, detail="", ok=False,
                 digest=""):
        self.path = path
        self.verified = verified
        self.detail = detail
        self.ok = ok
        self.digest = digest

    @property
    def folder(self):
        return os.path.dirname(self.path) if self.path else ""


def download_fetcher(url, timeout=TIMEOUT):
    """The default download fetcher. Separate from urllib_fetcher so a caller
    can inject one and not the other, and so the headers can differ."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def target_folder():
    """The Desktop, or the home folder when there is not one.

    ps3diag.config already works this out for the diagnostic report, and a
    user who has been told "it is on your Desktop" will not look anywhere else.
    """
    return config.desktop_dir()


#: How many names are tried before giving up and letting the write fail.
MAX_NAME_ATTEMPTS = 50


def free_path(folder, name, version=""):
    """A path in folder that nothing is using yet.

    Every release ships the same file name. Saving over the last one is how
    this failed on a real Desktop: the previous copy was the program doing the
    downloading, Windows had it locked, and the write came back as "write
    protected" with no way for the user to act on it.

    Nothing is ever overwritten now. The version goes in the name, which also
    makes it obvious which of two files on a Desktop is the new one, and a
    number is added after that if something of that name is somehow there too.
    """
    stem, extension = os.path.splitext(name)
    version = (version or "").strip().lstrip("vV")
    if version and version not in stem:
        stem = f"{stem}-{version}"
    candidate = os.path.join(folder, stem + extension)
    if not os.path.exists(candidate):
        return candidate
    for index in range(2, MAX_NAME_ATTEMPTS + 2):
        candidate = os.path.join(folder, f"{stem} ({index}){extension}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(folder, stem + extension)


def download(release, fetcher=None, folder=None, timeout=TIMEOUT):
    """Fetch the release's exe to the Desktop and check it against the notes.

    Only ever called because somebody pressed a button. Nothing here replaces
    the running program, and nothing here runs what it downloaded.
    """
    if release is None or not release.asset_url:
        return Download(detail="This release has no download attached to it. "
                               "Open the release page and fetch it by hand.")
    fetcher = download_fetcher if fetcher is None else fetcher
    folder = target_folder() if folder is None else folder
    try:
        data = fetcher(release.asset_url, timeout=timeout)
    except Exception:
        return Download(detail="The download did not finish. Check the "
                               "connection and try again, or fetch it from "
                               "the release page in your browser.")
    if not isinstance(data, (bytes, bytearray)):
        return Download(detail="The download did not arrive as a file.")
    data = bytes(data)
    digest = digest_bytes(data)

    if release.sha256 and digest != release.sha256:
        # Not written to disk at all. A file that failed its hash is either a
        # broken download or somebody else's file, and neither of them should
        # be sitting on a Desktop next to the real one.
        return Download(
            verified=False, digest=digest,
            detail="The downloaded file does not match the checksum published "
                   "with the release, so it has not been kept. Do not run any "
                   "copy of it. Try again, and if it fails a second time say "
                   "so before running anything.")

    try:
        os.makedirs(folder, exist_ok=True)
        path = free_path(folder,
                         os.path.basename(release.asset_name)
                         or "ps3-tools-update.exe",
                         release.tag)
        with open(path, "wb") as handle:
            handle.write(data)
    except OSError:
        return Download(detail="The file could not be saved to your Desktop. "
                               "It may be full or write protected.")
    name = os.path.basename(path)

    if release.sha256:
        return Download(
            path=path, verified=True, ok=True, digest=digest,
            detail=f"Saved to your Desktop as {name}, and it matches the "
                   f"checksum published with the release. Close this program "
                   f"and run the new file.")
    # Said plainly. Claiming a file is verified when nothing was checked is
    # the one outcome here that could get somebody hurt.
    return Download(
        path=path, verified=None, ok=True, digest=digest,
        detail=f"Saved to your Desktop as {name}. The release notes did not "
               f"publish a checksum, so this file has not been verified. "
               f"Its sha256 is {digest}.")
