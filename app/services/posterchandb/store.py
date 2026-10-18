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

import hashlib
import os
import re
import struct
import collections
import itertools
import threading
import time
import zlib
from functools import lru_cache

import numpy as np

from app.services.nostr.quotes import quote_pubkeys, quoted_ids_without_author, remember_quote_authors
from app.services.nostr_relay import store as relay_rules   # the relay's own rule constants: one source

from . import checkpoint, disk, snapshot, tsparser
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
    """The relay's parse: the FIRST expiration tag only, int(); unreadable = no expiration."""
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


# A NIP-44/NIP-04 ciphertext (or any long run with no space outside the base64 alphabet) has no words anybody can
# search for -- what Postgres made of one was hundreds of random fragments -- and tokenising it is the expensive part
# of indexing (the parser costs ~1.3 us a character, and a quarter of a relay's events are encrypted app documents).
_OPAQUE = re.compile(r"[A-Za-z0-9+/=_-]+(?:\?iv=[A-Za-z0-9+/=]+)?")
_OPAQUE_MIN = 256


def index_words(text: str):
    """The words an EVENT is indexed under: search_words, except that opaque ciphertext is not tokenised."""
    if text and len(text) >= _OPAQUE_MIN and _OPAQUE.fullmatch(text):
        return ()
    return search_words(text)


def search_words(text: str):
    """What a search matches on — EXACTLY the lexemes of the relay's `to_tsvector('simple', text)` (tsparser.py
    is Postgres's own parser; tests/test_posterchandb_tsparser.py holds it to a real Postgres). A search is the
    AND of `plainto_tsquery('simple', search)`'s lexemes: the same function applied to the search text."""
    return tsparser.search_set(text or "")


def _grow(op, *args):
    """Grow a column (bytearray / array.array) -- retrying for a moment while a reader's numpy view still exports it.

    A column cannot be resized while any `np.frombuffer` view of it is alive, and a view can outlive the lock
    that guarded its making by the few microseconds it takes its frame to end. One such race stopped the relay's
    mirror 200,000 events into its first copy ("BufferError: Existing exports of data: object cannot be
    re-sized"), the status thread's stats() against the copy's appends. Readers no longer keep views past the
    lock (stats, query), and this is the net under that: each append retries on its own, so the columns can
    never end up different lengths. tests/test_posterchandb_threads.py."""
    for _ in range(4000):
        try:
            return op(*args)
        except BufferError:
            time.sleep(0.0005)
    return op(*args)


def _merge_runs(ka, sa, pa, kb, sb, pb, dead=None):
    """Merge two CSR posting runs in RAM (disk.merge_postings_mem); `dead` is a per-seq array (nonzero = dead)."""
    return disk.merge_postings_mem(ka, sa, pa, kb, sb, pb, None if dead is None else (lambda p: dead[p] == 0))


class _Postings(disk.Leveled):
    """hash key -> ascending sequence numbers, as LEVELED sorted runs (an LSM tree in miniature).

    New postings collect in a small dict (`delta`). A flush turns the delta into one small CSR run --
    sorting only what arrived -- and merges RAM runs only while the newest is at least a quarter the size of
    the one before it. A run that grows past `seal_at`, and every RAM run at a checkpoint, is written to its own
    files and MAPPED (disk.py), and those merge in the background, streamed. So RAM holds the recent postings
    only, and CPU per flush follows what ARRIVED, not the size of the store."""

    dtypes = disk.POSTING_DTYPES
    seal_at = 1 << 22

    def __init__(self, kind: str = "idx"):
        super().__init__(kind)
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
        return np.asarray(parts[0]) if len(parts) == 1 else np.concatenate(parts)

    def get_many(self, keys) -> np.ndarray:
        """The postings of many keys, concatenated (no order, no de-duplication): ONE vectorised search per run for
        all of them. A feed naming 4,109 authors did 8,000+ separate searches per run per request -- a fifth of
        the relay's CPU (py-spy, server1, 2026-10-16)."""
        if not keys:
            return np.zeros(0, dtype=np.uint32)
        ks = np.asarray(keys, dtype=np.uint64)
        parts = []
        for rkeys, starts, post in self.runs:
            if not len(rkeys):
                continue
            i = np.searchsorted(rkeys, ks)
            i = i[i < len(rkeys)]
            i = i[np.isin(rkeys[i], ks, assume_unique=False)]
            for j in np.unique(i):
                parts.append(post[starts[j]:starts[j + 1]])
        if self.delta:
            for k in keys:
                d = self.delta.get(int(k))
                if d:
                    parts.append(np.asarray(d, dtype=np.uint32))
        if not parts:
            return np.zeros(0, dtype=np.uint32)
        return np.asarray(parts[0]) if len(parts) == 1 else np.concatenate(parts)

    def count(self, key: int) -> int:
        """How many postings `key` has, from the run boundaries -- nothing copied (a kind can hold millions)."""
        n = 0
        k = np.uint64(key)
        for keys, starts, _post in self.runs:
            i = int(np.searchsorted(keys, k))
            if i < len(keys) and keys[i] == k:
                n += int(starts[i + 1] - starts[i])
        d = self.delta.get(key)
        return n + (len(d) if d else 0)

    def member(self, key: int, seqs: np.ndarray) -> np.ndarray:
        """Boolean mask: which of `seqs` carry `key` -- binary search inside each run's slice for the key,
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

    def _has_delta(self) -> bool:
        return bool(self.delta)

    def _delta_run(self):
        items = sorted(self.delta.items())
        keys = np.fromiter((k for k, _ in items), dtype=np.uint64, count=len(items))
        lens = np.fromiter((len(v) for _, v in items), dtype=np.int64, count=len(items))
        starts = np.zeros(len(items) + 1, dtype=np.int64)
        np.cumsum(lens, out=starts[1:])
        post = np.fromiter((x for _, v in items for x in v), dtype=np.uint32, count=int(starts[-1]))
        return disk.Run([keys, starts, post])

    def _clear_delta(self) -> None:
        self.delta = {}

    def _merge(self, a, b, sink, live=None, pace=None):
        if sink.dirpath is None:                       # two small RAM runs: one pass
            return disk.Run(disk.merge_postings_mem(*a, *b, live))
        return disk.merge_postings(a, b, sink, live, pace)


class _Prefix(disk.StrRuns):
    """Sorted (value, seq) runs for prefix matching on ONE tag letter (`#d~`, the app's folder reads).
    Leveled like the postings; only events that carry the tag pay for it. Runs are numpy (a byte blob + offsets +
    seqs, disk.py) -- they used to be Python lists of tuples, ~200 bytes an entry, a third of a relay's events.

    A `#d~` read is how every app table loads, a page of 1000 at a time, so the same prefix is asked for
    hundreds of times in a row against runs that do not change. So the range is found with two binary searches and
    the sorted set of matching seqs is cached per prefix until a run changes; only the small unmerged delta is
    scanned per call. Runs are never mutated in place (a merge builds a new one), which is what keeps the cache
    valid."""

    def __init__(self):
        super().__init__("dp")
        self._cache: dict = {}          # (prefix, run ids) -> [those runs, delta scanned, sorted unique seqs]

    def add(self, value: str, seq: int) -> None:
        self.delta.append((value, seq))

    @staticmethod
    def _upper(pre: str):
        """The smallest string greater than every string that starts with `pre` (None: no bound)."""
        while pre:
            c = ord(pre[-1])
            if c < 0x10FFFF:
                return pre[:-1] + chr(c + 1)
            pre = pre[:-1]
        return None

    def seqs(self, pre: str) -> np.ndarray:
        """Sorted unique seqs whose value starts with `pre` (dead ones included). The cache entry also remembers how
        much of the unmerged delta it has looked at, so a query scans only the delta added since the last one --
        the delta holds every write since the last merge (27k+ after a restart), and scanning it whole in Python
        per query was a fifth of the relay's CPU. Delta seqs are newer than every run's, so a new match is
        appended, never re-sorted in."""
        runs = tuple(self.runs)
        key = (pre, tuple(id(r) for r in runs))
        ent = self._cache.get(key)
        if ent is None:
            hi = self._upper(pre)
            pb, hb = disk.enc(pre), (disk.enc(hi) if hi is not None else None)
            parts = []
            for run in runs:
                i, j = self.range(run, pb, hb)
                if j > i:
                    parts.append(np.asarray(run[2][i:j]))
            base = np.unique(np.concatenate(parts)).astype(np.uint32) if parts else np.zeros(0, dtype=np.uint32)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache = {k: v for k, v in self._cache.items() if k[1] == key[1]}
            ent = self._cache[key] = [runs, 0, base]
        d = self.delta
        if ent[1] > len(d):
            ent[1] = 0                                   # the delta was merged since: its matches are in the runs
        if ent[1] < len(d):
            new = sorted({q for v, q in d[ent[1]:] if v.startswith(pre)})
            ent[1] = len(d)
            if new:
                add = np.asarray(new, dtype=np.uint32)
                cur = ent[2]
                if len(cur) and int(add[0]) <= int(cur[-1]):
                    ent[2] = np.union1d(cur, add).astype(np.uint32)
                else:
                    ent[2] = np.concatenate([cur, add])
        return ent[2]

    def prefix(self, pre: str) -> list:
        return [int(q) for q in self.seqs(pre)]

    def pairs(self, pre: str) -> list:
        """[(value, seq)] for every value starting with `pre` (dead ones included), runs then delta."""
        hi = self._upper(pre)
        pb, hb = disk.enc(pre), (disk.enc(hi) if hi is not None else None)
        out = []
        for run in list(self.runs):
            i, j = self.range(run, pb, hb)
            for x in range(i, j):
                out.append((disk.dec(disk.str_at(run, x)), int(run[2][x])))
        out += [(v, q) for v, q in self.delta if v.startswith(pre)]
        return out


class _Authors:
    """Interned public keys: 32 raw bytes each in a column FILE, found by their first 8 bytes (KVRuns) -- the old
    list + dict cost ~250 bytes a key in RAM. Iterates and indexes like the list it replaces (`_authors[i]` -> hex)."""

    def __init__(self, col, runs):
        self.col, self.runs = col, runs

    def __len__(self) -> int:
        return len(self.col) // 32

    def __getitem__(self, i: int) -> str:
        n = len(self)
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError("author index out of range")
        return self.col[i * 32:i * 32 + 32].hex()

    def __iter__(self):
        raw = self.col[0:len(self.col)]
        return (raw[i:i + 32].hex() for i in range(0, len(raw), 32))

    def ix(self, pk):
        if not isinstance(pk, str) or len(pk) != 64:
            return None
        try:
            key = int(pk[:16], 16)
        except ValueError:
            return None
        for c in self.runs.get(key):
            if self.col[c * 32:c * 32 + 32].hex() == pk:
                return c
        return None

    def add(self, pk: str) -> int:
        raw = bytes.fromhex(pk) if isinstance(pk, str) and len(pk) == 64 else b""
        if len(raw) != 32 or raw.hex() != pk:
            raise ValueError("a pubkey is 64 lowercase hex characters: %r" % (pk,))
        aid = len(self)
        self.col.extend(raw)
        self.runs.add(int(pk[:16], 16), aid)
        return aid


class _AuthorIndex:
    """pubkey -> interned index, with the dict surface callers used (`get`, `in`, `[]`)."""

    def __init__(self, authors: _Authors):
        self.a = authors

    def get(self, pk, default=None):
        i = self.a.ix(pk)
        return default if i is None else i

    def __contains__(self, pk) -> bool:
        return self.a.ix(pk) is not None

    def __getitem__(self, pk) -> int:
        i = self.a.ix(pk)
        if i is None:
            raise KeyError(pk)
        return i


ORIGINS = {"direct": 0, "wot": 1, "bridge": 2, "ancestor": 3}
ORIGIN_NAMES = {v: k for k, v in ORIGINS.items()}
SEGMENT_BYTES = 256 * 1024 * 1024
DROPPED = 0xFFFFFFFF            # `seg` of a dead record whose segment was compacted away: no bytes anywhere
COMPACT_CHUNK = 4 * 1024 * 1024 # compaction writes in large sequential pieces (full RAID5 stripes, no RMW)


class StoreBusy(RuntimeError):
    """The directory is open in another Store (this process or another one)."""


def _lock_directory(path: str) -> int:
    """An exclusive, non-blocking flock on <path>/STORE.lock for as long as the Store is open.

    2026-10-16 09:36: a process that was not the relay opened server1's LIVE store directory (a default path, nothing
    to stop it), imported its index, wrote a checkpoint and run files into it and deleted index.snap -- underneath the
    running relay. Two writers on one directory is never right, so the second is refused, whoever it is. The kernel
    drops the lock when the holder exits, crashed or not, so there is nothing stale to clean up."""
    import fcntl
    fd = os.open(os.path.join(path, "STORE.lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "?"
        finally:
            os.close(fd)
        raise StoreBusy("%s is open in another PosterChanDB store (pid %s) -- refusing a second writer" % (path, holder))
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    return fd


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
        from collections import OrderedDict
        self._dec = OrderedDict()      # seq -> (decoded event, raw bytes): see get()
        self._dec_bytes = 0
        self._cols: dict = {}
        self._ckpt_id = 0
        self._run_seq = 0
        os.makedirs(path, exist_ok=True)
        self._dirlock = _lock_directory(path)
        self._reset_state()
        self._flushed = 0        # bytes of the active segment's arena that are on disk
        self._pending_n = 0
        self._last_flush = time.monotonic()
        self._active = 0
        self._file = None
        try:
            self._open()
        except BaseException:
            self._release_dirlock()
            raise

    def _release_dirlock(self) -> None:
        fd, self._dirlock = getattr(self, "_dirlock", None), None
        if fd is not None:
            try:
                os.close(fd)                 # closing the descriptor drops the flock
            except OSError:
                pass

    # the per-event columns: (name, array code). Each is a FILE (col-<name>.bin) mapped copy-on-write (disk.Column).
    COLUMNS = (("seg", "I"), ("off", "Q"), ("length", "I"), ("created", "Q"), ("kind", "I"), ("expires", "Q"),
               ("origin", "B"), ("author", "I"), ("dead", "B"), ("ids", "B"), ("pm", "Q"), ("authors", "B"))

    def _col_path(self, name: str) -> str:
        return os.path.join(self.path, "col-%s.bin" % name)

    def _next_run(self) -> int:
        self._run_seq += 1
        return self._run_seq

    def _reset_state(self, lengths: dict | None = None) -> None:
        """Every index and column EMPTY (before a full replay, and after a checkpoint that cannot be used) -- or, with
        `lengths`, the columns opened at a checkpoint's lengths (checkpoint.load fills in the rest)."""
        # Events whose SEARCH words are not indexed yet (a copy and a log replay defer them: the Postgres-exact
        # tokenizer is ~93% of the cost of storing a real event). A search catches them up first, so answers
        # stay exact; the relay store indexes the rest in the background (pcdb_store._index_words).
        self._unworded = collections.deque()
        # per-event columns (index = sequence number), in files: RAM holds what the kernel caches of them
        for c in self._cols.values():
            c.close()
        self._cols = {}
        for name, code in self.COLUMNS:
            c = disk.Column(self._col_path(name), code, (lengths or {}).get(name, 0))
            if lengths is None:
                c.reset()
            self._cols[name] = c
        (self.seg, self.off, self.length, self.created, self.kind, self.expires, self.origin, self.author, self.dead,
         self.ids, self._pmc, authors_col) = (self._cols[n] for n, _ in self.COLUMNS)
        self.arenas: dict = {}                     # segment id -> its file's bytes (resident) or None (cold)
        self.seg_size: dict[int, int] = {}         # bytes of each segment (file + unflushed tail)
        self.seg_hits: dict[int, float] = {}       # recent reads per segment (decayed by the cache governor)
        self.seg_last: dict[int, float] = {}       # last read, monotonic
        self._fds: dict[int, int] = {}             # read-only fds for cold reads
        # per-segment bookkeeping: bytes of records, bytes of dead records
        self.seg_bytes: dict[int, int] = {}
        self.seg_dead: dict[int, int] = {}
        # the OP_DEAD/OP_DERIVED payloads each segment holds, so compaction never has to read a segment back to find
        # what must be carried forward: a list for the active segment (and any just replayed), None once written to
        # the segment's `.mk` file (a closed segment's markers never change)
        self.seg_markers: dict = {}
        self._compacting = False
        self.derived: dict[int, set] = {}   # seq -> {(tag, value)}: re-emitted beside a record whenever it moves
        # id -> seq, by the id's first 8 bytes. A LIST per key: two ids can share a prefix (2^32 work to grind one on
        # purpose), and keeping one seq lost the other for good -- that event could never be found by id again
        # (dedupe, NIP-09, has_event). tests/test_pcdb_relay_store.py
        self._idk = disk.KVRuns("id")
        self._aurun = disk.KVRuns("au")
        self.idx = _Postings("idx")      # author+kind, author, kind, single-letter tags
        self.words = _Postings("w")      # search
        self.dprefix = _Prefix()         # `#d~` prefix reads
        for lv in self._leveled():
            # no checkpoints, no index files: such a store keeps its runs in RAM (and must never write over a
            # directory another store has checkpointed -- the tests open one beside the other)
            lv.dir, lv.next_id = (self.path if self.snapshots else None), self._next_run
        if lengths is None:
            # a fresh start never reuses a run file's name (one left by a checkpoint that was refused)
            nums = [int(m.group(1)) for m in (re.match(r"^run-[a-z]+-(\d+)\.", f) for f in os.listdir(self.path)) if m]
            self._run_seq = max(nums + [self._run_seq])
        self._authors = _Authors(authors_col, self._aurun)
        self._author_ix = _AuthorIndex(self._authors)

    def _leveled(self) -> list:
        return [self.idx, self.words, self.dprefix, self._idk, self._aurun]

    def _live_of(self, lv):
        """The filter a merge of `lv` applies: dead events' postings go; an id whose record was compacted away goes;
        the author lookup keeps everything. Called under the store lock (views die before it is released)."""
        if lv is self._aurun:
            return None
        if lv is self._idk:
            return lambda v: self.seg.view()[v] != DROPPED
        return lambda p: self.dead.view()[p] == 0

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

    def _open(self, force_replay: bool = False) -> None:
        t0 = time.monotonic()
        sids = self._segments()
        if not sids:
            self._new_segment(1)
            sids = [1]
        meta, how = None, "replay"
        if self.snapshots and not force_replay:
            ok, why, meta = checkpoint.load(self)
            if ok:
                how = "checkpoint"
            else:
                meta = None
                if why == "no checkpoint" and os.path.exists(os.path.join(self.path, snapshot.NAME)):
                    ok, why, meta = checkpoint.import_legacy(self)      # the index.snap of the previous format
                    how = "legacy" if ok else how
                    meta = meta if ok else None
                if meta is None:
                    if why != "no checkpoint":
                        self.log("[posterchandb] checkpoint not used: %s — replaying the whole log" % why)
                    self._reset_state()
            sids = self._segments()                   # a load removes what a finished compaction left behind
        replayed = since = 0
        for k, sid in enumerate(sids):
            p = self._seg_path(sid)
            last = sid == sids[-1]
            self.seg_bytes.setdefault(sid, 0)
            self.seg_dead.setdefault(sid, 0)
            if meta is not None and sid < meta["active"] and not last:
                # Closed and covered by the checkpoint: NOT READ. It starts cold (served by pread through the page
                # cache) and the cache governor brings it into RAM when it is actually read. Its markers are in its
                # `.mk` file (or are found by a scan the day compaction needs them).
                self.seg_size[sid] = os.path.getsize(p)
                self.arenas[sid] = None
                self.seg_markers[sid] = None
                continue
            with open(p, "rb") as f:
                data = f.read()
            if meta is not None and sid <= meta["active"]:
                # The checkpoint covers this (resident) segment up to `cover`: its frames are walked -- CRC-checked,
                # nothing decoded -- for its markers. A bad frame INSIDE the covered part means the checkpoint
                # points at bytes that are no longer what was written: it is not used, and the whole log replays
                # (which cuts the damage off, as a torn tail always was).
                cover = len(data) if sid < meta["active"] else meta["flushed"]
                ms, ok_to = self._scan_markers(data, cover, verify=True)
                if ok_to < cover:
                    self.log("[posterchandb] %s is damaged inside the checkpoint's range — replaying the whole log"
                             % os.path.basename(p))
                    del data
                    self._reset_state()
                    return self._open(force_replay=True)
                self.seg_markers[sid] = ms
            if meta is not None and sid < meta["active"]:
                good = len(data)                       # covered by the checkpoint: nothing to parse
            else:
                start = meta["flushed"] if (meta is not None and sid == meta["active"]) else None
                if not start:
                    self.seg_markers[sid] = []
                n0 = len(self.off)
                good = self._replay(sid, data, start)
                replayed += len(self.off) - n0
                since += len(self.off) - n0
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
            self.arenas[sid] = bytearray(data[:good]) if (last or self.admit(good)) else None
            del data
            if not last and since and self.snapshots:
                # A LONG REPLAY CHECKPOINTS AS IT GOES: what it built is written out segment by segment, so RAM holds
                # one segment's worth of new index at a time, and a crash part-way resumes from here.
                nxt = sids[k + 1]
                self._active, self._flushed = nxt, len(MAGIC)
                self.seg_size.setdefault(nxt, len(MAGIC))
                self._merge_all()
                while self.merge_disk_runs():
                    pass
                checkpoint.save(self)
                since = 0
        self._active = sids[-1]
        self._flushed = self.seg_size[self._active]
        self._file = open(self._seg_path(self._active), "ab")
        self._merge_all()
        self._since_snap = since
        if how == "legacy" and self.snapshots:
            checkpoint.save(self)                      # the new format exists before the old file goes
            self._since_snap = 0
            try:
                os.remove(os.path.join(self.path, snapshot.NAME))
            except OSError:
                pass
        try:
            self.last_snapshot = os.path.getmtime(os.path.join(self.path, checkpoint.NAME))
        except OSError:
            self.last_snapshot = 0.0
        self.last_open = {"snapshot": meta is not None, "how": how, "events": len(self.off), "replayed": replayed,
                          "seconds": round(time.monotonic() - t0, 3)}

    @staticmethod
    def _scan_markers(data, end: int, verify: bool = False):
        """The OP_DEAD/OP_DERIVED payloads in a segment's bytes up to `end` (frames walked, nothing decoded). With
        `verify`, every frame's CRC is checked and (markers, offset of the first bad frame or `end`) is returned."""
        out, i = [], len(MAGIC)
        end = min(end, len(data))
        mv = memoryview(data)
        while i + _FRAME.size <= end:
            n, crc = _FRAME.unpack_from(data, i)
            j = i + _FRAME.size
            if n == 0 or j + n > end:
                break
            if verify and zlib.crc32(mv[j:j + n]) & 0xFFFFFFFF != crc:
                break
            if data[j] != OP_PUT:
                out.append(bytes(data[j:j + n]))
            i = j + n
        mv.release()
        return (out, i) if verify else out

    def _mk_path(self, sid: int) -> str:
        return os.path.join(self.path, "seg-%06d.mk" % sid)

    def _write_markers(self, sid: int) -> None:
        """A CLOSED segment's markers go to its `.mk` file and leave RAM (they cannot change any more)."""
        ms = self.seg_markers.get(sid)
        if ms is None:
            return
        p = self._mk_path(sid)
        with open(p + ".tmp", "wb") as f:
            f.write(b"".join(struct.pack("<I", len(m)) + m for m in ms))
            f.flush()
            os.fsync(f.fileno())
        os.replace(p + ".tmp", p)
        self.seg_markers[sid] = None

    def _markers_of(self, sid: int) -> list:
        ms = self.seg_markers.get(sid)
        if ms is not None:
            return ms
        try:
            with open(self._mk_path(sid), "rb") as f:
                raw = f.read()
            out, i = [], 0
            while i + 4 <= len(raw):
                (n,) = struct.unpack_from("<I", raw, i)
                if i + 4 + n > len(raw):
                    raise ValueError("torn")
                out.append(raw[i + 4:i + 4 + n])
                i += 4 + n
            return out
        except (OSError, ValueError):
            with open(self._seg_path(sid), "rb") as f:     # no (good) .mk: read the segment once
                data = f.read()
            return self._scan_markers(data, len(data))

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
        _grow(arena.extend, _FRAME.pack(len(payload), zlib.crc32(payload) & 0xFFFFFFFF))
        _grow(arena.extend, payload)
        self.seg_size[self._active] = len(arena)
        self._pending_n += 1
        self._since_snap += 1
        return start

    def _merge_all(self) -> None:
        for lv in self._leveled():
            lv.merge(self._live_of(lv))

    def merge_disk_runs(self, pace=None, max_steps: int = 1) -> int:
        """Background housekeeping (maintenance): merge disk runs, STREAMED, with the lock taken only to pick a pair,
        to filter a piece, and to swap the result in. Returns how many merges ran."""
        n = 0
        for lv in self._leveled():
            for _ in range(max_steps):
                with self._lock:
                    live = self._live_of(lv)
                if not lv.disk_merge(self._lock, live, pace):
                    break
                n += 1
        return n

    def _drain(self, fsync: bool) -> int:
        """Hand the buffer to the kernel in ONE sequential write; fsync only when asked."""
        n = self._pending_n
        arena = self.arenas[self._active]
        if len(arena) > self._flushed:
            try:
                self._file.write(memoryview(arena)[self._flushed:])
                self._file.flush()
            except OSError:
                self._undo_partial_write()
                raise
            self._flushed = len(arena)
            self._pending_n = 0
        if fsync:
            os.fsync(self._file.fileno())
            try:   # the active segment is resident (its RAM copy IS the file): don't hold it twice in the page cache
                os.posix_fadvise(self._file.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
        return n

    def _undo_partial_write(self) -> None:
        """A FAILED WRITE LEAVES THE SEGMENT EXACTLY AS IT WAS (a full disk, an I/O error).

        Part of the batch may already be in the file -- and, with a buffered file object, part may still sit in
        its buffer, to be written by the next flush or by close(). Left there, every later record was appended
        AFTER a half record, and the next open's CRC check cut the whole tail away: all events flushed after the
        disk filled, lost (tests/test_posterchandb_disk_full.py measured 72 of 120). So the file is cut back to
        the last byte known good, and the file object -- buffer and all -- is replaced. The batch is still in the
        arena and `_flushed` did not move, so the next flush writes it again, whole."""
        old, path = self._file, self._seg_path(self._active)
        try:
            raw = getattr(old, "raw", None)
            fd = old.fileno()
            os.ftruncate(fd, self._flushed)
            os.fsync(fd)
            if raw is not None:
                raw.close()                # closing the raw file first discards the buffered remainder
            else:
                old.close()
        except (OSError, ValueError):
            pass
        self._file = open(path, "ab")

    def _rotate_if_full(self) -> None:
        """A full segment is closed, so compaction can later rewrite it on its own."""
        if self._flushed < self.segment_bytes:
            return
        os.fsync(self._file.fileno())
        self._file.close()
        try:
            self._write_markers(self._active)       # closed: its markers leave RAM for its .mk file
        except OSError as e:
            self.log("[posterchandb] markers of segment %d kept in RAM: %s" % (self._active, e))
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
        """Write everything buffered to the active segment and fsync. Returns records written.

        THE LOCK IS HELD ONLY FOR WHAT MUST NOT INTERLEAVE: handing the buffer to the kernel and turning the new
        postings into RAM runs. The fsync, the RAM-run merge cascade and the write of a run past `seal_at` happen
        off it (`sync`'s rule for the fsync, `disk_merge`'s for the runs) -- under it they held every reader, and a
        load test measured 45-53 s query stalls (Leveled.compact_ram). Everything is still done before this returns,
        so a caller sees the same store afterwards; one holding the lock already (a checkpoint) simply keeps it."""
        with self._lock:
            n = self._drain(fsync=False)
            for lv in self._leveled():
                lv.take_delta()
            self._last_flush = time.monotonic()
            f = self._file
            self._rotate_if_full()
        if f is not None:
            try:
                os.fsync(f.fileno())
            except (ValueError, OSError):
                if not f.closed:
                    raise                           # closed = a rotation fsynced and closed it: durable
        for lv in self._leveled():
            lv.compact_ram(self._lock, self._live_of(lv))
        return n

    def sync(self) -> int:
        """Make everything written so far durable WITHOUT the index merge a flush also does, and with the fsync
        OUTSIDE the lock: the relay's primary store calls this after each direct write and each purge, and a
        merge cascade or an fsync on a RAID5 must not hold every reader. The bytes reach the kernel under the
        lock (one sequential write); the fsync of that file then needs no lock. A rotation in between fsyncs and
        closes the file itself, which is why a closed file here is success, not an error."""
        with self._lock:
            if self._file is None:
                return 0
            n = self._drain(fsync=False)
            f = self._file
        try:
            os.fsync(f.fileno())
        except (ValueError, OSError):
            if not f.closed:
                raise
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
        if limit is None:
            with self._lock:           # everything, under the lock (a search's exact catch-up holds it already)
                q = self._unworded
                n = len(q) if limit is None else min(limit, len(q))
                batch = [q.popleft() for _ in range(n)]
                for seq in batch:
                    if self.dead[seq] or self.seg[seq] == DROPPED:
                        continue
                    for w in index_words(self.codec.decode(self._rec(seq)).get("content", "")):
                        self.words.add(_h(w), seq)
                return len(batch)
        # The background pass TOKENISES OUTSIDE THE LOCK: only reading the batch and adding its words hold it, so a
        # slow batch no longer stalls every query behind it. The batch stays queued (words_pending, and a search's
        # inline catch-up, still see it) until its words are in; if a search caught it up meanwhile, the queue no
        # longer starts with it and this batch adds nothing.
        with self._lock:
            q = self._unworded
            seqs = list(itertools.islice(q, min(limit, len(q))))
            contents = [None if (self.dead[s] or self.seg[s] == DROPPED)
                        else self.codec.decode(self._rec(s)).get("content", "") for s in seqs]
        found = [index_words(c) if c is not None else () for c in contents]
        with self._lock:
            q = self._unworded
            if not seqs or len(q) < len(seqs) or list(itertools.islice(q, len(seqs))) != seqs:
                return 0
            for _ in seqs:
                q.popleft()
            for s, ws in zip(seqs, found):
                if self.dead[s] or self.seg[s] == DROPPED:
                    continue
                for w in ws:
                    self.words.add(_h(w), s)
            return len(seqs)

    def snapshot(self):
        """A CHECKPOINT now (checkpoint.py): the log is fsynced, the RAM runs are written out, the columns' new rows are
        appended to their files and their changed pages journaled. It writes what changed since the last one, not
        the index, so it is cheap at any size. None while a compaction is running (it takes its own)."""
        with self._lock:
            if self._file is None or self._compacting:
                return None
            res = checkpoint.save(self)
            self._since_snap = 0
            self.last_snapshot = time.time()
            return res

    def close(self, take_snapshot: bool = True) -> None:
        """Flush (fsync), checkpoint, close. The checkpoint is what makes the next start replay nothing; it writes only
        what changed, so it is taken at every clean stop (`take_snapshot` is kept for callers and no longer skips
        it -- it used to, because a full snapshot did not fit systemd's 10 s, and then every deploy replayed up to a
        day of log)."""
        with self._lock:
            if self._file is None:
                return
            self.flush()
            if self.snapshots and self._since_snap > 0 and not self._compacting:
                try:
                    checkpoint.save(self)
                    self._since_snap = 0
                except OSError as e:
                    self.log("[posterchandb] checkpoint at close failed (%s) — the next start replays" % e)
            self._file.close()
            self._file = None
            for fd in self._fds.values():
                os.close(fd)
            self._fds = {}
            for c in self._cols.values():
                c.close()
            self._release_dirlock()

    def disk_bytes(self) -> int:
        return sum(self.seg_size.values())

    def stats(self) -> dict:
        with self._lock:
            return self._stats_locked()

    def _stats_locked(self) -> dict:
        dead = int(np.count_nonzero(self.dead.view())) if len(self.dead) else 0
        return {"events": len(self.off), "live": len(self.off) - dead, "dead": dead,
                "disk_bytes": self.disk_bytes(),
                "arena_bytes": self.resident_bytes(),
                "cold_segments": sum(1 for a in self.arenas.values() if a is None),
                "index_bytes": sum(lv.nbytes() for lv in self._leveled()),
                "index_ram_bytes": sum(lv.ram_bytes() for lv in self._leveled()),
                "index_disk_bytes": sum(lv.disk_bytes() for lv in self._leveled()),
                "column_anon_bytes": sum(c.anon_bytes() for c in self._cols.values()),
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

    def trim_index(self) -> int:
        """Give the index's resident pages back to the kernel (the cache governor, when the store is over its RAM
        budget or the machine is short of memory): every run mapping and column mapping. Returns how many mappings.
        The next read of a page faults it back in from the page cache or the disk -- slower, never wrong."""
        n = 0
        for lv in self._leveled():
            with self._lock:
                maps = [m for r in lv.runs if r.name is not None for m in map(disk.mapping_of, r) if m is not None]
            for m in maps:
                disk.release_mapping(m, file_backed_only=True)
                n += 1
        for c in list(self._cols.values()):
            with self._lock:
                disk.release_mapping(c._mm, file_backed_only=False)
            n += 1
        return n

    def pubkey_of(self, seq: int) -> str:
        return self._authors[self.author[seq]]

    # ---------------------------------------------------------------- ids
    def _seq_of(self, eid: str):
        if not isinstance(eid, str) or len(eid) != 64:
            return None
        try:
            key = int(eid[:16], 16)
        except ValueError:
            return None
        cands = self._idk.get(key)
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
        """Store a (signature-verified) event by the relay's ingest rules (store.py's constants: replaceable /
        addressable newest-wins with the lowest-id tie-break, NIP-09, NIP-40, retired kinds). Returns 'stored', 'duplicate', or why it was not:
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
        """Store an event EXACTLY as another store holds it -- no rule is re-decided (loading a copy, and the
        tests' fixtures): its rows are the result of the rules applied in ARRIVAL order, and re-running
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
            for s in self._live(quoting[self.kind.view(np.uint32)[quoting] == 1] if len(quoting) else []):
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
        _grow(self.seg.append, sid)
        _grow(self.off.append, off)
        _grow(self.length.append, n)
        _grow(self.ids.extend, raw_id)
        self.seg_bytes[sid] = self.seg_bytes.get(sid, 0) + n
        _grow(self.created.append, ev["created_at"])
        _grow(self.kind.append, ev["kind"])
        exp = None if int(ev["kind"]) in relay_rules._NEVER_EXPIRE_KINDS else _expiration(ev)
        _grow(self.expires.append, exp if exp and exp > 0 else 0)
        _grow(self.origin.append, origin)
        pk = ev["pubkey"]
        aid = self._authors.ix(pk)
        if aid is None:
            aid = self._authors.add(pk)
        _grow(self.author.append, aid)
        _grow(self.dead.append, 0)
        self._idk.add(int(ev["id"][:16], 16), seq)
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
            for w in index_words(ev.get("content", "")):
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
            seqs = np.nonzero(self.seg.view(np.uint32) == sid)[0]
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
                for payload in self._markers_of(sid):
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
                # A checkpoint that no longer lists the segment, THEN the file goes: a crash in between finds it
                # named in the checkpoint's `deleted` and removes it (a crash before finds the old checkpoint, whose
                # columns still point into the segment -- still there).
                if self.snapshots:
                    checkpoint.save(self, deleted=[sid])
                    self._since_snap = 0
                    self.last_snapshot = time.time()
                for f in (self._seg_path(sid), self._mk_path(sid)):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                self._fsync_dir()
                return {"compacted": sid, "moved": moved, "written": written, "carried": carried}
        finally:
            self._compacting = False

    # ---------------------------------------------------------------- aggregates (nostr_relay/aggregates.py)
    # Server Stats, the stats bot and the relay trace COUNT things; they never need the events themselves. These
    # hand out COPIES taken in short pieces under the lock, and the counting happens after it is released: one
    # 465 ms read under this lock starved every client of the relay (2026-10-09), and a stats page must never be
    # able to do that. Every answer is a superset/snapshot the caller filters with the copied columns.
    STATS_CHUNK = 262144

    def stats_columns(self, chunk: int | None = None) -> dict:
        """Copies of the per-event columns the aggregates read, plus the interned authors. Taken `chunk` events at
        a time with the lock released in between, so a copy of millions of events never holds a writer up for more
        than ~a millisecond. Seqs are stable and the columns append-only, so the pieces line up; `dead` may move
        on between pieces, which a statistic can afford (it is a snapshot either way)."""
        chunk = int(chunk or self.STATS_CHUNK)
        with self._lock:
            n = len(self.off)
        out = {k: np.empty(n, dtype=t) for k, t in (("created", np.uint64), ("kind", np.uint32),
                                                     ("origin", np.uint8), ("author", np.uint32),
                                                     ("dead", np.uint8), ("expires", np.uint64))}
        lo = 0
        while lo < n:
            hi = min(n, lo + chunk)
            with self._lock:
                if len(self.off) < hi:
                    raise RuntimeError("the store was reloaded while its columns were being copied")
                for name, col, dt in (("created", self.created, np.uint64), ("kind", self.kind, np.uint32),
                                      ("origin", self.origin, np.uint8), ("author", self.author, np.uint32),
                                      ("dead", self.dead, np.uint8), ("expires", self.expires, np.uint64)):
                    v = col.view(dt)
                    out[name][lo:hi] = v[lo:hi]
                    del v                           # no view may outlive the lock (see _grow)
            lo = hi
        with self._lock:
            out["authors"] = list(self._authors)
        out["n"] = n
        return out

    def kind_seqs(self, kind: int) -> np.ndarray:
        with self._lock:
            return np.unique(self.idx.get(_h("k:%d" % int(kind))))

    def tag_seqs(self, tag: str, value: str) -> np.ndarray:
        """Every seq carrying the single-letter tag `tag`=`value` (dead ones included: filter with the columns)."""
        with self._lock:
            return np.unique(self.idx.get(_h("t:%s:%s" % (tag, value))))

    def author_seqs(self, pubkey: str) -> np.ndarray:
        with self._lock:
            return np.unique(self.idx.get(_h("au:%s" % pubkey)))

    def d_prefix_pairs(self, pre: str) -> list:
        """[(d value, seq)] for every `d` tag starting with `pre` (dead ones included)."""
        with self._lock:
            return self.dprefix.pairs(pre)

    def columns_for(self, seqs, chunk: int = 4096) -> list:
        """[(kind, origin, created, dead, expires)] for a few seqs (one author's), read under the lock in pieces."""
        seqs = [int(s) for s in seqs]
        out = []
        for i in range(0, len(seqs), chunk):
            with self._lock:
                n = len(self.off)
                for s in seqs[i:i + chunk]:
                    if s < n:
                        out.append((self.kind[s], self.origin[s], self.created[s], self.dead[s], self.expires[s]))
        return out

    def contents(self, seqs, chunk: int = 256) -> list:
        """[(pubkey, content)] for `seqs`, decoded a few hundred at a time under the lock."""
        seqs = [int(s) for s in seqs]
        out = []
        for i in range(0, len(seqs), chunk):
            with self._lock:
                for s in seqs[i:i + chunk]:
                    if self.dead[s] or self.seg[s] == DROPPED:
                        continue
                    ev = self.get(s)
                    out.append((ev["pubkey"], ev.get("content") or ""))
        return out

    # ---------------------------------------------------------------- read
    def get(self, seq: int) -> dict:
        return self.codec.decode(self._rec(seq))

    def _get_cached(self, seq: int) -> dict:
        """The event at `seq`, for a QUERY ANSWER: decoded once and kept (bounded by raw bytes, oldest first out): the same popular events
        -- contact lists of thousands of p-tags, profiles, feed tops -- are in answer after answer, and decoding them
        each time was a tenth of the relay's CPU (py-spy, 2026-10-16). An event never changes, so its seq names one
        dict; each caller gets its own shallow copy, so one that adds a key cannot change it for the next."""
        with self._lock:
            hit = self._dec.get(seq)
            if hit is not None:
                self._dec.move_to_end(seq)
                sid = self.seg[seq]                 # still a read of that segment, for the RAM governor (cache.py)
                self.seg_hits[sid] = self.seg_hits.get(sid, 0) + 1
                self.seg_last[sid] = time.monotonic()
                return dict(hit[0])
            rec = self._rec(seq)
            ev = self.codec.decode(rec)
            n = len(rec)
            self._dec[seq] = (ev, n)
            self._dec_bytes += n
            while self._dec and self._dec_bytes > _DECODED_CACHE_BYTES:
                _k, (_e, m) = self._dec.popitem(last=False)
                self._dec_bytes -= m
            return dict(ev)

    def seq_of(self, eid: str):
        with self._lock:
            return self._seq_of(eid)

    def query(self, flt: dict, now: int | None = None) -> list:
        # The work runs in a helper so every numpy view it makes dies BEFORE the lock is released (see _grow).
        with self._lock:
            return self._query_locked(flt, now)

    def _prefmax(self, n: int) -> np.ndarray:
        """max(created[0..i]) for every seq i < n, kept incrementally in its own column (append-only columns)."""
        pm = self._pmc
        if len(pm) > n:
            pm.truncate(n)
        m = len(pm)
        if m < n:
            created = self.created.view()[:n]
            tail = np.maximum.accumulate(created[m:n])
            if m:
                np.maximum(tail, np.uint64(pm[m - 1]), out=tail)
            del created
            pm.write_range(m, tail)
        return pm.view()[:n]

    def _newest_first(self, flt: dict, kinds, n: int, now: int) -> np.ndarray:
        """Candidate seqs for a filter with nothing selective, found newest-first. A superset of the answer: the
        caller filters and orders as for any other plan. It stops once the `limit`-th newest match is STRICTLY newer
        than everything not yet looked at (the running max proves it), so a tie on the timestamp -- which the id
        decides -- can never be cut off."""
        limit = max(1, min(int(flt.get("limit") or 500), 5000))
        pm = self._prefmax(n)
        created = self.created.view(np.uint64)[:n]
        dead = self.dead.view(np.uint8)[:n]
        expires = self.expires.view(np.uint64)[:n]
        kind_col = self.kind.view(np.uint32)[:n]
        kinds_arr = np.asarray([int(k) for k in kinds], dtype=np.uint32) if kinds else None
        since = int(flt["since"]) if flt.get("since") is not None else None
        until = int(flt["until"]) if flt.get("until") is not None else None
        picks, total, hi = [], 0, n
        while hi > 0:
            lo = max(0, hi - _SCAN_CHUNK)
            c = created[lo:hi]
            m = dead[lo:hi] == 0
            e = expires[lo:hi]
            m &= (e == 0) | (e > now)
            if kinds_arr is not None:
                m &= np.isin(kind_col[lo:hi], kinds_arr)
            if since is not None:
                m &= c >= since
            if until is not None:
                m &= c <= until
            got = np.nonzero(m)[0].astype(np.uint32) + np.uint32(lo)
            if len(got):
                picks.append(got)
                total += len(got)
            hi = lo
            if hi and since is not None and int(pm[hi - 1]) < since:
                break                                   # everything older is older than `since`: nothing left to find
            if hi and total >= limit:
                allc = created[np.concatenate(picks)]
                kth = int(np.partition(allc, len(allc) - limit)[len(allc) - limit])
                bound = int(pm[hi - 1])
                if until is not None:
                    bound = min(bound, until)
                if since is not None and bound < since:
                    break                           # nothing older can match at all
                if kth > bound:
                    break
            elif hi and since is not None and int(pm[hi - 1]) < since:
                break
        del created, dead, expires, kind_col, pm
        if not picks:
            return np.zeros(0, dtype=np.uint32)
        out = np.concatenate(picks)
        out.sort()
        return out

    def _query_locked(self, flt: dict, now: int | None = None) -> list:
        """One NIP-01 filter (+ NIP-50 `search`), exactly as the relay's _query_one answers it:
        ORDER BY created_at DESC, id DESC, `limit or 500` clamped to 1..5000, dead and expired never returned.

        Planning: only the SELECTIVE parts of a filter (ids, authors, tags, `#d~`, search words) are turned
        into posting lists, intersected smallest-first by binary search; `kinds` with anything selective is
        a column check on what is left, never the union of every event of those kinds. Ordering uses the
        created_at column and the in-RAM ids, so only the events actually returned are decoded."""
        now = int(time.time()) if now is None else now
        with self._lock:
            cand = self._match_locked(flt, now, exhaustive=False)
            if not len(cand):
                return []
            created = self.created.view(np.uint64)
            # The relay's own rule (store.py _query_one): `limit or 500`, clamped to 1..5000.
            limit = max(1, min(int(flt.get("limit") or 500), 5000))
            c = created[cand]
            if len(cand) > limit:
                # keep every event at the cut-off timestamp: the id decides among them below
                cut = np.partition(c, len(c) - limit)[len(c) - limit]
                sel = c >= cut
                cand, c = cand[sel], c[sel]
            # ORDER BY created_at DESC, id DESC — the id as four big-endian words is its exact hex order
            idw = self.ids.view(">u8").reshape(-1, 4)[cand]
            order = np.lexsort((idw[:, 3], idw[:, 2], idw[:, 1], idw[:, 0], c))[::-1][:limit]
            # Answers read through the decoded-event cache; scans and cleaners use get() and never flood it.
            return [self._get_cached(int(s)) for s in cand[order]]

    def match(self, flt: dict, now: int | None = None) -> np.ndarray:
        """EVERY live, unexpired seq the filter matches (no `limit`), ascending -- for COUNT and negentropy, which
        the relay answers over the whole match. A filter with nothing selective unions whole kind posting lists
        here: callers that care about lock holds (nostr_relay/pcdb_store.py) scan the columns in chunks instead."""
        now = int(time.time()) if now is None else now
        with self._lock:
            return self._match_locked(flt, now, exhaustive=True).copy()

    def _match_locked(self, flt: dict, now: int, exhaustive: bool) -> np.ndarray:
        """The candidate seqs of one filter after every condition but the limit (dead/expired/since/until/cursor
        applied). `exhaustive=False` lets a filter with nothing selective stop at the newest `limit` (the query)."""
        _none = np.zeros(0, dtype=np.uint32)
        with self._lock:
            n = len(self.off)
            if not n:
                return _none
            sets = []
            ids = flt.get("ids")
            if ids:
                got = [self._seq_of(i) for i in ids if isinstance(i, str) and len(i) == 64]
                sets.append(np.unique(np.array([x for x in got if x is not None], dtype=np.uint32)))
            authors, kinds = flt.get("authors"), flt.get("kinds")
            kinds_done = False
            # A feed of 4,109 followed accounts sorted its whole candidate list (np.unique) on EVERY request:
            # 197 ms with kinds, 1.6 s without, measured on the production copy 2026-10-16 -- the relay's main CPU.
            # Per-author (and per-author-and-kind) posting lists are DISJOINT once the keys are unique (an event has
            # one author and one kind), so when they are the only selective part and the caller re-orders by time
            # anyway (`query`, not the ascending `match`), concatenating them is the whole union.
            alone = not exhaustive and not ids and not flt.get("search") and \
                not any(isinstance(k, str) and k.startswith("#") and v for k, v in flt.items())
            if authors:
                authors = list(dict.fromkeys(authors))
            if authors and kinds:
                kk = list(dict.fromkeys(int(k) for k in kinds))
                got = self.idx.get_many([_h("ak:%s:%d" % (a, k)) for a in authors for k in kk])
                sets.append(got if alone else _union([got]))
                kinds_done = True
            elif authors:
                got = self.idx.get_many([_h("au:%s" % a) for a in authors])
                sets.append(got if alone else _union([got]))
            for key, vals in flt.items():
                if not (isinstance(key, str) and key.startswith("#") and vals):
                    continue
                if len(key) == 2:
                    tags = [key[1]] + (["_quote_author"] if key == "#p" and flt.get("_include_quotes") is True else [])
                    sets.append(_union([self.idx.get(_h("t:%s:%s" % (tg, v))) for tg in tags for v in vals]))
                elif len(key) == 3 and key.endswith("~"):
                    if key[1] != "d":
                        return _none          # prefix matching is indexed for `d` only — the app's one use
                    got = [self.dprefix.seqs(str(v)) for v in vals]
                    sets.append(got[0] if len(got) == 1 else np.unique(np.concatenate(got)).astype(np.uint32))
            if flt.get("search"):
                if self._unworded:
                    self.index_pending_words()     # exact answers: catch up whatever a copy/replay deferred
                words = search_words(flt["search"])
                if not words:
                    return _none           # plainto_tsquery with no lexemes matches nothing
                for w in words:
                    sets.append(_union([self.words.get(_h(w))]))
            kind_col = self.kind.view(np.uint32)
            if sets:
                sets.sort(key=len)
                cand = sets[0]
                for other in sets[1:]:
                    if not len(cand):
                        break
                    cand = _intersect(cand, other)
                if kinds and not kinds_done and len(cand):
                    cand = cand[np.isin(kind_col[cand], np.asarray([int(k) for k in kinds], dtype=np.uint32))]
            elif not exhaustive and not isinstance(flt.get("_cursor"), list) and kinds and \
                    sum(self.idx.count(_h("k:%d" % int(k))) for k in kinds) <= _RARE_KIND_POSTINGS:
                # A RARE kind is answered from its own posting list. The newest-first walk below never reaches
                # `limit` matches for one, so it scanned the WHOLE store per request: {"kinds":[54],"since":...}
                # polled by a client kept the relay over a core (py-spy, server1, 2026-10-16).
                cand = _union([self.idx.get(_h("k:%d" % int(k))) for k in kinds])
            elif not exhaustive and not isinstance(flt.get("_cursor"), list):
                # Nothing selective (only `kinds`, or no filter at all): walk newest-first and stop as soon as the
                # newest `limit` are certain. Building the union of every kind-1 and kind-6 posting and sorting it
                # cost 465 ms at 2.6M events under the one store lock -- the global feed every client asks for --
                # and starved the relay (2026-10-09, serve mode).
                cand = self._newest_first(flt, kinds, n, now)
            elif kinds:
                cand = _union([self.idx.get(_h("k:%d" % int(k))) for k in kinds])
            else:
                cand = np.arange(n, dtype=np.uint32)
            if not len(cand):
                return _none
            created = self.created.view(np.uint64)
            expires = self.expires.view(np.uint64)
            dead = self.dead.view(np.uint8)
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
            return cand[keep]


_SCAN_CHUNK = 65536
_DECODED_CACHE_BYTES = 48 * 1024 * 1024   # raw record bytes of decoded events kept for get()
_RARE_KIND_POSTINGS = 50000   # at most this many events of the requested kinds: read their index, never walk the store


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


def _concat(arrs) -> np.ndarray:
    """The union of posting lists known to be DISJOINT, in no particular order (no sort, no de-duplication)."""
    arrs = [a for a in arrs if len(a)]
    if not arrs:
        return np.zeros(0, dtype=np.uint32)
    return arrs[0] if len(arrs) == 1 else np.concatenate(arrs)


def _intersect(small: np.ndarray, big: np.ndarray) -> np.ndarray:
    """Both sorted and unique: binary-search the smaller in the larger (m log n, no sort of either)."""
    if len(small) > len(big):
        small, big = big, small
    if not len(small) or not len(big):
        return small[:0]
    pos = np.searchsorted(big, small)
    pos[pos >= len(big)] = len(big) - 1
    return small[big[pos] == small]
