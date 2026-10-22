"""The profile header's buttons carry icons, and the QR button shows a QR code.

Reported: "The profile page have a button for profile management without any icon. The qr button
don't show any qr code, only copy the npub." Edit had no icon at all, Settings' compact form was a
text "⚙" in a slot styled for SVG icons, a desktop rule hid EVERY icon in the row (Call, Zap, Tip), and
the QR-glyph button was the Copy button. The real bundled client, phone and desktop widths.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ICON = """(sel=>{const b=document.querySelector(sel); if(!b) return 'missing';
  const i=b.querySelector('svg.ic'); if(!i) return 'no icon';
  const r=i.getBoundingClientRect(); return (r.width>4&&r.height>4&&getComputedStyle(i).display!=='none')?'ok':'hidden';})"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_own_profile_buttons_have_icons_and_the_qr_shows_a_code(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile(__PC.me().pubkey)")
        await b.until("!!document.querySelector('#edit-prof') && !!document.querySelector('#prof-qr')")
        for sel in ("#edit-prof", "#open-settings", "#prof-pay"):
            assert await b.js(f"({ICON})('{sel}')") == "ok", f"{sel} shows no icon at {width}px"
        npub = await b.js("NostrTools.nip19.npubEncode(__PC.me().pubkey)")
        await b.js("document.querySelector('#prof-qr').click()")
        await b.until("!!document.querySelector('.prof-qr-code svg')")
        got = await b.js("""({n:document.querySelector('.prof-qr-code svg').querySelectorAll('rect,path').length,
            code:document.querySelector('.prof-qr-npub').textContent})""")
        assert got["n"] >= 1 and got["code"] == npub, got
        assert await b.js("typeof document.getElementById('copy-npub').onclick") == "function", "Copy npub lost its binding"

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_someone_elses_profile_keeps_its_call_and_zap_icons_on_desktop():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile('" + "7" * 64 + "')")
        await b.until("!!document.querySelector('#zap-prof')")
        for sel in ("#call-prof", "#zap-prof"):
            assert await b.js(f"({ICON})('{sel}')") == "ok", f"{sel}'s icon is hidden on desktop"

    asyncio.run(desktop.with_browser("online", "", check))


# Someone with two followers this account does not follow yet, served by a relay that stores what it is sent.
PEOPLE = r'''
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000);
  const sk=i=>{const b=new Uint8Array(32); b[30]=7; b[31]=i; return b;};
  const target=NT.getPublicKey(sk(1)), fol=[sk(2),sk(3)];
  const k3s=fol.map(k=>NT.finalizeEvent({kind:3,created_at:now-100,tags:[['p',target]],content:''},k));
  const prof={[target]:NT.finalizeEvent({kind:0,created_at:now-50,tags:[],content:JSON.stringify({name:'Target Person'})},sk(1))};
  fol.forEach((k,i)=>{ const pk=NT.getPublicKey(k); prof[pk]=NT.finalizeEvent({kind:0,created_at:now-50,tags:[],content:JSON.stringify({name:'Follower '+i})},k); });
  const note=NT.finalizeEvent({kind:1,created_at:now-20,tags:[],content:'a post by the target'},sk(1));
  return (window.__made={target,k3s,prof,note,followers:fol.map(k=>NT.getPublicKey(k))}); }
window.__stored=[];
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  if(m[0]==='EVENT'){__stored.push(m[1]);setTimeout(()=>out(['OK',m[1].id,true,'']),5);return;}
  if(m[0]!=='REQ')return; const id=m[1], D=__mk();
  for(const f of m.slice(2)){
    const k=f.kinds||[];
    if(k.includes(3)&&(f['#p']||[]).includes(D.target)) D.k3s.forEach(e=>out(['EVENT',id,e]));
    if(k.includes(3)&&f.authors) __stored.filter(e=>e.kind===3&&f.authors.includes(e.pubkey)).forEach(e=>out(['EVENT',id,e]));
    if(k.includes(0)&&f.authors) f.authors.forEach(a=>D.prof[a]&&out(['EVENT',id,D.prof[a]]));
    if(k.includes(1)&&(f.authors||[]).includes(D.target)) out(['EVENT',id,D.note]);
  }
  setTimeout(()=>out(['EOSE',id]),10); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
window.__copied=[];
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{__copied.push(String(t));}}});}catch(_){}
'''


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_qr_copy_and_close_posts_followers_and_follow_all_back():
    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        target = await b.js("__mk().target")
        npub = await b.js(f"NostrTools.nip19.npubEncode('{target}')")
        await b.js(f"__PC.openProfile('{target}')")
        await b.until("!!document.querySelector('#prof-qr') && !!document.querySelector('#show-followers')")

        await b.js("document.querySelector('#prof-qr').click()")
        await b.until("!!document.querySelector('#prof-qr-copy')")
        await b.js("document.querySelector('#prof-qr-copy').click()")
        await b.until(f"__copied.includes('{npub}')")
        await b.js("document.querySelector('#prof-qr-close').click()")
        await b.until("!document.querySelector('#prof-qr-close')")

        # Posts sends you to the Notes tab, which lists the post.
        await b.js("document.querySelector('.prof-tab:not([data-tab=notes])').click()")
        await b.until("document.querySelector('.prof-tab.active')?.dataset.tab!=='notes'")
        await b.js("document.querySelector('#show-posts').click()")
        await b.until("document.querySelector('.prof-tab.active')?.dataset.tab==='notes' && /a post by the target/.test(document.querySelector('#feed').textContent)")

        # Followers lists both, and Follow all back follows both in ONE kind-3.
        await b.js("document.querySelector('#show-followers').click()")
        await b.until("document.querySelectorAll('#people-list [data-prof]').length===2")
        await b.until("getComputedStyle(document.querySelector('#follow-all-back')).display!=='none'")
        assert await b.js("document.querySelector('#follow-all-back').textContent") == "Follow all back (2)"
        await b.js("window.__publishOK=true; document.querySelector('#follow-all-back').click()")
        await b.until("__stored.some(e=>e.kind===3)")
        follows = await b.js("__stored.filter(e=>e.kind===3).pop().tags.filter(t=>t[0]==='p').map(t=>t[1])")
        assert sorted(follows) == sorted(await b.js("__mk().followers")), follows
        await b.until("getComputedStyle(document.querySelector('#follow-all-back')).display==='none'")
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check, PEOPLE))
