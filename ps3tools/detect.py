"""What is installed on the console, and whether it can be patched.

The patcher never asks the user where a game is. It finds every installation
itself, says which of them it can work on, and when it cannot, says why in a
sentence somebody with no technical knowledge can act on.

Three facts shape everything here.

The patchable files live in /dev_hdd0/game/<TITLEID>/USRDIR, and that directory
exists only once the title update has been installed. A disc or an ISO on its
own does not create it. So a title that is present with no USRDIR is the common
case, not a fault, and it is reported as no_update rather than as an error: the
user runs the game once, lets the update download, and comes back.

A title ID that is not in ps3tools.titles is refused rather than patched with
another region's signing parameters. Whether the klicensee is constant across
regional SKUs is not established, and re-signing a stranger's binary with the
wrong key produces something that will not boot. Refusing is the cheap failure.

Nothing here writes, and nothing here guesses. The title update version is
reported only where there is evidence for it; where there is not, tu_version is
None and tu_detail says what was missing. A version that was inferred from a
file size, a modification date or a region letter would look exactly like a
version that was read, and the user would have no way to tell them apart.

This module has never been run against a console that has Call of Duty on it.
The test console's /dev_hdd0/game held five homebrew folders and no games.
"""

import ftplib
import re
from dataclasses import dataclass, field

from ps3diag import isoid
from ps3diag.parsers import parse_ftp_list

from . import titles

GAME_ROOT = "/dev_hdd0/game"

# A PS3 title ID: four letters, five digits. Everything else under
# /dev_hdd0/game is homebrew, a plugin, or somebody's own folder, and none of
# it is this tool's business.
TITLE_DIR = re.compile(r"^[A-Z]{4}\d{5}$")

# Matches the diagnostic collector's cap. A console with more installed titles
# than this is not a case anybody has seen, and walking an unbounded list over
# FTP is how a scan appears to hang.
MAX_TITLE_DIRS = 60

READY = "ready"
NO_UPDATE = "no_update"
NOT_FOUND = "not_found"
UNKNOWN_VARIANT = "unknown_variant"

# Binary names that only a Call of Duty installation has. They are the one
# piece of evidence available for spotting a regional variant that is missing
# from the table: the title ID alone cannot say what a game is, and PARAM.SFO
# cannot be read over the read-only transport.
#
# EBOOT.BIN is deliberately not here, because every PS3 title has one.
# default.self is not here either: it is common enough elsewhere that treating
# it as proof would refuse innocent titles.
COD_MARKERS = frozenset({"t6_ps3f.self", "t6mp_ps3f.self", "default_mp.self"})

# Where the title update version comes from, and where it does not.
# update_for() maps the sha1 of Sony's update package to a version, and that
# package is on Sony's servers rather than on the console's disk, so it can
# never answer this question. The version is written into PARAM.SFO, which
# ps3diag.transport will now read verbatim for exactly these two paths.
#
# Which of the two carries APP_VER on a console that has a title update
# installed is unverified, so both are tried in turn. Neither is required to
# exist: a read that is refused costs the version and nothing else.
PARAM_SFO = ("{path}/PARAM.SFO", "{path}/USRDIR/PARAM.SFO")

NO_VERSION = "The installed title update version could not be read."

NO_READER = (f"{NO_VERSION} This connection has no way to fetch PARAM.SFO "
             f"from the title's folder, and the only other evidence is the "
             f"hash of Sony's update package, which is not on the console.")


@dataclass
class Installation:
    """One title, as found on the console. See docs/screen-interface.md."""

    title_id: str = None
    title_key: str = None
    name: str = None
    short: str = None
    region: str = None
    path: str = None
    usrdir: str = None
    state: str = NOT_FOUND
    files: list = field(default_factory=list)
    expected: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    tu_version: str = None
    tu_detail: str = None
    config: dict = None

    @property
    def ready(self):
        return self.state == READY


@dataclass
class Report:
    installations: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def for_title(self, key):
        """Every installation of one title, by title key or by title ID.

        Never empty for a title this tool knows: a title with nothing installed
        comes back as a single not_found entry, so a screen has something to
        render either way and does not have to invent the absent case itself.
        """
        key = _title_key(key)
        if key is None:
            return []
        found = [item for item in self.installations if item.title_key == key]
        return found or [_absent(key)]


def find_installations(lister, param_sfo_reader=None):
    """Walk /dev_hdd0/game and classify everything Call of Duty in it.

    param_sfo_reader is a callable taking a path and returning the bytes of
    that file. It defaults to the lister's own download_bytes, which is the
    read-only transport's capped binary read. An explicit one overrides that,
    and a lister that has no such method degrades to reporting no version with
    the reason, rather than failing.

    Never raises. A console that stops answering part way leaves the
    installations already found intact and the reason in notes.
    """
    report = Report()
    if param_sfo_reader is None:
        param_sfo_reader = getattr(lister, "download_bytes", None)
    try:
        _walk(lister, report, param_sfo_reader)
    except Exception as exc:                                # noqa: BLE001
        # The promise in the appendix is that this never raises. A screen that
        # has to handle an exception from a scan is a screen that will show a
        # traceback to somebody who wanted to play Black Ops II.
        report.note(f"The console stopped answering while it was being "
                    f"checked, so this list may be incomplete. Check it is "
                    f"still switched on and still on the network, then try "
                    f"again. ({_reason(exc)})")
    return report


def _walk(lister, report, param_sfo_reader):
    try:
        listing = lister.list_dir(GAME_ROOT + "/")
    except ftplib.error_perm:
        # A refusal is an answer: there is no such directory. Every console
        # that has ever had a game installed has one, so this is worth saying.
        report.note("There is no /dev_hdd0/game folder on this console, so "
                    "nothing is installed to the hard drive yet.")
        return

    entries, unparsed = parse_ftp_list(listing)
    if unparsed:
        report.note(f"{len(unparsed)} line(s) of the /dev_hdd0/game listing "
                    f"were in a format this tool does not recognise and were "
                    f"skipped.")

    folders = [entry["name"] for entry in entries
               if entry["kind"] == "directory"
               and TITLE_DIR.match(entry["name"].upper())]
    if len(folders) > MAX_TITLE_DIRS:
        report.note(f"/dev_hdd0/game holds {len(folders)} installed titles. "
                    f"The first {MAX_TITLE_DIRS} were looked inside.")
        folders = folders[:MAX_TITLE_DIRS]

    for name in folders:
        try:
            installation = _examine(lister, name, param_sfo_reader)
        except ftplib.error_perm:
            # Not reachable through _examine, which treats a refusal as an
            # absent USRDIR, but a future caller might list something else.
            continue
        except Exception as exc:                            # noqa: BLE001
            report.note(f"The console stopped answering while "
                        f"{GAME_ROOT}/{name} was being read, so this list may "
                        f"be incomplete. Anything found before that is kept. "
                        f"Check the console is still switched on and on the "
                        f"network, then try again. ({_reason(exc)})")
            break
        if installation is not None:
            report.installations.append(installation)
            _note_installation(report, installation)

    if not report.installations:
        report.note("No Call of Duty installation was found in "
                    "/dev_hdd0/game on this console.")


def _examine(lister, folder, param_sfo_reader):
    """One folder under /dev_hdd0/game, or None if it is nothing to do with us.

    Raises whatever the transport raises for a console that has stopped
    answering; the caller turns that into a note.
    """
    title_id = folder.upper()
    path = f"{GAME_ROOT}/{folder}"
    usrdir = f"{path}/USRDIR"
    config = titles.config_for(title_id)
    if title_id in titles.NOT_A_TITLE:
        # Listed in the table as explicitly not one of ours. config_for already
        # returns None for it; this is here so that an edit which puts it back
        # in the SKU table by accident still cannot make it a Black Ops II.
        config = None

    files = _list_usrdir(lister, usrdir)

    if config is None:
        if not _looks_like_cod(files):
            return None
        # A Call of Duty that is not in the table. Its signing parameters are
        # not known, so it is surfaced in order to be refused, not patched.
        return Installation(
            title_id=title_id, path=path, usrdir=usrdir,
            state=UNKNOWN_VARIANT, files=files or [],
            tu_detail=("This is not one of the SKUs this tool has been "
                       "verified against, so nothing about it was read."))

    sku = titles.sku_for(title_id) or {}
    expected = [record["name"] for record in titles.binaries_for(title_id)]
    present = {item["name"] for item in (files or [])}
    missing = [name for name in expected if name not in present]

    if files is None:
        state = NO_UPDATE
        tu_version, tu_detail = None, (
            "There is no USRDIR in this title's folder, which means the title "
            "update has not been installed yet.")
    else:
        # Literally the appendix rule: ready means the expected binaries are
        # there. A USRDIR that is missing one of them is an update that did not
        # finish, and the advice for the user is the same as for no update at
        # all, so it is reported the same way rather than as a fourth state.
        state = READY if not missing else NO_UPDATE
        tu_version, tu_detail = _title_update(param_sfo_reader, path)

    return Installation(
        title_id=title_id, title_key=config["key"], name=config["name"],
        short=config["short"], region=sku.get("region"), path=path,
        usrdir=usrdir, state=state, files=files or [], expected=expected,
        missing=missing, tu_version=tu_version, tu_detail=tu_detail,
        config=config)


def _list_usrdir(lister, usrdir):
    """The USRDIR contents, or None when there is no USRDIR at all.

    None and [] are different answers and the difference is the whole of the
    no_update case: no USRDIR means the update was never installed, an empty
    one means something else is wrong.
    """
    try:
        listing = lister.list_dir(usrdir + "/")
    except ftplib.error_perm:
        return None
    entries, _unparsed = parse_ftp_list(listing)
    return [{"name": entry["name"], "kind": entry["kind"],
             "size": entry["size"], "modified": entry["modified"]}
            for entry in entries]


def _looks_like_cod(files):
    return any(item["name"] in COD_MARKERS for item in (files or []))


def _title_update(param_sfo_reader, path):
    """(version, detail). None unless a version was actually read.

    Neither candidate path is required to be there and neither read is allowed
    to be fatal: a title with no readable PARAM.SFO is still a title that can
    be scanned and patched, and a guessed version would be indistinguishable
    from a read one.
    """
    if param_sfo_reader is None:
        return None, NO_READER
    reason = None
    for candidate in (name.format(path=path) for name in PARAM_SFO):
        try:
            data = param_sfo_reader(candidate)
        except Exception as exc:                            # noqa: BLE001
            # The first fetch failure is kept rather than the last, because
            # the file in the title's own folder is the one a reader would
            # expect to be told about.
            reason = reason or f"{candidate}: {_reason(exc)}"
            continue
        fields, parse_reason = isoid.parse_param_sfo(data)
        # The console writes 01.19; a person writes 1.19. isoid owns that
        # conversion, and having two answers for it is how a report ends up
        # disagreeing with itself.
        version = isoid._normalise_version(fields.get("APP_VER"))
        if version:
            return version, (f"Read from APP_VER in {candidate}, which the "
                             f"console rewrites when a title update is "
                             f"installed.")
        # A file that was read says more about why than one that was refused,
        # so it replaces whatever reason is held.
        reason = (f"{candidate}: "
                  f"{parse_reason or 'PARAM.SFO carries no APP_VER'}")
    return None, f"{NO_VERSION} ({reason})"


def _note_installation(report, installation):
    if installation.state == UNKNOWN_VARIANT:
        report.note(f"{installation.title_id} looks like a Call of Duty "
                    f"installation but is not one of the versions this tool "
                    f"has been checked against, so it will be left alone.")
    elif installation.state == NO_UPDATE and installation.missing:
        report.note(f"{installation.short} ({installation.title_id}) has a "
                    f"USRDIR, but "
                    f"{_and_list(installation.missing)} is not in it.")


def _absent(key):
    config = titles.TITLES[key]
    expected = [record["name"] for record in config["binaries"]]
    return Installation(
        title_key=key, name=config["name"], short=config["short"],
        state=NOT_FOUND, expected=expected, missing=list(expected),
        config=config,
        tu_detail="No installation of this title was found on the console.")


def _title_key(key):
    """A title key, from either a title key or a title ID."""
    if not isinstance(key, str):
        return None
    key = key.strip()
    if key in titles.TITLES:
        return key
    return titles.KNOWN_TITLE_IDS.get(key.upper())


def _and_list(names):
    names = list(names)
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _reason(exc):
    text = str(exc).strip()
    return text or exc.__class__.__name__
