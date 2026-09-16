"""One collector per category. Each one is independent and none may abort a run.

Every collector follows the same contract: it is handed a Context, it returns a
CollectorResult, and it does not raise. A collector that finds nothing is not a
failure, it is a result with no facts and a note saying what was tried. That
distinction matters to the helper reading the zip, who needs to tell "this
console has no crash reports" apart from "we never managed to ask".

Raw responses are kept whatever happens to the parsing. On a webMAN version
nobody here has seen the parsers will shrug, and the raw text is then the only
thing in the zip worth having, so it must survive.
"""

import json
import re
import time

from . import parsers
from .parsers import html_to_text, human_size
from .regioncodes import describe_model, describe_name, tally_regions

# Optional. A build without it collects everything else and identifies images by
# their file name, which is what this falls back to on a console that will not
# answer a ranged read anyway.
try:
    from . import isoreader
except ImportError:
    isoreader = None

OK = "ok"
PARTIAL = "partial"
FAILED = "failed"
SKIPPED = "skipped"

# The eight folders webMAN looks in. Checked on every mounted device, because a
# second drive holding the games is the normal arrangement rather than an odd
# one.
GAME_FOLDERS = ("PS3ISO", "PS2ISO", "PSXISO", "PSPISO", "BDISO", "DVDISO",
                "GAMES", "PKG")

# Where webMAN's own pages have lived across versions. Probed in order and the
# ones that 404 cost a request each, which is why the list is short.
ROOT_PATHS = ("/", "/index.ps3")
# /sysinfo.ps3 and /info.ps3 were probed here until a real console answered 501
# to both. They do not exist on webMAN 1.47.48q and are not requested.
SYSTEM_PATHS = ("/cpursx.ps3",)

MAX_CRASH_FILES = 20
MAX_GAME_ENTRIES = 2000
# Installed titles get a listing each, so this is a request budget rather than a
# storage one. Sixty covers any normal console and stops a machine with a
# thousand folders in /dev_hdd0/game from turning one category into the run.
MAX_INSTALLED_TITLES = 60
# One listing per user folder, so this is a request budget for the same reason
# as the one above: webMANftpd opens a fresh data connection per listing and
# hangs up when it has had too many of them in quick succession. Sixteen is
# well past the number of local users anybody sets up, so reaching it means
# something other than the console has been writing under /dev_hdd0/home.
MAX_USER_FOLDERS = 16

# A directory under /dev_hdd0/game named like a title. Patches, homebrew and
# webMAN's own folders live there too and are not worth a listing each.
TITLE_DIR = re.compile(r"^[A-Z]{4}\d{5}$")

# Where the console keeps one folder per local user.
HOME_ROOT = "/dev_hdd0/home"

# A user folder is eight digits and nothing else, which is how the console
# names them from 00000001 up. Anything else under /dev_hdd0/home was put there
# by something other than the console, so it is recorded and not descended
# into.
USER_DIR = re.compile(r"^\d{8}$")

# The file the Black Ops 1 stats fix reads the account ID out of. Named here so
# that the collector can say whether it is there without ever opening it.
NP_CACHE = "np_cache.dat"


class RunExpired(Exception):
    """The whole-run timeout went by. Collectors turn this into a result."""


class RunStopped(Exception):
    """The user pressed Stop.

    Raised from inside the long loops rather than only between collectors. A
    Stop that waits for the game inventory to finish is not a Stop: on a
    console with two dozen disc images that wait is minutes long, and the one
    thing the user wanted was for it to end now.

    It is not a failure. A collector that catches this keeps everything it had
    already collected and says it was stopped.
    """


class CollectorResult:
    def __init__(self, key, title):
        self.key = key
        self.title = title
        self.status = SKIPPED
        self.facts = {}
        self.artefacts = []
        self.notes = []
        # Listings that failed for a reason other than not existing. Kept apart
        # from notes so a collector can tell "there is nothing there" from
        # "I could not look", which are opposite conclusions.
        self.faults = []
        self.error = None
        self.seconds = 0.0
        # Set when the user stopped this category part way through. Kept apart
        # from the status because a stopped category still holds everything it
        # had collected, and that is not the same thing as a failure.
        self.stopped = False

    def artefact(self, name, text):
        if text:
            self.artefacts.append((name, text))

    def note(self, message):
        self.notes.append(message)

    def settle(self, got_something):
        """ok when something was parsed, partial when the console answered but
        nothing could be read out of it, failed when it did not answer."""
        if self.status == FAILED:
            return self
        if got_something:
            self.status = OK
        elif self.artefacts:
            self.status = PARTIAL
            if not self.notes:
                self.note("The console answered but nothing in the reply was "
                          "recognised. The raw reply is in this zip.")
        else:
            self.status = FAILED
            if not self.error:
                # "Nothing answered" is only true when nothing answered. When
                # something did and this tool could not cope with the reply,
                # saying so sends the reader at the fault rather than at their
                # network: a decode error reported as silence cost a real
                # afternoon.
                self.error = (f"Nothing could be collected. {self.notes[0]}"
                              if self.notes else "Nothing answered.")
        return self


class Context:
    def __init__(self, http, ftp=None, deadline=None, log=None, options=None,
                 progress=None, should_stop=None):
        self.http = http
        self.ftp = ftp
        self.deadline = deadline
        self.log = log
        self.options = options or {}
        self._progress = progress
        self._should_stop = should_stop
        self._fetched = {}
        self._ftp_error = None

    def check_deadline(self):
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise RunExpired("the overall time limit was reached")

    def check_stopped(self):
        """Raises RunStopped as soon as the user has asked for the run to end.

        The twin of check_deadline, and called in the same places: anywhere a
        loop can run for more than a second or two on its own.
        """
        if self._should_stop is not None and self._should_stop():
            raise RunStopped("stopped at your request")

    def progress(self, message):
        """Says what is being done right now. A no-op when nobody is listening.

        Deliberately free text rather than a percentage. A collection is a list
        of named things being read off a console, and "reading image 9 of 24"
        answers the question a waiting user actually has; a bar at 37 per cent
        does not.
        """
        if self._progress is not None:
            self._progress(message)

    def fetch(self, paths):
        """Responses for these paths, fetched at most once each per run.

        Half the collectors want something off the root page and refetching it
        for each of them would triple the run on a console that is already slow.
        """
        wanted = [path for path in paths if path not in self._fetched]
        for response in self.http.try_all(wanted):
            self._fetched[response.path] = response
        return [self._fetched[path] for path in paths
                if path in self._fetched]

    def text_of(self, paths):
        """Those same responses flattened to text and joined."""
        return "\n".join(html_to_text(response.body)
                          for response in self.fetch(paths) if response.ok)

    def root_text(self):
        return self.text_of(ROOT_PATHS)

    def ftp_or_none(self, result):
        """The shared FTP connection, or None with the reason recorded once.

        The first collector to need FTP pays for the connection and every later
        one reuses it. If it could not be opened, the reason is remembered so
        six collectors do not each spend the timeout rediscovering it.
        """
        if self.ftp is None:
            result.note("FTP was not available, so nothing needing a "
                        "directory listing could be collected.")
            return None
        if self._ftp_error:
            result.note(f"FTP is not usable: {self._ftp_error}")
            return None
        try:
            self.ftp.open()
        except Exception as exc:
            self._ftp_error = _reason(exc)
            result.note(f"Could not open FTP on port 21: {self._ftp_error}. "
                        "Check that webMAN's FTP server is switched on.")
            return None
        return self.ftp


def _artefact_name(path):
    """A path turned into a flat, safe file name for inside the zip."""
    name = path.strip("/").replace("/", "-") or "root"
    return name


def _reason(exc):
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _note_stopped(result):
    """Records a stop once, however many loops inside a collector saw it."""
    if result.stopped:
        return
    result.stopped = True
    result.note("Stopped at your request part way through. Everything "
                "collected before that point is kept and is in this file.")


# A directory that is simply not there answers 550, and most consoles do not
# have all eight game folders on all four devices. Those are expected and are
# not worth a line in the report; anything else is.
_MISSING = re.compile(r"(?i)\b550\b|no such|not found|cannot find")


def _listing(ctx, result, path, artefact_name, quiet_if_missing=False):
    """LIST one directory, keep the raw output, hand back parsed entries.

    A failure that is not simply "the directory is not there" is recorded on
    the result as a fault, so a later summary can tell the difference between
    a console with no game folders and a tool that could not read them.
    """
    ftp = ctx.ftp_or_none(result)
    if ftp is None:
        return None
    ctx.check_deadline()
    ctx.check_stopped()
    ctx.progress(f"Listing {path}")
    try:
        raw = ftp.list_dir(path)
    except Exception as exc:
        reason = _reason(exc)
        if quiet_if_missing and _MISSING.search(reason):
            return None
        result.note(f"{path} could not be listed: {reason}")
        result.faults.append(f"{path}: {reason}")
        return None
    result.artefact(artefact_name, raw or "(empty listing)\n")
    entries, unparsed = parsers.parse_ftp_list(raw)
    if unparsed:
        result.note(f"{path}: {len(unparsed)} listing lines were not in the "
                    "expected format and are in the raw file.")
    return entries


# --- system ----------------------------------------------------------------

def collect_system(ctx):
    """Firmware, CFW, the console's own model and how hot it is."""
    result = CollectorResult("system", "System and firmware")
    text_parts = []
    ctx.progress("Reading the webMAN pages")
    for response in ctx.fetch(ROOT_PATHS + SYSTEM_PATHS):
        ctx.check_deadline()
        ctx.check_stopped()
        if response.body:
            result.artefact(f"system/{_artefact_name(response.path)}.html",
                            response.body)
        if response.ok:
            text_parts.append(html_to_text(response.body))
        elif response.error:
            result.note(f"{response.path} did not answer: {response.error}")
        elif response.status != 200:
            result.note(f"{response.path} returned HTTP {response.status}. "
                        "That endpoint does not exist on this webMAN version.")
    combined = "\n".join(part for part in text_parts if part)

    facts = dict(parsers.parse_identity(combined))
    facts.update(parsers.parse_cpursx(combined))
    model = describe_model(combined)
    if model["model"]:
        facts["model"] = model["model"]
        facts["model_generation"] = model["generation"]
        facts["model_sales_region"] = model["sales_region"]
        facts["model_shipped_capacity"] = model["shipped_capacity"]
        facts["ps2_compatibility"] = model["ps2_compatibility"]

    # The exact build number only lives in dev_flash, and it is the one thing
    # that distinguishes two consoles both reporting "4.93 CEX".
    ftp = ctx.ftp_or_none(result)
    if ftp is not None:
        facts.update(parsers.parse_ftp_banner(getattr(ftp, "banner", "")))
        try:
            ctx.progress("Reading /dev_flash/vsh/etc/version.txt")
            raw = ftp.download_text("/dev_flash/vsh/etc/version.txt")
            result.artefact("system/version.txt", raw)
            version = parsers.parse_version_txt(raw)
            if version.get("release"):
                facts["firmware_release"] = version["release"]
            if version.get("build"):
                facts["firmware_build"] = version["build"]
            if version.get("system_sdk"):
                facts["system_sdk"] = version["system_sdk"]
            if version.get("flags"):
                facts["firmware_flags"] = ", ".join(version["flags"])
        except Exception as exc:
            result.note("/dev_flash/vsh/etc/version.txt could not be read "
                        f"({_reason(exc)}). The firmware version from the web "
                        "page is used instead.")

    result.facts = {key: value for key, value in facts.items()
                    if value not in (None, "", [])}
    return result.settle(bool(result.facts))


# --- storage ---------------------------------------------------------------

def collect_storage(ctx):
    """What is mounted, how big it is and how full."""
    result = CollectorResult("storage", "Storage and devices")
    devices = parsers.parse_storage(ctx.root_text())
    by_name = {device["device"]: device for device in devices}

    entries = _listing(ctx, result, "/", "storage/ftp-root.txt")
    if entries is not None:
        for entry in entries:
            if entry["kind"] != "directory":
                continue
            name = entry["name"].lower()
            # dev_flash is the system partition. It is not storage the owner
            # can use and listing it only invites someone to go poking at it.
            if name.startswith("dev_") and name != "dev_flash":
                by_name.setdefault(name, {"device": name})
                by_name[name]["present_over_ftp"] = True

    # A top level listing per device is the closest thing webMAN offers to a
    # partition layout, and it is what says whether the game folders exist at
    # all before the inventory goes looking for them.
    for name in sorted(by_name):
        if name in ("dev_flash", "dev_bdvd"):
            continue
        ctx.check_deadline()
        ctx.check_stopped()
        listed = _listing(ctx, result, f"/{name}/",
                          f"storage/listing-{name}.txt",
                          quiet_if_missing=True)
        if listed is None:
            continue
        by_name[name]["top_level_entries"] = len(listed)
        by_name[name]["folders"] = sorted(
            item["name"] for item in listed if item["kind"] == "directory")

    result.facts = {"devices": [by_name[name] for name in sorted(by_name)]}
    if not by_name:
        result.note("No mounted devices were reported. On some webMAN versions "
                    "the free space only appears once a game has been mounted.")
    return result.settle(bool(by_name))


# --- plugins ---------------------------------------------------------------

def collect_plugins(ctx):
    """boot_plugins.txt and whatever is sitting in the plugins folders."""
    result = CollectorResult("plugins", "Plugins")
    listed = {}
    ftp = ctx.ftp_or_none(result)
    if ftp is not None:
        for name in ("boot_plugins.txt", "boot_plugins_nocobra.txt"):
            ctx.check_deadline()
            ctx.check_stopped()
            path = f"/dev_hdd0/{name}"
            ctx.progress(f"Reading {path}")
            try:
                raw = ftp.download_text(path)
            except Exception as exc:
                result.note(f"{path} could not be read: {_reason(exc)}")
                continue
            result.artefact(f"plugins/{name}", raw)
            listed[name] = parsers.parse_boot_plugins(raw)

    entries = _listing(ctx, result, "/dev_hdd0/plugins/",
                       "plugins/folder-dev_hdd0.txt", quiet_if_missing=True)
    installed = []
    if entries:
        installed = [{"name": item["name"], "size": item["size"],
                      "size_human": human_size(item["size"]),
                      "kind": item["kind"]}
                     for item in entries]

    # The root page names the plugins it has loaded in most versions. Matching
    # those against the file list is how an unloaded plugin gets spotted.
    loaded = sorted(_sprx_names(ctx.root_text()))

    facts = {}
    for name, plugins in listed.items():
        facts[name] = plugins
        facts[f"{name}_enabled_count"] = sum(1 for item in plugins
                                             if item["enabled"])
        facts[f"{name}_disabled_count"] = sum(1 for item in plugins
                                              if not item["enabled"])
    if installed:
        facts["installed_in_plugins_folder"] = installed
    if loaded:
        facts["named_on_web_page"] = loaded

    boot = listed.get("boot_plugins.txt") or []
    if boot and installed:
        present = {item["name"].lower() for item in installed}
        missing = [item["path"] for item in boot
                   if item["enabled"]
                   and item["path"].lower().startswith("/dev_hdd0/plugins/")
                   and item["name"].lower() not in present]
        if missing:
            facts["listed_but_not_found"] = missing
            result.note("Some plugins listed in boot_plugins.txt are not in "
                        "/dev_hdd0/plugins/. Those will not be loading.")

    result.facts = facts
    return result.settle(bool(facts))


def _sprx_names(text):
    import re
    return {match.group(0) for match in
            re.finditer(r"(?i)[\w.&'()-]+\.sprx", text or "")}


# --- crash reports ---------------------------------------------------------

def collect_crash_reports(ctx):
    """Everything under /dev_hdd0/crash_report/, contents included."""
    result = CollectorResult("crash_reports", "Crash reports")
    entries = _listing(ctx, result, "/dev_hdd0/crash_report/",
                       "crash_reports/listing.txt")
    if entries is None:
        return result.settle(False)
    files = [item for item in entries if item["kind"] == "file"]
    result.facts["count"] = len(files)
    result.facts["files"] = [{"name": item["name"], "size": item["size"],
                              "size_human": human_size(item["size"]),
                              "modified": item["modified"]}
                             for item in files]
    if not files:
        result.note("There are no crash reports. That is the good outcome.")
        result.status = OK
        return result

    ftp = ctx.ftp_or_none(result)
    if ftp is None:
        return result.settle(True)
    read = 0
    wanted = sorted(files, key=lambda entry: entry["modified"],
                    reverse=True)[:MAX_CRASH_FILES]
    for index, item in enumerate(wanted, 1):
        ctx.check_deadline()
        ctx.check_stopped()
        if item["size"] == 0:
            continue
        path = f"/dev_hdd0/crash_report/{item['name']}"
        ctx.progress(f"Crash report {index} of {len(wanted)}: {item['name']}")
        try:
            raw = ftp.download_text(path)
        except Exception as exc:
            result.note(f"{item['name']} could not be read: {_reason(exc)}")
            continue
        result.artefact(f"crash_reports/{item['name']}", raw)
        read += 1
    result.facts["downloaded"] = read
    if len(files) > MAX_CRASH_FILES:
        result.note(f"There are {len(files)} crash reports. The newest "
                    f"{MAX_CRASH_FILES} were collected.")
    return result.settle(True)


# --- user accounts ---------------------------------------------------------

def collect_accounts(ctx):
    """The local user folders under /dev_hdd0/home, and what is in each one.

    One level down and no further. That is enough to answer the question this
    was added for: the Black Ops 1 stats fix reads np_cache.dat out of one of
    these folders, and a user was told no account had that file while it was
    sitting at /dev_hdd0/home/00000001/np_cache.dat. Nothing in the dump could
    say which of the two was wrong, because nothing in the dump had looked.

    No file here is ever opened, and that is a privacy decision rather than a
    performance one. np_cache.dat carries the account ID and the online ID, and
    localusername holds the name the console shows for a local user. Both are
    exactly the sort of thing that must not turn up in a zip somebody posts in
    a Discord, and neither is needed to answer the question. Names, sizes and
    dates are collected rather than contents.

    Everything this writes says "account" where the console would say "user".
    That is redaction.py talking: its online ID rule treats a bare "user" as a
    label and replaces the word after it, so a title of "User accounts" comes
    out of the zip as "User [ONLINE-ID-95cca5a0]". The rule is loose on
    purpose and catching a real online ID matters more than the wording here,
    so the wording moved.
    """
    result = CollectorResult("accounts", "Accounts")
    entries = _listing(ctx, result, f"{HOME_ROOT}/", "accounts/listing.txt")
    if entries is None:
        return result.settle(False)

    users = []
    others = []
    for item in parsers.real_entries(entries):
        if item["kind"] == "directory" and USER_DIR.match(item["name"]):
            users.append(item)
        else:
            others.append({"name": item["name"], "kind": item["kind"]})
    found = len(users)
    if found > MAX_USER_FOLDERS:
        result.note(f"{HOME_ROOT} holds {found} numbered folders, which is "
                    f"more than a console sets up on its own. The first "
                    f"{MAX_USER_FOLDERS} were looked inside.")
        users = users[:MAX_USER_FOLDERS]

    rows = []
    try:
        for index, item in enumerate(users, 1):
            ctx.check_deadline()
            ctx.check_stopped()
            folder = item["name"]
            ctx.progress(f"Account folder {index} of "
                         f"{len(users)}: {folder}")
            inside = _listing(ctx, result, f"{HOME_ROOT}/{folder}/",
                              f"accounts/listing-{folder}.txt")
            row = {"folder": folder, "path": f"{HOME_ROOT}/{folder}",
                   "listed": inside is not None}
            if inside is not None:
                files = [{"name": entry["name"], "kind": entry["kind"],
                          "size": entry["size"],
                          "modified": entry["modified"]}
                         for entry in parsers.real_entries(inside)]
                cache = next((entry for entry in files
                              if entry["name"].lower() == NP_CACHE), None)
                row["entry_count"] = len(files)
                row["has_np_cache"] = cache is not None
                row["entries"] = files
                if cache is not None:
                    row["np_cache_size"] = cache["size"]
                    row["np_cache_modified"] = cache["modified"]
            # A folder that would not list carries no has_np_cache at all.
            # Recording False there would be the very answer that started this:
            # "no account has np_cache.dat" said about a folder nobody read.
            rows.append(row)
    except RunStopped:
        _note_stopped(result)

    # user_count is what the home listing showed, and users is the folders
    # that were then looked inside. The two differ only when the budget above
    # or a stop cut the walk short, and a reader who is counting accounts
    # should be counting the first of them.
    result.facts = {
        "path": HOME_ROOT,
        "user_count": found,
        "with_np_cache": sum(1 for row in rows if row.get("has_np_cache")),
        "users": rows,
    }
    if others:
        result.facts["other_entries"] = others
    if not found:
        result.note(f"{HOME_ROOT} holds no numbered account folder, so no "
                    "account has been set up on this console. The Black Ops 1 "
                    "fix has nothing to read on a console in this state.")
        result.status = OK
        return result
    return result.settle(True)


# --- game inventory --------------------------------------------------------

def collect_games(ctx):
    """Names, sizes and region codes. No file contents are ever fetched.

    Three phases, and each one keeps what it had if the user stops part way
    through: the folder listings, the installed titles, and, only when it was
    asked for, reading the identity out of every disc image. That last phase is
    the slow one by a wide margin, which is why it is opt-in rather than part
    of what "game inventory" means.
    """
    result = CollectorResult("games", "Game inventory")
    devices = ctx.options.get("devices") or ["dev_hdd0"]
    folders = {}
    all_names = []

    try:
        _walk_game_folders(ctx, result, devices, folders, all_names)
    except RunStopped:
        _note_stopped(result)

    # Each later phase is skipped once a stop has been seen, and each one
    # catches its own stop so that what it had already read is kept.
    installed = []
    identified = 0
    if not result.stopped:
        installed = _installed_titles(ctx, result)
    if not result.stopped:
        identified = _identify_images(ctx, result, folders)

    total_items = sum(folder["count"] for folder in folders.values())
    total_bytes = sum(folder["bytes"] for folder in folders.values())
    regions, unknown = tally_regions(all_names)
    result.facts = {
        "folders": folders,
        "total_items": total_items,
        "total_bytes": total_bytes,
        "total_human": human_size(total_bytes),
        "regions": regions,
        "no_title_id": unknown,
    }
    if installed:
        result.facts["installed_titles"] = installed
    if identified:
        result.facts["images_identified"] = identified
    if not folders and not result.faults and not result.stopped:
        result.note("None of the game folders were found. On this console the "
                    "games may be somewhere else, or FTP may be switched off.")
    elif not folders and not result.stopped:
        result.note("The game folders could not be read, so this is not a "
                    "statement that the console has no games on it. The "
                    "reasons are listed above.")
    result.settle(bool(folders))
    if result.stopped and result.status == FAILED:
        # Being stopped before anything was read is not the console failing to
        # answer, and reporting it as one sends the reader looking for a fault
        # that is not there.
        result.status = SKIPPED
        result.error = None
    return result


def _walk_game_folders(ctx, result, devices, folders, all_names):
    """The eight folders on every device, listed. Fills folders and all_names.

    Written to mutate what it is handed rather than to return it, so a stop
    part way through leaves the caller holding everything listed so far.
    """
    for index, device in enumerate(devices, 1):
        ctx.progress(f"Device {index} of {len(devices)}: {device}")
        for folder in GAME_FOLDERS:
            ctx.check_deadline()
            ctx.check_stopped()
            path = f"/{device}/{folder}/"
            entries = _listing(ctx, result, path,
                               f"games/{device}-{folder}.txt",
                               quiet_if_missing=True)
            if not entries:
                continue
            rows = []
            for item in parsers.real_entries(entries)[:MAX_GAME_ENTRIES]:
                info = describe_name(item["name"])
                rows.append({
                    "name": item["name"],
                    "kind": item["kind"],
                    "size": item["size"],
                    "size_human": human_size(item["size"]),
                    "modified": item["modified"],
                    "title_id": info["title_id"],
                    "region": info["region"],
                    "video_standard": info["video_standard"],
                    "platform": info["platform"],
                    "media": info["media"],
                    "category": info["category"],
                    "category_letter": info["category_letter"],
                    "publisher_class": info["publisher_class"],
                })
                all_names.append(item["name"])
            if len(entries) > MAX_GAME_ENTRIES:
                result.note(f"{path} holds {len(entries)} entries. The first "
                            f"{MAX_GAME_ENTRIES} were recorded.")
            counts, unknown = tally_regions(row["name"] for row in rows)
            total = sum(row["size"] for row in rows)
            folders[f"{device}/{folder}"] = {
                "device": device,
                "folder": folder,
                "count": len(rows),
                "bytes": total,
                "bytes_human": human_size(total),
                "regions": counts,
                "no_title_id": unknown,
                "entries": rows,
            }
            ctx.progress(f"{device}/{folder}: {len(rows)} item(s), "
                         f"{human_size(total)}")


def _installed_titles(ctx, result):
    """What is installed under /dev_hdd0/game, and what is inside each one.

    Separate from the eight game folders because it is a different shape: these
    are installed titles rather than disc images, and the file that matters is
    the executable inside USRDIR rather than the folder itself. Listing it is
    what lets the patch state of a known fix be reported at all; without it
    every answer is "cannot tell".
    """
    entries = _listing(ctx, result, "/dev_hdd0/game/",
                       "games/dev_hdd0-game.txt", quiet_if_missing=True)
    if not entries:
        return []
    titles = [entry for entry in entries
              if entry["kind"] == "directory"
              and TITLE_DIR.match(entry["name"].upper())]
    if len(titles) > MAX_INSTALLED_TITLES:
        result.note(f"/dev_hdd0/game/ holds {len(titles)} installed titles. "
                    f"The first {MAX_INSTALLED_TITLES} were looked inside.")
        titles = titles[:MAX_INSTALLED_TITLES]
    installed = []
    try:
        for index, entry in enumerate(titles, 1):
            ctx.check_deadline()
            ctx.check_stopped()
            title_id = entry["name"].upper()
            ctx.progress(f"Installed title {index} of {len(titles)}: "
                         f"{title_id}")
            path = f"/dev_hdd0/game/{entry['name']}/USRDIR/"
            inside = _listing(ctx, result, path,
                              f"games/game-{title_id}-USRDIR.txt",
                              quiet_if_missing=True)
            installed.append({
                "title_id": title_id,
                "path": f"/dev_hdd0/game/{entry['name']}",
                "files": [{"name": item["name"], "kind": item["kind"],
                           "size": item["size"], "modified": item["modified"]}
                          for item in (inside or [])],
            })
    except RunStopped:
        _note_stopped(result)
    return installed


def _iso_entries(rows):
    """The rows isoreader would actually open, so a count can be honest.

    identify_isos skips anything that is not a file ending .iso, and a progress
    line that counted the folder games alongside them would say "image 30 of
    48" on a console holding eighteen of each.
    """
    out = []
    for row in rows:
        name = row.get("name") or ""
        if row.get("kind") in (None, "file") and \
                name.lower().endswith(isoreader.ISO_SUFFIXES):
            out.append(row)
    return out


def _identify_images(ctx, result, folders):
    """Reads the real identity out of each disc image, a few blocks at a time.

    File names lie: renamed folders, wrong region in brackets, a dump of one
    game sitting under another's name. A few aligned blocks out of the image
    settle it, and a mismatch between the two is the single most useful thing
    this collector produces. Nothing is ever downloaded whole.

    Off unless it was asked for. Every image costs a connection, a seek and a
    few reads on a console that may be busy, and on a shelf of two dozen games
    that is minutes rather than seconds: long enough that it has to be the
    user's choice rather than the default.

    Failure here costs the identification and nothing else. The inventory is
    already built by the time this runs.
    """
    if isoreader is None or not ctx.options.get("identify_isos", False):
        return 0
    ftp = ctx.ftp_or_none(result)
    if ftp is None:
        return 0
    rows = []
    for key, folder in folders.items():
        for entry in folder["entries"]:
            row = dict(entry)
            row["device"] = folder["device"]
            row["folder"] = folder["folder"]
            rows.append(row)
    rows = _iso_entries(rows)
    if not rows:
        return 0

    reader = isoreader.reader_for(ftp)
    images = []
    # One image per call, with the run's byte budget carried down by hand, so
    # that identifying them one at a time still costs the console no more than
    # identifying them in one call did. The loop has to be on this side of the
    # boundary for a stop or a progress line to happen between images at all.
    budget = isoreader.DEFAULT_TOTAL_BUDGET
    try:
        for index, row in enumerate(rows, 1):
            ctx.check_deadline()
            ctx.check_stopped()
            ctx.progress(f"Reading inside image {index} of {len(rows)}: "
                         f"{row['name']}")
            payload = isoreader.identify_isos([row], reader,
                                              total_budget=budget,
                                              log=ctx.log)
            for item in payload.get("isos") or []:
                images.append(item)
                budget = max(0, budget - (item.get("bytes_read") or 0))
    except RunExpired:
        result.note("The time limit was reached before the disc images could "
                    "be identified from their contents. The rest are listed "
                    "by file name only.")
    except RunStopped:
        _note_stopped(result)
    except Exception as exc:
        result.note(f"The disc images could not be read to identify them "
                    f"({_reason(exc)}). They are listed by file name only.")

    if not images:
        return 0
    payload = {"schema_version": isoreader.SCHEMA_VERSION, "isos": images}
    result.artefact("games/iso-identity.json",
                    json.dumps(payload, indent=2, sort_keys=True, default=str))
    mismatched = [row for row in images if row.get("name_mismatch")]
    if mismatched:
        result.note(f"{len(mismatched)} disc image(s) contain a different "
                    f"title from the one in the file name. See "
                    f"games/iso-identity.json.")
    return sum(1 for row in images if row.get("title_id"))


# --- network ---------------------------------------------------------------

def collect_network(ctx):
    """What the console says about its own connection."""
    result = CollectorResult("network", "Network")
    ctx.check_deadline()
    text = ctx.text_of(ROOT_PATHS + SYSTEM_PATHS)
    facts = parsers.parse_network(text)
    if facts:
        result.artefact("network/parsed-from.txt", text)
    else:
        result.note("This webMAN version does not report the console's "
                    "network settings on any page that answered. Nothing is "
                    "wrong with the console.")

    # Whatever the pages do or do not say, the address the tool reached the
    # console on is known for certain, and so is whether FTP answered. On
    # webMAN 1.47.48q that is the whole of what can be established, and saying
    # it is better than an empty section that reads like a failure.
    host = ctx.options.get("host")
    if host:
        facts.setdefault("reached_at", host)
    banner = getattr(ctx.ftp, "banner", "") if ctx.ftp is not None else ""
    if banner:
        facts["ftp_banner"] = banner.strip()
        facts["ftp_reachable"] = True

    result.facts = facts
    return result.settle(bool(facts))


# --- webMAN configuration --------------------------------------------------

def collect_webman_config(ctx):
    """webMAN's version and its own settings, read without submitting the form."""
    result = CollectorResult("webman_config", "webMAN configuration")
    facts = {}
    identity = parsers.parse_identity(ctx.root_text())
    if identity.get("webman_version"):
        facts["webman_version"] = identity["webman_version"]

    responses = ctx.fetch(("/setup.ps3",))
    response = responses[0] if responses else None
    if response is None:
        result.note("/setup.ps3 was not requested because it is not on the "
                    "read-only allowlist. This is a fault in ps3-diag.")
        result.facts = facts
        return result.settle(bool(facts))
    if response.body:
        result.artefact("webman_config/setup.ps3.html", response.body)
    if response.ok:
        settings = parsers.parse_setup(response.body)
        if settings:
            facts["settings"] = settings
            facts["settings_count"] = len(settings)
        else:
            result.note("The setup page answered but no settings could be read "
                        "out of it. The raw page is in this zip.")
    elif response.error:
        result.note(f"/setup.ps3 did not answer: {response.error}")
    else:
        result.note(f"/setup.ps3 returned HTTP {response.status}. This webMAN "
                    "version keeps its settings somewhere else.")

    result.facts = facts
    return result.settle(bool(facts))


# Order matters: system first so the summary has a console to describe, games
# last because it is much the slowest and a run that runs out of time should
# lose the inventory rather than the firmware version.
CATEGORIES = (
    ("system", "System and firmware", collect_system),
    ("storage", "Storage and devices", collect_storage),
    ("plugins", "Plugins", collect_plugins),
    ("crash_reports", "Crash reports", collect_crash_reports),
    ("accounts", "Accounts", collect_accounts),
    ("network", "Network", collect_network),
    ("webman_config", "webMAN configuration", collect_webman_config),
    ("games", "Game inventory", collect_games),
)

CATEGORY_KEYS = tuple(key for key, _title, _run in CATEGORIES)
CATEGORY_TITLES = {key: title for key, title, _run in CATEGORIES}
