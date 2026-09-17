"""The signing tool the flow uses, backed by keysmith instead of scetool.

Same three operations and the same shapes as the wrapper it replaces, so the
flow and its tests do not have to know which one they are talking to:

    info      read the signing parameters off the user's own file
    decrypt   SELF or fake-signed SELF to ELF
    sign      ELF back into the container it came out of

What changes underneath:

  * No subprocess, so no console window, no exit codes to interpret, and no
    output on stderr that looked like no output at all.
  * No Windows binary, so this works on the machine it is built on as well as
    the one it ships to, and no antivirus has an opinion about it.
  * Keys come from inside the package rather than from data/keys relative to
    the working directory.
  * Fake-signed files are read and written, so the digital releases no longer
    need TrueAncestor's unfself and can be patched end to end.

info is built by running keysmith's own report through the same header parser
the scetool wrapper used. That is deliberate: the report is byte for byte what
scetool printed, checked against the real binary across the whole corpus, so
reusing the parser means the fields cannot drift apart from what the flow
expects.
"""

import os

from ps3tools import keysmith

from .scetool import (FIELD_TITLES, REQUIRED_FIELDS, ScetoolError,
                      read_header, reached_app_info)


class Signer:
    """Drop-in for Scetool. Always available, because there is nothing to find.

    The runner seam the old wrapper had is gone: there is no process to stand
    in for. Tests that want to force a failure replace the whole object, which
    is what they already do.
    """

    def __init__(self, keys_path=""):
        self.keys_path = keys_path

    @property
    def problem(self):
        """Why this cannot be used, or "" if it can.

        The only thing that can be wrong is the keys file, and it is checked
        here rather than nine minutes into a job.
        """
        from ps3tools.keysmith import keys as keymod
        try:
            keymod.load(self.keys_path)
        except keysmith.SceError as exc:
            return str(exc)
        except Exception as exc:                            # noqa: BLE001
            return f"the keys could not be loaded: {exc}"
        return ""

    @property
    def available(self):
        return not self.problem

    def describe(self, path, klicensee=None):
        """The parsed file. The one seam: tests hand back a canned report."""
        try:
            return keysmith.inspect(path, klicensee or "", self.keys_path)
        except keysmith.SceError as exc:
            raise ScetoolError(str(exc)) from None

    def info(self, path, klicensee=None):
        """The signing parameters off one file, as a dict."""
        name = os.path.basename(path)
        described = self.describe(path, klicensee)
        output = described.text()
        found, unreadable = read_header(output)
        missing = [field for field in REQUIRED_FIELDS if field not in found]
        if missing and described.npdrm_is_zeroed:
            raise ScetoolError(
                f"{name} carries an NPDRM block that is entirely zero. It has "
                f"been rebuilt by a tool that dropped it, and a console that "
                f"checks licences answers 8001000F to a file in that state. "
                f"The licence type, application type, content ID and CID_FN "
                f"hash all have to come from the retail file it was made "
                f"from, so put the stock file back and patch that.")
        if missing and not reached_app_info(output):
            # The description stopped before the part these fields sit in, so
            # nothing here is a statement about the file. A file that could
            # not be read is a different thing from one that has been read and
            # rejected, and saying the second sends somebody looking for a
            # header block that is sitting there in full.
            lines = [line for line in output.splitlines() if line.strip()]
            ended = lines[-1].strip() if lines else "nothing at all"
            raise ScetoolError(
                f"reading {name} stopped before it printed the details. What "
                f"came back is {len(lines)} line(s), ending at {ended!r}, and "
                f"it never reached the Application Info block. Nothing here "
                f"says anything is wrong with the file: this is a read that "
                f"did not finish. The usual cause is the copy taken off the "
                f"console being short of the whole file. What was read:\n"
                f"{output}")
        if missing:
            parts = []
            absent = [f for f in missing if f not in unreadable]
            if absent:
                parts.append(f"{name} has no "
                             + ", ".join(FIELD_TITLES[f] for f in absent)
                             + " in its header")
            for field in (f for f in missing if f in unreadable):
                parts.append(f"{name} gives its {FIELD_TITLES[field]} as "
                             f"{unreadable[field]!r}, which this tool does "
                             f"not recognise")
            raise ScetoolError(
                "; ".join(parts) + f". It is not one of the signed binaries "
                f"this tool knows how to rebuild. What was read:\n{output}")
        found["raw"] = output
        found["fake_signed"] = described.fake_signed
        return found

    def decrypt(self, path, destination, klicensee=None):
        """The ELF, written to destination and returned.

        A fake-signed file goes down the same path as a signed one. That is
        the whole of what unfself was needed for.
        """
        try:
            elf = keysmith.decrypt(path, klicensee or "", self.keys_path)
        except keysmith.SceError as exc:
            raise ScetoolError(str(exc)) from None
        if destination:
            with open(destination, "wb") as handle:
                handle.write(elf)
        return elf

    def sign(self, profile, info, source, elf_path, destination, target_name,
             klicensee=None):
        """The patched ELF put back into the container source came out of.

        profile, info and target_name are taken for the same reasons the old
        wrapper took them and are not needed in the same way: every value they
        carried is read straight off source, which is the file itself, rather
        than being passed back in on a command line. They stay in the
        signature so the flow does not have to care which tool it holds.

        target_name fed scetool's -g, which decides the CID_FN hash. Here the
        hash comes off the original file unchanged, so a name that does not
        match cannot produce the valid-but-will-not-load file it used to.
        """
        del profile, info, target_name
        try:
            with open(elf_path, "rb") as handle:
                elf = handle.read()
            out = keysmith.sign(elf, source, klicensee or "", self.keys_path)
        except keysmith.SceError as exc:
            raise ScetoolError(str(exc)) from None
        except OSError as exc:
            raise ScetoolError(f"the patched ELF could not be read: "
                               f"{exc}") from None
        with open(destination, "wb") as handle:
            handle.write(out)
        return f"rebuilt {os.path.basename(destination)}, {len(out)} bytes"
