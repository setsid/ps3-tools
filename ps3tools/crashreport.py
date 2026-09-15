"""What happens when the program falls over.

An unhandled exception used to take the window with it and leave the user with
nothing to send. This turns one into a file they can find and an explanation
they can act on, and it does it without the program reaching the network: there
is a live console on the same LAN as most of the people running this, and a
crash reporter that phones home is a promise broken at the worst possible
moment. Nothing here sends anything. The user attaches the file themselves.

Three rules shape the code below.

  * Nothing in here may raise. A crash handler that crashes is worse than no
    crash handler at all -- it replaces a bad traceback with a confusing one
    and loses the original. Every step is wrapped, every step has a fallback,
    and the re-entrancy guard stops a fault inside the handler recursing.
  * The log goes through ps3diag.redaction.redact before it is written, on the
    same terms as the diagnostic zip. An exception message is one of the most
    likely places for an IDPS or a MAC address to end up, because it is usually
    a value that came back from the console.
  * The dialog must stand up on its own. Crashes during start-up are the ones
    worth catching most and there is no main window to parent to yet, so this
    builds its own QApplication if it has to and borrows nothing from the shell.
"""

import datetime
import os
import platform
import sys
import tempfile
import threading
import traceback
from collections import deque
from urllib.parse import quote

from ps3diag import config, redaction

from . import APP_NAME, FULL_NAME, VERSION

SUPPORT_ADDRESS = "setsid.research@proton.me"

#: Enough to show what the user was doing, few enough that the trail cannot
#: grow without bound in a session left open for a week.
BREADCRUMB_LIMIT = 50

#: Filename stem. Named after the program rather than the display name so the
#: file sorts beside the diagnostic zip in the same folder.
FILE_STEM = APP_NAME.replace(" ", "-")

WHAT_HAPPENED = (
    "{name} has stopped unexpectedly. Whatever you were doing did not finish, "
    "and anything not already saved has been lost. Your PS3 has not been "
    "changed by this.")

NOTHING_SENT = (
    "Nothing has been sent anywhere. This program does not report crashes by "
    "itself and never contacts anything except the console you point it at. "
    "If you would like this looked at, email the file yourself with it "
    "attached.")

REDACTED_NOTE = (
    "Console identifiers have been replaced with placeholders in the file "
    "before it was written, the same way the diagnostic zip does it. That "
    "covers the IDPS, the PSID, MAC addresses, and account and sign-in IDs.")

#: Written in place of the report body if redaction itself fails. Withholding
#: the traceback is the right way round: an unredacted log could carry the
#: user's IDPS to an inbox, and the promise that it will not is worth more than
#: one traceback.
REDACTION_FAILED = (
    "The report body was withheld.\n\n"
    "Redaction failed while this file was being written, and the identifiers "
    "in the report could not be replaced. Writing it unredacted would have put "
    "console identifiers in a file meant to be emailed, so the body was "
    "dropped instead. The exception type is above; there is nothing else to "
    "recover from here.")

_breadcrumbs = deque(maxlen=BREADCRUMB_LIMIT)
_lock = threading.Lock()

#: Set for the duration of a report. A fault raised while handling a fault --
#: a dialog that cannot be built on a headless box, a disk with nothing left on
#: it -- must not come back round through the hook and start again.
_handling = False


# --- breadcrumbs -----------------------------------------------------------

def note(action):
    """Records one thing the user did. Never raises.

    Called from UI code on the path a user is already walking, so it takes
    whatever it is given and makes a string of it rather than being fussy about
    the argument: a TypeError from the crash reporter's own breadcrumb trail
    would be a poor joke.
    """
    try:
        text = str(action)
    except Exception:
        # A repr that raises is rare and entirely possible; a half-built object
        # in a screen that is about to crash is exactly where it happens.
        text = "<action could not be described>"
    try:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
    except Exception:
        stamp = "--:--:--"
    with _lock:
        _breadcrumbs.append(f"{stamp}  {text}")


def breadcrumbs():
    """The trail, oldest first. A copy: the caller cannot disturb the deque."""
    with _lock:
        return list(_breadcrumbs)


def clear():
    """Empties the trail. For tests, and for a session that has been reported
    on already -- the next crash wants its own context, not the last one's."""
    with _lock:
        _breadcrumbs.clear()


# --- the report ------------------------------------------------------------

def _pyside_version():
    try:
        import PySide6
        return PySide6.__version__
    except Exception:
        return "not available"


def _qt_version():
    try:
        from PySide6.QtCore import qVersion
        return qVersion()
    except Exception:
        return "not available"


def environment():
    """The version lines. Deliberately says nothing about the machine beyond
    its OS: the hostname and the user's name are identifiers too, and neither
    of them has ever helped anybody read a traceback."""
    lines = [
        f"Application: {FULL_NAME} {VERSION}",
        f"Frozen build: {'yes' if getattr(sys, 'frozen', False) else 'no'}",
    ]
    try:
        lines.append(f"OS:          {platform.platform()}")
    except Exception:
        lines.append(f"OS:          {sys.platform}")
    lines.append(f"Python:      {platform.python_version()} "
                 f"({platform.python_implementation()})")
    lines.append(f"PySide6:     {_pyside_version()}")
    lines.append(f"Qt:          {_qt_version()}")
    return lines


def _traceback_text(exc_type, exc_value, exc_tb):
    """The traceback, or the best description available of why there isn't one.

    format_exception is given arbitrary objects here -- a hook can be called
    with anything, and an exception whose __str__ raises is a real thing -- so
    the failure of the formatter is a case to answer rather than a surprise.
    """
    try:
        lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        text = "".join(lines).strip()
        if text:
            return text
    except Exception:
        pass
    # Falling back by degrees: the type name alone is still worth having, and
    # it is the piece least likely to be the thing that could not be formatted.
    try:
        name = getattr(exc_type, "__name__", None) or repr(exc_type)
    except Exception:
        name = "unknown exception"
    return (f"{name}\n\n"
            f"The traceback could not be formatted. The exception did not "
            f"survive being turned into text.")


def compose(exc_type=None, exc_value=None, exc_tb=None, thread_name=None):
    """The whole report, before redaction. Never raises."""
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        stamp = "unknown"
    parts = [f"{FULL_NAME} crash report",
             f"Written: {stamp}",
             ""]
    parts.extend(environment())
    if thread_name:
        parts.append(f"Thread:      {thread_name}")
    # Not "what the user did last": the redactor's sign-in ID rule reads the
    # word "user" as a label and replaces the next word in the heading with a
    # placeholder. Cheaper to word the heading around it than to loosen a rule
    # whose looseness is the point.
    parts.extend(["", "-- recent actions " + "-" * 57, ""])
    trail = breadcrumbs()
    if trail:
        parts.extend(trail)
    else:
        parts.append("(nothing was recorded)")
    parts.extend(["", "-- traceback " + "-" * 62, ""])
    parts.append(_traceback_text(exc_type, exc_value, exc_tb))
    parts.append("")
    return "\n".join(parts)


def redacted(text):
    """The report with console identifiers replaced. Returns (text, ok).

    ok is False when redaction could not be done, in which case the text
    returned is a note saying so rather than the original: see REDACTION_FAILED.
    """
    try:
        cleaned, _findings = redaction.redact(text)
    except Exception as exc:
        header = (f"{FULL_NAME} crash report\n\n"
                  f"Redaction error: {exc.__class__.__name__}\n\n")
        return header + REDACTION_FAILED + "\n", False
    return cleaned, True


# --- where it goes ---------------------------------------------------------

def candidate_dirs(settings=None):
    """Somewhere the user will find it, in order of how obvious it is.

    The diagnostic zip goes to the configured output folder or the Desktop, so
    a crash log goes to the same place: someone told to "send me the file"
    should only ever have to look in one folder. The last two are there so that
    a machine with a read-only Desktop still gets a log rather than nothing.
    """
    paths = []
    try:
        configured = (settings or {}).get("output_dir") or ""
    except Exception:
        configured = ""
    if configured:
        paths.append(configured)
    for finder in (config.desktop_dir, config.user_dir, tempfile.gettempdir):
        try:
            found = finder()
        except Exception:
            continue
        if found:
            paths.append(found)
    return paths


def filename(when=None):
    when = when or datetime.datetime.now()
    return f"{FILE_STEM}-crash-{when.strftime('%Y%m%d-%H%M%S')}.txt"


def write_log(text, settings=None, directory=None):
    """Redacts the report and writes it. Returns the path, or None.

    Redaction happens here rather than at the call site so that there is one
    door into the file and no way to write a report that skipped it.
    """
    body, _ok = redacted(text)
    name = filename()
    targets = [directory] if directory else candidate_dirs(settings)
    for folder in targets:
        path = os.path.join(folder, name)
        try:
            os.makedirs(folder, exist_ok=True)
            with open(path, "w", encoding="utf-8", errors="replace") as handle:
                handle.write(body)
            return path
        except (OSError, ValueError, TypeError):
            continue
    return None


# --- the dialog ------------------------------------------------------------

def mailto_url(path=None):
    """A mailto: link with the version in the subject and the log path in the
    body. No attachment: mailto cannot carry one, and even if it could, this
    program does not put the user's file anywhere they did not put it."""
    subject = f"{FULL_NAME} {VERSION} crash report"
    where = path or "(the log file could not be written)"
    body = ("Something went wrong in the program.\n\n"
            f"The log file is at:\n{where}\n\n"
            "Please attach that file to this email before sending it. "
            "Nothing was sent automatically.\n\n"
            "What I was doing at the time:\n")
    # The @ stays literal: RFC 6068 wants the address itself readable, and a
    # percent-encoded one is not opened by every mail client that exists.
    return (f"mailto:{quote(SUPPORT_ADDRESS, safe='@')}"
            f"?subject={quote(subject, safe='')}"
            f"&body={quote(body, safe='')}")


def ensure_application():
    """A QApplication to hang the dialog on, existing or new.

    A crash during start-up is the one this matters for. Returns None if Qt
    cannot be brought up at all, in which case the caller falls back to stderr.
    """
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:
        return None
    try:
        existing = QApplication.instance()
        if existing is not None:
            return existing
        if threading.current_thread() is not threading.main_thread():
            # Same fatality as building a widget off the GUI thread, one step
            # earlier: a QApplication may only be created on the main thread.
            # A worker that failed before the application existed gets the file
            # and stderr, which is better than aborting the process.
            return None
        return QApplication(sys.argv[:1] or [FILE_STEM])
    except Exception:
        return None


def build_dialog(path=None, parent=None, report=None):
    """The dialog, built and not shown. Raises only if Qt itself is unusable.

    Parented to nothing by default. The main window may not exist -- may never
    have existed -- and a dialog that needs one is a dialog that is missing
    exactly when it is wanted.

    report is only used when there is no path. If the file could not be written
    the user is back to having nothing to send, which is the problem this whole
    module exists to fix, so the report goes on the screen where it can at
    least be selected and copied. It must already be redacted.
    """
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel,
                                   QPlainTextEdit, QVBoxLayout)

    dialog = QDialog(parent)
    dialog.setWindowTitle(f"{APP_NAME} has stopped")
    dialog.setMinimumWidth(520)

    rows = QVBoxLayout(dialog)
    rows.setContentsMargins(20, 18, 20, 16)
    rows.setSpacing(12)

    def line(text, rich=False):
        label = QLabel(text, dialog)
        label.setWordWrap(True)
        if rich:
            label.setTextFormat(Qt.TextFormat.RichText)
            # mailto only. Nothing in this dialog opens an http URL, which is
            # the whole point of the crash reporter not touching the network.
            label.setOpenExternalLinks(True)
        else:
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
        rows.addWidget(label)
        return label

    head = line(WHAT_HAPPENED.format(name=FULL_NAME))
    font = head.font()
    font.setBold(True)
    head.setFont(font)

    if path:
        line(f"A log of what happened was saved to:\n{path}")
        line(REDACTED_NOTE)
    else:
        line("A log file could not be written. There was nowhere on this "
             "machine to put it, so the details are below instead. Select "
             "them and copy them into the email.")
        if report:
            details = QPlainTextEdit(report, dialog)
            details.setReadOnly(True)
            details.setMinimumHeight(180)
            rows.addWidget(details)
    line(NOTHING_SENT)

    address = mailto_url(path)
    line(f'Email it to <a href="{address}">{SUPPORT_ADDRESS}</a>, attaching '
         f'the file above.', rich=True)

    buttons = QDialogButtonBox(dialog)
    close = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
    close.clicked.connect(dialog.reject)
    if path:
        folder = buttons.addButton("Open containing folder",
                                   QDialogButtonBox.ButtonRole.ActionRole)
        # The folder rather than the file: opening a .txt launches whatever the
        # machine thinks owns text files, which on a fresh Windows box is a
        # dialog asking the user to choose one.
        folder.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(os.path.dirname(os.path.abspath(path)))))
    rows.addWidget(buttons)

    # Held so the caller's reference is not the only thing keeping the link
    # target alive when the dialog is shown non-modally.
    dialog.log_path = path
    dialog.mailto = address
    return dialog


def _on_gui_thread(application):
    """Whether the caller is the thread Qt's widgets belong to."""
    from PySide6.QtCore import QThread
    return QThread.currentThread() == application.thread()


def show_dialog(path=None, parent=None, report=None):
    """Builds and runs the dialog. Returns True if one was put up or queued.

    The thread check is not a nicety. threading.excepthook runs on the thread
    that failed, and building a QWidget anywhere but the GUI thread is a fatal
    error in Qt rather than an exception -- it would take the process down and
    replace the crash we are reporting with a worse one. From a worker the call
    is posted to the GUI thread instead, which also means the worker is not
    left blocked behind a modal dialog it cannot dismiss.
    """
    application = ensure_application()
    if application is None:
        return False
    if not _on_gui_thread(application):
        from PySide6.QtCore import QTimer
        # The three argument form: the context object decides which thread the
        # functor runs on. Nothing is shown if the event loop is not running,
        # which is the case where the file and stderr are all there is anyway.
        QTimer.singleShot(0, application, lambda: _show_now(path, None, report))
        return True
    return _show_now(path, parent, report)


def _show_now(path=None, parent=None, report=None):
    """The GUI thread half of show_dialog. Never raises: it is reached from a
    queued call with nobody left to catch anything it throws."""
    try:
        dialog = build_dialog(path, parent, report)
        dialog.exec()
        return True
    except Exception:
        return False


# --- the handler -----------------------------------------------------------

def _to_stderr(text, path):
    """The traceback still goes to the console, redacted like the file.

    Run from a terminal, this is how a developer sees the fault at all; run
    from the exe there is no console and the write goes nowhere, which is fine.
    """
    try:
        body, _ok = redacted(text)
        sys.stderr.write(body + "\n")
        if path:
            sys.stderr.write(f"Crash log written to {path}\n")
        sys.stderr.flush()
    except Exception:
        pass


_installed_settings = None


def configure(settings):
    """Remembers the settings every later report should use.

    install() runs before the settings file has been read, so that an
    import-time crash is still caught. That leaves it with nowhere to put a log
    but the Desktop. Calling this once the settings are loaded moves later
    reports to the configured output folder, and lets a crash reported from
    somewhere other than a hook -- a failed background task, say -- find it
    without every call site having to carry the settings around.
    """
    global _installed_settings
    _installed_settings = settings
    return settings


def handle(exc_type=None, exc_value=None, exc_tb=None, thread_name=None,
           settings=None, dialog=True):
    """Reports one crash. Returns the log path, or None. Never raises.

    Every stage is independent: a report that cannot be written still gets a
    dialog, and a dialog that cannot be built still leaves the file.
    """
    global _handling
    if _handling:
        return None
    _handling = True
    if settings is None:
        settings = _installed_settings
    try:
        try:
            report = compose(exc_type, exc_value, exc_tb, thread_name)
        except Exception:
            # compose already swallows everything it can; this is the last
            # resort so that a fault in it does not cost the user the dialog.
            report = f"{FULL_NAME} {VERSION}\n\nThe report could not be built."
        try:
            path = write_log(report, settings=settings)
        except Exception:
            path = None
        _to_stderr(report, path)
        if dialog:
            try:
                # The redacted text, not the raw report: it is only shown when
                # no file could be written, and it goes on a screen the user is
                # about to copy from.
                shown, _ok = redacted(report)
                show_dialog(path, report=None if path else shown)
            except Exception:
                # Headless, no display, Qt half torn down during shutdown. The
                # file is written by now, which is the part that matters.
                pass
        return path
    except Exception:
        return None
    finally:
        _handling = False


def install(settings=None, dialog=True):
    """Puts the hooks in. Returns the hooks that were there before.

    sys.excepthook covers the main thread and, in PySide6 6.x, exceptions
    raised inside slots as well: Qt catches them at the C++ boundary and hands
    them to the hook rather than losing them, which is checked in the tests
    because it is a behaviour of the binding rather than a promise of the API.

    threading.excepthook covers Python worker threads. It does not cover
    Services.submit, whose work runs on a QThreadPool and never touches a
    threading.Thread -- shell/screen.py's _Runner already catches there and
    turns the failure into a message on the screen, which is the better answer
    for a background task that failed on its own.
    """
    configure(settings)
    previous = (sys.excepthook, threading.excepthook)

    def is_a(exc_type, kinds):
        """issubclass, without the TypeError it raises on anything that is not
        a class. The hooks are handed whatever the caller had."""
        try:
            return exc_type is not None and issubclass(exc_type, kinds)
        except TypeError:
            return False

    def excepthook(exc_type, exc_value, exc_tb):
        # Ctrl-C and sys.exit are not crashes and must not put a dialog up.
        if is_a(exc_type, (KeyboardInterrupt, SystemExit)):
            previous[0](exc_type, exc_value, exc_tb)
            return
        handle(exc_type, exc_value, exc_tb, settings=settings, dialog=dialog)

    def thread_hook(args):
        if is_a(getattr(args, "exc_type", None), SystemExit):
            return
        name = getattr(getattr(args, "thread", None), "name", None)
        handle(getattr(args, "exc_type", None),
               getattr(args, "exc_value", None),
               getattr(args, "exc_traceback", None),
               thread_name=name, settings=settings, dialog=dialog)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    return previous
