"""Admin → Bots → Edit: every list field is a searchable list like Admin → Relay's, at phone and desktop width.

"you were supposed to improve all the text area lists and improve it like you did in admin -> relays with
searchable fields". The previous pass only RESTYLED the four textareas (rate-limit exempt accounts, Concord
rooms, topics to rotate, trusted media hosts). This drives the shipped bot editor -- bots.html, the admin
stylesheets, admin-bots.js and admin-list-fields.js -- in a real Chrome, against a server whose
/api/admin/list-field/* calls the REAL relay_lists rules, and checks what a person sees and does:

  * opening a bot fills every list as ROWS (accounts with their name; topics split on lines AND commas,
    as the bot reads them);
  * a Concord room row never shows its key until Reveal, and hides it again on Hide;
  * the search box narrows a long list;
  * Enter in an Add box adds (and submits nothing); a bad entry is refused with a reason and changes
    nothing; Remove takes one row and leaves the rest -- and each edit lands in the textarea the bot's
    Save reads;
  * nothing overflows a 390px phone, every Remove button is on screen.
"""
import asyncio
import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from app.services import relay_blocklist, relay_lists
from app.services.nostr import bip340, nostr_service
from tests.client.test_effects_full_app import Browser

ROOT = Path(__file__).resolve().parents[2]
CHROME = "/opt/google/chrome/chrome"
PKS = [bip340.pubkey_from_seckey(bytes([i]) * 32).hex() for i in (3, 4)]
NPUBS = [nostr_service.npub_of(p) for p in PKS]
NAMES = {PKS[0]: "Operator Alice", PKS[1]: "Bob The Moderator With A Long Name"}
CONFIG = {
    "nostr_rate_exempt": NPUBS[0] + "\n" + NPUBS[1],
    "concord_invite": "https://poster.place/c/naddr1first#SECRETKEYONE, https://poster.place/c/naddr1second#SECRETKEYTWO",
    "auto_post_topics": "gaming, comics\nmedia double standards\nretro tech\nanime, synthwave\nopen source",
    "trusted_media_hosts": "nas.lan 192.168.0.85",
}
FIELDS = {"exempt": "bot_f_nostr_rate_exempt", "invite": "bot_f_concord_invite",
          "topics": "bot_f_auto_post_topics", "hosts": "bot_f_trusted_media_hosts"}


def _page():
    html = (ROOT / "templates/admin/tabs/bots.html").read_text()
    css = (ROOT / "static/css/admin-tabs.css").read_text() + (ROOT / "static/css/admin-theme.css").read_text()
    js = "".join("<script>" + (ROOT / f).read_text().replace("</script>", "<\\/script>") + "</script>"
                 for f in ("static/js/admin-bots.js", "static/js/admin-list-fields.js"))
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><style>" + css
            + "</style><body class=admin-page><form id=wrap onsubmit='window.__submits++;return false'>"
            "<div class=admin-page>" + html + "</div></form>"
            "<script>window.__errors=[];window.__submits=0;onerror=m=>__errors.push(String(m));"
            "window.pcAlert=()=>{};window.pcConfirm=async()=>true;"
            # admin-bots.js loads the bot list on boot; this page is only the editor.
            "const _f=window.fetch;window.fetch=(u,o)=>String(u).startsWith('/api/admin/list-field/')?_f(u,o)"
            ":Promise.resolve(new Response('{}',{status:200,headers:{'Content-Type':'application/json'}}));"
            "</script>" + js)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, _page(), "text/html; charset=utf-8")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        kind = body.get("kind")
        if kind not in relay_lists.FIELD_KINDS:
            return self._send(400, {"detail": "not a list kind"})
        if self.path.endswith("/rows"):
            return self._send(200, asyncio.run(relay_lists.rows_of(kind, body.get("raw") or "")))
        new, err = relay_lists.edit(body.get("raw") or "", kind, add=body.get("add") or "", remove=body.get("remove") or "")
        return self._send(400, {"detail": err}) if err else self._send(200, {"value": new})


STATE = r"""(()=>{const out={};
  for(const [k,id] of Object.entries(%s)){
    const ta=document.getElementById(id),p=document.querySelector('.lf-panel[data-for="'+id+'"]');
    if(!p){out[k]=null;continue;}
    const rows=[...p.querySelectorAll('.rl-row')];
    out[k]={rows:rows.map(r=>r.querySelector('.blk-name').textContent.trim()),
            searchShown:!p.querySelector('.lf-search').hidden,
            msg:p.querySelector('.lf-msg').textContent,value:ta.value,
            removeOnScreen:rows.every(r=>{const b=r.querySelector('.lf-remove').getBoundingClientRect();
              return b.width>0&&b.right<=innerWidth+1&&b.left>=0;})};
  }
  out.pageOverflow=document.documentElement.scrollWidth>innerWidth+1;
  out.errors=__errors;out.submits=__submits;return out;})()""" % json.dumps(FIELDS)


async def _settle(b, ms=700):
    await asyncio.sleep(ms / 1000)


async def _run(base, width):
    with tempfile.TemporaryDirectory(prefix="pc-botlf-") as profile:
        proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0",
                                 "--user-data-dir=" + profile, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(150):
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
                await b.call("Page.navigate", {"url": base})
                await _settle(b, 800)
                got = {}
                await b.js("_bots[7]={id:7,name:'listbot',bot_type:'text',platform:'nostr',host:'',modes:'',config:%s};"
                           "openBotModal(7);"
                           # every per-platform group is opened so each list is on screen to be measured
                           "for(const id of %s)for(let a=document.getElementById(id);a;a=a.parentElement)"
                           "if(getComputedStyle(a).display==='none')a.style.display='block';"
                           % (json.dumps(CONFIG), json.dumps(list(FIELDS.values()))))
                await _settle(b)
                got["opened"] = await b.js(STATE)
                await b.js("toggleBotConcord()")
                await _settle(b, 100)
                got["revealed"] = (await b.js(STATE))["invite"]
                await b.js("toggleBotConcord()")
                await _settle(b, 100)
                got["hidden_again"] = (await b.js(STATE))["invite"]
                # search the topics
                await b.js("(()=>{const s=document.querySelector('.lf-panel[data-for=bot_f_auto_post_topics] .lf-search');"
                           "if(!s)return;s.value='COMIC';s.dispatchEvent(new Event('input',{bubbles:true}));})()")
                await _settle(b, 100)
                got["searched"] = (await b.js(STATE))["topics"]
                await b.js("(()=>{const s=document.querySelector('.lf-panel[data-for=bot_f_auto_post_topics] .lf-search');"
                           "if(!s)return;s.value='';s.dispatchEvent(new Event('input',{bubbles:true}));})()")
                # a bad host, by clicking Add
                await b.js("(()=>{const p=document.querySelector('.lf-panel[data-for=bot_f_trusted_media_hosts]');"
                           "if(!p)return;p.querySelector('.lf-add-input').value='not a host!';p.querySelector('.lf-add-btn').click();})()")
                await _settle(b)
                got["bad_host"] = (await b.js(STATE))["hosts"]
                # a good host, by pressing Enter in the Add box
                await b.js("(()=>{const i=document.querySelector('.lf-panel[data-for=bot_f_trusted_media_hosts] .lf-add-input');"
                           "if(i){i.value='media.lan';i.focus();}})()")
                await b.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                                                        "windowsVirtualKeyCode": 13})
                await b.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                                        "windowsVirtualKeyCode": 13})
                await _settle(b)
                got["added_host"] = (await b.js(STATE))["hosts"]
                # Remove one topic
                await b.js("[...document.querySelectorAll('.lf-panel[data-for=bot_f_auto_post_topics] .rl-row')]"
                           ".find(r=>r.textContent.includes('comics'))?.querySelector('.lf-remove').click()")
                await _settle(b)
                got["removed_topic"] = await b.js(STATE)
                return got
        finally:
            proc.kill()


@pytest.fixture(scope="module")
def server():
    async def profiles(pks):
        return {pk: {"name": NAMES.get(pk, "")} for pk in pks}, True
    real = relay_blocklist.profiles
    relay_blocklist.profiles = profiles
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d/" % srv.server_address[1]
    finally:
        srv.shutdown()
        relay_blocklist.profiles = real


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_every_bot_list_is_a_searchable_list_like_the_relay_tab(server, width):
    got = asyncio.run(_run(server, width))
    o = got["opened"]
    for k in FIELDS:
        assert o[k] is not None, (k, "still a bare textarea: no list was drawn")
        assert o[k]["removeOnScreen"], (k, width)
    assert o["exempt"]["rows"] == ["Operator Alice", "Bob The Moderator With A Long Name"], o["exempt"]
    assert o["topics"]["rows"] == ["gaming", "comics", "media double standards", "retro tech", "anime",
                                   "synthwave", "open source"], o["topics"]
    assert o["hosts"]["rows"] == ["nas.lan", "192.168.0.85"], o["hosts"]
    assert len(o["invite"]["rows"]) == 2 and not any("SECRETKEY" in r for r in o["invite"]["rows"]), o["invite"]
    assert all("naddr1" in r for r in o["invite"]["rows"]), "a room row must still say WHICH room"
    assert any("SECRETKEYONE" in r for r in got["revealed"]["rows"]), got["revealed"]
    assert not any("SECRETKEY" in r for r in got["hidden_again"]["rows"]), got["hidden_again"]
    assert o["topics"]["searchShown"] and got["searched"]["rows"] == ["comics"], got["searched"]
    bad = got["bad_host"]
    assert bad["rows"] == ["nas.lan", "192.168.0.85"] and "Could not add" in bad["msg"], bad
    assert bad["value"] == CONFIG["trusted_media_hosts"], "a refused entry changed what Save sends"
    add = got["added_host"]
    assert add["rows"] == ["nas.lan", "192.168.0.85", "media.lan"], add
    assert "media.lan" in add["value"].split(), "the added host is not in what the bot's Save reads"
    end = got["removed_topic"]
    assert "comics" not in end["topics"]["rows"] and len(end["topics"]["rows"]) == 6, end["topics"]
    assert "comics" not in end["topics"]["value"] and "gaming" in end["topics"]["value"]
    assert end["submits"] == 0, "Enter in an Add box submitted the form"
    assert not end["pageOverflow"], width
    assert not end["errors"], end["errors"]
