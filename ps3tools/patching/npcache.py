"""np_cache.dat: whose it is, and getting a readable copy where the game
can reach it.

Why any of this is needed. The Black Ops 1 fix has the game read its own
account ID out of np_cache.dat. That file lives in the signed in user's home
folder and is mode rw-------, owned by a user the game is not, so the game
cannot open it. The fix therefore reads a copy, and the right place for the
copy is the game's own folder: a file written over FTP lands as rwxrwxrwx, so
a copy in /dev_hdd0/game/<TITLE ID>/USRDIR is readable, it sits with the game
it belongs to, and it goes when the game goes.

Two things follow from that and both are the caller's to act on.

The user folder is not always 00000001. A console that has had more than one
account on it has more than one numbered folder under /dev_hdd0/home, and
which of them is the signed in one is not something this module will guess.
Where there is one candidate it is the answer; where there is more than one
the caller is handed all of them, with the name each carries, and has to put
the question to somebody who knows.

The copy is a snapshot. It is right for the account that was signed in when it
was taken and wrong for any other, so the whole of it is re-read and replaced
every time the fix is applied or looked at again, and the screen says plainly
that the fix belongs to one account.

Nothing here writes anything on its own. read_for() and users() read; place()
writes one file and only when it is called.
"""

import os
import re
import struct

from ps3diag import patchstate
from ps3diag.parsers import parse_ftp_list


def _by_name(text):
    """A directory listing as {name: entry}, the way the flow reads one."""
    entries, _unparsed = parse_ftp_list(text)
    return {entry["name"]: entry for entry in entries}

#: Where the console keeps one folder per local user.
HOME = "/dev_hdd0/home"

#: The file itself, inside a user's folder.
NAME = "np_cache.dat"

#: The name the console shows for a local user, beside np_cache.dat in the
#: same folder. Read so that a person choosing between two numbered folders is
#: choosing between two names instead.
USERNAME = "localusername"

#: A user folder is eight digits and nothing else. Anything else under
#: /dev_hdd0/home is not one and is passed over rather than guessed about.
FOLDER = re.compile(r"^\d{8}$")

#: The account ID is the first eight bytes, big endian.
ACCOUNT_ID_BYTES = 8

#: The online ID sits straight after it, as an SceNpOnlineId: sixteen bytes of
#: name, a terminator and three of padding. Read off the real file rather than
#: taken from a header, and the layout is what that file shows: the account ID
#: at 0, the name at 8, the same name again at 28 where the next structure
#: starts, and an avatar URL further down.
ONLINE_ID_AT = 8
ONLINE_ID_BYTES = 16

#: What Sony allows in one: three to sixteen characters, letters, digits,
#: hyphen and underscore. Checked rather than trusted, because this string is
#: shown to somebody who is about to pick their account by it and a field of
#: rubbish read out of the wrong offset would be picked just as readily.
ONLINE_ID_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
ONLINE_ID_MINIMUM = 3

#: Months as an FTP listing spells them, for putting the newest account first.
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec")

#: What the tool reads and copies. Small: a few hundred bytes.
MOST = 64 * 1024


class NoAccount(Exception):
    """np_cache.dat could not be found or read. The message says which."""


class User:
    """One numbered folder under /dev_hdd0/home."""

    def __init__(self, folder, name="", has_cache=False, online_id="",
                 written=None):
        self.folder = folder
        self.name = name
        self.has_cache = has_cache
        #: The PSN name out of this account's own np_cache.dat.
        self.online_id = online_id
        #: When that file was last written, as a sort key. See _written.
        self.written = written or ()

    @property
    def usable(self):
        """Whether the fix can be run for this account.

        Both halves have to be there. A folder with an np_cache.dat this
        program cannot read an online ID out of is a folder it cannot name on
        screen either, and picking an account by a number nobody recognises is
        the thing this is meant to stop.
        """
        return bool(self.has_cache and self.online_id)

    @property
    def label(self):
        """What to show somebody choosing between two of these.

        The PSN name first, because that is what a person knows themselves
        by. The folder number stays in brackets: it is what the tool reads
        from, and somebody reporting a problem needs to be able to say it.
        """
        if self.online_id:
            return f"{self.online_id} ({self.folder})"
        return self.folder

    def __repr__(self):
        return (f"User({self.folder!r}, {self.online_id!r}, "
                f"{self.has_cache})")


def _written(entry):
    """A sort key for when a listing says a file was last written.

    An FTP listing gives "Sep 06 19:51" for anything recent and "Dec 20 2024"
    for anything older, so a file with a time is newer than a file with a
    year and that is the first thing compared. Within the recent group only
    the month and day and time are available, which inverts across a new
    year: a file from last December looks later in the year than one from
    this January. That is why the newest is preselected rather than chosen.
    """
    stamp = (entry or {}).get("modified", "")
    parts = stamp.split()
    if len(parts) != 3:
        return ()
    month = MONTHS.index(parts[0][:3].lower()) + 1 \
        if parts[0][:3].lower() in MONTHS else 0
    try:
        day = int(parts[1])
    except ValueError:
        return ()
    if ":" in parts[2]:
        hour, _, minute = parts[2].partition(":")
        try:
            return (1, month, day, int(hour), int(minute))
        except ValueError:
            return ()
    try:
        return (0, int(parts[2]), month, day, 0)
    except ValueError:
        return ()


def _read_text(lister, path):
    try:
        raw = lister.retrieve_bytes(path)
    except Exception:                                       # noqa: BLE001
        return ""
    return raw.split(b"\x00")[0].decode("utf-8", "replace").strip()


def users(lister):
    """Every local user on the console, in folder order.

    A folder with no np_cache.dat in it is still returned, marked as having
    none. Leaving it out would take a console whose second account has never
    been online and make it look as though that account did not exist, and
    "this account has never signed in to PSN" is a thing the user needs told
    rather than hidden from.
    """
    try:
        listing = lister.list_dir(HOME)
    except Exception as exc:                                # noqa: BLE001
        raise NoAccount(
            f"{HOME} could not be listed ({exc.__class__.__name__}: {exc}). "
            f"Check the console is switched on and that webMAN is running.")
    found = []
    for name in sorted(_by_name(listing)):
        if not FOLDER.match(name):
            continue
        path = f"{HOME}/{name}"
        try:
            inner = _by_name(lister.list_dir(path))
        except Exception:                                   # noqa: BLE001
            inner = {}
        entry = inner.get(NAME)
        # The file is a few hundred bytes and is read here rather than later,
        # because the name inside it is what this account is called on screen
        # and a list of numbers is a list nobody can choose from.
        raw = b""
        if entry is not None:
            try:
                raw = lister.retrieve_bytes(f"{path}/{NAME}")
            except Exception:                               # noqa: BLE001
                raw = b""
        found.append(User(name, _read_text(lister, f"{path}/{USERNAME}"),
                          entry is not None, online_id(raw),
                          _written(entry)))
    # Newest first, so the account signed in now is the one at the top. The
    # console writes np_cache.dat when an account signs in to PSN, so the most
    # recently written one is the one being used.
    found.sort(key=lambda person: person.written, reverse=True)
    return found


def with_cache(people):
    """The users the fix can actually be run for, newest first.

    Both an np_cache.dat and a readable name out of it. An account that has
    never signed in to PSN has no such file, because it is written the first
    time, so its absence is ordinary and is not a fault.
    """
    return [person for person in people if person.usable]


def path_for(folder):
    return f"{HOME}/{folder}/{NAME}"


def read_for(lister, folder):
    """np_cache.dat for one user, as bytes. Raises NoAccount if it is not
    readable, which over FTP it is, whatever its mode says on the console."""
    path = path_for(folder)
    try:
        raw = lister.retrieve_bytes(path)
    except Exception as exc:                                # noqa: BLE001
        raise NoAccount(
            f"{path} could not be read ({exc.__class__.__name__}: {exc}).")
    if len(raw) < ACCOUNT_ID_BYTES:
        raise NoAccount(
            f"{path} is {len(raw)} bytes and the account ID is the first "
            f"{ACCOUNT_ID_BYTES}, so there is nothing in it to read.")
    return raw[:MOST]


def account_id(raw):
    """The account ID out of np_cache.dat, as the number the server knows.

    The same number the game will format with "%llu" once it is patched, so
    this is what a screen shows somebody who wants to check the fix is about
    to use the right account.
    """
    if len(raw) < ACCOUNT_ID_BYTES:
        raise NoAccount("there are not eight bytes of account ID here")
    value = struct.unpack(">Q", raw[:ACCOUNT_ID_BYTES])[0]
    if not value:
        raise NoAccount(
            "the account ID in np_cache.dat is zero, which means this account "
            "has not finished signing in to PSN. Sign in once and run the fix "
            "again.")
    return value


def online_id(raw):
    """The online ID np_cache.dat carries, or "" -- for showing, not for use.

    The whole point of the fix is that this is the wrong thing to hash. It is
    read so that a screen can say which account it is about to tie the fix to
    in words somebody recognises.
    """
    field = raw[ONLINE_ID_AT:ONLINE_ID_AT + ONLINE_ID_BYTES]
    text = field.split(b"\x00")[0].decode("ascii", "ignore").strip()
    if len(text) < ONLINE_ID_MINIMUM:
        return ""
    return text if set(text) <= ONLINE_ID_ALLOWED else ""


def destination(content_id):
    """Where the readable copy goes, asked of the fix that will read it.

    The path is not built here. It is built by the same function the code
    cave's path is built by, so the file the tool writes and the file the game
    opens are the same string by construction. Two templates that happen to
    agree today are two templates that can stop agreeing, and the way that
    shows is a patch that applies cleanly and does nothing.
    """
    module = patchstate.patcher_module("bo1")
    if module is None:
        raise NoAccount(
            "the Black Ops 1 fix is not part of this build of the program, "
            "so there is nowhere to put the copy. This is a fault in this "
            "program and nothing is wrong with your console.")
    return module.np_cache_path(module.title_id_from_content_id(content_id))


def stage(raw, workdir, name=NAME):
    """Write the bytes to a local file for the uploader. Returns the path."""
    path = os.path.join(workdir, name)
    with open(path, "wb") as handle:
        handle.write(raw)
    return path


def place(writer, content_id, raw, workdir):
    """(remote path, local path) for the copy, staged and ready to send.

    The writing itself is the flow's job: it uploads this the same way it
    uploads a patched binary, reads it back and compares, because a supporting
    file that did not arrive whole is a fix that silently does nothing.
    """
    return destination(content_id), stage(raw, workdir)
