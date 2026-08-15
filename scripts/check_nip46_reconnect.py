#!/usr/bin/env python3
"""Does a NIP-46 session SURVIVE — a relay that went away, and a signer that was not listening?

Both failures were reported from one machine on one morning, and neither is about the signer:

  1. "I just woke my PC up and every action says `signer not connected`. Refreshing fixed it."
     Every socket closes when a machine suspends. The client's reconnect was ONE attempt, two
     seconds later — which on a resume lands before the wifi has associated — and its failure was
     swallowed by a bare `.catch(()=>{})`. Nothing was scheduled after it, so the session stayed
     `_wantOpen` with no sockets for the rest of the page's life.

  2. "Waiting for your signer… and the draft never sends. I had to click send again and it worked."
     A kind-24133 is EPHEMERAL: our relay fans it out to whoever is subscribed at that instant and
     stores nothing (`elif kind == 24133` in nostr_relay/server.py says so). A signer whose socket
     is redialling does not get a late copy — the request is destroyed, and this end waits out its
     full 120s ceiling for an answer that cannot come. The second click is what landed.

Both are driven here against the SHIPPED client in a real browser, with a real relay that is really
killed and a real bunker that is really absent. Neither case can be checked from a warm, healthy
page — which is exactly why they survived several rounds of "the signer is fixed".

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The relay, the fake bunker and the login script are the SAME ones check_nip46_signer.py drives.
# A second copy of a fake Amber is a second thing to keep correct, and the two checks would drift.
import check_nip46_signer as sig            # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9494)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-nip46-reconnect"


class Relay(sig.MiniRelay):
    """A MiniRelay that can be killed and brought back up ON THE SAME PORT.

    The port has to survive, because the client is holding a session pinned to that URL — a relay
    that comes back somewhere else is a different relay, and the reconnect being tested would have
    nothing to reconnect to.
    """

    def __init__(self):
        super().__init__()
        self._want_port = 0

    async def start(self):
        import websockets
        self._srv = await websockets.serve(self._client, "127.0.0.1", self._want_port)
        self.port = self._srv.sockets[0].getsockname()[1]
        self._want_port = self.port
        return f"ws://127.0.0.1:{self.port}"

    async def kill(self):
        """Down, and every open connection with it — a suspended machine's sockets do not linger."""
        for ws in list(self.subs):
            try:
                await ws.close()
            except Exception:
                pass
        await self.stop()
        self._srv = None
        await asyncio.sleep(0.2)


class Blackhole:
    """A TCP proxy that can go SILENT without closing — a carrier NAT dropping an idle mapping.

    This is the state a suspended machine actually comes back to, and it is the one a test cannot
    produce any other way: the browser still reports `readyState === 1`, `_live()` still counts the
    socket, every send() succeeds, and nothing is ever delivered in either direction. No error is
    raised anywhere, which is why it reads as "the signer stopped answering".

    Connections opened AFTER freeze() work normally — that is the whole point of the shape. The
    mapping for the OLD connection is gone; the network is fine. So an end that merely re-sends into
    its existing socket is lost for ever, and only one that DOUBTS THE SOCKET recovers.
    """

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.conns = []
        self._srv = None
        self.url = ""

    async def start(self):
        self._srv = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        p = self._srv.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{p}"
        return self.url

    def freeze(self):
        for c in self.conns:
            c["frozen"] = True

    async def _pump(self, reader, writer, conn):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                if conn["frozen"]:
                    continue            # swallowed, and the socket stays open — that is the bug
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _accept(self, cr, cw):
        conn = {"frozen": False}
        self.conns.append(conn)
        try:
            ur, uw = await asyncio.open_connection(self.host, self.port)
        except Exception:
            cw.close()
            return
        await asyncio.gather(self._pump(cr, uw, conn), self._pump(ur, cw, conn))

    async def stop(self):
        if self._srv:
            self._srv.close()
            try:
                await self._srv.wait_closed()
            except Exception:
                pass


def clone_bunker(old, relay_url):
    """The same signer, dialling back in. Same keys: to the client this is one identity throughout."""
    b = sig.Bunker(relay_url, reads="both")
    b.sk, b.pk = old.sk, old.pk
    b.user_sk, b.user_pk = old.user_sk, old.user_pk
    return b


# Sign an event WITHOUT publishing it (__PC.sign). Deliberately a sign_event and not an encrypt:
# signing is the interactive lane, which is the one a person is standing there waiting for, and it
# is the only lane the ephemeral-drop resend applies to.
SIGNJS = r"""(async (ms) => {
  const t0 = Date.now();
  try{
    const ev = await Promise.race([
      window.__PC.sign(1, 'reconnect probe ' + t0, []),
      new Promise((_,rej)=>setTimeout(()=>rej(new Error('gave up after ' + ms + 'ms')), ms)),
    ]);
    return { ok: !!(ev && ev.sig), took: Date.now() - t0 };
  }catch(e){ return { ok: false, took: Date.now() - t0, err: String((e && e.message) || e) }; }
})"""


async def drive(url):
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    import websockets

    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    relay = Relay()
    relay_url = await relay.start()
    bunker = clone_bunker(sig.Bunker(relay_url), relay_url)
    bunker.start()
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:800])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")

            async def load():
                await call("Page.navigate", {"url": url})
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("!!document.querySelector('#btn-amber')"):
                        return True
                return False

            if not await load():
                print("SKIP  the client never finished loading")
                return 2
            await js("try{ localStorage.clear(); sessionStorage.clear(); }catch(_){}")
            if not await load():
                print("SKIP  the client never finished loading")
                return 2

            uri = ("bunker://" + bunker.pk + "?relay="
                   + urllib.parse.quote(relay_url, safe="") + "&secret=s3cret")
            r = await js(f"({sig.LOGIN})({json.dumps(uri)})", awaited=True) or {}
            if not r.get("ok"):
                print(f"SKIP  could not log in to set the case up ({r.get('err') or 'no answer'})")
                return 2

            # A mark on the page. Every assertion below is about a session that was NOT reloaded —
            # reloading is the workaround being removed, so a check that quietly reloaded would pass
            # while the bug is still there.
            await js("window.__PC_MARK = 'alive';")

            async def signs(ms, why):
                got = await js(f"({SIGNJS})({ms})", awaited=True) or {}
                if not await js("window.__PC_MARK === 'alive'"):
                    problems.append((why, "the page reloaded", "the workaround, not the fix"))
                return got

            # ---- baseline: the session works at all -------------------------------------------
            base = await signs(20000, "baseline")
            if not base.get("ok"):
                print(f"SKIP  a fresh session could not sign ({base.get('err')}) — nothing to test")
                return 2

            # ---- 1. the relay goes away and comes back (the machine slept) ---------------------
            bunker.stop()
            await relay.kill()
            # Long enough that the OLD one-shot retry (2s) has fired and failed. That is the whole
            # difference between the two versions and it is worth being explicit about.
            await asyncio.sleep(7)
            relay_url2 = await relay.start()
            assert relay_url2 == relay_url, "the relay came back on a different port"
            bunker = clone_bunker(bunker, relay_url)
            bunker.start()
            await asyncio.sleep(1.0)

            got = await signs(45000, "relay-restart")
            if not got.get("ok"):
                problems.append(("relay-restart",
                                 got.get("err") or "the session never came back",
                                 "the socket died and nothing redialled it — refreshing the page "
                                 "is the only cure, which is the bug"))
            elif os.environ.get("PC_DEBUG"):
                print(f"  DEBUG relay-restart signed in {got.get('took')}ms", flush=True)

            # ---- 2. the signer is not listening when the request goes out ----------------------
            # The relay stays UP: only the signer is away. The request is fanned out to nobody and
            # is GONE — no relay stores it — so unless this end notices and sends it again, the
            # only thing left is the 120s ceiling. The budget below is well under that on purpose.
            if not problems:
                bunker.stop()
                await asyncio.sleep(0.5)
                pending = asyncio.ensure_future(signs(60000, "signer-away"))
                await asyncio.sleep(6)
                bunker = clone_bunker(bunker, relay_url)
                bunker.start()
                got = await pending
                if not got.get("ok"):
                    problems.append(("signer-away",
                                     got.get("err") or "the request was never answered",
                                     "published while the signer's socket was down, and a 24133 "
                                     "that reaches nobody is destroyed, not queued"))
                elif got.get("took", 0) > 55000:
                    problems.append(("signer-away",
                                     f"answered only after {got['took']}ms",
                                     "that is the 120s ceiling and _send's retry doing the work — "
                                     "the request was never re-sent while the user waited"))
                elif os.environ.get("PC_DEBUG"):
                    print(f"  DEBUG signer-away signed in {got.get('took')}ms", flush=True)

            # ---- 3. the socket is a ZOMBIE (the shape a suspend really leaves behind) ----------
            # Everything looks connected: readyState 1, every send() succeeds, nothing is delivered.
            # A fresh session on a proxied relay, because the session is pinned to the URL it paired
            # on and this case needs one that can be silenced under it.
            if not problems:
                hole = Blackhole("127.0.0.1", relay.port)
                hole_url = await hole.start()
                try:
                    if not await load():
                        print("SKIP  the client never finished loading")
                        return 2
                    await js("try{ localStorage.clear(); sessionStorage.clear(); }catch(_){}")
                    if not await load():
                        print("SKIP  the client never finished loading")
                        return 2
                    uri2 = ("bunker://" + bunker.pk + "?relay="
                            + urllib.parse.quote(hole_url, safe="") + "&secret=s3cret")
                    r2 = await js(f"({sig.LOGIN})({json.dumps(uri2)})", awaited=True) or {}
                    if not r2.get("ok"):
                        print(f"SKIP  could not log in through the proxy ({r2.get('err')})")
                        return 2
                    await js("window.__PC_MARK = 'alive';")
                    hole.freeze()
                    got = await signs(70000, "zombie-socket")
                    if not got.get("ok"):
                        problems.append(("zombie-socket",
                                         got.get("err") or "never recovered",
                                         "the socket reads OPEN and delivers nothing — re-sending "
                                         "into it is as lost as the first time; only tearing it "
                                         "down and redialling recovers"))
                    elif os.environ.get("PC_DEBUG"):
                        print(f"  DEBUG zombie-socket signed in {got.get('took')}ms", flush=True)
                finally:
                    await hole.stop()
    finally:
        try:
            bunker.stop()
        except Exception:
            pass
        await relay.stop()
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for name, err, detail in problems:
            print(f"  [{name}] {err} — {detail}")
        return 1
    print("OK  a NIP-46 session survives a dead relay and a signer that was not listening")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    try:
        with urllib.request.urlopen(BASE + "/client", timeout=8) as r:
            if r.status != 200:
                raise RuntimeError(r.status)
    except Exception as e:
        print(f"SKIP  {BASE}/client is not reachable ({e})")
        return 2
    return asyncio.run(drive(BASE + "/client"))


if __name__ == "__main__":
    sys.exit(main())
