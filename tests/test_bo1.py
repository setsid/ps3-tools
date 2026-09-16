"""The Black Ops 1 stats fix: finding its own way around, and the account.

Everything here runs against an image built in this file rather than against a
copy of the game. The point of the fix is that it finds every address it uses
by pattern, so a test that handed it a real binary and checked the addresses
would be checking one build; this builds an image whose landmarks are in known
places, and then moves them, which is the thing that actually has to keep
working.

The real binaries were used to write the patterns and are not in the
repository. What was read off them is recorded in ps3tools/titles.py.
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps3diag import patchstate
from ps3tools import titles
from ps3tools.patching import flow, npcache
from ps3tools.patching.scetool import ScetoolError
from ps3tools.patching.unfself import Unfself, WithFakeSigned

bo1 = patchstate.patcher_module("bo1")


# --- an image with the landmarks in known places ---------------------------

BASE = 0x10000
SIZE = 0x8000
NOP = 0x60000000

#: Where each thing goes, as a file offset. The addresses are this file's
#: choosing and nothing in the fix knows them, which is the whole point.
HOOK = 0x1000
FRIEND = 0x1100
VA_REF = 0x1200
VA = 0x1400
FS_OPEN = 0x1500
FS_READ = 0x1540
FS_CLOSE = 0x1580
GET_USER_ID = 0x2000
LENGTH_SETUP = 0x2020
REVERSAL = 0x2100
FORMAT = 0x3000
NIDS = 0x3100
FUNCS = 0x3110
LIBNAME = 0x3120
STUB = 0x3200
PRX = 0x3300
CAVE = 0x5000
CAVE_SIZE = 0x2000


def _put(data, offset, word):
    struct.pack_into(">I", data, offset, word)


def build_image(reversals=1, cave_size=CAVE_SIZE, imports=True):
    """A small ELF carrying everything the fix looks for.

    reversals draws the byte reversal more than once, which is the shape of an
    image the fix has to refuse rather than pick from.
    """
    data = bytearray(NOP.to_bytes(4, "big") * (SIZE // 4))

    # ELF header
    data[0:16] = b"\x7fELF\x02\x02\x01" + b"\x00" * 9
    struct.pack_into(">HHI", data, 0x10, 2, 0x15, 1)
    struct.pack_into(">Q", data, 0x20, 0x40)               # e_phoff
    # e_ehsize, e_phentsize, e_phnum
    struct.pack_into(">HHH", data, 0x34, 0x40, 0x38, 2)

    def phdr(index, p_type, flags, offset, vaddr, filesz):
        base = 0x40 + index * 0x38
        struct.pack_into(">II", data, base, p_type, flags)
        struct.pack_into(">QQQ", data, base + 0x08, offset, vaddr, vaddr)
        struct.pack_into(">QQ", data, base + 0x20, filesz, filesz)

    phdr(0, 1, 5, 0, BASE, SIZE)                            # loadable, R+X
    phdr(1, 0x60000002, 0, PRX, BASE + PRX, 0x40)           # the PRX param

    # The two calls to getUserID. The first is the hook and the second is the
    # friends list, which must be left exactly as it is.
    _put(data, HOOK, bo1.bl(BASE + HOOK, BASE + GET_USER_ID))
    _put(data, FRIEND, bo1.bl(BASE + FRIEND, BASE + GET_USER_ID))

    # Something builds the address of "%llu" and then tails into va().
    string = BASE + FORMAT
    _put(data, VA_REF, bo1.lis(3, (string >> 16) & 0xFFFF))
    _put(data, VA_REF + 4, bo1.addi(3, 3, string & 0xFFFF))
    _put(data, VA_REF + 12, bo1.b(BASE + VA_REF + 12, BASE + VA))
    _put(data, VA, bo1.stdu(1, 1, -0x80))
    _put(data, VA + 4, bo1.blr())

    for where in (FS_OPEN, FS_READ, FS_CLOSE):
        _put(data, where, bo1.blr())

    # getUserID: a prologue, the length setup with its loads, and the
    # reversal itself.
    _put(data, GET_USER_ID, bo1.stdu(1, 1, -0x120))
    _put(data, LENGTH_SETUP, bo1.li(8, 8))
    for step in range(6):
        _put(data, LENGTH_SETUP + 4 + step * 4,
             (34 << 26) | (9 << 21) | (4 << 16) | step)     # lbz r9, n(r4)
    for copy in range(reversals):
        at = REVERSAL + copy * 0x200
        for step in range(8):
            _put(data, at + step * 8,
                 (34 << 26) | (9 << 21) | (4 << 16) | step)
            _put(data, at + step * 8 + 4,
                 (38 << 26) | (9 << 21) | (31 << 16) | (7 - step))
        if copy:
            # Its own function, or both matches walk back to the same
            # prologue and the pair still agrees on one.
            _put(data, at - 0x40, bo1.stdu(1, 1, -0x80))
            _put(data, at - 0x3C, bo1.li(8, 8))
            for step in range(6):
                _put(data, at - 0x38 + step * 4,
                     (34 << 26) | (9 << 21) | (4 << 16) | step)

    data[FORMAT:FORMAT + 5] = b"%llu\x00"

    if imports:
        for index, fnid in enumerate((bo1.FNID_CELL_FS_OPEN,
                                      bo1.FNID_CELL_FS_READ,
                                      bo1.FNID_CELL_FS_CLOSE)):
            struct.pack_into(">I", data, NIDS + index * 4, fnid)
        for index, where in enumerate((FS_OPEN, FS_READ, FS_CLOSE)):
            struct.pack_into(">I", data, FUNCS + index * 4, BASE + where)
        data[LIBNAME:LIBNAME + 7] = b"sys_fs\x00"
        data[STUB] = 0x2C
        struct.pack_into(">H", data, STUB + 6, 3)
        struct.pack_into(">III", data, STUB + 16,
                         BASE + LIBNAME, BASE + NIDS, BASE + FUNCS)

    struct.pack_into(">II", data, PRX, 0x40, 0x1B434CEC)
    struct.pack_into(">II", data, PRX + 24, BASE + STUB, BASE + STUB + 0x2C)

    # The free space, and the only zeros in the image worth the name.
    data[CAVE:CAVE + cave_size] = b"\x00" * cave_size
    return bytes(data)


IMAGE = build_image()


# --- finding everything ----------------------------------------------------

class FindingTheLandmarks(unittest.TestCase):
    """Nothing is an address. Every one of these is found in the image."""

    def marks(self, data=None):
        return bo1.find_landmarks(bo1.Image(data if data is not None
                                            else IMAGE))

    def test_it_finds_the_function_that_hashes_the_identity(self):
        self.assertEqual(self.marks().get_user_id, BASE + GET_USER_ID)

    def test_the_hook_is_the_first_caller_and_not_the_friends_list(self):
        marks = self.marks()
        self.assertEqual(marks.hook, BASE + HOOK)
        self.assertIn(BASE + FRIEND, marks.callers)

    def test_it_finds_the_format_string_and_the_formatter(self):
        marks = self.marks()
        self.assertEqual(marks.format_string, BASE + FORMAT)
        self.assertEqual(marks.formatter, BASE + VA)

    def test_it_resolves_the_file_calls_out_of_the_import_tables(self):
        marks = self.marks()
        self.assertEqual(marks.fs_open, BASE + FS_OPEN)
        self.assertEqual(marks.fs_read, BASE + FS_READ)
        self.assertEqual(marks.fs_close, BASE + FS_CLOSE)

    def test_it_finds_the_free_space(self):
        marks = self.marks()
        self.assertEqual(marks.cave, BASE + CAVE)
        self.assertEqual(marks.cave_size, CAVE_SIZE)

    def test_moving_everything_moves_the_answers_with_it(self):
        # The reason none of this is written down: the two known builds of
        # this game put the same code in different places. An image with a
        # different layout has to come back with different addresses rather
        # than with the same ones.
        shifted = bytearray(build_image())
        moved = 0x40
        block = shifted[GET_USER_ID:GET_USER_ID + 0x400]
        shifted[GET_USER_ID:GET_USER_ID + 0x400] = \
            NOP.to_bytes(4, "big") * 0x100
        shifted[GET_USER_ID + moved:GET_USER_ID + moved + 0x400] = block
        _put(shifted, HOOK, bo1.bl(BASE + HOOK, BASE + GET_USER_ID + moved))
        _put(shifted, FRIEND,
             bo1.bl(BASE + FRIEND, BASE + GET_USER_ID + moved))
        marks = self.marks(bytes(shifted))
        self.assertEqual(marks.get_user_id, BASE + GET_USER_ID + moved)
        self.assertEqual(marks.hook, BASE + HOOK)


class RefusingRatherThanGuessing(unittest.TestCase):
    """Every landmark has to be found exactly once."""

    def test_two_byte_reversals_are_a_refusal(self):
        with self.assertRaises(bo1.NotThisBuild) as caught:
            bo1.find_landmarks(bo1.Image(build_image(reversals=2)))
        self.assertIn("exactly one", str(caught.exception))

    def test_no_free_space_is_a_refusal(self):
        with self.assertRaises(bo1.NotThisBuild) as caught:
            bo1.find_landmarks(bo1.Image(build_image(cave_size=0)))
        self.assertIn("free bytes", str(caught.exception))

    def test_a_binary_that_does_not_import_the_file_calls_is_a_refusal(self):
        with self.assertRaises(bo1.NotThisBuild) as caught:
            bo1.find_landmarks(bo1.Image(build_image(imports=False)))
        self.assertIn("cellFs", str(caught.exception))

    def test_something_that_is_not_an_elf_is_a_refusal(self):
        with self.assertRaises(bo1.NotThisBuild):
            bo1.Image(b"not an executable" * 100)


# --- patching --------------------------------------------------------------

class PatchingAndPuttingItBack(unittest.TestCase):

    def test_a_stock_image_reads_as_stock(self):
        offset, state = bo1.find_site(IMAGE)
        self.assertEqual(offset, HOOK)
        self.assertEqual(state, bo1.STOCK)

    def test_a_patched_image_reads_as_patched(self):
        out, _what = bo1.apply(IMAGE, "BLES01031")
        self.assertEqual(bo1.find_site(out)[1], bo1.PATCHED)

    def test_the_hook_is_the_only_instruction_changed_outside_the_cave(self):
        out, what = bo1.apply(IMAGE, "BLES01031")
        changed = [index for index in range(len(IMAGE))
                   if IMAGE[index] != out[index]]
        outside = [index for index in changed
                   if not CAVE <= index < CAVE + CAVE_SIZE]
        self.assertTrue(outside)
        self.assertTrue(all(HOOK <= index < HOOK + 4 for index in outside),
                        [hex(index) for index in outside])
        self.assertEqual(what["offset"], HOOK)

    def test_the_friends_list_is_left_exactly_as_it_was(self):
        out, _what = bo1.apply(IMAGE, "BLES01031")
        self.assertEqual(out[FRIEND:FRIEND + 4], IMAGE[FRIEND:FRIEND + 4])

    def test_putting_it_back_leaves_the_file_byte_for_byte_as_it_was(self):
        out, _what = bo1.apply(IMAGE, "BLES01031")
        self.assertEqual(bo1.restore(out), IMAGE)

    def test_the_path_carries_the_title_rather_than_a_guess(self):
        out, what = bo1.apply(IMAGE, "NPEB00756")
        self.assertEqual(what["paths"],
                         ["/dev_hdd0/game/NPEB00756/USRDIR/np_cache.dat"])
        for path in what["paths"]:
            self.assertIn(path.encode("ascii"), out)

    def test_the_copy_is_the_only_file_it_asks_for(self):
        """The real np_cache.dat is never asked for, on purpose.

        An earlier version tried the signed in user's own file first and fell
        back to the copy, on the reasoning that the game cannot open the real
        one anyway so a failed open costs nothing. That was the one thing this
        tool did that the build confirmed on hardware did not, and it is out.
        """
        _out, what = bo1.apply(IMAGE, "BLES01031")
        self.assertEqual(len(what["paths"]), 1)
        self.assertNotIn("/dev_hdd0/home/", what["paths"][0])
        self.assertIn("/USRDIR/", what["paths"][0])

    def test_patching_again_refreshes_the_path_rather_than_stacking(self):
        once, _ = bo1.apply(IMAGE, "BLES01031")
        twice, what = bo1.apply(once, "NPEB00756")
        self.assertEqual(bo1.find_site(twice)[1], bo1.PATCHED)
        self.assertIn("NPEB00756", what["paths"][0])
        self.assertNotIn(b"/dev_hdd0/game/BLES01031/USRDIR/np_cache.dat",
                         twice)
        self.assertEqual(bo1.restore(twice), IMAGE)

    def test_a_cave_this_fix_did_not_write_is_never_claimed_as_its_own(self):
        # Somebody else's patch, at the site this fix uses. It is not ours and
        # is not reported as ours, whatever else is done about it.
        meddled = bytearray(IMAGE)
        elsewhere = BASE + CAVE + 0x100
        _put(meddled, HOOK, bo1.bl(BASE + HOOK, elsewhere))
        image = bo1.Image(bytes(meddled))
        self.assertIsNone(bo1.cave_head(image, elsewhere))
        self.assertNotIn(BASE + HOOK, bo1.find_marked_calls(image))

    def test_a_cave_whose_head_is_damaged_stops_being_ours(self):
        # The head is the whole of how a patch is recognised, so a cave
        # without it is not one of ours and is not reported as one.
        out, _what = bo1.apply(IMAGE, "BLES01031")
        broken = bytearray(out)
        broken[CAVE:CAVE + 4] = b"XXXX"
        self.assertEqual(bo1.find_marked_calls(bo1.Image(bytes(broken))), [])

    def test_the_cave_is_marked_so_it_can_be_recognised_again(self):
        out, what = bo1.apply(IMAGE, "BLES01031")
        head = out[CAVE:CAVE + len(bo1.MARK)]
        self.assertEqual(head, bo1.MARK)
        self.assertEqual(
            struct.unpack_from(">I", out, CAVE + len(bo1.MARK))[0],
            what["bytes"])


# --- how the rest of the program sees it -----------------------------------

class WhatTheProgramMakesOfIt(unittest.TestCase):

    def test_the_state_reads_the_same_through_patchstate(self):
        self.assertEqual(patchstate.decrypted_state(IMAGE, "bo1")["state"],
                         patchstate.UNPATCHED)
        out, _ = bo1.apply(IMAGE, "BLES01031")
        self.assertEqual(patchstate.decrypted_state(out, "bo1")["state"],
                         patchstate.PATCHED)

    def test_a_refusal_comes_back_as_unknown_and_not_as_a_verdict(self):
        found = patchstate.decrypted_state(build_image(reversals=2), "bo1")
        self.assertEqual(found["state"], patchstate.UNKNOWN)

    def test_the_flow_reads_the_site_without_an_offset_to_check_against(self):
        site = titles.PATCH_SITES["bo1-multiplayer"]
        self.assertEqual(site.get("located_by"), "pattern")
        self.assertIsNone(site.get("file_offset"))
        self.assertEqual(flow.site_state(site, IMAGE, "bo1")[0],
                         flow.NOT_PATCHED)
        out, _ = bo1.apply(IMAGE, "BLES01031")
        self.assertEqual(flow.site_state(site, out, "bo1")[0], flow.PATCHED)

    def test_an_image_it_will_not_answer_for_is_unrecognised(self):
        site = titles.PATCH_SITES["bo1-multiplayer"]
        state, detail = flow.site_state(site, build_image(cave_size=0), "bo1")
        self.assertEqual(state, flow.UNRECOGNISED)
        self.assertIn("free bytes", detail)

    def test_the_flow_will_not_patch_without_being_told_which_title(self):
        with self.assertRaises(flow.PatchFailed):
            flow.apply_fix(IMAGE, "bo1", bo1, {})
        with self.assertRaises(flow.PatchFailed):
            flow.apply_fix(IMAGE, "bo1", bo1, {"title_id": ""})

    def test_the_flow_applies_it_with_the_context_it_is_given(self):
        out, offset = flow.apply_fix(IMAGE, "bo1", bo1,
                                     {"title_id": "NPEB00756"})
        self.assertEqual(offset, HOOK)
        self.assertIn(b"/dev_hdd0/game/NPEB00756/USRDIR/np_cache.dat", out)

    def test_only_the_multiplayer_binary_has_a_site(self):
        sites = {item["name"]: item["site"]
                 for item in titles.binaries_for("BLES01031")}
        self.assertEqual(sites["t5mp_ps3f.self"], "bo1-multiplayer")
        self.assertIsNone(sites["t5_ps3f.self"])
        self.assertIsNone(sites["EBOOT.BIN"])

    def test_the_releases_that_were_looked_at_are_the_ones_recognised(self):
        for title_id in ("BLES01031", "BLUS30591", "NPEB00756"):
            self.assertTrue(titles.is_recognised(title_id), title_id)
            self.assertEqual(titles.config_for(title_id)["key"], "bo1")
        # Not a release anybody has opened, so it is not attempted.
        self.assertFalse(titles.is_recognised("BLJM60352"))

    def test_the_fix_is_signed_with_compression_on(self):
        # With it off the rebuilt file comes out at nearly twice the size of
        # the stock one, which is the shape of a file that will not load.
        self.assertEqual(flow.SIGNING["bo1"].compressed, "TRUE")


# --- what the screen says --------------------------------------------------

class TheScreenSaysWhatIsKnown(unittest.TestCase):
    """The map packs: not this fix's doing, and not cured by it either."""

    def screen_class(self):
        from ps3tools.screens.patcher import BlackOpsOnePatcher
        return BlackOpsOnePatcher

    def test_it_says_the_map_packs_are_the_cause(self):
        notice = self.screen_class().NOTICE
        self.assertIn("map packs", notice)
        self.assertIn("public", notice)
        self.assertIn("rather than this fix", notice)

    def test_it_says_what_to_do_about_it(self):
        self.assertIn("Renaming or removing", self.screen_class().NOTICE)

    def test_it_says_what_the_evidence_is_and_claims_no_more(self):
        self.assertIn("two consoles", self.screen_class().NOTICE)

    def test_the_work_being_done_is_said_apart_from_the_facts(self):
        # Drawn bold and in the accent colour, so it is the half that catches
        # the eye rather than a clause at the end of a paragraph.
        work = self.screen_class().NOTICE_WORK
        self.assertIn("being looked into", work)
        self.assertIn("leaves the map packs alone", work)
        self.assertNotIn(work, self.screen_class().NOTICE)

    def test_it_promises_no_date(self):
        words = (self.screen_class().NOTICE + " "
                 + self.screen_class().NOTICE_WORK).lower()
        for forbidden in ("soon", "next release", "shortly", "will be fixed",
                          "coming", "shortly", "version 1."):
            self.assertNotIn(forbidden, words)

    def test_the_other_two_fixes_have_nothing_to_add(self):
        from ps3tools.screens.patcher import (BlackOpsTwoPatcher,
                                              ModernWarfareThreePatcher)
        for screen in (BlackOpsTwoPatcher, ModernWarfareThreePatcher):
            self.assertEqual(screen.NOTICE, "", screen.key)
            self.assertEqual(screen.NOTICE_WORK, "", screen.key)


# --- np_cache.dat ----------------------------------------------------------

class Lister:
    """A console with whatever users this test wants on it."""

    def __init__(self, people, cache=b""):
        self.people = people
        self.cache = cache or (struct.pack(">Q", 3034630675101139701)
                               + b"shtum_pill34\x00\x00\x00\x00")
        self.read = []

    def list_dir(self, path):
        if path == npcache.HOME:
            return "\n".join(
                "drwxr-xr-x 1 root root 0 Jan 1 00:00 %s" % folder
                for folder, _name, _has in self.people)
        for folder, _name, has in self.people:
            if path.endswith(folder):
                lines = ["-rw-r--r-- 1 root root 6 Jan 1 00:00 localusername"]
                if has:
                    lines.append("-rw------- 1 root root 248 Jan 1 00:00 "
                                 "np_cache.dat")
                return "\n".join(lines)
        return ""

    def retrieve_bytes(self, path):
        self.read.append(path)
        if path.endswith(npcache.USERNAME):
            folder = path.split("/")[-2]
            for item, name, _has in self.people:
                if item == folder:
                    return name.encode("utf-8") + b"\x00"
            return b""
        if path.endswith(npcache.NAME):
            return self.cache
        raise OSError("no such file")


class TheAccountTheFixIsTiedTo(unittest.TestCase):

    def test_the_account_id_is_the_first_eight_bytes_big_endian(self):
        raw = struct.pack(">Q", 3034630675101139701) + b"rest"
        self.assertEqual(npcache.account_id(raw), 3034630675101139701)

    def test_an_account_that_has_not_finished_signing_in_is_refused(self):
        with self.assertRaises(npcache.NoAccount) as caught:
            npcache.account_id(b"\x00" * 8)
        self.assertIn("sign in", str(caught.exception).lower())

    def test_the_online_id_is_read_for_showing_and_not_for_hashing(self):
        lister = Lister([("00000001", "Chris", True)])
        self.assertEqual(npcache.online_id(lister.cache), "shtum_pill34")

    def test_every_local_user_is_listed_with_the_name_it_carries(self):
        lister = Lister([("00000001", "Chris", True),
                         ("00000005", "Guest", False)])
        people = npcache.users(lister)
        self.assertEqual([person.folder for person in people],
                         ["00000001", "00000005"])
        self.assertEqual(people[0].label, "Chris (00000001)")

    def test_an_account_never_online_is_listed_and_then_excluded(self):
        # It has no np_cache.dat, because the file is written the first time
        # an account signs in. Leaving it out of the list entirely would make
        # the account look as though it did not exist.
        lister = Lister([("00000001", "Chris", True),
                         ("00000005", "Guest", False)])
        people = npcache.users(lister)
        self.assertEqual(len(people), 2)
        self.assertEqual([person.folder
                          for person in npcache.with_cache(people)],
                         ["00000001"])

    def test_the_user_folder_is_never_assumed_to_be_the_first_one(self):
        lister = Lister([("00000003", "Only", True)])
        people = npcache.with_cache(npcache.users(lister))
        self.assertEqual(people[0].folder, "00000003")
        self.assertEqual(npcache.path_for(people[0].folder),
                         "/dev_hdd0/home/00000003/np_cache.dat")

    def test_something_that_is_not_a_user_folder_is_passed_over(self):
        lister = Lister([("00000001", "Chris", True), ("notauser", "x", True)])
        self.assertEqual([person.folder for person in npcache.users(lister)],
                         ["00000001"])

    def test_the_copy_goes_into_the_game_s_own_folder(self):
        self.assertEqual(npcache.destination("bles01031"),
                         "/dev_hdd0/game/BLES01031/USRDIR/np_cache.dat")

    def test_a_short_file_is_refused_rather_than_read_past(self):
        lister = Lister([("00000001", "Chris", True)], cache=b"\x01\x02")
        with self.assertRaises(npcache.NoAccount):
            npcache.read_for(lister, "00000001")

    def test_the_copy_staged_is_what_was_read(self):
        import tempfile
        lister = Lister([("00000001", "Chris", True)])
        raw = npcache.read_for(lister, "00000001")
        workdir = tempfile.mkdtemp()
        remote, local = npcache.place(lister, "BLES01031", raw, workdir)
        self.assertEqual(remote, npcache.destination("BLES01031"))
        with open(local, "rb") as handle:
            self.assertEqual(handle.read(), raw)


# --- the fake-signed digital release ---------------------------------------

class TheFileScetoolWillNotOpen(unittest.TestCase):
    """unfself is not bundled, so what matters is an honest absence."""

    class Refuses:
        problem = ""
        available = True

        def decrypt(self, path, destination, klicensee=None):
            raise ScetoolError("this file is fake-signed.")

        def info(self, path, klicensee=None):
            return {"marker": "scetool"}

        def sign(self, *args, **kwargs):
            return "signed"

    class Opens:
        problem = ""
        available = True

        def __init__(self):
            self.asked = []

        def decrypt(self, path, destination, klicensee=None):
            self.asked.append(path)
            return b"\x7fELFopened"

    def test_without_unfself_the_refusal_says_what_is_missing(self):
        tool = WithFakeSigned(self.Refuses(), Unfself(executable=""))
        with self.assertRaises(ScetoolError) as caught:
            tool.decrypt("t5mp_ps3f.self", "out.elf", "KEY")
        self.assertIn("fake-signed", str(caught.exception))
        self.assertIn("unfself", str(caught.exception))

    def test_with_unfself_the_refused_file_is_handed_to_it(self):
        fallback = self.Opens()
        tool = WithFakeSigned(self.Refuses(), fallback)
        self.assertEqual(tool.decrypt("t5mp_ps3f.self", "out.elf", "KEY"),
                         b"\x7fELFopened")
        self.assertEqual(fallback.asked, ["t5mp_ps3f.self"])

    def test_a_file_that_opens_the_ordinary_way_never_reaches_it(self):
        class Works(self.Opens):
            def decrypt(self, path, destination, klicensee=None):
                return b"\x7fELFscetool"

        fallback = self.Opens()
        tool = WithFakeSigned(Works(), fallback)
        self.assertEqual(tool.decrypt("a", "b", "KEY"), b"\x7fELFscetool")
        self.assertEqual(fallback.asked, [])

    def test_what_it_produces_has_to_look_like_a_binary(self):
        import tempfile
        folder = tempfile.mkdtemp()
        out = os.path.join(folder, "out.elf")

        def runner(args):
            with open(args[1], "wb") as handle:
                handle.write(b"unfself: could not open that\n")
            return ""

        tool = Unfself(executable=__file__, runner=runner)
        with self.assertRaises(ScetoolError) as caught:
            tool.decrypt("whatever.self", out)
        self.assertIn("not an ELF", str(caught.exception))

    def test_everything_but_decrypt_is_still_scetool_s(self):
        tool = WithFakeSigned(self.Refuses(), Unfself(executable=""))
        self.assertEqual(tool.info("a")["marker"], "scetool")
        self.assertEqual(tool.sign(), "signed")


# --- the whole run, against a console --------------------------------------

class TheWholeRun(unittest.TestCase):
    """Scan, back up, place the copy, patch, sign and write back.

    Against the mock console the rest of the patching tests use, because the
    pieces working on their own says nothing about the order they run in, and
    the order is where this fix differs from the other two: the copy of
    np_cache.dat goes up before anything is patched, so a fix whose supporting
    file did not arrive stops before it has changed a binary.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, here)
        spec = importlib.util.spec_from_file_location(
            "make_images_bo1",
            os.path.join(here, "fixtures", "patching", "make_images.py"))
        cls.images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.images)

    def setUp(self):
        import shutil
        import tempfile
        self.workdir = tempfile.mkdtemp(prefix="ps3tools-bo1-")
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    # -- the console
    CONTENT = "EP0002-BLES01031_00-BLACKOPS00000000"

    def self_file(self, name, state="stock"):
        """One fake SELF holding a Black Ops 1 image."""
        record = next(item for item in titles.binaries_for("BLES01031")
                      if item["name"] == name)
        image = IMAGE if state == "stock" else \
            bo1.apply(IMAGE, "BLES01031")[0]
        if record["site"] is None:
            # Something beside the one being changed, which must be listed and
            # left alone. It is not the multiplayer binary and does not carry
            # anything the fix looks for.
            image = b"\x7fELF" + b"\x00" * 0x400
        fields = self.images.info_for(self.CONTENT, "USPRX", name,
                                      key_revision="0004",
                                      fw_version="0003004000000000")
        return self.images.wrap(fields, record["klicensee"] or "", image)

    def start(self, files, home=("00000001",)):
        from mock_webman import MockWebmanFtp
        usrdir = titles.usrdir_for("BLES01031")
        line = "-rw-rw-rw-   1 root     root  %11d Sep 06 19:51 %s"
        folder = "drwxrwxrwx   1 root     root            0 Sep 06 19:51 %s"
        listing = "\n".join(line % (len(body), name)
                            for name, body in sorted(files.items())) + "\n"
        listings = {
            usrdir + "/": listing.encode(),
            "/dev_hdd0/game/": (folder % "BLES01031").encode() + b"\n",
            "/dev_hdd0/home/": ("\n".join(folder % item
                                          for item in home)).encode() + b"\n",
        }
        stored = {f"{usrdir}/{name}": body for name, body in files.items()}
        for item in home:
            at = f"/dev_hdd0/home/{item}"
            listings[at + "/"] = (
                (line % (12, "localusername")) + "\n"
                + (line % (248, npcache.NAME)) + "\n").encode()
            stored[f"{at}/localusername"] = b"Chris\x00"
            stored[f"{at}/{npcache.NAME}"] = (
                struct.pack(">Q", self.account_for(item))
                + b"shtum_pill34\x00\x00\x00\x00")
        server = MockWebmanFtp(listings=listings, files=stored, writable=True)
        server.start()
        self.addCleanup(server.stop)
        self.server = server
        return server

    @staticmethod
    def account_for(folder):
        """A different account ID for each local user on the fake console.

        So that a test can tell which account's file was copied. 00000001 is
        the one read off a real np_cache.dat, which is what the rest of these
        tests expect to come out the other end.
        """
        return 3034630675101139700 + int(folder)

    def writer(self):
        import ftplib

        from ps3tools.patching.ftpwrite import FtpWriter

        def factory():
            ftp = ftplib.FTP()
            ftp.connect("127.0.0.1", self.server.port, timeout=10)
            ftp.login("anonymous", "anonymous@")
            return ftp

        made = FtpWriter("127.0.0.1", timeout=10, factory=factory)
        self.addCleanup(made.close)
        return made

    def tool(self):
        from ps3tools.patching import scetool

        images = self.images

        class FakeScetool:
            problem = ""

            def info(self, path, klicensee=None):
                with open(path, "rb") as handle:
                    fields, _key, _image = images.unwrap(handle.read())
                return scetool.parse_header(images.header_text(fields))

            def decrypt(self, path, destination, klicensee=None):
                with open(path, "rb") as handle:
                    _fields, wanted, image = images.unwrap(handle.read())
                if wanted != (klicensee or ""):
                    raise scetool.ScetoolError("wrong klicensee")
                with open(destination, "wb") as handle:
                    handle.write(image)
                return image

            def sign(self, profile, info, source, elf_path, destination,
                     target_name, klicensee=None):
                with open(elf_path, "rb") as handle:
                    image = handle.read()
                fields = {name: info[name]
                          for name in images.info_for("x", "y", "z")
                          if name in info}
                fields["cid_fn_hash"] = images.cid_fn_hash(target_name)
                with open(destination, "wb") as handle:
                    handle.write(images.wrap(fields, klicensee or "", image))
                return "signed"

        return FakeScetool()

    def run_it(self, home=("00000001",), user="00000001"):
        files = {name: self.self_file(name)
                 for name in ("t5mp_ps3f.self", "t5_ps3f.self", "EBOOT.BIN")}
        self.start(files, home=home)
        tool = self.tool()
        report = flow.scan(self.writer(), tool, "BLES01031")
        self.assertTrue(report.can_patch, report.error or report.notes)
        writer = self.writer()
        raw = npcache.read_for(writer, user)
        result = flow.patch(
            writer, tool, report, root=self.workdir,
            context={"title_id": "BLES01031"},
            extra_files=(npcache.place(writer, "BLES01031", raw,
                                       self.workdir),))
        return report, result, files

    # -- what it does
    def test_the_scan_reads_the_state_out_of_the_multiplayer_binary(self):
        files = {name: self.self_file(name)
                 for name in ("t5mp_ps3f.self", "t5_ps3f.self", "EBOOT.BIN")}
        self.start(files)
        report = flow.scan(self.writer(), self.tool(), "BLES01031")
        self.assertEqual(report.error, "")
        states = {item.name: item.state for item in report.files}
        self.assertEqual(states["t5mp_ps3f.self"], flow.NOT_PATCHED)
        self.assertEqual(states["t5_ps3f.self"], flow.NO_SITE)
        self.assertEqual(states["EBOOT.BIN"], flow.NO_SITE)

    def test_a_file_nothing_will_be_written_to_is_never_called_a_fault(self):
        """Reported off a real console: EBOOT.BIN came up in red.

        Its header does not read the way the two selfs beside it do, and the
        scan reached that before it reached the question of whether there was
        anything to patch in it. So the table said "not recognised", in the
        colour it uses for faults, in the row whose own description already
        said the file was unaffected.
        """
        from ps3tools.patching import scetool as scetool_module

        files = {name: self.self_file(name)
                 for name in ("t5mp_ps3f.self", "t5_ps3f.self", "EBOOT.BIN")}
        self.start(files)
        real = self.tool()

        class HeaderRefusesForTheLauncher:
            problem = ""

            def info(self, path, klicensee=None):
                if os.path.basename(path) == "EBOOT.BIN":
                    raise scetool_module.ScetoolError(
                        "EBOOT.BIN has no App type in its header")
                return real.info(path, klicensee)

            def decrypt(self, path, destination, klicensee=None):
                return real.decrypt(path, destination, klicensee)

            def sign(self, *args, **kwargs):
                return real.sign(*args, **kwargs)

        report = flow.scan(self.writer(), HeaderRefusesForTheLauncher(),
                           "BLES01031")
        launcher = report.file_for("EBOOT.BIN")
        self.assertEqual(launcher.state, flow.NO_SITE)
        self.assertEqual(launcher.detail, "the launcher; unaffected")
        self.assertNotIn(launcher, report.unrecognised)
        # And nothing in the notes tells anybody about it either.
        self.assertNotIn("EBOOT.BIN", " ".join(report.notes))
        # The file that is going to be written to is judged as it always was.
        self.assertEqual(report.file_for("t5mp_ps3f.self").state,
                         flow.NOT_PATCHED)

    def test_a_header_that_will_not_read_still_stops_a_file_being_written(self):
        # The other half of it: the relaxation is only for files with no patch
        # site. A file this program is going to sign has to have been read.
        from ps3tools.patching import scetool as scetool_module

        files = {name: self.self_file(name)
                 for name in ("t5mp_ps3f.self", "t5_ps3f.self", "EBOOT.BIN")}
        self.start(files)
        real = self.tool()

        class HeaderRefusesForTheBinary:
            problem = ""

            def info(self, path, klicensee=None):
                if os.path.basename(path) == "t5mp_ps3f.self":
                    raise scetool_module.ScetoolError("no App type")
                return real.info(path, klicensee)

            def decrypt(self, path, destination, klicensee=None):
                return real.decrypt(path, destination, klicensee)

            def sign(self, *args, **kwargs):
                return real.sign(*args, **kwargs)

        report = flow.scan(self.writer(), HeaderRefusesForTheBinary(),
                           "BLES01031")
        item = report.file_for("t5mp_ps3f.self")
        self.assertEqual(item.state, flow.UNRECOGNISED)
        self.assertFalse(report.can_patch)

    def test_a_clean_run_patches_the_binary_and_leaves_the_others(self):
        _report, result, _files = self.run_it()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.changed, ["t5mp_ps3f.self"])
        usrdir = titles.usrdir_for("BLES01031")
        self.assertNotIn(f"{usrdir}/t5_ps3f.self", self.server.written)
        self.assertNotIn(f"{usrdir}/EBOOT.BIN", self.server.written)

    def test_what_landed_on_the_console_reads_as_patched(self):
        _report, result, _files = self.run_it()
        self.assertTrue(result.ok, result.error)
        remote = f"{titles.usrdir_for('BLES01031')}/t5mp_ps3f.self"
        _fields, _key, landed = self.images.unwrap(self.server.written[remote])
        self.assertEqual(bo1.find_site(landed)[1], bo1.PATCHED)

    def test_the_copy_of_np_cache_goes_into_the_game_s_folder(self):
        _report, result, _files = self.run_it()
        self.assertTrue(result.ok, result.error)
        remote = npcache.destination("BLES01031")
        self.assertIn(remote, self.server.written)
        self.assertEqual(
            struct.unpack(">Q", self.server.written[remote][:8])[0],
            3034630675101139701)

    def test_the_path_written_into_the_binary_is_the_path_it_was_put_at(self):
        # The one thing the binary is told, and the one thing that has to
        # agree with where the tool actually put the file.
        _report, result, _files = self.run_it()
        remote = f"{titles.usrdir_for('BLES01031')}/t5mp_ps3f.self"
        _fields, _key, landed = self.images.unwrap(self.server.written[remote])
        self.assertIn(npcache.destination("BLES01031").encode("ascii"), landed)

    def test_the_copy_is_the_chosen_account_and_not_the_first_one(self):
        """The user folder is not always 00000001.

        It does not reach the binary any more, because the binary only knows
        the one path the copy is put at. It reaches the copy instead, which is
        where getting it wrong would show: the game would then work out an
        identity belonging to somebody else and be told about somebody else's
        rank, which looks exactly like the fix having done nothing.
        """
        _report, result, _files = self.run_it(home=("00000001", "00000006"),
                                              user="00000006")
        self.assertTrue(result.ok, result.error)
        landed = self.server.written[npcache.destination("BLES01031")]
        self.assertEqual(struct.unpack(">Q", landed[:8])[0],
                         self.account_for("00000006"))
        self.assertNotEqual(self.account_for("00000006"),
                            self.account_for("00000001"))

    def test_the_originals_are_kept_and_can_be_put_back(self):
        _report, result, files = self.run_it()
        entry = result.backup.entry_for("t5mp_ps3f.self")
        with open(entry["path"], "rb") as handle:
            self.assertEqual(handle.read(), files["t5mp_ps3f.self"])

    def test_the_run_says_the_copy_is_a_snapshot_of_one_account(self):
        _report, result, _files = self.run_it()
        said = " ".join(result.notes)
        self.assertIn(npcache.destination("BLES01031"), said)
        self.assertIn("signed in", said)

    def test_the_copy_is_not_counted_as_a_patched_binary(self):
        _report, result, _files = self.run_it()
        self.assertEqual(result.changed, ["t5mp_ps3f.self"])
        self.assertNotIn(npcache.destination("BLES01031"), result.uploaded)


if __name__ == "__main__":
    unittest.main()
