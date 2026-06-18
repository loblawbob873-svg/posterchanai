"""NIP-77 Negentropy (protocol v1) — non-initiator / relay side.

Range-Based Set Reconciliation: the client and relay exchange a compact summary of which
events each has (over a filter), so the client can pull only what it's missing instead of
refetching the whole timeline. We implement the *non-initiator* role: parse the client's
message, compare against our own sorted (timestamp, id) set, and reply with ranges that
narrow the difference (Skip / Fingerprint / IdList), recursing until resolved.

Wire format (v1):
    message  := <0x61> <range>*
    range    := <encoded-upper-bound> <mode varint> <payload>
    mode     := 0 Skip | 1 Fingerprint | 2 IdList
    Fingerprint payload := <16 bytes>
    IdList     payload  := <count varint> <id (32 bytes)>*
    bound    := <timestamp delta varint> <id-prefix-len varint> <id-prefix bytes>

Reference: https://github.com/hoytech/negentropy (the protocol strfry/nostrudel speak).
The server is stateless across rounds (it rebuilds its set per NEG-OPEN), which is fine for
our scale.
"""

import hashlib

PROTOCOL_VERSION = 0x61          # version 1
ID_SIZE = 32
FINGERPRINT_SIZE = 16
MAX_U64 = (1 << 64) - 1
_BUCKETS = 16
_MASK256 = (1 << 256) - 1


# --- varint -----------------------------------------------------------------

def encode_varint(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    o = bytearray()
    while n:
        o.append(n & 0x7F)
        n >>= 7
    o.reverse()
    for i in range(len(o) - 1):
        o[i] |= 0x80
    return bytes(o)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("negentropy: read past end")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def read_varint(self) -> int:
        n = 0
        while True:
            byte = self.read(1)[0]
            n = (n << 7) | (byte & 0x7F)
            if not (byte & 0x80):
                return n


# --- fingerprint ------------------------------------------------------------

def _fingerprint(ids) -> bytes:
    acc = 0
    for i in ids:
        acc = (acc + int.from_bytes(i, "little")) & _MASK256
    return hashlib.sha256(acc.to_bytes(32, "little") + encode_varint(len(ids))).digest()[:FINGERPRINT_SIZE]


# --- bounds -----------------------------------------------------------------

class Bound:
    """An exclusive upper bound = (timestamp, id-prefix)."""
    __slots__ = ("timestamp", "id")

    def __init__(self, timestamp: int, id_prefix: bytes = b""):
        self.timestamp = timestamp
        self.id = id_prefix

    @staticmethod
    def infinity() -> "Bound":
        return Bound(MAX_U64, b"")


def _item_lt_bound(ts: int, eid: bytes, b: Bound) -> bool:
    """Is item (ts, eid) strictly below bound b?"""
    if ts != b.timestamp:
        return ts < b.timestamp
    return eid[:len(b.id)] < b.id  # equal-or-longer prefix is NOT below


def _lower_index(items, b: Bound) -> int:
    """First index i with items[i] >= b (items sorted by (ts, id))."""
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi) // 2
        ts, eid = items[mid]
        if _item_lt_bound(ts, eid, b):
            lo = mid + 1
        else:
            hi = mid
    return lo


def _encode_bound(out: bytearray, b: Bound, state: list) -> None:
    # state[0] = last encoded timestamp (delta coding). 0 means infinity.
    if b.timestamp == MAX_U64:
        out += encode_varint(0)
        state[0] = MAX_U64
    else:
        delta = b.timestamp - state[0]
        state[0] = b.timestamp
        out += encode_varint(delta + 1)
    out += encode_varint(len(b.id))
    out += b.id


def _decode_bound(r: _Reader, state: list) -> Bound:
    enc = r.read_varint()
    if enc == 0:
        state[0] = MAX_U64
        ts = MAX_U64
    else:
        ts = state[0] + (enc - 1)
        state[0] = ts
    prefix_len = r.read_varint()
    if prefix_len > ID_SIZE:
        raise ValueError("negentropy: bad id prefix len")
    prefix = r.read(prefix_len)
    return Bound(ts, prefix)


def _minimal_bound(prev, curr) -> Bound:
    """Smallest bound that separates item `prev` from item `curr` (prev < curr)."""
    p_ts, p_id = prev
    c_ts, c_id = curr
    if p_ts != c_ts:
        return Bound(c_ts, b"")
    # same timestamp: shortest prefix of c_id that exceeds p_id
    n = 0
    while n < ID_SIZE and n < len(p_id) and p_id[n] == c_id[n]:
        n += 1
    return Bound(c_ts, c_id[:n + 1])


# --- reconcile (non-initiator) ----------------------------------------------

def reconcile(items, query: bytes, frame_size_limit: int = 0) -> bytes:
    """Given our sorted `items` (list of (timestamp, id_bytes)) and the client's `query`
    message, produce our response message. Raises on malformed input."""
    r = _Reader(query)
    version = r.read(1)[0]
    if version != PROTOCOL_VERSION:
        raise ValueError(f"negentropy: unsupported version {version:#x}")

    out = bytearray()
    out.append(PROTOCOL_VERSION)
    out_state = [0]      # delta-timestamp state for encoding
    in_state = [0]       # delta-timestamp state for decoding
    lower = Bound(0, b"")
    budget = frame_size_limit - 200 if frame_size_limit else 0

    while r.remaining() > 0:
        upper = _decode_bound(r, in_state)
        mode = r.read_varint()
        lo_idx = _lower_index(items, lower)
        hi_idx = _lower_index(items, upper)

        if mode == 0:  # Skip
            _append_range(out, out_state, upper, 0, b"")
        elif mode == 1:  # Fingerprint
            their_fp = r.read(FINGERPRINT_SIZE)
            our_fp = _fingerprint([items[i][1] for i in range(lo_idx, hi_idx)])
            if our_fp == their_fp:
                _append_range(out, out_state, upper, 0, b"")
            else:
                _split_range(out, out_state, items, lo_idx, hi_idx, upper)
        elif mode == 2:  # IdList — client told us its ids in this range
            count = r.read_varint()
            for _ in range(count):
                r.read(ID_SIZE)  # we don't need the client's ids; we just send ours
            our_ids = [items[i][1] for i in range(lo_idx, hi_idx)]
            payload = bytearray(encode_varint(len(our_ids)))
            for eid in our_ids:
                payload += eid
            _append_range(out, out_state, upper, 2, bytes(payload))
        else:
            raise ValueError(f"negentropy: unknown mode {mode}")

        lower = upper
        if budget and len(out) > budget:
            # Frame-size limit: stop here; client continues next round.
            break

    return bytes(out)


def _append_range(out: bytearray, state: list, upper: Bound, mode: int, payload: bytes) -> None:
    _encode_bound(out, upper, state)
    out += encode_varint(mode)
    out += payload


def _split_range(out, state, items, lo_idx, hi_idx, upper: Bound) -> None:
    num = hi_idx - lo_idx
    if num < _BUCKETS * 2:
        ids = [items[i][1] for i in range(lo_idx, hi_idx)]
        payload = bytearray(encode_varint(len(ids)))
        for eid in ids:
            payload += eid
        _append_range(out, state, upper, 2, bytes(payload))
        return
    per = num // _BUCKETS
    extra = num % _BUCKETS
    curr = lo_idx
    for b in range(_BUCKETS):
        size = per + (1 if b < extra else 0)
        bucket = [items[i][1] for i in range(curr, curr + size)]
        curr += size
        if b == _BUCKETS - 1:
            next_bound = upper
        else:
            next_bound = _minimal_bound(items[curr - 1], items[curr])
        _append_range(out, state, next_bound, 1, _fingerprint(bucket))
