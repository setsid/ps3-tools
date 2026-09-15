"""The install packages screen.

The user picks .pkg files off their own computer, this copies them into the
console's packages folder and asks the console to install them. DLC, homebrew,
a title update somebody downloaded elsewhere: anything already on their disk.

It shares every piece of machinery with the game updates card -- the packages
folder listing, the free space check, the upload, the install call and its
exact-match allowlist all live in ps3tools.updates and ps3tools.consoleactions
and are called from here rather than written again. Two code paths that install
packages onto somebody's console is exactly the thing worth not having.

**The difference between the two cards is stated on this screen, in words.**
Game updates downloads from Sony and checks what arrived against a checksum
Sony published, and that check is the reason that card is safe to use. There is
no equivalent here and there cannot be one: the user chose the file and there
is no authority to check it against. A screen that looked as careful as the
other one would be telling a lie by resemblance, so it says so instead.

The header of each file is read, and that is a sanity check and nothing more.
It says whether the file is shaped like a package and which title it claims to
be for, which is how somebody finds out they picked the wrong file before it is
on their console. It proves nothing about what is inside and it is never
described as though it did. There is no hash of the user's own file anywhere on
this screen, because a hash with nothing to compare it against is decoration
that looks like a guarantee.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from ps3diag import parsers, transport
from ps3tools import updates
from ps3tools.consoleactions import ConsoleActions
from ps3tools.patching.ftpwrite import FtpWriter
from ps3tools.shell import widgets
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

COLUMNS = ["File", "Size", "Title ID", "What it says it is"]

#: The same four, for what is already sitting on the console.
CONSOLE_COLUMNS = ["File", "Size", "Title ID", "What it says it is"]

CHAIN_NOTE = (
    "One package failing does not stop the others: every one you tick is "
    "tried, and each is reported on its own. Every package stays in the "
    "console's packages folder after it is installed, so anything that failed "
    "can be installed again from the list below without being sent across a "
    "second time. Nothing here deletes them; remove them from the console "
    "whenever you like.")

ON_CONSOLE_NOTE = (
    "These are already in the console's packages folder. Installing from here "
    "skips the copy entirely. This list is read from the console, so it finds "
    "what was left behind by a failed or interrupted install even after this "
    "program or the console has been restarted.")

FILE_FILTER = "PlayStation 3 packages (*.pkg)"

WHAT_THIS_IS = (
    "Copies package files from this computer to the console and installs "
    "them. Use it for extra content, for homebrew, or for a package somebody "
    "has given you.")

NO_HOST = (
    "Type the console's address into the box at the top of this window, or "
    "press Find my PS3 next to it. The console shows its own address in "
    "webMAN, and it usually starts 192.168.")


@register
class InstallPackagesScreen(Screen):
    """Upload .pkg files the user chose, and ask the console to install."""

    key = "packages"
    title = "Install packages"
    blurb = "Copies package files from this computer to the console."
    tile = "PK"
    # Beside the game updates card; both of them end at the same install call.
    order = 50

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        self._files = []
        self._packages = None
        self._task = None
        self._working = False
        self._panel_token = ""
        #: What the console's packages folder holds, as last read.
        self._console_rows = []
        #: True while the panel is showing the folder listing's own notice.
        #: It is the only thing the listing is allowed to clear.
        self._listing_owns_panel = False
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

        heading = QLabel("Install packages")
        font = heading.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)

        blurb = QLabel(WHAT_THIS_IS)
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        # The whole difference between this card and the game updates card, in
        # a frame of its own at the top where it cannot be scrolled past. It is
        # deliberately not a line in the paragraph above: the safety of the
        # other card must not be read across to this one by anybody.
        self._warning = QFrame()
        self._warning.setObjectName("warningnotice")
        warning = QVBoxLayout(self._warning)
        warning.setContentsMargins(16, 14, 16, 14)
        warning.setSpacing(0)
        self._warning_text = QLabel(updates.NO_WAY_TO_CHECK)
        self._warning_text.setWordWrap(True)
        warning_font = self._warning_text.font()
        warning_font.setBold(True)
        self._warning_text.setFont(warning_font)
        warning.addWidget(self._warning_text)
        layout.addWidget(self._warning)

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
        self._table.itemChanged.connect(lambda *args: self._update_go())
        layout.addWidget(self._table, 1)

        self._chain_note = QLabel(CHAIN_NOTE)
        self._chain_note.setWordWrap(True)
        layout.addWidget(self._chain_note)

        self._console_heading = QLabel("Already on the console")
        heading_font = self._console_heading.font()
        heading_font.setBold(True)
        self._console_heading.setFont(heading_font)
        layout.addWidget(self._console_heading)

        self._console_note = QLabel(ON_CONSOLE_NOTE)
        self._console_note.setWordWrap(True)
        layout.addWidget(self._console_note)

        self._console_table = QTreeWidget()
        self._console_table.setColumnCount(len(CONSOLE_COLUMNS))
        self._console_table.setHeaderLabels(CONSOLE_COLUMNS)
        self._console_table.setRootIsDecorated(False)
        self._console_table.setUniformRowHeights(True)
        self._console_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._console_table.setMaximumHeight(170)
        self._console_table.itemChanged.connect(
            lambda *args: self._update_install_here())
        layout.addWidget(self._console_table)

        self._install_here = QPushButton("Install the ticked packages")
        self._install_here.setEnabled(False)
        self._install_here.clicked.connect(self._on_install_here)
        layout.addWidget(self._install_here, 0, Qt.AlignRight)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._detail)

        self._stage = QLabel("")
        self._stage.setWordWrap(True)
        layout.addWidget(self._stage)

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
        self._add = QPushButton("Choose files")
        self._add.clicked.connect(self._on_add)
        buttons.addWidget(self._add)
        self._clear = QPushButton("Clear the list")
        self._clear.clicked.connect(self._on_clear)
        buttons.addWidget(self._clear)
        self._go = QPushButton("Copy to the console and install")
        widgets.set_role(self._go, widgets.PRIMARY)
        self._go.setEnabled(False)
        self._go.clicked.connect(self._on_go)
        buttons.addWidget(self._go)
        self._paint_go()
        layout.addLayout(buttons)
        self._paint_warning()

    # -- the seams. Every test replaces all of them; the suite reaches none.

    def _lister(self, host):
        return transport.FtpLister(host)

    def _writer(self, host):
        return FtpWriter(host)

    def _actions(self, host):
        return ConsoleActions(host)

    def _storage(self, host):
        """Free space per device, from the console's own status page.

        The read-only diagnostic client and the storage collector's own parser,
        so this card and the game updates card have one answer to how much room
        there is.
        """
        probe = transport.HttpProbe(host)
        response = probe.get("/")
        if not getattr(response, "ok", False):
            return []
        return parsers.parse_storage(parsers.html_to_text(response.body))

    def choose_files(self):
        """The file picker. Overridden in tests, which never open a dialog."""
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Choose package files", "", FILE_FILTER)
        return list(paths or [])

    # -- lifecycle

    def on_enter(self):
        self.check_console()

    def on_leave(self):
        if self._task is not None and not self._working:
            self._task.cancel()
            self._task = None

    def can_leave(self):
        return not self._working

    def leave_blocked_reason(self):
        return ("A package is part way through being copied to the console. "
                "Wait for this to finish: stopping now can leave a broken "
                "file in the console's packages folder.")

    # -- the console's packages folder, read before anything starts

    def check_console(self):
        """Read /dev_hdd0/packages and describe what is sitting in it.

        Two jobs. It says what is already there before anything is sent, and
        it is the recovery list: a package left behind by a failed install can
        be installed from here without being copied across again. The folder
        is read rather than remembered, so it survives a restart of this
        program or of the console.
        """
        host = self.connection.host if self.connection else ""
        if not host:
            self._show_panel("warn", "No console address has been entered yet",
                             NO_HOST)
            return None
        open_lister = self._lister

        def work(control):
            with open_lister(host) as lister:
                installed = updates.installed_title_ids(lister)
                rows, problem = updates.console_packages(lister, installed)
                return updates.inspect_packages_folder(lister), rows, problem

        task = self.submit(work)
        task.finished.connect(self._on_folder)
        task.failed.connect(self._on_failed)
        self._task = task
        return task

    def _on_folder(self, result):
        folder, rows, problem = result
        self._packages = folder
        self._console_rows = list(rows)
        self._fill_console_table()
        trouble = problem or (folder.reason if folder.unknown else "")
        if trouble:
            self._listing_owns_panel = True
            self._show_panel("warn", "The console's packages folder could not "
                                     "be read", trouble)
            return
        # This runs again straight after an install, so it may only clear a
        # notice it put up itself. Anything the run reported stays.
        if self._listing_owns_panel:
            self._listing_owns_panel = False
            self._hide_panel()

    # -- what is already on the console

    def _fill_console_table(self):
        self._console_table.blockSignals(True)
        self._console_table.clear()
        for item in self._console_rows:
            row = QTreeWidgetItem([item.filename, item.size_text,
                                   item.title_id or "", item.describes_as])
            if item.installed:
                row.setFlags(row.flags() & ~Qt.ItemIsUserCheckable)
                row.setText(3, f"{item.describes_as} (already installed)")
            else:
                row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
                row.setCheckState(0, Qt.Unchecked)
            row.setData(0, Qt.UserRole, item.filename)
            row.setData(1, Qt.UserRole, item.title_id or "")
            self._console_table.addTopLevelItem(row)
        for index in range(len(CONSOLE_COLUMNS)):
            self._console_table.resizeColumnToContents(index)
        self._console_table.blockSignals(False)
        count = len(self._console_rows)
        self._console_heading.setText(
            f"Already on the console: {count} package"
            f"{'' if count == 1 else 's'}" if count
            else "Already on the console: nothing in the packages folder")
        shown = bool(count)
        self._console_table.setVisible(shown)
        self._console_note.setVisible(shown)
        self._install_here.setVisible(shown)
        self._update_install_here()

    def _console_selection(self):
        chosen = []
        for index in range(self._console_table.topLevelItemCount()):
            row = self._console_table.topLevelItem(index)
            if row.flags() & Qt.ItemIsUserCheckable and \
                    row.checkState(0) == Qt.Checked:
                chosen.append((row.data(0, Qt.UserRole),
                               row.data(1, Qt.UserRole) or ""))
        return chosen

    def _update_install_here(self):
        self._install_here.setEnabled(
            bool(self._console_selection()) and not self._working)

    def _on_install_here(self):
        """Install what is already on the console. Nothing is copied across.

        This is the recovery path: the files are in the folder already, so the
        whole download and upload is skipped and the install call is made
        straight away.
        """
        chosen = self._console_selection()
        if not chosen:
            return None
        host = self.connection.host if self.connection else ""
        if not host:
            self._show_panel("warn", "No console address has been entered yet",
                             NO_HOST)
            return None
        self._working = True
        self._install_here.setEnabled(False)
        self._go.setEnabled(False)
        self._add.setEnabled(False)
        self._set_busy(True, "Installing on the console")

        open_actions = self._actions
        open_lister = self._lister
        poll_seconds = self.install_poll_seconds
        timeout_seconds = self.install_timeout_seconds

        def work(control):
            with open_lister(host) as confirm_lister:
                return updates.install_queue(
                    open_actions(host), chosen,
                    updates.installed_checker(confirm_lister),
                    on_progress=control.progress,
                    cancelled=lambda: control.cancelled,
                    poll_seconds=poll_seconds,
                    timeout_seconds=timeout_seconds)

        task = self.submit(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_installed_here)
        task.failed.connect(self._on_failed)
        task.done.connect(self._finished_working)
        self._task = task
        return task

    def _on_installed_here(self, results):
        landed = [item for item in results if item.confirmed]
        missed = [item for item in results if not item.confirmed]
        if missed:
            self._show_panel("warn", "Some packages did not install",
                             _install_report(landed, missed))
        else:
            count = len(landed)
            self._show_panel(
                "ok",
                f"{count} packages installed" if count > 1 else "Installed",
                f"{_and_list([item.filename for item in landed])} installed "
                f"on the console.")
        # Read the folder again so the list says what is actually there now.
        self.check_console()

    # -- choosing files

    def _on_add(self):
        paths = self.choose_files()
        if not paths:
            return None
        return self.add_files(paths)

    def add_files(self, paths):
        """Read each chosen file's header and put it in the table."""
        known = {item.path for item in self._files}
        for path in paths:
            if path in known:
                continue
            self._files.append(updates.read_package_file(path))
        self._fill_table()
        unusable = [item for item in self._files if not item.is_package]
        if unusable:
            self._detail.setText("\n\n".join(
                f"{item.filename}: {item.reason}" for item in unusable))
        else:
            self._detail.setText("")
        return self._files

    def _on_clear(self):
        self._files = []
        self._detail.setText("")
        self._fill_table()

    def _fill_table(self):
        self._table.blockSignals(True)
        self._table.clear()
        for item in self._files:
            row = QTreeWidgetItem([item.filename, item.size_text,
                                   item.title_id or "", item.describes_as])
            row.setData(0, Qt.UserRole, item.path)
            row.setToolTip(0, item.path)
            if item.reason:
                row.setToolTip(3, item.reason)
            if item.is_package:
                # Ticked when it arrives, unlike the game updates card. There
                # the program proposes the work and the user has not agreed to
                # any of it; here the user has already picked these files out
                # of a dialog, and unticking them the moment they appear would
                # make them do the same job twice.
                row.setCheckState(0, Qt.Checked)
            else:
                row.setFlags(row.flags() & ~Qt.ItemIsUserCheckable)
                brush = self._brush("error")
                if brush is not None:
                    row.setForeground(3, brush)
            self._table.addTopLevelItem(row)
        for index in range(len(COLUMNS)):
            self._table.resizeColumnToContents(index)
        self._table.blockSignals(False)
        self._update_go()

    def selected_files(self):
        out = []
        for index in range(self._table.topLevelItemCount()):
            row = self._table.topLevelItem(index)
            if row.checkState(0) != Qt.Checked:
                continue
            path = row.data(0, Qt.UserRole)
            for item in self._files:
                if item.path == path:
                    out.append(item)
                    break
        return out

    def _update_go(self):
        self._go.setEnabled(bool(self.selected_files()) and not self._working)

    # -- doing it

    def confirm(self, chosen):
        """Asked before anything is copied. Overridden in tests."""
        total = sum(item.size for item in chosen)
        lines = [
            f"{len(chosen)} package(s) will be copied to the console, "
            f"{parsers.human_size(total)} in total, and installed.",
            "",
            updates.NO_WAY_TO_CHECK,
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
        box.setWindowTitle("Copy to the console and install")
        box.setText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Yes

    def _on_go(self):
        chosen = self.selected_files()
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
        self._add.setEnabled(False)
        self._set_busy(True, "Copying to the console")

        open_writer = self._writer
        open_actions = self._actions
        open_lister = self._lister
        read_storage = self._storage
        poll_seconds = self.install_poll_seconds
        timeout_seconds = self.install_timeout_seconds

        def work(control):
            devices = read_storage(host)
            free = updates.free_bytes_for(devices)
            # Every file is weighed before any of them is sent, so a run that
            # cannot finish does not start and leave half of it on the console.
            updates.check_space(free, sum(item.size for item in chosen))
            sent = []
            with open_writer(host) as writer:
                for item in chosen:
                    if control.cancelled:
                        break
                    # Read again rather than trusting what the picker said. A
                    # file can be moved, renamed or deleted between choosing it
                    # and pressing the button, and a zero byte upload named
                    # after a package is worse than a refusal.
                    fresh = updates.read_package_file(item.path)
                    if not fresh.is_package:
                        raise updates.UpdateError(
                            f"{item.filename} could not be sent. "
                            f"{fresh.reason} Nothing else has been copied.")
                    remote, count = updates.upload_to_packages(
                        item.path, writer, filename=fresh.filename,
                        progress=control.progress, label=fresh.filename)
                    # The title ID travels with the upload: it is what the
                    # console is asked about afterwards to confirm the install
                    # landed before the next one is fired.
                    sent.append((fresh.filename, remote, count,
                                 getattr(item, "title_id", "") or ""))
            if not sent:
                return sent, []
            # Each install is confirmed on the console before the next is
            # fired. Firing them back to back drops them: webMAN ignores an
            # install while it is still busy with the last one.
            #
            # NEVER build this list by listing /dev_hdd0/packages: whatever
            # else is in there belongs to the user and is not ours to run or
            # to delete. Only what this upload just wrote, by exact name.
            with open_lister(host) as confirm_lister:
                results = updates.install_queue(
                    open_actions(host),
                    [(name, title)
                    for name, _remote, _count, title in sent],
                    updates.installed_checker(confirm_lister),
                    on_progress=control.progress,
                    cancelled=lambda: control.cancelled,
                    poll_seconds=poll_seconds,
                    timeout_seconds=timeout_seconds)
            return sent, results

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
        self._add.setEnabled(True)
        self._update_go()
        self._update_install_here()

    def _on_finished(self, result):
        sent, results = result
        if not sent:
            self._show_panel("warn", "Nothing was copied to the console",
                             "This was stopped before anything was copied "
                             "across.")
            return
        landed = [item for item in results if item.confirmed]
        missed = [item for item in results if not item.confirmed]
        if missed:
            self._show_panel("warn", "Some packages did not install",
                             _install_report(landed, missed))
            return
        count = len(landed)
        body = (f"{_and_list([item.filename for item in landed])} installed "
                f"on the console.")
        body += (" The package files stay in the console's packages folder. "
                 "Nothing here deletes them, and they can be removed from the "
                 "console whenever you like.")
        self._show_panel(
            "ok", f"{count} packages installed" if count > 1 else "Installed",
            body)
        self.check_console()

    def _on_failed(self, message):
        self._show_panel("error", "This did not finish", message)

    # -- progress

    def _on_progress(self, payload):
        if not isinstance(payload, tuple) or len(payload) < 4:
            return
        kind, label, done, total = payload[:4]
        if kind == "installing":
            self._stage.setText("Asking the console to install them")
            self._show_count("")
            self._bar.setRange(0, 0)
            return
        if kind == "upload":
            self._stage.setText(f"Copying {label} to the console")
            if total:
                self._bar.setRange(0, 100)
                self._bar.setValue(int(done * 100 / total))
                self._show_count(f"{parsers.human_size(done)} of "
                                 f"{parsers.human_size(total)}")

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

    def _paint_warning(self):
        accent = self._colour_name("warn") or self._colour_name("text")
        surface = self._colour_name("surface_alt") or \
            self._colour_name("surface")
        text = self._colour_name("text")
        if not (accent and surface and text):
            return
        self._warning.setStyleSheet(
            f"QFrame#warningnotice {{ background: {surface};"
            f" border: 1px solid {accent};"
            f" border-left: 4px solid {accent}; border-radius: 7px; }}"
            f"QFrame#warningnotice QLabel {{ color: {text};"
            f" background: transparent; border: none; }}")

    def _paint_count(self):
        dim = self._colour_name("text_dim")
        if dim:
            self._count.setStyleSheet(f"color: {dim};")

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
        self._paint_warning()
        if not self._panel.isHidden():
            self._paint_panel()

    def _on_back(self):
        if self.can_leave():
            self.request_home.emit()


def _install_report(landed, missed):
    """Which packages went in and which did not, naming both.

    A queue that stopped is reported in full. Somebody who sent seven and got
    three needs to know which three, and that the rest are sitting on the
    console waiting to be installed by hand.
    """
    lines = []
    if landed:
        lines.append(f"Installed: {_and_list([i.filename for i in landed])}.")
    unsent = [item for item in missed if "not sent" in item.reason]
    for item in missed:
        if item not in unsent and item.reason:
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
