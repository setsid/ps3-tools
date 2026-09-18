"""The About screen and the update banner, built for real and driven.

Both are built with an injected fetcher, every time, and setUp makes the real
urllib opener raise. Nothing in this file can make a request: the screen that
carries the promise about what leaves the machine is not going to be the one
that quietly breaks it in the test suite.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import pathlib
import tempfile
import time
import unittest
import urllib.request

from PySide6.QtWidgets import QApplication

from support import ROOT  # noqa: F401  (puts the project on sys.path)
from test_update import exe_asset, failing_fetcher, json_fetcher, \
    release_payload

from ps3tools import VERSION, update
from ps3tools.shell.registry import screen_for
from ps3tools.shell.screen import ConnectionState, Services, Theme, \
    THEME_TOKENS
from ps3tools.shell.updatebanner import NO_ASSET, UpdateBanner, first_line
from ps3tools.screens import about

# One QApplication for the module: a second one aborts the process.
APP = QApplication.instance() or QApplication([])

COLOURS = {token: "#%06x" % (0x010203 + index * 0x111111)
           for index, token in enumerate(THEME_TOKENS)}

DIGEST_A = "a" * 64


class StubTheme(Theme):
    def colour(self, token):
        return COLOURS[token]

    @property
    def dark(self):
        return False


class AboutCase(unittest.TestCase):
    def setUp(self):
        def refuse(*args, **kwargs):
            raise AssertionError(
                "the About screen reached the real urllib opener")

        self._saved = urllib.request.urlopen
        urllib.request.urlopen = refuse
        self.addCleanup(self._restore)

        self.opened = []
        self.settings = {}
        self.connection = ConnectionState()
        self.theme = StubTheme()
        self.services = Services(self.connection, self.theme, self.settings)
        self.screens = []
        self.addCleanup(self._teardown)

    def _restore(self):
        urllib.request.urlopen = self._saved

    def _teardown(self):
        self.services.wait(30000)
        for screen in self.screens:
            screen.deleteLater()
        self.screens = []
        APP.processEvents()

    def build(self, fetcher=None):
        screen = about.AboutScreen(self.services, fetcher=fetcher,
                                   opener=self.opened.append)
        self.screens.append(screen)
        return screen

    def banner(self, fetcher=None, downloader=None, compact=True):
        widget = UpdateBanner(self.services, fetcher=fetcher,
                              downloader=downloader,
                              opener=self.opened.append, compact=compact)
        self.screens.append(widget)
        return widget

    def pump(self, milliseconds=30000):
        """Wait for the worker AND for its result to reach the GUI thread.

        Each Services has its own pool, so "idle" is about this screen's own
        work, but idle can still be true before a task has been picked up at
        all. The task a screen holds is
        cleared by a queued signal, and a second press made before that
        arrives is quietly ignored, which is how pressing Check now twice
        counted as one request in a full run and as two on its own.
        """
        deadline = time.monotonic() + milliseconds / 1000.0
        quiet = 0
        while time.monotonic() < deadline:
            APP.processEvents()
            idle = self.services.wait(50)
            if self.services.running_tasks():
                quiet = 0
                continue
            quiet = quiet + 1 if idle else 0
            if quiet >= 3:
                for _ in range(5):
                    APP.processEvents()
                return
        raise AssertionError("work did not settle")

    def on_show(self, widget):
        """Whether the widget has been shown, without needing a real window.

        isVisible() is False for every child of a screen that has not been
        put on screen, which is every screen in this file.
        """
        return not widget.isHidden()

    def text_of(self, screen):
        """Every word the screen puts on itself, as one string."""
        from PySide6.QtWidgets import QLabel, QCheckBox, QPushButton
        parts = []
        for kind in (QLabel, QCheckBox, QPushButton):
            for widget in screen.findChildren(kind):
                parts.append(widget.text())
        return "\n".join(parts)


# --- registration ------------------------------------------------------------

class RegistrationTests(AboutCase):
    def test_registered_under_its_key(self):
        self.assertIs(screen_for("about"), about.AboutScreen)

    def test_sits_last_on_the_home_screen(self):
        from ps3tools.shell.registry import screens
        self.assertEqual(screens()[-1].key, "about")

    def test_tile_is_letters_and_no_emoji(self):
        self.assertTrue(about.AboutScreen.tile.isalpha())
        self.assertLessEqual(len(about.AboutScreen.tile), 3)
        self.assertTrue(about.AboutScreen.blurb.endswith("."))


# --- what it says ------------------------------------------------------------

class ContentTests(AboutCase):
    def test_it_builds(self):
        from PySide6.QtWidgets import QLabel
        screen = self.build()
        self.assertTrue(screen.findChildren(QLabel))

    def test_shows_the_version_and_a_build_date(self):
        screen = self.build()
        text = screen.version_label.text()
        self.assertIn(VERSION, text)
        self.assertIn("built", text)
        self.assertNotIn("built unknown", text)

    def test_shows_the_support_address(self):
        screen = self.build()
        self.assertIn(about.SUPPORT_ADDRESS, self.text_of(screen))
        self.assertEqual(screen.support_link.url,
                         f"mailto:{about.SUPPORT_ADDRESS}")

    def test_states_what_goes_over_the_network(self):
        text = self.text_of(self.build())
        # Reads from the console, writes only to the game folder, checks
        # GitHub. Named explicitly, because the update check is the first
        # outbound call in a program sold on "it only reads".
        self.assertIn("api.github.com", text)
        self.assertIn("It reads.", text)
        self.assertIn("game's own folder", text)
        self.assertIn("Nothing else leaves this machine", text)
        self.assertIn("no analytics", text)

    def test_does_not_overclaim_about_the_update_check(self):
        text = self.text_of(self.build())
        self.assertIn("never replaces this program while it is running", text)
        self.assertIn("never run for you", text)

    def test_links_to_the_three_repositories_and_the_account(self):
        screen = self.build()
        urls = [widget.url for widget in screen.findChildren(about.LinkButton)]
        self.assertIn("https://github.com/setsid/bo2-ps3-psn-freeze-fix", urls)
        self.assertIn("https://github.com/setsid/mw3-ps3-psn-fix", urls)
        self.assertIn(f"https://github.com/{update.REPOSITORY}", urls)
        self.assertIn("https://github.com/setsid", urls)

    def test_links_open_through_the_injected_opener(self):
        screen = self.build()
        links = [widget for widget in screen.findChildren(about.LinkButton)
                 if widget.url == "https://github.com/setsid"]
        links[0].open()
        self.assertEqual(self.opened, ["https://github.com/setsid"])

    def test_credits_name_what_is_bundled(self):
        text = self.text_of(self.build())
        for name in ("webMAN MOD", "keysmith", "naehrwert", "PySide6",
                     "patch-bo1.py", "patch-bo2.py", "patch-mw3.py"):
            self.assertIn(name, text)

    def test_the_patcher_credit_names_every_script_that_ships(self):
        """The entry has to keep pace with tools/patchers.

        It said "patch-bo2.py and patch-mw3.py" for as long as there were two
        of them, and stayed that way after the Black Ops script arrived. A
        credit for what is inside the exe is worth nothing if it does not list
        everything that is inside the exe, so the names are checked against
        the directory rather than against a list written out here.
        """
        entry = [detail for name, detail in about.CREDITS
                 if "patch-bo1.py" in name][0]
        scripts = sorted(path.name for path in
                         pathlib.Path(ROOT, "tools", "patchers").glob("*.py"))
        self.assertEqual(scripts,
                         ["patch-bo1.py", "patch-bo2.py", "patch-mw3.py"])
        name = [name for name, _detail in about.CREDITS
                if "patch-bo1.py" in name][0]
        for script in scripts:
            self.assertIn(script, name)
        self.assertIn("three", entry)

    def test_the_people_who_found_the_faults_are_credited(self):
        """Two of the three fixes exist because somebody outside this project
        did the work, and the screen is where that is said.

        bjocampos found that the Black Ops II patch was leaving a third of the
        game unpatched, and OpenResty found what Demonware actually hashes,
        which is the whole of the Black Ops fix. A refactor of this tuple that
        drops either name is a regression whatever else it improves.
        """
        names = [name for name, _detail in about.CREDITS]
        self.assertIn("bjocampos", names)
        self.assertIn("OpenResty", names)
        text = self.text_of(self.build())
        self.assertIn("bjocampos", text)
        self.assertIn("OpenResty", text)

    def test_it_states_its_own_licence_and_only_credits_the_rest(self):
        # The MIT claim is about this project's code. The others are credited,
        # and the screen must not put words in their authors' mouths.
        text = self.text_of(self.build())
        self.assertIn("MIT licensed", text)
        for name in ("webMAN MOD", "naehrwert", "PySide6"):
            self.assertIn(name, text)
        for claim in ("the keyset is MIT", "webMAN MOD is MIT",
                      "GPL compliant", "fully compliant"):
            self.assertNotIn(claim, text)

    def test_no_emoji_anywhere(self):
        for character in self.text_of(self.build()):
            self.assertLess(ord(character), 0x2190, repr(character))


# --- the setting -------------------------------------------------------------

class TheUpdatesSection(AboutCase):
    """One line and a button. There used to be a tick box.

    Once the check at start-up was made to run whatever the setting said, the
    box decided almost nothing, and the paragraph explaining that was longer
    than the thing it explained. The line that replaced it says when the
    program looks and how to make it look now.
    """

    def test_it_says_the_check_happens_at_start_up(self):
        from ps3tools.screens.about import UPDATE_HINT
        self.assertIn("when it starts", UPDATE_HINT)

    def test_it_points_at_the_button(self):
        from ps3tools.screens.about import UPDATE_HINT
        self.assertIn("below", UPDATE_HINT)

    def test_there_is_no_tick_box_any_more(self):
        self.assertFalse(hasattr(self.build(), "update_box"))

    def test_the_button_is_still_there(self):
        self.assertTrue(self.build().check_button.isEnabled())

    def test_it_no_longer_claims_nothing_leaves_your_network(self):
        """It said turning the box off meant nothing left your network, and
        that stopped being true when the start-up check was made
        unconditional. It is a claim people rely on."""
        from ps3tools.screens.about import UPDATE_HINT
        self.assertNotIn("never contacts", UPDATE_HINT)
        self.assertNotIn("once a day", UPDATE_HINT)

class CheckNowTests(AboutCase):
    def test_a_newer_version_shows_the_banner(self):
        screen = self.build(fetcher=json_fetcher(release_payload(
            "v9.9", notes="Fixes a thing.", assets=[exe_asset()])))
        screen.check_now()
        self.pump()
        self.assertTrue(self.on_show(screen.banner))
        self.assertIn("9.9", screen.banner.message_text())
        self.assertIn("Fixes a thing.", screen.banner.message_text())

    def test_up_to_date_says_so_and_shows_no_banner(self):
        screen = self.build(fetcher=json_fetcher(release_payload(VERSION)))
        screen.check_now()
        self.pump()
        self.assertFalse(self.on_show(screen.banner))
        self.assertEqual(screen.check_status.text(), about.UP_TO_DATE)

    def test_no_answer_is_said_gently_and_never_as_an_error(self):
        screen = self.build(fetcher=failing_fetcher(OSError("unreachable")))
        screen.check_now()
        self.pump()
        self.assertFalse(self.on_show(screen.banner))
        self.assertEqual(screen.check_status.text(), about.NO_ANSWER)

    def test_malformed_json_behaves_like_no_answer(self):
        def fetch(url, timeout=None):
            return b"{not json"
        screen = self.build(fetcher=fetch)
        screen.check_now()
        self.pump()
        self.assertEqual(screen.check_status.text(), about.NO_ANSWER)

    def test_check_now_ignores_the_daily_cache(self):
        seen = []
        screen = self.build(fetcher=json_fetcher(release_payload("v9.9"),
                                                 seen))
        screen.check_now()
        self.pump()
        screen.check_now()
        self.pump()
        self.assertEqual(len(seen), 2)

    def test_the_button_comes_back_afterwards(self):
        screen = self.build(fetcher=json_fetcher(release_payload(VERSION)))
        screen.check_now()
        self.pump()
        self.assertTrue(screen.check_button.isEnabled())


# --- the banner --------------------------------------------------------------

class BannerTests(AboutCase):
    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.folder = holder.name
        self.payload = b"MZ not really an exe"
        self.digest = update.digest_bytes(self.payload)

    def bytes_fetcher(self, data=None):
        def fetch(url, timeout=None):
            return self.payload if data is None else data
        return fetch

    def release(self, notes=""):
        return update.Release(release_payload(
            "v9.9", notes=notes, assets=[exe_asset()]))

    def test_starts_hidden(self):
        self.assertFalse(self.on_show(self.banner()))

    def test_stays_hidden_when_there_is_nothing_to_say(self):
        widget = self.banner(fetcher=json_fetcher(release_payload(VERSION)))
        widget.start_check()
        self.pump()
        self.assertFalse(self.on_show(widget))

    def test_stays_hidden_when_the_check_fails(self):
        widget = self.banner(fetcher=failing_fetcher(OSError("gone")))
        widget.start_check()
        self.pump()
        self.assertFalse(self.on_show(widget))

    def test_appears_for_a_newer_version(self):
        widget = self.banner(fetcher=json_fetcher(release_payload(
            "v9.9", notes="New things.", assets=[exe_asset()])))
        seen = []
        widget.checked.connect(seen.append)
        widget.start_check()
        self.pump()
        self.assertTrue(self.on_show(widget))
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])

    def test_never_checks_while_the_setting_is_off(self):
        self.settings[update.SETTING_ENABLED] = False
        called = []
        widget = self.banner(fetcher=json_fetcher(
            release_payload("v9.9"), called))
        widget.start_check()
        self.pump()
        self.assertEqual(called, [])
        self.assertFalse(self.on_show(widget))

    def test_dismissing_hides_it_and_stops_it_returning(self):
        widget = self.banner(fetcher=json_fetcher(release_payload(
            "v9.9", assets=[exe_asset()])))
        widget.start_check()
        self.pump()
        fired = []
        widget.dismissed.connect(lambda: fired.append(True))
        widget.dismiss()
        self.assertFalse(self.on_show(widget))
        self.assertEqual(fired, [True])
        self.assertIsNone(widget.start_check())

    def test_nothing_downloads_until_the_button_is_pressed(self):
        asked = []

        def downloader(url, timeout=None):
            asked.append(url)
            return self.payload

        widget = self.banner(fetcher=json_fetcher(release_payload(
            "v9.9", assets=[exe_asset()])), downloader=downloader)
        widget.start_check()
        self.pump()
        self.assertEqual(asked, [])

    def test_a_verified_download_says_it_is_verified(self):
        widget = self.banner(downloader=self.bytes_fetcher())
        widget.show_release(self.release(f"sha256 {self.digest}"))
        with _desktop(self.folder):
            widget.download()
            self.pump()
        self.assertIn("matches the checksum", widget.status_text())
        self.assertTrue(self.on_show(widget.folder_button))
        self.assertTrue(os.listdir(self.folder))

    def test_a_mismatched_download_is_refused_and_not_kept(self):
        widget = self.banner(downloader=self.bytes_fetcher())
        widget.show_release(self.release("sha256 " + "c" * 64))
        with _desktop(self.folder):
            widget.download()
            self.pump()
        self.assertIn("does not match", widget.status_text())
        self.assertEqual(os.listdir(self.folder), [])

    def test_notes_with_no_hash_are_not_called_verified(self):
        widget = self.banner(downloader=self.bytes_fetcher())
        widget.show_release(self.release("Just some notes."))
        with _desktop(self.folder):
            widget.download()
            self.pump()
        self.assertIn("not been verified", widget.status_text())
        self.assertNotIn("matches the checksum", widget.status_text())

    def test_the_release_page_opens_through_the_injected_opener(self):
        widget = self.banner()
        release = self.release()
        widget.show_release(release)
        widget.open_release_page()
        self.assertEqual(self.opened, [release.page_url])



# --- the banner as a notification bar ----------------------------------------

LONG_NOTE = ("A single line of release notes that runs on well past the width "
             "of any window this program will ever be opened in, and then "
             "keeps running on for a good while after that as well.")

MULTI_NOTE = ("Headline change.\n\n## What changed\n\n"
              "* A bullet about a thing\n* A bullet about another thing\n\n"
              "A closing paragraph that has no business being in a banner.")


class MutableTheme(StubTheme):
    """A theme whose accent can be changed, so the repaint can be watched."""

    def __init__(self):
        super().__init__()
        self._colours = dict(COLOURS)

    def colour(self, token):
        return self._colours[token]

    def recolour(self, token, value):
        self._colours[token] = value
        self.changed.emit()


class BannerActionTests(AboutCase):
    """The download button, on both of the paths a release can arrive by."""

    def newer(self, notes="Fixes a thing.", assets=None):
        return release_payload(
            "v9.9", notes=notes,
            assets=[exe_asset()] if assets is None else assets)

    def test_the_automatic_check_enables_the_download_button(self):
        widget = self.banner(fetcher=json_fetcher(self.newer()))
        widget.start_check()
        self.pump()
        self.assertTrue(self.on_show(widget))
        self.assertTrue(self.on_show(widget.download_button))
        self.assertTrue(widget.download_button.isEnabled())

    def test_the_forced_check_enables_it_the_same_way(self):
        widget = self.banner(fetcher=json_fetcher(self.newer()))
        widget.start_check(force=True)
        self.pump()
        self.assertTrue(widget.download_button.isEnabled())

    def test_a_release_out_of_the_daily_cache_is_still_downloadable(self):
        """The launch check answers from the cache; the button must not care."""
        first = self.banner(fetcher=json_fetcher(self.newer()))
        first.start_check()
        self.pump()
        self.assertIn(update.SETTING_CACHE, self.settings)

        def refuse(url, timeout=None):
            raise AssertionError("the cached check went to the network")

        second = self.banner(fetcher=refuse)
        second.start_check()
        self.pump()
        self.assertTrue(self.on_show(second))
        self.assertTrue(second.download_button.isEnabled())

    def test_the_banner_paints_its_own_background(self):
        """A plain QWidget ignores a stylesheet background without this, which
        is what left the strip colourless and its buttons looking like page."""
        from PySide6.QtCore import Qt as _Qt
        widget = self.banner()
        self.assertTrue(
            widget.testAttribute(_Qt.WidgetAttribute.WA_StyledBackground))

    def test_no_windows_asset_disables_the_button_and_says_why(self):
        widget = self.banner(fetcher=json_fetcher(self.newer(assets=[])))
        widget.start_check()
        self.pump()
        self.assertTrue(self.on_show(widget))
        self.assertFalse(widget.download_button.isEnabled())
        self.assertIn(NO_ASSET, widget.download_button.toolTip())
        self.assertIn("no Windows download", widget.message_text())

    def test_a_dead_button_never_starts_a_download(self):
        asked = []

        def downloader(url, timeout=None):
            asked.append(url)
            return b""

        widget = self.banner(fetcher=json_fetcher(self.newer(assets=[])),
                             downloader=downloader)
        widget.start_check()
        self.pump()
        self.assertIsNone(widget.download())
        self.pump()
        self.assertEqual(asked, [])

    def test_the_release_page_link_survives_a_missing_asset(self):
        widget = self.banner(fetcher=json_fetcher(self.newer(assets=[])))
        widget.start_check()
        self.pump()
        self.assertTrue(self.on_show(widget.page_button))

    def test_the_button_is_disabled_while_a_download_is_running(self):
        widget = self.banner(downloader=lambda url, timeout=None: b"MZ")
        widget.show_release(update.Release(self.newer()))
        self.assertTrue(widget.download_button.isEnabled())
        with _desktop(tempfile.mkdtemp()):
            widget.download()
            self.assertFalse(widget.download_button.isEnabled())
            self.pump()

    def test_a_failed_download_hands_the_button_back(self):
        def boom(url, timeout=None):
            raise OSError("no")

        widget = self.banner(downloader=boom)
        widget.show_release(update.Release(self.newer()))
        with _desktop(tempfile.mkdtemp()):
            widget.download()
            self.pump()
        self.assertTrue(widget.download_button.isEnabled())
        self.assertIn("did not finish", widget.status_text())


class BannerShapeTests(AboutCase):
    """One line high, whatever the release notes look like."""

    def shown(self, notes, compact=True, width=520):
        widget = self.banner(compact=compact)
        widget.resize(width, widget.sizeHint().height())
        widget.show_release(update.Release(release_payload(
            "v9.9", notes=notes, assets=[exe_asset()])))
        widget.show()
        APP.processEvents()
        return widget

    def test_a_multi_paragraph_body_is_reduced_to_its_first_line(self):
        widget = self.shown(MULTI_NOTE)
        self.assertIn("Headline change.", widget.message_text())
        self.assertNotIn("bullet", widget.message_text())
        self.assertNotIn("closing paragraph", widget.message_text())
        self.assertNotIn("\n", widget.message_text())

    def test_markdown_furniture_is_not_read_out(self):
        self.assertEqual(first_line("## What changed\n\nA real line."),
                         "What changed")
        self.assertEqual(first_line("\n\n* A bullet"), "A bullet")
        self.assertEqual(first_line(""), "")

    def test_a_long_note_is_elided_rather_than_wrapped(self):
        widget = self.shown(LONG_NOTE)
        text = widget.message.text()
        self.assertTrue(text.endswith("\u2026"), text)
        self.assertNotIn(LONG_NOTE, text)
        self.assertIn(LONG_NOTE, widget.message_text())

    def test_the_height_does_not_grow_with_the_notes(self):
        short = self.shown("Fixes a thing.").sizeHint().height()
        for notes in (LONG_NOTE, MULTI_NOTE, "x" * 6000):
            self.assertEqual(self.shown(notes).sizeHint().height(), short)

    def test_it_stays_about_a_toolbar_row_high(self):
        widget = self.shown(MULTI_NOTE)
        self.assertLess(widget.sizeHint().height(), 56)

    def test_widening_it_shows_more_of_the_line(self):
        narrow = self.shown(LONG_NOTE, width=380)
        wide = self.shown(LONG_NOTE, width=1400)
        self.assertGreater(len(wide.message.text()),
                           len(narrow.message.text()))

    def test_the_about_copy_is_no_taller_when_it_is_only_showing_news(self):
        self.assertEqual(self.shown(MULTI_NOTE, compact=False).height(),
                         self.shown(MULTI_NOTE).height())


class BannerColourTests(AboutCase):
    def test_the_bar_is_painted_from_theme_tokens(self):
        theme = MutableTheme()
        services = Services(self.connection, theme, self.settings)
        widget = UpdateBanner(services, opener=self.opened.append)
        self.screens.append(widget)
        self.assertIn(theme.colour("accent"), widget.styleSheet())
        self.assertIn(theme.colour("accent_text"), widget.styleSheet())

    def test_it_repaints_when_the_theme_changes(self):
        theme = MutableTheme()
        services = Services(self.connection, theme, self.settings)
        widget = UpdateBanner(services, opener=self.opened.append)
        self.screens.append(widget)
        theme.recolour("accent", "#123456")
        self.assertIn("#123456", widget.styleSheet())

    def test_no_colour_is_written_into_the_widget_by_hand(self):
        """Every colour on the bar has to come from a token, so both palettes
        stay legible without this file being reviewed twice."""
        import re
        theme = MutableTheme()
        services = Services(self.connection, theme, self.settings)
        widget = UpdateBanner(services, opener=self.opened.append)
        self.screens.append(widget)
        source = open(os.path.join(
            ROOT, "ps3tools", "shell", "updatebanner.py"), encoding="utf-8")
        with source as handle:
            self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}\b", handle.read()),
                             [])


class MainWindowBannerTests(AboutCase):
    def test_building_a_window_asks_nobody_anything(self):
        from ps3tools.shell import app as shell_app
        from ps3tools.shell.theme import AppTheme
        services = Services(self.connection, AppTheme("light"), self.settings)
        window = shell_app.MainWindow(services)
        self.addCleanup(window.deleteLater)
        APP.processEvents()
        # setUp has already made the real opener raise; nothing above may have
        # reached it, and no check may be in flight either.
        self.assertIsNone(window.update_banner._task)
        self.assertFalse(window.update_banner.isVisibleTo(window))
        self.assertNotIn(update.SETTING_CACHE, self.settings)

    def test_the_window_copy_is_the_compact_one(self):
        from ps3tools.shell import app as shell_app
        from ps3tools.shell.theme import AppTheme
        services = Services(self.connection, AppTheme("light"), self.settings)
        window = shell_app.MainWindow(services)
        self.addCleanup(window.deleteLater)
        self.assertTrue(window.update_banner.compact)

    def test_the_about_copy_is_the_roomy_one(self):
        screen = self.build()
        self.assertFalse(screen.banner.compact)


class _desktop:
    """Points update.target_folder at a temporary directory for one block.

    The download's default destination is the real Desktop, and a test suite
    has no business putting files on somebody's Desktop.
    """

    def __init__(self, folder):
        self.folder = folder

    def __enter__(self):
        self._saved = update.target_folder
        update.target_folder = lambda: self.folder
        return self

    def __exit__(self, *exc):
        update.target_folder = self._saved
        return False



class TheVersionHistory(unittest.TestCase):
    """What changed in each release, without going to a web page.

    The list is kept in the program rather than fetched: the answer for a
    build is fixed at the moment it is built, and somebody with no internet
    still needs to know what they are running.
    """

    def test_every_release_has_something_to_say(self):
        from ps3tools import history
        self.assertTrue(history.releases())
        for release in history.releases():
            with self.subTest(release.version):
                self.assertTrue(release.version)
                self.assertTrue(release.date)
                self.assertTrue(release.summary or release.changes)

    def test_newest_first(self):
        from ps3tools import history
        dates = [release.date for release in history.releases()]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_the_version_being_run_is_one_of_them(self):
        # A build whose version is not in the list is not an error, but the
        # released one always should be: it is how somebody checks what they
        # have against what changed.
        from ps3tools import history
        from ps3tools import VERSION
        self.assertIsNotNone(
            history.for_version(VERSION),
            f"{VERSION} has no entry in ps3tools/history.py")

    def test_a_v_prefix_and_whitespace_are_tolerated(self):
        from ps3tools import history
        newest = history.releases()[0].version
        self.assertIsNotNone(history.for_version(f" v{newest} "))
        self.assertIsNone(history.for_version("0.0.0-nothing"))

    def test_no_release_repeats_the_install_boilerplate(self):
        # The sha256 line and "restart the console after patching" belong on
        # the release page and in the README. Once a release, forever, is how
        # this turns back into the wall of text it replaced.
        from ps3tools import history
        for release in history.releases():
            words = (release.summary,) + tuple(release.changes)
            said = " ".join(words).lower()
            for boilerplate in ("sha256", "certutil", "windows will warn",
                                "restart the console"):
                self.assertNotIn(boilerplate, said, release.version)


class TheAboutTabs(AboutCase):
    """Four short pages rather than one long scroll."""

    def test_there_is_a_tab_for_each_thing_it_has_to_say(self):
        screen = self.build()
        labels = [screen.tabs.tabText(index)
                  for index in range(screen.tabs.count())]
        self.assertEqual(labels,
                         ["This program", "What's new", "Network", "Credits"])

    def test_the_history_tab_lists_every_release(self):
        from ps3tools import history
        screen = self.build()
        said = self._text_of(screen.tabs.widget(1))
        for release in history.releases():
            self.assertIn(release.version, said)

    def test_the_running_version_is_marked(self):
        from ps3tools import VERSION
        screen = self.build()
        said = self._text_of(screen.tabs.widget(1))
        self.assertIn("this is the one you are running", said)
        self.assertIn(VERSION, said)

    def test_the_update_check_is_still_where_it_was(self):
        # Rearranged, not removed: the button and the banner are the same two
        # widgets they were, on the first tab.
        screen = self.build()
        self.assertIsNotNone(screen.check_button)
        self.assertIsNotNone(screen.banner)
        said = self._text_of(screen.tabs.widget(0))
        self.assertIn("Version", said)

    def _text_of(self, widget):
        from PySide6.QtWidgets import QLabel
        return " ".join(label.text() for label in widget.findChildren(QLabel))



class TheLegalNotices(unittest.TestCase):
    """Fixed wording, in the app and in the README, word for word.

    The same two paragraphs go in the two standalone patcher repositories, so
    the text is quoted rather than rewritten to suit each place it appears.
    """

    TRADEMARK = ("Not affiliated with or endorsed by Activision, Treyarch or "
                 "Sony. All trademarks are the property of their respective "
                 "owners.")
    DISCLAIMER = ("Every reasonable step has been taken to make this software "
                  "safe, but no guarantee is given. Use it at your own risk. "
                  "The app backs up the files it modifies; keep your own "
                  "backups as well.")

    def test_the_wording_in_the_app_is_the_wording_that_was_agreed(self):
        self.assertEqual(about.TRADEMARK_NOTICE, self.TRADEMARK)
        self.assertEqual(about.DISCLAIMER_NOTICE, self.DISCLAIMER)

    def test_the_readme_carries_the_same_words(self):
        readme = " ".join(
            pathlib.Path(ROOT, "README.md").read_text(encoding="utf-8").split())
        self.assertIn("## Legal",
                      pathlib.Path(ROOT, "README.md").read_text(encoding="utf-8"))
        self.assertIn(self.TRADEMARK, readme)
        self.assertIn(self.DISCLAIMER, readme)

    def test_the_trademark_line_comes_first(self):
        readme = " ".join(
            pathlib.Path(ROOT, "README.md").read_text(encoding="utf-8").split())
        self.assertLess(readme.index(self.TRADEMARK),
                        readme.index(self.DISCLAIMER))

    def readme_images(self):
        import re
        readme = pathlib.Path(ROOT, "README.md").read_text(encoding="utf-8")
        images = set(re.findall(r'<img[^>]*src="([^"]+)"', readme))
        images |= set(re.findall(r'!\[[^\]]*\]\(([^)]+)\)', readme))
        return {name for name in images if not name.startswith("http")}

    def test_nothing_in_the_readme_points_at_box_art_or_a_screenshot(self):
        # No logos, box art or in-game screenshots. Everything shown is drawn
        # by this project: the wordmark, and the status cards at the top,
        # which docs/make-cards.py draws out of rectangles and letters. The
        # titles themselves are trademarks and their artwork is not ours to
        # ship, so an image arriving here that neither this project drew nor
        # can account for is a failure whatever it turns out to be.
        allowed = {"logo.png"}
        allowed |= {"docs/cards/" + name for name, _letters, _title, _status
                    in self.cards()}
        self.assertEqual(self.readme_images(), allowed)

    def test_every_picture_in_the_readme_is_one_this_project_drew(self):
        # The cards are checked as files rather than as names: an SVG that
        # embedded a photograph would pass a filename check and fail this.
        for name in sorted(self.readme_images()):
            if not name.endswith(".svg"):
                continue
            body = pathlib.Path(ROOT, name).read_text(encoding="utf-8")
            self.assertNotIn("<image", body, name)
            self.assertNotIn("data:", body, name)
            self.assertIn("<text", body, name)

    def cards(self):
        """What docs/make-cards.py says it draws."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "make_cards_legal",
            str(pathlib.Path(ROOT, "docs", "make-cards.py")))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.CARDS


class TheCreditsInTheReadme(unittest.TestCase):
    """The people credited on the screen are credited in the README as well.

    Somebody reading the repository and somebody running the exe should come
    away with the same list of who did what. The README carries the longer
    account of each contribution and the screen carries the short one, so this
    checks that the names are in both places rather than that the words match.
    """

    def readme(self):
        return pathlib.Path(ROOT, "README.md").read_text(encoding="utf-8")

    def test_the_readme_credits_the_people_the_screen_credits(self):
        body = self.readme()
        self.assertIn("## Credits", body)
        for name in ("bjocampos", "OpenResty"):
            self.assertIn(name, body)
            self.assertIn(name, [entry for entry, _detail in about.CREDITS])

    def test_the_readme_says_what_each_of_them_did(self):
        """A bare name is not a credit, so each one has to carry its story.

        The README entries run to a few sentences each and that is the point
        of them; a later tidy that cut them back to a list of names would pass
        the test above and lose what the section is for.
        """
        body = self.readme()
        section = body.split("## Credits", 1)[1].split("\n---", 1)[0]
        for name in ("bjocampos", "OpenResty"):
            entry = [block for block in section.split("\n\n")
                     if name in block][0]
            self.assertGreater(len(entry.split()), 40, name)
        self.assertIn("account ID", section)


class TheLegalNoticesOnScreen(AboutCase):
    def test_they_sit_under_the_credits(self):
        screen = self.build()
        credits_tab = screen.tabs.widget(3)
        from PySide6.QtWidgets import QLabel
        said = [label.text() for label in credits_tab.findChildren(QLabel)]
        joined = " ".join(said)
        self.assertIn(about.TRADEMARK_NOTICE, joined)
        self.assertIn(about.DISCLAIMER_NOTICE, joined)
        # Under the credits, not above them.
        self.assertLess(joined.index("webMAN MOD"),
                        joined.index(about.TRADEMARK_NOTICE))


if __name__ == "__main__":
    unittest.main()
