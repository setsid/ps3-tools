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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from ps3diag import parsers, transport
from ps3tools import titles, updates
from ps3tools.consoleactions import ConsoleActions
from ps3tools.patching.ftpwrite import FtpWriter
from ps3tools.shell import widgets
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

COLUMNS = ["Game", "Title ID", "Installed", "Newest", "Size", ""]

NO_HOST = (
    "Type the console's address into the box at the top of this window, or "
    "press Find my PS3 next to it. The console shows its own address in "
    "webMAN, and it usually starts 192.168.")

NOTHING_TO_DO = (
    "Every game on this console already has the newest title update Sony "
    "published for it, so there is nothing to do here.")

#: What the scan button says before anything has been scanned, and after.
SCAN_LABEL = "Check for updates"
RESCAN_LABEL = "Check again"

#: Said on arrival, in place of scanning unasked.
READY_TO_SCAN = (
    "Press Check for updates below. This reads every game on the console and "
    "then asks Sony about each one.\n\n"
    "On a console with a lot of games it takes a few minutes, and it says "
    "what it is doing as it goes. Nothing is downloaded or changed until you "
    "tick something afterwards.")

WHAT_THIS_IS = (
    "Games get fixes after they are released. This checks each game on the "
    "console against Sony's own list and offers the ones that are behind. "
    "Tick the ones you want and press the button underneath.")


@register
class GameUpdatesScreen(Screen):
    """Check installed games against Sony's published title updates."""

    key = "updates"
    title = "Game updates"
    blurb = "Checks each game on the console for a newer version from Sony."
    tile = "GU"
    # After the two patch cards and well before About.
    order = 40

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
        # What has already failed verification, and how, for the length of
        # this screen. It is what lets a second identical failure stop telling
        # the user to try again. See updates.verify_download.
        self._verify_history = {}
        # Seams, so a test does not sit through a real install timeout. The
        # defaults are what runs against a console.
        self.install_poll_seconds = updates.INSTALL_POLL_SECONDS
        self.install_timeout_seconds = updates.INSTALL_TIMEOUT_SECONDS
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

        self._table = QTreeWidget()
        self._table.setColumnCount(len(COLUMNS))
        self._table.setHeaderLabels(COLUMNS)
        self._table.setRootIsDecorated(False)
        self._table.setUniformRowHeights(True)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._table.itemChanged.connect(self._on_tick)
        layout.addWidget(self._table, 1)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._detail)

        self._stage = QLabel("")
        self._stage.setWordWrap(True)
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
        self._rescan = QPushButton(SCAN_LABEL)
        self._rescan.clicked.connect(lambda: self.start_scan())
        widgets.set_role(self._rescan, widgets.PRIMARY)
        # Only ever shown when a remembered scan left something behind. See
        # _offer_remembered.
        self._partial = QPushButton("Check the ones that were behind")
        self._partial.clicked.connect(self._on_partial)
        self._partial.hide()
        buttons.addWidget(self._partial)
        buttons.addWidget(self._rescan)
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
        was true a week ago may be acted on today. Both buttons ask Sony
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
        if self._behind:
            count = len(self._behind)
            self._partial.setText(
                f"Check the {count} that {'was' if count == 1 else 'were'} "
                f"behind")
            self._partial.show()
            body = (f"This is what the last check found, on {when}. "
                    f"{count} game{'' if count == 1 else 's'} "
                    f"{'was' if count == 1 else 'were'} behind.\n\n"
                    f"Checking those again takes seconds, because the console "
                    f"does not have to be read through from the start. "
                    f"Checking everything finds games added since.")
        else:
            self._partial.hide()
            body = (f"This is what the last check found, on {when}. Everything "
                    f"was up to date.\n\n{READY_TO_SCAN}")
        self._show_panel("info", "What the last check found", body)
        return True

    def _on_partial(self):
        return self.start_scan(only=list(self._behind))

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
        self._partial.setEnabled(False)
        self._set_busy(True, "Checking the games that were behind"
                       if only else "Looking at the games on the console")

        open_lister = self._lister
        read_storage = self._storage
        fetcher = self._fetcher()
        read_images = bool(read_images)
        wanted = list(only or [])

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
                identifier = (updates.parallel_image_identifier(
                    lambda: open_lister(host),
                    on_progress=lambda done, total, name: control.progress(
                        ("image", done, total, name)))
                    if read_images else None)
                installed, notes, unnamed = updates.scan_console(
                    lister, devices=devices, image_identifier=identifier,
                    on_stage=lambda text: control.progress(("stage", text)))
                folder = updates.inspect_packages_folder(lister)
            control.progress(("scanned", len(installed)))
            rows = updates.check_titles(
                installed, fetcher=fetcher, progress=control.progress,
                cancelled=lambda: control.cancelled)
            # The image pass counts as looked-in only when it left nothing
            # unidentified behind. A pass that was skipped, or that lost a
            # worker, has not looked and may not remove anything.
            looked = [updates.FROM_GAME_FOLDER]
            if read_images and not unnamed:
                looked.append(updates.FROM_DISC_IMAGE)
            return rows, notes, folder, unnamed, {"looked_in": looked}

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
        host = self.connection.host if self.connection else ""
        updates.remember_scan(self._settings(), host, self._rows)
        self._behind = [row.title_id for row in self._rows
                        if row.out_of_date or (row.nothing_installed
                                               and row.package is not None)]
        self._partial.setEnabled(True)
        self._partial.setVisible(bool(self._behind))
        if self._behind:
            count = len(self._behind)
            self._partial.setText(
                f"Check the {count} that {'is' if count == 1 else 'are'} "
                f"behind")
        self._rescan.setEnabled(True)
        self._rescan.setText(RESCAN_LABEL)
        # The action moves from "find out" to "do it" once there is a list.
        # One coloured button at a time, whichever moment it is.
        widgets.set_role(self._rescan, widgets.NEUTRAL)
        widgets.set_role(self._go, widgets.PRIMARY)
        self._fill_table()

        lines = list(notes)
        if folder is not None and folder.unknown:
            lines.append(folder.reason)
        elif folder is not None and folder.names:
            # Said plainly, and no longer as a warning: installs name one file,
            # so what else is in there is not going anywhere.
            lines.append(
                "The console's packages folder already holds "
                f"{_and_list(folder.names)}. Nothing here installs those; "
                "only the updates you tick are sent and installed.")
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

    def selected_rows(self):
        """The ticked rows, in the order they are shown."""
        out = []
        for index in range(self._table.topLevelItemCount()):
            item = self._table.topLevelItem(index)
            if item.checkState(0) == Qt.Checked:
                row = self._row_for_item(item)
                if row is not None:
                    out.append(row)
        return out

    def _update_go(self):
        self._go.setEnabled(bool(self.selected_rows()) and not self._working)

    # -- doing it

    def confirm(self, chosen):
        """Asked before anything is downloaded. Overridden in tests.

        The packages folder is named here rather than only on the scan, because
        this is the moment it matters: pressing this button installs everything
        in that folder and not just the files this program put there.
        """
        total = sum(row.package.size for row in chosen if row.package)
        lines = [
            f"{len(chosen)} update(s) will be downloaded from Sony, "
            f"{parsers.human_size(total)} in total, and copied to the "
            f"console.",
            "",
            "Each one is checked against Sony's own checksum before it is "
            "copied. If a download does not match, it is deleted and nothing "
            "is copied.",
        ]
        if self._packages is not None and self._packages.names:
            lines += [
                "",
                "The console's packages folder already holds "
                f"{_and_list(self._packages.names)}. The console installs "
                "everything in that folder at once, so those will be "
                "installed too. Continue only if you know what they are.",
            ]
        box = QMessageBox(self)
        box.setWindowTitle("Download and install updates")
        box.setText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Yes

    def _on_go(self):
        chosen = self.selected_rows()
        if not chosen or self._working:
            return None
        if not self.confirm(chosen):
            return None
        return self.start_run(chosen)

    def start_run(self, chosen):
        host = self.connection.host if self.connection else ""
        if not host:
            self._show_panel("warn", "No console address has been entered yet",
                             NO_HOST)
            return None

        self._working = True
        self._go.setEnabled(False)
        self._rescan.setEnabled(False)
        self._set_busy(True, "Downloading from Sony")

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

                with open_writer(host) as writer:
                    import tempfile
                    with tempfile.TemporaryDirectory(
                            prefix="ps3-update-") as temp:
                        for row in chosen:
                            if control.cancelled:
                                break
                            delivered = updates.deliver(
                                row, writer, temp, stream=stream,
                                progress=control.progress,
                                cancelled=lambda: control.cancelled,
                                free_bytes=free, history=history,
                                installed_reader=installed_reader)
                            if delivered.skipped:
                                skipped.append(delivered)
                                continue
                            done.append(delivered)
                            # Each upload eats into what is left, and the
                            # console is not asked again between them.
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
            with open_lister(host) as confirm_lister:
                results = updates.install_queue(
                    open_actions(host),
                    [(item.filename, item.title_id) for item in done],
                    updates.installed_checker(confirm_lister),
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
        self._update_go()

    def _on_finished(self, result):
        done, results, skipped = result
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
            self._show_panel(
                "warn", "Some updates did not install",
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
        if kind == "installing":
            self._stage.setText("Asking the console to install it")
            self._show_count("")
            self._bar.setRange(0, 0)
            return
        if kind in ("download", "upload") and len(payload) >= 4:
            _kind, title_id, done, total = payload[:4]
            word = "Downloading from Sony" if kind == "download" \
                else "Copying to the console"
            self._stage.setText(f"{word}: {title_id}")
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
