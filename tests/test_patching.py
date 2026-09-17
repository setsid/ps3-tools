"""The write path, exercised against a mock console and a stand-in scetool.

Two things here are not optional.

Nothing in this file talks to anything but 127.0.0.1. There is a real console on
the network these were written on, and the mock binds to loopback explicitly
rather than to 0.0.0.0 so that even a mistake cannot reach it.

scetool is a Windows binary and cannot run on the machine these are written on,
so it is injected. The stand-in is not a stub that returns success: it takes a
fake container apart, refuses the wrong klicensee, and rebuilds the container
from whatever ELF it is handed, so the round trip the flow insists on is a real
round trip. What it does not do is prove that the real scetool's arguments are
right. That needs Windows and a real file, and nothing here can stand in for it.

Most of the weight is on the failures. A patcher that works when everything
works is not the interesting case: the interesting case is the console being
switched off halfway through somebody's only copy of a game binary.
"""

import datetime
import ftplib
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

from support import ROOT  # noqa: F401  (puts the project on sys.path)

from mock_webman import MockWebmanFtp

from ps3diag import patchstate, transport
from ps3tools import titles
from ps3tools.patching import backup as backups
from ps3tools.patching import flow, scetool
from ps3tools.patching.ftpwrite import FtpWriter

_spec = importlib.util.spec_from_file_location(
    "make_images", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures", "patching", "make_images.py"))
images = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(images)

BO2_ID = "BLES01717"
MW3_ID = "BLES01428"
BO2_CONTENT = "EP0002-BLES01717_00-CODBLOPS2PATCH09"
MW3_CONTENT = "EP0002-BLES01428_00-MW3P000000000124"


# --- the stand-in for scetool ---------------------------------------------

class FakeScetool:
    """Decrypt and re-sign, without the Windows binary that cannot run here.

    Deliberately strict about the two things that are easy to get wrong and
    invisible when they are: a file wants the klicensee it was built with, and
    the CID_FN hash follows the name handed to -g rather than the file it was
    written to.
    """

    problem = ""

    def __init__(self, fail_signing=None):
        self.calls = []
        self.fail_signing = fail_signing

    def info(self, path, klicensee=None):
        with open(path, "rb") as handle:
            fields, _key, _image = images.unwrap(handle.read())
        self.calls.append(("info", os.path.basename(path), klicensee))
        # Rendered and parsed back through the real parser, so that a change in
        # what scetool prints breaks here rather than on somebody's console.
        parsed = scetool.parse_header(images.header_text(fields))
        missing = [name for name in scetool.REQUIRED_FIELDS
                   if name not in parsed]
        if missing:
            raise scetool.ScetoolError(f"no {missing} in the header")
        parsed["raw"] = images.header_text(fields)
        return parsed

    def decrypt(self, path, destination, klicensee=None):
        with open(path, "rb") as handle:
            _fields, wanted, image = images.unwrap(handle.read())
        self.calls.append(("decrypt", os.path.basename(path), klicensee))
        if wanted != (klicensee or ""):
            raise scetool.ScetoolError(
                f"decrypting {os.path.basename(path)} produced nothing. The "
                f"usual cause is the wrong klicensee for this title.")
        with open(destination, "wb") as handle:
            handle.write(image)
        return image

    def sign(self, profile, info, source, elf_path, destination, target_name,
             klicensee=None):
        self.calls.append(("sign", target_name, klicensee, profile))
        if self.fail_signing and target_name in self.fail_signing:
            raise scetool.ScetoolError(f"signing {target_name} failed")
        with open(elf_path, "rb") as handle:
            image = handle.read()
        fields = {name: info[name] for name in images.info_for("x", "y", "z")
                  if name in info}
        fields["cid_fn_hash"] = images.cid_fn_hash(target_name)
        with open(destination, "wb") as handle:
            handle.write(images.wrap(fields, klicensee or "", image))
        return "signed"


# --- building a console ----------------------------------------------------

LINE = "-rw-rw-rw-   1 root     root  %11d Sep 06 19:51 %s"
GAME_DIR = ("drwxrwxrwx   1 root     root            0 Sep 06 19:51 "
            "BLES01717\ndrwxrwxrwx   1 root     root            0 Sep 13 "
            "10:39 BLES01428\ndrwxrwxrwx   1 root     root            0 Aug "
            "22 16:02 PSNPATCH\n")


def self_file(title_key, name, state="stock", content_id=None):
    """One fake SELF as it would sit in USRDIR."""
    record = next(item for item in titles.TITLES[title_key]["binaries"]
                  if item["name"] == name)
    site = titles.site_for(record)
    if title_key == "bo2":
        image = images.bo2_image(state, offset=site["file_offset"])
        fields = images.info_for(content_id or BO2_CONTENT,
                                 "UEXEC" if name == "EBOOT.BIN" else "USPRX",
                                 name, key_revision="001C",
                                 fw_version="0004002000000000")
    else:
        image = (images.mw3_image(state) if site
                 else images.mw3_image(state, offset=0x2000, size=0x4000))
        fields = images.info_for(content_id or MW3_CONTENT, "USPRX", name,
                                 key_revision="0019",
                                 fw_version="0004000000000000")
    return images.wrap(fields, record["klicensee"] or "", image)


def console(title_id, files, writable=True, fault=None):
    """A mock webMAN holding one USRDIR of the given files."""
    usrdir = titles.usrdir_for(title_id)
    listing = "\n".join(LINE % (len(body), name)
                        for name, body in sorted(files.items())) + "\n"
    listings = {usrdir + "/": listing.encode(),
                "/dev_hdd0/game/": GAME_DIR.encode()}
    stored = {f"{usrdir}/{name}": body for name, body in files.items()}
    return MockWebmanFtp(listings=listings, files=stored, writable=writable,
                         fault=fault)


def factory_for(server):
    def factory():
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", server.port, timeout=10)
        ftp.login("anonymous", "anonymous@")
        return ftp
    return factory


RETAIL_DUMP = """[*] SCE Header:
 Magic                   SCE
 Version                 0x2
 Key Revision            0x0010
 Header Type             [SELF]
[*] Application Info:
 Auth-ID                 [retail game/update]
 Vendor-ID               [normal]
 SELF-Type               [NPDRM Application]
 Version                 01.00
[*] Control Info:
 NPDRM Info
 License Type            [Free]
 App Type                {app_type}
 ContentID               UP0002-BLUS31011_00-CODBLOPS2PATCH09
"""


class WhatTheHeaderReadSaysWhenItCannotRead(unittest.TestCase):
    """A field that was printed and a field that was never there.

    A US copy of Black Ops II was reported as having no App type in its
    header, while scetool -i on the same file printed a complete Application
    Info block. The field was there. Its value was one this program has no
    entry for, and it was dropped without a word, so the message named a
    cause that was not the cause.
    """

    def scetool(self, output):
        """A signer whose report is this text, with nothing to read it off.

        describe() is the seam the old wrapper's runner was: the messages
        under test are built from a report, and where the report came from is
        not what is being checked.
        """
        from ps3tools.patching.signer import Signer

        class Canned:
            text = staticmethod(lambda: output)
            npdrm_is_zeroed = False
            fake_signed = False

        tool = Signer()
        tool.describe = lambda path, klicensee=None: Canned()
        return tool

    def test_a_value_it_does_not_know_is_kept_apart_from_a_missing_one(self):
        info, unreadable = scetool.read_header(
            RETAIL_DUMP.format(app_type="[0x08]"))
        self.assertNotIn("app_type", info)
        self.assertEqual(unreadable["app_type"], "[0x08]")
        # Everything else read, including the two fields that sit beside it
        # in the same encrypted block.
        self.assertEqual(info["licence_type"], "FREE")
        self.assertEqual(info["key_revision"], "0010")
        self.assertTrue(info["content_id"])

    def test_a_known_value_reads_as_it_always_did(self):
        info, unreadable = scetool.read_header(
            RETAIL_DUMP.format(app_type="[Update SPRX]"))
        self.assertEqual(info["app_type"], "USPRX")
        self.assertEqual(unreadable, {})

    def test_the_message_names_the_value_that_was_printed(self):
        tool = self.scetool(RETAIL_DUMP.format(app_type="[0x08]"))
        with self.assertRaises(scetool.ScetoolError) as caught:
            tool.info("t6mp_ps3f.self")
        said = str(caught.exception)
        self.assertIn("gives its App type as '[0x08]'", said)
        self.assertNotIn("has no App type", said)
        # And the whole dump is still there to be read.
        self.assertIn("Key Revision", said)

    def test_a_field_that_really_is_absent_still_says_so(self):
        without = "\n".join(line for line in
                             RETAIL_DUMP.format(app_type="[Update SPRX]")
                             .splitlines()
                             if "App Type" not in line)
        tool = self.scetool(without)
        with self.assertRaises(scetool.ScetoolError) as caught:
            tool.info("t6mp_ps3f.self")
        self.assertIn("has no App type in its header", str(caught.exception))

    def test_parse_header_still_answers_the_way_it_did(self):
        parsed = scetool.parse_header(
            RETAIL_DUMP.format(app_type="[Update SPRX]"))
        self.assertEqual(parsed["app_type"], "USPRX")


#: What came back on a real console, in full. Seven lines and it stops.
TRUNCATED_DUMP = """scetool 0.2.9 <public build> (C) 2011-2012 by naehrwert
NP local license handling (C) 2012 by flatz

[*] SCE Header:
 Magic           0x53434500 [OK]
 Version         0x00000002
 Key Revision    0x0010
"""


class OutputThatStopsPartWayThrough(unittest.TestCase):
    """A file this program could not read is not one it has read and rejected.

    Measured on a console: t6mp_ps3f.self was reported as having no App type
    in its header. scetool -i on the same file from a shell printed the whole
    thing, Application Info and all. What the program captured stopped after
    the key revision, and every field below that point was called absent.
    """

    def scetool(self, output):
        """A signer whose report is this text, with nothing to read it off.

        describe() is the seam the old wrapper's runner was: the messages
        under test are built from a report, and where the report came from is
        not what is being checked.
        """
        from ps3tools.patching.signer import Signer

        class Canned:
            text = staticmethod(lambda: output)
            npdrm_is_zeroed = False
            fake_signed = False

        tool = Signer()
        tool.describe = lambda path, klicensee=None: Canned()
        return tool

    def test_a_dump_that_stops_early_is_not_called_a_missing_field(self):
        tool = self.scetool(TRUNCATED_DUMP)
        with self.assertRaises(scetool.ScetoolError) as caught:
            tool.info("t6mp_ps3f.self")
        said = str(caught.exception)
        self.assertNotIn("has no", said)
        self.assertNotIn("App type", said)
        self.assertIn("stopped before it printed the details", said)
        self.assertIn("read that did not finish", said)

    def test_it_says_how_much_came_back_and_where_it_stopped(self):
        tool = self.scetool(TRUNCATED_DUMP)
        with self.assertRaises(scetool.ScetoolError) as caught:
            tool.info("t6mp_ps3f.self")
        said = str(caught.exception)
        self.assertIn("6 line(s)", said)
        self.assertIn("Key Revision    0x0010", said)

    def test_a_dump_that_reached_the_details_is_judged_on_them(self):
        # The other side of it. Output that got as far as Application Info
        # and is missing a field is a statement about the file.
        tool = self.scetool(RETAIL_DUMP.format(app_type="[0x08]"))
        with self.assertRaises(scetool.ScetoolError) as caught:
            tool.info("t6mp_ps3f.self")
        self.assertIn("gives its App type", str(caught.exception))

    def test_reaching_the_block_is_what_separates_the_two(self):
        self.assertFalse(scetool.reached_app_info(TRUNCATED_DUMP))
        self.assertTrue(scetool.reached_app_info(
            RETAIL_DUMP.format(app_type="[Update SPRX]")))
        self.assertFalse(scetool.reached_app_info(""))


class AShortCopyOffTheConsole(unittest.TestCase):
    """The console said one size and sent another.

    The transfer raises when the server answers SIZE and the count does not
    match. A server that will not answer SIZE leaves that check with nothing
    to compare, and the part of the file that arrived was then read as though
    it were the file: scetool printed the first header and stopped, and the
    scan called the fields below it absent.

    The listing is the second opinion, and the scan already has it in hand.
    """

    LISTED = 14215768

    class ShortWriter:
        """A console that hands back part of a file and says nothing."""

        def __init__(self, listed, sending):
            self.listed = listed
            self.sending = sending
            self.asked = []

        def list_dir(self, path):
            return ("drwxrwxrwx 1 root root 0 Jan 1 00:00 .\n"
                    "-rw-rw-rw- 1 root root %d Jan 1 00:00 t6mp_ps3f.self"
                    % self.listed)

        def retrieve(self, path, destination, on_block=None):
            self.asked.append(path)
            with open(destination, "wb") as handle:
                handle.write(b"\x00" * self.sending)
            return {"bytes": self.sending, "sha1": "0" * 40}

    class ExplodingScetool:
        """Reaching scetool at all is the failure this test is about."""

        problem = ""

        def info(self, path, klicensee=None):
            raise AssertionError("part of a file was handed to scetool")

        def decrypt(self, *args, **kwargs):
            raise AssertionError("part of a file was handed to scetool")

    def scan_with(self, sending):
        writer = self.ShortWriter(self.LISTED, sending)
        return flow.scan(writer, self.ExplodingScetool(), BO2_ID), writer

    def test_part_of_a_file_is_never_read_as_though_it_were_the_file(self):
        report, _writer = self.scan_with(4096)
        item = report.file_for("t6mp_ps3f.self")
        self.assertEqual(item.state, flow.NOT_EXAMINED)

    def test_it_names_both_numbers_so_the_cause_is_visible(self):
        report, _writer = self.scan_with(4096)
        item = report.file_for("t6mp_ps3f.self")
        self.assertIn("4096 bytes arrived", item.detail)
        self.assertIn(str(self.LISTED), item.detail)

    def test_nothing_is_concluded_about_the_file_itself(self):
        # NOT_EXAMINED rather than UNRECOGNISED. The first says this program
        # could not look, the second says it looked and did not know what it
        # was seeing, and only the second is a claim about somebody's game.
        report, _writer = self.scan_with(4096)
        self.assertEqual(report.unrecognised, [])
        self.assertEqual([item.name for item in report.not_examined],
                         ["t6mp_ps3f.self"])
        self.assertFalse(report.can_patch)

    def test_a_whole_file_is_read_the_way_it_always_was(self):
        # The guard only fires on a mismatch. A copy that arrived in full goes
        # to scetool, which is what the exploding stub proves by raising.
        with self.assertRaises(AssertionError):
            self.scan_with(self.LISTED)


class ConsoleCase(unittest.TestCase):
    """Starts a mock console and gives out writers pointed at it."""

    def start(self, title_id, files, writable=True, fault=None):
        server = console(title_id, files, writable=writable, fault=fault)
        server.start()
        self.addCleanup(server.stop)
        self.server = server
        self.title_id = title_id
        return server

    def writer(self, cls=FtpWriter, **extra):
        made = cls("127.0.0.1", timeout=10, factory=factory_for(self.server),
                   **extra)
        self.addCleanup(made.close)
        return made

    def workspace(self):
        folder = tempfile.mkdtemp(prefix="ps3tools-test-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return folder

    def remote(self, name):
        return f"{titles.usrdir_for(self.title_id)}/{name}"

    def image_of(self, blob):
        _fields, _key, image = images.unwrap(blob)
        return image


# --- the write client ------------------------------------------------------

class TheWriteClient(ConsoleCase):
    def setUp(self):
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self")})

    def test_it_lists_sizes_and_reads_a_whole_file(self):
        writer = self.writer()
        text = writer.list_dir(titles.usrdir_for(MW3_ID))
        self.assertIn("default_mp.self", text)
        remote = self.remote("default_mp.self")
        self.assertEqual(writer.size(remote),
                         len(self.server.files[remote]))
        self.assertEqual(writer.retrieve_bytes(remote),
                         self.server.files[remote])

    def test_it_stores_and_deletes(self):
        writer = self.writer()
        folder = self.workspace()
        path = os.path.join(folder, "small")
        with open(path, "wb") as handle:
            handle.write(b"hello")
        writer.store(path, "/dev_hdd0/tmp/small")
        self.assertEqual(self.server.written["/dev_hdd0/tmp/small"], b"hello")
        self.assertTrue(writer.delete("/dev_hdd0/tmp/small"))
        self.assertFalse(writer.delete("/dev_hdd0/tmp/gone"))

    def test_a_dead_control_connection_is_reconnected_once(self):
        opened = []
        base = factory_for(self.server)
        dropped = []

        def factory():
            ftp = base()
            opened.append(ftp)
            return ftp

        def fault(verb, argument):
            # A console that has been sat idle, or that is busy with a disc,
            # drops the control connection rather than answering.
            if verb == "LIST" and not dropped:
                dropped.append(verb)
                return "DROP"
            return None

        self.server.fault = fault
        writer = FtpWriter("127.0.0.1", timeout=10, factory=factory)
        self.addCleanup(writer.close)
        self.assertIn("default_mp.self",
                      writer.list_dir(titles.usrdir_for(MW3_ID)))
        self.assertEqual(len(opened), 2)

    def test_a_refusal_is_not_retried(self):
        opened = []
        base = factory_for(self.server)

        def factory():
            ftp = base()
            opened.append(ftp)
            return ftp

        writer = FtpWriter("127.0.0.1", timeout=10, factory=factory)
        self.addCleanup(writer.close)
        with self.assertRaises(ftplib.error_perm):
            writer.retrieve_bytes("/dev_hdd0/game/nothing/here")
        self.assertEqual(len(opened), 1)


# --- the scan --------------------------------------------------------------

class TheScan(ConsoleCase):
    def scan(self, title_id, tool=None):
        return flow.scan(self.writer(), tool or FakeScetool(), title_id)

    def test_an_unknown_title_id_is_refused_without_touching_the_console(self):
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self")})
        report = flow.scan(self.writer(), FakeScetool(), "BLJM61034")
        self.assertFalse(report.ok)
        self.assertIn("not one of the games this tool fixes", report.error)
        self.assertFalse(report.can_patch)
        self.assertEqual(self.server.commands, [])

    def test_every_known_title_id_is_accepted_and_nothing_else_is(self):
        for title_id in titles.KNOWN_TITLE_IDS:
            self.assertIsNotNone(titles.config_for(title_id), title_id)
        for title_id in titles.NOT_A_TITLE + ("BLES01702", "", "BLUS99999"):
            self.assertIsNone(titles.config_for(title_id), title_id)

    def test_a_release_nobody_has_confirmed_is_attempted_not_refused(self):
        # The whole of the change. BLES01430 is a published Modern Warfare 3
        # release with no verified record, and the old table would have stopped
        # at the title ID without reading a byte. It is now scanned like any
        # other, and the files come back understood.
        self.assertFalse(titles.is_verified("BLES01430"))
        self.assertTrue(titles.is_recognised("BLES01430"))
        content = "EP0002-BLES01430_00-MW3P000000000124"
        self.start("BLES01430",
                   {"default_mp.self": self_file("mw3", "default_mp.self",
                                                 content_id=content),
                    "default.self": self_file("mw3", "default.self",
                                              content_id=content)})
        report = self.scan("BLES01430")
        self.assertTrue(report.ok, report.error)
        self.assertFalse(report.verified)
        self.assertEqual(report.file_for("default_mp.self").state,
                         flow.NOT_PATCHED)
        self.assertTrue(report.can_patch)

    def test_an_unverified_release_still_has_its_offset_cross_checked(self):
        # Decryption succeeding is not permission to patch wherever the fix
        # points. The site still has to be where the verified table says, and
        # a release nobody has confirmed gets no latitude on that.
        record = next(item for item in titles.MW3_BINARIES
                      if item["name"] == "default_mp.self")
        odd = images.wrap(
            images.info_for("EP0002-BLES01430_00-MW3P000000000124", "USPRX",
                            "default_mp.self", key_revision="0019",
                            fw_version="0004000000000000"),
            record["klicensee"],
            images.mw3_image("stock", offset=0x2000))
        self.start("BLES01430", {"default_mp.self": odd})
        report = self.scan("BLES01430")
        item = report.file_for("default_mp.self")
        self.assertEqual(item.state, flow.UNRECOGNISED)
        self.assertIn("00330D20", item.detail)
        self.assertFalse(report.can_patch)

    def test_a_release_whose_files_will_not_open_is_its_own_answer(self):
        # The failure the attempt exists to find. Nothing here is the user's
        # doing, nothing is missing from this program, and the file was not
        # misread, so it must not be told as any of those three.
        record = next(item for item in titles.MW3_BINARIES
                      if item["name"] == "default_mp.self")
        locked = images.wrap(
            images.info_for("EP0002-BLES01430_00-MW3P000000000124", "USPRX",
                            "default_mp.self", key_revision="0019",
                            fw_version="0004000000000000"),
            record["klicensee"][::-1], images.mw3_image("stock"))
        self.start("BLES01430", {"default_mp.self": locked})
        report = self.scan("BLES01430")
        item = report.file_for("default_mp.self")
        self.assertEqual(item.state, flow.CANNOT_DECRYPT)
        self.assertNotEqual(item.state, flow.UNRECOGNISED)
        self.assertNotEqual(item.state, flow.NOT_EXAMINED)
        # The title ID is the one thing worth reporting, so it is in the words
        # the user sees rather than only in the window behind them.
        self.assertIn("BLES01430", item.detail)
        self.assertFalse(report.can_patch)
        self.assertEqual(report.unrecognised, [])
        self.assertEqual(report.not_examined, [])
        for forbidden in ("not installed", "your fault", "fault in this "
                          "program"):
            self.assertNotIn(forbidden, item.detail.lower())
        self.assertEqual(self.server.written, {})

    def test_the_verified_and_unverified_distinction_is_queryable(self):
        self.assertTrue(titles.is_verified(MW3_ID))
        self.assertTrue(titles.is_verified(BO2_ID))
        self.assertFalse(titles.is_verified("BLES01430"))
        # Every verified release is a recognised one. The reverse is the
        # common case now and must not quietly become the same set again.
        self.assertTrue(set(titles.VERIFIED_TITLE_IDS)
                        <= set(titles.KNOWN_TITLE_IDS))
        self.assertLess(len(titles.VERIFIED_TITLE_IDS),
                        len(titles.KNOWN_TITLE_IDS))
        for title_id, key in titles.VERIFIED_TITLE_IDS.items():
            self.assertEqual(titles.KNOWN_TITLE_IDS[title_id], key)
            self.assertIsNotNone(titles.sku_for(title_id), title_id)

    def test_the_two_halves_recognise_exactly_the_same_releases(self):
        # The list is written out twice: once here for the patcher and once in
        # ps3diag.patchstate for the read-only half, which is not allowed to
        # import this one. Drift between them is a game the report cannot name
        # and the patcher can, so it is caught here rather than on a console.
        for spec in patchstate.TITLES:
            mine = sorted(title_id
                          for title_id, key in titles.KNOWN_TITLE_IDS.items()
                          if key == spec.key)
            self.assertEqual(sorted(spec.title_ids), mine, spec.key)
            self.assertEqual(
                sorted(spec.verified_title_ids),
                sorted(title_id
                       for title_id, key in titles.VERIFIED_TITLE_IDS.items()
                       if key == spec.key), spec.key)

    def test_a_title_id_in_neither_list_is_neither_game(self):
        for title_id in ("BLJS10032", "BLES01702", "BLUS99999", "NPEB02143",
                         "BLJM61034"):
            self.assertFalse(titles.is_recognised(title_id), title_id)
            self.assertIsNone(titles.config_for(title_id), title_id)
            self.assertFalse(titles.is_verified(title_id), title_id)

    def test_state_comes_from_the_bytes_and_not_from_the_size(self):
        stock = self_file("mw3", "default_mp.self", "stock")
        patched = self_file("mw3", "default_mp.self", "patched")
        # The fix replaces four bytes, so the two files are the same length.
        # Anything reading the size would have to call them the same.
        self.assertEqual(len(stock), len(patched))

        self.start(MW3_ID, {"default_mp.self": stock,
                            "default.self": self_file("mw3", "default.self")})
        report = self.scan(MW3_ID)
        self.assertEqual(report.file_for("default_mp.self").state,
                         flow.NOT_PATCHED)
        self.assertTrue(report.can_patch)

        self.tearDown_servers()
        self.start(MW3_ID, {"default_mp.self": patched,
                            "default.self": self_file("mw3", "default.self")})
        report = self.scan(MW3_ID)
        self.assertEqual(report.file_for("default_mp.self").state,
                         flow.PATCHED)
        self.assertFalse(report.can_patch)

    def tearDown_servers(self):
        self.server.stop()

    def test_default_self_is_reported_but_has_no_patch_site(self):
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self"),
                            "default.self": self_file("mw3", "default.self")})
        report = self.scan(MW3_ID)
        other = report.file_for("default.self")
        self.assertEqual(other.state, flow.NO_SITE)
        self.assertTrue(other.present)
        self.assertEqual([item.name for item in report.to_patch],
                         ["default_mp.self"])

    def test_bytes_matching_neither_value_are_never_patched(self):
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self",
                                                         "neither")})
        report = self.scan(MW3_ID)
        item = report.file_for("default_mp.self")
        self.assertEqual(item.state, flow.UNRECOGNISED)
        self.assertTrue(item.sha1)
        self.assertFalse(report.can_patch)

        result = flow.patch(self.writer(), FakeScetool(), report,
                            root=self.workspace())
        self.assertFalse(result.ok)
        self.assertEqual(self.server.written, {})

    def test_a_site_at_the_wrong_offset_is_unrecognised(self):
        # The instructions are all there, just not where the verified table for
        # this title says they are. That is a different build, and guessing
        # which of the two answers to believe is how an install stops booting.
        record = next(item for item in titles.MW3_BINARIES
                      if item["name"] == "default_mp.self")
        odd = images.wrap(
            images.info_for(MW3_CONTENT, "USPRX", "default_mp.self",
                            key_revision="0019",
                            fw_version="0004000000000000"),
            record["klicensee"],
            images.mw3_image("stock", offset=0x2000))
        self.start(MW3_ID, {"default_mp.self": odd})
        report = self.scan(MW3_ID)
        item = report.file_for("default_mp.self")
        self.assertEqual(item.state, flow.UNRECOGNISED)
        self.assertIn("00330D20", item.detail)
        self.assertFalse(report.can_patch)

    def test_the_files_that_will_not_open_are_named_and_left_alone(self):
        # Two of the three files need the klicensee; EBOOT.BIN does not and
        # opens regardless. Patching that one alone is the half-fixed install
        # the readme warns about, so it is said in full and the decision is
        # left with the user rather than taken from them.
        content = "EP0002-BLES01720_00-CODBLOPS2PATCH09"
        files = {"EBOOT.BIN": self_file("bo2", "EBOOT.BIN",
                                        content_id=content)}
        for name in ("t6_ps3f.self", "t6mp_ps3f.self"):
            record = next(item for item in titles.BO2_BINARIES
                          if item["name"] == name)
            site = titles.site_for(record)
            files[name] = images.wrap(
                images.info_for(content, "USPRX", name, key_revision="001C",
                                fw_version="0004002000000000"),
                record["klicensee"][::-1],
                images.bo2_image("stock", offset=site["file_offset"]))
        self.start("BLES01720", files)
        report = self.scan("BLES01720")
        self.assertEqual(sorted(item.name for item in report.cannot_decrypt),
                         ["t6_ps3f.self", "t6mp_ps3f.self"])
        self.assertEqual([item.name for item in report.to_patch],
                         ["EBOOT.BIN"])
        self.assertEqual(report.error, "")
        self.assertTrue(report.can_patch)
        # And the pair that would be left half done is named.
        pairs = report.half_pairs()
        self.assertEqual(len(pairs), 1)
        _image, writing, leaving = pairs[0]
        self.assertEqual(writing, ["EBOOT.BIN"])
        self.assertEqual(leaving, ["t6_ps3f.self"])
        result = flow.patch(self.writer(), FakeScetool(), report,
                            root=self.workspace())
        self.assertEqual([path.rsplit("/", 1)[-1]
                          for path in sorted(self.server.written)],
                         ["EBOOT.BIN"])
        self.assertEqual(result.changed, ["EBOOT.BIN"])

    def test_the_wrong_klicensee_on_a_verified_release_blames_nobody(self):
        # Same failure on a release the fix has worked on before, where the
        # signing parameters are not the suspect. It is still the same state:
        # this program cannot open the file and will not touch it.
        record = next(item for item in titles.MW3_BINARIES
                      if item["name"] == "default_mp.self")
        wrong = images.wrap(
            images.info_for(MW3_CONTENT, "USPRX", "default_mp.self",
                            key_revision="0019",
                            fw_version="0004000000000000"),
            record["klicensee"][::-1], images.mw3_image("stock"))
        self.start(MW3_ID, {"default_mp.self": wrong})
        report = self.scan(MW3_ID)
        item = report.file_for("default_mp.self")
        self.assertEqual(item.state, flow.CANNOT_DECRYPT)
        self.assertIn(MW3_ID, item.detail)
        self.assertIn("worked on before", item.detail)
        self.assertFalse(report.can_patch)

    def test_the_wrong_klicensee_is_reported_rather_than_guessed_at(self):
        record = next(item for item in titles.MW3_BINARIES
                      if item["name"] == "default_mp.self")
        wrong = images.wrap(
            images.info_for(MW3_CONTENT, "USPRX", "default_mp.self",
                            key_revision="0019",
                            fw_version="0004000000000000"),
            record["klicensee"][::-1], images.mw3_image("stock"))
        self.start(MW3_ID, {"default_mp.self": wrong})
        report = self.scan(MW3_ID)
        item = report.file_for("default_mp.self")
        # Not unrecognised: nothing in the file was read and found wrong. What
        # happened is that it would not open, and that has its own state.
        self.assertEqual(item.state, flow.CANNOT_DECRYPT)
        self.assertIn("klicensee", item.detail)
        self.assertFalse(report.can_patch)

    def test_black_ops_two_scans_all_three_files(self):
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self",
                                         "t6mp_ps3f.self")})
        report = self.scan(BO2_ID)
        self.assertEqual(sorted(item.name for item in report.to_patch),
                         ["EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self"])
        self.assertTrue(report.can_patch)

    def test_one_unrecognised_file_does_not_stop_the_other_two(self):
        # Measured on hardware: EBOOT.BIN already patched, t6_ps3f.self stock,
        # t6mp_ps3f.self a build this program did not recognise. The screen
        # refused to write anything at all, which left the console in the
        # half-patched state it was opened to get out of.
        #
        # These are separate binaries patched separately. EBOOT.BIN and
        # t6_ps3f.self carry campaign and zombies, t6mp_ps3f.self carries
        # multiplayer, and one of them being a stranger says nothing about
        # whether the others can be patched safely.
        files = {name: self_file("bo2", name)
                 for name in ("EBOOT.BIN", "t6_ps3f.self")}
        files["t6mp_ps3f.self"] = self_file("bo2", "t6mp_ps3f.self", "neither")
        self.start(BO2_ID, files)
        report = self.scan(BO2_ID)
        self.assertEqual(len(report.to_patch), 2)
        self.assertTrue(report.can_patch)
        self.assertEqual([item.name for item in report.blocked],
                         ["t6mp_ps3f.self"])

    def test_the_unrecognised_file_is_still_never_written(self):
        files = {name: self_file("bo2", name)
                 for name in ("EBOOT.BIN", "t6_ps3f.self")}
        files["t6mp_ps3f.self"] = self_file("bo2", "t6mp_ps3f.self", "neither")
        self.start(BO2_ID, files)
        report = self.scan(BO2_ID)
        self.assertNotIn("t6mp_ps3f.self",
                         [item.name for item in report.chosen])
        out = flow.patch(self.writer(), FakeScetool(), report,
                         root=self.workspace())
        self.assertNotIn("t6mp_ps3f.self", out.uploaded)
        self.assertNotIn("t6mp_ps3f.self", out.changed)

    def test_a_missing_file_does_not_stop_the_ones_that_are_there(self):
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self")})
        report = self.scan(BO2_ID)
        self.assertEqual(report.missing, ["t6mp_ps3f.self"])
        self.assertTrue(report.can_patch)
        self.assertEqual(len(report.chosen), 2)

    def test_files_from_two_different_installs_are_refused(self):
        files = {name: self_file("bo2", name)
                 for name in ("EBOOT.BIN", "t6_ps3f.self")}
        files["t6mp_ps3f.self"] = self_file(
            "bo2", "t6mp_ps3f.self",
            content_id="UP0002-BLUS31011_00-CODBLOPS2PATCH09")
        self.start(BO2_ID, files)
        report = self.scan(BO2_ID)
        self.assertFalse(report.ok)
        self.assertIn("ContentID", report.error)
        self.assertFalse(report.can_patch)

    def test_a_scetool_that_cannot_run_stops_the_scan_with_its_own_reason(self):
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self")})

        class Unusable(FakeScetool):
            problem = "scetool is a Windows program and this is not Windows."

        report = self.scan(MW3_ID, Unusable())
        self.assertFalse(report.ok)
        self.assertIn("Windows", report.error)

    def test_the_signer_runs_on_whatever_this_is(self):
        """What replaced the bundled Windows binary.

        scetool was an exe, so on anything but Windows the whole patcher was
        unavailable and every test had to inject a stand-in. The signer is
        Python and its keyset lives inside the package, so it works here, and
        the only thing that can be wrong with it is the keys file.
        """
        from ps3tools.patching.signer import Signer
        real = Signer()
        self.assertEqual(real.problem, "")
        self.assertTrue(real.available)

    def test_a_keys_file_that_is_not_there_says_which_file(self):
        from ps3tools.patching.signer import Signer
        broken = Signer(keys_path="/nowhere/at/all/keys")
        self.assertIn("/nowhere/at/all/keys", broken.problem)
        self.assertFalse(broken.available)


# --- backup ----------------------------------------------------------------

class TheBackup(ConsoleCase):
    def setUp(self):
        self.files = {"default_mp.self": self_file("mw3", "default_mp.self")}
        self.start(MW3_ID, self.files)

    def test_it_copies_and_checks_against_the_console(self):
        folder = os.path.join(self.workspace(), "backup")
        saved = backups.make(self.writer(), titles.usrdir_for(MW3_ID),
                             ["default_mp.self"], folder)
        entry = saved.entry_for("default_mp.self")
        with open(entry["path"], "rb") as handle:
            self.assertEqual(handle.read(), self.files["default_mp.self"])
        self.assertEqual(
            entry["sha1"],
            hashlib.sha1(self.files["default_mp.self"]).hexdigest())

    def test_it_refuses_when_the_local_folder_cannot_be_made(self):
        blocker = os.path.join(self.workspace(), "in-the-way")
        with open(blocker, "wb") as handle:
            handle.write(b"not a folder")
        with self.assertRaises(backups.BackupFailed) as caught:
            backups.make(self.writer(), titles.usrdir_for(MW3_ID),
                         ["default_mp.self"],
                         os.path.join(blocker, "backup"))
        self.assertIn("Nothing has been changed", str(caught.exception))

    def test_it_refuses_when_the_console_will_not_hand_the_file_over(self):
        self.server.fault = lambda verb, argument: (
            "550 no such file" if verb == "RETR" else None)
        with self.assertRaises(backups.BackupFailed):
            backups.make(self.writer(), titles.usrdir_for(MW3_ID),
                         ["default_mp.self"],
                         os.path.join(self.workspace(), "backup"))


# --- the whole patch -------------------------------------------------------

class ThePatch(ConsoleCase):
    def scan_and_patch(self, title_id, files, fault=None, writer_class=None,
                       root=None, tool=None, **writer_extra):
        self.start(title_id, files)
        tool = tool or FakeScetool()
        report = flow.scan(self.writer(), tool, title_id)
        self.assertTrue(report.can_patch, report.error or report.notes)
        if fault is not None:
            self.server.fault = fault
        writer = self.writer(writer_class or FtpWriter, **writer_extra)
        result = flow.patch(writer, tool, report,
                            root=root or self.workspace())
        return report, result

    def test_a_clean_run_writes_the_patched_file_and_keeps_the_original(self):
        original = self_file("mw3", "default_mp.self")
        report, result = self.scan_and_patch(
            MW3_ID, {"default_mp.self": original,
                     "default.self": self_file("mw3", "default.self")})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.changed, ["default_mp.self"])

        remote = self.remote("default_mp.self")
        self.assertEqual(list(self.server.written), [remote])
        landed = self.image_of(self.server.written[remote])
        site = titles.PATCH_SITES["mw3-multiplayer"]
        self.assertEqual(landed[site["file_offset"]:site["file_offset"] + 4],
                         site["patched"])
        # default.self has no patch site and must not have been touched.
        self.assertNotIn(self.remote("default.self"), self.server.written)

        entry = result.backup.entry_for("default_mp.self")
        with open(entry["path"], "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertTrue(os.path.isdir(result.folder))

    def test_black_ops_two_writes_all_three_files(self):
        files = {name: self_file("bo2", name)
                 for name in ("EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self")}
        report, result = self.scan_and_patch(BO2_ID, files)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(sorted(result.changed),
                         ["EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self"])
        campaign = titles.PATCH_SITES["bo2-campaign"]
        multiplayer = titles.PATCH_SITES["bo2-multiplayer"]
        for name, site in (("EBOOT.BIN", campaign),
                           ("t6_ps3f.self", campaign),
                           ("t6mp_ps3f.self", multiplayer)):
            landed = self.image_of(self.server.written[self.remote(name)])
            self.assertEqual(
                landed[site["file_offset"]:site["file_offset"] + 4],
                site["patched"], name)

    def test_the_name_on_the_console_is_what_feeds_the_filename_hash(self):
        # -g has to be the name the file will carry, not whatever it was
        # written to. A file with the wrong CID_FN hash is valid and will not
        # load, which is the failure that looks like nothing at all.
        tool = FakeScetool()
        self.scan_and_patch(MW3_ID,
                            {"default_mp.self": self_file("mw3",
                                                          "default_mp.self")},
                            tool=tool)
        signed = [call for call in tool.calls if call[0] == "sign"]
        self.assertEqual([call[1] for call in signed], ["default_mp.self"])
        self.assertEqual(signed[0][2], titles.MW3_KLICENSEE)

    def test_a_backup_that_cannot_be_written_stops_before_anything_else(self):
        blocker = os.path.join(self.workspace(), "in-the-way")
        with open(blocker, "wb") as handle:
            handle.write(b"not a folder")
        _report, result = self.scan_and_patch(
            MW3_ID, {"default_mp.self": self_file("mw3", "default_mp.self")},
            root=blocker)
        self.assertFalse(result.ok)
        self.assertIn("Nothing has been changed", result.error)
        self.assertEqual(self.server.written, {})
        self.assertEqual(result.changed, [])

    def test_a_backup_the_console_will_not_hand_over_stops_before_anything(self):
        def fault(verb, argument):
            return "550 no such file" if verb == "RETR" else None

        _report, result = self.scan_and_patch(
            MW3_ID, {"default_mp.self": self_file("mw3", "default_mp.self")},
            fault=fault)
        self.assertFalse(result.ok)
        self.assertEqual(self.server.written, {})
        self.assertIsNone(result.backup)

    def test_an_upload_that_dies_mid_write_is_detected_and_rolled_back(self):
        original = self_file("mw3", "default_mp.self")
        attempts = []

        def fault(verb, argument):
            if verb != "STOR":
                return None
            attempts.append(argument)
            # The upload and its one reconnect both die; the restore after them
            # is allowed to work, which is what the user is left with.
            return "TRUNCATE:1000" if len(attempts) <= 2 else None

        _report, result = self.scan_and_patch(
            MW3_ID, {"default_mp.self": original}, fault=fault)
        self.assertFalse(result.ok)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.restored, ["default_mp.self"])
        self.assertEqual(result.restore_failed, [])
        remote = self.remote("default_mp.self")
        self.assertEqual(self.server.written[remote], original)
        self.assertEqual(self.server.files[remote], original)

    def test_a_file_that_reads_back_wrong_is_rolled_back_and_reported(self):
        original = self_file("mw3", "default_mp.self")
        server_holder = self

        class Corrupting(FtpWriter):
            """The console says 226 and keeps something else. A real one does
            this when it is out of space or the filesystem is damaged."""

            stores = 0

            def store(self, source, path, on_block=None):
                sent = super().store(source, path, on_block)
                Corrupting.stores += 1
                if Corrupting.stores == 1:
                    held = bytearray(server_holder.server.files[path])
                    held[-1] ^= 0xFF
                    server_holder.server.files[path] = bytes(held)
                return sent

        Corrupting.stores = 0
        _report, result = self.scan_and_patch(
            MW3_ID, {"default_mp.self": original}, writer_class=Corrupting)
        self.assertFalse(result.ok)
        self.assertIn("does not match what was sent", result.error)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.restored, ["default_mp.self"])
        remote = self.remote("default_mp.self")
        self.assertEqual(self.server.files[remote], original)

    def test_the_console_disappearing_mid_patch_leaves_a_usable_backup(self):
        original = self_file("mw3", "default_mp.self")

        def fault(verb, argument):
            return "DROP" if verb == "STOR" else None

        _report, result = self.scan_and_patch(
            MW3_ID, {"default_mp.self": original}, fault=fault)
        self.assertFalse(result.ok)
        self.assertEqual(result.changed, [])
        self.assertEqual(self.server.written, {})
        # Nothing landed, so nothing could be put back either. The user has to
        # be told where their files are and what to do with them.
        self.assertTrue(result.restore_failed)
        self.assertIn(result.folder, result.error)
        self.assertIn("by hand", result.error)
        entry = result.backup.entry_for("default_mp.self")
        with open(entry["path"], "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_signing_that_fails_writes_nothing_at_all(self):
        _report, result = self.scan_and_patch(
            MW3_ID, {"default_mp.self": self_file("mw3", "default_mp.self")},
            tool=FakeScetool(fail_signing={"default_mp.self"}))
        self.assertFalse(result.ok)
        self.assertEqual(self.server.written, {})
        self.assertIn("Nothing has been written", result.error)

    def test_a_rebuild_that_does_not_come_back_the_same_writes_nothing(self):
        class Careless(FakeScetool):
            """Signs something other than what it was handed. The flow has to
            catch that before it reaches the console."""

            def sign(self, profile, info, source, elf_path, destination,
                     target_name, klicensee=None):
                with open(elf_path, "r+b") as handle:
                    handle.seek(0x800)
                    handle.write(b"\xDE\xAD\xBE\xEF")
                return super().sign(profile, info, source, elf_path,
                                    destination, target_name, klicensee)

        _report, result = self.scan_and_patch(
            MW3_ID, {"default_mp.self": self_file("mw3", "default_mp.self")},
            tool=Careless())
        self.assertFalse(result.ok)
        self.assertEqual(self.server.written, {})

    def test_progress_is_reported_per_file_and_per_stage(self):
        events = []
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self")})
        tool = FakeScetool()
        report = flow.scan(self.writer(), tool, MW3_ID, progress=events.append)
        result = flow.patch(self.writer(), tool, report,
                            root=self.workspace(), progress=events.append)
        self.assertTrue(result.ok, result.error)
        stages = {event["stage"] for event in events}
        self.assertLessEqual({"scan", "backup", "build", "upload", "done"},
                             stages)
        self.assertTrue(any(event.get("bytes") for event in events),
                        "no byte counts were reported, so the bar is a spinner")


# --- the screen ------------------------------------------------------------

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from ps3tools.shell.screen import ConnectionState, Services, Theme
    from ps3tools.screens import patcher
except ImportError:  # pragma: no cover - PySide6 is a hard dependency
    QApplication = None


class StubTheme(Theme if QApplication else object):
    changed = None

    def colour(self, token):
        return "#808080"

    @property
    def dark(self):
        return False


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class ScreenCase(ConsoleCase):
    """Builds a patcher screen with its three seams pointed somewhere safe."""

    application = None

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.application = QApplication.instance() or QApplication([])

    def build(self, screen_class, host="127.0.0.1"):
        connection = ConnectionState(host)
        services = Services(connection, StubTheme())
        screen = screen_class(services)
        self.addCleanup(screen.deleteLater)
        server = getattr(self, "server", None)
        if server is not None:
            screen._scetool = lambda: FakeScetool()
            screen._lister = lambda host: transport.FtpLister(
                host, factory=factory_for(server))
            screen._writer = lambda host: FtpWriter(
                host, factory=factory_for(server))
        return screen, services

    def settle(self, services):
        """Wait until nothing is outstanding and every result has been
        delivered.

        Draining the pool once and pumping is not enough: the handler that
        runs when a task finishes often starts another, and between the first
        finishing and the second being submitted the pool is genuinely idle.
        Services drops a task when its done signal reaches the GUI thread, so
        the queue has to be pumped before running_tasks() means anything.
        """
        deadline = time.monotonic() + 30.0
        quiet = 0
        while time.monotonic() < deadline:
            self.application.processEvents()
            idle = services.wait(50)
            self.application.processEvents()
            if services.running_tasks() or not idle:
                quiet = 0
                continue
            quiet += 1
            if quiet >= 3:
                for _ in range(10):
                    self.application.processEvents()
                return
        outstanding = ", ".join(services.running()) or "nothing named"
        raise AssertionError(
            f"work did not settle within thirty seconds; still running: "
            f"{outstanding}")


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class TheScreen(ScreenCase):
    def test_both_cards_register_from_one_class(self):
        from ps3tools.shell import registry
        keys = {item.key: item for item in registry.screens()}
        self.assertIn("bo2", keys)
        self.assertIn("mw3", keys)
        self.assertIs(keys["bo2"].__bases__[0], patcher.PatcherScreen)
        self.assertIs(keys["mw3"].__bases__[0], patcher.PatcherScreen)
        self.assertEqual((keys["bo2"].tile, keys["bo2"].order), ("B2", 20))
        self.assertEqual((keys["mw3"].tile, keys["mw3"].order), ("M3", 30))

    def test_with_no_address_it_says_so_rather_than_hanging(self):
        screen, services = self.build(patcher.BlackOpsTwoPatcher, host="")
        self.assertIsNone(screen.start_scan())
        self.assertFalse(screen._patch.isEnabled())
        # The state panel carries this now; the detail line underneath was
        # repeating it word for word and reading as a second problem.
        self.assertEqual(screen._detail.text(), "")
        self.assertIn("address", screen._panel_heading.text().lower())
        self.assertIn("address", screen._panel_body.text().lower())
        self.settle(services)

    def test_entering_the_screen_scans_without_a_button_being_pressed(self):
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self"),
                            "default.self": self_file("mw3", "default.self")})
        screen, services = self.build(patcher.ModernWarfareThreePatcher)
        # Detection is Subagent D's and may not be there yet; the fallback has
        # to work either way.
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = None
        screen.on_enter()
        self.settle(services)
        self.assertEqual(screen._files.topLevelItemCount(), 2)
        self.assertTrue(screen._patch.isEnabled())
        self.assertIn("default_mp.self", screen._detail.text())
        self.assertEqual(self.server.written, {})

    def no_title_update(self):
        """The screen after a scan of a console with the game but no update."""
        screen, services = self.build(patcher.BlackOpsTwoPatcher)
        found = flow.Location(flow.NO_UPDATE, title_ids=[BO2_ID])
        screen._on_scanned((found, None, ""))
        return screen

    def test_the_no_update_state_offers_to_fetch_it_from_game_updates(self):
        # The advice was to put the console online and launch the game, which
        # is the slow way round something this program already does.
        screen = self.no_title_update()
        self.assertFalse(screen._panel_button.isHidden())
        asked = []
        screen.request_tool.connect(lambda key, title: asked.append((key,
                                                                     title)))
        screen._panel_button.click()
        self.assertEqual(asked, [("updates", "bo2")])

    def test_launching_the_game_is_still_offered_as_the_other_way(self):
        screen = self.no_title_update()
        words = screen._panel_body.text()
        self.assertIn("Game updates can fetch it", words)
        self.assertIn("let it download its own update", words)

    def test_nothing_was_patched_so_there_is_nothing_to_put_back(self):
        screen = self.no_title_update()
        self.assertFalse(screen._restore.isEnabled())

    def test_it_refuses_to_navigate_away_while_writing(self):
        screen, _services = self.build(patcher.BlackOpsTwoPatcher)
        self.assertTrue(screen.can_leave())
        screen._writing = True
        self.assertFalse(screen.can_leave())



# --- what the screen says about the state of the console -------------------

from ps3tools import detect as detection  # noqa: E402


class StubLister:
    """A console that answers, refuses, or is not there at all.

    Stands in for the read-only transport so that each of the four ways a
    search can fail can be produced on demand. A real console that is switched
    off cannot be arranged from a test, and the difference between "switched
    off" and "answered with something unreadable" is the whole point of this.
    """

    def __init__(self, listing=GAME_DIR, open_error=None, list_error=None,
                 later_error=None):
        self.listing = listing
        self.open_error = open_error
        self.list_error = list_error
        self.later_error = later_error
        self.lists = 0

    def open(self):
        if self.open_error:
            raise self.open_error
        return self

    def list_dir(self, path):
        self.lists += 1
        if self.list_error:
            raise self.list_error
        if self.later_error and self.lists > 1:
            raise self.later_error
        return self.listing

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def installation(state, title_id=BO2_ID, key="bo2", tu_version=None,
                 expected=(), missing=()):
    config = titles.TITLES[key]
    unknown = state == "unknown_variant"
    return detection.Installation(
        title_id=title_id, title_key=None if unknown else key,
        name=config["name"], short=config["short"],
        path=f"/dev_hdd0/game/{title_id}",
        usrdir=titles.usrdir_for(title_id), state=state, config=config,
        expected=list(expected), missing=list(missing),
        tu_version=tu_version,
        # An unnamed release carries the game its files say it is, which is
        # what decides whether a screen may see it at all.
        candidate_keys=(key,) if unknown else (),
        verified=titles.is_verified(title_id))


def detector_for(*found, notes=()):
    report = detection.Report(installations=list(found), notes=list(notes))
    return lambda lister: report


class TheSearchForAnInstallation(unittest.TestCase):
    """flow.locate. Four failures, and none of them may be told as another."""

    def test_a_console_that_does_not_answer_is_not_an_empty_hard_drive(self):
        lister = StubLister(open_error=OSError("timed out"))
        found = flow.locate(lister, "bo2", detector=detector_for())
        self.assertEqual(found.state, flow.UNREACHABLE)
        self.assertIn("timed out", found.reason)
        self.assertEqual(lister.lists, 0)

    def test_a_folder_that_cannot_be_listed_is_this_tools_problem(self):
        lister = StubLister(list_error=ftplib.error_perm("550 access denied"))
        found = flow.locate(lister, "bo2", detector=detector_for())
        self.assertEqual(found.state, flow.LIST_FAILED)
        self.assertIn("550", found.reason)

    def test_a_listing_in_an_unknown_format_is_not_read_as_nothing_installed(self):
        lister = StubLister(listing="totally unexpected\nlines of nonsense\n")
        found = flow.locate(lister, "bo2", detector=detector_for())
        self.assertEqual(found.state, flow.LIST_FAILED)
        self.assertIn("totally unexpected", found.reason)

    def test_a_console_that_dies_during_the_walk_is_not_nothing_installed(self):
        # find_installations keeps what it found and notes the reason, so an
        # empty result on its own cannot be trusted. The second listing is
        # what separates an empty hard drive from a console being switched off.
        lister = StubLister(later_error=EOFError())
        found = flow.locate(lister, "bo2", detector=detector_for())
        self.assertEqual(found.state, flow.UNREACHABLE)

    def test_an_answering_console_with_no_copy_of_the_game_is_not_found(self):
        lister = StubLister()
        found = flow.locate(lister, "bo2", detector=detector_for())
        self.assertEqual(found.state, flow.NOT_INSTALLED)
        self.assertEqual(found.reason, "")

    def test_installed_without_a_title_update_keeps_its_own_state(self):
        lister = StubLister()
        found = flow.locate(lister, "bo2",
                            detector=detector_for(installation("no_update")))
        self.assertEqual(found.state, flow.NO_UPDATE)
        self.assertEqual(found.title_id, BO2_ID)

    def test_an_unknown_sku_is_kept_apart_from_a_missing_update(self):
        lister = StubLister()
        found = flow.locate(
            lister, "bo2",
            detector=detector_for(installation("unknown_variant")))
        self.assertEqual(found.state, flow.UNKNOWN_VARIANT)

    def test_a_ready_installation_wins_over_an_incomplete_one(self):
        lister = StubLister()
        found = flow.locate(
            lister, "bo2",
            detector=detector_for(installation("no_update"),
                                  installation("ready")))
        self.assertEqual(found.state, flow.READY)
        self.assertEqual(found.title_ids, [BO2_ID])

    def test_the_folder_holding_the_binaries_beats_a_licence_only_folder(self):
        # Measured on hardware. The console had NPUB31054 with the game's
        # binaries in it and a leftover BLUS31011 holding licence files and
        # nothing else. BLUS31011 was listed first and was taken, so the run
        # found nothing to patch and told the user the title update had not
        # been downloaded.
        wanted = [item["name"] for item in titles.binaries_for(BO2_ID)]
        licences = installation("no_update", title_id="BLUS31011",
                                expected=wanted, missing=wanted)
        game = installation("no_update", title_id="NPUB31054",
                            expected=wanted, missing=wanted[:1])
        found = flow.locate(StubLister(), "bo2",
                            detector=detector_for(licences, game))
        self.assertEqual(found.title_id, "NPUB31054")
        self.assertEqual(found.installation.title_id, "NPUB31054")

    def test_one_folder_is_not_reported_as_a_choice_between_folders(self):
        # A console with a single copy of the game is the ordinary case. A
        # sentence about which folder was picked, printed on every scan, is
        # furniture and gets read past, which is how the one scan where it
        # mattered would be missed.
        wanted = [item["name"] for item in titles.binaries_for(BO2_ID)]
        found = flow.locate(
            StubLister(), "bo2",
            detector=detector_for(installation("ready", expected=wanted)))
        self.assertEqual(found.state, flow.READY)
        self.assertEqual(
            [note for note in found.notes if "more than one folder" in note],
            [])

    def test_the_note_names_the_folder_that_was_used_and_the_others(self):
        # The user has no other way of telling which of their folders the fix
        # was about to be applied to, and that is the one thing they can check
        # for themselves before anything is written.
        wanted = [item["name"] for item in titles.binaries_for(BO2_ID)]
        licences = installation("no_update", title_id="BLUS31011",
                                expected=wanted, missing=wanted)
        game = installation("no_update", title_id="NPUB31054",
                            expected=wanted, missing=wanted[:1])
        found = flow.locate(StubLister(), "bo2",
                            detector=detector_for(licences, game))
        said = [note for note in found.notes if "more than one folder" in note]
        self.assertEqual(len(said), 1)
        self.assertIn("NPUB31054 was used", said[0])
        self.assertIn("BLUS31011", said[0])

    def test_without_the_detection_module_the_two_plain_answers_still_work(self):
        self.assertEqual(flow.locate(StubLister(), "bo2").state, flow.READY)
        self.assertEqual(
            flow.locate(StubLister(listing=""), "bo2").state,
            flow.NOT_INSTALLED)


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class TheStateTheScreenReports(ScreenCase):
    """Each state gets its own heading, its own colour and its own advice.

    The screen this replaced said "This game is not installed on the console,
    or the console could not be reached" for every one of these, which sent
    users to check a network cable when their game was simply waiting for its
    title update.
    """

    def screen_with(self, lister, detector=None):
        screen, services = self.build(patcher.BlackOpsTwoPatcher)
        screen._scetool = lambda: FakeScetool()
        screen._lister = lambda host: lister
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = (None if detector is None else
                          type("stub", (), {"find_installations":
                                            staticmethod(detector)}))
        screen.on_enter()
        self.settle(services)
        return screen

    def words(self, screen):
        return " ".join([screen._panel_heading.text(), screen._panel_body.text(),
                         screen._panel_reason.text()])

    def test_a_console_that_does_not_answer_says_what_to_check(self):
        screen = self.screen_with(StubLister(open_error=OSError("no route")),
                                  detector_for())
        self.assertTrue(screen._panel.isVisibleTo(screen))
        self.assertEqual(screen._panel_token, "error")
        self.assertIn("did not answer", screen._panel_heading.text())
        words = self.words(screen)
        for expected in ("switched on", "webMAN", "router", "main menu"):
            self.assertIn(expected, words)
        self.assertIn("no route", screen._panel_reason.text())
        self.assertFalse(screen._patch.isEnabled())

    def test_a_folder_that_could_not_be_listed_is_owned_not_blamed(self):
        screen = self.screen_with(
            StubLister(list_error=ftplib.error_perm("550 nope")),
            detector_for())
        self.assertEqual(screen._panel_token, "error")
        words = self.words(screen)
        self.assertIn("fault in this program", words)
        self.assertIn("you have not done anything to cause it", words)
        self.assertIn("550 nope", screen._panel_reason.text())
        # It must not tell the user their game is missing on the strength of a
        # listing this program could not read.
        self.assertNotIn("not installed", words)

    def test_nothing_installed_says_so_plainly_and_is_not_an_alarm(self):
        screen = self.screen_with(StubLister(), detector_for())
        self.assertEqual(screen._panel_token, "text_dim")
        words = self.words(screen)
        self.assertIn("not installed on this console", words)
        self.assertIn("no title update", words)

    def test_a_missing_title_update_is_not_presented_as_a_failure(self):
        screen = self.screen_with(
            StubLister(), detector_for(installation("no_update")))
        self.assertEqual(screen._panel_token, "info")
        self.assertNotIn(screen._panel_token, ("error", "warn"))
        words = self.words(screen)
        self.assertIn("title update has not been downloaded", words)
        self.assertIn("Nothing is wrong", words)
        # The instruction is the whole point of the state.
        self.assertIn("start", words.lower())
        self.assertIn("Scan again", words)
        for forbidden in ("error", "failed", "could not be reached"):
            self.assertNotIn(forbidden, words.lower() if forbidden.islower()
                             else words)

    def test_a_folder_that_could_be_two_games_is_the_one_refusal_left(self):
        # An unfamiliar title ID on its own is no longer refused: it comes
        # through as an untested release and is warned about. What is left
        # here is the folder whose files could belong to more than one game,
        # where nothing on the console says which it is.
        screen = self.screen_with(
            StubLister(), detector_for(installation("unknown_variant")))
        self.assertEqual(screen._panel_token, "warn")
        words = self.words(screen)
        self.assertIn("cannot tell which game", words)
        self.assertIn("more than one game", words)
        self.assertIn("exactly as it was", words)
        self.assertFalse(screen._patch.isEnabled())

    def test_the_refusal_never_puts_another_game_in_front_of_anybody(self):
        # A Black Ops 1 user was offered Ghosts here, deleted it, was offered
        # Modern Warfare 2, and deleted game data chasing it. Nothing on this
        # panel names another game or asks for anything to be removed.
        screen = self.screen_with(
            StubLister(), detector_for(installation("unknown_variant")))
        words = self.words(screen).lower()
        for forbidden in ("delete", "remove", "uninstall", "ghosts"):
            self.assertNotIn(forbidden, words)

    def test_no_two_states_say_the_same_thing(self):
        said = set()
        for detector in (detector_for(),
                         detector_for(installation("no_update")),
                         detector_for(installation("unknown_variant"))):
            screen = self.screen_with(StubLister(), detector)
            said.add(screen._panel_heading.text())
        screen = self.screen_with(StubLister(open_error=OSError("gone")),
                                  detector_for())
        said.add(screen._panel_heading.text())
        screen = self.screen_with(
            StubLister(list_error=ftplib.error_perm("550 nope")),
            detector_for())
        said.add(screen._panel_heading.text())
        self.assertEqual(len(said), 5, said)

    def test_a_ready_installation_shows_the_files_and_no_panel(self):
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self"),
                            "default.self": self_file("mw3", "default.self")})
        screen, services = self.build(patcher.ModernWarfareThreePatcher)
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = type("stub", (), {"find_installations": staticmethod(
            detector_for(installation("ready", MW3_ID, "mw3")))})
        screen.on_enter()
        self.settle(services)
        self.assertEqual(screen._panel_token, "")
        self.assertFalse(screen._panel.isVisibleTo(screen))
        self.assertEqual(screen._files.topLevelItemCount(), 2)
        self.assertTrue(screen._patch.isEnabled())
        self.assertEqual(self.server.written, {})



class AFileThatIsNotTheBinaryItShouldBe(ConsoleCase):
    """A mod menu puts one of the game's binaries under another's name.

    The usual shape is EBOOT.BIN replaced with a re-signed copy of the
    multiplayer binary, set to load an SPRX at boot. It decrypts, it is a
    binary of this game, and it is not the one that name is supposed to hold.
    Patched at the campaign site it would produce a file that starts and then
    fails once multiplayer does, which is a fault nobody would trace back to
    this program.

    The patch sites are found by the signature of the code around them, so the
    signatures answer this too: the file does not carry the site it should and
    does carry a different one of the same title's.
    """

    def swapped_eboot(self, state="stock"):
        """EBOOT.BIN holding the multiplayer image instead of the campaign."""
        record = next(item for item in titles.TITLES["bo2"]["binaries"]
                      if item["name"] == "EBOOT.BIN")
        site = titles.PATCH_SITES["bo2-multiplayer"]
        image = images.bo2_image(state, offset=site["file_offset"])
        fields = images.info_for(BO2_CONTENT, "UEXEC", "EBOOT.BIN",
                                 key_revision="001C",
                                 fw_version="0004002000000000")
        return images.wrap(fields, record["klicensee"] or "", image)

    def scan_with_swapped_eboot(self, state="stock"):
        files = {name: self_file("bo2", name)
                 for name in ("t6_ps3f.self", "t6mp_ps3f.self")}
        files["EBOOT.BIN"] = self.swapped_eboot(state)
        self.start(BO2_ID, files)
        return flow.scan(self.writer(), FakeScetool(), BO2_ID)

    def test_it_is_reported_as_replaced_rather_than_unrecognised(self):
        report = self.scan_with_swapped_eboot()
        item = report.file_for("EBOOT.BIN")
        self.assertEqual(item.state, flow.REPLACED)
        self.assertNotIn(item, report.unrecognised)
        self.assertIn(item, report.replaced)

    def test_it_says_what_the_file_actually_is(self):
        report = self.scan_with_swapped_eboot()
        said = report.file_for("EBOOT.BIN").detail
        self.assertIn("EBOOT.BIN", said)
        self.assertIn("replaced", said)
        self.assertIn("mp.elf", said)
        self.assertIn("multiplayer", said)

    def test_it_is_never_written_to(self):
        report = self.scan_with_swapped_eboot()
        self.assertNotIn("EBOOT.BIN",
                         [item.name for item in report.to_patch])
        self.assertNotIn("EBOOT.BIN", [item.name for item in report.chosen])
        self.assertIn(report.file_for("EBOOT.BIN"), report.blocked)

    def test_the_files_beside_it_are_still_judged_on_their_own(self):
        # One meddled file does not stop the rest. The other two are ordinary
        # and are still offered.
        report = self.scan_with_swapped_eboot()
        self.assertEqual(
            sorted(item.name for item in report.to_patch),
            ["t6_ps3f.self", "t6mp_ps3f.self"])
        self.assertTrue(report.can_patch)

    def test_it_is_not_reported_as_an_error_on_the_whole_title(self):
        # Its own state on its own row. A scan that came back as an error
        # would say nothing about the two files that are perfectly fine.
        report = self.scan_with_swapped_eboot()
        self.assertEqual(report.error, "")

    def test_the_scan_says_so_in_its_notes(self):
        report = self.scan_with_swapped_eboot()
        self.assertTrue(any("EBOOT.BIN" in note and "replaced" in note
                            for note in report.notes), report.notes)

    def test_an_already_patched_impostor_is_still_an_impostor(self):
        # The state of the site it does carry says nothing about whether the
        # file belongs under this name.
        report = self.scan_with_swapped_eboot(state="patched")
        self.assertEqual(report.file_for("EBOOT.BIN").state, flow.REPLACED)

    def test_an_ordinary_set_is_untouched_by_the_check(self):
        files = {name: self_file("bo2", name)
                 for name in ("EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self")}
        self.start(BO2_ID, files)
        report = flow.scan(self.writer(), FakeScetool(), BO2_ID)
        self.assertEqual(report.replaced, [])
        self.assertEqual(sorted(item.name for item in report.to_patch),
                         ["EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self"])

    def test_a_build_nobody_has_seen_is_not_called_replaced(self):
        """The two are different things and must not be told as each other.

        A file whose site is somewhere else is a build this fix was not
        written for. It is only a replaced file when the binary it does hold
        is another of this title's, which is a thing that can be named.
        """
        record = next(item for item in titles.TITLES["bo2"]["binaries"]
                      if item["name"] == "EBOOT.BIN")
        image = images.bo2_image("stock", offset=0x3000)
        fields = images.info_for(BO2_CONTENT, "UEXEC", "EBOOT.BIN",
                                 key_revision="001C",
                                 fw_version="0004002000000000")
        files = {name: self_file("bo2", name)
                 for name in ("t6_ps3f.self", "t6mp_ps3f.self")}
        files["EBOOT.BIN"] = images.wrap(fields, record["klicensee"] or "",
                                         image)
        self.start(BO2_ID, files)
        report = flow.scan(self.writer(), FakeScetool(), BO2_ID)
        self.assertEqual(report.file_for("EBOOT.BIN").state,
                         flow.UNRECOGNISED)
        self.assertEqual(report.replaced, [])


class ChoosingWhichCopyToPatch(ScreenCase):
    """Two supported releases of the same game on one console.

    The screen used to take whichever sorted first and say so in a line under
    the table. Somebody with both the European and the American disc had no
    way to say which one they play, and no way to tell which one was about to
    be written to.
    """

    OTHER = "BLUS31011"

    def screen_with(self, *found, answer=None):
        """A screen whose scan finds `found`, with the chooser stubbed.

        answer is the title ID the dialogue returns, or None for a cancel.
        """
        screen, services = self.build(patcher.BlackOpsTwoPatcher)
        screen._scetool = lambda: FakeScetool()
        screen._lister = lambda host: StubLister()
        self.asked = []

        def choose(title_ids):
            self.asked.append(list(title_ids))
            return answer or ""

        screen._choose_release = choose
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        detector = detector_for(*found)
        patcher.detect = type("stub", (), {"find_installations":
                                           staticmethod(detector)})
        screen.on_enter()
        self.settle_twice(services)
        return screen, services

    def settle_twice(self, services):
        """The scan that stops to ask, then the scan the answer starts."""
        for _ in range(3):
            self.settle(services)

    def test_one_release_is_not_a_question(self):
        screen, _services = self.screen_with(installation("ready"))
        self.assertEqual(self.asked, [])
        self.assertIn(BO2_ID, screen._where.text())

    def test_the_release_is_named_even_when_there_is_only_one(self):
        # Either way, the screen says which copy it read.
        screen, _services = self.screen_with(installation("ready"))
        self.assertIn(titles.usrdir_for(BO2_ID), screen._where.text())

    def test_two_releases_are_put_to_the_user(self):
        screen, _services = self.screen_with(
            installation("ready"), installation("ready", title_id=self.OTHER),
            answer=self.OTHER)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(sorted(self.asked[0]), sorted([BO2_ID, self.OTHER]))
        self.assertIn(self.OTHER, screen._where.text())

    def test_nothing_is_read_from_the_copy_that_was_not_chosen(self):
        screen, _services = self.screen_with(
            installation("ready"), installation("ready", title_id=self.OTHER),
            answer=self.OTHER)
        self.assertEqual(screen._scan.title_id, self.OTHER)
        self.assertNotIn(BO2_ID, screen._where.text())

    def test_saying_no_reads_nothing_and_says_why(self):
        screen, _services = self.screen_with(
            installation("ready"), installation("ready", title_id=self.OTHER),
            answer=None)
        self.assertIsNone(screen._scan)
        self.assertFalse(screen._patch.isEnabled())
        words = " ".join([screen._panel_heading.text(),
                          screen._panel_body.text(),
                          screen._panel_reason.text()])
        self.assertIn("more than one copy", words)
        self.assertIn(BO2_ID, words)
        self.assertIn(self.OTHER, words)

    def test_scanning_again_asks_again(self):
        # Otherwise a person who picked the wrong one has no way back to the
        # question short of leaving the screen.
        screen, services = self.screen_with(
            installation("ready"), installation("ready", title_id=self.OTHER),
            answer=self.OTHER)
        self.assertEqual(len(self.asked), 1)
        screen._on_rescan()
        self.settle_twice(services)
        self.assertEqual(len(self.asked), 2)


class ReconnectingCostsNothingWhenNobodyIsInAHurry(unittest.TestCase):
    """The pause before a reconnect is for one cause and should cost only it.

    webMANftpd hangs up when it has had several data connections in quick
    succession, and a fresh one opened microseconds later meets it in the same
    state. That is what the pause is for. A connection that sat unused through
    a several hundred megabyte download died of the server's idle timeout
    instead, and waiting there added seconds to the start of every upload that
    followed a download.
    """

    def writer_that_drops_once(self, last_ok_ago):
        from ps3tools.patching import ftpwrite
        slept = []
        attempts = []

        # The counter lives outside the class: reconnecting builds a fresh
        # connection through the factory, so per-instance state would reset
        # and the retry would fail the same way for ever.
        class Flaky:
            sock = None

            def getwelcome(inner):
                return "220 webMANftpd 1.47.48q MOD [NTFS:0]"

            def voidcmd(inner, command):
                attempts.append(command)
                if len(attempts) == 1:
                    raise ftplib.error_temp("421 goodbye")
                return "200 ok"

            def quit(inner):
                raise ftplib.error_temp("421 already gone")

            def close(inner):
                pass

        writer = ftpwrite.FtpWriter("127.0.0.1", factory=Flaky)
        writer._last_ok = (None if last_ok_ago is None
                           else time.monotonic() - last_ok_ago)
        with mock.patch.object(ftpwrite.time, "sleep", slept.append):
            writer._command(lambda ftp: ftp.voidcmd("NOOP"))
        return slept, attempts

    def test_a_drop_moments_after_working_waits_before_retrying(self):
        slept, attempts = self.writer_that_drops_once(last_ok_ago=1.0)
        self.assertEqual(slept, [ftpwrite_pause()])
        self.assertEqual(len(attempts), 2)

    def test_a_drop_long_after_working_retries_straight_away(self):
        # The console let go because nothing was happening. There is nothing
        # to wait for, and waiting is what made every upload start slowly.
        slept, attempts = self.writer_that_drops_once(last_ok_ago=600.0)
        self.assertEqual(slept, [])
        self.assertEqual(len(attempts), 2)

    def test_a_first_command_that_drops_does_not_wait(self):
        slept, _attempts = self.writer_that_drops_once(last_ok_ago=None)
        self.assertEqual(slept, [])

    def test_a_command_that_works_is_not_delayed_at_all(self):
        from ps3tools.patching import ftpwrite
        slept = []

        class Fine:
            sock = None

            def getwelcome(inner):
                return "220 webMANftpd 1.47.48q MOD [NTFS:0]"

            def voidcmd(inner, command):
                return "200 ok"

            def quit(inner):
                return "221 bye"

            def close(inner):
                pass

        writer = ftpwrite.FtpWriter("127.0.0.1", factory=Fine)
        with mock.patch.object(ftpwrite.time, "sleep", slept.append):
            writer._command(lambda ftp: ftp.voidcmd("NOOP"))
        self.assertEqual(slept, [])
        self.assertIsNotNone(writer._last_ok)


def ftpwrite_pause():
    from ps3tools.patching import ftpwrite
    return ftpwrite.RECONNECT_PAUSE


if __name__ == "__main__":
    unittest.main()


# --- a file this program could not look at at all --------------------------

class _NoPatchers:
    """Makes patcher_module answer None, the way an exe built without the
    vendored scripts did."""

    def __enter__(self):
        self._saved = dict(patchstate._patchers)
        patchstate._patchers.clear()
        patchstate._patchers.update({"bo2": None, "mw3": None})
        return self

    def __exit__(self, *_exc):
        patchstate._patchers.clear()
        patchstate._patchers.update(self._saved)
        return False


class AFileThatCouldNotBeExamined(ConsoleCase):
    """Separate from unrecognised, because they are separate faults.

    On a real console with a genuine install, all three Black Ops II files came
    back "not recognised" because the patcher script was not in the exe. The
    screen then warned the user about the wrong build of a binary, which is a
    statement about their files, when nothing of theirs had been read at all.
    """

    def scan(self, title_id):
        return flow.scan(self.writer(), FakeScetool(), title_id)

    def setUp(self):
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self",
                                         "t6mp_ps3f.self")})

    def test_a_missing_patcher_is_not_examined_rather_than_unrecognised(self):
        with _NoPatchers():
            report = self.scan(BO2_ID)
        self.assertEqual(len(report.not_examined), 3)
        self.assertEqual(report.unrecognised, [])
        self.assertFalse(report.can_patch)
        for item in report.not_examined:
            self.assertEqual(item.missing_tool, "patch-bo2.py")
            self.assertIsNone(item.offset)

    def test_the_reason_names_this_program_and_not_the_user_s_files(self):
        with _NoPatchers():
            report = self.scan(BO2_ID)
        detail = report.not_examined[0].detail
        self.assertIn("patch-bo2.py", detail)
        self.assertIn("fault in this program", detail)
        for blame in ("not one this fix was written for", "wrong build",
                      "stops starting", "not recognised"):
            self.assertNotIn(blame, detail)

    def test_the_two_states_do_not_share_a_sentence(self):
        with _NoPatchers():
            fault = self.scan(BO2_ID)
        odd = {name: self_file("bo2", name)
               for name in ("EBOOT.BIN", "t6_ps3f.self")}
        odd["t6mp_ps3f.self"] = self_file("bo2", "t6mp_ps3f.self", "neither")
        self.start(BO2_ID, odd)
        mismatch = self.scan(BO2_ID)
        self.assertNotEqual(fault.not_examined[0].detail,
                            mismatch.unrecognised[0].detail)
        self.assertNotIn("fault in this program",
                         mismatch.unrecognised[0].detail)
        # The mismatch reason talks about the file that was read. The tool
        # fault talks about a file this program did not ship.
        self.assertNotIn("patch-bo2.py", mismatch.unrecognised[0].detail)
        self.assertIn("Black Ops II binary", mismatch.unrecognised[0].detail)

    def test_the_notes_say_it_could_not_be_checked_not_that_it_is_wrong(self):
        with _NoPatchers():
            report = self.scan(BO2_ID)
        notes = " ".join(report.notes)
        self.assertIn("could not be checked by this program", notes)
        self.assertNotIn("was not recognised", notes)

    def test_applying_with_no_patcher_says_whose_fault_it_is(self):
        report = self.scan(BO2_ID)
        self.assertTrue(report.can_patch)
        with _NoPatchers():
            result = flow.patch(self.writer(), FakeScetool(), report,
                                root=self.workspace())
        self.assertFalse(result.ok)
        self.assertIn("patch-bo2.py", result.error)
        self.assertIn("fault in this program", result.error)
        self.assertEqual(self.server.written, {})


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class WhatTheScreenSaysAboutTheTwoStates(ScreenCase):
    def _screen_after_scan(self):
        screen, services = self.build(patcher.BlackOpsTwoPatcher)
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = None
        screen.on_enter()
        self.settle(services)
        return screen

    def the_mixed_install(self):
        """BLES01717 TU 1.19 exactly as it came off the user's console.

        EBOOT.BIN patched by this tool, t6_ps3f.self stock and wanting the
        patch, t6mp_ps3f.self a build this program does not recognise. The
        screen used to refuse to write anything at all.
        """
        files = {"EBOOT.BIN": self_file("bo2", "EBOOT.BIN", "patched"),
                 "t6_ps3f.self": self_file("bo2", "t6_ps3f.self", "stock"),
                 "t6mp_ps3f.self": self_file("bo2", "t6mp_ps3f.self",
                                             "neither")}
        self.start(BO2_ID, files)
        return self._screen_after_scan()

    def test_a_mixed_install_can_still_patch_the_file_that_needs_it(self):
        screen = self.the_mixed_install()
        self.assertTrue(screen._patch.isEnabled())
        self.assertEqual([item.name for item in screen._scan.chosen],
                         ["t6_ps3f.self"])

    def test_it_says_which_files_it_will_write_and_which_it_will_not(self):
        screen = self.the_mixed_install()
        words = screen._detail.text()
        self.assertIn("t6_ps3f.self will be replaced", words)
        self.assertIn("EBOOT.BIN is already fixed", words)
        self.assertIn("t6mp_ps3f.self is not a build this fix was written "
                      "for", words)

    def test_the_two_that_should_match_are_not_a_veto_when_one_is_patched(self):
        # They differ by exactly the four bytes of the fix, which is what a
        # half-finished patch looks like. Refusing on that left the console in
        # the state this screen exists to get it out of.
        screen = self.the_mixed_install()
        self.assertEqual(screen._scan.error, "")
        self.assertTrue(screen._scan.ok)

    def test_a_file_can_be_taken_out_of_the_write_by_hand(self):
        screen = self.the_mixed_install()
        row = self.row_named(screen, "t6_ps3f.self")
        self.assertTrue(row.flags() & Qt.ItemIsUserCheckable)
        row.setCheckState(0, Qt.Unchecked)
        self.assertEqual(screen._scan.chosen, [])
        self.assertFalse(screen._patch.isEnabled())
        self.assertIn("t6_ps3f.self was unticked", screen._detail.text())
        row.setCheckState(0, Qt.Checked)
        self.assertTrue(screen._patch.isEnabled())

    def test_a_file_the_user_excluded_stays_excluded_across_a_scan(self):
        # The same shape as the sticky overwrite flag and the Transfer games
        # queue re-ticking itself: a scan builds fresh file records, and the
        # tick a person took out came back on with them.
        screen = self.the_mixed_install()
        self.row_named(screen, "t6_ps3f.self").setCheckState(0, Qt.Unchecked)
        self.assertEqual(screen._scan.chosen, [])
        screen.start_scan()
        self.settle(screen.services if hasattr(screen, "services")
                    else self.services)
        self.assertEqual(
            self.row_named(screen, "t6_ps3f.self").checkState(0),
            Qt.Unchecked)
        self.assertEqual(screen._scan.chosen, [])

    def test_ticking_it_again_puts_it_back_across_a_scan(self):
        screen = self.the_mixed_install()
        row = self.row_named(screen, "t6_ps3f.self")
        row.setCheckState(0, Qt.Unchecked)
        row.setCheckState(0, Qt.Checked)
        screen.start_scan()
        self.settle(screen.services if hasattr(screen, "services")
                    else self.services)
        self.assertEqual(
            self.row_named(screen, "t6_ps3f.self").checkState(0), Qt.Checked)

    def test_an_unrecognised_file_is_told_where_else_to_try(self):
        # A game whose files somebody has already modified, by a mod menu that
        # replaces the eboot for instance, reads exactly like this from here.
        # The manual sequence can still do it, so the screen says so.
        screen = self.the_mixed_install()
        self.assertFalse(screen._next_step.isHidden())
        said = screen._next_step.text()
        self.assertIn("already been modified", said)
        self.assertIn("mod menu", said)
        self.assertIn(patcher.titles.TITLES["bo2"]["repo"], said)

    def test_the_link_goes_to_the_repository_and_not_the_exe(self):
        # The standalone exe uses the same detection as this screen and would
        # refuse the same file for the same reason.
        screen = self.the_mixed_install()
        said = screen._next_step.text()
        self.assertNotIn(".exe", said)
        self.assertIn("github.com/setsid/bo2-ps3-psn-freeze-fix", said)

    def test_nothing_is_said_when_every_file_was_recognised(self):
        # A line that is there whatever the scan found is furniture, and the
        # next real one is read as furniture too.
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self",
                                         "t6mp_ps3f.self")})
        screen = self._screen_after_scan()
        self.assertEqual(screen._scan.unrecognised, [])
        self.assertTrue(screen._next_step.isHidden())

    def test_a_file_that_cannot_be_written_has_no_tick_box(self):
        screen = self.the_mixed_install()
        for name in ("EBOOT.BIN", "t6mp_ps3f.self"):
            row = self.row_named(screen, name)
            self.assertFalse(row.flags() & Qt.ItemIsUserCheckable, name)

    def row_named(self, screen, name):
        for index in range(screen._files.topLevelItemCount()):
            row = screen._files.topLevelItem(index)
            if row.text(0) == name:
                return row
        raise AssertionError(f"no row for {name}")

    def test_a_tool_fault_does_not_warn_the_user_about_their_own_install(self):
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self",
                                         "t6mp_ps3f.self")})
        with _NoPatchers():
            screen = self._screen_after_scan()
        words = screen._detail.text()
        self.assertIn("patch-bo2.py", words)
        self.assertIn("fault in this program", words)
        self.assertNotIn("this fix was written for", words)
        self.assertNotIn("stops starting", words)
        self.assertFalse(screen._patch.isEnabled())
        self.assertEqual(self.server.written, {})

    def test_an_unrecognised_file_still_gets_the_mismatch_warning(self):
        files = {name: self_file("bo2", name)
                 for name in ("EBOOT.BIN", "t6_ps3f.self")}
        files["t6mp_ps3f.self"] = self_file("bo2", "t6mp_ps3f.self", "neither")
        self.start(BO2_ID, files)
        screen = self._screen_after_scan()
        words = screen._detail.text()
        self.assertIn("this fix was written for", words)
        self.assertNotIn("fault in this program", words)
        # The other two are ordinary files in an ordinary state, so the
        # button is live and the copy says which of the three it will write.
        self.assertTrue(screen._patch.isEnabled())

    def test_the_two_verdicts_are_not_the_same_words(self):
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self",
                                         "t6mp_ps3f.self")})
        with _NoPatchers():
            fault = self._screen_after_scan()._detail.text()
        files = {name: self_file("bo2", name)
                 for name in ("EBOOT.BIN", "t6_ps3f.self")}
        files["t6mp_ps3f.self"] = self_file("bo2", "t6mp_ps3f.self", "neither")
        self.start(BO2_ID, files)
        mismatch = self._screen_after_scan()._detail.text()
        self.assertTrue(fault)
        self.assertNotEqual(fault, mismatch)

    def test_the_column_does_not_call_an_unchecked_file_unrecognised(self):
        self.assertNotEqual(patcher.STATE_WORDS[flow.NOT_EXAMINED],
                            patcher.STATE_WORDS[flow.UNRECOGNISED])

    def test_it_does_not_ask_for_three_writes_when_one_is_already_done(self):
        # A console with EBOOT.BIN fixed on an earlier run was told all three
        # files still had to be done, under a table showing one of them
        # already fixed, which reads as the plan on screen being wrong.
        files = {"EBOOT.BIN": self_file("bo2", "EBOOT.BIN", "patched"),
                 "t6_ps3f.self": self_file("bo2", "t6_ps3f.self", "stock"),
                 "t6mp_ps3f.self": self_file("bo2", "t6mp_ps3f.self",
                                             "stock")}
        self.start(BO2_ID, files)
        screen = self._screen_after_scan()
        words = screen._detail.text()
        self.assertEqual([item.name for item in screen._scan.chosen],
                         ["t6_ps3f.self", "t6mp_ps3f.self"])
        self.assertNotIn("all three", words.lower())
        self.assertIn("One of the three files that carry the binary has the "
                      "fix already.", words)
        self.assertIn("Writing t6_ps3f.self and t6mp_ps3f.self completes the "
                      "set.", words)
        # The part that is true whatever the console holds stays put.
        self.assertIn("re-signed using the content ID", words)
        self.assertIn("Keep the backups", words)

    def test_it_still_says_all_three_when_none_of_them_is_done(self):
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self",
                                         "t6mp_ps3f.self")})
        screen = self._screen_after_scan()
        words = screen._detail.text()
        self.assertIn("All three files carry the binary and all three have "
                      "to end up fixed", words)
        self.assertIn("Keep the backups", words)

    def test_the_table_shows_every_row_of_a_three_file_title(self):
        # Two rows fitted on one console and EBOOT.BIN was scrolled out of
        # sight while the sentence below the table named it, which reads as a
        # file that is not on the console.
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self",
                                         "t6mp_ps3f.self")})
        screen = self._screen_after_scan()
        table = screen._files
        self.assertEqual(table.topLevelItemCount(), 3)
        row_height = table.sizeHintForRow(0)
        self.assertGreater(row_height, 0)
        # The height the table refuses to go below is the layout's floor, so
        # every row has room in it whatever else is on the screen.
        self.assertGreaterEqual(table.minimumHeight(),
                                table.header().height() + 3 * row_height)
        table.resize(table.width(), table.minimumHeight())
        self.application.processEvents()
        last = table.topLevelItem(table.topLevelItemCount() - 1)
        self.assertLessEqual(table.visualItemRect(last).bottom(),
                             table.viewport().height())


class _ColourfulTheme(Theme if QApplication else object):
    """Every token a different colour, so a control that is painted with the
    wrong one cannot pass by accident."""

    changed = None
    COLOURS = {"accent": "#1b5fd0", "accent_text": "#ffffff",
               "border": "#c6ccd8", "text_dim": "#525a68",
               "surface_alt": "#e4e8f0", "surface": "#ffffff",
               "text": "#12161d", "ok": "#0a6b45", "warn": "#8a4b00",
               "error": "#b3261e", "info": "#0f4fad", "bg": "#eef0f5"}

    def colour(self, token):
        return self.COLOURS[token]

    @property
    def dark(self):
        return False


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class TheApplyButton(ScreenCase):
    """It is the only control here that changes anything on the console, and
    it used to look exactly like the two that do not."""

    def _screen(self):
        connection = ConnectionState("127.0.0.1")
        services = Services(connection, _ColourfulTheme())
        screen = patcher.BlackOpsTwoPatcher(services)
        self.addCleanup(screen.deleteLater)
        return screen

    def test_enabled_it_is_filled_with_the_accent_and_its_own_text_colour(self):
        style = self._screen()._patch.styleSheet()
        self.assertIn(_ColourfulTheme.COLOURS["accent"], style)
        self.assertIn(_ColourfulTheme.COLOURS["accent_text"], style)

    def test_disabled_it_does_not_keep_the_accent_fill(self):
        style = self._screen()._patch.styleSheet()
        disabled = style.split("QPushButton:disabled", 1)[1]
        self.assertIn(_ColourfulTheme.COLOURS["text_dim"], disabled)
        self.assertNotIn(_ColourfulTheme.COLOURS["accent"], disabled)

    def test_no_colour_is_written_into_the_screen_by_hand(self):
        style = self._screen()._patch.styleSheet()
        for value in set(_ColourfulTheme.COLOURS.values()):
            style = style.replace(value, "")
        self.assertNotIn("#", style)


# --- what the screen does once the patch has finished ----------------------

def _patch_result(title_id, changed=(), error="", restored=(), notes=()):
    """A flow.PatchReport as the worker would hand one back."""
    result = flow.PatchReport(title_id)
    result.changed = list(changed)
    result.restored = list(restored)
    result.notes = list(notes)
    result.error = error
    return result


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class AfterAPatch(ScreenCase):
    """The table has to be read back off the console, not left as it was.

    On the first real run the three files were patched, the console had them,
    and the table still said "needs fixing" for all three because nothing had
    looked at the console since. The user is being asked to trust a grid that
    is describing a state that no longer exists.
    """

    BO2_FILES = ("EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self")

    def scanned_screen(self, state="stock"):
        self.start(BO2_ID, {name: self_file("bo2", name, state)
                            for name in self.BO2_FILES})
        screen, services = self.build(patcher.BlackOpsTwoPatcher)
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = type("stub", (), {"find_installations": staticmethod(
            detector_for(installation("ready", BO2_ID, "bo2")))})
        self.scans = []
        opener = screen._lister

        def counted(host):
            self.scans.append(host)
            return opener(host)

        screen._lister = counted
        screen.on_enter()
        self.settle(services)
        return screen, services

    def states(self, screen):
        return [screen._files.topLevelItem(row).text(3)
                for row in range(screen._files.topLevelItemCount())]

    def finish_patch(self, screen, result):
        """Deliver the two signals a finished patch task delivers, in order."""
        screen._writing = True
        screen._on_patched(result)
        screen._finished_writing()

    def patch_the_console(self):
        """What the write would have left behind, without doing the write.

        The bytes are put on the mock console directly. Nothing here is allowed
        to depend on how the patch is applied; the point of the read-back is
        that the screen finds out from the console rather than from the patch.
        """
        for name in self.BO2_FILES:
            self.server.files[self.remote(name)] = self_file(
                "bo2", name, "patched")

    # -- the good case

    def test_a_successful_patch_reads_the_console_back_exactly_once(self):
        screen, services = self.scanned_screen()
        self.assertEqual(self.states(screen), ["needs fixing"] * 3)
        self.assertEqual(len(self.scans), 1)

        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)

        self.assertEqual(len(self.scans), 2)
        self.assertEqual(self.states(screen), ["already fixed"] * 3)
        self.assertFalse(screen._patch.isEnabled())

    def test_the_read_back_does_not_wipe_the_result_the_user_just_earned(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)
        text = screen._detail.text()
        self.assertIn("Changed in %s: EBOOT.BIN, t6_ps3f.self, "
                      "t6mp_ps3f.self" % BO2_ID, text)
        # And the fresh verdict is there as well, under it.
        self.assertIn("already fixed", text)

    def test_the_screen_is_held_until_the_read_back_has_finished(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        # Between the last byte written and the table being redrawn there is no
        # moment at which the user can walk away and come back to the old one.
        self.assertFalse(screen.can_leave())
        self.assertIn("read back", screen.leave_blocked_reason())
        self.settle(services)
        self.assertTrue(screen.can_leave())

    # -- the read-back itself failing

    def test_a_read_back_that_fails_does_not_read_as_the_patch_failing(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()

        def gone(host):
            self.scans.append(host)
            raise OSError("the console stopped answering")

        screen._lister = gone
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)

        text = screen._detail.text()
        self.assertIn("Changed in %s: EBOOT.BIN" % BO2_ID, text)
        self.assertIn("could not be read back", text)
        self.assertIn("not the same as the fix having failed", text)
        # The sentence the other failures use would be a lie here: the files
        # on the console were changed a second ago.
        self.assertNotIn("Nothing has been changed", text)
        self.assertTrue(screen.can_leave())

    def test_an_unreachable_console_after_a_patch_says_which_half_worked(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        screen._lister = lambda host: StubLister(
            open_error=OSError("no route to host"))
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)

        text = screen._detail.text()
        self.assertIn("Changed in %s: EBOOT.BIN" % BO2_ID, text)
        self.assertIn("could not be read back", text)
        self.assertIn("no route to host", text)
        self.assertNotIn("did not answer", screen._panel_heading.text())

    # -- the bad case

    def test_a_failed_patch_is_not_rescanned_over_its_own_error(self):
        screen, services = self.scanned_screen()
        failed = _patch_result(
            BO2_ID, error="The console refused the upload halfway through.",
            restored=["EBOOT.BIN"])
        self.finish_patch(screen, failed)
        self.settle(services)

        self.assertEqual(len(self.scans), 1)
        text = screen._detail.text()
        self.assertIn("refused the upload", text)
        self.assertIn("Put back: EBOOT.BIN", text)
        self.assertNotIn("Changed:", text)
        self.assertTrue(screen.can_leave())

    def test_a_run_that_replaced_nothing_is_not_treated_as_a_patch(self):
        # No error, but no file was written either. There is nothing to read
        # back and nothing to restart the console for.
        screen, services = self.scanned_screen()
        self.finish_patch(screen, _patch_result(
            BO2_ID, notes=["Nothing was left to do."]))
        self.settle(services)
        self.assertEqual(len(self.scans), 1)
        self.assertFalse(screen._restart.isVisibleTo(screen))

    # -- the restart notice

    def test_a_confirmed_patch_says_to_restart_the_console(self):
        screen, services = self.scanned_screen()
        self.assertFalse(screen._restart.isVisibleTo(screen))
        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)
        self.assertTrue(screen._restart.isVisibleTo(screen))
        self.assertEqual(screen._restart_text.text(), patcher.RESTART_NOTICE)
        self.assertIn("Restart your PlayStation 3",
                      screen._restart_text.text())
        # In a frame of its own, not a paragraph in the running text.
        self.assertNotIn("Restart your PlayStation 3", screen._detail.text())

    def test_the_restart_notice_survives_the_read_back(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)
        self.assertEqual(len(self.scans), 2)
        self.assertTrue(screen._restart.isVisibleTo(screen))
        self.assertEqual(screen._restart_text.text(), patcher.RESTART_NOTICE)

    def test_a_failed_patch_does_not_tell_anybody_to_restart_anything(self):
        screen, services = self.scanned_screen()
        self.finish_patch(screen, _patch_result(
            BO2_ID, error="Nothing was uploaded."))
        self.settle(services)
        self.assertFalse(screen._restart.isVisibleTo(screen))

    def test_scanning_again_by_hand_clears_the_last_patch_s_messages(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)
        screen.start_scan()
        self.settle(services)
        self.assertFalse(screen._restart.isVisibleTo(screen))
        self.assertNotIn("Changed:", screen._detail.text())

    # -- the success panel, which is the read-back's to give and nobody else's

    def test_neither_panel_appears_before_the_read_back_has_answered(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        # The bytes are written and the confirming scan is still out. All that
        # is known here is what this program tried to do, which is not grounds
        # for a green panel or for sending anybody to restart a console.
        self.assertFalse(screen._success.isVisibleTo(screen))
        self.assertFalse(screen._restart.isVisibleTo(screen))
        self.settle(services)
        self.assertTrue(screen._success.isVisibleTo(screen))
        self.assertTrue(screen._restart.isVisibleTo(screen))

    def test_a_confirmed_patch_puts_the_green_panel_above_the_notice(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)

        self.assertEqual(self.states(screen), ["already fixed"] * 3)
        self.assertTrue(screen._success.isVisibleTo(screen))
        self.assertTrue(screen._restart.isVisibleTo(screen))
        layout = screen.layout()
        self.assertLess(layout.indexOf(screen._success),
                        layout.indexOf(screen._restart))
        self.assertFalse(screen._success_icon.pixmap().isNull())
        # And the one line the user came for is still underneath both.
        self.assertIn("Changed in %s: EBOOT.BIN, t6_ps3f.self, "
                      "t6mp_ps3f.self" % BO2_ID,
                      screen._detail.text())

    def test_a_read_back_that_fails_is_not_dressed_up_as_a_success(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        def gone(host):
            raise OSError("the console stopped answering")

        screen._lister = gone
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)

        self.assertFalse(screen._success.isVisibleTo(screen))
        self.assertFalse(screen._restart.isVisibleTo(screen))
        self.assertIn("could not be read back", screen._detail.text())

    def test_an_unreachable_console_gets_neither_panel(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        screen._lister = lambda host: StubLister(
            open_error=OSError("no route to host"))
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)

        self.assertFalse(screen._success.isVisibleTo(screen))
        self.assertFalse(screen._restart.isVisibleTo(screen))
        self.assertIn("could not be read back", screen._detail.text())

    def test_a_file_still_needing_fixing_is_not_a_success(self):
        # The console is left exactly as it was, so the read-back finds the
        # files unpatched. Whatever the patch believed it did, this is what is
        # on the hard drive, and it is not something to celebrate.
        screen, services = self.scanned_screen()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)

        self.assertEqual(self.states(screen), ["needs fixing"] * 3)
        self.assertFalse(screen._success.isVisibleTo(screen))
        self.assertFalse(screen._restart.isVisibleTo(screen))

    def test_a_file_that_comes_back_unrecognised_is_not_a_success(self):
        screen, services = self.scanned_screen()
        for name in self.BO2_FILES:
            self.server.files[self.remote(name)] = self_file(
                "bo2", name, "neither")
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)

        self.assertEqual(self.states(screen), ["not recognised"] * 3)
        self.assertFalse(screen._success.isVisibleTo(screen))
        self.assertFalse(screen._restart.isVisibleTo(screen))

    def test_a_file_that_was_never_examined_is_not_a_success(self):
        # Nothing was read out of these files at all, which is a fault in this
        # program. It is the one case where the console may well be perfect,
        # and it is still not something this screen knows.
        screen, services = self.scanned_screen()
        self.patch_the_console()
        with _NoPatchers():
            self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
            self.settle(services)

        self.assertEqual(self.states(screen), ["not checked"] * 3)
        self.assertFalse(screen._success.isVisibleTo(screen))
        self.assertFalse(screen._restart.isVisibleTo(screen))

    def test_a_failed_patch_never_reaches_the_success_panel(self):
        screen, services = self.scanned_screen()
        self.finish_patch(screen, _patch_result(
            BO2_ID, error="The console refused the upload halfway through."))
        self.settle(services)
        self.assertFalse(screen._success.isVisibleTo(screen))
        self.assertFalse(screen._restart.isVisibleTo(screen))

    def test_scanning_again_by_hand_takes_the_success_panel_away(self):
        screen, services = self.scanned_screen()
        self.patch_the_console()
        self.finish_patch(screen, _patch_result(BO2_ID, self.BO2_FILES))
        self.settle(services)
        self.assertTrue(screen._success.isVisibleTo(screen))
        screen.start_scan()
        self.settle(services)
        self.assertFalse(screen._success.isVisibleTo(screen))
        self.assertFalse(screen._restart.isVisibleTo(screen))

    def test_the_success_panel_takes_its_green_from_the_theme(self):
        connection = ConnectionState("127.0.0.1")
        services = Services(connection, _ColourfulTheme())
        screen = patcher.BlackOpsTwoPatcher(services)
        self.addCleanup(screen.deleteLater)
        screen._show_success()
        style = (screen._success.styleSheet()
                 + screen._success_heading.styleSheet()
                 + screen._success_body.styleSheet())
        self.assertIn(_ColourfulTheme.COLOURS["ok"], style)
        # The object-name selector is the only other "#" allowed in here.
        style = style.replace("QFrame#successpanel", "")
        for value in set(_ColourfulTheme.COLOURS.values()):
            style = style.replace(value, "")
        self.assertNotIn("#", style)
        self.assertFalse(screen._success_icon.pixmap().isNull())

    def test_the_restart_notice_takes_its_colour_from_the_theme(self):
        connection = ConnectionState("127.0.0.1")
        services = Services(connection, _ColourfulTheme())
        screen = patcher.BlackOpsTwoPatcher(services)
        self.addCleanup(screen.deleteLater)
        screen._show_restart_notice()
        style = (screen._restart.styleSheet()
                 + screen._restart_text.styleSheet())
        self.assertIn(_ColourfulTheme.COLOURS["warn"], style)
        # The object-name selector is the only other "#" allowed in here.
        style = style.replace("QFrame#restartnotice", "")
        for value in set(_ColourfulTheme.COLOURS.values()):
            style = style.replace(value, "")
        self.assertNotIn("#", style)


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class TheProgressReadout(ScreenCase):
    """The shell draws a progress bar three pixels tall.

    Anything painted on the bar is clipped away, so "3.1 MB of 6.9 MB" was
    reported to a place the user could not see it and the transfer looked like
    an unlabelled line creeping across the screen.
    """

    def screen(self):
        screen, _services = self.build(patcher.BlackOpsTwoPatcher)
        return screen

    def test_nothing_is_painted_on_the_bar_itself(self):
        screen = self.screen()
        self.assertFalse(screen._bar.isTextVisible())

    def test_the_size_of_a_transfer_is_readable_beside_the_bar(self):
        screen = self.screen()
        screen._set_busy(True, "Copying EBOOT.BIN")
        screen._on_progress({"stage": "backup", "message": "Copying EBOOT.BIN",
                             "bytes": int(3.1 * 1024 * 1024),
                             "of": int(6.9 * 1024 * 1024)})
        self.assertIn("3.1 MB of 6.9 MB", screen._count.text())
        self.assertTrue(screen._count.isVisibleTo(screen))
        self.assertNotIn("MB", screen._bar.format())
        self.assertEqual(screen._bar.value(), 44)

    def test_the_file_count_is_readable_too(self):
        screen = self.screen()
        screen._set_busy(True, "Building")
        screen._on_progress({"stage": "build", "message": "Building",
                             "done": 2, "total": 3})
        self.assertEqual(screen._count.text(), "2 of 3")
        self.assertTrue(screen._count.isVisibleTo(screen))
        self.assertNotIn("3", screen._bar.format())

    def test_the_readout_goes_away_when_the_work_does(self):
        screen = self.screen()
        screen._set_busy(True, "Copying")
        screen._on_progress({"message": "Copying", "done": 1, "total": 3})
        screen._set_busy(False)
        self.assertEqual(screen._count.text(), "")
        self.assertFalse(screen._count.isVisibleTo(screen))


# --- a release nobody has confirmed ----------------------------------------

@unittest.skipIf(QApplication is None, "PySide6 is not available")
class AReleaseNobodyHasConfirmed(ScreenCase):
    """What the screen says about a SKU the fix has never been proved on.

    The old table refused these outright. Now they are attempted, and the two
    outcomes have to read completely differently: one is a fix about to be
    applied with an honest caveat under it, the other is this tool admitting it
    cannot do anything with this release. Neither may read as the user having
    done something wrong, and neither may read as the game not being there.
    """

    TITLE_ID = "BLES01430"
    CONTENT = "EP0002-BLES01430_00-MW3P000000000124"

    def multiplayer(self, klicensee=None):
        record = next(item for item in titles.MW3_BINARIES
                      if item["name"] == "default_mp.self")
        return images.wrap(
            images.info_for(self.CONTENT, "USPRX", "default_mp.self",
                            key_revision="0019",
                            fw_version="0004000000000000"),
            record["klicensee"] if klicensee is None else klicensee,
            images.mw3_image("stock"))

    def screen_for(self, files):
        self.start(self.TITLE_ID, files)
        screen, services = self.build(patcher.ModernWarfareThreePatcher)
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = type("stub", (), {"find_installations": staticmethod(
            detector_for(installation("ready", self.TITLE_ID, "mw3")))})
        screen.on_enter()
        self.settle(services)
        return screen

    def states(self, screen):
        return [screen._files.topLevelItem(row).text(3)
                for row in range(screen._files.topLevelItemCount())]

    def test_files_that_open_are_offered_the_fix_with_the_caveat_said(self):
        screen = self.screen_for({"default_mp.self": self.multiplayer()})
        self.assertEqual(self.states(screen)[0], "needs fixing")
        self.assertTrue(screen._patch.isEnabled())
        words = screen._detail.text()
        self.assertIn(self.TITLE_ID, words)
        self.assertIn("nobody has confirmed", words)
        self.assertEqual(self.server.written, {})

    def test_files_that_will_not_open_are_not_blamed_on_the_user(self):
        record = next(item for item in titles.MW3_BINARIES
                      if item["name"] == "default_mp.self")
        screen = self.screen_for(
            {"default_mp.self": self.multiplayer(record["klicensee"][::-1])})
        self.assertEqual(self.states(screen)[0], "cannot be opened")
        self.assertFalse(screen._patch.isEnabled())
        words = screen._detail.text()
        self.assertIn(self.TITLE_ID, words)
        self.assertIn("Nothing is wrong with your console", words)
        # It is a limit of this tool. It is not the file being wrong, not a
        # piece of this program being absent, and not the game being missing.
        self.assertNotIn("not recognised", words)
        self.assertNotIn("fault in this program", words)
        self.assertNotIn("not installed", words)
        self.assertEqual(self.server.written, {})


# --- a release nobody has tested -------------------------------------------


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class AReleaseNobodyHasTested(ScreenCase):
    """A folder whose title ID this tool has never written down.

    These were refused outright, in words saying the file would not even be
    read. The fix finds its own patch site by the code around it and every
    file is read and checked before a byte goes back, so the refusal was
    costing people a fix their game would have taken.
    """

    def screen_for_untested(self):
        self.start(MW3_ID, {"default_mp.self": self_file("mw3",
                                                         "default_mp.self"),
                            "default.self": self_file("mw3", "default.self")})
        screen, services = self.build(patcher.ModernWarfareThreePatcher)
        found = installation("ready", MW3_ID, "mw3")
        # What detect hands over for a folder whose files name one game and
        # whose title ID is in no table.
        found.untested = True
        found.verified = False
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = type("stub", (), {"find_installations": staticmethod(
            detector_for(found))})
        screen.on_enter()
        self.settle(services)
        return screen

    def notice(self, screen):
        return " ".join([screen._untested_heading.text(),
                         screen._untested_body.text()])

    def test_an_untested_release_reads_as_a_warning_not_a_refusal(self):
        screen = self.screen_for_untested()
        self.assertTrue(screen._untested_notice.isVisibleTo(screen))
        words = self.notice(screen)
        self.assertIn("has not been tested", words)
        self.assertIn("checked before anything is written", words)
        self.assertIn("finds its own patch site", words)
        self.assertIn("Desktop", words)
        # The refusal that is left is the one the files themselves decide.
        self.assertIn("If the patch site is not in these files", words)
        for forbidden in ("will not start", "nothing will be read",
                          "This is a refusal"):
            self.assertNotIn(forbidden, words)

    def test_the_fix_is_still_offered_on_a_release_nobody_has_tested(self):
        screen = self.screen_for_untested()
        self.assertEqual(screen._files.topLevelItemCount(), 2)
        self.assertTrue(screen._patch.isEnabled())
        self.assertEqual(self.server.written, {})

    def test_the_release_is_called_untested_where_the_screen_names_it(self):
        # The panel is read once on the way in. This line sits beside the
        # button and names the folder about to be written to.
        screen = self.screen_for_untested()
        self.assertIn(MW3_ID, screen._where.text())
        self.assertIn("This release has not been tested.",
                      screen._where.text())

    def test_the_last_box_before_writing_says_the_release_is_untested(self):
        screen = self.screen_for_untested()
        said = []

        def capture(parent, title, text, *rest):
            said.append(text)
            return patcher.QMessageBox.No

        with mock.patch.object(patcher.QMessageBox, "question", capture):
            screen._on_patch()
        self.assertEqual(len(said), 1)
        self.assertIn("This release has not been tested.", said[0])
        self.assertIn("the patch site was found", said[0])
        self.assertEqual(self.server.written, {})

    def test_the_same_point_is_not_made_twice_under_the_table(self):
        # The panel above the table already says this release is untested,
        # and the caveat for an unverified release says it again in other
        # words, which reads as two separate problems.
        screen = self.screen_for_untested()
        self.assertNotIn("nobody has confirmed", screen._detail.text())


# --- being told where the game is ------------------------------------------


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class TypingWhereTheGameIs(ScreenCase):
    """The search found no folder for this game and the user knows better.

    The screen stopped at "not installed", and a user reading that went
    looking for the reason their console would not show the game and deleted
    game data over it. The way out offered here is the user saying where the
    game is, and nothing on this screen asks anybody to remove anything.
    """

    FILES = ("default_mp.self", "default.self")

    def screen_with_nothing_found(self, screen_class=None):
        self.start(MW3_ID, {name: self_file("mw3", name)
                            for name in self.FILES})
        screen, services = self.build(
            screen_class or patcher.ModernWarfareThreePatcher)
        # The game is in /dev_hdd0/game and the search does not come back with
        # it, which is the console this way out exists for.
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = type("stub", (), {"find_installations":
                                           staticmethod(detector_for())})
        screen.on_enter()
        self.settle(services)
        return screen, services

    def words(self, screen):
        return " ".join([screen._panel_heading.text(),
                         screen._panel_body.text(),
                         screen._panel_reason.text()])

    def answer(self, screen, path):
        """Stand in for the box that asks for the folder."""
        screen.ask_for_folder = lambda: path

    def test_a_game_that_was_not_found_offers_to_be_told_where_it_is(self):
        screen, _services = self.screen_with_nothing_found()
        self.assertIn("not installed", screen._panel_heading.text())
        self.assertTrue(screen._folder_button.isVisibleTo(screen))
        self.assertIn("the button below takes the folder it is in",
                      screen._panel_body.text())

    def test_nothing_here_suggests_removing_a_game_to_make_a_scan_work(self):
        screen, _services = self.screen_with_nothing_found()
        words = (self.words(screen) + " " + screen._detail.text()).lower()
        for forbidden in ("delete", "remove", "uninstall"):
            self.assertNotIn(forbidden, words)

    def test_the_folder_the_user_types_is_read_and_called_untested(self):
        screen, services = self.screen_with_nothing_found()
        self.answer(screen, f"/dev_hdd0/game/{MW3_ID}")
        screen._folder_button.click()
        self.settle(services)
        self.assertEqual(screen._files.topLevelItemCount(), len(self.FILES))
        self.assertTrue(screen._patch.isEnabled())
        self.assertTrue(screen._untested_notice.isVisibleTo(screen))
        self.assertIn("This release has not been tested.",
                      screen._where.text())
        self.assertEqual(self.server.written, {})

    def test_declining_to_type_one_reads_nothing(self):
        screen, services = self.screen_with_nothing_found()
        self.answer(screen, "")
        screen._folder_button.click()
        self.settle(services)
        self.assertIsNone(screen._scan)
        self.assertIn("not installed", screen._panel_heading.text())

    def test_a_path_outside_the_game_folder_is_answered_before_any_read(self):
        screen, services = self.screen_with_nothing_found()
        self.answer(screen, "C:/Games/BLES01428")
        screen._folder_button.click()
        self.settle(services)
        self.assertIsNone(screen._scan)
        self.assertIn("does not look like a folder",
                      screen._panel_heading.text())
        self.assertIn("/dev_hdd0/game", screen._panel_body.text())
        self.assertTrue(screen._folder_button.isVisibleTo(screen))

    def test_a_folder_that_is_not_on_the_console_says_so_and_asks_again(self):
        screen, services = self.screen_with_nothing_found()
        self.answer(screen, "/dev_hdd0/game/BLES09999")
        screen._folder_button.click()
        self.settle(services)
        self.assertIsNone(screen._scan)
        self.assertEqual(screen._panel_token, "warn")
        self.assertIn("not on the console", screen._panel_heading.text())
        # Quoted back as it was typed, so a slip is visible.
        self.assertIn("/dev_hdd0/game/BLES09999", screen._panel_body.text())
        self.assertTrue(screen._folder_button.isVisibleTo(screen))
        self.assertEqual(self.server.written, {})

    def test_the_path_may_name_the_folder_or_its_usrdir(self):
        self.assertEqual(patcher._folder_title_id(f"/dev_hdd0/game/{MW3_ID}"),
                         MW3_ID)
        self.assertEqual(
            patcher._folder_title_id(f"/dev_hdd0/game/{MW3_ID}/USRDIR/"),
            MW3_ID)
        self.assertEqual(patcher._folder_title_id("/dev_hdd0/GAME/bles01428"),
                         MW3_ID)
        # A title ID on its own says nothing about where the folder is.
        self.assertEqual(patcher._folder_title_id(MW3_ID), "")
        self.assertEqual(patcher._folder_title_id(""), "")

    def test_all_three_screens_take_a_folder_the_user_types(self):
        # One seam on the shared class rather than a special case on the one
        # screen somebody reported it from.
        for screen_class in (patcher.BlackOpsOnePatcher,
                             patcher.BlackOpsTwoPatcher,
                             patcher.ModernWarfareThreePatcher):
            self.assertIs(screen_class.ask_for_folder,
                          patcher.PatcherScreen.ask_for_folder)
            screen, _services = self.screen_with_nothing_found(screen_class)
            self.assertTrue(screen._folder_button.isVisibleTo(screen))


# --- the backup manifest ---------------------------------------------------
#
# A backup that cannot be verified is not a backup. Everything below turns on
# that: the sizes and hashes were known and checked against the console at the
# moment the copy was taken, and if they are not written down beside the files
# then nothing afterwards can answer the only question that matters about a
# folder of originals, which is whether it is still exactly what came off the
# console.

def backup_on_disk(root, title_id, files, when=None, folder_name=None,
                   manifest=True, title_in_manifest=None):
    """A backup folder exactly as make() leaves one, without a console.

    Written by hand rather than by taking one, so that a test can age it, break
    it, or leave the record out of it.
    """
    when = when or datetime.datetime(2026, 9, 1, 12, 0, 0)
    folder = os.path.join(root, backups.FOLDER_NAME,
                          folder_name or f"{title_id} {when:%Y-%m-%d}")
    os.makedirs(folder, exist_ok=True)
    usrdir = titles.usrdir_for(title_id)
    saved = backups.Backup(folder, title_in_manifest or title_id, usrdir, when)
    for name, body in files.items():
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(body)
        saved.entries.append({"name": name, "path": path,
                              "remote": f"{usrdir}/{name}",
                              "size": len(body),
                              "sha1": hashlib.sha1(body).hexdigest()})
    if manifest:
        backups.write_manifest(saved)
    return folder


class TheBackupManifest(ConsoleCase):
    def setUp(self):
        self.files = {"default_mp.self": self_file("mw3", "default_mp.self")}
        self.start(MW3_ID, self.files)

    def take_one(self):
        folder = os.path.join(self.workspace(), "backup")
        return backups.make(self.writer(), titles.usrdir_for(MW3_ID),
                            ["default_mp.self"], folder, title_id=MW3_ID)

    def test_taking_a_backup_writes_down_what_it_contains(self):
        saved = self.take_one()
        read = backups.read_manifest(saved.folder)
        self.assertEqual(read.problem, "")
        self.assertEqual(read.title_id, MW3_ID)
        self.assertEqual(read.names, ["default_mp.self"])
        entry = read.entry_for("default_mp.self")
        self.assertEqual(entry["sha1"],
                         hashlib.sha1(self.files["default_mp.self"]).hexdigest())
        self.assertEqual(entry["size"], len(self.files["default_mp.self"]))
        self.assertEqual(entry["remote"],
                         f"{titles.usrdir_for(MW3_ID)}/default_mp.self")
        self.assertIsNotNone(read.taken)

    def test_the_title_it_came_from_is_recorded_even_without_being_told(self):
        # The usrdir carries it, and a manifest without it could not refuse a
        # restore onto the wrong game.
        folder = os.path.join(self.workspace(), "backup")
        saved = backups.make(self.writer(), titles.usrdir_for(MW3_ID),
                             ["default_mp.self"], folder)
        self.assertEqual(backups.read_manifest(saved.folder).title_id, MW3_ID)

    def test_an_untouched_backup_checks_out(self):
        read = backups.read_manifest(self.take_one().folder)
        results = backups.verify(read)
        self.assertTrue(backups.all_good(results))
        self.assertEqual(results[0]["name"], "default_mp.self")
        self.assertIsNotNone(results[0]["modified"])
        self.assertEqual(results[0]["size"],
                         len(self.files["default_mp.self"]))

    def test_a_file_changed_since_it_was_copied_stops_checking_out(self):
        read = backups.read_manifest(self.take_one().folder)
        path = read.entry_for("default_mp.self")["path"]
        with open(path, "r+b") as handle:
            handle.seek(0x40)
            handle.write(b"\x00\x00\x00\x00")
        results = backups.verify(read)
        self.assertFalse(backups.all_good(results))
        self.assertIn("changed it", results[0]["reason"])

    def test_a_file_that_has_gone_missing_is_said_to_be_missing(self):
        read = backups.read_manifest(self.take_one().folder)
        os.remove(read.entry_for("default_mp.self")["path"])
        results = backups.verify(read)
        self.assertFalse(backups.all_good(results))
        self.assertIn("not in the backup folder", results[0]["reason"])

    def test_a_truncated_file_is_caught_on_its_size_alone(self):
        read = backups.read_manifest(self.take_one().folder)
        path = read.entry_for("default_mp.self")["path"]
        with open(path, "r+b") as handle:
            handle.truncate(16)
        results = backups.verify(read)
        self.assertFalse(backups.all_good(results))
        self.assertIn("bytes", results[0]["reason"])

    def test_a_folder_with_no_record_says_so_rather_than_looking_usable(self):
        folder = self.take_one().folder
        os.remove(backups.manifest_path(folder))
        read = backups.read_manifest(folder)
        self.assertIn("no record", read.problem)
        self.assertEqual(read.entries, [])

    def test_a_record_that_will_not_parse_is_not_read_as_an_empty_backup(self):
        folder = self.take_one().folder
        with open(backups.manifest_path(folder), "w", encoding="utf-8") as out:
            out.write("{not json at all")
        read = backups.read_manifest(folder)
        self.assertTrue(read.problem)
        self.assertEqual(read.entries, [])


class FindingABackup(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ps3tools-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.body = b"the original file" * 8

    def make(self, title_id, when, folder_name=None, **extra):
        return backup_on_disk(self.root, title_id, {"EBOOT.BIN": self.body},
                              when=when, folder_name=folder_name, **extra)

    def test_a_desktop_with_no_backup_folder_is_not_an_error(self):
        empty = os.path.join(self.root, "nothing-here")
        self.assertEqual(backups.find(BO2_ID, root=empty), [])
        self.assertFalse(os.path.isdir(backups.root_folder(empty)))

    def test_the_most_recent_comes_first(self):
        self.make(BO2_ID, datetime.datetime(2026, 1, 1, 9, 0),
                  folder_name="BLES01717 2026-01-01")
        self.make(BO2_ID, datetime.datetime(2026, 8, 30, 9, 0),
                  folder_name="BLES01717 2026-08-30")
        self.make(BO2_ID, datetime.datetime(2026, 4, 4, 9, 0),
                  folder_name="BLES01717 2026-04-04")
        found = backups.find(BO2_ID, root=self.root)
        self.assertEqual([item.taken.date().isoformat() for item in found],
                         ["2026-08-30", "2026-04-04", "2026-01-01"])

    def test_only_this_title_is_offered(self):
        self.make(BO2_ID, datetime.datetime(2026, 1, 1, 9, 0))
        self.make(MW3_ID, datetime.datetime(2026, 2, 2, 9, 0))
        found = backups.find(BO2_ID, root=self.root)
        self.assertEqual([item.title_id for item in found], [BO2_ID])

    def test_a_renamed_folder_is_matched_on_what_is_inside_it(self):
        # The folder name is the one part of a backup nothing ever verified.
        self.make(BO2_ID, datetime.datetime(2026, 1, 1, 9, 0),
                  folder_name="my black ops backup")
        found = backups.find(BO2_ID, root=self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].title_id, BO2_ID)

    def test_a_folder_with_no_record_is_still_listed_and_still_unusable(self):
        # Silence about a folder sitting on the Desktop looking like a backup
        # is worse than a sentence saying why it cannot be used.
        self.make(BO2_ID, datetime.datetime(2026, 1, 1, 9, 0), manifest=False)
        found = backups.find(BO2_ID, root=self.root)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].problem)


# --- putting the originals back --------------------------------------------

class PuttingTheOriginalsBack(ConsoleCase):
    """flow.restore. The undo half, and the one people reach for in a panic."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ps3tools-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.stock = {"default_mp.self": self_file("mw3", "default_mp.self"),
                      "default.self": self_file("mw3", "default.self")}
        self.patched = dict(self.stock)
        self.patched["default_mp.self"] = self_file("mw3", "default_mp.self",
                                                    "patched")

    def console_and_backup(self, title_id=MW3_ID, saved=None, **extra):
        self.start(title_id, dict(self.patched))
        folder = backup_on_disk(
            self.root, title_id,
            saved if saved is not None
            else {"default_mp.self": self.stock["default_mp.self"]},
            **extra)
        return backups.read_manifest(folder)

    def test_the_originals_go_back_and_are_confirmed_off_the_console(self):
        backup = self.console_and_backup()
        result = flow.restore(self.writer(), backup, MW3_ID)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.restored, ["default_mp.self"])
        self.assertEqual(self.server.files[self.remote("default_mp.self")],
                         self.stock["default_mp.self"])

    def test_it_writes_where_the_console_has_this_game(self):
        backup = self.console_and_backup()
        flow.restore(self.writer(), backup, MW3_ID)
        self.assertIn(self.remote("default_mp.self"), self.server.written)

    def test_a_backup_of_another_game_is_refused_and_says_why(self):
        backup = self.console_and_backup(title_in_manifest=BO2_ID)
        result = flow.restore(self.writer(), backup, MW3_ID)
        self.assertFalse(result.ok)
        self.assertTrue(result.refused)
        self.assertIn(BO2_ID, result.error)
        self.assertIn(MW3_ID, result.error)
        self.assertIn("not that game's files", result.error)
        self.assertEqual(self.server.written, {})

    def test_a_backup_whose_files_have_changed_is_refused(self):
        backup = self.console_and_backup()
        path = backup.entry_for("default_mp.self")["path"]
        with open(path, "r+b") as handle:
            handle.seek(0x40)
            handle.write(b"\xff\xff\xff\xff")
        result = flow.restore(self.writer(), backup, MW3_ID)
        self.assertFalse(result.ok)
        self.assertTrue(result.refused)
        self.assertIn("default_mp.self", result.error)
        self.assertIn("not the original", result.error)
        self.assertEqual(self.server.written, {})

    def test_a_backup_with_no_record_of_itself_is_refused(self):
        backup = self.console_and_backup(manifest=False)
        result = flow.restore(self.writer(), backup, MW3_ID)
        self.assertFalse(result.ok)
        self.assertIn("no record", result.error)
        self.assertEqual(self.server.written, {})

    def test_no_refusal_ever_reaches_the_console(self):
        """The seam, proved by taking it away.

        Every refusal is decided from the local disk, so a write client that
        explodes on touch must never be touched at all. This is the guard
        against a future change quietly moving one of these checks to after
        the first command.
        """
        class Exploding:
            def __getattr__(self, name):
                raise AssertionError(
                    f"a refused restore reached the console: {name}")

        backup = self.console_and_backup(title_in_manifest=BO2_ID)
        result = flow.restore(Exploding(), backup, MW3_ID)
        self.assertTrue(result.refused)
        self.assertEqual(self.server.written, {})

    def test_a_console_that_disappears_partway_says_where_that_leaves_you(self):
        backup = self.console_and_backup(saved=dict(self.stock))
        self.server.fault = lambda verb, argument: (
            "DROP" if verb == "STOR" else None)
        result = flow.restore(self.writer(), backup, MW3_ID)
        self.assertFalse(result.ok)
        self.assertFalse(result.refused)
        self.assertIn("could not be written to the console", result.error)
        # The two sentences that stop this being a dead end.
        self.assertIn("backup has not been touched", result.error)
        self.assertIn("safe", result.error)

    def test_a_file_that_does_not_land_whole_is_caught_on_the_read_back(self):
        backup = self.console_and_backup()
        holder = self

        class Corrupting(FtpWriter):
            """226, and something else on the disk. What a console out of room
            actually does."""

            def store(self, source, path, on_block=None):
                sent = super().store(source, path, on_block)
                holder.server.files[path] = \
                    holder.server.files[path][:-4]
                return sent

        result = flow.restore(self.writer(cls=Corrupting), backup, MW3_ID)
        self.assertFalse(result.ok)
        self.assertIn("other than what was sent", result.error)
        self.assertEqual(result.restored, [])
        self.assertEqual(result.failed[0][0], "default_mp.self")

    def test_a_console_that_will_not_be_read_back_is_not_called_a_success(self):
        backup = self.console_and_backup()
        self.server.fault = lambda verb, argument: (
            "550 gone" if verb == "RETR" else None)
        result = flow.restore(self.writer(), backup, MW3_ID)
        self.assertFalse(result.ok)
        self.assertIn("could not be read back", result.error)
        self.assertEqual(result.restored, [])

    def test_progress_says_which_file_is_going_back(self):
        backup = self.console_and_backup()
        seen = []
        flow.restore(self.writer(), backup, MW3_ID, progress=seen.append)
        stages = {event["stage"] for event in seen}
        self.assertIn("restore", stages)
        self.assertIn("done", stages)
        self.assertTrue(any("default_mp.self" in event["message"]
                            for event in seen))


# --- the restore, from the screen ------------------------------------------

@unittest.skipIf(QApplication is None, "PySide6 is not available")
class RestoringFromTheScreen(ScreenCase):
    """The button next to Apply, and everything it refuses to do.

    The README called the backups the only way back and the only way back was a
    hand-typed FTP session, which is not something the person this tool was
    written for can do. These are the cases they will actually meet.
    """

    BO2_FILES = ("EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self")

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ps3tools-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.stock = {name: self_file("bo2", name) for name in self.BO2_FILES}

    def scanned_screen(self, state="patched", title_id=BO2_ID,
                       tu_version="1.19"):
        self.start(title_id, {name: self_file("bo2", name, state)
                              for name in self.BO2_FILES})
        screen, services = self.build(patcher.BlackOpsTwoPatcher)
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        patcher.detect = type("stub", (), {"find_installations": staticmethod(
            detector_for(installation("ready", title_id, "bo2",
                                      tu_version=tu_version)))})
        screen._backup_root = self.root
        screen._confirm_restore = lambda *args: True
        screen.on_enter()
        self.settle(services)
        return screen, services

    def settle_twice(self, services):
        """A restore, then the read-back it starts. Two turns of the loop."""
        for _ in range(3):
            self.settle(services)

    def states(self, screen):
        return [screen._files.topLevelItem(row).text(3)
                for row in range(screen._files.topLevelItemCount())]

    # -- nothing to put back

    def test_with_no_backup_folder_it_says_there_is_nothing_to_put_back(self):
        screen, services = self.scanned_screen()
        screen._on_restore()
        self.settle(services)
        self.assertIn("no backups on this computer yet", screen._detail.text())
        self.assertIn(backups.FOLDER_NAME, screen._detail.text())
        self.assertEqual(self.server.written, {})

    def test_with_no_backup_of_this_game_it_says_which_game_it_looked_for(self):
        backup_on_disk(self.root, MW3_ID,
                       {"default_mp.self": self_file("mw3",
                                                     "default_mp.self")})
        screen, services = self.scanned_screen()
        screen._on_restore()
        self.settle(services)
        self.assertIn(f"no backup of {BO2_ID}", screen._detail.text())
        self.assertEqual(self.server.written, {})

    # -- refusals

    def test_one_file_that_does_not_check_out_leaves_the_rest_restorable(self):
        # The file that changed is never sent. The other two are still the
        # other two, and putting them back is two fewer patched files on the
        # console than refusing the lot.
        folder = backup_on_disk(self.root, BO2_ID, self.stock)
        with open(os.path.join(folder, "EBOOT.BIN"), "r+b") as handle:
            handle.seek(0x40)
            handle.write(b"\xff\xff\xff\xff")
        screen, services = self.scanned_screen()
        screen._on_restore()
        self.settle(services)
        self.assertIn("EBOOT.BIN", screen._detail.text())
        self.assertIn("not the original", screen._detail.text())
        written = [path.rsplit("/", 1)[-1] for path in self.server.written]
        self.assertNotIn("EBOOT.BIN", written)
        self.assertEqual(sorted(written), ["t6_ps3f.self", "t6mp_ps3f.self"])

    def test_the_button_is_dead_when_there_is_nothing_to_put_back(self):
        # It was live on a console with no title update installed and nothing
        # backed up, which is a button that can only produce a refusal.
        screen, _services = self.scanned_screen()
        self.assertFalse(screen._restore.isEnabled())
        self.assertIn("no backup", screen._restore.toolTip())

    def test_the_button_is_live_once_there_is_a_backup_of_this_game(self):
        backup_on_disk(self.root, BO2_ID, self.stock)
        screen, _services = self.scanned_screen()
        self.assertTrue(screen._restore.isEnabled())
        self.assertEqual(screen._restore.toolTip(), "")

    def test_a_backup_of_another_game_is_refused_on_screen(self):
        backup_on_disk(self.root, BO2_ID, self.stock,
                       title_in_manifest=MW3_ID)
        screen, services = self.scanned_screen()
        screen._on_restore()
        self.settle(services)
        self.assertIn("not that game's files", screen._detail.text())
        self.assertEqual(self.server.written, {})

    def test_a_refused_restore_never_asks_the_user_to_confirm_anything(self):
        backup_on_disk(self.root, BO2_ID, self.stock,
                       title_in_manifest=MW3_ID)
        screen, services = self.scanned_screen()
        asked = []
        screen._confirm_restore = lambda *args: asked.append(args) or True
        screen._on_restore()
        self.settle(services)
        self.assertEqual(asked, [])

    def test_what_is_in_the_backup_is_shown_before_anything_is_sent(self):
        """Names, sizes, dates and whether each file still checks out.

        A user restoring in a panic is being asked to overwrite the game they
        have; they are entitled to see what is about to overwrite it.
        """
        backup_on_disk(self.root, BO2_ID, self.stock)
        screen, services = self.scanned_screen()
        shown = []

        def refuse(chosen, checks):
            shown.append((chosen, screen._backup_contents(checks)))
            return False

        screen._confirm_restore = refuse
        screen._on_restore()
        self.settle(services)
        chosen, text = shown[0]
        for name in self.BO2_FILES:
            self.assertIn(name, text)
        self.assertEqual(text.count("checked and unchanged"), 3)
        self.assertIn("2026", text)
        self.assertRegex(text, r"\d+(\.\d+)? (B|KB|MB|GB)")
        self.assertEqual(chosen.title_id, BO2_ID)
        # Said no, so nothing happened.
        self.assertEqual(self.server.written, {})
        self.assertTrue(screen._restart.isHidden())

    # -- the good case

    def test_putting_them_back_reads_the_console_and_says_to_restart_it(self):
        backup_on_disk(self.root, BO2_ID, self.stock)
        screen, services = self.scanned_screen()
        self.assertEqual(self.states(screen), ["already fixed"] * 3)

        screen._on_restore()
        self.settle_twice(services)

        for name in self.BO2_FILES:
            self.assertEqual(self.server.files[self.remote(name)],
                             self.stock[name])
        # Read off the console afterwards, not worked out from what was sent.
        self.assertEqual(self.states(screen), ["needs fixing"] * 3)
        self.assertIn("Put back", screen._detail.text())
        self.assertFalse(screen._restart.isHidden())
        self.assertIn("Restart your PlayStation 3", screen._restart_text.text())
        # It has to be plain that this can be undone in turn.
        self.assertIn("apply the fix again", screen._detail.text())
        self.assertTrue(screen._patch.isEnabled())

    def test_the_success_panel_is_not_shown_for_a_restore(self):
        # The green panel says the fix is on the console. It is not.
        backup_on_disk(self.root, BO2_ID, self.stock)
        screen, services = self.scanned_screen()
        screen._on_restore()
        self.settle_twice(services)
        self.assertTrue(screen._success.isHidden())

    def test_the_newest_backup_is_the_one_offered(self):
        backup_on_disk(self.root, BO2_ID, self.stock,
                       when=datetime.datetime(2026, 1, 1, 9, 0),
                       folder_name="BLES01717 2026-01-01")
        backup_on_disk(self.root, BO2_ID, self.stock,
                       when=datetime.datetime(2026, 9, 1, 9, 0),
                       folder_name="BLES01717 2026-09-01")
        screen, _services = self.scanned_screen()
        offered = screen._backups(BO2_ID)
        self.assertEqual(offered[0].taken.date(), datetime.date(2026, 9, 1))
        self.assertEqual(len(offered), 2)

    def test_the_screen_cannot_be_left_while_files_are_going_back(self):
        screen, _services = self.scanned_screen()
        screen._writing = True
        self.assertFalse(screen.can_leave())

    # -- the seam

    def test_the_restore_only_ever_goes_through_the_write_client_seam(self):
        """Replace the client with one that raises, and nothing reaches a wire.

        The guard against the regression this suite already had once: a path
        that was inert in tests became live and the suite started talking to
        the console on the network.
        """
        backup_on_disk(self.root, BO2_ID, self.stock)
        screen, services = self.scanned_screen()

        def refuse(host):
            raise AssertionError("the real write client was used")

        screen._writer = refuse
        screen._on_restore()
        self.settle_twice(services)
        self.assertIn("the real write client was used", screen._detail.text())
        self.assertIn("backup folder has not been changed",
                      screen._detail.text())
        self.assertEqual(self.server.written, {})
        self.assertTrue(screen._restart.isHidden())

    def test_a_console_that_goes_away_mid_restore_says_to_try_again(self):
        backup_on_disk(self.root, BO2_ID, self.stock)
        screen, services = self.scanned_screen()
        self.server.fault = lambda verb, argument: (
            "DROP" if verb == "STOR" else None)
        screen._on_restore()
        self.settle_twice(services)
        self.assertIn("could not be written to the console",
                      screen._detail.text())
        self.assertIn("safe", screen._detail.text())
        self.assertTrue(screen._restart.isHidden())


# --- the title update the fix was verified against -------------------------

class TheVerifiedUpdateCheck(unittest.TestCase):
    """flow.update_check. Verified, never latest, and never a guess."""

    def test_the_verified_version_is_not_read_off_the_manifest(self):
        # 1.19 and 1.24 happen to be the newest today. The check that protects
        # the user is "is this the build the offset was confirmed on", and it
        # must not become "is this what Sony serves", which changes without
        # anybody here noticing.
        self.assertEqual(titles.verified_update_for(BO2_ID), "1.19")
        self.assertEqual(titles.verified_update_for(MW3_ID), "1.24")
        for key in ("bo2", "mw3"):
            self.assertIn("verified_update", titles.TITLES[key])

    def test_the_installed_version_matching_is_the_ordinary_case(self):
        found = flow.update_check(BO2_ID, "1.19")
        self.assertEqual(found.verdict, flow.UPDATE_MATCHES)
        self.assertFalse(found.blocks)

    def test_the_console_spelling_and_a_person_s_are_the_same_version(self):
        # The SFO stores 01.19 and a person writes 1.19.
        self.assertEqual(flow.update_check(BO2_ID, "01.19").verdict,
                         flow.UPDATE_MATCHES)

    def test_another_version_blocks_and_keeps_both_numbers(self):
        found = flow.update_check(BO2_ID, "1.09")
        self.assertEqual(found.verdict, flow.UPDATE_DIFFERS)
        self.assertTrue(found.blocks)
        self.assertEqual((found.installed, found.verified), ("1.09", "1.19"))

    def test_a_version_that_could_not_be_read_is_its_own_answer(self):
        found = flow.update_check(BO2_ID, None)
        self.assertEqual(found.verdict, flow.UPDATE_UNREADABLE)
        self.assertIsNone(found.installed)
        self.assertEqual(found.verified, "1.19")
        # Distinct from a version that was read and disagreed: there is nothing
        # here to tell the user to go and change.
        self.assertFalse(found.blocks)

    def test_an_unverified_release_has_nothing_to_compare_against(self):
        unverified = "NPEB01204"
        self.assertTrue(titles.is_recognised(unverified))
        self.assertFalse(titles.is_verified(unverified))
        self.assertIsNone(titles.verified_update_for(unverified))
        found = flow.update_check(unverified, "1.09")
        self.assertEqual(found.verdict, flow.UPDATE_NOT_ESTABLISHED)
        self.assertFalse(found.blocks)
        self.assertIsNone(found.verified)

    def test_a_title_that_is_not_ours_at_all_claims_nothing(self):
        found = flow.update_check("BLES99999", "1.00")
        self.assertEqual(found.verdict, flow.UPDATE_NOT_ESTABLISHED)
        self.assertFalse(found.blocks)


@unittest.skipIf(QApplication is None, "PySide6 is not available")
class TheUpdateGateOnScreen(ScreenCase):
    """Apply is not offered on a build nobody checked the fix against."""

    BO2_FILES = ("EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self")

    def scanned_screen(self, tu_version, title_id=BO2_ID):
        content = f"EP0002-{title_id}_00-CODBLOPS2PATCH09"
        self.start(title_id, {name: self_file("bo2", name,
                                              content_id=content)
                              for name in self.BO2_FILES})
        screen, services = self.build(patcher.BlackOpsTwoPatcher)
        self.addCleanup(setattr, patcher, "detect", patcher.detect)
        self.detector = detector_for(installation("ready", title_id, "bo2",
                                                  tu_version=tu_version))
        patcher.detect = type("stub", (), {
            "find_installations": staticmethod(
                lambda lister: self.detector(lister))})
        screen.on_enter()
        self.settle(services)
        return screen, services

    def words(self, screen):
        return " ".join([screen._update_heading.text(),
                         screen._update_body.text(), screen._detail.text()])

    def test_the_verified_version_is_offered_the_fix_as_before(self):
        screen, _services = self.scanned_screen("1.19")
        self.assertTrue(screen._patch.isEnabled())
        self.assertTrue(screen._update.isHidden())

    def test_another_version_does_not_get_the_fix_offered(self):
        screen, _services = self.scanned_screen("1.09")
        self.assertFalse(screen._patch.isEnabled())
        self.assertFalse(screen._update.isHidden())
        said = self.words(screen)
        self.assertIn("1.19", said)
        self.assertIn("1.09", said)
        self.assertIn("stops starting", said)
        # Not the user's fault and not a broken console.
        self.assertIn("Nothing is wrong with your console", said)

    def test_pressing_apply_on_the_wrong_version_writes_nothing(self):
        screen, services = self.scanned_screen("1.09")
        screen._on_patch()
        self.settle(services)
        self.assertEqual(self.server.written, {})

    def test_the_way_out_is_the_game_updates_card_on_this_title(self):
        screen, _services = self.scanned_screen("1.09")
        asked = []
        screen.request_tool.connect(lambda key, title: asked.append((key,
                                                                     title)))
        screen._updates_button.click()
        self.assertEqual(asked, [("updates", "bo2")])

    def test_with_nothing_listening_the_button_still_goes_somewhere(self):
        screen, _services = self.scanned_screen("1.09")
        home = []
        screen.request_home.connect(lambda: home.append(True))
        screen._updates_button.click()
        self.assertEqual(home, [True])

    def test_updating_and_scanning_again_offers_the_fix(self):
        screen, services = self.scanned_screen("1.09")
        self.assertFalse(screen._patch.isEnabled())
        self.detector = detector_for(installation("ready", BO2_ID, "bo2",
                                                  tu_version="1.19"))
        screen.start_scan()
        self.settle(services)
        self.assertTrue(screen._patch.isEnabled())
        self.assertTrue(screen._update.isHidden())

    def test_a_version_that_could_not_be_read_is_not_called_the_wrong_one(self):
        screen, _services = self.scanned_screen(None)
        said = self.words(screen)
        self.assertFalse(screen._update.isHidden())
        self.assertIn("could not tell which version", said)
        self.assertNotIn("has update None", said)
        # A different case with a different remedy, so not the same sentence.
        self.assertNotIn("is not the version the fix was checked on", said)
        self.assertTrue(screen._patch.isEnabled())

    def test_an_unverified_release_is_never_told_its_update_is_out_of_date(self):
        # There is no verified update for this release, so there is nothing for
        # the installed version to disagree with. It is already answered as the
        # release nobody has confirmed, and must not be answered twice.
        screen, _services = self.scanned_screen("1.09", title_id="NPEB01204")
        self.assertTrue(screen._update.isHidden())
        said = self.words(screen)
        self.assertNotIn("stops starting", screen._update_body.text())
        self.assertIn("nobody has confirmed", said)
        self.assertTrue(screen._patch.isEnabled())

    def test_no_colour_is_written_into_the_update_panel_by_hand(self):
        screen, _services = self.scanned_screen("1.09")
        sheet = screen._update.styleSheet() + screen._update_heading.styleSheet()
        self.assertNotIn("#f", sheet.lower().replace("#808080", ""))
