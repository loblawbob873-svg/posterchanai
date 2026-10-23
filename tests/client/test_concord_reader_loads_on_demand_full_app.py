"""cord-reader.js is not in the page; Concord, and the background path that needs it, load it.

The reader (1.1 MB, the CORD wire protocol) was a <script> tag in client.html: 12% of every cold
load, paid by people who never open Communities. It is fetched on first need now
(concord.js `cordReaderReady`), and these tests press the two things that need it, in a real
Chrome with an EMPTY profile running the real desktop bundle (tests/client/test_desktop_offline_full_app.py):

  * opening Communities on a client that has never loaded the reader draws a real minted community
    and its messages — the door awaits the reader instead of rendering without it;
  * an unread Concord badge on a page where Communities was NEVER opened. The background sweep that
    paints it guarded itself with `if(!reader) return`, which only ever passed because the tag was
    there; without a load of its own it would skip silently forever.

Both also assert the reader was NOT in the page at boot and WAS fetched afterwards, so putting the
tag back fails them as surely as breaking the door does.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM

pytestmark = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
         "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

# What a page that has not needed the reader yet looks like: no global, no script element, and no
# request for the file in this document's resource timeline.
READER_STATE = r'''(async()=>({
  global: typeof window.PosterCordReader!=='undefined',
  element: [...document.scripts].some(s=>/\/cord-reader\.js/.test(s.src)),
  fetched: performance.getEntriesByType('resource').some(e=>/\/cord-reader\.js/.test(e.name)),
}))()'''


def tag_in_shell():
    """The bundle's rendered shell (templates/client.html, as desktop/build-www.sh renders it)."""
    return bool(re.search(r'<script[^>]*cord-reader\.js', (desktop.BUNDLE_ROOT / 'index.html').read_text(encoding='utf-8')))

# Two chat messages written by the real reader into the minted room's general channel.
WRITE = r'''(async(texts)=>{
  const controls=__relayStore.filter(e=>e.kind===1059&&PosterCordReader.inspectControl(__cordBundle,[]).controlPubkeys.includes(e.pubkey));
  const general=PosterCordReader.inspectControl(__cordBundle,controls).channels.find(c=>c.name==='general');
  const out=[];
  for(const text of texts){const made=await PosterCordReader.createChatWrap(__cordBundle,controls,general.id,text,__PC.me().pubkey,__PC.signTemplate);out.push(made.wrap);}
  return out;
})'''


async def wait_for(b, expression, seconds):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return
        await asyncio.sleep(.1)
    raise AssertionError({'waiting': expression,
                          'state': await b.js("({errors:__errors,body:document.body.innerText.slice(-1500)})")})


async def signed_in(b):
    await b.until("document.body.classList.contains('guest') || !!(window.__PC&&__PC.me())")
    if not await b.js("!!(window.__PC&&__PC.me())"):
        await desktop.login(b)


def test_a_cold_client_opens_concord_and_shows_a_communitys_messages():
    minted = {}

    async def mint(b):
        await signed_in(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        got = await b.js(MINT.replace('NAME', json.dumps('Cold room')))
        wraps = await b.js(WRITE + '(' + json.dumps(['hello from before the reader', 'and a second line']) + ')')
        minted.update(room=got['room'], events=got['events'] + wraps)

    asyncio.run(desktop.with_browser('online', '', mint, NO_OS))
    assert minted.get('room'), 'the fixture community was not minted'

    async def cold(b):
        await signed_in(b)
        boot = await b.js(READER_STATE)
        assert not tag_in_shell(), 'client.html has a <script> tag for cord-reader.js again'
        assert boot == {'global': False, 'element': False, 'fetched': False}, (
            'cord-reader.js is part of the boot payload again', boot)
        await b.js(INSTALL)
        await b.js('__relaySeed(' + json.dumps(minted['events']) + ')')
        # The saved community, exactly as a returning user's device holds it -- and Communities opened
        # the way a person opens it, not by calling render().
        await b.js("((room)=>{localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));"
                   "localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');})(" + json.dumps(minted['room']) + ")")
        # THE DOOR'S CONTRACT: nothing of Communities is drawn before the reader exists. Without it the
        # first paint is a room whose channels and messages could not be read -- an empty channel
        # claiming to be empty -- which the next repaint may or may not correct.
        await b.js("(()=>{window.__drawnWithoutReader=0;new MutationObserver(()=>{if(typeof window.PosterCordReader==='undefined'"
                   "&&document.querySelector('.cc-messages,#cc-input'))window.__drawnWithoutReader++;})"
                   ".observe(document.body,{childList:true,subtree:true});})(); 1")
        await b.js("__PC.switchMessagesTab('concord')")
        # Well inside the background sweep's first 20 s tick -- which would load the reader too, and
        # would otherwise let a Communities screen drawn WITHOUT the door repair itself and pass here.
        await wait_for(b, "!!document.querySelector('.cc-messages') && document.querySelector('.cc-messages').innerText.includes('hello from before the reader')"
                          " && document.querySelector('.cc-messages').innerText.includes('and a second line')", 10)
        after = await b.js(READER_STATE)
        assert after['global'] and after['fetched'], (
            'the community drew without the reader being fetched on demand', after)
        assert await b.js('__drawnWithoutReader') == 0, 'Communities drew a room before the reader had loaded'
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', cold, NO_OS))


def test_the_communities_badge_lights_up_on_a_page_where_concord_was_never_opened():
    async def check(b):
        await signed_in(b)
        # A device that has used the community before: it is saved and its keys are cached.
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('Badge room')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('#cc-input')")
        await asyncio.sleep(2.5)    # what arrives next is newer than the read mark opening the room set
        wraps = await b.js(WRITE + '(["news while the app was closed"])')
        await b.js("sessionStorage.setItem('__fixtureStore',JSON.stringify(__relayStore));"
                   "sessionStorage.setItem('__fixtureNew'," + json.dumps(json.dumps(wraps)) + ")")
        # A cold start of the app that does NOT open Communities.
        await b.js("__PC.switchView('home'); 1")
        await b.call('Page.reload', {'ignoreCache': True})
        await b.until('!!window.__PC && !!window.PCConcord')
        await signed_in(b)
        boot = await b.js(READER_STATE)
        assert not tag_in_shell(), 'client.html has a <script> tag for cord-reader.js again'
        assert boot == {'global': False, 'element': False, 'fetched': False}, (
            'cord-reader.js is part of the boot payload again', boot)
        assert await b.js("document.querySelector('#cc-badge').classList.contains('hidden')"), 'badge lit before anything arrived'
        await b.js(INSTALL)
        await b.js("__relaySeed(JSON.parse(sessionStorage.getItem('__fixtureStore')));"
                   "__relaySeed(JSON.parse(sessionStorage.getItem('__fixtureNew'))); 1")
        # The sweep runs every 20 s; give it two turns.
        await wait_for(b, "!document.querySelector('#cc-badge').classList.contains('hidden')", 50)
        assert not await b.js("document.body.classList.contains('concord-view')"), 'Communities was opened -- not the background path'
        after = await b.js(READER_STATE)
        assert after['global'] and after['fetched'], ('the badge lit without the reader -- what loaded it?', after)

    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
