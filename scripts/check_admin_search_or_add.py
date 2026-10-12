#!/usr/bin/env python3
"""Admin lists have ONE "Search or add…" box: type once, add with Enter or "+ Add", and it persists.

"the entire list /search in admin ui is so dumb, to add an item, I have to search and then enter it again
below" (2026-10-11). Every list in the admin had a Search box above it AND an Add box below it. Now there is
one box: typing filters, a text that is not already an entry offers "+ Add "<text>"" right under the box,
Enter adds, an exact match says "already in the list" and adds nothing.

Renders the SHIPPED admin page (templates/admin.html + every shipped admin script and stylesheet) served by
the REAL admin router -- PUT/GET /api/admin/settings, POST /api/admin/relay/list, /api/admin/list-field/* --
over the REAL settings store, which writes operator-signed `pcai:setting:` documents to a relay on a real
socket (tests/relay_fake.FakeRelay). The bot list (/api/admin/bots) is an in-memory store that returns
exactly what the page's own Save sent; how the bots router persists a bot is tested elsewhere. At 390px
(mobile) and 1280px it drives a real Chrome:

  Admin → Relay (blocked words, blocked relays/domains, upstream relays):
    * each list has exactly ONE text box, it has no id/name (Save never reads it), it is shown on a short
      or empty list, and its placeholder says "Search or add";
    * typing something new shows "+ Add "<text>"" under the box; Enter adds it (the settings form is NOT
      submitted), the box is cleared, the WHOLE list is drawn again and the new row is highlighted;
    * "+ Add" (tapped) adds a domain typed as a URL -- stored normalised (`https://Spam.Example/x` →
      `spam.example`); an upstream relay typed with a trailing `/` is stored without it;
    * an exact match -- in another case, as a URL, with or without the trailing slash -- shows "already in
      the list", offers no Add, and Enter sends NOTHING to the server;
    * typing filters the rows;
    * the page's Save then succeeds, the node restarts its settings from the relay alone, a fresh page load
      shows every added entry, and the relay documents hold them.
  Admin → Bots → Edit (topics to rotate):
    * the same one box, Enter and "+ Add", exact match ("GAMING") not duplicated, filtering;
    * the bot's own Save sends the added topics; a fresh page load, reopening the bot, shows them as rows.

Exit 0 = pass, 1 = fail, 2 = could not run. Reads PC_CHECK_PORT and PC_CHECK_PROFILE; writes nothing into
the working tree.
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
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9573)
os.environ["POSTERCHANAI_LOCAL_SETTINGS"] = os.path.join(tempfile.mkdtemp(prefix="pc-admin-soa-"), "local.json")
os.environ.pop("POSTERCHANDB_MODE", None)

OP_SK = bytes.fromhex("6c" * 32)
WORDS, DOMAINS, UPSTREAM = "nostr_relay_blocked_words", "nostr_relay_blocked_relays", "nostr_relay_upstream_relays"


def render_admin():
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")))
    return env.get_template("admin.html").render(cache_bust="1")


def build_node():
    from unittest import mock
    from fastapi import FastAPI, Request
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
    S._schedule_relay_write = lambda changes: asyncio.run(S.write_through(None, changes))
    # No relay subprocess here: every live reload a list edit fires is a no-op.
    for name in dir(thread):
        if name.startswith("trigger_") or name.startswith("restart_"):
            setattr(thread, name, lambda *a, **k: None)

    def restart():
        """What a restarted node's settings are: defaults, then ONLY what the relay holds."""
        S._CACHE.clear()
        S._HYDRATED_KEYS.clear()
        S._LOCAL_KEYS.clear()
        S._HYDRATED = False
        init_db()
        S.hydrate_from_db(None)
        assert S.is_hydrated(), "the restart could not read the relay"

    restart()
    page = render_admin()
    app = FastAPI()
    posts = []
    bots = {7: {"id": 7, "name": "topicbot", "bot_type": "text", "platform": "nostr", "host": "", "modes": "",
                "enabled": False, "config": {"auto_post_topics": "gaming\ncomics"}}}
    passthrough = {"/api/admin/settings", "/api/admin/relay/list", "/api/admin/list-field/rows",
                   "/api/admin/list-field/apply"}

    @app.middleware("http")
    async def only_these(request, call_next):
        p = request.url.path
        if request.method == "POST" and p in ("/api/admin/relay/list", "/api/admin/list-field/apply"):
            posts.append(p)
        if p.startswith("/api/") and p not in passthrough and not p.startswith("/api/admin/bots"):
            return JSONResponse({"detail": "not in this check"}, status_code=404)
        return await call_next(request)

    @app.get("/api/admin/bots")
    def list_bots():
        return list(bots.values())

    @app.get("/api/admin/bots/status")
    def bot_status():
        return []

    @app.put("/api/admin/bots/{bot_id}")
    async def put_bot(bot_id: int, request: Request):
        body = await request.json()
        bots[bot_id] = dict(bots[bot_id], **{k: body[k] for k in ("name", "bot_type", "platform", "host", "modes",
                                                                  "config") if k in body})
        return bots[bot_id]

    app.include_router(admin_router.router)
    app.dependency_overrides[get_current_user] = lambda: mock.Mock(is_admin=True)
    app.dependency_overrides[get_db] = lambda: iter([object()])

    @app.get("/admin")
    def admin_page():
        return HTMLResponse(page)
    app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")
    return app, relay, restart, posts, bots


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


# One list panel as a person sees it. `sel` picks the panel; the box is whatever text input it holds.
PANEL = r"""((sel)=>{const p=document.querySelector(sel); if(!p) return null;
  const vis=e=>!!e&&!e.hidden&&getComputedStyle(e).display!=='none'&&e.getBoundingClientRect().width>0;
  const boxes=[...p.querySelectorAll('input')].filter(i=>i.type==='text'||i.type==='search');
  const box=boxes[0]||null;
  const add=[...p.querySelectorAll('button')].find(b=>vis(b)&&/^\+\s*Add\b/.test(b.textContent.trim()));
  const rows=[...p.querySelectorAll('.rl-row')];
  const r=e=>e.getBoundingClientRect();
  return {boxes:boxes.length, boxShown:vis(box), boxNamed:!!(box&&(box.name||box.id)),
    placeholder:box?box.placeholder:'', value:box?box.value:null,
    add:add?add.textContent.trim():null, addOnScreen:!!add&&r(add).left>=-1&&r(add).right<=innerWidth+1&&r(add).height>=24,
    exists:/already in the list/i.test([...p.querySelectorAll('*')].filter(vis).map(e=>e.childElementCount?'':e.textContent).join(' ')),
    rows:rows.map(x=>x.dataset.value), lit:rows.filter(x=>x.classList.contains('rl-new')).map(x=>x.dataset.value),
    msg:(p.querySelector('[role=status]')||{}).textContent||'',
    overflow:document.documentElement.scrollWidth>innerWidth+1};
})(%s)"""


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
    app, relay, restart, posts, bots = build_node()
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

            async def until(expr, secs=10):
                for _ in range(int(secs * 10)):
                    try:
                        if await ev(expr):
                            return True
                    except Exception:
                        pass
                    await asyncio.sleep(.1)
                return False

            def psel(key):
                return json.dumps(f'.rl-panel[data-key="{key}"]')

            async def panel(sel):
                return await ev(PANEL % sel)

            async def type_into(sel, text):
                """Focus the panel's box, clear it, and TYPE (real key input -> input events)."""
                await ev("(()=>{const b=[...document.querySelector(%s).querySelectorAll('input')].find(i=>i.type==='text'"
                         "||i.type==='search');b.scrollIntoView({block:'center'});b.focus();b.select();})()" % sel)
                await call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace",
                                                      "windowsVirtualKeyCode": 8})
                await call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace",
                                                      "windowsVirtualKeyCode": 8})
                if text:
                    await call("Input.insertText", {"text": text})
                await asyncio.sleep(.15)

            async def enter():
                for t in ("keyDown", "keyUp"):
                    await call("Input.dispatchKeyEvent", {"type": t, "key": "Enter", "code": "Enter",
                                                          "windowsVirtualKeyCode": 13, **({"text": "\r"} if t == "keyDown" else {})})
                await asyncio.sleep(.1)

            async def tap_add(sel):
                await ev("(()=>{const b=[...document.querySelector(%s).querySelectorAll('button')].find(b=>!b.hidden&&"
                         "/^\\+\\s*Add\\b/.test(b.textContent.trim()));if(b)b.click();})()" % sel)

            loads = [0]

            async def load_relay():
                loads[0] += 1
                await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin?n={loads[0]}#tab-relay/all"})
                ok = await until("(()=>{const ks=%s;return ks.every(k=>{const s=document.querySelector("
                                 "'.rl-panel[data-key=\"'+k+'\"] .rl-summary');return s&&!/Open this tab|Loading/"
                                 ".test(s.textContent)})&&!!document.getElementById('settingsForm')})()"
                                 % json.dumps([WORDS, DOMAINS, UPSTREAM]), 15)
                await ev("window.__submits=0;document.getElementById('settingsForm').addEventListener('submit',()=>"
                         "window.__submits++)")
                return ok

            async def settle(sel, want_rows=None, secs=5):
                """Wait for an add to land: the box cleared (and, when given, that many rows)."""
                return await until("(()=>{const p=%s;return p&&p.value===''&&(%s===null||p.rows.length===%s)})()"
                                   % (PANEL % sel, json.dumps(want_rows), json.dumps(want_rows)), secs)

            await call("Page.enable")
            await call("Runtime.enable")
            for width, mobile in ((390, True), (1280, False)):
                where = f"{width}px"
                await call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 860, "deviceScaleFactor": 1,
                                                                  "mobile": mobile})
                tag = "m" if mobile else "d"
                word, dom, up, topic = f"free crypto {tag}", f"spam-{tag}.example", f"wss://relay-{tag}.example", f"retro tech {tag}"
                if not await load_relay():
                    fails.append((where, "the relay tab never loaded its lists"))
                    break
                # --- the one box ---------------------------------------------------------------------------------
                for key in (WORDS, DOMAINS, UPSTREAM):
                    p = await panel(psel(key))
                    if not p or p["boxes"] != 1 or not p["boxShown"] or p["boxNamed"] \
                            or "search or add" not in p["placeholder"].lower():
                        fails.append((where, key, "not ONE visible, unnamed 'Search or add' box", p))
                    elif p["add"] is not None:
                        fails.append((where, key, "an Add action is offered with nothing typed", p))
                # --- Enter adds what was typed once ----------------------------------------------------------------
                before = (await panel(psel(WORDS)))["rows"]
                await type_into(psel(WORDS), word)
                p = await panel(psel(WORDS))
                if p["add"] != f'+ Add "{word}"' or not p["addOnScreen"]:
                    fails.append((where, "typing a new word does not offer '+ Add \"<text>\"' on screen", p))
                n_posts = len(posts)
                await enter()
                if not await settle(psel(WORDS), len(before) + 1):
                    fails.append((where, "Enter did not add the typed word", await panel(psel(WORDS))))
                p = await panel(psel(WORDS))
                if word not in p["rows"] or p["rows"][:len(before)] != before or p["value"] != "":
                    fails.append((where, "after Enter: box not cleared or the whole list not shown", p))
                if word not in p["lit"]:
                    fails.append((where, "the new row is not highlighted", p))
                if "Saved" not in p["msg"] or len(posts) != n_posts + 1:
                    fails.append((where, "the add did not go to the server once and say so", p["msg"], len(posts) - n_posts))
                if await ev("window.__submits"):
                    fails.append((where, "Enter in the box submitted the settings form"))
                # --- an exact match adds nothing ----------------------------------------------------------------
                n_posts = len(posts)
                await type_into(psel(WORDS), word.upper())
                p = await panel(psel(WORDS))
                if p["add"] is not None or not p["exists"]:
                    fails.append((where, "an exact match (other case) offers Add / does not say 'already in the list'", p))
                if p["rows"] != [word]:
                    fails.append((where, "typing does not filter to the matching row", p))
                await enter()
                await asyncio.sleep(.5)
                p = await panel(psel(WORDS))
                if len(posts) != n_posts or p["rows"] != [word]:
                    fails.append((where, "Enter on an exact match sent an add / duplicated", len(posts) - n_posts, p))
                await type_into(psel(WORDS), "")
                # --- filtering ---------------------------------------------------------------------------------
                await type_into(psel(WORDS), "crypto " + tag)
                p = await panel(psel(WORDS))
                if p["rows"] != [word]:
                    fails.append((where, "filtering 'crypto' does not narrow the list", p))
                await type_into(psel(WORDS), "")
                # --- "+ Add" with a domain typed as a URL: stored normalised -------------------------------------
                n_dom = len((await panel(psel(DOMAINS)))["rows"])
                await type_into(psel(DOMAINS), f"https://{dom.upper()}/some/path")
                await tap_add(psel(DOMAINS))
                if not await settle(psel(DOMAINS)):
                    fails.append((where, "'+ Add' did not add the domain", await panel(psel(DOMAINS))))
                p = await panel(psel(DOMAINS))
                if dom not in p["rows"] or len(p["rows"]) != n_dom + 1 or dom not in p["lit"]:
                    fails.append((where, "the domain was not stored normalised / not highlighted", p))
                n_posts = len(posts)
                await type_into(psel(DOMAINS), dom.upper())
                p = await panel(psel(DOMAINS))
                if p["add"] is not None or not p["exists"]:
                    fails.append((where, "the same domain in capitals is offered as new", p))
                await enter()
                await asyncio.sleep(.4)
                if len(posts) != n_posts:
                    fails.append((where, "Enter on an existing domain sent an add"))
                await type_into(psel(DOMAINS), "")
                # --- upstream relay: trailing slash ------------------------------------------------------------
                await type_into(psel(UPSTREAM), up + "/")
                await enter()
                if not await settle(psel(UPSTREAM)):
                    fails.append((where, "Enter did not add the upstream relay", await panel(psel(UPSTREAM))))
                p = await panel(psel(UPSTREAM))
                if up not in p["rows"]:
                    fails.append((where, "the relay URL was not stored without its trailing slash", p))
                await type_into(psel(UPSTREAM), up)
                p = await panel(psel(UPSTREAM))
                if p["add"] is not None or not p["exists"]:
                    fails.append((where, "the relay without its slash is offered as new", p))
                if p["overflow"]:
                    fails.append((where, "the page overflows sideways"))
                await type_into(psel(UPSTREAM), "")
                # --- Save, restart, fresh load ------------------------------------------------------------------
                said = await ev(r"""(async()=>{document.querySelector('#settingsForm .save-btn').click();
                    for(let i=0;i<150;i++){await new Promise(r=>setTimeout(r,100));
                      const m=document.body.innerText.match(/(Settings saved!|Not saved:[^\n]*|Failed to save settings|Nothing changed\.|Error saving settings)/);
                      if(m){const ok=[...document.querySelectorAll('button')].find(b=>/^(OK|Ok|Close)$/.test(b.textContent.trim())&&b.offsetParent!==null);
                        if(ok)ok.click();return m[0];}}return '';})()""")
                if said not in ("Settings saved!", "Nothing changed."):
                    fails.append((where, "the page's Save did not succeed after the adds", said))
                held = relay_values(relay, {WORDS, DOMAINS, UPSTREAM})
                for k, v in ((WORDS, word), (DOMAINS, dom), (UPSTREAM, up)):
                    if v not in (held.get(k) or "").split("\n"):
                        fails.append((where, k, "the relay document does not hold the added entry", held.get(k)))
                restart()
                if not await load_relay():
                    fails.append((where, "after a restart the relay tab never loaded"))
                else:
                    for k, v in ((WORDS, word), (DOMAINS, dom), (UPSTREAM, up)):
                        p = await panel(psel(k))
                        if v not in p["rows"]:
                            fails.append((where, k, "after Save + restart + reload the entry is not in the list", p["rows"]))
                    if (await ev("document.getElementById(%s).value" % json.dumps(WORDS))).count(word) != 1:
                        fails.append((where, "the words text box (what Save sends) does not hold the word once"))

                # --- Admin → Bots → Edit: topics -----------------------------------------------------------------
                bsel = json.dumps('.lf-panel[data-for="bot_f_auto_post_topics"]')

                async def open_bot():
                    loads[0] += 1
                    await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin?n={loads[0]}#tab-bots"})
                    await until("!!document.querySelector('[data-tab=\"bots\"]')&&typeof openBotModal==='function'", 15)
                    await ev("document.querySelector('[data-tab=\"bots\"]').click()")
                    if not await until("typeof _bots!=='undefined'&&!!_bots[7]", 10):
                        return False
                    await ev("openBotModal(7);for(let a=document.getElementById('bot_f_auto_post_topics');a;a=a.parentElement)"
                             "if(getComputedStyle(a).display==='none')a.style.display='block';")
                    return await until("(()=>{const p=%s;return p&&p.rows.length>0})()" % (PANEL % bsel), 10)

                if not await open_bot():
                    fails.append((where, "the bot editor never drew the topics list", await panel(bsel)))
                    continue
                p = await panel(bsel)
                if p["boxes"] != 1 or not p["boxShown"] or p["boxNamed"] or "search or add" not in p["placeholder"].lower():
                    fails.append((where, "bot topics: not ONE visible, unnamed 'Search or add' box", p))
                start = p["rows"]
                await type_into(bsel, topic)
                p = await panel(bsel)
                if p["add"] != f'+ Add "{topic}"' or not p["addOnScreen"]:
                    fails.append((where, "bot topics: typing a new topic does not offer '+ Add' on screen", p))
                await enter()
                if not await settle(bsel, len(start) + 1):
                    fails.append((where, "bot topics: Enter did not add", await panel(bsel)))
                p = await panel(bsel)
                if topic not in p["rows"] or topic not in p["lit"] or "press Save" not in p["msg"]:
                    fails.append((where, "bot topics: added row not shown in the whole list / highlighted / said", p))
                tap = f"anime {tag}"
                await type_into(bsel, tap)
                await tap_add(bsel)
                if not await settle(bsel, len(start) + 2):
                    fails.append((where, "bot topics: '+ Add' did not add", await panel(bsel)))
                await type_into(bsel, "GAMING")
                p = await panel(bsel)
                if p["add"] is not None or not p["exists"] or p["rows"] != ["gaming"]:
                    fails.append((where, "bot topics: an exact match is offered as new / not filtered", p))
                n_posts = len(posts)
                await enter()
                await asyncio.sleep(.5)
                await type_into(bsel, "")
                p = await panel(bsel)
                if p["rows"].count("gaming") != 1 or len(p["rows"]) != len(start) + 2 or len(posts) != n_posts:
                    fails.append((where, "bot topics: Enter on an exact match duplicated it", p))
                await type_into(bsel, "tech")
                p = await panel(bsel)
                if p["rows"] != [topic]:
                    fails.append((where, "bot topics: filtering does not narrow the list", p))
                await type_into(bsel, "")
                await ev("document.querySelector('#botModal [onclick=\"saveBot()\"]').click()")
                await until("document.getElementById('botModal').style.display==='none'", 8)
                saved = bots[7]["config"].get("auto_post_topics", "")
                if topic not in saved or tap not in saved:
                    fails.append((where, "bot topics: the bot's Save did not send the added topics", saved))
                if not await open_bot():
                    fails.append((where, "bot topics: the reopened bot drew no list"))
                    continue
                p = await panel(bsel)
                if topic not in p["rows"] or tap not in p["rows"] or p["rows"].count("gaming") != 1:
                    fails.append((where, "bot topics: a fresh load does not show the saved topics", p))
                bots[7]["config"]["auto_post_topics"] = "gaming\ncomics"
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
    print("Admin lists: one 'Search or add' box -- Enter and '+ Add' add once, exact matches not duplicated, "
          "filtering works, and the entries survive Save + restart + reload (relay lists) and the bot's Save "
          "+ reload (bot topics), at 390px and 1280px")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
