"""The home screen and the icon set it is drawn with.

Runs headless:

    QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests

The icons are vector art rendered at run time, so "it renders" is not a thing
that can be taken on trust: every one of them is rasterised here and looked at,
because an SVG with a typo in it renders as a perfectly valid empty square.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel

from ps3tools.shell import icons, registry
from ps3tools.shell.launcher import SUPPORT_ADDRESS, Launcher
from ps3tools.shell.screen import Screen
from ps3tools.shell.theme import AppTheme

application = None


def setUpModule():
    global application
    application = QApplication.instance() or QApplication([])


def ink_pixels(pixmap):
    """Every pixel with something drawn in it."""
    image = pixmap.toImage()
    found = []
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() > 0:
                found.append(pixel)
    return found


# --- the icon set ----------------------------------------------------------

class IconTests(unittest.TestCase):

    def setUp(self):
        icons.clear_cache()

    def test_the_set_covers_what_the_shell_asks_for(self):
        expected = {"diagnostics", "patch", "console", "check", "warning",
                    "error", "info", "search", "folder", "download",
                    "refresh", "back", "settings", "link"}
        self.assertTrue(expected.issubset(set(icons.names())),
                        expected - set(icons.names()))

    def test_every_icon_draws_something_at_every_size(self):
        for name in icons.names():
            for size in (16, 20, 24):
                pixmap = icons.pixmap(name, "#12161d", size)
                self.assertEqual(pixmap.width(), size, name)
                drawn = ink_pixels(pixmap)
                # A stroke on a 24 unit grid covers a good part of the box; a
                # handful of pixels would mean a path that failed to parse.
                self.assertGreater(len(drawn), size,
                                   f"{name} at {size} is all but empty")

    def test_an_unknown_name_is_blank_rather_than_an_error(self):
        pixmap = icons.pixmap("no-such-icon", "#12161d", 20)
        self.assertEqual(pixmap.width(), 20)
        self.assertEqual(ink_pixels(pixmap), [])
        self.assertFalse(icons.icon("no-such-icon", "#12161d").isNull())
        self.assertEqual(icons.svg("no-such-icon", "#12161d"), "")

    def test_the_colour_asked_for_is_the_colour_drawn(self):
        for name in ("check", "warning", "console"):
            red = icons.pixmap(name, "#ff0000", 24)
            blue = icons.pixmap(name, "#0000ff", 24)
            self.assertNotEqual(red.toImage(), blue.toImage(), name)
            for pixel in ink_pixels(red):
                self.assertGreater(pixel.red(), pixel.blue(), name)

    def test_both_theme_extremes_are_legible(self):
        # The one thing a drawn icon can get wrong that a glyph cannot: being
        # stroked in a colour that is not there in the other palette.
        for mode in ("light", "dark"):
            theme = AppTheme(mode)
            ground = QColor(theme.colour("surface"))
            for name in icons.names():
                for pixel in ink_pixels(icons.pixmap(
                        name, theme.colour("text"), 24)):
                    if pixel.alpha() < 200:
                        continue
                    self.assertNotEqual(pixel.rgb(), ground.rgb(),
                                        f"{mode}: {name} is drawn in the "
                                        f"surface colour")

    def test_the_same_request_is_not_rasterised_twice(self):
        first = icons.pixmap("check", "#12161d", 20)
        self.assertIs(icons.pixmap("check", "#12161d", 20), first)
        icons.clear_cache()
        self.assertIsNot(icons.pixmap("check", "#12161d", 20), first)

    def test_a_screen_key_picks_its_own_icon(self):
        self.assertEqual(icons.for_key("diagnostics"), "diagnostics")
        self.assertIn(icons.for_key("bo2"), icons.names())
        # Nothing recognisable leaves the card to its two letter tile.
        self.assertEqual(icons.for_key("zz", "Zz"), "")


# --- the home screen -------------------------------------------------------

class FirstScreen(Screen):
    key = "first"
    title = "First tool"
    blurb = "A screen that exists only to be drawn as a card."
    tile = "F1"
    order = 10


class SecondScreen(FirstScreen):
    key = "second"
    title = "Second tool"
    blurb = "A second screen, so the grid has more than one card."
    tile = "S2"
    order = 20


class RegisteredScreen(FirstScreen):
    key = "registered"
    title = "Registered tool"
    blurb = "In the registry, and not in the snapshot."
    tile = "R3"
    order = 30


class LauncherCase(unittest.TestCase):

    def setUp(self):
        self._saved = registry.screens()
        registry.clear()
        registry.register(RegisteredScreen)
        self.theme = AppTheme("light")
        self.launcher = Launcher(self.theme)

    def tearDown(self):
        self.launcher.deleteLater()
        registry.clear()
        for screen_class in self._saved:
            registry.register(screen_class)

    def labels(self):
        return [label.text() for label in self.launcher.findChildren(QLabel)]


class LauncherTests(LauncherCase):

    def test_a_card_per_screen_passed_in(self):
        self.launcher.rebuild([FirstScreen, SecondScreen])
        self.assertEqual(self.launcher.card_keys(), ["first", "second"])

    def test_the_snapshot_wins_over_the_registry(self):
        # The navigation bug this guards: the registry is a mutable global and
        # the launcher must draw what the window resolved, not what is in it
        # by the time the user comes back from a tool.
        self.launcher.rebuild([FirstScreen])
        self.assertEqual(self.launcher.card_keys(), ["first"])
        registry.clear()
        self.launcher.rebuild([FirstScreen])
        self.assertEqual(self.launcher.card_keys(), ["first"])

    def test_no_snapshot_falls_back_to_the_registry(self):
        self.launcher.rebuild()
        self.assertEqual(self.launcher.card_keys(), ["registered"])

    def test_a_card_carries_the_screen_s_own_words(self):
        self.launcher.rebuild([FirstScreen])
        card = self.launcher.cards[0]
        self.assertEqual(card.text(), FirstScreen.title)
        self.assertEqual(card.accessibleDescription(), FirstScreen.blurb)

    def test_an_unrecognised_screen_keeps_its_two_letter_tile(self):
        self.launcher.rebuild([FirstScreen])
        self.assertEqual(self.launcher.cards[0].icon_name, "")

    def test_a_card_opens_its_screen(self):
        self.launcher.rebuild([FirstScreen, SecondScreen])
        seen = []
        self.launcher.open_screen.connect(seen.append)
        self.launcher.cards[1].click()
        self.assertEqual(seen, ["second"])

    def test_the_support_address_is_on_the_page(self):
        self.launcher.rebuild([FirstScreen])
        self.assertTrue(
            any(SUPPORT_ADDRESS in text for text in self.labels()),
            self.labels())

    def test_the_address_footnote_is_still_there(self):
        self.launcher.rebuild([FirstScreen])
        self.assertTrue(
            any("Network Settings" in text for text in self.labels()))

    def test_an_empty_grid_says_so(self):
        self.launcher.rebuild([])
        self.assertEqual(self.launcher.card_keys(), [])
        self.assertTrue(any("No tools are registered" in text
                            for text in self.labels()))

    def test_a_card_paints_in_both_themes(self):
        self.launcher.rebuild([FirstScreen, SecondScreen])
        card = self.launcher.cards[0]
        card.resize(card.sizeHint())
        for mode in ("light", "dark"):
            self.theme.set_mode(mode)
            self.assertGreater(len(ink_pixels(card.grab())), 100, mode)

    def test_hover_and_press_still_move_the_card(self):
        self.launcher.rebuild([FirstScreen])
        card = self.launcher.cards[0]
        card.resize(card.sizeHint())
        resting = card.grab().toImage()
        card.set_lift(1.0)
        self.assertNotEqual(card.grab().toImage(), resting)
        card.set_lift(0.0)
        self.assertEqual(card.grab().toImage(), resting)


class LauncherOnScreenTests(LauncherCase):
    """A rebuild of a launcher that is already on screen.

    The home screen came back with a heading, a paragraph and no cards after a
    visit to a tool. The cards were all there, correctly sized and reporting
    themselves visible; the grid host around them had been measured while they
    were still hidden, been pinned to a fixed height of nought, and never
    measured again. Everything here is therefore about paint, not bookkeeping.
    """

    def setUp(self):
        super().setUp()
        self.launcher.resize(1200, 800)
        self.launcher.show()
        self.addCleanup(self.launcher.hide)
        self.settle()

    def settle(self):
        for _ in range(8):
            application.processEvents()

    def drawn(self):
        """Cards with somewhere inside the grid host to be painted in."""
        area = self.launcher._grid_host.rect()
        return [card for card in self.launcher.cards
                if card.isVisible()
                and not card.geometry().intersected(area).isEmpty()]

    def test_cards_added_to_a_launcher_on_screen_are_drawn(self):
        self.launcher.rebuild([FirstScreen, SecondScreen])
        self.settle()
        self.assertEqual([card.key for card in self.drawn()],
                         ["first", "second"])

    def test_the_grid_host_is_as_tall_as_the_cards_in_it(self):
        self.launcher.rebuild([FirstScreen, SecondScreen])
        self.settle()
        host = self.launcher._grid_host
        self.assertGreaterEqual(host.height(),
                                self.launcher.cards[0].sizeHint().height())

    def test_rebuilding_twice_over_does_not_lose_the_grid(self):
        for screens in ([FirstScreen], [FirstScreen, SecondScreen],
                        [SecondScreen], [FirstScreen, SecondScreen]):
            self.launcher.rebuild(screens)
            self.settle()
            self.assertEqual(len(self.drawn()), len(screens), screens)

    def test_the_grid_grabs_as_something_rather_than_nothing(self):
        self.launcher.rebuild([FirstScreen, SecondScreen])
        self.settle()
        pixmap = self.launcher._grid_host.grab()
        self.assertGreater(pixmap.height(), 0)
        self.assertGreater(
            len({colour.name() for colour in ink_pixels(pixmap)}), 3)

    def test_an_identical_rebuild_keeps_the_cards_it_has(self):
        self.launcher.rebuild([FirstScreen, SecondScreen])
        self.settle()
        before = self.launcher.cards
        self.launcher.rebuild([FirstScreen, SecondScreen])
        self.settle()
        for old, new in zip(before, self.launcher.cards):
            self.assertIs(old, new)
        self.assertEqual(len(self.drawn()), 2)

    def test_going_from_some_cards_to_none_still_says_so(self):
        self.launcher.rebuild([FirstScreen])
        self.settle()
        self.launcher.rebuild([])
        self.settle()
        self.assertEqual(self.drawn(), [])
        self.assertFalse(self.launcher._grid_host.isVisible())
        self.assertTrue(any("No tools are registered" in text
                            for text in self.labels()))


if __name__ == "__main__":
    unittest.main()
