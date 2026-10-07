"""SEARCHING THE EMOJI PICKER IN COMMUNITIES, END TO END.

Reported 2026-10-07: "concord emoji search broken, disappears." Two separate faults, both in the
shared picker (menus.js openEmojiPopover) that Concord's composer ☺ and a message's react button use:

  * THE RESULTS VANISHED. The search box only ever looked at the instance's custom packs: a unicode
    emoji has no name the browser will give us, so typing "smi" emptied the Emoji tab and showed
    custom shortcodes that happened to contain those letters -- never 😄.
  * THE PICKER VANISHED. The reaction picker is ANCHORED, and an anchored picker closed on every
    window `resize`. Tapping its search box on a phone raises the soft keyboard, which in the APK's
    WebView resizes the window -- so it closed the moment you started to search.

Driven with real input: a trusted click into the search box, the keyboard's viewport shrink, typed
keys (Input.dispatchKeyEvent), a message arriving in the room while the picker is open, and a real
mouse press on the result -- which must land in the composer, or be published as the reaction.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_react_full_app import ROOM

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


EXTRA = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
         # An instance with custom packs: that is what puts the search box in the picker at all.
         "localStorage.setItem('pc_emoji_index',JSON.stringify({at:Math.floor(Date.now()/1000),base:'/client/emoji',"
         "emojis:[{s:'smirkcat',p:'Cats',f:'smirkcat.png'},{s:'partyblob',p:'Blobs',f:'partyblob.png'}]}));")

OPEN_REACT = """(()=>{const m=[...document.querySelectorAll('.cc-message')].find(x=>x.textContent.includes('hello from bob'));
  m.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));const t=m.querySelector('[data-cc-actions]');if(t)t.click();
  m.querySelector('[data-cc-react]').click();})()"""

STATE = """(()=>{const pop=document.querySelector('.emoji-pop'),q=pop&&pop.querySelector('.ep-q');
  return {open:!!pop,query:q?q.value:null,focused:!!(q&&document.activeElement===q),
          grid:pop?[...pop.querySelectorAll('.ep-grid [data-e]')].map(e=>e.dataset.e):[]};})()"""


async def press(b, x, y):
    for t in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': t, 'button': 'left', 'clickCount': 1, 'x': x, 'y': y})


async def center(b, selector):
    return await b.js("(()=>{const e=document.querySelector(" + json.dumps(selector) + "),r=e.getBoundingClientRect();"
                      "if(!r.width)throw Error('not on screen');return {x:r.x+r.width/2,y:r.y+r.height/2};})()")


async def type_text(b, text):
    for ch in text:
        key = {'key': ch, 'code': 'Key' + ch.upper(), 'windowsVirtualKeyCode': ord(ch.upper())}
        await b.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'text': ch, 'unmodifiedText': ch, **key})
        await b.call('Input.dispatchKeyEvent', {'type': 'keyUp', **key})
        await asyncio.sleep(.05)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('surface', ['composer', 'reaction'])
@pytest.mark.parametrize('width', [1280, 390])
def test_searching_the_picker_finds_the_emoji_and_keeps_it_open(width, surface):
    got = {}
    phone = width < 600

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 850, 'deviceScaleFactor': 1, 'mobile': phone})
        await desktop.login(b)
        await b.js("window.__publishOK=true")
        await b.js(ROOM)
        await b.until("!!document.querySelector('#cc-input')")
        await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('hello from bob'))")
        if surface == 'reaction':
            await b.js(OPEN_REACT)
        else:
            p = await center(b, '#cc-emoji')
            await press(b, p['x'], p['y'])
        await b.until("!!document.querySelector('.emoji-pop .ep-head:not([hidden]) .ep-q')")
        p = await center(b, '.emoji-pop .ep-q')
        await press(b, p['x'], p['y'])
        if phone:
            # The soft keyboard comes up under the focused search box and the WebView shrinks.
            await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 480, 'deviceScaleFactor': 1, 'mobile': True})
            await asyncio.sleep(.3)
        got['focused'] = await b.js(STATE)
        await type_text(b, 'smi')
        await asyncio.sleep(.3)
        got['typed'] = await b.js(STATE)
        # A message arrives while the picker is open, through the room's own refresh -> fold -> repaint.
        await b.js("""(()=>{const r=window.PosterCordReader,old=r.inspectChat;r.inspectChat=async(...a)=>{const got=await old(...a);
          return {...got,messages:[...got.messages,{id:'msg-late',pubkey:'b'.repeat(64),text:'a late arrival',at:Date.now(),kind:9,tags:[]}]};};})()""")
        await b.js("void PCConcord.refreshActiveChannel(__PC)")
        for _ in range(30):
            if await b.js("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('a late arrival'))"):
                break
            await asyncio.sleep(.2)
        await asyncio.sleep(.3)
        got['repainted'] = await b.js(STATE)
        got['arrived'] = await b.js("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('a late arrival'))")
        if got['repainted']['open'] and '😄' in got['repainted']['grid']:
            p = await center(b, '.emoji-pop .ep-grid [data-e="😄"]')
            await press(b, p['x'], p['y'])
            await asyncio.sleep(1.2)
        got['composer'] = await b.js("document.querySelector('#cc-input').value")
        got['reactions'] = await b.js("__wraps.filter(w=>w.kind===7).map(w=>w.text)")
    asyncio.run(desktop.with_browser('online', '', check, EXTRA))

    assert got['focused']['open'] and got['focused']['focused'], ('the picker closed when its search box was focused', got)
    assert got['typed']['open'], ('the picker disappeared while typing a search', got)
    assert got['typed']['query'] == 'smi', got
    assert '😄' in got['typed']['grid'], ('searching "smi" does not find 😄', got)
    assert ':smirkcat:' in got['typed']['grid'], ('custom emoji dropped out of the search', got)
    assert got['arrived'], ('the late message never reached the room', got)
    assert got['repainted']['open'] and got['repainted']['query'] == 'smi', ('a repaint closed the picker', got)
    if surface == 'composer':
        assert '😄' in got['composer'], ('picking the result did not insert it', got)
    else:
        assert got['reactions'] == ['😄'], ('picking the result did not react with it', got)


def test_every_built_in_emoji_has_a_search_name():
    """A face with no name is one search can never find -- the bug, one emoji at a time."""
    app = (ROOT / 'static/js/client/app.js').read_text()
    menus = (ROOT / 'static/js/client/menus.js').read_text()
    built_in = re.findall(r"'([^']+)'", re.search(r"const REACTION_EMOJIS=\[(.*?)\];", app).group(1))
    table = re.search(r"const _EMOJI_NAMES=\{(.*?)\};", menus)
    assert table, 'menus.js has no names for the built-in emoji'
    named = set(re.findall(r"'([^']+)':'", table.group(1)))
    assert built_in and not [e for e in built_in if e not in named]
