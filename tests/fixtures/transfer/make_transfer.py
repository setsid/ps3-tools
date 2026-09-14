"""Disc images on disk for the transfer tests, with the names that broke it.

The images themselves come from tests/fixtures/iso, which already builds a PS3
bridge disc and a PS2 disc out of the specification longhand. Nothing is
duplicated here: this only writes those bytes into files, under the kinds of
names that come off a real dump, and pads them to a size worth measuring.

The names matter as much as the contents. "Gran Turismo 5 & Prologue,
Collector's Edition [BCES00569].iso" is the shape of the file that webMAN
silently skipped, and a test that only ever sees "game.iso" would not have
caught it.
"""

import os
import sys

_ISO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "iso")
if _ISO not in sys.path:
    sys.path.insert(0, _ISO)

import make_fixtures                                             # noqa: E402

#: The one that was skipped. Ampersand, comma, apostrophe, brackets and long.
AWKWARD_NAME = ("Gran Turismo 5 & Prologue, Collector's Edition "
                "[BCES00569] (Europe).iso")

#: Two dumps of the same game that differ only in the bracketed part, so they
#: shorten to the same remote name and collide.
COLLIDING_NAMES = ("Metal Gear Solid 4 [BLES00246].iso",
                   "Metal Gear Solid 4 [BLUS30109].iso")

#: A PS2 image whose filename claims it is a PS3 one. The folder it ends up in
#: must come from the bytes, not from this.
LYING_NAME = "Definitely A PS3 Game [PS3ISO].iso"


def _write(folder, name, blob):
    path = os.path.join(folder, name)
    with open(path, "wb") as handle:
        handle.write(blob)
    return path


def ps3(folder, name="game.iso", pad_to=None):
    """A PS3 disc image, identified by PS3_GAME/PARAM.SFO."""
    return _write(folder, name, make_fixtures.build_ps3_bridge(pad_to=pad_to))


def ps3_udf(folder, name="udf.iso", pad_to=None):
    return _write(folder, name, make_fixtures.build_ps3_udf(pad_to=pad_to))


def ps2(folder, name="ps2.iso", pad_to=None):
    """A PS2 disc image, identified by SYSTEM.CNF."""
    return _write(folder, name, make_fixtures.build_ps2(pad_to=pad_to))


def not_an_image(folder, name="holiday.iso", size=64 * 1024):
    """Something that is not a disc image at all."""
    return _write(folder, name, make_fixtures.build_not_an_iso(size=size))


def truncated(folder, name="cut-short.iso", keep=40 * 1024):
    """A PS3 image that stops before the part that says what it is."""
    return _write(folder, name, make_fixtures.build_truncated(keep=keep))
