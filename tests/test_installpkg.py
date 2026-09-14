"""The install packages card: the user's own files, onto their own console.

Two things are load bearing here and both are tested rather than assumed.

The first is that this card and the game updates card share one implementation
of everything they have in common -- the packages folder listing, the free
space check, the upload and the install call. Two code paths that write a
package to somebody's console is exactly the duplication that ends with one of
them quietly missing a check the other has.

The second is that this screen never pretends to verify anything. It reads the
header of a chosen file so that somebody who picked the wrong one finds out
before it is on their console, and that is all it is. There is a test below
that reads the screen's own text and fails if the word "verified" has crept
into it.

Nothing here touches the network. Every seam is injected and there is a guard
test that replaces the real clients with something that raises and then runs
the whole flow.
"""

import ftplib
import os
import tempfile
import time
import unittest
import unittest.mock as mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys                                                  # noqa: E402

from support import fixture_path                            # noqa: E402

# The package fixture generator lives beside the other fixtures and builds its
# data in memory, the way the ISO ones do. support.py puts the ISO generators
# on the path; this one is this file's business.
_GENERATORS = fixture_path("packages")
if _GENERATORS not in sys.path:
    sys.path.insert(0, _GENERATORS)

import make_packages                                        # noqa: E402

from PySide6.QtCore import Qt                               # noqa: E402
from PySide6.QtWidgets import QApplication                  # noqa: E402

from ps3diag import transport as real_transport             # noqa: E402
from ps3tools import consoleactions, updates                # noqa: E402
from ps3tools.patching import ftpwrite as real_ftpwrite     # noqa: E402
from ps3tools.screens import installpkg                     # noqa: E402
from ps3tools.shell.screen import (ConnectionState, Services,  # noqa: E402
                                   THEME_TOKENS, Theme)

from test_updates import (ExplodingWriter, FakeLister,      # noqa: E402
                          RecordingActions, RecordingWriter, files)

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
    def install_packages(self):
        raise AssertionError("the console was asked to install something")


# --- reading a file the user chose ------------------------------------------

class ReadingAPackage(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ps3-pkg-test-")
        self.addCleanup(lambda: _rmtree(self.folder))

    def write(self, name, blob):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as handle:
            handle.write(blob)
        return path

    def test_a_package_gives_up_its_content_id_and_title_id(self):
        path = self.write("update.pkg", make_packages.package())
        item = updates.read_package_file(path)
        self.assertTrue(item.is_package)
        self.assertEqual(item.content_id,
                         "EP0002-BLES01717_00-CODBLOPS2PATCH19")
        self.assertEqual(item.title_id, "BLES01717")
        self.assertEqual(item.filename, "update.pkg")
        self.assertEqual(item.size, os.path.getsize(path))

    def test_a_name_is_shown_when_the_header_carries_one(self):
        path = self.write("update.pkg",
                          make_packages.package(title="Black Ops II"))
        item = updates.read_package_file(path)
        self.assertEqual(item.title, "Black Ops II")
        self.assertEqual(item.describes_as, "Black Ops II")

    def test_a_package_with_no_name_in_it_shows_its_content_id_instead(self):
        path = self.write("update.pkg", make_packages.package())
        item = updates.read_package_file(path)
        self.assertIsNone(item.title)
        self.assertIn("BLES01717", item.describes_as)

    def test_something_that_is_not_a_package_says_so_plainly(self):
        path = self.write("holiday.jpg", make_packages.not_a_package())
        item = updates.read_package_file(path)
        self.assertFalse(item.is_package)
        self.assertIn("not a PlayStation 3 package", item.reason)

    def test_a_file_that_is_not_there_is_not_a_crash(self):
        item = updates.read_package_file(
            os.path.join(self.folder, "gone.pkg"))
        self.assertFalse(item.is_package)
        self.assertIn("moved, renamed or deleted", item.reason)

    def test_an_incomplete_package_is_refused_before_it_is_sent(self):
        path = self.write("half.pkg",
                          make_packages.package(claimed_size=900 * 1024 ** 2))
        item = updates.read_package_file(path)
        self.assertFalse(item.is_package)
        self.assertIn("incomplete", item.reason)

    def test_an_empty_file_is_not_a_package(self):
        path = self.write("empty.pkg", b"")
        item = updates.read_package_file(path)
        self.assertFalse(item.is_package)

    def test_a_content_id_that_is_not_one_is_not_turned_into_a_title_id(self):
        path = self.write("odd.pkg",
                          make_packages.package(content_id="HOMEBREW-THING"))
        item = updates.read_package_file(path)
        self.assertTrue(item.is_package)
        self.assertEqual(item.title_id, "")
        self.assertEqual(item.content_id, "HOMEBREW-THING")


# --- the shared machinery ---------------------------------------------------

class BothCardsShareOneImplementation(unittest.TestCase):
    """Stated as a test so nobody solves a future problem by forking it."""

    def test_the_upload_is_one_function(self):
        from ps3tools.screens import gameupdates
        self.assertTrue(hasattr(updates, "upload_to_packages"))
        for module in (gameupdates, installpkg):
            with open(module.__file__, encoding="utf-8") as handle:
                body = handle.read()
            self.assertNotIn(".store(", body,
                             f"{module.__name__} writes to the console "
                             f"itself instead of going through "
                             f"updates.upload_to_packages")

    def test_neither_screen_names_the_install_path_itself(self):
        from ps3tools.screens import gameupdates
        for module in (gameupdates, installpkg):
            with open(module.__file__, encoding="utf-8") as handle:
                body = handle.read()
            self.assertNotIn("install.ps3", body,
                             f"{module.__name__} builds the install path "
                             f"itself rather than calling consoleactions")

    def test_the_folder_has_one_spelling(self):
        self.assertEqual(updates.PACKAGES_DIR, consoleactions.PACKAGES_PATH)

    def test_the_upload_refuses_a_name_the_console_could_not_take(self):
        with self.assertRaises(updates.UploadFailed):
            updates.upload_to_packages("/tmp/x", ExplodingWriter(),
                                       filename="../../boot.pkg")


# --- the screen -------------------------------------------------------------

class ScreenCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("ps3tools.crashreport.handle",
                             lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = tempfile.mkdtemp(prefix="ps3-pkg-test-")
        self.addCleanup(lambda: _rmtree(self.folder))

    def write(self, name, blob=None):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as handle:
            handle.write(make_packages.package() if blob is None else blob)
        return path

    def build(self, listings=None, writer=None, actions=None, devices=None,
              host="127.0.0.1"):
        connection = ConnectionState(host)
        services = Services(connection, StubTheme(), {})
        self.addCleanup(services.wait)
        self.services = services

        self.lister = FakeLister(
            listings=listings if listings is not None
            else {"/dev_hdd0/packages": ""})
        self.writer = writer if writer is not None else RecordingWriter()
        self.actions = actions if actions is not None else RecordingActions()
        storage = devices if devices is not None else [
            {"device": "dev_hdd0", "free_bytes": 64 * 1024 ** 3}]

        screen = installpkg.InstallPackagesScreen(services)
        screen._lister = lambda _host: self.lister
        screen._writer = lambda _host: self.writer
        screen._actions = lambda _host: self.actions
        screen._storage = lambda _host: storage
        screen.confirm = lambda chosen: True
        self.screen = screen
        return screen

    def settle(self, task=None):
        """Wait for the worker AND for its result to reach the GUI thread.

        services.wait() only drains the thread pool. The task's finished signal
        is queued to the GUI thread, so a single processEvents() can return
        before the handler has run -- and worse, a handler that starts more
        work leaves a second round undelivered. That raced about one run in
        three: the panel was still empty when the assertion read it.

        Pumping until the pool has stayed idle across three passes is
        deterministic: the handler runs on the first, anything it submits is in
        flight by the second, and the third confirms nothing new arrived. The
        deadline means a genuine hang still fails rather than spinning.
        """
        deadline = time.monotonic() + 10.0
        quiet = 0
        while time.monotonic() < deadline:
            APP.processEvents()
            quiet = quiet + 1 if self.services.wait(50) else 0
            if quiet >= 3:
                return
        raise AssertionError("work did not settle within ten seconds")

    def rows(self):
        table = self.screen._table
        return [table.topLevelItem(index)
                for index in range(table.topLevelItemCount())]


class TheRegistration(unittest.TestCase):
    def test_it_registers_the_published_way(self):
        screen = installpkg.InstallPackagesScreen
        self.assertEqual(screen.key, "packages")
        self.assertTrue(screen.tile.isalpha() and 2 <= len(screen.tile) <= 3)
        self.assertTrue(screen.blurb.endswith("."))

    def test_the_two_cards_sit_next_to_each_other_before_about(self):
        from ps3tools.shell.registry import screens
        import ps3tools.screens.about                        # noqa: F401
        import ps3tools.screens.gameupdates                  # noqa: F401
        import ps3tools.screens.patcher                      # noqa: F401
        order = {item.key: item.order for item in screens()}
        self.assertGreater(order["packages"], order["updates"])
        self.assertLess(order["packages"], order["about"])

    def test_no_emoji_anywhere_in_the_module(self):
        with open(installpkg.__file__, encoding="utf-8") as handle:
            body = handle.read()
        self.assertEqual([char for char in body if ord(char) > 0x2100], [])


class WhatTheScreenSays(unittest.TestCase):
    """The wording is the safety feature on this card, so it is asserted."""

    def setUp(self):
        with open(installpkg.__file__, encoding="utf-8") as handle:
            self.body = handle.read()

    def test_it_says_it_cannot_check_a_file_the_user_supplied(self):
        self.assertIn("cannot do that for a file you supply",
                      updates.NO_WAY_TO_CHECK)
        self.assertIn("Only install packages you trust",
                      updates.NO_WAY_TO_CHECK)
        self.assertIn("NO_WAY_TO_CHECK", self.body)

    def test_nothing_on_this_screen_claims_to_verify_anything(self):
        # Not "verified", not "checked and safe", nothing that would read
        # across from the card that really does check what it downloads.
        for word in ("verified", "Verified", "verification", "guaranteed",
                     "safe to install", "known good"):
            self.assertNotIn(word, self.body, f"the screen says {word!r}")

    def test_it_does_not_hash_the_users_own_file(self):
        # A hash with nothing to compare it against is decoration that looks
        # like a guarantee.
        for word in ("sha1", "sha256", "hashlib", "checksum of"):
            self.assertNotIn(word, self.body)


class ChoosingFiles(ScreenCase):
    def test_a_chosen_package_appears_with_its_details(self):
        screen = self.build()
        screen.add_files([self.write(
            "patch.pkg", make_packages.package(title="Black Ops II"))])
        row = self.rows()[0]
        self.assertEqual(row.text(0), "patch.pkg")
        self.assertEqual(row.text(2), "BLES01717")
        self.assertEqual(row.text(3), "Black Ops II")
        self.assertEqual(row.checkState(0), Qt.Checked)
        self.assertTrue(screen._go.isEnabled())

    def test_something_that_is_not_a_package_cannot_be_ticked(self):
        screen = self.build()
        screen.add_files([self.write("holiday.jpg",
                                     make_packages.not_a_package())])
        row = self.rows()[0]
        self.assertFalse(row.flags() & Qt.ItemIsUserCheckable)
        self.assertFalse(screen._go.isEnabled())
        self.assertIn("not a PlayStation 3 package", screen._detail.text())

    def test_the_same_file_twice_is_listed_once(self):
        screen = self.build()
        path = self.write("patch.pkg")
        screen.add_files([path])
        screen.add_files([path])
        self.assertEqual(len(self.rows()), 1)

    def test_clearing_the_list_empties_it(self):
        screen = self.build()
        screen.add_files([self.write("patch.pkg")])
        screen._on_clear()
        self.assertEqual(self.rows(), [])
        self.assertFalse(screen._go.isEnabled())

    def test_the_picker_is_a_seam_and_the_button_uses_it(self):
        screen = self.build()
        path = self.write("patch.pkg")
        screen.choose_files = lambda: [path]
        screen._on_add()
        self.assertEqual(len(self.rows()), 1)


class ThePreflightListing(ScreenCase):
    def test_an_empty_packages_folder_says_nothing(self):
        screen = self.build()
        screen.check_console()
        self.settle()
        self.assertTrue(screen._panel.isHidden())

    def test_something_already_in_the_folder_is_surfaced(self):
        screen = self.build(
            listings={"/dev_hdd0/packages": files("someone-elses.pkg")})
        screen.check_console()
        self.settle()
        self.assertIn("already something", screen._panel_heading.text())
        self.assertIn("someone-elses.pkg", screen._panel_body.text())
        self.assertIn("installs everything in that folder",
                      screen._panel_body.text())

    def test_a_folder_that_cannot_be_read_is_said_rather_than_assumed_empty(self):
        screen = self.build(listings={})
        screen.check_console()
        self.settle()
        self.assertIn("could not be read", screen._panel_body.text())

    def test_no_host_asks_for_one_rather_than_listing(self):
        screen = self.build(host="")
        self.assertIsNone(screen.check_console())
        self.assertIn("address", screen._panel_heading.text())


class TheRun(ScreenCase):
    def test_the_whole_sequence(self):
        screen = self.build()
        path = self.write("patch.pkg")
        screen.add_files([path])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertEqual(len(self.writer.stored), 1)
        remote, body = self.writer.stored[0]
        self.assertEqual(remote, "/dev_hdd0/packages/patch.pkg")
        with open(path, "rb") as handle:
            self.assertEqual(body, handle.read())
        self.assertEqual(self.actions.calls, 1)
        self.assertIn("install", screen._panel_heading.text().lower())
        self.assertIn("Package Manager", screen._panel_body.text())

    def test_several_files_are_all_sent_before_the_install_is_asked_for(self):
        screen = self.build()
        screen.add_files([self.write("one.pkg"), self.write("two.pkg")])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertEqual([remote for remote, _body in self.writer.stored],
                         ["/dev_hdd0/packages/one.pkg",
                          "/dev_hdd0/packages/two.pkg"])
        self.assertEqual(self.actions.calls, 1)

    def test_a_file_that_vanished_between_picking_and_pressing(self):
        screen = self.build(writer=RecordingWriter(),
                            actions=ExplodingActions())
        path = self.write("patch.pkg")
        screen.add_files([path])
        os.remove(path)
        self.settle(screen.start_run(screen.selected_files()))
        self.assertIn("did not finish", screen._panel_heading.text())
        self.assertIn("moved, renamed or deleted", screen._panel_body.text())
        self.assertEqual(self.writer.stored, [])

    def test_a_file_that_turned_into_something_else_is_refused(self):
        screen = self.build(writer=ExplodingWriter(),
                            actions=ExplodingActions())
        path = self.write("patch.pkg")
        screen.add_files([path])
        with open(path, "wb") as handle:
            handle.write(make_packages.not_a_package())
        self.settle(screen.start_run(screen.selected_files()))
        self.assertIn("did not finish", screen._panel_heading.text())

    def test_no_free_space_refuses_before_anything_is_sent(self):
        screen = self.build(writer=ExplodingWriter(),
                            actions=ExplodingActions(),
                            devices=[{"device": "dev_hdd0",
                                      "free_bytes": 1024}])
        screen.add_files([self.write("patch.pkg")])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertIn("not enough room", screen._panel_body.text())

    def test_a_console_that_disappears_mid_upload(self):
        screen = self.build(
            writer=RecordingWriter(fault=ftplib.error_temp("421 goodbye")),
            actions=ExplodingActions())
        screen.add_files([self.write("patch.pkg")])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertIn("did not finish", screen._panel_heading.text())
        self.assertIn("patch.pkg", screen._panel_body.text())
        self.assertIn("switched on", screen._panel_body.text())

    def test_an_unexpected_answer_from_the_install_endpoint(self):
        screen = self.build(actions=RecordingActions(status=404))
        screen.add_files([self.write("patch.pkg")])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertEqual(len(self.writer.stored), 1)
        self.assertIn("not installed", screen._panel_heading.text())
        self.assertIn("Package Manager", screen._panel_body.text())

    def test_it_will_not_leave_while_an_upload_is_in_flight(self):
        screen = self.build()
        screen._working = True
        self.assertFalse(screen.can_leave())
        self.assertIn("packages folder", screen.leave_blocked_reason())


class NothingReachesARealClient(ScreenCase):
    """The same guard as the game updates card, for the same reason.

    An injectable seam on its own has been proved not to be enough: a feature
    was once added that made a previously inert path live inside the tests and
    the suite started making real requests to this LAN. So every real client
    this card could reach is replaced with something that raises, and then the
    whole flow is run.
    """

    def test_the_full_flow_never_touches_a_real_client(self):
        def explode(*args, **kwargs):
            raise AssertionError("a real network client was constructed")

        patches = [
            mock.patch.object(real_transport, "FtpLister", explode),
            mock.patch.object(real_transport, "HttpProbe", explode),
            mock.patch.object(real_ftpwrite, "FtpWriter", explode),
            mock.patch.object(consoleactions, "ConsoleActions", explode),
            mock.patch.object(installpkg, "FtpWriter", explode),
            mock.patch.object(installpkg, "ConsoleActions", explode),
            mock.patch.object(installpkg, "QFileDialog", explode),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        screen = self.build()
        screen.check_console()
        self.settle()
        screen.add_files([self.write("patch.pkg")])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertEqual(len(self.writer.stored), 1)
        self.assertEqual(self.actions.calls, 1)
        self.assertIn("install", screen._panel_heading.text().lower())


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
