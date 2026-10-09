"""Admin → Bots → Edit: every LIST field looks like Admin → Nostr Relay's lists.

"fix the Edit bots UI to look nice for text lists like in admin relay". The relay tab's lists are a
short label, a full-width box holding one entry per line, and the explanation UNDERNEATH. The bot
editor had four lists in four shapes: a one-line input (trusted media hosts), a one-line password
input (the Concord invite — which could therefore hold only one room), and two-row boxes with the
whole explanation crammed into the label. Measured in headless Chrome with the shipped markup, the
shipped stylesheets and the shipped admin-bots.js, at desktop and phone width.
"""
import asyncio
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser

ROOT = Path(__file__).resolve().parents[2]
LISTS = ["bot_f_nostr_rate_exempt", "bot_f_concord_invite", "bot_f_auto_post_topics", "bot_f_trusted_media_hosts"]

PROBE = r"""(ids=>{
  // The editor is a closed modal with per-platform groups: open every ancestor of every list.
  for(const id of ids)for(let a=document.getElementById(id);a;a=a.parentElement)
    if(getComputedStyle(a).display==='none')a.style.display='block';
  const out={};
  for(const id of ids){
    const el=document.getElementById(id),g=el.closest('.form-group'),r=el.getBoundingClientRect(),gr=g.getBoundingClientRect(),
          hint=el.nextElementSibling,cs=getComputedStyle(el),lab=g.querySelector('label[for="'+id+'"]');
    out[id]={tag:el.tagName,height:r.height,widthShare:r.width/gr.width,mono:/mono|consolas|menlo/i.test(cs.fontFamily),
             hintBelow:!!hint&&hint.tagName==='SMALL'&&hint.getBoundingClientRect().top>=r.bottom-1&&getComputedStyle(hint).display==='block',
             labelText:lab?lab.textContent.trim():'',overflow:el.getBoundingClientRect().right>innerWidth+1};
  }
  out.pageOverflow=document.documentElement.scrollWidth>innerWidth+1;
  return out;})"""


async def _run(width):
    html = (ROOT / "templates/admin/tabs/bots.html").read_text()
    js = (ROOT / "static/js/admin-bots.js").read_text()
    css = (ROOT / "static/css/admin-tabs.css").read_text() + (ROOT / "static/css/admin-theme.css").read_text()
    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><style>"
            + css + "</style><body class=admin-page><div class=admin-page>" + html + "</div>"
            "<script>window.__errors=[];onerror=m=>__errors.push(String(m));"
            "window.pcAlert=()=>{};window.pcConfirm=async()=>true;</script>"
            "<script>" + js.replace("</script>", "<\\/script>") + "</script>")
    with tempfile.TemporaryDirectory(prefix="pc-botlists-") as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            url = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(url, max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call("Page.enable")
                await b.call("Emulation.setDeviceMetricsOverride",
                             {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
                frame = (await b.call("Page.getFrameTree"))["frameTree"]["frame"]["id"]
                await b.call("Page.setDocumentContent", {"frameId": frame, "html": page})
                await asyncio.sleep(.3)
                got = await b.js(PROBE + "(" + str(LISTS).replace("'", '"') + ")")
                inv = "document.getElementById('bot_f_concord_invite')"
                await b.js(inv + ".value='https://x.example/c/naddr1a#secret1\\nhttps://x.example/c/naddr1b#secret2'")
                got["masked"] = await b.js(f"getComputedStyle({inv}).webkitTextSecurity")
                await b.js("toggleBotConcord()")
                got["revealed"] = await b.js(f"getComputedStyle({inv}).webkitTextSecurity")
                got["kept_lines"] = await b.js(inv + ".value.split('\\n').length")
                got["errors"] = await b.js("__errors")
                return got
        finally:
            proc.kill()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_every_list_is_a_full_width_box_with_its_hint_underneath(width):
    got = asyncio.run(_run(width))
    assert not got["errors"], got["errors"]
    for id_ in LISTS:
        f = got[id_]
        assert f["tag"] == "TEXTAREA", (id_, "a list in a one-line input", f)
        assert f["height"] >= 60, (id_, "a list box shows fewer than 3 lines", f)
        assert f["widthShare"] >= 0.9 and f["mono"], (id_, f)
        assert f["hintBelow"], (id_, "the explanation is not underneath the box, like the relay tab", f)
        assert len(f["labelText"]) <= 30, (id_, "the explanation is crammed into the label again", f)
        assert not f["overflow"], (id_, width, f)
    assert not got["pageOverflow"], width
    assert got["kept_lines"] == 2, "the invite box cannot hold one room per line"
    assert got["masked"] == "disc" and got["revealed"] == "none", (got["masked"], got["revealed"])
