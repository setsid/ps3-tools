"""The patcher screen, registered once per title.

One class does the work and two subclasses carry nothing but configuration.
There is no Black Ops II screen and no Modern Warfare 3 screen: the two titles
differ in a title key, a card heading and a tile, and everything else about
them lives in ps3tools.titles and ps3tools.patching.flow. A third card is four
lines here and an entry in the table.

The screen scans the moment it is entered, with no button to press. A user who
has come here to find out whether their game is patched should not have to work
out which control tells them, and the scan is read-only: it copies the files
down, decrypts them in a temporary folder and reads four bytes. Nothing is
written until the button is pressed and the warning is agreed to.

A patch is followed by a second scan of the console, so that what the table
shows afterwards has been read back off the hard drive rather than inferred
from what the patch set out to do. The screen stays blocked until that read
finishes: a stale table is how a user concludes the fix did nothing.

Nothing is called a success until that read-back has come back with every file
already fixed. The green panel and the restart notice are the screen saying
"this worked", and the only thing that entitles it to say so is having read the
files off the console afterwards. A patch that wrote its bytes and then could
not be confirmed says exactly that instead, and is not dressed up.

Undoing sits beside applying, on the same screen and the same row of buttons.
The backups on the Desktop were described everywhere as the only way back, and
the only way back was a hand-typed FTP session; a patcher whose undo needs a
command line is not one a non-technical person should be asked to run. The
restore takes the same route as the patch -- checked before anything is sent,
uploaded, read back off the console, then the table redrawn from what is
actually there -- and ends in the same restart notice, because the console goes
on running the module it loaded whichever direction the files moved in.

The fix is offered only on the build it was checked on. The change it makes is
at one exact place inside a binary and that place moves between versions, so
the installed title update is compared against the one somebody watched the fix
work on before Apply comes alive. That is a different question from whether the
update is the newest one, and a release nobody has verified has no answer to it
at all rather than a bad one.
"""

import os

from PySide6.QtCore import QMetaMethod, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ps3diag import transport
from ps3tools import titles
from ps3tools.patching import backup as backups
from ps3tools.patching import flow
from ps3tools.patching.ftpwrite import FtpWriter
from ps3tools.patching.scetool import Scetool
from ps3tools.shell import icons
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

# Imported defensively because the detection module and this screen are written
# in parallel. When it is not there the screen falls back to looking for the
# title IDs directly, which is a poorer answer: detect separates a title that
# is installed but has no update from one that is not installed at all.
try:
    from ps3tools import detect
except ImportError:
    detect = None

# Said word for word, because it is the difference between a fix that worked
# and a fix that looks like it bricked the game. The console keeps hold of the
# module it loaded, so the files written a moment ago do not take effect until
# it has been off and on again; a user who starts the game first sees a hang
# and concludes this program broke it.
RESTART_NOTICE = (
    "Restart your PlayStation 3 before launching the game. The patched files "
    "will not take effect until you do, and the game will hang on launch if "
    "you try it first.")

# Said only after the console has been read back and every file on it came back
# already fixed. Two sentences: what happened, and how this program knows.
# The same instruction, for the other direction. The console holds the module
# it loaded either way, so a restore needs the restart as much as a patch does
# -- and this is the one people will hit in a panic, having just watched a game
# fail to start, so it must not be left for them to work out.
RESTART_AFTER_RESTORE = (
    "Restart your PlayStation 3 before launching the game. The original files "
    "will not take effect until you do, and the game will hang on launch if "
    "you try it first.")

SUCCESS_HEADING = "The fix is on the console"
SUCCESS_BODY = (
    "The files were replaced and then read back off the console. Every one of "
    "them came back already fixed, so this is confirmed rather than assumed.")

STATE_TOKENS = {
    flow.PATCHED: "ok",
    flow.NOT_PATCHED: "warn",
    flow.UNRECOGNISED: "error",
    flow.NOT_EXAMINED: "warn",
    flow.CANNOT_DECRYPT: "warn",
    flow.NO_SITE: "text_dim",
}

STATE_WORDS = {
    flow.PATCHED: "already fixed",
    flow.NOT_PATCHED: "needs fixing",
    flow.UNRECOGNISED: "not recognised",
    # Deliberately not "not recognised". Nothing was read out of this file, so
    # the column must not suggest anything was found wrong with it.
    flow.NOT_EXAMINED: "not checked",
    # Also deliberately not "not recognised". The file was read and is exactly
    # what it should be; this tool has not got what it takes to open this
    # release, which is this tool's limit and not a fault in the file.
    flow.CANNOT_DECRYPT: "cannot be opened",
    flow.NO_SITE: "not affected",
}


# The heading, the colour and the advice for each way the search for an
# installation can end. Written out one state at a time on purpose: the screen
# this replaced said "This game is not installed on the console, or the console
# could not be reached" for every one of them, which is true of all of them and
# useful for none. Each entry is what the user is meant to do next.
#
# The token is the theme's, never a colour: see docs/screen-interface.md.


def _state_message(location, name):
    """(token, heading, body) for one location. The reason is added separately."""
    state = location.state
    title_id = location.title_id

    if state == flow.UNREACHABLE:
        return ("error", "The console did not answer", (
            "Nothing was read from the console and nothing has been changed on "
            "it.\n\n"
            "Check all four of these, then press Scan again:\n"
            "    the console is switched on;\n"
            "    it is sitting on its main menu and not inside a game;\n"
            "    webMAN is running on it;\n"
            "    it is connected to the same router as this computer."))

    if state == flow.LIST_FAILED:
        return ("error", "This program could not read the console's game list", (
            "This is a fault in this program, not anything you have done, and "
            "nothing is wrong with your console or your game.\n\n"
            "The console answered, but the list of installed games came back "
            "in a form this program could not make sense of, so it cannot say "
            "what is installed. Nothing has been read from your game and "
            "nothing has been changed on the console.\n\n"
            "Pressing Scan again is worth one try. If it says this a second "
            "time, this is one to report, with the line below."))

    if state == flow.NOT_INSTALLED:
        return ("text_dim", f"{name} is not installed on this console", (
            f"The console answered and its list of installed games was read. "
            f"There is no copy of {name} in it: no game installed to the hard "
            f"drive, no disc copy, and no title update.\n\n"
            f"This fix changes files that arrive with the game's title update, "
            f"so the game has to be installed and played once with the console "
            f"online before there is anything here to fix."))

    if state == flow.NO_UPDATE:
        return ("info", "The title update has not been downloaded yet", (
            f"Nothing is wrong. This is the usual thing to see the first time.\n\n"
            f"{name} is on the console{_as_id(title_id)}, but the fix changes "
            f"files that only arrive with the game's title update, and that "
            f"update has not been installed yet.\n\n"
            f"What to do: connect the console to the internet, start "
            f"{name} once, and let it download its update. That usually takes "
            f"a few minutes. Then come back here and press Scan again."))

    if state == flow.UNKNOWN_VARIANT:
        return ("warn", "This version of the game is not one this tool knows", (
            f"{title_id or 'The copy on the console'} is a Call of Duty "
            f"installation, but it is not one of the releases of the two "
            f"games this tool fixes. Every release of those two is known to "
            f"this tool by its title ID, and this is not one of them.\n\n"
            f"There is nothing here that could say what this game is, and the "
            f"settings needed to rebuild a file it knows nothing about are a "
            f"guess that produces a game that will not start at all. So "
            f"nothing will be read and nothing will be changed.\n\n"
            f"This is a refusal, not a failure: the copy on your console is "
            f"exactly as it was."))

    return ("text_dim", "Nothing to report", "")


def _all_fixed(report):
    """True only when the console has just been read and has nothing left wrong.

    Written as a whitelist rather than as a list of the states that would stop
    it. A file whose state this program has never heard of, or one that was
    never read at all, must fall out here rather than be counted as fixed by an
    omission; being wrong in that direction is telling a user a patch worked
    when nothing on the console says so.

    NO_SITE is allowed through because it is not a file the fix touches at all
    (MW3's default.self), and at least one file has to have come back patched:
    a report with nothing in it is not a confirmation of anything.
    """
    if report is None or not report.ok:
        return False
    if not any(item.state == flow.PATCHED for item in report.files):
        return False
    return all(item.state in (flow.PATCHED, flow.NO_SITE)
               for item in report.files)


def _listening(owner, name):
    """Whether anything is connected to a signal, by name. False if not sure.

    The shell decides which signals it wires, and this screen is written to be
    useful in a shell that has not wired this one. Asking is the difference
    between a button that leads somewhere and a button that silently does
    nothing.
    """
    try:
        meta = owner.metaObject()
        for index in range(meta.methodCount()):
            method = meta.method(index)
            if (method.methodType() == QMetaMethod.Signal
                    and bytes(method.name()).decode() == name):
                return owner.isSignalConnected(method)
    except (AttributeError, RuntimeError, TypeError, UnicodeDecodeError):
        return False
    return False


def _as_id(title_id):
    return f" as {title_id}" if title_id else ""


def _human(count):
    if not count:
        return ""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


class PatcherScreen(Screen):
    """Scan an installed title, and repair it once the user agrees."""

    #: which entry in ps3tools.titles this card is for
    title_key = ""

    #: ask the shell to open another tool, pre-filtered to one title.
    #: (screen key, title key). The shell owns navigation between screens and
    #: this screen must not reach into it; when nothing is connected the button
    #: falls back to request_home, which at least lands the user on the card.
    request_tool = Signal(str, str)

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        self.config = titles.TITLES.get(self.title_key, {})
        self._scan = None
        self._location = None
        self._panel_token = ""
        self._task = None
        self._writing = False
        # Set when a patch has just succeeded and the console has still to be
        # read back. Kept apart from _writing because the two refuse to leave
        # for different reasons and say different things when they do.
        self._read_back_wanted = False
        self._reading_back = False
        # The sentences the user has just earned. Held here rather than only in
        # the label, because the read-back rewrites that label and a result
        # that disappears a second after it arrives was never reported.
        self._patch_message = ""
        # What the last restore said, kept for the same reason as the line
        # above: the read-back that follows a restore redraws the label.
        self._restore_message = ""
        # The words the last scan put underneath, so that a restore refusal can
        # be added above them without throwing them away.
        self._scan_text = ""
        self._restoring = False
        self._restored_ok = False
        self._update_state = None
        # Where the backups are looked for. None means the user's Desktop,
        # which is the only answer in the built program; tests point it at a
        # folder of their own so that nothing reads a real Desktop.
        self._backup_root = None
        self._title_id = ""
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

        self._heading = QLabel(self.config.get("name", self.title))
        self._heading.setObjectName("heading")
        font = self._heading.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        self._heading.setFont(font)
        layout.addWidget(self._heading)

        self._symptom = QLabel(self.config.get("symptom", ""))
        self._symptom.setWordWrap(True)
        layout.addWidget(self._symptom)

        self._where = QLabel("")
        self._where.setWordWrap(True)
        layout.addWidget(self._where)

        # The green half of the answer, and the first thing on screen once a
        # patch has been read back. It is shown by the read-back and by nothing
        # else: writing the files is what this program did, and only the scan
        # that follows is evidence of what the console now has on it.
        self._success = QFrame()
        self._success.setObjectName("successpanel")
        success = QHBoxLayout(self._success)
        success.setContentsMargins(16, 14, 16, 14)
        success.setSpacing(12)
        self._success_icon = QLabel("")
        self._success_icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._success_icon.setFixedWidth(28)
        success.addWidget(self._success_icon)
        words = QVBoxLayout()
        words.setContentsMargins(0, 0, 0, 0)
        words.setSpacing(4)
        self._success_heading = QLabel(SUCCESS_HEADING)
        self._success_heading.setWordWrap(True)
        success_font = self._success_heading.font()
        success_font.setPointSize(success_font.pointSize() + 2)
        success_font.setBold(True)
        self._success_heading.setFont(success_font)
        words.addWidget(self._success_heading)
        self._success_body = QLabel(SUCCESS_BODY)
        self._success_body.setWordWrap(True)
        words.addWidget(self._success_body)
        success.addLayout(words, 1)
        self._success.hide()
        layout.addWidget(self._success)

        # The one thing a user must do after a successful patch, in a frame of
        # its own at the top of the screen. It is deliberately not a paragraph
        # in the detail text underneath: that text is several paragraphs long
        # by the time a patch has finished, and the console has to be restarted
        # before the game will start at all. A sentence buried three paragraphs
        # down is a sentence that gets skipped, and the user then sees the hang
        # this fix was meant to remove.
        self._restart = QFrame()
        self._restart.setObjectName("restartnotice")
        restart = QVBoxLayout(self._restart)
        restart.setContentsMargins(16, 14, 16, 14)
        restart.setSpacing(0)
        self._restart_text = QLabel(RESTART_NOTICE)
        self._restart_text.setWordWrap(True)
        restart_font = self._restart_text.font()
        restart_font.setPointSize(restart_font.pointSize() + 2)
        restart_font.setBold(True)
        self._restart_text.setFont(restart_font)
        restart.addWidget(self._restart_text)
        self._restart.hide()
        layout.addWidget(self._restart)

        # Why the fix is not being offered, and the way out of it. It sits
        # above the file table rather than replacing it, because the table is
        # still true: those really are the files, and this really is their
        # state. What has changed is only whether this program is willing to
        # write to them. The route to the other card is in the panel itself, so
        # that the instruction and the way to follow it are one thing.
        self._update = QFrame()
        self._update.setObjectName("updatenotice")
        update = QVBoxLayout(self._update)
        update.setContentsMargins(16, 14, 16, 14)
        update.setSpacing(6)
        self._update_heading = QLabel("")
        self._update_heading.setWordWrap(True)
        update_font = self._update_heading.font()
        update_font.setPointSize(update_font.pointSize() + 2)
        update_font.setBold(True)
        self._update_heading.setFont(update_font)
        update.addWidget(self._update_heading)
        self._update_body = QLabel("")
        self._update_body.setWordWrap(True)
        self._update_body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        update.addWidget(self._update_body)
        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
        self._updates_button = QPushButton("Go to Game updates")
        self._updates_button.clicked.connect(self._on_open_updates)
        row.addWidget(self._updates_button)
        row.addStretch(1)
        update.addLayout(row)
        self._update.hide()
        layout.addWidget(self._update)

        # The state of the console, said once and said loudly. It is the whole
        # answer in every case except a successful scan, so it sits above the
        # file table rather than below it, and the table is hidden when there
        # is nothing in it: an empty grid under a grey sentence reads as the
        # program having failed to do something rather than as an answer.
        self._panel = QFrame()
        self._panel.setObjectName("statepanel")
        panel = QVBoxLayout(self._panel)
        panel.setContentsMargins(16, 14, 16, 14)
        panel.setSpacing(8)
        self._panel_heading = QLabel("")
        self._panel_heading.setWordWrap(True)
        heading_font = self._panel_heading.font()
        heading_font.setPointSize(heading_font.pointSize() + 2)
        heading_font.setBold(True)
        self._panel_heading.setFont(heading_font)
        panel.addWidget(self._panel_heading)
        self._panel_body = QLabel("")
        self._panel_body.setWordWrap(True)
        self._panel_body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        panel.addWidget(self._panel_body)
        self._panel_reason = QLabel("")
        self._panel_reason.setWordWrap(True)
        self._panel_reason.setTextInteractionFlags(Qt.TextSelectableByMouse)
        panel.addWidget(self._panel_reason)
        self._panel.hide()
        layout.addWidget(self._panel)

        self._files = QTreeWidget()
        self._files.setColumnCount(4)
        self._files.setHeaderLabels(["File", "What it is", "Size", "State"])
        self._files.setRootIsDecorated(False)
        self._files.setUniformRowHeights(True)
        self._files.setSelectionMode(QAbstractItemView.NoSelection)
        self._files.setFocusPolicy(Qt.NoFocus)
        self._files.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._files, 1)

        # Takes the space the file table would have had while the table is
        # hidden, so the explanation sits under the heading it belongs to
        # rather than floating in the middle of an empty screen.
        self._filler = QWidget()
        self._filler.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._filler.hide()
        layout.addWidget(self._filler, 1)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._detail)

        self._stage = QLabel("")
        self._stage.setWordWrap(True)
        layout.addWidget(self._stage)

        # The shell styles every progress bar as a three pixel hairline, which
        # is the right look and leaves nowhere at all to draw text: the size
        # readout painted on the bar was clipped away entirely and the user
        # watched an unlabelled line creep across the screen. The count goes in
        # a label of its own, the same way the diagnostics screen solved it.
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
        self._rescan = QPushButton("Scan again")
        self._rescan.clicked.connect(lambda: self.start_scan())
        buttons.addWidget(self._rescan)
        # Beside Apply rather than in a card of its own. Undoing is the other
        # half of applying, it is wanted at the same moment and by the same
        # person, and a user looking for the way back will look where the way
        # forward was.
        self._restore = QPushButton("Put the originals back")
        self._restore.clicked.connect(self._on_restore)
        buttons.addWidget(self._restore)
        self._patch = QPushButton("Apply the fix")
        self._patch.setDefault(True)
        self._patch.setEnabled(False)
        self._patch.clicked.connect(self._on_patch)
        buttons.addWidget(self._patch)
        self._paint_patch_button()
        layout.addLayout(buttons)

    # -- lifecycle

    def on_enter(self):
        self.start_scan()

    def on_leave(self):
        if self._task is not None and not (self._writing or self._reading_back):
            self._task.cancel()
            self._task = None

    def can_leave(self):
        """Refused while a file is being written, and while the console is
        being read back afterwards.

        There is no safe moment to walk away from a half-uploaded game binary.
        The read-back is refused for a different reason: leaving partway
        through it and coming back would show the table as it was before the
        patch, which reads as the fix having done nothing at all.
        """
        return not (self._writing or self._reading_back)

    def leave_blocked_reason(self):
        if self._writing:
            return ("A file on the console is part written. Wait for this to "
                    "finish: switching away now can leave the game unable to "
                    "start.")
        return ("The fix has been applied and the console is being read back "
                "to confirm it. Wait for that to finish.")

    # -- the scan

    def start_scan(self):
        if self._writing:
            return None
        host = self.connection.host if self.connection else ""
        if not host:
            # The panel says it; repeating it underneath just reads as a
            # second, different problem.
            self._detail.setText("")
            self._show_state(
                "warn", "No console address has been entered yet",
                "Type the console's address into the box at the top of this "
                "window, or press Find my PS3 next to it. The console shows "
                "its own address in webMAN, and it usually starts 192.168.")
            self._files.clear()
            self._patch.setEnabled(False)
            self._restore.setEnabled(False)
            return None

        self._files.clear()
        # Anything a previous patch or restore said belongs to it, unless this
        # is the read-back of that very run.
        if not self._reading_back:
            self._patch_message = ""
            self._restore_message = ""
            self._restored_ok = False
            self._hide_success()
        self._scan_text = ""
        self._detail.setText(self._compose(""))
        self._hide_update()
        self._clear_state()
        self._patch.setEnabled(False)
        self._rescan.setEnabled(False)
        self._restore.setEnabled(False)
        self._set_busy(True, "Reading the console back"
                       if self._reading_back else "Looking at the console")
        title_key = self.title_key
        tool = self._scetool()

        lister = self._lister
        writer = self._writer

        def work(control):
            return _scan_console(host, title_key, tool, lister, writer,
                                 control.progress, control)

        task = self.submit(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_scanned)
        task.failed.connect(self._on_failed)
        task.done.connect(self._scan_done)
        self._task = task
        return task

    def _scan_done(self):
        """The screen is let go of here and nowhere else.

        A read-back holds the screen from the moment the last byte is written
        until the table has been redrawn from what the console actually has on
        it, so there is no window in which the user can walk away and come back
        to the pre-patch state.
        """
        self._reading_back = False
        self._back.setEnabled(True)
        self._set_busy(False)

    # The three seams. Tests replace all three: the bundled scetool is a
    # Windows binary, and the mock console listens on a port of its own rather
    # than on 21.
    def _scetool(self):
        return Scetool()

    def _lister(self, host):
        return transport.FtpLister(host)

    def _writer(self, host):
        return FtpWriter(host)

    def _on_scanned(self, result):
        location, report, where = result
        self._scan = report
        self._location = location
        self._task = None
        self._rescan.setEnabled(True)
        self._restore.setEnabled(True)
        self._where.setText(where)
        self._files.clear()
        if report is None:
            self._patch.setEnabled(False)
            self._hide_update()
            if self._reading_back:
                # The usual panel for this says nothing has been changed on the
                # console, and a moment ago something was. Say what happened
                # instead of reaching for the wrong sentence.
                self._read_back_failed(
                    location.reason if location is not None else "")
                return
            if location is not None:
                self._report_state(location)
            return
        self._clear_state()

        for item in report.files:
            row = QTreeWidgetItem([
                item.name,
                (item.record or {}).get("purpose", ""),
                _human(item.size),
                STATE_WORDS.get(item.state,
                                item.detail or "not in this folder")])
            token = STATE_TOKENS.get(item.state, "text_dim")
            colour = self._colour(token)
            if colour:
                row.setForeground(3, colour)
            row.setToolTip(3, item.detail)
            self._files.addTopLevelItem(row)
        for column in range(4):
            self._files.resizeColumnToContents(column)

        self._scan_text = self._verdict(report)
        self._detail.setText(self._compose(self._scan_text))
        # The gate. A fix confirmed on one build of the game is a fix for that
        # build, and offering it on another is how an install stops starting,
        # so the button is not enabled until the two agree.
        self._update_state = flow.update_check(report.title_id,
                                               self._installed_version())
        self._show_update(self._update_state)
        self._patch.setEnabled(report.can_patch
                               and not self._update_state.blocks)
        # The one place the screen is allowed to call a patch a success: a
        # read-back of a patch this session applied, which came back with every
        # file on the console already fixed. Anything else -- a file still
        # needing fixing, one this program could not recognise, one it never
        # managed to read -- leaves both the panel and the notice where they
        # are, hidden, and the wording underneath says what is actually known.
        if self._reading_back and self._patch_message and _all_fixed(report):
            self._show_success()
        # A restore needs the restart for the same reason a patch does: the
        # console is still running the module it loaded, so until it has been
        # off and on again the files just put back are not the ones in use.
        if self._reading_back and self._restored_ok:
            self._show_restart_notice(RESTART_AFTER_RESTORE)
        self.status_message.emit(
            f"{report.title_id}: " +
            (f"{len(report.to_patch)} file(s) to fix" if report.to_patch
             else "nothing to do"))

    # -- the state of the console, in words the user can act on

    def _report_state(self, location):
        """Say what was actually found. One heading and one colour per state."""
        name = self.config.get("name", self.title)
        token, heading, body = _state_message(location, name)
        reason = ""
        if location.reason:
            # Shown rather than swallowed. The user cannot fix it, but it is
            # the only thing they have to hand to somebody who can, and a
            # hidden reason is how a bug report becomes "it does not work".
            reason = f"What the console said: {location.reason}"
        notes = [note for note in location.notes if note]
        if notes and location.state in (flow.LIST_FAILED, flow.UNREACHABLE):
            reason = "\n\n".join([reason] + notes) if reason else "\n\n".join(notes)
        self._show_state(token, heading, body, reason)
        self._detail.setText(self._compose(""))
        self.status_message.emit(heading)

    def _show_state(self, token, heading, body, reason=""):
        self._panel_token = token
        self._panel_heading.setText(heading)
        self._panel_body.setText(body)
        self._panel_body.setVisible(bool(body))
        self._panel_reason.setText(reason)
        self._panel_reason.setVisible(bool(reason))
        self._paint_state()
        self._panel.show()
        # Nothing was read, so there are no rows. An empty table beside the
        # explanation only invites the user to wonder what should have been
        # in it.
        self._files.setVisible(False)
        self._filler.setVisible(True)

    def _clear_state(self):
        self._panel_token = ""
        self._panel.hide()
        self._files.setVisible(True)
        self._filler.setVisible(False)

    def _paint_state(self):
        accent = self._colour_name(self._panel_token) or self._colour_name("text")
        surface = self._colour_name("surface_alt") or self._colour_name("surface")
        border = self._colour_name("border") or accent
        text = self._colour_name("text")
        dim = self._colour_name("text_dim") or text
        if not accent or not surface:
            return
        self._panel.setStyleSheet(
            f"QFrame#statepanel {{ background-color: {surface};"
            f" border: 1px solid {border};"
            f" border-left: 6px solid {accent};"
            f" border-radius: 6px; }}")
        self._panel_heading.setStyleSheet(f"color: {accent}; border: none;")
        self._panel_body.setStyleSheet(f"color: {text}; border: none;")
        self._panel_reason.setStyleSheet(f"color: {dim}; border: none;")

    def _paint_success(self):
        """Green, in the theme's own ok, with the check mark drawn in it."""
        accent = self._colour_name("ok") or self._colour_name("text")
        surface = self._colour_name("surface_alt") or self._colour_name("surface")
        text = self._colour_name("text")
        if not (accent and surface and text):
            return
        self._success.setStyleSheet(
            f"QFrame#successpanel {{ background-color: {surface};"
            f" border: 1px solid {accent};"
            f" border-left: 6px solid {accent};"
            f" border-radius: 6px; }}")
        self._success_heading.setStyleSheet(f"color: {accent}; border: none;")
        self._success_body.setStyleSheet(f"color: {text}; border: none;")
        try:
            ratio = float(self.devicePixelRatioF())
        except (AttributeError, TypeError, ValueError):
            ratio = 1.0
        self._success_icon.setPixmap(icons.pixmap("check", accent, 22, ratio))

    def _paint_restart(self):
        """Loud on purpose, and in the theme's warning colour.

        This is an instruction rather than a fault, but it carries the same
        cost as one when it is missed: the game hangs and the user believes the
        patch did it.
        """
        accent = self._colour_name("warn") or self._colour_name("text")
        surface = self._colour_name("surface_alt") or self._colour_name("surface")
        if not (accent and surface):
            return
        self._restart.setStyleSheet(
            f"QFrame#restartnotice {{ background-color: {surface};"
            f" border: 2px solid {accent};"
            f" border-left: 8px solid {accent};"
            f" border-radius: 6px; }}")
        self._restart_text.setStyleSheet(f"color: {accent}; border: none;")

    def _paint_count(self):
        dim = self._colour_name("text_dim")
        if dim:
            self._count.setStyleSheet(f"color: {dim};")

    def _verdict(self, report):
        if report.error:
            return report.error
        lines = []
        if report.cannot_decrypt:
            # Said before anything else and in the plainest words available.
            # This is the release nobody has confirmed, found out at the only
            # point it can be found out, and the user has done nothing wrong.
            lines.append(
                f"This copy of the game cannot be opened by this tool, so "
                f"nothing will be changed. {report.title_id} is one of the "
                f"releases of this game that locks its files differently from "
                f"the ones the fix has been proved on, and without opening "
                f"them there is no way to fix them.\n\n"
                f"Nothing is wrong with your console, your game or your "
                f"connection, and the game is installed: it is this tool that "
                f"cannot do anything with this release. If you want to report "
                f"it, the thing to quote is {report.title_id}.")
        elif report.not_examined:
            # Says plainly whose fault it is. The sentence that used to appear
            # here described a file mismatch, which had the user checking a
            # console that was fine while the missing piece was in this build
            # of the program.
            missing = ", ".join(sorted(
                {item.missing_tool for item in report.not_examined
                 if item.missing_tool}))
            lines.append(
                "This program could not check "
                + ("these files" if len(report.not_examined) > 1
                   else report.not_examined[0].name)
                + ", so nothing will be changed. "
                + (f"{missing} is missing from this build of the program. "
                   if missing else "")
                + "That is a fault in this program and not in your console "
                  "or your game. Nothing here says anything is wrong with "
                  "your files.")
        elif report.unrecognised:
            lines.append(
                "One of these files is not one this fix was written for, so "
                "nothing will be changed. Sending a patch to the wrong build "
                "of a binary is how an install stops starting.")
        elif not report.to_patch:
            lines.append("Everything here is already fixed. Nothing to do.")
        else:
            names = ", ".join(item.name for item in report.to_patch)
            lines.append(f"{names} will be replaced. Your originals are "
                         f"copied to the Desktop first, and put back "
                         f"automatically if anything goes wrong.")
            lines.append(self.config.get("advice", ""))
        if not report.verified and not report.cannot_decrypt:
            if report.unrecognised:
                # The other way an unconfirmed release can turn out. The files
                # opened, so the key was right, but what is inside them is not
                # the build the fix was written against.
                lines.append(
                    f"{report.title_id} is a release nobody has confirmed "
                    f"this fix on yet, and this is where that shows: the "
                    f"files opened, but what is inside them is not the build "
                    f"the fix was written for. Quote {report.title_id} if you "
                    f"report this.")
            elif not report.not_examined:
                # The honest caveat, and it belongs under the plan rather than
                # over it: the scan has already opened the files and found
                # what it expected, so the fix suits this release as far as
                # anything can be checked from here. Nobody has simply watched
                # it work on this one yet.
                lines.append(
                    f"{report.title_id} is a release nobody has confirmed "
                    f"this fix on yet. Its files opened and their contents "
                    f"are what the fix expects, which is the check that "
                    f"matters, but you would be the first to report back on "
                    f"this release.")
        lines.extend(note for note in report.notes
                     if note not in lines)
        for item in report.files:
            if item.state in (flow.UNRECOGNISED, flow.NOT_EXAMINED):
                lines.append(f"{item.name}: {item.detail} "
                             f"(sha1 {item.sha1 or 'not read'})")
        return "\n\n".join(line for line in lines if line)

    # -- the patch

    def _on_patch(self):
        if self._scan is None or not self._scan.can_patch:
            return
        # The button is disabled in this case, so reaching here means something
        # else did. Refused rather than trusted: this is the one check standing
        # between a user and a patch built for another version of the game.
        if self._update_state is not None and self._update_state.blocks:
            return
        names = ", ".join(item.name for item in self._scan.to_patch)
        answer = QMessageBox.question(
            self, "Apply the fix",
            f"{names} on the console will be replaced.\n\n"
            f"Your original files are copied to the Desktop first and are put "
            f"back automatically if anything goes wrong. Close the game "
            f"completely before continuing: it is the running binary.\n\n"
            f"Do not switch the console off while this is happening.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        host = self.connection.host
        report = self._scan
        tool = self._scetool()
        self._writing = True
        self._read_back_wanted = False
        self._patch.setEnabled(False)
        self._rescan.setEnabled(False)
        self._back.setEnabled(False)
        self._set_busy(True, "Backing up your files")

        open_writer = self._writer

        def work(control):
            with open_writer(host) as writer:
                return flow.patch(writer, tool, report,
                                  progress=control.progress)

        task = self.submit(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_patched)
        task.failed.connect(self._on_failed)
        task.done.connect(self._finished_writing)
        self._task = task

    def _finished_writing(self):
        self._writing = False
        self._task = None
        if self._read_back_wanted:
            # Straight from the last byte written into a fresh scan, without
            # letting go of the screen in between. The table on it still
            # describes the files as they were before the patch, and the only
            # honest way to correct it is to read the console again: working
            # out what it ought to say from what the patch meant to do is how
            # a screen ends up confidently showing something that is not there.
            self._read_back_wanted = False
            self._reading_back = True
            if self.start_scan() is not None:
                return
            # No scan could be started at all, so there is nothing to wait for.
            self._reading_back = False
            self._read_back_failed("")
        self._restoring = False
        self._back.setEnabled(True)
        self._rescan.setEnabled(True)
        self._restore.setEnabled(True)
        self._set_busy(False)

    def _on_patched(self, result):
        lines = []
        if result.changed:
            lines.append("Changed: " + ", ".join(result.changed) + ".")
            lines.append(f"Your original files are in {result.folder}. Keep "
                         f"them: they are the only way back.")
            lines.append("The patched files are re-signed with the original "
                         "NPDRM control block and application type, so they "
                         "boot the way the originals did and do not need any "
                         "extra custom firmware controls switched on. If one "
                         "of them does not start, put the originals back: "
                         "they are the only way to undo this.")
        if result.error:
            lines.append(result.error)
        if result.restored:
            lines.append("Put back: " + ", ".join(result.restored) + ".")
        lines.extend(result.notes)
        message = "\n\n".join(line for line in lines if line)
        # Both halves are required. A run that came back without an error but
        # replaced nothing has not patched anything, and must not be followed
        # by an instruction to restart the console for files that were never
        # written.
        worked = bool(result.ok and result.changed)
        if worked:
            # Held as well as shown. The read-back that follows redraws this
            # label, and the list of files that were changed is the one thing
            # on the screen the user has been waiting for.
            #
            # Nothing is said about success here, and nothing tells the user to
            # restart the console yet. All that is known at this point is that
            # this program wrote what it meant to write; whether the console
            # has it is the read-back's answer to give, a second from now.
            self._patch_message = message
        else:
            self._patch_message = ""
        self._detail.setText(message)
        self.status_message.emit(
            "Finished" if result.ok else "Nothing was changed")
        # A failed or rolled-back attempt is left on screen saying so, rather
        # than having its message replaced by a second run of the scan.
        self._read_back_wanted = worked

    def _show_success(self):
        """The green panel, and then the restart notice under it.

        In that order and never one without the other: the panel is the good
        news and the notice is the price of it, and a user who reads the first
        and misses the second gets the hang this whole screen exists to remove.
        """
        self._success_heading.setText(SUCCESS_HEADING)
        self._success_body.setText(SUCCESS_BODY)
        self._paint_success()
        self._success.show()
        self._show_restart_notice()

    def _show_restart_notice(self, text=RESTART_NOTICE):
        self._restart_text.setText(text)
        self._paint_restart()
        self._restart.show()

    def _hide_success(self):
        self._success.hide()
        self._restart.hide()

    def _read_back_failed(self, reason=""):
        """The patch worked; the confirming read did not.

        These are two different statements and the screen must not let them be
        heard as one. The user's files have been changed and the backup is
        where the message above says it is; what is missing is only this
        program's confirmation of it.
        """
        # The report on hand describes the files as they were before the patch
        # and nothing has replaced it, so it is dropped rather than left for a
        # theme change or a repaint to put back on screen.
        self._scan = None
        self._location = None
        self._patch.setEnabled(False)
        # Neither of these has been earned. The files were written, but nothing
        # has been read back off the console, and "it worked" is a claim only
        # that read can make.
        self._hide_success()
        self._hide_update()
        self._files.clear()
        self._files.setVisible(False)
        self._filler.setVisible(True)
        lines = [
            "The fix was applied, but the console could not be read back "
            "afterwards to confirm it. That is not the same as the fix having "
            "failed: the files named above were written, and your original "
            "files are where the message above says they are.",
            "Press Scan again once the console is answering, and this screen "
            "will show the state of the files as they are now."]
        if reason:
            lines.append(f"What the console said: {reason}")
        self._detail.setText(self._compose("\n\n".join(lines)))
        self.status_message.emit("Applied, but not read back")

    # -- the title update the fix was verified against

    def _installed_version(self):
        """The APP_VER the detection step read, or None if it could not be.

        Read from the installation the search already produced rather than
        fetched again. There is exactly one reader of PARAM.SFO in this
        program, in ps3tools.detect, and a second one here would be a second
        answer to a question that must only have one.
        """
        installation = getattr(self._location, "installation", None)
        return getattr(installation, "tu_version", None)

    def _update_words(self, check):
        """(heading, body) for a title update that is not the verified one."""
        name = self.config.get("short", self.config.get("name", self.title))
        if check.verdict == flow.UPDATE_DIFFERS:
            return (
                "This copy of the game is not the version the fix was "
                "checked on",
                f"The fix for {name} was checked on update {check.verified}, "
                f"and this console has update {check.installed} installed. "
                f"The change it makes is at one exact place inside the game's "
                f"files, and that place moves between versions, so applying "
                f"it to a different version is how an install stops "
                f"starting.\n\n"
                f"Nothing is wrong with your console or your game, and "
                f"nothing has been changed. Install update "
                f"{check.verified} for {name}, come back here and press Scan "
                f"again, and the fix will be offered normally.")
        return (
            "This program could not tell which version of the game is "
            "installed",
            f"The fix for {name} was checked on update {check.verified}. The "
            f"console did not give up the version it has installed, so this "
            f"program cannot confirm you are on that one.\n\n"
            f"Nothing is wrong with your game. The files themselves are read "
            f"and checked before anything is written, and a version the fix "
            f"does not fit is refused at that point, so the fix is still "
            f"offered. If you know the game has not been updated, install "
            f"update {check.verified} first.")

    def _show_update(self, check):
        """The panel, but only where this program has something it can stand on.

        Never shown for a release with no verified update of its own. There is
        no expected version for one of those, so there is nothing the installed
        version can disagree with, and saying "your update is out of date"
        would be a claim about a table that has no entry for this release. That
        case is already answered, in its own words, as the release nobody has
        confirmed.
        """
        if check is None or check.verdict in (flow.UPDATE_MATCHES,
                                              flow.UPDATE_NOT_ESTABLISHED):
            self._hide_update()
            return
        heading, body = self._update_words(check)
        self._update_heading.setText(heading)
        self._update_body.setText(body)
        self._paint_update()
        self._update.show()

    def _hide_update(self):
        self._update.hide()

    def _paint_update(self):
        accent = self._colour_name("warn") or self._colour_name("text")
        surface = self._colour_name("surface_alt") or self._colour_name("surface")
        text = self._colour_name("text")
        if not (accent and surface and text):
            return
        self._update.setStyleSheet(
            f"QFrame#updatenotice {{ background-color: {surface};"
            f" border: 1px solid {accent};"
            f" border-left: 6px solid {accent};"
            f" border-radius: 6px; }}")
        self._update_heading.setStyleSheet(f"color: {accent}; border: none;")
        self._update_body.setStyleSheet(f"color: {text}; border: none;")

    def _on_open_updates(self):
        """Hand the user to the Game updates card, on this title.

        Navigation belongs to the shell. This asks for it and does not reach
        for it, and if nothing is listening it falls back to the launcher
        rather than leaving a button that does nothing at all.
        """
        if not self.can_leave():
            return
        if _listening(self, "request_tool"):
            self.request_tool.emit("updates", self.title_key)
            return
        self.request_home.emit()

    # -- putting the originals back

    def _backups(self, title_id):
        """Every backup of this title on the Desktop, newest first."""
        return backups.find(title_id, root=self._backup_root)

    def _choose_backup(self, found):
        """Which one to put back. The newest unless the user says otherwise.

        Overridden in tests so that nothing here opens a modal dialog on a
        machine with no display.
        """
        if len(found) == 1:
            return found[0]
        labels = [item.label for item in found]
        chosen, agreed = QInputDialog.getItem(
            self, "Which backup?",
            "There is more than one backup of this game. The most recent is "
            "at the top.", labels, 0, False)
        if not agreed:
            return None
        return found[labels.index(chosen)] if chosen in labels else None

    def _backup_contents(self, checks):
        """The files, their sizes, their dates and whether each one checks out."""
        lines = []
        for row in checks:
            when = row["modified"].strftime("%d %B %Y at %H:%M") \
                if row["modified"] else "date unknown"
            state = ("checked and unchanged" if row["ok"]
                     else row["reason"] or "cannot be checked")
            lines.append(f"    {row['name']}  -  {_human(row['size'])}  -  "
                         f"{when}  -  {state}")
        return "\n".join(lines)

    def _confirm_restore(self, chosen, checks):
        """The last word before anything is sent. Overridden in tests."""
        return QMessageBox.question(
            self, "Put the originals back",
            f"These files will be copied back onto the console, over the "
            f"patched ones:\n\n{self._backup_contents(checks)}\n\n"
            f"They are from {chosen.folder}, taken on {chosen.taken_text}, "
            f"and every one of them has been checked against the record "
            f"written when the copy was made.\n\n"
            f"This is safe. Your backup is not changed by this and you can "
            f"apply the fix again afterwards whenever you like. Close the "
            f"game completely before continuing.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _say_restore(self, text, status=""):
        """Put a restore's answer above whatever the scan is saying."""
        self._restore_message = text
        self._detail.setText(self._compose(self._scan_text))
        self.status_message.emit(status or "Nothing has been put back")

    def _on_restore(self):
        if self._writing or self._reading_back:
            return
        title_id = (self._scan.title_id if self._scan is not None
                    else (self._location.title_id if self._location else ""))
        if not title_id:
            self._say_restore(
                "This program does not know yet which game is on the console, "
                "so it cannot tell which backup belongs to it. Press Scan "
                "again first. Nothing has been sent to the console.")
            return

        if not os.path.isdir(backups.root_folder(self._backup_root)):
            self._say_restore(
                f"There are no backups on this computer yet, so there is "
                f"nothing to put back.\n\n"
                f"Every time this program applies the fix it copies your "
                f"original files to a folder called "
                f"\u201c{backups.FOLDER_NAME}\u201d on the Desktop first. "
                f"That folder is not there, which means the fix has not been "
                f"applied from this computer. Nothing has been changed.")
            return

        found = self._backups(title_id)
        if not found:
            self._say_restore(
                f"There is no backup of {title_id} on this computer, so there "
                f"is nothing to put back for this game.\n\n"
                f"The backups this program makes are in a folder called "
                f"\u201c{backups.FOLDER_NAME}\u201d on the Desktop, and each "
                f"one is named after the game it came from. If you made a "
                f"backup on another computer, copy that folder into this one "
                f"and press this again. Nothing has been changed.")
            return

        chosen = self._choose_backup(found)
        if chosen is None:
            return

        # Asked before the user is offered anything, so that a backup that
        # cannot be trusted is refused with its reason rather than confirmed
        # and then refused. flow.restore asks again on its own account: this is
        # the screen being polite, that is the rule being enforced.
        reason = flow.restore_refusal(chosen, title_id)
        if reason:
            self._say_restore(reason, "Nothing has been put back")
            return

        checks = backups.verify(chosen)
        if not self._confirm_restore(chosen, checks):
            return

        host = self.connection.host
        self._writing = True
        self._restoring = True
        self._read_back_wanted = False
        self._restored_ok = False
        self._patch.setEnabled(False)
        self._rescan.setEnabled(False)
        self._restore.setEnabled(False)
        self._back.setEnabled(False)
        self._hide_success()
        self._hide_update()
        self._set_busy(True, "Putting your original files back")

        open_writer = self._writer

        def work(control):
            with open_writer(host) as writer:
                return flow.restore(writer, chosen, title_id,
                                    progress=control.progress)

        task = self.submit(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_restored)
        task.failed.connect(self._on_failed)
        task.done.connect(self._finished_writing)
        self._task = task

    def _on_restored(self, result):
        lines = []
        if result.restored:
            lines.append("Put back: " + ", ".join(result.restored) + ".")
            lines.append(
                "Each file was read back off the console afterwards and came "
                "back matching the backup, so this is confirmed rather than "
                "assumed.")
            lines.append(
                f"Your backup in {result.folder} has not been changed. You "
                f"can apply the fix again whenever you like.")
        if result.error:
            lines.append(result.error)
        lines.extend(result.notes)
        message = "\n\n".join(line for line in lines if line)
        worked = bool(result.ok and result.restored)
        self._restore_message = message
        if worked:
            # Whatever the last patch said is now describing files that are no
            # longer on the console.
            self._patch_message = ""
        self._restored_ok = worked
        self._detail.setText(message)
        self.status_message.emit(
            "The originals are back on the console" if worked
            else "Nothing has been put back")
        # Same rule as a patch: what the table says next is read off the
        # console, never worked out from what this program set out to do.
        self._read_back_wanted = worked

    def _compose(self, text):
        """The last thing the user did, first. Then whatever this scan adds."""
        return "\n\n".join(
            line for line in (self._restore_message, self._patch_message, text)
            if line)

    # -- shared

    def _on_progress(self, event):
        if not isinstance(event, dict):
            return
        self._stage.setText(event.get("message", ""))
        sent, size = event.get("bytes"), event.get("of")
        if sent and size:
            self._bar.setRange(0, 100)
            self._bar.setValue(int(100 * sent / size))
            self._show_count(
                f"{int(100 * sent / size)}%  -  "
                f"{_human(sent)} of {_human(size)}")
            return
        total = event.get("total") or 0
        done = event.get("done") or 0
        if total:
            self._bar.setRange(0, total)
            self._bar.setValue(done)
            self._show_count(f"{done} of {total}")

    def _show_count(self, text):
        """Never on the bar itself: it is three pixels tall and clips text."""
        self._count.setText(text)
        self._paint_count()
        self._count.setVisible(bool(text))

    def _on_failed(self, message):
        self._task = None
        self._rescan.setEnabled(True)
        self._restore.setEnabled(True)
        self._patch.setEnabled(False)
        if self._reading_back:
            self._read_back_failed(message)
            return
        if self._restoring:
            # Said separately from the patch case below. "Nothing has been
            # changed" is the wrong sentence here: a restore that stopped may
            # have put some of the files back already, and the user has to be
            # told to run it again rather than left thinking it did nothing.
            self._restore_message = (
                f"Putting the originals back stopped: {message}. Your backup "
                f"folder has not been changed and is still complete. Check "
                f"the console is switched on with webMAN running, then press "
                f"Put the originals back again: doing it a second time is "
                f"safe.")
            self._detail.setText(self._compose(self._scan_text))
            self.status_message.emit("Stopped")
            return
        if self._writing:
            # Mid-write, the panel would cover the file table that says which
            # files have been touched, which is the thing to read first.
            self._detail.setText(
                f"Something went wrong that this program did not expect: "
                f"{message}. Nothing has been changed on the console.")
        else:
            self._show_state(
                "error", "This program stopped with a fault of its own",
                "Something went wrong here that this program did not expect, "
                "so it stopped. Nothing has been read from your game and "
                "nothing has been changed on the console.",
                f"What went wrong: {message}")
        self.status_message.emit("Stopped")

    def _on_back(self):
        if self.can_leave():
            self.request_home.emit()

    def _set_busy(self, busy, message=""):
        self._bar.setVisible(busy)
        if message:
            self._stage.setText(message)
        if not busy:
            self._stage.setText("")
            self._show_count("")
        self.busy_changed.emit(busy)

    def _paint_patch_button(self):
        """The only control here that changes anything on the console.

        It spent most of its life greyed out, so when it does come alive it has
        to look unmistakably different from the state it was in a second ago.
        The disabled half is left deliberately flat and dim: a user should be
        able to tell at a glance that pressing it would do nothing.
        """
        accent = self._colour_name("accent")
        on_accent = self._colour_name("accent_text")
        border = self._colour_name("border")
        dim = self._colour_name("text_dim")
        surface = self._colour_name("surface_alt")
        if not (accent and on_accent and border and dim and surface):
            return
        self._patch.setStyleSheet(
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

    def _colour(self, token):
        try:
            name = self.theme.colour(token)
        except (AttributeError, NotImplementedError, KeyError):
            return None
        return QBrush(QColor(name)) if name else None

    def _repaint(self):
        self._paint_patch_button()
        self._paint_count()
        if not self._success.isHidden():
            self._paint_success()
        if not self._restart.isHidden():
            self._paint_restart()
        if not self._update.isHidden():
            self._paint_update()
        if self._writing:
            return
        if self._panel_token:
            self._paint_state()
        if self._scan is not None:
            self._on_scanned((self._location, self._scan, self._where.text()))


def _scan_console(host, title_key, tool, open_lister, open_writer, progress,
                  control):
    """Find the installation and scan it. Runs on a worker, never on the GUI.

    Detection is a read, so it goes through the read-only transport. The write
    client is opened afterwards and only for the files themselves, which keeps
    every writing command in one short stretch of one run.

    Returns (location, report, where). The location is carried out whole rather
    than reduced to "found or not found": the screen has a different heading,
    a different colour and different advice for each way this can end, and none
    of that can be recovered once the answer has become a boolean.
    """
    progress({"stage": "find", "message": "looking for the game on the console",
              "done": 0, "total": 1})
    detector = detect.find_installations if detect is not None else None
    with open_lister(host) as lister:
        location = flow.locate(lister, title_key, detector=detector)

    if control.cancelled:
        return None, None, ""
    if not location.ready:
        return location, None, ""

    title_id = location.title_id
    where = f"{title_id} in {titles.usrdir_for(title_id)}"
    if len(location.title_ids) > 1:
        where += (" (more than one copy is installed; the first was used: " +
                  ", ".join(location.title_ids) + ")")
    with open_writer(host) as writer:
        report = flow.scan(writer, tool, title_id, progress=progress)
    return location, report, where


@register
class BlackOpsTwoPatcher(PatcherScreen):
    title_key = "bo2"
    key = "bo2"
    title = "Black Ops II patch"
    blurb = ("Stops the freeze that happens whenever a PSN session becomes "
             "active.")
    tile = "B2"
    order = 20


@register
class ModernWarfareThreePatcher(PatcherScreen):
    title_key = "mw3"
    key = "mw3"
    title = "Modern Warfare 3 patch"
    blurb = ("Stops multiplayer lobbies dropping you back to the menu on a "
             "newer PSN account.")
    tile = "M3"
    order = 30
