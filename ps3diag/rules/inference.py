"""Shared reading of the set, and the guesses the rules cannot avoid making.

webMAN does not report how a drive is formatted, and the schema says so
outright. Two of the rules need to know FAT32 from NTFS anyway, so they work
from side effects instead:

  * a single file of 4 GB or more cannot sit on a FAT32 drive, so its presence
    rules FAT32 out;
  * a game split into numbered parts is a thing people only do to get it onto
    a FAT32 drive, so its presence points at FAT32;
  * prepNTFS exists solely to make NTFS drives usable by webMAN, so somebody
    running it has at least one NTFS drive.

None of the three is proof. Each helper hands back the reason it reached its
answer so that the finding can say what it was reading, rather than announcing
a filesystem it cannot actually see.
"""

import re

# A FAT32 file is at most 4 GiB minus one byte, so exactly 4 GiB is already
# impossible. Nothing here needs the minus one, only the boundary.
FAT32_MAX_FILE_BYTES = 4 * 1024 ** 3

# The folder names webMAN scans for. Matched case-insensitively, and only ever
# against a whole name: a folder called "PS3ISO backup" is not one of these.
GAME_FOLDER_NAMES = ("PS3ISO", "PS2ISO", "PSXISO", "PSPISO", "BDISO", "DVDISO",
                     "GAMES", "PKG")

SPLIT_PART = re.compile(r"(?i)^(?P<base>.+\.iso)\.(?P<index>\d{1,3})$")
ISO_NAME = re.compile(r"(?i)\.iso(\.\d{1,3})?$")


def device_records(artefact_set):
    """The storage device records, or [] when they are not the shape they
    should be.

    Facts come out of parsers that may be older or newer than these rules, so a
    rule that meets a list where it expected a dict falls silent rather than
    being reported as broken. A broken rule is read by the helper as a fault in
    ps3-diag, which this is not.
    """
    return _rows(_safely(artefact_set.devices))


def title_rows(artefact_set):
    """Every game entry, each carrying its device and folder."""
    return _rows(_safely(artefact_set.game_entries))


def startup_plugins(artefact_set):
    """The boot_plugins.txt entries."""
    return _rows(_safely(artefact_set.boot_plugins))


def _safely(reader):
    try:
        return reader()
    except (AttributeError, TypeError, ValueError):
        return []


def _rows(value):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def is_internal(device):
    """The console's own drives. dev_hdd1 is the second bay, still internal."""
    return str(device or "").lower().startswith("dev_hdd")


def is_usb(device):
    return str(device or "").lower().startswith("dev_usb")


def describe_device(device):
    """A name for somebody who has never heard of dev_hdd0."""
    name = str(device or "").lower()
    if name == "dev_hdd0":
        return "the console's own internal drive"
    if name.startswith("dev_hdd"):
        return f"the second internal drive ({device})"
    if name.startswith("dev_usb"):
        return f"the USB drive shown as {device}"
    return str(device or "an unnamed drive")


def size_of(entry):
    """An entry's size in bytes, or None when it was not recorded.

    A missing size is not zero. Rules that compare sizes skip these rather than
    treating an unknown as an empty file.
    """
    value = (entry or {}).get("size")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def human_bytes(count):
    if count is None:
        return "an unknown size"
    value = float(count)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def counted(count, singular, plural=None):
    """"one game" or "3 games". A finding that says "1 games" reads as a bug
    and costs the reader confidence in everything else on the page."""
    if count == 1:
        return f"one {singular}"
    return f"{count} {plural or singular + 's'}"


def is_are(count):
    return "is" if count == 1 else "are"


def is_iso_name(name):
    return bool(ISO_NAME.search(str(name or "")))


def split_part(name):
    """("Game.iso", 3) for "Game.iso.3", otherwise None."""
    match = SPLIT_PART.match(str(name or ""))
    if not match:
        return None
    return match.group("base"), int(match.group("index"))


def files_by_device(artefact_set):
    """Every entry that is a file, grouped by the drive it sits on."""
    grouped = {}
    for entry in title_rows(artefact_set):
        if entry.get("kind") != "file":
            continue
        grouped.setdefault(entry.get("device"), []).append(entry)
    return grouped


def folder_format_games(artefact_set):
    """Games stored as a folder of files rather than as a single .iso.

    A directory sitting directly inside GAMES. Directories inside the ISO
    folders are left alone: those are somebody's own filing, not a game.
    """
    out = []
    for entry in title_rows(artefact_set):
        if entry.get("kind") != "directory":
            continue
        if str(entry.get("folder") or "").upper() != "GAMES":
            continue
        if str(entry.get("name") or "").upper() in GAME_FOLDER_NAMES:
            continue  # a nested folder, which is a different rule's business
        out.append(entry)
    return out


def oversize_files(entries):
    """Files too big for any FAT32 drive to hold, largest first."""
    big = [entry for entry in entries
           if (size_of(entry) or 0) >= FAT32_MAX_FILE_BYTES]
    return sorted(big, key=lambda entry: size_of(entry) or 0, reverse=True)


def has_split_sets(entries):
    return any(split_part(entry.get("name")) for entry in entries)


def prepntfs_evidence(artefact_set):
    """The line from boot_plugins.txt that loads prepNTFS, or None.

    prepNTFS has one job, which is preparing NTFS drives for webMAN, so a
    console that loads it has an NTFS drive attached to it somewhere. Which
    drive, it does not say.
    """
    if not artefact_set.collected("plugins"):
        return None
    for plugin in startup_plugins(artefact_set):
        if not plugin.get("enabled"):
            continue
        if "prepntfs" in str(plugin.get("name") or "").lower():
            return str(plugin.get("path") or plugin.get("name"))
    return None
