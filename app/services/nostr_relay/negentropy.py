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
The server builds its set once per NEG-OPEN and keeps it (an `ItemSet`, with the fingerprint prefix sums) for
the rounds of that session; `server.py` runs every round on a worker thread, never on the event loop.
"""

import hashlib
import time
from bisect import bisect_left

import numpy as np

PROTOCOL_VERSION = 0x61          # version 1
ID_SIZE = 32
FINGERPRINT_SIZE = 16
MAX_U64 = (1 << 64) - 1
_BUCKETS = 16
_MASK256 = (1 << 256) - 1


# --- varint -----------------------------------------------------------------

_SMALL_VARINTS = [bytes((i,)) for i in range(128)]


def encode_varint(n: int) -> bytes:
    if 0 <= n < 128:        # every mode byte, most bound deltas and prefix lengths: one table lookup
        return _SMALL_VARINTS[n]
    if n < 0:
        raise ValueError("negentropy: negative varint")
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


# --- the item set ------------------------------------------------------------

class IdItems(list):
    """A plain list of (timestamp, id) that ALSO carries every id concatenated in list order (`raw`), for a store
    that already holds them that way (PosterChanDB reads them as one array). The prefix sums are built from `raw`
    directly instead of re-joining 50k-200k scattered bytes objects -- that join was most of a session's cost
    once fingerprints became O(1). Everything else sees an ordinary list."""

    __slots__ = ("raw",)

    def __init__(self, items=(), raw: bytes = b""):
        super().__init__(items)
        self.raw = raw


class ItemSet:
    """Our sorted (timestamp, id) items, with what a range fingerprint needs precomputed ONCE.

    A fingerprint is sha256(sum of the ids as little-endian 256-bit ints, mod 2^256 || varint(count)). Summed per
    range in a Python loop that was the relay's whole negentropy cost -- every round re-read every id of every
    range the client asked about (a 50k set: ~45-100 ms of event-loop time per session, 200k: up to ~900 ms).
    Prefix sums make a range's sum one subtraction: built lazily, on the first fingerprint a session needs, and
    kept for the rest of the session (the relay stores the ItemSet as the session).

    Range lookup is `bisect_left` on the tuples themselves: an item is below a bound (ts, prefix) exactly when
    (ts_i, id_i) < (ts, prefix) -- at an equal timestamp, a 32-byte id whose first len(prefix) bytes equal the
    prefix compares GREATER than the prefix, which is `_item_lt_bound`'s "equal-or-longer prefix is not below"."""

    __slots__ = ("items", "_prefix", "pause")

    def __init__(self, items, cooperative: bool = False):
        self.items = items if isinstance(items, list) else list(items)
        self._prefix = None
        # cooperative: this set is reconciled on a WORKER thread next to an event loop (server.py). A thread running
        # pure Python only gives the GIL up when the interpreter forces a switch (every 5 ms, and under load the
        # loop then waits several of those), so the long loops here hand it over themselves, about every 64 ranges
        # / 8192 ids. Answers are unaffected; only who runs in between.
        self.pause = _yield_gil if cooperative else None

    def __len__(self) -> int:
        return len(self.items)

    def lower(self, b: Bound) -> int:
        """First index i with items[i] >= b -- `_lower_index`, in C."""
        return bisect_left(self.items, (b.timestamp, b.id))

    def ids(self, lo: int, hi: int) -> bytes:
        return _join_ids(self.items, lo, hi, self.pause)

    def fingerprint(self, lo: int, hi: int) -> bytes:
        if hi <= lo:      # a client's bounds out of order: an empty range, as the loop read it
            return _fingerprint_of_sum(0, 0)
        p = self._prefix
        if p is None:
            p = self._prefix = _prefix_limbs(self.items, self.pause)
        d = (p[hi] - p[lo]).tolist()          # eight per-limb sums, each exact (< 2^64)
        total = (d[0] + (d[1] << 32) + (d[2] << 64) + (d[3] << 96) + (d[4] << 128)
                 + (d[5] << 160) + (d[6] << 192) + (d[7] << 224))
        return _fingerprint_of_sum(total, hi - lo)


def _yield_gil() -> None:
    time.sleep(0)       # releases the GIL; a thread waiting for it (the event loop) takes it now


def _join_ids(items, lo: int, hi: int, pause=None, chunk: int = 8192) -> bytes:
    if pause is None or hi - lo <= chunk:
        return b"".join([eid for _, eid in items[lo:hi]])
    parts = []
    for a in range(lo, hi, chunk):
        parts.append(b"".join([eid for _, eid in items[a:min(hi, a + chunk)]]))
        pause()
    return b"".join(parts)


def _prefix_limbs(items, pause=None):
    """Prefix sums of the ids, as eight little-endian 32-bit limbs summed in uint64: row i is the limb-wise sum of
    ids[0:i]. A limb sum stays exact while the set has fewer than 2^32 items, and limb k of a range sum carries
    weight 2^(32k), so recombining the eight differences gives the exact integer sum of the range's ids; reducing
    it mod 2^256 afterwards gives what the old loop's running mask gave."""
    n = len(items)
    pre = np.zeros((n + 1, 8), dtype=np.uint64)
    if n:
        raw = getattr(items, "raw", None)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != ID_SIZE * n:
            raw = _join_ids(items, 0, n, pause)
        limbs = np.frombuffer(raw, dtype="<u4").reshape(n, 8)
        np.cumsum(limbs, axis=0, dtype=np.uint64, out=pre[1:])
    return pre


def _fingerprint_of_sum(total: int, count: int) -> bytes:
    return hashlib.sha256((total & _MASK256).to_bytes(32, "little")
                          + encode_varint(count)).digest()[:FINGERPRINT_SIZE]


def as_item_set(items) -> ItemSet:
    return items if isinstance(items, ItemSet) else ItemSet(items)


# --- reconcile (non-initiator) ----------------------------------------------

def reconcile(items, query: bytes, frame_size_limit: int = 0) -> bytes:
    """Given our sorted `items` (an ItemSet, or a list of (timestamp, id_bytes)) and the client's `query`
    message, produce our response message. Raises on malformed input.

    Pass the SAME ItemSet for every round of a session: a plain list is wrapped per call, which recomputes the
    prefix sums each round (correct, just slower)."""
    S = as_item_set(items)
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

    pause, ranges = S.pause, 0
    while r.remaining() > 0:
        if pause is not None:
            ranges += 1
            if not ranges & 63:
                pause()
        upper = _decode_bound(r, in_state)
        mode = r.read_varint()
        lo_idx = S.lower(lower)
        hi_idx = S.lower(upper)

        if mode == 0:  # Skip
            _append_range(out, out_state, upper, 0, b"")
        elif mode == 1:  # Fingerprint
            their_fp = r.read(FINGERPRINT_SIZE)
            if S.fingerprint(lo_idx, hi_idx) == their_fp:
                _append_range(out, out_state, upper, 0, b"")
            else:
                _split_range(out, out_state, S, lo_idx, hi_idx, upper)
        elif mode == 2:  # IdList — client told us its ids in this range
            count = r.read_varint()
            r.read(ID_SIZE * count)  # we don't need the client's ids; we just send ours (raises if short)
            _append_range(out, out_state, upper, 2,
                          encode_varint(max(0, hi_idx - lo_idx)) + S.ids(lo_idx, hi_idx))
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
    S = as_item_set(items)
    num = hi_idx - lo_idx
    if num < _BUCKETS * 2:
        _append_range(out, state, upper, 2, encode_varint(max(0, num)) + S.ids(lo_idx, hi_idx))
        return
    per = num // _BUCKETS
    extra = num % _BUCKETS
    curr = lo_idx
    its = S.items
    for b in range(_BUCKETS):
        size = per + (1 if b < extra else 0)
        fp = S.fingerprint(curr, curr + size)
        curr += size
        if b == _BUCKETS - 1:
            next_bound = upper
        else:
            next_bound = _minimal_bound(its[curr - 1], its[curr])
        _append_range(out, state, next_bound, 1, fp)


# --- initiator (client side) ------------------------------------------------

class Initiator:
    """The INITIATOR role, for a relay that syncs FROM another relay (PosterChanOS's local relay pulling
    from its instance, `osmode.py`). The same algorithm as static/js/client/negentropy.js, decision for
    decision: open with the whole set split into fingerprinted buckets, answer every mismatched range by
    splitting it again, and resolve an IdList range locally -- `need` collects what the other side holds and
    we do not, `have` the reverse. Nothing here talks to a socket; the caller sends `initiate()` as NEG-OPEN
    and each `reconcile()` answer as NEG-MSG until it returns None."""

    def __init__(self, items):
        # items: iterable of (timestamp, 32-byte id) -- sorted here, as the relay side sorts its own.
        self.items = sorted((int(ts), bytes(eid)) for ts, eid in items)
        self._set = ItemSet(self.items)
        self.need: set = set()
        self.have: set = set()

    def initiate(self) -> bytes:
        out = bytearray([PROTOCOL_VERSION])
        _split_range(out, [0], self._set, 0, len(self.items), Bound.infinity())
        return bytes(out)

    def reconcile(self, msg: bytes):
        """Process one message from the other side; the next message to send, or None when done."""
        r = _Reader(msg)
        version = r.read(1)[0]
        if version != PROTOCOL_VERSION:
            raise ValueError(f"negentropy: unsupported version {version:#x}")
        out = bytearray([PROTOCOL_VERSION])
        out_state, in_state = [0], [0]
        lower = Bound(0, b"")
        work = False
        while r.remaining() > 0:
            upper = _decode_bound(r, in_state)
            mode = r.read_varint()
            lo_idx = self._set.lower(lower)
            hi_idx = self._set.lower(upper)
            if mode == 0:
                _append_range(out, out_state, upper, 0, b"")
            elif mode == 1:
                theirs = r.read(FINGERPRINT_SIZE)
                if self._set.fingerprint(lo_idx, hi_idx) == theirs:
                    _append_range(out, out_state, upper, 0, b"")
                else:
                    work = True
                    _split_range(out, out_state, self._set, lo_idx, hi_idx, upper)
            elif mode == 2:
                count = r.read_varint()
                theirs = {r.read(ID_SIZE) for _ in range(count)}
                ours = {self.items[i][1] for i in range(lo_idx, hi_idx)}
                self.need.update(theirs - ours)
                self.have.update(ours - theirs)
                _append_range(out, out_state, upper, 0, b"")
            else:
                raise ValueError(f"negentropy: unknown mode {mode}")
            lower = upper
        return bytes(out) if work else None
