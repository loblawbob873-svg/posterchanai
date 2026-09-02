"""THE THREAD BUILDER MUST SEE THE WHOLE MAILBOX, NOT THE NEWEST PAGE OF IT.

Reported as "Email is missing messages I sent in the thread! you are not showing everything right!"
— after a first fix that was real but nowhere near sufficient.

Measured on the mailbox it was reported from:

    single page :  5,000 documents  ->   91 counted as the user's own sent mail
    paged       : 17,903 documents  ->  907

`list_messages(..., limit=0)` reads ONE page and the relay clamps any filter to 5000, so "all
messages" quietly meant "the newest 5000 documents". That mailbox's newest 5000 were 3,173 Trash
plus 1,711 Deleted Messages, leaving roughly a hundred live messages visible to threading. No
threading rule can find a message that was never handed to it, and nothing logs a read that hits
the cap — it looks exactly like a read that found everything.

Two independent faults, both fixed here:

1. THE WINDOW. `list_all_messages` walks the cursor to exhaustion, the same way `have_uids` already
   does for the sync's dedup — a bug that module's own comment describes arriving by another road.

2. THE NAME. `_logical_of` only rewrites a folder when the server's RFC 6154 flags name it and
   returns the raw name otherwise, so that mailbox held 52 messages under `Sent Messages` beside 39
   under `Sent`. An equality test against "sent" found 39 of 91.

The scan is cached for a minute because walking every page costs ~10s of NIP-44 decrypts. That is
affordable only because the client renders the opened message immediately and upgrades when the
thread returns, so the read is never blocked by it.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from app.routers import mail as mail_router
from app.routers.mail import _is_own_sent, _looks_sent
from app.services import mail_store

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["Sent", "sent", "Sent Messages", "Sent Items",
                                  "INBOX.Sent", "[Gmail]/Sent Mail", "Saved/Sent"])
def test_every_way_a_server_spells_sent(name):
    """THE 52 THAT WERE MISSED. `Sent Messages` is what the reporting mailbox actually used."""
    assert _looks_sent(name) is True


@pytest.mark.parametrize("name", ["INBOX", "Trash", "Deleted Messages", "Archive", "Drafts",
                                  "Consent forms", "Presentations", "", None])
def test_what_is_not_a_sent_folder(name):
    """Matched on whole words, so `Consent` and `Presentations` do not qualify — a false positive
    here would pull strangers' mail into your conversations."""
    assert _looks_sent(name) is False


def test_a_message_is_own_sent_by_either_its_logical_name_or_its_real_one():
    """`logical` is empty on anything stored before that field existed, so the real folder has to
    be consulted too — otherwise the fix only works for mail synced after it."""
    assert _is_own_sent({"logical": "Sent Messages", "folder": "Sent Messages"}) is True
    assert _is_own_sent({"logical": "", "folder": "INBOX.Sent"}) is True
    assert _is_own_sent({"logical": "INBOX", "folder": "INBOX"}) is False


def test_the_scan_walks_every_page():
    """THE CAP. One page is 5000 and the relay clamps to it; a mailbox larger than that was
    threading against its newest slice with nothing to say so."""
    pages = [[{"uid": str(i)} for i in range(3)], [{"uid": "x"}], []]
    cursors = [10, 20, None]
    seen = []

    async def fake_list_page(sk, account, folder, limit=None, until=None):
        seen.append(until)
        i = len(seen) - 1
        return pages[i], cursors[i]

    got = asyncio.run(_run(fake_list_page))
    assert len(got) == 4, f"the scan stopped early: {got}"
    assert seen == [None, 10, 20], f"the cursor was not walked: {seen}"


async def _run(fake_list_page):
    real = mail_store.list_page
    mail_store.list_page = fake_list_page
    try:
        return await mail_store.list_all_messages(b"k", None, None)
    finally:
        mail_store.list_page = real


def test_the_scan_is_bounded():
    """A relay that always returns a cursor must not spin for ever."""
    sig = inspect.signature(mail_store.list_all_messages)
    assert "max_pages" in sig.parameters
    assert isinstance(sig.parameters["max_pages"].default, int)


def test_a_repeated_cursor_cannot_loop_for_ever():
    """The same guard `have_uids` carries: never hand back a cursor already used."""
    src = inspect.getsource(mail_store.list_all_messages)
    assert "until != nxt" in src, "a relay repeating its cursor would spin the scan"


def test_the_thread_route_uses_the_paged_scan():
    """The wiring, not the helper — a paged reader nothing calls fixes nothing."""
    src = inspect.getsource(mail_router.mail_thread)
    assert "_thread_scan(" in src
    assert "list_messages(sk, acc.email if acc else None, None, limit=0)" not in src, (
        "the thread route is back on the single capped page")


def test_the_scan_is_cached_so_a_thread_open_does_not_pay_for_it_twice():
    """~10s of NIP-44 decrypts per scan. Affordable once per minute; not per opened message."""
    assert mail_router._THREAD_SCAN_TTL >= 30
    src = inspect.getsource(mail_router._thread_scan)
    assert "_THREAD_SCAN[key]" in src and "monotonic" in src
