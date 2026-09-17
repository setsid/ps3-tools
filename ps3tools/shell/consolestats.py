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

import time

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve,
                            QPropertyAnimation, QSize, Property, Qt,
                            Signal)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QWidget)

from ps3diag.parsers import (html_to_text, human_size, parse_cpursx,
                            parse_identity, parse_storage)
from ps3diag.transport import HttpProbe

from ..updates import free_bytes_for
from . import icons
from .widgets import IconLabel, mix

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

#: How long a figure takes to change: out of the old number and into the new
#: one, the whole of it. Short on purpose. This is a value being replaced in
#: front of somebody who is reading it, and a slow fade reads as the strip
#: having gone wrong rather than as a fresh reading arriving.
VALUE_FADE_MS = 100

#: How faint the value gets at the bottom of that fade. Not all the way to
#: nothing: a number that disappears completely, even for a twentieth of a
#: second, reads as the console having stopped answering.
VALUE_FADE_FLOOR = 0.15

#: The keys that get the brightening as well as the fade, and how long it
#: lasts. Temperatures only. They are the two figures on the strip that are
#: ever a reason to stop what you are doing, and a fan speed that flashed
#: every time it moved a percent would teach everybody to ignore the flash.
FLASH_KEYS = ("cpu", "rsx")
FLASH_MS = 320

#: How far towards the theme's strongest ink a flashed figure travels. Towards
#: `text` rather than towards white: in the light theme white is the page, and
#: a number that brightens into the page disappears instead of catching the
#: eye. Towards the full text colour it reads as emphasis in both themes.
FLASH_STRENGTH = 0.55

#: The strip arriving and leaving. In is the hundred and fifty to two hundred
#: that was asked for; out is shorter, because a strip on its way out is
#: describing a console that has already gone.
STRIP_IN_MS = 180
STRIP_OUT_MS = 150

#: How far the row travels as the strip comes and goes. Taken out of the
#: strip's own top and bottom margins, one against the other, so the contents
#: slide without the strip changing height and shoving the page below it.
STRIP_SLIDE_PX = 6


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
    # The mounted devices come off the same text rather than a second fetch.
    # The storage collector's own parser, so there is one answer in this
    # program to how much room a console has left.
    facts["devices"] = parse_storage(text)
    return facts


def free_space_text(facts, device="dev_hdd0"):
    """How much room is left on the console's drive, or "".

    free_bytes_for is the same reader the downloads use, and human_size is how
    every other size in this program is written, so the figure on the bar and
    the figure in a refusal to download cannot drift apart.

    A console that did not report its free space gets "", which the bar draws
    as nothing at all. None is not zero.
    """
    free = free_bytes_for((facts or {}).get("devices"), device)
    if free is None:
        return ""
    return f"{human_size(free)} free"


def relative_time(seconds):
    """How long ago, in the fewest words that are still true.

    Rounded down, so "4 min ago" is never said of something that happened
    three and a half minutes ago.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return "1 day ago" if days == 1 else f"{days} days ago"


def firmware_text(facts):
    """What the console is running, in two or three words, or "".

    Read from the console rather than asked, so "" is the honest answer for a
    console that did not say and the word unknown is never shown: a guess here
    is worse than a gap, because the firmware decides what this program is
    allowed to write.
    """
    facts = facts or {}
    kind = (facts.get("firmware_kind") or "").strip().lower()
    version = (facts.get("firmware_version") or "").strip()
    if kind == "hen":
        # HEN's own version when the page gave it, because that is the number
        # somebody is asked for; the firmware version otherwise.
        return f"HEN {(facts.get('hen_version') or version).strip()}".strip()
    if kind == "cfw":
        cobra = (facts.get("cobra_version") or "").strip()
        return f"CFW Cobra {cobra}" if cobra else f"CFW {version}".strip()
    if kind == "ofw":
        return f"OFW {version}".strip()
    return ""


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
    """Superseded by firmware_text, which names the kind as well.

    Kept as a function because FIELDS is read by name elsewhere, and removed
    from FIELDS itself: the strip showed "4.93 CEX" here while the fuller
    reading appeared a second time further along, and one console reported
    its firmware twice.
    """
    return None


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
#: The left-hand figures. Firmware is not among them: it is one of the three
#: readings on the right-hand end, and having it in both places is how the
#: same console came to report its firmware twice.
FIELDS = (
    ("cpu", "CPU", _cpu),
    ("rsx", "RSX", _rsx),
    ("fan", "Fan", _fan),
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
        self._fade = 1.0
        self._flash = 0.0
        # The reading that is waiting for the fade to reach its trough.
        self._pending = None
        # Both animations are owned by the figure they belong to, so they die
        # with it. The strip throws its rows away and builds new ones whenever
        # a console reports a different set of fields, and an animation left
        # ticking into a deleted label is a crash rather than a glitch.
        self._fading = QPropertyAnimation(self, b"fade", self)
        self._fading.setDuration(VALUE_FADE_MS)
        self._fading.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fading.setStartValue(1.0)
        self._fading.setKeyValueAt(0.5, VALUE_FADE_FLOOR)
        self._fading.setEndValue(1.0)
        self._fading.finished.connect(self._take_the_new_value)
        self._flashing = QPropertyAnimation(self, b"flash", self)
        self._flashing.setDuration(FLASH_MS)
        self._flashing.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._flashing.setStartValue(0.0)
        # Up quickly and down slowly. A temperature that brightened and dimmed
        # at the same rate reads as a throb; this reads as a nudge.
        self._flashing.setKeyValueAt(0.2, 1.0)
        self._flashing.setEndValue(0.0)
        theme.changed.connect(self.apply_theme)
        self.apply_theme()

    # -- the reading changing under somebody who is reading it

    @property
    def fade_animation(self):
        """The crossfade itself, for a test that wants to read it."""
        return self._fading

    @property
    def flash_animation(self):
        """The brightening itself, for a test that wants to read it."""
        return self._flashing

    def set_value(self, value, token):
        """A new reading for a figure that is already on the strip.

        Faded rather than swapped, and returns True when anything moved. The
        strip calls this instead of building a fresh row, because a label that
        has just been created has no old value to fade away from.

        Nothing waits on this. The reading has already arrived by the time it
        is called, and the figure the console reported is what `fields` says
        from the moment the fade reaches its trough.
        """
        if value == self.value.text() and token == self.token:
            return False
        self._pending = (value, token)
        self._fading.stop()
        self._fading.start()
        # Temperatures only, and never the first time a figure is drawn: the
        # flash says this number has moved, and everything moves on the first
        # reading.
        if self.key in FLASH_KEYS:
            self._flashing.stop()
            self._flashing.start()
        return True

    def finish_animations(self):
        """Jump to where the animations were going. For tests, and teardown.

        A test must not have to wait real time out to see where a figure
        ended up, and nothing functional is on the far side of one of these.
        """
        for animation in (self._fading, self._flashing):
            if animation.state() == QAbstractAnimation.State.Running:
                animation.setCurrentTime(animation.duration())
        self._take_the_new_value()

    def _take_the_new_value(self):
        if self._pending is None:
            return
        value, token = self._pending
        self._pending = None
        self.token = token
        self.value.setText(value)
        self.setAccessibleName(f"{self.caption.text()} {value}")
        self.apply_theme()

    def get_fade(self):
        return self._fade

    def set_fade(self, value):
        self._fade = float(value)
        # The swap happens at the bottom of the fade rather than at the top of
        # it, so the old number is at its faintest when it is replaced and
        # neither number is ever missing.
        if (self._pending is not None
                and self._fading.currentTime() * 2 >= self._fading.duration()):
            self._take_the_new_value()
            return
        self.apply_theme()

    fade = Property(float, get_fade, set_fade)

    def get_flash(self):
        return self._flash

    def set_flash(self, value):
        self._flash = float(value)
        self.apply_theme()

    flash = Property(float, get_flash, set_flash)

    def apply_theme(self):
        """Colour is never named here, only asked for.

        Set on the widgets rather than through the application stylesheet
        because the token a value is drawn in depends on the value: a
        temperature moves between three of them.
        """
        self.caption.setStyleSheet(
            f"color: {self._theme.colour('text_dim')};")
        self.value.setStyleSheet(
            f"color: {self._value_ink()}; font-weight: 600;")

    def _value_ink(self):
        """What the value is drawn in right now, animations and all.

        At rest this is the token's own colour and nothing more, so a settled
        strip is drawn in exactly what the theme says and the contrast figures
        that were checked against the palette are the ones on screen.
        """
        colour = self._theme.colour(self.token)
        if self._flash > 0.0:
            colour = mix(colour, self._theme.colour("text"),
                         FLASH_STRENGTH * self._flash)
        if self._fade >= 1.0:
            return colour
        ink = QColor(colour)
        return (f"rgba({ink.red()}, {ink.green()}, {ink.blue()},"
                f" {max(0.0, min(1.0, self._fade)):.3f})")

    def value_colour(self):
        """What the value settles at. For tests, and for review.

        The settled colour rather than the flashed one: the flash is a couple
        of hundred milliseconds of emphasis, and the question this answers is
        which band a temperature is in.
        """
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

        # The right-hand end. Everything above is a figure the console reports
        # about itself; these three are about the console's disk, its firmware
        # and what this program last did, so they sit apart from the figures
        # rather than in the run of them.
        self._row.addStretch(1)
        self._free_label = QLabel("", self)
        self._free_label.setObjectName("dim")
        self._free_label.setAccessibleName("Free space on the console")
        self._free_label.setVisible(False)
        self._row.addWidget(self._free_label, 0,
                            Qt.AlignmentFlag.AlignVCenter)

        self._firmware_label = QLabel("", self)
        self._firmware_label.setObjectName("dim")
        self._firmware_label.setAccessibleName("Console firmware")
        self._firmware_label.setVisible(False)
        self._row.addWidget(self._firmware_label, 0,
                            Qt.AlignmentFlag.AlignVCenter)

        self._event_label = QLabel("", self)
        self._event_label.setObjectName("dim")
        self._event_label.setAccessibleName("The last thing that happened")
        self._event_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                       | Qt.AlignmentFlag.AlignVCenter)
        self._event_label.setVisible(False)
        self._row.addWidget(self._event_label, 0,
                            Qt.AlignmentFlag.AlignVCenter)
        #: What last happened, and when, for the line on the right.
        self._event = ""
        self._event_at = 0.0

        # How far the strip is here: nought is gone, one is fully arrived. The
        # fade wants a graphics effect because the figures are real widgets
        # and a painter's opacity does not reach a child; the slide comes out
        # of the row's own margins, which is why it does not move the page.
        self._presence = 1.0
        # The row's resting margins, read rather than written again here, so
        # that moving the strip's padding does not silently move the slide.
        self._row_margins = self._row.getContentsMargins()
        self._fade_effect = QGraphicsOpacityEffect(self)
        self._fade_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._fade_effect)
        # Owned by the strip, in the same way the launcher owns its resync
        # timer: a singleShot left over from a widget that has gone is a crash
        # on shutdown rather than a dropped frame.
        self._arriving = QPropertyAnimation(self, b"presence", self)
        self._arriving.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._arriving.setDuration(STRIP_IN_MS)
        self._arriving.finished.connect(self._settled)

        self._theme.changed.connect(self._apply_theme)
        self._connection.changed.connect(self._connection_changed)
        self._apply_theme()
        # Absent, not empty, and absent is also how it starts: constructing the
        # home screen must not put a request on the wire.
        self.setVisible(False)

    # -- coming and going

    @property
    def presence_animation(self):
        """The arrive and leave animation, for a test that wants to read it."""
        return self._arriving

    def get_presence(self):
        return self._presence

    def set_presence(self, value):
        self._presence = float(value)
        self._fade_effect.setOpacity(max(0.0, min(1.0, self._presence)))
        # Down and out, up and in. The two margins move against each other so
        # the strip keeps its height while its contents travel.
        drop = round(STRIP_SLIDE_PX * (1.0 - self._presence))
        left, top, right, bottom = self._row_margins
        self._row.setContentsMargins(left, top + drop, right, bottom - drop)

    presence = Property(float, get_presence, set_presence)

    def finish_animations(self):
        """Jump every animation to its end. For tests, and for a teardown.

        A test must not have to wait a fifth of a second of real time to see
        where the strip ended up. Nothing functional is on the far side of one
        of these, so cutting them short changes only what is on the screen.
        """
        if self._arriving.state() == QAbstractAnimation.State.Running:
            self._arriving.setCurrentTime(self._arriving.duration())
        for stat in self._stats:
            stat.finish_animations()

    def _settled(self):
        # Only the leaving end hides. Qt cannot draw a widget that is not
        # visible, so the widget stays up for as long as the fade does and
        # goes when it is over. Nothing is waiting on that: the connection
        # changed state before this was called, and `visibility_changed` has
        # already told the home screen to close the gap.
        if self._presence <= 0.0:
            self.setVisible(False)

    def _arrive_or_leave(self, showing):
        """Fade and slide in, or fade and slide out.

        Called after the strip has finished deciding what it says, so it can
        never hold a reading up. A strip already in the state it is asked for
        does nothing at all.
        """
        self._arriving.stop()
        if showing:
            if self.isVisible() and self._presence >= 1.0:
                return
            if not self.isVisible():
                self.set_presence(0.0)
                self.setVisible(True)
            self._arriving.setDuration(STRIP_IN_MS)
            self._arriving.setStartValue(self._presence)
            self._arriving.setEndValue(1.0)
            self._arriving.start()
            return
        if not self.isVisible():
            self.set_presence(0.0)
            return
        self._arriving.setDuration(STRIP_OUT_MS)
        self._arriving.setStartValue(self._presence)
        self._arriving.setEndValue(0.0)
        self._arriving.start()

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
        found = read_fields(self._facts)
        if self._same_figures(found):
            # The same fields with new numbers in them, so the rows stay and
            # take the new values. Rebuilding them is what a change looked
            # like before: a label that has just been created has no old value
            # to fade away from.
            for stat, (_key, _label, value, token) in zip(self._stats, found):
                stat.set_value(value, token)
            self.setToolTip(self._summary())
            self.updateGeometry()
            self.visibility_changed.emit(True)
            return

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

        for index, (key, label, value, token) in enumerate(found):
            if index:
                rule = _Rule(self._theme, self)
                self._row.insertWidget(self._trailing_at(), rule,
                                       0, Qt.AlignmentFlag.AlignVCenter)
                rule.show()
            stat = _Stat(key, label, value, token, self._theme, self)
            self._row.insertWidget(self._trailing_at(), stat,
                                   0, Qt.AlignmentFlag.AlignVCenter)
            stat.show()
            self._stats.append(stat)

        self.setToolTip(self._summary())
        showing = bool(found)
        self._arrive_or_leave(showing)
        self.updateGeometry()
        self.visibility_changed.emit(showing)

    def _trailing_at(self):
        """Where a new figure goes: before the stretch and the right-hand end.

        Counted rather than written down, because the number of widgets after
        the figures is a thing that changes and an index that guessed it would
        put a figure on the wrong side of the stretch.
        """
        return self._row.count() - 4

    def note_event(self, text):
        """Record the last thing this program did, for the line on the right.

        An empty text clears it. The age beside it is worked out when the line
        is drawn rather than stored, so it stays true without anything having
        to tick on its account.
        """
        self._event = (text or "").strip()
        self._event_at = time.monotonic() if self._event else 0.0
        self._paint_right()

    def event_text(self):
        if not self._event:
            return ""
        ago = relative_time(time.monotonic() - self._event_at)
        return f"{self._event} · {ago}"

    def free_text(self):
        return self._free_label.text()

    def firmware_text_shown(self):
        return self._firmware_label.text()

    def _paint_right(self):
        """The three readings on the right, each hidden until it has one."""
        free = free_space_text(self._facts) if self._facts else ""
        self._free_label.setText(free)
        self._free_label.setVisible(bool(free))
        firmware = firmware_text(self._facts) if self._facts else ""
        self._firmware_label.setText(firmware)
        self._firmware_label.setVisible(bool(firmware))
        event = self.event_text()
        self._event_label.setText(event)
        self._event_label.setVisible(bool(event))

    def _same_figures(self, found):
        """Whether the row on screen holds exactly these fields, in order."""
        if not found or not self._stats:
            return False
        return ([stat.key for stat in self._stats]
                == [key for key, _label, _value, _token in found])

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
