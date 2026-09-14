"""Colour for the whole application, in one place.

Screens never name a colour. They ask for a token and get whichever of the two
palettes is current, which is the only way both light and dark stay legible
without every screen being reviewed twice.

The two palettes are not the same hues at different lightnesses. On white a
status colour has to be dark enough to read as text, and on near-black it has
to be light enough; the greens in particular are a long way apart. Both sets
are checked against the tokens they are actually drawn on by
tests/test_shell.py, so a future tweak that looks nicer and reads worse fails
the build rather than shipping.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

from .screen import THEME_TOKENS, Theme

MODES = ("system", "light", "dark")

PALETTES = {
    "light": {
        "bg": "#eef0f5",
        "surface": "#ffffff",
        "surface_alt": "#e4e8f0",
        "border": "#c6ccd8",
        "text": "#12161d",
        "text_dim": "#525a68",
        "accent": "#1b5fd0",
        "accent_text": "#ffffff",
        "ok": "#0a6b45",
        "warn": "#8a4b00",
        "error": "#b3261e",
        "info": "#0f4fad",
    },
    "dark": {
        "bg": "#0d0f14",
        "surface": "#161a22",
        "surface_alt": "#1e2430",
        "border": "#2d3543",
        "text": "#e9ecf3",
        "text_dim": "#9aa4b6",
        "accent": "#4d8dfa",
        "accent_text": "#06122a",
        "ok": "#4fd39a",
        "warn": "#f0b45e",
        "error": "#ff7b72",
        "info": "#79b8ff",
    },
}


def _channel(value):
    value = value / 255.0
    if value <= 0.03928:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(colour):
    """WCAG relative luminance of a "#rrggbb" string."""
    rgb = QColor(colour)
    return (0.2126 * _channel(rgb.red())
            + 0.7152 * _channel(rgb.green())
            + 0.0722 * _channel(rgb.blue()))


def contrast_ratio(foreground, background):
    """WCAG contrast ratio, 1.0 (identical) to 21.0 (black on white)."""
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def system_prefers_dark():
    """The desktop's own preference, or None when it will not say.

    Qt 6.5 added colorScheme(); older or headless platforms report Unknown, in
    which case the window palette is the next best clue.
    """
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is not None:
        value = scheme()
        if value == Qt.ColorScheme.Dark:
            return True
        if value == Qt.ColorScheme.Light:
            return False
    application = QGuiApplication.instance()
    if application is None:
        return None
    palette = application.palette()
    window = palette.color(QPalette.ColorRole.Window)
    text = palette.color(QPalette.ColorRole.WindowText)
    if window == text:
        return None
    return window.lightness() < text.lightness()


class AppTheme(Theme):
    """The real theme. One instance, handed to every screen by Services."""

    def __init__(self, mode="system", parent=None):
        super().__init__(parent)
        self._mode = mode if mode in MODES else "system"
        self._dark = self._resolve()

    # -- what screens use
    def colour(self, token):
        if token not in THEME_TOKENS:
            raise KeyError(f"{token!r} is not a theme token")
        return PALETTES["dark" if self._dark else "light"][token]

    @property
    def dark(self):
        return self._dark

    # -- what the shell uses
    @property
    def mode(self):
        """"system", "light" or "dark". The user's choice, not the result."""
        return self._mode

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(f"unknown theme mode {mode!r}")
        if mode == self._mode:
            return
        self._mode = mode
        self._apply(self._resolve())

    def toggle(self):
        """Light and dark only. Once a user has picked a side, following the
        desktop again is a thing they have to ask for by name."""
        self.set_mode("light" if self._dark else "dark")

    def refresh_from_system(self):
        """The desktop changed its mind. Only matters while following it."""
        if self._mode == "system":
            self._apply(self._resolve())

    def palette(self):
        return dict(PALETTES["dark" if self._dark else "light"])

    def _resolve(self):
        if self._mode == "dark":
            return True
        if self._mode == "light":
            return False
        prefers = system_prefers_dark()
        return True if prefers is None else prefers

    def _apply(self, dark):
        if dark == self._dark:
            return
        self._dark = dark
        self.changed.emit()


def qt_palette(theme):
    """A QPalette matching the tokens, so stock widgets in dialogs and menus
    are not left painting themselves in the desktop's colours inside a window
    painted in ours."""
    tokens = theme.palette()
    palette = QPalette()
    roles = QPalette.ColorRole
    palette.setColor(roles.Window, QColor(tokens["bg"]))
    palette.setColor(roles.WindowText, QColor(tokens["text"]))
    palette.setColor(roles.Base, QColor(tokens["surface"]))
    palette.setColor(roles.AlternateBase, QColor(tokens["surface_alt"]))
    palette.setColor(roles.Text, QColor(tokens["text"]))
    palette.setColor(roles.Button, QColor(tokens["surface_alt"]))
    palette.setColor(roles.ButtonText, QColor(tokens["text"]))
    palette.setColor(roles.Highlight, QColor(tokens["accent"]))
    palette.setColor(roles.HighlightedText, QColor(tokens["accent_text"]))
    palette.setColor(roles.ToolTipBase, QColor(tokens["surface"]))
    palette.setColor(roles.ToolTipText, QColor(tokens["text"]))
    palette.setColor(roles.PlaceholderText, QColor(tokens["text_dim"]))
    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, roles.Text, QColor(tokens["text_dim"]))
    palette.setColor(disabled, roles.ButtonText, QColor(tokens["text_dim"]))
    palette.setColor(disabled, roles.WindowText, QColor(tokens["text_dim"]))
    return palette


def menu_palette(theme):
    """A QPalette for popup menus.

    A QMenu is a top level window of its own. It is not reliably reached by the
    application stylesheet or by the application palette on every platform, so
    the one place a light window could still open a dark popup was here; the
    menu is handed its colours directly as well as being styled.
    """
    tokens = theme.palette()
    palette = qt_palette(theme)
    roles = QPalette.ColorRole
    palette.setColor(roles.Window, QColor(tokens["surface"]))
    palette.setColor(roles.Base, QColor(tokens["surface"]))
    palette.setColor(roles.Button, QColor(tokens["surface"]))
    palette.setColor(roles.WindowText, QColor(tokens["text"]))
    palette.setColor(roles.Text, QColor(tokens["text"]))
    palette.setColor(roles.ButtonText, QColor(tokens["text"]))
    return palette


def stylesheet(theme):
    """Application-wide QSS built from the tokens.

    Written once here rather than per widget, because a screen that sets its
    own stylesheet is a screen that only looks right in one of the two themes.
    """
    tokens = theme.palette()
    tokens["shade"] = ("rgba(255, 255, 255, 0.05)" if theme.dark
                       else "rgba(16, 22, 34, 0.04)")
    return _QSS % tokens


_QSS = """
QWidget {
    color: %(text)s;
    font-size: 10pt;
}
QMainWindow, QDialog, #pageHost, #launcher, #scrollHost {
    background: %(bg)s;
}
QScrollArea { background: transparent; border: 0; }
QLabel { background: transparent; }
QLabel#appTitle { font-size: 12pt; font-weight: 600; }
QLabel#screenTitle { font-size: 11pt; color: %(text_dim)s; }
QLabel#heading { font-size: 20pt; font-weight: 600; }
QLabel#subheading { font-size: 11pt; color: %(text_dim)s; }
QLabel#dim, QLabel#statusHint { color: %(text_dim)s; }

/* The network search's own line. Set in behind a rule of its own so it is not
   read as a continuation of what the connection status just said: the two are
   different questions and one has never been evidence about the other. */
QLabel#scanHint {
    color: %(text_dim)s;
    margin-left: 22px;
    padding-left: 10px;
    border-left: 2px solid %(border)s;
}
QLabel#brandLink { color: %(accent)s; }
QLabel#brandLink:hover { color: %(accent)s; text-decoration: underline; }

/* A hairline between the brand, the breadcrumb and the controls, so the bar
   reads as three groups rather than as loose text. */
QFrame#chromeRule {
    background: %(border)s;
    border: 0;
    max-width: 1px;
    min-width: 1px;
}

/* Pinned below the scrolling cards, so it needs a line to sit behind rather
   than appearing to be the last thing in the list. */
#launcherFoot {
    background: %(bg)s;
    border-top: 1px solid %(border)s;
}

#chrome, #connectionBar, #statusBar {
    background: %(surface)s;
    border: 0;
}
#chrome { border-bottom: 1px solid %(border)s; }
#connectionBar { border-bottom: 1px solid %(border)s; }
#statusBar { border-top: 1px solid %(border)s; }

QLineEdit {
    background: %(surface_alt)s;
    border: 1px solid %(border)s;
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: %(accent)s;
    selection-color: %(accent_text)s;
}
QLineEdit:focus { border: 1px solid %(accent)s; }
QLineEdit:disabled { color: %(text_dim)s; }

QPushButton {
    background: %(surface_alt)s;
    border: 1px solid %(border)s;
    border-radius: 7px;
    padding: 6px 14px;
}
QPushButton:hover { background: %(shade)s; border-color: %(accent)s; }
QPushButton:pressed { background: %(border)s; }
QPushButton:disabled { color: %(text_dim)s; border-color: %(border)s; }
QPushButton[primary="true"] {
    background: %(accent)s;
    color: %(accent_text)s;
    border: 1px solid %(accent)s;
    font-weight: 600;
}
QPushButton[primary="true"]:disabled {
    background: %(surface_alt)s;
    color: %(text_dim)s;
    border-color: %(border)s;
}
/* Room on the left for the chevron, so the arrow and the words read as one
   control rather than as a picture next to some text. */
QPushButton#backButton { padding: 6px 14px 6px 9px; font-weight: 600; }
QPushButton#backButton:hover { border-color: %(accent)s; }

QPushButton[flat="true"] {
    background: transparent;
    border: 1px solid transparent;
    padding: 5px 10px;
}
QPushButton[flat="true"]:hover {
    background: %(surface_alt)s;
    border-color: %(border)s;
}
QPushButton[flat="true"]:disabled { color: %(text_dim)s; }

QProgressBar {
    background: %(surface_alt)s;
    border: 0;
    border-radius: 2px;
    max-height: 3px;
    min-height: 3px;
    text-align: center;
}
QProgressBar::chunk { background: %(accent)s; border-radius: 2px; }

/* Popup menus are separate top level windows, so every colour they use is
   named here rather than left to whatever the desktop would have painted. */
QMenu {
    background: %(surface)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    padding: 4px;
}
QMenu::item {
    background: transparent;
    color: %(text)s;
    padding: 6px 24px 6px 24px;
    border-radius: 5px;
}
QMenu::item:selected {
    background: %(accent)s;
    color: %(accent_text)s;
}
QMenu::item:disabled { color: %(text_dim)s; }
QMenu::separator {
    height: 1px;
    background: %(border)s;
    margin: 4px 8px;
}
QMenu::indicator { width: 14px; height: 14px; margin-left: 6px; }

QToolTip {
    background: %(surface)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    padding: 4px 6px;
}

QScrollBar:vertical {
    background: transparent; width: 10px; margin: 2px;
}
QScrollBar::handle:vertical {
    background: %(border)s; border-radius: 4px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: %(text_dim)s; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""
