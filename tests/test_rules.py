"""Tests for the rules engine and the rules it ships with.

Every set here is built in memory. A rule is a pure function from an
ArtefactSet to findings, so a fixture is a dict, and a test that needs a drive
formatted a particular way says so in four lines rather than in a zip nobody
can read the diff of.

Each rule gets three checks: a set that should set it off, a set that should
not, and the same triggering set with its categories marked failed and then
removed altogether. That third one is the rule that matters most. "We never
managed to look" must never come out as "nothing is wrong".
"""

import json

from support import FixtureCase, fixture_path

from ps3diag.artefacts import ArtefactSet
from ps3diag.findings import Finding
from ps3diag.rules import (builtin, engine, get_rule, registered_rules,
                           run_rules)

GIB = 1024 ** 3
MIB = 1024 ** 2

FIXTURE_ZIP = fixture_path("sample-diagnostic.zip")


# --- fixture building ------------------------------------------------------

def make_set(facts=None, files=None, status="ok", extra_categories=()):
    """An ArtefactSet holding the given category facts.

    status=None leaves the categories out of the manifest entirely, which is
    how a run that was never asked for a category looks.
    """
    facts = facts or {}
    members = dict(files or {})
    keys = list(facts) + [key for key in extra_categories if key not in facts]
    for key, value in facts.items():
        members[f"{key}/facts.json"] = json.dumps(value)
    categories = []
    if status is not None:
        categories = [{"key": key, "title": key, "status": status,
                       "seconds": 0.0, "error": None, "notes": [],
                       "artefacts": []} for key in keys]
    manifest = {
        "schema_version": 1,
        "tool": "ps3-diag",
        "tool_version": "1.0",
        "target": "192.168.1.42",
        "duration_seconds": 1.0,
        "identifiers_included": False,
        "categories": categories,
    }
    return ArtefactSet(manifest, members)


def entry(name, **fields):
    row = {"name": name, "kind": "file", "size": 8 * GIB}
    row.update(fields)
    return row


def folder(device, name, entries):
    return {f"{device}/{name}": {"device": device, "folder": name,
                                 "count": len(entries), "entries": entries}}


def games(*folders):
    merged = {}
    for item in folders:
        merged.update(item)
    return {"games": {"folders": merged}}


def storage(*devices):
    return {"storage": {"devices": list(devices)}}


def plugins(*entries):
    return {"plugins": {"boot_plugins.txt": list(entries)}}


def plugin(name, enabled=True, line=1, path=None):
    return {"line": line, "name": name, "enabled": enabled,
            "path": path or f"/dev_hdd0/plugins/{name}"}


def merge(*dicts):
    out = {}
    for item in dicts:
        out.update(item)
    return out


HDD0_ROOMY = {"device": "dev_hdd0", "free_bytes": 400 * GIB,
              "free": "400.0 GB", "total_bytes": 900 * GIB,
              "folders": ["GAMES", "PS3ISO"], "top_level_entries": 8}


class RuleTestCase(FixtureCase):
    """Shared asserts. Every wording check runs on every finding produced."""

    def fire(self, rule_fn, facts, files=None, severity=None):
        produced = _as_list(rule_fn(make_set(facts, files)))
        self.assertTrue(produced,
                        f"{rule_fn.rule_id} produced nothing on a set that "
                        f"should set it off")
        for finding in produced:
            self.check_wording(finding, rule_fn)
        if severity:
            self.assertEqual(severity, produced[0].severity)
        self.assert_quiet_when_not_collected(rule_fn, facts, files)
        return produced

    def quiet(self, rule_fn, facts, files=None):
        produced = _as_list(rule_fn(make_set(facts, files)))
        self.assertEqual([], produced,
                         f"{rule_fn.rule_id} spoke up when it should not have")

    def assert_quiet_when_not_collected(self, rule_fn, facts, files=None):
        for status in ("failed", "skipped", None):
            produced = _as_list(rule_fn(make_set(facts, files, status=status)))
            self.assertEqual(
                [], produced,
                f"{rule_fn.rule_id} drew a conclusion from a category whose "
                f"status was {status!r}")

    def check_wording(self, finding, rule_fn):
        self.assertEqual(rule_fn.rule_id, finding.rule_id)
        self.assertIn(finding.severity, ("error", "warn", "info"))
        for label, text in (("title", finding.title),
                            ("explanation", finding.explanation),
                            ("fix", finding.fix)):
            self.assertTrue(text.strip(), f"{rule_fn.rule_id} has no {label}")
            # ASCII rules out emoji and the stray curly quote, both of which
            # the plain text summary has nowhere to put.
            self.assertTrue(text.isascii(),
                            f"{rule_fn.rule_id} {label} is not plain text: "
                            f"{text!r}")
            # "1 games" is the tell-tale of a count dropped into a sentence
            # without looking, and it costs the reader confidence in the rest.
            self.assertNotRegex(text, r"\b1 [a-z]+s\b",
                                f"{rule_fn.rule_id} {label} counts badly")
        self.assertLess(len(finding.explanation), 500)
        self.assertTrue(finding.evidence,
                        f"{rule_fn.rule_id} shows no evidence")
        self.assertTrue(finding.category)


def _as_list(produced):
    if produced is None:
        return []
    if isinstance(produced, Finding):
        return [produced]
    return list(produced)


# --- the engine ------------------------------------------------------------

class EngineTests(FixtureCase):

    def test_registered_ids_are_unique_and_kebab_case(self):
        ids = [item.rule_id for item in registered_rules()]
        self.assertEqual(len(ids), len(set(ids)))
        for rule_id in ids:
            self.assertRegex(rule_id, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_every_builtin_is_registered(self):
        for name in builtin.__all__:
            function = getattr(builtin, name)
            self.assertIsNotNone(get_rule(function.rule_id))

    def test_a_throwing_rule_does_not_stop_the_others(self):
        def explodes(artefact_set):
            raise ValueError("no")
        explodes.rule_id = "explodes"

        def speaks(artefact_set):
            return Finding("speaks", "warn", "t", "e", "f", ["x"], "system")
        speaks.rule_id = "speaks"

        outcome = run_rules(make_set(), rules=[explodes, speaks, explodes])
        self.assertEqual(1, len(outcome.findings))
        self.assertEqual(2, len(outcome.broken_rules))
        self.assertEqual("explodes", outcome.broken_rules[0]["rule_id"])
        self.assertIn("ValueError", outcome.broken_rules[0]["error"])

    def test_a_rule_returning_nonsense_is_broken_not_fatal(self):
        def nonsense(artefact_set):
            return "a string is not a finding"
        nonsense.rule_id = "nonsense"

        outcome = run_rules(make_set(), rules=[nonsense])
        self.assertEqual([], outcome.findings)
        self.assertIn("TypeError", outcome.broken_rules[0]["error"])

    def test_findings_come_back_most_serious_first(self):
        def three(artefact_set):
            return [Finding("c", "info", "t", "e", "f", ["x"], "system"),
                    Finding("a", "error", "t", "e", "f", ["x"], "system"),
                    Finding("b", "warn", "t", "e", "f", ["x"], "system")]
        three.rule_id = "three"

        outcome = run_rules(make_set(), rules=[three])
        self.assertEqual(["error", "warn", "info"],
                         [item.severity for item in outcome.findings])

    def test_a_rule_can_be_run_on_its_own_by_id(self):
        outcome = run_rules(make_set(), rules=["temperature-high"])
        self.assertEqual([], outcome.findings)
        self.assertEqual([], outcome.broken_rules)

    def test_an_unknown_rule_id_is_reported_not_raised(self):
        outcome = run_rules(make_set(), rules=["no-such-rule"])
        self.assertEqual("no-such-rule", outcome.broken_rules[0]["rule_id"])

    def test_the_engine_fills_in_rule_id_and_category(self):
        def bare(artefact_set):
            return {"severity": "info", "title": "t", "explanation": "e"}
        registered = engine.Rule("bare-rule", "system", bare)

        outcome = run_rules(make_set(), rules=[registered])
        self.assertEqual("bare-rule", outcome.findings[0].rule_id)
        self.assertEqual("system", outcome.findings[0].category)

    def test_an_empty_set_produces_nothing_and_breaks_nothing(self):
        outcome = run_rules(ArtefactSet())
        self.assertEqual([], outcome.findings)
        self.assertEqual([], outcome.broken_rules)

    def test_a_set_full_of_rubbish_breaks_nothing(self):
        rubbish = make_set({"storage": {"devices": "not a list"},
                            "games": {"folders": [1, 2, 3]},
                            "system": {"cpu_temp_c": "hot"},
                            "plugins": {"boot_plugins.txt": {"a": 1}}})
        outcome = run_rules(rubbish)
        self.assertEqual([], outcome.findings)
        # Facts of the wrong shape are a rule falling silent, not a fault in
        # ps3-diag, and the summary says very different things about the two.
        self.assertEqual([], outcome.broken_rules)

    def test_the_payload_matches_the_reserved_artefact_shape(self):
        outcome = run_rules(make_set(), rules=["temperature-high"])
        payload = outcome.to_dict()
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual([], payload["findings"])
        self.assertEqual([], payload["broken_rules"])

    def test_the_sample_diagnostic_runs_clean(self):
        outcome = run_rules(ArtefactSet.from_zip(FIXTURE_ZIP))
        self.assertEqual([], outcome.broken_rules)
        known = {item.rule_id for item in registered_rules()}
        for finding in outcome.findings:
            self.assertIn(finding.rule_id, known)
            self.assertTrue(finding.explanation.isascii())
            json.dumps(finding.to_dict())


# --- storage ---------------------------------------------------------------

class UsbNotMountedTests(RuleTestCase):

    def test_a_named_usb_drive_with_nothing_readable_on_it(self):
        self.fire(builtin.usb_not_mounted,
                  storage(HDD0_ROOMY, {"device": "dev_usb000",
                                       "present_over_ftp": True}),
                  severity="error")

    def test_a_usb_drive_reporting_its_size_is_fine(self):
        self.quiet(builtin.usb_not_mounted,
                   storage(HDD0_ROOMY,
                           {"device": "dev_usb000", "free_bytes": 200 * GIB,
                            "total_bytes": 900 * GIB}))

    def test_an_empty_but_mounted_usb_drive_is_fine(self):
        self.quiet(builtin.usb_not_mounted,
                   storage(HDD0_ROOMY,
                           {"device": "dev_usb000", "free_bytes": 900 * GIB,
                            "folders": [], "top_level_entries": 0}))

    def test_nothing_is_said_when_no_drive_reported_anything(self):
        # FTP refused: every drive looks unmounted and none of them is.
        self.quiet(builtin.usb_not_mounted,
                   storage({"device": "dev_hdd0"}, {"device": "dev_usb000"}))

    def test_an_internal_drive_is_not_accused_of_being_a_usb_stick(self):
        self.quiet(builtin.usb_not_mounted,
                   storage(HDD0_ROOMY, {"device": "dev_hdd1"}))


class FolderGamesOnNtfsTests(RuleTestCase):

    USB_FOLDER_GAMES = games(folder("dev_usb000", "GAMES", [
        entry("BLES01718", kind="directory", size=0),
        entry("Demon's Souls [BLES00932]", kind="directory", size=0)]))

    def test_prepntfs_loaded_points_at_an_ntfs_drive(self):
        produced = self.fire(
            builtin.folder_games_on_ntfs,
            merge(self.USB_FOLDER_GAMES, plugins(plugin("prepntfs.sprx"))),
            severity="warn")
        self.assertIn("prepNTFS", produced[0].explanation)

    def test_a_file_too_big_for_fat32_rules_fat32_out(self):
        facts = games(
            folder("dev_usb000", "GAMES",
                   [entry("BLES01718", kind="directory", size=0)]),
            folder("dev_usb000", "PS3ISO",
                   [entry("Big [BLES00001].iso", size=20 * GIB,
                          size_human="20.0 GB")]))
        produced = self.fire(builtin.folder_games_on_ntfs, facts)
        self.assertIn("20.0 GB", produced[0].explanation)

    def test_folder_games_on_the_internal_drive_are_normal(self):
        self.quiet(builtin.folder_games_on_ntfs,
                   merge(games(folder("dev_hdd0", "GAMES",
                                      [entry("BLES01718", kind="directory",
                                             size=0)])),
                         plugins(plugin("prepntfs.sprx"))))

    def test_nothing_is_said_when_the_format_cannot_be_guessed_at(self):
        self.quiet(builtin.folder_games_on_ntfs, self.USB_FOLDER_GAMES)

    def test_prepntfs_switched_off_says_nothing(self):
        self.quiet(builtin.folder_games_on_ntfs,
                   merge(self.USB_FOLDER_GAMES,
                         plugins(plugin("prepntfs.sprx", enabled=False))))


class FileTooBigForFat32Tests(RuleTestCase):

    def test_a_big_file_on_a_drive_that_looks_like_fat32(self):
        facts = games(folder("dev_usb000", "PS3ISO", [
            entry("Split [BLES00002].iso.0", size=4 * GIB - 1),
            entry("Split [BLES00002].iso.1", size=2 * GIB),
            entry("Whole [BLES00003].iso", size=20 * GIB,
                  size_human="20.0 GB")]))
        produced = self.fire(builtin.file_too_big_for_fat32, facts,
                             severity="warn")
        self.assertIn("Whole [BLES00003].iso", produced[0].explanation)

    def test_split_files_with_nothing_oversized_are_fine(self):
        self.quiet(builtin.file_too_big_for_fat32,
                   games(folder("dev_usb000", "PS3ISO", [
                       entry("Split [BLES00002].iso.0", size=4 * GIB - 1),
                       entry("Split [BLES00002].iso.1", size=2 * GIB)])))

    def test_a_big_file_on_a_drive_with_no_split_games_says_nothing(self):
        self.quiet(builtin.file_too_big_for_fat32,
                   games(folder("dev_hdd0", "PS3ISO",
                                [entry("Whole [BLES00003].iso",
                                       size=40 * GIB)])))


class InternalSpaceLowForPs2Tests(RuleTestCase):

    PS2_ON_USB = games(folder("dev_usb000", "PS2ISO", [
        entry("Persona 4 [SLUS-21782].iso", size=4 * GIB,
              size_human="4.0 GB")]))

    def test_a_full_console_cannot_copy_a_ps2_game_over(self):
        facts = merge(storage({"device": "dev_hdd0", "free_bytes": 2 * GIB,
                               "free": "2.0 GB", "total_bytes": 900 * GIB}),
                      self.PS2_ON_USB)
        produced = self.fire(builtin.internal_space_low_for_ps2, facts,
                             severity="warn")
        self.assertIn("2.0 GB", produced[0].explanation)

    def test_plenty_of_room_says_nothing(self):
        self.quiet(builtin.internal_space_low_for_ps2,
                   merge(storage(HDD0_ROOMY), self.PS2_ON_USB))

    def test_ps2_games_on_the_internal_drive_are_not_copied_anywhere(self):
        self.quiet(builtin.internal_space_low_for_ps2,
                   merge(storage({"device": "dev_hdd0",
                                  "free_bytes": 2 * GIB}),
                         games(folder("dev_hdd0", "PS2ISO", [
                             entry("Persona 4 [SLUS-21782].iso",
                                   size=4 * GIB)]))))

    def test_unknown_free_space_is_not_treated_as_none(self):
        self.quiet(builtin.internal_space_low_for_ps2,
                   merge(storage({"device": "dev_hdd0"}), self.PS2_ON_USB))


# --- games -----------------------------------------------------------------

class IsoFoldersNestedTests(RuleTestCase):

    def test_a_ps3iso_folder_inside_ps2iso(self):
        produced = self.fire(
            builtin.iso_folders_nested,
            games(folder("dev_hdd0", "PS2ISO",
                         [entry("PS3ISO", kind="directory", size=0)])),
            severity="error")
        self.assertIn("PS3ISO", produced[0].explanation)
        self.assertIn("PS2ISO", produced[0].explanation)

    def test_an_ordinary_folder_game_is_not_a_nested_folder(self):
        self.quiet(builtin.iso_folders_nested,
                   games(folder("dev_hdd0", "GAMES",
                                [entry("BLES01718", kind="directory",
                                       size=0)])))

    def test_a_folder_merely_named_like_one_is_left_alone(self):
        self.quiet(builtin.iso_folders_nested,
                   games(folder("dev_hdd0", "PS2ISO",
                                [entry("PS3ISO backup", kind="directory",
                                       size=0)])))


class SplitIsoMissingPartTests(RuleTestCase):

    def test_two_missing_parts_read_as_two(self):
        produced = self.fire(
            builtin.split_iso_missing_part,
            games(folder("dev_usb000", "PS3ISO", [
                entry("Game [BLES00004].iso.0", size=GIB),
                entry("Game [BLES00004].iso.3", size=GIB)])))
        self.assertIn("2 of them are", produced[0].explanation)

    def test_a_gap_in_the_numbering(self):
        produced = self.fire(
            builtin.split_iso_missing_part,
            games(folder("dev_usb000", "PS3ISO", [
                entry("Game [BLES00004].iso.0", size=4 * GIB - 1),
                entry("Game [BLES00004].iso.2", size=2 * GIB)])),
            severity="error")
        self.assertIn("Game [BLES00004].iso.1", produced[0].fix)

    def test_a_set_starting_at_one(self):
        produced = self.fire(
            builtin.split_iso_missing_part,
            games(folder("dev_usb000", "PS3ISO", [
                entry("Game [BLES00004].iso.1", size=4 * GIB - 1),
                entry("Game [BLES00004].iso.2", size=2 * GIB)])))
        self.assertIn("Game [BLES00004].iso.0", produced[0].fix)

    def test_a_complete_set_says_nothing(self):
        self.quiet(builtin.split_iso_missing_part,
                   games(folder("dev_usb000", "PS3ISO", [
                       entry("Game [BLES00004].iso.0", size=4 * GIB - 1),
                       entry("Game [BLES00004].iso.1", size=2 * GIB)])))

    def test_two_sets_in_one_folder_are_counted_separately(self):
        self.quiet(builtin.split_iso_missing_part,
                   games(folder("dev_usb000", "PS3ISO", [
                       entry("One [BLES00004].iso.0", size=GIB),
                       entry("One [BLES00004].iso.1", size=GIB),
                       entry("Two [BLES00005].iso.0", size=GIB)])))


class IsoZeroBytesTests(RuleTestCase):

    def test_several_empty_isos_are_counted_as_several(self):
        produced = self.fire(
            builtin.iso_zero_bytes,
            games(folder("dev_hdd0", "PS3ISO", [
                entry("Broken [BLES00006].iso", size=0),
                entry("Also broken [BLES00010].iso", size=0)])))
        self.assertIn("2 game files", produced[0].explanation)

    def test_an_empty_iso(self):
        produced = self.fire(
            builtin.iso_zero_bytes,
            games(folder("dev_hdd0", "PS3ISO",
                         [entry("Broken [BLES00006].iso", size=0)])),
            severity="error")
        self.assertIn("Broken [BLES00006].iso", produced[0].evidence[0])

    def test_a_full_size_iso_says_nothing(self):
        self.quiet(builtin.iso_zero_bytes,
                   games(folder("dev_hdd0", "PS3ISO",
                                [entry("Fine [BLES00007].iso",
                                       size=20 * GIB)])))

    def test_an_unrecorded_size_is_not_treated_as_zero(self):
        self.quiet(builtin.iso_zero_bytes,
                   games(folder("dev_hdd0", "PS3ISO",
                                [entry("Unknown [BLES00008].iso",
                                       size=None)])))

    def test_an_empty_folder_game_is_not_an_empty_iso(self):
        self.quiet(builtin.iso_zero_bytes,
                   games(folder("dev_hdd0", "GAMES",
                                [entry("BLES01718", kind="directory",
                                       size=0)])))


class IsoTooSmallTests(RuleTestCase):

    def test_a_ps3_iso_of_a_few_megabytes(self):
        produced = self.fire(
            builtin.iso_too_small,
            games(folder("dev_hdd0", "PS3ISO",
                         [entry("Cut [BLES00009].iso", size=5 * MIB,
                                size_human="5.0 MB")])),
            severity="warn")
        self.assertIn("Cut [BLES00009].iso", produced[0].evidence[0])

    def test_a_normal_ps3_iso_says_nothing(self):
        self.quiet(builtin.iso_too_small,
                   games(folder("dev_hdd0", "PS3ISO",
                                [entry("Fine [BLES00007].iso",
                                       size=20 * GIB)])))

    def test_a_small_last_part_of_a_split_set_is_normal(self):
        self.quiet(builtin.iso_too_small,
                   games(folder("dev_hdd0", "PS3ISO", [
                       entry("Game [BLES00004].iso.0", size=4 * GIB - 1),
                       entry("Game [BLES00004].iso.1", size=5 * MIB)])))

    def test_a_small_ps2_game_is_normal(self):
        self.quiet(builtin.iso_too_small,
                   games(folder("dev_hdd0", "PS2ISO",
                                [entry("Small [SLUS-12345].iso",
                                       size=5 * MIB)])))


# --- system ----------------------------------------------------------------

class ManagerPluginConflictTests(RuleTestCase):

    def test_webman_and_multiman_both_loading(self):
        produced = self.fire(
            builtin.manager_plugin_conflict,
            plugins(plugin("webftp_server.sprx", line=2),
                    plugin("multiman.sprx", line=3)),
            severity="warn")
        self.assertIn("multiMAN", produced[0].explanation)

    def test_multiman_commented_out_is_not_a_conflict(self):
        self.quiet(builtin.manager_plugin_conflict,
                   plugins(plugin("webftp_server.sprx", line=2),
                           plugin("multiman.sprx", line=3, enabled=False)))

    def test_webman_on_its_own_is_the_normal_case(self):
        self.quiet(builtin.manager_plugin_conflict,
                   plugins(plugin("webftp_server.sprx", line=2),
                           plugin("prepntfs.sprx", line=3)))

    def test_the_manager_is_found_by_its_path_too(self):
        self.fire(builtin.manager_plugin_conflict,
                  plugins(plugin("webftp_server.sprx", line=2),
                          plugin("RELOAD.SELF", line=3,
                                 path="/dev_hdd0/game/BLES80608/USRDIR/"
                                      "multiMAN.sprx")))


class CobraDisabledTests(RuleTestCase):

    ISOS = games(folder("dev_hdd0", "PS3ISO",
                        [entry("Fine [BLES00007].iso", size=20 * GIB)]))
    NO_COBRA = {"system": {"firmware": "4.91", "webman_version": "1.47.44",
                           "cfw_markers": ["Rogero"], "cfw_name": "Rogero"}}
    COBRA = {"system": {"firmware": "4.93", "webman_version": "1.47.48",
                        "cfw_markers": ["Evilnat", "Cobra"],
                        "cobra_version": "8.5"}}
    CFW_NO_COBRA = {"system": {"firmware": "4.91",
                               "webman_version": "1.47.44",
                               "firmware_kind": "cfw",
                               "cfw_markers": ["Rogero"],
                               "cfw_name": "Rogero"}}
    HEN_NO_COBRA = {"system": {"firmware": "4.91",
                               "webman_version": "1.47.44",
                               "firmware_kind": "hen",
                               "hen_version": "3.5.0",
                               "cfw_markers": ["HEN", "HFW"]}}
    PS2_ISOS = games(folder("dev_hdd0", "PS2ISO",
                            [entry("Also Fine [SLES00008].iso",
                                   size=4 * GIB)]))
    BOTH_ISOS = games(folder("dev_hdd0", "PS3ISO",
                             [entry("Fine [BLES00007].iso", size=20 * GIB)]),
                      folder("dev_hdd0", "PS2ISO",
                             [entry("Also Fine [SLES00008].iso",
                                    size=4 * GIB)]))

    def test_isos_present_and_no_cobra_reported(self):
        produced = self.fire(builtin.cobra_disabled_with_isos,
                             merge(self.NO_COBRA, self.ISOS), severity="warn")
        self.assertIn("Cobra", produced[0].explanation)

    def test_the_console_saying_cobra_is_off_is_taken_at_its_word(self):
        self.fire(builtin.cobra_disabled_with_isos,
                  merge(self.COBRA, self.ISOS),
                  files={"system/root.html": "<b>Cobra:</b> OFF<br>"},
                  severity="error")

    def test_cobra_running_says_nothing(self):
        self.quiet(builtin.cobra_disabled_with_isos,
                   merge(self.COBRA, self.ISOS))

    def test_no_isos_means_nothing_to_warn_about(self):
        self.quiet(builtin.cobra_disabled_with_isos,
                   merge(self.NO_COBRA,
                         games(folder("dev_hdd0", "GAMES",
                                      [entry("BLES01718", kind="directory",
                                             size=0)]))))

    def test_a_system_page_that_did_not_parse_is_not_evidence(self):
        self.quiet(builtin.cobra_disabled_with_isos,
                   merge({"system": {"uptime": "3h"}}, self.ISOS))

    def test_the_nocobra_plugin_file_is_not_mistaken_for_a_setting(self):
        self.quiet(
            builtin.cobra_disabled_with_isos,
            merge(self.COBRA, self.ISOS),
            files={"system/root.html": "boot_plugins_nocobra.txt"})

    def test_a_hen_console_is_not_told_to_fit_cobra(self):
        self.quiet(builtin.cobra_disabled_with_isos,
                   merge(self.HEN_NO_COBRA, self.ISOS))

    def test_ps2_games_on_hen_still_need_cobra(self):
        produced = self.fire(builtin.cobra_disabled_with_isos,
                             merge(self.HEN_NO_COBRA, self.PS2_ISOS),
                             severity="warn")
        self.assertIn("Cobra", produced[0].explanation)

    def test_only_the_ps2_games_are_counted_on_hen(self):
        produced = self.fire(
            builtin.cobra_disabled_with_isos,
            merge(self.HEN_NO_COBRA, self.BOTH_ISOS),
            severity="warn")
        self.assertIn("one game", produced[0].explanation)

    def test_hen_saying_cobra_is_off_says_nothing_about_ps3_games(self):
        self.quiet(builtin.cobra_disabled_with_isos,
                   merge(self.HEN_NO_COBRA, self.ISOS),
                   files={"system/root.html": "<b>Cobra:</b> OFF<br>"})

    def test_custom_firmware_without_cobra_is_still_reported(self):
        produced = self.fire(builtin.cobra_disabled_with_isos,
                             merge(self.CFW_NO_COBRA, self.ISOS),
                             severity="warn")
        self.assertIn("Cobra", produced[0].explanation)

    def test_a_console_that_did_not_say_its_firmware_is_not_guessed_at(self):
        self.fire(builtin.cobra_disabled_with_isos,
                  merge(self.NO_COBRA, self.ISOS), severity="warn")


class TemperatureHighTests(RuleTestCase):

    def test_a_warm_console_is_worth_checking(self):
        produced = self.fire(builtin.temperature_high,
                             {"system": {"cpu_temp_c": 82.0,
                                         "rsx_temp_c": 70.0}},
                             severity="warn")
        self.assertIn("82 C", produced[0].explanation)

    def test_a_very_hot_console_is_a_problem(self):
        self.fire(builtin.temperature_high,
                  {"system": {"cpu_temp_c": 71.0, "rsx_temp_c": 88.0}},
                  severity="error")

    def test_a_normal_temperature_says_nothing(self):
        self.quiet(builtin.temperature_high,
                   {"system": {"cpu_temp_c": 61.0, "rsx_temp_c": 54.0}})

    def test_a_missing_temperature_is_not_a_cold_console(self):
        self.quiet(builtin.temperature_high, {"system": {"uptime": "3h 42m"}})
