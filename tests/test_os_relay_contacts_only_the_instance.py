"""In --os-mode the relay contacts NOTHING but the instance chosen on the welcome screen.

PosterChanOS's local relay (app/services/nostr_relay/osmode.py) is the server's relay code with every server
job taken away. "Taken away" is only true if nothing in the process dials out on its own, so this runs the
SHIPPED process (`relay_main.py --os-mode`) with a recorder injected into its socket layer (every connect()
and every name lookup, from any thread) against a REAL instance (the server role's RelayServer, with
negentropy and NIP-42 exactly as on a node) whose data is a trap: the owner's relay list names other relays,
their follows' relay lists name more, a stranger posts, and a fediverse-looking NIP-05 is everywhere. The
cache must end up with the owner's own events, their PRIVATE document (fetched through the owner's client
signing the instance's AUTH -- `PC-SIGN-AUTH`), what the people they follow posted, and nothing of the
stranger's -- having opened connections to the instance and to nowhere else.

The second test is the other half of the rule: with NO instance chosen the relay makes no outbound
connection at all, and with the desktop's Tor on it does not dial the instance either.
"""
import asyncio
import json
import os
import shutil
import time

import pytest

from tests import os_relay_harness as H


def _wait_until(fn, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = fn()
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError("timed out waiting for " + what)


def _seed(inst):
    owner, friend = H.pk(H.OWNER_SK), H.pk(H.FRIEND_SK)
    now = int(time.time())
    evs = {
        "own_post": H.ev(H.OWNER_SK, 1, "my own post", created_at=now - 3600),
        "own_follows": H.ev(H.OWNER_SK, 3, "", [["p", friend]], created_at=now - 7200),
        "own_relays": H.ev(H.OWNER_SK, 10002, "", [["r", "wss://relay.elsewhere.example"],
                                                    ["r", "wss://nos.lol"]], created_at=now - 7200),
        "own_profile": H.ev(H.OWNER_SK, 0, json.dumps({"name": "me", "nip05": "me@fedi.example"}),
                            created_at=now - 7200),
        "private_doc": H.ev(H.OWNER_SK, 30078, "ciphertext", [["d", "pcai:note:abc"], ["l", "pcai-notes"]],
                            created_at=now - 600),
        "friend_post": H.ev(H.FRIEND_SK, 1, "friend says hi", created_at=now - 1800),
        "friend_relays": H.ev(H.FRIEND_SK, 10002, "", [["r", "wss://relay.friend.example"]], created_at=now - 1800),
        "mention": H.ev(H.STRANGER_SK, 1, "hey @me", [["p", owner]], created_at=now - 900),
        "stranger_post": H.ev(H.STRANGER_SK, 1, "nobody asked", created_at=now - 300),
        "stranger_profile": H.ev(H.STRANGER_SK, 0, json.dumps({"nip05": "x@mastodon.example"}), created_at=now - 300),
    }
    assert inst.add(evs.values()) == len(evs)
    return evs, owner


def test_os_mode_syncs_its_scope_from_the_instance_and_contacts_nothing_else():
    tmp = H.tmpdir()
    inst = H.Instance(tmp).start()
    relay = H.LocalRelay(tmp, instance_url=inst.url, record=True)
    try:
        evs, owner = _seed(inst)
        relay.start()

        async def session():
            # The owner's client: authenticates (that is how the relay learns whose cache it is) and stays
            # connected to answer the relay's request to sign the INSTANCE's challenge.
            async with H.Client(relay.url, H.OWNER_SK, sign_for=inst.relay_url) as c:
                ok, why = await c.auth()
                assert ok, why

                async def have(ids, *filters):
                    got = {e["id"] for e in await c.query(*filters)}
                    return ids <= got
                want_public = {evs[k]["id"] for k in ("own_post", "own_follows", "own_relays", "own_profile",
                                                       "friend_post", "mention")}
                deadline = time.time() + 120
                while time.time() < deadline:
                    pub = await have(want_public, {"ids": sorted(want_public)})
                    priv = await have({evs["private_doc"]["id"]},
                                      {"authors": [owner], "kinds": [30078]})
                    if pub and priv:
                        break
                    await asyncio.sleep(1)
                held = {e["id"] for e in await c.query({"limit": 500})}
                held |= {e["id"] for e in await c.query({"authors": [owner], "kinds": [30078]})}
                return held, c.signed
        held, signed = asyncio.run(session())

        missing = [k for k in ("own_post", "own_follows", "own_relays", "own_profile", "private_doc",
                               "friend_post", "mention") if evs[k]["id"] not in held]
        assert not missing, ("the cache did not sync its scope from the instance", missing, relay.output()[-2500:])
        assert evs["stranger_post"]["id"] not in held, "a stranger's post was synced into the cache"
        assert signed >= 1, "the private document arrived without the owner's client signing the instance's AUTH"

        allowed = {("127.0.0.1", inst.http_port), ("127.0.0.1", inst.ws_port)}
        conns = relay.connections()
        dials = [tuple(x[1]) for x in conns if x[0] == "connect"]
        assert dials, "the recorder saw no connection at all -- it is not measuring anything"
        strays = sorted({d for d in dials if d not in allowed})
        assert not strays, ("os-mode dialled something other than the chosen instance", strays)
        names = sorted({x[1] for x in conns if x[0] == "resolve"} - {"127.0.0.1", "localhost"})
        assert not names, ("os-mode looked up hosts other than the instance", names)
        # Loopback only, by construction.
        assert relay.url.startswith("ws://127.0.0.1:")
    finally:
        relay.stop()
        inst.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("case", ["no-instance", "tor-on"])
def test_without_a_usable_instance_the_relay_dials_nothing(case):
    tmp = H.tmpdir()
    inst = H.Instance(tmp).start()
    relay = H.LocalRelay(tmp, instance_url="" if case == "no-instance" else inst.url, record=True)
    if case == "tor-on":
        with open(relay.desktop_cfg, "w") as f:
            json.dump({"instance": inst.url, "tor": {"enabled": True}}, f)
    try:
        _seed(inst)
        relay.start()

        async def session():
            async with H.Client(relay.url, H.OWNER_SK, sign_for=inst.relay_url) as c:
                ok, why = await c.auth()
                assert ok, why
                # A cache still works: what the client writes, it can read back.
                e = H.ev(H.OWNER_SK, 1, "written locally")
                assert (await c.publish(e))[0]
                assert [x["id"] for x in await c.query({"ids": [e["id"]]})] == [e["id"]]
                await asyncio.sleep(8)          # longer than PC_RELAY_SYNC_SECONDS: a pass would have run
        asyncio.run(session())
        dials = [x for x in relay.connections() if x[0] in ("connect", "resolve")]
        assert not dials, (case, "the relay dialled out with no usable instance", dials)
    finally:
        relay.stop()
        inst.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_relay_refuses_to_listen_beyond_loopback():
    from app.services.nostr_relay import osmode
    with pytest.raises(SystemExit):
        osmode.read_config({"PC_RELAY_BIND": "0.0.0.0", "HOME": "/nonexistent"})
    assert osmode.read_config({"HOME": "/nonexistent"})["bind"] == "127.0.0.1"
    assert not os.environ.get("PC_RELAY_BIND")
