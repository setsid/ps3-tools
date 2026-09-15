"""One walk of one console, keyed on title ID.

This replaced five separate walks that could not see each other's answers. The
failure that paid for it: Transfer games offered fourteen disc images totalling
144.6 GB with six of those games already installed on the console, because it
matched on file name and an installed game is not a file.

Nothing here opens a socket. The lister is a dict with a list_dir on it.
"""

import ftplib
import unittest

from ps3tools import inventory


def listing(rows):
    """A Unix LIST as webMANftpd sends one. (name, size, is_directory).

    Every real listing starts with "." and "..", so every listing here does
    too. A walk that counted them reported two games on an empty console.
    """
    lines = ["drwxrwxrwx   1 root  root         0 Jan  1 00:00 .",
             "drwxrwxrwx   1 root  root         0 Jan  1 00:00 .."]
    for name, size, is_directory in rows:
        kind = "d" if is_directory else "-"
        lines.append(f"{kind}rw-rw-rw-   1 root  root  {size:>12} "
                     f"Jan  1 00:00 {name}")
    return "\n".join(lines)


class FakeLister:
    """Reads a dict. A path it does not know is a 550, as a console says."""

    def __init__(self, listings=None, dies_at=None):
        self.listings = dict(listings or {})
        self.dies_at = dies_at
        self.asked = []

    def list_dir(self, path):
        self.asked.append(path)
        if self.dies_at and path.startswith(self.dies_at):
            raise EOFError("the console went away")
        if path not in self.listings:
            raise ftplib.error_perm(f"550 no such directory: {path}")
        return listing(self.listings[path])


GAME = "/dev_hdd0/game/"
PS3ISO = "/dev_hdd0/PS3ISO/"
PS2ISO = "/dev_hdd0/PS2ISO/"
GAMES = "/dev_hdd0/GAMES/"
PACKAGES = "/dev_hdd0/packages/"


class OneConsole(unittest.TestCase):
    """A console holding the same game in four different shapes."""

    def setUp(self):
        self.lister = FakeLister({
            GAME: [("BLES01428", 0, True), ("BLES02077", 0, True)],
            PS3ISO: [("Ghosts [BLES01945].iso", 4 * 1024 ** 3, False),
                     ("Mystery.iso", 7 * 1024 ** 3, False)],
            PS2ISO: [("Ico [SLES50760].iso", 2 * 1024 ** 3, False)],
            GAMES: [("BLES00354-World at War", 0, True)],
            PACKAGES: [("EP0002-BLES01428_00-MW3PATCH.pkg", 500, False)],
        })
        self.found = inventory.read(self.lister)

    def test_every_place_a_game_can_be_is_one_title(self):
        title = self.found.get("BLES01428")
        self.assertEqual([place.where for place in title.places],
                         [inventory.INSTALLED, inventory.PACKAGE])
        self.assertTrue(title.installed)
        self.assertEqual(len(title.packages), 1)

    def test_an_installed_game_is_found_without_being_a_file(self):
        # The whole of the Transfer games failure. An installed game is a
        # folder under /dev_hdd0/game and matches no file name anywhere.
        self.assertEqual(self.found.installed_ids(),
                         ["BLES01428", "BLES02077"])
        self.assertTrue(self.found.is_installed("bles01428"))
        self.assertFalse(self.found.is_installed("BLES01945"))

    def test_a_disc_image_and_a_folder_game_are_told_apart(self):
        self.assertTrue(self.found.get("BLES01945").images)
        self.assertFalse(self.found.get("BLES01945").folder_games)
        self.assertTrue(self.found.get("BLES00354").folder_games)
        self.assertFalse(self.found.get("BLES00354").images)

    def test_the_console_name_is_taken_from_the_listing(self):
        self.assertEqual(self.found.name_for("BLES01945"), "Ghosts")
        self.assertEqual(self.found.name_for("BLES00354"), "World at War")

    def test_a_folder_named_only_after_its_title_id_supplies_no_name(self):
        # Returning "BLES01428" as the name would put the same string on the
        # row twice while claiming the game had been recognised.
        self.assertIsNone(self.found.name_for("BLES01428"))
        self.assertEqual(self.found.get("BLES01428").display_name,
                         "BLES01428")

    def test_the_region_and_platform_come_off_the_id(self):
        self.assertEqual(self.found.get("BLES01428").region, "Europe")
        self.assertEqual(self.found.get("BLES01428").platform, "PS3 disc")

    def test_an_image_with_no_title_id_in_its_name_is_kept_for_opening(self):
        self.assertEqual([row["name"] for row in self.found.unnamed],
                         ["Mystery.iso"])

    def test_a_package_is_filed_under_the_title_it_is_for(self):
        # A package file is named after its content ID. The reader that finds
        # a title ID in an ordinary name cannot see it there, because the _00
        # that follows leaves it no word boundary to stop at.
        self.assertEqual(
            inventory.title_id_in_package("EP0002-BLES01428_00-MW3PATCH.pkg"),
            "BLES01428")
        self.assertEqual(inventory.title_id_in_package("something.pkg"), "")

    def test_the_raw_listings_survive_for_callers_about_files(self):
        # Transfer games shows what is in each folder whether or not it could
        # name the game, so the rows have to come back as well as the titles.
        names = {row["name"] for row in self.found.entries}
        self.assertIn("Mystery.iso", names)
        self.assertEqual(self.found.listings["/dev_hdd0/PS3ISO"]
                         ["mystery.iso"], 7 * 1024 ** 3)

    def test_dot_and_dot_dot_are_never_games(self):
        for row in self.found.entries:
            self.assertNotIn(row["name"], (".", ".."))


class AConsoleThatWillNotAnswer(unittest.TestCase):
    """An empty console and a console that has gone are different answers."""

    def test_a_missing_game_folder_is_said_and_is_not_a_fault(self):
        found = inventory.read(FakeLister({}))
        self.assertEqual(found.titles, {})
        self.assertFalse(found.game_root_unknown)
        self.assertTrue(any("nothing is installed" in note
                            for note in found.notes))

    def test_a_console_that_stopped_answering_says_so(self):
        # Reading this as an empty console is how somebody with a shelf of
        # games is told they have none.
        found = inventory.read(FakeLister({}, dies_at="/dev_hdd0/game"))
        self.assertTrue(found.game_root_unknown)
        self.assertTrue(any("stopped answering" in note
                            for note in found.notes))

    def test_a_game_folder_that_is_not_there_costs_nothing(self):
        # Most of the seven do not exist on a normal console. A note for each
        # would bury the answer under six lines saying nothing happened.
        found = inventory.read(FakeLister({GAME: [("BLES01428", 0, True)]}))
        self.assertEqual(found.notes, [])
        self.assertEqual(found.installed_ids(), ["BLES01428"])

    def test_it_never_raises_whatever_the_console_does(self):
        found = inventory.read(FakeLister({}, dies_at="/"))
        self.assertEqual(found.titles, {})


class TheOneFolderListAndTheOnePattern(unittest.TestCase):
    """Written out four times before this, and two of them differed."""

    def test_the_game_folders_are_the_diagnostic_collector_s_own(self):
        from ps3diag import collectors
        self.assertEqual(
            inventory.GAME_FOLDERS,
            tuple(name for name in collectors.GAME_FOLDERS if name != "PKG"))

    def test_the_packages_folder_is_kept_out_of_the_game_folders(self):
        # A package waiting to be installed is not an installed game, and
        # counting it as one puts a row on a screen for something nobody has.
        self.assertNotIn("PKG", inventory.GAME_FOLDERS)
        self.assertTrue(inventory.PACKAGES_FOLDER.endswith("/packages"))

    def test_the_title_id_pattern_is_strict_about_what_a_folder_is(self):
        for good in ("BLES01428", "NPEB02143", "BLUS31011"):
            self.assertTrue(inventory.TITLE_ID.match(good), good)
        for bad in ("bles01428", "BLES0142", "BLES014288", "USRDIR",
                    "BLES01428-DATA"):
            self.assertFalse(inventory.TITLE_ID.match(bad), bad)

    def test_a_folder_that_is_not_title_shaped_is_not_a_game(self):
        found = inventory.read(FakeLister({
            GAME: [("BLES01428", 0, True), ("TEMP", 0, True),
                   ("readme.txt", 40, False)]}))
        self.assertEqual(found.installed_ids(), ["BLES01428"])


class EveryScreenAsksTheSameThing(unittest.TestCase):
    """The five walks are callers of this now rather than copies of it.

    Before the collapse: the patcher walked /dev_hdd0/game, Game updates
    walked it again and then every game folder, Install packages asked a
    third time for the folder names, Back up save data walked four roots of
    its own, and Transfer games walked the game folders keyed on file name.
    Each carried its own folder list and its own idea of a title ID, and two
    of the lists differed.
    """

    def test_there_is_one_folder_list(self):
        from ps3tools import transfer, updates
        self.assertIs(transfer.BROWSE_FOLDERS, inventory.GAME_FOLDERS)
        self.assertIs(updates.INVENTORY_FOLDERS, inventory.GAME_FOLDERS)

    def test_there_is_one_title_id_pattern(self):
        from ps3tools import detect, updates
        self.assertIs(updates.TITLE_ID, inventory.TITLE_ID)
        self.assertIs(detect.TITLE_DIR, inventory.TITLE_ID)

    def test_there_is_one_place_the_game_folder_is_named(self):
        from ps3tools import detect
        self.assertIs(detect.GAME_ROOT, inventory.GAME_ROOT)

    def test_no_other_module_writes_the_title_id_shape_out_again(self):
        # Checked in the source rather than by behaviour: a second copy of
        # this pattern is correct the day it is written and wrong the day one
        # of the two is changed.
        import ast
        import os
        from support import ROOT

        shape = "[A-Z]{4}"
        guilty = []
        folder = os.path.join(ROOT, "ps3tools")
        for where, _dirs, files in os.walk(folder):
            if "__pycache__" in where:
                continue
            for name in files:
                if not name.endswith(".py") or name == "inventory.py":
                    continue
                path = os.path.join(where, name)
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and \
                            isinstance(node.value, str) and \
                            shape in node.value and "\\d{5}" in node.value:
                        guilty.append(f"{name}:{node.lineno}")
        self.assertEqual(guilty, [], "a second title ID pattern")

    def test_no_other_module_builds_its_own_game_folder_list(self):
        import os
        from support import ROOT

        folder = os.path.join(ROOT, "ps3tools")
        guilty = []
        for where, _dirs, files in os.walk(folder):
            if "__pycache__" in where:
                continue
            for name in files:
                if not name.endswith(".py") or name == "inventory.py":
                    continue
                with open(os.path.join(where, name), encoding="utf-8") as fh:
                    if "collectors.GAME_FOLDERS" in fh.read():
                        guilty.append(name)
        self.assertEqual(guilty, [], "a second game folder list")
