"""A tiny RFB (VNC) client: just enough to ask a display which security types it offers and whether a password
opens it. Used by scripts/vmhost_live_probe.py against a real QEMU display, and unit-tested offline.

VNC authentication (RFB security type 2) is DES in ECB mode over a 16-byte server challenge, keyed with the
password padded/truncated to 8 bytes with the bits of EVERY KEY BYTE REVERSED — the quirk that makes a stock DES
library give the wrong answer. DES is implemented here in pure Python (`cryptography` moved it to "decrepit"
and pycryptodome is not a dependency); 16 bytes are two blocks, so speed does not matter.
"""
from __future__ import annotations

import asyncio
import struct

# ------------------------------------------------------------------------------------------------ DES (FIPS 46-3)
_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
        63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4]
_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
       64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
       37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
      16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25]
_S = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _permute(value: int, table: list, in_bits: int) -> int:
    out = 0
    for pos in table:
        out = (out << 1) | ((value >> (in_bits - pos)) & 1)
    return out


def des_encrypt_block(key: bytes, block: bytes) -> bytes:
    """One DES block (ECB). `key` and `block` are 8 bytes."""
    if len(key) != 8 or len(block) != 8:
        raise ValueError("DES works on 8-byte keys and blocks")
    k = _permute(int.from_bytes(key, "big"), _PC1, 64)
    c, d = k >> 28, k & 0xFFFFFFF
    subkeys = []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0xFFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0xFFFFFFF
        subkeys.append(_permute((c << 28) | d, _PC2, 56))
    v = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = v >> 32, v & 0xFFFFFFFF
    for sk in subkeys:
        x = _permute(right, _E, 32) ^ sk
        f = 0
        for i in range(8):
            chunk = (x >> (42 - 6 * i)) & 0x3F
            row = ((chunk >> 5) << 1) | (chunk & 1)
            col = (chunk >> 1) & 0xF
            f = (f << 4) | _S[i][row * 16 + col]
        left, right = right, left ^ _permute(f, _P, 32)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def _reverse_bits(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """The 16-byte answer to a VNC-auth challenge."""
    if len(challenge) != 16:
        raise ValueError("a VNC challenge is 16 bytes")
    key = bytes(_reverse_bits(b) for b in password.encode("latin-1")[:8].ljust(8, b"\0"))
    return des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:])


# ------------------------------------------------------------------------------------------------ RFB handshake
SEC_NONE, SEC_VNC = 1, 2


class RfbError(Exception):
    pass


async def _exact(reader: asyncio.StreamReader, n: int, timeout: float) -> bytes:
    try:
        return await asyncio.wait_for(reader.readexactly(n), timeout)
    except asyncio.IncompleteReadError as e:
        raise RfbError(f"the display closed the connection after {len(e.partial)} of {n} bytes")


async def handshake(reader, writer, password, *, timeout: float = 10.0) -> dict:
    """Run RFB 3.8 up to the SecurityResult. Returns {version, types, result, reason}; `result` is "ok", "failed",
    or "no-vnc-auth" (the display did not offer type 2, in which case nothing was attempted). With password=None
    only the security types are read and the connection is left there."""
    banner = await _exact(reader, 12, timeout)
    if not banner.startswith(b"RFB "):
        raise RfbError(f"not an RFB server: {banner!r}")
    writer.write(b"RFB 003.008\n")
    await writer.drain()
    count = (await _exact(reader, 1, timeout))[0]
    if count == 0:
        ln = struct.unpack(">I", await _exact(reader, 4, timeout))[0]
        raise RfbError("the display refused the connection: "
                       + (await _exact(reader, ln, timeout)).decode("utf-8", "replace"))
    types = list(await _exact(reader, count, timeout))
    out = {"version": banner.decode("ascii", "replace").strip(), "types": types, "result": "", "reason": ""}
    if password is None:
        return out
    if SEC_VNC not in types:
        out["result"] = "no-vnc-auth"
        return out
    writer.write(bytes([SEC_VNC]))
    await writer.drain()
    challenge = await _exact(reader, 16, timeout)
    writer.write(vnc_auth_response(password, challenge))
    await writer.drain()
    status = struct.unpack(">I", await _exact(reader, 4, timeout))[0]
    if status == 0:
        out["result"] = "ok"
    else:
        out["result"] = "failed"
        try:
            ln = struct.unpack(">I", await _exact(reader, 4, 2.0))[0]
            out["reason"] = (await _exact(reader, min(ln, 4096), 2.0)).decode("utf-8", "replace")
        except (RfbError, asyncio.TimeoutError):
            pass
    return out


async def try_password(host: str, port: int, password, *, timeout: float = 10.0) -> dict:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        return await handshake(reader, writer, password, timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
