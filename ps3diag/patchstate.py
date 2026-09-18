"""Patch state for the two known Call of Duty PSN fixes.

Two titles have a published fix for a fault that takes PSN down. Black Ops II
freezes the whole console whenever a PSN session becomes active. Modern Warfare
3 throws you out of every online lobby on an account made after 2018. Both
fixes replace one instruction inside the game's own executable. This module
works out, from an artefact set, whether those titles are installed and whether
the fix looks as though it has been applied.

What cannot be determined from here, said first because a confident wrong
answer costs somebody their install:

The files both fixes touch are encrypted SELF binaries. The patched bytes sit
inside the encrypted, compressed data section. Decrypting needs scetool and the
appldr keys, which this tool does not ship and will not ship, so the patched
instruction is never visible to it. Nothing below claims to have seen it. Where
the answer is "cannot tell", the answer is unknown, and unknown is the common
case rather than a failure.

What can be read with no keys at all, and is what the states below are built
from:

  * the size of the file on disk, from a directory listing
  * the SCE header: magic, version, key revision, header type, header length,
    and the data length, which is the size of the decrypted ELF and therefore
    pins down which build of the game this is
  * the NPDRM control info, which is stored in the clear: licence type,
    application type, the ContentID, which carries the title ID and the title
    update, and the CID_FN hash
  * a SHA-1 of the whole file, if a collector ever computes one

The reference sizes and hashes below cover exactly one title update per title,
the one each repository was tested against, taken from its README, plus the
sizes this program's own patcher produces on that same update. A file that
matches none of them is usually a different update rather than a patched file,
so a size that is merely unfamiliar produces unknown and not unpatched.

Why this does not just decrypt the file, when the patcher half does:

The patcher has a console in front of it. It opens a connection, pulls the
whole binary down, runs the scetool that ships alongside it and reads the
patched instruction out of the decrypted image. That is definitive and this is
not, and the two are still right to differ, because they are not doing the same
job. patch_state() runs over an ArtefactSet, and an artefact set carries file
names and sizes and never file contents. That is the schema, on purpose, so
that a report zipped up on somebody's console can be read by a helper with no
console anywhere near them. There are no bytes here to decrypt. Getting them
would mean a live console, three downloads of six or seven megabytes apiece,
and a check that quietly stopped working on every saved report, which is most
of them. Decrypting is a thing the patcher does to a console; this is a thing
the diagnostic does to a file somebody emailed.

So the two halves know different things and say so. The patcher reads the
instruction; this reports what a size and a plaintext header can support, and
says out loud that it has not seen the instruction. Where they disagree the
patcher is right. Making this one decrypt would take two changes nobody should
make quietly: artefact sets would have to carry file contents, and the
transport rule that allows small ranged reads but refuses whole-file downloads
would have to be widened. Neither is worth it for a check whose honest answer
is already available from the size.

The strongest keyless inference available is this: re-signing with scetool does
not reproduce Sony's own compression, so a re-signed file is a different size
from the stock file while its decrypted data length stays the same. A file
whose data length matches a known build but whose size does not match that
build's stock size has been rebuilt by somebody. That is consistent with the
fix, and it is not proof of it, so it is still reported as unknown with the
evidence spelled out.

If a decrypted binary ever becomes available, decrypted_state() answers
properly. It does not restate either patcher's signatures: it imports
patch-bo2.py and patch-mw3.py and calls their own site finding, so there is one
definition of where the patch site is and it lives in the repository that owns
the fix.
"""

import base64
import binascii
import importlib.util
import json
import os
import re
import struct
from collections import namedtuple

from . import config
from .findings import Finding
from .parsers import parse_ftp_list
from .regioncodes import find_title_id

SCHEMA_VERSION = 1
ARTEFACT_NAME = "patches/patch-state.json"
CATEGORY = "games"

PATCHED = "patched"
UNPATCHED = "unpatched"
UNKNOWN = "unknown"
MISSING = "missing"

# What a whole install is in, as opposed to what one file is in. A title update
# that is not installed has no files to have a state, so it is answered here
# rather than three times over with a file-level state that would have to mean
# "this file does not exist and that is fine".
INSTALLED = "installed"
NO_UPDATE = "no_update"

NO_UPDATE_NOTE = ("the game is here but its title update has not been "
                  "installed. The fix changes files that arrive with the "
                  "update, so there is nothing to patch until it has been "
                  "installed. That is normal and nothing is wrong")

HIGH = "high"
LOW = "low"

NO_KEYS = ("The patch is inside the encrypted part of the file and ps3-diag "
           "has no keys, so the instruction itself was not checked.")

# Title updates install to /dev_hdd0/game/<TITLE ID>/USRDIR wherever the game
# itself lives, and that is the copy both fixes patch.
GAME_DIR = "/dev_hdd0/game/%s/USRDIR"

Reference = namedtuple("Reference", "state title_id update size sha1")
Binary = namedtuple("Binary",
                    "name affected purpose key_revision app_type elf_size "
                    "references")
Title = namedtuple("Title",
                   "key title repo advice title_ids verified_title_ids "
                   "name_patterns binaries")

# Sizes and hashes are the reference values published in each repository's
# README, plus the sizes this program's own patcher has been seen to produce.
# Re-signing is what changes the size: the patched instruction is one word and
# costs nothing, while rebuilding the compressed data section with scetool does
# not reproduce Sony's own compression, so every signing setup lands on its own
# stable size. A patched size matching is therefore evidence and a patched size
# not matching is not.
BO2 = Title(
    key="bo2",
    title="Call of Duty: Black Ops II",
    repo="https://github.com/setsid/bo2-ps3-psn-freeze-fix",
    advice=("All three have to be done: fixing only the multiplayer binary "
            "leaves campaign and zombies freezing. Each file is re-signed "
            "with the content ID, application type and key revision read back "
            "off your own copy, so it boots the way the original did rather "
            "than depending on the extra custom firmware controls being left "
            "switched on. Keep the originals: they are the only way back, and "
            "one cannot be rebuilt from a patched copy."),
    # Every title ID Black Ops II was published under, so that an install is
    # named as the game it is wherever it came from. Recognising one of these
    # says nothing about whether the fix suits it: that is verified_title_ids
    # below, and the two are kept apart on purpose.
    #
    # ps3tools.titles holds the same list for the patcher. It is written out
    # twice rather than imported because this package is the read-only
    # diagnostic half and is not allowed to depend on the other half.
    title_ids=(
        "BCKS10223", "BCKS10232", "BCUS91450",
        "BLES01717", "BLES01718", "BLES01719", "BLES01720",
        "BLJM60548", "BLJM60549", "BLJM61109", "BLJM61110", "BLJM61230",
        "BLJM61231",
        "BLUS31011", "BLUS31080", "BLUS31140", "BLUS31141", "BLUS41005",
        "NPEB01204", "NPEB01205", "NPEB01206", "NPEB01207",
        # NPUB31054 is the North American digital release. It was missing
        # while 31055 and 31056 were here, and it is the copy somebody was
        # playing: the tool read a leftover disc folder beside it instead and
        # told him his title update had not been downloaded.
        "NPUB31054", "NPUB31055", "NPUB31056",
    ),
    # BLES01717, BLES01718 and BLUS31011 are named in the README. BLUS31140 is
    # in the repository's own scetool fixtures, as the ContentID
    # UP0002-BLUS31140_00-CODBLOPS2PATCH09. These four are the ones a working
    # fix has actually been seen on, and the reference sizes below are theirs.
    verified_title_ids=("BLES01717", "BLES01718", "BLUS31011", "BLUS31140"),
    name_patterns=(re.compile(r"(?i)\bblack\s*ops\s*(?:2|ii)\b"),
                   re.compile(r"(?i)\bbo\s*2\b"),
                   re.compile(r"(?i)\bblops\s*2\b")),
    # Two patched sizes per file, from two signing routes, both kept.
    #
    # 6095184 and 7214528 are the standalone repository's own build, published
    # in its README. 6096288, 6096288 and 7215488 are what this program's
    # patcher produced on a real BLES01717 title update 1.19 and were read back
    # off that console afterwards. Neither set is more correct than the other:
    # a file patched by either route is patched, and which one did it only says
    # who signed it. Without our own figures this tool could not recognise its
    # own output and answered "cannot tell" about a console it had just fixed.
    #
    # t6_ps3f.self patched comes out the same size as EBOOT.BIN patched. That
    # is an observation off a console, not a rule, and it is written out in
    # full for that reason. The two decrypt to the same image but are signed
    # separately, each with its own content ID and its own file name, so there
    # is no guarantee the sizes track each other on another SKU or another
    # update. Please do not tidy the repetition away by deriving one from the
    # other: a future pair that differs would be data, not a bug.
    binaries=(
        Binary("EBOOT.BIN", True, "campaign and zombies", 0x1C, 0x21,
               11706540,
               (Reference(UNPATCHED, "BLES01717", "1.19", 6108656,
                          "fceadf136dd4fbb6d0cb72f7df4a1eb35f7a33cb"),
                Reference(PATCHED, "BLES01717", "1.19", 6095184,
                          "bf32afcbefe96e1424215ffa5036088007f8d10e"),
                Reference(PATCHED, "BLES01717", "1.19", 6096288, None))),
        Binary("t6_ps3f.self", True,
               "campaign and zombies, the copy the console often loads",
               0x1C, 0x20, 11706540,
               (Reference(UNPATCHED, "BLES01717", "1.19", 6108656,
                          "457ba9131098a124b26e80b955722198e48dac35"),
                Reference(PATCHED, "BLES01717", "1.19", 6096288, None))),
        Binary("t6mp_ps3f.self", True, "multiplayer", 0x1C, 0x20, 14215768,
               (Reference(UNPATCHED, "BLES01717", "1.19", 7254288,
                          "0099df2812e45fcc36642df0ac014c4e3d4641c9"),
                Reference(PATCHED, "BLES01717", "1.19", 7214528,
                          "8af1f859c9fc0a96aae7b2e23abf5dd19b2cdce7"),
                Reference(PATCHED, "BLES01717", "1.19", 7215488, None))),
    ))

MW3 = Title(
    key="mw3",
    title="Call of Duty: Modern Warfare 3",
    repo="https://github.com/setsid/mw3-ps3-psn-fix",
    advice=("default.self is campaign and Spec Ops, is a separate binary, "
            "and the fix does not touch it."),
    # Every title ID Modern Warfare 3 was published under. See the note on
    # Black Ops II above: recognised is not the same as verified.
    title_ids=(
        "BCKS10195",
        "BLES01428", "BLES01429", "BLES01430", "BLES01431", "BLES01432",
        "BLES01433", "BLES01434",
        "BLJM60404", "BLJM60422", "BLJM60534", "BLJM60535", "BLJM61111",
        "BLJM61112",
        "BLUS30838", "BLUS30872", "BLUS30887",
        "NPEB00964", "NPEB00965", "NPEB00966", "NPEB00967", "NPEB00968",
        "NPEB00977", "NPEB00978",
        "NPEB90450", "NPEB90451",
        "NPUB30787", "NPUB30788",
    ),
    # BLES01428 is the tested one and is the ID in the README's ContentID.
    # BLUS30838 is the North American release.
    verified_title_ids=("BLES01428", "BLUS30838"),
    name_patterns=(re.compile(r"(?i)\bmodern\s*warfare\s*3\b"),
                   re.compile(r"(?i)\bmw\s*3\b")),
    binaries=(
        Binary("default_mp.self", True, "multiplayer", 0x19, 0x20, 7578328,
               (Reference(UNPATCHED, "BLES01428", "1.24", 7581072,
                          "1b02160e9daa943789ba5eda7258d1ce47d2df10"),
                # The American release. Two consoles show this size, which is
                # what makes it a stock build rather than one this program has
                # never seen. Without it every American copy came back as
                # "cannot tell", which is the honest answer to a size nobody
                # has reported and the wrong one for a size two people have.
                # No sha1: neither report carried one, and a hash guessed at
                # would be worse than none.
                Reference(UNPATCHED, "BLUS30838", "1.24", 7541328, None))),
        Binary("default.self", False, "campaign and Spec Ops", None, None,
               None, ()),
    ))

TITLES = (BO2, MW3)


# --- the encrypted file ----------------------------------------------------

SCE_MAGIC = 0x53434500
NPDRM_MAGIC = 0x4E504400
ELF_MAGIC = b"\x7fELF"

HEADER_TYPES = {1: "SELF", 2: "RVK", 3: "PKG", 4: "SPP"}
# Licence type as scetool prints it. 3 is what both of these titles carry.
LICENCE_TYPES = {1: "network", 2: "local", 3: "free"}


def _u16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def _u64(data, offset):
    return struct.unpack_from(">Q", data, offset)[0]


def header_bytes(record):
    """The captured head of a file, from whichever encoding it arrived in.

    Artefacts are UTF-8 text, so a collector that captures the first few KB of
    a binary records it as hex or base64 and names the encoding. Both are
    accepted here. Anything unreadable comes back as None rather than as a
    partial buffer that would be parsed as though it were real.
    """
    if not isinstance(record, dict):
        return None
    raw = record.get("header_hex")
    if isinstance(raw, str) and raw.strip():
        try:
            return bytes.fromhex(re.sub(r"\s+", "", raw))
        except ValueError:
            return None
    raw = record.get("header_base64")
    if isinstance(raw, str) and raw.strip():
        try:
            return base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            return None
    return None


def read_self_header(data):
    """Everything the plaintext head of a SELF says about itself.

    Returns a dict always. `kind` is one of self, elf, short or other, and
    every field that could not be read is absent rather than zero, because a
    zero here reads as a real value and would be acted on.
    """
    out = {"kind": "other", "notes": []}
    if not isinstance(data, (bytes, bytearray)):
        return out
    data = bytes(data)
    out["captured_bytes"] = len(data)
    if data[:4] == ELF_MAGIC:
        out["kind"] = "elf"
        out["notes"].append("starts with an ELF header, so it is not signed")
        return out
    if len(data) < 0x20:
        out["kind"] = "short"
        out["notes"].append("too short to hold an SCE header")
        return out
    try:
        magic = _u32(data, 0)
        if magic != SCE_MAGIC:
            out["notes"].append("no SCE magic at the start of the file")
            return out
        out["kind"] = "self"
        out["version"] = _u32(data, 0x04)
        out["key_revision"] = _u16(data, 0x08)
        header_type = _u16(data, 0x0A)
        out["header_type"] = header_type
        out["header_type_name"] = HEADER_TYPES.get(header_type)
        out["metadata_offset"] = _u32(data, 0x0C)
        out["header_length"] = _u64(data, 0x10)
        out["data_length"] = _u64(data, 0x18)
    except struct.error:
        out["notes"].append("SCE header is truncated")
        return out
    if header_type != 1:
        out["notes"].append("not a SELF: header type %s"
                            % out["header_type_name"] or header_type)
        return out
    try:
        control_offset = _u64(data, 0x58)
        control_size = _u64(data, 0x60)
        out["app_info_offset"] = _u64(data, 0x28)
        out["control_info_offset"] = control_offset
        out["control_info_size"] = control_size
    except struct.error:
        out["notes"].append("SELF header is truncated")
        return out
    app = _read_app_info(data, out.get("app_info_offset"))
    if app:
        out["app_info"] = app
    npdrm, note = _read_control_info(data, control_offset, control_size)
    if npdrm:
        out["npdrm"] = npdrm
    if note:
        out["notes"].append(note)
    return out


# scetool spells self_type out; only the values these two titles use are named,
# and an unrecognised one is reported as its number rather than guessed at.
SELF_TYPES = {1: "LV0", 2: "LV1", 3: "LV2", 4: "application", 5: "isoldr",
              6: "ldr", 8: "NPDRM application"}


def _read_app_info(data, offset):
    if not offset or offset + 0x20 > len(data):
        return {}
    try:
        self_type = _u32(data, offset + 0x0C)
        return {
            "auth_id": "%016X" % _u64(data, offset),
            "vendor_id": "%08X" % _u32(data, offset + 0x08),
            "self_type": self_type,
            "self_type_name": SELF_TYPES.get(self_type),
            "version": "%016X" % _u64(data, offset + 0x10),
        }
    except struct.error:
        return {}


def _read_control_info(data, offset, size):
    """The NPDRM block out of the control info, which is not encrypted.

    Walks the chain rather than assuming NPDRM is third, because it is third on
    a retail file and not on everything.
    """
    if not offset or not size:
        return {}, None
    end = min(len(data), offset + size)
    if offset + 0x10 > end:
        return {}, ("control info starts at 0x%X, past the %d bytes captured"
                    % (offset, len(data)))
    position = offset
    while position + 0x10 <= end:
        try:
            entry_type = _u32(data, position)
            entry_size = _u32(data, position + 0x04)
            has_next = _u64(data, position + 0x08)
        except struct.error:
            return {}, "control info is truncated"
        if entry_size < 0x10:
            return {}, "control info entry size is not believable"
        if entry_type == 3:
            return _read_npdrm(data, position + 0x10, position + entry_size)
        if not has_next:
            break
        position += entry_size
    return {}, None


def _read_npdrm(data, start, end):
    if start + 0x60 > min(end, len(data)):
        return {}, "NPDRM control info is past the end of what was captured"
    try:
        magic = _u32(data, start)
        licence_type = _u32(data, start + 0x08)
        app_type = _u32(data, start + 0x0C)
        content_id = data[start + 0x10:start + 0x40]
        cid_fn = data[start + 0x50:start + 0x60]
    except struct.error:
        return {}, "NPDRM control info is truncated"
    if magic != NPDRM_MAGIC:
        return {}, "NPDRM control info has the wrong magic"
    text = content_id.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    # EP0002-BLES01428_00-MW3P000000000124: the underscore is a word character
    # and swallows the boundary the title ID patterns need.
    return {
        "licence_type": licence_type,
        "licence_type_name": LICENCE_TYPES.get(licence_type),
        "app_type": app_type,
        "content_id": text,
        "title_id": find_title_id(text.replace("_", " ")),
        "cid_fn_hash": cid_fn.hex(),
    }, None


# --- the decrypted file, using each patcher's own site finding -------------

# Set either of these to the patcher script if the copy that ships is not the
# one you want used. Nothing here ever fetches them: a missing patcher means
# the decrypted path answers unknown, which is the honest answer anyway.
PATCHER_ENV = {"bo2": "PS3DIAG_BO2_PATCHER", "mw3": "PS3DIAG_MW3_PATCHER",
               "bo1": "PS3DIAG_BO1_PATCHER", "mw2": "PS3DIAG_MW2_PATCHER"}

# The vendored copies, which are what an exe has. They are the same scripts the
# two repositories publish, kept here because those repositories are not on the
# machine the exe runs on. Without them every file on the patcher screen came
# back "not recognised" on a console that was perfectly fine.
PATCHER_FILES = {"bo2": "patch-bo2.py", "mw3": "patch-mw3.py",
                 "mw2": "patch-mw2.py",
                 "bo1": "patch-bo1.py"}
BUNDLED_DIR = os.path.join("tools", "patchers")

# Sibling checkouts, still searched so that a developer working on one of the
# fixes gets their working tree rather than the vendored snapshot.
PATCHER_PATHS = {"bo2": ("bo2/repo/patch-bo2.py", "bo2/patch-bo2.py",
                         "bo2-ps3-psn-freeze-fix/patch-bo2.py"),
                 "mw3": ("mw3-psn-fix/patch-mw3.py",
                         "mw3-ps3-psn-fix/patch-mw3.py"),
                 "bo1": ("bo1/repo/patch-bo1.py", "bo1/patch-bo1.py",
                         "bo1-ps3-stats-fix/patch-bo1.py"),
                 "mw2": ("mw2/repo/patch-mw2.py", "mw2/patch-mw2.py",
                         "mw2-ps3-stats-fix/patch-mw2.py")}

_patchers = {}


def _search_roots():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return (os.path.dirname(here), os.path.expanduser("~"), here)


def _bundled_candidates(kind):
    """tools/patchers/<script> wherever this program's own files are.

    Three places, all of which are the same directory in a source checkout:
    where PyInstaller unpacks the bundle, the folder the exe sits in, and the
    repository root. config already works all three out for scetool and the
    icons, so the answer comes from there rather than from a second guess.
    """
    name = PATCHER_FILES.get(kind)
    if not name:
        return []
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots = []
    for root in (config.bundle_dir(), config.app_dir(), here):
        if root and root not in roots:
            roots.append(root)
    return [os.path.join(root, BUNDLED_DIR, name) for root in roots]


def patcher_module(kind):
    """patch-bo2.py or patch-mw3.py as a module, or None if not to hand.

    Imported rather than copied so that the definition of where the patch site
    is stays in the script that owns the fix. Both scripts guard their main(),
    so importing one does nothing.
    """
    if kind in _patchers:
        return _patchers[kind]
    _patchers[kind] = None
    candidates = []
    # An override that loses to the bundled copy is not an override, so it goes
    # first even though the bundled copy is what nearly every run will use.
    env = os.environ.get(PATCHER_ENV.get(kind, ""), "")
    if env:
        candidates.append(env)
    candidates.extend(_bundled_candidates(kind))
    for root in _search_roots():
        for tail in PATCHER_PATHS.get(kind, ()):
            candidates.append(os.path.join(root, tail))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            name = "ps3diag_patcher_%s" % kind
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            continue
        module.__ps3diag_path__ = path
        _patchers[kind] = module
        return module
    return None


def decrypted_state(data, kind):
    """Patch state of a DECRYPTED binary, from the patcher's own site finding.

    Nothing in an artefact set is decrypted, so this is not on the path any
    live run takes. It is here so that the day somebody does have a decrypted
    ELF, or a collector grows the ability to produce one, the answer comes from
    the same code that applies the patch rather than from a second opinion
    written here.
    """
    out = {"state": UNKNOWN, "confidence": LOW, "evidence": ""}
    module = patcher_module(kind)
    if module is None:
        # Flagged rather than only worded, because a caller has to be able to
        # tell "we looked and the bytes were strange" from "this program could
        # not look at all". Only the first says anything about the user's file.
        out["tool_fault"] = True
        out["missing"] = PATCHER_FILES.get(kind, "the %s patcher" % kind)
        out["evidence"] = ("%s is missing from this program, so the decrypted "
                           "binary was not examined"
                           % out["missing"])
        return out
    if not isinstance(data, (bytes, bytearray)):
        out["evidence"] = "no binary to examine"
        return out
    data = bytearray(data)
    try:
        if kind == "mw3":
            return _mw3_decrypted(module, data)
        if kind == "bo2":
            return _bo2_decrypted(module, data)
        if kind == "bo1":
            return _bo1_decrypted(module, data)
        if kind == "mw2":
            return _mw2_decrypted(module, data)
    except SystemExit as error:
        out["evidence"] = "the patcher stopped: %s" % (error or "no reason")
        return out
    except Exception as error:
        out["evidence"] = "could not examine the binary: %s" % error
        return out
    out["evidence"] = "no patch site is defined for %s" % kind
    return out


def _mw3_decrypted(module, data):
    offset, state = module.find_site(data)
    if offset is None:
        return {"state": UNKNOWN, "confidence": LOW,
                "evidence": ("the patch site was not found, so this is not "
                             "the MW3 multiplayer binary the fix was written "
                             "for")}
    return {"state": UNPATCHED if state == "stock" else PATCHED,
            "confidence": HIGH, "offset": offset,
            "evidence": ("the patcher's own signature matches at file offset "
                         "%08X and reads as %s" % (offset, state))}


def _mw2_decrypted(module, data):
    """Modern Warfare 2, which finds its hook by signature.

    Like Black Ops 1, there is no recorded offset for this to be checked
    against, so the whole of the checking lives in the patcher and a refusal
    from it comes back as unknown rather than as a verdict. Unlike Black Ops
    1, the patcher also checks two instructions and one pointer value before
    it will call a build its own, and any of those failing is what produces
    the unknown.
    """
    try:
        # The checks first. A build whose identity cache pointer reads
        # something else is one the fix would refuse, so reporting it as
        # unpatched would be telling somebody their file is fine to patch
        # when it is not.
        module.check(bytes(data))
        offset, state = module.find_site(bytes(data))
    except Exception as error:
        return {"state": UNKNOWN, "confidence": LOW,
                "evidence": ("the fix does not recognise this build: %s"
                             % error)}
    if state is None:
        return {"state": UNKNOWN, "confidence": LOW, "offset": offset,
                "evidence": ("the instruction the fix replaces has been "
                             "changed to something this program did not "
                             "write, so what is in this file is somebody "
                             "else's patch")}
    return {"state": UNPATCHED if state == module.STOCK else PATCHED,
            "confidence": HIGH, "offset": offset,
            "evidence": ("the fix's own signature matches at file offset "
                         "%08X and reads as %s" % (offset, state))}


def _bo1_decrypted(module, data):
    """Black Ops 1, which finds every address it uses in the image itself.

    The other two answer against an offset recorded in titles.py as well as
    against their own signature. This one has no such offset to answer
    against, because its two known builds put the same code in different
    places, so the whole of the checking lives in the patcher and a refusal
    from it comes back as unknown rather than as a verdict.
    """
    try:
        offset, state = module.find_site(bytes(data))
    except module.NotThisBuild as error:
        return {"state": UNKNOWN, "confidence": LOW,
                "evidence": "%s" % error}
    if state is None:
        return {"state": UNKNOWN, "confidence": LOW, "offset": offset,
                "evidence": ("the call at file offset %08X has been changed "
                             "to something this fix did not write, so the "
                             "file is left alone" % offset)}
    return {"state": UNPATCHED if state == "stock" else PATCHED,
            "confidence": HIGH, "offset": offset,
            "evidence": ("the call the fix replaces is at file offset %08X "
                         "and reads as %s" % (offset, state))}


def _bo2_decrypted(module, data):
    address = module.find_format_string(data)
    sites = module.find_construct(data, address)
    if len(sites) != 1:
        return {"state": UNKNOWN, "confidence": LOW,
                "evidence": ("the format string address is built in %d places, "
                             "so the patch site is ambiguous" % len(sites))}
    try:
        call = module.find_call(data, sites[0])
    except SystemExit:
        call = None
    if call is not None:
        return {"state": UNPATCHED, "confidence": HIGH, "offset": call,
                "evidence": ("the snprintf call the fix removes is still at "
                             "file offset %08X" % call)}
    found = _bo2_nop_sites(module, data, sites[0])
    if not found:
        return {"state": UNKNOWN, "confidence": LOW,
                "evidence": ("the call site could not be read, so this may "
                             "not be a Black Ops II binary")}
    return {"state": PATCHED, "confidence": HIGH, "offset": found[0],
            "offsets": tuple(found),
            "evidence": ("the call at file offset %08X has been replaced with "
                         "a nop" % found[0])}


def _bo2_nop_sites(module, data, start):
    """Every place in the window where a patched-out call could be sitting.

    The patcher has no already-patched case: it looks for a branch and stops if
    there is not one. The window and the two constants are its own.

    Every candidate comes back rather than the first one. find_call looks for a
    branch, which is never padding, so the first it meets is the call. This
    looks for a nop, and a nop between the size argument and the call is
    ordinary compiler output. Taking the first put Black Ops II's multiplayer
    patch site several instructions early, and a file this program had patched
    itself came back as unrecognised on the user's own console while the
    campaign binary beside it read correctly.

    Which candidate is the patch site is settled by the caller against the
    offset recorded for the title. That is still two readings of the same
    question: this one narrows the field by instruction pattern, and the table
    says which of them this build puts it at.
    """
    out = []
    seen_size = False
    for step in range(0, 16):
        position = start + 4 * step
        if position + 4 > len(data):
            break
        word = module.u32(data, position)
        if word == module.LI_R4_1B8:
            seen_size = True
            continue
        if seen_size and word == module.NOP:
            out.append(position)
    return out


# --- the inventory ---------------------------------------------------------


def is_verified(spec, title_id):
    """Whether a working fix has actually been seen on this exact release.

    False does not mean the release is a stranger. It means this is one of the
    published title IDs nobody has reported back on, so the reference sizes and
    hashes above are somebody else's release and everything read here is that
    much weaker. The patcher half attempts those; this half only says so.
    """
    return (title_id or "").upper() in spec.verified_title_ids


def _match(entry):
    """(title spec, title ID, how it was matched) for one inventory row."""
    name = entry.get("name") or ""
    title_id = (entry.get("title_id") or find_title_id(name) or "").upper()
    for spec in TITLES:
        if title_id and title_id in spec.title_ids:
            return spec, title_id, "title ID"
    for spec in TITLES:
        if any(pattern.search(name) for pattern in spec.name_patterns):
            return spec, title_id or None, "name"
    return None, None, None


def installed_titles(artefact_set):
    """What is under /dev_hdd0/game, or None when that was never listed.

    None and [] are different answers and the difference is the whole of the
    third state below. [] means the console was asked and has no title update
    installed for anything. None means nobody asked, because the category
    failed or the report predates the collector recording it, and then nothing
    at all can be said about title updates.
    """
    records = artefact_set.facts(CATEGORY).get("installed_titles")
    if not isinstance(records, list):
        return None
    return [record for record in records if isinstance(record, dict)]


def installed_entry(spec, records, title_id):
    """The /dev_hdd0/game entry for one install, or None.

    Matched on the title ID when the inventory row carried one. When it did not
    - a disc image named "Modern Warfare 3 (Europe).iso" carries no ID in its
    name, which is common - any of that release's published IDs being installed
    is taken as the same game, because a console holding one title update for
    Modern Warfare 3 and a separate disc of Modern Warfare 3 that it does not
    belong to is not a case worth reporting a worse answer for.
    """
    for record in records or []:
        found = (record.get("title_id") or "").upper()
        if title_id and found == title_id:
            return record
        if not title_id and spec is not None and found in spec.title_ids:
            return record
    return None


def collected_usrdir(artefact_set, title_id, spec=None):
    """(files, where they came from) for one title's USRDIR, or (None, "").

    Two shapes are accepted. A games/facts.json `installed_titles` list, which
    is what a collector walking /dev_hdd0/game produces, and a raw FTP listing
    saved as games/<something with the title ID and USRDIR>.txt. Both are read
    the same way so neither is privileged.
    """
    record = installed_entry(spec, installed_titles(artefact_set), title_id)
    if record is not None:
        files = [item for item in record.get("files") or []
                 if isinstance(item, dict)]
        return files, (record.get("path") or "games/facts.json")
    if not title_id:
        return None, ""
    for name in artefact_set.names("games/*%s*" % title_id):
        lowered = name.lower()
        if not lowered.endswith(".txt") or "usrdir" not in lowered:
            continue
        entries, _unparsed = parse_ftp_list(artefact_set.text(name, ""))
        return entries, name
    return None, ""


def _find_file(files, wanted):
    for record in files:
        if (record.get("name") or "").lower() == wanted.lower():
            return record
    return None


def _reference(binary, state):
    for reference in binary.references:
        if reference.state == state:
            return reference
    return None


def _reference_for_size(binary, state, size):
    """The reference of this state whose size is the one on disk, if any.

    Every signing setup gives a patched file its own size, so one file has as
    many patched references as there are known ways it gets patched. All of
    them are asked rather than only the first, which is how this tool came to
    say "cannot tell" about a file it had patched itself.
    """
    if size is None:
        return None
    for reference in binary.references:
        if reference.state == state and reference.size == size:
            return reference
    return None


def _header_notes(binary, header, title_id):
    """Everything the plaintext header says, and whether it says anything
    wrong. Returns (notes, problem)."""
    notes = []
    problem = None
    if header.get("kind") != "self":
        for note in header.get("notes", ()):
            notes.append(note)
        return notes, problem
    revision = header.get("key_revision")
    if revision is not None:
        notes.append("key revision 0x%04X" % revision)
        if binary.key_revision is not None and revision != binary.key_revision:
            notes.append("the fix was tested against key revision 0x%04X"
                         % binary.key_revision)
    npdrm = header.get("npdrm") or {}
    if npdrm.get("content_id"):
        notes.append("ContentID %s" % npdrm["content_id"])
    if npdrm.get("app_type") is not None:
        notes.append("application type 0x%02X" % npdrm["app_type"])
        if binary.app_type is not None and npdrm["app_type"] != binary.app_type:
            problem = ("%s is application type 0x%02X where this file should "
                       "be 0x%02X. A file signed with the wrong application "
                       "type is valid and will not load."
                       % (binary.name, npdrm["app_type"], binary.app_type))
    if npdrm.get("title_id") and title_id and npdrm["title_id"] != title_id:
        notes.append("the ContentID is for %s, not %s"
                     % (npdrm["title_id"], title_id))
    length = header.get("data_length")
    if length is not None and binary.elf_size:
        if length == binary.elf_size:
            notes.append("the decrypted image is %d bytes, which is the build "
                         "the fix was tested on" % length)
        else:
            notes.append("the decrypted image is %d bytes where the tested "
                         "build is %d, so this is a different title update"
                         % (length, binary.elf_size))
    return notes, problem


def _state_from_file(binary, record, header, title_id):
    """Patch state of one file that is present, and why.

    The order is deliberate: a hash settles it, a size against a known build is
    evidence, and a header on its own never decides the state, only describes
    the file.
    """
    notes = []
    size = record.get("size")
    if not isinstance(size, int):
        size = None
    sha1 = (record.get("sha1") or "").lower()
    stock = _reference(binary, UNPATCHED)

    if sha1:
        for reference in binary.references:
            if reference.sha1 and sha1 == reference.sha1:
                word = ("the stock file" if reference.state == UNPATCHED
                        else "the reference patched build")
                return reference.state, HIGH, [
                    "SHA-1 %s matches %s for %s title update %s exactly"
                    % (sha1, word, reference.title_id, reference.update)]
        notes.append("SHA-1 %s matches neither reference build" % sha1)

    header_notes = []
    problem = None
    if header:
        header_notes, problem = _header_notes(binary, header, title_id)
    same_build = header and binary.elf_size and \
        header.get("data_length") == binary.elf_size

    patched = _reference_for_size(binary, PATCHED, size)

    if size is not None and stock and size == stock.size:
        notes.append("the file is %d bytes, exactly the stock size for %s "
                     "title update %s" % (size, stock.title_id, stock.update))
        notes.extend(header_notes)
        notes.append(NO_KEYS)
        return UNPATCHED, LOW, notes
    if patched:
        notes.append("the file is %d bytes, which is the size this file comes "
                     "out at once the fix has been applied to %s title update "
                     "%s and it has been signed again"
                     % (size, patched.title_id, patched.update))
        notes.append("a patched file signed with different parameters is a "
                     "different size again, so this matching is evidence and "
                     "not proof")
        notes.extend(header_notes)
        notes.append(NO_KEYS)
        return PATCHED, LOW, notes
    if size is not None and same_build and stock:
        notes.append("the file is %d bytes where the stock file for this build "
                     "is %d, and the decrypted image is the size of the build "
                     "the fix was tested on" % (size, stock.size))
        notes.append("that means somebody has rebuilt and re-signed this file, "
                     "which is what applying the fix does, but the same is "
                     "true of any other rebuild")
        notes.extend(header_notes)
        notes.append(NO_KEYS)
        return UNKNOWN, LOW, notes
    if size is not None:
        if stock:
            notes.append("the file is %d bytes and the only build with "
                         "reference values is %s title update %s at %d bytes"
                         % (size, stock.title_id, stock.update, stock.size))
        else:
            notes.append("the file is %d bytes and there are no reference "
                         "values for it" % size)
    notes.extend(header_notes)
    notes.append(NO_KEYS)
    if problem:
        notes.append(problem)
    return UNKNOWN, LOW, notes


def _sentences(notes):
    text = ". ".join(note.rstrip(".") for note in notes if note)
    return text + "." if text else ""


def _binary_record(spec, binary, usrdir, files, source, title_id):
    out = {
        "name": binary.name,
        # No folder, no path. A path was once built out of whatever the
        # inventory row was called, which on a disc image produced
        # "....iso/default_mp.self" and told the reader their game files were
        # inside a file. The name on its own says less and says nothing wrong.
        "path": "%s/%s" % (usrdir, binary.name) if usrdir else binary.name,
        "size": 0,
        "state": UNKNOWN,
        "evidence": "",
        "headline": "",
        "confidence": LOW,
        "affected": bool(binary.affected),
        "purpose": binary.purpose,
    }
    if not binary.affected:
        out["headline"] = "not touched by the fix"
        out["evidence"] = ("the fix does not touch this file, so there is no "
                           "patch state to report for it.")
        return out
    where = usrdir or "the folder the title update installs to"
    if files is None:
        out["headline"] = "%s was not listed" % where
        out["evidence"] = ("no listing of %s was collected, so it is not known "
                           "whether this file is even there. %s"
                           % (where, NO_KEYS))
        return out
    record = _find_file(files, binary.name)
    if record is None:
        out["state"] = MISSING
        out["confidence"] = HIGH
        out["headline"] = "not in the listing of %s" % where
        out["evidence"] = ("%s is not in the listing of %s taken from %s."
                           % (binary.name, where, source))
        return out
    size = record.get("size")
    out["size"] = size if isinstance(size, int) else 0
    header = None
    raw = header_bytes(record)
    if raw is not None:
        header = read_self_header(raw)
        # A decrypted ELF left in the game folder is the one case where the
        # patched instruction can actually be read, and only when the whole
        # file was captured rather than its head.
        if (header.get("kind") == "elf" and isinstance(size, int)
                and len(raw) >= size > 0):
            answer = decrypted_state(raw, spec.key)
            out["state"] = answer["state"]
            out["confidence"] = answer["confidence"]
            out["headline"] = "decrypted ELF, %s" % answer["evidence"]
            out["evidence"] = _sentences([
                "this file is a decrypted ELF and not a signed SELF, so the "
                "patch site itself was read", answer["evidence"]])
            return out
    state, confidence, notes = _state_from_file(binary, record, header,
                                                title_id)
    out["state"] = state
    out["confidence"] = confidence
    out["headline"] = notes[0] if notes else ""
    out["evidence"] = _sentences(notes)
    if header:
        out["header"] = header
        problem = _header_notes(binary, header, title_id)[1]
        if problem:
            out["header_problem"] = problem
    return out


def _title_record(artefact_set, spec, entry, title_id):
    device = entry.get("device") or "dev_hdd0"
    folder = entry.get("folder") or ""
    name = entry.get("name") or ""
    location = "/" + "/".join(part for part in (device, folder, name) if part)
    # Where the game itself sits and where the files the fix patches sit are
    # two different places, and only the second is a folder this tool can list.
    # An inventory row can be a disc image, so it is never turned into one.
    records = installed_titles(artefact_set)
    installed = installed_entry(spec, records, title_id)
    if installed is not None and not title_id:
        title_id = (installed.get("title_id") or "").upper()
    usrdir = GAME_DIR % title_id if title_id else ""
    files, source = collected_usrdir(artefact_set, title_id, spec)
    # Three answers, and the third one is the ordinary one. The files the fix
    # patches arrive with the title update, so a game that has never been run
    # online has none of them and there is nothing to patch yet. That is not
    # the same as not having looked, which is what a report with no listing of
    # /dev_hdd0/game at all means, so the two are kept apart.
    if files is not None:
        update_state = INSTALLED
    elif records is not None:
        update_state = NO_UPDATE
    else:
        update_state = UNKNOWN
    record = {
        "fix_key": spec.key,
        "title_id": title_id or "",
        "title": spec.title,
        # Recognised is not verified. Anything quoting the states below has to
        # be able to say which of the two this install is, because the
        # reference figures they are read against belong to the verified
        # releases and to no others.
        "verified": is_verified(spec, title_id),
        "location": location,
        "install_kind": entry.get("kind") or "",
        "usrdir": usrdir,
        "listing_source": source,
        "update_state": update_state,
        "repo": spec.repo,
        "advice": spec.advice,
        # Nothing per file when the title update is not installed: naming three
        # files and their state would say they are somewhere, and they are not
        # anywhere yet.
        "binaries": [] if update_state == NO_UPDATE else
                    [_binary_record(spec, binary, usrdir, files, source,
                                    title_id)
                     for binary in spec.binaries],
    }
    if update_state == NO_UPDATE:
        record["note"] = NO_UPDATE_NOTE
    elif entry.get("kind") == "file":
        record["note"] = ("this title is an image file. Its USRDIR is inside "
                          "the image and is not listed, and the fix patches "
                          "the copy that the title update installs to "
                          "/dev_hdd0/game in any case")
    return record


def patch_state(artefact_set):
    """The patches/patch-state.json payload. Never raises."""
    payload = {"schema_version": SCHEMA_VERSION, "titles": [],
               "notes": [NO_KEYS], "games_collected": False}
    try:
        payload["games_collected"] = bool(artefact_set.collected(CATEGORY))
        if not payload["games_collected"]:
            payload["notes"].append(
                "the game inventory was not collected, so no conclusion is "
                "drawn about any title being installed or not")
            return payload
        seen = set()
        for entry in artefact_set.game_entries():
            if not isinstance(entry, dict):
                continue
            spec, title_id, how = _match(entry)
            if spec is None:
                continue
            key = (spec.key, title_id or "", entry.get("name") or "")
            if key in seen:
                continue
            seen.add(key)
            record = _title_record(artefact_set, spec, entry, title_id)
            record["matched_by"] = how
            if how == "name":
                record.setdefault("note", "")
                # Saying the title update "is not known" in front of a record
                # that goes on to say no title update is installed reads as a
                # contradiction, so only the part that is still true is said.
                unknowns = ("the region and title update are not known"
                            if record.get("update_state") != NO_UPDATE
                            else "which region this copy is cannot be said")
                record["note"] = ("matched on the name rather than a known "
                                  "title ID, so %s. %s"
                                  % (unknowns, record["note"])).strip()
            payload["titles"].append(record)
        if not payload["titles"]:
            payload["notes"].append(
                "neither title with a known PSN fix was found in the game "
                "inventory")
    except Exception as error:
        payload["notes"].append("patch state detection failed: %s" % error)
    return payload


def patch_state_json(artefact_set):
    return json.dumps(patch_state(artefact_set), indent=2, sort_keys=True)


def record_into(artefact_set):
    """Write the payload into the set under its reserved name."""
    payload = patch_state(artefact_set)
    artefact_set.put(ARTEFACT_NAME,
                     json.dumps(payload, indent=2, sort_keys=True))
    return payload


# --- findings --------------------------------------------------------------

FREEZE = {
    "bo2": ("Black Ops II freezes the whole console when a PSN session becomes "
            "active: at launch while signed in, on a mode switch, or on "
            "signing in from inside multiplayer."),
    "mw3": ("Modern Warfare 3 drops you back to the multiplayer menu about a "
            "second after you reach a lobby, on any PSN account made after "
            "late 2018."),
}


def findings(artefact_set):
    """Findings for the rules and report layers. Never raises.

    One finding per fix rather than one per install, because a console with the
    same game in a folder and in an image has one problem and not two.
    """
    out = []
    try:
        payload = patch_state(artefact_set)
        grouped = {}
        for record in payload.get("titles", []):
            grouped.setdefault(record.get("fix_key", ""), []).append(record)
        for spec in TITLES:
            records = grouped.get(spec.key)
            if records:
                out.extend(_fix_findings(spec, records))
    except Exception:
        return out
    return out


def _collect(records, wanted):
    """Every affected binary across every install of one title in a state."""
    out = []
    for record in records:
        for binary in record.get("binaries", []):
            if binary.get("affected") and binary.get("state") in wanted:
                out.append(binary)
    return out


def _named(binaries):
    """File names, in the order the fix lists them, without repeats."""
    out = []
    for binary in binaries:
        if binary["name"] not in out:
            out.append(binary["name"])
    return out


def _evidence(binaries):
    return ["%s: %s" % (binary["path"], binary["headline"] or binary["state"])
            for binary in binaries]


def _installs(records):
    return ", ".join(sorted({record.get("title_id") or record["location"]
                             for record in records}))


def _fix_findings(spec, records):
    pending = [record for record in records
               if record.get("update_state") == NO_UPDATE]
    unpatched = _collect(records, (UNPATCHED,))
    unresolved = _collect(records, (UNKNOWN, MISSING))
    applied = _collect(records, (PATCHED,))
    problems = [binary for record in records
                for binary in record.get("binaries", [])
                if binary.get("header_problem")]
    where = _installs(records)
    out = []

    if problems:
        out.append(Finding(
            rule_id="%s-self-wrong-app-type" % spec.key,
            severity="error",
            title="A %s game binary is signed in a way that will not load"
                  % spec.title,
            explanation=("One of the files the fix replaces has been signed "
                         "with the wrong application type. A file like that is "
                         "perfectly valid and the console still will not load "
                         "it, so the game fails with nothing obviously wrong."),
            fix=("Re-sign it with the same parameters as the original, or put "
                 "your backup of it back. The tool at %s reads the parameters "
                 "off your own file and does this for you." % spec.repo),
            evidence=[binary["header_problem"] for binary in problems],
            category=CATEGORY))

    if unpatched:
        names = _named(unpatched)
        out.append(Finding(
            rule_id="%s-psn-fix-not-applied" % spec.key,
            severity="warn",
            title="%s (%s) does not have the PSN fix applied" % (spec.title,
                                                                 where),
            explanation=("%s There is a fix for it, and these files are still "
                         "the stock ones: %s."
                         % (FREEZE.get(spec.key, ""), ", ".join(names))),
            fix=("Apply the fix from %s to %s. %s Keep the originals: they are "
                 "the only way back and they cannot be rebuilt from the "
                 "patched copies." % (spec.repo, ", ".join(names),
                                      spec.advice)),
            evidence=_evidence(unpatched),
            category=CATEGORY))

    if unresolved and not unpatched:
        names = _named(unresolved)
        out.append(Finding(
            rule_id="%s-psn-fix-state-unknown" % spec.key,
            severity="info",
            title="Cannot tell whether the PSN fix is applied to %s (%s)"
                  % (spec.title, where),
            explanation=("%s Whether the fix is in these files cannot be read "
                         "from here: they are encrypted with keys this tool "
                         "does not have, and %s."
                         % (FREEZE.get(spec.key, ""),
                            _unresolved_reason(unresolved))),
            fix=("If the game does that, the fix and how to check it by hand "
                 "are at %s." % spec.repo),
            evidence=_evidence(unresolved),
            category=CATEGORY))

    if pending:
        out.append(Finding(
            rule_id="%s-title-update-not-installed" % spec.key,
            severity="info",
            title="%s is installed but its update has not been downloaded yet"
                  % spec.title,
            explanation=("%s The fix for that changes files that only arrive "
                         "when the game downloads its own update, and this "
                         "console has not downloaded it for this game yet, so "
                         "there is nothing to fix at the moment. Nothing is "
                         "wrong with the game." % FREEZE.get(spec.key, "")),
            fix=("Start the game once with the console connected to the "
                 "internet and let it install its update, then run this check "
                 "again. If the fix is needed by then it will be reported."),
            evidence=["%s: no title update in /dev_hdd0/game"
                      % (record.get("location")
                         or record.get("title_id") or spec.title)
                      for record in pending],
            category=CATEGORY))

    if applied and not unpatched and not unresolved:
        out.append(Finding(
            rule_id="%s-psn-fix-applied" % spec.key,
            severity="info",
            title="%s (%s) looks as though the PSN fix has been applied"
                  % (spec.title, where),
            explanation=("The game files match the patched build rather than "
                         "the stock one. That is read off the size and the "
                         "plain part of the header of encrypted files, so it "
                         "is an inference and not a reading of the patched "
                         "instruction itself."),
            fix="",
            evidence=_evidence(applied),
            category=CATEGORY))
    return out


def _unresolved_reason(binaries):
    """Why nothing could be said, in the words of the commonest case."""
    if all("was not listed" in (binary["headline"] or "")
           for binary in binaries):
        return ("the folder the fix patches was not listed, so it is not even "
                "known whether they are there")
    if any(binary["state"] == MISSING for binary in binaries):
        return "some of them are not where the fix expects them"
    return ("their size does not match either build there are reference "
            "figures for")
