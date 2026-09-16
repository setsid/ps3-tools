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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from mock_webman import MockWebmanHttp
from ps3tools.shell import consolestats, icons, registry
from ps3tools.shell import launcher as launcher_module
from ps3diag.transport import Response
from ps3tools.shell.launcher import SUPPORT_ADDRESS, CardGrid, Launcher
from ps3tools.shell.theme import contrast_ratio
from ps3tools.shell.widgets import (ToolCard, all_of_it, pill_colours,
                                    pill_size, wrapped_lines)
from ps3tools.shell.screen import ConnectionState, Screen, Services
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



# --- four to a row ---------------------------------------------------------

def card_screens(count):
    """`count` throwaway screen classes, so a second row is a real case."""
    made = []
    for index in range(count):
        made.append(type(f"Screen{index}", (FirstScreen,), {
            "key": f"card{index}",
            "title": f"Tool number {index}",
            "blurb": "A card with a blurb about the length of a real one, "
                     "which is two lines.",
            "tile": f"T{index}",
            "order": 10 + index,
        }))
    return made


class CardGridTests(LauncherCase):
    """Four columns, and the same gutter either side of the row.

    A flow layout wrapped at whatever fitted and packed every row from the
    left, so the leftover width all collected on the right and a row of four
    looked shoved to one side. Both halves of that are checked here.
    """

    WIDE = 1500

    def setUp(self):
        super().setUp()
        self.launcher.resize(self.WIDE, 900)
        self.launcher.show()
        self.addCleanup(self.launcher.hide)
        self.settle()

    def settle(self):
        for _ in range(8):
            application.processEvents()

    def grid(self):
        return self.launcher._grid_host

    def shape(self, tools):
        """Row lengths for this many tools, with the placeholders counted.

        The grid holds the tools and the cards for what is still being worked
        on, so a test that expects only the tools is a test that breaks the
        day one of those is added.
        """
        self.launcher.rebuild(card_screens(tools))
        self.settle()
        return [len(row) for row in self.grid().rows()]

    def rows_for(self, count):
        full, over = divmod(count, CardGrid.COLUMNS)
        return [CardGrid.COLUMNS] * full + ([over] if over else [])

    def test_a_row_holds_at_most_four_cards(self):
        placed = 6 + len(launcher_module.COMING_SOON)
        self.assertEqual(self.shape(6), self.rows_for(placed))
        for row in self.grid().rows():
            self.assertLessEqual(len(row), CardGrid.COLUMNS)

    def test_eight_cards_fill_two_rows_before_starting_a_third(self):
        placed = 8 + len(launcher_module.COMING_SOON)
        self.assertEqual(self.shape(8), self.rows_for(placed))
        self.assertEqual(self.shape(8)[:2], [4, 4])

    def test_the_gutters_either_side_of_the_row_match(self):
        self.launcher.rebuild(card_screens(6))
        self.settle()
        left, right = self.grid().gutters()
        self.assertLessEqual(abs(left - right), 2, (left, right))

    def test_a_part_row_starts_at_the_same_left_edge(self):
        # Centring the last row on itself is the other way to get this wrong:
        # the second row is a continuation of the grid, not a block of its own.
        self.launcher.rebuild(card_screens(5))
        self.settle()
        rows = self.grid().rows()
        self.assertEqual(rows[1][0].x(), rows[0][0].x())

    def test_the_cards_in_a_row_do_not_overlap_or_leave_the_grid(self):
        self.launcher.rebuild(card_screens(6))
        self.settle()
        grid = self.grid()
        for row in grid.rows():
            for left, right in zip(row, row[1:]):
                self.assertLessEqual(left.geometry().right(), right.x())
            self.assertGreaterEqual(row[0].x(), 0)
            self.assertLessEqual(row[-1].geometry().right(), grid.width())

    def test_every_card_in_a_row_is_the_same_size(self):
        self.launcher.rebuild(card_screens(6))
        self.settle()
        sizes = {(card.width(), card.height())
                 for card in self.launcher.cards}
        self.assertEqual(len(sizes), 1, sizes)

    def test_a_card_stops_growing_and_the_leftover_becomes_even_gutter(self):
        self.launcher.resize(2600, 900)
        self.launcher.rebuild(card_screens(4))
        self.settle()
        self.assertEqual(self.launcher.cards[0].width(),
                         CardGrid.MAX_CARD_WIDTH)
        left, right = self.grid().gutters()
        self.assertGreater(left, 0)
        self.assertLessEqual(abs(left - right), 2, (left, right))

    def test_an_ordinary_window_fills_the_row_rather_than_centring_it(self):
        # A block centred under a left aligned heading reads as a mistake, so
        # the cards take the width up to the point where they stop growing.
        for width in (1280, 1440, 1920):
            columns, card_width, _height, left = CardGrid().metrics(width - 74)
            self.assertEqual(columns, CardGrid.COLUMNS, width)
            self.assertLessEqual(left, 2, (width, left))
            self.assertGreaterEqual(card_width, CardGrid.MIN_CARD_WIDTH)

    def test_a_narrow_card_is_given_a_line_for_its_blurb(self):
        # The blurbs are written to fall in two lines at the design width.
        self.launcher.resize(1440, 900)
        self.launcher.rebuild(card_screens(4))
        self.settle()
        self.assertLess(self.launcher.cards[0].width(), ToolCard.CARD_WIDTH)
        self.assertEqual(self.launcher.cards[0].height(),
                         ToolCard.CARD_HEIGHT + CardGrid.EXTRA_LINE)

    def test_a_narrow_window_drops_a_column_rather_than_clipping(self):
        # Four cards below the minimum readable width would be four cards with
        # their titles elided to nothing.
        columns = CardGrid().metrics(700)[0]
        self.assertLess(columns, CardGrid.COLUMNS)
        self.assertGreaterEqual(
            CardGrid().metrics(700)[1], CardGrid.MIN_CARD_WIDTH)

    def test_the_default_window_still_gets_four(self):
        # 1280 is what the window opens at, and the whole point of the change.
        self.assertEqual(CardGrid().metrics(1280 - 64)[0], CardGrid.COLUMNS)

    def test_the_grid_is_as_tall_as_the_rows_in_it(self):
        self.launcher.rebuild(card_screens(6))
        self.settle()
        grid = self.grid()
        card = self.launcher.cards[0]
        self.assertEqual(grid.height(), 2 * card.height() + CardGrid.GAP)


# --- the face of a card ----------------------------------------------------

class BadgedScreen(FirstScreen):
    key = "badged"
    title = "Modern Warfare 3 patch"
    blurb = "A screen with the longest title any of these carry."
    tile = "H1"
    badge = "Beta"


#: The longest titles the program carries, with a badge on them, which is the
#: worst case for room on a card.
GAME_FIXES = ("Black Ops II patch", "Modern Warfare 3 patch",
              "Black Ops 1 stats fix")

#: Every window the program supports, from the smallest it can be opened at.
WINDOWS = (1024, 1280, 1440, 1600, 1920, 2560)

#: What the launcher's own margins take out of the window before the grid
#: gets a say. 32 either side, as the body layout sets.
MARGINS = 64


class CardTitleTests(LauncherCase):
    """One size of title on every card, and none of them cut short.

    Drawing the game fixes larger than the tools beside them was tried and
    looked wrong: a grid of cards the same shape with one set of titles
    swollen reads as a mistake rather than as emphasis. What was kept from it
    is the wrapping, which every card now has.
    """

    def card(self, screen_class, width=ToolCard.CARD_WIDTH):
        self.launcher.rebuild([screen_class])
        card = self.launcher.cards[0]
        card.resize(width, ToolCard.CARD_HEIGHT)
        return card

    def test_every_card_sets_its_title_at_the_same_size(self):
        plain = self.card(FirstScreen).title_font()
        fix = self.card(BadgedScreen).title_font()
        self.assertEqual(fix.pointSizeF(), plain.pointSizeF())
        self.assertEqual(fix.weight(), plain.weight())
        self.assertEqual(plain.pointSizeF(),
                         self.card(FirstScreen).font().pointSizeF()
                         + ToolCard.TITLE_POINTS)

    def test_a_short_title_is_still_one_line(self):
        card = self.card(FirstScreen)
        self.assertEqual(card.title_lines(), [FirstScreen.title])

    def test_the_card_takes_the_badge_off_the_screen_class(self):
        self.launcher.rebuild([BadgedScreen])
        self.assertEqual(self.launcher.cards[0].badge, "Beta")

    def test_a_badge_added_to_a_screen_reaches_the_card(self):
        # The grid is left alone when nothing on it has changed, so anything
        # that shows on a card has to be part of what "changed" means. This
        # screen says the same words as the one before it and differs only in
        # the badge.
        class Later(FirstScreen):
            badge = "Beta"

        self.launcher.rebuild([FirstScreen])
        self.assertEqual(self.launcher.cards[0].badge, "")
        self.launcher.rebuild([Later])
        self.assertEqual(self.launcher.cards[0].badge, "Beta")

    def test_a_title_is_never_cut_short_on_a_window_it_supports(self):
        for window in WINDOWS:
            width = CardGrid().metrics(window - MARGINS)[1]
            for title in GAME_FIXES:
                card = ToolCard("fix", title, "blurb", "FX", self.theme,
                                badge="Beta")
                self.addCleanup(card.deleteLater)
                card.resize(width, ToolCard.CARD_HEIGHT)
                self.assertTrue(
                    all_of_it(title, card.title_lines()),
                    f"{title!r} at a {window} window is "
                    f"{card.title_lines()}")

    def test_a_long_title_takes_a_second_line_rather_than_an_ellipsis(self):
        card = ToolCard("fix", "Modern Warfare 3 patch", "blurb", "FX",
                        self.theme)
        self.addCleanup(card.deleteLater)
        card.resize(CardGrid.MIN_CARD_WIDTH, ToolCard.CARD_HEIGHT)
        lines = card.title_lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all_of_it("Modern Warfare 3 patch", lines))

    def test_the_title_never_takes_more_than_the_band_beside_the_icon(self):
        card = ToolCard("fix", " ".join(["Extremely"] * 12), "blurb", "FX",
                        self.theme)
        self.addCleanup(card.deleteLater)
        card.resize(ToolCard.CARD_WIDTH, ToolCard.CARD_HEIGHT)
        self.assertLessEqual(len(card.title_lines()), ToolCard.TITLE_LINES)

    def test_a_card_with_a_badge_paints_in_both_themes(self):
        card = self.card(BadgedScreen)
        for mode in ("light", "dark"):
            self.theme.set_mode(mode)
            self.assertGreater(len(ink_pixels(card.grab())), 100, mode)


class BadgePillTests(LauncherCase):
    """The pill, which is the one new thing on a card and has to be read."""

    def test_the_word_is_legible_on_the_pill_in_both_themes(self):
        for mode in ("light", "dark"):
            fill, ink, _edge = pill_colours(AppTheme(mode))
            ratio = contrast_ratio(ink, fill)
            self.assertGreaterEqual(
                ratio, 4.5, f"{mode}: the badge reads at {ratio:.2f}:1")

    def test_the_pill_stands_out_from_the_card_it_sits_on(self):
        for mode in ("light", "dark"):
            theme = AppTheme(mode)
            fill, _ink, _edge = pill_colours(theme)
            for ground in ("surface", "bg"):
                self.assertNotEqual(fill, theme.colour(ground), mode)

    def test_the_pill_costs_the_title_nothing(self):
        # It sits on the action row, which is empty to the right of "Open".
        # In the top corner it would take its width out of the one thing on a
        # headline card that was given more room rather than less.
        bare = ToolCard("fix", "A tool", "blurb", "FX", self.theme)
        badged = ToolCard("fix", "A tool", "blurb", "FX", self.theme,
                          badge="Beta")
        for card in (bare, badged):
            self.addCleanup(card.deleteLater)
            card.resize(ToolCard.CARD_WIDTH, ToolCard.CARD_HEIGHT)
        self.assertEqual(badged.title_box(), bare.title_box())
        # And the card itself is untouched, so the grid is the grid.
        self.assertEqual(badged.size(), bare.size())

    def test_a_badge_changes_what_is_drawn(self):
        bare = ToolCard("fix", "A tool", "blurb", "FX", self.theme)
        badged = ToolCard("fix", "A tool", "blurb", "FX", self.theme,
                          badge="Beta")
        for card in (bare, badged):
            self.addCleanup(card.deleteLater)
            card.resize(ToolCard.CARD_WIDTH, ToolCard.CARD_HEIGHT)
        self.assertNotEqual(badged.grab().toImage(), bare.grab().toImage())

    def test_the_pill_is_big_enough_for_its_own_word(self):
        card = ToolCard("fix", "A tool", "blurb", "FX", self.theme,
                        badge="Beta")
        self.addCleanup(card.deleteLater)
        size = pill_size(card.font(), "Beta")
        self.assertGreater(size.width(), 0)
        self.assertGreater(size.height(), 0)
        self.assertGreater(pill_size(card.font(), "Beta test").width(),
                           size.width())

    def test_a_badge_is_words_and_never_a_picture(self):
        for entry in launcher_module.COMING_SOON:
            badge = entry[5]
            self.assertTrue(badge.isascii(), badge)
            self.assertTrue(badge.replace(" ", "").isalnum(), badge)

    def test_a_card_with_no_badge_keeps_the_whole_band_for_its_title(self):
        bare = ToolCard("fix", "A tool", "blurb", "FX", self.theme)
        self.addCleanup(bare.deleteLater)
        bare.resize(ToolCard.CARD_WIDTH, ToolCard.CARD_HEIGHT)
        self.assertEqual(bare.badge, "")
        # QRect.right() is the last pixel inside, not the edge past it.
        self.assertEqual(bare.title_box().right() + 1,
                         bare.body_rect().right() - ToolCard.PADDING)


class NotedScreen(FirstScreen):
    key = "noted"
    title = "A tool with a caveat"
    blurb = "A screen that works, with something known wrong with it."
    tile = "N1"
    note = "Digital releases are not supported yet."


class CardCaveatTests(LauncherCase):
    """The line on a card saying what is known to be wrong with the tool."""

    def cards(self, *screen_classes):
        self.launcher.rebuild(list(screen_classes))
        self.launcher.resize(1500, 900)
        self.launcher.show()
        self.addCleanup(self.launcher.hide)
        for _ in range(8):
            application.processEvents()
        return self.launcher.cards

    def test_the_caveat_reaches_the_card(self):
        card = self.cards(NotedScreen)[0]
        self.assertEqual(card.note, NotedScreen.note)
        self.assertTrue(card.note_lines())

    def test_a_tool_with_nothing_wrong_with_it_has_no_caveat(self):
        card = self.cards(FirstScreen)[0]
        self.assertEqual(card.note, "")
        self.assertEqual(card.note_lines(), [])
        self.assertEqual(card.note_height(), 0)

    def test_the_whole_caveat_is_drawn_and_not_cut_short(self):
        for window in WINDOWS:
            width = CardGrid().metrics(window - MARGINS)[1]
            card = ToolCard("noted", "A tool", "blurb", "N1", self.theme,
                            note=NotedScreen.note)
            self.addCleanup(card.deleteLater)
            card.resize(width, ToolCard.CARD_HEIGHT)
            self.assertTrue(all_of_it(NotedScreen.note, card.note_lines()),
                            f"at a {window} window: {card.note_lines()}")

    def test_the_caveat_is_in_the_tooltip_and_the_description(self):
        # On the card it is drawn short. Somebody reading it with anything
        # other than their eyes gets the whole of it.
        card = self.cards(NotedScreen)[0]
        self.assertIn(NotedScreen.note, card.toolTip())
        self.assertIn(NotedScreen.blurb, card.toolTip())
        self.assertIn(NotedScreen.note, card.accessibleDescription())

    def test_the_caveat_buys_the_card_its_own_room(self):
        # Not taken out of the blurb: the blurb is the screen's own sentence
        # and half of one is worse than none.
        noted = ToolCard("noted", "A tool", "blurb", "N1", self.theme,
                         note=NotedScreen.note)
        plain = ToolCard("plain", "A tool", "blurb", "P1", self.theme)
        for card in (noted, plain):
            self.addCleanup(card.deleteLater)
            card.resize(ToolCard.CARD_WIDTH, ToolCard.CARD_HEIGHT)
        self.assertGreater(noted.note_height(), 0)
        self.assertEqual(plain.note_height(), 0)

    def test_one_caveat_makes_the_whole_row_taller(self):
        cards = self.cards(FirstScreen, NotedScreen)
        # Every card in a row is the same height, or it is not a row.
        self.assertEqual(cards[0].height(), cards[1].height())
        self.assertGreater(cards[0].height(), ToolCard.CARD_HEIGHT)

    def test_a_grid_with_no_caveat_in_it_is_the_height_it_always_was(self):
        cards = self.cards(FirstScreen, SecondScreen)
        self.assertEqual(cards[0].height(), ToolCard.CARD_HEIGHT)

    def test_a_caveat_added_to_a_screen_reaches_the_card(self):
        class Later(FirstScreen):
            note = "Something is wrong with it."

        self.launcher.rebuild([FirstScreen])
        self.assertEqual(self.launcher.cards[0].note, "")
        self.launcher.rebuild([Later])
        self.assertEqual(self.launcher.cards[0].note, Later.note)

    def test_it_is_drawn_in_the_warning_colour_in_both_themes(self):
        # A caveat drawn in the dim colour is one nobody reads, and this is
        # the line that decides whether somebody presses Open at all.
        card = ToolCard("noted", "A tool", "blurb", "N1", self.theme,
                        note=NotedScreen.note)
        self.addCleanup(card.deleteLater)
        card.resize(ToolCard.CARD_WIDTH, ToolCard.CARD_HEIGHT + 40)
        for mode in ("light", "dark"):
            self.theme.set_mode(mode)
            warn = QColor(self.theme.colour("warn"))
            image = card.grab().toImage()
            box = card.note_box()
            found = any(
                image.pixelColor(x, y) == warn
                for y in range(box.top(), box.bottom() + 1)
                for x in range(box.left(), box.right() + 1))
            self.assertTrue(found, f"{mode}: no warn pixels in the caveat")


class WrappingTests(unittest.TestCase):
    """The line breaking the titles are drawn with.

    Qt will word wrap a drawn string on its own and will run out of the bottom
    of the box doing it, so the lines are decided up front. That decision is
    checked here rather than by looking at a rendered card.
    """

    def setUp(self):
        from PySide6.QtGui import QFont, QFontMetrics
        self.metrics = QFontMetrics(QFont())

    def test_a_title_that_fits_is_left_in_one_piece(self):
        text = "Back up save data"
        room = self.metrics.horizontalAdvance(text) + 20
        self.assertEqual(wrapped_lines(self.metrics, text, room, 2), [text])

    def test_a_title_exactly_the_width_it_is_given_keeps_its_last_word(self):
        # Qt's own elidedText cuts a string whose advance is exactly the width
        # it was handed, which is a word lost for nothing.
        text = "Black Ops II patch"
        room = self.metrics.horizontalAdvance(text)
        self.assertEqual(wrapped_lines(self.metrics, text, room, 1), [text])

    def test_it_takes_a_second_line_rather_than_cutting(self):
        text = "Modern Warfare 3 patch"
        room = self.metrics.horizontalAdvance("Modern Warfare 3")
        lines = wrapped_lines(self.metrics, text, room, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all_of_it(text, lines))

    def test_it_cuts_only_once_it_is_out_of_lines(self):
        text = "Modern Warfare 3 patch"
        room = self.metrics.horizontalAdvance("Modern")
        lines = wrapped_lines(self.metrics, text, room, 1)
        self.assertEqual(len(lines), 1)
        self.assertFalse(all_of_it(text, lines))

    def test_nothing_to_say_is_no_lines_rather_than_one_empty_one(self):
        self.assertEqual(wrapped_lines(self.metrics, "", 100, 2), [])

    def test_one_word_wider_than_the_box_is_still_cut_to_the_box(self):
        text = "Supercalifragilisticexpialidocious"
        room = self.metrics.horizontalAdvance("Super")
        lines = wrapped_lines(self.metrics, text, room, 2)
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(self.metrics.horizontalAdvance(lines[0]), room)


# --- the console stats strip -----------------------------------------------

CPURSX_PARTIAL = """
<html><body>
CPU: 61&deg;C
</body></html>
"""

CPURSX_HOT = """
<html><body>
CPU: 84&deg;C RSX: 72&deg;C
Fan Speed: 77% (manual)
</body></html>
"""


class Canned:
    """A probe that answers from a dictionary. Never opens a socket."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.asked = []
        self.host = None
        self.timeout = None

    def __call__(self, host, timeout=None):
        self.host = host
        self.timeout = timeout
        return self

    def get(self, path):
        self.asked.append(path)
        return Response(path, 200, self.bodies.get(path, ""), "text/html")


class NoRealProbe:
    """Stands in for ps3diag.transport.HttpProbe and refuses to be one.

    The regression this exists for: a seam was added, nothing checked that it
    was the only route out, an inert path went live inside the suite and the
    tests started reaching the LAN. An injectable factory proves nothing on
    its own; this does, because it is the real name and it records every
    attempt to use it.
    """

    constructed = []

    def __init__(self, *args, **kwargs):
        NoRealProbe.constructed.append((args, kwargs))
        raise AssertionError("the real HttpProbe was constructed by a test")


class StatsCase(unittest.TestCase):
    """Services, and a parent to hang a strip off.

    The launcher is built by the tests that want one rather than here. A
    launcher builds a strip of its own, wired to the real client, and a case
    that then builds a second strip with a stub in it would have two strips
    racing for the same connection -- with one of them fetching for real. The
    guard below catches exactly that, which is how this arrangement was
    arrived at.
    """

    def setUp(self):
        self._saved = registry.screens()
        registry.clear()
        self.theme = AppTheme("light")
        self.connection = ConnectionState("")
        self.services = Services(self.connection, self.theme, {})
        self.host = QWidget()
        # Shown, because a strip only reads a console for a page somebody has
        # in front of them.
        self.host.show()
        self.addCleanup(self.host.hide)
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self._restore)
        # Added last so it runs first: a worker still out when the widget it
        # will call back into is torn down is a RuntimeError in a slot, on
        # somebody else's test.
        self.addCleanup(self.services.wait, 10000)

    def _restore(self):
        registry.clear()
        for screen_class in self._saved:
            registry.register(screen_class)

    def make_launcher(self, shown=True):
        launcher = Launcher(self.theme, services=self.services)
        self.addCleanup(launcher.deleteLater)
        if shown:
            launcher.resize(1400, 900)
            launcher.show()
            self.addCleanup(launcher.hide)
            for _ in range(6):
                application.processEvents()
        return launcher

    def settle(self):
        self.assertTrue(self.services.wait(10000))
        for _ in range(6):
            application.processEvents()

    def connect_to(self, host):
        self.connection.set_host(host)
        self.connection.set_connection("connected", "")
        self.settle()

    def values(self):
        return {key: value for key, _label, value, _token
                in self.strip.fields()}


class StatsStripTests(StatsCase):
    """The strip on a real home screen, against the loopback mock console."""

    def setUp(self):
        super().setUp()
        self.launcher = self.make_launcher()
        self.strip = self.launcher.stats

    def test_the_strip_is_absent_with_no_address(self):
        self.launcher.rebuild([FirstScreen])
        self.assertIsNotNone(self.strip)
        self.assertFalse(self.strip.isVisibleTo(self.launcher))
        self.assertEqual(self.strip.fields(), ())

    def test_the_strip_is_absent_when_the_console_cannot_be_reached(self):
        self.connection.set_host("127.0.0.1:1")
        self.connection.set_connection("unreachable", "nothing there")
        self.settle()
        self.assertFalse(self.strip.isVisibleTo(self.launcher))
        self.assertEqual(self.strip.fields(), ())

    def test_nothing_is_fetched_until_the_connection_is_made(self):
        self.connection.set_host("127.0.0.1:1")
        self.connection.set_connection("checking", "asking")
        self.settle()
        self.assertFalse(self.strip.busy)
        self.assertEqual(self.strip.fields(), ())

    def test_a_console_that_answers_puts_its_figures_on_the_page(self):
        console = MockWebmanHttp().start()
        self.addCleanup(console.stop)
        self.connect_to(console.address)
        values = self.values()
        self.assertEqual(values.get("cpu"), "61 \u00b0C")
        self.assertEqual(values.get("rsx"), "54 \u00b0C")
        self.assertEqual(values.get("fan"), "41% manual")
        self.assertEqual(values.get("firmware"), "4.93 CEX")
        self.assertEqual(values.get("uptime"), "3h 42m")
        self.assertTrue(self.strip.isVisibleTo(self.launcher))
        # Only the two read-only pages, and only over GET.
        self.assertEqual(sorted({path for _verb, path in console.requests}),
                         ["/", "/cpursx.ps3"])
        self.assertEqual({verb for verb, _path in console.requests}, {"GET"})

    def test_a_console_that_goes_away_takes_the_strip_with_it(self):
        console = MockWebmanHttp().start()
        self.addCleanup(console.stop)
        self.connect_to(console.address)
        self.assertTrue(self.strip.fields())
        self.connection.set_connection("unreachable", "gone")
        self.settle()
        self.assertEqual(self.strip.fields(), ())
        self.assertFalse(self.strip.isVisibleTo(self.launcher))

    def test_a_console_that_says_nothing_useful_shows_nothing_at_all(self):
        console = MockWebmanHttp(routes={}).start()
        self.addCleanup(console.stop)
        self.connect_to(console.address)
        self.assertEqual(self.strip.fields(), ())
        self.assertFalse(self.strip.isVisibleTo(self.launcher))

    def test_the_gap_under_the_strip_comes_and_goes_with_it(self):
        console = MockWebmanHttp().start()
        self.addCleanup(console.stop)
        self.assertFalse(self.launcher._stats_gap.isVisibleTo(self.launcher))
        self.connect_to(console.address)
        self.assertTrue(self.launcher._stats_gap.isVisibleTo(self.launcher))
        self.connection.set_connection("unreachable", "gone")
        self.settle()
        self.assertFalse(self.launcher._stats_gap.isVisibleTo(self.launcher))

    def test_the_manual_refresh_reads_the_console_again(self):
        console = MockWebmanHttp().start()
        self.addCleanup(console.stop)
        self.connect_to(console.address)
        before = len(console.requests)
        self.strip._refresh.click()
        self.settle()
        self.assertGreater(len(console.requests), before)
        self.assertTrue(self.strip.fields())

    def test_there_is_no_timer_reading_the_console_behind_anyone_s_back(self):
        console = MockWebmanHttp().start()
        self.addCleanup(console.stop)
        self.connect_to(console.address)
        after_connect = len(console.requests)
        for _ in range(40):
            application.processEvents()
        self.settle()
        self.assertEqual(len(console.requests), after_connect)



class OffScreenTests(StatsCase):
    """A home screen nobody has open does not go and read anybody's console.

    This is a network rule as much as a courtesy one. Several tests elsewhere
    drive a window that was never shown as far as "connected", using invented
    addresses on the real subnet; without this the strip would have answered
    that signal with a request to a stranger's machine.
    """

    def test_connecting_behind_a_window_nobody_opened_reads_nothing(self):
        launcher = self.make_launcher(shown=False)
        console = MockWebmanHttp().start()
        self.addCleanup(console.stop)
        self.connection.set_host(console.address)
        self.connection.set_connection("connected", "")
        self.settle()
        self.assertEqual(console.requests, [])
        self.assertEqual(launcher.stats.fields(), ())

    def test_the_reading_owed_is_taken_when_the_page_comes_up(self):
        launcher = self.make_launcher(shown=False)
        console = MockWebmanHttp().start()
        self.addCleanup(console.stop)
        self.connection.set_host(console.address)
        self.connection.set_connection("connected", "")
        self.settle()
        launcher.resize(1400, 900)
        launcher.show()
        self.addCleanup(launcher.hide)
        self.settle()
        self.assertEqual(sorted({path for _verb, path in console.requests}),
                         ["/", "/cpursx.ps3"])
        self.assertTrue(launcher.stats.fields())

    def test_coming_home_twice_over_does_not_read_it_twice(self):
        launcher = self.make_launcher(shown=False)
        console = MockWebmanHttp().start()
        self.addCleanup(console.stop)
        self.connection.set_host(console.address)
        self.connection.set_connection("connected", "")
        self.settle()
        launcher.resize(1400, 900)
        launcher.show()
        self.addCleanup(launcher.hide)
        self.settle()
        taken = len(console.requests)
        launcher.hide()
        launcher.show()
        self.settle()
        self.assertEqual(len(console.requests), taken)


class PartialDataTests(StatsCase):
    """webMAN 1.47.48q does not report everything, and that is not an error."""

    def strip_with(self, bodies):
        probe = Canned(bodies)
        strip = consolestats.ConsoleStats(self.services, self.host,
                                          probe_factory=probe)
        self.connection.set_host("127.0.0.1:1")
        self.connection.set_connection("connected", "")
        self.settle()
        return strip, probe

    def test_a_field_that_did_not_parse_is_left_out_not_blanked(self):
        strip, _probe = self.strip_with({"/": "", "/cpursx.ps3":
                                         CPURSX_PARTIAL})
        keys = [key for key, _label, _value, _token in strip.fields()]
        self.assertEqual(keys, ["cpu"])
        self.assertEqual(strip.fields()[0][2], "61 \u00b0C")

    def test_nothing_at_all_still_means_no_strip(self):
        strip, _probe = self.strip_with({"/": "", "/cpursx.ps3": ""})
        self.assertEqual(strip.fields(), ())
        self.assertFalse(strip.isVisibleTo(self.host))

    def test_a_fan_with_no_mode_does_not_trail_off(self):
        self.assertEqual(
            consolestats.read_fields({"fan_speed_percent": 41}),
            (("fan", "Fan", "41%", "text"),))

    def test_the_fields_keep_their_order_however_few_there_are(self):
        facts = {"uptime": "3h", "cpu_temp_c": 61.0, "firmware": "4.93"}
        keys = [key for key, _l, _v, _t in consolestats.read_fields(facts)]
        self.assertEqual(keys, ["cpu", "firmware", "uptime"])


class TemperatureColourTests(StatsCase):

    def bands(self):
        return {value: consolestats.temperature_token(value)
                for value in (20.0, 69.9, 70.0, 79.9, 80.0, 80.1, 95.0)}

    def test_the_bands_are_where_they_were_asked_to_be(self):
        self.assertEqual(self.bands(), {
            20.0: "ok", 69.9: "ok", 70.0: "warn", 79.9: "warn",
            80.0: "warn", 80.1: "error", 95.0: "error"})

    def test_a_hot_console_is_drawn_in_the_hot_tokens(self):
        probe = Canned({"/": "", "/cpursx.ps3": CPURSX_HOT})
        strip = consolestats.ConsoleStats(self.services, self.host,
                                          probe_factory=probe)
        self.connection.set_host("127.0.0.1:1")
        self.connection.set_connection("connected", "")
        self.settle()
        self.assertEqual(strip.stat("cpu").token, "error")
        self.assertEqual(strip.stat("rsx").token, "warn")
        self.assertEqual(strip.stat("cpu").value_colour(),
                         self.theme.colour("error"))
        self.assertEqual(strip.stat("fan").value_colour(),
                         self.theme.colour("text"))

    def test_a_theme_change_repaints_the_figures(self):
        probe = Canned({"/": "", "/cpursx.ps3": CPURSX_HOT})
        strip = consolestats.ConsoleStats(self.services, self.host,
                                          probe_factory=probe)
        self.connection.set_host("127.0.0.1:1")
        self.connection.set_connection("connected", "")
        self.settle()
        light = strip.stat("cpu").value.styleSheet()
        self.assertIn(AppTheme("light").colour("error").lower(),
                      light.lower())
        self.theme.set_mode("dark")
        application.processEvents()
        dark = strip.stat("cpu").value.styleSheet()
        self.assertNotEqual(dark, light)
        self.assertEqual(strip.stat("cpu").value_colour(),
                         self.theme.colour("error"))
        self.assertIn(AppTheme("dark").colour("error").lower(), dark.lower())
        # Still the same token; only what the token means has moved.
        self.assertEqual(strip.stat("cpu").token, "error")


class NothingReachesTheNetworkTests(StatsCase):
    """The guard. Not the seam -- the proof that the seam is the only way out.

    tests/check-no-network.py gates the release on this staying true, and it
    can only catch a packet that was actually sent. This catches the probe
    being constructed at all, which is one step earlier and does not depend on
    anything being listening at the other end.
    """

    def setUp(self):
        super().setUp()
        self._real = consolestats.HttpProbe
        consolestats.HttpProbe = NoRealProbe
        NoRealProbe.constructed = []
        self.addCleanup(self._put_it_back)

    def _put_it_back(self):
        consolestats.HttpProbe = self._real

    def test_building_and_driving_the_home_screen_constructs_no_probe(self):
        self.launcher = self.make_launcher()
        self.launcher.rebuild([FirstScreen, SecondScreen])
        for _ in range(8):
            application.processEvents()
        # Everything the shell does to a launcher, short of reaching a console.
        self.connection.set_host("192.168.1.42")
        self.connection.set_connection("checking", "asking")
        self.connection.set_connection("unreachable", "no answer")
        self.connection.set_scan("scanning", "looking")
        self.connection.set_scan("none", "nothing found")
        self.theme.set_mode("dark")
        self.launcher.rebuild([FirstScreen])
        self.launcher.rebuild([FirstScreen, SecondScreen])
        self.launcher.focus_first_card()
        self.settle()
        self.assertEqual(NoRealProbe.constructed, [])

    def test_connecting_with_the_page_put_away_constructs_no_probe(self):
        # The signal that does start a fetch, arriving at a strip nobody can
        # see. This is the shape the shell tests are in.
        launcher = self.make_launcher(shown=False)
        self.connection.set_host("192.168.1.42")
        self.connection.set_connection("connected", "webMAN answered")
        self.settle()
        self.assertEqual(NoRealProbe.constructed, [])
        self.assertEqual(launcher.stats.fields(), ())

    def test_the_guard_would_notice_a_fetch_it_was_not_meant_to_see(self):
        # A guard that cannot fail is not a guard. Driving the home screen the
        # way a user does -- on screen, connected -- goes through the seam
        # every time, so the empty lists above are saying something.
        seen = []
        real = consolestats.make_probe
        consolestats.make_probe = lambda host, timeout=None: seen.append(
            host) or Canned({})(host, timeout)
        try:
            launcher = self.make_launcher()
            self.connection.set_host("127.0.0.1:1")
            self.connection.set_connection("connected", "")
            self.settle()
        finally:
            consolestats.make_probe = real
        self.assertEqual(seen, ["127.0.0.1:1"])
        self.assertIsNotNone(launcher.stats)
        self.assertEqual(NoRealProbe.constructed, [])

    def test_a_fetch_goes_through_the_probe_it_was_given_and_no_other(self):
        # The half that matters most: the strip really does fetch here, and
        # the real client is still never touched.
        probe = Canned({"/": "", "/cpursx.ps3": CPURSX_HOT})
        strip = consolestats.ConsoleStats(self.services, self.host,
                                          probe_factory=probe)
        self.connection.set_host("127.0.0.1:1")
        self.connection.set_connection("connected", "")
        self.settle()
        self.assertEqual(probe.asked, ["/", "/cpursx.ps3"])
        self.assertTrue(strip.fields())
        self.assertEqual(NoRealProbe.constructed, [])

    def test_the_default_route_is_the_one_named_seam(self):
        # So that replacing make_probe in the test above is replacing the
        # thing the shipped code actually calls.
        seen = []

        def factory(host, timeout=None):
            seen.append((host, timeout))
            return Canned({})(host, timeout)

        real = consolestats.make_probe
        consolestats.make_probe = factory
        try:
            consolestats.read_console("127.0.0.1:1")
        finally:
            consolestats.make_probe = real
        self.assertEqual(seen, [("127.0.0.1:1", consolestats.TIMEOUT)])
        self.assertEqual(NoRealProbe.constructed, [])




class TheDiscordLink(LauncherCase):
    """A quiet third line in the footer, beside the other two."""

    def test_the_link_points_at_the_server(self):
        launcher = self.launcher
        self.assertIn("https://discord.gg/PDrSPNgeNj", launcher._discord.text())
        self.assertTrue(launcher._discord.openExternalLinks())

    def test_it_sits_in_the_footer_with_the_other_hints(self):
        launcher = self.launcher
        self.assertEqual(launcher._discord.objectName(), "dim")
        self.assertIs(launcher._discord.parent(), launcher._foot)

    def test_the_icon_is_in_the_set(self):
        from ps3tools.shell import icons
        self.assertIn("discord", icons.names())
        self.assertFalse(icons.pixmap("discord", "#9aa4b6", 16).isNull())

    def test_the_link_colour_follows_the_theme(self):
        launcher = self.launcher
        before = launcher._discord.text()
        self.theme.set_mode("dark")
        application.processEvents()
        self.assertNotEqual(launcher._discord.text(), before)
        self.assertIn(self.theme.colour("accent"), launcher._discord.text())


if __name__ == "__main__":
    unittest.main()
