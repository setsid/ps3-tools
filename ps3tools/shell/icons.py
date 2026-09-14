"""The icon set: line drawings, authored here, rendered through QSvgRenderer.

Vector rather than glyphs. An icon font would be one more file to ship and one
more thing to go missing on a machine we cannot see, and an emoji is whatever
the desktop decides it is that week, at whatever weight and colour it likes.
These are twenty lines of path data each and they look the same everywhere.

Everything is drawn on the same 24 unit grid at the same stroke weight with
round caps and joins, so the set reads as one family however it is mixed.
Colour is not baked in: a caller asks for a theme token's colour and gets the
icon stroked in it, which is the only way one set serves both palettes.
"""

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Stroke weight and geometry live in the wrapper, not in the bodies, so no
# single icon can drift off the family by accident.
STROKE = 1.8
_GRID = 24
_INK = "@ink@"

_WRAPPER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="%s" stroke-width="%s" '
    'stroke-linecap="round" stroke-linejoin="round">%s</svg>')

# A filled dot or blade, for the few marks too small to read as an outline.
_SOLID = 'fill="%s" stroke="none"' % _INK

_BODIES = {
    # -- tools ------------------------------------------------------------
    "diagnostics": (
        '<rect x="3" y="4.2" width="18" height="15.6" rx="3"/>'
        '<path d="M6.6 13.1h2.6l1.9-4.2 2.5 7 1.7-2.8h2.1"/>'),
    "patch": (
        '<rect x="1.9" y="8.8" width="20.2" height="6.4" rx="3.2" '
        'transform="rotate(-45 12 12)"/>'
        '<rect x="8.9" y="8.9" width="6.2" height="6.2" '
        'transform="rotate(-45 12 12)"/>'),
    "gamepad": (
        '<path d="M8.1 7.2h7.8a4.7 4.7 0 0 1 4.6 3.8l.8 4.6a2.5 2.5 0 0 1 '
        '-4.6 1.8l-1-1.6H8.3l-1 1.6a2.5 2.5 0 0 1-4.6-1.8l.8-4.6a4.7 4.7 0 '
        '0 1 4.6-3.8z"/>'
        '<path d="M7.9 10.7v2.8"/><path d="M6.5 12.1h2.8"/>'
        '<circle cx="15.7" cy="11.2" r="1.05" ' + _SOLID + '/>'
        '<circle cx="17.9" cy="13.4" r="1.05" ' + _SOLID + '/>'),
    "console": (
        '<rect x="2.4" y="7" width="19.2" height="10" rx="2.6"/>'
        '<path d="M6 12h5.4"/>'
        '<circle cx="17.4" cy="12" r="1.2" ' + _SOLID + '/>'),

    # -- status -----------------------------------------------------------
    "check": '<path d="M4.8 12.6l4.6 4.6L19.2 7"/>',
    "warning": (
        '<path d="M10.5 4.6a1.7 1.7 0 0 1 3 0l7.2 13.3a1.7 1.7 0 0 1-1.5 '
        '2.5H4.8a1.7 1.7 0 0 1-1.5-2.5z"/>'
        '<path d="M12 9.6v4.3"/>'
        '<circle cx="12" cy="17" r="1.05" ' + _SOLID + '/>'),
    "error": (
        '<circle cx="12" cy="12" r="8.6"/>'
        '<path d="M9.2 9.2l5.6 5.6"/><path d="M14.8 9.2l-5.6 5.6"/>'),
    "info": (
        '<circle cx="12" cy="12" r="8.6"/>'
        '<path d="M12 11.3v5"/>'
        '<circle cx="12" cy="8" r="1.05" ' + _SOLID + '/>'),
    "shield": (
        '<path d="M12 3.3l7.2 2.6v5.4c0 4.3-2.9 7.5-7.2 9-4.3-1.5-7.2-4.7'
        '-7.2-9V5.9z"/>'
        '<path d="M9.2 12.2l2 2 3.6-3.9"/>'),

    # -- actions ----------------------------------------------------------
    "search": (
        '<circle cx="10.7" cy="10.7" r="6.3"/>'
        '<path d="M15.3 15.3L20 20"/>'),
    "folder": (
        '<path d="M3.2 7.4a2.2 2.2 0 0 1 2.2-2.2h3.4l2.1 2.6h7.5a2.2 2.2 0 '
        '0 1 2.2 2.2v7.6a2.2 2.2 0 0 1-2.2 2.2H5.4a2.2 2.2 0 0 1-2.2-2.2z"/>'
        ),
    "download": (
        '<path d="M12 3.9v10.3"/><path d="M7.8 10.2L12 14.4l4.2-4.2"/>'
        '<path d="M4.6 16.3v2.1a1.7 1.7 0 0 0 1.7 1.7h11.4a1.7 1.7 0 0 0 '
        '1.7-1.7v-2.1"/>'),
    "refresh": (
        '<path d="M20.1 12a8.1 8.1 0 1 1-2.6-6"/>'
        '<path d="M20.4 4.3v4.9h-4.9"/>'),
    "back": '<path d="M19.4 12H5"/><path d="M11 6l-6 6 6 6"/>',
    "forward": '<path d="M4.6 12H19"/><path d="M13 6l6 6-6 6"/>',
    "settings": (
        '<path d="M3.6 8.2h4.7"/><circle cx="10.6" cy="8.2" r="2.2"/>'
        '<path d="M12.8 8.2h7.6"/>'
        '<path d="M3.6 15.8h8.1"/><circle cx="14" cy="15.8" r="2.2"/>'
        '<path d="M16.2 15.8h4.2"/>'),
    "link": (
        '<path d="M10.3 13.7a3.7 3.7 0 0 0 5.3 0l2.6-2.6a3.7 3.7 0 0 0 '
        '-5.3-5.3l-1.3 1.3"/>'
        '<path d="M13.7 10.3a3.7 3.7 0 0 0-5.3 0l-2.6 2.6a3.7 3.7 0 0 0 '
        '5.3 5.3l1.3-1.3"/>'),
    "mail": (
        '<rect x="2.9" y="5.4" width="18.2" height="13.2" rx="2.6"/>'
        '<path d="M4.6 8.6l6.2 4.3a2.1 2.1 0 0 0 2.4 0l6.2-4.3"/>'),
    "network": (
        '<path d="M3.4 9.1a12.2 12.2 0 0 1 17.2 0"/>'
        '<path d="M6.8 12.6a7.4 7.4 0 0 1 10.4 0"/>'
        '<path d="M10.1 16a3.1 3.1 0 0 1 3.8 0"/>'
        '<circle cx="12" cy="19.2" r="1.1" ' + _SOLID + '/>'),
}

_cache = {}


def names():
    """Every icon in the set, sorted."""
    return sorted(_BODIES)


def svg(name, colour):
    """The icon's SVG source, stroked in `colour`. Empty for an unknown name.

    Exposed so a test can read what is drawn without rasterising it.
    """
    body = _BODIES.get(name)
    if body is None:
        return ""
    return _WRAPPER % (colour, STROKE, body.replace(_INK, colour))


def pixmap(name, colour, size=20, ratio=1.0):
    """The icon at `size` logical pixels, stroked in `colour`.

    An unknown name is a transparent pixmap of the right size rather than an
    exception: a missing icon must never be the thing that takes the window
    down, and a hole in the layout is visible enough in review.
    """
    ratio = max(1.0, float(ratio))
    key = (name, colour, int(size), round(ratio, 2))
    hit = _cache.get(key)
    if hit is not None:
        return hit

    device = max(1, int(round(size * ratio)))
    result = QPixmap(QSize(device, device))
    result.setDevicePixelRatio(ratio)
    result.fill(Qt.GlobalColor.transparent)

    source = svg(name, QColor(colour).name())
    if source:
        renderer = QSvgRenderer(QByteArray(source.encode("utf-8")))
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Rendered into the full box: the grid already carries the padding, so
        # every icon in the set lands at the same optical size.
        renderer.render(painter, QRectF(0, 0, device, device))
        painter.end()

    _cache[key] = result
    return result


def icon(name, colour, size=20, ratio=1.0):
    """The same drawing as a QIcon, for buttons and actions."""
    return QIcon(pixmap(name, colour, size, ratio))


def clear_cache():
    """Tests, and anything that has just changed what a token means."""
    _cache.clear()


# Which icon a tool's card wears. Screens declare a key and a two letter tile
# and nothing else -- the Screen interface is frozen -- so the mapping from
# key to drawing lives here rather than on the screens.
_BY_KEY = {
    "diagnostics": "diagnostics",
    "bo2": "gamepad",
    "mw3": "gamepad",
    "about": "info",
}

_BY_WORD = (
    ("diagnos", "diagnostics"),
    ("patch", "patch"),
    ("game", "gamepad"),
    ("scan", "search"),
    ("find", "search"),
    ("log", "folder"),
    ("report", "folder"),
    ("setting", "settings"),
)


def for_key(key, title=""):
    """The icon a screen with this key should wear, or "" for none.

    Falls back to a word in the key or title, then to nothing at all, which is
    the card's cue to draw its two letter tile instead.
    """
    named = _BY_KEY.get(key)
    if named:
        return named
    haystack = f"{key} {title}".lower()
    for word, name in _BY_WORD:
        if word in haystack:
            return name
    return ""
