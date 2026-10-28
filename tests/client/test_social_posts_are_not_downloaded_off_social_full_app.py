"""Social posts are not downloaded while Social is not open -- bandwidth, CPU and disk writes (the owner, 2026-10-28:
"remember, we are supposed to be optimized to not download social posts when social is not open").

On a real relay: somebody publishes ten posts while this account sits in Files, and not one kind-1/6/7 reaches the
client; the same ten with the global feed open all do -- which is also what proves the counter can see them (a counter
on the page's own WebSocket saw nothing even then: the relay sockets are not the page's). Counted where every delivered
event lands, Store.saveEvent, new ids only.
"""
import asyncio

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

FIRST_FILL = 900
SCENARIOS = {}
COST = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


HOOK = r'''(()=>{window.__social=0;const o=Store.saveEvent.bind(Store);
  Store.saveEvent=ev=>{if(ev&&[1,6,7].includes(ev.kind)&&!Store.has(ev.id))__social++;return o(ev);};return 1})()'''


async def _ten_posts_while(S, view):
    author = await S.device(1)
    a = await S.device(0)
    await a.b.js(HOOK)
    await a.b.js("__PC.switchView(%r);true" % view)
    await asyncio.sleep(6)                         # whatever the screen opened with has arrived
    before = await a.b.js("__social")
    for i in range(10):
        await F.post_note(author, 'a post nobody here asked for %d (%d)' % (i, S.base))
    await asyncio.sleep(6)
    return before, await a.b.js("__social")


@scenario
async def nothing_arrives_while_files_is_open(S):
    before, after = await _ten_posts_while(S, 'blossom')
    assert after == before, ('social posts were downloaded while Social was not open', before, after)


@scenario
async def they_do_arrive_with_the_global_feed_open(S):
    before, after = await _ten_posts_while(S, 'global')
    assert after - before >= 10, ('the counter saw nothing even with the feed open -- it is blind', before, after)


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL)


@pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
def test_nothing_arrives_while_files_is_open(ran):
    ran('nothing_arrives_while_files_is_open')


@pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
def test_they_do_arrive_with_the_global_feed_open(ran):
    ran('they_do_arrive_with_the_global_feed_open')
