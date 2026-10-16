"""PosterChanDB checkpoints: what a start needs to skip the log, written as what CHANGED since the last one.

The index lives in files (disk.py). A checkpoint makes those files describe one point in the log, and records it:

  1. the log is fsynced up to (active segment, flushed offset) -- every file written below derives only from it;
  2. RAM runs are written out as run files (fsynced); new column rows are appended BEYOND the last checkpoint's
     lengths (where the last checkpoint does not look), fsynced;
  3. pages changed BELOW those lengths (a kill flips a byte, a compaction moves records) go to `journal.bin` first;
  4. MANIFEST.json is replaced atomically: the lengths, the run files, the log position, the journal's id;
  5. the journal is applied in place, the columns fsynced, the journal removed; private copies dropped (remap);
  6. run files no longer named anywhere are deleted.

A crash anywhere leaves either the old manifest (whose lengths and runs are untouched: steps 2-3 wrote only beyond
them or beside them) or the new one with a journal that is re-applied on open (idempotent). Opening replays the log
after the recorded position, so nothing that was fsynced is lost either way.

Cost: the new rows, the RAM runs and a few pages -- proportional to what was written since the last checkpoint,
not to the store. The previous format (`snapshot.py`, a single npz of everything) wrote ~260 bytes an event every
time: 1.24 GB on poster.place, and it would have been ~50 GB at 200 GB of events.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import zipfile
import zlib

import numpy as np

from . import disk

FORMAT = 2
NAME = "MANIFEST.json"
JOURNAL = "journal.bin"
_JMAGIC = b"PCDBJ1\n\0"


def fingerprint() -> str:
    """Anything that changes what the indexes CONTAIN invalidates a checkpoint: the format and the tokenizer (its
    source and the Postgres parser tables it was generated from)."""
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256(b"posterchandb-index-%d" % FORMAT)
    for name in ("tsparser.py", "pg_parser_tables.py"):
        with open(os.path.join(here, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:32]


def _atomic_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    disk.fsync_dir(os.path.dirname(path))


def _write_journal(path: str, ckid: int, entries) -> int:
    body = bytearray()
    for name, off, data in entries:
        nb = name.encode()
        body += struct.pack("<H", len(nb)) + nb + struct.pack("<QI", off, len(data)) + data
    head = _JMAGIC + struct.pack("<QI", ckid, zlib.crc32(body) & 0xFFFFFFFF)
    with open(path + ".tmp", "wb") as f:
        f.write(head)
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(path + ".tmp", path)
    return len(head) + len(body)


def _read_journal(path: str):
    """(ckid, [(name, off, data)]) or None when absent, torn or corrupt."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw.startswith(_JMAGIC) or len(raw) < len(_JMAGIC) + 12:
        return None
    ckid, crc = struct.unpack_from("<QI", raw, len(_JMAGIC))
    body = raw[len(_JMAGIC) + 12:]
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        return None
    out, i = [], 0
    while i < len(body):
        (n,) = struct.unpack_from("<H", body, i)
        name = body[i + 2:i + 2 + n].decode()
        off, ln = struct.unpack_from("<QI", body, i + 2 + n)
        j = i + 2 + n + 12
        out.append((name, off, body[j:j + ln]))
        i = j + ln
    return ckid, out


def save(st, deleted=()) -> dict:
    """Take a checkpoint of `st` (caller holds the store lock). `deleted`: segments a compaction has just dropped,
    whose files the caller removes AFTER this returns (recorded so a crash in between still removes them)."""
    if st._file is not None:
        st.flush()                                  # the log, fsynced: everything below derives only from it
    for lv in st._leveled():
        lv.seal_all(st._live_of(lv))
    st._prefmax(len(st.off))                       # the newest-first scan's running max: saved, not rebuilt in RAM
    wrote = 0
    for c in st._cols.values():
        wrote += c.write_tail()
    entries = [(name, off, data) for name, c in st._cols.items() for off, data in c.journal_entries()]
    ckid = st._ckpt_id + 1
    jpath = os.path.join(st.path, JOURNAL)
    if entries:
        wrote += _write_journal(jpath, ckid, entries)
    for c in st._cols.values():
        c.sync()
    for sid in [s for s, ms in st.seg_markers.items() if ms is not None and s != st._active]:
        st._write_markers(sid)
    covered = {s: v for s, v in st.seg_size.items() if s <= st._active}
    manifest = {
        "format": FORMAT, "fingerprint": fingerprint(), "ckpt": ckid, "journal": bool(entries),
        "events": len(st.off), "active": st._active, "flushed": st._flushed,
        "seg_size": {str(k): (st._flushed if k == st._active else v) for k, v in covered.items()},
        "seg_bytes": {str(k): v for k, v in st.seg_bytes.items() if k in covered},
        "seg_dead": {str(k): v for k, v in st.seg_dead.items() if k in covered},
        "columns": {name: len(c) for name, c in st._cols.items()},
        "runs": {lv.kind: lv.disk_names() for lv in st._leveled()},
        "run_seq": st._run_seq,
        "derived": [[s, t, v] for s, tv in st.derived.items() for t, v in sorted(tv)],
        "unworded": list(st._unworded),
        "deleted": sorted(set(int(x) for x in deleted)),
    }
    _atomic_json(os.path.join(st.path, NAME), manifest)
    if entries:
        for name, off, data in entries:
            disk.pwrite_all(st._cols[name].fd, data, off)
        for c in st._cols.values():
            c.sync()
        try:
            os.remove(jpath)
        except OSError:
            pass
    for c in st._cols.values():
        c.committed()
    st._ckpt_id = ckid
    _sweep_runs(st, manifest["runs"])
    for lv in st._leveled():
        lv.garbage = []
    return {"events": len(st.off), "bytes": wrote + os.path.getsize(os.path.join(st.path, NAME)),
            "journal_pages": len(entries)}


_RUN_FILE = re.compile(r"^run-(.+)\.\d+\.bin(\.tmp)?$")


def _sweep_runs(st, named: dict) -> None:
    """Delete run files nothing refers to: not in this manifest, not a live run, not being written."""
    keep = {n for names in named.values() for n in names}
    for lv in st._leveled():
        keep |= set(lv.disk_names()) | set(lv.pending)
    for f in os.listdir(st.path):
        m = _RUN_FILE.match(f)
        if m and m.group(1) not in keep:
            try:
                os.remove(os.path.join(st.path, f))
            except OSError:
                pass


def load(st):
    """Open `st` at its last checkpoint. Returns (ok, why, meta); on ok the caller replays the log from
    meta['active'] / meta['flushed'] onward. Anything that does not line up refuses it (the caller replays all)."""
    path = os.path.join(st.path, NAME)
    if not os.path.exists(path):
        return False, "no checkpoint", None
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        if m.get("format") != FORMAT or m.get("fingerprint") != fingerprint():
            return False, "made by another version", None
        jpath = os.path.join(st.path, JOURNAL)
        if os.path.exists(jpath):
            j = _read_journal(jpath)
            # The journal of THIS checkpoint may not have been applied (a crash after the manifest): apply it again
            # (idempotent). Any other journal belongs to a checkpoint that never completed, or was already applied.
            if j is not None and j[0] == m["ckpt"]:
                fds = {}
                try:
                    for name, off, data in j[1]:
                        fd = fds.get(name)
                        if fd is None:
                            fd = fds[name] = os.open(st._col_path(name), os.O_RDWR)
                        disk.pwrite_all(fd, data, off)
                    for fd in fds.values():
                        os.fsync(fd)
                finally:
                    for fd in fds.values():
                        os.close(fd)
            os.remove(jpath)
        on_disk = set(st._segments())
        sizes = {int(k): v for k, v in m["seg_size"].items()}
        active = int(m["active"])
        for sid in m.get("deleted") or ():
            if sid in on_disk and sid not in sizes:          # a compaction finished: its old file goes now
                for f in (st._seg_path(sid), st._mk_path(sid)):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                on_disk.discard(sid)
        for sid, size in sizes.items():
            if sid not in on_disk:
                return False, "segment %d is gone" % sid, None
            real = os.path.getsize(st._seg_path(sid))
            if (sid < active and real != size) or real < size:
                return False, "segment %d changed size" % sid, None
        if any(sid not in sizes and sid < active for sid in on_disk):
            return False, "an unknown segment older than the checkpoint", None
        st._reset_state(lengths=m["columns"])
        for lv in st._leveled():
            lv.load(m["runs"].get(lv.kind, []))
        st._run_seq = int(m.get("run_seq", 0))
        st._ckpt_id = int(m["ckpt"])
        if len(st._cols["authors"]) % 32 or len(st._cols["ids"]) != 32 * m["events"] or len(st.off) != m["events"]:
            raise ValueError("columns disagree")
        st.derived = {}
        for s, t, v in m["derived"]:
            st.derived.setdefault(int(s), set()).add((t, v))
        import collections
        st._unworded = collections.deque(int(x) for x in m.get("unworded") or ())
        st.seg_size = dict(sizes)
        st.seg_bytes = {int(k): v for k, v in m["seg_bytes"].items()}
        st.seg_dead = {int(k): v for k, v in m["seg_dead"].items()}
        return True, "loaded %d events" % m["events"], m
    except (OSError, ValueError, KeyError, TypeError) as e:
        st._reset_state()
        return False, "unreadable (%s: %s)" % (e.__class__.__name__, e), None


def import_legacy(st):
    """Open `st` from the PREVIOUS format's `index.snap` (one npz of every array) instead of replaying the whole log:
    the arrays are written into this format's files (columns, run files), then the caller replays the tail and
    takes a checkpoint. Same refusals as the old loader."""
    from . import snapshot as legacy
    path = os.path.join(st.path, legacy.NAME)
    try:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(bytes(z["meta"]).decode("utf-8"))
            if meta.get("format") != legacy.FORMAT or meta.get("fingerprint") != legacy._fingerprint():
                return False, "made by another version", None
            on_disk = set(st._segments())
            sizes = {int(k): v for k, v in meta["seg_size"].items()}
            active = int(meta["active"])
            for sid, size in sizes.items():
                if sid not in on_disk:
                    return False, "segment %d is gone (compacted since)" % sid, None
                real = os.path.getsize(st._seg_path(sid))
                if (sid < active and real != size) or real < size:
                    return False, "segment %d changed size" % sid, None
            if any(sid not in sizes and sid < active for sid in on_disk):
                return False, "an unknown segment older than the snapshot", None
            st._reset_state()
            for name in ("seg", "off", "length", "created", "kind", "expires", "origin", "author", "dead", "ids"):
                a = z[name]
                st._cols[name].extend(a.view(np.uint8).tobytes() if st._cols[name].isz == 1 else a)
                del a
            authors = z["authors"].tobytes()
            st._cols["authors"].extend(authors)
            n_auth = len(authors) // 32
            if n_auth:
                keys = np.frombuffer(authors, dtype=">u8").reshape(-1, 4)[:, 0].astype(np.uint64)
                o = np.argsort(keys, kind="stable")
                st._aurun.runs = [st._aurun._seal(disk.Run([keys[o], np.arange(n_auth, dtype=np.uint32)[o]]))]
            del authors
            for lv, pre in ((st.idx, "idx"), (st.words, "w")):
                k = z[pre + "_k"]
                if len(k):
                    lv.runs = [lv._seal(disk.Run([k, z[pre + "_s"], z[pre + "_p"]]))]
            if len(z["id_k"]):
                st._idk.runs = [st._idk._seal(disk.Run([z["id_k"], z["id_v"]]))]
            pv = z["pref_vals"].tobytes().decode("utf-8", "surrogatepass")
            ps = z["pref_seqs"]
            if len(ps):
                vals = pv.split("\x00")
                st.dprefix.runs = [st.dprefix._seal(disk.str_from_pairs(list(zip(vals, ps.tolist()))))]
            buf, o = z["mk_buf"].tobytes(), 0
            per = {}
            for sid, n in zip(z["mk_sid"].tolist(), z["mk_len"].tolist()):
                per.setdefault(sid, []).append(buf[o:o + n])
                o += n
            for sid, ms in per.items():
                if sid < active and sid in sizes:
                    st.seg_markers[sid] = ms
                    st._write_markers(sid)
            import collections
            st._unworded = collections.deque(z["unworded"].tolist()) if "unworded" in z.files else collections.deque()
        st.derived = {}
        for s, t, v in meta["derived"]:
            st.derived.setdefault(int(s), set()).add((t, v))
        st.seg_size = dict(sizes)
        st.seg_bytes = {int(k): v for k, v in meta["seg_bytes"].items()}
        st.seg_dead = {int(k): v for k, v in meta["seg_dead"].items()}
        return True, "imported %d events" % meta["events"], meta
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        st._reset_state()
        return False, "legacy snapshot unreadable (%s)" % e.__class__.__name__, None
