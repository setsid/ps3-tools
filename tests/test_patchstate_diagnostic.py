"""Two things patch-state detection got wrong on a real console.

The console is the one at 192.168.50.95, and its game inventory is saved as
tests/fixtures/patches/console/games-facts.json. It had Black Ops II patched by
this very program, which the diagnostic then could not recognise, and it had
Modern Warfare 3 as a disc image with no title update, which the diagnostic
described by building a folder path out of the image's file name.

Entirely offline, like everything else in this folder. Nothing here opens a
socket or looks at a console.
"""

import json
import os
import unittest

# support puts the repo root on the path, so it has to come first: importing
# ps3diag before it means this module cannot be run on its own.
from support import ROOT  # noqa: F401
from ps3diag import patchstate
from ps3diag.artefacts import ArtefactSet

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE_FACTS = os.path.join(HERE, "fixtures", "patches", "console",
                             "games-facts.json")

# Read off the console after this program patched Black Ops II title update
# 1.19, and the stock sizes of the same three files before it did.
OURS_PATCHED = {"EBOOT.BIN": 6096288, "t6_ps3f.self": 6096288,
                "t6mp_ps3f.self": 7215488}
STOCK = {"EBOOT.BIN": 6108656, "t6_ps3f.self": 6108656,
         "t6mp_ps3f.self": 7254288}
# The standalone repository's own build, signed by a different setup.
REPO_PATCHED = {"EBOOT.BIN": 6095184, "t6mp_ps3f.self": 7214528}


def artefacts(facts, status="ok"):
    """An artefact set holding one games/facts.json and nothing else."""
    files = {"games/facts.json": json.dumps(facts)}
    manifest = {"schema_version": 1, "categories": [
        {"key": "games", "title": "Games", "status": status,
         "artefacts": sorted(files)}]}
    return ArtefactSet(manifest, files)


def inventory(entries, installed=None):
    facts = {"folders": {"dev_hdd0/GAMES": {
        "device": "dev_hdd0", "folder": "GAMES", "count": len(entries),
        "entries": entries}}}
    if installed is not None:
        facts["installed_titles"] = installed
    return artefacts(facts)


def folder(title_id):
    return {"name": title_id, "kind": "directory", "size": 0,
            "title_id": title_id}


def installed(title_id, sizes):
    return [{"title_id": title_id,
             "path": "/dev_hdd0/game/%s" % title_id,
             "files": [{"name": name, "kind": "file", "size": size}
                       for name, size in sizes.items()]}]


def console():
    with open(CONSOLE_FACTS, encoding="utf-8") as handle:
        return artefacts(json.load(handle))


def title(payload, fix_key):
    for record in payload["titles"]:
        if record["fix_key"] == fix_key:
            return record
    raise AssertionError("no %s title in %r" % (fix_key, payload["titles"]))


def binary(record, name):
    for item in record["binaries"]:
        if item["name"] == name:
            return item
    raise AssertionError("no binary named %s" % name)


def rule_ids(artefact_set):
    return [finding.rule_id for finding in patchstate.findings(artefact_set)]


class WhatThisToolItselfProduces(unittest.TestCase):
    """The sizes this program's own patcher leaves behind read as patched.

    They used not to: only the standalone repository's sizes were known, so a
    console this program had just fixed came back "cannot tell whether the PSN
    fix is applied", which is the one answer that was certainly wrong.
    """

    def test_all_three_patched_files_read_as_applied(self):
        record = title(patchstate.patch_state(inventory(
            [folder("BLES01717")],
            installed=installed("BLES01717", OURS_PATCHED))), "bo2")
        for name in OURS_PATCHED:
            item = binary(record, name)
            self.assertEqual(item["state"], patchstate.PATCHED, name)
            self.assertIn("applied", item["evidence"], name)

    def test_the_finding_says_applied_rather_than_cannot_tell(self):
        found = rule_ids(inventory([folder("BLES01717")],
                                   installed=installed("BLES01717",
                                                       OURS_PATCHED)))
        self.assertIn("bo2-psn-fix-applied", found)
        self.assertNotIn("bo2-psn-fix-state-unknown", found)
        self.assertNotIn("bo2-psn-fix-not-applied", found)

    def test_the_other_signing_route_still_reads_as_applied(self):
        # A file patched by the standalone repository is patched too, so both
        # sets of sizes have to answer, not whichever was added last.
        record = title(patchstate.patch_state(inventory(
            [folder("BLES01717")],
            installed=installed("BLES01717", REPO_PATCHED))), "bo2")
        for name in REPO_PATCHED:
            self.assertEqual(binary(record, name)["state"],
                             patchstate.PATCHED, name)

    def test_the_stock_sizes_still_read_as_not_applied(self):
        record = title(patchstate.patch_state(inventory(
            [folder("BLES01717")],
            installed=installed("BLES01717", STOCK))), "bo2")
        for name in STOCK:
            self.assertEqual(binary(record, name)["state"],
                             patchstate.UNPATCHED, name)

    def test_every_patched_size_belongs_to_the_file_it_was_read_from(self):
        # t6_ps3f.self happens to come out the same size as EBOOT.BIN, but it
        # is signed separately, so a size is only ever evidence for the file it
        # was actually observed on. EBOOT.BIN's other patched size is not
        # t6_ps3f.self's, and being handed it must not produce a guess.
        record = title(patchstate.patch_state(inventory(
            [folder("BLES01717")],
            installed=installed("BLES01717",
                                {"t6_ps3f.self": REPO_PATCHED["EBOOT.BIN"]}))),
            "bo2")
        self.assertEqual(binary(record, "t6_ps3f.self")["state"],
                         patchstate.UNKNOWN)

    def test_a_size_nobody_has_seen_is_still_unknown(self):
        record = title(patchstate.patch_state(inventory(
            [folder("BLES01717")],
            installed=installed("BLES01717", {"EBOOT.BIN": 6100000}))), "bo2")
        self.assertEqual(binary(record, "EBOOT.BIN")["state"],
                         patchstate.UNKNOWN)


class ATitleUpdateThatIsNotInstalled(unittest.TestCase):
    """The third state: the game is here, the files the fix patches are not."""

    def mw3_image(self):
        return {"name": "Call of Duty - Modern Warfare 3 (Europe).iso",
                "kind": "file", "size": 17179869184, "title_id": None}

    def test_a_game_with_no_update_is_its_own_state_and_not_an_error(self):
        record = title(patchstate.patch_state(
            inventory([self.mw3_image()], installed=[])), "mw3")
        self.assertEqual(record["update_state"], patchstate.NO_UPDATE)
        self.assertEqual(record["binaries"], [])
        self.assertIn("nothing to patch", record["note"])

    def test_the_finding_is_information_and_says_what_to_do_about_it(self):
        artefact_set = inventory([self.mw3_image()], installed=[])
        found = {finding.rule_id: finding
                 for finding in patchstate.findings(artefact_set)}
        self.assertIn("mw3-title-update-not-installed", found)
        finding = found["mw3-title-update-not-installed"]
        self.assertEqual(finding.severity, "info")
        self.assertIn("internet", finding.fix)
        self.assertTrue(finding.evidence)
        self.assertNotIn("mw3-psn-fix-state-unknown", found)
        self.assertNotIn("mw3-psn-fix-not-applied", found)

    def test_a_game_folder_with_no_update_reads_the_same_way(self):
        # Not only disc images: a GAMES folder copy of a game that has never
        # been run online is in exactly the same position.
        record = title(patchstate.patch_state(inventory(
            [folder("BLES01428")],
            installed=installed("BLES01717", STOCK))), "mw3")
        self.assertEqual(record["update_state"], patchstate.NO_UPDATE)

    def test_an_uncollected_game_folder_is_cannot_tell_and_not_no_update(self):
        # installed_titles missing altogether means nobody listed
        # /dev_hdd0/game. Saying "no title update is installed" from that would
        # be inventing a reading of a listing that was never taken.
        artefact_set = inventory([folder("BLES01428")])
        record = title(patchstate.patch_state(artefact_set), "mw3")
        self.assertEqual(record["update_state"], patchstate.UNKNOWN)
        item = binary(record, "default_mp.self")
        self.assertEqual(item["state"], patchstate.UNKNOWN)
        self.assertIn("was not listed", item["headline"])
        found = rule_ids(artefact_set)
        self.assertIn("mw3-psn-fix-state-unknown", found)
        self.assertNotIn("mw3-title-update-not-installed", found)

    def test_a_title_in_both_places_is_checked_file_by_file_as_before(self):
        record = title(patchstate.patch_state(inventory(
            [folder("BLES01428")],
            installed=installed("BLES01428", {"default_mp.self": 7581072}))),
            "mw3")
        self.assertEqual(record["update_state"], patchstate.INSTALLED)
        self.assertEqual(record["usrdir"], "/dev_hdd0/game/BLES01428/USRDIR")
        self.assertEqual(binary(record, "default_mp.self")["state"],
                         patchstate.UNPATCHED)


class NoDiscImageIsEverTreatedAsAFolder(unittest.TestCase):
    """A .iso is a file. A path with one in the middle of it is nonsense.

    The console reported "...Modern Warfare 3 (Europe).iso/default_mp.self was
    not listed", which reads to someone without technical knowledge as though
    their game were damaged.
    """

    def paths(self, payload):
        return json.dumps(payload)

    def test_the_real_console_never_produces_a_path_through_an_image(self):
        artefact_set = console()
        payload = patchstate.patch_state(artefact_set)
        text = json.dumps(payload)
        self.assertNotIn(".iso/", text)
        for finding in patchstate.findings(artefact_set):
            for field in (finding.title, finding.explanation, finding.fix):
                self.assertNotIn(".iso/", field)
            for line in finding.evidence:
                self.assertNotIn(".iso/", line)

    def test_an_image_with_no_title_id_names_no_folder_it_invented(self):
        record = title(patchstate.patch_state(console()), "mw3")
        self.assertEqual(record["update_state"], patchstate.NO_UPDATE)
        self.assertEqual(record["usrdir"], "")
        self.assertIn(".iso", record["location"])

    def test_the_real_console_reports_black_ops_ii_as_fixed(self):
        # The same console, the same run: the game it had patched is reported
        # as patched and the game it had not updated is reported as not needing
        # anything yet.
        artefact_set = console()
        record = title(patchstate.patch_state(artefact_set), "bo2")
        self.assertEqual(record["update_state"], patchstate.INSTALLED)
        for name in OURS_PATCHED:
            self.assertEqual(binary(record, name)["state"],
                             patchstate.PATCHED, name)
        found = rule_ids(artefact_set)
        self.assertIn("bo2-psn-fix-applied", found)
        self.assertIn("mw3-title-update-not-installed", found)


class TheAnswerStaysSerialisableAndSafe(unittest.TestCase):
    def test_the_payload_still_serialises_and_states_stay_known(self):
        allowed = {patchstate.PATCHED, patchstate.UNPATCHED,
                   patchstate.UNKNOWN, patchstate.MISSING}
        payload = patchstate.patch_state(console())
        json.dumps(payload)
        for record in payload["titles"]:
            self.assertIn(record["update_state"],
                          (patchstate.INSTALLED, patchstate.NO_UPDATE,
                           patchstate.UNKNOWN))
            for item in record["binaries"]:
                self.assertIn(item["state"], allowed)

    def test_a_broken_installed_titles_list_never_raises(self):
        for value in ("not a list", 17, [None, "text"], [{}],
                      [{"title_id": "BLES01717", "files": "no"}]):
            facts = {"folders": {"dev_hdd0/GAMES": {
                "device": "dev_hdd0", "folder": "GAMES",
                "entries": [folder("BLES01717")]}},
                "installed_titles": value}
            payload = patchstate.patch_state(artefacts(facts))
            json.dumps(payload)
            self.assertIsInstance(payload["titles"], list)


if __name__ == "__main__":
    unittest.main()
