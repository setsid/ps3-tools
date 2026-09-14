"""A one row strip of console figures for the top of the home screen.

Deliberately small. The diagnostics screen is where everything the console
says is laid out; this is the handful of numbers somebody glances at before
they decide whether to go and look properly, and nothing more.

Two rules shape the whole of it.

The first is that it shows nothing at all until there is something to show.
No skeleton, no "unknown", no error line: with no address typed, or a console
that did not answer, the strip is simply absent and the home screen is the
home screen. A status widget that is permanently on display saying it does not
know anything is worse than no status widget, because it turns the first thing
on the page into a complaint.

The second is that it never puts a request on the wire by itself. It is built
inert; it reads the console when the connection becomes `connected` with the
home screen in front of somebody, and when somebody asks it to, and there is
no timer anywhere in here. Polling a console on a schedule costs the person
using that console something and buys the person looking at this screen
nothing, and reading it for a page nobody has open is the same trade with the
other half missing.

Read only throughout: HttpProbe, whose allowlist is the thing that makes that
true, and the two pages the diagnostics collector already reads.
"""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QWidget)

from ps3diag.parsers import html_to_text, parse_cpursx, parse_identity
from ps3diag.transport import HttpProbe

from . import icons
from .widgets import IconLabel

#: The two pages the figures come off. Both are on the transport's read-only
#: allowlist, and both are already fetched by the diagnostics collector, so
#: nothing here asks a console for anything it is not asked for elsewhere.
PATHS = ("/", "/cpursx.ps3")

#: Shorter than the diagnostic timeouts on purpose. Nothing on this strip is
#: worth a worker sitting on a socket for twenty seconds, and a console that
#: is too busy to draw its front page promptly is one that should be left
#: alone rather than waited on.
TIMEOUT = 5.0

#: Where the temperature colours change. A PS3 idles in the fifties and works
#: in the sixties; seventy is the point at which somebody should notice, and
#: eighty is the point at which the console itself starts to mind.
WARM_C = 70.0
HOT_C = 80.0


def make_probe(host, timeout=TIMEOUT):
    """The real read-only client.

    A function rather than the class used inline so that there is one name to
    replace in a test. The test that matters is the one which replaces this
    with something that raises: a seam nobody checks is a seam that quietly
    stops being used, and there is a live console on this network.
    """
    return HttpProbe(host, timeout=timeout)


def read_console(host, probe_factory=None, cancelled=lambda: False):
    """Fetch and parse. Pure enough to call off the GUI thread, and it is.

    Returns the facts dictionary, which is empty when the console said nothing
    usable. Never raises for a network problem: HttpProbe turns those into a
    Response that is simply not ok.
    """
    probe = (probe_factory or make_probe)(host, TIMEOUT)
    parts = []
    for path in PATHS:
        if cancelled():
            return {}
        response = probe.get(path)
        if response.ok:
            parts.append(html_to_text(response.body))
    if not parts:
        return {}
    # Both pages together, because which of the two carries a given figure
    # moves between webMAN versions; the parsers take the first match.
    text = "\n".join(parts)
    facts = dict(parse_identity(text))
    facts.update(parse_cpursx(text))
    return facts


def _number(value):
    """A temperature as the console meant it: 61, not 61.0."""
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.1f}"


def temperature_token(celsius):
    """Which theme token a temperature is drawn in."""
    if celsius > HOT_C:
        return "error"
    if celsius >= WARM_C:
        return "warn"
    return "ok"


def _temperature(facts, key):
    value = facts.get(key)
    if not isinstance(value, (int, float)):
        return None
    return f"{_number(value)} °C", temperature_token(float(value))


def _cpu(facts):
    return _temperature(facts, "cpu_temp_c")


def _rsx(facts):
    return _temperature(facts, "rsx_temp_c")


def _fan(facts):
    speed = facts.get("fan_speed_percent")
    if not isinstance(speed, int):
        return None
    mode = facts.get("fan_mode")
    # The mode is only worth the width when it is there; on 1.47.48q it often
    # is not, and "41%  " with a hole after it reads as a missing value.
    return (f"{speed}% {mode}" if mode else f"{speed}%"), "text"


def _firmware(facts):
    firmware = facts.get("firmware")
    if not firmware:
        return None
    kind = facts.get("firmware_type")
    return (f"{firmware} {kind}" if kind else str(firmware)), "text"


def _uptime(facts):
    uptime = facts.get("uptime")
    if not uptime:
        return None
    return str(uptime), "text"


#: The strip, left to right. Temperatures first: they are what was asked for,
#: they are the only figures here that change minute to minute, and they are
#: the only ones that are ever a reason to stop what you are doing.
#:
#: Each entry reads the facts and returns (value, token) or None, and a None
#: is left out of the row entirely rather than drawn empty. webMAN 1.47.48q
#: does not report all of this, and a console that only knows its own
#: temperature should show a strip with one figure on it, not five slots with
#: four dashes in them.
FIELDS = (
    ("cpu", "CPU", _cpu),
    ("rsx", "RSX", _rsx),
    ("fan", "Fan", _fan),
    ("firmware", "Firmware", _firmware),
    ("uptime", "Uptime", _uptime),
)


def read_fields(facts):
    """(key, label, value, token) for every field this console reported."""
    out = []
    for key, label, reader in FIELDS:
        found = reader(facts or {})
        if found is not None:
            out.append((key, label, found[0], found[1]))
    return tuple(out)


class _Rule(QWidget):
    """A hairline between two figures, so the row reads as separate values."""

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setFixedSize(1, 16)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        theme.changed.connect(self.update)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self._theme.colour("border")))
        painter.end()


class _Stat(QWidget):
    """One figure: a dim caption and the value, in the token it belongs to."""

    def __init__(self, key, caption, value, token, theme, parent=None):
        super().__init__(parent)
        self.key = key
        self.token = token
        self._theme = theme
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        self.caption = QLabel(caption, self)
        self.value = QLabel(value, self)
        # Selectable, because the commonest thing done with a number on this
        # strip is to repeat it to whoever is helping.
        self.value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.caption)
        row.addWidget(self.value)
        self.setAccessibleName(f"{caption} {value}")
        theme.changed.connect(self.apply_theme)
        self.apply_theme()

    def apply_theme(self):
        """Colour is never named here, only asked for.

        Set on the widgets rather than through the application stylesheet
        because the token a value is drawn in depends on the value: a
        temperature moves between three of them.
        """
        self.caption.setStyleSheet(
            f"color: {self._theme.colour('text_dim')};")
        self.value.setStyleSheet(
            f"color: {self._theme.colour(self.token)}; font-weight: 600;")

    def value_colour(self):
        """What the value is actually drawn in. For tests, and for review."""
        return self._theme.colour(self.token)


class ConsoleStats(QWidget):
    """The strip. Hidden until a console has told it something.

    Built with services rather than reaching for anything global: the
    connection it watches and the worker pool it fetches on both come from
    there, and `probe_factory` is the one seam a test replaces.
    """

    #: True when the strip has something on it. The home screen uses it to
    #: take the gap under the strip away as well, so that an absent strip
    #: leaves no trace of itself at the top of the page.
    visibility_changed = Signal(bool)

    def __init__(self, services, parent=None, probe_factory=None):
        super().__init__(parent)
        self.setObjectName("consoleStats")
        self._services = services
        self._theme = services.theme
        self._connection = services.connection
        self._probe_factory = probe_factory
        self._facts = {}
        self._stats = []
        self._task = None
        self._generation = 0
        # A reading is owed but has not been taken, because nobody is looking
        # at this page yet. See _on_screen.
        self._due = False
        # What the connection was last time it spoke. The connection state
        # emits changed() for scan progress as well, and a strip that refetched
        # on every one of those would be the timer this deliberately has not
        # got.
        self._seen = (self._connection.host, self._connection.connection)

        # Only as wide as the figures on it. A bar run out to the full width
        # of the page would be a dashboard with most of a dashboard missing;
        # a strip that ends where its last value ends stays the small thing it
        # was asked to be, and shrinks by itself when a console reports less.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(16, 9, 10, 9)
        self._row.setSpacing(18)

        self._mark = IconLabel("console", "text_dim", self._theme, 16, self)
        self._row.addWidget(self._mark, 0, Qt.AlignmentFlag.AlignVCenter)

        self._refresh = QPushButton("", self)
        self._refresh.setProperty("flat", True)
        self._refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh.setToolTip("Read these figures from the console again.")
        self._refresh.setAccessibleName("Refresh the console figures")
        self._refresh.clicked.connect(self.refresh)
        self._row.addWidget(self._refresh, 0, Qt.AlignmentFlag.AlignVCenter)

        self._theme.changed.connect(self._apply_theme)
        self._connection.changed.connect(self._connection_changed)
        self._apply_theme()
        # Absent, not empty, and absent is also how it starts: constructing the
        # home screen must not put a request on the wire.
        self.setVisible(False)

    # -- what the launcher and the tests ask it

    def fields(self):
        """(key, label, value, token) for what is on screen right now."""
        return tuple((stat.key, stat.caption.text(), stat.value.text(),
                      stat.token) for stat in self._stats)

    def stat(self, key):
        for stat in self._stats:
            if stat.key == key:
                return stat
        return None

    @property
    def busy(self):
        return self._task is not None

    def clear(self):
        """Forget everything and go away. No message is left behind."""
        self._facts = {}
        self._draw()

    def refresh(self):
        """Read the console, off the GUI thread. A no-op without an address.

        Nothing is cleared first. A refresh that failed should leave the last
        figures up rather than blanking the strip, because the numbers going
        away is a louder event than them being a minute old.
        """
        host = (self._connection.host or "").strip()
        if not host or self._task is not None:
            return None
        self._generation += 1
        generation = self._generation
        factory = self._probe_factory

        def work(control):
            facts = read_console(host, factory, lambda: control.cancelled)
            return {"host": host, "facts": facts}

        task = self._services.submit(work)
        self._task = task
        self._refresh.setEnabled(False)
        task.finished.connect(
            lambda result: self._arrived(generation, result))
        # A failure here is a fault in this program, not in anybody's network,
        # and the home screen is not where it gets reported: the strip keeps
        # whatever it had and the crash log has the rest.
        task.failed.connect(lambda _message: self._finished(generation))
        task.done.connect(lambda: self._finished(generation))
        return task

    # -- the connection

    def _connection_changed(self):
        now = (self._connection.host, self._connection.connection)
        if now == self._seen:
            return
        self._seen = now
        if self._connection.connected:
            self._due = True
            self.wake()
            return
        # A different address, or one that stopped answering. The figures on
        # screen describe a console nobody is asking about any more.
        self._due = False
        self.clear()

    def wake(self):
        """Take a reading that is owed, now that there is somebody to see it.

        Called by the home screen when it comes to the front. A connection
        made while a tool was open leaves the reading owed until then.
        """
        if self._due and self._on_screen():
            self._due = False
            self.refresh()

    def _on_screen(self):
        """Whether the page this strip sits on is in front of anybody.

        The console on the other end of this is somebody's games machine, and
        reading it for a widget nobody can see is a cost to them and no use to
        anyone. It is asked of the parent rather than of the strip, because
        the strip is itself hidden until it has something to show.

        It is also what keeps this out of the way in the test suite. Several
        tests drive a window that was never shown as far as "connected" using
        invented addresses, and a strip that read the console on that signal
        alone would have put requests on this LAN from a unit test. That is
        the regression this project has had once already.
        """
        parent = self.parentWidget()
        return parent is None or parent.isVisible()

    def _arrived(self, generation, result):
        if generation != self._generation or not result:
            return
        if result.get("host") != self._connection.host:
            # The address moved on while the worker was out.
            return
        self._facts = result.get("facts") or {}
        self._draw()

    def _finished(self, generation):
        if generation != self._generation:
            return
        self._task = None
        self._refresh.setEnabled(True)

    # -- drawing

    def _draw(self):
        for stat in self._stats:
            self._row.removeWidget(stat)
            stat.hide()
            stat.setParent(None)
            stat.deleteLater()
        for rule in self.findChildren(_Rule):
            self._row.removeWidget(rule)
            rule.hide()
            rule.setParent(None)
            rule.deleteLater()
        self._stats = []

        found = read_fields(self._facts)
        for index, (key, label, value, token) in enumerate(found):
            if index:
                rule = _Rule(self._theme, self)
                self._row.insertWidget(self._row.count() - 1, rule,
                                       0, Qt.AlignmentFlag.AlignVCenter)
                rule.show()
            stat = _Stat(key, label, value, token, self._theme, self)
            self._row.insertWidget(self._row.count() - 1, stat,
                                   0, Qt.AlignmentFlag.AlignVCenter)
            stat.show()
            self._stats.append(stat)

        self.setToolTip(self._summary())
        showing = bool(found)
        self.setVisible(showing)
        self.updateGeometry()
        self.visibility_changed.emit(showing)

    def _summary(self):
        """The couple of things worth knowing that are not worth a column.

        webMAN's own version among them: it explains why a field is missing,
        which is a question somebody asks once and then never again, so it
        belongs on the tooltip rather than in the row.
        """
        bits = []
        version = self._facts.get("webman_version")
        if version:
            bits.append(f"webMAN {version}")
        host = (self._connection.host or "").strip()
        if host:
            bits.append(f"read from the console at {host}")
        return ". ".join(bits) + "." if bits else ""

    def _apply_theme(self):
        self._refresh.setIcon(icons.icon(
            "refresh", self._theme.colour("text_dim"), 15,
            self.devicePixelRatioF()))
        self._refresh.setIconSize(QSize(15, 15))
        self.update()

    def paintEvent(self, event):
        """A quiet card behind the row.

        Painted rather than styled: the strip is not in the application
        stylesheet, and a widget that sets its own would need re-writing on
        every theme change anyway.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(body, 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._theme.colour("surface")))
        painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(self._theme.colour("border")))
        painter.drawPath(path)
        painter.end()
