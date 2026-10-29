"""Search while typing, and Search -> Social, as a person does it on a phone and on a desktop.

Reported 2026-10-29 (Vyram, video): "The first search starts a little clunky ... Switching from the search to
social has a clunky load." The video showed three things, each measured here on a real node (the shipped
RelayServer + PosterChanDB, the shipped client, real Chrome, typed keys and taps):

  newest     typing starts a search at every pause and Enter starts another; one that answered LATE painted over
             the newer one. Measured: 'kubernetes' showed its 40 posts, then the 'kub' typed on the way replaced
             them with "No matching posts" ~2 s later (the video's "Kub" screen flipping, and its 22 s of spinner
             ending in "didn't answer" over a search that had been retried).
  ngit       every search waited for the ngit index (a stranger's relay, 1.6-6.5 s measured from a server) before
             showing anything, even posts this node answered in milliseconds.
  social     Search set the view itself, so leaving the timeline for it skipped what every other screen does:
             the cards were not kept and the place was not remembered. Coming back rebuilt every card
             (measured at 4x CPU on a phone: 308-431 ms to the first card, 200-245 ms after) and landed at the
             top instead of on the post being read.

Harness: tests/client/discover_flows_harness.py (one node, scenarios three at a time); the ngit index is a slow
stand-in relay the page reaches instead of wss://index.ngit.dev.
Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/discover.js=/tmp/old/discover.js,/static/js/client/app.js=/tmp/old/app.js".
"""
import asyncio
import json
import time

import pytest
import websockets

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('search', 'timeline')
FIRST_FILL = 140
NGIT_DELAY = 5.0
SCENARIOS = {}
COST = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


class SlowIndex(R.FarRelay):
    """The ngit index: every REQ is answered NGIT_DELAY seconds late."""

    async def _handler(self, ws):
        try:
            async for raw in ws:
                m = json.loads(raw)
                if isinstance(m, list) and m and m[0] == 'REQ':
                    await asyncio.sleep(NGIT_DELAY)
                    for e in [e for e in self.events if any(R._match(f, e) for f in m[2:])][:500]:
                        await ws.send(json.dumps(['EVENT', m[1], e]))
                    await ws.send(json.dumps(['EOSE', m[1]]))
        except websockets.ConnectionClosed:
            pass


class Ctx:
    def __init__(self, n):
        self.gate = F.Gate(n.home)
        self.ngit = SlowIndex(n.loop, 'ngit')
        now = int(time.time())
        # A timeline long enough to scroll, from other people.
        n.home.add([R.sign(30 + i % 20, 1, 'Note %d from the timeline, about nothing in particular.' % i, [],
                           now - 60 - i * 30) for i in range(160)])


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


def page_init(S):
    """wss://index.ngit.dev -> the slow stand-in; every other non-loopback socket -> the empty relay."""
    return (r'''(()=>{const W=window.WebSocket;const T=function(u,p){u=String(u);
      if(/index\.ngit\.dev/.test(u))u='%s';else if(!/^wss?:\/\/(127\.0\.0\.1|localhost)[:/]/i.test(u))u='ws://127.0.0.1:__EMPTY__/';
      return p===undefined?new W(u):new W(u,p);};T.prototype=W.prototype;T.CONNECTING=0;T.OPEN=1;T.CLOSING=2;T.CLOSED=3;
      T.__pcLoopback=true;window.WebSocket=T;})();
      window.__paints=[];document.addEventListener('DOMContentLoaded',()=>{const f=document.querySelector('#feed');
        new MutationObserver(()=>{const t=(f.innerText||'');__paints.push({t:performance.now(),
          posts:f.querySelectorAll('#search-posts .note').length,none:/No matching posts/.test(t),spin:!!f.querySelector(':scope > .spinner')})})
        .observe(f,{childList:true});});''' % S.ctx.ngit.url)


async def boot(S, phone):
    d = await S.device(1, phone=phone, pre_init=page_init(S))
    await F.wait(d, "__PC.isView('global') && document.querySelectorAll('#tl-notes .note').length>20", 60, 'the timeline')
    return d


def box_sel(phone):
    return '#search-input' if phone else '#nav-search-input'


async def type_word(d, phone, word, pause_after=None):
    """Focus the search box and type `word` key by key; a pause after `pause_after` letters is long enough for
    the typing debounce to start a search, as a person reading what they typed does."""
    await d.b.js("(()=>{const i=document.querySelector(%s);i.focus();i.value='';return true})()" % json.dumps(box_sel(phone)))
    for k, ch in enumerate(word):
        await d.b.call('Input.insertText', {'text': ch})
        await asyncio.sleep(0.7 if pause_after and k == pause_after - 1 else 0.1)


async def enter(d):
    for t in ('keyDown', 'keyUp'):
        await d.b.call('Input.dispatchKeyEvent', {'type': t, 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13,
                                                 **({'text': '\r'} if t == 'keyDown' else {})})


async def tap(d, phone, selector):
    x, y = await d.b.js("""(()=>{const b=[...document.querySelectorAll(%s)].find(x=>x.offsetParent);
      const r=b.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()""" % json.dumps(selector))
    if phone:
        await d.b.call('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y}]})
        await d.b.call('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    else:
        for t in ('mousePressed', 'mouseReleased'):
            await d.b.call('Input.dispatchMouseEvent', {'type': t, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


# ------------------------------------------------------------------------------------------- newest wins
async def newest_search_wins(S, phone):
    word = 'kubernetes%d' % S.base
    a = await S.device(0, pre_init=page_init(S))
    note = await F.post_note(a, 'The %s upgrade went fine' % word)
    # The partial word typed on the way is answered late, the whole word at once.
    part = word[:3]
    slow = S.ctx.gate.add(lambda fl, c: 2.5 if any(f.get('search') == part for f in fl) else None)
    try:
        d = await boot(S, phone)
        await type_word(d, phone, word, pause_after=3)
        t0 = await d.b.js('performance.now()')
        await enter(d)
        posts = "[...document.querySelectorAll('#search-posts .note')].map(n=>n.dataset.id)"
        await F.wait(d, "%s.includes(%s)" % (posts, json.dumps(note['id'])), 30, 'the post found by its word')
        await asyncio.sleep(7)              # past the moment the late partial-word answer lands
        assert note['id'] in await d.b.js(posts), ('the results for what was typed were replaced', await d.b.js(F.STATE))
        later = await d.b.js("__paints.filter(p=>p.t>%f)" % t0)
        shown = [i for i, p in enumerate(later) if p['posts']]
        assert not [p for p in later[shown[0]:] if p['none']], \
            ('"No matching posts" was painted over the results of the search that was typed', later)
    finally:
        S.ctx.gate.drop(slow)


@scenario
async def newest_search_wins_phone(S):
    await newest_search_wins(S, True)


@scenario
async def newest_search_wins_desktop(S):
    await newest_search_wins(S, False)


@CHROME
def test_newest_search_wins_phone(ran):
    ran('newest_search_wins_phone')


@CHROME
def test_newest_search_wins_desktop(ran):
    ran('newest_search_wins_desktop')


# ------------------------------------------------------------------------------------------- ngit
@scenario
async def posts_do_not_wait_for_the_ngit_index(S):
    word = 'zeppelin%d' % S.base
    a = await S.device(0, pre_init=page_init(S))
    note = await F.post_note(a, 'A %s over the bay' % word)
    repo = R.sign(70, 30617, '', [['d', word + '-tools'], ['name', word + '-tools'], ['description', 'tools']])
    S.ctx.ngit.events.append(repo)
    d = await boot(S, True)
    # Typed, then the pause after typing starts the search (no Enter: one search, the one being timed).
    await type_word(d, True, word)
    t0 = time.monotonic()
    posts = "[...document.querySelectorAll('#search-posts .note')].map(n=>n.dataset.id)"
    await F.wait(d, "%s.includes(%s)" % (posts, json.dumps(note['id'])), 30, 'the post found by its word')
    took = time.monotonic() - t0
    assert took < NGIT_DELAY - 2, ('the posts waited for the ngit index', round(took, 1))
    # …and the index's repository still arrives, in Git Repos.
    await F.wait(d, "/GIT REPOS[\\s\\S]*%s-tools[\\s\\S]*POSTS/i.test(document.querySelector('#feed').innerText)" % word,
                 20, 'the ngit repository in Git Repos')
    assert note['id'] in await d.b.js(posts)


@CHROME
def test_posts_do_not_wait_for_the_ngit_index(ran):
    ran('posts_do_not_wait_for_the_ngit_index')


# ------------------------------------------------------------------------------------------- back to Social
CARDS = """(()=>{const kids=[...document.querySelectorAll('#tl-notes > .note, #tl-notes > .reply-pair')];
  const f=document.querySelector('#feed'),top=f.getBoundingClientRect().top;
  const first=kids.find(x=>x.getBoundingClientRect().bottom>top+60);
  return {n:kids.length,kept:kids.filter(x=>x.__pcMark).length,firstKept:!!(first&&first.__pcMark),
    composer:!!document.querySelector('#feed .tl-cmp'),scroll:Math.round(f.scrollTop)}})()"""


async def back_to_social(S, phone):
    """Social (the nav button lands at the top, by design) after a search shows the cards it was showing --
    not a rebuild of every one of them, which is the half-second a phone spent on it."""
    d = await boot(S, phone)
    await d.b.js("(()=>{for(const c of document.querySelectorAll('#tl-notes > *'))c.__pcMark=1;return true})()")
    await type_word(d, phone, 'nothing%d' % S.base)
    await enter(d)
    await F.wait(d, "!!document.querySelector('#feed .search-section-title')", 30, 'the search results')
    await tap(d, phone, '.nav-item[data-view=global]')
    await F.wait(d, "__PC.isView('global') && document.querySelectorAll('#tl-notes .note').length>20", 30, 'Social again')
    await asyncio.sleep(1)
    got = await d.b.js(CARDS)
    assert got['composer'] and got['scroll'] < 5, ('Social did not come back whole, at the top', got)
    assert got['firstKept'] and got['kept'] >= 0.9 * got['n'], ('every card was rebuilt coming back from Search', got)


@scenario
async def back_to_social_phone(S):
    await back_to_social(S, True)


@scenario
async def back_to_social_desktop(S):
    await back_to_social(S, False)


@CHROME
def test_back_to_social_phone(ran):
    ran('back_to_social_phone')


@CHROME
def test_back_to_social_desktop(ran):
    ran('back_to_social_desktop')
