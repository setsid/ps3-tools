# Screen interface

Published before the screens were written, and frozen. `ps3tools/shell/screen.py`
is the executable half; if the two disagree, this document is wrong.

A screen that believes the interface is wrong **stops and reports** rather than
editing it.

## Layering

```
ps3diag/            domain: collectors, parsers, rules, analysis. Read-only,
                    enforced in ps3diag/transport.py. No Qt, no patching.
ps3tools/titles.py  the verified title table. Facts, no behaviour.
ps3tools/patching/  the only code in the program that writes to a console.
ps3tools/shell/     application frame, launcher, navigation, theme, state.
ps3tools/screens/   one module per tool.
```

`ps3diag` must never import `ps3tools.patching`, directly or transitively.
`tests/test_layering.py` asserts it. The diagnostic path's promise is that no
code path in it can write; a stray import is how that stops being true without
anyone noticing.

The domain layers are UI-agnostic and are reused unchanged. **If a domain layer
needs changing to suit Qt, that is a layering bug: fix the layering.**

## Writing a screen

```python
from ps3tools.shell.registry import register
from ps3tools.shell.screen import Screen

@register
class MyScreen(Screen):
    key = "mytool"           # stable, used in navigation and settings
    title = "My tool"        # card heading
    blurb = "One sentence."  # card subtitle, a sentence not a slogan
    tile = "MT"              # two or three letters. Letters only, no emoji.
    order = 40               # card order, lowest first

    def on_enter(self): ...
```

That is the whole registration. The launcher enumerates `registry.screens()`
and nothing else, so a fourth card is a new module plus the decorator.

| Member | Meaning |
| --- | --- |
| `on_enter()` | Navigated to. A screen that scans on entry starts it here. |
| `on_leave()` | Navigated away. Cancel anything in flight. |
| `can_leave()` | Return `False` to refuse navigation, e.g. partway through writing a file. |
| `leave_blocked_reason()` | Optional. Why `can_leave()` said no, in words fit to show a user. The shell shows it verbatim after the screen's title. Override it whenever you override `can_leave()`: the screen knows what it is doing and the shell does not. |
| `busy_changed(bool)` | Emit while work is in flight. The shell shows it and blocks navigation. |
| `status_message(str)` | A short line for the shell's status area. |
| `request_home()` | Ask the shell to return to the launcher. |
| `self.services` | See below. |
| `self.connection`, `self.theme` | Shortcuts onto `services`. |

## Services

Everything a screen is given. Screens hold no global state of their own.

- `services.connection` — the shared `ConnectionState`
- `services.theme` — the shared `Theme`
- `services.settings` — a plain dict, persisted by the shell. **Keep it
  JSON-serialisable**: a value that will not serialise is dropped at the write,
  and a screen that stores a widget or a socket in here loses its settings
  silently rather than noisily.
- `services.submit(fn) -> Task` — run `fn(control)` on a worker

## Threading, and it is not negotiable

**No screen calls into `ps3diag` or `ps3tools.patching` on the GUI thread.**
Both talk to a console over a network that may be slow or gone, and a blocked
GUI thread is a frozen window.

```python
def work(control):
    for index, item in enumerate(items):
        if control.cancelled:
            return None
        control.progress((index, item))   # arrives on the GUI thread
    return result

task = self.submit(work)
task.progress.connect(self._on_progress)
task.finished.connect(self._on_done)     # the return value
task.failed.connect(self._on_error)      # a string, already fit to show
```

`finished`, `failed` and `progress` all arrive on the GUI thread. `task.cancel()`
asks; the worker stops at its next `control.cancelled` check. A cancelled task
emits `done` but not `finished`.

## ConnectionState

The console address and whether it can be reached. One instance, shared across
every screen, entered once.

| Member | Meaning |
| --- | --- |
| `host` / `set_host(str)` | The address. Setting a new one resets the verdict to `unknown`. |
| `connection` | `unknown` / `checking` / `connected` / `unreachable` |
| `connection_detail` | A sentence for the user |
| `connected` | Convenience boolean |
| `banner` | The `webMANftpd` greeting once seen |
| `set_connection(state, detail="", banner=None)` | |
| `scan` | `idle` / `scanning` / `found` / `none` / `failed` |
| `scan_detail`, `candidates` | |
| `set_scan(state, detail="", candidates=None)` | |
| `changed` | Signal, emitted on any change |

**Connection and scan are two different questions and must never be conflated.**
The old window would sit there saying "No PS3 found on this network" in red
while a collection from the typed address worked perfectly. The rule:

- `connection` describes the address in the box. It is the **only** thing the
  shell's status area may show.
- `scan` describes the last search of the subnet. It belongs **beside the Find
  button and nowhere else**, and never colours the overall state.

A scan that found nothing says nothing about an address the user typed by hand.

## Theme

Screens never name a colour. They ask for a token, and the shell answers for
whichever of light and dark is current. This is what keeps both legible without
every screen being checked twice.

```python
self.theme.colour("warn")     # -> a colour string
self.theme.dark               # -> bool
self.theme.changed            # -> signal, repaint on it
```

Tokens: `bg`, `surface`, `surface_alt`, `border`, `text`, `text_dim`, `accent`,
`accent_text`, `ok`, `warn`, `error`, `info`.

## House style

British English. **No emoji anywhere**, including tiles, status text and logs.
The audience has no technical knowledge: failure messages say what to check.
Comments explain why, not what.

---

# Appendix: detection API

Published alongside the screen interface because the patcher screen and the
detection module are written in parallel. `ps3tools/detect.py` implements
exactly this.

```python
from ps3tools import detect

report = detect.find_installations(lister)      # lister: an FtpLister
report.installations                            # list[Installation]
report.notes                                    # list[str], things worth saying
report.for_title("bo2")                         # installations of one title
```

`Installation`:

| Field | Meaning |
| --- | --- |
| `title_id` | e.g. `"BLES01717"` |
| `title_key` | `"bo2"` / `"mw3"`, or `None` when unrecognised |
| `name`, `short` | from `ps3tools.titles` |
| `region` | from the SKU table |
| `path` | `/dev_hdd0/game/BLES01717` |
| `usrdir` | `/dev_hdd0/game/BLES01717/USRDIR` |
| `state` | see below |
| `files` | `[{"name", "kind", "size", "modified"}]` from `USRDIR`, `[]` when absent |
| `expected` | the binaries `titles.py` says should be there |
| `missing` | expected names not present in `files` |
| `tu_version` | title update version if it could be read, else `None` |
| `tu_detail` | how that was worked out, or why it could not be |
| `config` | the `titles.TITLES` entry, or `None` |

`state` is one of:

| State | Meaning | What the screen says |
| --- | --- | --- |
| `ready` | `USRDIR` exists and holds the expected binaries | Proceed to scan |
| `no_update` | the title is present but there is no `USRDIR` | **Not an error.** The title update has not been installed. Tell the user to run the game once, let the update download, and come back. |
| `not_found` | no installation of this title | Say so plainly |
| `unknown_variant` | a Call of Duty title ID that is not in the known-good table | Refuse, and say why: the signing parameters for this SKU are not known and guessing them produces a binary that will not boot |

`find_installations` never raises. A console that stops answering leaves the
installations already found intact and records the reason in `notes`.
