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
                          RecordingActions, RecordingWriter, files, folders)

APP = QApplication.instance() or QApplication([])

#: The two title IDs the package fixtures carry. An install here is confirmed
#: by asking the console about the title afterwards, so a console where
#: installs land is one that can describe them.
FIXTURE_TITLES = ("BLES01717", "BLES01807")


def title_details(title_id, version="01.00"):
    """What the console has under /dev_hdd0/game/<title ID> afterwards."""
    return {f"/dev_hdd0/game/{title_id}/PARAM.SFO":
            make_packages.param_sfo((("APP_VER", version),
                                     ("CATEGORY", "GD"),
                                     ("TITLE", "A Game"),
                                     ("TITLE_ID", title_id)))}


def a_console_that_installs():
    """Blobs for a console that finishes the installs it is given."""
    blobs = {}
    for title_id in FIXTURE_TITLES:
        blobs.update(title_details(title_id))
    return blobs

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


# --- reading a file the user chose ------------------------------------------

class ReadingAPackage(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ps3-pkg-test-")
        self.addCleanup(lambda: _rmtree(self.folder))
        # Installing rescans the console's packages folder, so a test can
        # finish with a task still in flight. The pool is
        # QThreadPool.globalInstance(): work left running here turns up inside
        # whichever test comes next.
        self.addCleanup(self._drain)

    def _drain(self):
        services = getattr(self, "services", None)
        if services is None:
            return
        self.assertTrue(services.wait(10000),
                        "a test left work running in the pool")
        APP.processEvents()

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
        # Installing rescans the console's packages folder, so a test can
        # finish with a task still in flight. The pool is
        # QThreadPool.globalInstance(): work left running here turns up inside
        # whichever test comes next.
        self.addCleanup(self._drain)

    def _drain(self):
        services = getattr(self, "services", None)
        if services is None:
            return
        self.assertTrue(services.wait(10000),
                        "a test left work running in the pool")
        APP.processEvents()

    def write(self, name, blob=None):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as handle:
            handle.write(make_packages.package() if blob is None else blob)
        return path

    def build(self, listings=None, writer=None, actions=None, devices=None,
              host="127.0.0.1", blobs=None):
        connection = ConnectionState(host)
        services = Services(connection, StubTheme(), {})
        self.addCleanup(services.wait)
        self.services = services

        self.lister = FakeLister(
            listings=listings if listings is not None
            else {"/dev_hdd0/packages": ""},
            blobs=a_console_that_installs() if blobs is None else blobs)
        self.writer = writer if writer is not None else RecordingWriter()
        self.actions = actions if actions is not None else RecordingActions()
        storage = devices if devices is not None else [
            {"device": "dev_hdd0", "free_bytes": 64 * 1024 ** 3}]

        screen = installpkg.InstallPackagesScreen(services)
        screen._lister = lambda _host: self.lister
        screen._writer = lambda _host: self.writer
        screen._actions = lambda _host: self.actions
        screen.install_poll_seconds = 0.0
        screen.install_timeout_seconds = 0.0
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

        Where a task is given, its own done signal is what is waited for.
        The pool is QThreadPool.globalInstance(), shared with every other test
        in the process: "the pool is idle" can be true before this task has
        been picked up at all, and waiting on that raced against another file's
        leftovers. Waiting on the task itself cannot.

        Then the pool is pumped until it has stayed idle across three passes,
        which covers whatever the handler started -- an install rescans the
        console's packages folder, and that work must not be left in flight.
        """
        deadline = time.monotonic() + 10.0
        quiet = 0
        while time.monotonic() < deadline:
            APP.processEvents()
            idle = self.services.wait(50)
            # Services keeps a task until its done signal has been delivered,
            # so "no longer tracked" is the one reading of "this task has
            # finished" that does not race. Connecting to done here would:
            # a task can finish before the connection is made, and the signal
            # is never sent again.
            if task is not None and task in self.services.running_tasks():
                quiet = 0
                continue
            quiet = quiet + 1 if idle else 0
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
        self.assertIn("Nothing can check this file for you",
                      updates.NO_WAY_TO_CHECK)
        self.assertIn("Only install packages you trust",
                      updates.NO_WAY_TO_CHECK)
        self.assertIn("NO_WAY_TO_CHECK", self.body)

    def test_it_is_about_this_screen_rather_than_the_other_one(self):
        # It used to open by describing what Game updates does, which read on
        # this screen as a warning that had been put in the wrong place.
        self.assertNotIn("downloads from Sony", updates.NO_WAY_TO_CHECK)
        self.assertNotIn("every update", updates.NO_WAY_TO_CHECK)

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
        # It is a list to act on now, not a warning: anything sitting in that
        # folder can be installed from here without being sent again.
        names = [screen._console_table.topLevelItem(index).text(0)
                 for index in range(screen._console_table.topLevelItemCount())]
        self.assertEqual(names, ["someone-elses.pkg"])
        self.assertFalse(screen._console_table.isHidden())
        self.assertIn("Already on the console",
                      screen._console_heading.text())

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
        self.assertIn("installed", screen._panel_heading.text().lower())
        # Confirmed on the console, and the package left where it is.
        self.assertIn("installed on the console", screen._panel_body.text())
        self.assertIn("stay in the console's packages folder",
                      screen._panel_body.text())

    def test_every_file_is_sent_and_then_installed_one_at_a_time(self):
        # Everything is uploaded in one go, then installed one at a time with
        # the user saying when the console has finished each. Firing them in a
        # row drops some: seven sent back to back installed three.
        screen = self.build()
        screen.add_files([self.write("one.pkg"), self.write("two.pkg")])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertEqual([remote for remote, _body in self.writer.stored],
                         ["/dev_hdd0/packages/one.pkg",
                          "/dev_hdd0/packages/two.pkg"])
        self.assertEqual(self.actions.installed, ["one.pkg"])
        self.assertFalse(screen._next_button.isHidden())
        self.assertIn("1 left", screen._next_button.text())

        self.settle(screen._on_next_install())
        self.assertEqual(self.actions.installed, ["one.pkg", "two.pkg"])
        self.assertTrue(screen._next_button.isHidden())
        self.assertIn("2 packages installed", screen._panel_heading.text())

    def test_the_queue_can_be_turned_back_on_by_a_setting(self):
        # Kept so the batch can come back without a code change once the
        # console's install timing is understood.
        screen = self.build()
        updates.set_queue_installs(screen.services.settings, True)
        screen.add_files([self.write("one.pkg"), self.write("two.pkg")])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertEqual(self.actions.installed, ["one.pkg", "two.pkg"])
        self.assertTrue(screen._next_button.isHidden())

    def test_only_what_was_uploaded_is_ever_installed(self):
        # The folder may hold packages the user put there themselves. Those
        # are not ours to run, and the only defence against a later
        # "simplification" into a folder sweep is a test that says so.
        screen = self.build(
            listings={"/dev_hdd0/packages": files("someone-elses.pkg")})
        screen.add_files([self.write("mine.pkg")])
        self.settle(screen.start_run(screen.selected_files()))
        self.assertEqual(self.actions.installed, ["mine.pkg"])

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
        self.assertIn("did not install", screen._panel_heading.text())
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



def pkg_head(title_id):
    """The first bytes of a package, with a content ID in them."""
    head = bytearray(4096)
    head[0:4] = b"\x7fPKG"
    content = f"EP0002-{title_id}_00-SOMETHINGPKG000".encode()
    head[0x30:0x30 + len(content)] = content
    return bytes(head)


class WhatIsAlreadyOnTheConsole(ScreenCase):
    """The recovery list. Read off the console, not remembered.

    An install that failed leaves its package in the folder. Somebody who has
    just watched 2 GB fail needs to install it again without sending it again,
    and that has to work after this program or the console has been restarted.
    """

    def build_with(self, names, installed=None, heads=None, blobs=None):
        screen = self.build(
            listings={"/dev_hdd0/packages": files(*names),
                      "/dev_hdd0/game": folders(*(installed or []))},
            blobs=blobs)
        heads = heads or {}
        real = self.lister.download_bytes

        def read(path, max_bytes=None):
            name = path.rsplit("/", 1)[-1]
            if name in heads:
                return heads[name]
            return real(path, max_bytes)

        self.lister.download_bytes = read
        screen.check_console()
        self.settle()
        return screen

    def rows_on_console(self, screen):
        table = screen._console_table
        return [table.topLevelItem(index)
                for index in range(table.topLevelItemCount())]

    def test_each_package_is_listed_with_its_name_size_and_title(self):
        screen = self.build_with(
            ["EP0002-BLES01807_00-GTAV.pkg"],
            heads={"EP0002-BLES01807_00-GTAV.pkg": pkg_head("BLES01807")})
        row = self.rows_on_console(screen)[0]
        self.assertEqual(row.text(0), "EP0002-BLES01807_00-GTAV.pkg")
        self.assertTrue(row.text(1))
        self.assertEqual(row.text(2), "BLES01807")

    def test_a_title_already_installed_cannot_be_ticked(self):
        screen = self.build_with(
            ["EP0002-BLES01717_00-PATCH.pkg"], installed=["BLES01717"],
            heads={"EP0002-BLES01717_00-PATCH.pkg": pkg_head("BLES01717")})
        row = self.rows_on_console(screen)[0]
        self.assertFalse(row.flags() & Qt.ItemIsUserCheckable)
        self.assertIn("already installed", row.text(3))

    def test_installing_from_the_list_sends_nothing_across(self):
        screen = self.build_with(
            ["EP0002-BLES01807_00-GTAV.pkg"],
            heads={"EP0002-BLES01807_00-GTAV.pkg": pkg_head("BLES01807")})
        self.rows_on_console(screen)[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.assertTrue(screen._install_here.isEnabled())
        self.settle(screen._on_install_here())
        self.assertEqual(self.writer.stored, [])
        self.assertEqual(self.actions.installed,
                         ["EP0002-BLES01807_00-GTAV.pkg"])

    def test_the_list_is_read_again_after_installing(self):
        screen = self.build_with(
            ["EP0002-BLES01807_00-GTAV.pkg"],
            heads={"EP0002-BLES01807_00-GTAV.pkg": pkg_head("BLES01807")})
        self.rows_on_console(screen)[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        asked = len([path for path in self.lister.asked
                     if path.startswith("/dev_hdd0/packages")])
        self.settle(screen._on_install_here())
        self.settle()
        after = len([path for path in self.lister.asked
                     if path.startswith("/dev_hdd0/packages")])
        self.assertGreater(after, asked)

    def test_the_console_is_asked_about_the_title_after_a_package_goes(self):
        # A file somebody chose themselves carries no version Sony published,
        # so there is nothing to compare. What is asked instead is whether the
        # console can describe the title afterwards, and the point of this
        # test is that it is asked at all: the version check on the other card
        # shipped once in a state where it never ran and its tests all passed.
        screen = self.build_with(
            ["EP0002-BLES01807_00-GTAV.pkg"],
            heads={"EP0002-BLES01807_00-GTAV.pkg": pkg_head("BLES01807")})
        self.actions.on_install = lambda name: self.lister.listings.update(
            {"/dev_hdd0/packages": ""})
        self.rows_on_console(screen)[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        asked = []
        real = self.lister.download_bytes

        def watched(path, max_bytes=None):
            asked.append(path)
            return real(path, max_bytes)

        self.lister.download_bytes = watched
        self.settle(screen._on_install_here())
        self.assertIn("/dev_hdd0/game/BLES01807/PARAM.SFO", asked)

    def test_a_package_that_went_in_is_named_in_the_colour_for_done(self):
        # Visual feedback while the queue runs. The list is the only thing on
        # screen that says which of five packages is through.
        screen = self.build_with(
            ["EP0002-BLES01807_00-GTAV.pkg"],
            heads={"EP0002-BLES01807_00-GTAV.pkg": pkg_head("BLES01807")})
        self.actions.on_install = lambda name: self.lister.listings.update(
            {"/dev_hdd0/packages": ""})
        self.rows_on_console(screen)[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.settle(screen._on_install_here())
        ok = screen._colour_name("ok")
        self.assertIn(f'<span style="color: {ok}">{updates.STATE_INSTALLED}',
                      screen._queue.text())

    def test_a_title_the_console_cannot_describe_is_not_called_installed(self):
        # The package has gone from the folder and the console says nothing
        # about the title. Plenty of packages add to a game that is already
        # there and leave no folder of their own, so this is not reported as a
        # failure either. It is reported as what it is: unchecked.
        screen = self.build_with(
            ["EP0002-BLES01807_00-GTAV.pkg"],
            heads={"EP0002-BLES01807_00-GTAV.pkg": pkg_head("BLES01807")},
            blobs={})
        self.actions.on_install = lambda name: self.lister.listings.update(
            {"/dev_hdd0/packages": ""})
        self.rows_on_console(screen)[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.settle(screen._on_install_here())
        self.assertIn("could not be confirmed", screen._panel_heading.text())
        self.assertIn(updates.STATE_UNCONFIRMED, screen._queue.text())
        self.assertNotIn(updates.STATE_FAILED, screen._queue.text())

    def test_a_package_the_console_deletes_is_reported_as_installed(self):
        # The console deletes the package from its packages folder when the
        # install finishes. That is the signal the queue waits for, measured
        # twice on hardware with a 39 MB update.
        screen = self.build_with(
            ["EP0002-BLES01807_00-GTAV.pkg"],
            heads={"EP0002-BLES01807_00-GTAV.pkg": pkg_head("BLES01807")})
        self.actions.on_install = lambda name: self.lister.listings.update(
            {"/dev_hdd0/packages": ""})
        self.rows_on_console(screen)[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.settle(screen._on_install_here())
        self.assertIn("Installed", screen._panel_heading.text())
        self.assertIn(updates.STATE_INSTALLED, screen._queue.text())

    def test_a_package_the_console_never_finishes_with_stays_listed(self):
        # The file is still in the folder when the wait runs out, so nothing
        # here knows the install finished. It is reported as not confirmed
        # and it stays in the list saying so, where it can be read after the
        # run rather than disappearing.
        screen = self.build_with(
            ["EP0002-BLES01807_00-GTAV.pkg"],
            heads={"EP0002-BLES01807_00-GTAV.pkg": pkg_head("BLES01807")})
        self.rows_on_console(screen)[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.settle(screen._on_install_here())
        listed = screen._queue.text()
        self.assertIn("EP0002-BLES01807_00-GTAV.pkg", listed)
        self.assertIn(updates.STATE_UNCONFIRMED, listed)
        # Not "did not install": nothing here knows that. The console was
        # still holding the package when the wait ran out, and it may be
        # installing it still.
        self.assertIn("could not be confirmed", screen._panel_heading.text())

    def test_the_stage_line_says_the_seconds_spent_and_the_seconds_given(self):
        # There is no bar and no percentage for the install: the console
        # reports nothing between being asked and deleting the package.
        screen = self.build()
        screen._start_queue([("a.pkg", "a.pkg")])
        screen._on_progress(("waiting", "a.pkg", 14.0, 108.0, "BLES01807"))
        self.assertIn("14 seconds so far", screen._stage.text())
        self.assertIn("108", screen._stage.text())
        self.assertIn("installing, 14 seconds of 108", screen._queue.text())

    def test_every_stage_of_the_run_reaches_the_list(self):
        screen = self.build()
        screen._start_queue([("a.pkg", "a.pkg")])
        self.assertIn("waiting to start", screen._queue.text())
        for stage, first, second in (("upload", 1, 2), ("installing", 1, 1),
                                     ("checking", 1, 1)):
            screen._on_progress((stage, "a.pkg", first, second, ""))
            self.assertIn(updates.INSTALL_STAGES[stage], screen._queue.text())

    def test_the_screen_says_one_failure_does_not_stop_the_rest(self):
        screen = self.build()
        note = screen._chain_note.text()
        self.assertIn("does not stop the others", note)
        self.assertIn("stays in the console's packages folder", note)
        self.assertIn("Nothing here deletes them", note)


if __name__ == "__main__":
    unittest.main()
