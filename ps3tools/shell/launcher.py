"""The home screen: one card per registered tool.

The cards come from the screens the window resolved at start up and from
nothing else. There is no list of tools in this file, and adding one must
never require an edit to it.

Above the cards sits a strip of console figures, which lives in
consolestats.py and is absent whenever there is nothing to put on it.
"""

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import registry
from .consolestats import ConsoleStats
from .widgets import ComingSoonCard, IconLabel, ToolCard

SUPPORT_ADDRESS = "setsid.research@proton.me"

ADDRESS_HINT = (
    "The console shows its own address under Settings, Network Settings, "
    "Settings and Connection Status List.")

SUPPORT_HINT = "Something wrong? Write to " + SUPPORT_ADDRESS + "."

DISCORD_URL = "https://discord.gg/PDrSPNgeNj"

DISCORD_HINT = "Questions and reports in the Discord."


def _wording(screen_class):
    """Everything off a screen that ends up on the face of its card.

    Compared against what is already drawn to decide whether a rebuild has
    anything to do, so a screen attribute that shows on a card belongs here.
    A badge left out of this is a badge that only appears after the next time
    the grid happens to be torn down for some other reason.
    """
    return (screen_class.key, screen_class.title, screen_class.blurb,
            getattr(screen_class, "badge", "") or "",
            getattr(screen_class, "note", "") or "",
            registry.group_of(screen_class).key)


#: A tool being worked on, shown in the grid with the rest so that somebody
#: who wants it can see it is coming. It opens nothing.
#:
#: (key, title, blurb, link text, url, badge) -- the badge spelled the way a
#: Screen spells it, so a placeholder that becomes a real tool keeps the face
#: it had.
#:
#: Empty, and kept. The Black Ops 1 stats fix was the one entry in here and is
#: now a tool with a screen of its own, which no longer wears a badge at all.
#: The machinery stays because the next thing being worked on will want it,
#: and because a grid that has held one of these is a grid that has been
#: tested holding one.
COMING_SOON = ()


class CardGrid(QWidget):
    """The tool cards, four to a row, with the same gutter on either side.

    Not a flow layout. A flow wraps at whatever happens to fit, so the same
    build showed three cards on one machine and four on another, and the
    leftover width all collected on the right hand side because the row was
    packed from the left. The column count is decided here instead, and the
    cards are given the width that makes a row of them fill the space exactly.

    The geometry is computed rather than measured, which is also what keeps
    the old empty-home-screen bug from coming back. That bug was a container
    pinned to a fixed height of zero because it had asked its children how
    tall they were while they were still hidden. Nothing here asks a child
    anything: the height of a grid of n cards follows from n, the column
    count and the card height, all of which are known before a card is shown.
    """

    #: Four, and not "however many fit". A row of four is what the home screen
    #: is laid out around.
    COLUMNS = 4
    GAP = 18

    #: Below this a card cannot carry a tool's title without eliding most of
    #: it, so the row drops a column rather than shipping four unreadable
    #: cards. At the default window size four fit comfortably; this only bites
    #: near the 1024 minimum width.
    MIN_CARD_WIDTH = 256

    #: And above this a card is mostly empty space, so the row stops growing
    #: and the leftover becomes gutter instead -- evenly, on both sides. The
    #: figure is a third again on the design width, which is about where a
    #: 1920 pixel window lands, so on every ordinary monitor the row fills the
    #: page and sits under the heading rather than floating away from it.
    MAX_CARD_WIDTH = round(ToolCard.CARD_WIDTH * 1.3)

    #: A card narrower than it was drawn for needs a third line for its blurb.
    #: The blurbs are written to fall in two lines at ToolCard.CARD_WIDTH, and
    #: they wrap to three at anything less, so the last few words end up
    #: painted underneath the Open affordance or off the bottom edge. Any
    #: narrowing at all buys the card a line; the threshold is the design
    #: width itself rather than a guess below it, because a guess is how the
    #: first version of this shipped with a clipped blurb at 1440 pixels.
    EXTRA_LINE = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = []
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)

    # -- contents
    def set_cards(self, cards):
        self._cards = list(cards)
        self.sync_height()

    def note_room(self, card_width):
        """The extra height a caveat on any card needs at this card width.

        A row is a row: every card in the grid gets the room the largest of
        them needs, so one card carrying a caveat does not leave a ragged row.
        A card with nothing to warn about spends the room on a roomier blurb.
        """
        wanted = [0]
        for card in self._cards:
            ask = getattr(card, "note_height", None)
            if ask is not None:
                wanted.append(ask(card_width))
        return max(wanted)

    @property
    def cards(self):
        return list(self._cards)

    # -- geometry
    def metrics(self, width=None):
        """(columns, card width, card height, left gutter) at a given width."""
        usable = max(int(self.width() if width is None else width), 1)
        columns = self.COLUMNS
        while columns > 1:
            if (usable - (columns - 1) * self.GAP) / columns \
                    >= self.MIN_CARD_WIDTH:
                break
            columns -= 1
        card_width = (usable - (columns - 1) * self.GAP) // columns
        card_width = max(1, min(card_width, self.MAX_CARD_WIDTH))
        card_height = ToolCard.CARD_HEIGHT + self.note_room(card_width)
        if card_width < ToolCard.CARD_WIDTH:
            card_height += self.EXTRA_LINE
        block = columns * card_width + (columns - 1) * self.GAP
        return columns, card_width, card_height, max(0, (usable - block) // 2)

    def height_for(self, width=None):
        if not self._cards:
            return 0
        columns, _card_width, card_height, _left = self.metrics(width)
        rows = -(-len(self._cards) // columns)
        return rows * card_height + (rows - 1) * self.GAP

    def heightForWidth(self, width):
        return self.height_for(width)

    def hasHeightForWidth(self):
        return True

    def sizeHint(self):
        return QSize(self.COLUMNS * ToolCard.CARD_WIDTH
                     + (self.COLUMNS - 1) * self.GAP, self.height_for())

    def minimumSizeHint(self):
        return QSize(self.MIN_CARD_WIDTH, self.height_for())

    def sync_height(self):
        """Place the cards and take the height that follows from them."""
        columns, card_width, card_height, left = self.metrics()
        for index, card in enumerate(self._cards):
            row, column = divmod(index, columns)
            # A part row starts at the same left edge as a full one: it is the
            # continuation of the grid, not a block of its own to be centred.
            card.setGeometry(left + column * (card_width + self.GAP),
                             row * (card_height + self.GAP),
                             card_width, card_height)
        height = self.height_for()
        if height != self.minimumHeight() or height != self.maximumHeight():
            self.setMinimumHeight(height)
            self.setMaximumHeight(height)
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_height()

    # -- what a test asks it
    def rows(self):
        """The cards as they are laid out, row by row."""
        columns = self.metrics()[0]
        return [self._cards[start:start + columns]
                for start in range(0, len(self._cards), columns)]

    def gutters(self):
        """(left, right) space beside the widest row, which must match."""
        if not self._cards:
            return (0, 0)
        columns, card_width, _height, left = self.metrics()
        block = (min(columns, len(self._cards)) * card_width
                 + (min(columns, len(self._cards)) - 1) * self.GAP)
        return (left, self.width() - left - block)


class Section(QWidget):
    """One heading, one line under it, and the grid of cards below.

    A section rather than one grid of everything because the fixes and the
    rest of the tools are two different offers. The fixes write to a console
    and each one is for a single game; everything else is about the console
    itself, and most of it only reads. That was said inside each tool and
    nowhere on the page somebody chooses from.

    A section with no cards in it hides itself entirely, heading and all, so
    a build that ships only half the tools looks like a smaller program
    instead of a broken one.
    """

    #: Between the bottom of one section's cards and the next heading. Wider
    #: than the gap between rows of cards, because the whole point of the
    #: heading is that the eye stops at it.
    GAP_BELOW = 26

    def __init__(self, group, parent=None):
        super().__init__(parent)
        self.group = group
        self.setObjectName("launcherSection")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, self.GAP_BELOW)
        layout.setSpacing(0)

        self.heading = QLabel(group.heading, self)
        self.heading.setObjectName("sectionHeading")
        layout.addWidget(self.heading)

        self.blurb = QLabel(group.blurb, self)
        self.blurb.setObjectName("sectionBlurb")
        self.blurb.setWordWrap(True)
        # The same measure as the subheading above. A line of explanation run
        # out to the full width of a wide window is one nobody's eye tracks
        # back from.
        self.blurb.setMaximumWidth(720)
        self.blurb.setVisible(bool(group.blurb))
        layout.addWidget(self.blurb)
        layout.addSpacing(12)

        self.grid = CardGrid(self)
        layout.addWidget(self.grid)

    @property
    def cards(self):
        return self.grid.cards

    def set_cards(self, cards):
        self.grid.set_cards(cards)
        self.setVisible(bool(cards))

    def sync_height(self):
        self.grid.sync_height()


class Launcher(QWidget):
    """Home. Knows the registry, the theme, and nothing about any one tool."""

    open_screen = Signal(str)

    def __init__(self, theme, parent=None, services=None):
        super().__init__(parent)
        self.setObjectName("launcher")
        self._theme = theme
        self._cards = []
        self._placeholders = []
        # The window hands itself in as the parent and holds the services
        # every screen is given. Taken from there rather than added to the
        # call because the shell's construction of the launcher is not this
        # file's to change; a launcher built without either -- which is most
        # of the tests -- simply has no strip, which is the same thing it
        # shows when there is no console.
        self._services = (services if services is not None
                          else getattr(parent, "services", None))
        # Owned by the launcher so it dies with it: a bare singleShot firing
        # into a deleted widget is a crash on shutdown.
        self._resync = QTimer(self)
        self._resync.setSingleShot(True)
        self._resync.timeout.connect(self._sync_grid)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setObjectName("scrollHost")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        host = QWidget(scroll)
        host.setObjectName("launcher")
        scroll.setWidget(host)

        body = QVBoxLayout(host)
        body.setContentsMargins(32, 30, 32, 32)
        body.setSpacing(6)

        # Above everything, because it is about the console rather than about
        # the tools, and putting it between the heading and the cards would
        # cut that sentence off from the thing it is describing.
        self.stats = None
        self._stats_gap = QWidget(host)
        self._stats_gap.setFixedHeight(18)
        self._stats_gap.setVisible(False)
        if self._services is not None:
            self.stats = ConsoleStats(self._services, host)
            self.stats.visibility_changed.connect(self._stats_gap.setVisible)
            # Spanning the width rather than hugging its content. Aligned
            # left it drew as a pill about a third of the page wide, so the
            # stretch inside it had no room and the readings meant to sit at
            # the far right had nowhere to go.
            body.addWidget(self.stats)
        # A spacer item cannot be hidden, so the gap under the strip is a
        # widget that comes and goes with it. Without this the home screen
        # carries eighteen pixels of nothing at the top whenever there is no
        # console, which is exactly the empty placeholder the strip is written
        # to avoid.
        body.addWidget(self._stats_gap)

        heading = QLabel("Choose a tool", host)
        heading.setObjectName("heading")
        body.addWidget(heading)

        self._subheading = QLabel("", host)
        self._subheading.setObjectName("subheading")
        self._subheading.setWordWrap(True)
        # A measure, not the full window width: a subheading run out to 1400
        # pixels is a line nobody's eye tracks back from.
        self._subheading.setMaximumWidth(720)
        body.addWidget(self._subheading)
        body.addSpacing(22)

        # One per section, built once and shown or hidden as the tools in
        # them come and go. Built up front so that a rebuild never creates a
        # widget: a hidden card is the oldest bug this screen has had, and it
        # came from widgets appearing while the layout was settling.
        self._sections = []
        for group in registry.groups():
            section = Section(group, host)
            section.setVisible(False)
            body.addWidget(section)
            self._sections.append(section)

        self._empty = QLabel(
            "No tools are registered. This build is incomplete; reinstall it "
            "or ask whoever sent it to you for the full version.", host)
        self._empty.setObjectName("dim")
        self._empty.setWordWrap(True)
        self._empty.setVisible(False)
        body.addWidget(self._empty)

        body.addStretch(1)

        self._foot = QWidget(self)
        self._foot.setObjectName("launcherFoot")
        self._foot.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._foot.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Maximum)
        foot_rows = QVBoxLayout(self._foot)
        foot_rows.setContentsMargins(32, 14, 32, 16)
        foot_rows.setSpacing(8)

        self._footnote = QLabel(ADDRESS_HINT, self._foot)
        self._footnote.setObjectName("dim")
        self._footnote.setWordWrap(True)
        foot_rows.addLayout(self._foot_row("network", self._footnote))

        self._support = QLabel(SUPPORT_HINT, self._foot)
        self._support.setObjectName("dim")
        self._support.setWordWrap(True)
        # Selectable so the address can be copied rather than copied out by
        # hand; a mailto link would open whatever the desktop calls a mail
        # client, which on a machine set up for this is usually nothing.
        self._support.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        foot_rows.addLayout(self._foot_row("mail", self._support))

        # Last of the three, in the same quiet row as the rest of the footer.
        self._discord = QLabel(self._foot)
        self._discord.setObjectName("dim")
        self._discord.setWordWrap(True)
        self._discord.setOpenExternalLinks(True)
        self._discord.setToolTip(f"Open {DISCORD_URL} in your browser.")
        foot_rows.addLayout(self._foot_row("discord", self._discord))
        self._paint_discord()
        self._theme.changed.connect(self._paint_discord)

        outer.addWidget(self._foot)

        self.rebuild()

    def _paint_discord(self):
        """The link, in the theme's own accent.

        A QLabel anchor takes its colour from the palette rather than from the
        stylesheet, so the colour is written into the markup and rewritten
        whenever the theme changes.
        """
        try:
            accent = self._theme.colour("accent")
        except Exception:                                   # noqa: BLE001
            accent = "#4d8dfa"
        self._discord.setText(
            f'{DISCORD_HINT} <a href="{DISCORD_URL}" '
            f'style="color: {accent}; text-decoration: none;">Join it</a>.')

    def _foot_row(self, icon_name, label):
        """An icon and a line of text, the icon aligned to the first line."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        mark = IconLabel(icon_name, "text_dim", self._theme, 16, self._foot)
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        label.setMaximumWidth(760)
        row.addWidget(label, 1, Qt.AlignmentFlag.AlignTop)
        row.addStretch(0)
        return row

    @property
    def cards(self):
        return list(self._cards)

    def card_keys(self):
        return [card.key for card in self._cards]

    def support_address(self):
        return SUPPORT_ADDRESS

    def rebuild(self, screens=None):
        """Rebuild the grid. Cheap, and the only way cards are ever created.

        screens is the list of screen classes to draw. It is passed in rather
        than read from the registry because a mutable global re-read on every
        navigation is one more thing that can empty the home screen; whatever
        the registry does later, the launcher should be rendering the set the
        window resolved once at start up. None falls back to the registry for
        callers that have no snapshot.
        """
        if screens is None:
            screens = registry.screens()
        screens = list(screens)

        # Tearing the grid down and building an identical one is churn that
        # costs the focused card its focus and gives the layout a window in
        # which every card is hidden. Returning to the home screen is by far
        # the commonest caller and never changes a thing.
        # The placeholders are part of what is drawn, so a grid missing them
        # is a grid that still has to be built however well the tools match.
        if (self._drawn() == [_wording(item) for item in screens]
                and len(self._placeholders) == len(COMING_SOON)):
            self._apply_count(len(self._cards))
            return

        for card in self._cards + self._placeholders:
            # Hidden before it is orphaned: a parentless widget is a top-level
            # window, and one that is still visible when it becomes one
            # flashes on screen before it is collected.
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        self._placeholders = []

        # Per section, and the sections in registry order, so self._cards is
        # in the order the page reads down. card_keys() is that order and the
        # tab order follows it.
        for section in self._sections:
            mine = [item for item in screens
                    if registry.group_of(item) is section.group]
            cards = []
            for screen_class in mine:
                card = ToolCard(screen_class.key, screen_class.title,
                                screen_class.blurb, screen_class.tile,
                                self._theme, section.grid,
                                badge=getattr(screen_class, "badge", ""),
                                note=getattr(screen_class, "note", ""))
                card.activated.connect(self.open_screen)
                # Named on the card so that _drawn can tell a card that moved
                # section from one that merely changed its wording.
                card.group = section.group.key
                # Shown by hand, and this is the fix for the empty home screen
                # rather than a tidy-up. A widget built with a parent starts
                # hidden, and a hidden card is a card the user cannot see
                # however correctly it has been placed. The grid's height no
                # longer depends on measuring these -- it is arithmetic on the
                # count -- but the cards themselves still have to be shown.
                card.show()
                cards.append(card)
                self._cards.append(card)
            if section is self._sections[-1]:
                # The placeholders go at the end of the last section, in the
                # same grid as the tools. A placeholder off to one side is one
                # nobody connects to the tools it belongs beside. Kept apart
                # from _cards, which means the tools: a placeholder is not one,
                # and counting it as one would make a build with no tools in
                # it look as though it had one.
                for key, title, blurb, link_text, url, badge in COMING_SOON:
                    card = ComingSoonCard(key, title, blurb, link_text, url,
                                          self._theme, section.grid,
                                          badge=badge)
                    card.group = section.group.key
                    card.show()
                    self._placeholders.append(card)
                    cards.append(card)
            section.set_cards(cards)
        self._apply_count(len(self._cards))

    def _apply_count(self, count):
        """Everything that follows from how many cards there are."""
        self._empty.setVisible(count == 0)
        for section in self._sections:
            section.setVisible(bool(section.cards))
        self._subheading.setText(
            "Each one works on the console at the address above."
            if count else "")
        # With no tools there is no address to find and nothing to be stuck
        # on, so the footer would be two lines about a screen that is telling
        # the user the build is broken.
        self._foot.setVisible(count > 0)
        self._sync_grid()
        # Again once the layout has settled. A rebuild during a page
        # transition measures the host at whatever width it had before the
        # launcher was given its final geometry.
        self._resync.start(0)

    def _drawn(self):
        """What the cards on screen are currently saying."""
        return [(card.key, card.text(), card.accessibleDescription(),
                 card.badge, card.note, getattr(card, "group", ""))
                for card in self._cards]

    @property
    def sections(self):
        """Every section, in page order, shown and hidden alike."""
        return list(self._sections)

    @property
    def grids(self):
        """Every section's grid, in page order."""
        return [section.grid for section in self._sections]

    def section_for(self, key):
        for section in self._sections:
            if section.group.key == key:
                return section
        return None

    def _sync_grid(self):
        for section in self._sections:
            section.sync_height()
        self.updateGeometry()

    def showEvent(self, event):
        super().showEvent(event)
        # Coming home is when a reading owed since a connection was made
        # behind an open tool finally gets taken.
        if self.stats is not None:
            self.stats.wake()

    def focus_first_card(self):
        if self._cards:
            self._cards[0].setFocus()
