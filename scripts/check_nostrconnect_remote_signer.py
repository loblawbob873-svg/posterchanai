#!/usr/bin/env python3
"""Pair with a THIRD-PARTY remote-signer link of primal.net's exact shape, through the shipped client.

    venv-unified/bin/python scripts/check_nostrconnect_remote_signer.py [base_url]

WHY THIS EXISTS. `check_qr_device_login.py` pairs PosterChan with PosterChan: both ends are this app,
so the link is ours, the relay is ours, and the clocks are the only variable. Every one of those is
different when somebody logs into primal.net, and "primal still not working" survived four builds
while that check stayed green — because the parts it holds constant are exactly the parts that were
in question.

So this drives the SAME paste handler against a link that is not ours, on a relay that is not ours:

  * no `perms` at all. Ours always carries one, so `_grants` returning null — "declared nothing" —
    had never been exercised by a check, only by reading the code.
  * a `secret` in primal's `sec-<uuid>` form rather than our hex.
  * extra parameters we do not emit (`nwc=1`, `image=`), which must be ignored rather than tripping
    the parse.
  * a relay that is NOT this instance's, which is the branch `start()` guards with the "…wants to be
    signed in through <host>. Allow it?" question — a branch `check_qr_device_login` deliberately
    never reaches, because pairing elsewhere would test two things at once. That question is
    load-bearing: unanswered, the pairing simply never happens, and from the sofa that is
    indistinguishable from a QR that would not scan.

WHAT IS PROVEN. A real "PrimalWeb" is impersonated by a throwaway keypair that subscribes to
primal's own relay for kind-24133 addressed to it. The assertion is the one that matters to the
person logging in: the ACK arrives, decrypts with THEIR key, and carries back the secret they
minted. If that lands, the signer half did its job and anything still broken is on the other side of
the glass. Nothing here is stubbed — the relay is `wss://nrs.primal.net` unless PC_NC_RELAY says
otherwise.

Exit 0 pass, 1 fail, 2 could not run (no chrome, no instance, relay unreachable) — reported as a
SKIP with its reason rather than as a pass.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
RELAY = os.environ.get("PC_NC_RELAY", "wss://nrs.primal.net")

_spec = importlib.util.spec_from_file_location(
    "_qrlogin", os.path.join(ROOT, "scripts", "check_qr_device_login.py"))
_qrlogin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_qrlogin)          # guarded by __main__, so importing runs nothing

Browser = _qrlogin.Browser
PHONE_LOGIN = _qrlogin.PHONE_LOGIN
fresh_nsec = _qrlogin.fresh_nsec

from app.services.nostr import bip340, nip04, nip44   # noqa: E402


# The link, built to primal.net's shape rather than ours. Kept as a literal template so the
# difference from `signer_uri()` in check_qr_scan.py is readable: no perms, a sec-<uuid> secret,
# and two parameters we never emit.
def primal_shaped_uri(client_pk: str, relay: str, secret: str) -> str:
    import urllib.parse as up
    q = up.quote
    return (f"nostrconnect://{client_pk}"
            f"?relay={q(relay, safe='')}"
            f"&secret={secret}"
            f"&name=PrimalWeb"
            f"&url={q('https://primal.net', safe='')}"
            f"&image={q('https://primal.net/assets/logo_remote-afdf5895.png', safe='')}"
            f"&nwc=1")


# Paste the link, then answer BOTH questions this flow can ask — the foreign-relay one with ALLOW
# (the whole point) and the name clash with "keep both". uiConfirm renders its buttons with
# data-uc="1" for ok and data-uc="0" for cancel.
PHONE_PASTE = r"""(async (uri) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  const seen = [];
  window.__PC.switchView('signer');
  for (let i=0;i<60 && !$('#signer-scan-qr'); i++) await sleep(200);
  if (!$('#signer-scan-qr')) return { ok:false, err:'Signer has no "Scan sign-in QR" button' };
  $('#signer-scan-qr').click();
  for (let i=0;i<60 && !$('#qr-paste'); i++) await sleep(200);
  if (!$('#qr-paste')) return { ok:false, err:'the scanner offered no "paste link instead"' };
  $('#qr-paste').click();
  for (let i=0;i<60 && !$('#qr-paste-uri'); i++) await sleep(200);
  if (!$('#qr-paste-uri')) return { ok:false, err:'no paste box' };
  $('#qr-paste-uri').value = uri;
  $('#qr-paste-go').click();
  /* Answer whatever it asks. The relay question is the one being tested, so it is ALLOWED; a name
   * clash is answered "keep both" so a re-run does not quietly revoke the previous pairing. */
  /* ALWAYS the ok button. The first version picked ok-vs-cancel by matching the relay question's
   * wording, and when that match failed it clicked CANCEL — which DECLINES the pairing, so the
   * check reported "no ACK ever reached the relay" about a decline it had performed itself. The
   * text is still recorded, and asserted on separately; it must not steer the click. */
  for (let i=0;i<80;i++){
    await sleep(150);
    const dlg = document.querySelector('[data-uc="1"]');
    if (!dlg) continue;
    // uiConfirm renders .uiconfirm-bg > .uiconfirm > .uiconfirm-msg — NOT a .modal, which is what
    // the first version looked for, so every dialog was recorded as empty text.
    const msg = document.querySelector('.uiconfirm-msg');
    seen.push(((msg && msg.textContent) || '').slice(0,400));
    dlg.click();
    await sleep(300);
  }
  return { ok:true, asked:seen };
})"""


def _say(m):
    print(m, flush=True)


async def main() -> int:
    try:
        import websockets
    except Exception as exc:
        _say(f"SKIP: websockets not importable ({exc})")
        return 2

    import shutil
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium") or shutil.which("chromium-browser"))
    if not chrome:
        _say("SKIP: no chrome on this node")
        return 2

    # The "PrimalWeb" side: a throwaway key that will wait on primal's relay for our answer.
    skb = os.urandom(32)
    client_pk = bip340.pubkey_from_seckey(skb)
    client_pk = client_pk.hex() if isinstance(client_pk, (bytes, bytearray)) else client_pk
    secret = "sec-" + str(uuid.uuid4())
    uri = primal_shaped_uri(client_pk, RELAY, secret)
    _say(f"[nc] impersonating PrimalWeb as {client_pk[:16]}… on {RELAY}")
    _say(f"[nc] secret {secret}")

    problems: list[str] = []
    got: dict = {}

    async def listen(ready: asyncio.Event, done: asyncio.Event):
        """Subscribe as PrimalWeb would, and grab the first 24133 addressed to us."""
        try:
            async with websockets.connect(RELAY, open_timeout=20) as ws:
                sub = ["REQ", "nc", {"kinds": [24133], "#p": [client_pk],
                                     "since": int(time.time()) - 30}]
                await ws.send(json.dumps(sub))
                ready.set()
                while not done.is_set():
                    try:
                        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    except asyncio.TimeoutError:
                        continue
                    if m[0] != "EVENT" or len(m) < 3:
                        continue
                    ev = m[2]
                    if ev.get("kind") != 24133:
                        continue
                    got["event"] = ev
                    done.set()
                    return
        except Exception as exc:
            problems.append(f"could not listen on {RELAY}: {type(exc).__name__} {exc}")
            ready.set()
            done.set()

    ready, done = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(listen(ready, done))
    await asyncio.wait_for(ready.wait(), timeout=30)
    if problems:
        _say("SKIP: " + problems[0])
        return 2

    phone = Browser(int(os.environ.get("PC_CHECK_PORT") or 9487),
                    os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-nc-remote", "phone")
    try:
        await phone.start(chrome)
        if not await phone.load(BASE + "/client", "#btn-nsec-login"):
            _say(f"SKIP: no instance at {BASE}")
            return 2
        nsec, pk = fresh_nsec()
        r = await phone.js(f"({PHONE_LOGIN})({json.dumps(nsec)})") or {}
        if not r.get("ok"):
            _say(f"SKIP: could not sign the phone in ({r.get('err')})")
            return 2
        _say(f"[nc] signer is {pk[:16]}…")

        r = await phone.js(f"({PHONE_PASTE})({json.dumps(uri)})") or {}
        if not r.get("ok"):
            problems.append(f"the paste path refused the link: {r.get('err')}")
        else:
            asked = r.get("asked") or []
            _say(f"[nc] dialogs answered: {len(asked)}")
            for a in asked:
                _say(f"[nc]   dialog: {(a or '(empty)')[:220]}")
            if not any("signed in through" in a for a in asked):
                problems.append(
                    "the foreign-relay question never appeared — start() either never ran or took "
                    "the same-relay branch for a relay that is not this instance's")

        try:
            await asyncio.wait_for(done.wait(), timeout=45)
        except asyncio.TimeoutError:
            problems.append(f"no kind-24133 ACK reached {RELAY} within 45s — the app reports a "
                            f"successful pairing but the other side is never told")
    finally:
        done.set()
        try:
            await task
        except Exception:
            pass
        await phone.stop()

    ev = got.get("event")
    if ev:
        _say(f"[nc] ACK from {ev.get('pubkey','')[:16]}… ({len(ev.get('content') or '')} bytes)")
        plain, how = None, None
        for name, fn in (("nip44", lambda: nip44.decrypt(
                             ev["content"], nip44.get_conversation_key(skb, bytes.fromhex(ev["pubkey"])))),
                         ("nip04", lambda: nip04.decrypt(skb, bytes.fromhex(ev["pubkey"]), ev["content"]))):
            try:
                plain, how = fn(), name
                break
            except Exception:
                continue
        if plain is None:
            problems.append("the ACK could not be decrypted with EITHER nip44 or nip04 — the app "
                            "answered in a scheme the client cannot read, which is silent on both sides")
        else:
            _say(f"[nc] ACK decrypted with {how}: {plain[:160]}")
            try:
                body = json.loads(plain)
            except Exception:
                body = {}
            if body.get("result") != secret:
                problems.append(
                    f"the ACK did not carry the secret back (result={body.get('result')!r}, "
                    f"expected {secret!r}) — a nostrconnect client waits for exactly that")

    if problems:
        _say(f"FAIL  {len(problems)} problem(s):")
        for p in problems:
            _say("  " + p)
        return 1
    _say("OK  a primal-shaped remote-signer link pairs: relay allowed, ACK delivered, secret echoed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
