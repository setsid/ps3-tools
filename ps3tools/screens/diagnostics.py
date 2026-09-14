"""The diagnostics screen: collect from a console, analyse, show what is wrong.

A port of the old one-window tkinter tool onto the Qt shell. The collection
itself is unchanged, because it was never the problem: everything here calls
into ps3diag exactly as the old window did, on a worker, and interprets nothing
of its own.

Two things are deliberately different from the window this replaces.

The first is the status bug. The old window drove one status line from two
unrelated facts, so a subnet scan that found nothing would sit there in red
saying "No PS3 found on this network" while a collection from the typed address
was working perfectly. The fix here is absolute rather than careful: this
screen does not render scan state at all. It never reads connection.scan, it
owns no search control, and the only reachability it ever writes is the verdict
of a collection it actually ran. The address and the search live in the top
bar, which is the one place either belongs.

The second is the findings list. It used to be one long scrolling block of text
with the severity mixed into the prose, which for an audience that does not
know what any of it means is the same as no findings at all. Findings are now
cards grouped under a severity heading and filterable by severity, each
carrying its explanation and its suggested fix in full. Neither of those is
behind a disclosure control on purpose: somebody who cannot read the title will
not click to expand it. Evidence is the one thing that folds away, because it
is the part the user is not expected to read.
"""

import os
import subprocess
import sys
import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ps3diag import APP_NAME, READ_ONLY_NOTICE, config, report, runner
from ps3diag.analysis import analyse
from ps3diag.artefacts import ArtefactSet
from ps3diag.collectors import CATEGORIES
from ps3diag.findings import SEVERITIES, SEVERITY_LABELS
from ps3diag.logging_json import RunLog

from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

# Three across, and the grid is built to that number in two places.
COLUMNS = 3

# Enough for a status label plus its descenders at the shell's smallest window.
ROW_HEIGHT = 24

# How far a sub-option sits in from the tick boxes it belongs under. Roughly a
# tick indicator plus its gap, so it reads as subordinate to the row above.
SUB_INDENT = 26

# The running commentary. Big enough to read across the room, because during a
# collection that takes minutes it is the only thing on the screen that moves.
ACTIVITY_POINT_SIZE = 15
# Nine or so lines of history at the sizes the shell uses. Enough to see that
# the run is working through a folder rather than stuck on one file.
LOG_HEIGHT = 150
# A four minute run can emit a few thousand lines and none of the old ones are
# worth the memory.
LOG_LINES = 400

IDLE_ACTIVITY = "Not collecting. Press Collect diagnostics to start."

# Said in full on the tick box's own line, because the cost of this option is
# the whole reason it is an option.
IDENTIFY_LABEL = ("Read inside every disc image to identify it "
                  "(slow: adds minutes)")
IDENTIFY_HINT = (
    "Off by default. This opens every .iso on the console and reads a few "
    "blocks out of each one to find out which game is really in it, which is "
    "the only way to catch an image with the wrong name. On a console with a "
    "couple of dozen images it adds a minute or more to the collection. The "
    "names, sizes and regions above are collected either way.")

# The words shown against each category. The domain's own status names are
# ok/partial/failed/skipped, which mean nothing to the audience.
STATUS_TEXT = {
    "waiting": "waiting",
    "running": "collecting",
    "ok": "collected",
    "partial": "partly collected",
    "failed": "not collected",
    "skipped": "skipped",
    "absent": "not in this file",
    "not_asked": "not asked for",
}

STATUS_TOKEN = {
    "collecting": "info",
    "collected": "ok",
    "partly collected": "warn",
    "not collected": "error",
}

CANNOT_REACH = (
    "Could not reach the PS3 at {host}. Check that it is switched on, that it "
    "is showing the main menu rather than running a game, that webMAN is "
    "installed and running, and that it is plugged into the same router as "
    "this PC. If the console has a different address now, type it into the bar "
    "at the top of the window."
)

NOTHING_YET = ("Nothing collected yet. Press Collect diagnostics, or open a "
               "diagnostic somebody has sent you.")

NOTHING_STOOD_OUT = ("Nothing stood out. None of the faults this tool knows "
                     "how to spot are present. That is not the same as "
                     "proving the console is healthy.")


def show_in_folder(path):
    """Opens the containing folder with the file selected where the platform
    supports it. Never raises: this is a convenience, not the deliverable."""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", folder])
    except OSError:
        try:
            webbrowser.open(f"file://{folder}")
        except Exception:
            pass


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


class FindingCard(QFrame):
    """One finding. Title, explanation and fix all visible at once.

    The severity is carried by a coloured stripe down the left edge rather than
    by the wording, so the three groups are told apart at a glance without
    anybody having to know that "warn" is less serious than "error".
    """

    def __init__(self, finding, colours, parent=None):
        super().__init__(parent)
        self.finding = finding
        self.setObjectName("findingCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"#findingCard {{"
            f" background: {colours['surface']};"
            f" border-left: 3px solid {colours['severity']};"
            f" border-top-right-radius: 4px;"
            f" border-bottom-right-radius: 4px; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        heading = QHBoxLayout()
        heading.setSpacing(8)
        self.title_label = QLabel(finding.title)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(
            f"font-weight: 600; color: {colours['text']};")
        heading.addWidget(self.title_label, 1)
        if finding.category:
            badge = QLabel(finding.category)
            badge.setStyleSheet(f"color: {colours['dim']}; font-size: 11px;")
            badge.setAlignment(Qt.AlignRight | Qt.AlignTop)
            heading.addWidget(badge, 0)
        layout.addLayout(heading)

        self.explanation_label = QLabel(finding.explanation)
        self.explanation_label.setWordWrap(True)
        self.explanation_label.setStyleSheet(f"color: {colours['text']};")
        self.explanation_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        layout.addWidget(self.explanation_label)

        self.fix_label = None
        if finding.fix:
            self.fix_label = QLabel(f"What to do: {finding.fix}")
            self.fix_label.setWordWrap(True)
            self.fix_label.setStyleSheet(
                f"color: {colours['accent']}; font-weight: 500;")
            self.fix_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(self.fix_label)

        self.evidence_button = None
        self.evidence_label = None
        if finding.evidence:
            self.evidence_button = QToolButton()
            self.evidence_button.setText(
                f"Show evidence ({len(finding.evidence)})")
            self.evidence_button.setCheckable(True)
            self.evidence_button.setAutoRaise(True)
            self.evidence_button.setStyleSheet(
                f"QToolButton {{ color: {colours['dim']}; border: none;"
                f" padding: 2px 0px; }}")
            row = QHBoxLayout()
            row.setContentsMargins(0, 2, 0, 0)
            row.addWidget(self.evidence_button)
            row.addStretch(1)
            layout.addLayout(row)

            self.evidence_label = QLabel("\n".join(finding.evidence))
            self.evidence_label.setWordWrap(True)
            self.evidence_label.setTextInteractionFlags(
                Qt.TextSelectableByMouse)
            self.evidence_label.setStyleSheet(
                f"color: {colours['dim']}; font-family: monospace;"
                f" font-size: 11px;")
            self.evidence_label.setVisible(False)
            layout.addWidget(self.evidence_label)
            self.evidence_button.toggled.connect(self._toggle_evidence)

    def _toggle_evidence(self, shown):
        self.evidence_label.setVisible(shown)
        self.evidence_button.setText(
            "Hide evidence" if shown
            else f"Show evidence ({len(self.finding.evidence)})")


@register
class DiagnosticsScreen(Screen):
    key = "diagnostics"
    title = "Diagnostics"
    blurb = ("Reads the console and puts everything a helper needs into one "
             "file you can send them.")
    tile = "DX"
    order = 10

    #: Replaced by the tests so a whole run can be driven against the mock
    #: console on loopback. Returns (http, ftp) for a host, or None to let the
    #: runner build its own. The same seam runner.run itself offers, for the
    #: same reason: nothing in the test suite may touch a real console.
    transport_factory = None

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        self._run_task = None
        self._tinted = []
        self.category_titles = {key: title
                                for key, title, _collect in CATEGORIES}
        self.last_zip = None
        self.artefacts = None
        self.analysis = None
        self.severity_filters = {}
        self._build()
        self._restore()
        self.connection.changed.connect(self._connection_changed)
        self.theme.changed.connect(self._apply_theme)
        self._connection_changed()
        self._apply_theme()
        self._render_findings(None)

    # --- construction ------------------------------------------------------

    def _tint(self, widget, token, extra=""):
        """Colour from a token, remembered so a theme change can repaint it.

        The token is kept on the widget rather than in the list, because
        several of these change token as the run goes on and a theme switch
        halfway through must repaint them in the colour they are now, not the
        one they were built with.
        """
        widget.setProperty("colourExtra", extra)
        self._tinted.append(widget)
        self._paint(widget, token)
        return widget

    def _paint(self, widget, token):
        widget.setProperty("colourToken", token)
        extra = widget.property("colourExtra") or ""
        widget.setStyleSheet(f"color: {self.theme.colour(token)}; {extra}")

    @staticmethod
    def _group(title):
        """A group box whose layout is not pressed against its own border.

        Qt reserves only the title's height at the top of a group box, so a
        default four pixel top margin puts the first control into the frame
        line. These numbers are the ones the screen is designed at; every box
        uses them so the columns down the screen line up.
        """
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        return box, layout

    def _build(self):
        # Everything sits in a scroller. Four group boxes and a findings list
        # do not fit in the shell's smallest window at any spacing worth
        # having, and the shell places screens directly with nothing to scroll
        # them, so without this the choice is a cramped screen or a clipped
        # one. Scrolled, the layout can be sized for reading.
        frame = QVBoxLayout(self)
        frame.setContentsMargins(0, 0, 0, 0)
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setFrameShape(QFrame.NoFrame)
        self.scroller.viewport().setAutoFillBackground(False)
        body = QWidget()
        body.setAutoFillBackground(False)
        self.scroller.setWidget(body)
        frame.addWidget(self.scroller)

        outer = QVBoxLayout(body)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(14)

        self.notice_label = QLabel(READ_ONLY_NOTICE)
        self.notice_label.setWordWrap(True)
        self._tint(self.notice_label, "ok", "font-weight: 600;")
        outer.addWidget(self.notice_label)

        blurb = QLabel("It gathers what a helper needs to work out what is "
                       "wrong and puts it in one file for you to send them.")
        blurb.setWordWrap(True)
        self._tint(blurb, "text_dim")
        outer.addWidget(blurb)

        outer.addWidget(self._build_console())
        outer.addWidget(self._build_categories())
        outer.addLayout(self._build_actions())
        outer.addWidget(self._build_progress())
        outer.addWidget(self._build_results(), 1)

    def _build_console(self):
        """Which console this will read, and nothing about finding one.

        There is deliberately no search control and no scan line here. Both
        used to live on this screen, and the scan line beside them is what
        produced "No PS3 found on this network" in red underneath a populated
        address during a working collection. The address and the search belong
        to the top bar; this box only says which address is in force.
        """
        box, layout = self._group("Your PS3")

        self.address_label = QLabel()
        self.address_label.setWordWrap(True)
        self._tint(self.address_label, "text")
        layout.addWidget(self.address_label)

        hint = QLabel("On the console the address is under Settings, Network "
                      "Settings, Settings and Connection Status List, or at "
                      "the top of the webMAN page.")
        hint.setWordWrap(True)
        self._tint(hint, "text_dim")
        layout.addWidget(hint)
        return box

    def _build_categories(self):
        box, layout = self._group("What to collect")

        grid = QGridLayout()
        # The three columns are given equal stretch and a wide gutter so the
        # longest label cannot grow into its neighbour: without the stretch the
        # columns size to their own contents and "webMAN configuration" ends up
        # touching the tick box to its right.
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(10)
        grid.setContentsMargins(0, 0, 0, 2)
        self.category_boxes = {}
        for column in range(COLUMNS):
            grid.setColumnStretch(column, 1)
        for index, (key, title, _collect) in enumerate(CATEGORIES):
            tick = QCheckBox(title)
            tick.setChecked(True)
            # Room for the descenders in the label and for the tick indicator
            # beside it at the smallest window the shell allows.
            tick.setMinimumHeight(22)
            tick.setMinimumWidth(tick.sizeHint().width())
            tick.toggled.connect(self._remember)
            self.category_boxes[key] = tick
            grid.addWidget(tick, index // COLUMNS, index % COLUMNS)
        layout.addLayout(grid)

        layout.addLayout(self._build_identify_option())

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setFrameShadow(QFrame.Plain)
        layout.addWidget(rule)

        self.identifiers_box = QCheckBox("Include console identifiers")
        self.identifiers_box.setMinimumHeight(22)
        self.identifiers_box.setChecked(False)
        self.identifiers_box.toggled.connect(self._remember)
        layout.addWidget(self.identifiers_box)
        warning = QLabel("Leave this off unless you have been asked for it. "
                         "These values uniquely identify your console and "
                         "should not be posted publicly.")
        warning.setWordWrap(True)
        self._tint(warning, "warn")
        layout.addWidget(warning)
        return box

    def _build_identify_option(self):
        """The one sub-option: reading inside the disc images.

        Indented under the category grid rather than given a box of its own,
        because it is not an eighth category. The runner is handed the ticked
        categories; this is a thing the game inventory can additionally be
        asked to do, and it costs minutes, so it is off until it is asked for.
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addSpacing(SUB_INDENT)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        self.identify_box = QCheckBox(IDENTIFY_LABEL)
        self.identify_box.setChecked(False)
        self.identify_box.setMinimumHeight(22)
        self.identify_box.setMinimumWidth(self.identify_box.sizeHint().width())
        self.identify_box.toggled.connect(self._remember)
        column.addWidget(self.identify_box)

        self.identify_hint = QLabel(IDENTIFY_HINT)
        self.identify_hint.setWordWrap(True)
        self._tint(self.identify_hint, "text_dim")
        column.addWidget(self.identify_hint)
        row.addLayout(column, 1)

        # Nothing to read inside if the inventory is not being collected.
        games = self.category_boxes["games"]
        games.toggled.connect(self._identify_enabled)
        self._identify_enabled(games.isChecked())
        return row

    def _identify_enabled(self, enabled):
        self.identify_box.setEnabled(bool(enabled))
        self.identify_hint.setEnabled(bool(enabled))

    def _build_actions(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        self.run_button = QPushButton("Collect diagnostics")
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self._start)
        row.addWidget(self.run_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        row.addWidget(self.stop_button)
        self.open_button = QPushButton("Open a saved diagnostic")
        self.open_button.clicked.connect(self._open_saved)
        row.addWidget(self.open_button)
        row.addStretch(1)
        self.show_button = QPushButton("Show in folder")
        self.show_button.setEnabled(False)
        self.show_button.clicked.connect(self._show_last)
        row.addWidget(self.show_button)
        return row

    def _refresh_count(self):
        total = self.overall_progress.maximum()
        done = self.overall_progress.value()
        self.overall_count.setText(f"{done} of {total} collected"
                                   if total else "")
        self.overall_count.setVisible(bool(total))

    def _build_progress(self):
        box, layout = self._group("Progress")

        # Determinate, and counted in categories. An indeterminate bar tells a
        # worried user nothing except that something is still happening, which
        # during a five minute collection is the one thing they already know.
        # The shell styles a progress bar as a three pixel hairline, which is
        # the right look and leaves nowhere to draw text: painting the count on
        # the bar itself clipped it against the group border. The count goes in
        # a label of its own instead.
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, len(CATEGORIES))
        self.overall_progress.setValue(0)
        self.overall_progress.setTextVisible(False)
        self.overall_count = QLabel("")
        self._tint(self.overall_count, "text_dim")
        self.overall_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # Hidden until there is a count to show, so an idle screen does not
        # reserve a line of blank space above the categories and squeeze them.
        self.overall_count.hide()
        layout.addWidget(self.overall_count)
        layout.addWidget(self.overall_progress)

        # What is happening this second, in the largest text on the screen.
        # The per-category list underneath cannot carry this: a category is one
        # row and the inventory sat on "collecting" for two minutes, which told
        # a worried user nothing at all. This line and the log under it are the
        # answer to "is it stuck?", so they are sized to be read at a glance.
        self.activity_label = QLabel(IDLE_ACTIVITY)
        self.activity_label.setWordWrap(True)
        self.activity_label.setMinimumHeight(ACTIVITY_POINT_SIZE * 2)
        self._tint(self.activity_label, "text_dim",
                   f"font-size: {ACTIVITY_POINT_SIZE}px; font-weight: 600;")
        layout.addWidget(self.activity_label)

        # A running list rather than a single line, so the eye can see the run
        # moving through folder after folder even when one file is slow.
        self.detail_log = QPlainTextEdit()
        self.detail_log.setReadOnly(True)
        self.detail_log.setMaximumBlockCount(LOG_LINES)
        self.detail_log.setMinimumHeight(LOG_HEIGHT)
        self.detail_log.setMaximumHeight(LOG_HEIGHT + 40)
        self.detail_log.setFrameShape(QFrame.NoFrame)
        self.detail_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.detail_log.setPlaceholderText(
            "Everything this reads off the console is listed here as it "
            "happens.")
        layout.addWidget(self.detail_log)
        self._paint_log()

        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        # A status can wrap to two lines when a collector reports an error, so
        # the rows are spaced rather than packed: at four pixels the descenders
        # of one row met the ascenders of the next.
        grid.setVerticalSpacing(8)
        grid.setContentsMargins(0, 2, 0, 0)
        # The names column is held at its widest label so the statuses start on
        # one line, and the statuses take every pixel that is left.
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        self.progress_rows = {}
        for index, (key, title, _collect) in enumerate(CATEGORIES):
            name = QLabel(title)
            state = QLabel(STATUS_TEXT["waiting"])
            state.setWordWrap(True)
            self._tint(state, "text_dim")
            grid.addWidget(name, index, 0, Qt.AlignTop)
            grid.addWidget(state, index, 1)
            grid.setRowMinimumHeight(index, ROW_HEIGHT)
            self.progress_rows[key] = state
        layout.addLayout(grid)
        return box

    def _build_results(self):
        box, layout = self._group("Results")

        self.summary_label = QLabel(NOTHING_YET)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._tint(self.summary_label, "text_dim")
        layout.addWidget(self.summary_label)

        filters = QHBoxLayout()
        filters.setSpacing(16)
        self.filter_hint = QLabel("Show:")
        self._tint(self.filter_hint, "text_dim")
        filters.addWidget(self.filter_hint)
        for severity in SEVERITIES:
            tick = QCheckBox(SEVERITY_LABELS[severity])
            tick.setMinimumHeight(22)
            tick.setChecked(True)
            tick.toggled.connect(lambda _checked: self._render_findings(
                self.analysis))
            self.severity_filters[severity] = tick
            filters.addWidget(tick)
        filters.addStretch(1)
        layout.addLayout(filters)

        # The findings are not given a scroller of their own. The whole screen
        # scrolls, and a second scroller nested inside it traps the cards in a
        # short window that the page scroll cannot reach past.
        self.findings_host = QWidget()
        self.findings_layout = QVBoxLayout(self.findings_host)
        self.findings_layout.setContentsMargins(0, 0, 0, 0)
        self.findings_layout.setSpacing(8)
        self.findings_layout.setAlignment(Qt.AlignTop)
        self.findings_host.setSizePolicy(QSizePolicy.Expanding,
                                         QSizePolicy.Minimum)
        layout.addWidget(self.findings_host, 1)
        return box

    # --- theme -------------------------------------------------------------

    def _apply_theme(self):
        for widget in self._tinted:
            self._paint(widget, widget.property("colourToken"))
        self._paint_log()
        self._render_findings(self.analysis)

    def _paint_log(self):
        """The log is repainted whole rather than tinted.

        _tint only changes the foreground, and this widget needs a surface and
        a border as well, so a theme switch would otherwise leave dark text on
        a dark panel.
        """
        self.detail_log.setStyleSheet(
            f"QPlainTextEdit {{"
            f" background: {self.theme.colour('surface_alt')};"
            f" color: {self.theme.colour('text')};"
            f" border: 1px solid {self.theme.colour('border')};"
            f" border-radius: 4px;"
            f" font-family: monospace; font-size: 12px;"
            f" padding: 6px; }}")

    def _card_colours(self, severity):
        return {
            "severity": self.theme.colour(severity),
            "surface": self.theme.colour("surface_alt"),
            "text": self.theme.colour("text"),
            "dim": self.theme.colour("text_dim"),
            "accent": self.theme.colour("accent"),
        }

    # --- settings ----------------------------------------------------------

    def _restore(self):
        settings = self.services.settings
        self.identifiers_box.setChecked(
            bool(settings.get("include_identifiers")))
        self.identify_box.setChecked(bool(settings.get("identify_isos")))
        stored = settings.get("categories") or {}
        for key, tick in self.category_boxes.items():
            if key in stored:
                tick.setChecked(bool(stored[key]))
        self._identify_enabled(self.category_boxes["games"].isChecked())

    def _remember(self, *_args):
        settings = self.services.settings
        settings["include_identifiers"] = self.identifiers_box.isChecked()
        settings["identify_isos"] = self.identify_box.isChecked()
        settings["categories"] = {key: tick.isChecked()
                                  for key, tick in self.category_boxes.items()}

    # --- lifecycle ---------------------------------------------------------

    def on_enter(self):
        # Nothing is started here on purpose. Opening a screen is not consent
        # to put a packet on the network, and this screen has nothing it could
        # usefully ask a console anyway until the user presses Collect.
        self._connection_changed()

    def can_leave(self):
        if self._run_task is not None:
            self.status_message.emit(
                "A collection is running. Press Stop first, or wait for it to "
                "finish writing the file.")
            return False
        return True

    def _set_busy(self, busy):
        self.busy_changed.emit(busy)

    # --- connection --------------------------------------------------------

    def _connection_changed(self):
        self._refresh_address()

    def _refresh_address(self):
        """Repeats the address the top bar holds, and says nothing else.

        In particular it never reports whether a search found anything: a
        search that came back empty is not evidence about the address sitting
        in the box, and rendering the two together is the bug this screen was
        rebuilt to remove.
        """
        host = self.connection.host
        if host:
            self.address_label.setText(f"Console address: {host}")
        else:
            self.address_label.setText(
                "No address yet. Type the console's address into the bar at "
                "the top of the window.")

    # --- collection --------------------------------------------------------

    def _refuse(self, message):
        self.summary_label.setText(message)
        self._paint(self.summary_label, "warn")
        self.status_message.emit(message)

    def _start(self):
        if self._run_task is not None:
            return
        host = self.connection.host
        if not host:
            self._refuse("Type the PS3's address into the bar at the top of "
                         "the window first.")
            return
        wanted = [key for key, tick in self.category_boxes.items()
                  if tick.isChecked()]
        if not wanted:
            self._refuse("Tick at least one thing to collect.")
            return
        self._remember()

        self.run_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.show_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.overall_progress.setRange(0, len(wanted))
        self.overall_progress.setValue(0)
        self._refresh_count()
        for key, label in self.progress_rows.items():
            self._set_status(label, STATUS_TEXT["waiting"] if key in wanted
                             else STATUS_TEXT["not_asked"])
        self._render_findings(None)
        self.detail_log.clear()
        self._activity(f"Starting. Reading {host}.", "info")
        self._paint(self.summary_label, "text_dim")
        self.summary_label.setText(
            f"Collecting from {host}. A slow console is normal; this can take "
            f"a few minutes.")
        self.connection.set_connection("checking", f"Reading {host}.")
        self.status_message.emit(f"Collecting from {host}.")
        self._set_busy(True)

        settings = self.services.settings
        destination = settings.get("output_dir") or config.desktop_dir()
        include = self.identifiers_box.isChecked()
        # Only meaningful with the inventory, and the runner would otherwise be
        # handed an option for a category it was not asked to collect.
        options = {"identify_isos": (self.identify_box.isChecked()
                                     and "games" in wanted)}
        http_timeout = settings.get("http_timeout")
        ftp_timeout = settings.get("ftp_timeout")
        run_timeout = settings.get("run_timeout") or 900.0
        factory = self.transport_factory

        def work(control):
            name = report.timestamp_name()
            log = None
            try:
                log = RunLog(os.path.join(destination, f"{name}.log.jsonl"))
                http, ftp = factory(host) if factory else (None, None)
                result = runner.run(
                    host, wanted, include_identifiers=include,
                    http_timeout=http_timeout, ftp_timeout=ftp_timeout,
                    run_timeout=run_timeout, log=log, http=http, ftp=ftp,
                    options=options,
                    on_progress=lambda key, title, state, payload:
                        control.progress(
                            ("detail", key, payload) if state == "detail"
                            else ("category", key, state, payload)),
                    should_stop=lambda: control.cancelled)
                control.progress(("phase", "Working out what it means."))
                artefacts = ArtefactSet.from_run(result)
                analysis = analyse(artefacts)
                path, counts = report.write_zip(
                    artefacts, destination, analysis.findings,
                    analysis.broken_rules, name=name)
                if log:
                    log.event("finished", zip=os.path.basename(path),
                              findings=len(analysis.findings),
                              redactions=sum(counts.values()))
                # Delivered through progress rather than as the return value.
                # A cancelled Task does not emit finished, and a run somebody
                # pressed Stop on has still written a zip that is worth having.
                control.progress(("result", path, artefacts, analysis, result))
            finally:
                if log:
                    log.close()

        task = self.submit(work)
        self._run_task = task
        task.progress.connect(self._run_progress)
        task.failed.connect(self._run_failed)
        task.done.connect(self._run_done)

    def _stop(self):
        if self._run_task is None:
            return
        self._run_task.cancel()
        self.stop_button.setEnabled(False)
        self._activity("Stopping.", "warn")
        self._log_line("Stop pressed.")
        self._paint(self.summary_label, "text_dim")
        self.summary_label.setText(
            "Stopping. Whatever has been collected so far will still be "
            "saved.")

    # --- the running commentary -------------------------------------------

    def _activity(self, text, token="info"):
        self.activity_label.setText(text)
        self._paint(self.activity_label, token)

    def _log_line(self, text):
        self.detail_log.appendPlainText(text)
        # Pinned to the newest line. A log that has to be scrolled by hand to
        # see what is happening now is a log nobody reads.
        bar = self.detail_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _run_progress(self, message):
        kind = message[0]
        if kind == "category":
            _kind, key, state, outcome = message
            title = self.category_titles.get(key, key)
            label = self.progress_rows.get(key)
            if label is None:
                return
            if state == "running":
                self._set_status(label, STATUS_TEXT["running"])
                self._activity(f"{title}: starting.")
                self._log_line(title)
                return
            text = STATUS_TEXT.get(outcome.status, outcome.status)
            if outcome.status in ("failed", "partial") and outcome.error:
                text += f" - {outcome.error}"
            self._set_status(label, text)
            self._log_line(f"{title} - {text}")
            self.overall_progress.setValue(self.overall_progress.value() + 1)
            self._refresh_count()
        elif kind == "detail":
            _kind, key, text = message
            title = self.category_titles.get(key, key)
            self._activity(f"{title}: {text}")
            self._log_line(f"    {text}")
        elif kind == "phase":
            self._activity(message[1])
            self._log_line(message[1])
            self.status_message.emit(message[1])
        elif kind == "result":
            self._finished(*message[1:])

    def _set_status(self, label, text):
        # The token is chosen from the leading words so that "not collected -
        # the console refused the connection" still reads as a failure.
        token = "text_dim"
        for prefix, candidate in STATUS_TOKEN.items():
            if text.startswith(prefix):
                token = candidate
                break
        label.setText(text)
        self._paint(label, token)

    def _finished(self, path, artefacts, analysis, result):
        self.last_zip = path
        self.artefacts = artefacts
        counts = result.counts()
        failed = counts.get("failed", 0)
        stopped = getattr(result, "stopped", False)
        self._activity("Stopped. What was collected has been saved."
                       if stopped else "Finished.",
                       "warn" if stopped else "ok")
        if failed and failed == len(result.results):
            self.connection.set_connection(
                "unreachable", CANNOT_REACH.format(host=result.host))
            self._paint(self.summary_label, "error")
            self.summary_label.setText(
                CANNOT_REACH.format(host=result.host)
                + f"\n\nA file was still saved: {path}")
        else:
            self.connection.set_connection(
                "connected", f"Read {len(result.results) - failed} of "
                             f"{len(result.results)} categories from "
                             f"{result.host}.")
            summary = f"Saved to {path}"
            if stopped:
                summary = ("You stopped the collection. Everything collected "
                           "up to that point was still saved.\n" + summary)
            if failed:
                summary += (f"\n{failed} of {len(result.results)} categories "
                            f"could not be collected. The rest are in the "
                            f"file, and what went wrong is written in it.")
            summary += "\n\nSend that one file to whoever is helping you."
            self._paint(self.summary_label, "ok")
            self.summary_label.setText(summary)
        self.show_button.setEnabled(True)
        self.status_message.emit(f"Saved {os.path.basename(path)}.")
        self._render_findings(analysis)

    def _run_failed(self, message):
        self._activity("Stopped by an error.", "error")
        self._log_line(message)
        self._paint(self.summary_label, "error")
        self.summary_label.setText(
            f"{APP_NAME} hit a problem of its own and stopped: {message}")
        self.status_message.emit("The collection stopped with an error.")

    def _run_done(self):
        self._run_task = None
        self.run_button.setEnabled(True)
        self.open_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_busy(False)

    # --- opening a saved diagnostic ---------------------------------------

    def _choose_file(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open a saved diagnostic", config.desktop_dir(),
            "ps3-diag zip (*.zip);;All files (*)")
        return path

    def _open_saved(self):
        path = self._choose_file()
        if not path:
            return
        self._load_zip(path)

    def _load_zip(self, path):
        """Re-runs today's rules against a zip, with no console anywhere near.

        The point of the artefact set being the boundary is exactly this: a set
        loaded from a file somebody emailed is the same object a live run
        produces, so every rule works on it without knowing which it has.
        """
        self.open_button.setEnabled(False)
        self.status_message.emit(f"Opening {os.path.basename(path)}.")
        self._set_busy(True)

        def work(control):
            artefacts = ArtefactSet.from_zip(path)
            return artefacts, analyse(artefacts)

        task = self.submit(work)
        task.finished.connect(lambda payload: self._loaded(path, *payload))
        task.failed.connect(self._load_failed)
        task.done.connect(self._load_done)

    def _loaded(self, path, artefacts, analysis):
        self.artefacts = artefacts
        self.last_zip = path
        self.show_button.setEnabled(True)
        # Nothing was collected, so the commentary says what it is showing
        # instead of leaving the last run's last line standing.
        self._activity(f"Opened {os.path.basename(path)}.", "text_dim")
        # The progress column is repurposed to say what is in the file, which
        # is the same question in a different tense.
        self.overall_progress.setRange(0, len(CATEGORIES))
        self.overall_progress.setValue(
            len([key for key in self.progress_rows
                 if artefacts.collected(key)]))
        self._refresh_count()
        for key, label in self.progress_rows.items():
            status = artefacts.status(key)
            self._set_status(label, STATUS_TEXT.get(status, status))
        when = artefacts.generated_local.replace("T", " ")
        self._paint(self.summary_label, "text_dim")
        self.summary_label.setText(
            f"Opened {os.path.basename(path)}, collected from "
            f"{artefacts.host or 'an unknown console'} on "
            f"{when or 'an unknown date'}. The checks below are the ones this "
            f"version knows about, run against that file now.")
        if not artefacts.categories:
            self.summary_label.setText(
                self.summary_label.text()
                + "\n\nThere is no manifest.json in that zip, so it may "
                  "not be one of ours. It has been loaded anyway, but there "
                  "may be very little to show.")
        self._render_findings(analysis)

    def _load_failed(self, message):
        self._paint(self.summary_label, "error")
        self.summary_label.setText(
            f"That file could not be opened as a diagnostic. {message}")
        self.status_message.emit("That file could not be opened.")

    def _load_done(self):
        self.open_button.setEnabled(True)
        self._set_busy(False)

    def _show_last(self):
        if self.last_zip and os.path.exists(self.last_zip):
            show_in_folder(self.last_zip)
        else:
            self.status_message.emit("Nothing has been saved yet.")

    # --- findings ----------------------------------------------------------

    def _render_findings(self, analysis):
        # The analysis currently on show is kept here rather than only at the
        # call sites, because the severity filters re-render from it and a
        # filter must not be able to lose the findings it is filtering.
        self.analysis = analysis
        _clear(self.findings_layout)
        findings = list(analysis.findings) if analysis else []
        counts = {severity: len([item for item in findings
                                 if item.severity == severity])
                  for severity in SEVERITIES}
        for severity, tick in self.severity_filters.items():
            tick.setText(f"{SEVERITY_LABELS[severity]} ({counts[severity]})")
            tick.setEnabled(bool(counts[severity]))

        if analysis is None:
            return

        shown = 0
        for severity in SEVERITIES:
            group = [item for item in findings if item.severity == severity]
            if not group or not self.severity_filters[severity].isChecked():
                continue
            header = QLabel(f"{SEVERITY_LABELS[severity].upper()} "
                            f"({len(group)})")
            header.setStyleSheet(
                f"color: {self.theme.colour(severity)}; font-weight: 700;"
                f" font-size: 11px; letter-spacing: 1px;")
            self.findings_layout.addWidget(header)
            for item in group:
                self.findings_layout.addWidget(
                    FindingCard(item, self._card_colours(severity)))
                shown += 1

        if not findings:
            self.findings_layout.addWidget(
                self._note(NOTHING_STOOD_OUT, "text"))
        elif not shown:
            self.findings_layout.addWidget(self._note(
                "Everything found is hidden by the filters above. Tick a box "
                "to see it.", "text_dim"))
        # Said whether or not anything was found: "nothing stood out" means
        # something different when three of the checks did not run.
        if analysis.broken_rules:
            self.findings_layout.addWidget(self._note(
                f"{len(analysis.broken_rules)} check(s) could not run because "
                f"of a fault in this tool itself. The details are in the zip.",
                "text_dim"))

    def _note(self, text, token):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {self.theme.colour(token)};")
        return label
