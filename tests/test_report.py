"""The digest, rendered from an artefact set with no console anywhere near it."""

import json
import os
import tempfile
import zipfile

from support import FixtureCase, fixture_path
from ps3diag import report
from ps3diag.artefacts import ArtefactSet
from ps3diag.findings import Finding


def saved():
    return ArtefactSet.from_zip(fixture_path("sample-diagnostic.zip"))


class Digest(FixtureCase):
    def setUp(self):
        self.text = report.build_summary(saved())

    def test_it_leads_with_the_findings(self):
        # Findings come before the raw data, so somebody who reads only the
        # first screen already knows whether anything is wrong.
        for later in ("\nWHAT WAS COLLECTED\n", "\nCONSOLE\n",
                      "\nGAME INVENTORY\n", "\nENDPOINTS TRIED\n"):
            self.assertLess(self.text.index("\nWHAT THIS LOOKS LIKE\n"),
                            self.text.index(later), later)

    def test_it_says_it_only_reads(self):
        self.assertIn("ONLY READS", self.text)

    def test_the_console_is_described_including_its_region(self):
        for expected in ("CECH-2503B", "United Kingdom and Ireland",
                         "4.93", "Evilnat", "Cobra 8.5", "slim"):
            self.assertIn(expected, self.text)

    def test_sales_region_and_firmware_region_are_not_conflated(self):
        self.assertIn("where the console was sold", self.text)

    def test_the_inventory_is_broken_down_by_region(self):
        self.assertIn("By region, across everything found:", self.text)
        self.assertIn("Europe (PAL)", self.text)

    def test_region_locking_is_explained(self):
        # Compared against the unwrapped text: these paragraphs are folded to
        # fit a terminal, so the sentence spans lines.
        flowed = " ".join(self.text.split())
        self.assertIn("PS3 game discs are not region locked", flowed)
        self.assertIn("PS2 and PSOne discs are region locked", flowed)

    def test_no_file_is_listed_twice(self):
        start = self.text.index("FILES IN THIS ZIP")
        listed = [line.strip() for line in self.text[start:].splitlines()
                  if line.startswith("  ") and line.strip()]
        self.assertEqual(len(listed), len(set(listed)))

    def test_every_section_is_present(self):
        for heading in ("WHAT WAS COLLECTED", "CONSOLE",
                        "TEMPERATURES, CLOCKS AND FAN", "STORAGE",
                        "GAME INVENTORY", "REGION NOTES", "PLUGINS",
                        "CRASH REPORTS", "NETWORK, AS THE CONSOLE REPORTS IT",
                        "WEBMAN CONFIGURATION", "WHAT DID NOT WORK",
                        "ENDPOINTS TRIED", "FILES IN THIS ZIP"):
            self.assertIn(heading, self.text)

    def test_a_404_is_explained_as_normal(self):
        self.assertIn("normal and not a fault", self.text)

    def test_nothing_runs_off_the_side_of_the_page(self):
        # summary.txt gets read in Notepad, which does not wrap. Checked
        # against the findings too, whose text comes from the rules rather
        # than from this module and is the part most likely to be long.
        from ps3diag.analysis import analyse
        artefacts = saved()
        outcome = analyse(artefacts)
        rendered = report.build_summary(artefacts, outcome.findings,
                                        outcome.broken_rules)
        self.assertTrue(outcome.findings, "the sample should produce findings")
        for line in (self.text + rendered).splitlines():
            self.assertLessEqual(len(line), 100, line)

    def test_no_emoji_anywhere(self):
        for character in self.text:
            self.assertLess(ord(character), 0x2190, repr(character))


class FindingsRendering(FixtureCase):
    def findings(self):
        return [
            Finding("a-problem", "error", "Something is broken",
                    "The plain English explanation.", "Do this about it.",
                    ["the evidence"], "storage"),
            Finding("a-warning", "warn", "Something is odd",
                    "Another explanation.", "", [], "system"),
        ]

    def test_severity_grouping_and_wording(self):
        text = report.build_summary(saved(), self.findings())
        self.assertIn("[PROBLEM] Something is broken", text)
        self.assertIn("[WORTH CHECKING] Something is odd", text)
        self.assertIn("1 problem, 1 worth checking.", text)

    def test_explanation_and_fix_are_both_shown(self):
        text = report.build_summary(saved(), self.findings())
        self.assertIn("The plain English explanation.", text)
        self.assertIn("What to do: Do this about it.", text)
        self.assertIn("seen: the evidence", text)

    def test_a_finding_with_no_fix_shows_no_empty_instruction(self):
        text = report.build_summary(saved(), [self.findings()[1]])
        self.assertNotIn("What to do: \n", text)

    def test_no_findings_says_so_without_claiming_the_console_is_healthy(self):
        text = report.build_summary(saved(), [])
        self.assertIn("Nothing stood out", text)
        self.assertIn("does not prove the console is healthy", text)

    def test_a_broken_rule_is_reported_not_hidden(self):
        text = report.build_summary(
            saved(), [], [{"rule_id": "bad-rule", "error": "boom"}])
        self.assertIn("fault in ps3-diag", text)
        self.assertIn("bad-rule: boom", text)

    def test_findings_come_from_a_dict_as_readily_as_an_object(self):
        text = report.build_summary(saved(), [{
            "rule_id": "x", "severity": "error", "title": "From a dict",
            "explanation": "Loaded out of a saved findings file.",
            "fix": "", "evidence": [], "category": ""}])
        self.assertIn("From a dict", text)


class Csv(FixtureCase):
    def test_awkward_names_are_quoted_and_survive_a_round_trip(self):
        import csv
        import io
        text = report.inventory_csv(saved())
        rows = list(csv.DictReader(io.StringIO(text)))
        names = [row["name"] for row in rows]
        self.assertIn("Gran Turismo 5 & Prologue, Collector's Edition "
                      "[BCES00569].iso", names)
        row = next(item for item in rows if "Gran Turismo" in item["name"])
        self.assertEqual(row["title_id"], "BCES00569")
        self.assertEqual(row["region"], "Europe")
        self.assertEqual(row["video_standard"], "PAL")

    def test_an_empty_inventory_produces_no_file(self):
        self.assertEqual(report.inventory_csv(ArtefactSet({}, {})), "")


class Written(FixtureCase):
    def test_a_saved_set_can_be_re_rendered_and_re_saved(self):
        artefacts = saved()
        with tempfile.TemporaryDirectory() as folder:
            path, _counts = report.write_zip(
                artefacts, folder,
                [Finding("x", "info", "A note", "Explanation.")],
                name="re-rendered")
            self.assertTrue(os.path.exists(path))
            with zipfile.ZipFile(path) as archive:
                summary = archive.read("summary.txt").decode()
                findings = json.loads(
                    archive.read("analysis/findings.json").decode())
                manifest = json.loads(archive.read("manifest.json").decode())
        self.assertIn("A note", summary)
        self.assertEqual(findings["findings"][0]["rule_id"], "x")
        self.assertEqual(manifest["findings_count"]["info"], 1)

    def test_the_file_name_carries_the_date_and_time(self):
        name = report.timestamp_name()
        self.assertTrue(name.startswith("ps3-diag-"))
        self.assertRegex(name, r"^ps3-diag-\d{4}-\d{2}-\d{2}-\d{6}$")
