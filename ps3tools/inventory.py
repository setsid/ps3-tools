"""Everything on one console, keyed on title ID. One walk, one answer.

Five parts of this program used to walk a console for themselves. The patcher
walked /dev_hdd0/game, Game updates walked it again and then every game folder
on every device, Install packages asked a third time for the folder names
alone, Back up save data walked four roots of its own for names, and Transfer
games walked the game folders keyed on file name with no notion of a title ID
at all. Each had its own folder list, and two of them differed. Each had its
own idea of what a title ID looks like. None of them could see what another
had found, which is how Transfer games came to offer fourteen disc images
totalling 144.6 GB with six of those games already installed on the console it
was about to copy them to.

So the walk happens here, once, and the answer is keyed on the only thing that
identifies a game across all four places it can live: its title ID.

What this does not do is read anything out of a file. A name in a listing, the
inside of a disc image, a package's content ID and a signed binary's header are
four genuinely different things to read, and the four readers that do it stay
where they are. This calls the cheapest of them, the one that reads a name,
because a listing is already in hand by then. Anything that has to open a file
is the caller's business and is handed in.

Read-only throughout. Every question here is answered from directory listings
taken through the diagnostic client, and nothing in this module writes, deletes
or asks a console to do anything.
"""

import re
from dataclasses import dataclass, field

from ps3diag import collectors, parsers, regioncodes

#: What a title ID folder is called. One pattern for the whole program: two
#: parts of it disagreeing about the shape of a title ID is how a console ends
#: up with a game that one screen can see and another cannot.
TITLE_ID = re.compile(r"^[A-Z]{4}\d{5}$")

#: Where a console keeps games. The diagnostic collector's list is the one
#: answer to that question and it is imported rather than restated.
#:
#: PKG is left out of the game folders because a package sitting there is an
#: installer waiting to be run rather than a game that is installed. It is very
#: often the title update itself, named after the game it patches, and counting
#: it as a game would put a row on a screen for something nobody has installed.
#: It is asked about separately, as PACKAGES_FOLDER below.
GAME_FOLDERS = tuple(name for name in collectors.GAME_FOLDERS
                     if name != "PKG")

#: Devices looked at when the caller does not say. dev_hdd0 is on every console
#: and everything else has to be discovered.
DEVICES = ("dev_hdd0",)

#: Where the console installs a game, and where it keeps packages waiting.
GAME_ROOT = "/dev_hdd0/game"
PACKAGES_FOLDER = "/dev_hdd0/packages"

#: How many title folders are looked inside. The patcher's own figure, which
#: is the one that has been in front of real consoles.
MAX_TITLES = 60

#: The four places one game can be on a console, in the words the screens use.
INSTALLED = "installed"
DISC_IMAGE = "disc image"
FOLDER_GAME = "folder game"
PACKAGE = "package"

#: File endings that are a disc image rather than a folder of one.
IMAGE_SUFFIXES = (".iso", ".bin", ".img", ".mdf", ".cso")

#: A package's content ID in full, which is how its own header writes it.
CONTENT_ID = re.compile(
    r"^[A-Z]{2}\d{4}-([A-Z]{4}\d{5})_\d{2}-[A-Za-z0-9_]+$")

#: The same at the start of a file name. A package file is named after its
#: content ID and carries an extension the ID itself does not have, and the
#: reader that finds a title ID in an ordinary name cannot see one here: the
#: _00 that follows leaves it no word boundary to stop at, so
#: EP0002-BLES01428_00-MW3PATCH.pkg answers nothing.
_CONTENT_ID_START = re.compile(r"^[A-Z]{2}\d{4}-([A-Z]{4}\d{5})_\d{2}-")


@dataclass
class Place:
    """One game, in one of the places a console can hold it."""

    where: str = ""
    path: str = ""
    #: What the console calls it there: a folder name or a file name.
    name: str = ""
    size: int = 0
    device: str = ""
    folder: str = ""
    #: Whether this folder has a game in it, for the places where that is a
    #: separate question from the folder existing. None until somebody has
    #: looked. See Inventory.mark_empty.
    holds_game: bool = None

    @property
    def is_image(self):
        return self.where == DISC_IMAGE


@dataclass
class Title:
    """One game, and everywhere on this console it was found."""

    title_id: str = ""
    #: The console's own name for it, where a listing gave one. Empty where
    #: every place it was found is named after the title ID and nothing else.
    name: str = ""
    places: list = field(default_factory=list)

    def where(self, kind):
        return [place for place in self.places if place.where == kind]

    @property
    def installed(self):
        """Whether the console has this game installed under /dev_hdd0/game.

        A folder there is not proof that an install finished. It is proof that
        one started, which is why this only ever greys a row or adds a
        sentence and never decides anything on its own.

        A folder that somebody has looked inside and found no game in does not
        count. One console carried /dev_hdd0/game/BLUS31011 holding licence
        files and nothing else, beside the digital release of the same game
        with all its binaries in it. Reading the first as an installed game
        offered its owner a title update the console then refused.

        A folder nobody has looked inside still counts. Not looking is not
        evidence of an empty folder, and the cheap half of this walk does not
        look.
        """
        return any(place.holds_game is not False
                   for place in self.where(INSTALLED))

    @property
    def images(self):
        return self.where(DISC_IMAGE)

    @property
    def folder_games(self):
        return self.where(FOLDER_GAME)

    @property
    def packages(self):
        return self.where(PACKAGE)

    @property
    def region(self):
        return regioncodes.describe_title_id(self.title_id)["region"]

    @property
    def platform(self):
        return regioncodes.describe_title_id(self.title_id)["platform"]

    @property
    def display_name(self):
        """What to put on a row. The name where there is one, the ID where
        there is not: an ID is at least true."""
        return self.name or self.title_id


@dataclass
class Inventory:
    """What one walk of one console found.

    `titles` is keyed on title ID and is the answer to nearly every question.
    `entries` is every listing row exactly as it came back, for the callers
    that are about files rather than about games: Transfer games shows the
    user what is in each folder whether or not it could name the game.
    `unnamed` is the disc images whose names carry no title ID, which are the
    ones worth opening if the caller is willing to pay for it.
    """

    titles: dict = field(default_factory=dict)
    entries: list = field(default_factory=list)
    unnamed: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    #: Per-folder listings as {path: {lowercased name: size}}, which is what a
    #: caller matching on file names rather than on games needs.
    listings: dict = field(default_factory=dict)
    #: True when /dev_hdd0/game could not be read at all. An empty title list
    #: and a console that would not answer are different answers.
    game_root_unknown: bool = False

    def get(self, title_id):
        return self.titles.get(_clean(title_id))

    def installed_ids(self):
        """Title IDs with a folder under /dev_hdd0/game, sorted."""
        return sorted(key for key, title in self.titles.items()
                      if title.installed)

    def mark_empty(self, title_id, empty=True):
        """Record that this title's own folder has no game in it.

        The caller does the looking, because what counts as a game varies with
        what it is already reading: the scan is opening each title's PARAM.SFO
        anyway and a folder that answers 550 for it holds no game, and the
        patcher is listing each USRDIR anyway and reads the answer off that.
        """
        title = self.get(title_id)
        if title is None:
            return False
        for place in title.where(INSTALLED):
            place.holds_game = not empty
        return True

    def empty_folders(self):
        """Title IDs whose own folder was looked in and held no game."""
        return sorted(key for key, title in self.titles.items()
                      if title.where(INSTALLED)
                      and all(place.holds_game is False
                              for place in title.where(INSTALLED)))

    def installed_places(self):
        """Each installed title's own folder, in the order the console gave.

        The console's spelling of the name, and its order. A caller that walks
        into these folders one at a time and stops when the console goes quiet
        keeps whatever it found first, so the order is part of the answer.
        """
        return [place for title in self.titles.values()
                for place in title.where(INSTALLED)]

    def is_installed(self, title_id):
        title = self.get(title_id)
        return bool(title and title.installed)

    def name_for(self, title_id):
        """The console's own name for a title, or None.

        None rather than the title ID, so a caller can tell a name it was
        given from one it would be inventing.
        """
        title = self.get(title_id)
        return (title.name or None) if title else None

    def names(self):
        """{title ID: name} for every title the console named."""
        return {key: title.name for key, title in self.titles.items()
                if title.name}


def _clean(title_id):
    return str(title_id or "").strip().upper()


def _add(inventory, title_id, place, name=""):
    title_id = _clean(title_id)
    if not title_id:
        return None
    title = inventory.titles.get(title_id)
    if title is None:
        title = Title(title_id=title_id)
        inventory.titles[title_id] = title
    title.places.append(place)
    # First name wins, and the places are visited installed-first. A console
    # names the folder it installed into; whoever made a disc image named that.
    if name and not title.name:
        title.name = name
    return title


#: The title ID wherever it sits in a name, with whatever brackets, dashes or
#: underscores are wrapped around it. Both conventions are in the wild and a
#: pattern for one of them throws the name away on the other: consoles write
#: BLES00354-World at War and image dumps write Ghosts [BLES01945].iso.
_ID_ANYWHERE = re.compile(
    r"(?i)[\[(\s\-_]*\b[A-Z]{4}[-_]?\d{5}\b[\])\s\-_]*")
_EXTENSION = re.compile(r"(?i)\.(iso|bin|img|mdf|cso|pkg)$")
_SPACES = re.compile(r"\s+")


def name_in(entry_name, title_id=""):
    """The human part of a listing entry, or "" if there is not one.

    A folder called exactly BLES01717 says nothing a title ID does not already
    say, and returning it as a name would put the same string on the row twice
    while claiming the game had been recognised.
    """
    text = _EXTENSION.sub("", entry_name or "").strip()
    trimmed = _SPACES.sub(" ", _ID_ANYWHERE.sub(" ", text)).strip(" -_.[]()")
    if not trimmed:
        return ""
    if regioncodes.find_title_id(trimmed):
        return ""
    return "" if trimmed.upper() == _clean(title_id) else trimmed


def title_id_in_package(name):
    """The title a package file is for, or "" where its name does not say."""
    match = _CONTENT_ID_START.match(str(name or "").strip().upper())
    return match.group(1) if match else ""


def is_image(entry):
    """Whether a listing entry is a disc image, and so something to open.

    A folder game whose name carries no title ID is left alone: there is no
    image to open, and guessing from a folder's contents is a different
    feature altogether.
    """
    if entry.get("kind") not in (None, "file"):
        return False
    return (entry.get("name") or "").lower().endswith(IMAGE_SUFFIXES)


def installed(lister):
    """Only the titles under /dev_hdd0/game, in one listing.

    The whole walk is seven more listings on a console that gives up when it
    is pushed, and a caller that just wants to know whether a package has
    already gone in should not pay for them.
    """
    found = Inventory()
    _read_installed(lister, found)
    return found


def read(lister, devices=None, folders=None, packages=True):
    """Walk one console and return an Inventory. Never raises.

    A console that stops answering part way leaves what was already found
    intact and puts the reason in notes. Every caller of this is a screen in
    front of somebody, and an exception out of here would replace a sentence
    they can act on with a class name they cannot.
    """
    found = Inventory()
    _read_installed(lister, found)
    _read_game_folders(lister, found, devices, folders)
    if packages:
        _read_packages(lister, found)
    return found


def _read_installed(lister, found):
    """The title folders under /dev_hdd0/game."""
    try:
        listing = lister.list_dir(GAME_ROOT + "/")
    except Exception as exc:                                # noqa: BLE001
        # A refusal is an answer: the folder is not there. Anything else is a
        # console that stopped answering, and the two read differently.
        if _is_refusal(exc):
            found.notes.append(
                f"There is no {GAME_ROOT} folder on this console, so nothing "
                f"is installed to the hard drive yet.")
        else:
            found.game_root_unknown = True
            found.notes.append(
                f"The console stopped answering while {GAME_ROOT} was being "
                f"read, so this list may be incomplete. Check it is still "
                f"switched on, then try again. ({exc.__class__.__name__})")
        return
    entries, unparsed = parsers.parse_ftp_list(listing)
    if unparsed:
        found.notes.append(
            f"{len(unparsed)} line(s) of the {GAME_ROOT} listing were in a "
            f"format this tool does not recognise and were skipped.")
    folders = [entry for entry in entries
               if entry.get("kind") == "directory"
               and TITLE_ID.match((entry.get("name") or "").upper())]
    if len(folders) > MAX_TITLES:
        found.notes.append(
            f"This console has {len(folders)} games installed. The first "
            f"{MAX_TITLES} were checked.")
        folders = folders[:MAX_TITLES]
    for entry in folders:
        name = entry.get("name") or ""
        title_id = name.upper()
        _add(found, title_id,
             Place(where=INSTALLED, path=f"{GAME_ROOT}/{name}", name=name,
                   device="dev_hdd0", folder="game"))


def _read_game_folders(lister, found, devices=None, folders=None):
    """Every entry in the game folders on each device.

    A folder that is not there is passed over without a word. On a normal
    console most of the seven do not exist, and a note for each would bury the
    answer under six lines saying nothing happened.
    """
    for device in devices or DEVICES:
        device = str(device).strip("/")
        if not device:
            continue
        for folder in (folders or GAME_FOLDERS):
            path = f"/{device}/{folder}"
            try:
                listing = lister.list_dir(path + "/")
            except Exception:                               # noqa: BLE001
                continue
            try:
                entries, _unparsed = parsers.parse_ftp_list(listing)
            except Exception:                               # noqa: BLE001
                continue
            found.listings[path] = {}
            for entry in entries[:collectors.MAX_GAME_ENTRIES]:
                name = entry.get("name") or ""
                if name in (".", ".."):
                    continue
                row = dict(entry)
                row["device"] = device
                row["folder"] = folder
                found.entries.append(row)
                found.listings[path][name.lower()] = entry.get("size") or 0
                _place_entry(found, row, device, folder, path)


def _place_entry(found, row, device, folder, path):
    """File it under its title, or keep it for somebody willing to open it."""
    name = row.get("name") or ""
    title_id = regioncodes.find_title_id(name)
    if title_id is None:
        if is_image(row):
            found.unnamed.append(row)
        return
    where = DISC_IMAGE if is_image(row) else FOLDER_GAME
    _add(found, title_id,
         Place(where=where, path=f"{path}/{name}", name=name,
               size=row.get("size") or 0, device=device, folder=folder),
         name=name_in(name, title_id))


def _read_packages(lister, found):
    """What is sitting in the packages folder, by the title each one is for."""
    try:
        listing = lister.list_dir(PACKAGES_FOLDER + "/")
    except Exception:                                       # noqa: BLE001
        # Reported by the caller that is actually about packages. A console
        # with no packages folder is the ordinary case.
        return
    try:
        entries, _unparsed = parsers.parse_ftp_list(listing)
    except Exception:                                       # noqa: BLE001
        return
    for entry in entries:
        name = entry.get("name") or ""
        if name in (".", "..") or entry.get("kind") == "directory":
            continue
        title_id = title_id_in_package(name)
        if not title_id:
            continue
        _add(found, title_id,
             Place(where=PACKAGE, path=f"{PACKAGES_FOLDER}/{name}",
                   name=name, size=entry.get("size") or 0,
                   device="dev_hdd0", folder="packages"))


def _is_refusal(exc):
    """Whether the console answered that the path is not there."""
    return exc.__class__.__name__ == "error_perm"
