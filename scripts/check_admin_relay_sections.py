#!/usr/bin/env python3
"""Admin → Relay sections, in a real Chrome, at a 390px phone and a 1280px desktop.

"Settings → Relay is too clunky and should be broken up for organization. I also see limits in a place that
makes no sense. I don't see any DB tunables for PosterChanDB in admin either." (2026-10-11)

Renders the SHIPPED admin page (templates/admin.html with every tab, the shipped stylesheets and every shipped
admin script) against a small fake /api/admin backend whose Save uses the REAL range check
(app/services/relay_tunables.py). At each width it asserts what the operator sees:

  * the section bar is there and each button shows exactly its own section; nothing overflows sideways;
  * Limits holds every limit field (the posting limits that used to sit inside Auto-clean, and the connection /
    protocol limits) and they are on screen; Database holds every PosterChanDB knob and a status panel filled
    from the relay's status report;
  * Save still round-trips a field that moved: a changed posting limit is sent ALONE, stored, and hydrated
    back into the field on a fresh load; an out-of-range Database value is refused and the page SAYS why;
  * the open section is remembered across a reload, #tab-relay/limits deep-links to a section, and
    "Show all" puts every section back on one page.

Screenshots of Limits and Database at both widths go to $PC_CHECK_SHOTS (default: a temp dir), never into the
working tree. Exit 0 = pass, 1 = fail, 2 = could not run (no Chrome / no websockets).
"""
import asyncio
import base64
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9547)
VIEWPORTS = ((390, 844, True), (1280, 900, False))
LIMIT_KEYS = ["nostr_relay_posts_per_min", "nostr_relay_same_per_hour", "nostr_relay_gift_wraps_per_recipient",
              "nostr_relay_max_connections", "nostr_relay_max_subs_per_conn", "nostr_relay_max_filters_per_req",
              "nostr_relay_max_message_kb"]
SECTIONS = ["general", "access", "limits", "sync", "retention", "moderation", "database"]


def render_admin():
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")))
    return env.get_template("admin.html").render(cache_bust="1")


class Backend:
    """The admin settings API, just enough of it: GET/PUT settings (PUT validated by the real relay_tunables)
    and the relay status the Database panel reads."""

    def __init__(self):
        from app.schemas import SettingsResponse
        self.db = {k: v for k, v in SettingsResponse().model_dump().items()}
        self.puts = []
        self.lock = threading.Lock()

    def settings(self):
        with self.lock:
            return dict(self.db)

    def put(self, body):
        from app.services import relay_tunables
        s = (body or {}).get("settings") or {}
        with self.lock:
            self.puts.append(s)
            bad = relay_tunables.validate_many(s)
            if bad:
                return 400, {"detail": "Not saved: " + "; ".join(bad)}
            self.db.update(s)
        return 200, {"ok": True}

    @staticmethod
    def status():
        now = int(time.time())
        return {"running": True, "members": 42, "conns": 3, "online": 2, "ts": now, "wot_depth": 1,
                "posterchandb": {"mode": "primary", "state": "ready", "events": 1234570, "live": 1234567,
                                 "dead": 3, "disk_bytes": 5 * 1024 ** 3, "arena_bytes": 900 * 1024 ** 2,
                                 "index_bytes": 300 * 1024 ** 2, "segments": 21, "cold_segments": 4,
                                 "unflushed": 17, "since_flush_s": 42.0, "search_words_pending": 0,
                                 "cache_resident_mb": 900, "cache_budget_mb": 4800, "cache_available_mb": 9000,
                                 "cache_psi": 0.0, "last_snapshot": now - 1800, "max_lock_hold_ms": 3.2,
                                 "tuning": {"posterchandb_flush_seconds": 300.0}}}


AUDIT = r"""(async()=>{
const SECTIONS=%s, LIMITS=%s, PCDB=%s;
const sleep=ms=>new Promise(r=>setTimeout(r,ms)), R=e=>e.getBoundingClientRect();
const pane=document.getElementById('tab-relay'), nav=pane.querySelector('.admin-subnav');
const shown=()=>[...pane.querySelectorAll(':scope > .admin-sec')].filter(s=>s.offsetParent!==null).map(s=>s.dataset.sec);
const over=()=>document.documentElement.scrollWidth>innerWidth+1;
const out={relayActive:pane.classList.contains('active'), nav:!!nav, initial:shown(), perSection:{}, overflow:false,
  navFits:!!nav&&[...nav.querySelectorAll('.subnav-btn')].every(b=>R(b).right<=innerWidth+1&&R(b).left>=-1&&R(b).height>=30)};
for(const s of SECTIONS){
  nav.querySelector('.subnav-btn[data-sec="'+s+'"]').click(); await sleep(60);
  const sec=document.getElementById('relay-sec-'+s);
  const fields=[...sec.querySelectorAll('input[name],select[name],textarea[name]')].filter(f=>f.type!=='hidden'&&!f.closest('details:not([open])')&&f.offsetParent!==null);
  out.perSection[s]={shown:shown(), active:nav.querySelector('.subnav-btn.active')?.dataset.sec,
    overflow:over(), fieldsOffScreen:fields.filter(f=>R(f).right>innerWidth+1||R(f).left<-1).map(f=>f.name)};
  out.overflow=out.overflow||over();
}
nav.querySelector('.subnav-btn[data-sec="limits"]').click(); await sleep(60);
out.limits=LIMITS.map(k=>{const f=document.getElementById(k);return {k, inLimits:!!f&&!!f.closest('#relay-sec-limits'),
  visible:!!f&&f.offsetParent!==null, fits:!!f&&R(f).right<=innerWidth+1, value:f&&f.value};});
nav.querySelector('.subnav-btn[data-sec="database"]').click(); await sleep(60);
out.db=PCDB.map(k=>{const f=document.getElementById(k);return {k, inDb:!!f&&!!f.closest('#relay-sec-database'),
  visible:!!f&&f.offsetParent!==null, fits:!!f&&R(f).right<=innerWidth+1, value:f&&f.value};});
let stats='';
for(let i=0;i<120;i++){ stats=(document.getElementById('pcdb-stats')||{}).textContent||''; if(/live/.test(stats)&&/On disk/.test(stats)) break; await sleep(100); }
out.stats=stats; out.statsFits=R(document.getElementById('pcdb-stats')).right<=innerWidth+1;
nav.querySelector('.subnav-all').click(); await sleep(60);
out.all=shown();
return out;})()"""

SAVE = r"""(async(key,val)=>{
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const sec=document.getElementById(key).closest('.admin-sec').dataset.sec;
document.querySelector('#tab-relay .subnav-btn[data-sec="'+sec+'"]').click(); await sleep(60);
const f=document.getElementById(key); f.value=val; f.dispatchEvent(new Event('input',{bubbles:true}));
document.querySelector('#settingsForm .save-btn').click();
let said='';
for(let i=0;i<120;i++){ await sleep(100);
  const t=document.body.innerText;
  if(/Settings saved|Not saved|Failed to save|Nothing changed/.test(t)){ said=(t.match(/(Settings saved!|Not saved:[^\n]*|Failed to save settings|Nothing changed\.)/)||[''])[0]; break; } }
// dismiss the dialog so the next step starts clean
const ok=[...document.querySelectorAll('button')].find(b=>/^(OK|Ok|Close)$/.test(b.textContent.trim())&&b.offsetParent!==null);
if(ok) ok.click(); await sleep(100);
return {said, value:document.getElementById(key).value};
})(%s,%s)"""


async def run():
    try:
        import websockets  # noqa: F401
        import jinja2  # noqa: F401
    except ImportError as e:
        print("SKIP", e)
        return 2
    chrome = next((shutil.which(x) for x in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
                   if shutil.which(x)), None)
    if not chrome:
        print("SKIP no Chrome")
        return 2
    from app.services import relay_tunables
    PCDB = list(relay_tunables.PCDB)
    page = render_admin().encode()
    backend = Backend()

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p == "/admin":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(page)
                return
            if p == "/api/admin/settings":
                return self._json(200, backend.settings())
            if p == "/api/admin/nostr-relay/status":
                return self._json(200, backend.status())
            if p.startswith("/api/") or p in ("/sw.js", "/manifest.json"):
                return self._json(404, {"detail": "not in this check"})
            self.path = p
            return super().do_GET()

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_PUT(self):
            if self.path.split("?", 1)[0] == "/api/admin/settings":
                code, obj = backend.put(self._body())
                return self._json(code, obj)
            return self._json(404, {})

        def do_POST(self):
            self._body()
            return self._json(404, {"detail": "not in this check"})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    td = tempfile.TemporaryDirectory()
    prof = os.environ.get("PC_CHECK_PROFILE") or td.name
    shots = os.environ.get("PC_CHECK_SHOTS") or tempfile.mkdtemp(prefix="pc-relay-sections-")
    os.makedirs(shots, exist_ok=True)
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                             f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={prof}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fails, saved = [], []
    try:
        import websockets
        tab = None
        for _ in range(100):          # poll the port: Chrome 153 writes no DevToolsActivePort for a fixed port
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2))
                tab = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                await asyncio.sleep(.2)
        if not tab:
            print("SKIP Chrome did not start")
            return 2
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
            n = 0

            async def call(method, params=None):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == n:
                        return m.get("result", {})

            async def ev(expr, await_promise=False):
                z = await call("Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise,
                                                    "returnByValue": True})
                if z.get("exceptionDetails"):
                    raise RuntimeError(json.dumps(z["exceptionDetails"])[:800])
                return z.get("result", {}).get("value")

            loads = [0]

            async def load(path, ready_expr):
                # A fresh document every time (a unique query): a hash-only change is a same-document navigation
                # and would not re-run the page's start-up.
                loads[0] += 1
                base, _, frag = path.partition("#")
                url = f"http://127.0.0.1:{port}{base}?n={loads[0]}" + (("#" + frag) if frag else "")
                await call("Page.navigate", {"url": url})
                for _ in range(150):           # up to 15 s
                    await asyncio.sleep(.1)
                    try:
                        if await ev(ready_expr):
                            return True
                    except Exception:
                        pass
                return False

            async def shot(name, clip=None):
                params = {"format": "png"}
                if clip:
                    params.update(captureBeyondViewport=True, clip=dict(clip, scale=1))
                b = await call("Page.captureScreenshot", params)
                p = os.path.join(shots, name)
                with open(p, "wb") as f:
                    f.write(base64.b64decode(b.get("data", "")))
                saved.append(p)

            await call("Page.enable")
            await call("Runtime.enable")
            hydrated = ("(()=>{const p=document.getElementById('tab-relay'),f=document.getElementById("
                        "'nostr_relay_posts_per_min');return !!p&&p.classList.contains('active')&&!!f&&f.value!==''"
                        "&&!!document.querySelector('#tab-relay .subnav-btn.active')})()")
            for w, h, mobile in VIEWPORTS:
                where = f"{w}px"
                backend.__init__()
                await call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1,
                                                                  "mobile": mobile})
                await load("/admin", "document.readyState==='complete'")
                await ev("(()=>{try{localStorage.clear()}catch(_){};return true})()")
                if not await load("/admin#tab-relay", hydrated):
                    fails.append((where, "the Relay tab never opened with its fields hydrated"))
                    continue
                out = await ev(AUDIT % (json.dumps(SECTIONS), json.dumps(LIMIT_KEYS), json.dumps(PCDB)), True)
                if not (out["relayActive"] and out["nav"] and out["navFits"]):
                    fails.append((where, "section bar missing or off screen", {k: out[k] for k in ("relayActive", "nav", "navFits")}))
                if out["initial"] != ["general"]:
                    fails.append((where, "first visit should open General alone", out["initial"]))
                for s, v in out["perSection"].items():
                    if v["shown"] != [s] or v["active"] != s:
                        fails.append((where, f"section {s}: shown {v['shown']} active {v['active']}"))
                    if v["overflow"] or v["fieldsOffScreen"]:
                        fails.append((where, f"section {s} overflows", v))
                for row in out["limits"]:
                    if not (row["inLimits"] and row["visible"] and row["fits"]):
                        fails.append((where, "limit field not in Limits / not on screen", row))
                for row in out["db"]:
                    if not (row["inDb"] and row["visible"] and row["fits"]):
                        fails.append((where, "PosterChanDB field not in Database / not on screen", row))
                defaults = {r["k"]: r["value"] for r in out["limits"] + out["db"]}
                want = {k: str(v) for k, v in relay_tunables.DEFAULTS.items()}
                bad_def = {k: (defaults.get(k), want[k]) for k in want
                           if defaults.get(k) not in (want[k], want[k].rstrip("0").rstrip(".") if "." in want[k] else want[k])}
                if bad_def:
                    fails.append((where, "a field does not hydrate to the value in force", bad_def))
                if "1,234,567 live" not in out["stats"] or "On disk" not in out["stats"] or "Last index snapshot" not in out["stats"]:
                    fails.append((where, "Database status panel not filled from the relay report", out["stats"][:300]))
                if not out["statsFits"]:
                    fails.append((where, "Database status panel overflows"))
                if out["all"] != SECTIONS:
                    fails.append((where, "Show all does not show every section", out["all"]))
                if out["overflow"]:
                    fails.append((where, "horizontal overflow"))

                # Screenshots: Limits and Database, as the operator sees them.
                # The section bar and the whole section, from the top of the bar to the end of the section.
                for s in ("limits", "database"):
                    clip = await ev("(()=>{window.pcAdminSection('relay','%s');scrollTo(0,0);"
                                    "const a=document.querySelector('#tab-relay .admin-subnav').getBoundingClientRect(),"
                                    "b=document.getElementById('relay-sec-%s').getBoundingClientRect();"
                                    "return {x:0,y:Math.max(0,a.top+scrollY-8),width:innerWidth,"
                                    "height:Math.min(4000,b.bottom-a.top+16)}})()" % (s, s))
                    await asyncio.sleep(.3)
                    await shot(f"relay-{s}-{w}.png", clip)

                # Save round trip of a MOVED field: only it is sent, it is stored, and a fresh load shows it.
                n_before = len(backend.puts)
                r = await ev(SAVE % (json.dumps("nostr_relay_posts_per_min"), json.dumps("7")), True)
                sent = backend.puts[n_before:]
                if r["said"] != "Settings saved!" or sent != [{"nostr_relay_posts_per_min": "7"}] \
                        or str(backend.db.get("nostr_relay_posts_per_min")) != "7":
                    fails.append((where, "Save of a moved Limits field did not round-trip", r, sent))
                # Out of range: refused, nothing stored, and the page says why.
                n_before = len(backend.puts)
                r = await ev(SAVE % (json.dumps("posterchandb_compact_dead_pct"), json.dumps("5")), True)
                if "between 10 and 90" not in r["said"] or str(backend.db.get("posterchandb_compact_dead_pct")) != "40":
                    fails.append((where, "an out-of-range Database value was not refused with a reason", r))
                # Reload: the last section (Database) is remembered and the saved limit is hydrated.
                await ev("(()=>{const f=document.getElementById('posterchandb_compact_dead_pct');f.value='40';return true})()")
                if not await load("/admin", hydrated):
                    fails.append((where, "reload never hydrated"))
                else:
                    z = await ev("(()=>({shown:[...document.querySelectorAll('#tab-relay > .admin-sec')].filter(s=>s.offsetParent!==null).map(s=>s.dataset.sec),"
                                 "v:document.getElementById('nostr_relay_posts_per_min').value}))()")
                    if z["shown"] != ["database"] or z["v"] != "7":
                        fails.append((where, "section not remembered or saved value not hydrated after reload", z))
                # Deep link to a section.
                await ev("(()=>{try{localStorage.clear()}catch(_){};return true})()")
                if await load("/admin#tab-relay/limits", hydrated):
                    z = await ev("[...document.querySelectorAll('#tab-relay > .admin-sec')].filter(s=>s.offsetParent!==null).map(s=>s.dataset.sec)")
                    if z != ["limits"]:
                        fails.append((where, "#tab-relay/limits did not open Limits", z))
                else:
                    fails.append((where, "#tab-relay/limits did not open the Relay tab"))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        srv.shutdown()
        td.cleanup()
    for p in saved:
        print("screenshot:", p)
    if fails:
        for f in fails:
            print("FAIL", *f)
        return 1
    print("Admin relay sections: clean at", ", ".join(f"{w}px" for w, _, _ in VIEWPORTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
