#!/usr/bin/env python3
"""Runs the whole suite under an audit hook that refuses anything off-loopback.

Not a unittest: it has to wrap the entire run, and a test cannot install an
audit hook over its own runner usefully. Run it the same way CI would, or
before a release.

    python3 tests/check-no-network.py

Exits non-zero if any test connected to, bound to, or resolved anything that is
not 127.0.0.1. There is a live console on this network and the whole test suite
is built to stay off it; this is the thing that proves it rather than asserting
it.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFENCES = []
ALLOWED = ("127.0.0.1", "::1", "localhost")


def hook(event, args):
    if event == "socket.getaddrinfo":
        host = args[0]
        if host and str(host) not in ALLOWED:
            OFFENCES.append(f"{event}: {host!r}")
        return
    if event in ("socket.connect", "socket.bind"):
        target = args[1]
        if isinstance(target, tuple) and target:
            host = str(target[0])
            if not (host in ALLOWED or host.startswith("127.")
                    or host in ("0.0.0.0", "")):
                OFFENCES.append(f"{event}: {target}")
        return
    if event == "urllib.Request":
        url = str(args[0])
        if not url.startswith("http://127.0.0.1"):
            OFFENCES.append(f"urllib.Request: {url}")


def main():
    sys.addaudithook(hook)
    os.chdir(ROOT)
    suite = unittest.TestLoader().discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print()
    print(f"tests run: {result.testsRun}  "
          f"failures: {len(result.failures)}  errors: {len(result.errors)}")
    print(f"non-loopback network operations: {len(OFFENCES)}")
    for item in sorted(set(OFFENCES))[:40]:
        print(f"    {item}")
    ok = result.wasSuccessful() and not OFFENCES
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
