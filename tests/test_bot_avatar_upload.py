"""Admin → Bots: an avatar uploads for a bot that is still being created.

"bots -> avatar could not be uploaded" -- the server log: `[upload-avatar] failed: Client error '403
Forbidden' for url 'http://127.0.0.1:3051/blossom/upload'`. The endpoint uploaded over HTTP through the
PUBLIC Blossom gate, signed by the bot's key; a bot whose identity was just generated has no Bot row, so
its key is not an operator key and the gate refused it. The endpoint is admin-only, so it now stores the
image directly, owned by the bot's key, kept out of the age sweep, at the node's public URL.
"""
import asyncio
import base64
import os
from types import SimpleNamespace

import pytest

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()


class _Q:
    def __init__(self, row): self.row = row
    def filter(self, *a, **k): return self
    def first(self): return self.row


class _DB:
    def __init__(self, row): self.row = row
    def query(self, *_): return _Q(self.row)


def _request():
    return SimpleNamespace(headers={"x-forwarded-proto": "https", "x-forwarded-host": "poster.place"},
                           url=SimpleNamespace(scheme="http", netloc="127.0.0.1:3051"), client=None)


def test_a_new_bots_avatar_is_stored_without_the_public_gate(monkeypatch):
    from app.routers import bots
    from app.services import blossom_service
    from app.services.nostr import nostr_service, media
    from app.routers import blossom as blossom_router
    from app.services.nostr import bech32
    sk = os.urandom(32)
    nsec = bech32.encode("nsec", sk)                      # a key with NO Bot row: a bot still being created
    pub = nostr_service.derive_pubkey(sk); pub = pub if isinstance(pub, str) else pub.hex()

    async def gate(*a, **k):
        raise RuntimeError("Client error '403 Forbidden' for url 'http://127.0.0.1:3051/blossom/upload'")
    monkeypatch.setattr(media, "upload_blossom", gate)                     # the public gate refuses, as it did
    saved = {}

    async def save_blob(db, pubkey, data, mime, **kw):
        saved.update(pubkey=pubkey, size=len(data), mime=mime, **kw)
        return {}
    monkeypatch.setattr(blossom_service, "save_blob", save_blob)
    monkeypatch.setattr(blossom_service, "_cfg", lambda db: {"public_url": ""})
    monkeypatch.setattr(blossom_router.tor_service, "request_onion_host", lambda r: "")
    row = SimpleNamespace(sha256="ab" * 32, type="image/png", size=70, uploaded=0, created_at=0)

    async def stored(sha):          # the blob index (relay documents, #161) holds the row save_blob wrote
        return row
    monkeypatch.setattr(blossom_service.blob_index, "aget", stored)
    monkeypatch.setattr(blossom_service, "descriptor", lambda blob, base, name="": {"url": f"{base}/{blob.sha256}.png"})

    out = asyncio.run(bots.upload_bot_avatar(bots.AvatarPayload(nsec=nsec, picture_data="data:image/png;base64," + PNG),
                                             _request(), db=_DB(row), admin=None))
    assert saved["pubkey"] == pub and saved["mime"] == "image/png" and saved.get("keep") is True, saved
    assert out == {"url": f"https://poster.place/blossom/{'ab' * 32}.png"}, out


def test_only_images_and_a_size_cap(monkeypatch):
    from app.routers import bots
    from fastapi import HTTPException
    from app.services.nostr import bech32
    nsec = bech32.encode("nsec", os.urandom(32))
    with pytest.raises(HTTPException) as e:
        asyncio.run(bots.upload_bot_avatar(bots.AvatarPayload(nsec=nsec, picture_data="data:text/html;base64,PGI+"),
                                           _request(), db=_DB(None), admin=None))
    assert e.value.status_code == 400
    big = base64.b64encode(b"\0" * (bots._AVATAR_MAX + 1)).decode()
    with pytest.raises(HTTPException) as e:
        asyncio.run(bots.upload_bot_avatar(bots.AvatarPayload(nsec=nsec, picture_data="data:image/png;base64," + big),
                                           _request(), db=_DB(None), admin=None))
    assert e.value.status_code == 413
