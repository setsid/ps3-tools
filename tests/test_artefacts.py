"""The layer boundary: a set from a live run and one from a zip behave alike."""

import json
import os
import tempfile

from support import FixtureCase, fixture_path
from ps3diag.artefacts import ArtefactSet


def minimal(status="ok", facts=None, files=None):
    manifest = {
        "schema_version": 1,
        "tool_version": "1.0",
        "target": "192.168.1.42",
        "generated_local": "2026-09-14T14:32:10",
        "duration_seconds": 12.5,
        "identifiers_included": False,
        "categories": [{"key": "system", "title": "System and firmware",
                        "status": status, "notes": ["a note"], "error": None,
                        "artefacts": []}],
        "endpoints": [{"path": "/", "status": 200, "bytes": 10,
                       "content_type": "text/html", "error": None}],
    }
    payload = dict(files or {})
    if facts is not None:
        payload["system/facts.json"] = json.dumps(facts)
    return ArtefactSet(manifest, payload)


class Accessors(FixtureCase):
    def test_manifest_fields(self):
        artefacts = minimal()
        self.assertEqual(artefacts.host, "192.168.1.42")
        self.assertEqual(artefacts.tool_version, "1.0")
        self.assertEqual(artefacts.duration_seconds, 12.5)
        self.assertFalse(artefacts.identifiers_included)
        self.assertEqual(len(artefacts.endpoints), 1)

    def test_status_and_collected(self):
        self.assertEqual(minimal("ok").status("system"), "ok")
        self.assertTrue(minimal("ok").collected("system"))
        self.assertTrue(minimal("partial").collected("system"))
        self.assertFalse(minimal("failed").collected("system"))

    def test_a_category_that_is_not_there_is_absent_not_failed(self):
        artefacts = minimal()
        self.assertEqual(artefacts.status("games"), "absent")
        self.assertFalse(artefacts.collected("games"))
        self.assertEqual(artefacts.facts("games"), {})

    def test_notes_and_errors(self):
        self.assertEqual(minimal().notes("system"), ["a note"])
        self.assertIsNone(minimal().error("system"))

    def test_facts_parse_once_and_survive_being_broken(self):
        self.assertEqual(minimal(facts={"firmware": "4.93"}).facts("system"),
                         {"firmware": "4.93"})
        broken = ArtefactSet({}, {"system/facts.json": "{not json"})
        self.assertEqual(broken.facts("system"), {})

    def test_names_matches_a_pattern(self):
        artefacts = minimal(files={"games/a.txt": "x", "system/b.html": "y"})
        self.assertEqual(artefacts.names("games/*"), ["games/a.txt"])

    def test_put_writes_analysis_output_back_in(self):
        artefacts = minimal()
        artefacts.put("analysis/findings.json", "[]")
        self.assertEqual(artefacts.text("analysis/findings.json"), "[]")
        self.assertEqual(artefacts.json("analysis/findings.json"), [])


class Convenience(FixtureCase):
    def test_game_entries_carry_their_device_and_folder(self):
        artefacts = ArtefactSet({}, {"games/facts.json": json.dumps({
            "folders": {"dev_hdd0/PS3ISO": {
                "device": "dev_hdd0", "folder": "PS3ISO",
                "entries": [{"name": "a.iso", "size": 1}]}}})})
        rows = artefacts.game_entries()
        self.assertEqual(rows[0]["device"], "dev_hdd0")
        self.assertEqual(rows[0]["folder"], "PS3ISO")
        self.assertEqual(rows[0]["folder_key"], "dev_hdd0/PS3ISO")

    def test_the_convenience_accessors_are_empty_not_missing(self):
        artefacts = ArtefactSet({}, {})
        self.assertEqual(artefacts.devices(), [])
        self.assertEqual(artefacts.game_folders(), {})
        self.assertEqual(artefacts.game_entries(), [])
        self.assertEqual(artefacts.boot_plugins(), [])


class SavedDiagnostic(FixtureCase):
    """Loading a zip with no console present is the whole point of the split."""

    def setUp(self):
        self.artefacts = ArtefactSet.from_zip(
            fixture_path("sample-diagnostic.zip"))

    def test_it_loads(self):
        self.assertEqual(self.artefacts.host, "192.168.1.42")
        self.assertTrue(self.artefacts.categories)

    def test_the_categories_came_back(self):
        for key in ("system", "storage", "plugins", "crash_reports",
                    "network", "webman_config", "games"):
            self.assertNotEqual(self.artefacts.status(key), "absent", key)

    def test_the_facts_came_back(self):
        system = self.artefacts.facts("system")
        self.assertEqual(system["firmware"], "4.93")
        self.assertEqual(system["model"], "CECH-2503B")
        self.assertEqual(system["model_sales_region"],
                         "United Kingdom and Ireland")

    def test_the_game_inventory_came_back_with_regions(self):
        rows = self.artefacts.game_entries()
        self.assertTrue(rows)
        europe = [row for row in rows if row.get("region") == "Europe"]
        self.assertTrue(europe)

    def test_identifiers_in_the_saved_file_are_redacted(self):
        joined = "\n".join(self.artefacts.files.values())
        self.assertNotIn("00000001006A0200A1B2C3D4E5F60718", joined)
        self.assertNotIn("00:1F:A7:3C:9B:2E", joined)
        self.assertIn("[IDPS-", joined)

    def test_a_zip_that_is_not_ours_loads_without_exploding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "other.zip")
            import zipfile
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("readme.txt", "nothing to do with ps3-diag")
            artefacts = ArtefactSet.from_zip(path)
            self.assertEqual(artefacts.categories, [])
            self.assertEqual(artefacts.status("system"), "absent")

    def test_binary_members_are_skipped_rather_than_failing_the_load(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "mixed.zip")
            import zipfile
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", '{"target": "1.2.3.4"}')
                archive.writestr("blob.bin", b"\xff\xfe\x00\x01")
            artefacts = ArtefactSet.from_zip(path)
            self.assertEqual(artefacts.host, "1.2.3.4")
            self.assertNotIn("blob.bin", artefacts.files)
