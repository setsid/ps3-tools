"""Game updates, end to end, without a console and without Sony.

Nothing in this file opens a socket to anything. Every seam is injected: the
manifest fetcher, the package stream, the FTP lister, the write client and the
console action client. tests/check-no-network.py runs the whole suite under an
audit hook that fails the build on a single non-loopback connect, bind or DNS
lookup, and there is a live PS3 on the network these were written on.

Most of the weight here is on the failures, because the failures are the whole
point of the feature:

  * A sha1 that does not match must stop the flow before anything is written.
    That is tested with a write client that fails the test the moment it is
    touched, so "it did not upload" is proved rather than assumed.
  * An empty manifest means no update was ever released. It is a normal
    answer, not an error, and it must not be shown as one.
  * A certificate that will not verify gets its own message and never gets an
    unverified context. There is a test that greps the module for the ways
    somebody might add one.
  * The install endpoint installs a folder, so anything already in that folder
    is reported to the user rather than quietly installed.
"""

import ftplib
import hashlib
import io
import json
import datetime
import os
import ssl
import tempfile
import unittest
import unittest.mock as mock
import urllib.error

from support import FixtureCase, ROOT, fixture_path

import make_fixtures                                        # noqa: E402

from ps3diag import regioncodes                          # noqa: E402
from ps3tools import consoleactions, detect, updates        # noqa: E402
from ps3tools.consoleactions import (ActionFailed,          # noqa: E402
                                     ActionResponse)
from ps3tools.updates import (DownloadFailed, ManifestUnreadable,
                              NotEnoughSpace, Package, TlsNotTrusted,
                              UploadFailed, VerificationFailed)

GAME = detect.GAME_ROOT
BO2 = "BLES01717"
GT5 = "BCES00569"


def manifest_bytes(name):
    with open(fixture_path("updates", name), "rb") as handle:
        return handle.read()


def param_sfo(version, title_id=BO2, title="A Game"):
    return make_fixtures.param_sfo((("APP_VER", version),
                                    ("CATEGORY", "GD"),
                                    ("TITLE", title),
                                    ("TITLE_ID", title_id)))


def folders(*names):
    """A /dev_hdd0/game listing holding exactly these title folders."""
    return "".join("drwxrwxrwx   1 root     root            0 Sep 06 19:51 "
                   "%s\n" % name for name in names)


def files(*names):
    return "".join("-rw-rw-rw-   1 root     root      1048576 Sep 06 19:51 "
                   "%s\n" % name for name in names)


#: The other two places a console keeps games. /dev_hdd0/game holds installed
#: title updates; these hold the games themselves, and a title in one of these
#: and not in /dev_hdd0/game has never been patched.
GAMES = "/dev_hdd0/GAMES"
PS3ISO = "/dev_hdd0/PS3ISO"
PS2ISO = "/dev_hdd0/PS2ISO"

#: Folder names copied verbatim off a real console. The ID comes first and the
#: game's name is in brackets after it, which is the opposite way round from
#: the disc images, and both have to work.
REAL_FOLDER_NAMES = (
    "BLES00134-[Guitar Hero III Legends of Rock]",
    "BLES01031-[Call of Duty Black Ops]",
    "BLES01717-[Call of Duty Black Ops II]",
)


class ImageOpened(BaseException):
    """Deliberately not an Exception.

    scan_console catches Exception around the image pass so that a console
    going away during an opt-in slow read costs the identification and nothing
    else. An assertion that should have been unreachable must not be catchable
    by the code it is watching.
    """


class ExplodingIsoReader:
    """Stands in for ps3diag.isoreader. Touching anything fails the test.

    This is the instrument for the rule that makes the scan usable at all: a
    name that carries a title ID must cost nothing beyond the directory listing
    that has already happened. Asserting "it was quick" would prove nothing;
    this fails on the exact line that should never run.
    """

    ISO_SUFFIXES = (".iso",)
    DEFAULT_TOTAL_BUDGET = 1

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise ImageOpened(
                f"the image reader was used ({name}); the title ID was in "
                f"the name and no image should have been opened")
        return explode


def exploding_identifier(entries):
    raise ImageOpened("an image was opened during a scan that did not ask")


def identifier_over(images):
    """An image identifier reading real ISO bytes out of memory.

    ps3diag.isoreader does the actual work, over its own BytesReader, so the
    fallback is exercised for real rather than mocked into agreeing with
    itself. Nothing here opens a socket.
    """
    from ps3diag import isoreader

    reader = isoreader.BytesReader(images)

    def identify(entries):
        return isoreader.identify_isos(entries, reader).get("isos") or []
    return identify


def iso_of(title_id, version="01.00", title="A Game"):
    """A small but real PS3 disc image carrying that title ID."""
    return make_fixtures.build_ps3_bridge(
        sfo=make_fixtures.param_sfo((("APP_VER", version),
                                     ("CATEGORY", "DG"),
                                     ("TITLE", title),
                                     ("TITLE_ID", title_id))))


class FakeLister:
    """LIST and download_bytes out of dicts. A 550 for anything else.

    Shaped like ps3diag.transport.FtpLister, including the context manager, so
    the screen's seam can be filled with one of these.
    """

    def __init__(self, listings=None, blobs=None, fail_on=None,
                 installs_land=True):
        self.listings = {key.rstrip("/") or "/": value
                         for key, value in (listings or {}).items()}
        self.blobs = dict(blobs or {})
        self.fail_on = set(fail_on or ())
        self.asked = []
        #: A console where installs work: the title directory turns up under
        #: /dev_hdd0/game once a package has been installed. Set False for a
        #: console that takes the install and never finishes it, which is the
        #: case the queue has to stop on.
        self.installs_land = installs_land

    def list_dir(self, path):
        self.asked.append(path)
        key = path.rstrip("/") or "/"
        if key in self.fail_on:
            raise ftplib.error_temp("421 the console went away")
        if key not in self.listings:
            if self.installs_land and key.startswith(f"{GAME}/"):
                return ("drwxrwxrwx   1 root  root   0 Jan  1 00:00 .\n"
                        "drwxrwxrwx   1 root  root   0 Jan  1 00:00 USRDIR")
            raise ftplib.error_perm(f"550 {path}: no such directory")
        return self.listings[key]

    def download_bytes(self, path, max_bytes=None):
        # A path in fail_on is a console that went away rather than one
        # answering that the file is not there. The two are different
        # answers and the program says different things about them.
        if path in self.fail_on:
            raise ftplib.error_temp("421 the console went away")
        if path not in self.blobs:
            raise ftplib.error_perm(f"550 {path}: no such file")
        return self.blobs[path]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingWriter:
    """Stands in for FtpWriter. Records every write; opens no socket."""

    def __init__(self, fault=None):
        self.stored = []
        self.made = []
        self.deleted = []
        self.fault = fault

    def delete(self, path):
        """DELE, as FtpWriter has it. The console tidies up after an install."""
        self.deleted.append(path)
        return True

    def make_dir(self, path):
        self.made.append(path)
        return path

    def store(self, source, path, on_block=None):
        if self.fault is not None:
            raise self.fault
        size = os.path.getsize(source)
        with open(source, "rb") as handle:
            body = handle.read()
        if on_block:
            on_block(size, size)
        self.stored.append((path, body))
        return size

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class WriteAttempted(BaseException):
    """Deliberately not an Exception.

    deliver() turns every Exception the write client raises into an
    UploadFailed with a sentence about the console, which is right for a real
    failure and wrong for this: an assertion that should have been unreachable
    must not be catchable by the code it is watching.
    """


class ExplodingWriter:
    """Fails the test the moment anything tries to write with it.

    This is the instrument for the rule that matters most here: a package whose
    sha1 did not match must never reach an upload. Asserting "stored is empty"
    afterwards would pass just as happily if the flow had stopped for some
    other reason; this fails at the exact line that should be unreachable.
    """

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise WriteAttempted(
                f"the write client was used ({name}); nothing should have "
                f"been written to the console")
        return explode

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fetcher_for(table):
    """A manifest fetcher answering out of {title_id: bytes}."""
    def fetch(url, timeout=None):
        for title_id, body in table.items():
            if f"/{title_id}/" in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise urllib.error.URLError("no such title")
    return fetch


def exploding_fetcher(*args, **kwargs):
    raise AssertionError("the real manifest fetcher was called")


def stream_for(body, stop_after=None, raise_at=None, chunk=None):
    """A package stream over bytes, optionally breaking part way through.

    `chunk` hands the reader less than it asked for, which is what a real
    socket does and what the download's tail buffer has to cope with.
    """
    def open_stream(url, timeout=None):
        return FakeStream(body, stop_after=stop_after, raise_at=raise_at,
                          chunk=chunk)
    return open_stream


class FakeStream:
    def __init__(self, body, stop_after=None, raise_at=None, chunk=None):
        self._data = io.BytesIO(body)
        self._sent = 0
        self.stop_after = stop_after
        self.raise_at = raise_at
        self.chunk = chunk
        self.closed = False

    def read(self, size=-1):
        if self.chunk and size > 0:
            size = min(size, self.chunk)
        if self.raise_at is not None and self._sent >= self.raise_at:
            raise ConnectionResetError("the connection was reset")
        if self.stop_after is not None:
            if self._sent >= self.stop_after:
                return b""
            size = min(size, self.stop_after - self._sent)
        if self.raise_at is not None:
            size = min(size, self.raise_at - self._sent)
        block = self._data.read(size)
        self._sent += len(block)
        return block

    def close(self):
        self.closed = True


def signed_pkg(payload):
    """A blob shaped like a real PS3 package.

    The payload, then the 32-byte footer every .pkg ends with: its own sha1 in
    the first 20 bytes and twelve zero bytes after it. Sony's manifest
    publishes the digest of the payload, not of the whole file, which is the
    whole reason the check had to change.
    """
    return payload + hashlib.sha1(payload).digest() + b"\x00" * 12


def package_for(blob, **kw):
    """A manifest entry describing `blob` the way Sony's really does.

    size is the whole file including the footer; sha1sum covers everything
    except it.
    """
    return package(blob, sha1=hashlib.sha1(blob[:-32]).hexdigest(), **kw)


def package(body, url=None, version="01.19", sha1=None):
    return Package(
        version=version, size=len(body),
        sha1sum=sha1 if sha1 is not None else hashlib.sha1(body).hexdigest(),
        url=url or ("http://b0.ww.np.dl.playstation.net/tppkg/np/"
                    f"{BO2}/x-{version}.pkg"))


# --- the manifest ----------------------------------------------------------

class TheManifestUrl(unittest.TestCase):
    def test_it_is_built_from_one_constant(self):
        self.assertEqual(
            updates.manifest_url(BO2),
            "https://a0.ww.np.dl.playstation.net/tpl/np/"
            "BLES01717/BLES01717-ver.xml")

    def test_a_lowercase_id_is_accepted_and_uppercased(self):
        self.assertIn("/BLES01717/", updates.manifest_url("bles01717"))

    def test_anything_that_is_not_a_title_id_is_refused(self):
        # Title IDs come off a directory listing on somebody's console, so a
        # folder with a slash or a ".." in its name must never become a URL.
        for value in ("", None, "BLES0171", "../../evil", "BLES01717/x",
                      "BLES01717?x=1", "A B C", "BLES 1717"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                updates.manifest_url(value)


class ParsingAManifest(FixtureCase):
    def test_a_real_shaped_manifest(self):
        manifest = updates.parse_manifest(manifest_bytes("BLES01717-ver.xml"),
                                          BO2)
        self.assertFalse(manifest.empty)
        self.assertEqual(manifest.name, "Call of Duty: Black Ops II")
        self.assertEqual([item.version for item in manifest.packages],
                         ["01.05", "01.19"])
        self.assertEqual(manifest.latest.version, "01.19")
        self.assertEqual(manifest.latest.size, 419430400)
        self.assertEqual(manifest.latest.sha1sum, "2" * 40)
        self.assertTrue(manifest.latest.url.endswith("PATCH19.pkg"))

    def test_the_newest_is_by_version_not_by_document_order(self):
        # Every example seen is in ascending order. "Every example seen" is not
        # something to install somebody's game update on.
        raw = manifest_bytes("BLES01717-ver.xml").replace(b"01.19", b"00.09")
        manifest = updates.parse_manifest(raw, BO2)
        self.assertEqual(manifest.latest.version, "01.05")

    def test_an_empty_body_means_no_update_was_ever_released(self):
        manifest = updates.parse_manifest(manifest_bytes("empty-ver.xml"), BO2)
        self.assertTrue(manifest.empty)
        self.assertEqual(manifest.packages, [])
        self.assertIsNone(manifest.latest)

    def test_whitespace_only_is_the_same_answer(self):
        self.assertTrue(updates.parse_manifest(b"\n  \r\n", BO2).empty)

    def test_a_well_formed_document_with_nothing_in_it_is_empty_too(self):
        manifest = updates.parse_manifest(b"<titlepatch titleid='X'/>", BO2)
        self.assertTrue(manifest.empty)

    def test_malformed_xml_is_a_failure_that_says_so(self):
        with self.assertRaises(ManifestUnreadable) as caught:
            updates.parse_manifest(manifest_bytes("malformed-ver.xml"), BO2)
        self.assertIn("BLES01717", str(caught.exception))

    def test_a_captive_portal_page_is_not_a_manifest(self):
        with self.assertRaises(ManifestUnreadable):
            updates.parse_manifest(manifest_bytes("captive-portal.html"), BO2)

    def test_a_package_with_no_checksum_is_not_usable(self):
        manifest = updates.parse_manifest(manifest_bytes("nosha1-ver.xml"),
                                          "BLES00001")
        self.assertEqual(len(manifest.packages), 1)
        self.assertFalse(manifest.packages[0].usable)
        self.assertIsNone(manifest.latest)

    def test_a_nonsense_checksum_is_discarded_rather_than_kept(self):
        raw = manifest_bytes("BLES01717-ver.xml").replace(b"2" * 40, b"nope")
        manifest = updates.parse_manifest(raw, BO2)
        self.assertEqual(manifest.packages[1].sha1sum, "")

    def test_a_nonsense_size_is_zero_rather_than_a_crash(self):
        raw = manifest_bytes("BLES01717-ver.xml").replace(
            b'size="419430400"', b'size="a lot"')
        manifest = updates.parse_manifest(raw, BO2)
        self.assertEqual(manifest.packages[1].size, 0)
        self.assertFalse(manifest.packages[1].usable)


class WhereAPackageMayComeFrom(unittest.TestCase):
    def test_sonys_own_hosts_are_accepted(self):
        for url in ("http://b0.ww.np.dl.playstation.net/x.pkg",
                    "https://a0.ww.np.dl.playstation.net/x.pkg",
                    "http://zeus.dl.playstation.net/x.pkg"):
            with self.subTest(url=url):
                self.assertTrue(updates.is_sony_url(url))

    def test_anywhere_else_is_refused(self):
        for url in ("http://evil.example/x.pkg",
                    "http://playstation.net.evil.example/x.pkg",
                    "ftp://b0.ww.np.dl.playstation.net/x.pkg",
                    "file:///etc/passwd", "", None, "not a url"):
            with self.subTest(url=url):
                self.assertFalse(updates.is_sony_url(url))


# --- fetching, and the certificate ------------------------------------------

class ThePinnedRoot(unittest.TestCase):
    """The certificate is the whole of the trust here, so it is checked.

    Sony's endpoint presents a certificate from its own private CA, which no
    public trust store carries and never will. Pinning that root is what makes
    the manifest trustworthy, and the sha1 in the manifest is what makes the
    package download trustworthy. Substitute the root and both fall over
    quietly, which is why this is asserted rather than assumed.
    """

    def test_the_expected_fingerprint_is_the_scei_root(self):
        self.assertEqual(
            updates.CERT_SHA256,
            "51d5a7833f67f1d1c6212f75997e4b83"
            "dada0f7910ba4b5a3fa4e9b7b9bc827c")

    def test_the_bundled_certificate_is_that_one(self):
        path = updates.certificate_path()
        if not path:
            self.skipTest("the root is not bundled in this checkout")
        self.assertEqual(updates.certificate_fingerprint(path),
                         updates.CERT_SHA256)
        self.assertTrue(updates.certificate_is_expected(path))

    def test_it_is_the_self_signed_root_and_not_a_leaf(self):
        path = updates.certificate_path()
        if not path:
            self.skipTest("the root is not bundled in this checkout")
        details = ssl._ssl._test_decode_cert(path)
        self.assertEqual(details["subject"], details["issuer"])
        flat = dict(pair for rdn in details["subject"] for pair in rdn)
        self.assertEqual(flat["commonName"], "SCEI DNAS Root 05")

    def test_verification_is_on_and_explicitly_so(self):
        # PROTOCOL_TLS_CLIENT does not enable these the way
        # create_default_context does. Getting it wrong disables verification
        # silently, which is the opposite of what the pinning is for.
        if not updates.certificate_path():
            self.skipTest("the root is not bundled in this checkout")
        context = updates.ssl_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_only_the_pinned_root_is_loaded(self):
        if not updates.certificate_path():
            self.skipTest("the root is not bundled in this checkout")
        roots = updates.ssl_context().get_ca_certs()
        self.assertEqual(len(roots), 1)
        flat = dict(pair for rdn in roots[0]["subject"] for pair in rdn)
        self.assertEqual(flat["commonName"], "SCEI DNAS Root 05")

    def test_the_low_security_level_does_not_accept_another_root(self):
        """The property anybody would doubt, asserted rather than argued.

        SECLEVEL=0 is there for one reason: SCEI DNAS Root 05 is from 2004 and
        signed with SHA-1, which OpenSSL 3 refuses at its default level
        whatever root is pinned. It permits that digest. It does not make the
        context accept anybody else, and this proves it by handshaking against
        a server holding a certificate from a different CA entirely.
        """
        if not updates.certificate_path():
            self.skipTest("the root is not bundled in this checkout")
        import http.server
        import socket
        import threading
        import tempfile
        import subprocess
        folder = tempfile.mkdtemp()
        key = os.path.join(folder, "k.pem")
        crt = os.path.join(folder, "c.pem")
        made = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
             "-out", crt, "-days", "1", "-nodes", "-subj",
             "/CN=a0.ww.np.dl.playstation.net"],
            capture_output=True)
        if made.returncode != 0:
            self.skipTest("openssl not available to make a stand-in server")

        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(crt, key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            try:
                raw, _ = listener.accept()
                with server_context.wrap_socket(raw, server_side=True):
                    pass
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(listener.close)

        # A real certificate, a matching hostname, and a CA this context does
        # not pin. It must still be refused.
        with self.assertRaises(ssl.SSLError):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                with updates.ssl_context().wrap_socket(
                        raw, server_hostname="a0.ww.np.dl.playstation.net"):
                    pass

    def test_a_different_certificate_is_refused(self):
        import tempfile
        other = ("-----BEGIN CERTIFICATE-----\n"
                 + "MIIBkTCB+w==\n" + "-----END CERTIFICATE-----\n")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "wrong.pem")
            with open(path, "w", encoding="ascii") as handle:
                handle.write(other)
            self.assertFalse(updates.certificate_is_expected(path))

    def test_a_substituted_root_stops_the_check_rather_than_being_used(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, updates.CERT_NAME)
            with open(path, "w", encoding="ascii") as handle:
                handle.write("-----BEGIN CERTIFICATE-----\nAAA=\n"
                             "-----END CERTIFICATE-----\n")
            original = updates.certificate_path
            updates.certificate_path = lambda: path
            self.addCleanup(setattr, updates, "certificate_path", original)
            with self.assertRaises(updates.CertificateMissing):
                updates.ssl_context()


class FetchingTheManifest(unittest.TestCase):
    def test_the_injected_fetcher_is_used_and_the_real_one_is_not(self):
        with mock.patch.object(updates, "urllib_fetcher", exploding_fetcher):
            manifest = updates.fetch_manifest(
                BO2, fetcher=fetcher_for(
                    {BO2: manifest_bytes("BLES01717-ver.xml")}))
        self.assertEqual(manifest.latest.version, "01.19")

    def test_a_certificate_failure_says_what_happened_and_what_to_do(self):
        failure = urllib.error.URLError(
            ssl.SSLCertVerificationError(
                1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify "
                   "failed: unable to get local issuer certificate"))

        def fetch(url, timeout=None):
            raise failure

        with self.assertRaises(TlsNotTrusted) as caught:
            updates.fetch_manifest(BO2, fetcher=fetch)
        message = str(caught.exception)
        self.assertIn("a0.ww.np.dl.playstation.net", message)
        # The old message blamed the user's antivirus, network and router for
        # a refusal none of them caused: the endpoint presents Sony's own
        # private console CA, which no public trust store carries, so this
        # fails identically on every machine. Blaming wording must not come
        # back.
        for blamed in ("antivirus", "router", "company or school",
                       "interception"):
            self.assertNotIn(blamed, message)
        self.assertIn("could not be confirmed as Sony's", message)
        self.assertIn("Nothing has been downloaded", message)
        # It must not offer a way round itself. Checked as offers rather than
        # as words: the message legitimately says it "will not skip that
        # check", and banning the word would forbid the refusal along with the
        # offer.
        lowered = message.lower()
        for offer in ("you can skip", "to skip", "skip this", "ignore this",
                      "turn off certificate", "disable certificate",
                      "--insecure", "at your own risk"):
            self.assertNotIn(offer, lowered)
        self.assertIn("will not skip", lowered)

    def test_a_certificate_failure_is_not_the_same_as_no_network(self):
        def fetch(url, timeout=None):
            raise urllib.error.URLError("Name or service not known")

        with self.assertRaises(ManifestUnreadable):
            updates.fetch_manifest(BO2, fetcher=fetch)

    def test_a_tls_failure_stops_the_whole_check_rather_than_every_row(self):
        # Every later request would fail the same way. Forty identical rows
        # saying the same thing is not forty answers.
        def fetch(url, timeout=None):
            raise ssl.SSLCertVerificationError(
                1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

        titles = [updates.InstalledTitle(title_id=BO2),
                  updates.InstalledTitle(title_id=GT5)]
        with self.assertRaises(TlsNotTrusted):
            updates.check_titles(titles, fetcher=fetch)

    def test_a_failed_manifest_does_not_take_the_other_games_with_it(self):
        table = {BO2: urllib.error.URLError("timed out"),
                 GT5: manifest_bytes("BCES00569-ver.xml")}
        rows = updates.check_titles(
            [updates.InstalledTitle(title_id=BO2, version="01.05"),
             updates.InstalledTitle(title_id=GT5, version="01.00")],
            fetcher=fetcher_for(table))
        self.assertEqual([row.title_id for row in rows], [BO2, GT5])
        self.assertEqual(rows[0].blocked, updates.MANIFEST_FAILED)
        self.assertEqual(rows[0].state_text, "Could not be checked")
        self.assertEqual(rows[1].name, "Gran Turismo 5")
        self.assertTrue(rows[1].out_of_date)


class TheModuleNeverWeakensTls(unittest.TestCase):
    """Read as a grep over the source, because this is a thing to never add.

    A "just this once" unverified context would make every other check in this
    file meaningless: the sha1 that proves the package is Sony's comes out of
    the manifest, and an unverified manifest is whatever was in the way.
    """

    FORBIDDEN = ("_create_unverified_context", "CERT_NONE",
                 "check_hostname = False", "check_hostname=False",
                 "verify_mode = ssl.CERT_NONE")

    def test_no_file_this_feature_owns_turns_verification_off(self):
        offences = []
        for name in ("ps3tools/updates.py", "ps3tools/consoleactions.py",
                     "ps3tools/screens/gameupdates.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                body = handle.read()
            for token in self.FORBIDDEN:
                # The test's own name for the token would match, so only real
                # code is looked at: this list lives in the test file.
                if token in body:
                    offences.append(f"{name} contains {token!r}")
        self.assertEqual(offences, [], "\n".join(offences))


# --- what is installed ------------------------------------------------------

class ReadingTheInstalledVersion(unittest.TestCase):
    def test_app_ver_comes_out_of_param_sfo(self):
        lister = FakeLister(
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.19")})
        version, detail = updates.read_installed_version(
            lister.download_bytes, BO2)
        self.assertEqual(version, "1.19")
        self.assertEqual(detail, "")

    def test_a_title_with_no_param_sfo_says_so_rather_than_failing(self):
        lister = FakeLister(blobs={})
        version, detail = updates.read_installed_version(
            lister.download_bytes, "BLES99999")
        self.assertIsNone(version)
        self.assertIn("could not be read", detail)

    def test_a_param_sfo_with_no_app_ver_says_which_problem_it_was(self):
        blob = make_fixtures.param_sfo((("TITLE", "X"), ("TITLE_ID", BO2)))
        version, detail = updates.read_installed_version(
            lambda path: blob, BO2)
        self.assertIsNone(version)
        self.assertIn("APP_VER", detail)

    def test_a_truncated_param_sfo_is_not_a_crash(self):
        blob = param_sfo("01.19")[:12]
        version, detail = updates.read_installed_version(lambda path: blob, BO2)
        self.assertIsNone(version)
        self.assertTrue(detail)


class ScanningTheConsole(unittest.TestCase):
    def listings(self, *names):
        table = {f"{GAME}": folders(*names)}
        for name in names:
            table[f"{GAME}/{name}/USRDIR"] = files("EBOOT.BIN")
        return table

    def test_every_installed_title_is_found_not_just_the_two_patchable_ones(self):
        lister = FakeLister(
            listings=self.listings(BO2, GT5, "NPEB02143"),
            blobs={f"{GAME}/{GT5}/PARAM.SFO": param_sfo("01.00", GT5)})
        found, notes, _unnamed = updates.scan_console(lister)
        self.assertEqual([item.title_id for item in found],
                         [GT5, BO2, "NPEB02143"])
        self.assertEqual(
            {item.title_id: item.version for item in found}[GT5], "1.00")

    def test_folders_that_are_not_title_ids_are_ignored(self):
        lister = FakeLister(
            listings={f"{GAME}": folders(BO2, "webftp_server", "IRISMAN")})
        found, _notes, _unnamed = updates.scan_console(lister)
        self.assertEqual([item.title_id for item in found], [BO2])

    def test_a_game_with_no_title_update_installed_is_still_offered(self):
        # An ISO that has been run once and nothing more: the folder is there,
        # there is no USRDIR, and there is an update waiting for it. This is
        # the case the whole feature exists to catch.
        lister = FakeLister(
            listings={f"{GAME}": folders(GT5)},
            blobs={f"{GAME}/{GT5}/PARAM.SFO": param_sfo("01.00", GT5)})
        found, _notes, _unnamed = updates.scan_console(lister)
        self.assertEqual(len(found), 1)
        row = updates.row_for(
            found[0],
            updates.parse_manifest(manifest_bytes("BCES00569-ver.xml"), GT5))
        self.assertTrue(row.out_of_date)
        self.assertTrue(row.updatable)

    def test_a_console_that_stops_answering_keeps_what_was_found(self):
        lister = FakeLister(listings={}, fail_on={f"{GAME}"})
        found, notes, _unnamed = updates.scan_console(lister)
        self.assertEqual(found, [])
        self.assertTrue(any("stopped answering" in note for note in notes))

    def test_more_titles_than_detects_cap_are_capped_and_said_out_loud(self):
        names = [f"BLES{index:05d}" for index in range(detect.MAX_TITLE_DIRS + 5)]
        lister = FakeLister(listings={f"{GAME}": folders(*names)})
        found, notes, _unnamed = updates.scan_console(lister)
        self.assertEqual(len(found), detect.MAX_TITLE_DIRS)
        self.assertTrue(any("were checked" in note for note in notes))


# --- the rest of the console ------------------------------------------------

class TheGameInventory(unittest.TestCase):
    """The gap this closes: a game that is on the console but never patched.

    /dev_hdd0/game holds title updates, so a game that has one appears there
    and a game that does not appear nowhere at all -- which is exactly the game
    most likely to need one. The folder games and the disc images are the other
    two places a console keeps games, and all three are unioned by title ID.
    """

    def scan(self, listings, blobs=None, **kwargs):
        lister = FakeLister(listings=listings, blobs=blobs)
        return updates.scan_console(lister, **kwargs)

    def test_the_folder_name_form_off_a_real_console_is_read(self):
        # ID first, name in brackets after it. The opposite way round from the
        # disc images, and the common shape for an extracted game.
        for name in REAL_FOLDER_NAMES:
            self.assertIsNotNone(regioncodes.find_title_id(name), name)
        found, _notes, _unnamed = self.scan({GAMES: folders(*REAL_FOLDER_NAMES)})
        self.assertEqual([item.title_id for item in found],
                         ["BLES00134", "BLES01031", BO2])

    def test_a_game_with_no_update_installed_says_none_and_is_offered(self):
        found, _notes, _unnamed = self.scan(
            {PS3ISO: files(f"Gran Turismo 5 [{GT5}].iso")})
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].no_update_installed)
        self.assertIsNone(found[0].version)
        row = updates.row_for(
            found[0],
            updates.parse_manifest(manifest_bytes("BCES00569-ver.xml"), GT5))
        self.assertEqual(row.installed_text, "none")
        self.assertEqual(row.state_text, "Update available")
        self.assertTrue(row.updatable)
        self.assertIn("no title update installed", row.detail)

    def test_a_game_under_dev_hdd0_game_is_offered(self):
        found, _notes, _unnamed = self.scan(
            {GAME: folders(BO2), f"{GAME}/{BO2}": ""},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.05", BO2)})
        row = updates.row_for(
            found[0],
            updates.parse_manifest(manifest_bytes("BLES01717-ver.xml"), BO2))
        self.assertTrue(row.updatable)

    def test_a_game_in_both_places_is_listed_once(self):
        # The installed side wins: it is the only one that knows which version
        # is on the console.
        found, _notes, _unnamed = self.scan(
            {f"{GAME}": folders(BO2),
             GAMES: folders("BLES01717-[Call of Duty Black Ops II]"),
             PS3ISO: files(f"Black Ops II [{BO2}].iso")},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.05", BO2)})
        self.assertEqual([item.title_id for item in found], [BO2])
        self.assertEqual(found[0].version, "1.05")
        self.assertFalse(found[0].no_update_installed)

    def test_all_three_sources_are_unioned(self):
        found, _notes, _unnamed = self.scan(
            {f"{GAME}": folders(BO2),
             GAMES: folders("BLES01031-[Call of Duty Black Ops]"),
             PS3ISO: files(f"Gran Turismo 5 [{GT5}].iso")},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.05", BO2)})
        self.assertEqual(sorted(item.title_id for item in found),
                         [GT5, "BLES01031", BO2])

    def test_a_ps2_image_is_never_asked_about(self):
        # Sony's title update host has nothing for a PS2 disc, and asking is a
        # request spent to be told so.
        found, _notes, _unnamed = self.scan(
            {PS2ISO: files("Some PS2 Game [SLES50916].iso")})
        self.assertEqual(found, [])

    def test_a_usb_drive_is_looked_at_when_it_is_named(self):
        found, _notes, _unnamed = self.scan(
            {"/dev_usb000/PS3ISO": files(f"Gran Turismo 5 [{GT5}].iso")},
            devices=["dev_hdd0", "dev_usb000"])
        self.assertEqual([item.title_id for item in found], [GT5])
        self.assertTrue(found[0].path.startswith("/dev_usb000/"))

    def test_a_package_waiting_to_be_installed_is_not_a_game(self):
        # /dev_hdd0/PKG holds installers, very often the title update itself
        # named after the game it patches. A row for one would offer to fetch
        # what is already sitting on the drive.
        found, _notes, _unnamed = self.scan(
            {"/dev_hdd0/PKG": files(f"UP0002-{BO2}_00-0000111122223333.pkg")})
        self.assertEqual(found, [])

    def test_game_folders_that_are_not_there_are_passed_over_quietly(self):
        # Most of the eight do not exist on a normal console, and a note for
        # each one would bury the answer under seven lines saying nothing.
        _found, notes, _unnamed = self.scan({PS3ISO: ""})
        self.assertEqual([note for note in notes if "PS2ISO" in note], [])


class NoImageIsOpenedForAName(unittest.TestCase):
    """The performance rule, proved rather than assumed.

    Opening a disc image means a connection, a seek and a handful of reads over
    FTP; on a shelf of two dozen games the diagnostic measured that in minutes,
    which is why it made per-image identification opt-in. A name that already
    carries the ID has to cost nothing beyond the listing.
    """

    def setUp(self):
        patcher = mock.patch.object(updates, "isoreader", ExplodingIsoReader())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_shelf_of_named_games_never_opens_one(self):
        names = [f"Game {index} [BLES{index:05d}].iso" for index in range(40)]
        lister = FakeLister(listings={
            GAMES: folders(*REAL_FOLDER_NAMES), PS3ISO: files(*names)})
        found, _notes, unnamed = updates.scan_console(
            lister, image_identifier=exploding_identifier)
        self.assertEqual(len(found), 43)
        self.assertEqual(unnamed, [])

    def test_nothing_opens_an_image_unless_it_was_asked_for(self):
        lister = FakeLister(listings={PS3ISO: files("Mystery Disc.iso")})
        found, notes, unnamed = updates.scan_console(lister)
        self.assertEqual(found, [])
        self.assertEqual([entry["name"] for entry in unnamed],
                         ["Mystery Disc.iso"])
        said = " ".join(notes)
        self.assertIn("Mystery Disc.iso", said)
        # The note names them. What opening them costs is said once, next to
        # the button that would do it, not here as well.
        self.assertIn("does not have the game's ID", said)
        self.assertNotIn("several minutes", said)


class LookingInsideAnImage(unittest.TestCase):
    """The fallback, over real ISO bytes and no socket."""

    def test_a_name_with_no_id_is_read_out_of_the_image(self):
        path = f"{PS3ISO}/Mystery Disc.iso"
        image = iso_of(GT5, title="Gran Turismo 5")
        lister = FakeLister(listings={PS3ISO: files("Mystery Disc.iso")})
        found, _notes, unnamed = updates.scan_console(
            lister, image_identifier=identifier_over({path: image}))
        self.assertEqual([item.title_id for item in found], [GT5])
        self.assertTrue(found[0].no_update_installed)
        self.assertTrue(found[0].from_image)
        self.assertEqual(unnamed, [])

    def test_only_the_ones_that_need_it_are_opened(self):
        path = f"{PS3ISO}/Mystery Disc.iso"
        asked = []

        def identify(entries):
            asked.extend(entry["name"] for entry in entries)
            return identifier_over({path: iso_of(GT5)})(entries)

        lister = FakeLister(listings={
            PS3ISO: files("Mystery Disc.iso", f"Black Ops II [{BO2}].iso")})
        found, _notes, _unnamed = updates.scan_console(
            lister, image_identifier=identify)
        self.assertEqual(asked, ["Mystery Disc.iso"])
        self.assertEqual(sorted(item.title_id for item in found), [GT5, BO2])

    def test_a_folder_with_no_id_in_its_name_is_not_offered_for_opening(self):
        # There is no image to open, so it is left alone rather than guessed
        # at, and it is not counted in what the slow pass would cost.
        lister = FakeLister(listings={GAMES: folders("Some Extracted Game")})
        found, _notes, unnamed = updates.scan_console(lister)
        self.assertEqual(found, [])
        self.assertEqual(unnamed, [])

    def test_a_console_that_stops_answering_costs_only_the_identification(self):
        def identify(entries):
            raise ftplib.error_temp("421 the console went away")

        lister = FakeLister(listings={
            PS3ISO: files("Mystery Disc.iso", f"Black Ops II [{BO2}].iso")})
        found, notes, _unnamed = updates.scan_console(
            lister, image_identifier=identify)
        self.assertEqual([item.title_id for item in found], [BO2])
        self.assertTrue(any("could not be read" in note for note in notes))


class Homebrew(unittest.TestCase):
    """multiMAN is not a game and must not be offered a Sony title update.

    Homebrew borrows the shape of a real title ID, so the folder it installs
    into looks exactly like a game's. The diagnostic already ignores folders
    that are not title-shaped; these are, which is why this is a separate
    problem.
    """

    def test_multiman_is_left_out_of_the_list(self):
        lister = FakeLister(
            listings={f"{GAME}": folders(BO2, "BLES80608")},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.05", BO2)})
        found, notes, _unnamed = updates.scan_console(lister)
        self.assertEqual([item.title_id for item in found], [BO2])
        self.assertTrue(any("multiMAN" in note for note in notes))

    def test_it_is_said_out_loud_rather_than_silently_dropped(self):
        lister = FakeLister(listings={PS3ISO: files("BLES80608.iso")})
        _found, notes, _unnamed = updates.scan_console(lister)
        said = " ".join(notes)
        self.assertIn("multiMAN", said)
        self.assertIn("homebrew rather than a game Sony published", said)

    def test_the_list_is_named_ids_and_not_a_range(self):
        # A rule that is nearly right hides somebody's real game with no way
        # for them to find out why. Every entry here is one thing that can be
        # named, and its neighbours in the same range are not swallowed.
        self.assertTrue(all(updates.TITLE_ID.match(key)
                            for key in updates.HOMEBREW_TITLES))
        self.assertTrue(all(value for value in
                            updates.HOMEBREW_TITLES.values()))
        lister = FakeLister(listings={PS3ISO: files("A Game [BLES80607].iso")})
        found, _notes, _unnamed = updates.scan_console(lister)
        self.assertEqual([item.title_id for item in found], ["BLES80607"])


class TheThreeInstalledStates(FixtureCase):
    """none, a version, and could-not-be-read are three different answers.

    Collapsing the first two into each other would tell somebody their game is
    fine when it has never been patched; collapsing the first and the third
    would tell them something is broken when nothing is.
    """

    def row(self, installed):
        return updates.row_for(
            installed,
            updates.parse_manifest(manifest_bytes("BLES01717-ver.xml"), BO2))

    def test_no_update_installed(self):
        row = self.row(updates.InstalledTitle(title_id=BO2,
                                              no_update_installed=True))
        self.assertEqual(row.installed_text, "none")
        self.assertEqual(row.state_text, "Update available")
        self.assertTrue(row.nothing_installed)
        self.assertTrue(row.updatable)
        self.assertFalse(row.out_of_date)

    def test_a_version_that_was_read(self):
        row = self.row(updates.InstalledTitle(title_id=BO2, version="01.05"))
        self.assertEqual(row.installed_text, "01.05")
        self.assertEqual(row.state_text, "Update available")
        self.assertFalse(row.nothing_installed)

    def test_a_version_that_could_not_be_read_is_not_none(self):
        row = self.row(updates.InstalledTitle(
            title_id=BO2, version=None,
            detail="PARAM.SFO carries no APP_VER"))
        self.assertEqual(row.installed_text, "not known")
        self.assertEqual(row.state_text, "Cannot tell")
        self.assertFalse(row.nothing_installed)

    def test_a_game_with_nothing_installed_and_no_update_ever_published(self):
        row = updates.row_for(
            updates.InstalledTitle(title_id="BLES00003",
                                   no_update_installed=True),
            updates.parse_manifest(manifest_bytes("empty-ver.xml"),
                                   "BLES00003"))
        self.assertEqual(row.installed_text, "none")
        self.assertEqual(row.state_text, "No update exists")
        self.assertFalse(row.updatable)
        self.assertIn("nothing is missing", row.detail)
        self.assertNotIn("error", row.detail.lower())


class ATitleSonyHasNoManifestFor(unittest.TestCase):
    """A disc image of something Sony's host has never heard of.

    Common enough on a shelf of images, and nothing has gone wrong. It gets one
    row saying it could not be checked and takes nothing else with it.
    """

    def fetcher(self, known):
        def fetch(url, timeout=None):
            for title_id, body in known.items():
                if f"/{title_id}/" in url:
                    return body
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return fetch

    def test_it_is_one_quiet_row_and_the_others_are_unaffected(self):
        rows = updates.check_titles(
            [updates.InstalledTitle(title_id="BLES09999",
                                    no_update_installed=True),
             updates.InstalledTitle(title_id=BO2, version="01.05")],
            fetcher=self.fetcher({BO2: manifest_bytes("BLES01717-ver.xml")}))
        by_id = {row.title_id: row for row in rows}
        # A 404 is Sony saying it has no such title, which it answered fully
        # and correctly. Only a check that did not complete may say it could
        # not be checked.
        self.assertEqual(by_id["BLES09999"].blocked, updates.NOT_LISTED)
        self.assertEqual(by_id["BLES09999"].state_text, "Not in Sony's list")
        self.assertFalse(by_id["BLES09999"].updatable)
        self.assertEqual(by_id["BLES09999"].installed_text, "none")
        self.assertTrue(by_id[BO2].out_of_date)


# --- rows on the screen -----------------------------------------------------

class TheRows(FixtureCase):
    def row(self, installed_version, fixture_name=None, title_id=BO2):
        installed = updates.InstalledTitle(title_id=title_id,
                                           version=installed_version)
        raw = manifest_bytes(fixture_name or "BLES01717-ver.xml")
        return updates.row_for(installed,
                               updates.parse_manifest(raw, title_id))

    def test_an_out_of_date_game(self):
        row = self.row("01.05")
        self.assertEqual(row.name, "Call of Duty: Black Ops II")
        self.assertEqual(row.installed, "01.05")
        self.assertEqual(row.latest, "1.19")
        self.assertEqual(row.size, 419430400)
        self.assertTrue(row.out_of_date)
        self.assertTrue(row.updatable)
        self.assertEqual(row.state_text, "Update available")

    def test_an_up_to_date_game_cannot_be_ticked(self):
        row = self.row("01.19")
        self.assertFalse(row.out_of_date)
        self.assertFalse(row.updatable)
        self.assertEqual(row.blocked, updates.UP_TO_DATE)

    def test_a_newer_installed_version_is_not_called_out_of_date(self):
        row = self.row("01.20")
        self.assertFalse(row.out_of_date)

    def test_a_game_whose_version_could_not_be_read_is_not_called_anything(self):
        row = self.row(None)
        self.assertFalse(row.out_of_date)
        self.assertEqual(row.state_text, "Cannot tell")
        # Still offered, because installing an update you already have is
        # harmless and refusing outright would help nobody.
        self.assertTrue(row.updatable)

    def test_a_game_with_no_update_ever_published(self):
        row = self.row("01.00", "empty-ver.xml", title_id="BLES00003")
        self.assertEqual(row.blocked, updates.NO_UPDATE_EVER)
        self.assertEqual(row.state_text, "No update exists")
        self.assertFalse(row.updatable)
        self.assertIn("never released", row.detail)
        # And it must not read as something having gone wrong.
        self.assertNotIn("error", row.detail.lower())
        self.assertNotIn("failed", row.detail.lower())

    def test_a_manifest_with_no_checksum_is_refused_with_a_reason(self):
        row = self.row("01.00", "nosha1-ver.xml", title_id="BLES00001")
        self.assertEqual(row.blocked, updates.NO_SHA1)
        self.assertFalse(row.updatable)
        self.assertIn("checksum", row.detail)

    def test_a_download_address_that_is_not_sonys_is_refused(self):
        row = self.row("01.00", "offsite-ver.xml", title_id="BLES00002")
        self.assertEqual(row.blocked, updates.UNKNOWN_SOURCE)
        self.assertFalse(row.updatable)
        self.assertIsNone(row.package)

    def test_the_name_comes_from_the_manifest_and_not_from_a_local_table(self):
        row = self.row("01.00", "BCES00569-ver.xml", title_id=GT5)
        self.assertEqual(row.name, "Gran Turismo 5")


# --- the packages folder ----------------------------------------------------

class ThePackagesFolder(unittest.TestCase):
    def test_an_empty_folder_is_empty(self):
        lister = FakeLister(listings={"/dev_hdd0/packages": ""})
        folder = updates.inspect_packages_folder(lister)
        self.assertTrue(folder.empty)
        self.assertEqual(folder.names, [])

    def test_something_already_in_it_is_reported(self):
        # The install call installs the folder, not a file. Whatever is in
        # there gets installed too, and this program has no idea what it is.
        lister = FakeLister(
            listings={"/dev_hdd0/packages":
                      files("mystery.pkg", "someones-homebrew.pkg")})
        folder = updates.inspect_packages_folder(lister)
        self.assertFalse(folder.empty)
        self.assertEqual(folder.names,
                         ["mystery.pkg", "someones-homebrew.pkg"])

    def test_a_folder_that_cannot_be_listed_is_unknown_not_empty(self):
        lister = FakeLister(listings={})
        folder = updates.inspect_packages_folder(lister)
        self.assertTrue(folder.unknown)
        self.assertFalse(folder.empty)
        self.assertIn("could not be read", folder.reason)


# --- free space -------------------------------------------------------------

class FreeSpace(unittest.TestCase):
    DEVICES = [{"device": "dev_hdd0", "free_bytes": 800 * 1024 * 1024},
               {"device": "dev_usb000", "free_bytes": 32 * 1024 * 1024 * 1024},
               {"device": "dev_bdvd"}]

    def test_it_reads_the_storage_collectors_own_numbers(self):
        self.assertEqual(updates.free_bytes_for(self.DEVICES, "dev_hdd0"),
                         800 * 1024 * 1024)
        self.assertEqual(updates.free_bytes_for(self.DEVICES, "dev_usb000"),
                         32 * 1024 * 1024 * 1024)

    def test_a_device_that_did_not_say_is_none_rather_than_zero(self):
        self.assertIsNone(updates.free_bytes_for(self.DEVICES, "dev_bdvd"))
        self.assertIsNone(updates.free_bytes_for(self.DEVICES, "dev_sd"))
        self.assertIsNone(updates.free_bytes_for([], "dev_hdd0"))

    def test_enough_room_passes(self):
        self.assertIsNone(updates.check_space(2 * 1024 ** 3, 100 * 1024 ** 2))

    def test_not_enough_room_is_refused_with_both_numbers(self):
        with self.assertRaises(NotEnoughSpace) as caught:
            updates.check_space(200 * 1024 ** 2, 400 * 1024 ** 2)
        message = str(caught.exception)
        self.assertIn("400.0 MB", message)
        self.assertIn("200.0 MB", message)
        self.assertIn("Nothing has been downloaded", message)

    def test_a_console_that_did_not_report_is_not_refused_on_that_basis(self):
        # None is not zero. Refusing every console whose webMAN reports free
        # space differently would refuse consoles that have plenty.
        self.assertIsNone(updates.check_space(None, 400 * 1024 ** 2))


# --- downloading and the check that matters ---------------------------------

class Downloading(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ps3-updates-test-")
        self.addCleanup(lambda: _rmtree(self.folder))
        self.body = signed_pkg(os.urandom(4096) + b"PKG" * 300)

    def path(self, name="x.pkg"):
        return os.path.join(self.folder, name)

    def test_a_whole_download_hashes_everything_but_the_footer(self):
        item = package_for(self.body)
        seen = []
        arrived = updates.download_package(
            item, self.path(), stream=stream_for(self.body),
            on_block=lambda done, total: seen.append((done, total)))
        self.assertEqual(arrived.body_sha1,
                         hashlib.sha1(self.body[:-32]).hexdigest())
        self.assertNotEqual(arrived.body_sha1,
                            hashlib.sha1(self.body).hexdigest())
        self.assertEqual(arrived.size, len(self.body))
        self.assertEqual(os.path.getsize(self.path()), len(self.body))
        self.assertEqual(seen[-1], (len(self.body), len(self.body)))

    def test_the_footer_the_package_carries_is_read_back(self):
        item = package_for(self.body)
        arrived = updates.download_package(item, self.path(),
                                           stream=stream_for(self.body))
        self.assertEqual(arrived.footer_sha1, arrived.body_sha1)
        self.assertEqual(arrived.footer_sha1, item.sha1sum)

    def test_the_whole_file_is_still_written_out(self):
        # The footer is held back from the hash, not from the disk: the
        # console is given the package Sony published, footer and all.
        item = package_for(self.body)
        updates.download_package(item, self.path(),
                                 stream=stream_for(self.body))
        with open(self.path(), "rb") as handle:
            self.assertEqual(handle.read(), self.body)

    def test_a_block_size_that_lands_on_the_footer_boundary(self):
        # The tail buffer is the only part of this that could go wrong
        # quietly, so it is exercised at sizes either side of one block.
        for length in (0, 1, 31, 32, 33,
                       updates.BLOCK - 1, updates.BLOCK,
                       updates.BLOCK + 1, updates.BLOCK * 2 + 32):
            with self.subTest(length=length):
                blob = signed_pkg(b"z" * length)
                item = package_for(blob)
                arrived = updates.download_package(
                    item, self.path(), stream=stream_for(blob, chunk=97))
                self.assertEqual(arrived.body_sha1, item.sha1sum)
                self.assertEqual(arrived.footer_sha1, item.sha1sum)

    def test_a_stream_that_stops_early_is_a_failure_not_a_smaller_file(self):
        item = package_for(self.body)
        with self.assertRaises(DownloadFailed) as caught:
            updates.download_package(item, self.path(),
                                     stream=stream_for(self.body,
                                                       stop_after=1024))
        self.assertIn("stopped after", str(caught.exception))

    def test_a_connection_reset_mid_stream_says_where_it_got_to(self):
        item = package_for(self.body)
        with self.assertRaises(DownloadFailed) as caught:
            updates.download_package(item, self.path(),
                                     stream=stream_for(self.body,
                                                       raise_at=2048))
        self.assertIn("internet connection", str(caught.exception))

    def test_a_stream_that_will_not_open_is_a_failure_with_advice(self):
        def refuse(url, timeout=None):
            raise urllib.error.URLError("connection refused")

        with self.assertRaises(DownloadFailed):
            updates.download_package(package_for(self.body), self.path(),
                                     stream=refuse)

    def test_a_url_that_is_not_sonys_is_never_fetched(self):
        item = package_for(self.body, url="http://evil.example/x.pkg")

        def explode(url, timeout=None):
            raise AssertionError(f"it fetched {url}")

        with self.assertRaises(DownloadFailed):
            updates.download_package(item, self.path(), stream=explode)


class TheChecksum(unittest.TestCase):
    """What Sony's sha1sum actually covers.

    Read off a real download rather than reasoned about: BLES01031, 65,334,960
    bytes, manifest sha1sum 036c533b51df35d803df12f21a4abeb877fe43f5. The sha1
    of the whole file is 3d3a117d57323dbb4fcc84cef6636a39dcf88e9d and does not
    match anything. The sha1 of the file without its last 32 bytes is the
    manifest value exactly, and those 32 bytes are that same digest followed
    by twelve zero bytes. Every download failed verification until this was
    understood.
    """

    REAL_SIZE = 65334960
    REAL_SHA1 = "036c533b51df35d803df12f21a4abeb877fe43f5"

    def arrived(self, blob):
        return updates.Downloaded(
            body_sha1=hashlib.sha1(blob[:-32]).hexdigest(),
            footer_sha1=blob[-32:][:20].hex(), size=len(blob))

    def test_the_footer_is_twenty_bytes_of_digest_and_twelve_of_padding(self):
        self.assertEqual(updates.PKG_FOOTER_BYTES, 32)
        self.assertEqual(updates.PKG_FOOTER_DIGEST_BYTES, 20)
        blob = signed_pkg(b"payload")
        self.assertEqual(blob[-12:], b"\x00" * 12)
        self.assertEqual(blob[-32:-12].hex(),
                         hashlib.sha1(b"payload").hexdigest())

    def test_a_package_shaped_like_the_real_one_passes(self):
        blob = signed_pkg(b"PKG" * 1000)
        item = package_for(blob)
        self.assertTrue(updates.verify_download(item, self.arrived(blob)))

    def test_hashing_the_whole_file_would_have_been_refused(self):
        # The bug this replaced, kept as a test so it cannot come back.
        blob = signed_pkg(b"PKG" * 1000)
        item = package_for(blob)
        whole = updates.Downloaded(body_sha1=hashlib.sha1(blob).hexdigest(),
                                   footer_sha1=blob[-32:][:20].hex(),
                                   size=len(blob))
        with self.assertRaises(VerificationFailed):
            updates.verify_download(item, whole)

    def test_the_real_bles01031_numbers(self):
        item = Package(version="01.02", size=self.REAL_SIZE,
                       sha1sum=self.REAL_SHA1,
                       url="http://b0.ww.np.dl.playstation.net/x.pkg")
        good = updates.Downloaded(body_sha1=self.REAL_SHA1,
                                  footer_sha1=self.REAL_SHA1,
                                  size=self.REAL_SIZE)
        self.assertTrue(updates.verify_download(item, good))
        # The digest of the whole file, which is what the old check compared.
        whole = updates.Downloaded(
            body_sha1="3d3a117d57323dbb4fcc84cef6636a39dcf88e9d",
            footer_sha1=self.REAL_SHA1, size=self.REAL_SIZE)
        with self.assertRaises(VerificationFailed):
            updates.verify_download(item, whole)

    def test_the_manifest_size_is_bytes_and_is_compared_as_bytes(self):
        # The UI says 62.3 MB for this package because it counts in units of
        # 1024. The manifest's own number is 65,334,960 and the check uses it
        # unchanged -- no rounding gets anywhere near this comparison.
        item = Package(version="01.02", size=self.REAL_SIZE,
                       sha1sum=self.REAL_SHA1,
                       url="http://b0.ww.np.dl.playstation.net/x.pkg")
        short = updates.Downloaded(body_sha1=self.REAL_SHA1,
                                   footer_sha1=self.REAL_SHA1,
                                   size=self.REAL_SIZE - 1)
        with self.assertRaises(VerificationFailed) as caught:
            updates.verify_download(item, short)
        message = str(caught.exception)
        self.assertIn("65,334,960 bytes", message)
        self.assertIn("62.3 MB", message)

    def test_a_footer_that_disagrees_with_the_manifest_is_refused(self):
        # The payload hashes correctly but the package's own footer does not
        # match it: a file somebody assembled rather than one Sony published.
        blob = signed_pkg(b"PKG" * 1000)
        item = package_for(blob)
        forged = updates.Downloaded(body_sha1=item.sha1sum,
                                    footer_sha1="0" * 40, size=len(blob))
        with self.assertRaises(VerificationFailed) as caught:
            updates.verify_download(item, forged)
        self.assertIn("does not agree with itself", str(caught.exception))

    def test_a_package_with_no_footer_read_back_is_still_checked(self):
        # Only the body digest is evidence here, and it is enough: the footer
        # check strengthens the answer, it does not stand in for it.
        blob = signed_pkg(b"PKG" * 1000)
        item = package_for(blob)
        self.assertTrue(updates.verify_download(
            item, updates.Downloaded(body_sha1=item.sha1sum, footer_sha1="",
                                     size=len(blob))))
        with self.assertRaises(VerificationFailed):
            updates.verify_download(
                item, updates.Downloaded(body_sha1="0" * 40, footer_sha1="",
                                         size=len(blob)))

    def test_case_does_not_matter(self):
        blob = signed_pkg(b"hello")
        item = package_for(blob)
        arrived = self.arrived(blob)
        arrived.body_sha1 = arrived.body_sha1.upper()
        arrived.footer_sha1 = arrived.footer_sha1.upper()
        self.assertTrue(updates.verify_download(item, arrived))

    def test_a_bare_digest_is_still_accepted(self):
        blob = signed_pkg(b"hello")
        item = package_for(blob)
        self.assertTrue(updates.verify_download(item, item.sha1sum))

    def test_a_mismatch_raises_rather_than_returning_false(self):
        # Raising is the point. A function that returned False could be called
        # and its answer dropped by a later edit, and nobody would notice until
        # somebody's console had a corrupt package on it.
        blob = signed_pkg(b"hello")
        with self.assertRaises(VerificationFailed) as caught:
            updates.verify_download(package_for(blob), "0" * 40)
        self.assertIn("not the file Sony published", str(caught.exception))

    def test_no_published_checksum_is_also_a_refusal(self):
        item = Package(version="01.00", size=5, sha1sum="", url="x")
        with self.assertRaises(VerificationFailed):
            updates.verify_download(item, hashlib.sha1(b"hello").hexdigest())


class TheAdviceAfterAFailure(unittest.TestCase):
    """A systematic mismatch must stop telling people to try again.

    It failed identically every time on every title, and the message kept
    saying the download had probably been corrupted and was worth retrying.
    That is several hundred megabytes of advice that could never work.
    """

    def setUp(self):
        self.blob = signed_pkg(b"PKG" * 1000)
        self.item = package_for(self.blob)
        self.history = {}

    def fail(self, digest="0" * 40):
        with self.assertRaises(VerificationFailed) as caught:
            updates.verify_download(self.item, digest, history=self.history)
        return str(caught.exception)

    def test_the_first_failure_suggests_trying_again(self):
        message = self.fail()
        self.assertIn("worth trying once more", message)
        self.assertNotIn("will not change it", message)

    def test_the_same_failure_twice_stops_suggesting_it(self):
        self.fail()
        message = self.fail()
        self.assertIn("will not change it", message)
        self.assertNotIn("worth trying once more", message)
        self.assertIn("console can still fetch the update itself", message)

    def test_a_different_failure_the_second_time_is_a_first_failure(self):
        # Two different wrong answers is the corruption case, not the
        # systematic one, so the advice to try again is still right.
        self.fail("0" * 40)
        self.assertIn("worth trying once more", self.fail("1" * 40))

    def test_without_a_history_every_failure_reads_as_the_first(self):
        self.history = None
        self.fail()
        self.assertIn("worth trying once more", self.fail())

    def test_two_screens_do_not_see_each_others_attempts(self):
        # The history is passed in rather than kept in a module global, so a
        # second window cannot make the first one's first failure look like a
        # repeat.
        self.fail()
        self.history = {}
        self.assertIn("worth trying once more", self.fail())

    def test_a_success_after_a_failure_is_not_blocked_by_the_history(self):
        self.fail()
        self.assertTrue(updates.verify_download(
            self.item, self.item.sha1sum, history=self.history))


# --- the sequence, which is the feature -------------------------------------

def a_row(item, title_id=BO2, name="Black Ops II"):
    return updates.TitleUpdate(title_id=title_id, name=name,
                               installed="01.05", latest=item.version,
                               size=item.size, package=item)


class Delivering(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ps3-updates-test-")
        self.addCleanup(lambda: _rmtree(self.folder))
        self.body = signed_pkg(b"PKG" * 4096)

    def test_the_happy_path_downloads_verifies_and_uploads(self):
        item = package_for(self.body)
        writer = RecordingWriter()
        delivered = updates.deliver(a_row(item), writer, self.folder,
                                    stream=stream_for(self.body),
                                    free_bytes=8 * 1024 ** 3)
        self.assertEqual(writer.made, ["/dev_hdd0/packages"])
        self.assertEqual(len(writer.stored), 1)
        path, body = writer.stored[0]
        self.assertEqual(path, "/dev_hdd0/packages/x-01.19.pkg")
        self.assertEqual(body, self.body)
        self.assertEqual(delivered.remote_path, path)
        self.assertEqual(delivered.sha1,
                         hashlib.sha1(self.body[:-32]).hexdigest())

    def test_a_checksum_mismatch_stops_before_the_write_client_is_touched(self):
        # The rule this whole feature rests on. The write client raises the
        # moment anything calls it, so this proves the upload is unreachable
        # rather than merely observing that nothing was uploaded.
        item = package(self.body, sha1="0" * 40)
        with self.assertRaises(VerificationFailed):
            updates.deliver(a_row(item), ExplodingWriter(), self.folder,
                            stream=stream_for(self.body),
                            free_bytes=8 * 1024 ** 3)

    def test_a_checksum_mismatch_leaves_no_downloaded_file_behind(self):
        item = package(self.body, sha1="0" * 40)
        with self.assertRaises(VerificationFailed):
            updates.deliver(a_row(item), ExplodingWriter(), self.folder,
                            stream=stream_for(self.body),
                            free_bytes=8 * 1024 ** 3)
        self.assertEqual(os.listdir(self.folder), [])

    def test_an_interrupted_download_never_reaches_the_write_client(self):
        item = package_for(self.body)
        with self.assertRaises(DownloadFailed):
            updates.deliver(a_row(item), ExplodingWriter(), self.folder,
                            stream=stream_for(self.body, stop_after=64),
                            free_bytes=8 * 1024 ** 3)

    def test_not_enough_room_refuses_before_anything_is_downloaded(self):
        item = package_for(self.body)

        def explode(url, timeout=None):
            raise AssertionError("it started downloading before checking room")

        with self.assertRaises(NotEnoughSpace):
            updates.deliver(a_row(item), ExplodingWriter(), self.folder,
                            stream=explode, free_bytes=1024)

    def test_a_console_that_disappears_mid_upload_says_what_to_check(self):
        item = package_for(self.body)
        writer = RecordingWriter(fault=ftplib.error_temp("421 goodbye"))
        with self.assertRaises(UploadFailed) as caught:
            updates.deliver(a_row(item), writer, self.folder,
                            stream=stream_for(self.body),
                            free_bytes=8 * 1024 ** 3)
        message = str(caught.exception)
        self.assertIn("/dev_hdd0/packages", message)
        self.assertIn("switched on", message)

    def test_the_downloaded_file_is_cleaned_up_after_a_failed_upload(self):
        item = package_for(self.body)
        writer = RecordingWriter(fault=ftplib.error_temp("421 goodbye"))
        with self.assertRaises(UploadFailed):
            updates.deliver(a_row(item), writer, self.folder,
                            stream=stream_for(self.body),
                            free_bytes=8 * 1024 ** 3)
        self.assertEqual(os.listdir(self.folder), [])

    def test_an_update_the_console_already_has_is_not_fetched_again(self):
        # Matters most on a retry: half the queue may already have gone in,
        # and the scan that produced these rows ran before any of it. The
        # installed version is read again rather than trusted from the scan.
        item = package_for(self.body, version="01.19")

        def explode(url, timeout=None):
            raise AssertionError("it downloaded something it did not need")

        delivered = updates.deliver(
            a_row(item), ExplodingWriter(), self.folder, stream=explode,
            free_bytes=8 * 1024 ** 3,
            installed_reader=lambda title_id: ("01.19", ""))
        self.assertTrue(delivered.skipped)
        self.assertIn("already on 01.19", delivered.reason)
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_console_that_is_behind_is_still_updated(self):
        item = package_for(self.body, version="01.19")
        writer = RecordingWriter()
        delivered = updates.deliver(
            a_row(item), writer, self.folder, stream=stream_for(self.body),
            free_bytes=8 * 1024 ** 3,
            installed_reader=lambda title_id: ("01.05", ""))
        self.assertFalse(delivered.skipped)
        self.assertEqual(len(writer.stored), 1)

    def test_a_version_that_cannot_be_read_does_not_block_the_update(self):
        # No answer is not the same as "already up to date". A title whose
        # PARAM.SFO will not read still gets its update.
        item = package_for(self.body, version="01.19")
        writer = RecordingWriter()
        delivered = updates.deliver(
            a_row(item), writer, self.folder, stream=stream_for(self.body),
            free_bytes=8 * 1024 ** 3,
            installed_reader=lambda title_id: (None, "no PARAM.SFO"))
        self.assertFalse(delivered.skipped)
        self.assertEqual(len(writer.stored), 1)

    def test_a_row_that_cannot_be_updated_is_refused(self):
        row = updates.TitleUpdate(title_id=BO2, name="X",
                                  blocked=updates.UP_TO_DATE)
        with self.assertRaises(updates.UpdateError):
            updates.deliver(row, ExplodingWriter(), self.folder,
                            stream=stream_for(b""))


class TheUploadedFilename(unittest.TestCase):
    """The name comes out of Sony's answer, so it is never trusted verbatim."""

    def test_a_plain_package_name_is_kept(self):
        item = Package(version="01.19", url=(
            "http://b0.ww.np.dl.playstation.net/tppkg/np/x/"
            "BLES01717_00-CODBLOPS2PATCH19.pkg"))
        self.assertEqual(updates.package_filename(item, BO2),
                         "BLES01717_00-CODBLOPS2PATCH19.pkg")

    def test_a_name_with_a_traversal_in_it_is_replaced(self):
        for url in ("http://b0.ww.np.dl.playstation.net/x/..%2F..%2Fboot.pkg",
                    "http://b0.ww.np.dl.playstation.net/x/a b.pkg",
                    "http://b0.ww.np.dl.playstation.net/x/x.self",
                    "http://b0.ww.np.dl.playstation.net/x/",
                    "not a url at all"):
            with self.subTest(url=url):
                name = updates.package_filename(
                    Package(version="01.19", url=url), BO2)
                self.assertEqual(name, "BLES01717_01.19.pkg")

    def test_the_replacement_is_always_a_safe_name(self):
        name = updates.package_filename(
            Package(version="../../1.0", url=""), "../evil")
        self.assertRegex(name, r"^[A-Za-z0-9._-]+\.pkg$")


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)



class TheScanSaysWhatItIsDoing(unittest.TestCase):
    """It reads every installed game and then asks Sony about each one.

    On a console with a shelf of games that is minutes with nothing on screen,
    which is indistinguishable from a hang.
    """

    def test_every_stage_of_the_console_scan_is_reported(self):
        said = []
        lister = FakeLister(listings={GAME: folders(BO2),
                                      "/dev_hdd0/packages": ""},
                            blobs={f"{GAME}/{BO2}/PARAM.SFO":
                                   param_sfo("01.05", BO2)})
        updates.scan_console(lister, on_stage=said.append)
        self.assertIn("Reading the games installed on the console", said)
        self.assertTrue(any("game folders" in item for item in said), said)

    def test_each_title_is_named_with_how_far_through_it_is(self):
        said = []
        lister = FakeLister(listings={GAME: folders(BO2),
                                      "/dev_hdd0/packages": ""},
                            blobs={f"{GAME}/{BO2}/PARAM.SFO":
                                   param_sfo("01.05", BO2)})
        updates.scan_console(lister, on_stage=said.append)
        self.assertTrue(any(BO2 in item and "1 of 1" in item
                            for item in said), said)

    def test_a_scan_with_no_listener_is_no_trouble(self):
        lister = FakeLister(listings={GAME: folders(BO2)})
        updates.scan_console(lister)


class TheThreeWaysACheckCanEndWithoutAnUpdate(unittest.TestCase):
    """Sony answering "no such title" is not the check having failed.

    BLES01368 came back as "Could not be checked", which reads as a fault on
    this side. Sony had answered perfectly well: 404, no entry for that title
    ID. Expansion discs and homebrew do the same.
    """

    TITLE = "BLES01368"

    def _row(self, fetcher):
        title = updates.InstalledTitle(title_id=self.TITLE, version="01.00")
        return updates.check_titles([title], fetcher=fetcher)[0]

    def test_a_404_says_sony_has_no_entry_for_it(self):
        def not_found(url, timeout=None):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        row = self._row(not_found)
        self.assertEqual(row.blocked, updates.NOT_LISTED)
        self.assertEqual(row.state_text, "Not in Sony's list")
        self.assertEqual(row.latest_text, "none published")

    def test_an_empty_answer_still_means_no_update_was_released(self):
        row = self._row(lambda url, timeout=None: b"")
        self.assertEqual(row.blocked, updates.NO_UPDATE_EVER)
        self.assertEqual(row.state_text, "No update exists")

    def test_only_a_failed_request_says_it_could_not_be_checked(self):
        def offline(url, timeout=None):
            raise urllib.error.URLError("no route to host")

        row = self._row(offline)
        self.assertEqual(row.blocked, updates.MANIFEST_FAILED)
        self.assertEqual(row.state_text, "Could not be checked")

    def test_the_three_never_share_a_message(self):
        def not_found(url, timeout=None):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        def offline(url, timeout=None):
            raise urllib.error.URLError("no route to host")

        said = {self._row(not_found).state_text,
                self._row(lambda url, timeout=None: b"").state_text,
                self._row(offline).state_text}
        self.assertEqual(len(said), 3, said)

    def test_a_404_raises_its_own_exception_rather_than_the_generic_one(self):
        def not_found(url, timeout=None):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with self.assertRaises(updates.TitleNotListed):
            updates.fetch_manifest(self.TITLE, fetcher=not_found)



class Clock:
    """A clock and a sleep that move together, so no test waits on anything."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def read(self):
        return self.now


class FiringActions:
    def __init__(self):
        self.fired = []

    def install_package(self, filename):
        self.fired.append(filename)
        return ActionResponse("/install.ps3/dev_hdd0/packages/" + filename,
                              200, "ok")


class Folder:
    """The console's packages folder as the poll sees it.

    The console deletes a package when the install finishes, so `takes` is
    how long each one is busy for. A name in `keeps` is one the console never
    finishes with, which is the case that has to time out rather than be
    reported as installed. `unreadable` makes the first few polls answer
    "the folder could not be read", the way a dropped connection does.
    """

    def __init__(self, clock, takes=0.0, keeps=(), unreadable=0):
        self.clock = clock
        self.takes = takes
        self.keeps = set(keeps)
        self.unreadable = unreadable
        self.asked = []
        self._due = {}

    def __call__(self, filename):
        self.asked.append(filename)
        if self.unreadable > 0:
            self.unreadable -= 1
            return None
        if filename in self.keeps:
            return True
        due = self._due.setdefault(filename, self.clock.read() + self.takes)
        return self.clock.read() < due


def reads_title(readable=False, version="", absent=False, detail=""):
    """A title reader answering the one thing it is asked, every time."""
    def read(title_id):
        return updates.TitleOnConsole(readable=readable, version=version,
                                      absent=absent, detail=detail)
    return read


def reads_version(versions, detail="", absent=False):
    """A version reader answering out of a dict of title ID to version."""
    def read(title_id):
        version = versions.get(title_id)
        return version, ("" if version else detail), absent
    return read


class TheInstallQueue(unittest.TestCase):
    """One at a time, each one followed through before the next is fired.

    Seven packages fired back to back on a real console installed three. The
    console ignores an install request while it is still busy with the last
    one and says nothing about having done so.

    What it does say, measured twice on 192.168.50.95 with a 39 MB Minecraft
    update, is that the package disappears from /dev_hdd0/packages when the
    install finishes: present from the moment the upload lands, gone about 21
    seconds later, exactly when the console reported the install complete.
    That is what the queue waits for.
    """

    ITEM = ("a.pkg", BO2, 39 * 1024 * 1024, "01.19")
    OTHER = ("b.pkg", GT5, 39 * 1024 * 1024, "02.17")
    LANDED = {BO2: "01.19", GT5: "02.17"}

    def test_each_install_waits_for_the_package_to_go_from_the_folder(self):
        # The title directory used to be the signal and it appears when the
        # install starts, so the next install was fired into a console still
        # busy with the last one.
        clock = Clock()
        actions = FiringActions()
        folder = Folder(clock, takes=21.0)
        results = updates.install_queue(
            actions, [self.ITEM, self.OTHER], folder,
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(actions.fired, ["a.pkg", "b.pkg"])
        self.assertTrue(all(item.confirmed for item in results))
        # Two packages, twenty one seconds of installing each.
        self.assertGreaterEqual(clock.now, 42.0)
        self.assertEqual(sorted(set(folder.asked)), ["a.pkg", "b.pkg"])

    def test_there_is_no_fixed_gap_between_one_install_and_the_next(self):
        # A fixed sleep was what stood here before. A console that has
        # finished is not made safer by being waited on afterwards, and the
        # gap was both too long for a small package and too short for a big
        # one.
        clock = Clock()
        updates.install_queue(
            FiringActions(), [self.ITEM, self.OTHER], Folder(clock),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(clock.now, 0.0)

    def test_how_long_a_package_is_given_is_for_the_caller_to_say(self):
        # Install packages sends whole games, which take longer to install
        # than a title update of the same size takes to patch, so it hands the
        # queue its own formula rather than sharing the one for updates.
        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock, keeps={"a.pkg"}),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            timeout_for=updates.package_timeout_for,
            sleep=clock.sleep, clock=clock.read)
        allowed = updates.package_timeout_for(self.ITEM[2])
        self.assertGreaterEqual(clock.now, allowed)
        self.assertFalse(results[0].confirmed)

    def test_the_wait_is_worked_out_from_the_size_of_the_package(self):
        # Thirty seconds plus two a megabyte, with a minute underneath it.
        # The 39 MB update measured on hardware took 21 seconds and is
        # allowed 108; a flat figure is wrong at one end or the other.
        self.assertEqual(updates.install_timeout_for(39 * 1024 * 1024), 108.0)
        self.assertEqual(updates.install_timeout_for(1024), 60.0)
        self.assertEqual(updates.install_timeout_for(0), 60.0)
        self.assertAlmostEqual(
            updates.install_timeout_for(542 * 1024 * 1024), 1114.0)

    def test_a_package_that_never_goes_is_reported_as_not_confirmed(self):
        # The file sitting there after the whole wait says the console never
        # finished with it. Carrying on as though it had installed is the one
        # thing this must not do.
        clock = Clock()
        folder = Folder(clock, keeps={"a.pkg"})
        results = updates.install_queue(
            FiringActions(), [self.ITEM], folder,
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            sleep=clock.sleep, clock=clock.read)
        self.assertFalse(results[0].confirmed)
        self.assertEqual(results[0].state, updates.STATE_UNCONFIRMED)
        self.assertIn("could not be confirmed", results[0].reason)
        # Slow is not refused. A package may well be installing still, and
        # calling it a failure sends somebody off to send a 2 GB file across
        # a second time for nothing.
        self.assertIn("may still be installing", results[0].reason)
        self.assertIn("108 seconds", results[0].reason)

    def test_one_that_times_out_does_not_stop_the_ones_behind_it(self):
        # Every package is tried. A failure costs only itself, and the file
        # stays on the console so it can be installed again from there rather
        # than sent a second time.
        clock = Clock()
        actions = FiringActions()
        results = updates.install_queue(
            actions, [self.ITEM, self.OTHER],
            Folder(clock, keeps={"a.pkg"}),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(actions.fired, ["a.pkg", "b.pkg"])
        self.assertEqual([item.confirmed for item in results], [False, True])

    def test_an_unconfirmed_package_is_still_reported_with_its_state(self):
        # It has to stay in front of the user. A run that quietly dropped the
        # one that did not install would leave somebody believing it did.
        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [self.ITEM, self.OTHER],
            Folder(clock, keeps={"a.pkg"}),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual([item.filename for item in results],
                         ["a.pkg", "b.pkg"])
        self.assertEqual([item.state for item in results],
                         [updates.STATE_UNCONFIRMED, updates.STATE_INSTALLED])

    def test_a_folder_that_could_not_be_read_is_not_the_package_going(self):
        # A dropped listing says nothing about the install. Reading it as
        # "the file has gone" would confirm an install nobody has evidence
        # for, on the one failure the console is most likely to produce.
        clock = Clock()
        folder = Folder(clock, takes=5.0, unreadable=3)
        results = updates.install_queue(
            FiringActions(), [self.ITEM], folder,
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            sleep=clock.sleep, clock=clock.read)
        self.assertTrue(results[0].confirmed)
        self.assertGreaterEqual(clock.now, 5.0)

    def test_the_poll_asks_about_the_packages_folder_and_nothing_else(self):
        # /dev_hdd0/vsh/task is deliberately left alone: the task directory
        # 00000001 sat there throughout both hardware installs and never
        # changed. /dev_hdd0/game answers when the install starts.
        lister = FakeLister(listings={"/dev_hdd0/packages":
                                      files("a.pkg")})
        present = updates.package_checker(lister)
        self.assertTrue(present("a.pkg"))
        self.assertFalse(present("b.pkg"))
        self.assertEqual(set(lister.asked), {"/dev_hdd0/packages/"})

    def test_a_folder_the_console_will_not_list_answers_not_known(self):
        lister = FakeLister(listings={}, fail_on={"/dev_hdd0/packages"})
        self.assertIsNone(updates.package_checker(lister)("a.pkg"))

    def test_the_version_the_game_reports_afterwards_is_what_confirms_it(self):
        # The package going says the console has finished with it. Whether
        # the install worked is a second question and it has its own read.
        clock = Clock()
        asked = []

        def reader(title_id):
            asked.append(title_id)
            return "01.19", "", False

        results = updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock),
            confirm=updates.version_confirmation(reader),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(asked, [BO2])
        self.assertTrue(results[0].confirmed)
        self.assertEqual(results[0].state, updates.STATE_INSTALLED)
        self.assertEqual(results[0].installed_version, "01.19")

    def test_a_version_that_did_not_change_is_a_failure_naming_both(self):
        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock),
            confirm=updates.version_confirmation(
                reads_version({BO2: "01.05"})),
            sleep=clock.sleep, clock=clock.read)
        self.assertFalse(results[0].confirmed)
        self.assertEqual(results[0].state, updates.STATE_FAILED)
        self.assertIn("01.19", results[0].reason)
        self.assertIn("01.05", results[0].reason)
        self.assertEqual(results[0].installed_version, "01.05")

    def test_a_game_with_no_title_update_on_it_afterwards_is_a_failure(self):
        # read_installed_state says absent when the console answers that
        # there is no such path, which is a real answer: nothing went on.
        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock),
            confirm=updates.version_confirmation(
                reads_version({}, absent=True)),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(results[0].state, updates.STATE_FAILED)
        self.assertIn("no title update", results[0].reason)

    def test_a_version_that_cannot_be_read_is_neither_answer(self):
        # A check that could not complete is not a result. It is said as its
        # own sentence rather than rounded to "installed" or "failed".
        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock),
            confirm=updates.version_confirmation(reads_version(
                {}, detail="The console stopped answering.")),
            sleep=clock.sleep, clock=clock.read)
        self.assertFalse(results[0].confirmed)
        self.assertEqual(results[0].state, updates.STATE_UNCONFIRMED)
        self.assertIn("not known", results[0].reason)
        self.assertIn("stopped answering", results[0].reason)

    def test_two_spellings_of_one_version_are_not_a_failed_install(self):
        # The console writes 01.19 where Sony's list writes 1.19.
        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [("a.pkg", BO2, 1024, "1.19")], Folder(clock),
            confirm=updates.version_confirmation(
                reads_version({BO2: "01.19"})),
            sleep=clock.sleep, clock=clock.read)
        self.assertTrue(results[0].confirmed)

    def test_a_package_with_no_published_version_is_not_called_installed(self):
        # This used to report the install as done. Nothing had been checked:
        # with no version to compare against, the whole confirmation step
        # returned early and said yes, so it passed its tests while never
        # running and versions were being checked by hand on the console.
        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [("homebrew.pkg", "")], Folder(clock),
            confirm=updates.version_confirmation(reads_version({})),
            sleep=clock.sleep, clock=clock.read)
        self.assertFalse(results[0].confirmed)
        self.assertEqual(results[0].state, updates.STATE_UNCONFIRMED)
        self.assertIn("no version to check it against", results[0].reason)

    def test_the_version_is_actually_read_back_after_every_install(self):
        # The point of this one is that the reader is called at all. The
        # version comparison shipped once in a state where two short circuits
        # meant it never ran, and every test of it passed: they all checked
        # that the comparison was right when it happened, and none of them
        # checked that it happened.
        asked = []

        def counts_the_asking(title_id):
            asked.append(title_id)
            return self.LANDED.get(title_id), "", False

        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [self.ITEM, self.OTHER], Folder(clock),
            confirm=updates.version_confirmation(counts_the_asking),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(asked, [BO2, GT5])
        self.assertTrue(all(item.confirmed for item in results))

    def test_an_install_nothing_checked_is_never_reported_as_done(self):
        # A caller that supplies no confirmation has confirmed nothing, and
        # the queue has no default to fall back on. The file leaving the
        # packages folder says the console has finished with it, which is not
        # the same as the install having worked.
        clock = Clock()
        results = updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock),
            sleep=clock.sleep, clock=clock.read)
        self.assertFalse(results[0].confirmed)
        self.assertEqual(results[0].state, updates.STATE_UNCONFIRMED)
        self.assertIn("Nothing checked", results[0].reason)

    def test_nothing_is_ever_deleted_from_the_console(self):
        # There was a delete, gated on the title directory appearing. That
        # directory appears when the console's installer starts, so a 2.1 GB
        # package was deleted after an install that failed. The capability is
        # gone rather than guarded: a leftover package is visible in the
        # recovery list and can be installed again without being sent twice.
        import inspect
        self.assertFalse(hasattr(updates, "package_remover"))
        self.assertNotIn(
            "remove", inspect.signature(updates.install_queue).parameters)
        self.assertNotIn(
            "removed", updates.Installed.__dataclass_fields__)

    def test_a_writer_is_never_asked_for_during_an_install(self):
        writer = RecordingWriter()
        clock = Clock()
        updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(writer.deleted, [])

    def test_a_console_refusing_one_still_gets_asked_about_the_next(self):
        asked = []

        class RefusesTheFirst:
            def install_package(self, filename):
                asked.append(filename)
                if filename == "a.pkg":
                    raise ActionFailed("The console answered 404.")
                return ActionResponse("/install.ps3/dev_hdd0/packages/"
                                      + filename, 200, "ok")

        clock = Clock()
        results = updates.install_queue(
            RefusesTheFirst(), [self.ITEM, self.OTHER], Folder(clock),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(asked, ["a.pkg", "b.pkg"])
        self.assertIn("404", results[0].reason)
        self.assertTrue(results[1].confirmed)

    def test_progress_says_which_package_and_how_many(self):
        clock = Clock()
        seen = []
        updates.install_queue(
            FiringActions(), [self.ITEM, self.OTHER], Folder(clock),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            on_progress=seen.append, sleep=clock.sleep, clock=clock.read)
        self.assertIn(("installing", "a.pkg", 1, 2, BO2), seen)
        self.assertIn(("checking", "a.pkg", 1, 2, BO2), seen)
        self.assertIn(("installed", "a.pkg", 1, 2, BO2), seen)
        self.assertIn(("installing", "b.pkg", 2, 2, GT5), seen)

    def test_the_wait_reports_the_time_spent_against_the_time_allowed(self):
        # The install stage had nothing on screen at all, on the one stage
        # that takes the longest. There is no percentage to show: the console
        # says nothing between being asked and deleting the package.
        clock = Clock()
        seen = []
        updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock, takes=3.0),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            on_progress=seen.append, sleep=clock.sleep, clock=clock.read)
        waits = [item for item in seen if item[0] == "waiting"]
        self.assertTrue(waits)
        self.assertEqual([item[2] for item in waits], [0.0, 1.0, 2.0])
        self.assertTrue(all(item[3] == 108.0 for item in waits))
        self.assertEqual(updates.install_stage_text("waiting", 14.0, 108.0),
                         "installing, 14 seconds of 108")

    def test_an_unconfirmed_package_says_so_in_the_stage_it_reports(self):
        clock = Clock()
        seen = []
        updates.install_queue(
            FiringActions(), [self.ITEM], Folder(clock, keeps={"a.pkg"}),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            on_progress=seen.append, sleep=clock.sleep, clock=clock.read)
        self.assertIn(("unconfirmed", "a.pkg", 1, 1, BO2), seen)
        self.assertNotIn(("installed", "a.pkg", 1, 1, BO2), seen)
        self.assertEqual(updates.install_stage_text("unconfirmed"),
                         "install not confirmed")

    def test_cancelling_stops_within_one_poll_rather_than_the_timeout(self):
        # The wait is measured in minutes. A worker that only noticed at the
        # end of it would hold the window open on shutdown.
        clock = Clock()
        stop = []
        actions = FiringActions()

        def sleeping(seconds):
            clock.sleep(seconds)
            if clock.now >= 2.0:
                stop.append(True)

        results = updates.install_queue(
            actions, [self.ITEM, self.OTHER, ("c.pkg", "BLES00003")],
            Folder(clock, keeps={"a.pkg", "b.pkg", "c.pkg"}),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            cancelled=lambda: bool(stop), sleep=sleeping, clock=clock.read)
        self.assertLess(clock.now, 10.0)
        self.assertEqual(actions.fired, ["a.pkg"])
        self.assertIn("stopped", results[0].reason)
        self.assertIn("not sent", results[1].reason)
        self.assertEqual(results[1].state, updates.STATE_NOT_SENT)
        self.assertIn("not sent", results[2].reason)

    def test_nothing_new_is_fired_once_it_has_been_cancelled(self):
        clock = Clock()
        actions = FiringActions()
        results = updates.install_queue(
            actions, [self.ITEM], Folder(clock),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            cancelled=lambda: True, sleep=clock.sleep, clock=clock.read)
        self.assertEqual(actions.fired, [])
        self.assertIn("not sent", results[0].reason)

    def test_one_press_installs_one_package_the_same_way(self):
        clock = Clock()
        actions = FiringActions()
        outcome = updates.install_one(
            actions, "a.pkg", BO2, Folder(clock),
            confirm=updates.version_confirmation(
                reads_version(self.LANDED)),
            size=39 * 1024 * 1024, version="01.19",
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(actions.fired, ["a.pkg"])
        self.assertTrue(outcome.confirmed)



class ConfirmingAPackageSomebodyChose(unittest.TestCase):
    """Install packages has no published version to compare against.

    A file somebody picked off their own disk carries no version Sony ever
    published, so the question the update flow asks cannot be asked here. What
    can be asked is whether the console has the title's own details afterwards.

    Only after the package has gone from the packages folder. The directory
    under /dev_hdd0/game appears when the console's installer starts, and
    acting on it earlier is what deleted a 2.1 GB package after an install
    that had failed.
    """

    def test_a_title_the_console_can_describe_afterwards_is_installed(self):
        outcome = updates.Installed(filename="game.pkg", title_id=BO2)
        updates.directory_confirmation(
            reads_title(readable=True, version="01.00"))(outcome)
        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.state, updates.STATE_INSTALLED)
        self.assertEqual(outcome.installed_version, "01.00")

    def test_details_that_read_are_the_answer_whatever_is_in_them(self):
        # A game installed from a package is not a title update and often
        # carries no APP_VER at all. The question here is whether the console
        # has the title, so a PARAM.SFO that parses settles it.
        outcome = updates.Installed(filename="game.pkg", title_id=BO2)
        updates.directory_confirmation(
            reads_title(readable=True))(outcome)
        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.state, updates.STATE_INSTALLED)

    def test_nothing_under_the_title_is_unconfirmed_rather_than_failed(self):
        # DLC and patches go into a game that is already on the console and
        # leave no folder of their own, so an empty answer is not evidence
        # that the install failed.
        outcome = updates.Installed(filename="dlc.pkg", title_id=BO2)
        updates.directory_confirmation(reads_title(absent=True))(outcome)
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.state, updates.STATE_UNCONFIRMED)
        self.assertIn(BO2, outcome.reason)

    def test_details_that_could_not_be_read_say_so_in_their_own_words(self):
        # A console that stopped answering is not a console that has told us
        # anything about the install.
        outcome = updates.Installed(filename="game.pkg", title_id=BO2)
        updates.directory_confirmation(
            reads_title(detail="The console stopped answering."))(outcome)
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.state, updates.STATE_UNCONFIRMED)
        self.assertIn("stopped answering", outcome.reason)

    def test_a_package_that_says_nothing_about_which_title_it_is(self):
        # Homebrew often carries no title ID this end can read. There is then
        # nothing to ask the console about, and the install is reported as
        # unconfirmed rather than as done.
        outcome = updates.Installed(filename="homebrew.pkg", title_id="")
        updates.directory_confirmation(reads_title(readable=True))(outcome)
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.state, updates.STATE_UNCONFIRMED)

    def test_the_console_being_asked_at_all_is_what_is_checked_here(self):
        # The same trap as the version comparison, which shipped once in a
        # state where it never ran and every test of it still passed.
        asked = []

        def counts_the_asking(title_id):
            asked.append(title_id)
            return updates.TitleOnConsole(readable=True, version="01.00")

        clock = Clock()
        updates.install_queue(
            FiringActions(), [("game.pkg", BO2, 1024)], Folder(clock),
            confirm=updates.directory_confirmation(counts_the_asking),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(asked, [BO2])

    def test_the_title_is_not_looked_for_until_the_package_has_gone(self):
        # The directory appears when the install starts. Asking while the
        # package is still there is how this program once concluded that a
        # failed install had worked.
        asked_at = []
        clock = Clock()
        folder = Folder(clock, takes=21.0)

        def notes_when(title_id):
            asked_at.append(clock.read())
            return updates.TitleOnConsole(readable=True, version="01.00")

        updates.install_queue(
            FiringActions(), [("game.pkg", BO2, 1024)], folder,
            confirm=updates.directory_confirmation(notes_when),
            sleep=clock.sleep, clock=clock.read)
        self.assertEqual(len(asked_at), 1)
        self.assertGreaterEqual(asked_at[0], 21.0)

    def test_a_package_is_given_longer_than_a_title_update_of_one_size(self):
        # A 39 MB update took about 21 seconds on hardware. A whole game is
        # the case this exists for, and it is not the same size of job.
        for megabytes in (39, 542, 4096):
            size = megabytes * 1024 * 1024
            self.assertGreater(updates.package_timeout_for(size),
                               updates.install_timeout_for(size))

    def test_the_wait_for_a_package_has_a_floor_and_a_ceiling(self):
        # A tiny package still gets minutes: the poll costs nothing and the
        # console can be busy with something else. A huge one stops at four
        # hours, because past that nobody is still sitting there.
        self.assertEqual(updates.package_timeout_for(0),
                         updates.PACKAGE_MINIMUM_TIMEOUT_SECONDS)
        self.assertEqual(updates.package_timeout_for(500 * 1024 ** 3),
                         updates.PACKAGE_MAXIMUM_TIMEOUT_SECONDS)


class WhatTheLastCheckFound(unittest.TestCase):
    """The scan is remembered so the next visit has something to offer.

    A full check reads every game folder and opens every unnamed disc image.
    Doing that again to find out whether the three games that were behind are
    still behind is minutes spent on a question that needs seconds.
    """

    def rows(self):
        behind = updates.TitleUpdate(
            title_id=BO2, name="Black Ops II", installed="01.05",
            latest="01.19", size=100,
            package=updates.Package(version="01.19", size=100,
                                    sha1sum="a" * 40, url="http://x/y.pkg"))
        current = updates.TitleUpdate(title_id=GT5, name="GT5",
                                      installed="02.17", latest="02.17")
        return [behind, current]

    def test_a_scan_is_remembered_against_the_console_it_came_from(self):
        settings = {}
        updates.remember_scan(settings, "192.168.50.95", self.rows())
        self.assertIsNotNone(
            updates.remembered_scan(settings, "192.168.50.95"))
        # Another console has its own games and must not see these.
        self.assertIsNone(updates.remembered_scan(settings, "192.168.50.96"))

    def test_it_remembers_which_ones_were_behind(self):
        settings = {}
        entry = updates.remember_scan(settings, "host", self.rows())
        self.assertEqual(updates.titles_behind(entry), [BO2])

    def test_nothing_remembered_can_be_acted_on(self):
        settings = {}
        updates.remember_scan(settings, "host", self.rows())
        entry = updates.remembered_scan(settings, "host")
        rows = updates.remembered_rows(entry)
        self.assertEqual(len(rows), 2)
        for row in rows:
            with self.subTest(row.title_id):
                self.assertFalse(row.updatable)
                self.assertEqual(row.state_text, "From the last check")
                self.assertIsNone(row.package)

    def test_a_download_url_is_never_kept(self):
        # Sony is asked fresh every time. A remembered URL or checksum is the
        # one thing here that could be acted on while being out of date.
        settings = {}
        updates.remember_scan(settings, "host", self.rows())
        kept = json.dumps(settings)
        self.assertNotIn("http://x/y.pkg", kept)
        self.assertNotIn("a" * 40, kept)

    def test_a_scan_too_old_to_trust_is_not_offered(self):
        settings = {}
        old = datetime.datetime.now() - datetime.timedelta(days=60)
        updates.remember_scan(settings, "host", self.rows(), now=old)
        self.assertIsNone(updates.remembered_scan(settings, "host"))

    def test_rubbish_in_the_settings_file_is_ignored(self):
        for stored in ({"updates_seen": "not a dict"},
                       {"updates_seen": {"host": []}},
                       {"updates_seen": {"host": {"titles": "no"}}},
                       {"updates_seen": {"host": {"titles": [],
                                                  "checked": "never"}}},
                       {}):
            with self.subTest(stored):
                self.assertIsNone(updates.remembered_scan(stored, "host"))

    def test_it_survives_a_round_trip_through_the_settings_file(self):
        settings = {}
        updates.remember_scan(settings, "host", self.rows())
        reloaded = json.loads(json.dumps(settings))
        entry = updates.remembered_scan(reloaded, "host")
        self.assertEqual(updates.titles_behind(entry), [BO2])


class UnreadableLister(FakeLister):
    """A console that takes the connection and then fails the read.

    Kept apart from FakeLister's 550, which is the console answering that the
    file is not there. This is the read itself going wrong, and the two have
    to end up saying different things.
    """

    def __init__(self, fault, **kwargs):
        super().__init__(**kwargs)
        self.fault = fault

    def download_bytes(self, path, max_bytes=None):
        raise self.fault


class TheShortWayRound(unittest.TestCase):
    """A partial scan reads the named titles and nothing else."""

    def test_it_reads_only_the_titles_it_was_given(self):
        lister = FakeLister(
            listings={GAME: folders(BO2, GT5)},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.05", BO2)})
        found = updates.scan_titles(lister, [BO2])
        self.assertEqual([item.title_id for item in found], [BO2])
        self.assertEqual(found[0].version, "1.05")
        # No game folder walk and no device listing: that is the whole point.
        self.assertEqual(lister.asked, [])

    def test_a_title_that_has_gone_comes_back_with_no_version(self):
        # A game that was deleted and a game that has never been patched both
        # have no folder under /dev_hdd0/game. The short scan reads them the
        # same way, and a full scan is what tells them apart.
        lister = FakeLister(listings={}, blobs={})
        found = updates.scan_titles(lister, ["BLES09999"])
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].version)

    def test_it_says_which_title_it_is_reading(self):
        said = []
        lister = FakeLister(listings={}, blobs={})
        updates.scan_titles(lister, [BO2, GT5], on_stage=said.append)
        self.assertEqual(len(said), 2)
        self.assertIn(BO2, said[0])
        self.assertIn("1 of 2", said[0])

    def test_a_game_with_no_title_update_installed_reads_as_none(self):
        # BLES01945, BLES01976 and BLES02077 came back from a full scan as
        # installed "none" and went to "not known" with the reason error_perm
        # the moment the short check was pressed. A game that has never been
        # patched has no folder under /dev_hdd0/game, so the 550 is the answer
        # rather than a failure to get one.
        lister = FakeLister(listings={}, blobs={})
        found = updates.scan_titles(lister, ["BLES01945"])
        self.assertTrue(found[0].no_update_installed)
        self.assertIsNone(found[0].version)
        self.assertEqual(found[0].detail, "")
        row = updates.check_titles(
            found, fetcher=fetcher_for(
                {"BLES01945": manifest_bytes("BCES00569-ver.xml")}))[0]
        self.assertEqual(row.installed_text, "none")
        self.assertEqual(row.state_text, "Update available")

    def test_a_console_that_stopped_answering_is_still_not_known(self):
        # The other half of the same bug. "Cannot tell" has to keep meaning
        # something, so a read that genuinely failed must not be recorded as a
        # game with nothing installed.
        for fault in (ftplib.error_temp("421 the console went away"),
                      OSError("connection reset"),
                      TimeoutError("timed out")):
            with self.subTest(fault):
                found = updates.scan_titles(UnreadableLister(fault), [BO2])
                self.assertFalse(found[0].no_update_installed)
                self.assertIsNone(found[0].version)
                self.assertIn("could not be read", found[0].detail)
                row = updates.check_titles(
                    found, fetcher=fetcher_for(
                        {BO2: manifest_bytes("BLES01717-ver.xml")}))[0]
                self.assertEqual(row.installed_text, "not known")
                self.assertEqual(row.state_text, "Cannot tell")

    def test_a_param_sfo_that_will_not_parse_is_still_not_known(self):
        # The file was there and it was read. Something is wrong with it, and
        # calling that "no update installed" would offer a reinstall as though
        # the game had never been patched.
        lister = FakeLister(
            listings={},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": b"not a PARAM.SFO at all"})
        found = updates.scan_titles(lister, [BO2])
        self.assertFalse(found[0].no_update_installed)
        self.assertIsNone(found[0].version)
        self.assertTrue(found[0].detail)

    def test_a_refusal_that_is_not_a_550_is_not_an_answer(self):
        # 530 is the console refusing the login. Every title would read as
        # having nothing installed, and the screen would offer to reinstall a
        # whole console's worth of title updates.
        lister = UnreadableLister(ftplib.error_perm("530 Not logged in"))
        found = updates.scan_titles(lister, [BO2])
        self.assertFalse(found[0].no_update_installed)
        self.assertIsNone(found[0].version)

    def test_it_agrees_with_a_full_scan_of_the_same_console(self):
        # The two ways of looking at one console have to give one answer. A
        # game held as a disc image and never patched is the row this screen
        # exists for, and the short check used to turn it into "Cannot tell".
        lister = FakeLister(
            listings={GAME: folders(BO2),
                      f"{GAME}/{BO2}/USRDIR": files("EBOOT.BIN"),
                      PS3ISO: files(f"Gran Turismo 5 [{GT5}].iso")},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.05", BO2)})
        fetcher = fetcher_for({BO2: manifest_bytes("BLES01717-ver.xml"),
                               GT5: manifest_bytes("BCES00569-ver.xml")})
        full, _notes, _unnamed = updates.scan_console(lister)
        short = updates.scan_titles(lister, [BO2, GT5])

        def shown(found):
            return {row.title_id: (row.installed_text, row.state_text,
                                   row.blocked, row.updatable)
                    for row in updates.check_titles(found, fetcher=fetcher)}

        self.assertEqual(shown(full), shown(short))
        self.assertEqual(shown(short)[GT5][:2], ("none", "Update available"))
        self.assertEqual(shown(short)[BO2][0], "1.05")


class TheListIsMergedNotReplaced(unittest.TestCase):
    """One list per console, added to rather than rebuilt.

    Rows appeared and disappeared between scans because each scan replaced the
    list with whatever it found. The disc-image pass is the slow part, so a
    scan that skipped it, or lost a worker in it, took every image-only game
    off the table while nothing on the console had changed.
    """

    def row(self, title_id, where, name=None):
        return updates.TitleUpdate(title_id=title_id, name=name or title_id,
                                   found_in=where)

    def previous(self):
        return [self.row("BLES00001", updates.FROM_GAME_FOLDER),
                self.row("BLES00002", updates.FROM_DISC_IMAGE)]

    def test_a_scan_that_skipped_the_images_removes_no_image_title(self):
        merged = updates.merge_scan(
            self.previous(), [self.row("BLES00001", updates.FROM_GAME_FOLDER)],
            looked_in=[updates.FROM_GAME_FOLDER])
        self.assertEqual([row.title_id for row in merged],
                         ["BLES00001", "BLES00002"])

    def test_a_scan_that_did_look_removes_what_is_gone(self):
        merged = updates.merge_scan(
            self.previous(), [self.row("BLES00001", updates.FROM_GAME_FOLDER)],
            looked_in=[updates.FROM_GAME_FOLDER, updates.FROM_DISC_IMAGE])
        self.assertEqual([row.title_id for row in merged], ["BLES00001"])

    def test_a_partial_scan_speaks_only_for_what_it_was_asked(self):
        merged = updates.merge_scan(
            self.previous(), [self.row("BLES00001", updates.FROM_GAME_FOLDER)],
            looked_at=["BLES00001"])
        self.assertEqual([row.title_id for row in merged],
                         ["BLES00001", "BLES00002"])

    def test_a_partial_scan_removes_a_title_it_looked_for_and_lost(self):
        merged = updates.merge_scan(self.previous(), [],
                                    looked_at=["BLES00002"])
        self.assertEqual([row.title_id for row in merged], ["BLES00001"])

    def test_a_scan_that_found_nothing_and_looked_nowhere_changes_nothing(self):
        merged = updates.merge_scan(self.previous(), [])
        self.assertEqual([row.title_id for row in merged],
                         ["BLES00001", "BLES00002"])

    def test_a_fresh_row_replaces_the_one_it_matches(self):
        fresh = self.row("BLES00002", updates.FROM_DISC_IMAGE, name="Renamed")
        merged = updates.merge_scan(
            self.previous(), [fresh],
            looked_in=[updates.FROM_GAME_FOLDER, updates.FROM_DISC_IMAGE])
        by_id = {row.title_id: row for row in merged}
        self.assertEqual(by_id["BLES00002"].name, "Renamed")

    def test_every_row_a_scan_saw_is_stamped_and_keeps_its_provenance(self):
        merged = updates.merge_scan(
            self.previous(), [self.row("BLES00001", updates.FROM_GAME_FOLDER)],
            looked_in=[updates.FROM_GAME_FOLDER])
        by_id = {row.title_id: row for row in merged}
        self.assertTrue(by_id["BLES00001"].last_seen)
        self.assertEqual(by_id["BLES00001"].found_in, updates.FROM_GAME_FOLDER)
        self.assertEqual(by_id["BLES00002"].found_in, updates.FROM_DISC_IMAGE)

    def test_the_order_does_not_move_about(self):
        merged = updates.merge_scan(
            [], [self.row("BLES00009", updates.FROM_GAME_FOLDER),
                 self.row("BLES00001", updates.FROM_GAME_FOLDER)],
            looked_in=[updates.FROM_GAME_FOLDER])
        self.assertEqual([row.title_id for row in merged],
                         ["BLES00001", "BLES00009"])


class AnImagePassThatDidNotFinish(unittest.TestCase):
    """An image nobody opened stays unidentified, so the next scan tries it."""

    def image(self, name):
        return {"name": name, "kind": "file", "size": 4096,
                "device": "dev_hdd0", "folder": "PS3ISO"}

    def test_an_image_that_was_never_opened_is_still_unidentified(self):
        # A worker that died returns nothing for its share. Treating that as
        # "looked at and found nothing" is what made the games vanish.
        lister = FakeLister(listings={PS3ISO: files("One.iso", "Two.iso")})
        found, _notes, unnamed = updates.scan_console(
            lister, image_identifier=lambda entries: [])
        self.assertEqual(sorted(entry["name"] for entry in unnamed),
                         ["One.iso", "Two.iso"])
        self.assertEqual(found, [])

    def test_an_image_that_was_opened_leaves_the_list(self):
        lister = FakeLister(listings={PS3ISO: files("One.iso")})

        def identify(entries):
            return [{"name": "One.iso", "title_id": GT5, "opened": True,
                     "device": "dev_hdd0", "folder": "PS3ISO"}]

        _found, _notes, unnamed = updates.scan_console(
            lister, image_identifier=identify)
        self.assertEqual(unnamed, [])

    def test_an_image_opened_but_not_identified_also_leaves_the_list(self):
        # It had its chance and the answer was "nothing in it says". Asking
        # again every scan would be minutes spent on a settled question.
        lister = FakeLister(listings={PS3ISO: files("One.iso")})

        def identify(entries):
            return [{"name": "One.iso", "title_id": None, "opened": True,
                     "device": "dev_hdd0", "folder": "PS3ISO"}]

        _found, _notes, unnamed = updates.scan_console(
            lister, image_identifier=identify)
        self.assertEqual(unnamed, [])



class TheInstallWizard(unittest.TestCase):
    """What is about to happen, said before it happens.

    Two things go wrong at this point and both are avoidable: the console
    being inside a game rather than on its own menu, and somebody expecting
    seconds when it is minutes.
    """

    def rows(self, count=1):
        return [updates.TitleUpdate(
            title_id=f"BLES0000{index}", name=f"Game {index}",
            package=updates.Package(version="1.19", size=65334960))
            for index in range(count)]

    def wizard(self, count=1):
        from ps3tools.screens.gameupdates import InstallWizard
        from ps3tools.shell.app import AppTheme
        dialog = InstallWizard(self.rows(count), AppTheme("dark"))
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_the_console_menu_comes_first(self):
        # An update cannot be installed while its game is running, and the
        # console refuses it after the whole download has been paid for.
        wizard = self.wizard()
        self.assertIn("main menu", wizard.heading.text())
        self.assertIn("out of any game", wizard.body_text.text())

    def test_the_second_step_says_how_much_and_how_it_goes(self):
        wizard = self.wizard(count=2)
        wizard._forward()
        self.assertIn("2 updates", wizard.heading.text())
        self.assertIn("124.6 MB", wizard.heading.text())
        self.assertIn("one at a time", wizard.body_text.text())
        self.assertIn("press O", wizard.body_text.text())

    def test_the_last_step_says_start_rather_than_next(self):
        wizard = self.wizard()
        self.assertEqual(wizard.next_button.text(), "Next")
        wizard._forward()
        self.assertEqual(wizard.next_button.text(), "Start")

    def test_it_can_be_walked_backwards(self):
        wizard = self.wizard()
        self.assertFalse(wizard.back_button.isEnabled())
        wizard._forward()
        self.assertTrue(wizard.back_button.isEnabled())
        wizard._back()
        self.assertIn("main menu", wizard.heading.text())

    def test_finishing_accepts_and_cancelling_does_not(self):
        wizard = self.wizard()
        wizard._forward()
        wizard._forward()
        self.assertEqual(wizard.result(), wizard.DialogCode.Accepted)
        other = self.wizard()
        other.cancel_button.click()
        self.assertEqual(other.result(), other.DialogCode.Rejected)

    def test_it_reaches_no_console_and_no_network(self):
        # It asks and returns. Everything it says is built from the rows it
        # was handed.
        wizard = self.wizard()
        self.assertEqual(len(wizard.steps), 2)
        for heading, body in wizard.steps:
            self.assertTrue(heading)
            self.assertTrue(body)



class AWorkerThatLosesItsConnection(unittest.TestCase):
    """A dropped connection must not look like an empty disc image.

    Reported from hardware: Minecraft and Advanced Warfare identified on one
    scan and on the next were "could not be identified", with no row at all.
    Same files, same console, nothing changed. webMANftpd hangs up when it has
    had enough data connections in quick succession and this pass opens
    several at once, so one share died and returned nothing, which read as
    "these images have no game in them".
    """

    def images(self, *names):
        return [{"name": name, "kind": "file", "size": 4096,
                 "device": "dev_hdd0", "folder": "PS3ISO"} for name in names]

    def test_a_share_that_dies_is_tried_again(self):
        tries = []

        class Lister:
            def __enter__(inner):
                tries.append(1)
                if len(tries) == 1:
                    raise OSError("421 the console hung up")
                return inner

            def __exit__(inner, *exc):
                return False

        def identify(entries, reader, **kwargs):
            return {"isos": [{"name": entries[0]["name"], "opened": True,
                              "title_id": BO2}]}

        with mock.patch.object(updates.isoreader, "identify_isos", identify), \
             mock.patch.object(updates.isoreader, "reader_for",
                               lambda lister: mock.Mock()), \
             mock.patch.object(updates.time, "sleep", lambda seconds: None):
            found = updates.parallel_image_identifier(Lister, workers=1)(
                self.images("Minecraft.iso"))
        self.assertEqual(len(tries), 2)
        self.assertEqual(found[0]["title_id"], BO2)

    def test_a_share_that_will_not_read_reports_every_image_in_it(self):
        # It used to return nothing, so the images vanished with no row and
        # no sentence saying why.
        class Lister:
            def __enter__(inner):
                raise OSError("421 the console hung up")

            def __exit__(inner, *exc):
                return False

        with mock.patch.object(updates.isoreader, "reader_for",
                               lambda lister: mock.Mock()), \
             mock.patch.object(updates.time, "sleep", lambda seconds: None):
            found = updates.parallel_image_identifier(Lister, workers=1)(
                self.images("Minecraft.iso", "AW.iso"))
        self.assertEqual(sorted(row["name"] for row in found),
                         ["AW.iso", "Minecraft.iso"])
        for row in found:
            self.assertFalse(row["opened"])
            self.assertIn("stopped answering", row["reason"])

    def test_an_unread_image_stays_on_the_list_for_next_time(self):
        lister = FakeLister(listings={PS3ISO: files("Minecraft.iso")})

        def identify(entries):
            return [{"name": "Minecraft.iso", "opened": False,
                     "title_id": None, "reason": "the console stopped"}]

        _found, notes, unnamed = updates.scan_console(
            lister, image_identifier=identify)
        self.assertEqual([entry["name"] for entry in unnamed],
                         ["Minecraft.iso"])
        said = " ".join(notes)
        self.assertIn("could not be read from the console", said)
        self.assertIn("Minecraft.iso", said)

    def test_unread_and_unidentifiable_are_said_differently(self):
        # One is "we do not know", the other is "we looked and there is
        # nothing in it". Collapsing them is what made a game look lost.
        unread = updates._image_pass_note(1, [], [], [], ["Minecraft.iso"])
        empty = updates._image_pass_note(1, [], [], ["Odd.iso"], [])
        self.assertIn("could not be read", unread)
        self.assertNotIn("nothing in it that names a game", unread)
        self.assertIn("nothing in it that names a game", empty)
        self.assertNotIn("could not be read", empty)



class AnImageThatReadsEmptyOnce(unittest.TestCase):
    """A blank read must not undo an identification that already happened.

    Reported from hardware: Minecraft identified as BLES01976 on one scan and
    on the next came back as "read and has nothing in it that names a game",
    and the row disappeared. Two faults met there. The read came back short
    and was treated as an answer, and the merge let that answer remove a title
    the store already held.
    """

    def images(self, *names):
        # A realistic size: the volume descriptors live at 0x8000, so an entry
        # claiming to be smaller than that is never read at all.
        return [{"name": name, "kind": "file", "size": 4 * 1024 ** 3,
                 "device": "dev_hdd0", "folder": "PS3ISO"} for name in names]

    def test_a_short_read_is_not_an_answer_about_the_image(self):
        # What did arrive parses to nothing, and that used to be recorded as
        # a successful read of an image with no game in it.
        from ps3diag import isoreader

        class HalfAnswers:
            def read(self, path, offset, length):
                return b"\x00" * (length // 2)

        ranges = isoreader._BudgetedRanges(
            HalfAnswers(), "/dev_hdd0/PS3ISO/Minecraft.iso", 4096,
            isoreader.DEFAULT_FILE_BUDGET, lambda count: True, size=1 << 20)
        ranges(0, 4096)
        self.assertTrue(ranges.short)
        self.assertIsNone(ranges.refused)

    def test_a_read_that_stopped_is_reported_as_unread(self):
        from ps3diag import isoreader

        class HalfAnswers:
            def read(self, path, offset, length):
                return b"\x00" * (length // 2)

        payload = isoreader.identify_isos(
            self.images("Minecraft.iso"), HalfAnswers())
        row = payload["isos"][0]
        self.assertFalse(row["opened"])
        self.assertIn("did not arrive", row["reason"])

    def test_a_pass_that_read_nothing_useful_may_not_remove_a_title(self):
        # The earlier identification is better evidence than the later blank.
        previous = [updates.TitleUpdate(title_id="BLES01976",
                                        name="Minecraft",
                                        found_in=updates.FROM_DISC_IMAGE)]
        merged = updates.merge_scan(previous, [],
                                    looked_in=[updates.FROM_GAME_FOLDER])
        self.assertEqual([row.title_id for row in merged], ["BLES01976"])

    def test_a_scan_says_whether_its_image_pass_concluded_anything(self):
        lister = FakeLister(listings={PS3ISO: files("Minecraft.iso")})
        for rows, conclusive in (
                ([{"name": "Minecraft.iso", "opened": True,
                   "title_id": None}], False),
                ([{"name": "Minecraft.iso", "opened": False,
                   "title_id": None}], False),
                ([{"name": "Minecraft.iso", "opened": True,
                   "title_id": GT5}], True)):
            with self.subTest(conclusive=conclusive):
                verdict = {}
                updates.scan_console(
                    lister, image_identifier=lambda entries, rows=rows: rows,
                    verdict=verdict)
                self.assertEqual(verdict["images_conclusive"], conclusive)

    def test_a_scan_with_no_image_pass_concludes_nothing_about_images(self):
        lister = FakeLister(listings={PS3ISO: files("Minecraft.iso")})
        verdict = {}
        updates.scan_console(lister, verdict=verdict)
        self.assertFalse(verdict["images_conclusive"])


if __name__ == "__main__":
    unittest.main()


# --- the screen -------------------------------------------------------------
#
# Built for real and driven. Most of what goes wrong in a Qt screen goes wrong
# at construction or in the wiring between a worker and the widgets, and none
# of that shows up in a test of the layers underneath.

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                               # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

from ps3tools.shell.screen import (ConnectionState, Services,  # noqa: E402
                                   THEME_TOKENS, Theme)
from ps3tools.screens import gameupdates                    # noqa: E402

APP = QApplication.instance() or QApplication([])

COLOURS = {token: "#%06x" % (0x010203 + index * 0x111111)
           for index, token in enumerate(THEME_TOKENS)}


class StubTheme(Theme):
    def colour(self, token):
        return COLOURS[token]

    @property
    def dark(self):
        return False


class ExplodingActions:
    def install_package(self, filename):
        raise AssertionError("the console was asked to install something")


class RecordingActions:
    """Stands in for ConsoleActions. Records the call; opens no socket.

    `on_install` is what the console does when an install succeeds. A real
    one deletes the package and the game starts reporting the new version,
    and the queue reads both of those back, so a fake console that did
    neither would leave every screen test timing out.
    """

    def __init__(self, status=200, on_install=None):
        self.calls = 0
        self.status = status
        self.installed = []
        self.on_install = on_install

    def install_package(self, filename):
        from ps3tools.consoleactions import (ActionFailed, ActionResponse,
                                             install_path)
        self.calls += 1
        # Through the real builder, so a test cannot hand this a name the
        # allowlist would have refused.
        path = install_path(filename)
        self.installed.append(filename)
        if self.status != 200:
            # What the real client does with anything but a 200. A stub that
            # returned the response instead let a refused install read as a
            # successful one.
            raise ActionFailed(
                f"The console answered {self.status} when it was asked to "
                f"install {filename}. The file has been copied across and is "
                f"still there: you can install it yourself from the console, "
                f"under Package Manager.")
        if self.on_install is not None:
            self.on_install(filename)
        return ActionResponse(path, 200, "ok")


class ScreenCase(unittest.TestCase):
    """One screen, every seam filled, nothing on the wire."""

    #: Shaped like a real package: payload plus the 32-byte footer. The
    #: manifest below publishes the digest of the payload alone, as Sony's
    #: does, so the screen test verifies the same way the console would.
    BODY = signed_pkg(b"PKG" * 2048)

    def setUp(self):
        # A worker that raises writes a crash log beside the exe or into the
        # user's home folder, which is right in the program and wrong in a test
        # suite that deliberately drives half a dozen failures.
        patcher = mock.patch("ps3tools.crashreport.handle",
                             lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, listings=None, blobs=None, manifests=None, writer=None,
              actions=None, stream=None, devices=None, host="127.0.0.1",
              installs_version="01.19"):
        connection = ConnectionState(host)
        services = Services(connection, StubTheme(), {})
        self.addCleanup(services.wait)

        lister = FakeLister(listings=listings, blobs=blobs)
        self.lister = lister
        self.writer = writer if writer is not None else RecordingWriter()
        self.actions = actions if actions is not None else RecordingActions()
        fetcher = fetcher_for(manifests or {})
        package_stream = stream if stream is not None else stream_for(self.BODY)
        storage = devices if devices is not None else [
            {"device": "dev_hdd0", "free_bytes": 64 * 1024 ** 3}]

        # A console where installs work. The package is gone from the
        # packages folder the moment the console has finished with it, and
        # the game then reports the version that was sent, which is the pair
        # of things the queue reads back. installs_version="01.05" is the
        # console that took the package and installed nothing.
        if isinstance(self.actions, RecordingActions):
            self.actions.on_install = self._installs(installs_version)

        screen = gameupdates.GameUpdatesScreen(services)
        # No test waits on a real console: the confirmation poll is instant
        # and gives up immediately when nothing is going to appear.
        screen.install_poll_seconds = 0.0
        screen.install_timeout_seconds = 0.0
        screen._lister = lambda _host: lister
        screen._writer = lambda _host: self.writer
        screen._actions = lambda _host: self.actions
        screen._storage = lambda _host: storage
        screen._fetcher = lambda: fetcher
        screen._stream = lambda: package_stream
        screen.confirm = lambda chosen: True
        self.screen = screen
        self.services = services
        return screen

    def _installs(self, version):
        """What a console does when an install succeeds.

        Only BO2 is ever ticked in these runs: it is the one title in the
        fixture with an update to fetch.
        """
        def landed(_filename):
            if not version:
                return
            self.lister.blobs[f"{GAME}/{BO2}/PARAM.SFO"] = param_sfo(
                version, BO2)
        return landed

    def settle(self, task):
        if task is not None:
            self.services.wait(10000)
        APP.processEvents()

    def scan(self, **kwargs):
        screen = self.build(**kwargs)
        self.settle(screen.start_scan())
        return screen

    def rows(self):
        table = self.screen._table
        return [table.topLevelItem(index)
                for index in range(table.topLevelItemCount())]

    def manifest_for_body(self, body=None, title_id=BO2, version="01.19",
                          name="Call of Duty: Black Ops II", sha1=None):
        """The BO2 fixture with the size and checksum of the body under test.

        The fixture carries the real published numbers, which is what makes it
        worth having; a screen test that actually downloads has to agree with
        whatever it is downloading, so those two attributes are rewritten here
        and nothing else is.
        """
        body = self.BODY if body is None else body
        raw = manifest_bytes("BLES01717-ver.xml").decode("utf-8")
        raw = raw.replace('size="419430400"', f'size="{len(body)}"')
        raw = raw.replace(
            "2" * 40, sha1 or hashlib.sha1(body[:-32]).hexdigest())
        return raw.encode("utf-8")

    def standard(self, installed="01.05", sha1=None):
        return dict(
            listings={f"{GAME}": folders(BO2, GT5),
                      "/dev_hdd0/packages": ""},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo(installed, BO2),
                   f"{GAME}/{GT5}/PARAM.SFO": param_sfo("02.17", GT5)},
            manifests={BO2: self.manifest_for_body(sha1=sha1),
                       GT5: manifest_bytes("BCES00569-ver.xml")})


class TheScreenRegistration(unittest.TestCase):
    def test_it_registers_the_published_way(self):
        screen = gameupdates.GameUpdatesScreen
        self.assertEqual(screen.key, "updates")
        self.assertEqual(screen.title, "Game updates")
        self.assertTrue(screen.blurb.endswith("."))
        self.assertTrue(screen.tile.isalpha() and 2 <= len(screen.tile) <= 3)

    def test_the_card_sits_between_the_patchers_and_about(self):
        from ps3tools.shell.registry import screens
        import ps3tools.screens.about                        # noqa: F401
        import ps3tools.screens.patcher                      # noqa: F401
        order = {item.key: item.order for item in screens()}
        self.assertGreater(order["updates"], order["mw3"])
        self.assertLess(order["updates"], order["about"])

    def test_no_emoji_anywhere_in_the_module(self):
        with open(os.path.join(ROOT, "ps3tools/screens/gameupdates.py"),
                  encoding="utf-8") as handle:
            body = handle.read()
        offences = [char for char in body if ord(char) > 0x2100]
        self.assertEqual(offences, [])


class TheScreenScan(ScreenCase):
    def test_no_host_asks_for_one_rather_than_scanning(self):
        screen = self.build(host="")
        self.assertIsNone(screen.start_scan())
        self.assertIn("address", screen._panel_heading.text())

    def test_the_table_lists_every_game(self):
        self.scan(**self.standard())
        names = [item.text(0) for item in self.rows()]
        self.assertEqual(names, ["Gran Turismo 5",
                                 "Call of Duty: Black Ops II"])

    def test_nothing_is_ticked_when_the_list_appears(self):
        screen = self.scan(**self.standard())
        states = [item.checkState(0) for item in self.rows()
                  if item.flags() & Qt.ItemIsUserCheckable]
        self.assertTrue(states)
        self.assertTrue(all(state == Qt.Unchecked for state in states))
        self.assertEqual(screen.selected_rows(), [])
        self.assertFalse(screen._go.isEnabled())

    def test_an_up_to_date_game_cannot_be_ticked_at_all(self):
        self.scan(**self.standard())
        by_id = {item.text(1): item for item in self.rows()}
        self.assertFalse(by_id[GT5].flags() & Qt.ItemIsUserCheckable)
        self.assertTrue(by_id[BO2].flags() & Qt.ItemIsUserCheckable)

    def test_everything_up_to_date_says_so_and_offers_nothing(self):
        screen = self.scan(**self.standard(installed="01.19"))
        self.assertIn("up to date", screen._panel_heading.text().lower())
        self.assertFalse(screen._go.isEnabled())

    def test_a_game_with_no_update_ever_is_not_shown_as_a_failure(self):
        screen = self.scan(
            listings={f"{GAME}": folders("BLES00003"),
                      "/dev_hdd0/packages": ""},
            blobs={f"{GAME}/BLES00003/PARAM.SFO": param_sfo("01.00",
                                                            "BLES00003")},
            manifests={"BLES00003": b""})
        item = self.rows()[0]
        self.assertEqual(item.text(5), "No update exists")
        self.assertNotIn("error", screen._panel_body.text().lower())

    def test_a_title_with_no_param_sfo_is_still_listed(self):
        self.scan(
            listings={f"{GAME}": folders("BLES00003"),
                      "/dev_hdd0/packages": ""},
            blobs={},
            manifests={"BLES00003": manifest_bytes("BLES01717-ver.xml")})
        item = self.rows()[0]
        self.assertEqual(item.text(2), "not known")
        self.assertEqual(item.text(5), "Cannot tell")

    def test_a_packages_folder_that_is_not_empty_is_said_out_loud(self):
        screen = self.scan(
            listings={f"{GAME}": folders(BO2),
                      "/dev_hdd0/packages": files("someone-elses.pkg")},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.05", BO2)},
            manifests={BO2: manifest_bytes("BLES01717-ver.xml")})
        detail = screen._detail.text()
        self.assertIn("someone-elses.pkg", detail)
        # Installs name one file, so nothing else in that folder is touched.
        # Saying it would be installed as well was simply wrong.
        self.assertNotIn("installs everything", detail)
        self.assertIn("Nothing here installs those", detail)

    def test_a_certificate_failure_is_reported_once_with_its_own_advice(self):
        failure = ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        screen = self.scan(
            listings={f"{GAME}": folders(BO2, GT5),
                      "/dev_hdd0/packages": ""},
            blobs={},
            manifests={BO2: failure, GT5: failure})
        body = screen._panel_body.text()
        for blamed in ("antivirus", "router", "company or school"):
            self.assertNotIn(blamed, body)
        self.assertIn("could not be confirmed as Sony's", body)
        self.assertEqual(self.rows(), [])

    def test_the_bulk_tick_ticks_every_game_that_can_be_updated(self):
        # Ticking a shelf of games one at a time is where somebody gives up
        # and updates on the console instead.
        screen = self.scan(**self.standard())
        screen._all_box.setChecked(True)
        APP.processEvents()
        self.assertEqual([row.title_id for row in screen.selected_rows()],
                         [BO2])
        self.assertTrue(screen._go.isEnabled())

    def test_the_bulk_tick_leaves_the_rows_it_may_not_touch_alone(self):
        # GT5 is up to date and carries no tick box. A bulk tick that put one
        # on it would offer to download an update that does not exist.
        screen = self.scan(**self.standard())
        screen._all_box.setChecked(True)
        APP.processEvents()
        by_id = {item.text(1): item for item in self.rows()}
        self.assertFalse(by_id[GT5].flags() & Qt.ItemIsUserCheckable)
        self.assertEqual(by_id[GT5].checkState(0), Qt.Unchecked)
        self.assertEqual(by_id[BO2].checkState(0), Qt.Checked)

    def test_clearing_the_bulk_tick_unticks_everything_it_ticked(self):
        # One control for both, so there is no state it can be left in where
        # a few hundred megabytes stay ticked with no way to clear them.
        screen = self.scan(**self.standard())
        screen._all_box.setChecked(True)
        APP.processEvents()
        screen._all_box.setChecked(False)
        APP.processEvents()
        self.assertEqual(screen.selected_rows(), [])
        self.assertFalse(screen._go.isEnabled())

    def test_the_bulk_tick_follows_rows_ticked_one_at_a_time(self):
        # Every updatable row ticked by hand, with the bulk box still empty,
        # reads as a control that has stopped working.
        screen = self.scan(**self.standard())
        by_id = {item.text(1): item for item in self.rows()}
        by_id[BO2].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.assertTrue(screen._all_box.isChecked())
        by_id[BO2].setCheckState(0, Qt.Unchecked)
        APP.processEvents()
        self.assertFalse(screen._all_box.isChecked())

    def test_the_bulk_tick_is_dead_until_there_is_something_to_tick(self):
        # An enabled control that does nothing when pressed is the same
        # complaint as a scan button on a screen with no console address.
        screen = self.build(**self.standard())
        screen.on_enter()
        self.assertFalse(screen._all_box.isEnabled())
        self.settle(screen.start_scan())
        self.assertTrue(screen._all_box.isEnabled())

    def test_it_says_why_it_is_quicker_before_anything_is_pressed(self):
        # People asked why they should not simply update on the console. The
        # answer was in no version of this screen until it was put above the
        # table, where it is read before the first button is found.
        screen = self.build(**self.standard())
        screen.on_enter()
        text = screen._why_quicker.text()
        self.assertIn("newest title update", text)
        self.assertIn("every update Sony ever published", text)
        layout = screen.layout()
        self.assertLess(layout.indexOf(screen._why_quicker),
                        layout.indexOf(screen._table))
        self.assertEqual(self.lister.asked, [])


class TheScreenAndGamesWithNoUpdate(ScreenCase):
    """The new row on the screen, and the button that buys the slow path."""

    def iso_only(self, **extra):
        listings = {f"{GAME}": folders(BO2),
                    PS3ISO: files(f"Gran Turismo 5 [{GT5}].iso"),
                    "/dev_hdd0/packages": ""}
        listings.update(extra)
        return dict(
            listings=listings,
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.19", BO2)},
            manifests={BO2: self.manifest_for_body(),
                       GT5: manifest_bytes("BCES00569-ver.xml")})

    def test_an_iso_only_game_is_listed_with_none_and_can_be_ticked(self):
        screen = self.scan(**self.iso_only())
        by_id = {item.text(1): item for item in self.rows()}
        self.assertIn(GT5, by_id)
        self.assertEqual(by_id[GT5].text(2), "none")
        self.assertEqual(by_id[GT5].text(5), "Update available")
        self.assertTrue(by_id[GT5].flags() & Qt.ItemIsUserCheckable)
        by_id[GT5].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.assertTrue(screen._go.isEnabled())
        self.assertEqual([row.title_id for row in screen.selected_rows()],
                         [GT5])

    def test_it_is_not_confused_with_a_version_that_could_not_be_read(self):
        screen = self.scan(**self.iso_only(
            **{f"{GAME}": folders(BO2, "BLES00003")}))
        by_id = {item.text(1): item for item in self.rows()}
        # No PARAM.SFO for BLES00003, and no manifest for it either.
        self.assertEqual(by_id[GT5].text(2), "none")
        self.assertEqual(by_id["BLES00003"].text(2), "not known")
        self.assertNotIn("error", screen._panel_body.text().lower())

    def test_a_game_in_both_places_appears_once(self):
        self.scan(**self.iso_only(
            **{PS3ISO: files(f"Black Ops II [{BO2}].iso")}))
        self.assertEqual([item.text(1) for item in self.rows()], [BO2])

    def test_there_is_no_second_pass_to_offer_any_more(self):
        # The images are read as part of the check, so there is no button and
        # no note about one. A screen with a leftover Look inside would be
        # offering a pass that has already happened.
        screen = self.scan(**self.iso_only())
        self.assertFalse(hasattr(screen, "_look"))
        self.assertFalse(hasattr(screen, "_look_hint"))
        self.assertNotIn("Look inside", screen._detail.text())

    def test_an_unnamed_image_is_read_as_part_of_the_check(self):
        # There is no second button any more. Reading the images a few
        # connections at a time made the slow pass quick enough that offering
        # somebody an incomplete list first had nothing to recommend it.
        image = iso_of(GT5, title="Gran Turismo 5")
        identify = identifier_over({f"{PS3ISO}/Mystery Disc.iso": image})
        with mock.patch.object(updates, "parallel_image_identifier",
                               lambda *args, **kwargs: identify):
            screen = self.scan(**self.iso_only(
                **{PS3ISO: files("Mystery Disc.iso")}))
        by_id = {item.text(1): item for item in self.rows()}
        self.assertIn(GT5, by_id)
        self.assertEqual(by_id[GT5].text(2), "none")

    def test_it_says_what_it_found_and_why_the_rest_were_left_out(self):
        # Reported off hardware: thirteen images opened, one game added,
        # nothing said about the other twelve. They were PS2 games, which is
        # the right answer and looks like the pass having barely worked.
        ps2 = iso_of("SLES50916", title="Ico")
        ps3 = iso_of(GT5, title="Gran Turismo 5")
        identify = identifier_over({f"{PS3ISO}/One.iso": ps3,
                                    f"{PS3ISO}/Two.iso": ps2})
        with mock.patch.object(updates, "parallel_image_identifier",
                               lambda *args, **kwargs: identify):
            screen = self.scan(**self.iso_only(
                **{PS3ISO: files("One.iso", "Two.iso")}))
        detail = screen._detail.text()
        self.assertIn("Looked at 2 disc images", detail)
        self.assertIn("1 added to the list", detail)
        self.assertIn("PS2 or PSOne games", detail)
        self.assertIn("Two.iso", detail)


class NoImageIsOpenedByTheScreen(ScreenCase):
    """The same guard as NothingReachesARealClient, for the new slow path.

    An injectable seam is not enough on its own. Every real client the scan
    could reach is replaced with something that raises -- including
    ps3diag.isoreader, which is the one this feature added -- and then the
    whole scan is run against a console whose games all carry their IDs in
    their names. If anything opens an image, this fails here rather than
    spending four minutes on somebody's console.
    """

    def test_a_scan_of_named_games_touches_no_real_client_and_no_image(self):
        from ps3diag import transport as real_transport
        from ps3tools.patching import ftpwrite as real_ftpwrite

        def explode(*args, **kwargs):
            raise AssertionError("a real network client was constructed")

        patches = [
            mock.patch.object(updates, "isoreader", ExplodingIsoReader()),
            mock.patch.object(updates, "urllib_fetcher", explode),
            mock.patch.object(updates, "urllib_stream", explode),
            mock.patch.object(real_transport, "FtpLister", explode),
            mock.patch.object(real_transport, "HttpProbe", explode),
            mock.patch.object(real_ftpwrite, "FtpWriter", explode),
            mock.patch.object(consoleactions, "ConsoleActions", explode),
            mock.patch.object(gameupdates, "FtpWriter", explode),
            mock.patch.object(gameupdates, "ConsoleActions", explode),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        screen = self.scan(
            listings={f"{GAME}": folders(BO2),
                      GAMES: folders(*REAL_FOLDER_NAMES),
                      PS3ISO: files(f"Gran Turismo 5 [{GT5}].iso"),
                      "/dev_hdd0/packages": ""},
            blobs={f"{GAME}/{BO2}/PARAM.SFO": param_sfo("01.05", BO2)},
            manifests={BO2: self.manifest_for_body(),
                       GT5: manifest_bytes("BCES00569-ver.xml")})
        listed = {item.text(1) for item in self.rows()}
        self.assertEqual(listed,
                         {BO2, GT5, "BLES00134", "BLES01031"})
        self.assertFalse(hasattr(screen, "_look"))


class TheScreenRun(ScreenCase):
    def tick(self, title_id):
        for item in self.rows():
            if item.text(1) == title_id:
                item.setCheckState(0, Qt.Checked)

    def test_the_whole_sequence(self):
        screen = self.scan(**self.standard())
        self.tick(BO2)
        self.assertTrue(screen._go.isEnabled())
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertEqual(len(self.writer.stored), 1)
        path, body = self.writer.stored[0]
        self.assertTrue(path.startswith("/dev_hdd0/packages/"))
        self.assertEqual(body, self.BODY)
        # One install call, naming the file that was just uploaded.
        self.assertEqual(self.actions.calls, 1)
        self.assertEqual(self.actions.installed,
                         [os.path.basename(path)])
        # Confirmed on the console before anything else was fired, and the
        # package left exactly where it was put.
        self.assertIn("installed", screen._panel_heading.text().lower())
        self.assertIn("installed on the console", screen._panel_body.text())
        self.assertIn("stay in the console's packages folder",
                      screen._panel_body.text())
        self.assertEqual(self.writer.deleted, [])

    def test_the_install_is_confirmed_by_reading_the_version_back(self):
        # The package leaving the packages folder says the console has
        # finished with it. Whether the update went on is read from the
        # game's own PARAM.SFO afterwards, the same way the scan reads it.
        screen = self.scan(**self.standard())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertIn("installed", screen._panel_heading.text().lower())
        self.assertIn(f"{GAME}/{BO2}/PARAM.SFO", self.lister.blobs)

    def test_an_install_that_did_not_take_names_both_versions(self):
        # The console took the package, finished with it, and the game still
        # reports the version it had. That is a failed install and it is
        # reported as one.
        screen = self.scan(installs_version="", **self.standard())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        body = screen._panel_body.text()
        self.assertIn("did not install", screen._panel_heading.text())
        # Both of them: what was sent, and what the game reports now. The
        # console's 01.05 is shown the way the table shows it.
        self.assertIn("01.19", body)
        self.assertIn("1.05", body)

    def test_the_list_names_each_package_and_the_state_it_ended_in(self):
        # The install stage had nothing on screen at all. Every package says
        # which stage it is in, and the one that did not install stays in the
        # list saying so when the run is over.
        screen = self.scan(installs_version="", **self.standard())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        listed = screen._queue.text()
        self.assertIn("Call of Duty: Black Ops II", listed)
        self.assertIn(updates.STATE_FAILED, listed)
        self.assertTrue(screen._queue.isVisible() or screen.isHidden())

    def test_one_connection_answers_every_poll_of_the_install(self):
        # The poll runs every second for as long as the install takes, and
        # webMANftpd hangs up on a program that opens a connection a second.
        # Two are opened for a run: one for the downloads and one that the
        # whole install stage shares.
        screen = self.scan(**self.standard())
        opened = []
        real = screen._lister

        def counted(host):
            opened.append(host)
            return real(host)

        screen._lister = counted
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertEqual(len(opened), 2)

    def test_the_task_folder_is_left_alone_entirely(self):
        # /dev_hdd0/vsh/task holds one directory, 00000001, which sat there
        # through both hardware installs and never changed. Nothing here
        # reads it and nothing here decides anything from it.
        screen = self.scan(**self.standard())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertEqual([path for path in self.lister.asked
                          if "vsh" in path or "task" in path], [])

    def test_a_checksum_mismatch_stops_before_anything_is_written(self):
        # The write client raises on contact and the console action client
        # raises on contact, so this proves neither is reachable.
        options = self.standard(
            sha1=hashlib.sha1(b"a different file").hexdigest())
        screen = self.scan(writer=ExplodingWriter(),
                           actions=ExplodingActions(), **options)
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertIn("did not finish", screen._panel_heading.text())
        self.assertIn("not the file Sony published", screen._panel_body.text())

    def test_the_second_identical_failure_stops_saying_try_again(self):
        # The screen keeps the record, so pressing the button again is what
        # counts as the second attempt. Told to try again twice, somebody
        # would download several hundred megabytes for nothing.
        options = self.standard(
            sha1=hashlib.sha1(b"a different file").hexdigest())
        screen = self.scan(writer=ExplodingWriter(),
                           actions=ExplodingActions(), **options)
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertIn("worth trying once more", screen._panel_body.text())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        body = screen._panel_body.text()
        self.assertIn("will not change it", body)
        self.assertNotIn("worth trying once more", body)

    def test_a_download_that_stops_mid_stream_writes_nothing(self):
        screen = self.scan(writer=ExplodingWriter(),
                           actions=ExplodingActions(),
                           stream=stream_for(self.BODY, stop_after=32),
                           **self.standard())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertIn("did not finish", screen._panel_heading.text())

    def test_not_enough_room_on_the_console_refuses_the_whole_run(self):
        screen = self.scan(
            writer=ExplodingWriter(), actions=ExplodingActions(),
            devices=[{"device": "dev_hdd0", "free_bytes": 1024}],
            **self.standard())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertIn("not enough room", screen._panel_body.text())

    def test_a_console_that_disappears_mid_upload(self):
        screen = self.scan(
            writer=RecordingWriter(fault=ftplib.error_temp("421 goodbye")),
            actions=ExplodingActions(), **self.standard())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertIn("did not finish", screen._panel_heading.text())
        self.assertIn("switched on", screen._panel_body.text())

    def test_an_unexpected_answer_from_the_install_endpoint(self):
        # The file is on the console either way, so the user is told how to
        # finish the job by hand rather than being told it failed.
        screen = self.scan(actions=RecordingActions(status=404),
                           **self.standard())
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertEqual(len(self.writer.stored), 1)
        self.assertIn("did not install", screen._panel_heading.text())
        self.assertIn("Package Manager", screen._panel_body.text())

    def test_arriving_does_not_start_reading_the_console(self):
        # It used to scan the moment the screen opened: minutes of reading on
        # a full console before anybody had asked for it, with a window that
        # looks stuck while it happens.
        screen = self.build(**self.standard())
        screen.on_enter()
        self.settle(None)
        self.assertEqual(self.lister.asked, [])
        self.assertEqual(self.rows(), [])
        self.assertIn("Ready when you are", screen._panel_heading.text())
        self.assertIn("few minutes", screen._panel_body.text())

    def test_there_is_one_scan_button_and_not_two(self):
        # Two buttons that both started a scan, told apart only by reading
        # both labels. The second one is gone and nothing may bring it back.
        screen = self.build(**self.standard())
        self.assertFalse(hasattr(screen, "_partial"))

    def test_the_button_says_which_check_it_would_run(self):
        # A button whose label does not match what it does is how somebody
        # sits through a full check when they wanted the short one.
        screen = self.scan(**self.standard())
        self.assertEqual(screen._behind, [BO2])
        self.assertFalse(screen._full_box.isChecked())
        self.assertEqual(screen._rescan.text(),
                         "Check the 1 that was behind")
        screen._full_box.setChecked(True)
        APP.processEvents()
        self.assertEqual(screen._rescan.text(), "Check everything")
        screen._full_box.setChecked(False)
        APP.processEvents()
        self.assertEqual(screen._rescan.text(),
                         "Check the 1 that was behind")

    def test_the_button_runs_the_check_its_label_names(self):
        # The tick box and the button are wired separately, so the label can
        # agree with the box while the press does something else. That way
        # round, somebody asking for seconds gets minutes of console reading.
        screen = self.scan(**self.standard())
        everything = sorted(item.text(1) for item in self.rows())
        before = len(self.lister.asked)
        screen._rescan.click()
        self.settle(screen._task)
        # The short check asks Sony again without walking the console.
        self.assertEqual(len(self.lister.asked) - before, 1)
        self.assertEqual(sorted(item.text(1) for item in self.rows()),
                         everything)
        screen._full_box.setChecked(True)
        APP.processEvents()
        before = len(self.lister.asked)
        screen._rescan.click()
        self.settle(screen._task)
        self.assertGreater(len(self.lister.asked) - before, 1)

    def test_with_nothing_behind_the_full_check_is_the_only_offer(self):
        # Nothing has been checked yet, so there is no shorter check to run.
        # An empty short check would have looked at no games and said so.
        screen = self.build(**self.standard())
        screen.on_enter()
        self.assertTrue(screen._full_box.isChecked())
        self.assertFalse(screen._full_box.isEnabled())
        self.assertEqual(screen._rescan.text(), "Check everything")
        self.assertTrue(screen._rescan.property("primary"))
        self.settle(screen.start_scan())
        self.assertFalse(screen._rescan.property("primary"))

    def test_everything_up_to_date_takes_the_short_check_away_again(self):
        # The box was ticked and locked on arrival. Once a check leaves
        # nothing behind it has to stay that way, rather than offering a
        # short check over the empty list the last one produced.
        screen = self.scan(**self.standard(installed="01.19"))
        self.assertEqual(screen._behind, [])
        self.assertTrue(screen._full_box.isChecked())
        self.assertFalse(screen._full_box.isEnabled())
        self.assertEqual(screen._rescan.text(), "Check everything")

    def test_a_stage_message_reaches_the_status_line(self):
        screen = self.build(**self.standard())
        screen._on_progress(("stage", "Reading the games installed"))
        self.assertEqual(screen._stage.text(), "Reading the games installed")

    def test_arriving_again_shows_the_last_check_and_offers_a_short_one(self):
        screen = self.scan(**self.standard())
        settings = screen.services.settings
        self.assertTrue(updates.remembered_scan(settings, "127.0.0.1"))

        # A new screen over the same settings: what was found is on screen
        # before the console has been touched.
        again = self.build(**self.standard())
        again.services.settings.update(settings)
        again.on_enter()
        self.assertEqual(self.lister.asked, [])
        self.assertIn("last check", again._panel_heading.text().lower())
        # The short check is what the button offers, and the box beside it is
        # the way to the full one.
        self.assertEqual(again._rescan.text(), "Check the 1 that was behind")
        self.assertFalse(again._full_box.isChecked())
        self.assertTrue(again._full_box.isEnabled())
        self.assertIn("Include every game", again._panel_body.text())
        # Nothing remembered may be acted on.
        for item in self.rows():
            with self.subTest(item.text(1)):
                self.assertFalse(item.flags() & Qt.ItemIsUserCheckable)
        self.assertFalse(again._go.isEnabled())

    def test_the_short_check_updates_its_titles_and_leaves_the_rest(self):
        # A partial scan speaks only for the titles it was asked about. It
        # used to replace the table with them, which took every other game off
        # the screen although nothing had happened to them.
        screen = self.scan(**self.standard())
        everything = sorted(item.text(1) for item in self.rows())
        behind = list(screen._behind)
        self.assertEqual(behind, [BO2])
        before = len(self.lister.asked)
        self.settle(screen.start_scan(only=behind))
        # It asks Sony again, and it does not walk the console to do it.
        self.assertEqual(len(self.lister.asked) - before, 1)
        self.assertEqual(sorted(item.text(1) for item in self.rows()),
                         everything)

    def test_only_what_was_uploaded_is_ever_installed(self):
        # The packages folder may hold something the user put there. It is
        # not ours to run, and the only guard against a later
        # "simplification" into a folder sweep is a test that says so.
        options = self.standard()
        options["listings"]["/dev_hdd0/packages"] = files("someone-elses.pkg")
        screen = self.scan(**options)
        self.tick(BO2)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertEqual(len(self.actions.installed), 1)
        self.assertNotIn("someone-elses.pkg", self.actions.installed)
        self.assertEqual(self.actions.installed[0],
                         os.path.basename(self.writer.stored[0][0]))

    def test_it_will_not_leave_while_an_upload_is_in_flight(self):
        screen = self.scan(**self.standard())
        screen._working = True
        self.assertFalse(screen.can_leave())
        self.assertIn("packages folder", screen.leave_blocked_reason())
        screen._working = False
        self.assertTrue(screen.can_leave())


class NothingReachesARealClient(ScreenCase):
    """The regression this whole arrangement exists to stop.

    A feature was added once that made a previously inert code path live inside
    the tests, and the suite started making real requests to addresses on this
    LAN. An injectable seam is not on its own enough: this replaces every real
    client the feature could possibly reach -- Sony's manifest fetcher, Sony's
    package stream, the FTP lister, the write client and the console action
    client -- with something that raises, and then runs the complete flow.

    If any of them is reached, this fails here rather than on somebody's
    console.
    """

    def test_the_full_flow_never_touches_a_real_client(self):
        from ps3diag import transport as real_transport
        from ps3tools.patching import ftpwrite as real_ftpwrite

        def explode(*args, **kwargs):
            raise AssertionError("a real network client was constructed")

        patches = [
            mock.patch.object(updates, "urllib_fetcher", explode),
            mock.patch.object(updates, "urllib_stream", explode),
            mock.patch.object(real_transport, "FtpLister", explode),
            mock.patch.object(real_transport, "HttpProbe", explode),
            mock.patch.object(real_ftpwrite, "FtpWriter", explode),
            mock.patch.object(consoleactions, "ConsoleActions", explode),
            mock.patch.object(gameupdates, "FtpWriter", explode),
            mock.patch.object(gameupdates, "ConsoleActions", explode),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        screen = self.scan(**self.standard())
        for item in self.rows():
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(0, Qt.Checked)
        self.settle(screen.start_run(screen.selected_rows()))
        self.assertEqual(len(self.writer.stored), 1)
        self.assertEqual(self.actions.calls, 1)
        self.assertIn("install", screen._panel_heading.text().lower())

    def test_the_defaults_are_looked_up_late_enough_to_be_patchable(self):
        # If these were bound at class definition time the guard above would
        # pass while proving nothing: the screen would be holding a reference
        # to the real client that no patch of ps3tools.updates could reach.
        connection = ConnectionState("127.0.0.1")
        services = Services(connection, StubTheme(), {})
        self.addCleanup(services.wait)
        screen = gameupdates.GameUpdatesScreen(services)
        marker = object()
        with mock.patch.object(updates, "urllib_fetcher", marker):
            self.assertIs(screen._fetcher(), marker)
        with mock.patch.object(updates, "urllib_stream", marker):
            self.assertIs(screen._stream(), marker)


# --- arriving from the patcher ----------------------------------------------

class ThePreselect(ScreenCase):
    """The patcher hands somebody over when their game is the wrong version.

    They did not come here to browse. The row they were sent for is ticked for
    them, and nothing else about the screen changes: no download starts and
    nothing on the console is touched.
    """

    def tickable(self):
        return {item.text(1): item for item in self.rows()
                if item.flags() & Qt.ItemIsUserCheckable}

    def test_a_request_made_before_the_scan_is_applied_when_rows_land(self):
        # The shell calls preselect and then navigates, and this screen scans
        # on entry, so there are no rows at the moment it is asked.
        screen = self.build(**self.standard())
        screen.preselect("bo2")
        self.settle(screen.start_scan())
        self.assertEqual([row.title_id for row in screen.selected_rows()],
                         [BO2])
        self.assertTrue(screen._go.isEnabled())

    def test_a_request_made_after_the_scan_is_applied_at_once(self):
        screen = self.scan(**self.standard())
        self.assertEqual(screen.selected_rows(), [])
        screen.preselect("bo2")
        self.assertEqual([row.title_id for row in screen.selected_rows()],
                         [BO2])

    def test_it_says_why_the_row_is_ticked(self):
        screen = self.build(**self.standard())
        screen.preselect("bo2")
        self.settle(screen.start_scan())
        self.assertIn("proved against", screen._detail.text())

    def test_an_unrecognised_subject_is_ignored_quietly(self):
        screen = self.build(**self.standard())
        for subject in ("", None, "mw2", "BLES01717", 7):
            self.assertIsNone(screen.preselect(subject))
        self.settle(screen.start_scan())
        self.assertEqual(screen.selected_rows(), [])

    def test_it_ticks_nothing_and_says_so_when_there_is_no_update_to_offer(self):
        screen = self.build(**self.standard(installed="01.19"))
        screen.preselect("bo2")
        self.settle(screen.start_scan())
        self.assertEqual(screen.selected_rows(), [])
        self.assertIn("no newer title update", screen._detail.text())

    def test_it_starts_nothing_and_writes_nothing_by_itself(self):
        screen = self.build(writer=ExplodingWriter(),
                            actions=ExplodingActions(), **self.standard())
        screen.preselect("bo2")
        self.settle(screen.start_scan())
        self.assertFalse(screen._working)
        self.assertTrue(screen.can_leave())

    def test_a_game_that_is_not_installed_ticks_nothing(self):
        screen = self.build(
            listings={f"{GAME}": folders(GT5), "/dev_hdd0/packages": ""},
            blobs={f"{GAME}/{GT5}/PARAM.SFO": param_sfo("01.00", GT5)},
            manifests={GT5: manifest_bytes("BCES00569-ver.xml")})
        screen.preselect("mw3")
        self.settle(screen.start_scan())
        self.assertEqual(screen.selected_rows(), [])


# --- the mock console, over loopback ----------------------------------------

class AgainstTheMockConsole(unittest.TestCase):
    """The packages folder and the install call, through the real clients.

    Everything here is the mock in tests/mock_webman.py, bound to 127.0.0.1
    explicitly. It is worth doing once with the real transport and the real
    action client rather than only with stand-ins: the stand-ins agree with
    whatever this program expects, and a mock that answers like the hardware
    does not.
    """

    def ftp_lister(self, **kwargs):
        from mock_webman import MockWebmanFtp
        from ps3diag.transport import FtpLister
        server = MockWebmanFtp(**kwargs).start()
        self.addCleanup(server.stop)
        port = server.port

        def factory():
            ftp = ftplib.FTP()
            ftp.connect("127.0.0.1", port, timeout=5)
            ftp.login("anonymous", "ps3-diag@localhost")
            return ftp

        lister = FtpLister("127.0.0.1", 5, factory=factory)
        self.addCleanup(lister.close)
        return lister

    def test_an_empty_packages_folder_reads_as_empty(self):
        folder = updates.inspect_packages_folder(self.ftp_lister())
        self.assertTrue(folder.empty)

    def test_somebody_elses_package_already_in_the_folder_is_found(self):
        from mock_webman import DEFAULT_LISTINGS
        listings = dict(DEFAULT_LISTINGS)
        listings["/dev_hdd0/packages/"] = ("ftp",
                                           "list_packages_stranger.txt")
        folder = updates.inspect_packages_folder(
            self.ftp_lister(listings=listings))
        self.assertEqual(folder.names, ["SomeoneElsesHomebrew.pkg"])
        self.assertFalse(folder.empty)

    def test_the_install_call_against_the_mocks_own_route(self):
        from mock_webman import MockWebmanHttp
        from ps3tools.consoleactions import ConsoleActions
        with MockWebmanHttp() as server:
            response = updates.install(ConsoleActions(server.address),
                                       "patch.pkg")
        self.assertTrue(response.ok)
        self.assertEqual(
            server.requests,
            [("GET", "/install.ps3/dev_hdd0/packages/patch.pkg")])
