"""keysmith, checked against real files rather than against itself.

A test that asserts this package's output against this package's own parser
proves nothing, so the gates here are all external: the NIST vectors for the
cipher, scetool's own report for the parser, scetool's own decrypt and
TrueAncestor's unfself for the plaintext, and byte equality against the user's
unmodified retail files for the round trip.

Those retail files are not in the repository. They are several hundred
megabytes of game binaries and they live on the machine this was built on, so
every test that needs one skips with a message naming the file when it is not
there. The two small EBOOTs round trip in a tenth of a second and always run;
the large ones take about ten seconds each and run when KEYSMITH_CORPUS is set,
because the whole suite is already eleven minutes long.
"""

import hashlib
import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "sce"))

import corpus                                             # noqa: E402
from ps3tools import keysmith                             # noqa: E402
from ps3tools.keysmith import aes, ecdsa, fself, keys, npdrm  # noqa: E402
from ps3tools.keysmith.self import SelfFile                # noqa: E402

# keysmith.sign is the public function, so the name of the module it lives in
# is taken. Reaching the module itself goes through sys.modules.
import ps3tools.keysmith.sign                              # noqa: E402,F401
signing = sys.modules["ps3tools.keysmith.sign"]

FULL = os.environ.get("KEYSMITH_CORPUS", "") not in ("", "0")

#: The keyset is not redistributed with the source, so a fresh clone has no
#: copy and everything that needs one skips rather than failing.
HAVE_KEYS = os.path.isfile(keys.default_path())

#: Small enough to round trip in a tenth of a second, so they run every time.
QUICK = ("mw3-eboot", "mw2-eboot")

#: Where the curve table lives, which is beside the keys file and is not
#: redistributed either. Everything that reads it skips without it.
HAVE_CURVES = os.path.isfile(ecdsa.default_path())

#: The paired PS3HEN and custom firmware binaries the survey was run
#: against, cloned beside the rest of the user's files. Published by a
#: third party; the folder name is simply where they sit on this machine.
#: He ships a CFW build and a HEN build of the same Modern Warfare 2 ELF for
#: all seven regions, which is the external gate for what a HEN console wants.
PAIRED_BUILDS = os.path.join(corpus.HOME, "IW4-Binaries")


def need_keys():
    if not HAVE_KEYS:
        raise unittest.SkipTest(
            f"no keyset at {keys.default_path()}; see docs/keysmith.md")


def need_curves():
    need_keys()
    if not HAVE_CURVES:
        raise unittest.SkipTest(
            f"no curve table at {ecdsa.default_path()}; see docs/keysmith.md")


def signature_input(parsed, klicensee):
    """(the bytes a SELF's signature covers, the signature itself).

    What is covered is signature_input_length bytes from the start of the
    file with the metadata run and the metadata info block in the clear,
    which is the form the header has when scetool signs it and before it
    encrypts the run. Built here from the file's own parts rather than asked
    of this package's signing code, so that a test of a signature is not a
    test of the thing that made it.
    """
    from ps3tools.keysmith.structs import MetadataInfo, SceHeader
    metadata = parsed.decrypt_metadata(bytes.fromhex(klicensee))
    run_at = parsed.sce.metadata_offset + SceHeader.SIZE + MetadataInfo.SIZE
    plain = bytearray(parsed.raw[:parsed.sce.header_length])
    plain[run_at - MetadataInfo.SIZE:run_at] = metadata.info.pack()
    blob = (metadata.header.pack()
            + b"".join(section.pack() for section in metadata.sections)
            + bytes(metadata.keys) + bytes(metadata.optional)
            + bytes(metadata.signature))
    plain[run_at:run_at + len(blob)] = blob
    covered = bytes(plain)[:metadata.header.signature_input_length]
    return covered, bytes(metadata.signature)


def sample(key):
    need_keys()
    item = corpus.BY_KEY[key]
    if not item.there:
        raise unittest.SkipTest(f"{item.path} is not on this machine")
    return item


def chosen():
    for item in corpus.SAMPLES:
        if not item.there:
            continue
        if FULL or item.key in QUICK:
            yield item


class TheCipher(unittest.TestCase):
    """AES, against the published vectors and nothing else."""

    def test_the_fips_197_block(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(aes.ecb_encrypt(key, plain).hex(),
                         "69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(
            aes.ecb_decrypt(key, bytes.fromhex(
                "69c4e0d86a7b0430d8cdb78070b4c55a")), plain)

    def test_the_longer_keys(self):
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        for key, want in (
                ("000102030405060708090a0b0c0d0e0f1011121314151617",
                 "dda97ca4864cdfe06eaf70a0ec0d7191"),
                ("000102030405060708090a0b0c0d0e0f"
                 "101112131415161718191a1b1c1d1e1f",
                 "8ea2b7ca516745bfeafc49904b496089")):
            with self.subTest(bits=len(key) * 4):
                self.assertEqual(
                    aes.ecb_encrypt(bytes.fromhex(key), plain).hex(), want)

    def test_counter_mode_against_sp_800_38a(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        counter = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"
                              "ae2d8a571e03ac9c9eb76fac45af8e51")
        self.assertEqual(
            aes.ctr_crypt(key, counter, plain).hex(),
            "874d6191b620e3261bef6864990db6ce"
            "9806f66b7970fdff8617187bb9fffdff")

    def test_chaining_mode_against_sp_800_38a(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"
                              "ae2d8a571e03ac9c9eb76fac45af8e51")
        cipher = aes.cbc_encrypt(key, iv, plain)
        self.assertEqual(cipher.hex(),
                         "7649abac8119b246cee98e9b12e9197d"
                         "5086cb9b507219ee95db113a917678b2")
        self.assertEqual(aes.cbc_decrypt(key, iv, cipher), plain)

    def test_counter_mode_is_its_own_inverse(self):
        key = bytes(range(16))
        counter = bytes(range(16, 32))
        blob = bytes(range(256)) * 7
        self.assertEqual(
            aes.ctr_crypt(key, counter, aes.ctr_crypt(key, counter, blob)),
            blob)


class TheNpdrmHashes(unittest.TestCase):
    """Both are AES-OMAC1, and both are checked against the real files.

    These were the one thing this package could not reproduce for a while, so
    every claim here is against a value somebody else's tool wrote, never
    against our own.
    """

    #: The name each file has on the console, which is what CID_FN binds to.
    NAMES = {"bo1-blus30591": "t5mp_ps3f.self",
             "mw3-bles01428": "default_mp.self",
             "npeb00756-fself": "t5mp_ps3f.self",
             "npub30787-fself": "default_mp.self"}

    def name_of(self, item):
        return self.NAMES.get(item.key, os.path.basename(item.path))

    def test_omac1_against_rfc_4493(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        message = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"
                                "ae2d8a571e03ac9c9eb76fac45af8e51"
                                "30c81c46a35ce411e5fbc1191a0a52ef"
                                "f69f2445df4f9b17ad2b417be66c3710")
        for length, want in ((0, "bb1d6929e95937287fa37d129b756746"),
                             (16, "070a16b46b4d4144f79bdd9dd04a287c"),
                             (40, "dfa66747de9ae63030ca32611497c827"),
                             (64, "51f0bebf7e3b9d92fc49741779363cfe")):
            with self.subTest(length=length):
                self.assertEqual(aes.omac1(key, message[:length]).hex(), want)

    def test_the_cid_fn_hash_is_reproduced_for_every_file(self):
        store = keys.load() if HAVE_KEYS else self.skipTest("no keyset")
        ran = 0
        for item in chosen():
            if item.kind != "self":
                continue
            with self.subTest(sample=item.key):
                block = keysmith.read(item.path).npdrm
                self.assertEqual(
                    npdrm.cid_fn_hash(store, block.content_id,
                                      self.name_of(item)),
                    block.cid_fn_hash)
                ran += 1
        if not ran:
            self.skipTest("none of the corpus is on this machine")

    def test_the_ci_hash_is_reproduced_for_every_file(self):
        store = keys.load() if HAVE_KEYS else self.skipTest("no keyset")
        ran = 0
        for item in chosen():
            if item.kind != "self":
                continue
            with self.subTest(sample=item.key):
                block = keysmith.read(item.path).npdrm
                self.assertEqual(
                    npdrm.ci_hash(store, block.pack(),
                                  bytes.fromhex(item.klicensee)),
                    block.header_hash)
                ran += 1
        if not ran:
            self.skipTest("none of the corpus is on this machine")

    def test_the_ci_hash_covers_the_block_and_nothing_outside_it(self):
        """The question this package could not answer for a while.

        It matters because a patch moves the section table, the ELF digest and
        the metadata. If the hash covered any of those, a rebuild would need a
        value we could not work out. It does not: change the header and the
        hash is unmoved; change a byte inside the block's first 0x60 and it
        moves.
        """
        store = keys.load() if HAVE_KEYS else self.skipTest("no keyset")
        item = sample("mw3-eboot")
        block = keysmith.read(item.path).npdrm
        klic = bytes.fromhex(item.klicensee)
        before = npdrm.ci_hash(store, block.pack(), klic)

        moved = bytearray(block.pack())
        moved[npdrm.CI_HASH_AT + 4] ^= 0xFF      # past the covered range
        self.assertEqual(npdrm.ci_hash(store, bytes(moved), klic), before)

        inside = bytearray(block.pack())
        inside[npdrm.CID_FN_HASH_AT] ^= 0xFF     # within it
        self.assertNotEqual(npdrm.ci_hash(store, bytes(inside), klic), before)

    def test_it_is_keyed_by_the_klicensee(self):
        store = keys.load() if HAVE_KEYS else self.skipTest("no keyset")
        item = sample("mw3-eboot")
        block = keysmith.read(item.path).npdrm
        right = npdrm.ci_hash(store, block.pack(),
                              bytes.fromhex(item.klicensee))
        wrong = npdrm.ci_hash(store, block.pack(), b"\x00" * 16)
        self.assertEqual(right, block.header_hash)
        self.assertNotEqual(wrong, block.header_hash)

    def test_a_klicensee_that_is_the_wrong_length_is_refused(self):
        store = keys.load() if HAVE_KEYS else self.skipTest("no keyset")
        item = sample("mw3-eboot")
        block = keysmith.read(item.path).npdrm
        with self.assertRaises(ValueError):
            npdrm.ci_hash(store, block.pack(), b"\x00" * 8)

    def test_signing_under_another_name_moves_both_hashes(self):
        """CID_FN binds the content ID to the name on the console, and the CI
        hash covers CID_FN, so the two move together."""
        store = keys.load() if HAVE_KEYS else self.skipTest("no keyset")
        item = sample("mw3-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        same = keysmith.sign(elf, item.path, item.klicensee,
                             filename=self.name_of(item))
        other = keysmith.sign(elf, item.path, item.klicensee,
                              filename="something_else.self")
        self.assertEqual(same, open(item.path, "rb").read())
        first, second = keysmith.read(same).npdrm, keysmith.read(other).npdrm
        self.assertNotEqual(first.cid_fn_hash, second.cid_fn_hash)
        self.assertNotEqual(first.header_hash, second.header_hash)


class TheKeysFile(unittest.TestCase):
    """Loading keys, and the line endings problem that started this."""

    def setUp(self):
        need_keys()
        self.store = keys.load()

    def test_the_shipped_file_loads(self):
        self.assertGreater(len(self.store.keysets), 50)

    def test_the_path_is_not_relative_to_the_working_directory(self):
        """The scetool behaviour this replaces: it read data/keys from wherever
        it happened to be started, so the same command worked or failed
        depending on the folder it was typed in."""
        here = os.path.dirname(os.path.abspath(keys.__file__))
        self.assertTrue(keys.default_path().startswith(here),
                        keys.default_path())
        self.assertTrue(os.path.isabs(keys.default_path()))

    def test_every_line_ending_gives_the_same_keys(self):
        raw = open(self.store.source, "rb").read().decode("latin-1")
        unix = raw.replace("\r\n", "\n")
        old_mac = unix.replace("\n", "\r")
        counts = {name: len(keys.KeyStore.from_text(text).keysets)
                  for name, text in (("crlf", raw), ("lf", unix),
                                     ("cr", old_mac))}
        self.assertEqual(len(set(counts.values())), 1, counts)

    def test_a_line_that_will_not_parse_names_the_line(self):
        with self.assertRaises(keysmith.SceError) as caught:
            keys.KeyStore.from_text("[a]\nthis is not a setting\n", "k")
        self.assertIn("line 2", str(caught.exception))

    def test_a_missing_revision_says_which_ones_there_are(self):
        with self.assertRaises(keysmith.KeyNotFound) as caught:
            self.store.require_candidates("NPDRM", 0x7FFF, "f.self")
        message = str(caught.exception)
        self.assertIn("0x7FFF", message)
        self.assertIn("0x0010", message)


class TheParser(unittest.TestCase):
    """Field for field against what scetool prints for the same file."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sce"))
        import reference
        cls.reference = reference
        import tempfile
        cls.workdir = tempfile.mkdtemp(prefix="keysmith-ref-")
        try:
            cls.tool = reference.Scetool(cls.workdir)
            cls.usable = cls.tool.runnable()
        except Exception:                                   # noqa: BLE001
            cls.tool = None
            cls.usable = False

    def report_matches(self, item):
        if not self.usable:
            self.skipTest("scetool.exe cannot be run here")
        name = self.tool.bring_in(item.path, item.key + ".self")
        theirs = [line.rstrip() for line
                  in self.tool.info(name, item.klicensee).splitlines()
                  if line.startswith((" ", "[*]"))]
        mine = [line.rstrip() for line
                in keysmith.inspect(item.path, item.klicensee).lines()]
        self.assertEqual(theirs, mine, item.key)

    def test_it_agrees_with_scetool_field_for_field(self):
        need_keys()
        ran = 0
        for item in chosen():
            if item.kind != "self":
                continue
            with self.subTest(sample=item.key):
                self.report_matches(item)
                ran += 1
        if not ran:
            self.skipTest("none of the corpus is on this machine")

    def test_a_missing_keyset_says_where_to_put_one(self):
        with self.assertRaises(keysmith.SceError) as caught:
            keys.KeyStore.from_file("/nowhere/at/all/keys")
        message = str(caught.exception)
        self.assertIn("no keys file here", message)
        self.assertIn("/nowhere/at/all", message)

    def test_a_file_that_is_not_an_sce_says_so(self):
        with self.assertRaises(keysmith.NotAnSce) as caught:
            keysmith.read(b"\x7fELF" + b"\x00" * 200)
        self.assertIn("magic", str(caught.exception))

    def test_a_truncated_file_names_the_field(self):
        item = sample("mw3-eboot")
        raw = open(item.path, "rb").read()[:0x60]
        with self.assertRaises(keysmith.SceError) as caught:
            keysmith.read(raw)
        self.assertIn("SELF header", str(caught.exception))


class TheDecryption(unittest.TestCase):
    """Plaintext compared against figures produced by other tools."""

    def test_black_ops_one_matches_the_known_good_figure(self):
        item = sample("bo1-bles01031")
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        elf = keysmith.decrypt(item.path, item.klicensee)
        self.assertEqual(len(elf), corpus.BO1_ELF_SIZE)
        self.assertEqual(hashlib.sha1(elf).hexdigest(), corpus.BO1_ELF_SHA1)

    def test_both_black_ops_one_disc_builds_give_the_same_elf(self):
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        first = sample("bo1-bles01031")
        second = sample("bo1-blus30591")
        self.assertEqual(
            hashlib.sha1(keysmith.decrypt(first.path,
                                          first.klicensee)).hexdigest(),
            hashlib.sha1(keysmith.decrypt(second.path,
                                          second.klicensee)).hexdigest())

    def test_a_wrong_klicensee_says_it_is_the_klicensee(self):
        item = sample("bo1-bles01031")
        with self.assertRaises(keysmith.DecryptionFailed) as caught:
            keysmith.decrypt(item.path, "00" * 16)
        self.assertIn("klicensee", str(caught.exception))

    def test_a_klicensee_that_is_not_hex_says_so(self):
        item = sample("mw3-eboot")
        with self.assertRaises(keysmith.KeyNotFound) as caught:
            keysmith.decrypt(item.path, "not hex at all")
        self.assertIn("klicensee", str(caught.exception))


class TheRoundTrip(unittest.TestCase):
    """The acceptance test: unchanged in, identical out."""

    def test_every_sample_rebuilds_byte_for_byte(self):
        if not HAVE_KEYS:
            self.skipTest('no keyset on this machine')
        ran = 0
        for item in chosen():
            with self.subTest(sample=item.key):
                original = open(item.path, "rb").read()
                elf = keysmith.decrypt(item.path, item.klicensee)
                rebuilt = keysmith.sign(elf, item.path, item.klicensee)
                self.assertEqual(len(rebuilt), len(original), item.key)
                self.assertEqual(rebuilt, original, item.key)
                ran += 1
        if not ran:
            self.skipTest("none of the corpus is on this machine")

    def test_the_identifying_fields_survive(self):
        item = sample("bo1-bles01031")
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        before = keysmith.read(item.path)
        elf = keysmith.decrypt(item.path, item.klicensee)
        after = keysmith.read(keysmith.sign(elf, item.path, item.klicensee))
        self.assertEqual(before.sce.key_revision, after.sce.key_revision)
        self.assertEqual(before.app_info.self_type, after.app_info.self_type)
        self.assertEqual(before.app_info.auth_id, after.app_info.auth_id)
        self.assertEqual(before.app_info.vendor_id, after.app_info.vendor_id)
        first, second = before.npdrm, after.npdrm
        self.assertIsNotNone(second, "the NPDRM block must survive")
        for field in ("licence_type", "app_type", "content_id", "cid_fn_hash"):
            self.assertEqual(getattr(first, field), getattr(second, field),
                             field)

    def test_compression_stays_as_the_original_had_it(self):
        """Turning it off doubles the file; turning it on for a title that
        never had it would be just as wrong."""
        item = sample("bo1-bles01031")
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        parsed = keysmith.read(item.path)
        metadata = parsed.decrypt_metadata(bytes.fromhex(item.klicensee))
        compressed = [s for s in metadata.sections if s.compressed == 2]
        self.assertTrue(compressed, "this sample is compressed")
        elf = keysmith.decrypt(item.path, item.klicensee)
        rebuilt = keysmith.read(keysmith.sign(elf, item.path, item.klicensee))
        again = rebuilt.decrypt_metadata(bytes.fromhex(item.klicensee))
        self.assertEqual([s.compressed for s in metadata.sections],
                         [s.compressed for s in again.sections])

    def test_an_elf_that_has_moved_is_refused_rather_than_signed(self):
        item = sample("mw3-eboot")
        elf = bytearray(keysmith.decrypt(item.path, item.klicensee))
        elf[0x20:0x28] = (0xDEAD).to_bytes(8, "big")        # e_phoff
        with self.assertRaises(keysmith.SigningFailed) as caught:
            keysmith.sign(bytes(elf), item.path, item.klicensee)
        self.assertIn("phoff", str(caught.exception))

    def test_a_short_elf_is_refused(self):
        item = sample("mw3-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        with self.assertRaises(keysmith.SigningFailed):
            keysmith.sign(elf[:len(elf) // 2], item.path, item.klicensee)


class ThePatchedFile(unittest.TestCase):
    """A real patch, against the file scetool produced from the same ELF."""

    def test_it_decrypts_back_to_what_went_in(self):
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        stock = os.path.join(corpus.HOME, "bo1-BLES01031/t5mp_ps3f.self")
        patched = os.path.join(corpus.HOME,
                               "bo1-BLES01031/t5mp_ps3f.xuid.self")
        for path in (stock, patched):
            if not os.path.isfile(path):
                self.skipTest(f"{path} is not on this machine")
        elf = keysmith.decrypt(patched, corpus.BO1_KLIC)
        mine = keysmith.sign(elf, stock, corpus.BO1_KLIC)
        self.assertEqual(keysmith.decrypt(mine, corpus.BO1_KLIC), elf)

    def test_the_file_digest_is_the_sha1_of_the_elf_that_was_signed(self):
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        stock = os.path.join(corpus.HOME, "bo1-BLES01031/t5mp_ps3f.self")
        patched = os.path.join(corpus.HOME,
                               "bo1-BLES01031/t5mp_ps3f.xuid.self")
        for path in (stock, patched):
            if not os.path.isfile(path):
                self.skipTest(f"{path} is not on this machine")
        elf = keysmith.decrypt(patched, corpus.BO1_KLIC)
        mine = keysmith.read(keysmith.sign(elf, stock, corpus.BO1_KLIC))
        from ps3tools.keysmith.structs import CONTROL_DIGEST
        block = [c for c in mine.control_infos
                 if c.info_type == CONTROL_DIGEST][0]
        self.assertEqual(block.payload[20:40], hashlib.sha1(elf).digest())
        self.assertEqual(block.payload[0:20], signing.DIGEST_CONSTANT)


class TheFakeSigned(unittest.TestCase):
    """The format scetool cannot open at all."""

    def test_they_are_recognised_as_fake_signed(self):
        if not HAVE_KEYS:
            self.skipTest('no keyset on this machine')
        for item in corpus.fselfs():
            with self.subTest(sample=item.key):
                described = keysmith.inspect(item.path)
                self.assertTrue(described.fake_signed)
                self.assertEqual(described.key_revision,
                                 fself.FAKE_KEY_REVISION)

    def test_they_round_trip_byte_for_byte(self):
        if not HAVE_KEYS:
            self.skipTest('no keyset on this machine')
        ran = 0
        for item in corpus.fselfs():
            with self.subTest(sample=item.key):
                original = open(item.path, "rb").read()
                elf = keysmith.decrypt(item.path)
                self.assertEqual(keysmith.sign(elf, item.path), original)
                ran += 1
        if not ran:
            self.skipTest("no fake-signed samples on this machine")

    def test_the_zeroed_npdrm_block_is_reported_rather_than_hidden(self):
        if not HAVE_KEYS:
            self.skipTest('no keyset on this machine')
        """It is the reason these answer 8001000F, so it gets said plainly."""
        ran = 0
        for item in corpus.fselfs():
            with self.subTest(sample=item.key):
                described = keysmith.inspect(item.path)
                self.assertTrue(described.npdrm_is_zeroed)
                self.assertIn("8001000F", described.text())
                ran += 1
        if not ran:
            self.skipTest("no fake-signed samples on this machine")

    def test_a_zeroed_block_can_be_filled_in_from_a_retail_file(self):
        digital = sample("npeb00756-fself")
        retail = sample("bo1-bles01031")
        source = keysmith.read(retail.path).npdrm
        raw = open(digital.path, "rb").read()
        fixed = fself.fill_npdrm(raw, source)
        self.assertEqual(len(fixed), len(raw))
        described = keysmith.inspect(fixed)
        self.assertFalse(described.npdrm_is_zeroed)
        self.assertEqual(described.npdrm.content_id, source.content_id)
        self.assertEqual(described.npdrm.app_type, source.app_type)
        self.assertEqual(described.npdrm.licence_type, source.licence_type)

    def test_the_signed_path_refuses_a_fake_signed_template(self):
        item = sample("npeb00756-fself")
        parsed = keysmith.read(item.path)
        with self.assertRaises(keysmith.SigningFailed):
            signing.rebuild(parsed, b"\x7fELF" + b"\x00" * 100)


class TheFakeSignedFromRetail(unittest.TestCase):
    """Building a fake-signed SELF out of a retail one.

    This is the form PS3HEN loads. A retail re-sign carries a signature that
    cannot be regenerated once a file's bytes have moved, and HEN appears to
    check it: three consoles black screened at the moment the patched
    multiplayer binary loaded.

    rebuild() cannot do this, because it keeps an existing fake-signed
    header whole and only Modern Warfare 3 has such a file on disk. Both
    Black Ops titles have to have the container built for them.
    """

    def built(self, key):
        """A sample, the ELF out of it, and that ELF fake signed."""
        item = sample(key)
        elf = keysmith.decrypt(item.path, item.klicensee)
        return item, elf, keysmith.fake_sign(elf, item.path, item.klicensee)

    def digest_block(self, parsed):
        from ps3tools.keysmith.structs import CONTROL_DIGEST
        return [c for c in parsed.control_infos
                if c.info_type == CONTROL_DIGEST][0]

    def npdrm_block(self, parsed):
        from ps3tools.keysmith.structs import CONTROL_NPDRM
        return [c for c in parsed.control_infos
                if c.info_type == CONTROL_NPDRM][0]

    def test_every_retail_sample_gives_back_the_elf_that_went_in(self):
        need_keys()
        ran = 0
        for item in chosen():
            if item.kind != "self":
                continue
            with self.subTest(sample=item.key):
                elf = keysmith.decrypt(item.path, item.klicensee)
                out = keysmith.fake_sign(elf, item.path, item.klicensee)
                self.assertEqual(keysmith.decrypt(out), elf)
                ran += 1
        if not ran:
            self.skipTest("none of the corpus is on this machine")

    def test_the_header_is_the_shape_every_real_fself_has(self):
        item, elf, out = self.built("mw3-eboot")
        parsed = keysmith.read(out)
        self.assertEqual(parsed.sce.key_revision, fself.FAKE_KEY_REVISION)
        self.assertEqual(parsed.sce.metadata_offset, 0x480)
        self.assertEqual(parsed.sce.header_length, 0x980)
        self.assertEqual(parsed.sce.data_length, len(elf))
        self.assertTrue(keysmith.inspect(out).fake_signed)

    def test_nothing_is_compressed_and_nothing_is_encrypted(self):
        from ps3tools.keysmith.structs import SECTION_INFO_ENCRYPTED
        item, elf, out = self.built("mw3-eboot")
        for index, info in enumerate(keysmith.read(out).section_infos):
            with self.subTest(section=index):
                self.assertEqual(info.compressed, 1)
                self.assertNotEqual(info.encrypted, SECTION_INFO_ENCRYPTED)

    def test_the_body_is_the_elf_laid_down_whole_after_the_header(self):
        """Which is what all three fselfs in the corpus are."""
        item, elf, out = self.built("mw3-eboot")
        head = keysmith.read(out).sce.header_length
        self.assertEqual(out[head:], elf)
        self.assertEqual(len(out), head + len(elf))

    def test_the_npdrm_block_is_the_retail_one_rather_than_zeros(self):
        """The whole reason this exists rather than TrueAncestor's resigner.

        Every fake-signed release in the corpus carries a block that is
        entirely zero, and that is why those files answer 8001000F on a
        console that checks licences.
        """
        item, elf, out = self.built("mw3-eboot")
        source = keysmith.read(item.path).npdrm
        described = keysmith.inspect(out)
        self.assertFalse(described.npdrm_is_zeroed)
        self.assertEqual(described.npdrm.pack(), source.pack())

    def test_the_file_digest_is_the_sha1_of_the_elf_inside_it(self):
        item, elf, out = self.built("mw3-eboot")
        payload = self.digest_block(keysmith.read(out)).payload
        at = fself.DIGEST_ELF_SHA1_AT
        self.assertEqual(payload[at:at + 20], hashlib.sha1(elf).digest())

    def test_a_real_fself_carries_the_digest_this_computes(self):
        """The gate for that digest is a real file, not this package.

        The Modern Warfare 3 fake-signed release carries exactly the SHA1 of
        the ELF inside it, which is what makes recomputing it here right
        rather than merely plausible.
        """
        item = sample("mw3-bles01428")
        elf = keysmith.decrypt(item.path)
        payload = self.digest_block(keysmith.read(item.path)).payload
        at = fself.DIGEST_ELF_SHA1_AT
        self.assertEqual(payload[at:at + 20], hashlib.sha1(elf).digest())

    def test_it_matches_the_real_modern_warfare_three_fself(self):
        """The strongest gate to hand: a real fself of the same binary.

        Every structural field agrees. The file is not byte identical and is
        not meant to be, and the three things that differ are each
        deliberate: the NPDRM block, the minimum firmware version and the
        filler in the metadata region. The next test covers those.
        """
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        retail = sample("mw3-mp")
        elf = keysmith.decrypt(retail.path, retail.klicensee)
        mine = keysmith.read(keysmith.fake_sign(elf, retail.path,
                                                retail.klicensee))
        real = keysmith.read(sample("mw3-bles01428").path)
        for field in ("magic", "version", "key_revision", "header_type",
                      "metadata_offset", "header_length", "data_length"):
            self.assertEqual(getattr(mine.sce, field),
                             getattr(real.sce, field), field)
        for field in ("header_type", "app_info_offset", "elf_offset",
                      "phdr_offset", "shdr_offset", "section_info_offset",
                      "sce_version_offset", "control_info_offset",
                      "control_info_size", "padding"):
            self.assertEqual(getattr(mine.self_header, field),
                             getattr(real.self_header, field), field)
        self.assertEqual(mine.app_info.pack(), real.app_info.pack())
        self.assertEqual(mine.sce_version.pack(), real.sce_version.pack())
        self.assertEqual([i.pack() for i in mine.section_infos],
                         [i.pack() for i in real.section_infos])
        self.assertEqual(mine.elf_header.pack(), real.elf_header.pack())
        self.assertEqual([p.pack() for p in mine.program_headers],
                         [p.pack() for p in real.program_headers])
        self.assertEqual([s.pack() for s in mine.section_headers],
                         [s.pack() for s in real.section_headers])
        self.assertEqual([(b.info_type, b.size) for b in mine.control_infos],
                         [(b.info_type, b.size) for b in real.control_infos])
        self.assertEqual(mine.raw[mine.sce.header_length:],
                         real.raw[real.sce.header_length:])
        self.assertEqual(len(mine.raw), len(real.raw))

    def test_it_differs_from_the_real_fself_only_where_it_means_to(self):
        """Three fields, and each difference is the point of the exercise.

        The real file zeroes the NPDRM block, which is why it answers
        8001000F. It also zeroes the minimum firmware version, which is a
        field that identifies the title and that nothing here can derive, so
        it is carried over instead. The metadata region is filler that
        nothing reads.
        """
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        retail = sample("mw3-mp")
        elf = keysmith.decrypt(retail.path, retail.klicensee)
        mine = keysmith.read(keysmith.fake_sign(elf, retail.path,
                                                retail.klicensee))
        real = keysmith.read(sample("mw3-bles01428").path)
        stock = keysmith.read(retail.path)

        self.assertFalse(any(self.npdrm_block(real).payload))
        self.assertEqual(self.npdrm_block(mine).payload,
                         self.npdrm_block(stock).payload)

        at = fself.DIGEST_FIRMWARE_AT
        self.assertEqual(self.digest_block(real).payload[at:at + 8],
                         b"\x00" * 8)
        self.assertEqual(self.digest_block(mine).payload[at:at + 8],
                         self.digest_block(stock).payload[at:at + 8])

        start = (mine.self_header.control_info_offset
                 + mine.self_header.control_info_size)
        self.assertNotEqual(mine.raw[start:mine.sce.header_length],
                            real.raw[start:real.sce.header_length])

    def test_building_the_same_binary_twice_gives_the_same_file(self):
        """The filler is derived from the ELF rather than drawn at random.

        A user comparing two builds of the same patch should see no
        difference, and a difference that means nothing is the worst kind to
        have to account for.
        """
        item, elf, first = self.built("mw3-eboot")
        second = keysmith.fake_sign(elf, item.path, item.klicensee)
        self.assertEqual(first, second)

    def test_giving_the_name_it_already_has_reproduces_its_own_hashes(self):
        """A hash recomputed and agreeing is checked; a copied one is not."""
        item, elf, plain = self.built("mw3-eboot")
        named = keysmith.fake_sign(elf, item.path, item.klicensee,
                                   filename="EBOOT.BIN")
        self.assertEqual(keysmith.inspect(named).npdrm.pack(),
                         keysmith.inspect(plain).npdrm.pack())

    def test_a_new_name_moves_the_cid_fn_hash_and_the_ci_hash_with_it(self):
        """The CI hash covers the CID_FN hash, so one cannot move alone."""
        item, elf, plain = self.built("mw3-eboot")
        renamed = keysmith.fake_sign(elf, item.path, item.klicensee,
                                     filename="default_mp.self")
        before = keysmith.inspect(plain).npdrm
        after = keysmith.inspect(renamed).npdrm
        self.assertNotEqual(after.cid_fn_hash, before.cid_fn_hash)
        self.assertNotEqual(after.header_hash, before.header_hash)
        self.assertEqual(after.content_id, before.content_id)
        self.assertEqual(after.app_type, before.app_type)
        self.assertEqual(after.licence_type, before.licence_type)

    def test_an_elf_that_has_moved_is_refused_rather_than_signed(self):
        item, elf, out = self.built("mw3-eboot")
        moved = bytearray(elf)
        moved[0x18:0x20] = (0x1234).to_bytes(8, "big")
        with self.assertRaises(keysmith.SigningFailed) as caught:
            keysmith.fake_sign(bytes(moved), item.path, item.klicensee)
        self.assertIn("entry", str(caught.exception))

    def test_a_short_elf_is_refused(self):
        item, elf, out = self.built("mw3-eboot")
        with self.assertRaises(keysmith.SigningFailed) as caught:
            keysmith.fake_sign(elf[:len(elf) // 2], item.path,
                               item.klicensee)
        self.assertIn("too short", str(caught.exception))

    def test_a_fake_signed_template_is_rebuilt_through_its_own_header(self):
        item = sample("npub30787-fself")
        original = open(item.path, "rb").read()
        elf = keysmith.decrypt(item.path)
        self.assertEqual(keysmith.fake_sign(elf, item.path), original)

    def test_a_container_is_not_built_around_a_fake_signed_file(self):
        item = sample("npub30787-fself")
        parsed = keysmith.read(item.path)
        with self.assertRaises(keysmith.SigningFailed) as caught:
            fself.from_retail(parsed, b"\x7fELF" + b"\x00" * 200)
        self.assertIn("already fake signed", str(caught.exception))

    def test_a_patched_black_ops_one_binary_goes_in_and_comes_out(self):
        """The case this was written for, on a real patched file."""
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        need_keys()
        stock = os.path.join(corpus.HOME, "bo1-BLES01031/t5mp_ps3f.self")
        patched = os.path.join(corpus.HOME,
                               "bo1-BLES01031/t5mp_ps3f.xuid.self")
        for path in (stock, patched):
            if not os.path.isfile(path):
                self.skipTest(f"{path} is not on this machine")
        elf = keysmith.decrypt(patched, corpus.BO1_KLIC)
        out = keysmith.fake_sign(elf, stock, corpus.BO1_KLIC)
        self.assertEqual(keysmith.decrypt(out), elf)
        described = keysmith.inspect(out)
        self.assertTrue(described.fake_signed)
        self.assertFalse(described.npdrm_is_zeroed)

    def test_true_ancestors_unfself_reads_what_this_writes(self):
        """An external gate, by a tool that knows nothing of this package.

        unfself.exe is a Windows process and cannot open a /home path, so it
        and the file both go into one scratch directory and it is run from
        inside that with plain relative names.
        """
        import shutil
        import subprocess
        import tempfile

        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        exe = os.path.join(corpus.HOME, "bo1-regions", "unfself.exe")
        if not os.path.isfile(exe):
            self.skipTest(f"{exe} is not on this machine")
        item, elf, out = self.built("bo1-bles01031")
        work = tempfile.mkdtemp(prefix="keysmith-unfself-")
        try:
            shutil.copy2(exe, work)
            for name in ("msvcr100.dll", "zlib1.dll"):
                beside = os.path.join(os.path.dirname(exe), name)
                if os.path.isfile(beside):
                    shutil.copy2(beside, work)
            with open(os.path.join(work, "in.self"), "wb") as handle:
                handle.write(out)
            try:
                result = subprocess.run(
                    [os.path.join(work, "unfself.exe"), "in.self", "out.elf"],
                    cwd=work, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, timeout=600)
            except OSError as exc:
                self.skipTest(f"unfself.exe cannot be run here: {exc}")
            self.assertEqual(result.returncode, 0,
                             result.stdout.decode("utf-8", "replace"))
            with open(os.path.join(work, "out.elf"), "rb") as handle:
                self.assertEqual(handle.read(), elf)
        finally:
            shutil.rmtree(work, ignore_errors=True)


class TheKeyRevisionAFileIsRebuiltAt(unittest.TestCase):
    """Re-signing against a keyset that is not the template's own.

    PS3HEN runs on 4.8x firmware and loads a SELF through the 3.55-era
    keyset, so a HEN console wants key revision 0x000A. The gate is a
    third party's
    published binaries: a CFW build and a HEN build of the same Modern
    Warfare 2 ELF, shipped side by side for all seven regions.
    """

    def paired(self, build, region="bles00683", name="default_mp.self",
              folder="release"):
        need_keys()
        path = os.path.join(PAIRED_BUILDS, folder, region, build, name)
        if not os.path.isfile(path):
            self.skipTest(f"{path} is not on this machine")
        return path

    def test_the_paired_builds_are_one_elf_signed_at_two_key_revisions(self):
        """Neither is fake signed and the payload is the same file."""
        cfw = keysmith.read(self.paired("cfw"))
        hen = keysmith.read(self.paired("hen"))
        self.assertFalse(cfw.is_fake_signed)
        self.assertFalse(hen.is_fake_signed)
        self.assertEqual(cfw.sce.key_revision, 0x0010)
        self.assertEqual(hen.sce.key_revision, 0x000A)
        self.assertEqual(keysmith.decrypt(self.paired("hen"), corpus.IW_KLIC),
                         keysmith.decrypt(self.paired("cfw"), corpus.IW_KLIC))

    def test_the_npdrm_block_is_the_same_on_both_of_the_paired_builds(self):
        """Every byte of it, the pad at 0x40 included, so the HEN form is not
        a differently identified file."""
        from ps3tools.keysmith.structs import CONTROL_NPDRM
        blocks = []
        for build in ("cfw", "hen"):
            parsed = keysmith.read(self.paired(build))
            blocks.append([b.payload for b in parsed.control_infos
                           if b.info_type == CONTROL_NPDRM][0])
        self.assertTrue(blocks[0])
        self.assertEqual(blocks[0], blocks[1])

    def test_a_file_asked_for_0x000A_opens_with_the_3_55_keyset(self):
        item = sample("mw2-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        rebuilt = keysmith.read(keysmith.sign(elf, item.path, item.klicensee,
                                              key_revision=0x000A))
        self.assertEqual(rebuilt.sce.key_revision, 0x000A)
        rebuilt.decrypt_metadata(bytes.fromhex(item.klicensee))
        self.assertEqual(rebuilt.keyset.revision, 0x000A)
        self.assertEqual(rebuilt.keyset.self_type, "NPDRM")

    def test_the_revision_a_hen_console_wants_is_the_one_they_ship(self):
        """The number is read off his binary rather than written down here."""
        item = sample("mw2-mp")
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        theirs = keysmith.read(self.paired("hen"))
        elf = keysmith.decrypt(item.path, item.klicensee)
        mine = keysmith.read(keysmith.sign(
            elf, item.path, item.klicensee, filename="default_mp.self",
            key_revision=theirs.sce.key_revision))
        self.assertEqual(mine.sce.key_revision, theirs.sce.key_revision)
        self.assertEqual(mine.npdrm.content_id, theirs.npdrm.content_id)
        self.assertEqual(mine.npdrm.cid_fn_hash, theirs.npdrm.cid_fn_hash)
        self.assertEqual(mine.npdrm.app_type, theirs.npdrm.app_type)
        self.assertEqual(mine.npdrm.licence_type, theirs.npdrm.licence_type)

    def test_the_elf_comes_back_out_of_a_file_at_another_revision(self):
        item = sample("mw2-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        rebuilt = keysmith.sign(elf, item.path, item.klicensee,
                                key_revision=0x000A)
        self.assertEqual(keysmith.decrypt(rebuilt, item.klicensee), elf)

    def test_only_the_wrapping_changes_and_the_file_goes_back(self):
        """Signing the 0x000A file's ELF back through the retail template
        gives the retail bytes, so nothing under the wrapping moved."""
        item = sample("mw2-eboot")
        original = open(item.path, "rb").read()
        elf = keysmith.decrypt(item.path, item.klicensee)
        moved = keysmith.sign(elf, item.path, item.klicensee,
                              key_revision=0x000A)
        self.assertNotEqual(moved, original)
        back = keysmith.sign(keysmith.decrypt(moved, item.klicensee),
                             item.path, item.klicensee)
        self.assertEqual(back, original)

    def test_the_metadata_key_the_file_carries_is_the_one_it_had(self):
        """The key and the counter the metadata is encrypted under are kept.

        This used to assert the whole key table came back unchanged, which
        was true only while signing at another revision left the section
        bytes alone. A file signed for PS3HEN has its loaded sections
        compressed, so the bytes move and the per-section hashes in that
        table move with them, and it has its section table built from the
        ELF, so it has sections the template never had and a table with room
        for their keys. The key and the counter are what this is about and
        they are untouched.
        """
        item = sample("mw2-eboot")
        klic = bytes.fromhex(item.klicensee)
        before = keysmith.read(item.path)
        first = before.decrypt_metadata(klic)
        elf = keysmith.decrypt(item.path, item.klicensee)
        after = keysmith.read(keysmith.sign(elf, item.path, item.klicensee,
                                            key_revision=0x000A))
        second = after.decrypt_metadata(klic)
        self.assertEqual(first.info.key, second.info.key)
        self.assertEqual(first.start_iv, second.start_iv)
        self.assertGreater(len(second.keys), len(first.keys))

    def test_signing_for_hen_compresses_the_sections_worth_compressing(self):
        """The fourth thing that makes a file a PS3HEN file.

        Every HEN build surveyed stores a section compressed wherever that
        makes it smaller and stores it as it stands wherever it does not,
        which is what the four uncompressed sections of their default_mp.self
        have in common: 0, 4, 0x20 and 0x40 bytes, and only the last of those
        is worth packing at all. Every custom firmware build compresses
        nothing, across twenty-eight files. It was taken for a build choice,
        and a patch with the other three differences right black screened a
        real console the moment the patched binary loaded.
        """
        item = sample("mw2-eboot")
        klic = bytes.fromhex(item.klicensee)
        elf = keysmith.decrypt(item.path, item.klicensee)
        template = keysmith.read(item.path)
        for revision in (0x000A, None):
            with self.subTest(key_revision=revision):
                built = keysmith.read(keysmith.sign(
                    elf, item.path, item.klicensee, key_revision=revision))
                loaded = [s for s in built.decrypt_metadata(klic).sections
                          if s.section_type == 2]
                self.assertTrue(loaded)
                for section in loaded:
                    phdr = template.program_headers[section.index]
                    body = elf[phdr.offset:phdr.offset + phdr.filesz]
                    helps = len(zlib.compress(
                        body, signing.ZLIB_LEVEL)) < len(body)
                    want = 2 if revision == 0x000A and helps else 1
                    self.assertEqual(section.compressed, want, section.index)

    def test_a_hen_file_is_smaller_than_the_one_it_came_from(self):
        item = sample("mw2-mp")
        elf = keysmith.decrypt(item.path, item.klicensee)
        built = keysmith.sign(elf, item.path, item.klicensee,
                              key_revision=0x000A)
        self.assertLess(len(built), os.path.getsize(item.path))

    def test_it_still_decrypts_back_to_what_went_in(self):
        item = sample("mw2-mp")
        elf = keysmith.decrypt(item.path, item.klicensee)
        built = keysmith.sign(elf, item.path, item.klicensee,
                              key_revision=0x000A)
        self.assertEqual(keysmith.decrypt(built, item.klicensee), elf)

    def test_asking_for_the_revision_it_already_has_changes_nothing(self):
        item = sample("mw2-eboot")
        original = open(item.path, "rb").read()
        elf = keysmith.decrypt(item.path, item.klicensee)
        parsed = keysmith.read(item.path)
        rebuilt = keysmith.sign(elf, item.path, item.klicensee,
                                key_revision=parsed.sce.key_revision)
        self.assertEqual(rebuilt, original)

    def test_a_revision_the_keys_file_has_none_of_is_named(self):
        item = sample("mw2-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        with self.assertRaises(keysmith.KeyNotFound) as caught:
            keysmith.sign(elf, item.path, item.klicensee, key_revision=0x0099)
        self.assertIn("0x0099", str(caught.exception))

    def test_the_fake_signed_marker_is_refused_as_a_key_revision(self):
        """0x8000 is what a fake-signed SELF carries where a revision goes,
        and there is no keyset behind it."""
        item = sample("mw2-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        with self.assertRaises(keysmith.SigningFailed) as caught:
            keysmith.sign(elf, item.path, item.klicensee,
                          key_revision=signing.FAKE_KEY_REVISION)
        self.assertIn("fake", str(caught.exception).lower())


class TheErrors(unittest.TestCase):
    """Every failure names the file, the field and what was expected."""

    def test_the_message_carries_all_three(self):
        error = keysmith.SceError("it went wrong", path="a.self",
                                  field="magic", expected=0x53434500,
                                  found=0x41424344)
        text = str(error)
        self.assertIn("a.self", text)
        self.assertIn("magic", text)
        self.assertIn("0x53434500", text)
        self.assertIn("0x41424344", text)

    def test_the_fields_are_available_without_parsing_the_message(self):
        error = keysmith.SceError("nope", path="a", field="b", expected=1,
                                  found=2)
        self.assertEqual((error.path, error.field, error.expected,
                          error.found), ("a", "b", 1, 2))


if __name__ == "__main__":
    unittest.main()


class TheMinimumFirmwareMovesWithTheKeyRevision(unittest.TestCase):
    """Re-signing to another keyset moves the firmware field with it.

    The field tracks the keyset across every retail file here: 0x0010 carries
    36000, which is 3.60, 0x0019 carries 40000 and 0x001C carries 42000.
    the third party's PS3HEN builds of Modern Warfare 2 carry 35500, which is
    3.55 and belongs with 0x000A, and he set it deliberately, because his own
    custom firmware builds of the same binary leave it at zero.

    Leaving 3.60 in a file signed against the 3.55 keyset says two different
    things about one file, and this is the field that says which firmware will
    load it.
    """

    def firmware_of(self, blob):
        from ps3tools.keysmith.structs import CONTROL_DIGEST
        parsed = keysmith.read(blob)
        block = [item for item in parsed.control_infos
                 if item.info_type == CONTROL_DIGEST][0]
        return int.from_bytes(block.payload[40:48], "big")

    def test_re_signing_to_the_3_55_keyset_writes_3_55(self):
        item = sample("mw2-mp")
        elf = keysmith.decrypt(item.path, item.klicensee)
        rebuilt = keysmith.sign(elf, item.path, item.klicensee,
                                key_revision=0x000A)
        self.assertEqual(self.firmware_of(rebuilt), 35500)

    def test_leaving_the_revision_alone_leaves_the_field_alone(self):
        item = sample("mw2-mp")
        original = open(item.path, "rb").read()
        elf = keysmith.decrypt(item.path, item.klicensee)
        self.assertEqual(keysmith.sign(elf, item.path, item.klicensee),
                         original)

    def test_a_revision_nobody_has_measured_leaves_the_field_alone(self):
        """A wrong minimum firmware is a file the console refuses for a
        reason nothing on screen would explain, so it is never guessed."""
        from ps3tools.keysmith import sign as _unused                # noqa
        import ps3tools.keysmith.sign
        signing = sys.modules["ps3tools.keysmith.sign"]
        self.assertNotIn(0x0004, signing.FIRMWARE_FOR_REVISION)
        payload = bytearray(48)
        signing._set_firmware(payload, 0x0010, 0x0004)
        self.assertEqual(int.from_bytes(payload[40:48], "big"), 0)

    def test_the_figures_come_off_the_real_files(self):
        signing = sys.modules["ps3tools.keysmith.sign"]
        for item in chosen():
            if item.kind != "self":
                continue
            with self.subTest(sample=item.key):
                parsed = keysmith.read(item.path)
                want = signing.FIRMWARE_FOR_REVISION.get(
                    parsed.sce.key_revision)
                if want is None:
                    continue
                self.assertEqual(
                    self.firmware_of(open(item.path, "rb").read()), want)


class TheControlFlagsAHenBuildCarries(unittest.TestCase):
    """The flags were surveyed before they were written.

    All fourteen of the PS3HEN binaries carry the same value,
    seven regions across both the multiplayer and the campaign trees. All
    fourteen of his custom firmware builds are zero, and so is every stock
    retail file here. Every HEN build sets it, nothing else does, and it never
    varies, which is what makes it part of the build rather than noise.

    What the two bytes mean is not known. They are reproduced because the
    survey says they belong, which is a different thing from understanding
    them.
    """

    HEN_FLAGS = bytes.fromhex("40" + "00" * 30 + "02")
    PAIRED_BUILDS = os.path.join(corpus.HOME, "IW4-Binaries")

    def flags_of(self, path):
        from ps3tools.keysmith.structs import CONTROL_FLAGS
        parsed = keysmith.read(path)
        return [item for item in parsed.control_infos
                if item.info_type == CONTROL_FLAGS][0].payload

    def every_build(self, kind):
        found = []
        for tree in ("release", "release-sp"):
            base = os.path.join(self.PAIRED_BUILDS, tree)
            if not os.path.isdir(base):
                continue
            for region in sorted(os.listdir(base)):
                folder = os.path.join(base, region, kind)
                if not os.path.isdir(folder):
                    continue
                for name in sorted(os.listdir(folder)):
                    found.append(os.path.join(folder, name))
        if not found:
            self.skipTest(f"{self.PAIRED_BUILDS} is not on this machine")
        return found

    def test_every_hen_build_carries_the_same_flags(self):
        seen = {self.flags_of(path) for path in self.every_build("hen")}
        self.assertEqual(seen, {self.HEN_FLAGS})

    def test_no_custom_firmware_build_carries_them(self):
        seen = {self.flags_of(path) for path in self.every_build("cfw")}
        self.assertEqual(seen, {b"\x00" * 32})

    def test_no_stock_file_carries_them(self):
        ran = 0
        for item in chosen():
            with self.subTest(sample=item.key):
                self.assertEqual(self.flags_of(item.path), b"\x00" * 32)
                ran += 1
        if not ran:
            self.skipTest("none of the corpus is on this machine")

    def test_re_signing_to_3_55_writes_them(self):
        item = sample("mw2-mp")
        elf = keysmith.decrypt(item.path, item.klicensee)
        rebuilt = keysmith.sign(elf, item.path, item.klicensee,
                                key_revision=0x000A)
        self.assertEqual(self.flags_of(rebuilt), self.HEN_FLAGS)

    def test_leaving_the_revision_alone_leaves_them_alone(self):
        item = sample("mw2-mp")
        elf = keysmith.decrypt(item.path, item.klicensee)
        rebuilt = keysmith.sign(elf, item.path, item.klicensee)
        self.assertEqual(self.flags_of(rebuilt), b"\x00" * 32)

    def test_a_revision_nobody_has_surveyed_keeps_the_template_s(self):
        signing = sys.modules["ps3tools.keysmith.sign"]
        self.assertNotIn(0x0019, signing.CONTROL_FLAGS_FOR_REVISION)


class TheSectionTableAHenBuildCarries(unittest.TestCase):
    """The fifth difference, and the one a console found the hard way.

    The custom firmware builds in the survey were made with scetool's
    skip-sections option, so each one describes only some of its eight
    program headers: five in default_mp.self and three in default.self.
    Every one of the fourteen PS3HEN builds describes all eight. So a table
    copied out of a custom firmware template does not describe the file a HEN
    console has to be given, and at key revision 0x000A the table is built
    from the ELF instead.

    A patch with the other four differences right went on to a real HEN
    console cleanly and black screened at the moment the patched binary
    loaded, which is what sent the survey looking for a fifth.

    The gate is the same as for the other four: the custom firmware build of
    a pair, re-signed here at 0x000A, against the HEN build the same third
    party shipped beside it.
    """

    PAIRED_BUILDS = os.path.join(corpus.HOME, "IW4-Binaries")

    #: Two regions of the multiplayer binary and one of the campaign one. The
    #: campaign pair is in there because its custom firmware build describes
    #: three sections rather than five, so a rebuild fitted to the
    #: multiplayer template would not fit it.
    PAIRS = (
        ("release", "blus30377", "default_mp.self"),
        ("release", "bles00683", "default_mp.self"),
        ("release-sp", "bles00687", "default.self"),
    )

    def paired(self, tree, region, kind, name):
        need_keys()
        path = os.path.join(self.PAIRED_BUILDS, tree, region, kind, name)
        if not os.path.isfile(path):
            self.skipTest(f"{path} is not on this machine")
        return path

    def resigned(self, tree, region, name):
        """The custom firmware build of a pair, re-signed for PS3HEN."""
        path = self.paired(tree, region, "cfw", name)
        elf = keysmith.decrypt(path, corpus.IW_KLIC)
        return keysmith.read(keysmith.sign(elf, path, corpus.IW_KLIC,
                                           filename=name,
                                           key_revision=0x000A))

    def metadata_of(self, parsed):
        return parsed.decrypt_metadata(bytes.fromhex(corpus.IW_KLIC))

    def plaintext(self, parsed, metadata, section):
        """One section's bytes with the encryption taken back off.

        The ciphertext cannot match theirs and is not meant to: every
        encrypted section here is encrypted under a key generated at random,
        because the key travels in the file beside the data it covers and
        nothing outside the file has to agree with it. What has to match is
        what the console has after decrypting.
        """
        blob = parsed.raw[section.data_offset:
                          section.data_offset + section.data_size]
        if section.encrypted == 3:
            blob = aes.ctr_crypt(metadata.key_at(section.key_index),
                                 metadata.key_at(section.iv_index), blob)
        return blob

    def fields_of(self, parsed):
        """Every field of a file that does not depend on random material.

        The key table is left out because it is random by design, and the
        signature is left out because it covers that table and is made
        with a random nonce, so neither build signs the same input; both
        have tests of their own below. Everything else in the file is in
        here, the section plaintexts included.
        """
        metadata = self.metadata_of(parsed)
        fields = {"file size": len(parsed.raw)}
        for name in ("key_revision", "header_type", "metadata_offset",
                     "header_length", "data_length"):
            fields["sce " + name] = getattr(parsed.sce, name)
        for name in ("header_type", "app_info_offset", "elf_offset",
                     "phdr_offset", "shdr_offset", "section_info_offset",
                     "sce_version_offset", "control_info_offset",
                     "control_info_size", "padding"):
            fields["self header " + name] = getattr(parsed.self_header, name)
        for name in ("auth_id", "vendor_id", "self_type", "version",
                     "padding"):
            fields["app info " + name] = getattr(parsed.app_info, name)
        fields["elf header"] = parsed.elf_header.pack().hex()
        fields["program headers"] = b"".join(
            phdr.pack() for phdr in parsed.program_headers).hex()
        fields["section headers"] = b"".join(
            shdr.pack() for shdr in parsed.section_headers).hex()
        fields["section infos"] = b"".join(
            info.pack() for info in parsed.section_infos).hex()
        fields["sce version"] = parsed.sce_version.pack().hex()
        for block in parsed.control_infos:
            fields[f"control info {block.info_type}"] = (
                block.size, block.next, block.payload.hex())
        for name in ("signature_input_length", "unknown1", "section_count",
                     "key_count", "opt_header_size", "unknown2", "unknown3"):
            fields["metadata " + name] = getattr(metadata.header, name)
        fields["metadata optional"] = metadata.optional.hex()
        fields["metadata length"] = metadata.raw_length
        for index, section in enumerate(metadata.sections):
            for name in ("data_offset", "data_size", "section_type", "index",
                         "hashed", "sha1_index", "encrypted", "key_index",
                         "iv_index", "compressed"):
                fields[f"section {index} {name}"] = getattr(section, name)
            fields[f"section {index} plaintext"] = hashlib.sha1(
                self.plaintext(parsed, metadata, section)).hexdigest()
        return fields

    def test_the_two_builds_of_one_elf_describe_different_sections(self):
        """The evidence the rest of this rests on, read off their files."""
        cfw = self.metadata_of(keysmith.read(
            self.paired("release", "blus30377", "cfw", "default_mp.self")))
        hen = self.metadata_of(keysmith.read(
            self.paired("release", "blus30377", "hen", "default_mp.self")))
        self.assertEqual([s.index for s in cfw.sections
                          if s.section_type == 2], [0, 1, 2, 3, 4])
        self.assertEqual([s.index for s in hen.sections
                          if s.section_type == 2], [0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual((cfw.header.section_count, cfw.header.key_count),
                         (6, 0x2E))
        self.assertEqual((hen.header.section_count, hen.header.key_count),
                         (9, 0x46))

    def test_a_re_signed_custom_firmware_build_matches_theirs(self):
        """Field for field, on three pairs, everything but the signature."""
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        ran = 0
        for tree, region, name in self.PAIRS:
            with self.subTest(region=region, name=name):
                theirs = self.fields_of(keysmith.read(
                    self.paired(tree, region, "hen", name)))
                mine = self.fields_of(self.resigned(tree, region, name))
                self.assertEqual(sorted(mine), sorted(theirs))
                for field in sorted(mine):
                    self.assertEqual(mine[field], theirs[field], field)
                ran += 1
        if not ran:
            self.skipTest(f"{self.PAIRED_BUILDS} is not on this machine")

    def test_every_program_header_gets_a_section_and_the_table_follows(self):
        item = sample("mw2-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        built = keysmith.read(keysmith.sign(elf, item.path, item.klicensee,
                                            key_revision=0x000A))
        metadata = built.decrypt_metadata(bytes.fromhex(item.klicensee))
        loaded = [s for s in metadata.sections if s.section_type == 2]
        self.assertEqual([s.index for s in loaded],
                         list(range(built.elf_header.phnum)))
        table = [s for s in metadata.sections if s.section_type == 1]
        self.assertEqual(len(table), 1)
        # One past the sections written, which is what both of their builds
        # carry: 6 where five sections are written and 9 where eight are.
        # Since every program header is written here the gap could as well be
        # counted from the last index, and the two rules cannot be told apart
        # from any file on hand.
        self.assertEqual(table[0].index, len(loaded) + 1)
        # The SELF header is the only place that says where the section
        # header table is, and it has moved.
        self.assertEqual(table[0].data_offset, built.self_header.shdr_offset)
        self.assertEqual(
            table[0].data_size,
            built.elf_header.shnum * built.elf_header.shentsize)

    def test_the_key_table_is_as_long_as_those_sections_need(self):
        """Eight slots for an encrypted section and six for a plain one."""
        item = sample("mw2-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        built = keysmith.read(keysmith.sign(elf, item.path, item.klicensee,
                                            key_revision=0x000A))
        metadata = built.decrypt_metadata(bytes.fromhex(item.klicensee))
        encrypted = [s for s in metadata.sections if s.encrypted == 3]
        plain = [s for s in metadata.sections if s.encrypted != 3]
        self.assertEqual(metadata.header.key_count,
                         len(encrypted) * 8 + len(plain) * 6)
        self.assertEqual(len(metadata.keys),
                         metadata.header.key_count * signing.SLOT_BYTES)
        for section in metadata.sections:
            at = section.sha1_index * signing.SLOT_BYTES
            self.assertNotEqual(metadata.keys[at:at + 20], bytes(20),
                                section.index)

    def test_the_header_is_as_long_as_the_new_table_makes_it(self):
        """header_length has to move with the table, or the first section
        lands inside the header that describes it."""
        item = sample("mw2-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        built = keysmith.read(keysmith.sign(elf, item.path, item.klicensee,
                                            key_revision=0x000A))
        metadata = built.decrypt_metadata(bytes.fromhex(item.klicensee))
        self.assertGreater(built.sce.header_length,
                           keysmith.read(item.path).sce.header_length)
        self.assertEqual(metadata.sections[0].data_offset,
                         built.sce.header_length)
        self.assertEqual(built.sce.data_length,
                         len(built.raw) - built.sce.header_length)

    def test_the_signature_is_the_one_field_that_cannot_be_copied(self):
        """It is made rather than matched, and the field is the same size.

        The custom firmware template carries zeros there, because scetool
        left it at zero on a revision it has no private key for. Theirs
        carries a real signature and so does the rebuild, and the two cannot
        be the same bytes: what is signed includes the key table, which is
        random by design in their build and in this one, so the two builds
        are not even signing the same input. The test that matters is that
        each verifies, and it is in TheSignatureAFileCarries below.
        """
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        tree, region, name = self.PAIRS[0]
        theirs = self.metadata_of(keysmith.read(
            self.paired(tree, region, "hen", name)))
        template = self.metadata_of(keysmith.read(
            self.paired(tree, region, "cfw", name)))
        mine = self.metadata_of(self.resigned(tree, region, name))
        length = signing.SIGNATURE_BYTES
        self.assertEqual(template.signature[:length], bytes(length))
        self.assertNotEqual(theirs.signature[:length], bytes(length))
        self.assertNotEqual(mine.signature[:length], bytes(length))
        self.assertEqual(len(mine.signature), len(theirs.signature))


class TheCurveTable(unittest.TestCase):
    """data/ldr_curves, and the reading of it that the arithmetic confirms.

    The file is 7744 bytes, which is 64 entries of 121, and a keyset's ctype
    selects one. Every byte of it is stored complemented, which is the part
    that costs an evening and is not a thing to take on trust. These are the
    checks that settled it, and they are all external: the numbers are
    naehrwert's file and the tests are arithmetic on them.
    """

    def primes(self):
        """Miller-Rabin over the small bases, which is decisive at 160 bits.

        Deterministic for these sizes with the bases below in practice, and
        a composite slipping through would only make the test pass where it
        should fail, never the reverse.
        """
        def prime(number):
            if number < 2:
                return False
            for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
                if number % small == 0:
                    return number == small
            odd, twos = number - 1, 0
            while odd % 2 == 0:
                odd //= 2
                twos += 1
            for base in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
                witness = pow(base, odd, number)
                if witness in (1, number - 1):
                    continue
                for _ in range(twos - 1):
                    witness = witness * witness % number
                    if witness == number - 1:
                        break
                else:
                    return False
            return True
        return prime

    def test_every_entry_is_a_curve_once_the_bytes_are_complemented(self):
        """Four checks on all sixty-four, and every one of them passes.

        The prime field is prime, a is exactly p - 3 the way a curve of this
        family has it, the base point satisfies the curve equation, and the
        order is prime as well and takes the base point to infinity.
        """
        need_curves()
        prime = self.primes()
        curves = ecdsa.load()
        self.assertEqual(len(curves), 64)
        for index, curve in enumerate(curves):
            with self.subTest(ctype=f"0x{index:02X}"):
                self.assertTrue(prime(curve.p))
                self.assertEqual(curve.a, (curve.p - 3) % curve.p)
                self.assertTrue(curve.holds(curve.g))
                self.assertTrue(prime(curve.order))
                self.assertIsNone(
                    ecdsa.multiply(curve, curve.order, curve.g))

    def test_no_offset_in_the_raw_file_holds_a_prime_field(self):
        """The other half of the evidence, which is that the raw bytes hold
        no curve anywhere.

        Read as they stand, the six field offsets are composite in all but a
        handful of the sixty-four entries, which is what chance gives: two
        entries at offset 20 and two at offset 101, and nothing anywhere else.
        Complemented, offset 0 is prime in all sixty-four and so is the
        order. Every twenty and twenty-one byte window at every byte offset
        was tested the same way while this was being worked out and no offset
        in the file does better than three.
        """
        need_curves()
        prime = self.primes()
        raw = open(ecdsa.default_path(), "rb").read()
        entries = len(raw) // ecdsa.LDR_CURVE_BYTES
        found = {}
        for index in range(entries):
            entry = raw[index * ecdsa.LDR_CURVE_BYTES:
                        (index + 1) * ecdsa.LDR_CURVE_BYTES]
            for at, length in ((0, 20), (20, 20), (40, 20), (60, 21),
                               (81, 20), (101, 20)):
                if prime(int.from_bytes(entry[at:at + length], "big")):
                    found[at] = found.get(at, 0) + 1
        self.assertEqual(entries, 64)
        self.assertLessEqual(max(found.values(), default=0), 3)
        self.assertEqual(sum(1 for curve in ecdsa.load() if prime(curve.p)),
                         entries)

    def test_the_high_byte_of_every_order_is_zero_once_complemented(self):
        """Which is why the raw file carries 0xFF there.

        The field is twenty-one bytes and every order in the table fits in
        twenty, so the byte in front of it is zero and the raw file shows
        its complement. The vsh table has no such byte and its entries are
        one shorter.
        """
        need_curves()
        raw = open(ecdsa.default_path(), "rb").read()
        for index in range(len(raw) // ecdsa.LDR_CURVE_BYTES):
            with self.subTest(ctype=f"0x{index:02X}"):
                self.assertEqual(raw[index * ecdsa.LDR_CURVE_BYTES + 60],
                                 0xFF)
        for curve in ecdsa.load():
            self.assertLessEqual(curve.order.bit_length(), 160)

    def test_every_private_key_in_the_keys_file_matches_its_public_key(self):
        """The check that ties the two files together.

        The private key times the base point of the curve the same keyset's
        ctype names is the public key that keyset carries. Twenty-seven
        keysets in the file have a private key and all twenty-seven of them
        pass, which is the strongest statement available that both the
        table's layout and its complement are read right.
        """
        need_curves()
        store = keys.load()
        ran = 0
        for keyset in store.keysets:
            if not signing.can_sign(keyset):
                continue
            with self.subTest(keyset=repr(keyset)):
                curve = ecdsa.curve(keyset.curve_type)
                private = int.from_bytes(keyset.private, "big")
                self.assertLess(private, curve.order)
                self.assertEqual(ecdsa.multiply(curve, private, curve.g),
                                 ecdsa.point(keyset.public))
                ran += 1
        self.assertGreater(ran, 20)

    def test_the_four_npdrm_revisions_with_a_private_key_are_named(self):
        """0x000A is the one PS3HEN wants and it is not the only one.

        The keys file holds a real private key for NPDRM revisions 0x0001,
        0x0004, 0x0007 and 0x000A, and zeros for every other. This is what
        decides whether a rebuild signs or carries, so it is read off the
        file rather than written down in the code.
        """
        need_keys()
        store = keys.load()
        signable = sorted(keyset.revision for keyset in store.keysets
                          if keyset.type == "SELF"
                          and keyset.self_type == "NPDRM"
                          and signing.can_sign(keyset))
        self.assertEqual(signable, [0x0001, 0x0004, 0x0007, 0x000A])

    def test_a_ctype_past_the_end_of_the_table_is_refused(self):
        """Rather than masked down into it, which would sign against a curve
        nobody chose."""
        need_curves()
        with self.assertRaises(keysmith.SigningFailed) as caught:
            ecdsa.curve(64)
        self.assertIn("64", str(caught.exception))


class TheSignatureAFileCarries(unittest.TestCase):
    """Signing, checked against signatures this package did not make.

    Two external gates. Sony signed every retail file in the corpus, and
    scetool signed all fourteen PS3HEN builds in the survey with the private
    key the keys file holds for revision 0x000A. Both verify here, which is
    what says the scheme, the curve, the hash and the bytes that go into it
    are all right. Only then is a signature made here worth anything.
    """

    PAIRED_BUILDS = os.path.join(corpus.HOME, "IW4-Binaries")

    def hen_builds(self):
        found = []
        for tree in ("release", "release-sp"):
            base = os.path.join(self.PAIRED_BUILDS, tree)
            if not os.path.isdir(base):
                continue
            for region in sorted(os.listdir(base)):
                folder = os.path.join(base, region, "hen")
                if not os.path.isdir(folder):
                    continue
                for name in sorted(os.listdir(folder)):
                    found.append(os.path.join(folder, name))
        if not found:
            self.skipTest(f"{self.PAIRED_BUILDS} is not on this machine")
        return found

    def verifies(self, path, klicensee):
        parsed = keysmith.read(path)
        covered, signature = signature_input(parsed, klicensee)
        curve = ecdsa.curve(parsed.keyset.curve_type)
        return ecdsa.verify(curve, parsed.keyset.public,
                            ecdsa.digest_of(covered), signature)

    def test_the_signature_scetool_wrote_on_a_hen_build_verifies(self):
        """All fourteen of them, seven regions across both trees."""
        need_curves()
        for path in self.hen_builds():
            with self.subTest(build=os.path.relpath(path,
                                                    self.PAIRED_BUILDS)):
                self.assertTrue(self.verifies(path, corpus.IW_KLIC))

    def test_the_signature_sony_wrote_on_a_retail_file_verifies(self):
        """Across three key revisions, four titles and both regions.

        Nothing in this package made these and nothing in it can: the
        private key is Sony's. What it can do is check them, and a checker
        that accepts fifteen real signatures is a checker.
        """
        need_curves()
        ran = 0
        for item in chosen():
            if item.kind != "self":
                continue
            with self.subTest(sample=item.key):
                self.assertTrue(self.verifies(item.path, item.klicensee))
                ran += 1
        if not ran:
            self.skipTest("none of the corpus is on this machine")

    def test_a_signature_over_a_header_that_moved_does_not_verify(self):
        """Otherwise the check above would pass on anything."""
        need_curves()
        item = sample("mw2-eboot")
        parsed = keysmith.read(item.path)
        covered, signature = signature_input(parsed, item.klicensee)
        curve = ecdsa.curve(parsed.keyset.curve_type)
        self.assertTrue(ecdsa.verify(curve, parsed.keyset.public,
                                     ecdsa.digest_of(covered), signature))
        for at in (0, len(covered) // 2, len(covered) - 1):
            with self.subTest(byte=at):
                moved = bytearray(covered)
                moved[at] ^= 0x01
                self.assertFalse(ecdsa.verify(
                    curve, parsed.keyset.public,
                    ecdsa.digest_of(bytes(moved)), signature))

    def test_a_signature_with_a_half_of_zero_does_not_verify(self):
        """Which is what a custom firmware build carries, and it is what a
        console checking signatures would refuse."""
        need_curves()
        item = sample("mw2-eboot")
        parsed = keysmith.read(item.path)
        covered, _ = signature_input(parsed, item.klicensee)
        curve = ecdsa.curve(parsed.keyset.curve_type)
        self.assertFalse(ecdsa.verify(
            curve, parsed.keyset.public, ecdsa.digest_of(covered),
            bytes(signing.SIGNATURE_BYTES)))

    def test_a_file_signed_here_for_hen_verifies_against_the_public_key(self):
        """The gate this whole path exists for.

        Built from a retail template at key revision 0x000A and checked the
        same way their builds are checked, against the public key in the
        keyset the file names.
        """
        need_curves()
        item = sample("mw2-eboot")
        elf = keysmith.decrypt(item.path, item.klicensee)
        built = keysmith.sign(elf, item.path, item.klicensee,
                              key_revision=0x000A)
        parsed = keysmith.read(built)
        self.assertEqual(parsed.sce.key_revision, 0x000A)
        covered, signature = signature_input(parsed, item.klicensee)
        self.assertNotEqual(signature[:signing.SIGNATURE_BYTES],
                            bytes(signing.SIGNATURE_BYTES))
        self.assertTrue(ecdsa.verify(
            ecdsa.curve(parsed.keyset.curve_type), parsed.keyset.public,
            ecdsa.digest_of(covered), signature))

    def test_a_re_signed_custom_firmware_build_verifies_like_theirs(self):
        """Their HEN build and this rebuild of the custom firmware one it
        ships beside, both checked against the same public key."""
        need_curves()
        if not FULL:
            self.skipTest("set KEYSMITH_CORPUS=1 for the large samples")
        for tree, region, name in TheSectionTableAHenBuildCarries.PAIRS:
            path = os.path.join(self.PAIRED_BUILDS, tree, region, "cfw", name)
            if not os.path.isfile(path):
                self.skipTest(f"{path} is not on this machine")
            with self.subTest(region=region, name=name):
                elf = keysmith.decrypt(path, corpus.IW_KLIC)
                built = keysmith.read(keysmith.sign(
                    elf, path, corpus.IW_KLIC, filename=name,
                    key_revision=0x000A))
                covered, signature = signature_input(built, corpus.IW_KLIC)
                self.assertTrue(ecdsa.verify(
                    ecdsa.curve(built.keyset.curve_type),
                    built.keyset.public, ecdsa.digest_of(covered),
                    signature))

    def test_the_nonce_scetool_signed_with_is_a_different_one_every_time(
            self):
        """Which is why a signature made here cannot match one of theirs.

        ECDSA takes a nonce and a signature says nothing about which one was
        used, unless the private key is to hand. Here it is, so the nonce
        comes straight back out of each signature, and all fourteen builds
        used a different one. A fixed nonce is a known habit of PS3 tooling
        and it would have made a byte-for-byte match possible; this says
        plainly that scetool does not have it.

        The input differs between their build and a rebuild here in any case,
        because both put random per-section keys in the table that the
        signature covers.
        """
        need_curves()
        store = keys.load()
        keyset = [item for item in store.keysets
                  if item.type == "SELF" and item.self_type == "NPDRM"
                  and item.revision == 0x000A][0]
        curve = ecdsa.curve(keyset.curve_type)
        private = int.from_bytes(keyset.private, "big")
        nonces = set()
        for path in self.hen_builds():
            parsed = keysmith.read(path)
            covered, signature = signature_input(parsed, corpus.IW_KLIC)
            r, s = ecdsa.split(signature)
            value = int.from_bytes(ecdsa.digest_of(covered), "big")
            # k comes out of the signing equation, s = (z + r * d) / k, once
            # d is known.
            nonces.add((value + r * private)
                       * pow(s, -1, curve.order) % curve.order)
        self.assertEqual(len(nonces), len(self.hen_builds()))
        self.assertNotIn(0, nonces)

    def test_the_same_nonce_gives_the_same_signature_twice(self):
        """The nonce is the only thing that moves, so fixing it fixes the
        signature. This is what makes the arithmetic testable at all."""
        need_curves()
        store = keys.load()
        keyset = [item for item in store.keysets
                  if item.type == "SELF" and item.self_type == "NPDRM"
                  and item.revision == 0x000A][0]
        curve = ecdsa.curve(keyset.curve_type)
        digest = ecdsa.digest_of(b"a header that is not one")
        first = ecdsa.sign(curve, keyset.private, digest, nonce=0x1234)
        second = ecdsa.sign(curve, keyset.private, digest, nonce=0x1234)
        self.assertEqual(first, second)
        self.assertEqual(len(first), signing.SIGNATURE_BYTES)
        self.assertTrue(ecdsa.verify(curve, keyset.public, digest, first))

    def test_two_signatures_over_one_digest_differ_and_both_verify(self):
        """Because the nonce is drawn fresh each time, the way scetool's
        is."""
        need_curves()
        store = keys.load()
        keyset = [item for item in store.keysets
                  if item.type == "SELF" and item.self_type == "NPDRM"
                  and item.revision == 0x000A][0]
        curve = ecdsa.curve(keyset.curve_type)
        digest = ecdsa.digest_of(b"the same input twice")
        made = {ecdsa.sign(curve, keyset.private, digest) for _ in range(4)}
        self.assertEqual(len(made), 4)
        for signature in made:
            self.assertTrue(ecdsa.verify(curve, keyset.public, digest,
                                         signature))

    def test_a_retail_rebuild_carries_the_signature_it_came_with(self):
        """The retail path does not sign and must not start.

        No keyset for a retail key revision has a private key, and the
        byte-identical round trip depends on the field coming back out as it
        went in.
        """
        need_curves()
        item = sample("mw2-eboot")
        template = keysmith.read(item.path)
        self.assertFalse(signing.can_sign(template.keyset)
                         if template.keyset else False)
        before = template.decrypt_metadata(
            bytes.fromhex(item.klicensee)).signature
        elf = keysmith.decrypt(item.path, item.klicensee)
        rebuilt = keysmith.read(keysmith.sign(elf, item.path,
                                              item.klicensee))
        after = rebuilt.decrypt_metadata(
            bytes.fromhex(item.klicensee)).signature
        self.assertEqual(bytes(after), bytes(before))

    def test_a_keyset_with_no_private_key_says_so_rather_than_signing(self):
        need_curves()
        store = keys.load()
        retail = store.require_candidates("NPDRM", 0x0010)[0]
        self.assertFalse(signing.can_sign(retail))
        with self.assertRaises(keysmith.SigningFailed) as caught:
            ecdsa.sign(ecdsa.curve(retail.curve_type), retail.private,
                       ecdsa.digest_of(b"anything"))
        self.assertIn("private key", str(caught.exception))
