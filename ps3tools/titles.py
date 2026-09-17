"""The title table: which releases this tool recognises, and with what.

Two different questions are answered here and they must not be run together.

  Recognised   the title ID is one of the releases this tool has looked at.
               That is enough to detect it, name it on screen, and attempt the
               fix on it. For Black Ops II and Modern Warfare 3 the lists below
               are every title ID either game shipped under; for Black Ops 1
               they are the three builds that were actually compared.

  Verified     somebody has actually watched the fix work on that release, and
               the update-package hashes for it have been read off Sony's live
               manifests. Only a handful of releases are in that position, and
               they are the ones with a `skus` entry.

An unverified release is attempted rather than refused, because the attempt
cannot go wrong quietly. The klicensee is either right, in which case scetool
hands back a decrypted binary whose patch site is then checked, or it is wrong,
in which case scetool produces nothing at all and the run stops there having
read the file and written nothing. There is no third outcome where a wrong key
produces a plausible binary. The signing parameters that rebuild the file are
read back off the user's own copy rather than looked up here, so nothing about
re-signing needs a per-release table either.

What stays forbidden is patching at a site nobody has checked, and there are
two ways a site is checked.

  By offset    Black Ops II and Modern Warfare 3. The fix finds its own site in
               the decrypted image and that answer has to agree with the
               verified offset below. A disagreement is a different build of
               the game and is refused.

  By pattern   Black Ops 1, whose two known builds put the same code at
               different addresses, so there is no offset either of them could
               be checked against. Its site records none, and what takes the
               place of the agreement is inside patch-bo1.py: two independent
               signatures that have to land on the same function, imports
               resolved the way the loader resolves them, and a refusal
               wherever anything matches twice or not at all.

Two kinds of hash live here and they must never be confused:

  Update package sha1  identifies which title update is installed. It is the
                       hash of the PKG Sony serves, not of anything on the
                       console's disk. It says which TU the user is on. It can
                       never say whether a file has been patched.

  Patch state          is read from the bytes of the decrypted binary, at a
                       known offset where there is one and at the site the fix
                       finds for itself where there is not. Never from a file
                       size, never from a hash of a SELF.
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
# Black Ops 1's multiplayer binary.
BO1_KLICENSEE = "AF0A8F0A8909F09234091AFADF909AF0"

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
    # The one site with no numbers in it, and the reason the shape of this
    # table had to grow a second kind.
    #
    # Three builds of Black Ops 1 were compared. The two disc releases,
    # BLES01031 and BLUS30591, decrypt to a byte-identical image, so region on
    # its own changes nothing. The digital release NPEB00756 is a different
    # compile: the site happens to land at the same address and everything
    # after it has moved, code by 0x238 and data by 0x240. No single offset
    # covers both, so this site records none and the fix finds every address
    # it needs in the image it was handed.
    #
    # What replaces the cross-check against a recorded offset is written in
    # patch-bo1.py: two independent signatures have to agree on one function,
    # the imports are resolved the way the loader resolves them, and anything
    # that matches twice or not at all is refused rather than guessed at.
    "bo1-multiplayer": {
        "image": "t5mp.elf",
        "located_by": "pattern",
        "note": ("the call that hashes the online ID, sent into a code cave "
                 "that hashes the account ID instead"),
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

BO1_BINARIES = (
    binary("t5mp_ps3f.self", BO1_KLICENSEE, "bo1-multiplayer", "t5mp.elf",
           "multiplayer"),
    # Listed so a scan names them rather than passing over two files sitting
    # beside the one being changed. Neither has a patch site: the identity is
    # only asked for online, and neither of these goes online.
    binary("t5_ps3f.self", BO1_KLICENSEE, None, None,
           "campaign and zombies; unaffected"),
    binary("EBOOT.BIN", FREE, None, None,
           "the launcher; unaffected"),
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

# The same checks were run over these two and neither had the gap Black Ops
# had: the European language spread is complete in both, and every disc and
# digital release confirmed by Sony's manifest was already here.
#
# Three entries here could not be confirmed in any source, including Sony's
# own manifest: BLUS30872, NPEB90450 and NPEB90451, where only NPEB90449
# exists as the European demo. They are left in place rather than removed,
# because a title ID that matches nothing costs nothing and taking one out on
# a negative result is how a real release stops being recognised. They are
# recorded here so the next person knows they were looked at.
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

# Every release this tool will name. Being here does not mean anybody has
# watched the fix work on it: that is the skus table, and the two are kept
# apart on purpose. A release that is not here is still offered when its
# folder holds the right binaries and the patch site is found, and is reported
# as untested rather than refused.
#
# Europe was issued as six discs, one per language group, and the tool knew
# only the first. A user's Spanish and Italian copy, BLES01032, was refused as
# an unknown release while holding the BLES01031 binaries byte for byte:
# t5mp_ps3f.self at 6831584 and t5_ps3f.self at 6300560, read off his console.
#
# Each of these was confirmed in three or more independent sources, and each
# can be checked again in one line, which returns the content ID for a real
# title ID and nothing for an invented one:
#
#     curl -s https://a0.ww.np.dl.playstation.net/tpl/np/BLES01032/BLES01032-ver.xml
#
# Deliberately not included, because one source carried them and no other did:
# BLES00356 and BLES01013, whose numbers fall two years before the game came
# out, BLUS30625, and BLKS20228. A title ID invented here would have the tool
# name somebody's game wrongly, which is worse than not knowing it.
#
# There is no separate Hardened Edition title ID on PS3: BLUS30638 and the
# Platinum and ANZ pressings all boot as the ID they are grouped under.
BO1_TITLE_IDS = (
    # Europe and Australia, English and French.
    "BLES01031",
    # Europe, Italian and Spanish.
    "BLES01032",
    # Germany.
    "BLES01033",
    # Poland.
    "BLES01034",
    # Russia.
    "BLES01035",
    # Austria and Switzerland, German.
    "BLES01105",
    # United States.
    "BLUS30591",
    # Japan, subtitled and dubbed.
    "BLJM60286", "BLJM60287",
    # Digital.
    "NPEB00756", "NPUB30584",
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
    "bo1": {
        "key": "bo1",
        "name": "Call of Duty: Black Ops",
        "short": "Black Ops 1",
        "binaries": BO1_BINARIES,
        "title_ids": BO1_TITLE_IDS,
        "latest_update": "1.13",
        "verified_update": "1.13",
        "repo": "https://github.com/setsid/bo1-ps3-stats-fix",
        "symptom": ("Multiplayer opens at rank 1 every time and nothing you "
                    "do is kept, on any PSN account made after late 2018. "
                    "The game asks the server about an identity the server "
                    "has never held, so there is nothing to send back."),
        "set_advice": ("Only the multiplayer binary is changed. The campaign "
                       "and the launcher are left alone."),
        "advice": ("The fix reads your account ID out of np_cache.dat and "
                   "puts a readable copy of that file in the game's own "
                   "folder for the game to read. It is tied to the account "
                   "that was signed in when it ran: sign in with a different "
                   "PSN account and run the fix again. The file is re-signed "
                   "using the content ID, application type and key revision "
                   "read back off your own copy. Keep the backup: it is the "
                   "only way back."),
        # Both of these are confirmed on hardware. BLES01031 was watched
        # through a real public match with rank and experience surviving a
        # relaunch, and BLUS30591 was confirmed at sign-in on somebody else's
        # console.
        #
        # Neither carries the sha1 of Sony's update package, and for this game
        # that costs nothing. A package hash exists so a title-update check can
        # name which update is installed, and this game has no such check: the
        # other two fixes patch an offset that is only right for one build,
        # while this one finds its own site, so a build it was not written for
        # either matches the patterns and is the same code, or is refused.
        # There is nothing here for a hash to recognise.
        #
        # Being confirmed and having a package hash are therefore two
        # different things, and publishes_update_hashes below is what keeps
        # them apart. Dropping these entries to satisfy the hash rule is
        # something that was tried: it made the screen tell the user nobody
        # had confirmed the fix on the one release he had proved it on.
        "publishes_update_hashes": False,
        "skus": {
            "BLES01031": {"region": "Europe", "updates": {}},
            "BLUS30591": {"region": "North America", "updates": {}},
        },
    },
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


def publishes_update_hashes(title_id):
    """Whether this game's releases carry the sha1 of Sony's update package.

    A hash exists so a title-update check can say which update is installed.
    Black Ops 1 has no such check, because its fix finds its own patch site
    rather than trusting an offset that is only right for one build, so its
    releases are confirmed on hardware without one. Being confirmed and
    having a hash are separate, and this is the separation.
    """
    config = config_for(title_id)
    if config is None:
        return True
    return bool(config.get("publishes_update_hashes", True))


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


# A file name that says nothing about which game a folder holds. Every PS3
# game has an EBOOT.BIN, so it can never be the thing that decides.
GENERIC_BINARY_NAMES = frozenset({"EBOOT.BIN"})


def naming_files(key):
    """The file names that identify a folder as a release of this game.

    Taken from the binaries table so the two cannot drift apart, with the
    generic names dropped. These are names, not evidence that the fix will
    work: the patch site decides that, and it is checked separately.
    """
    config = TITLES.get(key)
    if not config:
        return frozenset()
    return frozenset(record["name"] for record in config["binaries"]
                     if record["name"] not in GENERIC_BINARY_NAMES)


def keys_for_files(names):
    """Which games a folder holding these files could be a release of.

    This is what stops a patch screen offering somebody another game. The
    Black Ops 1 screen was offering Ghosts, and then Modern Warfare 2, because
    the only test applied was "does this look like a Call of Duty" and both of
    those ship a default_mp.self. One user deleted game data chasing it.

    Black Ops 1 and Black Ops II are decided outright by their t5 and t6 file
    names. The Modern Warfare 3 names are shared with other games built on the
    same engine, so a match here means candidate and nothing more, and the
    patch site is what settles it.
    """
    present = {str(name) for name in (names or ())}
    return tuple(key for key in TITLES
                 if naming_files(key) & present)


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
