"""summary.txt, manifest.json and the zip they go in.

summary.txt is the deliverable. The assumption behind the whole tool is that the
person who ends up reading this has been sent one file by someone who does not
know what any of it means, so the digest has to answer the obvious questions
without the reader unzipping anything: what console is this, what is it running,
what is on it, how hot is it, and what could not be collected.

Redaction happens here rather than in the collectors, at the single point where
text becomes a file. Anything written into the zip goes through it, which is
easier to be sure of than seven collectors each remembering.
"""

import csv
import io
import json
import os
import textwrap
import time
import zipfile

from . import APP_NAME, READ_ONLY_NOTICE, VERSION
from .findings import SEVERITIES, SEVERITY_WORDS, normalise
from .redaction import redact, summarise
from .regioncodes import PS3_REGIONS

STATUS_WORDS = {
    "ok": "collected",
    "partial": "partly collected",
    "failed": "not collected",
    "skipped": "skipped",
}

# Printed under the temperatures. A PS3 idles in the fifties and the thermal
# cutout is at 85, so the figure only means something next to those.
TEMPERATURE_NOTE = (
    "For reference: a healthy PS3 idles around 50-60 C and runs 65-75 C in a "
    "game. Sustained readings above 80 C mean the fan, the vents or the thermal "
    "paste need attention."
)

REGION_NOTE = (
    "PS3 game discs are not region locked, so a disc from any region will run. "
    "The region code still matters for game updates, add-on content, save data "
    "and trophies, which do have to match. PS2 and PSOne discs are region "
    "locked and a PAL PS2 title will not boot from an NTSC dump."
)


def timestamp_name(when=None):
    when = when or time.localtime()
    return time.strftime(f"{APP_NAME}-%Y-%m-%d-%H%M%S", when)


def _wrap(text, width=76, indent="  "):
    """Folded to a fixed width. summary.txt is read in Notepad as often as not,
    which does not wrap, so a long line is a line nobody reads the end of."""
    if not text:
        return []
    # Hyphens and long words are left alone so that a repository URL in a
    # suggested fix stays one clickable, copyable string.
    return textwrap.wrap(text, width=width, initial_indent=indent,
                         subsequent_indent=indent, break_on_hyphens=False,
                         break_long_words=False) or [indent + text]


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


class Line:
    """Two column layout used all through the digest."""

    def __init__(self, width=22):
        self.width = width
        self.rows = []

    def add(self, label, value):
        value = _fmt(value)
        if value != "":
            self.rows.append((label, value))
        return self

    def render(self, indent="  "):
        return [f"{indent}{label.ljust(self.width)}{value}"
                for label, value in self.rows]


def build_summary(artefacts, findings=(), broken_rules=()):
    """The digest, rendered from an ArtefactSet.

    Rendered from the set rather than from a live run on purpose: the same
    function produces the summary for a console sitting on the desk and for a
    zip somebody emailed, so re-analysing an old dump gives a real report rather
    than a second-class one.
    """
    out = []
    add = out.append

    add(f"{APP_NAME} summary")
    add("=" * len(f"{APP_NAME} summary"))
    add("")
    head = Line()
    head.add("Collected", artefacts.generated_local.replace("T", " "))
    head.add("Console", artefacts.host)
    head.add("Tool", f"{APP_NAME} {artefacts.tool_version or VERSION}")
    seconds = artefacts.duration_seconds or 0
    head.add("Run took", f"{seconds:.0f} second"
                         f"{'' if round(seconds) == 1 else 's'}")
    head.add("Categories", f"{len(artefacts.categories)} attempted")
    out.extend(head.render(""))
    add("")
    out.extend(_wrap(READ_ONLY_NOTICE.upper(), indent=""))
    add("")
    if artefacts.identifiers_included:
        add("IDENTIFIERS ARE INCLUDED IN THIS ZIP. It contains the console's "
            "IDPS, PSID,")
        add("MAC address and account identifiers, which uniquely identify this "
            "console.")
        add("Send it to one person you trust. Do not post it publicly.")
    else:
        add("Console identifiers are redacted. IDPS, PSID, MAC address and "
            "account IDs")
        add("have been replaced with placeholders such as [IDPS-1a2b3c4d]. The "
            "same console")
        add("produces the same placeholder every time, so runs can still be "
            "compared.")
    add("")

    # Findings lead. Somebody who reads only the first screen of this file
    # should already know whether anything is wrong.
    out.extend(_findings_section(findings, broken_rules))

    add("WHAT WAS COLLECTED")
    add("-" * 18)
    for entry in artefacts.categories:
        word = STATUS_WORDS.get(entry.get("status"), entry.get("status", ""))
        line = f"  {str(entry.get('title', entry.get('key'))).ljust(26)}{word}"
        if entry.get("error"):
            line += f" - {entry['error']}"
        add(line)
    add("")

    out.extend(_console_section(artefacts))
    out.extend(_temperature_section(artefacts))
    out.extend(_storage_section(artefacts))
    out.extend(_games_section(artefacts))
    out.extend(_iso_identity_section(artefacts))
    out.extend(_patch_section(artefacts))
    out.extend(_psn_section(artefacts))
    out.extend(_plugins_section(artefacts))
    out.extend(_crash_section(artefacts))
    out.extend(_accounts_section(artefacts))
    out.extend(_network_section(artefacts))
    out.extend(_webman_section(artefacts))
    out.extend(_problems_section(artefacts))
    out.extend(_endpoints_section(artefacts))
    out.extend(_files_section(artefacts))
    return "\n".join(out) + "\n"


def _findings_section(findings, broken_rules=()):
    """The part that answers the question rather than describing the console."""
    findings = [normalise(item) for item in findings or ()]
    out = _section("WHAT THIS LOOKS LIKE")
    if not findings:
        out += ["  Nothing stood out. Every check ps3-diag knows how to make "
                "came back clean.",
                "  That does not prove the console is healthy, only that none "
                "of the known",
                "  faults are present.", ""]
    else:
        counts = {name: sum(1 for item in findings if item.severity == name)
                  for name in SEVERITIES}
        parts = [f"{counts[name]} {SEVERITY_WORDS[name]}"
                 for name in SEVERITIES if counts[name]]
        out.append(f"  {', '.join(parts)}.")
        out.append("")
        for severity in SEVERITIES:
            for item in [entry for entry in findings
                         if entry.severity == severity]:
                heading = _wrap(f"[{SEVERITY_WORDS[severity].upper()}] "
                                f"{item.title}", indent="      ")
                heading[0] = "  " + heading[0].lstrip()
                out.extend(heading)
                out.extend(_wrap(item.explanation, indent="      "))
                if item.fix:
                    out.extend(_wrap(f"What to do: {item.fix}", indent="      "))
                for line in item.evidence[:6]:
                    folded = _wrap(line, indent="              ")
                    folded[0] = "        seen: " + folded[0].lstrip()
                    out.extend(folded)
                out.append("")
    if broken_rules:
        out.append("  Some checks could not run because of a fault in ps3-diag "
                   "itself:")
        for broken in broken_rules:
            rule_id = (broken.get("rule_id") if isinstance(broken, dict)
                       else getattr(broken, "rule_id", "?"))
            error = (broken.get("error") if isinstance(broken, dict)
                     else getattr(broken, "error", ""))
            out.append(f"    {rule_id}: {error}")
        out.append("")
    return out


def _iso_identity_section(artefacts):
    """Present only when the ISO identification pass ran."""
    data = artefacts.json("games/iso-identity.json")
    if not data or not data.get("isos"):
        return []
    rows = data["isos"]
    out = _section("WHAT THE DISC IMAGES ACTUALLY ARE")
    out.append("  Read out of the image itself rather than from the file name.")
    named = [row for row in rows if row.get("title_id")]
    out.append(f"  {len(named)} of {len(rows)} images were identified from "
               f"their contents.")
    out.append("")
    for row in rows:
        name = str(row.get("name", ""))[:44]
        if row.get("title_id"):
            detail = f"{row['title_id']}  {row.get('title', '')}"
            if row.get("app_version"):
                detail += f"  v{row['app_version']}"
        else:
            detail = f"not identified ({row.get('reason', 'unknown')})"
        line = f"  {name.ljust(46)}{detail}"
        out.extend([line] if len(line) <= 96 else _wrap(f"{name}: {detail}"))
        if row.get("name_mismatch"):
            out.append("        the file name does not match what is inside "
                       "the image")
    out.append("")
    return out


def _patch_section(artefacts):
    data = artefacts.json("patches/patch-state.json")
    if not data or not data.get("titles"):
        return []
    out = _section("KNOWN PSN FIXES")
    for title in data["titles"]:
        out.append(f"  {title.get('title', title.get('title_id', ''))} "
                   f"({title.get('title_id', '')})")
        out.extend(_wrap(f"at {title.get('location', 'an unknown location')}",
                         indent="    "))
        for binary in title.get("binaries", []):
            state = binary.get("state", "unknown")
            out.append(f"    {str(binary.get('name', '')).ljust(22)}{state}")
        if title.get("advice"):
            out.extend(_wrap(title["advice"], indent="      "))
    out.append("")
    return out


def _psn_section(artefacts):
    data = artefacts.json("psn/safety.json")
    if not data or not data.get("assessment"):
        return []
    assessment = data["assessment"]
    out = _section("SIGNING IN TO PSN: WHAT THIS CONSOLE LOOKS LIKE")
    out.extend(_wrap(assessment.get("statement", "")))
    out.append("")
    for observation in assessment.get("observations", []):
        folded = _wrap(observation, indent="    ")
        folded[0] = "  - " + folded[0].lstrip()
        out.extend(folded)
    if assessment.get("caveat"):
        out.append("")
        out.extend(_wrap(assessment["caveat"]))
    out.append("")
    return out


def _section(title):
    return [title, "-" * len(title)]


def _facts(artefacts, key):
    return artefacts.facts(key)


def _status(artefacts, key):
    return artefacts.status(key)


def _console_section(artefacts):
    facts = _facts(artefacts, "system")
    out = _section("CONSOLE")
    if not facts:
        out += ["  Nothing was collected for this category.", ""]
        return out
    line = Line()
    line.add("Model", facts.get("model"))
    line.add("Generation", facts.get("model_generation"))
    line.add("Sold in", facts.get("model_sales_region"))
    line.add("Shipped with", facts.get("model_shipped_capacity"))
    line.add("PS2 compatibility", facts.get("ps2_compatibility"))
    firmware = facts.get("firmware") or facts.get("firmware_release")
    if firmware:
        parts = [firmware]
        if facts.get("firmware_type"):
            parts.append(facts["firmware_type"])
        extra = []
        if facts.get("firmware_build"):
            extra.append(f"build {facts['firmware_build']}")
        if facts.get("system_sdk"):
            extra.append(f"SDK {facts['system_sdk']}")
        joined = " ".join(parts)
        if extra:
            joined += f" ({', '.join(extra)})"
        line.add("Firmware", joined)
    cfw = []
    if facts.get("cfw_name"):
        cfw.append(facts["cfw_name"])
    if facts.get("cobra_version"):
        cfw.append(f"Cobra {facts['cobra_version']}")
    if facts.get("hen_version"):
        cfw.append(f"HEN {facts['hen_version']}")
    if not cfw and facts.get("cfw_markers"):
        cfw = facts["cfw_markers"]
    line.add("Custom firmware", ", ".join(cfw))
    if facts.get("syscall_state"):
        number = facts.get("syscall_number") or "8"
        line.add(f"Syscall {number}", facts["syscall_state"])
    line.add("webMAN", facts.get("webman_version"))
    line.add("Console clock", facts.get("console_time"))
    line.add("Uptime", facts.get("uptime"))
    out += line.render()
    if facts.get("model_sales_region"):
        out += ["", "  The region above is where the console was sold. It is "
                    "not the same thing as",
                "  the firmware region, which is the CEX or DEX above, and "
                "neither one stops a",
                "  game from another region running. See REGION NOTES below."]
    out.append("")
    return out


def _temperature_section(artefacts):
    facts = _facts(artefacts, "system")
    has = any(key in facts for key in
              ("cpu_temp_c", "rsx_temp_c", "cpu_clock_mhz", "fan_speed_percent"))
    if not has:
        return []
    out = _section("TEMPERATURES, CLOCKS AND FAN")
    rows = []
    for label, temp_c, temp_f, clock in (
            ("CPU", "cpu_temp_c", "cpu_temp_f", "cpu_clock_mhz"),
            ("RSX", "rsx_temp_c", "rsx_temp_f", "rsx_clock_mhz")):
        if facts.get(temp_c) is None and facts.get(clock) is None:
            continue
        temperature = ""
        if facts.get(temp_c) is not None:
            temperature = f"{facts[temp_c]:g} C"
            if facts.get(temp_f) is not None:
                temperature += f" ({facts[temp_f]:g} F)"
        speed = f"{facts[clock]} MHz" if facts.get(clock) else ""
        rows.append(f"  {label.ljust(10)}{temperature.ljust(22)}{speed}")
    if facts.get("memory_clock_mhz"):
        rows.append(f"  {'Memory'.ljust(10)}{''.ljust(22)}"
                    f"{facts['memory_clock_mhz']} MHz")
    if facts.get("fan_speed_percent") is not None:
        mode = f" ({facts['fan_mode']})" if facts.get("fan_mode") else ""
        rows.append(f"  {'Fan'.ljust(10)}{facts['fan_speed_percent']}%{mode}")
    elif facts.get("fan_mode"):
        rows.append(f"  {'Fan'.ljust(10)}{facts['fan_mode']}")
    out += rows
    out += [""] + _wrap(TEMPERATURE_NOTE) + [""]
    return out


def _storage_section(artefacts):
    facts = _facts(artefacts, "storage")
    devices = facts.get("devices") or []
    out = _section("STORAGE")
    if not devices:
        out += ["  No mounted devices were reported.", ""]
        return out
    for device in devices:
        bits = [f"  {device['device'].ljust(14)}"]
        if device.get("free") and device.get("total"):
            bits.append(f"{device['free']} free of {device['total']}")
            if device.get("used_percent") is not None:
                bits.append(f" ({device['used_percent']:g}% used)")
        elif device.get("free"):
            bits.append(f"{device['free']} free")
        elif device.get("note"):
            bits.append(device["note"])
        else:
            bits.append("mounted, size not reported")
        out.append("".join(bits))
        if device.get("folders"):
            folders = ", ".join(device["folders"][:12])
            if len(device["folders"]) > 12:
                folders += f", and {len(device['folders']) - 12} more"
            out.extend(_wrap(f"top level: {folders}", indent=" " * 16))
    out.append("")
    return out


def _games_section(artefacts):
    facts = _facts(artefacts, "games")
    folders = facts.get("folders") or {}
    out = _section("GAME INVENTORY")
    if not folders:
        out += ["  No game folders were read.", ""]
        return out
    for name in sorted(folders):
        entry = folders[name]
        out.append(f"  {name.ljust(26)}{str(entry['count']).rjust(4)} items"
                   f"{entry['bytes_human'].rjust(12)}")
    out.append(f"  {'Total'.ljust(26)}{str(facts['total_items']).rjust(4)} "
               f"items{facts['total_human'].rjust(12)}")
    out.append("")
    out.append("  By region, across everything found:")
    regions = facts.get("regions") or {}
    for region in sorted(regions, key=lambda name: -regions[name]):
        standard = next((value[1] for value in PS3_REGIONS.values()
                         if value[0] == region), "")
        label = f"{region} ({standard})" if standard else region
        out.append(f"    {label.ljust(34)}{str(regions[region]).rjust(4)}")
    if facts.get("no_title_id"):
        out.append(f"    {'No title ID in the name'.ljust(34)}"
                   f"{str(facts['no_title_id']).rjust(4)}")
        out.extend(_wrap("Those are usually folders somebody renamed. It "
                         "does not mean anything is wrong.", indent="    "))
    out += ["",
            "  Every title with its size, title ID and region is in "
            "games/inventory.csv",
            "  in this zip, and the raw listings are in games/.", ""]
    out += _section("REGION NOTES")
    out += _wrap(REGION_NOTE) + [""]
    return out


def _plugins_section(artefacts):
    facts = _facts(artefacts, "plugins")
    out = _section("PLUGINS")
    if not facts:
        out += ["  Nothing was collected for this category.", ""]
        return out
    for name in ("boot_plugins.txt", "boot_plugins_nocobra.txt"):
        plugins = facts.get(name)
        if plugins is None:
            continue
        out.append(f"  {name}: {facts.get(f'{name}_enabled_count', 0)} enabled,"
                   f" {facts.get(f'{name}_disabled_count', 0)} commented out")
        for plugin in plugins:
            mark = "     " if plugin["enabled"] else "  off"
            out.append(f"  {mark} {plugin['path']}")
    installed = facts.get("installed_in_plugins_folder")
    if installed:
        out.append(f"  /dev_hdd0/plugins/ holds {len(installed)} entries:")
        for item in installed:
            out.append(f"        {item['name'].ljust(34)}"
                       f"{item['size_human'].rjust(10)}")
    if facts.get("listed_but_not_found"):
        out.append("")
        out.append("  Listed in boot_plugins.txt but not present on disk, so "
                   "not loading:")
        for path in facts["listed_but_not_found"]:
            out.append(f"        {path}")
    out.append("")
    return out


def _crash_section(artefacts):
    status = _status(artefacts, "crash_reports")
    facts = _facts(artefacts, "crash_reports")
    out = _section("CRASH REPORTS")
    if not facts and status in ("skipped", "absent"):
        out += ["  Nothing was collected for this category.", ""]
        return out
    count = facts.get("count")
    if count == 0:
        out += ["  None. /dev_hdd0/crash_report/ is empty, which is what you "
                "want to see.", ""]
        return out
    if count is None:
        out += ["  The crash report folder could not be read.", ""]
        return out
    out.append(f"  {count} crash report(s). "
               f"{facts.get('downloaded', 0)} included in full in this zip "
               f"under crash_reports/.")
    for item in facts.get("files", []):
        out.append(f"    {item['name'].ljust(34)}"
                   f"{item['size_human'].rjust(10)}   {item['modified']}")
    out.append("")
    return out


def _accounts_section(artefacts):
    """Who has a folder under /dev_hdd0/home, and who has an np_cache.dat.

    Printed rather than left in facts.json because it is the answer to a
    question somebody is asking while they read this file: the Black Ops 1 fix
    said no account had np_cache.dat, and this is the line that either agrees
    with it or shows it was wrong.

    Every line here says "account" where the console would say "user". The
    online ID rule in redaction.py treats a bare "user" as a label and replaces
    the word after it, so "2 user folders" reached the zip as "2 user
    [ONLINE-ID-eaabbc2b]s". Summaries are redacted on the way in and this
    section has to survive that, so it avoids the word.
    """
    status = _status(artefacts, "accounts")
    facts = _facts(artefacts, "accounts")
    out = _section("ACCOUNTS ON THIS CONSOLE")
    if not facts and status in ("skipped", "absent"):
        out += ["  Nothing was collected for this category.", ""]
        return out
    count = facts.get("user_count", 0)
    if not count:
        out += [f"  {facts.get('path', '/dev_hdd0/home')} holds no numbered "
                "account folder, so no account", "  has been set up.",
                ""]
        return out
    out.append(f"  {count} account folder(s), "
               f"{facts.get('with_np_cache', 0)} with an np_cache.dat.")
    users = facts.get("users") or []
    for user in users:
        if not user.get("listed"):
            out.append(f"    {user['folder']}   could not be listed")
            continue
        if user.get("has_np_cache"):
            state = (f"np_cache.dat, {user.get('np_cache_size', 0)} bytes, "
                     f"{user.get('np_cache_modified', '')}".rstrip(", "))
        else:
            state = "no np_cache.dat"
        out.append(f"    {user['folder']}   {user.get('entry_count', 0)} "
                   f"entries, {state}")
    for other in facts.get("other_entries") or []:
        out.append(f"    {other['name']} is not an account folder and was "
                   "not looked inside")
    out.append("")
    return out


def _network_section(artefacts):
    facts = _facts(artefacts, "network")
    out = _section("NETWORK, AS THE CONSOLE REPORTS IT")
    if not facts:
        out += ["  The console did not report its network settings.", ""]
        return out
    line = Line()
    for label, key in (("IP address", "ip_address"),
                       ("Subnet mask", "subnet_mask"),
                       ("Gateway", "gateway"),
                       ("Primary DNS", "dns_primary"),
                       ("Secondary DNS", "dns_secondary"),
                       ("MAC address", "mac_address"),
                       ("Connection", "connection"),
                       ("Link speed", "link_speed"),
                       ("MTU", "mtu"),
                       ("Proxy", "proxy")):
        line.add(label, facts.get(key))
    out += line.render()
    out.append("")
    return out


def _webman_section(artefacts):
    facts = _facts(artefacts, "webman_config")
    out = _section("WEBMAN CONFIGURATION")
    if not facts:
        out += ["  Nothing was collected for this category.", ""]
        return out
    if facts.get("webman_version"):
        out.append(f"  Version {facts['webman_version']}")
    settings = facts.get("settings") or {}
    if settings:
        out.append(f"  {len(settings)} settings read from the setup page:")
        for key in sorted(settings):
            out.append(f"    {key.ljust(20)}{settings[key]}")
    out.append("")
    return out


def _problems_section(artefacts):
    out = _section("WHAT DID NOT WORK")
    any_problem = False
    for entry in artefacts.categories:
        if entry.get("status") == "ok" and not entry.get("notes"):
            continue
        lines = []
        if entry.get("error"):
            lines.extend(_wrap(entry["error"], indent="    "))
        for note in entry.get("notes", []):
            lines.extend(_wrap(note, indent="    "))
        if not lines:
            continue
        any_problem = True
        out.append(f"  {entry.get('title', entry.get('key'))} "
                   f"({STATUS_WORDS.get(entry.get('status'))}):")
        out.extend(lines)
    if not any_problem:
        out.append("  Nothing. Every category was collected.")
    out.append("")
    return out


def _endpoints_section(artefacts):
    out = _section("ENDPOINTS TRIED")
    out.append("  Every address asked for, and what came back. A 404 here means "
               "that page does")
    out.append("  not exist on this webMAN version, which is normal and not a "
               "fault.")
    for attempt in artefacts.endpoints:
        if attempt.get("error"):
            outcome = f"no answer ({attempt['error']})"
        else:
            outcome = (f"HTTP {attempt.get('status')}, "
                       f"{attempt.get('bytes', 0):,} bytes")
            if attempt.get("content_type"):
                outcome += f", {attempt['content_type'].split(';')[0]}"
        out.append(f"  GET {str(attempt.get('path', '')).ljust(18)}{outcome}")
    out.append("")
    return out


ROOT_MEMBERS = ("summary.txt", "manifest.json")


def _files_section(artefacts):
    out = _section("FILES IN THIS ZIP")
    rest = [name for name in artefacts.names() if name not in ROOT_MEMBERS]
    for name in list(ROOT_MEMBERS) + rest:
        out.append(f"  {name}")
    out.append("")
    return out


def inventory_csv(artefacts):
    """Every title as a row. CSV because the names are full of commas and
    ampersands and a spreadsheet is where a long list belongs."""
    folders = artefacts.game_folders()
    if not folders:
        return ""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["device", "folder", "name", "kind", "size_bytes",
                     "size", "modified", "title_id", "platform", "region",
                     "video_standard", "media", "category", "category_letter",
                     "publisher_class"])
    for key in sorted(folders):
        entry = folders[key]
        for row in entry.get("entries", []):
            writer.writerow([
                entry.get("device", ""), entry.get("folder", ""),
                row.get("name", ""), row.get("kind", ""), row.get("size", ""),
                row.get("size_human", ""), row.get("modified", ""),
                row.get("title_id") or "", row.get("platform") or "",
                row.get("region") or "", row.get("video_standard") or "",
                row.get("media") or "", row.get("category") or "",
                row.get("category_letter") or "",
                row.get("publisher_class") or "",
            ])
    return buffer.getvalue()


def build_manifest(artefacts, findings, broken_rules, redaction_counts,
                   file_names):
    manifest = dict(artefacts.manifest)
    manifest["files"] = list(file_names)
    manifest["redaction_counts"] = redaction_counts
    manifest["findings_count"] = {
        name: sum(1 for item in findings if normalise(item).severity == name)
        for name in SEVERITIES
    }
    manifest["broken_rules"] = list(broken_rules)
    return manifest


def findings_payload(findings, broken_rules):
    return json.dumps({
        "schema_version": 1,
        "findings": [normalise(item).to_dict() for item in findings],
        "broken_rules": list(broken_rules),
    }, indent=2, sort_keys=True, default=str)


def write_zip(artefacts, directory, findings=(), broken_rules=(), name=None,
              include_identifiers=None):
    """Builds the zip and returns its path.

    Everything written goes through redact() on the way in, including the
    summary and the manifest, so there is one place to be sure about rather
    than several.
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{name or timestamp_name()}.zip")
    if include_identifiers is None:
        include_identifiers = artefacts.identifiers_included
    found = {}

    def clean(text):
        result, hits = redact(text, include_identifiers)
        found.update(hits)
        return result

    payload = dict(artefacts.files)
    payload.pop("summary.txt", None)
    payload.pop("manifest.json", None)
    csv_text = inventory_csv(artefacts)
    if csv_text:
        payload["games/inventory.csv"] = csv_text
    payload["analysis/findings.json"] = findings_payload(findings,
                                                         broken_rules)

    file_names = ["summary.txt", "manifest.json"] + sorted(payload)
    cleaned = {name: clean(text) for name, text in payload.items()}
    summary = clean(build_summary(artefacts, findings, broken_rules))
    manifest = build_manifest(artefacts, findings, broken_rules,
                              summarise(found), file_names)
    manifest_text = clean(json.dumps(manifest, indent=2, sort_keys=True,
                                     default=str))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.txt", summary)
        archive.writestr("manifest.json", manifest_text)
        for artefact_name in sorted(cleaned):
            archive.writestr(artefact_name, cleaned[artefact_name])
    return path, summarise(found)
