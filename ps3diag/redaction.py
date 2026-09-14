"""Replaces console identifiers with stable placeholders.

Stable is the requirement that shapes this. A helper comparing today's zip
against one from last week needs to see that it is the same console, so the
placeholder is derived from the value rather than being a fixed string. It is a
one way digest with a fixed salt, so the same IDPS always becomes the same
placeholder on any machine, and the placeholder cannot be turned back into the
IDPS.

The salt is public and in this file on purpose. It is not a secret and pretending
otherwise would only mislead: the point is correlation between runs, not
resistance to someone who already knows the identifier they are looking for.
"""

import hashlib
import re

SALT = "ps3-diag/1"

# Kinds are ordered most specific first. A labelled IDPS must win over the bare
# 32 hex pattern below it, otherwise the same value gets two placeholders
# depending on which endpoint it came from and the correlation breaks.
_HEX = "[0-9A-Fa-f]"

RULES = (
    # label: IDPS, then up to a few characters of markup or punctuation, then
    # the value. webMAN prints these inside tables, so "</td><td>" sits between
    # the label and the value more often than a colon does.
    ("IDPS", re.compile(r"(?i)\bIDPS\b[\s:=]*(?:<[^>]*>[\s:=]*)*((?:0x)?"
                        + _HEX + r"{32})")),
    ("PSID", re.compile(r"(?i)\bPSID\b[\s:=]*(?:<[^>]*>[\s:=]*)*((?:0x)?"
                        + _HEX + r"{32})")),
    # Unlabelled. Every retail IDPS begins 0000000100 and no other 32 hex value
    # webMAN prints does, so this catches the ones printed bare without eating
    # hashes that happen to be the same length.
    ("IDPS", re.compile(r"\b(0000000100" + _HEX + r"{22})\b")),
    ("ACCOUNT", re.compile(r"(?i)\b(?:account[\s_-]?id|accountid|np[\s_-]?"
                           r"account)\b[\s:=]*(?:<[^>]*>[\s:=]*)*(" + _HEX
                           + r"{16})")),
    ("MAC", re.compile(r"\b((?:" + _HEX + r"{2}[:-]){5}" + _HEX + r"{2})\b")),
    # webMAN prints the MAC without separators in a couple of places.
    ("MAC", re.compile(r"(?i)\bMAC(?:\s*address)?\b[\s:=]*(?:<[^>]*>[\s:=]*)*"
                       r"(" + _HEX + r"{12})\b")),
    ("EMAIL", re.compile(r"\b([\w.+-]+@[\w-]+\.[\w.-]*\w)\b")),
    ("ONLINE-ID", re.compile(r"(?i)\b(?:online[\s_-]?id|psn[\s_-]?id|np[\s_-]?"
                             r"id|sign[\s_-]?in[\s_-]?id|user(?:name)?)\b"
                             r"[\s:=]*(?:<[^>]*>[\s:=]*)*([A-Za-z0-9_-]{3,32})")),
    ("CONSOLE-NAME", re.compile(r"(?i)\b(?:console[\s_-]?(?:name|nick)|"
                                r"nickname)\b[\s:=]*(?:<[^>]*>[\s:=]*)*"
                                r"([^\r\n<]{1,48})")),
)

# Values that match ONLINE-ID or CONSOLE-NAME but are plainly not identifiers.
# The label patterns are loose on purpose so a version of webMAN nobody here has
# seen still gets caught, and the cost of that is a short list of false hits.
NOT_IDENTIFIERS = {"none", "null", "n/a", "na", "unknown", "unset", "default",
                   "not set", "-", "--", "empty", "root", "anonymous", "user",
                   "admin", "guest", "ps3", "webman", "disabled", "enabled"}


def placeholder(kind, value):
    """The stable stand-in for one value. Same input, same output, always."""
    normalised = value.strip().lower().removeprefix("0x")
    if kind == "MAC":
        normalised = normalised.replace(":", "").replace("-", "")
    digest = hashlib.sha256(
        f"{SALT}:{kind}:{normalised}".encode("utf-8")).hexdigest()
    return f"[{kind}-{digest[:8]}]"


def redact(text, include_identifiers=False):
    """Returns (text, findings).

    findings maps placeholder to kind, so the caller can report how many of each
    were replaced without holding on to any of the originals. With
    include_identifiers set nothing is replaced, but the findings are still
    collected: the summary says what is in the file either way.
    """
    if not text:
        return text, {}
    findings = {}
    result = text
    for kind, pattern in RULES:
        def swap(match, kind=kind):
            value = match.group(1)
            if value.strip().lower() in NOT_IDENTIFIERS:
                return match.group(0)
            stand_in = placeholder(kind, value)
            findings[stand_in] = kind
            if include_identifiers:
                return match.group(0)
            start, end = match.span(1)
            return (match.group(0)[:start - match.start()] + stand_in
                    + match.group(0)[end - match.start():])
        result = pattern.sub(swap, result)
    return result, findings


def summarise(findings):
    """Counts by kind, for the line in summary.txt that says what was hidden."""
    counts = {}
    for kind in findings.values():
        counts[kind] = counts.get(kind, 0) + 1
    return counts
