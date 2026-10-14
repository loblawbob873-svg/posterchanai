"""A NIP-98 token the user signed for SOMEBODY ELSE'S server is not a proof here (security review #3).

Kind 27235 is NIP-98's HTTP-auth kind. The same key signs one for every NIP-96 upload to a media host,
every ngit push, every site that asks a NIP-46 signer, and each of those servers RECEIVES the signed event.
`verify_self_auth` with no purpose checked kind, signature, pubkey and five minutes of freshness, and
nothing else, so a hostile media host could replay the upload token it was just handed against
`/client/delete-account`, `/client/file-auth`, `/api/push/subscribe` or `/api/auth/google/link/start`
(which led to the account's stored nsec via a Google sign-in).

A real NIP-98 token has an empty content and `u`/`method` tags. Every proof this app's clients make has a
non-empty content and a `p` tag naming the signer, and never a `u`.
"""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from app.services.nostr import event as nostr_event

SK = (0x51A).to_bytes(32, "big")


def _pk():
    ev = nostr_event.build_event(SK, 27235, "x", tags=[])
    return ev["pubkey"]


def _b64(ev):
    return base64.b64encode(json.dumps(ev).encode()).decode()


def _foreign_upload_token():
    """Exactly what upload.js signs for a NIP-96 host -- and what that host then holds."""
    return _b64(nostr_event.build_event(SK, 27235, "", tags=[
        ["u", "https://nostr.build/api/v2/upload/files"], ["method", "POST"]]))


def _foreign_with_content():
    """A NIP-98 token from a client that puts something in content -- still carries the URL it was for."""
    return _b64(nostr_event.build_event(SK, 27235, "auth", tags=[
        ["u", "https://evil.example/api"], ["method", "GET"], ["p", _pk()]]))


def _ours(content="auth"):
    """What selfProof() / signAuth() / the APK's SyncNet send."""
    return _b64(nostr_event.build_event(SK, 27235, content, tags=[["p", _pk()]]))


@pytest.fixture(autouse=True)
def _not_blocked(monkeypatch):
    from app.services import relay_blocklist
    monkeypatch.setattr(relay_blocklist, "is_blocked", lambda _pk: False)


def test_a_token_signed_for_a_media_host_is_not_a_proof_here():
    assert nostr_event.verify_self_auth(_foreign_upload_token(), _pk()) is False
    assert nostr_event.verify_self_auth(_foreign_with_content(), _pk()) is False


def test_a_token_naming_nobody_or_somebody_else_is_not_a_proof():
    no_p = _b64(nostr_event.build_event(SK, 27235, "auth", tags=[]))
    other_p = _b64(nostr_event.build_event(SK, 27235, "auth", tags=[["p", "ab" * 32]]))
    assert nostr_event.verify_self_auth(no_p, _pk()) is False
    assert nostr_event.verify_self_auth(other_p, _pk()) is False


def test_every_shape_our_clients_send_still_works():
    for content in ("auth", "sync", "files-index", "sync-state", "sync-manifest", "push-subscribe",
                    "relay-sync", "stream-request", "posterchan-direct:register:dev-0123456789abcdef"):
        assert nostr_event.verify_self_auth(_ours(content), _pk()) is True, content
    # Purpose-bound callers keep their exact-content rule, including the welcome screen's tag-less proof.
    welcome = _b64(nostr_event.build_event(SK, 27235, "instance-welcome-status", tags=[]))
    assert nostr_event.verify_self_auth(welcome, _pk(), "instance-welcome-status") is True
    assert nostr_event.verify_self_auth(_foreign_upload_token(), _pk(), "") is False


def test_the_media_host_cannot_delete_the_account(monkeypatch):
    """The route itself, with the token a NIP-96 host was handed: refused before any account is read."""
    from app.routers import client as C
    from app.services import user_table
    looked = []

    async def aby_npub(_db, npub):
        looked.append(npub)
        return SimpleNamespace(id=7, is_admin=False, nostr_npub=npub)
    monkeypatch.setattr(user_table, "aby_npub", aby_npub)
    req = C.DeleteAccountReq(pubkey=_pk(), auth=_foreign_upload_token())
    r = asyncio.run(C.delete_account(req, db=None))
    assert r.status_code == 403, r.body
    assert looked == [], "the account was looked up on the strength of a foreign token"
