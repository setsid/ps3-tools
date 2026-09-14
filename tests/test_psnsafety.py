"""Tests for the PSN safety check.

Two things are being protected here. The first is the usual one: the right
conclusion from the right facts. The second matters more, which is that a
category that never ran is never reported as a category that found nothing,
because those two produce opposite advice and only one of them is honest.

Everything runs offline against sets built in memory or the small zips in
tests/fixtures/psn. Nothing here touches a console.
"""

import json
import unittest

from support import FixtureCase, fixture_path
from ps3diag.artefacts import ArtefactSet
from ps3diag import psnsafety


def make_set(system=None, plugins=None, webman=None, statuses=None):
    """A set carrying only the categories a test cares about, so that
    "absent" is the default and has to be overridden deliberately."""
    statuses = dict(statuses or {})
    categories = []
    files = {}
    for key, facts in (("system", system), ("plugins", plugins),
                       ("webman_config", webman)):
        status = statuses.get(key, "ok" if facts is not None else None)
        if status is None:
            continue
        categories.append({"key": key, "title": key, "status": status,
                           "seconds": 1.0, "error": statuses.get(f"{key}_error"),
                           "notes": [], "artefacts": []})
        if facts is not None:
            files[f"{key}/facts.json"] = json.dumps(facts)
    return ArtefactSet({"schema_version": 1, "target": "192.168.1.42",
                        "categories": categories}, files)


def plugin_facts(boot=(), installed=(), web=(), missing=()):
    return {
        "boot_plugins.txt": [
            {"line": index + 1, "name": name, "enabled": enabled,
             "path": f"/dev_hdd0/plugins/{name}"}
            for index, (name, enabled) in enumerate(boot)],
        "installed_in_plugins_folder": [
            {"name": name, "kind": "file", "size": 1024, "size_human": "1.0 KB"}
            for name in installed],
        "named_on_web_page": list(web),
        "listed_but_not_found": list(missing),
    }


CEX_OPEN = {"firmware": "4.93", "firmware_type": "CEX", "cfw_name": "Evilnat",
            "cfw_markers": ["Evilnat", "Cobra"], "cobra_version": "8.5",
            "syscall_state": "enabled", "syscall_number": "8"}
CEX_CLOSED = dict(CEX_OPEN, syscall_state="disabled")


def all_strings(payload):
    """Every string in the assessment, for the wording checks."""
    out = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            out.append(str(key))
            out.extend(all_strings(value))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            out.extend(all_strings(item))
    else:
        out.append(str(payload))
    return out


class SyscallTest(FixtureCase):
    def test_open_syscalls_are_named_as_the_visible_signature(self):
        result = psnsafety.assessment(make_set(system=CEX_OPEN))
        assessment = result["assessment"]
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(assessment["psn_visible_configuration"], "yes")
        self.assertIn("switched on", assessment["statement"])
        self.assertTrue(any("syscall 8" in line
                            for line in assessment["observations"]))

    def test_open_syscalls_produce_a_warning_with_something_to_do(self):
        findings = psnsafety.findings_for(make_set(system=CEX_OPEN))
        by_id = {finding.rule_id: finding for finding in findings}
        self.assertIn("psn-syscalls-open", by_id)
        self.assertEqual(by_id["psn-syscalls-open"].severity, "warn")
        self.assertTrue(by_id["psn-syscalls-open"].fix)

    def test_closed_syscalls_are_not_reported_as_open(self):
        assessment = psnsafety.assessment(
            make_set(system=CEX_CLOSED))["assessment"]
        ids = {f.rule_id for f in psnsafety.findings_for(
            make_set(system=CEX_CLOSED))}
        self.assertIn("switched off", assessment["statement"])
        self.assertNotIn("psn-syscalls-open", ids)

    def test_closed_syscalls_do_not_become_an_all_clear(self):
        # Syscalls off still leaves modified firmware underneath, and the
        # wording must not turn "one signal removed" into "nothing to see".
        assessment = psnsafety.assessment(
            make_set(system=CEX_CLOSED))["assessment"]
        self.assertEqual(assessment["psn_visible_configuration"], "yes")

    def test_partial_syscalls_are_treated_as_still_visible(self):
        facts = dict(CEX_OPEN, syscall_state="partial")
        assessment = psnsafety.assessment(make_set(system=facts))["assessment"]
        ids = {f.rule_id for f in psnsafety.findings_for(make_set(system=facts))}
        self.assertEqual(assessment["psn_visible_configuration"], "yes")
        self.assertIn("psn-syscalls-partial", ids)

    def test_missing_syscall_state_is_unknown_not_off(self):
        facts = {"firmware": "4.93", "firmware_type": "CEX"}
        assessment = psnsafety.assessment(make_set(system=facts))["assessment"]
        ids = {f.rule_id for f in psnsafety.findings_for(make_set(system=facts))}
        self.assertEqual(assessment["psn_visible_configuration"], "unknown")
        self.assertIn("psn-syscall-state-unknown", ids)
        self.assertTrue(any("did not say" in line or "not report" in line
                            for line in assessment["unknowns"]))


class FirmwareKindTest(FixtureCase):
    def test_cex_is_described_as_retail(self):
        assessment = psnsafety.assessment(make_set(system=CEX_OPEN))["assessment"]
        ids = {f.rule_id for f in psnsafety.findings_for(
            make_set(system=CEX_OPEN))}
        self.assertTrue(any("retail" in line
                            for line in assessment["observations"]))
        self.assertNotIn("psn-debug-firmware", ids)

    def test_dex_is_called_out_as_debug_firmware(self):
        facts = dict(CEX_OPEN, firmware_type="DEX")
        assessment = psnsafety.assessment(make_set(system=facts))["assessment"]
        by_id = {f.rule_id: f for f in psnsafety.findings_for(
            make_set(system=facts))}
        self.assertIn("psn-debug-firmware", by_id)
        self.assertEqual(by_id["psn-debug-firmware"].severity, "warn")
        self.assertTrue(any("debug" in line
                            for line in assessment["observations"]))

    def test_decr_is_treated_like_dex(self):
        facts = dict(CEX_OPEN, firmware_type="DECR")
        ids = {f.rule_id for f in psnsafety.findings_for(make_set(system=facts))}
        self.assertIn("psn-debug-firmware", ids)

    def test_hen_is_distinguished_from_replaced_firmware(self):
        facts = {"firmware": "4.91", "firmware_type": "CEX",
                 "cfw_markers": ["HEN", "HFW"], "hen_version": "3.0.3",
                 "syscall_state": "enabled"}
        assessment = psnsafety.assessment(make_set(system=facts))["assessment"]
        self.assertIn("HEN", assessment["statement"])
        self.assertNotIn("custom firmware, with", assessment["statement"])

    def test_cobra_is_reported_when_present(self):
        assessment = psnsafety.assessment(make_set(system=CEX_OPEN))["assessment"]
        self.assertTrue(any("Cobra 8.5" in line
                            for line in assessment["observations"]))


class PluginTest(FixtureCase):
    def test_spoofer_is_identified_by_name_and_hedged(self):
        facts = plugin_facts(boot=[("webftp_server.sprx", True),
                                   ("psnpatch.sprx", True)])
        assessment = psnsafety.assessment(
            make_set(system=CEX_OPEN, plugins=facts))["assessment"]
        joined = " ".join(assessment["observations"])
        self.assertIn("psnpatch.sprx", joined)
        self.assertIn("PSNPatch", joined)
        self.assertIn("not the same as working", joined)

    def test_spoof_in_the_name_is_matched(self):
        facts = plugin_facts(boot=[("fw_spoofer.sprx", True)])
        findings = psnsafety.findings_for(
            make_set(system=CEX_OPEN, plugins=facts))
        by_id = {f.rule_id: f for f in findings}
        self.assertIn("psn-spoofer-plugin-present", by_id)
        self.assertTrue(any("fw_spoofer.sprx" in line for line
                            in by_id["psn-spoofer-plugin-present"].evidence))

    def test_no_spoofer_is_stated_as_none_found_not_as_safe(self):
        facts = plugin_facts(boot=[("webftp_server.sprx", True),
                                   ("prepntfs.sprx", True)])
        assessment = psnsafety.assessment(
            make_set(system=CEX_OPEN, plugins=facts))["assessment"]
        ids = {f.rule_id for f in psnsafety.findings_for(
            make_set(system=CEX_OPEN, plugins=facts))}
        self.assertTrue(any("named like a known spoofer" in line
                            for line in assessment["observations"]))
        self.assertNotIn("psn-spoofer-plugin-present", ids)

    def test_installed_but_not_loading_is_separated_from_loading(self):
        facts = plugin_facts(boot=[("webftp_server.sprx", True)],
                             installed=["webftp_server.sprx", "psnpatch.sprx"])
        view = psnsafety.assessment(
            make_set(system=CEX_OPEN, plugins=facts))["assessment"]
        by_id = {f.rule_id: f for f in psnsafety.findings_for(
            make_set(system=CEX_OPEN, plugins=facts))}
        self.assertTrue(any("not in the boot list" in line
                            for line in view["observations"]))
        self.assertIn("psn-spoofer-plugin-not-loading", by_id)
        self.assertEqual(by_id["psn-spoofer-plugin-not-loading"].severity,
                         "warn")

    def test_loading_spoofer_does_not_get_the_not_loading_warning(self):
        facts = plugin_facts(boot=[("psnpatch.sprx", True)])
        ids = {f.rule_id for f in psnsafety.findings_for(
            make_set(system=CEX_OPEN, plugins=facts))}
        self.assertIn("psn-spoofer-plugin-present", ids)
        self.assertNotIn("psn-spoofer-plugin-not-loading", ids)

    def test_switched_off_plugins_are_listed_separately(self):
        facts = plugin_facts(boot=[("webftp_server.sprx", True),
                                   ("old_thing.sprx", False)])
        assessment = psnsafety.assessment(
            make_set(system=CEX_OPEN, plugins=facts))["assessment"]
        self.assertTrue(any("switched off" in line and "old_thing.sprx" in line
                            for line in assessment["observations"]))

    def test_missing_boot_plugin_file_is_reported(self):
        facts = plugin_facts(
            boot=[("irisman.sprx", True)],
            missing=["/dev_hdd0/plugins/Iris Manager/irisman.sprx"])
        ids = {f.rule_id for f in psnsafety.findings_for(
            make_set(system=CEX_OPEN, plugins=facts))}
        self.assertIn("psn-boot-plugin-missing", ids)


class NotCollectedTest(FixtureCase):
    """The category that never ran must never read as the category that
    found nothing."""

    def test_failed_plugins_are_unknown_rather_than_clean(self):
        artefacts = make_set(system=CEX_OPEN,
                             statuses={"plugins": "failed",
                                       "plugins_error": "FTP login refused"})
        assessment = psnsafety.assessment(artefacts)["assessment"]
        joined = " ".join(assessment["observations"] + assessment["unknowns"])
        self.assertIn("unknown", joined)
        self.assertIn("not the same as none", joined.lower())
        self.assertFalse(any("named like a known spoofer" in line
                             for line in assessment["observations"]))

    def test_absent_plugins_category_behaves_like_a_failed_one(self):
        artefacts = make_set(system=CEX_OPEN)
        ids = {f.rule_id for f in psnsafety.findings_for(artefacts)}
        self.assertIn("psn-plugins-not-collected", ids)

    def test_skipped_plugins_do_not_produce_plugin_conclusions(self):
        artefacts = make_set(system=CEX_OPEN, statuses={"plugins": "skipped"})
        assessment = psnsafety.assessment(artefacts)["assessment"]
        self.assertFalse(any("set to load at startup" in line
                             for line in assessment["observations"]))

    def test_partial_counts_as_collected(self):
        facts = plugin_facts(boot=[("psnpatch.sprx", True)])
        artefacts = make_set(system=CEX_OPEN, plugins=facts,
                             statuses={"plugins": "partial"})
        ids = {f.rule_id for f in psnsafety.findings_for(artefacts)}
        self.assertIn("psn-spoofer-plugin-present", ids)
        self.assertNotIn("psn-plugins-not-collected", ids)

    def test_no_system_category_says_so_plainly(self):
        artefacts = make_set(statuses={"system": "failed"})
        assessment = psnsafety.assessment(artefacts)["assessment"]
        ids = {f.rule_id for f in psnsafety.findings_for(artefacts)}
        self.assertEqual(assessment["psn_visible_configuration"], "unknown")
        self.assertIn("psn-system-not-collected", ids)
        self.assertNotIn("psn-syscalls-open", ids)

    def test_nothing_collected_at_all_still_answers(self):
        artefacts = ArtefactSet()
        assessment = psnsafety.assessment(artefacts)["assessment"]
        self.assertIn("Too little was collected", assessment["statement"])
        self.assertTrue(assessment["unknowns"])
        self.assertTrue(assessment["caveat"])


class WordingTest(FixtureCase):
    def test_there_is_no_score_of_any_kind(self):
        artefacts = make_set(system=CEX_OPEN,
                             plugins=plugin_facts(boot=[("psnpatch.sprx", True)]))
        blob = " ".join(all_strings(psnsafety.assessment(artefacts))).lower()
        for banned in ("score", "out of 10", "/10", "rating", "traffic light",
                       "green", "amber", "red flag", "percent chance"):
            self.assertNotIn(banned, blob)

    def test_nothing_is_promised_in_either_direction(self):
        artefacts = make_set(system=CEX_CLOSED)
        assessment = psnsafety.assessment(artefacts)["assessment"]
        blob = " ".join(all_strings(assessment)).lower()
        self.assertIn("nobody can tell you", blob)
        for banned in ("you will not be banned", "it is safe to sign in",
                       "guaranteed", "you will be banned", "definitely"):
            self.assertNotIn(banned, blob)

    def test_the_limits_of_the_tool_are_always_stated(self):
        for artefacts in (ArtefactSet(), make_set(system=CEX_OPEN),
                          make_set(system=CEX_CLOSED)):
            assessment = psnsafety.assessment(artefacts)["assessment"]
            blob = " ".join(all_strings(assessment)).lower()
            self.assertIn("cannot see everything psn can see", blob)
            self.assertIn("what its name suggests", blob)

    def test_output_is_plain_ascii_so_there_is_no_emoji(self):
        artefacts = make_set(system=CEX_OPEN,
                             plugins=plugin_facts(boot=[("fw_spoofer.sprx", True)]))
        payload = psnsafety.assessment(artefacts)
        for text in all_strings(payload):
            self.assertTrue(text.isascii(), text)
        for finding in psnsafety.findings_for(artefacts):
            for text in all_strings(finding.to_dict()):
                self.assertTrue(text.isascii(), text)

    def test_every_assessment_carries_the_caveat(self):
        assessment = psnsafety.assessment(make_set(system=CEX_OPEN))["assessment"]
        self.assertEqual(assessment["caveat"], psnsafety.CAVEAT)


class MalformedTest(FixtureCase):
    """Nothing in analysis may raise, including on facts nothing sane
    produced."""

    def test_empty_facts_do_not_raise(self):
        artefacts = make_set(system={}, plugins={}, webman={})
        psnsafety.assessment(artefacts)
        psnsafety.findings_for(artefacts)

    def test_wrong_types_throughout_do_not_raise(self):
        artefacts = make_set(
            system={"syscall_state": 5, "cfw_markers": "Evilnat",
                    "firmware_type": ["CEX"], "cobra_version": {"a": 1},
                    "cfw_name": None},
            plugins={"boot_plugins.txt": "not a list",
                     "installed_in_plugins_folder": [1, None, {"name": 5},
                                                     "loose.sprx"],
                     "named_on_web_page": {"a": "b"},
                     "listed_but_not_found": [None, 7]},
            webman={"settings": ["not", "a", "mapping"]})
        payload = psnsafety.assessment(artefacts)
        self.assertTrue(payload["assessment"]["statement"])
        psnsafety.findings_for(artefacts)

    def test_unparseable_facts_json_does_not_raise(self):
        artefacts = make_set(system=CEX_OPEN,
                             statuses={"plugins": "ok"})
        artefacts.files["plugins/facts.json"] = "{ not json at all"
        psnsafety.assessment(artefacts)
        psnsafety.findings_for(artefacts)

    def test_plugin_rows_without_names_do_not_raise(self):
        facts = {"boot_plugins.txt": [{"line": 1, "enabled": True},
                                      {"path": "/dev_hdd0/plugins/a.sprx"},
                                      {}]}
        artefacts = make_set(system=CEX_OPEN, plugins=facts)
        psnsafety.assessment(artefacts)
        psnsafety.findings_for(artefacts)

    def test_findings_are_all_well_formed(self):
        artefacts = make_set(system=dict(CEX_OPEN, firmware_type="DEX"),
                             plugins=plugin_facts(
                                 boot=[("webftp_server.sprx", True)],
                                 installed=["psnpatch.sprx"],
                                 missing=["/dev_hdd0/plugins/gone.sprx"]))
        for finding in psnsafety.findings_for(artefacts):
            self.assertIn(finding.severity, ("error", "warn", "info"))
            self.assertTrue(finding.rule_id.startswith("psn-"))
            self.assertTrue(finding.title)
            self.assertTrue(finding.explanation)
            self.assertIn(finding.category, ("system", "plugins"))


class IntegrationTest(FixtureCase):
    def test_the_names_ps3diag_analysis_reaches_for_exist(self):
        # analysis.py loads this pass by name, so the names are part of the
        # contract and not an implementation detail.
        from ps3diag import analysis
        wanted = {(module, attribute)
                  for module, attribute, _why in analysis.PASSES
                  + analysis.ARTEFACT_PASSES
                  if module == "ps3diag.psnsafety"}
        self.assertTrue(wanted)
        for _module, attribute in wanted:
            names = (attribute,) if isinstance(attribute, str) else attribute
            self.assertTrue(
                any(callable(getattr(psnsafety, name, None))
                    for name in names),
                f"psnsafety exposes none of {names}")

    def test_the_pass_runs_through_analysis_and_writes_its_artefact(self):
        from ps3diag import analysis
        artefacts = make_set(system=CEX_OPEN)
        outcome = analysis.analyse(artefacts)
        self.assertEqual([broken for broken in outcome.broken_rules
                          if "psn" in str(broken.get("rule_id", ""))], [])
        payload = artefacts.json(psnsafety.ARTEFACT_NAME)
        self.assertEqual(payload["assessment"]["caveat"], psnsafety.CAVEAT)
        self.assertTrue(any(f.rule_id.startswith("psn-")
                            for f in outcome.findings))

    def test_the_older_names_still_work(self):
        artefacts = make_set(system=CEX_OPEN)
        self.assertEqual(psnsafety.assess(artefacts),
                         psnsafety.assessment(artefacts))
        self.assertEqual(len(psnsafety.psn_findings(artefacts)),
                         len(psnsafety.findings_for(artefacts)))


class ArtefactTest(FixtureCase):
    def test_record_writes_the_reserved_name_as_json(self):
        artefacts = make_set(system=CEX_OPEN)
        psnsafety.record(artefacts)
        raw = artefacts.text(psnsafety.ARTEFACT_NAME)
        self.assertTrue(raw)
        payload = json.loads(raw)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(set(payload), {"schema_version", "assessment"})
        self.assertEqual(payload["assessment"]["caveat"], psnsafety.CAVEAT)

    def test_the_summary_can_render_what_was_written(self):
        from ps3diag import report
        artefacts = make_set(system=CEX_OPEN)
        psnsafety.record(artefacts)
        text = report.build_summary(artefacts)
        self.assertIn("SIGNING IN TO PSN", text)
        self.assertIn("Nobody can tell you", text)


class FixtureTest(FixtureCase):
    def test_real_sample_diagnostic(self):
        artefacts = ArtefactSet.from_zip(
            fixture_path("sample-diagnostic.zip"))
        assessment = psnsafety.assessment(artefacts)["assessment"]
        self.assertEqual(assessment["psn_visible_configuration"], "yes")
        joined = " ".join(assessment["observations"])
        self.assertIn("syscall 8", joined)
        self.assertIn("Evilnat", joined)
        self.assertIn("rebug_toolbox.sprx", joined)

    def test_dex_console_with_a_dormant_spoofer(self):
        artefacts = ArtefactSet.from_zip(
            fixture_path("psn", "dex-spoofer.zip"))
        assessment = psnsafety.assessment(artefacts)["assessment"]
        ids = {f.rule_id for f in psnsafety.findings_for(artefacts)}
        joined = " ".join(assessment["observations"])
        self.assertIn("psnpatch.sprx", joined)
        self.assertIn("fw_spoofer.sprx", joined)
        self.assertIn("psn-debug-firmware", ids)
        self.assertIn("psn-spoofer-plugin-not-loading", ids)

    def test_hen_console_whose_plugin_collection_failed(self):
        artefacts = ArtefactSet.from_zip(
            fixture_path("psn", "hen-plugins-failed.zip"))
        assessment = psnsafety.assessment(artefacts)["assessment"]
        ids = {f.rule_id for f in psnsafety.findings_for(artefacts)}
        self.assertIn("HEN", assessment["statement"])
        self.assertIn("psn-plugins-not-collected", ids)
        self.assertTrue(any("FTP login refused" in line
                            for line in assessment["unknowns"]))


if __name__ == "__main__":
    unittest.main()
