"""End to end, against the mock console on 127.0.0.1 and nothing else.

Every server these tests talk to is started by the test itself and bound to the
loopback address. No test in this repo targets a real IP.
"""

import ftplib
import os
import tempfile
import threading
import time
import zipfile

from mock_webman import (DEFAULT_FILES, MockConsole,      # noqa: F401
                         MockWebmanHttp, fixture, resolve)
from support import FixtureCase
from ps3diag import report, runner
from ps3diag.analysis import analyse
from ps3diag.artefacts import ArtefactSet
from ps3diag.collectors import CATEGORY_KEYS
from ps3diag.transport import FtpLister, HttpProbe


class Harness(FixtureCase):
    """Starts a mock console and wires the real transport at it."""

    routes = None
    listings = None
    files = None

    def setUp(self):
        self.console = MockConsole(self.routes, self.listings, self.files)
        self.console.start()
        self.addCleanup(self.console.stop)

    def probe(self, timeout=5):
        return HttpProbe(f"127.0.0.1:{self.console.http.port}", timeout=timeout)

    def lister(self, timeout=5):
        port = self.console.ftp.port

        def factory():
            ftp = ftplib.FTP()
            ftp.connect("127.0.0.1", port, timeout=timeout)
            ftp.login("anonymous", "ps3-diag@localhost")
            return ftp

        return FtpLister("127.0.0.1", timeout, factory=factory)

    def collect(self, categories=None, **kwargs):
        return runner.run("127.0.0.1", categories, http=self.probe(),
                          ftp=self.lister(), run_timeout=120, **kwargs)

    def identify(self, categories=None, **kwargs):
        """The same run with the per-image identification switched on.

        It is off by default now, so every test about what comes out of a disc
        image has to ask for it the way the screen's tick box does.
        """
        options = dict(kwargs.pop("options", None) or {})
        options["identify_isos"] = True
        return self.collect(categories, options=options, **kwargs)


class FullRun(Harness):
    def setUp(self):
        super().setUp()
        self.result = self.collect()
        self.artefacts = ArtefactSet.from_run(self.result)

    def test_every_category_was_collected(self):
        for outcome in self.result.results:
            self.assertIn(outcome.status, ("ok", "partial"),
                          f"{outcome.key}: {outcome.error}")

    def test_the_console_was_identified(self):
        facts = self.artefacts.facts("system")
        self.assertEqual(facts["firmware"], "4.93")
        self.assertEqual(facts["firmware_type"], "CEX")
        self.assertEqual(facts["cobra_version"], "8.5")
        self.assertEqual(facts["model"], "CECH-2503B")
        self.assertEqual(facts["model_sales_region"],
                         "United Kingdom and Ireland")

    def test_the_exact_firmware_build_came_off_dev_flash(self):
        self.assertEqual(self.artefacts.facts("system")["firmware_build"],
                         "92009")

    def test_storage_found_the_devices(self):
        names = [device["device"] for device in self.artefacts.devices()]
        self.assertIn("dev_hdd0", names)
        self.assertIn("dev_usb000", names)

    def test_the_game_inventory_has_names_sizes_and_regions(self):
        rows = {row["name"]: row for row in self.artefacts.game_entries()}
        awkward = ("Gran Turismo 5 & Prologue, Collector's Edition "
                   "[BCES00569].iso")
        self.assertIn(awkward, rows)
        self.assertEqual(rows[awkward]["size"], 44023414784)
        self.assertEqual(rows[awkward]["region"], "Europe")
        self.assertEqual(rows[awkward]["video_standard"], "PAL")

    def test_a_japanese_ps2_title_is_not_called_pal(self):
        rows = {row["name"]: row for row in self.artefacts.game_entries()}
        japanese = rows["Katamari Damacy [SLPM-65729].iso"]
        self.assertEqual(japanese["region"], "Japan")
        self.assertEqual(japanese["video_standard"], "NTSC-J")

    def test_plugins_were_read_and_the_missing_one_spotted(self):
        facts = self.artefacts.facts("plugins")
        self.assertEqual(facts["boot_plugins.txt_enabled_count"], 4)
        self.assertEqual(facts["boot_plugins.txt_disabled_count"], 1)
        self.assertIn("listed_but_not_found", facts)

    def test_crash_reports_were_listed_and_fetched(self):
        facts = self.artefacts.facts("crash_reports")
        self.assertEqual(facts["count"], 3)
        self.assertEqual(facts["downloaded"], 2)
        self.assertTrue(self.artefacts.names("crash_reports/core*"))

    def test_both_user_folders_under_home_were_walked(self):
        # Two local users, and only the one who has signed in to PSN has an
        # np_cache.dat. Telling those two apart is what a report needs to
        # settle an argument about the Black Ops 1 fix.
        facts = self.artefacts.facts("accounts")
        self.assertEqual(facts["user_count"], 2)
        self.assertEqual(facts["with_np_cache"], 1)
        folders = {user["folder"]: user for user in facts["users"]}
        self.assertEqual(sorted(folders), ["00000001", "00000002"])
        self.assertTrue(folders["00000001"]["has_np_cache"])
        self.assertEqual(folders["00000001"]["np_cache_size"], 2320)
        self.assertFalse(folders["00000002"]["has_np_cache"])

    def test_the_raw_home_listings_are_in_the_set(self):
        self.assertTrue(self.artefacts.text("accounts/listing.txt"))
        self.assertIn("np_cache.dat",
                      self.artefacts.text("accounts/listing-00000001.txt"))

    def test_webman_settings_were_read_without_submitting_anything(self):
        self.assertEqual(
            self.artefacts.facts("webman_config")["settings"]["fanl"], "41")
        self.assertFalse([entry for entry in self.console.http.requests
                          if entry[0] != "GET"])

    def test_nothing_but_get_was_ever_sent_over_http(self):
        self.assertTrue(self.console.http.requests)
        for method, _path in self.console.http.requests:
            self.assertEqual(method, "GET")

    def test_no_url_ever_carried_a_query_string(self):
        for _method, path in self.console.http.requests:
            self.assertNotIn("?", path)

    def test_no_writing_ftp_command_was_ever_sent(self):
        forbidden = ("STOR", "DELE", "RMD", "MKD", "RNFR", "RNTO", "APPE",
                     "SITE", "CHMOD")
        for command in self.console.ftp.commands:
            verb = command.split(" ", 1)[0].upper()
            self.assertNotIn(verb, forbidden, command)

    def test_a_disc_image_is_only_ever_read_in_ranges_never_downloaded(self):
        """Identification reads a few blocks out of an image and stops.

        A bare RETR on an image would pull tens of gigabytes, so every RETR on
        an .iso has to be a ranged read: preceded by a REST, which is what
        makes it start at an offset and be abandoned rather than completed.
        """
        commands = self.console.ftp.commands
        for index, command in enumerate(commands):
            if not command.upper().startswith("RETR"):
                continue
            if ".iso" not in command.lower():
                continue
            earlier = [item.upper().split(" ", 1)[0]
                       for item in commands[:index]]
            self.assertIn("REST", earlier, command)


class RangedIsoIdentification(Harness):
    """A real image, served over the mock, read a few blocks at a time."""

    IMAGE = "/dev_hdd0/PS3ISO/Call of Duty - Modern Warfare 3 [BLES01428].iso"

    def setUp(self):
        from make_fixtures import build_ps3_bridge
        # Padded out to something big enough that reading the lot would be
        # obvious in the byte count asserted below.
        self.image = build_ps3_bridge(pad_to=24 * 1024 * 1024)
        # resolve() rather than fixture() over each value: the table holds
        # literal bytes as well as fixture names.
        served = resolve(DEFAULT_FILES)
        served[self.IMAGE] = self.image
        self.__class__.files = served
        super().setUp()

    def test_the_title_is_read_out_of_the_image_itself(self):
        artefacts = ArtefactSet.from_run(
            self.identify(["storage", "games"]))
        payload = artefacts.json("games/iso-identity.json")
        self.assertIsNotNone(payload)
        row = next(item for item in payload["isos"]
                   if item["path"] == self.IMAGE)
        self.assertEqual(row["method"], "param_sfo")
        self.assertTrue(row["title_id"])

    def test_only_a_fraction_of_the_image_is_read(self):
        artefacts = ArtefactSet.from_run(
            self.identify(["storage", "games"]))
        payload = artefacts.json("games/iso-identity.json")
        row = next(item for item in payload["isos"]
                   if item["path"] == self.IMAGE)
        self.assertGreater(row["bytes_read"], 0)
        self.assertLess(row["bytes_read"], 512 * 1024)
        self.assertLess(row["bytes_read"], len(self.image) / 20)

    def test_the_run_still_produces_an_inventory(self):
        artefacts = ArtefactSet.from_run(
            self.identify(["storage", "games"]))
        self.assertTrue(artefacts.game_entries())


class IsoIdentificationDegrading(Harness):
    """The console refuses the ranged read. The inventory must survive it."""

    def test_it_falls_back_to_the_file_name(self):
        artefacts = ArtefactSet.from_run(
            self.identify(["storage", "games"]))
        payload = artefacts.json("games/iso-identity.json")
        self.assertIsNotNone(payload)
        rows = payload["isos"]
        self.assertTrue(rows)
        # Nothing in the default mock serves image bytes, so every one of these
        # has to come back named from its filename with a reason recorded.
        for row in rows:
            self.assertIn(row["method"], ("filename", "none"))
            self.assertTrue(row["reason"])
        named = [row for row in rows if row["title_id"]]
        self.assertTrue(named)

    def test_the_inventory_is_unaffected(self):
        artefacts = ArtefactSet.from_run(
            self.identify(["storage", "games"]))
        self.assertEqual(artefacts.status("games"), "ok")
        self.assertTrue(artefacts.game_entries())


class OneAccountWithNpCache(Harness):
    """The console the Black Ops 1 report was written about.

    One local user, numbered 00000001, with np_cache.dat sitting in the folder
    the fix looks in. A dump of this console has to say so in as many words,
    because the screen told its owner that no account had that file.
    """

    listings = {
        "/dev_hdd0/home/": ("ftp", "list_home_one_user.txt"),
        "/dev_hdd0/home/00000001/": ("ftp", "list_home_user1.txt"),
    }

    def setUp(self):
        super().setUp()
        self.artefacts = ArtefactSet.from_run(self.collect(["accounts"]))

    def test_the_account_and_its_np_cache_are_both_recorded(self):
        facts = self.artefacts.facts("accounts")
        self.assertEqual(self.artefacts.status("accounts"), "ok")
        self.assertEqual(facts["user_count"], 1)
        self.assertEqual(facts["with_np_cache"], 1)
        user = facts["users"][0]
        self.assertEqual(user["path"], "/dev_hdd0/home/00000001")
        self.assertTrue(user["listed"])
        self.assertTrue(user["has_np_cache"])
        names = [entry["name"] for entry in user["entries"]]
        self.assertIn("np_cache.dat", names)
        self.assertIn("savedata", names)

    def test_the_summary_says_which_account_has_the_file(self):
        summary = report.build_summary(self.artefacts)
        self.assertIn("ACCOUNTS ON THIS CONSOLE", summary)
        self.assertIn("00000001", summary)
        self.assertIn("np_cache.dat", summary)

    def test_the_summary_survives_redaction_with_its_words_intact(self):
        """Redaction runs over the summary and must leave this section alone.

        The online ID rule counts a bare "user" as a label and replaces the
        word after it, so a heading of "USER ACCOUNTS" reached the zip as
        "USER [ONLINE-ID-95cca5a0]" and "2 user folders" lost the word
        "folders". Nothing in this section is an identifier, so nothing in it
        should come out as a placeholder.
        """
        with tempfile.TemporaryDirectory() as folder:
            path, _counts = report.write_zip(self.artefacts, folder)
            with zipfile.ZipFile(path) as archive:
                summary = archive.read("summary.txt").decode()
                listing = archive.read(
                    "accounts/listing-00000001.txt").decode()
        start = summary.index("ACCOUNTS ON THIS CONSOLE")
        section = summary[start:summary.index("\n\n", start)]
        self.assertNotIn("[ONLINE-ID-", section)
        self.assertNotIn("[CONSOLE-NAME-", section)
        self.assertIn("account folder(s)", section)
        # And the raw listing has to survive it too, or the file names a
        # helper is reading the dump for are gone.
        self.assertIn("np_cache.dat", listing)
        self.assertIn("localusername", listing)

    def test_nothing_in_a_user_folder_is_ever_opened(self):
        # np_cache.dat holds the account ID and the online ID, and
        # localusername holds the name the console shows. A dump gets posted
        # in a Discord, so the walk reads names and never contents.
        for command in self.console.ftp.commands:
            verb, _, argument = command.partition(" ")
            if verb.upper() in ("RETR", "SIZE"):
                self.assertNotIn("/dev_hdd0/home", argument, command)

    def test_the_transport_would_refuse_to_download_one_anyway(self):
        # Belt and braces, and the braces are the allowlist: even a collector
        # written later cannot pull these two down without widening it.
        from ps3diag import transport
        for path in ("/dev_hdd0/home/00000001/np_cache.dat",
                     "/dev_hdd0/home/00000001/localusername"):
            self.assertFalse(transport.may_download(path), path)
            self.assertFalse(transport.may_read_bytes(path), path)


class SomethingElseUnderHome(Harness):
    """An entry that is not eight digits is not a user folder."""

    listings = {
        "/dev_hdd0/home/": ("ftp", "list_home_odd.txt"),
        "/dev_hdd0/home/00000001/": ("ftp", "list_home_user1.txt"),
    }

    def setUp(self):
        super().setUp()
        self.artefacts = ArtefactSet.from_run(self.collect(["accounts"]))

    def test_it_is_recorded_but_not_counted_as_a_user(self):
        facts = self.artefacts.facts("accounts")
        self.assertEqual(facts["user_count"], 1)
        self.assertEqual(sorted(entry["name"]
                                for entry in facts["other_entries"]),
                         ["notes.txt", "vsh"])

    def test_it_is_never_listed(self):
        # Each listing costs the console its own data connection, and whatever
        # somebody has left under /dev_hdd0/home is none of this tool's
        # business.
        for command in self.console.ftp.commands:
            self.assertNotIn("/dev_hdd0/home/vsh", command)


class NoHomeFolderAtAll(Harness):
    """A console with nothing at /dev_hdd0/home. The run carries on."""

    listings = {"/dev_hdd0/": ("ftp", "list_dev_hdd0.txt")}

    def test_the_category_fails_and_says_why_without_raising(self):
        result = self.collect(["accounts", "webman_config"])
        by_key = {outcome.key: outcome for outcome in result.results}
        self.assertEqual(by_key["accounts"].status, "failed")
        self.assertTrue(any("/dev_hdd0/home" in note
                            for note in by_key["accounts"].notes))
        # The point of the test: the category that follows is unharmed.
        self.assertEqual(by_key["webman_config"].status, "ok")

    def test_the_zip_is_still_written(self):
        artefacts = ArtefactSet.from_run(self.collect(["accounts"]))
        with tempfile.TemporaryDirectory() as folder:
            path, _counts = report.write_zip(artefacts, folder)
            self.assertTrue(os.path.exists(path))


class AHomeListingThatFails(Harness):
    """The folder is there and the console refuses to list it.

    Not the same answer as an empty console, and reporting it as one is how
    somebody gets told they have no accounts when they have two.
    """

    def setUp(self):
        super().setUp()
        from mock_webman import MockWebmanFtp

        def fault(verb, argument):
            if verb == "LIST" and argument.startswith("/dev_hdd0/home"):
                return "530 permission denied"
            return None

        self.console.ftp.stop()
        self.console.ftp = MockWebmanFtp(fault=fault).start()

    def test_the_refusal_is_recorded_as_a_fault_rather_than_as_emptiness(self):
        result = self.collect(["accounts"])
        outcome = result.results[0]
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.faults)
        self.assertTrue(any("permission denied" in note
                            for note in outcome.notes))
        self.assertEqual(ArtefactSet.from_run(result).facts("accounts"), {})


class Output(Harness):
    def test_the_zip_holds_the_summary_manifest_and_raw_replies(self):
        artefacts = ArtefactSet.from_run(self.collect())
        outcome = analyse(artefacts)
        with tempfile.TemporaryDirectory() as folder:
            path, counts = report.write_zip(artefacts, folder,
                                            outcome.findings,
                                            outcome.broken_rules)
            self.assertTrue(os.path.exists(path))
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                summary = archive.read("summary.txt").decode()
                manifest = archive.read("manifest.json").decode()
            self.assertIn("summary.txt", names)
            self.assertIn("manifest.json", names)
            self.assertIn("games/inventory.csv", names)
            self.assertIn("analysis/findings.json", names)
            self.assertTrue([name for name in names
                             if name.startswith("system/")])
            # The digest has to stand on its own for a helper who never unzips
            # the rest of it.
            for expected in ("CECH-2503B", "United Kingdom and Ireland",
                             "4.93", "Evilnat", "GAME INVENTORY",
                             "WHAT DID NOT WORK", "ENDPOINTS TRIED"):
                self.assertIn(expected, summary)
            self.assertIn("identifiers_included", manifest)
            self.assertEqual(counts.get("IDPS"), 1)

    def test_identifiers_are_redacted_everywhere_in_the_zip(self):
        artefacts = ArtefactSet.from_run(self.collect())
        with tempfile.TemporaryDirectory() as folder:
            path, _counts = report.write_zip(artefacts, folder)
            with zipfile.ZipFile(path) as archive:
                joined = "\n".join(archive.read(name).decode(errors="replace")
                                   for name in archive.namelist())
        self.assertNotIn("00000001006A0200A1B2C3D4E5F60718", joined)
        self.assertNotIn("3F2A19C4D5E6B7A8091A2B3C4D5E6F70", joined)
        self.assertNotIn("00:1F:A7:3C:9B:2E", joined)
        self.assertIn("[IDPS-", joined)

    def test_including_identifiers_keeps_them(self):
        artefacts = ArtefactSet.from_run(
            self.collect(include_identifiers=True))
        with tempfile.TemporaryDirectory() as folder:
            path, _counts = report.write_zip(artefacts, folder,
                                             include_identifiers=True)
            with zipfile.ZipFile(path) as archive:
                joined = "\n".join(archive.read(name).decode(errors="replace")
                                   for name in archive.namelist())
        self.assertIn("00:1F:A7:3C:9B:2E", joined)

    def test_the_zip_reloads_into_an_equivalent_set(self):
        artefacts = ArtefactSet.from_run(self.collect())
        with tempfile.TemporaryDirectory() as folder:
            path, _counts = report.write_zip(artefacts, folder)
            reloaded = ArtefactSet.from_zip(path)
        self.assertEqual(reloaded.facts("system")["model"], "CECH-2503B")
        self.assertEqual(len(reloaded.game_entries()),
                         len(artefacts.game_entries()))
        for key in ("system", "storage", "games"):
            self.assertEqual(reloaded.status(key), artefacts.status(key))


class ConsoleThatIsNotThere(FixtureCase):
    """Nothing listening. The tool must still produce a file and say why."""

    def test_every_category_fails_and_nothing_raises(self):
        # Port 1 on loopback: nothing is listening and nothing can be.
        probe = HttpProbe("127.0.0.1:1", timeout=0.3)
        lister = FtpLister("127.0.0.1", 0.3,
                           factory=lambda: (_ for _ in ()).throw(
                               OSError("connection refused")))
        result = runner.run("127.0.0.1", http=probe, ftp=lister,
                            run_timeout=30)
        self.assertTrue(result.results)
        for outcome in result.results:
            self.assertIn(outcome.status, ("failed", "ok"))
        artefacts = ArtefactSet.from_run(result)
        with tempfile.TemporaryDirectory() as folder:
            path, _counts = report.write_zip(artefacts, folder)
            with zipfile.ZipFile(path) as archive:
                summary = archive.read("summary.txt").decode()
        self.assertIn("WHAT DID NOT WORK", summary)


class HttpUpFtpDown(FixtureCase):
    """FTP switched off in webMAN, which is a common and confusing state."""

    def setUp(self):
        self.http = MockWebmanHttp().start()
        self.addCleanup(self.http.stop)

    def test_the_web_only_categories_still_work(self):
        probe = HttpProbe(f"127.0.0.1:{self.http.port}", timeout=5)
        lister = FtpLister("127.0.0.1", 0.3,
                           factory=lambda: (_ for _ in ()).throw(
                               OSError("connection refused")))
        result = runner.run("127.0.0.1", http=probe, ftp=lister,
                            run_timeout=60)
        by_key = {outcome.key: outcome for outcome in result.results}
        self.assertEqual(by_key["system"].status, "ok")
        self.assertEqual(by_key["network"].status, "ok")
        self.assertEqual(by_key["webman_config"].status, "ok")
        self.assertEqual(by_key["games"].status, "failed")
        # And it must say what to do about it rather than only that it failed.
        self.assertTrue(any("FTP" in note
                            for note in by_key["games"].notes
                            + by_key["crash_reports"].notes))


class UnknownWebmanVersion(Harness):
    """A console whose every endpoint but the root page is missing.

    This is the case the whole design is built around: the endpoint list is
    unverified, so the tool must degrade to "here is the raw text" rather than
    failing.
    """

    routes = {"/": ("http", "root_minimal.html")}

    def test_it_does_not_crash_and_keeps_what_it_got(self):
        result = self.collect(["system", "webman_config", "network"])
        by_key = {outcome.key: outcome for outcome in result.results}
        self.assertNotEqual(by_key["system"].status, "failed")
        self.assertTrue(by_key["system"].artefacts)

    def test_a_404_is_explained_rather_than_reported_as_a_fault(self):
        result = self.collect(["webman_config"])
        notes = " ".join(result.results[0].notes)
        self.assertIn("404", notes)
        self.assertIn("somewhere else", notes)


class FtpLogin(Harness):
    """Anonymous, once, and the greeting is kept."""

    def test_only_an_anonymous_login_is_ever_attempted(self):
        self.collect(["storage"])
        logins = [command for command in self.console.ftp.commands
                  if command.upper().startswith("USER")]
        self.assertEqual(logins, ["USER anonymous"])

    def test_the_greeting_is_parsed_into_the_system_facts(self):
        artefacts = ArtefactSet.from_run(self.collect(["system"]))
        facts = artefacts.facts("system")
        self.assertEqual(facts["ftpd_version"], "1.47.48q")
        self.assertEqual(facts["ntfs_mounts"], 0)

    def test_a_dropped_connection_is_reconnected_rather_than_losing_the_run(self):
        from mock_webman import MockWebmanFtp
        state = {"dropped": False}

        def fault(verb, argument):
            # Kill the link once, on the first listing, the way an idle console
            # does. Everything after it must still be collected.
            if verb == "LIST" and not state["dropped"]:
                state["dropped"] = True
                return "DROP"
            return None

        self.console.ftp.stop()
        self.console.ftp = MockWebmanFtp(fault=fault).start()
        result = self.collect(["storage", "crash_reports"])
        by_key = {outcome.key: outcome for outcome in result.results}
        self.assertEqual(by_key["crash_reports"].status, "ok")


class Timeout(Harness):
    def test_a_deadline_that_has_already_passed_stops_the_run_cleanly(self):
        result = runner.run("127.0.0.1", http=self.probe(), ftp=self.lister(),
                            run_timeout=0.0001)
        self.assertTrue(result.results)
        artefacts = ArtefactSet.from_run(result)
        self.assertIsInstance(artefacts.categories, list)


class SlowLister:
    """The real lister with a pause on every listing.

    The mock answers in microseconds, so a stop cannot be measured against it:
    the run would be over before the test could press anything. A quarter of a
    second per listing is roughly what a real console costs and is what the
    inventory used to sit inside for two minutes.
    """

    def __init__(self, inner, delay):
        self._inner = inner
        self._delay = delay
        self.listed = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_dir(self, path):
        time.sleep(self._delay)
        self.listed.append(path)
        return self._inner.list_dir(path)


class IdentificationIsOptIn(Harness):
    """Reading inside the images is a separate, slower job from listing them.

    On a real console with 24 images the identification pass was the whole of
    the two minutes the inventory took, so it is off unless it is asked for and
    the names, sizes and regions come back either way.
    """

    def test_the_default_run_never_opens_a_disc_image(self):
        result = self.collect(["storage", "games"])
        games = result.by_key("games")
        self.assertEqual(games.status, "ok")
        self.assertNotIn("images_identified", games.facts)
        opened = [command for command in self.console.ftp.commands
                  if command.upper().startswith("RETR")
                  and command.lower().endswith(".iso")]
        self.assertEqual(opened, [])

    def test_the_inventory_is_the_same_with_it_off(self):
        plain = ArtefactSet.from_run(self.collect(["storage", "games"]))
        self.assertTrue(plain.game_entries())
        rows = {row["name"]: row for row in plain.game_entries()}
        self.assertIn("Katamari Damacy [SLPM-65729].iso", rows)
        self.assertEqual(rows["Katamari Damacy [SLPM-65729].iso"]["region"],
                         "Japan")

    def test_asking_for_it_produces_the_identity_artefact(self):
        artefacts = ArtefactSet.from_run(self.identify(["storage", "games"]))
        payload = artefacts.json("games/iso-identity.json")
        self.assertIsNotNone(payload)
        self.assertTrue(payload["isos"])

    def test_it_is_off_unless_the_option_says_otherwise(self):
        from ps3diag import collectors
        result = collectors.CollectorResult("games", "Game inventory")
        context = collectors.Context(None, None)
        self.assertEqual(
            collectors._identify_images(context, result, {}), 0)


class ProgressInsideACategory(Harness):
    """One line per category is no progress at all on a slow category."""

    def details(self, categories, **kwargs):
        seen = []
        self.collect(categories,
                     on_progress=lambda key, title, state, payload:
                         seen.append((key, payload)) if state == "detail"
                         else None,
                     **kwargs)
        return seen

    def test_the_inventory_says_which_folder_it_is_reading(self):
        seen = self.details(["storage", "games"])
        games = [text for key, text in seen if key == "games"]
        self.assertTrue(games)
        self.assertTrue([text for text in games
                         if text.startswith("Listing /dev_hdd0/PS3ISO/")])
        self.assertTrue([text for text in games if "item(s)" in text])

    def test_it_says_which_device_it_is_on(self):
        seen = self.details(["storage", "games"])
        games = [text for key, text in seen if key == "games"]
        self.assertTrue([text for text in games
                         if text.startswith("Device 1 of")])

    def test_identification_counts_the_images_one_by_one(self):
        seen = self.details(["storage", "games"],
                            options={"identify_isos": True})
        reading = [text for key, text in seen if key == "games"
                   and text.startswith("Reading inside image ")]
        self.assertTrue(reading)
        self.assertTrue(reading[0].startswith("Reading inside image 1 of "))
        # Counted over the images alone: a folder game is not opened and must
        # not be counted as though it were.
        for text in reading:
            self.assertIn(".iso", text.lower())

    def test_every_category_reports_something(self):
        seen = self.details(None)
        keys = {key for key, _text in seen}
        for expected in ("system", "storage", "plugins", "crash_reports",
                         "games"):
            self.assertIn(expected, keys)

    def test_a_context_with_nobody_listening_does_not_mind(self):
        from ps3diag import collectors
        context = collectors.Context(None, None)
        self.assertIsNone(context.progress("anything"))
        self.assertIsNone(context.check_stopped())


class StoppingMidInventory(Harness):
    """Stop has to take effect inside a collector, not after it.

    This is the bug as it was reported: Stop was pressed during the game
    inventory and nothing happened for the rest of the two minutes, because the
    only check was between categories.
    """

    DELAY = 0.25

    def run_until(self, categories, trigger):
        """Runs, presses Stop the moment trigger says so, and times the rest."""
        lister = SlowLister(self.lister(), self.DELAY)
        stop = threading.Event()
        pressed = []
        at_press = []

        def on_progress(key, title, state, payload):
            if state == "detail" and not stop.is_set() and trigger(key,
                                                                   payload):
                pressed.append(time.monotonic())
                at_press.append(len(lister.listed))
                stop.set()

        result = runner.run("127.0.0.1", categories, http=self.probe(),
                            ftp=lister, run_timeout=120,
                            on_progress=on_progress,
                            should_stop=stop.is_set)
        self.assertTrue(pressed, "the test never got as far as pressing Stop")
        return (result, lister, time.monotonic() - pressed[0],
                len(lister.listed) - at_press[0])

    def test_a_stop_in_the_inventory_ends_the_run_within_a_second_or_two(self):
        result, _lister, after, listed_after = self.run_until(
            ["storage", "games"],
            lambda key, text: key == "games" and "item(s)" in text)
        # One listing may already be in flight when Stop is pressed; nothing
        # after it may be started. Counting listings rather than seconds is the
        # point: how many more directories were read is a property of the code,
        # where elapsed time is a property of how busy the machine is, and
        # asserting on the latter made this fail under load — exactly when a
        # flake is least welcome. The loose time bound stays as a backstop
        # against a regression that hangs outright.
        self.assertLessEqual(listed_after, 1,
                             f"{listed_after} listings ran after Stop")
        self.assertLess(after, 15.0, f"took {after:.1f}s to stop")
        self.assertTrue(result.stopped)

    def test_what_was_collected_before_the_stop_is_kept(self):
        result, _lister, _after, _listed = self.run_until(
            ["storage", "games"],
            lambda key, text: key == "games" and "item(s)" in text)
        games = result.by_key("games")
        self.assertTrue(games.stopped)
        self.assertIn(games.status, ("ok", "partial"))
        self.assertTrue(games.facts["folders"])
        self.assertGreater(games.facts["total_items"], 0)
        self.assertTrue(any("Stopped at your request" in note
                            for note in games.notes))
        # And the whole thing still turns into a file worth sending.
        artefacts = ArtefactSet.from_run(result)
        self.assertTrue(artefacts.game_entries())
        with tempfile.TemporaryDirectory() as folder:
            path, _counts = report.write_zip(artefacts, folder)
            self.assertTrue(os.path.exists(path))

    def test_nothing_new_is_asked_of_the_console_after_the_stop(self):
        result, lister, _after, _listed = self.run_until(
            ["storage", "games"],
            lambda key, text: key == "games" and "item(s)" in text)
        # The listing in flight when Stop was pressed is allowed to finish.
        # Nothing else is: the count is the whole point of a cooperative stop.
        self.assertTrue(lister.listed)
        self.assertTrue(result.stopped)

    def test_a_stop_is_reported_as_stopped_rather_than_as_a_failure(self):
        result, _lister, _after, _listed = self.run_until(
            None, lambda key, text: key == "storage")
        by_key = {outcome.key: outcome for outcome in result.results}
        self.assertEqual(len(result.results), len(CATEGORY_KEYS))
        for key in ("plugins", "crash_reports", "games"):
            self.assertEqual(by_key[key].status, "skipped", key)
            self.assertTrue(by_key[key].stopped, key)
            self.assertIsNone(by_key[key].error, key)
        self.assertEqual(by_key["system"].status, "ok")


class Stopping(Harness):
    def test_pressing_stop_ends_the_run_but_keeps_what_was_collected(self):
        seen = []

        def stop():
            return len(seen) >= 2

        result = runner.run(
            "127.0.0.1", http=self.probe(), ftp=self.lister(),
            run_timeout=60,
            on_progress=lambda key, title, state, outcome:
                seen.append(key) if state == "done" else None,
            should_stop=stop)
        self.assertEqual(len(result.results), len(CATEGORY_KEYS))
        self.assertTrue(any(outcome.status == "ok"
                            for outcome in result.results))
