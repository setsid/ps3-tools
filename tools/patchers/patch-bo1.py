#!/usr/bin/env python3
# Copyright (c) 2026 setsid
# SPDX-License-Identifier: MIT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction. The full text is in LICENSE, in the
# repository this file ships with.
"""Patch the Black Ops 1 PS3 multiplayer binary so the player's identity is
derived from the PSN account ID rather than from the online ID.

The fault. The game builds its XUID by lowercasing the online ID, hashing it
with Tiger192, taking the first eight bytes and byte-reversing them. Demonware
derives the same number from the account ID for any account made after Sony's
2018 change, and it hashes the account ID as its decimal ASCII string rather
than as raw bytes. The two never agree, so every stats lookup asks the server
for an identity it does not hold and the player sits at rank 1 forever.

The fix. The `bl getUserID` that hashes the online ID is replaced with a branch
into a code cave. The cave opens np_cache.dat, reads the account ID out of its
first eight bytes, formats it as a decimal string with the game's own va() and
its own "%llu", and calls getUserID with that instead. Every failure falls
through to the original online ID pointer, so an account that already works is
left exactly as it was.

Nothing here is an address. Three builds were compared -- BLES01031, BLUS30591
and NPEB00756. The two disc builds decrypt to a byte-identical image, so region
alone changes nothing, but the digital build is a different compile: the hook
site happens to land in the same place and everything after it moves, code by
0x238 and data by 0x240. One table of offsets cannot cover both, so there is no
table. Everything is found in the image:

  getUserID     two independent signatures that have to agree on one function.
                The first is the byte reversal itself: eight lbz/stb pairs
                whose load displacements count 0 to 7 while their stores count
                7 to 0. The second is the `li r8, 8` that sets up the length,
                followed by six or more lbz within the next 0x60 bytes. Each
                on its own matches several functions in the image; together
                they matched exactly one on every build tried. From the match,
                walk back to the enclosing function's stdu prologue.

  the hook      the first caller of getUserID, in address order. The other
                callers are the friends list and must not be touched.

  "%llu"        the first occurrence of the bytes.

  va()          the lis/addi pair that builds the address of that string, and
                then the branch a few instructions later. It is a tail branch
                and not a call, so it is opcode 18 with the link bit clear.
                Every reference found has to agree on one target.

  cellFs*       resolved out of the PRX import tables by FNID, which is how
                the loader itself resolves them. Structural rather than a
                signature, so it cannot drift.

  the cave      the longest run of zeros in the executable segment, which has
                to be long enough for the code, both paths and some headroom.

Operates on a decrypted ELF, not on the encrypted t5mp_ps3f.self.
"""

import argparse
import hashlib
import re
import shutil
import struct
import sys

# --- what the cave is marked with ------------------------------------------
#
# The head of the cave, which the hook branches past: eight bytes of magic and
# then the length of the whole cave. A patch is recognised by this and by
# nothing else -- a hook that branches somewhere unmarked is somebody else's
# patch, and this program will not touch it or claim to understand it.
#
# The length is in there because putting the file back the way it was means
# knowing how much of the cave to wipe, and by then the run of zeros it was
# found in is no longer a run of zeros.
MARK = b"BO1XUID\x01"
HEAD = len(MARK) + 4

#: Where the cave reads the account ID from. Written into the cave as data at
#: patch time, so the title ID in it is the one on the console rather than one
#: compiled in here.
#:
#: The copy the tool places, and only that. The real file, in the signed in
#: user's home folder, is mode rw------- and the game cannot open it; an
#: earlier version of this asked for the real one first and fell back to the
#: copy, on the reasoning that a failed open costs nothing. It was not what
#: was confirmed working on hardware, and being the one thing the tool did
#: that the confirmed build did not, it is gone. Files written over FTP land
#: as rwxrwxrwx, so a copy in the game's own folder is readable, sits with the
#: game it belongs to, and goes when the game goes.
USRDIR_PATH = "/dev_hdd0/game/%s/USRDIR/np_cache.dat"

#: The account ID is the first eight bytes of np_cache.dat, big endian.
ACCOUNT_ID_BYTES = 8

# --- FNIDs -----------------------------------------------------------------
#
# The numbers the loader itself matches on. They are the same on every PS3
# title because they are a hash of the function name.
FNID_CELL_FS_OPEN = 0x718BF5F8
FNID_CELL_FS_READ = 0x4D5FF8E2
FNID_CELL_FS_CLOSE = 0x2CB51F0D

PRX_PARAM_MAGIC = 0x1B434CEC
PT_PRX_PARAM = 0x60000002


class NotThisBuild(Exception):
    """Something the fix needs was not found, or was found more than once.

    Always a refusal and never a guess. Every message says what was looked
    for and what was found instead, because the answer to one of these is
    usually a build nobody has tried rather than a fault.
    """


# --- the image -------------------------------------------------------------

Segment = struct.Struct(">II QQQ QQQ")


def load_segments(data):
    """(type, flags, offset, vaddr, filesz) per program header, or None.

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
        filesz = struct.unpack_from(">Q", data, base + 0x20)[0]
        out.append((p_type, flags, offset, vaddr, filesz))
    return out


def executable_segment(segments):
    """The one loadable segment with the code in it.

    More than one would mean an image this was not written for, and picking
    the first of several is the kind of guess that produces a patch in the
    wrong place.
    """
    found = [item for item in (segments or [])
             if item[0] == 1 and item[4] and (item[1] & 1)]
    if len(found) != 1:
        raise NotThisBuild(
            f"the image has {len(found)} loadable executable segments and "
            f"this fix is written for one")
    _type, _flags, offset, vaddr, filesz = found[0]
    return offset, vaddr, filesz


class Image:
    """A decrypted ELF, with the address arithmetic in one place.

    Every address in this file is a virtual address, the way a disassembler
    shows it. The conversion to a file offset happens here and nowhere else,
    because a patch written at the vaddr rather than the offset is the one
    mistake in this whole exercise that produces a plausible looking file.
    """

    def __init__(self, data):
        self.data = data
        self.segments = load_segments(data)
        if self.segments is None:
            raise NotThisBuild("this is not a 64 bit big endian ELF, so it is "
                               "not a decrypted PS3 executable")
        self.offset, self.base, self.size = executable_segment(self.segments)
        self.end = self.base + self.size

    # -- addresses
    def contains(self, vaddr, length=4):
        return self.base <= vaddr and vaddr + length <= self.end

    def at(self, vaddr):
        if not self.contains(vaddr):
            raise NotThisBuild(f"{vaddr:08X} is outside the executable "
                               f"segment")
        return self.offset + (vaddr - self.base)

    def word(self, vaddr):
        return struct.unpack_from(">I", self.data, self.at(vaddr))[0]

    def vaddr_of(self, file_offset):
        return self.base + (file_offset - self.offset)

    def locate(self, vaddr, length=4):
        """The file offset of an address in any loaded segment, or None.

        Wider than at(), deliberately. at() answers for the executable
        segment only, so that a patch cannot be written anywhere but in code;
        this one is for reading tables, and the import function tables live in
        the data segment.
        """
        for p_type, _flags, offset, base, filesz in self.segments:
            if p_type != 1 or not filesz:
                continue
            if base <= vaddr and vaddr + length <= base + filesz:
                return offset + (vaddr - base)
        return None

    def read_word(self, vaddr):
        """A word from anywhere in the image, or None if it is not mapped."""
        position = self.locate(vaddr)
        if position is None:
            return None
        return struct.unpack_from(">I", self.data, position)[0]

    def words(self):
        """(vaddr, word) for every aligned word in the executable segment.

        Only for a scan that has no cheaper shape. A whole pass over this
        segment is eleven million bytes of Python, and the searches below use
        find_words instead wherever the thing they are looking for has a
        recognisable first byte.
        """
        data = self.data
        for position in range(self.offset, self.offset + self.size - 3, 4):
            yield (self.base + (position - self.offset),
                   struct.unpack_from(">I", data, position)[0])

    def find_words(self, pattern):
        """(vaddr, word) for each aligned match of a byte pattern.

        The regular expression does the walking, in C, and what comes back is
        the handful of places worth looking at properly. Every instruction
        this file searches for has a first byte inside a known range, because
        the opcode is the top six bits, so a pattern of one or two bytes takes
        a pass over eleven megabytes down to a few thousand candidates.
        """
        end = self.offset + self.size
        for match in re.finditer(pattern, self.data[self.offset:end]):
            position = self.offset + match.start()
            if (position - self.offset) % 4 or position + 4 > end:
                continue
            yield (self.vaddr_of(position),
                   struct.unpack_from(">I", self.data, position)[0])


# --- instruction shapes ----------------------------------------------------

def _op(word):
    return word >> 26


def _rt(word):
    return (word >> 21) & 31


def _ra(word):
    return (word >> 16) & 31


def _simm(word):
    value = word & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def is_branch(word, link=None):
    """opcode 18, optionally with the link bit required set or clear."""
    if _op(word) != 18 or (word & 2):
        return False
    return link is None or bool(word & 1) == link


def branch_target(vaddr, word):
    offset = word & 0x03FFFFFC
    if offset & 0x02000000:
        offset -= 0x04000000
    return (vaddr + offset) & 0xFFFFFFFF


def is_prologue(word):
    """stdu r1, r1, -N, which is how every function here opens its frame."""
    return (_op(word) == 62 and _rt(word) == 1 and _ra(word) == 1
            and (word & 3) == 1)


# --- finding getUserID -----------------------------------------------------

#: The first pair of an unrolled reversal, as bytes. lbz is opcode 34 and stb
#: is 38, and the top six bits of the word are the opcode, so the first byte of
#: each is pinned to four values by which register it names. The displacements
#: are the whole of the rest: nought on the load and seven on the store.
FIRST_PAIR = re.compile(rb"[\x88-\x8b].\x00\x00[\x98-\x9b].\x00\x07",
                        re.DOTALL)


def find_reversals(image):
    """Every unrolled eight byte reversal in the executable segment.

    Eight lbz/stb pairs, the loads counting 0 to 7 and the stores counting 7
    to 0, all the loads off one base register and all the stores off another.
    Which registers is not part of the pattern: a different build may allocate
    them differently and the shape of the thing is what identifies it.
    """
    found = []
    for vaddr, _word in image.find_words(FIRST_PAIR):
        if not image.contains(vaddr, 64):
            break
        source = destination = None
        for step in range(8):
            load = image.word(vaddr + step * 8)
            store = image.word(vaddr + step * 8 + 4)
            if _op(load) != 34 or _op(store) != 38:
                break
            if (load & 0xFFFF) != step or (store & 0xFFFF) != (7 - step):
                break
            if _rt(load) != _rt(store):
                break
            if source is None:
                source, destination = _ra(load), _ra(store)
            elif (_ra(load), _ra(store)) != (source, destination):
                break
        else:
            found.append(vaddr)
    return found


def find_length_setups(image, register=8, length=8, window=0x60, wanted=6):
    """Every `li rN, 8` with six or more lbz close behind it."""
    pattern = struct.pack(">I", (14 << 26) | (register << 21) | length)
    found = []
    start = image.offset
    limit = image.offset + image.size
    while True:
        position = image.data.find(pattern, start, limit)
        if position < 0:
            return found
        start = position + 1
        if (position - image.offset) % 4:
            continue
        vaddr = image.vaddr_of(position)
        loads = 0
        for step in range(1, window // 4 + 1):
            if not image.contains(vaddr + step * 4):
                break
            if _op(image.word(vaddr + step * 4)) == 34:
                loads += 1
        if loads >= wanted:
            found.append(vaddr)


def enclosing_function(image, vaddr, limit=0x4000):
    """Walk back to the stdu that opens the function this address is in."""
    for step in range(0, limit, 4):
        here = vaddr - step
        if not image.contains(here):
            break
        if is_prologue(image.word(here)):
            return here
    raise NotThisBuild(
        f"no function prologue within {limit:#x} bytes before {vaddr:08X}")


def find_get_user_id(image):
    """The function that turns a string into an XUID.

    Two signatures, found independently, that have to land in the same
    function. Either on its own matches several functions in this image; the
    pair matched one on every build this was tried on, and a pair that matches
    anything other than one function is refused rather than guessed at.
    """
    by_reversal = set()
    for vaddr in find_reversals(image):
        try:
            by_reversal.add(enclosing_function(image, vaddr))
        except NotThisBuild:
            continue
    by_length = set()
    for vaddr in find_length_setups(image):
        try:
            by_length.add(enclosing_function(image, vaddr))
        except NotThisBuild:
            continue
    both = sorted(by_reversal & by_length)
    if len(both) == 1:
        return both[0]
    raise NotThisBuild(
        f"the byte reversal signature and the length signature agree on "
        f"{len(both)} functions, and this fix needs exactly one. The "
        f"reversal matched {len(by_reversal)} and the length matched "
        f"{len(by_length)}")


#: A branch is opcode 18, so the first byte is 0x48 to 0x4B whatever the
#: displacement and the link bit say.
ANY_BRANCH = re.compile(rb"[\x48-\x4b]", re.DOTALL)


def find_callers(image, target):
    """Every `bl target`, in address order."""
    return [vaddr for vaddr, word in image.find_words(ANY_BRANCH)
            if is_branch(word, link=True)
            and branch_target(vaddr, word) == target]


def find_marked_calls(image):
    """Every call that lands in a cave this program wrote."""
    return [vaddr for vaddr, word in image.find_words(ANY_BRANCH)
            if is_branch(word, link=True)
            and cave_head(image, branch_target(vaddr, word))]


def find_hook(image, get_user_id):
    """The call to replace, on a stock image or on one already patched.

    On a stock image it is the first caller of getUserID in address order.
    The rest of them are the friends list, which asks the same question about
    other people and is answered correctly today; patching those would break a
    working screen to fix a different one.

    On a patched image that call no longer goes to getUserID, so looking for
    the first caller again would land on the friends list. The call into our
    own cave is what identifies it, and there is only ever one.

    The one thing this cannot see. If something other than this fix has
    already redirected that call somewhere unmarked, it is no longer a caller
    of getUserID and there is nothing left in the image that says it ever was.
    What was the second caller then looks like the first, and it is the
    friends list. Nothing in the image distinguishes those two cases, so this
    is stated rather than defended against: the fix is written for a stock
    binary or for one of its own, and find_site refuses to call anything else
    patched, which is what keeps it from claiming somebody else's work. If a
    third-party patch for this call ever exists, this is the function that has
    to learn to tell them apart.
    """
    callers = find_callers(image, get_user_id)
    marked = find_marked_calls(image)
    if len(marked) > 1:
        raise NotThisBuild(
            f"{len(marked)} calls lead into a cave this fix wrote, and there "
            f"should be one. This file has been patched more than once")
    if marked:
        return marked[0], callers
    if not callers:
        raise NotThisBuild(f"nothing calls the function at "
                           f"{get_user_id:08X}, so it is not getUserID")
    return callers[0], callers


# --- finding the formatter -------------------------------------------------

def find_bytes(image, wanted):
    """The first occurrence of some bytes in the executable segment."""
    position = image.data.find(wanted, image.offset,
                               image.offset + image.size)
    if position < 0:
        raise NotThisBuild(f"{wanted!r} is not in the executable segment")
    return image.vaddr_of(position)


def find_address_builds(image, target, window=8):
    """Every lis/addi or lis/ori pair that builds `target` in a register."""
    high = (target >> 16) & 0xFFFF
    # lis is opcode 15, so the first byte is 0x3C to 0x3F, and the two bytes
    # after the register are the half-word being loaded. That is enough of the
    # instruction to search for it as bytes.
    pattern = re.compile(
        rb"[\x3c-\x3f]." + re.escape(struct.pack(">H", high)), re.DOTALL)
    found = []
    for vaddr, word in image.find_words(pattern):
        if _op(word) != 15 or (word & 0xFFFF) != high:
            continue
        register = _rt(word)
        for step in range(1, window + 1):
            if not image.contains(vaddr + step * 4):
                break
            second = image.word(vaddr + step * 4)
            if _op(second) == 14 and _ra(second) == register:
                if (high << 16) + _simm(second) == target:
                    found.append(vaddr + step * 4)
                break
            if _op(second) == 24 and _rt(second) == register:
                if (high << 16) + (second & 0xFFFF) == target:
                    found.append(vaddr + step * 4)
                break
    return found


def find_formatter(image, format_string, window=0x30):
    """va(), reached by a tail branch shortly after its format string is built.

    The call is a branch rather than a bl: whatever builds the string address
    hands off to va() and does not come back. Every reference has to agree on
    the target, because two answers mean the shape assumed here is wrong.
    """
    targets = set()
    for vaddr in find_address_builds(image, format_string):
        for step in range(1, window // 4 + 1):
            if not image.contains(vaddr + step * 4):
                break
            word = image.word(vaddr + step * 4)
            if is_branch(word, link=False):
                targets.add(branch_target(vaddr + step * 4, word))
                break
            if is_branch(word, link=True):
                break
    if len(targets) == 1:
        return targets.pop()
    raise NotThisBuild(
        f"the references to the format string branch to {len(targets)} "
        f"different places, and this fix needs them to agree on one")


# --- finding the imports ---------------------------------------------------

def prx_param(image):
    """The sys_process_prx_param segment, which lists the import tables."""
    for p_type, _flags, offset, vaddr, filesz in image.segments:
        if p_type != PT_PRX_PARAM or filesz < 0x20:
            continue
        size, magic = struct.unpack_from(">II", image.data, offset)
        if magic == PRX_PARAM_MAGIC:
            return struct.unpack_from(">II", image.data, offset + 24)
    raise NotThisBuild("the image has no PRX parameter segment, so its "
                       "imports cannot be resolved")


def imports(image):
    """{fnid: thunk address} for every function this binary imports.

    Walked the way the loader walks it. A stub whose own size field is zero
    would loop forever, so the walk stops rather than trusting it.
    """
    start, end = prx_param(image)
    found = {}
    vaddr = start
    while start <= vaddr < end:
        position = image.locate(vaddr, 0x2C)
        if position is None:
            break
        size = image.data[position]
        if size < 0x2C:
            break
        count = struct.unpack_from(">H", image.data, position + 6)[0]
        nids, functions = struct.unpack_from(">II", image.data, position + 20)
        for index in range(count):
            nid = image.read_word(nids + index * 4)
            thunk = image.read_word(functions + index * 4)
            if nid is None or thunk is None:
                break
            found.setdefault(nid, thunk)
        vaddr += size
    return found


def find_import(table, fnid, name):
    thunk = table.get(fnid)
    if thunk is None:
        raise NotThisBuild(f"this binary does not import {name}, which the "
                           f"fix needs to read the account ID")
    return thunk


# --- finding the cave ------------------------------------------------------

def find_cave(image, needed):
    """The longest run of zeros in the executable segment.

    Longest rather than first: the first run big enough is usually padding
    between two functions, and a cave with a few bytes to spare is a cave that
    stops being big enough the next time this file grows a line. The run has
    to be reachable from the hook by a single branch, which on this image it
    always is, and that is checked where the branch is built rather than here.
    """
    window = image.data[image.offset:image.offset + image.size]
    best = None
    for match in re.finditer(rb"\x00{64,}", window):
        start = image.offset + match.start()
        length = match.end() - match.start()
        # Aligned, and never at the very start of the segment, which is the
        # ELF header rather than anything anybody would call free space.
        padding = -start % 4
        start += padding
        length -= padding
        if length >= needed and (best is None or length > best[1]):
            best = (start, length)
    if best is None:
        raise NotThisBuild(
            f"there is no run of {needed} free bytes in the executable "
            f"segment to put the fix's own code in")
    return image.vaddr_of(best[0]), best[1]


# --- the assembler ---------------------------------------------------------
#
# Enough PowerPC to write one function. Every one of these returns a single
# instruction word, so a cave is a list of integers until the moment it is
# packed.

def _d(op, rt, ra, si):
    return (op << 26) | (rt << 21) | (ra << 16) | (si & 0xFFFF)


def _ds(op, rt, ra, ds, xo):
    return (op << 26) | (rt << 21) | (ra << 16) | (ds & 0xFFFC) | xo


def lis(rt, value):
    return _d(15, rt, 0, value)


def ori(ra, rs, value):
    return (24 << 26) | (rs << 21) | (ra << 16) | (value & 0xFFFF)


def addi(rt, ra, value):
    return _d(14, rt, ra, value)


def li(rt, value):
    return _d(14, rt, 0, value)


def mr(rt, rs):
    return (31 << 26) | (rs << 21) | (rt << 16) | (rs << 11) | (444 << 1)


def std(rs, ra, d):
    return _ds(62, rs, ra, d, 0)


def stdu(rs, ra, d):
    return _ds(62, rs, ra, d, 1)


def ld(rt, ra, d):
    return _ds(58, rt, ra, d, 0)


def lwz(rt, ra, d):
    return _d(32, rt, ra, d)


def extsw(ra, rs):
    return (31 << 26) | (rs << 21) | (ra << 16) | (986 << 1)


def mflr(rt):
    return (31 << 26) | (rt << 21) | (8 << 16) | (339 << 1)


def mtlr(rs):
    return (31 << 26) | (rs << 21) | (8 << 16) | (467 << 1)


def blr():
    return (19 << 26) | (20 << 21) | (16 << 1)


def cmpwi(bf, ra, value):
    return (11 << 26) | (bf << 23) | (ra << 16) | (value & 0xFFFF)


def cmpdi(bf, ra, value):
    return (11 << 26) | (bf << 23) | (1 << 21) | (ra << 16) | (value & 0xFFFF)


def cmpd(bf, ra, rb):
    return (31 << 26) | (bf << 23) | (1 << 21) | (ra << 16) | (rb << 11)


def bl(frm, to):
    offset = to - frm
    if not -(1 << 25) <= offset < (1 << 25):
        raise NotThisBuild(f"a call from {frm:08X} to {to:08X} is "
                           f"{abs(offset):#x} bytes, which is further than "
                           f"one branch reaches")
    return (18 << 26) | (offset & 0x03FFFFFC) | 1


def b(frm, to):
    offset = to - frm
    if not -(1 << 25) <= offset < (1 << 25):
        raise NotThisBuild(f"a branch from {frm:08X} to {to:08X} is "
                           f"{abs(offset):#x} bytes, which is further than "
                           f"one branch reaches")
    return (18 << 26) | (offset & 0x03FFFFFC)


def bc(bo, bi, frm, to):
    offset = to - frm
    if not -(1 << 15) <= offset < (1 << 15):
        raise NotThisBuild("a conditional branch inside the cave is out of "
                           "range, which means the cave has been built wrong")
    return (16 << 26) | (bo << 21) | (bi << 16) | (offset & 0xFFFC)


def beq(frm, to, cr=0):
    return bc(12, cr * 4 + 2, frm, to)


def bne(frm, to, cr=0):
    return bc(4, cr * 4 + 2, frm, to)


def load32(rt, value):
    """lis/ori, which is safe for any 32 bit value with no sign extension."""
    return [lis(rt, (value >> 16) & 0xFFFF), ori(rt, rt, value & 0xFFFF)]


# --- the cave --------------------------------------------------------------
#
# Stack frame, 0x100 bytes. The first 0x70 belongs to the ABI: 48 bytes of
# linkage plus 64 bytes of parameter save area, which the functions called
# from here will write into. Everything of ours is above that.
#
#   0x70   fd, four bytes, written by cellFsOpen
#   0x78   nread, eight bytes, written by cellFsRead
#   0x80   the account ID, eight bytes, big endian as np_cache.dat stores it
#   0xE8   saved r30
#   0xF0   saved r31
#   0xF8   saved r29
#
# r31 holds the original argument, the online ID pointer, the whole way
# through, so the fallback still has it. r29 holds the path being tried.

FRAME = 0x100
FD = 0x70
NREAD = 0x78
ACCOUNT = 0x80
SAVE_R30 = 0xE8
SAVE_R31 = 0xF0
SAVE_R29 = 0xF8
LR_SLOT = FRAME + 0x10


class Landmarks:
    """Everything the fix had to find, and where it found it."""

    def __init__(self, get_user_id, hook, callers, format_string, formatter,
                 fs_open, fs_read, fs_close, cave, cave_size):
        self.get_user_id = get_user_id
        self.hook = hook
        self.callers = callers
        self.format_string = format_string
        self.formatter = formatter
        self.fs_open = fs_open
        self.fs_read = fs_read
        self.fs_close = fs_close
        self.cave = cave
        self.cave_size = cave_size

    def as_dict(self):
        return {"getUserID": self.get_user_id, "hook": self.hook,
                "callers": list(self.callers), "%llu": self.format_string,
                "va": self.formatter, "cellFsOpen": self.fs_open,
                "cellFsRead": self.fs_read, "cellFsClose": self.fs_close,
                "cave": self.cave, "cave_size": self.cave_size}

    def __repr__(self):
        said = []
        for name, value in self.as_dict().items():
            if name == "callers":
                continue
            said.append(f"{name}={value:08X}" if isinstance(value, int)
                        else f"{name}={value}")
        return "Landmarks(" + ", ".join(said) + ")"


#: What the cave needs, with room to spare, before a run of zeros will do.
CAVE_HEADROOM = 512


def find_landmarks(image):
    """Everything the fix needs, found in the image and never assumed."""
    get_user_id = find_get_user_id(image)
    hook, callers = find_hook(image, get_user_id)
    format_string = find_bytes(image, b"%llu\x00")
    formatter = find_formatter(image, format_string)
    table = imports(image)
    fs_open = find_import(table, FNID_CELL_FS_OPEN, "cellFsOpen")
    fs_read = find_import(table, FNID_CELL_FS_READ, "cellFsRead")
    fs_close = find_import(table, FNID_CELL_FS_CLOSE, "cellFsClose")
    cave, size = find_cave(image, CAVE_HEADROOM)
    return Landmarks(get_user_id, hook, callers, format_string, formatter,
                     fs_open, fs_read, fs_close, cave, size)


def cave_head(image, entry):
    """(start, length) of the cave a call lands in, or None.

    `entry` is wherever a call now points. A cave this program wrote has its
    head immediately before that, so this is what tells a patched file from a
    file somebody else has been at.
    """
    start = entry - HEAD
    position = image.locate(start, HEAD)
    if position is None:
        return None
    if image.data[position:position + len(MARK)] != MARK:
        return None
    length = struct.unpack_from(">I", image.data, position + len(MARK))[0]
    if not HEAD <= length <= (1 << 20):
        return None
    return start, length


def build_cave(marks, cave, paths):
    """The bytes that go at `cave`. Returns (bytes, entry address).

    The entry is past the head, which sits at the front so that reading a
    patch back is a matter of looking at the bytes before wherever the hook
    now points.

    Each path gets its own copy of the open-read-close sequence, one after
    the other, and a failure in one falls into the next. There is one path
    today. The shape is kept because it costs nothing and because the last
    time this took a list it walked it with a pointer and a stride, which is
    what you would want for ten of them and is not worth the arithmetic for
    two: there is nothing to get wrong in a branch to the next instruction,
    and there is something to get wrong in every stride.
    """
    entry = cave + HEAD
    code = []

    def here():
        return entry + len(code) * 4

    def emit(*words):
        code.extend(words)
        return len(code) - len(words)

    def address(index):
        return entry + index * 4

    emit(mflr(0))
    emit(stdu(1, 1, -FRAME))
    emit(std(0, 1, LR_SLOT))
    emit(std(31, 1, SAVE_R31))
    emit(std(30, 1, SAVE_R30))
    emit(mr(31, 3))                        # the online ID pointer, kept

    starts = []
    failures = []
    successes = []
    slots = []
    for number, _path in enumerate(paths):
        starts.append(len(code))
        # The address of this attempt's own path string, patched in once the
        # strings have been laid out at the end.
        slots.append(emit(*load32(3, 0)))

        # cellFsOpen(path, O_RDONLY, &fd, 0, 0)
        emit(li(4, 0))
        emit(addi(5, 1, FD))
        emit(li(6, 0))
        emit(li(7, 0))
        emit(bl(here(), marks.fs_open))
        emit(cmpwi(0, 3, 0))
        open_failed = emit(0)              # bne -> the next attempt

        # cellFsRead(fd, &account, 8, &nread)
        emit(lwz(3, 1, FD))
        emit(extsw(3, 3))
        emit(addi(4, 1, ACCOUNT))
        emit(li(5, ACCOUNT_ID_BYTES))
        emit(addi(6, 1, NREAD))
        emit(bl(here(), marks.fs_read))
        emit(mr(30, 3))                    # kept across the close

        # cellFsClose(fd), always, even after a read that failed
        emit(lwz(3, 1, FD))
        emit(extsw(3, 3))
        emit(bl(here(), marks.fs_close))

        emit(cmpwi(0, 30, 0))
        read_failed = emit(0)              # bne -> the next attempt
        emit(ld(4, 1, NREAD))
        emit(cmpdi(0, 4, ACCOUNT_ID_BYTES))
        short_read = emit(0)               # bne -> the next attempt

        failures.append((open_failed, read_failed, short_read))
        # A success jumps over the attempts that follow. The last attempt has
        # none to jump over: the formatting is the next instruction, and a
        # branch to the next instruction is the sort of thing that reads as a
        # mistake to whoever finds it.
        if number < len(paths) - 1:
            successes.append(emit(0))      # b   -> the formatting

    # va("%llu", account) and then getUserID on what it hands back
    formatting = len(code)
    emit(*load32(3, marks.format_string))
    emit(ld(4, 1, ACCOUNT))
    emit(bl(here(), marks.formatter))
    emit(cmpdi(0, 3, 0))
    no_string = emit(0)                    # beq -> the fallback
    emit(bl(here(), marks.get_user_id))
    done = emit(0)                         # b   -> the epilogue

    # The fallback: the game's own behaviour, on the pointer it was given.
    fallback = len(code)
    emit(mr(3, 31))
    emit(bl(here(), marks.get_user_id))

    epilogue = len(code)
    emit(ld(0, 1, LR_SLOT))
    emit(ld(31, 1, SAVE_R31))
    emit(ld(30, 1, SAVE_R30))
    emit(mtlr(0))
    emit(addi(1, 1, FRAME))
    emit(blr())

    # Every failure in one attempt goes to the start of the next, and every
    # failure in the last one goes to the fallback.
    for number, (open_failed, read_failed, short_read) in enumerate(failures):
        following = (starts[number + 1] if number + 1 < len(starts)
                     else fallback)
        for index in (open_failed, read_failed, short_read):
            code[index] = bne(address(index), address(following))
    for index in successes:
        code[index] = b(address(index), address(formatting))
    code[no_string] = beq(address(no_string), address(fallback))
    code[done] = b(address(done), address(epilogue))

    # The paths go after the code, each one NUL terminated and pointed at
    # directly by the attempt that uses it. Nothing walks from one to the
    # next, so they need no padding and there is no stride to be wrong about.
    strings = [path.encode("ascii") + b"\x00" for path in paths]
    at = entry + len(code) * 4
    for number, encoded in enumerate(strings):
        code[slots[number]] = lis(3, (at >> 16) & 0xFFFF)
        code[slots[number] + 1] = ori(3, 3, at & 0xFFFF)
        at += len(encoded)

    blob = bytearray(MARK) + b"\x00\x00\x00\x00"
    for word in code:
        blob += struct.pack(">I", word)
    for encoded in strings:
        blob += encoded
    struct.pack_into(">I", blob, len(MARK), len(blob))
    return bytes(blob), entry


# --- the state of a file ---------------------------------------------------

STOCK = "stock"
PATCHED = "patched"


def find_site(data):
    """(file offset of the hook, state), the way the other patchers spell it.

    state is "stock" when the hook still calls getUserID, "patched" when it
    calls a cave this program wrote, and None when it is neither -- which
    means somebody else has been here, and this program will not touch it.
    """
    image = Image(data)
    marks = find_landmarks(image)
    offset = image.at(marks.hook)
    word = image.word(marks.hook)
    if not is_branch(word, link=True):
        return offset, None
    target = branch_target(marks.hook, word)
    if target == marks.get_user_id:
        return offset, STOCK
    if cave_head(image, target):
        return offset, PATCHED
    return offset, None


def apply(data, title_id):
    """Patch a decrypted image. Returns (bytes, dict of what was done).

    title_id names the folder the readable copy of np_cache.dat is placed in,
    and is written into the cave as data, so it is the title on the console
    rather than one assumed here.

    Which account the copy came from is not this function's business. The tool
    reads the signed in user's np_cache.dat and puts a copy where this path
    points; from in here one copy looks like another, and the account it
    belongs to is settled before the file ever gets to the game's folder.
    """
    _offset, state = find_site(data)
    if state is None:
        raise NotThisBuild(
            "the call this fix replaces has already been changed to "
            "something else, so the file is left alone")
    if state == PATCHED:
        # Re-applying is the ordinary way the path is refreshed when the
        # signed in account changes, so it is done from the original rather
        # than layered on top of what is already there.
        data = restore(data)
    image = Image(data)
    marks = find_landmarks(image)
    paths = [USRDIR_PATH % title_id]
    blob, entry = build_cave(marks, marks.cave, paths)
    if len(blob) > marks.cave_size:
        raise NotThisBuild(
            f"the fix needs {len(blob)} bytes and the largest free run in "
            f"this build is {marks.cave_size}")
    out = bytearray(data)
    cave_at = image.at(marks.cave)
    if any(out[cave_at:cave_at + len(blob)]):
        raise NotThisBuild(
            f"the {len(blob)} bytes at {marks.cave:08X} are not all zero, so "
            f"this is not free space and nothing has been written")
    out[cave_at:cave_at + len(blob)] = blob
    hook_at = image.at(marks.hook)
    struct.pack_into(">I", out, hook_at, bl(marks.hook, entry))
    return bytes(out), {"hook": marks.hook, "offset": hook_at,
                        "cave": marks.cave, "entry": entry,
                        "bytes": len(blob), "paths": paths,
                        "landmarks": marks.as_dict()}


def restore(data):
    """Put the original call back and wipe the cave. Returns bytes.

    The cave is found through the hook rather than by looking for free space
    again: by this point it is not free space, and the length to wipe is the
    one written into its own head.
    """
    image = Image(data)
    get_user_id = find_get_user_id(image)
    hook, _callers = find_hook(image, get_user_id)
    out = bytearray(data)
    word = image.word(hook)
    if is_branch(word, link=True):
        head = cave_head(image, branch_target(hook, word))
        if head:
            start, length = head
            position = image.locate(start, length)
            if position is not None:
                out[position:position + length] = b"\x00" * length
    struct.pack_into(">I", out, image.at(hook), bl(hook, get_user_id))
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
    parser.add_argument("binary", help="decrypted Black Ops 1 MP ELF")
    parser.add_argument("-o", "--output",
                        help="write here instead of in place")
    parser.add_argument("--title-id", default="BLES01031",
                        help="the folder the readable np_cache.dat is in")
    parser.add_argument("--check", action="store_true",
                        help="report what was found and the state, then stop")
    parser.add_argument("--restore", action="store_true",
                        help="put the original call back")
    args = parser.parse_args()

    data = open(args.binary, "rb").read()
    print("input   %s" % args.binary)
    print("size    %d bytes" % len(data))
    print("sha1    %s" % sha1(args.binary))

    try:
        image = Image(data)
        marks = find_landmarks(image)
    except NotThisBuild as exc:
        sys.exit("this is not a build the fix understands: %s" % exc)

    for name, value in marks.as_dict().items():
        if name == "callers":
            print("callers %s" % " ".join("%08X" % item for item in value))
        elif name == "cave_size":
            print("free    %d bytes at the cave" % value)
        else:
            print("%-7s %08X" % (name, value))

    _offset, state = find_site(data)
    print("state   %s" % (state or "not recognised"))
    if args.check:
        return
    if state is None:
        sys.exit("the hook calls something this program did not write, so it "
                 "is left alone")

    if args.restore:
        if state == STOCK:
            print("nothing to do, already stock")
            return
        out = restore(data)
    else:
        if state == PATCHED:
            print("nothing to do, already patched")
            return
        out, what = apply(data, args.title_id)
        print()
        for path in what["paths"]:
            print("path    %s" % path)
        print("cave    %08X, %d bytes" % (what["cave"], what["bytes"]))

    destination = args.output or args.binary
    if args.output:
        shutil.copyfile(args.binary, destination)
    open(destination, "wb").write(out)
    print("wrote   %s" % destination)
    print("sha1    %s" % hashlib.sha1(out).hexdigest())


if __name__ == "__main__":
    main()
