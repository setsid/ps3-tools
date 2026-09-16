"""The assembled application: every screen registered, navigated between.

Everything else tests one layer. This is the one that would catch two screens
claiming the same key, a card that cannot be built, or a screen whose on_enter
throws the moment it is shown, none of which any single-layer test can see.

Written defensively on purpose: each screen is skipped rather than failing when
its module is absent, so this file is useful while the application is still
being assembled and becomes strict as the pieces land.
"""

import importlib
import os
import sys
import unittest

from support import ROOT  # noqa: F401  (puts the repo root on the path)

QT = True
try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
except ImportError:
    QT = False

SCREEN_MODULES = (
    "ps3tools.screens.diagnostics",
    "ps3tools.screens.patcher",
)


def application():
    return QApplication.instance() or QApplication([])


def load_screens():
    """Imports whichever screen modules exist. Returns the registry.

    A module already in sys.modules imports without running its register()
    again. Another file that imported these and then emptied the registry
    would leave this one looking at a launcher with no cards in it and
    calling that a missing screen, which it did: this passed on its own and
    failed after tests/test_shell.py. So where nothing is registered, a module
    that was already loaded is loaded again to put its card back.
    """
    from ps3tools.shell import registry
    if registry.screens():
        return registry
    for name in SCREEN_MODULES:
        cached = sys.modules.get(name)
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if cached is not None:
            importlib.reload(module)
    return registry


@unittest.skipUnless(QT, "PySide6 not available")
class Registration(unittest.TestCase):
    def setUp(self):
        self.app = application()
        self.registry = load_screens()

    def test_at_least_the_diagnostics_card_is_registered(self):
        keys = [screen.key for screen in self.registry.screens()]
        self.assertIn("diagnostics", keys)

    def test_every_registered_screen_can_draw_a_card(self):
        for screen in self.registry.screens():
            self.assertTrue(screen.title, screen)
            self.assertTrue(screen.blurb, screen)
            self.assertTrue(screen.tile, screen)
            # The tile goes on a card at a small size. Letters only: an emoji
            # renders differently on every machine and is banned besides.
            self.assertTrue(screen.tile.isalnum(), screen.tile)
            self.assertLessEqual(len(screen.tile), 3, screen.tile)

    def test_no_two_screens_claim_the_same_key(self):
        keys = [screen.key for screen in self.registry.screens()]
        self.assertEqual(len(keys), len(set(keys)), keys)

    def test_the_cards_are_in_a_deliberate_order(self):
        orders = [screen.order for screen in self.registry.screens()]
        self.assertEqual(orders, sorted(orders))

    def test_no_screen_says_anything_in_an_emoji(self):
        for screen in self.registry.screens():
            for text in (screen.title, screen.blurb, screen.tile):
                for character in text:
                    self.assertLess(ord(character), 0x2100,
                                    f"{screen.key}: {text!r}")


@unittest.skipUnless(QT, "PySide6 not available")
class Assembled(unittest.TestCase):
    """Builds the real window if the shell is there."""

    def setUp(self):
        self.app = application()
        self.registry = load_screens()
        try:
            self.shell = importlib.import_module("ps3tools.shell.app")
        except ImportError:
            self.skipTest("the shell is not written yet")
        if not hasattr(self.shell, "build"):
            self.skipTest("the shell exposes no build()")

    def build(self):
        # Built through the shell's own entry point rather than by assembling
        # the pieces here, so this exercises what actually runs. Settings are
        # passed in so a test run never reads or writes the user's own file.
        window = self.shell.build(self.app, settings={})
        self.addCleanup(window.deleteLater)
        return window

    def test_the_window_builds_with_every_screen_registered(self):
        window = self.build()
        self.assertTrue(window.isWidgetType())

    def test_the_console_address_is_shared_across_screens(self):
        window = self.build()
        services = getattr(window, "services", None)
        if services is None:
            self.skipTest("the shell exposes no services")
        services.connection.set_host("127.0.0.1")
        self.assertEqual(services.connection.host, "127.0.0.1")
        # Every screen sees the same object rather than a copy of the address.
        for screen in self.registry.screens():
            instance = screen(services)
            self.addCleanup(instance.deleteLater)
            self.assertIs(instance.connection, services.connection)
            self.assertEqual(instance.connection.host, "127.0.0.1")

    def test_a_failed_scan_never_makes_the_connection_look_broken(self):
        """The bug this application was partly written to fix."""
        window = self.build()
        services = getattr(window, "services", None)
        if services is None:
            self.skipTest("the shell exposes no services")
        connection = services.connection
        connection.set_host("192.168.1.42")
        connection.set_connection("connected", "Reached the console.")
        connection.set_scan("none", "No PS3 found on this network.")
        self.assertTrue(connection.connected)
        self.assertEqual(connection.connection, "connected")
        self.assertNotIn("No PS3 found", connection.connection_detail)


@unittest.skipUnless(QT, "PySide6 not available")
class ButtonColours(unittest.TestCase):
    """What a button does decides its colour, on every screen the same way.

    Every button was the same grey, including the ones that write to somebody's
    console: "Apply the fix" looked exactly like "Back". This walks the real
    screens rather than testing the helper, because the thing that goes wrong
    is a new screen forgetting to say what its button is for.
    """

    #: The one action each screen exists to perform, as a screen that has
    #: just opened. Game updates is the odd one: on arrival there is nothing
    #: to download, so the action is the scan, and it hands the colour over to
    #: the download button once there is a list. Either way, exactly one.
    PRIMARY_BUTTONS = {
        "ps3tools.screens.diagnostics": "run_button",
        "ps3tools.screens.gameupdates": "_rescan",
        "ps3tools.screens.patcher": "_patch",
        "ps3tools.screens.transfer": "_go",
        "ps3tools.screens.saves": "copy_button",
        "ps3tools.screens.installpkg": "_go",
    }

    #: Writes over what is already there. Red so it is not hit in passing.
    DANGER_BUTTONS = {
        "ps3tools.screens.patcher": "_restore",
    }

    def setUp(self):
        self.app = application()

    def _screen(self, module_name):
        from ps3tools.shell.app import AppTheme, ConnectionState
        from ps3tools.shell.screen import Services
        module = importlib.import_module(module_name)
        for candidate in vars(module).values():
            key = getattr(candidate, "key", None)
            if isinstance(key, str) and key and hasattr(candidate, "on_enter"):
                services = Services(ConnectionState(""), AppTheme("dark"), {})
                return candidate(services)
        self.skipTest(f"no screen class in {module_name}")

    def test_the_main_action_of_every_screen_is_coloured(self):
        for module_name, attribute in self.PRIMARY_BUTTONS.items():
            with self.subTest(module_name):
                screen = self._screen(module_name)
                button = getattr(screen, attribute)
                self.assertTrue(button.property("primary"), button.text())
                self.assertFalse(button.property("danger"), button.text())

    def test_what_writes_over_something_is_red(self):
        for module_name, attribute in self.DANGER_BUTTONS.items():
            with self.subTest(module_name):
                # The screen is held for the length of the check. Reading the
                # button straight off a throwaway leaves nothing referring to
                # the screen, and a screen collected between the two lines
                # takes its buttons with it on the C++ side.
                screen = self._screen(module_name)
                button = getattr(screen, attribute)
                self.assertTrue(button.property("danger"), button.text())
                self.assertFalse(button.property("primary"), button.text())

    def test_back_is_never_coloured_on_any_screen(self):
        for module_name in self.PRIMARY_BUTTONS:
            with self.subTest(module_name):
                screen = self._screen(module_name)
                back = getattr(screen, "_back", None)
                if back is None:
                    continue
                self.assertEqual(back.text(), "Back")
                self.assertFalse(back.property("primary"))
                self.assertFalse(back.property("danger"))

    def test_exactly_one_button_a_screen_is_the_primary_one(self):
        from PySide6.QtWidgets import QPushButton
        for module_name in self.PRIMARY_BUTTONS:
            with self.subTest(module_name):
                screen = self._screen(module_name)
                coloured = [button.text()
                            for button in screen.findChildren(QPushButton)
                            if button.property("primary")]
                self.assertEqual(len(coloured), 1, coloured)
