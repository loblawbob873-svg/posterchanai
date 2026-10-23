#!/usr/bin/env python3
"""Is Concord federation WORKING right now, or is the room just quiet? A live canary against production.

"Not seeing any room activity" has two answers that look the same from a chair: nobody wrote, or messages are not
getting through. Telling them apart used to mean querying relays by hand. This makes a throwaway community owned by
the test identity, posts a unique marker in its #general and times every hop the marker takes:

  send       browser A (the live-journey identity, signed in through the NIP-07 button like verify_live_signin.py)
             types the marker into #general's composer and presses Send; the page hands a kind-1059 envelope to its
             sockets (captured from WebSocket.send, so the envelope id is exact, never guessed from timestamps)
  node       that envelope is on poster.place's relay -- asked the way the client asks this node for a mirrored
             stream: a `pcmp…` subscription for kinds [1059], authors [the channel's stream key], ids [the envelope]
  cord       the envelope is on at least one of the community's OWN CORD relays (the bundle's relays that are not
             this node) -- asked directly, from this machine
  live       browser B -- a second, separately launched Chrome profile signed in as the same identity, ALREADY
             sitting in that room -- shows the marker without a reload ("do other devices see activity")

THE COMMUNITY LIVES FOR ONE RUN. "pc-federation-canary" is created through Concord's own create dialog at the start
and, in a `finally`, DISSOLVED by its owner (Community settings -> Dissolve community…) and LEFT on both devices,
whether the hops passed or not. A run first dissolves and leaves any "pc-federation-canary" an interrupted run left
behind, and ends by reloading A and checking that none comes back from the membership vault. Nothing is ever posted
in anybody else's community. Both Chrome profiles persist in ~/.cache/posterchanai/fed-canary/ (two devices that stay
signed in), so a run does not start every one of the account's communities cold.

    venv-unified/bin/python scripts/check_concord_federation_live.py [https://poster.place]

Exit 0 = every hop passed. 1 = federation is broken; the line names the hop, and the evidence follows (what each
relay answered, what browser B showed, both pages' PCConcord.liveDiag()). 2 = could not run (no key, no Chrome,
sign-in or the canary room could not be made or reached, or the cleanup could not be confirmed) -- never a pass.
Registered in checkall as "manual": it writes to public relays, so ./test.sh and the deploy gate never run it.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_live_signin import CHROME, KEY_FILE, SHIM, Page, _free_port  # noqa: E402  (same identity, shim, CDP tab)

NAME = "pc-federation-canary"
PROFILES = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "posterchanai" / "fed-canary"
NODE_RELAY = os.environ.get("PC_CANARY_NODE_RELAY", "wss://relay.poster.place")
HOP_SECS = 120          # how long each hop may take before it counts as broken
DISSOLVED: set = set()  # communities this run dissolved (either device -- same owner): the other device only leaves


class CouldNotRun(Exception):
    """The harness could not get to the point of measuring -- exit 2, never a verdict about federation."""


# Every EVENT the page sends, every OK a relay answers for one, and every toast -- before the page's own code.
# A tunnelled socket's URL carries the member ticket (`t`); only the relay it targets (`u`) is ever recorded.
HOOK = r"""
(()=>{if(window.__fcHook)return;window.__fcHook=1;window.__fcSent=[];window.__fcOk=[];window.__fcToasts=[];
 const target=ws=>{try{const u=new URL(ws.url);return u.searchParams.get('u')||(u.origin+u.pathname);}catch(_){return '?';}};
 const S=WebSocket.prototype.send;
 WebSocket.prototype.send=function(d){try{if(typeof d==='string'&&d.startsWith('["EVENT"')){
   const m=JSON.parse(d),ev=m[1];if(ev&&ev.id){const to=target(this);
    window.__fcSent.push({id:ev.id,kind:ev.kind,pubkey:ev.pubkey,to,at:Date.now()});if(window.__fcSent.length>2000)window.__fcSent.splice(0,500);
    if(!this.__fcOkHook){this.__fcOkHook=1;this.addEventListener('message',e=>{try{if(typeof e.data==='string'&&e.data.startsWith('["OK"')){
      const o=JSON.parse(e.data);window.__fcOk.push({id:o[1],ok:o[2],msg:String(o[3]||'').slice(0,160),from:to,at:Date.now()});}}catch(_){}});}}}}
  catch(_){}return S.call(this,d);};
 const watch=()=>{try{new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes)if(n.classList&&n.classList.contains('toast'))
   window.__fcToasts.push(new Date().toISOString().slice(11,19)+' '+n.textContent);}).observe(document.body,{childList:true,subtree:true});}catch(_){}};
 if(document.body)watch();else document.addEventListener('DOMContentLoaded',watch);
})();
"""


def say(msg: str) -> None:
    print(msg, flush=True)


def _ours(url: str, base: str) -> bool:
    """A relay that is this node under any of its names (poster.place/relay, relay.poster.place, …)."""
    try:
        h, o = urlparse(url).hostname or "", urlparse(base).hostname or ""
    except ValueError:
        return False
    return bool(o) and (h == o or h.endswith("." + o))


# ---------------------------------------------------------------------------------------------------------------
# Relays, asked from this machine.
# ---------------------------------------------------------------------------------------------------------------
async def ask_relay(url: str, sub_prefix: str, flt: dict, timeout: float = 10) -> tuple[list, str]:
    """(events, what the relay said) for one REQ. Never raises: a relay that fails is an answer too."""
    import websockets
    sub = sub_prefix + secrets.token_hex(4)
    got, said = [], []
    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(url, open_timeout=timeout, max_size=None) as ws:
                await ws.send(json.dumps(["REQ", sub, flt]))
                async for raw in ws:
                    m = json.loads(raw)
                    if m[0] == "EVENT" and len(m) > 2:
                        got.append(m[2])
                    elif m[0] == "EOSE":
                        said.append(f"EOSE ({len(got)} event(s))")
                        break
                    elif m[0] == "CLOSED":
                        said.append("CLOSED: " + str(m[2] if len(m) > 2 else "")[:200])
                        break
                    elif m[0] == "AUTH":
                        said.append("AUTH challenge")
                    elif m[0] == "NOTICE":
                        said.append("NOTICE: " + str(m[1])[:200])
                try:
                    await asyncio.wait_for(ws.send(json.dumps(["CLOSE", sub])), 2)
                except Exception:       # noqa: BLE001 -- closing politely is best effort
                    pass
    except TimeoutError:
        said.append(f"no EOSE in {timeout:.0f} s")
    except Exception as e:      # noqa: BLE001
        said.append(f"{type(e).__name__}: {str(e)[:160]}")
    return got, "; ".join(said) or "nothing"


async def wait_on_relay(url: str, sub_prefix: str, flt: dict, want: str, t0: float, deadline: float) -> dict:
    """Ask `url` again every 2 s until it holds event `want`. {ok, secs, said}."""
    said = ""
    while time.monotonic() < deadline:
        events, said = await ask_relay(url, sub_prefix, flt, timeout=min(10, max(2, deadline - time.monotonic())))
        if any(e.get("id") == want for e in events):
            return {"ok": True, "secs": round(time.monotonic() - t0, 1), "said": said}
        await asyncio.sleep(2)
    return {"ok": False, "secs": round(time.monotonic() - t0, 1), "said": said}


# ---------------------------------------------------------------------------------------------------------------
# One Chrome = one device.
# ---------------------------------------------------------------------------------------------------------------
class Device:
    def __init__(self, label: str, base: str, sk_hex: str):
        self.label, self.base, self.sk = label, base, sk_hex
        self.prof = PROFILES / f"profile-{label.lower()}"
        self.proc = self.page = self.pump = self._ws = None
        self.pk = ""

    async def start(self):
        import httpx
        import websockets
        self.prof.mkdir(parents=True, exist_ok=True)
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (self.prof / lock).unlink()
            except OSError:
                pass
        # The runner's port for the first device (checkall runs browser checks concurrently and hands each one
        # PC_CHECK_PORT); the second device asks the OS for a free one.
        port = int(os.environ.get("PC_CHECK_PORT") or 0) if self.label == "A" else 0
        port = port or _free_port()
        self.proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1366,900",
                                      "--no-first-run", "--disable-background-timer-throttling",
                                      "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                                      f"--remote-debugging-port={port}", f"--user-data-dir={self.prof}", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        target = None
        for _ in range(150):
            try:
                target = next(t for t in httpx.get(f"http://127.0.0.1:{port}/json").json() if t.get("type") == "page")
                break
            except Exception:       # noqa: BLE001
                await asyncio.sleep(0.1)
        if not target:
            raise CouldNotRun(f"{self.label}: Chrome did not start ({CHROME})")
        self._ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=None)
        self.page = Page(self._ws, urlparse(self.base).netloc)
        self.pump = asyncio.create_task(self.page.pump())
        for m in ("Runtime.enable", "Page.enable"):
            await self.page.call(m)
        bundle = (ROOT / "static/vendor/nostr/nostr.bundle.js").read_text()
        await self.page.call("Page.addScriptToEvaluateOnNewDocument", {
            "source": "(()=>{" + bundle + "\n;window.__NT=NostrTools;})();window.__LJ_SK=" + json.dumps(self.sk) + ";" + SHIM + HOOK})

    async def stop(self):
        if self.pump:
            self.pump.cancel()
        try:
            if self._ws:
                await self._ws.close()
        except Exception:       # noqa: BLE001
            pass
        if self.proc:
            try:
                os.killpg(self.proc.pid, 15)
                self.proc.wait(10)
            except Exception:       # noqa: BLE001
                try:
                    os.killpg(self.proc.pid, 9)
                except OSError:
                    pass

    async def js(self, expr, timeout=30):
        return await self.page.js(expr, timeout)

    async def until(self, expr, secs):
        return await self.page.until(expr, secs)

    async def text(self, sel=".cc-app", n=300):
        try:
            return await self.js("((document.querySelector(%s)||{}).innerText||'').replace(/\\s+/g,' ').slice(-%d)" % (json.dumps(sel), n))
        except Exception as e:      # noqa: BLE001
            return f"<page did not answer: {e}>"

    async def toasts(self, n=6):
        try:
            return await self.js("(window.__fcToasts||[]).slice(-%d)" % n)
        except Exception:       # noqa: BLE001
            return []

    async def diag(self):
        try:
            return await self.js("(()=>{try{return PCConcord.liveDiag()}catch(e){return 'liveDiag: '+e.message}})()")
        except Exception as e:      # noqa: BLE001
            return f"<page did not answer: {e}>"

    async def login(self):
        """The NIP-07 button a person presses, then the app session -- verify_live_signin.py's steps. A persistent
        profile may already be signed in; that is a device that stayed signed in, which is fine."""
        await self.page.call("Page.navigate", {"url": self.base + "/"})
        me = "!!(window.__PC && __PC.me && __PC.me() && __PC.me().pubkey)"
        if await self.until("(()=>{const b=document.querySelector('#btn-nip07');return (!!b&&typeof b.onclick==='function')||" + me + "})()", 60) is None:
            raise CouldNotRun(f"{self.label}: no sign-in button and nobody signed in after 60 s: {await self.text('body', 160)}")
        if not await self.js(me):
            await self.js("(()=>{const b=document.querySelector('#btn-nip07'); if(b) b.click(); return 1})()")
        if await self.until(me, 60) is None:
            raise CouldNotRun(f"{self.label}: the NIP-07 sign-in never completed: {await self.text('body', 160)}")
        if await self.until("(async()=>{try{if(window.__PC&&__PC.ensureAiSession)await __PC.ensureAiSession();}catch(_){}"
                            "const r=await fetch('/api/auth/me',{credentials:'include'});return r.ok})()", 60) is None:
            raise CouldNotRun(f"{self.label}: signed in on the page, but the server never gave this account a session")
        self.page.signed_in = True
        self.pk = await self.js("__PC.me().pubkey")
        return self.pk

    async def reload(self, settle=True):
        """A fresh page (its Concord start restores memberships from the vault), signed in, in Communities."""
        await self.login()
        await self.open_concord()
        if settle:
            await self.settle_rail(quiet=10, most=60)

    async def open_concord(self):
        await self.js("__PC.switchMessagesTab('concord'); 1")
        if await self.until("document.body.classList.contains('concord-view') && !!document.querySelector('.cc-app')", 45) is None:
            raise CouldNotRun(f"{self.label}: Communities did not open")

    async def rooms(self) -> list:
        """This device's saved rooms: [{name, id, dissolved, owned}]."""
        return await self.js("(()=>{let a=[];try{a=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+%s)||'[]')}catch(_){}"
                             "return (Array.isArray(a)?a:[]).filter(r=>r).map(r=>{const b=(r.cord&&r.cord.bundle)||{};"
                             "return {name:r.name||'',id:r.communityId||r.naddr||'',dissolved:!!b.dissolved,owned:b.owner===%s}})})()"
                             % (json.dumps(self.pk), json.dumps(self.pk))) or []

    async def saved_room(self, cid):
        return await self.js("(()=>{try{const a=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+%s)||'[]');"
                             "return a.find(r=>r&&(r.communityId||r.naddr)===%s)||null}catch(_){return null}})()" % (json.dumps(self.pk), json.dumps(cid)))

    async def settle_rail(self, quiet=8, most=45):
        """Wait until the membership restore stops adding rooms (the rail unchanged for `quiet` s, at most `most`)."""
        last, since, t0 = None, time.monotonic(), time.monotonic()
        while time.monotonic() - t0 < most:
            now = json.dumps(sorted(r["id"] for r in await self.rooms()))
            if now != last:
                last, since = now, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return
            await asyncio.sleep(1)

    async def open_room(self, cid):
        """Press the rail button of the saved room `cid` and wait until it is the open community. The rail is redrawn
        a moment after a join or a membership restore, so a press that found no button (or did not take) is retried."""
        for _ in range(4):
            idx = await self.js("(()=>{try{const a=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+%s)||'[]');"
                                "return a.findIndex(r=>r&&(r.communityId||r.naddr)===%s)}catch(_){return -1}})()" % (json.dumps(self.pk), json.dumps(cid)))
            if idx is None or idx < 0:
                return False
            await self.until("!!document.querySelector('[data-cc-server=\"%d\"]')" % idx, 20)
            await self.js("(()=>{const b=document.querySelector('[data-cc-server=\"%d\"]');if(b)b.click();return 1})()" % idx)
            if await self.until("(()=>{const b=document.querySelector('.cc-server.active');return !!b&&b.dataset.ccServer===%s}"
                                ")()" % json.dumps(str(idx)), 15) is not None:
                return True
            await asyncio.sleep(3)
        return False

    async def mark(self):
        return await self.js("Date.now()")

    async def publishes_since(self, ms) -> str:
        """What the page published since `ms`, per relay: kind, how long until the relay said OK (or that it did not)."""
        rows = await self.js("(()=>{const ok=window.__fcOk||[];return (window.__fcSent||[]).filter(s=>s.at>=%d).map(s=>{"
                             "const o=ok.find(o=>o.id===s.id&&o.from===s.to);return s.kind+'→'+s.to.replace(/^wss?:\\/\\//,'')+' '"
                             "+(o?(o.ok?'OK':'REFUSED '+o.msg)+' '+((o.at-s.at)/1000).toFixed(1)+'s':'no answer')})})()" % int(ms)) or []
        return "; ".join(rows[-24:]) or "nothing published"

    async def _confirm(self, secs=15):
        return await self.until("(()=>{const b=document.querySelector('.uiconfirm [data-uc=\"1\"]');if(b){b.click();return true}return false})()", secs) is not None

    async def dissolve(self, cid) -> str:
        """Community settings -> Dissolve community… -> Dissolve, for a room this identity owns."""
        if not await self.open_room(cid):
            return f"{self.label}: could not open {cid[:12]}… to dissolve it"
        if await self.until("!!document.querySelector('#cc-dissolve')", 20) is None:
            return f"{self.label}: no Dissolve button for {cid[:12]}… (not the owner, or already dissolved)"
        t0 = await self.mark()
        await self.js("document.querySelector('#cc-dissolve').click();1")
        if not await self._confirm():
            return f"{self.label}: no Dissolve confirmation for {cid[:12]}…"
        ok = await self.until("(()=>{try{const a=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+%s)||'[]');const r=a.find(r=>r&&(r.communityId||r.naddr)===%s);"
                              "return !!(r&&r.cord&&r.cord.bundle&&r.cord.bundle.dissolved)}catch(_){return false}})()" % (json.dumps(self.pk), json.dumps(cid)), 240)
        if ok is not None:
            DISSOLVED.add(cid)
        trace = f"; published: {await self.publishes_since(t0)}" if ok is None or ok > 10 else ""
        return (f"{self.label}: dissolved {cid[:12]}… ({ok} s){trace}" if ok is not None
                else f"{self.label}: dissolve of {cid[:12]}… NOT confirmed in 240 s; toasts {await self.toasts(3)}{trace}")

    async def leave(self, cid) -> str:
        """Community settings' Leave community -> Leave: the room goes from this device and from the membership vault."""
        if not await self.open_room(cid):
            return f"{self.label}: could not open {cid[:12]}… to leave it"
        if await self.until("!!document.querySelector('#cc-leave-community')", 20) is None:
            return f"{self.label}: no Leave button for {cid[:12]}…"
        t0 = await self.mark()
        await self.js("document.querySelector('#cc-leave-community').click();1")
        if not await self._confirm():
            return f"{self.label}: no Leave confirmation for {cid[:12]}…"
        gone = await self.until("(()=>{try{const a=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+%s)||'[]');"
                                "return !a.some(r=>r&&(r.communityId||r.naddr)===%s)}catch(_){return false}})()" % (json.dumps(self.pk), json.dumps(cid)), 120)
        trace = f"; published: {await self.publishes_since(t0)}" if gone is None or gone > 10 else ""
        return (f"{self.label}: left {cid[:12]}… ({gone} s){trace}" if gone is not None
                else f"{self.label}: {cid[:12]}… still saved 120 s after Leave; toasts {await self.toasts(3)}{trace}")

    async def remove_canaries(self, dissolve: bool) -> list:
        """Dissolve (when `dissolve` and owned) and leave every room named NAME on this device."""
        notes = []
        for r in [r for r in await self.rooms() if r["name"] == NAME]:
            if dissolve and r["owned"] and not r["dissolved"] and r["id"] not in DISSOLVED:
                notes.append(await self.dissolve(r["id"]))
            notes.append(await self.leave(r["id"]))
        return notes

    async def create(self) -> str:
        """The community's own create dialog: "+" -> Create a community -> name -> Create on relays. Returns its id."""
        before = {r["id"] for r in await self.rooms()}
        await self.js("(window.__fcToasts||[]).length=0;1")
        m0 = await self.mark()
        await self.js("document.querySelector('#cc-add').click();1")
        if await self.until("!!document.querySelector('#cc-join-create')&&!document.querySelector('#cc-join').classList.contains('hidden')", 15) is None:
            raise CouldNotRun(f"{self.label}: the create/join dialog did not open")
        await self.js("document.querySelector('#cc-join-create').click();1")
        if await self.until("!document.querySelector('#cc-create-dialog').classList.contains('hidden')", 15) is None:
            raise CouldNotRun(f"{self.label}: the create dialog did not open")
        await self.js("(()=>{const n=document.querySelector('#cc-community-name');n.value=%s;n.dispatchEvent(new Event('input',{bubbles:true}));"
                      "document.querySelector('#cc-create-go').click();return 1})()" % json.dumps(NAME))
        t0 = time.monotonic()
        while time.monotonic() - t0 < 300:
            new = [r for r in await self.rooms() if r["name"] == NAME and r["id"] not in before]
            failed = [t for t in await self.toasts(4) if "community creation failed" in t]
            if failed and not new:
                raise CouldNotRun(f"{self.label}: the create failed after {time.monotonic() - t0:.1f} s: {failed[-1]}; "
                                  f"published: {await self.publishes_since(m0)}")
            if new:
                say(f"      A: created {new[0]['id'][:16]}… in {time.monotonic() - t0:.1f} s")
                return new[0]["id"]
            await asyncio.sleep(1)
        raise CouldNotRun(f"{self.label}: '{NAME}' was not created in 300 s; toasts {await self.toasts()}")

    async def enter_general(self, cid):
        """Open the canary community's #general and wait until its pane says something (messages or 'start of')."""
        if not await self.open_room(cid):
            raise CouldNotRun(f"{self.label}: '{NAME}' did not become the open community")
        # #general is the default channel; press it if another one is open.
        await self.js("(()=>{const c=[...document.querySelectorAll('[data-cc-channel]')].find(x=>(x.dataset.ccChannel||'')==='general'||/^#?\\s*general$/i.test((x.innerText||'').trim()));"
                      "if(c&&!c.classList.contains('active'))c.click();return 1})()")
        if await self.until("(()=>{const m=document.querySelector('.cc-messages');if(!m)return false;return m.querySelectorAll('.cc-message').length>0"
                            "||/start of this|could not|not readable/i.test(m.innerText)})()", 90) is None:
            raise CouldNotRun(f"{self.label}: '{NAME}' #general drew nothing in 90 s: {await self.text('.cc-messages')}")
        shown = await self.text(".cc-messages", 200)
        if "could not" in shown.lower() or "not readable" in shown.lower():
            raise CouldNotRun(f"{self.label}: '{NAME}' #general did not open: {shown}")


async def measure(a: Device, b: Device, cid: str, base: str, hops: list, evidence: list) -> None:
    # A new room's saved channels carry their stream keys once the open room has hydrated from its control stream.
    room, general = None, None
    for dev in (a, b) * 30:
        room = await dev.saved_room(cid)
        general = next((c for c in (room or {}).get("channels") or [] if c.get("name") == "general"), None)
        if general and general.get("streamPubkeys"):
            break
        await asyncio.sleep(1)
    if not general or not general.get("streamPubkeys"):
        raise CouldNotRun(f"no #general stream key in the saved room after 60 s: {json.dumps((room or {}).get('channels'))[:300]}")
    streams = general["streamPubkeys"]
    relays = ((room.get("cord") or {}).get("bundle") or {}).get("relays") or []
    cord = [u for u in relays if not _ours(u, base) and not _ours(u, NODE_RELAY)]
    say(f"      #general stream key(s) {', '.join(s[:12] + '…' for s in streams)}; bundle relays: {', '.join(relays)}")
    if not cord:
        evidence.append(f"the bundle lists no relay other than this node: {relays}")
    # B must be LIVE, not merely painted: wait for its first completed live pass.
    lt = await b.until("(()=>{try{const d=PCConcord.liveDiag();return d.lastOkAgoMs!=null}catch(_){return false}})()", 60)
    say("      B's live sync: " + (f"a completed pass after {lt} s" if lt is not None else f"NO completed pass in 60 s {await b.diag()}"))

    marker = f"canary {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')} {secrets.token_hex(4)}"
    say(f"[4] A posts \"{marker}\"")
    await a.js("window.__fcSent.length=0;window.__fcOk.length=0;1")
    await a.js("(()=>{const i=document.querySelector('#cc-input');i.focus();i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));return 1})()"
               % json.dumps(marker))
    sent_ms = await a.js("Date.now()")
    t0 = time.monotonic()
    await a.js("document.querySelector('#cc-send').click();1")
    deadline = t0 + HOP_SECS

    async def seen_on_b():
        secs = await b.until("((document.querySelector('.cc-messages')||{}).innerText||'').includes(%s)" % json.dumps(marker),
                             max(1, deadline - time.monotonic()))
        return None if secs is None else round(time.monotonic() - t0, 1)

    b_task = asyncio.create_task(seen_on_b())
    frames = []
    while time.monotonic() < deadline and not frames:
        frames = await a.js("window.__fcSent.filter(s=>s.kind===1059&&%s.includes(s.pubkey))" % json.dumps(streams)) or []
        if not frames:
            await asyncio.sleep(0.25)
    if not frames:
        b_task.cancel()
        hops.append(("send", False, round(time.monotonic() - t0, 1), "A pressed Send but no kind-1059 envelope for #general's stream left the page"))
        evidence.append("A's pane: " + await a.text(".cc-main", 300))
        evidence.append(f"A's toasts: {await a.toasts()}")
        return
    env = frames[0]["id"]
    hops.append(("send", True, round((frames[0]["at"] - sent_ms) / 1000, 1),
                 f"envelope {env[:16]}… handed to {len({f['to'] for f in frames if f['id'] == env})} socket(s)"))
    flt = {"kinds": [1059], "authors": [frames[0]["pubkey"]], "ids": [env]}
    node_task = asyncio.create_task(wait_on_relay(NODE_RELAY, "pcmp", flt, env, t0, deadline))
    cord_tasks = {u: asyncio.create_task(wait_on_relay(u, "canary", flt, env, t0, deadline)) for u in cord}
    node = await node_task
    hops.append(("node", node["ok"], node["secs"], f"{NODE_RELAY}: {node['said']}"))
    first, answers, pending = None, {}, set(cord_tasks.values())
    while pending and not first:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            u = next(k for k, v in cord_tasks.items() if v is t)
            answers[u] = t.result()
            if answers[u]["ok"] and first is None:
                first = u
    hops.append(("cord", True, answers[first]["secs"], f"first on {first}") if first
                else ("cord", False, round(time.monotonic() - t0, 1), f"on none of {len(cord)} CORD relay(s)"))
    for u, t in cord_tasks.items():      # the rest, bounded, so the report says where else it landed
        if u not in answers:
            try:
                answers[u] = await asyncio.wait_for(t, max(1, min(15, deadline - time.monotonic())))
            except Exception:       # noqa: BLE001
                t.cancel()
                answers[u] = {"ok": False, "secs": None, "said": "still asking"}
    live = await b_task
    hops.append(("live", live is not None, live if live is not None else HOP_SECS,
                 "B showed it without a reload" if live is not None else f"B never showed it in {HOP_SECS} s"))
    evidence.append("per relay: " + "; ".join(f"{u} {'HAS it' if r['ok'] else 'missing'}"
                                               f"{'' if r['secs'] is None else ' @' + str(r['secs']) + 's'} ({r['said']})"
                                               for u, r in answers.items()))
    slow = [u for u, r in answers.items() if not r["ok"] or (r["secs"] or 0) > 10]
    if slow:
        # Not a failure (one CORD relay holding it is the hop), but the thing to look at when members elsewhere
        # see messages late: measured 2026-10-23, 0.7 s to all three on two runs, then 40-71 s to one and the
        # other two still without it on the next two.
        evidence.append(f"WARN slow or missing on {len(slow)} of {len(answers)} CORD relay(s): {', '.join(slow)}")
    oks = await a.js("window.__fcOk.filter(o=>o.id===%s).map(o=>o.from+(o.ok?' OK':' REFUSED '+o.msg))" % json.dumps(env))
    evidence.append(f"publish answers A's page got for the envelope: {oks or 'none'}")
    if not all(ok for _, ok, _, _ in hops):
        evidence.append("B's #general shows (tail): " + await b.text(".cc-messages", 300))
        evidence.append(f"B PCConcord.liveDiag(): {json.dumps(await b.diag())}")
        evidence.append(f"A PCConcord.liveDiag(): {json.dumps(await a.diag())}")


async def run(base: str, sk: str) -> int:
    a, b = Device("A", base, sk), Device("B", base, sk)
    hops, evidence, cleanup = [], [], []
    cid, setup_error = "", ""
    started = False
    try:
        say(f"[1] sign in twice: two Chrome profiles as the test identity ({PROFILES})")
        await asyncio.gather(a.start(), b.start())
        started = True
        pk_a, pk_b = await asyncio.gather(a.login(), b.login())
        if pk_a != pk_b:
            raise CouldNotRun("the two devices signed in as different identities")
        await asyncio.gather(a.open_concord(), b.open_concord())
        await asyncio.gather(a.settle_rail(), b.settle_rail())
        left = await a.remove_canaries(dissolve=True) + await b.remove_canaries(dissolve=False)
        for n in left:
            say(f"      leftover from an earlier run: {n}")

        say(f"[2] A creates '{NAME}' (it lives for this run only)")
        try:
            cid = await a.create()
        except CouldNotRun as e:
            # A create that throws half way may still have made the community AND written it to the membership vault
            # (seen 2026-10-23: "Community was created but its discovery announcement was not accepted"): it then
            # appears on the account's other devices. Clean that up, then try once more.
            say(f"      the create failed: {e}")
            await asyncio.gather(a.reload(), b.reload())
            for n in await a.remove_canaries(dissolve=True) + await b.remove_canaries(dissolve=True):
                say(f"      orphan of the failed create: {n}")
            cid = await a.create()

        say("[3] B (a second device) picks it up, both sit in #general")
        # B's Concord was started before the room existed: reload it once, BEFORE anything is measured, so its start
        # restores memberships the way a device opened after the create would.
        await b.reload(settle=False)
        t = await b.until("(()=>{try{const a=JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+%s)||'[]');return a.some(r=>r&&r.communityId===%s)}catch(_){return false}})()"
                          % (json.dumps(b.pk), json.dumps(cid)), 60)
        if t is None:
            say("      B: the membership vault did not bring it in 60 s -- saving A's room on B (what accepting the invite would)")
            room = await a.saved_room(cid)
            await b.js("(()=>{const k='pc.concord.rooms.v1.'+%s;let a=[];try{a=JSON.parse(localStorage.getItem(k)||'[]')}catch(_){}"
                       "if(!Array.isArray(a))a=[];a.push(%s);localStorage.setItem(k,JSON.stringify(a));try{PCConcord.render()}catch(_){}return 1})()"
                       % (json.dumps(b.pk), json.dumps(room)))
        else:
            say(f"      B: restored from the membership vault after {t} s")
        await asyncio.gather(a.enter_general(cid), b.enter_general(cid))
        await measure(a, b, cid, base, hops, evidence)
    except CouldNotRun as e:
        setup_error = str(e)
    except Exception as e:      # noqa: BLE001 -- anything else is the harness, and the cleanup must still run
        setup_error = f"{type(e).__name__}: {e}"
    finally:
        if started:
            say("[5] cleanup: dissolve the canary, leave it on both devices, and check it does not come back")
            # Same identity on both: either can dissolve. B first -- measured, the device that posted (A) took up to
            # four minutes to publish its dissolution while B's went out in under a second.
            for dev in (b, a):
                try:
                    cleanup += await asyncio.wait_for(dev.remove_canaries(dissolve=True), 420)
                except Exception as e:      # noqa: BLE001
                    cleanup.append(f"{dev.label}: cleanup failed: {type(e).__name__}: {e}")
            # A fresh load restores memberships from the vault: a left room must not come back. One that does (a
            # half-made create, a leave the vault missed) is dissolved and left again, and checked once more.
            for attempt in range(2):
                try:
                    await asyncio.gather(a.reload(), b.reload())
                    rest = {d.label: [r for r in await d.rooms() if r["name"] == NAME] for d in (a, b)}
                    cleanup.append(f"after a reload (check {attempt + 1}): " + ", ".join(
                        f"{k} holds {len(v)} '{NAME}'" + (f" {[(r['id'][:12], 'dissolved' if r['dissolved'] else 'LIVE') for r in v]}" if v else "")
                        for k, v in rest.items()))
                    if not any(rest.values()):
                        break
                    for dev in (a, b):
                        cleanup += await asyncio.wait_for(dev.remove_canaries(dissolve=True), 420)
                    if attempt:
                        setup_error = setup_error or f"cleanup not confirmed: {rest}"
                except Exception as e:      # noqa: BLE001
                    cleanup.append(f"could not re-check after cleanup: {type(e).__name__}: {e}")
                    setup_error = setup_error or "cleanup could not be re-checked"
                    break
        await asyncio.gather(a.stop(), b.stop(), return_exceptions=True)

    say("[6] report")
    for name, ok, secs, why in hops:
        say(f"  {'ok  ' if ok else 'FAIL'}  {name:5} {secs:6.1f}s  {why}")
    for line in evidence:
        say(f"        {line}")
    for line in cleanup:
        say(f"  cleanup: {line}")
    broken = [n for n, ok, _, _ in hops if not ok]
    if broken:
        say(f"FAIL: federation is broken at hop '{broken[0]}'" + (f" (also {', '.join(broken[1:])})" if broken[1:] else ""))
        return 1
    if setup_error or len(hops) < 4:
        say(f"COULD NOT RUN: {setup_error or 'the hops were not all measured'}")
        return 2
    say("OK: a message in a fresh community reached this node, the community's own relays and a second device, live")
    return 0


def main(argv) -> int:
    base = (argv[1] if len(argv) > 1 else "https://poster.place").rstrip("/")
    if not KEY_FILE.exists():
        print(f"COULD NOT RUN: no test identity ({KEY_FILE} holds a 64-hex secret key)")
        return 2
    sk = KEY_FILE.read_text().strip().lower()
    if len(sk) != 64 or any(c not in "0123456789abcdef" for c in sk):
        print(f"COULD NOT RUN: {KEY_FILE} is not a 64-hex secret key")
        return 2
    if not Path(CHROME).exists() or not shutil.which(CHROME):
        print(f"COULD NOT RUN: no Chrome at {CHROME}")
        return 2
    try:
        return asyncio.run(asyncio.wait_for(run(base, sk), 1500))
    except Exception as e:      # noqa: BLE001 -- could not run is never a pass
        print(f"COULD NOT RUN: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
