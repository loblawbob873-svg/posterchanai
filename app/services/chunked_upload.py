"""Uploads larger than one request may carry: a file sent in parts, joined and stored on this node's Blossom.

poster.place sits behind Cloudflare, which refuses a request body over 100 MB whatever this server allows -- so a
phone video for the Meme Builder (now a video editor, 2026-10-21) could not be uploaded at all from outside the LAN.
The client sends <= 64 MB parts (`put_part`), then asks to `finish`: the parts are joined IN ORDER on the data disk,
the size and SHA-256 the client declared are checked against the joined file (a missing or reordered part is
refused, never stored), and the file goes into the blob store by streaming (blossom_service.save_blob_file). Nothing
is held in memory. Unfinished uploads expire after UPLOAD_TTL_S. A failed join leaves the upload unusable; the
client starts over with a new id.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time

from app.services import work_dir

PART_MAX = 64 * 1024 * 1024
MAX_PARTS = 64
UPLOAD_TTL_S = 6 * 3600
_ID = re.compile(r"^[0-9a-f]{16,64}$")
_PK = re.compile(r"^[0-9a-f]{64}$")


class UploadError(ValueError):
    pass


def _dir(pubkey: str, upload_id: str) -> str:
    if not _PK.match(pubkey or "") or not _ID.match(upload_id or ""):
        raise UploadError("bad upload id")
    return os.path.join(work_dir.root(), "chunked", pubkey, upload_id)


def sweep(now: float | None = None) -> int:
    """Remove uploads nobody finished within UPLOAD_TTL_S. Returns how many were removed."""
    base = os.path.join(work_dir.root(), "chunked")
    now = now or time.time()
    gone = 0
    if not os.path.isdir(base):
        return 0
    for pk in os.listdir(base):
        for uid in os.listdir(os.path.join(base, pk)):
            d = os.path.join(base, pk, uid)
            try:
                if now - os.path.getmtime(d) > UPLOAD_TTL_S:
                    shutil.rmtree(d, ignore_errors=True)
                    gone += 1
            except OSError:
                pass
    return gone


async def put_part(pubkey: str, upload_id: str, index: int, chunks) -> int:
    """Write one part from an async byte iterator; refuses past PART_MAX while it arrives."""
    if not isinstance(index, int) or index < 0 or index >= MAX_PARTS:
        raise UploadError("bad part number")
    d = _dir(pubkey, upload_id)
    os.makedirs(d, exist_ok=True)
    part = os.path.join(d, "part-%04d" % index)
    tmp = part + ".writing"
    n = 0
    with open(tmp, "wb") as fh:
        async for chunk in chunks:
            n += len(chunk)
            if n > PART_MAX:
                fh.close()
                os.unlink(tmp)
                raise UploadError("a part is larger than %d MB" % (PART_MAX // (1024 * 1024)))
            fh.write(chunk)
    os.replace(tmp, part)          # a part is there whole, or not at all (a retried part replaces it)
    os.utime(d, None)
    return n


def join(pubkey: str, upload_id: str, parts: int, size: int, sha256: str, max_bytes: int) -> str:
    """Join parts 0..parts-1 into one file and check it is exactly what the client declared. Returns its path."""
    d = _dir(pubkey, upload_id)
    if not isinstance(parts, int) or parts < 1 or parts > MAX_PARTS:
        raise UploadError("bad part count")
    if not isinstance(size, int) or size < 1 or size > max_bytes:
        raise UploadError("the file is larger than this server accepts (%d MB)" % (max_bytes // (1024 * 1024)))
    if not re.match(r"^[0-9a-f]{64}$", sha256 or ""):
        raise UploadError("bad sha256")
    out = os.path.join(d, "joined")
    h = hashlib.sha256()
    total = 0
    with open(out, "wb") as dst:
        for i in range(parts):
            p = os.path.join(d, "part-%04d" % i)
            if not os.path.isfile(p):
                raise UploadError("part %d never arrived" % i)
            with open(p, "rb") as src:
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    total += len(b)
                    if total > size:
                        raise UploadError("the parts add up to more than the declared size")
                    h.update(b)
                    dst.write(b)
            os.unlink(p)           # freed as it is joined: the data disk never holds the file twice over
    if total != size:
        raise UploadError("the parts add up to %d bytes, not %d" % (total, size))
    if h.hexdigest() != sha256:
        raise UploadError("the joined file does not match its sha256 -- a part was damaged; upload it again")
    return out


def discard(pubkey: str, upload_id: str) -> None:
    try:
        shutil.rmtree(_dir(pubkey, upload_id), ignore_errors=True)
    except UploadError:
        pass
