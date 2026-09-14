"""The known-good table: which titles this tool will touch, and with what.

Everything here was verified against a real console or against Sony's live
update manifests. It is not to be re-derived, extended by pattern, or guessed
at. A title ID that is not in this table is refused rather than patched with
another region's parameters, because whether the klicensee is constant across
regional SKUs is not established. The BO2 repository states that it is; that is
one person's observation across the regions they had, and it is not enough to
risk re-signing a stranger's binary with the wrong key.

Two kinds of hash live here and they must never be confused:

  Update package sha1  identifies which title update is installed. It is the
                       hash of the PKG Sony serves, not of anything on the
                       console's disk. It says which TU the user is on. It can
                       never say whether a file has been patched.

  Patch state          is read from the bytes at a known offset in the
                       decrypted binary, and from nothing else. Never from a
                       file size, never from a hash of a SELF.
"""

# Where the update manifests were read from, recorded so the next person knows
# where the numbers came from. This tool does not fetch it and must not.
UPDATE_MANIFEST_URL = ("https://a0.ww.np.dl.playstation.net/tpl/np/"
                       "{title_id}/{title_id}-ver.xml")

# scetool takes the klicensee as hex on the command line. EBOOT.BIN is a free
# SELF and needs none; the two t6 selfs are spawned by the game with this one,
# so scetool needs it to decrypt or re-sign them.
BO2_KLICENSEE = "8C10AC1473DF38ADD7A4F2EE8C838DAB"
# InfinityWardKey as raw ASCII with a trailing NUL.
MW3_KLICENSEE = "496E66696E697479576172644B657900"

FREE = None

# --- patch sites -----------------------------------------------------------
#
# A site is expressed against the DECRYPTED image, which is what the patchers
# operate on. vaddr is what a disassembler shows; file_offset is where the
# bytes actually are. Both are recorded because a mismatch between them is the
# easiest way to be reading the wrong thing and not notice.
#
# find_site / find_call in the existing patchers locate these by instruction
# pattern rather than by offset, and remain the authority. The offsets here are
# for cross-checking what those functions return, and for reading the four
# bytes during a scan without running a full patch.

PATCH_SITES = {
    # EBOOT.BIN and t6_ps3f.self decrypt to the same image, so they share this.
    "bo2-campaign": {
        "image": "v119.elf",
        "vaddr": 0x004A80F0,
        "file_offset": 0x004980F0,
        "stock_is": "bl",
        "patched": b"\x60\x00\x00\x00",
        "note": "a bl to the CRM logging call, replaced with a nop",
    },
    "bo2-multiplayer": {
        "image": "mp.elf",
        "vaddr": 0x0050B414,
        "file_offset": 0x004FB414,
        "stock_is": "bl",
        "patched": b"\x60\x00\x00\x00",
        "note": "a bl to the CRM logging call, replaced with a nop",
    },
    "mw3-multiplayer": {
        "image": "default_mp.elf",
        "vaddr": 0x00340D20,
        "file_offset": 0x00330D20,
        "stock": b"\x60\x7C\x00\x00",
        "patched": b"\x3B\x80\x00\x00",
        "note": "the account identifier comparison, forced to the good path",
    },
}

# A bl is 18 in the top six bits with the link bit set, so the stock value is
# not one constant. Scanning compares against the patched value and falls back
# to asking the patcher's own finder, which is why stock_is is a word rather
# than bytes for the two BO2 sites.
NOP = b"\x60\x00\x00\x00"


# --- binaries --------------------------------------------------------------

def binary(name, klicensee, site, image, purpose):
    return {"name": name, "klicensee": klicensee, "site": site,
            "image": image, "purpose": purpose}


BO2_BINARIES = (
    binary("EBOOT.BIN", FREE, "bo2-campaign", "v119.elf",
           "campaign and zombies"),
    binary("t6_ps3f.self", BO2_KLICENSEE, "bo2-campaign", "v119.elf",
           "campaign and zombies, the copy the game actually spawns"),
    binary("t6mp_ps3f.self", BO2_KLICENSEE, "bo2-multiplayer", "mp.elf",
           "multiplayer"),
)

MW3_BINARIES = (
    binary("default_mp.self", MW3_KLICENSEE, "mw3-multiplayer",
           "default_mp.elf", "multiplayer"),
    # Listed so that a scan reports on it rather than silently ignoring a file
    # sitting next to the one being changed. It has no patch site: the fault is
    # not in it and the fix does not touch it.
    binary("default.self", MW3_KLICENSEE, None, None,
           "campaign and Spec Ops, not affected"),
)


# --- titles ----------------------------------------------------------------
#
# updates maps a title update version to the sha1 of Sony's package for that
# SKU. Read from the live manifests. Used to tell the user which TU they are on
# and which is current. Never used for patch state.

TITLES = {
    "bo2": {
        "key": "bo2",
        "name": "Call of Duty: Black Ops II",
        "short": "Black Ops II",
        "binaries": BO2_BINARIES,
        "latest_update": "1.19",
        "repo": "https://github.com/setsid/bo2-ps3-psn-freeze-fix",
        "symptom": ("The game freezes on PS3 while a PSN session is active. "
                    "It is a logging fault in the game, not a network "
                    "problem and not a ban."),
        # The "fake signed, so syscalls must be on" line that used to be here
        # described TrueAncestor's output, not this tool's. Recovering the
        # NPDRM control block and application type off the user's own files is
        # the whole point: the result is a properly re-signed binary that boots
        # where the original did. Saying otherwise sends somebody whose file
        # will not start off switching syscalls on instead of looking at the
        # real cause.
        "advice": ("All three files carry the binary and all three have to be "
                   "done. Patching only the multiplayer one leaves campaign "
                   "and zombies freezing. Each file is re-signed using the "
                   "content ID, application type and key revision read back "
                   "off your own copy, so it boots the way the original did "
                   "rather than depending on the extra custom firmware "
                   "controls being left switched on. Keep the backups: they "
                   "are the only way back, and an original cannot be rebuilt "
                   "from a patched copy."),
        "skus": {
            "BLUS31011": {"region": "North America",
                          "updates": {
                              "1.09": "a3557b87227c2c65193ecb2e9d637ed8"
                                      "da6e731e",
                              "1.19": "0aad7f0a4777f6ce82a3687d8ebc0f8d"
                                      "37bff6a9"}},
            "BLES01717": {"region": "Europe",
                          "updates": {
                              "1.09": "2fb88fe3bb68158b885a45c509be2355"
                                      "abdb8b97",
                              "1.19": "87c75a80a1df1518b5b2212c3c258ac7"
                                      "ef9094ac"}},
            "BLES01718": {"region": "Europe",
                          "updates": {
                              "1.09": "56d474a8e10f5b9114660ad8cd6b70b8"
                                      "4eb29517",
                              "1.19": "72269223d779d3c7dff0d5dbe86af17a"
                                      "9b807f1e"}},
            "BLUS31140": {"region": "North America",
                          "updates": {
                              "1.09": "e7ef3da3cb55f3931f5da20ba4871b8f"
                                      "c1cd667e",
                              "1.19": "af062f37bf01b04f5712cef9a7e7f931"
                                      "c9646458"}},
        },
    },
    "mw3": {
        "key": "mw3",
        "name": "Call of Duty: Modern Warfare 3",
        "short": "Modern Warfare 3",
        "binaries": MW3_BINARIES,
        "latest_update": "1.24",
        "repo": "https://github.com/setsid/mw3-ps3-psn-fix",
        "symptom": ("You reach a multiplayer lobby and are dropped back to "
                    "the menu about a second later, on any PSN account made "
                    "after late 2018. Campaign and Spec Ops are unaffected."),
        "advice": ("Only the multiplayer binary is changed. default.self is "
                   "campaign and Spec Ops and is left alone. The file is "
                   "re-signed using the content ID, application type and key "
                   "revision read back off your own copy, so it boots the way "
                   "the original did rather than depending on the extra "
                   "custom firmware controls being left switched on. Keep the "
                   "backup: it is the only way back, and the original cannot "
                   "be rebuilt from the patched copy."),
        "skus": {
            "BLUS30838": {"region": "North America",
                          "updates": {"1.24": "fc183a0e17a452dbcf492431668b"
                                              "1c96efbdc54c"}},
            "BLES01428": {"region": "Europe",
                          "updates": {"1.24": "a1fcbcb753bf36b4748fb8104699"
                                              "c56d9b6cc98c"}},
        },
    },
}

# BLJM61034 was in an earlier draft of this table. Sony's manifest returns
# nothing for it: it is not a Black Ops II title ID and it is not included.
NOT_A_TITLE = ("BLJM61034",)

# Every title ID this tool will act on, and nothing else.
KNOWN_TITLE_IDS = {
    title_id: config["key"]
    for config in TITLES.values()
    for title_id in config["skus"]
}

USRDIR = "/dev_hdd0/game/{title_id}/USRDIR"


def config_for(title_id):
    """The title configuration for a title ID, or None if it is not known.

    None means refuse. It does not mean "work it out from the region letter":
    the signing parameters are per SKU until somebody establishes otherwise,
    and re-signing with the wrong ones produces a binary that will not boot.
    """
    key = KNOWN_TITLE_IDS.get((title_id or "").upper())
    return TITLES.get(key) if key else None


def sku_for(title_id):
    config = config_for(title_id)
    if not config:
        return None
    return config["skus"].get(title_id.upper())


def site_for(binary_record):
    return PATCH_SITES.get(binary_record.get("site")) if binary_record else None


def binaries_for(title_id):
    config = config_for(title_id)
    return config["binaries"] if config else ()


def usrdir_for(title_id):
    return USRDIR.format(title_id=title_id.upper())


def update_for(title_id, sha1):
    """Which title update a package hash corresponds to, or None.

    Only ever used to tell the user which TU they have. It says nothing about
    whether anything has been patched.
    """
    sku = sku_for(title_id)
    if not sku:
        return None
    wanted = (sha1 or "").lower()
    for version, digest in sku.get("updates", {}).items():
        if digest == wanted:
            return version
    return None
