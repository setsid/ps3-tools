# PS3 Tools

Three tools for a PS3 running custom firmware, in one Windows program: a
diagnostic that reads the console and writes everything a helper needs into one
file, and the two Call of Duty PSN fixes, applied to the console over the
network rather than by hand. It replaces `bo2-psn-fix.exe` and `mw3-psn-fix.exe`.

## What you need

A PS3 running custom firmware with webMAN MOD, switched on, sitting at the main
menu rather than in a game, on the same network as the PC. The console's IP
address. Nothing else: there is nothing to install and nothing to configure.

The address is on the console under Settings, Network Settings, Settings and
Connection Status List, near the top. It is also at the top of the webMAN page
if you can already reach that from a browser. Press **Find my PS3** and the
program will look for it itself.

webMAN's FTP server needs to be switched on. Without it about half of what the
diagnostic collects is unavailable, and the patchers cannot work at all. The
report says so rather than failing quietly.

---

## Download and run

Get `ps3-tools.exe` from the [latest release](https://github.com/setsid/ps3-tools/releases/latest).
Put it somewhere you can write to, such as your Desktop. Not Program Files.

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
certutil -hashfile ps3-tools.exe SHA256
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
| Title update | 1.19 | 1.24 |

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

> ### Restart the console after patching
>
> **Restart your PlayStation 3 before launching the game. The patched files will
> not take effect until you do, and the game will hang on launch if you try it
> first.**

The console caches something about the module it has already loaded, so the new
files are not picked up until it comes back up. A hang on the first launch after
patching is this, not a bad patch. The program says the same thing on screen when
it finishes. It does not offer a button to restart the console, because rebooting
is a write action and webMAN exposes it as a query-string command, which is the
one thing the read-only guarantee rests on not doing.

Each file is re-signed using the content ID, application type and key revision
read back off your own copy, rather than with a generic fake signature, so it
boots where the original did without needing syscalls left switched on.

---

## Support

setsid.research@proton.me

---

## Credits

**webMAN MOD** — the homebrew on the console that all of this talks to. Not
bundled here. Everything the diagnostic reads and everything the patcher writes
goes through it.

**scetool**, by naehrwert — decrypts and re-signs a game binary. Nothing else in
this program can do that. Bundled in the built exe, not in this repository.

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

## Known limitations

**It will only patch these:**

| Black Ops II | BLUS31011, BLES01717, BLES01718, BLUS31140 |
| --- | --- |
| **Modern Warfare 3** | BLUS30838, BLES01428 |

Anything else is refused rather than guessed at. Whether the klicensee is the
same across regional SKUs is not established, and re-signing a binary with
another region's signing parameters produces a file that will not boot. If you
have a SKU that is not listed, say so rather than forcing it.

The patch sites were verified against **Black Ops II title update 1.19** and
**Modern Warfare 3 title update 1.24**. On any other update the site the search
finds is checked against where it was confirmed to be, and a disagreement is
reported as not recognised rather than patched at an unverified offset.

The filesystem of a device is not something webMAN reports, so anything in the
diagnostic depending on FAT32 versus NTFS is an inference and says so. Region
codes in the inventory come from file and folder names; a renamed folder has no
title ID in it, which is common and means nothing is wrong. On webMAN 1.47.48q
the console reports no network settings of its own on any page that answers, so
that section holds only the address it was reached on and the FTP greeting.
