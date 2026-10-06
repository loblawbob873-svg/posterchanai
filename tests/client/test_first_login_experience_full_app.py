"""First login: choose Social or Full Experience; logged out shows the simple social sidebar.

"if a user logs in for the first time, give them a nice splash page that lets them choose Social or Full
Experience. The Social experience hides all the non-social features ... (updates their settings ->
Sidebar)" / "the logged out page should show the social simple features" / "maybe every user should get
that option once, may help existing users" -- an account that already shaped its sidebar is asked too,
with a third answer that keeps it. Drives the SHIPPED client with a relay that answers (or does
not answer) the account's pcai:client-prefs document, and reads the real sidebar.
"""
import asyncio
import json
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full

SOCKET = r'''
window.__published=[]; window.__prefsMode='none';   // none | configured | silent | oldsocial | migrated
// An account that chose Social BEFORE 2026-10-06 (the old set hid the wallet and every game) and also
// switched Communities off itself; and the same account once the migration has been recorded.
window.__OLD_SOCIAL={experience:'social',navHidden:['mail','notes','wallet','group:games','chess','holdem','xdc','concord']};
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;__sockets.push(this);setTimeout(()=>{this.readyState=1;this.fire('open',{});},8);}
 fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}if(!Array.isArray(m))return;
  if(m[0]==='REQ'){const sub=m[1],fs=m.slice(2);
   const prefs=fs.some(f=>(f['#d']||[]).includes('pcai:client-prefs'));
   if(prefs && __prefsMode==='silent') return;                       // the relays never answer
   if(prefs && __prefsMode==='configured'){
     const me=NostrTools.getPublicKey(new Uint8Array(32).fill(1));
     const ev=NostrTools.finalizeEvent({kind:30078,created_at:Math.floor(Date.now()/1000)-60,tags:[['d','pcai:client-prefs']],
       content:JSON.stringify({navHidden:['chess']})},new Uint8Array(32).fill(1));
     this.fire('message',['EVENT',sub,ev]);
   }
   if(prefs && (__prefsMode==='oldsocial'||__prefsMode==='migrated')){
     const doc=__prefsMode==='oldsocial'?__OLD_SOCIAL:{...__OLD_SOCIAL,navHidden:['mail','notes','texts','wallet'],experienceRev:2};
     const ev=NostrTools.finalizeEvent({kind:30078,created_at:Math.floor(Date.now()/1000)-60,tags:[['d','pcai:client-prefs']],
       content:JSON.stringify(doc)},new Uint8Array(32).fill(1));
     this.fire('message',['EVENT',sub,ev]);
   }
   setTimeout(()=>this.fire('message',['EOSE',sub]),12);}
  if(m[0]==='EVENT'){__published.push(m[1]);setTimeout(()=>this.fire('message',['OK',m[1].id,true,'']),12);}}
 close(){if(this.readyState===3)return;this.readyState=3;this.fire('close',{});}
}
window.WebSocket=FixtureSocket;
window.__navOff=k=>{const b=document.querySelector('.sidebar .nav .nav-item[data-view="'+k+'"]');
  return b ? (b.classList.contains('nav-off') || !!(b.closest('.nav-group')&&b.closest('.nav-group').classList.contains('nav-off'))) : null;};
'''
INIT = full.INIT.replace(full.FIRST_RUN_DONE, "").split("class FixtureSocket")[0] + SOCKET


async def run(mode, choose=None, width=1280):
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-exp-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 f"--window-size={width},900", "--remote-debugging-port=0", "--user-data-dir=" + profile,
                                 "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            wsurl = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(wsurl, max_size=20_000_000) as ws:
                b = full.Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": INIT + f"\nwindow.__prefsMode={json.dumps(mode)};"})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await asyncio.sleep(.6)
                out = {"guest": await b.js("({mail:__navOff('mail'), notes:__navOff('notes'), notif:__navOff('notifications'), messages:__navOff('messages'),"
                                           " stored:localStorage.getItem('navHidden')||localStorage.getItem('pc_navHidden')||''})")}
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                           "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                await asyncio.sleep(3.5)                          # the prefs read retries before it gives up
                out["splash"] = await b.js("!!document.getElementById('pc-experience')")
                out["layout"] = await b.js("""(()=>{const c=[...document.querySelectorAll('#pc-experience .pc-exp-card')].map(e=>e.getBoundingClientRect());
                    return {w:innerWidth, n:c.length, right:Math.max(0,...c.map(r=>r.right)), left:Math.min(9999,...c.map(r=>r.left)),
                            stacked:c.length===2 && c[1].top>=c[0].bottom-1, scroll:document.documentElement.scrollWidth};})()""")
                out["keep_offered"] = await b.js("!!document.querySelector('#pc-experience [data-exp=\"keep\"]')")
                out["after_login"] = await b.js("""({mail:__navOff('mail'), chess:__navOff('chess'), holdem:__navOff('holdem'),
                    xdc:__navOff('xdc'), wallet:__navOff('wallet'), texts:__navOff('texts'), concord:__navOff('concord'), notes:__navOff('notes'),
                    prefs:__published.filter(e=>e.kind===30078&&(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs')).map(e=>JSON.parse(e.content))})""")
                if choose and out["splash"]:
                    await b.js(f"document.querySelector('#pc-experience [data-exp=\"{choose}\"]').click(); true")
                    await b.until("__published.some(e=>e.kind===30078&&(e.tags||[]).some(t=>t[1]==='pcai:client-prefs')&&/experience/.test(e.content))")
                    out["chosen"] = await b.js("""({gone:!document.getElementById('pc-experience'),
                        mail:__navOff('mail'), notes:__navOff('notes'), notif:__navOff('notifications'), messages:__navOff('messages'),
                        settings:__navOff('settings'), concord:__navOff('concord'), chess:__navOff('chess'),
                        wallet:__navOff('wallet'), texts:__navOff('texts'), xdc:__navOff('xdc'), holdem:__navOff('holdem'),
                        prefs:__published.filter(e=>e.kind===30078&&(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs')).map(e=>JSON.parse(e.content))})""")
                return out
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=5)
            server.shutdown()


CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@CHROME
def test_logged_out_shows_the_simple_social_sidebar_without_storing_it():
    o = asyncio.run(run("none"))
    g = o["guest"]
    assert g["mail"] is True and g["notes"] is True, ("logged out still shows the full app", g)
    assert g["notif"] is False and g["messages"] is False, ("logged out hid the social features", g)
    assert "mail" not in g["stored"], ("the logged-out sidebar was SAVED -- the next account would inherit it", g)
    assert o["after_login"]["mail"] is False, ("signing in kept the logged-out sidebar", o)


@CHROME
def test_a_first_login_chooses_social_and_it_is_saved_to_the_account():
    o = asyncio.run(run("none", choose="social"))
    assert o["splash"], o
    c = o["chosen"]
    assert c["gone"] and c["mail"] is True and c["notes"] is True, ("Social did not hide the rest", c)
    assert c["notif"] is False and c["messages"] is False and c["concord"] is False and c["settings"] is False, c
    # The owner's split (2026-10-06): "Monero wallet, Social, Games should be part of the Social features.
    # Texts should be part of the Full Experience" -- every game in the group, not just the first.
    assert c["wallet"] is False and c["chess"] is False and c["holdem"] is False and c["xdc"] is False, \
        ("Social hid the wallet or a game", c)
    assert c["texts"] is True, ("Social still shows Texts, a Full Experience app", c)
    assert o["keep_offered"] is False, ("a fresh account was offered to keep a sidebar it never shaped", o)
    # ONE document carrying both: two read-modify-writes let the second republish without the list.
    assert all("experience" in d and "navHidden" in d for d in c["prefs"]), ("a save carried only half the choice", c["prefs"])
    last = c["prefs"][-1] if c["prefs"] else {}
    assert last.get("experience") == "social" and "mail" in (last.get("navHidden") or []), ("not saved to the account", c["prefs"])


@CHROME
def test_full_experience_keeps_everything():
    o = asyncio.run(run("none", choose="full"))
    c = o["chosen"]
    assert c["gone"] and c["mail"] is False and c["notes"] is False, c
    last = c["prefs"][-1] if c["prefs"] else {}
    assert last.get("experience") == "full" and last.get("navHidden") == [], c["prefs"]


@CHROME
def test_an_account_with_its_own_sidebar_is_asked_once_and_can_keep_it():
    o = asyncio.run(run("configured", choose="keep"))
    assert o["splash"] and o["keep_offered"], ("an existing account was not offered to keep its sidebar", o)
    assert o["after_login"]["chess"] is True, ("the account's own sidebar was not applied", o)
    c = o["chosen"]
    assert c["gone"] and c["chess"] is True and c["mail"] is False, ("Keep changed the sidebar", c)
    last = c["prefs"][-1] if c["prefs"] else {}
    # The save merges into the account's document, so Keep carries ITS list through unchanged.
    assert last.get("experience") == "keep" and last.get("navHidden") == ["chess"], ("Keep changed the saved sidebar", c["prefs"])


@CHROME
def test_no_answer_from_the_relays_never_shows_it():
    o = asyncio.run(run("silent"))
    assert o["splash"] is False, ("a relay that never answered was read as 'nothing configured'", o)


@CHROME
def test_on_a_phone_the_cards_stack_and_fit():
    o = asyncio.run(run("none", choose="social", width=390))
    l = o["layout"]
    assert o["splash"] and l["n"] == 2 and l["stacked"], ("the choices do not stack on a phone", l)
    assert l["left"] >= 0 and l["right"] <= l["w"] and l["scroll"] <= l["w"], ("the splash runs off a phone screen", l)
    assert o["chosen"]["gone"], o


@CHROME
def test_an_account_that_chose_social_before_the_split_is_moved_once():
    """The owner said yes to updating existing Social accounts: the wallet and Games join them, Texts
    leaves, and a switch the person flipped themselves (Communities off) is left alone."""
    o = asyncio.run(run("oldsocial"))
    a = o["after_login"]
    assert o["splash"] is False, ("an account that already chose was asked again", o)
    assert a["wallet"] is False and a["chess"] is False and a["holdem"] is False and a["xdc"] is False, \
        ("an old Social account still hides the wallet or a game", a)
    assert a["texts"] is True, ("an old Social account still shows Texts", a)
    assert a["mail"] is True and a["notes"] is True and a["concord"] is True, ("the migration undid the person's own choices", a)
    assert len(a["prefs"]) == 1, ("the migration must be saved exactly once", a["prefs"])
    saved = a["prefs"][0]
    assert saved.get("experienceRev") == 2 and saved.get("experience") == "social", saved
    assert "texts" in saved["navHidden"] and "wallet" not in saved["navHidden"] and "concord" in saved["navHidden"], saved


@CHROME
def test_an_account_already_moved_is_left_exactly_as_it_is():
    """Once recorded, the person's later choices win: they hid the wallet again, and it stays hidden."""
    o = asyncio.run(run("migrated"))
    a = o["after_login"]
    assert a["wallet"] is True and a["texts"] is True, a
    assert a["prefs"] == [], ("an already-migrated account was written to again", a["prefs"])
