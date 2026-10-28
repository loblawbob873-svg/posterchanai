"""Bookmarks and Drafts, driven end to end on a REAL node (the shipped RelayServer over PosterChanDB) with the node's
REAL drafts route (`/client/drafts`, app/routers/client.py: one document encrypted under the account's server-held
storage key, read STRICTLY from this node's relay before every merge-and-write).

  bookmarks  another account posts with the shipped composer; this account bookmarks it from the post's ☰ menu
             (a NIP-51 kind 10003 the client publishes); Bookmarks lists it after a reload and on a FRESH device, and
             a removal made there sticks. A fresh device whose relay never answers the list read must not replace
             the account's list with the one post bookmarked there (the "fresh device on a silent relay wipes your
             data" class: follows had the gate, bookmarks did not), and must not tell the person they have none.
             A slow answer fills the screen in when it lands.
  drafts     a draft saved with the composer's ☁ button opens on a fresh device and a deletion there sticks; a
             fresh device whose node cannot read the drafts document (the relay never answers the storage key's
             read -> the router's own strict 503) says so instead of "No drafts", overwrites nothing, and the draft
             written there reaches the other devices once the node can read again; an answer that is slow to come
             is drawn when it lands.

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/app.js=/tmp/x/app.js".
"""
import asyncio
import hashlib
import json
import time

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('bookmarks', 'drafts')
FIRST_FILL = 170
SCENARIOS = {}
COST = {'a_fresh_device_nobody_answers_never_replaces_the_bookmarks': 40,
        'a_fresh_device_the_node_cannot_read_drafts_for_overwrites_nothing': 40,
        'a_bookmark_is_kept_after_a_reload_and_on_a_fresh_device': 35,
        'a_draft_opens_on_a_fresh_device_and_a_deletion_sticks': 30}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# ------------------------------------------------------------------------------------------------ the node
def storage_sk(user):
    return hashlib.sha256(b'pc-test-storage:' + str(user.nostr_npub).encode()).digest()


class Ctx:
    def __init__(self, n):
        from fastapi import APIRouter
        from app.routers import client
        from app.services import nostr_store, settings_store, user_table
        self.mp = mp = pytest.MonkeyPatch()
        self.n = n
        users = F.members(FIRST_FILL, SCENARIOS)
        self.users = users
        by_npub = {}
        from app.services.nostr import nostr_service
        for pk, u in users.items():
            by_npub[nostr_service.npub_of(pk)] = u
            by_npub[pk] = u

        async def aby_npub(_db, npub):
            return by_npub.get(npub)
        mp.setattr(user_table, 'aby_npub', aby_npub)
        # The account's server-held storage key: one per account, derived here (the real one is wrapped in the DB).
        mp.setattr(nostr_store, 'user_storage_seckey', lambda _db, u: storage_sk(u))
        real_get = settings_store.get
        mp.setattr(settings_store, 'get', lambda k, *a, **kw: str(n.home.port) if k == 'nostr_relay_port'
                   else real_get(k, *a, **kw))
        r = APIRouter()
        r.add_api_route('/client/drafts', client.drafts_sync, methods=['POST'])
        self.api = F.Routers(mp, [(r, '')], users, paths=['/client/drafts'])
        self.gate = F.Gate(n.home)

    def spk(self, pk):
        from app.services.nostr import bip340
        return bip340.pubkey_from_seckey(storage_sk(self.users[pk])).hex()

    def server_drafts(self, pk):
        """The account's drafts as the NODE holds them (decrypted with its storage key), live ones only."""
        from app.services import nostr_store
        doc = asyncio.run_coroutine_threadsafe(
            nostr_store.get_doc(self.n.home.port, 'pcai:drafts', seckey=storage_sk(self.users[pk]), strict=True),
            self.api.loop).result(30)
        return [d.get('text') for d in ((doc or {}).get('drafts') or []) if isinstance(d, dict) and not d.get('del')]

    def stop(self):
        self.api.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


async def reload(d):
    """The app reopened: back to /client (a thread's address is the client's own route, not this test instance's)."""
    await d.b.js("window.__beforeReload=1;true")
    await d.b.call('Page.navigate', {'url': d.node.origin + '/client'})
    await R.wait(d.b, "!window.__beforeReload && !!window.__PC && !!__PC.me && !!__PC.me() && Relay.status==='ok'", 60,
                 'reopened and signed in')


# ------------------------------------------------------------------------------------------------ bookmarks
def bookmarked(n, pk):
    """The e-tags of the account's newest kind-10003 on the home relay (None: there is no list at all)."""
    evs = n.home.query({'kinds': [10003], 'authors': [pk]})
    if not evs:
        return None
    ev = max(evs, key=lambda e: e['created_at'])
    return [t[1] for t in ev['tags'] if t and t[0] == 'e']


async def press_bookmark(d, note_id):
    """Open the post -> its ☰ menu -> 🔖 Bookmark (or Remove bookmark), as a person does."""
    await F.open_note(d, note_id)
    card = F.CARD % note_id
    await F.wait(d, "!!%s.querySelector('.act[data-a=menu]')" % card, 20, 'the post\'s ☰ button')
    await d.b.js("%s.querySelector('.act[data-a=menu]').click();true" % card)
    await F.wait(d, "!!document.querySelector('.menu-pop [data-m=bookmark]')", 20, 'the post menu')
    await d.b.js("document.querySelector('.menu-pop [data-m=bookmark]').click();true")


LISTED = "[...document.querySelectorAll('#feed .note[data-id]')].map(x=>x.dataset.id)"


async def open_bookmarks(d):
    await d.b.js("__PC.switchView('bookmarks');true")


@scenario
async def a_bookmark_is_kept_after_a_reload_and_on_a_fresh_device(S):
    author = await S.device(1)
    keep = await F.post_note(author, 'Sourdough starter notes (%d)' % S.base)
    drop = await F.post_note(author, 'Hydration chart (%d)' % S.base)
    a = await S.device(0)
    await press_bookmark(a, keep['id'])
    await F.until(lambda: bookmarked(S.n, S.pk(0)) == [keep['id']], 30, 'the bookmark on the home relay', a)
    await press_bookmark(a, drop['id'])
    await F.until(lambda: sorted(bookmarked(S.n, S.pk(0)) or []) == sorted([keep['id'], drop['id']]), 30,
                  'both bookmarks on the home relay', a)
    await reload(a)
    await open_bookmarks(a)
    try:
        await F.wait(a, "%s.includes(%s) && %s.includes(%s)" % (LISTED, json.dumps(keep['id']), LISTED, json.dumps(drop['id'])),
                     40, 'both bookmarks listed after a reload')
    except Exception as failure:
        # Intermittent (gate, 2026-10-26: one bookmark listed while the relay held both). Say what the client held,
        # so the next failure names the stale copy instead of only "one bookmark".
        try:
            state = await a.b.js(r"""(async()=>{const row=e=>[e.created_at,e.id.slice(0,8),
            e.tags.filter(t=>t[0]==='e').map(t=>t[1].slice(0,8))], mine=Store.query([{kinds:[10003]}]);
          const pk=(mine[0]||{}).pubkey; return JSON.stringify({store:mine.map(row), listed:""" + LISTED + r""",
            relay:pk?(await Relay.query([{kinds:[10003],authors:[pk]}])).map(row):null})})()""")
        except Exception as e:      # noqa: BLE001 -- the dump must never replace the real failure
            state = "could not read: %r" % (e,)
        # In the failure's own message: the gate's summary prints that, and never a test's stdout.
        raise AssertionError("%s | client: %s | home relay: %s | keep %s drop %s" % (
            failure, state, bookmarked(S.n, S.pk(0)), keep['id'][:8], drop['id'][:8])) from failure
    b = await S.device(0)                                   # a fresh device, same account
    await open_bookmarks(b)
    await F.wait(b, "%s.includes(%s) && %s.includes(%s)" % (LISTED, json.dumps(keep['id']), LISTED, json.dumps(drop['id'])),
                 40, 'both bookmarks listed on a fresh device')
    await press_bookmark(b, drop['id'])                     # removed there...
    await F.until(lambda: bookmarked(S.n, S.pk(0)) == [keep['id']], 30, 'the removal on the home relay', b)
    await reload(a)                                         # ...is removed here
    await open_bookmarks(a)
    await F.wait(a, "%s.includes(%s)" % (LISTED, json.dumps(keep['id'])), 40, 'the kept bookmark')
    await asyncio.sleep(1)
    assert drop['id'] not in await a.b.js(LISTED), 'a bookmark removed on another device is still listed'


@CHROME
def test_a_bookmark_is_kept_after_a_reload_and_on_a_fresh_device(ran):
    ran('a_bookmark_is_kept_after_a_reload_and_on_a_fresh_device')


@scenario
async def a_fresh_device_nobody_answers_never_replaces_the_bookmarks(S):
    author = await S.device(1)
    one = await F.post_note(author, 'Bike route to the coast (%d)' % S.base)
    two = await F.post_note(author, 'Tide tables for May (%d)' % S.base)
    a = await S.device(0)
    await press_bookmark(a, one['id'])
    await F.until(lambda: bookmarked(S.n, S.pk(0)) == [one['id']], 30, 'the first bookmark on the home relay', a)

    tag, pk = 'bm%d' % S.base, S.pk(0)
    rule = S.ctx.gate.add(lambda fl, c: 'never' if F.device_of(c) == tag and F.asks(fl, kinds=(10003,), authors=(pk,))
                          else None)
    try:
        b = await S.device(0, tag=tag)
        await open_bookmarks(b)
        await F.wait(b, "!document.querySelector('#feed .spinner') && document.querySelector('#feed').innerText.length>0",
                     40, 'Bookmarks gave up waiting')
        text = await b.b.js("document.querySelector('#feed').innerText")
        # Whatever the screen says, a person bookmarks the post in front of them.
        await press_bookmark(b, two['id'])
        loop = asyncio.get_running_loop()
        end = loop.time() + 10                              # the read times out (6 s) and a wrong publish lands after it
        while loop.time() < end:
            assert bookmarked(S.n, pk) == [one['id']], \
                ('a device that never read the bookmark list replaced it', bookmarked(S.n, pk))
            await asyncio.sleep(.25)
        assert any('bookmark' in t.lower() and ('couldn' in t.lower() or 'could not' in t.lower())
                   for t in await b.toasts()), ('the failed bookmark was not reported', await b.toasts())
        assert 'No bookmarks yet' not in text, ('an unanswered read told the person they have no bookmarks', text)
        S.ctx.gate.drop(rule)
        # The relay answers now. Reopening Bookmarks asks for the list again by itself (a list no relay has confirmed
        # this session is re-read when the view opens), so the saved bookmark may already be there; if the screen
        # still offers Try again, press it, as a person would. Either way the saved bookmark must be listed.
        await open_bookmarks(b)
        has = "%s.includes(%s)" % (LISTED, json.dumps(one['id']))
        await F.wait(b, "!!document.querySelector('#bm-retry') || " + has, 20, 'Bookmarks offered to try again or listed it')
        await b.b.js("(()=>{const r=document.querySelector('#bm-retry'); if(r) r.click(); return true})()")
        await F.wait(b, has, 40, 'the saved bookmark listed once the relay answered')
        await press_bookmark(b, two['id'])                  # the bookmark is ADDED to the list
        await F.until(lambda: sorted(bookmarked(S.n, pk) or []) == sorted([one['id'], two['id']]), 30,
                      'both bookmarks on the home relay', b)
    finally:
        S.ctx.gate.drop(rule)


@CHROME
def test_a_fresh_device_nobody_answers_never_replaces_the_bookmarks(ran):
    ran('a_fresh_device_nobody_answers_never_replaces_the_bookmarks')


@scenario
async def a_slow_bookmark_list_is_drawn_when_it_lands(S):
    author = await S.device(1)
    note = await F.post_note(author, 'Lighthouse open day (%d)' % S.base)
    a = await S.device(0)
    await press_bookmark(a, note['id'])
    await F.until(lambda: bookmarked(S.n, S.pk(0)) == [note['id']], 30, 'the bookmark on the home relay', a)
    tag, pk = 'bs%d' % S.base, S.pk(0)
    rule = S.ctx.gate.add(lambda fl, c: 3 if F.device_of(c) == tag and F.asks(fl, kinds=(10003,), authors=(pk,)) else None)
    try:
        b = await S.device(0, tag=tag)
        await open_bookmarks(b)
        await F.wait(b, "%s.includes(%s)" % (LISTED, json.dumps(note['id'])), 40, 'the bookmark, once the slow read landed')
    finally:
        S.ctx.gate.drop(rule)


@CHROME
def test_a_slow_bookmark_list_is_drawn_when_it_lands(ran):
    ran('a_slow_bookmark_list_is_drawn_when_it_lands')


# ------------------------------------------------------------------------------------------------ drafts
DRAFTS = "[...document.querySelectorAll('#feed .draft-card .draft-body')].map(x=>x.innerText.trim())"


async def save_draft(d, text):
    """Home -> ＋ -> type -> ☁ Save to drafts, as a person keeps a post for later."""
    await F.view(d, 'home', "!!document.querySelector('#tl-fab')")
    await d.b.js("document.querySelector('#tl-fab').click();true")
    await F.wait(d, "!!document.querySelector('#cmp') && !!document.querySelector('#cmp-draft')", 20, 'the composer')
    await d.b.js("(()=>{const t=document.querySelector('#cmp');t.focus();t.value=%s;"
                 "t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-draft').click();return true})()"
                 % json.dumps(text))
    await F.wait(d, "(window.__toastLog||[]).some(t=>/saved to drafts/.test(t))", 20, 'the draft saved')


async def open_drafts(d):
    await d.b.js("__PC.switchView('drafts');true")
    await F.wait(d, "!!document.querySelector('#feed #sched-section')", 30, 'the Drafts screen')


async def confirm(d):
    await F.wait(d, "!!document.querySelector('.uiconfirm-bg [data-uc=\"1\"]')", 20, 'the confirm dialog')
    await d.b.js("document.querySelector('.uiconfirm-bg [data-uc=\"1\"]').click();true")


@scenario
async def a_draft_opens_on_a_fresh_device_and_a_deletion_sticks(S):
    pk = S.pk(0)
    a = await S.device(0)
    await save_draft(a, 'Thread about the harbour fireworks (%d)' % S.base)
    await save_draft(a, 'Reply to the bakery (%d)' % S.base)
    await F.until(lambda: len(S.ctx.server_drafts(pk)) == 2, 30, 'both drafts in the node\'s document', a)
    spk = S.ctx.spk(pk)
    stored = F.stored(S.n, {'kinds': [30078], 'authors': [spk], '#d': ['pcai:drafts']})
    assert stored and 'harbour fireworks' not in stored[0]['content'], 'the drafts document is not encrypted'
    b = await S.device(0)
    await open_drafts(b)
    await F.wait(b, "%s.some(t=>t.includes('harbour fireworks')) && %s.some(t=>t.includes('bakery'))" % (DRAFTS, DRAFTS),
                 40, 'both drafts on the fresh device')
    await b.b.js("[...document.querySelectorAll('#feed .draft-card')].find(c=>c.innerText.includes('bakery'))"
                 ".querySelector('[data-act=del]').click();true")
    await confirm(b)
    await F.until(lambda: S.ctx.server_drafts(pk) == ['Thread about the harbour fireworks (%d)' % S.base], 30,
                  'the deletion in the node\'s document', b)
    await reload(a)
    await open_drafts(a)
    await F.wait(a, "%s.some(t=>t.includes('harbour fireworks'))" % DRAFTS, 30, 'the kept draft')
    await F.wait(a, "!%s.some(t=>t.includes('bakery'))" % DRAFTS, 20, 'the deleted draft gone on the first device')


@CHROME
def test_a_draft_opens_on_a_fresh_device_and_a_deletion_sticks(ran):
    ran('a_draft_opens_on_a_fresh_device_and_a_deletion_sticks')


@scenario
async def a_fresh_device_the_node_cannot_read_drafts_for_overwrites_nothing(S):
    pk = S.pk(0)
    a = await S.device(0)
    first = 'Draft from the laptop (%d)' % S.base
    await save_draft(a, first)
    await F.until(lambda: S.ctx.server_drafts(pk) == [first], 30, 'the draft in the node\'s document', a)
    spk = S.ctx.spk(pk)
    # The node's relay never answers the storage key's read: the drafts route's own strict read fails -> its 503.
    rule = S.ctx.gate.add(lambda fl, c: 'never' if F.asks(fl, kinds=(30078,), authors=(spk,)) else None)
    healed = False
    try:
        b = await S.device(0)
        await open_drafts(b)
        await F.wait(b, "!!document.querySelector('#feed .drafts-unreachable')", 40,
                     'Drafts said it could not read the account\'s drafts')
        text = await b.b.js("document.querySelector('#feed').innerText")
        assert 'No drafts.' not in text, ('an unanswered read told the person they have no drafts', text)
        second = 'Draft from the phone (%d)' % S.base
        await save_draft(b, second)
        await F.wait(b, "(window.__toastLog||[]).some(t=>/could not be saved to the server/.test(t))", 40,
                     'the refused sync was reported')
        await open_drafts(b)                           # still unreadable: the screen says so, with Try again
        await F.wait(b, "!!document.querySelector('#draft-retry')", 40, 'Drafts saying it could not load')
        S.ctx.gate.drop(rule)
        healed = True
        assert S.ctx.server_drafts(pk) == [first], 'the node\'s drafts document was replaced by an unread device'
        await asyncio.sleep(1)
        assert await b.b.js("!!document.querySelector('#draft-retry')"), 'it retried on its own -- the press would prove nothing'
        # Try again, pressed on the screen that said so: both drafts, and the one written blind reaches the laptop.
        await b.b.js("document.querySelector('#draft-retry').click();true")
        await F.wait(b, "%s.some(t=>t.includes('laptop')) && %s.some(t=>t.includes('phone'))" % (DRAFTS, DRAFTS), 40,
                     'both drafts once the node answered')
        await F.until(lambda: sorted(S.ctx.server_drafts(pk)) == sorted([first, second]), 30,
                      'the draft written while the node could not read reached the node', b)
        await reload(a)
        await open_drafts(a)
        await F.wait(a, "%s.some(t=>t.includes('phone'))" % DRAFTS, 30, 'the phone\'s draft on the laptop')
    finally:
        if not healed:
            S.ctx.gate.drop(rule)


@CHROME
def test_a_fresh_device_the_node_cannot_read_drafts_for_overwrites_nothing(ran):
    ran('a_fresh_device_the_node_cannot_read_drafts_for_overwrites_nothing')


@scenario
async def a_slow_drafts_answer_is_drawn_when_it_lands(S):
    pk = S.pk(0)
    a = await S.device(0)
    text = 'Grocery list for Sunday (%d)' % S.base
    await save_draft(a, text)
    await F.until(lambda: S.ctx.server_drafts(pk) == [text], 30, 'the draft in the node\'s document', a)
    tag = 'ds%d' % S.base
    S.ctx.api.fail('/client/drafts', 4, tag)               # the node answers four seconds late
    try:
        b = await S.device(0, tag=tag)
        await open_drafts(b)
        await F.wait(b, "%s.some(t=>t.includes('Grocery list'))" % DRAFTS, 40, 'the draft, once the slow answer landed')
    finally:
        S.ctx.api.heal('/client/drafts', tag)


@CHROME
def test_a_slow_drafts_answer_is_drawn_when_it_lands(ran):
    ran('a_slow_drafts_answer_is_drawn_when_it_lands')


@scenario
async def bookmarks_made_in_the_same_second_are_all_kept(S):
    """The gate's record of the "one of two bookmarks after a reload" flake (2026-10-28): both kind-10003 versions
    carried the SAME created_at, so NIP-01's tie rule (lower id wins) kept the older list on this device while the
    relay kept the newer. The page clock is frozen inside one second and three posts are bookmarked; after a reload
    all three must be listed."""
    author = await S.device(1)
    notes = [await F.post_note(author, 'Same-second bookmark %d (%d)' % (i, S.base)) for i in range(3)]
    a = await S.device(0)
    # Every page this device opens (opening a post navigates) reads one second off its clock.
    frozen = "(()=>{const t=%d;Date.now=()=>t;})()" % (int(time.time()) * 1000 + 500)
    await a.b.call('Page.addScriptToEvaluateOnNewDocument', {'source': frozen})
    await a.b.js(frozen)
    for i, n in enumerate(notes):
        await press_bookmark(a, n['id'])
        await F.until(lambda: len(bookmarked(S.n, S.pk(0)) or []) == i + 1, 30, 'bookmark %d on the home relay' % (i + 1), a)
    await reload(a)
    await open_bookmarks(a)
    want = " && ".join("%s.includes(%s)" % (LISTED, json.dumps(n['id'])) for n in notes)
    await F.wait(a, want, 40, 'all three bookmarks listed after a reload')


@CHROME
def test_bookmarks_made_in_the_same_second_are_all_kept(ran):
    ran('bookmarks_made_in_the_same_second_are_all_kept')
