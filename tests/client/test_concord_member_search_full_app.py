"""Communities: the Known members list can be searched, and a repaint keeps the search.

Asked for: "add a Search for Known members. will be useful for large rooms". Real bundled client; the
room is a local fixture with 20 members, each with a profile name.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP

NAMES = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi", "ivan", "judy",
         "mallory", "niaj", "olivia", "peggy", "rupert", "sybil", "trent", "victor", "walter", "zelda"]

MEMBERS = r"""(()=>{const names=%s;window.__msgs=names.map((n,i)=>{const sk=new Uint8Array(32).fill(i+20);const pk=NostrTools.getPublicKey(sk);
  Store.saveProfile(NostrTools.finalizeEvent({kind:0,created_at:1000,tags:[],content:JSON.stringify({name:n,nip05:n+'@example.com'})},sk));
  return {id:'m'+String(i).padStart(3,'0')+'x'.repeat(60),pubkey:pk,text:'hello from '+n,at:1000+i,kind:9,tags:[]};});})()""" % str(NAMES).replace("'", '"')

SHOWN = r"""(sel=>[...document.querySelectorAll(sel+' .cc-member')].filter(r=>!r.classList.contains('hidden')).map(r=>(r.querySelector('b')||{}).textContent))"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_known_members_can_be_searched_and_the_search_survives_a_repaint():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1500, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.js(MEMBERS)
        await b.js("PCConcord.render()")
        await b.until("document.querySelectorAll('.cc-members-pane .cc-member').length>=20")
        assert await b.js("!!document.querySelector('.cc-members-pane .cc-member-search')"), "no search box on a 20-member room"
        await b.js("document.querySelector('.cc-members-pane .cc-member-search').focus()")
        await b.call("Input.insertText", {"text": "zel"})
        assert await b.js(SHOWN + "('.cc-members-pane')") == ["zelda"]
        # A message arriving repaints the whole room; the search, its result and the caret stay.
        await b.js("PCConcord.render()")
        await asyncio.sleep(.2)
        got = await b.js("({shown:" + SHOWN + "('.cc-members-pane'), value:document.querySelector('.cc-members-pane .cc-member-search').value,"
                         " focused:document.activeElement===document.querySelector('.cc-members-pane .cc-member-search')})")
        assert got == {"shown": ["zelda"], "value": "zel", "focused": True}, got
        # By NIP-05 too, and a miss says so.
        await b.js("(()=>{const i=document.querySelector('.cc-members-pane .cc-member-search');i.value='grace@example';i.dispatchEvent(new Event('input'));})()")
        assert await b.js(SHOWN + "('.cc-members-pane')") == ["grace"]
        await b.js("(()=>{const i=document.querySelector('.cc-members-pane .cc-member-search');i.value='nobody-here';i.dispatchEvent(new Event('input'));})()")
        assert "No member matches" in await b.js("document.querySelector('.cc-members-pane').textContent")

    asyncio.run(desktop.with_browser("online", "", check))


ME = r"""(()=>{const sk=new Uint8Array(32).fill(1);
  Store.saveProfile(NostrTools.finalizeEvent({kind:0,created_at:1001,tags:[],content:JSON.stringify({name:'verita84',nip05:'verita84@poster.place'})},sk));
  window.__msgs.push({id:'mme'+'x'.repeat(61),pubkey:NostrTools.getPublicKey(sk),text:'my own message',at:2000,kind:9,tags:[]});})()"""
TYPE = r"""((SEL,t)=>{const i=document.querySelector(SEL+' .cc-member-search');i.focus();i.value=t;i.dispatchEvent(new Event('input'));})"""


async def _room(b, width):
    await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(SETUP)
    await b.js(MEMBERS)
    await b.js(ME)
    await b.js("PCConcord.render()")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_typing_the_start_of_your_own_name_finds_you_and_an_npub_finds_its_member():
    """"i should be able to type in ver and it find me". Your OWN row is labelled with your name, and
    a prefix of it finds you; so does your NIP-05; and a pasted npub finds its member (the search text
    held the hex key only, so an npub -- the form people actually copy -- found nobody)."""
    got = {}

    async def check(b):
        await _room(b, 1500)
        await b.until("document.querySelectorAll('.cc-members-pane .cc-member').length>=21")
        sel = "'.cc-members-pane'"
        for q in ("ver", "VERITA", "verita84@poster"):
            await b.js(TYPE + "(" + sel + "," + repr(q).replace("'", '"') + ")")
            got[q] = await b.js(SHOWN + "(" + sel + ")")
        npub = await b.js("NostrTools.nip19.npubEncode(NostrTools.getPublicKey(new Uint8Array(32).fill(20+3)))")
        await b.js(TYPE + "(" + sel + "," + repr(npub[:20]).replace("'", '"') + ")")
        got['npub'] = await b.js(SHOWN + "(" + sel + ")")
    asyncio.run(desktop.with_browser("online", "", check))
    for q in ("ver", "VERITA", "verita84@poster"):
        assert got[q] == ["verita84"], (q, got)
    assert got['npub'] == ["dave"], got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_a_phone_the_members_dialog_searches_too():
    """On a phone the side pane is not shown; Known members is the Members dialog, with its own box."""
    got = {}

    async def check(b):
        await _room(b, 390)
        # Opened the way a person does on a phone: into the channel, then Members in its header.
        await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
        await b.until("getComputedStyle(document.querySelector('.cc-conversation')).display!=='none'")
        await b.until("[...document.querySelectorAll('button')].some(x=>/^members$/i.test(x.getAttribute('aria-label')||x.title||''))")
        await b.js("[...document.querySelectorAll('button')].filter(x=>/^members$/i.test(x.getAttribute('aria-label')||x.title||'')&&x.getBoundingClientRect().width>0)[0].click()")
        await b.until("(()=>{const d=document.getElementById('cc-members-dialog');return !!d&&!d.classList.contains('hidden')&&d.getBoundingClientRect().height>0})()")
        await b.until("!!document.querySelector('#cc-members-dialog .cc-member-search')")
        # The channel is still loading behind it; its repaint must not shut the dialog (it did, in 0.1s).
        await asyncio.sleep(1.2)
        got['still_open'] = await b.js("getComputedStyle(document.getElementById('cc-members-dialog')).display!=='none'")
        await b.js(TYPE + "('#cc-members-dialog','ver')")
        await b.until("document.querySelector('#cc-members-dialog .cc-member-search').getBoundingClientRect().height>0")
        got['shown'] = await b.js(SHOWN + "('#cc-members-dialog')")
        got['fits'] = await b.js("""(()=>{const i=document.querySelector('#cc-members-dialog .cc-member-search').getBoundingClientRect();
          return {ok:i.left>=0&&i.right<=innerWidth+0.5&&i.height>=32,l:i.left,r:i.right,h:i.height,w:innerWidth};})()""")
    asyncio.run(desktop.with_browser("online", "", check))
    assert got['still_open'], ('the Members dialog shut itself while the channel loaded', got)
    assert got['shown'] == ["verita84"], got
    assert got['fits']['ok'], got
