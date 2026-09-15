"""Tests for patch-state detection.

Entirely offline. Nothing here talks to a console, and the only real artefact
set used is the sample zip in the fixtures folder.
"""

import base64
import json
import os
import unittest

# support puts the repo root on the path, so it has to come first: importing
# ps3diag before it means this module cannot be run on its own.
from support import FIXTURES as FIXTURE_ROOT, ROOT  # noqa: F401
from ps3diag import patchstate
from ps3diag.artefacts import ArtefactSet

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "patches")
SAMPLE = os.path.join(HERE, "fixtures", "sample-diagnostic.zip")

MW3_STOCK_SIZE = 7581072
MW3_STOCK_SHA1 = "1b02160e9daa943789ba5eda7258d1ce47d2df10"
BO2_MP_STOCK_SIZE = 7254288
BO2_MP_PATCHED_SIZE = 7214528


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def game_set(entries, installed=None, extra=None, status="ok"):
    """An artefact set carrying one GAMES folder and nothing else."""
    facts = {"folders": {"dev_hdd0/GAMES": {
        "device": "dev_hdd0", "folder": "GAMES", "count": len(entries),
        "entries": entries}}}
    if installed is not None:
        facts["installed_titles"] = installed
    files = {"games/facts.json": json.dumps(facts)}
    files.update(extra or {})
    manifest = {"schema_version": 1, "categories": [
        {"key": "games", "title": "Games", "status": status,
         "artefacts": sorted(files)}]}
    return ArtefactSet(manifest, files)


def directory(title_id):
    return {"name": title_id, "kind": "directory", "size": 0,
            "title_id": title_id}


def usrdir(title_id, files, path=None):
    return [{"title_id": title_id,
             "path": path or ("/dev_hdd0/game/%s/USRDIR" % title_id),
             "files": files}]


def binary(record, name):
    for item in record["binaries"]:
        if item["name"] == name:
            return item
    raise AssertionError("no binary named %s" % name)


class SelfHeaderTests(unittest.TestCase):
    def test_stock_mw3_header_reads_every_plaintext_field(self):
        header = patchstate.read_self_header(
            fixture("mw3/default_mp-bles01428-stock.selfhdr"))
        self.assertEqual(header["kind"], "self")
        self.assertEqual(header["key_revision"], 0x19)
        self.assertEqual(header["header_type_name"], "SELF")
        self.assertEqual(header["data_length"], 7578328)
        self.assertEqual(header["npdrm"]["app_type"], 0x20)
        self.assertEqual(header["npdrm"]["licence_type_name"], "free")
        self.assertEqual(header["npdrm"]["title_id"], "BLES01428")
        self.assertEqual(header["npdrm"]["content_id"],
                         "EP0002-BLES01428_00-MW3P000000000124")

    def test_bo2_eboot_and_multiplayer_differ_in_application_type(self):
        eboot = patchstate.read_self_header(
            fixture("bo2/eboot-bles01717-stock.selfhdr"))
        multiplayer = patchstate.read_self_header(
            fixture("bo2/t6mp-bles01717-stock.selfhdr"))
        self.assertEqual(eboot["npdrm"]["app_type"], 0x21)
        self.assertEqual(multiplayer["npdrm"]["app_type"], 0x20)
        self.assertEqual(multiplayer["data_length"], 14215768)

    def test_truncated_header_says_so_and_keeps_what_it_read(self):
        header = patchstate.read_self_header(fixture("bad/truncated.selfhdr"))
        self.assertEqual(header["kind"], "self")
        self.assertEqual(header["key_revision"], 0x19)
        self.assertNotIn("npdrm", header)
        self.assertTrue(any("captured" in note for note in header["notes"]))

    def test_rubbish_input_never_raises(self):
        for value in (fixture("bad/garbage.bin"), fixture("bad/empty.bin"),
                      fixture("bad/short.selfhdr"), b"SCE\x00", None, 17,
                      "not bytes", bytearray(b"\x00" * 4096)):
            header = patchstate.read_self_header(value)
            self.assertIn(header["kind"], ("self", "elf", "short", "other"))
            self.assertNotEqual(header["kind"], "self")

    def test_elf_is_recognised_as_unsigned(self):
        header = patchstate.read_self_header(fixture("bad/not-a-game.elf"))
        self.assertEqual(header["kind"], "elf")


class HeaderBytesTests(unittest.TestCase):
    def test_hex_and_base64_both_decode(self):
        raw = fixture("mw3/default_mp-bles01428-stock.selfhdr")
        self.assertEqual(patchstate.header_bytes({"header_hex": raw.hex()}),
                         raw)
        encoded = base64.b64encode(raw).decode("ascii")
        self.assertEqual(
            patchstate.header_bytes({"header_base64": encoded}), raw)

    def test_unreadable_encodings_give_nothing_rather_than_a_guess(self):
        self.assertIsNone(patchstate.header_bytes({"header_hex": "zz"}))
        self.assertIsNone(patchstate.header_bytes({}))
        self.assertIsNone(patchstate.header_bytes(None))
        self.assertIsNone(patchstate.header_bytes("string"))


class DecryptedTests(unittest.TestCase):
    """The path that reuses each patcher's own site finding."""

    def patcher(self, kind):
        module = patchstate.patcher_module(kind)
        if module is None:
            self.skipTest("the %s patcher is not on this machine" % kind)
        return module

    def test_mw3_stock_and_patched(self):
        self.patcher("mw3")
        stock = patchstate.decrypted_state(
            fixture("mw3/default_mp-stock.elf"), "mw3")
        patched = patchstate.decrypted_state(
            fixture("mw3/default_mp-patched.elf"), "mw3")
        self.assertEqual(stock["state"], patchstate.UNPATCHED)
        self.assertEqual(stock["confidence"], patchstate.HIGH)
        self.assertEqual(patched["state"], patchstate.PATCHED)

    def test_bo2_stock_and_patched(self):
        self.patcher("bo2")
        stock = patchstate.decrypted_state(fixture("bo2/spzm-stock.elf"),
                                           "bo2")
        patched = patchstate.decrypted_state(fixture("bo2/spzm-patched.elf"),
                                             "bo2")
        self.assertEqual(stock["state"], patchstate.UNPATCHED)
        self.assertEqual(patched["state"], patchstate.PATCHED)
        self.assertIn("nop", patched["evidence"])

    def test_a_patched_binary_with_padding_before_the_call(self):
        # Black Ops II's multiplayer binary, as it came back off the user's
        # own console: patched by this tool and read as unrecognised, while
        # the campaign binary beside it read correctly. The two are compiled
        # separately, and a nop between the size argument and the call is
        # ordinary output. Taking the first nop found the padding.
        self.patcher("bo2")
        stock = patchstate.decrypted_state(fixture("bo2/mp-stock.elf"), "bo2")
        patched = patchstate.decrypted_state(fixture("bo2/mp-patched.elf"),
                                             "bo2")
        self.assertEqual(stock["state"], patchstate.UNPATCHED)
        self.assertEqual(patched["state"], patchstate.PATCHED)
        # The call is the last of the instructions laid down, and the padding
        # sits two before it. Both are offered; which one this build uses is
        # settled against the offset recorded for the title.
        self.assertEqual(patched["offsets"], (0x114, 0x11C))
        self.assertEqual(stock["offset"], 0x11C)

    def test_the_stock_call_is_found_at_the_same_place_either_way(self):
        # The patched read has to land on the offset the stock read gives, or
        # the two disagree about where the site is and the file is reported as
        # a build the fix was not written for.
        self.patcher("bo2")
        for stock_name, patched_name in (
                ("bo2/spzm-stock.elf", "bo2/spzm-patched.elf"),
                ("bo2/mp-stock.elf", "bo2/mp-patched.elf")):
            with self.subTest(stock_name):
                stock = patchstate.decrypted_state(fixture(stock_name), "bo2")
                patched = patchstate.decrypted_state(fixture(patched_name),
                                                     "bo2")
                self.assertIn(stock["offset"], patched["offsets"])

    def test_the_wrong_binary_is_unknown_and_not_an_exception(self):
        self.patcher("bo2")
        for name, kind in (("bad/not-a-game.elf", "bo2"),
                           ("bad/garbage.bin", "mw3"),
                           ("bad/empty.bin", "bo2")):
            answer = patchstate.decrypted_state(fixture(name), kind)
            self.assertEqual(answer["state"], patchstate.UNKNOWN)
            self.assertTrue(answer["evidence"])

    def test_bad_input_never_raises(self):
        for value in (None, 17, "text", b"", bytearray(b"\xff" * 64)):
            for kind in ("bo2", "mw3", "nonsense"):
                answer = patchstate.decrypted_state(value, kind)
                self.assertIn(answer["state"],
                              (patchstate.UNKNOWN, patchstate.PATCHED,
                               patchstate.UNPATCHED))


class TitleMatchingTests(unittest.TestCase):
    def test_sample_diagnostic_finds_both_titles(self):
        payload = patchstate.patch_state(ArtefactSet.from_zip(SAMPLE))
        found = {(row["fix_key"], row["title_id"]) for row in payload["titles"]}
        self.assertIn(("bo2", "BLES01718"), found)
        self.assertIn(("bo2", "BLUS31140"), found)
        self.assertIn(("mw3", "BLES01428"), found)

    def test_every_published_release_is_matched_on_its_title_id(self):
        # The title IDs the diagnostic half recognises and the ones the patcher
        # half recognises are the same set, written out in both places because
        # the read-only half is not allowed to import the other one. A drift
        # between them shows up as a game the report cannot name.
        for spec in patchstate.TITLES:
            for title_id in spec.title_ids:
                found, matched, how = patchstate._match(directory(title_id))
                self.assertIs(found, spec, title_id)
                self.assertEqual(matched, title_id)
                self.assertEqual(how, "title ID")

    def test_a_release_nobody_has_confirmed_is_recognised_but_not_verified(self):
        record = patchstate.patch_state(
            game_set([directory("BLES01430")]))["titles"][0]
        self.assertEqual(record["fix_key"], "mw3")
        self.assertEqual(record["title_id"], "BLES01430")
        self.assertFalse(record["verified"])

    def test_a_confirmed_release_says_so(self):
        record = patchstate.patch_state(
            game_set([directory("BLES01428")]))["titles"][0]
        self.assertTrue(record["verified"])
        self.assertTrue(patchstate.is_verified(patchstate.MW3, "BLES01428"))
        self.assertFalse(patchstate.is_verified(patchstate.MW3, "BLES01430"))

    def test_the_verified_releases_are_a_subset_of_the_recognised_ones(self):
        for spec in patchstate.TITLES:
            self.assertTrue(set(spec.verified_title_ids)
                            <= set(spec.title_ids), spec.key)
            self.assertLess(len(spec.verified_title_ids),
                            len(spec.title_ids), spec.key)

    def test_a_console_without_either_title_reports_none(self):
        payload = patchstate.patch_state(game_set([directory("BLES01702")]))
        self.assertEqual(payload["titles"], [])
        self.assertTrue(any("neither title" in note
                            for note in payload["notes"]))

    def test_a_folder_named_for_the_game_is_matched_on_its_name(self):
        entry = {"name": "Black Ops II backup", "kind": "directory",
                 "size": 0, "title_id": None}
        payload = patchstate.patch_state(game_set([entry]))
        self.assertEqual(len(payload["titles"]), 1)
        self.assertEqual(payload["titles"][0]["matched_by"], "name")
        self.assertIn("matched on the name", payload["titles"][0]["note"])

    def test_a_title_that_is_only_an_image_says_so(self):
        entry = {"name": "Call of Duty - Modern Warfare 3 [BLES01428].iso",
                 "kind": "file", "size": 25769803776, "title_id": "BLES01428"}
        record = patchstate.patch_state(game_set([entry]))["titles"][0]
        self.assertIn("image", record["note"])
        self.assertEqual(record["usrdir"], "/dev_hdd0/game/BLES01428/USRDIR")

    def test_an_uncollected_inventory_draws_no_conclusion(self):
        payload = patchstate.patch_state(
            game_set([directory("BLES01428")], status="failed"))
        self.assertFalse(payload["games_collected"])
        self.assertEqual(payload["titles"], [])


class BinaryStateTests(unittest.TestCase):
    def record(self, title_id, files, extra=None):
        artefacts = game_set([directory(title_id)],
                             installed=usrdir(title_id, files), extra=extra)
        return patchstate.patch_state(artefacts)["titles"][0]

    def test_without_a_listing_every_file_is_unknown(self):
        record = patchstate.patch_state(
            game_set([directory("BLES01428")]))["titles"][0]
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.UNKNOWN)
        self.assertIn("no listing", item["evidence"])

    def test_stock_size_reads_as_unpatched(self):
        record = self.record("BLES01428", [
            {"name": "default_mp.self", "size": MW3_STOCK_SIZE}])
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.UNPATCHED)
        self.assertEqual(item["confidence"], patchstate.LOW)
        self.assertIn("stock size", item["evidence"])
        self.assertIn("no keys", item["evidence"])

    def test_stock_hash_reads_as_unpatched_with_confidence(self):
        record = self.record("BLES01428", [
            {"name": "default_mp.self", "size": 12, "sha1": MW3_STOCK_SHA1}])
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.UNPATCHED)
        self.assertEqual(item["confidence"], patchstate.HIGH)

    def test_published_patched_size_reads_as_patched(self):
        record = self.record("BLES01717", [
            {"name": "t6mp_ps3f.self", "size": BO2_MP_PATCHED_SIZE}])
        item = binary(record, "t6mp_ps3f.self")
        self.assertEqual(item["state"], patchstate.PATCHED)
        self.assertEqual(item["confidence"], patchstate.LOW)
        self.assertIn("not proof", item["evidence"])

    def test_a_file_that_is_not_there_is_missing(self):
        record = self.record("BLES01717", [
            {"name": "t6mp_ps3f.self", "size": BO2_MP_STOCK_SIZE}])
        self.assertEqual(binary(record, "EBOOT.BIN")["state"],
                         patchstate.MISSING)
        self.assertEqual(binary(record, "t6mp_ps3f.self")["state"],
                         patchstate.UNPATCHED)

    def test_an_unfamiliar_size_is_unknown_rather_than_unpatched(self):
        record = self.record("BLES01428", [
            {"name": "default_mp.self", "size": 1234567}])
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.UNKNOWN)
        self.assertIn("reference", item["evidence"])

    def test_a_resigned_file_of_a_known_build_is_unknown_and_says_why(self):
        header = fixture("mw3/default_mp-bles01428-stock.selfhdr")
        record = self.record("BLES01428", [
            {"name": "default_mp.self", "size": MW3_STOCK_SIZE - 9000,
             "header_hex": header.hex()}])
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.UNKNOWN)
        self.assertIn("re-signed", item["evidence"])
        self.assertEqual(item["header"]["npdrm"]["app_type"], 0x20)

    def test_the_wrong_application_type_is_reported_as_a_problem(self):
        header = fixture("mw3/default_mp-bles01428-wrong-app-type.selfhdr")
        record = self.record("BLES01428", [
            {"name": "default_mp.self", "size": MW3_STOCK_SIZE,
             "header_hex": header.hex()}])
        item = binary(record, "default_mp.self")
        self.assertIn("header_problem", item)
        self.assertIn("0x21", item["header_problem"])

    def test_a_garbage_header_does_not_change_the_answer(self):
        record = self.record("BLES01428", [
            {"name": "default_mp.self", "size": MW3_STOCK_SIZE,
             "header_hex": fixture("bad/garbage.bin").hex()}])
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.UNPATCHED)
        self.assertNotIn("header_problem", item)

    def test_a_decrypted_elf_left_in_place_is_read_properly(self):
        if patchstate.patcher_module("mw3") is None:
            self.skipTest("the mw3 patcher is not on this machine")
        elf = fixture("mw3/default_mp-patched.elf")
        record = self.record("BLES01428", [
            {"name": "default_mp.self", "size": len(elf),
             "header_hex": elf.hex()}])
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.PATCHED)
        self.assertEqual(item["confidence"], patchstate.HIGH)
        self.assertIn("decrypted ELF", item["evidence"])

    def test_the_campaign_binary_is_reported_as_untouched_by_the_fix(self):
        record = self.record("BLES01428", [
            {"name": "default.self", "size": 123},
            {"name": "default_mp.self", "size": MW3_STOCK_SIZE}])
        item = binary(record, "default.self")
        self.assertFalse(item["affected"])
        self.assertIn("does not touch", item["evidence"])

    def test_an_ftp_listing_artefact_is_read_when_there_are_no_facts(self):
        listing = ("drwxrwxrwx 1 root root        0 Sep 06 19:51 .\n"
                   "-rw-rw-rw- 1 root root  7581072 Sep 06 19:51 "
                   "default_mp.self\n"
                   "-rw-rw-rw- 1 root root  5000000 Sep 06 19:51 "
                   "default.self\n")
        artefacts = game_set(
            [directory("BLES01428")],
            extra={"games/game-BLES01428-USRDIR.txt": listing})
        record = patchstate.patch_state(artefacts)["titles"][0]
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.UNPATCHED)
        self.assertEqual(item["size"], MW3_STOCK_SIZE)
        self.assertIn("USRDIR.txt", record["listing_source"])


class FindingTests(unittest.TestCase):
    def findings(self, artefacts):
        return {finding.rule_id: finding
                for finding in patchstate.findings(artefacts)}

    def test_an_unpatched_title_names_the_files_and_the_repository(self):
        artefacts = game_set(
            [directory("BLES01717")],
            installed=usrdir("BLES01717", [
                {"name": "t6mp_ps3f.self", "size": BO2_MP_STOCK_SIZE},
                {"name": "t6_ps3f.self", "size": 6108656},
                {"name": "EBOOT.BIN", "size": 6108656}]))
        finding = self.findings(artefacts)["bo2-psn-fix-not-applied"]
        self.assertEqual(finding.severity, "warn")
        self.assertEqual(finding.category, "games")
        for name in ("EBOOT.BIN", "t6_ps3f.self", "t6mp_ps3f.self"):
            self.assertIn(name, finding.fix)
        self.assertIn("github.com/setsid/bo2-ps3-psn-freeze-fix", finding.fix)
        self.assertTrue(all("/dev_hdd0/game/BLES01717/USRDIR" in line
                            for line in finding.evidence))

    def test_an_unknown_title_is_information_and_admits_it_cannot_tell(self):
        artefacts = game_set([directory("BLES01428")])
        finding = self.findings(artefacts)["mw3-psn-fix-state-unknown"]
        self.assertEqual(finding.severity, "info")
        self.assertIn("cannot be read", finding.explanation)
        self.assertNotIn("mw3-psn-fix-not-applied", self.findings(artefacts))

    def test_a_patched_title_is_information_and_claims_no_more_than_that(self):
        if patchstate.patcher_module("mw3") is None:
            self.skipTest("the mw3 patcher is not on this machine")
        elf = fixture("mw3/default_mp-patched.elf")
        artefacts = game_set(
            [directory("BLES01428")],
            installed=usrdir("BLES01428", [
                {"name": "default_mp.self", "size": len(elf),
                 "header_hex": elf.hex()}]))
        found = self.findings(artefacts)
        self.assertIn("mw3-psn-fix-applied", found)
        self.assertEqual(found["mw3-psn-fix-applied"].severity, "info")
        self.assertIn("inference", found["mw3-psn-fix-applied"].explanation)
        self.assertNotIn("mw3-psn-fix-not-applied", found)

    def test_two_patched_files_and_one_unreadable_is_not_called_applied(self):
        """The Black Ops II repository publishes no patched size for
        t6_ps3f.self, so the honest answer for that file is unknown and the
        title as a whole cannot be called done."""
        artefacts = game_set(
            [directory("BLES01717")],
            installed=usrdir("BLES01717", [
                {"name": "EBOOT.BIN", "size": 6095184},
                {"name": "t6_ps3f.self", "size": 6095184},
                {"name": "t6mp_ps3f.self", "size": BO2_MP_PATCHED_SIZE}]))
        record = patchstate.patch_state(artefacts)["titles"][0]
        self.assertEqual(binary(record, "EBOOT.BIN")["state"],
                         patchstate.PATCHED)
        self.assertEqual(binary(record, "t6mp_ps3f.self")["state"],
                         patchstate.PATCHED)
        self.assertEqual(binary(record, "t6_ps3f.self")["state"],
                         patchstate.UNKNOWN)
        found = self.findings(artefacts)
        self.assertIn("bo2-psn-fix-state-unknown", found)
        self.assertNotIn("bo2-psn-fix-applied", found)
        self.assertNotIn("bo2-psn-fix-not-applied", found)

    def test_a_file_signed_the_wrong_way_is_an_error(self):
        header = fixture("mw3/default_mp-bles01428-wrong-app-type.selfhdr")
        artefacts = game_set(
            [directory("BLES01428")],
            installed=usrdir("BLES01428", [
                {"name": "default_mp.self", "size": MW3_STOCK_SIZE,
                 "header_hex": header.hex()}]))
        finding = self.findings(artefacts)["mw3-self-wrong-app-type"]
        self.assertEqual(finding.severity, "error")

    def test_one_finding_per_fix_even_when_the_game_is_installed_twice(self):
        artefacts = game_set([directory("BLES01717"), directory("BLES01718")])
        rules = [finding.rule_id for finding in patchstate.findings(artefacts)]
        self.assertEqual(rules.count("bo2-psn-fix-state-unknown"), 1)

    def test_the_sample_diagnostic_produces_only_honest_findings(self):
        artefacts = ArtefactSet.from_zip(SAMPLE)
        for finding in patchstate.findings(artefacts):
            self.assertIn(finding.severity, ("error", "warn", "info"))
            self.assertTrue(finding.title)
            self.assertTrue(finding.evidence)

    def test_an_empty_set_produces_nothing_rather_than_an_error(self):
        self.assertEqual(patchstate.findings(ArtefactSet()), [])


class RobustnessTests(unittest.TestCase):
    """Nothing in this module may raise, whatever it is handed."""

    def broken_sets(self):
        yield ArtefactSet()
        yield ArtefactSet({"categories": "not a list"}, {})
        yield ArtefactSet({"categories": [{"key": "games", "status": "ok"}]},
                          {"games/facts.json": "{not json"})
        yield game_set(["not a dict", None, 42, {}])
        yield game_set([{"name": None, "kind": None, "title_id": 17}])
        yield game_set([directory("BLES01428")],
                       installed=[{"title_id": "BLES01428",
                                   "files": ["not a dict", None]}])
        yield game_set([directory("BLES01428")],
                       installed=[{"title_id": "BLES01428", "files": [
                           {"name": "default_mp.self", "size": "big",
                            "sha1": 12, "header_hex": "not hex"}]}])
        yield game_set([directory("BLES01428")],
                       extra={"games/game-BLES01428-USRDIR.txt": "\x00rubbish"})

    def test_patch_state_never_raises_and_stays_serialisable(self):
        for artefacts in self.broken_sets():
            payload = patchstate.patch_state(artefacts)
            self.assertEqual(payload["schema_version"], 1)
            self.assertIsInstance(payload["titles"], list)
            json.dumps(payload)

    def test_findings_never_raise(self):
        for artefacts in self.broken_sets():
            for finding in patchstate.findings(artefacts):
                self.assertTrue(finding.rule_id)

    def test_every_state_is_one_of_the_four(self):
        allowed = {patchstate.PATCHED, patchstate.UNPATCHED,
                   patchstate.UNKNOWN, patchstate.MISSING}
        payload = patchstate.patch_state(ArtefactSet.from_zip(SAMPLE))
        for record in payload["titles"]:
            for item in record["binaries"]:
                self.assertIn(item["state"], allowed)
                self.assertIn(item["confidence"],
                              (patchstate.HIGH, patchstate.LOW))
                self.assertTrue(item["evidence"])


class PayloadTests(unittest.TestCase):
    def test_the_payload_has_the_shape_the_schema_reserves(self):
        artefacts = ArtefactSet.from_zip(SAMPLE)
        payload = json.loads(patchstate.patch_state_json(artefacts))
        self.assertEqual(payload["schema_version"], 1)
        for record in payload["titles"]:
            for key in ("title_id", "title", "location", "repo", "advice",
                        "binaries"):
                self.assertIn(key, record)
            for item in record["binaries"]:
                for key in ("name", "path", "size", "state", "evidence",
                            "confidence"):
                    self.assertIn(key, item)

    def test_recording_writes_the_reserved_artefact_name(self):
        artefacts = ArtefactSet.from_zip(SAMPLE)
        patchstate.record_into(artefacts)
        self.assertIn("patches/patch-state.json", artefacts.names())
        self.assertEqual(
            artefacts.json("patches/patch-state.json")["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()


class BundledPatchers(unittest.TestCase):
    """The two fix scripts have to be found with nothing set up.

    They used not to be: they lived in sibling checkouts that never shipped, so
    on a real console every file on the patcher screen came back "not
    recognised" while the console was perfectly fine. The copies in
    tools/patchers are what an exe carries, and a source checkout has to find
    the same ones by the same route.
    """

    def setUp(self):
        self._saved = dict(patchstate._patchers)
        patchstate._patchers.clear()
        self.addCleanup(self._restore)
        for name in patchstate.PATCHER_ENV.values():
            if name in os.environ:
                value = os.environ.pop(name)
                self.addCleanup(os.environ.__setitem__, name, value)

    def _restore(self):
        patchstate._patchers.clear()
        patchstate._patchers.update(self._saved)

    def test_both_patchers_resolve_with_no_environment_set(self):
        for kind in ("bo2", "mw3"):
            module = patchstate.patcher_module(kind)
            self.assertIsNotNone(module, kind)
            self.assertTrue(os.path.isfile(module.__ps3diag_path__))

    def test_they_come_from_the_copies_that_ship(self):
        wanted = os.path.join(ROOT, "tools", "patchers")
        for kind, filename in patchstate.PATCHER_FILES.items():
            module = patchstate.patcher_module(kind)
            self.assertEqual(os.path.abspath(module.__ps3diag_path__),
                             os.path.join(wanted, filename))

    def test_each_one_exposes_the_functions_the_tool_calls(self):
        # Named one at a time rather than checked with a loop: these are the
        # exact attributes patchstate and flow reach for, and a rename in
        # either script has to fail here rather than on somebody's console.
        bo2 = patchstate.patcher_module("bo2")
        for name in ("find_call", "find_construct", "find_format_string",
                     "u32", "NOP", "LI_R4_1B8"):
            self.assertTrue(hasattr(bo2, name), name)
        mw3 = patchstate.patcher_module("mw3")
        for name in ("find_site", "PATCHED"):
            self.assertTrue(hasattr(mw3, name), name)

    def test_the_environment_override_still_wins(self):
        os.environ[patchstate.PATCHER_ENV["mw3"]] = os.path.join(
            ROOT, "tools", "patchers", "patch-bo2.py")
        patchstate._patchers.clear()
        module = patchstate.patcher_module("mw3")
        self.assertTrue(module.__ps3diag_path__.endswith("patch-bo2.py"))


class AMissingPatcher(unittest.TestCase):
    def setUp(self):
        self._saved = dict(patchstate._patchers)
        patchstate._patchers.clear()
        patchstate._patchers.update({"bo2": None, "mw3": None})
        self.addCleanup(self._restore)

    def _restore(self):
        patchstate._patchers.clear()
        patchstate._patchers.update(self._saved)

    def test_it_is_flagged_as_this_program_s_fault_and_names_the_file(self):
        out = patchstate.decrypted_state(b"\x00" * 64, "bo2")
        self.assertEqual(out["state"], patchstate.UNKNOWN)
        self.assertTrue(out["tool_fault"])
        self.assertEqual(out["missing"], "patch-bo2.py")
        self.assertIn("patch-bo2.py", out["evidence"])

    def test_a_patcher_that_is_there_carries_no_fault_flag(self):
        self._restore()
        patchstate._patchers.clear()
        out = patchstate.decrypted_state(b"\x00" * 64, "mw3")
        self.assertFalse(out.get("tool_fault"))


class TheUpdateTheFixWasVerifiedOn(unittest.TestCase):
    """titles.verified_update_for. A fact about this program, not about Sony.

    The number this returns is the build somebody watched the fix work on. It
    happens to be the newest update for both games today, which is exactly why
    it is written down separately: the day another update ships, a check made
    against the manifest would start telling every user their console is wrong,
    and a check made against this would go on saying the only thing anyone here
    can support.
    """

    def test_the_two_verified_releases_have_the_confirmed_build(self):
        from ps3tools import titles
        self.assertEqual(titles.verified_update_for("BLES01717"), "1.19")
        self.assertEqual(titles.verified_update_for("BLES01428"), "1.24")

    def test_it_is_written_down_and_not_read_off_the_manifest(self):
        from ps3tools import titles
        for key in ("bo2", "mw3"):
            config = titles.TITLES[key]
            self.assertIn("verified_update", config)
            # Deliberately two keys with the same value today. They answer
            # different questions and one of them is allowed to move on its
            # own; a single key would make that impossible to express.
            self.assertIn("latest_update", config)

    def test_a_release_nobody_has_confirmed_has_no_expected_version(self):
        from ps3tools import titles
        for title_id in ("BLES01430", "NPEB01204", "BLJM60548"):
            self.assertTrue(titles.is_recognised(title_id), title_id)
            self.assertFalse(titles.is_verified(title_id), title_id)
            # None means "nothing to compare against", never "out of date".
            self.assertIsNone(titles.verified_update_for(title_id), title_id)

    def test_a_title_id_that_is_neither_game_says_nothing(self):
        from ps3tools import titles
        self.assertIsNone(titles.verified_update_for("BLES01702"))
        self.assertIsNone(titles.verified_update_for(""))
        self.assertIsNone(titles.verified_update_for(None))

    def test_every_verified_release_has_a_package_hash_for_that_build(self):
        # The two tables are independent and a verified update with no entry in
        # the SKU's update list would be a version this tool claims to have
        # confirmed and cannot recognise.
        from ps3tools import titles
        for title_id in titles.VERIFIED_TITLE_IDS:
            wanted = titles.verified_update_for(title_id)
            self.assertIn(wanted, titles.sku_for(title_id)["updates"],
                          title_id)
