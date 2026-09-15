"""Game updates: what Sony has published for the games on this console.

Pure logic. No Qt, no threads, nothing that happens on import.

This is the only part of the program that downloads a file from somebody else
and then writes it to a user's console, so the rules it works under are written
down here rather than left to be inferred.

**The sha1 check is the whole reason this is acceptable.** Sony's manifest is
fetched over HTTPS and publishes a sha1 for every package. The package itself
comes from a CDN over plain HTTP, because that is what the manifest's own URLs
say. A file fetched that way has been through an unknown number of hands, and
the only thing standing between "the update Sony published" and "whatever
answered that request" is comparing the sha1 of what arrived against the one
the manifest gave. A mismatch stops the flow dead: nothing is uploaded, nothing
is installed, and the downloaded file is deleted. It is not a warning, it is
not something a user can click past, and it must never be turned into one.

What that sha1 covers is not the whole file. Every PS3 package ends with a
32-byte footer holding its own sha1, and Sony's published value is the digest
of everything before it. Hashing the whole file matches nothing, on any title;
see PKG_FOOTER_BYTES and verify_download.

Every network call goes through an injected seam. The default is urllib, but no
test ever reaches it: the suite injects, so nothing in it can leave the machine
even by mistake. There is a live console on this LAN and
tests/check-no-network.py fails the build if a packet does.

Nothing is cached and there is no table of title IDs or names in this program.
The console reports the ID, and the manifest answers with the name, the
versions and the hashes. A local copy of any of that could only ever be a
staler answer to a question that has a live one.

An empty response from the manifest host is the normal answer for a game that
never had a title update. It is not an error and must not be shown as one.
"""

import datetime
import hashlib
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import threading
import xml.etree.ElementTree as ElementTree
from concurrent import futures
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ps3diag import collectors, config, isoid, parsers, regioncodes
from ps3diag.parsers import human_size

# Optional, and treated exactly as the diagnostic's own collectors treat it: a
# build without it reads every title ID out of a name, which is all the fast
# path ever needs. Only the opt-in "look inside the images" pass wants it.
try:
    from ps3diag import isoreader
except ImportError:                                         # pragma: no cover
    isoreader = None

from . import detect, titles
from .consoleactions import ActionFailed, PACKAGE_NAME, PACKAGES_PATH
from .update import USER_AGENT, compare_versions

# --- where updates come from ------------------------------------------------

#: The only host this module asks about updates. One host, one URL shape, built
#: from one constant, and nothing here ever fetches a URL a server handed it as
#: part of *this* request: the manifest's package URLs are fetched later, by
#: download_package, and are checked against PACKAGE_HOSTS first.
MANIFEST_HOST = "a0.ww.np.dl.playstation.net"

MANIFEST_URL = "https://{host}/tpl/np/{title_id}/{title_id}-ver.xml"

#: A PS3 title ID. The ID comes off a directory listing on the user's console,
#: so it is checked into this shape before it is ever put in a URL.
TITLE_ID = re.compile(r"^[A-Z]{4}\d{5}$")

#: The package URL in the manifest is the one piece of this that is chosen by
#: somebody else, so the host it names is checked before it is fetched. A
#: manifest that pointed somewhere else would be a manifest worth refusing.
PACKAGE_HOSTS = (".playstation.net", ".playstation.com", ".sony.net",
                 ".sonyentertainmentnetwork.com")

#: Short enough that a game with no update does not stall the whole scan.
MANIFEST_TIMEOUT = 15.0

#: Long, because this is a several hundred megabyte file over somebody's home
#: broadband and the read loop refreshes it on every block anyway.
DOWNLOAD_TIMEOUT = 60.0

#: Big enough that a 400 MB package is not a hundred thousand callbacks, small
#: enough that a progress bar still moves.
BLOCK = 262144

#: Where the console installs packages from, and the folder the update is
#: uploaded into. Comes from consoleactions so there is one spelling of it.
PACKAGES_DIR = PACKAGES_PATH

#: Left free on the console after the update has landed. A hard drive filled to
#: the last byte is how a PS3 starts failing at things that have nothing to do
#: with this program.
SPACE_MARGIN = 256 * 1024 * 1024

TLS_MESSAGE = (
    "The connection to {host} could not be confirmed as Sony's.\n\n"
    "Nothing has been downloaded. This program checks that the update list "
    "really came from Sony before it trusts anything in it, because the "
    "checksum it uses to verify the downloaded file comes from that same "
    "list. It will not skip that check.\n\n"
    "The console can still fetch the update itself in the usual way. Start "
    "the game with the console online and let it download. That is slower "
    "than this tool and it works.")

#: Said when the bundled root is not the certificate this program expects.
CERT_WRONG_MESSAGE = (
    "The certificate shipped with this program is not the one it expects, so "
    "it has not tried to reach Sony.\n\n"
    "Nothing has been downloaded. If you built this yourself, the file in "
    "certs is not the right root. If you did not, do not use this copy.")

#: Said when the pinned root is not bundled at all, which is a fault in the
#: build rather than anything about the user's machine or network.
CERT_MISSING_MESSAGE = (
    "This copy of the program is missing the certificate it needs to confirm "
    "it is talking to Sony, so it has not tried.\n\n"
    "That is a fault in this build. Nothing is wrong with your computer or "
    "your connection, and nothing has been downloaded. The console can still "
    "fetch updates itself in the usual way.")


# --- failures ---------------------------------------------------------------

class UpdateError(Exception):
    """A stop with an explanation already fit to show a user."""


class ManifestUnreadable(UpdateError):
    """The manifest host answered with something that is not a manifest."""


class TitleNotListed(UpdateError):
    """Sony has no entry for that title ID at all.

    A 404 from the manifest host. It comes up with expansion discs, with
    homebrew, and with anything else that was never a title Sony published.
    The answer arrived and it was complete, so this is a different thing from
    the check having failed.
    """


class CertificateMissing(UpdateError):
    """The pinned root is not in this build. A packaging fault, said as one."""


class TlsNotTrusted(UpdateError):
    """The certificate did not verify. Deliberately its own exception.

    It is the one failure here with a cause the user can do something about,
    and it must never be handled by the same branch as "the network is down",
    because the advice is completely different.
    """


class DownloadFailed(UpdateError):
    """The package did not arrive, or did not arrive whole."""


class VerificationFailed(UpdateError):
    """What arrived is not what Sony published. The flow stops here."""


class NotEnoughSpace(UpdateError):
    """Refused before anything was downloaded."""


class UploadFailed(UpdateError):
    """The console stopped answering partway through the copy."""


# --- what the manifest said -------------------------------------------------

@dataclass
class Package:
    """One published version of one game's title update."""

    version: str = ""
    size: int = 0
    sha1sum: str = ""
    url: str = ""
    system_version: str = None

    @property
    def usable(self):
        """Whether this entry has the three things the flow cannot do without.

        A package with no sha1 is not a package this program will install. It
        could be downloaded and it could be uploaded, and there would be no way
        on earth to tell afterwards whether it was the right file.
        """
        return bool(self.url) and self.size > 0 and len(self.sha1sum) == 40


@dataclass
class Manifest:
    """Everything the manifest host said about one title."""

    title_id: str = ""
    name: str = ""
    packages: list = field(default_factory=list)
    #: True when the host answered, correctly, with nothing at all.
    empty: bool = False

    @property
    def latest(self):
        """The newest usable package, or None.

        Newest by version number rather than by document order: the file is
        published in ascending order in every example seen, but "in every
        example seen" is not a thing to install somebody's game update on.
        """
        best = None
        for package in self.packages:
            if not package.usable:
                continue
            if best is None or _newer(package.version, best.version):
                best = package
        return best


def _newer(candidate, current):
    try:
        return compare_versions(candidate, current) > 0
    except ValueError:
        # An unreadable version loses to a readable one and ties with another
        # unreadable one. Guessing here would offer somebody an older build.
        return False


def manifest_url(title_id):
    """The one URL this module fetches for a title.

    Raises ValueError for anything that is not a title ID. The ID arrives from
    a directory listing on the user's console, and a folder called
    "../../whatever" would otherwise become part of a URL.
    """
    cleaned = (title_id or "").strip().upper()
    if not TITLE_ID.match(cleaned):
        raise ValueError(f"not a title ID: {title_id!r}")
    return MANIFEST_URL.format(host=MANIFEST_HOST, title_id=cleaned)


# --- fetching ---------------------------------------------------------------

#: Sony's own private CA, shipped beside the program. See certs/README.md.
CERT_NAME = "scei-dnas-root-05.pem"
CERT_FOLDER = "certs"

#: sha256 of the certificate itself (the DER body), not of the PEM file. A PEM
#: re-wrapped or re-exported is the same certificate and passes; a different
#: certificate does not, whatever it is called. Pinning the file's bytes would
#: fail on a harmless reformat and pass on nothing useful.
#:
#: CN=SCEI DNAS Root 05, O=Sony Computer Entertainment Inc., C=JP
#: self-signed, valid 2004-07-12 to 2037-12-06.
CERT_SHA256 = ("51d5a7833f67f1d1c6212f75997e4b83"
               "dada0f7910ba4b5a3fa4e9b7b9bc827c")


def certificate_fingerprint(path):
    """sha256 of the DER body of a PEM certificate, lowercase hex."""
    with open(path, "r", encoding="ascii", errors="replace") as handle:
        pem = handle.read()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def certificate_is_expected(path):
    """Whether the bundled root is the one this program was built to trust."""
    try:
        return certificate_fingerprint(path) == CERT_SHA256
    except Exception:                                       # noqa: BLE001
        return False


def certificate_path():
    """Where the pinned root lives, bundled or in a checkout. "" if absent."""
    for base in (config.bundle_dir(), config.app_dir()):
        candidate = os.path.join(base, CERT_FOLDER, CERT_NAME)
        if os.path.isfile(candidate):
            return candidate
    return ""


def ssl_context():
    """A context that trusts Sony's console root and nothing else.

    a0.ww.np.dl.playstation.net presents a certificate issued by
    "CN=SCEI DNAS Root 05, O=Sony Computer Entertainment Inc." -- Sony's own
    private CA for console traffic. It is in no public trust store and never
    will be: the endpoint is meant for PlayStation hardware, which ships that
    root built in. Windows, Python and the OS trust stores all refuse it, on
    every machine. It is not interception and it is not anybody's antivirus.

    So the root is pinned rather than looked up. This is stricter than the
    public CA path, not weaker: exactly one issuer is accepted, so an actual
    interception still fails, and the sha1 that the package download is checked
    against keeps a chain of trust worth having.

    Hostname checking stays on -- the leaf is *.ww.np.dl.playstation.net, which
    matches -- so a certificate from that root for some other host is refused
    too.

    Raises CertificateMissing when the root is not bundled. There is
    deliberately no fallback to the system store or to an unverified context:
    falling back would defeat the only reason any of this is checked.
    """
    path = certificate_path()
    if not path:
        raise CertificateMissing(CERT_MISSING_MESSAGE)
    # Checked every time rather than trusted because it is in the right place.
    # The whole value of pinning is that exactly one issuer is accepted, and a
    # substituted PEM would quietly hand that back.
    if not certificate_is_expected(path):
        raise CertificateMissing(CERT_WRONG_MESSAGE)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # PROTOCOL_TLS_CLIENT does not turn these on the way create_default_context
    # does, and getting it wrong here disables verification entirely rather
    # than loudly. Set explicitly, and asserted in the tests.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=path)
    # SCEI DNAS Root 05 was issued in 2004 and is signed with SHA-1. OpenSSL 3
    # refuses SHA-1 in a certificate signature at its default security level,
    # whatever root is pinned, so without this the connection fails with
    # "CA signature digest algorithm too weak". Sony cannot reissue it: PS3s
    # carry that root in firmware.
    #
    # This looks like switching security off to make an error go away, so,
    # precisely: the trust decision is unchanged and is stricter than the
    # public CA path. Exactly one root is accepted, hostname checking is on,
    # and the root's own fingerprint was verified above. All SECLEVEL=0 permits
    # is the 2004 digest algorithm that one root happens to be signed with. A
    # certificate from any other issuer is still refused, which the tests
    # assert because it is the part anybody would reasonably doubt.
    #
    # Only ever on this context. The GitHub update check and everything else
    # use the default, untouched. SECLEVEL=1 does not work here -- it still
    # rejects the digest -- and layering it onto create_default_context leaves
    # other policy in place besides.
    context.set_ciphers("ALL:@SECLEVEL=0")
    return context


def urllib_fetcher(url, timeout=MANIFEST_TIMEOUT):
    """The default manifest fetcher. Bytes, or an exception.

    Tests never reach this: they pass a fetcher of their own, and the suite
    runs under an audit hook that fails on anything off loopback.

    The certificate failure is converted here rather than at the call site so
    that there is exactly one place in the program that knows what an SSL
    verification error looks like. There is no branch anywhere in this module
    that builds an unverified context: see TLS_MESSAGE.
    """
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Connection": "close",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl_context()) as response:
            return response.read()
    except CertificateMissing:
        raise
    except urllib.error.URLError as exc:
        if _is_certificate_failure(exc):
            raise TlsNotTrusted(
                TLS_MESSAGE.format(host=MANIFEST_HOST)) from exc
        raise
    except ssl.SSLError as exc:
        if _is_certificate_failure(exc):
            raise TlsNotTrusted(
                TLS_MESSAGE.format(host=MANIFEST_HOST)) from exc
        raise


def urllib_stream(url, timeout=DOWNLOAD_TIMEOUT):
    """The default package stream. A file-like object with .read(n).

    Separate from urllib_fetcher because a several hundred megabyte package
    read into memory in one go is how this program runs a machine out of it.
    Injected the same way, and reached by no test.
    """
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    })
    return urllib.request.urlopen(request, timeout=timeout,
                                  context=ssl_context())


def _is_certificate_failure(exc):
    """True for the one failure that has its own advice.

    urllib buries the SSL error inside a URLError, and some versions nest it
    twice, so the reason chain is walked rather than the top exception typed.
    """
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        if isinstance(exc, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in \
                str(getattr(exc, "reason", "") or exc):
            return True
        text = str(getattr(exc, "reason", "") or "")
        if "CERTIFICATE_VERIFY_FAILED" in text or \
                "certificate verify failed" in text.lower():
            return True
        exc = getattr(exc, "reason", None)
        if isinstance(exc, str):
            return False
        seen += 1
    return False


def fetch_manifest(title_id, fetcher=None, timeout=MANIFEST_TIMEOUT):
    """What Sony publishes for one title.

    An empty body is a real and common answer: it means no title update was
    ever released for that game. It comes back as an empty Manifest with
    .empty set, and is not an error anywhere in this module.

    Four outcomes, kept apart because the sentence a user should read is
    different for each:

    * a manifest, parsed and returned
    * an empty body, which is how Sony says a game never had a title update
    * a 404, which is how Sony says it has no such title at all, and which
      raises TitleNotListed
    * anything else, which means the check did not complete, and which raises
      ManifestUnreadable or TlsNotTrusted
    """
    url = manifest_url(title_id)
    fetcher = urllib_fetcher if fetcher is None else fetcher
    try:
        raw = fetcher(url, timeout=timeout)
    except TlsNotTrusted:
        raise
    except Exception as exc:                                # noqa: BLE001
        # Checked here as well as inside urllib_fetcher, because the fetcher is
        # a seam: an injected one raises whatever it raises, and the one
        # failure with advice of its own must not be lost just because it came
        # up through a different door.
        if _is_certificate_failure(exc):
            raise TlsNotTrusted(
                TLS_MESSAGE.format(host=MANIFEST_HOST)) from exc
        if getattr(exc, "code", None) == 404:
            raise TitleNotListed(
                f"Sony's update list has no entry for {title_id}.") from exc
        raise ManifestUnreadable(
            f"Sony's update list for {title_id} could not be fetched. Check "
            f"this computer is connected to the internet, then try again. "
            f"({exc.__class__.__name__})") from exc
    return parse_manifest(raw, title_id)


def parse_manifest(raw, title_id=""):
    """The XML Sony answers with, or an empty Manifest, or ManifestUnreadable.

    The three outcomes are deliberately distinct. Empty means the game never
    had an update. Unreadable means the answer was not a manifest at all, which
    is a different thing and gets a different sentence.
    """
    title_id = (title_id or "").strip().upper()
    if raw is None:
        raise ManifestUnreadable(
            f"Sony's update list for {title_id} could not be read.")
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not raw.strip():
        # The documented way of saying "no title update was ever released".
        return Manifest(title_id=title_id, empty=True)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ManifestUnreadable(
            f"Sony's update list for {title_id} was not in a format this "
            f"program understands, so no update can be offered for this game. "
            f"This usually means something on this network answered "
            f"instead of Sony. ({exc})") from exc

    if root.tag != "titlepatch":
        # A captive portal's sign-in page is perfectly good XML and would
        # otherwise parse to nothing and be reported as "no update was ever
        # released", which is a lie told confidently. Sony's document has one
        # root element and this is it.
        raise ManifestUnreadable(
            f"Something other than Sony answered when this program asked for "
            f"{title_id}'s update list, so no update can be offered for this "
            f"game. If this computer is on a network that asks you to sign in "
            f"through your browser, sign in and try again.")

    manifest = Manifest(
        title_id=title_id or (root.get("titleid") or "").upper())
    for element in root.iter("package"):
        package = Package(
            version=(element.get("version") or "").strip(),
            size=_whole_number(element.get("size")),
            sha1sum=_sha1_text(element.get("sha1sum")),
            url=(element.get("url") or "").strip(),
            system_version=((element.get("ps3_system_ver") or "").strip()
                            or None),
        )
        manifest.packages.append(package)
    # The game's name is inside the package's copy of PARAM.SFO. Taking the
    # last one means the name shown is the one that ships with the newest
    # update, which is the name the console itself shows.
    for element in root.iter("TITLE"):
        text = (element.text or "").strip()
        if text:
            manifest.name = text
    if not manifest.packages and not manifest.name:
        # A well formed XML document with nothing in it is the same answer as
        # an empty body, and a user is owed the same sentence for both.
        manifest.empty = True
    return manifest


def _whole_number(text):
    try:
        return max(0, int(str(text).strip()))
    except (TypeError, ValueError):
        return 0


def _sha1_text(text):
    cleaned = (text or "").strip().lower()
    return cleaned if re.fullmatch(r"[0-9a-f]{40}", cleaned) else ""


# --- what is on the console -------------------------------------------------

#: Title IDs that belong to homebrew rather than to a game Sony published.
#:
#: Homebrew borrows the shape of a real title ID, so the folder it installs
#: into looks exactly like a game's. Asking Sony for a title update for
#: somebody's file manager gets nothing back, and a row offering one would be
#: actively wrong rather than merely useless.
#:
#: Named IDs only, one entry per thing that can be named. There is a temptation
#: to write a rule instead -- BLES806xx and BLJS900xx are both said to be
#: homebrew ranges -- and it is the wrong trade: a rule that is nearly right
#: hides somebody's real game with no way for them to find out why, and a
#: homebrew row that slips through costs one useless line on a screen. Add IDs
#: here as they are confirmed on real consoles; do not widen this into a match.
HOMEBREW_TITLES = {
    # The file manager nearly every CFW console has. mmCM and IRISMAN are built
    # from it and install into the same folder under the same ID.
    "BLES80608": "multiMAN",
}

#: The devices looked at when the caller does not say. The diagnostic's own
#: default, for the same reason: dev_hdd0 is on every console and everything
#: else has to be discovered.
INVENTORY_DEVICES = ("dev_hdd0",)

#: The folders walked for games. The diagnostic's own list, less PKG: what
#: sits in /dev_hdd0/PKG is an installer waiting to be run -- very often the
#: title update itself, named after the game it patches -- and counting it as a
#: game on the console would put a row on this screen for something that is not
#: installed and offer to fetch what is already sitting there.
INVENTORY_FOLDERS = tuple(name for name in collectors.GAME_FOLDERS
                          if name != "PKG")

#: File endings the image reader can open. Anything else with no title ID in
#: its name is left alone rather than guessed at.
ISO_SUFFIXES = getattr(isoreader, "ISO_SUFFIXES", (".iso",))

#: Platforms with a title update manifest on Sony's host. A PS2 or PSP image
#: sitting in one of these folders has no answer there, and asking is a request
#: spent to be told so.
UPDATABLE_PLATFORMS = ("PS3 disc", "PS3 digital")


@dataclass
class InstalledTitle:
    """One game on the console, before Sony was asked about it.

    It may be an installed title update under /dev_hdd0/game, an extracted
    folder game, or a disc image. Which of those it is decides nothing except
    the sentence the user reads.
    """

    title_id: str = ""
    path: str = ""
    #: APP_VER from PARAM.SFO, or None when it could not be read.
    version: str = None
    #: Why, when version is None. Always a sentence.
    detail: str = ""
    #: The name ps3tools.detect knew, when it knew one. Usually None: the
    #: manifest is where names come from.
    known_name: str = None
    #: True when the game is on the console with no title update installed at
    #: all. Deliberately not the same thing as version being None, which means
    #: an update is installed and its version could not be read. Two different
    #: answers to two different questions, and collapsing them would tell
    #: somebody their game is fine when it has never been patched.
    no_update_installed: bool = False
    #: Where it was found: "installed", "folder game" or "disc image". Only
    #: used to explain a row, never to decide anything.
    source: str = "installed"
    #: True when the title ID had to be read out of the image itself rather
    #: than off its name. The slow path, and the user paid for it.
    from_image: bool = False


def scan_console(lister, param_sfo_reader=None, devices=None,
                 image_identifier=None, on_stage=None):
    """Every game on the console, with the title update installed on it.

    Returns (titles, notes, unidentified). The last is the disc images whose
    names carry no title ID, which the caller may hand to image_identifier on a
    second pass; see there for why that is not done by default.

    Three sources, unioned and deduplicated by title ID, because a console
    holds its games in three shapes and only one of them was being looked at:

      * ``/dev_hdd0/game`` -- installed title updates. ps3tools.detect's walk,
        which is the Call of Duty patcher's eyes and reports only the two
        titles it can fix, plus the rest of the listing, because this tool has
        no list of games at all and has to offer every one of them.
      * the folder games and disc images in the diagnostic's own game folders.
        A title found there and *not* under /dev_hdd0/game has no title update
        installed at all, and that is the single most actionable row this
        screen can show: it is the game most likely to need one.

    A title in both places is one row, and the installed side wins, because it
    is the one that knows which version is on the console.

    Never raises. A console that stops answering leaves what was already found.
    """
    def stage(text):
        """Say what is happening. This runs for minutes on a full console."""
        if on_stage:
            on_stage(text)

    if param_sfo_reader is None:
        param_sfo_reader = getattr(lister, "download_bytes", None)

    found = {}
    notes = []
    homebrew = {}

    stage("Reading the games installed on the console")
    report = detect.find_installations(lister, param_sfo_reader)
    notes.extend(report.notes)
    total = len(report.installations)
    for index, installation in enumerate(report.installations, start=1):
        stage(f"Reading the version of {installation.title_id or 'a game'} "
              f"({index} of {total})")
        if not installation.title_id:
            continue
        if installation.title_id in HOMEBREW_TITLES:
            homebrew[installation.title_id] = HOMEBREW_TITLES[
                installation.title_id]
            continue
        version, detail = installation.tu_version, installation.tu_detail or ""
        if version is None:
            # detect stops looking once it has decided there is no USRDIR,
            # because for the patcher a title with no title update is the end
            # of the question. For this tool it is the start of one: the game
            # is installed, PARAM.SFO is still there, and the version in it is
            # exactly what has to be compared with Sony's.
            version, fallback = read_installed_version(param_sfo_reader,
                                                       installation.title_id)
            if version is None:
                detail = detail or fallback
            else:
                detail = ""
        found[installation.title_id] = InstalledTitle(
            title_id=installation.title_id,
            path=(installation.path
                  or f"{detect.GAME_ROOT}/{installation.title_id}"),
            version=version,
            detail=detail,
            known_name=installation.name,
        )

    stage("Looking at what else is installed")
    try:
        for title_id in _title_folders(lister, notes):
            if title_id in found:
                continue
            if title_id in HOMEBREW_TITLES:
                homebrew[title_id] = HOMEBREW_TITLES[title_id]
                continue
            path = f"{detect.GAME_ROOT}/{title_id}"
            version, detail = read_installed_version(param_sfo_reader,
                                                     title_id)
            found[title_id] = InstalledTitle(title_id=title_id, path=path,
                                             version=version, detail=detail)
    except Exception as exc:                                # noqa: BLE001
        notes.append(f"The console stopped answering while its installed "
                     f"games were being listed, so this list may be "
                     f"incomplete. Check it is still switched on, then try "
                     f"again. ({exc.__class__.__name__})")

    stage("Looking through the game folders on every device")
    entries = inventory_entries(lister, devices)
    unidentified = []
    for entry in entries:
        # Off the name, which costs nothing beyond the listing that has already
        # happened. BLES01717-[Call of Duty Black Ops II], Some Game
        # [BLES01234].iso and SLUS-20946 are all the same question, and
        # regioncodes answers all three; a second opinion on it here is how two
        # parts of one program start disagreeing about what is installed.
        title_id = regioncodes.find_title_id(entry.get("name") or "")
        if title_id is None:
            if _is_image(entry):
                unidentified.append(entry)
            continue
        _add_inventory_title(found, homebrew, title_id, entry)

    if image_identifier is not None and unidentified:
        opened = len(unidentified)
        added, elsewhere, unreadable = [], [], []
        seen_names = set()
        for row in _identify_images(image_identifier, unidentified, notes):
            title_id = row.get("title_id")
            name = row.get("name") or ""
            if row.get("opened"):
                seen_names.add(name)
            else:
                continue
            if not title_id:
                unreadable.append(name)
                continue
            outcome = _add_inventory_title(found, homebrew, title_id, row,
                                           from_image=True)
            if outcome == "added":
                added.append(name)
            else:
                elsewhere.append((name, title_id, outcome))
        notes.append(_image_pass_note(opened, added, elsewhere, unreadable))
        # Only images that were actually read leave the unidentified list. A
        # worker that died, a budget that ran out and an image that could not
        # be read all leave it exactly where it was: nothing was learned, so
        # nothing may be concluded, and the next scan tries again.
        unidentified = [entry for entry in unidentified
                        if entry.get("name") not in seen_names]
    elif unidentified:
        notes.append(_unidentified_note(unidentified))

    for name in sorted(set(homebrew.values())):
        # One sentence each, which keeps it singular. There is normally
        # exactly one of these and there has never been a console with many.
        notes.append(
            f"{name} was found on this console. It is homebrew rather than a "
            f"game Sony published, so it is not in the list above: there are "
            f"no title updates for it to be missing.")

    return [found[key] for key in sorted(found)], notes, unidentified


def _add_inventory_title(found, homebrew, title_id, entry, from_image=False):
    """Record one inventory entry as a game with no title update installed.

    A title already under /dev_hdd0/game wins and this does nothing: that
    entry knows which version is installed and this one only knows the game
    exists. This is what stops a game that is both an image and an installed
    update appearing twice.

    Returns what happened, in one word, because the slow image pass has to be
    able to say why it opened thirteen images and added one. Silently dropping
    the other twelve is correct and looks like a failure.
    """
    title_id = title_id.upper()
    if title_id in found:
        return "already"
    if title_id in HOMEBREW_TITLES:
        homebrew[title_id] = HOMEBREW_TITLES[title_id]
        return "homebrew"
    if regioncodes.describe_title_id(title_id)["platform"] \
            not in UPDATABLE_PLATFORMS:
        return "other platform"
    found[title_id] = InstalledTitle(
        title_id=title_id,
        path=_entry_path(entry),
        version=None,
        detail="",
        no_update_installed=True,
        source="disc image" if _is_image(entry) else "folder game",
        from_image=from_image,
    )
    return "added"


def inventory_entries(lister, devices=None, folders=None):
    """Every entry in the console's game folders, from the listings alone.

    The folders and the per-folder cap are the diagnostic's games collector's,
    imported rather than restated: which folders count as game folders on a
    PS3 is its decision and there is no value in a second opinion on it. See
    INVENTORY_FOLDERS for the one folder of its eight that is left out here.

    A folder that is not there raises and is passed over without a word. On a
    normal console most of these eight do not exist, and a note for each one
    would bury the answer under seven lines saying nothing happened. A console
    that has genuinely stopped answering is reported by the /dev_hdd0/game walk
    in scan_console, which runs first and does say so.
    """
    out = []
    for device in devices or INVENTORY_DEVICES:
        device = str(device).strip("/")
        if not device:
            continue
        for folder in (folders or INVENTORY_FOLDERS):
            path = f"/{device}/{folder}/"
            try:
                listing = lister.list_dir(path)
            except Exception:                               # noqa: BLE001
                continue
            try:
                entries, _unparsed = parsers.parse_ftp_list(listing)
            except Exception:                               # noqa: BLE001
                continue
            for item in entries[:collectors.MAX_GAME_ENTRIES]:
                if item.get("name") in (".", ".."):
                    continue
                row = dict(item)
                row["device"] = device
                row["folder"] = folder
                out.append(row)
    return out


def _entry_path(entry):
    device = (entry.get("device") or "").strip("/")
    folder = (entry.get("folder") or "").strip("/")
    name = entry.get("name") or ""
    return "/" + "/".join(part for part in (device, folder, name) if part)


def _is_image(entry):
    """Whether this entry is a disc image, and so something that can be opened.

    A folder game whose name carries no title ID is left alone: there is no
    image to open, and guessing from the folder's contents is a different
    feature.
    """
    if entry.get("kind") not in (None, "file"):
        return False
    return (entry.get("name") or "").lower().endswith(tuple(ISO_SUFFIXES))


def _identify_images(image_identifier, entries, notes):
    """Run the caller's image reader, and survive it failing.

    Failure here costs the identification and nothing else. Everything found by
    name is already in hand by the time this runs, and a console that goes away
    part way through an opt-in slow pass must not take the fast answer with it.
    """
    try:
        return list(image_identifier(entries) or [])
    except Exception as exc:                                # noqa: BLE001
        notes.append(f"The disc images could not be read to find out which "
                     f"games they are ({exc.__class__.__name__}). The rest of "
                     f"this list is unaffected.")
        return []


#: Grouped platform wording for the image-pass summary. regioncodes says "PS2
#: or PSOne" because a four-letter ID cannot tell them apart, and the sentence
#: reads better than the raw value.
PLATFORM_WORDS = {
    "PS2 or PSOne": "PS2 or PSOne games",
    "PSP": "PSP games",
}


def _image_pass_note(opened, added, elsewhere, unreadable):
    """What the slow pass did, including what it deliberately left out.

    Thirteen images opened and one game added is the normal result on a
    console with a shelf of PS2 games, and without this it reads as the pass
    having barely worked. Sony publishes title updates for PS3 titles and
    nothing else, so a PS2 image having no update is the answer, not a gap.
    """
    parts = [f"Opened {opened} disc image{'' if opened == 1 else 's'} "
             f"and read the game ID out of "
             f"{'it' if opened == 1 else 'each one'}."]
    if added:
        parts.append(f"{len(added)} added to the list: "
                     f"{_names_sentence(added)}.")
    groups = {}
    for name, title_id, outcome in elsewhere:
        if outcome == "other platform":
            platform = regioncodes.describe_title_id(title_id)["platform"]
            key = PLATFORM_WORDS.get(platform, f"{platform} games")
        elif outcome == "homebrew":
            key = "homebrew"
        else:
            key = "already in the list above"
        groups.setdefault(key, []).append(name)
    for key in sorted(groups):
        names = groups[key]
        if key == "already in the list above":
            parts.append(f"{len(names)} {key}: {_names_sentence(names)}.")
        elif key == "homebrew":
            parts.append(f"{len(names)} homebrew rather than a game Sony "
                         f"published, so there are no title updates for "
                         f"{'it' if len(names) == 1 else 'them'}: "
                         f"{_names_sentence(names)}.")
        else:
            parts.append(f"{len(names)} {key}, which Sony publishes no PS3 "
                         f"title updates for: {_names_sentence(names)}.")
    if unreadable:
        parts.append(f"{len(unreadable)} could not be identified even after "
                     f"being opened: {_names_sentence(unreadable)}.")
    if not added and not groups and not unreadable:
        parts.append("Nothing could be read out of them.")
    return " ".join(parts)


def _unidentified_note(entries):
    """What is said when images were left unopened.

    Names them and stops. What it would cost to open them, and why anybody
    would want to, belongs next to the button that does it -- this used to say
    both, in a paragraph sitting directly above one that said the same thing.
    """
    names = [entry.get("name") or "" for entry in entries]
    count = len(entries)
    return (f"{count} disc image{'' if count == 1 else 's'} here "
            f"{'does' if count == 1 else 'do'} not have the game's ID in the "
            f"file name, so there is no way to tell which "
            f"game{'' if count == 1 else 's'} "
            f"{'it is' if count == 1 else 'they are'} without opening "
            f"{'it' if count == 1 else 'them'}: {_names_sentence(names)}.")


def _names_sentence(names):
    """'a', 'a and b', 'a, b and c'. Used in sentences shown to a user."""
    names = [str(name) for name in names if name]
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


#: How many console connections the image pass uses at once.
#:
#: Deliberately not higher. webMAN's FTP server has dropped connections here
#: before when several data connections were opened in quick succession: the
#: save data survey lost its listing as the sixth in a row. Each worker holds
#: one connection and works through its own share, so this is the number of
#: connections open at a time rather than the number of images in flight.
IMAGE_WORKERS = 4


def parallel_image_identifier(open_lister, workers=IMAGE_WORKERS, budget=None,
                              on_progress=None):
    """Read the title ID out of each unnamed image, several at a time.

    One image at a time was minutes of waiting on a console with a shelf of
    games, because every image costs a connection, a seek and a handful of
    reads and none of that overlapped. The entries are split into `workers`
    shares and each share is read down one connection of its own.

    Never raises: a share that fails gives up its images and the rest stand.
    """
    def identify(entries):
        entries = list(entries or [])
        if not entries:
            return []
        shares = [entries[index::workers] for index in range(workers)]
        shares = [share for share in shares if share]
        done = [0]
        lock = threading.Lock()

        def report(name):
            with lock:
                done[0] += 1
                if on_progress:
                    on_progress(done[0], len(entries), name)

        def run(share):
            try:
                with open_lister() as lister:
                    reader = isoreader.reader_for(lister)
                    try:
                        payload = isoreader.identify_isos(
                            share, reader,
                            total_budget=(budget if budget is not None
                                          else isoreader.DEFAULT_TOTAL_BUDGET),
                            on_progress=lambda _done, _total, name:
                                report(name))
                    finally:
                        reader.release()
                return payload.get("isos") or []
            except Exception:                               # noqa: BLE001
                return []

        found = []
        with futures.ThreadPoolExecutor(max_workers=len(shares)) as pool:
            for result in pool.map(run, shares):
                found.extend(result)
        return found

    if isoreader is None:                                   # pragma: no cover
        return lambda entries: []
    return identify


def image_identifier(lister, budget=None, on_progress=None):
    """A callable that reads the title ID out of each disc image handed to it.

    **This is the slow path and it is opt-in on purpose.** Every image costs a
    connection, a seek and a handful of reads on a console that may be busy; on
    a shelf of two dozen games the diagnostic measured that in minutes rather
    than seconds, which is exactly why its own games collector made per-image
    identification a thing the user asks for. Nothing here is cheaper than it
    was there, so nothing here gets to make it the default.

    It is also only ever handed the entries whose names carry no ID, which on
    a normally named console is none of them.

    ps3diag.isoreader does the reading. It never downloads an image whole and
    it never raises.

    on_progress(done, total, name) is handed straight through, so the screen
    can say which image it is on. It ran for several minutes on a real console
    with nothing on screen at all, which looks exactly like a hang.
    """
    if isoreader is None:                                   # pragma: no cover
        return lambda entries: []

    def identify(entries):
        reader = isoreader.reader_for(lister)
        try:
            payload = isoreader.identify_isos(
                entries, reader,
                total_budget=(budget if budget is not None
                              else isoreader.DEFAULT_TOTAL_BUDGET),
                on_progress=on_progress)
        finally:
            reader.release()
        return payload.get("isos") or []

    return identify


def _title_folders(lister, notes):
    """The title ID folders under /dev_hdd0/game, from one listing.

    detect's constants rather than new ones: the shape of a title ID folder and
    the cap on how many are looked at are its decisions, and having a second
    opinion on either is how two parts of one program start disagreeing about
    what is installed.
    """
    listing = lister.list_dir(detect.GAME_ROOT + "/")
    entries, _unparsed = parsers.parse_ftp_list(listing)
    folders = [entry["name"].upper() for entry in entries
               if entry["kind"] == "directory"
               and detect.TITLE_DIR.match(entry["name"].upper())]
    if len(folders) > detect.MAX_TITLE_DIRS:
        notes.append(f"This console has {len(folders)} games installed. The "
                     f"first {detect.MAX_TITLE_DIRS} were checked.")
        folders = folders[:detect.MAX_TITLE_DIRS]
    return folders


def read_installed_version(param_sfo_reader, title_id):
    """(version, detail). APP_VER out of the title's own PARAM.SFO.

    ps3diag.isoid parses the file and ps3diag.transport allows exactly this
    path to be fetched verbatim. A title with no PARAM.SFO is normal -- plenty
    of folders under /dev_hdd0/game are save data or an installer's leftovers
    -- and it comes back as (None, why), never as a failure.
    """
    path = f"{detect.GAME_ROOT}/{title_id}/PARAM.SFO"
    if param_sfo_reader is None:
        return None, ("This connection has no way to read the game's details "
                      "from the console.")
    try:
        data = param_sfo_reader(path)
    except Exception as exc:                                # noqa: BLE001
        return None, (f"The installed version could not be read from the "
                      f"console ({exc.__class__.__name__}).")
    fields, reason = isoid.parse_param_sfo(data)
    # The console writes 01.19 and a person writes 1.19. isoid owns that
    # conversion; a second opinion on it is how one screen ends up disagreeing
    # with another about which version is installed.
    version = isoid._normalise_version(fields.get("APP_VER"))
    if version:
        return version, ""
    return None, (f"The installed version could not be read from this game's "
                  f"details on the console. "
                  f"({reason or 'PARAM.SFO carries no APP_VER'})")


# --- one row on the screen --------------------------------------------------

#: Why a row cannot be ticked, or "" when it can.
CAN_UPDATE = ""
NO_UPDATE_EVER = "no update was ever released"
UP_TO_DATE = "up to date"
NO_SHA1 = "no checksum published"
UNKNOWN_SOURCE = "the download address is not one of Sony's"
MANIFEST_FAILED = "the update list could not be read"
NOT_LISTED = "Sony has no entry for this title"
REMEMBERED = "this is what the last check found"

#: The places a title can be found, and the places a scan can look. A scan
#: removes a title only when it looked in the place that title came from and
#: did not find it there. Anything else leaves it alone: a pass that was
#: skipped, or that died part way, knows nothing.
FROM_GAME_FOLDER = "game folder"
FROM_DISC_IMAGE = "disc image"
FROM_MEMORY = "previous scan"


@dataclass
class TitleUpdate:
    """One game, as the screen shows it. Nothing is ticked by default."""

    title_id: str = ""
    name: str = ""
    #: Where this title was found: FROM_GAME_FOLDER, FROM_DISC_IMAGE or
    #: FROM_MEMORY. A scan may only remove a title whose place it looked in.
    found_in: str = ""
    #: When a scan last saw it, ISO format. Empty for a row never merged.
    last_seen: str = ""
    installed: str = None
    latest: str = None
    size: int = 0
    package: Package = None
    #: "" when this row can be updated; otherwise why not, in a short phrase.
    blocked: str = ""
    #: A sentence for the user. Always populated.
    detail: str = ""
    #: True when this game has no title update installed at all. Kept apart
    #: from installed being None, which means one is installed and could not be
    #: read. "none" and "not known" are different answers and the screen says
    #: so; the row that has never been patched is the one worth acting on.
    nothing_installed: bool = False

    @property
    def out_of_date(self):
        """True only when both versions were read and Sony's is the greater.

        A game whose installed version could not be read is not called out of
        date. It might be, and it might not, and saying so either way would be
        a guess presented as a fact.
        """
        if self.latest is None or self.installed is None:
            return False
        return _newer(self.latest, self.installed)

    @property
    def updatable(self):
        return not self.blocked and self.package is not None

    @property
    def size_text(self):
        return human_size(self.size) if self.size else ""

    @property
    def installed_text(self):
        if self.installed:
            return self.installed
        if self.nothing_installed:
            return "none"
        return "not known"

    @property
    def latest_text(self):
        if self.latest:
            return self.latest
        if self.blocked == NO_UPDATE_EVER:
            return "none published"
        if self.blocked == NOT_LISTED:
            return "none published"
        return "not known"

    @property
    def state_text(self):
        if self.blocked == NO_UPDATE_EVER:
            return "No update exists"
        if self.blocked == REMEMBERED:
            return "From the last check"
        if self.blocked == NOT_LISTED:
            return "Not in Sony's list"
        if self.blocked == MANIFEST_FAILED:
            return "Could not be checked"
        if self.out_of_date:
            return "Update available"
        if self.nothing_installed:
            # Nothing is installed, so there is nothing to be behind. Whether
            # there is an update to offer is the only question left.
            return "Update available" if self.package else "None installed"
        if self.installed is None:
            return "Cannot tell"
        return "Up to date"


# --- what was found last time ----------------------------------------------

#: Where the last scan is kept, inside the shell's settings dictionary. Keyed
#: by console address: two consoles have two different sets of games, and
#: showing one console's list for another would be worse than showing none.
REMEMBERED_KEY = "updates_seen"

#: How long a remembered scan is worth offering. Past this the console has
#: probably had games added or removed and a full look is the honest answer.
REMEMBERED_DAYS = 30


def remember_scan(settings, host, rows, now=None):
    """Keep what this scan found, so the next visit has something to show.

    Only what the table shows: title, versions and what state it was in. No
    package URLs and no checksums -- those come from Sony every time, and a
    stale one is exactly the thing that must never be acted on.
    """
    if settings is None or not host:
        return None
    seen = settings.get(REMEMBERED_KEY)
    if not isinstance(seen, dict):
        seen = {}
    seen[host] = {
        "checked": (now or datetime.datetime.now()).isoformat(timespec="seconds"),
        "titles": [
            {"title_id": row.title_id, "name": row.name,
             "installed": row.installed, "latest": row.latest,
             "size": row.size, "blocked": row.blocked,
             "found_in": row.found_in or FROM_MEMORY,
             "last_seen": row.last_seen,
             "behind": bool(row.out_of_date or (row.nothing_installed
                                                and row.package is not None))}
            for row in rows],
    }
    settings[REMEMBERED_KEY] = seen
    return seen[host]


def remembered_scan(settings, host, now=None, max_days=REMEMBERED_DAYS):
    """What the last scan of this console found, or None.

    None for a console never scanned, for a remembered scan too old to be
    worth offering, and for anything that does not read back as one. A
    settings file edited by hand or written by an older build must not be able
    to put a screen into a state it cannot get out of.
    """
    if settings is None or not host:
        return None
    seen = settings.get(REMEMBERED_KEY)
    if not isinstance(seen, dict):
        return None
    entry = seen.get(host)
    if not isinstance(entry, dict) or not isinstance(entry.get("titles"), list):
        return None
    stamp = entry.get("checked")
    try:
        when = datetime.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    age = (now or datetime.datetime.now()) - when
    if age.days > max_days or age.total_seconds() < 0:
        return None
    return entry


def remembered_rows(entry):
    """The remembered titles as screen rows, for showing while nothing is
    known. Every one is blocked: nothing remembered may be acted on."""
    rows = []
    for item in (entry or {}).get("titles", []):
        if not isinstance(item, dict) or not item.get("title_id"):
            continue
        rows.append(TitleUpdate(
            title_id=str(item.get("title_id")),
            name=str(item.get("name") or item.get("title_id")),
            installed=item.get("installed"),
            latest=item.get("latest"),
            size=item.get("size") or 0,
            found_in=str(item.get("found_in") or FROM_MEMORY),
            last_seen=str(item.get("last_seen") or ""),
            blocked=REMEMBERED,
            detail="This is what the last check found. Check again to see "
                   "what is true now."))
    return rows


def titles_behind(entry):
    """The title IDs that were not up to date last time, in order."""
    out = []
    for item in (entry or {}).get("titles", []):
        if isinstance(item, dict) and item.get("behind") and \
                item.get("title_id"):
            out.append(str(item["title_id"]))
    return out


def merge_scan(previous, fresh, looked_in=(), looked_at=None, now=None):
    """One list per console, added to rather than replaced.

    Rows appeared and disappeared between scans because each scan replaced the
    list with whatever it happened to find. The disc-image pass is the slow
    part, so any scan that skipped it, or lost a worker part way through it,
    dropped every image-only game from the table while nothing on the console
    had changed.

    So a scan may only remove a title from a place it actually looked:

    * `looked_in` is the set of places this scan covered, out of
      FROM_GAME_FOLDER and FROM_DISC_IMAGE. A pass that was skipped or that
      did not finish is not in it, and nothing found that way is removed.
    * `looked_at` is a list of title IDs for a partial scan. Only those are
      updated or removed; everything else is left exactly as it was.

    A scan that found nothing and looked nowhere changes nothing at all.
    """
    stamp = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    kept = {row.title_id: row for row in previous or []}
    covered = set(looked_in or ())
    asked = ({str(title_id).upper() for title_id in looked_at}
             if looked_at is not None else None)

    seen = set()
    for row in fresh or []:
        row.last_seen = stamp
        if not row.found_in:
            row.found_in = FROM_GAME_FOLDER
        kept[row.title_id] = row
        seen.add(row.title_id)

    for title_id, row in list(kept.items()):
        if title_id in seen:
            continue
        if asked is not None:
            # A partial scan speaks only for the titles it was given.
            if title_id.upper() in asked:
                del kept[title_id]
            continue
        where = row.found_in or FROM_MEMORY
        if where in covered:
            # Looked in the place this came from, and it was not there.
            del kept[title_id]

    # By title ID, which is the order check_titles produces and so the order
    # the table has always been in. Sorting by name here would reshuffle the
    # list every time a manifest named something differently.
    return [kept[key] for key in sorted(kept)]


def row_for(installed, manifest):
    """One InstalledTitle plus what the manifest said, as a screen row."""
    name = manifest.name or installed.known_name or installed.title_id
    row = TitleUpdate(title_id=installed.title_id, name=name,
                      installed=installed.version,
                      nothing_installed=installed.no_update_installed,
                      found_in=(FROM_DISC_IMAGE
                                if installed.source == "disc image"
                                else FROM_GAME_FOLDER))
    if manifest.empty and not manifest.packages:
        row.blocked = NO_UPDATE_EVER
        row.detail = ("Sony never released a title update for this game, so "
                      "there is nothing to install. The game is complete as "
                      "it is.")
        if installed.no_update_installed:
            row.detail = ("Sony never released a title update for this game. "
                          "There is none installed because there has never "
                          "been one to install, and nothing is missing.")
        return row

    package = manifest.latest
    if package is None:
        # Entries came back but not one of them had a URL, a size and a sha1.
        row.blocked = NO_SHA1
        row.detail = ("Sony's update list for this game does not publish a "
                      "checksum for the download, so this program has no way "
                      "to prove a downloaded file is the right one. It will "
                      "not install a file it cannot check.")
        return row

    row.package = package
    # 01.19 as Sony writes it, 1.19 as a person does. The installed version has
    # already been through isoid's conversion, and a screen showing 1.05
    # against 01.19 looks like two different kinds of number.
    row.latest = isoid._normalise_version(package.version) or package.version
    row.size = package.size
    if not is_sony_url(package.url):
        row.package = None
        row.blocked = UNKNOWN_SOURCE
        row.detail = ("The download address in Sony's update list is not on "
                      "one of Sony's own servers, so this program will not "
                      "fetch it.")
        return row

    if installed.no_update_installed:
        # The case the cross-reference exists to catch: the game is on the
        # console and has never been patched. Not a failure, and the most
        # useful row on the screen.
        where = {"disc image": "as a disc image",
                 "folder game": "as a copied game"}.get(installed.source, "")
        row.detail = (f"This game is on the console {where} with no title "
                      f"update installed at all. The newest Sony published "
                      f"is {package.version}, which is "
                      f"{human_size(package.size)}.").replace("  ", " ")
        return row

    if installed.version is None:
        row.detail = (f"The newest title update Sony published is "
                      f"{package.version}. This program could not read which "
                      f"version is installed, so it cannot tell you whether "
                      f"you already have it. Installing it again is harmless. "
                      f"({installed.detail})".strip())
        return row

    if not row.out_of_date:
        row.blocked = UP_TO_DATE
        row.detail = (f"Version {installed.version} is installed and "
                      f"{package.version} is the newest Sony published, so "
                      f"this game is up to date.")
        return row

    row.detail = (f"Version {installed.version} is installed. Sony published "
                  f"{package.version}, which is {human_size(package.size)}.")
    return row


def is_sony_url(url):
    """Whether a URL out of the manifest points at one of Sony's own hosts."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    return any(host == suffix.lstrip(".") or host.endswith(suffix)
               for suffix in PACKAGE_HOSTS)


def check_titles(installed_titles, fetcher=None, progress=None,
                 cancelled=None):
    """Ask Sony about each installed title. Returns a list of TitleUpdate.

    One request per game, done here rather than in the screen so the whole of
    this is testable without Qt. A game whose manifest could not be read does
    not stop the others: it comes back as a row that says so.
    """
    rows = []
    total = len(installed_titles)
    for index, installed in enumerate(installed_titles, 1):
        if cancelled is not None and cancelled():
            break
        if progress:
            progress((index, total, installed.title_id))
        try:
            manifest = fetch_manifest(installed.title_id, fetcher=fetcher)
        except TlsNotTrusted:
            # The one failure that is about the network rather than the game.
            # Every later request would fail the same way, so it is raised
            # rather than turned into forty identical rows.
            raise
        except TitleNotListed as exc:
            # Sony answered, and the answer was that it has never heard of
            # this title. Expansion discs and homebrew come back this way.
            rows.append(TitleUpdate(
                title_id=installed.title_id,
                name=installed.known_name or installed.title_id,
                installed=installed.version,
                nothing_installed=installed.no_update_installed,
                blocked=NOT_LISTED, detail=str(exc)))
            continue
        except (ManifestUnreadable, ValueError) as exc:
            row = TitleUpdate(title_id=installed.title_id,
                              name=installed.known_name or installed.title_id,
                              installed=installed.version,
                              nothing_installed=installed.no_update_installed,
                              blocked=MANIFEST_FAILED, detail=str(exc))
            rows.append(row)
            continue
        rows.append(row_for(installed, manifest))
    return rows


# --- the packages folder ----------------------------------------------------

@dataclass
class PackagesFolder:
    """What is already sitting in /dev_hdd0/packages."""

    names: list = field(default_factory=list)
    #: {name: bytes} for the same files, so a screen can say how big they are
    #: without asking the console a second time.
    sizes: dict = field(default_factory=dict)
    #: True when the folder could not be listed at all.
    unknown: bool = False
    reason: str = ""

    @property
    def empty(self):
        return not self.names and not self.unknown


def scan_titles(lister, title_ids, on_stage=None):
    """Read the installed version of a named handful of titles.

    The short way round for a second look. A full scan walks every game folder
    and every device and opens any disc image whose name carries no ID, which
    is the part that takes minutes; this reads one small file per title and
    nothing else.

    A title that has gone from the console since the last check is dropped,
    which is the honest answer: it cannot be updated if it is not there.
    """
    reader = getattr(lister, "download_bytes", None)
    found = []
    wanted = [str(title_id).upper() for title_id in (title_ids or [])]
    for index, title_id in enumerate(wanted, start=1):
        if on_stage:
            on_stage(f"Reading the version of {title_id} "
                     f"({index} of {len(wanted)})")
        version, detail = read_installed_version(reader, title_id)
        path = f"{detect.GAME_ROOT}/{title_id}"
        found.append(InstalledTitle(title_id=title_id, path=path,
                                    version=version, detail=detail))
    return found


def installed_title_ids(lister):
    """Which titles have a folder under /dev_hdd0/game. Never raises.

    Enough to say whether a package sitting in the packages folder has already
    gone in. A title with a folder is not proof the install finished, which is
    why this only ever greys a row rather than deciding anything.
    """
    try:
        return _title_folders(lister, [])
    except Exception:                                       # noqa: BLE001
        return []


def console_packages(lister, installed=()):
    """Every .pkg sitting in the console's packages folder, described.

    This is the recovery path. An install that failed, or a copy that was
    interrupted, leaves the package on the console; reading the folder finds it
    again after a restart of this program or of the console, which remembering
    what this session uploaded would not.

    The head of each file is read for its content ID. A name is read off the
    file name where the ID is in it, because that costs nothing.
    """
    folder = inspect_packages_folder(lister)
    if folder.unknown:
        return [], folder.reason
    known = {str(title_id).upper() for title_id in (installed or ())}
    found = []
    for name in folder.names:
        item = PackageFile(path=f"{PACKAGES_DIR}/{name}", filename=name,
                           size=folder.sizes.get(name, 0))
        try:
            head = lister.download_bytes(item.path, max_bytes=PKG_HEAD_BYTES)
        except Exception as exc:                            # noqa: BLE001
            item.reason = (f"The start of this file could not be read from "
                           f"the console ({exc.__class__.__name__}), so there "
                           f"is nothing to say about what is in it.")
            found.append(item)
            continue
        _read_header(item, head)
        if not item.title_id:
            # The file name carries the ID on anything Sony published.
            item.title_id = regioncodes.find_title_id(name) or ""
        if item.title_id:
            record = titles.config_for(item.title_id)
            if record:
                item.title = record.get("name") or item.title
            item.installed = item.title_id.upper() in known
        found.append(item)
    return found, ""


def inspect_packages_folder(lister):
    """List /dev_hdd0/packages before anything is put in it.

    The install call names one file, so nothing already in this folder is
    installed by anything this program does. It used to be believed that the
    whole folder went in at once, and both screens warned about it; that was
    wrong and the warning is gone.

    The listing is still worth having: it is how the screen can say a file of
    the same name is already there, and it is a fair thing to show somebody
    before writing into a folder they may be keeping things in.
    """
    try:
        listing = lister.list_dir(PACKAGES_DIR + "/")
    except Exception as exc:                                # noqa: BLE001
        return PackagesFolder(
            unknown=True,
            reason=(f"The folder the console installs updates from could not "
                    f"be read ({exc.__class__.__name__})."))
    entries, _unparsed = parsers.parse_ftp_list(listing)
    files = [entry for entry in parsers.real_entries(entries)
             if entry["kind"] != "directory"]
    return PackagesFolder(
        names=sorted(entry["name"] for entry in files),
        sizes={entry["name"]: entry.get("size") or 0 for entry in files})


# --- free space -------------------------------------------------------------

def free_bytes_for(devices, device="dev_hdd0"):
    """Free bytes on one device, out of what the storage collector reported.

    None when the console did not say. None is not zero: a console that has not
    reported its free space has not said there is none, and refusing on that
    basis would refuse every console whose webMAN reports it differently.
    """
    for entry in devices or []:
        if (entry.get("device") or "").lower() == device.lower():
            value = entry.get("free_bytes")
            return value if isinstance(value, int) else None
    return None


def check_space(free, needed, device="dev_hdd0"):
    """Raise NotEnoughSpace, or return quietly. Called before downloading.

    Refusing before the download is the point. Filling somebody's hard drive
    and only then discovering it, having already spent twenty minutes of their
    broadband on it, is the failure this exists to prevent.
    """
    if free is None:
        return None
    if free >= needed + SPACE_MARGIN:
        return None
    raise NotEnoughSpace(
        f"There is not enough room on the console. The update needs "
        f"{human_size(needed)} and {device} has {human_size(free)} free.\n\n"
        f"Delete something from the console and try again. A game you have "
        f"finished or some old video clips will do it. Nothing has been "
        f"downloaded.")


def check_local_space(folder, needed):
    """The same question about this computer's own disk."""
    try:
        free = _disk_free(folder)
    except OSError:
        return None
    if free >= needed + (64 * 1024 * 1024):
        return None
    raise NotEnoughSpace(
        f"There is not enough room on this computer to download the update. "
        f"It is {human_size(needed)} and there is {human_size(free)} free. "
        f"Nothing has been downloaded.")


def _disk_free(folder):
    import shutil
    return shutil.disk_usage(folder).free


# --- downloading ------------------------------------------------------------

#: What a package file may be called once it is on the console. The name comes
#: out of Sony's manifest, which makes it the one string here chosen by
#: somebody else, and it is about to become part of an FTP path.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def package_filename(package, title_id=""):
    """A safe file name for one package.

    Taken from the URL when the URL's last segment is a plain .pkg name, and
    invented from the title ID and version when it is anything else. Never
    trusted verbatim: a name with a slash or a ".." in it would put the upload
    somewhere other than /dev_hdd0/packages.
    """
    tail = ""
    try:
        tail = urlsplit(package.url or "").path.rsplit("/", 1)[-1]
    except ValueError:
        tail = ""
    if SAFE_NAME.match(tail) and tail.lower().endswith(".pkg"):
        return tail
    version = re.sub(r"[^0-9A-Za-z.]", "", package.version or "0")
    cleaned = re.sub(r"[^0-9A-Za-z]", "", (title_id or "update").upper())
    return f"{cleaned or 'UPDATE'}_{version or '0'}.pkg"


#: A PS3 package carries its own sha1 in a 32-byte footer: the digest in the
#: first 20 bytes, then twelve zero bytes. The value Sony publishes in ver.xml
#: is that digest, and it covers the file WITHOUT the footer -- hashing the
#: whole file never matches, which is why every download failed verification.
#:
#: Confirmed on BLES01031 (Black Ops, 65,334,960 bytes):
#:   manifest sha1sum    036c533b51df35d803df12f21a4abeb877fe43f5
#:   sha1 of whole file  3d3a117d57323dbb4fcc84cef6636a39dcf88e9d   no
#:   sha1 of file[:-32]  036c533b51df35d803df12f21a4abeb877fe43f5   yes
PKG_FOOTER_BYTES = 32
PKG_FOOTER_DIGEST_BYTES = 20


@dataclass
class Downloaded:
    """What arrived, in the three forms the check needs."""

    body_sha1: str = ""    #: sha1 of everything but the footer
    footer_sha1: str = ""  #: the digest the package carries in its footer
    size: int = 0          #: bytes written


def download_package(package, destination, stream=None, on_block=None,
                     cancelled=None, timeout=DOWNLOAD_TIMEOUT):
    """Fetch one package to a local file. Returns a Downloaded.

    The hash is taken from the stream as it goes past rather than by reading
    the file back, so a short write to the local disk shows up as a mismatch
    rather than being hashed twice out of the same bad copy.

    The last 32 bytes are held back rather than hashed: they are the package's
    own footer, and Sony's published digest covers the file without them. The
    tail buffer keeps this single-pass, so a 4 GB package is still never held
    in memory.

    A stream that stops early is a failure, not a smaller file: the manifest
    said how many bytes there are.
    """
    if not is_sony_url(package.url):
        raise DownloadFailed(
            "The download address for this update is not one of Sony's, so it "
            "was not fetched.")
    opener = urllib_stream if stream is None else stream
    digest = hashlib.sha1()
    pending = b""
    total = 0
    try:
        handle = opener(package.url, timeout=timeout)
    except Exception as exc:                                # noqa: BLE001
        raise DownloadFailed(
            f"The update could not be downloaded from Sony. Check this "
            f"computer is connected to the internet and try again. "
            f"({exc.__class__.__name__})") from exc
    try:
        with open(destination, "wb") as out:
            while True:
                if cancelled is not None and cancelled():
                    raise DownloadFailed("The download was stopped.")
                try:
                    block = handle.read(BLOCK)
                except Exception as exc:                    # noqa: BLE001
                    raise DownloadFailed(
                        f"The download stopped after "
                        f"{human_size(total)} of {human_size(package.size)}. "
                        f"Check this computer's internet connection and try "
                        f"again. ({exc.__class__.__name__})") from exc
                if not block:
                    break
                out.write(block)
                total += len(block)
                # Everything but the final 32 bytes is hashed. Holding those
                # back as they stream past is what keeps this one pass.
                pending += block
                if len(pending) > PKG_FOOTER_BYTES:
                    digest.update(pending[:-PKG_FOOTER_BYTES])
                    pending = pending[-PKG_FOOTER_BYTES:]
                if on_block:
                    on_block(total, package.size)
    finally:
        close = getattr(handle, "close", None)
        if close:
            try:
                close()
            except Exception:                               # noqa: BLE001
                pass
    if package.size and total != package.size:
        raise DownloadFailed(
            f"The download stopped after {human_size(total)} of the "
            f"{human_size(package.size)} Sony said the update is. Nothing has "
            f"been copied to the console. Check this computer's internet "
            f"connection and try again.")
    footer = pending[-PKG_FOOTER_BYTES:] if total >= PKG_FOOTER_BYTES else b""
    return Downloaded(
        body_sha1=digest.hexdigest(),
        footer_sha1=footer[:PKG_FOOTER_DIGEST_BYTES].hex() if footer else "",
        size=total)


#: Advice for a first failure. A download that arrived over plain HTTP really
#: can be corrupted in transit, and trying again really does fix that.
RETRY_ADVICE = (
    "This can happen if the download was corrupted on the way, so it is worth "
    "trying once more.")

#: Advice once the same file has failed the same check the same way twice.
#: Telling somebody to try again is worse than useless here: a systematic
#: mismatch reproduces every time, and the tool would send them round the same
#: several hundred megabyte download forever.
SETTLED_ADVICE = (
    "This is the second time this download has failed in exactly the same "
    "way, so it is not a corrupted transfer and trying again will not change "
    "it. Either Sony's list and the file it points at disagree, or something "
    "between this computer and Sony is consistently handing back a different "
    "file.\n\n"
    "This tool will not install it. The console can still fetch the update "
    "itself: start the game with the console online and let it download.")


def verify_download(package, downloaded, history=None):
    """The check the whole of this feature rests on.

    Three things have to agree before anything is written to a console:

    * the body digest -- sha1 of the file WITHOUT its last 32 bytes -- against
      the sha1sum in Sony's manifest,
    * the digest the package carries in its own footer, against that same
      manifest value,
    * the number of bytes that arrived, against the manifest's size.

    The footer check is not redundant. The body digest proves the payload is
    the one Sony hashed; the footer proves the copy is internally consistent,
    which is what the console itself will look at. A file that passed one and
    failed the other would be a file somebody had assembled.

    A mismatch stops everything. There is no option, no override and no
    degraded path where it becomes a warning and the upload happens anyway:
    the sha1 is the only evidence that the file about to be written to
    somebody's console is the one Sony published rather than whatever answered
    a plain HTTP request on an unknown network.

    `history` is an optional dict the caller keeps for the length of a session.
    It is how a second identical failure can give different advice from the
    first, and it is passed in rather than kept in a module global so that two
    windows, or a test, cannot see each other's attempts.
    """
    expected = (package.sha1sum or "").lower()
    if not expected:
        raise VerificationFailed(
            "Sony's update list does not publish a checksum for this "
            "download, so there is no way to prove the file is the right one. "
            "It has not been copied to the console.")

    # Accepts the digest on its own as well as a Downloaded, so the check can
    # still be called with nothing but a hash in hand.
    if isinstance(downloaded, str):
        downloaded = Downloaded(body_sha1=downloaded, footer_sha1="",
                                size=package.size)

    body = (downloaded.body_sha1 or "").lower()
    footer = (downloaded.footer_sha1 or "").lower()

    if package.size and downloaded.size != package.size:
        _refuse(package, history, ("size", downloaded.size),
                f"The update that was downloaded is not the size Sony "
                f"published.\n\n"
                f"Sony's list says {package.size:,} bytes "
                f"({human_size(package.size)}) and {downloaded.size:,} bytes "
                f"({human_size(downloaded.size)}) arrived. Nothing has been "
                f"copied to the console and the downloaded file has been "
                f"deleted.")

    if body != expected:
        _refuse(package, history, ("body", body),
                "The update that was downloaded is not the file Sony "
                "published.\n\n"
                "Its checksum does not match the one in Sony's update list. "
                "Nothing has been copied to the console and the downloaded "
                "file has been deleted.")

    if footer and footer != expected:
        _refuse(package, history, ("footer", footer),
                "The update that was downloaded does not agree with itself."
                "\n\n"
                "Every PS3 package ends with its own checksum, and the one in "
                "this file is not the checksum Sony's update list gives. "
                "Nothing has been copied to the console and the downloaded "
                "file has been deleted.")

    return True


def _refuse(package, history, fingerprint, reason):
    """Raise, with advice that depends on whether this has happened before."""
    key = package.url or package.sha1sum
    repeated = history is not None and history.get(key) == fingerprint
    if history is not None:
        history[key] = fingerprint
    raise VerificationFailed(
        f"{reason}\n\n{SETTLED_ADVICE if repeated else RETRY_ADVICE}")


# --- the whole sequence -----------------------------------------------------

@dataclass
class Delivered:
    """What deliver() did, for the screen and for a test to assert on."""

    title_id: str = ""
    filename: str = ""
    remote_path: str = ""
    bytes_sent: int = 0
    sha1: str = ""
    #: True when the console already had this update and nothing was sent.
    skipped: bool = False
    reason: str = ""


def deliver(row, writer, folder, stream=None, progress=None, cancelled=None,
            free_bytes=None, history=None, installed_reader=None):
    """Download, verify, and only then upload. Returns a Delivered.

    The order is the feature. Nothing touches the writer until verify_download
    has returned, and verify_download raises rather than returning False, so
    there is no way to reach the upload with a file that failed the check --
    not by forgetting a branch and not by a later edit that stops reading a
    return value. tests/test_updates.py hands this a writer that raises the
    moment it is touched and proves the mismatch path never reaches it.
    """
    package = row.package
    if package is None or not row.updatable:
        raise UpdateError(f"{row.name} cannot be updated: "
                          f"{row.blocked or 'there is no update for it'}.")

    # Asked again rather than trusting the scan. On a second run after a
    # failure, half the queue may already have gone in, and the scan that
    # produced these rows happened before any of it. Re-reading one small file
    # is cheaper than several hundred megabytes fetched for nothing.
    #
    # The installed version comes from the title's own PARAM.SFO, not from the
    # package file name. The name carries a version field, but which of its
    # fields means what is not something worth guessing at when the number the
    # console actually reports is one small read away.
    if installed_reader is not None:
        current, _detail = installed_reader(row.title_id)
        if current and compare_versions(current, package.version) >= 0:
            return Delivered(
                title_id=row.title_id, skipped=True,
                reason=(f"{row.name} is already on {current}, which is not "
                        f"behind {package.version}. Nothing was downloaded."))

    check_space(free_bytes, package.size)
    check_local_space(folder, package.size)

    filename = package_filename(package, row.title_id)
    local = os.path.join(folder, filename)

    def on_block(done, total):
        if progress:
            progress(("download", row.title_id, done, total))

    arrived = download_package(package, local, stream=stream,
                               on_block=on_block, cancelled=cancelled)
    try:
        verify_download(package, arrived, history=history)
    except VerificationFailed:
        # A file that failed the check is not left lying about where somebody
        # could find it and install it by hand.
        _remove(local)
        raise

    try:
        remote, sent = upload_to_packages(local, writer, filename=filename,
                                          progress=progress,
                                          label=row.title_id)
    finally:
        _remove(local)

    return Delivered(title_id=row.title_id, filename=filename,
                     remote_path=remote, bytes_sent=sent,
                     sha1=arrived.body_sha1)


def upload_to_packages(local, writer, filename=None, progress=None, label=""):
    """Copy one local file into the console's packages folder.

    The one implementation of this. Both cards that put a package on a console
    -- the update flow above and the install-packages screen -- call it, so
    there is one answer to what the folder is called, what happens when the
    console goes away part way through, and what the user is told about it.

    Returns (remote path, bytes sent).
    """
    filename = filename or os.path.basename(local)
    if not SAFE_NAME.match(filename):
        raise UploadFailed(
            f"{filename} cannot be copied to the console: its name has "
            f"characters in it that the console's file system will not take. "
            f"Rename it using only letters, numbers, full stops, dashes and "
            f"underscores, then try again.")
    remote = f"{PACKAGES_DIR}/{filename}"
    try:
        writer.make_dir(PACKAGES_DIR)
        def watch(done, total):
            if progress:
                progress(("upload", label or filename, done, total))

        sent = writer.store(local, remote, on_block=watch)
    except Exception as exc:                                # noqa: BLE001
        raise UploadFailed(
            f"The console stopped answering while {filename} was being copied "
            f"to it, so a part-copied file may be left in "
            f"{PACKAGES_DIR}. Check the console is switched on, on the same "
            f"network and sitting on its main menu, then try again. "
            f"({exc.__class__.__name__})") from exc
    return remote, sent or 0


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --- a package file the user chose themselves --------------------------------
#
# Read so that somebody who picked the wrong file finds out before it is on
# their console. This is a sanity check and nothing more: it says whether the
# file is shaped like a package and which title it says it is for. It is not
# verification, there is nothing to verify a user's own file against, and no
# part of it may ever be presented as though there were.

#: Every PS3 package starts with these four bytes.
PKG_MAGIC = b"\x7fPKG"

#: The content ID sits at a fixed offset in the header and is plain ASCII, e.g.
#: EP0002-BLES01717_00-CODBLOPS2PATCH19. The title ID is the middle of it.
PKG_CONTENT_ID_AT = 0x30
PKG_CONTENT_ID_LENGTH = 0x24

#: The total size the package claims, as a big-endian 64 bit count.
PKG_TOTAL_SIZE_AT = 0x18

#: How far into the file to look for an unencrypted PARAM.SFO. Some packages
#: carry one in the header area and some do not; where there is one it gives
#: the game's name, and where there is not the content ID is shown instead.
PKG_SFO_SEARCH = 256 * 1024

SFO_MAGIC = b"\x00PSF"

CONTENT_ID = re.compile(r"^[A-Z]{2}\d{4}-([A-Z]{4}\d{5})_\d{2}-[A-Za-z0-9_]+$")


@dataclass
class PackageFile:
    """One .pkg the user picked off their own computer."""

    path: str = ""
    filename: str = ""
    size: int = 0
    content_id: str = ""
    title_id: str = ""
    title: str = None
    #: False when the file is not shaped like a package at all.
    is_package: bool = False
    #: True when the console already has this title installed. Only ever set
    #: for a package read off the console.
    installed: bool = False
    #: Why not, or what else is worth saying. Always a sentence when set.
    reason: str = ""

    @property
    def size_text(self):
        return human_size(self.size)

    @property
    def describes_as(self):
        """What to show in the "what it is" column."""
        if not self.is_package:
            return self.reason or "not a package file"
        if self.title:
            return self.title
        if self.content_id:
            return self.content_id
        return "a package, with nothing in its header naming it"


def read_package_file(path):
    """Look at one local .pkg. Never raises; the trouble comes back in reason.

    A file that is not a package is reported as one that is not a package. The
    console would refuse it anyway, and finding out here costs the user a
    second rather than an upload.
    """
    item = PackageFile(path=path, filename=os.path.basename(path))
    try:
        item.size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(PKG_SFO_SEARCH)
    except OSError as exc:
        item.reason = (f"This file could not be read from this computer. It "
                       f"may have been moved, renamed or deleted since it was "
                       f"picked. ({exc.__class__.__name__})")
        return item

    return _read_header(item, head)


#: Enough of a package to carry its content ID and the size it claims. Both
#: live in the first 0x54 bytes; the rest of PKG_SFO_SEARCH is only wanted when
#: looking for a title name, which is not worth a quarter megabyte a file over
#: FTP.
PKG_HEAD_BYTES = 4096


def _read_header(item, head):
    """Fill in what a package's first bytes say about it. Never raises."""
    if head[:4] != PKG_MAGIC:
        item.reason = ("This is not a PlayStation 3 package file. Its first "
                       "few bytes are not the ones every .pkg starts with, so "
                       "the console would refuse it.")
        return item

    item.is_package = True

    if len(head) >= PKG_CONTENT_ID_AT + PKG_CONTENT_ID_LENGTH:
        raw = head[PKG_CONTENT_ID_AT:
                   PKG_CONTENT_ID_AT + PKG_CONTENT_ID_LENGTH]
        content_id = raw.split(b"\x00", 1)[0].decode("ascii",
                                                    "replace").strip()
        if CONTENT_ID.match(content_id):
            item.content_id = content_id
            item.title_id = CONTENT_ID.match(content_id).group(1)
        elif content_id:
            item.content_id = content_id

    # A truncated download is the common way a package is broken, and the file
    # says how big it should be. Said plainly rather than treated as a check:
    # a package whose size matches has not been proved to be anything.
    claimed = 0
    if len(head) >= PKG_TOTAL_SIZE_AT + 8:
        claimed = int.from_bytes(
            head[PKG_TOTAL_SIZE_AT:PKG_TOTAL_SIZE_AT + 8], "big")
    if claimed and item.size and claimed != item.size:
        item.reason = (f"This file says it should be {human_size(claimed)} "
                       f"but it is {human_size(item.size)}, so it is "
                       f"incomplete. Copying it across would waste the time "
                       f"and the console would refuse it.")
        item.is_package = False
        return item

    offset = head.find(SFO_MAGIC)
    if offset >= 0:
        fields, _reason = isoid.parse_param_sfo(head[offset:])
        title = fields.get("TITLE")
        if isinstance(title, str) and title.strip():
            item.title = title.strip()
        if not item.title_id:
            candidate = (fields.get("TITLE_ID") or "").strip().upper()
            if TITLE_ID.match(candidate):
                item.title_id = candidate
    return item


def read_package_files(paths):
    """read_package_file over a list, in the order they were given."""
    return [read_package_file(path) for path in paths]


#: Said on the install-packages screen, in the screen's own words as well.
#: Kept here so there is one wording of it and it cannot drift.
#: Shown on the Install packages screen, which is the screen it is about: a
#: file you chose yourself is the one thing here nothing can vouch for. It used
#: to open by describing what Game updates does, which read as though it had
#: been put on the wrong screen.
NO_WAY_TO_CHECK = (
    "Nothing can check this file for you. You chose it, so there is no "
    "checksum published by anybody to compare it against. Only install "
    "packages you trust and know the source of.")


#: How often the console is asked whether an install has landed.
INSTALL_POLL_SECONDS = 1.0

#: How long one package may take before the queue gives up on it. Generous:
#: a large package on a slow drive is slow, and the cost of waiting is nothing
#: next to the cost of firing the next install into a console still busy with
#: this one.
INSTALL_TIMEOUT_SECONDS = 300.0


@dataclass
class Installed:
    """One package's trip through the install queue."""

    filename: str = ""
    title_id: str = ""
    confirmed: bool = False
    reason: str = ""


def install(actions, filename):
    """Ask the console to install one package that has just been uploaded.

    One call names one file. Asking for the folder did nothing at all: that
    URL is a picker page whose dropdown appends the file name in the browser.
    See ConsoleActions.install_package.
    """
    return actions.install_package(filename)


def installed_checker(lister):
    """Whether a title's directory is on the console yet.

    Through the read-only client: confirming an install is a question, and
    questions go through the client that cannot write. A folder that is not
    there answers 550 and comes back as False.
    """
    def exists(title_id):
        if not title_id:
            return False
        try:
            lister.list_dir(f"{detect.GAME_ROOT}/{title_id}/")
            return True
        except Exception:                                   # noqa: BLE001
            return False
    return exists


# There is deliberately no way to delete an uploaded package from here.
#
# There was. It ran once an install was "confirmed", and confirmation was the
# title's directory appearing under /dev_hdd0/game. That directory appears when
# the console's installer starts, not when it finishes, so a 2.1 GB package was
# deleted after an install that failed with 80010006 and left nothing behind.
#
# A leftover package is visible and actionable: the Install packages screen
# lists what is in the folder and offers to install it again without sending it
# across a second time. That is worth more than reclaiming the space on
# evidence which cannot tell a finished install from a started one.


def install_queue(actions, items, installed_dir, on_progress=None,
                  cancelled=None, poll_seconds=INSTALL_POLL_SECONDS,
                  timeout_seconds=INSTALL_TIMEOUT_SECONDS,
                  sleep=time.sleep, clock=time.monotonic):
    """Install packages one at a time, waiting for each to land.

    Firing them back to back drops them. Seven were sent with no gap on a real
    console and three installed: webMAN ignores an install request while it is
    still busy with the last one, and says nothing about having done so.

    So each call is confirmed before the next is made. A fixed gap was tried
    and rejected: three seconds was enough on a 1TB SSD, which says nothing
    about a slower drive or a bigger package, and a gap that is too short
    drops installs in exactly the way this is here to stop. Asking the console
    whether the title directory has appeared costs nothing when it already
    has.

    `items` are (filename, title_id) in upload order. `installed_dir` is asked
    whether a title has landed. Nothing is ever deleted: see the note above
    this function.

    A package that fails does not stop the ones behind it. Each is tried, each
    is reported, and a failure costs only itself: the file stays on the console
    so it can be installed again without being sent a second time. Only being
    cancelled stops the queue.

    `cancelled` is checked before each install and on every pass of the wait,
    so asking this to stop takes one poll interval rather than however long is
    left of the timeout.
    """
    done = []
    pending = list(items)
    total = len(pending)
    for index, (filename, title_id) in enumerate(pending, start=1):
        if cancelled is not None and cancelled():
            return done + _not_sent(pending[index - 1:], "stopped")
        if on_progress:
            on_progress(("installing", filename, index, total))
        outcome = Installed(filename=filename, title_id=title_id or "")
        try:
            install(actions, filename)
        except (UpdateError, ActionFailed) as exc:
            # The console refused the request outright. Whatever is wrong is
            # not going to be better for the next one.
            outcome.reason = str(exc)
        else:
            outcome.confirmed = _wait_for_install(
                installed_dir, title_id, poll_seconds, timeout_seconds,
                sleep, clock, cancelled)
            if not outcome.confirmed:
                stopped = cancelled is not None and cancelled()
                outcome.reason = (
                    f"{filename} was still installing when this was stopped."
                    if stopped else
                    f"{filename} did not appear on the console within "
                    f"{int(timeout_seconds)} seconds.")
        if not outcome.confirmed:
            done.append(outcome)
            if cancelled is not None and cancelled():
                return done + _not_sent(pending[index:], "stopped")
            # On to the next one. One package failing says nothing about the
            # rest, and stopping here used to leave somebody re-sending files
            # that were already sitting on the console.
            continue
        if on_progress:
            on_progress(("installed", filename, index, total))
        done.append(outcome)
    return done


def _not_sent(items, why):
    """The packages the queue never got to, named."""
    reason = ("not sent: this was stopped before it got that far"
              if why == "stopped" else
              "not sent: the queue stopped before it got this far")
    return [Installed(filename=name, title_id=title or "", reason=reason)
            for name, title in items]


def _wait_for_install(installed_dir, title_id, poll_seconds, timeout_seconds,
                      sleep, clock, cancelled=None):
    """Whether the title's directory turns up before the timeout.

    Cancellation is checked on every pass, so stopping costs one poll interval
    rather than whatever is left of a timeout measured in minutes.

    A package whose title ID is not known cannot be confirmed this way. It is
    still given the console the same pause a confirmation would have taken, so
    the next install is not fired on top of it, and it comes back unconfirmed.
    """
    if not title_id:
        sleep(poll_seconds)
        return False
    deadline = clock() + timeout_seconds
    while True:
        if installed_dir(title_id):
            return True
        if cancelled is not None and cancelled():
            return False
        if clock() >= deadline:
            return False
        sleep(poll_seconds)


INSTALL_NOTICE = (
    "The console is installing now. Look at the television: it shows the "
    "install, and you press O on the console when it has finished. More than "
    "one queues up behind the first, so one press at the end covers them "
    "all.\n\n"
    "If nothing appears within a few seconds, the files are still there. "
    "Open Package Manager on the console, then Install Package Files, and "
    "pick them from the list.")
