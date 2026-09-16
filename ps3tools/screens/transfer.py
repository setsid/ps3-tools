"""The transfer games card: disc images from this computer onto the console.

The screen exists because the copy built into webMAN is unusable rather than
broken. On a real console it ran for two hours with nothing on screen, put a
PS3 image into the PS2 folder because it believed the filename, and dropped a
file whose name had an ampersand in it without a word. The only way to follow
any of it was to open a shell and poll FTP.

So this screen is mostly made of things being said out loud. The queue is
shown before anything starts, with what each image actually is, where it is
going and the name it will have when it gets there. The time it will take is
an estimate on the screen before the button is pressed, not a discovery made
four hours in. Progress is bytes, a rate and a time remaining, per file and
overall. And when it finishes, the console is asked again what is in the
folder, because a 226 from an FTP server is not evidence that a file arrived.

It is also honest about what it cannot do. A PS3 takes files at about four
megabytes a second and nothing on this side changes that. The screen says so,
in a panel that cannot be scrolled past, and the tips beside it are worded as
the modest gains they are.

Nothing here opens a socket of its own. Every client comes from a seam below
that the tests replace, and there is a test that replaces them with something
that raises on construction and then runs the whole flow.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog,
                               QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ps3diag import parsers, transport
from ps3tools import transfer, updates
from ps3tools.patching.ftpwrite import FtpWriter
from ps3tools.shell import widgets
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

COLUMNS = ["File", "Size", "What it is", "Goes to", "Name on the console",
           "What will happen"]

CONSOLE_COLUMNS = ["Folder", "Name", "Size"]

FILE_FILTER = "Disc images (*.iso *.ISO)"

WHAT_THIS_IS = (
    "Copies PlayStation 3 and PlayStation 2 disc images from this computer to "
    "the console, so they can be played from the hard drive without the disc.")

NO_HOST = (
    "Type the console's address into the box at the top of this window, or "
    "press Find my PS3 next to it.")

ALREADY_THERE = (
    "These are the games already on the console. Anything you choose that is "
    "already there is shown in the list below with the tick taken out, so a "
    "copy that would take hours and change nothing does not start by "
    "accident. Tick it again to copy over the top of it.")

CHECKING_AGAIN = "Asking the console what it already has"

NOTHING_LEFT = (
    "The name and the size both match what is on the console, so nothing was "
    "copied.\n\n"
    "Tick a file again to copy it over the top of the one on the console.")

CHECK_FAILED = (
    "What is already on the console could not be read just now. Each file is "
    "checked against the folder again as it is about to be copied, and "
    "anything that is already there in full is left alone.")

NOT_RECOGNISED = (
    "Some of these were not recognised. This program reads the inside of each "
    "file to find out what it is, rather than trusting what it is called, and "
    "for these it could not tell. They are listed below with the reason and "
    "they will not be copied. If you know what one of them is, put it in the "
    "PS3ISO or PS2ISO folder on the console yourself.")


@register
class TransferGamesScreen(Screen):
    """Queue disc images, send them, and say what happened."""

    key = "transfer"
    title = "Transfer games"
    blurb = "Copies game disc images from this computer to the console."
    tile = "TR"
    order = 60

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        self._items = []
        self._console_files = []
        self._listings = {}
        #: Title IDs with a folder under /dev_hdd0/game. A game installed
        #: there is not a file in any of the folders this screen copies into,
        #: so nothing in _listings can answer whether it is already here.
        self._installed = []
        self._task = None
        self._control = None
        self._working = False
        self._panel_token = ""
        # A panel holding the result of a run stays up until the user does
        # something else. A background scan finishing afterwards must not wipe
        # the only record of what happened off the screen.
        self._sticky_panel = False
        # Set when the console could not be read again on the way to the
        # confirmation box, so that the box can say so instead of the user
        # being asked to commit to hours without knowing the check was missed.
        self._check_note = ""
        self._devices = []
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

        heading = QLabel("Transfer games")
        font = heading.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)

        blurb = QLabel(WHAT_THIS_IS)
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        # The honest paragraph about speed, at the top where it cannot be
        # scrolled past, with the tips under it. Somebody about to start a
        # thirteen hour job is entitled to know that before they start it and
        # not after.
        self._notice = QFrame()
        self._notice.setObjectName("speednotice")
        notice = QVBoxLayout(self._notice)
        notice.setContentsMargins(16, 14, 16, 14)
        notice.setSpacing(6)
        self._notice_text = QLabel(transfer.HONEST_SPEED)
        self._notice_text.setWordWrap(True)
        notice_font = self._notice_text.font()
        notice_font.setBold(True)
        self._notice_text.setFont(notice_font)
        notice.addWidget(self._notice_text)
        self._tips = QLabel(
            "What does help, in order:\n"
            + "\n".join(f"•  {tip}" for tip in transfer.TIPS))
        self._tips.setWordWrap(True)
        notice.addWidget(self._tips)
        layout.addWidget(self._notice)

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

        where = QHBoxLayout()
        where.addWidget(QLabel("Copy to:"))
        self._device = QComboBox()
        self._device.addItem(transfer.DEFAULT_DEVICE)
        self._device.currentTextChanged.connect(self._on_device)
        where.addWidget(self._device)
        self._space = QLabel("")
        where.addWidget(self._space)
        where.addStretch(1)
        layout.addLayout(where)

        self._already = QLabel(ALREADY_THERE)
        self._already.setWordWrap(True)
        layout.addWidget(self._already)

        self._console_heading = QLabel("Already on the console")
        console_font = self._console_heading.font()
        console_font.setBold(True)
        self._console_heading.setFont(console_font)
        layout.addWidget(self._console_heading)

        self._console_table = QTreeWidget()
        self._console_table.setColumnCount(len(CONSOLE_COLUMNS))
        self._console_table.setHeaderLabels(CONSOLE_COLUMNS)
        self._console_table.setRootIsDecorated(False)
        self._console_table.setUniformRowHeights(True)
        self._console_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._console_table.setMaximumHeight(150)
        layout.addWidget(self._console_table)

        # Above the queue rather than in it. The same warning was already in
        # the last column of every row that carried it, and that column is off
        # the right-hand edge of the window at the width this screen opens at:
        # eight of fourteen rows said it on a real console and none of them
        # could be seen without scrolling sideways.
        #
        # Behind an arrow and closed, the way the Game updates summary is.
        # Nine games named at full length with a paragraph under them left two
        # of twelve file rows on the screen, and the file list is the thing
        # being acted on.
        self._installed_panel = widgets.Disclosure("")
        layout.addWidget(self._installed_panel)

        self._table = QTreeWidget()
        self._table.setColumnCount(len(COLUMNS))
        self._table.setHeaderLabels(COLUMNS)
        self._table.setRootIsDecorated(False)
        self._table.setUniformRowHeights(True)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._table.itemChanged.connect(self._on_ticked)
        # The redraw a tick causes is queued rather than done inside the
        # signal. Owned by the screen so it dies with it: a bare singleShot
        # firing into a deleted widget is a crash on shutdown, and the whole
        # reason this timer exists is a crash of exactly that kind.
        self._redraw = QTimer(self)
        self._redraw.setSingleShot(True)
        self._redraw.timeout.connect(self._fill_table)
        layout.addWidget(self._table, 1)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._detail)

        self._estimate = QLabel("")
        self._estimate.setWordWrap(True)
        layout.addWidget(self._estimate)

        self._stage = QLabel("")
        # Room for four lines. It was cut off at three, mid-sentence.
        widgets.fit_progress_label(self._stage)
        layout.addWidget(self._stage)

        self._file_line = QLabel("")
        self._file_line.setWordWrap(True)
        layout.addWidget(self._file_line)

        self._file_bar = QProgressBar()
        self._file_bar.setRange(0, 100)
        self._file_bar.setTextVisible(False)
        self._file_bar.hide()
        layout.addWidget(self._file_bar)

        self._overall_line = QLabel("")
        self._overall_line.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._overall_line.hide()
        layout.addWidget(self._overall_line)

        self._overall_bar = QProgressBar()
        self._overall_bar.setRange(0, 100)
        self._overall_bar.setTextVisible(False)
        self._overall_bar.hide()
        layout.addWidget(self._overall_bar)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        layout.addWidget(line)

        buttons = QHBoxLayout()
        self._back = QPushButton("Back")
        self._back.clicked.connect(self._on_back)
        buttons.addWidget(self._back)
        buttons.addStretch(1)
        self._add = QPushButton("Choose disc images")
        self._add.clicked.connect(self._on_add)
        buttons.addWidget(self._add)
        self._clear = QPushButton("Clear the list")
        self._clear.clicked.connect(self._on_clear)
        buttons.addWidget(self._clear)
        self._pause = QPushButton("Pause")
        self._pause.clicked.connect(self._on_pause)
        self._pause.hide()
        buttons.addWidget(self._pause)
        self._stop = QPushButton("Stop")
        self._stop.clicked.connect(self._on_stop)
        self._stop.hide()
        buttons.addWidget(self._stop)
        self._go = QPushButton("Copy to the console")
        widgets.set_role(self._go, widgets.PRIMARY)
        self._go.setEnabled(False)
        self._go.clicked.connect(self._on_go)
        buttons.addWidget(self._go)
        self._paint_go()
        layout.addLayout(buttons)
        self._paint_notice()

    # -- the seams. Every test replaces both; the suite reaches neither.

    def _writer(self, host):
        return FtpWriter(host)

    def _lister(self, host):
        """The read-only diagnostic client, for looking at what is there.

        Listing the console's game folders changes nothing, so it goes through
        the client that cannot change anything rather than through the writer.
        """
        return transport.FtpLister(host)

    def _storage(self, host):
        """Free space per device, from the console's own status page.

        The read-only diagnostic client and the storage collector's own
        parser, so this card gives the same answer to "how much room is there"
        as the two cards that already ask.
        """
        probe = transport.HttpProbe(host)
        response = probe.get("/")
        if not getattr(response, "ok", False):
            return []
        return parsers.parse_storage(parsers.html_to_text(response.body))

    def choose_files(self):
        """The file picker. Overridden in tests, which never open a dialog."""
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Choose disc images", "", FILE_FILTER)
        return list(paths or [])

    # -- lifecycle

    def on_enter(self):
        self.scan_console()

    def on_leave(self):
        if self._control is not None:
            self._control.cancel()

    def can_leave(self):
        return not self._working

    def leave_blocked_reason(self):
        return ("A game is part way through being copied to the console. "
                "Press Stop first: what has already arrived is kept and the "
                "copy carries on from there next time.")

    # -- free space, read on entry so the figure is on screen before choosing

    def scan_console(self):
        """Free space, and every game already in the console's game folders.

        Both at once and on entry rather than behind a button. It costs one
        page and one listing per folder, all of them read-only, and it is the
        difference between seeing that the console already has a game and
        finding out thirteen hours later.
        """
        host = self.connection.host if self.connection else ""
        if not host:
            self._show_panel("warn", "No console address has been entered yet",
                             NO_HOST)
            return None
        read_storage = self._storage
        open_lister = self._lister
        device = self._device.currentText() or transfer.DEFAULT_DEVICE

        def work(control):
            devices = read_storage(host)
            with open_lister(host) as lister:
                # One walk answers all three. A game installed under its own
                # title ID is not a file in any of these folders, so the
                # listings alone could never see it.
                files, listings, installed = transfer.console_games(
                    lister, device)
            return devices, files, listings, installed

        task = self.submit(work)
        task.finished.connect(self._on_scan)
        task.failed.connect(self._on_scan_failed)
        self._task = task
        return task

    def _on_scan(self, result):
        devices, files, listings, installed = result
        self._devices = list(devices or [])
        self._console_files = list(files or [])
        self._listings = dict(listings or {})
        self._installed = list(installed or [])
        wanted = self._device.currentText() or transfer.DEFAULT_DEVICE
        names = transfer.storage_devices(self._devices)
        self._device.blockSignals(True)
        self._device.clear()
        self._device.addItems(names)
        if wanted in names:
            self._device.setCurrentText(wanted)
        self._device.blockSignals(False)
        self._show_space()
        self._fill_console_table()
        transfer.match_console(self._items, self._listings,
                               self._installed)
        self._fill_table()
        self._hide_panel()

    def _on_scan_failed(self, message):
        # Not fatal, and said rather than swallowed. The space check treats an
        # unknown figure as unknown rather than as zero, and the queue can
        # still be built and sent; the user is simply working without the list
        # of what is already there.
        self._space.setText("The console did not say how much room it has.")
        self._paint_space()
        self._console_heading.setText(
            "What is already on the console could not be read")

    def _fill_console_table(self):
        self._console_table.clear()
        for entry in self._console_files:
            row = QTreeWidgetItem([entry.folder, entry.name, entry.size_text])
            self._console_table.addTopLevelItem(row)
        for index in range(len(CONSOLE_COLUMNS)):
            self._console_table.resizeColumnToContents(index)
        count = len(self._console_files)
        if count:
            self._console_heading.setText(
                f"Already on the console: {count} "
                f"item{'s' if count != 1 else ''}")
        else:
            self._console_heading.setText(
                "Already on the console: nothing in the game folders")

    def _show_space(self):
        free = transfer.free_for(self._devices, self._device.currentText())
        if free is None:
            self._space.setText("The console did not say how much room it has.")
        else:
            self._space.setText(f"{parsers.human_size(free)} free")
        self._paint_space()

    def _on_device(self, name):
        # A different device is a different set of folders and a different
        # amount of room, so both are read again rather than carried over.
        for item in self._items:
            item.device = name
        self._show_space()
        # The marks on the queue were made against the folders of the device
        # that was selected a moment ago. Matching again against the listings
        # in hand drops them, so a row cannot go on claiming the console has a
        # file in a folder nobody has looked in yet; the scan below then fills
        # in what the new device really holds.
        transfer.match_console(self._items, self._listings,
                               self._installed)
        self._fill_table()
        self.scan_console()

    # -- choosing files

    def _on_add(self):
        paths = self.choose_files()
        if not paths:
            return None
        return self.add_files(paths)

    def add_files(self, paths):
        """Read the inside of each chosen image and put it in the queue."""
        known = {item.path for item in self._items}
        fresh = [path for path in paths if path not in known]
        device = self._device.currentText() or transfer.DEFAULT_DEVICE
        for path in fresh:
            self._items.append(transfer.inspect(path, device))
        # Names are settled across the whole queue rather than per file, so two
        # images that shorten to the same name are told apart before the user
        # is shown either of them. Only then is the console consulted, because
        # the name that matters is the one that will land on it.
        transfer.assign_names(self._items)
        transfer.match_console(self._items, self._listings,
                               self._installed)
        self._fill_table()
        return self._items

    def _on_clear(self):
        self._items = []
        self._detail.setText("")
        self._sticky_panel = False
        self._hide_panel()
        self._fill_table()

    def _fill_table(self):
        self._table.blockSignals(True)
        self._table.clear()
        for item in self._items:
            row = QTreeWidgetItem([
                item.filename, item.size_text, item.describes_as,
                item.destination or "", item.name if item.identified else "",
                item.plan_text])
            row.setData(0, Qt.UserRole, item.path)
            row.setToolTip(0, item.path)
            if item.detail:
                row.setToolTip(5, item.detail)
            if not item.identified:
                row.setFlags(row.flags() & ~Qt.ItemIsUserCheckable)
                brush = self._brush("error")
                if brush is not None:
                    row.setForeground(2, brush)
                if item.reason:
                    row.setToolTip(2, item.reason)
                self._table.addTopLevelItem(row)
                continue
            row.setCheckState(0, Qt.Checked if item.wanted else Qt.Unchecked)
            if item.filename != item.name:
                # The rename is the thing most likely to be mistaken for a
                # missing file later, so it is marked where it happens.
                brush = self._brush("info")
                if brush is not None:
                    row.setForeground(4, brush)
            if item.present:
                brush = self._brush("ok" if item.present == "same" else "warn")
                if brush is not None:
                    row.setForeground(5, brush)
            self._table.addTopLevelItem(row)
        for index in range(len(COLUMNS)):
            self._table.resizeColumnToContents(index)
        self._table.blockSignals(False)
        self._show_unidentified()
        self._show_installed()
        self._show_estimate()
        self._update_go()

    def _show_installed(self):
        """Name the queued images the console already has the game of.

        Every one of them, whether ticked or not. They arrive unticked now, so
        a block that listed only the ticked ones would be empty exactly when
        it has something to say.
        """
        group = transfer.installed_on_console(self._items)
        if not group:
            self._installed_panel.set_summary("")
            self._installed_panel.setText("")
            return
        one = len(group) == 1
        self._installed_panel.set_summary(
            "One of these games is already installed on this console"
            if one else
            f"{len(group)} of these games are already installed on this "
            f"console")
        lines = [f"    {transfer.describe_installed(item)}" for item in group]
        lines += ["", transfer.INSTALLED_WHY + "."]
        self._installed_panel.setText("\n".join(lines))

    def _on_ticked(self, row, column):
        """The user overruling the proposal. Their decision, not ours."""
        if column != 0:
            return
        path = row.data(0, Qt.UserRole)
        for item in self._items:
            if item.path == path:
                item.wanted = row.checkState(0) == Qt.Checked
                # Recorded here because this is the only place a tick is
                # known to have come from the user. The button reads the
                # console again on its way to the confirmation box, and the
                # match that follows used to put the tick back into every row
                # the console did not hold; unticking eight games and pressing
                # copy sent all eight of them.
                item.ticked = item.wanted
                # Ticking a file the console already has is the only thing
                # that authorises copying over the top of it, and it is
                # recorded here because this is the moment the user was
                # looking at the row that said so.
                item.overwrite = item.wanted and item.present == "same"
                break
        # The plan column and the estimate both change with the tick, so the
        # table is redrawn rather than left saying something that is no longer
        # true.
        #
        # Queued, and that is not a detail. itemChanged is emitted from inside
        # QTreeWidgetItem::setCheckState, and _fill_table clears the table,
        # which deletes every item including the one that call is still
        # running on. Qt then carries on against freed memory. It survived for
        # a long time because the block is usually still mapped; under a long
        # test run it stopped surviving and took the process down with SIGBUS.
        # Redrawing after Qt has finished with the item costs nothing and
        # cannot do that.
        self._redraw.start(0)

    def _show_unidentified(self):
        unknown = transfer.unidentified(self._items)
        if not unknown:
            self._detail.setText("")
            return
        lines = [NOT_RECOGNISED, ""]
        lines += [f"{item.filename}: "
                  f"{item.reason or 'it could not be identified.'}"
                  for item in unknown]
        self._detail.setText("\n".join(lines))

    def _show_estimate(self):
        queue = transfer.chosen(self._items)
        if not queue:
            self._estimate.setText("")
            return
        self._estimate.setText(transfer.time_estimate(self._items))

    def _update_go(self):
        self._go.setEnabled(bool(transfer.chosen(self._items))
                            and not self._working)

    # -- doing it

    def confirm_text(self, queue):
        """The last thing the user reads before committing to several hours.

        What is about to happen, and nothing else. It used to be built from
        the whole list, so with one file ticked it still said nine games were
        ticked and named two duplicates that were not going anywhere. A box
        that describes the screen's opening proposal rather than the queue is
        a box nobody can check their decision against.

        Kept apart from confirm() so that what it says can be read back
        without a modal dialog being put on the screen.
        """
        lines = [transfer.time_estimate(queue), "", transfer.HONEST_SPEED]
        if self._check_note:
            lines += ["", self._check_note]
        notes = transfer.console_notes(queue)
        if notes:
            lines += [""] + notes
        renamed = [item for item in queue if item.filename != item.name]
        if renamed:
            lines += ["", "These will be given shorter names on the console, "
                          "because long names and characters like & do not "
                          "list properly on it:"]
            lines += [f"    {item.filename}  becomes  {item.name}"
                      for item in renamed]
        return "\n".join(lines)

    def confirm(self, queue):
        """Asked before anything is sent. Overridden in tests."""
        box = QMessageBox(self)
        box.setWindowTitle("Copy to the console")
        box.setText(self.confirm_text(queue))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Yes

    def _on_go(self):
        """The button, and the check that should have been here all along.

        The list of what is on the console was read when the screen was
        entered. By the time the button is pressed it can be an hour old and a
        run in between can have put a game on the console that it does not
        mention, which is how somebody was shown "It will be copied" for a game
        they had just finished copying. So the console is asked again here,
        before the confirmation is shown and before a byte is sent. It costs
        one read-only listing per folder.
        """
        if self._working or not transfer.chosen(self._items):
            return None
        host = self.connection.host if self.connection else ""
        if not host:
            self._show_panel("warn", "No console address has been entered yet",
                             NO_HOST)
            return None
        open_lister = self._lister
        device = self._device.currentText() or transfer.DEFAULT_DEVICE

        def work(control):
            with open_lister(host) as lister:
                files, listings, installed = transfer.console_games(
                    lister, device)
            return files, listings, installed

        self._check_note = ""
        self._go.setEnabled(False)
        self._stage.setText(CHECKING_AGAIN)
        task = self.submit(work)
        task.finished.connect(self._on_recheck)
        task.failed.connect(self._on_recheck_failed)
        self._task = task
        return task

    def _on_recheck(self, result):
        """What the console says now, put on the screen before the question."""
        files, listings, installed = result
        self._console_files = list(files or [])
        self._listings = dict(listings or {})
        self._installed = list(installed or [])
        self._fill_console_table()
        transfer.match_console(self._items, self._listings,
                               self._installed)
        self._stage.setText("")
        # Redrawn first so that the rows and the ticks behind the box already
        # say what the box is about to say. A dialog that contradicts the table
        # under it is worse than either of them alone.
        self._fill_table()
        if not transfer.chosen(self._items):
            self._say_nothing_left()
            return None
        return self._start_confirmed()

    def _on_recheck_failed(self, message):
        # The console could not be read again. It is said in the box the user
        # is about to answer rather than in a panel behind it, and the run is
        # still offered, because every file is checked against a fresh listing
        # of its folder at the moment it is about to be copied and an identical
        # file already there is left alone by that check on its own.
        self._stage.setText("")
        self._check_note = CHECK_FAILED
        self._update_go()
        return self._start_confirmed()

    def _say_nothing_left(self):
        """Everything ticked turned out to be on the console already.

        Said in the panel and left there, because this is the answer to the
        button the user just pressed and they are entitled to see what became
        of it rather than watching the ticks come out of the rows.
        """
        group = transfer.already_on_console(self._items)
        names = _and_list([item.name for item in group])
        one = len(group) == 1
        heading = ("It is already on the console" if one
                   else "They are already on the console")
        lead = ("This is already on the console" if one
                else "These are already on the console")
        self._show_panel("ok", heading,
                         f"{lead}: {names}.\n\n{NOTHING_LEFT}", sticky=True)
        self._update_go()

    def _start_confirmed(self):
        queue = transfer.chosen(self._items)
        if not queue:
            return None
        if not self.confirm(queue):
            self._update_go()
            return None
        return self.start_run()

    def start_run(self):
        host = self.connection.host if self.connection else ""
        if not host:
            self._show_panel("warn", "No console address has been entered yet",
                             NO_HOST)
            return None
        queue = transfer.sendable(self._items)
        if not transfer.chosen(self._items):
            return None

        self._sticky_panel = False
        self._hide_panel()
        self._working = True
        self._control = transfer.Controller()
        self._go.setEnabled(False)
        self._add.setEnabled(False)
        self._clear.setEnabled(False)
        self._device.setEnabled(False)
        self._pause.setText("Pause")
        self._pause.show()
        self._stop.show()
        self._set_busy(True, "Starting")

        open_writer = self._writer
        read_storage = self._storage
        control_object = self._control
        device = self._device.currentText() or transfer.DEFAULT_DEVICE

        def work(control):
            devices = read_storage(host)
            free = transfer.free_for(devices, device)
            with open_writer(host) as writer:
                job = transfer.Transfer(queue, writer, control=control_object,
                                        free_bytes=free)
                return job.run(on_progress=control.progress)

        task = self.submit(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_finished)
        task.failed.connect(self._on_failed)
        task.done.connect(self._finished_working)
        self._task = task
        return task

    def _on_pause(self):
        if self._control is None:
            return
        if self._control.paused:
            self._control.resume()
            self._pause.setText("Pause")
            self.status_message.emit("Carrying on")
        else:
            self._control.pause()
            self._pause.setText("Carry on")
            self.status_message.emit("Paused")

    def _on_stop(self):
        if self._control is not None:
            self._control.cancel()
        self._stage.setText("Stopping after this part of the file")

    def _finished_working(self):
        self._working = False
        self._control = None
        self._set_busy(False)
        self._add.setEnabled(True)
        self._clear.setEnabled(True)
        self._device.setEnabled(True)
        self._pause.hide()
        self._stop.hide()
        # What the run did to the console is known here, and the queue is told
        # before the table is redrawn. Without this a game that had just
        # finished copying kept its tick and the button came back enabled, and
        # the console was not listed again until seconds later: enough of a gap
        # for somebody to press it and start the whole file over.
        transfer.settle_after_run(self._items)
        self._fill_table()
        self._update_go()
        # What is on the console has just changed, so the list of it is read
        # again rather than left showing what was there an hour ago.
        self.scan_console()

    # -- progress

    def _on_progress(self, progress):
        if not isinstance(progress, transfer.Progress):
            return
        self._stage.setText(progress.headline())
        if progress.stage == transfer.SENDING:
            self._file_line.setText(progress.detail())
            self._file_bar.setRange(0, 100)
            self._file_bar.setValue(progress.file_percent)
            self._file_bar.show()
            self._overall_line.setText(progress.overall_detail())
            self._overall_line.show()
            self._overall_bar.setRange(0, 100)
            self._overall_bar.setValue(progress.overall_percent)
            self._overall_bar.show()
            self._paint_count()
        elif progress.stage == "done":
            self._file_line.setText("")
            self._file_bar.hide()

    # -- what happened

    def _on_finished(self, report):
        lines = []
        if report.sent:
            names = _and_list([item.name for item in report.sent])
            lines.append(f"Copied: {names}.")
        if report.already:
            names = _and_list([item.name for item in report.already])
            lines.append(f"Already on the console, so not sent again: "
                         f"{names}.")
        for item in report.partial:
            lines.append(f"{item.name}: {item.detail}")
        for item in report.failed:
            lines.append(f"{item.filename}: {item.detail}")
        for item in report.skipped:
            lines.append(f"{item.filename}: {item.detail}")
        if report.renamed:
            lines.append("Renamed to avoid overwriting something already "
                         "there: " + ", ".join(f"{was} became {now}"
                                               for was, now in report.renamed))
        if report.checked:
            lines.append("")
            lines.append("The console's folder was listed again afterwards "
                         "and what is above is what it actually holds.")
        elif report.check_reason:
            lines.append("")
            lines.append("What arrived could not be checked afterwards: "
                         + report.check_reason)

        body = "\n".join(line for line in lines if line is not None)
        if report.missing or report.failed:
            self._show_panel("error", "Some of it did not arrive", body,
                             sticky=True)
        elif report.cancelled or report.partial:
            self._show_panel("warn", "Stopped before it finished", body,
                             sticky=True)
        elif report.sent or report.already:
            self._show_panel("ok", "Finished", body, sticky=True)
        else:
            self._show_panel("warn", "Nothing was copied", body, sticky=True)

    def _on_failed(self, message):
        self._show_panel("error", "This did not finish", message, sticky=True)

    # -- panel, colours and chrome

    def _show_panel(self, token, heading, body, sticky=False):
        self._sticky_panel = sticky
        self._panel_token = token
        self._panel_heading.setText(heading)
        self._panel_body.setText(body)
        self._panel.show()
        self._paint_panel()

    def _hide_panel(self):
        if self._sticky_panel:
            return
        self._panel.hide()
        self._panel_heading.setText("")
        self._panel_body.setText("")

    def _set_busy(self, busy, message=""):
        if message:
            self._stage.setText(message)
        if not busy:
            self._file_bar.hide()
            self._overall_bar.hide()
            self._overall_line.hide()
            self._file_line.setText("")
            self._stage.setText("")
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

    def _paint_notice(self):
        accent = self._colour_name("info") or self._colour_name("text")
        surface = self._colour_name("surface_alt") or \
            self._colour_name("surface")
        text = self._colour_name("text")
        if not (accent and surface and text):
            return
        self._notice.setStyleSheet(
            f"QFrame#speednotice {{ background: {surface};"
            f" border: 1px solid {accent};"
            f" border-left: 4px solid {accent}; border-radius: 7px; }}"
            f"QFrame#speednotice QLabel {{ color: {text};"
            f" background: transparent; border: none; }}")

    def _paint_count(self):
        dim = self._colour_name("text_dim")
        if dim:
            self._overall_line.setStyleSheet(f"color: {dim};")
            self._file_line.setStyleSheet(f"color: {dim};")

    def _paint_space(self):
        dim = self._colour_name("text_dim")
        if dim:
            self._space.setStyleSheet(f"color: {dim};")

    def _paint_go(self):
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
        self._paint_space()
        self._paint_notice()
        if not self._panel.isHidden():
            self._paint_panel()

    def _on_back(self):
        if self.can_leave():
            self.request_home.emit()


def _and_list(names):
    """"a", "a and b", "a, b and c". Used in sentences shown to the user."""
    names = [str(name) for name in names if name]
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]
