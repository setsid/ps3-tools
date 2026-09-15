"""What this console looks like to PSN, before anybody signs in.

This is the one part of the tool where being wrong in either direction is
expensive: told it is fine, somebody loses an account; told it is hopeless,
somebody throws away a working console. So this module reports and does not
rate. There is no score, no traffic light and no verdict, because a number
would imply a precision that nothing here has. It states what was observable,
names the things that are known to be observable by somebody else, says plainly
what it could not see, and leaves the decision with the person reading it.

Three limits are structural rather than fixable, and every caller has to carry
them: this tool sees what webMAN reports over the network and PSN sees the
console itself; a plugin's name is not its behaviour, so a spoofer being
present is not a spoofer working; and what Sony currently checks for is not
knowable from here.

Absent is not empty, as everywhere else in analysis. A plugins category that
never ran means the plugin picture is unknown, which is emphatically not the
same as a console with no plugins, and the wording keeps those apart.
"""

import json
import re

from .findings import Finding

SCHEMA_VERSION = 1
ARTEFACT_NAME = "psn/safety.json"

# The parser records whatever word the page used, so both vocabularies appear.
OPEN_SYSCALLS = ("enabled", "open")
CLOSED_SYSCALLS = ("disabled", "closed", "blocked")
PARTIAL_SYSCALLS = ("partial",)

# Matched against the plugin's file name only, which is all a diagnostic over
# the network has. A match says "this is named like a thing that claims to
# change what PSN sees" and nothing whatsoever about whether it does.
SPOOFER_PATTERNS = (
    (re.compile(r"(?i)psn[\s_-]*patch"), "PSNPatch",
     "named for the tool whose stated purpose is to undo or hide custom "
     "firmware changes before signing in"),
    (re.compile(r"(?i)spoof"), "a spoofer",
     "the name contains \"spoof\", the usual naming for anything that reports "
     "false console or firmware details"),
    (re.compile(r"(?i)ps3xploit"), "a PS3Xploit tool",
     "part of the PS3Xploit family of exploit and re-signing tools"),
    (re.compile(r"(?i)idps"), "an IDPS tool",
     "IDPS is the console's own identity number, which is exactly what PSN "
     "reads"),
    (re.compile(r"(?i)psid"), "a PSID tool",
     "PSID is part of the console identity that PSN reads"),
)

# Not spoofers. Named separately because they touch the same machinery and a
# reader is better off seeing them listed than discovering them later.
PSN_RELEVANT_PATTERNS = (
    (re.compile(r"(?i)webftp_server|webman"), "webMAN MOD",
     "the server this diagnostic talked to; it can switch the extra custom "
     "firmware controls on and off"),
    (re.compile(r"(?i)ps3mapi"), "PS3MAPI",
     "gives other software direct access to the console's memory"),
    (re.compile(r"(?i)rebug[\s_-]*toolbox"), "Rebug Toolbox",
     "can change the firmware version and console type that this console "
     "reports about itself"),
    (re.compile(r"(?i)ps3hen|(?:^|[\s_.-])hen(?:[\s_.-]|$)"), "HEN",
     "the exploit that gives official firmware custom firmware abilities"),
    (re.compile(r"(?i)mamba"), "Mamba",
     "a payload that provides the same extra controls as Cobra"),
)

CAVEAT = (
    "This is a description of what this tool could see. It is not permission "
    "and it is not a warning off. Nobody can tell you that a console or an "
    "account will not be banned, and nobody outside Sony knows what is "
    "currently being checked or what has already been recorded. This tool "
    "reads what webMAN reports over your network, so it cannot see everything "
    "PSN can see and it cannot test whether a plugin does what its name "
    "suggests. The decision is yours."
)

ALWAYS_UNKNOWN = (
    "What PSN actually checks for, and what it may already have recorded "
    "about this console, cannot be seen by any tool of this kind.",
    "Whether a plugin does what its name suggests cannot be tested from "
    "outside the console; only the name was read.",
)


# --- tolerant readers ------------------------------------------------------

def _text(value):
    """Facts may have been written by an older version of the tool, so no
    field is assumed to be the type this module would like it to be."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return ""
    return str(value).strip()


def _rows(value):
    return [item for item in value] if isinstance(value, (list, tuple)) else []


def _entry_name(entry):
    """A plugin row, whether it arrived as a record or as a bare name."""
    if isinstance(entry, dict):
        name = _text(entry.get("name"))
        if name:
            return name
        return _text(entry.get("path")).rsplit("/", 1)[-1]
    return _text(entry).rsplit("/", 1)[-1]


def _match_patterns(name, table):
    for pattern, label, why in table:
        if pattern.search(name):
            return label, why
    return None, None


# --- what the system category says ----------------------------------------

def _system_view(artefact_set):
    view = {
        "collected": artefact_set.collected("system"),
        "status": artefact_set.status("system"),
        "syscalls": "unknown", "syscall_word": "", "syscall_number": "",
        "firmware": "", "firmware_type": "", "firmware_build": "",
        "cfw_name": "", "markers": [], "cobra_version": "", "hen_version": "",
        "kind": "unknown",
    }
    if not view["collected"]:
        return view

    facts = artefact_set.facts("system")
    word = _text(facts.get("syscall_state")).lower()
    view["syscall_word"] = word
    if word in OPEN_SYSCALLS:
        view["syscalls"] = "open"
    elif word in CLOSED_SYSCALLS:
        view["syscalls"] = "closed"
    elif word in PARTIAL_SYSCALLS:
        view["syscalls"] = "partial"
    view["syscall_number"] = _text(facts.get("syscall_number"))
    view["firmware"] = _text(facts.get("firmware"))
    view["firmware_type"] = _text(facts.get("firmware_type")).upper()
    view["firmware_build"] = _text(facts.get("firmware_build"))
    view["cfw_name"] = _text(facts.get("cfw_name"))
    view["markers"] = [_text(item) for item in _rows(facts.get("cfw_markers"))
                       if _text(item)]
    view["cobra_version"] = _text(facts.get("cobra_version"))
    view["hen_version"] = _text(facts.get("hen_version"))

    lowered = {marker.lower() for marker in view["markers"]}
    named_build = view["cfw_name"] and view["cfw_name"].lower() not in (
        "hen", "hfw", "cobra", "mamba")
    if named_build:
        view["kind"] = "cfw"
    elif view["hen_version"] or "hen" in lowered:
        # HEN runs on official or hacked-official firmware, which is a
        # different thing to see than a replaced firmware, so it is kept apart.
        view["kind"] = "hen"
    elif "hfw" in lowered:
        view["kind"] = "hfw"
    elif lowered or view["cobra_version"]:
        view["kind"] = "cfw"
    elif view["firmware"] or view["syscalls"] != "unknown":
        view["kind"] = "none-detected"
    return view


# --- what the plugins category says ---------------------------------------

def _plugin_view(artefact_set):
    view = {
        "collected": artefact_set.collected("plugins"),
        "status": artefact_set.status("plugins"),
        "error": artefact_set.error("plugins"),
        "loading": [], "switched_off": [], "installed_not_loading": [],
        "listed_but_not_found": [], "spoofers": [], "psn_relevant": [],
    }
    if not view["collected"]:
        return view

    facts = artefact_set.facts("plugins")
    candidates = []
    # Through the accessor, but only when the facts really hold a list: a
    # hand-edited facts.json with a string there would otherwise be walked a
    # character at a time.
    boot = (artefact_set.boot_plugins()
            if isinstance(facts.get("boot_plugins.txt"), (list, tuple)) else [])
    for entry in boot:
        name = _entry_name(entry)
        if not name:
            continue
        enabled = bool(entry.get("enabled")) if isinstance(entry, dict) else True
        (view["loading"] if enabled else view["switched_off"]).append(name)
        candidates.append((name, "listed to load at boot" if enabled
                           else "in the boot list but switched off", enabled))

    listed = {name.lower() for name in view["loading"] + view["switched_off"]}
    for entry in _rows(facts.get("installed_in_plugins_folder")):
        name = _entry_name(entry)
        kind = _text(entry.get("kind")) if isinstance(entry, dict) else "file"
        if not name or kind == "directory":
            continue
        if name.lower() not in listed:
            view["installed_not_loading"].append(name)
            candidates.append((name, "in the plugins folder but not in the "
                                     "boot list", False))

    seen = {name.lower() for name, _where, _on in candidates}
    for entry in _rows(facts.get("named_on_web_page")):
        name = _entry_name(entry)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            candidates.append((name, "named on the console's web page", None))

    view["listed_but_not_found"] = [
        _text(item).rsplit("/", 1)[-1]
        for item in _rows(facts.get("listed_but_not_found")) if _text(item)]

    for name, where, enabled in candidates:
        label, why = _match_patterns(name, SPOOFER_PATTERNS)
        if label:
            view["spoofers"].append({"name": name, "identified_as": label,
                                     "why": why, "where": where,
                                     "loading": enabled})
            continue
        label, why = _match_patterns(name, PSN_RELEVANT_PATTERNS)
        if label:
            view["psn_relevant"].append({"name": name, "identified_as": label,
                                         "why": why, "where": where,
                                         "loading": enabled})
    return view


def _settings_view(artefact_set):
    """Only whatever the setup form itself calls PSN or syscall related.

    Read by key rather than against a list of known setting names, because
    webMAN's form changes between versions and a guessed key name that has
    moved would silently report nothing.
    """
    view = {"collected": artefact_set.collected("webman_config"),
            "mentions": []}
    if not view["collected"]:
        return view
    settings = artefact_set.facts("webman_config").get("settings")
    if not isinstance(settings, dict):
        return view
    for key in sorted(settings):
        blob = f"{key} {_text(settings.get(key))}".lower()
        if "psn" in blob or "syscall" in blob:
            view["mentions"].append(f"{_text(key)} = {_text(settings[key])}")
    return view


# --- the assessment --------------------------------------------------------

def _firmware_phrase(system):
    if system["firmware_type"] in ("DEX", "DECR"):
        flavour = ("debug firmware, which is the kind used on development "
                   "consoles rather than on consoles sold in shops")
    elif system["firmware_type"] == "CEX":
        flavour = "retail firmware, the kind a console is sold with"
    else:
        flavour = ""
    version = system["firmware"]
    if version and flavour:
        return f"firmware {version} reported as {flavour}"
    if version:
        return f"firmware {version}"
    return flavour


def _statement(system, plugins):
    """One or two plain sentences. Deliberately descriptive: it says what is
    visible and stops, because any sentence about what would happen next would
    be a prediction this tool has no basis for."""
    if not system["collected"] and not plugins["collected"]:
        return ("Too little was collected from this console to say what it "
                "would look like to PSN. Nothing here should be read as "
                "either reassurance or a warning.")

    if not system["collected"]:
        return ("The console's firmware and its extra controls were not "
                "read, so what it would look like to PSN is unknown. Only "
                "the plugin list below was seen.")

    if system["syscalls"] == "open":
        head = ("This console would look to PSN like a PlayStation 3 running "
                "modified firmware with the extra controls left switched on, "
                "which is the signal that has historically been the easiest "
                "one for Sony to notice.")
    elif system["syscalls"] == "closed":
        head = ("This console reports that the extra controls modified "
                "firmware adds are currently switched off, which removes the "
                "most obvious single signal but not necessarily anything "
                "else about it.")
    elif system["syscalls"] == "partial":
        head = ("This console reports that the extra controls modified "
                "firmware adds are only partly switched off, so some of them "
                "are still there to be seen.")
    else:
        head = ("Whether this console has the extra controls modified "
                "firmware adds switched on could not be read, so the single "
                "most important thing about how it looks to PSN is unknown.")

    if system["kind"] == "cfw":
        # Without a name this used to fall back to the word "custom" and then
        # append "custom firmware" to it, giving "custom custom firmware".
        name = system["cfw_name"]
        tail = (f"It is running {name} custom firmware" if name
                else "It is running custom firmware")
    elif system["kind"] == "hen":
        tail = ("It is running HEN, which gives official firmware custom "
                "firmware abilities")
    elif system["kind"] == "hfw":
        tail = "It is running hacked official firmware"
    elif system["kind"] == "none-detected":
        tail = ("Nothing in what was collected named a custom firmware, "
                "though this tool only sees what the console says about "
                "itself")
    else:
        tail = "What firmware it is running could not be determined"

    phrase = _firmware_phrase(system)
    if phrase and system["kind"] != "none-detected":
        tail = f"{tail}, with {phrase}"
    return f"{head} {tail}."


def _observations(system, plugins, settings):
    out = []
    if not system["collected"]:
        out.append("The system and firmware details were not collected "
                   f"(status: {system['status']}), so nothing below describes "
                   "the firmware.")
    else:
        if system["syscalls"] == "open":
            number = (f" (syscall {system['syscall_number']})"
                      if system["syscall_number"] else "")
            out.append("The extra controls that custom firmware adds are "
                       f"switched on{number}. This is the classic detectable "
                       "signature.")
        elif system["syscalls"] == "closed":
            out.append("The extra controls that custom firmware adds are "
                       "reported as switched off.")
        elif system["syscalls"] == "partial":
            out.append("The extra controls that custom firmware adds are "
                       "reported as only partly switched off.")
        else:
            out.append("The console did not report whether the extra custom "
                       "firmware controls are switched on.")

        if system["firmware_type"] in ("DEX", "DECR"):
            out.append(f"The firmware reports itself as {system['firmware_type']}, "
                       "which is debug firmware rather than the retail kind.")
        elif system["firmware_type"]:
            out.append(f"The firmware reports itself as {system['firmware_type']}"
                       f"{' ' + system['firmware'] if system['firmware'] else ''}"
                       ", the retail kind.")
        if system["markers"]:
            out.append("Custom firmware markers seen on the console's own "
                       f"pages: {', '.join(system['markers'])}.")
        if system["cobra_version"]:
            out.append(f"Cobra {system['cobra_version']} is present.")
        if system["hen_version"]:
            out.append(f"HEN {system['hen_version']} is present.")

    if not plugins["collected"]:
        out.append("The plugin list was not collected "
                   f"(status: {plugins['status']}), so which plugins are "
                   "loaded is unknown. This is not the same as none.")
        return out

    if plugins["loading"]:
        count = len(plugins["loading"])
        out.append(f"{count} plugin{'' if count == 1 else 's'} "
                   f"{'is' if count == 1 else 'are'} set to load at startup: "
                   f"{', '.join(plugins['loading'])}.")
    else:
        out.append("No plugins are set to load at startup.")
    if plugins["switched_off"]:
        out.append("In the boot list but switched off: "
                   f"{', '.join(plugins['switched_off'])}.")
    if plugins["installed_not_loading"]:
        out.append("Installed in the plugins folder but not in the boot "
                   f"list, so not loading: "
                   f"{', '.join(plugins['installed_not_loading'])}.")
    if plugins["listed_but_not_found"]:
        out.append("Listed to load but the file was not found: "
                   f"{', '.join(plugins['listed_but_not_found'])}.")

    if plugins["spoofers"]:
        for match in plugins["spoofers"]:
            out.append(f"{match['name']} matches the name of "
                       f"{match['identified_as']}, {match['why']}. It is "
                       f"{match['where']}. Being present is not the same as "
                       "working.")
    else:
        out.append("No plugin in the list is named like a known spoofer or "
                   "PSN patcher.")
    for match in plugins["psn_relevant"]:
        out.append(f"{match['name']} is {match['identified_as']}, {match['why']}. "
                   f"It is {match['where']}.")

    for mention in settings["mentions"]:
        out.append(f"A webMAN setting mentions PSN or the syscalls: {mention}.")
    return out


def _unknowns(system, plugins, settings):
    out = []
    if not system["collected"]:
        out.append("The firmware, its type and the state of the extra "
                   "controls were not collected "
                   f"(status: {system['status']}), so none of them is known.")
    elif system["syscalls"] == "unknown":
        out.append("The console did not say whether the extra custom "
                   "firmware controls are switched on, so the most important "
                   "single fact here is missing.")
    if system["collected"] and not system["firmware_type"]:
        out.append("Whether this is retail or debug firmware was not "
                   "reported by the console.")
    if not plugins["collected"]:
        out.append("The plugin list was not collected "
                   f"(status: {plugins['status']}"
                   f"{'; ' + _text(plugins['error']) if plugins['error'] else ''})"
                   ", so whether a spoofer or any other plugin is loaded is "
                   "unknown. There may be plugins loaded that this did not "
                   "see.")
    if not settings["collected"]:
        out.append("The webMAN settings were not collected, so any "
                   "PSN-related option set there was not seen.")
    out.extend(ALWAYS_UNKNOWN)
    return out


def _psn_visible(system, plugins):
    """Three states on purpose. "Unknown" has to survive all the way to the
    reader rather than collapsing into "no"."""
    if not system["collected"]:
        return "unknown"
    if system["syscalls"] == "open" or system["syscalls"] == "partial":
        return "yes"
    if system["syscalls"] == "unknown":
        return "unknown"
    if system["firmware_type"] in ("DEX", "DECR") or system["markers"]:
        # Syscalls closed still leaves a modified console underneath.
        return "yes"
    if not plugins["collected"]:
        return "unknown"
    return "nothing-seen"


def assessment(artefact_set):
    """The whole assessment, in the shape psn/safety.json carries."""
    system = _system_view(artefact_set)
    plugins = _plugin_view(artefact_set)
    settings = _settings_view(artefact_set)
    return {
        "schema_version": SCHEMA_VERSION,
        "assessment": {
            "statement": _statement(system, plugins),
            "observations": _observations(system, plugins, settings),
            "unknowns": _unknowns(system, plugins, settings),
            "caveat": CAVEAT,
            "psn_visible_configuration": _psn_visible(system, plugins),
        },
    }


def record(artefact_set):
    """Write the assessment into the set under its reserved name.

    ps3diag.analysis writes the artefact itself from what assessment() returns,
    so this is for a caller driving the pass on its own.
    """
    payload = assessment(artefact_set)
    artefact_set.put(ARTEFACT_NAME,
                     json.dumps(payload, indent=2, sort_keys=True))
    return payload


# --- findings for the rules and report layers -----------------------------

def findings_for(artefact_set):
    """Findings, for the rules engine. The full picture stays in the
    assessment; these are only the parts that a reader has to be told."""
    system = _system_view(artefact_set)
    plugins = _plugin_view(artefact_set)
    out = []

    if not system["collected"]:
        out.append(Finding(
            rule_id="psn-system-not-collected", severity="info",
            title="Nothing can be said about how this console looks to PSN",
            explanation=("The firmware details were not collected, so this "
                         "check could not run. That is not the same as the "
                         "console being clean."),
            evidence=[f"system category status: {system['status']}"],
            category="system"))
    elif system["syscalls"] == "open":
        number = (f" (syscall {system['syscall_number']})"
                  if system["syscall_number"] else "")
        out.append(Finding(
            rule_id="psn-syscalls-open", severity="warn",
            title="The extra custom firmware controls are switched on",
            explanation=("Custom firmware adds a set of extra controls, and "
                         "this console has them switched on. That is the "
                         "thing PSN checks have historically been able to "
                         "notice most easily. It does not tell you what would "
                         "happen if you signed in."),
            fix=("If you do not want that visible, switch the extra controls "
                 "off before signing in. It removes one known signal and is "
                 "not a guarantee of anything."),
            evidence=[f"syscall_state: {system['syscall_word']}{number}"],
            category="system"))
    elif system["syscalls"] == "partial":
        out.append(Finding(
            rule_id="psn-syscalls-partial", severity="warn",
            title="The extra custom firmware controls are only partly off",
            explanation=("The console reports that some of the extra controls "
                         "custom firmware adds are still switched on. Partly "
                         "off is not off."),
            evidence=[f"syscall_state: {system['syscall_word']}"],
            category="system"))
    elif system["syscalls"] == "unknown":
        out.append(Finding(
            rule_id="psn-syscall-state-unknown", severity="info",
            title="Whether the extra custom firmware controls are on is unknown",
            explanation=("The console did not report this, so the single most "
                         "visible thing about it could not be checked. Treat "
                         "it as unknown rather than as off."),
            category="system"))

    if system["collected"] and system["firmware_type"] in ("DEX", "DECR"):
        out.append(Finding(
            rule_id="psn-debug-firmware", severity="warn",
            title="This console is running debug firmware",
            explanation=("The firmware reports itself as "
                         f"{system['firmware_type']}, which is the kind used "
                         "on development consoles rather than the kind a "
                         "console is sold with. It behaves differently from a "
                         "retail console in ways that are not hidden."),
            evidence=[f"firmware_type: {system['firmware_type']}",
                      f"firmware: {system['firmware'] or 'not reported'}"],
            category="system"))

    if not plugins["collected"]:
        out.append(Finding(
            rule_id="psn-plugins-not-collected", severity="info",
            title="Which plugins are loaded is unknown",
            explanation=("The plugin list could not be read, so nothing can "
                         "be said about what is loaded on this console. "
                         "Treat it as unknown. There may be plugins loaded "
                         "that this did not see."),
            evidence=[f"plugins category status: {plugins['status']}"],
            category="plugins"))
    else:
        if plugins["spoofers"]:
            loading = [m for m in plugins["spoofers"] if m["loading"]]
            not_loading = [m for m in plugins["spoofers"] if not m["loading"]]
            out.append(Finding(
                rule_id="psn-spoofer-plugin-present", severity="info",
                title="A plugin named like a spoofer is on this console",
                explanation=("One or more plugins are named like tools that "
                             "claim to change what PSN sees. This check only "
                             "read the names: it cannot tell whether any of "
                             "them is running, working, or up to date."),
                evidence=[f"{m['name']} - {m['identified_as']} - {m['where']}"
                          for m in plugins["spoofers"]],
                category="plugins"))
            if not_loading and not loading:
                out.append(Finding(
                    rule_id="psn-spoofer-plugin-not-loading", severity="warn",
                    title="The spoofer style plugin is not set to load",
                    explanation=("A plugin named like a spoofer is on the "
                                 "console but is not in the list of plugins "
                                 "that load at startup, so it is not running. "
                                 "People are caught out by assuming an "
                                 "installed plugin is an active one."),
                    evidence=[f"{m['name']} - {m['where']}"
                              for m in not_loading],
                    category="plugins"))
        if plugins["listed_but_not_found"]:
            out.append(Finding(
                rule_id="psn-boot-plugin-missing", severity="info",
                title="A plugin in the startup list is not actually there",
                explanation=("The startup list names a plugin file that could "
                             "not be found, so the list does not describe "
                             "what is really loading."),
                evidence=list(plugins["listed_but_not_found"]),
                category="plugins"))

    visible = _psn_visible(system, plugins)
    if visible in ("yes", "unknown"):
        out.append(Finding(
            rule_id="psn-visible-configuration",
            severity="warn" if visible == "yes" else "info",
            title=("This console is set up in a way PSN could see"
                   if visible == "yes"
                   else "Whether PSN could see this set-up is unknown"),
            explanation=("See the PSN section of the summary for what was "
                         "observed and what could not be seen. Nobody can "
                         "promise a console or an account will not be "
                         "banned, and this tool cannot see everything PSN "
                         "can."),
            evidence=[_statement(system, plugins)],
            category="system"))
    return out


# The names the rest of the tool uses to reach this pass are findings_for and
# assessment; these are here for anything that reached for the longer ones.
assess = assessment
psn_findings = findings_for
