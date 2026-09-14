"""The allowlist on the client that can ask a console to act.

Most of this file is about paths that must be refused rather than paths that
work, and that is the right proportion. webMAN does everything over GET on one
URL space, and two of the most destructive things it can be asked to do sit one
path segment away from the only call this program makes:

    /install.ps3/dev_hdd0/packages   the one this program needs
    /recovery.ps3                    boots the console into recovery
    /rebuild.ps3                     rebuilds the database

A prefix test on "/install" would accept all three of those and a good deal
more. So the allowlist is exact match against complete paths, and the tests
below are what stops somebody turning it back into a prefix test because a
fourth endpoint would be convenient.

Nothing here leaves loopback. The mock HTTP server binds to 127.0.0.1
explicitly and every other test injects an opener of its own.
"""

import unittest
import urllib.error

from support import ROOT  # noqa: F401  (puts the project on sys.path)

from mock_webman import MockWebmanHttp

from ps3tools import consoleactions
from ps3tools.consoleactions import (ActionFailed, ActionRefused,
                                     ConsoleActions, assert_allowed)

# The route the coordinator is adding to tests/mock_webman.py. It is passed in
# explicitly here so this file does not depend on that landing first; when it
# lands, DEFAULT_ROUTES gains the same path and nothing here has to change.
INSTALL_ROUTES = {
    "/install.ps3/dev_hdd0/packages": b"<html><body>Installing</body></html>",
}


class FakeResponse:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingOpener:
    """Stands in for urllib. Records every URL and never opens a socket."""

    def __init__(self, status=200, body=b"ok", raises=None):
        self.urls = []
        self.status = status
        self.body = body
        self.raises = raises

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        if self.raises is not None:
            raise self.raises
        return FakeResponse(self.status, self.body)


class ExplodingOpener:
    """Anything that touches the network fails the test on the spot."""

    def open(self, request, timeout=None):
        raise AssertionError(f"a request was made: {request.full_url}")


# --- the allowlist ---------------------------------------------------------

class TheAllowlist(unittest.TestCase):
    def test_it_holds_exactly_one_path(self):
        # A second entry is a decision somebody makes on purpose. If this fails
        # because one was added, read consoleactions.py before changing it.
        self.assertEqual(sorted(consoleactions.ALLOWED_ACTIONS),
                         ["/install.ps3/dev_hdd0/packages"])

    def test_the_install_path_is_allowed(self):
        self.assertEqual(assert_allowed("/install.ps3/dev_hdd0/packages"),
                         "/install.ps3/dev_hdd0/packages")

    def test_recovery_is_refused(self):
        with self.assertRaises(ActionRefused):
            assert_allowed("/recovery.ps3")

    def test_rebuild_is_refused(self):
        with self.assertRaises(ActionRefused):
            assert_allowed("/rebuild.ps3")

    def test_install_with_a_different_tail_is_refused(self):
        # The endpoint takes the folder as part of the path, so a prefix test
        # on "/install.ps3" would install whatever came after it.
        for path in ("/install.ps3/dev_hdd0",
                     "/install.ps3/dev_hdd0/packages/",
                     "/install.ps3/dev_usb000/packages",
                     "/install.ps3/dev_flash",
                     "/install.ps3"):
            with self.subTest(path=path), self.assertRaises(ActionRefused):
                assert_allowed(path)

    def test_a_query_string_is_refused(self):
        for path in ("/install.ps3/dev_hdd0/packages?x=1",
                     "/install.ps3/dev_hdd0/packages#x",
                     "/install.ps3/dev_hdd0/packages&recovery.ps3"):
            with self.subTest(path=path), self.assertRaises(ActionRefused):
                assert_allowed(path)

    def test_path_traversal_is_refused(self):
        for path in ("/install.ps3/dev_hdd0/packages/../../recovery.ps3",
                     "/install.ps3/../recovery.ps3",
                     "/install.ps3/dev_hdd0/../../rebuild.ps3"):
            with self.subTest(path=path), self.assertRaises(ActionRefused):
                assert_allowed(path)

    def test_a_case_variation_is_refused(self):
        # Exact match means exact. webMAN's own casing is the one on the list.
        with self.assertRaises(ActionRefused):
            assert_allowed("/INSTALL.PS3/dev_hdd0/packages")

    def test_something_that_is_not_a_path_is_refused(self):
        for path in ("", "install.ps3/dev_hdd0/packages", None, 7,
                     "http://192.168.1.10/install.ps3/dev_hdd0/packages"):
            with self.subTest(path=path), self.assertRaises(ActionRefused):
                assert_allowed(path)

    def test_a_refused_path_never_reaches_the_opener(self):
        client = ConsoleActions("127.0.0.1:1", opener=ExplodingOpener())
        for path in ("/recovery.ps3", "/rebuild.ps3", "/install.ps3"):
            with self.subTest(path=path), self.assertRaises(ActionRefused):
                client._get(path)
        self.assertEqual(client.requests, [])


# --- the call itself -------------------------------------------------------

class TheInstallCall(unittest.TestCase):
    def test_it_asks_for_the_one_allowed_path(self):
        opener = RecordingOpener()
        client = ConsoleActions("127.0.0.1:8080", opener=opener)
        response = client.install_packages()
        self.assertTrue(response.ok)
        self.assertEqual(
            opener.urls,
            ["http://127.0.0.1:8080/install.ps3/dev_hdd0/packages"])

    def test_it_works_against_the_mock_console(self):
        with MockWebmanHttp(routes=INSTALL_ROUTES) as server:
            client = ConsoleActions(server.address)
            response = client.install_packages()
        self.assertEqual(response.status, 200)
        self.assertIn("Installing", response.body)
        self.assertEqual(server.requests,
                         [("GET", "/install.ps3/dev_hdd0/packages")])

    def test_an_unexpected_status_is_a_failure_with_advice(self):
        # webMAN answering 404 means this build has no such endpoint. The file
        # is already on the console, so the user is told how to finish by hand.
        opener = RecordingOpener(status=404, body=b"not found")
        client = ConsoleActions("127.0.0.1:8080", opener=opener)
        with self.assertRaises(ActionFailed) as caught:
            client.install_packages()
        self.assertIn("Package Manager", str(caught.exception))

    def test_an_http_error_comes_back_as_the_status_it_was(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1/install.ps3/dev_hdd0/packages", 500,
            "Internal Server Error", {}, None)
        client = ConsoleActions("127.0.0.1:8080",
                                opener=RecordingOpener(raises=error))
        with self.assertRaises(ActionFailed) as caught:
            client.install_packages()
        self.assertIn("500", str(caught.exception))

    def test_a_console_that_vanished_says_what_to_check(self):
        error = urllib.error.URLError("connection refused")
        client = ConsoleActions("127.0.0.1:8080",
                                opener=RecordingOpener(raises=error))
        with self.assertRaises(ActionFailed) as caught:
            client.install_packages()
        self.assertIn("switched on", str(caught.exception))

    def test_no_host_is_refused_before_anything_is_built(self):
        client = ConsoleActions("", opener=ExplodingOpener())
        with self.assertRaises(ActionFailed):
            client.install_packages()

    def test_html_that_is_not_a_success_is_not_treated_as_one(self):
        # A 200 whose body is webMAN's 404 page is still a 200; this client
        # reports the status and lets the flow decide, rather than reading the
        # page and guessing at what it means.
        opener = RecordingOpener(status=200, body=b"<html>404</html>")
        response = ConsoleActions("127.0.0.1:1",
                                  opener=opener).install_packages()
        self.assertTrue(response.ok)
        self.assertIn("404", response.body)


class TheModuleItself(unittest.TestCase):
    def test_it_names_the_install_call_as_untested(self):
        # The one thing in this program that nobody has ever fired at a real
        # console must say so where a reader will find it.
        text = ConsoleActions.install_packages.__doc__ or ""
        self.assertIn("UNTESTED", text)


if __name__ == "__main__":
    unittest.main()
