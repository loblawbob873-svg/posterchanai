"""bech32 (BIP173) + minimal NIP-19 encode/decode for Nostr keys.

Only what Nostr needs: the simple `npub`/`nsec`/`note` forms (32-byte payload,
no TLV). `decode_key` also accepts a bare 64-char hex key so callers can paste
either form. TLV forms (nprofile/nevent) aren't minted here; `decode_any` can
still pull the primary 32-byte value out of an nevent/nprofile for convenience.
"""

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _verify_checksum(hrp, data):
    return _polymod(_hrp_expand(hrp) + data) == 1


def _create_checksum(hrp, data):
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32_encode(hrp, data):
    combined = data + _create_checksum(hrp, data)
    return hrp + "1" + "".join([CHARSET[d] for d in combined])


def bech32_decode(bech):
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return (None, None)
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        return (None, None)
    if not all(x in CHARSET for x in bech[pos + 1:]):
        return (None, None)
    hrp = bech[:pos]
    data = [CHARSET.find(x) for x in bech[pos + 1:]]
    if not _verify_checksum(hrp, data):
        return (None, None)
    return (hrp, data[:-6])


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def encode(hrp: str, payload: bytes) -> str:
    """Encode a raw byte payload as a bech32 string with the given hrp (npub/nsec/note)."""
    data = _convertbits(list(payload), 8, 5)
    return bech32_encode(hrp, data)


def decode(expected_hrp: str, bech: str) -> bytes | None:
    """Decode a bech32 string, returning the raw bytes if the hrp matches."""
    hrp, data = bech32_decode(bech)
    if hrp != expected_hrp or data is None:
        return None
    decoded = _convertbits(data, 5, 8, False)
    return bytes(decoded) if decoded is not None else None


def decode_any(bech: str) -> bytes | None:
    """Decode any nostr bech32 entity and return its primary 32-byte value.

    For simple npub/nsec/note that's the whole payload; for TLV forms
    (nprofile/nevent) it's the first 32-byte TLV value (the pubkey/event id)."""
    hrp, data = bech32_decode(bech)
    if data is None:
        return None
    raw = _convertbits(data, 5, 8, False)
    if raw is None:
        return None
    raw = bytes(raw)
    if hrp in ("npub", "nsec", "note"):
        return raw if len(raw) == 32 else None
    # TLV (nprofile/nevent): type/length/value triples; type 0 = the 32-byte special value.
    i = 0
    while i + 2 <= len(raw):
        t, ln = raw[i], raw[i + 1]
        val = raw[i + 2:i + 2 + ln]
        if t == 0 and len(val) == 32:
            return val
        i += 2 + ln
    return None


def is_hex_key(s: str) -> bool:
    s = s.strip()
    if len(s) != 64:
        return False
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def decode_key(s: str) -> bytes | None:
    """Accept a secret key as `nsec1...` or bare 64-char hex; return 32 raw bytes."""
    s = s.strip()
    if is_hex_key(s):
        return bytes.fromhex(s)
    if s.startswith("nsec1"):
        return decode("nsec", s)
    return None
