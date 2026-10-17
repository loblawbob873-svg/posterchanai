"""Trending -> Hot is ranked on the server once a minute, not from 3,000 reactions pulled to every phone.

Vyram's video (2026-10-16): Trending spun for ~4 s, then "Couldn't reach your relays just now" -- the query was
2.2 MB of kind 6/7 events, which a phone on mobile data could not fetch inside the client's timer.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import nostr_store, trending_service
from app.services.relay_reader import Unavailable


def _r(i, target, kind=7):
    return {"id": "%064x" % i, "kind": kind, "pubkey": "a" * 64, "created_at": 1, "tags": [["e", "root"], ["e", target]]}


@pytest.fixture(autouse=True)
def fresh():
    trending_service._cache.clear()
    trending_service._locks.clear()


def test_the_tally_is_the_clients(monkeypatch):
    rows = trending_service.rank([_r(1, "x"), _r(2, "x"), _r(3, "y", kind=6), _r(4, "x")])
    assert rows[0] == ["x", 3, "❤️"] and rows[1] == ["y", 1, "\U0001F501"], rows


def test_many_phones_at_once_cost_one_relay_query(monkeypatch):
    calls = []

    async def q(port, filters, strict=False, **kw):
        calls.append(filters)
        await asyncio.sleep(0.05)
        return [_r(1, "x")]
    monkeypatch.setattr(nostr_store, "_ws_query", q)

    async def many():
        return await asyncio.gather(*(trending_service.hot(86400) for _ in range(40)))
    answers = asyncio.run(many())
    assert len(calls) == 1 and all(a["rows"] == [["x", 1, "❤️"]] for a in answers)
    assert calls[0][0]["kinds"] == [6, 7] and calls[0][0]["limit"] == 3000
    assert trending_service.window_of(100000) == 2 * 86400 and trending_service.window_of(10 ** 9) == 30 * 86400


def test_a_relay_that_could_not_be_asked_is_a_503_never_an_empty_list(monkeypatch):
    async def down(port, filters, strict=False, **kw):
        raise Unavailable("down (test)")
    monkeypatch.setattr(nostr_store, "_ws_query", down)
    from app.routers import client
    app = FastAPI()
    app.include_router(client.router)
    r = TestClient(app).get("/client/trending?window=86400")
    assert r.status_code == 503, r.text
