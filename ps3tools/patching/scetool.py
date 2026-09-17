"""The report format keysmith prints, and the parser for it.

This was the wrapper around scetool.exe. The binary is gone: keysmith does the
decrypting and the re-signing now, in Python, with its keyset inside the
package rather than beside the working directory.

What is left is the part that was never about the process. keysmith's report is
byte for byte the report scetool printed, checked field by field against the
real binaries, so this parser reads either one and the fields the flow works
with cannot drift apart from what is printed.

The exception keeps its name because the flow catches it in a dozen places and
the name still says which layer failed. It no longer means a program refused;
it means a file could not be read or rebuilt.
"""

import re
from collections import namedtuple

class ScetoolError(Exception):
    """A file could not be read or rebuilt. The message says which and why."""


# --- reading a header ------------------------------------------------------
#
# Fields are found by their own label anchored to the start of a line and by
# nothing else. Which block a field sits in, and how many blocks come before
# it, varies between scetool builds and between files: on a retail file the
# NPDRM values sit inside the third "Control Info" block, and an earlier
# version of this looked for an "NPDRM Info" heading that does not exist, so
# every one of those fields was silently dropped.
#
# The application version is the exception. Its label is the bare word
# "Version", which also appears in the SCE header and in the ELF header, so it
# is the only field that has to know which block it is in.

APP_SECTIONS = ("application info", "app info", "program identification")

HEADER_FIELDS = {
    "key_revision": ((), ("key revision", "key-revision")),
    "self_type": ((), ("self-type", "self type", "selftype")),
    "auth_id": ((), ("auth-id", "auth id", "authid")),
    "vendor_id": ((), ("vendor-id", "vendor id", "vendorid")),
    "app_version": (APP_SECTIONS, ("app version", "version")),
    "fw_version": ((), ("fw version", "firmware version", "self-fw-version")),
    "licence_type": ((), ("licence type", "license type", "drm type",
                          "np license type", "np licence type")),
    "app_type": ((), ("app type", "app-type", "apptype", "application type",
                      "np app type")),
    "content_id": ((), ("contentid", "content id", "content-id")),
    "cid_fn_hash": ((), ("cid_fn hash", "cid-fn hash", "cid fn hash",
                         "cidfn hash", "real filename hash",
                         "real fname hash")),
}

# Without these five there is nothing to re-sign with, so a file missing any of
# them is not one of the binaries this tool knows and the run stops.
REQUIRED_FIELDS = ("key_revision", "self_type", "app_type", "licence_type",
                   "content_id")

# Some builds print a name, some print only the number, and a retail file
# prints "NPDRM Application" where the documentation says "NPDRM". Each of
# these is resolved by name, by distinctive word, and by value.
SELF_TYPES = {"npdrm": "NPDRM", "app": "APP", "application": "APP",
              "lv0": "LV0", "lv1": "LV1", "lv2": "LV2", "iso": "ISO",
              "ldr": "LDR"}
SELF_TYPE_NUMBERS = {1: "LV0", 2: "LV1", 3: "LV2", 4: "APP", 5: "ISO",
                     6: "LDR", 8: "NPDRM"}
APP_TYPES = {"sprx": "SPRX", "exec": "EXEC", "executable": "EXEC",
             "usprx": "USPRX", "update sprx": "USPRX",
             "uexec": "UEXEC", "update exec": "UEXEC",
             "update executable": "UEXEC"}
# Read off real retail files: BO2's EBOOT.BIN is 0x21 and both its selfs are
# 0x20, and MW3's default_mp.self is 0x20. -c USPRX is what produces 0x20
# despite the name, and a file signed as UEXEC is valid and will not load.
APP_TYPE_NUMBERS = {0x01: "SPRX", 0x02: "EXEC", 0x20: "USPRX", 0x21: "UEXEC"}
LICENCE_TYPES = {"free": "FREE", "local": "LOCAL", "network": "NETWORK"}
LICENCE_TYPE_NUMBERS = {1: "NETWORK", 2: "LOCAL", 3: "FREE"}
# scetool prints these as names rather than as values on a retail file. The
# pair is what its own sign command wants back for -3 and -4.
AUTH_ID_NAMES = {"retail game/update": "1010000001000003"}
VENDOR_ID_NAMES = {"normal": "01000002"}

FIELD_TITLES = {
    "key_revision": "Key revision",
    "self_type": "SELF type",
    "auth_id": "Auth-ID",
    "vendor_id": "Vendor-ID",
    "app_version": "App version",
    "fw_version": "Firmware version",
    "licence_type": "Licence type",
    "app_type": "App type",
    "content_id": "ContentID",
    "cid_fn_hash": "CID_FN hash",
}


def _bracketed(text):
    match = re.search(r"\[([^\]]*)\]", text)
    return match.group(1) if match else text


def _number(text):
    match = re.search(r"0x([0-9A-Fa-f]+)", text)
    if match:
        return int(match.group(1), 16)
    match = re.search(r"\b(\d+)\b", text)
    return int(match.group(1)) if match else None


def _hex_value(text, digits):
    """A fixed width hex field, however widely the build has printed it."""
    match = re.search(r"0x([0-9A-Fa-f]+)", text)
    if not match:
        match = re.search(r"\b([0-9A-Fa-f]{%d})\b" % digits, text)
    if not match:
        return None
    return match.group(1).upper().rjust(digits, "0")[-digits:]


def _id_value(text, digits, names):
    value = _hex_value(text, digits)
    if value is not None:
        return value
    return names.get(_bracketed(text).strip().lower())


def _named_value(text, names, numbers, priority=()):
    lowered = _bracketed(text).strip().lower()
    for key in priority:
        if key in lowered:
            return names[key]
    if lowered in names:
        return names[lowered]
    for key, value in names.items():
        if key in lowered:
            return value
    number = _number(text)
    return numbers.get(number)


def _version_value(text):
    raw = _bracketed(text).strip()
    match = re.fullmatch(r"(\d{1,2})\.(\d{2})", raw)
    if match:
        return "%04X%04X00000000" % (int(match.group(1)), int(match.group(2)))
    digits = re.sub(r"[^0-9A-Fa-f]", "", raw)
    return digits.upper().rjust(16, "0")[-16:] if digits else None


def _content_id(text):
    value = _bracketed(text).replace("\x00", "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", value) else None


NORMALISERS = {
    # Four digits rather than two so it reads back as the 0019 the Modern
    # Warfare 3 readme passes to -2. scetool takes either: they are the same
    # number, and the Black Ops II readme writes the same field as 1C.
    "key_revision": lambda v: _hex_value(v, 4),
    "auth_id": lambda v: _id_value(v, 16, AUTH_ID_NAMES),
    "vendor_id": lambda v: _id_value(v, 8, VENDOR_ID_NAMES),
    "self_type": lambda v: _named_value(v, SELF_TYPES, SELF_TYPE_NUMBERS,
                                        priority=("npdrm",)),
    "app_type": lambda v: _named_value(v, APP_TYPES, APP_TYPE_NUMBERS),
    "licence_type": lambda v: _named_value(v, LICENCE_TYPES,
                                           LICENCE_TYPE_NUMBERS),
    "app_version": _version_value,
    "fw_version": _version_value,
    "content_id": _content_id,
    "cid_fn_hash": lambda v: re.sub(r"[^0-9A-Fa-f]", "", v).upper() or None,
}


def parse_header(text):
    """The signing parameters out of scetool -i output."""
    return read_header(text)[0]


def read_header(text):
    """(what was understood, {field: the text that was not understood}).

    Two different answers about one field, and they were being told as one. A
    field whose label never appears is not in the file as far as this program
    can see. A field that was printed, with a value that means nothing here,
    is a file this program has not been taught about.

    Saying the first when it is the second sends somebody to look for a header
    block that was there all along. It did: a US copy of Black Ops II was
    reported as having no App type in its header while scetool -i on the same
    file printed a complete Application Info block.
    """
    found = {}
    section = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("[*]"):
            section = line[3:].strip().rstrip(":").lower()
            continue
        lowered = line.lower()
        for name, (sections, labels) in HEADER_FIELDS.items():
            if name in found:
                continue
            if sections and not any(part in section for part in sections):
                continue
            for label in labels:
                if not lowered.startswith(label):
                    continue
                value = line[len(label):].lstrip(" :\t")
                if value:
                    found[name] = value
                break
    info = {}
    unreadable = {}
    for name, value in found.items():
        normalised = NORMALISERS[name](value)
        if normalised:
            info[name] = normalised
        else:
            unreadable[name] = value.strip()
    return info, unreadable


def reached_app_info(text):
    """Whether scetool's output got as far as the block the fields sit in.

    A complete -i on a signed binary prints an Application Info block, and the
    fields this program needs are in it and below it. An output that stops
    before it never mentioned them, so they are missing from the text rather
    than from the file.

    Measured: a scan reported "has no App type in its header" for a file whose
    header was complete. What came back was seven lines ending at the key
    revision, and scetool had exited without complaint.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("[*]"):
            continue
        section = line[3:].strip().rstrip(":").lower()
        if any(part in section for part in APP_SECTIONS):
            return True
    return False


def title_id_from(content_id):
    """UP0002-BLUS31011_00-CODBLOPS2PATCH09 is noise; BLUS31011 is the part
    anybody actually checks."""
    match = re.search(r"-([A-Za-z]{4}\d{5})_", content_id or "")
    return match.group(1) if match else ""


# --- signing ---------------------------------------------------------------
#
# The two fixes re-sign with different argument shapes, and the difference is
# not derivable from anything: each was arrived at by testing against real
# files of that title. So it is written down per title rather than guessed.
#
#   compressed / skip_sections   -1 and -s
#   use_template                 -t, which carries the Auth-ID, Vendor-ID and
#                                control info across from the original file
#   carry_ids                    pass -3 and -4 explicitly instead

SigningProfile = namedtuple(
    "SigningProfile", "compressed skip_sections use_template carry_ids")
