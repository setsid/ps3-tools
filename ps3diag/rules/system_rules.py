"""Rules about the console itself: its plug-ins, its firmware and its heat."""

import re

from ..findings import Finding
from .engine import rule
from .inference import (counted, is_are, is_iso_name,
                        startup_plugins, title_rows)

# Above this the console is too hot to be healthy. The PS3 shuts itself down
# somewhere in the eighties, so 80 C is the point at which a session is about
# to be interrupted, and 85 C is close enough to call it a fault.
HOT_C = 80.0
VERY_HOT_C = 85.0

# Substrings of the plug-in path. Both managers ship under several names and
# all of them contain one of these.
WEBMAN_MARKERS = ("webftp_server", "webman")
MULTIMAN_MARKERS = ("multiman", "mmcm")

# Deliberately narrow. It must not match boot_plugins_nocobra.txt, which is a
# filename and says nothing about how the console booted, and the gap it allows
# between the two words holds no letters or digits so that a version number
# cannot sit in it.
TAG = re.compile(r"<[^>]*>")
COBRA_OFF = re.compile(r"(?i)\bnon-?cobra\b|\bcobra\b[^a-z0-9\n]{0,12}"
                       r"(off|disabled)\b")


@rule("manager-plugin-conflict", category="plugins")
def manager_plugin_conflict(artefact_set):
    """Two game managers both set to load at start-up."""
    if not artefact_set.collected("plugins"):
        return None
    enabled = [plugin for plugin in startup_plugins(artefact_set)
               if plugin.get("enabled")]
    webman = _matching(enabled, WEBMAN_MARKERS)
    multiman = _matching(enabled, MULTIMAN_MARKERS)
    if not (webman and multiman):
        return None
    return Finding(
        rule_id="manager-plugin-conflict",
        severity="warn",
        title="Two game managers are both set to load when the console starts",
        explanation=(
            "webMAN and multiMAN are both in the start-up list. They do the "
            "same job and get in each other's way, which shows up as freezing "
            "at start-up, or as games that refuse to load for no obvious "
            "reason."),
        fix=("Keep only one of the two. Open /dev_hdd0/boot_plugins.txt, put "
             "a # at the start of the line naming the one you do not use, "
             "save it, and restart the console."),
        evidence=[f"line {plugin.get('line')}: {plugin.get('path')}"
                  for plugin in (webman[:1] + multiman[:1])],
        category="plugins")


def _matching(plugins, markers):
    out = []
    for plugin in plugins:
        haystack = f"{plugin.get('path') or ''} {plugin.get('name') or ''}"
        if any(marker in haystack.lower() for marker in markers):
            out.append(plugin)
    return out


@rule("cobra-disabled-with-isos", category="system")
def cobra_disabled_with_isos(artefact_set):
    """ISO games on a console that shows no sign of Cobra.

    Absence of a fact is not the same as a fact being false, so this only
    speaks when the system page clearly parsed: if the firmware and webMAN
    version came through and Cobra did not, the silence means something.
    """
    if not (artefact_set.collected("system")
            and artefact_set.collected("games")):
        return None
    facts = artefact_set.facts("system")
    if not (facts.get("firmware") or facts.get("webman_version")):
        return None

    isos = [entry for entry in title_rows(artefact_set)
            if entry.get("kind") == "file" and is_iso_name(entry.get("name"))]
    # HEN consoles were being sent away to have custom firmware with Cobra
    # fitted over games that were already starting: webMAN mounts PS3 ISOs
    # without Cobra on HEN, so the .iso files this was counting were fine.
    # PS2 images are the one kind that genuinely will not start without
    # Cobra whatever the firmware, so on HEN the finding speaks only for
    # those. Anything other than a reported "hen" keeps the old behaviour,
    # including a console that did not say what it runs, because a guess
    # either way is worse than the warning people already know how to read.
    if _firmware_kind(facts) == "hen":
        isos = [entry for entry in isos if _is_ps2_iso(entry)]
    if not isos:
        return None

    markers = [str(name).lower() for name in facts.get("cfw_markers", [])]
    has_cobra = bool(facts.get("cobra_version")) or any(
        name in ("cobra", "mamba") for name in markers)
    switched_off = _cobra_switched_off(artefact_set)
    if has_cobra and not switched_off:
        return None

    count = len(isos)
    stuck = ("It will not start" if count == 1
             else "None of them will start")
    if switched_off:
        return Finding(
            rule_id="cobra-disabled-with-isos",
            severity="error",
            title="The part that starts .iso games is switched off",
            explanation=(
                f"The console reports Cobra as off. Cobra is what lets a "
                f"game saved as a single .iso file be started, and there "
                f"{is_are(count)} {counted(count, 'game')} like that on this "
                f"console. {stuck} until it is switched back on."),
            fix=("Restart the console and look at webMAN's front page again. "
                 "If it still says Cobra is off, the custom firmware needs "
                 "reinstalling by whoever set the console up."),
            evidence=[switched_off,
                      f"{counted(count, 'game')} saved as .iso files"],
            category="system")
    return Finding(
        rule_id="cobra-disabled-with-isos",
        severity="warn",
        title="Games saved as .iso files may not start on this console",
        explanation=(
            f"Nothing the console reported mentions Cobra, which is the "
            f"part of the custom firmware that lets a game saved as one .iso "
            f"file be started, and there {is_are(count)} "
            f"{counted(count, 'game')} saved that way here. This is going on "
            f"what the console did not say, so it is worth checking rather "
            f"than taking as read."),
        fix=("Open webMAN in a browser and read the top of its front page. If "
             "Cobra is not named there, ask whoever installed the custom "
             "firmware to fit a version that includes it."),
        evidence=[f"firmware reported: {facts.get('firmware', 'unknown')} "
                  f"{facts.get('cfw_name', '')}".strip(),
                  "no Cobra or Mamba version was reported",
                  f"{counted(count, 'game')} saved as .iso files"],
        category="system")


def _firmware_kind(facts):
    """"cfw", "hen", "ofw", or "" when the console did not say.

    Older artefact sets predate the fact altogether and land on "" here,
    which reads the same as a console that stayed silent about it.
    """
    return str(facts.get("firmware_kind") or "").strip().lower()


def _is_ps2_iso(entry):
    """PS2ISO is where webMAN itself decides an image is a PS2 game, so it
    is the only place a PS2 image can be sitting and be playable."""
    return str(entry.get("folder") or "").upper() == "PS2ISO"


def _cobra_switched_off(artefact_set):
    """The words on the console's own page saying Cobra is off, or None."""
    for name in artefact_set.names("system/*"):
        # Tags out first: "<b>Cobra:</b> OFF" is two words with markup between
        # them, and the markup is not part of what the page says.
        plain = TAG.sub(" ", artefact_set.text(name) or "")
        match = COBRA_OFF.search(plain)
        if match:
            return f"{name} says: {match.group(0).strip()}"
    return None


@rule("temperature-high", category="system")
def temperature_high(artefact_set):
    """Either chip above 80 C while the diagnostic was running."""
    if not artefact_set.collected("system"):
        return None
    facts = artefact_set.facts("system")
    cpu = _temperature(facts.get("cpu_temp_c"))
    rsx = _temperature(facts.get("rsx_temp_c"))
    hottest = max([value for value in (cpu, rsx) if value is not None],
                  default=None)
    if hottest is None or hottest <= HOT_C:
        return None

    evidence = []
    if cpu is not None:
        evidence.append(f"CPU {cpu:.0f} C")
    if rsx is not None:
        evidence.append(f"graphics chip {rsx:.0f} C")
    return Finding(
        rule_id="temperature-high",
        severity="error" if hottest >= VERY_HOT_C else "warn",
        title="The console is running hot",
        explanation=(
            f"It reached {hottest:.0f} C while this check was running. At "
            f"that temperature the console shuts itself down part way "
            f"through a game to protect itself, and staying that hot wears "
            f"it out."),
        fix=("Turn the console off and let it cool down. Have the dust "
             "cleaned out of it and the thermal paste replaced before playing "
             "anything demanding, and stand it somewhere with clear space "
             "around the vents."),
        evidence=evidence,
        category="system")


def _temperature(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
