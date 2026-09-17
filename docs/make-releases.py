#!/usr/bin/env python3
"""Write docs/tested-releases.md out of the tables the program itself uses.

Generated rather than kept by hand. The list of releases each fix recognises
lives in ps3tools/titles.py because that is what the program reads at run
time, and a second copy of it in a document is a copy that goes stale the
first time somebody adds a title ID and forgets. This reads that table, works
each region out of the title ID itself with ps3diag.regioncodes, and writes
the page. Run it after changing titles.py.

What it will not do is invent a test result. Every release is reported in one
of three states and the difference between them is the whole point of the
page:

  Confirmed on hardware   somebody patched this release, put it back on a
                          console and watched the game work. These are the
                          entries in a title's skus table, plus the build
                          named in its confirmed_on note below.

  Reference data recorded the update package hashes for this release have
                          been read off Sony's manifests, so the program can
                          say which title update is installed. It does not
                          mean the fix has been seen working on it.

  Recognised              the program knows this is the game and will attempt
                          the fix. Nobody has reported back on it. This is
                          most of the list and it is not a defect: whether the
                          fix suits a release is settled by trying to decrypt
                          one of its files, which costs nothing and cannot go
                          wrong quietly.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ps3diag import regioncodes                            # noqa: E402
from ps3tools import titles                                # noqa: E402

PAGE = os.path.join(HERE, "tested-releases.md")

#: The builds somebody has actually watched the fix work on, and the title
#: update each was on. Written here rather than derived, because it is a
#: statement about what a person did and there is nowhere else it lives.
#:
#: A title may have more than one. Black Ops has two, which is what makes the
#: claim about that fix finding its own patch site something more than a
#: claim: the two are different regions of the same game and the fix was not
#: told anything about either of them.
CONFIRMED = {
    "bo2": (("BLES01717", "1.19"),),
    "mw3": (("BLES01428", "1.24"),),
    "bo1": (("BLES01031", "1.13"), ("BLUS30591", "1.13")),
}

#: The order the titles appear in, which is the order of the cards on the
#: README rather than alphabetical.
ORDER = ("bo2", "mw3", "bo1")

#: Anything true about one release that the tables above cannot say, keyed by
#: title ID. These are facts about how a release is built rather than about
#: whether the fix works on it, so they do not belong in the status column.
PER_RELEASE = {
    "BLUS30591": (
        "Confirmed by OpenResty on a console running Evilnat with webMAN and "
        "no other VSH plugins, title update 1.13, process ID 01020200. Stats "
        "synced at sign-in. This is the second region confirmed for this fix "
        "and the first one it was not written against, which is the test that "
        "matters for a fix that finds its own patch site rather than being "
        "given an address."),
    "NPEB00756": (
        "This release is fake-signed, which scetool could not open at all. "
        "The program reads and writes fake-signed binaries itself now, so "
        "nothing extra has to be found on disk. What it cannot do is invent "
        "an NPDRM block: every fake-signed copy seen carries one that is "
        "entirely zero, which is what makes a console answer 8001000F, and "
        "the licence type, application type, content ID and CID_FN hash all "
        "have to come from the retail file it was built from. A copy in that "
        "state is reported as such rather than patched."),
}

#: The games with no fix, said once each so that somebody arriving from the
#: README card lands on something rather than on a missing anchor.
NO_FIX = (
    ("modern-warfare-2", "Modern Warfare 2",
     "Believed to be the same identity fault as Black Ops: an account made "
     "after Sony's 2018 change works out to a different player than the one "
     "the server holds. Nothing has been taken apart yet and no release has "
     "been looked at."),
    ("world-at-war", "World at War",
     "Lobbies are reported as failing to connect. Not investigated, and no "
     "release has been looked at."),
    ("modern-warfare", "Modern Warfare",
     "Lobbies are reported as failing to connect. Not investigated, and no "
     "release has been looked at."),
    ("ghosts", "Ghosts",
     "Connection problems are reported. Not investigated, and no release has "
     "been looked at."),
    ("advanced-warfare", "Advanced Warfare",
     "The online services for this title are reported to have ended, so "
     "there is nothing here a patch could put right. Out of scope."),
)

CONFIRMED_NOTE = "Confirmed on hardware"
REFERENCE_NOTE = "Reference data recorded"
RECOGNISED_NOTE = "Recognised, not reported on"


def region_of(title_id):
    """The region and the medium, worked out of the title ID itself."""
    found = regioncodes.describe_title_id(title_id)
    region = found.get("region") or "unknown"
    media = found.get("media")
    if media == "download":
        return region, "download"
    if media == "Blu-ray disc":
        return region, "disc"
    return region, media or "unknown"


def confirmed_builds(key):
    return CONFIRMED.get(key, ())


def state_of(key, title_id):
    """Which of the three things this release is, and nothing stronger."""
    if any(build == title_id for build, _update in confirmed_builds(key)):
        return CONFIRMED_NOTE
    if titles.sku_for(title_id):
        return REFERENCE_NOTE
    return RECOGNISED_NOTE


def updates_for(title_id):
    """The title updates whose package hash is recorded, as a sentence."""
    sku = titles.sku_for(title_id) or {}
    got = sorted(sku.get("updates", {}))
    return ", ".join(got) if got else ""


def table_for(key):
    """One release per row: the ID, where it sold, and what is known of it."""
    config = titles.TITLES[key]
    rows = ["| Title ID | Region | Media | Status | Update hashes |",
            "| --- | --- | --- | --- | --- |"]
    for title_id in sorted(config["title_ids"]):
        if title_id in titles.NOT_A_TITLE:
            continue
        region, media = region_of(title_id)
        state = state_of(key, title_id)
        shown = f"**{title_id}**" if state == CONFIRMED_NOTE else title_id
        rows.append(f"| {shown} | {region} | {media} | {state} | "
                    f"{updates_for(title_id) or 'none'} |")
    return rows


def tally(key):
    """How many releases are in each state, for the line above the table."""
    config = titles.TITLES[key]
    counted = {}
    for title_id in config["title_ids"]:
        if title_id in titles.NOT_A_TITLE:
            continue
        state = state_of(key, title_id)
        counted[state] = counted.get(state, 0) + 1
    return counted


def confirmed_sentence(key):
    """The builds somebody has watched this fix work on, as one sentence."""
    builds = confirmed_builds(key)
    said = " and on ".join(f"{build}, title update {update}"
                           for build, update in builds)
    closing = {1: "on that build"}.get(len(builds), "on each of them")
    if len(builds) == 2:
        closing = "on both"
    return (f"**Confirmed working on {said}.** Patched, written back, read "
            f"off the console again and booted, {closing}.")


def section_for(key):
    config = titles.TITLES[key]
    counted = tally(key)
    total = sum(counted.values())
    lines = [
        f"## {config['name']}",
        "",
        confirmed_sentence(key),
        "",
        config["symptom"],
        "",
        f"{total} release"
        + ("" if total == 1 else "s")
        + " of this game "
        + ("is" if total == 1 else "are")
        + " recognised. "
        + ", ".join(f"{count} {state.lower()}"
                    for state, count in sorted(counted.items()))
        + ".",
        "",
    ]
    lines += table_for(key)
    lines.append("")
    for title_id in sorted(config["title_ids"]):
        words = PER_RELEASE.get(title_id)
        if words:
            lines += [f"**{title_id}.** {words}", ""]
    if not config["skus"]:
        lines += [
            "> No update package hashes have been read for this title, so the "
            "program cannot say which title update is installed and does not "
            "check it before patching. That matters less here than it would "
            "elsewhere: this fix finds its own patch site in whatever build "
            "it is handed rather than trusting an address, so a build it was "
            "not written for is refused rather than patched wrongly.",
            "",
        ]
    return lines


def build():
    lines = [
        "# Which releases have been tested",
        "",
        "Every release this tool recognises, what region it sold in, and how "
        "much is actually known about it. Three states, and the difference "
        "between them is the point of the page.",
        "",
        f"- **{CONFIRMED_NOTE}** - somebody patched this release, put it "
        "back on a console and watched the game work.",
        f"- **{REFERENCE_NOTE}** - the update package hashes have been read "
        "off Sony's manifests, so the program can say which title update is "
        "installed. It does not mean the fix has been seen working on it.",
        f"- **{RECOGNISED_NOTE}** - the program knows this is the game and "
        "will attempt the fix. Nobody has reported back. This is most of the "
        "list and it is not a defect: whether the fix suits a release is "
        "settled by trying to decrypt one of its files, which costs nothing "
        "and cannot go wrong quietly. **If one fails to decrypt, that is "
        "worth reporting** - the message names the title ID so you can say "
        "which.",
        "",
        "This page is generated from the same table the program reads at run "
        "time, by `docs/make-releases.py`. Do not edit it by hand.",
        "",
        "---",
        "",
    ]
    for key in ORDER:
        lines += section_for(key)
        lines.append("---")
        lines.append("")
    lines += ["# Titles with no fix", ""]
    for anchor, name, words in NO_FIX:
        lines += [f"## {name}", "", words, "",
                  "No release of this title has been examined, so there is "
                  "nothing to list.", "", "---", ""]
    lines += [
        "Report a release that does not work, with its title ID, to "
        "setsid.research@proton.me or in "
        "[the Discord](https://discord.gg/PDrSPNgeNj).",
        "",
    ]
    return "\n".join(lines)


def main():
    with open(PAGE, "w", encoding="utf-8") as handle:
        handle.write(build())
    print("wrote %s" % PAGE)


if __name__ == "__main__":
    main()
