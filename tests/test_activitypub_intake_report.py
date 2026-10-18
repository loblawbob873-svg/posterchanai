"""The inbox says, per sending server, what it received and what became of it (every 10 minutes).

2026-10-17: nicecrew.digital's posts fell from ~100 an hour to ~15 while the inbox kept answering ~1,000 deliveries
an hour with 202, and no log could say whether nicecrew had stopped sending, was refused, or was accepted and then
ignored -- `inbox.process` returned its verdict to a caller that threw it away. Drives the SHIPPED route.
"""
import asyncio
import json
import logging
import time

from app.services.activitypub import inbox, intake
from tests.test_activitypub import BASE, REMOTE, _create, _post_signed, client, world  # noqa: F401  (fixtures)


def _fresh():
    intake._received.clear()
    intake._processed.clear()
    intake._since = 0.0


def test_each_delivery_is_counted_under_the_server_that_sent_it(client, caplog):  # noqa: F811
    _fresh()
    intake._since = 1e18                                # no report mid-test
    assert _post_signed(client, _create()).status_code == 202
    assert client.post("/ap/inbox", content=json.dumps(_create()).encode()).status_code == 401   # unsigned
    rows = intake.lines()
    assert any(r.startswith("mastodon.example 1 (scheduled 1") for r in rows), rows
    assert any(r.startswith("? 1 (401") for r in rows), rows


def test_what_processing_made_of_a_delivery_is_counted_and_reported(monkeypatch, caplog):
    _fresh()

    async def verdict(activity, signer):
        return "ignored: nobody here follows or was addressed"
    monkeypatch.setattr(inbox, "process", verdict)
    intake._since = time.monotonic()
    intake.received("mastodon.example", "scheduled")
    asyncio.run(inbox._run(_create(), REMOTE))
    with caplog.at_level(logging.INFO, logger="app.services.activitypub.intake"):
        assert intake.report(now=intake._since + intake.EVERY + 1)
    line = [r.getMessage() for r in caplog.records if "intake" in r.getMessage()][0]
    assert "mastodon.example 1 (scheduled 1 -> Create ignored: nobody here follows or was addressed 1)" in line, line
    assert intake.lines() == []                         # a new window starts empty


def test_junk_keyid_hosts_cannot_grow_the_window_without_bound(monkeypatch):
    """Every POST to the inbox is counted under the host its UNVERIFIED keyId names -- a stream of junk, each naming a
    new host, made one counter apiece for the whole window (security review 2026-10-18)."""
    from app.services.activitypub import intake
    intake._received.clear()
    intake._processed.clear()
    monkeypatch.setattr(intake, "_since", 10 ** 12)          # the window never closes during the test
    for i in range(10_000):
        intake.received("junk%d.example" % i, "401 bad signature")
    assert len(intake._received) <= 2001, len(intake._received)
    assert sum(sum(c.values()) for c in intake._received.values()) == 10_000   # nothing goes uncounted
    intake._received.clear()
