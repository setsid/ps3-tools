"""Parser tests. Every one of these is fixture text in, dict out."""

from support import FixtureCase
from ps3diag import parsers


class HtmlToText(FixtureCase):
    def test_row_breaks_become_newlines(self):
        text = parsers.html_to_text("<tr><td>CPU: 61</td></tr>"
                                    "<tr><td>RSX: 54</td></tr>")
        self.assertEqual(text.splitlines(), ["CPU: 61", "RSX: 54"])

    def test_entities_are_decoded(self):
        self.assertIn("61.0°C",
                      parsers.html_to_text("61.0&deg;C").replace(" ", ""))

    def test_script_and_style_are_dropped(self):
        text = parsers.html_to_text("<script>var x='CPU: 99'</script>CPU: 61")
        self.assertNotIn("99", text)

    def test_none_and_bytes_are_both_accepted(self):
        self.assertEqual(parsers.html_to_text(None), "")
        self.assertIn("hello", parsers.html_to_text(b"<b>hello</b>"))


class Identity(FixtureCase):
    def setUp(self):
        self.text = parsers.html_to_text(self.fixture("http", "root_147.html"))

    def test_reads_webman_and_firmware(self):
        facts = parsers.parse_identity(self.text)
        self.assertEqual(facts["webman_version"], "1.47.48q")
        self.assertEqual(facts["firmware"], "4.93")
        self.assertEqual(facts["firmware_type"], "CEX")

    def test_reads_cfw_build_and_cobra(self):
        facts = parsers.parse_identity(self.text)
        self.assertEqual(facts["cfw_name"], "Evilnat")
        self.assertEqual(facts["cobra_version"], "8.5")

    def test_reads_syscall_uptime_and_clock(self):
        facts = parsers.parse_identity(self.text)
        self.assertEqual(facts["syscall_state"], "enabled")
        self.assertEqual(facts["syscall_number"], "8")
        self.assertEqual(facts["uptime"], "3h 42m")
        self.assertEqual(facts["console_time"], "2026-09-14 14:22:07")

    def test_a_different_layout_gives_the_same_fields(self):
        facts = parsers.parse_identity(
            parsers.html_to_text(self.fixture("http", "root_180.html")))
        self.assertEqual(facts["webman_version"], "1.80.00")
        self.assertEqual(facts["firmware"], "4.91")
        self.assertEqual(facts["firmware_type"], "DEX")
        self.assertEqual(facts["hen_version"], "3.3.0")
        self.assertEqual(facts["syscall_state"], "disabled")

    def test_a_page_with_nothing_on_it_returns_no_guesses(self):
        facts = parsers.parse_identity(
            parsers.html_to_text(self.fixture("http", "root_minimal.html")))
        self.assertNotIn("firmware", facts)
        self.assertNotIn("cobra_version", facts)

    def test_never_raises_on_rubbish(self):
        for value in ("", "\x00\x01", "<<<>>>", "firmware:"):
            self.assertIsInstance(parsers.parse_identity(value), dict)


class Cpursx(FixtureCase):
    def test_celsius_fahrenheit_and_clocks(self):
        facts = parsers.parse_cpursx(
            parsers.html_to_text(self.fixture("http", "cpursx_147.html")))
        self.assertEqual(facts["cpu_temp_c"], 61.0)
        self.assertEqual(facts["rsx_temp_c"], 54.0)
        self.assertEqual(facts["cpu_temp_f"], 141.8)
        self.assertEqual(facts["cpu_clock_mhz"], 3192)
        self.assertEqual(facts["rsx_clock_mhz"], 500)
        self.assertEqual(facts["memory_clock_mhz"], 650)
        self.assertEqual(facts["fan_speed_percent"], 41)
        self.assertEqual(facts["fan_mode"], "manual")

    def test_gigahertz_is_normalised_to_megahertz(self):
        facts = parsers.parse_cpursx(
            parsers.html_to_text(self.fixture("http", "cpursx_alt.html")))
        self.assertEqual(facts["cpu_clock_mhz"], 3200)
        self.assertEqual(facts["rsx_clock_mhz"], 550)
        self.assertEqual(facts["fan_mode"], "syscon")

    def test_fahrenheit_is_never_invented(self):
        facts = parsers.parse_cpursx("CPU: 61 C")
        self.assertNotIn("cpu_temp_f", facts)

    def test_the_firmware_line_is_read_alongside_the_temperatures(self):
        facts = parsers.parse_cpursx(
            parsers.html_to_text(self.fixture("http", "cpursx_147.html")))
        self.assertEqual(facts["cpu_temp_c"], 61.0)
        self.assertEqual(facts["firmware_line"], "SYS: 4.93 CEX")
        self.assertEqual(facts["firmware_version"], "4.93")
        self.assertEqual(facts["firmware_region"], "CEX")
        self.assertEqual(facts["firmware_kind"], "ofw")

    def test_a_blank_cobra_version_never_reaches_the_caller(self):
        # parse_identity supplies these from the whole page and callers merge
        # the two dicts, so an empty one here would wipe a real answer.
        facts = parsers.parse_cpursx("SYS: 4.93 CEX")
        self.assertNotIn("cobra_version", facts)
        self.assertNotIn("hen_version", facts)


class FirmwareLine(FixtureCase):
    """The firmware kind is worked out from the page and never asked for."""

    def read(self, text):
        return parsers.parse_firmware_line(text)

    def test_a_hen_console_is_read_as_hen(self):
        facts = self.read("4.93 CEX PS3HEN 3.5.0")
        self.assertEqual(facts["firmware_kind"], "hen")
        self.assertEqual(facts["firmware_version"], "4.93")
        self.assertEqual(facts["firmware_region"], "CEX")
        self.assertEqual(facts["hen_version"], "3.5.0")
        self.assertEqual(facts["cobra_version"], "")

    def test_a_cobra_console_is_read_as_cfw(self):
        facts = self.read("4.93 CEX Cobra 8.5")
        self.assertEqual(facts["firmware_kind"], "cfw")
        self.assertEqual(facts["firmware_version"], "4.93")
        self.assertEqual(facts["firmware_region"], "CEX")
        self.assertEqual(facts["cobra_version"], "8.5")
        self.assertEqual(facts["hen_version"], "")

    def test_a_plain_line_is_read_as_official_firmware(self):
        facts = self.read("4.93 CEX")
        self.assertEqual(facts["firmware_kind"], "ofw")
        self.assertEqual(facts["firmware_version"], "4.93")
        self.assertEqual(facts["firmware_region"], "CEX")
        self.assertEqual(facts["cobra_version"], "")
        self.assertEqual(facts["hen_version"], "")

    def test_the_raw_line_is_kept_exactly_as_the_console_printed_it(self):
        for line in ("4.93 CEX PS3HEN 3.5.0", "4.93 CEX Cobra 8.5",
                     "SYS: 4.93 CEX", "Firmware: 4.93 CEX (Evilnat Cobra 8.5)",
                     "4.91 DEX | Mamba | HEN 3.3.0"):
            self.assertEqual(self.read(line)["firmware_line"], line)

    def test_the_raw_line_keeps_the_spacing_around_it(self):
        line = "  SYS:   4.93   CEX   Cobra 8.5  "
        facts = self.read("CPU: 61 C\n" + line + "\nRSX: 54 C")
        self.assertEqual(facts["firmware_line"], line)
        self.assertEqual(facts["firmware_kind"], "cfw")

    def test_unexpected_extra_words_still_leave_the_line_readable(self):
        facts = self.read(
            "SYS: 4.93 CEX PS3HEN 3.5.0 [DEBUG] Toolbox 2.02 webMAN 1.47.44")
        self.assertEqual(facts["firmware_kind"], "hen")
        self.assertEqual(facts["firmware_version"], "4.93")
        self.assertEqual(facts["hen_version"], "3.5.0")

    def test_a_webman_version_on_the_line_is_never_read_as_the_firmware(self):
        facts = self.read("webMAN MOD 1.47.48q | 4.93 CEX | Cobra 8.5")
        self.assertEqual(facts["firmware_version"], "4.93")
        self.assertEqual(facts["cobra_version"], "8.5")

    def test_an_empty_page_leaves_the_kind_empty(self):
        facts = self.read("")
        self.assertEqual(facts["firmware_kind"], "")
        self.assertEqual(facts["firmware_line"], "")
        self.assertEqual(facts["firmware_version"], "")
        self.assertEqual(facts["firmware_region"], "")

    def test_a_page_without_a_firmware_line_leaves_the_kind_empty(self):
        facts = self.read(parsers.html_to_text(
            self.fixture("http", "root_minimal.html")))
        self.assertEqual(facts["firmware_kind"], "")
        self.assertEqual(facts["firmware_line"], "")

    def test_a_malformed_line_leaves_the_kind_empty(self):
        for text in ("SYS: CEX 4.93", "SYS: 4.9 CEX", "SYS: ??? ???",
                     "\x00\x01", "<<<>>>", "SYS:"):
            facts = self.read(text)
            self.assertEqual(facts["firmware_kind"], "", text)
            self.assertEqual(facts["firmware_version"], "", text)

    def test_an_unknown_kind_is_told_apart_from_official_firmware(self):
        unknown = self.read("SYS: something the page never explained")
        stock = self.read("4.93 CEX")
        self.assertEqual(unknown["firmware_kind"], "")
        self.assertEqual(stock["firmware_kind"], "ofw")
        self.assertNotEqual(unknown["firmware_kind"], stock["firmware_kind"])

    def test_hybrid_firmware_on_its_own_is_left_unknown(self):
        # HFW is repacked official firmware, so it is neither of the two and
        # there is no honest answer for it among the four.
        facts = self.read("4.90 HFW")
        self.assertEqual(facts["firmware_kind"], "")
        self.assertEqual(facts["firmware_version"], "4.90")
        self.assertEqual(facts["firmware_region"], "")
        self.assertEqual(facts["firmware_line"], "4.90 HFW")

    def test_hen_on_hybrid_firmware_is_read_as_hen(self):
        facts = self.read("4.90 HFW PS3HEN 3.0.3")
        self.assertEqual(facts["firmware_kind"], "hen")
        self.assertEqual(facts["hen_version"], "3.0.3")

    def test_hen_is_reported_even_when_it_brings_its_own_cobra(self):
        # HEN carries a Cobra of its own, so a HEN console names both and the
        # Cobra version there describes HEN.
        facts = self.read("4.91 CEX PS3HEN 3.5.0 Cobra 8.2")
        self.assertEqual(facts["firmware_kind"], "hen")
        self.assertEqual(facts["hen_version"], "3.5.0")
        self.assertEqual(facts["cobra_version"], "8.2")

    def test_a_named_build_is_read_as_cfw(self):
        for line, region in (("4.93 CEX (Evilnat Cobra 8.5)", "CEX"),
                             ("4.84.2 Rebug REX CEX", "CEX"),
                             ("4.88 DEX Ferrox", "DEX"),
                             ("4.87 CEX Habib", "CEX")):
            facts = self.read(line)
            self.assertEqual(facts["firmware_kind"], "cfw", line)
            self.assertEqual(facts["firmware_region"], region, line)

    def test_a_three_part_version_is_kept_whole(self):
        facts = self.read("4.91.2 CEX Cobra 8.4")
        self.assertEqual(facts["firmware_version"], "4.91.2")

    def test_a_debug_console_keeps_its_region(self):
        facts = self.read("4.91 DEX | Mamba | HEN 3.3.0")
        self.assertEqual(facts["firmware_region"], "DEX")
        self.assertEqual(facts["firmware_kind"], "hen")
        self.assertEqual(facts["hen_version"], "3.3.0")
        facts = self.read("4.86 DECR")
        self.assertEqual(facts["firmware_region"], "DECR")

    def test_the_line_that_names_a_payload_is_the_one_kept(self):
        # The root page and the cpursx page are flattened together and both
        # carry a firmware string, so the fuller of the two is the evidence.
        facts = self.read("Firmware: 4.93 CEX (Evilnat Cobra 8.5)\n"
                          "SYS: 4.93 CEX")
        self.assertEqual(facts["firmware_line"],
                         "Firmware: 4.93 CEX (Evilnat Cobra 8.5)")
        self.assertEqual(facts["firmware_kind"], "cfw")
        self.assertEqual(facts["cobra_version"], "8.5")

    def test_a_bare_line_on_a_page_that_names_a_build_is_read_as_cfw(self):
        # A console with its Cobra payload switched off prints a bare line
        # while the rest of the page still says which build it is running.
        facts = self.read("SYS: 4.93 CEX\nEvilnat 1.02\nsyscall8: enabled")
        self.assertEqual(facts["firmware_line"], "SYS: 4.93 CEX")
        self.assertEqual(facts["firmware_kind"], "cfw")

    def test_a_menu_label_is_never_taken_for_what_is_running(self):
        facts = self.read("SYS: 4.93 CEX\nInstall HEN\nCobra: OFF")
        self.assertEqual(facts["firmware_kind"], "ofw")
        self.assertEqual(facts["hen_version"], "")

    def test_the_fixture_pages_are_read_the_same_way(self):
        text = parsers.html_to_text(self.fixture("http", "root_147.html"))
        facts = self.read(text)
        self.assertEqual(facts["firmware_line"],
                         "Firmware: 4.93 CEX (Evilnat Cobra 8.5)")
        self.assertEqual(facts["firmware_kind"], "cfw")
        self.assertEqual(facts["cobra_version"], "8.5")
        text = parsers.html_to_text(self.fixture("http", "root_180.html"))
        facts = self.read(text)
        self.assertEqual(facts["firmware_kind"], "hen")
        self.assertEqual(facts["hen_version"], "3.3.0")
        self.assertEqual(facts["firmware_region"], "DEX")

    def test_every_field_comes_back_whatever_the_page_said(self):
        keys = {"firmware_line", "firmware_version", "firmware_region",
                "firmware_kind", "cobra_version", "hen_version"}
        for text in ("", "4.93 CEX PS3HEN 3.5.0", "rubbish", "\x00"):
            self.assertEqual(set(self.read(text)), keys, text)

    def test_never_raises_on_rubbish(self):
        for value in ("", None, "\x00\x01", "<<<>>>", "firmware:",
                      "4.93", "CEX", "4.93.4.93.4.93 CEX CEX"):
            self.assertIsInstance(self.read(value), dict)


class RunningTitle(FixtureCase):
    def read(self, text):
        return parsers.parse_running_title(text)

    def test_the_game_label_on_its_own_names_the_running_title(self):
        self.assertEqual(self.read("Game: BLES01031"), "BLES01031")

    def test_the_running_label_with_the_name_in_front_of_the_id(self):
        self.assertEqual(
            self.read("Running: Call of Duty: Black Ops (BLES01031)"),
            "BLES01031")

    def test_a_playing_line_with_the_id_in_brackets_is_read(self):
        self.assertEqual(self.read("Playing: Uncharted 3 [BCES01175]"),
                         "BCES01175")
        self.assertEqual(self.read("Now playing: NPEB02143"), "NPEB02143")

    def test_the_process_and_pid_wording_on_a_cpursx_page_is_read(self):
        text = ("CPU: 61.0 C\n"
                "PID: 0x0000001A Process: BLES01031 Black Ops\n")
        self.assertEqual(self.read(text), "BLES01031")

    def test_a_title_id_written_with_a_dash_comes_back_normalised(self):
        self.assertEqual(self.read("Game ID: bles-01031"), "BLES01031")

    def test_a_console_sitting_on_the_xmb_reports_no_game(self):
        for line in ("Game: XMB", "Running: none", "Playing: -",
                     "Game: /dev_hdd0/game"):
            self.assertEqual(self.read(line), "", line)

    def test_a_page_that_says_nothing_about_a_game_reports_no_game(self):
        for name in ("root_147.html", "root_180.html", "cpursx_147.html",
                     "root_minimal.html"):
            text = parsers.html_to_text(self.fixture("http", name))
            self.assertEqual(self.read(text), "", name)

    def test_a_malformed_title_id_is_never_reported(self):
        # Four digits and six digits. Either is a line this tool cannot read
        # rather than a game, and half of an identifier patches nothing.
        self.assertEqual(self.read("Game: BLES0103"), "")
        self.assertEqual(self.read("Game: BLES010311"), "")

    def test_something_shaped_like_a_title_id_is_not_taken_for_one(self):
        # Four letters and five digits, and still not an identifier any PS3
        # game has ever had.
        self.assertEqual(self.read("Game: ABCD12345"), "")
        self.assertEqual(self.read("Running: CECH25030"), "")

    def test_a_title_id_with_no_label_in_front_of_it_is_left_alone(self):
        # The disc in the tray and a link that would start a game both say
        # what is installed rather than what the console has loaded.
        self.assertEqual(self.read("dev_bdvd BLES01031"), "")
        self.assertEqual(self.read("/play.ps3?BLES01031"), "")

    def test_two_games_named_on_one_page_report_neither_of_them(self):
        # A list of what is installed, and nothing here can say which of them
        # is running, so a guess would be right half the time at best.
        self.assertEqual(self.read("Game: BLES01031\nGame: BLUS40015"), "")

    def test_the_word_games_is_not_the_label(self):
        self.assertEqual(self.read("Games: 42 BLES01031"), "")

    def test_never_raises_on_rubbish(self):
        for value in ("", None, "\x00\x01", "<<<>>>", "Game:", "Game: "):
            self.assertEqual(self.read(value), "")


class Storage(FixtureCase):
    def test_free_and_total_with_used_derived(self):
        devices = parsers.parse_storage(
            parsers.html_to_text(self.fixture("http", "root_147.html")))
        by_name = {device["device"]: device for device in devices}
        self.assertEqual(by_name["dev_hdd0"]["free"], "412.7 GB")
        self.assertEqual(by_name["dev_hdd0"]["total"], "931.5 GB")
        self.assertAlmostEqual(by_name["dev_hdd0"]["used_percent"], 55.7,
                               places=1)

    def test_a_device_with_no_disc_is_recorded_not_dropped(self):
        devices = parsers.parse_storage(
            parsers.html_to_text(self.fixture("http", "root_147.html")))
        by_name = {device["device"]: device for device in devices}
        self.assertIn("dev_bdvd", by_name)
        self.assertNotIn("free_bytes", by_name["dev_bdvd"])

    def test_slash_separated_layout_also_parses(self):
        devices = parsers.parse_storage(
            parsers.html_to_text(self.fixture("http", "root_180.html")))
        by_name = {device["device"]: device for device in devices}
        self.assertEqual(by_name["dev_usb001"]["total"], "465.8 GB")


class Network(FixtureCase):
    def test_all_fields(self):
        facts = parsers.parse_network(
            parsers.html_to_text(self.fixture("http", "network.html")))
        self.assertEqual(facts["ip_address"], "192.168.1.42")
        self.assertEqual(facts["gateway"], "192.168.1.1")
        self.assertEqual(facts["dns_secondary"], "8.8.8.8")
        self.assertEqual(facts["mac_address"], "00:1F:A7:3C:9B:2E")
        self.assertEqual(facts["mtu"], "1500")
        self.assertEqual(facts["link_speed"], "100 Mbps Full Duplex")

    def test_empty_input_is_empty_output(self):
        self.assertEqual(parsers.parse_network(""), {})


class Setup(FixtureCase):
    def test_checkboxes_text_and_select(self):
        settings = parsers.parse_setup(self.fixture("http", "setup.html"))
        self.assertEqual(settings["fanc"], "on")
        self.assertEqual(settings["usb0"], "off")
        self.assertEqual(settings["fanl"], "41")
        self.assertEqual(settings["lang"], "en")

    def test_buttons_and_passwords_are_not_settings(self):
        settings = parsers.parse_setup(
            '<input type="submit" name="go" value="Save">'
            '<input type="password" name="pw" value="hunter2">'
            '<input type="text" name="keep" value="1">')
        self.assertEqual(settings, {"keep": "1"})


class FtpList(FixtureCase):
    def entries(self, name):
        return parsers.parse_ftp_list(self.fixture("ftp", name))

    def test_names_with_ampersands_commas_and_apostrophes_survive(self):
        entries, unparsed = self.entries("list_ps3iso.txt")
        self.assertEqual(unparsed, [])
        names = [entry["name"] for entry in entries]
        self.assertIn(
            "Gran Turismo 5 & Prologue, Collector's Edition [BCES00569].iso",
            names)
        self.assertIn("Ratchet & Clank - A Crack in Time [BCES00511].iso",
                      names)

    def test_sizes_and_kinds(self):
        entries, _unparsed = self.entries("list_ps3iso.txt")
        by_name = {entry["name"]: entry for entry in entries}
        self.assertEqual(
            by_name["Call of Duty - Modern Warfare 3 [BLES01428].iso"]["size"],
            25769803776)
        self.assertEqual(by_name["_incoming"]["kind"], "directory")

    def test_a_year_in_place_of_a_time_still_parses(self):
        entries, _unparsed = self.entries("list_ps3iso.txt")
        names = [entry["name"] for entry in entries]
        self.assertIn("The Last of Us [BCES01584].iso", names)

    def test_internal_and_trailing_spaces_are_kept_exactly(self):
        entries, _unparsed = self.entries("list_odd.txt")
        names = [entry["name"] for entry in entries]
        self.assertIn("two   spaces   inside.txt", names)
        self.assertIn("trailing space .txt", names)
        self.assertIn("100% & more, done.txt", names)

    def test_symlink_target_is_split_off_the_name(self):
        entries, _unparsed = self.entries("list_odd.txt")
        link = next(entry for entry in entries if entry["kind"] == "link")
        self.assertEqual(link["name"], "link_to_games")
        self.assertEqual(link["link_target"], "/dev_hdd0/GAMES")

    def test_unreadable_lines_are_handed_back_not_dropped(self):
        _entries, unparsed = self.entries("list_odd.txt")
        self.assertEqual(len(unparsed), 2)
        self.assertTrue(any("garbage" in line for line in unparsed))

    def test_the_total_line_is_ignored(self):
        _entries, unparsed = self.entries("list_odd.txt")
        self.assertFalse(any(line.startswith("total") for line in unparsed))

    def test_an_empty_listing_is_empty_not_an_error(self):
        entries, unparsed = self.entries("list_empty.txt")
        self.assertEqual((entries, unparsed), ([], []))


class BootPlugins(FixtureCase):
    def test_order_enabled_state_and_paths_with_spaces(self):
        plugins = parsers.parse_boot_plugins(
            self.fixture("text", "boot_plugins.txt"))
        self.assertEqual(len(plugins), 5)
        self.assertTrue(plugins[0]["enabled"])
        self.assertEqual(plugins[0]["name"], "webftp_server.sprx")
        disabled = [item for item in plugins if not item["enabled"]]
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0]["name"], "disabled_example.sprx")
        self.assertIn("Iris Manager & tools",
                      [item["path"] for item in plugins][3])

    def test_a_prose_comment_is_not_reported_as_a_plugin(self):
        plugins = parsers.parse_boot_plugins(
            "# webMAN MOD boot plugins\n/dev_hdd0/plugins/a.sprx\n")
        self.assertEqual(len(plugins), 1)


class VersionTxt(FixtureCase):
    def test_release_and_build(self):
        facts = parsers.parse_version_txt(self.fixture("text", "version.txt"))
        self.assertEqual(facts["release"], "4.93")
        self.assertEqual(facts["build"], "92009")
        self.assertIn("cex", facts["flags"])


class Signature(FixtureCase):
    def test_webman_pages_are_recognised(self):
        for name in ("root_147.html", "root_180.html", "root_minimal.html"):
            self.assertTrue(parsers.looks_like_webman(
                self.fixture("http", name)), name)

    def test_a_router_login_page_is_not(self):
        self.assertFalse(parsers.looks_like_webman(
            self.fixture("http", "not_webman.html")))

    def test_an_empty_body_is_not(self):
        self.assertFalse(parsers.looks_like_webman(""))


class LocalAddresses(FixtureCase):
    def test_ipconfig_output(self):
        found = parsers.parse_ipconfig(self.fixture("net",
                                                    "ipconfig_windows.txt"))
        self.assertIn("192.168.1.23", found)
        self.assertIn("172.28.128.1", found)

    def test_ip_addr_output_drops_loopback(self):
        found = parsers.parse_ip_addr(self.fixture("net", "ip_addr_linux.txt"))
        self.assertIn("192.168.1.23", found)
        self.assertNotIn("127.0.0.1", found)


class WhichAdapterLeadsAnywhere(FixtureCase):
    """The default gateway per adapter, which is what tells the real LAN from
    a Hyper-V or VMware adapter. Nothing here sends anything."""

    def test_the_failing_machine_adapters_bring_their_gateways(self):
        adapters = parsers.parse_ipconfig_adapters(
            self.fixture("net", "ipconfig_failing_machine.txt"))
        found = {adapter["addresses"][0]: adapter["gateway"]
                 for adapter in adapters if adapter["addresses"]}
        # The real LAN has one; every virtual adapter on that PC has none.
        self.assertEqual(found["192.168.50.10"], "192.168.50.1")
        self.assertEqual(found["192.168.140.1"], "")
        self.assertEqual(found["172.19.16.1"], "")
        self.assertEqual(found["172.29.64.1"], "")
        self.assertEqual(found["192.168.56.1"], "")

    def test_the_ipv4_gateway_under_the_ipv6_one_is_still_read(self):
        # ipconfig prints the IPv6 gateway on the label line and wraps the
        # IPv4 one onto the line below it with no label of its own.
        text = ("Ethernet adapter Ethernet:\n\n"
                "   IPv4 Address. . . . . . . . . . . : 192.168.50.10\n"
                "   Default Gateway . . . . . . . . . : fe80::1%12\n"
                "                                       192.168.50.1\n")
        adapters = parsers.parse_ipconfig_adapters(text)
        self.assertEqual(adapters[0]["gateway"], "192.168.50.1")

    def test_an_adapter_with_no_gateway_line_has_no_gateway(self):
        adapters = parsers.parse_ipconfig_adapters(
            self.fixture("net", "ipconfig_windows.txt"))
        gateways = [adapter["gateway"] for adapter in adapters]
        self.assertIn("192.168.1.1", gateways)
        self.assertIn("", gateways)

    def test_the_banner_and_a_disconnected_adapter_are_not_interfaces(self):
        adapters = parsers.parse_ipconfig_adapters(
            self.fixture("net", "ipconfig_failing_machine.txt"))
        names = [adapter["name"] for adapter in adapters]
        self.assertNotIn("Windows IP Configuration", names)
        self.assertNotIn("Wireless LAN adapter Wi-Fi", names)

    def test_ip_addr_is_grouped_by_device(self):
        devices = parsers.parse_ip_addr_devices(
            self.fixture("net", "ip_addr_multi.txt"))
        found = {device["name"]: device["addresses"] for device in devices}
        self.assertEqual(found["enp3s0"], ["192.168.50.10"])
        self.assertEqual(found["virbr0"], ["192.168.140.1"])
        self.assertNotIn("lo", found)

    def test_the_default_route_names_its_device(self):
        routes = parsers.parse_ip_route_default(
            self.fixture("net", "ip_route_default.txt"))
        self.assertEqual(routes, [{"device": "enp3s0",
                                   "gateway": "192.168.50.1"}])

    def test_a_route_with_no_via_is_still_a_default_route(self):
        routes = parsers.parse_ip_route_default("default dev tun0 scope link")
        self.assertEqual(routes, [{"device": "tun0", "gateway": ""}])

    def test_no_default_route_at_all(self):
        self.assertEqual(parsers.parse_ip_route_default(""), [])
        self.assertEqual(parsers.parse_ip_route_default(
            "192.168.50.0/24 dev enp3s0 proto kernel scope link"), [])


class Sizes(FixtureCase):
    def test_human_size(self):
        self.assertEqual(parsers.human_size(0), "0 B")
        self.assertEqual(parsers.human_size(1024), "1.0 KB")
        self.assertEqual(parsers.human_size(25769803776), "24.0 GB")
        self.assertEqual(parsers.human_size(None), "")
