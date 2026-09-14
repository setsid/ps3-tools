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
"""

import datetime
import hashlib
import os

from .ftpwrite import TransferFailed, sha1_of_file

FOLDER_NAME = "PS3 Tools backups"


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

    def __init__(self, folder, title_id):
        self.folder = folder
        self.title_id = title_id
        self.entries = []

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


def make(writer, usrdir, names, folder, progress=None, reread=True):
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

    backup = Backup(folder, "")
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
