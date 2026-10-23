"""EVERY synced client preference persists and hydrates -- and a change made during a bad moment still syncs.

Operator: "you always screw up hydration and setting persistence". The account's client preferences
are ONE replaceable kind-30078 document (`d=pcai:client-prefs`, static/js/client/blossom.js). Each key
needs two hand-written halves -- a save (`saveClientPrefsNostr({key: ...})`) and a clause in
`restoreClientPrefsNostr` (or the restore's callers in app.js) -- and the failure is always silent:
a key with no restore clause turns itself off on every other device; a save that could not read the
document, or whose publish was refused, used to be dropped for good because each save only ever
sent its own patch.

Driven through the SHIPPED client in headless Chrome (real login, real signing, the real Settings
screen, sidebar editor, first-login splash, tip sheets, Concord theme API). Only the relay socket and
the instance HTTP are fixtures: an in-page relay that STORES what it accepts (replaceable by d-tag),
answers REQs from its store, and can be told to stop answering the prefs read (`read: down` -- a
REQ that never gets EOSE, i.e. "could not ask") or to refuse the prefs publish (`accept: false`).
Its store lives in localStorage, so it survives a reload of the same "device", and device B is a
FRESH browser profile seeded with exactly what device A's relay accepted.

* `test_every_saved_key_is_restored_and_every_restored_key_is_saved` -- static: the key lists are
  read from the source at run time, so a new pref is covered (or this fails) automatically.
* `test_every_synced_pref_reaches_a_second_device` -- device A sets EVERY key through its real setter,
  device B comes up with every value applied (what the app reads, plus the visible UI).
* the failure paths: read down at save time / publish refused / an older remote copy arriving after
  a local unsent change / two saves at once / an account switch.

PC_PREFS_APP_ROOT serves another checkout (used to show the failure-path tests failing without the
pending-prefs change).
"""
import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full

ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(os.environ.get("PC_PREFS_APP_ROOT", ROOT))
CHROME = Path("/opt/google/chrome/chrome")
needs_chrome = pytest.mark.skipif(not CHROME.exists(), reason="Chrome required")

KEY_A = 1          # NostrTools key bytes: device A / B (same account)
KEY_Y = 3          # a second account on the same device

# full.INIT minus its fixture socket. `FIRST_RUN_DONE` (the "experience already chosen" seed) is kept
# by default and stripped for the sweep, which drives the first-login splash itself.
BASE = full.INIT.split("class FixtureSocket")[0]
BASE_NO_FIRST_RUN = BASE.replace(full.FIRST_RUN_DONE, "")

XMR = "48TAuDzhzyRCLBAP7v86GGBLrTB2PNyYn1y7DL86H7sj2rRt7mipXRN2vigRChENnZWFDHRzuPoU9WvUxSy3AecgGDaj8Vb"
BCH = "bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a"

RELAY = r"""
(()=>{
 const SK='__fxRelay', MK='__fxMode';
 const load=()=>{try{const v=JSON.parse(localStorage.getItem(SK)||'null');return Array.isArray(v)?v:null;}catch(_){return null;}};
 if(!load()) localStorage.setItem(SK, JSON.stringify(window.__fxSeed||[]));
 const fx=window.__fx={published:[],dropped:0,refused:0,refusedOk:0,accepted:0,reads:0,restored:0,
   mode:()=>{try{return Object.assign({read:'up',accept:true},JSON.parse(localStorage.getItem(MK)||'{}'));}catch(_){return {read:'up',accept:true};}},
   set:m=>localStorage.setItem(MK,JSON.stringify(Object.assign(fx.mode(),m))),
   events:()=>load()||[],
   dtag:ev=>((ev.tags||[]).find(t=>t[0]==='d')||[])[1],
   isPrefs:ev=>ev&&ev.kind===30078&&fx.dtag(ev)==='pcai:client-prefs',
   prefs:pk=>fx.events().filter(e=>fx.isPrefs(e)&&(!pk||e.pubkey===pk)).sort((a,b)=>b.created_at-a.created_at)[0]||null,
   prefsDoc:pk=>{const e=fx.prefs(pk);return e?JSON.parse(e.content):null;}};
 const store=ev=>{const evs=fx.events();
   const repl=ev.kind===0||ev.kind===3||(ev.kind>=10000&&ev.kind<20000)||(ev.kind>=30000&&ev.kind<40000);
   const same=e=>e.pubkey===ev.pubkey&&e.kind===ev.kind&&(ev.kind<30000||fx.dtag(e)===fx.dtag(ev));
   const old=repl?evs.find(same):null;
   if(old&&(old.created_at>ev.created_at||(old.created_at===ev.created_at&&old.id<ev.id)))return;
   localStorage.setItem(SK,JSON.stringify(evs.filter(e=>!(repl&&same(e))&&e.id!==ev.id).concat([ev])));};
 fx.put=store;
 // A stranger with a timeline and BOTH tip addresses in their profile (the tip sheets' real path).
 let stranger=null;
 const strangerEvents=()=>{ if(stranger||!window.NostrTools)return stranger||[];
   const sk=new Uint8Array(32).fill(7),now=Math.floor(Date.now()/1000);
   stranger=[NostrTools.finalizeEvent({kind:0,created_at:now-5000,tags:[],content:JSON.stringify({name:'Tipper',monero_address:'XMR_ADDR',bch:'BCH_ADDR'})},sk)];
   for(let i=0;i<8;i++)stranger.push(NostrTools.finalizeEvent({kind:1,created_at:now-60*i,tags:[],content:'fixture post '+i+' with enough text to give the card some height'},sk));
   window.__strangerPk=stranger[0].pubkey; return stranger; };
 const match=(f,ev)=>{
   if(f.kinds&&!f.kinds.includes(ev.kind))return false;
   if(f.authors&&!f.authors.includes(ev.pubkey))return false;
   if(f.ids&&!f.ids.includes(ev.id))return false;
   if(f.since&&ev.created_at<f.since)return false;
   if(f.until&&ev.created_at>f.until)return false;
   for(const k of Object.keys(f))if(k[0]==='#'){const v=f[k];if(!(ev.tags||[]).some(t=>t[0]===k.slice(1)&&v.includes(t[1])))return false;}
   return true;};
 class FixtureSocket extends EventTarget{
  static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
  constructor(url){super();this.url=String(url);this.readyState=0;(window.__sockets=window.__sockets||[]).push(this);
   setTimeout(()=>{this.readyState=1;this.fire('open',{});},8);}
  fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e);}
  send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}if(!Array.isArray(m))return;
   if(m[0]==='REQ'){const id=m[1],fl=m.slice(2);
    if(fl.some(f=>(f['#d']||[]).includes('pcai:client-prefs'))&&fx.mode().read==='down'){fx.dropped++;(fx.droppedIds=fx.droppedIds||new Set()).add(id);return;}   // "could not ask": no EOSE, ever
    if(fl.some(f=>(f['#d']||[]).includes('pcai:client-prefs')))fx.reads=(fx.reads||0)+1;
    const all=strangerEvents().concat(fx.events());
    setTimeout(()=>{const out=[];
      for(const f of fl){let hits=all.filter(ev=>match(f,ev)).sort((a,b)=>b.created_at-a.created_at);if(f.limit)hits=hits.slice(0,f.limit);
        for(const ev of hits)if(!out.includes(ev))out.push(ev);}
      for(const ev of out)this.fire('message',['EVENT',id,ev]);this.fire('message',['EOSE',id]);},10);}
   if(m[0]==='EVENT'){const ev=m[1];fx.published.push(ev);let ok=true,msg='';
    if(fx.isPrefs(ev)&&!fx.mode().accept){ok=false;msg='blocked: fixture refuses';fx.refused++;}
    else{store(ev);if(fx.isPrefs(ev))fx.accepted++;}
    setTimeout(()=>{this.fire('message',['OK',ev.id,ok,msg]);if(!ok)fx.refusedOk++;},12);}
   if(m[0]==='CLOSE'){if(fx.droppedIds&&fx.droppedIds.delete(m[1]))fx.droppedClosed=(fx.droppedClosed||0)+1;return;}}
  close(){if(this.readyState===3)return;this.readyState=3;this.fire('close',{});}
 }
 window.WebSocket=FixtureSocket;
 // When the account's prefs restore has FINISHED (read, applied, sent anything pending): the module's own
 // restoreClientPrefsNostr, wrapped as the lazy module is built, so the gate waits on the event, not a guess.
 let factory;
 Object.defineProperty(window,'PCBlossomFactory',{configurable:true,get:()=>factory,set(f){
   factory=function(){const m=f.apply(this,arguments),restore=m&&m.restoreClientPrefsNostr;
     if(typeof restore==='function')m.restoreClientPrefsNostr=function(){return Promise.resolve(restore.apply(this,arguments)).finally(()=>{fx.restored++;});};
     return m;};}});
})();
""".replace("XMR_ADDR", XMR).replace("BCH_ADDR", BCH)


# ----------------------------------------------------------------------------------- source scan

def _src(name):
    return (APP_ROOT / "static/js/client" / name).read_text()


def _call_args(text, fn):
    """Every argument text of `fn(...)`, balanced over (), {}, []."""
    out = []
    for m in re.finditer(r"\b" + re.escape(fn) + r"\s*\(", text):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            depth += {"(": 1, "{": 1, "[": 1, ")": -1, "}": -1, "]": -1}.get(text[i], 0)
            i += 1
        out.append((m.start(), text[m.end():i - 1]))
    return out


def _top_level_keys(arg):
    """Keys of the patch object(s): inside exactly ONE `{` (calls/ternaries around it allowed, nested
    object values -- a pref whose value is an object -- not descended into)."""
    keys, stack = set(), []
    for m in re.finditer(r"[{}()\[\]]|\b([A-Za-z_]\w*)\s*:(?!:)", arg):
        tok = m.group(0)
        if tok in "{([":
            stack.append(tok)
        elif tok in "})]":
            stack.pop()
        elif m.group(1) and stack and stack[-1] == "{" and stack.count("{") == 1:
            keys.add(m.group(1))
    return keys


def saved_keys():
    """Every key any client module hands to saveClientPrefsNostr (and the notification path's own key)."""
    keys = set()
    for path in sorted((APP_ROOT / "static/js/client").glob("*.js")):
        text = path.read_text()
        for at, arg in _call_args(text, "saveClientPrefsNostr"):
            if text[max(0, at - 9):at].endswith("function "):
                continue
            arg = arg.strip()
            if re.fullmatch(r"[A-Za-z_]\w*", arg):
                # A named patch: its literal and every `name.key =` in the enclosing 60 lines.
                body = text[max(0, at - 4000):at]
                lit = list(re.finditer(r"\b(?:const|let)\s+" + arg + r"\s*=\s*", body))
                assert lit, f"{path.name}: saveClientPrefsNostr({arg}) -- cannot find what {arg} holds"
                start = lit[-1].end()
                keys |= _top_level_keys(body[start:start + 600].split(";")[0])
                keys |= set(re.findall(r"\b" + arg + r"\.(\w+)\s*=(?!=)", body[start:]))
            else:
                keys |= _top_level_keys(arg)
    if "notificationPrefs:merged" in _src("blossom.js").replace(" ", ""):
        keys.add("notificationPrefs")
    return keys


def _function_body(text, name):
    m = re.search(r"function\s+" + name + r"\s*\(", text)
    assert m, name
    i = text.index("{", m.end())
    depth, j = 0, i
    while True:
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        j += 1
        if not depth:
            return text[i:j]


def restored_keys():
    """Every key the restore adopts, and every key its callers (the first-login choice) read off `pr`."""
    keys = set(re.findall(r"\bpr\.(\w+)", _function_body(_src("blossom.js"), "restoreClientPrefsNostr")))
    app = _src("app.js")
    for fn in ("_migrateSocialSet", "_maybeExperienceSplash", "_sidebarConfigured"):
        keys |= set(re.findall(r"\bpr\.(\w+)", _function_body(app, fn)))
    keys.discard("_pcPrefsCreatedAt")
    return keys


def test_every_saved_key_is_restored_and_every_restored_key_is_saved():
    saved, restored = saved_keys(), restored_keys()
    # The scan must SEE the keys the operator listed, or it would pass vacuously.
    for k in ("concordTheme", "navHidden", "experience", "startupAppsAt", "xmrTip", "zapPresets", "navGroupOf"):
        assert k in saved, (k, sorted(saved))
    assert not saved - restored, ("saved to the account but never read back -- it turns itself off on every "
                                  "other device", sorted(saved - restored))
    assert not restored - saved, ("read back but nothing ever saves it", sorted(restored - saved))


# ----------------------------------------------------------------------------------- the browser

# Order-insensitive JSON of a value (an object's keys sorted), for comparing what two writers built.
CANON = ("(v=>JSON.stringify(v,(k,x)=>x&&typeof x==='object'&&!Array.isArray(x)"
         "?Object.keys(x).sort().reduce((o,k)=>(o[k]=x[k],o),{}):x))")


async def wait(b, expr, timeout=60.0, what=None):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            if await b.js(expr):
                return
        except AssertionError:
            pass
        await asyncio.sleep(.2)
    raise AssertionError({"waiting": what or expr, "state": await b.js(
        "({errors:(window.__errors||[]).slice(-5),pending:Object.fromEntries(Object.keys(localStorage)"
        ".filter(k=>k.startsWith('pc.prefs.pending.')).map(k=>[k,localStorage.getItem(k)])),"
        "mode:window.__fx&&__fx.mode(),dropped:window.__fx&&__fx.dropped,refused:window.__fx&&__fx.refused,"
        "doc:window.__fx&&__fx.prefsDoc()})")})


async def settle(b, expr, timeout):
    """Poll an expression answering a LIST of what is still wrong; returns the last list (empty = settled)."""
    loop = asyncio.get_running_loop()
    end, left = loop.time() + timeout, None
    while loop.time() < end:
        left = await b.js(expr)
        if not left:
            return left
        await asyncio.sleep(.3)
    return left


async def device(act, seed=(), key=KEY_A, base=BASE, mode=None):
    """One "device": a fresh Chrome profile, signed in with `key`, its relay seeded with `seed`."""
    full.ROOT = APP_ROOT
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    init = base + "window.__fxSeed=" + json.dumps(list(seed)) + ";" + (
        "if(!localStorage.getItem('__fxMode'))localStorage.setItem('__fxMode'," + json.dumps(json.dumps(mode)) + ");"
        if mode else "") + RELAY
    with tempfile.TemporaryDirectory(prefix="pc-prefs-", ignore_cleanup_errors=True) as prof:
        p = subprocess.Popen([str(CHROME), "--headless=new", "--no-sandbox", "--disable-gpu",
                              "--remote-debugging-port=0", "--user-data-dir=" + prof, "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(300):
                if Path(prof, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(prof, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                for _ in range(100):
                    try:
                        pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(.1)
            async with websockets.connect(next(x for x in pages if x["type"] == "page")["webSocketDebuggerUrl"],
                                          max_size=20_000_000) as ws:
                b = full.Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride", dict(width=1440, height=900, deviceScaleFactor=1,
                                                                        mobile=False, screenWidth=1440, screenHeight=900))
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": init})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                b.origin = f"http://127.0.0.1:{server.server_port}"
                await login(b, key)
                return await act(b)
        finally:
            p.terminate()
            p.wait(timeout=10)
            server.shutdown()


async def login(b, key):
    await wait(b, '!!window.__PC && !!window.NostrTools && document.readyState==="complete"', 30)
    await wait(b, "document.body.classList.contains('guest')", 30)
    await b.js("(()=>{const key=new Uint8Array(32).fill(%d);document.querySelector('#nsec-input').value="
               "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()" % key)
    await wait(b, '!!__PC.me() && Relay.status==="ok"', 30)


async def restored(b):
    """The login (or reload) restore of the account's prefs has finished."""
    await wait(b, "__fx.restored>=1", 40, "the account's prefs restore to finish")


async def sent(b):
    """Nothing is left unsent: every recorded change was accepted in a published document."""
    await wait(b, "!localStorage.getItem('pc.prefs.pending.'+__PC.me().pubkey)", 30, "every pending change to be sent")


async def reloading(b, script):
    """Run `script` (which reloads the page) and wait until the OLD document is gone."""
    await b.js("window.__oldPage=true;" + script)
    await wait(b, "!window.__oldPage", 30, "the page to reload")


async def reload(b):
    await b.call("Page.reload")
    await wait(b, '!!window.__PC && !!window.NostrTools && document.readyState==="complete"', 30)
    await wait(b, '!!__PC.me() && Relay.status==="ok"', 30)


async def open_settings(b):
    await b.js("__PC.switchView('settings')")
    await wait(b, "!!document.getElementById('set-no-images') && !!document.getElementById('nav-hide-list')", 30)


async def toggle(b, el_id):
    """Flip a Settings switch the way a tap does; answers the new state."""
    return await b.js(f"(()=>{{const x=document.getElementById({json.dumps(el_id)});x.checked=!x.checked;"
                      "x.dispatchEvent(new Event('change',{bubbles:true}));return x.checked;})()")


def me_pk(b):
    return b.js("__PC.me().pubkey")


async def read_fails_once_more(b, before):
    """Wait until a client-prefs read has been tried and given up on (3 unanswered REQs + the last timeout)."""
    await wait(b, f"__fx.dropped>={before + 3}", 40, "the save's read to be tried three times")
    # The client gives up on the last one when it CLOSEs it; a flat 7s guessed at that (called 7 times per run).
    t0 = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t0 < 7:
        if await b.js("(__fx.droppedClosed||0)>=__fx.dropped"):
            break
        await asyncio.sleep(.2)
    await asyncio.sleep(.5)


# ----------------------------------------------------------------------------------- the sweep

# Settings switches and selects (id -> pref key, and how the app names it in ClientSettings).
SWITCHES = {"set-vim": "vimKeys", "set-no-images": "noImages", "set-hide-replies": "hideReplies",
            "set-read-aloud": "readAloudHold", "set-new-posts-pill": "newPostsPill",
            "set-auto-new-posts": "autoNewPosts", "set-clean-links": "cleanLinks", "set-post-effects": "postEffects",
            "set-right-panel": "rightPanel"}
SELECTS = {"set-start-timeline": "startTimeline", "set-start-view": "startView", "set-media-cache": "mediaCacheGB",
           "set-music-offline": "musicOfflineGB", "set-desktop-buddy-who": "desktopBuddy",
           "set-search-bar": "searchBar"}

# What the app on device B reads for each key (an expression evaluated there).
READ = {k: f"ClientSettings.get({json.dumps(k)})" for k in (
    "vimKeys", "noImages", "hideReplies", "readAloudHold", "newPostsPill", "autoNewPosts", "cleanLinks",
    "postEffects", "startTimeline", "startView", "mediaCacheGB", "musicOfflineGB", "desktopBuddy", "navHidden",
    "navOrder", "tlHidden", "mobileNav", "navGroupOf", "navUserGroups", "startupApps", "startupAppsAt",
    "zapPresets", "xmrPresets", "bchPresets", "experience", "experienceRev", "searchBar", "searchBarAt")}
READ.update({
    "xmrTip": "ClientSettings.get('xmrLastAmt')",
    "bchTip": "ClientSettings.get('bchLastAmt')",
    "rightPanel": "!PCRightPanel.hidden()",
    "concordTheme": "(()=>{const t=PCConcordTheme.get();return {theme:t.theme,accent:t.accent,bg:t.bg,at:t.at};})()",
    "appLook": "PCLook.get()",
})
# `experience` is not adopted into ClientSettings on another device -- the restore's caller reads it off
# the document to decide the first-login splash (asserted visibly below instead).
DOC_ONLY = {"experience"}
# notificationPrefs has its own per-key dirty set and clock (blossom.js _syncNotificationPreferences);
# tests/client/test_notification_settings_full_app.py and _runtime.py cover its sync.
OWN_SYNC = {"notificationPrefs"}


async def set_every_pref(b):
    """Device A: every synced key, each through the path a person uses (or the module API that path calls)."""
    # experience (+ experienceRev, navHidden): the first-login splash, on a definite empty read.
    await wait(b, "!!document.querySelector('#pc-experience [data-exp=social]')", 30, "the first-login splash")
    await b.js("document.querySelector('#pc-experience [data-exp=social]').click()")
    await open_settings(b)
    for el in SWITCHES:
        await toggle(b, el)
    for el in SELECTS:
        changed = await b.js(f"""(()=>{{const s=document.getElementById({json.dumps(el)});
            const o=[...s.options].map(o=>o.value).filter(v=>v!==s.value && v!=='');
            const want={json.dumps(el)}==='set-start-view' && o.includes('notifications') ? 'notifications' : o[o.length-1];
            if(!want) return false; s.value=want; s.dispatchEvent(new Event('change',{{bubbles:true}})); return true;}})()""")
        assert changed, f"{el} offers nothing to change to"
    # The sidebar editor (Settings → Sidebar): hide a row, hide a timeline tab, move a row, a phone slot.
    await b.js("""(()=>{const x=[...document.querySelectorAll('#nav-hide-list input[data-navkey]')].find(c=>c.checked&&!c.disabled);
        x.checked=false;x.dispatchEvent(new Event('change',{bubbles:true}));})()""")
    await b.js("""(()=>{const x=document.querySelector('#nav-hide-list input[data-tltab=trending]');
        x.checked=false;x.dispatchEvent(new Event('change',{bubbles:true}));})()""")
    await b.js("document.querySelector('#nav-hide-list [data-orddown]').click()")
    await b.js("""(()=>{const s=document.querySelector('#mnav-slots select[data-mnavslot]');
        const o=[...s.options].map(o=>o.value).filter(v=>v&&v!==s.value);s.value=o[o.length-1];
        s.dispatchEvent(new Event('change',{bubbles:true}));})()""")
    # A new group (navUserGroups) and a row moved into it (navGroupOf), through the ▦ picker.
    await b.js("document.querySelector('#nav-hide-list [data-grpkey]').click()")
    await wait(b, "!!document.querySelector('.uipick[data-v=\"__new\"]')", 15)
    await b.js("document.querySelector('.uipick[data-v=\"__new\"]').click()")
    await wait(b, "!!document.querySelector('.uiprompt-in')", 15)
    await b.js("document.querySelector('.uiprompt-in').value='Fixture group';document.querySelector('.uiconfirm [data-uc=\"1\"]').click()")
    await wait(b, "(ClientSettings.get('navUserGroups')||[]).length===1 && Object.keys(ClientSettings.get('navGroupOf')||{}).length>0", 15)
    # Zaps & tips presets: edited, then the screen's Save.
    await b.js("""(()=>{for(const [id,v] of [['us-zap-presets','21, 42, 84'],['us-xmr-presets','0.01, 0.02'],['us-bch-presets','0.003, 0.03']]){
        const x=document.getElementById(id);x.value=v;x.dispatchEvent(new Event('input',{bubbles:true}));}
        document.getElementById('us-save').click();})()""")
    await wait(b, "ClientSettings.get('bchPresets')==='0.003, 0.03'", 15)
    # Apps opened at login: os.js's switch handler, verbatim (stamp, local copy, the account save).
    await b.js("(()=>{const list=['notes'],at=Date.now();ClientSettings.set('startupApps',list);"
               "ClientSettings.set('startupAppsAt',at);__PC.saveStartupApps(list,at);})()")
    # The Concord theme: the picker's own call.
    await b.js("PCConcordTheme.forOwner(__PC.me().pubkey);PCConcordTheme.set({theme:'oled',accent:'#ff8800'})")
    # Settings -> Appearance: its own module API (what the pane's inputs call).
    await b.js("PCLook.set({accent:'#11aa77',sec:{music:{bg:'#202040'}},wall:{url:'https://cdn.example/w.jpg',pos:'tile',size:'fill',dim:.4}})")
    # The two remembered tip amounts: a stranger's profile → the Monero and Bitcoin Cash tip sheets.
    await b.js("__PC.openProfile(window.__strangerPk)")
    await wait(b, "!!document.getElementById('prof-xmr') && !!document.getElementById('prof-bch')", 30, "the profile's tip buttons")
    await b.js("document.getElementById('prof-xmr').click()")
    await wait(b, "!!document.getElementById('xmr-sent')", 30, "the Monero tip sheet")
    await b.js("""(()=>{const a=document.getElementById('xmr-amt');a.value='0.0123';a.dispatchEvent(new Event('input',{bubbles:true}));
        document.getElementById('xmr-quiet-zap').checked=true;document.getElementById('xmr-sent').click();})()""")
    await wait(b, "ClientSettings.get('xmrLastAmt')==='0.0123' && !document.getElementById('xmr-sent')", 15)
    await b.js("document.getElementById('prof-bch').click()")
    await wait(b, "!!document.getElementById('bch-sent')", 30, "the Bitcoin Cash tip sheet")
    await b.js("""(()=>{const a=document.getElementById('bch-amt');a.value='0.0456';a.dispatchEvent(new Event('input',{bubbles:true}));
        document.getElementById('bch-sent').click();})()""")
    await wait(b, "ClientSettings.get('bchLastAmt')==='0.0456'", 15)


@needs_chrome
def test_every_synced_pref_reaches_a_second_device():
    saved = saved_keys() - OWN_SYNC
    untested = saved - set(READ)
    assert not untested, ("a pref is synced to the account but this test never sets it -- add its setter "
                          "to set_every_pref and how the app reads it to READ", sorted(untested))
    state = {}

    async def on_a(b):
        await set_every_pref(b)
        want = {k: await b.js(READ[k]) for k in saved}
        # The first-login choice writes `experienceRev` into the document only (app.js chooseExperience);
        # the device that made the choice has no local copy to compare -- it is the revision it shipped.
        want["experienceRev"] = int(re.search(r"const EXPERIENCE_REV = (\d+)", _src("app.js")).group(1))
        state["want"] = want
        # Every value lands in ONE accepted document, and nothing is left unsent.
        left = await settle(b, "(()=>{const d=__fx.prefsDoc(__PC.me().pubkey)||{};const w=" + json.dumps(want) + ";"
                            "const bad=Object.keys(w).filter(k=>" + CANON + "(d[k])!==" + CANON + "(w[k])).map(k=>[k,d[k],w[k]]);"
                            "if(localStorage.getItem('pc.prefs.pending.'+__PC.me().pubkey))bad.push(['<pending>',localStorage.getItem('pc.prefs.pending.'+__PC.me().pubkey)]);"
                            "return bad;})()", 90)
        assert not left, ("these never reached the account's published document as set", left)
        state["seed"] = [e for e in await b.js("__fx.events()") if e["kind"] == 30078]
        return await b.js("__fx.prefsDoc(__PC.me().pubkey)")

    doc = asyncio.run(device(on_a, base=BASE_NO_FIRST_RUN))
    want = state["want"]
    for k in saved:
        assert k in doc, (k, "never reached the account's document")
    # Sanity: the sweep really moved every value off its default.
    assert want["concordTheme"]["theme"] == "oled" and want["rightPanel"] is False and want["experience"] == "social"

    async def on_b(b):
        # The fresh device adopts every value (what the app reads)...
        expect = {k: v for k, v in want.items() if k not in DOC_ONLY}
        left = await settle(b, "(()=>{const w=" + json.dumps(expect) + ",R={" +
                            ",".join(f"{json.dumps(k)}:()=>{READ[k]}" for k in expect) + "};"
                            "return Object.keys(w).filter(k=>" + CANON + "(R[k]())!==" + CANON + "(w[k])).map(k=>[k,R[k](),w[k]]);})()", 60)
        assert not left, ("device B did not adopt these synced prefs [key, B has, A set]", left)
        got = {k: await b.js(READ[k]) for k in expect}
        # ...and SHOWS it: no first-login splash (the account chose), the Concord theme painted, the right
        # panel gone, the hidden sidebar row gone, and the Settings screen drawn from the synced values.
        # The splash is decided synchronously right after the restore resolves (app.js
        # _maybeExperienceSplash), so once the restore has finished a wrong splash is already on screen.
        await restored(b)
        VISIBLE = """({splash:!!document.getElementById('pc-experience'),
            theme:document.documentElement.dataset.ccTheme,
            accent:document.documentElement.style.getPropertyValue('--cct-accent').trim(),
            panel:document.querySelector('.rightbar').getClientRects().length>0,
            hiddenRowShown:(ClientSettings.get('navHidden')||[]).filter(k=>!k.startsWith('group:')).some(k=>{
              const e=document.querySelector('.sidebar .nav [data-v="'+k+'"]');return e&&e.getClientRects().length>0;})})"""
        want_visible = {"splash": False, "theme": "oled", "accent": "#ff8800", "panel": False, "hiddenRowShown": False}
        loop = asyncio.get_running_loop()
        end = loop.time() + 10
        while (visible := await b.js(VISIBLE)) != want_visible and loop.time() < end:
            await asyncio.sleep(.2)
        await open_settings(b)
        controls = await b.js("(()=>{const o={};for(const id of " + json.dumps(list(SWITCHES)) + ")o[id]=document.getElementById(id).checked;"
                              "for(const id of " + json.dumps(list(SELECTS)) + ")o[id]=document.getElementById(id).value;"
                              "o.trending=document.querySelector('#nav-hide-list input[data-tltab=trending]').checked;return o;})()")
        return got, visible, controls

    got, visible, controls = asyncio.run(device(on_b, seed=state["seed"], base=BASE_NO_FIRST_RUN))
    for k, v in got.items():
        assert v == want[k], (k, "device B did not adopt it", v, want[k])
    assert visible == {"splash": False, "theme": "oled", "accent": "#ff8800", "panel": False, "hiddenRowShown": False}, visible
    for el, k in SWITCHES.items():
        assert controls[el] == want[k], (el, "the Settings switch on device B disagrees with the account", controls[el], want[k])
    for el, k in SELECTS.items():
        if k == "desktopBuddy":
            assert controls[el] == want[k]["who"], (el, controls[el], want[k])
        else:
            assert str(controls[el]) == str(want[k]), (el, controls[el], want[k])
    assert controls["trending"] is False, "the hidden Trending tab shows as on in device B's sidebar editor"


# ----------------------------------------------------------------------------------- failure paths

def _b_sees(seed, expr):
    async def on_b(b):
        await wait(b, expr, 30, "device B to adopt " + expr)
        return True
    return asyncio.run(device(on_b, seed=seed))


@needs_chrome
def test_a_change_made_while_the_prefs_could_not_be_read_rides_the_next_save():
    """(a) The read fails at save time: nothing is published, the change waits; the relay recovers and the
    next save -- of a DIFFERENT key -- carries both. Device B gets both."""
    state = {}

    async def on_a(b):
        await restored(b)                                    # the login restore has read (an empty account)
        await open_settings(b)
        await b.js("__fx.set({read:'down'})")
        d0 = await b.js("__fx.dropped")
        assert await toggle(b, "set-no-images") is True
        await read_fails_once_more(b, d0)
        assert await b.js("__fx.prefsDoc()") is None, "a doc was published without reading the current one"
        await b.js("__fx.set({read:'up'})")
        assert await toggle(b, "set-hide-replies") is True
        await wait(b, "(__fx.prefsDoc()||{}).hideReplies===true", 30, "the second save to land")
        await sent(b)
        doc = await b.js("__fx.prefsDoc()")
        state["seed"] = [await b.js("__fx.prefs()")]
        return doc

    doc = asyncio.run(device(on_a))
    assert doc.get("noImages") is True, ("the change made while the read failed never reached the account", doc)
    assert _b_sees(state["seed"], "ClientSettings.get('noImages')===true && ClientSettings.get('hideReplies')===true")


@needs_chrome
def test_a_change_made_while_the_prefs_could_not_be_read_syncs_on_its_own():
    """(a') Nothing else is changed and the page is not reloaded: once the relay answers again, the
    waiting change is sent by itself (a desktop left open for days must not keep it to itself)."""
    async def on_a(b):
        await restored(b)
        await open_settings(b)
        await b.js("__fx.set({read:'down'})")
        d0 = await b.js("__fx.dropped")
        await toggle(b, "set-clean-links")
        await read_fails_once_more(b, d0)
        await b.js("__fx.set({read:'up'})")
        await wait(b, "'cleanLinks' in (__fx.prefsDoc()||{})", 60, "the waiting change to be sent without being asked")
        await wait(b, "!localStorage.getItem('pc.prefs.pending.'+__PC.me().pubkey)", 10)
        return await b.js("__fx.prefsDoc()")

    doc = asyncio.run(device(on_a))
    assert doc["cleanLinks"] is True, doc


@needs_chrome
def test_a_refused_publish_stays_pending_until_a_relay_accepts_it():
    """(b) The relay refuses the document: the value stays pending, and the next accepted save carries it."""
    state = {}

    async def on_a(b):
        await restored(b)
        await open_settings(b)
        await b.js("__fx.set({accept:false})")
        await toggle(b, "set-vim")
        await wait(b, "__fx.refused>=1 && __fx.refusedOk>=__fx.refused", 30, "the refusal to reach the page")
        await b.js("__fx.set({accept:true})")
        state["pe"] = await toggle(b, "set-post-effects")
        await wait(b, "__fx.accepted>=1 && 'postEffects' in (__fx.prefsDoc()||{})", 30)
        await wait(b, "!localStorage.getItem('pc.prefs.pending.'+__PC.me().pubkey)", 10, "the sent values to leave pending")
        state["seed"] = [await b.js("__fx.prefs()")]
        state["pending"] = await b.js("localStorage.getItem('pc.prefs.pending.'+__PC.me().pubkey)")
        return await b.js("__fx.prefsDoc()")

    doc = asyncio.run(device(on_a))
    assert doc.get("vimKeys") is True, ("the refused change never reached the account", doc)
    assert state["pending"] is None, ("sent values are still pending", state["pending"])
    assert _b_sees(state["seed"], "ClientSettings.get('vimKeys')===true && ClientSettings.get('postEffects')===" + json.dumps(state["pe"]))


@needs_chrome
def test_an_older_account_copy_does_not_revert_an_unsent_change_and_a_reload_sends_it():
    """(c) + (a, reload). Another device left `noImages:false` on the account. This device turns the data
    saver ON while the prefs cannot be read, then reloads once the relay is back: the restore must NOT
    put the older `false` back over the change, and must send the change -- keeping the other keys."""
    async def make_old(b):           # "another device": an ordinary accepted save
        await restored(b)
        await open_settings(b)
        if not await b.js("document.getElementById('set-no-images').checked"):
            await toggle(b, "set-no-images")
            await wait(b, "(__fx.prefsDoc()||{}).noImages===true", 30)
        await toggle(b, "set-no-images")
        await toggle(b, "set-vim")
        await wait(b, "(()=>{const d=__fx.prefsDoc()||{};return d.vimKeys===true&&d.noImages===false;})()", 30)
        return [await b.js("__fx.prefs()")]

    seed = asyncio.run(device(make_old))
    assert json.loads(seed[0]["content"]) == {**json.loads(seed[0]["content"]), "vimKeys": True, "noImages": False}

    async def on_a(b):
        await restored(b)
        await open_settings(b)
        await wait(b, "ClientSettings.get('vimKeys')===true", 15)
        await b.js("__fx.set({read:'down'})")
        d0 = await b.js("__fx.dropped")
        assert await toggle(b, "set-no-images") is True
        await read_fails_once_more(b, d0)
        await b.js("__fx.set({read:'up'})")
        await reload(b)                         # (the fixture's counters start again with the page)
        await wait(b, "__fx.reads>=1", 30, "the reload's restore to read the account's copy")
        await restored(b)                       # the restore has applied what it read
        await open_settings(b)
        after = await b.js("({stored:ClientSettings.get('noImages'),switch:document.getElementById('set-no-images').checked})")
        await settle(b, "(__fx.prefsDoc()||{}).noImages===true?[]:[1]", 30)
        return after, await b.js("__fx.prefsDoc()")

    after, doc = asyncio.run(device(on_a, seed=seed, mode={"read": "up", "accept": True}))
    assert after == {"stored": True, "switch": True}, ("the restore put the OLDER account copy back over the unsent change", after)
    assert doc.get("noImages") is True, ("the reload never sent the waiting change", doc)
    assert doc.get("vimKeys") is True, ("sending it lost another device's key", doc)


@needs_chrome
def test_two_saves_at_once_both_survive():
    """(d) Two switches flipped in the same tick: two read-modify-writes of one replaceable document. Both
    values must be in the account's document (the chain serializes them)."""
    async def on_a(b):
        await restored(b)
        await open_settings(b)
        await b.js("""(()=>{for(const id of ['set-hide-replies','set-clean-links']){const x=document.getElementById(id);
            x.checked=!x.checked;x.dispatchEvent(new Event('change',{bubbles:true}));}})()""")
        await wait(b, "(()=>{const d=__fx.prefsDoc()||{};return d.hideReplies===true&&d.cleanLinks===true;})()", 30)
        await sent(b)
        return await b.js("__fx.prefsDoc()")

    doc = asyncio.run(device(on_a))
    assert doc["hideReplies"] is True and doc["cleanLinks"] is True, doc


@needs_chrome
def test_an_unsent_change_is_never_published_under_another_account():
    """(e) Account X changes a pref while it cannot be sent, then the device switches to account Y (the
    switcher's "add account" → sign in). Y's saves never carry X's change; back on X, it is sent as X."""
    async def on_x(b):
        x = await me_pk(b)
        await restored(b)
        await open_settings(b)
        await b.js("__fx.set({read:'down'})")
        d0 = await b.js("__fx.dropped")
        await toggle(b, "set-vim")
        await read_fails_once_more(b, d0)
        await b.js("__fx.set({read:'up'})")
        # Switch to a second account: the live session is cleared (the remembered one stays), sign in as Y.
        await reloading(b, "Session.clear();location.reload()")
        await login(b, KEY_Y)
        y = await me_pk(b)
        assert y != x
        await restored(b)
        await open_settings(b)
        await toggle(b, "set-hide-replies")
        await wait(b, f"(__fx.prefsDoc({json.dumps(y)})||{{}}).hideReplies!==undefined", 30)
        await sent(b)                           # Y's restore and save are done: nothing more is queued as Y
        prefs_sent = await b.js("__fx.published.filter(__fx.isPrefs)")
        y_docs = [json.loads(e["content"]) for e in prefs_sent if e["pubkey"] == y]
        x_docs_while_y = [e for e in prefs_sent if e["pubkey"] == x]
        # Back to X: its unsent change goes out, signed by X.
        await reloading(b, "Session.clear();location.reload()")
        await login(b, KEY_A)
        await settle(b, f"(__fx.prefsDoc({json.dumps(x)})||{{}}).vimKeys!==undefined?[]:[1]", 40)
        return x, y, y_docs, x_docs_while_y, await b.js(f"__fx.prefsDoc({json.dumps(x)})")

    x, y, y_docs, x_docs_while_y, x_doc = asyncio.run(device(on_x))
    assert y_docs, "account Y's save never went out"
    for d in y_docs:
        assert "vimKeys" not in d, ("account X's unsent change was published under account Y", d)
    assert not x_docs_while_y, "something was published as X while Y was signed in"
    assert (x_doc or {}).get("vimKeys") is True, ("account X's change made while it could not be sent never reached X", x_doc)


@needs_chrome
def test_an_unsent_sidebar_change_survives_the_social_set_migration():
    """The restore's caller decides from what it returns. An account that chose Social before 2026-10-06
    (`experienceRev` 1) is migrated ONCE from `pr.navHidden`; a sidebar row hidden on this device while the
    prefs could not be read must be part of what that migration starts from -- never dropped from `pr`, which
    made the migration rebuild the list from nothing and save it over the person's choice."""
    async def make_old(b):
        await wait(b, "__fx.dropped>=1", 30, "the login restore to ask (and get no answer)")
        await b.js("__fx.put(NostrTools.finalizeEvent({kind:30078,created_at:Math.floor(Date.now()/1000)-600,"
                   "tags:[['d','pcai:client-prefs']],content:JSON.stringify({experience:'social',experienceRev:1,"
                   "navHidden:['notes']})},new Uint8Array(32).fill(1)))")
        return [await b.js("__fx.prefs()")]

    seed = asyncio.run(device(make_old, mode={"read": "down", "accept": True}))

    async def on_a(b):
        await wait(b, "__fx.dropped>=1", 30, "the login restore to ask (and get no answer)")
        await open_settings(b)
        d0 = await b.js("__fx.dropped")
        # A row the Social migration neither shows nor hides by itself.
        hidden = await b.js("""(()=>{const boxes=[...document.querySelectorAll('#nav-hide-list input[data-navkey]')];
            const x=['calendar','mail','contacts','budget','news','markets'].map(k=>boxes.find(c=>c.dataset.navkey===k&&c.checked&&!c.disabled)).find(Boolean);
            x.checked=false;x.dispatchEvent(new Event('change',{bubbles:true}));return x.dataset.navkey;})()""")
        await read_fails_once_more(b, d0)
        await b.js("__fx.set({read:'up'})")
        await reload(b)
        await settle(b, "(__fx.prefsDoc()||{}).experienceRev===2?[]:[1]", 40)
        await sent(b)
        return hidden, await b.js("ClientSettings.get('navHidden')"), await b.js("__fx.prefsDoc()")

    hidden, local, doc = asyncio.run(device(on_a, seed=seed, mode={"read": "down", "accept": True}))
    assert hidden in local, ("the migration rebuilt the sidebar without the row hidden on this device", hidden, local)
    assert hidden in doc.get("navHidden", []), ("the account lost the row hidden on this device", hidden, doc)
    assert "texts" in local and doc.get("experienceRev") == 2, (local, doc)


@needs_chrome
def test_an_older_unsent_concord_theme_never_overwrites_a_newer_one():
    """The Concord theme carries its own clock (`at`). One picked here while the prefs could not be read, and
    a different one picked LATER on another device, must leave the account -- and this device after its
    restore -- on the newer one; sending the older one would overwrite it and the two devices would flip."""
    async def on_a(b):
        await restored(b)
        await b.js("__fx.set({read:'down'})")
        d0 = await b.js("__fx.dropped")
        await b.js("PCConcordTheme.forOwner(__PC.me().pubkey);PCConcordTheme.set({theme:'monero'})")
        await read_fails_once_more(b, d0)
        # Meanwhile, the phone: a newer theme, in the account's document.
        await b.js("__fx.put(NostrTools.finalizeEvent({kind:30078,created_at:Math.floor(Date.now()/1000),"
                   "tags:[['d','pcai:client-prefs']],content:JSON.stringify({vimKeys:true,concordTheme:"
                   "{theme:'synthwave',accent:'',bg:'',at:Date.now()+5000}})},new Uint8Array(32).fill(1)))")
        await b.js("__fx.set({read:'up'})")
        await reload(b)
        await settle(b, "document.documentElement.dataset.ccTheme==='synthwave'?[]:[1]", 20)
        # Give any (wrong) send of the older theme every chance to happen.
        await b.js("__PC.switchView('settings')")
        await wait(b, "!!document.getElementById('set-hide-replies')", 30)
        await toggle(b, "set-hide-replies")
        await settle(b, "'hideReplies' in (__fx.prefsDoc()||{})?[]:[1]", 30)
        await sent(b)                           # every send this page had queued has gone out
        return await b.js("document.documentElement.dataset.ccTheme"), await b.js("__fx.prefsDoc()")

    painted, doc = asyncio.run(device(on_a))
    assert doc["concordTheme"]["theme"] == "synthwave", ("an older unsent theme overwrote the newer one on the account", doc)
    assert painted == "synthwave", ("this device kept its older theme over the account's newer one", painted)
    assert doc.get("vimKeys") is True and "hideReplies" in doc, doc
