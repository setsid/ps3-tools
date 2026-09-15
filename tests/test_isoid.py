"""Identification by ranged read, entirely offline.

Nothing here opens a socket to anything but 127.0.0.1, and the only server it
talks to is the mock in tests/mock_webman.py. The images come from
tests/fixtures/iso/make_fixtures.py rather than from disk, because a crafted
header that says where every field came from is worth more as a fixture than
an opaque blob.
"""

import ftplib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for candidate in (ROOT, HERE, os.path.join(HERE, "fixtures", "iso")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

# Imported as top level modules rather than through a tests package: there
# is an unrelated "tests" package installed on this machine, and a regular
# package anywhere on the path beats a namespace one in the repo.
import make_fixtures                                          # noqa: E402
from ps3diag import isoid, isoreader                          # noqa: E402
from ps3diag.artefacts import ArtefactSet                     # noqa: E402
from ps3diag.transport import FtpLister, UnsafeRequest        # noqa: E402
from mock_webman import MockWebmanFtp                         # noqa: E402

SAMPLE_ZIP = os.path.join(HERE, "fixtures", "sample-diagnostic.zip")

PS3_PATH = "/dev_hdd0/PS3ISO/Call of Duty - Modern Warfare 3 [BLES01428].iso"
PS2_PATH = "/dev_hdd0/PS2ISO/Persona 4 [SLUS-21782].iso"


def entry(name, device="dev_hdd0", folder="PS3ISO", size=0, kind="file"):
    return {"name": name, "device": device, "folder": folder, "size": size,
            "kind": kind}


class CountingRead:
    """A read_range over a buffer that remembers what was asked for."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def __call__(self, offset, length):
        self.calls.append((offset, length))
        return self.data[offset:offset + length]

    @property
    def bytes_returned(self):
        return sum(len(self.data[offset:offset + length])
                   for offset, length in self.calls)


# --- the pure parsers ------------------------------------------------------

class ParamSfoTests(unittest.TestCase):

    def test_reads_back_what_the_console_writes(self):
        fields, reason = isoid.parse_param_sfo(make_fixtures.param_sfo())
        self.assertIsNone(reason)
        self.assertEqual(fields["TITLE_ID"], "BLES01428")
        self.assertEqual(fields["TITLE"], "Call of Duty: Modern Warfare 3")
        self.assertEqual(fields["APP_VER"], "01.24")
        self.assertEqual(fields["CATEGORY"], "DG")
        self.assertEqual(fields["PS3_SYSTEM_VER"], "03.5500")
        self.assertEqual(fields["PARENTAL_LEVEL"], 5)

    def test_a_half_written_sfo_gives_back_what_it_has(self):
        whole = make_fixtures.param_sfo()
        fields, reason = isoid.parse_param_sfo(whole[:len(whole) // 2])
        self.assertIsNotNone(reason)
        self.assertIn("truncated", reason)
        self.assertIsInstance(fields, dict)

    def test_rubbish_is_a_reason_not_an_exception(self):
        for data in (b"", b"not a psf at all", b"\x00" * 64, None):
            fields, reason = isoid.parse_param_sfo(data)
            self.assertEqual(fields, {})
            self.assertIsNotNone(reason)

    def test_an_absurd_entry_count_is_refused(self):
        head = bytearray(make_fixtures.param_sfo())
        head[16:20] = (99999).to_bytes(4, "little")
        fields, reason = isoid.parse_param_sfo(bytes(head))
        self.assertEqual(fields, {})
        self.assertIn("credible", reason)


class SystemCnfTests(unittest.TestCase):

    def test_boot2_gives_the_title_id(self):
        fields = isoid.parse_system_cnf(make_fixtures.PS2_SYSTEM_CNF)
        self.assertEqual(fields["BOOT2"], "cdrom0:\\SLUS_217.82;1")
        self.assertEqual(isoid.title_id_from_boot(fields["BOOT2"]),
                         "SLUS21782")

    def test_the_psone_spelling_works_too(self):
        self.assertEqual(isoid.title_id_from_boot("cdrom:\\SLES_007.27;1"),
                         "SLES00727")

    def test_a_boot_line_with_no_id_is_not_invented(self):
        self.assertIsNone(isoid.title_id_from_boot("cdrom0:\\ESR.ELF;1"))
        self.assertIsNone(isoid.title_id_from_boot(""))
        self.assertIsNone(isoid.title_id_from_boot(None))


# --- walking an image ------------------------------------------------------

class WalkTests(unittest.TestCase):

    def test_ps3_bridge_image(self):
        identity = isoid.identify_bytes(make_fixtures.build_ps3_bridge())
        self.assertEqual(identity.method, "param_sfo")
        self.assertEqual(identity.filesystem, "iso9660")
        self.assertEqual(identity.title_id, "BLES01428")
        self.assertEqual(identity.title, "Call of Duty: Modern Warfare 3")
        self.assertEqual(identity.app_version, "1.24")
        self.assertEqual(identity.category, "DG")
        self.assertEqual(identity.ps3_system_ver, "3.5500")
        self.assertIsNone(identity.reason)

    def test_ps3_udf_only_image(self):
        identity = isoid.identify_bytes(make_fixtures.build_ps3_udf())
        self.assertEqual(identity.method, "param_sfo")
        self.assertEqual(identity.filesystem, "udf")
        self.assertEqual(identity.title_id, "BLES01428")

    def test_ps2_image(self):
        identity = isoid.identify_bytes(make_fixtures.build_ps2())
        self.assertEqual(identity.method, "system_cnf")
        self.assertEqual(identity.title_id, "SLUS21782")
        self.assertIsNone(identity.title)

    def test_a_truncated_image_gives_a_reason(self):
        identity = isoid.identify_bytes(make_fixtures.build_truncated())
        self.assertEqual(identity.method, "none")
        self.assertIsNone(identity.title_id)
        self.assertIsInstance(identity.reason, str)
        self.assertTrue(identity.reason)

    def test_a_file_that_is_not_an_image_gives_a_reason(self):
        identity = isoid.identify_bytes(make_fixtures.build_not_an_iso())
        self.assertEqual(identity.method, "none")
        self.assertIsNone(identity.title_id)
        self.assertIn("sector 16", identity.reason)

    def test_nothing_readable_at_all(self):
        identity = isoid.identify(lambda offset, length: b"")
        self.assertIsNone(identity.title_id)
        self.assertTrue(identity.reason)

    def test_a_read_that_raises_is_a_reason_not_a_traceback(self):
        def read_range(offset, length):
            raise OSError("the console stopped answering")

        identity = isoid.identify(read_range)
        self.assertIsNone(identity.title_id)
        self.assertIn("stopped answering", identity.reason)

    def test_the_walk_asks_for_a_few_sectors_of_a_large_image(self):
        image = make_fixtures.build_ps3_bridge(pad_to=8 * 1024 * 1024)
        counter = CountingRead(image)
        identity = isoid.identify(counter)
        self.assertEqual(identity.title_id, "BLES01428")
        self.assertLess(counter.bytes_returned, 64 * 1024)


# --- the driver ------------------------------------------------------------

class DriverTests(unittest.TestCase):

    def build(self, files, **kwargs):
        reader = isoreader.BytesReader(files)
        entries = [entry(os.path.basename(path),
                         folder=path.split("/")[2],
                         size=len(data))
                   for path, data in files.items()]
        payload = isoreader.identify_isos(entries, reader, **kwargs)
        return reader, payload

    def test_payload_shape(self):
        _reader, payload = self.build(
            {PS3_PATH: make_fixtures.build_ps3_bridge()})
        self.assertEqual(payload["schema_version"], 1)
        row = payload["isos"][0]
        self.assertEqual(sorted(row), sorted([
            "name", "opened", "device", "folder", "path", "size", "method",
            "title_id", "title", "app_version", "category", "region",
            "bytes_read", "name_mismatch", "reason"]))
        # It was read to a conclusion, so the caller may act on the answer.
        self.assertTrue(row["opened"])
        self.assertEqual(row["method"], "param_sfo")
        self.assertEqual(row["title_id"], "BLES01428")
        self.assertEqual(row["region"], "Europe")
        self.assertEqual(row["path"], PS3_PATH)
        self.assertFalse(row["name_mismatch"])
        self.assertIsNone(row["reason"])

    def test_a_ps2_image_is_identified_from_system_cnf(self):
        _reader, payload = self.build({PS2_PATH: make_fixtures.build_ps2()})
        row = payload["isos"][0]
        self.assertEqual(row["method"], "system_cnf")
        self.assertEqual(row["title_id"], "SLUS21782")
        self.assertEqual(row["region"], "North America")

    def test_the_name_disagreeing_with_the_image_is_the_headline(self):
        path = "/dev_hdd0/PS3ISO/The Last of Us [BCES01584].iso"
        _reader, payload = self.build(
            {path: make_fixtures.build_ps3_bridge()})
        row = payload["isos"][0]
        self.assertTrue(row["name_mismatch"])
        self.assertEqual(row["title_id"], "BLES01428")
        self.assertEqual(isoreader.mismatches(payload), [row])

    def test_an_unreadable_image_falls_back_to_its_name(self):
        _reader, payload = self.build(
            {PS3_PATH: make_fixtures.build_not_an_iso()})
        row = payload["isos"][0]
        self.assertEqual(row["method"], "filename")
        self.assertEqual(row["title_id"], "BLES01428")
        self.assertEqual(row["region"], "Europe")
        self.assertFalse(row["name_mismatch"])
        self.assertTrue(row["reason"])

    def test_a_nameless_unreadable_image_says_so(self):
        path = "/dev_hdd0/PS3ISO/disc dump 04.iso"
        _reader, payload = self.build({path: make_fixtures.build_not_an_iso()})
        row = payload["isos"][0]
        self.assertEqual(row["method"], "none")
        self.assertIsNone(row["title_id"])
        self.assertTrue(row["reason"])

    def test_the_budget_stops_the_walk_rather_than_the_walk_stopping(self):
        image = make_fixtures.build_ps3_bridge(pad_to=4 * 1024 * 1024)
        reader, payload = self.build({PS3_PATH: image},
                                     block=16 * 1024, file_budget=16 * 1024)
        row = payload["isos"][0]
        self.assertLessEqual(row["bytes_read"], 16 * 1024)
        self.assertEqual(row["method"], "filename")
        self.assertIn("stopped after", row["reason"])
        self.assertLessEqual(reader.bytes_read, 16 * 1024)

    def test_the_total_budget_is_shared_across_files(self):
        files = {f"/dev_hdd0/PS3ISO/dump {index} [BLES0142{index}].iso":
                 make_fixtures.build_ps3_bridge(pad_to=1024 * 1024)
                 for index in range(4)}
        reader, payload = self.build(files, total_budget=96 * 1024)
        self.assertLessEqual(reader.bytes_read, 96 * 1024)
        self.assertTrue(any(row["method"] == "filename"
                            for row in payload["isos"]))

    def test_folders_and_non_images_are_left_alone(self):
        entries = [entry("BLES01718", folder="GAMES", kind="directory"),
                   entry("notes.txt", folder="PS3ISO"),
                   entry("Persona 5 [NPEB02143]", folder="GAMES",
                         kind="directory")]
        payload = isoreader.identify_isos(entries,
                                          isoreader.BytesReader({}))
        self.assertEqual(payload["isos"], [])

    def test_a_path_off_the_ranged_read_allowlist_is_never_fetched(self):
        entries = [entry("something.iso", device="dev_flash",
                         folder="vsh/etc")]
        reader = isoreader.BytesReader({})
        payload = isoreader.identify_isos(entries, reader)
        row = payload["isos"][0]
        self.assertEqual(row["bytes_read"], 0)
        self.assertIn("allowlist", row["reason"])

    def test_the_reader_refuses_an_off_allowlist_read_outright(self):
        reader = isoreader.BytesReader({})
        ftp_reader = isoreader.FtpRangeReader(connect=lambda: reader)
        with self.assertRaises(UnsafeRequest):
            ftp_reader.read("/dev_flash/vsh/etc/version.txt", 0, 16)


# --- against the mock console ---------------------------------------------

def connector(port):
    def connect():
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", port, timeout=5)
        ftp.login("anonymous", "ps3-diag@localhost")
        return ftp
    return connect


class MockConsoleTests(unittest.TestCase):
    """The whole path: FTP, REST, RETR, abort, parse. 127.0.0.1 only."""

    def setUp(self):
        self.image = make_fixtures.build_ps3_bridge(pad_to=4 * 1024 * 1024)
        self.ps2 = make_fixtures.build_ps2(pad_to=2 * 1024 * 1024)
        self.server = MockWebmanFtp(files={PS3_PATH: self.image,
                                           PS2_PATH: self.ps2}).start()
        self.addCleanup(self.server.stop)
        self.lister = FtpLister("127.0.0.1",
                                factory=connector(self.server.port))
        self.addCleanup(self.lister.close)

    def entries(self):
        return [entry(os.path.basename(PS3_PATH), size=len(self.image)),
                entry(os.path.basename(PS2_PATH), folder="PS2ISO",
                      size=len(self.ps2))]

    def test_identifies_over_ftp_without_downloading_the_image(self):
        reader = isoreader.reader_for(self.lister)
        payload = isoreader.identify_isos(self.entries(), reader)
        rows = {row["title_id"]: row for row in payload["isos"]}
        self.assertEqual(sorted(rows), ["BLES01428", "SLUS21782"])
        self.assertEqual(rows["BLES01428"]["method"], "param_sfo")
        self.assertEqual(rows["SLUS21782"]["method"], "system_cnf")
        for row in payload["isos"]:
            self.assertLess(row["bytes_read"], 256 * 1024)
            self.assertLess(row["bytes_read"], row["size"] // 8)
        self.assertLess(reader.bytes_read, 512 * 1024)

    def test_the_transfers_are_ranged_and_aborted(self):
        reader = isoreader.reader_for(self.lister)
        isoreader.identify_isos(self.entries(), reader)
        verbs = [command.split(" ")[0].upper()
                 for command in self.server.commands]
        self.assertIn("REST", verbs)
        self.assertIn("RETR", verbs)
        self.assertGreater(reader.aborted, 0)
        # Nothing that changes anything was ever sent.
        self.assertEqual(set(verbs) - {"USER", "PASS", "TYPE", "PASV", "REST",
                                       "RETR", "SIZE", "QUIT", "LIST"}, set())

    def test_the_connection_is_let_go_between_images(self):
        reader = isoreader.reader_for(self.lister)
        isoreader.identify_isos(self.entries(), reader)
        self.assertIsNone(self.lister._ftp)
        # And it is still usable afterwards rather than wedged.
        self.assertEqual(reader.read(PS3_PATH, 0, 16), self.image[:16])
        reader.release()

    def test_a_missing_image_is_a_reason_not_a_crash(self):
        reader = isoreader.reader_for(self.lister)
        payload = isoreader.identify_isos(
            [entry("Ghost [BLES00001].iso", size=1024)], reader)
        row = payload["isos"][0]
        self.assertEqual(row["method"], "filename")
        self.assertEqual(row["title_id"], "BLES00001")
        self.assertTrue(row["reason"])


class SampleSetTests(unittest.TestCase):
    """The rows this feature is handed in real life come from a saved set."""

    def test_every_iso_in_the_sample_set_produces_a_row(self):
        artefacts = ArtefactSet.from_zip(SAMPLE_ZIP)
        entries = artefacts.game_entries()
        payload = isoreader.identify_isos(entries, isoreader.BytesReader({}))
        names = [row["name"] for row in payload["isos"]]
        self.assertIn("Call of Duty - Modern Warfare 3 [BLES01428].iso", names)
        self.assertIn("Persona 4 [SLUS-21782].iso", names)
        self.assertNotIn("_incoming", names)
        self.assertNotIn("BLES01718", names)
        for row in payload["isos"]:
            self.assertEqual(row["method"], "filename")
            self.assertEqual(row["bytes_read"], 0)
            self.assertFalse(row["name_mismatch"])


if __name__ == "__main__":
    unittest.main()
