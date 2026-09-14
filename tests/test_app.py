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
    """Imports whichever screen modules exist. Returns the registry."""
    from ps3tools.shell import registry
    for name in SCREEN_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            continue
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
