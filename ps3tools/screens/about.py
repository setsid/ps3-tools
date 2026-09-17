"""The About screen: what this is, what it talks to, and who wrote the rest.

The section that matters is the network one. The diagnostic half of this
program is sold on "it only reads", and the update check is the first outbound
call the program has ever made. A user who was told the program only reads
from their console deserves to find out about api.github.com here, in plain
words, rather than from a firewall prompt. So the statement is exhaustive and
it is not softened: everything that leaves this machine is listed, and the
switch that stops the one optional part of it is on this screen.

British English, no emoji, colours only through the theme. See
docs/screen-interface.md.
"""

import os
import sys
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy,
                               QTabWidget, QVBoxLayout, QWidget)

from ps3tools import (APP_NAME, FULL_NAME, PROJECT_URL, VENDOR, VERSION,
                      history, update)
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen
from ps3tools.shell.updatebanner import UpdateBanner

SUPPORT_ADDRESS = "setsid.research@proton.me"

#: The two patcher repositories and this one. The third is built from
#: update.REPOSITORY, so the link and the update check can never disagree.
REPOSITORIES = (
    ("Black Ops II PSN freeze fix",
     "https://github.com/setsid/bo2-ps3-psn-freeze-fix"),
    ("Modern Warfare 3 PSN fix",
     "https://github.com/setsid/mw3-ps3-psn-fix"),
    (f"{APP_NAME} (this program)",
     f"https://github.com/{update.REPOSITORY}"),
    # Not used by this program, but the same console and the same audience:
    # somebody here for a PSN fix is often the same person wondering what else
    # the machine will do.
    ("Linux on the PS3's internal drive",
     "https://github.com/setsid/ps3-linux-internal-ssd"),
)

# Written out in full rather than summarised as "we respect your privacy",
# which tells a reader nothing they can check. Each line is one thing that
# crosses the network and who it goes to.
NETWORK_LINES = (
    ("Reads from your console.",
     "The diagnostic asks webMAN on your PS3 for its settings, its logs and "
     "its file listings, over your own network. It reads. There is no code "
     "path in it that can write to the console."),
    ("Writes to one folder on your console, and only when patching.",
     "The patcher copies the game's binaries down, changes them on this PC "
     "and puts them back in that game's own folder. It touches nothing else "
     "on the console and nothing at all outside that folder."),
    ("Checks GitHub for a newer version, once a day.",
     "One request to api.github.com asking what the latest release is. It "
     "sends the name and version of this program and nothing else. Your "
     "games and everything read from the console stay on this machine. "
     "GitHub sees the address any web request would show it. "
     "If you ask for the update, the file is fetched "
     "from GitHub and saved to your Desktop; it is never run for you and it "
     "never replaces this program while it is running. The checkbox below "
     "turns all of it off."),
)

NOTHING_ELSE = (
    "Nothing else leaves this machine. There is no analytics, no error "
    "reporting and no account. The diagnostic report is written to a file on "
    "this PC and stays there until you send it to somebody yourself.")

CREDITS = (
    ("webMAN MOD",
     "The homebrew this program talks to. It is not bundled here and it is "
     "not by this project. Everything the diagnostic reads and everything "
     "the patcher writes goes through it."),
    ("scetool, by naehrwert",
     "This program no longer bundles it, and its keyset is the one this "
     "program reads. The SELF handling here was written against its output "
     "field for field, and it is the reason any of this was possible."),
    ("TrueAncestor SELF Resigner",
     "Its unfself is no longer needed: fake-signed binaries are read and "
     "written here. Working out what those files look like started with it."),
    ("patch-bo1.py, patch-bo2.py and patch-mw3.py",
     "The three patch scripts in tools/patchers, two of them from the "
     "standalone repositories listed above and the Black Ops one written "
     "for this program, shipped inside the exe so the patcher does not "
     "depend on anything being installed alongside it."),
    ("PySide6 and Qt",
     "The window, the widgets and the drawing. Used as a dynamically linked "
     "LGPL build, which is the condition that matters for passing this "
     "program on."),
    ("bjocampos",
     "Tested these fixes on his own console and reported back on what they "
     "did there. The Black Ops II patch took the shape it has because of "
     "those reports, and he has kept testing on real hardware since."),
    ("OpenResty",
     "Worked out that Demonware derives the XUID from the account ID "
     "rather than from the PSN online ID, which is the fault the Black Ops "
     "fix is built on. Also supplied the regional binaries the patcher was "
     "developed against, and tested the result, including the American disc "
     "release."),
)

# Covers this project's own code only, which is what the MIT header on each
# of the patch scripts says as well. The entries above it credit the work of others;
# this line is not a statement about their terms.
LICENCE_NOTE = (
    "PS3 Tools is MIT licensed, and so are the patch scripts it ships. "
    "The components credited above are other people's work and are credited "
    "here as such.")

#: Fixed wording. The same two paragraphs go in the README here and in the
#: READMEs of the two standalone patcher repositories, so they are quoted
#: rather than rephrased for the screen.
TRADEMARK_NOTICE = (
    "Not affiliated with or endorsed by Activision, Treyarch or Sony. All "
    "trademarks are the property of their respective owners.")

DISCLAIMER_NOTICE = (
    "Every reasonable step has been taken to make this software safe, but no "
    "guarantee is given. Use it at your own risk. The app backs up the files "
    "it modifies; keep your own backups as well.")

UPDATE_LABEL = "Check GitHub for a newer version once a day"
UPDATE_HINT = (
    "Turn this off and the program never contacts anything outside your own "
    "network. You would then need to look for new versions yourself.")

CHECKING = "Asking GitHub..."
UP_TO_DATE = "This is the newest version that was published."
NO_ANSWER = (
    "GitHub did not answer. That is usually no connection or a busy address, "
    "and it is nothing to worry about.")
DISABLED_NOTE = "Switch the check back on first."


def build_date():
    """A date to print beside the version.

    Taken from ps3tools.BUILD_DATE when the build stamps one, and otherwise
    from the file the program was loaded from, which under a onefile build is
    the exe the user is holding. Approximate by design: the point of the line
    is to tell two builds apart when somebody is being helped over a message,
    not to be an audit trail.
    """
    import ps3tools

    stamped = getattr(ps3tools, "BUILD_DATE", "")
    if stamped:
        return str(stamped)
    for path in (sys.executable if getattr(sys, "frozen", False) else "",
                 __file__):
        try:
            if path and os.path.isfile(path):
                return time.strftime("%d %B %Y",
                                     time.localtime(os.path.getmtime(path)))
        except OSError:
            continue
    return "unknown"


class LinkButton(QPushButton):
    """A link that is a button, so it is reachable by keyboard and by touch.

    The system browser and not an embedded one: this program has no business
    rendering a web page, and the browser the user already trusts is the only
    one they have chosen.
    """

    def __init__(self, text, url, theme, opener=None, parent=None):
        super().__init__(text, parent)
        self.url = url
        self._theme = theme
        self._opener = opener or (
            lambda target: QDesktopServices.openUrl(QUrl(target)))
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Open {url}")
        self.setAccessibleName(f"{text}, opens {url}")
        self.setSizePolicy(QSizePolicy.Policy.Maximum,
                           QSizePolicy.Policy.Fixed)
        self.clicked.connect(self.open)
        theme.changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self):
        self.setStyleSheet(
            f"text-align: left; border: none; padding: 2px 0; "
            f"color: {self._theme.colour('accent')};")

    def open(self):
        return self._opener(self.url)


@register
class AboutScreen(Screen):
    """What the program is, what it talks to, and the update setting."""

    key = "about"
    title = "About"
    blurb = "Version, what this program talks to, and who wrote the rest."
    tile = "AB"
    #: Last card. It is the one nobody came here for.
    order = 900

    def __init__(self, services, parent=None, fetcher=None, opener=None):
        super().__init__(services, parent)
        self.settings = services.settings
        self._fetcher = fetcher
        self._opener = opener
        self._task = None
        # Every label whose colour follows a theme token, so a theme change is
        # one loop rather than a line per label.
        self._headings = []
        self._bodies = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Four short pages rather than one long one. Everything here is worth
        # keeping and none of it is worth scrolling past to reach the rest:
        # what you are running, what changed, what it does over the network,
        # and who wrote the parts that are not this.
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs)

        self._page("This program", self._build_identity, self._build_update)
        self._page("What's new", self._build_history)
        self._page("Network", self._build_network)
        self._page("Credits", self._build_links, self._build_credits)

        self.theme.changed.connect(self._apply_theme)
        self._apply_theme()

    def _page(self, label, *builders):
        """One tab: a scrolling column that the builders write into.

        self.column is what _heading and _body add to, so it is pointed at
        this page for as long as the builders run and every one of them stays
        exactly as it was.
        """
        scroll = QScrollArea(self.tabs)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget(scroll)
        self.column = QVBoxLayout(page)
        self.column.setContentsMargins(24, 20, 24, 24)
        self.column.setSpacing(14)
        scroll.setWidget(page)
        for builder in builders:
            builder()
        self.column.addStretch(1)
        self.tabs.addTab(scroll, label)
        return scroll

    # -- construction

    def _heading(self, text):
        label = QLabel(text)
        font = QFont(self.font())
        font.setPointSizeF(self.font().pointSizeF() + 2.0)
        font.setWeight(QFont.Weight.DemiBold)
        label.setFont(font)
        label.setWordWrap(True)
        self.column.addWidget(label)
        self._headings.append(label)
        return label

    def _body(self, text, token="text_dim"):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.column.addWidget(label)
        self._bodies.append((label, token))
        return label

    def _build_identity(self):
        title = QLabel(FULL_NAME)
        font = QFont(self.font())
        font.setPointSizeF(self.font().pointSizeF() + 6.0)
        font.setWeight(QFont.Weight.DemiBold)
        title.setFont(font)
        self.column.addWidget(title)
        self._headings.append(title)

        self.version_label = QLabel(
            f"Version {VERSION}, built {build_date()}")
        self.version_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.column.addWidget(self.version_label)
        self._bodies.append((self.version_label, "text_dim"))

        self._body(
            "Tools for a CFW PS3 running webMAN MOD: a diagnostic that reads "
            "the console and explains what it found, the two Call of Duty "
            "PSN fixes that used to be separate programs, title updates "
            "fetched from Sony, a package installer, a save data backup and "
            "a game transfer that resumes.")

        row = QHBoxLayout()
        row.setSpacing(12)
        support = QLabel("Support:")
        row.addWidget(support)
        self._bodies.append((support, "text_dim"))
        self.support_link = LinkButton(SUPPORT_ADDRESS,
                                       f"mailto:{SUPPORT_ADDRESS}",
                                       self.theme, self._opener)
        row.addWidget(self.support_link)
        row.addStretch(1)
        self.column.addLayout(row)

    def _build_update(self):
        self._heading("Updates")
        self.update_box = QCheckBox(UPDATE_LABEL)
        self.update_box.setChecked(update.enabled(self.settings))
        self.update_box.toggled.connect(self._update_setting_changed)
        self.column.addWidget(self.update_box)
        self._body(UPDATE_HINT)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.check_button = QPushButton("Check now")
        self.check_button.clicked.connect(self.check_now)
        row.addWidget(self.check_button)
        self.check_status = QLabel("")
        self.check_status.setWordWrap(True)
        row.addWidget(self.check_status, 1)
        self._bodies.append((self.check_status, "text_dim"))
        self.column.addLayout(row)

        # The same widget the shell shows at launch, reused rather than a
        # second way of saying the same thing that could drift from it.
        # compact=False: this copy is not competing with a tool for the
        # window, so it may take a second line to say how a download went.
        self.banner = UpdateBanner(self.services, fetcher=self._fetcher,
                                   opener=self._opener, compact=False)
        self.column.addWidget(self.banner)

    def _build_history(self):
        """Every published release and what changed in it.

        The one being run is marked, because "what am I on and what is new"
        is the question this tab exists to answer without sending anybody to
        a web page.
        """
        self._heading("What's new")
        running = history.for_version(VERSION)
        if running is None:
            self._body(f"You are running {VERSION}, which is not one of the "
                       f"published releases below.")
        for release in history.releases():
            label = QLabel(f"{release.version}    {release.date}"
                           + ("    (this is the one you are running)"
                              if release is running else ""))
            font = QFont(self.font())
            font.setWeight(QFont.Weight.DemiBold)
            label.setFont(font)
            label.setWordWrap(True)
            self.column.addWidget(label)
            self._bodies.append(
                (label, "accent" if release is running else "text"))
            if release.summary:
                self._body(release.summary)
            for change in release.changes:
                bullet = QLabel(f"\u2022  {change}")
                bullet.setWordWrap(True)
                bullet.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse)
                bullet.setContentsMargins(10, 0, 0, 0)
                self.column.addWidget(bullet)
                self._bodies.append((bullet, "text_dim"))

    def _build_network(self):
        self._heading("What this program does over the network")
        for lead, detail in NETWORK_LINES:
            label = QLabel(lead)
            font = QFont(self.font())
            font.setWeight(QFont.Weight.DemiBold)
            label.setFont(font)
            label.setWordWrap(True)
            self.column.addWidget(label)
            self._bodies.append((label, "text"))
            self._body(detail)
        self.nothing_else_label = self._body(NOTHING_ELSE, "text")

    def _build_links(self):
        self._heading("Where the code is")
        for name, url in REPOSITORIES:
            self.column.addWidget(
                LinkButton(f"{name} - {url}", url, self.theme, self._opener))
        self.column.addWidget(
            LinkButton(f"Everything else by {VENDOR} - {PROJECT_URL}",
                       PROJECT_URL, self.theme, self._opener))

    def _build_credits(self):
        self._heading("Credits")
        for name, detail in CREDITS:
            label = QLabel(name)
            font = QFont(self.font())
            font.setWeight(QFont.Weight.DemiBold)
            label.setFont(font)
            label.setWordWrap(True)
            self.column.addWidget(label)
            self._bodies.append((label, "text"))
            self._body(detail)
        self.licence_label = self._body(LICENCE_NOTE, "text_dim")
        self._build_legal()

    def _build_legal(self):
        """The trademark line and the disclaimer, under the credits."""
        self._heading("Legal")
        self.trademark_label = self._body(TRADEMARK_NOTICE, "text_dim")
        self.disclaimer_label = self._body(DISCLAIMER_NOTICE, "text_dim")

    # -- theme

    def _apply_theme(self):
        colour = self.theme.colour
        for label in self._headings:
            label.setStyleSheet(f"color: {colour('text')};")
        for label, token in self._bodies:
            label.setStyleSheet(f"color: {colour(token)};")

    # -- the update setting

    def _update_setting_changed(self, checked):
        update.set_enabled(self.settings, checked)
        if checked:
            # Turning it back on should mean a check happens, not that the
            # cache from before it was switched off keeps it quiet for a day.
            update.clear_cache(self.settings)
            self.check_status.setText("")
        else:
            self.banner.setVisible(False)
            self.check_status.setText("")

    def check_now(self):
        """Ask GitHub, off the GUI thread, because a button was pressed.

        Forced past the daily cache: somebody pressing this has a reason to
        think there is something new, and telling them yesterday's answer
        would not be an answer.
        """
        if self._task is not None:
            return None
        if not update.enabled(self.settings):
            self.check_status.setText(DISABLED_NOTE)
            return None
        self.check_button.setEnabled(False)
        self.check_status.setText(CHECKING)
        task = self.banner.start_check(force=True)
        if task is None:
            self.check_button.setEnabled(True)
            self.check_status.setText("")
            return None
        self._task = task
        self.banner.checked.connect(self._check_finished)
        task.done.connect(self._check_done)
        return task

    def _check_done(self):
        self._task = None
        self.check_button.setEnabled(True)

    def _check_finished(self, release):
        try:
            self.banner.checked.disconnect(self._check_finished)
        except (RuntimeError, TypeError):
            pass
        if release is None:
            # Told apart here and nowhere else. The launch check stays silent
            # about a failure because nobody asked it anything, but somebody
            # who pressed a button is owed the difference between "there is
            # nothing newer" and "nobody answered".
            self.check_status.setText(
                UP_TO_DATE if self._had_answer() else NO_ANSWER)
        else:
            self.check_status.setText("")

    def _had_answer(self):
        """Whether the last check got a release out of GitHub at all."""
        cache = self.settings.get(update.SETTING_CACHE)
        return bool(isinstance(cache, dict) and cache.get("release"))

    # -- lifecycle

    def on_leave(self):
        if self._task is not None:
            self._task.cancel()
