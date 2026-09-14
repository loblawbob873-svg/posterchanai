"""A SUPERSEDED AUTH CHALLENGE MUST COST A RETRY, NOT THE WRITE.

NIP-78 (kind 78/30078) is refused by this relay unless the socket has authenticated as the event's
author, and 30078 is where the app keeps its OWN documents: drafts (`pcai:drafts`), notes, settings,
the desktop layout, the drive index.

MEASURED ON THE LIVE RELAY over six hours: 5,719 successful AUTHs, 43 refusals — and 40 of the 43
were `invalid: AUTH challenge does not match` with signed-for and observed IDENTICAL
(`wss://poster.place/relay` both sides). So it is neither a credential problem nor the proxy: the
relay mints a fresh challenge per connection, and a signature produced across a reconnect is simply
late. The relay says so and issues a new challenge in the same breath.

The publish path gave up anyway — one AUTH attempt per relay, ever, then `auth rejected` — so a
single race threw the document away. For `pcai:drafts` that is exactly the report "lots of my fedi
replies get stuck in drafts": sending a reply removes the draft LOCALLY and publishes the new drafts
document; when that publish is refused the removal never reaches the relay, and the draft comes back
on the next load. The reply itself was posted — no kind-1 or kind-1111 refusal appears anywhere in
24h of relay log — which is why it looks like the draft "never sent".

THE RULE: when an AUTH is refused and the relay has since issued a DIFFERENT challenge, try once
more with that one. Keyed on the challenge rather than a count, so a genuinely rejected key still
fails immediately instead of looping.

This runs the SHIPPED relay.js against a stub relay that reproduces the exact exchange.
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import websockets

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name('stale_challenge_fixture.html')
CHROME = '/usr/bin/google-chrome-stable'


def _serve():
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(ROOT), **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == '/':
                body = FIXTURE.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return super().do_GET()

    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _run():
    srv = _serve()
    profile = tempfile.mkdtemp(prefix='pc-stale-challenge-')
    proc = subprocess.Popen(
        [CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
         '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if os.path.exists(profile + '/DevToolsActivePort'):
                break
            await asyncio.sleep(.1)
        port = open(profile + '/DevToolsActivePort').read().splitlines()[0]
        pages = json.load(urllib.request.urlopen('http://127.0.0.1:%s/json' % port))
        tgt = next(x for x in pages if x.get('type') == 'page')
        async with websockets.connect(tgt['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({'id': n[0], 'method': method, 'params': params or {}}))
                while True:
                    r = json.loads(await asyncio.wait_for(ws.recv(), 30))
                    if r.get('id') == n[0]:
                        return r.get('result', {})

            await call('Page.enable')
            await call('Page.navigate', {'url': 'http://127.0.0.1:%d/' % srv.server_port})
            for _ in range(120):
                r = await call('Runtime.evaluate',
                               {'expression': 'window.__result||null',
                                'returnByValue': True, 'awaitPromise': True})
                v = (r.get('result') or {}).get('value')
                if v:
                    return v
                await asyncio.sleep(.25)
            raise AssertionError('the publish never settled')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        srv.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='no Chrome')
def test_a_superseded_challenge_is_retried_not_fatal():
    got = asyncio.run(_run())
    print('result=%r' % (got,))
    assert got['ok'] and got['accepted'], (
        'a stale AUTH challenge lost the write. The relay refused the first signature as superseded '
        'and issued a fresh challenge; the client gave up instead of using it, so the document — a '
        'draft, a note, a setting — was never stored. frames=%r msg=%r'
        % (got['frames'], got['msg']))
    # It must actually re-authenticate, not merely resend blindly.
    assert 'AUTH:challenge-2' in got['frames'], (
        'the retry did not sign the NEW challenge: %r' % (got['frames'],))
    assert got['frames'].count('AUTH:challenge-2') == 1, (
        'the retry looped instead of trying the fresh challenge once: %r' % (got['frames'],))
