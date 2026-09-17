"""Keysets, read from a file this code chooses the path of.

scetool looks for data/keys relative to the working directory, so whether it
works depends on where it was started from, and a file with the wrong line
endings comes out as "Could not decrypt header" rather than anything about
lines. Both of those are fixed here: the path is always given explicitly, and
the parser treats CR, LF and CRLF alike and says exactly which line it could
not read.

The file format is naehrwert's, so an existing keys file works unchanged:

    [appldr]
    type=SELF
    revision=0010
    version=0003005500000000
    self_type=NPDRM
    erk=<64 hex>
    riv=<32 hex>
    pub=<80 hex>
    priv=<42 hex>
    ctype=<hex>

More than one keyset can carry the same revision. Rather than picking the
first and hoping, every candidate is returned in file order and the caller
tries each: a SELF says whether the key was right, because the metadata info
decrypts with two blocks of padding that are zero and otherwise are not.
"""

import os
import re

from .errors import KeyNotFound, SceError

SECTION = re.compile(r"^\[([^\]]+)\]\s*$")
SETTING = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class KeySet:
    """One entry out of the keys file."""

    def __init__(self, name, fields, source="", line=0):
        self.name = name
        self.fields = fields
        self.source = source
        self.line = line

    def _hex(self, field, required=True):
        raw = self.fields.get(field, "")
        if not raw:
            if required:
                raise KeyNotFound(f"keyset [{self.name}] has no {field}",
                                  path=self.source, field=field)
            return b""
        try:
            return bytes.fromhex(raw)
        except ValueError:
            raise KeyNotFound(f"keyset [{self.name}] has a {field} that is "
                              f"not hex", path=self.source, field=field,
                              found=raw) from None

    @property
    def type(self):
        return self.fields.get("type", "")

    @property
    def self_type(self):
        return self.fields.get("self_type", "")

    @property
    def revision(self):
        raw = self.fields.get("revision", "")
        if not raw:
            return None
        try:
            return int(raw, 16)
        except ValueError:
            return None

    @property
    def version(self):
        raw = self.fields.get("version", "")
        try:
            return int(raw, 16) if raw else 0
        except ValueError:
            return 0

    @property
    def erk(self):
        return self._hex("erk")

    @property
    def riv(self):
        return self._hex("riv")

    @property
    def key(self):
        """Keysets that carry a bare key rather than an erk/riv pair."""
        return self._hex("key")

    @property
    def public(self):
        return self._hex("pub", required=False)

    @property
    def private(self):
        return self._hex("priv", required=False)

    @property
    def curve_type(self):
        raw = self.fields.get("ctype", "")
        try:
            return int(raw, 16) if raw else 0
        except ValueError:
            return 0

    def __repr__(self):
        rev = self.revision
        return (f"<KeySet {self.name} {self.self_type} "
                f"rev={'-' if rev is None else f'0x{rev:04X}'}>")


class KeyStore:
    """Every keyset in one file, with lookups that never guess silently."""

    def __init__(self, keysets, source=""):
        self.keysets = list(keysets)
        self.source = source

    @classmethod
    def from_text(cls, text, source=""):
        keysets = []
        name = ""
        fields = {}
        start = 0
        # splitlines handles CR, LF and CRLF alike, which is the whole of the
        # line endings problem. A keys file saved on Windows, edited on Linux,
        # or passed through a chat client all read the same.
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            header = SECTION.match(line)
            if header:
                if name:
                    keysets.append(KeySet(name, fields, source, start))
                name = header.group(1)
                fields = {}
                start = number
                continue
            setting = SETTING.match(line)
            if not setting:
                raise SceError(
                    f"line {number} is neither a [section] nor a name=value "
                    f"setting", path=source, field="keys file", found=line)
            if not name:
                raise SceError(f"line {number} sets a value before any "
                               f"[section]", path=source, field="keys file",
                               found=line)
            fields[setting.group(1)] = setting.group(2)
        if name:
            keysets.append(KeySet(name, fields, source, start))
        if not keysets:
            raise SceError("no keysets in this file", path=source,
                           field="keys file")
        return cls(keysets, source)

    @classmethod
    def from_file(cls, path):
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            if not os.path.exists(path):
                raise SceError(
                    "there is no keys file here. The keyset is naehrwert's "
                    "work and is not redistributed with this source, the "
                    "same as scetool never was. Put a copy of scetool's data "
                    "folder at " + os.path.dirname(path) + ". It has to "
                    "carry key revisions 0010, 0019 and 001C, which are what "
                    "Black Ops 1, Modern Warfare 3 and Black Ops II are "
                    "signed with.", path=path, field="keys file") from None
            raise SceError(f"the keys file could not be read "
                           f"({exc.strerror})",
                           path=path, field="keys file") from None
        # Decoded as latin-1 so that no byte can fail to decode. Everything
        # meaningful in the file is ASCII, and a stray byte in a comment should
        # not stop the keys loading.
        return cls.from_text(raw.decode("latin-1"), source=str(path))

    def named(self, name):
        """The single keyset with this name, for the NP_* entries."""
        found = [keyset for keyset in self.keysets if keyset.name == name]
        if not found:
            raise KeyNotFound(f"no keyset named [{name}]", path=self.source,
                              field="keys file")
        if len(found) > 1:
            raise KeyNotFound(f"[{name}] appears {len(found)} times and it "
                              f"should appear once", path=self.source,
                              field="keys file")
        return found[0]

    def named_key(self, name):
        return self.named(name).key

    def candidates(self, self_type, revision):
        """Every SELF keyset that could be the right one, in file order.

        More than one keyset can share a revision. Returning all of them and
        letting the file say which was right beats picking one and reporting a
        decryption failure that names no cause.
        """
        return [keyset for keyset in self.keysets
                if keyset.type == "SELF"
                and keyset.self_type == self_type
                and keyset.revision == revision]

    def require_candidates(self, self_type, revision, path=""):
        found = self.candidates(self_type, revision)
        if not found:
            known = sorted({f"0x{k.revision:04X}" for k in self.keysets
                            if k.type == "SELF" and k.self_type == self_type
                            and k.revision is not None})
            raise KeyNotFound(
                f"no {self_type} keyset for key revision 0x{revision:04X}. "
                f"This keys file has {', '.join(known) or 'none'}",
                path=path or self.source, field="key revision",
                found=revision)
        return found


def default_path():
    """The keys file shipped inside this package.

    Worked out from this module's own path and never from the working
    directory. That is the scetool behaviour this replaces: the same command
    worked or failed depending on which folder it was typed in, and the
    failure came out as "Could not decrypt header".

    Under PyInstaller the package is unpacked below sys._MEIPASS and
    __file__ still points inside it, so this keeps working in a frozen build
    without the caller passing anything.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "keys")


_CACHE = {}


def load(path=""):
    """The keystore at this path, read once and kept."""
    path = path or default_path()
    key = os.path.abspath(path)
    if key not in _CACHE:
        _CACHE[key] = KeyStore.from_file(path)
    return _CACHE[key]
