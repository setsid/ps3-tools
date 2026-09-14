"""The read-only guarantee.

These are the tests that matter most in this repo. Everything else being wrong
produces a bad report; this being wrong writes to somebody's console.
"""

from support import FixtureCase
from ps3diag import transport


class Allowed(FixtureCase):
    def test_the_pages_the_collectors_need(self):
        for path in ("/", "/index.ps3", "/cpursx.ps3", "/setup.ps3"):
            self.assertEqual(transport.assert_safe_path(path), path)

    def test_directory_listings_on_every_device(self):
        for path in ("/dev_hdd0/", "/dev_hdd0/PS3ISO", "/dev_usb000/GAMES",
                     "/dev_usb1/", "/dev_ntfs0/PS3ISO"):
            self.assertEqual(transport.assert_safe_path(path), path)

    def test_listing_paths_with_awkward_characters(self):
        path = "/dev_usb000/GAMES/Ratchet & Clank (2009), Collector's Ed."
        self.assertEqual(transport.assert_safe_path(path), path)


class Refused(FixtureCase):
    def refuse(self, path):
        with self.assertRaises(transport.UnsafeRequest, msg=path):
            transport.assert_safe_path(path)

    def test_anything_that_acts_on_the_console(self):
        for path in ("/mount.ps3", "/unmount.ps3", "/delete.ps3",
                     "/restart.ps3", "/shutdown.ps3", "/rebuild.ps3",
                     "/eject.ps3", "/install.ps3", "/format.ps3",
                     "/copy.ps3", "/rename.ps3", "/play.ps3"):
            self.refuse(path)

    def test_syscall8_is_never_fetched(self):
        # On several builds fetching this toggles the syscall state rather than
        # reporting it, so it is denied outright rather than being trusted.
        self.refuse("/syscall8.ps3")
        self.refuse("/syscall.ps3")

    def test_dev_blind_is_never_touched(self):
        self.refuse("/dev_blind/")
        self.refuse("/dev_blind/vsh")

    def test_a_query_string_is_never_sent(self):
        # webMAN takes its instructions in the query string, so a path with one
        # is refused even when the path itself is on the allowlist.
        self.refuse("/setup.ps3?fanc=0")
        self.refuse("/cpursx.ps3?up")
        self.refuse("/?mount=1")

    def test_path_traversal(self):
        self.refuse("/../etc/passwd")
        self.refuse("/dev_hdd0/../../dev_flash")

    def test_endpoints_a_real_console_answered_501_to_are_gone(self):
        # Both were guesses from the naming convention. A real webMAN 1.47.48q
        # answers 501 to each, so they are not requested at all any more.
        self.refuse("/sysinfo.ps3")
        self.refuse("/info.ps3")

    def test_paths_not_on_the_allowlist(self):
        self.refuse("/popup.ps3")
        self.refuse("/anything.ps3")
        self.refuse("/dev_flash/")

    def test_not_a_rooted_path(self):
        self.refuse("dev_hdd0")
        self.refuse("http://192.168.1.42/")
        self.refuse(None)
        self.refuse(42)


class Downloads(FixtureCase):
    def test_only_small_text_files_may_be_fetched(self):
        for path in ("/dev_hdd0/crash_report/core.txt",
                     "/dev_hdd0/boot_plugins.txt",
                     "/dev_hdd0/boot_plugins_nocobra.txt",
                     "/dev_flash/vsh/etc/version.txt"):
            self.assertTrue(transport.may_download(path), path)

    def test_game_images_are_never_downloaded(self):
        for path in ("/dev_hdd0/PS3ISO/game.iso",
                     "/dev_hdd0/GAMES/BLES01428/USRDIR/EBOOT.BIN",
                     "/dev_flash/vsh/module/vsh.self",
                     "/dev_hdd0/crash_report/sub/dir/file.txt"):
            self.assertFalse(transport.may_download(path), path)


class ByteReads(FixtureCase):
    """A small binary read is its own permission, narrower than a download."""

    def test_param_sfo_may_be_read_verbatim(self):
        for path in ("/dev_hdd0/game/BLES01717/PARAM.SFO",
                     "/dev_hdd0/game/BLES01428/USRDIR/PARAM.SFO"):
            self.assertTrue(transport.may_read_bytes(path), path)

    def test_nothing_else_may(self):
        for path in ("/dev_hdd0/game/BLES01717/USRDIR/EBOOT.BIN",
                     "/dev_hdd0/PS3ISO/game.iso",
                     "/dev_flash/vsh/module/vsh.self",
                     "/dev_hdd0/game/NOTATITLE/PARAM.SFO"):
            self.assertFalse(transport.may_read_bytes(path), path)

    def test_it_does_not_widen_the_text_download_allowlist(self):
        self.assertFalse(
            transport.may_download("/dev_hdd0/game/BLES01717/PARAM.SFO"))


class NonAsciiFtp(FixtureCase):
    """The bug that broke every collector against a real console.

    webMANftpd puts a raw 0xb0 in the status reply it sends before a data
    transfer. ftplib has decoded the control connection as strict UTF-8 since
    Python 3.9, so every LIST died with "'utf-8' codec can't decode byte 0xb0"
    and four collectors reported nothing. The mock reproduces the byte at the
    same offsets the console produced.
    """

    def lister(self, **kwargs):
        import ftplib
        from mock_webman import MockWebmanFtp
        server = MockWebmanFtp(**kwargs).start()
        self.addCleanup(server.stop)

        def factory():
            ftp = ftplib.FTP()
            ftp.connect("127.0.0.1", server.port, timeout=5)
            ftp.login("anonymous", "anonymous@")
            return ftp

        listing = transport.FtpLister("127.0.0.1", 5, factory=factory)
        self.addCleanup(listing.close)
        return listing

    def test_a_listing_survives_the_status_byte(self):
        from ps3diag.parsers import parse_ftp_list
        entries, unparsed = parse_ftp_list(
            self.lister().list_dir("/dev_hdd0/"))
        self.assertEqual(unparsed, [])
        self.assertTrue(entries)

    def test_the_byte_lands_where_the_real_console_put_it(self):
        # /dev_hdd0/ produced "position 41" on the console. If the mock stops
        # matching that, it has stopped reproducing the fault.
        from mock_webman import TRANSFER_REPLY
        reply = TRANSFER_REPLY.format(path="/dev_hdd0/")
        self.assertEqual(reply.index("\xb0"), 41)
        self.assertEqual(
            TRANSFER_REPLY.format(path="/dev_hdd0/PS3ISO/").index("\xb0"), 48)
        self.assertEqual(
            TRANSFER_REPLY.format(
                path="/dev_hdd0/crash_report/").index("\xb0"), 54)

    def test_filenames_that_are_not_utf_8_come_back_readable(self):
        from ps3diag.parsers import parse_ftp_list
        listing = self.lister(
            listings={"/dev_hdd0/": ("ftp", "list_non_utf8.txt")})
        entries, unparsed = parse_ftp_list(listing.list_dir("/dev_hdd0/"))
        names = [entry["name"] for entry in entries]
        self.assertEqual(unparsed, [])
        # A latin-1 filename, a latin-1 degree sign mid-name, and a genuinely
        # UTF-8 one all have to survive the same round trip.
        self.assertIn("Caf\u00e9 Racer [BLES01234].iso", names)
        self.assertIn("Temperature 40\u00b0C log.txt", names)
        self.assertIn("Caf\u00e9 properly encoded.iso", names)

    def test_a_well_behaved_server_still_works(self):
        listing = self.lister(quirks=False)
        self.assertTrue(listing.list_dir("/dev_hdd0/").splitlines())

    def test_force_byte_safe_rebuilds_the_reader_not_just_the_attribute(self):
        # Setting ftp.encoding alone is not enough: ftplib builds its reader
        # once, in connect(), and a connection handed over by somebody else is
        # already wrapped in a strict decoder.
        import ftplib
        from mock_webman import MockWebmanFtp
        server = MockWebmanFtp().start()
        self.addCleanup(server.stop)
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", server.port, timeout=5)
        ftp.login("anonymous", "anonymous@")
        self.assertEqual(ftp.file.encoding.lower().replace("-", ""), "utf8")
        transport.force_byte_safe(ftp)
        self.assertEqual(ftp.encoding, "latin-1")
        self.assertEqual(ftp.file.encoding.lower().replace("-", ""),
                         "latin1")
        ftp.close()


class Recoding(FixtureCase):
    def test_latin_1_round_trips_to_real_text(self):
        from ps3diag.parsers import recode_ftp_line
        raw = "Caf\u00c3\u00a9"          # UTF-8 bytes read as latin-1
        self.assertEqual(recode_ftp_line(raw), "Caf\u00e9")

    def test_a_line_that_is_not_utf_8_keeps_its_latin_1_reading(self):
        from ps3diag.parsers import recode_ftp_line
        self.assertEqual(recode_ftp_line("40\u00b0C"), "40\u00b0C")

    def test_it_never_raises(self):
        from ps3diag.parsers import recode_ftp_line
        for value in ("", None, 42, "plain"):
            recode_ftp_line(value)


class ProbeBehaviour(FixtureCase):
    def test_try_all_skips_a_refused_path_and_keeps_the_rest(self):
        class Recording(transport.HttpProbe):
            def __init__(self):
                super().__init__("127.0.0.1")
                self.asked = []

            def get(self, path):
                self.asked.append(path)
                return transport.Response(path, 200, "body")

        probe = Recording()
        responses = probe.try_all(["/", "/mount.ps3", "/cpursx.ps3"])
        self.assertEqual(probe.asked, ["/", "/cpursx.ps3"])
        self.assertEqual(len(responses), 2)

    def test_get_refuses_before_opening_a_socket(self):
        # No opener is installed, so if the guard did not fire first this would
        # attempt a real connection. It must raise instead.
        probe = transport.HttpProbe("192.168.1.42")
        with self.assertRaises(transport.UnsafeRequest):
            probe.get("/mount.ps3")
        self.assertEqual(probe.attempts, [])


class Redirects(FixtureCase):
    def test_redirects_are_not_followed(self):
        handler = transport.NoRedirects()
        self.assertIsNone(handler.redirect_request(
            None, None, 302, "Found", {}, "http://elsewhere/"))
