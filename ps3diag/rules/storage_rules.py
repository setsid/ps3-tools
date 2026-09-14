"""Rules about the drives themselves.

Every one of these starts by asking whether storage was actually collected.
"No USB drive is mounted" and "we never got to look" lead to opposite advice,
and the second one is the common case on a console where FTP was switched off.
"""

from ..findings import Finding
from .engine import rule
from .inference import (describe_device, device_records, files_by_device,
                        folder_format_games, has_split_sets, human_bytes,
                        is_internal, is_usb, oversize_files, prepntfs_evidence,
                        size_of, split_part, title_rows)

# Mounting a PS2 game copies it to the internal drive first, so the internal
# drive needs room for the largest one plus a little. A gigabyte of headroom is
# a guess, chosen to be smaller than any game and larger than the odds and ends
# a mount leaves behind.
PS2_HEADROOM_BYTES = 1024 ** 3

# Used when no PS2 game on the drive recorded a size. A dual layer DVD is
# 8.5 GB, so this is that plus the same headroom, rounded up.
PS2_FALLBACK_NEED_BYTES = 10 * 1024 ** 3


@rule("usb-not-mounted", category="storage")
def usb_not_mounted(artefact_set):
    """A USB drive the console names but cannot read anything from."""
    if not artefact_set.collected("storage"):
        return None
    devices = device_records(artefact_set)
    if not _storage_is_informative(devices):
        return None

    findings = []
    for device in devices:
        name = device.get("device")
        if not is_usb(name) or _looks_mounted(device):
            continue
        evidence = [f"{name}: no free space reported and nothing listed on it"]
        if device.get("note"):
            evidence.append(f"{name}: {device['note']}")
        if not device.get("present_over_ftp"):
            evidence.append(f"{name}: the console named it but its contents "
                            f"were not there to read")
        findings.append(Finding(
            rule_id="usb-not-mounted",
            severity="error",
            title="A USB drive is plugged in but the console cannot read it",
            explanation=(
                f"The console knows a drive is there but cannot get anything "
                f"off it, so any games on it will not appear. This is nearly "
                f"always down to how the drive was formatted rather than "
                f"anything wrong with the console."),
            fix=("Plug the drive into a computer and format it again as FAT32 "
                 "with 32K clusters, choosing MBR rather than GPT when asked "
                 "about the partition style. Copy the games back on "
                 "afterwards, as formatting erases the drive."),
            evidence=evidence,
            category="storage"))
    return findings


def _looks_mounted(device):
    """Anything at all came back about this drive's contents or capacity."""
    if device.get("free_bytes") is not None:
        return True
    if device.get("total_bytes") is not None:
        return True
    if device.get("folders"):
        return True
    return bool(device.get("top_level_entries"))


def _storage_is_informative(devices):
    """True when at least one drive reported something.

    Guards the rule above against a run where nothing could be read from any
    drive: that is a collection failure and saying every USB drive is broken
    would be exactly the mistake the schema warns about.
    """
    return any(_looks_mounted(device) for device in devices)


@rule("folder-games-on-ntfs", category="games")
def folder_games_on_ntfs(artefact_set):
    """Games saved as folders on an external drive that is not FAT32.

    webMAN does not report the format of a drive, so this fires only when
    something else in the set rules FAT32 out, and the finding says what that
    something was.
    """
    if not artefact_set.collected("games"):
        return None
    games = [entry for entry in folder_format_games(artefact_set)
             if not is_internal(entry.get("device"))]
    if not games:
        return None

    by_device = files_by_device(artefact_set)
    prepntfs = prepntfs_evidence(artefact_set)
    findings = []
    # Kept in inventory order rather than sorted: a device name can come
    # through as None, and sorting a set holding one against strings throws.
    seen = []
    for entry in games:
        if entry.get("device") not in seen:
            seen.append(entry.get("device"))
    for device in seen:
        here = [entry for entry in games if entry.get("device") == device]
        reason, proof = _not_fat32_reason(by_device.get(device, []), prepntfs)
        if not reason:
            continue
        these = "this game" if len(here) == 1 else f"these {len(here)} games"
        subject = "it is" if len(here) == 1 else "they are"
        findings.append(Finding(
            rule_id="folder-games-on-ntfs",
            severity="warn",
            title=("A game saved as a folder is on a drive that probably "
                   "cannot run it" if len(here) == 1 else
                   "Games saved as folders are on a drive that probably "
                   "cannot run them"),
            explanation=(
                f"A game saved as a folder full of files, rather than as one "
                f".iso file, only plays from the console's own drive or "
                f"from a USB drive formatted as FAT32. The console does not "
                f"report how {describe_device(device)} is formatted, but "
                f"{reason}, so {subject} unlikely to start."),
            fix=(f"Copy {these} onto the console's internal drive, or onto a "
                 f"USB drive formatted as FAT32 with 32K clusters. Games "
                 f"saved as a single .iso file can stay where they are."),
            evidence=[proof] + [f"{item.get('folder_key')}/"
                                f"{item.get('name')}"
                                for item in here[:4]],
            category="games"))
    return findings


def _not_fat32_reason(entries, prepntfs):
    """Why we think a drive is not FAT32, in words, or (None, None)."""
    big = oversize_files(entries)
    if big:
        name = big[0].get("name")
        size = big[0].get("size_human") or human_bytes(size_of(big[0]))
        return (f"it holds a file of {size}, which is more than FAT32 allows, "
                f"so it must be formatted as something else",
                f"{name} is {size}, above the 4 GB FAT32 limit")
    if prepntfs:
        return ("the prepNTFS plug-in is switched on, and that is only needed "
                "for drives formatted as NTFS",
                f"prepNTFS is loaded at start-up: {prepntfs}")
    return None, None


@rule("file-too-big-for-fat32", category="games")
def file_too_big_for_fat32(artefact_set):
    """A file over 4 GB on a drive that everything else says is FAT32."""
    if not artefact_set.collected("games"):
        return None
    findings = []
    grouped = files_by_device(artefact_set)
    for device in sorted(grouped, key=str):
        entries = grouped[device]
        if not has_split_sets(entries):
            continue  # nothing here suggests the drive is FAT32
        big = oversize_files(entries)
        if not big:
            continue
        worst = big[0]
        size = worst.get("size_human") or human_bytes(size_of(worst))
        findings.append(Finding(
            rule_id="file-too-big-for-fat32",
            severity="warn",
            title="A game file is bigger than the drive should be able to "
                  "hold",
            explanation=(
                f"Other games on {describe_device(device)} have been split "
                f"into numbered parts, which people only do for drives "
                f"formatted as FAT32, and a FAT32 drive cannot hold a file "
                f"over 4 GB. '{worst.get('name')}' is {size}, so either the "
                f"drive is not FAT32 after all and the splitting was never "
                f"needed, or that file did not copy across in one piece."),
            fix=(f"Try starting {worst.get('name')} first. If it does not "
                 f"start, copy it onto the drive again from your original. If "
                 f"it does start, the drive is not FAT32 and the split games "
                 f"on it can be joined back into single files."),
            evidence=[f"{worst.get('folder_key')}/{worst.get('name')} "
                      f"is {size}",
                      f"split parts also on {device}: "
                      f"{_first_split_name(entries)}"],
            category="games"))
    return findings


def _number(value):
    """A count of bytes, or None when the fact is not one.

    Sizes arrive from a parser rather than from the console, so a rule that
    compares them without checking would fall over on the day a parser hands
    one back as a string.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _first_split_name(entries):
    for entry in entries:
        if split_part(entry.get("name")):
            return entry.get("name")
    return ""


@rule("internal-space-low-for-ps2", category="storage")
def internal_space_low_for_ps2(artefact_set):
    """PS2 games on a USB drive, not enough room on the console to copy one."""
    if not (artefact_set.collected("storage")
            and artefact_set.collected("games")):
        return None

    internal = None
    for device in device_records(artefact_set):
        if device.get("device") == "dev_hdd0":
            internal = device
            break
    free = _number(internal.get("free_bytes")) if internal else None
    if free is None:
        return None

    external = [entry for entry in title_rows(artefact_set)
                if str(entry.get("folder") or "").upper() == "PS2ISO"
                and entry.get("kind") == "file"
                and not is_internal(entry.get("device"))]
    if not external:
        return None

    sizes = [size for size in (size_of(entry) for entry in external)
             if size]
    if sizes:
        biggest = max(sizes)
        needed = biggest + PS2_HEADROOM_BYTES
    else:
        biggest = None
        needed = PS2_FALLBACK_NEED_BYTES

    if free >= needed:
        return None

    largest = max(external, key=lambda entry: size_of(entry) or 0)
    return Finding(
        rule_id="internal-space-low-for-ps2",
        severity="warn",
        title="Not enough room on the console to start a PS2 game",
        explanation=(
            f"A PS2 game kept on a USB drive is copied onto the console's own "
            f"drive before it will play. That needs about "
            f"{human_bytes(needed)} free and the console has "
            f"{internal.get('free') or human_bytes(free)}, so the game will "
            f"stop partway through starting."),
        fix=(f"Delete games, videos or old downloads from the console's "
             f"internal drive until at least {human_bytes(needed)} is free, "
             f"then start the PS2 game again."),
        evidence=[f"dev_hdd0 free: "
                  f"{internal.get('free') or human_bytes(free)}",
                  f"largest PS2 game on a USB drive: "
                  f"{largest.get('name')} "
                  f"({largest.get('size_human') or human_bytes(biggest)})"],
        category="storage")
