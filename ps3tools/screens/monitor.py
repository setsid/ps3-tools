"""Monitor: what the console is doing, as it does it.

Everywhere else in this program reads the console once and tells you what it
found. This screen reads it again every few seconds and draws the answer, so a
figure that only means something as a trend means something here: a fan that
climbs for ten minutes and never comes down, a CPU that sits at seventy-five
while the console is idle, a drive that loses a gigabyte every time a game is
launched.

It polls, which nothing else in this program does, so the rules around that are
written down rather than left to the reader.

It polls only while it is the screen in front of somebody. Entering starts it,
leaving stops it, and a window left on another tool has nothing on the wire.
Polling a console for a graph nobody is looking at costs the console something
and buys nobody anything. The one exception is a tick box that says plainly
what it does: with it on the reading carries on after somebody leaves, which
is what watching a console through a session of play needs. It is off by
default, it is not saved, and the shell's event line is told when it keeps
going.

It polls only the console's own status page, which is the same page the
diagnostics collector reads and is on the transport's read-only allowlist. The
front page, which lists installed games and is the expensive one to draw, is
read once at the start and then rarely, because the only figure on it that
changes in an hour is the free space.

It keeps its readings in memory for as long as the program is open and writes
nothing anywhere unless somebody presses Save. A file of when somebody's
console was warm is not this program's to create unasked.

A console that stops answering leaves a break in the line and a count in the
footer. Nothing is interpolated and nothing is assumed to have carried on.
"""

import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from ps3diag.parsers import (html_to_text, parse_cpursx, parse_hdd_free,
                             parse_identity, parse_running_title,
                             parse_storage)

from ps3tools import telemetry
from ps3tools.shell import widgets
from ps3tools.shell.chart import Chart, TitleBand
from ps3tools.shell.consolestats import (firmware_text, free_space_text,
                                         make_probe, running_title,
                                         temperature_token, TIMEOUT)
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

WHAT_THIS_IS = (
    "Reads the console's status page every few seconds while this screen is "
    "open, and graphs what it says. Nothing is recorded anywhere unless you "
    "save it, and nothing is read once you leave unless you ask below.")

NOT_CONNECTED = (
    "Type the console's address into the bar at the top of the window, then "
    "come back here.")

WAITING = "Waiting for the first reading."

#: The heading over the list of what crossed a threshold, and what it says
#: when nothing has. Said as a fact about the recording rather than as
#: reassurance: a console that has been read for ten seconds has not proved
#: anything about its temperature.
EVENTS_HEADING = "Heat"

NOTHING_CROSSED = "Nothing has crossed {level} in what has been recorded."

#: How many episodes are listed per figure. The newest, because a console
#: that has been above eighty forty times this afternoon needs a line saying
#: so rather than forty lines.
EPISODE_LIMIT = 4

#: Which graph carries the band saying what was loaded. The temperatures,
#: because the reason for having it is that a climb and the launch that
#: caused it are the same moment.
BAND_ON = "temperature"

#: Where a readings file is looked for, and what the dialogue filters to.
CSV_FILTER = "Readings (*.csv);;All files (*)"

KEEP_RECORDING = "Keep recording when I leave this screen"

KEEP_RECORDING_HINT = (
    "With it on, the console is still being read while you are using another "
    "tool. It is forgotten when the program closes.")

#: The page every reading comes off. One page rather than two: it carries the
#: temperatures, the fan, the clocks and the firmware, and it is the cheapest
#: page webMAN draws.
SAMPLE_PATHS = ("/cpursx.ps3",)

#: With the front page added, for the readings that also want free space. The
#: front page lists every installed game, so a console with a full drive takes
#: a noticeable moment to draw it.
STORAGE_PATHS = ("/", "/cpursx.ps3")

#: How often the free space is read, counted in readings. At the default
#: interval that is once a minute, which is often enough for a figure that
#: moves when a game is installed and at no other time.
STORAGE_EVERY = 12

#: The intervals on offer, in seconds, and the default. Two seconds is there
#: for watching a console warm up under load; a minute is there for leaving it
#: on the second monitor for an afternoon.
INTERVALS = ((2, "every 2 seconds"), (5, "every 5 seconds"),
             (10, "every 10 seconds"), (30, "every 30 seconds"),
             (60, "every minute"))
DEFAULT_INTERVAL = 5

#: How much history the graphs show. "Everything" is bounded by the history's
#: own cap rather than by this.
SPANS = ((300, "last 5 minutes"), (900, "last 15 minutes"),
         (3600, "last hour"), (None, "everything"))
DEFAULT_SPAN = 900


def default_csv_path(when=None):
    """Where the Save dialogue opens.

    The Desktop, because the diagnostic report and the downloaded update
    already go there and somebody who has used this program once knows where
    to look.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    return os.path.join(os.path.expanduser("~"), "Desktop",
                        f"ps3-monitor-{stamp}.csv")


def read_sample(host, probe_factory=None, cancelled=lambda: False,
                storage=False):
    """One reading, as a facts dictionary. Empty when the console said nothing.

    The same parsers the diagnostics collector uses, over the same pages, so a
    figure on this graph and the same figure in a report cannot disagree.

    Never raises for a network problem: HttpProbe turns those into a response
    that is simply not ok, and a console that went off mid-graph is an ordinary
    thing rather than an error.
    """
    probe = (probe_factory or make_probe)(host, TIMEOUT)
    parts = []
    for path in (STORAGE_PATHS if storage else SAMPLE_PATHS):
        if cancelled():
            return {}
        response = probe.get(path)
        if response.ok:
            parts.append(html_to_text(response.body))
    if not parts:
        return {}
    text = "\n".join(parts)
    facts = dict(parse_identity(text))
    facts.update(parse_cpursx(text))
    facts["running_title"] = parse_running_title(text)
    if storage:
        facts["devices"] = parse_storage(text)
        free = parse_hdd_free(text)
        if free is not None:
            facts["hdd_free_bytes"] = free
    return facts


class _Readout(QWidget):
    """One figure, large, with what it has done underneath.

    The current value is the size it is because it is the thing being read
    from across a desk. The low and high are the ones on the graph beside it,
    so the two describe the same window.
    """

    def __init__(self, series, parent=None):
        super().__init__(parent)
        self.series = series
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.label = QLabel(series.label)
        small = self.label.font()
        small.setPointSize(max(6, small.pointSize() - 1))
        self.label.setFont(small)
        layout.addWidget(self.label)
        self.value = QLabel("")
        big = self.value.font()
        big.setPointSize(big.pointSize() + 8)
        big.setBold(True)
        self.value.setFont(big)
        layout.addWidget(self.value)
        self.range = QLabel("")
        self.range.setFont(small)
        layout.addWidget(self.range)

    def show_reading(self, latest, lowest, highest, colour, dim):
        self.value.setText(telemetry.format_value(latest, self.series)
                           or "no reading")
        self.value.setStyleSheet(f"color: {colour};")
        self.label.setStyleSheet(f"color: {dim};")
        self.range.setStyleSheet(f"color: {dim};")
        if lowest is None:
            self.range.setText("")
            return
        self.range.setText(
            f"low {telemetry.format_value(lowest, self.series)}   "
            f"high {telemetry.format_value(highest, self.series)}")


@register
class MonitorScreen(Screen):
    """Poll the console while this screen is open and graph the readings."""

    key = "monitor"
    title = "Monitor"
    blurb = "Graphs the console's temperatures, fan and free space as it runs."
    tile = "MO"
    # Next to the diagnostics card. Both of them answer "what is this console
    # doing", one as a snapshot and one over time.
    order = 20

    #: A line for the shell's event strip, when the shell has wired it.
    event_noted = Signal(str)

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        self.history = telemetry.History()
        self._task = None
        self._interval = DEFAULT_INTERVAL
        self._span = DEFAULT_SPAN
        self._running = False
        self._entered = False
        #: Which console the readings are from, so a new address starts a new
        #: graph rather than joining two consoles into one line.
        self._host = ""
        #: Counted so the front page is read occasionally and not every time.
        self._reads = 0
        #: A tick that arrived while the previous read was still out. Counted
        #: and reported: it is what a console too slow for the chosen interval
        #: looks like, and the answer is a longer interval.
        self._overlaps = 0
        #: The last facts, for the lines that are text rather than a graph.
        self._facts = {}
        #: The file whose readings are on the graphs, when they came from a
        #: file rather than a console. Recording again empties it, because a
        #: file's readings and a live console's on one graph would be a graph
        #: that lied about when any of it happened.
        self._loaded_from = ""
        #: Replaceable so a test never touches a network.
        self.sampler = read_sample
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.CoarseTimer)
        self._timer.timeout.connect(self._tick)
        self._build()
        self.connection.changed.connect(self._connection_changed)
        if self.theme is not None:
            try:
                self.theme.changed.connect(self._apply_theme)
            except (AttributeError, RuntimeError):
                pass
        self._apply_theme()
        self._update_controls()

    # -- construction

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        heading = QLabel(self.title)
        font = heading.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)

        blurb = QLabel(WHAT_THIS_IS)
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        layout.addLayout(self._build_controls())
        layout.addLayout(self._build_keep_row())
        layout.addWidget(self._build_readouts())

        self.charts = {}
        self.band = None
        for key, title, keys in telemetry.GRAPHS:
            layout.addWidget(self._build_chart(key, title, keys), 1)

        layout.addWidget(self._build_events())

        self.status = QLabel(WAITING)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _build_controls(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        self.start_button = QPushButton("Start")
        widgets.set_role(self.start_button, widgets.PRIMARY)
        self.start_button.clicked.connect(self._on_start_pressed)
        row.addWidget(self.start_button)

        self.interval_box = QComboBox()
        for seconds, words in INTERVALS:
            self.interval_box.addItem(words, seconds)
        self.interval_box.setCurrentIndex(
            [seconds for seconds, _w in INTERVALS].index(DEFAULT_INTERVAL))
        self.interval_box.currentIndexChanged.connect(self._on_interval)
        row.addWidget(self.interval_box)

        self.span_box = QComboBox()
        for seconds, words in SPANS:
            self.span_box.addItem(words, seconds)
        self.span_box.setCurrentIndex(
            [seconds for seconds, _w in SPANS].index(DEFAULT_SPAN))
        self.span_box.currentIndexChanged.connect(self._on_span)
        row.addWidget(self.span_box)

        row.addStretch(1)

        self.open_button = QPushButton("Open readings")
        self.open_button.clicked.connect(self._on_open_pressed)
        row.addWidget(self.open_button)

        self.save_button = QPushButton("Save readings")
        self.save_button.clicked.connect(self._on_save_pressed)
        row.addWidget(self.save_button)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._on_clear_pressed)
        row.addWidget(self.clear_button)
        return row

    def _build_keep_row(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        self.keep_box = QCheckBox(KEEP_RECORDING)
        self.keep_box.setToolTip(KEEP_RECORDING_HINT)
        # Not saved with the settings, on purpose. Everything else in this
        # tool reads the console only while somebody is looking at it, and an
        # opt-out of that which came back by itself on the next launch would
        # be a program polling a console nobody asked it to.
        self.keep_box.setChecked(False)
        row.addWidget(self.keep_box)
        hint = QLabel(KEEP_RECORDING_HINT)
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        hint.setMaximumWidth(620)
        row.addWidget(hint, 1)
        return row

    def _build_readouts(self):
        frame = QFrame()
        frame.setObjectName("statepanel")
        grid = QGridLayout(frame)
        grid.setContentsMargins(16, 12, 16, 12)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)
        self.readouts = {}
        for column, series in enumerate(telemetry.SERIES):
            readout = _Readout(series)
            self.readouts[series.key] = readout
            grid.addWidget(readout, 0, column)
        grid.setColumnStretch(len(telemetry.SERIES), 1)

        # The three facts that are worth having on the screen and are not
        # worth a graph: they change once an hour at most, or not at all.
        self.facts_label = QLabel("")
        self.facts_label.setWordWrap(True)
        self.facts_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.facts_label.setSizePolicy(QSizePolicy.Expanding,
                                       QSizePolicy.Preferred)
        grid.addWidget(self.facts_label, 0, len(telemetry.SERIES) + 1)
        return frame

    def _build_chart(self, key, title, keys):
        frame = QFrame()
        frame.setObjectName("statepanel")
        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 10, 12, 8)
        box.setSpacing(4)

        head = QHBoxLayout()
        label = QLabel(title)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        head.addWidget(label)
        head.addStretch(1)
        legend = QLabel("")
        legend.setTextFormat(Qt.RichText)
        head.addWidget(legend)
        box.addLayout(head)

        chart = Chart(keys, heading=title)
        chart.set_history(self.history)
        chart.set_span(self._span)
        chart.set_interval(self._interval)
        chart.hovered.connect(self._on_hover)
        box.addWidget(chart, 1)
        self.charts[key] = chart
        chart.legend_label = legend
        if key == BAND_ON:
            # In this panel rather than one of its own, so that the blocks
            # and the part of the line above them are the same pixels wide:
            # every graph here has the same margins, and a band in a frame
            # with different ones would point at the wrong minute.
            self.band = TitleBand(frame)
            self.band.set_history(self.history)
            self.band.set_span(self._span)
            self.band.set_interval(self._interval)
            box.addWidget(self.band)
        return frame

    def _build_events(self):
        """The list of what crossed a threshold, under the graphs.

        A list rather than a colour on a figure. A temperature that was above
        eighty for six minutes while somebody was playing is the answer to
        why the console turned itself off, and it is gone from the figures
        the moment it comes back down.
        """
        frame = QFrame()
        frame.setObjectName("statepanel")
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(4)
        heading = QLabel(EVENTS_HEADING)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        box.addWidget(heading)
        self.events_label = QLabel("")
        self.events_label.setWordWrap(True)
        self.events_label.setTextFormat(Qt.RichText)
        box.addWidget(self.events_label)
        return frame

    # -- theme

    def _apply_theme(self):
        if self.theme is None:
            return
        for chart in getattr(self, "charts", {}).values():
            chart.set_theme(self.theme)
        if getattr(self, "band", None) is not None:
            self.band.set_theme(self.theme)
        self._paint_readouts()
        self._paint_legends()
        self._paint_events()

    def _colour(self, token, fallback="#888888"):
        if self.theme is None:
            return fallback
        try:
            return self.theme.colour(token)
        except Exception:
            return fallback

    # -- running

    def on_enter(self):
        self._entered = True
        self._forget_other_console()
        # A file on the graphs is left alone. Coming back to a screen that
        # was showing a saved afternoon and finding it replaced by a fresh
        # reading of the console would throw away the thing being looked at.
        if self.connection.connected and not self._loaded_from:
            self.start()
        else:
            self._update_status()
        self._update_controls()

    def on_leave(self):
        self._entered = False
        if self._running and self.keep_box.isChecked():
            # Asked for, explicitly, by somebody who ticked a box that is off
            # by default. The shell's event line is told, because a program
            # reading a console while its screen is elsewhere should say so
            # somewhere the person can see.
            self.event_noted.emit("Monitor is still recording")
            self.status_message.emit("Monitor is still recording.")
            return
        self.stop()

    def start(self):
        """Begins polling. Reads at once rather than after the first interval.

        Waiting a whole interval before the first reading means a screen that
        sits empty for up to a minute after it was opened, which reads as a
        tool that did not work.
        """
        if not self.connection.connected or not self.connection.host:
            return
        if self._loaded_from:
            # The file goes when recording starts. Said in the footer rather
            # than asked about: the readings are still in the file they came
            # from, so nothing is lost by this.
            self._fresh_history()
        self._host = self.connection.host
        self._running = True
        self._timer.setInterval(self._interval * 1000)
        self._timer.start()
        self._update_controls()
        self._tick()

    def stop(self):
        self._running = False
        self._timer.stop()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._update_controls()
        self._update_status()

    @property
    def running(self):
        return self._running

    @property
    def interval(self):
        return self._interval

    @property
    def span(self):
        return self._span

    def _connection_changed(self):
        host = self.connection.host
        if host != self._host:
            self._forget_other_console()
        if not self.connection.connected:
            if self._running:
                self.stop()
            else:
                self._update_controls()
                self._update_status()
            return
        if self._entered and not self._running and not self._loaded_from:
            self.start()

    def _forget_other_console(self):
        """Drops the readings when the address changes.

        Two consoles on one graph would be a line that jumped, and there is
        nothing on the graph that says which console a point came from.
        """
        host = self.connection.host
        if host == self._host or self._loaded_from:
            # A file belongs to no console, so a change of address says
            # nothing about whether its readings are still worth showing.
            return
        self._host = host
        if len(self.history):
            self._fresh_history()
            self._refresh()

    # -- one reading

    def _tick(self):
        if not self._running:
            return
        if self._task is not None:
            # The console has not answered the previous request yet. Skipping
            # is the honest thing: two requests in flight would arrive out of
            # order and put a point in the past.
            self._overlaps += 1
            self._update_status()
            return
        host = self._host or self.connection.host
        if not host:
            self.stop()
            return
        storage = self._reads % STORAGE_EVERY == 0
        sampler = self.sampler

        def work(control):
            return sampler(host, cancelled=lambda: control.cancelled,
                           storage=storage)

        task = self.submit(work)
        self._task = task
        task.finished.connect(self._arrived)
        task.failed.connect(self._failed)
        task.done.connect(lambda: self._finished(task))

    def _finished(self, task):
        if self._task is task:
            self._task = None

    def _arrived(self, facts):
        self._reads += 1
        if facts:
            self._facts = facts
        self.history.append(facts)
        self._refresh()

    def _failed(self, message):
        """A read that raised rather than a console that stayed quiet.

        Counted as a miss the same way, because the graph has nothing to draw
        either way, and the message goes to the footer so that a broken
        install says something rather than looking like a quiet console.
        """
        self._reads += 1
        self.history.misses += 1
        self.history.last_miss_at = time.time()
        self._refresh(message)

    # -- drawing

    def _refresh(self, problem=""):
        for chart in self.charts.values():
            chart.refresh()
        if self.band is not None:
            self.band.refresh()
        self._paint_readouts()
        self._paint_legends()
        self._paint_events()
        # Save and Clear are about whether there are any readings, so they
        # move when a reading arrives. Without this the first reading landed
        # on a screen whose Save button was still disabled.
        self._update_controls()
        self._update_status(problem)

    def _paint_readouts(self):
        dim = self._colour("text_dim")
        for series in telemetry.SERIES:
            readout = self.readouts.get(series.key)
            if readout is None:
                continue
            latest = self.history.latest(series.key)
            lowest, highest = self.history.extremes(series.key, self._span)
            readout.show_reading(latest, lowest, highest,
                                 self._value_colour(series, latest), dim)
        self.facts_label.setText(self._facts_words())

    def _value_colour(self, series, value):
        """Temperatures carry the warning colours, everything else the ink.

        The thresholds are the strip's, so a temperature that is orange on the
        home screen is orange here.
        """
        if value is None:
            return self._colour("text_dim")
        if series.unit == "°C":
            return self._colour(temperature_token(float(value)))
        return self._colour("text")

    def _facts_words(self):
        """Firmware, what is running, and how long it has been up.

        Left empty rather than filled with dashes when the console did not
        say. A console that reports none of this should show three blank lines
        instead of three claims.
        """
        lines = []
        firmware = firmware_text(self._facts)
        if firmware:
            lines.append(f"Firmware {firmware}")
        game = running_title(self._facts)
        if game:
            # Named where this program knows the game, the same way the band
            # under the graphs names it, so the two do not read as two
            # different things being reported.
            lines.append(f"Running {telemetry.title_words(game) or game}")
        free = free_space_text(self._facts)
        if free:
            # free_space_text says "566.9 GB free" already.
            lines.append(free)
        uptime = self._facts.get("uptime")
        if uptime:
            lines.append(f"Up {uptime}")
        return "\n".join(lines)

    def event_lines(self):
        """[(token, sentence)] for the heat list, worst first.

        Episodes rather than a count of readings: a console that touched 81
        once and a console that sat above eighty for six minutes cross the
        same line, and only one of them explains a console that switched
        itself off. Only the highest level a figure crossed is listed, so an
        afternoon above seventy does not bury the four minutes above eighty.

        Returned as words so a test can read the sentence rather than watch a
        label change, and so the same sentences can go in a report later.
        """
        gap = self._interval * telemetry.GAP_FACTOR
        lines = []
        for series in telemetry.SERIES:
            if not series.levels:
                continue
            points = self.history.points(series.key, self._span)
            if not points:
                continue
            for value, token in reversed(series.levels):
                found = telemetry.episodes(points, value, gap)
                if not found:
                    continue
                newest = self.history.last_at
                for start, end, peak in found[-EPISODE_LIMIT:]:
                    lines.append((token, self._episode_words(
                        series, value, start, end, peak, newest)))
                if len(found) > EPISODE_LIMIT:
                    lines.append((token, f"{len(found) - EPISODE_LIMIT} "
                                  f"earlier spells above "
                                  f"{telemetry.format_value(value, series)} "
                                  f"are not listed."))
                break
        if not lines:
            lowest = min(value for series in telemetry.SERIES
                         for value, _token in series.levels)
            series = telemetry.series_for("cpu")
            lines.append(("text_dim", NOTHING_CROSSED.format(
                level=telemetry.format_value(lowest, series))))
        peak = self._peak_words()
        if peak:
            lines.append(("text_dim", peak))
        return lines

    def _episode_words(self, series, level, start, end, peak, newest):
        """One episode as a sentence, with the times a person can act on."""
        words = telemetry.format_value(level, series)
        top = telemetry.format_value(peak, series)
        from_at = time.strftime("%H:%M:%S", time.localtime(start))
        lasted = telemetry.duration_words(end - start)
        if newest is not None and end >= newest:
            # Still going: the console is above the line as this is read, and
            # saying it ended at the time of the last reading would be a
            # statement about a console that has not stopped doing it.
            return (f"{series.label} has been above {words} since {from_at}, "
                    f"{lasted} so far, peaking at {top}.")
        to_at = time.strftime("%H:%M:%S", time.localtime(end))
        return (f"{series.label} was above {words} from {from_at} to "
                f"{to_at}, {lasted}, peaking at {top}.")

    def _peak_words(self):
        """The highest each temperature reached, which is worth saying even
        when nothing crossed a line."""
        parts = []
        for series in telemetry.SERIES:
            if not series.levels:
                continue
            _low, high = self.history.extremes(series.key, self._span)
            if high is None:
                continue
            parts.append(f"{series.label} "
                         f"{telemetry.format_value(high, series)}")
        if not parts:
            return ""
        return "Highest recorded: " + ", ".join(parts) + "."

    def _paint_events(self):
        dim = self._colour("text_dim")
        rows = []
        for token, words in self.event_lines():
            colour = dim if token == "text_dim" else self._colour(token)
            rows.append(f'<div style="color:{colour}">{words}</div>')
        self.events_label.setText("".join(rows))

    def _paint_legends(self):
        """The line colours, named, above each graph.

        Two temperatures on one graph need saying which is which, and a key
        drawn inside the plot would sit on top of the lines it describes.
        """
        for key, _title, keys in telemetry.GRAPHS:
            chart = self.charts.get(key)
            if chart is None:
                continue
            parts = []
            for name in keys:
                series = telemetry.series_for(name)
                colour = self._colour(series.token)
                latest = self.history.latest(name)
                words = telemetry.format_value(latest, series) or "no reading"
                parts.append(
                    f'<span style="color:{colour}">&#9632;</span> '
                    f'{series.label} {words}')
            chart.legend_label.setText("&nbsp;&nbsp;".join(parts))

    def _update_controls(self):
        connected = self.connection.connected
        self.start_button.setText("Stop" if self._running else "Start")
        self.start_button.setEnabled(connected or self._running)
        self.save_button.setEnabled(bool(len(self.history)))
        self.clear_button.setEnabled(bool(len(self.history)))

    def _update_status(self, problem=""):
        self.status.setText(self.status_words(problem))

    def status_words(self, problem=""):
        """The footer, which is the only place a miss is reported.

        Built as a string by a method so that a test can read the sentence
        rather than watch for a label changing.
        """
        if self._loaded_from and len(self.history):
            count = len(self.history)
            words = "reading" if count == 1 else "readings"
            first = self.history.first_at
            when = time.strftime("%d %b %Y, %H:%M",
                                 time.localtime(first)) if first else ""
            return (f"Showing {count} {words} from "
                    f"{os.path.basename(self._loaded_from)}, recorded "
                    f"{when}. Press Start to record from the console again.")
        if not self.connection.connected and not len(self.history):
            return NOT_CONNECTED
        parts = []
        count = len(self.history)
        if count:
            words = "reading" if count == 1 else "readings"
            interval = dict(INTERVALS).get(self._interval,
                                           f"every {self._interval} seconds")
            parts.append(f"{count} {words}, {interval}.")
            last = self.history.last_at
            if last:
                parts.append(
                    f"Last at {time.strftime('%H:%M:%S', time.localtime(last))}.")
        elif self._running:
            parts.append(WAITING)
        else:
            parts.append("Not reading the console.")
        if self.history.misses:
            times = "time" if self.history.misses == 1 else "times"
            parts.append(f"{self.history.misses} {times} the console did not "
                         f"answer.")
        if self._overlaps:
            parts.append(f"{self._overlaps} readings were skipped because the "
                         f"console was still answering the one before. A "
                         f"longer interval would suit it better.")
        if not self._running and count:
            parts.append("Stopped.")
        if problem:
            parts.append(problem)
        return " ".join(parts)

    # -- controls

    def _on_start_pressed(self):
        if self._running:
            self.stop()
        else:
            self.start()

    def _on_interval(self, _index):
        self._interval = self.interval_box.currentData()
        for chart in self.charts.values():
            chart.set_interval(self._interval)
        if self.band is not None:
            self.band.set_interval(self._interval)
        if self._running:
            self._timer.setInterval(self._interval * 1000)
        self._update_status()

    def _on_span(self, _index):
        self._span = self.span_box.currentData()
        for chart in self.charts.values():
            chart.set_span(self._span)
        if self.band is not None:
            self.band.set_span(self._span)
        self._paint_readouts()
        self._paint_events()

    def _on_clear_pressed(self):
        self._fresh_history()
        self._refresh()
        self._update_controls()

    def _fresh_history(self, history=None):
        """Start again, and hand the new history to everything drawing it.

        One place, because a history handed to the graphs and not to the band
        is a band still showing the game from the console before last.
        """
        self.history = history if history is not None else telemetry.History()
        self._facts = {}
        self._reads = 0
        self._overlaps = 0
        self._loaded_from = ""
        for chart in self.charts.values():
            chart.set_history(self.history)
        if self.band is not None:
            self.band.set_history(self.history)

    def _on_hover(self, reading):
        """The readout follows the pointer over a graph.

        Reading a value off a line by eye is guesswork, and this is a graph
        somebody is looking at to answer "how hot did it get while I was in
        that lobby".
        """
        if reading is None:
            self._paint_readouts()
            return
        dim = self._colour("text_dim")
        stamp = time.strftime("%H:%M:%S", time.localtime(reading.at))
        for series in telemetry.SERIES:
            readout = self.readouts.get(series.key)
            if readout is None:
                continue
            value = reading.value(series.key)
            readout.show_reading(value, None, None,
                                 self._value_colour(series, value), dim)
            readout.range.setText(f"at {stamp}")

    def _on_open_pressed(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open readings", os.path.dirname(default_csv_path()),
            CSV_FILTER)
        if not path:
            return
        self.load_csv(path)

    def load_csv(self, path):
        """Graphs a saved file. Separate from the dialogue so a test can call it.

        Recording stops. A file's readings and a live console's on one graph
        would be one line made of two afternoons, and the time axis would
        have nothing to say about either.
        """
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            self.status.setText(
                f"Could not read {path}: {error.strerror or error}.")
            return False
        try:
            history = telemetry.History.from_csv(text, path=path)
        except telemetry.ReadingsFileError as error:
            # The message names the file, the field and what was expected,
            # which is the whole reason for that exception carrying them.
            self.status.setText(str(error))
            return False
        self.stop()
        self._fresh_history(history)
        self._loaded_from = path
        self._refresh()
        self._update_controls()
        self.status_message.emit(f"Opened {os.path.basename(path)}.")
        return True

    @property
    def loaded_from(self):
        """The file on the graphs, or "" when these came off a console."""
        return self._loaded_from

    def _on_save_pressed(self):
        if not len(self.history):
            return
        suggested = default_csv_path()
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save readings", suggested, "CSV files (*.csv)")
        if not path:
            return
        self.save_csv(path)

    def save_csv(self, path):
        """Writes the history. Separate from the dialogue so a test can call it."""
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(self.history.to_csv())
        except OSError as error:
            self.status.setText(
                f"Could not write {path}: {error.strerror or error}.")
            return False
        self.status.setText(f"Saved {len(self.history)} readings to {path}.")
        self.status_message.emit(f"Saved {os.path.basename(path)}.")
        self.event_noted.emit("Monitor readings saved")
        return True
