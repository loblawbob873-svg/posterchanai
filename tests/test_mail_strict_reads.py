"""Mail never treats "the relay could not answer" as "there is no mail".

Every deploy restarts the relay for ~30s, and `list_dtags` had no strict mode at all, so `have_uids`
answered an EMPTY set: the sync then read every message in the folder as new -- fetching and
re-writing the whole mailbox over IMAP -- and the new-mail notifier saw the whole inbox arrive. The
folder listing answered an empty inbox. Run here against a relay that is not listening.
"""
import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

from app.services import mail_store, mail_sync, nostr_store


async def _down(port, filters, strict=False, auth_seckey=None):
    if strict:
        raise ConnectionError("relay not listening")
    return []


def test_have_uids_raises_instead_of_answering_empty():
    with mock.patch.object(nostr_store, "_ws_query", _down):
        with pytest.raises(Exception):
            asyncio.run(mail_store.have_uids(bytes(range(1, 33)), "me@home.test", "INBOX"))


def test_the_sync_fetches_nothing_when_it_cannot_tell_what_it_has():
    fetched = []
    with mock.patch.object(nostr_store, "_ws_query", _down), \
         mock.patch.object(mail_sync.mail_service, "list_uids", lambda acc, f: [1, 2, 3]), \
         mock.patch.object(mail_sync.mail_service, "fetch_by_uids", lambda acc, f, uids: fetched.extend(uids) or []):
        n = asyncio.run(mail_sync._sync_folder(None, None, bytes(range(1, 33)), "ab" * 32,
                                               SimpleNamespace(email="me@home.test"), "INBOX"))
    assert n == 0 and fetched == [], "the whole mailbox was re-fetched because the relay did not answer"


def test_the_folder_listing_raises_instead_of_answering_an_empty_inbox():
    with mock.patch.object(nostr_store, "_ws_query", _down):
        with pytest.raises(Exception):
            asyncio.run(mail_store.list_page(bytes(range(1, 33)), "me@home.test", "INBOX"))
