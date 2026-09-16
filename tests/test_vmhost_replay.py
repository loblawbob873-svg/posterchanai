"""A stored request is handled ONCE — across a host-service restart, and against a flood.

The relay STORES 5310s (until their expiration) and replays every stored match to a new subscription.
The first version subscribed with no `since` and kept its seen-ids in the Transport object, which a
settings Save throws away and rebuilds. So every restart re-ran whatever the last two minutes held:
a console ticket re-issued (and the VNC password rotated under whoever was mid-login), and a refused
mutating op — refusals are deliberately not journaled — run again as if the user had just asked.

And the seen-set evicted by COUNT: 2000 cheap valid requests from one allowed user pushed a victim's
event id out, after which a replay of the victim's captured request was "new". The set is now kept by
TIME (an id lives as long as its event could still pass the clock window) with a hard cap that REFUSES
new requests instead of forgetting young ids.

The lifecycle tests drive the SHIPPED `transport.start/stop/reload` with only the network stubbed: a
fake relay subscription that replays its stored events to every new subscriber, exactly as a relay does.
"""
import asyncio
import json
import time

import pytest

from app.services.nostr import bip340, nip44
from app.services.vmhost import domainxml, transport
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.journal import SeenIds
from tests.vmhost_fake import FakeBackend

NODE_SK = bytes.fromhex("01" * 32)
USER_SK = bytes.fromhex("03" * 32)
NODE = bip340.pubkey_from_seckey(NODE_SK).hex()
USER = bip340.pubkey_from_seckey(USER_SK).hex()
U1 = "11111111-1111-4111-8111-111111111111"


# ------------------------------------------------------------------------------------ L1: the seen-set
def test_a_flood_cannot_push_a_young_id_out():
    clock = [1000.0]
    seen = SeenIds(ttl=150, cap=5, now=lambda: clock[0])
    assert seen.add("victim") == "ok"
    for i in range(4):
        assert seen.add(f"flood{i}") == "ok"
    assert seen.add("flood-more") == "full", "at the cap a NEW request is refused…"
    assert "victim" in seen, "…and the victim's id is never evicted to make room"
    assert seen.add("victim") == "dup"
    clock[0] += 151
    assert "victim" not in seen, "past the clock window the id may go: the event itself is now refused"
    assert seen.add("fresh") == "ok"


def test_ids_are_kept_for_the_whole_clock_window():
    clock = [1000.0]
    seen = SeenIds(ttl=transport.REQ_MAX_AGE + transport.REQ_MAX_FUTURE, now=lambda: clock[0])
    seen.add("a")
    for i in range(5000):                      # far past the old 2000-entry count cap
        seen.add(f"x{i}")
    clock[0] += transport.REQ_MAX_AGE + transport.REQ_MAX_FUTURE - 1
    assert "a" in seen


def test_a_full_seen_set_drops_the_request_unanswered(tmp_path):
    from tests.test_vmhost_transport import setup
    tr, svc, be, published = setup(tmp_path)
    tr.seen = SeenIds(ttl=150, cap=1)
    reqs = [transport.build_request(USER_SK, NODE, "host.whoami", {}, f"w{i}") for i in range(2)]

    async def go():
        await svc.refresh_index()
        return [await tr.on_event(r) for r in reqs]
    tr.service.cfg.allowed_pubkeys = [USER]
    a, b = asyncio.run(go())
    assert a is not None and b is None and len(published) == 1


# ------------------------------------------------------------------------------------ M3: restarts
class FakeRelay:
    """Stores what it is given and replays every stored match to each new subscription."""

    def __init__(self):
        self.stored = []
        self.filters = []
        self.handlers = []
        self.published = []

    async def subscribe(self, relay, filters, handler, stop, direct=False, **kw):
        self.filters.append(filters)
        self.handlers.append(handler)
        since = min((f.get("since", 0) for f in filters), default=0)
        for ev in list(self.stored):
            if ev["created_at"] >= since:
                await handler(ev)
        await stop.wait()

    async def publish(self, relay, ev, direct=False):
        self.published.append(ev)
        return 1


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    from app.services import nostr_dvm
    from app.services.nostr import relay as nostr_relay
    from app.services.vmhost import backend as backend_mod
    from app.services.vmhost import config as config_mod

    root = tmp_path / "vms"
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), allowed_pubkeys=[USER], announce=False)
    be = FakeBackend()
    be.add_domain(U1, "alpha", state="shutoff", meta=domainxml.VmMeta(owner=NODE, assigned=[USER]))
    relay = FakeRelay()
    monkeypatch.setattr(nostr_dvm, "node_seckey", lambda: NODE_SK)
    monkeypatch.setattr(nostr_dvm, "node_pubkey", lambda: NODE)
    monkeypatch.setattr(nostr_dvm, "relay_url", lambda *a, **k: "ws://relay.test")
    monkeypatch.setattr(nostr_relay, "subscribe", relay.subscribe)
    monkeypatch.setattr(nostr_relay, "publish", relay.publish)
    monkeypatch.setattr(backend_mod, "make_backend", lambda c: be)
    monkeypatch.setattr(config_mod, "current", lambda: cfg)

    async def admins():
        return set()
    from app.services.vmhost import service as service_mod
    real_init = service_mod.VmHostService.__init__

    def init(self, *a, **kw):
        kw.setdefault("admin_provider", admins)
        real_init(self, *a, **kw)
    monkeypatch.setattr(service_mod.VmHostService, "__init__", init)
    monkeypatch.setattr(transport, "_state", {"stop": None, "tasks": [], "lock_fd": None,
                                              "transport": None, "error": ""})
    return cfg, be, relay


async def _settle(relay, n_subs):
    for _ in range(400):
        if len(relay.handlers) >= n_subs and not transport._tasks - set(transport._state["tasks"]):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    pending = [t for t in transport._tasks if t not in transport._state["tasks"]]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def replies(relay):
    return [json.loads(nip44.decrypt_from(USER_SK, bytes.fromhex(NODE), e["content"]))
            for e in relay.published if e["kind"] == 6310]


def test_the_subscription_does_not_ask_for_the_whole_stored_history(lifecycle):
    cfg, be, relay = lifecycle

    async def go():
        transport.start()
        await _settle(relay, 1)
        await transport.stop()
    asyncio.run(go())
    [filters] = relay.filters
    since = filters[0].get("since")
    assert since is not None, "no `since`: the relay replays every stored request on every restart"
    assert time.time() - transport.REQ_MAX_AGE - 5 <= since <= time.time()


@pytest.mark.parametrize("simulate_new_process", [False, True])
def test_a_restart_does_not_rerun_a_console_ticket_or_a_refused_op(lifecycle, simulate_new_process):
    cfg, be, relay = lifecycle
    be.domains[U1]["state"] = "running"
    ticket_req = transport.build_request(USER_SK, NODE, "console.ticket", {"vm": U1}, "tk-1")
    refused = transport.build_request(USER_SK, NODE, "vm.power", {"vm": U1, "action": "start"}, "pw-1")
    relay.stored = [ticket_req, refused]

    async def go():
        transport.start()
        await _settle(relay, 1)
        first = replies(relay)
        pw_calls = [c for c in be.calls if c[0] in ("set_vnc_password", "start")]
        if simulate_new_process:
            transport._SEEN.clear()          # a new process: only what was written to disk survives
        await transport.reload()             # what a settings Save does
        await _settle(relay, 2)
        await transport.stop()
        return first, pw_calls
    first, calls_before = asyncio.run(go())
    assert len(first) == 2
    assert {r["id"] for r in first} == {"tk-1", "pw-1"}
    assert [r for r in first if r["id"] == "pw-1"][0]["error"]["code"] == "conflict"
    assert len(relay.handlers) == 2, "the restart must really have re-subscribed"
    assert len(replies(relay)) == 2, "a restart re-ran a stored request"
    assert [c for c in be.calls if c[0] in ("set_vnc_password", "start")] == calls_before
