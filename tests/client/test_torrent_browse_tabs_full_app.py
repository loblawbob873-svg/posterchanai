"""Torrents → Nyaa and TGX: browse, search, and Download straight into this node's client.

Asked for as: a Nyaa tab showing nyaa.si's newest torrents with a search, and a TGX tab showing what
the AI chat's `torrents` command shows (its categories) with a search -- each Download adding the
torrent to the Downloads tab. Drives the SHIPPED bundle; only the node's /api/torrent answers are
fixtures, and every request the page makes is recorded so the test checks what was ASKED, not only
what was drawn:

  * Nyaa opens on the newest uploads (a query-less /nyaa), a search asks for exactly that query;
  * TGX opens on Movies (/catalog), a category chip and a search each ask for their own list;
  * Download POSTs the row's magnet to /add, the row says it was added, and the view stays put;
  * a slow earlier search cannot land on top of a later one;
  * leaving for Downloads and coming back repaints the same results without asking again;
  * a refusal from the node is shown as its words, not as an empty list;
  * at a phone width nothing in the new rows overflows the screen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
window.__torReq=[];
(()=>{const inner=window.fetch;
 const row=(t,i)=>({num:i+1,title:t,magnet:'magnet:?xt=urn:btih:'+(i+1).toString(16).padStart(40,'0')+'&dn='+encodeURIComponent(t),
   size:(i+1)+'.2 GiB',seeders:40-i,leechers:i,url:''});
 const ok=j=>new Response(JSON.stringify(j),{status:200,headers:{'Content-Type':'application/json'}});
 window.fetch=async function(url,opts={}){
  const u=String(url);const at=u.indexOf('/api/torrent/');
  if(at<0) return inner(url,opts);
  const p=new URL(u.slice(at),'http://x');const path=p.pathname.replace('/api/torrent','');
  __torReq.push({path,q:p.searchParams.get('q'),cat:p.searchParams.get('category'),method:(opts.method||'GET'),body:opts.body||''});
  if(path==='/list') return ok({torrents:[]});
  if(path==='/add') return ok({ok:true,info_hash:'ab'.repeat(20)});
  if(path==='/nyaa'){
    const q=p.searchParams.get('q')||'';
    if(q==='slow'){await new Promise(r=>setTimeout(r,900));return ok({query:q,items:[row('SLOW RESULT',0)]});}
    return ok({query:q,items:(q?['Q:'+q+' one','Q:'+q+' two']:['[Sub] Newest Show - 01 [1080p]','[Sub] Another Show - 12 [720p] '+'x'.repeat(160)]).map(row)});
  }
  if(path==='/catalog') return ok({category:p.searchParams.get('category'),items:['CAT:'+p.searchParams.get('category')+' A','CAT:'+p.searchParams.get('category')+' B'].map(row)});
  if(path==='/search'){
    const q=p.searchParams.get('q');
    if(q==='broken') return new Response(JSON.stringify({detail:'Torrent search requires HTTP proxy to Tor.'}),{status:400,headers:{'Content-Type':'application/json'}});
    return ok({query:q,items:['S:'+q].map(row)});
  }
  return ok({});
 };})();
'''

ITEMS = "[...document.querySelectorAll('#tb-list .tb-item .tb-name')].map(e=>e.textContent)"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_nyaa_and_tgx_tabs_browse_search_and_download():
    got = {}

    async def tab(b, name):
        await b.js(f"document.querySelector('.tor-tabs .ntab[data-tt={name}]').click();true")
        await b.until("!!document.querySelector('#tb-list .tb-item') || !!document.querySelector('#tb-list .empty')")

    async def search(b, q):
        await b.js("(()=>{const i=document.getElementById('tb-q');i.value=%r;"
                   "document.getElementById('tb-form').requestSubmit();})();true" % q)

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('torrents');true")
        await b.until("!!document.querySelector('.tor-tabs .ntab[data-tt=nyaa]')")

        # --- Nyaa: newest first, then a search
        await tab(b, 'nyaa')
        got['nyaa_latest'] = await b.js(ITEMS)
        got['nyaa_head'] = await b.js("document.getElementById('tb-head').textContent")
        got['nyaa_first_req'] = await b.js("__torReq.filter(r=>r.path==='/nyaa')")
        await search(b, 'frieren')
        await b.until("[...document.querySelectorAll('#tb-list .tb-name')].some(e=>e.textContent.includes('frieren'))")
        got['nyaa_search'] = await b.js(ITEMS)

        # --- Download adds to the client and stays on the tab
        await b.js("document.querySelector('#tb-list .tb-item .tb-get').click();true")
        await b.until("document.querySelector('#tb-list .tb-item .tb-get').textContent.includes('Added')")
        got['add'] = await b.js("__torReq.filter(r=>r.path==='/add').map(r=>({m:r.method,b:JSON.parse(r.body)}))")
        got['still_nyaa'] = await b.js("document.querySelector('.tor-tabs .ntab.on').dataset.tt")

        # --- A slow earlier search never overwrites a later one
        await search(b, 'slow')
        await asyncio.sleep(.1)
        await search(b, 'fast')
        await b.until("[...document.querySelectorAll('#tb-list .tb-name')].some(e=>e.textContent.includes('fast'))")
        await asyncio.sleep(1.3)
        got['after_race'] = await b.js(ITEMS)

        # --- Away to Downloads and back: same list, no new request
        before = await b.js("__torReq.filter(r=>r.path==='/nyaa').length")
        await b.js("document.querySelector('.tor-tabs .ntab[data-tt=dl]').click();true")
        await b.until("!!document.getElementById('tm-list')")
        await tab(b, 'nyaa')
        got['back_again'] = await b.js(ITEMS)
        got['back_query'] = await b.js("document.getElementById('tb-q').value")
        got['nyaa_requests_added'] = (await b.js("__torReq.filter(r=>r.path==='/nyaa').length")) - before

        # --- TGX: Movies, a category chip, a search, and a refusal
        await tab(b, 'tgx')
        got['tgx_default'] = await b.js(ITEMS)
        await b.js("document.querySelector('.tb-cat[data-cat=tv]').click();true")
        await b.until("[...document.querySelectorAll('#tb-list .tb-name')].some(e=>e.textContent.startsWith('CAT:tv'))")
        got['tgx_tv'] = await b.js(ITEMS)
        await search(b, 'ubuntu')
        await b.until("[...document.querySelectorAll('#tb-list .tb-name')].some(e=>e.textContent==='S:ubuntu')")
        got['tgx_chip_off'] = await b.js("[...document.querySelectorAll('.tb-cat.btn-neon')].length")
        await search(b, 'broken')
        await b.until("!!document.querySelector('#tb-list .empty')")
        got['tgx_error'] = await b.js("document.querySelector('#tb-list .empty').textContent")
        got['tgx_reqs'] = await b.js("__torReq.filter(r=>r.path==='/catalog'||r.path==='/search').map(r=>[r.path,r.cat,r.q])")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))

    assert got['nyaa_first_req'][0]['q'] in (None, ''), got['nyaa_first_req']
    assert got['nyaa_latest'][0].startswith('[Sub] Newest Show'), got
    assert 'Newest' in got['nyaa_head'], got['nyaa_head']
    assert got['nyaa_search'] == ['Q:frieren one', 'Q:frieren two'], got['nyaa_search']

    assert got['add'] and got['add'][0]['m'] == 'POST', got['add']
    assert got['add'][0]['b'] == {'magnet': 'magnet:?xt=urn:btih:' + '1'.zfill(40) + '&dn=' + 'Q%3Afrieren%20one'}, got['add']
    assert got['still_nyaa'] == 'nyaa', 'Download threw the user off the list'

    assert got['after_race'] == ['Q:fast one', 'Q:fast two'], 'a slow earlier search overwrote a later one: %r' % got['after_race']

    assert got['back_again'] == ['Q:fast one', 'Q:fast two'] and got['back_query'] == 'fast', got
    assert got['nyaa_requests_added'] == 0, 'coming back to the tab searched again'

    assert got['tgx_default'] == ['CAT:movies A', 'CAT:movies B'], got['tgx_default']
    assert got['tgx_tv'] == ['CAT:tv A', 'CAT:tv B'], got['tgx_tv']
    assert got['tgx_chip_off'] == 0, 'a search still shows a category as selected'
    assert 'proxy' in got['tgx_error'], got['tgx_error']
    assert ['/catalog', 'movies', None] in got['tgx_reqs'] and ['/catalog', 'tv', None] in got['tgx_reqs']
    assert ['/search', None, 'ubuntu'] in got['tgx_reqs'], got['tgx_reqs']


PHONE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_browse_tabs_fit_a_phone():
    """The phone layout (no desktop windows): the search row, the rows and their buttons stay on
    screen, a long release name wraps instead of pushing the row wide, and the tab strip wraps."""
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('torrents');true")
        await b.until("!!document.querySelector('.tor-tabs .ntab[data-tt=nyaa]')")
        for name in ('nyaa', 'tgx'):
            await b.js(f"document.querySelector('.tor-tabs .ntab[data-tt={name}]').click();true")
            await b.until("!!document.querySelector('#tb-list .tb-item')")
            await asyncio.sleep(.3)
            got[name] = await b.js(r'''(()=>{const vw=document.documentElement.clientWidth;
              const over=[...document.querySelectorAll('.tb-bar, .tb-bar *, .tb-cats *, .tb-item, .tb-item *, .tor-tabs .ntab')]
                .filter(e=>e.getClientRects().length && e.getBoundingClientRect().right>vw+1).map(e=>e.className||e.tagName);
              const inWindow=!!document.querySelector('.osw #tb-list');
              return {vw, inWindow, over, pageScroll: document.documentElement.scrollWidth>vw+1}})()''')

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + PHONE))
    for name in ('nyaa', 'tgx'):
        assert not got[name]['inWindow'], 'the phone run is still in desktop mode: %r' % got
        assert not got[name]['over'] and not got[name]['pageScroll'], got
