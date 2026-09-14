"""Finding save data on a console and copying it to this PC. Read only.

Saves live in /dev_hdd0/home/<8 digits>/savedata/<FOLDER>, one folder per game
per user. The folder name carries the title ID and nothing else a person would
recognise, so BLES01717-DATA000 is what the console calls a Black Ops II save
and is no use at all to somebody deciding which of eleven folders to keep. The
game inventory already on the console -- the installed titles, the GAMES
folders, the disc images -- spells the same IDs out in words, so it is read
once and used as a name table. Where that fails the raw folder name is shown,
never a guess.

Two things are deliberately not here.

There is no restore. A PS3 save is bound to the console and often to the
account that made it, by a signature in PARAM.PFD that this tool cannot
produce; copying the files back is not the reverse of copying them off and
would, at best, give a game a save it refuses to load. Anything that implied
otherwise would be a promise this cannot keep, so the module offers no way to
write and the screen says so in plain words.

There is no transport. Nothing in this module opens a connection or knows what
one looks like. The directory listings come from an injected lister, and the
file contents come from an injected reader callable, the same seam
ps3tools.detect uses for PARAM.SFO. That is what lets the whole of this be
exercised without a console, and it is what stops a test reaching the real one
on the other side of the room.

The survey asks for as little as it can get away with. webMANftpd is a small
embedded server and every directory listing is its own passive data
connection; a console that answers one listing from curl instantly was seen to
drop the control connection on the sixth in a row from here. So the survey
lists /dev_hdd0/home and each user's savedata folder and stops there. What is
inside a save folder is read when somebody actually asks for that folder, at
copy time, which is the only moment it is needed. The game inventory is read
after the saves rather than before, because it only supplies names: losing it
costs a nicety, and losing it first used to cost the saves as well.

Nothing here raises. A console that stops answering part way through leaves
everything already found or already copied intact and records the reason.

A survey that failed is never allowed to look like a survey that found
nothing. Those are different answers and only one of them is a statement about
the console, so the reasons are kept in their own list and the screen is
expected to read it before it says anything about what is there.
"""

import errno
import ftplib
import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from ps3diag import config
from ps3diag.parsers import parse_ftp_list
from ps3diag.regioncodes import describe_name, find_title_id

from . import titles

HOME_ROOT = "/dev_hdd0/home"

# Where the console writes down what a title ID means in words. Read for names
# only; nothing in any of these folders is ever fetched.
INVENTORY_ROOTS = (
    "/dev_hdd0/game/",
    "/dev_hdd0/GAMES/",
    "/dev_hdd0/PS3ISO/",
    "/dev_hdd0/PS2ISO/",
)

# A user folder under /dev_hdd0/home. The console numbers them 00000001 up.
# Anything else in there belongs to something other than a user profile.
USER_DIR = re.compile(r"^\d{8}$")

# Caps, so an unexpected listing cannot turn into an unbounded walk over FTP.
# A console with more users or more saves than this is not a case anybody has
# seen, and the alternative to a cap is a scan that appears to hang.
MAX_USERS = 16
MAX_SAVES_PER_USER = 400

# One save file. Real ones run to a few megabytes; the largest anybody reports
# is well under this. A file above it is recorded and skipped rather than
# pulled, because a save folder is not a place a 2 GB file belongs and fetching
# one over FTP would look exactly like the program having frozen.
MAX_SAVE_FILE_BYTES = 32 * 1024 * 1024

# Said on the screen as well. Repeated here because the manifest travels
# separately from the screen that produced it, and the person reading the
# manifest in six months is the one most likely to assume these can be put
# back.
NO_RESTORE_NOTICE = (
    "This is a copy, not a backup that can be put back. PS3 saves are "
    "normally locked to the console and the account that made them, so "
    "copying these files on to a console does not restore them. Keep them "
    "for safekeeping and for reading on a PC. Nothing on the console was "
    "changed, moved or deleted to make this copy."
)

NO_READER = (
    "This connection cannot fetch save files, so nothing was copied. The "
    "list of saves above is still correct. Tell whoever gave you this "
    "program that the save transport is not switched on in this build."
)

NOTHING_PICKED = (
    "Nothing was ticked, so there was nothing to copy. Tick a user to take "
    "all of their saves, or tick individual saves."
)

NO_HOME = (
    "This console has no /dev_hdd0/home folder, which is where the PS3 keeps "
    "every user's saves. That normally means no user profile has been created "
    "on it yet. Switch the console on, let it reach the main menu, and try "
    "again."
)

NO_SAVEDATA = (
    "This user has no savedata folder yet, so they have no saves to copy. "
    "That is normal for a profile that has never played a game."
)

UNREADABLE_SAVE = (
    "The console refused to list this save folder, so what is inside it is "
    "not known and it was not copied."
)


def _reason(exc):
    """A failure in words, without a traceback and without an empty string."""
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _lost_console(where, exc):
    return (f"The console stopped answering while {where} was being read, so "
            f"this list may be incomplete. Anything found before that is "
            f"kept. Check it is still switched on, still on the main menu and "
            f"still on the network, then try again. ({_reason(exc)})")


# --- what is on the console ------------------------------------------------

@dataclass
class SaveFile:
    """One file inside one save folder, as the listing describes it."""

    name: str = ""
    path: str = ""
    size: int = 0


@dataclass
class SaveFolder:
    """One game's saves for one user."""

    user_id: str = ""
    folder: str = ""
    path: str = ""
    title_id: str = None
    game_name: str = None
    #: Region and platform read out of the title ID, for the second line of
    #: the row. None where the ID means nothing to us.
    region: str = None
    platform: str = None
    files: list = field(default_factory=list)
    #: True once the folder itself has been listed. False means the files are
    #: not known yet rather than that there are none, and the two must not be
    #: shown the same way: an empty list on a folder nobody has opened would
    #: read as "this save is empty" on the row.
    listed: bool = False
    #: The size the parent listing gave for the folder, where it gave one. The
    #: PS3 reports 0 for every directory, so this is normally 0 and the size is
    #: simply not known until the folder is read.
    listed_size: int = 0
    #: Present when the folder could not be read. The folder is still listed,
    #: because a save the user cannot see is a save they will assume was taken.
    note: str = None

    @property
    def key(self):
        """Stable across a rescan, which is what the tick boxes are keyed on."""
        return f"{self.user_id}/{self.folder}"

    @property
    def readable(self):
        return self.note is None

    @property
    def display_name(self):
        """What to put on the row. The game where we know it, the folder where
        we do not: a folder name is at least true."""
        return self.game_name or self.folder

    @property
    def named(self):
        return self.game_name is not None

    @property
    def size_known(self):
        """False until somebody has actually opened this folder.

        total_bytes is 0 either way, so a caller that shows a size has to ask
        this first or it will print "0 bytes" for a save it has never looked
        inside.
        """
        return self.listed or self.listed_size > 0

    @property
    def total_bytes(self):
        if self.listed:
            return sum(item.size for item in self.files)
        return self.listed_size


@dataclass
class SaveUser:
    """One console user profile and everything it has saved."""

    user_id: str = ""
    path: str = ""
    savedata_path: str = ""
    saves: list = field(default_factory=list)
    note: str = None

    @property
    def total_bytes(self):
        return sum(save.total_bytes for save in self.saves)

    @property
    def size_known(self):
        return all(save.size_known for save in self.saves)


@dataclass
class Survey:
    """Everything one scan found. Never an exception."""

    users: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    #: Every reason this scan may not have seen everything: a console that
    #: stopped answering, a listing that came back in a shape nothing here
    #: understands. Kept apart from notes because a caller has to be able to
    #: ask "did this finish" without reading English, and because an empty
    #: result with something in here is not a statement that there is nothing
    #: on the console.
    errors: list = field(default_factory=list)
    #: title ID to game name, for showing what the survey found
    inventory: dict = field(default_factory=dict)

    def note(self, text):
        if text and text not in self.notes:
            self.notes.append(text)

    def problem(self, text):
        """A note that also means the answer below it is not to be trusted."""
        self.note(text)
        if text and text not in self.errors:
            self.errors.append(text)

    @property
    def complete(self):
        """True when the console answered everything it was asked.

        The distinction this exists for is the one between "asked, and there
        is nothing" and "could not finish asking". The same distinction
        ps3diag.patchstate draws between an empty installed_titles list and a
        missing one: collapsing them is how a diagnostic ends up stating
        something false with confidence.
        """
        return not self.errors

    @property
    def saves(self):
        return [save for user in self.users for save in user.saves]

    @property
    def save_count(self):
        return len(self.saves)

    @property
    def total_bytes(self):
        return sum(save.total_bytes for save in self.saves)

    @property
    def size_known(self):
        return all(save.size_known for save in self.saves)

    def find(self, key):
        for save in self.saves:
            if save.key == key:
                return save
        return None


# Strips the bracketed title ID and anything trailing it out of an inventory
# entry, so "Persona 5 [NPEB02143]" becomes "Persona 5". Also handles the
# parenthesised and bare-suffix forms that turn up in the same listings.
_TRAILING_ID = re.compile(
    r"\s*[\[(\-_]*\s*[A-Z]{4}[-_]?\d{5}\s*[\])]*\s*.*$")
_EXTENSION = re.compile(r"(?i)\.(iso|bin|img|pkg)$")


def _name_from_entry(entry_name, title_id):
    """The human part of an inventory entry, or None if there is not one.

    A folder called exactly BLES01717 says nothing a title ID does not already
    say, and returning it as a name would put the same string on the row twice
    while claiming it had been recognised.
    """
    text = _EXTENSION.sub("", entry_name or "").strip()
    trimmed = _TRAILING_ID.sub("", text).strip(" -_.")
    if not trimmed:
        return None
    if find_title_id(trimmed) or trimmed.upper() == (title_id or "").upper():
        return None
    return trimmed


def build_inventory(lister):
    """Title ID to game name, from what is installed on this console.

    Read once per scan and reused for every save folder. A root that is not
    there, or that the console refuses, costs the names in it and nothing else,
    so every one of them is tried and none of them can fail the scan.
    """
    found = {}
    for root in INVENTORY_ROOTS:
        try:
            listing = lister.list_dir(root)
        except Exception:                                   # noqa: BLE001
            # A missing GAMES folder is the normal case, not a fault, and a
            # console that has stopped answering is reported by the caller
            # that is actually trying to read saves. Either way the names are
            # a nicety and must not take the scan down with them.
            continue
        entries, _unparsed = parse_ftp_list(listing)
        for entry in entries:
            title_id = find_title_id(entry.get("name", ""))
            if not title_id:
                continue
            name = _name_from_entry(entry.get("name", ""), title_id)
            # First name wins. The roots are ordered installed-first, and an
            # installed title's folder is the one most likely to have been
            # named by the console rather than by whoever made the image.
            if name and title_id.upper() not in found:
                found[title_id.upper()] = name
    return found


def name_for(title_id, inventory=None):
    """The best name available for a title ID, or None.

    The verified table wins over the console's own folder names: it is checked,
    and a folder name is whatever somebody typed.
    """
    if not title_id:
        return None
    entry = titles.config_for(title_id)
    if entry and entry.get("name"):
        return entry["name"]
    return (inventory or {}).get(title_id.upper())


def describe_folder(user_id, folder, inventory=None, listed_size=0):
    """A SaveFolder with its identity filled in and no files read yet."""
    title_id = find_title_id(folder)
    info = describe_name(folder)
    return SaveFolder(
        user_id=user_id,
        folder=folder,
        path=f"{HOME_ROOT}/{user_id}/savedata/{folder}",
        title_id=title_id,
        game_name=name_for(title_id, inventory),
        region=info.get("region"),
        platform=info.get("platform"),
        listed_size=int(listed_size or 0),
    )


def survey(lister, inventory=None):
    """Every user on the console and every save each of them has.

    Never raises. See the module docstring: a console that goes away part way
    leaves what was already found intact and puts the reason in notes, and the
    reason also goes in errors so the caller cannot mistake this for an empty
    console.

    The saves are read before the game inventory, not after. The inventory is
    four more directory listings and it supplies nothing but nicer names, so
    asking for it first spent the connection this feature actually needs on
    the part of it nobody would miss.
    """
    report = Survey()
    try:
        _walk_home(lister, report)
    except ftplib.error_perm:
        # A refusal is an answer: the folder is not there. Every console with
        # a user profile on it has one, so this is worth saying plainly. Not a
        # problem(): the console was asked and it answered.
        report.note(NO_HOME)
    except Exception as exc:                                # noqa: BLE001
        report.problem(_lost_console(HOME_ROOT, exc))

    try:
        report.inventory = (build_inventory(lister) if inventory is None
                            else dict(inventory))
    except Exception:                                       # noqa: BLE001
        report.inventory = {}
    _apply_names(report)
    return report


def _apply_names(report):
    """Put the inventory's names on to folders that were listed before it.

    A missing name is cosmetic, so this never records a problem: a row falls
    back to the folder name, which is at least what the console calls it.
    """
    for save in report.saves:
        if save.game_name is None:
            save.game_name = name_for(save.title_id, report.inventory)


def _walk_home(lister, report):
    entries, unparsed = parse_ftp_list(lister.list_dir(HOME_ROOT + "/"))
    if unparsed:
        # A problem rather than a note. Lines nobody could read may have been
        # user folders, so what follows is not the whole picture and must not
        # be reported as though it were.
        report.problem(f"{len(unparsed)} line(s) of the {HOME_ROOT} listing "
                       f"were in a format this tool does not recognise and "
                       f"were skipped, so a user profile may have been missed.")
    names = sorted({entry["name"] for entry in entries
                    if entry.get("kind") == "directory"
                    and USER_DIR.match(entry.get("name", ""))})
    if not names:
        report.note("No user profiles were found on this console, so there "
                    "are no saves to copy.")
        return
    if len(names) > MAX_USERS:
        report.problem(f"This console lists {len(names)} user profiles, which "
                       f"is more than expected. Only the first {MAX_USERS} "
                       f"were looked at.")
        names = names[:MAX_USERS]

    for user_id in names:
        user = SaveUser(user_id=user_id,
                        path=f"{HOME_ROOT}/{user_id}",
                        savedata_path=f"{HOME_ROOT}/{user_id}/savedata")
        report.users.append(user)
        try:
            _walk_user(lister, report, user)
        except ftplib.error_perm:
            user.note = NO_SAVEDATA
        except Exception as exc:                            # noqa: BLE001
            # The console has gone. Keep this user and every user before them,
            # say why the rest are missing, and stop asking.
            user.note = ("The console stopped answering while this user's "
                         "saves were being read, so some of them may be "
                         "missing.")
            report.problem(_lost_console(user.savedata_path, exc))
            return


def _walk_user(lister, report, user):
    """One listing, and one listing only.

    This used to descend into every save folder as well, which turned a scan
    of one user with four saves into six passive data connections in a row and
    was where a real console gave up. What is inside a folder is read by
    read_files when something actually needs it.
    """
    entries, unparsed = parse_ftp_list(
        lister.list_dir(user.savedata_path + "/"))
    if unparsed:
        report.problem(f"{len(unparsed)} line(s) of user {user.user_id}'s "
                       f"savedata listing were in a format this tool does not "
                       f"recognise and were skipped, so a save may have been "
                       f"missed.")
    sizes = {}
    for entry in entries:
        name = entry.get("name", "")
        if entry.get("kind") == "directory" and name not in (".", ".."):
            sizes[name] = int(entry.get("size") or 0)
    folders = sorted(sizes)
    if len(folders) > MAX_SAVES_PER_USER:
        report.problem(f"User {user.user_id} has {len(folders)} save folders, "
                       f"which is more than expected. Only the first "
                       f"{MAX_SAVES_PER_USER} were looked at.")
        folders = folders[:MAX_SAVES_PER_USER]

    for folder in folders:
        user.saves.append(describe_folder(user.user_id, folder,
                                          report.inventory, sizes[folder]))

    if not user.saves and user.note is None:
        user.note = ("This user's savedata folder is empty, so they have no "
                     "saves to copy.")


def read_files(lister, save):
    """What is inside one save folder, read now. Returns why not, or None.

    Deliberately not called by the survey. One listing per save folder is what
    took a scan of four saves from three requests to a dozen, and a console
    that stops answering has to be asked for the folders somebody chose rather
    than for all of them.

    "refused" means the console answered and said no, which is about this
    folder alone. "lost" means the console stopped answering, which is about
    everything after it as well, so the caller is expected to stop.
    """
    if save.listed:
        return None
    # A second attempt is a fresh question. Without this, a folder that failed
    # once would keep the note from that attempt and be skipped by the copy
    # even after the console came back and answered it.
    save.note = None
    ask = getattr(lister, "list_dir", None)
    if ask is None:
        save.note = UNREADABLE_SAVE
        return "refused"
    try:
        listing = ask(save.path + "/")
    except ftplib.error_perm:
        # One unreadable folder among thirty. Show it, say it cannot be read,
        # and carry on: the other twenty-nine are still worth having.
        save.note = UNREADABLE_SAVE
        return "refused"
    except Exception:                                       # noqa: BLE001
        save.note = ("The console stopped answering before this save could be "
                     "opened, so what is inside it is not known.")
        return "lost"
    entries, _unparsed = parse_ftp_list(listing)
    files = []
    for entry in entries:
        name = entry.get("name", "")
        if not name or name in (".", ".."):
            continue
        if entry.get("kind") != "file":
            # Real PS3 saves are flat. A folder inside one is something this
            # tool has never seen, and walking into it would widen what gets
            # fetched on a guess.
            continue
        files.append(SaveFile(name=name,
                              path=f"{save.path}/{name}",
                              size=int(entry.get("size") or 0)))
    files.sort(key=lambda item: item.name.lower())
    save.files = files
    save.listed = True
    return None


# --- copying it here -------------------------------------------------------

@dataclass
class CopiedFile:
    name: str = ""
    source: str = ""
    stored: str = ""
    size: int = 0
    sha256: str = ""


@dataclass
class SaveResult:
    """What became of one save folder."""

    key: str = ""
    user_id: str = ""
    folder: str = ""
    display_name: str = ""
    title_id: str = None
    source: str = ""
    files: list = field(default_factory=list)
    #: (file name, why) for anything that was listed but not copied
    skipped: list = field(default_factory=list)
    note: str = None

    @property
    def complete(self):
        return not self.skipped and self.note is None and bool(self.files)

    @property
    def bytes_copied(self):
        return sum(item.size for item in self.files)


@dataclass
class Backup:
    """The result of one copy. Never an exception."""

    folder: str = ""
    zip_path: str = None
    saves: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    started: str = ""
    finished: str = ""
    cancelled: bool = False
    #: False when the copy was cut short by this PC rather than by the console:
    #: a full disk, a folder that could not be written. The zip is then missing
    #: its manifest and is not a thing to hand anybody.
    complete: bool = True

    def note(self, text):
        if text and text not in self.notes:
            self.notes.append(text)

    @property
    def files_copied(self):
        return sum(len(save.files) for save in self.saves)

    @property
    def bytes_copied(self):
        return sum(save.bytes_copied for save in self.saves)

    @property
    def ok(self):
        return (bool(self.zip_path) and self.files_copied > 0
                and self.complete)


def destination_for(when=None, base=None):
    r"""Desktop\PS3 Tools saves\<date>. Made when the copy actually starts."""
    when = when or datetime.now()
    root = base or config.desktop_dir()
    return os.path.join(root, "PS3 Tools saves", when.strftime("%Y-%m-%d"))


def _emit(progress, **event):
    """Progress is a courtesy. A screen that throws must not lose the copy."""
    if progress is None:
        return
    try:
        progress(event)
    except Exception:                                       # noqa: BLE001
        pass


def _safe_component(text):
    """A folder or file name that cannot escape the zip it is written into.

    The names come off somebody else's console and go into a path on this PC.
    Anything with a separator or a dot-dot in it is replaced rather than
    cleaned, so a hostile name becomes an ugly entry instead of a write
    somewhere it was not invited.
    """
    cleaned = re.sub(r"[^A-Za-z0-9 ._@()\[\]+-]", "_", text or "")
    cleaned = cleaned.replace("..", "__").strip(" .")
    return cleaned or "unnamed"


def back_up(lister, selection, destination=None, save_reader=None,
            progress=None, cancelled=None, when=None):
    """Copy the chosen save folders into one zip, with a manifest.

    lister is used for two things: it is asked what is inside each chosen save
    folder, because the survey deliberately did not look, and it supplies the
    default save_reader. Only the folders that were ticked are opened, which
    is the whole point of leaving it until now.

    save_reader is a callable taking a path and returning that file's bytes.
    It defaults to the lister's own save read, the same seam
    ps3tools.detect uses for PARAM.SFO; a lister that has no such method
    degrades to copying nothing and saying why, rather than failing.

    progress is called with one dict per event, on whatever thread this runs
    on. The events are save_start, file and save_done, each carrying the index
    of the save folder, because one bar for a hundred folders tells the user
    nothing about the one that is taking four minutes.

    Never raises.
    """
    when = when or datetime.now()
    result = Backup(started=when.isoformat(timespec="seconds"))
    selection = list(selection or [])

    if not selection:
        result.note(NOTHING_PICKED)
        result.finished = result.started
        return result

    if save_reader is None:
        save_reader = getattr(lister, "download_save_bytes", None)
    if save_reader is None:
        result.note(NO_READER)
        result.finished = result.started
        return result

    result.folder = destination or destination_for(when)
    try:
        os.makedirs(result.folder, exist_ok=True)
    except OSError as exc:
        result.note(f"The folder for these saves could not be made. "
                    f"{_disk_advice(exc)} ({_reason(exc)})")
        result.finished = datetime.now().isoformat(timespec="seconds")
        return result

    name = f"PS3 saves {when.strftime('%Y-%m-%d %H%M%S')}.zip"
    path = os.path.join(result.folder, name)
    archive = None
    try:
        archive = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
        result.zip_path = path
        _copy_all(lister, archive, selection, save_reader, result, progress,
                  cancelled)
        # Stamped before the manifest is written, so the manifest inside the
        # zip carries the same finishing time as the one reported to the user.
        result.finished = datetime.now().isoformat(timespec="seconds")
        _write_manifest(archive, result)
    except OSError as exc:
        # Disk full, a read-only Desktop, a path the antivirus has locked.
        # Whatever went into the zip before this point is still in it, so it
        # is closed rather than removed and the user is told what is missing.
        result.complete = False
        result.note(f"Copying stopped because the file could not be written. "
                    f"{_disk_advice(exc)} ({_reason(exc)})")
    except Exception as exc:                                # noqa: BLE001
        result.complete = False
        result.note(f"Copying stopped unexpectedly and what had already been "
                    f"copied has been kept. ({_reason(exc)})")
    finally:
        if archive is not None:
            try:
                archive.close()
            except Exception:                               # noqa: BLE001
                pass

    if not result.finished:
        result.finished = datetime.now().isoformat(timespec="seconds")
    return result


def _disk_advice(exc):
    """The one cause worth naming, because it is the one the user can fix."""
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return ("This PC has run out of disk space. Free some space on the "
                "drive your Desktop is on and try again.")
    return ("Check there is space on this PC and that the Desktop folder can "
            "be written to.")


def _copy_all(lister, archive, selection, save_reader, result, progress,
              cancelled):
    total = len(selection)
    for index, save in enumerate(selection):
        if cancelled is not None and cancelled():
            result.cancelled = True
            result.note("Stopped before the rest of the saves were copied. "
                        "Everything copied up to that point is in the file.")
            return
        entry = SaveResult(key=save.key, user_id=save.user_id,
                           folder=save.folder,
                           display_name=save.display_name,
                           title_id=save.title_id, source=save.path)
        result.saves.append(entry)

        # The survey does not open save folders, so for most of these this is
        # the first time anybody has asked what is in one. Done here, where the
        # answer is about to be used, rather than for every folder on the
        # console during a scan the user may not copy anything from.
        opened = read_files(lister, save)
        _emit(progress, kind="save_start", index=index, total=total,
              key=save.key, name=save.display_name,
              files=len(save.files), bytes=save.total_bytes)
        if opened == "lost":
            entry.note = save.note
            _emit(progress, kind="save_done", index=index, total=total,
                  key=save.key, copied=0, note=entry.note)
            result.note("The console stopped answering, so this save and the "
                        "ones after it were not copied. Everything copied "
                        "before it is in the file. Check the console is still "
                        "switched on and on the network, then run this again.")
            return

        if save.note is not None:
            entry.note = save.note
            _emit(progress, kind="save_done", index=index, total=total,
                  key=save.key, copied=0, note=entry.note)
            continue
        if not save.files:
            entry.note = "This save folder is empty, so nothing was copied."
            _emit(progress, kind="save_done", index=index, total=total,
                  key=save.key, copied=0, note=entry.note)
            continue

        lost = _copy_one(archive, save, entry, save_reader, result, progress,
                         index, total, cancelled)
        _emit(progress, kind="save_done", index=index, total=total,
              key=save.key, copied=len(entry.files), note=entry.note)
        if lost:
            # The console has gone. Asking the next folder would only produce
            # the same failure a hundred more times, each one slower than the
            # last, while the user watches a bar that never moves.
            result.note("The console stopped answering, so the saves after "
                        "this one were not copied. Everything copied before "
                        "it is in the file. Check the console is still "
                        "switched on and on the network, then run this again.")
            return
    return


def _copy_one(archive, save, entry, save_reader, result, progress, index,
              total, cancelled):
    """One save folder. Returns True if the console stopped answering."""
    base = f"{_safe_component(save.user_id)}/{_safe_component(save.folder)}"
    done_bytes = 0
    for position, item in enumerate(save.files):
        if cancelled is not None and cancelled():
            entry.skipped.append((item.name, "stopped before this file"))
            continue
        if item.size > MAX_SAVE_FILE_BYTES:
            entry.skipped.append(
                (item.name, f"larger than {MAX_SAVE_FILE_BYTES // (1024*1024)} "
                            f"MB, which is not a size a save file should be"))
            continue
        try:
            blob = save_reader(item.path)
        except ftplib.error_perm as exc:
            # A refusal is about this file. The rest of the folder is fine.
            entry.skipped.append((item.name,
                                  f"the console refused it ({_reason(exc)})"))
            continue
        except Exception as exc:                            # noqa: BLE001
            entry.skipped.append((item.name,
                                  f"the console stopped answering "
                                  f"({_reason(exc)})"))
            entry.note = ("The console stopped answering part way through "
                          "this save, so it is incomplete.")
            return True
        blob = bytes(blob or b"")
        stored = f"{base}/{_safe_component(item.name)}"
        archive.writestr(stored, blob)
        entry.files.append(CopiedFile(
            name=item.name, source=item.path, stored=stored, size=len(blob),
            sha256=hashlib.sha256(blob).hexdigest()))
        done_bytes += len(blob)
        _emit(progress, kind="file", index=index, total=total, key=save.key,
              name=save.display_name, file=item.name,
              done_files=position + 1, total_files=len(save.files),
              done_bytes=done_bytes, total_bytes=save.total_bytes)
    return False


# --- the manifest ----------------------------------------------------------

MANIFEST_JSON = "manifest.json"
MANIFEST_TEXT = "manifest.txt"
READ_ME = "read me first.txt"


def manifest(result):
    """What came from where, and the hash of every file. A plain dict."""
    return {
        "tool": "PS3 Tools by setsid",
        "what_this_is": "A copy of PS3 save data. Read only.",
        "restore": NO_RESTORE_NOTICE,
        "started": result.started,
        "finished": result.finished,
        "files_copied": result.files_copied,
        "bytes_copied": result.bytes_copied,
        "hash": "sha256",
        "notes": list(result.notes),
        "saves": [
            {
                "user": save.user_id,
                "folder": save.folder,
                "game": save.display_name,
                "title_id": save.title_id,
                "from": save.source,
                "note": save.note,
                "not_copied": [{"file": name, "why": why}
                               for name, why in save.skipped],
                "files": [
                    {
                        "file": item.name,
                        "from": item.source,
                        "in_this_zip": item.stored,
                        "bytes": item.size,
                        "sha256": item.sha256,
                    }
                    for item in save.files
                ],
            }
            for save in result.saves
        ],
    }


def manifest_text(result):
    """The same thing for somebody who will never open a .json file."""
    lines = [
        "PS3 Tools by setsid -- save data copy",
        "",
        NO_RESTORE_NOTICE,
        "",
        f"Started:  {result.started}",
        f"Finished: {result.finished}",
        f"Copied:   {result.files_copied} file(s), "
        f"{result.bytes_copied} bytes",
        "",
    ]
    for save in result.saves:
        lines.append(f"{save.display_name}")
        lines.append(f"    user {save.user_id}, folder {save.folder}")
        lines.append(f"    from {save.source}")
        if save.note:
            lines.append(f"    {save.note}")
        for item in save.files:
            lines.append(f"    {item.name}  {item.size} bytes  "
                         f"sha256 {item.sha256}")
        for name, why in save.skipped:
            lines.append(f"    {name}  NOT COPIED: {why}")
        lines.append("")
    if result.notes:
        lines.append("Notes")
        for note in result.notes:
            lines.append(f"    {note}")
        lines.append("")
    return "\n".join(lines)


def _write_manifest(archive, result):
    archive.writestr(MANIFEST_JSON,
                     json.dumps(manifest(result), indent=2, sort_keys=False))
    archive.writestr(MANIFEST_TEXT, manifest_text(result))
    archive.writestr(READ_ME, NO_RESTORE_NOTICE + "\n\nmanifest.txt lists "
                              "every file in this zip, where on the console it "
                              "came from, and a checksum for it.\n")
