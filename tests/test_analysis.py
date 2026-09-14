"""The analysis layer must survive its own passes being missing or broken."""

import sys

from support import FixtureCase, fixture_path
from ps3diag import analysis
from ps3diag.artefacts import ArtefactSet
from ps3diag.findings import Finding


class Wiring(FixtureCase):
    def setUp(self):
        self.artefacts = ArtefactSet.from_zip(
            fixture_path("sample-diagnostic.zip"))
        self.original = analysis.PASSES
        self.addCleanup(setattr, analysis, "PASSES", self.original)

    def test_a_missing_pass_is_a_broken_rule_not_a_crash(self):
        analysis.PASSES = (("ps3diag.not_a_module", ("run",),
                            "made up pass"),)
        outcome = analysis.analyse(self.artefacts)
        self.assertEqual(outcome.findings, [])
        self.assertEqual(len(outcome.broken_rules), 1)
        self.assertIn("could not be loaded", outcome.broken_rules[0]["error"])

    def test_a_pass_that_throws_does_not_stop_the_others(self):
        module = type(sys)("ps3diag.fake_passes")

        def explodes(_artefacts):
            raise ValueError("deliberate")

        def works(_artefacts):
            return [Finding("ok", "info", "It ran", "Explanation.")]

        module.explodes = explodes
        module.works = works
        sys.modules["ps3diag.fake_passes"] = module
        self.addCleanup(sys.modules.pop, "ps3diag.fake_passes", None)

        analysis.PASSES = (
            ("ps3diag.fake_passes", ("explodes",), "exploding pass"),
            ("ps3diag.fake_passes", ("works",), "working pass"),
        )
        outcome = analysis.analyse(self.artefacts)
        self.assertEqual([item.rule_id for item in outcome.findings], ["ok"])
        self.assertEqual(len(outcome.broken_rules), 1)
        self.assertIn("deliberate", outcome.broken_rules[0]["error"])

    def test_a_pass_is_found_under_any_of_its_names(self):
        module = type(sys)("ps3diag.renamed")
        module.second_choice = lambda _artefacts: [
            Finding("found", "info", "It ran", "Explanation.")]
        sys.modules["ps3diag.renamed"] = module
        self.addCleanup(sys.modules.pop, "ps3diag.renamed", None)
        analysis.PASSES = (("ps3diag.renamed",
                            ("first_choice", "second_choice"), "renamed"),)
        outcome = analysis.analyse(self.artefacts)
        self.assertEqual([item.rule_id for item in outcome.findings],
                         ["found"])
        self.assertEqual(outcome.broken_rules, [])

    def test_findings_come_back_most_serious_first(self):
        module = type(sys)("ps3diag.ordered")
        module.run = lambda _artefacts: [
            Finding("c", "info", "c", ""),
            Finding("a", "error", "a", ""),
            Finding("b", "warn", "b", ""),
        ]
        sys.modules["ps3diag.ordered"] = module
        self.addCleanup(sys.modules.pop, "ps3diag.ordered", None)
        analysis.PASSES = (("ps3diag.ordered", ("run",), "ordered"),)
        outcome = analysis.analyse(self.artefacts)
        self.assertEqual([item.severity for item in outcome.findings],
                         ["error", "warn", "info"])


class OnRealData(FixtureCase):
    def test_a_saved_diagnostic_analyses_with_no_console_present(self):
        artefacts = ArtefactSet.from_zip(
            fixture_path("sample-diagnostic.zip"))
        outcome = analysis.analyse(artefacts)
        self.assertIsInstance(outcome.findings, list)
        for finding in outcome.findings:
            self.assertIn(finding.severity, ("error", "warn", "info"))
            self.assertTrue(finding.title)
            self.assertTrue(finding.explanation)

    def test_an_empty_set_produces_nothing_rather_than_raising(self):
        outcome = analysis.analyse(ArtefactSet({}, {}))
        self.assertIsInstance(outcome.findings, list)
