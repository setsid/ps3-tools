"""The game updates screen.

It lists every game installed on the console, asks Sony what the newest title
update for each one is, and offers to fetch and install the ones that are
behind. Nothing is ticked when the list appears: this screen downloads several
hundred megabytes and writes it to somebody's console, and a screen that
arrives with work already selected is a screen that gets one accidental click
away from doing all of it.

Three things about it are not decoration.

The console's packages folder is listed before anything starts, and what is in
it is shown. The install call installs the whole folder rather than a named
file, so anything already sitting there gets installed alongside the update.
This screen says what it found and makes the user agree to it; it never
installs a stranger's package quietly on their behalf.

Every downloaded file is checked against the sha1 in Sony's list before it goes
anywhere near the console. That check lives in ps3tools.updates.deliver and a
mismatch stops the whole thing. This screen's part is only to report it.

The install call itself has never been fired against a console by anybody. The
screen says so in the words it uses afterwards: the console was asked, and
being asked is all this program can honestly claim.
"""

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFrame,
                               QGraphicsBlurEffect, QHBoxLayout, QLabel,
                               QProgressBar, QPushButton, QSizePolicy,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ps3diag import parsers, transport
from ps3tools import titles, updates
from ps3tools.consoleactions import ConsoleActions
from ps3tools.patching.ftpwrite import FtpWriter
from ps3tools.shell import widgets
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

COLUMNS = ["Game", "Title ID", "Installed", "Newest", "Size", ""]

#: The header over the summary block while there is nothing to count.
DETAIL_SUMMARY = "What this scan found"


def _detail_summary(counts):
    """One line saying what is inside the block, so it can be left closed.

    Numbers rather than a word like "details": the question somebody has is
    whether anything in there needs them, and 2 unread answers it.
    """
    if not counts or not counts.get("opened"):
        return DETAIL_SUMMARY
    opened = counts.get("opened", 0)
    parts = [f"{opened} disc image{'' if opened == 1 else 's'} checked"]
    for key, word in (("added", "added"), ("unreadable", "with no game in"),
                      ("unread", "unread")):
        if counts.get(key):
            parts.append(f"{counts[key]} {word}")
    return ", ".join(parts)


NO_HOST = (
    "Type the console's address into the box at the top of this window, or "
    "press Find my PS3 next to it.")

NOTHING_TO_DO = (
    "Every game on this console already has the newest title update Sony "
    "published for it, so there is nothing to do here.")

#: The one scan button, and the tick box that decides which check it runs.
#: The button always says which of the two it would do; see _scan_label.
FULL_SCAN_LABEL = "Check everything"
FULL_SCAN_OPTION = "Include every game"

#: Said on arrival, in place of scanning unasked.
READY_TO_SCAN = (
    "Press the button below. This reads every game on the console and then "
    "asks Sony about each one.\n\n"
    "On a console with a lot of games it takes a few minutes, and it says "
    "what it is doing as it goes. Nothing is downloaded or changed until you "
    "tick something afterwards.")

WHAT_THIS_IS = (
    "Games get fixes after they are released. This checks each game on the "
    "console against Sony's own list and offers the ones that are behind. "
    "Tick the ones you want and press the button underneath.")

#: Why this finishes sooner than doing the same job on the console. It is
#: shown at the top, where it is read before anything has been pressed.
ONLY_THE_NEWEST = (
    "Only the newest title update for each game is fetched. The console "
    "works through every update Sony ever published for a game, one after "
    "another, which is why updating there takes longer than this does.")


class InstallWizard(QDialog):
    """What is about to happen, one step at a time, before anything happens.

    Two things go wrong at this point and both are avoidable. The console has
    to be sat on its own menu rather than inside a game, and somebody who has
    pressed the button expects it to be over in seconds when it is minutes of
    downloading. Saying both once, in front of the thing they are about to
    start, costs a press and saves a failed run.

    It asks and returns. Nothing here touches a console or a network.
    """

    XMB_STEP = (
        "Go to the console first and leave it on the main menu, out of any "
        "game. An update cannot be installed while the game it belongs to is "
        "running, and the console will refuse it.")

    PACE_STEP = (
        "Each update is downloaded from Sony, checked against Sony's own "
        "checksum, and copied across. A download that does not match is "
        "deleted and nothing is copied.\n\n"
        "They are then installed one at a time. The console shows each "
        "install on the television and waits for you to press O, and this "
        "window asks before it sends the next one.")

    def __init__(self, chosen, theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._chosen = list(chosen)
        self._step = 0
        self.setObjectName("firstRun")
        self.setWindowTitle("Download and install updates")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self.body = QFrame(self)
        self.body.setObjectName("firstRunBody")
        self.body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shell.addWidget(self.body)

        column = QVBoxLayout(self.body)
        column.setContentsMargins(28, 22, 28, 24)
        column.setSpacing(14)

        self.heading = QLabel("", self.body)
        font = QFont(self.heading.font())
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        self.heading.setFont(font)
        self.heading.setWordWrap(True)
        column.addWidget(self.heading)

        self.body_text = QLabel("", self.body)
        self.body_text.setWordWrap(True)
        self.body_text.setMinimumWidth(420)
        column.addWidget(self.body_text)

        buttons = QHBoxLayout()
        self.back_button = QPushButton("Back", self.body)
        self.back_button.clicked.connect(self._back)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel", self.body)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.next_button = QPushButton("Next", self.body)
        widgets.set_role(self.next_button, widgets.PRIMARY)
        self.next_button.clicked.connect(self._forward)
        buttons.addWidget(self.next_button)
        column.addLayout(buttons)

        self._paint()

    @property
    def steps(self):
        total = sum(row.package.size for row in self._chosen if row.package)
        count = len(self._chosen)
        return [
            ("Go to the console's main menu", self.XMB_STEP),
            (f"{count} update{'' if count == 1 else 's'}, "
             f"{parsers.human_size(total)}", self.PACE_STEP),
        ]

    def _paint(self):
        heading, body = self.steps[self._step]
        self.heading.setText(heading)
        self.body_text.setText(body)
        self.back_button.setEnabled(self._step > 0)
        last = self._step == len(self.steps) - 1
        self.next_button.setText("Start" if last else "Next")

    def _back(self):
        if self._step > 0:
            self._step -= 1
            self._paint()

    def _forward(self):
        if self._step < len(self.steps) - 1:
            self._step += 1
            self._paint()
        else:
            self.accept()


@register
class GameUpdatesScreen(Screen):
    """Check installed games against Sony's published title updates."""

    key = "updates"
    title = "Game updates"
    blurb = "Checks each game on the console for a newer version from Sony."
    tile = "GU"
    # After the two patch cards and well before About.
    order = 30

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        self._rows = []
        self._packages = None
        self._task = None
        self._working = False
        self._panel_token = ""
        # A title key the user was sent here for, held until the scan produces
        # rows to apply it to. See preselect().
        self._wanted = ""
        # Disc images whose names carry no title ID. The slow pass is offered
        # for these and only these. See _on_scanned.
        self._unidentified = []
        #: Title IDs the last remembered scan found behind.
        self._behind = []
        # Whether the user asked for the full check. Kept apart from the tick
        # box because the box is ticked and locked whenever there is nothing
        # behind, and that is the screen's doing rather than a choice worth
        # remembering. See _refresh_scan_button.
        self._full_choice = False
        # What has already failed verification, and how, for the length of
        # this screen. It is what lets a second identical failure stop telling
        # the user to try again. See updates.verify_download.
        self._verify_history = {}
        # Seams, so a test does not sit through a real install timeout. The
        # defaults are what runs against a console: None means each package
        # is allowed a wait worked out from its own size.
        self.install_poll_seconds = updates.INSTALL_POLL_SECONDS
        self.install_timeout_seconds = None
        #: One line per package in the run, for the list under the table.
        #: [key, what it is called, what it is doing].
        self._queue_rows = []
        #: What the last image pass counted, for the header over the summary.
        self._image_counts = None
        #: Where the last scan spent its time, measured. See updates.ScanMeter.
        self._timing = ""
        self._build()
        if self.theme is not None:
            try:
                self.theme.changed.connect(self._repaint)
            except (AttributeError, RuntimeError):
                pass

    # -- construction

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        heading = QLabel("Game updates")
        font = heading.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)

        blurb = QLabel(WHAT_THIS_IS)
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        # Above the table and above every button, because it is the reason
        # somebody would do this here rather than on the console.
        self._why_quicker = QLabel(ONLY_THE_NEWEST)
        self._why_quicker.setWordWrap(True)
        layout.addWidget(self._why_quicker)

        # Said once, loudly, above the table. It is the whole answer whenever
        # the table is empty, and an empty grid under a grey sentence reads as
        # the program having failed rather than as an answer.
        self._panel = QFrame()
        self._panel.setObjectName("statepanel")
        panel = QVBoxLayout(self._panel)
        panel.setContentsMargins(16, 14, 16, 14)
        panel.setSpacing(8)
        self._panel_heading = QLabel("")
        self._panel_heading.setWordWrap(True)
        panel_font = self._panel_heading.font()
        panel_font.setPointSize(panel_font.pointSize() + 2)
        panel_font.setBold(True)
        self._panel_heading.setFont(panel_font)
        panel.addWidget(self._panel_heading)
        self._panel_body = QLabel("")
        self._panel_body.setWordWrap(True)
        self._panel_body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        panel.addWidget(self._panel_body)
        self._panel.hide()
        layout.addWidget(self._panel)

        picks = QHBoxLayout()
        picks.setContentsMargins(0, 0, 0, 0)
        self._all_box = QCheckBox("Tick every game that can be updated")
        self._all_box.setEnabled(False)
        self._all_box.toggled.connect(self._on_all_toggled)
        picks.addWidget(self._all_box)
        picks.addStretch(1)
        layout.addLayout(picks)

        self._table = QTreeWidget()
        self._table.setColumnCount(len(COLUMNS))
        self._table.setHeaderLabels(COLUMNS)
        self._table.setRootIsDecorated(False)
        self._table.setUniformRowHeights(True)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._table.itemChanged.connect(self._on_tick)
        layout.addWidget(self._table, 1)

        # Behind an arrow and closed to start with. It runs to a paragraph a
        # game on a full console and pushed everything else off the screen.
        self._detail = widgets.Disclosure(DETAIL_SUMMARY)
        layout.addWidget(self._detail)

        # One line per package, saying which stage it is in. It stays on
        # screen when the run ends, so a package that did not install is
        # still there to be read.
        self._queue = QLabel("")
        self._queue.setWordWrap(True)
        self._queue.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._queue.hide()
        layout.addWidget(self._queue)

        self._stage = QLabel("")
        # Room for four lines. It was cut off at three, mid-sentence.
        widgets.fit_progress_label(self._stage)
        layout.addWidget(self._stage)

        # The shell paints every progress bar as a hairline with nowhere to put
        # text on it, so the byte count goes in a label of its own. The same
        # thing was solved the same way on the patcher and diagnostics screens.
        self._count = QLabel("")
        self._count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._count.hide()
        layout.addWidget(self._count)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.hide()
        layout.addWidget(self._bar)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        layout.addWidget(line)

        buttons = QHBoxLayout()
        self._back = QPushButton("Back")
        self._back.clicked.connect(self._on_back)
        buttons.addWidget(self._back)
        buttons.addStretch(1)
        # One button, with a tick box that decides which check it runs. Two
        # buttons sat here before and both of them started a scan, so which
        # one to press had to be read off a pair of long labels.
        self._full_box = QCheckBox(FULL_SCAN_OPTION)
        self._full_box.toggled.connect(self._on_full_toggled)
        buttons.addWidget(self._full_box)
        self._rescan = QPushButton(FULL_SCAN_LABEL)
        self._rescan.clicked.connect(self._on_scan)
        widgets.set_role(self._rescan, widgets.PRIMARY)
        buttons.addWidget(self._rescan)
        self._refresh_scan_button()
        # Only ever shown when there is something it would do, and it says how
        # many images it would open. It is the slow path and it is the user's
        # choice; see ps3tools.updates.image_identifier.
        self._go = QPushButton("Download and install the ticked updates")
        self._go.setEnabled(False)
        self._go.clicked.connect(self._on_go)
        buttons.addWidget(self._go)
        self._paint_go()
        layout.addLayout(buttons)

    # -- the seams. Every test replaces all five; none of them is reached by
    # -- the suite, which is what keeps a live console and Sony off the wire.

    def _lister(self, host):
        return transport.FtpLister(host)

    def _writer(self, host):
        return FtpWriter(host)

    def _actions(self, host):
        return ConsoleActions(host)

    def _storage(self, host):
        """Free space per device, read off the console's own status page.

        The read-only diagnostic client, and the root page is on its allowlist.
        parse_storage is the storage collector's parser, reused rather than
        copied so there is one answer to how much room there is.
        """
        probe = transport.HttpProbe(host)
        response = probe.get("/")
        if not getattr(response, "ok", False):
            return []
        return parsers.parse_storage(parsers.html_to_text(response.body))

    def _fetcher(self):
        """The manifest fetcher, looked up when it is wanted rather than bound
        at class definition time.

        That is deliberate. Bound as a class attribute, this would keep a
        reference to the real urllib fetcher that no amount of patching
        ps3tools.updates could reach, and the guard test in test_updates.py --
        the one that replaces every real client with something that raises and
        proves this screen never reaches one -- would quietly stop proving it.
        """
        return updates.urllib_fetcher

    def _stream(self):
        """The package stream. Looked up late, for the reason above."""
        return updates.urllib_stream

    # -- arriving from somewhere else

    def preselect(self, subject):
        """Tick this game's rows once the scan has found them.

        The patcher sends people here when their copy of a game is a title
        update the patch was never proved against, so whoever arrives this way
        is not browsing: they have been told their version is wrong and handed
        a button. Ticking the row they came for saves them picking their own
        game out of a list of everything on the console.

        It is remembered rather than applied, because the shell calls this
        before navigating and this screen scans on entry: there are no rows yet
        when it arrives. Nothing is started and nothing is changed -- the
        person who arrives still presses the button themselves.

        A subject this does not recognise is ignored without a word. The shell
        wraps this call, and a screen that threw here would cost the user the
        navigation as well as the selection.
        """
        # Typed loosely on purpose: the caller is the shell, handing over
        # whatever a screen gave it, and this must not be the thing that costs
        # somebody the navigation.
        key = subject.strip().lower() if isinstance(subject, str) else ""
        if key not in titles.TITLES:
            return None
        self._wanted = key
        if self._rows:
            self._apply_preselect()
        return key

    def _apply_preselect(self):
        """Tick the rows belonging to the remembered title, once only."""
        key, self._wanted = self._wanted, ""
        config = titles.TITLES.get(key) or {}
        # Every published title ID of that game, which is the table's business
        # and not this screen's. The console reports an ID; the user knows a
        # game; this is the only thing that joins them.
        wanted = {str(item).upper() for item in config.get("title_ids", ())}
        wanted.update(str(item).upper() for item in (config.get("skus") or {}))
        if not wanted:
            return
        ticked = []
        self._table.blockSignals(True)
        for index in range(self._table.topLevelItemCount()):
            item = self._table.topLevelItem(index)
            if item.text(1).upper() not in wanted:
                continue
            row = self._row_for_item(item)
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(0, Qt.Checked)
                ticked.append(row)
            elif row is not None:
                ticked.append(row)
        self._table.blockSignals(False)
        self._update_go()
        if not ticked:
            return
        name = config.get("short") or config.get("name") or key
        updatable = [row for row in ticked
                     if row is not None and row.updatable]
        if updatable:
            self._detail.setText(
                f"{name} has been ticked for you, because the version "
                f"installed is not the one the fix was proved against. "
                f"Install this update, then go back to the patch and try "
                f"again.\n\n{self._detail.text()}".strip())
        else:
            # Worth saying plainly: they were sent here for an update that
            # this console cannot be offered, and being left looking at a
            # list with nothing ticked would read as the button being broken.
            self._detail.setText(
                f"There is no newer title update for {name} on this console "
                f"to install, so the patch cannot be brought into line that "
                f"way.\n\n{self._detail.text()}".strip())

    # -- lifecycle

    def on_enter(self):
        """Wait to be asked.

        It used to scan the moment the screen opened. On a console with a
        shelf of games that is minutes of reading before anybody has said they
        want it, with a window that looks stuck while it happens.
        """
        if self._rows or self._working:
            return
        if self._offer_remembered():
            return
        self._show_panel("info", "Ready when you are", READY_TO_SCAN)

    def _offer_remembered(self):
        """Show what the last check found, and offer the short way round.

        The table is filled from memory so the screen has something in it, and
        every row is marked as remembered and cannot be ticked: nothing that
        was true a week ago may be acted on today. Either check asks Sony
        fresh; the short one just looks at fewer games.
        """
        host = self.connection.host if self.connection else ""
        entry = updates.remembered_scan(self._settings(), host)
        if entry is None:
            return False
        rows = updates.remembered_rows(entry)
        if not rows:
            return False
        # Kept, not just drawn: the next scan merges into these rather than
        # starting from nothing, which is what stops a title vanishing because
        # one scan did not look where it came from. Every one is blocked, so
        # none of them can be acted on while it is only a memory.
        self._rows = rows
        self._fill_table()
        self._behind = updates.titles_behind(entry)
        when = str(entry.get("checked", "")).replace("T", " at ")
        self._refresh_scan_button()
        if self._behind:
            count = len(self._behind)
            body = (f"This is what the last check found, on {when}. "
                    f"{count} game{'' if count == 1 else 's'} "
                    f"{'was' if count == 1 else 'were'} behind.\n\n"
                    f"Checking those again takes seconds, because the console "
                    f"does not have to be read through from the start. Tick "
                    f"{FULL_SCAN_OPTION} beside the button to look at all of "
                    f"them and pick up anything added since.")
        else:
            body = (f"This is what the last check found, on {when}. "
                    f"Everything was up to date.\n\n{READY_TO_SCAN}")
        self._show_panel("info", "What the last check found", body)
        return True

    def _on_scan(self):
        """The one scan button. The tick box beside it says which check."""
        if self._full_box.isChecked():
            return self.start_scan()
        return self.start_scan(only=list(self._behind))

    def _on_full_toggled(self, _checked):
        # The user's own answer, which is why _refresh_scan_button blocks this
        # signal when it ticks the box on their behalf.
        self._full_choice = self._full_box.isChecked()
        self._rescan.setText(self._scan_label())

    def _scan_label(self):
        """What the button would do if it were pressed now."""
        if self._full_box.isChecked() or not self._behind:
            return FULL_SCAN_LABEL
        count = len(self._behind)
        return (f"Check the {count} that "
                f"{'was' if count == 1 else 'were'} behind")

    def _refresh_scan_button(self):
        """Put the button and its tick box in step with what is known.

        With nothing remembered as behind there is no shorter check to run.
        The box is ticked and disabled in that state, because a box that can
        be cleared and still runs the same check says something untrue about
        the button next to it.
        """
        short = bool(self._behind)
        self._full_box.blockSignals(True)
        self._full_box.setChecked(self._full_choice if short else True)
        self._full_box.setEnabled(short)
        self._full_box.blockSignals(False)
        self._rescan.setText(self._scan_label())

    def _settings(self):
        """The shell's settings dictionary, or nothing if there is not one."""
        return getattr(self.services, "settings", None)

    def on_leave(self):
        if self._task is not None and not self._working:
            self._task.cancel()
            self._task = None

    def can_leave(self):
        return not self._working

    def leave_blocked_reason(self):
        return ("An update is part way through being copied to the console. "
                "Wait for this to finish: stopping now can leave a broken "
                "file in the console's packages folder.")

    # -- the scan

    def start_scan(self, read_images=True, only=None):
        """Look at the console and ask Sony about what is on it.

        Disc images whose names carry no title ID are opened and read as part
        of this. It used to be a second button, because reading them one after
        another took minutes; several connections at a time made it quick
        enough that a separate pass was only ever going to be a way of showing
        somebody an incomplete list first. See
        ps3tools.updates.parallel_image_identifier.

        `only` is a list of title IDs to look at and nothing else. It is the
        short way round after a first full check: the console is not walked and
        no disc image is opened, so it comes back in seconds rather than
        minutes. Everything else about the run is the same, including asking
        Sony fresh -- nothing remembered is ever acted on.
        """
        if self._working:
            return None
        host = self.connection.host if self.connection else ""
        if not host:
            self._table.clear()
            self._rows = []
            self._detail.setText("")
            self._show_panel("warn", "No console address has been entered yet",
                             NO_HOST)
            self._go.setEnabled(False)
            return None

        self._detail.setText("")
        self._hide_panel()
        self._go.setEnabled(False)
        self._rescan.setEnabled(False)
        self._full_box.setEnabled(False)
        self._set_busy(True, "Checking the games that were behind"
                       if only else "Looking at the games on the console")

        open_lister = self._lister
        read_storage = self._storage
        fetcher = self._fetcher()
        read_images = bool(read_images)
        wanted = list(only or [])
        settings = self._settings()

        def work(control):
            stage = lambda text: control.progress(("stage", text))
            if wanted:
                with open_lister(host) as lister:
                    installed = updates.scan_titles(lister, wanted,
                                                    on_stage=stage)
                    folder = updates.inspect_packages_folder(lister)
                control.progress(("scanned", len(installed)))
                rows = updates.check_titles(
                    installed, fetcher=fetcher, progress=control.progress,
                    cancelled=lambda: control.cancelled)
                return rows, [], folder, [], {"looked_at": wanted}
            devices = _device_names(read_storage, host)
            with open_lister(host) as lister:
                # An image already identified is not opened again. The title
                # ID is inside the file and cannot change, and opening one is
                # most of what makes a full scan take minutes.
                meter = updates.ScanMeter()
                identifier = (updates.cached_image_identifier(
                    updates.parallel_image_identifier(
                        lambda: open_lister(host),
                        on_progress=lambda done, total, name:
                            control.progress(("image", done, total, name)),
                        meter=meter),
                    settings)
                    if read_images else None)
                verdict = {}
                installed, notes, unnamed = updates.scan_console(
                    lister, devices=devices, image_identifier=identifier,
                    on_stage=lambda text: control.progress(("stage", text)),
                    verdict=verdict, meter=meter)
                folder = updates.inspect_packages_folder(lister)
            control.progress(("scanned", len(installed)))
            rows = updates.check_titles(
                installed, fetcher=fetcher, progress=control.progress,
                cancelled=lambda: control.cancelled)
            # The image pass counts as looked-in only when it left nothing
            # unidentified behind. A pass that was skipped, or that lost a
            # worker, has not looked and may not remove anything.
            looked = [updates.FROM_GAME_FOLDER]
            # Only a pass that read every image AND got a title ID out of each
            # one may remove a game found in an image before. An image that
            # was read and yielded nothing is not evidence the game is gone,
            # and an earlier positive identification is better evidence than a
            # later blank.
            if read_images and not unnamed and verdict.get(
                    "images_conclusive"):
                looked.append(updates.FROM_DISC_IMAGE)
            return rows, notes, folder, unnamed, {
                "looked_in": looked, "images": verdict.get("images"),
                "timing": meter.report()}

        task = self.submit(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_scanned)
        task.failed.connect(self._on_failed)
        task.done.connect(lambda: self._set_busy(False))
        self._task = task
        return task

    def _on_scanned(self, result):
        rows, notes, folder, unnamed, where = result
        # Merged, never replaced. A scan that did not look at disc images must
        # not take the image-only games off the table.
        self._rows = updates.merge_scan(
            self._rows, list(rows),
            looked_in=where.get("looked_in", ()),
            looked_at=where.get("looked_at"))
        self._packages = folder
        self._unidentified = list(unnamed)
        self._image_counts = where.get("images")
        self._timing = where.get("timing") or ""
        host = self.connection.host if self.connection else ""
        updates.remember_scan(self._settings(), host, self._rows)
        self._behind = [row.title_id for row in self._rows
                        if row.out_of_date or (row.nothing_installed
                                               and row.package is not None)]
        self._rescan.setEnabled(True)
        self._refresh_scan_button()
        # The action moves from "find out" to "do it" once there is a list.
        # One coloured button at a time, whichever moment it is.
        widgets.set_role(self._rescan, widgets.NEUTRAL)
        widgets.set_role(self._go, widgets.PRIMARY)
        self._fill_table()

        lines = list(notes)
        if self._timing:
            lines.append(f"Where the time went: {self._timing}")
        if folder is not None and folder.unknown:
            lines.append(folder.reason)
        elif folder is not None and folder.names:
            # Said plainly, and no longer as a warning: installs name one file,
            # so what else is in there is not going anywhere.
            lines.append(
                "The console's packages folder already holds "
                f"{_and_list(folder.names)}. Nothing here installs those; "
                "only the updates you tick are sent and installed.")
        self._detail.set_summary(_detail_summary(self._image_counts))
        self._detail.setText("\n\n".join(item for item in lines if item))

        if self._wanted:
            self._apply_preselect()

        available = [row for row in self._rows if row.updatable
                     and (row.out_of_date or row.installed is None)]
        if not self._rows:
            self._show_panel("warn", "No games were found on this console",
                             "Nothing was found in the console's game "
                             "folders. A game that is only in the disc drive, "
                             "and has never been copied onto the console, "
                             "does not appear here.")
        elif not available:
            self._show_panel("ok", "Everything is up to date", NOTHING_TO_DO)
        else:
            self._hide_panel()

    def _fill_table(self):
        self._table.blockSignals(True)
        self._table.clear()
        for row in self._rows:
            item = QTreeWidgetItem([
                row.name, row.title_id, row.installed_text, row.latest_text,
                row.size_text, row.state_text,
            ])
            item.setData(0, Qt.UserRole, row.title_id)
            item.setToolTip(0, row.detail)
            item.setToolTip(5, row.detail)
            if row.updatable and (row.out_of_date or row.installed is None):
                # Nothing is ticked when the list appears. See the docstring.
                item.setCheckState(0, Qt.Unchecked)
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
            self._paint_row(item, row)
            self._table.addTopLevelItem(item)
        for index in range(len(COLUMNS)):
            self._table.resizeColumnToContents(index)
        self._table.blockSignals(False)
        self._update_go()

    def _paint_row(self, item, row):
        token = "text"
        if row.out_of_date or (row.nothing_installed and row.updatable):
            # A game with nothing installed is the most actionable row here and
            # is coloured like one, rather than dimmed alongside the rows whose
            # version could not be read.
            token = "warn"
        elif row.blocked == updates.MANIFEST_FAILED:
            token = "error"
        elif row.blocked == updates.UP_TO_DATE:
            token = "ok"
        elif row.installed is None:
            token = "text_dim"
        brush = self._brush(token)
        if brush is not None:
            item.setForeground(5, brush)

    def _on_tick(self, item, column):
        if column == 0:
            self._update_go()
            row = self._row_for_item(item)
            if row is not None:
                self._detail.setText(row.detail)

    def _row_for_item(self, item):
        wanted = item.data(0, Qt.UserRole)
        for row in self._rows:
            if row.title_id == wanted:
                return row
        return None

    def _items(self):
        """Every row in the table, in the order they are shown."""
        return [self._table.topLevelItem(index)
                for index in range(self._table.topLevelItemCount())]

    def selected_rows(self):
        """The ticked rows, in the order they are shown."""
        out = []
        for item in self._items():
            if item.checkState(0) == Qt.Checked:
                row = self._row_for_item(item)
                if row is not None:
                    out.append(row)
        return out

    def _on_all_toggled(self, checked):
        """Tick or untick every row that carries a tick box.

        A row that is up to date, or blocked for any other reason, has the
        checkable flag cleared in _fill_table and is passed over here. Giving
        one a tick would offer to download something this screen has already
        said it cannot fetch.
        """
        state = Qt.Checked if checked else Qt.Unchecked
        self._table.blockSignals(True)
        for item in self._items():
            if item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(0, state)
        self._table.blockSignals(False)
        self._update_go()

    def _sync_all_box(self):
        """Keep the bulk tick in step with the rows under it.

        Ticking the last row by hand leaves every updatable row ticked, and a
        bulk box still sitting empty at that point reads as a control that has
        stopped working.
        """
        tickable = [item for item in self._items()
                    if item.flags() & Qt.ItemIsUserCheckable]
        every = bool(tickable) and all(
            item.checkState(0) == Qt.Checked for item in tickable)
        self._all_box.blockSignals(True)
        self._all_box.setEnabled(bool(tickable))
        self._all_box.setChecked(every)
        self._all_box.blockSignals(False)

    def _update_go(self):
        self._sync_all_box()
        self._go.setEnabled(bool(self.selected_rows()) and not self._working)

    # -- doing it

    def _on_go(self):
        chosen = self.selected_rows()
        if not chosen or self._working:
            return None
        if not self.confirm(chosen):
            return None
        return self.start_run(chosen)

    def confirm(self, chosen):
        """Walk through what is about to happen, then say whether to do it.

        A modal over the window rather than a line of text on it. Everything
        after this point takes minutes and writes to somebody's console, and
        the one thing that makes it go wrong is the console being inside a
        game rather than sat on its own menu. That is worth stopping for.

        Overridden wholesale in tests, which is why the work is in a dialog of
        its own rather than inline here.
        """
        dialog = InstallWizard(chosen, self.theme, self)
        try:
            self._blur(True)
            return bool(dialog.exec())
        finally:
            self._blur(False)
            dialog.deleteLater()

    def _blur(self, on):
        """Soften the screen behind the wizard.

        Wrapped because a graphics effect is the sort of thing a remote
        desktop or a software renderer refuses, and a blur that will not apply
        is no reason to withhold the dialog.
        """
        try:
            if on:
                effect = QGraphicsBlurEffect(self)
                effect.setBlurRadius(9)
                self.setGraphicsEffect(effect)
            else:
                self.setGraphicsEffect(None)
        except Exception:                                   # noqa: BLE001
            pass

    def start_run(self, chosen):
        host = self.connection.host if self.connection else ""
        if not host:
            self._show_panel("warn", "No console address has been entered yet",
                             NO_HOST)
            return None

        self._working = True
        self._go.setEnabled(False)
        self._rescan.setEnabled(False)
        self._full_box.setEnabled(False)
        self._set_busy(True, "Downloading from Sony")
        self._start_queue([(row.title_id, row.name or row.title_id)
                           for row in chosen])

        open_writer = self._writer
        open_actions = self._actions
        open_lister = self._lister
        read_storage = self._storage
        stream = self._stream()
        history = self._verify_history
        poll_seconds = self.install_poll_seconds
        timeout_seconds = self.install_timeout_seconds

        def work(control):
            devices = read_storage(host)
            free = updates.free_bytes_for(devices)
            done = []
            skipped = []
            with open_lister(host) as lister:
                # Asked fresh, per title, right before its download starts.
                # See updates.deliver: a retry after a part-finished run must
                # not fetch what already went in.
                def installed_reader(title_id):
                    return updates.read_installed_version(
                        getattr(lister, "download_bytes", None), title_id)

                # The write connection is opened when each upload is ready
                # to start rather than held open across the download in front
                # of it. webMANftpd lets go of an idle one, and the first
                # upload command then met a socket that was already dead and
                # sat waiting on it.
                import tempfile
                with tempfile.TemporaryDirectory(prefix="ps3-update-") as temp:
                    for row in chosen:
                        if control.cancelled:
                            break
                        delivered = updates.deliver(
                            row, lambda: open_writer(host), temp,
                            stream=stream,
                            progress=control.progress,
                            cancelled=lambda: control.cancelled,
                            free_bytes=free, history=history,
                            installed_reader=installed_reader)
                        if delivered.skipped:
                            skipped.append(delivered)
                            continue
                        done.append(delivered)
                        # Each upload eats into what is left, and the console
                        # is not asked again between them.
                        if free is not None:
                            free = max(0, free - delivered.bytes_sent)
            if not done:
                return done, [], skipped
            # One call per file, and each one is confirmed on the console
            # before the next is fired. Seven sent back to back landed three:
            # webMAN ignores an install while it is still busy with the last.
            #
            # NEVER build this list by listing /dev_hdd0/packages: the folder
            # may hold packages the user put there themselves and those are
            # not ours to run. The only thing installed is what this upload
            # just wrote, by exact name, and the same name is what gets
            # deleted afterwards.
            #
            # One connection for the whole install stage: the poll that waits
            # for the console to delete each package runs down it every
            # second, and the version read that confirms the install goes the
            # same way.
            with open_lister(host) as confirm_lister:
                results = updates.install_queue(
                    open_actions(host),
                    [(item.filename, item.title_id, item.bytes_sent,
                      item.version) for item in done],
                    updates.package_checker(confirm_lister),
                    confirm=updates.version_confirmation(
                        updates.version_reader(confirm_lister)),
                    on_progress=control.progress,
                    cancelled=lambda: control.cancelled,
                    poll_seconds=poll_seconds,
                    timeout_seconds=timeout_seconds)
            return done, results, skipped

        task = self.submit(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_finished)
        task.failed.connect(self._on_failed)
        task.done.connect(self._finished_working)
        self._task = task
        return task

    def _finished_working(self):
        self._working = False
        self._set_busy(False)
        self._rescan.setEnabled(True)
        self._refresh_scan_button()
        self._update_go()

    def _on_finished(self, result):
        done, results, skipped = result
        self._queue_results(results)
        already = "\n\n".join(item.reason for item in skipped if item.reason)
        if not done:
            if skipped:
                self._show_panel("ok", "Nothing needed sending", already)
                return
            self._show_panel("warn", "Nothing was copied to the console",
                             "The update was stopped before anything was "
                             "copied across.")
            return
        landed = [item for item in results if item.confirmed]
        missed = [item for item in results if not item.confirmed]
        if missed:
            # An update the version read could not settle is not an update
            # that failed, and the heading says which of the two happened.
            failed = [item for item in missed
                      if item.state == updates.STATE_FAILED]
            self._show_panel(
                "warn",
                "Some updates did not install" if failed
                else "Some installs could not be confirmed",
                _install_report(landed, missed)
                + ("\n\n" + already if already else ""))
        else:
            count = len(landed)
            body = (f"{_and_list([item.filename for item in landed])} "
                    f"installed on the console, checked against Sony's "
                    f"checksum on the way.")
            body += (" The package files stay in the console's packages "
                     "folder. Nothing here deletes them, and they can be "
                     "removed from the console whenever you like.")
            if already:
                body += f"\n\n{already}"
            self._show_panel(
                "ok",
                f"{count} updates installed" if count > 1 else "Installed",
                body)
        # The table still says what was true before any of this. Read it
        # again so the versions on screen are the ones on the console.
        self.start_scan()

    def _on_failed(self, message):
        self._rescan.setEnabled(True)
        self._refresh_scan_button()
        self._show_panel("error", "This did not finish", message)

    # -- progress

    def _on_progress(self, payload):
        if not isinstance(payload, tuple) or not payload:
            return
        kind = payload[0]
        if kind == "scanned":
            self._stage.setText(f"Asking Sony about {payload[1]} game(s)")
            return
        if kind == "stage" and len(payload) >= 2:
            self._stage.setText(payload[1])
            return
        if kind == "image" and len(payload) >= 4:
            _kind, done, total, name = payload[:4]
            # Which one and how far through, the same as the transfer screen.
            # It read thirteen images with nothing on screen and looked hung.
            self._stage.setText(f"Reading {name} ({done + 1} of {total})")
            if total:
                self._bar.setRange(0, 100)
                self._bar.setValue(int(done * 100 / total))
            return
        if kind in updates.INSTALL_QUEUE_STAGES and len(payload) >= 5:
            _kind, filename, first, second, title_id = payload[:5]
            self._queue_stage(title_id or filename,
                              updates.install_stage_text(kind, first, second))
            if kind == "installing":
                self._stage.setText("Asking the console to install it")
                self._show_count("")
                self._bar.setRange(0, 0)
            elif kind == "waiting":
                # Seconds and the name of the stage. The console reports
                # nothing else until it deletes the package, so a bar here
                # would be moving on a guess.
                self._stage.setText(
                    f"Installing {filename}. {int(first)} seconds so far, "
                    f"of {int(second)} allowed")
            elif kind == "checking":
                self._stage.setText(
                    f"Reading the version {title_id or filename} reports now")
            return
        if kind in ("download", "upload") and len(payload) >= 4:
            _kind, title_id, done, total = payload[:4]
            word = "Downloading from Sony" if kind == "download" \
                else "Copying to the console"
            self._stage.setText(f"{word}: {title_id}")
            self._queue_stage(title_id, updates.install_stage_text(kind))
            if total:
                self._bar.setRange(0, 100)
                self._bar.setValue(int(done * 100 / total))
                self._show_count(f"{parsers.human_size(done)} of "
                                 f"{parsers.human_size(total)}")
            return
        if len(payload) == 3:
            index, total, title_id = payload
            self._stage.setText(f"Checking {title_id} ({index} of {total})")
            if total:
                self._bar.setRange(0, 100)
                self._bar.setValue(int(index * 100 / total))

    # -- the per-package list

    def _start_queue(self, rows):
        """Begin the list: every package named, none of them started."""
        self._queue_rows = [[key, name, "waiting to start"]
                            for key, name in rows]
        self._paint_queue()

    def _queue_stage(self, key, text):
        """Put one package at a stage. An unlisted one is added as it is."""
        if not text:
            return
        for row in self._queue_rows:
            if row[0] == key:
                row[2] = text
                break
        else:
            self._queue_rows.append([key, key, text])
        self._paint_queue()

    def _queue_results(self, results):
        """The state each package ended in, left on screen to be read.

        A package that did not install stays in the list saying so. It is the
        line somebody needs after the run, and a list that quietly dropped it
        would leave them thinking everything went in.
        """
        for item in results:
            self._queue_stage(item.title_id or item.filename,
                              item.state or updates.STATE_UNCONFIRMED)

    def _paint_queue(self):
        """The list, with where each package ended up in its own colour.

        Rich text rather than a stylesheet: the colour is per line, and the
        row it belongs to is the one that changed.
        """
        lines = []
        for _key, name, state in self._queue_rows:
            said = html.escape(str(state))
            colour = self._colour_name(updates.install_state_token(state))
            if colour:
                said = f'<span style="color: {colour}">{said}</span>'
            lines.append(f"{html.escape(str(name))}: {said}")
        self._queue.setText("<br>".join(lines))
        self._queue.setVisible(bool(self._queue_rows))

    # -- panel, colours and chrome

    def _show_panel(self, token, heading, body):
        self._panel_token = token
        self._panel_heading.setText(heading)
        self._panel_body.setText(body)
        self._panel.show()
        self._paint_panel()

    def _hide_panel(self):
        self._panel.hide()
        self._panel_heading.setText("")
        self._panel_body.setText("")

    def _show_count(self, text):
        self._count.setText(text)
        self._count.setVisible(bool(text))
        self._paint_count()

    def _set_busy(self, busy, message=""):
        self._bar.setVisible(busy)
        if busy:
            self._bar.setRange(0, 0)
        if message:
            self._stage.setText(message)
        if not busy:
            self._stage.setText("")
            self._show_count("")
        self.busy_changed.emit(busy)

    def _paint_panel(self):
        accent = self._colour_name(self._panel_token) or \
            self._colour_name("text")
        surface = self._colour_name("surface_alt") or \
            self._colour_name("surface")
        border = self._colour_name("border") or accent
        text = self._colour_name("text")
        if not (accent and surface and text):
            return
        self._panel.setStyleSheet(
            f"QFrame#statepanel {{ background: {surface};"
            f" border: 1px solid {border};"
            f" border-left: 4px solid {accent}; border-radius: 7px; }}"
            f"QFrame#statepanel QLabel {{ color: {text};"
            f" background: transparent; border: none; }}")

    def _paint_count(self):
        dim = self._colour_name("text_dim")
        if dim:
            self._count.setStyleSheet(f"color: {dim};")

    def _paint_go(self):
        """The one control here that spends anything or changes anything.

        Flat and dim while it does nothing, unmistakable once it does.
        """
        accent = self._colour_name("accent")
        on_accent = self._colour_name("accent_text")
        border = self._colour_name("border")
        dim = self._colour_name("text_dim")
        surface = self._colour_name("surface_alt")
        if not (accent and on_accent and border and dim and surface):
            return
        self._go.setStyleSheet(
            f"QPushButton {{ background: {accent}; color: {on_accent};"
            f" border: 1px solid {accent}; border-radius: 7px;"
            f" padding: 6px 18px; font-weight: 600; }}"
            f"QPushButton:hover {{ border: 1px solid {on_accent}; }}"
            f"QPushButton:disabled {{ background: {surface}; color: {dim};"
            f" border: 1px solid {border}; font-weight: 400; }}")

    def _colour_name(self, token):
        try:
            return self.theme.colour(token)
        except (AttributeError, NotImplementedError, KeyError):
            return ""

    def _brush(self, token):
        from PySide6.QtGui import QBrush, QColor
        name = self._colour_name(token)
        return QBrush(QColor(name)) if name else None

    def _repaint(self):
        self._paint_go()
        self._paint_count()
        self._paint_queue()
        if not self._panel.isHidden():
            self._paint_panel()
        for index in range(self._table.topLevelItemCount()):
            item = self._table.topLevelItem(index)
            row = self._row_for_item(item)
            if row is not None:
                self._paint_row(item, row)

    def _on_back(self):
        if self.can_leave():
            self.request_home.emit()


def _device_names(read_storage, host):
    """The mounted devices, so games on a USB drive are looked at too.

    The console's own status page, through the same read-only seam the free
    space check uses. A console that will not answer it costs the USB drives
    and nothing else: dev_hdd0 is on every console and is the one that matters.
    """
    try:
        entries = read_storage(host) or []
    except Exception:                                       # noqa: BLE001
        return None
    names = [str(entry.get("device")) for entry in entries
             if entry.get("device")]
    return names or None


def _install_report(landed, missed):
    """Which ones went in and which did not, naming both."""
    lines = []
    if landed:
        lines.append(f"Installed: {_and_list([i.filename for i in landed])}.")
    unsent = [item for item in missed if "not sent" in item.reason]
    stalled = [item for item in missed if item not in unsent]
    for item in stalled:
        lines.append(item.reason)
    if unsent:
        lines.append(f"The queue stopped before these were sent: "
                     f"{_and_list([i.filename for i in unsent])}.")
    lines.append("Everything named here is on the console in its packages "
                 "folder. Open Package Manager on the console and install "
                 "what is missing from the list.")
    return "\n\n".join(lines)


def _and_list(names):
    """"a", "a and b", "a, b and c". Used in sentences shown to the user."""
    names = [str(name) for name in names if name]
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]
