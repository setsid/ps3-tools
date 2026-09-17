"""Finding installations, and refusing to guess about unknown ones.

Everything here is offline. The one test that uses the real FTP transport
points it at a mock bound to 127.0.0.1.

None of this has ever run against a console with Call of Duty installed on it.
The console the tool was developed against had five homebrew folders under
/dev_hdd0/game and no games at all, which is the listing in
fixtures/detect/list_game_real_console.txt.
"""

import ftplib

from support import FixtureCase, fixture

import make_fixtures                                        # noqa: E402
from mock_webman import MockWebmanFtp                       # noqa: E402
from ps3diag.transport import FtpLister, UnsafeRequest      # noqa: E402
from ps3tools import detect, titles                         # noqa: E402

GAME = "/dev_hdd0/game"


class FakeLister:
    """LIST out of a dict, and a 550 for anything else.

    A path the console does not have is a permanent refusal, not a dead link,
    and detect has to tell the two apart: one is a title with no update yet and
    the other is a console that has stopped answering.
    """

    def __init__(self, listings, fail_on=None, error=None):
        self.listings = {key.rstrip("/") or "/": value
                         for key, value in listings.items()}
        self.fail_on = (fail_on or "").rstrip("/")
        self.error = error or ftplib.error_temp("421 timeout")
        self.asked = []

    def list_dir(self, path):
        self.asked.append(path)
        key = path.rstrip("/") or "/"
        if self.fail_on and key == self.fail_on:
            raise self.error
        if key not in self.listings:
            raise ftplib.error_perm(f"550 {path}: no such directory")
        return self.listings[key]


def usrdir(title_id):
    return f"{GAME}/{title_id}/USRDIR"


def mock_lister(case, **kwargs):
    """The real transport, pointed at a mock console on 127.0.0.1."""
    server = MockWebmanFtp(**kwargs).start()
    case.addCleanup(server.stop)
    port = server.port

    def factory():
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", port, timeout=5)
        ftp.login("anonymous", "ps3-diag@localhost")
        return ftp

    lister = FtpLister("127.0.0.1", 5, factory=factory)
    case.addCleanup(lister.close)
    return lister


def param_sfo(version, title_id="BLES01717"):
    """A PSF the way the console writes one, carrying one APP_VER."""
    return make_fixtures.param_sfo((("APP_VER", version),
                                    ("CATEGORY", "GD"),
                                    ("TITLE", "Call of Duty: Black Ops II"),
                                    ("TITLE_ID", title_id)))


def listing(*parts):
    return fixture("detect", *parts)


def folders(*names):
    """A /dev_hdd0/game listing holding exactly these title folders."""
    return "".join("drwxrwxrwx   1 root     root            0 Sep 06 19:51 "
                   "%s\n" % name for name in names)


class Ready(FixtureCase):
    """The happy case, driven through the real transport at a mock console."""

    def setUp(self):
        self.lister = mock_lister(self)
        self.report = detect.find_installations(self.lister)

    def test_both_installed_titles_are_found_and_ready(self):
        rows = {item.title_id: item for item in self.report.installations}
        self.assertEqual(sorted(rows), ["BLES01428", "BLES01717"])
        self.assertEqual(rows["BLES01717"].state, detect.READY)
        self.assertEqual(rows["BLES01428"].state, detect.READY)
        self.assertTrue(all(item.ready for item in rows.values()))

    def test_the_fields_the_screen_reads(self):
        bo2 = self.report.for_title("bo2")[0]
        self.assertEqual(bo2.title_id, "BLES01717")
        self.assertEqual(bo2.title_key, "bo2")
        self.assertEqual(bo2.short, "Black Ops II")
        self.assertEqual(bo2.region, "Europe")
        self.assertEqual(bo2.path, f"{GAME}/BLES01717")
        self.assertEqual(bo2.usrdir, usrdir("BLES01717"))
        self.assertEqual(bo2.usrdir, titles.usrdir_for("BLES01717"))
        self.assertEqual(bo2.missing, [])
        self.assertEqual(bo2.expected,
                         ["EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self"])
        self.assertIs(bo2.config, titles.TITLES["bo2"])
        names = {item["name"] for item in bo2.files}
        self.assertTrue(set(bo2.expected) <= names)
        for item in bo2.files:
            self.assertEqual(sorted(item),
                             ["kind", "modified", "name", "size"])

    def test_nothing_that_is_not_ours_is_picked_up(self):
        # NPEB02143 and PSNPATCH are both under /dev_hdd0/game and neither is
        # a Call of Duty. A false positive here is a patcher offering to
        # rewrite somebody's unrelated game.
        found = {item.title_id for item in self.report.installations}
        self.assertNotIn("NPEB02143", found)
        self.assertNotIn("PSNPATCH", found)

    def test_for_title_accepts_a_title_id_as_well_as_a_key(self):
        self.assertEqual(self.report.for_title("BLES01428")[0].title_key,
                         "mw3")
        self.assertEqual(self.report.for_title("nonsense"), [])

    def test_the_title_update_version_is_not_guessed(self):
        for item in self.report.installations:
            self.assertIsNone(item.tu_version)
            self.assertIn("PARAM.SFO", item.tu_detail)


class States(FixtureCase):
    def report(self, listings, **kwargs):
        return detect.find_installations(FakeLister(listings, **kwargs))

    def test_no_update_when_there_is_no_usrdir(self):
        # The common case, and not an error: the game is installed from disc
        # or ISO but the title update has never been downloaded.
        report = self.report({GAME: listing("list_game_regional.txt"),
                              usrdir("BLES01717"): listing(
                                  "list_usrdir_bo2.txt")})
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(rows["BLES01718"].state, detect.NO_UPDATE)
        self.assertEqual(rows["BLES01718"].files, [])
        self.assertEqual(rows["BLES01718"].missing,
                         rows["BLES01718"].expected)
        self.assertIn("title update has not been installed",
                      rows["BLES01718"].tu_detail)

    def test_not_found_on_a_console_shaped_like_the_real_one(self):
        # Exactly what /dev_hdd0/game held on the console this was built
        # against: homebrew, a PSN title, and no Call of Duty anywhere.
        report = self.report({GAME: listing("list_game_real_console.txt")})
        self.assertEqual(report.installations, [])
        for key in ("bo2", "mw3"):
            absent = report.for_title(key)
            self.assertEqual(len(absent), 1)
            self.assertEqual(absent[0].state, detect.NOT_FOUND)
            self.assertIsNone(absent[0].title_id)
            self.assertEqual(absent[0].title_key, key)
            self.assertEqual(absent[0].short, titles.TITLES[key]["short"])
        self.assertTrue(any("No Call of Duty installation" in note
                            for note in report.notes), report.notes)

    def test_another_game_never_reaches_a_screen_that_does_not_fix_it(self):
        report = self.report({
            GAME: listing("list_game_unknown_variant.txt"),
            usrdir("BLJS10032"): listing("list_usrdir_bo2.txt"),
            usrdir("NPEB02143"): listing("list_usrdir_npeb02143.txt")})
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(sorted(rows), ["BLJS10032"])
        variant = rows["BLJS10032"]
        # Its files are Black Ops II's, so it is offered as an untested
        # release of Black Ops II and reaches no other screen. Turning up
        # elsewhere is the fault that had the Black Ops 1 screen offering
        # somebody Ghosts.
        self.assertEqual(variant.title_key, "bo2")
        self.assertTrue(variant.untested)
        self.assertFalse(variant.verified)
        self.assertEqual(report.for_title("bo2")[0].title_id, "BLJS10032")
        self.assertEqual(report.for_title("bo1")[0].state, detect.NOT_FOUND)
        self.assertEqual(report.for_title("mw3")[0].state, detect.NOT_FOUND)

    def test_unnamed_releases_are_offered_rather_than_collected_up(self):
        """Three Call of Duty folders the table cannot name.

        They used to be gathered into one sentence saying they would be left
        alone. Their files say which game they are, so each is offered as an
        untested release of that game instead, and there is nothing to
        collect.
        """
        report = self.report({
            GAME: folders("BLES00683", "BLES01945", "BLES02077"),
            usrdir("BLES00683"): listing("list_usrdir_bo2.txt"),
            usrdir("BLES01945"): listing("list_usrdir_bo2.txt"),
            usrdir("BLES02077"): listing("list_usrdir_bo2.txt")})
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(sorted(rows),
                         ["BLES00683", "BLES01945", "BLES02077"])
        for row in rows.values():
            self.assertEqual(row.title_key, "bo2")
            self.assertTrue(row.untested)
            self.assertFalse(row.verified)
        self.assertEqual([note for note in report.notes
                          if "left alone" in note], [])

    def test_a_folder_that_could_be_two_games_is_the_one_case_left(self):
        """The only honest refusal that remains.

        A folder holding the naming files of more than one game cannot be
        attributed from the listing alone, so it is left alone and said to be
        left alone.
        """
        self.assertEqual(
            sorted(titles.keys_for_files(
                ["t5mp_ps3f.self", "default_mp.self"])),
            ["bo1", "mw3"])

    def test_bljm61034_is_never_treated_as_black_ops_two(self):
        # It was in an earlier draft of the table and Sony's manifest returns
        # nothing for it. Here it is sitting on the console with a USRDIR full
        # of Black Ops II looking files, which is the way it would get patched
        # by accident.
        self.assertIn("BLJM61034", titles.NOT_A_TITLE)
        self.assertIsNone(titles.config_for("BLJM61034"))
        report = self.report({
            GAME: listing("list_game_regional.txt"),
            usrdir("BLJM61034"): listing("list_usrdir_bo2.txt")})
        rows = {item.title_id: item for item in report.installations}
        self.assertIsNone(rows["BLJM61034"].title_key)
        self.assertIsNone(rows["BLJM61034"].config)
        self.assertEqual(rows["BLJM61034"].state, detect.UNKNOWN_VARIANT)
        for item in report.for_title("bo2"):
            self.assertNotEqual(item.title_id, "BLJM61034")


class Robustness(FixtureCase):
    def test_a_console_that_stops_answering_keeps_what_was_found(self):
        lister = FakeLister({GAME: listing("list_game_regional.txt"),
                             usrdir("BLES01717"): listing(
                                 "list_usrdir_bo2.txt")},
                            fail_on=usrdir("BLES01718"))
        report = detect.find_installations(lister)
        self.assertEqual([item.title_id for item in report.installations],
                         ["BLES01717"])
        self.assertTrue(any("stopped answering" in note
                            for note in report.notes), report.notes)
        # The walk stops rather than hammering a console that has gone.
        self.assertNotIn(usrdir("BLUS31011") + "/", lister.asked)

    def test_a_dropped_control_connection_through_the_real_transport(self):
        def fault(verb, argument):
            if verb == "LIST" and "BLES01428" in argument:
                return "DROP"
            return None

        report = detect.find_installations(mock_lister(self, fault=fault))
        self.assertEqual([item.title_id for item in report.installations],
                         ["BLES01717"])
        self.assertTrue(report.notes)

    def test_no_game_directory_at_all(self):
        report = detect.find_installations(FakeLister({}))
        self.assertEqual(report.installations, [])
        self.assertTrue(any("/dev_hdd0/game" in note
                            for note in report.notes), report.notes)
        self.assertEqual(report.for_title("mw3")[0].state, detect.NOT_FOUND)

    def test_it_never_raises_whatever_the_lister_does(self):
        class Exploding:
            def list_dir(self, path):
                raise RuntimeError("the socket went away")

        report = detect.find_installations(Exploding())
        self.assertEqual(report.installations, [])
        self.assertTrue(report.notes)

    def test_a_listing_it_cannot_parse_is_reported_not_dropped(self):
        report = detect.find_installations(
            FakeLister({GAME: fixture("ftp", "list_odd.txt")}))
        self.assertEqual(report.installations, [])
        self.assertTrue(any("does not recognise" in note
                            for note in report.notes), report.notes)


class TitleUpdateVersion(FixtureCase):
    """Reported where there is evidence, and honest where there is not."""

    def listings(self):
        return {GAME: listing("list_game_regional.txt"),
                usrdir("BLES01717"): listing("list_usrdir_bo2.txt"),
                usrdir("BLES01718"): listing("list_usrdir_bo2.txt")}

    def test_read_from_param_sfo_when_something_can_read_one(self):
        sfo = param_sfo("01.19")

        def reader(path):
            return sfo if path == f"{GAME}/BLES01717/PARAM.SFO" else None

        report = detect.find_installations(FakeLister(self.listings()),
                                           param_sfo_reader=reader)
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(rows["BLES01717"].tu_version, "1.19")
        self.assertIn("APP_VER", rows["BLES01717"].tu_detail)
        # The second title has no PARAM.SFO to read, so it says so rather than
        # borrowing the answer from the first.
        self.assertIsNone(rows["BLES01718"].tu_version)
        self.assertIn("could not be read", rows["BLES01718"].tu_detail)

    def test_without_a_reader_there_is_no_version_and_a_reason(self):
        report = detect.find_installations(FakeLister(self.listings()))
        ready = [item for item in report.installations if item.ready]
        self.assertTrue(ready)
        for item in ready:
            self.assertIsNone(item.tu_version)
            self.assertIn("could not be read", item.tu_detail)

    def test_a_reader_that_fails_costs_the_version_and_nothing_else(self):
        def reader(path):
            raise OSError("connection reset")

        report = detect.find_installations(FakeLister(self.listings()),
                                           param_sfo_reader=reader)
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(rows["BLES01717"].state, detect.READY)
        self.assertIsNone(rows["BLES01717"].tu_version)
        self.assertIn("connection reset", rows["BLES01717"].tu_detail)

    def test_read_through_the_real_transport_by_default(self):
        # No reader is passed: the lister's own capped binary read is used.
        lister = mock_lister(
            self, files={f"{GAME}/BLES01717/PARAM.SFO": param_sfo("01.19")})
        report = detect.find_installations(lister)
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(rows["BLES01717"].tu_version, "1.19")
        self.assertIn(f"{GAME}/BLES01717/PARAM.SFO",
                      rows["BLES01717"].tu_detail)
        # The console has no PARAM.SFO for the other title, which costs the
        # version and nothing else.
        self.assertEqual(rows["BLES01428"].state, detect.READY)
        self.assertIsNone(rows["BLES01428"].tu_version)
        self.assertIn("could not be read", rows["BLES01428"].tu_detail)

    def test_the_usrdir_copy_is_tried_when_the_folder_has_none(self):
        # Which of the two carries APP_VER on a real console is unverified, so
        # both are tried.
        lister = mock_lister(
            self,
            files={f"{usrdir('BLES01717')}/PARAM.SFO": param_sfo("01.09")})
        report = detect.find_installations(lister)
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(rows["BLES01717"].tu_version, "1.09")
        self.assertIn("USRDIR/PARAM.SFO", rows["BLES01717"].tu_detail)

    def test_a_lister_that_cannot_read_bytes_says_so(self):
        lister = FakeLister(self.listings())
        self.assertFalse(hasattr(lister, "download_bytes"))
        report = detect.find_installations(lister)
        ready = [item for item in report.installations if item.ready]
        self.assertTrue(ready)
        for item in ready:
            self.assertIsNone(item.tu_version)
            self.assertIn("no way to fetch PARAM.SFO", item.tu_detail)

    def test_a_refused_read_is_not_fatal(self):
        class Refusing(FakeLister):
            def download_bytes(self, path, max_bytes=None):
                raise UnsafeRequest(f"not on the byte-read allowlist: "
                                    f"{path!r}")

        report = detect.find_installations(Refusing(self.listings()))
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(rows["BLES01717"].state, detect.READY)
        self.assertIsNone(rows["BLES01717"].tu_version)
        self.assertIn("byte-read allowlist", rows["BLES01717"].tu_detail)

    def test_an_explicit_reader_wins_over_the_listers_own(self):
        lister = mock_lister(
            self, files={f"{GAME}/BLES01717/PARAM.SFO": param_sfo("01.19")})
        report = detect.find_installations(
            lister, param_sfo_reader=lambda path: param_sfo("01.09"))
        rows = {item.title_id: item for item in report.installations}
        self.assertEqual(rows["BLES01717"].tu_version, "1.09")

    def test_an_update_package_hash_is_never_mistaken_for_disk_evidence(self):
        # update_for maps Sony's package hash to a version. Nothing on the
        # console's disk hashes to it, so it is not used here at all.
        self.assertEqual(
            titles.update_for("BLES01717",
                              "87c75a80a1df1518b5b2212c3c258ac7ef9094ac"),
            "1.19")
        self.assertIsNone(titles.update_for("BLES01717", "0" * 40))
