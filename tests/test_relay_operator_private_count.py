"""The relay COUNTs private documents (NIP-78, kinds 78/30078) for THIS node's operator key -- and only counts.

Admin -> Identities decides "signed up and never did anything" (and offers those accounts for removal) from
relay COUNTs (#161). A member who only writes Notes has nothing but kind-30078 documents, which the relay serves
to nobody but their author and so never counted -- the member read as inactive. A COUNT from a socket signed in
(NIP-42) as the node's own key (`node_pubkey`) now includes them and says so (`"private": true`). Nothing else
changes: no other socket's count, no REQ, no EVENT, no content.
"""
import json
import uuid

from websockets.sync.client import connect

from app.services import relay_reader
from app.services.nostr import bip340
from app.services.nostr.event import build_event
from tests.relay_fake import ShippedRelay, ev

OP_SK = bytes.fromhex("42" * 32)
OP = bip340.pubkey_from_seckey(OP_SK).hex()
OTHER_SK = bytes.fromhex("43" * 32)          # a member / linked user -- not the node's key
D = "d" * 64

EVENTS = [ev(D, 0, 1700000000), ev(D, 1, 1700000100)] + \
         [ev(D, 30078, 1700001000 + i, tags=[["d", "pcai:note:%d" % i]], content="SECRET") for i in range(3)]


def _session(port, sk, frames):
    """Sign in as `sk` (when given), send `frames`, return everything the relay answered until it goes quiet."""
    target = "ws://127.0.0.1:%d/relay" % port
    out = []
    with connect(target, open_timeout=5) as ws:
        if sk:
            relay_reader._authenticate(ws, target, sk, 5)
        for f in frames:
            ws.send(json.dumps(f))
        while True:
            try:
                out.append(json.loads(ws.recv(timeout=1.0)))
            except TimeoutError:
                return out


def _counts(msgs):
    return {m[1]: m[2] for m in msgs if m[0] == "COUNT"}


def test_the_operator_count_includes_other_authors_private_documents():
    r = ShippedRelay(EVENTS, {"node_pubkey": OP})
    try:
        got = relay_reader.counts([{"authors": [D]}, {"authors": [D], "kinds": [30078]}],
                                  port=r.port, timeout=5, auth_seckey=OP_SK, private=True)
        assert got == [5, 3]
    finally:
        r.close()


def test_any_other_count_is_exactly_what_it_was():
    r = ShippedRelay(EVENTS, {"node_pubkey": OP})
    try:
        for sk in (None, OTHER_SK):
            msgs = _session(r.port, sk, [["COUNT", "all", {"authors": [D]}],
                                         ["COUNT", "priv", {"authors": [D], "kinds": [30078]}]])
            c = _counts(msgs)
            assert c["all"] == {"count": 2}, "a non-operator count included somebody else's private documents"
            assert "priv" not in c and ["CLOSED", "priv", "auth-required: NIP-78 counts require AUTH and "
                                        "matching authors"] in msgs
        # ...and asking for the operator count without being the operator is "could not ask", never the narrow number.
        try:
            relay_reader.counts([{"authors": [D]}], port=r.port, timeout=5, auth_seckey=OTHER_SK, private=True)
            raise AssertionError("a narrower count was accepted as the operator count")
        except relay_reader.Unavailable:
            pass
    finally:
        r.close()


def test_a_relay_with_no_node_key_grants_nobody():
    r = ShippedRelay(EVENTS, {})
    try:
        assert _counts(_session(r.port, OP_SK, [["COUNT", "all", {"authors": [D]}]]))["all"] == {"count": 2}
    finally:
        r.close()


def test_the_operator_still_receives_no_other_authors_private_events():
    r = ShippedRelay(EVENTS, {"node_pubkey": OP})
    try:
        sub = uuid.uuid4().hex[:8]
        msgs = _session(r.port, OP_SK, [["REQ", sub, {"authors": [D]}],
                                        ["REQ", sub + "p", {"authors": [D], "kinds": [30078]}]])
        served = [m[2] for m in msgs if m[0] == "EVENT"]
        assert served and all(e["kind"] != 30078 for e in served)
        assert not any("SECRET" in json.dumps(m) for m in msgs)
        assert any(m[0] == "CLOSED" and m[1] == sub + "p" and str(m[2]).startswith("auth-required") for m in msgs)
    finally:
        r.close()


def test_the_operator_count_carries_no_ids_and_no_content():
    r = ShippedRelay(EVENTS, {"node_pubkey": OP})
    try:
        msgs = _session(r.port, OP_SK, [["COUNT", "c", {"authors": [D], "kinds": [30078]}]])
        assert _counts(msgs)["c"] == {"count": 3, "private": True}
        assert not any(m[0] == "EVENT" for m in msgs) and "SECRET" not in json.dumps(msgs)
    finally:
        r.close()


def test_building_an_auth_event_for_the_wrong_relay_is_refused():
    """The operator count rides NIP-42: an AUTH signed for another relay URL never unlocks it."""
    r = ShippedRelay(EVENTS, {"node_pubkey": OP})
    try:
        with connect("ws://127.0.0.1:%d/relay" % r.port, open_timeout=5) as ws:
            chal = json.loads(ws.recv(timeout=5))[1]
            auth = build_event(OP_SK, 22242, "", tags=[["relay", "wss://elsewhere.example/relay"], ["challenge", chal]])
            ws.send(json.dumps(["AUTH", auth]))
            ok = json.loads(ws.recv(timeout=5))
            assert ok[0] == "OK" and ok[2] is False
            ws.send(json.dumps(["COUNT", "c", {"authors": [D]}]))
            while True:
                m = json.loads(ws.recv(timeout=5))
                if m[0] == "COUNT":
                    assert m[2] == {"count": 2}
                    break
    finally:
        r.close()
