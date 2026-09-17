"""Runs the real scetool.exe so its output can be compared against ours.

scetool resolves data/keys relative to the working directory and is a Windows
process, so it cannot be handed a Linux path. Both of those are worked around
here by copying it and its data into one scratch directory and running it from
inside that with plain relative names. It writes everything to stderr, so
stderr is captured as the output.

This is test scaffolding. Nothing the program ships ever calls it.
"""

import os
import shutil
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
BUNDLED = os.path.join(REPO, "tools", "scetool")
EXTRA_DLL = os.path.join(os.path.expanduser("~"), "bo1-BLES01031",
                         "msvcr100.dll")


class NoScetool(Exception):
    """scetool.exe cannot be run here, so the comparison has to be skipped."""


class Scetool:
    def __init__(self, workdir):
        self.workdir = workdir
        self.exe = os.path.join(workdir, "scetool.exe")
        os.makedirs(os.path.join(workdir, "data"), exist_ok=True)
        if not os.path.isfile(os.path.join(BUNDLED, "scetool.exe")):
            raise NoScetool(f"no scetool.exe under {BUNDLED}")
        for name in ("scetool.exe", "zlib1.dll"):
            source = os.path.join(BUNDLED, name)
            if os.path.isfile(source):
                shutil.copy2(source, workdir)
        if os.path.isfile(EXTRA_DLL):
            shutil.copy2(EXTRA_DLL, workdir)
        for name in os.listdir(os.path.join(BUNDLED, "data")):
            shutil.copy2(os.path.join(BUNDLED, "data", name),
                         os.path.join(workdir, "data", name))
        os.chmod(self.exe, 0o755)

    def runnable(self):
        try:
            self.run(["-h"])
            return True
        except Exception:                                   # noqa: BLE001
            return False

    def run(self, args, timeout=600):
        """Returns scetool's combined output. It writes to stderr."""
        result = subprocess.run([self.exe] + list(args), cwd=self.workdir,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
        return result.stdout.decode("utf-8", "replace")

    def bring_in(self, path, name=""):
        """Copies a file into the working directory and returns its name.

        scetool is a Windows process and cannot open /home/..., so everything
        it touches has to sit beside it under a plain name.
        """
        name = name or os.path.basename(path).replace(" ", "_")
        shutil.copy2(path, os.path.join(self.workdir, name))
        return name

    def info(self, name, klicensee=""):
        args = ["-i", name]
        if klicensee:
            args = ["-l", klicensee] + args
        return self.run(args)

    def decrypt(self, name, out_name, klicensee=""):
        args = ["-d", name, out_name]
        if klicensee:
            args = ["-l", klicensee] + args
        text = self.run(args)
        return os.path.join(self.workdir, out_name), text
