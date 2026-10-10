"""Concord community folders in the real client: made from the menu, synced, Armada's imported.

Three rooms. Right-click (or long-press) a community -> "New folder with ..." -> the folder appears in
the rail holding both, opens and closes on click, and the account's own encrypted doc d=pcai:concord-rail
is written with it. A fresh account whose Armada settings (d=armada/metadata) hold a folder sees that
folder -- and Armada's doc is never written (it holds every other Armada setting).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ROOMS = r"""(armada)=>{
  const me=__PC.me().pubkey, ids=['a','b','c'].map(c=>c.repeat(64)), names=['Lounge','Politics','Games'];
  const rooms=ids.map((id,i)=>({name:names[i],communityId:id,naddr:'n'+i,url:'https://fixture.invalid/invite/'+i+'#s',
    channels:[{id:'ch'+i,name:'general'}],cord:{bundle:{owner:me,relays:['wss://fixture.invalid']},hydrated:true}}));
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify(rooms));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  localStorage.removeItem('pc.concord.railOpen');
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'ch0',name:'general',streamPubkeys:[]}]}),
    inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]})};
  // The account's relays: an empty rail doc, and (optionally) Armada's settings with a folder.
  window.__published=[];
  const docs={};
  if(armada) docs['armada/metadata']={railLayout:[{type:'folder',id:'arm',name:'From Armada',keys:['c1:'+ids[1],'c2:'+ids[2]]},{type:'item',key:'c2:'+ids[0]}],theme:'dark',other:'kept'};
  __PC.nip44enc=async(peer,text)=>'enc:'+text; __PC.nip44dec=async(peer,ct)=>String(ct).replace(/^enc:/,'');
  __PC.relayQuery=async(filters)=>{const d=(filters[0]['#d']||[])[0]; const out=docs[d]?[{kind:30078,pubkey:me,created_at:1,content:'enc:'+JSON.stringify(docs[d]),tags:[['d',d]]}]:[]; out.complete=true; return out;};
  __PC.relayPublish=async(ev)=>{__published.push(ev); const d=(ev.tags.find(t=>t[0]==='d')||[])[1]; docs[d]=JSON.parse(String(ev.content).replace(/^enc:/,'')); return {ok:true};};
  __PC.uiPrompt=async()=> 'Fun';
  __PC.switchView('concord');
}"""

RAIL = r"""(()=>{const rail=document.querySelector('.cc-communities');if(!rail)return null;
  return [...rail.children].filter(n=>n.matches('[data-cc-server]:not(#cc-add):not(#cc-discovery),.cc-folder')).map(n=>n.classList.contains('cc-folder')
    ?{folder:n.querySelector('[data-cc-folder]').getAttribute('title'),open:n.classList.contains('open'),members:[...n.querySelectorAll('[data-cc-server]')].map(b=>b.getAttribute('title'))}
    :{room:n.getAttribute('title')});})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_folder_is_made_from_the_menu_opens_closes_and_syncs():
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=850, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("(" + ROOMS + ")(false)")
        await b.until("document.querySelectorAll('.cc-communities [data-cc-server]').length>=3")
        res["before"] = await b.js(RAIL)
        # Right-click "Lounge" -> New folder with "Politics".
        await b.js("(()=>{const b=[...document.querySelectorAll('.cc-communities [data-cc-server]')].find(x=>x.title==='Lounge');b.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));})()")
        await b.until("!!document.querySelector('.menu-pop')")
        res["menu"] = await b.js("[...document.querySelectorAll('.menu-pop [data-m]')].map(x=>x.textContent)")
        await b.js("[...document.querySelectorAll('.menu-pop [data-m]')].find(x=>/New folder with .Politics/.test(x.textContent)).click()")
        await b.until("!!document.querySelector('.cc-folder')")
        await asyncio.sleep(.3)
        res["made"] = await b.js(RAIL)
        await b.js("document.querySelector('[data-cc-folder]').click()")
        await asyncio.sleep(.3)
        res["closed"] = await b.js(RAIL)
        await b.until("__published.some(e=>(e.tags.find(t=>t[0]==='d')||[])[1]==='pcai:concord-rail')")
        res["doc"] = await b.js("JSON.parse(String(__published.filter(e=>(e.tags.find(t=>t[0]==='d')||[])[1]==='pcai:concord-rail').pop().content).replace(/^enc:/,''))")

    asyncio.run(desktop.with_browser("online", "", check))
    assert [x.get("room") for x in res["before"]] == ["Lounge", "Politics", "Games"], res["before"]
    assert any("New folder with “Politics”" == m for m in res["menu"]), res["menu"]
    made = res["made"]
    assert made[0] == {"folder": "Fun", "open": True, "members": ["Politics", "Lounge"]} and made[1] == {"room": "Games"}, made
    assert res["closed"][0]["folder"] == "Fun" and res["closed"][0]["open"] is False and res["closed"][0]["members"] == [], res["closed"]
    layout = res["doc"]["layout"]
    assert layout[0]["type"] == "folder" and layout[0]["name"] == "Fun" and len(layout[0]["keys"]) == 2, res["doc"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_folders_made_in_armada_are_shown_and_armadas_settings_never_written():
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=390, height=850, deviceScaleFactor=1, mobile=True))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("(" + ROOMS + ")(true)")
        await b.until("!!document.querySelector('.cc-folder')")
        res["rail"] = await b.js(RAIL)
        # An edit here writes OUR doc only.
        await b.js("document.querySelector('[data-cc-folder]').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))")
        await b.until("!!document.querySelector('.menu-pop')")
        await b.js("[...document.querySelectorAll('.menu-pop [data-m]')].find(x=>x.dataset.m==='rename').click()")
        await b.until("__published.length>0")
        await asyncio.sleep(.3)
        res["ds"] = await b.js("__published.map(e=>(e.tags.find(t=>t[0]==='d')||[])[1])")
        res["rail2"] = await b.js(RAIL)

    asyncio.run(desktop.with_browser("online", "", check))
    assert res["rail"][0]["folder"] == "From Armada" and res["rail"][1] == {"room": "Lounge"}, res["rail"]
    assert set(res["ds"]) == {"pcai:concord-rail"}, ("Armada's settings doc was written", res["ds"])
    assert res["rail2"][0]["folder"] == "Fun", res["rail2"]
