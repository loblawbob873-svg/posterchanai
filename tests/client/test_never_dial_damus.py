"""relay.damus.io is never dialled -- by the client or this node ("we should never connect to the damus relay, it's
super rate-limited", 2026-10-27). A community may still LIST it; nothing opens a socket to it.

Client: the SHIPPED relay.js against a recording WebSocket, through queryFrom with and without `allowBlocked` (the
opt-out Concord and the DM inbox lookup pass), subscribeFrom and a pool connect; and no default list names it.
Server: peer_ua.connect (every outbound relay socket) refuses without opening one, and the Concord tunnel and mirror
refuse it as a target.
"""
import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_client_never_opens_a_socket_to_damus():
    r = subprocess.run(["node", str(ROOT / "tests/client/never_dial_runtime.mjs")], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    opened = json.loads(r.stdout.strip().splitlines()[-1])["opened"]
    assert not [u for u in opened if "damus" in u], ("a socket was opened to damus", opened)
    assert any("nos.lol" in u for u in opened), ("the harness opened nothing at all, so it proves nothing", opened)


def test_no_default_relay_list_in_the_client_names_damus():
    hits = []
    for js in (ROOT / "static/js/client").glob("*.js"):
        for m in re.finditer(r"\[[^\[\]]*'wss://relay\.damus\.io[^\[\]]*\]", js.read_text(encoding="utf-8")):
            if "_queryFromBlockedHosts" not in js.read_text(encoding="utf-8")[max(0, m.start() - 60):m.start()]:
                hits.append(f"{js.name}: {m.group(0)[:80]}")
    assert not hits, hits


def test_the_server_never_opens_a_socket_to_damus(monkeypatch):
    import websockets
    from app.services.nostr import peer_ua
    opened = []
    monkeypatch.setattr(websockets, "connect", lambda url, **kw: opened.append(url))

    async def go():
        with pytest.raises(peer_ua.NeverDialled):
            await peer_ua.connect("wss://relay.damus.io", open_timeout=1)
        with pytest.raises(peer_ua.NeverDialled):
            async with peer_ua.connect("wss://RELAY.DAMUS.IO/", proxy="socks5h://127.0.0.1:9052"):
                pass
    asyncio.run(go())
    assert opened == [], opened


def test_the_concord_tunnel_and_mirror_refuse_it_as_a_target():
    from app.services import concord_tunnel
    from app.services.nostr_relay.concord_mirror import default_url_ok
    ok, why = concord_tunnel.check_target("wss://relay.damus.io")
    assert not ok and "never" in why
    assert "never dials" in default_url_ok("wss://relay.damus.io", {})
