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
        version="1.4.0",
        date="2026-09-17",
        summary="The program does its own decrypting and re-signing, and "
                "signs for the firmware your console is running.",
        changes=(
            "PS3 Tools no longer uses scetool. The decrypting, re-signing "
            "and fake signing are this program's own, written in Python, so "
            "there is no separate program to find, nothing for antivirus to "
            "object to, and the keys are looked up inside the program rather "
            "than beside whatever folder it was started from.",
            "Fake-signed binaries are read and written, so the digital "
            "releases patch the same way the disc ones do and no second tool "
            "has to be found. The cards no longer say digital releases are "
            "unsupported, because they are.",
            "The program reads which firmware your console is running off "
            "webMAN's own page and signs for it. A HEN console gets the file "
            "re-signed against the 3.55-era keyset, which is what the advice "
            "to resign to 3.55 means. A custom firmware console keeps its "
            "own key revision as before.",
            "Where the console does not say which firmware it is, the "
            "program asks rather than choosing. Signing for the wrong one "
            "gives a game that will not start.",
            "The two NPDRM hashes are worked out rather than copied, so a "
            "file rebuilt under a different name on the console carries the "
            "hash that name needs.",
            "A patch screen only ever offers releases of the game it fixes. "
            "The Black Ops 1 screen was offering Ghosts, and then Modern "
            "Warfare 2, to somebody who had Black Ops 1 installed.",
            "A release this program has not been tested on is offered with a "
            "warning rather than refused. The fix finds its own patch site, "
            "so an unfamiliar title ID is a reason to be careful and nothing "
            "more, and the site is what decides whether it fits.",
            "Where no copy of the game is found, the folder can be typed in "
            "rather than the search simply failing.",
            "Black Ops 1 knows eleven releases rather than four. Europe was "
            "issued as six discs, one for each language group, and only the "
            "English and French one was recognised.",
            "Black Ops 1 no longer carries a Beta badge on its card.",
            "Modern Warfare 3's card no longer says it is failing on HEN.",
            "Every file a patcher is about to change is checked for actually "
            "being that binary. A mod menu commonly puts a re-signed copy of "
            "the multiplayer binary in place of EBOOT.BIN, set to load an "
            "SPRX at boot. That file decrypts, and it is one of the game's "
            "binaries, and it is not the one that name is supposed to hold. "
            "Patched at the campaign site it would produce a file that "
            "starts and then fails once multiplayer does.",
            "A file like that is left alone and reported on its own row as "
            "replaced, naming what is actually in it. It is kept apart from "
            "not recognised, which says the bytes were strange rather than "
            "that the file is somebody else's work.",
            "Whether this accounts for the Modern Warfare 3 reports on HEN "
            "is not settled. A console that was failing there will now say "
            "plainly whether its files are what they should be, which is the "
            "first thing to rule out.",
            "A build nobody has seen is still reported as a build nobody has "
            "seen. The stronger claim is only made when the file holds "
            "another of that same game's binaries, which is a thing that can "
            "be named.",
            "The Black Ops 1 fix no longer refuses an account whose PSN name "
            "cannot be read out of np_cache.dat. The name is for the picker; "
            "the fix needs the account ID and nothing else. A console with "
            "the file plainly present was being told that no account had "
            "one.",
            "A file that will not come off the console, a file with no "
            "account ID in it, and no file at all are three separate "
            "messages. Only the last one tells you to sign in to PSN.",
            "The diagnostic collects /dev_hdd0/home and one level inside each "
            "user folder, so a report about the Black Ops 1 fix can be "
            "answered from the dump. Names, sizes and dates only; no file in "
            "there is opened.",
            "Modern Warfare 3 recognises the American build of "
            "default_mp.self at 7541328 bytes, so an American copy is named "
            "rather than reported as a size nobody has seen.",
            "The home screen and the four screens that ask for an address say "
            "less. The address hint no longer explains what an address looks "
            "like.",
        ),
    ),
    Release(
        version="1.3.2",
        date="2026-09-16",
        summary="Choosing which copy of a game to fix, and saying which one "
                "was fixed.",
        changes=(
            "Where more than one supported release of a game is installed, "
            "the patchers ask which copy to fix. They used to take whichever "
            "they found first, with no way to choose and no way to tell "
            "which one it had been. Nothing is selected in that box until "
            "you pick, and the copies you do not pick are left untouched.",
            "Every patch names the release it worked on, on the screen "
            "before it runs and in the result afterwards, including when "
            "only one copy is installed.",
            "The Black Ops 1 account question lists PSN online IDs rather "
            "than folder numbers. Nobody knows their own folder number.",
            "That question offers the account that signed in most recently "
            "first, and does not ask at all when only one account on the "
            "console can be used. The account it settled on is named on the "
            "screen either way.",
            "An account whose name cannot be read is still listed, as its "
            "folder number, rather than being left out of the list.",
            "Black Ops 1 is confirmed working on the American disc release, "
            "BLUS30591, as well as the European one. That is a region the "
            "fix was never written against, which is the test that matters "
            "for a fix that finds its own patch site.",
            "A file with nothing in it for a fix to change reads as not "
            "affected. Black Ops 1's EBOOT.BIN came up in red as not "
            "recognised, in a row whose own description already said it was "
            "unaffected.",
            "bjocampos and OpenResty are credited in the About screen and "
            "the README for the testing and the finding that this release "
            "and the last one rest on.",
        ),
    ),
    Release(
        version="1.3.1",
        date="2026-09-16",
        summary="Black Ops 1 patched the wrong folder on two of the three "
                "releases it says it supports.",
        changes=(
            "The Black Ops 1 fix reads the folder it looks for np_cache.dat "
            "in out of the game's own content ID. It used to be told the "
            "folder name, which is right on the European disc and wrong on "
            "the American and digital ones, and the fix then opened a path "
            "that was not there, quietly did nothing, and still reported as "
            "applied.",
            "A title that does not read as four letters and five digits is "
            "refused with a reason rather than written into a path.",
            "The copy of np_cache.dat the tool places and the path the fix "
            "opens are built by the same function, so they cannot come out "
            "different.",
            "The Black Ops 1 screen says to leave the fix alone unless your "
            "rank actually resets. Accounts made before late 2018 already "
            "work, and this fix would hand one of those an identity the "
            "server does not hold. Apply stays off until you confirm you are "
            "seeing resets.",
            "Every release of every title this tool recognises is listed in "
            "docs/tested-releases.md, with its region and what is actually "
            "known about it. The front page links to it from a card for each "
            "game, coloured by how far along that fix is.",
        ),
    ),
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
            "A card can carry a short warning about what is known to be "
            "wrong with a tool, in a box of its own beside the Open link.",
            "A file the patchers do not recognise no longer points somebody "
            "with a modified game at a page that will patch it anyway. Black "
            "Ops 1 says to put the stock files back, run this fix on those, "
            "and apply their own changes again afterwards.",
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
