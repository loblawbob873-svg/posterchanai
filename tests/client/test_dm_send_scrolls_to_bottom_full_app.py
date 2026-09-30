"""THE MESSAGE YOU JUST SENT IS ON SCREEN.

Reported on PosterChanOS as "I sent message, it does not scroll down anymore". Driven against the
SHIPPED bundle: a conversation with enough history to scroll, opened at the bottom, then a message
typed and sent through the real composer. Whatever happens in between — the composer growing for a
long message and shrinking back, the pane swapping its bubbles when our own copy lands — the pane
must end at the bottom with the sent text in view.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r'''(n=>{
 const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me),now=Math.floor(Date.now()/1000);
 const peer=new Uint8Array(32).fill(7);
 const {createRumor,createSeal}=NostrTools.nip59;
 const out=[];
 for(let i=0;i<n;i++){const t=now-3600+i*30;
  const seal=createSeal(createRumor({kind:14,created_at:t,tags:[['p',mePk]],content:'history line '+i},peer),peer,mePk);
  const eph=NostrTools.generateSecretKey();
  out.push(NostrTools.finalizeEvent({kind:1059,created_at:t,tags:[['p',mePk]],
    content:NostrTools.nip44.encrypt(JSON.stringify(seal),NostrTools.nip44.getConversationKey(eph,mePk))},eph));}
 const r=_rel();for(const e of out)r.push(e);localStorage.setItem('__relayEvents',JSON.stringify(r));
 return NostrTools.getPublicKey(peer);})'''

WHERE = r'''(()=>{const m=document.querySelector('#dm-msgs');if(!m)return null;
 const last=[...m.querySelectorAll('.bubble')].pop();
 const lr=last?last.getBoundingClientRect():null, mr=m.getBoundingClientRect();
 return {overflow:m.scrollHeight-m.clientHeight, height:m.clientHeight, fromBottom:Math.round(m.scrollHeight-m.scrollTop-m.clientHeight),
   lastText:last?last.textContent:'', lastVisible:!!lr&&lr.bottom<=mr.bottom+2&&lr.top>=mr.top-2};})()'''

MESSAGES = {
    'one-line': 'hello there',
    'multi-line': 'line one\nline two\nline three\nline four\nline five\nline six\nline seven',
}


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('kind', list(MESSAGES))
@pytest.mark.parametrize('desk', ['web', 'posterchanos'])
@pytest.mark.parametrize('relay_ms', [60, 1500])
def test_sending_leaves_the_pane_on_your_message(kind, desk, relay_ms):
    text = MESSAGES[kind]

    async def check(b):
        await b.js("localStorage.setItem('__relayMs','" + str(relay_ms) + "')")
        await desktop.login(b)
        pk = await b.js(SEED + '(40)')
        await b.js("__PC.switchView('messages')")
        await b.until("!!document.querySelector('#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]')")
        await b.js("document.querySelector('#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]').click()")
        await b.until("document.querySelectorAll('#dm-msgs .bubble').length>=10 && "
                      "!document.querySelector('#dm-msgs').textContent.includes('decrypting')")
        await asyncio.sleep(4.5)                    # let the open's pin loop finish (4s hard stop)
        in_window = await b.js("document.documentElement.classList.contains('pc-oswin')")
        assert in_window == (desk == 'posterchanos'), ('wrong shell for this case', desk, in_window)
        before = await b.js(WHERE)
        assert before['overflow'] > 200, ('the pane does not scroll, so this proves nothing', before)
        assert before['fromBottom'] <= 2, ('the thread did not open at the bottom', before)
        # Type it the way a person does: into the box, firing 'input' so the composer grows.
        await b.js("(t=>{const ta=document.querySelector('#dm-in');ta.focus();ta.value=t;"
                   "ta.dispatchEvent(new Event('input',{bubbles:true}));})(" + json.dumps(text) + ")")
        await asyncio.sleep(.3)
        # Enter, the way people send.
        await b.js("document.querySelector('#dm-in').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        last = json.dumps(text.split('\n')[-1])
        await b.until("[...document.querySelectorAll('#dm-msgs .bubble')].pop()?.textContent.includes(" + last + ")")
        await asyncio.sleep(1.5 + relay_ms / 1000 * 3)
        after = await b.js(WHERE)
        assert after['fromBottom'] <= 2 and after['lastVisible'], ('your sent message is off screen', kind, desk, after)

    # PosterChanOS: Messages is its OWN native window -- a separate page booted for that view.
    extra = RELAY
    if desk == 'posterchanos':
        extra += "window.pcShell.windowContext={role:'app',view:'messages'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online', '?pcwin=messages' if desk == 'posterchanos' else '', check, extra))
