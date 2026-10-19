"""A NEGENTROPY SESSION MUST NOT STOP THE RELAY SERVING EVERYBODY ELSE.

`_on_neg_open`/`_on_neg_msg` ran `negentropy.reconcile` straight on the relay's event loop. A round is pure CPU in
proportion to the set and to how far the two sides differ -- measured, a 50k set ~90 ms per session and 200k ~800
ms -- and for that long no other socket on the relay was read or written: every REQ, EVENT and EOSE in the node
waited behind one PosterChanOS desktop's periodic sync (or any external client's). The rounds now run on a worker
thread; the connection's own frames are still dispatched one at a time, so a session's rounds stay in order.

The test drives the REAL entry points with a 50k-item set and a client that differs by 2% (a heavy second
round), while another coroutine on the same loop measures how late its 1 ms sleeps wake up. It also checks the
answers are exactly what the pre-change module gives, so "responsive" cannot be bought by answering wrongly.
"""
import asyncio
import importlib.util
import pathlib
import random
import time

from app.services.nostr_relay.server import RelayServer

_REF = pathlib.Path(__file__).parent / "fixtures" / "negentropy_reference_before_itemset.py"
_spec = importlib.util.spec_from_file_location("negentropy_reference_before_itemset", _REF)
ref = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ref)

N = 50_000
BUDGET = 0.020      # the longest any other coroutine may be kept waiting


def _items(rnd, n):
    out = {}
    while len(out) < n:
        out[rnd.randbytes(32)] = 1_700_000_000 + rnd.randrange(5_000_000)
    return sorted((ts, i) for i, ts in out.items())


class Store:
    def __init__(self, items):
        self.items = items

    async def neg_items(self, filters, cap=0):
        return list(self.items)


class Gate:
    def is_member(self, _pk): return True
    def is_operator(self, _pk): return False
    def is_blocked(self, _pk): return False
    def is_puppet_event(self, _ev): return False


def test_a_50k_reconcile_never_holds_the_loop_for_20ms():
    rnd = random.Random(5)
    ours = _items(rnd, N)
    theirs = sorted(set(x for x in ours if rnd.random() > 0.02) | set(_items(rnd, 500)))

    # The whole transcript is worked out BEFORE the loop starts, with the reference relay: the client's own
    # rounds are the client's CPU, not the relay's, and must not be what the ticker measures.
    ini = ref.Initiator(theirs)
    queries, expected = [], []
    msg = ini.initiate()
    while msg is not None:
        queries.append(msg)
        expected.append(ref.reconcile(ours, msg))
        msg = ini.reconcile(expected[-1])
    assert len(queries) >= 2 and ini.need == {i for _, i in ours} - {i for _, i in theirs}

    srv = RelayServer(Store(ours), Gate(), {"wot_enabled": False})
    sent = []
    srv._send = lambda conn, obj: sent.append(obj)
    conn = object()

    async def main():
        gaps, stop = [], asyncio.Event()

        async def ticker():
            while not stop.is_set():
                t = time.perf_counter()
                await asyncio.sleep(0.001)
                gaps.append(time.perf_counter() - t - 0.001)

        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.02)                     # the ticker is running before the first frame arrives
        gaps.clear()
        started = time.perf_counter()
        await srv._on_neg_open(conn, "s", {"kinds": [1]}, queries[0].hex())
        for q in queries[1:]:
            await srv._on_neg_msg(conn, "s", q.hex())
        busy = time.perf_counter() - started
        stop.set()
        await tick
        return gaps, busy

    gaps, busy = asyncio.run(main())
    rounds = len(queries)
    # Byte-identical to the module before the change, round for round.
    assert [(m[0], m[1]) for m in sent] == [("NEG-MSG", "s")] * rounds, sent[-1][:2]
    assert [bytes.fromhex(m[2]) for m in sent] == expected
    worst = max(gaps)       # a blocked loop shows up as ONE huge gap: the ticker's sleep that spanned the session
    assert worst < BUDGET, (
        f"another coroutine waited {worst * 1000:.1f} ms while a {N}-item negentropy session ran "
        f"({busy * 1000:.0f} ms, {rounds} rounds) -- the reconcile is blocking the relay's event loop")
    assert len(gaps) > 5, "the ticker barely ran while the session did"
