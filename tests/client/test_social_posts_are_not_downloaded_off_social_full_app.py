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


async def _desktop(S, then_open=None):
    """Desktop mode entered from Social (the Windows app, PosterChanOS, a wide browser), then maybe a feed window."""
    author = await S.device(1)
    a = await S.device(0)
    await a.b.call('Emulation.setDeviceMetricsOverride', {'width': 1440, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
    await a.b.js("__PC.switchView('global');true")
    await asyncio.sleep(3)
    await a.b.js("PCOS.enter();true")
    await F.wait(a, "document.body.classList.contains('os-on')", 20, 'the desktop')
    if then_open:
        await a.b.js("PCOS.routeView(%r);true" % then_open)
        await F.wait(a, "!!document.querySelector('#os-root .osw #feed')", 20, 'a window showing the feed')
    await a.b.js(HOOK)
    await asyncio.sleep(4)
    before = await a.b.js("__social")
    for i in range(10):
        await F.post_note(author, 'a desktop post %d (%d)' % (i, S.base))
    await asyncio.sleep(6)
    return before, await a.b.js("__social"), await a.b.js("document.querySelectorAll('#os-root .osw').length")


@scenario
async def nothing_arrives_on_a_desktop_with_no_feed_window(S):
    """Measured 2026-10-28 before the fix: all ten arrived -- VIEW still named the feed with no window showing it."""
    before, after, wins = await _desktop(S)
    assert wins == 0, ('the desktop opened a window by itself', wins)
    assert after == before, ('social posts were downloaded to a desktop with no feed window', before, after)


@scenario
async def a_feed_window_on_the_desktop_does_get_them(S):
    before, after, wins = await _desktop(S, then_open='global')
    assert after - before >= 10, ('a feed window on the desktop received nothing', before, after, wins)


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL)


@pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
def test_nothing_arrives_while_files_is_open(ran):
    ran('nothing_arrives_while_files_is_open')


@pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
def test_they_do_arrive_with_the_global_feed_open(ran):
    ran('they_do_arrive_with_the_global_feed_open')


@pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
def test_nothing_arrives_on_a_desktop_with_no_feed_window(ran):
    ran('nothing_arrives_on_a_desktop_with_no_feed_window')


@pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
def test_a_feed_window_on_the_desktop_does_get_them(ran):
    ran('a_feed_window_on_the_desktop_does_get_them')
