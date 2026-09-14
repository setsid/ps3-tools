"""Turns title IDs and console model numbers into something a helper can read.

All of this is offline reasoning over strings that are already in the listings.
Nothing here asks the console anything.

Two separate schemes are decoded:

  Title IDs   BLES01428, NPEB02143, SLUS20946, ULES00124 and friends. The
              region character is the reliable part and is what the report
              leads with. The category character is indicative only and is
              always printed alongside the raw letter so a helper can second
              guess it.

  Model codes CECH-2503B. The fourth digit is the sales region and the trailing
              letter is the hard disk the console shipped with, which together
              answer "which shop did this come from" without touching the
              console's own identifiers.
"""

import re

# --- title IDs -------------------------------------------------------------

# Third character of a PS3 or PSP title ID.
PS3_REGIONS = {
    "E": ("Europe", "PAL"),
    "U": ("North America", "NTSC-U"),
    "J": ("Japan", "NTSC-J"),
    "H": ("Asia", "NTSC-J"),
    "A": ("Asia", "NTSC-J"),
    "K": ("Korea", "NTSC-K"),
    "C": ("China", "NTSC-J"),
    "I": ("Internal or press", "unknown"),
}

# Third character of a PS1 or PS2 title ID. P is Japan here, not PAL, which is
# the single easiest mistake to make reading these.
PS2_REGIONS = {
    "E": ("Europe", "PAL"),
    "U": ("North America", "NTSC-U"),
    "P": ("Japan", "NTSC-J"),
    "J": ("Japan", "NTSC-J"),
    "K": ("Korea", "NTSC-K"),
    "A": ("Asia", "NTSC-J"),
    "I": ("Internal or press", "unknown"),
}

# Fourth character. Best effort, and labelled as such wherever it is printed.
PS3_CATEGORIES = {
    "A": "retail game, Sony published",
    "B": "retail game, third party published",
    "C": "retail game",
    "D": "demo or trial",
    "E": "add-on content",
    "F": "PSOne or PS2 Classic",
    "G": "PSP or minis title",
    "H": "add-on content",
    "I": "internal or test",
    "J": "PSOne Classic",
    "K": "add-on content",
    "M": "video or non-game",
    "S": "retail disc",
    "W": "theme or non-game",
    "X": "promotional",
    "Z": "not for resale",
}

PATTERNS = (
    # PS3 retail disc: BLES01428, BCUS98114, BLJM60123.
    ("PS3 disc", re.compile(r"\bB[CL][EUJAHKCI][SM]\d{5}\b"), PS3_REGIONS),
    # PS3 digital, PSN: NPEB02143, NPUA80145.
    ("PS3 digital", re.compile(r"\bNP[EUJAHKCI][A-Z]\d{5}\b"), PS3_REGIONS),
    # PSP UMD and digital: ULES00124, UCUS98615.
    ("PSP", re.compile(r"\bU[CL][EUJAHKCI][SMB]\d{5}\b"), PS3_REGIONS),
    # PS2 and PSOne: SLES50916, SCUS97129, SLPM66013.
    ("PS2 or PSOne", re.compile(r"\bS[CL][EUPJKAI][SMD]\d{5}\b"), PS2_REGIONS),
)

# Some dumps write the PS1 and PS2 style with a dash. Normalised before matching
# so SLUS-20946 and SLUS20946 give the same answer.
_DASHED = re.compile(r"\b([A-Z]{4})[-_](\d{5})\b")


def find_title_id(name):
    """The first title ID in a file or folder name, or None.

    Names in these listings routinely carry the ID in brackets after the title,
    sometimes twice, sometimes with a dash. The first match is used because the
    convention is [ID] immediately after the name and anything later tends to be
    a patch or a duplicate marker.
    """
    if not name:
        return None
    upper = _DASHED.sub(r"\1\2", name.upper())
    best = None
    for _kind, pattern, _regions in PATTERNS:
        match = pattern.search(upper)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), match.group(0))
    return best[1] if best else None


def describe_title_id(title_id):
    """Everything the ID itself says. Unknown fields come back as None rather
    than a guess, so the report can show a blank instead of inventing a region.
    """
    out = {
        "title_id": title_id,
        "platform": None,
        "region": None,
        "video_standard": None,
        "media": None,
        "category": None,
        "category_letter": None,
        "publisher_class": None,
    }
    if not title_id:
        return out
    code = title_id.upper()
    for kind, pattern, regions in PATTERNS:
        if not pattern.fullmatch(code):
            continue
        out["platform"] = kind
        region, standard = regions.get(code[2], (None, None))
        out["region"] = region
        out["video_standard"] = standard
        out["category_letter"] = code[3]
        if kind == "PS3 digital":
            out["media"] = "download"
            out["category"] = PS3_CATEGORIES.get(code[3])
        elif kind == "PS3 disc":
            out["media"] = "Blu-ray disc"
            out["category"] = "retail game"
            out["publisher_class"] = ("Sony published" if code[1] == "C"
                                      else "third party published")
        elif kind == "PSP":
            out["media"] = "UMD or download"
            out["publisher_class"] = ("Sony published" if code[1] == "C"
                                      else "third party published")
        elif kind == "PS2 or PSOne":
            out["media"] = "disc"
            out["category"] = "demo" if code[3] == "D" else "retail game"
            out["publisher_class"] = ("Sony published" if code[1] == "C"
                                      else "third party published")
        break
    return out


def describe_name(name):
    """describe_title_id for a raw listing entry."""
    return describe_title_id(find_title_id(name))


# --- console model ---------------------------------------------------------

# Fourth digit of a CECH model number. This is the sales region, which is not
# the same thing as the firmware region: a UK console can run any region's
# firmware, and both are reported separately for that reason.
MODEL_REGIONS = {
    "0": "Japan",
    "1": "USA and Canada",
    "2": "Australia and New Zealand",
    "3": "United Kingdom and Ireland",
    "4": "Europe, Middle East and Africa",
    "5": "South Korea",
    "6": "Hong Kong and Singapore",
    "7": "Taiwan",
    "8": "Russia",
    "9": "China",
}

# Trailing letter. The capacity the console left the factory with, which is
# worth printing next to the actual disk size because a mismatch is the
# quickest way to spot a drive that has been swapped.
MODEL_CAPACITY = {
    "A": "20 GB or 120 GB as shipped",
    "B": "60 GB or 250 GB as shipped",
    "C": "80 GB or 320 GB as shipped",
    "D": "40 GB or 500 GB as shipped",
    "E": "160 GB as shipped",
    "F": "12 GB flash as shipped",
    "G": "500 GB as shipped",
}

MODEL = re.compile(r"\bCECH[-\s]?([A-Z]?\d{2,4})([A-Z])?\b")


def describe_model(text):
    """Pulls CECH-2503B out of whatever it is embedded in and unpacks it."""
    out = {
        "model": None,
        "generation": None,
        "sales_region": None,
        "shipped_capacity": None,
        "ps2_compatibility": None,
    }
    if not text:
        return out
    match = MODEL.search(text.upper())
    if not match:
        return out
    digits, suffix = match.group(1), match.group(2) or ""
    out["model"] = f"CECH-{digits}{suffix}"
    if digits[0].isalpha():
        # CECHA00, CECHC04 and so on: the launch consoles, before the numeric
        # scheme started.
        out["model"] = f"CECH{digits}{suffix}"
        out["generation"] = "original (fat)"
        letter = digits[0]
        if letter in "AB":
            out["ps2_compatibility"] = "full, PS2 hardware fitted"
        elif letter in "CE":
            out["ps2_compatibility"] = "partial, software emulation"
        else:
            out["ps2_compatibility"] = "none"
        return out
    series = int(digits[:2]) if len(digits) >= 2 else 0
    if series < 20:
        out["generation"] = "original (fat)"
        out["ps2_compatibility"] = "none"
    elif series < 40:
        out["generation"] = "slim"
        out["ps2_compatibility"] = "none"
    else:
        out["generation"] = "super slim"
        out["ps2_compatibility"] = "none"
    if len(digits) >= 4:
        out["sales_region"] = MODEL_REGIONS.get(digits[3])
    if suffix:
        out["shipped_capacity"] = MODEL_CAPACITY.get(suffix)
    return out


def tally_regions(names):
    """Counts titles by region across a listing, for the digest.

    Returns (counts, unknown), counts keyed by region name and unknown being the
    number of entries with no recognisable title ID. Those are usually renamed
    folders rather than anything wrong, which the report says.
    """
    counts = {}
    unknown = 0
    for name in names:
        info = describe_name(name)
        if info["region"]:
            counts[info["region"]] = counts.get(info["region"], 0) + 1
        else:
            unknown += 1
    return counts, unknown
