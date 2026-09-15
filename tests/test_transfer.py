"""The transfer games card: images onto the console, and everything said out loud.

Four things are load bearing here and all four are tested rather than assumed,
because all four are failures that happened on a real console with webMAN's own
copy and cost an evening each.

The platform comes out of the image and never off the filename. There is a
fixture whose name says PS3ISO and whose bytes are a PS2 disc, and it must end
up in PS2ISO.

The ampersand. A file called "Gran Turismo 5 & Prologue, Collector's Edition
[BCES00569] (Europe).iso" was skipped silently by the real thing. Here it gets
a name that is shown to the user before anything starts.

Resume. A transfer that dies at 90% of a 36 GB file has to carry on rather
than start again, and cancelling mid-file has to leave something the next run
recognises as a partial rather than as a finished game. That is exercised
against the FTP mock as well as against a fake, so REST before STOR is proven
against something that speaks the protocol.

And nothing here touches the network. Every client is injected, and the last
class in this file replaces the real ones with something that raises the
moment it is constructed and then runs the whole flow through the screen. This
is the third time in this project that a new feature has quietly made a dead
code path live inside the suite and pointed it at a real address on this
network; a seam alone has not been enough before.
"""

import ftplib
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock as mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys                                                      # noqa: E402

from support import fixture_path                                # noqa: E402

_GENERATORS = fixture_path("transfer")
if _GENERATORS not in sys.path:
    sys.path.insert(0, _GENERATORS)

import make_transfer                                            # noqa: E402
from mock_webman import MockWebmanFtp                           # noqa: E402

from PySide6.QtCore import Qt                                   # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

from ps3diag import parsers                                     # noqa: E402
from ps3diag import transport as real_transport                 # noqa: E402
from ps3tools import transfer                                   # noqa: E402
from ps3tools.patching import ftpwrite as real_ftpwrite         # noqa: E402
from ps3tools.patching.ftpwrite import FtpWriter                # noqa: E402
from ps3tools.screens import transfer as transfer_screen        # noqa: E402
from ps3tools.shell.screen import (ConnectionState, Services,   # noqa: E402
                                   THEME_TOKENS, Theme)

APP = QApplication.instance() or QApplication([])

COLOURS = {token: "#%06x" % (0x010203 + index * 0x111111)
           for index, token in enumerate(THEME_TOKENS)}

PS3_DIR = "/dev_hdd0/PS3ISO"
PS2_DIR = "/dev_hdd0/PS2ISO"


class StubTheme(Theme):
    def colour(self, token):
        return COLOURS[token]

    @property
    def dark(self):
        return False


class NetworkAttempted(BaseException):
    """Deliberately not an Exception.

    The worker wraps every Exception into a message for the user, which is
    right for a console that has been switched off and wrong for this: an
    assertion that should have been unreachable must not be catchable by the
    code it is watching.
    """


class ExplodingClient:
    """Raises before it exists. Stands where the real FTP clients stand."""

    def __init__(self, *args, **kwargs):
        raise NetworkAttempted(
            "a real network client was constructed; nothing in this suite may "
            "reach anything but the loopback mock")


def listing_text(entries):
    """A Unix LIST as webMANftpd sends one. (name, size) or (name, size, dir).

    Every real listing starts with "." and "..", so every listing here does
    too. They were missing, and the screen counted them: somebody with a
    handful of games was told there were 30 items on their console.
    """
    lines = ["drwxrwxrwx   1 root  root             0 Jan  1 00:00 .",
             "drwxrwxrwx   1 root  root             0 Jan  1 00:00 .."]
    for entry in entries:
        name, size = entry[0], entry[1]
        directory = len(entry) > 2 and entry[2]
        kind = "d" if directory else "-"
        lines.append(f"{kind}rw-rw-rw-   1 root  root  {size:>12} "
                     f"Jan  1 00:00 {name}")
    return "\n".join(lines)


class FakeLister:
    """Stands in for transport.FtpLister. Reads a dict; opens no socket."""

    def __init__(self, listings=None):
        self.listings = dict(listings or {})
        self.asked = []

    def list_dir(self, path):
        self.asked.append(path)
        if path not in self.listings:
            raise ftplib.error_perm("550 no such directory")
        return listing_text(self.listings[path])

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class WriterBackedLister(FakeLister):
    """A lister that reports whatever the writer actually holds.

    The screen lists the console again after a run and again when the button
    is pressed, and a fake that kept answering with what was there beforehand
    would make those reads prove the opposite of what just happened. This one
    is the same console the writer wrote to.
    """

    def __init__(self, writer, folders=(PS3_DIR, PS2_DIR)):
        FakeLister.__init__(self)
        self.writer = writer
        self.folders = tuple(folders)

    def list_dir(self, path):
        self.asked.append(path)
        if path not in self.folders:
            raise ftplib.error_perm("550 no such directory")
        return listing_text(
            [(os.path.basename(name), len(body))
             for name, body in sorted(self.writer.files.items())
             if os.path.dirname(name) == path])


class ConsoleGone(Exception):
    """The console stopped answering partway through."""


class UnreachableLister(FakeLister):
    """A console that has stopped answering, from the moment it is opened.

    console_games() treats a folder it cannot list as a folder it knows
    nothing about, which is right for a console with no PSPISO. A console that
    has gone is a different thing, so this fails where that one does: at the
    connection.
    """

    def __enter__(self):
        raise ConsoleGone("the console stopped answering")


class FakeWriter:
    """Stands in for FtpWriter, with a real resume and a real stop check.

    Chunked deliberately: cancelling between files would pass a test that the
    requirement fails, so the fake has to stop where the real one stops, which
    is between blocks.
    """

    def __init__(self, files=None, folders=(PS3_DIR, PS2_DIR), chunk=4096,
                 fail_after=None, refuse_listing=(), refuse_mkd=False):
        self.files = dict(files or {})
        self.folders = set(folders)
        self.chunk = chunk
        self.fail_after = fail_after
        self.refuse_listing = set(refuse_listing)
        self.refuse_mkd = refuse_mkd
        self.made = []
        self.listed = []
        self.stores = []
        self.closed = False

    # -- reading

    def list_dir(self, path):
        self.listed.append(path)
        if path in self.refuse_listing or path not in self.folders:
            raise ftplib.error_perm("550 no such directory")
        entries = [(os.path.basename(name), len(body))
                   for name, body in sorted(self.files.items())
                   if os.path.dirname(name) == path]
        return listing_text(entries)

    def size(self, path):
        body = self.files.get(path)
        return len(body) if body is not None else None

    def make_dir(self, path):
        self.made.append(path)
        if self.refuse_mkd:
            raise ftplib.error_perm("550 refused")
        self.folders.add(path)
        return path

    # -- writing

    def store_resumable(self, source, path, on_block=None,
                        should_continue=None, resume_from=None,
                        blocksize=None):
        with open(source, "rb") as handle:
            data = handle.read()
        existing = self.files.get(path, b"")
        offset = len(existing) if resume_from is None else resume_from
        if offset < 0 or offset > len(data):
            offset = 0
        body = bytearray(existing[:offset])
        position = offset
        sent = 0
        complete = True
        while position < len(data):
            if should_continue is not None and not should_continue():
                complete = False
                break
            if self.fail_after is not None and sent >= self.fail_after:
                self.files[path] = bytes(body)
                raise ConsoleGone("the console stopped answering")
            block = data[position:position + self.chunk]
            body += block
            position += len(block)
            sent += len(block)
            if on_block:
                on_block(position, len(data))
        self.files[path] = bytes(body)
        self.stores.append((path, offset, sent, complete))
        return {"bytes": len(body), "sent": sent, "offset": offset,
                "complete": complete}

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class ImageCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ps3-transfer-test-")
        self.addCleanup(lambda: shutil.rmtree(self.folder,
                                              ignore_errors=True))

    def ps3(self, name="game.iso", pad_to=256 * 1024):
        return make_transfer.ps3(self.folder, name, pad_to=pad_to)

    def ps2(self, name="ps2.iso", pad_to=200 * 1024):
        return make_transfer.ps2(self.folder, name, pad_to=pad_to)

    @staticmethod
    def size(path):
        """The real size. The fixture builder has a floor of its own, so a
        test that assumed its pad_to was the answer would be asserting against
        a number that is not the file's."""
        return os.path.getsize(path)


# --- the name it will have ---------------------------------------------------

class NamingTheFile(unittest.TestCase):
    def test_the_ampersand_that_broke_the_real_transfer_is_removed(self):
        name = transfer.remote_name(make_transfer.AWKWARD_NAME)
        self.assertNotIn("&", name)

    def test_commas_and_brackets_go_too(self):
        name = transfer.remote_name(make_transfer.AWKWARD_NAME)
        for character in (",", "[", "]", "(", ")", "'"):
            self.assertNotIn(character, name, name)

    def test_the_bracketed_parts_are_dropped_whole(self):
        name = transfer.remote_name("Gran Turismo 5 [BCES00569] (Europe).iso")
        self.assertEqual(name, "Gran Turismo 5.iso")

    def test_a_long_name_is_shortened_at_a_word(self):
        long_name = ("The Elder Scrolls Four Oblivion Game of the Year "
                     "Edition Special Extended.iso")
        name = transfer.remote_name(long_name)
        self.assertLessEqual(len(name), transfer.MAX_STEM + len(".iso"))
        self.assertFalse(name.startswith(" "))
        # Cut between words, not through one.
        self.assertTrue(long_name.startswith(os.path.splitext(name)[0]))

    def test_the_extension_survives_and_is_lower_case(self):
        self.assertTrue(transfer.remote_name("GAME.ISO").endswith(".iso"))

    def test_accents_are_folded_rather_than_dropped(self):
        self.assertEqual(transfer.remote_name("Pokémon Rumble.iso"),
                         "Pokemon Rumble.iso")

    def test_a_name_made_only_of_punctuation_falls_back_to_the_title_id(self):
        name = transfer.remote_name("[BCES00569].iso", title_id="BCES00569")
        self.assertEqual(name, "BCES00569.iso")

    def test_two_files_that_shorten_the_same_do_not_collide(self):
        items = [transfer.QueueItem(filename=name, platform=transfer.PS3,
                                    name=transfer.remote_name(name))
                 for name in make_transfer.COLLIDING_NAMES]
        transfer.assign_names(items)
        self.assertNotEqual(items[0].name, items[1].name)
        self.assertEqual(items[0].name, "Metal Gear Solid 4.iso")
        self.assertEqual(items[1].name, "Metal Gear Solid 4 2.iso")

    def test_a_name_already_on_the_console_is_stepped_around(self):
        item = transfer.QueueItem(filename="Game.iso", platform=transfer.PS3,
                                  name="Game.iso")
        transfer.assign_names([item], existing=["game.iso"])
        self.assertEqual(item.name, "Game 2.iso")


# --- what the image actually is ---------------------------------------------

class ReadingTheImage(ImageCase):
    def test_a_ps3_image_is_identified_from_its_param_sfo(self):
        item = transfer.inspect(self.ps3())
        self.assertEqual(item.platform, transfer.PS3)
        self.assertEqual(item.title_id, "BLES01428")
        self.assertEqual(item.destination, PS3_DIR)

    def test_a_ps2_image_is_identified_from_its_system_cnf(self):
        item = transfer.inspect(self.ps2())
        self.assertEqual(item.platform, transfer.PS2)
        self.assertEqual(item.title_id, "SLUS21782")
        self.assertEqual(item.destination, PS2_DIR)

    def test_the_filename_is_never_believed(self):
        # The exact fault: webMAN made a PS3ISO folder inside PS2ISO because it
        # read the name. This file says PS3ISO and is a PS2 disc.
        path = make_transfer.ps2(self.folder, make_transfer.LYING_NAME)
        item = transfer.inspect(path)
        self.assertEqual(item.platform, transfer.PS2)
        self.assertEqual(item.destination, PS2_DIR)

    def test_a_udf_only_image_is_identified_too(self):
        path = make_transfer.ps3_udf(self.folder)
        self.assertEqual(transfer.inspect(path).platform, transfer.PS3)

    def test_something_that_is_not_an_image_is_reported_not_guessed(self):
        item = transfer.inspect(make_transfer.not_an_image(self.folder))
        self.assertFalse(item.identified)
        self.assertTrue(item.reason)
        self.assertEqual(item.destination, "")

    def test_a_truncated_image_says_where_it_stopped(self):
        item = transfer.inspect(make_transfer.truncated(self.folder))
        self.assertFalse(item.identified)
        self.assertIn("image", item.reason.lower())

    def test_a_file_that_is_not_there_is_not_a_crash(self):
        item = transfer.inspect(os.path.join(self.folder, "gone.iso"))
        self.assertFalse(item.identified)
        self.assertTrue(item.reason)

    def test_the_root_marker_places_a_ps3_disc_with_an_unreadable_sfo(self):
        # PS3_DISC.SFB at the root is enough to know which folder it belongs
        # in even when the part carrying the name and ID cannot be read.
        path = self.ps3()
        with mock.patch.object(transfer.isoid, "identify",
                               return_value=transfer.isoid.IsoIdentity(
                                   reason="PARAM.SFO carries no TITLE_ID")):
            with mock.patch.object(transfer, "_has_root_file",
                                   return_value=True):
                platform, identity = transfer.identify_image(path)
        self.assertEqual(platform, transfer.PS3)
        self.assertIn("could not be read", identity.reason)


# --- the queue the user is shown --------------------------------------------

class TheQueue(ImageCase):
    def test_the_queue_says_what_each_one_is_and_where_it_is_going(self):
        items = transfer.build_queue([self.ps3("A Game [BLES01428].iso"),
                                      self.ps2("Another & One.iso")])
        self.assertEqual(items[0].destination, PS3_DIR)
        self.assertEqual(items[1].destination, PS2_DIR)
        self.assertEqual(items[0].name, "A Game.iso")
        self.assertEqual(items[1].name, "Another One.iso")
        self.assertIn("PlayStation 3", items[0].describes_as)
        self.assertIn("PlayStation 2", items[1].describes_as)

    def test_the_size_and_total_are_real_numbers(self):
        items = transfer.build_queue([self.ps3(pad_to=300 * 1024)])
        self.assertEqual(items[0].size, 300 * 1024)
        self.assertEqual(transfer.total_bytes(items), 300 * 1024)

    def test_something_unidentified_is_listed_separately(self):
        items = transfer.build_queue(
            [self.ps3(), make_transfer.not_an_image(self.folder)])
        self.assertEqual(len(transfer.unidentified(items)), 1)
        self.assertEqual(len(transfer.sendable(items)), 1)


# --- what is already on the console -----------------------------------------

class WhatIsAlreadyThere(ImageCase):
    def test_the_game_folders_are_listed_read_only(self):
        lister = FakeLister({PS3_DIR: [("Old Game.iso", 1024)],
                             PS2_DIR: [("Ico.iso", 512)]})
        files, listings = transfer.console_games(lister)
        self.assertEqual([item.name for item in files],
                         ["Ico.iso", "Old Game.iso"])
        self.assertEqual(listings[PS3_DIR], {"old game.iso": 1024})
        # A folder the console does not have is not an error worth a sentence.
        self.assertNotIn("/dev_hdd0/PSPISO", listings)

    def test_a_queued_file_already_there_is_marked_and_left_unticked(self):
        path = self.ps3("A Game.iso", pad_to=8192)
        items = transfer.build_queue([path])
        transfer.match_console(items,
                               {PS3_DIR: {"a game.iso": self.size(path)}})
        self.assertEqual(items[0].present, "same")
        self.assertFalse(items[0].wanted)
        self.assertIn("Already on the console", items[0].plan_text)

    def test_the_match_is_made_on_the_name_it_will_have_not_the_local_one(self):
        # The point of matching after renaming: on this computer it is called
        # "A Game & Friends [BLES01428].iso" and on the console it is not.
        path = self.ps3("A Game & Friends [BLES01428].iso", pad_to=8192)
        items = transfer.build_queue([path])
        transfer.match_console(
            items, {PS3_DIR: {"a game friends.iso": self.size(path)}})
        self.assertEqual(items[0].present, "same")

    def test_a_shorter_file_of_that_name_is_a_partial_not_a_collision(self):
        items = transfer.build_queue([self.ps3("A Game.iso", pad_to=8192)])
        transfer.match_console(items, {PS3_DIR: {"a game.iso": 4096}})
        self.assertEqual(items[0].present, "shorter")
        self.assertTrue(items[0].wanted)
        self.assertIn("carry on", items[0].plan_text)

    def test_a_longer_file_of_that_name_is_a_collision_not_a_partial(self):
        items = transfer.build_queue([self.ps3("A Game.iso", pad_to=8192)])
        transfer.match_console(items, {PS3_DIR: {"a game.iso": 900000}})
        self.assertEqual(items[0].present, "different")
        self.assertIn("different file", items[0].plan_text)

    def test_an_unticked_file_is_not_counted_in_the_estimate(self):
        items = transfer.build_queue([self.ps3(pad_to=8192)])
        items[0].wanted = False
        self.assertEqual(transfer.bytes_to_send(items), 0)
        self.assertEqual(transfer.chosen(items), [])


# --- recognising a game that is already there --------------------------------

class RecognisingWhatIsAlreadyThere(ImageCase):
    """Item 10. A game was copied, and copying it again copied all of it.

    Three things had to line up for that. The list of what is on the console
    was read when the screen was entered and never again, so a game put there
    by the run a minute earlier was not in it. The item that had just finished
    kept its tick, because nothing told the queue what the run had done. And
    the permission to write over the top of a file outlived the file it was
    given about, which disarmed the one check made at the moment of the copy.
    """

    def item(self, size=8192, name="A Game.iso", **kwargs):
        return transfer.QueueItem(path="x", filename=name, size=size,
                                  platform=transfer.PS3, name=name, **kwargs)

    def test_a_finished_transfer_leaves_the_queue_saying_it_is_there_now(self):
        # The reported fault. The run put the file on the console and proved
        # it with a fresh listing; leaving the row ticked and the button live
        # invited the user to spend the hours a second time.
        item = self.item(status=transfer.DONE, wanted=True)
        transfer.settle_after_run([item])
        self.assertEqual(item.present, "same")
        self.assertEqual(item.present_bytes, item.size)
        self.assertFalse(item.wanted)
        self.assertIn("on the console", item.plan_text)

    def test_a_stopped_transfer_leaves_it_marked_as_part_copied(self):
        # A run that stopped leaves a short file under the right name, and the
        # next one has to be offered as carrying on from it.
        item = self.item(status=transfer.PARTIAL, resume_from=3000)
        transfer.settle_after_run([item])
        self.assertEqual(item.present, "shorter")
        self.assertEqual(item.present_bytes, 3000)

    def test_a_tick_survives_the_console_being_read_again(self):
        # The user was shown the row, told the console already had it, and
        # ticked it anyway. Re-reading the console on the way to the copy must
        # not quietly undo that decision.
        item = self.item(present="same", wanted=True, overwrite=True)
        transfer.match_console([item], {PS3_DIR: {"a game.iso": 8192}})
        self.assertTrue(item.wanted)
        self.assertTrue(item.overwrite)
        self.assertIn("copied over again", item.plan_text)

    def test_permission_to_copy_over_the_top_does_not_outlive_its_file(self):
        # This is what made a whole copy possible. The tick authorised writing
        # over one particular file; once the console no longer has that file,
        # keeping the authorisation set would let a later identical file
        # through the last check in plan() without anybody deciding anything.
        item = self.item(present="same", wanted=True, overwrite=True)
        transfer.match_console([item], {PS3_DIR: {}})
        self.assertFalse(item.overwrite)
        transfer.plan([item], {PS3_DIR: {"a game.iso": 8192}})
        self.assertEqual(item.status, transfer.ALREADY)

    def test_the_same_name_at_a_different_size_is_still_offered(self):
        # A partial and a different file share a name with this one and must
        # both still be copyable, each saying which of the two it is.
        part = self.item()
        transfer.match_console([part], {PS3_DIR: {"a game.iso": 3000}})
        self.assertTrue(part.wanted)
        self.assertIn("Part copied already", part.plan_text)
        other = self.item()
        transfer.match_console([other], {PS3_DIR: {"a game.iso": 90000}})
        self.assertTrue(other.wanted)
        self.assertIn("different file", other.plan_text)

    def test_the_notes_name_every_file_the_console_already_has(self):
        # One wording, used by the table, the confirmation and the panel, so
        # that the three cannot come to disagree about the same file.
        same = self.item(name="Same.iso", present="same", wanted=False)
        part = self.item(name="Part.iso", present="shorter", wanted=True)
        other = self.item(name="Other.iso", present="different", wanted=True)
        again = self.item(name="Again.iso", present="same", wanted=True,
                          overwrite=True)
        notes = "\n".join(transfer.console_notes([same, part, other, again]))
        self.assertIn(transfer.ALREADY_LEAD, notes)
        self.assertIn("Same.iso", notes)
        self.assertIn("Part.iso", notes)
        self.assertIn("Other.iso", notes)
        self.assertIn(transfer.OVERWRITE_LEAD, notes)
        self.assertIn("Again.iso", notes)

    def test_a_queue_the_console_has_nothing_of_is_given_no_notes(self):
        self.assertEqual(transfer.console_notes([self.item()]), [])

    def test_a_row_ticked_again_after_the_run_stops_saying_copied(self):
        # Otherwise the column reports the last run while the tick beside it
        # asks for another, and the two say opposite things.
        item = self.item(status=transfer.DONE, present="same", wanted=True,
                         overwrite=True)
        self.assertIn("copied over again", item.plan_text)


# --- free space --------------------------------------------------------------

class FreeSpace(unittest.TestCase):
    def test_a_queue_that_does_not_fit_is_refused_before_anything_starts(self):
        with self.assertRaises(transfer.NotEnoughSpace) as caught:
            transfer.check_space(4 * 1024 ** 3, 40 * 1024 ** 3)
        self.assertIn("not enough room", str(caught.exception))
        self.assertIn("Nothing has been copied", str(caught.exception))

    def test_a_margin_is_left_rather_than_filling_it_to_the_last_byte(self):
        needed = 10 * 1024 ** 3
        self.assertRaises(transfer.NotEnoughSpace, transfer.check_space,
                          needed + 1, needed)
        self.assertIsNone(transfer.check_space(
            needed + transfer.SPACE_MARGIN, needed))

    def test_a_console_that_did_not_say_is_not_treated_as_having_no_room(self):
        self.assertIsNone(transfer.check_space(None, 40 * 1024 ** 3))

    def test_the_figure_comes_from_the_storage_collector(self):
        devices = parsers.parse_storage("dev_hdd0: 120.5GB free of 465.7GB")
        self.assertIsInstance(transfer.free_for(devices), int)

    def test_the_devices_offered_are_the_ones_that_can_hold_a_game(self):
        devices = [{"device": "dev_hdd0"}, {"device": "dev_usb000"},
                   {"device": "dev_flash"}]
        names = transfer.storage_devices(devices)
        self.assertIn("dev_hdd0", names)
        self.assertIn("dev_usb000", names)
        self.assertNotIn("dev_flash", names)


# --- being honest about the time --------------------------------------------

class HonestAboutTheTime(ImageCase):
    def test_hours_are_said_as_hours(self):
        self.assertEqual(transfer.format_duration(3600), "about 1 hour")
        self.assertEqual(transfer.format_duration(45), "less than a minute")
        self.assertEqual(transfer.format_duration(600), "about 10 minutes")

    def test_a_thirteen_hour_job_says_so_before_it_starts(self):
        items = [transfer.QueueItem(platform=transfer.PS3, wanted=True,
                                    size=180 * 1024 ** 3)]
        text = transfer.time_estimate(items)
        self.assertIn("hour", text)
        self.assertIn("180.0 GB", text)

    def test_the_wording_does_not_promise_to_make_it_faster(self):
        self.assertIn("cannot make it faster", transfer.HONEST_SPEED)
        for tip in transfer.TIPS:
            self.assertNotIn("much faster", tip)
        self.assertTrue(any("network cable" in tip for tip in transfer.TIPS))
        self.assertTrue(any("SSD" in tip for tip in transfer.TIPS))


# --- planning against the console -------------------------------------------

class Planning(ImageCase):
    def item(self, size=8192, name="A Game.iso", **kwargs):
        return transfer.QueueItem(path="x", filename=name, size=size,
                                  platform=transfer.PS3, name=name, **kwargs)

    def test_the_same_size_under_the_same_name_is_not_sent_again(self):
        item = self.item(wanted=False)
        transfer.plan([item], {PS3_DIR: {"a game.iso": 8192}})
        self.assertEqual(item.status, transfer.ALREADY)

    def test_a_file_never_checked_against_the_console_is_not_copied_over(self):
        # The scan failed, or the file was added before it finished, so the
        # user was never shown that the console already had it. Finding out at
        # the last moment must not cost them the hours anyway.
        item = self.item(wanted=True)
        transfer.plan([item], {PS3_DIR: {"a game.iso": 8192}})
        self.assertEqual(item.status, transfer.ALREADY)

    def test_a_ticked_file_that_is_already_there_is_copied_over(self):
        item = self.item(wanted=True, overwrite=True)
        transfer.plan([item], {PS3_DIR: {"a game.iso": 8192}})
        self.assertEqual(item.resume_from, 0)
        self.assertNotEqual(item.status, transfer.ALREADY)

    def test_a_shorter_file_is_carried_on_from(self):
        item = self.item()
        transfer.plan([item], {PS3_DIR: {"a game.iso": 3000}})
        self.assertEqual(item.status, transfer.RESUMING)
        self.assertEqual(item.resume_from, 3000)
        self.assertEqual(item.remaining, 8192 - 3000)

    def test_a_longer_file_of_that_name_is_given_a_new_name(self):
        item = self.item()
        report = transfer.plan([item], {PS3_DIR: {"a game.iso": 90000}})
        self.assertEqual(item.name, "A Game 2.iso")
        self.assertEqual(report.renamed, [("A Game.iso", "A Game 2.iso")])


# --- the run itself ----------------------------------------------------------

class RunningIt(ImageCase):
    def run_transfer(self, items, writer, **kwargs):
        job = transfer.Transfer(items, writer, **kwargs)
        self.progress = []
        return job.run(on_progress=self.progress.append)

    def test_the_files_land_in_the_folder_their_contents_chose(self):
        items = transfer.build_queue([self.ps3("A [BLES01428].iso", 8192),
                                      self.ps2("B.iso", 4096)])
        writer = FakeWriter()
        report = self.run_transfer(items, writer)
        self.assertEqual(len(report.sent), 2)
        self.assertIn("/dev_hdd0/PS3ISO/A.iso", writer.files)
        self.assertIn("/dev_hdd0/PS2ISO/B.iso", writer.files)
        self.assertTrue(report.checked)

    def test_the_destination_folders_are_created_first(self):
        items = transfer.build_queue([self.ps3(pad_to=4096)])
        writer = FakeWriter(folders=())
        self.run_transfer(items, writer)
        self.assertIn(PS3_DIR, writer.made)

    def test_progress_is_bytes_a_rate_and_a_file_of_how_many(self):
        path = self.ps3("A.iso", 40960)
        items = transfer.build_queue([path])
        clock = iter([float(tick) for tick in range(0, 400)])
        self.run_transfer(items, FakeWriter(chunk=4096),
                          clock=lambda: next(clock))
        sending = [item for item in self.progress
                   if item.stage == transfer.SENDING]
        self.assertTrue(sending)
        last = sending[-1]
        self.assertEqual((last.index, last.count), (1, 1))
        self.assertEqual(last.file_total, self.size(path))
        self.assertGreater(last.rate, 0)
        self.assertIn("File 1 of 1", last.detail())
        self.assertIn("a second", last.detail())
        self.assertIn("left", last.detail())

    def test_what_arrived_is_checked_against_a_fresh_listing(self):
        items = transfer.build_queue([self.ps3("A.iso", 4096)])
        writer = FakeWriter()
        self.run_transfer(items, writer)
        # Listed once before, once after. The one after is the evidence.
        self.assertGreaterEqual(writer.listed.count(PS3_DIR), 2)

    def test_a_file_that_did_not_land_is_reported_rather_than_claimed(self):
        items = transfer.build_queue([self.ps3("A.iso", 4096)])

        class Vanishing(FakeWriter):
            def store_resumable(self, source, path, **kwargs):
                result = FakeWriter.store_resumable(self, source, path,
                                                    **kwargs)
                # A 226 and nothing in the folder: exactly what a console that
                # ran out of room does.
                self.files.pop(path, None)
                return result

        report = self.run_transfer(items, Vanishing())
        self.assertEqual(len(report.missing), 1)
        self.assertEqual(report.sent, [])
        self.assertIn("does not have this file", report.missing[0].detail)

    def test_an_unidentified_file_is_reported_and_never_sent(self):
        items = transfer.build_queue(
            [make_transfer.not_an_image(self.folder), self.ps3("A.iso", 4096)])
        writer = FakeWriter()
        report = self.run_transfer(items, writer)
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(len(writer.stores), 1)

    def test_an_unticked_file_is_reported_not_quietly_dropped(self):
        items = transfer.build_queue([self.ps3("A.iso", 4096)])
        items[0].wanted = False
        report = self.run_transfer(items, FakeWriter())
        self.assertEqual(len(report.skipped), 1)
        self.assertTrue(report.skipped[0].detail)


# --- resume ------------------------------------------------------------------

class Resuming(ImageCase):
    def test_a_partial_from_a_previous_run_is_carried_on_from(self):
        path = self.ps3("A.iso", 8192)
        whole = self.size(path)
        with open(path, "rb") as handle:
            head = handle.read(3000)
        writer = FakeWriter(files={f"{PS3_DIR}/A.iso": head})
        items = transfer.build_queue([path])
        transfer.match_console(items, {PS3_DIR: {"a.iso": 3000}})
        report = transfer.Transfer(items, writer).run()
        self.assertEqual(writer.stores[0][1], 3000)          # started at 3000
        self.assertEqual(writer.stores[0][2], whole - 3000)  # sent the rest
        self.assertEqual(len(writer.files[f"{PS3_DIR}/A.iso"]), whole)
        self.assertEqual(len(report.sent), 1)

    def test_cancelling_mid_file_leaves_a_partial_and_not_a_finished_game(self):
        path = self.ps3("A.iso", 40960)
        writer = FakeWriter(chunk=4096)
        control = transfer.Controller()
        calls = {"n": 0}

        def should_continue():
            calls["n"] += 1
            if calls["n"] > 3:
                control.cancel()
            return not control.cancelled

        control.should_continue = should_continue
        items = transfer.build_queue([path])
        report = transfer.Transfer(items, writer, control=control).run()
        landed = writer.files[f"{PS3_DIR}/A.iso"]
        self.assertGreater(len(landed), 0)
        self.assertLess(len(landed), self.size(path))
        self.assertEqual(len(report.partial), 1)
        self.assertEqual(report.sent, [])
        self.assertIn("carries on from there", report.partial[0].detail)
        self.assertIn("will not play it", report.partial[0].detail)

    def test_the_partial_left_by_a_cancel_resumes_and_completes(self):
        path = self.ps3("A.iso", 40960)
        writer = FakeWriter(chunk=4096)
        control = transfer.Controller()
        calls = {"n": 0}

        def should_continue():
            calls["n"] += 1
            if calls["n"] > 3:
                control.cancel()
            return not control.cancelled

        control.should_continue = should_continue
        first = transfer.build_queue([path])
        transfer.Transfer(first, writer, control=control).run()
        stopped_at = len(writer.files[f"{PS3_DIR}/A.iso"])

        second = transfer.build_queue([path])
        listings = {PS3_DIR: {"a.iso": stopped_at}}
        transfer.match_console(second, listings)
        self.assertEqual(second[0].present, "shorter")
        report = transfer.Transfer(second, writer).run()
        self.assertEqual(writer.stores[-1][1], stopped_at)
        self.assertEqual(len(report.sent), 1)
        with open(path, "rb") as handle:
            self.assertEqual(writer.files[f"{PS3_DIR}/A.iso"], handle.read())

    def test_a_file_already_there_in_full_costs_no_transfer(self):
        path = self.ps3("A.iso", 8192)
        with open(path, "rb") as handle:
            whole = handle.read()
        writer = FakeWriter(files={f"{PS3_DIR}/A.iso": whole})
        items = transfer.build_queue([path])
        transfer.match_console(items, {PS3_DIR: {"a.iso": len(whole)}})
        report = transfer.Transfer(items, writer).run()
        self.assertEqual(writer.stores, [])
        self.assertEqual(len(report.skipped), 1)

    def test_pause_blocks_and_carries_on(self):
        ticks = []
        control = transfer.Controller(sleep=lambda seconds: ticks.append(
            control.resume()))
        control.pause()
        self.assertTrue(control.should_continue())
        self.assertTrue(ticks)               # it waited rather than returning
        self.assertFalse(control.paused)

    def test_cancel_releases_a_pause(self):
        control = transfer.Controller(sleep=lambda seconds: control.cancel())
        control.pause()
        self.assertFalse(control.should_continue())


# --- the failures that actually happen --------------------------------------

class WhenThingsGoWrong(ImageCase):
    def test_a_file_removed_between_the_queue_and_the_upload(self):
        path = self.ps3("A.iso", 4096)
        items = transfer.build_queue([path, self.ps3("B.iso", 4096)])
        os.remove(path)
        writer = FakeWriter()
        report = transfer.Transfer(items, writer).run()
        self.assertEqual(len(report.failed), 1)
        self.assertIn("no longer on your computer", report.failed[0].detail)

    def test_the_console_disappearing_mid_transfer_leaves_a_resumable_partial(self):
        path = self.ps3("A.iso", 40960)
        writer = FakeWriter(chunk=4096, fail_after=8192)
        items = transfer.build_queue([path])
        report = transfer.Transfer(items, writer).run()
        self.assertEqual(len(report.failed), 1)
        self.assertIn("carries on from where it got to",
                      report.failed[0].detail)
        landed = len(writer.files[f"{PS3_DIR}/A.iso"])
        self.assertEqual(landed, 8192)

        # And the next run finishes it rather than starting again.
        writer.fail_after = None
        again = transfer.build_queue([path])
        transfer.match_console(again, {PS3_DIR: {"a.iso": landed}})
        report = transfer.Transfer(again, writer).run()
        self.assertEqual(writer.stores[-1][1], landed)
        self.assertEqual(len(report.sent), 1)
        with open(path, "rb") as handle:
            self.assertEqual(writer.files[f"{PS3_DIR}/A.iso"], handle.read())

    def test_a_console_that_has_gone_does_not_cost_ten_more_failures(self):
        items = transfer.build_queue([self.ps3("A.iso", 40960),
                                      self.ps3("B.iso", 40960),
                                      self.ps3("C.iso", 40960)])
        writer = FakeWriter(chunk=4096, fail_after=4096)
        report = transfer.Transfer(items, writer).run()
        self.assertEqual(len(report.failed), 1)
        self.assertEqual(len(report.skipped), 2)

    def test_no_room_refuses_before_a_single_byte_is_sent(self):
        items = transfer.build_queue([self.ps3("A.iso", 8192)])
        writer = FakeWriter()
        job = transfer.Transfer(items, writer, free_bytes=1024)
        with self.assertRaises(transfer.NotEnoughSpace):
            job.run()
        self.assertEqual(writer.stores, [])

    def test_a_destination_folder_that_cannot_be_made_or_read_says_so(self):
        items = transfer.build_queue([self.ps3("A.iso", 4096)])
        writer = FakeWriter(folders=(), refuse_mkd=True)
        with self.assertRaises(transfer.TransferError) as caught:
            transfer.Transfer(items, writer).run()
        self.assertIn("could not be read", str(caught.exception))
        self.assertEqual(writer.stores, [])

    def test_a_collision_after_renaming_takes_a_name_of_its_own(self):
        path = self.ps3("A Game [BLES01428].iso", 4096)
        writer = FakeWriter(files={f"{PS3_DIR}/A Game.iso": b"x" * 900000})
        items = transfer.build_queue([path])
        report = transfer.Transfer(items, writer).run()
        self.assertEqual(items[0].name, "A Game 2.iso")
        self.assertEqual(report.renamed, [("A Game.iso", "A Game 2.iso")])
        self.assertEqual(len(writer.files[f"{PS3_DIR}/A Game.iso"]), 900000)
        self.assertIn(f"{PS3_DIR}/A Game 2.iso", writer.files)


# --- REST before STOR, against something that speaks FTP --------------------

def factory_for(server):
    def factory():
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", server.port, timeout=10)
        ftp.login("anonymous", "anonymous@")
        return ftp
    return factory


class ResumeOverRealFtp(ImageCase):
    """The mock speaks REST before STOR, so the mechanism is proven not assumed."""

    def start(self, files=None):
        server = MockWebmanFtp(listings={}, files=files or {}, writable=True)
        server.start()
        self.addCleanup(server.stop)
        return server

    def writer_for(self, server):
        writer = FtpWriter("127.0.0.1", timeout=10,
                           factory=factory_for(server))
        self.addCleanup(writer.close)
        return writer

    def test_a_whole_file_goes_up(self):
        path = self.ps3("A.iso", 20000)
        server = self.start()
        result = self.writer_for(server).store_resumable(
            path, "/dev_hdd0/PS3ISO/A.iso")
        self.assertTrue(result["complete"])
        with open(path, "rb") as handle:
            self.assertEqual(server.files["/dev_hdd0/PS3ISO/A.iso"],
                             handle.read())

    def test_a_partial_is_continued_from_where_it_stopped(self):
        path = self.ps3("A.iso", 20000)
        with open(path, "rb") as handle:
            whole = handle.read()
        server = self.start({"/dev_hdd0/PS3ISO/A.iso": whole[:12000]})
        result = self.writer_for(server).store_resumable(
            path, "/dev_hdd0/PS3ISO/A.iso")
        self.assertEqual(result["offset"], 12000)
        self.assertEqual(result["sent"], len(whole) - 12000)
        self.assertIn("REST 12000", server.commands)
        self.assertEqual(server.files["/dev_hdd0/PS3ISO/A.iso"], whole)

    def test_a_fresh_upload_does_not_bother_with_rest(self):
        server = self.start()
        self.writer_for(server).store_resumable(self.ps3("A.iso", 4096),
                                                "/dev_hdd0/PS3ISO/A.iso")
        self.assertEqual([line for line in server.commands
                          if line.startswith("REST")], [])

    def test_stopping_between_blocks_leaves_a_short_file(self):
        path = self.ps3("A.iso", 40960)
        server = self.start()
        blocks = {"n": 0}

        def should_continue():
            blocks["n"] += 1
            return blocks["n"] <= 2

        result = self.writer_for(server).store_resumable(
            path, "/dev_hdd0/PS3ISO/A.iso", should_continue=should_continue,
            blocksize=4096)
        self.assertFalse(result["complete"])
        landed = server.files["/dev_hdd0/PS3ISO/A.iso"]
        self.assertEqual(len(landed), 8192)
        self.assertLess(len(landed), self.size(path))

    def test_something_longer_than_the_local_file_is_not_appended_to(self):
        path = self.ps3("A.iso", 4096)
        server = self.start({"/dev_hdd0/PS3ISO/A.iso": b"x" * 999999})
        result = self.writer_for(server).store_resumable(
            path, "/dev_hdd0/PS3ISO/A.iso")
        self.assertEqual(result["offset"], 0)
        with open(path, "rb") as handle:
            self.assertEqual(server.files["/dev_hdd0/PS3ISO/A.iso"],
                             handle.read())

    def test_the_existing_methods_still_behave(self):
        # ftpwrite is another agent's module and this addition is additive.
        path = self.ps3("A.iso", 4096)
        server = self.start()
        writer = self.writer_for(server)
        whole = self.size(path)
        self.assertEqual(writer.store(path, "/dev_hdd0/packages/x.pkg"), whole)
        self.assertEqual(writer.size("/dev_hdd0/packages/x.pkg"), whole)


# --- the screen --------------------------------------------------------------

class ScreenCase(ImageCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("ps3tools.crashreport.handle",
                             lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, listings=None, writer=None, devices=None,
              host="127.0.0.1"):
        connection = ConnectionState(host)
        services = Services(connection, StubTheme(), {})
        self.addCleanup(services.wait)
        self.services = services

        self.lister = FakeLister(listings if listings is not None
                                 else {PS3_DIR: [], PS2_DIR: []})
        self.writer = writer if writer is not None else FakeWriter()
        self.devices = devices if devices is not None else [
            {"device": "dev_hdd0", "free_bytes": 400 * 1024 ** 3}]

        screen = transfer_screen.TransferGamesScreen(services)
        screen._lister = lambda host: self.lister
        screen._writer = lambda host: self.writer
        screen._storage = lambda host: self.devices
        screen.confirm = lambda queue: True
        self.addCleanup(screen.deleteLater)
        # deleteLater only queues it. Without a pump the screens pile up and
        # are destroyed at some unpredictable later moment, which is how the
        # shell tests came to take the process down several hundred tests
        # after the one that made the mess.
        self.addCleanup(APP.processEvents)
        self.screen = screen
        return screen

    def settle(self, screen=None):
        """Wait for the workers AND for their results to reach the GUI thread.

        services.wait() only drains the thread pool. A task's finished signal
        is queued to the GUI thread, so a single processEvents() can return
        before the handler has run, and a handler that starts more work -- the
        end of a transfer queues the console being listed again -- leaves a
        second round undelivered. A fixed number of rounds was close enough to
        pass most of the time, which is the worst way for a test to behave.

        Pumping until the pool has stayed idle across three passes is
        deterministic: the handler runs on the first, anything it submits is
        in flight by the second, and the third confirms nothing new arrived.
        The deadline means a genuine hang still fails rather than spinning.
        """
        deadline = time.monotonic() + 10.0
        quiet = 0
        while time.monotonic() < deadline:
            APP.processEvents()
            quiet = quiet + 1 if self.services.wait(50) else 0
            if quiet >= 3:
                return
        raise AssertionError("work did not settle within ten seconds")

    def rows(self):
        table = self.screen._table
        return [table.topLevelItem(index)
                for index in range(table.topLevelItemCount())]

    def console_rows(self):
        table = self.screen._console_table
        return [table.topLevelItem(index)
                for index in range(table.topLevelItemCount())]


class TheScreen(ScreenCase):
    def test_the_queue_is_shown_before_anything_starts(self):
        screen = self.build()
        screen.add_files([self.ps3("A Game & Friends [BLES01428].iso", 8192)])
        row = self.rows()[0]
        self.assertEqual(row.text(0), "A Game & Friends [BLES01428].iso")
        self.assertEqual(row.text(3), PS3_DIR)
        self.assertEqual(row.text(4), "A Game Friends.iso")
        self.assertIn("PlayStation 3", row.text(2))
        self.assertEqual(self.writer.stores, [])

    def test_the_time_it_will_take_is_on_screen_before_the_button(self):
        screen = self.build()
        screen.add_files([self.ps3("A.iso", 8192)])
        self.assertIn("copy", screen._estimate.text())
        self.assertIn("cannot make it faster", screen._notice_text.text())

    def test_the_games_already_on_the_console_are_listed_on_entry(self):
        screen = self.build(listings={PS3_DIR: [("Old Game.iso", 4096)],
                                      PS2_DIR: [("Ico.iso", 2048)]})
        screen.on_enter()
        self.settle(screen)
        names = [row.text(1) for row in self.console_rows()]
        self.assertEqual(sorted(names), ["Ico.iso", "Old Game.iso"])
        self.assertIn("Already on the console",
                      screen._console_heading.text())

    def test_the_dot_entries_are_not_games_and_are_not_counted(self):
        # Reported off hardware: the heading said 30 items when the console
        # had far fewer, because every folder contributes a "." and a ".."
        # and they were being listed with blank sizes.
        screen = self.build(listings={PS3_DIR: [("Old Game.iso", 4096)],
                                      PS2_DIR: [("Ico.iso", 2048)]})
        screen.on_enter()
        self.settle(screen)
        names = [row.text(1) for row in self.console_rows()]
        self.assertNotIn(".", names)
        self.assertNotIn("..", names)
        self.assertIn("2 items", screen._console_heading.text())

    def test_a_file_the_console_already_has_arrives_unticked_and_marked(self):
        path = self.ps3("A Game.iso", 8192)
        screen = self.build(
            listings={PS3_DIR: [("A Game.iso", self.size(path))],
                      PS2_DIR: []})
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path])
        row = self.rows()[0]
        self.assertEqual(row.checkState(0), Qt.Unchecked)
        self.assertIn("Already on the console", row.text(5))

    def test_ticking_it_again_copies_over_the_top(self):
        path = self.ps3("A Game.iso", 8192)
        with open(path, "rb") as handle:
            whole = handle.read()
        writer = FakeWriter(files={f"{PS3_DIR}/A Game.iso": whole})
        screen = self.build(
            listings={PS3_DIR: [("A Game.iso", len(whole))], PS2_DIR: []},
            writer=writer)
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path])
        self.rows()[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.assertIn("copied over again", self.rows()[0].text(5))
        screen.start_run()
        self.settle(screen)
        self.assertEqual(len(writer.stores), 1)
        self.assertEqual(writer.stores[0][1], 0)

    def test_a_part_copied_file_says_it_will_carry_on(self):
        path = self.ps3("A Game.iso", 8192)
        screen = self.build(listings={PS3_DIR: [("A Game.iso", 3000)],
                                      PS2_DIR: []})
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path])
        row = self.rows()[0]
        self.assertEqual(row.checkState(0), Qt.Checked)
        self.assertIn("carry on", row.text(5))

    def test_something_unrecognised_is_named_with_its_reason(self):
        screen = self.build()
        screen.add_files([make_transfer.not_an_image(self.folder)])
        self.assertIn("holiday.iso", screen._detail.text())
        self.assertIn("not recognised", screen._detail.text().lower())
        self.assertFalse(screen._go.isEnabled())

    def test_the_whole_thing_runs_and_reports_what_landed(self):
        screen = self.build()
        screen.on_enter()
        self.settle(screen)
        screen.add_files([self.ps3("A [BLES01428].iso", 8192),
                          self.ps2("B & C.iso", 4096)])
        screen.start_run()
        self.settle(screen)
        self.assertIn(f"{PS3_DIR}/A.iso", self.writer.files)
        self.assertIn(f"{PS2_DIR}/B C.iso", self.writer.files)
        self.assertIn("Finished", screen._panel_heading.text())
        self.assertIn("A.iso", screen._panel_body.text())
        self.assertFalse(screen._working)
        # The console is listed again afterwards, and the report of what
        # happened is still on screen rather than having been wiped by it.
        self.assertGreaterEqual(self.lister.asked.count(PS3_DIR), 2)
        self.assertIn("Finished", screen._panel_heading.text())

    def test_the_free_space_check_refuses_rather_than_filling_the_drive(self):
        screen = self.build(devices=[{"device": "dev_hdd0",
                                      "free_bytes": 1024}])
        screen.add_files([self.ps3("A.iso", 8192)])
        screen.start_run()
        self.settle(screen)
        self.assertEqual(self.writer.stores, [])
        self.assertIn("did not finish", screen._panel_heading.text())
        self.assertIn("not enough room", screen._panel_body.text())

    def test_stopping_mid_file_is_reported_as_resumable(self):
        screen = self.build(writer=FakeWriter(chunk=1024))
        screen.add_files([self.ps3("A.iso", 40960)])
        task = screen.start_run()
        self.assertIsNotNone(task)
        screen._on_stop()
        self.settle(screen)
        self.assertIn("Stopped", screen._panel_heading.text())

    def test_it_will_not_be_navigated_away_from_mid_transfer(self):
        screen = self.build()
        screen._working = True
        self.assertFalse(screen.can_leave())
        self.assertIn("carries on from there", screen.leave_blocked_reason())
        screen._working = False

    def test_no_address_says_where_to_type_one(self):
        screen = self.build(host="")
        screen.on_enter()
        self.assertIn("No console address", screen._panel_heading.text())


# --- item 10: the game that was copied twice ---------------------------------

class TheGameItAlreadyHas(ScreenCase):
    """Reported off hardware. Minecraft was copied, then copied again in full.

    The list of what is on the console was read when the screen was entered
    and at no point after it, so by the time the button was pressed it was
    describing a console that no longer existed. The row said "It will be
    copied", the tick was in it, and the confirmation box did not mention the
    console at all.
    """

    def console_from(self, writer):
        """A lister reading the same console the writer is writing to."""
        self.lister = WriterBackedLister(writer)
        return lambda host: self.lister

    def test_a_game_that_has_just_been_copied_is_not_offered_again(self):
        # The fault itself, end to end: copy it, and then find out whether the
        # screen still invites you to spend the hours a second time.
        path = self.ps3("Minecraft [BLES01976].iso", 8192)
        writer = FakeWriter()
        screen = self.build(writer=writer)
        screen._lister = self.console_from(writer)
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path])
        self.assertIn("It will be copied", self.rows()[0].text(5))
        screen._on_go()
        self.settle(screen)
        self.assertEqual(len(writer.stores), 1)

        row = self.rows()[0]
        self.assertEqual(row.checkState(0), Qt.Unchecked)
        self.assertIn("on the console", row.text(5))
        self.assertFalse(screen._go.isEnabled())
        writer.stores.clear()
        screen._on_go()
        self.settle(screen)
        self.assertEqual(writer.stores, [])

    def test_the_console_is_asked_again_when_the_button_is_pressed(self):
        # A game can arrive between entering the screen and pressing the
        # button: from the run a minute ago, or from another computer. The
        # snapshot taken on entry knows nothing about it.
        path = self.ps3("A Game.iso", 8192)
        screen = self.build(listings={PS3_DIR: [], PS2_DIR: []})
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path])
        self.assertIn("It will be copied", self.rows()[0].text(5))

        size = self.size(path)
        self.lister.listings[PS3_DIR] = [("A Game.iso", size)]
        self.writer.files[f"{PS3_DIR}/A Game.iso"] = b"x" * size
        screen._on_go()
        self.settle(screen)
        self.assertEqual(self.writer.stores, [])
        self.assertIn("already on the console",
                      screen._panel_heading.text().lower())
        self.assertIn("A Game.iso", screen._panel_body.text())
        row = self.rows()[0]
        self.assertEqual(row.checkState(0), Qt.Unchecked)
        self.assertIn("Already on the console", row.text(5))

    def test_the_confirmation_names_what_the_console_already_has(self):
        # It has to be said before the button as well as at the copy, and the
        # box was the one place it was not said: the file is unticked by then
        # and the box was built from the ticked ones only.
        path = self.ps3("A Game.iso", 8192)
        screen = self.build(
            listings={PS3_DIR: [("A Game.iso", self.size(path))],
                      PS2_DIR: []})
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path, self.ps3("B Game.iso", 4096)])
        text = screen.confirm_text(transfer.chosen(screen._items))
        self.assertIn(transfer.ALREADY_LEAD, text)
        self.assertIn("A Game.iso", text)

    def test_a_part_copied_file_is_still_offered_and_says_which_it_is(self):
        # Same name, different size. That is a run that stopped or another
        # dump of the game, and either way it must still be copyable.
        path = self.ps3("A Game.iso", 8192)
        with open(path, "rb") as handle:
            head = handle.read(3000)
        writer = FakeWriter(files={f"{PS3_DIR}/A Game.iso": head})
        screen = self.build(listings={PS3_DIR: [("A Game.iso", 3000)],
                                      PS2_DIR: []}, writer=writer)
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path])
        row = self.rows()[0]
        self.assertEqual(row.checkState(0), Qt.Checked)
        self.assertIn("Part copied already", row.text(5))
        self.assertIn(transfer.PART_LEAD,
                      screen.confirm_text(transfer.chosen(screen._items)))
        screen._on_go()
        self.settle(screen)
        self.assertEqual(len(writer.stores), 1)
        self.assertEqual(writer.stores[0][1], 3000)

    def test_a_console_that_cannot_be_read_again_says_so_in_the_box(self):
        # The check is not always possible. Saying nothing and copying anyway
        # is how the hours get spent, so the box says the check was missed and
        # the run still refuses to send a file that is already there in full.
        path = self.ps3("A Game.iso", 8192)
        with open(path, "rb") as handle:
            whole = handle.read()
        writer = FakeWriter(files={f"{PS3_DIR}/A Game.iso": whole})
        screen = self.build(writer=writer)
        screen.add_files([path])
        seen = []
        screen.confirm = lambda queue: (seen.append(screen.confirm_text(queue))
                                        or True)
        screen._lister = lambda host: UnreachableLister()
        screen._on_go()
        self.settle(screen)
        self.assertTrue(seen)
        self.assertIn(transfer_screen.CHECK_FAILED, seen[0])
        self.assertEqual(writer.stores, [])
        self.assertIn("Already on the console", screen._panel_body.text())

    def test_ticking_it_again_survives_the_check_the_button_makes(self):
        # The user was told and ticked it anyway. The new listing on the way
        # to the copy must not quietly take their decision back off them.
        path = self.ps3("A Game.iso", 8192)
        with open(path, "rb") as handle:
            whole = handle.read()
        writer = FakeWriter(files={f"{PS3_DIR}/A Game.iso": whole})
        screen = self.build(
            listings={PS3_DIR: [("A Game.iso", len(whole))], PS2_DIR: []},
            writer=writer)
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path])
        self.rows()[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        screen._on_go()
        self.settle(screen)
        self.assertEqual(len(writer.stores), 1)
        self.assertEqual(writer.stores[0][1], 0)


# --- the guard ---------------------------------------------------------------

class NothingReachesTheNetwork(ScreenCase):
    """The rule, tested the only way that has held in this project.

    A seam is not enough on its own: the last three times a feature went in,
    the seam existed and something still reached a real address, because a
    code path nobody was looking at built its own client. So the real client
    classes are replaced with one that raises the moment it is constructed,
    and the entire flow is then driven through the screen. If any line
    anywhere in this card builds a real client, this fails at that line rather
    than after a test run has already been out on the network.
    """

    def test_the_whole_flow_runs_with_every_real_client_replaced(self):
        with mock.patch.object(transfer_screen, "FtpWriter", ExplodingClient), \
                mock.patch.object(real_ftpwrite, "FtpWriter",
                                  ExplodingClient), \
                mock.patch.object(real_transport, "FtpLister",
                                  ExplodingClient), \
                mock.patch.object(real_transport, "HttpProbe",
                                  ExplodingClient), \
                mock.patch.object(transfer_screen.transport, "FtpLister",
                                  ExplodingClient), \
                mock.patch.object(transfer_screen.transport, "HttpProbe",
                                  ExplodingClient):
            screen = self.build(listings={PS3_DIR: [("Old.iso", 512)],
                                          PS2_DIR: []})
            screen.on_enter()
            self.settle(screen)
            screen.add_files([self.ps3("A [BLES01428].iso", 8192),
                              self.ps2("B & C.iso", 4096),
                              make_transfer.not_an_image(self.folder)])
            screen.start_run()
            self.settle(screen)
        self.assertIn(f"{PS3_DIR}/A.iso", self.writer.files)
        self.assertIn("Finished", screen._panel_heading.text())

    def test_the_button_itself_reaches_nothing_real_either(self):
        # The button now reads the console before it writes to it, which is a
        # new place for a real client to be built. It goes through the same
        # seam as the scan on entry, and this is what proves it.
        with mock.patch.object(transfer_screen, "FtpWriter",
                               ExplodingClient), \
                mock.patch.object(real_ftpwrite, "FtpWriter",
                                  ExplodingClient), \
                mock.patch.object(real_transport, "FtpLister",
                                  ExplodingClient), \
                mock.patch.object(real_transport, "HttpProbe",
                                  ExplodingClient), \
                mock.patch.object(transfer_screen.transport, "FtpLister",
                                  ExplodingClient), \
                mock.patch.object(transfer_screen.transport, "HttpProbe",
                                  ExplodingClient):
            screen = self.build(listings={PS3_DIR: [("Old.iso", 512)],
                                          PS2_DIR: []})
            screen.on_enter()
            self.settle(screen)
            screen.add_files([self.ps3("A [BLES01428].iso", 8192)])
            screen._on_go()
            self.settle(screen)
        self.assertIn(f"{PS3_DIR}/A.iso", self.writer.files)
        self.assertIn("Finished", screen._panel_heading.text())

    def test_the_domain_has_no_way_to_build_a_client_of_its_own(self):
        # Transfer is handed a writer and has no factory, no host and no
        # import that could produce one.
        with open(transfer.__file__, encoding="utf-8") as handle:
            source = handle.read()
        imported = [line for line in source.splitlines()
                    if line.startswith(("import ", "from "))]
        for name in ("FtpWriter", "FtpLister", "HttpProbe", "ftplib",
                     "socket", "urllib", "http"):
            self.assertFalse([line for line in imported if name in line],
                             f"transfer.py imports {name}")
        self.assertNotIn("ps3tools.patching", source)

    def test_the_exploding_client_really_does_explode(self):
        # Otherwise the test above would pass by being wrong.
        with self.assertRaises(NetworkAttempted):
            ExplodingClient("192.168.1.10")


# --- the wording -------------------------------------------------------------

class WhatTheScreenSays(unittest.TestCase):
    def setUp(self):
        with open(transfer_screen.__file__, encoding="utf-8") as handle:
            self.body = handle.read()
        with open(transfer.__file__, encoding="utf-8") as handle:
            self.domain = handle.read()

    def test_it_does_not_claim_to_speed_anything_up(self):
        for phrase in ("much faster", "speeds up", "faster transfer",
                       "turbo", "accelerate"):
            self.assertNotIn(phrase, self.body + self.domain)

    def test_the_tips_are_on_the_screen(self):
        self.assertIn("TIPS", self.body)
        self.assertIn("network cable", self.domain)

    def test_there_are_no_emoji_anywhere_in_it(self):
        for character in self.body + self.domain:
            self.assertLess(ord(character), 0x2190,
                            f"{character!r} is not plain text")

    def test_it_is_registered_where_the_launcher_will_find_it(self):
        self.assertEqual(transfer_screen.TransferGamesScreen.key, "transfer")
        self.assertEqual(transfer_screen.TransferGamesScreen.order, 60)
        self.assertTrue(transfer_screen.TransferGamesScreen.tile)



class TickingARowDoesNotDeleteItUnderQt(ScreenCase):
    """The redraw a tick causes happens after Qt has finished with the item.

    itemChanged is emitted from inside QTreeWidgetItem::setCheckState. The
    handler used to rebuild the table straight away, and rebuilding clears it,
    which deletes every item including the one that call is still running on.
    Qt then carried on against freed memory. It survived for a long time
    because the block is usually still mapped; under a long test run it
    stopped surviving and took the whole process down with SIGBUS.
    """

    def ticked_screen(self):
        path = self.ps3("A Game.iso", 8192)
        screen = self.build(listings={PS3_DIR: [], PS2_DIR: []})
        screen.on_enter()
        self.settle(screen)
        screen.add_files([path])
        return screen

    def test_the_item_survives_the_call_that_ticked_it(self):
        screen = self.ticked_screen()
        row = self.rows()[0]
        row.setCheckState(0, Qt.Checked)
        # Reading it is the whole test: a deleted item raises RuntimeError
        # from shiboken rather than returning its text.
        self.assertEqual(row.checkState(0), Qt.Checked)
        self.assertTrue(row.text(0))

    def test_the_table_is_redrawn_once_qt_has_finished(self):
        screen = self.ticked_screen()
        self.rows()[0].setCheckState(0, Qt.Checked)
        APP.processEvents()
        self.assertIn("copied", self.rows()[0].text(5).lower())

    def test_the_tick_is_recorded_straight_away(self):
        # Only the redraw is deferred. What the user chose is acted on inside
        # the handler, so nothing depends on the timer having run.
        screen = self.ticked_screen()
        self.rows()[0].setCheckState(0, Qt.Checked)
        self.assertTrue([item for item in screen._items if item.wanted])

    def test_the_redraw_timer_belongs_to_the_screen(self):
        # A bare singleShot outliving the widget is a crash on shutdown, and
        # a crash on shutdown is what this whole area is about.
        screen = self.ticked_screen()
        self.assertIs(screen._redraw.parent(), screen)
        self.assertTrue(screen._redraw.isSingleShot())


if __name__ == "__main__":
    unittest.main()
