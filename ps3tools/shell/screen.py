"""The published screen interface. Frozen.

Every tool in this application is a Screen. The launcher knows nothing about
any of them beyond what is on this class, which is what makes adding a fourth
card a matter of writing one module and registering it.

This file is the contract between the shell and the screens. It is published
before the screens are written and does not change afterwards: a screen that
believes the interface is wrong stops and says so rather than editing it.

Three things live here because all four screens need them and none of them owns
them: the shared connection state, the background task helper, and the theme
token list.

Threading rule, and it is not negotiable: **no screen calls into ps3diag or
ps3tools.patching on the GUI thread.** Collection and patching both talk to a
console over a network that may be slow or gone, and a blocked GUI thread is a
frozen window. Everything goes through Services.submit, which runs the callable
on a worker and delivers the result back on the GUI thread.
"""

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QWidget

# --- connection ------------------------------------------------------------

CONNECTION_STATES = ("unknown", "checking", "connected", "unreachable")
SCAN_STATES = ("idle", "scanning", "found", "none", "failed")


class ConnectionState(QObject):
    """The console address and whether we can reach it. One instance, shared.

    Connection and scan are deliberately two separate things. The old tkinter
    window conflated them and would sit there saying "No PS3 found on this
    network" in red while a collection from the typed address was working
    perfectly. The rule that fixes it:

      connection  describes the address currently in the box. It is the only
                  thing the shell's status bar is allowed to show.
      scan        describes the last search of the subnet. It belongs beside
                  the Find button and nowhere else, and it never colours the
                  overall state.

    A scan that found nothing says nothing about a console the user typed in by
    hand, and must not be allowed to claim otherwise.
    """

    changed = Signal()

    def __init__(self, host="", parent=None):
        super().__init__(parent)
        self._host = host
        self._connection = "unknown"
        self._connection_detail = ""
        self._banner = ""
        self._scan = "idle"
        self._scan_detail = ""
        self._candidates = []

    # -- address
    @property
    def host(self):
        return self._host

    def set_host(self, host):
        host = (host or "").strip()
        if host == self._host:
            return
        self._host = host
        # A new address has not been reached yet, and the old verdict says
        # nothing about it.
        self._connection = "unknown"
        self._connection_detail = ""
        self._banner = ""
        self.changed.emit()

    # -- reachability of that address
    @property
    def connection(self):
        return self._connection

    @property
    def connection_detail(self):
        return self._connection_detail

    @property
    def banner(self):
        """The webMANftpd greeting, when one has been seen."""
        return self._banner

    @property
    def connected(self):
        return self._connection == "connected"

    def set_connection(self, state, detail="", banner=None):
        if state not in CONNECTION_STATES:
            raise ValueError(f"unknown connection state {state!r}")
        self._connection = state
        self._connection_detail = detail or ""
        if banner is not None:
            self._banner = banner
        self.changed.emit()

    # -- the subnet search, which is a different question
    @property
    def scan(self):
        return self._scan

    @property
    def scan_detail(self):
        return self._scan_detail

    @property
    def candidates(self):
        return list(self._candidates)

    def set_scan(self, state, detail="", candidates=None):
        if state not in SCAN_STATES:
            raise ValueError(f"unknown scan state {state!r}")
        self._scan = state
        self._scan_detail = detail or ""
        if candidates is not None:
            self._candidates = list(candidates)
        self.changed.emit()


# --- background work -------------------------------------------------------

class TaskControl:
    """Handed to the worker callable. The only way it talks back."""

    def __init__(self, task):
        self._task = task

    def progress(self, value):
        """Emitted on the GUI thread. Any object; the screen decides what."""
        self._task.progress.emit(value)

    @property
    def cancelled(self):
        return self._task.is_cancelled


class Task(QObject):
    """One unit of background work.

    finished carries the callable's return value, failed carries a message
    already fit to show a user. Both arrive on the GUI thread.
    """

    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)
    done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self):
        """Asks the worker to stop. It stops at its next check, not at once."""
        self._cancelled = True

    @property
    def is_cancelled(self):
        return self._cancelled


class _Runner(QRunnable):
    def __init__(self, task, function):
        super().__init__()
        self._task = task
        self._function = function

    @Slot()
    def run(self):
        try:
            result = self._function(TaskControl(self._task))
        except Exception as exc:
            # A traceback is no use to the audience. The screen turns this into
            # something actionable; what must not happen is the worker thread
            # dying silently and the screen waiting forever.
            #
            # It is written to a crash log as well, without a dialog. Work runs
            # on a QThreadPool, so neither sys.excepthook nor
            # threading.excepthook ever sees a failure here: without this line
            # the most likely crash in the program is the one that leaves
            # nothing to send. The screen is already telling the user, so the
            # log is for whoever they send it to.
            self._log_crash(exc)
            self._task.failed.emit(f"{exc.__class__.__name__}: {exc}")
        else:
            if not self._task.is_cancelled:
                self._task.finished.emit(result)
        finally:
            self._task.done.emit()

    @staticmethod
    def _log_crash(exc):
        """Never allowed to make things worse than the failure it is recording."""
        try:
            from ..crashreport import handle
            handle(type(exc), exc, exc.__traceback__, dialog=False)
        except Exception:
            pass


# --- theme -----------------------------------------------------------------

# The tokens a screen may ask for. The shell decides what they are in the
# current light or dark theme; a screen never names a colour of its own, which
# is what keeps both themes legible without every screen being checked twice.
THEME_TOKENS = (
    "bg", "surface", "surface_alt", "border",
    "text", "text_dim", "accent", "accent_text",
    "ok", "warn", "error", "info",
)


class Theme(QObject):
    """Interface only. The shell provides the real one."""

    changed = Signal()

    def colour(self, token):
        raise NotImplementedError

    @property
    def dark(self):
        raise NotImplementedError


# --- services --------------------------------------------------------------

class Services(QObject):
    """Everything a screen is given. Nothing else is shared.

    Screens receive one of these and hold no global state of their own.
    """

    def __init__(self, connection, theme, settings=None, parent=None):
        super().__init__(parent)
        self.connection = connection
        self.theme = theme
        self.settings = settings if settings is not None else {}
        self._pool = QThreadPool.globalInstance()
        self._tasks = []

    def submit(self, function):
        """Runs function(control) on a worker. Returns a Task immediately.

        The Task is kept referenced until it finishes, because a Task that is
        garbage collected mid-flight takes its signals with it and the screen
        waits for a result that can never arrive.
        """
        task = Task(self)
        self._tasks.append(task)
        task.done.connect(lambda: self._forget(task))
        self._pool.start(_Runner(task, function))
        return task

    def _forget(self, task):
        if task in self._tasks:
            self._tasks.remove(task)

    def wait(self, milliseconds=10000):
        """Blocks until the worker pool is idle. For tests and for shutdown."""
        return self._pool.waitForDone(milliseconds)


# --- the screen itself -----------------------------------------------------

class Screen(QWidget):
    """One tool.

    Subclasses set the five class attributes, which is the whole of what the
    launcher needs, and implement on_enter. Everything else is optional.
    """

    #: stable identifier, used for navigation and in settings
    key = ""
    #: card heading
    title = ""
    #: one line under the heading on the card. A sentence, not a slogan.
    blurb = ""
    #: two or three letters for the card's tile. Letters only, never an emoji.
    tile = ""
    #: card order on the home screen, lowest first
    order = 100

    #: True while work is in flight; the shell shows it and blocks navigation
    busy_changed = Signal(bool)
    #: a short line for the shell's status area
    status_message = Signal(str)
    #: ask the shell to go back to the launcher
    request_home = Signal()

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services

    # -- lifecycle, all optional
    def on_enter(self):
        """Navigated to. A screen that scans on entry starts it here."""

    def on_leave(self):
        """Navigated away. Cancel anything in flight."""

    def can_leave(self):
        """False to refuse navigation, e.g. partway through writing a file."""
        return True

    def leave_blocked_reason(self):
        """Why can_leave() said no, in words fit to show the user.

        Optional. The default is deliberately vague because a screen that
        refuses navigation without overriding this knows something the shell
        does not; overriding it lets the patcher say "the EBOOT is half
        written" rather than the shell guessing.
        """
        return "This tool is in the middle of something that cannot be " \
               "interrupted. Wait for it to finish."

    # -- convenience
    @property
    def connection(self):
        return self.services.connection

    @property
    def theme(self):
        return self.services.theme

    def submit(self, function):
        return self.services.submit(function)
