"""scetool, wrapped so that it can be replaced.

scetool is naehrwert's. A copy is bundled under tools/scetool so the program
works out of the box on the machine it is meant to run on, which is Windows.
It is a Windows binary: on anything else this reports itself unavailable rather
than failing nine minutes into a job with a misleading message about the
klicensee. Every test injects a stand-in instead, which is also why the only
way the flow ever reaches scetool is through the small interface below.

Three operations are needed and no others:

    info      read the signing parameters off the user's own file
    decrypt   SELF to ELF, with the title's klicensee where one is needed
    sign      ELF back to SELF, with the parameters read in step one

The parameters are read off the file rather than written down here on purpose.
The two repositories' readmes print one worked example each, for one region and
one title update, and a file signed with another install's ContentID or the
wrong CID_FN hash is perfectly valid and will not load.
"""

import os
import re
import subprocess
import sys
from collections import namedtuple

# Keeps a console window from flashing on every one of the many calls.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_TIMEOUT = 300


class ScetoolError(Exception):
    """scetool refused or could not be run. The message is what it said."""


# --- where it lives --------------------------------------------------------

def bundled():
    """The copy shipped with the program, or "" if it is not there.

    Under PyInstaller --onefile the tools folder is unpacked below _MEIPASS, so
    that is looked at first; from a checkout it sits beside the package.
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(meipass)
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    roots.append(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    for root in roots:
        folder = os.path.join(root, "tools", "scetool")
        for name in ("scetool.exe", "scetool"):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
    return ""


def _is_windows_binary(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"MZ"
    except OSError:
        return False


def why_not(path):
    """Why this scetool cannot be used, or "" if it can.

    Checked up front because every one of these fails late and unhelpfully
    otherwise. The usual mistake is copying scetool.exe out on its own, which
    only goes wrong once it tries to look up a key.
    """
    if not path:
        return ("scetool is missing. It is bundled with this program, so a "
                "copy that has lost it was not unpacked properly.")
    if not os.path.isfile(path):
        return f"There is no file at {path}."
    folder = os.path.dirname(os.path.abspath(path))
    data = os.path.join(folder, "data")
    if not os.path.isdir(data):
        return ("There is no data folder beside this scetool. It looks its "
                "keys up relative to its own folder, so a copy of the "
                "executable on its own cannot work.")
    if not os.path.isfile(os.path.join(data, "keys")):
        return ("The data folder beside this scetool has no keys file in it.")
    if _is_windows_binary(path) and sys.platform != "win32":
        return ("The bundled scetool is a Windows program and this is not "
                "Windows, so the files cannot be decrypted or re-signed here. "
                "Run this tool on Windows.")
    return ""


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


def info_args(path, klicensee=None):
    args = ["-l", klicensee] if klicensee else []
    return args + ["-i", path]


def decrypt_args(source, destination, klicensee=None):
    args = ["-v"]
    if klicensee:
        args += ["-l", klicensee]
    return args + ["-d", source, destination]


def sign_args(profile, info, source, elf_path, destination, target_name,
              klicensee=None):
    """The re-sign from the readmes, with the user's own values in it.

    -g is the name the file will carry on the console, not wherever this
    happens to be writing to. It feeds the CID_FN hash, and getting it wrong
    gives a file that is perfectly valid and will not load.
    """
    args = []
    if profile.use_template:
        args += ["-t", source]
    args += ["-0", "SELF",
             "-1", profile.compressed,
             "-s", profile.skip_sections,
             "-2", info["key_revision"]]
    if profile.carry_ids:
        args += ["-3", info["auth_id"], "-4", info["vendor_id"]]
    args += ["-5", info["self_type"],
             "-A", info["app_version"],
             "-6", info["fw_version"],
             "-b", info["licence_type"],
             "-c", info["app_type"],
             "-f", info["content_id"],
             "-g", target_name]
    if klicensee:
        args += ["-l", klicensee]
    return args + ["-e", elf_path, destination]


# --- the wrapper itself ----------------------------------------------------

class Scetool:
    """The real thing. Tests inject something with the same three methods.

    runner is the only seam: give it something callable taking a list of
    arguments and returning scetool's output, and nothing here needs a process.
    """

    def __init__(self, executable=None, runner=None, timeout=DEFAULT_TIMEOUT):
        self.executable = executable if executable is not None else bundled()
        self.timeout = timeout
        self._runner = runner or self._run

    @property
    def problem(self):
        return why_not(self.executable)

    @property
    def available(self):
        return not self.problem

    def _run(self, args, what):
        """scetool resolves its data and keys folder relative to the working
        directory, so it has to run from its own folder whatever ours is."""
        try:
            proc = subprocess.run(
                [self.executable] + list(args),
                cwd=os.path.dirname(os.path.abspath(self.executable)),
                capture_output=True, text=True, errors="replace",
                timeout=self.timeout, creationflags=NO_WINDOW)
        except OSError as exc:
            raise ScetoolError(
                f"scetool could not be run ({exc}). Check that the copy "
                f"bundled with this program is built for this machine.")
        except subprocess.TimeoutExpired:
            raise ScetoolError(f"{what} did not finish within "
                               f"{self.timeout} seconds.")
        output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr)
                           if part and part.strip())
        if proc.returncode != 0:
            raise ScetoolError(f"{what} failed, scetool exited "
                               f"{proc.returncode}.\n"
                               f"{output or '(scetool printed nothing)'}")
        return output

    def info(self, path, klicensee=None):
        """The signing parameters off one file, as a dict."""
        name = os.path.basename(path)
        output = self._runner(info_args(path, klicensee),
                              f"reading the header of {name}")
        found, unreadable = read_header(output)
        missing = [field for field in REQUIRED_FIELDS if field not in found]
        if missing and not reached_app_info(output):
            # The output stopped before the part that carries these fields, so
            # nothing here is a statement about the file. A file this program
            # could not read is a different thing from one it has read and
            # rejected, and saying the second sends somebody to look for a
            # header block that is sitting there in full.
            lines = [line for line in output.splitlines() if line.strip()]
            ended = lines[-1].strip() if lines else "nothing at all"
            raise ScetoolError(
                f"scetool stopped before it printed the details of {name}. "
                f"What came back is {len(lines)} line(s), ending at "
                f"{ended!r}, and it never reached the Application Info block. "
                f"Nothing here says anything is wrong with the file: this is "
                f"a read that did not finish. The usual cause is the copy "
                f"taken off the console being short of the whole file. "
                f"scetool said:\n{output}")
        if missing:
            absent = [field for field in missing if field not in unreadable]
            strange = [field for field in missing if field in unreadable]
            parts = []
            if absent:
                words = ", ".join(FIELD_TITLES[field] for field in absent)
                parts.append(f"{name} has no {words} in its header")
            for field in strange:
                # Named with the value that was actually printed. This is the
                # file this program has not been taught about, and the value
                # is the whole of what anybody needs in order to teach it.
                parts.append(f"{name} gives its {FIELD_TITLES[field]} as "
                             f"{unreadable[field]!r}, which this tool does "
                             f"not recognise")
            raise ScetoolError(
                "; ".join(parts) + f". It is not one of the signed binaries "
                f"this tool knows how to rebuild. scetool said:\n{output}")
        found["raw"] = output
        return found

    def decrypt(self, path, destination, klicensee=None):
        """SELF to ELF. Returns the decrypted bytes."""
        name = os.path.basename(path)
        output = self._runner(decrypt_args(path, destination, klicensee),
                              f"decrypting {name}")
        if not os.path.isfile(destination) or not os.path.getsize(destination):
            raise ScetoolError(
                f"decrypting {name} produced nothing. The usual cause is the "
                f"wrong klicensee for this title. scetool said:\n"
                f"{output or '(scetool printed nothing)'}")
        with open(destination, "rb") as handle:
            return handle.read()

    def sign(self, profile, info, source, elf_path, destination, target_name,
             klicensee=None):
        """ELF back to SELF, under the name it will carry on the console."""
        output = self._runner(
            sign_args(profile, info, source, elf_path, destination,
                      target_name, klicensee),
            f"signing {target_name}")
        if not os.path.isfile(destination) or not os.path.getsize(destination):
            raise ScetoolError(
                f"signing {target_name} produced no output. scetool said:\n"
                f"{output or '(scetool printed nothing)'}")
        return output
