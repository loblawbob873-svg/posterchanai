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
  * the reload-upstream branch, so turning the host on is picked up without a relay restart.
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


def server(node_pubkey=NODE, wot=True, gate=None):
    s = object.__new__(RelayServer)
    s.gate = gate or NobodyGate()
    s.store = Store()
    s.subs = SimpleNamespace(fanout=lambda *a, **k: None)
    s.cfg = {"wot_enabled": wot, "node_pubkey": node_pubkey}
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


def test_results_route_to_the_host_or_its_users_only():
    to_member = build_event(bytes.fromhex("22" * 32), kinds.RES_KIND, "ct",
                            [["e", "a" * 64], ["p", MEMBER], ["nofederate"],
                             ["expiration", str(int(time.time()) + 300)]])
    to_stranger = build_event(bytes.fromhex("22" * 32), kinds.RES_KIND, "ct",
                              [["e", "a" * 64], ["p", OTHER], ["nofederate"],
                               ["expiration", str(int(time.time()) + 300)]])
    progress_to_node = build_event(bytes.fromhex("22" * 32), kinds.PROGRESS_KIND, "ct",
                                   [["e", "a" * 64], ["p", NODE], ["nofederate"],
                                    ["expiration", str(int(time.time()) + 300)]])
    s = server(gate=NobodyGate(members={MEMBER}))
    assert deliver(s, to_member)[2] is True
    assert deliver(s, to_stranger)[2] is False
    assert deliver(s, progress_to_node)[2] is True


def test_the_hosts_own_result_to_a_stranger_client_is_accepted():
    """The node key is an operator (hence a member), and its answer is p-tagged to the requester —
    who is NOT a member. Refusing it would make every request unanswerable."""
    res = build_event(NODE_SK, kinds.RES_KIND, "ct", [["e", "a" * 64], ["p", OTHER], ["nofederate"],
                                                      ["expiration", str(int(time.time()) + 300)]])
    s = server(gate=NobodyGate(operators={NODE}))
    assert deliver(s, res)[2] is True


def test_an_announcement_from_anyone_is_accepted_but_only_with_our_d_tag():
    good = build_event(bytes.fromhex("33" * 32), kinds.ANNOUNCE_KIND, '{"v":1}', [["d", kinds.ANNOUNCE_D]])
    foreign = build_event(bytes.fromhex("33" * 32), kinds.ANNOUNCE_KIND, '{}', [["d", "something-else"]])
    s = server()
    assert deliver(s, good)[2] is True
    assert deliver(s, foreign)[2] is False, "another app's 31310 gets the ordinary WoT gate"


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


def firehose_env(node_pubkey=NODE, members=()):
    cfg = {"blocked_words": set(), "blocked_langs": set(), "blocked_relays": [], "operator": [],
           "block_bridged": False, "fetch_ancestors": False, "node_pubkey": node_pubkey}
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


def test_reload_upstream_refreshes_the_vmhost_switch_and_node_key():
    tree = ast.parse(SOURCE)
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "cmd.get('cmd') == 'reload-upstream'")
    body = ast.unparse(branch)
    assert "cfg['vmhost_enabled'] = fresh['vmhost_enabled']" in body
    assert "cfg['node_pubkey'] = fresh['node_pubkey']" in body
    assert body.index("cfg['vmhost_enabled']") < body.index("await _restart_firehose()"), \
        "the switch must be refreshed BEFORE the firehose is respawned from cfg"


def test_read_config_reads_the_switch_and_the_node_key():
    fn = ast.unparse(_fn("_read_config"))
    assert "'vmhost_enabled': gb('vmhost_enabled', False)" in fn
    assert "cfg['node_pubkey'] = _node_pubkey()" in fn
