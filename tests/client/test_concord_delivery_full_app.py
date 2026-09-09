"""Real bundled DOM, login, signer worker, Relay, and IndexedDB retry flow.

HTTP/WS boundaries and CORD wrapping are fixtures. No public messages are sent.
"""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module',autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize("width",[1280,390])
def test_concord_rejected_room_send_is_visible_and_retry_does_not_sign_again(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride",{"width":width,"height":850,"deviceScaleFactor":1,"mobile":width<600})
        await desktop.login(b)
        await b.js(r'''(()=>{
          const room={name:'Delivery fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
            channels:[{id:'fixture-general',name:'general'}],cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}]}),
          inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]}),
          createChatWrap:async(_bundle,_wraps,_channel,text,owner,sign,tags,kind)=>{
            let sealed;try{sealed=await sign({kind:20013,created_at:Math.floor(Date.now()/1000),content:'encrypted-fixture',tags:[]});}catch(e){window.__concordSignError=String(e);throw e;}
            return{rumorId:sealed.id,wrap:{...sealed,kind:1059},ms:Date.now()};
          }};
          __PC.switchMessagesTab('concord');
        })()''')
        await b.until("!!document.querySelector('#cc-input')")
        # ON MOBILE THE CONVERSATION IS A SEPARATE PANE, so open the room the way a tap does.
        # `.cc-conversation` is `display:none` under max-width:820px until `.cc-app` carries
        # `show-chat`; without that the message renders into a pane with no box at all and every
        # geometry assertion below measures zero — which says nothing about the check mark.
        await b.js("""(()=>{const app=document.querySelector('.cc-app');
          if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}
          return true;})()""")
        await b.until("getComputedStyle(document.querySelector('.cc-conversation')).display!=='none'")
        await b.js("document.querySelector('#cc-input').value='Visible failed room send';document.querySelector('#cc-input').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cc-send').click()")
        await b.until("!!document.querySelector('[data-cc-retry-delivery]') || !!window.__concordSignError")
        assert not await b.js("window.__concordSignError||null")
        assert await b.js("[...document.querySelectorAll('.cc-delivery-status')].some(x=>x.innerText==='Not sent')")
        assert not await b.js("!!document.querySelector('.cc-delivery-confirmed')"),'failure must not show confirmation'
        assert await b.js("document.querySelector('#cc-input').value===''"),'failed signed send restored blind-new-send draft'
        assert await b.js('__concordSigns')==1
        first=await b.js("JSON.stringify(__published.find(e=>e.kind===1059))")
        assert first and first!='null'
        await b.js("__publishOK=true;document.querySelector('[data-cc-retry-delivery]').click()")
        await b.until("!!document.querySelector('.cc-delivery-confirmed')")
        assert await b.js("""(()=>{const status=document.querySelector('.cc-delivery-confirmed');
          if(!status||status.getAttribute('role')!=='status'||status.getAttribute('aria-label')!=='Sent'||status.title!=='Sent')return false;
          const rect=status.getBoundingClientRect(),body=status.closest('.cc-message-body').getBoundingClientRect();
          return !status.textContent.trim()&&status.querySelectorAll('use[href="#i-check"]').length===1&&
            !!document.querySelector('symbol#i-check')&&rect.width>5&&rect.width<=16&&rect.height<=16&&
            rect.right<=body.right+1&&status.scrollWidth<=status.clientWidth+1;
        })()"""),'confirmed delivery must show one visible, accessible check without wrapping or a Sent text label'
        assert await b.js('__concordSigns')==1,'retry invoked signer again'
        assert await b.js("JSON.stringify(__published.filter(e=>e.kind===1059).at(-1))")==first
        assert await b.js("![...Object.values(localStorage)].some(x=>String(x).includes('Visible failed room send'))")
    extra=r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__concordSigns=0;
const originalWorkerPost=Worker.prototype.postMessage;
Worker.prototype.postMessage=function(message,...rest){if(message?.op==='sign'&&message.args?.event?.kind===20013)__concordSigns++;return originalWorkerPost.call(this,message,...rest);};
'''
    asyncio.run(desktop.with_browser('online','',check,extra))
