"""unfself, for a fake-signed SELF that scetool will not open.

The digital release of Black Ops 1, NPEB00756, is fake-signed. scetool cannot
decrypt it: there is no real signature for it to work back from, and what it
prints is a failure rather than a wrong answer. TrueAncestor's unfself handles
exactly that case, so where a file will not come open the ordinary way and
this is to hand, it is tried once and the result is used only if it is an ELF.

Nothing is bundled. unfself is not this program's to redistribute, so a copy
put in tools/unfself beside the bundled scetool is found and used, and a
program without one behaves exactly as it did before this file existed: the
file does not decrypt, the scan says so, and nothing is written.

The wrapper is the same shape as scetool.py's on purpose. The flow asks a tool
for info, decrypt and sign, and the only thing this changes is what happens
inside decrypt when the first attempt fails.
"""

import os
import subprocess
import sys

from .scetool import NO_WINDOW, ScetoolError

DEFAULT_TIMEOUT = 300

#: What the file has to start with for the result to be believed. A tool that
#: printed an error and wrote nothing, or wrote its own message into the
#: output, does not get to be treated as a decrypted binary.
ELF_MAGIC = b"\x7fELF"


def bundled():
    """The copy put beside the program, or "" if there is none."""
    roots = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(meipass)
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    roots.append(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    for root in roots:
        folder = os.path.join(root, "tools", "unfself")
        for name in ("unfself.exe", "unfself"):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
    return ""


def why_not(path):
    """Why this unfself cannot be used, or "" if it can."""
    if not path:
        return ("unfself is not part of this program. A fake-signed file "
                "cannot be opened without it.")
    if not os.path.isfile(path):
        return f"There is no file at {path}."
    try:
        with open(path, "rb") as handle:
            windows = handle.read(2) == b"MZ"
    except OSError as exc:
        return f"{path} could not be read ({exc})."
    if windows and sys.platform != "win32":
        return ("The unfself found here is a Windows program and this is not "
                "Windows.")
    return ""


class Unfself:
    """One operation: a fake-signed SELF to an ELF.

    runner is the only seam, spelled the way Scetool spells it, so a test
    gives it something callable and needs no process.
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

    def _run(self, args):
        try:
            finished = subprocess.run(
                [self.executable] + list(args), capture_output=True,
                timeout=self.timeout, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise ScetoolError(
                f"unfself did not finish within {self.timeout} seconds.")
        except OSError as exc:
            raise ScetoolError(f"unfself could not be run ({exc}).")
        return (finished.stdout or b"").decode("utf-8", "replace") \
            + (finished.stderr or b"").decode("utf-8", "replace")

    def decrypt(self, path, destination):
        """The decrypted ELF as bytes. Raises ScetoolError if it did not."""
        problem = self.problem
        if problem:
            raise ScetoolError(problem)
        output = self._runner([path, destination])
        if not os.path.isfile(destination):
            raise ScetoolError(
                f"unfself wrote nothing for {os.path.basename(path)}"
                + (f": {output.strip()}" if output.strip() else "."))
        with open(destination, "rb") as handle:
            data = handle.read()
        if data[:4] != ELF_MAGIC:
            raise ScetoolError(
                f"what unfself produced for {os.path.basename(path)} is not "
                f"an ELF, so it is not a decrypted binary.")
        return data


class WithFakeSigned:
    """scetool, with unfself behind it for the one case scetool cannot do.

    Everything except decrypt is scetool's, untouched. decrypt tries scetool
    first, so a file that opens the ordinary way is opened the ordinary way
    and nothing about the disc releases changes. Only a file that scetool
    refuses reaches unfself, and only if there is an unfself to reach.
    """

    def __init__(self, scetool, fallback=None):
        self._scetool = scetool
        self._fallback = fallback if fallback is not None else Unfself()

    @property
    def problem(self):
        # scetool's, and only scetool's. Without it there is no signing, and
        # without signing there is no patch whatever unfself can open.
        return self._scetool.problem

    @property
    def available(self):
        return self._scetool.available

    def info(self, path, klicensee=None):
        return self._scetool.info(path, klicensee)

    def sign(self, *args, **kwargs):
        return self._scetool.sign(*args, **kwargs)

    def decrypt(self, path, destination, klicensee=None):
        try:
            return self._scetool.decrypt(path, destination, klicensee)
        except ScetoolError as first:
            if not self._fallback.available:
                raise ScetoolError(
                    f"{first} This release is fake-signed, which scetool "
                    f"cannot open. {self._fallback.problem}")
            return self._fallback.decrypt(path, destination)
