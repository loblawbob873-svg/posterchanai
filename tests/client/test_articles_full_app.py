"""Articles (NIP-23 long-form) list, resume drafts, count comments, open, and explain an empty state
(2026-10-22: every screen gets a browser test).

Fed through the real Relay.query seam: two versions of ONE article (an edit), a second article, one of my drafts and
two comments on the first article. A person must see each article ONCE at its newest title, the draft with a working
Resume, the comment count, a card that opens the reader, a Write button that opens the editor -- and with nothing
published, a sentence saying how to publish the first one. Real login, DOM, discover.js and the article card.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FEED = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''

SETUP = r'''(()=>{
  const me=__PC.me().pubkey, now=Math.floor(Date.now()/1000), A='a'.repeat(64), B='b'.repeat(64);
  const art=(id,pk,d,title,at)=>({id,pubkey:pk,kind:30023,created_at:at,content:'# '+title+'\n\nbody',sig:'s',tags:[['d',d],['title',title],['published_at',String(at)]]});
  window.__art={mode:'data'};
  const q=Relay.query.bind(Relay);
  Relay.query=async(filters,...rest)=>{
    const f=filters[0]||{}, ks=f.kinds||[];
    const out=[];
    if(__art.mode==='data'){
      if(ks.includes(30023)) out.push(art('1'.repeat(64),A,'guide','Old title',now-7200), art('2'.repeat(64),A,'guide','How relays work',now-3600),
                                      art('3'.repeat(64),B,'notes','Second article',now-1800));
      else if(ks.includes(30024)) out.push({id:'4'.repeat(64),pubkey:me,kind:30024,created_at:now-600,content:'wip',sig:'s',tags:[['d','wip-draft'],['title','My unfinished essay']]});
      else if(filters.some(x=>x['#A']||x['#a'])) out.push(...['5','6'].map(c=>({id:c.repeat(64),pubkey:B,kind:1111,created_at:now-60,content:'great',sig:'s',
                                      tags:[['A','30023:'+A+':guide'],['K','30023'],['P',A]]})));
      else return q(filters,...rest);
    } else if(!ks.includes(30023)&&!ks.includes(30024)) return q(filters,...rest);
    Object.defineProperty(out,'complete',{value:true}); return out;
  };
  __PC.switchView('articles'); return 1;
})()'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_articles_list_once_resume_drafts_count_comments_and_open():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP)
        await b.until("document.querySelectorAll('#art-list .article-card').length>0")
        titles = await b.js("[...document.querySelectorAll('#art-list .art-title')].map(t=>t.textContent)")
        assert sorted(titles) == ["How relays work", "Second article"], titles   # the edit shows ONCE, newest title
        await b.until("/💬 2/.test(document.querySelector('#art-list').innerText)")   # comments counted
        assert await b.js("/My unfinished essay/.test(document.querySelector('#art-drafts').innerText)")
        await b.js("document.querySelector('#art-drafts .da-edit').click(); 1")
        await b.until("document.querySelector('#ae-title') && document.querySelector('#ae-title').value==='My unfinished essay'")
        await b.js("document.querySelector('#ae-back').click(); __PC.switchView('articles'); 1")
        await b.until("document.querySelectorAll('#art-list .article-card').length>0")
        await b.js("[...document.querySelectorAll('#art-list .article-card')].find(c=>/How relays work/.test(c.innerText)).click(); 1")
        await b.until("/body/.test(document.querySelector('#feed').innerText) && !document.querySelector('#art-list')")
        await b.js("__PC.switchView('articles'); 1")
        await b.until("!!document.querySelector('#art-new')")
        await b.js("document.querySelector('#art-new').click(); 1")
        await b.until("!!document.querySelector('#ae-title') && document.querySelector('#ae-title').value===''")
    asyncio.run(desktop.with_browser('online', '', check, FEED))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_with_no_articles_it_says_how_to_publish_one():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP.replace("mode:'data'", "mode:'none'"))
        await b.until("/No articles yet/.test(document.querySelector('#feed').innerText)")
    asyncio.run(desktop.with_browser('online', '', check, FEED))
