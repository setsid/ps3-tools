"""Named consoles, and the settings file moving to the user's own folder.

Somebody with two PS3s on one network was shown one console's remembered games
against the other, because everything per-console was keyed by IP address and
both consoles take whatever the router hands them.

Nothing here touches a network or a real settings file: profiles is pure data
over a dictionary, and the path tests use a temporary folder.
"""

import json
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock

from support import ROOT  # noqa: F401  (puts the project on sys.path)

from ps3diag import config
from ps3tools import profiles


class UpgradingFromOneConsole(unittest.TestCase):
    """A settings file that only ever knew one address."""

    def test_the_saved_address_becomes_the_first_console(self):
        # Nobody is asked to set up something they already had.
        settings = {"host": "192.168.50.95"}
        key = profiles.ensure(settings)
        self.assertTrue(key)
        current = profiles.current(settings)
        self.assertEqual(current["host"], "192.168.50.95")
        self.assertEqual(current["name"], profiles.DEFAULT_NAME)
        # A profile made for it is not the same as the user having saved it.
        self.assertFalse(current["named"])
        self.assertFalse(profiles.is_saved(settings, "192.168.50.95"))

    def test_asking_twice_does_not_make_two(self):
        settings = {"host": "192.168.50.95"}
        first = profiles.ensure(settings)
        second = profiles.ensure(settings)
        self.assertEqual(first, second)
        self.assertEqual(len(profiles.all_profiles(settings)), 1)


class TwoConsolesOnOneNetwork(unittest.TestCase):
    def setUp(self):
        self.settings = {"host": "192.168.50.95"}
        self.living = profiles.ensure(self.settings)
        profiles.rename(self.settings, self.living, "Living room")
        self.bedroom = profiles.add(self.settings, host="192.168.50.42",
                                    name="Bedroom")

    def test_each_console_keeps_its_own_state(self):
        # The whole point. One console's remembered games must never be shown
        # against the other.
        profiles.state(self.settings, self.living)["updates_seen"] = ["a"]
        profiles.state(self.settings, self.bedroom)["updates_seen"] = ["b"]
        self.assertEqual(
            profiles.state(self.settings, self.living)["updates_seen"], ["a"])
        self.assertEqual(
            profiles.state(self.settings, self.bedroom)["updates_seen"], ["b"])

    def test_state_is_filed_by_profile_rather_than_by_address(self):
        # A DHCP lease changes and the console is still the same console.
        profiles.state(self.settings, self.living)["updates_seen"] = ["a"]
        profiles.set_host(self.settings, self.living, "192.168.50.200")
        profiles.select(self.settings, self.living)
        self.assertEqual(profiles.state(self.settings)["updates_seen"], ["a"])

    def test_two_consoles_swapping_addresses_keep_their_own_state(self):
        profiles.state(self.settings, self.living)["updates_seen"] = ["a"]
        profiles.state(self.settings, self.bedroom)["updates_seen"] = ["b"]
        profiles.set_host(self.settings, self.living, "192.168.50.42")
        profiles.set_host(self.settings, self.bedroom, "192.168.50.95")
        self.assertEqual(
            profiles.state(self.settings, self.living)["updates_seen"], ["a"])

    def test_switching_changes_the_address_in_use(self):
        self.assertEqual(profiles.select(self.settings, self.living),
                         "192.168.50.95")
        self.assertEqual(self.settings["host"], "192.168.50.95")

    def test_forgetting_a_console_takes_its_state_with_it(self):
        profiles.state(self.settings, self.bedroom)["updates_seen"] = ["b"]
        self.assertTrue(profiles.forget(self.settings, self.bedroom))
        self.assertNotIn(self.bedroom, self.settings.get("console_state", {}))
        self.assertEqual(len(profiles.all_profiles(self.settings)), 1)

    def test_a_name_is_tidied_and_capped(self):
        profiles.rename(self.settings, self.bedroom, "  the  back \n room  ")
        self.assertEqual(profiles.all_profiles(self.settings)[self.bedroom]
                         ["name"], "the back room")
        profiles.rename(self.settings, self.bedroom, "x" * 200)
        self.assertLessEqual(
            len(profiles.all_profiles(self.settings)[self.bedroom]["name"]),
            profiles.MAX_NAME)

    def test_an_empty_name_still_gives_the_console_one(self):
        profiles.rename(self.settings, self.bedroom, "   ")
        self.assertTrue(profiles.all_profiles(self.settings)[self.bedroom]
                        ["name"])

    def test_it_all_survives_the_settings_file(self):
        reloaded = json.loads(json.dumps(self.settings))
        self.assertEqual(len(profiles.all_profiles(reloaded)), 2)
        self.assertIsNotNone(profiles.current(reloaded))


class AskingAboutAConsoleWeDoNotKnow(unittest.TestCase):
    """A different address must not quietly move an existing console."""

    def test_an_unknown_address_is_not_claimed_by_the_current_console(self):
        settings = {"host": "192.168.50.95"}
        key = profiles.ensure(settings)
        self.assertEqual(profiles.for_host(settings, "192.168.50.96"), "")
        # The console that was there is still where it was.
        self.assertEqual(profiles.all_profiles(settings)[key]["host"],
                         "192.168.50.95")

    def test_a_known_address_finds_its_own_console(self):
        settings = {"host": "192.168.50.95"}
        key = profiles.ensure(settings)
        other = profiles.add(settings, host="192.168.50.42", name="Bedroom")
        self.assertEqual(profiles.for_host(settings, "192.168.50.95"), key)
        self.assertEqual(profiles.for_host(settings, "192.168.50.42"), other)

    def test_a_console_coming_back_on_a_new_lease_is_still_itself(self):
        # ensure() is the wider question and is allowed to move the address.
        settings = {"host": "192.168.50.95"}
        key = profiles.ensure(settings)
        self.assertEqual(profiles.ensure(settings, host="192.168.50.200"), key)
        self.assertEqual(profiles.all_profiles(settings)[key]["host"],
                         "192.168.50.200")


class RememberedGamesFollowTheConsole(unittest.TestCase):
    """The remembered list moves out from under the address it was filed by."""

    def entry(self):
        return {"checked": "2026-09-15T10:00:00",
                "titles": [{"title_id": "BLES01717", "behind": True}]}

    def test_a_list_kept_under_an_address_is_carried_across(self):
        # Upgrading must not lose somebody's remembered games.
        from ps3tools import updates
        settings = {"host": "192.168.50.95",
                    "updates_seen": {"192.168.50.95": self.entry()}}
        found = updates.remembered_scan(settings, "192.168.50.95")
        self.assertIsNotNone(found)
        self.assertEqual(updates.titles_behind(found), ["BLES01717"])
        # And it is filed under the console now, with the old key cleared.
        self.assertNotIn("updates_seen", settings)
        self.assertIn(profiles.current_id(settings),
                      settings.get("console_state", {}))

    def test_a_second_console_does_not_see_the_first_ones_games(self):
        from ps3tools import updates
        settings = {"host": "192.168.50.95"}
        updates.remember_scan(settings, "192.168.50.95", [])
        profiles.add(settings, host="192.168.50.42", name="Bedroom")
        self.assertIsNone(
            updates.remembered_scan(settings, "192.168.50.42"))


class RubbishInTheSettingsFile(unittest.TestCase):
    """A file edited by hand, or written by an older build."""

    def test_nothing_here_raises_on_nonsense(self):
        for settings in ({"consoles": "not a dict"},
                         {"consoles": {"a": "not a dict"}},
                         {"current_console": "gone"},
                         {"console_state": "not a dict"},
                         {}):
            with self.subTest(settings):
                self.assertIsInstance(profiles.all_profiles(settings), dict)
                self.assertIsNone(profiles.current(settings))
                self.assertEqual(profiles.current_id(settings), "")
                self.assertIsInstance(profiles.state(settings), dict)


class WhereTheSettingsFileLives(unittest.TestCase):
    """In the user's own folder, and moved there from beside the exe."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ps3-config-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.beside = os.path.join(self.folder, "beside")
        self.user = os.path.join(self.folder, "user")
        os.makedirs(self.beside)
        os.makedirs(self.user)
        patches = [mock.patch.object(config, "app_dir", lambda: self.beside),
                   mock.patch.object(config, "user_dir", lambda: self.user)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, folder, name, payload):
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_the_user_folder_is_looked_at_first(self):
        self.assertEqual(config.candidate_paths()[0], config.settings_path())

    def test_a_file_beside_the_exe_is_moved_rather_than_copied(self):
        # Moved, so a later run cannot read a stale copy and put back an
        # address the user has since changed.
        self.write(self.beside, config.FILENAME, {"ip": "192.168.50.95"})
        moved = config.migrate()
        self.assertEqual(moved, config.settings_path())
        self.assertFalse(os.path.exists(config.legacy_path()))
        with open(moved, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["ip"], "192.168.50.95")

    def test_migrating_never_overwrites_what_is_already_there(self):
        # The user folder's copy is the current one by definition.
        self.write(self.beside, config.FILENAME, {"ip": "old"})
        self.write(self.user, config.FILENAME, {"ip": "current"})
        self.assertIsNone(config.migrate())
        with open(config.settings_path(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["ip"], "current")

    def test_nothing_to_move_is_not_a_failure(self):
        self.assertIsNone(config.migrate())

    def test_the_shell_settings_file_moves_too(self):
        from ps3tools.shell import app as shell_app
        self.write(self.beside, shell_app.SETTINGS_FILE, {"host": "1.2.3.4"})
        config.migrate(shell_app.SETTINGS_FILE)
        self.assertTrue(os.path.isfile(
            config.settings_path(shell_app.SETTINGS_FILE)))

    def test_a_folder_that_cannot_be_written_is_survived(self):
        # Failing to move a settings file costs a saved address. Failing to
        # start costs everything.
        self.write(self.beside, config.FILENAME, {"ip": "192.168.50.95"})
        with mock.patch.object(config.shutil, "move",
                               side_effect=OSError("read only")):
            self.assertIsNone(config.migrate())


if __name__ == "__main__":
    unittest.main()
