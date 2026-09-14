"""The home screen: one card per registered tool.

The cards come from the screens the window resolved at start up and from
nothing else. There is no list of tools in this file, and adding one must
never require an edit to it.
"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import registry
from .widgets import FlowHost, IconLabel, ToolCard

SUPPORT_ADDRESS = "setsid.research@proton.me"

ADDRESS_HINT = (
    "Not sure of the address? On the PS3: Settings, Network Settings, "
    "Settings and Connection Status List. It is near the top of that page, "
    "and it is four numbers with dots between them.")

SUPPORT_HINT = (
    "Stuck, or something here did not do what it said? Write to "
    + SUPPORT_ADDRESS + " and say what you saw.")


def _wording(screen_class):
    """The three things off a screen that end up on the face of its card."""
    return (screen_class.key, screen_class.title, screen_class.blurb)


class Launcher(QWidget):
    """Home. Knows the registry, the theme, and nothing about any one tool."""

    open_screen = Signal(str)

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.setObjectName("launcher")
        self._theme = theme
        self._cards = []
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

        self._grid_host = FlowHost(spacing=18, parent=host)
        self._grid_host.setSizePolicy(QSizePolicy.Policy.Preferred,
                                      QSizePolicy.Policy.Fixed)
        self._grid = self._grid_host.flow
        body.addWidget(self._grid_host)

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

        outer.addWidget(self._foot)

        self.rebuild()

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
        if self._drawn() == [_wording(item) for item in screens]:
            self._apply_count(len(self._cards))
            return

        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Hidden before it is orphaned: a parentless widget is a
                # top-level window, and one that is still visible when it
                # becomes one flashes on screen before it is collected.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._cards = []

        for screen_class in screens:
            card = ToolCard(screen_class.key, screen_class.title,
                            screen_class.blurb, screen_class.tile,
                            self._theme, self._grid_host)
            card.activated.connect(self.open_screen)
            self._grid.addWidget(card)
            # Shown by hand, and this is the fix for the empty home screen
            # rather than a tidy-up. A widget built with a parent starts
            # hidden and Qt only shows one just added to a layout on the next
            # turn of the event loop; until then QWidgetItem.sizeHint() calls
            # it (0, 0). sync_height() below therefore measured the grid as
            # nothing high and pinned the host to a fixed height of zero, and
            # nothing measured it again, because sync_height only runs from
            # FlowHost.resizeEvent and a host frozen at zero never gets one.
            # The cards were all there, correctly laid out, and clipped out
            # of existence. The first build escaped it only because the
            # launcher was not on screen yet and the show that followed
            # resized the host.
            card.show()
            self._cards.append(card)

        self._apply_count(len(self._cards))

    def _apply_count(self, count):
        """Everything that follows from how many cards there are."""
        self._empty.setVisible(count == 0)
        self._grid_host.setVisible(count > 0)
        self._subheading.setText(
            "Each one works on the console at the address above, so that only "
            "gets typed once. Nothing is changed on the console until a tool "
            "says what it is about to do." if count else "")
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
        return [(card.key, card.text(), card.accessibleDescription())
                for card in self._cards]

    def _sync_grid(self):
        self._grid_host.sync_height()
        self.updateGeometry()

    def focus_first_card(self):
        if self._cards:
            self._cards[0].setFocus()
