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
from ps3tools.shell.updatebanner import NOTES_LIMIT, UpdateBanner
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

    def banner(self, fetcher=None, downloader=None):
        widget = UpdateBanner(self.services, fetcher=fetcher,
                              downloader=downloader,
                              opener=self.opened.append)
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
        self.assertIn("9.9", screen.banner.heading.text())
        self.assertIn("Fixes a thing.", screen.banner.notes.text())

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
        self.assertIn("matches the checksum", widget.detail.text())
        self.assertTrue(self.on_show(widget.folder_button))
        self.assertTrue(os.listdir(self.folder))

    def test_a_mismatched_download_is_refused_and_not_kept(self):
        widget = self.banner(downloader=self.bytes_fetcher())
        widget.show_release(self.release("sha256 " + "c" * 64))
        with _desktop(self.folder):
            widget.download()
            self.pump()
        self.assertIn("does not match", widget.detail.text())
        self.assertEqual(os.listdir(self.folder), [])

    def test_notes_with_no_hash_are_not_called_verified(self):
        widget = self.banner(downloader=self.bytes_fetcher())
        widget.show_release(self.release("Just some notes."))
        with _desktop(self.folder):
            widget.download()
            self.pump()
        self.assertIn("not been verified", widget.detail.text())
        self.assertNotIn("matches the checksum", widget.detail.text())

    def test_the_release_page_opens_through_the_injected_opener(self):
        widget = self.banner()
        release = self.release()
        widget.show_release(release)
        widget.open_release_page()
        self.assertEqual(self.opened, [release.page_url])

    def test_long_notes_are_trimmed_rather_than_scrolled(self):
        widget = self.banner()
        widget.show_release(self.release("x" * 2000))
        self.assertLessEqual(len(widget.notes.text()),
                             NOTES_LIMIT + 3)


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
