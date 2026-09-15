"""Small shared widgets. No domain knowledge, no console, no decisions.

The cards and the status dot are painted rather than assembled out of stock
widgets: hover, press and focus want to move together, and a QFrame holding
three QLabels cannot do that without a stylesheet per state.
"""

from PySide6.QtCore import (QEasingCurve, QPoint, QPropertyAnimation, QRect,
                            QSize, Property, Qt, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QAbstractButton, QLayout, QSizePolicy, QWidget

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


def mix(first, second, amount):
    """`amount` of second blended into first. All three are "#rrggbb"."""
    one, two = QColor(first), QColor(second)
    return QColor(
        round(one.red() + (two.red() - one.red()) * amount),
        round(one.green() + (two.green() - one.green()) * amount),
        round(one.blue() + (two.blue() - one.blue()) * amount),
    ).name()


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

    activated = Signal(str)

    def __init__(self, key, title, blurb, tile, theme, parent=None,
                 icon_name=None):
        super().__init__(parent)
        self.key = key
        self._blurb = blurb
        self._tile = tile
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
        self.setAccessibleDescription(blurb)
        self.setToolTip(blurb)
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

        title_font = QFont(self.font())
        title_font.setPointSizeF(self.font().pointSizeF() + 2.0)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(colour("text")))
        title_box = QRect(badge.right() + 14, badge.top(),
                          body.right() - badge.right() - 14 - padding,
                          badge.height())
        metrics = QFontMetrics(title_font)
        painter.drawText(
            title_box,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(self.text(), Qt.TextElideMode.ElideRight,
                               title_box.width()))

        # The affordance sits on the bottom line of the card, so the blurb
        # stops short of it rather than running underneath.
        action_font = QFont(self.font())
        action_font.setWeight(QFont.Weight.DemiBold)
        action_metrics = QFontMetrics(action_font)
        action_height = max(action_metrics.height(), 18)
        action_top = body.bottom() - padding - action_height

        painter.setFont(self.font())
        painter.setPen(QColor(colour("text_dim")))
        blurb_box = QRect(body.left() + padding, badge.bottom() + 14,
                          body.width() - 2 * padding,
                          action_top - badge.bottom() - 14 - 8)
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
