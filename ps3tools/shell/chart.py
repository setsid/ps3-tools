"""A line graph of readings over time, painted by hand.

Hand painted because the alternative is a charting library, and this program
ships as one file built from the standard library and PySide6. A graph of four
figures against time is a few hundred lines of QPainter, and it themes itself
properly, which a bundled widget set would need talking round.

Where the lines go, what the axis says and where the breaks are all come from
ps3tools/telemetry.py. This file decides pixels and nothing else, so a bug in
the arithmetic is caught by a test that never opens a window, and a bug in
here shows up as something looking wrong rather than as a wrong number.

Two things it refuses to do, both for the same reason: a graph is read as a
statement about a console.

It draws nothing across a hole in the readings. A break in the line is what a
console that stopped answering looks like.

It never labels an axis it has no data for. An empty graph says so in words
and draws no frame, because an empty pair of axes with a grid on it reads as a
console that reported a flat zero.
"""

import time

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..telemetry import (GAP_FACTOR, bounds, format_value, runs, series_for,
                         ticks, time_labels)
from .widgets import mix

#: Room for the axis labels. Fixed pixels: the numbers are drawn in a small
#: font that does not follow the desktop's text size, because an axis label
#: that grows pushes the graph itself down to nothing on a large-text desktop.
LEFT_GUTTER = 44
BOTTOM_GUTTER = 18
TOP_GUTTER = 6
RIGHT_GUTTER = 8

#: The shortest graph worth drawing. Below this the frame, the grid and the
#: labels take the whole height and the line has nowhere to go.
LEAST_HEIGHT = 96

#: How thick a line is, and how much of the theme's surface the fill under it
#: keeps. The fill is faint on purpose: two temperatures on one graph with a
#: solid fill under each hides whichever is lower.
LINE_WIDTH = 1.8
FILL_ALPHA = 0.14
GRID_ALPHA = 0.55

#: The dot on the newest reading, and the halo behind it. The newest point is
#: the one somebody is reading, and on a graph that updates every few seconds
#: it is worth being able to find without counting along the line.
HEAD_RADIUS = 2.6
HEAD_HALO = 5.5

#: The axis font, in points off the widget's own font. Smaller than the body
#: text because these are reference marks rather than something to read.
AXIS_POINT_DROP = 2


def plot(points, rect, start, end, low, high):
    """The points as pixels inside rect.

    start and end are the ends of the time axis and low and high the ends of
    the value axis, so the same series can be drawn against a shared axis by
    handing every call the same four numbers. A span of zero puts the reading
    at the right-hand edge, which is where the newest one belongs.
    """
    out = []
    span = end - start
    height = high - low
    for at, value in points:
        across = 1.0 if span <= 0 else (at - start) / span
        up = 0.5 if height <= 0 else (value - low) / height
        out.append(QPointF(rect.left() + across * rect.width(),
                           rect.bottom() - up * rect.height()))
    return out


class Chart(QWidget):
    """One graph. Any number of series from the same history on it.

    The widget holds no readings of its own. It is handed the history and
    reads it when it paints, so there is one copy of the data and no way for
    the graph to fall behind what the screen says beside it.
    """

    #: Emitted with the reading under the pointer, or None when it leaves.
    hovered = Signal(object)

    def __init__(self, keys, heading="", parent=None):
        super().__init__(parent)
        self._keys = tuple(keys)
        self.heading = heading
        self._history = None
        self._span = None
        self._interval = 5.0
        self._theme = None
        self._hover_at = None
        self.setMinimumHeight(LEAST_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    # -- what it is drawing
    @property
    def keys(self):
        return self._keys

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def set_history(self, history):
        self._history = history
        self.update()

    def set_span(self, span):
        """Seconds of history to draw, or None for everything there is."""
        self._span = span
        self.update()

    def set_interval(self, seconds):
        """The sampling interval, which is what decides what counts as a gap."""
        self._interval = max(0.5, float(seconds))
        self.update()

    def refresh(self):
        """Called when a reading arrives. Named so the caller reads plainly."""
        self.update()

    # -- the numbers, so a test can ask rather than read pixels
    def points_for(self, key):
        if self._history is None:
            return ()
        return self._history.points(key, self._span)

    def has_anything(self):
        return any(self.points_for(key) for key in self._keys)

    def axis(self):
        """(low, high) of the vertical axis, shared by every line on here."""
        values = []
        floor = ceiling = None
        least = 1.0
        for key in self._keys:
            series = series_for(key)
            values.extend(value for _at, value in self.points_for(key))
            floor = series.floor if floor is None else (
                floor if series.floor is None else min(floor, series.floor))
            ceiling = series.ceiling if ceiling is None else (
                ceiling if series.ceiling is None
                else max(ceiling, series.ceiling))
            least = max(least, series.least_span)
        return bounds(values, floor=floor, ceiling=ceiling, least_span=least)

    def window(self):
        """(start, end) of the time axis in epoch seconds.

        The right-hand edge is the newest reading rather than the clock. A
        console that stopped answering two minutes ago should leave its last
        reading at the edge where it can be read, and a graph that scrolled
        away from the data would say nothing at all about what happened.
        """
        at = []
        for key in self._keys:
            at.extend(point[0] for point in self.points_for(key))
        if not at:
            now = time.time()
            return now - (self._span or 60.0), now
        end = max(at)
        if self._span:
            return end - self._span, end
        start = min(at)
        # A single reading has no span of its own, so it gets the interval
        # either side of it and sits in the middle rather than in the corner.
        return (start - self._interval, end) if end > start else (
            start - self._interval, end + self._interval)

    # -- colours
    def _colour(self, token, fallback="#888888"):
        if self._theme is None:
            return QColor(fallback)
        return QColor(self._theme.colour(token))

    def _ink(self):
        return self._colour("text", "#222222")

    def _dim(self):
        return self._colour("text_dim", "#777777")

    # -- pointer
    def mouseMoveEvent(self, event):
        if not self.has_anything():
            return
        rect = self._plot_rect()
        if not rect.contains(event.position()):
            self._clear_hover()
            return
        start, end = self.window()
        span = end - start
        across = (event.position().x() - rect.left()) / max(1.0, rect.width())
        self._hover_at = start + across * span
        self.hovered.emit(self.reading_near(self._hover_at))
        self.update()

    def leaveEvent(self, event):
        self._clear_hover()
        super().leaveEvent(event)

    def _clear_hover(self):
        if self._hover_at is None:
            return
        self._hover_at = None
        self.hovered.emit(None)
        self.update()

    def reading_near(self, at):
        """The reading closest to a moment, for the readout under the pointer.

        Closest rather than the one before it: the pointer is a rough
        instrument and the nearest point is the one somebody thinks they are
        pointing at.
        """
        if self._history is None:
            return None
        best = None
        gap = None
        for reading in self._history.readings:
            if not any(key in reading.values for key in self._keys):
                continue
            distance = abs(reading.at - at)
            if gap is None or distance < gap:
                best, gap = reading, distance
        return best

    # -- painting
    def _plot_rect(self):
        return QRectF(LEFT_GUTTER, TOP_GUTTER,
                      max(1, self.width() - LEFT_GUTTER - RIGHT_GUTTER),
                      max(1, self.height() - TOP_GUTTER - BOTTOM_GUTTER))

    def _axis_font(self):
        font = QFont(self.font())
        font.setPointSize(max(6, font.pointSize() - AXIS_POINT_DROP))
        return font

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            if not self.has_anything():
                self._paint_empty(painter)
                return
            rect = self._plot_rect()
            low, high = self.axis()
            start, end = self.window()
            self._paint_grid(painter, rect, low, high, start, end)
            # Every fill, then every line, then every head. One series at a
            # time put the second fill over the first line and left the lower
            # of two temperatures drawn in a colour that was neither of them.
            for pass_name in ("fill", "line", "head"):
                for key in self._keys:
                    self._paint_series(painter, rect, key, low, high, start,
                                       end, pass_name)
            self._paint_hover(painter, rect, start, end)
        finally:
            painter.end()

    def _paint_empty(self, painter):
        painter.setPen(QPen(self._dim()))
        painter.setFont(self._axis_font())
        painter.drawText(self.rect(), Qt.AlignCenter,
                         "Nothing recorded yet")

    def _paint_grid(self, painter, rect, low, high, start, end):
        surface = self._colour("border", "#cccccc")
        grid = QColor(surface)
        grid.setAlphaF(GRID_ALPHA)
        painter.setFont(self._axis_font())
        metrics = painter.fontMetrics()
        series = series_for(self._keys[0])

        painter.setPen(QPen(grid, 1.0))
        for value in ticks(low, high):
            y = rect.bottom() - (value - low) / max(1e-9, high - low) \
                * rect.height()
            if y < rect.top() - 1 or y > rect.bottom() + 1:
                continue
            painter.setPen(QPen(grid, 1.0))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            text = (f"{value:.{series.decimals}f}" if series.decimals
                    else f"{value:g}")
            painter.setPen(QPen(self._dim()))
            painter.drawText(
                QRectF(0, y - metrics.height() / 2, LEFT_GUTTER - 6,
                       metrics.height()),
                Qt.AlignRight | Qt.AlignVCenter, text)

        span = end - start
        for back, label in time_labels(span):
            x = rect.right() - (back / max(1e-9, span)) * rect.width()
            if x < rect.left() - 1:
                continue
            painter.setPen(QPen(grid, 1.0))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            width = metrics.horizontalAdvance(label) + 8
            # The label is centred on its gridline, so the leftmost one hangs
            # over into the gutter and lands on top of the bottom value label.
            # The gridline stays and the word is dropped: it is the one label
            # whose value can be read off the graph's own left edge.
            if x - width / 2 < rect.left():
                continue
            painter.setPen(QPen(self._dim()))
            painter.drawText(
                QRectF(x - width / 2, rect.bottom() + 2, width,
                       BOTTOM_GUTTER - 2),
                Qt.AlignHCenter | Qt.AlignTop, label)

    def _paint_series(self, painter, rect, key, low, high, start, end,
                      pass_name):
        """One series, in the pass named. See paintEvent for why in passes."""
        points = self.points_for(key)
        if not points:
            return
        series = series_for(key)
        colour = self._colour(series.token)
        if pass_name == "head":
            self._paint_head(painter, rect, points, colour, low, high, start,
                             end)
            return
        for run in runs(points, self._interval * GAP_FACTOR):
            pixels = plot(run, rect, start, end, low, high)
            if len(pixels) == 1:
                # A reading on its own between two silences. Drawn as a dot in
                # the line pass, because a dot is the whole of what is known.
                if pass_name == "line":
                    painter.setBrush(colour)
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(pixels[0], HEAD_RADIUS, HEAD_RADIUS)
                continue
            if pass_name == "fill":
                self._paint_fill(painter, rect, pixels, colour)
            else:
                self._paint_line(painter, pixels, colour)

    def _paint_fill(self, painter, rect, pixels, colour):
        path = QPainterPath(QPointF(pixels[0].x(), rect.bottom()))
        for point in pixels:
            path.lineTo(point)
        path.lineTo(QPointF(pixels[-1].x(), rect.bottom()))
        path.closeSubpath()
        faint = QColor(colour)
        faint.setAlphaF(FILL_ALPHA)
        painter.setPen(Qt.NoPen)
        painter.setBrush(faint)
        painter.drawPath(path)

    def _paint_line(self, painter, pixels, colour):
        path = QPainterPath(pixels[0])
        for point in pixels[1:]:
            path.lineTo(point)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(colour, LINE_WIDTH, Qt.SolidLine, Qt.RoundCap,
                            Qt.RoundJoin))
        painter.drawPath(path)

    def _paint_head(self, painter, rect, points, colour, low, high, start,
                    end):
        """The newest reading, marked, because that is the one being read."""
        head = plot(points[-1:], rect, start, end, low, high)[0]
        halo = QColor(colour)
        halo.setAlphaF(0.25)
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(head, HEAD_HALO, HEAD_HALO)
        painter.setBrush(colour)
        painter.drawEllipse(head, HEAD_RADIUS, HEAD_RADIUS)

    def _paint_hover(self, painter, rect, start, end):
        if self._hover_at is None:
            return
        reading = self.reading_near(self._hover_at)
        if reading is None:
            return
        span = max(1e-9, end - start)
        x = rect.left() + (reading.at - start) / span * rect.width()
        if x < rect.left() or x > rect.right():
            return
        pen = QPen(QColor(mix(self._dim().name(), self._ink().name(), 0.3)))
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

    # -- words for the readout beside the graph
    def summary(self, key):
        """"61 °C   low 54   high 66", or "" when there is nothing.

        Built here rather than in the screen so that the figures quoted are
        the figures on the graph, taken from the same window.
        """
        series = series_for(key)
        points = self.points_for(key)
        if not points:
            return ""
        values = [value for _at, value in points]
        return (f"{format_value(values[-1], series)}   "
                f"low {format_value(min(values), series)}   "
                f"high {format_value(max(values), series)}")
