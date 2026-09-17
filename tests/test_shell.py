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
import time
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent,
                            QPointF, QPropertyAnimation)
from PySide6.QtCore import Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from mock_webman import MockWebmanHttp
from ps3diag import discovery
from ps3diag.parsers import SIGNATURE_THRESHOLD, human_size, \
    looks_like_webman, webman_score
from ps3diag.transport import HttpProbe, assert_safe_path, tcp_open
from ps3tools.shell import app as shell_app
from ps3tools.shell import registry
from ps3tools.shell import widgets
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


class FinishingScreen(DummyScreen):
    """A screen that tells the shell when it has finished something.

    The signal is optional, the way request_tool is: most screens have nothing
    to report and the shell wires whichever ones do.
    """

    key = "finishing"
    title = "Finishing tool"
    blurb = "A screen that announces what it has just finished."
    tile = "FN"
    order = 50
    event_noted = Signal(str)


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
        # Drain the worker pool first. Each Services has a pool of its own,
        # so leftovers cannot reach another test, but work left running here
        # still runs into the widgets this teardown is about to destroy.
        if not self.services.wait(10000):
            raise AssertionError("a test left work running in the pool")
        application.processEvents()
        self.window.pages.finish_now()
        # Any graphics effect comes off before the widget it is attached to is
        # destroyed. A blur left on a window being torn down took the process
        # with it several hundred tests later, in whatever was running then.
        central = self.window.centralWidget()
        if central is not None:
            central.setGraphicsEffect(None)
        self.window.close()
        self.window.deleteLater()
        # deleteLater only queues it. Without a pump the windows pile up and
        # are destroyed at some unpredictable later moment, which is not a
        # thing a test should leave to chance.
        application.processEvents()
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


class SilentProbe:
    """A console that answers nothing, with no socket opened to find out."""

    class Response:
        ok = False
        body = ""

    def __init__(self, host, timeout=0.0):
        self.host = host

    def get(self, _path):
        return self.Response()


class TheRightHandSideOfTheBar(unittest.TestCase):
    """Free space, the firmware, and the last thing that happened.

    They sit on the telemetry strip, beside the figures they belong with. An
    earlier pass put them in the connection controls at the top right, where
    they were off the bar entirely and the firmware was reported twice, once
    there and once among the figures.

    No test here reaches a console: the strip is handed its facts directly.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.application = QApplication.instance() or QApplication([])

    FACTS = {
        "cpu_temp_c": 48, "rsx_temp_c": 51,
        "firmware": "4.93", "firmware_type": "CEX",
        "firmware_kind": "cfw", "firmware_version": "4.93",
        "cobra_version": "8.5",
        "devices": [{"device": "dev_hdd0", "free_bytes": 90_000_000_000}],
    }

    def strip(self, facts=None):
        from ps3tools.shell.consolestats import ConsoleStats
        from ps3tools.shell.screen import ConnectionState, Services
        from ps3tools.shell.theme import AppTheme
        services = Services(ConnectionState("192.168.1.50"),
                            AppTheme("dark"), {})
        strip = ConsoleStats(services, probe_factory=lambda *a, **k: None)
        self.addCleanup(strip.deleteLater)
        strip._facts = dict(self.FACTS if facts is None else facts)
        strip._paint_right()
        return strip

    def test_the_free_space_is_shown(self):
        self.assertIn("free", self.strip().free_text())

    def test_the_free_space_is_formatted_the_way_the_rest_of_the_app_is(self):
        from ps3diag.parsers import human_size
        self.assertIn(human_size(90_000_000_000), self.strip().free_text())

    def test_a_console_that_did_not_report_its_drive_shows_nothing(self):
        facts = dict(self.FACTS)
        facts.pop("devices")
        self.assertEqual(self.strip(facts).free_text(), "")

    def test_the_firmware_is_shown_once(self):
        """It used to read twice, once here and once among the figures."""
        from ps3tools.shell.consolestats import read_fields
        strip = self.strip()
        figures = " ".join(str(value)
                           for _k, _l, value, _t in read_fields(strip._facts))
        self.assertIn("Cobra 8.5", strip.firmware_text_shown())
        self.assertNotIn("CEX", figures)

    def test_a_hen_console_says_hen(self):
        facts = dict(self.FACTS, firmware_kind="hen", hen_version="3.5.0")
        facts.pop("cobra_version")
        self.assertIn("HEN", self.strip(facts).firmware_text_shown())

    def test_an_unknown_firmware_is_not_guessed_at(self):
        facts = dict(self.FACTS, firmware_kind="")
        self.assertEqual(self.strip(facts).firmware_text_shown(), "")

    def test_the_event_line_is_empty_until_something_happens(self):
        self.assertEqual(self.strip().event_text(), "")

    def test_an_event_says_what_happened_and_when(self):
        strip = self.strip()
        strip.note_event("Patched Black Ops 1")
        self.assertIn("Patched Black Ops 1", strip.event_text())
        self.assertIn("just now", strip.event_text())

    def test_the_age_is_worked_out_when_the_line_is_read(self):
        strip = self.strip()
        strip.note_event("Patched Black Ops 1")
        strip._event_at -= 245
        self.assertIn("4 min ago", strip.event_text())

    def test_clearing_the_event_empties_the_line(self):
        strip = self.strip()
        strip.note_event("Patched Black Ops 1")
        strip.note_event("")
        self.assertEqual(strip.event_text(), "")

    def test_a_console_read_fills_them_in(self):
        """The path a real console takes, which is the one that was broken.

        The three were painted from the event seam alone, so a console read
        filled in the temperatures and left the right-hand end empty. Every
        other test here set the facts and called the painter directly, which
        is exactly the step that was missing.
        """
        from ps3tools.shell.consolestats import ConsoleStats
        from ps3tools.shell.screen import ConnectionState, Services
        from ps3tools.shell.theme import AppTheme
        services = Services(ConnectionState("192.168.1.50"),
                            AppTheme("dark"), {})
        strip = ConsoleStats(services, probe_factory=lambda *a, **k: None)
        self.addCleanup(strip.deleteLater)
        strip._arrived(strip._generation,
                       {"host": "192.168.1.50", "facts": dict(self.FACTS)})
        self.assertIn("free", strip.free_text())
        self.assertIn("Cobra 8.5", strip.firmware_text_shown())

    def test_the_strip_shows_for_the_right_hand_end_alone(self):
        """A console with no temperatures can still have said how much room
        is left on its drive."""
        from ps3tools.shell.consolestats import ConsoleStats
        from ps3tools.shell.screen import ConnectionState, Services
        from ps3tools.shell.theme import AppTheme
        services = Services(ConnectionState("192.168.1.50"),
                            AppTheme("dark"), {})
        strip = ConsoleStats(services, probe_factory=lambda *a, **k: None)
        self.addCleanup(strip.deleteLater)
        facts = {"devices": [{"device": "dev_hdd0",
                              "free_bytes": 90_000_000_000}]}
        strip._arrived(strip._generation,
                       {"host": "192.168.1.50", "facts": facts})
        self.assertTrue(strip._has_right())

    def test_a_console_that_answered_nothing_leaves_all_three_blank(self):
        strip = self.strip({})
        self.assertEqual(strip.free_text(), "")
        self.assertEqual(strip.firmware_text_shown(), "")
        self.assertEqual(strip.event_text(), "")

class HowLongAgoSomethingWas(unittest.TestCase):

    def test_something_that_has_just_happened_has_no_number_on_it(self):
        self.assertEqual(shell_app.relative_time(0), "just now")
        self.assertEqual(shell_app.relative_time(59), "just now")

    def test_minutes_are_rounded_down_so_the_line_is_never_early(self):
        self.assertEqual(shell_app.relative_time(60), "1 min ago")
        self.assertEqual(shell_app.relative_time(299), "4 min ago")

    def test_an_hour_is_said_in_hours_and_a_day_in_days(self):
        self.assertEqual(shell_app.relative_time(3600), "1 hr ago")
        self.assertEqual(shell_app.relative_time(7200), "2 hr ago")
        self.assertEqual(shell_app.relative_time(90000), "1 day ago")
        self.assertEqual(shell_app.relative_time(200000), "2 days ago")


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
        """Start a search and wait for its result to reach the GUI thread.

        wait() drains the pool, but the task's result arrives as a queued
        signal and one processEvents() can return before the handler has run.
        Pumping until the pool has stayed idle across three passes is
        deterministic; a fixed pump was close enough to pass most of the time.
        """
        task = self.bar.find()
        self.assertIsNotNone(task)
        self.assertEqual(self.connection.scan, "scanning")
        deadline = time.monotonic() + 10.0
        quiet = 0
        while time.monotonic() < deadline:
            application.processEvents()
            quiet = quiet + 1 if self.services.wait(50) else 0
            if quiet >= 3:
                return task
        raise AssertionError("the search did not settle within ten seconds")

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



class TheFirstRunDialog(ShellCase):
    """Asked once, on a start with no console saved, and never after.

    It drives the connection bar rather than talking to anything itself, so
    there is still exactly one Find, one Check and one address field in this
    program. Nothing here opens a socket: the bar's own seams are untouched
    and no scan is started.
    """

    def _dialog(self):
        """The dialog, closed again however the test ends.

        It is modal and it is parented to the window, so a test that leaves
        one open hands the next one a dialog whose parent is being torn down.
        Closing it also takes the blur off the window behind it.
        """
        dialog = self.window.offer_to_find_console()
        self.assertIsNotNone(dialog)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(application.processEvents)
        self.addCleanup(dialog.reject)
        return dialog

    def test_it_is_offered_when_no_address_has_ever_been_saved(self):
        dialog = self._dialog()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.find_button.text(), "Find my PS3")

    def test_it_is_never_offered_when_an_address_is_already_saved(self):
        self.connection.set_host("192.168.1.9")
        self.assertIsNone(self.window.offer_to_find_console())

    def test_the_window_behind_it_is_blurred_and_put_back_afterwards(self):
        dialog = self._dialog()
        self.assertIsNotNone(self.window.centralWidget().graphicsEffect())
        dialog.reject()
        application.processEvents()
        self.assertIsNone(self.window.centralWidget().graphicsEffect())

    def test_the_x_dismisses_it_and_leaves_the_application_as_it_was(self):
        dialog = self._dialog()
        dialog.dismiss_button.click()
        application.processEvents()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.connection.host, "")
        # Still usable with no console, which is the whole point of the X.
        self.assertTrue(self.window.isEnabled())

    def test_a_console_answering_draws_the_tick_and_closes_itself(self):
        dialog = self._dialog()
        dialog.LINGER_MS = 0
        self.connection.set_host("192.168.1.50")
        self.connection.set_connection("connected", "webMAN 1.47.48q")
        application.processEvents()
        self.assertTrue(dialog.tick.isVisible())
        self.assertIn("192.168.1.50", dialog.status.text())
        # The buttons get out of the way rather than sitting there inviting a
        # second press while it closes.
        self.assertTrue(dialog.find_button.isHidden())
        self.assertTrue(dialog.dismiss_button.isHidden())

    def test_typing_an_address_goes_through_the_one_address_field(self):
        dialog = self._dialog()
        checked = []
        self.window.connection_bar.check = lambda: checked.append(True)
        dialog.address.setText(" 192.168.1.77 ")
        dialog.check_button.click()
        self.assertEqual(self.connection.host, "192.168.1.77")
        self.assertEqual(checked, [True])

    def test_an_empty_address_says_so_rather_than_checking_nothing(self):
        dialog = self._dialog()
        checked = []
        self.window.connection_bar.check = lambda: checked.append(True)
        dialog.check_button.click()
        self.assertEqual(checked, [])
        self.assertIn("Find my PS3", dialog.status.text())

    def test_the_rounded_corners_have_nothing_square_behind_them(self):
        # Reported from a screenshot: the stylesheet rounds the dialog but the
        # window underneath still painted its own square, so the four corners
        # showed as darker notches.
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        from ps3tools.shell.theme import stylesheet
        application.setStyleSheet(stylesheet(self.theme))
        self.addCleanup(application.setStyleSheet, "")
        dialog = self._dialog()
        dialog.adjustSize()
        application.processEvents()
        image = QImage(dialog.size(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        dialog.render(image)
        corners = ((1, 1), (dialog.width() - 2, 1), (1, dialog.height() - 2),
                   (dialog.width() - 2, dialog.height() - 2))
        for x, y in corners:
            with self.subTest(corner=(x, y)):
                self.assertEqual(image.pixelColor(x, y).alpha(), 0)
        # And the body itself is solid. Making the window transparent on its
        # own takes the background with it and leaves the buttons floating.
        body = image.pixelColor(24, 24)
        self.assertEqual(body.alpha(), 255)
        self.assertEqual(body.name(), self.theme.colour("surface"))

    def test_the_address_box_is_wide_enough_for_its_own_placeholder(self):
        # It was showing "or type it, for exa..." and eliding the rest.
        from PySide6.QtGui import QFontMetrics
        dialog = self._dialog()
        metrics = QFontMetrics(dialog.address.font())
        needed = metrics.horizontalAdvance(dialog.address.placeholderText())
        self.assertGreaterEqual(dialog.address.minimumWidth(), needed)

    def test_searching_shows_a_spinner_and_what_it_is_doing(self):
        dialog = self._dialog()
        self.assertTrue(dialog.spinner.isHidden())
        self.connection.set_scan("scanning", "checked 48 of 253 addresses")
        application.processEvents()
        self.assertTrue(dialog.spinner.isVisible())
        # The same words the bar at the top uses, and they keep up: this is
        # the whole of what there is to show while a subnet is swept.
        self.assertEqual(dialog.status.text(), "checked 48 of 253 addresses")
        self.connection.set_scan("scanning", "checked 200 of 253 addresses")
        application.processEvents()
        self.assertEqual(dialog.status.text(), "checked 200 of 253 addresses")

    def test_the_spinner_stops_when_the_search_does(self):
        dialog = self._dialog()
        self.connection.set_scan("scanning", "checked 1 of 253 addresses")
        application.processEvents()
        self.assertTrue(dialog.spinner.isVisible())
        self.connection.set_scan("none", "Nothing answered on this network.")
        application.processEvents()
        self.assertFalse(dialog.spinner.isVisible())
        self.assertEqual(dialog.status.text(),
                         "Nothing answered on this network.")

    def test_find_is_the_bar_s_own_find_and_nothing_new(self):
        dialog = self._dialog()
        found = []
        self.window.connection_bar.find = lambda: found.append(True)
        dialog.find_button.click()
        self.assertEqual(found, [True])


class TheTick(ShellCase):
    def test_it_draws_itself_and_says_when_it_has(self):
        from ps3tools.shell.app import AnimatedTick
        done = []
        tick = AnimatedTick("#4caf50", parent=self.window)
        tick.finished.connect(lambda: done.append(True))
        self.addCleanup(tick.deleteLater)
        tick.start()
        tick.set_progress(1.0)
        self.assertEqual(tick.get_progress(), 1.0)
        # Painting at every stage must not throw: the tick is drawn in two
        # halves and the second one only starts part way through.
        for step in (0.0, 0.3, 0.5, 0.7, 1.0):
            tick.set_progress(step)
            tick.render(tick.grab())


def _blues(image):
    """Every strongly blue pixel in an image, as "#rrggbb"."""
    seen = set()
    for x in range(0, image.width(), 3):
        for y in range(0, image.height(), 3):
            colour = image.pixelColor(x, y)
            if (colour.alpha() > 250
                    and colour.blue() - (colour.red() + colour.green()) / 2
                    > 60):
                seen.add(colour.name())
    return seen


class TheBrandLogo(ShellCase):
    """The wordmark in the top left, and what happens without the file."""

    def test_the_logo_is_shown_rather_than_the_words(self):
        logo = self.window.brand_logo
        if logo is None:
            self.skipTest("logo.png is not in this checkout")
        self.assertFalse(logo.pixmap().isNull())
        self.assertIn("setsid", logo.accessibleName())

    def test_the_neutrals_turn_round_for_a_light_palette(self):
        logo = self.window.brand_logo
        if logo is None:
            self.skipTest("logo.png is not in this checkout")
        # Rendered large: at bar size the wordmark is 26 pixels tall and
        # every stroke is part anti-aliased, so there is no pixel to read.
        logo._height = 120
        logo._cache.clear()
        dark = logo._render(True)
        light = logo._render(False)

        def brightest_neutral(image):
            """Where the lightest grey pixel is: the "PS3" half of the mark."""
            best = None
            for x in range(image.width()):
                for y in range(image.height()):
                    colour = image.pixelColor(x, y)
                    if colour.alpha() < 250:
                        continue
                    blueness = (colour.blue()
                                - (colour.red() + colour.green()) / 2)
                    if blueness > 60:
                        continue        # the brand blue, which never changes
                    if best is None or colour.lightness() > best[0]:
                        best = (colour.lightness(), x, y)
            return best

        found = brightest_neutral(dark)
        self.assertIsNotNone(found)
        _lightness, x, y = found
        on_dark = dark.pixelColor(x, y)
        on_light = light.pixelColor(x, y)
        # The same pixel, light on a dark palette and dark on a light one.
        # Without this the grey "PS3" is all but invisible on white.
        self.assertGreater(on_dark.lightness(), 180)
        self.assertLess(on_light.lightness(), 80)
        # And the brand blue is left alone in both.
        self.assertEqual(sorted(_blues(dark)), sorted(_blues(light)))

    def test_a_missing_file_falls_back_to_the_words(self):
        from ps3tools.shell.app import BrandLogo
        logo = BrandLogo("no-such-logo.png", "http://example.invalid", 26)
        self.addCleanup(logo.deleteLater)
        self.assertFalse(logo.usable)


class TheThemeButton(ShellCase):
    def test_it_is_an_icon_rather_than_a_labelled_drop_down(self):
        button = self.window.theme_button
        self.assertEqual(button.text(), "")
        self.assertFalse(button.icon().isNull())
        self.assertEqual(button.accessibleName(), "Theme")
        self.assertIsNotNone(button.menu())

    def test_the_menu_arrow_is_styled_off(self):
        from ps3tools.shell.theme import stylesheet
        css = stylesheet(self.theme)
        self.assertIn("QPushButton#themeButton::menu-indicator", css)
        self.assertIn("image: none", css)



class SavingAConsole(ShellCase):
    """The control that names a console, and how it is reached.

    It used to appear only once there were two consoles, which left no way to
    make the second: the menu that adds one was behind a button that needed
    one to exist.
    """

    def bar(self):
        return self.window.connection_bar

    def connect_to(self, host="192.168.50.95"):
        self.connection.set_host(host)
        self.connection.set_connection("connected", "webMAN 1.47.48q")
        application.processEvents()
        return self.bar()._console_button

    def test_nothing_is_offered_before_a_console_answers(self):
        self.assertTrue(self.bar()._console_button.isHidden())

    def test_a_console_that_answers_can_be_saved(self):
        button = self.connect_to()
        self.assertFalse(button.isHidden())
        self.assertEqual(button.text(), "Save this console")
        # No menu while there is nothing to choose between: the press saves.
        self.assertIsNone(button.menu())

    def test_saving_it_gives_it_the_name_and_a_menu(self):
        from ps3tools import profiles
        button = self.connect_to()
        profiles.add(self.settings, host="192.168.50.95", name="Living room")
        self.bar()._paint_console_button()
        self.assertEqual(button.text(), "Living room")
        self.assertIsNotNone(button.menu())

    def test_naming_it_keeps_what_was_already_remembered(self):
        # A profile is made as soon as there is state to keep. Saving must
        # name that one rather than starting a second and orphaning the list.
        from ps3tools import profiles, updates
        self.connect_to()
        updates.remember_scan(self.settings, "192.168.50.95",
                              [updates.TitleUpdate(title_id="BLES01717",
                                                   name="BO2")])
        key = profiles.for_host(self.settings, "192.168.50.95")
        profiles.rename(self.settings, key, "Living room")
        self.assertTrue(profiles.is_saved(self.settings, "192.168.50.95"))
        self.assertIsNotNone(
            updates.remembered_scan(self.settings, "192.168.50.95"))

    def test_a_profile_on_its_own_is_not_a_saved_console(self):
        from ps3tools import profiles, updates
        self.connect_to()
        updates.remember_scan(self.settings, "192.168.50.95", [])
        self.assertFalse(profiles.is_saved(self.settings, "192.168.50.95"))
        self.assertEqual(self.bar()._console_button.text(),
                         "Save this console")

    def test_forgetting_a_console_takes_its_address_and_games(self):
        from ps3tools import profiles, updates
        self.connect_to()
        updates.remember_scan(self.settings, "192.168.50.95", [])
        key = profiles.for_host(self.settings, "192.168.50.95")
        profiles.rename(self.settings, key, "Living room")
        self.assertTrue(profiles.forget(self.settings, key))
        self.assertEqual(profiles.all_profiles(self.settings), {})
        self.assertIsNone(
            updates.remembered_scan(self.settings, "192.168.50.95"))


if __name__ == "__main__":
    unittest.main()


class TheBlackOpsOneCard(unittest.TestCase):
    """It was a placeholder that opened nothing. Now it is a tool.

    The badge it wore as a placeholder it still wears, because it says the
    same thing about the fix it always did.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.application = QApplication.instance() or QApplication([])

    def build(self):
        from ps3tools.shell import launcher as launcher_module
        theme = AppTheme("dark")
        connection = ConnectionState("")
        window = shell_app.MainWindow(Services(connection, theme, {}))
        self.addCleanup(window.deleteLater)
        return window.launcher, launcher_module

    def card(self):
        # Built from the class rather than from the registry. The registry is
        # a global that every other test in this file clears and refills, and
        # importing a module that has already been imported does not register
        # anything a second time.
        from ps3tools.screens.patcher import BlackOpsOnePatcher
        launcher, _module = self.build()
        launcher.rebuild([BlackOpsOnePatcher])
        self.assertEqual(launcher.card_keys(), ["bo1"])
        return launcher.cards[0]

    def test_it_is_a_tool_now_and_not_a_placeholder(self):
        launcher, module = self.build()
        self.assertEqual(module.COMING_SOON, ())
        self.assertEqual(launcher._placeholders, [])

    def test_it_opens_its_screen(self):
        card = self.card()
        seen = []
        card.activated.connect(seen.append)
        card.click()
        self.assertEqual(seen, ["bo1"])

    def test_it_no_longer_says_beta(self):
        """The badge went when the fix stopped being the newest of the three.

        It is watched working on the European and American discs and on every
        European language variant the table now carries, so a pill saying
        Beta was telling people to be careful about the wrong thing.
        """
        self.assertEqual(self.card().badge, "")

    def test_it_says_what_it_fixes(self):
        card = self.card()
        self.assertIn("rank 1", card.accessibleDescription())


class WhatTheRealCardsSay(unittest.TestCase):
    """The badges and caveats the shipped screens carry, on the real set.

    The point of a badge is that it is rationed. This is the test that notices
    the day somebody gives every screen one.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.application = QApplication.instance() or QApplication([])

    def screens(self):
        # Off the modules rather than out of the registry, which is a global
        # that every other test in this file clears and refills. Importing a
        # module that is already imported registers nothing a second time, so
        # a registry emptied by somebody else stays empty.
        import importlib
        import pkgutil

        import ps3tools.screens as package
        from ps3tools.shell.screen import Screen

        found = {}
        for info in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(
                f"{package.__name__}.{info.name}")
            for value in vars(module).values():
                if (isinstance(value, type) and issubclass(value, Screen)
                        and getattr(value, "key", "")):
                    found[value.key] = value
        self.assertTrue(found)
        return [found[key] for key in sorted(found)]

    def test_a_badge_is_rationed_to_the_screen_that_asked_for_one(self):
        """Nothing wears one today. The machinery stays for the next thing
        being worked on, and a grid that has held one has been tested holding
        one."""
        # None today. A page where everything is badged is a page where a
        # badge means nothing, and the one screen that wore a badge has been
        # watched working on every release the table carries.
        badged = {screen.key: screen.badge for screen in self.screens()
                  if screen.badge}
        self.assertEqual(badged, {})

    def test_no_fix_still_says_digital_releases_are_unsupported(self):
        """They are supported. Fake-signed binaries are read and written, so
        a digital release patches the way a disc one does."""
        from ps3tools.screens.patcher import PatcherScreen
        fixes = [screen for screen in self.screens()
                 if issubclass(screen, PatcherScreen)]
        self.assertTrue(fixes)
        for screen in fixes:
            note = getattr(screen, "note", "") or ""
            self.assertNotIn("igital", note, screen.key)

    def test_no_fix_still_says_it_is_failing_on_hen(self):
        """The program signs for HEN now, so the warning is out of date."""
        from ps3tools.screens.patcher import PatcherScreen
        for screen in self.screens():
            if not issubclass(screen, PatcherScreen):
                continue
            note = getattr(screen, "note", "") or ""
            self.assertNotIn("HEN", note, screen.key)

class ClosingTheWindowEndsTheProgram(unittest.TestCase):
    """Pressing the X left it running with nothing on screen.

    Reported off a real machine: the window disappeared and the process stayed
    in the task manager. The event loop had ended and the interpreter was
    waiting to join a worker that was itself waiting on a console.

    Ending it is safe at that point. Every screen that writes to a console
    refuses to close while it is writing, so whatever is still going is a read.
    """

    def test_an_ordinary_shutdown_is_left_alone(self):
        killed = []
        code = shell_app.finish(0, wait=0.0, exit_now=killed.append)
        self.assertEqual(code, 0)
        self.assertEqual(killed, [])

    def test_a_worker_that_will_not_stop_does_not_hold_the_program_open(self):
        stop = threading.Event()
        # Not a daemon, which is the whole point: Python joins these before
        # the interpreter exits, and concurrent.futures registers its
        # executors' threads for exactly that.
        worker = threading.Thread(target=stop.wait, name="stubborn")
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(stop.set)
        killed = []
        shell_app.finish(3, wait=0.05, exit_now=killed.append)
        self.assertEqual(killed, [3])

    def test_it_waits_a_moment_for_one_that_is_nearly_done(self):
        # An ordinary shutdown stays an ordinary shutdown. A worker that stops
        # of its own accord inside the grace period is joined rather than cut.
        worker = threading.Thread(target=time.sleep, args=(0.1,),
                                  name="nearly-done")
        worker.start()
        self.addCleanup(worker.join)
        killed = []
        shell_app.finish(0, wait=5.0, exit_now=killed.append)
        self.assertEqual(killed, [])

    def test_a_daemon_thread_is_never_a_reason_to_cut_the_exit(self):
        # Python does not join these, so they cannot be what is holding it.
        stop = threading.Event()
        worker = threading.Thread(target=stop.wait, name="daemon",
                                  daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(stop.set)
        killed = []
        shell_app.finish(0, wait=0.0, exit_now=killed.append)
        self.assertEqual(killed, [])

    def test_the_names_of_what_was_left_running_are_recorded(self):
        stop = threading.Event()
        worker = threading.Thread(target=stop.wait, name="image-reader")
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(stop.set)
        said = []
        from ps3tools import crashreport
        with mock.patch.object(crashreport, "note", said.append):
            shell_app.finish(0, wait=0.05, exit_now=lambda code: None)
        self.assertTrue(any("image-reader" in line for line in said), said)


# --- the animation pass ----------------------------------------------------

def hover(widget, over):
    """Put the pointer on a widget, or take it off again.

    A real event rather than a call to the handler, so that what the test
    drives is what a pointer drives.
    """
    from PySide6.QtGui import QEnterEvent
    if over:
        spot = QPointF(1.0, 1.0)
        application.sendEvent(widget, QEnterEvent(spot, spot, spot))
    else:
        application.sendEvent(widget, QEvent(QEvent.Type.Leave))


class TheConnectedDotBreathes(unittest.TestCase):
    """The one animation in this program that is meant never to stop.

    Everything else here happens once and settles. This one runs for as long
    as a console is connected, which is the whole of what it says.
    """

    def setUp(self):
        self.dot = widgets.StatusDot()
        self.addCleanup(self.dot.deleteLater)

    def test_the_breath_is_owned_by_the_dot_it_belongs_to(self):
        breath = self.dot.breathing
        self.assertIsInstance(breath, QPropertyAnimation)
        self.assertIs(breath.parent(), self.dot)
        self.assertIs(breath.targetObject(), self.dot)
        self.assertEqual(bytes(breath.propertyName()), b"breath")

    def test_one_breath_takes_between_two_and_three_seconds(self):
        self.assertEqual(self.dot.breathing.duration(),
                         widgets.DOT_BREATH_MS)
        self.assertGreaterEqual(widgets.DOT_BREATH_MS, 2000)
        self.assertLessEqual(widgets.DOT_BREATH_MS, 3000)

    def test_it_moves_between_nought_point_six_five_and_one(self):
        breath = self.dot.breathing
        self.assertEqual(widgets.DOT_BREATH_DIM, 0.65)
        self.assertEqual(widgets.DOT_BREATH_FULL, 1.0)
        self.assertEqual(breath.startValue(), widgets.DOT_BREATH_DIM)
        self.assertEqual(breath.endValue(), widgets.DOT_BREATH_DIM)
        self.assertEqual(breath.keyValueAt(0.5), widgets.DOT_BREATH_FULL)

    def test_it_eases_in_and_out_rather_than_running_at_one_speed(self):
        self.assertEqual(self.dot.breathing.easingCurve().type(),
                         QEasingCurve.Type.InOutSine)

    def test_the_connected_dot_breathes_continuously(self):
        self.assertFalse(self.dot.is_breathing())
        self.dot.set_breathing(True)
        self.assertTrue(self.dot.is_breathing())
        # The only animation in the pass that loops. Everything else is one
        # event and one run.
        self.assertEqual(self.dot.breathing.loopCount(), -1)

    def test_a_dot_that_stops_breathing_settles_at_full_opacity(self):
        self.dot.set_breathing(True)
        self.dot.set_breath(widgets.DOT_BREATH_DIM)
        self.dot.set_breathing(False)
        self.assertFalse(self.dot.is_breathing())
        self.assertEqual(self.dot.breath, widgets.DOT_BREATH_FULL)

    def test_being_told_twice_over_does_not_restart_it(self):
        self.dot.set_breathing(True)
        started = self.dot.breathing.currentTime()
        self.dot.breathing.setCurrentTime(started + 400)
        self.dot.set_breathing(True)
        self.assertGreaterEqual(self.dot.breathing.currentTime(),
                                started + 400)

    def test_the_colour_is_still_the_only_thing_the_caller_sets(self):
        # The breath is opacity and nothing else: no halo of its own and no
        # size change, so the word beside the dot does not move.
        self.dot.set_colour("#2e7d32")
        self.assertEqual(self.dot.size(), widgets.StatusDot().size())


class TheCardsLiftOnHover(unittest.TestCase):
    """Two pixels and a border. No growing, and nothing that overshoots."""

    def setUp(self):
        self.theme = AppTheme("light")
        self.card = widgets.ToolCard("dummy", "Dummy tool", "A blurb.", "DY",
                                     self.theme, badge="Beta",
                                     note="Reads only.")
        self.addCleanup(self.card.deleteLater)

    def test_the_lift_is_owned_by_the_card_it_belongs_to(self):
        lift = self.card.lift_animation
        self.assertIsInstance(lift, QPropertyAnimation)
        self.assertIs(lift.parent(), self.card)
        self.assertIs(lift.targetObject(), self.card)
        self.assertEqual(bytes(lift.propertyName()), b"lift")

    def test_it_takes_between_a_hundred_and_twenty_and_a_hundred_and_sixty(
            self):
        self.assertEqual(self.card.lift_animation.duration(),
                         widgets.ToolCard.LIFT_MS)
        self.assertGreaterEqual(widgets.ToolCard.LIFT_MS, 120)
        self.assertLessEqual(widgets.ToolCard.LIFT_MS, 160)

    def test_the_card_rises_by_two_pixels_and_does_not_scale(self):
        self.assertEqual(widgets.ToolCard.LIFT_PIXELS, 2)
        before = self.card.sizeHint()
        hover(self.card, True)
        self.card.set_lift(1.0)
        self.assertEqual(self.card.sizeHint(), before)

    def test_the_lift_runs_once_and_does_not_bounce(self):
        lift = self.card.lift_animation
        self.assertEqual(lift.loopCount(), 1)
        # OutCubic arrives and stays. An easing with an overshoot in it turns
        # a grid of twelve cards into a trampoline.
        self.assertEqual(lift.easingCurve().type(), QEasingCurve.Type.OutCubic)

    def test_the_pointer_arriving_and_leaving_runs_it_both_ways(self):
        hover(self.card, True)
        self.assertEqual(self.card.lift_animation.endValue(), 1.0)
        self.assertEqual(self.card.lift_animation.state(),
                         QAbstractAnimation.State.Running)
        hover(self.card, False)
        self.assertEqual(self.card.lift_animation.endValue(), 0.0)

    def test_the_border_moves_towards_the_accent_as_the_card_comes_up(self):
        colour = self.theme.colour
        at_rest = widgets.mix(colour("border"), colour("accent"), 0.0)
        raised = widgets.mix(colour("border"), colour("accent"), 1.0)
        self.assertEqual(at_rest, colour("border"))
        self.assertNotEqual(raised, at_rest)


class ThePillsAnswerThePointer(unittest.TestCase):
    """A warning or a beta pill that sits at one colour reads as a picture."""

    def setUp(self):
        self.theme = AppTheme("light")
        self.pill = widgets.PillBadge("Beta", self.theme)
        self.addCleanup(self.pill.deleteLater)

    def test_the_hover_is_owned_by_the_pill_it_belongs_to(self):
        hovering = self.pill.hover_animation
        self.assertIsInstance(hovering, QPropertyAnimation)
        self.assertIs(hovering.parent(), self.pill)
        self.assertIs(hovering.targetObject(), self.pill)
        self.assertEqual(bytes(hovering.propertyName()), b"hover")

    def test_it_runs_once_at_the_same_speed_as_a_card(self):
        self.assertEqual(self.pill.hover_animation.duration(),
                         widgets.PILL_HOVER_MS)
        self.assertEqual(widgets.PILL_HOVER_MS, widgets.ToolCard.LIFT_MS)
        self.assertEqual(self.pill.hover_animation.loopCount(), 1)

    def test_the_pointer_arriving_and_leaving_runs_it_both_ways(self):
        hover(self.pill, True)
        self.assertEqual(self.pill.hover_animation.endValue(), 1.0)
        hover(self.pill, False)
        self.assertEqual(self.pill.hover_animation.endValue(), 0.0)

    def test_the_border_and_the_fill_move_and_the_word_does_not(self):
        at_rest = widgets.pill_colours(self.theme, "warn", 0.0)
        raised = widgets.pill_colours(self.theme, "warn", 1.0)
        self.assertNotEqual(raised[0], at_rest[0])
        self.assertNotEqual(raised[2], at_rest[2])
        # The word keeps the colour its contrast was checked at.
        self.assertEqual(raised[1], at_rest[1])
        self.assertEqual(at_rest[1], self.theme.colour("warn"))

    def test_the_pill_redraws_itself_as_the_hover_moves(self):
        at_rest = self.pill.styleSheet()
        self.pill.set_hover(1.0)
        self.assertNotEqual(self.pill.styleSheet(), at_rest)
        self.pill.set_hover(0.0)
        self.assertEqual(self.pill.styleSheet(), at_rest)

    def test_a_card_that_writes_the_pill_s_rule_says_it_again(self):
        # A rule naming the card beats one the pill sets on itself, so a pill
        # inside a ComingSoonCard would animate and show nothing without this.
        card = widgets.ComingSoonCard("soon", "Soon", "A blurb.", "Talk",
                                      "https://example.invalid", self.theme,
                                      badge="Planned")
        self.addCleanup(card.deleteLater)
        at_rest = card.styleSheet()
        card._pill.set_hover(1.0)
        self.assertNotEqual(card.styleSheet(), at_rest)


class ThePanelSweepsOnceWhenSomethingFinishes(unittest.TestCase):
    """One event, one animation, then static.

    The sweep marks the two moments something was waited for: a console
    answering, and an upload the console has confirmed. It runs once and stops,
    because a panel that keeps moving while somebody reads it is worse than one
    that never moved.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.application = QApplication.instance() or QApplication([])

    def window(self):
        from ps3tools.shell import app as shell_app
        from ps3tools.shell.screen import ConnectionState, Services
        from ps3tools.shell.theme import AppTheme
        services = Services(ConnectionState(""), AppTheme("light"), {})
        window = shell_app.MainWindow(services)
        window.pages.animations_enabled = False
        self.addCleanup(window.deleteLater)
        return window

    def test_the_sweep_runs_once_and_stops(self):
        window = self.window()
        self.assertEqual(window._status_sweep.loopCount(), 1)
        self.assertEqual(window._status_sweep.duration(), window.SWEEP_MS)

    def test_it_is_owned_by_the_panel_it_paints(self):
        """A loose animation outliving its widget is a crash on the way out."""
        window = self.window()
        self.assertIs(window._status_sweep.parent(), window.status_bar)

    def test_an_event_sweeps_the_panel(self):
        window = self.window()
        window._status_sweep.stop()
        window.note_event("Patched Black Ops 1")
        self.assertEqual(window._status_sweep.state(),
                         QAbstractAnimation.State.Running)

    def test_clearing_the_line_does_not_sweep(self):
        window = self.window()
        window._status_sweep.stop()
        window.note_event("")
        self.assertNotEqual(window._status_sweep.state(),
                            QAbstractAnimation.State.Running)

    def test_the_theme_button_animates_when_the_theme_changes(self):
        window = self.window()
        window._paint_theme_button()
        spin = getattr(window, "_theme_spin", None)
        self.assertIsNotNone(spin)
        self.assertEqual(spin.duration(), window.THEME_SPIN_MS)
        self.assertEqual(spin.loopCount(), 1)
        self.assertIs(spin.parent(), window.theme_button)


class EveryAnimationHasACaller(unittest.TestCase):
    """The painter existing is not the same as anything calling it.

    Three times in one day something passed its tests and was never reached
    on the real path: the Apply button, the right-hand end of the strip, and
    the free space reading. These assert the caller rather than the painter.
    """

    def source_of(self, function):
        import inspect
        return inspect.getsource(function)

    def test_the_dot_is_told_to_breathe_when_a_console_connects(self):
        from ps3tools.shell.app import ConnectionBar
        self.assertIn("set_breathing", self.source_of(ConnectionBar._refresh))

    def test_the_figures_are_told_to_fade_when_a_reading_lands(self):
        from ps3tools.shell.consolestats import ConsoleStats
        self.assertIn("set_value", self.source_of(ConsoleStats._draw))

    def test_the_strip_is_told_to_arrive_or_leave_on_a_reading(self):
        from ps3tools.shell.consolestats import ConsoleStats
        self.assertIn("_arrive_or_leave", self.source_of(ConsoleStats._draw))

    def test_the_right_hand_end_is_painted_on_a_reading(self):
        from ps3tools.shell.consolestats import ConsoleStats
        self.assertIn("_paint_right", self.source_of(ConsoleStats._draw))

    def test_a_card_starts_its_lift_from_the_pointer_entering(self):
        from ps3tools.shell.widgets import ToolCard
        self.assertIn("_animate_to", self.source_of(ToolCard.enterEvent))
        self.assertIn("_animate_to", self.source_of(ToolCard.leaveEvent))
        self.assertIn("start", self.source_of(ToolCard._animate_to))

    def test_a_pill_starts_its_own_from_the_pointer_entering(self):
        from ps3tools.shell.widgets import PillBadge
        self.assertIn("_hover_to", self.source_of(PillBadge.enterEvent))
        self.assertIn("_hover_to", self.source_of(PillBadge.leaveEvent))
        self.assertIn("start", self.source_of(PillBadge._hover_to))

    def test_the_panel_sweeps_from_a_connection_and_from_an_event(self):
        from ps3tools.shell.app import ConnectionBar, MainWindow
        self.assertIn("sweep_status", self.source_of(ConnectionBar._checked))
        self.assertIn("sweep_status", self.source_of(MainWindow.note_event))

    def test_the_theme_button_spins_when_the_theme_is_applied(self):
        from ps3tools.shell.app import MainWindow
        self.assertIn("_spin_theme_button",
                      self.source_of(MainWindow._paint_theme_button))
        self.assertIn("_paint_theme_button",
                      self.source_of(MainWindow._apply_theme))
