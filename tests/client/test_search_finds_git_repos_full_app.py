"""Search finds git repositories — by name, by identifier, by hashtag — wherever they are announced.

Reported 2026-10-21: "posterchan search should search git repos too". It had a Git Repos section, fed by
ONE read shared with articles, streams and torrents (`limit:240` across four kinds, pool relays only), so on
a real relay almost no announcement was ever in the set being filtered, and repos announced where ngit,
gitworkshop and Armada announce (the ngit index) were never asked for at all.

The SHIPPED bundle against a fixture relay that holds real signed announcements:
  * repos get their OWN read (no other kind in it, a limit sized for repositories) and the ngit index is asked;
  * a repo is found by name, by identifier and by `t` hashtag, exact name first;
  * a forged announcement (bad signature — anyone can answer a REQ on the index) is not shown;
  * nothing scrolls sideways, at phone and desktop width.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


RECORD = r'''
window.__reqs=[];
const _send=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){ try{ const m=JSON.parse(raw); if(Array.isArray(m)&&m[0]==='REQ')__reqs.push({url:String(this.url||''),filters:m.slice(2)}); }catch(_){} return _send.call(this,raw); };
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_search_finds_git_repos(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=860, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(r'''(()=>{
          const NT=NostrTools,k=n=>new Uint8Array(32).fill(n),now=Math.floor(Date.now()/1000);
          const ann=(sk,d,name,extra=[],at=now)=>NT.finalizeEvent({kind:30617,created_at:at,content:'',tags:[['d',d],['name',name],['description','a '+name+' repo'],...extra]},k(sk));
          const exact=ann(5,'quill','quill',[],now-500),prefix=ann(6,'quill-tools','Quill Tools',[],now-10),tagged=ann(7,'inkpot','inkpot',[['t','quill']],now-5);
          const forged={...ann(8,'quill-evil','quill evil'),sig:'0'.repeat(128)};
          window.__events=[...(window.__events||[]),exact,prefix,tagged,forged];
        })()''')
        await b.js("__PC.runSearch('quill')")
        await b.until("[...document.querySelectorAll('.search-section-title')].some(x=>x.textContent.includes('Git Repos'))")
        names = await b.js("[...document.querySelectorAll('#feed .repo-grid .repo-card, #feed .repo-grid > *')].map(c=>c.textContent.replace(/\\s+/g,' ').trim())")
        joined = ' | '.join(names)
        names = [n.lstrip('🌱 ').strip() for n in names]
        assert names and names[0].lower().startswith('quill') and 'tools' not in names[0].lower(), ('the exact name is not first', names)
        assert 'Quill Tools' in joined, ('a name that starts with the query is not found', names)
        assert 'inkpot' in joined, ('a repo tagged #quill is not found', names)
        assert 'evil' not in joined, ('a forged announcement is shown', names)
        reqs = await b.js("__reqs")
        repo_reads = [r for r in reqs if any(f.get('kinds') == [30617] for f in r['filters'])]
        assert any(f.get('limit', 0) >= 1000 for r in repo_reads for f in r['filters']), ('repositories have no read of their own', reqs[-6:])
        assert any('index.ngit.dev' in r['url'] for r in repo_reads), ('the ngit index is never asked', [r['url'] for r in repo_reads])
        assert not any(30617 in (f.get('kinds') or []) and len(f.get('kinds')) > 1 for r in reqs for f in r['filters']), 'repos still share a read with other kinds'
        over = await b.js("document.scrollingElement.scrollWidth-innerWidth")
        assert over <= 1, f'search results scroll sideways by {over}px'

    asyncio.run(desktop.with_browser('online', '', check, RECORD))
