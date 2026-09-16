"""A REPLY WRITTEN IN THE DESKTOP'S COMPOSE WINDOW MUST LEAVE THAT WINDOW BEFORE IT CLOSES.

Reported as "none of my replies to posts are being sent from PosterChanOS desktop ... new posts
posted, no replies". On the desktop a reply opens as its own compositor window (`?pcpopup=compose`,
os.js `renderComposePopup`), and that window closes itself the moment the modal leaves the DOM.
The composer's Send closes the modal FIRST and publishes AFTER (so the page never waits on a slow
relay) — which is harmless in a page that goes on living, and fatal in a window whose renderer is
torn down by that close: the reply was never signed or never reached a socket. New posts looked
fine because the desktop's inline timeline composer is in the page, not a popup.

Nothing tested this seam: the popup tests check its CSS and its geometry, and the composer tests
run in an ordinary page where closing the modal kills nothing. This runs the SHIPPED bundle as the
popup, with `window.close` modelled as what it is in Electron — the end of this renderer, after
which no socket sends anything — and asserts the relay received the reply first.
"""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# window.close() ends the renderer. Everything a socket is asked to send after it is lost.
CLOSE_IS_DEATH = r'''
window.__closed=false;window.__sentBeforeClose=[];
window.__events=JSON.parse(localStorage.getItem('__replyParentEvents')||'[]');
window.__publishOK=true;
window.close=function(){ if(!__closed){ __closed=true; __sentBeforeClose=__published.slice();
  // Nothing written after this point survives either, so record what the drafts were at the end.
  __draftsAtClose=Object.keys(localStorage).filter(k=>k.startsWith('pc_drafts_'))
    .flatMap(k=>JSON.parse(localStorage.getItem(k)||'[]')).filter(d=>d&&!d.del&&(d.text||'').trim()).map(d=>d.text); } };
window.__draftsAtClose=null;
const _fixtureSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){ if(window.__closed) return; return _fixtureSend.call(this,raw); };
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('ack', ['accepted', 'refused'])
def test_reply_popup_publishes_before_its_window_closes(ack):
    text = 'a reply that must reach the relay ' + ack

    async def check(b):
        await desktop.login(b)
        parent = await b.js(r'''(()=>{
          const other=new Uint8Array(32).fill(2);
          const ev=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-60,
                                             content:'the post being answered',tags:[]},other);
          localStorage.setItem('__replyParentEvents',JSON.stringify([ev]));
          return {id:ev.id,pubkey:ev.pubkey};
        })()''')
        arg = json.dumps({'reply': parent['id'], 'replyPk': parent['pubkey']})
        url = await b.js("location.origin+'/index.html?pcpopup=compose&pcarg='+" + json.dumps(quote(arg)))
        await b.call('Page.navigate', {'url': url})
        await b.until("!!window.__PC && !!__PC.me() && !!document.querySelector('#modal-root #cmp-send')")
        if ack == 'refused':
            await b.js('__publishOK=false')
        await b.js("(()=>{const ta=document.querySelector('#cmp');ta.value=" + json.dumps(text)
                   + ";ta.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-send').click()})()")
        # The composer must hand the reply to a socket BEFORE the window goes. Wait long enough
        # for a publish to finish, whichever way the relay answered.
        for _ in range(80):
            if await b.js('__closed'):
                break
            await asyncio.sleep(.1)
        sent = await b.js('(__closed?__sentBeforeClose:__published)')
        # The kind is replyKindFor's decision (NIP-22 1111 for note replies since bc77193bd), not this test's.
        replies = [e for e in sent if e.get('content') == text]
        assert replies, {'closed': await b.js('__closed'), 'published': sent, 'errors': await b.js('__errors')}
        assert any(t[0] == 'e' and t[1] == parent['id'] for t in replies[0]['tags']), replies[0]['tags']
        assert any(t[0] == 'p' and t[1] == parent['pubkey'] for t in replies[0]['tags']), replies[0]['tags']
        if ack == 'accepted':
            # And the window does still close — a reply that sends and leaves an empty rectangle
            # on the desktop is the bug this close was added to fix.
            assert await b.js('__closed'), 'the compose window never closed after a sent reply'
            # A sent reply's draft is dropped BEFORE the window goes, or Drafts keeps a copy of it.
            assert text not in await b.js('__draftsAtClose'), 'the window closed before the sent reply dropped its draft'
        else:
            # A refused reply keeps its draft: that copy is the recovery path.
            if await b.js('__closed'):
                assert text in await b.js('__draftsAtClose')

    asyncio.run(desktop.with_browser('online', '', check, CLOSE_IS_DEATH))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_composer_drawn_straight_away_still_sends_and_closes_its_window():
    """No post to fetch (a new post, or one already in the Store) draws immediately — a path that
    returned before the close watcher was installed, so the window stayed on the desktop for ever."""
    text = 'a post from a composer that did not have to wait'

    async def check(b):
        await desktop.login(b)
        url = await b.js("location.origin+'/index.html?pcpopup=compose&pcarg='+" + json.dumps(quote(json.dumps({'text': 'x'}))))
        await b.call('Page.navigate', {'url': url})
        await b.until("!!window.__PC && !!__PC.me() && !!document.querySelector('#modal-root #cmp-send')")
        await b.js("(()=>{const ta=document.querySelector('#cmp');ta.value=" + json.dumps(text)
                   + ";ta.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-send').click()})()")
        for _ in range(80):
            if await b.js('__closed'):
                break
            await asyncio.sleep(.1)
        assert await b.js('__closed'), 'the compose window never closed'
        assert [e for e in await b.js('__sentBeforeClose') if e.get('content') == text], 'closed before the post was sent'

    asyncio.run(desktop.with_browser('online', '', check, CLOSE_IS_DEATH))
