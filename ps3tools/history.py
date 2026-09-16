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
        version="1.3.0",
        date="2026-09-16",
        summary="The Black Ops 1 stats fix, and a home screen that says which "
                "cards are the game fixes.",
        changes=(
            "The Black Ops 1 stats fix. Multiplayer opened at rank 1 every "
            "time on any PSN account made after late 2018, because the game "
            "works out who you are from your online ID and the server works "
            "it out from your account ID. The fix has the game use the "
            "account ID, and rank and experience are then kept. Watched "
            "working on a real console through backing out of a lobby, going "
            "back in, and a full restart.",
            "The Black Ops 1 fix finds everything it needs inside your own "
            "copy of the game rather than at written-down addresses. The "
            "disc and digital builds of that game are laid out differently, "
            "and one set of numbers could not have been right for both.",
            "The Black Ops 1 fix is tied to the PSN account that was signed "
            "in when it ran, and says so. Run it again after switching "
            "accounts. Where a console has more than one account on it, it "
            "asks which one rather than assuming the first.",
            "A console that has never signed in to PSN is told to do that "
            "once and come back, rather than being patched into a fix with "
            "nothing to read.",
            "The Black Ops 1 screen says that the map packs, not this fix, "
            "are what stops public matches being found, and what to do about "
            "it in the meantime.",
            "Black Ops 1 is marked Beta on its card.",
            "The three game fixes carry their titles in larger type, so the "
            "cards most people open are the ones the eye lands on first.",
            "Black Ops II and Modern Warfare 3 say on their cards that "
            "digital releases are not supported yet. Modern Warfare 3 also "
            "says it has been reported failing on HEN consoles and that this "
            "is being looked into.",
            "A long title on one of those cards wraps rather than being cut "
            "short with a full stop.",
        ),
    ),
    Release(
        version="1.2.3",
        date="2026-09-15",
        summary="A console read once, and said the same way on every screen.",
        changes=(
            "One walk of the console answers what is installed, what is a "
            "disc image and what is waiting in the packages folder, so every "
            "screen sees the same games.",
            "Transfer games names the games already installed on the console "
            "above the list, behind an arrow, and costs the queue with and "
            "without them.",
            "A disc image of a game the console already has arrives unticked. "
            "Tick it if you want the image as well.",
            "The box before a copy describes the files that are going and "
            "leaves out anything already decided against.",
            "Closing the window ends the program. It used to leave it "
            "running with nothing on screen while a worker waited on a "
            "console that had gone quiet.",
            "A file the patchers do not recognise now says where else to "
            "try, with a link to the manual sequence that can still do it.",
            "A card on the home screen for the Black Ops 1 stats fix, marked "
            "as under development, with a link to the Discord.",
            "The digital release of Black Ops II is recognised, so a console "
            "that plays that copy is patched rather than being told its title "
            "update has not been downloaded.",
            "A title folder holding a licence and no game is left out of the "
            "installed list and is named, so no update is offered for a game "
            "that is not there.",
            "A row you untick stays unticked when the console is read again.",
            "The scan progress counter no longer runs past its own total.",
            "Several Call of Duty installations this tool does not patch are "
            "named in one line rather than one line each.",
            "The Black Ops II advice says what is left to do when some of "
            "the files are already fixed.",
            "Every file of a title fits in the table on the patch screens.",
            "A package or a file you untick stays unticked when another file "
            "is added or the console is read again.",
        ),
    ),
    Release(
        version="1.2.2",
        date="2026-09-15",
        summary="What the console actually does, on the evidence of two of "
                "them.",
        changes=(
            "An install is confirmed when the game reports the version that "
            "was sent, as well as when the console deletes the package. One "
            "console installs an update and keeps the package, and that used "
            "to wait out the whole timeout and then report a success as "
            "unconfirmed.",
            "A package of the same name left over from an earlier attempt is "
            "removed before the new one is sent.",
            "A console that stops answering during an install is said "
            "plainly rather than waited on, and a part-copied package is "
            "taken off the console.",
            "The write connection is opened when the upload starts rather "
            "than held open through the download in front of it.",
            "A game Sony publishes nothing for takes its name and version "
            "from the console instead of showing a bare title ID.",
            "A disc image already identified is never opened again.",
            "The summary under the table is behind an arrow with a line "
            "saying what is in it.",
            "Black Ops II and Modern Warfare 3 judge each file on its own, "
            "so a console with one file already fixed and one the program "
            "does not recognise can still have the rest patched.",
            "An already-patched multiplayer binary reads as already fixed.",
            "Putting the originals back works file by file.",
            "A console with no title update installed is offered Game "
            "updates rather than being told to launch the game.",
            "Save data rows no longer report every folder as 512 bytes.",
            "An update that has been installed is asked about again rather "
            "than being remembered as the newest for the rest of the day.",
            "Transfer games says which of the images you have chosen are "
            "games already installed on the console, and leaves the decision "
            "with you.",
            "A file the patchers could not read off the console in full is "
            "said to be a copy that did not finish, rather than a file with "
            "something missing from it.",
        ),
    ),
    Release(
        version="1.2.1",
        date="2026-09-15",
        summary="Fixes and a tidier Game updates screen.",
        changes=(
            "The quick check no longer says it cannot tell what is installed "
            "for a game that simply has no update installed.",
            "One button for checking, with a tick box for checking "
            "everything instead of only what was behind.",
            "Tick or untick every game that has an update, in one go.",
            "Settings and remembered game lists are kept in your user folder "
            "rather than beside the program, and are moved there the first "
            "time this version runs.",
            "Consoles can be given names, so two PS3s on one network keep "
            "their own saved address and their own remembered games.",
            "A saved address is connected to when the program opens.",
            "Transferring a game the console already has is recognised and "
            "said, rather than copying the whole thing again.",
            "Installs are followed through on the console: each package is "
            "waited for, checked afterwards, and reported as installed only "
            "where that check answered.",
            "Bigger packages are given longer to install, and running out of "
            "time says the console may still be installing rather than "
            "reporting a failure.",
        ),
    ),
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
