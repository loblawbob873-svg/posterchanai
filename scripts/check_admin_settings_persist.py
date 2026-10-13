#!/usr/bin/env python3
"""Admin settings added 2026-10-10/11 survive Save → restart → reload, in a real Chrome, end to end.

"make sure all your admin changes persist and hydrate properly with today's changes" (2026-10-11).

Renders the SHIPPED admin page (templates/admin.html, the shipped admin scripts) served by the REAL admin router
(PUT/GET /api/admin/settings, its validation and normalisation) over the REAL settings store, which writes
operator-signed `pcai:setting:` documents to a relay on a real socket (tests/relay_fake.FakeRelay, signatures
checked). Every other /api path answers 404, so nothing else on the page can touch anything. Then:

  1. Admin → Network → Extra Tor daemons and every Relay → Limits / Database field (and the PosterChanDB mode
     select) are TYPED into and saved with the page's own Save button -- including the values a generic form
     loses: a ZERO in a number field, a fraction, a select's non-first option;
  2. the node RESTARTS its settings: the in-process cache is thrown away, init_db() defaults are applied and the
     settings are hydrated back from the relay documents alone;
  3. the page is reloaded and every field must show what was typed;
  4. the second round puts values BACK (Tor "auto", the mode back to Automatic = "", a limit back to its
     default), restart, reload -- they must show again (a blank select / "auto" text must not be dropped);
  5. "1e3" typed into a number field (the browser accepts it: 1000) is stored as 1000, and the page still loads
     after a restart -- stored verbatim it made GET 500 and the whole page hydrated nothing;
  6. an out-of-range value typed into a field is refused, the page says why, and nothing reaches the relay.

Exit 0 = pass, 1 = fail, 2 = could not run (no Chrome / websockets / uvicorn). Reads PC_CHECK_PORT and
PC_CHECK_PROFILE; writes nothing into the working tree.
"""
import asyncio
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
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9561)
os.environ["POSTERCHANAI_LOCAL_SETTINGS"] = os.path.join(tempfile.mkdtemp(prefix="pc-admin-persist-"), "local.json")
os.environ.pop("POSTERCHANDB_MODE", None)

OP_SK = bytes.fromhex("6b" * 32)

# Round 1: a non-default value in every field added in the last two days (what the operator types).
ROUND1 = {
    "tor_extra_daemons": "3",
    "posterchandb_flush_seconds": "45",
    "posterchandb_read_cache_mb": "256",
    "posterchandb_cache_low_water_mb": "900",
    "posterchandb_snapshot_hours": "0.5",
    "posterchandb_compact_dead_pct": "55",
    "posterchandb_maint_interval_sec": "120",
    "posterchandb_maint_io_mb_s": "32.5",
    "posterchandb_maint_cpu_pct": "25",
    "posterchandb_maint_busy_load": "0",
    "posterchandb_words_batch": "100",
    "nostr_relay_posts_per_min": "0",
    "nostr_relay_same_per_hour": "7",
    "nostr_relay_gift_wraps_per_recipient": "1000",
    "nostr_relay_max_connections": "250",
    "nostr_relay_max_subs_per_conn": "40",
    "nostr_relay_max_filters_per_req": "12",
    "nostr_relay_max_message_kb": "16384",
}
# Round 2: values put BACK -- "auto", the select's blank first option (Automatic), a limit at its default.
ROUND2 = {"tor_extra_daemons": "auto", "nostr_relay_posts_per_min": "10",
          "posterchandb_maint_busy_load": "0.75"}


def render_admin():
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")))
    return env.get_template("admin.html").render(cache_bust="1")


def build_node():
    """The settings store wired to a relay on a socket, and the admin router in front of it."""
    from unittest import mock
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from app.auth import get_current_user
    from app.database import get_db, init_db
    from app.routers import admin as admin_router
    from app.services import nostr_store, settings_store as S
    from app.services.nostr_relay import thread
    from tests.relay_fake import FakeRelay

    relay = FakeRelay()
    S._operator_seckey = lambda db: OP_SK
    S._OP_SK = OP_SK
    S._port = lambda db=None: relay.port
    real_wt = S.write_through

    async def write_through(db, changes):
        try:
            return await real_wt(db, changes)
        finally:
            loop = asyncio.get_running_loop()
            for k, (lp, ws) in list(nostr_store._pub_pool.items()):
                if lp is loop:
                    nostr_store._pub_pool.pop(k, None)
                    await ws.close()
    S.write_through = write_through
    # The Save writes through before it answers (the background writer's retries would outlive the check).
    S._schedule_relay_write = lambda changes: asyncio.run(S.write_through(None, changes))
    thread.trigger_store_config_reload = lambda *a, **k: None
    thread.restart_nostr_relay = lambda *a, **k: None

    def restart():
        """What a restarted node's settings are: defaults, then ONLY what the relay holds."""
        S._CACHE.clear()
        S._HYDRATED_KEYS.clear()
        S._LOCAL_KEYS.clear()
        S._HYDRATED = False
        init_db()
        n = S.hydrate_from_db(None)
        assert S.is_hydrated(), "the restart could not read the relay"
        return n

    restart()
    page = render_admin()
    app = FastAPI()

    @app.middleware("http")
    async def only_settings(request, call_next):
        p = request.url.path
        if p.startswith("/api/") and p != "/api/admin/settings":
            return JSONResponse({"detail": "not in this check"}, status_code=404)
        return await call_next(request)

    app.include_router(admin_router.router)
    app.dependency_overrides[get_current_user] = lambda: mock.Mock(is_admin=True)
    app.dependency_overrides[get_db] = lambda: iter([object()])

    @app.get("/admin")
    def admin_page():
        return HTMLResponse(page)
    app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")
    return app, relay, restart


def relay_values(relay, keys):
    from app.services.nostr import nip44
    from app.services.nostr.event import verify_event
    out = {}
    for e in relay.events:
        d = next((t[1] for t in e.get("tags", []) if t and t[0] == "d"), "")
        k = d[len("pcai:setting:"):]
        if d.startswith("pcai:setting:") and k in keys and verify_event(e):
            out[k] = json.loads(nip44.decrypt_self(OP_SK, e["content"]))["value"]
    return out


FILL_AND_SAVE = r"""(async(vals)=>{
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
for(const [k,v] of Object.entries(vals)){
  const f=document.getElementById(k); if(!f) return {missing:k};
  f.focus(); f.value=v; f.dispatchEvent(new Event('input',{bubbles:true})); f.dispatchEvent(new Event('change',{bubbles:true}));
  if(f.value!==v) return {refusedByField:k, has:f.value};
}
document.querySelector('#settingsForm .save-btn').click();
let said='';
for(let i=0;i<150;i++){ await sleep(100);
  const t=document.body.innerText;
  const m=t.match(/(Settings saved!|Not saved:[^\n]*|Failed to save settings|Nothing changed\.|Error saving settings)/);
  if(m){ said=m[0]; break; } }
const ok=[...document.querySelectorAll('button')].find(b=>/^(OK|Ok|Close)$/.test(b.textContent.trim())&&b.offsetParent!==null);
if(ok) ok.click(); await sleep(100);
return {said};
})(%s)"""

READ = r"""(keys=>Object.fromEntries(keys.map(k=>{const f=document.getElementById(k);return [k,f?f.value:null];})))(%s)"""


async def run():
    try:
        import websockets  # noqa: F401
        import uvicorn
        import jinja2  # noqa: F401
    except ImportError as e:
        print("SKIP", e)
        return 2
    chrome = next((shutil.which(x) for x in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
                   if shutil.which(x)), None)
    if not chrome:
        print("SKIP no Chrome")
        return 2
    app, relay, restart = build_node()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    td = tempfile.TemporaryDirectory()
    prof = os.environ.get("PC_CHECK_PROFILE") or td.name
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                             f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={prof}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fails = []
    keys = sorted(set(ROUND1) | set(ROUND2))
    try:
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

            async def ev(expr):
                z = await call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
                if z.get("exceptionDetails"):
                    raise RuntimeError(json.dumps(z["exceptionDetails"])[:800])
                return z.get("result", {}).get("value")

            loads = [0]

            async def load():
                """A fresh document; ready once GET /api/admin/settings has hydrated the form."""
                loads[0] += 1
                await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin?n={loads[0]}#tab-network"})
                for _ in range(150):           # up to 15 s
                    await asyncio.sleep(.1)
                    try:
                        if await ev("(()=>{const f=document.getElementById('nostr_relay_max_subs_per_conn');"
                                    "return document.readyState==='complete'&&!!f&&f.value!==''})()"):
                            await asyncio.sleep(.3)
                            return True
                    except Exception:
                        pass
                return False

            await call("Page.enable")
            await call("Runtime.enable")
            await call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1,
                                                              "mobile": False})
            for label, vals in (("round 1", ROUND1), ("round 2 (values put back)", ROUND2)):
                if not await load():
                    fails.append((label, "the admin page never hydrated"))
                    break
                r = await ev(FILL_AND_SAVE % json.dumps(vals))
                if r.get("said") != "Settings saved!":
                    fails.append((label, "Save did not succeed", r))
                    continue
                held = relay_values(relay, set(vals))
                wrong = {k: (held.get(k), v) for k, v in vals.items() if held.get(k) != v}
                if wrong:
                    fails.append((label, "the relay does not hold what was saved (have, want)", wrong))
                restart()
                if not await load():
                    fails.append((label, "after a restart the admin page never hydrated"))
                    continue
                shown = await ev(READ % json.dumps(keys))
                want = dict(ROUND1)
                if vals is ROUND2:
                    want.update(ROUND2)
                bad = {k: (shown.get(k), w) for k, w in want.items() if shown.get(k) != w}
                if bad:
                    fails.append((label, "after Save + restart + reload a field shows the wrong value (shown, saved)", bad))
            # A number the browser accepts and the schema could not read back: "1e3" in a number input is 1000.
            # Stored verbatim it made GET answer 500, so the page after a restart hydrated NOTHING.
            if await load():
                r = await ev(FILL_AND_SAVE % json.dumps({"posterchandb_flush_seconds": "1e3"}))
                if r.get("said") != "Settings saved!":
                    fails.append(("1e3", "Save of 1e3 (= 1000) did not succeed", r))
                restart()
                if not await load():
                    fails.append(("1e3", "after saving 1e3 and a restart the admin page never hydrated"))
                else:
                    shown = await ev(READ % json.dumps(["posterchandb_flush_seconds"]))
                    if shown.get("posterchandb_flush_seconds") != "1000":
                        fails.append(("1e3", "the field does not show 1000", shown))
            # Out of range, typed into the page: refused, said, nothing written.
            if await load():
                before = list(relay.writes)
                r = await ev(FILL_AND_SAVE % json.dumps({"tor_extra_daemons": "12"}))
                if not str(r.get("said", "")).startswith("Not saved") or "tor_extra_daemons" not in r.get("said", ""):
                    fails.append(("out of range", "tor_extra_daemons=12 was not refused with a reason", r))
                if relay.writes != before:
                    fails.append(("out of range", "a refused Save wrote to the relay"))
                restart()
                if await load():
                    shown = await ev(READ % json.dumps(["tor_extra_daemons"]))
                    if shown.get("tor_extra_daemons") != "auto":
                        fails.append(("out of range", "the refused value came back after a restart", shown))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        server.should_exit = True
        th.join(10)
        relay.close()
        td.cleanup()
    if fails:
        for f in fails:
            print("FAIL", *f)
        return 1
    print(f"Admin settings persist: {len(ROUND1)} fields saved, restarted, reloaded and shown; "
          f"{len(ROUND2)} put back and shown; 1e3 stored as 1000; out-of-range refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
