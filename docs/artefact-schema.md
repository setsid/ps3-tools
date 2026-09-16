# Artefact schema

This is the contract between the three layers. It is frozen. Anything written
against it can assume it will not move underneath them.

    Collectors  ->  ArtefactSet  ->  Analysis  ->  Presentation

- **Collectors** talk to the console and produce artefacts. They do not
  interpret. A collector never decides whether something is a problem.
- **Analysis** is pure functions over an `ArtefactSet`. No sockets, no file
  system, no clock-dependent behaviour. Given the same set it returns the same
  findings.
- **Presentation** renders an `ArtefactSet` and a list of findings. It decides
  nothing.

The reason for the split: an `ArtefactSet` built from a live console and one
loaded from a zip a stranger emailed are the same object. Every rule therefore
works on both, today's rules can be run against last month's dump, and a helper
can analyse a console they have no access to.

`ps3diag/artefacts.py` is the executable half of this document. If the two
disagree, this document is wrong and the code is right.

## Rules that apply to everything

1. **Every artefact is UTF-8 text.** No binary members. A collector holding
   binary data records a hex or base64 rendering of it, with the encoding named
   in the file. This keeps a zip readable by a person and loadable with nothing
   but the standard library.
2. **Paths inside the set are `category/name`**, forward slashes, no leading
   slash. `summary.txt` and `manifest.json` are the only members at the root.
3. **Absent is not empty.** `set.status(key)` returning `absent` means the
   category was never attempted. `failed` means it was attempted and did not
   work. Neither is the same as a category that ran and found nothing. Rules
   call `set.collected(key)` before drawing any conclusion from a category.
4. **Nothing in analysis may raise.** A rule that throws is reported as a broken
   rule; it does not end the run.
5. **Reading bytes out of a game file is a ranged read, not a download.**
   `transport.may_download` governs whole files and refuses disc images.
   `transport.may_range_read` governs the few aligned blocks that ISO
   identification pulls before aborting the transfer. They are separate
   permissions and the second must never be widened into the first.
6. **Redaction happens on the way into the zip**, once, in `report.py`. Nothing
   upstream of that needs to think about it. Analysis therefore sees redacted
   placeholders such as `[IDPS-1a2b3c4d]` when it runs against a saved zip, and
   raw values when it runs in-process against a live run. No rule may depend on
   the value of an identifier, only on its presence.

## `manifest.json`

```json
{
  "schema_version": 1,
  "tool": "ps3-diag",
  "tool_version": "1.0",
  "generated_local": "2026-09-14T14:32:10",
  "generated_utc": "2026-09-14T13:32:10Z",
  "target": "192.168.1.42",
  "duration_seconds": 48.2,
  "read_only": true,
  "identifiers_included": false,
  "requested_categories": ["system", "storage", "..."],
  "categories": [
    {
      "key": "system",
      "title": "System and firmware",
      "status": "ok | partial | failed | skipped",
      "seconds": 4.1,
      "error": null,
      "notes": ["..."],
      "artefacts": ["system/root.html", "system/cpursx.ps3.html"]
    }
  ],
  "endpoints": [
    {"path": "/cpursx.ps3", "status": 200, "bytes": 301,
     "content_type": "text/html", "seconds": 0.2, "error": null}
  ]
}
```

## `ArtefactSet` API

Everything analysis is allowed to use. Nothing else is stable.

| Call | Returns |
| --- | --- |
| `set.host` | target IP as a string |
| `set.tool_version`, `set.generated_local`, `set.generated_utc` | strings |
| `set.duration_seconds` | number |
| `set.identifiers_included` | bool |
| `set.categories` | list of the manifest category records |
| `set.category(key)` | one category record, or `{}` |
| `set.status(key)` | `ok`/`partial`/`failed`/`skipped`/`absent` |
| `set.collected(key)` | `True` for `ok` or `partial` |
| `set.notes(key)`, `set.error(key)` | collector-recorded text |
| `set.facts(key)` | parsed `category/facts.json`, or `{}` |
| `set.text(name, default=None)` | one artefact as text |
| `set.json(name, default=None)` | one artefact parsed as JSON |
| `set.names(pattern="*")` | sorted artefact names matching an fnmatch pattern |
| `set.put(name, text)` | analysis writing its own output back into the set |
| `set.devices()` | `storage` facts `devices` list, or `[]` |
| `set.game_folders()` | `games` facts `folders` mapping, or `{}` |
| `set.game_entries()` | every title row, each carrying `device`, `folder`, `folder_key` |
| `set.boot_plugins()` | `plugins` facts `boot_plugins.txt` list, or `[]` |

## Category facts

Every field is optional. A parser that could not find a value omits the key
rather than guessing, so `facts.get("x")` returning `None` means "not known",
never "zero".

### `system/facts.json`

`webman_version`, `firmware` (`"4.93"`), `firmware_type` (`CEX`/`DEX`/`DECR`),
`firmware_release`, `firmware_build`, `system_sdk`, `firmware_flags`,
`cfw_name`, `cfw_markers` (list), `cobra_version`, `hen_version`,
`ftp_banner`, `ftpd_version`, `ntfs_mounts`,
`syscall_state` (`enabled`/`disabled`/...), `syscall_number`, `uptime`,
`console_time`, `cpu_temp_c`, `rsx_temp_c`, `cpu_temp_f`, `rsx_temp_f`,
`cpu_clock_mhz`, `rsx_clock_mhz`, `memory_clock_mhz`, `fan_speed_percent`,
`fan_mode`, `model`, `model_generation`, `model_sales_region`,
`model_shipped_capacity`, `ps2_compatibility`.

### `storage/facts.json`

```json
{"devices": [
  {"device": "dev_hdd0", "free_bytes": 443133250764, "free": "412.7 GB",
   "total_bytes": 1000190509056, "total": "931.5 GB", "used_bytes": 0,
   "used": "518.8 GB", "used_percent": 55.7, "present_over_ftp": true,
   "top_level_entries": 14, "folders": ["GAMES", "PS3ISO"],
   "note": "no disc or not mounted"}
]}
```

`device` is always present. Everything else may be missing. **The filesystem of
a device is not reported by webMAN and is not in this schema** — a rule that
needs to know FAT32 from NTFS infers it and says that it inferred it.

### `games/facts.json`

```json
{"folders": {"dev_hdd0/PS3ISO": {
    "device": "dev_hdd0", "folder": "PS3ISO", "count": 10,
    "bytes": 0, "bytes_human": "241.3 GB",
    "regions": {"Europe": 8}, "no_title_id": 1,
    "entries": [{
      "name": "Gran Turismo 5 & Prologue [BCES00569].iso",
      "kind": "file | directory | link", "size": 44023414784,
      "size_human": "41.0 GB", "modified": "Jul 19 22:41",
      "title_id": "BCES00569", "region": "Europe", "video_standard": "PAL",
      "platform": "PS3 disc", "media": "Blu-ray disc",
      "category": "retail game", "category_letter": "S",
      "publisher_class": "Sony published"}]}},
 "total_items": 0, "total_bytes": 0, "total_human": "0 B",
 "regions": {}, "no_title_id": 0,
 "installed_titles": [
   {"title_id": "BLES01428", "path": "/dev_hdd0/game/BLES01428",
    "files": [{"name": "default_mp.self", "kind": "file",
               "size": 7581072, "modified": "Sep 13 10:39"}]}],
 "images_identified": 0}
```

`installed_titles` is what is under `/dev_hdd0/game`, which is a different shape
from the eight disc-image folders: installed titles, with a listing of each
`USRDIR`. It is what makes the patch state of a known fix answerable at all.
`images_identified` is how many disc images were identified from their contents
rather than from their file names. It is `0` unless the run opted in: reading
inside the images is a tick box under Game inventory and is **off by default**,
because each image costs its own connection and a console with a couple of dozen
of them adds a minute or more. `games/iso-identity.json` is absent on a run that
did not opt in, which is not the same as a run where nothing could be
identified.

`title_id` and everything derived from it are `null` when the name carried no
recognisable ID. That is common and does not mean anything is wrong.

### `plugins/facts.json`

`boot_plugins.txt` and `boot_plugins_nocobra.txt` are lists of
`{"line": 2, "path": "/dev_hdd0/plugins/webftp_server.sprx", "enabled": true,
"name": "webftp_server.sprx"}`. Alongside them:
`boot_plugins.txt_enabled_count`, `boot_plugins.txt_disabled_count`,
`installed_in_plugins_folder` (list of `{name, size, size_human, kind}`),
`named_on_web_page` (list of sprx names seen on the root page),
`listed_but_not_found` (list of paths).

### `crash_reports/facts.json`

`count`, `downloaded`, `files` as `{name, size, size_human, modified}`.
The reports themselves are at `crash_reports/<name>`.

### `accounts/facts.json`

```json
{"path": "/dev_hdd0/home",
 "user_count": 2, "with_np_cache": 1,
 "users": [
   {"folder": "00000001", "path": "/dev_hdd0/home/00000001",
    "listed": true, "entry_count": 5, "has_np_cache": true,
    "np_cache_size": 2320, "np_cache_modified": "Sep 14 14:22",
    "entries": [{"name": "np_cache.dat", "kind": "file", "size": 2320,
                 "modified": "Sep 14 14:22"}]}],
 "other_entries": [{"name": "vsh", "kind": "directory"}]}
```

One level down from `/dev_hdd0/home` and no further. A user folder is eight
digits, which is how the console names them; `other_entries` is everything else
under that folder, recorded as seen and never listed. The raw listings are at
`accounts/listing.txt` and `accounts/listing-<folder>.txt`.

`user_count` is how many numbered folders the home listing showed and `users`
is the ones that were then looked inside. The two differ only when the request
budget or a stop cut the walk short, and anything counting accounts wants the
first of them.

`listed` is `false` for a folder that would not list, and such a folder carries
no `has_np_cache` at all. **Absent there means "could not look", never "the
file is not there"**: reporting the second as the first is what left a user
being told no account had an `np_cache.dat` that was sitting in front of them.

Everything this category writes into the zip says "account" where the console
would say "user". The online ID rule in `redaction.py` treats a bare `user` as
a label and replaces the word after it, so a heading of `USER ACCOUNTS` came
out as `USER [ONLINE-ID-95cca5a0]`. Anything added here wants the same care.

**No file under `/dev_hdd0/home` is ever opened.** `np_cache.dat` carries the
account ID and the online ID and `localusername` carries the name the console
shows for a local user, so this category holds names, sizes and dates and
nothing else. The transport allowlist refuses both files as well.

### `network/facts.json`

`ip_address`, `subnet_mask`, `gateway`, `dns_primary`, `dns_secondary`,
`mac_address`, `connection`, `link_speed`, `mtu`, `proxy`, `reached_at`,
`ftp_banner`, `ftp_reachable`.

webMAN 1.47.48q reports none of the first group on any page that answers, so on
that version this category holds only the last three: the address the tool
actually reached the console on, and the FTP greeting. That is a limitation of
the console's web pages rather than a failure, and the collector says so.

`ntfs_mounts` in `system` comes from the `webMANftpd` greeting and is the only
direct statement the console makes about NTFS anywhere. Every other mention of
NTFS in this tool is inference.

### `webman_config/facts.json`

`webman_version`, `settings` (flat string-to-string mapping as read off the
setup form), `settings_count`.

## Reserved artefact names

One writer each, named below. Nothing else writes them.

`games/iso-identity.json` is the exception to the layering: identifying a disc
image needs the console, so it is produced by the games collector rather than by
analysis. It is listed here because it has a single owner and a fixed shape, and
because rules read it.

| Name | Owner | Contents |
| --- | --- | --- |
| `analysis/findings.json` | rules engine | `{"schema_version": 1, "findings": [...], "broken_rules": [...]}` |
| `games/iso-identity.json` | ISO identification | `{"schema_version": 1, "isos": [...]}` |
| `patches/patch-state.json` | patch-state detection | `{"schema_version": 1, "titles": [...]}` |

Each title in `patches/patch-state.json` carries an `update_state`:

| Value | Meaning |
| --- | --- |
| `installed` | The title update is installed, so the binaries exist and were checked. |
| `no_update` | The game is present but its title update has not been downloaded. The patch targets live in `/dev_hdd0/game/<TITLEID>/USRDIR/`, which only exists once the update has installed, so there is nothing to patch yet. **Not an error**, and no per-file rows are reported: claiming a state for files that are not there is how this went wrong before. |
| `unknown` | `installed_titles` was not collected, so nothing can be said either way. Distinct from `no_update`, and collapsing the two is the bug that made a diagnostic report an ISO as though it were a folder. |

Binaries are read from the `games` facts' `installed_titles` and from nowhere
else. **A path is never constructed from an inventory row**: an inventory entry
may be an `.iso`, which is a file, and appending a binary name to it produces a
path that cannot exist.
| `psn/safety.json` | PSN safety check | `{"schema_version": 1, "assessment": {...}}` |

## Finding

The single currency of the analysis layer. Produced by rules, consumed by the
UI and by `summary.txt`.

```python
Finding(
    rule_id="usb-not-mounted",       # stable, kebab-case, unique
    severity="error",                # "error" | "warn" | "info"
    title="USB drive is plugged in but the console cannot see it",
    explanation="...",               # plain English, no jargon, 1-3 sentences
    fix="...",                       # what to actually do, or "" if nothing
    evidence=["dev_usb000 ..."],     # short strings quoting what was seen
    category="storage",              # which artefact category it came from
)
```

- `severity` is a judgement about the console, not about confidence.
  `error` means something is broken or unsafe. `warn` means it will bite later
  or is probably not what was wanted. `info` is worth knowing and needs no
  action.
- `explanation` is read by someone who does not know what a syscall is.
- `fix` is an instruction, not a description. Empty when there is nothing to do.
- A rule returning no findings is the normal case and is not reported.

## Rules engine entry point

```python
from ps3diag.rules import run_rules
outcome = run_rules(artefact_set)
outcome.findings      # list[Finding], sorted error, warn, info
outcome.broken_rules  # list[{"rule_id", "error"}] for rules that raised
```

`run_rules` never raises. A rule that throws lands in `broken_rules` with its
exception text and the rest of the run continues.
