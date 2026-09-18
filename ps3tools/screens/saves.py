"""Back up save data: list every user's saves, copy the chosen ones to a zip.

The screen is deliberately blunt about one thing before it is useful about
anything else. A tool that copies saves off a console reads, to anybody who has
used a PC backup program, as a tool that can put them back. It cannot. PS3
saves are normally signed against the console and the account that made them,
so copying the files on to a console gives a game a save it refuses to load.
That sentence is on the screen above the list, not behind a help link and not
in the small print underneath, because somebody who deletes a game trusting
this to restore it afterwards has been misled by the shape of the program
rather than by anything it said.

Everything the screen shows about what is on the console comes from
ps3tools.savedata, which holds no connection of its own. This module owns the
two places a connection is made -- make_lister and save_reader_for -- and both
are attributes on the screen so a test can replace them with something that
refuses to talk to anything. Nothing here calls either of them on the GUI
thread.

Progress is one bar per save folder rather than one for the lot. A single save
can be a hundred megabytes and take minutes over the console's FTP, and a bar
that sits at 3% for four minutes is indistinguishable from a program that has
frozen.

The scan does not look inside the save folders, so the sizes are not known
until a copy opens them. The screen says "not known yet" for those rather than
"0 bytes". The same honesty applies to the line above the list: a scan that
broke off is never summarised as a console with no saves on it.
"""

import os
import subprocess
import sys
import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel,
                               QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ps3diag.transport import DEFAULT_FTP_TIMEOUT, FtpLister

from ps3tools import savedata
from ps3tools.shell import widgets
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

# The heading nobody is allowed to miss. Short sentences, no jargon, and the
# consequence stated before the reason.
NO_RESTORE_HEADING = "This copies saves off the console. It cannot put them back."

NO_RESTORE_BODY = (
    "PS3 saves are normally locked to the console and to the account that "
    "made them. Copying these files back on to a PS3 does not restore them: "
    "the game will refuse a save it did not make. Keep this copy for "
    "safekeeping, and do not delete anything on the console expecting to put "
    "it back from here.\n\n"
    "Nothing on your console is changed, moved or deleted by this tool."
)

IDLE = "Press Find saves to see what is on the console."
NOT_CONNECTED = (
    "Type the console's address into the bar at the top of the window first, "
    "then press Find saves.")

CANNOT_REACH = (
    "Could not read the saves on the PS3 at {host}. Check that it is switched "
    "on, that it is showing the main menu rather than running a game, that "
    "webMAN is installed and running, and that it is plugged into the same "
    "router as this PC.")

# Said only when the console answered every question it was asked and the
# answer was genuinely nothing. A scan that broke off gets DID_NOT_FINISH
# instead: "no saves were found" is a statement about the console, and a scan
# that did not finish is not entitled to make one.
NOTHING_FOUND = ("No saves were found on this console. Anything worth saying "
                 "about why is listed below.")

DID_NOT_FINISH = (
    "The list of saves did not finish, so what is on this console is not "
    "known. This does not mean there are no saves on it. Check the console is "
    "switched on, showing the main menu rather than running a game, and "
    "plugged into the same router as this PC, then press Find saves again.")

PARTLY_DONE = (
    "Found {count} save folder(s) so far, but the list did not finish, so "
    "there may be more that are not shown. What is listed here is real and "
    "can be copied. Press Find saves again to try for the rest.")

# Shown in the size column for a folder nobody has opened yet. The survey no
# longer looks inside every save folder -- that was a dozen requests to a
# console that would only stand a few -- so the size is genuinely unknown
# until the copy opens it. "0 bytes" here would be a lie the user could act on.
SIZE_UNKNOWN = "Not known yet"

# Enough rows to see that the copy is working through the list rather than
# stuck on one folder, without the panel taking the whole window.
PROGRESS_HEIGHT = 220


def make_lister(host):
    """The one place in this screen a connection to the console is opened.

    An attribute on the screen rather than a call in the middle of the worker,
    so a test can put something that refuses in its place and prove the screen
    reaches the console through this and nowhere else.
    """
    return FtpLister(host, timeout=DEFAULT_FTP_TIMEOUT)


def save_reader_for(lister):
    """The callable that fetches one save file's bytes, or None.

    ps3diag.transport allowlists what may be pulled off a console, and save
    files are their own permission rather than a widening of the existing one.
    A build whose transport does not carry it returns None here, and the copy
    says plainly that it could not fetch anything rather than failing halfway
    through with something nobody can act on.
    """
    return getattr(lister, "download_save_bytes", None)


def human_bytes(count):
    """Sizes for somebody who has never seen a kibibyte."""
    count = float(count or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(count)} bytes"
            return f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GB"


def show_in_folder(path):
    """Opens the containing folder. Never raises: a convenience, not the job."""
    folder = path if os.path.isdir(path) else os.path.dirname(
        os.path.abspath(path))
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", os.path.normpath(folder)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except OSError:
        try:
            webbrowser.open(f"file://{folder}")
        except Exception:                                   # noqa: BLE001
            pass


class ProgressRow(QWidget):
    """One save folder's own bar, its name, and what happened to it."""

    def __init__(self, key, name, detail, theme, parent=None):
        super().__init__(parent)
        self.key = key
        self._theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 6)
        layout.setSpacing(2)

        self.name_label = QLabel(name)
        self.name_label.setWordWrap(True)
        layout.addWidget(self.name_label)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        layout.addWidget(self.bar)

        self.detail_label = QLabel(detail)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)
        self.repaint_theme()

    def repaint_theme(self, token="text_dim"):
        self.detail_label.setStyleSheet(
            f"color: {self._theme.colour(token)};")
        self.name_label.setStyleSheet(
            f"color: {self._theme.colour('text')};")

    def set_detail(self, text, token="text_dim"):
        self.detail_label.setText(text)
        self.repaint_theme(token)

    def set_fraction(self, done, total):
        self.bar.setValue(int(100 * done / total) if total else 100)


@register
class SavesScreen(Screen):
    key = "saves"
    title = "Back up save data"
    blurb = ("Copies your game saves off the console to this PC. It cannot "
             "put them back.")
    tile = "SV"
    # After both patchers and well before About. A user who has come to fix a
    # game is not looking for this, and a user looking for this knows it by
    # name.
    order = 60

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        # The two seams. Replaced wholesale by the tests; never called on the
        # GUI thread.
        self.lister_factory = make_lister
        self.reader_factory = save_reader_for
        #: Overridden by a test so nothing is written near a real Desktop.
        self.destination = None

        self.survey = None
        self.result = None
        self._scan_task = None
        self._copy_task = None
        self._stop = {"stop": False}
        self._rows = {}
        self._build()
        self.theme.changed.connect(self._repaint)

    # -- construction
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QLabel(self.title)
        heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(heading)

        layout.addWidget(self._warning_panel())

        controls = QHBoxLayout()
        self.find_button = QPushButton("Find saves")
        self.find_button.clicked.connect(self.start_scan)
        controls.addWidget(self.find_button)

        self.all_button = QPushButton("Tick everything")
        self.all_button.clicked.connect(lambda: self.set_all(True))
        self.all_button.setEnabled(False)
        controls.addWidget(self.all_button)

        self.none_button = QPushButton("Tick nothing")
        self.none_button.clicked.connect(lambda: self.set_all(False))
        self.none_button.setEnabled(False)
        controls.addWidget(self.none_button)

        controls.addStretch(1)

        self.copy_button = QPushButton("Copy the ticked saves to my Desktop")
        widgets.set_role(self.copy_button, widgets.PRIMARY)
        self.copy_button.clicked.connect(self.start_copy)
        self.copy_button.setEnabled(False)
        controls.addWidget(self.copy_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_copy)
        self.stop_button.setVisible(False)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)

        self.status_label = QLabel(IDLE)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Save", "Size"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree, 1)

        self.notes_label = QLabel("")
        self.notes_label.setWordWrap(True)
        self.notes_label.setVisible(False)
        layout.addWidget(self.notes_label)

        self.progress_host = QWidget()
        self.progress_layout = QVBoxLayout(self.progress_host)
        self.progress_layout.setContentsMargins(0, 0, 0, 0)
        self.progress_layout.setSpacing(0)
        self.progress_layout.addStretch(1)

        self.progress_area = QScrollArea()
        self.progress_area.setWidgetResizable(True)
        self.progress_area.setWidget(self.progress_host)
        self.progress_area.setFixedHeight(PROGRESS_HEIGHT)
        self.progress_area.setVisible(False)
        layout.addWidget(self.progress_area)

        finished = QHBoxLayout()
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setSizePolicy(QSizePolicy.Expanding,
                                        QSizePolicy.Preferred)
        finished.addWidget(self.result_label, 1)
        self.open_button = QPushButton("Open the folder")
        self.open_button.clicked.connect(self._open_folder)
        self.open_button.setVisible(False)
        finished.addWidget(self.open_button)
        layout.addLayout(finished)

        self._repaint()

    def _warning_panel(self):
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        inner = QVBoxLayout(panel)
        inner.setContentsMargins(12, 10, 12, 10)
        inner.setSpacing(4)
        self.warning_heading = QLabel(NO_RESTORE_HEADING)
        self.warning_heading.setWordWrap(True)
        self.warning_heading.setStyleSheet("font-weight: 600;")
        inner.addWidget(self.warning_heading)
        self.warning_body = QLabel(NO_RESTORE_BODY)
        self.warning_body.setWordWrap(True)
        inner.addWidget(self.warning_body)
        self.warning_panel = panel
        return panel

    def _repaint(self):
        colour = self.theme.colour
        self.warning_panel.setStyleSheet(
            f"QFrame {{ background: {colour('surface_alt')}; "
            f"border: 1px solid {colour('warn')}; border-radius: 6px; }}")
        self.warning_heading.setStyleSheet(
            f"color: {colour('warn')}; font-weight: 600; border: none;")
        self.warning_body.setStyleSheet(
            f"color: {colour('text')}; border: none;")
        self.status_label.setStyleSheet(f"color: {colour('text_dim')};")
        self.notes_label.setStyleSheet(f"color: {colour('text_dim')};")
        for row in self._rows.values():
            row.repaint_theme()

    # -- lifecycle
    def on_enter(self):
        # Deliberately does not scan on entry. The console may be mid-game and
        # a screen that starts talking to it the moment it is opened gives the
        # user no chance to decide otherwise.
        if not self.survey:
            self._set_status(IDLE)

    def on_leave(self):
        if self._scan_task is not None:
            self._scan_task.cancel()

    def can_leave(self):
        return self._copy_task is None

    def leave_blocked_reason(self):
        return ("The saves are still being copied. Wait for it to finish, or "
                "press Stop, so the file on your Desktop is complete.")

    # -- scanning
    def start_scan(self):
        host = (self.connection.host or "").strip()
        if not host:
            self._set_status(NOT_CONNECTED, "warn")
            return
        if self._scan_task is not None or self._copy_task is not None:
            return

        self._clear_progress()
        self.result = None
        self.result_label.setText("")
        self.open_button.setVisible(False)
        self._set_busy(True)
        self._set_status(f"Reading the saves on {host}. This can take a "
                         f"moment.", "info")

        factory = self.lister_factory

        def work(control):
            # Everything that touches the console happens here, on a worker.
            lister = factory(host)
            try:
                return savedata.survey(lister)
            finally:
                close = getattr(lister, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:                       # noqa: BLE001
                        pass

        task = self.submit(work)
        self._scan_task = task
        task.finished.connect(self._scan_done)
        task.failed.connect(self._scan_failed)
        task.done.connect(lambda: self._scan_ended(task))

    def _scan_ended(self, task):
        if self._scan_task is task:
            self._scan_task = None
        self._set_busy(False)

    def _scan_failed(self, message):
        self.survey = None
        self.tree.clear()
        self._enable_selection(False)
        host = self.connection.host or "the console"
        self._set_status(CANNOT_REACH.format(host=host), "error")
        self._show_notes([message])

    def _scan_done(self, survey):
        self.survey = survey
        self._populate(survey)
        self._set_status(*self._summary(survey))
        self._show_notes(survey.notes)

    @staticmethod
    def _summary(survey):
        """(what to say, which colour) about a finished scan.

        Three outcomes, not two. Nothing found on a scan that finished, a scan
        that did not finish at all, and a scan that found some and then broke
        off. The middle one used to be told as the first, which is how a
        console with four saves on it was told it had none.
        """
        count = survey.save_count
        if count and survey.complete:
            return (f"Found {count} save folder(s) across "
                    f"{len(survey.users)} user(s). Tick what you want and "
                    f"press Copy.", "ok")
        if count:
            return PARTLY_DONE.format(count=count), "warn"
        if survey.complete:
            return NOTHING_FOUND, "warn"
        return DID_NOT_FINISH, "error"

    def _populate(self, survey):
        self.tree.blockSignals(True)
        self.tree.clear()
        for user in survey.users:
            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, f"User {user.user_id}")
            parent.setText(1, self._size_text(user) if user.saves else "")
            parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable
                            | Qt.ItemIsAutoTristate)
            parent.setCheckState(0, Qt.Unchecked)
            parent.setData(0, Qt.UserRole, None)
            if user.note:
                note = QTreeWidgetItem(parent)
                note.setText(0, user.note)
                note.setFlags(Qt.ItemIsEnabled)
                note.setForeground(0, self._brush("text_dim"))
            for save in user.saves:
                child = QTreeWidgetItem(parent)
                child.setText(0, self._save_label(save))
                child.setText(1, self._size_text(save))
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked)
                child.setData(0, Qt.UserRole, save.key)
                if save.note:
                    child.setToolTip(0, save.note)
                    child.setForeground(0, self._brush("warn"))
            parent.setExpanded(True)
        self.tree.resizeColumnToContents(0)
        self.tree.blockSignals(False)
        self._enable_selection(bool(survey.save_count))
        self._update_copy_button()

    def _brush(self, token):
        return QBrush(QColor(self.theme.colour(token)))

    @staticmethod
    def _size_text(item):
        """A size, or a plain admission that nobody has looked yet."""
        return human_bytes(item.total_bytes) if item.size_known \
            else SIZE_UNKNOWN

    def _save_label(self, save):
        """What the row says. The game where it is known, and always the folder.

        The folder is shown even when the game is recognised, because it is
        what the console itself calls the save and it is the only way to tell
        two saves of the same game apart.
        """
        parts = [save.display_name]
        if save.named:
            parts.append(f"  (folder {save.folder})")
        elif save.title_id:
            # Recognised as a title ID, but nothing on this console says what
            # game it is. Say what little the ID does tell us and no more.
            where = save.region or "unknown region"
            parts.append(f"  (game not installed on this console, {where})")
        else:
            parts.append("  (this folder does not name a game)")
        if save.note:
            parts.append(f"  -- {save.note}")
        return "".join(parts)

    # -- selection
    def _item_changed(self, _item, _column):
        self._update_copy_button()

    def set_all(self, ticked):
        state = Qt.Checked if ticked else Qt.Unchecked
        self.tree.blockSignals(True)
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            parent.setCheckState(0, state)
            for position in range(parent.childCount()):
                child = parent.child(position)
                if child.data(0, Qt.UserRole) is not None:
                    child.setCheckState(0, state)
        self.tree.blockSignals(False)
        self._update_copy_button()

    def selected_keys(self):
        keys = []
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            for position in range(parent.childCount()):
                child = parent.child(position)
                key = child.data(0, Qt.UserRole)
                if key is not None and child.checkState(0) == Qt.Checked:
                    keys.append(key)
        return keys

    def selection(self):
        """The chosen SaveFolders, in the order they appear on screen."""
        if self.survey is None:
            return []
        chosen = set(self.selected_keys())
        return [save for save in self.survey.saves if save.key in chosen]

    def _enable_selection(self, enabled):
        self.all_button.setEnabled(enabled)
        self.none_button.setEnabled(enabled)

    def _update_copy_button(self):
        self.copy_button.setEnabled(
            bool(self.selected_keys()) and self._copy_task is None)

    # -- copying
    def start_copy(self):
        if self._copy_task is not None or self.survey is None:
            return
        chosen = self.selection()
        if not chosen:
            self._set_status(savedata.NOTHING_PICKED, "warn")
            return
        host = (self.connection.host or "").strip()
        if not host:
            self._set_status(NOT_CONNECTED, "warn")
            return

        self._build_progress(chosen)
        self.result = None
        self.result_label.setText("")
        self.open_button.setVisible(False)
        self._set_busy(True)
        self.copy_button.setEnabled(False)
        self.stop_button.setVisible(True)
        self.stop_button.setEnabled(True)
        self._set_status(f"Copying {len(chosen)} save folder(s). Leave the "
                         f"console switched on and on the main menu.", "info")

        factory = self.lister_factory
        reader_for = self.reader_factory
        destination = self.destination
        # Stop is this flag rather than Task.cancel, because a cancelled Task
        # emits done and not finished: the worker's partial result would be
        # thrown away and the user would be left with a half-filled zip on
        # their Desktop that the screen never mentioned. Task.cancel is still
        # honoured, for the shell shutting the screen down.
        stop = {"stop": False}
        self._stop = stop

        def work(control):
            lister = factory(host)
            try:
                return savedata.back_up(
                    lister, chosen, destination=destination,
                    save_reader=reader_for(lister),
                    progress=control.progress,
                    cancelled=lambda: stop["stop"] or control.cancelled)
            finally:
                close = getattr(lister, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:                       # noqa: BLE001
                        pass

        task = self.submit(work)
        self._copy_task = task
        task.progress.connect(self._copy_progress)
        task.finished.connect(self._copy_done)
        task.failed.connect(self._copy_failed)
        task.done.connect(lambda: self._copy_ended(task))
        self._update_copy_button()

    def stop_copy(self):
        """Asks the copy to stop. What is already in the zip stays in it.

        The worker notices at its next file, so the button says what it has
        done rather than pretending the transfer ended the instant it was
        pressed.
        """
        if self._copy_task is None:
            return
        self._stop["stop"] = True
        self.stop_button.setEnabled(False)
        self._set_status("Stopping after the file being copied now. What has "
                         "already been copied is kept.", "warn")

    def _copy_ended(self, task):
        if self._copy_task is task:
            self._copy_task = None
        self.stop_button.setVisible(False)
        self.stop_button.setEnabled(True)
        self._set_busy(False)
        self._update_copy_button()

    def _clear_progress(self):
        self._rows = {}
        while self.progress_layout.count() > 1:
            item = self.progress_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.progress_area.setVisible(False)

    def _build_progress(self, chosen):
        self._clear_progress()
        for save in chosen:
            # The folder has not been opened yet for most of these, so there
            # is nothing honest to say about its size until the copy gets to
            # it. "Waiting. 0 file(s), 0 bytes." would say the wrong thing.
            detail = (f"Waiting. {len(save.files)} file(s), "
                      f"{human_bytes(save.total_bytes)}." if save.listed
                      else "Waiting.")
            row = ProgressRow(save.key, save.display_name, detail, self.theme)
            self.progress_layout.insertWidget(self.progress_layout.count() - 1,
                                              row)
            self._rows[save.key] = row
        self.progress_area.setVisible(bool(self._rows))

    def _copy_progress(self, event):
        row = self._rows.get(event.get("key"))
        if row is None:
            return
        kind = event.get("kind")
        if kind == "save_start":
            row.set_detail(f"Copying {event.get('files', 0)} file(s), "
                           f"{human_bytes(event.get('bytes', 0))}.", "info")
        elif kind == "file":
            row.set_fraction(event.get("done_files", 0),
                             event.get("total_files", 0))
            row.set_detail(
                f"{event.get('done_files', 0)} of "
                f"{event.get('total_files', 0)} file(s), "
                f"{human_bytes(event.get('done_bytes', 0))} copied.", "info")
        elif kind == "save_done":
            note = event.get("note")
            if note:
                row.set_detail(note, "warn")
            else:
                row.bar.setValue(100)
                row.set_detail(f"Copied {event.get('copied', 0)} file(s).",
                               "ok")

    def _copy_failed(self, message):
        self._set_status("The saves could not be copied.", "error")
        self.result_label.setText(message)
        self.result_label.setStyleSheet(
            f"color: {self.theme.colour('error')};")

    def _copy_done(self, result):
        self.result = result
        if result.ok and result.cancelled:
            self._set_status(
                f"Stopped. {result.files_copied} file(s), "
                f"{human_bytes(result.bytes_copied)}, were copied before it "
                f"stopped and are in the file.", "warn")
            self.result_label.setText(
                f"Saved to {result.zip_path}\n"
                f"This is only the part that had been copied when you pressed "
                f"Stop. Run it again to take the rest.")
            self.result_label.setStyleSheet(
                f"color: {self.theme.colour('text')};")
            self.open_button.setVisible(True)
        elif result.ok:
            self._set_status(
                f"Copied {result.files_copied} file(s), "
                f"{human_bytes(result.bytes_copied)}.", "ok")
            self.result_label.setText(
                f"Saved to {result.zip_path}\n"
                f"manifest.txt inside the zip lists every file, where on the "
                f"console it came from and a checksum for it. Remember: these "
                f"cannot be copied back on to a PS3.")
            self.result_label.setStyleSheet(
                f"color: {self.theme.colour('text')};")
            self.open_button.setVisible(True)
        elif result.files_copied:
            # Something is on the Desktop, but not everything, and telling the
            # user "nothing was copied" would be a lie they would act on by
            # deleting a save that had in fact been taken.
            self._set_status(
                f"Only part of this was copied: {result.files_copied} "
                f"file(s), {human_bytes(result.bytes_copied)}.", "warn")
            self.result_label.setText(
                f"{result.zip_path}\n" + "\n".join(result.notes))
            self.result_label.setStyleSheet(
                f"color: {self.theme.colour('warn')};")
            self.open_button.setVisible(True)
        else:
            self._set_status("Nothing was copied.", "warn")
            self.result_label.setText("\n".join(result.notes)
                                      or "Nothing was copied.")
            self.result_label.setStyleSheet(
                f"color: {self.theme.colour('warn')};")
            self.open_button.setVisible(bool(result.zip_path))
        if result.notes:
            self._show_notes(result.notes)

    def _open_folder(self):
        if self.result is not None and self.result.folder:
            show_in_folder(self.result.folder)

    # -- small helpers
    def _set_status(self, text, token="text_dim"):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {self.theme.colour(token)};")
        self.status_message.emit(text)

    def _show_notes(self, notes):
        notes = [note for note in (notes or []) if note]
        self.notes_label.setText("\n\n".join(notes))
        self.notes_label.setVisible(bool(notes))

    def _set_busy(self, busy):
        self.find_button.setEnabled(not busy)
        self.busy_changed.emit(busy)
