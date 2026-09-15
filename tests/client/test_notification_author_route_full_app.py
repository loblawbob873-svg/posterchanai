"""Real notification markup routes author clicks separately from post-body clicks."""
import asyncio
import json
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


EXTRA = r'''
window.__authorActions=[];
window.pcPopup.act=async action=>{__authorActions.push(action);return true;};
window.__events=JSON.parse(localStorage.getItem('__authorEvents')||'[]');
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize('target', ['author','body'])
def test_native_notification_author_and_body_have_distinct_click_routes(target):
    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js("""(()=>{
          const mine=new Uint8Array(32).fill(1), other=new Uint8Array(32).fill(2), now=Math.floor(Date.now()/1000);
          const post=NostrTools.finalizeEvent({kind:1,created_at:now-20,content:'Notification target post',tags:[]},mine);
          const reaction=NostrTools.finalizeEvent({kind:7,created_at:now-10,content:'+',tags:[['e',post.id],['p',post.pubkey]]},other);
          const profile=NostrTools.finalizeEvent({kind:0,created_at:now-30,content:JSON.stringify({name:'Fixture Author'}),tags:[]},other);
          __events=[post,reaction,profile];localStorage.setItem('__authorEvents',JSON.stringify(__events));
          document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(mine);document.querySelector('#btn-nsec-login').click();
        })()""")
        await b.until("!!__PC.me() && __PC.notifItems(60).some(e=>e.kind===7)")
        url=await b.js("location.origin+'/index.html?pcpopup=noti'")
        await b.call('Page.navigate',{'url':url})
        await b.until("!!window.__PC && __PC.notifItems(60).some(e=>e.kind===7) && !!document.querySelector('#os-noti-ding')")
        # Rebuild through the shipped sound control after asynchronous relay history arrives.
        await b.js("document.querySelector('#os-noti-ding').click()")
        await b.until("!!document.querySelector('#os-noti .notif[data-open] .name[data-prof]')")
        selector = '#os-noti .notif[data-open] .name[data-prof]' if target=='author' else '#os-noti .notif[data-open]'
        point=await b.js("""(()=>{const node=document.querySelector(%s),r=node.getBoundingClientRect();
          const x=%s,y=r.top+r.height/2;const hit=document.elementFromPoint(x,y);
          return {x,y,visible:r.width>0&&r.height>0,author:!!hit?.closest('.name[data-prof]'),row:!!hit?.closest('.notif[data-open]')};})()""" % (json.dumps(selector),'r.left+r.width/2' if target=='author' else 'r.left+3'))
        assert point['visible'] and point['row'],point
        assert point['author'] is (target=='author'),point
        await b.call('Input.dispatchMouseEvent',{'type':'mousePressed','x':point['x'],'y':point['y'],'button':'left','clickCount':1})
        await b.call('Input.dispatchMouseEvent',{'type':'mouseReleased','x':point['x'],'y':point['y'],'button':'left','clickCount':1})
        await b.until('__authorActions.length>0')
        expected=await b.js("( %s ? 'profile:'+__events[1].pubkey : 'thread:'+__events[0].id)" % ('true' if target=='author' else 'false'))
        assert await b.js('__authorActions') == [expected]
    asyncio.run(desktop.with_browser('online','',check,EXTRA))
