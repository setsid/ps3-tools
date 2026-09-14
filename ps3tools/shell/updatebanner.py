"""The strip that appears when there is a newer version, and nothing else.

Self-contained: it is a plain QWidget, it starts hidden, and it asks for
nothing beyond the Services every screen already gets. Drop it into a layout,
call start_check() once and forget about it.

It is a banner and not a dialog on purpose. A modal box at launch stops a
person doing the thing they opened the program to do, and this news is never
urgent enough for that. It is dismissable, it never appears twice in a run
once dismissed, and if the check fails it never appears at all.

Nothing here downloads until a button is pressed, and nothing here runs or
replaces anything. See ps3tools/update.py for why.
"""

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import update
from .widgets import mix

#: The banner shows the gist and links to the rest. A release note long enough
#: to need scrolling has stopped being a banner.
NOTES_LIMIT = 420

DOWNLOADING = "Downloading. This does not replace the running program."


class UpdateBanner(QWidget):
    """One strip above the page area.

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
                 opener=None):
        super().__init__(parent)
        self.services = services
        self.theme = services.theme
        self.settings = services.settings
        # Injected in tests. The defaults are resolved inside update.py, so
        # nothing here holds a reference to urllib at all.
        self._fetcher = fetcher
        self._downloader = downloader
        self._opener = opener or self._open_url
        self.release = None
        self._task = None
        self._dismissed = False

        self.setObjectName("updateBanner")
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Maximum)

        column = QVBoxLayout(self)
        column.setContentsMargins(16, 12, 16, 12)
        column.setSpacing(6)

        self.heading = QLabel("")
        heading_font = QFont(self.font())
        heading_font.setWeight(QFont.Weight.DemiBold)
        self.heading.setFont(heading_font)
        self.heading.setWordWrap(True)
        column.addWidget(self.heading)

        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.notes)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setVisible(False)
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.detail)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.download_button = QPushButton("Download to Desktop")
        self.download_button.clicked.connect(self.download)
        row.addWidget(self.download_button)

        self.notes_button = QPushButton("Release notes")
        self.notes_button.clicked.connect(self.open_release_page)
        row.addWidget(self.notes_button)

        self.folder_button = QPushButton("Open the folder")
        self.folder_button.clicked.connect(self.open_folder)
        self.folder_button.setVisible(False)
        row.addWidget(self.folder_button)

        row.addStretch(1)
        self.dismiss_button = QPushButton("Not now")
        self.dismiss_button.clicked.connect(self.dismiss)
        row.addWidget(self.dismiss_button)
        column.addLayout(row)

        self.theme.changed.connect(self._apply_theme)
        self._apply_theme()
        self.setVisible(False)

    # -- appearance

    def _apply_theme(self):
        colour = self.theme.colour
        # Tinted towards the accent rather than a colour of its own: news, not
        # a warning. A red or yellow strip at launch reads as something being
        # wrong with the console, which is exactly the wrong conclusion.
        fill = mix(colour("surface"), colour("accent"), 0.14)
        border = mix(colour("border"), colour("accent"), 0.5)
        self.setStyleSheet(
            f"#updateBanner {{ background: {fill}; "
            f"border: 1px solid {border}; border-radius: 10px; }}")
        self.heading.setStyleSheet(f"color: {colour('text')};")
        self.notes.setStyleSheet(f"color: {colour('text_dim')};")
        self.detail.setStyleSheet(f"color: {colour('text')};")

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
        task.finished.connect(self._checked)
        # Silence is the whole policy. failed only fires if update.check
        # raised, which it is written not to do, and even then nothing is said.
        task.failed.connect(lambda _message: self._checked(None))
        task.done.connect(self._task_done)
        return task

    def _task_done(self):
        self._task = None

    def _checked(self, release):
        if release is not None:
            self.show_release(release)
        self.checked.emit(release)

    def show_release(self, release):
        """Display a release. Public so the About screen can hand one over."""
        self.release = release
        self._dismissed = False
        self.heading.setText(release.summary())
        notes = release.notes or ""
        if len(notes) > NOTES_LIMIT:
            notes = notes[:NOTES_LIMIT].rstrip() + "..."
        self.notes.setText(notes or "No release notes were published.")
        self.notes.setVisible(bool(notes))
        self.detail.setVisible(False)
        self.detail.setText("")
        self.download_button.setEnabled(bool(release.asset_url))
        self.download_button.setVisible(True)
        self.notes_button.setVisible(bool(release.page_url))
        self.folder_button.setVisible(False)
        self.setVisible(True)
        self.shown.emit()

    # -- actions, none of which happen on their own

    def download(self):
        if self.release is None or self._task is not None:
            return None
        release = self.release
        downloader = self._downloader
        self.download_button.setEnabled(False)
        self.detail.setVisible(True)
        self.detail.setText(DOWNLOADING)

        def work(control):
            return update.download(release, fetcher=downloader)

        task = self.services.submit(work)
        self._task = task
        task.finished.connect(self._download_finished)
        task.failed.connect(self._download_failed)
        task.done.connect(self._task_done)
        return task

    def _download_finished(self, result):
        self.downloaded.emit(result)
        self.detail.setVisible(True)
        self.detail.setText(result.detail)
        self.folder_button.setVisible(bool(result.path))
        self._result = result
        # Offering the same button again after a success invites a second copy
        # on the Desktop and a question about which one is the real one.
        self.download_button.setVisible(False if result.ok else True)
        self.download_button.setEnabled(not result.ok)
        colour = self.theme.colour
        if result.verified is True:
            token = "ok"
        elif result.verified is False:
            token = "error"
        else:
            token = "warn"
        self.detail.setStyleSheet(f"color: {colour(token)};")

    def _download_failed(self, message):
        self.detail.setVisible(True)
        self.detail.setText(
            "The download did not finish. Fetch it from the release page in "
            "your browser instead.")
        self.download_button.setEnabled(True)

    def open_release_page(self):
        if self.release is not None and self.release.page_url:
            return self._opener(self.release.page_url)
        return False

    def open_folder(self):
        result = getattr(self, "_result", None)
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
