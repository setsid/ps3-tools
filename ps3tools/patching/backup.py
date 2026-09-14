"""Pull the originals off the console and prove the copy is good.

The rule the rest of the flow is built on: no backup, no patch. The files this
program replaces cannot be rebuilt from the patched copies and cannot be
downloaded from anywhere, so a user who loses them reinstalls the game and the
title update. A backup that failed quietly is worse than no backup at all,
which is why every copy is checked twice rather than trusted because the
transfer said 226.

Verification is size and hash, both against the console rather than against
ourselves:

  size  the console's own SIZE reply against the bytes that arrived and
        against the file on disk afterwards
  hash  the sha1 of the stream, the sha1 of the file re-read from disk, and
        the sha1 of a second read of the file on the console

The third of those costs a second download of every file. It is the only way to
compare a local copy against a remote one honestly, and this is the copy that
has to be right, so it is on by default and the caller has to ask for it to be
skipped.

What was checked is then written down, in a manifest beside the files, because
none of the above is worth anything at the moment it is needed unless it can be
asked again. A backup is used months later by somebody whose game has stopped
starting, off a folder that has since been copied, moved or edited, and the
only honest answer to "is this still the original?" is a hash recorded when the
copy was known to be good. A backup that cannot be verified is not a backup,
and this module refuses to put one back.
"""

import datetime
import hashlib
import json
import os

from .ftpwrite import TransferFailed, sha1_of_file

FOLDER_NAME = "PS3 Tools backups"

# The record of what was taken, written into the backup folder itself.
#
# Without it a backup folder is a pile of files nobody can check: the sizes and
# hashes were known at the moment the copy was made and verified against the
# console, and if they are not written down then that knowledge is thrown away
# the moment the program closes. A backup that cannot be verified is not a
# backup, because the one thing it has to answer -- "is this still exactly what
# came off the console?" -- can no longer be asked.
#
# It sits beside the files rather than in an application data folder so that a
# folder the user has copied to a memory stick is still a complete backup.
MANIFEST_NAME = "backup-contents.json"
MANIFEST_FORMAT = 1


class BackupFailed(Exception):
    """The backup is not trustworthy. Nothing is patched after one of these."""


def desktop():
    """The user's Desktop, or their home folder if there is not one.

    Deliberately somewhere the user will trip over rather than an application
    data folder they will never find. The one thing they must be able to do
    afterwards is put these files back.
    """
    home = os.path.expanduser("~")
    candidate = os.path.join(home, "Desktop")
    return candidate if os.path.isdir(candidate) else home


def folder_for(title_id, root=None, when=None):
    """Desktop/PS3 Tools backups/BLES01717 2026-09-14, made unique if taken."""
    when = when or datetime.datetime.now()
    base = os.path.join(root or desktop(), FOLDER_NAME,
                        f"{title_id.upper()} {when:%Y-%m-%d}")
    candidate = base
    index = 2
    while os.path.exists(candidate):
        candidate = f"{base} ({index})"
        index += 1
    return candidate


class Backup:
    """Where the originals went and what they were."""

    def __init__(self, folder, title_id, usrdir="", taken=None, problem=""):
        self.folder = folder
        self.title_id = (title_id or "").upper()
        self.usrdir = usrdir
        #: when the copy was taken, as a datetime, or None if that is not known
        self.taken = taken
        #: why this folder could not be read as a backup, in words fit to show
        self.problem = problem
        self.entries = []

    @property
    def label(self):
        """What to call this backup in a list the user is choosing from."""
        name = os.path.basename(self.folder.rstrip(os.sep)) or self.folder
        return f"{name} ({self.taken_text})" if self.taken else name

    @property
    def taken_text(self):
        if not self.taken:
            return "date not recorded"
        return self.taken.strftime("%d %B %Y at %H:%M")

    def __len__(self):
        return len(self.entries)

    @property
    def names(self):
        return [entry["name"] for entry in self.entries]

    def entry_for(self, name):
        for entry in self.entries:
            if entry["name"] == name:
                return entry
        return None


def make(writer, usrdir, names, folder, progress=None, reread=True,
         title_id=None, when=None):
    """Copy each name out of usrdir into folder. Raises rather than half works.

    A folder holding a partial backup is left behind on purpose: it is evidence
    for whoever has to work out what happened, and nothing has been written to
    the console at the point this can fail.
    """
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise BackupFailed(
            f"The backup folder could not be created at {folder} ({exc}). "
            f"Nothing has been changed on the console. Check there is room on "
            f"the disk and that the Desktop is writable.")

    backup = Backup(folder, title_id or _title_id_from(usrdir), usrdir,
                    when or datetime.datetime.now())
    for index, name in enumerate(names):
        remote = f"{usrdir}/{name}"
        local = os.path.join(folder, name)
        if progress:
            progress({"stage": "backup", "file": name, "done": index,
                      "total": len(names),
                      "message": f"copying {name} to the backup folder"})
        try:
            pulled = writer.retrieve(
                remote, local,
                on_block=(lambda sent, total, _name=name, _i=index:
                          progress and progress(
                              {"stage": "backup", "file": _name, "done": _i,
                               "total": len(names), "bytes": sent,
                               "of": total,
                               "message": f"copying {_name}"})))
        except TransferFailed as exc:
            raise BackupFailed(f"{name} could not be copied off the console. "
                               f"{exc} Nothing has been changed.")
        except OSError as exc:
            raise BackupFailed(
                f"{name} could not be written to {folder} ({exc}). Nothing "
                f"has been changed on the console.")
        except Exception as exc:
            raise BackupFailed(
                f"{name} could not be copied off the console "
                f"({exc.__class__.__name__}: {exc}). Nothing has been "
                f"changed.")

        try:
            on_disk = os.path.getsize(local)
            disk_hash = sha1_of_file(local)
        except OSError as exc:
            raise BackupFailed(
                f"The copy of {name} could not be read back from {folder} "
                f"({exc}). Nothing has been changed on the console.")
        if on_disk != pulled["bytes"] or disk_hash != pulled["sha1"]:
            raise BackupFailed(
                f"The copy of {name} on this computer does not match what "
                f"came off the console ({pulled['bytes']} bytes in, "
                f"{on_disk} bytes on disk). The disk may be full. Nothing "
                f"has been changed on the console.")

        if reread:
            if progress:
                progress({"stage": "backup", "file": name, "done": index,
                          "total": len(names),
                          "message": f"checking the backup of {name}"})
            try:
                again = writer.retrieve_bytes(remote)
            except Exception as exc:
                raise BackupFailed(
                    f"The backup of {name} could not be checked against the "
                    f"console ({exc.__class__.__name__}: {exc}). Nothing has "
                    f"been changed.")
            if hashlib.sha1(again).hexdigest() != disk_hash:
                raise BackupFailed(
                    f"The backup of {name} does not match the file on the "
                    f"console when it is read a second time, so the copy "
                    f"cannot be trusted. Nothing has been changed.")

        backup.entries.append({"name": name, "path": local, "remote": remote,
                               "size": on_disk, "sha1": disk_hash})

    # Written last, so a manifest on disk means every file beside it was copied
    # and checked. A half-written backup has no manifest and is refused later
    # rather than half trusted.
    write_manifest(backup)
    if progress:
        progress({"stage": "backup", "file": "", "done": len(names),
                  "total": len(names),
                  "message": f"backup complete in {folder}"})
    return backup


def restore(writer, backup, names=None, progress=None):
    """Push backed-up files back over the top. Returns (restored, failed).

    Never raises. It is called when something has already gone wrong, and a
    second exception on the way out would replace the reason with its own.
    """
    wanted = names if names is not None else backup.names
    restored = []
    failed = []
    for index, name in enumerate(wanted):
        entry = backup.entry_for(name)
        if entry is None:
            failed.append((name, "it is not in the backup"))
            continue
        if progress:
            progress({"stage": "restore", "file": name, "done": index,
                      "total": len(wanted),
                      "message": f"putting the original {name} back"})
        try:
            writer.store(entry["path"], entry["remote"])
            landed = writer.retrieve_bytes(entry["remote"])
        except Exception as exc:
            failed.append((name, f"{exc.__class__.__name__}: {exc}"))
            continue
        if hashlib.sha1(landed).hexdigest() != entry["sha1"]:
            failed.append((name, "what landed does not match the backup"))
            continue
        restored.append(name)
    return restored, failed


# --- the manifest ----------------------------------------------------------
#
# Everything below here exists so that a backup folder can be read back months
# later by a user who has forgotten what is in it, and so that "put it back"
# can be refused rather than attempted when the answer is not certain.


def _title_id_from(usrdir):
    """BLES01717 out of /dev_hdd0/game/BLES01717/USRDIR, or "" if it is not there."""
    parts = [part for part in (usrdir or "").replace("\\", "/").split("/")
             if part]
    for part in reversed(parts):
        if part.upper() != "USRDIR":
            return part.upper()
    return ""


def manifest_path(folder):
    return os.path.join(folder, MANIFEST_NAME)


def write_manifest(backup):
    """Record what this folder holds. Never raises: the files are the backup.

    A manifest that could not be written leaves a backup that cannot be
    verified afterwards, which is a real loss, but it is not a reason to fail a
    run whose files are already copied and already checked against the console.
    The refusal happens later, when a restore is asked for and there is nothing
    to check the files against.
    """
    record = {
        "format": MANIFEST_FORMAT,
        "title_id": backup.title_id,
        "usrdir": backup.usrdir,
        "taken": (backup.taken or datetime.datetime.now()).isoformat(
            timespec="seconds"),
        "files": [{"name": entry["name"], "remote": entry["remote"],
                   "size": entry["size"], "sha1": entry["sha1"]}
                  for entry in backup.entries],
    }
    try:
        with open(manifest_path(backup.folder), "w", encoding="utf-8") as out:
            json.dump(record, out, indent=2)
            out.write("\n")
    except OSError:
        return False
    return True


def _taken_from(text, folder):
    """The recorded date, falling back to when the folder was last written."""
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(folder))
    except OSError:
        return None


def read_manifest(folder):
    """The backup in this folder, or a Backup carrying the reason it is not one.

    Always returns a Backup. A folder that cannot be read as a backup still has
    to be shown to the user with its name on it, because it is sitting on their
    Desktop looking like a backup and silence about it is worse than a
    sentence saying why it cannot be used.
    """
    path = manifest_path(folder)
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return Backup(
            folder, "", taken=_taken_from(None, folder),
            problem=("This folder has no record of what was copied into it, "
                     "so there is no way to check that the files in it are "
                     "still exactly what came off the console. It was made by "
                     "an older version of this program, or the file listing "
                     "its contents has been deleted."))
    except (OSError, ValueError) as exc:
        return Backup(
            folder, "", taken=_taken_from(None, folder),
            problem=(f"The record of what this folder holds could not be read "
                     f"({exc.__class__.__name__}), so the files in it cannot "
                     f"be checked."))
    if not isinstance(record, dict):
        return Backup(folder, "", taken=_taken_from(None, folder),
                      problem="The record of what this folder holds is not in "
                              "a form this program understands.")

    backup = Backup(folder, record.get("title_id") or "",
                    record.get("usrdir") or "",
                    _taken_from(record.get("taken"), folder))
    for item in record.get("files") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        backup.entries.append({
            "name": name,
            "path": os.path.join(folder, name),
            "remote": item.get("remote") or f"{backup.usrdir}/{name}",
            "size": item.get("size"),
            "sha1": (item.get("sha1") or "").lower(),
        })
    if not backup.entries:
        backup.problem = ("The record of what this folder holds does not name "
                          "any files, so there is nothing here to put back.")
    return backup


def root_folder(root=None):
    """Desktop/PS3 Tools backups, whether or not it exists yet."""
    return os.path.join(root or desktop(), FOLDER_NAME)


def find(title_id=None, root=None):
    """Every backup on the Desktop, newest first, optionally for one title.

    Matching is on what the manifest recorded rather than on the folder name.
    A user who renames a folder has not changed which console files are inside
    it, and the name is the one part of a backup nothing verified.
    """
    base = root_folder(root)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []

    wanted = "".join(character for character in (title_id or "").upper()
                     if character.isalnum())
    found = []
    for name in names:
        folder = os.path.join(base, name)
        if not os.path.isdir(folder):
            continue
        backup = read_manifest(folder)
        if wanted:
            # Either the record inside or the name on the outside. A folder
            # with no manifest has only its name, and a folder whose name and
            # record disagree is exactly the one a user needs to be shown
            # rather than quietly hidden: it is offered, and then refused at
            # the point of restoring, where the reason can be given in full.
            if wanted not in {backup.title_id, name.split(" ")[0].upper()}:
                continue
        found.append(backup)
    found.sort(key=lambda item: (item.taken or datetime.datetime.min),
               reverse=True)
    return found


def verify(backup):
    """Each file against what was recorded. [{name, ok, reason, ...}].

    Reads every byte back off the disk. A backup is checked at the moment it is
    used and not before: the interesting failures are a file edited, truncated
    or half copied off a memory stick some time after it was made.
    """
    results = []
    for entry in backup.entries:
        row = {"name": entry["name"], "path": entry["path"], "ok": False,
               "reason": "", "size": None, "modified": None,
               "recorded_size": entry.get("size"),
               "recorded_sha1": entry.get("sha1") or ""}
        try:
            row["size"] = os.path.getsize(entry["path"])
            row["modified"] = datetime.datetime.fromtimestamp(
                os.path.getmtime(entry["path"]))
        except OSError:
            row["reason"] = "it is not in the backup folder any more"
            results.append(row)
            continue
        if (entry.get("size") is not None
                and row["size"] != entry["size"]):
            row["reason"] = (f"it is {row['size']} bytes and it was "
                             f"{entry['size']} bytes when it was copied")
            results.append(row)
            continue
        try:
            digest = sha1_of_file(entry["path"])
        except OSError as exc:
            row["reason"] = f"it could not be read ({exc.strerror or exc})"
            results.append(row)
            continue
        row["sha1"] = digest
        if not entry.get("sha1"):
            row["reason"] = ("there is no record of what this file should "
                             "look like, so it cannot be checked")
        elif digest != entry["sha1"]:
            row["reason"] = ("it is not the same file that was copied off the "
                             "console; something has changed it since")
        else:
            row["ok"] = True
        results.append(row)
    return results


def all_good(results):
    """True only when there is something here and every part of it checked out."""
    return bool(results) and all(row["ok"] for row in results)
