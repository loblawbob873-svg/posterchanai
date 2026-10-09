"""PosterChanDB store: RAM-first, append-only log on disk, numpy indexes in memory.

Nothing here allocates a Python object PER EVENT that lives beyond the call: records live in one
`bytearray` arena, per-event columns in `array.array` (compact, O(1) append) viewed as numpy for queries,
and every index is a sorted numpy key array + one flat postings array ("base"), with a small dict of
recent additions ("delta") merged in bulk at each flush. See docs/POSTERCHANDB.md.

Durability:
  * every write is appended to an in-memory pending buffer and indexed at once (reads see it);
  * `flush()` writes the buffer to the current log file and fsyncs it — on the timer
    (`flush_interval`, default 60 s), on `close()` (a clean stop), and immediately for a DIRECT write
    when `direct_durable` is on;
  * log records are framed `<u32 len><u32 crc32><payload>`; a torn or corrupt tail found on open is
    truncated away and reported, never read as data.
"""
from __future__ import annotations

import array
import bisect
import hashlib
import os
import re
import struct
import threading
import time
import zlib

import numpy as np

from .codec import Codec

MAGIC = b"PCDB1\n"
OP_PUT, OP_DEAD, OP_DERIVED = 1, 2, 3
_FRAME = struct.Struct("<II")
_WORD = re.compile(r"\w+", re.U)
_CIPHER = re.compile(r"^[A-Za-z0-9+/=]{40,}(\?iv=[A-Za-z0-9+/=]+)?$")


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "little")


def is_replaceable(kind: int) -> bool:
    return kind in (0, 3) or 10000 <= kind < 20000


def is_addressable(kind: int) -> bool:
    return 30000 <= kind < 40000


def is_ephemeral(kind: int) -> bool:
    return 20000 <= kind < 30000


def _dtag(ev: dict) -> str:
    for t in ev.get("tags") or []:
        if len(t) >= 2 and t[0] == "d":
            return t[1]
        if len(t) == 1 and t[0] == "d":
            return ""
    return ""


def _expiration(ev: dict) -> int:
    for t in ev.get("tags") or []:
        if len(t) >= 2 and t[0] == "expiration":
            try:
                return max(0, int(t[1]))
            except ValueError:
                return 0
    return 0


def search_words(text: str):
    """The words a search matches on: lowercased \\w+ runs (Postgres's `simple` config: no stemming,
    no stop words). Ciphertext is not text and is not indexed."""
    t = (text or "").strip()
    if not t or _CIPHER.match(t):
        return set()
    return {w for w in _WORD.findall(t.lower()) if len(w) <= 64}


def _merge_runs(ka, sa, pa, kb, sb, pb):
    """Merge two CSR runs (keys sorted unique, starts, postings). Every posting in run B is NEWER than
    every posting in run A (sequence numbers only grow), so per key the result is simply A's list then
    B's: a STABLE sort on keys alone, never a re-sort of postings."""
    ka_rep = np.repeat(ka, np.diff(sa))
    kb_rep = np.repeat(kb, np.diff(sb))
    allk = np.concatenate([ka_rep, kb_rep])
    allp = np.concatenate([pa, pb])
    order = np.argsort(allk, kind="stable")
    allk, allp = allk[order], allp[order]
    uk, first = np.unique(allk, return_index=True)
    return uk, np.append(first, len(allk)).astype(np.int64), allp


class _Postings:
    """hash key -> ascending sequence numbers, as LEVELED sorted runs (an LSM tree in miniature).

    New postings collect in a small dict (`delta`). A flush turns the delta into one small CSR run —
    sorting only what arrived — and merges runs only while the newest is at least a quarter the size of
    the one before it. So the number of runs stays ~log(N) and each posting is merged ~log(N) times in its
    life: CPU per flush follows what ARRIVED, not the size of the store (a full rebuild every minute
    would re-sort ~20M postings on this relay to add a few hundred)."""

    RATIO = 4

    def __init__(self):
        self.runs: list = []          # oldest first: (keys u64 sorted unique, starts i64, post u32)
        self.delta: dict[int, list] = {}

    def add(self, key: int, seq: int) -> None:
        self.delta.setdefault(key, []).append(seq)

    def get(self, key: int) -> np.ndarray:
        parts = []
        k = np.uint64(key)
        for keys, starts, post in self.runs:
            i = int(np.searchsorted(keys, k))
            if i < len(keys) and keys[i] == k:
                parts.append(post[starts[i]:starts[i + 1]])
        d = self.delta.get(key)
        if d:
            parts.append(np.asarray(d, dtype=np.uint32))
        if not parts:
            return np.zeros(0, dtype=np.uint32)
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def merge(self) -> None:
        if self.delta:
            items = sorted(self.delta.items())
            keys = np.fromiter((k for k, _ in items), dtype=np.uint64, count=len(items))
            lens = np.fromiter((len(v) for _, v in items), dtype=np.int64, count=len(items))
            starts = np.zeros(len(items) + 1, dtype=np.int64)
            np.cumsum(lens, out=starts[1:])
            post = np.fromiter((x for _, v in items for x in v), dtype=np.uint32, count=int(starts[-1]))
            self.runs.append((keys, starts, post))
            self.delta = {}
        while len(self.runs) > 1 and len(self.runs[-1][2]) * self.RATIO >= len(self.runs[-2][2]):
            b = self.runs.pop()
            a = self.runs.pop()
            self.runs.append(_merge_runs(*a, *b))

    def nbytes(self) -> int:
        return sum(k.nbytes + s.nbytes + p.nbytes for k, s, p in self.runs)


class _Prefix:
    """Sorted (value, seq) runs for prefix matching on ONE tag letter (`#d~`, the app's folder reads).
    Leveled like the postings; only events that carry the tag pay for it."""

    def __init__(self):
        self.runs: list = []
        self.delta: list = []

    def add(self, value: str, seq: int) -> None:
        self.delta.append((value, seq))

    def merge(self) -> None:
        if self.delta:
            self.runs.append(sorted(self.delta))
            self.delta = []
        while len(self.runs) > 1 and len(self.runs[-1]) * 4 >= len(self.runs[-2]):
            b = self.runs.pop(); a = self.runs.pop()
            self.runs.append(sorted(a + b))

    def prefix(self, pre: str) -> list:
        out = [q for v, q in self.delta if v.startswith(pre)]
        for run in self.runs:
            i = bisect.bisect_left(run, (pre, -1))
            while i < len(run) and run[i][0].startswith(pre):
                out.append(run[i][1]); i += 1
        return out


class Store:
    def __init__(self, path: str, *, zdict: bytes = b"", flush_interval: float = 60.0,
                 direct_durable: bool = True, log=None):
        self.path = path
        self.codec = Codec(zdict)
        self.flush_interval = float(flush_interval)
        self.direct_durable = bool(direct_durable)
        self.log = log or (lambda *a: None)
        self._lock = threading.RLock()
        # per-event columns (index = sequence number)
        self.arena = bytearray()
        self.off = array.array("Q")
        self.length = array.array("I")
        self.created = array.array("Q")
        self.kind = array.array("I")
        self.expires = array.array("Q")
        self.dead = bytearray()
        # id -> seq: sorted base + delta
        self._id_runs: list = []      # leveled sorted runs of (id-prefix u64, seq u32)
        self._id_delta: dict[int, int] = {}
        self.idx = _Postings()       # author+kind, kind, single-letter tags
        self.words = _Postings()     # search
        self.dprefix = _Prefix()     # `#d~` prefix reads
        self.current: dict[int, int] = {}   # replaceable/addressable address hash -> seq of current version
        self._pending = bytearray()
        self._pending_n = 0
        self._last_flush = time.monotonic()
        self._file = None
        os.makedirs(path, exist_ok=True)
        self._open()

    # ---------------------------------------------------------------- disk
    def _log_path(self) -> str:
        return os.path.join(self.path, "events.log")

    def _open(self) -> None:
        p = self._log_path()
        if not os.path.exists(p):
            with open(p, "wb") as f:
                f.write(MAGIC)
                f.flush()
                os.fsync(f.fileno())
        good = self._replay(p)
        size = os.path.getsize(p)
        if good < size:
            self.log("[posterchandb] truncating %d torn/corrupt bytes at the end of the log" % (size - good))
            with open(p, "r+b") as f:
                f.truncate(good)
                f.flush()
                os.fsync(f.fileno())
        self._file = open(p, "ab")
        self.idx.merge()
        self.words.merge()
        self.dprefix.merge()
        self._merge_ids()

    def _replay(self, p: str) -> int:
        with open(p, "rb") as f:
            data = f.read()
        if not data.startswith(MAGIC):
            raise ValueError("not a PosterChanDB log: " + p)
        i = len(MAGIC)
        while i + _FRAME.size <= len(data):
            n, crc = _FRAME.unpack_from(data, i)
            j = i + _FRAME.size
            if j + n > len(data):
                break
            payload = data[j:j + n]
            if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                break
            op = payload[0]
            if op == OP_PUT:
                ev = self.codec.decode(payload[1:])
                self._apply_put(ev, payload[1:], replay=True)
            elif op == OP_DEAD:
                s = self._seq_of(bytes(payload[1:33]).hex())
                if s is not None:
                    self.dead[s] = 1
            elif op == OP_DERIVED:
                s = self._seq_of(bytes(payload[1:33]).hex())
                if s is not None:
                    tag, _, value = bytes(payload[33:]).decode("utf-8").partition("\x00")
                    self.idx.add(_h("t:%s:%s" % (tag, value)), s)
            i = j + n
        return i

    def _frame(self, payload: bytes) -> None:
        self._pending += _FRAME.pack(len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
        self._pending += payload
        self._pending_n += 1

    def flush(self) -> int:
        """Write everything buffered to disk and fsync. Returns how many records were written."""
        with self._lock:
            n = self._pending_n
            if self._pending:
                self._file.write(self._pending)
                self._file.flush()
                os.fsync(self._file.fileno())
                self._pending = bytearray()
                self._pending_n = 0
            self.idx.merge()
            self.words.merge()
            self.dprefix.merge()
            self._merge_ids()
            self._last_flush = time.monotonic()
            return n

    def maybe_flush(self) -> int:
        if self._pending_n and time.monotonic() - self._last_flush >= self.flush_interval:
            return self.flush()
        return 0

    def close(self) -> None:
        with self._lock:
            self.flush()
            if self._file:
                self._file.close()
                self._file = None

    def stats(self) -> dict:
        return {"events": len(self.off), "dead": int(sum(self.dead)), "arena_bytes": len(self.arena),
                "index_bytes": self.idx.nbytes() + self.words.nbytes() + sum(k.nbytes + v.nbytes for k, v in self._id_runs),
                "runs": len(self.idx.runs),
                "unflushed": self._pending_n, "since_flush_s": round(time.monotonic() - self._last_flush, 1)}

    # ---------------------------------------------------------------- ids
    def _merge_ids(self) -> None:
        """Same leveled scheme as the postings: sort only the new ids into a run; merge runs geometrically."""
        if self._id_delta:
            k = np.fromiter(self._id_delta.keys(), dtype=np.uint64, count=len(self._id_delta))
            v = np.fromiter(self._id_delta.values(), dtype=np.uint32, count=len(self._id_delta))
            o = np.argsort(k)
            self._id_runs.append((k[o], v[o]))
            self._id_delta = {}
        while len(self._id_runs) > 1 and len(self._id_runs[-1][0]) * 4 >= len(self._id_runs[-2][0]):
            kb, vb = self._id_runs.pop()
            ka, va = self._id_runs.pop()
            k = np.concatenate([ka, kb]); v = np.concatenate([va, vb])
            o = np.argsort(k, kind="stable")
            self._id_runs.append((k[o], v[o]))

    def _seq_of(self, eid: str):
        key = int(eid[:16], 16)
        s = self._id_delta.get(key)
        cands = [s] if s is not None else []
        ku = np.uint64(key)
        for keys, seqs in self._id_runs:
            lo = int(np.searchsorted(keys, ku, "left"))
            if lo < len(keys) and keys[lo] == ku:
                hi = int(np.searchsorted(keys, ku, "right"))
                cands += [int(x) for x in seqs[lo:hi]]
        for c in cands:          # an 8-byte prefix can collide: confirm against the record's own id
            if self.arena[self.off[c]:self.off[c] + 32].hex() == eid:
                return c
        return None

    # ---------------------------------------------------------------- write
    def _address(self, ev: dict):
        k = ev["kind"]
        if is_replaceable(k):
            return _h("r:%s:%d" % (ev["pubkey"], k))
        if is_addressable(k):
            return _h("a:%s:%d:%s" % (ev["pubkey"], k, _dtag(ev)))
        return None

    def _newer(self, a: dict, b_seq: int) -> bool:
        """NIP-01: the newer created_at wins; on a tie the LOWER id wins."""
        bc = self.created[b_seq]
        if a["created_at"] != bc:
            return a["created_at"] > bc
        return a["id"] < self.arena[self.off[b_seq]:self.off[b_seq] + 32].hex()

    def put(self, ev: dict, *, direct: bool = False) -> str:
        """Store a (signature-verified) event. Returns 'stored', 'duplicate', 'superseded' (an older
        version of a replaceable the store already has newer), or 'ephemeral' (not stored)."""
        if is_ephemeral(ev["kind"]):
            return "ephemeral"
        with self._lock:
            if self._seq_of(ev["id"]) is not None:
                return "duplicate"
            addr = self._address(ev)
            if addr is not None:
                cur = self.current.get(addr)
                if cur is not None and not self.dead[cur] and not self._newer(ev, cur):
                    return "superseded"
            rec = self.codec.encode(ev)
            self._frame(bytes([OP_PUT]) + rec)
            self._apply_put(ev, rec, replay=False)
            if direct and self.direct_durable:
                self.flush()
            return "stored"

    def add_derived_tag(self, event_id: str, tag: str, value: str) -> bool:
        """An index-only tag the relay derives (today: `_quote_author`). Logged, so it survives a restart."""
        with self._lock:
            s = self._seq_of(event_id)
            if s is None:
                return False
            self._frame(bytes([OP_DERIVED]) + bytes.fromhex(event_id) + ("%s\x00%s" % (tag, value)).encode("utf-8"))
            self.idx.add(_h("t:%s:%s" % (tag, value)), s)
            return True

    def _apply_put(self, ev: dict, rec, *, replay: bool) -> None:
        seq = len(self.off)
        self.off.append(len(self.arena))
        self.length.append(len(rec))
        self.arena += rec
        self.created.append(ev["created_at"])
        self.kind.append(ev["kind"])
        self.expires.append(_expiration(ev))
        self.dead.append(0)
        self._id_delta[int(ev["id"][:16], 16)] = seq
        k = ev["kind"]
        self.idx.add(_h("ak:%s:%d" % (ev["pubkey"], k)), seq)
        self.idx.add(_h("au:%s" % ev["pubkey"]), seq)
        self.idx.add(_h("k:%d" % k), seq)
        for t in ev.get("tags") or []:
            if len(t) >= 2 and len(t[0]) == 1:
                self.idx.add(_h("t:%s:%s" % (t[0], t[1])), seq)
                if t[0] == "d":
                    self.dprefix.add(t[1], seq)
        for w in search_words(ev.get("content", "")):
            self.words.add(_h(w), seq)
        addr = self._address(ev)
        if addr is not None:
            cur = self.current.get(addr)
            if cur is None or self.dead[cur] or self._newer(ev, cur):
                if cur is not None:
                    self.dead[cur] = 1
                self.current[addr] = seq
            else:
                self.dead[seq] = 1
        if k == 5:
            self._apply_deletion(ev, replay)

    def _apply_deletion(self, ev: dict, replay: bool) -> None:
        """NIP-09: an author can delete their OWN events, by id (`e`) or address (`a`, up to the deletion's time)."""
        for t in ev.get("tags") or []:
            if len(t) < 2:
                continue
            if t[0] == "e":
                s = self._seq_of(t[1])
                if s is not None and self.arena[self.off[s] + 32:self.off[s] + 64].hex() == ev["pubkey"] \
                        and self.kind[s] != 5:
                    self.dead[s] = 1
            elif t[0] == "a":
                parts = t[1].split(":", 2)
                if len(parts) == 3 and parts[1] == ev["pubkey"]:
                    try:
                        k = int(parts[0])
                    except ValueError:
                        continue
                    addr = _h("a:%s:%d:%s" % (parts[1], k, parts[2])) if is_addressable(k) \
                        else _h("r:%s:%d" % (parts[1], k))
                    s = self.current.get(addr)
                    if s is not None and self.created[s] <= ev["created_at"]:
                        self.dead[s] = 1

    # ---------------------------------------------------------------- read
    def get(self, seq: int) -> dict:
        o = self.off[seq]
        return self.codec.decode(memoryview(self.arena)[o:o + self.length[seq]])

    def query(self, flt: dict, now: int | None = None) -> list:
        """One NIP-01 filter (+ NIP-50 `search`), newest first, `limit` applied. Dead and expired
        events are never returned."""
        now = int(time.time()) if now is None else now
        with self._lock:
            sets = []
            if flt.get("ids"):
                s = [self._seq_of(i) for i in flt["ids"] if isinstance(i, str) and len(i) == 64]
                sets.append(np.array(sorted(x for x in s if x is not None), dtype=np.uint32))
            authors, kinds = flt.get("authors"), flt.get("kinds")
            if authors and kinds:
                sets.append(np.unique(np.concatenate([self.idx.get(_h("ak:%s:%d" % (a, k)))
                                                      for a in authors for k in kinds] or [np.zeros(0, np.uint32)])))
            elif authors:
                sets.append(np.unique(np.concatenate([self.idx.get(_h("au:%s" % a)) for a in authors]
                                                     or [np.zeros(0, np.uint32)])))
            elif kinds:
                sets.append(np.unique(np.concatenate([self.idx.get(_h("k:%d" % k)) for k in kinds]
                                                     or [np.zeros(0, np.uint32)])))
            for key, vals in flt.items():
                if not (isinstance(key, str) and key.startswith("#") and vals):
                    continue
                if len(key) == 2:
                    tags = [key[1]] + (["_quote_author"] if key == "#p" and flt.get("_include_quotes") is True else [])
                    sets.append(np.unique(np.concatenate([self.idx.get(_h("t:%s:%s" % (tg, v)))
                                                          for tg in tags for v in vals])))
                elif len(key) == 3 and key.endswith("~"):
                    if key[1] != "d":
                        return []          # prefix matching is indexed for `d` only — the app's one use
                    sets.append(np.unique(np.array([q for v in vals for q in self.dprefix.prefix(str(v))],
                                                   dtype=np.uint32)))
            if flt.get("search"):
                words = search_words(flt["search"])
                if not words:
                    return []
                for w in words:
                    sets.append(np.unique(self.words.get(_h(w))))
            n = len(self.off)
            if sets:
                cand = sets[0]
                for s in sets[1:]:
                    cand = np.intersect1d(cand, s, assume_unique=True)
            else:
                cand = np.arange(n, dtype=np.uint32)
            if not len(cand):
                return []
            created = np.frombuffer(self.created, dtype=np.uint64) if n else np.zeros(0, np.uint64)
            expires = np.frombuffer(self.expires, dtype=np.uint64) if n else np.zeros(0, np.uint64)
            dead = np.frombuffer(self.dead, dtype=np.uint8) if n else np.zeros(0, np.uint8)
            keep = dead[cand] == 0
            exp = expires[cand]
            keep &= (exp == 0) | (exp > now)
            if "since" in flt:
                keep &= created[cand] >= int(flt["since"])
            if "until" in flt:
                keep &= created[cand] <= int(flt["until"])
            cur = flt.get("_cursor")
            if isinstance(cur, list) and len(cur) == 2:
                c0, c1 = int(cur[0]), str(cur[1])
                cc = created[cand]
                keep &= cc <= c0
                same = np.nonzero(keep & (cc == c0))[0]
                for j in same:              # equal timestamps: page on the id (`e.id < cursor id`)
                    o = self.off[int(cand[j])]
                    if not self.arena[o:o + 32].hex() < c1:
                        keep[j] = False
            cand = cand[keep]
            # The relay's own rule (store.py _query_one): `limit or 500`, clamped to 1..5000.
            limit = max(1, min(int(flt.get("limit") or 500), 5000))
            if not len(cand):
                return []
            c = created[cand]
            if len(cand) > limit:
                # keep every event at the cut-off timestamp: the id decides among them below
                cut = np.partition(c, len(c) - limit)[len(c) - limit]
                sel = c >= cut
                cand, c = cand[sel], c[sel]
            evs = [self.get(int(s)) for s in cand]
            # ORDER BY created_at DESC, id DESC — exactly the relay's order
            evs.sort(key=lambda e: (e["created_at"], e["id"]), reverse=True)
            return evs[:limit]
