<img src="logo.png" width="420" alt="PS3 Tools by setsid">

# PS3 Tools

[![latest release](https://img.shields.io/github/v/release/setsid/ps3-tools?label=latest&color=003791)](https://github.com/setsid/ps3-tools/releases/latest)
[![downloads](https://img.shields.io/github/downloads/setsid/ps3-tools/total?color=003791)](https://github.com/setsid/ps3-tools/releases)
[![licence](https://img.shields.io/badge/licence-MIT-003791)](LICENSE)
![platform](https://img.shields.io/badge/platform-PS3-003791)

Tools for a PS3 running custom firmware, in one Windows program. A diagnostic
that reads the console and writes everything a helper needs into one file; the
two Call of Duty PSN fixes, applied over the network rather than by hand; title
updates fetched from Sony at full speed; a package installer; a save data
backup; and a game transfer that resumes. It replaces `bo2-psn-fix.exe` and
`mw3-psn-fix.exe`.

## What you need

A PS3 running custom firmware with webMAN MOD, switched on, sitting at the main
menu rather than in a game, on the same network as the PC. The console's IP
address. Nothing else: there is nothing to install and nothing to configure.

The address is on the console under Settings, Network Settings, Settings and
Connection Status List, near the top. It is also at the top of the webMAN page
if you can already reach that from a browser.

Or press **Find my PS3** and it will look for the console itself, then connect
to whatever it finds. If you would rather type the address in, put it in the
box and press **Check IP**.

Once it is connected, **Check IP** becomes **Disconnect**, and Find is switched
off until you press it. That stops a search changing the address out from under
a tool that is halfway through something. Disconnecting sends
nothing to the console; it only stops this program treating the address as
live.

webMAN's FTP server needs to be switched on. Without it about half of what the
diagnostic collects is unavailable, and the patchers cannot work at all. The
report says so rather than failing quietly.

---

## Download and run

Get `ps3-tools-<version>.exe` from the
[latest release](https://github.com/setsid/ps3-tools/releases/latest). The
version is in the file name, so two of them can sit side by side and you can
tell which is which. Put it somewhere you can write to, such as your Desktop.
Not Program Files.

### Windows will warn you

It will say **"Windows protected your PC"** and offer only a **Don't run**
button. Click **More info**, then **Run anyway**.

That is SmartScreen saying the file is not signed with a paid code-signing
certificate and has not been downloaded enough times to have a reputation.
Some antivirus products flag it too: the program is packed into a single exe
that unpacks itself to a temporary folder and runs from there, which is a
pattern malware also uses.

Check it rather than taking that on trust. Every release publishes a sha256,
and a VirusTotal scan of that exact file is linked from the release notes:

```
certutil -hashfile ps3-tools-1.2.0.exe SHA256
```

If it does not match the hash on the release page, do not run it. The source is
this repository, so you or somebody you trust can read it and build it yourself.

---

## The diagnostic

Tick what you want, press **Collect diagnostics**, and it writes one zip to your
Desktop.

| | |
| --- | --- |
| System and firmware | Firmware version and build, custom firmware flavour, Cobra or HEN, syscall state, temperatures, clock speeds, fan, console model and the region it was sold in |
| Storage and devices | What is mounted, free and total space, how full, top level layout |
| Plugins | `boot_plugins.txt`, what is actually in the plugins folder, and anything listed that is not there |
| Crash reports | Everything under `/dev_hdd0/crash_report/`, contents included |
| Game inventory | Names, sizes, title IDs and region codes across every mounted device, and what is installed under `/dev_hdd0/game` |
| Network | What the console reports, and the address it was actually reached on |
| webMAN configuration | Version and the settings on its setup page |

It then works out what that means. Every check that fires produces a finding in
plain English with something to do about it: a USB drive the console can see but
cannot mount, game folders nested inside each other, split `.iso` sets with a
part missing, Cobra switched off while ISOs are present, a CPU or RSX above
80 C, two game managers both in `boot_plugins.txt`. It also reports whether the
Call of Duty fixes have been applied, and what the console looks like to PSN
before you sign in.

**It only reads.** It never writes, deletes, mounts, unmounts or powers the
console on or off. That is enforced rather than intended: requests are matched
against an allowlist, no URL ever carries a query string (webMAN takes its
instructions in the query string, so `/setup.ps3` is read and `/setup.ps3?fanc=0`
is refused before it leaves the PC), redirects are not followed, and over FTP
only `LIST` is used plus reads of a few small text files. The patcher is a
separate module with its own client and the diagnostic has no import path that
reaches it.

The IDPS, PSID, MAC address and any account identifiers are replaced with
placeholders before anything is written. The placeholder is derived from the
value, so the same console gives the same placeholder every time and two reports
can be compared, but it cannot be turned back into the original. There is a tick
box to include the real values; leave it off unless you have been asked.

**File, Open a saved diagnostic** loads a zip and runs the current checks against
it with no console present. Somebody can analyse a console they have no access
to, and checks added later can be run against a report collected months ago.

---

## The patchers

Both games freeze or drop you out because of a fault in the game binary, not
because of your account or your network.

| | Black Ops II | Modern Warfare 3 |
| --- | --- | --- |
| Symptom | Freezes while a PSN session is active | You reach a multiplayer lobby and are dropped back to the menu about a second later, on any account made after late 2018 |
| Files changed | `EBOOT.BIN`, `t6_ps3f.self`, `t6mp_ps3f.self` | `default_mp.self` |
| Left alone | | `default.self`, campaign and Spec Ops |
| Confirmed on | BLES01717, title update 1.19 | BLES01428, title update 1.24 |

Other releases are attempted rather than refused. See
[Which releases it will patch](#which-releases-it-will-patch).

Black Ops II carries the same binary in three files and **all three are done**.
Patching only the multiplayer one leaves campaign and zombies still freezing.

Open a patcher and it scans straight away. For each file it pulls the file down,
decrypts it, and reads the four bytes at the patch site: *patched*, *not
patched*, or *not recognised*. It is never inferred from a file size. A file it
does not recognise is not patched, and it says so rather than guessing.

Before anything is written, the originals are pulled to a dated folder on your
Desktop and verified against the console by size and hash. **No backup, no
patch.** Keep them: they are the only way back, and an original cannot be
rebuilt from a patched copy. After writing, each file is read back off the
console and confirmed.

### It checks your title update first

The patch offsets were confirmed against one specific update of each game. Apply
stays disabled until the installed version matches it, because patching a
different build of a binary is how an install stops starting.

If yours does not match, it says so and offers a route straight to **Game
updates** for that title. Update, come back, rescan, and Apply enables itself.

The check asks whether this is the build the offset was confirmed on. It does
not ask whether it is the newest Sony serves. Those happen to be the same
today, because 1.19 and 1.24 are the last updates either game received, and
only the first question protects you.

A version that could not be read does not block Apply. An unreadable version
is no evidence either way, and the scan still reads the actual bytes at the
patch site and refuses anything that does not fit.

> ### Restart the console after patching
>
> **Restart your PlayStation 3 before launching the game. The patched files will
> not take effect until you do, and the game will hang on launch if you try it
> first.**

The console caches something about the module it has already loaded, so the new
files are not picked up until it comes back up. A hang on the first launch after
patching is this. The patch itself is fine. The program says the same thing on screen when
it finishes. It does not offer a button to restart the console, because rebooting
is a write action and webMAN exposes it as a query-string command, which is the
one thing the read-only guarantee rests on not doing.

Each file is re-signed using the content ID, application type and key revision
read back off your own copy, rather than with a generic fake signature, so it
boots where the original did without needing syscalls left switched on.

---

## Game updates

Downloads title updates from Sony at full speed and puts them on the console,
instead of leaving it to fetch them at its own pace. On a real console the Black
Ops II update took over two minutes to download and about ninety seconds to
install. The same package pulls at around 18 MB/s to a PC, so the download half
becomes seconds. The install is console-side and is not made any faster.

The real gain is a console with a lot of games on it, where the alternative is
launching each one and waiting.

It scans what is installed, reads the version out of each game's `PARAM.SFO`,
asks Sony what the latest is, and shows you the two side by side. Nothing is
ticked to start with.

**Every download is checked against the sha1 Sony publishes for it, and a
mismatch stops everything before a single byte is uploaded.** That check is the
reason downloading from somebody else and writing the result to your console is
a reasonable thing to do at all.

It uploads to `/dev_hdd0/packages/` and then you install it from the console:
**Game, then Package Manager, then Install Package Files.** webMAN is asked to
start the install as well, but on the firmware this has been tried against
nothing happens when it is, so do not sit waiting for it.

Because webMAN's install command works on **everything in that folder**, the
folder is listed first and you are told if anything is already in there that
this program did not put there.

## Install packages

The same thing for `.pkg` files you already have: DLC, homebrew, anything on
your PC. Pick them, and they are uploaded the same way, and installed from
Package Manager on the console the same way.

**This one has nothing to verify against.** Game updates checks what it
downloads against a hash published by Sony. Here you chose the file, and there
is no authority to check it against. The screen says so. It reads the package
header to confirm the file really is a package and shows the title ID inside it,
which is worth having as a guard against picking the wrong file, but that is a
sanity check and not a safety guarantee. Only install packages you trust.

## Transfer games

Copies ISOs from your PC to the console, sorted into the right folder.

**It will not make your console faster.** Over FTP this runs at roughly 4 MB/s,
so a 36 GB disc image takes about two and a half hours and a large queue can run
overnight. What it does is make the wait predictable and survivable: you get the
estimate before you commit, real progress while it runs, and a transfer that
picks up where it stopped rather than starting again.

- **The platform is read out of the image itself.** The filename is never
  guessed from. A
  PS3 image goes to `PS3ISO` and a PS2 image to `PS2ISO`, and anything it cannot
  identify is handed to you to decide rather than filed somewhere wrong.
- **Files are renamed before upload.** Brackets, commas and ampersands come out.
  An ampersand in a filename is what caused a real transfer to silently skip a
  file, and long names list badly on the console. You are shown the new name
  before anything starts.
- **It shows what is already on the console**, and flags a queued file that
  looks like it is already there, so you do not spend an evening copying
  something you already have.
- **Free space is checked against the whole queue before it starts**, rather
  than filling the drive most of the way through.
- **Pause and stop work mid-file**, and a stopped transfer leaves something the
  next run resumes rather than something it mistakes for finished.

### Making it less slow

The bottleneck is mostly the console, so the gains here are modest and honest:

- **A wired network connection rather than Wi-Fi.** This is the one that makes a
  real difference.
- **A faster hard drive, or an SSD, in the console.** Helps somewhat.
- Leave the console on the main menu rather than running something, and stop
  your PC going to sleep partway through.

## Putting the originals back

The patcher backs the original files up to `Desktop\PS3 Tools backups\<TITLEID>
<date>` before it writes anything, and **Put the originals back**, next to Apply,
puts them where they came from.

It reads the manifest written alongside them, checks every file against the hash
recorded when the backup was taken, and refuses a backup that does not verify or
that came from a different title ID. It uploads, reads each file back off the
console, and compares. Then it rescans, so what you see afterwards is read off
the console rather than assumed.

**Restart the console after restoring, the same as after patching.** The
originals will not take effect until you do, and the game will hang on launch if
you try it first. This is the one people hit, because restoring is what you
reach for when something has already gone wrong.

Restoring is safe, and you can patch again afterwards.

## Back up save data

Reads your saves off the console and writes them to
`Desktop\PS3 Tools saves\<date>\` as a zip, with a manifest recording where
each file came from and its hash.

Pick everything, one user, or individual saves. Folder names carry the title ID,
so it shows real game names where it can work them out and the folder name where
it cannot.

**This is one way only. It copies saves off the console and cannot put them
back.** PS3 saves are often copy-protected and tied to the console or the
account they were made on, so restoring is not simply the reverse of copying.
Do not treat this as a two-way sync.

It only reads. It is worth doing before you let anything write to your console.

## Support

setsid.research@proton.me

---

## Credits

**webMAN MOD** — the homebrew on the console that all of this talks to. Not
bundled here. Everything the diagnostic reads and everything the patcher writes
goes through it.

**scetool**, by naehrwert. Decrypts and re-signs a game binary. Nothing else in
this program can do that. It is bundled in the built exe. This repository does
not carry a copy.

**PySide6 and Qt** — the window and the widgets, used as a dynamically linked
LGPL build.

**bjocampos** — ran an earlier version of the Black Ops II fix on his own console
and reported back that multiplayer was working while campaign and zombies still
froze. That is what turned up `t6_ps3f.self` being loaded and still unpatched.
Without that test it would have shipped fixing two thirds of the game. He has
kept testing against real hardware since, and most of what this gets right about
a real console was found that way.

---

## Building from source

Python 3.10 or newer.

```
pip install PySide6==6.8.1
pip install pyinstaller==6.22.3
```

`certs/scei-dnas-root-05.pem` is not in this repository and you have to supply
it. Sony's update endpoint presents a certificate from its own private CA,
which is in no public trust store and never will be, so the update check
verifies against that root specifically. See
[`certs/README.md`](certs/README.md) for how to capture it. Without it the
Game updates card says it cannot verify the connection and stops; it does not
fall back to an unverified one.

Both pins are exact, and the build refuses a different PyInstaller version
rather than producing an exe nobody can reproduce.

**`tools/scetool/` is not in this repository and you have to supply it.** It is
naehrwert's work, bundled into the built exe rather than redistributed here, the
same as in the two standalone patcher repos. Put `scetool.exe`, `zlib1.dll` and
the whole `data` folder at `tools/scetool/`. The keyset must carry key revision
0019, which is what both games are signed with; the build checks that before it
starts, and tells you where the files go if they are missing.

```
.\build-exe.ps1
```

`icon.ico` and `icon.png` are placeholders drawn by `make-icon.py`.

Run the tests with:

```
python3 -m unittest discover -s tests
python3 tests/check-no-network.py
```

Everything is offline. Tests run either against fixtures or against the mock
webMAN server in `tests/mock_webman.py`, which binds to `127.0.0.1` and nothing
else. The second command runs the whole suite under an audit hook and fails if
anything connects to, binds to or resolves anything off loopback. The mock
reproduces the console's quirks rather than an idealised FTP server, including
the non-ASCII byte webMAN puts in its status replies, which is what broke every
directory listing until it was found.

The version number is in `ps3diag/__init__.py` and is the only place it is
written down.

---

## Licence

MIT. See [LICENSE](LICENSE). That covers everything written here, including the
two patch scripts in `tools/patchers/`.

---

## Which releases it will patch

It will **attempt any released version of either game**, and tells you honestly
when it cannot read one, rather than refusing everything it has not seen before.

The mechanism is attempt-and-verify rather than a list of approved SKUs.
Decryption is
self-verifying: with the wrong key it fails outright rather than quietly
producing something wrong. So the tool tries, and what happens next is one of
three things:

- **It decrypts and the patch site is where it was verified to be.** Patched.
- **It decrypts but the site is somewhere else.** Reported as not recognised.
  It will not patch at an unverified offset.
- **It will not decrypt.** That release was signed with different parameters.
  It says so, names the title ID, and stops.

Re-signing needs no regional table: the content ID, application type, licence
type and key revision are all read back off your own file.

### Confirmed working on hardware

| | |
| --- | --- |
| Black Ops II | **BLES01717**, title update 1.19 |
| Modern Warfare 3 | **BLES01428**, title update 1.24 |

Patched, written back, read off the console again and booted. The patch offsets
come from these two, and any other release is checked against them.

### Reference data

`BLUS31011`, `BLES01718`, `BLUS31140`, `BLUS30838`. Stock file sizes and update
hashes are recorded for these, so the tool can tell you which title update you
are on.

### Recognised

Every other release below. The tool knows they are the game and will try. **If
one fails to decrypt, that is worth reporting** — it means that region needs
different parameters, and the message names the title ID so you can say which.

Some of these are demos or betas rather than the full game.

**Black Ops II**

```
BCKS10223  BCKS10232  BCUS91450
BLES01717  BLES01718  BLES01719  BLES01720
BLJM60548  BLJM60549  BLJM61109  BLJM61110  BLJM61230  BLJM61231
BLUS31011  BLUS31080  BLUS31140  BLUS31141  BLUS41005
NPEB01204  NPEB01205  NPEB01206  NPEB01207
NPUB31055  NPUB31056
```

**Modern Warfare 3**

```
BCKS10195
BLES01428  BLES01429  BLES01430  BLES01431  BLES01432  BLES01433  BLES01434
BLJM60404  BLJM60422  BLJM60534  BLJM60535  BLJM61111  BLJM61112
BLUS30838  BLUS30872  BLUS30887
NPEB00964  NPEB00965  NPEB00966  NPEB00967  NPEB00968  NPEB00977  NPEB00978
NPEB90450  NPEB90451
NPUB30787  NPUB30788
```

Title IDs from [SerialStation](https://serialstation.com).

## Known limitations

The filesystem of a device is not something webMAN reports, so anything in the
diagnostic depending on FAT32 versus NTFS is an inference and says so. Region
codes in the inventory come from file and folder names; a renamed folder has no
title ID in it, which is common and means nothing is wrong. On webMAN 1.47.48q
the console reports no network settings of its own on any page that answers, so
that section holds only the address it was reached on and the FTP greeting.

## Legal

Not affiliated with or endorsed by Activision, Treyarch or Sony. All trademarks
are the property of their respective owners.

Every reasonable step has been taken to make this software safe, but no
guarantee is given. Use it at your own risk. The app backs up the files it
modifies; keep your own backups as well.
