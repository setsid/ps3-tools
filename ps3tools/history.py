"""What changed in each release, so the About screen can say it.

Kept here rather than fetched, because the point of it is to answer "what am I
running and what is new" on a machine that may have no internet at all, and
because the answer for a build is fixed at the moment it is built. The release
pages on GitHub carry the same words.

Only what changed goes in. The install instructions, the sha256 of each exe and
the "restart the console after patching" notice are on the release pages and in
the README; repeating them once a release, forever, is how a list like this
turns back into a wall of text.

Newest first. VERSION in ps3tools/__init__.py is the one being run; a version
in here with no entry is simply a release nobody wrote notes for.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Release:
    """One published version."""

    version: str
    date: str                       #: ISO, the day it was published
    summary: str = ""
    changes: tuple = field(default_factory=tuple)


RELEASES = (
    Release(
        version="1.2.0",
        date="2026-09-15",
        summary="Packages now tell you the truth about installing, and the "
                "window has had a going over.",
        changes=(
            "Copying a package across no longer claims the console has been "
            "asked to install it. webMAN answers but does not act on it, so "
            "the screen says where the file is and how to install it from "
            "Package Manager on the console.",
            "A window on the first run, with a Find my PS3 button, for a "
            "start with no console saved.",
            "Buttons are coloured by what they do, so the one that writes to "
            "your console no longer looks like Back.",
            "A wordmark in the top left, and an app icon cut from it.",
            "About is four tabs rather than one long page, and has a list of "
            "what changed in each release.",
            "Look inside the disc images says which one it is reading and how "
            "far through it is, and afterwards says what it found and why "
            "anything else was left out.",
            "What is already on the console no longer counts \".\" and "
            "\"..\" as games.",
            "The theme button is an icon rather than a drop-down.",
        ),
    ),
    Release(
        version="1.1.0",
        date="2026-09-14",
        summary="Five new tools.",
        changes=(
            "Game updates: checks every game against Sony's list and fetches "
            "the ones that are behind, much faster than letting the console "
            "do it. Checked against Sony's checksum before anything is "
            "copied across.",
            "Install packages: copies PKGs from this PC to the console.",
            "Transfer games: copies disc images over, works out the right "
            "folder by reading the image, and resumes if it drops.",
            "Back up save data: copies saves off the console. It cannot put "
            "them back.",
            "Restore: the Black Ops II and Modern Warfare 3 screens can put "
            "your original files back, checked against the backup.",
            "The patchers will not run unless you are on the right title "
            "update, and point you at Game updates when you are not.",
            "The diagnostic recognises files this tool has already patched.",
            "Both patchers know every published release of each game, and "
            "say plainly when a regional one signs its files differently.",
        ),
    ),
    Release(
        version="1.0.1",
        date="2026-09-14",
        changes=(
            "Recognises every published release of both games rather than a "
            "handful, and says plainly when a regional release signs its "
            "files differently instead of refusing without explanation.",
            "Find my PS3 connects on its own when it finds one console.",
            "The update banner is a control again rather than flat page.",
        ),
    ),
    Release(
        version="1.0",
        date="2026-09-14",
        summary="Diagnostics and the two Call of Duty PSN fixes in one "
                "application.",
    ),
    Release(
        version="0.9",
        date="2026-09-14",
        summary="First build.",
    ),
)


def releases():
    """Every release, newest first."""
    return RELEASES


def for_version(version):
    """The Release matching a version string, or None.

    Compared as written. A build from a checkout between releases has a version
    nobody has published notes for, and that is not a fault worth a message.
    """
    wanted = (version or "").strip().lstrip("vV")
    for release in RELEASES:
        if release.version == wanted:
            return release
    return None
