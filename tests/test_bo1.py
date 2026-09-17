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

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sce"))
import corpus as sce_corpus                            # noqa: E402
from ps3diag import patchstate
from ps3tools import titles
from ps3tools.patching import flow, npcache
from ps3tools.patching.scetool import ScetoolError
from ps3tools.patching.signer import Signer

bo1 = patchstate.patcher_module("bo1")


#: The account ID in the np_cache.dat this repository was developed against.
ACCOUNT_ID = 3034630675101139701


def content_id_for(title_id):
    """A content ID carrying a given title, the way a real SELF spells one.

    The fix takes the folder it reads from out of this rather than out of the
    folder's own name, because a folder can be renamed and a content ID that
    travels in the SELF header cannot.
    """
    return f"EP0002-{title_id}_00-CODBLOPSPATCH012"


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
        out, _what = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
        self.assertEqual(bo1.find_site(out)[1], bo1.PATCHED)

    def test_the_hook_is_the_only_instruction_changed_outside_the_cave(self):
        out, what = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
        changed = [index for index in range(len(IMAGE))
                   if IMAGE[index] != out[index]]
        outside = [index for index in changed
                   if not CAVE <= index < CAVE + CAVE_SIZE]
        self.assertTrue(outside)
        self.assertTrue(all(HOOK <= index < HOOK + 4 for index in outside),
                        [hex(index) for index in outside])
        self.assertEqual(what["offset"], HOOK)

    def test_the_friends_list_is_left_exactly_as_it_was(self):
        out, _what = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
        self.assertEqual(out[FRIEND:FRIEND + 4], IMAGE[FRIEND:FRIEND + 4])

    def test_putting_it_back_leaves_the_file_byte_for_byte_as_it_was(self):
        out, _what = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
        self.assertEqual(bo1.restore(out), IMAGE)

    def test_the_path_carries_the_title_rather_than_a_guess(self):
        out, what = bo1.apply(IMAGE, "EP0002-NPEB00756_00-CODBLOPSPATCH012")
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
        _out, what = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
        self.assertEqual(len(what["paths"]), 1)
        self.assertNotIn("/dev_hdd0/home/", what["paths"][0])
        self.assertIn("/USRDIR/", what["paths"][0])

    def test_patching_again_refreshes_the_path_rather_than_stacking(self):
        once, _ = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
        twice, what = bo1.apply(once, "EP0002-NPEB00756_00-CODBLOPSPATCH012")
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
        out, _what = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
        broken = bytearray(out)
        broken[CAVE:CAVE + 4] = b"XXXX"
        self.assertEqual(bo1.find_marked_calls(bo1.Image(bytes(broken))), [])

    def test_the_cave_is_marked_so_it_can_be_recognised_again(self):
        out, what = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
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
        out, _ = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
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
        out, _ = bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")
        self.assertEqual(flow.site_state(site, out, "bo1")[0], flow.PATCHED)

    def test_an_image_it_will_not_answer_for_is_unrecognised(self):
        site = titles.PATCH_SITES["bo1-multiplayer"]
        state, detail = flow.site_state(site, build_image(cave_size=0), "bo1")
        self.assertEqual(state, flow.UNRECOGNISED)
        self.assertIn("free bytes", detail)

    def test_the_flow_will_not_patch_without_a_content_id(self):
        with self.assertRaises(flow.PatchFailed):
            flow.apply_fix(IMAGE, "bo1", bo1, {})
        with self.assertRaises(flow.PatchFailed):
            flow.apply_fix(IMAGE, "bo1", bo1, {"content_id": ""})

    def test_the_flow_will_not_take_a_folder_name_for_a_content_id(self):
        """The defect this guards is silent.

        A folder name was what the flow used to hand over. It is right on the
        European disc, where the folder happens to be called BLES01031, and
        wrong on the American and digital releases, where the fix then opened
        a path that did not exist, fell through to the stock behaviour and
        still reported as applied.
        """
        with self.assertRaises(flow.PatchFailed):
            flow.apply_fix(IMAGE, "bo1", bo1, {"content_id": "BLES01031"})

    def test_the_flow_applies_it_with_the_context_it_is_given(self):
        out, offset = flow.apply_fix(
            IMAGE, "bo1", bo1, {"content_id": content_id_for("NPEB00756")})
        self.assertEqual(offset, HOOK)
        self.assertIn(b"/dev_hdd0/game/NPEB00756/USRDIR/np_cache.dat", out)

    def test_the_title_comes_off_the_file_and_not_off_the_folder(self):
        # Every release gets its own folder in the path, from its own content
        # ID. This is the whole of the first defect.
        for title_id in ("BLES01031", "BLUS30591", "NPEB00756"):
            _out, what = bo1.apply(IMAGE, content_id_for(title_id))
            self.assertEqual(what["title_id"], title_id)
            self.assertEqual(
                what["paths"],
                [f"/dev_hdd0/game/{title_id}/USRDIR/np_cache.dat"])

    def test_a_title_that_is_not_one_is_refused_rather_than_formatted(self):
        for bad in ("", "BLES0103", "BLES010311", "BLES 1031", "../../etc"):
            with self.assertRaises(bo1.NotThisBuild):
                bo1.np_cache_path(bad)

    def test_the_copy_lands_where_the_fix_will_look_for_it(self):
        """One value, so the two cannot drift apart.

        The path the cave opens and the path the tool writes to are built by
        the same function. Two templates that agree today are two templates
        that can stop agreeing, and the way that shows is a patch that applies
        cleanly and does nothing.
        """
        for title_id in ("BLES01031", "BLUS30591", "NPEB00756"):
            content_id = content_id_for(title_id)
            _out, what = bo1.apply(IMAGE, content_id)
            self.assertEqual(npcache.destination(content_id),
                             what["paths"][0])

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
    """What the screen has to say before somebody presses anything."""

    def screen_class(self):
        from ps3tools.screens.patcher import BlackOpsOnePatcher
        return BlackOpsOnePatcher

    def notices(self):
        return self.screen_class().NOTICES

    def joined(self):
        return " ".join(body + " " + emphasis
                        for body, emphasis, _token in self.notices())

    # -- the one that decides whether to go any further
    def test_the_first_thing_said_is_who_should_not_apply_it(self):
        """It comes first because it decides whether to read on.

        The fix makes the client hash the account ID. An account made before
        Sony's 2018 change authenticates on a hash of the online ID, so this
        would hand it an identity the server has never held and break what
        works today. Nothing on the console tells the two apart.
        """
        body, emphasis, _token = self.notices()[0]
        self.assertIn("Only apply this if your rank actually resets", body)
        self.assertIn("before late 2018", body)
        self.assertIn("identity the server does not hold", body)
        self.assertIn("leave this alone", emphasis)

    def test_that_warning_is_drawn_as_a_caution(self):
        self.assertEqual(self.notices()[0][2], "warn")

    def test_apply_waits_on_the_person_saying_they_see_resets(self):
        # Nothing client side can tell which scheme an account uses, so the
        # decision is the user's and the button waits for it.
        self.assertTrue(self.screen_class().CONFIRM_WITH)
        self.assertIn("resets", self.screen_class().CONFIRM_WITH.lower())

    # -- the map packs
    def test_it_says_the_map_packs_are_the_cause(self):
        said = self.joined()
        self.assertIn("map packs", said)
        self.assertIn("public", said)
        self.assertIn("rather than this fix", said)

    def test_it_says_what_to_do_about_it(self):
        self.assertIn("Renaming or removing", self.joined())

    def test_it_says_what_the_evidence_is_and_claims_no_more(self):
        self.assertIn("two consoles", self.joined())

    def test_the_work_being_done_is_said_apart_from_the_facts(self):
        # Drawn bold and in the accent colour, so the half that decides what
        # somebody does is the half that catches the eye.
        for body, emphasis, _token in self.notices():
            self.assertTrue(emphasis)
            self.assertNotIn(emphasis, body)

    def test_it_promises_no_date(self):
        words = self.joined().lower()
        for forbidden in ("soon", "next release", "shortly", "will be fixed",
                          "coming", "version 1."):
            self.assertNotIn(forbidden, words)

    def test_the_other_two_fixes_have_nothing_to_add(self):
        from ps3tools.screens.patcher import (BlackOpsTwoPatcher,
                                              ModernWarfareThreePatcher)
        for screen in (BlackOpsTwoPatcher, ModernWarfareThreePatcher):
            self.assertEqual(screen.NOTICES, (), screen.key)
            self.assertEqual(screen.CONFIRM_WITH, "", screen.key)


# --- np_cache.dat ----------------------------------------------------------

def np_cache(online_id="shtum_pill34", account=3034630675101139701):
    """np_cache.dat as the console writes it, to the layout of the real one.

    Read off ~/bo1-BLES01031/np_cache.dat: the account ID in the first eight
    bytes, then an SceNpOnlineId, which is sixteen bytes of name and a
    terminator and three of padding.
    """
    name = online_id.encode("ascii")
    return (struct.pack(">Q", account)
            + name + b"\x00" * (npcache.ONLINE_ID_BYTES + 4 - len(name)))


class Lister:
    """A console with whatever users this test wants on it.

    people is (folder, local name, np_cache.dat or None). The cache carries
    that account's own online ID, so a test can tell which one was read.
    """

    def __init__(self, people, cache=None, stamps=None):
        self.people = people
        #: Overrides every account's file, for testing a broken one.
        self.cache = cache
        #: {folder: listing timestamp}, for testing the ordering.
        self.stamps = stamps or {}
        self.read = []

    def stamp_for(self, folder):
        return self.stamps.get(folder, "Jan 01 00:00")

    def list_dir(self, path):
        if path == npcache.HOME:
            return "\n".join(
                "drwxr-xr-x 1 root root 0 Jan 01 00:00 %s" % folder
                for folder, _name, _cache in self.people)
        for folder, _name, cache in self.people:
            if path.endswith(folder):
                lines = ["-rw-r--r-- 1 root root 6 Jan 01 00:00 "
                         "localusername"]
                if cache is not None:
                    lines.append("-rw------- 1 root root 248 %s np_cache.dat"
                                 % self.stamp_for(folder))
                return "\n".join(lines)
        return ""

    def download_account_file(self, path):
        self.read.append(path)
        folder = path.split("/")[-2]
        if path.endswith(npcache.USERNAME):
            for item, name, _cache in self.people:
                if item == folder:
                    return name.encode("utf-8") + b"\x00"
            return b""
        if path.endswith(npcache.NAME):
            if self.cache is not None:
                return self.cache
            for item, _name, cache in self.people:
                if item == folder and cache is not None:
                    return cache
            raise OSError("no np_cache.dat")
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
        # Verified against the real np_cache.dat: the account ID occupies the
        # first eight bytes and the name follows as an SceNpOnlineId.
        self.assertEqual(npcache.online_id(np_cache()), "shtum_pill34")
        self.assertEqual(npcache.ONLINE_ID_AT, 8)
        self.assertEqual(npcache.ONLINE_ID_BYTES, 16)

    def test_a_field_that_is_not_a_name_is_not_shown_as_one(self):
        # Offsets are the easiest thing to get wrong here, and a field of
        # rubbish read out of the wrong one would be shown to somebody who is
        # about to pick their account by it.
        for rubbish in (b"\x00" * 8 + b"\xff\xfe\x01\x02",
                        b"\x00" * 8 + b"ab\x00",
                        b"\x00" * 8 + b"has spaces\x00",
                        b"\x00" * 4):
            self.assertEqual(npcache.online_id(rubbish), "")

    def test_the_newest_account_is_first(self):
        """The one signed in now is the one written most recently.

        The console writes np_cache.dat when an account signs in to PSN, so
        the newest file is the account being used. The listing only gives a
        month, day and time for anything recent, which inverts across a new
        year, and that is why this decides what is offered first rather than
        what is used.
        """
        lister = Lister(
            [("00000001", "One", np_cache("older_one")),
             ("00000013", "Two", np_cache("shtum_pill34")),
             ("00000007", "Three", np_cache("middle_one"))],
            stamps={"00000001": "Jan 02 09:00",
                    "00000013": "Sep 06 19:51",
                    "00000007": "Mar 11 12:30"})
        people = npcache.users(lister)
        self.assertEqual([person.folder for person in people],
                         ["00000013", "00000007", "00000001"])
        self.assertEqual(people[0].label, "shtum_pill34 (00000013)")

    def test_a_file_stamped_with_a_year_is_older_than_one_with_a_time(self):
        lister = Lister(
            [("00000001", "Old", np_cache("last_year")),
             ("00000002", "New", np_cache("this_year"))],
            stamps={"00000001": "Dec 20 2024", "00000002": "Jan 05 10:00"})
        self.assertEqual([person.folder for person in npcache.users(lister)],
                         ["00000002", "00000001"])

    def test_a_folder_with_no_readable_name_is_listed_as_its_number(self):
        # Shown rather than hidden. A console with an account this program
        # cannot read is a console whose owner should see that it is there.
        lister = Lister([("00000001", "Chris", np_cache()),
                         ("00000009", "Broken", b"\x00" * 40)])
        people = npcache.users(lister)
        self.assertEqual(len(people), 2)
        labels = {person.folder: person.label for person in people}
        self.assertEqual(labels["00000001"], "shtum_pill34 (00000001)")
        self.assertEqual(labels["00000009"], "00000009")
        # And it is not offered, because the fix cannot name what it would be
        # tied to.
        self.assertEqual([person.folder
                          for person in npcache.with_cache(people)],
                         ["00000001"])

    def test_each_account_is_read_from_its_own_file(self):
        lister = Lister([("00000001", "One", np_cache("first_player")),
                         ("00000002", "Two", np_cache("second_player"))])
        found = {person.folder: person.online_id
                 for person in npcache.users(lister)}
        self.assertEqual(found, {"00000001": "first_player",
                                 "00000002": "second_player"})

    def test_every_local_user_is_listed_with_the_name_it_carries(self):
        lister = Lister([("00000001", "Chris", np_cache()),
                         ("00000005", "Guest", None)])
        people = npcache.users(lister)
        self.assertEqual([person.folder for person in people],
                         ["00000001", "00000005"])
        self.assertEqual(people[0].label, "shtum_pill34 (00000001)")

    def test_an_account_never_online_is_listed_and_then_excluded(self):
        # It has no np_cache.dat, because the file is written the first time
        # an account signs in. Leaving it out of the list entirely would make
        # the account look as though it did not exist.
        lister = Lister([("00000001", "Chris", np_cache()),
                         ("00000005", "Guest", None)])
        people = npcache.users(lister)
        self.assertEqual(len(people), 2)
        self.assertEqual([person.folder
                          for person in npcache.with_cache(people)],
                         ["00000001"])

    def test_the_user_folder_is_never_assumed_to_be_the_first_one(self):
        lister = Lister([("00000003", "Only", np_cache())])
        people = npcache.with_cache(npcache.users(lister))
        self.assertEqual(people[0].folder, "00000003")
        self.assertEqual(npcache.path_for(people[0].folder),
                         "/dev_hdd0/home/00000003/np_cache.dat")

    def test_something_that_is_not_a_user_folder_is_passed_over(self):
        lister = Lister([("00000001", "Chris", np_cache()),
                         ("notauser", "x", np_cache())])
        self.assertEqual([person.folder for person in npcache.users(lister)],
                         ["00000001"])

    def test_the_copy_goes_into_the_game_s_own_folder(self):
        self.assertEqual(npcache.destination(content_id_for("BLES01031")),
                         "/dev_hdd0/game/BLES01031/USRDIR/np_cache.dat")

    def test_a_short_file_is_refused_rather_than_read_past(self):
        lister = Lister([("00000001", "Chris", np_cache())], cache=b"\x01\x02")
        with self.assertRaises(npcache.NoAccount):
            npcache.read_for(lister, "00000001")

    def test_the_copy_staged_is_what_was_read(self):
        import tempfile
        lister = Lister([("00000001", "Chris", np_cache())])
        raw = npcache.read_for(lister, "00000001")
        workdir = tempfile.mkdtemp()
        content_id = content_id_for("BLES01031")
        remote, local = npcache.place(lister, content_id, raw, workdir)
        self.assertEqual(remote, npcache.destination(content_id))
        with open(local, "rb") as handle:
            self.assertEqual(handle.read(), raw)


class WhichAccountTheScreenAsksAbout(unittest.TestCase):
    """One account is not a question. Several are, and they get names."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance() or QApplication([])

    def screen(self, people):
        from ps3tools.shell.screen import ConnectionState, Services
        from ps3tools.shell.theme import AppTheme
        from ps3tools.screens.patcher import BlackOpsOnePatcher
        made = BlackOpsOnePatcher(
            Services(ConnectionState(""), AppTheme("dark"), {}))
        self.addCleanup(made.deleteLater)
        made.on_scan_extras({"users": people})
        return made

    def refuse_to_ask(self):
        """Make the chooser fail loudly, so a test can prove it never ran."""
        from PySide6.QtWidgets import QInputDialog
        saved = QInputDialog.getItem

        def refuse(*args, **kwargs):
            raise AssertionError("the screen asked which account to use")

        QInputDialog.getItem = staticmethod(refuse)
        self.addCleanup(lambda: setattr(QInputDialog, "getItem", saved))

    def test_one_usable_account_is_used_without_asking(self):
        """Asking somebody to confirm the only answer teaches them to click
        through questions, and the next question is the one that matters."""
        self.refuse_to_ask()
        only = npcache.User("00000013", "Chris", True, "shtum_pill34",
                           account_id=ACCOUNT_ID)
        screen = self.screen([only])
        ready, why = screen.patch_ready()
        self.assertTrue(ready, why)
        self.assertIs(screen._user, only)

    def test_the_account_it_used_is_said_on_the_screen(self):
        self.refuse_to_ask()
        screen = self.screen(
            [npcache.User("00000013", "Chris", True, "shtum_pill34",
                           account_id=ACCOUNT_ID)])
        screen.patch_ready()

        class Chosen:
            name = "t5mp_ps3f.self"
            present = True
            state = flow.NOT_PATCHED

        class Writing:
            chosen = [Chosen()]
            files = [Chosen()]
        said = " ".join(screen._plan(Writing()))
        self.assertIn("shtum_pill34 (00000013)", said)

    def test_an_account_with_no_readable_name_is_not_offered(self):
        self.refuse_to_ask()
        screen = self.screen([npcache.User("00000009", "Broken", True, "")])
        ready, why = screen.patch_ready()
        self.assertFalse(ready)
        self.assertIn("sign in", why.lower())

    def test_several_accounts_are_put_to_the_user_by_name(self):
        from PySide6.QtWidgets import QInputDialog
        asked = []

        def remember(parent, title, words, items, current, editable):
            asked.append((words, list(items), current))
            return items[current], True

        saved = QInputDialog.getItem
        QInputDialog.getItem = staticmethod(remember)
        self.addCleanup(lambda: setattr(QInputDialog, "getItem", saved))

        people = [npcache.User("00000013", "A", True, "shtum_pill34",
                              account_id=ACCOUNT_ID),
                  npcache.User("00000001", "B", True, "someone_else",
                               account_id=ACCOUNT_ID + 1)]
        screen = self.screen(people)
        ready, _why = screen.patch_ready()
        self.assertTrue(ready)
        _words, items, current = asked[0]
        self.assertEqual(items, ["shtum_pill34 (00000013)",
                                 "someone_else (00000001)"])
        # The newest is first and starts selected.
        self.assertEqual(current, 0)
        self.assertIs(screen._user, people[0])


class AnAccountTheFixCanUse(unittest.TestCase):
    """What makes an account usable, and what only makes it presentable.

    Reported from a real console: the screen said no account had an
    np_cache.dat while the file was at /dev_hdd0/home/00000001/np_cache.dat.
    The account was being dropped because the online ID inside it would not
    parse, and the online ID is only ever used for the label on the picker.
    The fix reads eight bytes at the front of that file and nothing else.
    """

    def test_an_account_id_is_all_it_takes(self):
        person = npcache.User("00000001", "Chris", True, "",
                              account_id=ACCOUNT_ID)
        self.assertTrue(person.usable)

    def test_a_name_that_will_not_parse_does_not_drop_the_account(self):
        # The label falls back to the folder number. The account stays.
        lister = Lister([("00000001", "Chris",
                          struct.pack(">Q", ACCOUNT_ID) + b"\xff\xfe\x01")])
        people = npcache.users(lister)
        self.assertEqual(people[0].online_id, "")
        self.assertTrue(people[0].usable)
        self.assertEqual(people[0].label, "00000001")
        self.assertEqual([item.folder for item in npcache.with_cache(people)],
                         ["00000001"])

    def test_no_account_id_means_it_cannot_be_used(self):
        person = npcache.User("00000001", "Chris", True, "shtum_pill34")
        self.assertFalse(person.usable)

    def test_a_read_that_fails_is_not_a_file_that_is_absent(self):
        """The two must never be told as each other.

        Saying a file is not there when it is there sends somebody to sign in
        to PSN again, which they have already done, and leaves the real fault
        unreported.
        """
        class Refuses(Lister):
            def download_account_file(self, path):
                if path.endswith(npcache.NAME):
                    raise OSError("connection reset")
                return super().download_account_file(path)

        lister = Refuses([("00000001", "Chris", np_cache())])
        people = npcache.users(lister)
        self.assertTrue(people[0].has_cache)
        self.assertFalse(people[0].usable)
        self.assertIn("could not be read", people[0].unreadable)
        self.assertIn("connection reset", people[0].unreadable)
        self.assertIn("/dev_hdd0/home/00000001/np_cache.dat",
                      people[0].unreadable)
        # And the console is not one where nobody has the file.
        self.assertFalse(npcache.none_have_the_file(people))
        self.assertEqual(npcache.why_unreadable(people),
                         [people[0].unreadable])

    def test_a_console_with_no_such_file_anywhere_says_so(self):
        lister = Lister([("00000001", "Chris", None)])
        people = npcache.users(lister)
        self.assertTrue(npcache.none_have_the_file(people))
        self.assertEqual(npcache.why_unreadable(people), [])

    def test_a_file_too_short_to_hold_an_account_id_is_reported(self):
        lister = Lister([("00000001", "Chris", b"\x01\x02")])
        people = npcache.users(lister)
        self.assertTrue(people[0].has_cache)
        self.assertFalse(people[0].usable)
        self.assertTrue(people[0].no_account)
        self.assertFalse(people[0].unreadable)
        self.assertFalse(npcache.none_have_the_file(people))


class WhatTheScreenSaysWhenNoAccountWorks(unittest.TestCase):
    """Three different faults, and only one of them is answered by signing in."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.application = QApplication.instance() or QApplication([])

    def screen(self, people):
        from ps3tools.shell.screen import ConnectionState, Services
        from ps3tools.shell.theme import AppTheme
        from ps3tools.screens.patcher import BlackOpsOnePatcher
        made = BlackOpsOnePatcher(
            Services(ConnectionState(""), AppTheme("dark"), {}))
        self.addCleanup(made.deleteLater)
        made.on_scan_extras({"users": people})
        return made

    def test_no_file_anywhere_says_to_sign_in(self):
        from ps3tools.screens.patcher import ACCOUNT_MISSING
        screen = self.screen([npcache.User("00000001", "Chris", False)])
        ready, why = screen.patch_ready()
        self.assertFalse(ready)
        self.assertEqual(why, ACCOUNT_MISSING)

    def test_a_file_that_would_not_read_never_says_to_sign_in(self):
        screen = self.screen([
            npcache.User("00000001", "Chris", True,
                         unreadable="/dev_hdd0/home/00000001/np_cache.dat "
                                    "could not be read (OSError: reset)")])
        ready, why = screen.patch_ready()
        self.assertFalse(ready)
        self.assertNotIn("Sign in to PSN once", why)
        self.assertIn("could not be read", why)
        self.assertIn("/dev_hdd0/home/00000001/np_cache.dat", why)

    def test_a_usable_account_with_no_name_is_still_offered(self):
        # The reported console. The file is there, the name will not parse,
        # and the fix works perfectly well on it.
        screen = self.screen([npcache.User("00000001", "Chris", True, "",
                                           account_id=ACCOUNT_ID)])
        ready, why = screen.patch_ready()
        self.assertTrue(ready, why)
        self.assertEqual(screen._user.label, "00000001")


# --- the fake-signed digital release ---------------------------------------

class TheFileScetoolWouldNotOpen(unittest.TestCase):
    """The digital release, which used to need a second tool.

    scetool cannot open a fake-signed SELF: there is no signature for it to
    work back from, and it refuses the file for want of a keyset. That is why
    the Black Ops 1 screen used to put TrueAncestor's unfself behind it and
    tell people to go and find a copy. keysmith reads and writes them itself,
    so there is no fallback left to be missing.
    """

    def sample(self, key):
        item = sce_corpus.BY_KEY[key]
        if not item.there:
            self.skipTest(f"{item.path} is not on this machine")
        return item

    def test_the_signer_opens_one_without_any_fallback(self):
        item = self.sample("npeb00756-fself")
        signer = Signer()
        elf = signer.decrypt(item.path, "", item.klicensee)
        self.assertEqual(elf[:4], b"\x7fELF")

    def test_it_needs_no_klicensee_at_all(self):
        """Nothing in one is encrypted, so there is no key to get wrong."""
        item = self.sample("npeb00756-fself")
        self.assertEqual(Signer().decrypt(item.path, "", None),
                         Signer().decrypt(item.path, "", item.klicensee))

    def test_the_screen_no_longer_layers_a_second_tool_behind_it(self):
        from ps3tools.screens import patcher as patcher_screen
        self.assertIs(
            patcher_screen.BlackOpsOnePatcher._scetool,
            patcher_screen.PatcherScreen._scetool,
            "the Black Ops 1 screen should use the ordinary signer")

    def test_a_zeroed_npdrm_block_is_reported_as_the_8001000f_cause(self):
        """Every fake-signed file here carries one, and it is why they fail."""
        item = self.sample("npeb00756-fself")
        with self.assertRaises(ScetoolError) as caught:
            Signer().info(item.path, item.klicensee)
        message = str(caught.exception)
        self.assertIn("8001000F", message)
        self.assertIn("stock", message)


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
            bo1.apply(IMAGE, "EP0002-BLES01031_00-CODBLOPSPATCH012")[0]
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
        content_id = content_id_for("BLES01031")
        result = flow.patch(
            writer, tool, report, root=self.workdir,
            extra_files=(npcache.place(writer, content_id, raw,
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
        remote = npcache.destination(content_id_for("BLES01031"))
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
        self.assertIn(npcache.destination(content_id_for("BLES01031")).encode("ascii"), landed)

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
        landed = self.server.written[npcache.destination(content_id_for("BLES01031"))]
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
        self.assertIn(npcache.destination(content_id_for("BLES01031")), said)
        self.assertIn("signed in", said)

    def test_the_copy_is_not_counted_as_a_patched_binary(self):
        _report, result, _files = self.run_it()
        self.assertEqual(result.changed, ["t5mp_ps3f.self"])
        self.assertNotIn(npcache.destination(content_id_for("BLES01031")), result.uploaded)


if __name__ == "__main__":
    unittest.main()


class AccountsOverARealLister(unittest.TestCase):
    """npcache.users() against ps3diag.transport.FtpLister itself.

    Every other test in this file hands users() a fake. A fake grows whatever
    method the code under test asks for, so npcache spent a release calling
    lister.retrieve_bytes, which FtpWriter has and FtpLister does not, and the
    screen failed on every console with an AttributeError while the suite
    stayed green. These tests drive the real class over a real socket to a
    mock console, so the two cannot drift apart again.
    """

    def setUp(self):
        import ftplib
        import mock_webman
        from ps3diag import transport

        self.server = mock_webman.MockWebmanFtp().start()
        self.addCleanup(self.server.stop)

        def connect():
            ftp = ftplib.FTP()
            ftp.connect("127.0.0.1", self.server.port, timeout=5)
            ftp.login("anonymous", "ps3-diag@localhost")
            return ftp

        self.lister = transport.FtpLister("127.0.0.1", factory=connect)
        self.addCleanup(self.lister.close)

    def test_users_reads_the_accounts_off_a_real_lister(self):
        found = npcache.users(self.lister)
        self.assertTrue(found)
        with_cache = [user for user in found if user.has_cache]
        self.assertTrue(with_cache, "the mock console has an np_cache.dat")
        for user in with_cache:
            self.assertEqual(user.unreadable, "")
            self.assertIsNotNone(user.account_id)

    def test_read_for_returns_bytes_off_a_real_lister(self):
        folder = next(user.folder for user in npcache.users(self.lister)
                      if user.has_cache)
        raw = npcache.read_for(self.lister, folder)
        self.assertIsInstance(raw, bytes)
        self.assertGreaterEqual(len(raw), npcache.ACCOUNT_ID_BYTES)

    def test_every_method_the_patchers_ask_for_exists_on_the_real_classes(
            self):
        """The general form of the same bug, read out of the source.

        Anything the patching code calls on a lister or a writer has to be a
        real method on the real class. A fake grows whatever it is asked for,
        so this is read from the source rather than from a test double, and it
        catches the next mismatch without anybody remembering to write a test
        for it.
        """
        import ast
        import glob
        from ps3diag import transport
        from ps3tools.patching import ftpwrite

        classes = {"lister": transport.FtpLister, "writer": ftpwrite.FtpWriter}
        # npcache is handed whichever connection is open, so its argument name
        # says "lister" while the whole run passes the writer. Both classes
        # have to answer everything it asks for.
        both = (transport.FtpLister, ftpwrite.FtpWriter)
        tree = ast.parse(open(npcache.__file__).read())
        asked = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "lister"}
        self.assertTrue(asked, "npcache calls something on its connection")
        for owner in both:
            absent = sorted(name for name in asked if not hasattr(owner, name))
            self.assertEqual(absent, [], f"{owner.__name__} lacks {absent}")

        seen = 0
        missing = []
        for path in sorted(glob.glob("ps3tools/patching/*.py")
                           + glob.glob("ps3tools/screens/patcher.py")):
            tree = ast.parse(open(path).read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)):
                    continue
                owner = classes.get(node.func.value.id)
                if owner is None:
                    continue
                seen += 1
                if not hasattr(owner, node.func.attr):
                    missing.append(f"{path}: {owner.__name__} has no "
                                   f"{node.func.attr}")
        self.assertGreater(seen, 0, "the patchers call something")
        self.assertEqual(missing, [], "\n".join(missing))
