"""The strip that appears when there is a newer version, and nothing else.

Self-contained: it is a plain QWidget, it starts hidden, and it asks for
nothing beyond the Services every screen already gets. Drop it into a layout,
call start_check() once and forget about it.

It is a banner and not a dialog on purpose. A modal box at launch stops a
person doing the thing they opened the program to do, and this news is never
urgent enough for that. It is dismissable, it never appears twice in a run
once dismissed, and if the check fails it never appears at all.

One line high, always. The banner sits in the window's fixed column above the
pages, where every pixel it takes is a pixel the tool below it does not get,
so it says the gist and links to the rest. The release notes live on the
release page; rendering them here made the strip a page section and squeezed
the actions into whatever height was left over.

Nothing here downloads until a button is pressed, and nothing here runs or
replaces anything. See ps3tools/update.py for why.
"""

from PySide6.QtCore import QEvent, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontMetrics
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import update
from .widgets import mix

#: A hard cap on the line before it is measured. Elision is what decides what
#: is shown; this only stops QFontMetrics being handed a whole notes body.
NOTES_LIMIT = 420

DOWNLOADING = "Downloading. This does not replace the running program."

#: Said out loud rather than left as a button that looks live and is not.
NO_ASSET = ("This release has no Windows download. Open the release page to "
            "see what is in it.")


def first_line(notes, limit=NOTES_LIMIT):
    """The first line worth reading out of a release notes body, or "".

    Markdown headings and list bullets are stripped: they are punctuation for
    a page, and on a one line banner they read as noise.
    """
    for line in (notes or "").splitlines():
        line = " ".join(line.split())
        line = line.lstrip("#*->+ ").strip()
        if line:
            return line[:limit]
    return ""


class UpdateBanner(QWidget):
    """One coloured strip, a toolbar row high.

    compact=False is the About screen's copy: the same widget, allowed a
    second line for the result of a download because it is not competing with
    a tool for the window. It is still one line of release, never the notes.

    Signals, all of which the shell may ignore:

        checked(object)   a Release, or None when there is nothing to say
        shown()           the banner has become visible
        dismissed()       the user closed it
        downloaded(object)  an update.Download, after the user asked for one
    """

    checked = Signal(object)
    shown = Signal()
    dismissed = Signal()
    downloaded = Signal(object)

    def __init__(self, services, parent=None, fetcher=None, downloader=None,
                 opener=None, compact=True):
        super().__init__(parent)
        self.services = services
        self.theme = services.theme
        self.settings = services.settings
        # Injected in tests. The defaults are resolved inside update.py, so
        # nothing here holds a reference to urllib at all.
        self._fetcher = fetcher
        self._downloader = downloader
        self._opener = opener or self._open_url
        self.compact = bool(compact)
        self.release = None
        self._task = None
        self._downloading = False
        self._dismissed = False
        self._result = None
        self._message = ""
        self._status_full = ""
        self._eliding = False

        self.setObjectName("updateBanner")
        # Without this a plain QWidget paints the ground behind it and
        # ignores the background in its own stylesheet, which is why the
        # strip had no colour of its own and its buttons read as part of
        # the page rather than as live controls. Every other bar in the
        # shell sets it for the same reason.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Fixed, not Maximum. Maximum let the column squeeze the strip below
        # its own sizeHint, which is how the actions ended up crushed against
        # the bottom edge on a short window while the About copy, sitting in a
        # scroll area that always grants a full height, looked fine.
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)

        column = QVBoxLayout(self)
        column.setContentsMargins(14, 6, 10, 6)
        column.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.message = QLabel("")
        message_font = QFont(self.font())
        message_font.setWeight(QFont.Weight.DemiBold)
        self.message.setFont(message_font)
        # No word wrap anywhere on this widget: wrapping is what turned a
        # notification into a paragraph. The text is elided to the width it
        # actually gets, in _elide.
        self.message.setWordWrap(False)
        self.message.setMinimumWidth(1)
        self.message.setSizePolicy(QSizePolicy.Policy.Ignored,
                                   QSizePolicy.Policy.Fixed)
        self.message.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.message, 1)

        self.download_button = QPushButton(
            "Download" if self.compact else "Download to Desktop")
        self.download_button.setObjectName("updateDownload")
        self.download_button.clicked.connect(self.download)
        row.addWidget(self.download_button)

        self.page_button = QPushButton("Release page")
        self.page_button.clicked.connect(self.open_release_page)
        row.addWidget(self.page_button)

        self.folder_button = QPushButton("Open the folder")
        self.folder_button.clicked.connect(self.open_folder)
        self.folder_button.setVisible(False)
        row.addWidget(self.folder_button)

        self.dismiss_button = QPushButton("×")
        self.dismiss_button.setObjectName("updateDismiss")
        self.dismiss_button.setToolTip("Dismiss")
        self.dismiss_button.setFixedWidth(28)
        self.dismiss_button.clicked.connect(self.dismiss)
        row.addWidget(self.dismiss_button)
        column.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(False)
        self.status.setMinimumWidth(1)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Fixed)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setVisible(False)
        column.addWidget(self.status)

        # The labels are elided to the width the layout actually hands them,
        # which is not known until after the row has been laid out and changes
        # again whenever a button appears or goes away.
        self.message.installEventFilter(self)
        self.status.installEventFilter(self)

        self.theme.changed.connect(self._apply_theme)
        self._apply_theme()
        self._sync_actions()
        self.setVisible(False)

    # -- appearance

    def _apply_theme(self):
        colour = self.theme.colour
        accent, ink = colour("accent"), colour("accent_text")
        # A notification bar, so the accent is the ground rather than a tint of
        # the surface. accent_text is the token that is guaranteed legible on
        # accent in both palettes, so every mark on the bar is mixed from it.
        raised = mix(accent, ink, 0.18)
        hovered = mix(accent, ink, 0.32)
        edge = mix(accent, ink, 0.45)
        faded = mix(accent, ink, 0.55)
        self.setStyleSheet(f"""
#updateBanner {{ background: {accent}; border: 0; }}
#updateBanner QLabel {{ color: {ink}; background: transparent; }}
#updateBanner QPushButton {{
    background: {raised}; color: {ink};
    border: 1px solid {edge}; border-radius: 6px; padding: 3px 10px;
}}
#updateBanner QPushButton:hover {{ background: {hovered}; }}
#updateBanner QPushButton:pressed {{ background: {edge}; }}
#updateBanner QPushButton:disabled {{
    background: transparent; color: {faded}; border-color: {faded};
}}
#updateBanner QPushButton#updateDownload {{
    background: {ink}; color: {accent};
    border: 1px solid {ink}; font-weight: 600;
}}
#updateBanner QPushButton#updateDownload:hover {{
    background: {mix(ink, accent, 0.12)};
}}
#updateBanner QPushButton#updateDownload:disabled {{
    background: transparent; color: {faded}; border-color: {faded};
}}
#updateBanner QPushButton#updateDismiss {{
    background: transparent; border-color: transparent;
    font-weight: 600; padding: 3px 0;
}}
#updateBanner QPushButton#updateDismiss:hover {{ background: {raised}; }}
""")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.Resize
                and watched in (self.message, self.status)):
            self._elide()
        return super().eventFilter(watched, event)

    def _elide(self):
        """Truncate to the width the label actually got, not to a guess."""
        if self._eliding:
            return
        self._eliding = True
        try:
            self._elide_now()
        finally:
            self._eliding = False

    def _elide_now(self):
        for label, text in ((self.message, self._message),
                            (self.status, self._status_full)):
            width = label.width()
            if width <= 0:
                label.setText(text)
                continue
            label.setText(QFontMetrics(label.font()).elidedText(
                text, Qt.TextElideMode.ElideRight, width))

    def message_text(self):
        """The whole line, before it was elided to the width it was given."""
        return self._message

    def status_text(self):
        """The whole of the last thing said about a download, un-elided."""
        return self._status_full

    def _set_message(self, text):
        self._message = text
        self._elide()

    def _say(self, text):
        """Report the state of a download.

        In the top bar it replaces the release line, because the bar is one
        line and a second one would make it the thing it stopped being.
        """
        self._status_full = text
        if self.compact:
            self._set_message(text)
        else:
            self.status.setVisible(bool(text))
        self._elide()
        self.updateGeometry()

    # -- the check

    def start_check(self, force=False):
        """Run the check on a worker and show the banner if there is news.

        Never blocks the GUI thread and never reports a failure: a check that
        could not run is not something the user asked for or can act on.
        """
        if self._task is not None:
            return None
        if self._dismissed and not force:
            return None
        settings = self.settings
        fetcher = self._fetcher

        def work(control):
            return update.check(settings, fetcher=fetcher, force=force)

        task = self.services.submit(work)
        self._task = task
        self._downloading = False
        task.finished.connect(self._checked)
        # Silence is the whole policy. failed only fires if update.check
        # raised, which it is written not to do, and even then nothing is said.
        task.failed.connect(lambda _message: self._checked(None))
        task.done.connect(self._task_done)
        return task

    def _task_done(self):
        self._task = None
        self._downloading = False
        self._sync_actions()

    def _checked(self, release):
        if release is not None:
            self.show_release(release)
        self.checked.emit(release)

    def show_release(self, release):
        """Display a release. Public so the About screen can hand one over."""
        self.release = release
        self._dismissed = False
        self._result = None
        self._status_full = ""
        self.status.setVisible(False)
        self._set_message(self.release_line(release))
        self._sync_actions()
        self.setVisible(True)
        self.shown.emit()

    def release_line(self, release):
        """The whole of what the banner says about a release, on one line."""
        line = f"{release.summary()}."
        if not release.asset_url:
            return f"{line} {NO_ASSET}"
        notes = first_line(release.notes)
        return f"{line}  {notes}" if notes else line

    # -- button state, in one place

    def _sync_actions(self):
        """The only thing that decides what the actions look like.

        Derived from the state every time rather than set once on the path the
        release happened to arrive by, so the automatic launch check and the
        About screen's forced check cannot disagree about it.
        """
        release = self.release
        has_asset = bool(release is not None and release.asset_url)
        saved = bool(self._result is not None and self._result.ok)
        # Offering the same button again after a success invites a second copy
        # on the Desktop and a question about which one is the real one.
        self.download_button.setVisible(release is not None and not saved)
        self.download_button.setEnabled(
            has_asset and not saved and not self._downloading)
        self.download_button.setToolTip("" if has_asset else NO_ASSET)
        self.page_button.setVisible(
            bool(release is not None and release.page_url))
        self.folder_button.setVisible(
            bool(self._result is not None and self._result.path))

    # -- actions, none of which happen on their own

    def download(self):
        if self.release is None or self._task is not None:
            return None
        if not self.release.asset_url:
            return None
        release = self.release
        downloader = self._downloader
        self._downloading = True
        self._say(DOWNLOADING)

        def work(control):
            return update.download(release, fetcher=downloader)

        task = self.services.submit(work)
        self._task = task
        task.finished.connect(self._download_finished)
        task.failed.connect(self._download_failed)
        task.done.connect(self._task_done)
        self._sync_actions()
        return task

    def _download_finished(self, result):
        self._result = result
        self._downloading = False
        self.downloaded.emit(result)
        self._say(result.detail)
        self._sync_actions()

    def _download_failed(self, message):
        self._result = None
        self._downloading = False
        self._say("The download did not finish. Fetch it from the release "
                  "page in your browser instead.")
        self._sync_actions()

    def open_release_page(self):
        if self.release is not None and self.release.page_url:
            return self._opener(self.release.page_url)
        return False

    def open_folder(self):
        result = self._result
        folder = result.folder if result is not None else ""
        if not folder:
            folder = update.target_folder()
        return self._opener(QUrl.fromLocalFile(folder).toString())

    def dismiss(self):
        self._dismissed = True
        self.setVisible(False)
        self.dismissed.emit()

    @staticmethod
    def _open_url(url):
        """The system browser or file manager. This program has no business
        rendering a web page and no business being a file manager."""
        return QDesktopServices.openUrl(QUrl(url))
