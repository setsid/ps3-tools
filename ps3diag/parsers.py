"""Parsers for everything the console sends back.

The shape of these is driven by one fact: the endpoint list is unverified and
webMAN's markup has changed repeatedly between 1.47 and 10.x. So nothing here
parses HTML as HTML. Every parser flattens the response to text first and then
looks for its own labels in it. A layout change moves a value from a table cell
to a div and these carry on working; an HTML parser tied to the table would not.

Every parser returns a dict and never raises. A field that could not be found
comes back absent rather than guessed, because a helper reading summary.txt can
work with a blank and cannot work with a wrong answer that looks right.
"""

import html as html_module
import re

from .regioncodes import find_title_id

SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
              "TB": 1024 ** 4, "PB": 1024 ** 5}

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_BREAK = re.compile(r"(?i)<\s*(br|/tr|/p|/div|/h[1-6]|/li)\s*/?>")


def html_to_text(body):
    """Markup to plain text, keeping the line structure that carries meaning.

    Row and line breaks become newlines first, because "CPU: 61 RSX: 54" on one
    line and on two lines are different values and collapsing everything to a
    single line makes the second case unparseable.
    """
    if body is None:
        return ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    text = _SCRIPT.sub(" ", body)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html_module.unescape(text)
    text = text.replace(" ", " ").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def human_size(count):
    if count is None:
        return ""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def parse_size(number, unit):
    try:
        return int(float(number) * SIZE_UNITS[unit.upper()])
    except (TypeError, ValueError, KeyError):
        return None


# --- FTP LIST --------------------------------------------------------------

# Everything up to the name is fixed width-ish and the name is whatever is left,
# which is the only way to get "Gran Turismo 5 & Prologue, Collector's Edition
# [BCES00569].iso" out intact. The name is never split on whitespace and never
# stripped, so ampersands, commas, apostrophes, double spaces and a space before
# the extension all survive.
LIST_LINE = re.compile(
    r"^(?P<type>[-dlbcps])(?P<perms>[rwxstST-]{9})\S*\s+"
    r"(?:(?P<links>\d+)\s+)?"
    r"(?P<owner>\S+)\s+(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<stamp>\d{1,2}:\d{2}|\d{4})\s+"
    r"(?P<name>.+)$"
)

KINDS = {"-": "file", "d": "directory", "l": "link", "b": "block",
         "c": "char", "p": "pipe", "s": "socket"}


#: The two entries every FTP listing has and nobody ever means. They are left
#: in parse_ftp_list's output because that function reports what the console
#: said; anything counting or showing a folder's contents drops them first.
DOT_ENTRIES = (".", "..")


def real_entries(entries):
    """A listing without "." and "..".

    A transfer screen that counted them told somebody they had 30 things on
    their console when they had rather fewer, which is the kind of wrong that
    looks like the tool cannot read the console properly.
    """
    return [entry for entry in entries
            if entry.get("name") not in DOT_ENTRIES]


def parse_ftp_list(text):
    """Unix LIST output to entries.

    Returns (entries, unparsed). Lines that do not match are handed back rather
    than dropped: a listing this does not understand is a thing the helper needs
    to see, and silently returning an empty directory would hide it.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    entries = []
    unparsed = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if re.match(r"(?i)^total\s+\d+$", line.strip()):
            continue
        match = LIST_LINE.match(line)
        if not match:
            unparsed.append(line)
            continue
        name = match.group("name")
        target = None
        if match.group("type") == "l" and " -> " in name:
            name, _, target = name.partition(" -> ")
        entries.append({
            "name": name,
            "kind": KINDS.get(match.group("type"), "other"),
            "size": int(match.group("size")),
            "permissions": match.group("type") + match.group("perms"),
            "owner": match.group("owner"),
            "group": match.group("group"),
            "modified": f"{match.group('month')} {match.group('day')} "
                        f"{match.group('stamp')}",
            "link_target": target,
        })
    return entries, unparsed


# --- identity, firmware, CFW ----------------------------------------------

WEBMAN_VERSION = re.compile(r"(?i)webMAN(?:\s*MOD)?\s*v?\.?\s*"
                            r"(\d+\.\d+(?:\.\d+)?[a-z]?)")
FIRMWARE = re.compile(r"(?i)\b(\d\.\d{2})\s*(CEX|DEX|DECR)\b")
FIRMWARE_LOOSE = re.compile(r"(?i)firmware[:\s]*v?(\d\.\d{2})")
COBRA = re.compile(r"(?i)\bcobra\s*v?\.?\s*([\d.]+)")
CFW_NAMES = ("Evilnat", "Rebug", "Ferrox", "Habib", "Rogero", "Mamba",
             "Cobra", "HEN", "HFW", "Darknet", "Overflow", "Wutangrza")
SYSCALL = re.compile(r"(?i)syscall\s*(\d+)?\s*[:\s]*\b(enabled|disabled|"
                     r"open|closed|partial|blocked)\b")
UPTIME = re.compile(r"(?i)\buptime[:\s]*([0-9hmsd :]+)")
SYSTIME = re.compile(r"(?i)system\s*time[:\s]*([0-9]{4}-[0-9]{2}-[0-9]{2}"
                     r"[ T][0-9:]{5,8})")
HEN_VER = re.compile(r"(?i)\bHEN\s*v?\.?\s*([\d.]+)")


def parse_identity(text):
    """Firmware, CFW flavour and anything else the root page says about itself.

    Fed the flattened root page, and also the cpursx page, because which of the
    two carries the firmware string moves between versions.
    """
    out = {}
    match = WEBMAN_VERSION.search(text)
    if match:
        out["webman_version"] = match.group(1)
    match = FIRMWARE.search(text)
    if match:
        out["firmware"] = match.group(1)
        out["firmware_type"] = match.group(2).upper()
    else:
        match = FIRMWARE_LOOSE.search(text)
        if match:
            out["firmware"] = match.group(1)
    found = [name for name in CFW_NAMES
             if re.search(rf"(?i)\b{name}\b", text)]
    if found:
        out["cfw_markers"] = found
        # Cobra and HEN describe the payload rather than the build, so the
        # named build is whichever marker is not one of those.
        builds = [name for name in found
                  if name.lower() not in ("cobra", "mamba", "hen", "hfw")]
        if builds:
            out["cfw_name"] = builds[0]
    match = COBRA.search(text)
    if match:
        out["cobra_version"] = match.group(1)
    match = HEN_VER.search(text)
    if match:
        out["hen_version"] = match.group(1)
    match = SYSCALL.search(text)
    if match:
        out["syscall_state"] = match.group(2).lower()
        if match.group(1):
            out["syscall_number"] = match.group(1)
    match = UPTIME.search(text)
    if match:
        out["uptime"] = match.group(1).strip()
    match = SYSTIME.search(text)
    if match:
        out["console_time"] = match.group(1).strip()
    return out


# --- what the console says it is running -----------------------------------

# webMAN prints one line naming the firmware and then appends whatever is
# running on top of it: "4.93 CEX", "4.93 CEX Cobra 8.5", "4.93 CEX PS3HEN
# 3.5.0". That line is the console's own answer to the question this tool is
# most often asked, so it is read whole and kept verbatim as the evidence for
# whatever is concluded from it.
#
# The anchor is the version sitting next to the CEX/DEX/DECR/HFW token,
# because every layout seen prints those two together and in that order. A
# couple of words are tolerated between them for the builds that print their
# own name there, and an intervening word has to begin with a letter so that
# the "1.47.48" of a webMAN version on the same line can never be paired with
# a region token further along it.
FW_LINE = re.compile(r"(?i)\b(\d\.\d{2}(?:\.\d+)?)(?![\w.])"
                     r"(?:\s+[A-Za-z][\w.-]*){0,2}?\s*"
                     r"\b(?:CEX|DEX|DECR|HFW)\b")
REGION_ON_LINE = re.compile(r"(?i)\b(CEX|DEX|DECR)\b")
HFW_ON_LINE = re.compile(r"(?i)\bHFW\b")
HEN_ON_LINE = re.compile(r"(?i)\b(?:PS3)?HEN\b\s*v?\.?\s*(\d+(?:\.\d+)*)?")
COBRA_ON_LINE = re.compile(r"(?i)\bcobra\b\s*v?\.?\s*(\d+(?:\.\d+)*)?")
MAMBA_ON_LINE = re.compile(r"(?i)\bmamba\b")
CFW_WORD = re.compile(r"(?i)\bCFW\b")
# Cobra, Mamba, HEN and HFW name the payload or the repack rather than the
# build, so the names that identify a replaced firmware are all the others.
BUILD_NAMES = tuple(name for name in CFW_NAMES
                    if name.lower() not in ("cobra", "mamba", "hen", "hfw"))
BUILD_NAME = re.compile(r"(?i)\b(" + "|".join(BUILD_NAMES) + r")\b")


def _names_a_payload(line):
    """Whether a line names something running on top of the firmware."""
    return bool(BUILD_NAME.search(line) or HEN_ON_LINE.search(line)
                or COBRA_ON_LINE.search(line) or MAMBA_ON_LINE.search(line))


def _kind_from_line(line):
    """Which kind one line names: "cfw", "hen", or "" for neither.

    A named build beats HEN because a replaced firmware is the stronger fact
    and PS3HEN only ever runs on an official or hybrid one. HEN beats Cobra
    because HEN carries a Cobra of its own, so a HEN console prints both and
    the Cobra version there describes HEN rather than a custom firmware.
    """
    if BUILD_NAME.search(line):
        return "cfw"
    if HEN_ON_LINE.search(line):
        return "hen"
    if (COBRA_ON_LINE.search(line) or MAMBA_ON_LINE.search(line)
            or CFW_WORD.search(line)):
        return "cfw"
    return ""


def _kind_from_page(text):
    """The same question put to the whole page, for a line that named nothing.

    Only a marker carrying a version number counts here, along with the build
    names, because a menu entry reading "Install HEN" is a label on a button
    and not a report of what the console is running. This runs at all because
    a console with its Cobra payload switched off prints a bare firmware line
    while the rest of the page still says which build it is.
    """
    if BUILD_NAME.search(text):
        return "cfw"
    if HEN_VER.search(text):
        return "hen"
    if COBRA.search(text):
        return "cfw"
    return ""


def _firmware_line(text):
    """The line the firmware is printed on, preferring the one that says most.

    Several pages are flattened together before this runs and more than one of
    them can carry a firmware string, so a line that also names Cobra, HEN or
    a build is taken over a bare one. The loose "Firmware: 4.90" form is only
    looked at when no line carries a region token at all: that layout is one
    this tool has not seen on a console, and its silence about a payload
    proves nothing about what is running.
    """
    first_line, first_match = None, None
    lines = (text or "").splitlines()
    for line in lines:
        match = FW_LINE.search(line)
        if match is None:
            continue
        if _names_a_payload(line):
            return line, match
        if first_match is None:
            first_line, first_match = line, match
    if first_match is not None:
        return first_line, first_match
    for line in lines:
        match = FIRMWARE_LOOSE.search(line)
        if match:
            return line, match
    return None, None


def parse_firmware_line(text):
    """What the console says it is running, from its own firmware line.

    firmware_kind is "" whenever the page did not say, and a reader must be
    able to tell that from a confident "ofw": the difference is a console this
    tool could not read against one it read as stock. So no kind is ever
    inferred from missing evidence. The single positive case is webMAN's stock
    string, a version and a region with no payload named on the line and none
    named anywhere else on the page, which is a console reporting official
    firmware outright.

    firmware_line is the line exactly as the console printed it, prefix and
    all. It is the evidence behind every other field here, and trimming it to
    the part that was understood would throw away the part that explains a
    field this tool got wrong.

    All six fields always come back, empty where the page said nothing, which
    is the one place in this module where a blank is reported rather than left
    out: an empty firmware_kind is itself the answer and a field that vanished
    could not carry it. parse_cpursx drops the blanks the merge would spoil.
    """
    facts = {"firmware_line": "", "firmware_version": "",
             "firmware_region": "", "firmware_kind": "",
             "cobra_version": "", "hen_version": ""}
    line, match = _firmware_line(text)
    if match is None:
        return facts
    facts["firmware_line"] = line
    facts["firmware_version"] = match.group(1)
    region = REGION_ON_LINE.search(line)
    if region:
        facts["firmware_region"] = region.group(1).upper()
    hen = HEN_ON_LINE.search(line)
    if hen and hen.group(1):
        facts["hen_version"] = hen.group(1)
    cobra = COBRA_ON_LINE.search(line)
    if cobra and cobra.group(1):
        facts["cobra_version"] = cobra.group(1)
    kind = _kind_from_line(line) or _kind_from_page(text)
    # HFW is official firmware that has been repacked, which is a third thing
    # alongside stock and custom, so on its own it has no honest answer among
    # the four. It stays unknown and firmware_line keeps the word for whoever
    # reads the evidence.
    if not kind and facts["firmware_region"] and not HFW_ON_LINE.search(line):
        kind = "ofw"
    facts["firmware_kind"] = kind
    return facts


# --- the game the console has loaded ---------------------------------------

# Why this is read at all: the console keeps hold of a game's modules once it
# has loaded them, so writing to those files while the game is up changes
# nothing whatsoever. Quitting to the XMB does not release them either; the
# console itself has to be restarted. Somebody spent an evening patching a
# game that was running and got no result from any of it, so the running
# title is read and the patcher stops rather than prints a warning nobody
# reads.
#
# The label is matched loosely because webMAN's wording for it has moved
# between versions, and the identifier is matched strictly because the cost
# of the two mistakes is not the same. Missing a running game leaves things
# as they were before this existed. Naming the wrong game refuses to patch a
# console that is sitting ready, which looks like the tool being broken.
RUNNING_LABEL = re.compile(
    r"(?i)\b(?:game(?:\s*id)?|now\s+playing|playing|running|process|pid)"
    r"\b\s*[:=]")

#: Four letters and five digits, with the dash some pages print inside it.
#: This is the shape alone. find_title_id decides whether a shape is really a
#: title ID, which is what keeps a serial number or a model code printed
#: after one of these labels from being reported as a game.
TITLE_SHAPE = re.compile(r"(?i)\b[A-Z]{4}[-_]?\d{5}\b")


def parse_running_title(text):
    """The title ID of the game the console is running, uppercase, or "".

    A console sitting on the XMB and a page that never mentions a game both
    come back empty, and nothing here tells those two apart. That is the
    trade this parser is built around: an empty answer leaves the caller
    where it would have been anyway, and a confident wrong answer stops
    somebody patching a console that has nothing loaded.

    Only text after one of the labels is looked at, so the disc in the tray,
    a link that would start a game and a folder listing are all ignored. They
    say what is installed rather than what is loaded.
    """
    found = set()
    for line in (text or "").splitlines():
        label = RUNNING_LABEL.search(line)
        if label is None:
            continue
        for shape in TITLE_SHAPE.findall(line[label.end():]):
            title_id = find_title_id(shape)
            if title_id:
                found.add(title_id)
    # Two different games named on the same page is a list of what is
    # installed rather than a report of what is running, and there is no way
    # to tell from here which of them the console has loaded.
    return found.pop() if len(found) == 1 else ""


# --- temperatures, clocks, fan --------------------------------------------

TEMP_C = r"([\d.]+)\s*(?:°|&deg;)?\s*C\b"
CPU_TEMP = re.compile(r"(?i)\bCPU\b[^0-9\n]{0,16}" + TEMP_C)
RSX_TEMP = re.compile(r"(?i)\bRSX\b[^0-9\n]{0,16}" + TEMP_C)
CPU_TEMP_F = re.compile(r"(?i)\bCPU\b[^\n]{0,24}?([\d.]+)\s*(?:°|&deg;)?\s*F\b")
RSX_TEMP_F = re.compile(r"(?i)\bRSX\b[^\n]{0,24}?([\d.]+)\s*(?:°|&deg;)?\s*F\b")
CLOCK = r"([\d.]+)\s*(MHz|GHz)"
CPU_CLOCK = re.compile(r"(?i)\bCPU\b(?:\s*clock)?[^0-9\n]{0,16}" + CLOCK)
RSX_CLOCK = re.compile(r"(?i)\bRSX\b(?:\s*clock)?[^0-9\n]{0,16}" + CLOCK)
MEM_CLOCK = re.compile(r"(?i)\bmem(?:ory)?\s*clock[^0-9\n]{0,16}" + CLOCK)
FAN_SPEED = re.compile(r"(?i)\bfan\b[^0-9\n]{0,24}(\d{1,3})\s*%")
FAN_MODE = re.compile(r"(?i)\bfan\b[^\n]{0,40}?\b(manual|auto(?:matic)?|"
                      r"syscon|dynamic|step|smooth)\b")


def _mhz(number, unit):
    try:
        value = float(number)
    except (TypeError, ValueError):
        return None
    return round(value * 1000) if unit.lower() == "ghz" else round(value)


def parse_cpursx(text):
    """Temperatures, clock speeds, fan state and the firmware line.

    Celsius is what the console reports and Fahrenheit is only taken when it is
    printed; it is never converted, so a helper can tell which figure the
    console actually gave.

    The firmware fields come from parse_firmware_line, because this is the page
    that names the firmware and the tool is to work it out for itself rather
    than ask whoever is running it.
    """
    out = {}
    for key, pattern in (("cpu_temp_c", CPU_TEMP), ("rsx_temp_c", RSX_TEMP)):
        match = pattern.search(text)
        if match:
            out[key] = float(match.group(1))
    for key, pattern in (("cpu_temp_f", CPU_TEMP_F), ("rsx_temp_f", RSX_TEMP_F)):
        match = pattern.search(text)
        if match:
            out[key] = float(match.group(1))
    for key, pattern in (("cpu_clock_mhz", CPU_CLOCK),
                         ("rsx_clock_mhz", RSX_CLOCK),
                         ("memory_clock_mhz", MEM_CLOCK)):
        match = pattern.search(text)
        if match:
            value = _mhz(match.group(1), match.group(2))
            if value:
                out[key] = value
    match = FAN_SPEED.search(text)
    if match:
        out["fan_speed_percent"] = int(match.group(1))
    match = FAN_MODE.search(text)
    if match:
        out["fan_mode"] = match.group(1).lower()
    firmware = parse_firmware_line(text)
    # parse_identity reads Cobra and HEN from the whole page and every caller
    # merges that dict with this one, so a blank from the firmware line is
    # dropped here rather than allowed to erase the version the page gave
    # somewhere else. The other four fields are always reported, empty or not.
    for key, value in firmware.items():
        if value or key not in ("cobra_version", "hen_version"):
            out[key] = value
    return out


# --- storage ---------------------------------------------------------------

DEVICE = re.compile(r"(?i)\b(dev_(?:hdd\d|usb\d{3}|sd|ms|cf|bdvd|flash\d?|"
                    r"ntfs\d*|blind|usb\d{1,3}))\b")
SIZE_TOKEN = re.compile(r"([\d.]+)\s*(TB|GB|MB|KB|B)\b", re.IGNORECASE)


#: webMAN's own pages say "HDD: 572.9 GB free" rather than naming the device
#: the way a mount listing does. parse_storage matches device names, so it
#: reads nothing from that wording and the figure was being dropped on a
#: console that had reported it perfectly well.
HDD_FREE = re.compile(
    r"(?i)\bHDD\b[^\n]{0,24}?([\d.,]+)\s*(TB|GB|MB|KB)\b[^\n]{0,12}?\bfree\b")


def parse_hdd_free(text):
    """Free bytes on the internal drive as webMAN's pages word it, or None.

    Separate from parse_storage because that reads a mount listing, where the
    device is named and the numbers are positional. This reads one sentence
    meant for a person.
    """
    match = HDD_FREE.search(text or "")
    if not match:
        return None
    return parse_size(match.group(1).replace(",", ""), match.group(2))


def parse_storage(text):
    """Mounted devices with free and total space.

    The two numbers are taken positionally, free then total, because every
    layout seen prints them in that order and only some of them label which is
    which. A line with one number is recorded with total unknown rather than
    being guessed at.
    """
    devices = {}
    for line in text.splitlines():
        match = DEVICE.search(line)
        if not match:
            continue
        name = match.group(1).lower()
        tail = line[match.end():]
        sizes = SIZE_TOKEN.findall(tail)
        entry = devices.setdefault(name, {"device": name})
        if sizes:
            free = parse_size(*sizes[0])
            if free is not None:
                entry["free_bytes"] = free
                entry["free"] = human_size(free)
            if len(sizes) > 1:
                total = parse_size(*sizes[1])
                if total is not None:
                    entry["total_bytes"] = total
                    entry["total"] = human_size(total)
                    if free is not None and total:
                        entry["used_bytes"] = max(0, total - free)
                        entry["used"] = human_size(entry["used_bytes"])
                        entry["used_percent"] = round(
                            (total - free) * 100.0 / total, 1)
        if re.search(r"(?i)no\s+disc|not\s+mounted|empty", tail):
            entry["note"] = "no disc or not mounted"
    return list(devices.values())


# --- network ---------------------------------------------------------------

NETWORK_FIELDS = (
    ("ip_address", r"(?i)\bIP(?:\s*address)?\b[:\s]*((?:\d{1,3}\.){3}\d{1,3})"),
    ("subnet_mask", r"(?i)\b(?:subnet\s*mask|netmask)\b[:\s]*"
                    r"((?:\d{1,3}\.){3}\d{1,3})"),
    ("gateway", r"(?i)\b(?:default\s*)?gateway\b[:\s]*"
                r"((?:\d{1,3}\.){3}\d{1,3})"),
    ("dns_primary", r"(?i)\b(?:primary\s*dns|dns\s*1)\b[:\s]*"
                    r"((?:\d{1,3}\.){3}\d{1,3})"),
    ("dns_secondary", r"(?i)\b(?:secondary\s*dns|dns\s*2)\b[:\s]*"
                      r"((?:\d{1,3}\.){3}\d{1,3})"),
    ("mac_address", r"(?i)\bMAC(?:\s*address)?\b[:\s]*"
                    r"((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})"),
    ("connection", r"(?i)\bconnection\b[:\s]*([^\n]{1,60})"),
    ("mtu", r"(?i)\bMTU\b[:\s]*(\d{3,5})"),
    ("proxy", r"(?i)\bproxy\b[:\s]*([^\n]{1,40})"),
    ("link_speed", r"(?i)\b(\d{2,4}\s*Mbps[^\n)]{0,24})"),
)


def parse_network(text):
    out = {}
    for key, pattern in NETWORK_FIELDS:
        match = re.search(pattern, text)
        if match:
            out[key] = match.group(1).strip()
    return out


# --- webMAN settings -------------------------------------------------------

INPUT_TAG = re.compile(r"(?is)<input\b([^>]*)>")
SELECT_TAG = re.compile(r"(?is)<select\b([^>]*)>(.*?)</select>")
OPTION_TAG = re.compile(r"(?is)<option\b([^>]*)>(.*?)(?=<option|\Z)")
ATTR = re.compile(r"""(?i)(\w[\w-]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")


def _attrs(chunk):
    out = {}
    for match in ATTR.finditer(chunk):
        name = match.group(1).lower()
        out[name] = (match.group(3) if match.group(3) is not None
                     else match.group(4) if match.group(4) is not None
                     else match.group(5) or "")
    for flag in ("checked", "selected", "disabled"):
        if re.search(rf"(?i)\b{flag}\b(?!\s*=)", chunk):
            out[flag] = "true"
    return out


def parse_setup(body):
    """webMAN's own settings, read off the setup form without submitting it.

    The form is read as text and never posted back. That is the whole reason
    this collector exists as a parser rather than as a request with parameters:
    the same endpoint with a query string writes the settings.
    """
    settings = {}
    for match in INPUT_TAG.finditer(body or ""):
        attrs = _attrs(match.group(1))
        name = attrs.get("name")
        if not name:
            continue
        kind = (attrs.get("type") or "text").lower()
        if kind in ("submit", "button", "image", "reset", "password"):
            continue
        if kind in ("checkbox", "radio"):
            settings[name] = ("on" if attrs.get("checked") else "off")
        else:
            settings[name] = attrs.get("value", "")
    for match in SELECT_TAG.finditer(body or ""):
        name = _attrs(match.group(1)).get("name")
        if not name:
            continue
        chosen = None
        for option in OPTION_TAG.finditer(match.group(2)):
            attrs = _attrs(option.group(1))
            label = html_module.unescape(_TAG.sub("", option.group(2))).strip()
            if attrs.get("selected"):
                chosen = attrs.get("value", label)
        if chosen is not None:
            settings[name] = chosen
    return settings


# --- boot_plugins.txt ------------------------------------------------------

def parse_boot_plugins(text):
    """Plugin paths in load order, with the commented out ones kept.

    A plugin that is present but commented out is the single most useful line in
    this file when something has stopped loading, so it is reported rather than
    filtered.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    plugins = []
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip().lstrip("﻿")
        if not line:
            continue
        disabled = line.startswith("#") or line.startswith(";")
        path = line.lstrip("#; ").strip().strip('"')
        if not path:
            continue
        if disabled and not re.match(r"(?i)^[/\\]|^\w+:", path):
            # A comment that is prose rather than a path. Kept out of the list
            # so a header line does not show up as a disabled plugin.
            continue
        plugins.append({
            "line": number,
            "path": path,
            "enabled": not disabled,
            "name": path.replace("\\", "/").rsplit("/", 1)[-1],
        })
    return plugins


def parse_version_txt(text):
    """/dev_flash/vsh/etc/version.txt, the exact firmware build."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    out = {}
    for line in (text or "").splitlines():
        parts = line.strip().strip(":").split(":")
        if len(parts) >= 2 and parts[0]:
            out[parts[0].strip()] = parts[1].strip()
        elif len(parts) == 1 and parts[0]:
            out.setdefault("flags", []).append(parts[0].strip())
    return out


def recode_ftp_line(line):
    """A latin-1 decoded FTP line turned back into the text it was meant to be.

    The control connection is read as latin-1 so that no byte can fail to
    decode, which means a line arrives with every byte preserved but any real
    UTF-8 filename mangled. latin-1 maps bytes 0-255 one to one, so re-encoding
    recovers the original bytes exactly and they can then be decoded properly.
    A line that is not valid UTF-8 keeps its latin-1 reading, which is the best
    available guess and never raises.
    """
    if not isinstance(line, str):
        return line
    raw = line.encode("latin-1", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


# --- FTP greeting ----------------------------------------------------------

# 220 webMANftpd 1.47.48q MOD [NTFS:0]
FTP_BANNER = re.compile(r"(?i)webMANftpd\s+v?([\d.]+[a-z]?)")
FTP_NTFS = re.compile(r"(?i)\[\s*NTFS\s*:\s*(\d+)\s*\]")


def parse_ftp_banner(text):
    """The greeting webMANftpd sends before anything is asked of it.

    Worth parsing because it is free: the version and the number of mounted
    NTFS volumes arrive with the connection that was being opened anyway, and
    the NTFS count is the only direct statement of it the console makes
    anywhere. Everything else about NTFS in this tool is inference.
    """
    out = {}
    if not text:
        return out
    out["ftp_banner"] = text.strip()
    match = FTP_BANNER.search(text)
    if match:
        out["ftpd_version"] = match.group(1)
    match = FTP_NTFS.search(text)
    if match:
        out["ntfs_mounts"] = int(match.group(1))
    return out


# --- discovery signature ---------------------------------------------------

# Scored rather than matched on one string, because the root page differs
# enormously between versions and a single marker either misses consoles or
# picks up routers. Two independent markers is the bar.
SIGNATURE_MARKERS = (
    (4, re.compile(r"(?i)webman")),
    (3, re.compile(r"(?i)cpursx\.ps3")),
    (2, re.compile(r"(?i)\.ps3[\"'?>]")),
    (2, re.compile(r"(?i)dev_hdd0")),
    (2, re.compile(r"(?i)\bPS3\b")),
    (1, re.compile(r"(?i)dev_usb\d")),
    (1, re.compile(r"(?i)\bsyscall")),
    (1, re.compile(r"(?i)wm_|wmtoolbar")),
)
SIGNATURE_THRESHOLD = 5


def webman_score(body):
    """Returns (score, markers). At or above SIGNATURE_THRESHOLD it is webMAN."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    score = 0
    markers = []
    for weight, pattern in SIGNATURE_MARKERS:
        match = pattern.search(body or "")
        if match:
            score += weight
            markers.append(match.group(0))
    return score, markers


def looks_like_webman(body):
    return webman_score(body)[0] >= SIGNATURE_THRESHOLD


# --- local addresses -------------------------------------------------------

IPV4 = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b")
IPCONFIG_V4 = re.compile(r"(?i)IPv4 Address[^\n:]*:\s*((?:\d{1,3}\.){3}\d{1,3})")
IP_ADDR_V4 = re.compile(r"(?m)^\s*inet\s+((?:\d{1,3}\.){3}\d{1,3})/(\d{1,2})")


def parse_ipconfig(text):
    """IPv4 addresses out of Windows ipconfig output, loopback dropped."""
    return [address for address in IPCONFIG_V4.findall(text or "")
            if not address.startswith("127.")]


def parse_ip_addr(text):
    """IPv4 addresses out of ip addr output, loopback dropped."""
    return [address for address, _bits in IP_ADDR_V4.findall(text or "")
            if not address.startswith("127.")]


# --- which adapter leads anywhere ------------------------------------------

# A field line in ipconfig is "  Label . . . : value". The space before the
# colon is what separates it from a wrapped IPv6 value underneath it, which
# has colons of its own and no space in front of them.
IPCONFIG_FIELD = re.compile(r"^\s+(?P<label>.+?)\s+:\s?(?P<value>.*)$")
IP_ROUTE_DEFAULT = re.compile(
    r"(?m)^\s*(?:default|0\.0\.0\.0/0)\b(?P<rest>[^\n]*)$")
IP_ROUTE_DEV = re.compile(r"\bdev\s+(\S+)")
IP_ROUTE_VIA = re.compile(r"\bvia\s+((?:\d{1,3}\.){3}\d{1,3})")
IP_ADDR_HEADER = re.compile(r"^\d+:\s*(?P<name>[^:@\s]+)")


def _first_ipv4(text):
    """The first thing in text that is really a dotted quad, or ""."""
    for candidate in IPV4.findall(text or ""):
        if all(int(part) < 256 for part in candidate.split(".")):
            return candidate
    return ""


def parse_ipconfig_adapters(text):
    """Windows ipconfig split per adapter: name, IPv4 addresses, gateway.

    The gateway is the whole point. An adapter with a default gateway is on a
    network that leads somewhere; a Hyper-V, WSL or VMware host-only adapter
    has an address and no gateway at all, and is the one the old picker kept
    choosing. ipconfig prints the IPv6 gateway on the label line and the IPv4
    one on a continuation line below it, so continuations are read as well.
    """
    adapters = []
    current = None
    label = ""
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        if not raw[:1].isspace():
            # An unindented line is an adapter heading, or the "Windows IP
            # Configuration" banner, which simply ends up with no addresses.
            current = {"name": raw.strip().rstrip(":").strip(),
                       "addresses": [], "gateway": ""}
            adapters.append(current)
            label = ""
            continue
        if current is None:
            current = {"name": "", "addresses": [], "gateway": ""}
            adapters.append(current)
        match = IPCONFIG_FIELD.match(raw)
        if match:
            label = match.group("label").strip(" .").lower()
            value = match.group("value")
        else:
            value = raw
        address = _first_ipv4(value)
        if not address or address.startswith("127."):
            continue
        if label.startswith("default gateway"):
            if not current["gateway"] and address != "0.0.0.0":
                current["gateway"] = address
        elif "ipv4 address" in label:
            if address not in current["addresses"]:
                current["addresses"].append(address)
    return [adapter for adapter in adapters
            if adapter["addresses"] or adapter["gateway"]]


def parse_ip_addr_devices(text):
    """ip addr output as [{"name", "addresses"}], loopback dropped.

    Grouped by device because the default route names a device, and matching
    it back to an address is the only way to know which address it belongs to.
    """
    devices = []
    current = None
    for raw in (text or "").splitlines():
        header = (IP_ADDR_HEADER.match(raw.strip())
                  if raw[:1].isdigit() else None)
        if header:
            current = {"name": header.group("name"), "addresses": []}
            devices.append(current)
            continue
        found = IP_ADDR_V4.match(raw)
        if not found or current is None:
            continue
        address = found.group(1)
        if address.startswith("127.") or address in current["addresses"]:
            continue
        current["addresses"].append(address)
    return [device for device in devices if device["addresses"]]


def parse_ip_route_default(text):
    """Default routes as [{"device", "gateway"}], in the order ip printed them.

    A default route with no via, which is how a point-to-point VPN prints, is
    still a default route, so the gateway comes back empty rather than the
    whole entry being dropped.
    """
    routes = []
    for match in IP_ROUTE_DEFAULT.finditer(text or ""):
        rest = match.group("rest")
        device = IP_ROUTE_DEV.search(rest)
        if not device:
            continue
        via = IP_ROUTE_VIA.search(rest)
        routes.append({"device": device.group(1),
                       "gateway": via.group(1) if via else ""})
    return routes
