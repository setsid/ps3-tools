"""The diagnostic code must have no way to reach the write client.

This is the test that keeps the read-only promise true. ps3diag is what the
window tells the user cannot write to their console. If anything under it grows
an import of the patcher, that promise quietly stops being true, and nobody
finds out from reading the code because the import would be three modules away.

Checked two ways: statically, by walking every import in the package with ast,
and dynamically, by importing the whole of ps3diag in a clean interpreter and
asserting the patching modules never got loaded.
"""

import ast
import os
import subprocess
import sys
import unittest

from support import ROOT

DOMAIN = os.path.join(ROOT, "ps3diag")
FORBIDDEN_PREFIXES = ("ps3tools.patching", "ps3tools.shell",
                      "ps3tools.screens", "PySide6", "PyQt5", "PyQt6")


def imported_names(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


def domain_modules():
    for folder, _dirs, files in os.walk(DOMAIN):
        if "__pycache__" in folder:
            continue
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(folder, name)


class Statically(unittest.TestCase):
    def test_no_domain_module_imports_the_patcher_or_a_gui(self):
        offences = []
        for path in domain_modules():
            for name in imported_names(path):
                if name.startswith(FORBIDDEN_PREFIXES):
                    offences.append(
                        f"{os.path.relpath(path, ROOT)} imports {name}")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_the_domain_does_not_import_the_application_at_all(self):
        # Not even titles.py. The domain predates this application and has to
        # stay usable without it; an import the other way would make the
        # layering diagram a lie.
        offences = []
        for path in domain_modules():
            for name in imported_names(path):
                if name == "ps3tools" or name.startswith("ps3tools."):
                    offences.append(
                        f"{os.path.relpath(path, ROOT)} imports {name}")
        self.assertEqual(offences, [], "\n".join(offences))


class TheDiagnosticScreen(unittest.TestCase):
    """The screen is diagnostic code too, and the same rule applies to it.

    The domain check above cannot see this one: the screen lives under
    ps3tools, which is allowed to import ps3tools.patching in general. It is
    this particular module that must not, because it is the one the window
    labels as read-only.
    """

    PATH = os.path.join(ROOT, "ps3tools", "screens", "diagnostics.py")

    @unittest.skipUnless(os.path.exists(PATH), "diagnostics screen not written")
    def test_it_does_not_import_the_write_client(self):
        offences = [name for name in imported_names(self.PATH)
                    if name.startswith("ps3tools.patching")]
        self.assertEqual(offences, [], f"diagnostics.py imports {offences}")

    @unittest.skipUnless(os.path.exists(PATH), "diagnostics screen not written")
    def test_importing_it_does_not_pull_the_write_client_in(self):
        script = (
            "import sys\n"
            "import ps3tools.screens.diagnostics\n"
            "bad = [n for n in sys.modules if n.startswith('ps3tools.patching')]\n"
            "print('LOADED:' + ','.join(sorted(bad)))\n"
        )
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                              capture_output=True, text=True, timeout=120,
                              env=environment)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [item for item in proc.stdout.splitlines()
                if item.startswith("LOADED:")][0]
        self.assertEqual(line, "LOADED:", proc.stdout)


class AtRunTime(unittest.TestCase):
    def test_importing_every_domain_module_loads_no_write_client(self):
        script = (
            "import sys, pkgutil, importlib\n"
            "import ps3diag\n"
            "for info in pkgutil.walk_packages(ps3diag.__path__,\n"
            "                                  'ps3diag.'):\n"
            "    importlib.import_module(info.name)\n"
            "bad = [name for name in sys.modules\n"
            "       if name.startswith(('ps3tools.patching', 'PySide6'))]\n"
            "print('LOADED:' + ','.join(sorted(bad)))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [item for item in proc.stdout.splitlines()
                if item.startswith("LOADED:")][0]
        self.assertEqual(line, "LOADED:", proc.stdout)


class TheOtherDirectionIsFine(unittest.TestCase):
    def test_the_patcher_may_use_the_domain(self):
        # Stated as a test so nobody "fixes" the rule by making it symmetric.
        # The application is allowed to depend on the domain. That is the
        # direction the arrow points.
        from ps3tools import titles
        self.assertTrue(titles.KNOWN_TITLE_IDS)
