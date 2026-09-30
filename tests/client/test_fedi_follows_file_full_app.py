"""Settings → Fediverse: Export my fediverse follows / Import a follows file, in the real client.

"add ability for users to export their Fediverse follows so they can import it back into posterchan or
a different fediverse server". Driven in the bundled client at phone and desktop width, with only the
network boundary faked: Export sends the member's own signed follow list and saves a Mastodon-format
`following_accounts.csv`; Import reads such a file (Mastodon's, a one-column list, with @s, quotes and
junk) and sends exactly its accounts; both buttons are on screen; nothing to export says so.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
P1, P2, NATIVE = "11" * 32, "22" * 32, "ab" * 32

NET = r"""
window.__bodies={export:[],import:[]};window.__saved=[];window.__exportAccounts=EXPORT;
const _f=window.fetch;
window.fetch=function(url,opts={}){
  const u=String(url);
  const ok=o=>Promise.resolve(new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}}));
  if(u.includes('/api/activitypub/export-following')){__bodies.export.push(JSON.parse(opts.body));
    return ok({following:3,accounts:__exportAccounts});}
  if(u.includes('/api/activitypub/import-following')){__bodies.import.push(JSON.parse(opts.body));
    return ok({following:JSON.parse(opts.body).accounts.length,people:[]});}
  return _f.apply(this,arguments);
};
const _q=window.Relay.query.bind(window.Relay);
window.Relay.query=async(filters,...rest)=>{
  if(filters&&filters[0]&&(filters[0].kinds||[]).includes(3))
    return [{kind:3,created_at:10,pubkey:__PC.ME.pubkey,tags:[['p',P1],['p',NATIVE],['p',P2]],content:''}];
  return _q(filters,...rest);
};
const _blobs=new Map(), _mk=URL.createObjectURL.bind(URL);
URL.createObjectURL=o=>{const u=_mk(o);_blobs.set(u,o);return u;};
HTMLAnchorElement.prototype.click=function(){ if(this.download){ const n=this.download, bl=_blobs.get(this.href);
  (bl?bl.text():Promise.resolve('')).then(t=>__saved.push({name:n,text:t})); } };
"""

FILE = ('Account address,Show boosts,Notify on new posts,Languages\r\n'
        'alice@mastodon.example,true,false,\r\n'
        '"@bob@pleroma.example",true,false,\r\n'
        'ALICE@mastodon.example,true,false,\r\n'
        'this is not an account\r\n'
        'https://evil.example/users/x\r\n'
        'carol@gts.example\n')


async def _social(b, phone):
    w, h = (390, 844) if phone else (1280, 900)
    await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': phone})
    await desktop.login(b)
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('#us-fedi-export')")
    await b.js("document.querySelector('.us-tab[data-tab=social]')?.click()")
    await b.until("document.querySelector('#us-fedi-export').offsetParent!==null")
    await b.js(f"window.P1={json.dumps(P1)};window.P2={json.dumps(P2)};window.NATIVE={json.dumps(NATIVE)};"
               "window.EXPORT=[{pubkey:P2,acct:'user2@pleroma.example'},{pubkey:P1,acct:'user1@mastodon.example'}];" + NET)


def _visible(sel):
    return f"""(()=>{{const e=[...document.querySelectorAll('{sel}')].find(x=>x.offsetParent!==null);if(!e)return null;
      const r=e.getBoundingClientRect();return {{l:r.left,r:r.right,w:r.width,vw:innerWidth}}}})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_export_saves_a_mastodon_follows_file_from_the_signed_list(phone):
    got = {}

    async def check(b):
        await _social(b, phone)
        for sel in ('#us-fedi-export', '#us-fedi-import-file-btn'):
            r = await b.js(_visible(sel))
            assert r and r['l'] >= 0 and r['r'] <= r['vw'] + .5 and r['w'] > 40, (sel, r)
        await b.js("[...document.querySelectorAll('#us-fedi-export')].find(x=>x.offsetParent!==null).click()")
        await b.until("__saved.length===1")
        got['body'] = await b.js("__bodies.export[0]")
        got['file'] = await b.js("__saved[0]")
        got['said'] = await b.js("[...document.querySelectorAll('#us-fedi-file-said')].map(x=>x.textContent).join('|')")
        # Nothing to export is a sentence, not an empty file.
        await b.js("__exportAccounts=[];[...document.querySelectorAll('#us-fedi-export')].find(x=>x.offsetParent!==null).click()")
        await b.until("[...document.querySelectorAll('#us-fedi-file-said')].some(x=>/nothing to export/.test(x.textContent))")
        got['saved_after'] = await b.js("__saved.length")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
    assert set(got['body']['pubkeys']) >= {P1, P2, NATIVE}, got['body']
    assert got['file']['name'] == 'following_accounts.csv'
    assert got['file']['text'] == ('Account address,Show boosts,Notify on new posts,Languages\n'
                                   'user2@pleroma.example,true,false,\n'
                                   'user1@mastodon.example,true,false,\n'), got['file']
    assert 'Exported 2 fediverse accounts' in got['said'], got['said']
    assert got['saved_after'] == 1


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_importing_a_follows_file_sends_exactly_its_accounts(phone):
    got = {}

    async def check(b):
        await _social(b, phone)
        await b.js("""(text=>{const input=[...document.querySelectorAll('#us-fedi-import-file')]
            .find(x=>x.closest('label').offsetParent!==null);
          const dt=new DataTransfer();dt.items.add(new File([text],'following_accounts.csv',{type:'text/csv'}));
          input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));})(""" + json.dumps(FILE) + ")")
        await b.until("__bodies.import.length===1")
        got['body'] = await b.js("__bodies.import[0]")
        await b.until("[...document.querySelectorAll('#us-fedi-file-said')].some(x=>/of 3 there/.test(x.textContent))")
        # A file with no accounts in it never reaches the server.
        await b.js("""(()=>{const input=[...document.querySelectorAll('#us-fedi-import-file')].find(x=>x.closest('label').offsetParent!==null);
          const dt=new DataTransfer();dt.items.add(new File(['hello\\nworld'],'notes.txt',{type:'text/plain'}));
          input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()""")
        await b.until("[...document.querySelectorAll('#us-fedi-file-said')].some(x=>/no fediverse accounts/.test(x.textContent))")
        got['calls'] = await b.js("__bodies.import.length")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
    assert got['body'] == {'accounts': ['alice@mastodon.example', 'bob@pleroma.example', 'carol@gts.example']}, got
    assert got['calls'] == 1
