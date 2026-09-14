"""The Qt shell: launcher, navigation, connection bar, theme.

Runs headless:

    QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests

Every test builds its own registry out of dummy screens, so none of this
depends on which real tools happen to exist yet. The registry is global, so
what was in it is put back afterwards rather than left cleared for whichever
test module runs next.
"""

import importlib
import json
import os
import pkgutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from mock_webman import MockWebmanHttp
from ps3diag import discovery
from ps3diag.parsers import SIGNATURE_THRESHOLD, looks_like_webman, \
    webman_score
from ps3diag.transport import HttpProbe, assert_safe_path, tcp_open
from ps3tools.shell import app as shell_app
from ps3tools.shell import registry
from ps3tools.shell.screen import (THEME_TOKENS, ConnectionState, Screen,
                                   Services)
from ps3tools.shell.theme import PALETTES, AppTheme, contrast_ratio
from ps3tools import PROJECT_URL, update

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "shell")

application = None


def setUpModule():
    global application
    application = QApplication.instance() or QApplication([])


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def net_fixture(name):
    """The interface-table fixtures, which live with the other network ones
    rather than under this module's own shell directory."""
    with open(os.path.join(os.path.dirname(FIXTURES), "net", name),
              encoding="utf-8") as handle:
        return handle.read()


# --- dummy screens ---------------------------------------------------------

class DummyScreen(Screen):
    """Counts its own lifecycle calls so navigation can be checked."""

    key = "dummy"
    title = "Dummy tool"
    blurb = "A screen that exists only to be navigated to."
    tile = "DY"
    order = 10

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        self.entered = 0
        self.left = 0
        self.leavable = True

    def on_enter(self):
        self.entered += 1

    def on_leave(self):
        self.left += 1

    def can_leave(self):
        return self.leavable


class OtherScreen(DummyScreen):
    key = "other"
    title = "Other tool"
    blurb = "A second screen, so the grid has more than one card."
    tile = "OT"
    order = 20


class LateScreen(DummyScreen):
    key = "late"
    title = "Late tool"
    blurb = "Registered after the launcher was built."
    tile = "LT"
    order = 30


class UnregisteredScreen(DummyScreen):
    key = "never"
    title = "Never registered"
    blurb = "Written but never registered, so it has no card."
    tile = "NV"
    order = 40


class ShellCase(unittest.TestCase):
    """A window over a registry holding exactly what the test put there."""

    screens = (DummyScreen, OtherScreen)

    def setUp(self):
        self._saved = registry.screens()
        registry.clear()
        for screen_class in self.screens:
            registry.register(screen_class)
        self.settings = {}
        self.theme = AppTheme("light")
        self.connection = ConnectionState("")
        self.services = Services(self.connection, self.theme, self.settings)
        self.window = shell_app.MainWindow(self.services)
        self.window.pages.animations_enabled = False

    def tearDown(self):
        self.window.pages.finish_now()
        self.window.close()
        self.window.deleteLater()
        registry.clear()
        for screen_class in self._saved:
            registry.register(screen_class)


# --- the launcher ----------------------------------------------------------

class LauncherTests(ShellCase):

    def test_one_card_per_registered_screen(self):
        self.assertEqual(self.window.launcher.card_keys(), ["dummy", "other"])

    def test_no_card_for_an_unregistered_screen(self):
        self.assertNotIn(UnregisteredScreen.key,
                         self.window.launcher.card_keys())

    def test_cards_carry_the_screen_s_own_words(self):
        card = self.window.launcher.cards[0]
        self.assertEqual(card.text(), DummyScreen.title)
        self.assertEqual(card.accessibleDescription(), DummyScreen.blurb)

    def test_cards_are_in_registry_order(self):
        registry.clear()
        for screen_class in (OtherScreen, DummyScreen):
            registry.register(screen_class)
        self.window.launcher.rebuild()
        self.assertEqual(self.window.launcher.card_keys(), ["dummy", "other"])

    def test_registering_a_screen_adds_a_card(self):
        # The point of the whole arrangement: a new tool is a new module plus
        # a register() call, and the launcher is not edited to suit it.
        registry.register(LateScreen)
        self.window.launcher.rebuild()
        self.assertEqual(self.window.launcher.card_keys(),
                         ["dummy", "other", "late"])

    def test_a_card_opens_its_screen(self):
        card = self.window.launcher.cards[1]
        card.click()
        self.assertEqual(self.window.current_key, "other")


# --- navigation ------------------------------------------------------------

class NavigationTests(ShellCase):

    def test_opening_a_screen_calls_on_enter(self):
        self.assertTrue(self.window.open_screen("dummy"))
        screen = self.window.screen_for("dummy")
        self.assertEqual(screen.entered, 1)
        self.assertEqual(screen.left, 0)
        self.assertIs(self.window.pages.current, screen)

    def test_leaving_a_screen_calls_on_leave(self):
        self.window.open_screen("dummy")
        screen = self.window.screen_for("dummy")
        self.assertTrue(self.window.go_home())
        self.assertEqual(screen.left, 1)
        self.assertEqual(self.window.current_key, shell_app.HOME_KEY)
        self.assertIs(self.window.pages.current, self.window.launcher)

    def test_moving_between_screens_leaves_then_enters(self):
        self.window.open_screen("dummy")
        self.window.open_screen("other")
        self.assertEqual(self.window.screen_for("dummy").left, 1)
        self.assertEqual(self.window.screen_for("other").entered, 1)
        self.assertEqual(self.window.current_key, "other")

    def test_request_home_returns_to_the_launcher(self):
        self.window.open_screen("dummy")
        self.window.screen_for("dummy").request_home.emit()
        self.assertEqual(self.window.current_key, shell_app.HOME_KEY)

    def test_the_cards_survive_a_round_trip_through_a_screen(self):
        before = self.window.launcher.card_keys()
        self.window.open_screen("dummy")
        self.window.go_home()
        self.assertEqual(self.window.current_key, shell_app.HOME_KEY)
        self.assertEqual(self.window.launcher.card_keys(), before)
        self.assertTrue(self.window.launcher.cards)

    def test_home_draws_the_set_the_window_resolved_at_start_up(self):
        # The registry is a mutable global and the launcher used to re-read it
        # on every navigation, so anything that emptied it emptied the home
        # screen. Whatever happens to it later, the window draws what it
        # started with.
        self.window.open_screen("dummy")
        registry.clear()
        self.window.go_home()
        self.assertEqual(self.window.launcher.card_keys(), ["dummy", "other"])

    def test_a_screen_can_still_be_opened_after_the_registry_empties(self):
        registry.clear()
        self.assertTrue(self.window.open_screen("other"))
        self.assertEqual(self.window.current_key, "other")

    def test_a_screen_may_word_its_own_refusal(self):
        self.window.open_screen("dummy")
        screen = self.window.screen_for("dummy")
        screen.leavable = False
        screen.leave_blocked_reason = lambda: "The EBOOT is half written."
        self.assertFalse(self.window.go_home())
        self.assertIn("The EBOOT is half written.",
                      self.window.status_text())

    def test_can_leave_false_refuses_navigation(self):
        self.window.open_screen("dummy")
        screen = self.window.screen_for("dummy")
        screen.leavable = False
        self.assertFalse(self.window.go_home())
        self.assertEqual(self.window.current_key, "dummy")
        self.assertEqual(screen.left, 0)
        # The screen's own wording is shown after its title, so the assertion
        # is on the title plus the interface's default reason rather than on
        # the shell's old generic sentence.
        text = self.window.status_text()
        self.assertIn("Dummy tool:", text)
        self.assertIn("cannot be interrupted", text)

    def test_can_leave_false_also_refuses_another_screen(self):
        self.window.open_screen("dummy")
        self.window.screen_for("dummy").leavable = False
        self.assertFalse(self.window.open_screen("other"))
        self.assertEqual(self.window.current_key, "dummy")

    def test_busy_blocks_navigation(self):
        self.window.open_screen("dummy")
        screen = self.window.screen_for("dummy")
        screen.busy_changed.emit(True)
        self.assertTrue(self.window.busy_bar.isVisibleTo(self.window))
        self.assertFalse(self.window.go_home())
        self.assertEqual(self.window.current_key, "dummy")
        self.assertEqual(screen.left, 0)
        screen.busy_changed.emit(False)
        self.assertTrue(self.window.go_home())

    def test_busy_disables_the_back_button(self):
        self.window.open_screen("dummy")
        self.assertTrue(self.window.back_button.isEnabled())
        self.window.screen_for("dummy").busy_changed.emit(True)
        self.assertFalse(self.window.back_button.isEnabled())

    def test_status_message_reaches_the_status_area(self):
        self.window.open_screen("dummy")
        screen = self.window.screen_for("dummy")
        screen.status_message.emit("Reading dev_hdd0.")
        self.assertEqual(self.window.status_text(), "Reading dev_hdd0.")

    def test_an_unknown_key_is_refused_rather_than_crashing(self):
        self.assertFalse(self.window.open_screen("no-such-tool"))
        self.assertEqual(self.window.current_key, shell_app.HOME_KEY)

    def test_a_transition_cannot_be_re_entered(self):
        self.window.pages.animations_enabled = True
        self.window.show()
        self.assertTrue(self.window.open_screen("dummy"))
        self.assertTrue(self.window.pages.animating)
        # Second request arrives mid-slide and is refused, not queued.
        self.assertFalse(self.window.open_screen("other"))
        self.assertEqual(self.window.current_key, "dummy")
        self.window.pages.finish_now()
        self.assertFalse(self.window.pages.animating)
        self.assertTrue(self.window.open_screen("other"))


# --- the connection bar ----------------------------------------------------

class ConnectionBarTests(ShellCase):

    def setUp(self):
        super().setUp()
        self.bar = self.window.connection_bar

    def test_it_shows_the_connection_state(self):
        self.connection.set_host("192.168.1.50")
        self.connection.set_connection(
            "connected", "webMAN answered at 192.168.1.50.")
        self.assertEqual(self.bar.state_text(), "Connected")
        self.assertIn("webMAN answered", self.bar.detail_text())

        self.connection.set_connection("unreachable", "Could not reach it.")
        self.assertEqual(self.bar.state_text(), "Cannot be reached")

        self.connection.set_connection("checking", "Asking 192.168.1.50.")
        self.assertEqual(self.bar.state_text(), "Checking")
        self.assertFalse(self.bar._check_button.isEnabled())

    def test_it_never_shows_scan_state(self):
        # The bug this shell exists to stop repeating: a subnet scan that found
        # nothing saying nothing about an address the user typed by hand.
        self.connection.set_host("192.168.1.50")
        self.connection.set_connection(
            "connected", "webMAN answered at 192.168.1.50.")
        self.connection.set_scan("none", "No PS3 found on this network.")

        shown = self.bar.visible_text().lower()
        self.assertEqual(self.bar.state_text(), "Connected")
        for forbidden in ("not found", "no ps3", "found on this network",
                          "scan", "searching"):
            self.assertNotIn(forbidden, shown)

    def test_a_failed_scan_does_not_touch_an_unchecked_address(self):
        self.connection.set_scan("failed", "The search could not be run.")
        self.assertEqual(self.bar.state_text(), "Not checked")
        self.assertNotIn("search", self.bar.visible_text().lower())

    def test_a_new_address_resets_the_verdict(self):
        self.connection.set_host("192.168.1.50")
        self.connection.set_connection("connected", "webMAN answered.")
        self.bar.set_address("192.168.1.51")
        self.assertEqual(self.connection.host, "192.168.1.51")
        self.assertEqual(self.bar.state_text(), "Not checked")

    def test_typing_an_address_shares_it_with_every_screen(self):
        self.bar.set_address("10.0.0.7")
        self.assertEqual(self.services.connection.host, "10.0.0.7")

    def test_check_with_no_address_asks_for_one_rather_than_probing(self):
        self.assertIsNone(self.bar.check())
        self.assertEqual(self.connection.connection, "unknown")
        self.assertIn("IP address", self.connection.connection_detail)

    def test_a_webman_reply_is_a_connection(self):
        self.connection.set_host("192.168.1.50")
        self.bar._checked({"host": "192.168.1.50", "ok": True,
                           "body": fixture("webman_root.html")})
        self.assertEqual(self.connection.connection, "connected")
        self.assertEqual(self.bar.state_text(), "Connected")

    def test_something_that_is_not_webman_is_not_a_connection(self):
        self.connection.set_host("192.168.1.1")
        self.bar._checked({"host": "192.168.1.1", "ok": True,
                           "body": fixture("router_root.html")})
        self.assertEqual(self.connection.connection, "unreachable")
        self.assertIn("not webMAN", self.connection.connection_detail)

    def test_no_reply_says_what_to_check(self):
        self.connection.set_host("192.168.1.50")
        self.bar._checked({"host": "192.168.1.50", "ok": False, "body": ""})
        self.assertEqual(self.connection.connection, "unreachable")
        for expected in ("switched on", "main menu", "router"):
            self.assertIn(expected, self.connection.connection_detail)

    def test_a_reply_about_a_stale_address_is_ignored(self):
        self.connection.set_host("192.168.1.51")
        self.bar._checked({"host": "192.168.1.50", "ok": True,
                           "body": fixture("webman_root.html")})
        self.assertEqual(self.connection.connection, "unknown")


class UpdateBannerTests(ShellCase):
    """The strip above the pages. Nothing here is allowed to check anything.

    The window must be buildable without a network, because every other test
    in this file builds one and tests/check-no-network.py fails the whole run
    on a single off-loopback connect.
    """

    def test_the_banner_sits_between_the_bar_and_the_pages(self):
        column = self.window.centralWidget().layout()
        order = [column.itemAt(index).widget()
                 for index in range(column.count())]
        self.assertIn(self.window.update_banner, order)
        self.assertEqual(order.index(self.window.update_banner),
                         order.index(self.window.connection_bar) + 1)
        self.assertEqual(order.index(self.window.pages),
                         order.index(self.window.update_banner) + 1)

    def test_it_starts_hidden(self):
        self.assertFalse(self.window.update_banner.isVisibleTo(self.window))

    def test_building_the_window_checks_nothing(self):
        # The check is started from main() and from nowhere else. A window
        # built for any other reason must not reach for the network.
        calls = []
        original = update.check

        def refuse(*args, **kwargs):
            calls.append(args)
            raise AssertionError("the update check ran from a test")

        update.check = refuse
        self.addCleanup(setattr, update, "check", original)
        window = shell_app.MainWindow(self.services)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        application.processEvents()
        self.assertEqual(calls, [])
        self.assertIsNone(window.update_banner._task)
        self.assertFalse(window.update_banner.isVisibleTo(window))


class FindTests(ShellCase):
    """The Find button end to end, with the two calls that would put a packet
    on the wire replaced.

    ps3diag.discovery takes `connect` and `fetch` as parameters for exactly
    this reason. Nothing in this class opens a socket and no test here asks for
    a real subnet: the host list is injected as well.
    """

    def setUp(self):
        super().setUp()
        self.bar = self.window.connection_bar
        self.hosts = [f"192.168.9.{number}" for number in range(1, 6)]
        self.bar._scan_targets = lambda: ("192.168.9.100", list(self.hosts))
        self.answers = {}
        self.bar.scan_connect = lambda host: host in self.answers
        self.bar.scan_fetch = lambda host: self.answers.get(host)
        # Find now runs a check on a single match, so the check needs a stub
        # as well. Without it the test reaches for the invented addresses for
        # real, which tests/check-no-network.py catches and the live console
        # on this LAN is the reason it exists.
        self.bar.check_fetch = lambda host: self.answers.get(host)
        self.asked = []
        self.bar._choose = self.asked.append

    def _run_find(self):
        task = self.bar.find()
        self.assertIsNotNone(task)
        self.assertEqual(self.connection.scan, "scanning")
        self.assertTrue(self.services.wait(10000))
        application.processEvents()
        return task

    def test_one_console_fills_the_address_in_and_checks_it(self):
        self.answers["192.168.9.3"] = fixture("webman_root.html")
        self._run_find()
        self.assertEqual(self.connection.scan, "found")
        self.assertEqual(self.connection.host, "192.168.9.3")
        # Pressing Find is already a statement that this is the console to
        # use, so the check runs without a second button press. Leaving the
        # status on "Not checked" beside an address the program had just
        # found read as though the search had failed.
        self.assertIn(self.connection.connection, ("checking", "connected"))

    def test_find_never_reaches_the_network_through_the_auto_check(self):
        """The guard for the hole this feature opened.

        Find running a check by itself means a test feeding it invented
        addresses will reach for them for real unless the check is stubbed
        too. That happened, and tests/check-no-network.py caught it: three
        requests went to made-up LAN addresses. This asserts the seam is
        honoured, so the audit is not the only thing standing between a test
        and the live console on this network.
        """
        def explode(*args, **kwargs):
            raise AssertionError("the real HttpProbe was constructed")

        original = shell_app.HttpProbe
        shell_app.HttpProbe = explode
        self.addCleanup(setattr, shell_app, "HttpProbe", original)
        self.answers["192.168.9.3"] = fixture("webman_root.html")
        self._run_find()
        self.assertEqual(self.connection.host, "192.168.9.3")
        self.assertIn(self.connection.connection, ("checking", "connected"))

    def test_the_scan_result_is_still_not_the_connection_result(self):
        # The two remain separate things even though one now triggers the
        # other: the scan says where it looked, the connection says whether
        # that address answered.
        self.answers["192.168.9.3"] = fixture("webman_root.html")
        self._run_find()
        self.assertEqual(self.connection.scan, "found")
        self.assertNotEqual(self.connection.scan_detail,
                            self.connection.connection_detail)

    def test_several_consoles_are_put_to_the_user(self):
        self.answers["192.168.9.3"] = fixture("webman_root.html")
        self.answers["192.168.9.4"] = fixture("webman_root.html")
        self._run_find()
        self.assertEqual(self.connection.scan, "found")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(self.asked[0]), 2)
        # Nothing is chosen on the user's behalf.
        self.assertEqual(self.connection.host, "")

    def test_a_router_is_not_a_console(self):
        self.answers["192.168.9.1"] = fixture("router_root.html")
        self._run_find()
        self.assertEqual(self.connection.scan, "none")
        self.assertEqual(self.connection.host, "")

    def test_nothing_found_is_written_to_scan_and_never_to_connection(self):
        # The bug being fixed. A console answering at a typed address, and a
        # search of the wrong subnet finding nothing, must not cancel it out.
        self.connection.set_host("192.168.1.50")
        self.connection.set_connection(
            "connected", "webMAN answered at 192.168.1.50.")
        self._run_find()

        self.assertEqual(self.connection.scan, "none")
        self.assertEqual(self.connection.connection, "connected")
        self.assertEqual(self.bar.state_text(), "Connected")
        self.assertIn("No PS3 found", self.bar.scan_text())
        self.assertNotIn("No PS3", self.bar.visible_text())

    def test_the_scan_and_the_connection_are_different_widgets(self):
        self.connection.set_connection("connected", "webMAN answered.")
        self.connection.set_scan("none", "No PS3 found on this network.")
        scan_label = self.bar._scan_label
        self.assertIsNot(scan_label, self.bar._state_label)
        self.assertIsNot(scan_label, self.bar._detail_label)
        self.assertEqual(scan_label.text(), "No PS3 found on this network.")
        self.assertEqual(self.bar._state_label.text(), "Connected")
        self.assertEqual(self.bar._detail_label.text(), "webMAN answered.")

    def test_the_scan_line_is_never_coloured_like_a_verdict(self):
        dim = self.theme.colour("text_dim")
        for state, detail in (("none", "No PS3 found on this network."),
                              ("failed", "The search could not be run."),
                              ("found", "Found a PS3 at 192.168.1.50.")):
            self.connection.set_scan(state, detail)
            self.assertIn(dim, self.bar._scan_label.styleSheet())
            for token in ("error", "warn", "ok"):
                self.assertNotIn(self.theme.colour(token),
                                 self.bar._scan_label.styleSheet())

    def test_the_diagnostic_line_names_the_single_network_searched(self):
        self._run_find()
        line = self.bar.scan_text()
        self.assertIn("Searched 5 addresses on 192.168.9.0/24", line)
        self.assertIn("this PC being 192.168.9.100", line)

    def test_the_diagnostic_line_names_every_network_searched(self):
        # The line is what identified the bug in the first place, so a search
        # of more than one subnet has to say so rather than name the first.
        hosts = ["192.168.9.3", "192.168.140.3", "172.19.16.3"]
        self.bar._scan_targets = lambda: (
            ["192.168.9.100", "192.168.140.1", "172.19.16.1"], hosts)
        self._run_find()
        line = self.bar.scan_text()
        self.assertIn("Searched 3 addresses on", line)
        for network, address in (("192.168.9.0/24", "192.168.9.100"),
                                 ("192.168.140.0/24", "192.168.140.1"),
                                 ("172.19.16.0/24", "172.19.16.1")):
            self.assertIn(f"{network} (this PC {address})", line)

    def test_the_progress_line_says_where_it_is_looking(self):
        self.bar._scan_targets = lambda: (
            ["192.168.9.100", "192.168.140.1"], list(self.hosts))
        self._run_find()
        self.bar._scan_progress((2, 5, 0))
        self.assertIn("192.168.9.0/24 and 192.168.140.0/24",
                      self.bar.scan_text())

    def test_a_console_on_the_second_subnet_is_still_found(self):
        self.bar._scan_targets = lambda: (
            ["192.168.9.100", "192.168.140.1"],
            ["192.168.9.3", "192.168.140.95"])
        self.answers["192.168.140.95"] = fixture("webman_root.html")
        self._run_find()
        self.assertEqual(self.connection.scan, "found")
        self.assertEqual(self.connection.host, "192.168.140.95")

    def test_a_pc_with_no_usable_address_says_so_without_searching(self):
        self.bar._scan_targets = lambda: ("", [])
        self._run_find()
        self.assertEqual(self.connection.scan, "failed")
        self.assertIn("type the console's address",
                      self.connection.scan_detail.lower())
        self.assertEqual(self.connection.connection, "unknown")
        self.assertEqual(self.bar.state_text(), "Not checked")

    def test_a_second_search_is_refused_while_one_is_running(self):
        self.connection.set_scan("scanning", "Looking for a PS3.")
        self.assertIsNone(self.bar.find())
        self.assertFalse(self.bar._find_button.isEnabled())
        self.connection.set_scan("none", "No PS3 found on this network.")
        self.assertTrue(self.bar._find_button.isEnabled())

    def test_progress_is_reported_against_the_search_and_not_the_pill(self):
        self.connection.set_connection("connected", "webMAN answered.")
        self.bar._scan_progress((12, 254))
        self.assertEqual(self.connection.scan, "scanning")
        self.assertIn("12 of 254", self.bar.scan_text())
        self.assertEqual(self.bar.state_text(), "Connected")

    def test_the_local_address_chosen_is_the_one_the_router_hands_out(self):
        self.assertEqual(
            shell_app._private_address(
                ["10.4.0.2", "192.168.1.23", "169.254.7.7"]),
            "192.168.1.23")
        self.assertEqual(
            shell_app._private_address(["10.4.0.2"]), "10.4.0.2")
        self.assertEqual(
            shell_app._private_address(["8.8.8.8", "127.0.0.1", "nonsense"]),
            "")


class CheckActionTests(ShellCase):
    """The Check button end to end, against the loopback mock console.

    Nothing here reaches the network: MockWebmanHttp binds to 127.0.0.1 and
    the test asks for that address by name.
    """

    def setUp(self):
        super().setUp()
        self.console = MockWebmanHttp().start()
        self.addCleanup(self.console.stop)
        self.bar = self.window.connection_bar

    def _run_check(self):
        task = self.bar.check()
        self.assertIsNotNone(task)
        self.assertEqual(self.connection.connection, "checking")
        self.assertTrue(self.services.wait(10000))
        application.processEvents()
        return task

    def test_a_reachable_console_becomes_connected(self):
        self.bar.set_address(self.console.address)
        self._run_check()
        self.assertEqual(self.connection.connection, "connected")
        self.assertEqual(self.bar.state_text(), "Connected")
        self.assertEqual([request[1] for request in self.console.requests],
                         ["/"])

    def test_nothing_listening_is_unreachable_not_a_crash(self):
        port = self.console.port
        self.console.stop()
        self.bar.set_address(f"127.0.0.1:{port}")
        self._run_check()
        self.assertEqual(self.connection.connection, "unreachable")
        self.assertIn("switched on", self.connection.connection_detail)


# --- the theme -------------------------------------------------------------

class ThemeTests(unittest.TestCase):

    def test_every_token_has_a_value_in_both_themes(self):
        for mode in ("light", "dark"):
            theme = AppTheme(mode)
            for token in THEME_TOKENS:
                value = theme.colour(token)
                self.assertTrue(value.startswith("#"),
                                f"{mode} {token} is {value!r}")

    def test_the_two_themes_differ(self):
        light, dark = AppTheme("light"), AppTheme("dark")
        self.assertFalse(light.dark)
        self.assertTrue(dark.dark)
        for token in THEME_TOKENS:
            self.assertNotEqual(light.colour(token), dark.colour(token),
                                f"{token} is the same in both themes")

    def test_an_unknown_token_is_an_error_not_a_blank(self):
        with self.assertRaises(KeyError):
            AppTheme("light").colour("backgrounde")

    def test_both_themes_are_legible(self):
        # A screen asks for a token and draws it; nobody checks each one by
        # eye in both themes, so the palettes are checked here instead.
        for mode, palette in PALETTES.items():
            for ground in ("bg", "surface", "surface_alt"):
                for token in ("text", "text_dim", "ok", "warn", "error",
                              "info", "accent"):
                    ratio = contrast_ratio(palette[token], palette[ground])
                    self.assertGreaterEqual(
                        ratio, 4.5,
                        f"{mode}: {token} on {ground} is only {ratio:.2f}:1")
            ratio = contrast_ratio(palette["accent_text"], palette["accent"])
            self.assertGreaterEqual(ratio, 4.5,
                                    f"{mode}: accent_text on accent is "
                                    f"only {ratio:.2f}:1")

    def test_the_card_tile_is_legible_in_both_of_its_states(self):
        # The tile is painted rather than styled, so its two states are the
        # one pair of colours the palette check above cannot see.
        for mode, palette in PALETTES.items():
            quiet = contrast_ratio(palette["accent"], palette["surface_alt"])
            lit = contrast_ratio(palette["accent_text"], palette["accent"])
            self.assertGreaterEqual(quiet, 4.5,
                                    f"{mode}: resting tile is {quiet:.2f}:1")
            self.assertGreaterEqual(lit, 4.5,
                                    f"{mode}: lit tile is {lit:.2f}:1")

    def test_toggling_announces_the_change(self):
        theme = AppTheme("light")
        seen = []
        theme.changed.connect(lambda: seen.append(theme.dark))
        theme.toggle()
        self.assertEqual(seen, [True])
        self.assertEqual(theme.mode, "dark")
        theme.toggle()
        self.assertEqual(seen, [True, False])

    def test_a_repeated_choice_says_nothing(self):
        theme = AppTheme("dark")
        seen = []
        theme.changed.connect(lambda: seen.append(theme.dark))
        theme.set_mode("dark")
        self.assertEqual(seen, [])


class ThemeInTheWindowTests(ShellCase):

    def test_choosing_a_theme_is_remembered(self):
        self.window._choose_theme("dark")
        self.assertTrue(self.theme.dark)
        self.assertEqual(self.settings["theme_mode"], "dark")
        self.window._choose_theme("light")
        self.assertFalse(self.theme.dark)
        self.assertEqual(self.settings["theme_mode"], "light")


# --- settings --------------------------------------------------------------

class SettingsTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "settings.json")
        self._saved = shell_app.settings_paths
        shell_app.settings_paths = lambda: [self.path]

    def tearDown(self):
        shell_app.settings_paths = self._saved
        self.directory.cleanup()

    def test_a_round_trip(self):
        written = shell_app.save_settings(
            {"host": "192.168.1.50", "theme_mode": "dark", "_path": "ignored"})
        self.assertEqual(written, self.path)
        with open(self.path, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertNotIn("_path", stored)
        loaded = shell_app.load_settings()
        self.assertEqual(loaded["host"], "192.168.1.50")
        self.assertEqual(loaded["theme_mode"], "dark")

    def test_one_unserialisable_setting_does_not_lose_the_rest(self):
        shell_app.save_settings({"host": "192.168.1.50",
                                 "handle": object()})
        loaded = shell_app.load_settings()
        self.assertEqual(loaded["host"], "192.168.1.50")
        self.assertNotIn("handle", loaded)

    def test_a_window_stores_what_it_should_remember(self):
        saved = registry.screens()
        registry.clear()
        self.addCleanup(lambda: [registry.register(item) for item in saved])
        self.addCleanup(registry.clear)
        theme = AppTheme("dark")
        connection = ConnectionState("192.168.1.50")
        settings = {}
        window = shell_app.MainWindow(
            Services(connection, theme, settings))
        window.store_settings()
        window.deleteLater()
        loaded = shell_app.load_settings()
        self.assertEqual(loaded["host"], "192.168.1.50")
        self.assertEqual(loaded["theme_mode"], "dark")
        self.assertTrue(loaded["geometry"])


# --- screen discovery ------------------------------------------------------

class ScreenDiscoveryTests(unittest.TestCase):
    """The frozen build has no directory to enumerate.

    pkgutil walks a filesystem path. A onefile exe keeps its modules in an
    archive, so iter_modules finds nothing there and the launcher came up with
    no cards at all. Discovery has to work from the explicit list as well.
    """

    MODULES = ("ps3tools.screens.diagnostics", "ps3tools.screens.patcher")

    def setUp(self):
        self._saved = registry.screens()
        self._saved_modules = {name: sys.modules[name]
                               for name in self.MODULES if name in sys.modules}

    def tearDown(self):
        registry.clear()
        for screen_class in self._saved:
            registry.register(screen_class)
        sys.modules.update(self._saved_modules)

    def _forget(self):
        registry.clear()
        for name in self.MODULES:
            sys.modules.pop(name, None)

    def test_the_known_modules_import_when_nothing_can_be_enumerated(self):
        self._forget()
        saved = pkgutil.iter_modules
        pkgutil.iter_modules = lambda *args, **kwargs: iter(())
        try:
            problems = shell_app.load_screen_modules()
        finally:
            pkgutil.iter_modules = saved
        self.assertEqual(problems, [])
        self.assertTrue(registry.screens())
        for name in self.MODULES:
            self.assertIn(name, sys.modules)

    def test_enumeration_is_still_what_finds_a_new_module(self):
        self._forget()
        problems = shell_app.load_screen_modules()
        self.assertEqual(problems, [])
        self.assertTrue(registry.screens())

    def test_the_fallback_list_matches_what_is_on_disk(self):
        # The list exists for the exe. If a module is added and not named in
        # it, the exe silently ships one tool fewer than the checkout.
        package = importlib.import_module("ps3tools.screens")
        on_disk = {info.name for info in pkgutil.iter_modules(
            list(package.__path__)) if not info.name.startswith("_")}
        self.assertEqual(set(shell_app.KNOWN_SCREEN_MODULES), on_disk)


# --- the window frame ------------------------------------------------------

class WindowSizeTests(ShellCase):

    def test_the_window_opens_big_enough_not_to_clip(self):
        self.assertGreaterEqual(self.window.width(), 1024)
        self.assertGreaterEqual(self.window.height(), 700)

    def test_the_minimum_leaves_room_for_the_screens(self):
        self.assertGreaterEqual(self.window.minimumWidth(), 1024)
        self.assertGreaterEqual(self.window.minimumHeight(), 700)

    def test_nothing_caps_how_big_it_may_be(self):
        # Qt's own "no maximum" value. A maximum would stop a user with a
        # large monitor from using it.
        self.assertGreaterEqual(self.window.maximumWidth(), 16777215)
        self.assertGreaterEqual(self.window.maximumHeight(), 16777215)


class ChromeTests(ShellCase):

    def test_the_vendor_is_a_link_to_the_project(self):
        link = self.window.vendor_link
        self.assertEqual(link.text(), "setsid")
        self.assertEqual(link.url, PROJECT_URL)
        self.assertEqual(link.url, "https://github.com/setsid")

    def test_the_link_says_it_is_one(self):
        link = self.window.vendor_link
        self.assertEqual(link.cursor().shape(),
                         shell_app.Qt.CursorShape.PointingHandCursor)
        self.assertIn("brandLink", link.objectName())
        self.assertIn(PROJECT_URL, link.toolTip())

    def test_the_back_button_carries_a_drawn_arrow(self):
        self.window.open_screen("dummy")
        button = self.window.back_button
        self.assertFalse(button.icon().isNull())
        self.assertFalse(button.icon().pixmap(14, 14).isNull())

    def test_the_arrow_is_not_a_character(self):
        # No emoji and no icon font: both are a font substitution away from a
        # box on a machine with nothing installed on it.
        text = self.window.back_button.text()
        self.assertEqual(text, "Back to tools")
        self.assertTrue(all(ord(character) < 128 for character in text))

    def test_the_back_button_and_its_rule_appear_together(self):
        self.assertFalse(self.window.back_button.isVisible())
        self.assertFalse(self.window._chrome_rule.isVisible())
        self.window.show()
        self.window.open_screen("dummy")
        self.assertTrue(self.window.back_button.isVisible())
        self.assertTrue(self.window._chrome_rule.isVisible())
        self.window.go_home()
        self.assertFalse(self.window.back_button.isVisible())
        self.assertFalse(self.window._chrome_rule.isVisible())

    def test_the_breadcrumb_names_the_screen(self):
        self.window.open_screen("dummy")
        self.assertIn(DummyScreen.title, self.window.screen_title.text())
        self.window.go_home()
        self.assertEqual(self.window.screen_title.text(), "")


class ThemeMenuTests(ShellCase):
    """The popup is a top level window of its own, and left to itself it
    opened in the desktop's colours: a light window with a dark menu."""

    def _menu_colour(self, role):
        return self.window.theme_menu.palette().color(role).name()

    def test_the_menu_matches_the_light_theme(self):
        self.window._choose_theme("light")
        roles = QPalette.ColorRole
        self.assertEqual(self._menu_colour(roles.WindowText),
                         PALETTES["light"]["text"])
        self.assertEqual(self._menu_colour(roles.Window),
                         PALETTES["light"]["surface"])
        self.assertEqual(self._menu_colour(roles.Text),
                         PALETTES["light"]["text"])

    def test_the_menu_matches_the_dark_theme(self):
        self.window._choose_theme("dark")
        roles = QPalette.ColorRole
        self.assertEqual(self._menu_colour(roles.WindowText),
                         PALETTES["dark"]["text"])
        self.assertEqual(self._menu_colour(roles.Window),
                         PALETTES["dark"]["surface"])
        self.assertEqual(self._menu_colour(roles.Text),
                         PALETTES["dark"]["text"])

    def test_the_menu_is_styled_in_the_active_theme(self):
        for mode in ("light", "dark"):
            self.window._choose_theme(mode)
            sheet = self.window.theme_menu.styleSheet()
            self.assertIn("QMenu", sheet)
            self.assertIn(PALETTES[mode]["surface"], sheet)
            self.assertIn(PALETTES[mode]["text"], sheet)
            self.assertNotIn(PALETTES["dark" if mode == "light" else "light"]
                             ["bg"], sheet)

    def test_the_menu_text_is_legible_on_its_own_background(self):
        for mode in ("light", "dark"):
            tokens = PALETTES[mode]
            self.assertGreaterEqual(
                contrast_ratio(tokens["text"], tokens["surface"]), 4.5)
            self.assertGreaterEqual(
                contrast_ratio(tokens["accent_text"], tokens["accent"]), 4.5)


# --- the home screen after a visit to a tool -------------------------------

class LauncherRoundTripTests(ShellCase):
    """Back to tools, and the cards are still drawn.

    The window is shown, because the bug only exists on a launcher that is
    already on screen when it is rebuilt, and the assertions are about paint
    rather than bookkeeping. A launcher holding three cards it never draws
    passes any count of len(launcher.cards); what the user was looking at was
    a heading, a paragraph, and nothing else.
    """

    def setUp(self):
        super().setUp()
        # Wide enough that all the cards fit on one row, so a grid of the
        # right height is unambiguous.
        self.window.resize(1400, 900)
        self.window.show()
        self.addCleanup(self.window.hide)
        self._settle()

    def _settle(self):
        for _ in range(8):
            application.processEvents()

    def _round_trip(self):
        self.assertTrue(self.window.open_screen("dummy"))
        self._settle()
        self.assertTrue(self.window.go_home())
        self._settle()

    def _drawn_cards(self):
        """The cards with somewhere on screen to be drawn in.

        A card can be visible, the right size and still invisible to the user,
        because the host it sits in has been squeezed to nothing around it.
        Only the part of the card that falls inside its host is ever painted.
        """
        host = self.window.launcher._grid_host
        area = host.rect()
        return [card for card in self.window.launcher.cards
                if card.isVisible()
                and not card.geometry().intersected(area).isEmpty()]

    def test_the_cards_are_drawn_before_anyone_goes_anywhere(self):
        self.assertEqual(len(self._drawn_cards()), 2)

    def test_the_cards_are_still_drawn_after_a_round_trip(self):
        before = len(self._drawn_cards())
        self._round_trip()
        self.assertEqual(self.window.current_key, shell_app.HOME_KEY)
        self.assertTrue(self.window.launcher.isVisible())
        self.assertIs(self.window.pages.current, self.window.launcher)
        self.assertEqual(len(self._drawn_cards()), before)
        for card in self.window.launcher.cards:
            self.assertTrue(card.isVisible(), card.key)
            self.assertGreater(card.width(), 0, card.key)
            self.assertGreater(card.height(), 0, card.key)

    def test_the_grid_keeps_its_height_after_a_round_trip(self):
        # The failure itself: the host was pinned to a fixed height of nought
        # because it was measured while the new cards were still hidden, and
        # nothing ever measured it again.
        host = self.window.launcher._grid_host
        before = host.height()
        self.assertGreater(before, 0)
        self._round_trip()
        self.assertEqual(host.height(), before)
        self.assertGreaterEqual(host.maximumHeight(),
                                self.window.launcher.cards[0].height())

    def test_the_card_grid_still_has_something_in_it_after_a_round_trip(self):
        # Rendered rather than reasoned about. An empty grid grabs as either
        # nothing at all or one flat colour.
        self._round_trip()
        pixmap = self.window.launcher._grid_host.grab()
        self.assertGreaterEqual(pixmap.height(),
                                self.window.launcher.cards[0].height())
        image = pixmap.toImage()
        colours = {image.pixel(x, y)
                   for y in range(0, image.height(), 4)
                   for x in range(0, image.width(), 4)}
        self.assertGreater(len(colours), 3, "the card grid grabbed blank")

    def test_several_round_trips_do_not_wear_the_grid_away(self):
        for _ in range(3):
            self._round_trip()
        self.assertEqual(len(self._drawn_cards()), 2)

    def test_coming_home_does_not_churn_cards_that_have_not_changed(self):
        # Rebuilding an identical grid is what opened the window in which the
        # cards were hidden, and it costs the focused card its focus.
        before = self.window.launcher.cards
        self._round_trip()
        self.assertEqual([card.key for card in self.window.launcher.cards],
                         [card.key for card in before])
        for old, new in zip(before, self.window.launcher.cards):
            self.assertIs(old, new)

    def test_a_rebuild_with_a_different_set_still_replaces_the_cards(self):
        self.window.launcher.rebuild([DummyScreen])
        self._settle()
        self.assertEqual(self.window.launcher.card_keys(), ["dummy"])
        self.assertEqual(len(self._drawn_cards()), 1)
        self.window.launcher.rebuild([DummyScreen, OtherScreen, LateScreen])
        self._settle()
        self.assertEqual(len(self._drawn_cards()), 3)

    def test_a_rebuild_on_a_shown_launcher_draws_the_new_cards(self):
        # The general form of the bug: any rebuild of a launcher that is
        # already on screen, not just the one coming home does.
        self.window.launcher.rebuild([LateScreen])
        self._settle()
        drawn = self._drawn_cards()
        self.assertEqual([card.key for card in drawn], ["late"])
        self.assertGreater(self.window.launcher._grid_host.height(), 0)


# --- which network the search looks at -------------------------------------

IPCONFIG_WITH_EVERYTHING = """
Windows IP Configuration

Ethernet adapter vEthernet (WSL (Hyper-V firewall)):

   IPv4 Address. . . . . . . . . . . : 172.29.16.1
   Subnet Mask . . . . . . . . . . . : 255.255.240.0

Ethernet adapter vEthernet (Default Switch):

   IPv4 Address. . . . . . . . . . . : 172.17.240.1

Unknown adapter NordLynx:

   IPv4 Address. . . . . . . . . . . : 10.5.0.2

Ethernet adapter VirtualBox Host-Only Network:

   IPv4 Address. . . . . . . . . . . : 192.168.56.1

Ethernet adapter Ethernet:

   Connection-specific DNS Suffix  . : lan
   IPv4 Address. . . . . . . . . . . : 192.168.50.10
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.50.1
"""


class ScanTargetTests(unittest.TestCase):
    """Which /24 the search is pointed at, on a real developer's PC.

    Nothing here sends anything: local_addresses reads the interface table
    through a runner, and the runner is a canned string.
    """

    def _addresses(self, text):
        return discovery.local_addresses(runner=lambda command: text)

    def _interfaces(self, text):
        return discovery.local_interfaces(runner=lambda command: text)

    def test_the_router_s_network_wins_over_wsl_docker_and_the_vpn(self):
        chosen = shell_app._private_address(
            self._addresses(IPCONFIG_WITH_EVERYTHING))
        self.assertEqual(chosen, "192.168.50.10")

    def test_a_host_only_network_does_not_win_for_being_192_168(self):
        # 192.168.56.1 is VirtualBox's, and it sorts before 192.168.99 and
        # after 192.168.137 by nothing but the spelling of the number.
        for virtual in ("192.168.56.1", "192.168.99.1", "192.168.137.1"):
            self.assertEqual(
                shell_app._private_address([virtual, "192.168.178.22"]),
                "192.168.178.22", virtual)
            # Still better than nothing when it is all there is.
            self.assertEqual(shell_app._private_address([virtual]), virtual)

    def test_the_whole_step_from_ipconfig_to_a_list_of_hosts(self):
        address = shell_app._private_address(
            self._addresses(IPCONFIG_WITH_EVERYTHING))
        hosts = discovery.subnet_hosts(address)
        self.assertEqual(len(hosts), 253)
        self.assertEqual(hosts[0], "192.168.50.1")
        self.assertEqual(hosts[-1], "192.168.50.254")
        self.assertIn("192.168.50.95", hosts)
        self.assertNotIn(address, hosts)

    def test_the_preferred_suffix_ipconfig_prints_is_still_read(self):
        text = "   IPv4 Address. . . . . . . . . . . : 192.168.50.10(Preferred)"
        self.assertEqual(shell_app._private_address(self._addresses(text)),
                         "192.168.50.10")

    def test_a_pc_with_only_an_unplugged_adapter_has_nowhere_to_search(self):
        text = "   Autoconfiguration IPv4 Address. . : 169.254.3.4"
        self.assertEqual(shell_app._private_address(self._addresses(text)), "")

    def test_the_network_searched_can_be_named(self):
        self.assertEqual(shell_app._subnet_label("192.168.50.10"),
                         "192.168.50.0/24")
        self.assertEqual(shell_app._subnet_label("not an address"), "")

    def test_the_adapter_with_the_default_gateway_wins(self):
        # The machine from the report: the search was landing on the VMware
        # adapter's 192.168.140.0/24 while the console sat on 192.168.50.0/24.
        text = net_fixture("ipconfig_failing_machine.txt")
        chosen = discovery.choose_subnets(self._interfaces(text))
        self.assertEqual(chosen, ["192.168.50.10"])
        self.assertEqual(shell_app._private_address(self._addresses(text)),
                         "192.168.50.10")

    def test_with_no_gateway_anywhere_several_subnets_are_searched(self):
        text = net_fixture("ipconfig_no_default_gateway.txt")
        chosen = discovery.choose_subnets(self._interfaces(text))
        self.assertGreater(len(chosen), 1)
        self.assertEqual(chosen[0], "192.168.50.10")
        hosts = discovery.scan_hosts(chosen)
        self.assertEqual(len(hosts), 253 * len(chosen))
        self.assertIn("192.168.50.95", hosts)

    def test_every_network_searched_is_named(self):
        self.assertEqual(shell_app._networks_label(["192.168.50.10"]),
                         "192.168.50.0/24")
        self.assertEqual(
            shell_app._networks_label(["192.168.50.10", "192.168.140.1"]),
            "192.168.50.0/24 and 192.168.140.0/24")
        self.assertEqual(
            shell_app._networks_label(["192.168.50.10", "172.19.16.1",
                                       "192.168.140.1"]),
            "192.168.50.0/24, 172.19.16.0/24 and 192.168.140.0/24")
        self.assertEqual(shell_app._networks_label([]), "")
        # Two adapters on one wire are one network, named once.
        self.assertEqual(
            shell_app._networks_label(["192.168.50.10", "192.168.50.11"]),
            "192.168.50.0/24")


class ScanTimeoutTests(unittest.TestCase):
    """How long an address gets to answer, and where that number comes from."""

    def test_the_connect_timeout_is_no_longer_the_tight_one(self):
        # 0.4s is shorter than a PS3 on Wi-Fi takes to answer its first
        # packet, and the Check button that reaches the same console allows
        # eight seconds.
        self.assertGreaterEqual(shell_app.SCAN_CONNECT_TIMEOUT, 1.0)
        self.assertGreaterEqual(shell_app.SCAN_FETCH_TIMEOUT,
                                shell_app.SCAN_CONNECT_TIMEOUT)

    def test_a_timeout_can_be_raised_from_the_settings_file(self):
        settings = {shell_app.SCAN_CONNECT_TIMEOUT_KEY: 3,
                    shell_app.SCAN_FETCH_TIMEOUT_KEY: "6.5"}
        self.assertEqual(
            shell_app._timeout(settings, shell_app.SCAN_CONNECT_TIMEOUT_KEY,
                               shell_app.SCAN_CONNECT_TIMEOUT), 3.0)
        self.assertEqual(
            shell_app._timeout(settings, shell_app.SCAN_FETCH_TIMEOUT_KEY,
                               shell_app.SCAN_FETCH_TIMEOUT), 6.5)

    def test_a_useless_timeout_in_the_settings_file_is_ignored(self):
        for value in (0, -1, "", "soon", None, 600, [4]):
            self.assertEqual(
                shell_app._timeout({"t": value}, "t", 1.5), 1.5, repr(value))
        self.assertEqual(shell_app._timeout(None, "t", 1.5), 1.5)


class ScanDiagnosticsTests(FindTests):
    """A search that found nothing says what it did, not just what it did not
    find. Three quite different faults all end in no PS3 found, and without
    this line there is no telling them apart from the other end of a phone."""

    def test_nothing_answering_at_all_names_the_network_and_the_timeout(self):
        self._run_find()
        text = self.bar.scan_text()
        self.assertIn("No PS3 found", text)
        self.assertIn("192.168.9.0/24", text)
        self.assertIn("192.168.9.100", text)
        self.assertIn("5 addresses", text)
        self.assertIn("port 80", text)

    def test_something_that_answered_but_is_not_webman_is_named(self):
        self.answers["192.168.9.1"] = fixture("router_root.html")
        self._run_find()
        text = self.bar.scan_text()
        self.assertIn("No PS3 found", text)
        self.assertIn("1 answered on port 80", text)
        self.assertIn("192.168.9.1", text)

    def test_the_diagnostics_go_to_the_scan_line_and_nowhere_near_the_pill(
            self):
        self.connection.set_host("192.168.1.50")
        self.connection.set_connection("connected", "webMAN answered.")
        self._run_find()
        self.assertEqual(self.connection.connection, "connected")
        self.assertNotIn("192.168.9.0/24", self.bar.visible_text())
        self.assertIn("192.168.9.0/24", self.bar.scan_text())

    def test_a_search_that_worked_is_not_buried_in_diagnostics(self):
        self.answers["192.168.9.3"] = fixture("webman_root.html")
        self._run_find()
        self.assertEqual(self.bar.scan_text(), "Found a PS3 at 192.168.9.3.")

    def test_progress_says_how_many_have_answered_so_far(self):
        self.bar._scan_progress((12, 254, 3))
        self.assertIn("12 of 254", self.bar.scan_text())
        self.assertIn("3 answered", self.bar.scan_text())
        self.bar._scan_progress((12, 254))
        self.assertIn("12 of 254", self.bar.scan_text())

    def test_a_search_stops_when_the_window_is_closed(self):
        # should_stop is wired to the task's cancelled flag, and until now
        # nothing ever set it: a quarter of a /24 still in flight held the
        # exe open after the user had asked it to go away. The gate holds the
        # search open until the window has gone, so this does not depend on
        # which of the two wins a race.
        gate = threading.Event()

        def held(host):
            gate.wait(10)
            return False

        self.bar.scan_connect = held
        task = self.bar.find()
        self.assertIsNotNone(task)
        self.window.close()
        self.assertTrue(task.is_cancelled)
        gate.set()
        self.assertTrue(self.services.wait(10000))
        application.processEvents()
        # A cancelled search delivers no verdict at all. Saying it found
        # nothing would be a claim about a search that did not finish.
        self.assertEqual(self.connection.scan, "scanning")

    def test_the_counting_does_not_change_what_is_found(self):
        self.answers["192.168.9.2"] = fixture("webman_root.html")
        self._run_find()
        self.assertEqual(self.connection.scan, "found")
        self.assertEqual(self.connection.host, "192.168.9.2")


class ScanProbeTests(unittest.TestCase):
    """The two calls the search makes for real, against 127.0.0.1.

    The GUI's own fetch is HttpProbe(host).get("/"), and every step of it is
    checkable without a console: that "/" gets past the read-only allowlist,
    that a root page comes back whole, and that it scores over the threshold.
    MockWebmanHttp binds to the loopback and nothing else.
    """

    def setUp(self):
        self.console = MockWebmanHttp()
        self.console.start()
        self.addCleanup(self.console.stop)

    def _fetch(self, timeout):
        response = HttpProbe(self.console.address, timeout=timeout).get("/")
        return response.body if response.ok else None

    def test_the_root_page_is_on_the_read_only_allowlist(self):
        self.assertEqual(assert_safe_path("/"), "/")

    def test_a_real_root_page_is_fetched_whole_and_scores_over_the_line(self):
        body = self._fetch(shell_app.SCAN_FETCH_TIMEOUT)
        self.assertTrue(body)
        score, markers = webman_score(body)
        self.assertGreaterEqual(score, SIGNATURE_THRESHOLD)
        self.assertTrue(looks_like_webman(body))

    def test_the_tcp_probe_sees_a_listening_port(self):
        self.assertTrue(tcp_open("127.0.0.1", self.console.port,
                                 shell_app.SCAN_CONNECT_TIMEOUT))

    def test_the_tcp_probe_does_not_invent_a_console(self):
        # A port nothing is listening on, on the loopback. Refused at once.
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            closed = spare.getsockname()[1]
        self.assertFalse(tcp_open("127.0.0.1", closed, 0.5))

    def test_a_console_slower_than_the_timeout_is_simply_lost(self):
        # Why the numbers matter. A listener that accepts and then says
        # nothing is a console too busy to draw its own front page: the port
        # answers, the fetch runs out of time, and the search reports the
        # same nothing as a search of the wrong network. Nothing is wrong
        # with the allowlist, the parser or the threading.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        self.assertTrue(tcp_open("127.0.0.1", port, 0.5))
        response = HttpProbe(f"127.0.0.1:{port}", timeout=0.3).get("/")
        self.assertFalse(response.ok)
        self.assertIsNotNone(response.error)


if __name__ == "__main__":
    unittest.main()
