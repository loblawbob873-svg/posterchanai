"""PosterChanDB store: RAM-first, append-only log on disk, numpy indexes in memory.

Nothing here allocates a Python object PER EVENT that lives beyond the call: records live in one
`bytearray` arena, per-event columns in `array.array` (compact, O(1) append) viewed as numpy for queries,
and every index is a sorted numpy key array + one flat postings array ("base"), with a small dict of
recent additions ("delta") merged in bulk at each flush. See docs/POSTERCHANDB.md.

Layout on disk: `seg-000001.log`, `seg-000002.log`, … (~256 MB each). A segment's RAM copy (its
"arena") is the file's bytes verbatim, so a record's offset is the same in RAM and on disk.

RAM is a CACHE, not a requirement. Indexes, ids and the per-event columns always stay in RAM (~100
bytes an event); a CLOSED segment's record bytes may be dropped from RAM (`evict`) and are then read
from its file on demand (`os.pread`, through the kernel's page cache — closed segments never change).
cache.py decides which segments stay resident from the machine's free memory and pressure. The ACTIVE
segment is always resident: its unflushed tail exists nowhere else.

Durability:
  * every write is appended to an in-memory pending buffer and indexed at once (reads see it);
  * `flush()` writes the buffer to the current log file and fsyncs it — on the timer
    (`flush_interval`, default 300 s), on `close()` (a clean stop), and immediately for a DIRECT write
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

from . import tsparser
from .codec import Codec

MAGIC = b"PCDB1\n"
OP_PUT, OP_DEAD, OP_DERIVED = 1, 2, 3   # OP_PUT payload: op, origin byte, codec record
_FRAME = struct.Struct("<II")


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
            return t[1] if isinstance(t[1], str) else str(t[1])   # the relay matches str(t[1]) via event_tags
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
    """What a search matches on — EXACTLY the lexemes of the relay's `to_tsvector('simple', text)` (tsparser.py
    is Postgres's own parser; tests/test_posterchandb_tsparser.py holds it to a real Postgres). A search is the
    AND of `plainto_tsquery('simple', search)`'s lexemes: the same function applied to the search text."""
    return tsparser.search_set(text or "")


def _merge_runs(ka, sa, pa, kb, sb, pb, dead=None):
    """Merge two CSR runs (keys sorted unique, starts, postings). Every posting in run B is NEWER than
    every posting in run A (sequence numbers only grow), so per key the result is simply A's list then
    B's: a STABLE sort on keys alone, never a re-sort of postings."""
    ka_rep = np.repeat(ka, np.diff(sa))
    kb_rep = np.repeat(kb, np.diff(sb))
    allk = np.concatenate([ka_rep, kb_rep])
    allp = np.concatenate([pa, pb])
    if dead is not None and len(allp):
        # SELF-CLEANING: postings of dead events are dropped by the merge that happens anyway
        live = dead[allp] == 0
        allk, allp = allk[live], allp[live]
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

    def merge(self, dead=None) -> None:
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
            self.runs.append(_merge_runs(*a, *b, dead=dead))

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

    def merge(self, dead=None) -> None:
        if self.delta:
            self.runs.append(sorted(self.delta))
            self.delta = []
        while len(self.runs) > 1 and len(self.runs[-1]) * 4 >= len(self.runs[-2]):
            b = self.runs.pop(); a = self.runs.pop()
            self.runs.append(sorted(x for x in a + b if dead is None or not dead[x[1]]))

    def prefix(self, pre: str) -> list:
        out = [q for v, q in self.delta if v.startswith(pre)]
        for run in self.runs:
            i = bisect.bisect_left(run, (pre, -1))
            while i < len(run) and run[i][0].startswith(pre):
                out.append(run[i][1]); i += 1
        return out


ORIGINS = {"direct": 0, "wot": 1, "bridge": 2, "ancestor": 3}
ORIGIN_NAMES = {v: k for k, v in ORIGINS.items()}
SEGMENT_BYTES = 256 * 1024 * 1024
DROPPED = 0xFFFFFFFF            # `seg` of a dead record whose segment was compacted away: no bytes anywhere
COMPACT_CHUNK = 4 * 1024 * 1024 # compaction writes in large sequential pieces (full RAID5 stripes, no RMW)


class Store:
    """See the module docstring. Every public method is thread-safe (one re-entrant lock)."""

    def __init__(self, path: str, *, zdict: bytes = b"", flush_interval: float = 300.0,
                 direct_durable: bool = True, segment_bytes: int = SEGMENT_BYTES,
                 compact_dead_pct: float = 40.0, admit=None, log=None):
        self.path = path
        self.codec = Codec(zdict)
        self.flush_interval = float(flush_interval)
        self.direct_durable = bool(direct_durable)
        self.segment_bytes = int(segment_bytes)
        self.compact_dead_pct = float(compact_dead_pct)
        self.log = log or (lambda *a: None)
        # admit(nbytes) -> bool: may a closed segment stay resident after it is read at startup? (cache.py)
        self.admit = admit or (lambda n: True)
        self._lock = threading.RLock()
        # per-event columns (index = sequence number)
        self.arenas: dict = {}                     # segment id -> its file's bytes (resident) or None (cold)
        self.seg_size: dict[int, int] = {}         # bytes of each segment (file + unflushed tail)
        self.seg_hits: dict[int, float] = {}       # recent reads per segment (decayed by the cache governor)
        self.seg_last: dict[int, float] = {}       # last read, monotonic
        self._fds: dict[int, int] = {}             # read-only fds for cold reads
        self.ids = bytearray()                     # 32 raw bytes per event: id checks never touch a record
        self.seg = array.array("I")
        self.off = array.array("Q")
        self.length = array.array("I")
        self.created = array.array("Q")
        self.kind = array.array("I")
        self.expires = array.array("Q")
        self.origin = bytearray()
        self.author = array.array("I")             # interned public key
        self.dead = bytearray()
        self._authors: list[str] = []
        self._author_ix: dict[str, int] = {}
        # per-segment bookkeeping: bytes of records, bytes of dead records
        self.seg_bytes: dict[int, int] = {}
        self.seg_dead: dict[int, int] = {}
        # the OP_DEAD/OP_DERIVED payloads each segment holds, kept in RAM so compaction never has to
        # read a segment back from disk to find what must be carried forward
        self.seg_markers: dict[int, list] = {}
        self._compacting = False
        self.derived: dict[int, set] = {}   # seq -> {(tag, value)}: re-emitted beside a record whenever it moves
        # id -> seq: leveled sorted runs + delta
        self._id_runs: list = []
        self._id_delta: dict[int, int] = {}
        self.idx = _Postings()       # author+kind, author, kind, single-letter tags
        self.words = _Postings()     # search
        self.dprefix = _Prefix()     # `#d~` prefix reads
        self.current: dict[int, int] = {}   # replaceable/addressable address hash -> seq of current version
        self._flushed = 0        # bytes of the active segment's arena that are on disk
        self._pending_n = 0
        self._last_flush = time.monotonic()
        self._active = 0
        self._file = None
        os.makedirs(path, exist_ok=True)
        self._open()

    # ---------------------------------------------------------------- disk
    def _seg_path(self, sid: int) -> str:
        return os.path.join(self.path, "seg-%06d.log" % sid)

    def _segments(self) -> list:
        out = []
        for name in os.listdir(self.path):
            m = re.match(r"^seg-(\d{6})\.log$", name)
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def _new_segment(self, sid: int) -> None:
        p = self._seg_path(sid)
        with open(p, "wb") as f:
            f.write(MAGIC)
            f.flush()
            os.fsync(f.fileno())
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _open(self) -> None:
        sids = self._segments()
        if not sids:
            self._new_segment(1)
            sids = [1]
        for sid in sids:
            p = self._seg_path(sid)
            with open(p, "rb") as f:
                data = f.read()
            self.seg_bytes.setdefault(sid, 0)
            self.seg_dead.setdefault(sid, 0)
            self.seg_markers.setdefault(sid, [])
            good = self._replay(sid, data)
            if good < len(data):
                # Only the LAST segment can legitimately have a torn tail; an earlier one means a
                # crash mid-compaction left a partial copy — its records are also in a later segment.
                self.log("[posterchandb] cutting %d torn/corrupt bytes off %s" % (len(data) - good, os.path.basename(p)))
                with open(p, "r+b") as f:
                    f.truncate(good)
                    f.flush()
                    os.fsync(f.fileno())
            self.seg_size[sid] = good
            # the active (last) segment is always resident; a closed one only while RAM allows
            self.arenas[sid] = bytearray(data[:good]) if (sid == sids[-1] or self.admit(good)) else None
            del data
        self._active = sids[-1]
        self._flushed = self.seg_size[self._active]
        self._file = open(self._seg_path(self._active), "ab")
        self._merge_all()

    def _replay(self, sid: int, data) -> int:
        if not data.startswith(MAGIC):
            raise ValueError("not a PosterChanDB segment: %s" % self._seg_path(sid))
        mv = memoryview(data)
        i = len(MAGIC)
        while i + _FRAME.size <= len(data):
            n, crc = _FRAME.unpack_from(data, i)
            j = i + _FRAME.size
            if j + n > len(data) or n == 0:
                break
            payload = mv[j:j + n]
            if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                break
            op = payload[0]
            if op == OP_PUT:
                origin = payload[1]
                rec = payload[2:]
                eid = bytes(rec[:32]).hex()
                if self._seq_of(eid) is None:          # a compaction copy may exist twice after a crash
                    ev = self.codec.decode(rec)
                    self._apply_put(ev, bytes(rec[:32]), len(rec), j + 2, origin, sid, replay=True)
            elif op == OP_DEAD:
                self.seg_markers[sid].append(bytes(payload))
                s = self._seq_of(bytes(payload[1:33]).hex())
                if s is not None:
                    self._kill(s, persist=False)
            elif op == OP_DERIVED:
                self.seg_markers[sid].append(bytes(payload))
                s = self._seq_of(bytes(payload[1:33]).hex())
                if s is not None:
                    tag, _, value = bytes(payload[33:]).decode("utf-8").partition("\x00")
                    if (tag, value) not in self.derived.setdefault(s, set()):
                        self.derived[s].add((tag, value))
                        self.idx.add(_h("t:%s:%s" % (tag, value)), s)
            i = j + n
        return i

    def _frame(self, payload: bytes) -> int:
        """Append one framed payload to the active segment's arena (on disk at the next drain).
        Returns the payload's offset in the segment."""
        if payload[0] != OP_PUT:
            self.seg_markers.setdefault(self._active, []).append(bytes(payload))
        arena = self.arenas[self._active]
        start = len(arena) + _FRAME.size
        arena += _FRAME.pack(len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
        arena += payload
        self.seg_size[self._active] = len(arena)
        self._pending_n += 1
        return start

    def _merge_all(self) -> None:
        dead = np.frombuffer(self.dead, dtype=np.uint8) if len(self.dead) else None
        self.idx.merge(dead)
        self.words.merge(dead)
        self.dprefix.merge(self.dead)
        self._merge_ids()

    def _drain(self, fsync: bool) -> int:
        """Hand the buffer to the kernel in ONE sequential write; fsync only when asked."""
        n = self._pending_n
        arena = self.arenas[self._active]
        if len(arena) > self._flushed:
            self._file.write(memoryview(arena)[self._flushed:])
            self._file.flush()
            self._flushed = len(arena)
            self._pending_n = 0
        if fsync:
            os.fsync(self._file.fileno())
            try:   # the active segment is resident (its RAM copy IS the file): don't hold it twice in the page cache
                os.posix_fadvise(self._file.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
        return n

    def _rotate_if_full(self) -> None:
        """A full segment is closed, so compaction can later rewrite it on its own."""
        if self._flushed < self.segment_bytes:
            return
        os.fsync(self._file.fileno())
        self._file.close()
        self._active += 1
        self._new_segment(self._active)
        self.arenas[self._active] = bytearray(MAGIC)
        self.seg_size[self._active] = len(MAGIC)
        self._flushed = len(MAGIC)
        self.seg_bytes[self._active] = 0
        self.seg_dead[self._active] = 0
        self.seg_markers[self._active] = []
        self._file = open(self._seg_path(self._active), "ab")

    def flush(self) -> int:
        """Write everything buffered to the active segment and fsync. Returns records written."""
        with self._lock:
            n = self._drain(fsync=True)
            self._merge_all()
            self._last_flush = time.monotonic()
            self._rotate_if_full()
            return n

    def maybe_flush(self) -> int:
        if self._pending_n and time.monotonic() - self._last_flush >= self.flush_interval:
            return self.flush()
        return 0

    def close(self) -> None:
        with self._lock:
            if self._file is None:
                return
            self.flush()
            self._file.close()
            self._file = None
            for fd in self._fds.values():
                os.close(fd)
            self._fds = {}

    def disk_bytes(self) -> int:
        return sum(self.seg_size.values())

    def stats(self) -> dict:
        dead = int(np.count_nonzero(np.frombuffer(self.dead, np.uint8))) if len(self.dead) else 0
        return {"events": len(self.off), "live": len(self.off) - dead, "dead": dead,
                "disk_bytes": self.disk_bytes(),
                "arena_bytes": self.resident_bytes(),
                "cold_segments": sum(1 for a in self.arenas.values() if a is None),
                "index_bytes": self.idx.nbytes() + self.words.nbytes() + sum(k.nbytes + v.nbytes for k, v in self._id_runs),
                "segments": len(self.arenas), "runs": len(self.idx.runs), "id_bytes": len(self.ids),
                "unflushed": self._pending_n, "since_flush_s": round(time.monotonic() - self._last_flush, 1)}

    # ---------------------------------------------------------------- record access
    def _rec(self, seq: int):
        sid = self.seg[seq]
        o, n = self.off[seq], self.length[seq]
        self.seg_hits[sid] = self.seg_hits.get(sid, 0) + 1
        self.seg_last[sid] = time.monotonic()
        a = self.arenas.get(sid)
        if a is not None:
            return memoryview(a)[o:o + n]
        fd = self._fds.get(sid)
        if fd is None:
            fd = self._fds[sid] = os.open(self._seg_path(sid), os.O_RDONLY)
        return os.pread(fd, n, o)

    def _id_hex(self, seq: int) -> str:
        return self.ids[seq * 32:seq * 32 + 32].hex()

    # ---------------------------------------------------------------- the RAM cache (driven by cache.py)
    def resident_bytes(self) -> int:
        return sum(len(a) for a in self.arenas.values() if a is not None)

    def evict(self, sid: int) -> int:
        """Drop a CLOSED segment's bytes from RAM (reads then come from its file). Returns bytes freed."""
        with self._lock:
            a = self.arenas.get(sid)
            if a is None or sid == self._active:
                return 0
            self.arenas[sid] = None
            return len(a)

    def load(self, sid: int) -> int:
        """Read a closed segment back into RAM — one sequential read. Returns bytes loaded."""
        with self._lock:
            if sid not in self.arenas or self.arenas[sid] is not None:
                return 0
            size = self.seg_size[sid]
        with open(self._seg_path(sid), "rb") as f:     # immutable once closed: safe to read unlocked
            data = bytearray(f.read(size))
        with self._lock:
            if sid in self.arenas and self.arenas[sid] is None and len(data) == size:
                self.arenas[sid] = data
                return size
            return 0

    def pubkey_of(self, seq: int) -> str:
        return self._authors[self.author[seq]]

    # ---------------------------------------------------------------- ids
    def _merge_ids(self) -> None:
        if self._id_delta:
            k = np.fromiter(self._id_delta.keys(), dtype=np.uint64, count=len(self._id_delta))
            v = np.fromiter(self._id_delta.values(), dtype=np.uint32, count=len(self._id_delta))
            o = np.argsort(k)
            self._id_runs.append((k[o], v[o]))
            self._id_delta = {}
        seg = np.frombuffer(self.seg, dtype=np.uint32) if len(self.seg) else None
        while len(self._id_runs) > 1 and len(self._id_runs[-1][0]) * 4 >= len(self._id_runs[-2][0]):
            kb, vb = self._id_runs.pop()
            ka, va = self._id_runs.pop()
            k = np.concatenate([ka, kb]); v = np.concatenate([va, vb])
            if seg is not None:      # records compacted away are forgotten, here, in a merge that happens anyway
                keep = seg[v] != DROPPED
                k, v = k[keep], v[keep]
            o = np.argsort(k, kind="stable")
            self._id_runs.append((k[o], v[o]))

    def _seq_of(self, eid: str):
        if not isinstance(eid, str) or len(eid) != 64:
            return None
        try:
            key = int(eid[:16], 16)
        except ValueError:
            return None
        s = self._id_delta.get(key)
        cands = [s] if s is not None else []
        ku = np.uint64(key)
        for keys, seqs in self._id_runs:
            lo = int(np.searchsorted(keys, ku, "left"))
            if lo < len(keys) and keys[lo] == ku:
                hi = int(np.searchsorted(keys, ku, "right"))
                cands += [int(x) for x in seqs[lo:hi]]
        for c in cands:          # an 8-byte prefix can collide: confirm against the record's own id
            if self.seg[c] != DROPPED and self._id_hex(c) == eid:
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
        return a["id"] < self._id_hex(b_seq)

    def _kill(self, seq: int, *, persist: bool) -> None:
        """Mark an event dead. `persist` writes an OP_DEAD marker — only for deaths that cannot be
        re-derived on replay (auto-clean); superseded versions, NIP-09 deletions and expiry are
        recomputed from the events themselves every time the store opens."""
        if self.dead[seq]:
            return
        self.dead[seq] = 1
        if self.seg[seq] != DROPPED:
            self.seg_dead[self.seg[seq]] = self.seg_dead.get(self.seg[seq], 0) + self.length[seq]
        if persist:
            self._frame(bytes([OP_DEAD]) + bytes.fromhex(self._id_hex(seq)))

    def put(self, ev: dict, *, direct: bool = False, origin: str | None = None) -> str:
        """Store a (signature-verified) event. Returns 'stored', 'duplicate', 'superseded' (an older
        version of a replaceable the store already has newer), 'deleted' (its author's kind-5 already
        names it) or 'ephemeral' (not stored)."""
        if is_ephemeral(ev["kind"]):
            return "ephemeral"
        with self._lock:
            if self._seq_of(ev["id"]) is not None:
                return "duplicate"
            if self._deleted_by_author(ev):
                return "deleted"
            addr = self._address(ev)
            if addr is not None:
                cur = self.current.get(addr)
                if cur is not None and not self.dead[cur] and not self._newer(ev, cur):
                    return "superseded"
            o = ORIGINS.get(origin or ("direct" if direct else "wot"), 4)
            rec = self.codec.encode(ev)
            start = self._frame(bytes([OP_PUT, o]) + rec)
            self._apply_put(ev, rec[:32], len(rec), start + 2, o, self._active)
            if (direct or o == 0) and self.direct_durable:
                self.flush()
            return "stored"

    def add_derived_tag(self, event_id: str, tag: str, value: str) -> bool:
        """An index-only tag the relay derives (today: `_quote_author`). Logged, so it survives a restart."""
        with self._lock:
            s = self._seq_of(event_id)
            if s is None:
                return False
            if (tag, value) in self.derived.get(s, ()):
                return True
            self._frame(bytes([OP_DERIVED]) + bytes.fromhex(event_id) + ("%s\x00%s" % (tag, value)).encode("utf-8"))
            self.derived.setdefault(s, set()).add((tag, value))
            self.idx.add(_h("t:%s:%s" % (tag, value)), s)
            return True

    def _apply_put(self, ev: dict, raw_id: bytes, n: int, off: int, origin: int, sid: int,
                   replay: bool = False) -> None:
        seq = len(self.off)
        self.seg.append(sid)
        self.off.append(off)
        self.length.append(n)
        self.ids += raw_id
        self.seg_bytes[sid] = self.seg_bytes.get(sid, 0) + n
        self.created.append(ev["created_at"])
        self.kind.append(ev["kind"])
        self.expires.append(_expiration(ev))
        self.origin.append(origin)
        pk = ev["pubkey"]
        aid = self._author_ix.get(pk)
        if aid is None:
            aid = self._author_ix[pk] = len(self._authors)
            self._authors.append(pk)
        self.author.append(aid)
        self.dead.append(0)
        self._id_delta[int(ev["id"][:16], 16)] = seq
        k = ev["kind"]
        self.idx.add(_h("ak:%s:%d" % (pk, k)), seq)
        self.idx.add(_h("au:%s" % pk), seq)
        self.idx.add(_h("k:%d" % k), seq)
        for t in ev.get("tags") or []:
            # exactly the relay's rule: single-letter STRING names, value indexed as str(t[1])
            if len(t) >= 2 and isinstance(t[0], str) and len(t[0]) == 1:
                v = t[1] if isinstance(t[1], str) else str(t[1])
                self.idx.add(_h("t:%s:%s" % (t[0], v)), seq)
                if t[0] == "d":
                    self.dprefix.add(v, seq)
        for w in search_words(ev.get("content", "")):
            self.words.add(_h(w), seq)
        addr = self._address(ev)
        if addr is not None:
            cur = self.current.get(addr)
            if cur is None or self.dead[cur] or self._newer(ev, cur):
                if cur is not None:
                    self._kill(cur, persist=False)
                self.current[addr] = seq
            else:
                self._kill(seq, persist=False)
        if k == 5 and not replay:       # on replay its deaths come back as the OP_DEAD markers it wrote
            self._apply_deletion(ev)

    def _apply_deletion(self, ev: dict) -> None:
        """NIP-09, exactly as the relay applies it (nostr_relay/store.py `if kind == 5`): `e` removes the
        author's OWN event of any kind but 5 and 1059 (Concord giftwraps fold their own deletions); `a`
        removes the author's events of that kind carrying that `d` value, up to the deletion's time.
        The deaths are PERSISTED (OP_DEAD), not re-derived on replay: compaction moves records forward,
        and a deletion replayed after an event that arrived later would kill what the relay kept."""
        pk = ev["pubkey"]
        for t in ev.get("tags") or []:
            if len(t) < 2:
                continue
            if t[0] == "e":
                s = self._seq_of(t[1])
                if s is not None and self.pubkey_of(s) == pk and self.kind[s] not in (5, 1059):
                    self._kill(s, persist=True)
            elif t[0] == "a":
                parts = str(t[1]).split(":", 2)
                if len(parts) == 3 and parts[1] == pk and parts[0].isdigit():
                    hit = np.intersect1d(self.idx.get(_h("t:d:%s" % parts[2])),
                                         self.idx.get(_h("ak:%s:%d" % (pk, int(parts[0])))))
                    for s in hit:
                        s = int(s)
                        if s < len(self.dead) and not self.dead[s] and self.created[s] <= ev["created_at"]:
                            self._kill(s, persist=True)

    def _deleted_by_author(self, ev: dict) -> bool:
        """The relay's rule: a sync/backfill must not resurrect an event its author already deleted —
        the retained kind-5 naming it (`e`) is the record, even when the deletion arrived first."""
        if ev["kind"] in (5, 1059):
            return False
        for s in np.intersect1d(self.idx.get(_h("t:e:%s" % ev["id"])),
                                self.idx.get(_h("ak:%s:5" % ev["pubkey"]))):
            if not self.dead[int(s)]:
                return True
        return False

    def kill(self, seqs) -> int:
        """Auto-clean: mark events dead durably (an OP_DEAD marker each). Returns how many changed.
        A direct write is never killed here — that is not an auto-clean decision (see maintenance.py)."""
        n = 0
        with self._lock:
            for s in seqs:
                s = int(s)
                if not self.dead[s] and self.seg[s] != DROPPED:
                    self._kill(s, persist=True)
                    n += 1
        return n

    # ---------------------------------------------------------------- compaction
    def segment_dead_pct(self) -> dict:
        return {sid: (100.0 * self.seg_dead.get(sid, 0) / b if b else 0.0) for sid, b in self.seg_bytes.items()}

    def compaction_candidate(self):
        """The deadest CLOSED segment over the threshold, or None."""
        cands = [(pct, sid) for sid, pct in self.segment_dead_pct().items()
                 if sid != self._active and pct >= self.compact_dead_pct]
        return max(cands)[1] if cands else None

    def compact(self, sid: int | None = None, pace=None) -> dict:
        """Reclaim a closed segment: copy its LIVE records into the active segment, carry forward the
        markers that still mean something, fsync ONCE, then delete the old file and free its RAM.

        Disk cost is exactly the segment's live bytes, written sequentially in COMPACT_CHUNK pieces;
        nothing is read from disk while the segment is resident (records and markers are in RAM; a COLD
        segment is read once, in file order, through the page cache). The lock is released
        between chunks and `pace(nbytes, cpu_s)` (maintenance's throttle) is called there, so a
        compaction never holds up a write for more than one chunk. Safe at any crash point: the old
        file is deleted only after the copies are fsynced, and a replay skips an id it already has."""
        with self._lock:
            if self._compacting:
                return {"compacted": None}
            sid = self.compaction_candidate() if sid is None else sid
            if sid is None or sid == self._active or sid not in self.arenas or sid not in self.seg_size:
                return {"compacted": None}
            self._compacting = True
        try:
            seqs = np.nonzero(np.frombuffer(self.seg, dtype=np.uint32) == sid)[0]
            moved = written = 0
            i = 0
            while i < len(seqs):
                t0 = time.thread_time()
                with self._lock:
                    chunk = 0
                    while i < len(seqs) and chunk < COMPACT_CHUNK:
                        s = int(seqs[i]); i += 1
                        if self.dead[s] or self.seg[s] != sid:
                            continue
                        n = self.length[s]
                        rec = bytes(self._rec(s))
                        start = self._frame(bytes([OP_PUT, self.origin[s]]) + rec)
                        self.seg[s] = self._active
                        self.off[s] = start + 2
                        # a derived tag must follow its record on replay, wherever its old marker sits
                        for tag, value in sorted(self.derived.get(s, ())):
                            self._frame(bytes([OP_DERIVED]) + rec[:32] + ("%s\x00%s" % (tag, value)).encode("utf-8"))
                        self.seg_bytes[self._active] = self.seg_bytes.get(self._active, 0) + n
                        chunk += n
                        moved += 1
                    self._drain(fsync=False)
                    self._rotate_if_full()
                written += chunk
                if pace:
                    pace(chunk, time.thread_time() - t0)
            with self._lock:
                carried = 0
                for payload in self.seg_markers.get(sid, []):
                    s = self._seq_of(bytes(payload[1:33]).hex())
                    if s is None:
                        continue
                    if payload[0] == OP_DEAD and self.dead[s] and self.seg[s] != sid:
                        self._frame(payload); carried += 1       # the record it kills lives on elsewhere
                    elif payload[0] == OP_DERIVED and not self.dead[s] and self.seg[s] < sid:
                        # its record sits in an EARLIER segment, so the copy stays after it on replay; a
                        # record that moved (now in a later segment) already had its tags re-emitted beside it
                        self._frame(payload); carried += 1
                self._drain(fsync=True)
                self._rotate_if_full()
                os.remove(self._seg_path(sid))
                self._fsync_dir()
                seg = self.seg
                for s in seqs:                                # dead records here now have no bytes anywhere
                    s = int(s)
                    if seg[s] == sid:
                        seg[s] = DROPPED
                        self.derived.pop(s, None)
                del self.arenas[sid]
                fd = self._fds.pop(sid, None)
                if fd is not None:
                    os.close(fd)
                for d in (self.seg_size, self.seg_hits, self.seg_last):
                    d.pop(sid, None)
                self.seg_bytes.pop(sid, None)
                self.seg_dead.pop(sid, None)
                self.seg_markers.pop(sid, None)
                return {"compacted": sid, "moved": moved, "written": written, "carried": carried}
        finally:
            self._compacting = False

    # ---------------------------------------------------------------- read
    def get(self, seq: int) -> dict:
        return self.codec.decode(self._rec(seq))

    def seq_of(self, eid: str):
        with self._lock:
            return self._seq_of(eid)

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
                    if not self._id_hex(int(cand[j])) < c1:
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
