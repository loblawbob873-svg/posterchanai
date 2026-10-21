"""On a phone (and in the Android app) the timeline header is a PICKER + a REPLIES SWITCH.

Asked for: "a Timeline switcher at the top that serves as a dropdown with the different timelines.
Somewhere near that, add a slider that lets you show/hide replies. Show replies is default. Should
honor the User Settings for this."

The real client at 390px over a fixture relay serving posts AND replies:

  * the three tabs are gone; a dropdown naming the current timeline is shown, and choosing another
    timeline in it switches to that timeline;
  * the Replies switch is ON by default and replies are on screen; turning it off takes every reply
    off the feed (the composer above it is untouched), stores `hideReplies` and publishes it to the
    account's `pcai:client-prefs`; turning it on brings them back;
  * User Settings -> "Hide replies in timelines" drives the same switch (it starts OFF after that);
  * at 1280px nothing changes: the tabs are still the tabs and the picker/switch are not drawn.
"""
import asyncio

from tests.client import test_search_bar_autohide_full_app as sb

needs_chrome = sb.needs_chrome

# Six of the fixture's posts get a reply each. Replaces soc.INIT's builder (a global function, so the
# fixture socket's call to it resolves to this one).
REPLIES = r'''
(()=>{const orig=window.__buildPosts;
 window.__buildPosts=function(){
  if(window.__withReplies) return window.__withReplies;
  const base=orig(); const sk=new Uint8Array(32).fill(9); const now=Math.floor(Date.now()/1000);
  const out=base.slice();
  for(let i=0;i<6;i++){ const p=base[i];
    out.push(NostrTools.finalizeEvent({kind:1,created_at:now-i*60-30,
      tags:[['e',p.id,'','root'],['p',p.pubkey]],content:'fixture REPLY number '+i},sk)); }
  window.__withReplies=out; return out; };})();
window.__tlState=()=>{const f=document.querySelector('#feed');const vis=el=>!!el&&getComputedStyle(el).display!=='none'&&el.getBoundingClientRect().width>0;
  const sel=f.querySelector('#tl-select'),sw=f.querySelector('#tl-replies');
  const notes=f.querySelector('#tl-notes');const txt=notes?notes.textContent:'';
  return {view:__PC.VIEW,tabsShown:[...f.querySelectorAll('.tltab')].filter(vis).length,
    pickShown:vis(f.querySelector('.tl-pick')),pickValue:sel&&sel.value,
    options:sel?[...sel.options].map(o=>o.value):[],
    switchShown:vis(f.querySelector('.tl-replies')),switchOn:sw?sw.checked:null,
    replies:(txt.match(/fixture REPLY number/g)||[]).length,posts:(txt.match(/fixture timeline post number/g)||[]).length,
    hide:ClientSettings.get('hideReplies',false)};};
'''


async def _device(act, **kw):
    orig = sb.PREFS
    sb.PREFS = orig + REPLIES
    try:
        return await sb.device(act, **kw)
    finally:
        sb.PREFS = orig


async def st(b):
    return await b.js("window.__tlState()")


@needs_chrome
def test_phone_header_is_a_picker_and_a_replies_switch():
    async def act(b):
        out = {}
        await b.until("window.__tlState().replies>0")
        out["start"] = await st(b)
        # Choose Home from the dropdown, then back to the Nostrverse.
        await b.js("(()=>{const s=document.getElementById('tl-select');s.value='home';s.dispatchEvent(new Event('change'));})()")
        await b.until("__PC.VIEW==='home'")
        out["home"] = await st(b)
        await b.js("(()=>{const s=document.getElementById('tl-select');s.value='global';s.dispatchEvent(new Event('change'));})()")
        await b.until("__PC.VIEW==='global' && window.__tlState().replies>0")
        # Type into the composer first: hiding replies must not wipe it.
        await b.js("(()=>{const t=document.getElementById('tl-cmp-ta');t.value='half typed';t.dispatchEvent(new Event('input'));window.__ta=t;})()")
        await b.js("(()=>{const s=document.getElementById('tl-replies');s.checked=false;s.dispatchEvent(new Event('change'));})()")
        await asyncio.sleep(.6)
        out["off"] = await st(b)
        out["composer"] = await b.js("document.getElementById('tl-cmp-ta')===window.__ta && window.__ta.value")
        await b.until("window.__published.some(e=>(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs'))")
        out["published"] = await b.js("window.__published")
        await b.js("(()=>{const s=document.getElementById('tl-replies');s.checked=true;s.dispatchEvent(new Event('change'));})()")
        await asyncio.sleep(.6)
        out["on"] = await st(b)
        return out
    s = asyncio.run(_device(act))
    a = s["start"]
    assert a["tabsShown"] == 0 and a["pickShown"], ("the phone header still shows tabs, not the picker", a)
    assert a["pickValue"] == "global" and set(a["options"]) >= {"home", "global"}, a
    assert a["switchShown"] and a["switchOn"] is True and a["replies"] > 0, (
        "replies must be shown by default, with the switch on", a)
    assert s["home"]["view"] == "home" and s["home"]["pickValue"] == "home", ("the picker did not switch", s["home"])
    off = s["off"]
    assert off["replies"] == 0 and off["posts"] > 0 and off["hide"] is True, ("turning Replies off left replies", off)
    assert s["composer"] == "half typed", "hiding replies rebuilt the composer and lost the text"
    assert (sb._prefs_of(s["published"]) or {}).get("hideReplies") is True, "the choice was not saved to the account"
    on = s["on"]
    assert on["replies"] > 0 and on["hide"] is False and on["switchOn"] is True, ("turning Replies on did not bring them back", on)


@needs_chrome
def test_user_settings_drive_the_switch():
    async def act(b):
        await sb.open_timeline_settings(b)
        await b.until("!!document.getElementById('set-hide-replies')")
        await b.js("(()=>{const c=document.getElementById('set-hide-replies');c.checked=true;c.dispatchEvent(new Event('change'));})()")
        await b.js("__PC.switchView('global')")
        await b.until("window.__feedCards() >= 8 && !!document.getElementById('tl-replies')")
        await asyncio.sleep(.6)
        return await st(b)
    s = asyncio.run(_device(act))
    assert s["switchOn"] is False and s["replies"] == 0 and s["posts"] > 0, (
        "Settings -> Hide replies is not reflected by the timeline's switch", s)


@needs_chrome
def test_desktop_keeps_its_tabs():
    async def act(b):
        return await st(b)
    s = asyncio.run(_device(act, width=1280, height=800, mobile=False))
    assert s["tabsShown"] >= 2 and not s["pickShown"] and not s["switchShown"], (
        "the desktop header changed", s)
