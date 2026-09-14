"""Region code tests: title IDs and console model numbers."""

from support import FixtureCase
from ps3diag import regioncodes


class TitleIds(FixtureCase):
    def test_ps3_disc_regions(self):
        for code, region, standard in (
                ("BLES01428", "Europe", "PAL"),
                ("BLUS31140", "North America", "NTSC-U"),
                ("BLJM57001", "Japan", "NTSC-J"),
                ("BCES00569", "Europe", "PAL")):
            info = regioncodes.describe_title_id(code)
            self.assertEqual(info["region"], region, code)
            self.assertEqual(info["video_standard"], standard, code)
            self.assertEqual(info["platform"], "PS3 disc", code)

    def test_sony_and_third_party_are_told_apart(self):
        self.assertEqual(
            regioncodes.describe_title_id("BCES00569")["publisher_class"],
            "Sony published")
        self.assertEqual(
            regioncodes.describe_title_id("BLES01428")["publisher_class"],
            "third party published")

    def test_psn_titles(self):
        info = regioncodes.describe_title_id("NPEB02143")
        self.assertEqual(info["platform"], "PS3 digital")
        self.assertEqual(info["region"], "Europe")
        self.assertEqual(info["media"], "download")

    def test_ps2_p_means_japan_not_pal(self):
        info = regioncodes.describe_title_id("SLPM65729")
        self.assertEqual(info["region"], "Japan")
        self.assertEqual(info["video_standard"], "NTSC-J")

    def test_ps2_europe_and_america(self):
        self.assertEqual(
            regioncodes.describe_title_id("SCES53326")["region"], "Europe")
        self.assertEqual(
            regioncodes.describe_title_id("SLUS21782")["region"],
            "North America")

    def test_psp(self):
        info = regioncodes.describe_title_id("ULES00089")
        self.assertEqual(info["platform"], "PSP")
        self.assertEqual(info["region"], "Europe")

    def test_an_unknown_code_gives_no_region_rather_than_a_guess(self):
        info = regioncodes.describe_title_id("ZZZZ99999")
        self.assertIsNone(info["region"])
        self.assertIsNone(info["platform"])

    def test_none_and_empty(self):
        self.assertIsNone(regioncodes.describe_title_id(None)["region"])
        self.assertIsNone(regioncodes.describe_title_id("")["region"])


class FindingIdsInNames(FixtureCase):
    def test_square_brackets_round_brackets_and_dashes(self):
        cases = {
            "Call of Duty - Modern Warfare 3 [BLES01428].iso": "BLES01428",
            "Gran Turismo 5 (BCES00569).iso": "BCES00569",
            "Persona 4 [SLUS-21782].iso": "SLUS21782",
            "NPEB02143-SomeGame": "NPEB02143",
            "BLUS31140": "BLUS31140",
        }
        for name, expected in cases.items():
            self.assertEqual(regioncodes.find_title_id(name), expected, name)

    def test_names_with_ampersands_and_commas(self):
        self.assertEqual(
            regioncodes.find_title_id(
                "Ico & Shadow of the Colossus, Vol 2 [BLES01481].iso"),
            "BLES01481")

    def test_a_plain_folder_name_has_none(self):
        self.assertIsNone(regioncodes.find_title_id("Random Folder Name"))
        self.assertIsNone(regioncodes.find_title_id(""))


class Models(FixtureCase):
    def test_uk_slim(self):
        info = regioncodes.describe_model("PS3 Slim CECH-2503B")
        self.assertEqual(info["model"], "CECH-2503B")
        self.assertEqual(info["generation"], "slim")
        self.assertEqual(info["sales_region"], "United Kingdom and Ireland")
        self.assertIn("250 GB", info["shipped_capacity"])

    def test_super_slim(self):
        self.assertEqual(
            regioncodes.describe_model("CECH-4003C")["generation"],
            "super slim")

    def test_launch_console_ps2_compatibility(self):
        self.assertIn(
            "hardware",
            regioncodes.describe_model("CECHA00")["ps2_compatibility"])
        self.assertIn(
            "emulation",
            regioncodes.describe_model("CECHC04")["ps2_compatibility"])

    def test_no_model_in_the_text(self):
        self.assertIsNone(regioncodes.describe_model("nothing here")["model"])
        self.assertIsNone(regioncodes.describe_model("")["model"])


class Tally(FixtureCase):
    def test_counts_and_unknowns(self):
        counts, unknown = regioncodes.tally_regions([
            "a [BLES01428]", "b [BLUS30670]", "c [BLES01718]", "d nothing"])
        self.assertEqual(counts["Europe"], 2)
        self.assertEqual(counts["North America"], 1)
        self.assertEqual(unknown, 1)
