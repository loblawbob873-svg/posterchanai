"""The relay CARRIES the VM-hosting kinds — through the SHIPPED write gate and the SHIPPED firehose.

The node-agent transport lost a round of debugging to exactly this failure: its kinds were missing
from the relay's hardcoded lists, so every request was refused as "not in web of trust" while both
ends were correct. A VM request is authored by an admin's or an assigned user's own npub, which is
almost never in the WoT, so without its own branch in `_on_event` and `_firehose_event` the feature
simply does not work, and nothing on the host side says so.

These drive real signed events through:
  * `RelayServer._on_event` with a gate where NOBODY is a member (the pre-fix refusal, verbatim);
  * `_firehose_event` and `_spawn_firehose`, extracted from thread.py's run loop by AST (they are
    closures) and executed against stub stores/gates — the same technique as
    tests/test_relay_live_word_reload.py;
  * the reload-upstream branch and `_read_config`, EXECUTED (not read as text), so turning the host on
    is picked up without a relay restart and turning it off closes the stranger path again.

WHAT A STRANGER MAY WRITE (a WoT member keeps the ordinary pre-VM-hosting treatment for all four kinds —
other applications use these numbers too):
  * 5310 only while THIS node runs a VM host, p-tagged to exactly this node, short-lived, nofederate;
  * 6310/7310 only when authored by this node (or a configured peer host — the phase-3 hook);
  * 31310 only from this node, a peer host or an operator.
"""
import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.nostr import bip340
from app.services.nostr.event import build_event, verify_event
from app.services.nostr_relay import thread
from app.services.nostr_relay.server import RelayServer
from app.services.vmhost import kinds

NODE_SK = bytes.fromhex("01" * 32)
REQUESTER_SK = bytes.fromhex("11" * 32)
NODE = bip340.pubkey_from_seckey(NODE_SK).hex()
OTHER = "ee" * 32
MEMBER = "cc" * 32


class NobodyGate:
    def __init__(self, members=(), operators=()):
        self._m, self._o = set(members), set(operators)
    def is_member(self, pk):        return pk in self._m or pk in self._o
    def is_operator(self, pk):      return pk in self._o
    def is_blocked(self, pk):       return False
    def is_puppet_event(self, ev):  return False
    def mark_bridged(self, pk):     pass
    def mark_bridged_identity(self, pk): pass


class Store:
    def __init__(self):
        self.added = []
    async def add_event(self, ev, origin="direct"):
        self.added.append(ev)
        return True
    async def has_event(self, eid):
        return False
    async def query(self, filters, hard_cap=None):
        return []
    async def is_repo_announced(self, *a):
        return False


def server(node_pubkey=NODE, wot=True, gate=None, vmhost=True, peers=(), cfg=None):
    s = object.__new__(RelayServer)
    s.gate = gate or NobodyGate()
    s.store = Store()
    s.subs = SimpleNamespace(fanout=lambda *a, **k: None)
    s.cfg = cfg if cfg is not None else {"wot_enabled": wot, "node_pubkey": node_pubkey,
                                         "vmhost_enabled": vmhost, "vmhost_peer_hosts": list(peers)}
    s.private_cb = None
    s.outbox_cb = None
    s._auth_pubkeys = {}
    s._call_seen = {}
    s._bridge_pubkeys = set()
    s.sent = []
    s._send = lambda conn, msg: s.sent.append(msg)
    s._refuse = lambda conn, eid, ev, why: s.sent.append(["OK", eid, False, why])
    return s


def deliver(s, ev):
    asyncio.run(s._on_event(object(), ev))
    return s.sent[-1]


def req(to=NODE, exp_in=90, nofed=True, content="ciphertext", sk=REQUESTER_SK, kind=kinds.REQ_KIND, p=None):
    tags = [["p", p or to]]
    if exp_in is not None:
        tags.append(["expiration", str(int(time.time()) + exp_in)])
    if nofed:
        tags.append(["nofederate"])
    return build_event(sk, kind, content, tags)


# ------------------------------------------------------------------------------------ write gate
def test_a_non_member_request_to_this_host_is_accepted_and_stored():
    s = server()
    ok = deliver(s, req())
    assert ok[2] is True, ok
    assert len(s.store.added) == 1


def test_a_non_member_request_is_refused_while_this_node_runs_no_vm_host():
    s = server(vmhost=False)
    ok = deliver(s, req())
    assert ok[2] is False, "with VM hosting off the relay must not store strangers' 5310s"
    assert s.store.added == []


def test_a_non_member_request_must_be_addressed_to_this_node_alone():
    extra = build_event(REQUESTER_SK, kinds.REQ_KIND, "ct",
                        [["p", NODE], ["p", OTHER], ["nofederate"],
                         ["expiration", str(int(time.time()) + 90)]])
    doubled = build_event(REQUESTER_SK, kinds.REQ_KIND, "ct",
                          [["p", NODE], ["p", NODE], ["nofederate"],
                           ["expiration", str(int(time.time()) + 90)]])
    s = server()
    assert deliver(s, extra)[2] is False, "a p-tag list is a fan-out list; one extra name is a relay abuse"
    assert deliver(s, doubled)[2] is False
    assert s.store.added == []


@pytest.mark.parametrize("label,ev,why", [
    ("addressed to another host", lambda: req(to=OTHER), "not addressed"),
    ("no expiration", lambda: req(exp_in=None), "expiration"),
    ("expiration too far", lambda: req(exp_in=kinds.REQ_MAX_EXPIRATION + 120), "expiration"),
    ("not nofederate", lambda: req(nofed=False), "nofederate"),
    ("oversized", lambda: req(content="x" * (kinds.MAX_CONTENT + 1)), "too large"),
])
def test_a_non_member_request_is_refused_when_it_breaks_a_rule(label, ev, why):
    s = server()
    ok = deliver(s, ev())
    assert ok[2] is False and why in ok[3], (label, ok)
    assert s.store.added == []


def test_with_no_node_key_nothing_counts_as_addressed_to_this_host():
    s = server(node_pubkey="")
    assert deliver(s, req())[2] is False


def test_a_member_may_carry_a_request_for_another_host():
    s = server(gate=NobodyGate(members={bip340.pubkey_from_seckey(REQUESTER_SK).hex()}))
    assert deliver(s, req(to=OTHER))[2] is True


@pytest.mark.parametrize("kind", [kinds.REQ_KIND, kinds.RES_KIND, kinds.PROGRESS_KIND])
def test_a_members_events_of_these_kinds_keep_the_ordinary_treatment(kind):
    """Other applications use 5310/6310/7310 too. Before VM hosting a member's such event was stored
    like any other; the new shape rules (expiration, nofederate, size) apply to STRANGERS only."""
    member_sk = bytes.fromhex("44" * 32)
    ev = build_event(member_sk, kind, "x" * (kinds.MAX_CONTENT + 10), [["p", OTHER]])
    for vmhost in (True, False):
        s = server(gate=NobodyGate(members={bip340.pubkey_from_seckey(member_sk).hex()}), vmhost=vmhost)
        assert deliver(s, ev)[2] is True, (kind, vmhost)


def result(sk, kind=kinds.RES_KIND, p=MEMBER):
    return build_event(sk, kind, "ct", [["e", "a" * 64], ["p", p], ["nofederate"],
                                        ["expiration", str(int(time.time()) + 300)]])


STRANGER_HOST_SK = bytes.fromhex("22" * 32)
PEER_SK = bytes.fromhex("23" * 32)
PEER = bip340.pubkey_from_seckey(PEER_SK).hex()


@pytest.mark.parametrize("kind", [kinds.RES_KIND, kinds.PROGRESS_KIND])
def test_a_strangers_result_is_refused_whoever_it_is_for(kind):
    """A result is only ever signed by a host. A stranger's 6310 p-tagged to one of our members is a
    stored, pushed event nobody on this relay asked for — the hole the first version left open."""
    s = server(gate=NobodyGate(members={MEMBER}, operators={"ab" * 32}))
    for p in (MEMBER, OTHER, NODE, "ab" * 32):
        assert deliver(s, result(STRANGER_HOST_SK, kind, p))[2] is False, p
    assert s.store.added == []


@pytest.mark.parametrize("kind", [kinds.RES_KIND, kinds.PROGRESS_KIND])
def test_this_nodes_own_result_to_a_stranger_client_is_accepted(kind):
    """The node's answer is p-tagged to the requester, who is NOT a member. Refusing it would make every
    request unanswerable — accepted here even with a gate that does not know the node key."""
    s = server()
    assert deliver(s, result(NODE_SK, kind, OTHER))[2] is True
    s = server(gate=NobodyGate(operators={NODE}))
    assert deliver(s, result(NODE_SK, kind, OTHER))[2] is True


def test_a_configured_peer_hosts_result_is_accepted_and_only_then():
    assert deliver(server(), result(PEER_SK, p=OTHER))[2] is False
    assert deliver(server(peers=[PEER]), result(PEER_SK, p=OTHER))[2] is True


def test_a_hosts_result_still_obeys_the_shape_rules():
    s = server()
    no_exp = build_event(NODE_SK, kinds.RES_KIND, "ct", [["e", "a" * 64], ["p", OTHER], ["nofederate"]])
    assert deliver(s, no_exp)[2] is False


def test_an_announcement_is_accepted_only_from_this_node_a_peer_or_an_operator():
    body, d = '{"v":1}', [["d", kinds.ANNOUNCE_D]]
    stranger = build_event(bytes.fromhex("33" * 32), kinds.ANNOUNCE_KIND, body, d)
    foreign = build_event(bytes.fromhex("33" * 32), kinds.ANNOUNCE_KIND, '{}', [["d", "something-else"]])
    op_sk = bytes.fromhex("34" * 32)
    op = build_event(op_sk, kinds.ANNOUNCE_KIND, body, d)
    s = server(gate=NobodyGate(operators={bip340.pubkey_from_seckey(op_sk).hex()}), peers=[PEER])
    assert deliver(s, stranger)[2] is False, "anybody's 31310 was accepted: a free public billboard"
    assert deliver(s, foreign)[2] is False, "another app's 31310 gets the ordinary WoT gate"
    assert deliver(s, build_event(NODE_SK, kinds.ANNOUNCE_KIND, body, d))[2] is True
    assert deliver(s, build_event(PEER_SK, kinds.ANNOUNCE_KIND, body, d))[2] is True
    assert deliver(s, op)[2] is True


def test_the_ordinary_gate_still_refuses_a_non_member_note():
    s = server()
    ok = deliver(s, build_event(REQUESTER_SK, 1, "hello"))
    assert ok[2] is False and "web of trust" in ok[3]


def test_ciphertext_kinds_are_never_word_filtered():
    from app.services.nostr_relay.langfilter import _NEVER_WORD_FILTERED
    for k in kinds.TRANSPORT_KINDS:
        assert k in _NEVER_WORD_FILTERED


# ------------------------------------------------------------------------------------ firehose
SOURCE = Path(thread.__file__).read_text()


def _fn(name):
    tree = ast.parse(SOURCE)
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)


def firehose_env(node_pubkey=NODE, members=(), vmhost=True, peers=()):
    cfg = {"blocked_words": set(), "blocked_langs": set(), "blocked_relays": [], "operator": [],
           "block_bridged": False, "fetch_ancestors": False, "node_pubkey": node_pubkey,
           "vmhost_enabled": vmhost, "vmhost_peer_hosts": list(peers)}
    store = SimpleNamespace(has_event=AsyncMock(return_value=False), add_event=AsyncMock(return_value=True))
    gate = NobodyGate(members=members)
    srv = SimpleNamespace(subs=SimpleNamespace(fanout=Mock()), _send=Mock())
    env = {**vars(thread), "cfg": cfg, "_bl": set(), "_bw": set(), "store": store, "gate": gate,
           "server": srv, "verify_event": verify_event, "_FH_SEEN": set(), "_fh_mark": Mock()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[_fn("_firehose_event")], type_ignores=[])),
                 thread.__file__, "exec"), env)
    return env, store


def test_the_firehose_keeps_a_non_member_request_addressed_to_this_host():
    env, store = firehose_env()
    asyncio.run(env["_firehose_event"](req()))
    assert store.add_event.await_count == 1


@pytest.mark.parametrize("label,ev", [("another host", lambda: req(to=OTHER)),
                                      ("no expiration", lambda: req(exp_in=None))])
def test_the_firehose_drops_what_is_not_for_this_host(label, ev):
    env, store = firehose_env()
    asyncio.run(env["_firehose_event"](ev()))
    assert store.add_event.await_count == 0, label


def test_the_firehose_applies_the_same_stranger_rules_as_the_write_gate():
    off, off_store = firehose_env(vmhost=False)
    asyncio.run(off["_firehose_event"](req()))
    assert off_store.add_event.await_count == 0, "no VM host here: a stranger's request is not ours"
    env, store = firehose_env(members={MEMBER})
    asyncio.run(env["_firehose_event"](result(STRANGER_HOST_SK, p=MEMBER)))
    asyncio.run(env["_firehose_event"](build_event(bytes.fromhex("33" * 32), kinds.ANNOUNCE_KIND, "{}",
                                                   [["d", kinds.ANNOUNCE_D]])))
    assert store.add_event.await_count == 0
    asyncio.run(env["_firehose_event"](result(NODE_SK, p=OTHER)))
    assert store.add_event.await_count == 1


def spawn_env(vmhost_enabled, node_pubkey=NODE):
    calls = []

    async def run_firehose(upstream, kinds_, cb, stop, direct, **kw):
        calls.append((list(kinds_), kw.get("extra"), kw.get("label")))

    cfg = {"wot_enabled": True, "send_only": False, "firehose_enabled": True, "upstream": ["wss://up"],
           "ingest_kinds": [1], "direct": False, "firehose_max_relays": 0, "operator": ["op"],
           "dvm_enabled": False, "agent_enabled": False, "vmhost_enabled": vmhost_enabled,
           "node_pubkey": node_pubkey}
    firehose_mod = SimpleNamespace(run_firehose=run_firehose, _STAGGER_SPAN=1.0)
    import sys
    env = {**vars(thread), "cfg": cfg, "_firehose": {"tasks": [], "stop": None}, "_firehose_event": None}
    fn = _fn("_spawn_firehose")
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), thread.__file__, "exec"), env)

    async def go():
        saved = sys.modules.get("app.services.nostr_relay.firehose")
        sys.modules["app.services.nostr_relay.firehose"] = firehose_mod
        try:
            env["_spawn_firehose"]()
            await asyncio.gather(*env["_firehose"]["tasks"])
        finally:
            if saved is not None:
                sys.modules["app.services.nostr_relay.firehose"] = saved
            else:
                sys.modules.pop("app.services.nostr_relay.firehose", None)
    asyncio.run(go())
    return calls


def test_the_firehose_subscribes_to_the_vm_kinds_for_this_node_when_enabled():
    on = spawn_env(True)
    vm = [c for c in on if set(c[0]) & set(kinds.TRANSPORT_KINDS)]
    assert len(vm) == 1
    assert sorted(vm[0][0]) == sorted(kinds.TRANSPORT_KINDS)
    assert vm[0][1] == {"#p": [NODE]}, "keyed on the node's own key, never the operator list"
    off = spawn_env(False)
    assert not [c for c in off if set(c[0]) & set(kinds.TRANSPORT_KINDS)]


def _reload_upstream_fn(cfg, fresh):
    """The SHIPPED `reload-upstream` control branch, lifted out of the relay's run loop (it is inline
    in a closure) and executed against stubs — the technique tests/test_relay_live_word_reload.py uses."""
    tree = ast.parse(SOURCE)
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "cmd.get('cmd') == 'reload-upstream'")
    fn = ast.AsyncFunctionDef(name="reload_upstream", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=branch.body,
        decorator_list=[])
    order = []

    async def restart_firehose():
        order.append(("firehose respawned", cfg.get("vmhost_enabled")))
    env = {**vars(thread), "cfg": cfg, "_read_config": lambda: fresh, "_restart_firehose": restart_firehose,
           "outbox": SimpleNamespace(upstream=None), "private": None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), thread.__file__, "exec"), env)
    return env["reload_upstream"], order


def _fresh(**over):
    base = {"upstream": [], "private_relays": [], "firehose_max_relays": 0, "ingest_kinds": [1],
            "operator": [], "dvm_enabled": False, "agent_enabled": False, "vmhost_enabled": False,
            "node_pubkey": NODE, "vmhost_peer_hosts": []}
    base.update(over)
    return base


def test_reload_upstream_opens_and_closes_the_stranger_path_on_the_live_server():
    """The relay server reads `cfg` live; the reload branch must refresh the VM-hosting keys IN that
    dict, before the firehose is respawned from it. Driven end to end: a stranger's request is refused,
    the host is turned on (reload) and it is accepted, turned off (reload) and refused again."""
    cfg = _fresh()
    cfg.update({"wot_enabled": True})
    s = server(cfg=cfg)
    assert deliver(s, req())[2] is False
    reload, order = _reload_upstream_fn(cfg, _fresh(vmhost_enabled=True, vmhost_peer_hosts=[PEER]))
    asyncio.run(reload())
    assert order == [("firehose respawned", True)], "the switch must be refreshed BEFORE the respawn"
    assert deliver(s, req())[2] is True
    assert deliver(s, result(PEER_SK, p=OTHER))[2] is True, "peer hosts refresh live too"
    reload, _ = _reload_upstream_fn(cfg, _fresh(vmhost_enabled=False))
    asyncio.run(reload())
    assert deliver(s, req())[2] is False


def test_read_config_carries_the_switch_the_node_key_and_the_peer_hook(monkeypatch):
    """`_read_config` EXECUTED against a stub datastore: what the relay subprocess actually builds."""
    from app.services import keystore, settings_store
    import app.database as database
    monkeypatch.setattr(database, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(settings_store, "load_local", lambda: None)
    monkeypatch.setattr(settings_store, "hydrate_from_db", lambda db: 0)
    monkeypatch.setattr(thread, "_collect_operator_pubkeys", lambda db: [])
    monkeypatch.setattr(thread, "_collect_preserve_pubkeys", lambda db: [])
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: NODE_SK.hex())
    monkeypatch.setattr(keystore, "get_bridge_secret", lambda: None, raising=False)
    monkeypatch.setitem(settings_store._CACHE, "vmhost_peer_hosts", "")
    for value, expect in (("true", True), ("false", False), ("", False)):
        monkeypatch.setitem(settings_store._CACHE, "vmhost_enabled", value)
        cfg = thread._read_config()
        assert cfg["vmhost_enabled"] is expect, value
        assert cfg["node_pubkey"] == NODE
        assert cfg["vmhost_peer_hosts"] == [], "no peer line configured: no peer is trusted"


def _read_config_env(monkeypatch, enabled, peer_line):
    from app.services import keystore, settings_store
    import app.database as database
    monkeypatch.setattr(database, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(settings_store, "load_local", lambda: None)
    monkeypatch.setattr(settings_store, "hydrate_from_db", lambda db: 0)
    monkeypatch.setattr(thread, "_collect_operator_pubkeys", lambda db: [])
    monkeypatch.setattr(thread, "_collect_preserve_pubkeys", lambda db: [])
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: NODE_SK.hex())
    monkeypatch.setattr(keystore, "get_bridge_secret", lambda: None, raising=False)
    monkeypatch.setitem(settings_store._CACHE, "vmhost_enabled", "true" if enabled else "false")
    monkeypatch.setitem(settings_store._CACHE, "vmhost_peer_hosts", peer_line)
    return thread._read_config()


def _npub(hexkey):
    from app.services.nostr import nostr_service
    return nostr_service.npub_of(hexkey)


def test_a_configured_peer_hosts_results_pass_the_gate_through_the_shipped_hook(monkeypatch):
    """END TO END across the seam phase 1 left as a hook: the admin's `vmhost_peer_hosts` line (the exact
    `npub relay https` format migrate.py parses) → `_read_config` → the LIVE write gate and the firehose.
    A peer host's 6310/7310/31310 are accepted; a stranger host's are not; a malformed line trusts nobody;
    and with VM hosting off the peer list widens nothing."""
    stranger_host = STRANGER_HOST_SK
    line = f"{_npub(PEER)} wss://peer.example/relay https://peer.example\n# a comment\nnot a peer line"
    cfg = _read_config_env(monkeypatch, True, line)
    assert cfg["vmhost_peer_hosts"] == [PEER], cfg["vmhost_peer_hosts"]

    s = server(cfg={"wot_enabled": True, **cfg})
    for kind in (kinds.RES_KIND, kinds.PROGRESS_KIND):
        assert deliver(s, result(PEER_SK, kind, p=NODE))[2] is True, f"a peer host's {kind} was refused"
        assert deliver(s, result(stranger_host, kind, p=NODE))[2] is False, f"a stranger host's {kind} passed"
    body, d = '{"v":1}', [["d", kinds.ANNOUNCE_D]]
    assert deliver(s, build_event(PEER_SK, kinds.ANNOUNCE_KIND, body, d))[2] is True
    assert deliver(s, build_event(stranger_host, kinds.ANNOUNCE_KIND, body, d))[2] is False

    env, store = firehose_env(peers=cfg["vmhost_peer_hosts"])
    asyncio.run(env["_firehose_event"](result(PEER_SK, p=NODE)))
    asyncio.run(env["_firehose_event"](result(stranger_host, p=NODE)))
    assert [c.args[0]["pubkey"] for c in store.add_event.await_args_list] == [PEER]

    # Hosting off: the same line trusts nobody.
    off = _read_config_env(monkeypatch, False, line)
    assert off["vmhost_peer_hosts"] == []
    assert deliver(server(cfg={"wot_enabled": True, **off}), result(PEER_SK, p=NODE))[2] is False
    # A line migrate.py would reject (no https) is trusted by neither the host nor the relay.
    bad = _read_config_env(monkeypatch, True, f"{_npub(PEER)} wss://peer.example/relay")
    assert bad["vmhost_peer_hosts"] == []
    assert deliver(server(cfg={"wot_enabled": True, **bad}), result(PEER_SK, p=NODE))[2] is False


def test_the_relay_and_the_host_parse_the_peer_line_with_one_rule():
    from app.services.vmhost import migrate
    line = f"{_npub(PEER)} wss://peer.example/relay https://peer.example\n{NODE} wss://x/r https://x"
    peers, bad = migrate.parse_peer_hosts(line)
    assert thread._vmhost_peer_hosts(True, line, NODE) == sorted({p.pubkey for p in peers} - {NODE})
    assert thread._vmhost_peer_hosts(True, line, NODE) == [PEER], "the node itself is never its own peer"
