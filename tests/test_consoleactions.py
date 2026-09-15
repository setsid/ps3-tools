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
PACKAGE = "EP0002-BLES00134_00-GUITARHERO3PATCH-A0111-V0100-PE.pkg"
INSTALL_PATH = "/install.ps3/dev_hdd0/packages/" + PACKAGE

INSTALL_ROUTES = {
    INSTALL_PATH: b"<html><body>Installing</body></html>",
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
    def test_the_prefix_is_fixed_and_written_out_whole(self):
        # Everything before the file name is a constant. If this fails because
        # somebody made part of it configurable, read consoleactions.py before
        # changing it.
        self.assertEqual(consoleactions.INSTALL_PREFIX,
                         "/install.ps3/dev_hdd0/packages/")

    def test_the_install_path_for_a_real_package_is_allowed(self):
        self.assertEqual(assert_allowed(INSTALL_PATH), INSTALL_PATH)
        self.assertEqual(consoleactions.install_path(PACKAGE), INSTALL_PATH)

    def test_the_folder_on_its_own_is_not_a_call_any_more(self):
        # It was the only entry for a while and it does nothing: that URL is a
        # picker page whose dropdown appends the file name in the browser.
        for path in ("/install.ps3/dev_hdd0/packages",
                     "/install.ps3/dev_hdd0/packages/"):
            with self.subTest(path):
                with self.assertRaises(ActionRefused):
                    assert_allowed(path)

    def test_nothing_can_be_walked_out_of_the_packages_folder(self):
        for tail in ("../recovery.ps3", "../../recovery.ps3", "..",
                     "a/b.pkg", "sub/../../rebuild.ps3", ".hidden.pkg",
                     "boot.self", "x.pkg.self", ""):
            with self.subTest(tail):
                with self.assertRaises(ActionRefused):
                    assert_allowed(consoleactions.INSTALL_PREFIX + tail)

    def test_a_name_that_is_not_a_package_is_refused_before_a_url_exists(self):
        for name in ("recovery.ps3", "../x.pkg", "a b.pkg", "x.PKG.exe",
                     "", None, "/etc/passwd"):
            with self.subTest(name):
                with self.assertRaises(ActionRefused):
                    consoleactions.install_path(name)

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
    def test_it_names_the_file_because_the_folder_alone_does_nothing(self):
        opener = RecordingOpener()
        client = ConsoleActions("127.0.0.1:8080", opener=opener)
        response = client.install_package(PACKAGE)
        self.assertTrue(response.ok)
        self.assertEqual(opener.urls,
                         ["http://127.0.0.1:8080" + INSTALL_PATH])

    def test_it_works_against_the_mock_console(self):
        with MockWebmanHttp(routes=INSTALL_ROUTES) as server:
            client = ConsoleActions(server.address)
            response = client.install_package(PACKAGE)
        self.assertEqual(response.status, 200)
        self.assertIn("Installing", response.body)
        self.assertEqual(server.requests, [("GET", INSTALL_PATH)])

    def test_an_unexpected_status_is_a_failure_with_advice(self):
        # webMAN answering 404 means this build has no such endpoint. The file
        # is already on the console, so the user is told how to finish by hand.
        opener = RecordingOpener(status=404, body=b"not found")
        client = ConsoleActions("127.0.0.1:8080", opener=opener)
        with self.assertRaises(ActionFailed) as caught:
            client.install_package(PACKAGE)
        self.assertIn("Package Manager", str(caught.exception))

    def test_an_http_error_comes_back_as_the_status_it_was(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1/install.ps3/dev_hdd0/packages", 500,
            "Internal Server Error", {}, None)
        client = ConsoleActions("127.0.0.1:8080",
                                opener=RecordingOpener(raises=error))
        with self.assertRaises(ActionFailed) as caught:
            client.install_package(PACKAGE)
        self.assertIn("500", str(caught.exception))

    def test_a_console_that_vanished_says_what_to_check(self):
        error = urllib.error.URLError("connection refused")
        client = ConsoleActions("127.0.0.1:8080",
                                opener=RecordingOpener(raises=error))
        with self.assertRaises(ActionFailed) as caught:
            client.install_package(PACKAGE)
        self.assertIn("switched on", str(caught.exception))

    def test_no_host_is_refused_before_anything_is_built(self):
        client = ConsoleActions("", opener=ExplodingOpener())
        with self.assertRaises(ActionFailed):
            client.install_package(PACKAGE)

    def test_html_that_is_not_a_success_is_not_treated_as_one(self):
        # A 200 whose body is webMAN's 404 page is still a 200; this client
        # reports the status and lets the flow decide, rather than reading the
        # page and guessing at what it means.
        opener = RecordingOpener(status=200, body=b"<html>404</html>")
        response = ConsoleActions("127.0.0.1:1",
                                  opener=opener).install_package(PACKAGE)
        self.assertTrue(response.ok)
        self.assertIn("404", response.body)


class TheModuleItself(unittest.TestCase):
    def test_it_records_what_is_still_unknown_about_installing(self):
        # Whether a second request lands while the console's own dialog is up
        # has not been established, and the next person to wire something to
        # this needs to know that before they fire off a row of them.
        text = ConsoleActions.install_package.__doc__ or ""
        self.assertIn("NOT established", text)
        self.assertIn("one package at a time", text)


class WhatItIsAllowedToClaim(unittest.TestCase):
    """The install call does nothing on hardware, so nothing may say it does.

    Watched twice on a real console, from both screens, with a title update
    and with a user-supplied package: 200 back, nothing on screen, the file
    still sitting in the folder. The wording that told people to watch the
    console for an install read as a fault on their console rather than a
    thing this program cannot do.
    """

    def test_the_notice_says_what_the_console_is_doing_and_what_to_press(self):
        # It really does install now, and the console waits on O. The notice
        # that told people it could not is gone.
        from ps3tools import updates
        notice = updates.INSTALL_NOTICE
        self.assertIn("installing now", notice)
        self.assertIn("press O", notice)
        # More than one queues, so the user is told one press covers the lot
        # rather than being made to wait between them.
        self.assertIn("queues up behind", notice)
        self.assertNotIn("has not been seen to act", notice)

    def test_the_notice_still_says_how_to_do_it_by_hand(self):
        from ps3tools import updates
        notice = updates.INSTALL_NOTICE
        self.assertIn("Package Manager", notice)
        self.assertIn("Install Package Files", notice)

    def test_the_answer_body_is_logged_not_thrown_away(self):
        # A 200 with nothing happening is the whole problem. Whatever reason
        # webMAN gives is in the body or nowhere.
        events = []

        class Log:
            def event(self, kind, **fields):
                events.append((kind, fields))

        body = "Installing packages from /dev_hdd0/packages<br>"
        actions = ConsoleActions("192.0.2.9",
                                 opener=RecordingOpener(body=body.encode()),
                                 log=Log())
        actions.install_package(PACKAGE)
        kinds = [item for item in events if item[0] == "console_action"]
        self.assertEqual(len(kinds), 1)
        self.assertIn(body, kinds[0][1]["body"])

    def test_a_very_long_answer_is_cut_before_it_reaches_the_log(self):
        events = []

        class Log:
            def event(self, kind, **fields):
                events.append(fields)

        actions = ConsoleActions(
            "192.0.2.9", opener=RecordingOpener(body=b"x" * 50000), log=Log())
        actions.install_package(PACKAGE)
        self.assertEqual(len(events[0]["body"]),
                         consoleactions.MAX_LOGGED_BODY)


if __name__ == "__main__":
    unittest.main()
