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
import collections
import threading
import time
import zlib
from functools import lru_cache

import numpy as np

from app.services.nostr.quotes import quote_pubkeys, quoted_ids_without_author, remember_quote_authors
from app.services.nostr_relay import store as relay_rules   # the relay's own rule constants: one source

from . import snapshot, tsparser
from .codec import Codec

MAGIC = b"PCDB1\n"
OP_PUT, OP_DEAD, OP_DERIVED = 1, 2, 3   # OP_PUT payload: op, origin byte, codec record
_FRAME = struct.Struct("<II")


@lru_cache(maxsize=1 << 17)    # index keys repeat constantly (k:1, an author's au:/ak:): hashing was ~10% of ingest
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


def _expiration(ev: dict):
    """The relay's parse (_insert_one): the FIRST expiration tag only, int(); unreadable = no expiration."""
    for t in ev.get("tags") or []:
        if len(t) >= 2 and t[0] == "expiration":
            try:
                return int(t[1])
            except (ValueError, TypeError):
                return None
    return None


def _first_d(tags) -> str:
    """The relay's `d` for an addressable event: the first ["d", v] with a value (a bare ["d"] is skipped)."""
    return next((t[1] for t in tags if len(t) >= 2 and t[0] == "d"), "")


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

    def member(self, key: int, seqs: np.ndarray) -> np.ndarray:
        """Boolean mask: which of `seqs` carry `key` — binary search inside each run's slice for the key,
        no copy of the posting list (a common key like an empty `d` can hold millions)."""
        seqs = np.asarray(seqs, dtype=np.uint32)
        out = np.zeros(len(seqs), dtype=bool)
        if not len(seqs):
            return out
        k = np.uint64(key)
        for keys, starts, post in self.runs:
            i = int(np.searchsorted(keys, k))
            if i < len(keys) and keys[i] == k:
                sl = post[starts[i]:starts[i + 1]]
                if len(sl) > 1 and not bool(np.all(sl[1:] >= sl[:-1])):
                    sl = np.sort(sl)
                pos = np.searchsorted(sl, seqs)
                pos[pos >= len(sl)] = len(sl) - 1
                out |= sl[pos] == seqs
        d = self.delta.get(key)
        if d:
            out |= np.isin(seqs, np.asarray(d, dtype=np.uint32))
        return out

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
                 compact_dead_pct: float = 40.0, admit=None, log=None, snapshots: bool = True,
                 snapshot_min_events: int = 20000):
        self.path = path
        self.codec = Codec(zdict)
        self.flush_interval = float(flush_interval)
        self.direct_durable = bool(direct_durable)
        self.segment_bytes = int(segment_bytes)
        self.compact_dead_pct = float(compact_dead_pct)
        self.log = log or (lambda *a: None)
        # admit(nbytes) -> bool: may a closed segment stay resident after it is read at startup? (cache.py)
        self.admit = admit or (lambda n: True)
        self.snapshots = bool(snapshots)
        self.snapshot_min_events = int(snapshot_min_events)
        self._since_snap = 0          # log records written since the last snapshot (what a restart would replay)
        self.last_open = {}
        self._lock = threading.RLock()
        self._reset_state()
        self._flushed = 0        # bytes of the active segment's arena that are on disk
        self._pending_n = 0
        self._last_flush = time.monotonic()
        self._active = 0
        self._file = None
        os.makedirs(path, exist_ok=True)
        self._open()

    def _reset_state(self) -> None:
        """Every index and column, empty — used before a full replay (and after a snapshot that failed part-way)."""
        # Events whose SEARCH words are not indexed yet (a copy and a log replay defer them: the Postgres-exact
        # tokenizer is ~93% of the cost of storing a real event). A search catches them up first, so answers
        # stay exact; the mirror indexes them in the background and sends searches to Postgres meanwhile.
        self._unworded = collections.deque()
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
        t0 = time.monotonic()
        sids = self._segments()
        if not sids:
            self._new_segment(1)
            sids = [1]
        meta = None
        if self.snapshots:
            ok, why, meta = snapshot.load(self)
            if not ok:
                if meta is None and why != "no snapshot":
                    self.log("[posterchandb] snapshot not used: %s — replaying the whole log" % why)
                self._reset_state()
                meta = None
        replayed = 0
        for sid in sids:
            p = self._seg_path(sid)
            with open(p, "rb") as f:
                data = f.read()
            self.seg_bytes.setdefault(sid, 0)
            self.seg_dead.setdefault(sid, 0)
            self.seg_markers.setdefault(sid, [])
            if meta is not None and sid < meta["active"]:
                good = len(data)                       # closed and covered by the snapshot: nothing to parse
            else:
                start = meta["flushed"] if (meta is not None and sid == meta["active"]) else None
                n0 = len(self.off)
                good = self._replay(sid, data, start)
                replayed += len(self.off) - n0
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
        self._since_snap = replayed if meta is not None else len(self.off)
        try:
            self.last_snapshot = os.path.getmtime(os.path.join(self.path, snapshot.NAME))
        except OSError:
            self.last_snapshot = 0.0
        self.last_open = {"snapshot": meta is not None, "events": len(self.off), "replayed": replayed,
                          "seconds": round(time.monotonic() - t0, 3)}

    def _replay(self, sid: int, data, start=None) -> int:
        if not data.startswith(MAGIC):
            raise ValueError("not a PosterChanDB segment: %s" % self._seg_path(sid))
        mv = memoryview(data)
        i = start if start else len(MAGIC)
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
                s0 = self._seq_of(eid)
                # Skip only a LIVE duplicate (a compaction copy may exist twice after a crash). A dead one is an
                # event pruned/purged and then received again, which is stored again -- as on Postgres.
                if s0 is None or self.dead[s0]:
                    ev = self.codec.decode(rec)
                    self._apply_put(ev, bytes(rec[:32]), len(rec), j + 2, origin, sid, replay=True, words=False)
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
        self._since_snap += 1
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

    @property
    def words_pending(self) -> int:
        return len(self._unworded)

    def index_pending_words(self, limit: int | None = None) -> int:
        """Index the search words of up to `limit` deferred events (all when None). Returns how many."""
        with self._lock:
            q = self._unworded
            n = len(q) if limit is None else min(limit, len(q))
            batch = [q.popleft() for _ in range(n)]
            for seq in batch:
                if self.dead[seq] or self.seg[seq] == DROPPED:
                    continue
                for w in search_words(self.codec.decode(self._rec(seq)).get("content", "")):
                    self.words.add(_h(w), seq)
            return len(batch)

    def snapshot(self):
        """Write an index snapshot now (flushes first). None while a compaction is running or search words are
        still deferred (a snapshot is a COMPLETE index, so a restart from it has nothing left to catch up)."""
        with self._lock:
            if self._file is None or self._compacting or self._unworded:
                return None
            self.flush()
            res = snapshot.save(self)
            self._since_snap = 0
            self.last_snapshot = time.time()
            return res

    def close(self, take_snapshot: bool = True) -> None:
        """Flush (fsync) and close. `snapshot=False` for a stop with a deadline (the relay has systemd's 10 s):
        the snapshot is the slow part, and the log alone is complete -- the next start replays its tail."""
        with self._lock:
            if self._file is None:
                return
            self.flush()
            if take_snapshot and self.snapshots and self._since_snap >= self.snapshot_min_events and not self._unworded:
                try:
                    snapshot.save(self)          # a clean stop: the next start loads instead of re-indexing
                    self._since_snap = 0
                except OSError as e:
                    self.log("[posterchandb] snapshot at close failed (%s) — the next start replays" % e)
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

    def _merge_ids_once_more(self) -> None:
        """Merge the two newest id runs regardless of size (snapshot: one run)."""
        if len(self._id_runs) < 2:
            return
        seg = np.frombuffer(self.seg, dtype=np.uint32) if len(self.seg) else None
        kb, vb = self._id_runs.pop()
        ka, va = self._id_runs.pop()
        k = np.concatenate([ka, kb]); v = np.concatenate([va, vb])
        if seg is not None:
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
        # An 8-byte prefix can collide: confirm against the record's own id. One id can have SEVERAL records --
        # an event pruned or purged and then received again (the firehose re-sends old events) is stored
        # again, exactly as the relay's Postgres re-inserts a deleted row -- so the LIVE one is the answer.
        dead_hit = None
        for c in cands:
            if self.seg[c] != DROPPED and self._id_hex(c) == eid:
                if not self.dead[c]:
                    return c
                dead_hit = c
        return dead_hit

    # ---------------------------------------------------------------- write
    def _kill(self, seq: int, *, persist: bool) -> None:
        """Mark an event dead. `persist` writes an OP_DEAD marker. Every decision that depends on ARRIVAL
        (a superseded version, a NIP-09 deletion, auto-clean) is persisted: compaction reorders the log, so
        re-deriving them on replay could reach a different answer than the relay did."""
        if self.dead[seq]:
            return
        self.dead[seq] = 1
        if self.seg[seq] != DROPPED:
            self.seg_dead[self.seg[seq]] = self.seg_dead.get(self.seg[seq], 0) + self.length[seq]
        if persist:
            self._frame(bytes([OP_DEAD]) + bytes.fromhex(self._id_hex(seq)))

    def _live(self, arr):
        return [int(x) for x in arr if not self.dead[int(x)]]

    def _same_coordinate(self, ev: dict, kind: int) -> list:
        """The rows the relay would compare a replaceable/addressable event against (live versions only)."""
        pk = ev["pubkey"]
        rows = self.idx.get(_h("ak:%s:%d" % (pk, kind)))
        if is_replaceable(kind):
            return self._live(rows)
        d = _first_d(ev.get("tags") or [])
        d = d if isinstance(d, str) else str(d)
        rows = np.asarray(rows, dtype=np.uint32)
        if d:     # a stored version matches if ANY of its d tags is this value (the relay joins event_tags)
            return self._live(rows[self.idx.member(_h("t:d:%s" % d), rows)])
        # empty d = no d tag at all OR an explicit ["d", ""]
        m = self.idx.member(_h("nod:%s:%d" % (pk, kind)), rows) | self.idx.member(_h("t:d:"), rows)
        return self._live(rows[m])

    def put(self, ev: dict, *, direct: bool = False, origin: str | None = None) -> str:
        """Store a (signature-verified) event, by EXACTLY the rules of the relay's _insert_one, which
        tests/test_posterchandb_vs_relay.py holds it to. Returns 'stored', 'duplicate', or why it was not:
        'retired', 'future', 'fedi-only', 'expired', 'deleted', 'superseded'."""
        origin_name = origin or ("direct" if direct else "wot")
        kind, created = int(ev["kind"]), int(ev["created_at"])
        tags = ev.get("tags") or []
        now = int(time.time())
        if kind in relay_rules._RETIRED_KINDS:
            return "retired"
        if created > now + 900:
            return "future"
        if ["client-mode", "fedi-only"] in tags:
            return "fedi-only"
        exp = None if kind in relay_rules._NEVER_EXPIRE_KINDS else _expiration(ev)
        if exp is not None and exp <= now:
            return "expired"
        with self._lock:
            s0 = self._seq_of(ev["id"])
            dup = s0 is not None and not self.dead[s0]
            # A LIVE DUPLICATE still runs the relay's side effects: its INSERT is ON CONFLICT DO NOTHING but the
            # statements around it are not, so a re-sent deletion deletes targets that arrived since, and a re-sent
            # newest version deletes older versions of its coordinate that arrived since (an older version whose
            # FIRST d tag differs is stored beside it, and only the newer one's re-send matches it).
            if dup and kind == 5:
                self._apply_deletion(ev)
            if not dup and self._deleted_by_author(ev):
                return "deleted"
            losers = []
            if is_replaceable(kind) or is_addressable(kind):
                strict = kind == 10133 if is_replaceable(kind) else kind in relay_rules._STRICT_TIE_KINDS
                eid = ev["id"]
                for s in self._same_coordinate(ev, kind):
                    if s == s0:
                        continue                     # the relay skips its own row (`row["id"] == eid`)
                    rc = self.created[s]
                    tie_direct = (not strict and rc == created and origin_name == "direct"
                                  and self.origin[s] == ORIGINS["direct"])
                    if rc < created or (rc == created and eid < self._id_hex(s)) or tie_direct:
                        losers.append(s)
                    else:
                        return "duplicate" if dup else "superseded"
            for s in losers:
                self._kill(s, persist=True)
            if dup:
                return "duplicate"
            o = ORIGINS.get(origin_name, 4)
            rec = self.codec.encode(ev)
            start = self._frame(bytes([OP_PUT, o]) + rec)
            self._apply_put(ev, rec[:32], len(rec), start + 2, o, self._active)
            self._index_quotes(ev)
            if kind == 5:
                self._apply_deletion(ev)
            if o == 0 and self.direct_durable:
                self.flush()
            return "stored"

    def copy_put(self, ev: dict, origin: str = "wot") -> str:
        """Store an event EXACTLY as another store holds it -- no rule is re-decided. For copying the relay's
        Postgres (mirror.py): its rows are the result of the rules applied in ARRIVAL order, and re-running
        them in another order (a load goes oldest-first) does not reach the same state -- which version of a
        multi-d document survives, whether a deletion arrived before or after its target. Derived tags are
        copied separately (add_derived_tag)."""
        with self._lock:
            s0 = self._seq_of(ev["id"])
            if s0 is not None and not self.dead[s0]:
                return "duplicate"
            o = ORIGINS.get(origin or "wot", 4)
            rec = self.codec.encode(ev)
            start = self._frame(bytes([OP_PUT, o]) + rec)
            self._apply_put(ev, rec[:32], len(rec), start + 2, o, self._active, words=False)
            return "stored"

    def _index_quotes(self, ev: dict) -> None:
        """The relay's derived `_quote_author` index, both directions: this quote's authors now, and — when
        a kind-1 arrives — the quotes that arrived BEFORE it and named no author."""
        eid = ev["id"]
        resolved = {}
        for qid in quoted_ids_without_author(ev):
            s = self._seq_of(qid)
            if s is not None and not self.dead[s]:
                resolved[qid] = self.pubkey_of(s)
        if resolved:
            remember_quote_authors(eid, resolved.values())
        for recipient in quote_pubkeys(ev, resolved):
            self.add_derived_tag(eid, "_quote_author", recipient)
        if int(ev["kind"]) == 1:
            quoting = np.asarray(self.idx.get(_h("t:q:%s" % eid)), dtype=np.uint32)
            for s in self._live(quoting[np.frombuffer(self.kind, dtype=np.uint32)[quoting] == 1] if len(quoting) else []):
                q = self.get(s)
                if eid in quoted_ids_without_author({"kind": 1, "tags": q["tags"]}):
                    self.add_derived_tag(q["id"], "_quote_author", ev["pubkey"])

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
                   replay: bool = False, words: bool = True) -> None:
        seq = len(self.off)
        self.seg.append(sid)
        self.off.append(off)
        self.length.append(n)
        self.ids += raw_id
        self.seg_bytes[sid] = self.seg_bytes.get(sid, 0) + n
        self.created.append(ev["created_at"])
        self.kind.append(ev["kind"])
        exp = None if int(ev["kind"]) in relay_rules._NEVER_EXPIRE_KINDS else _expiration(ev)
        self.expires.append(exp if exp and exp > 0 else 0)
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
        has_d = False
        for t in ev.get("tags") or []:
            # exactly the relay's rule: single-letter STRING names, value indexed as str(t[1])
            if len(t) >= 2 and isinstance(t[0], str) and len(t[0]) == 1:
                v = t[1] if isinstance(t[1], str) else str(t[1])
                self.idx.add(_h("t:%s:%s" % (t[0], v)), seq)
                if t[0] == "d":
                    self.dprefix.add(v, seq)
                    has_d = True
        if is_addressable(k) and not has_d:
            self.idx.add(_h("nod:%s:%d" % (pk, k)), seq)
        if words:
            for w in search_words(ev.get("content", "")):
                self.words.add(_h(w), seq)
        else:
            self._unworded.append(seq)

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
                    rows = np.asarray(self.idx.get(_h("ak:%s:%d" % (pk, int(parts[0])))), dtype=np.uint32)
                    hit = rows[self.idx.member(_h("t:d:%s" % parts[2]), rows)] if len(rows) else rows
                    for s in hit:
                        s = int(s)
                        if s < len(self.dead) and not self.dead[s] and self.created[s] <= ev["created_at"]:
                            self._kill(s, persist=True)

    def _deleted_by_author(self, ev: dict) -> bool:
        """The relay's rule: a sync/backfill must not resurrect an event its author already deleted —
        the retained kind-5 naming it (`e`) is the record, even when the deletion arrived first."""
        if ev["kind"] in (5, 1059):
            return False
        naming = np.asarray(self.idx.get(_h("t:e:%s" % ev["id"])), dtype=np.uint32)
        if not len(naming):
            return False
        mine = naming[self.idx.member(_h("ak:%s:5" % ev["pubkey"]), naming)]
        return any(not self.dead[int(s)] for s in mine)

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
        """One NIP-01 filter (+ NIP-50 `search`), exactly as the relay's _query_one answers it:
        ORDER BY created_at DESC, id DESC, `limit or 500` clamped to 1..5000, dead and expired never returned.

        Planning: only the SELECTIVE parts of a filter (ids, authors, tags, `#d~`, search words) are turned
        into posting lists, intersected smallest-first by binary search; `kinds` with anything selective is
        a column check on what is left, never the union of every event of those kinds. Ordering uses the
        created_at column and the in-RAM ids, so only the events actually returned are decoded."""
        now = int(time.time()) if now is None else now
        with self._lock:
            n = len(self.off)
            if not n:
                return []
            sets = []
            ids = flt.get("ids")
            if ids:
                got = [self._seq_of(i) for i in ids if isinstance(i, str) and len(i) == 64]
                sets.append(np.unique(np.array([x for x in got if x is not None], dtype=np.uint32)))
            authors, kinds = flt.get("authors"), flt.get("kinds")
            kinds_done = False
            if authors and kinds:
                sets.append(_union([self.idx.get(_h("ak:%s:%d" % (a, int(k)))) for a in authors for k in kinds]))
                kinds_done = True
            elif authors:
                sets.append(_union([self.idx.get(_h("au:%s" % a)) for a in authors]))
            for key, vals in flt.items():
                if not (isinstance(key, str) and key.startswith("#") and vals):
                    continue
                if len(key) == 2:
                    tags = [key[1]] + (["_quote_author"] if key == "#p" and flt.get("_include_quotes") is True else [])
                    sets.append(_union([self.idx.get(_h("t:%s:%s" % (tg, v))) for tg in tags for v in vals]))
                elif len(key) == 3 and key.endswith("~"):
                    if key[1] != "d":
                        return []          # prefix matching is indexed for `d` only — the app's one use
                    sets.append(np.unique(np.array([q for v in vals for q in self.dprefix.prefix(str(v))],
                                                   dtype=np.uint32)))
            if flt.get("search"):
                if self._unworded:
                    self.index_pending_words()     # exact answers: catch up whatever a copy/replay deferred
                words = search_words(flt["search"])
                if not words:
                    return []              # plainto_tsquery with no lexemes matches nothing
                for w in words:
                    sets.append(_union([self.words.get(_h(w))]))
            kind_col = np.frombuffer(self.kind, dtype=np.uint32)
            if sets:
                sets.sort(key=len)
                cand = sets[0]
                for other in sets[1:]:
                    if not len(cand):
                        break
                    cand = _intersect(cand, other)
                if kinds and not kinds_done and len(cand):
                    cand = cand[np.isin(kind_col[cand], np.asarray([int(k) for k in kinds], dtype=np.uint32))]
            elif kinds:
                cand = _union([self.idx.get(_h("k:%d" % int(k))) for k in kinds])
            else:
                cand = np.arange(n, dtype=np.uint32)
            if not len(cand):
                return []
            created = np.frombuffer(self.created, dtype=np.uint64)
            expires = np.frombuffer(self.expires, dtype=np.uint64)
            dead = np.frombuffer(self.dead, dtype=np.uint8)
            keep = dead[cand] == 0
            exp = expires[cand]
            keep &= (exp == 0) | (exp > now)
            if "since" in flt and flt["since"] is not None:
                keep &= created[cand] >= int(flt["since"])
            if "until" in flt and flt["until"] is not None:
                keep &= created[cand] <= int(flt["until"])
            cur = flt.get("_cursor")
            if isinstance(cur, list) and len(cur) == 2:
                c0, c1 = int(cur[0]), str(cur[1])
                cc = created[cand]
                keep &= cc <= c0
                for j in np.nonzero(keep & (cc == c0))[0]:   # equal timestamps: page on the id (`e.id < cursor`)
                    if not self._id_hex(int(cand[j])) < c1:
                        keep[j] = False
            cand = cand[keep]
            if not len(cand):
                return []
            # The relay's own rule (store.py _query_one): `limit or 500`, clamped to 1..5000.
            limit = max(1, min(int(flt.get("limit") or 500), 5000))
            c = created[cand]
            if len(cand) > limit:
                # keep every event at the cut-off timestamp: the id decides among them below
                cut = np.partition(c, len(c) - limit)[len(c) - limit]
                sel = c >= cut
                cand, c = cand[sel], c[sel]
            # ORDER BY created_at DESC, id DESC — the id as four big-endian words is its exact hex order
            idw = np.frombuffer(self.ids, dtype=">u8").reshape(-1, 4)[cand]
            order = np.lexsort((idw[:, 3], idw[:, 2], idw[:, 1], idw[:, 0], c))[::-1][:limit]
            return [self.get(int(s)) for s in cand[order]]


def _union(arrs) -> np.ndarray:
    """Sorted unique union of posting lists. A single list that is already ascending (the usual case:
    postings are appended in sequence order) is de-duplicated in one pass instead of sorted."""
    arrs = [a for a in arrs if len(a)]
    if not arrs:
        return np.zeros(0, dtype=np.uint32)
    a = arrs[0] if len(arrs) == 1 else np.concatenate(arrs)
    if len(arrs) == 1 and (len(a) < 2 or bool(np.all(a[1:] >= a[:-1]))):
        return a if len(a) < 2 else a[np.concatenate(([True], a[1:] != a[:-1]))]
    return np.unique(a)


def _intersect(small: np.ndarray, big: np.ndarray) -> np.ndarray:
    """Both sorted and unique: binary-search the smaller in the larger (m log n, no sort of either)."""
    if len(small) > len(big):
        small, big = big, small
    if not len(small) or not len(big):
        return small[:0]
    pos = np.searchsorted(big, small)
    pos[pos >= len(big)] = len(big) - 1
    return small[big[pos] == small]
