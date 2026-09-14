#!/usr/bin/env python3
"""Rebuilds tests/fixtures/sample-diagnostic.zip.

A real end to end run against the mock console, saved. It is a fixture rather
than a committed hand-written file so that it cannot drift away from what the
tool actually produces, which is the whole point of having it: the analysis
layer, the report and the window are all tested against a set built the way a
real one is built.

    python3 tests/fixtures/make-sample.py

Binds to 127.0.0.1 only, like everything else in tests/.
"""

import ftplib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from mock_webman import MockConsole
from ps3diag import report, runner
from ps3diag.analysis import analyse
from ps3diag.artefacts import ArtefactSet
from ps3diag.transport import FtpLister, HttpProbe


def main():
    with MockConsole() as console:
        def factory():
            ftp = ftplib.FTP()
            ftp.connect("127.0.0.1", console.ftp.port, timeout=10)
            ftp.login("anonymous", "ps3-diag@localhost")
            return ftp

        result = runner.run(
            "192.168.1.42",
            http=HttpProbe(f"127.0.0.1:{console.http.port}", timeout=5),
            ftp=FtpLister("127.0.0.1", 10, factory=factory),
            run_timeout=180)
    artefacts = ArtefactSet.from_run(result)
    outcome = analyse(artefacts)
    path, counts = report.write_zip(artefacts, HERE, outcome.findings,
                                    outcome.broken_rules,
                                    name="sample-diagnostic")
    print(f"wrote {path}")
    print(f"  {len(artefacts.files)} artefacts, "
          f"{len(outcome.findings)} findings, "
          f"{sum(counts.values())} identifiers redacted")


if __name__ == "__main__":
    main()
