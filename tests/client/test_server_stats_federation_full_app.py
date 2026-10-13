"""Discover → Server Stats → Fediverse, in the real bundled client, on a phone and on a laptop.

Owner: "serverstats should probably show the fediverse queue too". Asserts what a person SEES:
the tab is reachable by a real tap, the panel shows the delivery worker's numbers (lag, sends per
minute, retry queue, failed sends, deletion backlog, failed passes, last pass, last error class),
nothing spills off a 390px screen, it turns RED once the lag passes the alert threshold (and not
before), and a 503 from /client/federation reads "unknown" -- never zeros, never green.

Only the instance HTTP answer for /client/federation is a fixture; the page, scripts and stylesheet
are the shipped bundle.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

# Times are stamped when the page ASKS (below), not at import: a long run aged a module-level stamp to "1m ago".
NOW = int(time.time())
GOOD = {"available": True, "enabled": True, "state": "ok", "lag": 300, "per_min": 6.5, "fails": 2,
        "queued": 12, "failed": 37, "delivered": 4210, "deleting": 3, "last_pass": NOW - 40,
        "error_class": "ConnectError", "lag_alert": 900, "fails_alert": 5, "stale_after": 900,
        "ttl": 30, "now": NOW}

INIT = "window.__fed=" + json.dumps({"status": 200, "body": GOOD}) + r''';
const fedFetch=window.fetch;window.__fedAsks=0;
window.fetch=(url,opts)=>{ if(String(url).includes('/client/federation')){ __fedAsks++;
  return Promise.resolve(new Response(JSON.stringify(__fed.status===200?Object.assign({},__fed.body,(n=>({now:n,last_pass:n-40}))(Math.floor(Date.now()/1000))):{error:'unavailable',state:'unknown'}),
    {status:__fed.status,headers:{'Content-Type':'application/json'}})); }
  return fedFetch(url,opts); };
'''

TILES = "Object.fromEntries([...document.querySelectorAll('.fed-tiles .st-tile')].map(t=>[t.querySelector('.st-tlbl').textContent.trim(),t.querySelector('.st-tval').textContent.trim()]))"
BANNER = "(()=>{const b=document.querySelector('.fed-state');return b?{state:b.dataset.state,text:b.textContent.trim(),color:getComputedStyle(b).color,red:b.classList.contains('down')}:null})()"
FITS = r"""(()=>{const W=innerWidth, bad=[...document.querySelectorAll('.st-wrap .st-tab, .fed-tiles .st-tile, .fed-state')]
  .filter(e=>{const r=e.getBoundingClientRect();return r.width<=0||r.left<-1||r.right>W+1});
  return {overflow:document.documentElement.scrollWidth>W+1, bad:bad.map(e=>e.className+':'+e.textContent.trim().slice(0,30))}})()"""
RED = "rgb(248, 113, 113)"


async def _tap(b, selector):
    target = await b.js("""(()=>{const e=document.querySelector(%s);if(!e)return null;
      e.scrollIntoView({block:'center',inline:'center'});const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
      return {x,y,hit:e.contains(document.elementFromPoint(x,y))};})()""" % json.dumps(selector))
    assert target and target["hit"], f"{selector} is missing, covered or off screen: {target}"
    for kind in ("mousePressed", "mouseReleased"):
        await b.call("Input.dispatchMouseEvent", dict(type=kind, x=target["x"], y=target["y"], button="left", clickCount=1))


async def _scenario(b, width):
    await b.call("Emulation.setDeviceMetricsOverride",
                 {"width": width, "height": 844 if width < 600 else 900, "deviceScaleFactor": 1, "mobile": width < 600})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('stats')")
    await b.until("!!document.querySelector('.st-wrap .st-tab[data-tab=\"fedi\"]')")
    await _tap(b, '.st-tab[data-tab="fedi"]')
    await b.until("!!document.querySelector('.fed-tiles')")

    # Under the alert threshold: behind, said so, but not red.
    tiles = await b.js(TILES)
    assert tiles == {"delivery": "5m behind", "sends per minute": "6.5", "retry queue": "12", "failed sends": "37",
                     "deletion backlog": "3", "failed passes in a row": "2", "last pass": tiles.get("last pass"),
                     "last error": "ConnectError"}, tiles
    assert tiles["last pass"].endswith("s ago"), tiles
    banner = await b.js(BANNER)
    assert banner["state"] == "lagging" and "5m behind" in banner["text"] and not banner["red"], banner
    assert banner["color"] != RED, banner
    fits = await b.js(FITS)
    assert fits == {"overflow": False, "bad": []}, f"at {width}px: {fits}"

    # Past the threshold: red.
    await b.js("__fed.body=Object.assign({},__fed.body,{lag:7200,state:'behind'});PCStats.render()")
    await b.until("document.querySelector('.fed-state')?.dataset.state==='behind'")
    banner = await b.js(BANNER)
    assert banner["red"] and banner["color"] == RED and "2h behind" in banner["text"], banner
    assert (await b.js(TILES))["delivery"] == "2h behind"

    # Caught up: green, and says so.
    await b.js("__fed.body=Object.assign({},__fed.body,{lag:0,state:'ok',fails:0});PCStats.render()")
    await b.until("document.querySelector('.fed-state')?.dataset.state==='ok'")
    assert "Caught up" in (await b.js(BANNER))["text"]
    assert (await b.js(TILES))["delivery"] == "caught up"

    # The endpoint cannot read the report: "unknown" -- not the last numbers, not zeros, not green.
    await b.js("__fed.status=503;PCStats.render()")
    await b.until("document.querySelector('.fed-state')?.dataset.state==='unknown'")
    banner = await b.js(BANNER)
    assert "unknown" in banner["text"].lower() and not banner["red"], banner
    assert await b.js("document.querySelectorAll('.fed-tiles .st-tile').length") == 0, "numbers shown for an unread report"
    assert "Caught up" not in await b.js("document.querySelector('.st-body').textContent")
    fits = await b.js(FITS)
    assert fits["overflow"] is False, f"at {width}px: {fits}"


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_the_fediverse_panel_shows_the_queue_and_turns_red_and_unknown(width):
    async def check(b):
        await _scenario(b, width)
    asyncio.run(desktop.with_browser("online", "", check, extra_init=INIT))
