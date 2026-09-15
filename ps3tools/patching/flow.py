"""Scan and patch. One flow, and everything title-specific comes from a table.

There is no Black Ops II code path and no Modern Warfare 3 code path. There is
one sequence, and ps3tools.titles says which files it applies to, which of them
need a klicensee, where each one's patch site is and what the bytes there mean.
Adding a third title is an entry in that table plus a signing profile below.

Two rules run through all of it.

The state is read from bytes. Never from a file size, never from the sha1 of a
SELF, and never from the title update the console reports. A file whose four
bytes match neither the stock nor the patched instruction is unrecognised, and
an unrecognised file is reported and left alone. A file this program could not
read at all is not unrecognised: that is NOT_EXAMINED, and it is a fault here
rather than anything to do with the file. A file that would not decrypt is
neither of those: that is CANNOT_DECRYPT, and it means this release of the game
signs its binaries differently from the ones the fix was proved on. The
update-package hashes in titles.py say which title update is installed and
nothing else; nothing here looks at them.

Which releases get this far is a question for titles.py. Every published title
ID of either game is attempted, because a wrong klicensee cannot produce a
plausible answer: scetool either opens the file or produces nothing. Where it
produces nothing the run stops, having read and written nothing, and says which
title ID it was.

Nothing is written without a verified backup first. The originals cannot be
rebuilt from the patched copies and cannot be downloaded from anywhere.
"""

import ftplib
import hashlib
import os
import shutil
import struct
import tempfile

from ps3diag import parsers, patchstate
from ps3tools import titles

from . import backup as backups
from .scetool import (FIELD_TITLES, ScetoolError, SigningProfile,
                      title_id_from)

# What a scan can conclude about one file.
NOT_PATCHED = "not patched"
PATCHED = "patched"
UNRECOGNISED = "unrecognised"
# Kept apart from UNRECOGNISED on purpose. Unrecognised means the bytes were
# read and matched neither value, which is a statement about the user's file.
# This one means the check never ran because something this program needs was
# not there, which is a statement about this program.
NOT_EXAMINED = "not examined"
NO_SITE = "no patch site"
# The file is one of this game's, and scetool could not unlock it with the
# klicensee this game uses. Kept apart from all three of the above because it
# asks something different of everyone reading it: nothing is wrong with the
# user's console, nothing is missing from this program, and the file was not
# misread. This release simply signs its binaries differently, which is only
# discoverable by trying, and the title ID is the thing worth reporting.
CANNOT_DECRYPT = "cannot decrypt"

# The re-sign argument shapes, one per title, taken from the two repositories'
# readmes. The differences between them are not derivable from anything: each
# was arrived at by testing against real files of that title, so each is
# written down rather than worked out.
#
#   bo2  -1 TRUE -s FALSE, with -3 and -4 passed explicitly
#   mw3  -1 FALSE -s TRUE, with -t handing scetool the original as a template,
#        which is where the Auth-ID, Vendor-ID and control info come from
SIGNING = {
    "bo2": SigningProfile(compressed="TRUE", skip_sections="FALSE",
                          use_template=False, carry_ids=True),
    "mw3": SigningProfile(compressed="FALSE", skip_sections="TRUE",
                          use_template=True, carry_ids=False),
}

# Read from the header when scetool prints them, which not every build does.
# These are the values on the files each fix was verified against, and a run
# that falls back to one of them says so in its notes.
FIELD_FALLBACKS = {
    "bo2": {"auth_id": "1010000001000003", "vendor_id": "01000002",
            "app_version": "0001000000000000",
            "fw_version": "0004002000000000"},
    "mw3": {"auth_id": "1010000001000003", "vendor_id": "01000002",
            "app_version": "0001000000000000",
            "fw_version": "0004000000000000"},
}

# The five fields that must come back unchanged on a rebuilt file. CID_FN is a
# hash of the name given to -g, and a file with the wrong one is perfectly
# valid and will not load, which is the failure that looks like nothing at all.
CARRIED_FIELDS = ("key_revision", "self_type", "app_type", "licence_type",
                  "content_id", "cid_fn_hash")


class PatchFailed(Exception):
    """A stop with an explanation. Reported without a traceback."""


def cannot_decrypt_detail(title_id, name, verified, reason=""):
    """Why a file of a recognised release would not come open.

    The title ID is in the sentence rather than only in the window behind it,
    because it is the one piece of information that makes the difference
    between somebody reporting "it did not work" and somebody reporting a
    release that can then be added.
    """
    title_id = (title_id or "").upper()
    if verified:
        # This release has been done before, so the signing parameters are not
        # the suspect. Something about this particular copy is.
        return (f"{name} could not be unlocked, even though {title_id} is a "
                f"release this fix has worked on before. The copy on the "
                f"console may have been replaced or rebuilt by something "
                f"else. Nothing has been changed"
                + (f". {reason}" if reason else ""))
    return (f"{name} could not be unlocked. {title_id} is a release of this "
            f"game that signs its files differently from the ones this fix "
            f"has been proved on, so this tool cannot patch it. Nothing is "
            f"wrong with your console or your copy of the game, and nothing "
            f"has been changed. Quote {title_id} if you report this"
            + (f". {reason}" if reason else ""))


def _sha1(data):
    return hashlib.sha1(data).hexdigest()


def _is_bl(word):
    """A bl is opcode 18 with the link bit set, so it is not one constant."""
    value = struct.unpack(">I", word)[0]
    return (value >> 26) == 18 and bool(value & 1)


# --- reading the state out of a decrypted image ----------------------------

def _bytes_state(site, word):
    """What the four bytes at the patch site say, or None if neither."""
    if word == site.get("patched"):
        return PATCHED
    stock = site.get("stock")
    if stock is not None and word == stock:
        return NOT_PATCHED
    if site.get("stock_is") == "bl" and _is_bl(word):
        return NOT_PATCHED
    return None


def site_state(site, image, kind):
    """(state, detail) for one decrypted image, from its bytes.

    Two independent readings have to agree. The patcher that owns the fix finds
    its own site by instruction pattern, which is the authority on where the
    site is; the offset recorded in titles.py is what that answer is checked
    against. A disagreement means this is not the build either of them was
    written for, and the answer is then unrecognised rather than a guess at
    which of the two to believe.
    """
    offset = site["file_offset"]
    if len(image) < offset + 4:
        return UNRECOGNISED, (
            f"the decrypted binary is {len(image)} bytes, and the patch site "
            f"for this title is at {offset:08X}, past the end of it")
    word = bytes(image[offset:offset + 4])

    found = patchstate.decrypted_state(image, kind)
    if found.get("tool_fault"):
        return NOT_EXAMINED, (
            f"{found.get('missing') or 'a file this program needs'} is not "
            f"part of this build of the program, so the patch site was never "
            f"looked at. This is a fault in this program. Nothing is wrong "
            f"with the file on the console.")
    if found["state"] == patchstate.UNKNOWN:
        return UNRECOGNISED, found["evidence"]
    # A patched file can offer more than one candidate: a nop sitting between
    # the size argument and the call looks exactly like a call that has been
    # nopped out. The site finding narrows the field and the recorded offset
    # says which of them this build uses, so agreement is the offset being
    # among the candidates rather than being the first of them.
    candidates = found.get("offsets") or (
        (found["offset"],) if found.get("offset") is not None else ())
    if offset not in candidates:
        return UNRECOGNISED, (
            f"the fix's own site finding puts the patch at file offset "
            f"{found.get('offset', 0):08X}, where the verified table for this "
            f"title says {offset:08X}. This is not the build the fix was "
            f"written against")

    by_finder = PATCHED if found["state"] == patchstate.PATCHED else NOT_PATCHED
    by_bytes = _bytes_state(site, word)
    if by_bytes is None:
        return UNRECOGNISED, (
            f"the four bytes at file offset {offset:08X} are "
            f"{word.hex().upper()}, which is neither the stock nor the "
            f"patched instruction for this title")
    if by_bytes != by_finder:
        return UNRECOGNISED, (
            f"the four bytes at file offset {offset:08X} read as {by_bytes} "
            f"and the fix's own check reads as {by_finder}")
    return by_bytes, (f"the four bytes at file offset {offset:08X} are "
                      f"{word.hex().upper()}, which is {by_bytes}")


# --- the scan --------------------------------------------------------------

class FileScan:
    """One file in USRDIR, and what was read out of it."""

    def __init__(self, name, record, remote):
        self.name = name
        self.record = record
        self.remote = remote
        self.size = None
        self.sha1 = ""
        self.state = ""
        self.detail = ""
        self.offset = None
        self.image = record.get("image") if record else None
        self.image_sha1 = ""
        self.info = {}
        self.present = False
        #: Whether the user wants this one written. Every file that can be
        #: acted on starts ticked; the screen unticks what they exclude.
        self.selected = True
        # Set only for NOT_EXAMINED: what this program was missing. Carried as
        # a field so the screen can name it without picking it back out of a
        # sentence.
        self.missing_tool = ""

    @property
    def site(self):
        return titles.site_for(self.record)

    @property
    def content_id(self):
        return self.info.get("content_id", "")

    @property
    def needs_patching(self):
        return self.state == NOT_PATCHED

    def __repr__(self):
        return f"<FileScan {self.name} {self.state or 'unread'}>"


class ScanReport:
    def __init__(self, title_id, config=None, usrdir=""):
        self.title_id = titles.normalise(title_id)
        self.config = config
        self.title_key = config["key"] if config else None
        self.usrdir = usrdir
        self.files = []
        self.notes = []
        self.error = ""
        # Whether anybody has watched the fix work on this exact release. False
        # is not a refusal and must never be shown as one: it is the reason the
        # screen says the fix is being attempted rather than applied.
        self.verified = titles.is_verified(self.title_id)

    @property
    def ok(self):
        return not self.error

    @property
    def present(self):
        return [item for item in self.files if item.present]

    @property
    def to_patch(self):
        return [item for item in self.files if item.needs_patching]

    @property
    def unrecognised(self):
        return [item for item in self.files if item.state == UNRECOGNISED]

    @property
    def not_examined(self):
        return [item for item in self.files if item.state == NOT_EXAMINED]

    @property
    def cannot_decrypt(self):
        return [item for item in self.files if item.state == CANNOT_DECRYPT]

    @property
    def missing(self):
        return [item.name for item in self.files
                if not item.present and item.site]

    @property
    def chosen(self):
        """The files that will actually be written."""
        return [item for item in self.to_patch if item.selected]

    @property
    def blocked(self):
        """Files with a patch site that this program cannot act on.

        Unrecognised, unexamined, and would not decrypt. Each of them is left
        alone, and each of them is named on the screen.
        """
        return [item for item in self.files
                if item.site and item.state in (UNRECOGNISED, NOT_EXAMINED,
                                                CANNOT_DECRYPT)]

    @property
    def already_patched(self):
        return [item for item in self.files if item.state == PATCHED]

    def half_pairs(self):
        """Files that are one binary where only one of them is being written.

        Black Ops II's EBOOT.BIN and t6_ps3f.self are the same binary signed
        twice under different names. Writing one and leaving the other stock
        is a half-finished campaign, and the fix's own readme says that
        freezes.

        It is said rather than refused. The other file may be one this program
        cannot open at all, and whether to go ahead with what can be done is
        the user's decision to make with that in front of them.

        A file that is already patched is not one being left behind, which is
        the ordinary mixed install: one of the pair done on an earlier run and
        the other about to be.
        """
        chosen = {item.name for item in self.chosen}
        by_image = {}
        for item in self.files:
            if item.image and item.present:
                by_image.setdefault(item.image, []).append(item)
        out = []
        for image, items in sorted(by_image.items()):
            writing = [item.name for item in items if item.name in chosen]
            leaving = [item.name for item in items
                       if item.name not in chosen and item.state != PATCHED]
            if writing and leaving:
                out.append((image, writing, leaving))
        return out

    @property
    def can_patch(self):
        """Whether there is at least one file this run can act on.

        Judged one file at a time. These are separate binaries patched
        separately: Black Ops II's EBOOT.BIN and t6_ps3f.self carry campaign
        and zombies, t6mp_ps3f.self carries multiplayer, and one of them being
        a build this program does not recognise says nothing about whether the
        others can be patched safely.

        This used to require every file with a patch site to be understood, so
        a console with one file already patched, one stock and one
        unrecognised was refused outright and could do nothing at all. That is
        an ordinary mixed install and it was measured on hardware.

        An unrecognised file is still never written. That part has not moved.
        """
        return bool(self.ok and self.chosen)

    def file_for(self, name):
        for item in self.files:
            if item.name == name:
                return item
        return None


def _listing(writer, usrdir):
    text = writer.list_dir(usrdir)
    entries, unparsed = parsers.parse_ftp_list(text)
    return {entry["name"]: entry for entry in entries}, unparsed


def scan(writer, tool, title_id, progress=None, workdir=None):
    """What is in USRDIR and what state each file is in. Never raises.

    Entering a patcher screen runs this with no user action, so it has to come
    back with something to show whatever the console does.
    """
    title_id = titles.normalise(title_id)
    config = titles.config_for(title_id)
    if config is None:
        report = ScanReport(title_id)
        report.error = (
            f"{title_id} is not one of the games this tool fixes. It is not a "
            f"published release of Black Ops II or of Modern Warfare 3, so "
            f"there is nothing here for it. Nothing has been read and nothing "
            f"will be written.")
        return report

    usrdir = titles.usrdir_for(title_id)
    report = ScanReport(title_id, config, usrdir)

    problem = getattr(tool, "problem", "")
    if problem:
        report.error = problem
        return report

    try:
        entries, unparsed = _listing(writer, usrdir)
    except Exception as exc:
        report.error = (
            f"{usrdir} could not be listed ({exc.__class__.__name__}: {exc}). "
            f"Check the console is switched on, that webMAN is running and "
            f"that the address is right.")
        return report
    if unparsed:
        report.notes.append(f"{len(unparsed)} lines of the directory listing "
                            f"were not understood and were ignored")

    records = config["binaries"]
    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="ps3tools-scan-")
    states = {}
    try:
        for index, record in enumerate(records):
            item = FileScan(record["name"], record, f"{usrdir}/{record['name']}")
            report.files.append(item)
            entry = entries.get(record["name"])
            if entry is None or entry.get("kind") != "file":
                item.state = ""
                item.detail = "it is not in this folder"
                continue
            item.present = True
            item.size = entry.get("size")
            _scan_one(writer, tool, config, item, workdir, states,
                      index, len(records), progress, report)
    finally:
        if own_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    _cross_check(report)
    return report


def _scan_one(writer, tool, config, item, workdir, states, index, total,
              progress, report):
    """Pull one file, read its header, and read the state out of its bytes."""
    kind = config["key"]
    local = os.path.join(workdir, item.name)
    # What the console's own listing said this file is. Kept before the
    # download overwrites item.size with what actually arrived, because the
    # two differing is the whole of how a short copy is noticed.
    listed_size = item.size

    def step(message, **extra):
        if progress:
            progress(dict({"stage": "scan", "file": item.name, "done": index,
                           "total": total, "message": message}, **extra))

    step(f"copying {item.name} from the console")
    try:
        pulled = writer.retrieve(
            item.remote, local,
            on_block=lambda sent, size: step(f"copying {item.name}",
                                             bytes=sent, of=size))
    except Exception as exc:
        # A copy that did not finish says nothing about the file. Reported as
        # a read this program could not complete rather than as a file it has
        # read and rejected: the two ask different things of the user.
        item.state = NOT_EXAMINED
        item.detail = (f"it could not be copied off the console "
                       f"({exc.__class__.__name__}: {exc})")
        return
    item.sha1 = pulled["sha1"]
    item.size = pulled["bytes"]
    if isinstance(listed_size, int) and listed_size > 0 \
            and pulled["bytes"] != listed_size:
        # The console said one size and sent another, and no exception was
        # raised because the server would not answer SIZE. Everything below
        # would then be read out of part of a file: scetool prints the first
        # header and stops, which came back as the file having no App type in
        # it while the file on the console was complete.
        item.state = NOT_EXAMINED
        item.detail = (
            f"{pulled['bytes']} bytes arrived and the console's own listing "
            f"says it is {listed_size}, so what was copied is part of the "
            f"file. Nothing was read out of it. Switch the console off and on "
            f"and scan again.")
        return

    step(f"reading the header of {item.name}")
    try:
        item.info = tool.info(local, item.record["klicensee"])
    except ScetoolError as exc:
        item.state = UNRECOGNISED
        item.detail = str(exc)
        return

    site = item.site
    if site is None:
        # MW3's default.self. Reported rather than ignored, because a file
        # sitting beside the one being changed is something the user should
        # see named, but it has no patch site: the fault is not in it.
        item.state = NO_SITE
        item.detail = item.record["purpose"]
        return

    step(f"decrypting {item.name}")
    image_path = os.path.join(workdir, item.name + ".elf")
    try:
        image = tool.decrypt(local, image_path, item.record["klicensee"])
    except ScetoolError as exc:
        # The one place the attempt-and-see mechanism actually pays out. A
        # klicensee that does not suit this release cannot produce a plausible
        # binary, so this is where a release nobody has confirmed is found out,
        # cheaply and before anything is written.
        item.state = CANNOT_DECRYPT
        item.detail = cannot_decrypt_detail(
            report.title_id, item.name, report.verified, str(exc))
        return
    item.image_sha1 = _sha1(image)

    step(f"reading the patch site in {item.name}")
    # Two of Black Ops II's three files decrypt to the same image and share a
    # patch site, so the same answer is not worked out twice. The site is part
    # of the key: the same image read against a different site is a different
    # question, and answering it from this cache is how the multiplayer file
    # came back with the campaign file's verdict.
    key = (item.image_sha1, item.record.get("site"))
    if key not in states:
        states[key] = site_state(site, image, kind)
    item.state, item.detail = states[key]
    if item.state == NOT_EXAMINED:
        item.missing_tool = patchstate.PATCHER_FILES.get(kind, "")
    item.offset = (site["file_offset"]
                   if item.state not in (UNRECOGNISED, NOT_EXAMINED) else None)


def _cross_check(report):
    """Checks that only make sense across the set rather than file by file."""
    present = [item for item in report.files if item.present]
    ids = {item.content_id for item in present if item.content_id}
    if len(ids) > 1:
        report.error = (
            "These files do not all carry the same ContentID (" +
            ", ".join(sorted(ids)) + "). They are from different installs or "
            "different consoles, and mixing them gives a set that fails for "
            "no obvious reason. Nothing will be written.")
        return

    for content_id in sorted(ids):
        found = title_id_from(content_id)
        if found and found.upper() != report.title_id:
            report.error = (
                f"The files in {report.usrdir} carry the ContentID "
                f"{content_id}, which is {found} and not {report.title_id}. "
                f"Nothing will be written.")
            return

    _same_image_check(report, present)

    if report.missing:
        report.notes.append(
            "not in this folder: " + ", ".join(report.missing))
    for item in report.unrecognised:
        report.notes.append(f"{item.name} was not recognised and will be left "
                            f"alone: {item.detail}")
    for item in report.not_examined:
        report.notes.append(f"{item.name} could not be checked by this "
                            f"program and will be left alone: {item.detail}")
    if report.cannot_decrypt:
        # One note for the set rather than one per file. All three of Black
        # Ops II's files fail together for the same single reason, and saying
        # it three times reads as three separate faults.
        names = ", ".join(item.name for item in report.cannot_decrypt)
        report.notes.append(
            f"could not be opened and will be left alone: {names}")


def _same_image_check(report, present):
    """BO2's EBOOT.BIN and t6_ps3f.self are one binary signed twice.

    Two files that should decrypt to the same image and do not used to stop
    the whole title. On a console with one of them patched and the other still
    stock that refused a perfectly ordinary install: they differ by exactly the
    four bytes of the fix, which is what a half-finished patch looks like and
    is the state this screen exists to get somebody out of.

    So the comparison is made within each state. Two stock copies that differ,
    or two patched copies that differ, is one of them having been replaced and
    is worth saying. It is said and named, and it stops nothing: a file is
    judged on its own bytes, and these two are read separately.
    """
    by_image = {}
    for item in present:
        if item.image and item.image_sha1 and item.state:
            by_image.setdefault((item.image, item.state), []).append(item)
    for (image, state), items in sorted(by_image.items()):
        if len({item.image_sha1 for item in items}) < 2:
            continue
        names = ", ".join(sorted(item.name for item in items))
        report.notes.append(
            f"{names} should all decrypt to {image} and each read as {state}, "
            f"and they do not decrypt to the same thing. One of them has been "
            f"replaced or came from a different install. Each file is still "
            f"judged on its own bytes, so what can be patched safely still "
            f"can be.")


# --- applying the fix ------------------------------------------------------

def apply_fix(image, kind, module):
    """The fix itself, from the repository that owns it. Returns (bytes, offset).

    This is each script's main() with the file handling and the printing taken
    out. It is not called directly because both take their paths on argv, and
    because patch-bo2.py's main stops on a file that is already patched rather
    than saying so.
    """
    data = bytearray(image)
    try:
        if kind == "mw3":
            offset, state = module.find_site(data)
            if offset is None:
                raise PatchFailed("the patch site was not found in the "
                                  "decrypted binary")
            data[offset:offset + 4] = module.PATCHED
            return bytes(data), offset
        if kind == "bo2":
            address = module.find_format_string(data)
            sites = module.find_construct(data, address)
            if len(sites) != 1:
                raise PatchFailed(
                    f"the format string address is built in {len(sites)} "
                    f"places, so the call to remove is ambiguous")
            call = module.find_call(data, sites[0])
            struct.pack_into(">I", data, call, module.NOP)
            return bytes(data), call
    except SystemExit as exc:
        raise PatchFailed(f"the fix stopped: {exc or 'no reason given'}")
    raise PatchFailed(f"there is no fix for {kind}")


class PatchReport:
    def __init__(self, title_id):
        self.title_id = title_id
        self.backup = None
        self.changed = []
        self.uploaded = []
        self.restored = []
        self.restore_failed = []
        self.notes = []
        self.error = ""

    @property
    def ok(self):
        return not self.error

    @property
    def folder(self):
        return self.backup.folder if self.backup else ""


def patch(writer, tool, report, root=None, progress=None, when=None,
          reread=True, workdir=None):
    """Back up, patch, write back, verify, and put the originals back if not.

    The order is the whole design. The backup is taken and checked against the
    console before anything is decrypted, so a run that cannot produce a good
    backup stops having changed nothing. Everything is then built and verified
    locally, and only files that decrypt back to exactly what went into them
    are uploaded at all.
    """
    out = PatchReport(report.title_id)
    if not report.can_patch:
        out.error = (report.error or
                     "There is nothing here that can safely be patched.")
        return out
    chosen = report.chosen

    kind = report.title_key
    module = patchstate.patcher_module(kind)
    if module is None:
        name = patchstate.PATCHER_FILES.get(kind, "the %s patcher" % kind)
        out.error = (f"{name} is not part of this build of the program, so "
                     f"the fix cannot be applied. This is a fault in this "
                     f"program. Nothing has been changed.")
        return out

    wanted = [item.name for item in chosen]
    folder = backups.folder_for(report.title_id, root, when)
    try:
        saved = backups.make(writer, report.usrdir, wanted, folder,
                             progress=progress, reread=reread,
                             title_id=report.title_id, when=when)
    except backups.BackupFailed as exc:
        out.error = str(exc)
        return out
    out.backup = saved

    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="ps3tools-patch-")
    try:
        try:
            built = _build_all(tool, chosen, saved, kind, module, workdir,
                               progress, out)
        except (PatchFailed, ScetoolError) as exc:
            out.error = (f"{exc} Nothing has been written to the console, and "
                         f"your original files are in {folder}.")
            return out
        _upload_all(writer, report, saved, built, progress, out)
    finally:
        if own_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return out


def _build_all(tool, chosen, saved, kind, module, workdir, progress, out):
    """Every file rebuilt and checked locally before any of them is uploaded.

    Built from the backup copies rather than from what the scan downloaded, so
    that what is verified against the console and what is patched are the same
    bytes.
    """
    profile = SIGNING[kind]
    fallbacks = FIELD_FALLBACKS.get(kind, {})
    built = {}
    images = {}
    total = len(chosen)
    for index, item in enumerate(chosen):
        entry = saved.entry_for(item.name)
        source = entry["path"]
        klicensee = item.record["klicensee"]

        def step(message, **extra):
            if progress:
                progress(dict({"stage": "build", "file": item.name,
                               "done": index, "total": total,
                               "message": message}, **extra))

        step(f"decrypting {item.name}")
        image_path = os.path.join(workdir, item.name + ".elf")
        image = tool.decrypt(source, image_path, klicensee)

        step(f"applying the fix to {item.name}")
        patched, offset = apply_fix(image, kind, module)
        site = item.site
        if offset != site["file_offset"]:
            raise PatchFailed(
                f"the fix wants to change {item.name} at file offset "
                f"{offset:08X}, where the verified table for this title says "
                f"{site['file_offset']:08X}.")
        state, detail = site_state(site, patched, kind)
        if state != PATCHED:
            raise PatchFailed(f"{item.name} does not read as patched after "
                              f"the fix was applied: {detail}.")
        images[item.name] = patched
        patched_path = os.path.join(workdir, item.name + ".patched.elf")
        with open(patched_path, "wb") as handle:
            handle.write(patched)

        step(f"signing {item.name}")
        info = dict(fallbacks)
        info.update(item.info)
        used_fallback = [name for name in fallbacks if name not in item.info]
        if used_fallback:
            out.notes.append(
                f"{item.name}: this scetool did not print " +
                ", ".join(sorted(used_fallback)) +
                ", so the values the fix was verified against were used")
        destination = os.path.join(workdir, item.name + ".signed")
        tool.sign(profile, info, source, patched_path, destination,
                  item.name, klicensee)

        step(f"checking the rebuilt {item.name}")
        _verify_build(tool, item, source, destination, patched, klicensee,
                      workdir, kind)
        built[item.name] = destination
    return built


def _verify_build(tool, item, source, destination, patched, klicensee,
                  workdir, kind):
    """A rebuilt file has to decrypt back to exactly what went into it."""
    roundtrip_path = os.path.join(workdir, item.name + ".roundtrip.elf")
    actual = tool.decrypt(destination, roundtrip_path, klicensee)
    if actual != patched:
        raise PatchFailed(
            f"the rebuilt {item.name} does not decrypt back to the patched "
            f"binary ({len(patched)} bytes in, {len(actual)} bytes out).")
    state, detail = site_state(item.site, actual, kind)
    if state != PATCHED:
        raise PatchFailed(f"the rebuilt {item.name} does not read as patched "
                          f"after the trip through scetool: {detail}.")
    rebuilt = tool.info(destination, klicensee)
    changed = [name for name in CARRIED_FIELDS
               if item.info.get(name) and rebuilt.get(name) != item.info[name]]
    if changed:
        words = ", ".join(FIELD_TITLES[name] for name in changed)
        raise PatchFailed(
            f"the rebuilt {item.name} came back with a different {words}. A "
            f"file signed with the wrong one of those is perfectly valid and "
            f"will not load.")


def _upload_all(writer, report, saved, built, progress, out):
    """Upload, then read each file back and compare it with what was sent."""
    total = len(built)
    for index, (name, path) in enumerate(sorted(built.items())):
        item = report.file_for(name)
        with open(path, "rb") as handle:
            expected = _sha1(handle.read())

        def step(message, **extra):
            if progress:
                progress(dict({"stage": "upload", "file": name, "done": index,
                               "total": total, "message": message}, **extra))

        step(f"writing {name} to the console")
        try:
            writer.store(path, item.remote,
                         on_block=lambda sent, size: step(f"writing {name}",
                                                          bytes=sent, of=size))
        except Exception as exc:
            _roll_back(writer, saved, out, progress,
                       f"{name} could not be written to the console "
                       f"({exc.__class__.__name__}: {exc}).")
            return
        out.uploaded.append(name)

        step(f"checking {name} on the console")
        try:
            landed = writer.retrieve_bytes(item.remote)
        except Exception as exc:
            _roll_back(writer, saved, out, progress,
                       f"{name} was written but could not be read back to "
                       f"check it ({exc.__class__.__name__}: {exc}).")
            return
        if _sha1(landed) != expected:
            _roll_back(
                writer, saved, out, progress,
                f"{name} does not match what was sent: "
                f"{len(landed)} bytes arrived where {os.path.getsize(path)} "
                f"were sent. The transfer did not finish.")
            return
        out.changed.append(name)
    if progress:
        progress({"stage": "done", "file": "", "done": total, "total": total,
                  "message": "finished"})


def _roll_back(writer, saved, out, progress, reason):
    """Put the originals back, and say plainly if that could not be done."""
    restored, failed = backups.restore(writer, saved, progress=progress)
    out.restored = restored
    out.restore_failed = failed
    out.changed = []
    if failed:
        names = ", ".join(name for name, _why in failed)
        out.error = (
            f"{reason} Putting the original {names} back did not work either, "
            f"so the game may not start until it is restored by hand. Your "
            f"original files are in {saved.folder}. Switch the console off and "
            f"on, make sure webMAN is running, and copy them back into "
            f"{os.path.dirname(saved.entries[0]['remote'])} over FTP.")
    else:
        out.error = (f"{reason} The original files have been put back and the "
                     f"game is as it was. Your backup is in {saved.folder}.")


# --- finding an installation without ps3tools.detect -----------------------

def installed_title_ids(lister, title_key=None):
    """Title IDs of the titles this tool knows, from /dev_hdd0/game.

    A standby for the case where ps3tools.detect cannot be imported, which is
    only ever true while the two are being written side by side. detect does
    this properly: it separates a title that is installed but has no update
    from one that is not installed at all, and it refuses a Call of Duty title
    ID that is not in the known-good table rather than ignoring it.
    """
    text = lister.list_dir("/dev_hdd0/game")
    entries, _unparsed = parsers.parse_ftp_list(text)
    found = []
    for entry in entries:
        if entry.get("kind") != "directory":
            continue
        name = entry["name"].upper()
        key = titles.KNOWN_TITLE_IDS.get(name)
        if key and (title_key is None or key == title_key):
            found.append(name)
    return found


# --- where the title is, and why it might not be ---------------------------

# What the search for an installation concluded, before a single byte of a
# game file is read. These are kept apart rather than collapsed into "found or
# not found" because the four failures ask completely different things of the
# user: one is the console, one is this program's own fault, one is an empty
# hard drive and one is a download that has not happened yet. A screen that
# cannot tell them apart can only offer the user a sentence covering all of
# them, which is what the first version did and why nobody could act on it.
UNREACHABLE = "unreachable"
LIST_FAILED = "list_failed"
NOT_INSTALLED = "not_found"
NO_UPDATE = "no_update"
UNKNOWN_VARIANT = "unknown_variant"
READY = "ready"

GAME_ROOT = "/dev_hdd0/game"


class Location:
    """Where a title is on the console, or why that could not be answered.

    reason carries the underlying fault verbatim. It is shown rather than
    swallowed: the user cannot fix it, but it is the only thing they have to
    quote to somebody who can.
    """

    def __init__(self, state, title_ids=None, reason="", notes=None,
                 installation=None):
        self.state = state
        self.title_ids = list(title_ids or [])
        self.reason = reason
        self.notes = list(notes or [])
        self.installation = installation

    @property
    def title_id(self):
        return self.title_ids[0] if self.title_ids else ""

    @property
    def ready(self):
        return self.state == READY

    def __repr__(self):
        return f"<Location {self.state} {','.join(self.title_ids)}>"


def _files_present(item):
    """How many of this title's binaries are in that folder."""
    expected = list(getattr(item, "expected", None) or [])
    missing = list(getattr(item, "missing", None) or [])
    return len(expected) - len(missing)


def _by_files_present(picked):
    """The folders of one game, the one holding the most binaries first.

    A console can carry several folders for the same game. One user had
    NPUB31054 with the binaries in it and a leftover BLUS31011 holding
    licence files and nothing else; the folder the console happened to list
    first was taken, which was the empty one, and the run reported the title
    update as not downloaded. The title ID breaks a tie so that two scans of
    an unchanged console give the same answer.
    """
    return sorted(picked,
                  key=lambda item: (-_files_present(item),
                                    (item.title_id or "").upper()))


def _several_folders_note(chosen, others):
    """Which folder the fix will work on, when there is a choice.

    Said only when there is more than one, because the user's way of checking
    that the right copy was picked is seeing the folder named. A sentence on
    every scan would be read past.
    """
    names = ", ".join(item.title_id or "an unnamed folder" for item in others)
    detail = ""
    total = len(list(getattr(chosen, "expected", None) or []))
    if total:
        detail = (f", which holds {_files_present(chosen)} of the {total} "
                  f"files this fix changes")
    return (f"This game is in more than one folder on the console. "
            f"{chosen.title_id} was used{detail}. Also on the console: "
            f"{names}.")


def locate(lister, title_key, detector=None):
    """Find one title on the console, or say precisely why it was not found.

    Never raises, and never answers "not installed" unless the console was
    actually asked and actually answered. A listing that failed halfway is not
    evidence of an empty hard drive, and reporting it as one sends the user off
    to reinstall a game that is already there.

    detector is ps3tools.detect.find_installations. Without it only the two
    plain answers are available, because the title ID alone cannot tell an
    installation with no title update from one with a complete one.
    """
    opener = getattr(lister, "open", None)
    if opener is not None:
        try:
            opener()
        except Exception as exc:                            # noqa: BLE001
            # Nothing was asked of the console at all, so this is the address,
            # the network or webMAN, and never the state of the hard drive.
            return Location(UNREACHABLE, reason=_reason(exc))

    try:
        listing = lister.list_dir(GAME_ROOT + "/")
    except Exception as exc:                                # noqa: BLE001
        return Location(_failure_kind(exc), reason=_reason(exc))

    entries, unparsed = parsers.parse_ftp_list(listing)
    if unparsed and not entries:
        # The console answered and this program could not read the answer.
        # That is this program's fault and is reported as such.
        return Location(
            LIST_FAILED,
            reason=(f"the console sent {len(unparsed)} line(s) of listing and "
                    f"none of them were in a format this program understands. "
                    f"The first was: {unparsed[0].strip()}"))

    if detector is None:
        found = _known_ids(entries, title_key)
        if found:
            return Location(READY, found)
        return _confirmed_absence(lister)

    report = detector(lister)
    notes = list(getattr(report, "notes", []))
    found = report.for_title(title_key)
    for state in (READY, NO_UPDATE):
        picked = [item for item in found if item.state == state]
        if picked:
            ordered = _by_files_present(picked)
            if len(ordered) > 1:
                notes.append(_several_folders_note(ordered[0], ordered[1:]))
            return Location(state, [item.title_id for item in ordered],
                            notes=notes, installation=ordered[0])

    # An unrecognised SKU carries no title key, because nothing on the console
    # says which of the two games it is. It cannot be attributed to this screen
    # and it cannot be ignored either: it is the one case where the answer is a
    # refusal, and a user told "not installed" while the game sits in
    # /dev_hdd0/game learns nothing.
    strangers = [item for item in report.installations
                 if item.state == UNKNOWN_VARIANT]
    if strangers:
        return Location(UNKNOWN_VARIANT,
                        [item.title_id for item in strangers],
                        notes=notes, installation=strangers[0])
    return _confirmed_absence(lister, notes)


def _confirmed_absence(lister, notes=None):
    """Nothing found. Ask the console once more before saying so.

    find_installations keeps whatever it found when a console stops answering
    partway through and records the reason in a note, so an empty result on its
    own does not distinguish an empty hard drive from a console that was
    switched off during the walk. One more listing does.
    """
    try:
        lister.list_dir(GAME_ROOT + "/")
    except Exception as exc:                                # noqa: BLE001
        return Location(_failure_kind(exc), reason=_reason(exc), notes=notes)
    return Location(NOT_INSTALLED, notes=notes)


def _failure_kind(exc):
    """Whose problem the caller should be told this is.

    A refusal is the console answering, so the fault is in what this program
    asked for or what it did with the reply. A dead socket is the console not
    being there.
    """
    if isinstance(exc, ftplib.error_perm):
        return LIST_FAILED
    if isinstance(exc, (OSError, EOFError, ftplib.error_temp,
                        ftplib.error_proto, ftplib.error_reply)):
        return UNREACHABLE
    return LIST_FAILED


def _known_ids(entries, title_key):
    found = []
    for entry in entries:
        if entry.get("kind") != "directory":
            continue
        name = entry["name"].upper()
        key = titles.KNOWN_TITLE_IDS.get(name)
        if key and (title_key is None or key == title_key):
            found.append(name)
    return found


def _reason(exc):
    text = str(exc).strip()
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


# --- putting the originals back --------------------------------------------
#
# The undo half of this program. Before this existed the only route back was a
# hand-typed FTP session, which is not something the person this tool was
# written for can do, and it was the route they were most likely to need: a
# restore is what somebody reaches for when the game has stopped starting.
#
# The order matters as much as it does in patch(). Nothing is sent to the
# console until the backup has been checked against its own manifest and the
# title ID it was taken from has been matched against the one the console has
# now. Sending the wrong game's binaries, or a file that has been altered on
# the Desktop since, would produce exactly the install this tool exists to
# repair.


class RestoreReport:
    """What was put back, and what stopped it if anything did."""

    def __init__(self, title_id, backup=None):
        self.title_id = titles.normalise(title_id)
        self.backup = backup
        self.restored = []
        self.failed = []
        self.notes = []
        self.error = ""
        #: True when the refusal happened before anything was sent anywhere
        self.refused = False

    @property
    def ok(self):
        return not self.error

    @property
    def folder(self):
        return self.backup.folder if self.backup else ""


def _refusal(out, reason):
    """A stop with nothing sent. Always says that second part out loud."""
    out.error = reason
    out.refused = True
    return out


def restore_refusal(backup, title_id):
    """Why this backup must not be put back, or "" if it may be.

    Separate from restore() so that a screen can ask the question before it
    offers the user a button, and so that every refusal is worded in one place
    whether it is reached from the screen or from the flow. Touches nothing but
    the local disk: no part of deciding this involves the console.
    """
    if backup is None:
        return ("There is no backup here to put back. Nothing has been sent "
                "to the console.")
    # Asked before the emptiness below it: a folder with no record of its
    # contents reads as empty, and the reason it is empty is the thing to say.
    if backup.problem:
        return (f"{backup.problem} Because of that, this folder cannot be "
                f"checked before it is used, and files that cannot be checked "
                f"are not sent to the console. Nothing has been changed. If "
                f"you have another backup of this game, try that one.")
    if not backup.entries:
        return ("There is no backup here to put back. Nothing has been sent "
                "to the console.")

    wanted = titles.normalise(title_id)
    if not wanted:
        return ("This program does not know which game is on the console at "
                "the moment, so it cannot tell whether this backup belongs to "
                "it. Press Scan again first. Nothing has been sent to the "
                "console.")
    if titles.normalise(backup.title_id) != wanted:
        # The one mistake that turns a rescue into a much worse problem. The
        # files in a backup are one game's binaries, signed for the title they
        # came from; on another title they are a game that will not start.
        return (f"This backup was taken from "
                f"{backup.title_id or 'another game'} and the console has "
                f"{wanted} on it, so these are not that game's files. Putting "
                f"them back would leave you with an install that will not "
                f"start. Nothing has been sent to the console.\n\n"
                f"Look for a backup folder whose name begins {wanted}.")

    good, bad = checked_entries(backup)
    if not good:
        detail = "\n".join(f"    {row['name']}: {row['reason']}"
                           for row in bad)
        return (f"This backup does not match the record written when it was "
                f"taken, so it is not safe to put back and nothing has been "
                f"sent to the console:\n\n{detail}\n\n"
                f"A file that has changed since it was copied is not the "
                f"original any more, and sending it to the console would be a "
                f"guess. If you have another backup of this game, try that "
                f"one.")
    return ""


def checked_entries(backup):
    """(the files that still match the record, the rows that do not).

    A file that has changed since it was copied is not the original any more
    and is never sent. The rest of the backup is still the rest of the
    backup: these are separate binaries and putting two of the three back is
    two fewer patched files on the console than doing nothing at all.
    """
    rows = {row["name"]: row for row in backups.verify(backup)}
    good = [entry for entry in backup.entries
            if rows.get(entry["name"], {}).get("ok")]
    bad = [rows[name] for name in sorted(rows) if not rows[name]["ok"]]
    return good, bad


def restore(writer, backup, title_id, progress=None):
    """Put a checked backup back on the console, and confirm every file landed.

    Never raises. The caller is a screen in the middle of somebody's bad day,
    and an exception out of here would replace a sentence they can act on with
    a class name they cannot.
    """
    out = RestoreReport(title_id, backup)
    reason = restore_refusal(backup, title_id)
    if reason:
        return _refusal(out, reason)

    wanted = titles.normalise(title_id)
    usrdir = titles.usrdir_for(wanted)
    entries, unusable = checked_entries(backup)
    for row in unusable:
        out.failed.append((row["name"], row["reason"]))
    if unusable:
        names = ", ".join(row["name"] for row in unusable)
        out.notes.append(
            f"{names} did not match the record written when the backup was "
            f"taken, so it was left alone. A file that has changed since it "
            f"was copied is not the original any more. Everything else in "
            f"this backup is still put back.")
    total = len(entries)
    for index, entry in enumerate(entries):
        name = entry["name"]
        # Built from the title ID that was just matched rather than trusted
        # from the manifest, so the path written to is the one the console
        # actually has this game in.
        remote = f"{usrdir}/{name}"

        def step(message, **extra):
            if progress:
                progress(dict({"stage": "restore", "file": name, "done": index,
                               "total": total, "message": message}, **extra))

        step(f"putting the original {name} back")
        try:
            writer.store(entry["path"], remote,
                         on_block=lambda sent, size: step(
                             f"putting the original {name} back",
                             bytes=sent, of=size))
        except Exception as exc:                            # noqa: BLE001
            # On to the next file. These are separate binaries, and one that
            # could not be written says nothing about whether the next one
            # can be: a run that stopped here used to leave the files behind
            # it patched when their originals were sitting on the Desktop.
            out.failed.append(
                (name, f"it could not be written to the console "
                       f"({exc.__class__.__name__}: {exc})"))
            continue

        step(f"checking {name} on the console")
        try:
            landed = writer.retrieve_bytes(remote)
        except Exception as exc:                            # noqa: BLE001
            out.failed.append((name, f"it was written but could not be read "
                                     f"back to check it "
                                     f"({exc.__class__.__name__}: {exc})"))
            continue
        if _sha1(landed) != entry["sha1"]:
            out.failed.append(
                (name, f"it arrived on the console as something other than "
                       f"what was sent ({len(landed)} bytes arrived where "
                       f"{entry['size']} were sent), so the transfer did not "
                       f"finish"))
            continue
        out.restored.append(name)

    if out.failed:
        out.error = _restore_stopped(out)

    if progress:
        progress({"stage": "done", "file": "", "done": total, "total": total,
                  "message": "finished"})
    return out


def _restore_stopped(out):
    """What did not go back, and where that leaves them.

    The second half is the important one. A restore that could not finish has
    left the console holding a mixture, and a user who is told only that
    something failed does not know whether to run it again.

    Every file is attempted before this is written, so the list is the whole
    of what is still patched rather than the first thing that went wrong.
    """
    trouble = "\n".join(f"    {name}: {why}" for name, why in out.failed)
    done = (", ".join(out.restored) if out.restored else "none of them")
    return (f"These were not put back:\n\n{trouble}\n\n"
            f"Files put back: {done}. Your backup has not been touched and is "
            f"still complete, so nothing has been lost. Check the console is "
            f"switched on, sitting on its main menu with webMAN running, and "
            f"press this again: putting the same files back a second time is "
            f"safe and is the thing to do.")


# --- which title update the fix was verified against -----------------------
#
# Deliberately not a freshness check. The question is not "is this what Sony
# serves today", which changes without anyone here noticing and which would
# start telling users their console is wrong the day a new update ships. The
# question is "is this the build the patch offset was confirmed on", which is a
# fact about this program and changes only when somebody verifies another one.

#: the installed update is the one the fix was confirmed against
UPDATE_MATCHES = "update_matches"
#: the console has a different build from the one the fix was confirmed on
UPDATE_DIFFERS = "update_differs"
#: nobody has established a verified update for this release, so there is
#: nothing to compare against and nothing may be claimed either way
UPDATE_NOT_ESTABLISHED = "update_not_established"
#: there is a verified update, and the installed version could not be read
UPDATE_UNREADABLE = "update_unreadable"


def _version(value):
    """1.19 out of 01.19, so that the console's spelling and a person's match."""
    text = str(value or "").strip()
    return text.lstrip("0") if text and text[0] == "0" else text


class UpdateCheck:
    """Whether the installed title update is the one the fix was proved on."""

    def __init__(self, verdict, title_id="", installed=None, verified=None):
        self.verdict = verdict
        self.title_id = titles.normalise(title_id)
        #: what the console's PARAM.SFO said, or None if it could not be read
        self.installed = installed
        #: the update the fix was confirmed against, or None if there is none
        self.verified = verified

    @property
    def blocks(self):
        """Whether the fix must not be offered.

        Only a version that was read and disagrees. A version that could not be
        read is a different answer with a different remedy, and a release
        nobody has verified has no expected version at all: telling either of
        those users that their update is wrong would be a claim this program
        cannot support.
        """
        return self.verdict == UPDATE_DIFFERS

    def __repr__(self):
        return (f"<UpdateCheck {self.verdict} installed={self.installed!r} "
                f"verified={self.verified!r}>")


def update_check(title_id, installed):
    """Compare what is installed against the update the fix was verified on."""
    verified = titles.verified_update_for(title_id)
    if not verified:
        # An unverified release. It has no known-good update version, so the
        # only honest answer is silence: it is already handled as the release
        # nobody has confirmed, and must never be routed into "your update is
        # out of date", which is a different and unsupported claim.
        return UpdateCheck(UPDATE_NOT_ESTABLISHED, title_id, installed, None)
    if not installed:
        return UpdateCheck(UPDATE_UNREADABLE, title_id, None, verified)
    if _version(installed) == _version(verified):
        return UpdateCheck(UPDATE_MATCHES, title_id, installed, verified)
    return UpdateCheck(UPDATE_DIFFERS, title_id, installed, verified)
