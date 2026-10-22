"""A cold device reads this node's Concord copy at once (2026-10-22, Firefox over Tor: "no rooms are loading" -- every
room tunnelled to every relay it lists, because which rooms this node mirrors was known only to the browser that had
registered them, and a fresh browser could not register: the stream authors come from control events it had not read).

The node already holds every registration; concord_mirror.member_state answers one member's, the relay process
serves it as the 'concord-mirror-state' aggregate, and /client/concord/mirror-state hands it to that member only."""
import asyncio
import time
from types import SimpleNamespace

from app.services.nostr_relay import aggregates
from app.services.nostr_relay import concord_mirror as cm

ME, OTHER = "1" * 64, "2" * 64
G1, G2, G3, G4 = "a" * 64, "b" * 64, "c" * 64, "4" * 64


class _Store:
    async def kv_get(self, k):
        return None

    async def kv_set(self, k, v):
        return None


def _mirror():
    m = cm.ConcordMirror(_Store(), None, {"concord_mirror": True, "concord_mirror_proxy": ""})
    now = int(time.time())
    m.regs = {
        ME: {G1: {"relays": ["wss://relay.dreamith.to"], "authors": ["d" * 64], "at": now, "skipped": ["wss://odd.example:4444"]},
             G2: {"relays": ["wss://jskitty.com/nostr"], "authors": ["e" * 64], "at": now},              # recorded before `skipped`
             G3: {"relays": ["wss://x"], "authors": ["f" * 64], "at": now, "skipped": [], "hosted": True},
             G4: {"relays": ["wss://jskitty.com/nostr"], "authors": ["5" * 64], "at": now, "skipped": []}},
        OTHER: {"9" * 64: {"relays": ["wss://y"], "authors": ["8" * 64], "at": now, "skipped": []}},
    }
    m.links = {"wss://relay.dreamith.to": SimpleNamespace(refused=False)}
    return m


def test_a_member_gets_their_own_live_registrations_and_what_to_read_directly():
    st = _mirror().member_state(ME)
    assert set(st) == {G1, G2, G4}, "hosted replicas and other members' rooms are not this member's mirror"
    assert st[G1]["authors"] == ["d" * 64] and st[G1]["direct"] == ["wss://odd.example:4444"]
    assert st[G2]["direct"] is None, "recorded before `skipped` was kept: unknown, never 'nothing to read directly'"
    assert st[G4]["direct"] == ["wss://jskitty.com/nostr"], "a relay the node holds no link for is read directly"


def test_an_expired_registration_is_not_reported():
    m = _mirror()
    m.regs[ME][G1]["at"] = int(time.time()) - cm.TTL_SECONDS - 10
    assert G1 not in m.member_state(ME)


def test_the_relay_process_answers_it_as_an_aggregate():
    store = SimpleNamespace(store=None, concord_mirror=_mirror())
    aggregates._CACHE.clear()
    out = aggregates.compute(store, "concord-mirror-state", {"member": ME})
    assert set(out["groups"]) == {G1, G2, G4}
    assert aggregates.compute(SimpleNamespace(store=None), "concord-mirror-state", {"member": ME}) == {"groups": {}}


def test_the_endpoint_answers_only_the_caller(monkeypatch):
    from app.routers import concord_tunnel
    from app.services.nostr import nostr_service
    asked = []

    def ask(what, args=None, **kw):
        asked.append((what, args))
        return {"groups": {G1: {"authors": ["d" * 64], "direct": [], "at": 1}}}
    monkeypatch.setattr(aggregates, "ask", ask)
    user = SimpleNamespace(nostr_npub=nostr_service.npub_of(ME))
    out = asyncio.run(concord_tunnel.mirror_state(user))
    assert asked == [("concord-mirror-state", {"member": ME})] and set(out["groups"]) == {G1}
    assert asyncio.run(concord_tunnel.mirror_state(SimpleNamespace(nostr_npub=""))) == {"groups": {}}


def test_a_registration_keeps_the_relays_it_said_to_read_directly():
    src = open(cm.__file__).read()
    assert '"skipped": skipped, **({"hosted": True} if hosted else {})' in src
