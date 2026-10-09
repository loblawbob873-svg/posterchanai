"""PosterChanDB record codec: a Nostr event <-> compact bytes, LOSSLESSLY.

The one property that cannot bend: `decode(encode(ev)) == ev` field for field, string for string —
the event id is a hash of exactly these values and the signature covers that id, so a codec that
"normalises" anything (hex case, whitespace in content, an integer that was sent as 1.0) would return
an event no client can verify. Every space-saving trick below is therefore applied ONLY when it is
provably reversible for that particular value, and falls back to storing the original otherwise.

Layout (all integers are unsigned LEB128 varints unless noted):
    id 32 bytes | pubkey 32 bytes | sig 64 bytes | created_at | kind
    tag count, then per tag: element count, then per element one of
        0x00 + 32 raw bytes          a 64-char LOWERCASE hex string (ids, pubkeys — most `p`/`e` values)
        0x01 + len + utf-8 bytes     anything else
    content: one mode byte, then
        0x00 + len + utf-8           as written
        0x01 + len + zlib(dict)      text that compressed smaller with the relay's preset dictionary
        0x02 + len + bytes           standard base64 that re-encodes to exactly the same string
                                     (NIP-44 payloads, gift-wrap content): stored decoded, 25% smaller
        0x03 + len + bytes + len + bytes   NIP-04 "<base64>?iv=<base64>", both halves decoded
Ids, pubkeys and signatures are always lowercase hex in a valid event; `encode` refuses anything else
rather than store a value it could not give back.
"""
from __future__ import annotations

import base64
import binascii
import re
import zlib

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")
_B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_NIP04 = re.compile(r"^([A-Za-z0-9+/]+={0,2})\?iv=([A-Za-z0-9+/]+={0,2})$")


def _varint(n: int, out: bytearray) -> None:
    if n < 0:
        raise ValueError("negative varint")
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _read_varint(buf, i: int):
    n = shift = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _bytes(b: bytes, out: bytearray) -> None:
    _varint(len(b), out)
    out += b


def _b64_exact(s: str):
    """Decoded bytes if `s` is standard base64 that encodes back to exactly `s`, else None."""
    if len(s) < 16 or len(s) % 4 or not _B64.match(s):
        return None
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if base64.b64encode(raw).decode("ascii") == s else None


class Codec:
    """`zdict` is the preset compression dictionary (bytes) — part of the store's format: a record
    written with one dictionary can only be read with the same one, so the store keeps it with its data."""

    def __init__(self, zdict: bytes = b""):
        self.zdict = zdict or b""

    def _compress(self, data: bytes) -> bytes:
        co = zlib.compressobj(6, zlib.DEFLATED, -15, 9, zlib.Z_DEFAULT_STRATEGY, self.zdict) if self.zdict \
            else zlib.compressobj(6, zlib.DEFLATED, -15)
        return co.compress(data) + co.flush()

    def _decompress(self, data: bytes) -> bytes:
        do = zlib.decompressobj(-15, self.zdict) if self.zdict else zlib.decompressobj(-15)
        return do.decompress(data) + do.flush()

    def encode(self, ev: dict) -> bytes:
        eid, pk, sig = ev["id"], ev["pubkey"], ev["sig"]
        if not (isinstance(eid, str) and _HEX64.match(eid) and isinstance(pk, str) and _HEX64.match(pk)
                and isinstance(sig, str) and _HEX128.match(sig)):
            raise ValueError("id/pubkey/sig must be lowercase hex")
        ca, kind = ev["created_at"], ev["kind"]
        if type(ca) is not int or type(kind) is not int or ca < 0 or kind < 0:
            raise ValueError("created_at/kind must be non-negative integers")
        out = bytearray(bytes.fromhex(eid) + bytes.fromhex(pk) + bytes.fromhex(sig))
        _varint(ca, out)
        _varint(kind, out)
        tags = ev.get("tags") or []
        _varint(len(tags), out)
        for t in tags:
            if not isinstance(t, list):
                raise ValueError("a tag must be a list")
            _varint(len(t), out)
            for v in t:
                if not isinstance(v, str):
                    raise ValueError("tag elements must be strings")
                if len(v) == 64 and _HEX64.match(v):
                    out.append(0)
                    out += bytes.fromhex(v)
                else:
                    out.append(1)
                    _bytes(v.encode("utf-8"), out)
        content = ev.get("content", "")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        raw = content.encode("utf-8")
        m = _NIP04.match(content) if "?iv=" in content else None
        ct = _b64_exact(m.group(1)) if m else None
        iv = _b64_exact(m.group(2)) if m and len(m.group(2)) >= 16 else None
        if m and ct is not None and iv is not None:
            out.append(3)
            _bytes(ct, out)
            _bytes(iv, out)
            return bytes(out)
        b = _b64_exact(content)
        if b is not None:
            out.append(2)
            _bytes(b, out)
            return bytes(out)
        z = self._compress(raw) if len(raw) >= 48 else None
        if z is not None and len(z) < len(raw):
            out.append(1)
            _bytes(z, out)
        else:
            out.append(0)
            _bytes(raw, out)
        return bytes(out)

    def decode(self, buf) -> dict:
        mv = memoryview(buf)
        eid, pk, sig = bytes(mv[0:32]).hex(), bytes(mv[32:64]).hex(), bytes(mv[64:128]).hex()
        i = 128
        ca, i = _read_varint(mv, i)
        kind, i = _read_varint(mv, i)
        nt, i = _read_varint(mv, i)
        tags = []
        for _ in range(nt):
            ne, i = _read_varint(mv, i)
            t = []
            for _ in range(ne):
                mode = mv[i]
                i += 1
                if mode == 0:
                    t.append(bytes(mv[i:i + 32]).hex())
                    i += 32
                else:
                    n, i = _read_varint(mv, i)
                    t.append(bytes(mv[i:i + n]).decode("utf-8"))
                    i += n
            tags.append(t)
        mode = mv[i]
        i += 1
        n, i = _read_varint(mv, i)
        body = bytes(mv[i:i + n])
        i += n
        if mode == 0:
            content = body.decode("utf-8")
        elif mode == 1:
            content = self._decompress(body).decode("utf-8")
        elif mode == 2:
            content = base64.b64encode(body).decode("ascii")
        elif mode == 3:
            n2, i = _read_varint(mv, i)
            iv = bytes(mv[i:i + n2])
            content = base64.b64encode(body).decode("ascii") + "?iv=" + base64.b64encode(iv).decode("ascii")
        else:
            raise ValueError("unknown content mode %d" % mode)
        return {"id": eid, "pubkey": pk, "created_at": ca, "kind": kind, "tags": tags,
                "content": content, "sig": sig}
