#!/usr/bin/env python3
"""Admin → Relay: every list setting is a working list, on a phone and on a desktop.

Renders the SHIPPED Relay tab (templates/admin/tabs/nostr_relay.html) with the shipped admin
stylesheets and the shipped admin-identities.js / admin-relay-lists.js, against a stubbed
/api/admin/* that behaves like the real endpoints. At 360px and 1280px it asserts, in a real Chrome:

  * all eight list textareas became a list with an Add box, and each textarea is still in the form
    (folded into "Edit as text") with its name, so Save still sends it;
  * rows are drawn with the entries the server holds (profiles for key lists), nothing overflows
    the viewport and every Remove/Add button is on screen and tappable;
  * Enter in an Add box adds (POST) and does NOT submit the settings form; the text box and Save's
    baseline both become the server's new value; Remove does the same;
  * Identities offers NO "Remove all not in profile": a name here is membership whatever the profile
    shows (people may keep an identity of their own), so that bulk action would take their access.
"""
import asyncio, json, os, re, shutil, subprocess, sys, tempfile, threading, urllib.request, http.server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9531)
VIEWPORTS = ((360, 780, True), (1280, 900, False))
KEYS = ["nostr_relay_posterchan_origins", "nostr_relay_wot_seeds", "nostr_dvm_peers",
        "nostr_relay_blocked_words", "nostr_relay_blocked_relays", "nostr_relay_nip05_relays",
        "nostr_relay_upstream_relays", "nostr_relay_private_relays",
        # Admin → Blossom ("fix blossom list textboxes to function like the way you improved the relays")
        "blossom_whitelist", "media_own_hosts", "blossom_mirror_servers",
        # Admin → Fediverse → Blocking ("make the text area lists like you did for relays")
        "fedi_bridge_blocked_domains"]


def page(names_complete, old_server=False):
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")))
    tab = env.get_template("admin/tabs/nostr_relay.html").render(cache_bust="1") \
        + env.get_template("admin/tabs/blossom.html").render(cache_bust="1") \
        + env.get_template("admin/tabs/social.html").render(cache_bust="1")
    stub = r"""
<script>
window.__errors=[]; addEventListener('error',e=>__errors.push(e.message));
const loadedValues=new Map(); window.__posts=[]; window.__submits=0;
const LONG='wss://a-very-long-relay-hostname-that-keeps-going.example.org/some/deep/path/relay';
const PK='npub1'+'q'.repeat(58);
const DB={
 nostr_relay_posterchan_origins:'https://poster.place\ncapacitor://localhost',
 nostr_relay_wot_seeds:PK,
 nostr_dvm_peers:PK+' wss://peer.example/relay',
 nostr_relay_blocked_words:'spammy phrase here\nbuy now',
 nostr_relay_blocked_relays:'mostr.pub',
 nostr_relay_nip05_relays:'wss://nos.lol/',
 nostr_relay_upstream_relays:'wss://relay.damus.io\n'+LONG,
 nostr_relay_private_relays:'',
 blossom_whitelist:PK,
 media_own_hosts:'media.poster.place',
 blossom_mirror_servers:'https://backup.example.com/blossom\n'+LONG.replace('wss://','https://'),
 fedi_bridge_blocked_domains:'spam.example\nsomeone-with-a-long-handle@a-very-long-instance-hostname.example.social',
};
const rowsOf=k=>DB[k].split('\n').filter(Boolean).map(v=>(k==='nostr_relay_wot_seeds'||k==='nostr_dvm_peers'||k==='blossom_whitelist')
  ?{value:v,pubkey:'ab'.repeat(32),npub:PK,name:'Seed Person With A Rather Long Display Name',picture:'',nip05:'seed@poster.place',relay:v.split(' ')[1]||'',valid:true}
  :(k==='fedi_bridge_blocked_domains'?{value:v,valid:true,type:v.includes('@')?'account':'instance'}:{value:v,valid:true}));
const IDS=[{name:'alice',address:'alice@poster.place',npub:'npub1a',verified:true,display:'Alice'},
  {name:'ghost',address:'ghost@poster.place',npub:'npub1g',verified:false,display:''},
  {name:'liar',address:'liar@poster.place',npub:'npub1l',verified:false,display:'Liar',profile_nip05:'x@y'}];
window.fetch=async(url,opt)=>{
  const u=new URL(url,location.href), body=opt&&opt.body?JSON.parse(opt.body):null;
  const ok=j=>({ok:true,status:200,json:async()=>j});
  if(u.pathname==='/api/admin/relay/list'&&__OLD__) return {ok:false,status:404,json:async()=>({detail:'Not Found'})};
  // A READ answers with the list AS IT WAS when the request started, after window.__getDelay ms -- a
  // real read takes time, and a reload begun before a Remove carries the old list back.
  if(u.pathname==='/api/admin/relay/list'&&!body){ const k=u.searchParams.get('key'), items=rowsOf(k);
    if(window.__getDelay) await new Promise(r=>setTimeout(r,window.__getDelay));
    return ok({key:k,items,names_complete:true}); }
  if(u.pathname==='/api/admin/relay/list'){ window.__posts.push(body);
    let lines=DB[body.key].split('\n').filter(Boolean);
    if(body.remove) lines=lines.filter(l=>l.toLowerCase()!==body.remove.toLowerCase());
    if(body.add) lines.push(body.add);
    DB[body.key]=lines.join('\n'); return ok({ok:true,value:DB[body.key],durable:true}); }
  if(u.pathname==='/api/admin/relay/identities') return ok({identities:IDS,names_complete:__NAMES_COMPLETE__});
  if(u.pathname==='/api/admin/relay/blocked') return ok({accounts:[],names_complete:true});
  return {ok:false,status:404,json:async()=>({})};
};
window.csrfFetch=window.fetch; window.pcConfirm=async()=>true;
</script>""".replace("__NAMES_COMPLETE__", "true" if names_complete else "false").replace("__OLD__", "true" if old_server else "false")
    fill = "<script>" + "".join(
        f"document.getElementById({json.dumps(k)}).value=DB[{json.dumps(k)}];loadedValues.set({json.dumps(k)},DB[{json.dumps(k)}]);"
        for k in KEYS) + "</script>"
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/style.css"><link rel="stylesheet" href="/static/css/modules/components.css">
<link rel="stylesheet" href="/static/css/admin-tabs.css"><link rel="stylesheet" href="/static/css/admin-theme.css">
</head><body class="admin-page">{stub}<div class="admin-container">
<nav class="admin-tabs"><button type="button" class="tab-btn" data-tab="relay" id="relaytab">Relay</button><button type="button" class="tab-btn" data-tab="blossom" id="blossomtab">Blossom</button></nav>
<form id="settingsForm" class="settings-form" novalidate onsubmit="event.preventDefault();window.__submits++">{tab}</form></div>
{fill}
<script src="/static/js/admin-identities.js"></script><script src="/static/js/admin-relay-lists.js"></script>
<script>document.getElementById('tab-relay').classList.add('active');document.getElementById('tab-blossom').classList.add('active');document.getElementById('tab-social').classList.add('active');
setTimeout(()=>{{document.getElementById('relaytab').click();setTimeout(()=>window.__ready=true,300)}},50);</script>
</body></html>"""


AUDIT = r"""(async()=>{
const KEYS=%s, sleep=ms=>new Promise(r=>setTimeout(r,ms)), r=e=>e.getBoundingClientRect();
const vis=e=>!!e&&r(e).width>0&&r(e).height>0, onScreen=e=>{const b=r(e);return b.left>=-1&&b.right<=innerWidth+1&&b.width>=36&&b.height>=24};
const out={errors:window.__errors, overflow:document.documentElement.scrollWidth>innerWidth+1, keys:{}};
for(const k of KEYS){
  const ta=document.getElementById(k), panel=document.querySelector(`.rl-panel[data-key="${k}"]`);
  const det=ta&&ta.closest('details.blk-raw');
  // Open the folded text box too: it must fit as well.
  if(det)det.open=true;
  const rows=panel?[...panel.querySelectorAll('.rl-row')]:[];
  const add=panel&&panel.querySelector('.rl-add-input'), addBtn=panel&&panel.querySelector('.rl-add-btn');
  out.keys[k]={panel:!!panel, inForm:!!(ta&&ta.form&&ta.name===k), folded:!!det,
    rows:rows.length, removeOnScreen:rows.every(x=>onScreen(x.querySelector('.rl-remove'))),
    rowsFit:rows.every(x=>r(x).right<=innerWidth+1), addOnScreen:vis(add)&&onScreen(addBtn)&&r(add).width>=120,
    addNamed:!!(add&&add.name), taFits:!ta||r(ta).right<=innerWidth+1};
}
// A fediverse line says whether it blocks an instance or one account.
out.fediTypes=[...document.querySelectorAll('.rl-panel[data-key="fedi_bridge_blocked_domains"] .rl-type')].map(x=>x.textContent);
// Add by Enter in the blocked-words box.
const wp=document.querySelector('.rl-panel[data-key="nostr_relay_blocked_words"]'), wi=wp.querySelector('.rl-add-input');
wi.value='free crypto'; wi.focus();
const ev=new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}); wi.dispatchEvent(ev);
// A real Enter in a text input submits the form unless prevented -- emulate the browser's default.
if(!ev.defaultPrevented) document.getElementById('settingsForm').requestSubmit();
await sleep(200);
const ta=document.getElementById('nostr_relay_blocked_words');
out.added={posted:JSON.stringify(window.__posts.at(-1)), submits:window.__submits, text:ta.value,
  said:wp.querySelector('.rl-msg').textContent,
  baseline:loadedValues.get('nostr_relay_blocked_words'), drawn:[...wp.querySelectorAll('.rl-row')].map(x=>x.dataset.value), cleared:wi.value};
// Remove the long upstream relay.
const up=document.querySelector('.rl-panel[data-key="nostr_relay_upstream_relays"]');
const row=[...up.querySelectorAll('.rl-row')].find(x=>x.dataset.value.includes('very-long'));
row.querySelector('.rl-remove').click(); await sleep(200);
out.removed={posted:JSON.stringify(window.__posts.at(-1)), text:document.getElementById('nostr_relay_upstream_relays').value,
  baseline:loadedValues.get('nostr_relay_upstream_relays'), drawn:[...up.querySelectorAll('.rl-row')].length};
// "tried to remove ditto.pub": Remove while a reload is IN FLIGHT. The in-flight read carries the list from
// before the removal; the reload the removal asks for must still happen, or the row stays on screen.
{ const bp=document.querySelector('.rl-panel[data-key="nostr_relay_blocked_relays"]');
  window.__getDelay=300;
  document.querySelector('[data-tab="relay"]').click();            // starts a load of every list
  await sleep(20);
  const r=[...bp.querySelectorAll('.rl-row')].find(x=>x.dataset.value==='mostr.pub');
  if(r) r.querySelector('.rl-remove').click();
  await sleep(1200); window.__getDelay=0;
  out.race={had:!!r, drawn:[...bp.querySelectorAll('.rl-row')].map(x=>x.dataset.value)}; }
// Identities: there is no bulk remove of "not in profile" any more.
out.prune={exists:!!document.getElementById('ids_prune'),
           offered:/not in profile/i.test((document.getElementById('tab-relay')||document.body).innerText)};
// Admin → Blossom: a person added to the upload whitelist with Enter is saved to THAT list and drawn.
{ const bp=document.querySelector('.rl-panel[data-key="blossom_whitelist"]'), bi=bp.querySelector('.rl-add-input');
  const before=bp.querySelectorAll('.rl-row').length, subs=window.__submits;
  bi.value='npub1'+'z'.repeat(58);
  bi.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  await sleep(250);
  out.blossom={posted:JSON.stringify(window.__posts.at(-1)), submits:window.__submits-subs,
    rows:bp.querySelectorAll('.rl-row').length, before, text:document.getElementById('blossom_whitelist').value,
    said:(bp.querySelector('.rl-msg')||{}).textContent||''}; }
out.overflowAfter=document.documentElement.scrollWidth>innerWidth+1;
return out;})()""" % json.dumps(KEYS)


async def run():
    try:
        import websockets, jinja2  # noqa: F401
    except ImportError as e:
        print("SKIP", e); return 2
    chrome = next((shutil.which(x) for x in ("chromium", "google-chrome", "google-chrome-stable") if shutil.which(x)), None)
    if not chrome:
        print("SKIP no Chrome"); return 2
    pages = {"/": page(True), "/unread": page(False), "/old": page(True, old_server=True)}

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p in pages:
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(pages[p].encode()); return
            self.path = p
            return super().do_GET()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    td = tempfile.TemporaryDirectory()
    prof = os.environ.get("PC_CHECK_PROFILE") or td.name
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", f"--remote-debugging-port={CDP_PORT}",
                             f"--user-data-dir={prof}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tab = None
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list"))
                tab = next(t for t in tabs if t.get("type") == "page"); break
            except Exception:
                await asyncio.sleep(.2)
        if not tab:
            print("SKIP Chrome did not start"); return 2
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
            n = 0

            async def call(method, params={}):
                nonlocal n; n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == n:
                        return m.get("result", {})

            async def load(path, w, h, mobile):
                await call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": mobile})
                await call("Page.navigate", {"url": f"http://127.0.0.1:{port}{path}"})
                for _ in range(60):
                    await asyncio.sleep(.1)
                    z = await call("Runtime.evaluate", {"expression": "window.__ready===true", "returnByValue": True})
                    if z.get("result", {}).get("value"):
                        return True
                return False

            await call("Page.enable"); await call("Runtime.enable")
            fails = []
            for w, h, mobile in VIEWPORTS:
                if not await load("/", w, h, mobile):
                    print(f"FAIL page never became ready at {w}px"); return 1
                z = await call("Runtime.evaluate", {"expression": AUDIT, "awaitPromise": True, "returnByValue": True})
                out = z.get("result", {}).get("value")
                if not out:
                    print("FAIL audit threw", z.get("exceptionDetails")); return 1
                where = f"{w}px"
                if out["errors"]:
                    fails.append((where, "js errors", out["errors"]))
                if out["overflow"] or out["overflowAfter"]:
                    fails.append((where, "horizontal overflow"))
                for k, v in out["keys"].items():
                    want = {"panel": True, "inForm": True, "folded": True, "removeOnScreen": True, "rowsFit": True,
                            "addOnScreen": True, "addNamed": False, "taFits": True}
                    bad = {x: v[x] for x in want if v[x] != want[x]}
                    if bad:
                        fails.append((where, k, bad))
                    if k != "nostr_relay_private_relays" and v["rows"] < 1:
                        fails.append((where, k, "no rows drawn"))
                if out.get("fediTypes") != ["instance", "account"]:
                    fails.append((where, "fediverse rows do not say instance/account", out.get("fediTypes")))
                a = out["added"]
                if a["submits"] or json.loads(a["posted"] or "{}") != {"key": "nostr_relay_blocked_words", "add": "free crypto"}:
                    fails.append((where, "add", a))
                if a["text"] != "spammy phrase here\nbuy now\nfree crypto" or a["baseline"] != a["text"] \
                        or "free crypto" not in a["drawn"] or a["cleared"]:
                    fails.append((where, "add did not land", a))
                if not re.search(r"Saved.*free crypto added", a.get("said") or ""):
                    fails.append((where, "an Add saved but said nothing, so Save then reads as 'nothing to save'", a.get("said")))
                rm = out["removed"]
                if "very-long" in rm["text"] or rm["baseline"] != rm["text"] or rm["drawn"] != 1:
                    fails.append((where, "remove did not land", rm))
                rc = out["race"]
                if not rc["had"] or "mostr.pub" in rc["drawn"]:
                    fails.append((where, "a Remove during a reload left the removed row on screen", rc))
                bl = out["blossom"]
                if bl["submits"] or json.loads(bl["posted"] or "{}") != {"key": "blossom_whitelist", "add": "npub1" + "z" * 58} \
                        or bl["rows"] != bl["before"] + 1 or ("npub1" + "z" * 58) not in bl["text"] or "Saved" not in bl["said"]:
                    fails.append((where, "Blossom whitelist Add did not land", bl))
                p = out["prune"]
                if p["exists"] or p["offered"]:
                    fails.append((where, "a bulk 'remove all not in profile' is still offered", p))
                # 2026-10-10 "IDENTITIES is formatted really bad, checkboxes are far right and barely can see the
                # account info": `.form-group input{width:100%}` stretched each row's select checkbox into a wide
                # flex item, squeezing the name/profile/activity column. Measured, not read: the box is box-sized
                # and the account text gets most of the row.
                z = await call("Runtime.evaluate", {"expression": """(async()=>{for(let i=0;i<50&&!document.querySelector('#ids_list .ids-row');i++)
                    await new Promise(r=>setTimeout(r,100));
                    return [...document.querySelectorAll('#ids_list .ids-row')].map(r=>{const c=r.querySelector('.ids-pick').getBoundingClientRect(),
                      who=r.querySelector('.blk-who').getBoundingClientRect(),row=r.getBoundingClientRect();
                      return {box:Math.round(c.width),left:Math.round(c.left-row.left),who:Math.round(who.width),row:Math.round(row.width)}})})()""",
                    "awaitPromise": True, "returnByValue": True})
                ids = z.get("result", {}).get("value") or []
                if not ids:
                    fails.append((where, "Identities drew no rows"))
                for r in ids:
                    if r["box"] > 28 or r["left"] > 16 or r["who"] < r["row"] * (0.4 if mobile else 0.6):
                        fails.append((where, "Identities row: the checkbox is stretched or the account info is squeezed", r))
                        break
            # A backend older than the page (no list endpoint): the text box is open and editable,
            # and no "Could not load the list" is left standing over a closed box.
            await load("/old", 360, 780, True)
            z = await call("Runtime.evaluate", {"expression": """(()=>{const ks=%s;return ks.map(k=>{const t=document.getElementById(k),
                p=document.querySelector('.rl-panel[data-key="'+k+'"]'),d=t&&t.closest('details');
                return {k, open:!!(d&&d.open), vis:!!t&&t.offsetParent!==null, panelHidden:!!(p&&p.hidden),
                        err:/Could not load/.test((p&&p.textContent)||'')}})})()""" % json.dumps(KEYS), "returnByValue": True})
            for row in z.get("result", {}).get("value") or [{"k": "?", "open": False}]:
                if not (row["open"] and row["vis"] and row["panelHidden"] and not row["err"]):
                    fails.append(("old server", row))
            if fails:
                for f in fails:
                    print("FAIL", *f)
                return 1
            print("Admin relay lists: clean at", ", ".join(f"{w}px" for w, _, _ in VIEWPORTS), "+ unread-profile guard + old-server fallback")
            return 0
    finally:
        proc.terminate(); srv.shutdown(); td.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
