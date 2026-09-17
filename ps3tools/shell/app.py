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
import time

from PySide6.QtCore import (QByteArray, QEasingCurve, QParallelAnimationGroup,
                            QPoint, QPointF, QPropertyAnimation, QRectF, QSize,
                            QVariantAnimation, Property, Qt, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices,
                           QFont, QGuiApplication, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QInputDialog,
                               QMessageBox,
                               QGraphicsBlurEffect, QGraphicsOpacityEffect,
                               QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ps3diag import config, discovery
from .. import profiles
from ps3diag.parsers import SIGNATURE_THRESHOLD, looks_like_webman, \
    webman_score
from ps3diag.transport import HttpProbe, tcp_open

from .. import APP_NAME, FULL_NAME, PROJECT_URL, VENDOR, VERSION, crashreport
from . import consolestats, icons, registry, widgets
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
CHECK_LABEL = "Check IP"
CHECK_HINT = ("Ask the console at this address whether it is there and "
              "running webMAN.")
DISCONNECT_LABEL = "Disconnect"
DISCONNECT_HINT = ("Stop using this console, so a different one can be found. "
                   "Nothing is sent to the console.")

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
    """Where the shell's settings may be. config decides, not this module.

    It used to look beside the exe first. See ps3diag.config: that cost people
    their saved address whenever the exe moved or sat somewhere unwritable.
    """
    return config.candidate_paths(SETTINGS_FILE)


def load_settings():
    # Moves a file left beside the exe by an older version, so an upgrade
    # keeps the saved address and the remembered game lists.
    config.migrate(SETTINGS_FILE)
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
KNOWN_SCREEN_MODULES = ("about", "diagnostics", "gameupdates",
                        "installpkg", "patcher", "saves",
                        "transfer")


def _bundled_screens():
    """Never called. It exists to be read by PyInstaller, which follows import
    statements and not strings; without it the screen modules are absent from
    the exe rather than merely undiscovered in it."""
    from ..screens import (about, diagnostics, gameupdates,  # noqa: F401
                           installpkg, patcher, saves, transfer)
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


#: The three flat colours in logo.png. The blue is the brand and is left
#: alone; the two neutrals are drawn for a dark background and have to be
#: turned round for a light one, or "PS3" is near enough invisible on white.
LOGO_BLUE_EXCESS = 60


#: How tall the wordmark is drawn. The chrome bar is 59 pixels tall, so this
#: fills it with a little air above and below. Scaling the whole 390 pixel
#: file down to the height of one line of text was what made it illegible:
#: most of that height is space above and below the drawing.
#: How long shutdown waits for workers to notice they were cancelled. Long
#: enough for a poll or a block read to come round, short enough that nobody
#: watches a window refuse to close.
SHUTDOWN_WAIT_MS = 4000

BRAND_LOGO_HEIGHT = 44

#: The three bands of logo.png, in its own pixels, measured off the file.
#: Cropping to them is what stops the empty margin eating the height.
LOGO_CHEVRONS = (40, 64, 260, 260)          # left, top, right, bottom
LOGO_WORDMARK = (340, 74, 949, 176)         # "PS3 Tools"
LOGO_BYLINE = (340, 217, 586, 269)          # "by setsid"

#: The space between the chevrons and the wordmark, as drawn.
LOGO_GAP = 81

#: How tall "by setsid" has to come out before it is worth drawing. Below
#: this it is a grey smudge, so the mark is built without it instead.
LOGO_BYLINE_MIN = 13

LOGO_FILE = "logo.png"


def brand_logo_path():
    """Where the wordmark lives, bundled or in a checkout. "" if absent."""
    for base in (config.bundle_dir(), config.app_dir()):
        candidate = os.path.join(base, LOGO_FILE)
        if os.path.isfile(candidate):
            return candidate
    return ""


class BrandLogo(QLabel):
    """The wordmark in the top left, as a link to the project.

    The file is one flat-coloured PNG. Rather than ship a second copy for the
    light palette, the neutral pixels have their lightness turned round and the
    blue is left as it is: the light grey of "PS3" becomes a dark grey, "by
    setsid" stays the quieter of the two, and the blue stays the blue. Anti-
    aliased edges come out right because the rule is applied to every pixel
    rather than to a list of exact colours.

    Recolouring happens after scaling, on a few thousand pixels rather than on
    the half million in the source, so a theme change is not something anybody
    waits for.
    """

    def __init__(self, path, url, height, parent=None):
        super().__init__(parent)
        self.url = url
        self._source = QImage(path)
        self._height = height
        self._cache = {}
        self.setObjectName("brandLogo")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Open {url} in your browser.")
        self.setAccessibleName(f"{FULL_NAME}, opens {url}")

    @property
    def usable(self):
        return not self._source.isNull()

    def apply_theme(self, dark):
        if not self.usable:
            return
        pixmap = self._cache.get(dark)
        if pixmap is None:
            pixmap = QPixmap.fromImage(self._render(dark))
            self._cache[dark] = pixmap
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())

    def _render(self, dark):
        ratio = self.devicePixelRatioF() if hasattr(
            self, "devicePixelRatioF") else 1.0
        tall = max(1, int(self._height * ratio))
        source = self._laid_out(tall)
        image = source.scaledToHeight(
            tall, Qt.TransformationMode.SmoothTransformation)
        image = image.convertToFormat(QImage.Format.Format_ARGB32)
        if not dark:
            _invert_neutrals(image)
        image.setDevicePixelRatio(ratio)
        return image

    def _laid_out(self, tall):
        """The drawing to scale, with or without the byline.

        The file has the byline under the wordmark and the chevrons spanning
        both, so the byline cannot simply be cropped off the bottom without
        taking half the chevrons with it. The two parts are laid out again
        instead, which also lets the gap between them stay as drawn.
        """
        full = self._cropped(_union(LOGO_CHEVRONS, LOGO_WORDMARK,
                                    LOGO_BYLINE))
        byline_height = (LOGO_BYLINE[3] - LOGO_BYLINE[1])
        if byline_height * tall / max(1, full.height()) >= LOGO_BYLINE_MIN:
            return full
        chevrons = self._cropped(LOGO_CHEVRONS)
        wordmark = self._cropped(LOGO_WORDMARK)
        width = chevrons.width() + LOGO_GAP + wordmark.width()
        height = max(chevrons.height(), wordmark.height())
        composed = QImage(width, height, QImage.Format.Format_ARGB32)
        composed.fill(Qt.GlobalColor.transparent)
        painter = QPainter(composed)
        painter.drawImage(0, (height - chevrons.height()) // 2, chevrons)
        painter.drawImage(chevrons.width() + LOGO_GAP,
                          (height - wordmark.height()) // 2, wordmark)
        painter.end()
        return composed

    def _cropped(self, box):
        left, top, right, bottom = box
        return self._source.copy(left, top, right - left, bottom - top)

    def open(self):
        return QDesktopServices.openUrl(QUrl(self.url))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.open()
        super().mouseReleaseEvent(event)


def _union(*boxes):
    """The smallest box holding all of them."""
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _invert_neutrals(image):
    """Turn the grey pixels round in place. The blue ones are left alone.

    "Grey" is decided by how much more blue a pixel has than its red and green,
    which separates the brand blue from both neutrals cleanly and keeps working
    through the anti-aliased edges where a colour match would not.

    Lightness is what is turned round, not each channel on its own: the light
    grey is very slightly warm, and inverting its channels gives a brown rather
    than the dark grey it is supposed to become. The two neutrals keep their
    order either way -- "PS3" stays the stronger of the two and "by setsid" the
    quieter -- which is the whole point of not simply painting both one colour.
    """
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            alpha = pixel.alpha()
            if not alpha:
                continue
            red, green, blue = pixel.red(), pixel.green(), pixel.blue()
            if blue - (red + green) / 2 > LOGO_BLUE_EXCESS:
                continue
            level = 255 - (red + green + blue) // 3
            image.setPixelColor(x, y, QColor(level, level, level, alpha))


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


class AnimatedTick(QWidget):
    """A tick that draws itself on. Shown for a moment when a console answers.

    Painted rather than an icon swap, because the point of it is the drawing:
    something happened, it worked, and the window is about to get out of the
    way. A static tick appearing and vanishing reads as a flicker.
    """

    finished = Signal()

    def __init__(self, colour, size=72, parent=None):
        super().__init__(parent)
        self._colour = colour
        self._progress = 0.0
        self.setFixedSize(size, size)
        self._animation = QPropertyAnimation(self, b"progress", self)
        self._animation.setDuration(420)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self.finished)

    def start(self):
        self._animation.stop()
        self._progress = 0.0
        self._animation.start()

    def get_progress(self):
        return self._progress

    def set_progress(self, value):
        self._progress = float(value)
        self.update()

    progress = Property(float, get_progress, set_progress)

    def paintEvent(self, _event):
        side = min(self.width(), self.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(self._colour))
        pen.setWidthF(max(2.0, side * 0.09))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        # The ring first, then the tick inside it, so the two halves of the
        # animation read as one movement.
        inset = pen.widthF()
        box = QRectF(inset, inset, side - inset * 2, side - inset * 2)
        ring = min(1.0, self._progress / 0.6)
        if ring > 0:
            painter.drawArc(box, 90 * 16, -int(360 * 16 * ring))
        mark = max(0.0, (self._progress - 0.45) / 0.55)
        if mark > 0:
            points = [(0.30, 0.52), (0.44, 0.66), (0.71, 0.38)]
            path = QPainterPath()
            path.moveTo(side * points[0][0], side * points[0][1])
            first = QPointF(side * points[1][0], side * points[1][1])
            second = QPointF(side * points[2][0], side * points[2][1])
            if mark <= 0.5:
                where = mark / 0.5
                path.lineTo(
                    side * points[0][0] + (first.x() - side * points[0][0])
                    * where,
                    side * points[0][1] + (first.y() - side * points[0][1])
                    * where)
            else:
                where = (mark - 0.5) / 0.5
                path.lineTo(first)
                path.lineTo(first.x() + (second.x() - first.x()) * where,
                            first.y() + (second.y() - first.y()) * where)
            painter.drawPath(path)


class Spinner(QWidget):
    """A turning arc, for the moments there is nothing to measure.

    The search has no total worth showing -- it is a sweep of a subnet whose
    size the user does not care about -- so this says "still going" and the
    label beside it says what it is doing. It only runs while it is visible:
    an animation ticking away behind a closed dialog is wasted work.
    """

    def __init__(self, colour, size=18, parent=None):
        super().__init__(parent)
        self._colour = colour
        self._angle = 0
        self.setFixedSize(size, size)
        self._animation = QPropertyAnimation(self, b"angle", self)
        self._animation.setDuration(1100)
        self._animation.setStartValue(0)
        self._animation.setEndValue(360)
        self._animation.setLoopCount(-1)

    def set_colour(self, colour):
        self._colour = colour
        self.update()

    def get_angle(self):
        return self._angle

    def set_angle(self, value):
        self._angle = int(value)
        self.update()

    angle = Property(int, get_angle, set_angle)

    def showEvent(self, event):
        super().showEvent(event)
        self._animation.start()

    def hideEvent(self, event):
        self._animation.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        # A looping animation still running while its widget is destroyed is
        # a way to take the process with it.
        self._animation.stop()
        super().closeEvent(event)

    def paintEvent(self, _event):
        side = min(self.width(), self.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(self._colour))
        pen.setWidthF(max(1.6, side * 0.13))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        inset = pen.widthF()
        box = QRectF(inset, inset, side - inset * 2, side - inset * 2)
        # Three quarters of a circle, turning. A full ring would not read as
        # moving at all.
        painter.drawArc(box, -self._angle * 16, 270 * 16)


class FirstRunDialog(QDialog):
    """Shown once, on a start with no console address saved.

    It drives the connection bar rather than talking to the network itself:
    one Find, one Check and one address field in this program, wherever they
    are pressed from. Dismissing it leaves the application exactly as it was
    before it existed, with no console and every screen still reachable.
    """

    #: How long the tick stays up once it has drawn itself.
    LINGER_MS = 700

    def __init__(self, bar, theme, parent=None):
        super().__init__(parent)
        self._bar = bar
        self._theme = theme
        self._closing = False
        self.setObjectName("firstRun")
        self.setWindowTitle("Find your PS3")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Without this the window still paints its own square behind the
        # stylesheet's rounded rectangle, and the corners show up as four
        # darker notches.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # The window itself is transparent and an inner frame carries the
        # rounded background. Rounding the window directly leaves it painting
        # its own square behind the corners, which showed as four darker
        # notches; making the window transparent on its own leaves the body
        # transparent too and only the buttons visible.
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self.body = QFrame(self)
        self.body.setObjectName("firstRunBody")
        self.body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shell.addWidget(self.body)

        column = QVBoxLayout(self.body)
        column.setContentsMargins(28, 18, 28, 26)
        column.setSpacing(14)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addStretch(1)
        self.dismiss_button = QPushButton("\u2715", self)
        self.dismiss_button.setObjectName("dismiss")
        self.dismiss_button.setProperty("flat", True)
        self.dismiss_button.setFixedSize(30, 30)
        self.dismiss_button.setToolTip("Carry on without a console.")
        self.dismiss_button.setAccessibleName("Close")
        self.dismiss_button.clicked.connect(self.reject)
        top.addWidget(self.dismiss_button)
        column.addLayout(top)

        heading = QLabel("Let's find your PS3", self)
        heading_font = QFont(heading.font())
        heading_font.setPointSize(heading_font.pointSize() + 7)
        heading_font.setBold(True)
        heading.setFont(heading_font)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(heading)

        blurb = QLabel(
            "Switch the console on, leave it on the main menu, and press the "
            "button. If you already know its address, type it in instead.",
            self)
        blurb.setWordWrap(True)
        blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(blurb)

        self.tick = AnimatedTick(theme.colour("ok") or theme.colour("accent"),
                                 parent=self)
        self.tick.hide()
        self.tick.finished.connect(self._after_tick)
        tick_row = QHBoxLayout()
        tick_row.addStretch(1)
        tick_row.addWidget(self.tick)
        tick_row.addStretch(1)
        column.addLayout(tick_row)

        self.find_button = QPushButton("Find my PS3", self)
        self.find_button.setObjectName("bigFind")
        self.find_button.setMinimumHeight(54)
        widgets.set_role(self.find_button, widgets.PRIMARY)
        find_font = QFont(self.find_button.font())
        find_font.setPointSize(find_font.pointSize() + 3)
        find_font.setBold(True)
        self.find_button.setFont(find_font)
        self.find_button.clicked.connect(self._on_find)
        column.addWidget(self.find_button)

        # The same two things the bar at the top shows while it searches: a
        # sign that it is still going, and what it is doing.
        busy = QHBoxLayout()
        busy.setSpacing(8)
        busy.addStretch(1)
        self.spinner = Spinner(theme.colour("accent"), parent=self)
        self.spinner.hide()
        busy.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status = QLabel("", self)
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        busy.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
        busy.addStretch(1)
        column.addLayout(busy)

        typed = QHBoxLayout()
        typed.setSpacing(8)
        self.address = QLineEdit(self)
        self.address.setPlaceholderText("or type it: 192.168.1.50")
        self.address.setMinimumWidth(210)
        self.address.setAccessibleName("PlayStation 3 address")
        self.address.returnPressed.connect(self._on_check)
        typed.addWidget(self.address, 1)
        self.check_button = QPushButton(CHECK_LABEL, self)
        self.check_button.clicked.connect(self._on_check)
        typed.addWidget(self.check_button)
        column.addLayout(typed)

        self._bar.connection.changed.connect(self._on_connection)
        self._on_connection()

    # -- driving the one connection bar there is

    def _on_find(self):
        self.status.setText("Looking for a console on this network.")
        self.spinner.show()
        self._bar.find()

    def _on_check(self):
        host = self.address.text().strip()
        if not host:
            self.status.setText("Type the console's address, or press Find "
                                "my PS3.")
            return
        self._bar.set_address(host)
        self._bar.check()

    def _on_connection(self):
        state = self._bar.connection.connection
        if state == "connected":
            self._succeed()
            return
        scanning = self._bar.connection.scan == "scanning"
        busy = state == "checking" or scanning
        self.find_button.setEnabled(not busy)
        self.check_button.setEnabled(not busy)
        self.spinner.setVisible(busy)
        # While it is working, say what it is doing and keep saying it: the
        # search reports how far through the subnet it is and that is the
        # whole of what there is to show. The bar at the top shows the same
        # detail in the same words.
        if busy:
            detail = self._bar.connection.scan_detail
            if detail:
                self.status.setText(detail)
            else:
                self.status.setText(
                    "Looking for a console on this network." if scanning
                    else "Asking that address whether it is there.")
            return
        detail = (self._bar.connection.scan_detail
                  or self._bar.connection.connection_detail)
        if state == "unreachable" and detail:
            self.status.setText(detail)
        elif self._bar.connection.scan == "none" and detail:
            # A finished search that found nothing. Its own words say what to
            # check, and they are better than anything repeated here.
            self.status.setText(detail)

    def _succeed(self):
        if self._closing:
            return
        self._closing = True
        host = self._bar.connection.host
        self.status.setText(f"Found it at {host}.")
        self.spinner.hide()
        self.find_button.hide()
        self.check_button.hide()
        self.address.hide()
        self.dismiss_button.hide()
        self.tick.show()
        self.tick.start()

    def _after_tick(self):
        # A timer owned by the dialog, not a loose singleShot: if the dialog
        # is closed or destroyed while the tick is still up, the timer goes
        # with it rather than firing into something that is no longer there.
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self.accept)
        timer.start(self.LINGER_MS)


#: Between the event and how long ago it was. The line is two facts about
#: one thing rather than a sentence, so it is punctuated as a list.
EVENT_SEPARATOR = "\u00b7"

#: How often the "4 min ago" on the bar is rewritten. Nothing is fetched on
#: this tick: it relabels one line and stops.
AGO_TICK_MS = 30000


def relative_time(seconds):
    """How long ago, in the fewest words that are still true.

    Rounded down, so "4 min ago" is never said of something that happened
    three and a half minutes ago.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return "1 day ago" if days == 1 else f"{days} days ago"


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

        # Which console this is. Only shown once there is more than one, so
        # somebody with a single PS3 never has to think about profiles at all.
        self._console_button = QPushButton(self)
        self._console_button.setObjectName("consoleButton")
        self._console_button.setProperty("flat", True)
        self._console_button.setToolTip("Switch between your consoles, or "
                                        "give this one a name.")
        self._console_menu = QMenu(self._console_button)
        self._console_button.setMenu(self._console_menu)
        self._console_menu.aboutToShow.connect(self._fill_console_menu)
        self._console_button.clicked.connect(self._console_pressed)
        # Hidden until it has something to say. Painted here rather than left
        # to the first address change, or it shows blank on the way up.
        self._console_button.hide()
        row.addWidget(self._console_button)

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

        self._check_button = QPushButton(CHECK_LABEL, self)
        self._check_button.setProperty("primary", True)
        self._check_button.setToolTip(
            "Ask the console at that address whether it is there.")
        self._check_button.clicked.connect(self._check_or_disconnect)
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

        # The right-hand side: what this console is running, how much room it
        # has left, and the last thing that finished. Each one is drawn only
        # once there is something to draw, so a console that has not answered
        # leaves this side of the bar empty rather than full of gaps.
        self._firmware_label = QLabel("", self)
        self._firmware_label.setObjectName("dim")
        self._firmware_label.setAccessibleName("Console firmware")
        self._firmware_label.setVisible(False)
        row.addWidget(self._firmware_label)

        self._free_label = QLabel("", self)
        self._free_label.setObjectName("dim")
        self._free_label.setAccessibleName("Free space on the console")
        self._free_label.setVisible(False)
        row.addWidget(self._free_label)

        self._event_label = QLabel("", self)
        self._event_label.setObjectName("dim")
        self._event_label.setAccessibleName("The last thing that happened")
        self._event_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                       | Qt.AlignmentFlag.AlignVCenter)
        self._event_label.setVisible(False)
        row.addWidget(self._event_label)

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
        #: Set by tests. None means the real HttpProbe.
        self.check_fetch = None
        self.scan_fetch = None
        self._scan_task = None
        self._scan_networks = ""

        # The right-hand side's state. Nothing is read on the way up: the
        # bar asks a console only once that console has answered a check.
        self._console_host = ""
        self._console_facts = {}
        self._event = ""
        self._event_at = 0.0
        # Owned by the bar so it dies with it. It is the only repeating timer
        # here and it puts nothing on the wire; see tick().
        self._ago_timer = QTimer(self)
        self._ago_timer.setInterval(AGO_TICK_MS)
        self._ago_timer.timeout.connect(self.tick)

        self._connection.changed.connect(self._refresh)
        self._theme.changed.connect(self._refresh)
        self._refresh()

    # -- address field
    @property
    def connection(self):
        """The shared ConnectionState. Read-only on purpose: everything that
        changes it goes through this bar's own find/check/set_address."""
        return self._connection

    def _address_typed(self, text):
        # set_host resets the verdict to unknown, which is right: the old
        # answer described the old address.
        self._connection.set_host(text)

    def _check_or_disconnect(self):
        """One button, two jobs, decided by whether a console is in use."""
        if self._connection.connection == "connected":
            return self.disconnect_console()
        return self.check()

    def disconnect_console(self):
        """Let go of the console so another one can be looked for.

        Nothing is sent: this end simply stops treating the address as live.
        The address is left in the box, because the usual reason for pressing
        this is to search for a different console and the old address is the
        thing somebody wants to see replaced, not something to hunt for again.
        """
        crashreport.note("disconnected from the console")
        self._connection.set_connection(
            "unknown", "Not connected. Press Find my PS3, or type an address "
                       "and press Check IP.")
        self._connection.set_scan("idle", "")
        # The next thing anybody does here is type an address or press the
        # button beside it, so the box is ready for them rather than waiting
        # to be clicked.
        self._address.setFocus(Qt.OtherFocusReason)
        self._address.selectAll()
        return True

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
            # Injectable for the same reason scan_fetch is: a test that drives
            # this must not put a packet on the network. There is a live
            # console on this LAN, and Find now runs a check by itself, so a
            # test feeding it invented addresses would reach for them for real.
            fetch = self.check_fetch
            if fetch is None:
                response = HttpProbe(host, timeout=CHECK_TIMEOUT).get("/")
                ok, body = response.ok, response.body
            else:
                body = fetch(host)
                ok = bool(body)
            if control.cancelled:
                return None
            return {"host": host, "ok": ok, "body": body or ""}

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
            # One of the two moments something was waited for. The window owns
            # the panel, so the bar asks it rather than reaching for it.
            window = self.window()
            if hasattr(window, "sweep_status"):
                window.sweep_status()
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

    # -- what the console said, on the right of the bar
    def note_event(self, text):
        """Record the last thing that finished, for the line on the right.

        The seam the rest of the window records events through, so that a
        screen does not have to know where the line is drawn or how the
        "4 min ago" on the end of it is worked out. An empty text clears it.
        """
        self._event = (text or "").strip()
        self._event_at = time.monotonic()
        if self._event:
            self._ago_timer.start()
        else:
            self._ago_timer.stop()
        self._paint_event()

    def tick(self):
        """Rewrite how long ago the event on the right was.

        Nothing is fetched here and no console is touched. It exists so that a
        window left open for an hour stops claiming something happened just
        now, which is the only part of that line that goes stale by itself.
        """
        self._paint_event()

    def event_text(self):
        return self._event_label.text()

    def firmware_text(self):
        return self._firmware_label.text()

    def free_text(self):
        return self._free_label.text()

    def _paint_event(self):
        if not self._event:
            self._event_label.setText("")
            self._event_label.setVisible(False)
            return
        ago = relative_time(time.monotonic() - self._event_at)
        self._event_label.setText(f"{self._event} {EVENT_SEPARATOR} {ago}")
        self._event_label.setVisible(True)

    def _sync_console(self):
        """Ask a console that has just answered what it is, off the thread.

        Asked once per console and never retried. The answer is worth two
        short labels, and a console that will not give it is somebody's games
        machine being asked the same question again for no gain. It runs after
        the connection is already made, so nothing here can delay connecting.
        """
        host = self._connection.host if self._connection.connected else ""
        if host == self._console_host:
            return None
        self._console_host = host
        # Whatever is up describes the console that was there before.
        self._console_facts = {}
        self._paint_console()
        if not host:
            return None
        if not self.isVisible():
            # Nobody can see this bar, so nobody is waiting on these two
            # labels, and the console on the other end is somebody's games
            # machine. The reading is left owed until showEvent, which is the
            # same rule the figures on the home screen follow and the same
            # reason: several tests drive a window that was never shown as far
            # as connected, using addresses that belong to real machines on
            # this network.
            self._console_host = ""
            return None
        read = self._read_console

        def work(control):
            if control.cancelled:
                return None
            return {"host": host, "facts": read(host) or {}}

        task = self.services.submit(work)
        # finished and nothing else. A failure leaves this side of the bar
        # empty and says nothing: none of it is worth a word in front of
        # somebody who has just got their console to answer.
        task.finished.connect(self._console_read)
        return task

    def showEvent(self, event):
        """Take a reading that was owed while there was nobody to see it."""
        super().showEvent(event)
        self._sync_console()

    def _read_console(self, host):
        """What the console says about itself. The seam; tests replace this.

        Runs on a worker and never on the GUI thread. read_console is the same
        reader the figures on the home screen come from, off the same two
        pages, so this asks a console for nothing it is not asked elsewhere.
        """
        try:
            return consolestats.read_console(host)
        except Exception:                                   # noqa: BLE001
            # A console that will not answer has said nothing, and nothing is
            # exactly what this side of the bar then shows.
            return {}

    def _console_read(self, result):
        if not result or result.get("host") != self._console_host:
            # The address moved on while the worker was out.
            return
        self._console_facts = result.get("facts") or {}
        self._paint_console()

    def _paint_console(self):
        firmware = consolestats.firmware_text(self._console_facts)
        self._firmware_label.setText(firmware)
        self._firmware_label.setVisible(bool(firmware))
        free = consolestats.free_space_text(self._console_facts)
        self._free_label.setText(free)
        self._free_label.setVisible(bool(free))

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
            # Hosts, not counts. The search sweeps a second time when the
            # first found nothing -- the first packet to an address nobody has
            # spoken to waits on ARP, and a console can miss a short timeout
            # because of it -- so counting increments would report one console
            # as two and five addresses as ten.
            tally = {"answered": set(), "fetched": set(), "best": 0,
                     "best_host": ""}
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
                        tally["answered"].add(host)
                return opened

            def counted_fetch(host):
                body = real_fetch(host)
                if not body:
                    return body
                score = webman_score(body)[0]
                with lock:
                    tally["fetched"].add(host)
                    if score > tally["best"]:
                        tally["best"], tally["best_host"] = score, host
                return body

            candidates = discovery.scan(
                hosts, counted_fetch,
                connect=counted_connect,
                connect_timeout=connect_timeout,
                on_progress=lambda done, total, found: control.progress(
                    (done, total, len(tally["answered"]))),
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
            # Exactly one console, so fill the address in and check it without
            # making the user press a second button. Pressing Find is already
            # a statement that they want this console found and used; leaving
            # the status on "Not checked" next to an address the program just
            # discovered reads as though the search failed.
            #
            # Only for a single match. Picking one of several is the user's
            # choice, and _choose handles that case itself.
            self.set_address(candidates[0].address)
            self.check()
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
        # Sets since the retry sweep landed, but a cached result from an
        # older run may still hold plain counts, so accept either.
        answered = tally.get("answered", 0)
        answered = len(answered) if isinstance(answered, (set, list)) \
            else answered
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
            fetched = len(fetched) if isinstance(fetched, (set, list)) \
                else fetched
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
            # Picking one out of the list is the same statement as a single
            # match: this is the console. Check it rather than leaving the
            # status reading "Not checked" beside an address just chosen.
            self.set_address(dialog.chosen())
            self.check()

    def _refresh(self):
        # A console that has just answered can be saved, so the control that
        # saves it appears now rather than on the next address change.
        self._paint_console_button()
        state = self._connection.connection
        word, token = CONNECTION_WORDS.get(state, ("Not checked", "text_dim"))
        colour = self._theme.colour(token)
        self._dot.set_colour(colour)
        # It breathes while there is a console on the other end and is still
        # while there is not, so the one moving thing on the bar means
        # something rather than being decoration.
        self._dot.set_breathing(state == "connected")
        self._state_label.setText(word)
        self._state_label.setStyleSheet(f"color: {colour};")
        detail = self._connection.connection_detail
        self._detail_label.setText(detail)
        self._detail_label.setVisible(bool(detail))
        # Once a console is connected the pair become one thing: Check turns
        # into Disconnect, and Find is refused while a console is in use.
        # Searching the network for another one while a tool is pointed at this
        # one is not something anybody means to do, and an address that arrived
        # by itself halfway through a transfer would be worse than useless.
        connected = state == "connected"
        scanning = self._connection.scan == "scanning"
        busy = state == "checking" or scanning
        self._check_button.setText(DISCONNECT_LABEL if connected
                                   else CHECK_LABEL)
        self._check_button.setProperty("primary", not connected)
        self._check_button.setProperty("danger", connected)
        self._check_button.setToolTip(
            DISCONNECT_HINT if connected else CHECK_HINT)
        self._check_button.setEnabled(connected or not busy)
        # Re-polish, or the property change does not repaint.
        self._check_button.style().unpolish(self._check_button)
        self._check_button.style().polish(self._check_button)

        # Never coloured, never merged into the pill above.
        scan_detail = self._connection.scan_detail
        self._scan_label.setText(scan_detail)
        self._scan_label.setVisible(bool(scan_detail))
        self._scan_label.setStyleSheet(
            f"color: {self._theme.colour('text_dim')};")
        self._find_button.setEnabled(not scanning and not connected
                                     and state != "checking")

        if self._address.text().strip() != self._connection.host:
            blocked = self._address.blockSignals(True)
            self._address.setText(self._connection.host)
            self._address.blockSignals(blocked)

        # The right-hand side follows the connection: a console that has just
        # answered is asked what it is, and one that has gone takes its
        # figures with it.
        self._sync_console()
        self._paint_event()

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
        # A saved console answering on a new address is still that console,
        # so the profile follows it. An address nobody has saved creates
        # nothing: that is what the Save button is for.
        settings = getattr(self.services, "settings", None)
        if settings is not None and host:
            key = profiles.current_id(settings)
            if key and not profiles.for_host(settings, host):
                profiles.set_host(settings, key, host)
        self._paint_console_button()

    # -- which console

    def _paint_console_button(self):
        """Name the console, or offer to save it.

        Shown as soon as a console answers. It used to appear only once there
        were two, which left no way to make the second: the menu that adds one
        was behind a button that needed one to exist.
        """
        settings = getattr(self.services, "settings", None)
        if settings is None:
            self._console_button.hide()
            return
        host = self._connection.host
        connected = self._connection.connection == "connected"
        known = profiles.all_profiles(settings)
        saved = profiles.named_id(settings, host) if host else ""
        self._console_button.setVisible(bool(connected or len(known) > 1))
        if saved:
            self._console_button.setText(known[saved]["name"])
            self._console_button.setMenu(self._console_menu)
            self._console_button.setToolTip(
                "Switch between your consoles, rename this one, or forget it.")
        else:
            # No menu while there is nothing to choose between: the press
            # saves rather than opening a list of one thing.
            self._console_button.setText("Save this console")
            self._console_button.setMenu(None)
            self._console_button.setToolTip(
                "Give this PS3 a name so it is remembered separately from "
                "any other.")

    def _console_pressed(self):
        """Save this console, when it is not one yet. The menu handles the
        rest, and a button with a menu does not reach here at all."""
        settings = getattr(self.services, "settings", None)
        host = self._connection.host
        if settings is None or not host or profiles.named_id(settings, host):
            return
        name, taken = QInputDialog.getText(
            self, "Save this console", "What do you call this PS3?",
            text=profiles.DEFAULT_NAME)
        if not taken:
            return
        # It may already have an unnamed profile holding what was remembered
        # about it. Naming that one keeps the remembered list.
        key = profiles.for_host(settings, host)
        if key:
            profiles.rename(settings, key, name)
            profiles.select(settings, key)
        else:
            profiles.add(settings, host=host, name=name)
        self._paint_console_button()

    def _fill_console_menu(self):
        settings = getattr(self.services, "settings", None)
        menu = self._console_menu
        menu.clear()
        if settings is None:
            return
        here = profiles.current_id(settings)
        for key, profile in profiles.all_profiles(settings).items():
            label = profile["name"]
            if profile["host"]:
                label = f"{label}  ({profile['host']})"
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(key == here)
            action.triggered.connect(
                lambda _checked=False, chosen=key: self._switch_console(chosen))
            menu.addAction(action)
        menu.addSeparator()
        rename = QAction("Rename this console...", menu)
        rename.triggered.connect(self._rename_console)
        menu.addAction(rename)
        another = QAction("Add another console...", menu)
        another.triggered.connect(self._add_console)
        menu.addAction(another)
        forget = QAction("Forget this console", menu)
        forget.triggered.connect(self._forget_console)
        menu.addAction(forget)

    def _forget_console(self):
        """Remove this console and everything remembered about it."""
        settings = getattr(self.services, "settings", None)
        if settings is None:
            return
        key = profiles.current_id(settings)
        current = profiles.all_profiles(settings).get(key)
        if not current:
            return
        answer = QMessageBox.question(
            self, "Forget this console",
            f"Forget {current['name']}?\n\nIts saved address and the list "
            f"of games this program last found on it are removed from this "
            f"computer. Nothing on the console itself is changed.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        profiles.forget(settings, key)
        host = str(settings.get(profiles.HOST_KEY) or "")
        self._connection.set_host(host)
        self._address.setText(host)
        self._paint_console_button()

    def _switch_console(self, key):
        settings = getattr(self.services, "settings", None)
        if settings is None:
            return
        host = profiles.select(settings, key)
        self._connection.set_host(host)
        self._address.setText(host)
        self._paint_console_button()

    def _rename_console(self):
        settings = getattr(self.services, "settings", None)
        if settings is None:
            return
        key = profiles.ensure(settings, host=self._connection.host)
        current = profiles.all_profiles(settings).get(key, {})
        name, taken = QInputDialog.getText(
            self, "Name this console", "What do you call this PS3?",
            text=current.get("name", ""))
        if taken:
            profiles.rename(settings, key, name)
            self._paint_console_button()

    def _add_console(self):
        settings = getattr(self.services, "settings", None)
        if settings is None:
            return
        name, taken = QInputDialog.getText(
            self, "Add a console", "What do you call it?")
        if not taken:
            return
        profiles.add(settings, host="", name=name)
        self._connection.set_host("")
        self._address.setText("")
        self._paint_console_button()


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
        # The floor, not the target. Anything below this clips a screen
        # somewhere, and every screen is built to scroll rather than trap
        # content when it is squeezed to it.
        self.setMinimumSize(1024, 720)
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

        self.status_bar = self._build_status_bar()
        column.addWidget(self.status_bar)

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
        # Taller than wide-ish on purpose. The screens that matter are lists
        # that grow downwards -- collection progress, a queue of transfers, a
        # column of findings -- so vertical room buys more than horizontal.
        # 1050 rather than 900 puts the whole of the diagnostics screen on one
        # page at the default size instead of most of it.
        width, height = 1280, 1050
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

        # An icon rather than the word, and the menu arrow suppressed in the
        # stylesheet: with both it read as a form control sitting in a strip
        # of toolbar buttons.
        self.theme_button = QPushButton(bar)
        self.theme_button.setObjectName("themeButton")
        self.theme_button.setProperty("flat", True)
        self.theme_button.setToolTip("Light, dark, or whichever the desktop "
                                     "is using.")
        self.theme_button.setAccessibleName("Theme")
        self.theme_menu = self._build_theme_menu(bar)
        self.theme_button.setMenu(self.theme_menu)
        row.addWidget(self.theme_button)
        return bar

    def _build_brand(self, parent):
        """The wordmark, or the words it is a picture of if it is missing.

        A checkout or a build without logo.png still has to start and still has
        to say whose program it is, so the text version stays as the fallback
        rather than the top left going blank.
        """
        brand = QWidget(parent)
        row = QHBoxLayout(brand)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        self.brand_logo = None
        self.vendor_link = LinkLabel(VENDOR, PROJECT_URL, brand)
        path = brand_logo_path()
        if path:
            logo = BrandLogo(path, PROJECT_URL, BRAND_LOGO_HEIGHT, brand)
            if logo.usable:
                logo.apply_theme(self.theme.dark)
                row.addWidget(logo)
                self.brand_logo = logo
                # Kept, not shown: the About screen and the tests both ask the
                # shell for the project link and neither should have to know
                # whether the picture loaded.
                self.vendor_link.hide()
                return brand
            logo.deleteLater()
        title = QLabel(APP_NAME, brand)
        title.setObjectName("appTitle")
        row.addWidget(title)
        byline = QLabel("by", brand)
        byline.setObjectName("dim")
        row.addWidget(byline)
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

    #: One sweep of the status panel's top border, and then it goes static.
    #: Long enough to be noticed on a glance away from the panel, short enough
    #: that it is over before anybody looks for a way to stop it.
    SWEEP_MS = 900

    def sweep_status(self):
        """One pass of light along the status panel's edge.

        Called when a connection succeeds and when an upload finishes, which
        are the two moments something was waited for. One event, one
        animation: the sweep runs once and the panel is static again after
        it, so nothing on screen moves while somebody is reading.
        """
        holder = getattr(self, "status_bar", None)
        sweep = getattr(self, "_status_sweep", None)
        if holder is None or sweep is None:
            return
        sweep.stop()
        sweep.start()

    def _build_status_bar(self):
        holder = QWidget(self)
        holder.setObjectName("statusBar")
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        # Owned by the panel so it dies with it. A loose animation outliving
        # the widget it paints is a crash on the way out.
        self._sweep_position = 0.0
        self._status_sweep = QVariantAnimation(holder)
        self._status_sweep.setDuration(self.SWEEP_MS)
        self._status_sweep.setStartValue(0.0)
        self._status_sweep.setEndValue(1.0)
        self._status_sweep.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._status_sweep.setLoopCount(1)
        self._status_sweep.valueChanged.connect(self._on_sweep)
        self._status_sweep.finished.connect(lambda: self._on_sweep(0.0))

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
        self._paint_theme_button()
        if getattr(self, "brand_logo", None) is not None:
            self.brand_logo.apply_theme(self.theme.dark)
        self.back_button.setIcon(back_arrow_icon(
            self.theme.colour("text"), self.theme.colour("text_dim")))
        self._paint_status()
        self.update()

    def _on_sweep(self, value):
        """Paint the sweep. Position only: the panel keeps its own colours."""
        self._sweep_position = float(value or 0.0)
        holder = getattr(self, "status_bar", None)
        if holder is not None:
            holder.update()

    #: Long enough to read as a turn rather than a flicker.
    THEME_SPIN_MS = 260

    def _paint_theme_button(self):
        button = getattr(self, "theme_button", None)
        if button is None:
            return
        button.setIcon(icons.icon("theme", self.theme.colour("text"),
                                  size=18))
        button.setIconSize(QSize(18, 18))
        self._spin_theme_button(button)

    def _spin_theme_button(self, button):
        """The icon eases out and back as the theme changes under it.

        Owned by the button so it dies with it. The size is animated rather
        than the icon rotated: rotating means repainting the pixmap every
        frame, and this says the same thing for a fraction of the work.
        """
        spin = getattr(self, "_theme_spin", None)
        if spin is None:
            spin = QVariantAnimation(button)
            spin.setDuration(self.THEME_SPIN_MS)
            spin.setLoopCount(1)
            spin.setEasingCurve(QEasingCurve.Type.InOutCubic)
            spin.setKeyValueAt(0.0, 18.0)
            spin.setKeyValueAt(0.5, 12.0)
            spin.setKeyValueAt(1.0, 18.0)
            spin.valueChanged.connect(
                lambda value: button.setIconSize(
                    QSize(int(value), int(value))))
            self._theme_spin = spin
        spin.stop()
        spin.start()

    def offer_to_find_console(self):
        """Ask for a console, once, on a start with no address saved.

        Returns the dialog so a test can drive it, or None when there was
        nothing to ask. Dismissing it leaves the application exactly as it is
        without one: every screen still opens and says what it needs.
        """
        if self.connection.host:
            return None
        dialog = FirstRunDialog(self.connection_bar, self.theme, self)
        self._blur(True)
        dialog.finished.connect(lambda _result: self._blur(False))
        dialog.show()
        self._centre_on_window(dialog)
        return dialog

    def _centre_on_window(self, dialog):
        dialog.adjustSize()
        centre = self.geometry().center()
        dialog.move(centre.x() - dialog.width() // 2,
                    centre.y() - dialog.height() // 2)

    def _blur(self, on):
        """Soften the window behind the first-run dialog.

        The dialog is a window of its own, so the effect on the central widget
        does not reach it. Wrapped because a graphics effect is the sort of
        thing a remote desktop or a software renderer refuses, and a blur that
        will not apply is not a reason to withhold the dialog.
        """
        target = self.centralWidget()
        if target is None:
            return
        try:
            if on:
                effect = QGraphicsBlurEffect(target)
                effect.setBlurRadius(9)
                target.setGraphicsEffect(effect)
            else:
                target.setGraphicsEffect(None)
        except Exception:                                   # noqa: BLE001
            pass

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
        # Optional: a screen that can hand the user on to another tool. The
        # patcher uses it to send somebody whose title update is wrong to the
        # Game updates card rather than leaving them to find it themselves.
        # Attribute-checked because the screen interface is frozen and this is
        # not on it; a screen without the signal is the normal case.
        if hasattr(screen, "request_tool"):
            screen.request_tool.connect(self._open_tool)
        # Optional in the same way: a screen that knows when it has finished
        # something worth remembering says so, and the bar shows the last one.
        if hasattr(screen, "event_noted"):
            screen.event_noted.connect(self.note_event)
        screen.hide()
        self._screens[key] = screen
        return screen

    def _open_tool(self, key, subject=""):
        """Navigate to another tool, telling it what the user came about.

        The target decides what to do with the subject. A screen that has no
        preselect gets shown anyway: arriving at the right card with nothing
        filled in is a far better outcome than a button that does nothing.
        """
        target = self.screen_for(key)
        if target is None:
            crashreport.note(f"asked for the missing tool {key}")
            return False
        preselect = getattr(target, "preselect", None)
        if subject and callable(preselect):
            try:
                preselect(subject)
            except Exception:
                # Landing on the card still helps. Failing to pre-filter is
                # not a reason to refuse to navigate.
                pass
        return self.open_screen(key)

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

    def note_event(self, text):
        """Record something that has just finished, for the connection bar.

        The one place the rest of the window records an event, so that a
        screen holds no opinion about where the line is drawn.

        The panel sweeps once at the same moment. An event reaching here is
        something that was waited for, which is exactly when a sweep is worth
        having, and it keeps the two ways of saying "that finished" together.
        """
        self.connection_bar.note_event(text)
        if text:
            self.sweep_status()

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
        # Ask first, wait second. Cancellation is co-operative -- a worker
        # stops at its next check -- so waiting before asking would simply
        # sit through whatever is in flight.
        self.connection_bar.cancel_scan()
        if screen is not None:
            screen.on_leave()
        self._drain_workers()
        self.store_settings()
        super().closeEvent(event)

    def start_launch(self):
        """Everything that happens once, at the start, in a settled order.

        The order is the point. These used to be two singleShot calls with no
        relationship to each other, and the result depended on which finished
        first: on a fresh install the update check was announced to a window
        with a modal over it and the news was never seen.

        1. A console that is already known is connected to straight away, so
           the window is usable by the time somebody looks at it.
        2. A console that is not known is asked for, first thing, before
           anything else can take the foreground.
        3. The update check runs either way, and whatever it finds is shown
           when there is a window to show it on.

        The check is forced and runs whatever the setting says. Every start
        of the program asks GitHub whether there is a newer release. Two
        things used to stop it: the cache, whose timestamp is written even
        when the fetch failed, so a machine that was offline once did not ask
        again until the next day; and the setting, which is now overridden
        here and here only. The About screen's button still respects it.

        This is the only way somebody running an old build hears about a fix,
        and it costs one request. Nothing about it is louder than the banner:
        a check that finds nothing, or cannot run, says nothing at all.
        """
        host = self.connection.host if self.connection else ""
        dialog = None
        if host:
            self.connection_bar.check()
        else:
            dialog = self.offer_to_find_console()
        if dialog is not None:
            # The news may arrive while this is up, over a blurred window.
            # Say it again when the window comes back.
            dialog.finished.connect(
                lambda _result: self.update_banner.reassert())
        self.update_banner.start_check(force=True,
                                       whatever_the_setting=True)
        return dialog

    def _drain_workers(self):
        """Wait for the worker pool, but never hold the window open on it.

        Work runs on the Services' own pool and nothing else waits for it, so
        without this the window closes while workers are still running and Qt
        starts tearing down underneath them. The wait is bounded: a
        worker that will not stop is recorded and the window closes anyway,
        because a program that will not shut down is worse than one that
        leaves a thread behind.
        """
        try:
            if self.services.wait(SHUTDOWN_WAIT_MS):
                return
            still = ", ".join(self.services.running()) or "unnamed work"
            crashreport.note(f"closed with work still running: {still}")
        except Exception:                                   # noqa: BLE001
            # Never allowed to stop the window closing.
            pass


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


#: How long a thread that will not stop is given once the window has gone.
#: Long enough for a read that is nearly finished, short enough that nobody
#: sits watching a program with no window in their task manager.
SHUTDOWN_GRACE = 2.0


def lingering_threads():
    """Threads that would hold the interpreter open after the window closes.

    Python joins every non-daemon thread before it exits, and
    concurrent.futures registers its executors' threads for exactly that. The
    disc image pass and the console search both use an executor, so one read
    waiting on a console that has gone quiet holds the whole program open.
    """
    here = threading.current_thread()
    return [thread for thread in threading.enumerate()
            if thread is not here and thread.is_alive()
            and not thread.daemon]


def finish(code, wait=SHUTDOWN_GRACE, exit_now=None):
    """End the process, whatever is still holding a socket open.

    Closing the window left the program running with nothing on screen. The
    window had gone, the event loop had ended, and the interpreter was waiting
    on a worker that was itself waiting on a console.

    Ending it without asking is safe here. Every screen that writes to a
    console refuses to close while it is writing, so whatever is still going
    at this point is a read, and somebody who has closed the window has said
    they are finished. A short grace period first, so an ordinary shutdown
    stays an ordinary shutdown.
    """
    deadline = time.monotonic() + max(0.0, wait)
    while lingering_threads() and time.monotonic() < deadline:
        time.sleep(0.05)
    left = lingering_threads()
    if not left:
        return code
    crashreport.note("still running at exit: " + ", ".join(
        sorted(thread.name for thread in left)))
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:                                   # noqa: BLE001
            pass
    (exit_now or os._exit)(code)
    return code


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
    # Only from the real entry point. build() is left free of it so that
    # anything constructing a window for another reason -- a test, a
    # screenshot -- asks nobody anything and reaches no network.
    QTimer.singleShot(0, window.start_launch)
    return finish(application.exec())
