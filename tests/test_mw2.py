"""The Modern Warfare 2 stats fix: the signature, the checks and the cave.

Two kinds of test, and the second is the one that counts.

Most of what is here runs against an image built in this file. The fix finds
its hook by signature rather than by an address, so a test that only fed it a
copy of the game would be testing one build; this builds an image with the
landmarks where it wants them and then moves them, which is the thing that has
to keep working.

The rest runs against the real files when they are on the machine, and against
the build this fix was taken from. That is the gate: the instruction written
at the hook, the forty four bytes of the cave, and the program header change
all have to come out byte for byte the same as the build somebody has actually
played. A test asserting this program's output against this program's own
reader would prove nothing.
"""

import hashlib
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sce"))

import corpus as sce_corpus                                # noqa: E402
from ps3diag import patchstate                             # noqa: E402
from ps3tools import titles                                # noqa: E402
from ps3tools.patching import flow                         # noqa: E402

mw2 = patchstate.patcher_module("mw2")


# --- an image with the landmarks where this file wants them ----------------

BASE = 0x10000
#: Big enough to hold the cave and the identity cache pointer, both of which
#: are at fixed addresses in this fix. Zeros except where a test puts
#: something, which is also what the real file holds around the cave.
SIZE = 0x720000
CODE_FILESZ = 0x6DA960          # as the stock build ships it
DATA_OFFSET = 0x6E0000
DATA_VADDR = 0x6F0000
DATA_FILESZ = 0x40000
NOP = 0x60000000

#: Where the test puts the hook. Deliberately not the address the real build
#: has it at: nothing in the fix knows this number.
HOOK = 0x00123456 & ~3


def build_image(hook=HOOK, anchors=1, cache=None, displaced=None,
                continuation=None, code_filesz=CODE_FILESZ):
    """An ELF carrying what the fix looks for, with everything adjustable."""
    data = bytearray(SIZE)
    data[0:16] = b"\x7fELF\x02\x02\x01" + b"\x00" * 9
    struct.pack_into(">HHI", data, 0x10, 2, 0x15, 1)
    struct.pack_into(">Q", data, 0x18, DATA_VADDR + 0xA960)   # e_entry
    struct.pack_into(">Q", data, 0x20, 0x40)                  # e_phoff
    struct.pack_into(">HHH", data, 0x36, 0x38, 2, 0x38)       # entsize, phnum

    def header(index, flags, offset, vaddr, filesz):
        base = 0x40 + index * 0x38
        struct.pack_into(">II", data, base, 1, flags)
        struct.pack_into(">QQQQQQ", data, base + 8, offset, vaddr, vaddr,
                         filesz, filesz, 0x10000)

    header(0, 0x400005, 0, BASE, code_filesz)
    header(1, 0x600006, DATA_OFFSET, DATA_VADDR, DATA_FILESZ)

    for index in range(anchors):
        # Every copy of the anchor, at file offsets: the addresses in this
        # file are virtual and the bytes go in at offsets, which is the one
        # mistake in this exercise that produces a plausible looking image.
        at = (hook - BASE - len(mw2.ANCHOR)) + index * 0x400
        data[at:at + len(mw2.ANCHOR)] = mw2.ANCHOR
        site = at + len(mw2.ANCHOR)
        struct.pack_into(">I", data, site,
                         mw2.HOOK_STOCK if displaced is None else displaced)
        struct.pack_into(">I", data, site + 4,
                         mw2.CONTINUATION if continuation is None
                         else continuation)
    struct.pack_into(">I", data, mw2.CACHE_POINTER - BASE,
                     mw2.CACHE_POINTER_VALUE if cache is None else cache)
    return bytes(data)


def word_at(data, vaddr):
    return struct.unpack_from(">I", data, vaddr - BASE)[0]


def headers(data):
    out = []
    for index in range(struct.unpack_from(">H", data, 0x38)[0]):
        base = 0x40 + index * 0x38
        out.append(struct.unpack_from(">QQQQQQ", data, base + 8))
    return out


class TheSignature(unittest.TestCase):
    """Finding the hook, wherever the build happens to have put it."""

    def test_the_hook_is_found_by_its_signature_and_not_by_an_address(self):
        for hook in (0x00123454, 0x00200000, 0x0040FFF0):
            with self.subTest(hook=hook):
                data = build_image(hook=hook)
                offset, state = mw2.find_site(data)
                self.assertEqual(offset, hook - BASE)
                self.assertEqual(state, mw2.STOCK)

    def test_the_anchor_alone_is_not_the_signature(self):
        """It appears four times in the real image, so the instruction after
        the site is what tells the copies apart."""
        data = build_image(anchors=3)
        # Only the first copy carries the continuation this build's parser
        # has, because build_image writes it for every copy; blank the others.
        out = bytearray(data)
        for index in (1, 2):
            at = HOOK - BASE + index * 0x400 + 4
            struct.pack_into(">I", out, at, NOP)
        offset, state = mw2.find_site(bytes(out))
        self.assertEqual(offset, HOOK - BASE)
        self.assertEqual(state, mw2.STOCK)

    def test_two_places_that_both_match_are_refused(self):
        data = build_image(anchors=2)
        with self.assertRaises(mw2.NotThisBuild) as caught:
            mw2.find_site(data)
        self.assertIn("matched 2 places", str(caught.exception))

    def test_a_build_without_the_parser_is_refused(self):
        data = bytearray(build_image())
        at = HOOK - len(mw2.ANCHOR) - BASE
        data[at:at + len(mw2.ANCHOR)] = bytes(len(mw2.ANCHOR))
        with self.assertRaises(mw2.NotThisBuild) as caught:
            mw2.find_site(bytes(data))
        self.assertIn("was not found", str(caught.exception))

    def test_something_that_is_not_an_elf_is_refused(self):
        with self.assertRaises(mw2.NotThisBuild):
            mw2.find_site(b"not an ELF" + bytes(0x1000))


class TheChecks(unittest.TestCase):
    """The three facts, each refused by name."""

    def test_a_stock_build_passes_all_three(self):
        facts = mw2.check(build_image())
        self.assertEqual(facts["hook"], HOOK)
        self.assertEqual(facts["cache"], mw2.CACHE_POINTER_VALUE)

    def test_the_wrong_instruction_after_the_hook_is_named(self):
        data = build_image(continuation=NOP)
        with self.assertRaises(mw2.NotThisBuild) as caught:
            mw2.check(data)
        self.assertIn(f"{mw2.CONTINUATION:08X}", str(caught.exception))

    def test_the_wrong_value_at_the_cache_pointer_is_named(self):
        data = build_image(cache=0x01020304)
        with self.assertRaises(mw2.NotThisBuild) as caught:
            mw2.check(data)
        message = str(caught.exception)
        self.assertIn(f"{mw2.CACHE_POINTER:08X}", message)
        self.assertIn("01020304", message)

    def test_a_check_that_failed_means_nothing_is_written(self):
        data = build_image(cache=0)
        with self.assertRaises(mw2.NotThisBuild):
            mw2.apply(data)


class TheCave(unittest.TestCase):
    """The code itself, against the bytes it was taken from."""

    #: The cave in the build this fix came from, at its own addresses.
    REFERENCE = bytes.fromhex(
        "80010098e90100b02c2800004182001c3d600073816bbacc"
        "f90b00087c2004ac39400001994b00044bd172cc")

    def test_it_is_the_code_the_reference_build_carries(self):
        self.assertEqual(mw2.cave_bytes(0x004033F0, 0x006EC100),
                         self.REFERENCE)

    def test_the_hook_branch_is_the_one_the_reference_build_carries(self):
        self.assertEqual(mw2.branch(0x004033F0, 0x006EC100), 0x482E8D10)

    def test_the_branch_home_returns_to_the_instruction_after_the_hook(self):
        hook, cave = 0x00200000, 0x006EC100
        words = mw2.cave_words(hook, cave)
        home = cave + (len(words) - 1) * 4
        distance = words[-1] & 0x03FFFFFC
        if distance & (1 << 25):
            distance -= (1 << 26)
        self.assertEqual(home + distance, hook + 4)

    def test_the_pointer_pair_builds_the_cache_address(self):
        words = mw2.cave_words(0x00200000, 0x006EC100, pointer=0x00812345 & ~3)
        high = words[4] & 0xFFFF
        low = words[5] & 0xFFFF
        if low & 0x8000:
            low -= 0x10000
        self.assertEqual((high << 16) + low, 0x00812345 & ~3)

    def test_the_valid_flag_is_written_after_the_identity(self):
        """The fast path must not see a valid flag over half an identity."""
        words = mw2.cave_words(0x00200000, 0x006EC100)
        identity = next(index for index, word in enumerate(words)
                        if word >> 26 == 62)          # std
        barrier = words.index(0x7C2004AC)             # lwsync
        flag = next(index for index, word in enumerate(words)
                    if word >> 26 == 38)              # stb
        self.assertLess(identity, barrier)
        self.assertLess(barrier, flag)

    def test_the_zero_guard_skips_the_write(self):
        words = mw2.cave_words(0x00200000, 0x006EC100)
        compare = words.index(0x2C280000)
        conditional = words[compare + 1]
        distance = conditional & 0xFFFC
        if distance & 0x8000:
            distance -= 0x10000
        target = 0x006EC100 + (compare + 1) * 4 + distance
        self.assertEqual(target, 0x006EC100 + (len(words) - 1) * 4)


class TheExecutableCoverage(unittest.TestCase):
    """The cave has to be in the part of the file the console loads as code."""

    def test_a_stock_shaped_image_does_not_reach_the_cave(self):
        covered, index, filesz, wanted = mw2.coverage(build_image())
        self.assertFalse(covered)
        self.assertEqual(index, 0)
        self.assertEqual(filesz, CODE_FILESZ)
        self.assertEqual(wanted, DATA_OFFSET)

    def test_applying_grows_the_segment_and_says_so(self):
        patched, what = mw2.apply(build_image())
        self.assertFalse(what["was_executable"])
        self.assertEqual(what["filesz_was"], CODE_FILESZ)
        self.assertEqual(what["filesz_now"], DATA_OFFSET)
        filesz, memsz = headers(patched)[0][3], headers(patched)[0][4]
        self.assertEqual(filesz, DATA_OFFSET)
        # Both together: a segment that maps less than it holds would put the
        # cave in the file and not in memory.
        self.assertEqual(memsz, DATA_OFFSET)

    def test_a_segment_that_already_covers_the_cave_is_left_alone(self):
        data = build_image(code_filesz=DATA_OFFSET)
        covered, _index, filesz, wanted = mw2.coverage(data)
        self.assertTrue(covered)
        patched, what = mw2.apply(data)
        self.assertTrue(what["was_executable"])
        self.assertEqual(headers(patched)[0][3], DATA_OFFSET)
        self.assertEqual(headers(data)[0], headers(patched)[0])

    def test_the_other_segments_are_not_touched(self):
        patched, _what = mw2.apply(build_image())
        self.assertEqual(headers(build_image())[1], headers(patched)[1])


class TheRoundTrip(unittest.TestCase):
    def setUp(self):
        self.stock = build_image()
        self.patched, self.what = mw2.apply(self.stock)

    def test_the_hook_becomes_a_branch_to_the_cave(self):
        self.assertEqual(word_at(self.patched, HOOK),
                         mw2.branch(HOOK, mw2.CAVE))

    def test_the_cave_is_written_where_it_says_it_is(self):
        at = mw2.CAVE - BASE
        self.assertEqual(self.patched[at:at + mw2.CAVE_BYTES],
                         mw2.cave_bytes(HOOK, mw2.CAVE))

    def test_nothing_else_in_the_file_changes(self):
        changed = [index for index in range(len(self.stock))
                   if self.stock[index] != self.patched[index]]
        runs = []
        for index in changed:
            if runs and index == runs[-1][1]:
                runs[-1][1] = index + 1
            else:
                runs.append([index, index + 1])
        # The two program header size fields, the hook, and the cave. Three
        # places and no others.
        self.assertTrue(all(start >= 0x40 and end <= 0x78
                            or start == HOOK - BASE
                            or mw2.CAVE - BASE <= start
                            and end <= mw2.CAVE - BASE + mw2.CAVE_BYTES
                            for start, end in runs), runs)

    def test_it_reads_back_as_patched(self):
        self.assertEqual(mw2.find_site(self.patched)[1], mw2.PATCHED)

    def test_applying_twice_changes_nothing(self):
        again, what = mw2.apply(self.patched)
        self.assertEqual(again, self.patched)
        self.assertTrue(what["already"])

    def test_restoring_puts_the_instruction_back_and_wipes_the_cave(self):
        back = mw2.restore(self.patched)
        self.assertEqual(word_at(back, HOOK), mw2.HOOK_STOCK)
        at = mw2.CAVE - BASE
        self.assertEqual(back[at:at + mw2.CAVE_BYTES],
                         bytes(mw2.CAVE_BYTES))
        self.assertEqual(mw2.find_site(back)[1], mw2.STOCK)

    def test_somebody_elses_patch_is_left_alone(self):
        meddled = bytearray(self.stock)
        struct.pack_into(">I", meddled, HOOK - BASE, 0x48000004)
        self.assertIsNone(mw2.find_site(bytes(meddled))[1])
        with self.assertRaises(mw2.NotThisBuild) as caught:
            mw2.apply(bytes(meddled))
        self.assertIn("left alone", str(caught.exception))

    def test_a_cave_that_is_not_free_space_is_refused(self):
        data = bytearray(self.stock)
        data[mw2.CAVE - BASE + 8] = 0x01
        with self.assertRaises(mw2.NotThisBuild) as caught:
            mw2.apply(bytes(data))
        self.assertIn("not all zero", str(caught.exception))


class TheWiring(unittest.TestCase):
    """The rest of the program knowing about this fix at all."""

    def test_the_title_table_carries_the_game(self):
        config = titles.config_for("BLES00683")
        self.assertEqual(config["key"], "mw2")
        self.assertEqual(config["binaries"][0]["name"], "default_mp.self")
        self.assertEqual(config["binaries"][0]["site"], "mw2-multiplayer")
        self.assertEqual(config["binaries"][0]["klicensee"],
                         "496E66696E697479576172644B657900")

    def test_the_site_is_located_by_pattern(self):
        site = titles.PATCH_SITES["mw2-multiplayer"]
        self.assertEqual(site["located_by"], "pattern")
        self.assertNotIn("file_offset", site)

    def test_every_region_the_fix_was_published_for_is_recognised(self):
        for title_id in ("BLES00683", "BLES00684", "BLES00685", "BLES00686",
                         "BLES00687", "BLES00690", "BLUS30377"):
            self.assertTrue(titles.is_recognised(title_id), title_id)
            self.assertEqual(titles.config_for(title_id)["key"], "mw2")

    def test_nothing_claims_this_program_has_seen_it_work(self):
        """Somebody else's build has been played. This one has not."""
        self.assertEqual(titles.TITLES["mw2"]["skus"], {})
        self.assertFalse(titles.is_verified("BLES00683"))

    def test_a_folder_of_this_name_is_a_candidate_for_both_titles(self):
        """Modern Warfare 2 and 3 both ship a default_mp.self, so the name
        alone settles nothing and the title ID has to."""
        found = titles.keys_for_files(["default_mp.self", "default.self"])
        self.assertIn("mw2", found)
        self.assertIn("mw3", found)

    def test_the_flow_applies_it(self):
        image = build_image()
        patched, offset = flow.apply_fix(image, "mw2", mw2)
        self.assertEqual(offset, HOOK - BASE)
        self.assertEqual(mw2.find_site(patched)[1], mw2.PATCHED)

    def test_the_patch_state_reads_a_decrypted_image(self):
        stock = build_image()
        self.assertEqual(patchstate.decrypted_state(stock, "mw2")["state"],
                         patchstate.UNPATCHED)
        patched, _what = mw2.apply(stock)
        found = patchstate.decrypted_state(patched, "mw2")
        self.assertEqual(found["state"], patchstate.PATCHED)
        self.assertEqual(found["confidence"], patchstate.HIGH)

    def test_a_build_it_does_not_know_is_unknown_rather_than_unpatched(self):
        found = patchstate.decrypted_state(build_image(cache=0), "mw2")
        self.assertEqual(found["state"], patchstate.UNKNOWN)


class TheApplyPath(unittest.TestCase):
    """What Apply actually runs, which is not what the rest of this file runs.

    Everything above calls the fix directly. Apply goes through
    flow._build_all: decrypt, apply the fix, sign, decrypt the result again
    and check it. That path looks several things up by title key, and one of
    them was a plain subscript into a table with no entry for this game, so a
    console with a real install got "KeyError: 'mw2'" after a scan that had
    gone perfectly. Nothing in the suite ran that path for this title.
    """

    class Tool:
        """A signer that keeps the bytes and none of the cryptography.

        The container is a prefix and the ELF. That is enough for this: what
        is being tested is the flow around signing, and keysmith's own round
        trips are what test the signing.
        """

        PREFIX = b"SELF"
        key_revision = None

        def __init__(self, info):
            self.fields = dict(info)
            self.signed = []

        def decrypt(self, source, destination, klicensee=None):
            with open(source, "rb") as handle:
                body = handle.read()
            if body.startswith(self.PREFIX):
                body = body[len(self.PREFIX):]
            if destination:
                with open(destination, "wb") as handle:
                    handle.write(body)
            return body

        def sign(self, profile, info, source, elf_path, destination,
                 target_name, klicensee=None):
            self.signed.append((profile, target_name))
            with open(elf_path, "rb") as handle:
                elf = handle.read()
            with open(destination, "wb") as handle:
                handle.write(self.PREFIX + elf)
            return destination

        def info(self, path, klicensee=None):
            return dict(self.fields)

    class Saved:
        def __init__(self, paths):
            self.paths = paths

        def entry_for(self, name):
            return {"path": self.paths[name]}

    #: What the scan reads off a real file, enough for the checks the build
    #: path makes. The carried fields are what a rebuilt file has to come
    #: back with unchanged.
    INFO = {"content_id": "EP0002-BLES00683_00-IW4PATCH0000000",
            "self_type": "APP", "app_type": "NPDRM", "licence_type": "free",
            "auth_id": "1010000001000003", "vendor_id": "01000002",
            "app_version": "0001000000000000",
            "fw_version": "0004000000000000", "key_revision": "0010",
            "cid_fn_hash": "00" * 16}

    def setUp(self):
        import tempfile
        from ps3tools.patching.flow import FileScan, PatchReport
        self.workdir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.workdir,
                        ignore_errors=True)
        config = titles.config_for("BLES00683")
        record = config["binaries"][0]
        self.item = FileScan(record["name"], record,
                             "/dev_hdd0/game/BLES00683/USRDIR/"
                             + record["name"])
        self.item.info = dict(self.INFO)
        self.source = os.path.join(self.workdir, record["name"])
        with open(self.source, "wb") as handle:
            handle.write(self.Tool.PREFIX + build_image())
        self.saved = self.Saved({record["name"]: self.source})
        self.tool = self.Tool(self.INFO)
        self.out = PatchReport("BLES00683")

    def build(self):
        return flow._build_all(self.tool, [self.item], self.saved, "mw2",
                               mw2, self.workdir, None, self.out)

    def test_the_whole_build_runs_for_this_title(self):
        built = self.build()
        self.assertEqual(sorted(built), [self.item.name])
        self.assertTrue(os.path.isfile(built[self.item.name]))

    def test_what_comes_out_is_the_patched_binary(self):
        built = self.build()
        with open(built[self.item.name], "rb") as handle:
            body = handle.read()[len(self.Tool.PREFIX):]
        self.assertEqual(mw2.find_site(body)[1], mw2.PATCHED)
        self.assertEqual(body, mw2.apply(build_image())[0])

    def test_the_file_is_signed_under_the_name_it_will_have(self):
        """The CID_FN hash binds the content ID to the file name, so a file
        signed under the wrong one is valid and will not load."""
        self.build()
        self.assertEqual([name for _profile, name in self.tool.signed],
                         [self.item.name])

    def test_a_file_that_comes_back_wrong_stops_the_run(self):
        """The check that the rebuilt file decrypts to what went into it."""
        def wrong(source, destination, klicensee=None):
            return b"not what went in"

        self.tool.decrypt_real = self.tool.decrypt

        calls = []

        def decrypt(source, destination, klicensee=None):
            calls.append(source)
            if len(calls) == 1:
                return self.tool.decrypt_real(source, destination, klicensee)
            return wrong(source, destination, klicensee)

        self.tool.decrypt = decrypt
        with self.assertRaises(flow.PatchFailed) as caught:
            self.build()
        self.assertIn("does not decrypt back", str(caught.exception))

    def test_a_carried_field_that_changed_stops_the_run(self):
        """The content ID is one of the five that have to come back the same.

        A file signed with the wrong one of those is perfectly valid and will
        not load, which is the failure that looks like nothing at all.
        """
        self.tool.fields["content_id"] = "EP0002-BLES99999_00-SOMETHINGELSE00"
        with self.assertRaises(flow.PatchFailed) as caught:
            self.build()
        self.assertIn("different", str(caught.exception))


# --- the real files --------------------------------------------------------

HOME = sce_corpus.HOME
STOCK_SELF = os.path.join(HOME, "mw2-BLES00683", "stock", "default_mp.self")
REFERENCE = os.path.join(HOME, "IW4-Binaries", "release", "bles00683", "cfw",
                         "default_mp.self")
SECOND_REGION = os.path.join(HOME, "IW4-Binaries", "release", "blus30377",
                             "cfw", "default_mp.self")

#: The decrypted multiplayer ELF of title update 1.14.
STOCK_SHA256 = ("a2e79a8498dd63bebbf899eb04cc5928574fd229"
                "df52c082a8f01184906237a6")

#: Where the reference build has the hook and the cave. Recorded so that this
#: file says what was measured, and never used to find anything.
REFERENCE_HOOK = 0x004033F0
REFERENCE_CAVE = 0x006EC100


def decrypted(path):
    from ps3tools import keysmith
    return keysmith.decrypt(path, sce_corpus.IW_KLIC)


class TheRealBuild(unittest.TestCase):
    """The gate: this program's output against the build it came from.

    Skipped where the files are not on the machine, which is every checkout
    but the one they were developed on.
    """

    @classmethod
    def setUpClass(cls):
        for path in (STOCK_SELF, REFERENCE):
            if not os.path.isfile(path):
                raise unittest.SkipTest(f"{path} is not on this machine")
        try:
            from ps3tools.keysmith.keys import KeyStore     # noqa: F401
        except Exception as error:                          # pragma: no cover
            raise unittest.SkipTest(f"the keyset is not here: {error}")
        try:
            cls.stock = decrypted(STOCK_SELF)
            cls.reference = decrypted(REFERENCE)
        except Exception as error:
            raise unittest.SkipTest(f"the files would not decrypt: {error}")
        cls.patched, cls.what = mw2.apply(cls.stock)

    def test_the_build_is_the_one_the_figures_were_measured_on(self):
        self.assertEqual(hashlib.sha256(self.stock).hexdigest(), STOCK_SHA256)

    def test_the_hook_is_where_it_was_measured(self):
        self.assertEqual(self.what["hook"], REFERENCE_HOOK)
        self.assertEqual(self.what["cave"], REFERENCE_CAVE)

    def test_the_three_checks_hold_on_the_real_file(self):
        facts = mw2.check(self.stock)
        self.assertEqual(facts["cache"], mw2.CACHE_POINTER_VALUE)
        self.assertEqual(struct.unpack_from(">I", self.stock,
                                            facts["offset"])[0],
                         mw2.HOOK_STOCK)
        self.assertEqual(struct.unpack_from(">I", self.stock,
                                            facts["offset"] + 4)[0],
                         mw2.CONTINUATION)

    def test_the_instruction_written_is_the_reference_build_s(self):
        at = REFERENCE_HOOK - BASE
        self.assertEqual(self.patched[at:at + 4], self.reference[at:at + 4])

    def test_the_cave_written_is_the_reference_build_s(self):
        at = REFERENCE_CAVE - BASE
        length = mw2.CAVE_BYTES
        self.assertEqual(self.patched[at:at + length],
                         self.reference[at:at + length])

    def test_the_segment_is_grown_the_way_the_reference_build_grew_it(self):
        self.assertFalse(self.what["was_executable"])
        self.assertEqual(headers(self.patched)[0][3],
                         headers(self.reference)[0][3])
        self.assertEqual(headers(self.patched)[0][4],
                         headers(self.reference)[0][4])

    def test_the_reference_build_reads_as_already_patched(self):
        """It carries a great deal this program did not write, and the state
        this program reports is still the right one."""
        offset, state = mw2.find_site(self.reference)
        self.assertEqual(offset, REFERENCE_HOOK - BASE)
        self.assertEqual(state, mw2.PATCHED)
        self.assertEqual(
            patchstate.decrypted_state(self.reference, "mw2")["state"],
            patchstate.PATCHED)

    def test_the_stock_file_reads_as_unpatched(self):
        self.assertEqual(mw2.find_site(self.stock)[1], mw2.STOCK)

    def test_restoring_puts_the_real_file_back(self):
        back = mw2.restore(self.patched)
        at = REFERENCE_HOOK - BASE
        self.assertEqual(back[at:at + 4], self.stock[at:at + 4])
        cave = REFERENCE_CAVE - BASE
        self.assertEqual(back[cave:cave + mw2.CAVE_BYTES],
                         self.stock[cave:cave + mw2.CAVE_BYTES])

    def test_the_second_region_decrypts_to_the_same_image(self):
        """Which is what says region alone changes nothing on this update."""
        if not os.path.isfile(SECOND_REGION):
            self.skipTest(f"{SECOND_REGION} is not on this machine")
        other = decrypted(SECOND_REGION)
        self.assertEqual(hashlib.sha256(other).hexdigest(),
                         hashlib.sha256(self.reference).hexdigest())


if __name__ == "__main__":
    unittest.main()
