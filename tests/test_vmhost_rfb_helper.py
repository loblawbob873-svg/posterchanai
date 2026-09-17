"""scripts/vmhost_rfb.py — the RFB client the live probe uses to prove a QEMU display demands its password.

A VNC-auth DES that is subtly wrong would make the probe report "the right password FAILS" (a false alarm) or,
worse, pass for a display that accepts anything. So the DES is checked against FIPS test vectors and against
`cryptography` (single DES == 3DES with K1=K2=K3), and the handshake against a server that verifies with that
independent implementation.
"""
import asyncio
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import vmhost_rfb as rfb  # noqa: E402


def test_des_known_answer_vectors():
    assert rfb.des_encrypt_block(bytes.fromhex("0123456789ABCDEF"), bytes.fromhex("4E6F772069732074")).hex() == \
        "3fa40e8a984d4815"
    assert rfb.des_encrypt_block(bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")).hex() == \
        "85e813540f0ab405"


def _independent_des():
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:
        try:
            from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
        except ImportError:
            pytest.skip("cryptography is not installed")
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    def enc(key, block):
        e = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
        return e.update(block) + e.finalize()
    return enc


def test_des_matches_an_independent_implementation():
    enc = _independent_des()
    for _ in range(64):
        k, b = os.urandom(8), os.urandom(8)
        assert rfb.des_encrypt_block(k, b) == enc(k, b)


def _server(password, sec_types, verify):
    async def handle(reader, writer):
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        await reader.readexactly(12)
        writer.write(bytes([len(sec_types)]) + bytes(sec_types))
        await writer.drain()
        if not sec_types:
            return writer.close()
        chosen = (await reader.readexactly(1))[0]
        if chosen == rfb.SEC_VNC:
            challenge = os.urandom(16)
            writer.write(challenge)
            await writer.drain()
            answer = await reader.readexactly(16)
            if verify(password, challenge) == answer:
                writer.write(struct.pack(">I", 0))
            else:
                reason = b"Authentication failed"
                writer.write(struct.pack(">I", 1) + struct.pack(">I", len(reason)) + reason)
            await writer.drain()
        writer.close()
    return handle


def _expected(enc):
    def verify(password, challenge):
        key = bytes(int(f"{b:08b}"[::-1], 2) for b in password.encode()[:8].ljust(8, b"\0"))
        return enc(key, challenge[:8]) + enc(key, challenge[8:])
    return verify


def test_handshake_right_wrong_and_no_vnc_auth():
    enc = _independent_des()

    async def go():
        out = {}
        srv = await asyncio.start_server(_server("s3cret99", [rfb.SEC_VNC], _expected(enc)), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        async with srv:
            out["types"] = (await rfb.try_password("127.0.0.1", port, None))["types"]
            out["good"] = await rfb.try_password("127.0.0.1", port, "s3cret99")
            out["bad"] = await rfb.try_password("127.0.0.1", port, "s3cret98")
        none = await asyncio.start_server(_server("", [rfb.SEC_NONE], None), "127.0.0.1", 0)
        async with none:
            out["none"] = await rfb.try_password("127.0.0.1", none.sockets[0].getsockname()[1], "x")
        return out
    out = asyncio.run(go())
    assert out["types"] == [rfb.SEC_VNC]
    assert out["good"]["result"] == "ok"
    assert out["bad"]["result"] == "failed" and out["bad"]["reason"] == "Authentication failed"
    assert out["none"]["result"] == "no-vnc-auth", "a display without password auth must never read as protected"
