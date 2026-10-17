"""A deletion of an author's own private app documents (kind 30078) goes only to subscriptions that name that author.

server1, 2026-10-17: every client's always-on `{kinds:[5]}` subscription took the relay's latest 500 deletions at
boot and every new one live -- ~9,400 a day are the operator's DocTables deleting app documents, which nobody else
can hold (NIP-78 reads need AUTH as the author). Measured in the relay's cost line as 26 x 500-frame answers in
five minutes, plus live fan-out of each to every client.
"""
from app.services.nostr_relay import server as S

OP, ALICE = "a" * 64, "b" * 64


def ev(pk, tags, i=1):
    return {"id": "%064x" % i, "pubkey": pk, "kind": 5, "created_at": 1, "tags": tags, "content": "", "sig": "s"}


PRIVATE = ev(OP, [["a", "30078:%s:pcai:t:fedi_ledger_index:u:1" % OP], ["k", "30078"]], 1)
POST = ev(ALICE, [["e", "c" * 64], ["k", "1"]], 2)
NO_KINDS = ev(ALICE, [["e", "d" * 64]], 3)
MIXED = ev(OP, [["a", "30078:%s:x" % OP], ["e", "e" * 64], ["k", "1"]], 4)


def test_which_deletions_are_private_document_deletions():
    assert S._private_doc_deletion(PRIVATE)
    assert not S._private_doc_deletion(POST)
    assert not S._private_doc_deletion(NO_KINDS), "a deletion naming no kinds is ordinary"
    assert not S._private_doc_deletion(MIXED), "one naming a post as well is ordinary"


def test_live_delivery_keeps_them_with_their_author():
    subs = S.SubscriptionManager()
    everyone, owner = object(), object()
    subs.add(everyone, "del", [{"kinds": [5], "limit": 500}])
    subs.add(owner, "mine", [{"kinds": [5], "authors": [OP]}])
    got = []
    for e in (PRIVATE, POST, NO_KINDS, MIXED):
        subs.fanout(e, lambda c, m: got.append((c, m[2]["id"])), lambda c, x: True)
    to_everyone = {i for c, i in got if c is everyone}
    to_owner = {i for c, i in got if c is owner}
    assert PRIVATE["id"] not in to_everyone, "a private-document deletion was fanned out to every client"
    assert PRIVATE["id"] in to_owner, "its author's own client must still get it (DocTable live loops depend on it)"
    assert {POST["id"], NO_KINDS["id"], MIXED["id"]} <= to_everyone


def test_the_backlog_answer_keeps_them_with_their_author_too():
    import asyncio
    from types import SimpleNamespace
    srv = S.RelayServer.__new__(S.RelayServer)
    sent = []
    srv._send = lambda conn, m: sent.append(m)
    srv.subs = S.SubscriptionManager()
    srv.cfg = {}
    srv.mirror = None
    srv._can_serve_event = lambda conn, e: True
    srv._is_private_repo_event = lambda e: False
    srv._filter_explicitly_requests_nip78 = lambda f: False
    srv._note_req_cost = lambda *a, **k: None
    srv._req_origin = lambda *a: ""

    async def query(filters):
        return [MIXED, NO_KINDS, POST, PRIVATE]
    srv.store = SimpleNamespace(query=query)
    for name in ("_withheld_repo_closed", "_maybe_auth_hint"):
        if not hasattr(srv, name):
            setattr(srv, name, lambda *a, **k: None)

    def ids(filters):
        sent.clear()
        try:
            asyncio.run(srv._on_req("c", "s", filters))
        except Exception:
            pass
        return {m[2]["id"] for m in sent if m[0] == "EVENT"}
    assert PRIVATE["id"] not in ids([{"kinds": [5], "limit": 500}]), "the backlog still hands every client private deletions"
    assert POST["id"] in ids([{"kinds": [5], "limit": 500}])
    assert PRIVATE["id"] in ids([{"kinds": [5], "authors": [OP]}])
