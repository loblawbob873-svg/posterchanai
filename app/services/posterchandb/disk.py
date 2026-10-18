"""PosterChanDB's index ON DISK: per-event columns and sorted runs that live in files and are only CACHED in RAM.

Why (2026-10-16): every structure that grows with the number of events used to be a Python/numpy object in RAM, so a
store's RAM was ~200 bytes an event on top of the segment cache, and every snapshot rewrote all of it. A 200 GB store
(~260M events) needed ~50 GB of RAM before a byte of cache and a ~50 GB write per snapshot. Here:

  * a COLUMN is one file mapped copy-on-write (MAP_PRIVATE). Reading it is reading the page cache: the kernel keeps the
    pages a query touches and drops the rest under pressure, exactly as it does for the segment files. Writes land in
    private copies that NEVER reach the file on their own -- only a checkpoint writes them, after the log they derive
    from is fsynced, so a column file can never hold a state the log cannot reproduce. A checkpoint appends the tail
    (rows added since the last one) beyond the old length, and journals the few changed pages below it (a kill, a
    compaction's relocation) before writing them in place; then the mapping is rebuilt and the private copies are gone.
  * a RUN (postings, the id lookup, the `#d~` prefix index, the author lookup) is a set of immutable files, one per
    array, mapped read-only. Small runs are built in RAM as before; at a checkpoint, or once one is big, it is written
    out ("sealed") and mapped. Disk runs are merged in the background by STREAMING -- a key range at a time, so a merge
    never holds more than a chunk in RAM however large the runs are.

The structures keep their old in-RAM shapes (a column indexes like an array, a run is a tuple of numpy arrays), so the
store's query code reads them unchanged.
"""
from __future__ import annotations

import bisect
import mmap
import os

import numpy as np

PAGE = mmap.PAGESIZE
_MIN_CAP = 1 << 16                  # bytes: a column's first mapping
_GROW_STEP = 1 << 30                # past 1 GB a column grows a GB at a time (not doubling)
CHUNK = 1 << 20                     # rows per piece of a streaming merge
CHUNK_POSTINGS = 1 << 22            # postings per piece of a streaming merge (a hot key is split across pieces)
STR_CHUNK_BYTES = 32 << 20          # the fixed-width matrix a string-merge piece may build


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def pwrite_all(fd: int, data, off: int) -> None:
    mv = memoryview(data)
    try:
        while len(mv):
            n = os.pwrite(fd, mv, off)
            if n <= 0:
                raise OSError("short write")
            mv, off = mv[n:], off + n
    finally:
        mv.release()


# ------------------------------------------------------------------------------------------------ columns
class Column:
    """A typed, append-mostly array in its own file, mapped copy-on-write (see the module docstring).

    Indexing, `append`, `extend` (bytes columns) and `len` behave like the `array.array`/`bytearray` it replaces;
    `view(dtype)` is the numpy view the old code made with `np.frombuffer(column, dtype)` -- exactly `len` rows, and like
    any view of a store column it must not outlive the store lock (a growth or a checkpoint replaces the mapping; the
    old one stays valid for a view still holding it and is unmapped when that view dies)."""

    _CODES = {"B": np.uint8, "I": np.uint32, "Q": np.uint64}

    def __init__(self, path: str, code: str, length: int = 0):
        self.path, self.code = path, code
        self.dtype = np.dtype(self._CODES[code])
        self.isz = self.dtype.itemsize
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        size = os.fstat(self.fd).st_size
        if size < length * self.isz:
            os.close(self.fd)
            raise ValueError("column file %s is shorter than its checkpoint" % os.path.basename(path))
        self.n = self.ckpt_n = int(length)
        self.dirty: set = set()        # pages below ckpt_n*isz changed since the checkpoint
        self._mm = self._mv = None
        self.cap = 0
        self._remap(max(size, length * self.isz, _MIN_CAP))

    # -- mapping
    def _remap(self, cap_bytes: int) -> None:
        cap_bytes = -(-int(cap_bytes) // PAGE) * PAGE
        if os.fstat(self.fd).st_size < cap_bytes:
            os.ftruncate(self.fd, cap_bytes)          # sparse: nothing is written
        mm = mmap.mmap(self.fd, cap_bytes, access=mmap.ACCESS_COPY)
        old_mm, old_mv = self._mm, self._mv
        if old_mm is not None:
            # carry the private (not yet checkpointed) state over: the tail, and changed pages below it
            lo, hi = self.ckpt_n * self.isz, self.n * self.isz
            if hi > lo:
                mm[lo:hi] = old_mm[lo:hi]
            for p in self.dirty:
                a = p * PAGE
                b = min(a + PAGE, lo)
                if b > a:
                    mm[a:b] = old_mm[a:b]
            old_mv.release()
            try:
                old_mm.close()
            except BufferError:
                pass                                   # a reader's view holds it: unmapped when that view dies
        self._mm, self._mv = mm, memoryview(mm).cast(self.code)
        self.cap = cap_bytes // self.isz

    def _grow(self, need: int) -> None:
        cap = self.cap * self.isz
        while cap < need * self.isz:
            cap = cap * 2 if cap < _GROW_STEP else cap + _GROW_STEP
        self._remap(cap)

    # -- the array.array surface
    def __len__(self) -> int:
        return self.n

    def __getitem__(self, i):
        if isinstance(i, slice):
            a, b, step = i.indices(self.n)
            if step != 1:
                raise ValueError("Column slices take no step")
            return self._mm[a * self.isz:max(a, b) * self.isz] if self.isz == 1 else \
                self.view()[a:b].tolist()
        if i < 0:
            i += self.n
        if not 0 <= i < self.n:
            raise IndexError("column index out of range")
        return self._mv[i]

    def __setitem__(self, i: int, v) -> None:
        if i < 0:
            i += self.n
        if not 0 <= i < self.n:
            raise IndexError("column index out of range")
        self._mv[i] = v
        if i < self.ckpt_n:
            self.dirty.add(i * self.isz // PAGE)

    def append(self, v) -> None:
        n = self.n
        if n >= self.cap:
            self._grow(n + 1)
        self._mv[n] = v
        self.n = n + 1

    def extend(self, data) -> None:
        """Append raw bytes (a bytes column) or an iterable of values."""
        if self.isz == 1 and isinstance(data, (bytes, bytearray, memoryview)):
            raw = bytes(data)
        else:
            raw = np.asarray(list(data) if not isinstance(data, np.ndarray) else data, dtype=self.dtype).tobytes()
        k = len(raw) // self.isz
        if self.n + k > self.cap:
            self._grow(self.n + k)
        self._mm[self.n * self.isz:(self.n + k) * self.isz] = raw
        self.n += k

    def view(self, dtype=None) -> np.ndarray:
        dt = np.dtype(dtype) if dtype is not None else self.dtype
        return np.frombuffer(self._mm, dtype=dt, count=(self.n * self.isz) // dt.itemsize)

    def write_range(self, start: int, values: np.ndarray) -> None:
        """values -> rows [start, start+len): may extend the column; changed rows below the checkpoint are tracked."""
        values = np.asarray(values, dtype=self.dtype)
        end = start + len(values)
        if end > self.cap:
            self._grow(end)
        if start < self.ckpt_n:
            for p in range(start * self.isz // PAGE, (min(end, self.ckpt_n) * self.isz - 1) // PAGE + 1):
                self.dirty.add(p)
        self._mm[start * self.isz:end * self.isz] = values.tobytes()
        self.n = max(self.n, end)

    def truncate(self, n: int) -> None:
        """Forget rows >= n (logically: the file is rewritten from there at the next checkpoint)."""
        self.n = min(self.n, int(n))
        if self.ckpt_n > self.n:
            self.ckpt_n = self.n
            self.dirty = {p for p in self.dirty if p * PAGE < self.ckpt_n * self.isz}

    def reset(self) -> None:
        self.n = self.ckpt_n = 0
        self.dirty.clear()
        self._remap(_MIN_CAP)

    # -- checkpoints (checkpoint.py drives these, holding the store lock, after the log is fsynced)
    def tail_bytes(self) -> int:
        return (self.n - self.ckpt_n) * self.isz

    def write_tail(self) -> int:
        lo, hi = self.ckpt_n * self.isz, self.n * self.isz
        if hi > lo:
            mv = memoryview(self._mm)
            try:
                pwrite_all(self.fd, mv[lo:hi], lo)
            finally:
                mv.release()
        return hi - lo

    def journal_entries(self) -> list:
        lim = self.ckpt_n * self.isz
        out = []
        for p in sorted(self.dirty):
            a = p * PAGE
            b = min(a + PAGE, lim)
            if b > a:
                out.append((a, self._mm[a:b]))
        return out

    def sync(self) -> None:
        os.fsync(self.fd)

    def committed(self) -> None:
        """The checkpoint that holds every row is on disk: drop the private copies (rebuild the mapping)."""
        self.ckpt_n = self.n
        self.dirty.clear()
        self._remap(self.cap * self.isz)

    def anon_bytes(self) -> int:
        return self.tail_bytes() + len(self.dirty) * PAGE

    def close(self) -> None:
        if self._mv is not None:
            self._mv.release()
            try:
                self._mm.close()
            except BufferError:
                pass
            self._mm = self._mv = None
        try:
            os.close(self.fd)
        except OSError:
            pass


# ------------------------------------------------------------------------------------------------ runs
class Run(tuple):
    """A sorted run: a tuple of numpy arrays, plus `name` when it lives in files (None: built in RAM)."""

    def __new__(cls, arrays, name=None):
        r = tuple.__new__(cls, arrays)
        r.name = name
        return r

    @property
    def nbytes(self) -> int:
        return sum(a.nbytes for a in self)


def run_files(dirpath: str, name: str, k: int) -> list:
    return [os.path.join(dirpath, "run-%s.%d.bin" % (name, i)) for i in range(k)]


def map_run(dirpath: str, name: str, dtypes) -> Run:
    arrays = []
    for path, dt in zip(run_files(dirpath, name, len(dtypes)), dtypes):
        size = os.path.getsize(path)
        if size % np.dtype(dt).itemsize:
            raise ValueError("run file %s is torn" % os.path.basename(path))
        if size == 0:
            arrays.append(np.zeros(0, dtype=dt))
            continue
        with open(path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        arrays.append(np.frombuffer(mm, dtype=dt))
    return Run(arrays, name)


class RunSink:
    """Where a merge writes: arrays appended piece by piece, then `finish()` gives the Run. In RAM (dirpath None) the
    pieces are concatenated; on disk each array streams into its own file (fsynced, renamed, then mapped)."""

    def __init__(self, dtypes, dirpath: str | None = None, name: str | None = None):
        self.dtypes = [np.dtype(d) for d in dtypes]
        self.dirpath, self.name = dirpath, name
        if dirpath is None:
            self.parts = [[] for _ in self.dtypes]
        else:
            self.paths = run_files(dirpath, name, len(self.dtypes))
            self.files = [open(p + ".tmp", "wb") for p in self.paths]
        self.count = [0] * len(self.dtypes)

    def put(self, i: int, arr) -> None:
        arr = np.ascontiguousarray(arr, dtype=self.dtypes[i])
        if not len(arr):
            return
        self.count[i] += len(arr)
        if self.dirpath is None:
            self.parts[i].append(arr)
        else:
            self.files[i].write(arr.tobytes())

    def finish(self) -> Run:
        if self.dirpath is None:
            return Run([np.concatenate(p) if len(p) > 1 else (p[0].copy() if p else np.zeros(0, dt))
                        for p, dt in zip(self.parts, self.dtypes)])
        for f in self.files:
            f.flush()
            os.fsync(f.fileno())
            f.close()
        for p in self.paths:
            os.replace(p + ".tmp", p)
        fsync_dir(self.dirpath)
        return map_run(self.dirpath, self.name, self.dtypes)

    def abort(self) -> None:
        if self.dirpath is not None:
            for f, p in zip(self.files, self.paths):
                f.close()
                try:
                    os.remove(p + ".tmp")
                except OSError:
                    pass


def remove_run(dirpath: str, name: str, k: int) -> None:
    for p in run_files(dirpath, name, k):
        for q in (p, p + ".tmp"):
            try:
                os.remove(q)
            except OSError:
                pass


def write_run(run, dtypes, dirpath: str, name: str) -> Run:
    s = RunSink(dtypes, dirpath, name)
    try:
        for i, a in enumerate(run):
            for lo in range(0, len(a), CHUNK * 4):
                s.put(i, a[lo:lo + CHUNK * 4])
        return s.finish()
    except BaseException:
        s.abort()
        raise


# -- postings: (keys u64 sorted unique, starts i64 len+1, post u32) ------------------------------------
POSTING_DTYPES = (np.uint64, np.int64, np.uint32)


def merge_postings_mem(ka, sa, pa, kb, sb, pb, live=None):
    """Merge two CSR runs in RAM. Every posting in run B is NEWER than every posting in run A (sequence numbers only
    grow), so per key the result is simply A's list then B's: a STABLE sort on keys alone, never a re-sort of postings.
    `live(postings) -> bool mask` drops postings of dead events (a key left with none disappears)."""
    ka_rep = np.repeat(ka, np.diff(sa))
    kb_rep = np.repeat(kb, np.diff(sb))
    allk = np.concatenate([ka_rep, kb_rep])
    allp = np.concatenate([pa, pb])
    if live is not None and len(allp):
        keep = live(allp)
        allk, allp = allk[keep], allp[keep]
    order = np.argsort(allk, kind="stable")
    allk, allp = allk[order], allp[order]
    uk, first = np.unique(allk, return_index=True)
    return uk, np.append(first, len(allk)).astype(np.int64), allp


def merge_postings(a, b, sink: RunSink, live=None, pace=None) -> Run:
    """Merge two posting runs into `sink` a bounded piece at a time (key ranges; a key with more postings than a piece
    is streamed on its own), whatever their size. A's postings before B's for every key, as merge_postings_mem."""
    ka, sa, pa = a
    kb, sb, pb = b
    na, nb = len(ka), len(kb)
    i = j = 0
    base = 0                         # postings written so far

    def emit(keys, starts, post):
        nonlocal base
        sink.put(0, keys)
        sink.put(1, np.asarray(starts[:-1], dtype=np.int64) + base)
        sink.put(2, post)
        base += len(post)

    def stream_key(key):
        """One key whose postings exceed a piece: A's slice then B's, filtered in pieces."""
        nonlocal i, j, base
        total = 0
        for (k, s, p, idx) in ((ka, sa, pa, i), (kb, sb, pb, j)):
            if idx < len(k) and k[idx] == key:
                lo, hi = int(s[idx]), int(s[idx + 1])
                for x in range(lo, hi, CHUNK_POSTINGS):
                    piece = np.asarray(p[x:min(hi, x + CHUNK_POSTINGS)])
                    if live is not None and len(piece):
                        piece = piece[live(piece)]
                    sink.put(2, piece)
                    total += len(piece)
                    if pace:
                        pace(piece.nbytes)
        if total:
            sink.put(0, np.asarray([key], dtype=np.uint64))
            sink.put(1, np.asarray([base], dtype=np.int64))
            base += total
        if i < na and ka[i] == key:
            i += 1
        if j < nb and kb[j] == key:
            j += 1

    while i < na or j < nb:
        # the piece: up to CHUNK keys and CHUNK_POSTINGS postings of A, and B's keys below A's boundary (bounded too)
        if i < na:
            i2 = min(na, i + CHUNK, int(np.searchsorted(sa, sa[i] + CHUNK_POSTINGS, "right")) - 1)
            if i2 <= i:                                  # A's next key alone exceeds a piece
                key = ka[i]
                if j < nb and kb[j] < key:
                    i2 = i                               # B's smaller keys first (below)
                else:
                    stream_key(key)
                    continue
            bound = ka[i2] if i2 < na else None
        else:
            i2, bound = na, None
        j2 = nb if bound is None else int(np.searchsorted(kb, bound, "left"))
        if j < nb:
            jcap = min(nb, j + CHUNK, int(np.searchsorted(sb, sb[j] + CHUNK_POSTINGS, "right")) - 1)
            if jcap < j2:
                if jcap <= j:                            # B's next key alone exceeds a piece
                    key = kb[j]
                    if i < na and ka[i] < key:
                        j2 = j
                        bound = key
                        i2 = int(np.searchsorted(ka, key, "left"))
                    else:
                        stream_key(key)
                        continue
                else:
                    j2, bound = jcap, kb[jcap]
                    i2 = min(i2, int(np.searchsorted(ka, bound, "left")))
        if i2 <= i and j2 <= j:
            # both heads are the same key, too big for a piece together
            stream_key(min(ka[i] if i < na else np.uint64(-1), kb[j] if j < nb else np.uint64(-1)))
            continue
        a0, a1, b0, b1 = int(sa[i]), int(sa[i2]), int(sb[j]), int(sb[j2])
        uk, st, post = merge_postings_mem(np.asarray(ka[i:i2]), np.asarray(sa[i:i2 + 1]) - a0, np.asarray(pa[a0:a1]),
                                          np.asarray(kb[j:j2]), np.asarray(sb[j:j2 + 1]) - b0, np.asarray(pb[b0:b1]),
                                          live)
        emit(uk, st, post)
        if pace:
            pace(post.nbytes + uk.nbytes * 2)
        i, j = i2, j2
    sink.put(1, np.asarray([base], dtype=np.int64))
    return sink.finish()


# -- key/value: (keys u64 sorted, vals u32), duplicates allowed -----------------------------------------
KV_DTYPES = (np.uint64, np.uint32)


def merge_kv(a, b, sink: RunSink, keep=None, pace=None) -> Run:
    """Merge two (key, value) runs, A's entries before B's for equal keys; `keep(values) -> bool mask` filters."""
    ka, va = a
    kb, vb = b
    na, nb = len(ka), len(kb)
    i = j = 0
    while i < na or j < nb:
        i2 = min(na, i + CHUNK)
        if i2 < na:
            bound = ka[i2]
            i2 = int(np.searchsorted(ka, bound, "left", sorter=None)) if ka[i2 - 1] == bound else i2
            if i2 <= i:
                i2 = int(np.searchsorted(ka, bound, "right"))
                bound = ka[i2] if i2 < na else None
        else:
            bound = None
        j2 = nb if bound is None else int(np.searchsorted(kb, bound, "left"))
        if j2 - j > CHUNK and j + CHUNK < nb:
            bound = kb[j + CHUNK]
            j2 = int(np.searchsorted(kb, bound, "left"))
            if j2 <= j:
                j2 = int(np.searchsorted(kb, bound, "right"))
                bound = kb[j2] if j2 < nb else None
            i2 = min(i2, na if bound is None else int(np.searchsorted(ka, bound, "left")))
        k = np.concatenate([np.asarray(ka[i:i2]), np.asarray(kb[j:j2])])
        v = np.concatenate([np.asarray(va[i:i2]), np.asarray(vb[j:j2])])
        if keep is not None and len(v):
            m = keep(v)
            k, v = k[m], v[m]
        o = np.argsort(k, kind="stable")
        sink.put(0, k[o])
        sink.put(1, v[o])
        if pace:
            pace(k.nbytes + v.nbytes)
        i, j = i2, j2
    return sink.finish()


def kv_get(run, key: int) -> list:
    keys, vals = run
    ku = np.uint64(key)
    lo = int(np.searchsorted(keys, ku, "left"))
    if lo < len(keys) and keys[lo] == ku:
        hi = int(np.searchsorted(keys, ku, "right"))
        return [int(x) for x in vals[lo:hi]]
    return []


def kv_from_pairs(pairs) -> Run:
    """[(key, value), ...] in arrival order -> a sorted run (equal keys keep arrival order)."""
    n = len(pairs)
    k = np.fromiter((p[0] for p in pairs), dtype=np.uint64, count=n)
    v = np.fromiter((p[1] for p in pairs), dtype=np.uint32, count=n)
    o = np.argsort(k, kind="stable")
    return Run([k[o], v[o]])


# -- strings: (blob u8, offs i64 len+1, seqs u32) sorted by (value bytes, seq) ---------------------------
STR_DTYPES = (np.uint8, np.int64, np.uint32)


def enc(s: str) -> bytes:
    return s.encode("utf-8", "surrogatepass")


def dec(b: bytes) -> str:
    return b.decode("utf-8", "surrogatepass")


def str_at(run, i: int) -> bytes:
    blob, offs, _ = run
    return blob[int(offs[i]):int(offs[i + 1])].tobytes()


def str_lower(run, value: bytes, seq: int = -1, lo: int = 0, hi: int | None = None) -> int:
    """First index whose (value, seq) is >= (value, seq) -- a binary search reading only log(n) entries."""
    seqs = run[2]
    hi = len(seqs) if hi is None else hi
    while lo < hi:
        mid = (lo + hi) // 2
        v = str_at(run, mid)
        if v < value or (v == value and int(seqs[mid]) < seq):
            lo = mid + 1
        else:
            hi = mid
    return lo


def str_from_pairs(pairs) -> Run:
    """[(value str, seq)] -> a sorted string run."""
    items = sorted((enc(v), q) for v, q in pairs)
    blob = b"".join(v for v, _ in items)
    offs = np.zeros(len(items) + 1, dtype=np.int64)
    if items:
        np.cumsum(np.fromiter((len(v) for v, _ in items), dtype=np.int64, count=len(items)), out=offs[1:])
    return Run([np.frombuffer(blob, dtype=np.uint8).copy(), offs,
                np.fromiter((q for _, q in items), dtype=np.uint32, count=len(items))])


def _fixed(run, i: int, j: int):
    """Entries [i, j) as a fixed-width byte-string array + their true lengths (ordering: S-array, then length)."""
    blob, offs, _ = run
    o = np.asarray(offs[i:j + 1], dtype=np.int64)
    lens = np.diff(o)
    width = max(1, int(lens.max()) if len(lens) else 1)
    if not len(lens):
        return np.zeros(0, dtype="S%d" % width), lens
    col = np.arange(width, dtype=np.int64)
    mask = col[None, :] < lens[:, None]
    idx = (o[:-1, None] + col[None, :])[mask]
    mat = np.zeros((len(lens), width), dtype=np.uint8)
    mat[mask] = np.asarray(blob[int(o[0]):int(o[-1])])[idx - int(o[0])]
    return mat.view("S%d" % width).ravel(), lens


def merge_strings(a, b, sink: RunSink, live=None, pace=None) -> Run:
    """Merge two string runs by (value bytes, seq) a bounded piece at a time; `live(seqs) -> mask` filters."""
    na, nb = len(a[2]), len(b[2])
    i = j = 0
    base = 0

    def piece_len(run, start, n):
        if start >= n:
            return start
        offs = run[1]
        # as many entries as fit the matrix budget at the widest value among them
        end = min(n, start + max(1, CHUNK // 16))
        while end > start + 1:
            w = int(np.max(np.diff(np.asarray(offs[start:end + 1]))))
            if (end - start) * max(1, w) <= STR_CHUNK_BYTES:
                break
            end = start + max(1, (end - start) // 2)
        return end

    while i < na or j < nb:
        i2 = piece_len(a, i, na)
        if i2 < na:
            pv, ps = str_at(a, i2), int(a[2][i2])
            j2 = str_lower(b, pv, ps, j)
        else:
            j2 = nb
        jcap = piece_len(b, j, nb)
        if j2 > jcap:
            j2 = jcap
            pv, ps = str_at(b, j2), int(b[2][j2])
            i2 = str_lower(a, pv, ps, i, i2)
        sa_, la = _fixed(a, i, i2)
        sb_, lb = _fixed(b, j, j2)
        w = max(sa_.dtype.itemsize, sb_.dtype.itemsize)
        svals = np.concatenate([sa_.astype("S%d" % w), sb_.astype("S%d" % w)])
        lens = np.concatenate([la, lb])
        seqs = np.concatenate([np.asarray(a[2][i:i2]), np.asarray(b[2][j:j2])])
        raw = [(a, i, i2), (b, j, j2)]
        blobs = np.concatenate([np.asarray(r[0][int(r[1][x]):int(r[1][y])]) for r, x, y in raw])
        starts = np.concatenate([np.asarray(r[1][x:y]) - int(r[1][x]) + (0 if r is a else int(a[1][i2]) - int(a[1][i]))
                                 for r, x, y in raw])
        order = np.lexsort((seqs, lens, svals))
        if live is not None and len(order):
            order = order[live(seqs[order])]
        ol = lens[order]
        out_offs = np.zeros(len(order), dtype=np.int64)
        if len(order) > 1:
            np.cumsum(ol[:-1], out=out_offs[1:])
        if len(order):
            src = np.repeat(starts[order], ol) + (np.arange(int(ol.sum())) - np.repeat(out_offs, ol))
            sink.put(0, blobs[src])
        sink.put(1, out_offs + base)
        sink.put(2, seqs[order])
        base += int(ol.sum())
        if pace:
            pace(int(ol.sum()) + len(order) * 12)
        i, j = i2, j2
    sink.put(1, np.asarray([base], dtype=np.int64))
    return sink.finish()


# ------------------------------------------------------------------------------------------------ the run sets
class Leveled:
    """Leveled runs (oldest first) + a RAM delta, generic over the run kind. Subclasses define how a delta becomes a
    run, how two runs merge, and the run's dtypes. RAM runs merge under the store lock as before (they are small);
    a RAM run that grows past `seal_at` is written to disk; disk runs merge in the background (`disk_merge`)."""

    RATIO = 4
    MAX_DISK_RUNS = 12
    dtypes: tuple = ()
    seal_at = 1 << 22

    def __init__(self, kind: str):
        self.kind = kind                 # file-name stem: run-<kind>-<n>.<i>.bin
        self.runs: list = []
        self.dir: str | None = None
        self.next_id = None              # callable -> unique int (the store's run counter)
        self.garbage: list = []          # names of disk runs replaced since the last checkpoint
        self.pending: set = set()        # names a background merge is writing (a checkpoint must not sweep them)

    # subclass hooks
    def _delta_run(self):
        raise NotImplementedError

    def _clear_delta(self) -> None:
        raise NotImplementedError

    def _merge(self, a, b, sink, live=None, pace=None):
        raise NotImplementedError

    @staticmethod
    def _size(run) -> int:
        return len(run[-1])

    def _new_name(self) -> str:
        return "%s-%08d" % (self.kind, self.next_id())

    def _seal(self, run) -> Run:
        return write_run(run, self.dtypes, self.dir, self._new_name())

    def merge(self, live=None) -> None:
        """Under the store lock: the delta becomes a RAM run; RAM runs merge while the newest is >= 1/RATIO of the one
        before it; a RAM run past `seal_at` is written out."""
        if live is not None and not callable(live):          # a per-seq dead array (nonzero = dead)
            arr = np.frombuffer(live, dtype=np.uint8) if isinstance(live, (bytes, bytearray)) else np.asarray(live)
            live = (lambda p: arr[p] == 0)
        if self._has_delta():
            r = self._delta_run()
            self._clear_delta()
            if self._size(r):
                self.runs.append(r)
        while (len(self.runs) > 1 and self.runs[-1].name is None and self.runs[-2].name is None
               and self._size(self.runs[-1]) * self.RATIO >= self._size(self.runs[-2])):
            b = self.runs.pop()
            a = self.runs.pop()
            self.runs.append(self._merge(a, b, RunSink(self.dtypes), live))
        if self.dir and self.runs and self.runs[-1].name is None and self._size(self.runs[-1]) >= self.seal_at:
            self.runs[-1] = self._seal(self.runs[-1])

    def take_delta(self) -> None:
        """Under the store lock: what arrived since the last flush becomes a RAM run at the end (a sort of the new
        postings only -- cheap). The cascade and the seal that `merge` also does are `compact_ram`'s, off the lock."""
        if self._has_delta():
            r = self._delta_run()
            self._clear_delta()
            if self._size(r):
                self.runs.append(r)

    def compact_ram(self, lock, live=None) -> None:
        """`merge`'s RAM-run cascade and its seal, WITHOUT holding `lock` while merging or writing: each step picks
        under the lock, works on runs nothing mutates, and swaps the result in only if its inputs are still where they
        were (the `disk_merge` rule). Under the lock the whole of it -- a merge of runs up to `seal_at` and, past
        that, a run WRITTEN TO DISK -- blocked every reader: a load test (2026-10-17, 400 clients on a copy of
        poster.place's store) measured timeline answers of 32 ms median and 45-53 s at the 99th percentile while
        the relay sat at a quarter of a core, every query waiting behind the flush."""
        def locked_live(x):
            if live is None:
                return np.ones(len(x), dtype=bool)
            with lock:
                return live(x)
        while True:
            with lock:
                if not (len(self.runs) > 1 and self.runs[-1].name is None and self.runs[-2].name is None
                        and self._size(self.runs[-1]) * self.RATIO >= self._size(self.runs[-2])):
                    break
                a, b = self.runs[-2], self.runs[-1]
            out = self._merge(a, b, RunSink(self.dtypes), locked_live)
            with lock:
                for i in range(len(self.runs) - 1):
                    if self.runs[i] is a and self.runs[i + 1] is b:
                        self.runs[i:i + 2] = [out]
                        self._changed()
                        break
                else:
                    return                    # a checkpoint or another flush rearranged them: their result stands
        if not self.dir:
            return
        with lock:
            if not (self.runs and self.runs[-1].name is None and self._size(self.runs[-1]) >= self.seal_at):
                return
            r = self.runs[-1]
            name = self._new_name()
            self.pending.add(name)
        try:
            sealed = write_run(r, self.dtypes, self.dir, name)
        except BaseException:
            with lock:
                self.pending.discard(name)
            raise
        with lock:
            self.pending.discard(name)
            for i, x in enumerate(self.runs):
                if x is r:
                    self.runs[i] = sealed
                    self._changed()
                    return
            self.garbage.append(name)       # sealed meanwhile by a checkpoint: this copy is swept

    def seal_all(self, live=None) -> None:
        """Checkpoint: every RAM run merged into one and written out (under the store lock)."""
        self.merge(live)
        if not self.dir:
            return
        ram = [r for r in self.runs if r.name is None]
        if not ram:
            return
        disk = [r for r in self.runs if r.name is not None]
        assert self.runs == disk + ram, "RAM runs are always the newest"
        while len(ram) > 1:
            b = ram.pop()
            a = ram.pop()
            ram.append(self._merge(a, b, RunSink(self.dtypes), live))
        self.runs = disk + ([self._seal(ram[0])] if self._size(ram[0]) else [])

    def pick_disk_merge(self):
        """(index, a, b) of the disk-run pair to merge next, or None: the newest pair within RATIO, or -- with too many
        disk runs -- the pair with the smallest sum (a backlog after a long replay must still converge)."""
        disk = [i for i, r in enumerate(self.runs) if r.name is not None]
        pairs = [(i, i + 1) for i in disk if i + 1 < len(self.runs) and self.runs[i + 1].name is not None]
        for i, k in reversed(pairs):
            if self._size(self.runs[k]) * self.RATIO >= self._size(self.runs[i]):
                return i, self.runs[i], self.runs[k]
        if len(disk) > self.MAX_DISK_RUNS and pairs:
            i, k = min(pairs, key=lambda p: self._size(self.runs[p[0]]) + self._size(self.runs[p[1]]))
            return i, self.runs[i], self.runs[k]
        return None

    def disk_merge(self, lock, live=None, pace=None) -> bool:
        """One background merge of two adjacent disk runs: picked under `lock`, STREAMED without it, swapped in under it
        (only if both are still where they were). Returns True when one was merged."""
        with lock:
            pick = self.pick_disk_merge()
            if pick is None:
                return False
            _, a, b = pick
            name = self._new_name()
            self.pending.add(name)

        def locked_live(x):
            if live is None:
                return np.ones(len(x), dtype=bool)
            with lock:
                return live(x)
        sink = RunSink(self.dtypes, self.dir, name)
        try:
            out = self._merge(a, b, sink, locked_live, pace)
        except BaseException:
            sink.abort()
            with lock:
                self.pending.discard(name)
            raise
        with lock:
            self.pending.discard(name)
            for i in range(len(self.runs) - 1):
                if self.runs[i] is a and self.runs[i + 1] is b:
                    self.runs[i:i + 2] = [out] if self._size(out) else []
                    self.garbage += [a.name, b.name]
                    if not self._size(out):
                        self.garbage.append(out.name)
                    self._changed()
                    return True
            self.garbage.append(out.name)
            return False

    def _changed(self) -> None:
        pass

    def _has_delta(self) -> bool:
        raise NotImplementedError

    def disk_names(self) -> list:
        return [r.name for r in self.runs if r.name is not None]

    def ram_bytes(self) -> int:
        return sum(r.nbytes for r in self.runs if r.name is None)

    def disk_bytes(self) -> int:
        return sum(r.nbytes for r in self.runs if r.name is not None)

    def nbytes(self) -> int:
        return sum(r.nbytes for r in self.runs)

    def load(self, names) -> None:
        self.runs = [map_run(self.dir, n, self.dtypes) for n in names]


class KVRuns(Leveled):
    """u64 key -> u32 values (the id lookup by 8-byte id prefix, the author lookup by 8-byte pubkey prefix)."""

    dtypes = KV_DTYPES
    seal_at = 1 << 20

    def __init__(self, kind):
        super().__init__(kind)
        self.delta: dict = {}

    def add(self, key: int, val: int) -> None:
        self.delta.setdefault(key, []).append(val)

    def get(self, key: int) -> list:
        out = []
        for r in self.runs:
            out += kv_get(r, key)
        out += self.delta.get(key, ())
        return out

    def _has_delta(self):
        return bool(self.delta)

    def _delta_run(self):
        return kv_from_pairs([(k, v) for k, vs in self.delta.items() for v in vs])

    def _clear_delta(self):
        self.delta = {}

    def _merge(self, a, b, sink, live=None, pace=None):
        return merge_kv(a, b, sink, live, pace)

    def entries(self) -> int:
        return sum(len(r[0]) for r in self.runs) + sum(len(v) for v in self.delta.values())


class StrRuns(Leveled):
    """Sorted (value, seq) for PREFIX matching on one tag letter (`#d~`, how every app table loads)."""

    dtypes = STR_DTYPES
    seal_at = 1 << 18

    def __init__(self, kind):
        super().__init__(kind)
        self.delta: list = []

    def _has_delta(self):
        return bool(self.delta)

    def _delta_run(self):
        return str_from_pairs(self.delta)

    def _clear_delta(self):
        self.delta = []

    def _merge(self, a, b, sink, live=None, pace=None):
        return merge_strings(a, b, sink, live, pace)

    @staticmethod
    def _size(run) -> int:
        return len(run[2])

    @staticmethod
    def range(run, pre: bytes, hi: bytes | None):
        """[i, j): the entries whose value starts with `pre` (`hi` = the smallest string above every such value)."""
        i = str_lower(run, pre, -1)
        j = str_lower(run, hi, -1, i) if hi is not None else len(run[2])
        return i, j


def bisect_pairs(pairs, pre: str):
    """Index of the first (value, seq) >= (pre, -1) in a Python list sorted the same way."""
    return bisect.bisect_left(pairs, (pre, -1))


# ------------------------------------------------------------------------------------------------ giving RAM back
_MADV_COLD, _MADV_PAGEOUT = 20, 21          # Linux 5.4+ (not exported by Python's mmap module)


def release_mapping(mm, file_backed_only: bool) -> None:
    """Hand a mapping's resident pages back to the kernel. A read-only run mapping is dropped outright (DONTNEED: the
    pages are the file's, re-read on the next touch). A copy-on-write column mapping is only PAGED OUT -- never
    DONTNEED, which would discard rows not yet checkpointed; PAGEOUT reclaims the clean file pages and leaves the
    private ones (there is no swap to put them in)."""
    if mm is None or getattr(mm, "closed", False):
        return
    for adv in ((mmap.MADV_DONTNEED,) if file_backed_only else ()) + (_MADV_PAGEOUT, _MADV_COLD):
        try:
            mm.madvise(adv)
            return
        except (OSError, ValueError, AttributeError):
            continue


def mapping_of(arr):
    """The mmap a run array views (None for an array built in RAM)."""
    b = getattr(arr, "base", None)
    return b if isinstance(b, mmap.mmap) else None

