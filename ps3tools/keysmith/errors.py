"""Failures that name the file, the field and what was expected.

scetool's habit of writing "Could not decrypt header" to stderr and carrying on
is what these exist to replace. Every failure in this package carries enough to
act on without opening a hex editor: which file, which field, what was there
and what should have been.
"""


class SceError(Exception):
    """Base for everything this package raises.

    path, field, expected and found are all optional, because some failures
    genuinely have no field to point at. Where they are known they are in the
    message, so a caught exception printed on its own still says everything.
    """

    def __init__(self, message, path="", field="", expected=None, found=None):
        self.path = str(path or "")
        self.field = field
        self.expected = expected
        self.found = found
        parts = []
        if self.path:
            parts.append(self.path)
        if field:
            parts.append(field)
        head = ": ".join(parts)
        detail = message
        if expected is not None or found is not None:
            detail = (f"{message} (expected {_show(expected)}, "
                      f"found {_show(found)})")
        super().__init__(f"{head}: {detail}" if head else detail)


class NotAnSce(SceError):
    """The file does not start with an SCE header at all."""


class UnsupportedSce(SceError):
    """A real SCE file of a kind or version this does not handle."""


class TruncatedFile(SceError):
    """A field runs past the end of the file."""


class KeyNotFound(SceError):
    """No key in the keyset matches what this file needs."""


class DecryptionFailed(SceError):
    """Decryption produced something that cannot be right."""


class SigningFailed(SceError):
    """The file could not be rebuilt as asked."""


def _show(value):
    if isinstance(value, (bytes, bytearray)):
        return value.hex().upper()
    if isinstance(value, int):
        return f"0x{value:X}"
    return repr(value)
