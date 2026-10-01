"""Admin → Bots: a NOSTR bot is offered "Welcome new users" and "Report bot".

Reported: "i don't see report bot in the edit bots". Both modes run on Nostr since the Nostr report
and welcome bots (botframework/nostr_reportbot.py, nostr_welcomebot.py), but the form still hid
their checkboxes for every platform but Pleroma -- and UNTICKED them, so a Nostr bot could not be
given either mode at all. Renders the shipped tab markup with the shipped admin-bots.js in headless
Chrome and reads what is actually visible after choosing each platform.
"""
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser

ROOT = Path(__file__).resolve().parents[2]

PROBE = r"""(()=>{const vis=id=>{const c=document.getElementById(id);const l=c&&c.closest('label');
  return !!(l&&getComputedStyle(l).display!=='none');};
  return {report:vis('bot_ft_report'),welcome:vis('bot_ft_welcome'),unfollow:vis('bot_ft_unfollow'),
          block:vis('bot_ft_block'),dvm:vis('bot_ft_dvm')};})()"""


async def _run():
    html = (ROOT / "templates/admin/tabs/bots.html").read_text()
    js = (ROOT / "static/js/admin-bots.js").read_text()
    page = ("<!doctype html><meta charset=utf-8><body>" + html +
            "<script>window.__errors=[];onerror=m=>__errors.push(String(m));" 
            "window.pcAlert=()=>{};window.pcConfirm=async()=>true;</script>"
            "<script>" + js.replace("</script>", "<\\/script>") + "</script>")
    with tempfile.TemporaryDirectory(prefix="pc-botform-") as profile:
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
                frame = (await b.call("Page.getFrameTree"))["frameTree"]["frame"]["id"]
                await b.call("Page.setDocumentContent", {"frameId": frame, "html": page})
                await asyncio.sleep(.3)
                out = {}
                for plat in ("nostr", "pleroma"):
                    await b.js(f"""(()=>{{const s=document.getElementById('bot_f_platform');
                        if(![...s.options||[]].some(o=>o.value==={json.dumps(plat)})){{const o=document.createElement('option');o.value={json.dumps(plat)};s.appendChild(o);}}
                        s.value={json.dumps(plat)};const t=document.getElementById('bot_f_type');if(t)t.value='text';
                        ['bot_ft_report','bot_ft_welcome'].forEach(i=>document.getElementById(i).checked=true);
                        onBotFormChange();}})()""")
                    out[plat] = await b.js(PROBE)
                    out[plat]["kept"] = await b.js("document.getElementById('bot_ft_report').checked && document.getElementById('bot_ft_welcome').checked")
                out["errors"] = await b.js("__errors")
                return out
        finally:
            proc.kill()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_nostr_bot_is_offered_the_report_and_welcome_bots():
    got = asyncio.run(_run())
    assert not got["errors"], got["errors"]
    n = got["nostr"]
    assert n["report"] and n["welcome"], ("a Nostr bot cannot be given the report/welcome bot", n)
    assert n["kept"], "choosing Nostr unticked the report/welcome bot, so it is never saved"
    assert n["block"] and n["dvm"], n                     # the form itself rendered
    assert not n["unfollow"], "the unfollow bot still needs Pleroma's database"
    p = got["pleroma"]
    assert p["report"] and p["welcome"] and p["unfollow"] and not p["dvm"], p
