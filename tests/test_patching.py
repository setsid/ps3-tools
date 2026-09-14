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

import ftplib
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

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
        self.assertIn("signing parameters", report.error)
        self.assertFalse(report.can_patch)
        self.assertEqual(self.server.commands, [])

    def test_every_known_title_id_is_accepted_and_nothing_else_is(self):
        for title_id in titles.KNOWN_TITLE_IDS:
            self.assertIsNotNone(titles.config_for(title_id), title_id)
        for title_id in titles.NOT_A_TITLE + ("BLES01702", "", "BLUS99999"):
            self.assertIsNone(titles.config_for(title_id), title_id)

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
        self.assertEqual(report.file_for("default_mp.self").state,
                         flow.UNRECOGNISED)
        self.assertIn("klicensee",
                      report.file_for("default_mp.self").detail)

    def test_black_ops_two_scans_all_three_files(self):
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self",
                                         "t6mp_ps3f.self")})
        report = self.scan(BO2_ID)
        self.assertEqual(sorted(item.name for item in report.to_patch),
                         ["EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self"])
        self.assertTrue(report.can_patch)

    def test_one_unrecognised_file_stops_the_whole_title(self):
        # Patching two of Black Ops II's three files leaves campaign and
        # zombies freezing, which is worse than doing nothing.
        files = {name: self_file("bo2", name)
                 for name in ("EBOOT.BIN", "t6_ps3f.self")}
        files["t6mp_ps3f.self"] = self_file("bo2", "t6mp_ps3f.self", "neither")
        self.start(BO2_ID, files)
        report = self.scan(BO2_ID)
        self.assertEqual(len(report.to_patch), 2)
        self.assertFalse(report.can_patch)

    def test_a_missing_file_stops_the_whole_title(self):
        self.start(BO2_ID, {name: self_file("bo2", name)
                            for name in ("EBOOT.BIN", "t6_ps3f.self")})
        report = self.scan(BO2_ID)
        self.assertEqual(report.missing, ["t6mp_ps3f.self"])
        self.assertFalse(report.can_patch)

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

    def test_the_bundled_scetool_refuses_to_pretend_it_can_run_here(self):
        # It is a Windows binary. Anything that claimed otherwise would fail
        # halfway through somebody's only copy of a game binary.
        real = scetool.Scetool()
        if sys.platform == "win32":
            self.skipTest("this machine can run it")
        self.assertTrue(real.problem)
        self.assertIn("Windows", real.problem)


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
        services.wait(30000)
        for _ in range(10):
            self.application.processEvents()


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


def installation(state, title_id=BO2_ID, key="bo2"):
    config = titles.TITLES[key]
    return detection.Installation(
        title_id=title_id, title_key=key if state != "unknown_variant" else None,
        name=config["name"], short=config["short"],
        path=f"/dev_hdd0/game/{title_id}",
        usrdir=titles.usrdir_for(title_id), state=state, config=config)


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
        self.assertIn("not anything you have done", words)
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

    def test_an_unknown_sku_is_refused_with_its_own_reason(self):
        screen = self.screen_with(
            StubLister(), detector_for(installation("unknown_variant")))
        self.assertEqual(screen._panel_token, "warn")
        words = self.words(screen)
        self.assertIn("not one this tool knows", words)
        self.assertIn("will not start", words)
        self.assertFalse(screen._patch.isEnabled())

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
        self.assertFalse(screen._patch.isEnabled())

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
        self.assertIn("Changed: EBOOT.BIN, t6_ps3f.self, t6mp_ps3f.self", text)
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
        self.assertIn("Changed: EBOOT.BIN", text)
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
        self.assertIn("Changed: EBOOT.BIN", text)
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
        self.assertIn("Changed: EBOOT.BIN, t6_ps3f.self, t6mp_ps3f.self",
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
