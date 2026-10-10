"""A whole-table read survives one slow page (2026-10-10).

The Blossom index (190k documents) could not load in production: list_all_docs reads 1,000 at a time, and ONE
page timing out threw away the whole read, so the table never loaded and file listings (the music library) answered
503 indefinitely. A page that fails is retried; a page that keeps failing still raises (never a short answer)."""
import asyncio

import pytest

from app.services import nostr_store


def _fake(pages, fail_on):
    """Pages are served newest-first: page i carries stamp 100-i; the cursor is the oldest (stamp, id) seen."""
    calls = {"n": 0}

    async def list_docs(port, prefix, **kw):
        calls["n"] += 1
        if calls["n"] in fail_on:
            raise TimeoutError()
        cur = kw.get("cursor")
        i = 0 if cur is None else 100 - cur[0] + 1
        if i >= len(pages):
            return {}
        return {f"{prefix}{k}": (v, 100 - i, f"{j:03d}") for j, (k, v) in enumerate(pages[i].items())}
    return list_docs, calls


def test_one_failed_page_is_retried(monkeypatch):
    pages = [{f"a{i}": i for i in range(3)}, {f"b{i}": i for i in range(3)}, {"c0": 0}]
    list_docs, calls = _fake(pages, fail_on={2})

    async def ld(port, prefix, **kw):
        return await list_docs(port, prefix, **kw)
    monkeypatch.setattr(nostr_store, "list_docs", ld)
    monkeypatch.setattr(nostr_store, "_PAGE_RETRY_SLEEP", 0.0, raising=False)
    out = asyncio.run(nostr_store.list_all_docs(3052, "p:", page=3))
    assert len(out) == 7, out


def test_a_page_that_keeps_failing_still_raises(monkeypatch):
    async def ld(port, prefix, **kw):
        raise TimeoutError()
    monkeypatch.setattr(nostr_store, "list_docs", ld)
    monkeypatch.setattr(nostr_store, "_PAGE_RETRY_SLEEP", 0.0, raising=False)
    with pytest.raises(Exception):
        asyncio.run(nostr_store.list_all_docs(3052, "p:", page=3))
