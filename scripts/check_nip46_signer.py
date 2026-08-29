#!/usr/bin/env python3
"""Remote-signer (NIP-46) login, driven end to end against a REAL bunker.

    venv-unified/bin/python scripts/check_nip46_signer.py [base_url]

Two throwaway relays are started here, a fake bunker is connected to ONE of them, and the shipped
client is driven through its own sign-in UI in headless Chrome — paste a bunker:// link, press
Connect, and see whether it ends up logged in. Nothing is stubbed on the client side: the app opens
its own sockets, encrypts with its own worker and matches its own request ids.

Each scenario is a way remote signing has actually broken:

  wrong-relay      The bunker link names several relays and the signer is not on the FIRST one.
                   Amber's links routinely carry four, and a resumed session adds this node's own
                   relay to the list — so a transport that keeps one socket (the fastest to open,
                   which is nearly always ours) publishes every request to a relay the signer is not
                   in. Nothing errors: the relay accepts the event and the app waits out its 120s
                   ceiling. Reported as "existing sessions say waiting for signer, and nothing ever
                   shows up in Amber". The request has to go to ALL of them.

  nip44-only       A spec-current signer that reads only NIP-44. NIP-46 has no capability
                   negotiation, so an app that always writes NIP-04 simply cannot talk to it.

  nip04-only       The long tail, which reads only NIP-04. The scheme has to be settled by trying,
                   and the fallback attempt has to actually happen.

  oversize-request NIP-44 has an absolute 65535-byte plaintext ceiling. A 100KB request must be
                   refused by the client with attachment guidance and must never reach the signer;
                   silently falling back to NIP-04 would change the requested protocol, while
                   chunking would invent an incompatible event format.

  slow-approval    The signer takes longer to answer the connect than the probe waits — which is not
                   an edge case, it is a human being asked to approve on a phone. The first attempt
                   must stay outstanding: a probe that retires its own request throws away the
                   approval when it finally arrives, and then waits for an answer to a request the
                   signer was never asked about again.

Exit 0 = clean, 1 = failures (printed), 2 = could not run (no Chrome / no instance / no websockets).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9489)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-nip46-check"


class CDPError(RuntimeError):
    """A Chrome DevTools command failed before producing a result."""


def _cdp_result(message, method):
    if not isinstance(message, dict):
        raise CDPError(f"Chrome returned no response for {method}")
    if message.get("error"):
        error = message["error"]
        raise CDPError(f"Chrome {method} failed ({error.get('code', '?')}): "
                       f"{error.get('message') or error}")
    if "result" not in message:
        raise CDPError(f"Chrome returned no result for {method}: {message}")
    return message["result"]

# --------------------------------------------------------------------------------------------
# A minimal NIP-01 relay: enough to carry a signer handshake and nothing else.
# --------------------------------------------------------------------------------------------
class MiniRelay:
    def __init__(self):
        self.subs = {}       # ws -> {subid: filter}
        self.port = None
        self._srv = None
        # Every event this relay was handed. A check that asks "did the client publish?" gets a
        # straight answer from the far side, in another process, instead of inferring it from the
        # page — which is what check_qr_scan.py needs, because a successful scan CLOSES the modal it
        # would otherwise have to watch.
        self.events = []

    async def start(self):
        import websockets
        self._srv = await websockets.serve(self._client, "127.0.0.1", 0)
        self.port = self._srv.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{self.port}"

    async def stop(self):
        if self._srv:
            self._srv.close()
            await self._srv.wait_closed()

    @staticmethod
    def _matches(f, ev):
        if "kinds" in f and ev["kind"] not in f["kinds"]:
            return False
        for key, want in f.items():
            if key.startswith("#"):
                tag = key[1:]
                have = {t[1] for t in ev.get("tags", []) if len(t) > 1 and t[0] == tag}
                if not have & set(want):
                    return False
        # `since` is deliberately ignored: this relay exists to prove routing, and a clock-skew
        # argument between two processes on one box is not what any of these scenarios is about.
        return True

    async def _client(self, ws):
        self.subs[ws] = {}
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                if m[0] == "REQ":
                    self.subs[ws][m[1]] = m[2] if len(m) > 2 else {}
                    await ws.send(json.dumps(["EOSE", m[1]]))
                elif m[0] == "CLOSE":
                    self.subs[ws].pop(m[1], None)
                elif m[0] == "EVENT":
                    ev = m[1]
                    self.events.append(ev)
                    await ws.send(json.dumps(["OK", ev["id"], True, ""]))
                    for peer, subs in list(self.subs.items()):
                        for sid, filt in list(subs.items()):
                            if self._matches(filt, ev):
                                try:
                                    await peer.send(json.dumps(["EVENT", sid, ev]))
                                except Exception:
                                    pass
        except Exception:
            pass
        finally:
            self.subs.pop(ws, None)


# --------------------------------------------------------------------------------------------
# A fake bunker: what Amber is, reduced to the parts the client talks to.
# --------------------------------------------------------------------------------------------
class Bunker:
    """reads: 'nip04' | 'nip44' | 'both'.  delay: seconds before answering a connect."""

    def __init__(self, relay_url, reads="both", delay=0.0):
        from app.services.nostr import bip340
        self.relay_url = relay_url
        self.reads = reads
        self.delay = delay
        self.sk = os.urandom(32)
        self.pk = bip340.pubkey_from_seckey(self.sk).hex()
        self.user_sk = os.urandom(32)
        self.user_pk = bip340.pubkey_from_seckey(self.user_sk).hex()
        self.seen = []           # every request method we decrypted, with its scheme
        self._activity = asyncio.Event()
        self._activity_seq = 0
        self._event_ids = set()  # a relay resend is the same signed event, not another approval
        self._approval_delayed = False
        self._task = None
        self._stop = False
        self.ready = asyncio.Event()
        self.error = None

    def _decrypt(self, peer_hex, ct):
        from app.services.nostr import nip04, nip44
        peer = bytes.fromhex(peer_hex)
        if "?iv=" in ct:
            if self.reads == "nip44":
                return None, None
            return nip04.decrypt(self.sk, peer, ct), "nip04"
        if self.reads == "nip04":
            return None, None
        return nip44.decrypt_from(self.sk, peer, ct), "nip44"

    def _encrypt(self, peer_hex, text, scheme):
        from app.services.nostr import nip04, nip44
        peer = bytes.fromhex(peer_hex)
        if scheme == "nip44":
            return nip44.encrypt_to(self.sk, peer, text)
        return nip04.encrypt(self.sk, peer, text)

    async def run(self):
        import websockets
        from app.services.nostr import event as nevent
        async with websockets.connect(self.relay_url) as ws:
            await ws.send(json.dumps(["REQ", "b1", {"kinds": [24133], "#p": [self.pk]}]))
            self.ready.set()
            while not self._stop:
                raw = await ws.recv()
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                if m[0] != "EVENT" or len(m) < 3:
                    continue
                ev = m[2]
                if ev.get("kind") != 24133:
                    continue
                # _RESEND_AT deliberately republishes identical signed events. A real signer
                # deduplicates by event id; sleeping once per duplicate in this serial fake blocks
                # its receive loop and turns a 16-second approval into a false 60-second timeout.
                if ev.get("id") in self._event_ids:
                    continue
                self._event_ids.add(ev.get("id"))
                pt, scheme = self._decrypt(ev["pubkey"], ev["content"])
                if pt is None:
                    continue                       # cannot read this scheme — exactly like a real one
                try:
                    req = json.loads(pt)
                except Exception:
                    continue
                self.seen.append((req.get("method"), scheme))
                self._activity_seq += 1
                self._activity.set()
                if os.environ.get("PC_DEBUG"):
                    print(f"  DEBUG bunker <- {req.get('method')} {scheme} "
                          f"reqid={req.get('id')} evid={ev['id'][:8]}", flush=True)
                # The delay models the one human approval for this pairing. Once approved, a
                # fallback connect is the same session and real signers answer it immediately;
                # sleeping for every probe serializes the fake for 32+ seconds and loses replies.
                if req.get("method") == "connect" and self.delay and not self._approval_delayed:
                    self._approval_delayed = True
                    await asyncio.sleep(self.delay)
                if req.get("method") == "connect":
                    result = "ack"
                elif req.get("method") == "get_public_key":
                    result = self.user_pk
                elif req.get("method") in ("nip44_encrypt", "nip04_encrypt"):
                    # Reversible, so a decrypt round-trip is a REAL answer (check_nip46_bulk_lane
                    # times one after a relay restart). Length-only broke nothing until a check
                    # needed the plaintext back — and an instant "" from the fallthrough below made
                    # that check pass vacuously in 5ms.
                    result = "ct:" + req["params"][1]
                elif req.get("method") in ("nip44_decrypt", "nip04_decrypt"):
                    ct = req["params"][1]
                    result = ct[3:] if ct.startswith("ct:") else ""
                    if self.delay:
                        await asyncio.sleep(self.delay)
                elif req.get("method") == "sign_event":
                    tpl = json.loads(req["params"][0])
                    result = json.dumps(nevent.build_event(
                        self.user_sk, tpl.get("kind", 1), tpl.get("content", ""),
                        tpl.get("tags", []), tpl.get("created_at")))
                else:
                    result = ""
                body = json.dumps({"id": req.get("id"), "result": result})
                out = nevent.build_event(self.sk, 24133,
                                         self._encrypt(ev["pubkey"], body, scheme),
                                         [["p", ev["pubkey"]]])
                await ws.send(json.dumps(["EVENT", out]))

    def start(self):
        self._task = asyncio.ensure_future(self._guard())
        return self._task

    async def _guard(self):
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error
        finally:
            self.ready.set()

    async def wait_ready(self, timeout=5):
        await asyncio.wait_for(self.ready.wait(), timeout)
        if self.error:
            raise RuntimeError(f"fake signer could not subscribe: {self.error}")

    def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()

    async def wait_quiet(self, quiet_for=1.5, timeout=12.0):
        """Wait for a real signer-traffic quiet window, resetting it on every decrypted request.

        Login closes its gate before startApp's profile/session signatures finish. A fixed handful
        of sleeps can therefore label that legitimate tail as traffic caused by the next probe when
        the suite is loaded. This observes the far side instead: only a full quiet window counts,
        with an overall bound so a broken client cannot hang the gate forever.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            self._activity.clear()
            sequence = self._activity_seq
            try:
                await asyncio.wait_for(self._activity.wait(), min(quiet_for, remaining))
            except asyncio.TimeoutError:
                if sequence == self._activity_seq:
                    return True


# --------------------------------------------------------------------------------------------
# Drive the shipped sign-in UI.
# --------------------------------------------------------------------------------------------
LOGIN = r"""(async (uri) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  // Open the gate the way the guest card does — the handlers are bound at boot either way.
  document.body.classList.remove('guest');
  $('#auth-gate').classList.remove('hidden'); $('#app').classList.add('hidden');
  $('#auth-login').classList.remove('hidden');
  $('#btn-amber').click(); await sleep(60);
  $('#amber-input').value = uri;
  $('#btn-amber-connect').click();
  // WHO ended up signed in, not merely "the gate closed". A session resumed from an earlier case
  // would close it too, and that is a pass this check must never be able to hand out.
  const who = () => { try{ const m = window.__PC && window.__PC.me && window.__PC.me();
                           return (m && m.pubkey) || ''; }catch(_){ return ''; } };
  // Long enough for the 12s probe plus a slow signer, and it returns the moment it is done.
  for (let i = 0; i < 300; i++) {
    await sleep(200);
    if ($('#auth-gate').classList.contains('hidden') && who()) return { ok: true, err: '', pk: who() };
    const e = ($('#amber-error') || {}).textContent || '';
    if (e) return { ok: false, err: e, pk: who() };
  }
  return { ok: false, err: 'still waiting after 60s', pk: who() };
})"""


# A request too big for its NIP-44 envelope. Correct behavior is a local, actionable rejection and
# zero signer traffic; large data belongs in Blossom with only its pointer encrypted.
BIG = r"""(async () => {
  try{
    const me = window.__PC.me();
    const r = await window.__PC.nip44enc(me.pubkey, 'x'.repeat(100000));
    return { ok: true, got: String(r).slice(0, 40) };
  }catch(e){ return { ok: false, err: String((e && e.message) || e) }; }
})()"""


async def scenario(js, bunker, uri_relays):
    """Run one login attempt through the real UI. Returns (ok, detail)."""
    uri = "bunker://" + bunker.pk + "?" + "&".join(
        "relay=" + urllib.parse.quote(r, safe="") for r in uri_relays) + "&secret=s3cret"
    r = await js(f"({LOGIN})({json.dumps(uri)})", awaited=True)
    return r or {"ok": False, "err": "the page did not answer"}


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
    relay_a, relay_b = MiniRelay(), MiniRelay()
    url_a, url_b = await relay_a.start(), await relay_b.start()
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
                call_id = n[0]
                await ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == call_id:
                        return _cdp_result(msg, method)

            async def js(expr, awaited=False):
                try:
                    r = await call("Runtime.evaluate",
                                   {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                except CDPError as error:
                    # A diagnostic must report the browser failure, not replace it with
                    # AttributeError: NoneType has no attribute get.
                    print(f"  CDP: {error}", flush=True)
                    return None
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:800])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")

            # (name, bunker kwargs, relays named in the URI, which relay the signer sits on)
            cases = [
                ("wrong-relay",   dict(reads="both"),            [url_a, url_b], url_b),
                ("nip44-only",    dict(reads="nip44"),           [url_b],        url_b),
                ("nip04-only",    dict(reads="nip04"),           [url_b],        url_b),
                ("slow-approval", dict(reads="both", delay=16),  [url_b],        url_b),
            ]
            only = os.environ.get("PC_ONLY")
            if only:
                cases = [c for c in cases if c[0] == only]
            for name, kw, uri_relays, on in cases:
                event_start = (len(relay_a.events), len(relay_b.events))
                bunker = Bunker(on, **kw)
                bunker.start()
                try:
                    await bunker.wait_ready()
                except Exception as error:
                    problems.append((name, str(error), "the client scenario was not started"))
                    bunker.stop()
                    continue
                # A fresh page per case, SIGNED OUT: a login sticks, and an app that boots holding
                # the previous case's session would both hide the gate and keep that session's
                # sockets — which is how one connect went out twice. Clear, then load again.
                async def load():
                    # A same-URL navigation may reuse a document through Chromium's navigation
                    # cache. Leave it first so no previous case keeps NIP-46 timers or sockets alive.
                    await call("Page.navigate", {"url": "about:blank"})
                    # Login also sets an HTTP session cookie. localStorage.clear() cannot remove it,
                    # so without this the next case auto-enters the previous bunker account while
                    # this gate is forcing the sign-in UI open on top of it.
                    await call("Network.clearBrowserCookies")
                    await call("Page.navigate", {"url": url})
                    for _ in range(80):
                        await asyncio.sleep(0.25)
                        if await js("typeof document.querySelector('#btn-amber')?.onclick==='function' && "
                                    "typeof document.querySelector('#btn-amber-connect')?.onclick==='function'"):
                            return True
                    return False
                if not await load():
                    print("SKIP  the client never finished loading")
                    return 2
                await js("try{ localStorage.clear(); sessionStorage.clear(); }catch(_){}")
                if not await load():
                    print("SKIP  the client never finished loading")
                    return 2
                t0 = time.time()
                r = await scenario(js, bunker, uri_relays)
                took = time.time() - t0
                if not r.get("ok"):
                    case_events = relay_a.events[event_start[0]:] + relay_b.events[event_start[1]:]
                    sent = [e for e in case_events if any(
                        len(t) > 1 and t[0] == "p" and t[1] == bunker.pk
                        for t in e.get("tags", []))]
                    schemes = ["nip04" if "?iv=" in e.get("content", "") else "nip44"
                               for e in sent]
                    problems.append((name, r.get("err") or "login did not complete",
                                     f"signer saw {bunker.seen or 'nothing at all'}; client published "
                                     f"{schemes or 'nothing'}; signer task error "
                                     f"{bunker.error or 'none'} in {took:.0f}s"))
                elif r.get("pk") != bunker.user_pk:
                    problems.append((name, "signed in as the wrong key",
                                     f"got {str(r.get('pk'))[:12]}…, this bunker holds "
                                     f"{bunker.user_pk[:12]}…"))
                elif kw.get("reads") == "both":
                    # Closing the auth gate happens before the login's profile/session signatures
                    # have all crossed the fake signer. Let that legitimate tail go quiet before
                    # attributing any new signer traffic to the oversize request below.
                    if not await bunker.wait_quiet():
                        problems.append(("oversize-request", "login signer traffic never quiesced",
                                         f"signer saw {bunker.seen}"))
                        bunker.stop()
                        continue
                    seen_before = len(bunker.seen)
                    b = await js(BIG, awaited=True) or {}
                    err = b.get("err") or ""
                    if b.get("ok"):
                        problems.append(("oversize-request",
                                         "a 100KB NIP-44 plaintext was accepted",
                                         f"signer saw {bunker.seen[seen_before:] or 'nothing more'}"))
                    elif "65535" not in err or "attachment" not in err.lower():
                        problems.append(("oversize-request", "rejection lacked size/attachment guidance: "+err,
                                         f"signer saw {bunker.seen[seen_before:] or 'nothing more'}"))
                    elif len(bunker.seen) != seen_before:
                        problems.append(("oversize-request", "rejected locally but still contacted signer",
                                         f"signer saw {bunker.seen[seen_before:]}"))
                    elif os.environ.get("PC_DEBUG"):
                        print(f"  DEBUG oversize: {b} / {bunker.seen[-1:]}", flush=True)
                if os.environ.get("PC_DEBUG"):
                    print(f"  DEBUG {name}: ok in {took:.0f}s, signer saw {bunker.seen}", flush=True)
                bunker.stop()
    finally:
        await relay_a.stop()
        await relay_b.stop()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        # Chrome can finish writing profile bookkeeping just after its browser process exits.
        # shutil retries neither a racing directory nor an antivirus/indexer handle, so give that
        # harmless cleanup a short bounded retry instead of printing a scary rm error after PASS.
        for attempt in range(5):
            try:
                shutil.rmtree(PROFILE)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt == 4:
                    break
                await asyncio.sleep(0.1 * (attempt + 1))

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for name, err, detail in problems:
            print(f"  [{name}] {err} — {detail}")
        return 1
    print("OK  NIP-46 remote signer login works in every scenario")
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
