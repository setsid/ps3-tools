"""Save data: the survey, the copy, and the screen that drives both.

Two rules this module keeps absolutely.

Nothing here reaches a network except the loopback mock the test starts itself.
There is a live PS3 on this LAN and the way a suite starts talking to it is
never on purpose: it is a seam that was inert becoming live because something
downstream of it grew a default. So both seams in the screen -- the thing that
opens a connection and the thing that reads a save file -- are replaced in
these tests with objects that raise if they are so much as touched, and there
are tests whose entire job is to prove that replacing them is enough.

Nothing here writes to a real Desktop. Every copy is given an explicit
destination inside a temporary folder.
"""

import errno
import ftplib
import hashlib
import json
import os
import unittest
import zipfile
from datetime import datetime
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile

from support import FixtureCase, fixture

from mock_webman import MockWebmanFtp
from ps3diag.transport import FtpLister

from ps3tools import savedata

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    QT = True
except ImportError:                                         # pragma: no cover
    QT = False


HOME = savedata.HOME_ROOT


def listing(name):
    return fixture("saves", name)


# The console the fakes below pretend to be. One user with five save folders,
# one user whose savedata folder does not exist, one whose savedata folder is
# empty.
LISTINGS = {
    f"{HOME}/": listing("list_home.txt"),
    f"{HOME}/00000001/savedata/": listing("list_savedata_00000001.txt"),
    f"{HOME}/00000003/savedata/": listing("list_savedata_00000003.txt"),
    f"{HOME}/00000001/savedata/BLES01717-DATA000/":
        listing("list_save_bles01717.txt"),
    f"{HOME}/00000001/savedata/NPEB02143-AUTOSAVE/":
        listing("list_save_npeb02143.txt"),
    f"{HOME}/00000001/savedata/BLES09999-DATA000/":
        listing("list_save_bles09999.txt"),
    f"{HOME}/00000001/savedata/MY OLD SAVES/": listing("list_save_named.txt"),
    # BLES01428-DATA000 is deliberately absent: a folder the console refuses.
    "/dev_hdd0/GAMES/": listing("list_inventory_games.txt"),
}


class FakeLister:
    """A lister with no socket in it at all.

    A path it does not know about is a 550, which is what the console says for
    a directory that is not there and is the only kind of failure the survey is
    allowed to treat as an answer.
    """

    def __init__(self, listings=None, dies_at=None):
        self.listings = dict(LISTINGS if listings is None else listings)
        #: path prefix at which the console stops answering altogether
        self.dies_at = dies_at
        self.asked = []
        self.closed = False

    def list_dir(self, path):
        self.asked.append(path)
        if self.dies_at and path.startswith(self.dies_at):
            raise OSError("connection reset by peer")
        if path in self.listings:
            return self.listings[path]
        raise ftplib.error_perm(f"550 no such directory: {path}")

    def close(self):
        self.closed = True


class Refuses:
    """Stands in for the real client. Touching it at all is the failure.

    This is the guard. A seam that has quietly become live shows up here as an
    exception with a sentence in it that could only have come from this class,
    rather than as a packet on the LAN that nobody notices for a fortnight.
    """

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "the real console client was constructed inside a test")

    @staticmethod
    def factory(*args, **kwargs):
        raise AssertionError(
            "the real console client was constructed inside a test")


def save_bytes(path):
    """Deterministic contents for any save path, so hashes can be checked."""
    return f"contents of {path}".encode("utf-8")


class Reader:
    """An injected save reader. Records what it was asked for."""

    def __init__(self, fails_at=None, error=None):
        self.asked = []
        self.fails_at = fails_at
        self.error = error or OSError("connection reset by peer")

    def __call__(self, path):
        self.asked.append(path)
        if self.fails_at and self.fails_at in path:
            raise self.error
        return save_bytes(path)


# --- the survey ------------------------------------------------------------

class SurveyTests(FixtureCase):

    def survey(self, **kwargs):
        self.lister = FakeLister(**kwargs)
        return savedata.survey(self.lister)

    def test_it_finds_every_user_folder_and_ignores_anything_else(self):
        report = self.survey()
        self.assertEqual([user.user_id for user in report.users],
                         ["00000001", "00000002", "00000003"])

    def test_it_finds_the_save_folders_under_a_user(self):
        report = self.survey()
        first = report.users[0]
        self.assertEqual(
            sorted(save.folder for save in first.saves),
            ["BLES01428-DATA000", "BLES01717-DATA000", "BLES09999-DATA000",
             "MY OLD SAVES", "NPEB02143-AUTOSAVE"])

    def test_a_known_title_gets_its_real_name(self):
        report = self.survey()
        save = report.find("00000001/BLES01717-DATA000")
        self.assertEqual(save.title_id, "BLES01717")
        self.assertTrue(save.named)
        self.assertIn("Black Ops", save.display_name)

    def test_a_name_is_taken_from_the_consoles_own_game_inventory(self):
        # NPEB02143 is not in the verified table. The console's GAMES folder
        # spells it out, which is the whole point of reading the inventory.
        report = self.survey()
        save = report.find("00000001/NPEB02143-AUTOSAVE")
        self.assertEqual(save.display_name, "Persona 5")

    def test_a_title_id_with_no_name_anywhere_keeps_the_folder_name(self):
        report = self.survey()
        save = report.find("00000001/BLES09999-DATA000")
        self.assertEqual(save.title_id, "BLES09999")
        self.assertFalse(save.named)
        self.assertEqual(save.display_name, "BLES09999-DATA000")
        # The ID still says where the game came from, and that is all it says.
        self.assertEqual(save.region, "Europe")

    def test_a_folder_with_no_recognisable_title_id_is_still_listed(self):
        report = self.survey()
        save = report.find("00000001/MY OLD SAVES")
        self.assertIsNotNone(save)
        self.assertIsNone(save.title_id)
        self.assertFalse(save.named)
        self.assertEqual(save.display_name, "MY OLD SAVES")
        savedata.read_files(self.lister, save)
        self.assertEqual([item.name for item in save.files], ["notes.txt"])

    def test_the_files_in_a_save_folder_are_read_with_their_sizes(self):
        report = self.survey()
        save = report.find("00000001/BLES01717-DATA000")
        self.assertIsNone(savedata.read_files(self.lister, save))
        self.assertEqual([item.name for item in save.files],
                         ["DATA000.DAT", "ICON0.PNG", "PARAM.PFD",
                          "PARAM.SFO"])
        self.assertEqual(save.total_bytes, 2048 + 640 + 30512 + 184320)
        self.assertEqual(save.files[0].path,
                         f"{HOME}/00000001/savedata/BLES01717-DATA000/"
                         f"DATA000.DAT")

    def test_a_folder_inside_a_save_folder_is_not_taken_for_a_file(self):
        report = self.survey()
        save = report.find("00000001/NPEB02143-AUTOSAVE")
        savedata.read_files(self.lister, save)
        self.assertEqual([item.name for item in save.files],
                         ["PARAM.SFO", "SYSTEM.DAT"])

    # -- how much it asks the console for

    def test_the_scan_never_opens_a_save_folder(self):
        # The bug this exists for: a scan of one user with five saves opened
        # every one of them, which is five more passive data connections than
        # curl needs to list the same directory, and a real console gave up
        # part way through. A save folder is opened when somebody picks it.
        report = self.survey()
        self.assertEqual(report.save_count, 5)
        for save in report.saves:
            self.assertFalse(save.listed, save.folder)
            self.assertEqual(save.files, [])
            self.assertNotIn(save.path + "/", self.lister.asked)

    def test_the_scan_costs_one_listing_for_home_and_one_per_user(self):
        self.survey()
        saves_asked = [path for path in self.lister.asked
                       if path.startswith(HOME)]
        self.assertEqual(saves_asked, [
            f"{HOME}/",
            f"{HOME}/00000001/savedata/",
            f"{HOME}/00000002/savedata/",
            f"{HOME}/00000003/savedata/",
        ])

    def test_the_saves_are_read_before_the_inventory_that_only_names_them(self):
        # The inventory is four more listings and buys nothing but nicer
        # words. Asking for it first spent the connection on the part of this
        # nobody would miss, and the saves paid for it.
        self.survey()
        first_save = self.lister.asked.index(f"{HOME}/")
        first_name = self.lister.asked.index("/dev_hdd0/GAMES/")
        self.assertLess(first_save, first_name)

    def test_a_folder_size_is_not_claimed_before_the_folder_is_opened(self):
        # The console reports 0 for a directory. Showing that as "0 bytes"
        # tells the user the save is empty, which is a different statement
        # from not having looked.
        report = self.survey()
        save = report.find("00000001/BLES01717-DATA000")
        self.assertFalse(save.size_known)
        self.assertFalse(report.size_known)
        savedata.read_files(self.lister, save)
        self.assertTrue(save.size_known)

    def test_a_folder_that_failed_once_is_asked_again_and_not_written_off(self):
        # The note from a failed attempt must not outlive the attempt. A copy
        # run again after the console came back would otherwise skip the very
        # folder it was run again for.
        report = self.survey()
        save = report.find("00000001/MY OLD SAVES")
        self.assertEqual(savedata.read_files(FakeLister(dies_at=HOME), save),
                         "lost")
        self.assertIsNotNone(save.note)
        self.assertIsNone(savedata.read_files(self.lister, save))
        self.assertIsNone(save.note)
        self.assertEqual([item.name for item in save.files], ["notes.txt"])

    def test_opening_a_folder_twice_only_asks_the_console_once(self):
        report = self.survey()
        save = report.find("00000001/MY OLD SAVES")
        savedata.read_files(self.lister, save)
        before = list(self.lister.asked)
        self.assertIsNone(savedata.read_files(self.lister, save))
        self.assertEqual(self.lister.asked, before)

    def test_a_console_that_refuses_one_folder_says_so_about_that_folder(self):
        report = self.survey()
        save = report.find("00000001/BLES01428-DATA000")
        self.assertEqual(savedata.read_files(self.lister, save), "refused")
        self.assertEqual(save.note, savedata.UNREADABLE_SAVE)
        self.assertFalse(save.readable)
        self.assertEqual(save.files, [])

    def test_a_console_that_vanishes_while_opening_a_folder_says_so(self):
        report = self.survey()
        save = report.find("00000001/MY OLD SAVES")
        lister = FakeLister(dies_at=HOME)
        self.assertEqual(savedata.read_files(lister, save), "lost")
        self.assertIn("stopped answering", save.note)

    def test_a_lister_that_cannot_list_at_all_is_not_a_crash(self):
        report = self.survey()
        save = report.find("00000001/MY OLD SAVES")
        self.assertEqual(savedata.read_files(object(), save), "refused")

    # -- the console that was actually in front of somebody

    def test_the_four_folders_off_the_real_console_are_all_listed(self):
        # Every other directory on that console is drwxrwxrwx; the save
        # folders are drwx------, which the listing parser has to accept and
        # nothing on this path is allowed to branch on.
        report = self.survey(listings={
            f"{HOME}/": listing("list_home.txt"),
            f"{HOME}/00000001/savedata/":
                listing("list_savedata_real_console.txt"),
        })
        folders = [save.folder for save in report.users[0].saves]
        self.assertEqual(sorted(folders), [
            "BLES01428-AUTO-", "BLES01717-AUTO-",
            "BLES01976--260914094245", "BLES01976-OPTIONS"])
        for save in report.users[0].saves:
            self.assertIsNotNone(save.title_id, save.folder)
        # Two saves of the same game are still two rows, told apart by the
        # folder the console calls them.
        keys = {save.key for save in report.users[0].saves}
        self.assertEqual(len(keys), 4)

    # -- the required failure paths

    def test_no_home_folder_at_all_is_said_plainly_and_never_raised(self):
        report = self.survey(listings={})
        self.assertEqual(report.users, [])
        self.assertIn(savedata.NO_HOME, report.notes)

    def test_a_user_with_no_savedata_folder_is_listed_with_the_reason(self):
        report = self.survey()
        user = report.users[1]
        self.assertEqual(user.user_id, "00000002")
        self.assertEqual(user.saves, [])
        self.assertEqual(user.note, savedata.NO_SAVEDATA)

    def test_a_user_with_an_empty_savedata_folder_says_so(self):
        report = self.survey()
        user = report.users[2]
        self.assertEqual(user.saves, [])
        self.assertIn("empty", user.note)

    def test_a_save_folder_that_cannot_be_listed_is_kept_and_marked(self):
        # Still listed after the scan: the folder is there and the user must
        # see it. Whether it can be opened is not asked until it is picked.
        report = self.survey()
        save = report.find("00000001/BLES01428-DATA000")
        self.assertIsNotNone(save)
        self.assertTrue(save.readable)
        self.assertEqual(save.files, [])

    def test_a_console_that_disappears_mid_scan_keeps_what_it_found(self):
        report = self.survey(dies_at=f"{HOME}/00000003")
        self.assertTrue(report.users)
        # Everything listed before the user that killed it is still here.
        self.assertEqual(len(report.users[0].saves), 5)
        self.assertIn("stopped answering", report.users[2].note)
        self.assertTrue(any("stopped answering" in note
                            for note in report.notes))

    def test_a_console_that_disappears_on_the_home_listing_never_raises(self):
        report = savedata.survey(FakeLister(dies_at=HOME))
        self.assertEqual(report.users, [])
        self.assertTrue(any("stopped answering" in note
                            for note in report.notes))

    def test_a_lister_that_raises_something_unexpected_is_still_survived(self):
        class Broken:
            def list_dir(self, path):
                raise ValueError("nonsense")

        report = savedata.survey(Broken())
        self.assertEqual(report.users, [])
        self.assertTrue(report.notes)


class AFailedScanIsNotAnEmptyOne(FixtureCase):
    """The distinction bug 2 was: "nothing there" against "could not tell".

    ps3diag.patchstate already draws it -- an absent installed_titles means
    nobody asked, which is not the same as no update installed. A survey that
    broke off is the same shape of answer and must not be collapsed into the
    other one.
    """

    def test_a_scan_that_finished_and_found_nothing_is_marked_complete(self):
        report = savedata.survey(FakeLister(listings={
            f"{HOME}/": listing("list_home.txt"),
        }))
        self.assertEqual(report.save_count, 0)
        self.assertTrue(report.complete)
        self.assertEqual(report.errors, [])

    def test_a_console_that_went_away_is_never_marked_complete(self):
        report = savedata.survey(FakeLister(dies_at=HOME))
        self.assertEqual(report.save_count, 0)
        self.assertFalse(report.complete)
        self.assertTrue(report.errors)
        # Whatever is said about it, it is said in the notes as well.
        for reason in report.errors:
            self.assertIn(reason, report.notes)

    def test_a_scan_that_broke_off_part_way_is_partial_not_complete(self):
        report = savedata.survey(FakeLister(dies_at=f"{HOME}/00000003"))
        self.assertTrue(report.save_count)
        self.assertFalse(report.complete)

    def test_a_listing_nobody_could_read_counts_as_not_having_finished(self):
        # Lines in a shape this tool does not know may have been user folders.
        # Reporting what parsed as though it were everything is the same
        # mistake in a quieter form.
        report = savedata.survey(FakeLister(listings={
            f"{HOME}/": "this is not an FTP listing at all\nnor is this\n",
        }))
        self.assertEqual(report.save_count, 0)
        self.assertFalse(report.complete)

    def test_no_home_folder_is_an_answer_and_counts_as_finished(self):
        # A refusal is the console saying the folder is not there, which is a
        # statement it is entitled to make.
        report = savedata.survey(FakeLister(listings={}))
        self.assertIn(savedata.NO_HOME, report.notes)
        self.assertTrue(report.complete)


class InventoryTests(FixtureCase):

    def test_a_bare_title_id_folder_contributes_no_name(self):
        # "BLES01428" says nothing that the ID does not already say, and using
        # it as a name would print the same string twice while claiming the
        # game had been recognised.
        found = savedata.build_inventory(FakeLister())
        self.assertNotIn("BLES01428", found)
        self.assertEqual(found.get("NPEB02143"), "Persona 5")

    def test_an_inventory_root_that_is_missing_costs_only_its_names(self):
        found = savedata.build_inventory(FakeLister(listings={}))
        self.assertEqual(found, {})

    def test_the_verified_table_beats_a_folder_name_on_the_console(self):
        name = savedata.name_for("BLES01717", {"BLES01717": "whatever"})
        self.assertIn("Black Ops", name)

    def test_an_iso_name_loses_its_extension_and_its_bracketed_id(self):
        found = savedata.build_inventory(FakeLister(listings={
            "/dev_hdd0/PS3ISO/": fixture("ftp", "list_ps3iso.txt")}))
        self.assertEqual(found.get("BCES01584"), "The Last of Us")


# --- a console that hangs up ------------------------------------------------

class HangsUpAfter:
    """A webMANftpd that drops the control connection after N listings.

    This is the shape the real console failed in. curl asks it for one
    directory and gets it instantly; this tool asked for a dozen in a row, each
    one its own passive data connection, and somewhere around the sixth the
    server stopped answering and ftplib raised EOFError. The count is per
    control connection and is reset by USER, because a fresh connection is
    what the client's retry gets and the point is whether that is enough.
    """

    def __init__(self, per_connection):
        self.per_connection = per_connection
        self.drops = 0
        self._served = 0

    def __call__(self, verb, _argument):
        if verb == "USER":
            self._served = 0
        elif verb in ("LIST", "NLST"):
            self._served += 1
            if self._served > self.per_connection:
                self.drops += 1
                return "DROP"
        return None


class AConsoleThatKeepsHangingUp(FixtureCase):
    """Bug 1, through the real transport, against 127.0.0.1 and nothing else."""

    def console(self, per_connection):
        fault = HangsUpAfter(per_connection)
        server = MockWebmanFtp(
            listings={path: text.encode("utf-8")
                      for path, text in LISTINGS.items()},
            fault=fault).start()
        self.addCleanup(server.stop)

        lister = FtpLister("127.0.0.1", timeout=10)

        def connect():
            ftp = ftplib.FTP(encoding="latin-1")
            ftp.connect("127.0.0.1", server.port, timeout=10)
            ftp.login("anonymous", "anonymous@")
            return ftp

        lister._factory = connect
        self.addCleanup(lister.close)
        self.server = server
        return lister, fault

    def test_the_survey_finishes_against_a_console_that_keeps_hanging_up(self):
        # One reconnect per command is what the transport offers. As long as a
        # fresh connection will serve at least one listing, that is enough for
        # a whole survey, and this proves the listing path really does go
        # through it rather than round it.
        for per_connection in (1, 2, 3):
            with self.subTest(listings_per_connection=per_connection):
                lister, fault = self.console(per_connection)
                report = savedata.survey(lister)
                self.assertGreater(fault.drops, 0)
                self.assertEqual(report.save_count, 5)
                self.assertTrue(report.complete, report.errors)
                self.assertTrue(any("Black Ops" in save.display_name
                                    for save in report.saves))

    def test_the_survey_asks_for_three_directories_not_a_dozen(self):
        # The fix that matters: the number of data connections a scan opens.
        # Counted against the real client so a future descent into every save
        # folder shows up here as a number, not as a console that gave up.
        lister, _fault = self.console(99)
        savedata.survey(lister)
        asked = [command.split(" ", 1)[1]
                 for command in self.server.commands
                 if command.startswith("LIST ")]
        under_home = [path for path in asked if path.startswith(HOME)]
        self.assertEqual(under_home, [
            f"{HOME}/",
            f"{HOME}/00000001/savedata/",
            f"{HOME}/00000002/savedata/",
            f"{HOME}/00000003/savedata/",
        ])

    def test_a_console_that_hangs_up_every_time_is_never_called_empty(self):
        # Nothing gets through at all. The survey's duty here is to say the
        # scan did not finish; saying the console has no saves on it would be
        # a statement it has no grounds for. This is where bug 1 and bug 2
        # meet: the failure mode of one is the false summary of the other.
        lister, fault = self.console(0)
        report = savedata.survey(lister)
        self.assertGreater(fault.drops, 0)
        self.assertEqual(report.save_count, 0)
        self.assertFalse(report.complete)
        for note in report.notes:
            self.assertNotIn("no saves were found", note.lower())


# --- the copy --------------------------------------------------------------

class BackupCase(FixtureCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ps3-saves-test-")
        self.addCleanup(self._clean)
        self.lister = FakeLister()
        self.report = savedata.survey(self.lister)

    def _clean(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    def pick(self, *keys):
        return [self.report.find(key) for key in keys]

    def run_backup(self, selection, reader=None, **kwargs):
        return savedata.back_up(
            self.lister, selection, destination=self.folder,
            save_reader=reader if reader is not None else Reader(), **kwargs)


class BackupTests(BackupCase):

    def test_the_zip_lands_in_the_folder_it_was_given(self):
        result = self.run_backup(self.pick("00000001/BLES01717-DATA000"))
        self.assertTrue(result.ok)
        self.assertTrue(os.path.isfile(result.zip_path))
        self.assertEqual(os.path.dirname(result.zip_path), self.folder)

    def test_the_default_destination_is_a_dated_folder_on_the_desktop(self):
        path = savedata.destination_for(datetime(2026, 9, 14), base="/base")
        self.assertEqual(path,
                         os.path.join("/base", "PS3 Tools saves", "2026-09-14"))

    def test_every_file_of_the_chosen_save_is_in_the_zip(self):
        result = self.run_backup(self.pick("00000001/BLES01717-DATA000"))
        with zipfile.ZipFile(result.zip_path) as archive:
            names = archive.namelist()
        for name in ("DATA000.DAT", "ICON0.PNG", "PARAM.PFD", "PARAM.SFO"):
            self.assertIn(f"00000001/BLES01717-DATA000/{name}", names)

    def test_the_manifest_records_where_each_file_came_from_and_its_hash(self):
        result = self.run_backup(self.pick("00000001/BLES01717-DATA000"))
        with zipfile.ZipFile(result.zip_path) as archive:
            book = json.loads(archive.read(savedata.MANIFEST_JSON))
            stored = archive.read(
                "00000001/BLES01717-DATA000/PARAM.SFO")
        entry = book["saves"][0]
        self.assertEqual(entry["user"], "00000001")
        self.assertEqual(entry["folder"], "BLES01717-DATA000")
        self.assertIn("Black Ops", entry["game"])
        self.assertEqual(entry["from"],
                         f"{HOME}/00000001/savedata/BLES01717-DATA000")
        record = [item for item in entry["files"]
                  if item["file"] == "PARAM.SFO"][0]
        self.assertEqual(record["from"],
                         f"{HOME}/00000001/savedata/BLES01717-DATA000/"
                         f"PARAM.SFO")
        self.assertEqual(record["sha256"],
                         hashlib.sha256(stored).hexdigest())
        self.assertEqual(book["hash"], "sha256")

    def test_the_manifest_says_in_words_that_this_cannot_be_restored(self):
        result = self.run_backup(self.pick("00000001/BLES01717-DATA000"))
        with zipfile.ZipFile(result.zip_path) as archive:
            text = archive.read(savedata.MANIFEST_TEXT).decode("utf-8")
            book = json.loads(archive.read(savedata.MANIFEST_JSON))
            readme = archive.read(savedata.READ_ME).decode("utf-8")
        self.assertIn(savedata.NO_RESTORE_NOTICE, text)
        self.assertEqual(book["restore"], savedata.NO_RESTORE_NOTICE)
        self.assertIn(savedata.NO_RESTORE_NOTICE, readme)
        self.assertIn("not a backup that can be put back",
                      savedata.NO_RESTORE_NOTICE)

    def test_several_saves_are_kept_apart_inside_the_zip(self):
        result = self.run_backup(self.pick("00000001/BLES01717-DATA000",
                                           "00000001/MY OLD SAVES"))
        with zipfile.ZipFile(result.zip_path) as archive:
            names = archive.namelist()
        self.assertIn("00000001/BLES01717-DATA000/PARAM.SFO", names)
        self.assertIn("00000001/MY OLD SAVES/notes.txt", names)
        self.assertEqual(len(result.saves), 2)

    def test_progress_is_reported_per_save_folder_not_once_for_the_lot(self):
        events = []
        self.run_backup(self.pick("00000001/BLES01717-DATA000",
                                  "00000001/MY OLD SAVES"),
                        progress=events.append)
        starts = [item for item in events if item["kind"] == "save_start"]
        dones = [item for item in events if item["kind"] == "save_done"]
        files = [item for item in events if item["kind"] == "file"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(dones), 2)
        self.assertEqual({item["index"] for item in starts}, {0, 1})
        self.assertTrue(all(item["total"] == 2 for item in starts))
        # Five files across the two folders, each one its own event.
        self.assertEqual(len(files), 5)
        first = [item for item in files
                 if item["key"] == "00000001/BLES01717-DATA000"]
        self.assertEqual([item["done_files"] for item in first], [1, 2, 3, 4])

    def test_a_screen_that_throws_in_its_progress_handler_loses_nothing(self):
        def explode(_event):
            raise RuntimeError("the screen fell over")

        result = self.run_backup(self.pick("00000001/BLES01717-DATA000"),
                                 progress=explode)
        self.assertTrue(result.ok)

    def test_the_reader_is_only_ever_asked_for_the_chosen_saves(self):
        reader = Reader()
        self.run_backup(self.pick("00000001/MY OLD SAVES"), reader=reader)
        self.assertEqual(reader.asked,
                         [f"{HOME}/00000001/savedata/MY OLD SAVES/notes.txt"])

    # -- the required failure paths

    def test_an_empty_selection_writes_nothing_and_says_why(self):
        result = savedata.back_up(self.lister, [], destination=self.folder,
                                  save_reader=Reader())
        self.assertFalse(result.ok)
        self.assertIsNone(result.zip_path)
        self.assertIn(savedata.NOTHING_PICKED, result.notes)
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_connection_with_no_save_reader_copies_nothing_and_says_so(self):
        result = savedata.back_up(self.lister,
                                  self.pick("00000001/MY OLD SAVES"),
                                  destination=self.folder, save_reader=None)
        self.assertFalse(result.ok)
        self.assertIsNone(result.zip_path)
        self.assertIn(savedata.NO_READER, result.notes)

    def test_a_save_folder_that_could_not_be_listed_is_skipped_not_fatal(self):
        result = self.run_backup(self.pick("00000001/BLES01428-DATA000",
                                           "00000001/MY OLD SAVES"))
        self.assertTrue(result.ok)
        refused = result.saves[0]
        self.assertEqual(refused.note, savedata.UNREADABLE_SAVE)
        self.assertEqual(refused.files, [])
        self.assertEqual(len(result.saves[1].files), 1)

    def test_a_console_that_disappears_mid_transfer_keeps_the_partial(self):
        reader = Reader(fails_at="NPEB02143-AUTOSAVE/SYSTEM.DAT")
        result = self.run_backup(
            self.pick("00000001/BLES01717-DATA000",
                      "00000001/NPEB02143-AUTOSAVE",
                      "00000001/MY OLD SAVES"),
            reader=reader)
        # Nothing raised, the first folder is whole, the second is partial and
        # says so, and the third was never attempted.
        self.assertEqual(len(result.saves), 2)
        self.assertEqual(len(result.saves[0].files), 4)
        self.assertEqual(len(result.saves[1].files), 1)
        self.assertIn("stopped answering", result.saves[1].note)
        self.assertTrue(any("stopped answering" in note
                            for note in result.notes))
        with zipfile.ZipFile(result.zip_path) as archive:
            self.assertIn("00000001/NPEB02143-AUTOSAVE/PARAM.SFO",
                          archive.namelist())
            self.assertEqual(archive.testzip(), None)

    def test_one_file_the_console_refuses_does_not_lose_the_folder(self):
        reader = Reader(fails_at="ICON0.PNG",
                        error=ftplib.error_perm("550 access denied"))
        result = self.run_backup(self.pick("00000001/BLES01717-DATA000"),
                                 reader=reader)
        self.assertTrue(result.ok)
        save = result.saves[0]
        self.assertEqual(len(save.files), 3)
        self.assertEqual([name for name, _why in save.skipped], ["ICON0.PNG"])
        self.assertFalse(save.complete)

    def test_a_copy_records_when_it_finished(self):
        result = self.run_backup(self.pick("00000001/MY OLD SAVES"))
        with zipfile.ZipFile(result.zip_path) as archive:
            book = json.loads(archive.read(savedata.MANIFEST_JSON))
        self.assertTrue(result.finished)
        self.assertEqual(book["finished"], result.finished)

    def test_a_full_disk_stops_cleanly_and_names_the_cause(self):
        real = zipfile.ZipFile

        class FullDisk(real):
            def writestr(self, name, data, *args, **kwargs):
                if "PARAM.PFD" in str(name):
                    raise OSError(errno.ENOSPC, "No space left on device")
                return real.writestr(self, name, data, *args, **kwargs)

        with mock.patch.object(savedata.zipfile, "ZipFile", FullDisk):
            result = self.run_backup(self.pick("00000001/BLES01717-DATA000"))
        self.assertFalse(result.ok)
        self.assertTrue(any("run out of disk space" in note
                            for note in result.notes))
        # What was written before the disk filled is still a readable zip.
        self.assertTrue(os.path.isfile(result.zip_path))

    def test_a_destination_that_cannot_be_made_is_reported_not_raised(self):
        blocked = os.path.join(self.folder, "a-file", "inside-it")
        with open(os.path.join(self.folder, "a-file"), "w") as handle:
            handle.write("not a folder")
        result = savedata.back_up(self.lister,
                                  self.pick("00000001/MY OLD SAVES"),
                                  destination=blocked, save_reader=Reader())
        self.assertFalse(result.ok)
        self.assertTrue(any("could not be made" in note
                            for note in result.notes))

    def test_a_cancelled_copy_keeps_what_it_had_and_says_it_stopped(self):
        state = {"count": 0}

        def cancelled():
            state["count"] += 1
            return state["count"] > 1

        result = self.run_backup(self.pick("00000001/BLES01717-DATA000",
                                           "00000001/MY OLD SAVES"),
                                 cancelled=cancelled)
        self.assertTrue(result.cancelled)
        self.assertTrue(any("Stopped before" in note
                            for note in result.notes))

    def test_a_file_too_large_to_be_a_save_is_recorded_and_not_fetched(self):
        save = self.report.find("00000001/MY OLD SAVES")
        savedata.read_files(self.lister, save)
        save.files[0].size = savedata.MAX_SAVE_FILE_BYTES + 1
        reader = Reader()
        result = self.run_backup([save], reader=reader)
        self.assertEqual(reader.asked, [])
        self.assertEqual([name for name, _why in result.saves[0].skipped],
                         ["notes.txt"])

    def test_the_copy_opens_the_folders_the_scan_deliberately_did_not(self):
        # The other half of the lazy descent. The scan does not look inside a
        # save folder, so the copy has to, and only for the ones ticked.
        asked_before = list(self.lister.asked)
        result = self.run_backup(self.pick("00000001/MY OLD SAVES"))
        opened = [path for path in self.lister.asked[len(asked_before):]]
        self.assertEqual(opened,
                         [f"{HOME}/00000001/savedata/MY OLD SAVES/"])
        self.assertTrue(result.ok)
        self.assertEqual(len(result.saves[0].files), 1)

    def test_a_console_that_dies_opening_a_folder_stops_and_keeps_the_rest(self):
        # Not the same as a folder being refused. Refused is about one folder;
        # gone is about every folder after it, so the copy stops rather than
        # asking a hundred more times.
        lister = FakeLister(dies_at=f"{HOME}/00000001/savedata/MY OLD SAVES")
        chosen = self.pick("00000001/BLES01717-DATA000",
                           "00000001/MY OLD SAVES",
                           "00000001/NPEB02143-AUTOSAVE")
        result = savedata.back_up(lister, chosen, destination=self.folder,
                                  save_reader=Reader())
        self.assertEqual(len(result.saves), 2)
        self.assertEqual(len(result.saves[0].files), 4)
        self.assertIn("stopped answering", result.saves[1].note)
        self.assertTrue(any("stopped answering" in note
                            for note in result.notes))
        with zipfile.ZipFile(result.zip_path) as archive:
            self.assertEqual(archive.testzip(), None)

    def test_a_hostile_name_cannot_escape_the_folder_it_is_written_into(self):
        save = self.report.find("00000001/MY OLD SAVES")
        savedata.read_files(self.lister, save)
        save.files[0].name = "../../escaped.txt"
        save.files[0].path = f"{save.path}/../../escaped.txt"
        result = self.run_backup([save])
        with zipfile.ZipFile(result.zip_path) as archive:
            names = [name for name in archive.namelist()
                     if not name.startswith(("manifest", "read me"))]
        self.assertEqual(len(names), 1)
        stored = names[0]
        self.assertTrue(stored.startswith("00000001/MY OLD SAVES/"))
        leaf = stored.rsplit("/", 1)[1]
        self.assertNotIn("..", leaf)
        self.assertTrue(leaf.endswith("escaped.txt"))


# --- the screen ------------------------------------------------------------

if QT:
    from ps3tools.screens import saves as saves_screen
    from ps3tools.shell.screen import ConnectionState, Services, Theme, \
        THEME_TOKENS

    APP = QApplication.instance() or QApplication([])

    COLOURS = {token: "#%06x" % (0x010203 + index * 0x111111)
               for index, token in enumerate(THEME_TOKENS)}

    class StubTheme(Theme):
        def colour(self, token):
            return COLOURS[token]

        @property
        def dark(self):
            return False


@unittest.skipUnless(QT, "PySide6 not available")
class ScreenCase(FixtureCase):
    def setUp(self):
        self.connection = ConnectionState("127.0.0.1")
        self.services = Services(self.connection, StubTheme(), {})
        self.screen = saves_screen.SavesScreen(self.services)
        # Both seams closed by default. A test that wants one open replaces it
        # deliberately; a test that forgets gets an AssertionError naming this
        # line rather than a packet on the LAN.
        self.screen.lister_factory = Refuses.factory
        self.screen.reader_factory = Refuses.factory
        self.folder = tempfile.mkdtemp(prefix="ps3-saves-screen-")
        self.screen.destination = self.folder
        # A guard test makes a worker raise on purpose. The shell writes a
        # crash log for any worker that dies, which here would drop a file in
        # somebody's home folder for a failure the test arranged itself.
        patch = mock.patch("ps3tools.crashreport.handle")
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._teardown)

    def _teardown(self):
        import shutil
        for task in (self.screen._scan_task, self.screen._copy_task):
            if task is not None:
                task.cancel()
        self.services.wait(30000)
        self.screen.deleteLater()
        APP.processEvents()
        shutil.rmtree(self.folder, ignore_errors=True)

    def pump(self, milliseconds=30000):
        self.services.wait(milliseconds)
        for _ in range(5):
            APP.processEvents()

    def use_fake_console(self):
        self.screen.lister_factory = lambda host: FakeLister()

    def scan(self):
        self.screen.start_scan()
        self.pump()


class Registration(unittest.TestCase):
    @unittest.skipUnless(QT, "PySide6 not available")
    def test_it_registers_itself_after_the_patchers(self):
        from ps3tools.shell import registry
        entry = registry.screen_for("saves")
        self.assertIs(entry, saves_screen.SavesScreen)
        self.assertGreater(entry.order, 30)
        self.assertLess(entry.order, 900)

    @unittest.skipUnless(QT, "PySide6 not available")
    def test_the_tile_is_letters_and_says_nothing_in_an_emoji(self):
        entry = saves_screen.SavesScreen
        self.assertTrue(entry.tile.isalnum())
        self.assertLessEqual(len(entry.tile), 3)
        for text in (entry.title, entry.blurb, entry.tile):
            for character in text:
                self.assertLess(ord(character), 0x2100, text)


class TheReadOnlyPromise(unittest.TestCase):
    """The module and the screen must both stay on the reading side."""

    def test_neither_module_imports_the_write_client(self):
        import ast
        for name in (savedata.__file__,
                     os.path.join(os.path.dirname(savedata.__file__),
                                  "screens", "saves.py")):
            with open(name, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    found = [node.module or ""]
                else:
                    continue
                for item in found:
                    self.assertFalse(item.startswith("ps3tools.patching"),
                                     f"{name} imports {item}")

    def test_the_module_offers_nothing_that_writes_to_a_console(self):
        # Stated as a test so a future "and put them back" arrives as a
        # deliberate change to this file rather than as a quiet new function.
        # The notice constants are allowed to use the word; nothing callable
        # is.
        for name in dir(savedata):
            if not callable(getattr(savedata, name)):
                continue
            for banned in ("restore", "upload", "write_to", "put_back",
                           "delete", "rename"):
                self.assertNotIn(banned, name.lower(), name)


@unittest.skipUnless(QT, "PySide6 not available")
class TheScreenSaysItCannotRestore(ScreenCase):

    def test_the_warning_is_on_the_screen_before_anything_is_scanned(self):
        self.assertIn("cannot put them back",
                      self.screen.warning_heading.text().lower())
        self.assertIn("does not restore them",
                      self.screen.warning_body.text())

    def test_the_card_blurb_says_it_too(self):
        self.assertIn("cannot put them back",
                      saves_screen.SavesScreen.blurb.lower())

    def test_the_warning_is_visible_rather_than_folded_away(self):
        self.assertTrue(self.screen.warning_panel.isVisibleTo(self.screen))
        self.assertTrue(self.screen.warning_body.wordWrap())


@unittest.skipUnless(QT, "PySide6 not available")
class TheGuard(ScreenCase):
    """The seams, and proof that closing them is enough.

    A recent regression made a previously inert path live inside the tests and
    the suite started talking to the console on the LAN. These are the tests
    that would have caught it on the day.
    """

    def test_a_scan_goes_through_the_injected_factory_and_nowhere_else(self):
        # The default factory is replaced with one that raises on sight. If
        # the screen had any other way of reaching a console, the scan would
        # succeed and this would fail.
        self.screen.start_scan()
        self.pump()
        self.assertIsNone(self.screen.survey)
        self.assertIn("Could not read the saves",
                      self.screen.status_label.text())

    def test_a_copy_goes_through_the_injected_factory_and_nowhere_else(self):
        self.use_fake_console()
        self.scan()
        self.screen.set_all(True)
        self.screen.lister_factory = Refuses.factory
        self.screen.start_copy()
        self.pump()
        self.assertIsNone(self.screen.result)
        self.assertIn("could not be copied", self.screen.status_label.text())

    def test_the_real_client_is_never_constructed_during_a_scan(self):
        self.use_fake_console()
        with mock.patch.object(saves_screen, "FtpLister", Refuses):
            self.scan()
        self.assertIsNotNone(self.screen.survey)

    def test_the_real_client_is_never_constructed_during_a_copy(self):
        self.use_fake_console()
        self.scan()
        self.screen.set_all(True)
        self.screen.reader_factory = lambda lister: Reader()
        with mock.patch.object(saves_screen, "FtpLister", Refuses):
            self.screen.start_copy()
            self.pump()
        self.assertTrue(self.screen.result.ok)

    def test_the_default_factory_is_the_only_thing_that_builds_a_client(self):
        # Belt to the braces above: the default really does construct the real
        # client, so replacing it in a test is replacing something live.
        with mock.patch.object(saves_screen, "FtpLister", Refuses):
            with self.assertRaises(AssertionError):
                saves_screen.make_lister("127.0.0.1")

    def test_the_default_reader_uses_the_transports_save_permission(self):
        # This asserted the opposite while the read client had no save-file
        # permission. It now has one: download_save_bytes, its own allowlist
        # and its own size cap, kept apart from the 64 KB PARAM.SFO read
        # because the two answer different questions.
        lister = FtpLister("127.0.0.1")
        self.assertTrue(hasattr(lister, "download_save_bytes"))
        reader = saves_screen.save_reader_for(lister)
        self.assertIsNotNone(reader)

    def test_the_reader_refuses_anything_that_is_not_a_save(self):
        from ps3diag.transport import UnsafeRequest
        reader = saves_screen.save_reader_for(FtpLister("127.0.0.1"))
        for path in ("/dev_hdd0/game/BLES01717/USRDIR/EBOOT.BIN",
                     "/dev_hdd0/home/00000001/savedata/../../game/x.bin",
                     "/dev_hdd0/home/00000001/savedata/a/b/c.bin"):
            with self.assertRaises(UnsafeRequest, msg=path):
                reader(path)


@unittest.skipUnless(QT, "PySide6 not available")
class TheScreenLists(ScreenCase):

    def setUp(self):
        super().setUp()
        self.use_fake_console()

    def test_it_will_not_scan_without_an_address(self):
        self.connection.set_host("")
        self.screen.lister_factory = Refuses.factory
        self.screen.start_scan()
        self.pump()
        self.assertIn("address", self.screen.status_label.text())

    def test_a_scan_puts_a_row_under_each_user(self):
        self.scan()
        tree = self.screen.tree
        self.assertEqual(tree.topLevelItemCount(), 3)
        self.assertEqual(tree.topLevelItem(0).text(0), "User 00000001")
        labels = [tree.topLevelItem(0).child(index).text(0)
                  for index in range(tree.topLevelItem(0).childCount())]
        self.assertTrue(any("Black Ops" in text for text in labels))
        self.assertTrue(any("Persona 5" in text for text in labels))

    def test_a_row_always_shows_the_folder_the_console_calls_it(self):
        self.scan()
        parent = self.screen.tree.topLevelItem(0)
        named = [parent.child(index).text(0)
                 for index in range(parent.childCount())
                 if "Black Ops" in parent.child(index).text(0)][0]
        self.assertIn("BLES01717-DATA000", named)

    def test_a_folder_with_no_recognisable_game_says_so_in_plain_words(self):
        self.scan()
        parent = self.screen.tree.topLevelItem(0)
        labels = [parent.child(index).text(0)
                  for index in range(parent.childCount())]
        self.assertTrue(any("does not name a game" in text
                            for text in labels), labels)

    def test_a_user_with_no_saves_shows_the_reason_rather_than_nothing(self):
        self.scan()
        parent = self.screen.tree.topLevelItem(1)
        self.assertEqual(parent.childCount(), 1)
        self.assertIn("no savedata folder", parent.child(0).text(0))

    def test_a_console_with_no_home_folder_leaves_a_readable_message(self):
        self.screen.lister_factory = lambda host: FakeLister(listings={})
        self.scan()
        self.assertEqual(self.screen.tree.topLevelItemCount(), 0)
        self.assertIn(savedata.NO_HOME, self.screen.notes_label.text())


@unittest.skipUnless(QT, "PySide6 not available")
class TheScreenNeverCallsAFailedScanEmpty(ScreenCase):
    """Bug 2. The summary is the sentence a user acts on.

    The detail underneath already said "the console stopped answering while
    this user's saves were being read". The line above it said "No saves were
    found on this console", and that is the one somebody reads before deciding
    the console is empty.
    """

    def scan_with(self, **kwargs):
        self.screen.lister_factory = lambda host: FakeLister(**kwargs)
        self.scan()
        return self.screen.survey

    def test_a_scan_that_broke_off_with_nothing_found_says_exactly_that(self):
        report = self.scan_with(dies_at=HOME)
        self.assertFalse(report.complete)
        self.assertEqual(self.screen.status_label.text(),
                         saves_screen.DID_NOT_FINISH)
        self.assertIn("does not mean there are no saves",
                      self.screen.status_label.text())

    def test_a_scan_that_broke_off_part_way_says_there_may_be_more(self):
        report = self.scan_with(dies_at=f"{HOME}/00000003")
        self.assertTrue(report.save_count)
        self.assertFalse(report.complete)
        self.assertIn("did not finish", self.screen.status_label.text())
        self.assertIn(str(report.save_count), self.screen.status_label.text())

    def test_a_clean_scan_that_found_nothing_is_still_allowed_to_say_so(self):
        self.scan_with(listings={f"{HOME}/": listing("list_home.txt")})
        self.assertEqual(self.screen.status_label.text(),
                         saves_screen.NOTHING_FOUND)

    def test_a_scan_that_found_saves_and_finished_says_how_many(self):
        self.scan_with()
        self.assertIn("Found 5 save folder(s)",
                      self.screen.status_label.text())

    def test_no_saves_were_found_can_never_be_said_after_any_error(self):
        # The rule, stated once and checked against every shape of broken scan
        # this module knows how to produce. A failed read is not an empty
        # result and must never be summarised as one.
        broken = [
            {"dies_at": HOME},
            {"dies_at": f"{HOME}/00000001"},
            {"dies_at": f"{HOME}/00000003"},
            {"listings": {f"{HOME}/": "not an FTP listing at all\n"}},
        ]
        for kwargs in broken:
            with self.subTest(**kwargs):
                report = self.scan_with(**kwargs)
                self.assertTrue(report.errors, "expected a broken scan")
                said = "\n".join([self.screen.status_label.text(),
                                  self.screen.notes_label.text()])
                self.assertNotIn("no saves were found", said.lower())

    def test_the_reasons_are_still_shown_underneath_the_summary(self):
        # The summary changing must not cost the detail that was already
        # right. Both are on screen.
        report = self.scan_with(dies_at=HOME)
        for reason in report.errors:
            self.assertIn(reason, self.screen.notes_label.text())


@unittest.skipUnless(QT, "PySide6 not available")
class TheScreenDoesNotInventSizes(ScreenCase):
    """The other half of bug 1 as the user sees it.

    The scan no longer opens save folders, so it does not know how big they
    are. A column reading "0 bytes" would say the save is empty, which is a
    different and false statement.
    """

    def setUp(self):
        super().setUp()
        self.use_fake_console()
        self.scan()

    def test_a_folder_nobody_has_opened_says_the_size_is_not_known(self):
        parent = self.screen.tree.topLevelItem(0)
        for index in range(parent.childCount()):
            child = parent.child(index)
            if child.data(0, Qt.UserRole) is None:
                continue
            self.assertEqual(child.text(1), saves_screen.SIZE_UNKNOWN)
        self.assertEqual(parent.text(1), saves_screen.SIZE_UNKNOWN)

    def test_no_row_ever_claims_zero_bytes_before_the_copy(self):
        parent = self.screen.tree.topLevelItem(0)
        for index in range(parent.childCount()):
            self.assertNotIn("0 bytes", parent.child(index).text(1))

    def test_the_waiting_row_does_not_promise_a_file_count_either(self):
        self.screen.reader_factory = lambda lister: Reader()
        self.screen.set_all(True)
        self.screen._build_progress(self.screen.selection())
        for row in self.screen._rows.values():
            self.assertEqual(row.detail_label.text(), "Waiting.")

    def test_a_size_appears_once_the_copy_has_opened_the_folder(self):
        self.screen.reader_factory = lambda lister: Reader()
        parent = self.screen.tree.topLevelItem(0)
        for index in range(parent.childCount()):
            child = parent.child(index)
            if child.data(0, Qt.UserRole) == "00000001/MY OLD SAVES":
                child.setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.screen.start_copy()
        self.pump()
        row = self.screen._rows["00000001/MY OLD SAVES"]
        self.assertIn("Copied 1 file(s)", row.detail_label.text())


@unittest.skipUnless(QT, "PySide6 not available")
class TheScreenPicks(ScreenCase):

    def setUp(self):
        super().setUp()
        self.use_fake_console()
        self.scan()

    def test_nothing_is_ticked_to_start_with_and_copy_is_refused(self):
        self.assertEqual(self.screen.selected_keys(), [])
        self.assertFalse(self.screen.copy_button.isEnabled())

    def test_ticking_everything_takes_every_save_on_the_console(self):
        self.screen.set_all(True)
        self.assertEqual(len(self.screen.selection()),
                         self.screen.survey.save_count)
        self.assertTrue(self.screen.copy_button.isEnabled())

    def test_ticking_one_user_takes_only_that_users_saves(self):
        parent = self.screen.tree.topLevelItem(0)
        parent.setCheckState(0, Qt.Checked)
        APP.processEvents()
        keys = self.screen.selected_keys()
        self.assertTrue(keys)
        self.assertTrue(all(key.startswith("00000001/") for key in keys))

    def test_ticking_one_save_takes_exactly_that_one(self):
        parent = self.screen.tree.topLevelItem(0)
        parent.child(0).setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.assertEqual(len(self.screen.selected_keys()), 1)

    def test_ticking_nothing_puts_it_back_and_refuses_the_copy(self):
        self.screen.set_all(True)
        self.screen.set_all(False)
        self.assertEqual(self.screen.selected_keys(), [])
        self.assertFalse(self.screen.copy_button.isEnabled())

    def test_pressing_copy_with_nothing_ticked_says_so_and_does_nothing(self):
        self.screen.start_copy()
        self.pump()
        self.assertIsNone(self.screen.result)
        self.assertEqual(self.screen.status_label.text(),
                         savedata.NOTHING_PICKED)


@unittest.skipUnless(QT, "PySide6 not available")
class TheScreenCopies(ScreenCase):

    def setUp(self):
        super().setUp()
        self.use_fake_console()
        self.screen.reader_factory = lambda lister: Reader()
        self.scan()

    def select(self, key):
        parent = self.screen.tree.topLevelItem(0)
        for index in range(parent.childCount()):
            child = parent.child(index)
            if child.data(0, Qt.UserRole) == key:
                child.setCheckState(0, Qt.Checked)
        APP.processEvents()

    def test_a_copy_writes_a_zip_and_says_where_it_went(self):
        self.select("00000001/BLES01717-DATA000")
        self.screen.start_copy()
        self.pump()
        result = self.screen.result
        self.assertTrue(result.ok)
        self.assertTrue(os.path.isfile(result.zip_path))
        self.assertIn(result.zip_path, self.screen.result_label.text())
        self.assertTrue(self.screen.open_button.isVisible()
                        or self.screen.open_button.isVisibleTo(self.screen))

    def test_the_result_repeats_that_these_cannot_go_back_on_a_console(self):
        self.select("00000001/BLES01717-DATA000")
        self.screen.start_copy()
        self.pump()
        self.assertIn("cannot be copied back",
                      self.screen.result_label.text())

    def test_each_save_folder_gets_its_own_bar(self):
        self.select("00000001/BLES01717-DATA000")
        self.select("00000001/MY OLD SAVES")
        self.screen.start_copy()
        self.pump()
        self.assertEqual(sorted(self.screen._rows),
                         ["00000001/BLES01717-DATA000",
                          "00000001/MY OLD SAVES"])
        for row in self.screen._rows.values():
            self.assertEqual(row.bar.value(), 100)
            self.assertIn("Copied", row.detail_label.text())

    def test_a_save_that_could_not_be_read_says_so_on_its_own_row(self):
        self.select("00000001/BLES01428-DATA000")
        self.screen.start_copy()
        self.pump()
        row = self.screen._rows["00000001/BLES01428-DATA000"]
        self.assertIn("refused", row.detail_label.text())

    def test_a_connection_without_a_save_reader_copies_nothing_and_says_so(self):
        self.screen.reader_factory = lambda lister: None
        self.select("00000001/MY OLD SAVES")
        self.screen.start_copy()
        self.pump()
        self.assertFalse(self.screen.result.ok)
        self.assertIn(savedata.NO_READER, self.screen.result_label.text())

    def test_stop_is_offered_only_while_a_copy_is_running(self):
        self.assertFalse(self.screen.stop_button.isVisibleTo(self.screen))
        self.select("00000001/BLES01717-DATA000")
        self.screen.start_copy()
        # Pressing Stop before the worker has finished is the case that
        # matters; whichever way the race falls, nothing raises and the button
        # is put away again.
        self.screen.stop_copy()
        self.pump()
        self.assertFalse(self.screen.stop_button.isVisibleTo(self.screen))
        self.assertIsNotNone(self.screen.result)

    def test_stop_does_nothing_when_no_copy_is_running(self):
        self.screen.stop_copy()
        self.assertIsNone(self.screen.result)

    def test_the_screen_refuses_to_be_left_while_it_is_copying(self):
        # can_leave is only False while a copy is in flight, and the reason
        # given to the shell is about this screen rather than the shell's
        # default shrug.
        self.assertTrue(self.screen.can_leave())
        self.screen._copy_task = object()
        try:
            self.assertFalse(self.screen.can_leave())
            self.assertIn("still being copied",
                          self.screen.leave_blocked_reason())
        finally:
            self.screen._copy_task = None


@unittest.skipUnless(QT, "PySide6 not available")
class AgainstTheLoopbackMock(ScreenCase):
    """The real transport, against a server this test starts on 127.0.0.1.

    Everything above uses a lister with no socket in it. This one proves the
    survey works through ps3diag.transport.FtpLister as well, because the
    listing path is the half of this feature the transport already permits.
    """

    def setUp(self):
        super().setUp()
        self.server = MockWebmanFtp(listings={
            path: text.encode("utf-8")
            for path, text in LISTINGS.items()}).start()
        self.addCleanup(self.server.stop)
        host = f"127.0.0.1:{self.server.port}"

        def factory(_host):
            lister = FtpLister("127.0.0.1")
            lister._factory = self._connect
            return lister

        self.screen.lister_factory = factory
        self.connection.set_host("127.0.0.1")
        self.host = host

    def _connect(self):
        ftp = ftplib.FTP(encoding="latin-1")
        ftp.connect("127.0.0.1", self.server.port, timeout=10)
        ftp.login("anonymous", "anonymous@")
        return ftp

    def test_the_survey_works_through_the_real_read_client(self):
        self.scan()
        self.assertIsNotNone(self.screen.survey)
        self.assertEqual(self.screen.survey.save_count, 5)
        self.assertTrue(any("Black Ops" in save.display_name
                            for save in self.screen.survey.saves))

    def test_the_read_client_still_refuses_to_fetch_a_save_file(self):
        # Until the transport grows a save permission of its own, this is what
        # a save path gets, and it is why the reader is injected.
        from ps3diag.transport import UnsafeRequest
        lister = FtpLister("127.0.0.1")
        with self.assertRaises(UnsafeRequest):
            lister.download_bytes(
                f"{HOME}/00000001/savedata/BLES01717-DATA000/PARAM.SFO")


if __name__ == "__main__":
    unittest.main()
