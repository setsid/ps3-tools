# Which releases have been tested

Every release this tool recognises, what region it sold in, and how much is actually known about it. Three states, and the difference between them is the point of the page.

- **Confirmed on hardware** - somebody patched this release, put it back on a console and watched the game work.
- **Reference data recorded** - the update package hashes have been read off Sony's manifests, so the program can say which title update is installed. It does not mean the fix has been seen working on it.
- **Recognised, not reported on** - the program knows this is the game and will attempt the fix. Nobody has reported back. This is most of the list and it is not a defect: whether the fix suits a release is settled by trying to decrypt one of its files, which costs nothing and cannot go wrong quietly. **If one fails to decrypt, that is worth reporting** - the message names the title ID so you can say which.

This page is generated from the same table the program reads at run time, by `docs/make-releases.py`. Do not edit it by hand.

---

## Call of Duty: Black Ops II

**Confirmed working on BLES01717, title update 1.19.** Patched, written back, read off the console again and booted.

The game freezes on PS3 while a PSN session is active. It is a logging fault in the game. Your connection is fine and your account has not been banned.

25 releases of this game are recognised. 1 confirmed on hardware, 21 recognised, not reported on, 3 reference data recorded.

| Title ID | Region | Media | Status | Update hashes |
| --- | --- | --- | --- | --- |
| BCKS10223 | Korea | disc | Recognised, not reported on | none |
| BCKS10232 | Korea | disc | Recognised, not reported on | none |
| BCUS91450 | North America | disc | Recognised, not reported on | none |
| **BLES01717** | Europe | disc | Confirmed on hardware | 1.09, 1.19 |
| BLES01718 | Europe | disc | Reference data recorded | 1.09, 1.19 |
| BLES01719 | Europe | disc | Recognised, not reported on | none |
| BLES01720 | Europe | disc | Recognised, not reported on | none |
| BLJM60548 | Japan | disc | Recognised, not reported on | none |
| BLJM60549 | Japan | disc | Recognised, not reported on | none |
| BLJM61109 | Japan | disc | Recognised, not reported on | none |
| BLJM61110 | Japan | disc | Recognised, not reported on | none |
| BLJM61230 | Japan | disc | Recognised, not reported on | none |
| BLJM61231 | Japan | disc | Recognised, not reported on | none |
| BLUS31011 | North America | disc | Reference data recorded | 1.09, 1.19 |
| BLUS31080 | North America | disc | Recognised, not reported on | none |
| BLUS31140 | North America | disc | Reference data recorded | 1.09, 1.19 |
| BLUS31141 | North America | disc | Recognised, not reported on | none |
| BLUS41005 | North America | disc | Recognised, not reported on | none |
| NPEB01204 | Europe | download | Recognised, not reported on | none |
| NPEB01205 | Europe | download | Recognised, not reported on | none |
| NPEB01206 | Europe | download | Recognised, not reported on | none |
| NPEB01207 | Europe | download | Recognised, not reported on | none |
| NPUB31054 | North America | download | Recognised, not reported on | none |
| NPUB31055 | North America | download | Recognised, not reported on | none |
| NPUB31056 | North America | download | Recognised, not reported on | none |

---

## Call of Duty: Modern Warfare 3

**Confirmed working on BLES01428, title update 1.24.** Patched, written back, read off the console again and booted.

You reach a multiplayer lobby and are dropped back to the menu about a second later, on any PSN account made after late 2018. Campaign and Spec Ops are unaffected.

28 releases of this game are recognised. 1 confirmed on hardware, 26 recognised, not reported on, 1 reference data recorded.

| Title ID | Region | Media | Status | Update hashes |
| --- | --- | --- | --- | --- |
| BCKS10195 | Korea | disc | Recognised, not reported on | none |
| **BLES01428** | Europe | disc | Confirmed on hardware | 1.24 |
| BLES01429 | Europe | disc | Recognised, not reported on | none |
| BLES01430 | Europe | disc | Recognised, not reported on | none |
| BLES01431 | Europe | disc | Recognised, not reported on | none |
| BLES01432 | Europe | disc | Recognised, not reported on | none |
| BLES01433 | Europe | disc | Recognised, not reported on | none |
| BLES01434 | Europe | disc | Recognised, not reported on | none |
| BLJM60404 | Japan | disc | Recognised, not reported on | none |
| BLJM60422 | Japan | disc | Recognised, not reported on | none |
| BLJM60534 | Japan | disc | Recognised, not reported on | none |
| BLJM60535 | Japan | disc | Recognised, not reported on | none |
| BLJM61111 | Japan | disc | Recognised, not reported on | none |
| BLJM61112 | Japan | disc | Recognised, not reported on | none |
| BLUS30838 | North America | disc | Reference data recorded | 1.24 |
| BLUS30872 | North America | disc | Recognised, not reported on | none |
| BLUS30887 | North America | disc | Recognised, not reported on | none |
| NPEB00964 | Europe | download | Recognised, not reported on | none |
| NPEB00965 | Europe | download | Recognised, not reported on | none |
| NPEB00966 | Europe | download | Recognised, not reported on | none |
| NPEB00967 | Europe | download | Recognised, not reported on | none |
| NPEB00968 | Europe | download | Recognised, not reported on | none |
| NPEB00977 | Europe | download | Recognised, not reported on | none |
| NPEB00978 | Europe | download | Recognised, not reported on | none |
| NPEB90450 | Europe | download | Recognised, not reported on | none |
| NPEB90451 | Europe | download | Recognised, not reported on | none |
| NPUB30787 | North America | download | Recognised, not reported on | none |
| NPUB30788 | North America | download | Recognised, not reported on | none |

---

## Call of Duty: Black Ops

**Confirmed working on BLES01031, title update 1.13.** Patched, written back, read off the console again and booted.

Multiplayer opens at rank 1 every time and nothing you do is kept, on any PSN account made after late 2018. The game asks the server about an identity the server has never held, so there is nothing to send back.

3 releases of this game are recognised. 1 confirmed on hardware, 2 recognised, not reported on.

| Title ID | Region | Media | Status | Update hashes |
| --- | --- | --- | --- | --- |
| **BLES01031** | Europe | disc | Confirmed on hardware | none |
| BLUS30591 | North America | disc | Recognised, not reported on | none |
| NPEB00756 | Europe | download | Recognised, not reported on | none |

**NPEB00756.** This release is fake-signed. scetool, which is what the program ships to decrypt with, cannot open a fake-signed file at all. The program falls back to TrueAncestor's unfself where a copy of it has been put in `tools/unfself`, and unfself is not redistributed here, so out of the box this release is recognised and then refused with a message saying why.

> No update package hashes have been read for this title, so the program cannot say which title update is installed and does not check it before patching. That matters less here than it would elsewhere: this fix finds its own patch site in whatever build it is handed rather than trusting an address, so a build it was not written for is refused rather than patched wrongly.

---

# Titles with no fix

## Modern Warfare 2

Believed to be the same identity fault as Black Ops: an account made after Sony's 2018 change works out to a different player than the one the server holds. Nothing has been taken apart yet and no release has been looked at.

No release of this title has been examined, so there is nothing to list.

---

## World at War

Lobbies are reported as failing to connect. Not investigated, and no release has been looked at.

No release of this title has been examined, so there is nothing to list.

---

## Modern Warfare

Lobbies are reported as failing to connect. Not investigated, and no release has been looked at.

No release of this title has been examined, so there is nothing to list.

---

## Ghosts

Connection problems are reported. Not investigated, and no release has been looked at.

No release of this title has been examined, so there is nothing to list.

---

## Advanced Warfare

The online services for this title are reported to have ended, so there is nothing here a patch could put right. Out of scope.

No release of this title has been examined, so there is nothing to list.

---

Report a release that does not work, with its title ID, to setsid.research@proton.me or in [the Discord](https://discord.gg/PDrSPNgeNj).
