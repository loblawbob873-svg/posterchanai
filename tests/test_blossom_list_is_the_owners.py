"""Nobody browses anybody else's Blossom drive on a PosterChan node (2026-10-21).

"We need to disable external users from being able to browse a user's blossom files on a posterchan server, make sure
not to break our files implementation" / "basically only posterchan clients should be able to browse blossom" / "don't
break our file chooser". GET /blossom/list/<pubkey> answered anyone. It now needs BUD-02's `list` authorization signed
by that pubkey (what our clients send -- upload.js blossomListAuth) or by an admin of this node (Files' admin view).
A single blob by its hash stays public: every picture in every post is that request.
"""
import asyncio
import base64
import json
import time
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from app.routers import blossom as R
from app.services import blossom_service
from app.services.nostr import bip340
from app.services.nostr.event import build_event

ROOT = Path(__file__).resolve().parents[1]
OWNER_SK, OTHER_SK = bytes.fromhex("11" * 32), bytes.fromhex("22" * 32)
OWNER, OTHER = bip340.pubkey_from_seckey(OWNER_SK).hex(), bip340.pubkey_from_seckey(OTHER_SK).hex()


def _auth(sk, verb="list", ttl=600):
    ev = build_event(sk, 24242, verb, tags=[["t", verb], ["expiration", str(int(time.time()) + ttl)]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def _list(headers, admin=False):
    req = Request({"type": "http", "method": "GET", "path": "/list/" + OWNER, "query_string": b"",
                   "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                   "client": ("1.2.3.4", 1), "server": ("x", 443), "scheme": "https"})

    async def go():
        async def is_admin(db, pk):
            return admin
        with mock.patch.object(blossom_service, "is_enabled", lambda db: True), \
             mock.patch.object(blossom_service, "list_for_pubkey", lambda db, pk: []), \
             mock.patch.object(blossom_service, "names_for_pubkey", lambda db, pk: {}), \
             mock.patch.object(R, "_base_url", lambda r, db: "https://x/blossom"), \
             mock.patch.object(R, "_is_node_admin", is_admin):
            return await R.list_blobs(OWNER, req, db=None)
    return asyncio.run(go())


def test_a_stranger_with_no_signature_is_refused():
    assert _list({}).status_code == 401


def test_the_owner_lists_their_own_drive():
    assert _list({"authorization": _auth(OWNER_SK)}).status_code == 200


def test_somebody_elses_signature_is_refused_unless_they_are_an_admin():
    assert _list({"authorization": _auth(OTHER_SK)}).status_code == 403
    assert _list({"authorization": _auth(OTHER_SK)}, admin=True).status_code == 200


def test_an_upload_token_or_an_expired_one_does_not_list():
    assert _list({"authorization": _auth(OWNER_SK, verb="upload")}).status_code == 401
    assert _list({"authorization": _auth(OWNER_SK, ttl=-10)}).status_code == 401


def test_every_listing_our_client_makes_is_signed():
    """Files, the media picker, Folder Sync, Music, Go Live, the Meme Builder: a request that forgot the header would
    now show an empty drive."""
    import re
    js = ROOT / "static/js/client"
    calls = []
    for f in js.glob("*.js"):
        src = f.read_text()
        for m in re.finditer(r"fetch\([^;]*?'/list/'[^;]*", src):
            calls.append((f.name, m.group(0)))
        for m in re.finditer(r"_fetchTimeout\([^;]*?'/list/'[^;]*", src):
            calls.append((f.name, m.group(0)))
    assert len(calls) >= 7, calls
    for name, call in calls:
        assert "blossomListAuth" in call, (name, call[:200])


def test_the_signature_is_reused_not_asked_for_on_every_open():
    src = (ROOT / "static/js/client/upload.js").read_text()
    body = src[src.index("async function blossomListAuth(){"):][:1200]
    assert "hit.exp - now > 600" in body and "hit.pending" in body and "['t','list']" in body


def test_the_client_signature_cache_behaves():
    import subprocess
    r = subprocess.run(["node", "tests/client/blossom_list_auth_runtime.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr
