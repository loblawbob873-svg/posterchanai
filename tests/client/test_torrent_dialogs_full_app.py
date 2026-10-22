"""Torrents: adding a magnet, choosing which files to download, and managing RSS feeds all work through the real
dialogs (2026-10-22: every button gets a test -- 9 of torrents.js's 10 buttons were named by none).

Only the node's /api/torrent answers are fixtures, and every request the page makes is recorded, so each step checks
what was ASKED of the server, not only what was drawn:
  * Add: the magnet is POSTed to /add as `magnet`, and "choose files" opens the file chooser for the new torrent;
  * Files: Select all / Select none / Check again / Save send exactly the ticked files to /files, Close closes;
  * Feeds: Subscribe POSTs the URL and filters, Check now and Filters→Save hit that feed, Remove asks first and
    DELETEs, Cancel leaves the edit without saving.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

H = "ab" * 20
FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__torReq=[];
(()=>{const inner=window.fetch;
 const ok=j=>new Response(JSON.stringify(j),{status:200,headers:{'Content-Type':'application/json'}});
 window.__feeds=[{id:'f1',url:'https://feeds.example/show.rss',title:'My show',enabled:true,primed:true,last_check:Math.floor(Date.now()/1000)-60,added_total:3,include:'1080p',exclude:''}];
 window.fetch=async function(url,opts={}){
  const u=String(url);const at=u.indexOf('/api/torrent/');
  if(at<0) return inner(url,opts);
  const p=new URL(u.slice(at),'http://x');const path=p.pathname.replace('/api/torrent','');const method=(opts.method||'GET');
  __torReq.push({path,method,body:opts.body||''});
  if(path==='/list') return ok({torrents:[{num:1,info_hash:'HASH',name:'Show S01',state:'downloading',progress:0.2,size:3000000000},
                                          {num:2,info_hash:'cd'.repeat(20),name:'Fresh magnet',state:'metadata',progress:0,size:0}]});
  if(path==='/files/2') return ok({files:[]});
  if(path==='/files/1') return ok({files:[{index:0,path:'Show/e01.mkv',size:1e9,wanted:true,progress:0.5},
                                          {index:1,path:'Show/e02.mkv',size:1e9,wanted:true,progress:0},
                                          {index:2,path:'Show/extras.zip',size:1e9,wanted:true,progress:0}]});
  if(path==='/files' && method==='POST') return ok({ok:true});
  if(path==='/add') return ok({ok:true,info_hash:'HASH'});
  if(path==='/feeds' && method==='GET') return ok({feeds:__feeds,enabled:true});
  if(path==='/feeds' && method==='POST') return ok({ok:true,id:'f2'});
  if(path==='/feeds/f1/check') return ok({ok:true,added:1});
  if(path.startsWith('/feeds/f1')) return ok({ok:true});
  return ok({});
 };})();
'''.replace("'HASH'", "'" + H + "'")


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def asked(path, method):
    return f"__torReq.filter(r=>r.path==={json.dumps(path)}&&r.method==={json.dumps(method)})"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_add_a_magnet_and_choose_its_files():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('torrents'); 1")
        await b.until("!!window.PCTorrents && !!document.querySelector('#feed .tor-tabs')")
        await b.js("PCTorrents.openAdd(); 1")
        await b.until("!!document.querySelector('#tmx-aq')")
        await b.js("(()=>{document.querySelector('#tmx-aq').value='magnet:?xt=urn:btih:%s&dn=Show';"
                   "const c=document.querySelector('#tmx-apick'); if(c) c.checked=true;"
                   "document.querySelector('#tmx-aadd').click();})(); 1" % H)
        await b.until(asked("/add", "POST") + ".length===1")
        body = json.loads(await b.js(asked("/add", "POST") + "[0].body"))
        assert body == {"magnet": f"magnet:?xt=urn:btih:{H}&dn=Show"}, body
        # "choose files" opened the chooser for the torrent it just added.
        await b.until("document.querySelectorAll('.tmx-f input[type=checkbox]').length===3")
        await b.js("document.querySelector('#tmx-fnone').click(); 1")
        assert await b.js("[...document.querySelectorAll('.tmx-f input')].every(i=>!i.checked)")
        await b.js("document.querySelector('#tmx-fall').click(); 1")
        assert await b.js("[...document.querySelectorAll('.tmx-f input')].every(i=>i.checked)")
        await b.js("(()=>{const x=document.querySelector('.tmx-f[data-i=\"2\"] input'); x.checked=false; x.dispatchEvent(new Event('change',{bubbles:true}));})(); 1")
        await b.js("document.querySelector('#tmx-fsave').click(); 1")
        await b.until(asked("/files", "POST") + ".length===1")
        saved = json.loads(await b.js(asked("/files", "POST") + "[0].body"))
        assert saved["num"] == 1 and sorted(saved["files"]) == [0, 1], saved     # extras.zip left out
        # A torrent still fetching its metadata: "Check again" asks the server again.
        await b.js("PCTorrents.openFiles('%s'); 1" % ("cd" * 20))
        await b.until("!!document.querySelector('#tmx-frefresh')")
        before = await b.js(asked("/files/2", "GET") + ".length")
        await b.js("document.querySelector('#tmx-frefresh').click(); 1")
        await b.until(asked("/files/2", "GET") + ".length>%d" % before)
        await b.js("(document.querySelector('#tmx-fcancel')||{click(){}}).click(); __PC.closeModal&&__PC.closeModal(); 1")
        # Add -> Cancel closes and sends nothing.
        await b.js("PCTorrents.openAdd(); 1")
        await b.until("!!document.querySelector('#tmx-acancel')")
        await b.js("document.querySelector('#tmx-acancel').click(); 1")
        await b.until("!document.querySelector('#tmx-acancel')")
        assert await b.js(asked("/add", "POST") + ".length") == 1
        # Close leaves without saving anything more.
        await b.js("PCTorrents.openFiles(%s); 1" % json.dumps(H))
        await b.until("!!document.querySelector('#tmx-fcancel')")
        await b.js("document.querySelector('#tmx-fcancel').click(); 1")
        await b.until("!document.querySelector('#tmx-fcancel')")
        assert await b.js(asked("/files", "POST") + ".length") == 1
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_feeds_subscribe_check_filter_and_remove():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('torrents'); 1")
        await b.until("!!document.querySelector('#tmx-feeds')")
        await b.js("__PC.uiConfirm=async()=>true; 1")      # Remove asks first; torrents.js reads this when Feeds opens
        await b.js("document.querySelector('#tmx-feeds').click(); 1")
        await b.until("/My show/.test((document.querySelector('#tmx-feedbody')||{}).innerText||'')")
        # Subscribe to a new feed with filters.
        await b.js("(()=>{const s=(k,v)=>{document.querySelector(k).value=v;};s('#tmx-nurl','https://feeds.example/other.rss');"
                   "s('#tmx-ntitle','Other');s('#tmx-ninc','720p');s('#tmx-nexc','cam');document.querySelector('#tmx-nadd').click();})(); 1")
        await b.until(asked("/feeds", "POST") + ".length===1")
        sub = json.loads(await b.js(asked("/feeds", "POST") + "[0].body"))
        assert sub.get("url") == "https://feeds.example/other.rss" and sub.get("include") == "720p" and sub.get("exclude") == "cam", sub
        # Check now on the existing feed.
        await b.until("!!document.querySelector('.tmx-feed[data-id=\"f1\"] [data-a=check]')")
        await b.js("document.querySelector('.tmx-feed[data-id=\"f1\"] [data-a=check]').click(); 1")
        await b.until(asked("/feeds/f1/check", "POST") + ".length===1")
        # Filters -> Cancel saves nothing; Filters -> Save sends the new filters.
        await b.until("!!document.querySelector('.tmx-feed[data-id=\"f1\"] [data-a=edit]')")
        await b.js("document.querySelector('.tmx-feed[data-id=\"f1\"] [data-a=edit]').click(); 1")
        await b.until("!!document.querySelector('#tmx-ecancel')")
        await b.js("document.querySelector('#tmx-ecancel').click(); 1")
        await b.until("!!document.querySelector('.tmx-feed[data-id=\"f1\"] [data-a=edit]')")
        assert await b.js(asked("/feeds/f1", "POST") + ".length") == 0
        await b.js("document.querySelector('.tmx-feed[data-id=\"f1\"] [data-a=edit]').click(); 1")
        await b.until("!!document.querySelector('#tmx-esave')")
        await b.js("(()=>{document.querySelector('#tmx-eti').value='Renamed';document.querySelector('#tmx-einc').value='2160p';document.querySelector('#tmx-esave').click();})(); 1")
        await b.until(asked("/feeds/f1", "POST") + ".length===1")
        edit = json.loads(await b.js(asked("/feeds/f1", "POST") + "[0].body"))
        assert edit["title"] == "Renamed" and edit["include"] == "2160p", edit
        # Remove asks first; confirming DELETEs.
        await b.until("!!document.querySelector('.tmx-feed[data-id=\"f1\"] [data-a=del]')")
        await b.js("document.querySelector('.tmx-feed[data-id=\"f1\"] [data-a=del]').click(); 1")
        await b.until(asked("/feeds/f1", "DELETE") + ".length===1")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
