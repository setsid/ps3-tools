"""Copying disc images from this computer onto the console, and the queue.

This exists because webMAN's own copy did four things on a real console that
between them cost an evening: it ran for two hours with nothing on screen to
say whether it was working, it created a PS3ISO folder inside PS2ISO because
it trusted a filename, it skipped a file whose name contained an ampersand
without saying so, and there was no way to find out any of it except polling
FTP from a shell. Every rule in this module comes from one of those.

Three of them are the same mistake: a guess presented as a fact. So nothing
here guesses. The platform is read out of the image rather than off the name,
the name that will exist on the console is computed and shown before anything
starts, and the free space is compared against the whole queue before the
first byte is sent rather than 180 GB in.

The fourth is about honesty. A PS3 receives files at roughly four megabytes a
second and no program on this side of the wire changes that. This one does not
pretend otherwise: it says what the queue will cost in hours before the user
commits to it. Making the wait predictable and survivable is the whole of what
is on offer, and claiming more would be the same kind of lie as trusting the
filename.

No Qt and no sockets. The screen supplies a writer and drives this from a
worker thread; everything here is either pure or goes through that writer,
which is what lets the tests run the entire flow with a writer that raises the
moment it is constructed.
"""

import collections
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

from ps3diag import isoid, parsers
from ps3tools import inventory
from ps3tools.updates import free_bytes_for          # one answer to "how much
                                                     # room is there", shared
                                                     # with the two cards that
                                                     # already ask it

# --- what a disc image turns out to be -------------------------------------

PS3 = "PS3ISO"
PS2 = "PS2ISO"
UNKNOWN = ""

#: Where webMAN expects each kind of image to live. The folder name is the
#: whole of how the console tells them apart, which is why putting a PS3 image
#: in PS2ISO is not a cosmetic mistake.
FOLDERS = {PS3: "PS3ISO", PS2: "PS2ISO"}

DEFAULT_DEVICE = "dev_hdd0"

#: A PS3 disc carries this at the root alongside PS3_GAME. It is checked only
#: when PS3_GAME/PARAM.SFO could not be read, so that an image with a damaged
#: or unusual PARAM.SFO is still filed in the right folder rather than being
#: handed back to the user as unidentifiable.
PS3_ROOT_MARKER = "PS3_DISC.SFB"

IMAGE_EXTENSIONS = (".iso",)

#: The folders on a device that hold games, listed before anything is queued so
#: the user can see what is already there. ps3tools.inventory owns the list:
#: two parts of this program disagreeing about where a console keeps its games
#: is how a screen comes to offer a copy of something already there.
BROWSE_FOLDERS = inventory.GAME_FOLDERS


class TransferError(Exception):
    """Something stopped. The message is already fit to show a user."""


class NotEnoughSpace(TransferError):
    pass


# --- reading the image -----------------------------------------------------

def _read_range_for(handle):
    """The callback ps3diag.isoid wants, over an open local file.

    isoid asks for the few tens of kilobytes it is about to parse and nothing
    else, which is what makes a 40 GB image identifiable in well under a
    second from a spinning disk.
    """

    def read_range(offset, length):
        handle.seek(offset)
        return handle.read(length)

    return read_range


def _has_root_file(read, name):
    """Whether a named file sits at the root of any filesystem in the image.

    This reaches into isoid for its walkers on purpose. The alternative is a
    second ISO 9660 and UDF implementation in this file, which would be the
    one that goes wrong on the disc neither of them was tested against; one
    walker with two callers is the lesser evil, and the contract it is used
    through -- a reader, a path, a cap -- is the same one identify() uses.
    """
    for filesystem in isoid._filesystems(read, []):
        if filesystem.open((name,), 4096):
            return True
    return False


def identify_image(path):
    """(platform, identity) for one local image. Never raises.

    identity is an isoid.IsoIdentity, so the caller gets the title and title
    ID for free where the image carried them, and a sentence saying what
    stopped it where it did not.
    """
    try:
        with open(path, "rb") as handle:
            read_range = _read_range_for(handle)
            identity = isoid.identify(read_range)
            if identity.method == isoid.METHOD_SFO:
                return PS3, identity
            if identity.method == isoid.METHOD_CNF:
                return PS2, identity
            # Nothing named itself. One more question before giving up, and
            # only this one: is the marker every PS3 disc carries at its root
            # there? That settles which folder it belongs in without pretending
            # to know which game it is.
            reader = isoid._Reader(read_range)
            if _has_root_file(reader, PS3_ROOT_MARKER):
                identity.reason = (
                    "This is a PlayStation 3 disc image, but the part of it "
                    "that carries the game's name and ID could not be read.")
                return PS3, identity
            return UNKNOWN, identity
    except OSError as exc:
        return UNKNOWN, isoid.IsoIdentity(
            reason=f"This file could not be read ({exc.strerror or exc}).")


# --- the name it will have on the console ----------------------------------

#: Whole bracketed groups go, contents and all. "Gran Turismo 5 [BCES00569]
#: (Europe) (v1.02).iso" is the normal shape of a dumped image and almost all
#: of its length is in those groups; dropping them is most of what keeps the
#: name short enough to read on a television.
_BRACKETED = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")

#: The ampersand is first in this list because it is the character that broke
#: the real transfer: the file was silently skipped and nobody found out until
#: the game was not on the console. The rest are the ones that make an FTP
#: path or a console listing awkward. Apostrophes go too, which costs nothing
#: and saves an argument with a shell somewhere down the line.
_UNWANTED = re.compile(r"[&,;:\[\]\(\)\{\}<>\"'`|?*/\\#%$!~^=+]")

_WHITESPACE = re.compile(r"\s+")

#: Long names list badly on the console: the XMB truncates them and webMAN's
#: own list runs off the side. Short enough to read, long enough to tell two
#: games apart.
MAX_STEM = 42


def remote_name(filename, title_id=None):
    """The name this file will have on the console, computed not guessed.

    Shown to the user before anything starts, because a rename they did not
    expect is indistinguishable from a file that went missing.
    """
    stem, extension = os.path.splitext(os.path.basename(filename or ""))
    extension = (extension or ".iso").lower()
    # Accents and the like are folded to ASCII rather than dropped outright:
    # the FTP control connection is latin-1 and a console listing is not
    # reliably anything, so a name that is plain ASCII is a name that arrives
    # intact. "Pokémon" becoming "Pokemon" is a smaller loss than a 550.
    stem = unicodedata.normalize("NFKD", stem)
    stem = stem.encode("ascii", "ignore").decode("ascii")
    stem = _BRACKETED.sub(" ", stem)
    stem = _UNWANTED.sub(" ", stem)
    stem = _WHITESPACE.sub(" ", stem).strip(" .-_")
    if len(stem) > MAX_STEM:
        cut = stem[:MAX_STEM]
        # Cut at a word rather than mid-syllable where there is one to cut at.
        if " " in cut[MAX_STEM // 2:]:
            cut = cut[:cut.rfind(" ")]
        stem = cut.strip(" .-_")
    if not stem:
        # Everything in the name was brackets and punctuation. The title ID is
        # the only other thing known about it, and "GAME" is better than a file
        # called ".iso" that the console will not show at all.
        stem = (title_id or "GAME").strip() or "GAME"
    return stem + extension


def _free_name(wanted, taken):
    """wanted, or "wanted 2", "wanted 3"... Case-insensitive, like the console."""
    if wanted.lower() not in taken:
        return wanted
    stem, extension = os.path.splitext(wanted)
    for number in range(2, 1000):
        candidate = f"{stem} {number}{extension}"
        if candidate.lower() not in taken:
            return candidate
    raise TransferError(
        f"There are already too many files on the console called something "
        f"like {wanted}. Rename this one on your computer first.")


# --- the queue -------------------------------------------------------------

WAITING = "waiting"
SENDING = "sending"
DONE = "done"
ALREADY = "already there"
RESUMING = "resuming"
PARTIAL = "part copied"
FAILED = "failed"
SKIPPED = "not copied"


@dataclass
class QueueItem:
    """One image, and everything the user is shown about it before starting."""

    path: str = ""
    filename: str = ""
    size: int = 0
    platform: str = UNKNOWN
    device: str = DEFAULT_DEVICE
    name: str = ""
    title: str = None
    title_id: str = None
    reason: str = None
    status: str = WAITING
    #: Bytes already on the console under this name, from a run that stopped.
    resume_from: int = 0
    sent: int = 0
    detail: str = ""
    #: What the console already has under this name: "" for nothing, "same"
    #: for a file of the same size, "shorter" for one that stopped part way,
    #: "different" for one that is longer and so is not this file at all.
    present: str = ""
    present_bytes: int = 0
    #: True when this game is installed on the console under its own title ID
    #: in /dev_hdd0/game. A different question from `present`, which is about
    #: a file of this name in the folder this one is going to, and the two
    #: want different things done about them. An installed game and an image
    #: of it are not the same thing, so it is said rather than assumed. The
    #: row arrives unticked: a copy that takes hours and changes nothing must
    #: never start by accident, which is the same rule an image of this name
    #: already in the destination folder has always followed.
    installed: bool = False
    #: Whether to copy it. Untick a file the console already has; tick it to
    #: send it again over the top. Never decided silently: a tool that quietly
    #: declines to copy something is how somebody ends up with a missing game
    #: and no explanation for it.
    wanted: bool = True
    #: The user's own answer about this row, or None where they have not given
    #: one. Kept apart from `wanted` because a scan is free to change its mind
    #: about a row nobody has touched and is never free to change its mind
    #: about a row somebody has.
    #:
    #: Both directions matter. Reading the console again re-ticked every row
    #: it did not already hold, which threw away eight unticks and sent eight
    #: games. And a row that arrives unticked because the game is installed
    #: already has to stay ticked once the user says they want it after all.
    ticked: bool = None

    @property
    def declined(self):
        """Whether the user took the tick out of this row by hand."""
        return self.ticked is False
    #: Set only when the user has been told the console already has this file
    #: and has ticked it anyway. Kept apart from wanted because the two answer
    #: different questions: a file that was never checked against the console
    #: is wanted and must still not be copied over the top of an identical one
    #: found at the last moment. Hours of somebody's evening turn on that.
    overwrite: bool = False

    @property
    def identified(self):
        return self.platform in FOLDERS

    @property
    def destination(self):
        if not self.identified:
            return ""
        return f"/{self.device}/{FOLDERS[self.platform]}"

    @property
    def remote_path(self):
        return f"{self.destination}/{self.name}" if self.destination else ""

    @property
    def size_text(self):
        return parsers.human_size(self.size)

    @property
    def describes_as(self):
        """What it is, in the words of whoever pressed the button on the dump."""
        if not self.identified:
            return "Not recognised"
        kind = "PlayStation 3 game" if self.platform == PS3 \
            else "PlayStation 2 game"
        if self.title and self.title_id:
            return f"{kind}: {self.title} ({self.title_id})"
        if self.title_id:
            return f"{kind}: {self.title_id}"
        return kind

    @property
    def remaining(self):
        return max(0, self.size - self.resume_from)

    @property
    def plan_text(self):
        """What is about to happen to this one, in a column of its own.

        The three "it is already there" cases are deliberately not collapsed
        into one sentence. They want different things done about them: one is
        a copy that need not be repeated, one is the remains of a run that
        stopped and should be carried on, and one is a different file that
        happens to share a name and must not be written over.
        """
        if not self.identified:
            return "Not recognised, so it will not be copied"
        # Once a run has been through it, the column says what happened rather
        # than what was going to happen. A table still describing its plan
        # after the event is how somebody comes away believing a file copied.
        # The two "it is on the console" outcomes give way to a fresh tick,
        # because a tick put in after the run is a decision about the next one
        # and a column still reading "Copied" would hide it.
        again = self.wanted and self.overwrite
        if self.status == DONE and not again:
            return "Copied, and it is on the console now"
        if self.status == ALREADY and not again:
            return "Already on the console; not copied again"
        if self.status == PARTIAL:
            return (f"Part copied ({parsers.human_size(self.resume_from)} of "
                    f"{self.size_text}); running this again carries on")
        if self.status == FAILED:
            return "Did not copy"
        if self.status == SKIPPED:
            return "Not copied"
        if self.present == "same":
            if self.wanted and self.overwrite:
                return "Already on the console; it will be copied over again"
            return "Already on the console; it will not be copied"
        if self.installed:
            # Said before the bare "not ticked" below, which is true of this
            # row and does not say why it arrived that way.
            if self.wanted:
                return (f"{self.title_id} is already installed on this "
                        f"console. This is the disc image of it, which is a "
                        f"separate thing, and it will be copied")
            return (f"{self.title_id} is already installed on this console. "
                    f"This is the disc image of it, which is a separate "
                    f"thing. Tick it to copy it as well")
        if not self.wanted:
            return "Not ticked, so it will not be copied"
        if self.present == "shorter":
            return (f"Part copied already "
                    f"({parsers.human_size(self.present_bytes)} of "
                    f"{self.size_text}); it will carry on from there")
        if self.present == "different":
            return (f"A different file of that name is already there; this "
                    f"one will be called {self.name}")
        return "It will be copied"


def inspect(path, device=DEFAULT_DEVICE):
    """One chosen file, read rather than assumed."""
    filename = os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return QueueItem(path=path, filename=filename, status=FAILED,
                         reason=f"This file could not be read "
                                f"({exc.strerror or exc}).")
    platform, identity = identify_image(path)
    item = QueueItem(path=path, filename=filename, size=size,
                     platform=platform, device=device,
                     title=identity.title, title_id=identity.title_id,
                     reason=identity.reason)
    item.name = remote_name(filename, identity.title_id)
    return item


def build_queue(paths, device=DEFAULT_DEVICE, existing=None):
    """A queue with its final names already decided.

    Collisions between two chosen files are settled here, before the user is
    shown anything, so the names in the table are the names that will exist.
    """
    items = [inspect(path, device) for path in paths]
    assign_names(items, existing)
    return items


def assign_names(items, existing=None):
    """Give every item a name nothing else in the queue is using.

    Shortening names makes collisions likelier, not rarer: two dumps of the
    same game that differed only in their bracketed region tags come out of
    remote_name() identical. Renaming afterwards is the price of a name that
    reads well, and the user sees the result before pressing anything.
    """
    taken = {str(name).lower() for name in (existing or ())}
    for item in items:
        if not item.identified:
            continue
        item.name = _free_name(item.name or remote_name(item.filename,
                                                        item.title_id), taken)
        taken.add(item.name.lower())
    return items


def unidentified(items):
    return [item for item in items if not item.identified]


def sendable(items):
    return [item for item in items if item.identified]


def chosen(items):
    """The ones that will actually be sent: recognised, and ticked."""
    return [item for item in sendable(items) if item.wanted]


def total_bytes(items):
    return sum(item.size for item in sendable(items))


def bytes_to_send(items):
    """What is actually left to push, once partials on the console are counted."""
    return sum(item.remaining for item in chosen(items))


def installed_and_ticked(items):
    """The ones whose game is installed already and which are still ticked.

    These are the difference between the two totals the estimate offers.
    """
    return [item for item in installed_on_console(items) if item.wanted]


def bytes_without_installed(items):
    """What would be left to push if those ones were unticked instead.

    Nothing here unticks anything. A running total that counted eight games
    the console could already play was the whole of the complaint, and the
    answer to it is the second figure rather than a decision taken on the
    user's behalf.
    """
    # Identity rather than equality: QueueItem is a dataclass, so two rows
    # describing the same image compare equal and neither can go in a set.
    skip = {id(item) for item in installed_and_ticked(items)}
    return sum(item.remaining for item in chosen(items)
               if id(item) not in skip)


# --- what is already on the console ----------------------------------------

@dataclass
class ConsoleFile:
    """One thing already in a game folder on the console."""

    folder: str = ""
    name: str = ""
    size: int = 0
    directory: bool = False

    @property
    def size_text(self):
        return "" if self.directory else parsers.human_size(self.size)


def read_listing(lister, path):
    """{name lowercased: size} for one folder, or None if it is not there.

    None and {} are different answers and are kept apart. A console with no
    PSPISO folder has not told us it has no PSP games; it has told us nothing,
    and a folder that does not exist is not a fault worth a sentence.
    """
    try:
        text = lister.list_dir(path)
    except Exception:                                     # noqa: BLE001
        return None
    entries, _unparsed = parsers.parse_ftp_list(text)
    return {entry["name"].lower(): entry
            for entry in parsers.real_entries(entries)}


def console_games(lister, device=DEFAULT_DEVICE, folders=BROWSE_FOLDERS):
    """(files, listings, installed) for the game folders on one device.

    Read-only, through the diagnostic client, and done as part of the first
    scan rather than behind a button: somebody about to spend thirteen hours
    copying a game the console already has should find that out before they
    start.

    One walk, in ps3tools.inventory, which is also what answers the third of
    these. A game installed under its own title ID is a folder under
    /dev_hdd0/game and matches no file name in any of these folders, so the
    listings alone could never see it.
    """
    found = inventory.read(lister, devices=[device], folders=folders,
                           packages=False)
    files = [ConsoleFile(folder=row.get("folder") or "",
                         name=row.get("name") or "",
                         size=row.get("size") or 0,
                         directory=row.get("kind") == "directory")
             for row in found.entries]
    files.sort(key=lambda item: (item.folder, item.name.lower()))
    return files, found.listings, found.installed_ids()


def _proposed(item):
    """Whether to offer this row ticked, once the user has had their say.

    A game the console has installed already arrives unticked. Nine ticked
    rows and a paragraph asking somebody to untick what they do not need is
    how a 141 GB queue starts by accident, and it is the same situation as an
    image of this name already sitting in the destination folder, which has
    always arrived unticked.
    """
    if item.ticked is not None:
        return item.ticked
    return not item.installed


def match_console(items, listings, installed=()):
    """Mark each queued file against what the console already holds.

    Two separate questions, and they were being asked as one.

    The first is whether a file of this name is already in the folder this one
    is going to. Matched on the name it will have after renaming, because that
    is the name that will exist on the console; matching on the name on this
    computer would miss every file that is about to be shortened, which is
    most of them. Size is used as well wherever both are known, and it is what
    separates the three cases: the same file, the remains of one, and a
    different one.

    The second is whether this game is already installed on the console under
    its own title ID. `installed` is those title IDs. Fourteen images were
    offered as 144.6 GB and ten and a half hours of copying with every one
    ticked, and six of them were games sitting in /dev_hdd0/game already. The
    file names matched nothing because an installed game is not a file.

    The two answers do different things. A duplicate image is a duplicate and
    is unticked. An installed game is a different thing from an image of it,
    and somebody may well want both, so that one is said and left ticked for
    the user to decide.
    """
    known = {str(title_id).upper() for title_id in (installed or ())}
    for item in items:
        item.present = ""
        item.present_bytes = 0
        item.installed = bool(item.title_id
                              and item.title_id.upper() in known)
        if not item.identified:
            item.wanted = False
            item.overwrite = False
            continue
        found = listings.get(item.destination, {}).get(item.name.lower())
        if found is None:
            # The console having nothing under this name says nothing about
            # whether the user wants it sent, and reading the console again
            # used to answer that on their behalf every time the button was
            # pressed. A game already installed arrives unticked; everything
            # else arrives ticked; the user's own answer beats both.
            item.wanted = _proposed(item)
            # Permission to write over the top was given about the file that
            # was on the console at the time. The console has nothing under
            # this name now, so that permission has stopped meaning anything,
            # and leaving it set would disarm the last check in plan() if an
            # identical file turned up under the name before the copy.
            item.overwrite = False
            continue
        item.present_bytes = found
        if found == item.size:
            item.present = "same"
            if item.wanted and item.overwrite:
                # Already told, already ticked, and nothing has changed about
                # what is there. Reading the console again must not quietly
                # undo a decision the user made looking at the row that said
                # the file was already on it.
                continue
            # Shown, marked, and left to the user. Unticked is a proposal, not
            # a decision: ticking it again copies over the top.
            item.wanted = False
            item.overwrite = False
        elif found < item.size:
            # Part copied, so it is offered as something to carry on with.
            # That is a proposal about a row nobody has touched, and it gives
            # way to a user who has already said no to this one.
            item.present = "shorter"
            item.wanted = _proposed(item)
            item.overwrite = False
        else:
            item.present = "different"
            item.wanted = _proposed(item)
            item.overwrite = False
    return items


#: The leads for the four things the console can already have to say about a
#: queued file. They live here so that the table, the confirmation box and the
#: panel afterwards all say it in the same words; the fault being fixed was in
#: part that the confirmation said nothing at all.
ALREADY_LEAD = ("These are already on the console. The name and the size both "
                "match what is there, so they will not be copied again:")

PART_LEAD = ("These are part copied on the console already. Each one carries "
             "on from where it stopped:")

DIFFERENT_LEAD = ("The console already has a different file under each of "
                  "these names. Each one is copied under a name of its own so "
                  "that what is already there is left alone:")

OVERWRITE_LEAD = ("These are already on the console and you have ticked them, "
                  "so they will be copied over the top:")

#: Why the installed ones are still ticked, on its own. The block above the
#: queue says which games they are in its heading, so the first sentence of
#: INSTALLED_LEAD would be the same fact a second time there.
#: Why the block above the queue exists, for a row nobody has ticked yet.
INSTALLED_WHY = ("A disc image is a separate thing from an installed game and "
                 "there are reasons to want both, so these arrive unticked. "
                 "Tick any you do want")

#: The same games at the point of confirming, where the user has ticked them
#: and the box is describing what is about to happen rather than proposing it.
INSTALLED_LEAD = ("These games are already installed on this console and you "
                  "have ticked them, so their disc images will be copied:")


def already_on_console(items):
    """The ones the console holds in full that are not going to be sent."""
    return [item for item in items
            if item.identified and item.present == "same"
            and not (item.wanted and item.overwrite)]


def part_on_console(items):
    """The ones the console holds part of, which carry on from there."""
    return [item for item in items
            if item.identified and item.present == "shorter"]


def clashing_on_console(items):
    """The ones whose name on the console is taken by a different file."""
    return [item for item in items
            if item.identified and item.present == "different"]


def overwriting(items):
    """The ones the console holds in full that the user has ticked anyway."""
    return [item for item in items
            if item.identified and item.present == "same"
            and item.wanted and item.overwrite]


def installed_on_console(items):
    """The ones whose game is installed on the console already.

    Kept apart from already_on_console, which is about a file of this name in
    the folder this one is going to. These are about the game rather than the
    file, so they are said in different words and nothing is unticked for
    them.
    """
    return [item for item in items
            if item.identified and item.installed and item.present != "same"]


def console_notes(items):
    """Lines saying what the console already holds. Empty when it holds none.

    A queue that has never been checked against the console produces nothing
    here, which is a different thing from a queue that was checked and found
    clear. The caller that asked for the check is the one that knows which it
    has, so this does not guess on its behalf.
    """
    lines = []
    for lead, group in ((ALREADY_LEAD, already_on_console(items)),
                        (PART_LEAD, part_on_console(items)),
                        (DIFFERENT_LEAD, clashing_on_console(items)),
                        (OVERWRITE_LEAD, overwriting(items)),
                        (INSTALLED_LEAD, installed_on_console(items))):
        if not group:
            continue
        if lines:
            lines.append("")
        lines.append(lead)
        lines += [_listed(item, lead) for item in group]
    return lines


def describe_installed(item):
    """One installed game, named by both the things that identify it.

    Both where both are known. The name is what somebody recognises and the
    title ID is the folder on the console they would go and look in. Shared
    with the screen so that the block above the queue and the confirmation
    box name the same game the same way.
    """
    said = ", ".join(part for part in (item.title, item.title_id) if part)
    return f"{item.name} ({said})" if said else item.name


def _listed(item, lead):
    """One line under a lead. The installed group names the game as well.

    The other four groups are about a file of that name on the console, so the
    name is the whole of what identifies it. This one is about the game, and a
    row reading only Ghosts.iso does not say which title it clashes with.
    """
    if lead is not INSTALLED_LEAD:
        return f"    {item.name}"
    return f"    {describe_installed(item)}"


def settle_after_run(items):
    """Bring "what the console has" up to date with the run that just ended.

    This is the fault that was reported. A game that had just finished copying
    kept its tick, the button went live again the moment the run ended, and
    pressing it started the whole file a second time; the list of what is on
    the console was only read when the screen was entered, so it still said
    the game was not there. The run has since listed the folder and proved
    otherwise, and that is what the queue is set to say.
    """
    for item in items:
        if item.status in (DONE, ALREADY):
            item.present = "same"
            item.present_bytes = item.size
            item.wanted = False
            item.overwrite = False
        elif item.status == PARTIAL and item.resume_from:
            # What a stopped run left behind is a short file under the right
            # name, and the next run should be offered as carrying on from it.
            item.present = "shorter"
            item.present_bytes = item.resume_from
            item.overwrite = False
    return items


# --- free space ------------------------------------------------------------

#: Left free afterwards. A PS3 hard drive filled to the last byte starts
#: failing at things that have nothing to do with this program, and a game
#: written into the last gigabyte of a drive is how that happens.
SPACE_MARGIN = 1024 * 1024 * 1024


def check_space(free, needed, device=DEFAULT_DEVICE):
    """Refuse before the first byte, or return quietly.

    free is None when the console did not report it. None is not zero: a
    console whose webMAN words its status page differently has not said there
    is no room, and refusing on that basis would refuse everybody.
    """
    if free is None:
        return None
    if free >= needed + SPACE_MARGIN:
        return None
    raise NotEnoughSpace(
        f"There is not enough room on the console. What you have chosen "
        f"needs {parsers.human_size(needed)} and {device} has "
        f"{parsers.human_size(free)} free.\n\n"
        f"Delete a game you have finished from the console, or take some "
        f"files out of the list, and try again. Nothing has been copied.")


def free_for(devices, device=DEFAULT_DEVICE):
    """Free bytes on one device, out of what the storage collector reported."""
    return free_bytes_for(devices, device)


def storage_devices(devices):
    """Device names that can hold a game, in the order a person would expect."""
    names = []
    for entry in devices or []:
        name = (entry.get("device") or "").lower()
        if name.startswith("dev_hdd") or name.startswith("dev_usb"):
            names.append(name)
    if DEFAULT_DEVICE not in names:
        names.insert(0, DEFAULT_DEVICE)
    return sorted(set(names), key=lambda item: (not item.startswith("dev_hdd"),
                                                item))


# --- how long it is going to take, said out loud ---------------------------

#: What a PS3 actually manages over FTP, measured rather than hoped for. Used
#: only for the estimate shown before starting; once bytes are moving the real
#: rate replaces it.
ASSUMED_RATE = 4 * 1024 * 1024

HONEST_SPEED = (
    "Copying a game to the console is slow, and this program cannot make it "
    "faster. The console takes files at about 4 MB a second however they are "
    "sent to it, so one game can take several hours and a full drive can take "
    "days. What this does instead is tell you beforehand how long it will "
    "take, show you it happening, and carry on from where it stopped if "
    "something interrupts it.")

TIPS = (
    "Connect the console to the router with a network cable rather than "
    "Wi-Fi. Of everything on this list, this is the one that makes a real "
    "difference.",
    "A faster hard drive, or an SSD, in the console helps somewhat. It is "
    "worth having for other reasons, but do not expect it to transform this.",
    "Leave the console on its main menu with no game running while this "
    "happens, so it is not doing something else at the same time.",
    "Stop this computer going to sleep partway through. A sleeping computer "
    "stops the copy; it can be picked up again afterwards, but only once you "
    "come back to it.",
)


def format_duration(seconds):
    """A length of time as somebody would say it, deliberately vague."""
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "less than a minute"
    minutes = seconds // 60
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes / 60.0
    if hours < 24:
        rounded = round(hours * 2) / 2
        if rounded == int(rounded):
            rounded = int(rounded)
        return f"about {rounded} hour{'s' if rounded != 1 else ''}"
    days = hours / 24.0
    rounded = round(days * 2) / 2
    if rounded == int(rounded):
        rounded = int(rounded)
    return f"about {rounded} day{'s' if rounded != 1 else ''}"


def format_rate(bytes_per_second):
    if not bytes_per_second:
        return ""
    return f"{parsers.human_size(int(bytes_per_second))} a second"


def estimate_seconds(count, rate=ASSUMED_RATE):
    if not count or not rate:
        return None
    return count / float(rate)


def _sized(files, count, rate):
    """A queue said as its three numbers: files, bytes, hours."""
    how_long = format_duration(estimate_seconds(count, rate))
    return (f"{files} file{'s' if files != 1 else ''}, "
            f"{parsers.human_size(count)} and {how_long}")


def without_installed_estimate(items, rate=ASSUMED_RATE):
    """The same queue costed with the already-installed games left out.

    Empty when there are none of them, so a queue with nothing installed
    reads exactly as it did. On the console this came from, the figure on
    screen was 144.6 GB and ten and a half hours, and eight of the fourteen
    rows behind it were games sitting in /dev_hdd0/game. Both numbers are
    given because the choice between them is the user's.
    """
    group = installed_and_ticked(items)
    if not group:
        return ""
    count = bytes_without_installed(items)
    one = len(group) == 1
    says = ("That total includes one game this console already has "
            "installed." if one else
            f"That total includes {len(group)} games this console already "
            f"has installed.")
    without = "Without it" if one else "Without them"
    if not count:
        return f"{says} {without} there is nothing left to copy."
    left = _sized(len(chosen(items)) - len(group), count, rate)
    return f"{says} {without} the queue is {left}."


def time_estimate(items, rate=ASSUMED_RATE):
    """The sentence shown before the user commits to the queue."""
    count = bytes_to_send(items)
    if not count:
        return "There is nothing left to copy."
    files = len(chosen(items))
    how_long = format_duration(estimate_seconds(count, rate))
    said = (f"{files} file{'s' if files != 1 else ''}, "
            f"{parsers.human_size(count)} to copy. At the speed a PS3 "
            f"normally manages that is {how_long}. You can leave it running "
            f"and come back to it.")
    rest = without_installed_estimate(items, rate)
    return f"{said}\n\n{rest}" if rest else said


# --- progress --------------------------------------------------------------

class RateMeter:
    """Bytes per second over a short window, so the figure settles but moves.

    A rate taken over the whole transfer is useless: it takes an hour to
    notice that the console has slowed down. A rate taken between two blocks
    is noise. Twenty seconds is long enough to be steady and short enough to
    react.
    """

    WINDOW = 20.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._samples = collections.deque()

    def add(self, done):
        now = self._clock()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.WINDOW:
            self._samples.popleft()

    @property
    def rate(self):
        if len(self._samples) < 2:
            return 0.0
        (first_time, first_bytes) = self._samples[0]
        (last_time, last_bytes) = self._samples[-1]
        span = last_time - first_time
        if span <= 0:
            return 0.0
        return max(0.0, (last_bytes - first_bytes) / span)


@dataclass
class Progress:
    """One update, complete enough that the screen does no arithmetic."""

    stage: str = SENDING
    index: int = 0
    count: int = 0
    name: str = ""
    file_done: int = 0
    file_total: int = 0
    overall_done: int = 0
    overall_total: int = 0
    rate: float = 0.0
    eta: float = None

    @property
    def file_percent(self):
        if not self.file_total:
            return 0
        return min(100, int(self.file_done * 100 / self.file_total))

    @property
    def overall_percent(self):
        if not self.overall_total:
            return 0
        return min(100, int(self.overall_done * 100 / self.overall_total))

    def headline(self):
        if self.stage == "listing":
            return "Looking at what is already on the console"
        if self.stage == "checking":
            return "Checking what arrived"
        if self.stage == "done":
            return "Finished"
        return f"Copying {self.name}"

    def detail(self):
        """The line that made the difference: bytes, rate, what is left."""
        if self.stage != SENDING:
            return ""
        parts = [f"File {self.index} of {self.count}",
                 f"{parsers.human_size(self.file_done)} of "
                 f"{parsers.human_size(self.file_total)}"]
        rate = format_rate(self.rate)
        if rate:
            parts.append(rate)
        if self.eta is not None:
            parts.append(f"{format_duration(self.eta)} left")
        return "   ".join(parts)

    def overall_detail(self):
        return (f"{parsers.human_size(self.overall_done)} of "
                f"{parsers.human_size(self.overall_total)} copied in total")


# --- pause and cancel ------------------------------------------------------

class Controller:
    """Pause and stop, asked between chunks rather than between files.

    Between files is not good enough and this is the reason: a 36 GB image is
    a nine hour file. A stop button that waits for the current file is a stop
    button that does not work.
    """

    def __init__(self, sleep=time.sleep):
        self._sleep = sleep
        self._paused = False
        self._cancelled = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def cancel(self):
        self._cancelled = True
        self._paused = False

    @property
    def paused(self):
        return self._paused

    @property
    def cancelled(self):
        return self._cancelled

    def should_continue(self):
        """False to stop. Blocks while paused, which is what pause means."""
        while self._paused and not self._cancelled:
            self._sleep(0.05)
        return not self._cancelled


# --- the run ---------------------------------------------------------------

@dataclass
class Report:
    sent: list = field(default_factory=list)
    already: list = field(default_factory=list)
    partial: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    renamed: list = field(default_factory=list)
    cancelled: bool = False
    bytes_sent: int = 0
    seconds: float = 0.0
    checked: bool = False
    check_reason: str = ""


def _listing(writer, path):
    """{name lowercased: size} for one folder on the console.

    Fresh every time it is asked for. A listing taken before the transfer is
    not evidence about what is there after it, and the whole point of the
    check at the end is that it is evidence.
    """
    try:
        text = writer.list_dir(path)
    except Exception as exc:                              # noqa: BLE001
        raise TransferError(
            f"The folder {path} on the console could not be read "
            f"({exc.__class__.__name__}). The console may have been switched "
            f"off, or it may not have that folder.") from exc
    entries, _unparsed = parsers.parse_ftp_list(text)
    return {entry["name"].lower(): entry["size"] for entry in entries
            if entry["kind"] != "directory"}


class Transfer:
    """The whole job: prepare, send, check. Driven from a worker thread.

    Everything that talks to the console goes through writer, which the screen
    supplies and the tests replace. There is no fallback path that builds one
    of its own, deliberately: this is the third time in this project that a
    new feature has made a previously inert code path live inside the test
    suite and sent it at a real address on this network, and a constructor
    this class cannot reach is the only version of the fix that holds.
    """

    def __init__(self, items, writer, control=None, free_bytes=None,
                 clock=time.monotonic):
        self.items = list(items)
        self.writer = writer
        self.control = control or Controller()
        self.free_bytes = free_bytes
        self._clock = clock
        self._meter = RateMeter(clock)
        self._report = Report()
        self._overall_done = 0
        self._overall_total = 0
        self._on_progress = None

    # -- preparation, all of it before the first byte

    def prepare(self):
        """Folders, existing files, resume points, names, space. In that order.

        The space check is last because it cannot be right until the resume
        points are known: refusing a 36 GB queue for want of room when 32 GB
        of it is already on the console would be a false refusal.
        """
        queue = chosen(self.items)
        for item in self.items:
            if not item.identified:
                item.status = SKIPPED
                item.detail = "Not recognised, so it was not copied."
                self._report.skipped.append(item)
            elif not item.wanted:
                # Said out loud in the report rather than quietly dropped. The
                # user unticked it, but they are still told it did not go.
                item.status = SKIPPED
                item.detail = (
                    "Already on the console, and you left it unticked, so it "
                    "was not copied again."
                    if item.present == "same" else
                    "You did not tick this one, so it was not copied.")
                self._report.skipped.append(item)
        folders = []
        for item in queue:
            if item.destination not in folders:
                folders.append(item.destination)
        listings = {}
        for folder in folders:
            # MKD treats "already there" as success, so this is also how the
            # missing destination folder case is handled: it is created rather
            # than reported as a fault the user has to go and fix by hand.
            try:
                self.writer.make_dir(folder)
            except Exception:                             # noqa: BLE001
                pass
            listings[folder] = _listing(self.writer, folder)
        plan(queue, listings, self._report)
        self._overall_total = sum(item.size for item in queue)
        self._overall_done = sum(item.resume_from for item in queue
                                 if item.status in (RESUMING, ALREADY))
        check_space(self.free_bytes, bytes_to_send(queue),
                    queue[0].device if queue else DEFAULT_DEVICE)
        return listings

    # -- sending

    def run(self, on_progress=None):
        self._on_progress = on_progress
        started = self._clock()
        self._emit(Progress(stage="listing", count=len(chosen(self.items))))
        self.prepare()
        count = len(chosen(self.items))
        for index, item in enumerate(chosen(self.items), start=1):
            if item.status == ALREADY:
                self._report.already.append(item)
                continue
            if not self.control.should_continue():
                item.status = SKIPPED
                item.detail = "Stopped before this one was started."
                self._report.skipped.append(item)
                continue
            self._send(item, index, count)
        self._report.cancelled = self.control.cancelled
        self._report.seconds = self._clock() - started
        self._check()
        self._emit(Progress(stage="done", count=count,
                            overall_done=self._overall_done,
                            overall_total=self._overall_total))
        return self._report

    def _send(self, item, index, count):
        # Read the size again rather than trusting the queue. A file can be
        # moved, renamed or deleted between the user choosing it and this
        # reaching it, and an upload of nothing under a game's name is worse
        # than a refusal, because the console will show it and it will not run.
        try:
            live_size = os.path.getsize(item.path)
        except OSError:
            item.status = FAILED
            item.detail = ("This file is no longer on your computer. It was "
                           "not copied.")
            self._report.failed.append(item)
            return
        if live_size != item.size:
            # Changed under us. The resume point was worked out against the old
            # length and is no longer trustworthy, so start this one again.
            item.size = live_size
            item.resume_from = 0
        item.status = SENDING
        base = self._overall_done - item.resume_from

        def on_block(done, total):
            item.sent = done
            self._overall_done = base + done
            self._meter.add(self._overall_done)
            rate = self._meter.rate
            left = max(0, self._overall_total - self._overall_done)
            self._emit(Progress(
                stage=SENDING, index=index, count=count, name=item.name,
                file_done=done, file_total=total,
                overall_done=self._overall_done,
                overall_total=self._overall_total, rate=rate,
                eta=(left / rate) if rate else None))

        try:
            result = self.writer.store_resumable(
                item.path, item.remote_path, on_block=on_block,
                should_continue=self.control.should_continue,
                resume_from=item.resume_from)
        except Exception as exc:                          # noqa: BLE001
            item.status = FAILED
            item.detail = (
                f"The copy stopped ({exc.__class__.__name__}). What reached "
                f"the console is kept, so running this again carries on from "
                f"where it got to rather than starting over.")
            self._report.failed.append(item)
            # The console is what failed, not this file. Stopping the queue
            # here is deliberate: ten more files against a console that has
            # gone is ten more identical failures and half an hour of waiting
            # for them.
            self.control.cancel()
            return
        sent = int(result.get("sent", 0))
        landed = int(result.get("bytes", item.resume_from + sent))
        self._report.bytes_sent += sent
        self._overall_done = base + landed
        if result.get("complete"):
            item.status = DONE
            item.detail = ""
            self._report.sent.append(item)
        else:
            item.status = PARTIAL
            item.resume_from = landed
            item.detail = (
                f"Stopped after {parsers.human_size(item.resume_from)} of "
                f"{parsers.human_size(item.size)}. The part that arrived is "
                f"kept; starting this again carries on from there. The "
                f"console will not play it until it is finished.")
            self._report.partial.append(item)

    # -- did it actually land

    def _check(self):
        """Every file that was sent, against a listing taken now.

        A 226 from an FTP server is not evidence. A console that ran out of
        room, or that dropped the connection at the last block, answers 226
        just the same. The only thing that settles it is asking the console
        what is in the folder afterwards.
        """
        landed = [item for item in self.items
                  if item.status in (DONE, ALREADY)]
        if not landed:
            return
        self._emit(Progress(stage="checking",
                            overall_done=self._overall_done,
                            overall_total=self._overall_total))
        folders = []
        for item in landed:
            if item.destination not in folders:
                folders.append(item.destination)
        listings = {}
        try:
            for folder in folders:
                listings[folder] = _listing(self.writer, folder)
        except TransferError as exc:
            self._report.check_reason = str(exc)
            return
        self._report.checked = True
        for item in landed:
            found = listings.get(item.destination, {}).get(item.name.lower())
            if found is None:
                item.status = FAILED
                item.detail = ("The console does not have this file, even "
                               "though it was sent. Nothing arrived.")
                self._report.missing.append(item)
            elif found != item.size:
                item.status = FAILED
                item.detail = (
                    f"Only {parsers.human_size(found)} of "
                    f"{parsers.human_size(item.size)} is on the console. It "
                    f"is not complete and will not play.")
                self._report.missing.append(item)
        for item in self._report.missing:
            if item in self._report.sent:
                self._report.sent.remove(item)
            if item in self._report.already:
                self._report.already.remove(item)

    def _emit(self, progress):
        if self._on_progress is not None:
            self._on_progress(progress)


def plan(items, listings, report=None):
    """Decide, per item, whether to resume, skip, or take a different name.

    The rule is the size of what is already on the console under that name:

      the same size   it is already there, and sending it again would cost
                      hours to produce the file that is there now
      smaller         the remains of a run that stopped; carry on from there
      larger          it is not this file at all, so this one needs a name of
                      its own rather than being written over something

    The middle case is the one that matters, and it is why nothing here writes
    a marker file alongside the image: a partial is recognised by being short,
    which survives this program being closed, upgraded or run from another
    computer.
    """
    report = report if report is not None else Report()
    for item in items:
        existing = listings.get(item.destination, {})
        found = existing.get(item.name.lower())
        if found is None:
            continue
        if found == item.size:
            if not (item.wanted and item.overwrite):
                item.status = ALREADY
                item.resume_from = item.size
                item.detail = ("This is already on the console. It was not "
                               "sent again.")
                continue
            # Ticked anyway: the user has asked for it to be copied over the
            # top, so it goes from the beginning rather than being resumed
            # from its own full length, which would send nothing at all.
            item.resume_from = 0
            item.detail = ("It was already on the console and has been copied "
                           "over again.")
            continue
        if found < item.size:
            item.status = RESUMING
            item.resume_from = found
            item.detail = (
                f"{parsers.human_size(found)} of this is already on the "
                f"console from a run that did not finish. It carries on from "
                f"there rather than starting again.")
            continue
        was = item.name
        taken = set(existing) | {other.name.lower() for other in items
                                 if other is not item}
        item.name = _free_name(item.name, taken)
        item.detail = (f"The console already has a different file called "
                       f"{was}, so this one is being called {item.name}.")
        report.renamed.append((was, item.name))
    return report
