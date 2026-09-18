"""ECDSA over the curves a keyset's ctype selects, standard library only.

This exists for one field. A SELF carries a signature at the end of its
metadata run, and every path in this package used to carry the template's
own through because there was nothing to make another with. That is right
for a retail rebuild and wrong for a PS3HEN one: the community keys file
holds a real private key for key revision 0x000A, so scetool signed the
PS3HEN builds in the survey for real, and a file rebuilt from a custom
firmware template carried that template's zeros instead.

Four NPDRM revisions in that keys file carry a private key, 0x0001, 0x0004,
0x0007 and 0x000A, and 0x000A is the one PS3HEN wants. Each was checked the
only way that settles it: the private key times the base point is the public
key the same keyset carries, on all twenty-seven keysets in the file that
have a private key at all, SELF keysets and the pkg, spp and rvk entries
alike.

## The curve table, and how its layout was found

`data/ldr_curves` is 7744 bytes, which is 64 entries of 121, and a keyset's
ctype selects one. An entry is p, a and b at twenty bytes each, then the
order at twenty-one, then the base point as two twenty byte halves:

    0x00  p    20      the prime field
    0x14  a    20      always p - 3
    0x28  b    20
    0x3C  N    21      the order of the base point
    0x51  Gx   20
    0x65  Gy   20

**Every byte of both curve files is stored complemented.** That is the part
that costs an evening, and it was not guessed at. Read as they stand, the
files hold no prime anywhere: every 20 and 21 byte window of all 64 entries
was tested and none of them is one, so nothing in the file can be a prime
field. Complemented, the first twenty bytes of all 64 entries are prime, the
next twenty are exactly p - 3, the base point satisfies the curve equation,
and the order is prime as well. Four independent checks on sixty-four
entries, and then the twenty-seven private keys above.

The complement also explains the byte the raw file carries at 0x3C of every
entry, which is 0xFF: it is the high byte of a 21 byte order whose real
value is zero, because these orders all fit in twenty bytes and the field
was made one byte wider to hold one that does not.

`data/vsh_curves` is 360 bytes, which is 3 entries of 120, and it is the
same encoding with the order at twenty bytes rather than twenty-one. Its
first entry is filler. What indexes it is not known here: every ctype in the
keys file resolves against `ldr_curves`, including the pkg, spp and rvk
entries, so nothing in this package reads the vsh table and it is described
here only so the next person does not have to find it again.

## What is signed

The signature covers `signature_input_length` bytes from the start of the
file, hashed with SHA-1, with the metadata run and the metadata info block
in the clear. That ordering is scetool's: it builds the header, signs it,
writes the signature at the end of the metadata run and encrypts the run
afterwards, so the bytes that were hashed are never the bytes on disk.
Confirmed by verifying real signatures both ways round: the plaintext form
verifies on all fifteen retail files here across three key revisions and on
all fourteen PS3HEN builds in the survey, and the on-disk form verifies on
none of them.

## What this is not

A hardened implementation. The scalar multiplication is plain affine
double-and-add and its timing follows the bits of the key. That is
deliberate: the only private key it will ever hold is one published years
ago in a keys file anybody can download, and a signature is one scalar
multiplication, so there is nothing here worth the complexity of a constant
time ladder.
"""

import hashlib
import os

from .errors import SigningFailed

#: One entry of each curve file, and where the fields sit inside it.
LDR_CURVE_BYTES = 121
VSH_CURVE_BYTES = 120

#: The signature, as two halves of this size. A 160 bit value in 21 bytes
#: leaves the first byte zero, which is what every real signature read here
#: carries, and the two halves together are the 0x2A bytes the metadata run
#: ends with.
HALF_BYTES = 21
SIGNATURE_BYTES = HALF_BYTES * 2


class Curve:
    """One entry of a curve file, in the clear."""

    def __init__(self, p, a, b, order, gx, gy, ctype=0):
        self.p = p
        self.a = a
        self.b = b
        self.order = order
        self.g = (gx, gy)
        self.ctype = ctype

    def holds(self, candidate):
        """Whether this point satisfies the curve equation."""
        if candidate is None:
            return False
        x, y = candidate
        if not (0 <= x < self.p and 0 <= y < self.p):
            return False
        return (y * y - x * x * x - self.a * x - self.b) % self.p == 0

    def __repr__(self):
        return f"<Curve ctype=0x{self.ctype:02X} {self.p.bit_length()} bit>"


def default_path(name="ldr_curves"):
    """A curve file shipped inside this package.

    Worked out from this module's own path for the same reason keys.py does
    it: scetool resolved its data folder against the working directory, so
    the same command worked or failed depending on which folder it was typed
    in, and the failure came out as "Could not decrypt header".
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", name)


_CACHE = {}


def load(path=""):
    """Every entry of a curve file, complemented and parsed.

    Read once and kept, because the table is small and a rebuild asks for
    the same entry every time.
    """
    path = path or default_path()
    key = os.path.abspath(path)
    if key in _CACHE:
        return _CACHE[key]
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        if not os.path.exists(path):
            raise SigningFailed(
                "there is no curve table here. It is naehrwert's work and is "
                "not redistributed with this source, the same as the keys "
                "file. Put a copy of scetool's data folder at "
                + os.path.dirname(path) + ". Signing a file at key revision "
                "0x000A needs it, because that is where the curve the "
                "keyset's ctype names lives.",
                path=path, field="curve table") from None
        raise SigningFailed(f"the curve table could not be read "
                            f"({exc.strerror})", path=path,
                            field="curve table") from None
    # The two tables differ only in how wide the order is, and their lengths
    # say which is which: 7744 is 64 entries of 121 and divides by nothing
    # else here, 360 is 3 of 120.
    if raw and len(raw) % LDR_CURVE_BYTES == 0:
        size = LDR_CURVE_BYTES
    elif raw and len(raw) % VSH_CURVE_BYTES == 0:
        size = VSH_CURVE_BYTES
    else:
        raise SigningFailed(
            "this curve table is not a whole number of entries",
            path=path, field="curve table", found=len(raw),
            expected=f"a multiple of {LDR_CURVE_BYTES} "
                     f"or {VSH_CURVE_BYTES}")
    # Complemented. See the module docstring for the evidence, which is that
    # nothing in either file reads as a prime until every byte is flipped and
    # then everything in them does.
    clear = bytes(byte ^ 0xFF for byte in raw)
    curves = []
    for index in range(len(clear) // size):
        entry = clear[index * size:(index + 1) * size]
        # Five fields of twenty bytes and the order, which takes whatever is
        # left over: twenty-one in the loader table and twenty in the vsh
        # one.
        at = 60 + (size - 5 * 20)
        curves.append(Curve(
            int.from_bytes(entry[0:20], "big"),
            int.from_bytes(entry[20:40], "big"),
            int.from_bytes(entry[40:60], "big"),
            int.from_bytes(entry[60:at], "big"),
            int.from_bytes(entry[at:at + 20], "big"),
            int.from_bytes(entry[at + 20:at + 40], "big"),
            index))
    _CACHE[key] = curves
    return curves


def curve(ctype, path=""):
    """The curve a keyset's ctype names.

    A ctype outside the table is refused rather than masked down into it.
    Sixty-four entries is six bits, so masking is the obvious thing to do and
    it cannot be told apart from a bounds check on this keys file, where
    every ctype is inside the table already. What a keyset with a ctype
    outside it would mean is not known here, and signing against a curve
    nobody chose is worse than saying so.
    """
    curves = load(path)
    index = int(ctype)
    if not 0 <= index < len(curves):
        raise SigningFailed(
            f"there is no curve {index} in a table of {len(curves)}",
            path=path or default_path(), field="ctype", found=index,
            expected=f"< {len(curves)}")
    return curves[index]


def _add(curve_, left, right):
    """Two points added, with None standing for the point at infinity."""
    if left is None:
        return right
    if right is None:
        return left
    p = curve_.p
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % p == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1 + curve_.a) * pow(2 * y1, -1, p) % p
    else:
        slope = (y2 - y1) * pow((x2 - x1) % p, -1, p) % p
    x3 = (slope * slope - x1 - x2) % p
    return (x3, (slope * (x1 - x3) - y1) % p)


def multiply(curve_, scalar, point):
    """A point multiplied by a scalar, by double and add."""
    result = None
    scalar = int(scalar)
    if scalar < 0:
        raise SigningFailed("a scalar cannot be negative", field="scalar",
                            found=scalar)
    while scalar:
        if scalar & 1:
            result = _add(curve_, result, point)
        point = _add(curve_, point, point)
        scalar >>= 1
    return result


def point(blob):
    """A public key's two halves as a point.

    Both halves are the same length, which is how the keys file stores a
    public key: 40 bytes for the 160 bit curves every keyset here names.
    """
    raw = bytes(blob)
    if not raw or len(raw) % 2:
        raise SigningFailed("a public key is two halves of equal length",
                            field="public key", found=len(raw))
    half = len(raw) // 2
    return (int.from_bytes(raw[:half], "big"),
            int.from_bytes(raw[half:], "big"))


def _hash_value(curve_, digest):
    """The number a digest stands for in the signature equations.

    The digest is used as it is, big endian, and reduced modulo the order.
    Nothing is truncated, because SHA-1 and these orders are both 160 bits
    wide, and a digest above the order is simply reduced. Confirmed by the
    real files rather than by reading a standard: all twenty-nine of them
    verify under this reading, and ten of those twenty-nine have a digest
    above the order, so the reduction is exercised rather than assumed.
    """
    return int.from_bytes(bytes(digest), "big") % curve_.order


def split(signature):
    """A signature's two halves as (r, s)."""
    raw = bytes(signature)
    if len(raw) < SIGNATURE_BYTES:
        raise SigningFailed(
            "a signature is two halves of 21 bytes", field="signature",
            expected=SIGNATURE_BYTES, found=len(raw))
    return (int.from_bytes(raw[:HALF_BYTES], "big"),
            int.from_bytes(raw[HALF_BYTES:SIGNATURE_BYTES], "big"))


def verify(curve_, public, digest, signature):
    """Whether this signature over this digest is the holder's.

    public is the keyset's 40 byte pub field or a point. Returns False for
    anything malformed rather than raising, because a file carrying a
    signature that does not verify is a fact about the file.
    """
    order = curve_.order
    r, s = split(signature)
    if not (0 < r < order and 0 < s < order):
        return False
    key = public if isinstance(public, tuple) else point(public)
    if not curve_.holds(key):
        return False
    value = _hash_value(curve_, digest)
    inverse = pow(s, -1, order)
    found = _add(curve_,
                 multiply(curve_, value * inverse % order, curve_.g),
                 multiply(curve_, r * inverse % order, key))
    if found is None:
        return False
    return found[0] % order == r


def sign(curve_, private, digest, nonce=None):
    """A signature over this digest, as two 21 byte halves.

    The nonce is random, which is what scetool's is: the fourteen PS3HEN
    builds in the survey carry fourteen different ones, recovered from the
    signatures themselves now that the private key is to hand. So a
    signature made here cannot match one made there byte for byte, and
    nothing outside the file needs it to. What a console checks is that the
    signature verifies against the public key, and any nonce gives that.

    nonce is for the tests, which need a signature that does not move.
    """
    order = curve_.order
    key = int.from_bytes(bytes(private), "big")
    if not key:
        raise SigningFailed(
            "this keyset has no private key, so it can check a signature "
            "and cannot make one. The keys file carries a private key for "
            "NPDRM key revisions 0x0001, 0x0004, 0x0007 and 0x000A, and "
            "zeros for every other", field="private key", found="zero")
    if key >= order:
        raise SigningFailed(
            "this private key is larger than the order of the curve its "
            "keyset's ctype names, so the key and the curve do not belong "
            "together", field="private key",
            found=f"{key.bit_length()} bits",
            expected=f"< {order.bit_length()} bits")
    value = _hash_value(curve_, digest)
    while True:
        k = int(nonce) % order if nonce is not None else _random_scalar(order)
        made = _halves(curve_, key, value, k)
        if made is not None:
            return made
        # A nonce that gives r or s of zero is thrown away and another
        # drawn, which is what the scheme says to do. It has never happened
        # here and it will not: r is an x coordinate and s is a product, and
        # either being exactly zero is about as likely as guessing the key.
        # A caller who asked for one particular nonce is told rather than
        # looped over, because the answer would not change.
        if nonce is not None:
            raise SigningFailed(
                "this nonce gives a signature with a half of zero, which is "
                "not a signature. Any other nonce will do",
                field="nonce", found=k)


def _halves(curve_, key, value, k):
    """(r, s) for one nonce, or None where that nonce gives neither."""
    order = curve_.order
    if not k:
        return None
    found = multiply(curve_, k, curve_.g)
    if found is None:
        return None
    r = found[0] % order
    if not r:
        return None
    s = (value + r * key) * pow(k, -1, order) % order
    if not s:
        return None
    return r.to_bytes(HALF_BYTES, "big") + s.to_bytes(HALF_BYTES, "big")


def _random_scalar(order):
    """A nonce, from the system generator and nothing else.

    Rejection sampling rather than a modulo of something wider, so every
    value in range is as likely as every other. Cheap: the orders here are
    all a little over half of the range the twenty bytes cover, so two draws
    is the usual worst case.
    """
    while True:
        value = int.from_bytes(os.urandom((order.bit_length() + 7) // 8),
                               "big")
        if 0 < value < order:
            return value


def digest_of(data):
    """The SHA-1 the signature is over, named here so callers agree on it."""
    return hashlib.sha1(bytes(data)).digest()
