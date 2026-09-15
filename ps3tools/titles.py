"""The title table: which releases this tool recognises, and with what.

Two different questions are answered here and they must not be run together.

  Recognised   the title ID is one of the published releases of one of the two
               games. That is enough to detect it, name it on screen, and
               attempt the fix on it. The lists below are every title ID either
               game shipped under.

  Verified     somebody has actually watched the fix work on that release, and
               the update-package hashes for it have been read off Sony's live
               manifests. Only a handful of releases are in that position, and
               they are the ones with a `skus` entry.

An unverified release is attempted rather than refused, because the attempt
cannot go wrong quietly. The klicensee is either right, in which case scetool
hands back a decrypted binary whose patch site is then cross-checked against
the offset in PATCH_SITES, or it is wrong, in which case scetool produces
nothing at all and the run stops there having read the file and written
nothing. There is no third outcome where a wrong key produces a plausible
binary. The signing parameters that rebuild the file are read back off the
user's own copy rather than looked up here, so nothing about re-signing needs
a per-release table either.

What stays forbidden is patching at an offset nobody has checked. The fix finds
its own site in the decrypted image and that answer has to agree with the
verified offset below; a disagreement is a different build of the game and is
refused.

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
           "campaign and Spec Ops; unaffected"),
)


# --- which releases are which game -----------------------------------------
#
# Every title ID each game was published under, normalised the way the rest of
# the program spells them: upper case, no dash. Being in one of these lists
# means the tool will say what the game is and will attempt the fix on it. It
# does not mean anybody has watched the fix work on that release; that is the
# skus table further down, and the two are deliberately separate.
#
# These are release identifiers and nothing more. Which of them are the full
# game and which are something smaller is not recorded, because it is not known
# here and a guess written down as a fact would be read as one.

BO2_TITLE_IDS = (
    "BCKS10223", "BCKS10232", "BCUS91450",
    "BLES01717", "BLES01718", "BLES01719", "BLES01720",
    "BLJM60548", "BLJM60549", "BLJM61109", "BLJM61110", "BLJM61230",
    "BLJM61231",
    "BLUS31011", "BLUS31080", "BLUS31140", "BLUS31141", "BLUS41005",
    "NPEB01204", "NPEB01205", "NPEB01206", "NPEB01207",
    # NPUB31054 was missing while 31055 and 31056 were here. It is the North
    # American digital release and it is the copy somebody was playing: his
    # console held it with all three binaries in its USRDIR, beside a leftover
    # BLUS31011 folder holding licence files and nothing else. The tool read
    # the leftover and told him his title update had not been downloaded.
    "NPUB31054", "NPUB31055", "NPUB31056",
)

MW3_TITLE_IDS = (
    "BCKS10195",
    "BLES01428", "BLES01429", "BLES01430", "BLES01431", "BLES01432",
    "BLES01433", "BLES01434",
    "BLJM60404", "BLJM60422", "BLJM60534", "BLJM60535", "BLJM61111",
    "BLJM61112",
    "BLUS30838", "BLUS30872", "BLUS30887",
    "NPEB00964", "NPEB00965", "NPEB00966", "NPEB00967", "NPEB00968",
    "NPEB00977", "NPEB00978",
    "NPEB90450", "NPEB90451",
    "NPUB30787", "NPUB30788",
)

# BLJM61034 was in an earlier draft of this table. Sony's manifest returns
# nothing for it: it is not a Black Ops II title ID and it is not included.
NOT_A_TITLE = ("BLJM61034",)


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
        "title_ids": BO2_TITLE_IDS,
        "latest_update": "1.19",
        # The build the patch offset was confirmed on. Kept separate from
        # latest_update even though the two say the same thing today: they
        # answer different questions and they will part company the day Sony
        # ships another update. latest_update is read off a manifest and says
        # what is being served; this one says what somebody watched the fix
        # work on, and is the only one a refusal may be based on. It changes
        # when a person verifies another build and never automatically.
        "verified_update": "1.19",
        "repo": "https://github.com/setsid/bo2-ps3-psn-freeze-fix",
        "symptom": ("The game freezes on PS3 while a PSN session is active. "
                    "It is a logging fault in the game. Your connection is "
                    "fine and your account has not been banned."),
        # The "fake signed, so syscalls must be on" line that used to be here
        # described TrueAncestor's output, not this tool's. Recovering the
        # NPDRM control block and application type off the user's own files is
        # the whole point: the result is a properly re-signed binary that boots
        # where the original did. Saying otherwise sends somebody whose file
        # will not start off switching syscalls on instead of looking at the
        # real cause.
        # What the set has to end up as, for a console where none of it is
        # done yet. A console with some of the three already fixed is told
        # what remains instead, worked out from the scan in the patcher
        # screen: this sentence there said three writes were needed while
        # the table above it showed two of them already made.
        "set_advice": ("All three files carry the binary and all three have "
                       "to end up fixed. Patching only the multiplayer one "
                       "leaves campaign and zombies freezing."),
        "advice": ("Each file is re-signed using the content ID, application "
                   "type and key revision read back off your own copy, so it "
                   "boots the way the original did rather than depending on "
                   "the extra custom firmware controls being left switched "
                   "on. Keep the backups: they are the only way back, and an "
                   "original cannot be rebuilt from a patched copy."),
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
        "title_ids": MW3_TITLE_IDS,
        "latest_update": "1.24",
        # See the note on the same key above. Verified, not latest.
        "verified_update": "1.24",
        "repo": "https://github.com/setsid/mw3-ps3-psn-fix",
        "symptom": ("You reach a multiplayer lobby and are dropped back to "
                    "the menu about a second later, on any PSN account made "
                    "after late 2018. Campaign and Spec Ops are unaffected."),
        # See the note on the same key above. One file carries the fault
        # here, so the set is one file wide.
        "set_advice": ("Only the multiplayer binary is changed. default.self "
                       "is campaign and Spec Ops and is left alone."),
        "advice": ("The file is re-signed using the content ID, application "
                   "type and key revision read back off your own copy, so it "
                   "boots the way the original did rather than depending on "
                   "the extra custom firmware controls being left switched "
                   "on. Keep the backup: it is the only way back, and the "
                   "original cannot be rebuilt from the patched copy."),
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

# Every title ID this tool recognises as one of the two games. Being in here is
# what gets a release detected, named, and attempted.
KNOWN_TITLE_IDS = {
    title_id: config["key"]
    for config in TITLES.values()
    for title_id in config["title_ids"]
    if title_id not in NOT_A_TITLE
}

# The releases a working patch has actually been observed on, which is a much
# shorter list. Kept apart from the one above so that a screen or a report can
# tell the user which of the two situations they are in rather than implying
# that every recognised release is a tested one.
VERIFIED_TITLE_IDS = {
    title_id: config["key"]
    for config in TITLES.values()
    for title_id in config["skus"]
}

USRDIR = "/dev_hdd0/game/{title_id}/USRDIR"


def normalise(title_id):
    """A title ID the way this table spells them: upper case, no dash.

    Users copy them off a box, a disc or a web page, where they are as often
    BLES-01717 as BLES01717, and a lookup that missed on the punctuation would
    tell somebody their own game was not recognised.
    """
    return "".join(character for character in (title_id or "").upper()
                   if character.isalnum())


def config_for(title_id):
    """The title configuration for a title ID, or None if it is not one of ours.

    None means this is not a release of either game, so nothing is read and
    nothing is attempted. It is not a statement about signing parameters: a
    recognised release whose klicensee turns out to be different is found out
    at the decryption step, where the failure is plain and costs nothing.
    """
    key = KNOWN_TITLE_IDS.get(normalise(title_id))
    return TITLES.get(key) if key else None


def is_recognised(title_id):
    """Whether this title ID is a release of either game."""
    return normalise(title_id) in KNOWN_TITLE_IDS


def is_verified(title_id):
    """Whether the fix has actually been observed working on this release.

    False is not a refusal. It means nobody has reported back on this one, so
    the tool will attempt it and say so rather than claim more than it knows.
    """
    return normalise(title_id) in VERIFIED_TITLE_IDS


def sku_for(title_id):
    """The verified SKU record for a title ID, or None if it is not verified.

    A recognised release with no record here is the ordinary case now. Anything
    reading this has to treat a missing record as "not established" rather than
    as "not a real title".
    """
    config = config_for(title_id)
    if not config:
        return None
    return config["skus"].get(normalise(title_id))


def verified_update_for(title_id):
    """The title update the fix was confirmed on for this release, or None.

    None is the ordinary answer for a release nobody has verified, and it means
    "there is nothing to compare against" rather than "the installed update is
    wrong". Anything acting on this has to keep those apart: a user on an
    unverified release told their title update is out of date has been told
    something this table does not know.

    Never the latest update. The two happen to coincide for both games today,
    but the question this answers is which build the patch offset was proved
    against, and that changes only when a person verifies another one.
    """
    sku = sku_for(title_id)
    if not sku:
        return None
    # A per-SKU override is allowed for the day a region is verified on a
    # different build from the rest of its game.
    config = config_for(title_id)
    return sku.get("verified_update") or (config or {}).get("verified_update")


def site_for(binary_record):
    return PATCH_SITES.get(binary_record.get("site")) if binary_record else None


def binaries_for(title_id):
    config = config_for(title_id)
    return config["binaries"] if config else ()


def usrdir_for(title_id):
    return USRDIR.format(title_id=normalise(title_id))


def update_for(title_id, sha1):
    """Which title update a package hash corresponds to, or None.

    Only ever used to tell the user which TU they have. It says nothing about
    whether anything has been patched.
    """
    sku = sku_for(title_id)
    if not sku:
        # No verified record for this release, so there is no table of update
        # hashes to match against and nothing can be said about which title
        # update is installed. Silence is the correct answer here.
        return None
    wanted = (sha1 or "").lower()
    for version, digest in sku.get("updates", {}).items():
        if digest == wanted:
            return version
    return None
