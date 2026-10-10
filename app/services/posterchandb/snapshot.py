"""Index snapshots: start in seconds instead of re-indexing every event.

Without one, opening the store REPLAYS the whole log — decode every record, tokenise its content, hash ~14 index
keys — measured at ~145 µs an event, i.e. ~6½ minutes of a CPU core for poster.place's 2.6M events, at EVERY
restart and deploy. A snapshot is the finished in-RAM state (columns, ids, authors, postings, the `#d~` prefix
index, the id lookup, derived tags, per-segment markers and sizes) written as raw numpy arrays, plus the log
position it covers. Opening loads it (a memory copy, no parsing) and replays only what was written after it.

A snapshot can only make a start FASTER, never wrong. It is refused — and the full replay used — when anything
does not line up: another format or tokenizer (a different parser means a different search index), a segment
it lists that is gone or shorter (compaction ran since), or one it does not know about below the active segment.
Writing is atomic (temp file, fsync, rename, fsync of the directory), so a crash mid-write leaves the old one.

SSD writes: a snapshot is ~170-280 bytes an event, so it is written only when it pays — at a clean stop when at
least `min_events` were written since the last one, and periodically (maintenance, `snapshot_hours`). A restart
right after a restart writes nothing.
"""
from __future__ import annotations

import array
import hashlib
import io
import json
import os
import zipfile

import numpy as np

FORMAT = 1
NAME = "index.snap"


def _fingerprint() -> str:
    """Anything that changes what the indexes CONTAIN invalidates a snapshot: the store's format and the
    tokenizer (its source and the Postgres parser tables it was generated from)."""
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256(b"posterchandb-index-%d" % FORMAT)
    for name in ("tsparser.py", "pg_parser_tables.py"):
        with open(os.path.join(here, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:32]


def _one_run(postings, dead):
    """All runs + delta merged into ONE (keys, starts, post), dropping dead events."""
    postings.merge(dead)
    while len(postings.runs) > 1:
        b = postings.runs.pop()
        a = postings.runs.pop()
        from .store import _merge_runs
        postings.runs.append(_merge_runs(*a, *b, dead=dead))
    if postings.runs:
        return postings.runs[0]
    return (np.zeros(0, np.uint64), np.zeros(1, np.int64), np.zeros(0, np.uint32))


def save(store) -> dict:
    """Write a snapshot of `store` (caller holds the lock; the store has just flushed). Returns a summary."""
    st = store
    dead = np.frombuffer(st.dead, dtype=np.uint8) if len(st.dead) else None
    ik, is_, ip = _one_run(st.idx, dead)
    wk, ws, wp = _one_run(st.words, dead)
    st.dprefix.merge(st.dead)
    while len(st.dprefix.runs) > 1:
        b = st.dprefix.runs.pop(); a = st.dprefix.runs.pop()
        st.dprefix.runs.append(sorted(x for x in a + b if not st.dead[x[1]]))
    pref = st.dprefix.runs[0] if st.dprefix.runs else []
    st._merge_ids()
    while len(st._id_runs) > 1:
        st._merge_ids_once_more()
    idk, idv = st._id_runs[0] if st._id_runs else (np.zeros(0, np.uint64), np.zeros(0, np.uint32))
    pref_vals = "\x00".join(v for v, _ in pref).encode("utf-8")
    pref_seqs = np.fromiter((q for _, q in pref), dtype=np.uint32, count=len(pref))
    markers = []
    for sid, ms in st.seg_markers.items():
        for m in ms:
            markers.append((sid, m))
    mk_sid = np.fromiter((s for s, _ in markers), dtype=np.uint32, count=len(markers))
    mk_len = np.fromiter((len(m) for _, m in markers), dtype=np.uint32, count=len(markers))
    mk_buf = np.frombuffer(b"".join(m for _, m in markers), dtype=np.uint8)
    authors = np.frombuffer(b"".join(bytes.fromhex(a) for a in st._authors), dtype=np.uint8)
    meta = {"format": FORMAT, "fingerprint": _fingerprint(), "events": len(st.off), "active": st._active,
            "flushed": st._flushed, "seg_size": {str(k): v for k, v in st.seg_size.items()},
            "seg_bytes": {str(k): v for k, v in st.seg_bytes.items()},
            "seg_dead": {str(k): v for k, v in st.seg_dead.items()},
            "derived": [[s, t, v] for s, tv in st.derived.items() for t, v in sorted(tv)]}
    arrays = {
        "seg": np.frombuffer(st.seg, dtype=np.uint32), "off": np.frombuffer(st.off, dtype=np.uint64),
        "length": np.frombuffer(st.length, dtype=np.uint32), "created": np.frombuffer(st.created, dtype=np.uint64),
        "kind": np.frombuffer(st.kind, dtype=np.uint32), "expires": np.frombuffer(st.expires, dtype=np.uint64),
        "origin": np.frombuffer(st.origin, dtype=np.uint8), "author": np.frombuffer(st.author, dtype=np.uint32),
        "dead": np.frombuffer(st.dead, dtype=np.uint8), "ids": np.frombuffer(st.ids, dtype=np.uint8),
        "authors": authors, "idx_k": ik, "idx_s": is_, "idx_p": ip, "w_k": wk, "w_s": ws, "w_p": wp,
        "id_k": idk, "id_v": idv, "pref_vals": np.frombuffer(pref_vals, dtype=np.uint8), "pref_seqs": pref_seqs,
        "mk_sid": mk_sid, "mk_len": mk_len, "mk_buf": mk_buf,
        "meta": np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8),
    }
    path = os.path.join(st.path, NAME)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        np.savez(f, **arrays)          # uncompressed: loading is a memory copy, no CPU spent inflating
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    st._fsync_dir()
    return {"events": len(st.off), "bytes": os.path.getsize(path)}


def load(store):
    """Restore `store` from its snapshot. Returns (ok, why). On ok the store is in the state the snapshot
    describes and the caller replays the log from meta['active'] / meta['flushed'] onward."""
    st = store
    path = os.path.join(st.path, NAME)
    if not os.path.exists(path):
        return False, "no snapshot", None
    try:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(bytes(z["meta"]).decode("utf-8"))
            if meta.get("format") != FORMAT or meta.get("fingerprint") != _fingerprint():
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
            st.seg = array.array("I", z["seg"].tobytes())
            st.off = array.array("Q", z["off"].tobytes())
            st.length = array.array("I", z["length"].tobytes())
            st.created = array.array("Q", z["created"].tobytes())
            st.kind = array.array("I", z["kind"].tobytes())
            st.expires = array.array("Q", z["expires"].tobytes())
            st.origin = bytearray(z["origin"].tobytes())
            st.author = array.array("I", z["author"].tobytes())
            st.dead = bytearray(z["dead"].tobytes())
            st.ids = bytearray(z["ids"].tobytes())
            raw = z["authors"].tobytes()
            st._authors = [raw[i:i + 32].hex() for i in range(0, len(raw), 32)]
            st._author_ix = {a: i for i, a in enumerate(st._authors)}
            st.idx.runs = [(z["idx_k"], z["idx_s"], z["idx_p"])] if len(z["idx_k"]) else []
            st.idx.delta = {}
            st.words.runs = [(z["w_k"], z["w_s"], z["w_p"])] if len(z["w_k"]) else []
            st.words.delta = {}
            st._id_runs = [(z["id_k"], z["id_v"])] if len(z["id_k"]) else []
            st._id_delta = {}
            vals = z["pref_vals"].tobytes().decode("utf-8").split("\x00") if len(z["pref_seqs"]) else []
            st.dprefix.runs = [list(zip(vals, (int(x) for x in z["pref_seqs"])))] if vals else []
            st.dprefix.delta = []
            st.seg_markers = {}
            buf, o = z["mk_buf"].tobytes(), 0
            for sid, n in zip(z["mk_sid"].tolist(), z["mk_len"].tolist()):
                st.seg_markers.setdefault(sid, []).append(buf[o:o + n])
                o += n
        st.derived = {}
        for s, t, v in meta["derived"]:
            st.derived.setdefault(int(s), set()).add((t, v))
        st.seg_size = dict(sizes)
        st.seg_bytes = {int(k): v for k, v in meta["seg_bytes"].items()}
        st.seg_dead = {int(k): v for k, v in meta["seg_dead"].items()}
        return True, "loaded %d events" % meta["events"], meta
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, io.UnsupportedOperation) as e:
        return False, "unreadable (%s)" % e.__class__.__name__, None
