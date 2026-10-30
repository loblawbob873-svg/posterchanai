"""THE MAGNIFIER ON A PROFILE FINDS EVERY POST OF THEIRS THAT HAS THE WORDS.

"On X if you go to someone's profile and hit the magnifying glass and search key words. All posts with that word
shows up" (2026-10-30), on mobile, desktop and web. Real mouse clicks and typed keys against the shipped bundle:
three of six posts say "banana", two of them only the relay holds (the device's cache knows the newest one). Run
against a relay that does NIP-50 search AND one that ignores `search` and answers with everything the author wrote --
the second must not put a single non-matching post on screen. Phone width: the magnifier must be on screen (the tab
row scrolls sideways there) and the box must fit.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
           "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

SETUP = r'''(async(nip50)=>{const me=__PC.me(); window.__all=[];
  const texts=['I like a Banana split','nothing here','second banana of the day','plain post','old BANANA bread','apples only'];
  for(let i=0;i<texts.length;i++){ __all.push(await __PC.signTemplate({kind:1,pubkey:me.pubkey,
    created_at:Math.floor(Date.now()/1000)-i*86400*30,tags:[],content:texts[i]})); }
  Store.saveEvent(__all[0]); Store.saveEvent(__all[1]);           // the device knows the two newest
  const q=Relay.query.bind(Relay); window.__searches=[];
  Relay.query=async(f,...r)=>{
    const fl=Array.isArray(f)?f:[f];
    if(fl.some(x=>x.search && (x.authors||[]).includes(me.pubkey))){
      __searches.push(fl[0].search);
      const words=fl[0].search.toLowerCase().match(/\w+/g)||[];
      const a = nip50 ? __all.filter(e=>words.every(w=>e.content.toLowerCase().includes(w))) : __all.slice();
      a.complete=true; return a;
    }
    if(fl.some(x=>(x.authors||[]).includes(me.pubkey) && (x.kinds||[]).includes(1))){ const a=[__all[0],__all[1]]; a.complete=true; return a; }
    return q(f,...r);
  };
  return me.pubkey})'''

SHOWN = r'''[...document.querySelectorAll('#prof-list article, #prof-list .note')].map(n=>n.textContent.replace(/\s+/g,' ').trim())'''


async def _click(b, selector):
    """A real click at the point where the browser itself says `selector` is on top."""
    doc = await b.call('DOM.getDocument', {'depth': 0})
    node = await b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': selector})
    assert node.get('nodeId'), selector
    q = (await b.call('DOM.getContentQuads', {'nodeId': node['nodeId']}))['quads'][0]
    x, y = (q[0] + q[4]) / 2, (q[1] + q[5]) / 2
    hit = await b.call('DOM.getNodeForLocation', {'x': int(x), 'y': int(y), 'includeUserAgentShadowDOM': False})
    obj = await b.call('DOM.resolveNode', {'backendNodeId': hit['backendNodeId']})
    on_top = await b.call('Runtime.callFunctionOn', {'objectId': obj['object']['objectId'], 'returnByValue': True,
        'functionDeclaration': 'function(s){const e=this.nodeType===1?this:this.parentElement;return !!(e&&e.closest(s));}',
        'arguments': [{'value': selector}]})
    assert on_top['result'].get('value'), (selector + ' is covered or off-screen at its centre', x, y)
    await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})


async def _key(b, key, code, vk):
    for kind in ('rawKeyDown', 'keyUp'):
        await b.call('Input.dispatchKeyEvent', {'type': kind, 'key': key, 'code': code, 'windowsVirtualKeyCode': vk})


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('nip50', [True, False])
@pytest.mark.parametrize('width', [1280, 390])
def test_the_magnifier_finds_every_post_with_the_words(nip50, width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 860, 'deviceScaleFactor': 1,
                                                            'mobile': width < 600})
        await desktop.login(b)
        pk = await b.js(SETUP + '(' + json.dumps(nip50) + ')')
        await b.js(f"__PC.openProfile('{pk}')")
        await b.until("!!document.querySelector('.prof-tabs [data-tab=search]')")
        await asyncio.sleep(1)
        await b.js("document.querySelector('.prof-tabs').scrollIntoView({block:'center'});true")
        await asyncio.sleep(.4)
        await _click(b, ".prof-tabs [data-tab=search]")
        await b.until("document.activeElement && document.activeElement.id==='prof-find-q'")
        got['fits'] = await b.js("(()=>{const r=document.querySelector('#prof-find').getBoundingClientRect();"
                                 "return r.left>=0 && r.right<=innerWidth+1 && r.height>20})()")
        await b.call('Input.insertText', {'text': 'banana'})
        await _key(b, 'Enter', 'Enter', 13)
        await b.until("/posts? with/.test((document.querySelector('.prof-find-sum')||{}).textContent||'')")
        await asyncio.sleep(.5)
        got['shown'] = await b.js(SHOWN)
        got['sum'] = await b.js("document.querySelector('.prof-find-sum').textContent")
        got['searches'] = await b.js("__searches")
        await _click(b, "#prof-find-x")
        await asyncio.sleep(.5)
        got['after'] = await b.js("({tab:(document.querySelector('.prof-tab.active')||{}).dataset.tab,"
                                  "hidden:document.querySelector('#prof-find').hidden})")
        got['overflow'] = await b.js("document.documentElement.scrollWidth<=innerWidth+1")

    asyncio.run(desktop.with_browser('online', '', check, CLASSIC))
    assert got['fits'], ('the search box does not fit the screen', got)
    assert 'banana' in got['searches'], ('the relay was never asked', got)
    assert len(got['shown']) == 3 and all('banana' in t.lower() for t in got['shown']), \
        ('the results are not exactly the three posts with the word', got['shown'])
    assert got['sum'].startswith('3 posts with'), got['sum']
    assert got['after'] == {'tab': 'notes', 'hidden': True}, ('closing the search does not go back to Notes', got)
    assert got['overflow'], 'the page scrolls sideways'
