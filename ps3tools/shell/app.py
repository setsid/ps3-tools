"""The application frame: window, navigation, connection bar, status area.

Owns no domain logic. It knows how to show screens, how to get between them
without losing anyone's work, and where the console's address is kept. What any
of that means to a PS3 is somebody else's module.

Two rules from the published interface show up repeatedly below:

  * Nothing here calls ps3diag on the GUI thread. The one thing this file does
    reach a console for -- the Check button -- goes through Services.submit.
  * The connection bar shows `connection` and never `scan`. A subnet search
    that found nothing says nothing at all about an address somebody typed in
    by hand, and the old window's habit of colouring the whole status area red
    on that basis is the bug this shell exists to stop repeating.
"""

import base64
import importlib
import ipaddress
import json
import os
import pkgutil
import sys
import threading

from PySide6.QtCore import (QByteArray, QEasingCurve, QParallelAnimationGroup,
                            QPoint, QPropertyAnimation, QSize, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices,
                           QFont, QGuiApplication, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame,
                               QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ps3diag import config, discovery
from ps3diag.parsers import SIGNATURE_THRESHOLD, looks_like_webman, \
    webman_score
from ps3diag.transport import HttpProbe, tcp_open

from .. import APP_NAME, FULL_NAME, PROJECT_URL, VENDOR, VERSION, crashreport
from . import registry
from .launcher import Launcher
from .screen import ConnectionState, Services
from .theme import AppTheme, menu_palette, qt_palette, stylesheet
from .updatebanner import UpdateBanner
from .widgets import StatusDot

#: Long enough for a console that has been asleep to wake up and answer, short
#: enough that a wrong address does not look like a hang.
CHECK_TIMEOUT = 8.0

#: How long an address gets to accept a connection on port 80.
#:
#: This was 0.4s, which is the number that makes a whole /24 finish while the
#: user is still watching, and it is too short to be safe. The first packet to
#: a console that has not been spoken to yet waits on an ARP exchange, and a
#: PS3 on Wi-Fi with power saving on can take most of a second to answer it --
#: while the Check button, which reaches the same console at a typed address,
#: allows eight. A search that times out before the console replies reports
#: exactly the same empty answer as a search of the wrong network, which is
#: why this is the first thing to raise when Find comes back with nothing.
#: 64 addresses are in flight at once, so a whole /24 is four rounds of this.
SCAN_CONNECT_TIMEOUT = 1.0

#: Only the handful of addresses that accepted a connection are fetched, so
#: this can afford to be patient. A console part way through a transfer is
#: slow to draw its own front page.
SCAN_FETCH_TIMEOUT = 4.0

#: Either may be overridden from the settings file, for a network where the
#: defaults are still not enough, without a rebuild.
SCAN_CONNECT_TIMEOUT_KEY = "scan_connect_timeout"
SCAN_FETCH_TIMEOUT_KEY = "scan_fetch_timeout"


def _timeout(settings, key, default):
    """A timeout from the settings file, or the default. Never raises, and
    never returns something daft: a nought in that file must not turn the
    search into a loop that answers nothing."""
    try:
        value = float((settings or {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return value if 0.05 <= value <= 60.0 else default


def _seconds(value):
    """A number of seconds with the right noun on the end of it."""
    return f"{value:g} second" + ("" if value == 1 else "s")

#: Slide and fade. Long enough to read as movement, short enough that nobody
#: navigating quickly is ever waiting for it.
TRANSITION_MS = 200

HOME_KEY = "__home__"

CANNOT_REACH = (
    "Could not reach {host}. Check the PS3 is switched on, that it is showing "
    "the main menu rather than running a game, that webMAN is running, and "
    "that it is plugged into the same router as this PC.")

NOT_WEBMAN = (
    "Something answered at {host}, but it is not webMAN. Check the address is "
    "the console's and not the router's, and that webMAN is running.")

CONNECTION_WORDS = {
    "unknown": ("Not checked", "text_dim"),
    "checking": ("Checking", "info"),
    "connected": ("Connected", "ok"),
    "unreachable": ("Cannot be reached", "error"),
}


# --- settings --------------------------------------------------------------

#: Beside the exe, falling back to per-user, which is what ps3diag.config
#: already works out. Its own load/save filter to the diagnostic tool's keys,
#: so the shell keeps a file of its own in the same two places rather than
#: quietly losing every setting a screen stores.
# Named after the exe rather than after the display name, so a
# user looking at the folder can see which program owns it.
SETTINGS_FILE = "ps3-tools.json"


def settings_paths():
    return [os.path.join(config.app_dir(), SETTINGS_FILE),
            os.path.join(config.user_dir(), SETTINGS_FILE)]


def load_settings():
    for path in settings_paths():
        try:
            with open(path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(stored, dict):
            stored["_path"] = path
            return stored
    # First run of the new shell on a machine that has run the old diagnostic
    # tool: the address it remembered is still the right one.
    return {"host": config.load().get("ip", "")}


def save_settings(settings):
    """Returns the path written, or None. Never raises: failing to remember a
    window size is not worth interrupting anything over."""
    payload = {}
    for key, value in settings.items():
        if key.startswith("_"):
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            # One screen storing something unserialisable must not cost every
            # other screen its settings.
            continue
        payload[key] = value
    for path in settings_paths():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            return path
        except OSError:
            continue
    return None


# --- screen discovery ------------------------------------------------------

#: The screens this build is known to ship. Enumeration is still the way a new
#: tool arrives from a source checkout, but pkgutil walks a directory and a
#: onefile exe has no directory to walk: the modules live in an archive, so
#: iter_modules finds nothing there and nothing registers itself.
#:
#: This is not why the home screen came back empty after a visit to a tool --
#: that was the launcher measuring its card grid while the cards were still
#: hidden, and is fixed in launcher.py -- but it is a real hazard of its own
#: and the belt to the build's braces, which now names the same modules to
#: PyInstaller as hidden imports. _bundled_screens() below names them in real
#: import statements as well, because the exe build's import analysis cannot
#: follow a module name assembled at run time.
KNOWN_SCREEN_MODULES = ("about", "diagnostics", "patcher")


def _bundled_screens():
    """Never called. It exists to be read by PyInstaller, which follows import
    statements and not strings; without it the screen modules are absent from
    the exe rather than merely undiscovered in it."""
    from ..screens import about, diagnostics, patcher  # noqa: F401
    return (about, diagnostics, patcher)


def _discover_screen_modules(package):
    """Module names under the screens package, enumerated where that works.

    Returns (names, enumerated). enumerated is False when the environment
    cannot be walked, which is the frozen build.
    """
    try:
        found = [info.name for info in pkgutil.iter_modules(
            list(getattr(package, "__path__", [])))
            if not info.name.startswith("_")]
    except Exception:
        found = []
    return found, bool(found)


def load_screen_modules():
    """Import every module under ps3tools.screens so it can register itself.

    Enumerated first, so a new tool is a new file. A module that fails to
    import is reported and stepped over: one broken screen must not take the
    other three down with it.
    """
    problems = []
    try:
        package = importlib.import_module("ps3tools.screens")
    except Exception as exc:
        return [f"The tool modules could not be loaded: {exc}"]

    discovered, enumerated = _discover_screen_modules(package)
    names = list(discovered)
    names.extend(name for name in KNOWN_SCREEN_MODULES if name not in names)

    for name in names:
        full = f"{package.__name__}.{name}"
        try:
            importlib.import_module(full)
        except ModuleNotFoundError as exc:
            # A name from the fallback list that this build genuinely does not
            # carry is not worth complaining about; anything else is.
            if name in discovered or exc.name != full:
                problems.append(f"{name} could not be loaded: {exc}")
        except Exception as exc:
            problems.append(f"{name} could not be loaded: {exc}")

    if not enumerated and not registry.screens():
        problems.append(
            "No tool modules could be found in this build. Reinstall it or "
            "ask whoever sent it to you for the full version.")
    return problems


# --- chrome pieces ---------------------------------------------------------

def _chevron(colour, size=14):
    """A left pointing chevron, painted. Not an emoji and not an icon font:
    both are a font substitution away from a box, and this has to survive a
    Windows machine with nothing installed on it."""
    ratio = 2  # Twice the pixels, same coordinates: crisp on a HiDPI display.
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(colour), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    path.moveTo(size * 0.63, size * 0.18)
    path.lineTo(size * 0.33, size * 0.5)
    path.lineTo(size * 0.63, size * 0.82)
    painter.drawPath(path)
    painter.end()
    return pixmap


def back_arrow_icon(colour, dim_colour, size=14):
    """The arrow in both the states the button has: enabled, and greyed while
    a screen is busy."""
    icon = QIcon(_chevron(colour, size))
    icon.addPixmap(_chevron(dim_colour, size), QIcon.Mode.Disabled)
    return icon


class LinkLabel(QLabel):
    """Text that behaves like a link and looks like one.

    The system browser rather than anything embedded: this program has no
    business rendering a web page, and the user's own browser is the only one
    they have already decided to trust.
    """

    def __init__(self, text, url, parent=None):
        super().__init__(text, parent)
        self.url = url
        self.setObjectName("brandLink")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Open {url} in your browser.")
        self.setAccessibleName(f"{text}, opens {url}")

    def open(self):
        return QDesktopServices.openUrl(QUrl(self.url))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.open()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._underline(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._underline(False)
        super().leaveEvent(event)

    def _underline(self, on):
        font = QFont(self.font())
        font.setUnderline(on)
        self.setFont(font)


# --- connection bar --------------------------------------------------------

#: Kept for the window's own use; the ranking itself lives in ps3diag so the
#: same rules apply wherever the search is driven from.
VIRTUAL_SUBNETS = discovery.VIRTUAL_SUBNETS


def _private_address(addresses):
    """The single address most likely to share a wire with the console.

    Thin now: the adapter carrying the default route wins, and everything
    below that is ranked in discovery. Kept because a caller that genuinely
    wants one answer should not have to unpack a list of them.
    """
    return discovery.preferred_address(addresses)


def _listed(items):
    """Items as English: "a", "a and b", "a, b and c"."""
    items = [item for item in items if item]
    if len(items) < 2:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def _networks_label(addresses):
    """The searched networks, as a sentence fragment: "a/24, b/24 and c/24"."""
    networks = []
    for address in addresses:
        network = _subnet_label(address)
        if network and network not in networks:
            networks.append(network)
    return _listed(networks)


def _subnet_label(address):
    """The /24 an address sits in, written the way the search reads it."""
    try:
        network = ipaddress.ip_network(f"{address}/24", strict=False)
    except ValueError:
        return ""
    return str(network)


class CandidateDialog(QDialog):
    """Which of these is yours. Only ever shown when the search found more
    than one thing that looks like a console."""

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("More than one PS3 answered")
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(12)

        blurb = QLabel(
            "These answered like a PS3 running webMAN. Pick the one you want "
            "to work on; the address goes into the box and nothing is done to "
            "it until you ask.", self)
        blurb.setWordWrap(True)
        column.addWidget(blurb)

        self._list = QListWidget(self)
        for candidate in candidates:
            text = (f"{candidate.address}  -  {candidate.title}"
                    if candidate.title else candidate.address)
            item = QListWidgetItem(text, self._list)
            item.setData(Qt.ItemDataRole.UserRole, candidate.address)
        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self.accept)
        column.addWidget(self._list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    def chosen(self):
        item = self._list.currentItem()
        return "" if item is None else item.data(Qt.ItemDataRole.UserRole)


class ConnectionBar(QWidget):
    """The console's address, entered once and shared by every screen.

    The bar owns both questions and keeps them apart. The status pill shows
    `connection` and `connection_detail` and nothing else. The search of the
    subnet shows `scan` and `scan_detail`, in its own label beside the Find
    button, in the dim text colour, always: the old window coloured the whole
    status area red because a scan found nothing, while a collection from the
    typed address was working perfectly. A scan that found nothing says
    nothing at all about an address somebody typed in by hand.
    """

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.setObjectName("connectionBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.services = services
        self._connection = services.connection
        self._theme = services.theme

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 10, 24, 10)
        column.setSpacing(4)
        line = QWidget(self)
        column.addWidget(line)
        row = QHBoxLayout(line)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        label = QLabel("PlayStation 3 address", self)
        row.addWidget(label)

        self._address = QLineEdit(self._connection.host, self)
        self._address.setPlaceholderText("for example 192.168.1.50")
        self._address.setClearButtonEnabled(True)
        self._address.setFixedWidth(200)
        self._address.setAccessibleName("PlayStation 3 address")
        self._address.textChanged.connect(self._address_typed)
        self._address.returnPressed.connect(self.check)
        row.addWidget(self._address)

        self._check_button = QPushButton("Check", self)
        self._check_button.setProperty("primary", True)
        self._check_button.setToolTip(
            "Ask the console at that address whether it is there.")
        self._check_button.clicked.connect(self.check)
        row.addWidget(self._check_button)

        self._find_button = QPushButton("Find my PS3", self)
        self._find_button.setToolTip(
            "Look for a console on this network and fill the address in.")
        self._find_button.clicked.connect(self.find)
        row.addWidget(self._find_button)

        row.addSpacing(8)
        self._dot = StatusDot(parent=self)
        row.addWidget(self._dot)
        self._state_label = QLabel("", self)
        font = QFont(self._state_label.font())
        font.setWeight(QFont.Weight.DemiBold)
        self._state_label.setFont(font)
        row.addWidget(self._state_label)

        row.addStretch(1)

        # The detail is where "check the PS3 is switched on" lives, so it gets
        # a line of its own rather than being elided into uselessness beside
        # everything else. The bar is only that tall when there is something
        # to say.
        self._detail_label = QLabel("", self)
        self._detail_label.setObjectName("dim")
        self._detail_label.setWordWrap(True)
        self._detail_label.setVisible(False)
        column.addWidget(self._detail_label)

        # The search gets a label of its own, under the button that starts it
        # and separate from everything the status pill says. It is dim in
        # every state it has, including failure: this line is never allowed to
        # look like a verdict on the address in the box.
        self._scan_label = QLabel("", self)
        self._scan_label.setObjectName("scanHint")
        self._scan_label.setWordWrap(True)
        self._scan_label.setVisible(False)
        self._scan_label.setAccessibleName("Network search")
        column.addWidget(self._scan_label)

        # The two calls that put a packet on the wire, injected rather than
        # imported, so the search can be driven end to end by a test without
        # anything leaving this machine.
        self.scan_connect = None
        self.scan_fetch = None
        self._scan_task = None
        self._scan_networks = ""

        self._connection.changed.connect(self._refresh)
        self._theme.changed.connect(self._refresh)
        self._refresh()

    # -- address field
    def _address_typed(self, text):
        # set_host resets the verdict to unknown, which is right: the old
        # answer described the old address.
        self._connection.set_host(text)

    def check(self):
        """Verify reachability, off the GUI thread."""
        host = self._address.text().strip()
        if not host:
            self._connection.set_connection(
                "unknown",
                "Type the console's IP address. It is on the PS3 under "
                "Settings, Network Settings, Settings and Connection Status "
                "List.")
            return None
        self._connection.set_host(host)
        crashreport.note("pressed Check")
        self._connection.set_connection("checking", f"Asking {host}.")

        def work(control):
            probe = HttpProbe(host, timeout=CHECK_TIMEOUT)
            response = probe.get("/")
            if control.cancelled:
                return None
            return {"host": host, "ok": response.ok, "body": response.body}

        task = self.services.submit(work)
        task.finished.connect(self._checked)
        task.failed.connect(self._check_failed)
        return task

    def _checked(self, result):
        if not result:
            return
        if result["host"] != self._connection.host:
            # The address changed while we were waiting. The answer is about a
            # console nobody is asking about any more.
            return
        host = result["host"]
        crashreport.note(f"check finished, reachable={bool(result['ok'])}")
        if result["ok"] and looks_like_webman(result["body"]):
            self._connection.set_connection(
                "connected", f"webMAN answered at {host}.")
        elif result["ok"]:
            self._connection.set_connection(
                "unreachable", NOT_WEBMAN.format(host=host))
        else:
            self._connection.set_connection(
                "unreachable", CANNOT_REACH.format(host=host))

    def _check_failed(self, message):
        # HttpProbe turns network trouble into a Response, so reaching here is
        # a fault in this program rather than in anybody's network.
        crashreport.note("check failed")
        self._connection.set_connection(
            "unreachable",
            f"The check could not be completed: {message}")

    # -- the subnet search, which is a different question
    def _scan_targets(self):
        """(this PC's addresses, the hosts to try). Reads the interface table
        and the routing table; sends nothing. Overridden in tests.

        A list rather than one address, because a PC with no default route to
        be found -- or with several -- is better served by searching every
        plausible subnet than by picking one and reporting failure.
        """
        interfaces = discovery.local_interfaces()
        addresses = discovery.choose_subnets(interfaces)
        hosts = discovery.scan_hosts(
            addresses, exclude=[item.address for item in interfaces])
        return addresses, hosts

    def find(self):
        """Search the subnets this PC is attached to for something that
        answers like webMAN."""
        if self._connection.scan == "scanning":
            return None
        crashreport.note("pressed Find")
        self._connection.set_scan("scanning", "Looking for a PS3.", [])
        settings = getattr(self.services, "settings", None)
        connect_timeout = _timeout(settings, SCAN_CONNECT_TIMEOUT_KEY,
                                   SCAN_CONNECT_TIMEOUT)
        fetch_timeout = _timeout(settings, SCAN_FETCH_TIMEOUT_KEY,
                                 SCAN_FETCH_TIMEOUT)

        def work(control):
            addresses, hosts = self._scan_targets()
            # Older overrides, and the tests that drive one subnet by hand,
            # hand back a single address rather than a list of them.
            if isinstance(addresses, str):
                addresses = [addresses] if addresses else []
            addresses = list(addresses)
            # Read by the progress line, which runs on the GUI thread once the
            # worker has worked out where it is searching.
            self._scan_networks = _networks_label(addresses)
            if not hosts:
                return {"problem": (
                    "This PC's own network address could not be read, so "
                    "there is nowhere to search. Type the console's address "
                    "in instead.")}

            # What the search actually did, as opposed to what it concluded.
            # Nothing else can tell "searched the wrong network", "the console
            # never answered in time" and "something answered but was a
            # router" apart, and all three come back as no PS3 found.
            tally = {"answered": 0, "fetched": 0, "best": 0, "best_host": ""}
            lock = threading.Lock()

            def fetch(host):
                response = HttpProbe(host, timeout=fetch_timeout).get("/")
                return response.body if response.ok else None

            real_connect = self.scan_connect or (
                lambda host: tcp_open(host, 80, connect_timeout))
            real_fetch = self.scan_fetch or fetch

            def counted_connect(host):
                opened = bool(real_connect(host))
                if opened:
                    with lock:
                        tally["answered"] += 1
                return opened

            def counted_fetch(host):
                body = real_fetch(host)
                if not body:
                    return body
                score = webman_score(body)[0]
                with lock:
                    tally["fetched"] += 1
                    if score > tally["best"]:
                        tally["best"], tally["best_host"] = score, host
                return body

            candidates = discovery.scan(
                hosts, counted_fetch,
                connect=counted_connect,
                connect_timeout=connect_timeout,
                on_progress=lambda done, total, found: control.progress(
                    (done, total, tally["answered"])),
                should_stop=lambda: control.cancelled)
            return {"address": addresses[0] if addresses else "",
                    "addresses": addresses, "candidates": candidates,
                    "total": len(hosts), "tally": dict(tally),
                    "connect_timeout": connect_timeout}

        task = self.services.submit(work)
        task.progress.connect(self._scan_progress)
        task.finished.connect(self._scanned)
        task.failed.connect(self._scan_failed)
        self._scan_task = task
        return task

    def cancel_scan(self):
        """Stop the search at its next address. The worker checks between
        hosts, so a quarter of a /24 still in flight does not hold the window
        open while the user is trying to shut it."""
        task, self._scan_task = self._scan_task, None
        if task is not None:
            task.cancel()
        return task

    def _scan_progress(self, value):
        # The worker sends (done, total, answered); older callers, and the
        # test that drives this by hand, send the pair without the count.
        done, total = value[0], value[1]
        answered = value[2] if len(value) > 2 else 0
        networks = getattr(self, "_scan_networks", "")
        where = f" on {networks}" if networks else ""
        line = f"Looking for a PS3{where}: {done} of {total} addresses tried"
        line += (f", {answered} answered." if answered
                 else ", none has answered yet.")
        self._connection.set_scan("scanning", line)

    def _scanned(self, result):
        if not result:
            return
        if result.get("problem"):
            self._connection.set_scan("failed", result["problem"], [])
            return
        candidates = result["candidates"]
        crashreport.note(f"search finished, {len(candidates)} found")
        kind, message = discovery.describe_outcome(candidates)
        if kind == "none":
            message = f"{message} {self._scan_summary(result)}".strip()
        self._connection.set_scan(
            "none" if kind == "none" else "found", message, candidates)
        if kind == "one":
            # The address is filled in and nothing else. The user asked where
            # the console is, not for anything to be done to it.
            self.set_address(candidates[0].address)
        elif kind == "several":
            self._choose(candidates)

    @staticmethod
    def _scan_summary(result):
        """What the search did, for a search that found nothing.

        Written to be read back over the phone: which networks were
        searched, how many addresses were tried, how many answered, and whether
        any of them nearly looked like webMAN. Without this, a search of a
        Docker bridge and a search of the right network with the console
        asleep are the same sentence.
        """
        tally = result.get("tally") or {}
        addresses = result.get("addresses")
        if addresses is None:
            addresses = [result["address"]] if result.get("address") else []
        parts = []
        if len(addresses) == 1:
            parts.append(f"Searched {result.get('total', 0)} addresses on "
                         f"{_subnet_label(addresses[0])}, this PC being "
                         f"{addresses[0]}.")
        elif addresses:
            # Every subnet is named. "Searched the wrong network" is only
            # diagnosable if the line says which networks were searched, and
            # a five-adapter PC searches more than one of them.
            where = _listed(f"{_subnet_label(item)} (this PC {item})"
                            for item in addresses)
            parts.append(f"Searched {result.get('total', 0)} addresses on "
                         f"{where}.")
        answered = tally.get("answered", 0)
        if not answered:
            # "on it" would be wrong the moment more than one subnet is
            # searched, and the number searched varies by machine.
            parts.append(
                "Nothing answered on port 80 within "
                + _seconds(result.get("connect_timeout",
                                      SCAN_CONNECT_TIMEOUT))
                + ". If the console is on a different network from this PC, "
                  "or was too slow to answer, it would not be found.")
        else:
            fetched = tally.get("fetched", 0)
            parts.append(f"{answered} answered on port 80 and {fetched} sent "
                         f"back a page.")
            best_host = tally.get("best_host")
            if best_host:
                parts.append(
                    f"The closest was {best_host}, which scored "
                    f"{tally.get('best', 0)} out of the "
                    f"{SIGNATURE_THRESHOLD} a webMAN page needs.")
        return " ".join(parts)

    def _scan_failed(self, message):
        crashreport.note("search failed")
        self._connection.set_scan(
            "failed", f"The search could not be completed: {message}", [])

    def _choose(self, candidates):
        dialog = CandidateDialog(candidates, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.chosen():
            self.set_address(dialog.chosen())

    def _refresh(self):
        state = self._connection.connection
        word, token = CONNECTION_WORDS.get(state, ("Not checked", "text_dim"))
        colour = self._theme.colour(token)
        self._dot.set_colour(colour)
        self._state_label.setText(word)
        self._state_label.setStyleSheet(f"color: {colour};")
        detail = self._connection.connection_detail
        self._detail_label.setText(detail)
        self._detail_label.setVisible(bool(detail))
        self._check_button.setEnabled(state != "checking")

        # Never coloured, never merged into the pill above.
        scan_detail = self._connection.scan_detail
        self._scan_label.setText(scan_detail)
        self._scan_label.setVisible(bool(scan_detail))
        self._scan_label.setStyleSheet(
            f"color: {self._theme.colour('text_dim')};")
        self._find_button.setEnabled(self._connection.scan != "scanning")

        if self._address.text().strip() != self._connection.host:
            blocked = self._address.blockSignals(True)
            self._address.setText(self._connection.host)
            self._address.blockSignals(blocked)

    # -- for tests and for the shell
    def state_text(self):
        return self._state_label.text()

    def detail_text(self):
        return self._detail_label.text()

    def visible_text(self):
        """Everything the bar is saying about the connection, for anyone
        checking what it does not say. The search's own line is not part of
        it, which is the whole point."""
        return " ".join(part for part in
                        (self._state_label.text(), self._detail_label.text())
                        if part)

    def scan_text(self):
        return self._scan_label.text()

    def set_address(self, host):
        self._address.setText(host)


# --- navigation ------------------------------------------------------------

class PageHost(QWidget):
    """Holds one page at a time and animates the change.

    Pages are positioned by hand rather than by a layout because two of them
    are on screen at once for the length of a transition.
    """

    transition_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pageHost")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.animations_enabled = True
        self._current = None
        self._outgoing = None
        self._group = None

    @property
    def current(self):
        return self._current

    @property
    def animating(self):
        return self._group is not None

    def set_page(self, widget, forward=True):
        """Show widget. Returns False if a transition is already running.

        Refusing rather than queueing is deliberate: a second navigation
        half way through the first leaves two pages animating over each other
        and the wrong one on top when they stop.
        """
        if self._group is not None:
            return False
        if widget is self._current:
            return True

        previous = self._current
        self._current = widget
        widget.setParent(self)
        widget.setGeometry(self.rect())
        widget.show()
        widget.raise_()

        if (previous is None or not self.animations_enabled
                or not self.isVisible()):
            if previous is not None:
                previous.hide()
            return True

        self._outgoing = previous
        distance = self.width() or widget.width()
        offset = distance if forward else -distance

        fade = QGraphicsOpacityEffect(widget)
        fade.setOpacity(0.0)
        widget.setGraphicsEffect(fade)
        widget.move(offset, 0)

        group = QParallelAnimationGroup(self)
        for animation in (
                self._slide(widget, QPoint(offset, 0), QPoint(0, 0)),
                self._slide(previous, QPoint(0, 0), QPoint(-offset, 0)),
                self._fade(fade, 0.0, 1.0)):
            group.addAnimation(animation)
        group.finished.connect(self._settle)
        self._group = group
        group.start()
        return True

    def _slide(self, widget, start, end):
        animation = QPropertyAnimation(widget, b"pos", self)
        animation.setDuration(TRANSITION_MS)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        return animation

    def _fade(self, effect, start, end):
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(TRANSITION_MS)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        return animation

    def _settle(self):
        self._group = None
        if self._outgoing is not None:
            self._outgoing.hide()
            self._outgoing.move(0, 0)
            self._outgoing = None
        if self._current is not None:
            # The effect is removed rather than left at opacity 1: it disables
            # the widget's own double buffering for as long as it is attached.
            self._current.setGraphicsEffect(None)
            self._current.setGeometry(self.rect())
        self.transition_finished.emit()

    def finish_now(self):
        """Jump to the end of a running transition. Shutdown and tests."""
        if self._group is not None:
            self._group.stop()
            self._settle()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._group is None and self._current is not None:
            self._current.setGeometry(self.rect())


# --- the window ------------------------------------------------------------

class MainWindow(QMainWindow):
    """One window. A launcher, the screens, and the state they share."""

    NOTICE_MS = 7000

    def __init__(self, services, problems=(), parent=None):
        super().__init__(parent)
        self.services = services
        self.settings = services.settings
        self.theme = services.theme
        self.connection = services.connection

        # Resolved once, here, and drawn from every time after. The registry
        # is a mutable global, and a window that re-read it on every
        # navigation would render whatever happened to be in it rather than
        # the set of tools it started with.
        self._screen_classes = list(registry.screens())

        self._screens = {}
        self._current_key = HOME_KEY
        self._busy = False
        self._status = ""
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._restore_status)

        self.setWindowTitle(FULL_NAME)
        # The old default clipped and overlapped the busier screens. No
        # maximum: a user with the room to spare is welcome to it.
        self.setMinimumSize(1024, 700)
        self.resize(self._default_size())

        central = QWidget(self)
        self.setCentralWidget(central)
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        column.addWidget(self._build_chrome())
        self.connection_bar = ConnectionBar(services, self)
        column.addWidget(self.connection_bar)

        # Above the pages and below the bar, hidden until it has news, and
        # never started from here: constructing a window must not put a
        # request on the wire, least of all in a test.
        self.update_banner = UpdateBanner(services, self)
        column.addWidget(self.update_banner)

        self.pages = PageHost(self)
        column.addWidget(self.pages, 1)

        column.addWidget(self._build_status_bar())

        self.launcher = Launcher(self.theme, self)
        self.launcher.open_screen.connect(self.open_screen)
        self.launcher.rebuild(self._screen_classes)
        self.pages.set_page(self.launcher)

        self.theme.changed.connect(self._apply_theme)
        self._apply_theme()
        self._restore_geometry()
        self._update_chrome()

        if problems:
            self.notice(" ".join(problems), "error")

    def _default_size(self):
        """Roomy, but never larger than the desktop it opens on: a window with
        its buttons off the bottom of a small laptop screen is worse than a
        cramped one."""
        width, height = 1280, 900
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            room = screen.availableGeometry()
            width = min(width, max(self.minimumWidth(), room.width() - 80))
            height = min(height, max(self.minimumHeight(), room.height() - 80))
        return QSize(width, height)

    # -- chrome
    def _build_chrome(self):
        bar = QWidget(self)
        bar.setObjectName("chrome")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 10, 20, 10)
        row.setSpacing(14)

        # Three groups, left to right: who made it, where you are, what you can
        # change. Each is spaced as one thing, with a rule between, so the bar
        # stops reading as four unrelated fragments.
        row.addWidget(self._build_brand(bar))

        self._chrome_rule = self._build_rule(bar)
        row.addWidget(self._chrome_rule)

        self.back_button = QPushButton("Back to tools", bar)
        self.back_button.setObjectName("backButton")
        self.back_button.setShortcut(Qt.Key.Key_Escape)
        self.back_button.setToolTip("Return to the list of tools. Escape.")
        self.back_button.setIconSize(QSize(14, 14))
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(self.go_home)
        row.addWidget(self.back_button)

        self.screen_title = QLabel("", bar)
        self.screen_title.setObjectName("screenTitle")
        row.addWidget(self.screen_title)

        row.addStretch(1)

        self.theme_button = QPushButton("Theme", bar)
        self.theme_button.setProperty("flat", True)
        self.theme_button.setToolTip("Light, dark, or whichever the desktop "
                                     "is using.")
        self.theme_menu = self._build_theme_menu(bar)
        self.theme_button.setMenu(self.theme_menu)
        row.addWidget(self.theme_button)
        return bar

    def _build_brand(self, parent):
        brand = QWidget(parent)
        row = QHBoxLayout(brand)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        title = QLabel(APP_NAME, brand)
        title.setObjectName("appTitle")
        row.addWidget(title)
        byline = QLabel("by", brand)
        byline.setObjectName("dim")
        row.addWidget(byline)
        self.vendor_link = LinkLabel(VENDOR, PROJECT_URL, brand)
        row.addWidget(self.vendor_link)
        return brand

    def _build_rule(self, parent):
        rule = QFrame(parent)
        rule.setObjectName("chromeRule")
        rule.setFrameShape(QFrame.Shape.NoFrame)
        rule.setFixedWidth(1)
        rule.setVisible(False)
        return rule

    def _build_theme_menu(self, parent):
        menu = QMenu(parent)
        group = QActionGroup(menu)
        group.setExclusive(True)
        self._theme_actions = {}
        for mode, label in (("system", "Follow the desktop"),
                            ("light", "Light"),
                            ("dark", "Dark")):
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(self.theme.mode == mode)
            action.triggered.connect(
                lambda _checked=False, chosen=mode: self._choose_theme(chosen))
            group.addAction(action)
            menu.addAction(action)
            self._theme_actions[mode] = action
        return menu

    def _choose_theme(self, mode):
        crashreport.note(f"chose the {mode} theme")
        self.theme.set_mode(mode)
        self.settings["theme_mode"] = mode
        for key, action in self._theme_actions.items():
            action.setChecked(key == mode)
        # set_mode is silent when the resolved light or dark did not change,
        # so the stored choice still has to be written through.
        self._apply_theme()

    def _build_status_bar(self):
        holder = QWidget(self)
        holder.setObjectName("statusBar")
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self.busy_bar = QProgressBar(holder)
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setVisible(False)
        column.addWidget(self.busy_bar)

        row = QWidget(holder)
        line = QHBoxLayout(row)
        line.setContentsMargins(24, 8, 24, 8)
        line.setSpacing(12)
        self.status_label = QLabel("", row)
        self.status_label.setObjectName("statusHint")
        line.addWidget(self.status_label, 1)
        version = QLabel(f"Version {VERSION}", row)
        version.setObjectName("dim")
        line.addWidget(version)
        column.addWidget(row)
        return holder

    # -- theme
    def _apply_theme(self):
        application = QGuiApplication.instance()
        if application is not None and hasattr(application, "setStyleSheet"):
            application.setPalette(qt_palette(self.theme))
            application.setStyleSheet(stylesheet(self.theme))
        self._style_menu()
        self.back_button.setIcon(back_arrow_icon(
            self.theme.colour("text"), self.theme.colour("text_dim")))
        self._paint_status()
        self.update()

    def _style_menu(self):
        """A popup is a top level window of its own, and neither the
        application stylesheet nor the application palette reliably reaches
        one. Left to itself the theme menu opened in the desktop's colours,
        which is how a light window came to show a dark popup."""
        menu = getattr(self, "theme_menu", None)
        if menu is None:
            return
        menu.setPalette(menu_palette(self.theme))
        menu.setStyleSheet(stylesheet(self.theme))

    # -- navigation
    def screen_for(self, key):
        """The live instance of a screen, built on first use."""
        if key in self._screens:
            return self._screens[key]
        screen_class = next(
            (item for item in self._screen_classes if item.key == key), None)
        if screen_class is None:
            screen_class = registry.screen_for(key)
        if screen_class is None:
            return None
        screen = screen_class(self.services, self)
        screen.busy_changed.connect(self._on_busy)
        screen.status_message.connect(self.set_status)
        screen.request_home.connect(self.go_home)
        screen.hide()
        self._screens[key] = screen
        return screen

    @property
    def screen_classes(self):
        """The tools this window resolved at start up."""
        return list(self._screen_classes)

    @property
    def current_key(self):
        return self._current_key

    @property
    def current_screen(self):
        return self._screens.get(self._current_key)

    def open_screen(self, key):
        if key == self._current_key:
            return True
        screen = self.screen_for(key)
        if screen is None:
            self.notice(f"There is no tool called {key}.", "error")
            return False
        if not self._leave_current():
            return False
        if not self.pages.set_page(screen, forward=True):
            return False
        self._current_key = key
        crashreport.note(f"opened {key}")
        self.set_status("")
        self._update_chrome()
        screen.on_enter()
        return True

    def go_home(self):
        if self._current_key == HOME_KEY:
            return True
        if not self._leave_current():
            return False
        if not self.pages.set_page(self.launcher, forward=False):
            return False
        self._current_key = HOME_KEY
        crashreport.note("went back to the launcher")
        self.set_status("")
        self._update_chrome()
        # Cheap, and a no-op while the set of tools is the same one it drew
        # last time, which it always is. It stays because a window that only
        # ever built its cards once would quietly stop working the day a
        # screen is registered after start up.
        self.launcher.rebuild(self._screen_classes)
        return True

    def _leave_current(self):
        """Whether the current screen will let go, and on_leave if it does."""
        screen = self.current_screen
        if screen is None:
            return True
        if self._busy:
            self.notice(
                f"{screen.title} is still working. Wait for it to finish, or "
                f"stop it, before going anywhere else.", "warn")
            return False
        if not screen.can_leave():
            # The screen knows what it is doing and the shell does not, so it
            # gets to word the refusal if it has bothered to.
            self.notice(f"{screen.title}: {_leave_reason(screen)}", "warn")
            return False
        screen.on_leave()
        return True

    def _update_chrome(self):
        home = self._current_key == HOME_KEY
        self.back_button.setEnabled(not home and not self._busy)
        self.back_button.setVisible(not home)
        self._chrome_rule.setVisible(not home)
        screen = self.current_screen
        self.screen_title.setText("" if screen is None else screen.title)
        self.setWindowTitle(FULL_NAME if screen is None
                            else f"{screen.title} - {FULL_NAME}")

    # -- status area
    def _on_busy(self, busy):
        sender = self.sender()
        if sender is not None and sender is not self.current_screen:
            # A screen left running in the background is not this window's
            # business until it is on screen again.
            return
        self._busy = bool(busy)
        self.busy_bar.setVisible(self._busy)
        self._update_chrome()

    def set_status(self, message):
        self._status = message or ""
        self._notice_timer.stop()
        self._paint_status()

    def notice(self, message, token="warn"):
        """A short-lived line in the status area, coloured. Used for refusals,
        which are the one thing a user must not miss."""
        self._notice_timer.stop()
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {self.theme.colour(token)};")
        self._notice_timer.start(self.NOTICE_MS)

    def status_text(self):
        return self.status_label.text()

    def _restore_status(self):
        self._paint_status()

    def _paint_status(self):
        self.status_label.setText(self._status)
        self.status_label.setStyleSheet(
            f"color: {self.theme.colour('text_dim')};")

    # -- settings and lifecycle
    def _restore_geometry(self):
        stored = self.settings.get("geometry")
        if not stored:
            return
        try:
            data = QByteArray(base64.b64decode(stored))
        except (ValueError, TypeError):
            return
        self.restoreGeometry(data)

    def store_settings(self):
        self.settings["host"] = self.connection.host
        self.settings["theme_mode"] = self.theme.mode
        self.settings["geometry"] = base64.b64encode(
            bytes(self.saveGeometry())).decode("ascii")
        return save_settings(self.settings)

    def closeEvent(self, event):
        screen = self.current_screen
        if screen is not None and not screen.can_leave():
            self.notice(f"{screen.title}: {_leave_reason(screen)} The window "
                        f"will close when it is safe.", "warn")
            event.ignore()
            return
        crashreport.note("closed the window")
        self.pages.finish_now()
        self.connection_bar.cancel_scan()
        if screen is not None:
            screen.on_leave()
        self.store_settings()
        super().closeEvent(event)


# --- entry point -----------------------------------------------------------

def _leave_reason(screen):
    """A screen's own wording where it has one, the interface default where
    it has not. Never allowed to fail: this runs while refusing to close."""
    try:
        return str(screen.leave_blocked_reason())
    except Exception:
        return ("It is part way through something that must not be "
                "interrupted.")


def _icon():
    for extension in ("ico", "png"):
        for base in (config.bundle_dir(), config.app_dir()):
            candidate = os.path.join(base, f"icon.{extension}")
            if os.path.isfile(candidate):
                return QIcon(candidate)
    return QIcon()


def _windows_taskbar_identity():
    """Windows groups taskbar buttons by application id; without one of its own
    the program is grouped under python.exe and shows its icon there."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"{VENDOR}.{APP_NAME}")
    except Exception:
        pass


def build(application, settings=None):
    """Everything between a QApplication and a window worth showing."""
    settings = load_settings() if settings is None else settings
    # install() ran before there were any settings to read, so until this is
    # called a crash log lands on the Desktop rather than the chosen folder.
    crashreport.configure(settings)
    theme = AppTheme(settings.get("theme_mode", "system"))
    connection = ConnectionState(settings.get("host", ""))
    services = Services(connection, theme, settings)

    hints = QGuiApplication.styleHints()
    changed = getattr(hints, "colorSchemeChanged", None)
    if changed is not None:
        changed.connect(lambda _scheme: theme.refresh_from_system())

    problems = load_screen_modules()
    window = MainWindow(services, problems)
    application.setPalette(qt_palette(theme))
    application.setStyleSheet(stylesheet(theme))
    return window


def main(argv=None):
    from PySide6.QtWidgets import QApplication

    _windows_taskbar_identity()
    application = QApplication(list(argv if argv is not None else sys.argv))
    application.setApplicationName(APP_NAME)
    application.setApplicationDisplayName(FULL_NAME)
    application.setOrganizationName(VENDOR)
    application.setApplicationVersion(VERSION)
    application.setWindowIcon(_icon())

    window = build(application)
    window.show()
    # After the window is up, and only from the real entry point. build() is
    # left free of it so that anything constructing a window for another
    # reason -- a test, a screenshot -- asks nobody anything.
    QTimer.singleShot(0, window.update_banner.start_check)
    return application.exec()
