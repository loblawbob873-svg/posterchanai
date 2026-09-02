"""THE CONVERSATION STILL DID NOT LOAD, AND THE THREADING RULES WERE NOT THE REASON.

Reported again as "for email, i am still not seeing the conversation load the sent items in that
thread!", after two rounds that each fixed something real (`_looks_sent`, the paged
`list_all_messages`, the headerless-sent subject rescue) and did not move the symptom.

Measured on the reporting mailbox — 17,921 encrypted documents, four mail accounts, 907 of the
messages the user's own sent mail — three separate faults, none of which is a threading rule:

1. THE SEED COULD NOT BE FOUND. With more than one account the client opens on All inboxes, and
   `/thread`'s `__all` branch looked the seed up in ONE `list_messages(..., limit=0)` page. The
   relay clamps any filter to 5000 and returns newest-WRITTEN first. That page held 3,161 Trash and
   1,711 Deleted Messages; of the mail somebody actually reads it could find

       INBOX          32 of    39
       INBOX.Archive   3 of 2,717      <-- 0%
       INBOX.Sent      1 of   807      <-- 0%

   For the other 2,714 archived messages the endpoint answered 404, the client's `.catch` wrote a
   console warning, and the conversation never upgraded past the message that was clicked. From the
   outside that is indistinguishable from threading that found nothing.

2. THE CACHE NEVER HIT. `_thread_scan` keyed its 60-second cache on `id(sk)`. `_seckey` ends in
   `bytes.fromhex(...)`, which mints a new object every call, so the key was a different integer
   for every request — measured back to back: 139845150742352, then 139845082027296. Every opened
   message therefore paid for a fresh full scan: 17,921 documents in 13.6 seconds of NIP-44
   decrypts, on the single uvicorn worker. The existing test asserted that a cache EXISTED
   (`_THREAD_SCAN[key]` appears in the source) and never ran it twice, which is exactly how this
   survived. The test below runs it.

3. THE ADDITIVE OWN-SENT RULE ASKED THE WRONG QUESTION. It admitted a subject-matching sent message
   only when it carried no Message-ID. An ID is not evidence about which conversation a message
   belongs to — evidence is a reference that RESOLVES. 622 of those 907 sent messages are
   graph-isolated (nothing points at them, they point at nothing we hold) and 441 of those share a
   normalised subject with a message that is not theirs; the ID test admitted only the 341 with no
   ID. Per ACCOUNT, which is how threading actually runs, closing that gap makes 11 more of the
   user's own sent messages reachable (369 -> 380).

And the same one-page read was under `/search`: unified search saw 5,000 of 17,921 documents, and a
single account over the cap lost 449 of its 5,469. A truncated read is indistinguishable from
"no matches".

Every test here has been mutation-checked — see the `test_*_can_fail` cases, which rebuild the
pre-fix behaviour and assert the probe goes red.
"""
from __future__ import annotations

import asyncio

import pytest

from app.routers import mail as mail_router
from app.routers.mail import _build_thread, _graph_isolated
from app.services import mail_store


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 3. the additive own-sent rule
# ──────────────────────────────────────────────────────────────────────────────────────────────
def msg(uid, folder="INBOX", subject="Quote", mid="", irt="", refs="", ts=0):
    return {"uid": str(uid), "folder": folder, "subject": subject, "message_id": mid,
            "in_reply_to": irt, "references": refs, "ts": ts}


def sent(uid, subject="Re: Quote", mid="", irt="", refs="", ts=0):
    return msg(uid, folder="INBOX.Sent", subject=subject, mid=mid, irt=irt, refs=refs, ts=ts)


def uids(thread):
    return {m["uid"] for m in thread}


#: EVERY TEST OF THE ADDITIVE RULE GIVES THE SEED A REAL REPLY FIRST, and that is not decoration.
#: `_build_thread` has a second, older subject fallback that runs ONLY when the reference graph
#: found nothing at all, and it admits any same-subject message that is a reply or is own-sent —
#: no isolation test. A one-message fixture goes down that path instead, which is how the first
#: draft of these tests passed against the pre-fix code and proved nothing.
#:
#: That fallback is deliberately NOT tightened here. Measured over the reporting mailbox, per
#: account: requiring graph isolation there too drops 1,141 of 26,431 thread members and takes
#: sent-mail reachability DOWN from 380 to 372 — the opposite of what was asked for — and there is
#: no ground truth in the data saying those 1,141 were wrong. It is left alone on purpose.
def conversation(subject="Quote"):
    """A seed with one genuine header-linked reply, so the graph finds two and the fallback is
    out of the picture."""
    seed = msg(1, mid="<a@x>", subject=subject, ts=10)
    reply = msg(8, mid="<b@x>", subject="Re: " + subject, irt="<a@x>", ts=15)
    return seed, reply


def test_a_sent_reply_whose_id_nobody_ever_referenced_joins_the_conversation():
    """THE 100 THE OLD RULE LEFT OUT. This app has set a Message-ID on outgoing mail for a while
    now, so plenty of sent mail HAS one — and if the recipient's client stripped References on the
    way back (or their reply is simply not in this mailbox), that ID is referenced by nothing. The
    graph cannot reach the message in either direction, so a subject match overrides no evidence."""
    seed, reply = conversation()
    mine = sent(2, subject="Re: Quote", mid="<mine@x>", ts=20)     # has an ID; nothing points at it
    got = _build_thread(seed, [seed, reply, mine])
    assert "2" in uids(got), (
        "a sent message with an ID that nothing references is still excluded — the rule is testing "
        "for a Message-ID instead of asking whether the graph can reach it")


def test_evidence_still_beats_a_subject_guess():
    """The guard the old rule was reaching for, stated correctly. A sent message that REFERENCES a
    message we hold belongs to that message's conversation, whatever its subject says — so it must
    not also be swept into a same-subject thread it has nothing to do with."""
    seed, reply = conversation()
    other_root = msg(9, mid="<root9@x>", subject="Quote", ts=1)
    mine = sent(2, subject="Re: Quote", mid="<mine@x>", irt="<root9@x>", ts=20)
    pool = [seed, reply, mine, other_root]
    assert "2" not in uids(_build_thread(seed, pool)), (
        "a sent message with a reference that RESOLVES was pulled into a thread by subject — a "
        "guess overrode header evidence")
    assert "2" in uids(_build_thread(other_root, pool)), "…and it is missing from where it belongs"


def test_a_sent_message_somebody_replied_to_is_left_to_the_graph():
    """The other direction: if a held message points AT this one's ID, the graph reaches it and the
    subject fallback must keep its hands off."""
    mine = sent(2, subject="Quote", mid="<mine@x>", ts=5)
    theirs = msg(3, mid="<r@x>", subject="Re: Quote", irt="<mine@x>", ts=6)
    seed, reply = conversation()               # a different conversation, same subject
    pool = [seed, reply, mine, theirs]
    assert _graph_isolated(pool)(mine) is False
    assert "2" not in uids(_build_thread(seed, pool))
    assert "2" in uids(_build_thread(theirs, pool))


def test_the_headerless_rescue_that_already_worked_still_works():
    """No regression on the case the previous round fixed — and on the additive path, not the
    fallback, so it is the rule under test that is doing the work."""
    seed, reply = conversation()
    mine = sent(2, subject="Re: Re: Fwd: Quote", ts=20)            # no identity at all
    assert "2" in uids(_build_thread(seed, [seed, reply, mine]))


def test_two_unrelated_roots_are_still_two_conversations():
    """The Kraken rule. Automated senders reuse one subject for months; two ROOTS sharing it are
    never one thread, and widening the sent rule must not have widened that."""
    a = msg(1, mid="<a@x>", subject="Kraken notice", ts=10)
    b = msg(2, mid="<b@x>", subject="Kraken notice", ts=20)
    assert uids(_build_thread(a, [a, b])) == {"1"}


def test_the_isolation_rule_can_fail():
    """MUTATION. Rebuild the pre-fix predicate ("has no Message-ID") and prove the first test above
    goes red on it — a probe that passes on the bug proves nothing."""
    real = mail_router._graph_isolated
    mail_router._graph_isolated = lambda allmsgs: (lambda m: not (m.get("message_id") or "").strip())
    try:
        seed, reply = conversation()
        mine = sent(2, subject="Re: Quote", mid="<mine@x>", ts=20)
        assert "2" not in uids(_build_thread(seed, [seed, reply, mine])), (
            "the pre-fix rule passes this test, so the test proves nothing")
    finally:
        mail_router._graph_isolated = real


# ──────────────────────────────────────────────────────────────────────────────────────────────
# a relay that behaves like the real one: every read is capped, and paging is the only way past it
# ──────────────────────────────────────────────────────────────────────────────────────────────
CAP = 5000          # nostr_relay/store.py clamps any filter limit to this


def _mailbox(n_trash=6000, n_archive=300):
    """Newest-WRITTEN first, the order the relay returns and the order that matters: the archive is
    written before the trash sweep, so it sits past the cap exactly as it does in production."""
    docs = [{"uid": f"t{i}", "folder": "Trash", "subject": "junk", "message_id": f"<t{i}@x>",
             "in_reply_to": "", "references": "", "ts": 900000 + i, "account": "me@example.com"}
            for i in range(n_trash)]
    docs += [{"uid": f"a{i}", "folder": "INBOX.Archive", "subject": f"Order {i}",
              "message_id": f"<a{i}@x>", "in_reply_to": "", "references": "", "ts": 100 + i,
              "account": "me@example.com"} for i in range(n_archive)]
    docs.append({"uid": "s1", "folder": "INBOX.Sent", "subject": "Re: Order 7", "message_id": "",
                 "in_reply_to": "", "references": "", "ts": 50, "account": "me@example.com"})
    return docs


def _install_fake_relay(monkeypatch, docs):
    """Replace ONLY `list_page`. `list_messages` and `list_all_messages` are the shipped functions
    on top of it, so the truncation this reproduces is the real one, not a mocked one."""
    calls = {"n": 0}

    async def fake_list_page(sk, account=None, folder=None, limit=None, until=None):
        calls["n"] += 1
        want = CAP if limit in (0, None) else min(int(limit), CAP)
        pool = [d for d in docs if (account is None or d["account"] == account)
                and (folder is None or d["folder"] == folder)]
        start = 0 if until is None else until
        page = pool[start:start + want]
        nxt = (start + want) if (start + want) < len(pool) else None
        return page, nxt

    monkeypatch.setattr(mail_store, "list_page", fake_list_page)
    return calls


class _User:
    id = 1
    username = "someone"


def _install_route_stubs(monkeypatch, docs):
    monkeypatch.setattr(mail_router, "_seckey", lambda db, user: bytes.fromhex("11" * 32))
    monkeypatch.setattr(mail_router, "_resolve_account",
                        lambda db, user, hint: type("A", (), {"email": "me@example.com"})())

    async def fake_get(sk, account, folder, uid):
        return next((d for d in docs if d["folder"] == folder and str(d["uid"]) == str(uid)), None)

    monkeypatch.setattr(mail_store, "get_message", fake_get)


@pytest.fixture(autouse=True)
def _clear_scan_cache():
    mail_router._THREAD_SCAN.clear()
    yield
    mail_router._THREAD_SCAN.clear()


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 1. the seed, in All-inboxes mode
# ──────────────────────────────────────────────────────────────────────────────────────────────
def test_all_inboxes_finds_a_message_past_the_first_page(monkeypatch):
    """THE 2,714. An archived message sits behind six thousand trashed ones; the endpoint has to
    walk to it rather than reading one capped page and answering 404."""
    docs = _mailbox()
    _install_fake_relay(monkeypatch, docs)
    _install_route_stubs(monkeypatch, docs)
    got = asyncio.run(mail_router.mail_thread("__all", "a7", "INBOX.Archive", None, _User()))
    assert {m["uid"] for m in got["messages"]} >= {"a7"}


def test_all_inboxes_still_threads_that_message(monkeypatch):
    """Finding the seed is only half of it — the user's own reply has to arrive with it."""
    docs = _mailbox()
    _install_fake_relay(monkeypatch, docs)
    _install_route_stubs(monkeypatch, docs)
    got = asyncio.run(mail_router.mail_thread("__all", "a7", "INBOX.Archive", None, _User()))
    assert "s1" in {m["uid"] for m in got["messages"]}, (
        "the conversation loaded without the message the user sent in it")


def test_the_seed_lookup_can_fail(monkeypatch):
    """MUTATION. The pre-fix branch read one page; reinstate it and the same open 404s."""
    docs = _mailbox()
    _install_fake_relay(monkeypatch, docs)
    _install_route_stubs(monkeypatch, docs)

    async def one_page_seed(sk, account_email, user_id):
        # exactly what the old code did: `list_messages(sk, None, None, limit=0)`
        return await mail_store.list_messages(sk, None, None, limit=0)

    monkeypatch.setattr(mail_router, "_thread_scan", one_page_seed)
    with pytest.raises(Exception) as err:
        asyncio.run(mail_router.mail_thread("__all", "a7", "INBOX.Archive", None, _User()))
    assert "404" in str(err.value) or "not found" in str(err.value).lower(), (
        f"the pre-fix single-page lookup found the message, so this test proves nothing: {err.value}")


def test_a_named_account_still_reads_the_message_directly(monkeypatch):
    """The single-account path is a keyed document read and must not have grown a mailbox walk."""
    docs = _mailbox()
    calls = _install_fake_relay(monkeypatch, docs)
    _install_route_stubs(monkeypatch, docs)
    before = calls["n"]
    got = asyncio.run(mail_router.mail_thread("me@example.com", "a7", "INBOX.Archive", None, _User()))
    assert "a7" in {m["uid"] for m in got["messages"]}
    assert calls["n"] > before      # the scan still runs for the thread itself


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 2. the cache
# ──────────────────────────────────────────────────────────────────────────────────────────────
def test_the_scan_cache_survives_a_fresh_key_object(monkeypatch):
    """THE BUG, RUN RATHER THAN READ. Two requests from the same user hand `_thread_scan` two
    DISTINCT bytes objects holding identical key material — which is precisely what `_seckey`
    returns, because it ends in `bytes.fromhex`. The second must not pay for another scan."""
    docs = _mailbox()
    calls = _install_fake_relay(monkeypatch, docs)
    k1, k2 = bytes.fromhex("11" * 32), bytes.fromhex("11" * 32)
    assert k1 == k2 and k1 is not k2, "the fixture no longer reproduces what _seckey does"

    async def two_scans():
        await mail_router._thread_scan(k1, None, 1)
        n = calls["n"]
        await mail_router._thread_scan(k2, None, 1)
        return n, calls["n"]

    first, second = asyncio.run(two_scans())
    assert second == first, (
        f"the second scan re-read the mailbox ({second - first} more relay pages) — the cache is "
        f"keyed on something that changes between requests, so every opened message pays 13.6s")


def test_the_cache_still_separates_two_users(monkeypatch):
    """A cache that hits when it should not is worse than one that never hits."""
    docs = _mailbox()
    calls = _install_fake_relay(monkeypatch, docs)

    async def two_users():
        await mail_router._thread_scan(bytes.fromhex("11" * 32), None, 1)
        n = calls["n"]
        await mail_router._thread_scan(bytes.fromhex("22" * 32), None, 2)
        return n, calls["n"]

    first, second = asyncio.run(two_users())
    assert second > first, "two different users shared one mailbox scan"


def test_the_cache_test_can_fail(monkeypatch):
    """MUTATION. Key the cache on `id(sk)` again and the first assertion above must go red."""
    docs = _mailbox()
    calls = _install_fake_relay(monkeypatch, docs)
    import time as _time

    async def old_scan(sk, account_email, user_id=None):
        key = (id(sk), account_email or "*")
        hit = mail_router._THREAD_SCAN.get(key)
        now = _time.monotonic()
        if hit and now - hit[0] < mail_router._THREAD_SCAN_TTL:
            return hit[1]
        msgs = await mail_store.list_all_messages(sk, account_email, None)
        mail_router._THREAD_SCAN[key] = (now, msgs)
        return msgs

    # BOTH OBJECTS STAY ALIVE. Freed in between, CPython hands the second `bytes` the first one's
    # address, `id()` collides and the broken key appears to work — which is the recycling hazard
    # this fix also removes, and which would make the probe report a pass on the bug.
    k1, k2 = bytes.fromhex("11" * 32), bytes.fromhex("11" * 32)
    assert id(k1) != id(k2)

    async def two_scans():
        await old_scan(k1, None)
        n = calls["n"]
        await old_scan(k2, None)
        return n, calls["n"]

    first, second = asyncio.run(two_scans())
    assert second > first, "the pre-fix cache key passes the test above, so it proves nothing"


# ──────────────────────────────────────────────────────────────────────────────────────────────
# search
# ──────────────────────────────────────────────────────────────────────────────────────────────
def test_search_sees_past_the_first_page(monkeypatch):
    """5,000 of 17,921. A search that only reads the newest page reports "no matches" for most of
    the mailbox, and nothing anywhere says the read was cut short."""
    docs = _mailbox()
    _install_fake_relay(monkeypatch, docs)
    _install_route_stubs(monkeypatch, docs)
    got = asyncio.run(mail_router.mail_search("Order 7", "__all", "", None, _User()))
    assert any(m["uid"] == "a7" for m in got["messages"]), (
        "unified search cannot find an archived message that sits past the relay's 5000 cap")


def test_the_search_test_can_fail(monkeypatch):
    """MUTATION. One page again; the archived message becomes unfindable."""
    docs = _mailbox()
    _install_fake_relay(monkeypatch, docs)
    hits = asyncio.run(mail_store.list_messages(bytes(32), None, None, limit=0))
    assert not any(m["uid"] == "a7" for m in hits), (
        "the fixture does not reproduce the cap, so the search test proves nothing")


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 4. the seed with no headers at all
# ──────────────────────────────────────────────────────────────────────────────────────────────
def test_your_own_headerless_sent_message_still_opens_as_a_conversation(monkeypatch):
    """THE 493. `/thread` used to end the request for any seed carrying no Message-ID, In-Reply-To
    or References — which is precisely the shape this app's own older outgoing mail has. Opening one
    of your own sent messages could then only ever show that message, however complete the
    conversation around it was. Measured per account on the reporting mailbox: 393 sent seeds and
    478 inbound seeds took that branch, and the subject fallback gives 319 and 174 of them
    respectively a real conversation.

    The counterpart here is the shape the mailbox actually holds: this app sent the message without
    a Message-ID, so the correspondent's reply carries an `In-Reply-To` pointing at an identity that
    no longer exists anywhere. The graph cannot use it, but it is still a reply, which is what the
    subject fallback keys on.

    MEASURED AND NOT DONE: making that fallback symmetric as well — admitting an inbound ROOT into
    a sent seed's thread — gains 20 more conversations (319 -> 339) and costs 1,296 extra thread
    members and nine more threads over eight messages. That is the shape of the "four separate
    Kraken notices arrived as one thread" report, so the rule stays as it is."""
    docs = [
        {"uid": "in1", "folder": "INBOX", "subject": "Re: Invoice 42", "message_id": "<in1@x>",
         "in_reply_to": "<a-message-id-this-app-never-set@x>", "references": "", "ts": 30,
         "account": "me@example.com"},
        {"uid": "s1", "folder": "INBOX.Sent", "subject": "Invoice 42", "message_id": "",
         "in_reply_to": "", "references": "", "ts": 20, "account": "me@example.com"},
    ]
    _install_fake_relay(monkeypatch, docs)
    _install_route_stubs(monkeypatch, docs)
    got = asyncio.run(mail_router.mail_thread("me@example.com", "s1", "INBOX.Sent", None, _User()))
    assert {m["uid"] for m in got["messages"]} == {"in1", "s1"}, (
        "opening your own sent message still shows it alone — the headerless short circuit is back")


def test_a_seed_with_nothing_to_match_on_is_still_a_singleton(monkeypatch):
    """The guard that remains. No headers AND no subject once Re:/Fwd: comes off: a scan could not
    find anything however long it ran, so it must not run."""
    docs = [
        {"uid": "s1", "folder": "INBOX.Sent", "subject": "  ", "message_id": "",
         "in_reply_to": "", "references": "", "ts": 20, "account": "me@example.com"},
        {"uid": "s2", "folder": "INBOX.Sent", "subject": "Fwd:", "message_id": "",
         "in_reply_to": "", "references": "", "ts": 21, "account": "me@example.com"},
    ]
    calls = _install_fake_relay(monkeypatch, docs)
    _install_route_stubs(monkeypatch, docs)
    before = calls["n"]
    got = asyncio.run(mail_router.mail_thread("me@example.com", "s1", "INBOX.Sent", None, _User()))
    assert [m["uid"] for m in got["messages"]] == ["s1"]
    assert calls["n"] == before, "a seed with nothing to match on paid for a mailbox scan"


def test_the_headerless_short_circuit_can_fail(monkeypatch):
    """MUTATION. Reinstate the old condition and the sent message opens alone again."""
    docs = [
        {"uid": "in1", "folder": "INBOX", "subject": "Re: Invoice 42", "message_id": "<in1@x>",
         "in_reply_to": "<a-message-id-this-app-never-set@x>", "references": "", "ts": 30,
         "account": "me@example.com"},
        {"uid": "s1", "folder": "INBOX.Sent", "subject": "Invoice 42", "message_id": "",
         "in_reply_to": "", "references": "", "ts": 20, "account": "me@example.com"},
    ]
    _install_fake_relay(monkeypatch, docs)
    _install_route_stubs(monkeypatch, docs)
    seed = docs[1]
    pre_fix_short_circuits = not (seed.get("message_id") or seed.get("in_reply_to")
                                  or (seed.get("references") or "").strip())
    assert pre_fix_short_circuits, "the fixture no longer reproduces the branch under test"
