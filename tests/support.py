"""Shared test helpers. Nothing here touches the network."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
# Fixture generators are importable by name. They build their data in memory
# rather than committing blobs, so a test that wants a disc image asks for one.
GENERATORS = os.path.join(FIXTURES, "iso")
for path in (ROOT, GENERATORS):
    if path not in sys.path:
        sys.path.insert(0, path)


def fixture(*parts):
    with open(os.path.join(FIXTURES, *parts), encoding="utf-8") as handle:
        return handle.read()


def fixture_path(*parts):
    return os.path.join(FIXTURES, *parts)


class FixtureCase(unittest.TestCase):
    def fixture(self, *parts):
        return fixture(*parts)
