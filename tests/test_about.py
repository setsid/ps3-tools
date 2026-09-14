"""The About screen and the update banner, built for real and driven.

Both are built with an injected fetcher, every time, and setUp makes the real
urllib opener raise. Nothing in this file can make a request: the screen that
carries the promise about what leaves the machine is not going to be the one
that quietly breaks it in the test suite.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
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
        self.services.wait(milliseconds)
        for _ in range(5):
            APP.processEvents()

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
        screen = self.build()
        self.assertTrue(screen.findChildren(type(screen.update_box)))

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
        for name in ("webMAN MOD", "scetool", "naehrwert", "PySide6",
                     "patch-bo2.py", "patch-mw3.py"):
            self.assertIn(name, text)

    def test_it_states_its_own_licence_and_only_credits_the_rest(self):
        # The MIT claim is about this project's code. The others are credited,
        # and the screen must not put words in their authors' mouths.
        text = self.text_of(self.build())
        self.assertIn("MIT licensed", text)
        for name in ("webMAN MOD", "scetool", "PySide6"):
            self.assertIn(name, text)
        for claim in ("scetool is MIT", "webMAN MOD is MIT",
                      "GPL compliant", "fully compliant"):
            self.assertNotIn(claim, text)

    def test_no_emoji_anywhere(self):
        for character in self.text_of(self.build()):
            self.assertLess(ord(character), 0x2190, repr(character))


# --- the setting -------------------------------------------------------------

class SettingTests(AboutCase):
    def test_on_by_default(self):
        self.assertTrue(self.build().update_box.isChecked())

    def test_unticking_it_reaches_the_settings(self):
        screen = self.build()
        screen.update_box.setChecked(False)
        self.assertIs(self.settings[update.SETTING_ENABLED], False)
        self.assertFalse(update.enabled(self.settings))

    def test_the_setting_round_trips_through_a_rebuild(self):
        screen = self.build()
        screen.update_box.setChecked(False)
        stored = json.loads(json.dumps(self.settings))
        self.settings.clear()
        self.settings.update(stored)
        self.assertFalse(self.build().update_box.isChecked())
        self.build().update_box.setChecked(True)
        self.assertTrue(update.enabled(self.settings))

    def test_switching_it_back_on_forgets_the_cache(self):
        # Otherwise yesterday's answer keeps it quiet for a day after the user
        # has just asked for it back.
        screen = self.build()
        self.settings[update.SETTING_CACHE] = {"checked": 1.0, "release": None}
        screen.update_box.setChecked(False)
        screen.update_box.setChecked(True)
        self.assertNotIn(update.SETTING_CACHE, self.settings)

    def test_check_now_refuses_while_it_is_switched_off(self):
        seen = []
        screen = self.build(fetcher=json_fetcher(
            release_payload("v9.9"), seen))
        screen.update_box.setChecked(False)
        screen.check_now()
        self.pump()
        self.assertEqual(seen, [])
        self.assertEqual(screen.check_status.text(), about.DISABLED_NOTE)
        self.assertFalse(self.on_show(screen.banner))


# --- checking ----------------------------------------------------------------

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


if __name__ == "__main__":
    unittest.main()
