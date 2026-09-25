"""Drive System Settings → PosterChan Server → Enable on an INSTALLED PosterChanOS, through its UI.

THE GATE THE RELEASE PROCESS WAS MISSING. check_livecd_install_vm.py --server runs `pc-server enable`
from a root shell -- the command the button calls -- so the button, the desktop bridge
(pcServer.enable → desktop/server.js), the owner's sudo rule and the panel were never exercised on an
install. First run 2026-09-25: PASS (job rc=0 in 116s, server active + reachable, posts flowing), and
it found that a fresh install carried the build host's users in /etc/group (wheel included).

Run the VM half on the KVM host (scripts/vm_server_ui_boot.py), tunnel its port here, then:
    ssh -N -J nas.lan -L 9322:127.0.0.1:9322 root@<kvm-host> &
    PC_UI_CDP_PORT=9322 venv-unified/bin/python scripts/check_server_enable_ui.py


Talks CDP to the desktop shell (tunnelled to 127.0.0.1:$PC_UI_CDP_PORT). Phases, each printed:
  greeter   first-run wizard -> no instance -> sign in with a fresh key (the first owner)
  session   the owner's own desktop session comes up (a NEW shell process)
  settings  System Settings -> PosterChan Server -> click Enable, follow the panel until the job ends
Exit 0 only when the PANEL reports the server running. (Posts flowing is checked from the root shell
by the caller, the same way the install gate does.)"""
import asyncio, json, os, sys, time, urllib.request
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import websockets
from tests.client.test_effects_full_app import Browser

PORT = int(os.environ.get("PC_UI_CDP_PORT", "9322"))
BASE = f"http://127.0.0.1:{PORT}"


def pages():
    try:
        return [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
                if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    except Exception:
        return []


async def attach(pred_js="!!window.__PC", timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in pages():
            try:
                ws = await websockets.connect(p["webSocketDebuggerUrl"], max_size=20_000_000)
                b = Browser(ws)
                if await b.js(f"(()=>{{try{{return !!({pred_js})}}catch(e){{return false}}}})()"):
                    return ws, b, p
                await ws.close()
            except Exception:
                pass
        await asyncio.sleep(3)
    raise SystemExit(f"FAIL  no shell page satisfying {pred_js} within {timeout}s; pages={[(p.get('url'),p.get('title')) for p in pages()]}")


async def until(b, expr, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await b.js(f"(()=>{{try{{return !!({expr})}}catch(e){{return false}}}})()"):
                return
        except Exception:
            pass
        await asyncio.sleep(2)
    raise SystemExit(f"FAIL  {what}: {expr} not true within {timeout}s")


async def main():
    # Needs an installed PosterChanOS VM behind a tunnel. checkall discovers every check_*.py, so with
    # none configured this is "could not run" (exit 2, a SKIP with its reason) -- never a 10-minute FAIL.
    if not os.environ.get("PC_UI_CDP_PORT"):
        print("SKIP  no installed VM: set PC_UI_CDP_PORT to its tunnelled DevTools port (see the docstring)")
        return 2
    if not pages():
        print(f"SKIP  nothing answers DevTools on {BASE} -- is the VM up and the tunnel open?")
        return 2
    phase = sys.argv[1] if len(sys.argv) > 1 else "all"
    if phase in ("greeter", "all"):
        ws, b, p = await attach("!!document.querySelector('[data-fr], #nsec-input') || !!window.__PC")
        print("OK  attached to the shell:", p.get("url"))
        # The wizard: network passes by itself on a wired VM; instance -> "Use no instance".
        for _ in range(60):
            step = await b.js("(()=>{const c=document.querySelector('[data-fr]');return c?[...document.querySelectorAll('[data-fr]')].map(x=>x.dataset.fr).join(','):''})()")
            if "skip" in step:
                await b.js("document.querySelector('[data-fr=\"skip\"]').click()"); print("OK  wizard: use no instance"); await asyncio.sleep(2); continue
            if "qr" in step:
                await b.js("document.querySelector('[data-fr=\"qr\"]').click()"); print("OK  wizard: to the sign-in screen"); await asyncio.sleep(2); break
            if "nonet" in step:
                print("..  wizard waits on the network step:", step)
            if await b.js("!!document.querySelector('#nsec-input')"):
                break
            await asyncio.sleep(3)
        await until(b, "document.querySelector('#nsec-input')", 120, "the sign-in screen offers a key field")
        await b.js("(()=>{const k=crypto.getRandomValues(new Uint8Array(32));document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(k);document.querySelector('#btn-nsec-login').click()})()")
        print("OK  signed in with a fresh key (this key is the machine's first owner)")
        await ws.close()
    if phase in ("session", "all"):
        # The owner's session is a NEW shell process; wait for its page with a signed-in user.
        ws, b, p = await attach("window.__PC && __PC.me && __PC.me() && window.PCOS", timeout=900)
        who = await b.js("(()=>{try{return __PC.me().pubkey.slice(0,12)}catch(e){return ''}})()")
        print("OK  the owner's desktop is up, signed in as", who)
        await ws.close()
    if phase in ("settings", "all"):
        # On PosterChanOS, System Settings is its OWN native window -- a separate CDP page.
        if not any("__ossettings" in (pg.get("title") or "") for pg in pages()):
            ws0, b0, _ = await attach("window.PCOS && PCOS.openSystemSettings", timeout=300)
            await b0.js("PCOS.openSystemSettings()"); await ws0.close()
        ws, b, p = await attach("document.querySelector('[data-page=\"server\"]') && window.pcServer", timeout=120)
        print("OK  System Settings is open in its own window:", p.get("title"))
        await b.js("document.querySelector('[data-page=\"server\"]').click()")
        await until(b, "document.querySelector('[data-srv-enable]') && !document.querySelector('[data-settings-page=\"server\"]').hidden", 60, "the Server page is showing")
        # The panel disables every control until its first status read answers ("Checking…").
        await until(b, "!/^Checking/.test(document.querySelector('[data-srv-summary]').textContent.trim())", 120,
                    "the Server panel finished reading the server's status")
        before = await b.js("document.querySelector('[data-srv-summary]').textContent.trim()")
        print("OK  System Settings -> PosterChan Server:", before[:120])
        dis = await b.js("document.querySelector('[data-srv-enable]').disabled")
        label = await b.js("document.querySelector('[data-srv-enable]').textContent.trim()")
        if dis:
            raise SystemExit(f"FAIL  the '{label}' button is disabled before anything was pressed")
        await b.js("document.querySelector('[data-srv-enable]').click()")
        print(f"OK  clicked '{label}'")
        # The panel asks first ("Enable the PosterChan server? …") -- answer it as a person would.
        await until(b, "[...document.querySelectorAll('button')].some(x=>x.offsetParent&&/^\s*Enable server\s*$/.test(x.textContent)&&!x.matches('[data-srv-enable]'))",
                    30, "the confirmation dialog appeared")
        await b.js("[...document.querySelectorAll('button')].find(x=>x.offsetParent&&/^\s*Enable server\s*$/.test(x.textContent)&&!x.matches('[data-srv-enable]')).click()")
        print("OK  confirmed the dialog")
        last = ""
        deadline = time.time() + int(os.environ.get("PC_UI_SERVER_TIMEOUT", "5400"))
        while time.time() < deadline:
            st = await b.js("""(async()=>{const s=await pcServer.status().catch(e=>({err:String(e)}));
              return {summary:(document.querySelector('[data-srv-summary]')||{}).textContent||'',
                      job:((document.querySelector('[data-srv-job]')||{}).textContent||'').slice(-300),
                      status:s}})()""")
            line = f"{st['summary'].strip()[:100]} | job={json.dumps((st['status'] or {}).get('job'))[:120]}"
            if line != last:
                print("..", line); last = line
            s = st["status"] or {}
            job = s.get("job") or {}
            if s.get("err"):
                raise SystemExit(f"FAIL  pcServer.status() threw through the bridge: {s['err']}")
            # The panel's own rules (os.js draw()): running = job.running; failed = a stopped job with a
            # nonzero rc; up = active === 'active' and reachable.
            if not job.get("running") and job.get("rc") and str(job.get("rc")) != "0":
                print(st["job"]); raise SystemExit(f"FAIL  the server install job failed (rc={job.get('rc')})")
            if s.get("active") == "active" and s.get("reachable"):
                print("OK  the panel reports the server ACTIVE and reachable:", st["summary"].strip()[:120])
                return 0
            await asyncio.sleep(15)
        raise SystemExit("FAIL  the server did not come up from the Settings button in time")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
