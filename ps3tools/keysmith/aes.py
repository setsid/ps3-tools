"""AES-128/192/256 in pure Python, enough for what SCE files need.

The project depends on PySide6 and PyInstaller and nothing else, and a native
crypto wheel is exactly the kind of thing that turns a working Windows build
into a support thread. So this is written out here instead.

Only three modes are needed: ECB for single-block key derivation, CBC for the
small header blocks, and CTR for the bulk section data. CTR and CBC encryption
need the forward cipher only; the inverse cipher is used on header-sized
buffers, so the fast table-driven path is spent where it pays and the inverse
stays plain.

Speed matters here. A retail SELF is eleven megabytes and every byte of it goes
through CTR twice in a round trip. The forward cipher is table driven, the
keystream is built in blocks, and the exclusive-or is done on one big integer
rather than byte by byte, which is several times quicker than a loop.
"""

import struct

SBOX = bytes([
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B,
    0xFE, 0xD7, 0xAB, 0x76, 0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0,
    0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0, 0xB7, 0xFD, 0x93, 0x26,
    0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2,
    0xEB, 0x27, 0xB2, 0x75, 0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0,
    0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84, 0x53, 0xD1, 0x00, 0xED,
    0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F,
    0x50, 0x3C, 0x9F, 0xA8, 0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5,
    0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2, 0xCD, 0x0C, 0x13, 0xEC,
    0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14,
    0xDE, 0x5E, 0x0B, 0xDB, 0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C,
    0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79, 0xE7, 0xC8, 0x37, 0x6D,
    0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F,
    0x4B, 0xBD, 0x8B, 0x8A, 0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E,
    0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E, 0xE1, 0xF8, 0x98, 0x11,
    0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F,
    0xB0, 0x54, 0xBB, 0x16])

INV_SBOX = bytes(256)
_inv = bytearray(256)
for _i, _v in enumerate(SBOX):
    _inv[_v] = _i
INV_SBOX = bytes(_inv)

RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80,
        0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D, 0x9A)


def _xtime(value):
    value <<= 1
    if value & 0x100:
        value = (value ^ 0x1B) & 0xFF
    return value


def _mul(a, b):
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _build_tables():
    """The four forward T-tables and the four inverse ones."""
    te = [[0] * 256 for _ in range(4)]
    td = [[0] * 256 for _ in range(4)]
    for index in range(256):
        s = SBOX[index]
        word = (_mul(s, 2) << 24) | (s << 16) | (s << 8) | _mul(s, 3)
        for shift in range(4):
            te[shift][index] = ((word >> (8 * shift))
                                | (word << (32 - 8 * shift))) & 0xFFFFFFFF
        t = INV_SBOX[index]
        word = ((_mul(t, 0x0E) << 24) | (_mul(t, 0x09) << 16)
                | (_mul(t, 0x0D) << 8) | _mul(t, 0x0B))
        for shift in range(4):
            td[shift][index] = ((word >> (8 * shift))
                                | (word << (32 - 8 * shift))) & 0xFFFFFFFF
    return te, td


TE, TD = _build_tables()
TE0, TE1, TE2, TE3 = TE
TD0, TD1, TD2, TD3 = TD


class AES:
    """One key, expanded once. Instances are cheap to keep and reuse."""

    BLOCK = 16

    def __init__(self, key):
        if len(key) not in (16, 24, 32):
            raise ValueError(f"AES key must be 16, 24 or 32 bytes, "
                             f"got {len(key)}")
        self.key = bytes(key)
        self.rounds = {16: 10, 24: 12, 32: 14}[len(key)]
        self._round_keys = self._expand(self.key)
        self._inv_round_keys = None

    # -- key schedule

    def _expand(self, key):
        words = list(struct.unpack(">%dI" % (len(key) // 4), key))
        count = len(words)
        total = 4 * (self.rounds + 1)
        for index in range(count, total):
            word = words[index - 1]
            if index % count == 0:
                word = ((word << 8) | (word >> 24)) & 0xFFFFFFFF
                word = ((SBOX[(word >> 24) & 0xFF] << 24)
                        | (SBOX[(word >> 16) & 0xFF] << 16)
                        | (SBOX[(word >> 8) & 0xFF] << 8)
                        | SBOX[word & 0xFF])
                word ^= RCON[index // count - 1] << 24
            elif count > 6 and index % count == 4:
                word = ((SBOX[(word >> 24) & 0xFF] << 24)
                        | (SBOX[(word >> 16) & 0xFF] << 16)
                        | (SBOX[(word >> 8) & 0xFF] << 8)
                        | SBOX[word & 0xFF])
            words.append(word ^ words[index - count])
        return words

    def _inverse_schedule(self):
        """The inverse schedule, built the first time it is wanted."""
        if self._inv_round_keys is not None:
            return self._inv_round_keys
        keys = list(self._round_keys)
        out = [0] * len(keys)
        rounds = self.rounds
        for step in range(rounds + 1):
            for column in range(4):
                out[4 * step + column] = keys[4 * (rounds - step) + column]
        for step in range(1, rounds):
            for column in range(4):
                word = out[4 * step + column]
                out[4 * step + column] = (
                    TD0[SBOX[(word >> 24) & 0xFF]]
                    ^ TD1[SBOX[(word >> 16) & 0xFF]]
                    ^ TD2[SBOX[(word >> 8) & 0xFF]]
                    ^ TD3[SBOX[word & 0xFF]])
        self._inv_round_keys = out
        return out

    # -- one block

    def encrypt_block(self, block):
        return struct.pack(">4I", *self._encrypt_words(
            *struct.unpack(">4I", block)))

    def _encrypt_words(self, s0, s1, s2, s3):
        rk = self._round_keys
        te0, te1, te2, te3 = TE0, TE1, TE2, TE3
        s0 ^= rk[0]
        s1 ^= rk[1]
        s2 ^= rk[2]
        s3 ^= rk[3]
        offset = 4
        for _ in range(self.rounds - 1):
            t0 = (te0[(s0 >> 24) & 0xFF] ^ te1[(s1 >> 16) & 0xFF]
                  ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ rk[offset])
            t1 = (te0[(s1 >> 24) & 0xFF] ^ te1[(s2 >> 16) & 0xFF]
                  ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ rk[offset + 1])
            t2 = (te0[(s2 >> 24) & 0xFF] ^ te1[(s3 >> 16) & 0xFF]
                  ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ rk[offset + 2])
            t3 = (te0[(s3 >> 24) & 0xFF] ^ te1[(s0 >> 16) & 0xFF]
                  ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ rk[offset + 3])
            s0, s1, s2, s3 = t0, t1, t2, t3
            offset += 4
        sbox = SBOX
        t0 = ((sbox[(s0 >> 24) & 0xFF] << 24) | (sbox[(s1 >> 16) & 0xFF] << 16)
              | (sbox[(s2 >> 8) & 0xFF] << 8) | sbox[s3 & 0xFF]) ^ rk[offset]
        t1 = ((sbox[(s1 >> 24) & 0xFF] << 24)
              | (sbox[(s2 >> 16) & 0xFF] << 16)
              | (sbox[(s3 >> 8) & 0xFF] << 8)
              | sbox[s0 & 0xFF]) ^ rk[offset + 1]
        t2 = ((sbox[(s2 >> 24) & 0xFF] << 24)
              | (sbox[(s3 >> 16) & 0xFF] << 16)
              | (sbox[(s0 >> 8) & 0xFF] << 8)
              | sbox[s1 & 0xFF]) ^ rk[offset + 2]
        t3 = ((sbox[(s3 >> 24) & 0xFF] << 24)
              | (sbox[(s0 >> 16) & 0xFF] << 16)
              | (sbox[(s1 >> 8) & 0xFF] << 8)
              | sbox[s2 & 0xFF]) ^ rk[offset + 3]
        return t0, t1, t2, t3

    def decrypt_block(self, block):
        rk = self._inverse_schedule()
        s0, s1, s2, s3 = struct.unpack(">4I", block)
        s0 ^= rk[0]
        s1 ^= rk[1]
        s2 ^= rk[2]
        s3 ^= rk[3]
        td0, td1, td2, td3 = TD0, TD1, TD2, TD3
        offset = 4
        for _ in range(self.rounds - 1):
            t0 = (td0[(s0 >> 24) & 0xFF] ^ td1[(s3 >> 16) & 0xFF]
                  ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ rk[offset])
            t1 = (td0[(s1 >> 24) & 0xFF] ^ td1[(s0 >> 16) & 0xFF]
                  ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ rk[offset + 1])
            t2 = (td0[(s2 >> 24) & 0xFF] ^ td1[(s1 >> 16) & 0xFF]
                  ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ rk[offset + 2])
            t3 = (td0[(s3 >> 24) & 0xFF] ^ td1[(s2 >> 16) & 0xFF]
                  ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ rk[offset + 3])
            s0, s1, s2, s3 = t0, t1, t2, t3
            offset += 4
        inv = INV_SBOX
        t0 = ((inv[(s0 >> 24) & 0xFF] << 24) | (inv[(s3 >> 16) & 0xFF] << 16)
              | (inv[(s2 >> 8) & 0xFF] << 8) | inv[s1 & 0xFF]) ^ rk[offset]
        t1 = ((inv[(s1 >> 24) & 0xFF] << 24) | (inv[(s0 >> 16) & 0xFF] << 16)
              | (inv[(s3 >> 8) & 0xFF] << 8) | inv[s2 & 0xFF]) ^ rk[offset + 1]
        t2 = ((inv[(s2 >> 24) & 0xFF] << 24) | (inv[(s1 >> 16) & 0xFF] << 16)
              | (inv[(s0 >> 8) & 0xFF] << 8) | inv[s3 & 0xFF]) ^ rk[offset + 2]
        t3 = ((inv[(s3 >> 24) & 0xFF] << 24) | (inv[(s2 >> 16) & 0xFF] << 16)
              | (inv[(s1 >> 8) & 0xFF] << 8) | inv[s0 & 0xFF]) ^ rk[offset + 3]
        return struct.pack(">4I", t0, t1, t2, t3)


def _xor(left, right):
    """Exclusive-or of two equal-length byte strings, via one big integer."""
    size = len(left)
    if size == 0:
        return b""
    return (int.from_bytes(left, "big")
            ^ int.from_bytes(right, "big")).to_bytes(size, "big")


def xor_bytes(left, right):
    """Exclusive-or of two equal-length byte strings."""
    if len(left) != len(right):
        raise ValueError(f"lengths differ: {len(left)} and {len(right)}")
    return _xor(bytes(left), bytes(right))


def ecb_encrypt(key, data):
    cipher = AES(key)
    return b"".join(cipher.encrypt_block(data[at:at + 16])
                    for at in range(0, len(data), 16))


def ecb_decrypt(key, data):
    cipher = AES(key)
    return b"".join(cipher.decrypt_block(data[at:at + 16])
                    for at in range(0, len(data), 16))


def cbc_encrypt(key, iv, data):
    cipher = AES(key)
    previous = bytes(iv)
    out = []
    for at in range(0, len(data), 16):
        block = cipher.encrypt_block(_xor(data[at:at + 16], previous))
        out.append(block)
        previous = block
    return b"".join(out)


def cbc_decrypt(key, iv, data):
    cipher = AES(key)
    previous = bytes(iv)
    out = []
    for at in range(0, len(data), 16):
        block = bytes(data[at:at + 16])
        out.append(_xor(cipher.decrypt_block(block), previous))
        previous = block
    return b"".join(out)


def ctr_keystream(key, counter, length):
    """length bytes of AES-CTR keystream from a 16-byte big-endian counter.

    The counter increments over the whole 128 bits, which is what the PS3 does
    and what scetool does. Rolling over only the low 64 would differ after
    2**64 blocks and never in practice, but the whole-width version costs
    nothing here.
    """
    cipher = AES(key)
    value = int.from_bytes(counter, "big")
    pack = struct.pack
    unpack = struct.unpack
    encrypt = cipher._encrypt_words
    blocks = []
    append = blocks.append
    for _ in range((length + 15) // 16):
        words = unpack(">4I", value.to_bytes(16, "big"))
        append(pack(">4I", *encrypt(*words)))
        value = (value + 1) & ((1 << 128) - 1)
    return b"".join(blocks)[:length]


def ctr_crypt(key, counter, data):
    """CTR is its own inverse, so this both encrypts and decrypts."""
    return _xor(bytes(data), ctr_keystream(key, counter, len(data)))


def omac1(key, message):
    """AES-OMAC1, which is AES-CMAC. Checked against the RFC 4493 vectors.

    This is what the two NPDRM hashes are built on. It is here rather than in
    a module of its own because it is a mode of the cipher above and nothing
    else uses it.
    """
    cipher = AES(key)

    def double(block):
        value = int.from_bytes(block, "big")
        shifted = (value << 1) & ((1 << 128) - 1)
        if value & (1 << 127):
            shifted ^= 0x87
        return shifted.to_bytes(16, "big")

    subkey = double(cipher.encrypt_block(b"\x00" * 16))
    message = bytes(message)
    if message and len(message) % 16 == 0:
        blocks = len(message) // 16
        tail = _xor(message[-16:], subkey)
    else:
        subkey = double(subkey)
        blocks = len(message) // 16 + 1
        padded = message[(blocks - 1) * 16:]
        padded = padded + b"\x80" + b"\x00" * (15 - len(padded))
        tail = _xor(padded, subkey)
    state = b"\x00" * 16
    for index in range(blocks - 1):
        chunk = message[index * 16:index * 16 + 16]
        state = cipher.encrypt_block(_xor(state, chunk))
    return cipher.encrypt_block(_xor(state, tail))
