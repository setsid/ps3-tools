"""The artefact set: the one thing analysis is allowed to see.

This is the boundary between the three layers. Collectors produce an ArtefactSet
and never interpret it. Analysis consumes an ArtefactSet and never touches a
socket. Presentation consumes both and never decides anything.

The point of the boundary is that an ArtefactSet built from a live console and
one loaded from a zip somebody emailed a year ago are the same object, so every
rule works on both without knowing which it has. That is what makes it possible
to run today's rules against last month's dump, and to analyse a console the
helper has no access to.

Everything in here is text. No artefact is binary, so a zip can be opened and
read by a person, and so loading one cannot depend on anything but the standard
library. A collector with binary data to record encodes it.

The schema is documented in docs/artefact-schema.md. This module is the
executable half of that document; if the two disagree, the document is wrong.
"""

import fnmatch
import json
import zipfile

SCHEMA_VERSION = 1

# Categories, in the order they are presented. Analysis must cope with any of
# them being absent: a run where the user unticked a box produces a set with no
# such category, which is not the same as a category that failed.
CATEGORY_KEYS = ("system", "storage", "plugins", "crash_reports", "accounts",
                 "network", "webman_config", "games")

ABSENT = "absent"


class ArtefactSet:
    def __init__(self, manifest=None, files=None):
        self.manifest = manifest or {}
        self.files = dict(files or {})
        self._facts_cache = {}

    # --- construction ------------------------------------------------------

    @classmethod
    def from_zip(cls, path):
        """Load a diagnostic somebody else produced.

        Tolerant on purpose: a zip from an older version of the tool, or one a
        user has opened and re-saved, still loads. Anything that is not valid
        UTF-8 text is skipped rather than failing the load, because one odd file
        must not cost the helper the other forty.
        """
        files = {}
        manifest = {}
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                try:
                    text = archive.read(info.filename).decode("utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                files[info.filename.replace("\\", "/")] = text
        raw_manifest = files.get("manifest.json")
        if raw_manifest:
            try:
                manifest = json.loads(raw_manifest)
            except ValueError:
                manifest = {}
        return cls(manifest, files)

    @classmethod
    def from_run(cls, run):
        """Build the set a live run just produced, before it is written out."""
        files = {}
        for result in run.results:
            for name, text in result.artefacts:
                files[name] = text
            if result.facts:
                files[f"{result.key}/facts.json"] = json.dumps(
                    result.facts, indent=2, sort_keys=True, default=str)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "tool": "ps3-diag",
            "tool_version": run.tool_version,
            "generated_local": run.generated_local,
            "generated_utc": run.generated_utc,
            "target": run.host,
            "duration_seconds": round(run.seconds, 2),
            "read_only": True,
            "identifiers_included": run.include_identifiers,
            "requested_categories": sorted(run.requested),
            "categories": [
                {
                    "key": result.key,
                    "title": result.title,
                    "status": result.status,
                    "seconds": round(result.seconds, 2),
                    "error": result.error,
                    "notes": list(result.notes),
                    "artefacts": [name for name, _text in result.artefacts],
                }
                for result in run.results
            ],
            "endpoints": [
                {
                    "path": attempt.path,
                    "status": attempt.status,
                    "bytes": len(attempt.body),
                    "content_type": attempt.content_type,
                    "seconds": round(attempt.elapsed, 2),
                    "error": attempt.error,
                }
                for attempt in run.attempts
            ],
        }
        return cls(manifest, files)

    # --- manifest ----------------------------------------------------------

    @property
    def host(self):
        return self.manifest.get("target", "")

    @property
    def tool_version(self):
        return self.manifest.get("tool_version", "")

    @property
    def generated_local(self):
        return self.manifest.get("generated_local", "")

    @property
    def generated_utc(self):
        return self.manifest.get("generated_utc", "")

    @property
    def duration_seconds(self):
        return self.manifest.get("duration_seconds", 0)

    @property
    def identifiers_included(self):
        return bool(self.manifest.get("identifiers_included", False))

    @property
    def endpoints(self):
        return list(self.manifest.get("endpoints", []))

    @property
    def categories(self):
        return list(self.manifest.get("categories", []))

    @property
    def requested_categories(self):
        return list(self.manifest.get("requested_categories", []))

    def category(self, key):
        for entry in self.categories:
            if entry.get("key") == key:
                return entry
        return {}

    def status(self, key):
        """One of ok, partial, failed, skipped, or absent when the category is
        not in this set at all. Rules must treat absent as "cannot say" rather
        than as "nothing found"."""
        return self.category(key).get("status", ABSENT)

    def collected(self, key):
        """True when this category produced something worth reasoning over.

        The single most common rule bug is drawing a conclusion from a category
        that failed: "no USB devices found" and "we never managed to look" lead
        to opposite advice. Rules call this first.
        """
        return self.status(key) in ("ok", "partial")

    def notes(self, key):
        return list(self.category(key).get("notes", []))

    def error(self, key):
        return self.category(key).get("error")

    # --- files -------------------------------------------------------------

    def facts(self, key):
        """The parsed facts for one category, or {} when there are none."""
        if key not in self._facts_cache:
            raw = self.files.get(f"{key}/facts.json")
            try:
                value = json.loads(raw) if raw else {}
            except ValueError:
                value = {}
            self._facts_cache[key] = value if isinstance(value, dict) else {}
        return self._facts_cache[key]

    def text(self, name, default=None):
        return self.files.get(name, default)

    def json(self, name, default=None):
        raw = self.files.get(name)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def names(self, pattern="*"):
        return sorted(name for name in self.files
                      if fnmatch.fnmatch(name, pattern))

    def put(self, name, text):
        """Used by the analysis layer to record its own output into the set.

        Analysis writes findings back in so the zip carries the conclusions
        alongside the evidence, and so a later run can see what an earlier rule
        set concluded.
        """
        self.files[name] = text
        self._facts_cache.pop(name.split("/", 1)[0], None)

    # --- convenience used by several rule sets -----------------------------

    def devices(self):
        """Mounted devices as storage recorded them. [] when not collected."""
        return list(self.facts("storage").get("devices", []))

    def game_folders(self):
        """Mapping of "dev_hdd0/PS3ISO" to its folder record. {} when absent."""
        return dict(self.facts("games").get("folders", {}))

    def game_entries(self):
        """Every title across every folder, each row carrying its device and
        folder so a rule does not have to walk the nesting itself."""
        rows = []
        for key, folder in self.game_folders().items():
            for entry in folder.get("entries", []):
                row = dict(entry)
                row["device"] = folder.get("device")
                row["folder"] = folder.get("folder")
                row["folder_key"] = key
                rows.append(row)
        return rows

    def boot_plugins(self):
        """boot_plugins.txt entries. [] when not collected."""
        return list(self.facts("plugins").get("boot_plugins.txt", []))

    def __repr__(self):
        return (f"<ArtefactSet {self.host or 'unknown'} "
                f"{len(self.files)} files "
                f"{len(self.categories)} categories>")
