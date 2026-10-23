"""Search and Trending, driven end to end between real accounts on this node's REAL relay.

Every post and every reaction here is made by the shipped client -- the ＋ composer and a card's react button --
on one account's device, and read on ANOTHER account's FRESH device (nothing cached), through the shipped
RelayServer's own NIP-50 search and the node's real /client/trending route (app/routers/client.py ->
trending_service, which asks the same relay). The failure conditions are the ones a person meets: a relay that
answers late, one that never answers a read, and an instance whose ranking endpoint answers 503.

  search    the word another account posted finds their post, which opens; a late answer still lands; a
            search nobody answers says so (never "No matching posts") and its Search again finds it.
  trending  two accounts' reactions put a third account's post on a fourth's Hot list with its count, ranked by
            the instance; with the instance answering 503 it ranks from the relay instead; and when the posts
            themselves cannot be fetched, the screen says it could not load them -- never "Nothing trending".

Harness: tests/client/discover_flows_harness.py (one node per module, scenarios three at a time).
Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/discover.js=/tmp/old/discover.js".
"""
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('search', 'trending')
FIRST_FILL = 60
SCENARIOS = {}
COST = {'two_accounts_reactions_put_a_post_on_a_third_accounts_hot_list': 30,
        'hot_ranks_from_the_relay_when_the_instance_answers_503': 25,
        'a_search_nobody_answers_says_so_and_search_again_finds_it': 25}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


class Ctx:
    def __init__(self, n):
        from fastapi import APIRouter
        from app.routers import client
        from app.services import settings_store, trending_service
        self.mp = pytest.MonkeyPatch()
        # The instance's ranking reads THIS node's relay (the route's own _ws_query), fresh on every ask.
        self.mp.setattr(settings_store, '_port', lambda db=None: n.home.port)
        self.mp.setattr(trending_service, 'TTL', 0.0)
        r = APIRouter()
        r.add_api_route('/client/trending', client.client_trending, methods=['GET'])
        self.api = F.Routers(self.mp, [(r, '')], {}, paths=['/client/trending'])
        self.gate = F.Gate(n.home)

    def stop(self):
        self.api.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


# ------------------------------------------------------------------------------------------------ search

async def search(d, q):
    """Type into the search box and press Enter, as a person does."""
    await d.b.js("""(q=>{const i=document.querySelector('#nav-search-input')||document.querySelector('#search-input');
      i.value=q;i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return true})(%s)""" % json.dumps(q))


POSTS = "[...document.querySelectorAll('#search-posts .note')].map(n=>n.dataset.id)"


@scenario
async def a_post_is_found_by_another_account_and_opens(S):
    word = 'quokka%d' % S.base
    a = await S.device(0)
    note = await F.post_note(a, 'A %s at the pier this morning' % word)
    b = await S.device(1)                                   # another account, a fresh device
    await search(b, word)
    await F.wait(b, "%s.includes(%s)" % (POSTS, json.dumps(note['id'])), 40, 'the post found by its word')
    assert 'No matching posts' not in await b.b.js("document.querySelector('#feed').innerText")
    await b.b.js("document.querySelector('#search-posts .note[data-id=\"%s\"] .content, "
                 "#search-posts .note[data-id=\"%s\"]').click();true" % (note['id'], note['id']))
    await F.wait(b, "__PC.isView('thread') && /at the pier this morning/.test(document.querySelector('#feed').innerText)",
                 30, 'the found post opened')


@CHROME
def test_a_post_is_found_by_another_account_and_opens(ran):
    ran('a_post_is_found_by_another_account_and_opens')


@scenario
async def a_search_nobody_answers_says_so_and_search_again_finds_it(S):
    word = 'wombat%d' % S.base
    a = await S.device(0)
    note = await F.post_note(a, 'Spotted a %s by the creek' % word)
    gate = S.ctx.gate
    b = await S.device(1)

    # A slow relay: the answer comes 2.5 s late -- it still lands as results, with no "nothing found" on the way.
    slow = gate.add(lambda fl, c: 2.5 if any(f.get('search') == word for f in fl) else None)
    await search(b, word)
    await F.wait(b, "%s.includes(%s) || /No matching posts|didn.t answer/.test(document.querySelector('#feed').innerText)"
                 % (POSTS, json.dumps(note['id'])), 40, 'the late answer')
    assert note['id'] in await b.b.js(POSTS), await b.b.js(F.STATE)
    gate.drop(slow)

    # Nobody answers at all: the screen says the relays did not answer, offers the retry, never "No matching posts".
    never = gate.add(lambda fl, c: 'never' if any(f.get('search') == word for f in fl) else None)
    await b.b.js("__PC.switchView('home');true")
    await search(b, word)
    await F.wait(b, "!!document.querySelector('#search-retry')", 60, 'the unanswered search said so')
    text = await b.b.js("document.querySelector('#feed').innerText")
    assert 'No matching posts' not in text and not await b.b.js("!!document.querySelector('#feed .spinner')"), text
    gate.drop(never)
    await b.b.js("document.querySelector('#search-retry').click();true")
    await F.wait(b, "%s.includes(%s)" % (POSTS, json.dumps(note['id'])), 40, 'Search again found it')


@CHROME
def test_a_search_nobody_answers_says_so_and_search_again_finds_it(ran):
    ran('a_search_nobody_answers_says_so_and_search_again_finds_it')


# ------------------------------------------------------------------------------------------------ trending

ITEM = ("(()=>{const n=document.querySelector('#tl-notes .note[data-id=\"%s\"]');const it=n&&n.closest('.tr-item');"
        "return it?(it.querySelector('.tr-heat')||{}).textContent:null})()")


async def hot_post(S):
    """Account 0 posts; accounts 1 and 2 each react to it from their own device."""
    a = await S.device(0)
    note = await F.post_note(a, 'Hot take #%d: tabs beat spaces' % S.base)
    for i in (1, 2):
        d = await S.device(i)
        await F.open_note(d, note['id'])
        await F.react(d, note['id'], '🔥')
    return note


async def open_trending(d):
    await d.b.js("try{localStorage.setItem('trTab','hot')}catch(_){};__PC.switchView('trending');true")


@scenario
async def two_accounts_reactions_put_a_post_on_a_third_accounts_hot_list(S):
    note = await hot_post(S)
    asked = len([p for _m, p in S.ctx.api.seen if p == '/client/trending'])
    d = await S.device(3)                                   # a fourth account, fresh device
    await open_trending(d)
    await F.wait(d, ITEM % note['id'] + " !== null", 40, 'the post on Hot')
    heat = await d.b.js(ITEM % note['id'])
    assert '2 reactions' in heat, heat
    assert len([p for _m, p in S.ctx.api.seen if p == '/client/trending']) > asked, 'Hot did not ask the instance'
    assert 'tabs beat spaces' in await d.b.js("document.querySelector('#tl-notes').innerText")


@CHROME
def test_two_accounts_reactions_put_a_post_on_a_third_accounts_hot_list(ran):
    ran('two_accounts_reactions_put_a_post_on_a_third_accounts_hot_list')


@scenario
async def hot_ranks_from_the_relay_when_the_instance_answers_503(S):
    note = await hot_post(S)
    tag = 'tr503-%d' % S.base
    S.ctx.api.fail('/client/trending', 503, tag)
    try:
        d = await S.device(3, tag=tag)
        await open_trending(d)
        await F.wait(d, ITEM % note['id'] + " !== null", 40, 'the post on Hot, ranked from the relay')
        assert '2 reactions' in await d.b.js(ITEM % note['id'])
        assert ('/client/trending', tag) in S.ctx.api.faulted, 'the instance was never asked (so never refused)'
    finally:
        S.ctx.api.heal('/client/trending', tag)


@CHROME
def test_hot_ranks_from_the_relay_when_the_instance_answers_503(ran):
    ran('hot_ranks_from_the_relay_when_the_instance_answers_503')


@scenario
async def posts_that_cannot_be_fetched_are_not_nothing_trending(S):
    note = await hot_post(S)
    gate = S.ctx.gate
    # The instance ranked it; this device's relay never hands over a post (no read of notes is answered).
    tag = 'tr%d' % S.base
    rule = gate.add(lambda fl, c: 'never' if F.device_of(c) == tag and F.asks(fl, kinds=(1,)) or (
        F.device_of(c) == tag and F.asks(fl, ids=True)) else None)
    try:
        d = await S.device(3, tag=tag)
        await open_trending(d)
        await F.wait(d, "!!document.querySelector('#tl-notes .empty') || !!document.querySelector('#tl-notes .note')",
                     60, 'Hot finished loading')
        text = await d.b.js("document.querySelector('#tl-notes').innerText")
        assert 'Nothing trending' not in text, ('posts that could not be fetched were reported as nothing trending', text)
        assert await d.b.js("!!document.querySelector('#tr-retry')"), text
        gate.drop(rule)
        await d.b.js("document.querySelector('#tr-retry').click();true")
        await F.wait(d, ITEM % note['id'] + " !== null", 40, 'Try again shows the post')
    finally:
        gate.drop(rule)


@CHROME
def test_posts_that_cannot_be_fetched_are_not_nothing_trending(ran):
    ran('posts_that_cannot_be_fetched_are_not_nothing_trending')
