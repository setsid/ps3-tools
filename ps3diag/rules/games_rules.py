"""Rules about the game files: where they are filed and whether they are whole.

All of these read the games inventory and nothing else, so all of them begin by
asking whether the inventory was collected. A console where FTP was refused
produces an empty inventory, and "no games found" there would be a lie.
"""

from ..findings import Finding
from .engine import rule
from .inference import (GAME_FOLDER_NAMES, counted, describe_device,
                        human_bytes, is_are, size_of, split_part, title_rows)

# A PS3 disc game smaller than this is not a game. The smallest retail PS3
# discs are a few hundred megabytes, so 100 MB is below anything real and well
# above the few kilobytes a half-finished copy leaves behind.
MIN_PLAUSIBLE_PS3_ISO_BYTES = 100 * 1024 ** 2

# Folders whose contents are meant to be disc images. GAMES and PKG are not
# here: a directory inside those is normal.
ISO_FOLDERS = ("PS3ISO", "PS2ISO", "PSXISO", "PSPISO", "BDISO", "DVDISO")


@rule("iso-folders-nested", category="games")
def iso_folders_nested(artefact_set):
    """A folder webMAN scans, sitting inside another folder webMAN scans."""
    if not artefact_set.collected("games"):
        return None
    findings = []
    for entry in title_rows(artefact_set):
        if entry.get("kind") != "directory":
            continue
        inner = str(entry.get("name") or "").upper()
        outer = str(entry.get("folder") or "").upper()
        if inner not in GAME_FOLDER_NAMES or not outer:
            continue
        findings.append(Finding(
            rule_id="iso-folders-nested",
            severity="error",
            title="One games folder is sitting inside another",
            explanation=(
                f"The {inner} folder is inside the {outer} folder on "
                f"{describe_device(entry.get('device'))}. The console only "
                f"looks in the folders at the very top of a drive, so nothing "
                f"filed inside that one will appear in your games list."),
            fix=(f"Move the {inner} folder out of {outer} so that it sits at "
                 f"the top level of the drive, alongside {outer} instead of "
                 f"inside it."),
            evidence=[f"{entry.get('folder_key')}/{entry.get('name')} "
                      f"is a folder"],
            category="games"))
    return findings


@rule("split-iso-missing-part", category="games")
def split_iso_missing_part(artefact_set):
    """A .iso.0 / .iso.1 set with a gap in the numbering."""
    if not artefact_set.collected("games"):
        return None

    sets = {}
    for entry in title_rows(artefact_set):
        if entry.get("kind") != "file":
            continue
        parsed = split_part(entry.get("name"))
        if not parsed:
            continue
        base, index = parsed
        key = (entry.get("folder_key"), base.lower())
        record = sets.setdefault(key, {"base": base, "indices": set(),
                                       "folder_key": entry.get("folder_key"),
                                       "device": entry.get("device")})
        record["indices"].add(index)

    findings = []
    for record in sets.values():
        indices = record["indices"]
        # The highest part present says how many there should be. A set that
        # stops early looks complete from here and is left alone rather than
        # guessed at.
        missing = [index for index in range(max(indices) + 1)
                   if index not in indices]
        if not missing:
            continue
        names = [f"{record['base']}.{index}" for index in missing]
        absent = ("one of them is" if len(names) == 1
                  else f"{len(names)} of them are")
        findings.append(Finding(
            rule_id="split-iso-missing-part",
            severity="error",
            title="Part of a split-up game is missing",
            explanation=(
                f"'{record['base']}' was split into numbered parts to be "
                f"copied across, and {absent} not in the folder. The game "
                f"cannot start until every part is back together in the same "
                f"place."),
            fix=(f"Copy {_join(names)} into {record['folder_key']} from "
                 f"wherever this game was copied from. All the numbered parts "
                 f"have to sit in that one folder together."),
            evidence=[f"{record['folder_key']}: parts present "
                      f"{sorted(indices)}",
                      f"missing: {_join(names)}"],
            category="games"))
    return findings


def _join(names):
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


@rule("iso-zero-bytes", category="games")
def iso_zero_bytes(artefact_set):
    """Disc images of no size at all: a copy that never really happened."""
    if not artefact_set.collected("games"):
        return None
    empty = [entry for entry in _iso_files(artefact_set)
             if size_of(entry) == 0]
    if not empty:
        return None
    count = len(empty)
    outcome = ("it will not start" if count == 1
               else "none of them will start")
    return Finding(
        rule_id="iso-zero-bytes",
        severity="error",
        title=("A game file is completely empty" if count == 1
               else "Some game files are completely empty"),
        explanation=(
            f"{counted(count, 'game file').capitalize()} on this console "
            f"{'contains' if count == 1 else 'contain'} nothing at all. That "
            f"happens when a copy was interrupted or the drive filled up "
            f"partway through, and {outcome}."),
        fix=(f"Delete {'it' if count == 1 else 'them'} and copy the "
             f"{'game' if count == 1 else 'games'} across again, letting "
             f"{'it' if count == 1 else 'each copy'} finish before "
             f"unplugging the drive."),
        evidence=[f"{entry.get('folder_key')}/{entry.get('name')}: 0 bytes"
                  for entry in empty[:5]],
        category="games")


@rule("iso-too-small", category="games")
def iso_too_small(artefact_set):
    """A PS3 disc image far too small to be a whole game."""
    if not artefact_set.collected("games"):
        return None
    suspect = []
    for entry in _iso_files(artefact_set):
        if str(entry.get("folder") or "").upper() != "PS3ISO":
            continue
        if split_part(entry.get("name")):
            continue  # the last part of a split set is legitimately small
        size = size_of(entry)
        if size is None or size == 0:
            continue  # empty files are the other rule's business
        if size < MIN_PLAUSIBLE_PS3_ISO_BYTES:
            suspect.append(entry)
    if not suspect:
        return None
    count = len(suspect)
    return Finding(
        rule_id="iso-too-small",
        severity="warn",
        title=("A game file looks far too small to be complete" if count == 1
               else "Some game files look far too small to be complete"),
        explanation=(
            f"{counted(count, 'game').capitalize()} in the PS3ISO folder "
            f"{is_are(count)} smaller than any real PS3 game, so the copy "
            f"almost certainly stopped partway through. "
            f"{'It will' if count == 1 else 'They will'} either not appear at "
            f"all or will freeze soon after starting."),
        fix=(f"Copy {'it' if count == 1 else 'them'} across again from your "
             f"original, and wait for each copy to finish before unplugging "
             f"the drive or turning the console off."),
        evidence=[f"{entry.get('folder_key')}/{entry.get('name')}: "
                  f"{entry.get('size_human') or human_bytes(size_of(entry))}"
                  for entry in suspect[:5]],
        category="games")


def _iso_files(artefact_set):
    """Files sitting in one of the folders meant to hold disc images."""
    return [entry for entry in title_rows(artefact_set)
            if entry.get("kind") == "file"
            and str(entry.get("folder") or "").upper() in ISO_FOLDERS]
