"""The patcher screen, registered once per title.

One class does the work and the subclasses carry configuration. There is no
Black Ops II screen and no Modern Warfare 3 screen: those two differ in a title
key, a card heading and a tile, and everything else about them lives in
ps3tools.titles and ps3tools.patching.flow. A card like that is four lines here
and an entry in the table.

Black Ops 1 is the one that needed more, and what it needed is four methods
rather than a second screen. Its fix has to know which local user is signed in,
because the account ID it hashes is that user's, and it has to put a readable
copy of that user's np_cache.dat where the game can open it. scan_extras,
on_scan_extras, patch_ready and patch_extras are where that goes, and every
other title inherits a default that does nothing.

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
already fixed. The green panel and the dialogue telling somebody to restart the
console are the screen saying "this worked", and the only thing that entitles
it to say so is having read the files off the console afterwards. A patch that
wrote its bytes and then could not be confirmed says exactly that instead, and
is not dressed up.

Undoing sits beside applying, on the same screen and the same row of buttons.
The backups on the Desktop were described everywhere as the only way back, and
the only way back was a hand-typed FTP session; a patcher whose undo needs a
command line is not one a non-technical person should be asked to run. The
restore takes the same route as the patch -- checked before anything is sent,
uploaded, read back off the console, then the table redrawn from what is
actually there -- and ends in the same dialogue, because the console goes on
running the module it loaded whichever direction the files moved in.

Two dialogues carry the whole of what a user has to do about that module, one
before the screen does anything and one after the files have moved, and both
of them are built once on the base class. The rest of what this screen used to
say above the file table is now a verdict line under the game's name and one
expander. An evening was lost to a screen that had all of it on the page at
once: the sentence that would have saved the evening was there, among the
others, and was read past.

The fix is offered only on the build it was checked on. The change it makes is
at one exact place inside a binary and that place moves between versions, so
the installed title update is compared against the one somebody watched the fix
work on before Apply comes alive. That is a different question from whether the
update is the newest one, and a release nobody has verified has no answer to it
at all rather than a bad one.

A release this tool has never seen reads as a warning here. The files are read
and checked before a byte is written and the fix finds its own patch site by
the code around it, so an unfamiliar title ID costs the user a sentence saying
so. The screen says it again on the line naming the folder it is about to write
to and in the box that asks for the last word, because those are what somebody
reads on the way to pressing Apply.

What is still refused is a folder whose files could belong to more than one
game, where nothing on the console says which of them it is. A screen that
guessed there offered a Black Ops 1 user Ghosts, then Modern Warfare 2, and
they deleted game data chasing it. Nothing on this screen suggests deleting
anything, and where the search finds no folder for this game the way out
offered is the user saying where it is.
"""

import os
import shutil
import tempfile

from PySide6.QtCore import QMetaMethod, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QDialogButtonBox, QFrame,
                               QGraphicsBlurEffect, QHBoxLayout,
                               QListWidget,
                               QInputDialog, QLabel, QMessageBox, QProgressBar,
                               QSizePolicy,
                               QPushButton, QSizePolicy, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ps3diag import parsers, transport
from ps3tools import titles
from ps3tools.patching import backup as backups
from ps3tools.patching import flow
from ps3tools.patching import npcache
from ps3tools.patching.ftpwrite import FtpWriter

#: webMAN's own page, which names the firmware outright.
FIRMWARE_PAGE = "/cpursx.ps3"

#: Asked only when the console did not name its own firmware. The two forms a
#: file can take are not interchangeable, and signing for the wrong one gives
#: a game that will not start, so this says what it is for.
FIRMWARE_PROMPT = (
    "This console did not say which firmware it is running, which is "
    "unusual.\n\nThe patched file is signed differently for each, and the "
    "wrong one gives a game that will not start, so it is worth being sure.")
from ps3tools.patching.signer import Signer
from ps3tools.shell import icons
from ps3tools.shell import widgets
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

# The same defence, for the same reason: the console reader and this screen
# are being written in parallel. read_page hands over webMAN's own pages as
# text, which is where the title ID of whatever the console has open appears.
# Where they are not there yet the seam below answers "nothing is running",
# which is the only safe direction: a reader this screen cannot reach must
# never turn into a refusal to patch.
try:
    from ps3tools.shell.consolestats import read_page_text as read_page
except ImportError:
    read_page = None

# The first thing on the screen, before the scan and before the console is
# read at all. It is a dialogue rather than a paragraph because of what it
# costs to miss it.
#
# The console loads a game's module when the game starts and keeps hold of it.
# Patching a game the console has loaded writes the files to the hard drive
# and changes nothing whatsoever about what is running, and restarting the
# game does not shake the module loose either: the console has to be restarted
# for that. Somebody spent an entire evening on this. The game was running on
# the console the whole time, every file came back "already fixed" on every
# scan, and the fault he was patching out sat there unchanged all evening.
# Nothing on the screen was wrong. The sentence that would have saved the
# evening was on it, among the other sentences, and he read past it.
BEFORE_NOTICE = (
    "Before you start, make sure the game has not been run since the console "
    "was last switched on. If you have played it, restart your PlayStation 3 "
    "and leave it sitting on the XMB.")

# The other half of the same fact, after the files have moved. The console
# goes on running the module it loaded whichever direction they moved in, so a
# restore needs this as much as a patch does, and this is the one people reach
# in a panic having just watched a game fail to start.
#
# One sentence. Its predecessor was a three-line framed panel saying the same
# thing twice, which is a panel people learn the shape of and stop reading.
RESTART_NOTICE = ("Now fully restart your PlayStation 3 before running "
                  "the game.")

#: How far the screen behind a dialogue is blurred. Far enough that the words
#: under it stop being readable, which is the whole point: both of these say
#: something that has to be done before the next thing happens, and a dialogue
#: over a screen somebody can still read is one they dismiss while reading
#: past it.
NOTICE_BLUR = 9

# A release the title table does not name, said as the warning it is. The
# table decides which releases somebody has watched the fix work on, and it
# decides nothing else: the binaries and the key come from the game, and the
# patch site is found by the code around it, so a title ID this tool has not
# written down is a reason to say so and nothing more. This used to be a
# refusal, and it refused people whose game the fix would have suited.
#: The fact in one line, for the places the screen names the release it is
#: about to write to. There was a framed panel of four paragraphs here as
#: well. It said at length what this says in a sentence, it sat between the
#: user and the state of their own game, and the state of their own game is
#: what they came for, so it is now a clause on the verdict line instead.
UNTESTED_LINE = "This release has not been tested."

#: What the box asking for the folder says. The example is a real title ID
#: shape, because a person who has never typed a console path needs to see one
#: before they can produce one.
FOLDER_PROMPT = (
    "Type the folder this game is installed in on the console, for example "
    "/dev_hdd0/game/BLES01032.\n\n"
    "The files in it are read and checked before anything is written, and the "
    "fix is refused if they are not the ones it expects. Your originals are "
    "copied to the Desktop first, the same as on any other release.")

#: A folder somebody typed that the console does not have. Kept apart from
#: flow's own states, which are answers to where this game is; this is an
#: answer to whether that is the path they meant.
TYPED_MISSING = "typed_folder_missing"

#: Where the console keeps installed games, and the only place a typed folder
#: can be. The fix reads the files out of that folder's USRDIR afterwards.
GAME_FOLDER = "/dev_hdd0/game"

SUCCESS_HEADING = "The fix is on the console"
SUCCESS_BODY = (
    "The files were replaced and then read back off the console. Every one of "
    "them came back already fixed, so this is confirmed rather than assumed.")

# The verdict line, one form for each way a console can stand. It goes under
# the game's name and above everything else on the screen.
#
# Until now the answer to "is my game fixed" was a small green "already fixed"
# in the fourth column of a table, under a stack of banners. Somebody read
# that whole screen and then asked for the feature it had already told him he
# had. The information was there and the screen was arranged so that it was
# not the thing he saw.
VERDICT_FIXED = "{name} on this console is already fixed. Nothing to do."
VERDICT_NOT_FIXED = "{name} on this console has not been fixed yet."
VERDICT_MIXED = "Some of {name}'s files are fixed and some are not."

# The two cases that have no answer to give about the fix at all. Said on the
# same line rather than in a panel of their own, because a user reading down
# the screen should find the state of their game in one place whatever that
# state turns out to be.
VERDICT_STRANGE = ("{name} on this console has files that are not what the "
                   "fix expects.")
VERDICT_UNREAD = "{name} on this console could not be checked."

#: Added to whichever line above applies. Both of these were a framed
#: paragraph further down the page, which is the same fault the line itself
#: exists to fix.
VERDICT_SOME_STRANGE = "Some of its files are not what the fix expects."
VERDICT_UNTESTED = "Nobody has reported back on this release yet."

#: The colour each verdict is drawn in, as a theme token and never a colour.
VERDICT_TOKENS = {
    VERDICT_FIXED: "ok",
    VERDICT_NOT_FIXED: "warn",
    VERDICT_MIXED: "warn",
    VERDICT_STRANGE: "error",
    VERDICT_UNREAD: "warn",
}

# Said when the console has this screen's own game loaded. It names the
# release so that somebody with two copies installed can see which one the
# console means, and it says both halves of what has to happen: quitting the
# game is not enough on its own, because the module stays loaded until the
# console restarts.
RUNNING_NOTICE = (
    "{name} is running on the console now, as {title_id}. Nothing can be "
    "patched while the console has the game loaded: the files would be "
    "replaced on the hard drive and the console would carry on running the "
    "copy it already has.\n\n"
    "Quit the game, restart your PlayStation 3, leave it sitting on the XMB "
    "and press Scan again.")

STATE_TOKENS = {
    flow.PATCHED: "ok",
    flow.NOT_PATCHED: "warn",
    flow.UNRECOGNISED: "error",
    flow.NOT_EXAMINED: "warn",
    flow.CANNOT_DECRYPT: "warn",
    flow.REPLACED: "error",
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
    # The file opened and holds one of this game's other binaries. Said as
    # what it is rather than as "not recognised", because this one was
    # recognised, as something else.
    flow.REPLACED: "replaced",
    flow.NO_SITE: "not affected",
}


# The heading, the colour and the advice for each way the search for an
# installation can end. Written out one state at a time on purpose: the screen
# this replaced said "This game is not installed on the console, or the console
# could not be reached" for every one of them, which is true of all of them and
# useful for none. Each entry is what the user is meant to do next.
#
# The token is the theme's, never a colour: see docs/screen-interface.md.


def _wrapping(widget, vertical=QSizePolicy.Policy.Minimum):
    """Let a word-wrapped widget ask a layout for the height it needs.

    setWordWrap on its own is not enough inside a layout with something
    stretchy in it: the label's own minimum is one line, so that is what it
    gets and the rest of the sentence is drawn outside its box.
    """
    if isinstance(widget, QLabel):
        widget.setWordWrap(True)
    policy = QSizePolicy(QSizePolicy.Policy.Preferred, vertical)
    policy.setHeightForWidth(True)
    widget.setSizePolicy(policy)
    return widget


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
            "This is a fault in this program. Nothing is wrong with your "
            "console or your game, and you have not done anything to cause "
            "it.\n\n"
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
            f"online before there is anything here to fix.\n\n"
            f"If the game is on the console and this search has missed "
            f"it, the button below takes the folder it is in. The files in "
            f"it are checked before anything is written."))

    if state == TYPED_MISSING:
        typed = getattr(location, "typed_path", "") or "That folder"
        return ("warn", "That folder is not on the console", (
            f"{typed} was not there when the console was asked for it, so "
            f"nothing has been read and nothing has been changed.\n\n"
            f"Check the path against what webMAN or an FTP client shows, and "
            f"give the folder the game is installed in under {GAME_FOLDER}, "
            f"for example {GAME_FOLDER}/BLES01032. The button below asks "
            f"again."))

    if state == flow.NO_UPDATE:
        return ("info", "The title update has not been downloaded yet", (
            f"Nothing is wrong. This is the usual thing to see the first "
            f"time.\n\n"
            f"{name} is on the console{_as_id(title_id)}, but the fix changes "
            f"files that only arrive with the game's title update, and that "
            f"update has not been installed yet.\n\n"
            f"Game updates can fetch it and put it on the console for you. "
            f"The button below goes straight there with {name} picked "
            f"out.\n\n"
            f"The other way round is to connect the console to the internet, "
            f"start {name} once and let it download its own update, which "
            f"usually takes a few minutes. Either way, come back here and "
            f"press Scan again afterwards."))

    # All that is left of what used to be the refusal for every title ID this
    # tool had not written down. A folder whose files name one game is now
    # offered as an untested release of it; this is the folder whose files
    # could belong to several, where the honest answer is that it cannot tell.
    if state == flow.UNKNOWN_VARIANT:
        return ("warn", "This tool cannot tell which game this folder holds", (
            f"{title_id or 'The folder on the console'} holds files that "
            f"could belong to more than one game, and its title ID is not one "
            f"this tool has written down, so there is nothing here that says "
            f"which game this is.\n\n"
            f"A fix meant for one game and applied to another is a game that "
            f"stops starting, so nothing has been read out of these files and "
            f"nothing on the console has been changed. The copy on your "
            f"console is exactly as it was."))

    return ("text_dim", "Nothing to report", "")


# Small counts read as words in a sentence. Anything larger is a title this
# tool does not handle today and the digits will do.
COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
               6: "six"}


def _count_word(number):
    return COUNT_WORDS.get(number, str(number))


def _and_list(parts):
    """"a", "a and b", "a, b and c". Used in sentences shown to the user."""
    parts = [str(part) for part in parts if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


#: Why one file is being left as it is, in the words the plan uses.
# No title this tool handles has more than three files. The cap is here for
# the day one does, so that a longer table stops growing rather than pushing
# the buttons off the bottom of the screen.
MOST_FILE_ROWS = 6

# Qt's own row hint is only available once there is a row in the table. The
# fallback matches what a delegate leaves round a line of text.
ROW_PADDING = 8


LEFT_ALONE = {
    flow.PATCHED: "{name} is already fixed",
    flow.UNRECOGNISED: "{name} is not a build this fix was written for",
    flow.REPLACED: "{name} has been replaced with another of the game's "
                   "binaries",
    flow.CANNOT_DECRYPT: "{name} could not be opened",
    flow.NOT_EXAMINED: "{name} could not be checked by this program",
}


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


def _fix_state(report):
    """The files of a report, sorted into what the verdict line says of them.

    Returns (fixed, stock, strange, unread). NO_SITE is deliberately in none
    of the four: it is a file the fix does not touch at all, MW3's default.self
    among them, and counting it anywhere would have the line describe a file
    nobody is being asked to decide about.

    A state this program has never heard of falls out of all four as well, so
    a verdict is only ever given on files that were actually recognised.
    """
    fixed, stock, strange, unread = [], [], [], []
    for item in report.files:
        if item.state == flow.PATCHED:
            fixed.append(item)
        elif item.state == flow.NOT_PATCHED:
            stock.append(item)
        elif item.state in (flow.UNRECOGNISED, flow.REPLACED):
            strange.append(item)
        elif item.state in (flow.CANNOT_DECRYPT, flow.NOT_EXAMINED):
            unread.append(item)
    return fixed, stock, strange, unread


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

    #: Every patcher is in the same section of the home screen, set here so
    #: that a fourth one is in it without being told. It is the one section
    #: whose tools write to a console.
    group = "fixes"

    #: What a tick box beside Apply has to say before Apply will do anything,
    #: for a fix that is wrong for some consoles and right for others. Empty
    #: for a screen whose fix suits everybody its scan will offer it to, which
    #: is most of them.
    CONFIRM_WITH = ""

    #: Framed notes under the one-line summary, in the order they are shown.
    #: Each is (body, emphasis, colour token). The emphasis is drawn bold and
    #: in the token's colour, so the half that decides what somebody does is
    #: the half that catches the eye; pass "" for a note with no such half.
    #:
    #: Only a warning that changes what somebody does belongs here. Every one
    #: of these costs the verdict line above it some of its weight, and the
    #: screen this replaced had so many of them that the state of the user's
    #: own game was off the bottom of what anybody read. Anything worth having
    #: and not worth that goes in BACKGROUND.
    #:
    #: Empty for a screen with nothing to add, which is most of them.
    NOTICES = ()

    #: What the expander holds beyond the fault this fix addresses. Each is
    #: (body, emphasis), joined into one paragraph. True and worth keeping,
    #: and none of it changes what somebody does next, so none of it earns a
    #: frame between the user and the answer they came for.
    BACKGROUND = ()

    #: (words, link) naming whoever the fix was built from, shown under the
    #: one line summary. On the screen rather than only in the About page,
    #: because somebody using a fix somebody else worked out should be able to
    #: see whose work it is without going looking. Empty for a fix that is
    #: this program's own throughout.
    CREDIT = ()

    #: The one line on the expander. The same on all three screens, because
    #: what is behind it is the same kind of thing on all three.
    MORE_SUMMARY = "More about this fix"

    #: which entry in ps3tools.titles this card is for
    title_key = ""

    #: ask the shell to open another tool, pre-filtered to one title.
    #: (screen key, title key). The shell owns navigation between screens and
    #: this screen must not reach into it; when nothing is connected the button
    #: falls back to request_home, which at least lands the user on the card.
    request_tool = Signal(str, str)

    #: tell the shell something finished, for the line on the connection bar.
    #: Optional in the same way request_tool is: a shell that has not wired it
    #: simply has no record of the run, and the screen is unaffected.
    event_noted = Signal(str)

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        self.config = titles.TITLES.get(self.title_key, {})
        #: What webMAN said this console is running, and the host it was read
        #: from, so a different console is asked again rather than inheriting
        #: the last one's answer.
        self._firmware_kind = ""
        self._firmware_line = ""
        self._firmware_host = ""
        #: What the user said, where the console did not. Empty until they
        #: answer, and Apply waits for it.
        self._firmware_choice = ""
        self._scan = None
        #: File names the user has taken the tick out of. Held here rather
        #: than on the scan, which is rebuilt every time the console is read.
        self._declined = set()
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
        #: Which installed release the user picked, when more than one of them
        #: is on the console. Empty until they have been asked.
        self._release = ""
        #: The folder on the console the user typed, for a game the search did
        #: not find. Empty for every scan that found the game itself.
        self._folder = ""
        #: The release of this screen's own game the console had loaded when
        #: the last scan started, or "". Read again on every scan rather than
        #: kept per host: unlike the firmware, this is the one fact on the
        #: screen that the user is being asked to change.
        self._running = ""
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

        # Directly under the game's name, above every banner and above the
        # table, and large enough that it is the thing somebody reads first.
        # It is hidden until a scan has actually read the console: a verdict
        # on a game nobody has looked at yet is a guess with a big font.
        self._verdict_line = QLabel("")
        self._verdict_line.setWordWrap(True)
        verdict_font = self._verdict_line.font()
        verdict_font.setPointSize(verdict_font.pointSize() + 3)
        verdict_font.setBold(True)
        self._verdict_line.setFont(verdict_font)
        self._verdict_token = ""
        self._verdict_line.hide()
        layout.addWidget(self._verdict_line)

        # One line saying what the fix does, and nothing else. The card's own
        # blurb, so the sentence on the tile somebody pressed to get here is
        # the sentence waiting for them when they arrive. The paragraphs that
        # used to be here describe the fault at length and are in the
        # expander below.
        self._symptom = QLabel(self.blurb or self.config.get("symptom", ""))
        self._symptom.setWordWrap(True)
        layout.addWidget(self._symptom)

        # Whose work this fix came from, under the line that says what it
        # does. A link rather than a name alone, so somebody can go and look
        # at the work rather than take this program's word for whose it is.
        self._credit = None
        if self.CREDIT:
            words, link = self.CREDIT
            label = QLabel(f'{words} <a href="{link}">{link}</a>'
                           if link else words)
            label.setWordWrap(True)
            label.setObjectName("dim")
            label.setOpenExternalLinks(True)
            label.setToolTip(f"Open {link} in your browser." if link else "")
            layout.addWidget(label)
            self._credit = label

        # Under the explanation, because it is about the game rather than
        # about this program, and above everything else, because somebody who
        # needs it needs it before they start reading a table.
        # One framed note per entry, under the explanation, because they are
        # about the game rather than about this program and somebody who
        # needs them needs them before they start reading a table.
        self._notices = []
        for body, emphasis, token in self.NOTICES:
            frame = QFrame()
            frame.setObjectName("sidenotice")
            box = QVBoxLayout(frame)
            box.setContentsMargins(16, 12, 16, 12)
            box.setSpacing(6)
            words = _wrapping(QLabel(body))
            box.addWidget(words)
            loud = _wrapping(QLabel(emphasis))
            loud_font = loud.font()
            loud_font.setBold(True)
            loud.setFont(loud_font)
            loud.setVisible(bool(emphasis))
            box.addWidget(loud)
            # Otherwise the table below takes the stretch and squeezes this to
            # one line, which cuts the second one through the middle of its
            # letters. A word-wrapped QLabel reports the height of a single
            # line until it has been laid out, and a layout that believes it
            # never gives it the room to be more than that.
            _wrapping(frame, QSizePolicy.Policy.Minimum)
            layout.addWidget(frame)
            self._notices.append((frame, words, loud, token))
        # Painted here as well as on a theme change. The other panels on this
        # screen are hidden until something shows them and are painted at that
        # moment; these are on the screen from the start and would otherwise
        # be unstyled rectangles until the user changed theme.
        self._paint_notice()

        # Everything that was explained at length above the table, behind one
        # line and closed. It is here to be found rather than to be read, and
        # its height is what pushed the state of the user's own game off the
        # part of the screen anybody looks at.
        self._more = widgets.Disclosure(self.MORE_SUMMARY)
        self._more.setText(self.more_about_this_fix())
        layout.addWidget(self._more)

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

        # The console has this game loaded right now, which is the one state
        # in which applying the fix is guaranteed to achieve nothing. Said
        # here rather than only in a refusal at the button, because the whole
        # point is that somebody finds out before they spend an evening on it.
        self._running_notice = QFrame()
        self._running_notice.setObjectName("runningnotice")
        running = QVBoxLayout(self._running_notice)
        running.setContentsMargins(16, 14, 16, 14)
        running.setSpacing(0)
        self._running_text = _wrapping(QLabel(""))
        self._running_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        running.addWidget(self._running_text)
        _wrapping(self._running_notice, QSizePolicy.Policy.Minimum)
        self._running_notice.hide()
        layout.addWidget(self._running_notice)

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
        # Shown for the one state where this program can do the next step
        # itself. Sending somebody off to put the console online and launch
        # the game is the slow way round something this app already does.
        panel_row = QHBoxLayout()
        panel_row.setContentsMargins(0, 4, 0, 0)
        self._panel_button = QPushButton("Get the title update")
        self._panel_button.clicked.connect(self._on_open_updates)
        self._panel_button.hide()
        panel_row.addWidget(self._panel_button)
        # Shown where the search came back with no folder for this game. The
        # user may well know where it is, and what was offered before this
        # existed was nothing at all.
        self._folder_button = QPushButton("Enter the folder myself")
        self._folder_button.clicked.connect(self._on_type_folder)
        self._folder_button.hide()
        panel_row.addWidget(self._folder_button)
        panel_row.addStretch(1)
        panel.addLayout(panel_row)
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
        self._files.itemChanged.connect(self._on_file_ticked)
        self._fit_files(len(self.config.get("binaries", ())))
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

        # Its own label so that the rest of the wording above stays plain
        # text. One link in a block that is otherwise built by hand out of
        # file names and console output is a block that has to escape all of
        # it, and a title with an ampersand in it would come out wrong.
        self._next_step = QLabel("")
        self._next_step.setWordWrap(True)
        self._next_step.setTextFormat(Qt.RichText)
        self._next_step.setOpenExternalLinks(True)
        self._next_step.hide()
        layout.addWidget(self._next_step)

        self._stage = QLabel("")
        # Room for four lines. It was cut off at three, mid-sentence.
        widgets.fit_progress_label(self._stage)
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

        # A tick box for a screen whose fix is only right for some accounts,
        # on the row with Apply so it is read on the way to pressing it. Built
        # for every screen and shown on the ones that ask for it, because a
        # widget that only exists on one subclass is a widget every other
        # method has to check for.
        buttons = QHBoxLayout()
        self._confirm = QCheckBox(self.CONFIRM_WITH)
        self._confirm.setVisible(bool(self.CONFIRM_WITH))
        self._confirm.toggled.connect(self._on_confirmed)
        self._back = QPushButton("Back")
        self._back.clicked.connect(self._on_back)
        buttons.addWidget(self._back)
        buttons.addStretch(1)
        self._rescan = QPushButton("Scan again")
        # A fresh scan asks again which release to work on. Keeping the answer
        # would leave somebody with two copies installed no way to change
        # their mind short of leaving the screen.
        self._rescan.clicked.connect(self._on_rescan)
        buttons.addWidget(self._rescan)
        # Beside Apply rather than in a card of its own. Undoing is the other
        # half of applying, it is wanted at the same moment and by the same
        # person, and a user looking for the way back will look where the way
        # forward was.
        self._restore = QPushButton("Put the originals back")
        # Red for the same reason Disconnect is: nothing bad happens, but
        # it writes over what is on the console now and should not be hit
        # by accident on the way to Apply.
        widgets.set_role(self._restore, widgets.DANGER)
        self._restore.clicked.connect(self._on_restore)
        buttons.addWidget(self._restore)
        buttons.addWidget(self._confirm)
        self._patch = QPushButton("Apply the fix")
        widgets.set_role(self._patch, widgets.PRIMARY)
        self._patch.setDefault(True)
        self._patch.setEnabled(False)
        self._patch.clicked.connect(self._on_patch)
        buttons.addWidget(self._patch)
        self._paint_patch_button()
        layout.addLayout(buttons)

    # -- lifecycle

    def on_enter(self):
        # First, and before anything at all is read off the console. A screen
        # that starts scanning behind the dialogue has already asked a console
        # that is holding the game loaded, and every answer it gets back from
        # one of those is the wrong answer.
        self.ask_before_starting()
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
                "Type the console's address into the box at the top of "
                "this window, or press Find my PS3 next to it.")
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
        self._hide_verdict()
        # Asked before a byte is read out of the game, because the answer
        # decides whether Apply may come alive at all and because somebody
        # should be reading it while the scan runs rather than after it.
        self._running = self.running_this_game()
        self._show_running(self._running)
        if self._running:
            # Stop here. Every file the scan would read has to come off the
            # console and be decrypted, several megabytes of it, and the
            # answer is already known: nothing in this folder can be patched
            # while the console has the game loaded. It used to say so in a
            # panel and then spend the time anyway, with the status line
            # reading "decrypting t5mp_ps3f.self" underneath it.
            #
            # Scan again is the way back, once the game has been quit and the
            # console restarted, which is what the panel asks for.
            self._clear_state()
            self._files.clear()
            self._set_busy(False, "")
            self._rescan.setEnabled(True)
            self._patch.setEnabled(False)
            self._restore.setEnabled(False)
            self._scan = None
            return None
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

        extras = self.scan_extras
        wanted = self._release
        folder = self._folder

        def work(control):
            return _scan_console(host, title_key, tool, lister, writer,
                                 control.progress, control, extras, wanted,
                                 folder)

        task = self.submit(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_scanned)
        task.failed.connect(self._on_failed)
        task.done.connect(self._scan_done)
        self._task = task
        return task

    def _on_rescan(self):
        self._release = ""
        # And the folder somebody typed, for the same reason: this button is
        # the way back to the question, and a folder that stayed put would
        # leave them no way to point somewhere else. The read-back that
        # follows a patch does not come through here, so it keeps the folder
        # it has just written to.
        self._folder = ""
        self.start_scan()

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

    # -- what a title needs beyond replacing some bytes
    #
    # Two of the three fixes need none of this and inherit every default. The
    # Black Ops 1 fix has to know which local user is signed in and has to put
    # a readable copy of their np_cache.dat where the game can reach it, and
    # neither of those is a thing the other two should have to know about.

    def scan_extras(self, lister):
        """Anything else to read while the console is open for the scan.

        Runs on the worker with the read-only client, alongside the scan
        itself, so a title that needs to know something about the console
        does not cost a second trip.
        """
        return {}

    def on_scan_extras(self, extras):
        """Whatever scan_extras read, back on the GUI thread."""

    def patch_ready(self):
        """(ready, why not). Asked before the confirmation box is shown."""
        return True, ""

    def patch_allowed(self):
        """Whether Apply may be enabled at all, whatever the scan found.

        For a fix that is only right for some accounts. A scan cannot tell
        which of those the person in front of it has, so the screen asks and
        this is where the answer is read.

        And for the game the console has loaded right now. That one is not a
        judgement about the files: they may be perfectly patchable and the
        patch would still achieve nothing, because the console goes on running
        the module it loaded until it is restarted. Refused here rather than
        allowed and regretted, which is the evening this screen lost once
        already.
        """
        if self._running:
            return False
        return not self.CONFIRM_WITH or self._confirm.isChecked()

    def _on_confirmed(self, _checked):
        """The tick box moved, so Apply may have just become available."""
        if self._scan is None or self._writing:
            return
        self._patch.setEnabled(
            self._scan.can_patch
            and not (self._update_state is not None
                     and self._update_state.blocks)
            and self.patch_allowed())

    def _notice_dialogue(self, title, body, button="OK"):
        """One plain modal dialogue, with this screen blurred out behind it.

        Both of the dialogues this screen shows are built here, so that they
        are the same shape as each other on all three screens and so there is
        one place that knows how the blur goes on and comes off again.

        The blur is not decoration. What is behind a dialogue is a screen full
        of sentences, and a dialogue over a screen somebody can still read is
        one they dismiss while reading past it. Blurred, there is one thing on
        the screen to read, which is the whole reason either of these is a
        dialogue rather than another sentence.
        """
        effect = QGraphicsBlurEffect(self)
        effect.setBlurRadius(NOTICE_BLUR)
        self.setGraphicsEffect(effect)
        try:
            box = QDialog(self)
            box.setWindowTitle(title)
            box.setModal(True)
            rows = QVBoxLayout(box)
            rows.setContentsMargins(20, 18, 20, 16)
            rows.setSpacing(14)
            words = _wrapping(QLabel(body))
            words.setMinimumWidth(420)
            rows.addWidget(words)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok)
            buttons.button(QDialogButtonBox.Ok).setText(button)
            buttons.accepted.connect(box.accept)
            rows.addWidget(buttons)
            box.exec()
        finally:
            # Whatever happened to the dialogue, the screen behind it has to
            # come back. A blur left on is a screen nobody can read again.
            self.setGraphicsEffect(None)

    def ask_before_starting(self):
        """The one thing to do before a patch screen is any use at all.

        A seam like ask_firmware and ask_for_folder, so that a test can answer
        it without a modal dialogue on a machine with no display, and so that
        all three screens say it in the same words from the same place.

        Why it is a dialogue and why nothing else may happen behind it: the
        console loads a game's module when the game starts and keeps hold of
        it until the console is restarted. Patching a game the console has
        loaded writes new files to the hard drive and changes nothing at all
        about what is running, and quitting back to the XMB does not shake the
        module loose either. Somebody lost an evening to exactly this. The
        game was running the whole time, every scan came back saying the files
        were already fixed, and the fault he was patching out sat there
        unchanged until he gave up. Nothing on the screen was wrong. This
        sentence was on it, in among the others, and he read past it.
        """
        self._notice_dialogue("Before you start", BEFORE_NOTICE, "Continue")

    def say_restart_needed(self):
        """Shown once, after a patch or a restore has been read back.

        The same seam as the one above and for the same reasons. It replaced a
        framed panel that said this at three times the length and stayed on
        screen afterwards, which is a panel people learn the shape of and stop
        reading. Shown after the read-back rather than after the write,
        because until the console has been read back nobody knows there is
        anything on it to restart for.
        """
        self._notice_dialogue("Restart the console", RESTART_NOTICE)

    def more_about_this_fix(self):
        """What the expander holds, as one block of text.

        Everything in here was a paragraph or a framed banner above the file
        table. None of it changes what somebody does next: it is the fault the
        fix addresses said at length, and whatever else is worth knowing about
        this particular game. It is kept because it is true and worth having,
        and it is behind one closed line because in front of the table it
        stood between every user and the state of their own game.

        One implementation for all three screens. A screen with something of
        its own to put here fills in BACKGROUND rather than overriding this.
        """
        parts = [self.config.get("symptom", "")]
        for body, emphasis in self.BACKGROUND:
            parts.append(" ".join(part for part in (body, emphasis) if part))
        return "\n\n".join(part for part in parts if part)

    def ask_firmware(self):
        """Which firmware the console is running, from the user, or "".

        Only reached when the console did not say, which is unusual: every
        custom firmware fork names itself and HEN names itself. A seam like
        the others, so a test can answer it without a dialogue box.

        Nothing is preselected. The two forms are not interchangeable and
        signing for the wrong one gives a game that will not start, so this
        asks rather than offering a default to click past.
        """
        choices = ["PS3HEN", "Custom firmware (Cobra, Evilnat, Rebug and the "
                   "rest)"]
        picked, said_yes = QInputDialog.getItem(
            self, "Which firmware is this console running?",
            FIRMWARE_PROMPT, choices, 0, False)
        if not said_yes:
            return ""
        return "hen" if picked == choices[0] else "cfw"

    def ask_for_folder(self):
        """Where the game is, from the user, or "" if they would rather not.

        A seam like the others here, so that all three screens ask the same
        question in the same words and a test can answer it without a dialogue
        box on a machine with no display. Nothing is done with the answer
        until it has been checked: it has to name a folder, the console is
        asked for that folder, and the files in it are read against what this
        fix expects before a byte is written.
        """
        typed, said_yes = QInputDialog.getText(
            self, "Where is the game?", FOLDER_PROMPT)
        return typed.strip() if said_yes else ""

    def patch_context(self):
        """What the fix needs that is not in the binary. See flow.apply_fix."""
        return None

    def patch_extras(self, writer, workdir):
        """(remote, local) pairs to put on the console beside the binaries.

        Runs on the worker with the writing client already open. Staged into
        workdir, which the caller owns and cleans up.
        """
        return ()

    # The three seams. Tests replace all three: the mock console listens on a
    # port of its own rather than on 21.
    def _scetool(self):
        return Signer(firmware_kind=self.effective_firmware())

    def effective_firmware(self):
        """What to sign for: what the console said, or what the user answered.

        A console this program can reach is already running homebrew. webMAN
        does not run on stock firmware, and on PS3HEN it only runs once HEN
        has been enabled, so a reachable console that does not name itself as
        custom firmware is more likely to be HEN than CFW. Every custom
        firmware fork names itself, Evilnat and Rebug and Ferrox and Habib
        among them, so silence is genuinely unusual.

        That is why this never falls back to custom firmware on its own.
        Signing for the wrong one produces a game that will not start, and
        guessing custom firmware gets it wrong for exactly the people the
        3.55 re-sign exists to help. Where the console did not say, the user
        is asked and Apply waits for the answer.
        """
        read = self.firmware_kind()
        if read in ("cfw", "hen"):
            return read
        return self._firmware_choice

    def firmware_is_settled(self):
        return self.effective_firmware() in ("cfw", "hen")

    def firmware_words(self):
        """The firmware line as the console gave it, for the screen to show."""
        # Asked first: it is what fills in the line, and reading the line
        # before asking gave a screen that named the kind and not the words
        # the console used.
        kind = self.firmware_kind()
        line = self._firmware_line or ""
        if kind == "hen":
            return f"Firmware: {line} (HEN)" if line else "Firmware: HEN"
        if kind == "cfw":
            return (f"Firmware: {line} (custom firmware)" if line
                    else "Firmware: custom firmware")
        if line:
            return (f"Firmware: {line}. This does not name HEN or a custom "
                    f"firmware, so which one it is has to be said below.")
        return ("The console did not say which firmware it is running, so "
                "which one it is has to be said below.")

    def set_firmware_choice(self, kind):
        """The user's answer, when the console did not say. "" clears it."""
        self._firmware_choice = kind if kind in ("cfw", "hen") else ""
        # Apply is gated on this, so the button has to be looked at again the
        # moment the answer changes.
        self._on_confirmed(None)

    def firmware_kind(self):
        """What the console said it is running, or "" if it did not say.

        Read once per host off webMAN's own page and kept, because it decides
        which form a rebuilt file takes and the answer cannot change while the
        console is sitting there. A console that did not answer leaves this
        empty, and every caller treats empty as "keep the behaviour that has
        shipped" rather than guessing.
        """
        host = self.connection.host
        if not host:
            return ""
        if self._firmware_host != host:
            self._firmware_host = host
            found = self._read_firmware(host)
            self._firmware_kind, self._firmware_line = found
            # A different console is a different answer, so an answer given
            # for the last one must not be carried over to this one.
            self._firmware_choice = ""
        return self._firmware_kind

    def _read_firmware(self, host):
        """(kind, the line as printed). The seam; tests replace this.

        The line is kept as well as the kind so the screen can show what the
        console actually said, which is what makes a bug report say which
        firmware it came from without anybody being asked.
        """
        try:
            page = transport.HttpProbe(host).get(FIRMWARE_PAGE)
            if not getattr(page, "ok", False):
                return "", ""
            found = parsers.parse_firmware_line(page.body)
            return (found.get("firmware_kind", ""),
                    found.get("firmware_line", ""))
        except Exception:                                   # noqa: BLE001
            # A console that will not answer has not said, which is the same
            # as saying nothing. It must never become a guess.
            return "", ""

    def _read_running_page(self, host):
        """webMAN's cpursx page as text, or "". The seam.

        Written as its own method for the same reason _read_firmware is: it
        is the one place this screen reaches the network for this answer, so
        a test replaces it and nothing in a test goes near a console.

        A console that will not answer has not said what it has loaded, and
        silence must never become a refusal to patch. The dialogue on the way
        in has already asked the question in words.
        """
        if read_page is None:
            return ""
        try:
            return read_page(host) or ""
        except Exception:                                   # noqa: BLE001
            return ""

    def running_this_game(self):
        """This screen's own game, if the console has it loaded, or "".

        Only this screen's game counts. A console sitting in another game is
        not a reason to refuse anything here: the module this fix cares about
        is the one belonging to the title on this card, and refusing a Black
        Ops 1 patch because somebody left Modern Warfare 3 running would be a
        refusal the user cannot make sense of and did not deserve.
        """
        host = self.connection.host if self.connection else ""
        if not host:
            return ""
        page = self._read_running_page(host)
        if not page:
            return ""
        # Asked of each release of this game in turn. The page carries the
        # title ID of whatever is open and nothing that says which field is
        # which, so the answerable question is whether one of ours is on it.
        for title_id in self._releases_to_check():
            if parsers.title_is_loaded(page, title_id):
                return titles.normalise(title_id)
        return ""

    def _releases_to_check(self):
        """The title IDs worth looking for, this screen's release first.

        The release the scan settled on is the one about to be patched, so it
        is asked about first. The rest of the game's releases follow, because
        a console can have a copy this screen has not landed on yet and it is
        the same game either way.
        """
        found = []
        chosen = getattr(self, "_release", "") or ""
        if chosen:
            found.append(chosen)
        found += [item for item in self.config.get("title_ids", ())
                  if item not in found]
        return found

    def _show_running(self, title_id):
        """Say the console has the game loaded, or take the notice away."""
        if not title_id:
            self._running_notice.hide()
            return
        self._running_text.setText(RUNNING_NOTICE.format(
            name=self.config.get("short") or self.config.get("name")
            or self.title,
            title_id=title_id))
        self._paint_running()
        self._running_notice.show()

    def _paint_running(self):
        """The theme's warning colour, in the shape of the other notices."""
        accent = self._colour_name("warn") or self._colour_name("text")
        surface = self._colour_name("surface_alt") \
            or self._colour_name("surface")
        text = self._colour_name("text")
        if not (accent and surface and text):
            return
        self._running_notice.setStyleSheet(
            f"QFrame#runningnotice {{ background-color: {surface};"
            f" border: 1px solid {accent};"
            f" border-left: 6px solid {accent};"
            f" border-radius: 6px; }}")
        self._running_text.setStyleSheet(f"color: {text}; border: none;")

    def _lister(self, host):
        return transport.FtpLister(host)

    def _writer(self, host):
        return FtpWriter(host)

    def _fit_files(self, rows):
        """Give the table room for every row of the title, without scrolling.

        A three-file title came up two rows tall on one console, with
        EBOOT.BIN scrolled out of sight while the sentence underneath named
        it, which reads as a file that is not on the console at all.
        """
        rows = max(1, min(rows, MOST_FILE_ROWS))
        header = self._files.header()
        header_height = max(header.height(), header.sizeHint().height())
        row_height = self._files.sizeHintForRow(0)
        if row_height <= 0:
            # Asked before there is a row to measure, which is every scan
            # before the first one.
            row_height = self._files.fontMetrics().height() + ROW_PADDING
        self._files.setMinimumHeight(header_height + rows * row_height
                                     + 2 * self._files.frameWidth())

    def _choose_release(self, title_ids):
        """Which of several installed releases to work on, or "" if cancelled.

        Nothing is selected when the box opens and the button stays off until
        something is. A preselected answer to this question is an answer
        somebody can accept without reading it, and the cost of the wrong one
        is a patched copy of a game they do not play.
        """
        box = QDialog(self)
        box.setWindowTitle("Which copy of the game?")
        rows = QVBoxLayout(box)
        words = _wrapping(QLabel(
            "There is more than one copy of this game on the console. Choose "
            "the one to fix. The others are left exactly as they are.\n\n"
            "If you pick the wrong one, put its originals back from the "
            "button on this screen and run it again on the other."))
        words.setMinimumWidth(420)
        rows.addWidget(words)
        listing = QListWidget()
        for title_id in title_ids:
            listing.addItem(_release_label(title_id))
        listing.setCurrentRow(-1)
        rows.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(box.accept)
        buttons.rejected.connect(box.reject)
        rows.addWidget(buttons)
        ok = buttons.button(QDialogButtonBox.Ok)
        ok.setEnabled(False)
        listing.currentRowChanged.connect(
            lambda row: ok.setEnabled(row >= 0))
        listing.itemDoubleClicked.connect(lambda _item: box.accept())
        if box.exec() != QDialog.Accepted or listing.currentRow() < 0:
            return ""
        return title_ids[listing.currentRow()]

    def _on_type_folder(self):
        """The user knows where the game is, so nothing is searched for.

        Whatever they give is treated as a release nobody has tested: the
        files in that folder are read and checked against what this fix
        expects, the screen says the release is untested, and the fix is
        refused if the patch site is not there. Nothing else on the console is
        looked at, and nothing on this screen asks anybody to remove a game to
        make a search work.
        """
        if self._writing or self._reading_back:
            return
        typed = self.ask_for_folder()
        if not typed:
            return
        if not _folder_title_id(typed):
            self._show_state(
                "warn", "That does not look like a folder on the console",
                f"Nothing has been read and nothing has been changed.\n\n"
                f"The console keeps installed games in {GAME_FOLDER}, one "
                f"folder each, and the path typed was {typed}. Give that "
                f"folder, for example {GAME_FOLDER}/BLES01032. The button "
                f"below asks again.",
                offer_folder=True)
            return
        self._folder = typed
        self.start_scan()

    def _on_scanned(self, result):
        # Three or four. A test that drives this screen builds the three the
        # screen has always taken, and a title with nothing extra to read
        # never produces the fourth.
        location, report, where = result[0], result[1], result[2]
        self.on_scan_extras(result[3] if len(result) > 3 else {})
        self._scan = report
        self._location = location
        self._task = None
        self._rescan.setEnabled(True)
        self._where.setText(self._where_words(where))
        self._files.clear()
        self._refresh_restore(
            (report.title_id if report is not None else "")
            or (location.title_id if location is not None else ""))
        if (report is None and location is not None and location.ready
                and len(location.title_ids) > 1 and not self._release):
            # The scan stopped to ask. Whatever comes back, the next scan
            # knows which folder to read, and a cancelled question leaves the
            # screen saying so rather than showing another copy's files.
            self._rescan.setEnabled(True)
            chosen = self._choose_release(location.title_ids)
            if chosen:
                self._release = chosen
                self.start_scan()
                return
            self._patch.setEnabled(False)
            self._restore.setEnabled(False)
            self._hide_update()
            self._show_state(
                "text_dim", "There is more than one copy of this game",
                "Press Scan again and choose which one to fix. Nothing has "
                "been read and nothing will be written until you do.",
                reason="On the console: "
                       + _and_list(location.title_ids) + ".")
            return
        if report is None:
            self._patch.setEnabled(False)
            self._hide_update()
            self._hide_verdict()
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

        actionable = {item.name for item in report.to_patch}
        for item in report.files:
            if item.name in self._declined:
                item.selected = False
        self._files.blockSignals(True)
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
            row.setData(0, Qt.UserRole, item.name)
            # A tick box only on the files this run could write. Anything
            # else is shown and is not something there is a choice about.
            if item.name in actionable:
                row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
                row.setCheckState(0, Qt.Checked if item.selected
                                  else Qt.Unchecked)
            else:
                row.setFlags(row.flags() & ~Qt.ItemIsUserCheckable)
            self._files.addTopLevelItem(row)
        self._files.blockSignals(False)
        for column in range(4):
            self._files.resizeColumnToContents(column)
        self._fit_files(len(report.files))

        # Before anything else on the page is worked out, because it is the
        # answer the user opened this screen for.
        self._show_verdict(report)

        self._scan_text = self._verdict(report)
        self._detail.setText(self._compose(self._scan_text))
        self._show_next_step(report)
        # The gate. A fix confirmed on one build of the game is a fix for that
        # build, and offering it on another is how an install stops starting,
        # so the button is not enabled until the two agree.
        self._update_state = flow.update_check(report.title_id,
                                               self._installed_version())
        self._show_update(self._update_state)
        self._patch.setEnabled(report.can_patch
                               and not self._update_state.blocks
                               and self.patch_allowed())
        # The one place the screen is allowed to call a patch a success: a
        # read-back of a patch this session applied, which came back with every
        # file on the console already fixed. Anything else -- a file still
        # needing fixing, one this program could not recognise, one it never
        # managed to read -- leaves the green panel hidden and shows no
        # dialogue, and the wording underneath says what is actually known.
        confirmed = bool(self._reading_back and self._patch_message
                         and _all_fixed(report))
        if confirmed:
            self._show_success()
        self.status_message.emit(
            f"{report.title_id}: " +
            (f"{len(report.to_patch)} file(s) to fix" if report.to_patch
             else "nothing to do"))
        # Last, once, and for either direction. A restore needs the restart
        # for the same reason a patch does, because the console is still
        # running the module it loaded whichever way the files moved, and a
        # user who has just been shown two dialogues saying the same thing has
        # been taught that the second one is not worth reading. The two are
        # decided together here so that they cannot both fire.
        #
        # A repaint replays this whole method to redraw the table, and
        # _reading_back is false by then, so the dialogue belongs to the
        # read-back and to nothing else.
        if confirmed or (self._reading_back and self._restored_ok):
            self.say_restart_needed()

    def next_step_words(self):
        """What to say when this program will not touch a file, or "".

        Only reached for a file it did not recognise. A game whose files
        somebody has already modified, by a mod menu that replaces the eboot
        for instance, reads exactly like that from here.

        The repository rather than the standalone exe beside it. That exe uses
        the same detection as this screen and would refuse the same file for
        the same reason.
        """
        repo = self.config.get("repo", "")
        if not repo:
            return ""
        return (f"If the game files have already been modified, for example "
                f"by a mod menu that replaces the eboot, this tool will not "
                f"touch them. The manual scetool sequence in "
                f"<a href=\"{repo}\">the repo</a> will, because it lets you "
                f"supply the signing details yourself.")

    def _show_next_step(self, report):
        """Where to go when this program will not touch a file."""
        words = self.next_step_words()
        if report is None or not report.unrecognised or not words:
            self._next_step.setText("")
            self._next_step.hide()
            return
        self._next_step.setText(words)
        self._next_step.show()

    def _on_file_ticked(self, row, column):
        """One file taken out of the write, or put back into it.

        Only the wording and the button are touched. Rebuilding the table from
        inside its own itemChanged is how this program once destroyed the item
        whose tick was still on the stack, which the console reported as a bus
        error.
        """
        if column != 0 or self._scan is None:
            return
        name = row.data(0, Qt.UserRole)
        item = self._scan.file_for(name) if name else None
        if item is None:
            return
        item.selected = row.checkState(0) == Qt.Checked
        # Kept on the screen as well as on the file, because a scan builds
        # fresh file records and the tick would otherwise come back on. A
        # decision a person made is not something to recompute.
        if item.selected:
            self._declined.discard(name)
        else:
            self._declined.add(name)
        self._scan_text = self._verdict(self._scan)
        self._detail.setText(self._compose(self._scan_text))
        self._patch.setEnabled(
            self._scan.can_patch
            and not (self._update_state is not None
                     and self._update_state.blocks)
            and self.patch_allowed())

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
        self._show_state(token, heading, body, reason,
                         offer_updates=location.state == flow.NO_UPDATE,
                         offer_folder=location.state in (flow.NOT_INSTALLED,
                                                         TYPED_MISSING))
        self._detail.setText(self._compose(""))
        self._show_next_step(None)
        self.status_message.emit(heading)

    def _show_state(self, token, heading, body, reason="",
                    offer_updates=False, offer_folder=False):
        self._panel_token = token
        self._panel_heading.setText(heading)
        self._panel_body.setText(body)
        self._panel_body.setVisible(bool(body))
        self._panel_reason.setText(reason)
        self._panel_reason.setVisible(bool(reason))
        self._panel_button.setVisible(bool(offer_updates))
        self._folder_button.setVisible(bool(offer_folder))
        self._paint_state()
        self._panel.show()
        # Nothing was read, so there are no rows. An empty table beside the
        # explanation only invites the user to wonder what should have been
        # in it.
        self._files.setVisible(False)
        self._filler.setVisible(True)

    def _clear_state(self):
        self._panel_token = ""
        self._panel_button.hide()
        self._folder_button.hide()
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

    def _untested(self):
        """Whether nobody has watched this fix work on the release on screen.

        True for a folder whose title ID the table does not name and whose
        game was worked out from the files in it, and true for a folder the
        user typed. Both are the same thing to a user: the fix is being tried
        on a release nobody has reported back on.
        """
        installation = getattr(self._location, "installation", None)
        return bool(getattr(installation, "untested", False))

    def _where_words(self, where):
        """The folder being worked on, and whether its release is tested.

        The warning is on this line as well as in the panel because this is
        the line that names what is about to be written to, and it stays on
        screen beside the button while the panel above is read once.
        """
        if not where or not self._untested():
            return where
        return f"{where}. {UNTESTED_LINE}"

    def verdict_words(self, report):
        """(token, line) for the sentence under the game's name, or ("", "").

        One implementation for all three screens and for any added later. The
        question is the same one on every card, the answer comes out of the
        same report, and three screens answering it in three sets of words
        would be three screens to keep in step.

        The two cases that have no fix state to report, a set of files that is
        not what the fix expects and a set nothing could be read out of, are
        answered on this line as well. They used to be a framed paragraph of
        their own further down, which is the arrangement that put the answer
        somebody came for below the part of the screen they read.
        """
        if report is None:
            return "", ""
        name = (self.config.get("short") or self.config.get("name")
                or self.title)
        fixed, stock, strange, unread = _fix_state(report)
        if fixed and stock:
            pattern = VERDICT_MIXED
        elif fixed:
            pattern = VERDICT_FIXED
        elif stock:
            pattern = VERDICT_NOT_FIXED
        elif strange:
            pattern = VERDICT_STRANGE
        elif unread:
            pattern = VERDICT_UNREAD
        else:
            # Nothing in this folder that the fix has any opinion about, so
            # there is no verdict to give and the line stays off rather than
            # inventing one.
            return "", ""
        line = pattern.format(name=name)
        # Only where there is a fix state as well. Said on its own above, the
        # strange files are the whole verdict, and repeating it here would be
        # the same point twice in one sentence.
        if strange and (fixed or stock):
            line = f"{line} {VERDICT_SOME_STRANGE}"
        if self._untested():
            line = f"{line} {VERDICT_UNTESTED}"
        return VERDICT_TOKENS.get(pattern, "text"), line

    def _show_verdict(self, report):
        token, line = self.verdict_words(report)
        if not line:
            self._hide_verdict()
            return
        self._verdict_token = token
        self._verdict_line.setText(line)
        self._paint_verdict()
        self._verdict_line.show()

    def _hide_verdict(self):
        self._verdict_token = ""
        self._verdict_line.setText("")
        self._verdict_line.hide()

    def _paint_verdict(self):
        """The theme's own colour for the state, and never a colour name."""
        ink = (self._colour_name(self._verdict_token)
               or self._colour_name("text"))
        if ink:
            self._verdict_line.setStyleSheet(f"color: {ink};")

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

    def _paint_notice(self):
        """Quieter than the restart notice, and in the same shape.

        These are information rather than an instruction: nobody has to act on
        one to make a fix work, and drawing them as loudly as the sentence
        somebody must not miss would cost that sentence its meaning.
        """
        surface = self._colour_name("surface_alt") \
            or self._colour_name("surface")
        text = self._colour_name("text")
        if not (surface and text):
            return
        for frame, words, loud, token in self._notices:
            accent = self._colour_name(token) or text
            frame.setStyleSheet(
                f"QFrame#sidenotice {{ background-color: {surface};"
                f" border: 1px solid {accent};"
                f" border-left: 6px solid {accent};"
                f" border-radius: 6px; }}"
                f"QFrame#sidenotice QLabel {{ background: transparent;"
                f" border: none; }}")
            words.setStyleSheet(f"color: {text}; border: none;")
            loud.setStyleSheet(f"color: {accent}; border: none;")

    def _paint_count(self):
        dim = self._colour_name("text_dim")
        if dim:
            self._count.setStyleSheet(f"color: {dim};")

    def _plan(self, report):
        """Which files this run will write, and which it will leave alone.

        Said in one place because it is the thing the user is deciding on.
        Each file is judged on its own, so a set where one is already fixed,
        one is stock and one is a stranger is three separate answers and all
        three belong on the screen.
        """
        writing = [item.name for item in report.chosen]
        leaving = []
        for item in report.files:
            if not item.present or item.name in writing:
                continue
            if item.state == flow.NO_SITE:
                # MW3's default.self. The fix does not touch it at all, so it
                # is not something being left out of anything.
                continue
            if item.state == flow.NOT_PATCHED:
                leaving.append(f"{item.name} was unticked")
                continue
            words = LEFT_ALONE.get(item.state)
            if words:
                leaving.append(words.format(name=item.name))
        lines = []
        if writing:
            lines.append(
                f"{_and_list(writing)} will be replaced. Your originals are "
                f"copied to the Desktop first, and put back automatically if "
                f"anything goes wrong.")
        if leaving:
            said = "it is" if len(leaving) == 1 else "they are"
            lines.append(f"{_and_list(leaving)}, so {said} left exactly as "
                         f"{said}.")
        return lines

    def _completeness(self, report):
        """What it takes for the set to end up complete, from this console.

        Every file that carries the binary has to end up fixed. How many
        writes that takes depends on what the scan found, and the fixed
        sentence that used to sit here said three were needed while the table
        above it showed two of the three already done.
        """
        carriers = [item for item in report.files if item.site]
        done = [item for item in carriers if item.state == flow.PATCHED]
        if not done or len(carriers) < 2:
            return self.config.get("set_advice", "")
        writing = [item.name for item in report.chosen]
        left = [item.name for item in carriers
                if item.present and item.state != flow.PATCHED
                and item.name not in writing]
        # Counted rather than named. The plan above this has already said
        # which file is already fixed, and saying it twice reads as two
        # different points.
        has = "has" if len(done) == 1 else "have"
        opening = (f"{_count_word(len(done)).capitalize()} of the "
                   f"{_count_word(len(carriers))} files that carry the "
                   f"binary {has} the fix already.")
        if not left:
            return f"{opening} Writing {_and_list(writing)} completes the set."
        stays = "stays" if len(left) == 1 else "stay"
        return (f"{opening} {_and_list(left)} {stays} stock after this run, "
                f"and the game keeps freezing until the set is complete.")

    def _verdict(self, report):
        if report.error:
            return report.error
        lines = self._plan(report)
        if not report.chosen:
            if report.already_patched and not report.blocked:
                lines = ["Everything here is already fixed. Nothing to do."]
            elif report.to_patch:
                lines.append("Tick at least one file to apply the fix to.")
            else:
                lines.append("There is nothing here this program can write.")
        if report.cannot_decrypt:
            # The release nobody has confirmed, found out at the only point it
            # can be found out. The user has done nothing wrong.
            names = _and_list([item.name for item in report.cannot_decrypt])
            lines.append(
                f"{names} could not be opened by this tool. {report.title_id} "
                f"is one of the releases of this game that locks its files "
                f"differently from the ones the fix has been proved on, and "
                f"without opening them there is no way to fix them. Nothing "
                f"is wrong with your console, your game or your connection. "
                f"If you want to report it, the thing to quote is "
                f"{report.title_id}.")
        if report.not_examined:
            # Says plainly whose fault it is. The sentence that used to appear
            # here described a file mismatch, which had the user checking a
            # console that was fine while the missing piece was in this build
            # of the program.
            missing = ", ".join(sorted(
                {item.missing_tool for item in report.not_examined
                 if item.missing_tool}))
            names = _and_list([item.name for item in report.not_examined])
            lines.append(
                f"This program could not check {names}. "
                + (f"{missing} is missing from this build of the program, "
                   f"which is a fault in this program and not in your "
                   f"console or your game. " if missing else "")
                + "Nothing here says anything is wrong with your files: it "
                  "is a check that did not run.")
        if report.unrecognised:
            names = _and_list([item.name for item in report.unrecognised])
            lines.append(
                f"{names} is not a build this fix was written for, so it is "
                f"left alone. Sending a patch to the wrong build of a binary "
                f"is how an install stops starting."
                if len(report.unrecognised) == 1 else
                f"{names} are not builds this fix was written for, so they "
                f"are left alone. Sending a patch to the wrong build of a "
                f"binary is how an install stops starting.")
        for image, writing, leaving in report.half_pairs():
            lines.append(
                f"{_and_list(writing)} and {_and_list(leaving)} are the same "
                f"binary signed twice, and only "
                f"{_and_list(writing)} can be written here. A copy of that "
                f"binary that has the fix and one that does not is the "
                f"half-finished state the fix's own notes say can freeze the "
                f"game. Going ahead is still yours to decide, and everything "
                f"written is backed up first.")
        if report.chosen:
            lines.append(self._completeness(report))
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
            elif not report.not_examined and not self._untested():
                # The honest caveat, and it belongs under the plan rather than
                # over it: the scan has already opened the files and found
                # what it expected, so the fix suits this release as far as
                # anything can be checked from here. Nobody has simply watched
                # it work on this one yet.
                #
                # Left out for a release the title table does not name at all,
                # which has the panel above the table saying this already. The
                # same point made twice in different words reads as two
                # separate problems.
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
            elif item.state == flow.REPLACED:
                # What it is has already been said once, in the notes. This
                # adds the hash, which is what somebody reporting the file
                # needs and what tells two copies of it apart.
                lines.append(f"{item.name}: sha1 "
                             f"{item.sha1 or 'not read'}")
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
        # The same, for a console that has this game loaded. The button is off
        # in that case too, and this is the check that makes the refusal real
        # rather than a matter of which widget happened to be enabled.
        if self._running:
            return
        # Which firmware this is signing for, asked here rather than gating
        # the button on it. Gating it disabled Apply on a console that did not
        # say, with nothing on screen to answer with, so the fix could not be
        # applied at all.
        if not self.firmware_is_settled():
            self.set_firmware_choice(self.ask_firmware())
            if not self.firmware_is_settled():
                return
        ready, why = self.patch_ready()
        if not ready:
            # An empty reason means the user has just been asked something
            # and said no, which does not want a second box telling them so.
            if why:
                QMessageBox.information(self, "Not yet", why)
            return
        names = _and_list([item.name for item in self._scan.chosen])
        # Said again here for a release nobody has tested. This box is the
        # last word before anything is written and it is the one thing on
        # screen somebody has to read to get past, so it is where the warning
        # is worth most.
        untested = ""
        if self._untested():
            untested = (f"{UNTESTED_LINE} The files were read and the patch "
                        f"site was found in them, which is the check that "
                        f"decides whether the fix fits.\n\n")
        answer = QMessageBox.question(
            self, "Apply the fix",
            f"{names} on the console will be replaced.\n\n"
            f"{untested}"
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
        context = self.patch_context()
        collect = self.patch_extras

        def work(control):
            workdir = tempfile.mkdtemp(prefix="ps3tools-extra-")
            try:
                with open_writer(host) as writer:
                    return flow.patch(writer, tool, report,
                                      progress=control.progress,
                                      context=context,
                                      extra_files=collect(writer, workdir))
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

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
            lines.append(f"Changed in {result.title_id}: "
                         + ", ".join(result.changed) + ".")
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
        if worked:
            # Only a run that actually replaced something. An attempt that
            # changed nothing is not an event anybody wants remembered on the
            # bar for the next hour.
            self.event_noted.emit(f"{self.title} applied")
        # A failed or rolled-back attempt is left on screen saying so, rather
        # than having its message replaced by a second run of the scan.
        self._read_back_wanted = worked

    def _show_success(self):
        """The green panel. What the user is to do next is a dialogue now.

        The panel used to be followed by a framed instruction to restart the
        console, which stayed on the screen afterwards and was the fourth
        framed paragraph on it. The instruction is worth more said once, over
        a blurred screen, with nothing else to look at.
        """
        self._success_heading.setText(SUCCESS_HEADING)
        self._success_body.setText(SUCCESS_BODY)
        self._paint_success()
        self._success.show()

    def _hide_success(self):
        self._success.hide()

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
        self._hide_verdict()
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

    def _refresh_restore(self, title_id):
        """The button is live only when there is something to put back.

        It was live on a console with no title update installed and nothing on
        the Desktop, where the only thing it could produce was a refusal. The
        reason moves to the tooltip rather than being lost.
        """
        found = bool(title_id) and bool(self._backups(title_id))
        self._restore.setEnabled(found)
        self._restore.setToolTip(
            "" if found else
            f"There is no backup of {title_id or 'this game'} on this "
            f"computer. Every time this program applies the fix it copies "
            f"your original files to a folder called "
            f"\u201c{backups.FOLDER_NAME}\u201d on the Desktop first.")

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
        if self._notices:
            self._paint_notice()
        if not self._success.isHidden():
            self._paint_success()
        if not self._update.isHidden():
            self._paint_update()
        if not self._running_notice.isHidden():
            self._paint_running()
        if not self._verdict_line.isHidden():
            self._paint_verdict()
        if self._writing:
            return
        if self._panel_token:
            self._paint_state()
        if self._scan is not None:
            self._on_scanned((self._location, self._scan, self._where.text()))


def _release_label(title_id):
    """One installed release, as the dialogue and the screen name it."""
    return f"{title_id} in {titles.usrdir_for(title_id)}"


def _folder_title_id(typed):
    """The title ID in a path somebody typed, or "" if there is not one.

    The game's own folder and its USRDIR are both accepted, with or without a
    trailing slash, because both are what a person copies out of an FTP client
    or off webMAN's file manager.

    Anywhere other than /dev_hdd0/game comes back empty. The fix reads and
    writes the files in that folder's USRDIR, so a path pointing somewhere
    else is one this screen would quietly ignore in favour of a folder it
    worked out for itself, and being told so is better than being ignored.
    """
    parts = [part for part in str(typed or "").replace("\\", "/").split("/")
             if part]
    if parts and parts[-1].upper() == "USRDIR":
        parts = parts[:-1]
    if not parts:
        return ""
    if [part.lower() for part in parts[:-1]] != \
            GAME_FOLDER.strip("/").split("/"):
        return ""
    return titles.normalise(parts[-1])


class _TypedRelease:
    """The installation record for a folder the user pointed this screen at.

    The screen reads two things off an installation: which title update is on
    it, which nothing has read here, and whether anybody has tested the fix on
    this release, which a folder somebody typed never is. Standing in for the
    record rather than going without one keeps the typed folder on exactly the
    same path through this screen as a folder the search found.
    """

    untested = True
    verified = False
    tu_version = None

    def __init__(self, title_id):
        self.title_id = title_id


def _typed_location(lister, folder):
    """The folder the user typed, as a Location the screen already reads.

    Nothing is searched for and nothing else on the console is looked at. The
    user has said where the game is; the only question left is whether that
    folder is there, and it is put to the console before a report is built on
    it so that a mistyped path is answered as a mistyped path.
    """
    title_id = _folder_title_id(folder)
    try:
        lister.list_dir(titles.usrdir_for(title_id) + "/")
    except Exception as exc:                                # noqa: BLE001
        location = flow.Location(
            TYPED_MISSING, [title_id],
            reason=f"{exc.__class__.__name__}: {exc}")
    else:
        location = flow.Location(flow.READY, [title_id],
                                 installation=_TypedRelease(title_id))
    # Carried on the location so that the wording can quote what was typed
    # rather than the folder it was turned into.
    location.typed_path = folder
    return location


def _scan_console(host, title_key, tool, open_lister, open_writer, progress,
                  control, extras=None, wanted="", folder=""):
    """Find the installation and scan it. Runs on a worker, never on the GUI.

    Detection is a read, so it goes through the read-only transport. The write
    client is opened afterwards and only for the files themselves, which keeps
    every writing command in one short stretch of one run.

    Returns (location, report, where, extras). The location is carried out
    whole rather than reduced to "found or not found": the screen has a
    different heading, a different colour and different advice for each way
    this can end, and none of that can be recovered once the answer has become
    a boolean. extras is whatever the screen's own scan_extras read while the
    console was open, and is empty for a title that needs nothing.

    folder is a path the user typed. Where there is one the search is set
    aside altogether and that folder is what is read, which is the whole point
    of having asked them.
    """
    progress({"stage": "find",
              "message": ("reading the folder you gave" if folder
                          else "looking for the game on the console"),
              "done": 0, "total": 1})
    detector = detect.find_installations if detect is not None else None
    extra = {}
    with open_lister(host) as lister:
        if folder:
            location = _typed_location(lister, folder)
        else:
            location = flow.locate(lister, title_key, detector=detector,
                                   wanted=wanted)
        if extras is not None:
            extra = extras(lister) or {}

    if control.cancelled:
        return None, None, "", extra
    if not location.ready:
        return location, None, "", extra

    if len(location.title_ids) > 1 and not wanted:
        # Several supported releases are installed and nobody has said which.
        # The scan stops here rather than reading one of them. Everything this
        # screen shows afterwards is a statement about one particular folder,
        # and a page of facts about the wrong folder is worse than a question.
        return location, None, "", extra
    title_id = location.title_id
    with open_writer(host) as writer:
        report = flow.scan(writer, tool, title_id, progress=progress,
                           title_key=title_key)
    return location, report, _release_label(title_id), extra


@register
class BlackOpsTwoPatcher(PatcherScreen):
    title_key = "bo2"
    key = "bo2"
    title = "Black Ops II patch"
    blurb = ("Stops the freeze that happens whenever a PSN session becomes "
             "active.")
    tile = "B2"
    order = 10


@register
class ModernWarfareThreePatcher(PatcherScreen):
    title_key = "mw3"
    key = "mw3"
    title = "Modern Warfare 3 patch"
    blurb = ("Stops multiplayer lobbies dropping you back to the menu on a "
             "newer PSN account.")
    tile = "M3"
    order = 20
    # Two things wrong rather than one. The HEN reports are not understood yet
    # and saying so is better than a card that looks clean to somebody who is
    # about to hit it.


#: Said on the screen and again in the notes after a run, because it is the
#: one thing about this fix that is not true forever: the copy it reads is a
#: snapshot of whoever was signed in when it ran.
ACCOUNT_LINE = ("The fix will be tied to {label}. It reads that account's own "
                "identity, so if you sign in with a different PSN account "
                "afterwards, run this again.")

ACCOUNT_CHOICE = ("There are {count} accounts on this console. You will be "
                  "asked which one is signed in before anything is written, "
                  "with the one that signed in most recently offered first.")

#: The one thing somebody has to decide before this fix is any use to them,
#: said on the screen and agreed to before Apply will do anything.
CAUTION = (
    "Only apply this if your rank actually resets. Accounts made before late "
    "2018 already work, and this fix would give them an identity the server "
    "does not hold.")
CAUTION_ACTION = "If your progress saves, leave this alone."

#: What the tick box beside Apply says.
CONFIRM = "My rank resets to 1 every time I play"

ACCOUNT_MISSING = (
    "No account on this console has an np_cache.dat yet. That file is written "
    "the first time an account signs in to PSN, and the fix reads the account "
    "ID out of it. Sign in to PSN once on the console and run this again.")

#: Said instead of the above when the file is there and could not be used.
#: Telling somebody to sign in to PSN when they already have, and when the
#: file is sitting in the folder they were told to look in, is the report this
#: wording exists to stop.
ACCOUNT_UNREADABLE = (
    "np_cache.dat is on this console and would not come off it, so the fix "
    "has no account ID to work from. Signing in to PSN again will not change "
    "this. What went wrong:")

#: And a third, for a file that arrived with nothing usable in it. Kept apart
#: from the one above because the advice is opposite: an account ID of zero is
#: a sign-in that has not finished, which is the one case where going back to
#: PSN is the answer.
ACCOUNT_NO_ID = (
    "np_cache.dat came off this console and the fix could not read an account "
    "ID out of it. What the file says:")


#: Whose work the Modern Warfare 2 fix is, and where it lives. Said on the
#: screen, in the About page and in the README, because all three are places
#: somebody might look and none of them is where everybody looks.
MW2_CREDIT_WORDS = "The Modern Warfare 2 fix is built from work by Jakes625."
MW2_CREDIT_LINK = "https://github.com/jacob-schroeder/IW4-Binaries"


@register
class ModernWarfareTwoPatcher(PatcherScreen):
    """Modern Warfare 2, whose correct identity the server has already sent.

    The opposite direction to Black Ops 1. That game's sign-in reply carries
    no user ID, so its fix works one out from the account ID. This one's
    reply does carry it, so the fix takes what the server said and seeds the
    game's own cache with it.

    Which is why this screen has no tick box in front of Apply and Black Ops
    1 does. Black Ops 1 replaces one way of working out an identity with
    another, and on an account the first way suited that is a change for the
    worse; this hands the game the identity the server itself is holding, and
    an account that already works is handed the one it already had.
    """

    title_key = "mw2"
    key = "mw2"
    title = "Modern Warfare 2 stats fix"
    blurb = ("Stops multiplayer showing level 1 and keeping nothing on a "
             "newer PSN account.")
    tile = "M2"
    order = 40
    CREDIT = (MW2_CREDIT_WORDS, MW2_CREDIT_LINK)

    BACKGROUND = (
        ("The identity the server holds you under is in the reply the game "
         "reads when it signs in, so the fix uses that rather than working "
         "one out. Where the reply carries no identity, the game is left to "
         "do exactly what it did before.",
         "An account that already works is handed the identity it already "
         "had, so this is not a fix that can be applied to the wrong "
         "account."),
        (f"{MW2_CREDIT_WORDS} His releases carry about twenty security "
         f"patches and a script compiler as well; none of that is in this "
         f"program, which applies the stats fix and nothing else.",
         f"His work is at {MW2_CREDIT_LINK}."),
    )


@register
class BlackOpsOnePatcher(PatcherScreen):
    """Black Ops 1, which needs two things the other two fixes do not.

    It has to know which local user is signed in, because the account ID it
    hashes is that user's and the numbered folder it lives in is not always
    00000001. And it has to put a readable copy of np_cache.dat where the game
    can open it, because the real one is mode rw------- and the game is not
    that user.
    """

    title_key = "bo1"
    key = "bo1"
    title = "Black Ops 1 stats fix"
    blurb = ("Stops multiplayer opening at rank 1 on a PSN account made after "
             "2018.")
    tile = "B1"
    # Not this fix's doing and not something this fix cures, so it is said on
    # the screen rather than left for somebody to find out in a lobby. The
    # wording stops at what has been seen: two consoles, no date, and no claim
    # about why the map packs do it.
    CONFIRM_WITH = CONFIRM

    # The only one of the three screens with anything framed above the table,
    # and it has one thing rather than the two it used to carry.
    NOTICES = (
        # It stays because it decides whether to go any further at all. The
        # fix makes the client hash the account ID. An account made before
        # Sony's 2018 change still authenticates on a hash of the online ID,
        # so applying this to one hands it an identity the server has never
        # held and breaks something that works today. Nothing on the console
        # tells the two apart, so the person at the keyboard has to, and they
        # have to do it before they press anything.
        (CAUTION, CAUTION_ACTION, "warn"),
    )

    # The map packs. Worth having and true, and it changes nothing about how
    # this fix is applied: it is not this fix's doing, it is not something
    # this fix cures, and there is no step here anybody takes differently
    # because of it. So it moved off the front of the screen, where it was the
    # second framed paragraph a user met before reaching the state of their
    # own game. The wording stops at what has been seen: two consoles, no
    # date, and no claim about why the map packs do it.
    BACKGROUND = (
        ("If you cannot find a public match, the map packs are the cause "
         "rather than this fix. Black Ops 1 will not place you in a public "
         "game while its map packs are installed. Renaming or removing them "
         "lets matchmaking work again. This has been confirmed on two "
         "consoles.",
         "This is being looked into, and the aim is a fix that leaves the "
         "map packs alone."),
    )
    # Last of the three fixes, which is the order the README lists them in.
    order = 30

    def __init__(self, services, parent=None):
        super().__init__(services, parent)
        #: Local users the fix can be run for.
        self._users = []
        #: Every local user found, usable or not. See _no_account_words.
        self._all_users = []
        #: The one the fix will be tied to, once there is an answer.
        self._user = None
        self._account_problem = ""

    # -- the account, read with the scan and settled before anything is written
    def scan_extras(self, lister):
        try:
            return {"users": npcache.users(lister)}
        except npcache.NoAccount as exc:
            return {"users": [], "problem": str(exc)}

    def on_scan_extras(self, extras):
        """Whose accounts these are, read while the console was open.

        The single account case is settled here rather than at the moment
        Apply is pressed, so that the sentence naming it is on the screen
        before anybody decides anything.
        """
        if not extras:
            # A repaint replays the last scan through _on_scanned without the
            # extras, so an empty one means "nothing new was read" rather than
            # "this console has nobody on it". Forgetting which account was
            # chosen because somebody changed theme would be a strange way to
            # lose it. A real scan always carries a users key, even when the
            # list under it is empty.
            return
        #: Everyone found, including the accounts the fix cannot use. Kept so
        #: that "there is no such file" and "the file is there and would not
        #: read" can be told apart when there is nothing usable.
        self._all_users = list(extras.get("users") or [])
        self._users = npcache.with_cache(self._all_users)
        # One candidate is the answer. More than one is a question, and it is
        # put at the point where it matters rather than on the way in.
        self._user = self._users[0] if len(self._users) == 1 else None
        self._account_problem = extras.get("problem", "")

    def patch_ready(self):
        if self._account_problem:
            return False, self._account_problem
        if not self._users:
            return False, self._no_account_words()
        if len(self._users) == 1:
            # Nothing to choose between. Asking somebody to confirm the only
            # answer is a question that teaches them to click through
            # questions. The screen says which account it is instead, above
            # the button they are about to press.
            self._user = self._users[0]
            return True, ""
        labels = [person.label for person in self._users]
        # The newest np_cache.dat first, which is the account signed in now,
        # and it starts selected. The list is short and a person reading their
        # own PSN name knows at a glance whether the top one is right.
        chosen, said_yes = QInputDialog.getItem(
            self, "Which account is signed in?",
            "The fix reads the identity of the account signed in on the "
            "console. There is more than one account here.\n\nThe one that "
            "signed in most recently is first and is already selected. If "
            "you pick the wrong one the fix will do nothing, and running it "
            "again with the right one puts it right.",
            labels, 0, False)
        if not said_yes:
            return False, ""
        self._user = self._users[labels.index(chosen)]
        return True, ""

    def patch_context(self):
        # Nothing. The one thing the fix needs that is not in the image is
        # this file's own content ID, and the flow takes that off the file it
        # is about to change rather than being told it from here.
        return {}

    def content_id(self):
        """The content ID of the file the fix changes, or "".

        Off the file rather than off the folder it sits in. The folder can be
        renamed and often has been; the content ID is in the SELF header and
        travels with the binary.
        """
        if self._scan is None:
            return ""
        for item in self._scan.files:
            if item.site is not None and item.content_id:
                return item.content_id
        return ""

    def patch_extras(self, writer, workdir):
        """A fresh copy of np_cache.dat, every time.

        Re-read rather than kept from the scan: the point of the copy is that
        it is the account signed in now, and a console can have been signed
        out of and into something else since the table on this screen was
        drawn.
        """
        if self._user is None:
            raise npcache.NoAccount(ACCOUNT_MISSING)
        content_id = self.content_id()
        if not content_id:
            raise npcache.NoAccount(
                "the content ID could not be read off the multiplayer "
                "binary, and the copy has to go in the folder that names, so "
                "nothing has been written.")
        raw = npcache.read_for(writer, self._user.folder)
        # Reading the account ID here is not for the cave, which reads the
        # file itself. It is so that a file with nothing usable in it stops
        # the run before anything is written rather than producing a patch
        # that quietly falls back to the old behaviour.
        npcache.account_id(raw)
        return (npcache.place(writer, content_id, raw, workdir),)

    def next_step_words(self):
        """Put the stock files back, run this, then change them again.

        No link to the manual sequence here. Sending somebody with a modified
        game to a page that will patch it anyway means they end up with this
        fix applied on top of somebody else's changes to the same binary, and
        nobody can then say which of the two is responsible for whatever
        happens next. The order that works is the plain one.
        """
        return ("If the game files have already been modified, for example by "
                "a mod menu that replaces the eboot, this tool will not touch "
                "them. Put the stock files back on the console first, run "
                "this fix on those, and then apply your own changes again on "
                "top of the patched files.")

    def _no_account_words(self):
        """Why no account can be used, told apart from each other.

        Three different things end up here and only one of them is answered
        by signing in to PSN: a file that is not there, a file that would not
        come off the console, and a file whose first eight bytes are not an
        account ID.
        """
        if npcache.none_have_the_file(self._all_users):
            return ACCOUNT_MISSING
        stuck = npcache.why_unreadable(self._all_users)
        if stuck:
            return ACCOUNT_UNREADABLE + "\n\n" + "\n".join(stuck)
        empty = npcache.why_no_account_id(self._all_users)
        if empty:
            return ACCOUNT_NO_ID + "\n\n" + "\n".join(empty)
        return ACCOUNT_MISSING

    def _plan(self, report):
        lines = super()._plan(report)
        if not writing_anything(report):
            return lines
        if self._account_problem:
            lines.append(self._account_problem)
        elif not self._users:
            lines.append(self._no_account_words())
        elif self._user is not None:
            lines.append(ACCOUNT_LINE.format(label=self._user.label))
        else:
            lines.append(ACCOUNT_CHOICE.format(count=len(self._users)))
        return lines


def writing_anything(report):
    """Whether this run would write a binary at all.

    The account only matters when something is going to be patched. Saying
    which account a fix would be tied to, on a console where the fix is
    already applied and there is nothing to do, is a sentence about a thing
    that is not going to happen.
    """
    return bool(report is not None and report.chosen)
