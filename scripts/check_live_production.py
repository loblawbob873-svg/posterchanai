#!/usr/bin/env python3
"""Is the deploy that just landed actually WORKING on production? Six questions asked of the real instance.

Every other check runs this checkout against stubs or a loopback server, so all of them can be green while
production is broken — which is what the owner kept reporting: "it broke in production while all tests
passed". This one asks the deployed instance itself, over the public internet, right after `sync.sh` has
verified every node is on the commit:

  server_stats   GET /client/server-stats -> 200 and the RELAY figures are real (not unavailable / null)
  relay_publish  a throwaway key publishes an EPHEMERAL event (kind 2xxxx: forwarded, never stored) and the
                 relay answers OK=true; if policy refuses a stranger's write (WoT gate), the relay must
                 instead answer a REQ with EOSE -- the result says which path was proven
  concord_ticket /ws/concord-relay with a FORGED ticket must be refused at the handshake (403), never accepted
  client_shell   GET / is the client HTML and /client/sw.js carries `pc-nostr-v<N>` (N is printed)
  federation     GET /client/federation -> 200 JSON in state ok/off; 503 = could not ask (never a pass)
  concord_calls  when /client/config says `concord_calls`, the CORD-07 broker probe answers 204 and an UNSIGNED
                 grant is refused (401) -- the SFU is up and the broker checks signatures; a node that does not
                 offer calls must answer 404 (2026-10-22: a green deploy shipped calls whose broker said 404)

    venv-unified/bin/python scripts/check_live_production.py https://poster.place

Exit 0 = all six passed, 1 = at least one FAILED (printed), 2 = could not run / could not ask (never a pass).
No Chrome. Load is a handful of requests. No secrets; the throwaway key lives only in memory; nothing a user
wrote is printed — only status codes, counts and the check's own event.
"""
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
TIMEOUT = 20.0
EPHEMERAL_KIND = 29_731          # 20000-29999: relays forward these and store nothing
# After a restart the instance can take a little while to answer; a deploy check that fires into that gap
# would report a working deploy as unreachable.
READY_WAIT = float(os.environ.get("PC_LIVE_READY_WAIT", "120"))


def _http(url, timeout=TIMEOUT):
    import httpx
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "posterchan-live-check/1"}) as c:
        return c.get(url)


def wait_ready(base):
    """True once /client/config answers 200 (polled for READY_WAIT seconds), else the last error."""
    deadline = time.time() + READY_WAIT
    last = "no answer"
    while True:
        try:
            r = _http(base + "/client/config", timeout=10)
            if r.status_code == 200:
                return True, r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = type(e).__name__
        if time.time() >= deadline:
            return False, last
        time.sleep(5)


# ---------------------------------------------------------------------------------------------- checks
def check_server_stats(base):
    try:
        r = _http(base + "/client/server-stats", timeout=40)
    except Exception as e:
        return SKIP, f"could not ask: {type(e).__name__}"
    if r.status_code == 503:
        return SKIP, "503 — the server could not compute stats (could not ask)"
    if r.status_code != 200:
        return FAIL, f"HTTP {r.status_code}"
    try:
        d = r.json()
    except Exception:
        return FAIL, "200 but not JSON"
    relay = d.get("relay") or {}
    totals = d.get("totals") or {}
    bad = []
    if d.get("relay_unavailable") is not False:
        bad.append(f"relay_unavailable={d.get('relay_unavailable')!r}")
    if relay.get("running") is not True:
        bad.append(f"relay.running={relay.get('running')!r}")
    for k in ("events", "notes"):
        if not isinstance(totals.get(k), int):
            bad.append(f"totals.{k}={totals.get(k)!r}")
    if bad:
        return FAIL, "relay figures missing: " + ", ".join(bad)
    return PASS, (f"relay running, {totals['events']} events, {relay.get('online')} online, "
                  f"backend={d.get('relay_backend')}")


def _sign(sk_bytes, ev):
    import coincurve
    priv = coincurve.PrivateKey(sk_bytes)
    ev["pubkey"] = priv.public_key.format(compressed=True)[1:].hex()
    ser = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                     separators=(",", ":"), ensure_ascii=False)
    ev["id"] = hashlib.sha256(ser.encode()).hexdigest()
    ev["sig"] = priv.sign_schnorr(bytes.fromhex(ev["id"]), secrets.token_bytes(32)).hex()
    return ev


async def _relay_publish(relay_url):
    import websockets
    ev = _sign(secrets.token_bytes(32), {"created_at": int(time.time()), "kind": EPHEMERAL_KIND,
                                         "tags": [], "content": "posterchan post-deploy live check"})
    ok = None
    async with websockets.connect(relay_url, open_timeout=TIMEOUT, close_timeout=5, max_size=2**22) as ws:
        await ws.send(json.dumps(["EVENT", ev]))
        end = time.time() + TIMEOUT
        while time.time() < end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time()))
            except asyncio.TimeoutError:
                break
            # An AUTH challenge may arrive first; a stranger's key cannot usefully answer it, so wait for the OK.
            if isinstance(msg, list) and len(msg) >= 3 and msg[0] == "OK" and msg[1] == ev["id"]:
                ok = (bool(msg[2]), str(msg[3]) if len(msg) > 3 else "")
                break
        if ok and ok[0]:
            return PASS, f"ephemeral kind {EPHEMERAL_KIND} from a throwaway key: OK=true (publish path proven)"
        # Refused by policy, or no OK at all: the relay must at least READ.
        sub = "live-check-" + secrets.token_hex(4)
        await ws.send(json.dumps(["REQ", sub, {"kinds": [0], "limit": 1}]))
        end = time.time() + TIMEOUT
        eose = False
        while time.time() < end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time()))
            except asyncio.TimeoutError:
                break
            if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "EOSE" and msg[1] == sub:
                eose = True
                break
        try:
            await ws.send(json.dumps(["CLOSE", sub]))
        except Exception:
            pass
    why = (f"write refused by policy ({ok[1][:80] or 'no reason'})" if ok
           else "no OK for the write within %ds" % TIMEOUT)
    if eose:
        if ok:   # an explicit refusal is the WoT gate working; reading proves the relay is up
            return PASS, f"{why}; REQ answered with EOSE (read path proven, write path NOT)"
        return FAIL, f"{why}, although REQ was answered with EOSE — the publish path is silent"
    return FAIL, f"{why}; REQ got no EOSE within {int(TIMEOUT)}s"


def check_relay_publish(relay_url):
    try:
        return asyncio.run(_relay_publish(relay_url))
    except Exception as e:
        return SKIP, f"could not connect to {relay_url}: {type(e).__name__}"


async def _concord_forged(base):
    import websockets
    ws_base = "wss://" + base.split("://", 1)[1] if base.startswith("https://") else \
              "ws://" + base.split("://", 1)[1]
    url = ws_base + "/ws/concord-relay?u=wss://relay.damus.io&t=forged"
    try:
        async with websockets.connect(url, open_timeout=TIMEOUT, close_timeout=5) as ws:
            # Accepted. A server may still close straight away, but a forged ticket must never get this far.
            try:
                await asyncio.wait_for(ws.recv(), timeout=3)
            except Exception:
                pass
            return FAIL, "a FORGED ticket was ACCEPTED at the handshake — the tunnel is open to anyone"
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
        if status in (401, 403):
            return PASS, f"forged ticket refused at the handshake (HTTP {status})"
        if status is not None:
            if status == 404:
                return FAIL, "HTTP 404 — /ws/concord-relay is not served"
            if status in (502, 503, 504, 520, 521, 522, 523, 524):
                return SKIP, f"could not ask: HTTP {status} from the edge"
            return FAIL, f"refused with an unexpected HTTP {status} (expected 403)"
        return SKIP, f"could not ask: {type(e).__name__}"


def check_concord_ticket(base):
    try:
        return asyncio.run(_concord_forged(base))
    except Exception as e:
        return SKIP, f"could not ask: {type(e).__name__}"


def check_client_shell(base):
    import re
    try:
        r = _http(base + "/")
        sw = _http(base + "/client/sw.js")
    except Exception as e:
        return SKIP, f"could not ask: {type(e).__name__}"
    if r.status_code != 200:
        return FAIL, f"/ answered HTTP {r.status_code}"
    if "text/html" not in r.headers.get("content-type", "") or "<script" not in r.text:
        return FAIL, "/ is not the client HTML (no text/html or no <script>)"
    if sw.status_code != 200:
        return FAIL, f"/client/sw.js answered HTTP {sw.status_code}"
    m = re.search(r"""const\s+CACHE\s*=\s*['"]pc-nostr-v(\d+)['"]""", sw.text)
    if not m:
        return FAIL, "/client/sw.js carries no `pc-nostr-v<N>` CACHE version"
    return PASS, f"client HTML 200 ({len(r.content)} bytes); service worker cache pc-nostr-v{m.group(1)}"


def check_federation(base):
    try:
        r = _http(base + "/client/federation")
    except Exception as e:
        return SKIP, f"could not ask: {type(e).__name__}"
    if r.status_code == 503:
        return SKIP, "503 — the delivery stats document could not be read (could not ask, not a pass)"
    if r.status_code != 200:
        return FAIL, f"HTTP {r.status_code}"
    try:
        d = r.json()
    except Exception:
        return FAIL, "200 but not JSON"
    state = d.get("state")
    tail = f"state={state} lag={d.get('lag')}s sends/min={d.get('per_min')} queued={d.get('queued')}"
    if state in ("ok", "off"):
        return PASS, tail
    if state in ("behind", "failing", "silent"):
        return FAIL, "federation delivery is stalled: " + tail
    return SKIP, "could not ask: " + tail


def check_concord_calls(base, cfg):
    offered = bool((cfg or {}).get("concord_calls"))
    try:
        probe = _http(base + "/.well-known/concord/av")
    except Exception as e:
        return SKIP, f"could not ask: {type(e).__name__}"
    if not offered:
        if probe.status_code == 204:
            return FAIL, "the broker answers 204 but /client/config does not offer calls"
        return PASS, f"calls not offered here (probe {probe.status_code})"
    if probe.status_code != 204:
        return FAIL, f"calls are enabled but the broker probe answered HTTP {probe.status_code} (is livekit-server running?)"
    try:
        unsigned = _http(base + "/.well-known/concord/av/" + "0" * 64)
    except Exception as e:
        return SKIP, f"could not ask the grant: {type(e).__name__}"
    if unsigned.status_code != 401:
        return FAIL, f"an UNSIGNED call grant answered HTTP {unsigned.status_code}, not 401"
    return PASS, "broker probe 204; an unsigned grant is refused (401)"


# ------------------------------------------------------------------------------------------------ main
def main(argv):
    base = (argv[1] if len(argv) > 1 else "").rstrip("/")
    if not base:
        print("SKIP: needs an instance URL (./test.sh --live URL)")
        return 2
    import importlib.util
    for dep in ("httpx", "websockets", "coincurve"):
        if importlib.util.find_spec(dep) is None:
            print(f"SKIP: python dependency missing: {dep}")
            return 2

    ready, cfg = wait_ready(base)
    if not ready:
        print(f"SKIP: {base} did not answer /client/config within {int(READY_WAIT)}s ({cfg})")
        return 2
    relay_url = (cfg or {}).get("relay_url") or "wss://relay.poster.place"

    results = [
        ("server_stats", check_server_stats(base)),
        ("relay_publish", check_relay_publish(relay_url)),
        ("concord_ticket", check_concord_ticket(base)),
        ("client_shell", check_client_shell(base)),
        ("federation", check_federation(base)),
        ("concord_calls", check_concord_calls(base, cfg)),
    ]
    for name, (verdict, why) in results:
        print(f"{verdict:4}  {name:15} {why}")
    fails = [n for n, (v, _) in results if v == FAIL]
    skips = [n for n, (v, _) in results if v == SKIP]
    if fails:
        print(f"FAIL: {len(fails)} production check(s) failed on {base}: {', '.join(fails)}")
        return 1
    if skips:
        print(f"SKIP: could not ask {', '.join(skips)} on {base} — NOT verified")
        return 2
    print(f"OK: all {len(results)} production checks passed on {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
