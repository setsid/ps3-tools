"""keysmith: PS3 SELF and fake-signed SELF handling, in Python.

This replaces scetool, a 2012 Windows binary that writes its whole report to
stderr, resolves its keys relative to the working directory, cannot read or
write a fake-signed SELF at all, and gets flagged as malware because it is an
unsigned executable of that age.

Four calls cover everything:

    inspect(file, klicensee="")           describe a file
    decrypt(file, klicensee="")           SELF or fself to ELF
    sign(elf, template, klicensee="")     ELF back to the form it came from
    fake_sign(elf, template)              ELF into a fake-signed SELF

All four take a path or bytes. All four raise on failure, with the file, the
field and what was expected in the message, rather than printing a warning and
carrying on.

sign() gives back the form the template came in, which for a retail file means
a retail re-sign. Its key_revision argument rebuilds against a different
keyset, which is what a PS3HEN console needs: HEN runs on 4.8x firmware and
loads a SELF through the 3.55-era keyset, key revision 0x000A. fake_sign()
gives a fake-signed file whatever the template was, and the case that calls
for it is a template that is already fake signed.

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
    "inspect", "decrypt", "sign", "fake_sign", "read", "Description",
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


def sign(elf, template, klicensee="", keys_path="", filename="",
         key_revision=None, grown_segments=False):
    """An ELF put back into the container its template came from.

    The template is the user's own original file. Everything that identifies
    the file is taken from it, which is the whole point: a rebuild that loses
    the NPDRM block or writes application type 0 gives 8001000F, and both of
    those are mistakes shipped tools have made.

    filename is the name the file will carry on the console. It feeds the
    CID_FN hash, so a file that will be written under a different name has to
    say so or it will be perfectly valid and refuse to load. Left empty, the
    name is taken to be unchanged.

    key_revision rebuilds against a different keyset rather than the
    template's own, writing the number into the SCE header and wrapping the
    metadata info under that revision's erk and riv. None keeps the
    template's. PS3HEN wants 0x000A; sign.py's docstring has the evidence,
    which is a third party's paired CFW and HEN builds of the same title
    differing in that field and in nothing else.

    grown_segments says the caller deliberately made a loadable segment
    longer, which a fix needing a code cave past the end of the loaded part of
    one has to do. Off by default, because a patch that changes instructions
    in place moves nothing and a header that moved under one of those is a
    fault. See sign.py for what it does and does not allow.

    A fake-signed template is rebuilt through its own header, which carries no
    keyset at all, so key_revision does not apply to one and is ignored.
    """
    parsed = template if isinstance(template, SelfFile) else read(template)
    if isinstance(elf, (str, os.PathLike)):
        with open(os.fspath(elf), "rb") as handle:
            elf = handle.read()
    elf = bytes(elf)
    if _fself.is_fake_signed(parsed):
        return _fself.rebuild(parsed, elf)
    store = _keys.load(keys_path) if keys_path else None
    return _sign.rebuild(parsed, elf, _klic(klicensee), store,
                         filename=filename, key_revision=key_revision,
                         grown_segments=grown_segments)


def fake_sign(elf, template, klicensee="", keys_path="", filename=""):
    """An ELF put into a fake-signed container, built from any template.

    The case this is right for is a template that arrives already fake signed,
    which is how the digital releases ship. Such a file has no keyset and no
    metadata, so it goes back out in the form it came in.

    It is not what a PS3HEN console needs. HEN loads an ordinary retail
    re-sign, as long as it is built against the 3.55-era keyset: Jakes625's
    paired CFW and HEN builds of Modern Warfare 2 are both retail
    re-signs, and the only header field that differs between them is the SCE
    key revision, 0x0010 against 0x000A. That is what sign(key_revision=...)
    is for.

    The template is the user's own retail file and everything that identifies
    the file is taken from it, the whole NPDRM control block included. That is
    the difference from every fake-signed release in the corpus, which carries
    a block of zeros and therefore answers 8001000F on a console that checks
    licences.

    A template that is already fake signed is rebuilt through its own header
    instead, so passing one here does the sensible thing rather than building
    a second container around it.

    filename is the name the file will carry on the console. It feeds the
    CID_FN hash, so only a file being renamed needs to give it, and only that
    case needs a keyset.
    """
    parsed = template if isinstance(template, SelfFile) else read(template)
    if isinstance(elf, (str, os.PathLike)):
        with open(os.fspath(elf), "rb") as handle:
            elf = handle.read()
    elf = bytes(elf)
    if _fself.is_fake_signed(parsed):
        return _fself.rebuild(parsed, elf)
    store = _keys.load(keys_path) if keys_path else None
    return _fself.from_retail(parsed, elf, filename=filename,
                              klicensee=_klic(klicensee), store=store)
