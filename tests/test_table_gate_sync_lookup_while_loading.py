"""A synchronous single-row lookup on the event loop answers from the relay while the table is still loading.

2026-10-10, production right after wave 2 shipped: mail sync answered 503 for 10+ minutes. Mail reads the user's
`mail_accounts` row through the SYNCHRONOUS user_settings lookup, which -- on the event loop, table not loaded --
could only say "still loading"; and the whole-table load kept timing out while the relay was busy with wave 1's
copy. One row needs one relay read, not the whole table. These tests fail against the old get_row."""
import asyncio

import pytest

from app.services import table_gate
from app.services.relay_reader import Unavailable


class FakeTable:
    name = "fake"
    loaded = False

    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.remote_calls = rows or {}, fail, 0

    async def aget_remote(self, k):
        self.remote_calls += 1
        if self.fail:
            raise Unavailable("relay down")
        r = self.rows.get(k)
        return dict(r) if r is not None else None

    async def aload(self, **kw):           # the background load the old code kicked off
        raise Unavailable("still busy")

    def peek(self, k):
        return None


def test_a_row_is_read_from_the_relay_while_the_table_loads():
    t = FakeTable({"u1:mail_accounts": {"value": "[]"}})

    async def on_loop():
        return table_gate.get_row(t, "u1:mail_accounts")
    assert asyncio.run(on_loop()) == {"value": "[]"}
    assert t.remote_calls == 1


def test_a_missing_row_is_none_not_unavailable():
    t = FakeTable({})

    async def on_loop():
        return table_gate.get_row(t, "nope")
    assert asyncio.run(on_loop()) is None


def test_could_not_ask_is_still_unavailable():
    t = FakeTable(fail=True)

    async def on_loop():
        return table_gate.get_row(t, "u1:mail_accounts")
    with pytest.raises(Unavailable):
        asyncio.run(on_loop())
