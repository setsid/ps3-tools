#!/usr/bin/env python3
# Copyright (c) 2026 setsid
# SPDX-License-Identifier: MIT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction. The full text is in LICENSE, in the
# repository this file ships with.
"""Patch the Modern Warfare 2 PS3 multiplayer binary so the player's identity
comes from the server rather than from a hash of the online ID.

Built from work by Jakes625, with permission, and his binaries are what this
was checked against. The stats fix is all that is here; his releases carry
about twenty security patches and a script compiler besides, and none of that
is in scope for this program.

The fault. The game derives its online user ID from the PSN online ID with a
Tiger192 hash, caches it, and every stats read uses the cached value. On an
account the server knows by account ID instead, that read asks for an identity
the server does not hold, and the player is level 1 every time.

The fix. The authoritative ID is already in the reply the client is parsing:
bdAuthTicket.user_id, at r1+0xB0 in the type 0x13 auth reply parser. One
instruction in that parser is replaced with a branch into a code cave; the cave
replays the displaced instruction, reads that user ID, and seeds the cache with
it before letting the parser carry on.

This is the opposite direction to the Black Ops 1 fix, which derives the
account ID locally because that game's auth reply carries no user ID at all.
Modern Warfare 2's does, so it is taken rather than worked out.

Nothing is guessed and nothing is left to an offset alone. The hook is found by
signature, the way the Black Ops 1 fix finds everything it uses, because seven
regions of this game shipped and the figures below were measured on one build.
Three facts are checked before a byte is written, and any one of them failing
is a refusal that names it:

  the hook        the instruction being displaced reads 80010098
  the parser      the instruction after it reads 90190024
  the cache       *(u32 *)0x0072BACC reads 0x020AEFE0

Measured on title update 1.14, whose decrypted multiplayer ELF is sha256
a2e79a8498dd63bebbf899eb04cc5928574fd229df52c082a8f01184906237a6. Every region
of that update decrypts to the same image: BLES00683 and BLUS30377 were
compared byte for byte and are identical.

Operates on a decrypted ELF, not on the encrypted default_mp.self.
"""

import argparse
import hashlib
import struct
import sys

# --- what is looked for ----------------------------------------------------
#
# The hook sits at the end of a run of byte reversals in the auth reply
# parser: eight lbz/stb pairs that swap two four byte fields. That run appears
# four times in the image, so it is not a signature on its own. The
# instruction two words on from the site is what tells the four apart, and the
# pair matched exactly one place in every build tried, stock and patched
# alike.
#
# Written as bytes rather than as an address because the address is only known
# for one build. A build that moved this code is still found; a build that
# changed it is refused.
ANCHOR = bytes.fromhex(
    "88090017"      # lbz  r0,0x17(r9)
    "89490014"      # lbz  r10,0x14(r9)
    "89090015"      # lbz  r8,0x15(r9)
    "88e90016"      # lbz  r7,0x16(r9)
    "980b0017"      # stb  r0,0x17(r11)
    "994b0014"      # stb  r10,0x14(r11)
    "990b0015"      # stb  r8,0x15(r11)
    "98eb0016")     # stb  r7,0x16(r11)

#: The instruction the hook displaces, which the cave replays first.
HOOK_STOCK = 0x80010098         # lwz r0,0x98(r1)

#: The instruction after the hook. Never touched, and it is what makes the
#: anchor above unambiguous.
CONTINUATION = 0x90190024       # stw r0,0x24(r25)

#: Where the pointer to the identity cache lives, and what it reads on the
#: build this was measured on. The cave loads the pointer from here, so a
#: build where this is something else is a build where the cave would write
#: into whatever happens to be at that address.
CACHE_POINTER = 0x0072BACC
CACHE_POINTER_VALUE = 0x020AEFE0

#: Where the cave goes. Past the end of the executable segment on a stock
#: image, in the gap before the next segment, and zero on every build seen.
#: Fixed rather than searched for: the reference build this was taken from
#: puts it here, and a cave somewhere else would be a different patch that
#: nobody has run on a console.
CAVE = 0x006EC100

#: The user ID's place in the auth reply parser's own stack frame.
USER_ID_AT = 0xB0

#: Where the cave writes: the ID at +8 of the cache, and the valid flag at +4.
CACHE_ID_AT = 8
CACHE_VALID_AT = 4

STOCK = "stock"
PATCHED = "patched"

#: This fix makes a loadable segment longer, which the other three do not.
#: Read by whatever re-signs the patched file: a rebuild checks the ELF
#: against the shape the original had, and a segment that grew is a fault
#: under every fix but this one. Declared here rather than in a table
#: somewhere else so that it travels with the fix that does it.
EXTENDS_SEGMENT = True


class NotThisBuild(Exception):
    """Something the fix needs was not found, or was not what it must be.

    Always a refusal and never a guess. Every message says what was looked for
    and what was there instead, because the answer to one of these is usually
    a build nobody has tried rather than a fault.
    """


# --- the image -------------------------------------------------------------

def load_program_headers(data):
    """[(index, type, flags, offset, vaddr, filesz, memsz)], or None.

    None means this is not a 64 bit big endian ELF, which is the shape every
    decrypted PS3 executable has.
    """
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 2:
        return None
    phoff = struct.unpack_from(">Q", data, 0x20)[0]
    phentsize, phnum = struct.unpack_from(">HH", data, 0x36)
    out = []
    for index in range(phnum):
        base = phoff + index * phentsize
        if base + 0x38 > len(data):
            break
        p_type, flags = struct.unpack_from(">II", data, base)
        offset, vaddr = struct.unpack_from(">QQ", data, base + 0x08)
        filesz, memsz = struct.unpack_from(">QQ", data, base + 0x20)
        out.append((index, p_type, flags, offset, vaddr, filesz, memsz))
    return out


class Image:
    """A decrypted ELF, with the address arithmetic in one place.

    Every address in this file is a virtual address, the way a disassembler
    shows it. The conversion to a file offset happens here and nowhere else,
    because a patch written at the vaddr rather than at the offset is the one
    mistake in this exercise that produces a plausible looking file.
    """

    def __init__(self, data, holding=None):
        """holding is a file offset the executable segment has to contain.

        A file somebody else has already added segments to has more than one
        executable segment, and the one this fix cares about is whichever
        holds the code it found. Asked that way round rather than by the ELF
        entry point, which on this platform is a function descriptor in the
        data segment and so names no code segment at all. The reference build
        carries two executable segments of its own, and a rule that refused
        such a file would leave this program unable to say even that the file
        is already patched.
        """
        self.data = data
        self.headers = load_program_headers(data)
        if self.headers is None:
            raise NotThisBuild("this is not a 64 bit big endian ELF, so it is "
                               "not a decrypted PS3 executable")
        found = [item for item in self.headers
                 if item[1] == 1 and item[5] and (item[2] & 1)]
        if not found:
            raise NotThisBuild(
                "the image has no loadable executable segment, so it is not "
                "a decrypted PS3 executable")
        if holding is not None:
            found = [item for item in found
                     if item[3] <= holding < item[3] + item[5]] or found
        if len(found) != 1:
            raise NotThisBuild(
                f"the image has {len(found)} loadable executable segments "
                f"and this fix cannot tell which one holds the game's own "
                f"code")
        self.code = found[0]
        self.phoff = struct.unpack_from(">Q", data, 0x20)[0]
        self.phentsize = struct.unpack_from(">H", data, 0x36)[0]

    @property
    def base(self):
        """The virtual address the executable segment's file offset 0 is at."""
        _index, _type, _flags, offset, vaddr, _filesz, _memsz = self.code
        return vaddr - offset

    def at(self, vaddr):
        """The file offset of a virtual address, whether it is loaded or not.

        The cave sits past the end of the loaded part of the segment on a
        stock image, so this deliberately does not refuse an address outside
        it. Whether an address is executable is a separate question, asked by
        covers() where it matters.

        One difference between address and offset for the whole file, taken
        off the executable segment. That is what this build is: both of its
        loadable segments sit the same distance from their addresses. A build
        where they did not would have this reading the identity cache pointer
        somewhere else, and the value found there would not be the one the
        check demands, so such a build is refused rather than patched wrongly.
        """
        offset = vaddr - self.base
        if offset < 0 or offset + 4 > len(self.data):
            raise NotThisBuild(
                f"{vaddr:08X} is outside the file, which is "
                f"{len(self.data)} bytes")
        return offset

    def covers(self, vaddr, length=4):
        """Whether the executable segment maps this address."""
        _index, _type, _flags, _offset, start, filesz, memsz = self.code
        end = start + min(filesz, memsz)
        return start <= vaddr and vaddr + length <= end

    def word(self, vaddr):
        return struct.unpack_from(">I", self.data, self.at(vaddr))[0]


# --- instructions ----------------------------------------------------------

def ha_lo(value):
    """(high adjusted, low) halves of an address for a lis/lwz pair.

    The low half is a signed 16 bit displacement, so the high half is bumped
    by one whenever the low half comes out negative. Written out rather than
    the constants being typed in, so that the pair and the address it builds
    cannot drift apart.
    """
    low = value & 0xFFFF
    high = ((value >> 16) + (1 if low & 0x8000 else 0)) & 0xFFFF
    return high, low


def lis(register, half):
    return 0x3C000000 | (register << 21) | (half & 0xFFFF)


def lwz(target, base, displacement):
    return 0x80000000 | (target << 21) | (base << 16) \
        | (displacement & 0xFFFF)


def branch(source, target):
    """An unconditional b from one virtual address to another."""
    distance = target - source
    if distance & 3 or not -(1 << 25) <= distance < (1 << 25):
        raise NotThisBuild(
            f"the branch from {source:08X} to {target:08X} is {distance} "
            f"bytes, which a single branch instruction cannot reach")
    return 0x48000000 | (distance & 0x03FFFFFC)


def cave_words(hook, cave, pointer=CACHE_POINTER, displaced=HOOK_STOCK):
    """The cave, as instructions, for a hook and a cave at these addresses.

    Everything that depends on where the code sits is worked out here: the
    branch home, and the pair that builds the cache pointer. The conditional
    branch over the write is relative to the cave itself, so it is the same
    wherever the cave lands.

    The order at the end is deliberate and is not tidiness. lwsync and then
    the valid byte written last mean the fast path that reads this cache
    cannot see a valid flag over a half written identity.
    """
    high, low = ha_lo(pointer)
    words = [
        displaced,                              # replay what was displaced
        0xE9010000 | (USER_ID_AT & 0xFFFF),     # ld    r8,0xB0(r1)
        0x2C280000,                             # cmpdi r8,0
        0x4182001C,                             # beq   the branch home
        lis(11, high),                          # lis   r11,cache pointer
        lwz(11, 11, low),                       # lwz   r11,pointer(r11)
        0xF9000008 | (11 << 16) | CACHE_ID_AT,  # std   r8,8(r11)
        0x7C2004AC,                             # lwsync
        0x39400001,                             # li    r10,1
        0x99400004 | (11 << 16) | CACHE_VALID_AT,   # stb r10,4(r11)
    ]
    home = cave + len(words) * 4
    words.append(branch(home, hook + 4))
    return words


def cave_bytes(hook, cave, pointer=CACHE_POINTER, displaced=HOOK_STOCK):
    return b"".join(struct.pack(">I", word)
                    for word in cave_words(hook, cave, pointer, displaced))


CAVE_BYTES = len(cave_words(0, 0)) * 4


# --- finding the site ------------------------------------------------------

def hook_offset(data):
    """The file offset of the instruction the fix displaces.

    Refuses on anything other than exactly one match. Two matches would mean
    a build where the anchor no longer tells the four byte reversal runs
    apart, and picking one of them is the kind of guess that writes a branch
    into the wrong function.

    A file offset rather than an address, because which segment the address
    belongs to is settled by where this lands rather than the other way
    about.
    """
    hits = []
    seen = []
    start = 0
    while True:
        found = data.find(ANCHOR, start)
        if found < 0:
            break
        start = found + 4
        site = found + len(ANCHOR)
        if site + 8 > len(data):
            continue
        after = struct.unpack_from(">I", data, site + 4)[0]
        seen.append(after)
        if after == CONTINUATION:
            hits.append(site)
    if not hits and seen:
        # The parser is there and the instruction this fix returns to is not,
        # which is a different thing from not finding the parser at all and
        # gets a different refusal. It is the second of the three checks, and
        # a refusal that named neither would leave somebody with a build and
        # nothing to go on.
        raise NotThisBuild(
            f"the instruction after the hook reads "
            f"{seen[0]:08X} and this fix returns to {CONTINUATION:08X}")
    if not hits:
        raise NotThisBuild(
            "the auth reply parser this fix hooks was not found, so this is "
            "not a build it was written for")
    if len(hits) > 1:
        raise NotThisBuild(
            f"the signature matched {len(hits)} places, so which one to hook "
            f"is ambiguous and nothing has been changed")
    return hits[0]


def read(data):
    """(image, hook virtual address) for a file this fix understands."""
    offset = hook_offset(data)
    image = Image(data, holding=offset)
    return image, image.base + offset


def find_hook(data):
    """The virtual address of the instruction the fix displaces."""
    return read(data)[1]


def find_site(data):
    """(file offset of the hook, state), the way the other patchers spell it.

    state is "stock" when the instruction is still the one the game shipped,
    "patched" when it is a branch to a cave holding this fix, and None when it
    is neither, which means somebody else has been here and this program will
    not touch it.
    """
    data = bytes(data)
    image, hook = read(data)
    offset = image.at(hook)
    word = image.word(hook)
    if word == HOOK_STOCK:
        return offset, STOCK
    if (word & 0xFC000003) != 0x48000000:
        return offset, None
    distance = word & 0x03FFFFFC
    if distance & (1 << 25):
        distance -= (1 << 26)
    target = hook + distance
    try:
        at = image.at(target)
    except NotThisBuild:
        return offset, None
    wanted = cave_bytes(hook, target)
    if data[at:at + len(wanted)] == wanted:
        return offset, PATCHED
    return offset, None


def check(data):
    """Everything that has to be true before a byte is written.

    Returns a dict of what was found. Raises NotThisBuild naming the check
    that failed, because "this is not the right build" is no use to somebody
    holding a file and wanting to know which part of it is unexpected.
    """
    data = bytes(data)
    image, hook = read(data)
    word = image.word(hook)
    if word not in (HOOK_STOCK,) and find_site(data)[1] != PATCHED:
        raise NotThisBuild(
            f"the instruction at {hook:08X} reads {word:08X} and the fix "
            f"displaces {HOOK_STOCK:08X}")
    after = image.word(hook + 4)
    if after != CONTINUATION:
        raise NotThisBuild(
            f"the instruction at {hook + 4:08X} reads {after:08X} and this "
            f"fix returns to {CONTINUATION:08X}")
    pointer = image.word(CACHE_POINTER)
    if pointer != CACHE_POINTER_VALUE:
        raise NotThisBuild(
            f"the identity cache pointer at {CACHE_POINTER:08X} reads "
            f"{pointer:08X} and this fix is written for "
            f"{CACHE_POINTER_VALUE:08X}")
    return {"hook": hook, "offset": image.at(hook), "cave": CAVE,
            "cache_pointer": CACHE_POINTER, "cache": pointer}


# --- the executable segment ------------------------------------------------

def coverage(data, through=None):
    """(covers, segment index, filesz, what it would have to become).

    The cave is past the end of the loaded part of the segment on a stock
    image, so a patch written there would sit in a file nobody loads. This
    says whether the segment already reaches it and what reaching it would
    take, without changing anything.
    """
    image = Image(data, holding=hook_offset(data))
    end = CAVE + CAVE_BYTES if through is None else through
    index, _type, _flags, offset, vaddr, filesz, memsz = image.code
    needed = end - vaddr
    if image.covers(CAVE, CAVE_BYTES):
        return True, index, filesz, filesz
    # As far as the next segment starts in the file, which is what the
    # reference build does and is the whole of the gap the cave sits in.
    # Falls back to exactly what the cave needs where there is no next
    # segment to stop at.
    starts = [item[3] for item in image.headers
              if item[1] == 1 and item[5] and item[3] > offset + filesz]
    wanted = min(starts) - offset if starts else needed
    if wanted < needed:
        wanted = needed
    return False, index, filesz, wanted


def extend(data, wanted, index):
    """Grows one program header's file and memory sizes. Returns bytes.

    Both together: a segment whose memsz stops short of its filesz maps less
    than it holds, and the cave would be in the file and not in memory.
    """
    image = Image(data, holding=hook_offset(data))
    out = bytearray(data)
    base = image.phoff + index * image.phentsize
    offset = struct.unpack_from(">Q", out, base + 0x08)[0]
    if offset + wanted > len(out):
        raise NotThisBuild(
            f"the executable segment would have to run to {offset + wanted} "
            f"bytes and the file is {len(out)}")
    struct.pack_into(">QQ", out, base + 0x20, wanted, wanted)
    return bytes(out)


# --- applying --------------------------------------------------------------

def apply(data):
    """Patch a decrypted image. Returns (bytes, dict of what was done).

    The dict says what happened to the program header as well as where the
    code went, because extending the executable segment is a change to the
    shape of the file and a caller that reports a patch should be able to
    report that too.
    """
    data = bytes(data)
    _offset, state = find_site(data)
    if state is None:
        raise NotThisBuild(
            "the instruction this fix replaces has already been changed to "
            "something else, so the file is left alone")
    if state == PATCHED:
        return data, {"already": True, **check(data)}
    facts = check(data)
    hook = facts["hook"]
    image = Image(data, holding=facts["offset"])
    cave_at = image.at(CAVE)
    blob = cave_bytes(hook, CAVE)
    if any(data[cave_at:cave_at + len(blob)]):
        raise NotThisBuild(
            f"the {len(blob)} bytes at {CAVE:08X} are not all zero, so this "
            f"is not free space and nothing has been written")
    covered, index, filesz, wanted = coverage(data)
    out = bytearray(data)
    out[cave_at:cave_at + len(blob)] = blob
    struct.pack_into(">I", out, facts["offset"], branch(hook, CAVE))
    result = bytes(out)
    if not covered:
        result = extend(result, wanted, index)
    facts.update({"already": False, "bytes": len(blob),
                  "segment": index, "was_executable": covered,
                  "filesz_was": filesz,
                  "filesz_now": filesz if covered else wanted})
    return result, facts


def restore(data):
    """Put the displaced instruction back and wipe the cave. Returns bytes.

    The executable segment is left as it is. Its size cannot be put back from
    the file alone, because nothing in the file records what it was, and a
    guess there would be a segment that maps less than it holds. What it maps
    is zeros either way, and the program that calls this keeps a copy of the
    original file, which is the honest way back.
    """
    data = bytes(data)
    offset, state = find_site(data)
    if state != PATCHED:
        return data
    image = Image(data, holding=offset)
    out = bytearray(data)
    cave_at = image.at(CAVE)
    out[cave_at:cave_at + CAVE_BYTES] = b"\x00" * CAVE_BYTES
    struct.pack_into(">I", out, offset, HOOK_STOCK)
    return bytes(out)


# --- the command line ------------------------------------------------------

def sha1(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("binary", help="decrypted Modern Warfare 2 MP ELF")
    parser.add_argument("-o", "--output",
                        help="write here instead of in place")
    parser.add_argument("--check", action="store_true",
                        help="report what was found and the state, then stop")
    parser.add_argument("--restore", action="store_true",
                        help="put the displaced instruction back")
    args = parser.parse_args()

    data = open(args.binary, "rb").read()
    print("input   %s" % args.binary)
    print("size    %d bytes" % len(data))
    print("sha1    %s" % sha1(args.binary))

    try:
        facts = check(data)
        _offset, state = find_site(data)
    except NotThisBuild as exc:
        sys.exit("this is not a build the fix understands: %s" % exc)

    print("hook    %08X" % facts["hook"])
    print("cave    %08X" % facts["cave"])
    print("cache   %08X reads %08X" % (facts["cache_pointer"],
                                       facts["cache"]))
    print("state   %s" % (state or "not recognised"))
    covered, index, filesz, wanted = coverage(data)
    print("segment %d, filesz %08X, %s" % (
        index, filesz,
        "the cave is already executable" if covered
        else "would grow to %08X to make the cave executable" % wanted))
    if args.check:
        return
    if state is None:
        sys.exit("the instruction this fix replaces has been changed to "
                 "something else, so the file is left alone")

    if args.restore:
        if state == STOCK:
            print("nothing to do, already stock")
            return
        out = restore(data)
    else:
        if state == PATCHED:
            print("nothing to do, already patched")
            return
        out, what = apply(data)
        print("wrote   %d bytes at %08X" % (what["bytes"], what["cave"]))
        if not what["was_executable"]:
            print("grew    segment %d from %08X to %08X so the cave is "
                  "executable" % (what["segment"], what["filesz_was"],
                                  what["filesz_now"]))

    where = args.output or args.binary
    with open(where, "wb") as handle:
        handle.write(out)
    print("output  %s" % where)


if __name__ == "__main__":
    main()
