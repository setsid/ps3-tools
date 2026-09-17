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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "sce"))

import corpus                                             # noqa: E402
from ps3tools import keysmith                             # noqa: E402
from ps3tools.keysmith import aes, fself, keys, npdrm      # noqa: E402
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


def need_keys():
    if not HAVE_KEYS:
        raise unittest.SkipTest(
            f"no keyset at {keys.default_path()}; see docs/keysmith.md")


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
