"""keysmith: PS3 SELF and fake-signed SELF handling, in Python.

This replaces scetool, a 2012 Windows binary that writes its whole report to
stderr, resolves its keys relative to the working directory, cannot read or
write a fake-signed SELF at all, and gets flagged as malware because it is an
unsigned executable of that age.

Three calls cover everything:

    inspect(file, klicensee="")           describe a file
    decrypt(file, klicensee="")           SELF or fself to ELF
    sign(elf, template, klicensee="")     ELF back to the form it came from

All three take a path or bytes. All three raise on failure, with the file, the
field and what was expected in the message, rather than printing a warning and
carrying on.

The public sign() takes the name of the module it is implemented in, so
keysmith.sign is the function and the module is reached through sys.modules
where anything needs it. Only the tests do.

Nothing in this package imports the rest of the application, and it depends on
the standard library alone, so it can be lifted into a repository of its own.
"""

import os

from . import fself as _fself
from . import keys as _keys
from . import report as _report
from . import sign as _sign
from .errors import (DecryptionFailed, KeyNotFound, NotAnSce, SceError,
                     SigningFailed, TruncatedFile, UnsupportedSce)
from .self import SelfFile

__all__ = [
    "inspect", "decrypt", "sign", "read", "Description",
    "SceError", "NotAnSce", "UnsupportedSce", "TruncatedFile", "KeyNotFound",
    "DecryptionFailed", "SigningFailed", "SelfFile", "VERSION",
]

VERSION = "1.0.0"


def _bytes_of(file):
    if isinstance(file, (bytes, bytearray, memoryview)):
        return bytes(file), ""
    path = os.fspath(file)
    with open(path, "rb") as handle:
        return handle.read(), path


def _klic(klicensee):
    """A klicensee as bytes, given bytes or the hex people paste around."""
    if not klicensee:
        return b""
    if isinstance(klicensee, (bytes, bytearray)):
        return bytes(klicensee)
    text = str(klicensee).strip().replace(" ", "")
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise KeyNotFound(
            "the klicensee is not hex", field="klicensee",
            expected="32 hex characters", found=klicensee) from None


def read(file):
    """The parsed file, without decrypting anything."""
    data, path = _bytes_of(file)
    return SelfFile(data, path)


class Description:
    """What inspect returns: the parsed file and a printable report."""

    def __init__(self, self_file, metadata=None):
        self.file = self_file
        self.metadata = metadata
        self.fake_signed = _fself.is_fake_signed(self_file)

    @property
    def key_revision(self):
        return self.file.sce.key_revision

    @property
    def content_id(self):
        npdrm = self.npdrm
        return npdrm.content_id_text if npdrm else ""

    @property
    def npdrm(self):
        if self.fake_signed:
            return _fself.npdrm_block(self.file)
        return self.file.npdrm

    @property
    def npdrm_is_zeroed(self):
        """True when the NPDRM block is present and entirely zero.

        That is what a fake-signed release built by TrueAncestor carries, and
        it is why such a file answers 8001000F on a console that checks
        licences. Worth reporting on its own rather than as "no block".
        """
        return _fself.has_zeroed_npdrm(self.file)

    def lines(self):
        return _report.describe(self.file, self.metadata)

    def text(self):
        return "\n".join(self.lines())


def inspect(file, klicensee="", keys_path=""):
    """Describe a SELF or a fake-signed SELF.

    The metadata is decrypted when a key for it can be found, and left out
    when it cannot, so a file whose klicensee is not to hand still describes
    itself rather than failing.
    """
    parsed = read(file)
    if _fself.is_fake_signed(parsed):
        return Description(parsed)
    store = _keys.load(keys_path) if keys_path else None
    try:
        metadata = parsed.decrypt_metadata(_klic(klicensee), store)
    except SceError:
        metadata = None
    return Description(parsed, metadata)


def decrypt(file, klicensee="", keys_path=""):
    """The ELF inside, whether the file is signed or fake signed."""
    parsed = read(file)
    if _fself.is_fake_signed(parsed):
        return _fself.to_elf(parsed)
    store = _keys.load(keys_path) if keys_path else None
    return parsed.to_elf(_klic(klicensee), store)


def sign(elf, template, klicensee="", keys_path=""):
    """An ELF put back into the container its template came from.

    The template is the user's own original file. Everything that identifies
    the file is taken from it, which is the whole point: a rebuild that loses
    the NPDRM block or writes application type 0 gives 8001000F, and both of
    those are mistakes shipped tools have made.
    """
    parsed = template if isinstance(template, SelfFile) else read(template)
    if isinstance(elf, (str, os.PathLike)):
        with open(os.fspath(elf), "rb") as handle:
            elf = handle.read()
    elf = bytes(elf)
    if _fself.is_fake_signed(parsed):
        return _fself.rebuild(parsed, elf)
    store = _keys.load(keys_path) if keys_path else None
    return _sign.rebuild(parsed, elf, _klic(klicensee), store)
