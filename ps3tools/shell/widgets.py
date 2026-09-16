"""Small shared widgets. No domain knowledge, no console, no decisions.

The cards and the status dot are painted rather than assembled out of stock
widgets: hover, press and focus want to move together, and a QFrame holding
three QLabels cannot do that without a stylesheet per state.
"""

from PySide6.QtCore import (QEasingCurve, QPoint, QPropertyAnimation, QRect,
                            QSize, Property, Qt, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel,
                               QLayout, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget)

from . import icons


#: What a button does, which is what decides its colour. One spelling of the
#: three, so a screen cannot invent a fourth.
#:
#: PRIMARY  the one action a screen exists to perform
#: DANGER   writes over or undoes something that is already there
#: NEUTRAL  everything else: Back, Stop, Choose files, ticking helpers
PRIMARY = "primary"
DANGER = "danger"
NEUTRAL = ""


def set_role(button, role=NEUTRAL):
    """Colour one button by what it does. Returns it, so it can be chained.

    Every button was the same grey, including the ones that write to a
    console. "Apply the fix" and "Back" looked identical, which is the wrong
    way round for the two of them.
    """
    button.setProperty(PRIMARY, role == PRIMARY)
    button.setProperty(DANGER, role == DANGER)
    # A property set after the widget is styled does not repaint on its own.
    style = button.style()
    if style is not None:
        style.unpolish(button)
        style.polish(button)
    return button


#: How many lines of progress text a screen makes room for.
#:
#: A word-wrapped QLabel in a vertical layout reports the height of one line
#: until it has been laid out, and the layout settles on that. The text then
#: wraps inside a box too short for it and the fourth line is cut in half.
#: Asking for the room up front is what stops that.
PROGRESS_LINES = 4


def fit_progress_label(label, lines=PROGRESS_LINES):
    """Give a progress label room to grow to `lines` without being clipped."""
    metrics = QFontMetrics(label.font())
    label.setWordWrap(True)
    label.setMinimumHeight(metrics.lineSpacing() * lines)
    label.setSizePolicy(QSizePolicy.Policy.Preferred,
                        QSizePolicy.Policy.MinimumExpanding)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignTop)
    return label


def mix(first, second, amount):
    """`amount` of second blended into first. All three are "#rrggbb"."""
    one, two = QColor(first), QColor(second)
    return QColor(
        round(one.red() + (two.red() - one.red()) * amount),
        round(one.green() + (two.green() - one.green()) * amount),
        round(one.blue() + (two.blue() - one.blue()) * amount),
    ).name()


#: The space either side of the word in a pill, and the space above and below
#: it. Both are fixed pixels rather than a fraction of the text, because a
#: pill is a shape with a word in it and the shape should be the same on every
#: card whatever the desktop's font size does to the word.
PILL_SIDES = 9
PILL_ENDS = 3
PILL_MINIMUM_HEIGHT = 18


def pill_colours(theme, token="accent"):
    """(fill, ink, edge) for a pill in one of the theme's own colours.

    The colour on the quiet tile colour rather than white on a solid fill: a
    pill shares a card with an icon tile, and two solid shapes on one card is
    one too many. It is also the pair the palette check already guarantees at
    4.5:1 in both themes for every token it is used with, which is the reason
    for reusing it rather than mixing a wash that reads at 4.3 in the dark.
    """
    colour = theme.colour
    return (colour("surface_alt"), colour(token),
            mix(colour("border"), colour(token), 0.55))


def pill_font(base):
    """The font a pill's word is set in, given the card's own font."""
    font = QFont(base)
    font.setPointSizeF(max(7.0, base.pointSizeF() - 1.0))
    font.setWeight(QFont.Weight.DemiBold)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
    return font


def pill_size(base, text):
    """The QSize a pill carrying `text` wants, at the card's own font."""
    metrics = QFontMetrics(pill_font(base))
    return QSize(metrics.horizontalAdvance(text) + 2 * PILL_SIDES,
                 max(PILL_MINIMUM_HEIGHT, metrics.height() + 2 * PILL_ENDS))


def pill_rule(theme, font, height):
    """The body of a stylesheet rule that draws a pill.

    The size is named here as well as set on the font: an application-wide
    stylesheet with a font-size in it beats setFont on any widget the
    stylesheet reaches, which is every label on a card. A pill measured at one
    size and drawn at another is a pill with its word hanging out of it.
    """
    fill, ink, edge = pill_colours(theme)
    return (f"background: {fill}; color: {ink}; border: 1px solid {edge};"
            f" border-radius: {height // 2}px;"
            f" font-size: {font.pointSizeF():g}pt; font-weight: 600;")


def draw_pill(painter, rect, text, base, theme):
    """Paint a pill into `rect`. Used by the cards that paint themselves."""
    fill, ink, edge = pill_colours(theme)
    radius = rect.height() / 2.0
    painter.setPen(QColor(edge))
    painter.setBrush(QColor(fill))
    painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), radius, radius)
    painter.setPen(QColor(ink))
    painter.setFont(pill_font(base))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def wrapped_lines(metrics, text, width, limit):
    """`text` broken to fit `width` in at most `limit` lines, last elided.

    Qt word wraps a drawn string on its own, but it will happily run past the
    bottom of the box doing it, and a title clipped through the middle of its
    letters reads as a rendering fault rather than as a long title. The lines
    are decided here so the caller knows how many there are before it draws
    any of them, and so a test can ask whether a title fits without looking at
    pixels.
    """
    words = (text or "").split()
    if not words:
        return []
    lines = [words[0]]
    for word in words[1:]:
        candidate = lines[-1] + " " + word
        if metrics.horizontalAdvance(candidate) <= width:
            lines[-1] = candidate
        elif len(lines) < limit:
            lines.append(word)
        else:
            # Out of lines: the rest goes on the end of the last one and is
            # cut off there, which at least ends in an ellipsis rather than
            # in the middle of a letter.
            lines[-1] = candidate
    # Only what overflows is cut. Qt's own elidedText will shorten a line
    # whose advance is exactly the width it is given, and a title that fits to
    # the pixel losing its last word to that is a title cut short for nothing.
    return [line if metrics.horizontalAdvance(line) <= width
            else metrics.elidedText(line, Qt.TextElideMode.ElideRight, width)
            for line in lines]


def all_of_it(text, lines):
    """True when `lines` is the whole of `text` with nothing cut off."""
    return " ".join(lines) == " ".join((text or "").split())


class PillBadge(QLabel):
    """A pill, as a widget, for a card that is built out of labels.

    The same colours and the same word as the painted cards use, so the two
    kinds of card cannot drift apart.
    """

    def __init__(self, text, theme, parent=None):
        super().__init__(text, parent)
        self._theme = theme
        # Sized off the card's font rather than the label's own, so that a
        # pill on a card and a pill painted by a card come out the same.
        base = parent.font() if parent is not None else self.font()
        # Named, because a card that styles its own labels reaches this one
        # too and a pill with the card's "no background, no border" applied to
        # it is a pill that has stopped being a pill.
        self.setObjectName("pillBadge")
        self.setFont(pill_font(base))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(pill_size(base, text))
        self._paint()
        theme.changed.connect(self._paint)

    def _paint(self):
        self.setStyleSheet("QLabel { " + pill_rule(self._theme, self.font(),
                                                   self.height()) + " }")


class FlowLayout(QLayout):
    """Left to right, wrapping at the edge.

    The card grid has to reflow as the window resizes; a QGridLayout would need
    the column count recomputed by hand on every resize event.
    """

    def __init__(self, parent=None, margin=0, spacing=14):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _layout(self, rect, apply):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > area.right() and line_height > 0:
                x = area.x()
                y = y + line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class FlowHost(QWidget):
    """A widget whose height follows its flow layout at the width it is given.

    A flow layout's minimum size is one card per row, and a scroll area takes
    that as the floor; without this the home page shows a scrollbar at every
    window size, however few cards there are.
    """

    def __init__(self, spacing=16, parent=None):
        super().__init__(parent)
        self.flow = FlowLayout(self, 0, spacing)

    def sync_height(self):
        height = self.flow.heightForWidth(max(self.width(), 1))
        if height != self.minimumHeight() or height != self.maximumHeight():
            self.setMinimumHeight(height)
            self.setMaximumHeight(height)
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync_height()


class StatusDot(QWidget):
    """A filled circle with a soft halo. Never the only signal: every use of it
    sits beside a word, because a colour alone is no use to the third of the
    audience who cannot tell these two greens from these two reds."""

    def __init__(self, colour="#888888", diameter=10, parent=None):
        super().__init__(parent)
        self._colour = colour
        self._diameter = diameter
        self.setFixedSize(diameter + 8, diameter + 8)

    def set_colour(self, colour):
        if colour != self._colour:
            self._colour = colour
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        centre = self.rect().center()
        halo = QColor(self._colour)
        halo.setAlpha(60)
        painter.setBrush(halo)
        radius = (self._diameter + 6) / 2
        painter.drawEllipse(centre, radius, radius)
        painter.setBrush(QColor(self._colour))
        radius = self._diameter / 2
        painter.drawEllipse(centre, radius, radius)
        painter.end()


class ToolCard(QAbstractButton):
    """One registered screen on the home page.

    Knows the four class attributes off a Screen and nothing else, so a new
    tool is a new module plus a register() call and this needs no edit.
    """

    CARD_WIDTH = 344
    CARD_HEIGHT = 176
    PADDING = 20
    BADGE = 48
    ACTION = "Open"

    #: Points over the card's own font for the title. One size for every
    #: card. Drawing the game fixes larger than the tools around them was
    #: tried and looked wrong: a grid of cards the same shape with one set of
    #: titles swollen reads as a mistake rather than as emphasis.
    TITLE_POINTS = 2.0

    #: The space inside the caveat's own rounded box, and the radius of it.
    #: Bare coloured text on a card reads as something having gone wrong with
    #: the drawing; the same words in a box of their own read as a label.
    NOTE_SIDES = 9
    NOTE_ENDS = 5
    NOTE_RADIUS = 8

    #: The most lines a caveat may take, and the gap above the action row. A
    #: caveat is given the room it actually needs at the width it will be
    #: drawn at rather than this many lines always: the wording that falls in
    #: two lines on a wide window falls in three on a narrow one, and picking
    #: one of those and living with it means either a cut sentence or a band
    #: of empty card. The grid gives every card in it the room the largest of
    #: them needs, so a row stays a row and a card with nothing to warn about
    #: spends the room on a roomier blurb.
    NOTE_LINES = 3
    NOTE_GAP = 8

    #: A title wraps rather than being cut short. Some of these are the names
    #: of games, and a game recognised by the first half of its name is one
    #: the reader still has to stop and work out. Two lines is what the band
    #: beside the icon holds.
    TITLE_LINES = 2

    activated = Signal(str)

    def __init__(self, key, title, blurb, tile, theme, parent=None,
                 icon_name=None, badge="", note=""):
        super().__init__(parent)
        self.key = key
        self._blurb = blurb
        self._tile = tile
        self.badge = badge or ""
        self.note = note or ""
        # The two letter tile is the fallback the frozen Screen interface
        # guarantees; a drawn icon is preferred where the set has one.
        self.icon_name = (icons.for_key(key, title) if icon_name is None
                          else icon_name)
        self._theme = theme
        self._lift = 0.0
        self.setText(title)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(title)
        # The caveat goes in both, because it is the half of what the card
        # says that a reader most needs before pressing it, and on the card
        # itself it is drawn short.
        self.setAccessibleDescription(
            f"{blurb} {self.note}".strip() if self.note else blurb)
        self.setToolTip(f"{blurb}\n\n{self.note}" if self.note else blurb)
        # Hover is animated rather than switched so that sweeping the pointer
        # across the grid does not strobe.
        self._animation = QPropertyAnimation(self, b"lift", self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.clicked.connect(lambda: self.activated.emit(self.key))
        theme.changed.connect(self.update)

    def sizeHint(self):
        return QSize(self.CARD_WIDTH, self.CARD_HEIGHT)

    def get_lift(self):
        return self._lift

    def set_lift(self, value):
        self._lift = value
        self.update()

    lift = Property(float, get_lift, set_lift)

    def enterEvent(self, event):
        self._animate_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_to(0.0)
        super().leaveEvent(event)

    def _animate_to(self, value):
        self._animation.stop()
        self._animation.setStartValue(self._lift)
        self._animation.setEndValue(value)
        self._animation.start()

    # -- the title, worked out rather than measured off the screen
    #
    # The paint below asks these three for what it draws, so a test can ask
    # the same questions and get the same answers without reading pixels back
    # out of a rendered card.
    def body_rect(self, width=None, height=None):
        """The rounded rectangle the card is drawn inside."""
        rect = QRect(0, 0,
                     self.width() if width is None else width,
                     self.height() if height is None else height)
        return rect.adjusted(1, 2, -1, -3)

    def title_font(self):
        font = QFont(self.font())
        font.setPointSizeF(self.font().pointSizeF() + self.TITLE_POINTS)
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def title_box(self, width=None, body=None):
        """Where the title goes: the whole band beside the icon."""
        body = self.body_rect(width) if body is None else body
        left = body.left() + self.PADDING + self.BADGE + 14
        right = body.right() - self.PADDING
        return QRect(left, body.top() + self.PADDING,
                     max(1, right - left), self.BADGE)

    def title_layout(self, width=None, body=None):
        """(font, lines) for the title."""
        font = self.title_font()
        lines = wrapped_lines(QFontMetrics(font), self.text(),
                              self.title_box(width, body).width(),
                              self.TITLE_LINES)
        return font, lines

    def title_lines(self, width=None):
        """The title as it is drawn, line by line, elided where it must be."""
        return self.title_layout(width)[1]

    def note_font(self):
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.0))
        return font

    def note_width(self, width=None, body=None):
        """The room the words have, which is inside the box they sit in."""
        body = self.body_rect(width) if body is None else body
        return max(1, body.width() - 2 * self.PADDING - 2 * self.NOTE_SIDES)

    def note_lines(self, width=None, body=None):
        """The caveat as it is drawn, line by line."""
        if not self.note:
            return []
        return wrapped_lines(QFontMetrics(self.note_font()), self.note,
                             self.note_width(width, body), self.NOTE_LINES)

    def note_height(self, width=None, body=None):
        """The room a caveat needs at that width, or 0 where there is none.

        Asked by the grid before the card is that size, so the width is an
        argument rather than something read off the widget.
        """
        lines = self.note_lines(width, body)
        if not lines:
            return 0
        return (QFontMetrics(self.note_font()).lineSpacing() * len(lines)
                + 2 * self.NOTE_ENDS + self.NOTE_GAP)

    def note_box(self, body=None, width=None):
        """The box the caveat is drawn in, just above the action row.

        As wide as the words in it and no wider. A box run out to the full
        card with one short line in it reads as an empty panel somebody forgot
        to fill; one that stops where the words do reads as a label.
        """
        body = self.body_rect(width) if body is None else body
        action_height = max(QFontMetrics(self.font()).height(), 18)
        bottom = body.bottom() - self.PADDING - action_height - self.NOTE_GAP
        height = max(0, self.note_height(body=body) - self.NOTE_GAP)
        room = body.width() - 2 * self.PADDING
        metrics = QFontMetrics(self.note_font())
        wanted = max([metrics.horizontalAdvance(line)
                      for line in self.note_lines(body=body)] or [0])
        return QRect(body.left() + self.PADDING, bottom - height,
                     min(room, wanted + 2 * self.NOTE_SIDES), height)

    def paintEvent(self, event):
        colour = self._theme.colour
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pressed = self.isDown()
        raised = self._lift if not pressed else 0.0
        body = QRect(self.rect())
        body.adjust(1, 2, -1, -3)
        if pressed:
            body.adjust(0, 1, 0, 1)

        # A soft stack of translucent rounded rectangles reads as a shadow and
        # costs less than a graphics effect on every card in the grid.
        if raised > 0.01:
            shadow = QColor(0, 0, 0)
            for step in range(1, 5):
                shadow.setAlpha(int(16 * raised / step))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(shadow)
                painter.drawRoundedRect(
                    body.adjusted(-step, -step + 2, step, step + 2), 14, 14)

        surface = colour("surface")
        if pressed:
            fill = mix(surface, colour("accent"), 0.14)
        else:
            fill = mix(surface, colour("accent"), 0.06 * raised)
        towards_accent = min(1.0, raised + (0.6 if pressed else 0.0))
        border = mix(colour("border"), colour("accent"), towards_accent)

        path = QPainterPath()
        path.addRoundedRect(body, 14, 14)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(border))
        painter.drawPath(path)

        padding = self.PADDING
        badge = QRect(body.left() + padding, body.top() + padding,
                      self.BADGE, self.BADGE)
        # Two states rather than a fade between them: the mark is accent on a
        # quiet badge or accent_text on a solid one, and every colour in
        # between is too close to one of the two to read.
        lit = pressed or raised >= 0.5
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour("accent") if lit
                                else colour("surface_alt")))
        painter.drawRoundedRect(badge, 14, 14)

        mark = QColor(colour("accent_text") if lit else colour("accent"))
        if self.icon_name:
            glyph = icons.pixmap(self.icon_name, mark.name(), 24,
                                 self.devicePixelRatioF())
            painter.drawPixmap(
                QRect(badge.center().x() - 11, badge.center().y() - 11,
                      24, 24), glyph)
        else:
            tile_font = QFont(self.font())
            tile_font.setPointSizeF(self.font().pointSizeF() + 1.5)
            tile_font.setBold(True)
            tile_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
            painter.setFont(tile_font)
            painter.setPen(mark)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._tile)

        drawn_font, lines = self.title_layout(body=body)
        painter.setFont(drawn_font)
        painter.setPen(QColor(colour("text")))
        title_box = self.title_box(body=body)
        metrics = QFontMetrics(drawn_font)
        # Centred as a block on the icon beside it, so a one line title and a
        # two line one both sit against the middle of the tile rather than the
        # second line hanging below it.
        step = metrics.lineSpacing()
        top = title_box.center().y() - (len(lines) * step) // 2
        for index, line in enumerate(lines):
            painter.drawText(
                QRect(title_box.left(), top + index * step,
                      title_box.width(), step),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line)

        # The affordance sits on the bottom line of the card, so the blurb
        # stops short of it rather than running underneath.
        action_font = QFont(self.font())
        action_font.setWeight(QFont.Weight.DemiBold)
        action_metrics = QFontMetrics(action_font)
        action_height = max(action_metrics.height(), 18)
        action_top = body.bottom() - padding - action_height
        blurb_bottom = action_top - self.note_height(body=body)

        painter.setFont(self.font())
        painter.setPen(QColor(colour("text_dim")))
        blurb_box = QRect(body.left() + padding, badge.bottom() + 14,
                          body.width() - 2 * padding,
                          max(0, blurb_bottom - badge.bottom() - 14 - 8))
        painter.drawText(
            blurb_box,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap),
            self._blurb)

        # Always legible, brighter under the pointer: a call to action that
        # only appears on hover is one a touch or keyboard user never sees.
        accent = QColor(colour("accent"))
        accent.setAlphaF(0.72 + 0.28 * min(1.0, raised + (1.0 if pressed
                                                          else 0.0)))
        nudge = round(4 * (raised if not pressed else 1.0))
        painter.setFont(action_font)
        painter.setPen(accent)
        action_box = QRect(body.left() + padding, action_top,
                           body.width() - 2 * padding, action_height)
        painter.drawText(
            action_box,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.ACTION)
        arrow = icons.pixmap("forward", accent.name(), 16,
                             self.devicePixelRatioF())
        painter.setOpacity(accent.alphaF())
        painter.drawPixmap(
            QRect(action_box.left() + action_metrics.horizontalAdvance(
                      self.ACTION) + 6 + nudge,
                  action_box.center().y() - 8, 16, 16), arrow)
        painter.setOpacity(1.0)

        # The caveat sits above the action row, wrapped, in the warn colour.
        # Dim would have been quieter and this is the line that decides
        # whether somebody presses Open at all, so it is not drawn as an
        # afterthought. It is given its own room rather than taking the
        # blurb's: the blurb is the screen's own sentence and cutting it in
        # half to fit a caveat leaves two half-sentences.
        if self.note:
            note_font = self.note_font()
            note_metrics = QFontMetrics(note_font)
            box = self.note_box(body)
            fill, ink, edge = pill_colours(self._theme, "warn")
            painter.setPen(QColor(edge))
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(box.adjusted(0, 0, -1, -1),
                                    self.NOTE_RADIUS, self.NOTE_RADIUS)
            painter.setFont(note_font)
            painter.setPen(QColor(ink))
            step = note_metrics.lineSpacing()
            top = box.top() + self.NOTE_ENDS
            for index, line in enumerate(self.note_lines(body=body)):
                painter.drawText(
                    QRect(box.left() + self.NOTE_SIDES, top + index * step,
                          box.width() - 2 * self.NOTE_SIDES, step),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    line)

        # The pill goes on the action row rather than up beside the title.
        # The row is otherwise empty to the right of "Open", and a pill in the
        # top corner takes its width out of the title, which is the one thing
        # on the card that wants all the room it can have.
        if self.badge:
            size = pill_size(self.font(), self.badge)
            pill = QRect(body.right() - padding - size.width(),
                         action_box.center().y() - size.height() // 2,
                         size.width(), size.height())
            draw_pill(painter, pill, self.badge, self.font(), self._theme)

        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QColor(colour("accent")))
            painter.drawRoundedRect(body.adjusted(2, 2, -2, -2), 11, 11)
        painter.end()


class IconLabel(QWidget):
    """One icon from the set, beside a line of text, in step with the theme.

    A QLabel with a pixmap would have to be re-tinted by hand at every theme
    change and re-measured at every font change; this keeps both in one place
    so the footer rows do not each grow their own copy of that.
    """

    def __init__(self, name, token, theme, size=16, parent=None):
        super().__init__(parent)
        self._name = name
        self._token = token
        self._theme = theme
        self._size = size
        # Two pixels taller than the drawing, so that top aligning it against
        # a wrapped paragraph lands it on the first line rather than above it.
        self.setFixedSize(size, size + 2)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        theme.changed.connect(self.update)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(
            QRect(0, 2, self._size, self._size),
            icons.pixmap(self._name, self._theme.colour(self._token),
                         self._size, self.devicePixelRatioF()))
        painter.end()


class Disclosure(QWidget):
    """A one-line header with an arrow, and a body that starts closed.

    For a block of text that is worth having and is not worth the height it
    takes by default. The summary on the header says what is inside it, so
    somebody can tell whether opening it is worth doing without opening it.

    setText and text are spelled as QLabel spells them. This replaced a bare
    label in two screens and everything that already wrote to those keeps
    working.
    """

    def __init__(self, summary="Details", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._header = QToolButton()
        self._header.setCheckable(True)
        self._header.setChecked(False)
        self._header.setAutoRaise(True)
        self._header.setArrowType(Qt.RightArrow)
        self._header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._header.setText(summary)
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._header.toggled.connect(self._on_toggled)
        layout.addWidget(self._header)
        self._body = QLabel("")
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._body.hide()
        layout.addWidget(self._body)
        self._summary = summary
        self.setVisible(False)

    def _on_toggled(self, open_now):
        self._header.setArrowType(Qt.DownArrow if open_now else Qt.RightArrow)
        self._body.setVisible(bool(open_now) and bool(self._body.text()))

    def setText(self, text):
        """The body. An empty one hides the whole thing, header and all."""
        text = text or ""
        self._body.setText(text)
        self.setVisible(bool(text))
        self._body.setVisible(bool(text) and self._header.isChecked())

    def text(self):
        return self._body.text()

    def set_summary(self, summary):
        """The one line on the header. Say what is inside, in numbers."""
        self._summary = summary or "Details"
        self._header.setText(self._summary)

    def summary(self):
        return self._summary

    def is_open(self):
        return self._header.isChecked()

    def set_open(self, open_now):
        self._header.setChecked(bool(open_now))

    @property
    def header(self):
        """The button, for a test that wants to click it."""
        return self._header


class ComingSoonCard(QFrame):
    """A tool that is being worked on, sitting in the grid with the rest.

    Deliberately unlike a ToolCard. It does nothing when it is clicked, and a
    card that looks live and does nothing reads as a card that is broken. So
    it is drawn quiet and dashed, and the one thing on it that can be clicked
    is the link that says where the work is being talked about.

    Same size as the cards beside it, because a placeholder off to one side is
    a placeholder nobody connects to the grid it belongs to.
    """

    CARD_WIDTH = ToolCard.CARD_WIDTH
    CARD_HEIGHT = ToolCard.CARD_HEIGHT
    PADDING = ToolCard.PADDING

    STATUS = "Under development"

    def __init__(self, key, title, blurb, link_text, url, theme,
                 parent=None, badge=""):
        super().__init__(parent)
        self.key = key
        self.url = url
        self.badge = badge or ""
        self._theme = theme
        self.setObjectName("comingsoon")
        # Sized, not fixed. The grid gives every card in a row the same width,
        # and a card that refuses it sits in the row at the width it was drawn
        # for -- which on any window but the design width is a card visibly
        # wider than the ones beside it.
        self.resize(self.CARD_WIDTH, self.CARD_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The pill says what state the tool will be in when it lands. What it
        # is now is still said here, because a screen reader is the one place
        # a dashed border and a quiet grey say nothing at all.
        self.setAccessibleName(f"{title}, under development")
        self.setAccessibleDescription(blurb)

        box = QVBoxLayout(self)
        box.setContentsMargins(self.PADDING, self.PADDING,
                               self.PADDING, self.PADDING)
        box.setSpacing(8)

        # One state marker, not two. A card reading "Under development" over
        # a pill reading "Beta" is a card asking the reader to work out which
        # of the two it means, so the pill stands in place of the line when
        # there is one -- and it goes down on the last row, where a painted
        # card puts its own.
        self._status = None
        self._pill = None
        if not self.badge:
            self._status = QLabel(self.STATUS)
            status_font = QFont(self.font())
            status_font.setWeight(QFont.Weight.DemiBold)
            status_font.setLetterSpacing(
                QFont.SpacingType.AbsoluteSpacing, 0.8)
            status_font.setPointSizeF(max(7.0,
                                          self.font().pointSizeF() - 0.5))
            self._status.setFont(status_font)
            box.addWidget(self._status)

        self._title = QLabel(title)
        self._title.setFont(self.title_font())
        self._title.setWordWrap(True)
        box.addWidget(self._title)

        self._blurb = QLabel(blurb)
        self._blurb.setWordWrap(True)
        box.addWidget(self._blurb)
        box.addStretch(1)

        self._link = QLabel(f'<a href="{url}">{link_text}</a>')
        self._link.setOpenExternalLinks(True)
        self._link.setToolTip(f"Open {url} in your browser.")
        last = QHBoxLayout()
        last.setContentsMargins(0, 0, 0, 0)
        last.setSpacing(8)
        last.addWidget(self._link, 1, Qt.AlignmentFlag.AlignVCenter)
        if self.badge:
            self._pill = PillBadge(self.badge, theme, self)
            last.addWidget(self._pill, 0, Qt.AlignmentFlag.AlignVCenter)
        box.addLayout(last)

        self._paint()
        theme.changed.connect(self._paint)

    def sizeHint(self):
        return QSize(self.CARD_WIDTH, self.CARD_HEIGHT)

    def title_font(self):
        """The same title a painted card would draw, at the same size."""
        font = QFont(self.font())
        font.setPointSizeF(self.font().pointSizeF() + ToolCard.TITLE_POINTS)
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def text(self):
        """As QAbstractButton spells it, so the grid can read every card."""
        return self._title.text()

    def _pill_rule(self):
        """The pill, said again past the card's own rule for its labels.

        A rule naming the card wins over one the pill sets on itself however
        specific the pill tries to be, so the card has to say it.
        """
        if self._pill is None:
            return ""
        return ("QFrame#comingsoon QLabel#pillBadge { "
                + pill_rule(self._theme, self._pill.font(),
                            self._pill.height()) + " }")

    def _paint(self):
        colour = self._theme.colour
        self.setStyleSheet(
            f"QFrame#comingsoon {{ background: {colour('bg')};"
            f" border: 1px dashed {colour('border')};"
            f" border-radius: 14px; }}"
            f"QFrame#comingsoon QLabel {{ background: transparent;"
            f" border: none; }}"
            + self._pill_rule())
        if self._pill is not None:
            self._pill._paint()
        if self._status is not None:
            self._status.setStyleSheet(f"color: {colour('text_dim')};")
        # The title is the one thing on a quiet card that still has to be
        # read from the far side of the grid, so it keeps the full text
        # colour where the rest of the card is dimmed.
        title = self.title_font()
        self._title.setStyleSheet(
            f"color: {colour('text')};"
            f" font-size: {title.pointSizeF():g}pt; font-weight: 600;")
        self._blurb.setStyleSheet(f"color: {colour('text_dim')};")
        self._link.setStyleSheet(f"color: {colour('accent')};")
