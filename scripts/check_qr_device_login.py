#!/usr/bin/env python3
"""Sign one device in from another by QR — driven through both halves of the real client.

    venv-unified/bin/python scripts/check_qr_device_login.py [base_url]

Two independent browsers, two profiles, one relay: the "desktop" opens Sign in → Amber / remote
signer → "Open in Amber / scan QR" and shows a nostrconnect:// link, and the "phone" — signed in with
its own key — pastes that link into Settings → Log in another device. Nothing is stubbed: both sides
are the shipped app, and the handshake goes over this node's own relay.

WHY THIS EXISTS AS ITS OWN CHECK. check_nip46_signer.py drives the OTHER entry point (paste a
bunker:// link) against a fake bunker, so it exercises only the client half of NIP-46. This flow is
the one where the app is BOTH ends, and the two ends are separate code with separate bugs — the
signer half (Nip46Signer) is not touched by that check at all.

The scenarios are clock skew, because that is the whole failure mode here and it is invisible:

  in-step        Both clocks agree. The baseline — if this fails, nothing below means anything.

  desktop-behind The desktop's clock is a minute behind the phone's. A signer stamps its events with
                 ITS clock and the relay applies `since` server-side, so a subscription whose window
                 is a few seconds wide drops the other device's requests before they are ever seen.
                 The desktop shows "waiting for the signer to approve…" until it times out, the phone
                 shows a cheerful "now logged in", and no error is raised anywhere by anyone. Two
                 devices are two clocks BY DEFINITION, and a desktop that was never NTP-synced drifts
                 minutes in a week — so this is the ordinary case, not the edge one.

  desktop-ahead  The same gap the other way, which travels a different code path (the ack is filtered
                 by the desktop's window, the requests by the phone's). Both windows have to be wide,
                 and only asking in both directions proves it.

  two-apps       TWO apps signed in from one phone, and both still there after it reloads. The signer
                 could not do this at all: it held ONE pairing and `start()` began with `stop()`, so
                 linking a second app silently ended the first — the earlier device's requests went to
                 a relay nobody was listening on any more, and it waited on a signer that had, from
                 its point of view, stopped existing. Verified to fail against that behaviour before
                 the fix, with the message "the phone is signing for 1 app(s), not 2". The reload is
                 the other half: sessions lived in memory, so refreshing the phone orphaned every app
                 it signed for, undetectable from the app until the next signature never came.

Exit 0 = clean, 1 = failures (printed), 2 = could not run (no Chrome / no instance / no websockets).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
# Two browsers, so two ports and two profiles. They must not share a profile: localStorage IS the
# session here, so one profile would sign the "desktop" in as the "phone" and the check would pass
# without a handshake ever happening.
PORT = int(os.environ.get("PC_CHECK_PORT") or 9493)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-qrlogin-check"

# The gap between the two clocks, in seconds. A minute is small as drift goes and far past the
# five-second window that used to be there.
SKEW = 60


# --------------------------------------------------------------------------------------------
# Half a CDP client: enough to load a page and run a promise in it.
# --------------------------------------------------------------------------------------------
class Browser:
    def __init__(self, port, profile, label):
        self.port, self.profile, self.label = port, profile, label
        self.proc = self.ws = None
        self._n = 0

    async def start(self, chrome):
        subprocess.run(["rm", "-rf", self.profile], check=False)
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import websockets
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            return False
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
        await self.call("Runtime.enable")
        await self.call("Page.enable")
        return True

    async def call(self, method, params=None):
        self._n += 1
        await self.ws.send(json.dumps({"id": self._n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self._n:
                return msg.get("result")

    async def js(self, expr, awaited=True):
        r = await self.call("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
        if r.get("exceptionDetails"):
            if os.environ.get("PC_DEBUG"):
                print(f"  DEBUG {self.label} EXC:", json.dumps(r["exceptionDetails"])[:600])
            return None
        return r["result"].get("value")

    async def skew(self, seconds):
        """Shift this browser's wall clock. Applied on every new document, so it is in place before
        a single line of the client runs — the client stamps `created_at` and `since` from
        Date.now(), which is exactly what a wrong clock moves."""
        await self.call("Page.addScriptToEvaluateOnNewDocument", {"source": f"""
            (() => {{ const off = {int(seconds)} * 1000, N = Date.now.bind(Date);
                      Date.now = () => N() + off; }})()"""})

    async def load(self, url, ready_sel):
        await self.call("Page.navigate", {"url": url})
        for _ in range(100):
            await asyncio.sleep(0.25)
            if await self.js(f"!!document.querySelector({json.dumps(ready_sel)})", awaited=False):
                return True
        return False

    async def stop(self):
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
        if self.proc:
            self.proc.terminate()
        subprocess.run(["rm", "-rf", self.profile], check=False)


# --------------------------------------------------------------------------------------------
# The two halves, driven through their own UI.
# --------------------------------------------------------------------------------------------
PHONE_LOGIN = r"""(async (nsec) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  document.body.classList.remove('guest');
  $('#auth-gate').classList.remove('hidden'); $('#app').classList.add('hidden');
  $('#auth-login').classList.remove('hidden');
  $('#nsec-input').value = nsec;
  $('#btn-nsec-login').click();
  for (let i=0;i<120;i++){ await sleep(200);
    const m = window.__PC && window.__PC.me && window.__PC.me();
    if (m && m.pubkey) return { ok:true, pk:m.pubkey };
  }
  return { ok:false, err:'the phone never signed in with its own key' };
})"""

# Open Sign in → Amber / remote signer → "Open in Amber / scan QR" and read the link off the screen.
DESKTOP_SHOW_QR = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  document.body.classList.remove('guest');
  $('#auth-gate').classList.remove('hidden'); $('#app').classList.add('hidden');
  $('#auth-login').classList.remove('hidden');
  $('#btn-amber').click(); await sleep(60);
  $('#btn-amber-nc').click();
  for (let i=0;i<100;i++){ await sleep(250);
    const u = ($('#amber-nc-uri')||{}).textContent||'';
    if (u) return { uri:u, err:'' };
    const e = ($('#amber-error')||{}).textContent||'';
    if (e) return { uri:'', err:e };
  }
  return { uri:'', err:'the desktop never produced a nostrconnect link' };
})()"""

# Settings → Log in another device → Scan QR code → "paste link instead". The camera path needs a
# camera; the paste path is the same handler and is what a headless browser can reach.
PHONE_SCAN = r"""(async (uri) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  window.__PC.switchView('settings');
  for (let i=0;i<60 && !$('#set-scan-qr'); i++) await sleep(200);
  if (!$('#set-scan-qr')) return { ok:false, err:'Settings has no "Scan QR code" button' };
  $('#set-scan-qr').click();
  for (let i=0;i<60 && !$('#qr-paste'); i++) await sleep(200);
  if (!$('#qr-paste')) return { ok:false, err:'the scanner offered no "paste link instead"' };
  $('#qr-paste').click();
  for (let i=0;i<60 && !$('#qr-paste-uri'); i++) await sleep(200);
  if (!$('#qr-paste-uri')) return { ok:false, err:'no paste box' };
  $('#qr-paste-uri').value = uri;
  $('#qr-paste-go').click();
  /* ANSWER THE "replace or keep both" QUESTION. Every PosterChan client announces itself as
   * "PosterChan", so a second pairing from the same phone always collides by name and is asked.
   * `data-uc="0"` is the CANCEL button, which here means "Keep both" — this check exists to prove
   * two apps can be paired at once, so it must say so out loud rather than leave the dialog
   * unanswered and then report the feature as broken. */
  for (let i=0;i<40;i++){ await sleep(150);
    const c = document.querySelector('.uiconfirm [data-uc="0"]');
    if (c) { c.click(); break; }
  }
  return { ok:true };
})"""

# A SECOND app paired from the same phone. `Nip46Signer` held one session and `start()` began with
# `stop()`, so this used to kill the first pairing outright — the earlier device's requests went to a
# relay nobody was listening on any more, and it waited on a signer that had stopped existing.
PHONE_SCAN_AGAIN = """(async (uri) => {
  const sl=ms=>new Promise(r=>setTimeout(r,ms)); const $=s=>document.querySelector(s);
  window.__PC.switchView('settings');
  for(let i=0;i<60 && !$('#set-scan-qr'); i++) await sl(200);
  $('#set-scan-qr').click();
  for(let i=0;i<60 && !$('#qr-paste'); i++) await sl(200);
  $('#qr-paste').click();
  for(let i=0;i<60 && !$('#qr-paste-uri'); i++) await sl(200);
  $('#qr-paste-uri').value = uri;
  $('#qr-paste-go').click();
  /* ANSWER THE "replace or keep both" QUESTION. Every PosterChan client announces itself as
   * "PosterChan", so a second pairing from the same phone always collides by name and is asked.
   * `data-uc="0"` is the CANCEL button, which here means "Keep both" — this check exists to prove
   * two apps can be paired at once, so it must say so out loud rather than leave the dialog
   * unanswered and then report the feature as broken. */
  for (let i=0;i<40;i++){ await sl(150);
    const c = document.querySelector('.uiconfirm [data-uc="0"]');
    if (c) { c.click(); break; }
  }
  return {ok:true};})"""

# What the phone thinks it is signing for, and whether it survives a reload.
PHONE_SESSIONS = """(async () => {
  const sl=ms=>new Promise(r=>setTimeout(r,ms));
  for(let i=0;i<40;i++){ await sl(250);
    try{ const n = window.__PC.signerApps().length; if(n) return {n:n, apps:window.__PC.signerApps()}; }catch(_){}
  }
  try{ return {n:0, apps:window.__PC.signerApps()}; }catch(e){ return {err:String(e)}; }})()"""


# PAIRING IS NOT THE FEATURE — SIGNING IS. Every scenario above stops at "the desktop is logged in",
# which only exercises the handshake and `get_public_key`. The thing people actually do next is write
# a post, and that is a `sign_event` travelling the same path in the other direction: reported as
# "signer request timed out when trying to make a new post", with pairing working perfectly.
DESKTOP_SIGN = """(async () => {
  const sl=ms=>new Promise(r=>setTimeout(r,ms));
  try{
    const ev = await window.__PC.sign(1, 'check_qr_device_login test note', []);
    if(!ev || !ev.sig || !ev.id) return {ok:false, err:'the signer returned no signature'};
    return {ok:true, id:String(ev.id).slice(0,12)};
  }catch(e){ return {ok:false, err:String((e && e.message) || e)}; }})()"""


# THE SIGNER'S SOCKET, KILLED. A relay idle-drops a connection nobody is using, and a signer is used
# only when something is signed — so the FIRST post after a quiet spell is the one that finds the
# socket gone. Reported as "signer request timed out when trying to make a new post" while pairing
# itself worked perfectly, which is exactly the shape of a reconnect that never happens.
PHONE_DROP_SOCKETS = """(() => {
  try{ return {closed: window.__PC.signerDropSockets()}; }
  catch(e){ return {err:String((e&&e.message)||e)}; }})()"""


# Did the desktop actually end up signed in, and AS WHOM. "the gate closed" is not the question: a
# resumed session closes it too, and that is a pass this check must never be able to hand out.
DESKTOP_WAIT = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  const who = () => { try{ const m=window.__PC&&window.__PC.me&&window.__PC.me();
                           return (m&&m.pubkey)||''; }catch(_){ return ''; } };
  for (let i=0;i<250;i++){ await sleep(200);
    if ($('#auth-gate').classList.contains('hidden') && who()) return { ok:true, pk:who() };
    const e = ($('#amber-error')||{}).textContent||'';
    if (e) return { ok:false, err:e };
  }
  return { ok:false, err:'gave up: '+(($('#amber-nc-status')||{}).textContent||'no status') };
})()"""


def fresh_nsec():
    from app.services.nostr import bech32, bip340
    sk = os.urandom(32)
    return bech32.encode("nsec", sk), bip340.pubkey_from_seckey(sk).hex()


async def run_case(name, desktop, phone, url, skew, problems):
    """One pairing attempt, from a clean page on both sides."""
    await desktop.skew(skew)
    if not await phone.load(url + "/client", "#btn-nsec-login"):
        return "SKIP the phone never finished loading"
    if not await desktop.load(url + "/client", "#btn-amber"):
        return "SKIP the desktop never finished loading"
    await phone.js("try{localStorage.clear();sessionStorage.clear();}catch(_){}", awaited=False)
    await desktop.js("try{localStorage.clear();sessionStorage.clear();}catch(_){}", awaited=False)
    if not await phone.load(url + "/client", "#btn-nsec-login"):
        return "SKIP the phone never finished loading"
    if not await desktop.load(url + "/client", "#btn-amber"):
        return "SKIP the desktop never finished loading"

    nsec, pk = fresh_nsec()
    r = await phone.js(f"({PHONE_LOGIN})({json.dumps(nsec)})") or {}
    if not r.get("ok"):
        return "SKIP " + str(r.get("err") or "the phone would not sign in")

    q = await desktop.js(DESKTOP_SHOW_QR) or {}
    uri = q.get("uri") or ""
    if not uri:
        problems.append((name, q.get("err") or "no nostrconnect link", ""))
        return None

    t0 = time.time()
    s = await phone.js(f"({PHONE_SCAN})({json.dumps(uri)})") or {}
    if not s.get("ok"):
        problems.append((name, s.get("err") or "the phone could not take the link", ""))
        return None

    w = await desktop.js(DESKTOP_WAIT) or {}
    took = time.time() - t0
    if not w.get("ok"):
        problems.append((name, w.get("err") or "the desktop never signed in",
                         f"the phone holds {pk[:12]}…, {took:.0f}s"))
    elif w.get("pk") != pk:
        problems.append((name, "the desktop signed in as the wrong key",
                         f"got {str(w.get('pk'))[:12]}…, the phone holds {pk[:12]}…"))
    else:
        # …and it can actually SIGN, which is what a pairing is FOR.
        sg = await desktop.js(DESKTOP_SIGN) or {}
        if not sg.get("ok"):
            problems.append((name + "/sign", sg.get("err") or "no signature came back",
                             "paired fine, then could not sign a note"))
        elif os.environ.get("PC_DEBUG"):
            print(f"  DEBUG {name}: paired in {took:.0f}s as {pk[:12]}…, signed {sg.get('id')}",
                  flush=True)
    return None


async def run_idle_socket(desktop, phone, url, problems):
    """Pair, drop the signer's sockets as an idle relay would, then post.

    A signer is used only when something is signed, so its connection is idle by definition — and
    the first post after a quiet spell is the one that discovers the relay dropped it. Everything
    before this scenario signs seconds after pairing, on a socket that was never allowed to go cold,
    which is the one condition under which a missing reconnect cannot show itself.
    """
    if not await phone.load(url + "/client", "#btn-nsec-login"):
        return "SKIP the phone never finished loading"
    await phone.js("try{localStorage.clear();sessionStorage.clear();}catch(_){}", awaited=False)
    if not await phone.load(url + "/client", "#btn-nsec-login"):
        return "SKIP the phone never finished loading"
    nsec, pk = fresh_nsec()
    r = await phone.js(f"({PHONE_LOGIN})({json.dumps(nsec)})") or {}
    if not r.get("ok"):
        return "SKIP " + str(r.get("err") or "the phone would not sign in")

    await desktop.skew(0)
    if not await desktop.load(url + "/client", "#btn-amber"):
        return "SKIP the desktop never finished loading"
    await desktop.js("try{localStorage.clear();sessionStorage.clear();}catch(_){}", awaited=False)
    if not await desktop.load(url + "/client", "#btn-amber"):
        return "SKIP the desktop never finished loading"
    # Retried once on a relay timeout, for the reason spelled out in run_two_apps: this browser has
    # already opened and torn down pairings, and the next dial is occasionally slow. Anything that is
    # not a timeout still fails immediately.
    q = {}
    for _attempt in (1, 2):
        q = await desktop.js(DESKTOP_SHOW_QR) or {}
        if q.get("uri") or "timed out" not in str(q.get("err") or ""):
            break
        await asyncio.sleep(3)
    if not q.get("uri"):
        problems.append(("idle-socket", q.get("err") or "no nostrconnect link", ""))
        return None
    sc = await phone.js(f"({PHONE_SCAN})({json.dumps(q['uri'])})") or {}
    if not sc.get("ok"):
        problems.append(("idle-socket", sc.get("err") or "the phone could not take the link", ""))
        return None
    w = await desktop.js(DESKTOP_WAIT) or {}
    if not w.get("ok"):
        problems.append(("idle-socket", w.get("err") or "the desktop never signed in", ""))
        return None

    dropped = await phone.js(PHONE_DROP_SOCKETS) or {}
    if dropped.get("err") or not dropped.get("closed"):
        return f"SKIP could not drop the signer's sockets ({dropped})"
    # NO GRACE PERIOD, deliberately. Waiting for the reconnect tests the reconnect; the case that
    # actually bites is the request arriving DURING it, because that is what pressing Post
    # immediately after a quiet spell looks like. The reply has nowhere to go for a couple of
    # seconds, and a reply that is dropped rather than held is a request that times out 120s later.
    sg = await desktop.js(DESKTOP_SIGN) or {}
    if not sg.get("ok"):
        problems.append(("idle-socket", sg.get("err") or "no signature came back",
                         f"closed {dropped.get('closed')} socket(s); the signer never reconnected"))
    elif os.environ.get("PC_DEBUG"):
        print(f"  DEBUG idle-socket: dropped {dropped.get('closed')}, signed {sg.get('id')}",
              flush=True)
    return None


async def run_two_apps(desktop, phone, url, problems):
    """TWO apps signed in from one phone, and both still there after it reloads.

    This is the case the signer could not do at all: it held ONE pairing and `start()` began with
    `stop()`, so linking a second app silently ended the first — the earlier device's requests went
    to a relay nobody was listening on any more, and it waited on a signer that had, from its point
    of view, stopped existing. Nothing errored on either side.

    The reload is the other half. Sessions lived in memory, so refreshing the phone orphaned every
    app it was signing for, with no way to tell from the app except the next signature never coming.
    """
    if not await phone.load(url + "/client", "#btn-nsec-login"):
        return "SKIP the phone never finished loading"
    await phone.js("try{localStorage.clear();sessionStorage.clear();}catch(_){}", awaited=False)
    if not await phone.load(url + "/client", "#btn-nsec-login"):
        return "SKIP the phone never finished loading"
    nsec, pk = fresh_nsec()
    r = await phone.js(f"({PHONE_LOGIN})({json.dumps(nsec)})") or {}
    if not r.get("ok"):
        return "SKIP " + str(r.get("err") or "the phone would not sign in")

    # Two desktops in sequence, through the SAME browser: each gets its own nostrconnect link, which
    # is its own app key, which is its own session.
    await desktop.skew(0)
    uris = []
    for i in (1, 2):
        if not await desktop.load(url + "/client", "#btn-amber"):
            return "SKIP the desktop never finished loading"
        await desktop.js("try{localStorage.clear();sessionStorage.clear();}catch(_){}", awaited=False)
        if not await desktop.load(url + "/client", "#btn-amber"):
            return "SKIP the desktop never finished loading"
        # RETRIED ONCE, and only for a relay that did not open in time. This case runs after three
        # pairings have already been made and torn down through the same browser, and
        # `beginNostrConnect` resets its pool and redials on every attempt, so the fourth dial in a
        # row occasionally times out. That is the CHECK's own impatience, not the signer's: run on
        # its own the same code passes every time. A flaky check is worse than no check, because it
        # teaches people to re-run until it is green. Anything that is NOT a timeout still fails
        # immediately, because those are real.
        q = {}
        for attempt in (1, 2):
            q = await desktop.js(DESKTOP_SHOW_QR) or {}
            if q.get("uri") or "timed out" not in str(q.get("err") or ""):
                break
            await asyncio.sleep(3)
        if not q.get("uri"):
            problems.append(("two-apps", q.get("err") or "no nostrconnect link", f"app {i}"))
            return None
        uris.append(q["uri"])
        s = await phone.js(f"({PHONE_SCAN_AGAIN})({json.dumps(q['uri'])})") or {}
        if not s.get("ok"):
            problems.append(("two-apps", "the phone could not take the link", f"app {i}"))
            return None
        await asyncio.sleep(3)

    got = await phone.js(PHONE_SESSIONS) or {}
    if got.get("n") != 2:
        problems.append(("two-apps", f"the phone is signing for {got.get('n')} app(s), not 2",
                         "pairing a second app ended the first"))
        return None

    # …and they survive a reload, which is the whole reason they are persisted.
    if not await phone.load(url + "/client", "#feed, #btn-nsec-login"):
        return "SKIP the phone never came back"
    await asyncio.sleep(3)
    back = await phone.js(PHONE_SESSIONS) or {}
    if back.get("n") != 2:
        problems.append(("two-apps-reload",
                         f"{back.get('n')} app(s) survived the reload, not 2",
                         "sessions are not being restored"))
    return None


async def drive(url):
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    desktop = Browser(PORT, PROFILE, "desktop")
    phone = Browser(PORT + 1, PROFILE + "-b", "phone")
    problems = []
    try:
        if not await desktop.start(chrome) or not await phone.start(chrome):
            print("SKIP  could not start Chrome")
            return 2
        cases = [("in-step", 0), ("desktop-behind", -SKEW), ("desktop-ahead", SKEW)]
        only = os.environ.get("PC_ONLY")
        if only:
            cases = [c for c in cases if c[0] == only]
        for name, skew in cases:
            skipped = await run_case(name, desktop, phone, url, skew, problems)
            if skipped:
                print(skipped)
                return 2
        if not only or only == "idle-socket":
            skipped = await run_idle_socket(desktop, phone, url, problems)
            if skipped:
                print(skipped)
                return 2
        if not only or only == "two-apps":
            skipped = await run_two_apps(desktop, phone, url, problems)
            if skipped:
                print(skipped)
                return 2
    finally:
        await desktop.stop()
        await phone.stop()

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for name, err, detail in problems:
            print(f"  [{name}] {err}" + (f" — {detail}" if detail else ""))
        return 1
    print("OK  QR device login: both clocks, two apps at once, and both survive a reload")
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
    return asyncio.run(drive(BASE))


if __name__ == "__main__":
    sys.exit(main())
